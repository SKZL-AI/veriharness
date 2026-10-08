"""The project storage steward and its hooks (STORAGE-D, P5-30).

The world is `test_storage_tiers.World` (a real registry with injected OS
readers, a canonical repository, owned scratch, a ``tmp_path`` target with
injected probes). Free space is never this machine's: the steward observes a
`FakeAdmission` whose state and free bytes the test chooses -- except in the
park and controller tests, which use B3's real admission on a simulated
``statvfs`` and real run stores, timers and run locks.

Foreign data is a directory under ``tmp_path`` outside every registered root,
and another registered project; the namespace test records every
``open``/``os.open``/``listdir``/``scandir``/``walk`` while the steward runs.

Every test name contains the selector it is run by; every docstring names the
failure (the mutant) it catches and the negative control that changes only the
condition under test. Nothing here removes a file.

Revision 11 item 6 (recorded): Revision 10 item 5 forced a setup change in
`test_candidate_plan_bytes_not_overclaimed` (it registers the target ``t9``
holding the placement, since an extract root under no registered target is
kept); its assertions are unchanged, and the change is authorised
retroactively by Revision 11 item 6.
"""

from __future__ import annotations

import builtins
import errno
import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

import hoh.storage_steward as ss
import hoh.verified_relocation as vr
from hoh import storage_coordinator as sc
from hoh import storage_park as spk
from hoh import storage_tiers as st
from hoh.contracts import Budgets, Condition, Stage
from hoh.controller import Controller
from hoh.storage_coordinator import NEEDS_YOU_PREFIX, CandidatePlan, PlanItem, ProjectSteward
from hoh.store import RunStore
from hoh.timers import TimerStore
from test_storage_tiers import (  # noqa: F401  (isolated_env and w are fixtures)
    BOOT,
    OTHER,
    OTHER_PID,
    PROJECT,
    FakeAdmission,
    GiB,
    World,
    git,
    isolated_env,
    w,
)

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def steward_for(world: World, admission: FakeAdmission, **kw) -> ss.StorageSteward:
    target = kw.pop("target", None)
    policy = kw.pop("policy", None) or ss.StewardPolicy(target_timeout_s=60.0)
    relocation = world.relocation(admission=admission, target=target)
    return ss.StorageSteward(PROJECT, world.registry, admission, relocation, policy, **kw)


def plans(world: World) -> list[Path]:
    directory = world.receipts / "plans"
    return sorted(directory.iterdir()) if directory.exists() else []


def steward_receipts(world: World) -> list[Path]:
    return sorted(world.receipts.glob("steward-*.json"))


def foreign_dirs(world: World, tmp_path: Path) -> tuple[Path, Path, Path]:
    """A foreign directory (no registered root), and a second registered
    project with its own canonical repository and an eligible duplicate."""
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "big.bin").write_bytes(b"\0" * 4096)
    shutil.copytree(world.canon / "data", foreign / "data-copy")
    canon_x = tmp_path / "canon-x"
    (canon_x / "data").mkdir(parents=True)
    (canon_x / "data" / "x.txt").write_text("x\n")
    git(canon_x, "init", "-q")
    git(canon_x, "add", "-A")
    git(canon_x, "commit", "-qm", "x")
    world.registry.register_project(OTHER, canon_x)
    world.registry.open_session(OTHER, "s1", pid=OTHER_PID, boot_id=BOOT)
    scratch_x = world.registry.scratch_dir(OTHER, "s1", "scratch")
    shutil.copytree(canon_x / "data", scratch_x / "dup-x")
    world.fake.procs.pop(OTHER_PID)
    return foreign, scratch_x, canon_x


def tree_digest(path: Path) -> dict[str, str]:
    out = {}
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            full = os.path.join(dirpath, name)
            out[os.path.relpath(full, path)] = st.sha256_file(full)
    return out


# --------------------------------------------------------------------------- #
# K6 -- namespace, foreign low space, state gate, receipt, events
# --------------------------------------------------------------------------- #


