"""P3-39 (the remaining two thirds): storage is a reserved capacity on the wave's production path.

A wave's admission (`capacitycheck.admit_wave`, called by `ProjectController._wave_round`) now asks for storage beside the
sessions: each node wants the bytes one run is predicted to write (the admission ledger's own history-windowed estimate)
and the inodes it is predicted to create, the scheduler admits the nodes in order so that their combined want never crosses
the reserve, and what is admitted is held in the capacity ledger in the same locked step, where `hoh capacity check` and
`hoh capacity show --source` list it. The operator's `storage-policy.json` in the state root is what turns it on. How a wave's
rows end is the ledger's own business (`tests/test_capacity_reservation.py`): the owner's next ask, its death, the ttl and --
when P3-04 C4b is merged -- the end of the wave; nothing here depends on which of them ends a row.

What is real: `ProjectController`, `ParallelWaveExecutor`, `HohRunLauncher` (only its process boundary replaced, as in
`tests/test_wave_capacity.py`), the registry, the capacity ledger and its lock, `StorageAdmission` (its ledger, history and
state files in `tmp_path`), the scheduler and the broker. What is not: the host -- the admission observes a fake `statvfs`
(no test reads this machine's free space), with its liveness probes injected as in `tests/test_storage_pressure.py`. The
harness of `tests/test_wave_capacity.py` is imported, not copied. No agent, no Herdr, no network.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_capacity_reservation import _second_project, ledger
from test_storage_pressure import FakeHost, make
from test_wave_capacity import (  # noqa: F401 - fixtures are collected from here
    Gates, Launcher, World, hermetic_environment, kinds, utc, waves,
)
from test_wave_capacity import world as world  # the alias form is an explicit re-export: no F811 on the parameters

from hoh import cli
from hoh.orchestrator import HaltClass, ProjectController
from hoh.project import Lifecycle
from hoh.storage_pressure import GiB, StoragePolicy

LEDGER = "_capacity-reservations.json"
POLICY = "storage-policy.json"
BYTES, INODES, SESSIONS = "machine/storage.bytes", "machine/storage.inodes", "machine/host.sessions"
RUN_BYTES = 3 * GiB            # role_run (2 GiB) + tmp_workspace (1 GiB): the ledger's defaults while it has no history
RUN_INODES = 50_000            # the stated default of a run's inodes, literal on purpose
RESERVE = 30 * GiB             # the policy's default reserve
FLOOR = 100_000                # the policy's default inode floor (`min_inodes_free`)


def _check():
    import importlib
    return importlib.import_module("hoh.capacitycheck")


def _capacity():
    import importlib
    return importlib.import_module("hoh.capacity")


class Statvfs:
    """The host's free space as a test says it: `free` bytes, `inodes` free, and a `files` total when given
    (0 is a filesystem with no inode table)."""

    def __init__(self, free: int, inodes: int = 10_000_000, files: int | None = None) -> None:
        self.free, self.inodes, self.files = free, inodes, files

    def __call__(self, path):
        extra = {} if self.files is None else {"f_files": self.files}
        return SimpleNamespace(f_bavail=self.free, f_frsize=1, f_favail=self.inodes, f_fsid=7, **extra)


def clock(offset: int = 0):
    return lambda: datetime.now(timezone.utc) + timedelta(seconds=offset)


class Admissions:
    """The `storage_admission_factory` of a launcher: an admission over the state root on a fake host. `built` counts how
    often the gate asked for one."""

    def __init__(self, free: int = RESERVE + 7 * GiB, **kw) -> None:
        self.statvfs = Statvfs(free, kw.pop("inodes", 10_000_000), kw.pop("files", None))
        self.clock = kw.pop("clock", None) or clock()
        self.kw, self.built = kw, 0

    def __call__(self, root):
        self.built += 1
        return make(root, statvfs=self.statvfs, clock=self.clock, **self.kw)[0]


def opt_in(world, text: str = "{}") -> Path:
    path = world.state / POLICY
    path.write_text(text, encoding="utf-8")
    return path


def rows(world, **match) -> list[dict]:
    return [h for h in ledger(world.root)["holds"] if all(h[k] == v for k, v in match.items())]


def ask(world, nodes, admissions, **env):
    """`admit_wave` as the controller calls it: the storage reader of the state root over the launcher's factory."""
    check = _check()
    return check.admit_wave(nodes, runs_root=world.root, state_root=world.state, now=utc,
                            storage=check.storage_reader(world.state, factory=admissions), **env)


