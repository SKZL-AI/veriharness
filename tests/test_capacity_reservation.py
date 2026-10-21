"""P3-04, capacity chain step C4: what a wave was admitted against is held, atomically with the decision.

`capacitycheck.admit_wave` (P3-19 slice 2) judged; it did not claim. Two controllers on one host could each be told
"one session is free". This file pins `reservation.CapacityReservations` and its use by the gate: the hold is taken in the
same locked step as the decision, it ends by its owner asking again, by its owner's death, by its ttl or by `settle`, every
one of those paths ends in a visible terminal row, and nothing here waits without an end.

What is real: the ledger and its lock, `capacitycheck`, `ProjectController`, `ParallelWaveExecutor`, `HohRunLauncher` (only
its process boundary replaced, as in `tests/test_wave_controller.py`), real run leases, the registry. An "other owner" is
the same module with another pid and an `alive` that says yes: the facts of a process are injectable by design. No agent, no
Herdr, no network. The harness of `tests/test_wave_capacity.py` is imported, not copied.
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest
from test_wave_capacity import (  # noqa: F401 - fixtures are collected from here
    Gates, Launcher, World, git, hermetic_environment, kinds, utc, waves,
)
from test_wave_capacity import world as world  # the alias form is an explicit re-export: no F811 on the parameters

from hoh.orchestrator import HaltClass, ProjectController
from hoh.project import Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore

LEDGER = "_capacity-reservations.json"
NOW = utc()                       # the clock the rows are written with; the TTL tests move it by whole seconds


def plus(moment: str, seconds: int) -> str:
    from datetime import UTC, datetime, timedelta
    return (datetime.strptime(moment, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _res():
    import importlib
    return importlib.import_module("hoh.reservation")


def _check():
    import importlib
    return importlib.import_module("hoh.capacitycheck")


def owner(pid: int = 4242, **kw):
    """Facts of a process that is somebody else's and alive (nothing about it can be ended by a test)."""
    facts = dict(pid=pid, thread=7, boot=lambda: "", host="host-1", pidns=lambda: "ns-1",
                 start=lambda p: f"start-{p}", alive=lambda p: True, clock=lambda: NOW,
                 live_threads=lambda: {7})
    facts.update(kw)
    return facts


def ledger(root: Path) -> dict:
    return json.loads((root / LEDGER).read_text(encoding="utf-8"))


def statuses(root: Path) -> dict[str, str]:
    return {h["run_id"]: h["status"] for h in ledger(root)["holds"]}


@pytest.fixture
def alarm_free_timeout():
    """A context manager that fails the test when its body takes more than 5 s (a FIFO must not block a reader)."""
    import contextlib
    import signal

    @contextlib.contextmanager
    def watch():
        def fire(signum, frame):
            raise AssertionError("blocked for 5 s")
        old = signal.signal(signal.SIGALRM, fire)
        signal.alarm(5)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
    return watch


def ask(world, nodes, **env):
    """`admit_wave` as the controller calls it, with `now` a function (the real clock) and the facts of a given owner."""
    return _check().admit_wave(nodes, runs_root=world.root, state_root=world.state, now=utc, **env)


# --- R1 two controllers, one capacity --------------------------------------------------------------------------------

def _second_project(world: World, names=("m1", "m2")):
    repo = world.tmp / "repo-q"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    (repo / "README").write_text("x\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first")
    nodes = []
    for name in names:
        spec = world.tmp / f"{name}.md"
        spec.write_text(f"# {name}\n", encoding="utf-8")
        nodes.append(TaskNode(id=name, spec_path=str(spec), writes=[f"{name}.txt"]))
    store = ProjectStore(world.root, "q")
    store.create(ProjectState(project_id="q", repo_path=str(repo), nodes=nodes))
    return store, repo


def test_a_second_controller_is_not_admitted_against_capacity_the_first_holds_while_its_wave_runs(world):
    """Catches the double admission this slice exists for: registry of 2 sessions; project p's wave of two is *inside* `hoh
    run` (a barrier of three, the test being the third party); project q, another controller of the same runs root,
    asks for its two nodes: `CAPACITY_WAIT` ("2 of 2 sessions ... held"), nothing prepared, its nodes READY. When p's wave
    has ended q is admitted and closes -- p's rows were settled by the wave's end (C4b), not left for ever."""
    world.sessions(2)
    barrier = threading.Barrier(3)
    first = world.launcher(barrier=barrier)
    outcome: dict = {}

    def drive_first():
        outcome["first"] = ProjectController(world.store, first, Gates(), width=2).run()
    thread = threading.Thread(target=drive_first, name="controller-p")
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while first.running < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert first.running == 2, "the first wave never started"
        store_q, repo_q = _second_project(world)
        second = Launcher(world.root, repo_q, storage_root=world.state)
        refused = ProjectController(store_q, second, Gates(), width=2).run()
        assert refused.halt is HaltClass.CAPACITY_WAIT, (refused.halt, refused.reason)
        assert "2 of 2 sessions" in refused.reason and second.calls == []
        assert {n.lifecycle for n in store_q.read_state().nodes} == {Lifecycle.READY}
        assert set(statuses(world.root).values()) == {"HELD"} and len(statuses(world.root)) == 2
    finally:
        barrier.wait(timeout=30)
        thread.join(timeout=60)
    assert outcome["first"].halt is HaltClass.CLOSED, outcome["first"].reason
    again = ProjectController(store_q, second, Gates(), width=2).run()
    assert again.halt is HaltClass.CLOSED, again.reason
    held = ledger(world.root)["holds"]
    assert {h["status"] for h in held if h["run_id"] in ("n1", "n2")} == {"SETTLED"}     # the wave ended: C4b
    assert all("ended" in h["reason"] for h in held if h["run_id"] in ("n1", "n2"))