def test_own_namespace_only_reads(w, tmp_path, monkeypatch):
    """S22. Catches the namespace filter removed: during a whole steward
    pass that relocates an owned duplicate, no ``open``, ``os.open``,
    ``listdir``, ``scandir`` or ``walk`` touches the foreign directory, the
    other project's scratch or the other project's repository, and the other
    project's eligible duplicate is neither planned nor touched. Negative
    control: the recorder does see the steward reading its own scratch."""
    item = w.copy("dup")
    w.end_setup()
    foreign, scratch_x, canon_x = foreign_dirs(w, tmp_path)
    before = tree_digest(foreign), tree_digest(scratch_x)
    seen: list[str] = []

    def record(fn):
        def wrapper(path=".", *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                seen.append(os.path.abspath(os.fsdecode(path)))
            return fn(path, *args, **kwargs)
        return wrapper

    steward = steward_for(w, FakeAdmission("PRESSURE"))
    for module, name in ((builtins, "open"), (io, "open"), (os, "open"), (os, "listdir"),
                         (os, "scandir"), (os, "walk")):
        monkeypatch.setattr(module, name, record(getattr(module, name)))
    report = steward.run_once()
    monkeypatch.undo()
    assert report["action"] == ss.EXECUTED and not item.exists()
    for forbidden in (foreign, scratch_x, canon_x):
        assert not [p for p in seen if p == str(forbidden) or p.startswith(str(forbidden) + "/")]
    assert any(p.startswith(str(w.scratch)) for p in seen)
    assert (tree_digest(foreign), tree_digest(scratch_x)) == before
    planned = json.loads(Path(report["plan"]["path"]).read_text())["plan"]
    assert [i["path"] for i in planned["items"]] == [str(item)]
    assert all(c.parts[-3] != OTHER for c in steward.candidates())


def test_foreign_low_space_needs_you(w, tmp_path):
    """S22. Catches foreign-caused pressure answered by touching foreign data
    or by silence: in PRESSURE with 10 GiB free under a 30 GiB reserve, the
    steward relocates only its own duplicate, leaves the foreign directory
    and the other project byte-for-byte unchanged, and reports NEEDS_YOU
    with the coordinator's prefix and the missing bytes. Negative control:
    with enough free space the same pass reports no NEEDS_YOU."""
    item = w.copy("dup")
    w.end_setup()
    foreign, scratch_x, _ = foreign_dirs(w, tmp_path)
    before = tree_digest(foreign), tree_digest(scratch_x)
    # Staged on the target (guest staging refused): the reclaimed bytes are
    # the item's own (`test_guest_staging_leftover_reported` nets the guest case).
    admission = FakeAdmission("PRESSURE", free=10 * GiB, admit=False)
    observed = admission.status

    def status():                           # after the pass the state reads better;
        answer = observed()                 # the shortfall alone must decide
        admission.state = "OPTIMIZE"
        return answer

    admission.status = status
    report = steward_for(w, admission).run_once()
    assert report["state"] == "PRESSURE" and report["state_after"] == "OPTIMIZE"
    assert report["needs_you"] is True and report["message"].startswith(NEEDS_YOU_PREFIX)
    assert f"missing {20 * GiB - report['bytes_reclaimed']} bytes" in report["message"]
    # bytes_reclaimed is the pass's net (it may be negative: C2's placement
    # leaves extract roots in the guest); the gross removal is bytes_removed.
    assert report["bytes_removed"] > 0 and not item.exists()
    assert (tree_digest(foreign), tree_digest(scratch_x)) == before
    assert report["shortfall_bytes"] == 20 * GiB
    second = w.copy("dup2")
    calm = steward_for(w, FakeAdmission("OPTIMIZE", free=60 * GiB)).run_once()
    assert calm["needs_you"] is False and second.is_dir()


def test_normal_state_observes_only(w):
    """Catches the state gate removed: in NORMAL the steward only observes --
    no plan file, no steward receipt, the eligible duplicate untouched.
    Negative control: in OPTIMIZE the same pass plans (a plan file and a
    receipt) but still removes nothing."""
    item = w.copy("dup")
    w.end_setup()
    report = steward_for(w, FakeAdmission("NORMAL", free=100 * GiB)).run_once()
    assert report["action"] == ss.OBSERVED
    assert plans(w) == [] and steward_receipts(w) == [] and item.is_dir()
    report = steward_for(w, FakeAdmission("OPTIMIZE", free=60 * GiB)).run_once()
    assert report["action"] == ss.PLANNED and len(plans(w)) == 1
    assert len(steward_receipts(w)) == 1 and item.is_dir()


def test_steward_receipt_written(w):
    """Catches the steward receipt not written (directive section P): an
    executing pass writes ``receipts/steward-<UTC>.json`` with free space
    before and after, VHDX allocation and host volume (NOT_MEASURED without
    STORAGE-F), bytes selected, compressed bytes, readback status, bytes
    reclaimed, items kept and why, and staging leftovers. Negative control:
    a NORMAL pass writes none (`test_normal_state_observes_only`)."""
    item = w.copy("dup")
    notes = w.scratch / "notes"
    notes.mkdir()
    (notes / "n.txt").write_text("unique\n")
    w.end_setup()
    report = steward_for(w, FakeAdmission("PRESSURE", admit=False)).run_once()
    [path] = steward_receipts(w)
    receipt = json.loads(path.read_text())
    for key in ("free_before", "free_after", "vhdx_allocation", "host_volume_free",
                "bytes_selected", "compressed_bytes", "readback_status", "bytes_reclaimed",
                "items_kept", "staging_leftovers", "needs_you", "events", "plan",
                "bytes_removed", "guest_staging_bytes", "extract_root_bytes",
                "extract_roots_pending"):
        assert key in receipt, key
    assert receipt["vhdx_allocation"] == receipt["host_volume_free"] == ss.NOT_MEASURED
    assert receipt["bytes_selected"] == receipt["bytes_removed"] > 0
    assert receipt["bytes_reclaimed"] == (receipt["bytes_removed"]
                                          - receipt["guest_staging_bytes"]
                                          - receipt["extract_root_bytes"]
                                          - receipt["bookkeeping_bytes"])
    assert receipt["compressed_bytes"] > 0
    assert receipt["readback_status"] == [{"path": str(item), "status": "ARCHIVED_VERIFIED"}]
    assert [k["path"] for k in receipt["items_kept"]] == [str(notes)]
    assert receipt["staging_leftovers"][0]["status"] == vr.STAGING_LEFTOVER
    assert report["receipt_path"] == str(path)


def test_each_event_kind_handled(w):
    """Catches one event kind ignored: each of the five kinds recorded by a
    hook is consumed by the next pass and listed as handled; a recorded
    ``forecast_crossing`` makes a NORMAL pass execute, and so does a
    forecast that reaches the reserve within the horizon. Negative controls:
    an unknown kind is refused, and without a forecast a NORMAL pass only
    observes."""
    item = w.copy("dup")
    w.end_setup()
    admission = FakeAdmission("NORMAL", free=100 * GiB)
    steward = steward_for(w, admission)
    first = steward.run_once()
    assert first["action"] == ss.OBSERVED and item.is_dir()
    with pytest.raises(ValueError):
        steward.on_event("disk_full")
    for kind in ss.EVENT_KINDS:
        assert steward.on_event(kind)["recorded"] is True
    report = steward.run_once()
    assert set(ss.EVENT_KINDS) <= set(report["handled"])
    assert report["action"] == ss.EXECUTED and not item.exists()
    again = w.copy("again")
    assert steward.run_once()["action"] == ss.OBSERVED and again.is_dir()
    admission.seconds_to_reserve = 600.0
    report = steward.run_once()
    assert "forecast_crossing" in report["handled"] and not again.exists()


def test_hook_only_records_event(w, monkeypatch):
    """Catches a hook that plans or executes (Revision 6a item 1): `on_event`
    appends exactly one bounded line to the steward queue and returns; no
    plan, no execute, no candidate scan. Without the relocation scratch the
    event is dropped and the answer says so (the hook never creates it).
    Negative control: the command pass, not the hook, plans."""
    item = w.copy("dup")
    w.end_setup()
    steward = steward_for(w, FakeAdmission("PRESSURE"))
    answer = steward.on_event("admission_refused")
    assert answer["recorded"] is False and "does not exist yet" in answer["note"]
    assert not steward.relocation_scratch.exists()
    calls = []
    for name in ("plan", "execute"):
        real = getattr(steward.relocation, name)
        monkeypatch.setattr(steward.relocation, name,
                            lambda *a, _n=name, _r=real, **k: calls.append(_n) or _r(*a, **k))
    monkeypatch.setattr(steward, "candidates", lambda: calls.append("candidates") or [])
    steward._ensure_relocation_scratch()
    assert steward.on_event("terminal_transition", run_id="r1")["recorded"] is True
    lines = (steward.relocation_scratch / ss.EVENTS_NAME).read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["kind"] == "terminal_transition"
    assert len(lines[0]) <= ss.StewardPolicy().max_event_bytes
    assert calls == [] and plans(w) == [] and item.is_dir()
    monkeypatch.undo()
    steward.run_once()
    assert len(plans(w)) == 1


def test_target_operations_time_out(w):
    """Catches a target call without a timeout (Revision 6a item 3): a
    target whose identity probe hangs ends the pass within the steward's
    ``target_timeout_s``; the item is kept (TARGET_TIMEOUT) and the target
    reported. Negative control: a responsive target relocates the item
    (`test_steward_receipt_written`)."""
    item = w.copy("dup")
    w.end_setup()
    target, probe = w.target()
    probe.block = threading.Event()
    steward = steward_for(w, FakeAdmission("PRESSURE"), target=target,
                          policy=ss.StewardPolicy(target_timeout_s=0.5))
    result: dict = {}
    runner = threading.Thread(target=lambda: result.update(steward.run_once()), daemon=True)
    started = time.monotonic()
    runner.start()
    runner.join(30)
    probe.block.set()
    assert not runner.is_alive() and time.monotonic() - started < 30
    [kept] = result["items_kept"]
    assert kept["path"] == str(item) and kept["why"].startswith(vr.TARGET_TIMEOUT)
    assert item.is_dir() and steward.relocation.timeout == 0.5


def test_candidate_plan_no_side_effects(w):
    """Catches a coordinator protocol with side effects or over-claims: the
    steward is a `ProjectSteward`; `candidate_plan` returns `PlanItem`s of
    eligible items only, writes no plan and no event and removes nothing.
    The claim of a duplicate is 0 -- relocating it places a bundle whose
    verification leaves extract roots of its size in the guest
    (`test_candidate_plan_bytes_not_overclaimed`). Negative control: a
    non-eligible item (unique notes) is not offered at all."""
    item = w.copy("dup")
    notes = w.scratch / "notes"
    notes.mkdir()
    (notes / "n.txt").write_text("unique\n")
    w.end_setup()
    steward = steward_for(w, FakeAdmission("PRESSURE"))
    assert isinstance(steward, ProjectSteward)
    plan = steward.candidate_plan({"state": "PRESSURE"})
    assert isinstance(plan, CandidatePlan) and plan.project_id == PROJECT
    assert plan.items == (PlanItem(str(item), 0, st.VERIFIED_DUPLICATE),)
    assert plans(w) == [] and item.is_dir() and notes.is_dir()
    assert not steward.relocation_scratch.exists()


# --------------------------------------------------------------------------- #
# K6 -- the hooks in the park and the controller
# --------------------------------------------------------------------------- #


class Recorder:
    """A steward stand-in for the hooks: records each call, optionally checks
    the store at call time, optionally raises or blocks."""

    def __init__(self, store: RunStore | None = None, *, raises: bool = False) -> None:
        self.store, self.raises = store, raises
        self.calls: list[tuple[str, dict]] = []
        self.seen_state = None
        self.timers_at_call = None

    def on_event(self, kind, **detail):
        if self.store is not None:
            self.seen_state = self.store.read_state()
            try:
                self.timers_at_call = TimerStore(Path(self.store.dir).parent).read()
            except Exception:                   # noqa: BLE001 - no timer store yet
                self.timers_at_call = []
        self.calls.append((kind, detail))
        if self.raises:
            raise RuntimeError("steward exploded")
        return {"recorded": True, "kind": kind, "note": ""}


def park_world(tmp_path: Path):
    """A real run store with a run state, and a real refusal of B3's
    admission on a simulated statvfs (20 GiB free under a 30 GiB reserve)."""
    from test_storage_park import admission as b3_admission

    tmp_path.mkdir(parents=True, exist_ok=True)
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n", encoding="utf-8")
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-q")
    store = RunStore(tmp_path / "runs", "r1")
    state = Controller.new_state(run_id="r1", repo_path=repo, project_name="project",
                                 spec_path=spec, budgets=Budgets(max_iterations=5))
    store.write_state(state)
    state_root = tmp_path / "vh-state"
    adm = b3_admission(state_root, 20)
    refusal = adm.reserve("project", "role_run")
    assert not refusal.admitted
    return store, state, refusal, spec


def test_park_calls_steward_after_durable_park(tmp_path, monkeypatch):
    """Catches the hook before the park is durable: when the steward is told
    ``admission_refused``, the persisted state is already parked
    (``storage_pressure``) and the recheck timer is armed. An ARM_FAILED
    stop records the event flagged ``arm_failed``. Negative control: without
    a steward the park is exactly B3's (parked, nothing recorded)."""
    store, state, refusal, _ = park_world(tmp_path)
    steward = Recorder(store)
    assert spk.park_for_storage_pressure(state, refusal, store=store, governor=None,
                                         policy=spk.ParkPolicy(), now=datetime.now(UTC),
                                         steward=steward) is True
    [(kind, detail)] = steward.calls
    assert kind == "admission_refused" and detail == {}
    assert steward.seen_state.blocked_kind == spk.STORAGE_CLASS
    assert steward.seen_state.condition == Condition.BLOCKED
    assert len(steward.timers_at_call) == 1
    store2, state2, refusal2, _ = park_world(tmp_path / "b")
    assert spk.park_for_storage_pressure(state2, refusal2, store=store2, governor=None,
                                         policy=spk.ParkPolicy(),
                                         now=datetime.now(UTC)) is True
    assert store2.read_state().blocked_kind == spk.STORAGE_CLASS
    store3, state3, refusal3, _ = park_world(tmp_path / "c")

    def cannot_arm(self, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(spk.TimerStore, "arm_new", cannot_arm)
    failing = Recorder(store3)
    assert spk.park_for_storage_pressure(state3, refusal3, store=store3, governor=None,
                                         policy=spk.ParkPolicy(), now=datetime.now(UTC),
                                         steward=failing) is False
    assert failing.calls == [("admission_refused", {"arm_failed": True})]
    assert failing.seen_state.stop_reason == spk.ARM_FAILED


def test_steward_failure_never_fails_the_park(tmp_path):
    """Catches a steward exception propagated: a steward that raises leaves
    the park durable and successful, and the failure is noted in the
    persisted state. Negative control: a steward that records leaves no such
    note."""
    store, state, refusal, _ = park_world(tmp_path)
    assert spk.park_for_storage_pressure(state, refusal, store=store, governor=None,
                                         policy=spk.ParkPolicy(), now=datetime.now(UTC),
                                         steward=Recorder(raises=True)) is True
    persisted = store.read_state()
    assert persisted.blocked_kind == spk.STORAGE_CLASS
    assert any("steward exploded" in n for n in persisted.history)
    store2, state2, refusal2, _ = park_world(tmp_path / "b")
    spk.park_for_storage_pressure(state2, refusal2, store=store2, governor=None,
                                  policy=spk.ParkPolicy(), now=datetime.now(UTC),
                                  steward=Recorder())
    assert not any("steward" in n for n in store2.read_state().history)


def test_terminal_transition_calls_steward(tmp_path, monkeypatch):
    """Catches the controller hook missing: an iteration that persists a
    CHECKPOINTED state tells the steward ``terminal_transition`` once, after
    the run lock is released. Negative controls: an iteration that stays
    non-terminal tells nothing, and a controller without a steward emits no
    event and runs as before."""
    store, state, _, spec = park_world(tmp_path)
    steward = Recorder()
    controller = Controller(store, None, spec_path=spec, steward=steward)
    lock_free = []

    def iteration(self, st_):
        st_.stage = Stage.DEVELOPING
        self.store.write_state(st_)
        return "developing"

    monkeypatch.setattr(Controller, "_run_iteration_locked", iteration)
    controller.run_iteration(state)
    assert steward.calls == []

    def checkpoint(self, st_):
        st_.stage = Stage.CHECKPOINTED
        self.store.write_state(st_)
        return "checkpointed"

    real_tell = Controller._tell_steward_terminal

    def tell(self, st_, before):
        with self.store.lock():                 # the run lock is free again
            lock_free.append(True)
        return real_tell(self, st_, before)

    monkeypatch.setattr(Controller, "_run_iteration_locked", checkpoint)
    monkeypatch.setattr(Controller, "_tell_steward_terminal", tell)
    assert controller.run_iteration(state) == "checkpointed"
    assert steward.calls == [("terminal_transition", {})] and lock_free == [True]
    plain = Controller(store, None, spec_path=spec)
    assert plain.steward is None and plain.run_iteration(state) == "checkpointed"


def test_blocking_steward_never_holds_the_run_lock(w, tmp_path, monkeypatch):
    """Catches an unbounded queue write under the run lock (Revision 6a item
    4): a steward whose append blocks is given up after
    ``hook_timeout_s``; the park returns, the drop is noted, and the run
    lock can be taken at once (as `resume_storage` takes it). Negative
    control: a steward whose append works records the event."""
    w.end_setup()
    steward = steward_for(w, FakeAdmission("PRESSURE"),
                          policy=ss.StewardPolicy(hook_timeout_s=0.3))
    steward._ensure_relocation_scratch()
    gate = threading.Event()
    real_append = ss._append_event
    monkeypatch.setattr(ss, "_append_event", lambda path, line: gate.wait(60))
    store, state, refusal, _ = park_world(tmp_path / "park")
    started = time.monotonic()
    try:
        with store.lock():
            assert spk.park_for_storage_pressure(
                state, refusal, store=store, governor=None, policy=spk.ParkPolicy(),
                now=datetime.now(UTC), steward=steward) is True
        elapsed = time.monotonic() - started
        with store.lock():
            pass
    finally:
        gate.set()
    assert elapsed < 10
    assert any("admission_refused dropped" in n for n in store.read_state().history)
    monkeypatch.setattr(ss, "_append_event", real_append)
    assert steward.on_event("admission_refused")["recorded"] is True


def test_event_cursor_append_only(w):
    """Catches a consumed event read twice, or a cursor that is rewritten in
    place: each queued event is handled by exactly one pass, and the cursor
    file only grows (every earlier line stays). Negative control: a pass
    with nothing queued handles only its own ``periodic``."""
    w.end_setup()
    steward = steward_for(w, FakeAdmission("NORMAL", free=100 * GiB))
    steward._ensure_relocation_scratch()
    steward.on_event("threshold_changed")
    assert steward.run_once()["events"] == ["periodic", "threshold_changed"]
    cursor = steward.relocation_scratch / ss.CURSOR_NAME
    first = cursor.read_text()
    assert steward.run_once()["events"] == ["periodic"]
    steward.on_event("terminal_transition")
    assert steward.run_once()["events"] == ["periodic", "terminal_transition"]
    after = cursor.read_text()
    assert after.startswith(first) and len(after.splitlines()) == 2


def namespace_bytes(world: World) -> int:
    """Bytes of the regular files in the project's scratch namespace (the
    items and the relocation scratch with its guest staging) -- not the
    receipts, which are bookkeeping."""
    total = 0
    for dirpath, _, filenames in os.walk(world.registry.state_root / "projects" / PROJECT
                                         / "scratch"):
        for name in filenames:
            info = os.lstat(os.path.join(dirpath, name))
            if not os.path.islink(os.path.join(dirpath, name)):
                total += info.st_size
    return total


def test_guest_staging_leftover_reported(w):
    """Catches a steward receipt that lists target leftovers only: with
    guest staging admitted, the staged bundle left in the guest is listed in
    the steward receipt's staging_leftovers (where=guest, path, bytes) and
    bytes_reclaimed is net of it. Negative control: with staging refused the
    leftover is the target's and bytes_reclaimed is the item's size."""
    item = w.copy("dup")
    w.end_setup()
    report = steward_for(w, FakeAdmission("PRESSURE", admit=True)).run_once()
    [leftover] = report["staging_leftovers"]
    assert leftover["where"] == "guest" and leftover["status"] == vr.STAGING_LEFTOVER
    assert Path(leftover["path"]).is_dir() and not item.exists()
    assert report["guest_staging_bytes"] == leftover["bytes"] > 0
    assert report["bytes_reclaimed"] == (report["bytes_removed"] - leftover["bytes"]
                                         - report["extract_root_bytes"]
                                         - report["bookkeeping_bytes"])
    assert json.loads(Path(report["receipt_path"]).read_text())["staging_leftovers"] == [leftover]
    second = w.copy("dup2")
    calm = steward_for(w, FakeAdmission("PRESSURE", admit=False)).run_once()
    assert [x["where"] for x in calm["staging_leftovers"]] == ["target"] and not second.exists()
    assert calm["guest_staging_bytes"] == 0
    assert calm["bytes_reclaimed"] == (calm["bytes_removed"] - calm["extract_root_bytes"]
                                       - calm["bookkeeping_bytes"])
    assert calm["bytes_removed"] == len("alpha\n") + len("beta\n")


def test_candidate_plan_bytes_not_overclaimed(w):
    """Catches the full item size claimed (the coordinator's trust
    obligation, E-gap-fae8d7db): the bytes `candidate_plan` claims never
    exceed what the execute that follows actually frees in the project's
    namespace, measured before and after -- with guest staging admitted and
    with staging on the target, an archived duplicate frees nothing net
    (C2's placement leaves extract roots of its size in the guest), so its
    claim is 0, and the steward's bytes_reclaimed equals the measured net
    exactly. Negative control: an extract root released on its committed
    receipt (no bundle, no staging, no placement) is claimed at its full
    size, and exactly that is freed -- the relocation journal's own growth
    beside relocation.lock measured apart (it is the steward's
    bookkeeping_bytes, `test_bytes_reclaimed_net_of_journal_growth`)."""
    from test_verified_relocation import _extract_world

    w.commit("big/blob.txt", "compressible line\n" * 60000)
    for admit, name in ((True, "big-guest"), (False, "big-target")):
        item = w.copy(name, rel="big")
        w.end_setup()
        steward = steward_for(w, FakeAdmission("PRESSURE", admit=admit))
        steward._ensure_relocation_scratch()
        [offer] = steward.candidate_plan({"state": "PRESSURE"}).items
        assert offer.path == str(item)
        before = namespace_bytes(w)
        report = steward.run_once()
        freed = before - namespace_bytes(w)
        assert report["action"] == ss.EXECUTED and not item.exists()
        assert offer.bytes <= max(freed, 0), (admit, offer.bytes, freed)
        assert report["bytes_reclaimed"] == freed, (admit, report["bytes_reclaimed"], freed)
        assert report["bytes_removed"] == 60000 * len("compressible line\n")
        assert {r["status"] for r in report["extract_roots_pending"]} == {
            st.AWAITING_COMMITTED_RECEIPT}
    w2 = World(w.tmp.parent / "w2")
    root, _ = _extract_world(w2)
    # The target holding the placement is registered (Revision 10 item 5).
    steward = steward_for(w2, FakeAdmission("PRESSURE", admit=True), target=w2.target("t9")[0])
    steward._ensure_relocation_scratch()            # its owner record exists from pass one
    claims = {i.path: i.bytes for i in steward.candidate_plan({"state": "PRESSURE"}).items}
    assert claims[str(root)] > 0
    rel = steward.relocation
    plan = rel.plan(PROJECT, [root])
    # The relocation's own journal stays append-only in the project's scratch
    # beside relocation.lock (Revision 8 item 4): its growth is bookkeeping,
    # reported as the steward's bookkeeping_bytes and pinned by
    # test_bytes_reclaimed_net_of_journal_growth -- measured here on its own,
    # so the item's claim is compared with what the item freed.
    journal = steward.relocation_scratch / vr.INTENTS_NAME
    journal_before = journal.stat().st_size if journal.exists() else 0
    before = namespace_bytes(w2)
    outcome = rel.execute(plan.path)
    assert by_status(outcome, root) == vr.RELOCATED
    growth = journal.stat().st_size - journal_before
    assert growth > 0
    assert claims[str(root)] == before - namespace_bytes(w2) + growth == plan.items[0]["bytes"]


def by_status(report, path) -> str:
    return next(i["status"] for i in report.items if i["path"] == str(path))


# --------------------------------------------------------------------------- #
# K14 -- --once, unobserved states, the unit (Revision 8 item 4)
# --------------------------------------------------------------------------- #


def test_cli_requires_once(w, tmp_path, monkeypatch, capsys):
    """Catches the ``--once`` flag ignored: ``hoh storage steward --project
    P`` without ``--once`` refuses with exit 2, says the timer is the only
    scheduler and runs no pass; with ``--once`` it runs exactly one pass and
    exits 0. Mutant: the flag ignored (a pass runs without it). Negative
    control: the same command with ``--once`` -- the one the service unit
    runs -- passes once."""
    from hoh import cli

    canon_c = tmp_path / "canon-c"
    canon_c.mkdir()
    (canon_c / "README").write_text("c\n")
    git(canon_c, "init", "-q")
    git(canon_c, "add", "-A")
    git(canon_c, "commit", "-qm", "c")
    w.registry.register_project("pc", canon_c)
    root = w.registry.state_root
    passes = []

    def one_pass(self, **kw):
        passes.append((self.project_id, kw))
        return {"action": ss.OBSERVED, "project_id": self.project_id}

    monkeypatch.setattr(cli, "storage_admission_for",
                        lambda option, **kw: (FakeAdmission("NORMAL", free=100 * GiB), root))
    monkeypatch.setattr(ss.StorageSteward, "run_once", one_pass)
    code = cli.main(["storage", "steward", "--project", "pc", "--storage-root", str(root)])
    err = capsys.readouterr().err
    assert code == 2 and passes == []
    assert "--once" in err and "timer is the only scheduler" in err
    code = cli.main(["storage", "steward", "--project", "pc", "--once",
                     "--storage-root", str(root)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert passes == [("pc", {"plan_only": False, "events": ("periodic",)})]
    assert json.loads(out)["steward"]["project_id"] == "pc"


class RaisingAdmission(FakeAdmission):
    def status(self) -> dict:
        raise OSError(5, "statvfs failed (simulated)")


def test_unobserved_state_executes_nothing(w):
    """Catches an unknown admission state mapped to CRITICAL (Revision 8
    item 4): a ``status()`` that raises and a state string the steward does
    not know are both UNOBSERVED -- no plan, no execute, the eligible
    duplicate untouched, and a steward receipt with ``state: UNOBSERVED``
    and the error. Mutant: unknown mapped to CRITICAL (it plans and
    executes). Negative control: the same item in PRESSURE is relocated."""
    item = w.copy("dup")
    w.end_setup()
    for admission, error in ((RaisingAdmission("PRESSURE"), "statvfs failed"),
                             (FakeAdmission("SEVERE"), "SEVERE")):
        before = steward_receipts(w)
        report = steward_for(w, admission).run_once()
        assert report["state"] == ss.UNOBSERVED and report["action"] == ss.UNOBSERVED
        assert error in report["error"] and report["needs_you"] is True
        assert report["plan"] is None and report["execute"] is None
        assert plans(w) == [] and (item / "a.txt").read_text() == "alpha\n"
        [path] = [p for p in steward_receipts(w) if p not in before]
        on_disk = json.loads(path.read_text())
        assert on_disk["state"] == ss.UNOBSERVED and error in on_disk["error"]
    report = steward_for(w, FakeAdmission("PRESSURE")).run_once()
    assert report["action"] == ss.EXECUTED and not item.exists()


def test_unobserved_state_keeps_events(w):
    """Catches queued events consumed before ``status()`` is read: a pass
    whose state is UNOBSERVED leaves the recorded events in the queue (the
    cursor does not move), and the next observed pass handles them. Mutant:
    events consumed before the state is observed (the next pass never sees
    them). Negative control: the observed pass consumes them once."""
    w.end_setup()
    admission = FakeAdmission("NORMAL", free=100 * GiB)
    steward = steward_for(w, admission)
    steward._ensure_relocation_scratch()
    assert steward.on_event("threshold_changed")["recorded"] is True
    assert steward.on_event("terminal_transition")["recorded"] is True
    cursor = steward.relocation_scratch / ss.CURSOR_NAME
    blind = ss.StorageSteward(PROJECT, w.registry, RaisingAdmission(), steward.relocation,
                              steward.policy)
    report = blind.run_once()
    assert report["state"] == ss.UNOBSERVED and report["events"] == ["periodic"]
    assert not cursor.exists()
    report = steward.run_once()
    assert report["events"] == ["periodic", "threshold_changed", "terminal_transition"]
    assert steward.run_once()["events"] == ["periodic"]


def _unit_sections(path: Path) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            sections.setdefault(current, [])
            continue
        assert current is not None, line
        sections[current].append(stripped)
    return sections


def test_service_unit_has_no_install_section():
    """Catches the service unit installable on its own (Revision 8 item 4):
    ``ops/hoh-storage-steward@.service`` has no ``[Install]`` section -- only
    its timer starts it -- and runs exactly one pass (``--once``), while
    ``ops/hoh-storage-steward@.timer`` keeps its own ``[Install]``
    (``WantedBy=timers.target``). Mutant: the ``[Install]`` section back in
    the service. Negative control: the timer's ``[Install]`` is found by the
    same parser."""
    ops = Path(__file__).resolve().parent.parent / "ops"
    service = _unit_sections(ops / "hoh-storage-steward@.service")
    timer = _unit_sections(ops / "hoh-storage-steward@.timer")
    assert sorted(service) == ["Service", "Unit"]
    [command] = [line for line in service["Service"] if line.startswith("ExecStart=")]
    assert "storage steward --project %i --once" in command
    assert "Install" in timer and "WantedBy=timers.target" in timer["Install"]
    assert sorted(timer) == ["Install", "Timer", "Unit"]


# --------------------------------------------------------------------------- #
# iteration 2 -- the net of bytes_reclaimed includes the journal's growth
# --------------------------------------------------------------------------- #


def test_bytes_reclaimed_net_of_journal_growth(w):
    """Catches the journal's growth left out of the net: the relocation's
    ``intents.jsonl`` stays append-only beside ``relocation.lock`` in the
    project's scratch (Revision 8 item 4), so every relocated item grows it
    (intent, removal_started, completion); after one optimisation pass with
    a relocated item, ``bytes_reclaimed`` equals the measured decrease of
    the project's scratch namespace and ``bookkeeping_bytes`` equals the
    journal's growth. Mutant: bookkeeping not subtracted (bytes_reclaimed
    over-claims by exactly the journal's growth). Negative control: the
    pass's other terms are those the existing net already had --
    bytes_removed is the item's size and no guest staging was left."""
    item = w.copy("dup")
    w.end_setup()
    steward = steward_for(w, FakeAdmission("PRESSURE", admit=False))
    steward._ensure_relocation_scratch()
    journal = steward.relocation_scratch / vr.INTENTS_NAME
    cursor = steward.relocation_scratch / ss.CURSOR_NAME

    def size(path: Path) -> int:
        return path.stat().st_size if path.exists() else 0

    journal_before, cursor_before = size(journal), size(cursor)
    before = namespace_bytes(w)
    report = steward.run_once()
    freed = before - namespace_bytes(w)
    assert report["action"] == ss.EXECUTED and not item.exists()
    assert report["execute"]["status"] == vr.DONE
    growth = size(journal) - journal_before
    assert growth > 0 and size(cursor) == cursor_before
    assert report["bookkeeping_bytes"] == growth
    assert report["bytes_reclaimed"] == freed, (report["bytes_reclaimed"], freed)
    assert report["bytes_removed"] == len("alpha\n") + len("beta\n")
    assert report["guest_staging_bytes"] == 0
    assert report["bytes_reclaimed"] == (report["bytes_removed"] - report["extract_root_bytes"]
                                         - report["bookkeeping_bytes"])
    receipt = json.loads(Path(report["receipt_path"]).read_text())
    assert receipt["bookkeeping_bytes"] == growth and receipt["bytes_reclaimed"] == freed


# --------------------------------------------------------------------------- #
# K18 -- Revision 9 items 1 and 7: the report's fields stay apart
# --------------------------------------------------------------------------- #


def test_report_fields_separate(w, monkeypatch):
    """Catches the report's fields summed into each other (sstew2-b M3,
    Revision 9 items 1 and 7). A first pass (guest staging refused) fails a
    target-side build after its payload and leaves its ``.building``
    directory on the target; a second pass (guest staging admitted)
    relocates the same item and reports, each on its own: the earlier
    build's leftover on the target in ``target_leftovers`` and
    ``target_leftover_bytes``; this pass's guest bundle in
    ``staging_leftovers`` and ``guest_staging_bytes`` (exactly its bytes);
    the placement scratch in ``extract_root_bytes`` (exactly its measured
    bytes); the journal's growth in ``bookkeeping_bytes``; and
    ``bytes_reclaimed = bytes_removed - guest_staging_bytes -
    extract_root_bytes - bookkeeping_bytes``. Mutants: bookkeeping added into
    ``extract_root_bytes``; target leftovers summed into
    ``guest_staging_bytes``. Negative control: the first pass, which built
    nothing earlier, reports no target leftover."""
    import hoh.archive_bundle as ab

    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()

    def full(source, bundle_dir):
        raise OSError(28, "No space left on device (simulated)")

    monkeypatch.setattr(ab, "_after_payload_hook", full)
    first = steward_for(w, FakeAdmission("PRESSURE", admit=False), target=target).run_once()
    [left] = first["staging_leftovers"]
    assert left["where"] == "target" and left["bytes"] > 0 and item.is_dir()
    assert first["target_leftovers"] == [] and first["target_leftover_bytes"] == 0
    monkeypatch.setattr(ab, "_after_payload_hook", None)
    second = steward_for(w, FakeAdmission("PRESSURE", admit=True), target=target).run_once()
    assert second["action"] == ss.EXECUTED and not item.exists()
    [onto] = second["target_leftovers"]
    assert (onto["path"], onto["status"], onto["target_id"]) == (
        left["path"], vr.STAGING_LEFTOVER, target.target_id)
    assert second["target_leftover_bytes"] == onto["bytes"] == left["bytes"]
    [guest] = second["staging_leftovers"]
    assert guest["where"] == "guest" and guest["bytes"] > 0
    assert second["guest_staging_bytes"] == guest["bytes"]
    placement = Path(second["extract_roots_pending"][0]["path"]).parent
    assert second["extract_root_bytes"] == vr._tree_bytes(placement) > 0
    assert second["bookkeeping_bytes"] > 0
    assert second["bytes_reclaimed"] == (second["bytes_removed"] - second["guest_staging_bytes"]
                                         - second["extract_root_bytes"]
                                         - second["bookkeeping_bytes"])
    on_disk = json.loads(Path(second["receipt_path"]).read_text())
    for key in ("target_leftovers", "target_leftover_bytes", "guest_staging_bytes",
                "extract_root_bytes", "bookkeeping_bytes", "bytes_reclaimed"):
        assert on_disk[key] == second[key], key


# --------------------------------------------------------------------------- #
# K20 -- Revision 10 items 2, 3 and 6: records, listings, archive-targets.json
# --------------------------------------------------------------------------- #


def test_unreadable_receipt_reported(w):
    """Catches an unreadable receipt skipped silently, or a stranded
    temporary file not listed (sstew4spec-b M5, Revision 10 item 2). Cases:

    * a stranded ``.reclaim-<id>.json.tmp-<hex>`` (a pass died before
      publishing) is listed ``STRANDED_TEMP`` with path and size, and does
      not set NEEDS_YOU by itself;
    * a temporary name that is a second link of its published receipt (the
      link fallback) is not listed;
    * a torn ``reclaim-<id>.json`` is listed ``UNREADABLE`` and the report is
      NEEDS_YOU.

    Mutants: the unreadable receipt skipped silently; stranded temporary
    files not listed. Negative control: the published receipt itself is
    not listed."""
    w.end_setup()
    steward = steward_for(w, FakeAdmission("NORMAL", free=100 * GiB))
    stranded = w.receipts / ".reclaim-rr-20261002t100000z-00000001.json.tmp-0a1b2c3d"
    stranded.write_bytes(b'{"schema": "veriharness.recl')
    good = w.receipts / "reclaim-rr-20261002t100000z-00000002.json"
    vr.write_new(good, json.dumps({"schema": vr.RECEIPT_SCHEMA, "status": vr.KEPT,
                                   "receipt_id": "rr-20261002t100000z-00000002",
                                   "path": str(w.scratch / "gone"),
                                   "retained_originals": []}).encode())
    os.link(good, w.receipts / f".{good.name}.tmp-0000beef")
    report = steward.run_once()
    assert report["unreadable_receipts"] == [
        {"status": vr.STRANDED_TEMP, "path": str(stranded), "bytes": stranded.stat().st_size}]
    assert report["needs_you"] is False
    torn = w.receipts / "reclaim-rr-20261002t100000z-00000003.json"
    torn.write_bytes(b'{"schema": "veriharness.reclaim-receipt/1", "sta')
    report = steward.run_once()
    unreadable = [e for e in report["unreadable_receipts"] if e["status"] == vr.UNREADABLE]
    assert [e["path"] for e in unreadable] == [str(torn)] and report["needs_you"] is True
    assert {e["status"] for e in report["unreadable_receipts"]} == {
        vr.UNREADABLE, vr.STRANDED_TEMP}


def test_steward_receipt_written_whole(w, monkeypatch):
    """Catches a steward receipt written with one `os.write` whose return
    value is ignored (sstew3-b M1, Revision 10 item 2): while every
    `os.write` into the steward receipt (or its temporary file) writes at
    most 7 bytes, the receipt parses and equals the report. Mutant: one
    `os.write`, its return value ignored (7 bytes land). Negative control:
    other files are written without the limit."""
    item = w.copy("dup")
    w.end_setup()
    steward = steward_for(w, FakeAdmission("OPTIMIZE", free=60 * GiB))
    real = os.write
    limited: list[int] = []

    def short(fd, data):
        try:
            name = os.path.basename(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            name = ""
        if "steward-" in name:
            limited.append(len(data))
            return real(fd, bytes(data)[:7])
        return real(fd, data)

    monkeypatch.setattr(os, "write", short)
    try:
        report = steward.run_once()
    finally:
        monkeypatch.setattr(os, "write", real)
    assert report["action"] == ss.PLANNED and item.is_dir() and limited
    on_disk = json.loads(Path(report["receipt_path"]).read_text())
    expected = json.loads(json.dumps({k: v for k, v in report.items() if k != "receipt_path"},
                                     default=str))
    assert on_disk == expected


def test_failed_execute_needs_you(w, monkeypatch):
    """Catches a ``FAILED`` execute not judged like ``REFUSED``, or its items
    not aggregated (Revision 10 item 2):

    * pass 1, in PRESSURE: two duplicates are relocated, then closing the
      place session raises `RuntimeError` -- ``execute.status`` is
      ``FAILED``, the report NEEDS_YOU, and ``bytes_removed`` counts both
      relocated items;
    * pass 2: the work fails before any item (an ``after_lock`` hook raises
      `RuntimeError`), the state reads OPTIMIZE after the pass and nothing
      is short or negative -- so only the ``FAILED`` execute can make the
      report NEEDS_YOU, and it does.

    Mutants: FAILED not NEEDS_YOU (pass 2); the items of a FAILED report not
    aggregated (pass 1). Negative control: a pass with nothing to execute in
    the same admission is not NEEDS_YOU."""
    from test_verified_relocation import _failing_place_close

    one, two = w.copy("dup-1"), w.copy("dup-2")
    w.end_setup()

    def admission_reading_better_after() -> FakeAdmission:
        admission = FakeAdmission("PRESSURE", free=100 * GiB, admit=False)
        observed = admission.status

        def status():                   # after the pass the state reads OPTIMIZE,
            answer = observed()         # and there is no shortfall
            admission.state = "OPTIMIZE"
            return answer

        admission.status = status
        return admission

    with monkeypatch.context() as patch:
        _failing_place_close(w, patch)
        report = steward_for(w, admission_reading_better_after()).run_once()
    assert report["execute"]["status"] == vr.FAILED and report["needs_you"] is True
    assert report["bytes_removed"] == 2 * (len("alpha\n") + len("beta\n"))
    assert not one.exists() and not two.exists()
    calm = steward_for(w, admission_reading_better_after()).run_once()
    assert calm["execute"] is None and calm["needs_you"] is False, calm["message"]
    three = w.copy("dup-3")

    def explode(plan_path, project_id):
        raise RuntimeError("exploded inside the work (simulated)")

    monkeypatch.setitem(vr._hooks, "after_lock", explode)
    report = steward_for(w, admission_reading_better_after()).run_once()
    assert report["execute"]["status"] == vr.FAILED and three.is_dir()
    assert report["bytes_reclaimed"] == 0 and report["shortfall_bytes"] == 0
    assert report["state_after"] == "OPTIMIZE" and report["needs_you"] is True


def _relocated_on_target(world: World):
    """An item relocated with its bundle staged on the target (guest staging
    refused): the staged bundle stays there, labelled with that receipt's
    id. Returns the target and that bundle."""
    item = world.copy("dup")
    world.end_setup()
    target, _ = world.target()
    report = steward_for(world, FakeAdmission("PRESSURE", admit=False),
                         target=target).run_once()
    assert report["action"] == ss.EXECUTED and not item.exists()
    [left] = report["staging_leftovers"]
    assert left["where"] == "target"
    return target, Path(left["path"])


def test_target_leftovers_reported_in_every_state(w):
    """Catches target leftovers listed only where an item reaches `_archive`,
    or skipped in an UNOBSERVED pass (sstew3-b M2, Revision 10 item 3): a
    labelled bundle of an earlier, relocated item lies on the target, so no
    item of the later passes reaches `_archive`; passes in NORMAL, OPTIMIZE,
    PRESSURE, CRITICAL and UNOBSERVED (``status()`` raising) each report it
    exactly once in ``target_leftovers``, with its bytes in
    ``target_leftover_bytes``, and it sets no NEEDS_YOU by itself. Mutants:
    the listing only in `_archive`; the listing skipped in the UNOBSERVED
    pass. Negative control: the bundle stays on the target."""
    target, bundle = _relocated_on_target(w)
    size = vr._tree_bytes(bundle)
    assert size > 0
    for admission in (FakeAdmission("NORMAL", free=100 * GiB),
                      FakeAdmission("OPTIMIZE", free=100 * GiB),
                      FakeAdmission("PRESSURE", free=100 * GiB),
                      FakeAdmission("CRITICAL", free=100 * GiB),
                      RaisingAdmission("PRESSURE")):
        report = steward_for(w, admission, target=target).run_once()
        assert [(x["target_id"], x["status"], x["path"]) for x in report["target_leftovers"]] \
            == [(target.target_id, vr.STAGING_LEFTOVER, str(bundle))], report["state"]
        assert report["target_leftover_bytes"] == size
        assert report["target_leftovers_unmeasured"] == []
        if report["state"] in ("NORMAL", "OPTIMIZE"):
            assert report["needs_you"] is False, report["state"]
    assert bundle.is_dir()


def test_same_pass_kept_item_leftover_reported_once(w, monkeypatch):
    """Catches a leftover counted twice -- by the pass-level listing and by
    the item that the same pass kept (sstew4spec-b B2, Revision 10 item 3):
    PRESSURE, guest staging refused, one target; a candidate whose earlier
    build left a labelled ``.building`` directory there is ``KEPT``
    LEFTOVER_PRESENT and its own ReclaimReceipt's ``target_leftovers`` names
    the directory; the report lists the path once and counts its bytes once.
    Mutant: the pass-level list and the items' lists both added. Negative
    control: the item is intact."""
    import hoh.archive_bundle as ab

    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    monkeypatch.setattr(ab, "_after_payload_hook",
                        lambda source, bundle_dir: (_ for _ in ()).throw(
                            OSError(28, "No space left on device (simulated)")))
    first = steward_for(w, FakeAdmission("PRESSURE", admit=False), target=target).run_once()
    [left] = first["staging_leftovers"]
    monkeypatch.setattr(ab, "_after_payload_hook", None)
    report = steward_for(w, FakeAdmission("PRESSURE", admit=False), target=target).run_once()
    [kept] = report["items_kept"]
    assert kept["path"] == str(item) and vr.LEFTOVER_PRESENT in kept["why"]
    receipts = [json.loads(p.read_text()) for p in w.receipts.glob("reclaim-*.json")]
    [mine] = [r for r in receipts if vr.LEFTOVER_PRESENT in (r.get("reason") or "")]
    assert [x["path"] for x in mine["target_leftovers"]] == [left["path"]]
    assert [x["path"] for x in report["target_leftovers"]] == [left["path"]]
    assert report["target_leftover_bytes"] == left["bytes"] > 0
    assert (item / "a.txt").read_text() == "alpha\n"


def test_unmeasured_listing_aggregated(w):
    """Catches an unmeasurable staging listing not aggregated (sstew3-b QA
    gap 1, Revision 10 item 3): when the listing of one target fails (its
    ``staging`` directory is mode 000, so its ``openat`` is refused), the
    report's ``target_leftovers_unmeasured`` names that target. Mutant: not
    aggregated. Negative control: the other, readable target is not
    named."""
    w.end_setup()
    t1, _ = w.target("t1")
    t2, _ = w.target("t2")
    admission = FakeAdmission("NORMAL", free=100 * GiB)
    relocation = vr.VerifiedRelocation(
        w.registry, [t1, t2], None, w.receipts, admission=admission,
        sources=lambda project: w.sources(project), boot_id=w.fake.boot_id, timeout=60.0)
    steward = ss.StorageSteward(PROJECT, w.registry, admission, relocation,
                                ss.StewardPolicy(target_timeout_s=60.0))
    locked = t2.root / "staging"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        report = steward.run_once()
    finally:
        locked.chmod(0o755)
    assert [u["target_id"] for u in report["target_leftovers_unmeasured"]] == ["t2"]
    assert report["target_leftovers"] == []


def _malformed_cases(tmp_path: Path, valid_root: Path) -> list[tuple[str, str]]:
    valid = {"target_id": "t1", "root": str(valid_root), "failure_domain": "domain-t1",
             "expected_identity": {"serial": "S-t1"}}
    return [
        ("invalid-json", '[{"target_id": "t1",'),
        ("not-a-list", json.dumps({"targets": [valid]})),
        ("not-a-mapping", json.dumps([valid, "t2"])),
        ("relative-root", json.dumps([valid, dict(valid, target_id="t2", root="relative/t2")])),
        ("invalid-target-id", json.dumps([valid, dict(valid, target_id="../bad")])),
        ("duplicate-target-id", json.dumps([valid, dict(valid, root=str(tmp_path / "t1b"))])),
    ]


def _problem_relocation(world: World, targets, problems, admission) -> vr.VerifiedRelocation:
    from test_storage_tiers import RELOC_PID
    return vr.VerifiedRelocation(
        world.registry, targets, None, world.receipts, admission=admission,
        sources=lambda project: world.sources(project), getpid=lambda: RELOC_PID,
        boot_id=world.fake.boot_id, timeout=60.0, target_problems=problems)


def test_malformed_archive_targets_refuses_relocation(w, tmp_path):
    """Catches a malformed ``archive-targets.json`` skipped silently (sstew3-a
    M4, Revision 10 item 6). Through `cli._archive_targets` and a steward
    pass in PRESSURE, for each case -- invalid JSON; a non-list; an entry
    that is not a mapping; an entry with a relative ``root``; an entry with
    an invalid ``target_id``; a duplicate ``target_id`` beside a valid entry
    -- the problems are not empty, every candidate is rejected
    ``TARGET_UNMEASURABLE``, nothing is executed, ``target_config_problems``
    lists them and the report is NEEDS_YOU. Mutants: the refused entry
    skipped silently; an unreadable file read as no targets; a relative
    root accepted. Negative control: no file -- no targets, no problem."""
    from hoh import cli

    item = w.copy("dup")
    w.end_setup()
    valid_root = w.tmp / "target-t1"
    for n, (case, text) in enumerate(_malformed_cases(tmp_path, valid_root)):
        root = tmp_path / f"case-{n}-{case}"
        root.mkdir()
        (root / cli.ARCHIVE_TARGETS_FILE).write_text(text, encoding="utf-8")
        targets, problems = cli._archive_targets(root, w.registry, "steward")
        assert problems, case
        admission = FakeAdmission("PRESSURE", free=100 * GiB)
        steward = ss.StorageSteward(PROJECT, w.registry, admission,
                                    _problem_relocation(w, targets, problems, admission),
                                    ss.StewardPolicy(target_timeout_s=60.0))
        report = steward.run_once()
        assert report["execute"] is None and item.is_dir(), case
        assert [k["path"] for k in report["items_kept"]] == [str(item)], case
        assert all(k["why"].startswith(vr.TARGET_UNMEASURABLE + ": archive-targets.json")
                   for k in report["items_kept"]), case
        assert report["target_config_problems"] == problems and report["needs_you"] is True
    empty = tmp_path / "case-absent"
    empty.mkdir()
    assert cli._archive_targets(empty, w.registry, "steward") == ([], [])


def test_target_problems_stop_recover_and_user_reclaimed(w, tmp_path):
    """Catches recovery, the user-reclaimed scan or the staging listing run
    while ``archive-targets.json`` has problems (9a, Revision 10 items 3 and
    6, 10a item 3). The file holds one valid entry -- the real target, which
    holds a labelled staged bundle -- and one entry that is not a mapping.
    With an open intent and a relocated bundle the user took away:

    * the pass's `recover` makes the intent ``UNRESOLVED`` (NEEDS_YOU,
      ``TARGET_PROBLEMS``), writes no receipt and appends no completion;
    * the staging listing does not run: ``target_leftovers`` is empty and
      ``target_leftovers_unmeasured`` carries ``TARGET_PROBLEMS`` for the
      parsed target;
    * `user_reclaimed` returns one ``UNRESOLVED`` entry (``TARGET_PROBLEMS``)
      and appends nothing to ``user-reclaimed.jsonl``.

    Mutants: recover runs anyway; `user_reclaimed` judges anyway; the
    listing runs anyway. Negative control: without problems the same
    relocation finds the leftover, judges USER_RECLAIMED_ARCHIVE and
    resolves the intent."""
    from test_verified_relocation import Crash

    from hoh import cli

    target, bundle_staged = _relocated_on_target(w)
    [relocated] = [json.loads(p.read_text()) for p in w.receipts.glob("reclaim-*.json")]
    Path(relocated["bundle_path"]).rename(w.tmp / "user-took-it")
    open_item = w.copy("open")
    plain = w.relocation(target=target, admission=FakeAdmission("PRESSURE", admit=False))
    plan = plain.plan(PROJECT, [open_item])
    vr._hooks["after_intent"] = lambda **context: (_ for _ in ()).throw(Crash("dies"))
    with pytest.raises(Crash):
        plain.execute(plan.path)
    vr._hooks.clear()
    scratch = w.registry.state_root / "projects" / PROJECT / "scratch" / vr.RELOCATION_SESSION
    [intent] = vr._open_intents(scratch)
    config = tmp_path / "config"
    config.mkdir()
    (config / cli.ARCHIVE_TARGETS_FILE).write_text(json.dumps([
        {"target_id": target.target_id, "root": str(target.root),
         "failure_domain": "domain-t1", "expected_identity": {"serial": "S-t1"}},
        ["not", "a", "mapping"]]), encoding="utf-8")
    targets, problems = cli._archive_targets(config, w.registry, "steward")
    assert [t.target_id for t in targets] == [target.target_id] and len(problems) == 1
    admission = FakeAdmission("PRESSURE", free=100 * GiB)
    troubled = _problem_relocation(w, targets, problems, admission)
    log = w.receipts / vr.USER_RECLAIMED_LOG
    report = ss.StorageSteward(PROJECT, w.registry, admission, troubled,
                               ss.StewardPolicy(target_timeout_s=60.0)).run_once()
    [outcome] = report["recovered"]
    assert outcome["status"] == vr.UNRESOLVED and outcome["needs_you"] is True
    assert outcome["detail"].startswith(vr.TARGET_PROBLEMS)
    assert not (w.receipts / f"reclaim-{intent['receipt_id']}.json").exists()
    assert [i["receipt_id"] for i in vr._open_intents(scratch)] == [intent["receipt_id"]]
    assert report["target_leftovers"] == [] and report["target_leftover_bytes"] == 0
    assert [(u["target_id"], u["detail"].split(":")[0])
            for u in report["target_leftovers_unmeasured"]] == [
        (target.target_id, vr.TARGET_PROBLEMS)]
    assert report["needs_you"] is True and report["target_config_problems"] == problems
    [judged] = troubled.user_reclaimed(targets[0], PROJECT)
    assert judged["status"] == vr.UNRESOLVED and judged["reason"].startswith(vr.TARGET_PROBLEMS)
    assert judged["target_id"] == target.target_id and not log.exists()
    # without problems: the listing finds the leftover, the scan judges, the
    # intent resolves
    # (the crashed execute's own build, before its intent, lies there too)
    assert str(bundle_staged) in {f["path"] for f in
                                  plain.target_leftovers(PROJECT, scratch)["found"]}
    assert plain.user_reclaimed(target, PROJECT)[0]["status"] == vr.USER_RECLAIMED_ARCHIVE
    [resolved] = plain.recover(PROJECT)
    assert resolved["status"] == vr.KEPT and resolved["detail"].startswith(vr.INTERRUPTED)


def test_duplicate_target_id_is_a_problem(w, tmp_path):
    """Catches a duplicate ``target_id`` resolved by "the last entry wins" (9a,
    Revision 10 item 6): two valid entries with one ``target_id`` and
    different roots give one target (the first) and a problem naming the
    id, and a relocation built from them refuses the item
    ``TARGET_UNMEASURABLE``. Mutant: last entry wins (no problem, the item
    planned). Negative control: the same entries with distinct ids give two
    targets and no problem."""
    from hoh import cli

    item = w.copy("dup")
    w.end_setup()
    first = {"target_id": "t1", "root": str(tmp_path / "root-a"), "failure_domain": "d",
             "expected_identity": {"serial": "S-t1"}}
    second = dict(first, root=str(tmp_path / "root-b"))
    for name, entries in (("dup", [first, second]),
                          ("distinct", [first, dict(second, target_id="t2")])):
        root = tmp_path / f"config-{name}"
        root.mkdir()
        (root / cli.ARCHIVE_TARGETS_FILE).write_text(json.dumps(entries), encoding="utf-8")
        targets, problems = cli._archive_targets(root, w.registry, "steward")
        if name == "dup":
            assert [str(t.root) for t in targets] == [first["root"]]
            assert len(problems) == 1 and "'t1'" in problems[0] and "twice" in problems[0]
            plan = _problem_relocation(w, targets, problems, FakeAdmission()).plan(
                PROJECT, [item])
            assert not plan.items
            assert plan.rejected[0]["reason"].startswith(vr.TARGET_UNMEASURABLE)
        else:
            assert [t.target_id for t in targets] == ["t1", "t2"] and problems == []


# --------------------------------------------------------------------------- #
# Revision 11 -- FAILED in NORMAL, a non-blocking archive-targets.json, the
# production wiring of target problems
# --------------------------------------------------------------------------- #


def test_failed_execute_needs_you_in_normal_pass(w, monkeypatch):
    """Catches a ``FAILED`` execute not judged in a NORMAL pass (sstew4b-a
    B1, Revision 11 item 1): NORMAL, ample free space, a forecast crossing
    the reserve (so the pass executes); ENOSPC while ``DRIVER.json`` is
    written makes the execute ``FAILED`` and the report NEEDS_YOU, the item
    untouched. Mutant: FAILED not judged in NORMAL. Negative control, the
    same pass with only the cause changed: another holder has
    ``relocation.lock`` flocked -- ``BUSY``, a proven live holder, is not
    NEEDS_YOU."""
    item = w.copy("dup")
    w.end_setup()
    admission = FakeAdmission("NORMAL", free=100 * GiB, seconds_to_reserve=60.0)
    steward = steward_for(w, admission)
    assert steward.relocation.recover(PROJECT) == []     # the relocation scratch and lock
    fd = os.open(steward.relocation_scratch / vr.LOCK_NAME, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        busy = steward.run_once()
    finally:
        os.close(fd)
    assert busy["state"] == "NORMAL" and busy["action"] == ss.EXECUTED
    assert busy["execute"]["status"] == vr.BUSY and busy["needs_you"] is False, busy["message"]

    def full(fd, data):
        raise OSError(errno.ENOSPC, "No space left on device (simulated)")

    monkeypatch.setattr(sc, "_write_whole", full)
    failed = steward.run_once()
    assert failed["state"] == "NORMAL" and failed["state_after"] == "NORMAL"
    assert failed["execute"]["status"] == vr.FAILED
    assert "No space left on device" in failed["execute"]["detail"]
    assert failed["needs_you"] is True and item.is_dir()
    assert json.loads(Path(failed["receipt_path"]).read_text())["needs_you"] is True


_TARGETS_PROBE = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "from hoh import cli\n"
    "targets, problems = cli._archive_targets(Path(sys.argv[1]), None, 'steward')\n"
    "print(json.dumps({'targets': len(targets), 'problems': problems}))\n"
)


def _archive_targets_in_subprocess(root: Path) -> dict:
    """`cli._archive_targets(root, ...)` in a child process bounded at 10 s:
    a call that blocks fails the test instead of hanging it. The child
    imports the same `hoh` package this process imported."""
    from hoh import cli

    package_root = str(Path(cli.__file__).resolve().parent.parent)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        p for p in (package_root, os.environ.get("PYTHONPATH", "")) if p))
    try:
        done = subprocess.run([sys.executable, "-B", "-c", _TARGETS_PROBE, str(root)],
                              capture_output=True, text=True, timeout=10, env=env,
                              check=False)
    except subprocess.TimeoutExpired:
        pytest.fail(f"_archive_targets blocked for more than 10 s on {root}")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_archive_targets_fifo_is_a_problem(tmp_path):
    """Catches ``archive-targets.json`` read by path or through a link
    (sstew4b-a M2, Revision 11 item 3): a FIFO at the name (no writer) and,
    separately, a symlink at the name to a valid file are each a
    configuration problem with no targets -- the call returns at once (it
    runs in a subprocess with a 10 s timeout, so a blocking read fails the
    test rather than hanging it). Mutants: read by path (blocks on the
    FIFO); ``O_NOFOLLOW`` dropped (the symlink's target is read as the
    list). Negative control: a regular file holding ``[]`` is no targets
    and no problem."""
    from hoh import cli

    fifo_root = tmp_path / "fifo"
    fifo_root.mkdir()
    os.mkfifo(fifo_root / cli.ARCHIVE_TARGETS_FILE)
    link_root = tmp_path / "link"
    link_root.mkdir()
    (tmp_path / "elsewhere.json").write_text("[]", encoding="utf-8")
    (link_root / cli.ARCHIVE_TARGETS_FILE).symlink_to(tmp_path / "elsewhere.json")
    for root in (fifo_root, link_root):
        answer = _archive_targets_in_subprocess(root)
        assert answer["targets"] == 0 and len(answer["problems"]) == 1, (root, answer)
        assert cli.ARCHIVE_TARGETS_FILE in answer["problems"][0]
    plain_root = tmp_path / "plain"
    plain_root.mkdir()
    (plain_root / cli.ARCHIVE_TARGETS_FILE).write_text("[]", encoding="utf-8")
    assert _archive_targets_in_subprocess(plain_root) == {"targets": 0, "problems": []}


def test_cli_passes_target_problems(w, monkeypatch, capsys):
    """Catches the production wiring that drops the target problems
    (sstew4b-b M1, Revision 11 item 2): ``hoh storage steward --project P
    --storage-root <tmp root> --once --plan-only`` through `cli.main`, with a
    malformed ``archive-targets.json`` under that root, plans every
    candidate as ``TARGET_UNMEASURABLE: archive-targets.json`` and lists the
    problem; nothing is executed. Mutant: `cmd_storage_steward` does not
    pass ``target_problems`` to `VerifiedRelocation`. Negative control, only
    the file moved aside: the same command plans the item. The command's
    registry, admission and boot id are this test's (injected), so it never
    reaches a real state root."""
    from test_storage_tiers import STEWARD_PID

    from hoh import cli, succession
    from hoh import storage_registry as sr

    item = w.copy("dup")
    w.end_setup()
    w.fake.procs.pop(STEWARD_PID)                # the world's steward session has ended
    w.fake.procs[os.getpid()] = 4242              # this process, as the command's own
    root = w.registry.state_root
    monkeypatch.setattr(cli, "storage_admission_for",
                        lambda option, **kw: (FakeAdmission("PRESSURE", free=100 * GiB), root))
    monkeypatch.setattr(sr, "StorageRegistry", lambda *a, **k: w.registry)
    monkeypatch.setattr(succession, "boot_id", lambda: BOOT)
    config = root / cli.ARCHIVE_TARGETS_FILE
    config.write_text('[{"target_id": "t1",', encoding="utf-8")
    command = ["storage", "steward", "--project", PROJECT, "--storage-root", str(root),
               "--once", "--plan-only"]

    assert cli.main(command) == 0
    report = json.loads(capsys.readouterr().out)["steward"]
    assert report["action"] == ss.PLANNED and report["execute"] is None
    assert report["plan"]["items"] == 0 and len(report["target_config_problems"]) == 1
    assert [k["path"] for k in report["items_kept"]] == [str(item)]
    assert all(k["why"].startswith(vr.TARGET_UNMEASURABLE + ": archive-targets.json")
               for k in report["items_kept"])
    assert report["needs_you"] is True and item.is_dir()

    config.rename(w.tmp / "archive-targets.json.aside")
    assert cli.main(command) == 0
    control = json.loads(capsys.readouterr().out)["steward"]
    assert control["plan"]["items"] == 1 and control["target_config_problems"] == []
    assert control["execute"] is None and item.is_dir()


def test_config_problems_needs_you_in_normal(w):
    """Catches a problem of ``archive-targets.json`` not NEEDS_YOU in a
    NORMAL pass (sstew4b-a M1, Revision 11 item 2): a NORMAL pass that only
    observes, with one configuration problem, lists it in
    ``target_config_problems`` and is NEEDS_YOU. Mutant: the problems not
    judged NEEDS_YOU. Negative control, only the problem removed: the same
    NORMAL pass is not NEEDS_YOU."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    admission = FakeAdmission("NORMAL", free=100 * GiB)
    policy = ss.StewardPolicy(target_timeout_s=60.0)
    problem = "entry 1 is not a mapping but str (simulated)"

    def pass_with(problems):
        relocation = _problem_relocation(w, [target], problems, admission)
        return ss.StorageSteward(PROJECT, w.registry, admission, relocation, policy).run_once()

    calm = pass_with([])
    assert calm["action"] == ss.OBSERVED and calm["needs_you"] is False, calm["message"]
    troubled = pass_with([problem])
    assert troubled["action"] == ss.OBSERVED and troubled["state"] == "NORMAL"
    assert troubled["target_config_problems"] == [problem]
    assert troubled["needs_you"] is True and item.is_dir()
