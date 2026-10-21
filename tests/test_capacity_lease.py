"""P3-04, capacity chain step C6: a capacity hold is lease-aware -- a run that is there keeps what it was admitted against.

C4 held what a wave was admitted against and C4b gave it back when the wave ended; both let a hold end by its *owner's* death
or its ttl, and counted a run that had taken its run lease (`hoh run`, `rundrive.drive_lease`) twice -- once through the lease and
once through the row the admission wrote for it. This file pins the link and what it changes: the admission names the lease of
each node's run in the row it holds (`runs=`), `CapacityReservations.judge` keeps a row `HELD` for as long as that lease is live
(its owner gone, its ttl past), and `capacitycheck.live_holds` counts the session of a running run once.

What is real: the controller, the executor, the real launcher over a fake process boundary (the harnesses of
`tests/test_wave_capacity.py` and `tests/test_capacity_reservation.py`, imported), the ledger and its lock, real run leases (the
launcher's `hoh run` of these tests takes the lease the way `cli.main` does: `rundrive.drive_lease`), `cli.main project drive`.
"An other owner" is the same module with another pid and an `alive` that says it is gone: the facts of a process are injectable by
design. No agent, no Herdr, no network.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
from test_capacity_reservation import (  # noqa: F401 - helpers; the fixtures are collected below
    LEDGER, NOW, _check, _res, _second_project, ask, ledger, owner, plus, statuses,
)
from test_capacity_reservation import alarm_free_timeout as alarm_free_timeout  # the alias form: an explicit re-export
from test_capacity_settlement import _recovery_world
from test_wave_capacity import (  # noqa: F401 - fixtures are collected from here
    Gates, Launcher, World, hermetic_environment,
)
from test_wave_capacity import world as world  # the alias form is an explicit re-export: no F811 on the parameters

from hoh import cli, rundrive
from hoh.launcher import Dispatch
from hoh.orchestrator import HaltClass, ProjectController
from hoh.project import Lifecycle
from hoh.runlease import DIRNAME, RunLeases

GONE = 1111                                       # the pid of the owner that the asking process finds gone
CONFIG: dict = {}


def gone(pid: int) -> bool:
    return pid != GONE


def lease_dir(world: World) -> Path:
    return world.root / "projects" / rundrive.LEASE_PROJECT / DIRNAME


def run_lease(world: World, run_id: str, *, at: str | None = None, ttl: int = 3600):
    """The lease `hoh run` keeps for `run_id`, as another live process holds it: `(RunLeases, the lease)`. `at` is the clock of the
    taker (a lease taken later lives later)."""
    leases = RunLeases(lease_dir(world), **({} if at is None else {"clock": lambda: at}))
    return leases, leases.acquire(rundrive.lease_name(run_id), "hoh-run", ttl_seconds=ttl)


def declare(world: World, sessions: int = 8, claude: int | None = 8) -> None:
    entries = [("machine", "host.sessions", sessions, "sessions", "declared")]
    if claude is not None:
        entries.append(("provider", "claude.sessions", claude, "sessions", "declared"))
    world.registry(*entries)


class Running(Launcher):
    """`hoh run` as the command does it around the run body (`cli.main`: `rundrive.drive_lease` for as long as the run is driven)."""

    def _run_cli(self, argv):
        if "run" in argv and "worktree" not in argv and "start" not in argv:
            with rundrive.drive_lease(self.root, argv[argv.index("run") + 1]):
                return super()._run_cli(argv)
        return super()._run_cli(argv)


class InFlight(Launcher):
    """n1's `hoh run` answers "already being executed by another process" (the launch comes back `IN_FLIGHT`: a live process runs it)."""

    def _run_cli(self, argv):
        if "run" in argv and "worktree" not in argv and "start" not in argv and argv[argv.index("run") + 1] == "n1":
            return Dispatch(2, "", "Run n1 is already being executed by another process")
        return super()._run_cli(argv)


def build_driver(store):
    """The `driver.json` factory of the CLI test: the real launcher, its `hoh run` holding the lease, over a fake process boundary."""
    launcher = Running(CONFIG["root"], CONFIG["repo"], barrier=CONFIG.get("barrier"), storage_root=CONFIG["state"])
    CONFIG["launcher"] = launcher
    return launcher, Gates()