def test_two_owners_asking_one_registry_are_one_after_the_other(world):
    """Catches a check-then-act race in the adapter: with 2 sessions declared, owner A asks for a, b, c and is admitted a
    and b; owner B (another process: another pid, alive) asks for x and y and is admitted **nobody** -- the ledger holds
    a and b -- and B's reason says "2 of 2 sessions". The ledger rows carry the owner and an `expires_at`."""
    world.sessions(2)
    first = ask(world, ["a", "b", "c"], **owner(pid=1111))
    assert first.admitted == ("a", "b") and first.reserved == ("a", "b") and first.ended == 0
    second = ask(world, ["x", "y"], **owner(pid=2222))
    assert second.configured and second.admitted == () and second.reserved == ()
    assert all("2 of 2 sessions" in why for _, why in second.waits)
    rows = ledger(world.root)["holds"]
    assert [(h["run_id"], h["status"], h["owner_pid"]) for h in rows] == [("a", "HELD", 1111), ("b", "HELD", 1111)]
    assert all(h["expires_at"] == plus(NOW, 86400) and (h["kind"], h["name"], h["amount"], h["unit"]) ==
               ("machine", "host.sessions", 1, "sessions") for h in rows)
    assert _check().live_holds(world.root).reserved == 2


# --- R2 what ends a hold -------------------------------------------------------------------------------------------------

def test_a_slow_decision_keeps_a_second_caller_out_until_its_claim_is_written(world):
    """Catches the check-then-act race itself, deterministically: owner A's decision is held inside the locked step
    (its `now()` is called after the holds were read, and blocks) while owner B, in another thread, asks for the same
    two sessions. B must wait for the lock -- it is not decided against the holds A has not written yet -- and then
    sees A's claim: A is admitted both nodes, B nobody (\"2 of 2 sessions\"), and B really waited."""
    world.sessions(2)
    entered, release = threading.Event(), threading.Event()
    got: dict = {}

    def slow_now() -> str:
        entered.set()
        assert release.wait(20), "the test never released the first decision"
        return utc()

    def first() -> None:
        got["a"] = _check().admit_wave(["a1", "a2"], runs_root=world.root, state_root=world.state, now=slow_now,
                                       **owner(pid=1111))

    def second() -> None:
        started = time.monotonic()
        got["b"] = ask(world, ["b1", "b2"], **owner(pid=2222))
        got["b_waited"] = time.monotonic() - started

    a = threading.Thread(target=first)
    b = threading.Thread(target=second)
    try:
        a.start()
        assert entered.wait(20)
        b.start()
        time.sleep(0.6)                               # B is in the lock's retry loop, not decided
        assert "b" not in got
    finally:
        release.set()                                 # a failed assertion must not leave A waiting for 20 s
        a.join(30)
        if b.ident is not None:
            b.join(30)
    assert got["a"].admitted == ("a1", "a2") and got["b"].admitted == ()
    assert "2 of 2 sessions" in got["b"].why and got["b_waited"] >= 0.5
    assert [(h["run_id"], h["status"]) for h in ledger(world.root)["holds"]] == [("a1", "HELD"), ("a2", "HELD")]


def test_the_owner_asking_again_ends_its_earlier_wave_and_nobody_elses(world):
    """Catches a hold that outlives the wave of a controller that goes on: owner A holds a and b; B (limit 4) holds c;
    A asks again for d and e: a and b are `SUPERSEDED` (reason: the owner asked again), c stays `HELD`, d and e are
    admitted; and the same owner asking for the same node twice in a row holds it once."""
    world.sessions(4)
    ask(world, ["a", "b"], **owner(pid=1111))
    ask(world, ["c"], **owner(pid=2222))
    third = ask(world, ["d", "e"], **owner(pid=1111))
    assert third.admitted == ("d", "e") and third.ended == 2
    assert statuses(world.root) == {"a": "SUPERSEDED", "b": "SUPERSEDED", "c": "HELD", "d": "HELD", "e": "HELD"}
    assert ledger(world.root)["holds"][0]["reason"] == "the owner asked again: its earlier wave is over"
    assert {h["run_id"]: h["ended_at"] for h in ledger(world.root)["holds"]} == {
        "a": NOW, "b": NOW, "c": None, "d": None, "e": None}                # an ended row says when
    ask(world, ["d"], **owner(pid=1111))
    assert [h["status"] for h in ledger(world.root)["holds"] if h["run_id"] == "d"] == ["SUPERSEDED", "HELD"]