CONFIG: dict = {}


def build_driver(store):
    """The `driver.json` factory of `hoh project drive`: the real launcher with a fake process boundary and a fake host."""
    cfg = CONFIG
    launcher = Launcher(cfg["root"], cfg["repo"], storage_root=cfg["state"], storage_admission_factory=cfg["admissions"])
    cfg["launcher"] = launcher
    return launcher, Gates()


def _drive(world, admissions, monkeypatch, capsys, width="3"):
    CONFIG.clear()
    CONFIG.update(root=world.root, repo=world.repo, state=world.state, admissions=admissions)
    (world.store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_wave_storage:build_driver", "path": str(Path(__file__).resolve().parent)}))
    monkeypatch.setenv("HOH_WAVE_WIDTH", width)
    rc = cli.main(["--root", str(world.root), "project", "drive", "p", "--session", "sA"])
    return rc, json.loads(capsys.readouterr().out)


# --- R1 the production path ---------------------------------------------------------------------------------------------

def test_hoh_project_drive_reserves_storage_per_node_cuts_the_wave_at_the_reserve_and_holds_it_in_the_ledger(world, monkeypatch,
                                                                                                    capsys):
    """The production path, end to end: `hoh project drive` (cli.main) -> `resumeloop.drive_governed` ->
    `ProjectController.run` -> `_wave_round` -> `_capacity_admission` -> `capacitycheck.admit_wave` (the storage reader over
    the launcher's admission) -> `scheduler.admit` -> `CapacityReservations.reserve_atomically`. Catches a storage that is
    judged but not wanted by the nodes, or not held: with room for 8 sessions, three nodes at width 3 and 7 GiB above the
    reserve, the first wave names n1 and n2 (3 GiB each), n3 is a `CAPACITY_WAIT` and runs after it, and the ledger holds a
    session, 3 GiB and 50000 inodes per admitted node; when the owner asks again its waves' rows are over."""
    world.sessions(8)
    opt_in(world)
    admissions = Admissions(RESERVE + 7 * GiB)
    rc, out = _drive(world, admissions, monkeypatch, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert [s for s in out["steps"] if s.startswith("CAPACITY_WAIT:")] == ["CAPACITY_WAIT:n3"]
    assert [n.node_id for n in waves(world)[0].nodes] == ["n1", "n2"]
    assert sorted(CONFIG["launcher"].merged) == ["n1", "n2", "n3"]
    held = ledger(world.root)["holds"]
    assert sorted((h["run_id"], f"{h['kind']}/{h['name']}", h["amount"], h["unit"]) for h in held) == sorted(
        (node, name, amount, unit) for node in ("n1", "n2")
        for name, amount, unit in ((SESSIONS, 1, "sessions"), (BYTES, RUN_BYTES, "bytes"), (INODES, RUN_INODES, "items")))
    assert admissions.built >= 1
    _check().admit_wave(["x"], runs_root=world.root, state_root=world.state, now=utc)         # the owner asks again
    assert {h["status"] for h in ledger(world.root)["holds"] if h["run_id"] in ("n1", "n2")} <= {"SETTLED", "SUPERSEDED"}


def test_the_combined_want_of_a_wave_is_cut_where_it_would_cross_the_reserve_and_the_step_names_the_figures(world):
    """Catches a want that is judged node by node (every node fits alone) and a boundary that is off by one: 36 GiB free
    is exactly 6 GiB above the 30 GiB reserve, so n1 and n2 (3 GiB each) are admitted and n3 waits `exhausted` with
    "6442450944 of 6442450944 bytes of machine/storage.bytes are held"; one byte less and n2 waits too. With 100 GiB all three
    are admitted. The admitted step quotes the state (PRESSURE below 50 GiB: work continues, narrowed by the reserve only)."""
    world.sessions(8)
    opt_in(world)
    for free, expected in ((RESERVE + 6 * GiB, ["n1", "n2"]), (RESERVE + 6 * GiB - 1, ["n1"]),
                           (100 * GiB, ["n1", "n2", "n3"])):
        gate = ask(world, ["n1", "n2", "n3"], Admissions(free))
        assert list(gate.admitted) == expected, (free, gate.why)
        assert len(gate.reserved) == len(expected)
        if len(expected) < 3:
            waiting, why = gate.waits[0]
            assert waiting == f"n{len(expected) + 1}"
            assert why.startswith("wait/exhausted:") and f"bytes of {BYTES} are held" in why
    gate = ask(world, ["n1", "n2", "n3"], Admissions(RESERVE + 6 * GiB))
    assert gate.registry == _capacity().read(world.record).digest        # the operator's record, not record plus storage
    assert gate.waits == (("n3", f"wait/exhausted: {6 * GiB} of {6 * GiB} bytes of {BYTES} are held"),)
    assert gate.why == (f"capacity: 2 of 3 node(s) admitted on registry {gate.registry[:12]}; storage PRESSURE: "
                        f"{RUN_BYTES} bytes and {RUN_INODES} inodes held per node (inodes: the RUN_INODES floor)")


def test_a_wave_of_the_controller_waits_ready_and_unprepared_for_storage_and_the_step_says_which_capacity(world):
    """Catches the explanation of a storage wait that names the first capacity instead of the one that decided: with
    sessions plentiful and 7 GiB above the reserve, the controller's first wave is n1 and n2, n3 is a `CAPACITY_WAIT` step
    whose detail names `machine/storage.bytes` (not `machine/host.sessions`), nothing is prepared for it in that round, and
    the project closes."""
    world.sessions(8)
    opt_in(world)
    launcher = world.launcher(storage_admission_factory=Admissions(RESERVE + 7 * GiB), barrier=threading.Barrier(2))
    touched: list[str] = []
    original = launcher.prepared_hold
    launcher.prepared_hold = lambda node: (touched.append(node.id), original(node))[1]
    result = ProjectController(world.store, launcher, Gates(), width=3).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert touched == ["n1", "n2"]
    [waiting] = [s for s in kinds(result, "CAPACITY_WAIT")]
    assert waiting.node == "n3" and waiting.round == 1
    assert waiting.detail == f"wait/exhausted: {2 * RUN_BYTES} of {7 * GiB} bytes of {BYTES} are held"
    assert launcher.peak == 2


# --- R2 two controllers, one storage --------------------------------------------------------------------------------------

def test_a_second_controller_is_not_admitted_against_storage_the_first_waves_rows_hold_and_check_and_show_list_them(
        world, capsys):
    """Catches a storage that only the wave's own controller sees: project p's wave of two is inside `hoh run` (a barrier of
    three, the test being the third party) holding 6 GiB and 100000 inodes in the ledger; `hoh capacity check` -- another
    process's view -- lists them (`holds.storage_bytes`, `holds.storage_inodes`, two reserved sessions) and so does `hoh
    capacity show --source` (two FRESH `reserved` facts of `machine/storage.bytes` and `machine/storage.inodes`, which it
    does not print while no wave holds any); project q, another controller of the same runs root with sessions to spare,
    asks for its two nodes and halts `CAPACITY_WAIT` on the storage (9 GiB would be above the 7 GiB left over the reserve),
    nothing prepared; when p's controller has finished its rows no longer count and q is admitted and closes."""
    world.sessions(8)
    opt_in(world)
    admissions = Admissions(RESERVE + 7 * GiB)
    barrier = threading.Barrier(3)
    first = world.launcher(storage_admission_factory=admissions, barrier=barrier)
    outcome: dict = {}
    thread = threading.Thread(target=lambda: outcome.update(first=ProjectController(world.store, first, Gates(),
                                                                                    width=2).run()), name="controller-p")
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while first.running < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert first.running == 2, "the first wave never started"
        held = ledger(world.root)["holds"]
        assert {h["status"] for h in held} == {"HELD"} and len(held) == 6
        assert sum(h["amount"] for h in held if h["name"] == "storage.bytes") == 2 * RUN_BYTES
        holds = _check().live_holds(world.root)
        assert (holds.reserved, holds.storage_bytes, holds.storage_inodes) == (2, 2 * RUN_BYTES, 2 * RUN_INODES)
        rc = cli.main(["--root", str(world.root), "capacity", "check", "machine", "host.sessions", "1", "sessions",
                       "--storage-root", str(world.state)])
        shown = json.loads(capsys.readouterr().out)["holds"]
        assert rc == 0 and (shown["storage_bytes"], shown["storage_inodes"]) == (2 * RUN_BYTES, 2 * RUN_INODES)
        rc = cli.main(["--root", str(world.root), "capacity", "show", "--source", "--storage-root", str(world.state)])
        shown = json.loads(capsys.readouterr().out)["source"]["facts"]
        stored = {f["name"]: (f["field"], f["value"], f["unit"], f["status"]) for f in shown
                  if f["family"] == "slots" and f["name"].startswith("storage.")}
        assert rc == 0 and stored == {"storage.bytes": ("reserved", 2 * RUN_BYTES, "bytes", "FRESH"),
                                      "storage.inodes": ("reserved", 2 * RUN_INODES, "items", "FRESH")}
        store_q, repo_q = _second_project(world)
        second = Launcher(world.root, repo_q, storage_root=world.state, storage_admission_factory=admissions)
        refused = ProjectController(store_q, second, Gates(), width=2).run()
        assert refused.halt is HaltClass.CAPACITY_WAIT, (refused.halt, refused.reason)
        assert f"{2 * RUN_BYTES} of {7 * GiB} bytes of {BYTES} are held" in refused.reason and second.calls == []
        assert {n.lifecycle for n in store_q.read_state().nodes} == {Lifecycle.READY}
    finally:
        barrier.wait(timeout=30)
        thread.join(timeout=60)
    assert outcome["first"].halt is HaltClass.CLOSED, outcome["first"].reason
    assert _check().live_holds(world.root).reserved == 0                      # p is finished: its rows no longer count
    again = ProjectController(store_q, second, Gates(), width=2).run()
    assert again.halt is HaltClass.CLOSED, again.reason


def test_the_open_reservations_of_the_admission_ledger_are_held_against_the_wave(world):
    """Catches a wave that ignores what runs already started have reserved: with 7 GiB above the reserve and a role run
    that holds 4 GiB in the admission ledger, one node (3 GiB) fits and the second waits ("7516192768 ... are held": 4 GiB of
    the ledger and the first node's 3 GiB), where without that reservation two nodes are admitted."""
    world.sessions(8)
    opt_in(world)
    admissions = Admissions(RESERVE + 7 * GiB)
    assert ask(world, ["n1", "n2"], admissions).admitted == ("n1", "n2")
    admissions(world.state).reserve("p", "role_run", estimate_bytes=4 * GiB)
    gate = ask(world, ["n1", "n2"], admissions)
    assert gate.admitted == ("n1",)
    assert gate.waits == (("n2", f"wait/exhausted: {4 * GiB + RUN_BYTES} of {7 * GiB} bytes of {BYTES} are held"),)


def test_a_prediction_above_the_registrys_ceiling_admits_nobody_and_writes_no_row(world):
    """Catches a prediction that is clipped or ignored when the registry cannot hold it: one role run recorded with 10**15
    bytes written predicts 1.5 x 10**15 for the next, above the 10**15 any capacity may state; the broker refuses the amount
    (`BrokerError`), so the gate admits nobody, says why and writes no row."""
    world.sessions(8)
    opt_in(world)
    admissions = Admissions(100 * GiB)
    adm = admissions(world.state)
    adm.release(adm.reserve("p", "role_run"), 10 ** 15)
    gate = ask(world, ["n1", "n2"], admissions)
    assert gate.configured and gate.admitted == () and gate.reserved == () and "BrokerError" in gate.why, gate.why
    assert not (world.root / LEDGER).exists()


# --- R3 what the prediction rests on ---------------------------------------------------------------------------------------

def test_the_bytes_a_node_wants_are_the_ledgers_prediction_from_its_recorded_history(world):
    """Catches a constant where the directive says "predicted per-run storage from measured history": with no history a node
    wants 3 GiB (the defaults of a role run and its workspace); after one role run released with 10 GiB actually written the
    ledger predicts 15 GiB (1.5 x the p95 of the window) for it and a node wants 16 GiB with its workspace's 1 GiB -- the row
    carries that figure, and 44 GiB above the reserve admit two nodes, not three."""
    world.sessions(8)
    opt_in(world)
    admissions = Admissions(RESERVE + 44 * GiB)
    assert ask(world, ["n1"], admissions).admitted == ("n1",)
    assert [h["amount"] for h in rows(world, name="storage.bytes")] == [RUN_BYTES]
    adm = admissions(world.state)
    reservation = adm.reserve("p", "role_run")
    adm.release(reservation, 10 * GiB)
    gate = ask(world, ["n1", "n2", "n3"], admissions)
    assert gate.admitted == ("n1", "n2")
    assert {h["amount"] for h in rows(world, name="storage.bytes", status="HELD")} == {16 * GiB}


def test_a_node_in_critical_is_refused_even_when_no_positive_want_is_predicted(world):
    """Catches a CRITICAL host that admits because nothing is wanted (the spec review's M3): with the policy's default estimates
    set to 0, no history and a filesystem with no inode table (so no inode want either), the predicted bytes of a node are 0 --
    and 0 fits the limit of 0 that CRITICAL gives. A node is never wanted at less than one byte: 14 GiB free (CRITICAL) admits
    nobody, with the reason `more than its limit 0`; the negative control, 100 GiB free with the same policy, admits."""
    world.sessions(8)
    opt_in(world)
    policy = StoragePolicy(defaults={"role_run": 0, "tmp_workspace": 0})
    critical = ask(world, ["n1", "n2"], Admissions(14 * GiB, inodes=0, files=0, policy=policy))
    assert critical.admitted == () and all("more than its limit 0" in why for _, why in critical.waits), critical.why
    assert not (world.root / LEDGER).exists()
    assert ask(world, ["n1", "n2"], Admissions(100 * GiB, inodes=0, files=0, policy=policy)).admitted == ("n1", "n2")


def _closed_runs(admissions, root, role_inodes=None, workspace_inodes=None, role_bytes=2 * GiB):
    """Closes one role run and one workspace in the admission ledger the way the product does -- `release` with the bytes, then
    `record_inodes` with the count -- so that its history holds them."""
    adm = admissions(root)
    for kind, inodes in (("role_run", role_inodes), ("tmp_workspace", workspace_inodes)):
        reservation = adm.reserve("p", kind)
        adm.release(reservation, role_bytes if kind == "role_run" else 1)
        if inodes is not None:
            assert adm.record_inodes(reservation, inodes) is True


@pytest.mark.parametrize("role, workspace, want, from_history", [
    (None, None, 50_000, False), (6_000, 4_000, 50_000, False), (None, 4_000, 50_000, False), (30_000, 4_000, 51_000, True),
    (70_000, None, 105_000, True)], ids=["cold", "history-below-the-floor", "one-kind-below", "history-above-the-floor", "one-kind-above"])
def test_the_inodes_a_node_wants_are_the_history_above_the_floor_and_the_floor_below_it(world, role, workspace, want, from_history):
    """Catches an inode want that falls below the floor once history exists (the spec review's blocking finding, A-B1) and one that
    ignores history above it. `RUN_INODES` (50000) is a permanent floor, as the policy's default is the floor of the bytes: with no
    count recorded, with a role run closed at 6000 and a workspace at 4000 inodes (the ledger's 1.5 x p95 is 9000 + 6000 = 15000,
    **below the floor**) and with a workspace alone, a node wants 50000 and the line says it is the floor -- two nodes fit the 102000
    free inodes above the inode floor, where a mutant that keeps the floor only for the cold start wants 15000 and admits three; with
    30000 and 4000 the history is 45000 + 6000 = 51000 and with a role run alone at 70000 it is 105000, **above** it: the node
    wants that, the line does not say "floor", and exactly the nodes that fit are admitted (two at 51000, none at 105000)."""
    world.sessions(8)
    opt_in(world)
    admissions = Admissions(100 * GiB, inodes=FLOOR + 2 * 51_000)
    if role is not None or workspace is not None:
        _closed_runs(admissions, world.state, role_inodes=role, workspace_inodes=workspace)
    gate = ask(world, ["n1", "n2", "n3"], admissions)
    if gate.admitted:
        assert {h["amount"] for h in rows(world, name="storage.inodes", status="HELD")} == {want}
        assert ("(inodes: the RUN_INODES floor)" in gate.why) is (not from_history)
    else:
        assert all(f"{want} items of {INODES} is more than its limit 102000" in why for _, why in gate.waits), gate.why
    assert len(gate.admitted) == min(2, (FLOOR + 2 * 51_000 - FLOOR) // want)


def test_the_inodes_a_node_wants_are_fifty_thousand_and_the_line_is_the_floor_above_the_reserve(world):
    """Catches the inode axis dropped, or its figure moved: a node wants 50000 inodes of the `free - floor` the snapshot
    states (floor 100000), so exactly 200000 free admit two nodes and 199999 admit one (a default of 49999 or 50001 moves a
    line); and a filesystem with no inode table (`f_files` 0) is not asked for inodes at all: three nodes, no inode row."""
    world.sessions(8)
    opt_in(world)
    assert ask(world, ["n1", "n2", "n3"], Admissions(100 * GiB, inodes=FLOOR + 2 * RUN_INODES)).admitted == ("n1", "n2")
    assert ask(world, ["n1", "n2", "n3"], Admissions(100 * GiB, inodes=FLOOR + 2 * RUN_INODES - 1)).admitted == ("n1",)
    assert ask(world, ["n1", "n2", "n3"], Admissions(100 * GiB, inodes=0, files=0)).admitted == ("n1", "n2", "n3")
    assert [h["name"] for h in rows(world, status="HELD")] == ["host.sessions", "storage.bytes"] * 3


# --- R4 fail closed ------------------------------------------------------------------------------------------------------

def _damaged_ledger(world, admissions):
    adm = admissions(world.state)
    (adm.reservations_dir / "junk.json").write_text("{not json", encoding="utf-8")


@pytest.mark.parametrize("label", ["critical", "malformed-reservation", "host-unobservable", "stale-reading",
                                   "future-reading", "factory-raises", "registry-records-storage"])
def test_what_cannot_be_judged_or_does_not_fit_admits_nobody_and_writes_no_row(world, label):
    """Catches a storage that fails open: in CRITICAL (14 GiB free: the limit is 0), with a malformed reservation in the
    admission ledger, with the host unobservable and no persisted reading, with a reading older than the measured age or
    stamped after now, with an admission that raises, and with a registry that records a storage name itself, no node is
    admitted, each reason names the cause, the capacity ledger is not created and nothing is held."""
    world.sessions(8)
    opt_in(world)
    admissions = Admissions(14 * GiB if label == "critical" else 100 * GiB)
    if label == "malformed-reservation":
        _damaged_ledger(world, admissions)
    if label == "host-unobservable":
        admissions = Admissions(100 * GiB, host=FakeHost(None))
    if label == "stale-reading":
        admissions = Admissions(100 * GiB, clock=clock(-600))
    if label == "future-reading":
        admissions = Admissions(100 * GiB, clock=clock(+600))
    if label == "factory-raises":
        def admissions(root):
            raise OSError("the ledger cannot be opened")
    if label == "registry-records-storage":
        world.registry(("machine", "host.sessions", 8, "sessions", "declared"),
                       ("machine", "storage.bytes", 10 ** 12, "bytes", "declared"))
    gate = ask(world, ["n1", "n2"], admissions)
    assert gate.configured and gate.admitted == () and gate.reserved == () and gate.reserved == (), gate.why
    reasons = " ".join(why for _, why in gate.waits) + gate.why
    assert {"critical": "more than its limit 0", "malformed-reservation": "not stated by the storage ledger",
            "host-unobservable": "not stated by the storage ledger", "stale-reading": "not stated by the storage ledger",
            "future-reading": "not stated by the storage ledger", "factory-raises": "OSError",
            "registry-records-storage": "records a storage capacity itself"}[label] in reasons
    assert not (world.root / LEDGER).exists()


@pytest.mark.parametrize("label", ["directory", "symlink", "fifo", "corrupt", "non-utf8", "not-an-object", "oversize"])
def test_a_storage_policy_that_is_not_a_regular_readable_policy_admits_nobody(world, label):
    """Catches `ENOENT` read as "no policy" for every kind of odd file: a policy that is a directory, a link (even to a
    valid file), a FIFO (it must not block), not JSON, not UTF-8, not an object or above 64 KiB admits nobody, with the reason, and
    writes nothing -- only a **missing** policy is the sessions-only wave."""
    world.sessions(8)
    path = world.state / POLICY
    if label == "directory":
        path.mkdir()
    elif label == "symlink":
        (world.state / "real-policy.json").write_text("{}", encoding="utf-8")
        path.symlink_to(world.state / "real-policy.json")
    elif label == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes({"corrupt": b"{not json", "non-utf8": b"\xff\xfe{}", "not-an-object": b"[]",
                          "oversize": b"{" + b" " * 65535 + b"}"}[label])
    check = _check()
    done: dict = {}
    thread = threading.Thread(target=lambda: done.update(gate=check.admit_wave(
        ["n1", "n2"], runs_root=world.root, state_root=world.state, now=utc,
        storage=check.storage_reader(world.state))), daemon=True)
    thread.start()
    thread.join(timeout=20)
    assert not thread.is_alive(), "a policy that is a FIFO blocked the gate"
    gate = done["gate"]
    assert gate.configured and gate.admitted == () and gate.reserved == (), gate.why
    assert ("not a file of its own" in gate.why or "StoragePolicyError" in gate.why
            or "larger than 65536 bytes" in gate.why), gate.why
    assert not (world.root / LEDGER).exists()


def test_a_storage_ledger_that_does_not_answer_is_waited_for_a_bounded_time_and_holds_nobody_else_up(world, monkeypatch):
    """The termination of the storage read: the admission ledger's lock is a blocking `flock`. An admission whose `status()` never
    returns must not hold the controller for ever, nor anyone else: with the bound at 3 s the gate answers (admitted nobody, "did
    not answer within 3 s", no row for it) inside the 20 s the test allows, where an unbounded wait would never return; and while
    it is waiting, another controller's ask of the same runs root is answered at once (the read is made before the capacity
    ledger's lock is taken, so it holds no one up -- read under that lock the second ask would wait for it). The bound is the
    module's `STORAGE_TIMEOUT`, 10 s."""
    world.sessions(8)
    opt_in(world)
    check = _check()
    assert check.STORAGE_TIMEOUT == 10.0
    monkeypatch.setattr(check, "STORAGE_TIMEOUT", 3.0)
    stuck, entered = threading.Event(), threading.Event()

    class Blocked:
        def status(self):
            entered.set()
            stuck.wait(60)
            return {}

        def estimate(self, kind):
            return 1

        def estimate_inodes(self, kind):
            return None

    done: dict = {}
    thread = threading.Thread(target=lambda: done.update(gate=ask(world, ["n1", "n2"], lambda root: Blocked())), daemon=True)
    started = time.monotonic()
    thread.start()
    try:
        assert entered.wait(20), "the gate never asked the storage ledger"
        other = time.monotonic()
        second = check.admit_wave(["x"], runs_root=world.root, state_root=world.state, now=utc)
        assert second.admitted == ("x",) and time.monotonic() - other < 1.5, "the capacity lock was held by the storage read"
        thread.join(timeout=20)
    finally:
        stuck.set()
    assert not thread.is_alive(), "the gate waited for a storage ledger that never answers"
    assert time.monotonic() - started < 15 and "did not answer within 3 s" in done["gate"].why
    assert done["gate"].admitted == () and done["gate"].reserved == ()
    assert {h["run_id"] for h in ledger(world.root)["holds"]} == {"x"}


def test_a_storage_policy_of_exactly_the_size_bound_is_a_policy(world):
    """Pins the ceiling from the other side: a policy file of exactly 65536 bytes (spaces around `{}`) is the operator's
    policy and the wave is storage-judged, where one byte more (the `oversize` case above) admits nobody."""
    world.sessions(8)
    opt_in(world, "{" + " " * 65534 + "}")
    assert (world.state / POLICY).stat().st_size == 65536
    admissions = Admissions(100 * GiB)
    assert ask(world, ["n1", "n2"], admissions).admitted == ("n1", "n2") and admissions.built == 1


def test_without_a_storage_policy_the_wave_is_judged_on_sessions_only_as_before(world):
    """Catches a storage gate that is on for everyone: with a registry and no `storage-policy.json` the launcher's admission
    is not even asked for, the wave's words are exactly the sessions-only ones, and the ledger holds sessions only. The same
    wave with the policy present holds bytes and inodes too."""
    world.sessions(8)
    admissions = Admissions(RESERVE + 1)             # would refuse every node if it were asked
    gate = ask(world, ["n1", "n2"], admissions)
    assert gate.admitted == ("n1", "n2") and admissions.built == 0
    assert gate.why == f"capacity: 2 of 2 node(s) admitted on registry {gate.registry[:12]}"
    assert {h["name"] for h in ledger(world.root)["holds"]} == {"host.sessions"}
    opt_in(world)
    assert ask(world, ["n1", "n2"], Admissions(RESERVE + 1)).admitted == ()