# --- R1 the probe: the production path --------------------------------------------------------------------------------------

def test_hoh_project_drive_counts_the_session_of_a_running_run_once_and_its_other_holds_in_full(world, monkeypatch, capsys):
    """The production path (`cli.main` -> `hoh project drive` -> `ProjectController.run` -> `_wave_round` ->
    `_capacity_admission` -> `capacitycheck.admit_wave` -> `CapacityReservations.reserve_atomically`; the runs take their leases
    as `hoh run` does): two nodes of a wave are *inside* their runs (a barrier of three, the test being the third party) with
    room for four sessions and four of the provider's. Another owner (`hoh capacity check` is another process) reads two
    sessions held -- one per running run, not four (a lease and the row of the same run) -- and two provider sessions, which the
    leases do not count: the rows name the lease of their run (`lease`), the provider's rows included."""
    declare(world, 4, 4)
    barrier = threading.Barrier(3)
    CONFIG.clear()
    CONFIG.update(root=world.root, repo=world.repo, state=world.state, barrier=barrier)
    (world.store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_capacity_lease:build_driver", "path": str(Path(__file__).resolve().parent)}))
    monkeypatch.setenv("HOH_WAVE_WIDTH", "2")
    done: dict = {}
    thread = threading.Thread(target=lambda: done.update(rc=cli.main(
        ["--root", str(world.root), "project", "drive", "p", "--session", "sA"])), name="controller-p")
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and (CONFIG.get("launcher") is None or CONFIG["launcher"].running < 2):
            time.sleep(0.02)
        assert CONFIG["launcher"].running == 2, "the wave never started"
        held = _check().live_holds(world.root)
        amounts = {(a.kind, a.name): a.amount for a in held.amounts}
        assert (held.sessions, held.reserved, held.absorbed) == (2, 0, 2), (held.sessions, held.reserved, held.absorbed)
        assert held.as_dict()["absorbed"] == 2 and held.as_dict()["sessions"] - held.as_dict()["reserved"] == 2
        assert amounts[("machine", "host.sessions")] == 2 and amounts[("provider", "claude.sessions")] == 2, amounts
        rows = [(h["run_id"], h["name"], h.get("lease")) for h in ledger(world.root)["holds"]]
        assert sorted(rows) == [("n1", "claude.sessions", "n1"), ("n1", "host.sessions", "n1"),
                                ("n2", "claude.sessions", "n2"), ("n2", "host.sessions", "n2")], rows
    finally:
        try:
            barrier.wait(timeout=30)
        except threading.BrokenBarrierError:      # the wave never started: the assertion above is the failure to report
            pass
        thread.join(timeout=60)
    assert done["rc"] == 0 and json.loads(capsys.readouterr().out)["halt"] == "CLOSED"


# --- R2 a run that outlives its controller ----------------------------------------------------------------------------------

def test_a_run_that_outlives_its_controller_keeps_its_provider_session_while_its_lease_is_live(world):
    """The point of the slice, through the production controller: project p's wave starts n1 (its launch comes back `IN_FLIGHT`:
    another process runs it) and n2; p's controller then ends -- its thread is gone, so its rows are its dead owner's. n1's run holds
    a live lease. Project q, another controller of the same runs root, finds room for two sessions but only **one of the provider's
    two**: n1's provider session is still held (before the slice p's rows were `LOST` with its thread and q was admitted two nodes).
    When n1's lease ends, the next ask ends the rows."""
    declare(world, 4, 2)
    first = InFlight(world.root, world.repo, storage_root=world.state)
    box: dict = {}
    thread = threading.Thread(target=lambda: box.update(p=ProjectController(world.store, first, Gates(), width=2).run()),
                              name="controller-p")
    thread.start()
    thread.join(timeout=60)
    assert box["p"].halt in (HaltClass.RUN_IN_FLIGHT, HaltClass.AMBIGUOUS), (box["p"].halt, box["p"].reason)
    leases, lease = run_lease(world, "n1")
    store_q, repo_q = _second_project(world)
    second = Launcher(world.root, repo_q, storage_root=world.state)
    result = ProjectController(store_q, second, Gates(), width=2).run()
    first_round = [s for s in result.steps if s.kind == "CAPACITY_WAIT" and s.round == 1]
    assert [s.node for s in first_round] == ["m2"], [(s.node, s.detail) for s in first_round]
    assert "claude.sessions" in first_round[0].detail and "2 of 2" in first_round[0].detail, first_round[0].detail
    assert result.halt is HaltClass.CLOSED, result.reason
    assert {(h["run_id"], h["name"]): h["status"] for h in ledger(world.root)["holds"] if h["run_id"] == "n1"} == {
        ("n1", "host.sessions"): "HELD", ("n1", "claude.sessions"): "HELD"}
    leases.release(lease, reason="the run ended")
    after = ask(world, ["z"])
    assert after.admitted == ("z",) and after.ended == 2, (after.admitted, after.ended)
    assert {h["status"] for h in ledger(world.root)["holds"] if h["run_id"] == "n1"} == {"LOST"}


# --- R3 what a live lease keeps, and what ends it ---------------------------------------------------------------------------

def test_a_hold_whose_run_lease_is_live_outlives_its_owner_and_its_ttl_and_ends_when_the_lease_does(world):
    """Catches a lease that is not looked at, one that is looked at for the wrong run, and one that is believed for ever: three runs
    are admitted by one owner (a: a lease taken for a day, b: a lease that was released, c: none; a run named by more than 64
    characters is `d`, its lease under the name `rundrive.lease_name` gives it) and the owner is gone: a and d keep both their rows
    (not counted as released by `live()`, not ended by the next ask), b and c end `LOST` with it. The ttl of the rows passes: a and d
    are `HELD` still, with a lease that lives past it. When a's lease is released its rows end at the next ask; when d's lease runs
    out they end at the next. A further ask that ends nothing writes nothing."""
    res = _res()
    declare(world)
    long_name = "r" * 70
    ask(world, ["a", "b", "c", "d"], runs={"a": "a", "b": "b", "c": "c", "d": long_name}, **owner(pid=GONE))
    assert {h.get("lease") for h in ledger(world.root)["holds"] if h["run_id"] == "d"} == {rundrive.lease_name(long_name)}
    assert rundrive.lease_name(long_name) != long_name
    res.CapacityReservations(world.root, **owner(pid=3333, alive=gone)).live()
    assert not lease_dir(world).parent.exists(), "judging a row must create no lease directory"
    a_leases, a_lease = run_lease(world, "a", at=plus(NOW, 3600), ttl=86400)      # lives until NOW + 90000
    b_leases, b_lease = run_lease(world, "b")
    b_leases.release(b_lease, reason="the run ended")
    _, d_lease = run_lease(world, long_name, at=plus(NOW, 3600), ttl=86400)
    watcher = res.CapacityReservations(world.root, **owner(pid=3333, alive=gone))
    assert {h.run_id for h in watcher.live()} == {"a", "d"}                          # b and c: their owner is gone, nothing says the run is there
    ask(world, ["e"], **owner(pid=3333, alive=gone))
    assert {k: v for k, v in statuses(world.root).items() if k != "e"} == {"a": "HELD", "b": "LOST", "c": "LOST", "d": "HELD"}
    later = res.CapacityReservations(world.root, **owner(pid=3334, clock=lambda: plus(NOW, 86400)))
    assert {h.run_id for h in later.live()} == {"a", "d"}                            # the rows' ttl is past; the leases are not
    ask(world, ["f"], **owner(pid=3334, clock=lambda: plus(NOW, 86400)))
    assert {k: v for k, v in statuses(world.root).items() if k not in ("e", "f")} == {
        "a": "HELD", "b": "LOST", "c": "LOST", "d": "HELD"}
    assert statuses(world.root)["e"] == "EXPIRED"                                    # a row with no lease still ends at its ttl
    a_leases.release(a_lease, reason="the run ended")
    ask(world, ["g"], **owner(pid=3335, clock=lambda: plus(NOW, 86400), alive=gone))
    assert statuses(world.root)["a"] == "LOST" and statuses(world.root)["d"] == "HELD"
    expired = {h.run_id for h in res.CapacityReservations(world.root, **owner(pid=3336, clock=lambda: plus(NOW, 90000))).live()}
    assert "d" not in expired                                                        # the lease itself ran out at NOW + 90000
    ask(world, ["h"], **owner(pid=3336, clock=lambda: plus(NOW, 90000)))
    assert statuses(world.root)["d"] == "EXPIRED"
    world.registry(("machine", "host.sessions", 0, "sessions", "declared"))          # a figure that admits nobody
    bytes_ = (world.root / LEDGER).read_bytes()
    for tick in (90001, 90002):                                                      # the clock moves: a rewrite would show
        again = ask(world, ["z"], **owner(pid=3337, clock=lambda tick=tick: plus(NOW, tick)))
        assert again.admitted == () and again.ended == 0
    assert (world.root / LEDGER).read_bytes() == bytes_
    assert d_lease.run_id == rundrive.lease_name(long_name)


def test_a_lease_that_is_lost_or_taken_over_says_nothing_for_the_row_and_a_live_one_of_another_name_neither(world):
    """Catches a link that is a prefix or a substring: the row of `a` is kept by a live lease **named `a`** only -- a live lease
    named `ab`, a lease of the same name in another project of the runs root (not the `hoh run` namespace) and a lease that has
    run out (`LOST`) keep nothing."""
    res = _res()
    declare(world)
    ask(world, ["a"], runs={"a": "a"}, **owner(pid=GONE))
    run_lease(world, "ab")
    world.lease("other", "a")
    run_lease(world, "a", at=plus(NOW, -86400), ttl=3600)                              # taken a day ago for an hour: lost
    assert res.CapacityReservations(world.root, **owner(pid=3333, alive=gone)).live() == []
    ask(world, ["z"], **owner(pid=3333, alive=gone))
    assert statuses(world.root)["a"] == "LOST"


@pytest.mark.parametrize("label", ["not JSON", "non-UTF-8", "oversize", "a directory", "a FIFO", "a link", "a second name",
                                   "the lease directory is a link"])
def test_a_run_lease_that_cannot_be_read_is_not_a_free_run_the_hold_is_kept_and_nobody_is_admitted(world, label, alarm_free_timeout):
    """Fail closed: the lease file of the run is spoiled. The owner's death does not end the row (a hold that cannot be known is not
    a hold of zero) -- `live()` still counts it, the next ask ends nothing and admits nobody (the holds cannot be read), the ledger's
    bytes are unchanged. Moving the file aside (a versioned rename, as the refusal says) makes the run's lease absent: the row then
    ends with its owner and the ask is admitted."""
    res = _res()
    declare(world)
    ask(world, ["a"], runs={"a": "a"}, **owner(pid=GONE))
    directory = lease_dir(world)
    if label == "the lease directory is a link":
        (world.root / "elsewhere").mkdir()
        directory.parent.mkdir(parents=True)
        directory.symlink_to(world.root / "elsewhere")
        target = world.root / "elsewhere" / "a.json"
        target.write_bytes(b"{}")
    else:
        directory.mkdir(parents=True)
        target = directory / "a.json"
        if label == "not JSON":
            target.write_bytes(b"{not json")
        elif label == "non-UTF-8":
            target.write_bytes(b"\xff\xfe{\x00}")
        elif label == "oversize":
            target.write_bytes(b" " * 8192)
        elif label == "a second name":
            (world.root / "elsewhere").write_text("{}")
            os.link(world.root / "elsewhere", target)
        elif label == "a directory":
            target.mkdir()
        elif label == "a FIFO":
            os.mkfifo(target)
        else:
            (world.root / "elsewhere").write_text("{}")
            target.symlink_to(world.root / "elsewhere")
    before = (world.root / LEDGER).read_bytes()
    with alarm_free_timeout():
        assert {h.run_id for h in res.CapacityReservations(world.root, **owner(pid=3333, alive=gone)).live()} == {"a"}
        gate = ask(world, ["z"], **owner(pid=3333, alive=gone))
    assert gate.admitted == () and gate.configured and "run lease" in gate.why, gate.why
    assert (world.root / LEDGER).read_bytes() == before
    aside = directory if label == "the lease directory is a link" else target
    aside.rename(aside.with_name(aside.name + ".v1.aside"))
    again = ask(world, ["z"], **owner(pid=3333, alive=gone))
    assert again.admitted == ("z",) and statuses(world.root)["a"] == "LOST"


def test_a_run_lease_that_cannot_be_read_never_keeps_a_hold_past_its_ttl(world):
    """The bound: an unreadable lease suspends the owner-gone rule, not the ttl. The row ends `EXPIRED` at its `expires_at` (a day
    after it was taken) even though the lease is still unreadable -- it is the ask's refusal that is visible, the row is not held for ever."""
    res = _res()
    declare(world)
    ask(world, ["a"], runs={"a": "a"}, **owner(pid=GONE))
    lease_dir(world).mkdir(parents=True)
    (lease_dir(world) / "a.json").write_bytes(b"{not json")
    clock = lambda: plus(NOW, 86400)       # noqa: E731
    assert res.CapacityReservations(world.root, **owner(pid=3333, clock=clock)).live() == []
    gate = ask(world, ["z"], **owner(pid=3333, clock=clock))
    assert gate.admitted == () and statuses(world.root)["a"] == "EXPIRED"


# --- R4 one run, one session ------------------------------------------------------------------------------------------------

def test_a_running_runs_session_is_counted_once_through_its_lease_and_a_lease_hides_at_most_one_row(world):
    """Catches a count that charges a run twice, one that lets a lease hide another owner's row, and a dedupe by the wrong key: two
    owners each hold a node `t1` (run `t1`) and the run `t1` takes its lease -> two sessions (the lease and one row), not three and not
    one. A lease in another project of the runs root (not the `hoh run` namespace), a lease that was released and a row that names no
    lease do not absorb a row. The rows of other kinds (the provider's, a device's) are counted in full whatever the lease says."""
    res = _res()
    declare(world)
    ask(world, ["t1"], runs={"t1": "t1"}, **owner(pid=2222))
    ask(world, ["t1"], runs={"t1": "t1"}, **owner(pid=3333))
    leases, lease = run_lease(world, "t1")
    held = _check().live_holds(world.root)
    assert (held.sessions, held.reserved, held.absorbed) == (2, 1, 1), (held.sessions, held.reserved, held.absorbed)
    amounts = {(a.kind, a.name): a.amount for a in held.amounts}
    assert amounts[("provider", "claude.sessions")] == 2                              # the lease does not count the provider's session
    res.CapacityReservations(world.root, **owner(pid=4444)).reserve_atomically(
        lambda: [("t1", "machine", "gpu.devices", 2, "items", "t1")])
    assert {(a.kind, a.name): a.amount for a in _check().live_holds(world.root).amounts}[("machine", "gpu.devices")] == 2
    leases.release(lease, reason="the run ended")
    released = _check().live_holds(world.root)
    assert (released.sessions, released.reserved, released.absorbed) == (2, 2, 0)                           # no live lease: both rows count, as before


def test_a_lease_of_another_project_or_a_row_without_a_lease_does_not_absorb_a_row(world):
    """The key is the `hoh run` namespace and the lease name: a live lease `t1` in another project, and a live `hoh run` lease `t1`
    beside a row that names no lease (a caller that did not say), are two sessions each -- over-counted, never under-counted."""
    declare(world, 8, None)
    ask(world, ["t1"], runs={"t1": "t1"}, **owner(pid=2222))
    world.lease("other", "t1")
    assert _check().live_holds(world.root).sessions == 2
    fresh = World(world.tmp / "again")
    fresh.sessions(8)
    ask(fresh, ["t1"], **owner(pid=2222))
    run_lease(fresh, "t1")
    held = _check().live_holds(fresh.root)
    assert (held.sessions, held.reserved) == (2, 1)


# --- R5 the link, written by the admission ----------------------------------------------------------------------------------

def test_the_controller_names_the_lease_of_each_nodes_run_by_its_run_id_else_its_id(world):
    """Catches a link made from the node id alone: a node whose run has another id (`TaskNode.run_id`, here longer than a lease name
    may be) holds its rows under the name `rundrive.lease_name` gives that run -- the name `hoh run` keeps its lease under -- and the
    others under their ids (the launcher's own rule, `node.run_id or node.id`)."""
    world.sessions(8)
    controller = ProjectController(world.store, world.launcher(), Gates(), width=3)
    state = world.store.read_state()
    nodes = [n.model_copy(update={"run_id": "r" * 70}) if n.id == "n2" else n for n in state.nodes]
    assert controller._capacity_admission(nodes, 1, [], state, 3) == {"n1", "n2", "n3"}
    assert {h["run_id"]: h.get("lease") for h in ledger(world.root)["holds"]} == {
        "n1": "n1", "n2": rundrive.lease_name("r" * 70), "n3": "n3"}


def test_a_row_names_its_lease_only_when_it_has_one_and_a_name_that_is_not_one_is_refused(world):
    """The ledger's spelling: a row with a lease has the key, a row with none has not (a ledger nothing links is byte-for-byte what it
    was). `reserve_atomically` takes the old five-tuple, a six-tuple whose last element is a lease name or None, and refuses anything
    else with nothing written; a stored row that names a lease that is not a name is a ledger that is refused whole."""
    res = _res()
    world.sessions(8)
    ask(world, ["a", "b"], runs={"a": "a", "x": "x"}, **owner(pid=1111))
    rows = {h["run_id"]: h for h in ledger(world.root)["holds"]}
    assert rows["a"].get("lease") == "a" and "lease" not in rows["b"] and set(rows) == {"a", "b"}
    book = res.CapacityReservations(world.root, **owner(pid=2222))
    before = (world.root / LEDGER).read_bytes()
    for bad in (("e", "machine", "gpu.devices", 1, "items", "a b"), ("e", "machine", "gpu.devices", 1, "items", "../x"),
                ("e", "machine", "gpu.devices", 1, "items", "x" * 65), ("e", "machine", "gpu.devices", 1, "items", "x" * 300), ("e", "machine", "gpu.devices", 1, "items", 7),
                ("e", "machine", "gpu.devices", 1, "items", "a\n"), ("e", "machine", "gpu.devices", 1, "items", "a\x00b"),
                ("e", "machine", "gpu.devices", 1, "items", "a\ud800"),
                ("e", "machine", "gpu.devices", 1, "items", "a", "b"), ("e", "machine", "gpu.devices", 1)):
        with pytest.raises(res.ReservationError):
            book.reserve_atomically(lambda bad=bad: [bad])
    assert (world.root / LEDGER).read_bytes() == before
    book.reserve_atomically(lambda: [("c", "machine", "gpu.devices", 1, "items", None), ("d", "machine", "gpu.devices", 1, "items")])
    assert all("lease" not in h for h in ledger(world.root)["holds"] if h["run_id"] in ("c", "d"))
    before = (world.root / LEDGER).read_bytes()
    for bad_name in ("a b", 7, ["a"], "a\n", "", "x" * 65, "-a"):
        spoiled = json.loads(before)
        spoiled["holds"][0]["lease"] = bad_name
        (world.root / LEDGER).write_text(json.dumps(spoiled))
        with pytest.raises(res.ReservationError):
            book.read()
        assert ask(world, ["z"], **owner(pid=3333)).admitted == ()


def test_runs_that_are_not_a_map_of_node_ids_to_run_ids_admit_nobody(world):
    """A caller that hands `admit_wave` something else for `runs` is a bad request: nobody is admitted, nothing is held."""
    world.sessions(8)
    for bad in (["a"], {"a": 1}, {1: "a"}, "a"):
        gate = ask(world, ["a"], runs=bad, **owner(pid=1111))
        assert gate.admitted == () and "run" in gate.why, (bad, gate.why)
    assert not (world.root / LEDGER).exists()


# --- R6 compatibility: a ledger nothing links to a run is what it was ------------------------------------------------------

def test_a_ledger_of_rows_without_a_lease_is_read_judged_and_written_as_before_and_no_lease_directory_is_looked_at(world, monkeypatch):
    """The compatibility row (green on the base, kept true by the slice): a ledger written before the field -- rows with no `lease`
    -- is read; its dead owner's row ends `LOST` at the next ask; no row of the ledger has a `lease` key afterwards; and nothing
    reads a run lease for a row that names none (`rundrive.consult` is never called: a host that links nothing pays nothing)."""
    res = _res()
    world.sessions(8)
    ask(world, ["a"], **owner(pid=GONE))
    monkeypatch.setattr(rundrive, "consult", lambda *a, **k: pytest.fail("a run lease was read for a row that names none"))
    assert [h.run_id for h in res.CapacityReservations(world.root, **owner(pid=3333)).live()] == ["a"]
    assert res.CapacityReservations(world.root, **owner(pid=3333, alive=gone)).live() == []
    ask(world, ["z"], **owner(pid=3333, alive=gone))
    holds = ledger(world.root)["holds"]
    assert statuses(world.root)["a"] == "LOST" and all("lease" not in h for h in holds)


# --- R13 the recovery keeps what a run that outlives its crashed controller holds ----------------------------------------------

def _crash_left_world(tmp_path, capsys, monkeypatch):
    """A controller that dies at the dispatch of its wave, after its two nodes reached RUNNING: the ledger holds their rows (naming
    the leases `n1` and `n2`), the wave record is OPEN, and the master is gone -- what `test_wave_recovery.py` leaves."""
    wr, repo, root, store = _recovery_world(tmp_path, monkeypatch)
    original = ProjectController._authorise

    def interrupt_at_dispatch(self, act):
        if act.startswith("dispatch of wave"):
            raise KeyboardInterrupt()
        return original(self, act)

    monkeypatch.setattr(ProjectController, "_authorise", interrupt_at_dispatch)
    wr.CONFIG["dying"] = "gone"
    wr.interrupted(root, capsys)
    wr.CONFIG["dying"] = False
    monkeypatch.setattr(ProjectController, "_authorise", original)
    wr.kill_the_master(store)
    found = [o.wave_id for o in wr.outcomes(store).values()]
    wr.CONFIG["crashed"] = found[0] if found else None
    assert {n.lifecycle for n in wr._nodes(store).values()} == {Lifecycle.RUNNING}
    assert {h["run_id"]: h.get("lease") for h in ledger(root)["holds"]} == {"n1": "n1", "n2": "n2"}
    return wr, root, store


def test_the_recovery_of_a_crash_left_wave_keeps_the_rows_of_a_run_whose_lease_is_live_and_settles_the_others(
        tmp_path, capsys, monkeypatch):
    """The recovery window (review A's F1, reproduced): both nodes are RUNNING when their controller dies, so the recovery reaches the
    terminal write without probing them and calls the capacity callback. n1's run holds a live lease and n2's lease was released:
    n1's row stays `HELD` (the run outlives its crashed controller), n2's is `SETTLED` by the recovery; the outcome says it settled
    one row; a further drive changes no byte of the ledger or the outcome. Before the slice both rows were settled."""
    wr, root, store = _crash_left_world(tmp_path, capsys, monkeypatch)
    leases = rundrive._leases(root)
    leases.acquire("n1", "hoh-run-1", ttl_seconds=300)
    leases.release(leases.acquire("n2", "hoh-run-2", ttl_seconds=300), reason="the run ended")
    assert rundrive.consult(root, "n1").state == "LIVE" and rundrive.consult(root, "n2").state == "RELEASED"
    rc, out = wr.drive(root, capsys)
    assert any(step.startswith("WAVE_CAPACITY_SETTLED") for step in out["steps"]), out["steps"]
    assert {h["run_id"]: h["status"] for h in ledger(root)["holds"]} == {"n1": "HELD", "n2": "SETTLED"}
    path, outcome = wr.the_outcome(store)
    assert "settled 1 capacity row(s)" in outcome.note, outcome.note
    fixed, bytes_ = wr.snapshot(path), (root / LEDGER).read_bytes()
    rc, again = wr.drive(root, capsys)
    assert wr.snapshot(path) == fixed and (root / LEDGER).read_bytes() == bytes_
    assert not [s for s in again["steps"] if s.startswith("WAVE_CAPACITY")], again["steps"]


def test_settle_owned_keeps_the_rows_of_a_live_or_unreadable_lease_only_when_asked_and_a_second_call_writes_nothing(world):
    """`settle_owned(keep_live=True)` over one owner's rows: a (live lease) and b (a lease that cannot be read) stay `HELD`, c (no lease)
    and d (a released lease) are `SETTLED` (two rows each: the session and the provider's); a second call returns 0 and writes nothing; without `keep_live` the same call ends every row."""
    res = _res()
    declare(world)
    ask(world, ["a", "b", "c", "d"], runs={n: n for n in "abcd"}, **owner(pid=GONE))
    run_lease(world, "a")
    leases, lease = run_lease(world, "d")
    leases.release(lease, reason="the run ended")
    (lease_dir(world) / "b.json").write_bytes(b"{not json")
    book = res.CapacityReservations(world.root, **owner(pid=3333))
    assert book.settle_owned("recovered", pid=GONE, keep_live=True) == 4          # c and d: two rows each
    assert {k: v for k, v in statuses(world.root).items()} == {
        "a": "HELD", "b": "HELD", "c": "SETTLED", "d": "SETTLED"}
    bytes_ = (world.root / LEDGER).read_bytes()
    assert book.settle_owned("recovered", pid=GONE, keep_live=True) == 0 and (world.root / LEDGER).read_bytes() == bytes_
    assert book.settle_owned("recovered", pid=GONE) == 4                                 # a and b: two rows each
    assert set(statuses(world.root).values()) == {"SETTLED"}


# --- R14 the lease is judged in the ledger object's view of the machine -------------------------------------------------------

@pytest.mark.parametrize("label", ["holder gone", "holder pid reused", "holder on another boot", "holder in another pid namespace"])
def test_a_lease_is_judged_in_the_ledgers_view_of_the_machine_not_the_real_one(world, label):
    """Review B's F1: the facts of the ledger object (host, boot id, `alive`, pid namespace, start time) are the ones `consult` judges the
    lease by. A lease whose holder those facts call gone, a reused pid or a restarted machine does not keep the row of its owner's
    death (the row ends `LOST`); a holder in another pid namespace cannot be judged by its pid, only by its ttl, so the lease
    is live and keeps the row. Every test lease of the other nodes is held by the live test process, which hides these."""
    res = _res()
    declare(world)
    ask(world, ["a"], runs={"a": "a"}, **owner(pid=GONE))
    holder = dict(host="host-1", pid=5555, boot=lambda: "boot-A", pidns=lambda: "ns-1", start=lambda p: "start-1",
                  alive=lambda p: True)
    if label == "holder in another pid namespace":
        holder["pidns"] = lambda: "ns-elsewhere"
    RunLeases(lease_dir(world), **holder).acquire(rundrive.lease_name("a"), "hoh-run", ttl_seconds=3600)
    asker = owner(pid=3333, alive=gone, boot=lambda: "boot-A", start=lambda p: "start-1")
    asker["alive"] = lambda p: p not in (GONE, 5555) if label == "holder gone" else p != GONE
    if label == "holder pid reused":
        asker["start"] = lambda p: "start-2"
    if label == "holder on another boot":
        asker["boot"] = lambda: "boot-B"
    kept = {h.run_id for h in res.CapacityReservations(world.root, **asker).live()}
    ask(world, ["z"], **asker)
    if label == "holder in another pid namespace":
        assert kept == {"a"} and statuses(world.root)["a"] == "HELD"
    else:
        assert kept == set() and statuses(world.root)["a"] == "LOST"


# --- R15 limit L1 stays what it is ----------------------------------------------------------------------------------------------

def test_the_owners_own_next_ask_still_supersedes_its_earlier_rows_though_their_lease_is_live(world):
    """Limit L1, pinned so that nobody widens it by accident: one owner asks, its run takes a live lease, the same owner asks again
    (it is the same process and thread): the earlier rows are `SUPERSEDED` (a controller runs one wave at a time), the live lease
    notwithstanding; the run's session is still counted, through its lease."""
    declare(world)
    ask(world, ["a"], runs={"a": "a"}, **owner(pid=2222))
    run_lease(world, "a")
    ask(world, ["b"], runs={"b": "b"}, **owner(pid=2222))
    assert {k: v for k, v in statuses(world.root).items()} == {"a": "SUPERSEDED", "b": "HELD"}
    assert _check().live_holds(world.root).sessions == 2                              # the lease of a, the row of b