def test_a_gone_owner_a_past_ttl_and_a_settled_run_end_their_holds_once_and_quietly(world):
    """Catches a hold that nobody ends: (1) the owner's pid no longer exists -> `LOST` by the next ask of anyone, and
    `live()` does not count it even before that; (2) the owner is alive but the ttl has passed (exactly at
    `expires_at` is past; one second before is live) -> `EXPIRED`; (3) `settle(run_id)` -> `SETTLED`; every ended row
    is left as it is by a further ask (the ledger bytes do not change)."""
    res = _res()
    world.sessions(4)
    ask(world, ["a"], **owner(pid=1111))
    ask(world, ["b"], **owner(pid=1112))
    ask(world, ["c"], **owner(pid=1113))
    gone = res.CapacityReservations(world.root, **owner(pid=3333, alive=lambda p: p != 1111))
    assert {h.run_id for h in gone.live()} == {"b", "c"}                 # a's owner is gone: not counted, not yet ended
    assert statuses(world.root)["a"] == "HELD"
    ask(world, ["d"], **owner(pid=3333, alive=lambda p: p != 1111))
    assert statuses(world.root)["a"] == "LOST"
    assert [h["ended_at"] for h in ledger(world.root)["holds"] if h["run_id"] == "a"] == [NOW]
    assert "no longer exists" in [h for h in ledger(world.root)["holds"] if h["run_id"] == "a"][0]["reason"]
    before = res.CapacityReservations(world.root, **owner(pid=3333, clock=lambda: plus(NOW, 86399)))
    assert {h.run_id for h in before.live()} == {"b", "c", "d"}          # one second before the ttl
    after = res.CapacityReservations(world.root, **owner(pid=3333, clock=lambda: plus(NOW, 86400)))
    assert after.live() == []                                             # exactly at expires_at: past
    ask(world, ["e"], **owner(pid=3333, clock=lambda: plus(NOW, 86400)))
    assert {k: v for k, v in statuses(world.root).items() if k != "e"} == {
        "a": "LOST", "b": "EXPIRED", "c": "EXPIRED", "d": "EXPIRED"}
    assert {h["run_id"]: h["ended_at"] for h in ledger(world.root)["holds"] if h["status"] == "EXPIRED"} == {
        k: plus(NOW, 86400) for k in ("b", "c", "d")}
    ask(world, ["f"], **owner(pid=4444, clock=lambda: plus(NOW, 86400)))
    assert res.CapacityReservations(world.root, **owner(clock=lambda: plus(NOW, 86401))).settle("e", "r" * 500) == 1
    assert statuses(world.root)["e"] == "SETTLED" and statuses(world.root)["f"] == "HELD"     # only that run's holds
    settled = [h for h in ledger(world.root)["holds"] if h["run_id"] == "e"][0]
    assert settled["ended_at"] == plus(NOW, 86401) and settled["reason"] == "r" * 200       # the reason is cut
    assert res.CapacityReservations(world.root, **owner()).settle("e", "x") == 0
    bytes_ = (world.root / LEDGER).read_bytes()
    world.registry(("machine", "host.sessions", 0, "sessions", "declared"))      # a figure that admits nobody
    for _ in range(2):
        again = ask(world, ["z"], **owner(pid=3334, clock=lambda: plus(NOW, 86400)))
        assert again.admitted == () and again.ended == 0
    assert (world.root / LEDGER).read_bytes() == bytes_


# --- R3 an unusable ledger admits nobody -------------------------------------------------------------------------------

def _spoil(world: World, label: str) -> None:
    ledger_path = world.root / LEDGER
    if label == "not JSON":
        ledger_path.write_bytes(b"{not json")
    elif label == "non-UTF-8":
        ledger_path.write_bytes(b"\xff\xfe{\x00}")
    elif label == "a JSON array":
        ledger_path.write_text("[]")
    elif label == "mode 000":
        if os.geteuid() == 0:                       # root reads through a mode: nothing to prove as root
            pytest.skip("mode 000 does not stop root")
        ledger_path.write_text(json.dumps({"schema_version": "hoh-capacity-reservations/1", "holds": []}))
        ledger_path.chmod(0)
    elif label == "a second name":
        (world.root / "elsewhere.json").write_text(
            json.dumps({"schema_version": "hoh-capacity-reservations/1", "holds": []}))
        os.link(world.root / "elsewhere.json", ledger_path)
    elif label == "a stored run id the root keeps":
        row = dict(json.loads((world.root / "_capacity-reservations.json.set-aside").read_text())["holds"][0],
                   run_id="projects")
        ledger_path.write_text(json.dumps({"schema_version": "hoh-capacity-reservations/1", "holds": [row]}))
    elif label == "wrong schema":
        ledger_path.write_text(json.dumps({"schema_version": "x/1", "holds": []}))
    elif label == "an invalid row":
        ledger_path.write_text(json.dumps({"schema_version": "hoh-capacity-reservations/1", "holds": [{"run_id": "a"}]}))
    elif label == "oversize":
        ledger_path.write_bytes(b" " * (16 * 1024 * 1024 + 1))
    elif label == "a directory":
        ledger_path.mkdir()
    elif label == "a link":
        (world.root / "elsewhere.json").write_text("{}")
        ledger_path.symlink_to(world.root / "elsewhere.json")
    elif label == "a dangling link":
        ledger_path.symlink_to(world.root / "nowhere.json")
    elif label == "a FIFO":
        os.mkfifo(ledger_path)
    elif label == "the lock is a directory":
        (world.root / "_capacity-reservations.lock").mkdir()
    elif label == "the lock is a link":
        (world.root / "somewhere").write_bytes(b"")
        (world.root / "_capacity-reservations.lock").symlink_to(world.root / "somewhere")
    else:
        raise AssertionError(label)


SPOILED = ["not JSON", "non-UTF-8", "a JSON array", "mode 000", "a second name", "a stored run id the root keeps", "wrong schema", "an invalid row", "oversize", "a directory", "a link", "a dangling link", "a FIFO",
           "the lock is a directory", "the lock is a link"]


@pytest.mark.parametrize("label", SPOILED)
def test_a_ledger_or_lock_that_cannot_be_used_admits_nobody_and_is_left_alone(world, label, alarm_free_timeout):
    """Catches a gate that admits when the claim cannot be recorded (the over-admission this slice prevents): each
    spoiled ledger or lock makes the gate a configured one with nobody admitted and the exception named, within a
    second (a FIFO must not block), the spoiled file untouched; the control (no ledger) admits."""
    world.sessions(2)
    assert ask(world, ["a"], **owner(pid=1111)).admitted == ("a",)
    first = (world.root / LEDGER).read_bytes()
    if label in ("the lock is a directory", "the lock is a link"):
        (world.root / "_capacity-reservations.lock").rename(world.root / "_capacity-reservations.lock.set-aside")
    else:
        (world.root / LEDGER).rename(world.root / "_capacity-reservations.json.set-aside")
    _spoil(world, label)
    started = time.monotonic()
    gate = ask(world, ["b"], **owner(pid=2222))
    assert time.monotonic() - started < 5
    assert gate.configured and gate.admitted == () and gate.reserved == ()
    assert gate.why.startswith("capacity cannot be judged") and "ReservationError" in gate.why
    if label not in ("the lock is a directory", "the lock is a link"):
        assert (world.root / "_capacity-reservations.json.set-aside").read_bytes() == first
        with alarm_free_timeout():
            with pytest.raises(_check().CheckError) as refused:
                _check().live_holds(world.root)          # `hoh capacity check` reads the same ledger: no decision either
        assert "capacity reservations cannot be read" in str(refused.value)


def test_a_lock_that_is_held_is_a_refusal_after_the_stated_wait_and_not_a_hang(world, monkeypatch):
    """Catches an unbounded wait: another process holds the ledger lock; the gate gives up after
    `CAPACITY_LOCK_TIMEOUT_SECONDS` (5 s by default: the literal below) and admits nobody, naming the lock without the
    runs root's path in front of the cause (a reason is cut at a few hundred characters); patched to 0.3 s the refusal
    comes in under 3 s; an explicit `lock_timeout` wins over the module's; released, the same ask is admitted."""
    import fcntl
    res = _res()
    assert res.CAPACITY_LOCK_TIMEOUT_SECONDS == 5.0
    world.sessions(2)
    monkeypatch.setattr(res, "CAPACITY_LOCK_TIMEOUT_SECONDS", 0.3)
    held = os.open(world.root / "_capacity-reservations.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        gate = ask(world, ["a"], **owner(pid=1111))
        spent = time.monotonic() - started
        assert gate.admitted == () and "is held by another process" in gate.why and 0.25 < spent < 3
        assert str(world.root) not in gate.why and "<runs root>/_capacity-reservations.lock" in gate.why   # cause first
        monkeypatch.setattr(res, "CAPACITY_LOCK_TIMEOUT_SECONDS", 60.0)       # an explicit wait wins over the module's
        started = time.monotonic()
        gate = ask(world, ["a"], **owner(pid=1111), lock_timeout=0.3)
        assert gate.admitted == () and 0.25 < time.monotonic() - started < 3
    finally:
        os.close(held)
    assert ask(world, ["a"], **owner(pid=1111)).admitted == ("a",)


def test_a_reason_names_the_runs_root_by_role_so_the_cause_stays_in_view(world):
    """Catches a refusal whose cause is cut off behind a long path (a reason is cut at a few hundred characters): a
    ledger that is not JSON is named `<runs root>/_capacity-reservations.json` and the real path is not in the reason;
    a root that is not a path leaves the reason as it is."""
    world.sessions(2)
    (world.root / LEDGER).write_text("not json at all")
    gate = ask(world, ["a"], **owner(pid=1111))
    assert gate.admitted == () and str(world.root) not in gate.why and "<runs root>/_capacity-reservations.json" in gate.why
    assert _check()._shorten("x /a/b y", object()) == "x /a/b y" and _check()._shorten("x /a/b y", "/a/b") == "x <runs root> y"


# --- R4 bounded history -------------------------------------------------------------------------------------------------

def test_terminal_rows_are_rolled_to_a_versioned_file_and_the_open_holds_are_kept(world):
    """Catches a ledger that grows with the host's history (the lesson of the lease files): with 1025 terminal rows
    and one open hold, the next admission copies the terminal rows whole to `_capacity-reservations.<stamp>-<token>.json`
    (never replaced, never removed) and rewrites the ledger with the open hold and the new one only; a further ask over
    that ledger changes nothing; with exactly 1024 terminal rows nothing is rolled (the threshold is 1025, not 1026)."""
    res = _res()
    world.sessions(8)
    ask(world, ["open"], **owner(pid=1111))
    document = ledger(world.root)
    template = dict(document["holds"][0])
    rows = [dict(template, reservation_id=f"t{n}", run_id=f"r{n:04d}", status="SETTLED", ended_at=NOW, reason="done")
            for n in range(1024)]
    document["holds"] = rows + document["holds"]
    (world.root / LEDGER).write_text(json.dumps(document))
    ask(world, ["x"], **owner(pid=2222))
    assert len(ledger(world.root)["holds"]) == 1026 and not list(world.root.glob("_capacity-reservations.2*.json"))
    ask(world, ["y"], **owner(pid=2222))                                  # x is superseded: the 1025th terminal row
    archived = list(world.root.glob("_capacity-reservations.2*.json"))
    assert len(archived) == 1
    assert len(json.loads(archived[0].read_text())["holds"]) == 1025      # 1024 settled + x, superseded by the ask
    assert stat.S_IMODE(os.lstat(archived[0]).st_mode) == 0o600
    assert [(h["run_id"], h["status"]) for h in ledger(world.root)["holds"]] == [("open", "HELD"), ("y", "HELD")]
    settled = (world.root / LEDGER).read_bytes()
    world.registry(("machine", "host.sessions", 0, "sessions", "declared"))
    ask(world, ["z"], **owner(pid=5555))
    assert (world.root / LEDGER).read_bytes() == settled and len(list(world.root.glob("_capacity-reservations.2*.json"))) == 1
    assert res.CAPACITY_ARCHIVE_ROWS == 1024


# --- R5 what is counted, and what is not asked ---------------------------------------------------------------------

def test_a_lease_and_a_hold_are_both_counted_other_kinds_add_their_amount_and_the_command_sees_them(world, capsys):
    """Catches a command that cannot see a running wave's hold, a dropped capacity kind and a merged count: run `n1` has a
    live lease **and** a hold of another owner -> both count (the project-local name is not a key: over-counted, never
    under-counted); `n2` has a hold only, `n3` a lease only -> four sessions (`holds.sessions` 4, `holds.reserved` 2). A
    row of another kind (`machine/gpu.devices`, 2) adds its `Amount`, and a row of the budget's own key
    (`provider/dispatches.reserved`, 3) is added to the dispatches `Amount` -- one `Amount` per key. `hoh capacity
    check` (another process than the owner) reports exactly that and writes nothing."""
    from hoh import cli
    res = _res()
    world.sessions(8)
    world.lease("other", "n1")
    world.lease("other", "n3")
    ask(world, ["n1", "n2"], **owner(pid=1111))
    res.CapacityReservations(world.root, **owner(pid=2222)).reserve_atomically(
        lambda: [("g1", "machine", "gpu.devices", 2, "items"), ("d1", "provider", "dispatches.reserved", 3, "requests")])
    held = _check().live_holds(world.root)
    assert (held.sessions, held.reserved) == (4, 2)
    amounts = {(a.kind, a.name, a.unit): a.amount for a in held.amounts}
    assert len(held.amounts) == len(amounts) == 3
    assert amounts == {("machine", "host.sessions", "sessions"): 4, ("provider", "dispatches.reserved", "requests"): 3,
                       ("machine", "gpu.devices", "items"): 2}
    before = {p.name: p.read_bytes() for p in world.root.iterdir() if p.is_file()}
    code = cli.main(["--root", str(world.root), "capacity", "check", "machine", "host.sessions", "1", "sessions",
                     "--storage-root", str(world.state)])
    printed = json.loads(capsys.readouterr().out)
    assert code == 0 and printed["holds"]["sessions"] == 4 and printed["holds"]["reserved"] == 2
    assert {p.name: p.read_bytes() for p in world.root.iterdir() if p.is_file()} == before


def test_one_node_id_held_by_three_owners_is_three_sessions_and_equal_lease_names_are_two(world):
    """Catches review A's F1/F4: node ids and run names are project-local, so the same name in three projects is three
    holds and two leases of one run name in two projects are two sessions -- never one. Registry of 2 sessions, three
    live owners each ask for a node `t1`: the first two are admitted, the third waits ("2 of 2 sessions"); the ledger
    holds two rows and `live_holds` counts two sessions (not one). Two leases named `t1` in two projects are two."""
    world.sessions(2)
    first = ask(world, ["t1"], **owner(pid=1111))
    second = ask(world, ["t1"], **owner(pid=2222))
    third = ask(world, ["t1"], **owner(pid=3333))
    assert (first.admitted, second.admitted, third.admitted) == (("t1",), ("t1",), ())
    assert "2 of 2 sessions" in third.why
    held = _check().live_holds(world.root)
    assert (held.sessions, held.reserved) == (2, 2)
    assert [(h["run_id"], h["status"], h["owner_pid"]) for h in ledger(world.root)["holds"]] == [
        ("t1", "HELD", 1111), ("t1", "HELD", 2222)]
    world.lease("p1", "t1")
    world.lease("p2", "t1")
    assert _check().live_holds(world.root).sessions == 4        # two leases (equal names) and two holds


_CHILD = """
import json, sys, time
from pathlib import Path
from hoh import capacitycheck
from hoh.contracts import utcnow
root, state, node, go = sys.argv[1:5]
gate = capacitycheck.admit_wave([node], runs_root=root, state_root=state, now=utcnow)
print(json.dumps({"admitted": list(gate.admitted), "why": gate.why[:160]}), flush=True)
deadline = time.monotonic() + 40
while not Path(go).exists() and time.monotonic() < deadline:
    time.sleep(0.05)
"""


def test_real_processes_asking_at_once_are_admitted_up_to_the_limit_and_their_rows_end_with_them(world, tmp_path):
    """The race across real processes with the real facts (pid, boot id, start time, pid namespace; no injection): six
    processes ask one registry of 3 sessions for a node of the same name `t1` (node ids are project-local) and stay alive
    until released: exactly three are admitted, three wait; the ledger holds three `HELD` rows of three pids and the
    reader counts three sessions. When the processes have exited, the next ask finds all three rows `LOST` (the owner no
    longer exists), ends them in its own locked step and is admitted."""
    import subprocess
    import sys
    world.sessions(3)
    go = tmp_path / "go"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p), PYTHONDONTWRITEBYTECODE="1")
    children = [subprocess.Popen([sys.executable, "-B", "-c", _CHILD, str(world.root), str(world.state), "t1", str(go)],
                                 stdout=subprocess.PIPE, text=True, env=env) for _ in range(6)]
    try:
        answers = [json.loads(c.stdout.readline()) for c in children]
        assert sorted(len(a["admitted"]) for a in answers) == [0, 0, 0, 1, 1, 1], answers
        assert all("3 of 3 sessions" in a["why"] for a in answers if not a["admitted"]), answers
        rows = ledger(world.root)["holds"]
        assert [h["status"] for h in rows] == ["HELD"] * 3 and len({h["owner_pid"] for h in rows}) == 3
        held = _check().live_holds(world.root)
        assert (held.sessions, held.reserved) == (3, 3)
    finally:
        go.write_text("go")
        for c in children:
            c.wait(timeout=60)
    after = _check().admit_wave(["z"], runs_root=world.root, state_root=world.state, now=utc)
    assert after.admitted == ("z",) and after.ended == 3
    assert [h["status"] for h in ledger(world.root)["holds"]] == ["LOST"] * 3 + ["HELD"]


def test_a_caller_does_not_count_its_own_reservation_against_itself(world):
    """Catches a gate that blocks its own controller's next wave with the wave it just ran: with the caller's real
    identity, `live_holds` from the same thread counts none of the caller's rows (its earlier wave is over when it
    asks), while a reader in **another thread** (another owner: `hoh capacity check` is another process) counts them."""
    world.sessions(3)
    gate = _check().admit_wave(["a", "b"], runs_root=world.root, state_root=world.state, now=utc)
    assert gate.reserved == ("a", "b")
    assert _check().live_holds(world.root).reserved == 0
    seen: dict = {}
    reader = threading.Thread(target=lambda: seen.update(held=_check().live_holds(world.root)))
    reader.start()
    reader.join(30)
    assert seen["held"].reserved == 2 and seen["held"].sessions == 2


def test_nothing_is_reserved_without_a_registry_in_the_sequential_round_or_for_a_waiting_node(world):
    """Catches a hold taken where no capacity was judged: with no registry record the wave runs as before and no ledger or
    lock exists; with a registry, width 1 (LINEAR) reserves nothing; and the nodes that wait (n3 of three at limit 2) have
    no row."""
    first = ProjectController(world.store, world.launcher(barrier=threading.Barrier(2)), Gates(), width=2).run()
    assert first.halt is HaltClass.CLOSED, first.reason
    assert not (world.root / LEDGER).exists() and not (world.root / "_capacity-reservations.lock").exists()
    fresh = World(world.tmp / "linear")
    fresh.sessions(2)
    assert ProjectController(fresh.store, fresh.launcher(), Gates()).run().halt is HaltClass.CLOSED
    assert not (fresh.root / LEDGER).exists()
    narrow = World(world.tmp / "narrow")
    narrow.sessions(2)
    result = ProjectController(narrow.store, narrow.launcher(barrier=threading.Barrier(2)), Gates(), width=3).run()
    assert result.halt is HaltClass.CLOSED, result.reason
    assert {h["run_id"] for h in ledger(narrow.root)["holds"]} == {"n1", "n2"}


def test_hoh_project_drive_holds_the_admitted_nodes_of_each_wave_in_the_ledger(world, monkeypatch, capsys):
    """The production path (`cli.main` -> `hoh project drive` -> `ProjectController.run` -> `_wave_round` ->
    `capacitycheck.admit_wave` -> `CapacityReservations.reserve_atomically`): with room for one session and three nodes at
    `HOH_WAVE_WIDTH=3` each wave's admitted node is a row of the ledger, written by the drive's own process and
    thread, `SETTLED` when its wave ended (C4b; before it, `SUPERSEDED` by the owner's next ask), and the project closes."""
    import test_wave_capacity as harness
    world.sessions(1)
    rc, out = harness._drive(world, monkeypatch, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    rows = ledger(world.root)["holds"]
    assert [(h["run_id"], h["status"]) for h in rows] == [("n1", "SETTLED"), ("n2", "SETTLED")]
    assert {h["owner_pid"] for h in rows} == {os.getpid()}
    assert stat.S_IMODE(os.lstat(world.root / LEDGER).st_mode) == 0o600


def test_a_drive_over_an_unusable_ledger_halts_capacity_wait_visibly_and_leaves_the_ledger_alone(world, monkeypatch, capsys):
    """The terminal state of a refusal is visible: with a ledger that is not JSON, `hoh project drive` prints the halt
    `CAPACITY_WAIT` and a reason that names `ReservationError`, exits 1, launches nothing, leaves every node READY,
    and the ledger's bytes are what they were (a refusal writes nothing and never repairs a file it did not write)."""
    import test_wave_capacity as harness
    world.sessions(3)
    (world.root / LEDGER).write_bytes(b"{not json")
    rc, out = harness._drive(world, monkeypatch, capsys)
    assert rc == 1 and out["halt"] == "CAPACITY_WAIT" and "ReservationError" in out["reason"], out
    assert harness.CONFIG["launcher"].calls == []
    assert {n.lifecycle for n in world.store.read_state().nodes} == {Lifecycle.READY}
    assert (world.root / LEDGER).read_bytes() == b"{not json"


def test_a_ledger_write_or_an_archive_that_fails_admits_nobody_and_loses_no_row(world, monkeypatch):
    """Catches a hold that is granted but not recorded, and a roll that drops rows it did not copy: (1) the ledger write
    fails (no space) -> nobody is admitted, no ledger exists, the next ask is admitted; (2) with 1025 terminal rows the
    archive copy fails -> nobody is admitted and the ledger is byte for byte what it was; (3) every archive name is
    taken -> exactly five attempts, then a refusal, the ledger and the file that was there untouched; the next ask
    archives once and keeps every row (in the ledger or in the archive)."""
    res = _res()
    world.sessions(8)

    def full(*_a, **_k):
        raise OSError(28, "No space left on device")
    with monkeypatch.context() as patch:
        patch.setattr(res.durable_io, "durable_write", full)
        gate = ask(world, ["a"], **owner(pid=1111))
    assert gate.configured and gate.admitted == () and "could not be written" in gate.why and not (world.root / LEDGER).exists()
    assert ask(world, ["a"], **owner(pid=1111)).admitted == ("a",)
    document = ledger(world.root)
    template = dict(document["holds"][0])
    document["holds"] += [dict(template, reservation_id=f"t{n}", run_id=f"r{n:04d}", status="SETTLED", ended_at=NOW,
                               reason="done") for n in range(1025)]
    (world.root / LEDGER).write_text(json.dumps(document))
    before = (world.root / LEDGER).read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(res.durable_io, "durable_create", full)
        gate = ask(world, ["b"], **owner(pid=2222))
    assert gate.admitted == () and "could not be archived" in gate.why
    assert (world.root / LEDGER).read_bytes() == before and not list(world.root.glob("_capacity-reservations.2*.json"))
    taken = world.root / f"_capacity-reservations.{NOW.replace(':', '').replace('-', '')}-ababab.json"
    taken.write_text("{}")
    draws: list[int] = []

    def same_token(n: int) -> str:
        draws.append(n)
        return "ab" * n
    with monkeypatch.context() as patch:                      # every archive name is taken: five attempts, then a refusal
        patch.setattr(res.secrets, "token_hex", same_token)
        gate = ask(world, ["b"], **owner(pid=2222))
    assert gate.admitted == () and "no free archive name" in gate.why and draws.count(3) == 5, (gate.why, draws)
    assert (world.root / LEDGER).read_bytes() == before and taken.read_text() == "{}"
    assert ask(world, ["b"], **owner(pid=2222)).admitted == ("b",)
    archived = [p for p in world.root.glob("_capacity-reservations.2*.json") if p != taken]
    assert len(archived) == 1
    kept = {h["reservation_id"] for h in ledger(world.root)["holds"]}
    rolled = {h["reservation_id"] for h in json.loads(archived[0].read_text())["holds"]}
    assert rolled == {f"t{n}" for n in range(1025)} and not rolled & kept
    assert [(h["run_id"], h["status"]) for h in ledger(world.root)["holds"]] == [("a", "HELD"), ("b", "HELD")]


# --- R6 the module's contract ----------------------------------------------------------------------------------------

def test_the_ledger_vocabulary_and_bounds_are_what_the_spec_says(world):
    """Catches a moved bound or a renamed status: the file names, the schema, the ttl (a day), the lock wait (5 s), the
    archive threshold (1024 rows), the statuses, and a `CapacityHold` that refuses an amount below one, a run id the runs
    root keeps for itself and an extra key."""
    res = _res()
    assert (res.CAPACITY_LEDGER_NAME, res.CAPACITY_LOCK_NAME) == (LEDGER, "_capacity-reservations.lock")
    assert (res.CAPACITY_SCHEMA, res.CAPACITY_TTL_SECONDS, res.CAPACITY_LOCK_TIMEOUT_SECONDS,
            res.CAPACITY_ARCHIVE_ROWS) == ("hoh-capacity-reservations/1", 86400, 5.0, 1024)
    assert [s.value for s in res.CapacityStatus] == ["HELD", "SUPERSEDED", "LOST", "EXPIRED", "SETTLED"]
    base = dict(reservation_id="x", run_id="a", kind="machine", name="host.sessions", amount=1, unit="sessions",
                owner_pid=1, owner_thread=0, owner_boot="", owner_host="h", owner_pidns="", owner_start="",
                reserved_at=NOW, expires_at=NOW)
    res.CapacityHold(**base)
    for bad in (dict(base, amount=0), dict(base, extra=1), dict(base, owner_pid=0)):
        with pytest.raises(Exception):
            res.CapacityHold(**bad)
    with pytest.raises(res.ReservationError):
        res.CapacityReservations(world.root, **owner()).settle("projects", "x")
    # a run id that is not one (NUL, a separator, `..`, empty, non-ASCII, a lone surrogate, a leading space, a trailing
    # newline, 300 characters, the names the runs root keeps for itself) is refused when a hold is made: nothing written
    for bad_id in ("a\x00b", "a/b", "..", "", "\u00e9", "a\ud800", " a", "a\n", "x" * 300, "projects", "backups"):
        with pytest.raises(res.ReservationError):
            res.CapacityReservations(world.root, **owner()).reserve_atomically(
                lambda bad_id=bad_id: [(bad_id, "machine", "host.sessions", 1, "sessions")])
        assert not (world.root / LEDGER).exists()
    # an injected ttl is the ttl of the rows; zero is refused
    short = res.CapacityReservations(world.root, **owner(), ttl_seconds=60)
    created, _ = short.reserve_atomically(lambda: [("ttl1", "machine", "host.sessions", 1, "sessions")])
    assert [h.expires_at for h in created] == [plus(NOW, 60)]
    with pytest.raises(res.ReservationError):
        res.CapacityReservations(world.root, **owner(), ttl_seconds=0)
    # a row that has ended is judged as what it is, whoever asks and whatever its owner's process is doing
    obs = res.CapacityReservations(world.root, **owner())
    row = res.CapacityHold(**dict(base, owner_host="host-1", owner_pidns="ns-1", owner_pid=4242, owner_thread=7,
                                  owner_start="",
                                  expires_at=plus(NOW, 99), status="SETTLED", reason="done", ended_at=NOW))
    assert obs.judge(row, NOW) == (res.CapacityStatus.SETTLED, "done")
    assert obs.judge(row.model_copy(update={"status": res.CapacityStatus.HELD}), NOW)[0] is res.CapacityStatus.HELD


def test_an_oversize_ledger_is_refused_and_not_read(world, monkeypatch):
    """Catches a ledger read without its size bound: a ledger above `MAX_CAPACITY_BYTES` is `ReservationError`, and the
    gate over it admits nobody."""
    res = _res()
    world.sessions(2)
    ask(world, ["a"], **owner(pid=1111))
    monkeypatch.setattr(res, "MAX_CAPACITY_BYTES", 100)
    with pytest.raises(res.ReservationError, match="bytes, above the 100"):
        res.CapacityReservations(world.root, **owner()).read()
    assert ask(world, ["b"], **owner(pid=2222)).admitted == ()


def test_a_reused_pid_and_a_restarted_machine_end_the_hold_and_a_raising_decision_keeps_what_it_ended(world):
    """Catches an owner judged by its pid number alone: (1) the pid of a hold is alive but now another process (another
    start time) -> `LOST`; (2) the machine's boot id changed -> `LOST` although the pid is alive; (3) `decide` raising
    after a hold was ended leaves the ended row persisted and nothing held."""
    res = _res()
    world.sessions(6)
    ask(world, ["a"], **owner(pid=1111, boot=lambda: "boot-1"))
    ask(world, ["b"], **owner(pid=1112, boot=lambda: "boot-1"))
    ask(world, ["c"], **owner(pid=1113, boot=lambda: "boot-1"))
    same = res.CapacityReservations(world.root, **owner(pid=3333, boot=lambda: "boot-1"))
    assert {h.run_id for h in same.live()} == {"a", "b", "c"}
    reused = res.CapacityReservations(world.root, **owner(pid=3333, boot=lambda: "boot-1",
                                                          start=lambda p: "start-other" if p == 1111 else f"start-{p}"))
    assert {h.run_id for h in reused.live()} == {"b", "c"}
    rebooted = res.CapacityReservations(world.root, **owner(pid=3333, boot=lambda: "boot-2"))
    assert rebooted.live() == []

    def boom():
        raise RuntimeError("the decision failed")
    with pytest.raises(RuntimeError, match="the decision failed"):
        reused.reserve_atomically(boom)
    assert statuses(world.root) == {"a": "LOST", "b": "HELD", "c": "HELD"}
    lost = [h for h in ledger(world.root)["holds"] if h["run_id"] == "a"][0]
    assert "another process" in lost["reason"] and lost["ended_at"] == NOW
    # an owner on another host or in another pid namespace cannot be judged by a local pid: only the ttl ends its hold
    ask(world, ["f"], **owner(pid=1119, host="host-2"))
    ask(world, ["g"], **owner(pid=1120, pidns=lambda: "ns-2"))
    gone_locally = lambda p: p not in (1119, 1120)                          # noqa: E731 - "no such pid" on this host
    observer = res.CapacityReservations(world.root, **owner(pid=3333, alive=gone_locally))
    assert {"f", "g"} <= {h.run_id for h in observer.live()}
    late = res.CapacityReservations(world.root, **owner(pid=3333, alive=gone_locally, clock=lambda: plus(NOW, 86400)))
    assert not {"f", "g"} & {h.run_id for h in late.live()}
