"""P3-22 / P3-24, slice 3: a wave has an outcome record; what a dead controller left is recovered.

Every recovery test drives `hoh project drive` (cli.main) with a project `driver.json`
whose factory builds the real launcher over a fake process boundary (the harness of
`tests/test_wave_controller.py` and `tests/test_wave_settlement_wired.py`), so the
recovery runs where production runs it: `cli.py` -> `drive_governed` ->
`ProjectController.run` -> `_drive`. Hermetic: everything is under `tmp_path`; no agent
is started. A "dead controller" is a drive that is interrupted between the wave record
and RUNNING with the launcher's give-back made inert -- the process is gone before it
can end anything."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

import pytest

from test_wave_controller import Gates, _held_ids, _hold_process_lock, _let_go, _nodes, _world
from test_wave_settlement_wired import Launcher
from hoh import cli, durable_io

try:                                  # the module is this slice's: on the base the tests still
    from hoh import waverecovery      # run, and fail on the behaviour that is missing
except ImportError:                   # pragma: no cover - the base
    waverecovery = None
from hoh.orchestrator import HaltClass, ProjectController
from hoh import orchestrator as orch_module
from hoh.project import Lifecycle
from hoh.reservation import BudgetReservations
from hoh.store import StaleWrite
from hoh.succession import StaleMaster
from hoh.wave import WaveError, read_record
from hoh.wslease import WorkspaceLease

CONFIG: dict = {}
HERE = Path(__file__).resolve().parent
MAX = 3
OUTCOME_DIR = "wave-outcomes"


class Mortal(Launcher):
    """`dying`: the process is gone before it can give anything back -- the launcher's
    release is inert. `"record"`: the controller is interrupted inside `on_admitted`, after
    the wave's record and before RUNNING is persisted; `"lease"`: inside the executor's
    admission, after the holds were adopted and **before** the wave record exists."""

    def __init__(self, *a, dying=False, **kw):
        super().__init__(*a, **kw)
        if dying:
            self.release_prepared_hold = lambda node, reason: False

            def interrupt(*args, **kwargs):
                raise KeyboardInterrupt()

            if dying == "lease":
                self.lease_for = interrupt
            elif dying == "record":
                self.accepted_baseline = interrupt

    def evaluate(self, node, *args, **kwargs):
        if CONFIG.get("in_recovery") is not None:         # what the recovery probes (and writes)
            CONFIG["in_recovery"].append(node.id)
        if CONFIG.get("evaluate_raises"):                 # a crash inside the recovery's probe
            raise KeyboardInterrupt()
        return super().evaluate(node, *args, **kwargs)


def build_driver(store):
    cfg = CONFIG
    launcher = Mortal(cfg["root"], cfg["repo"], cfg["script"], dying=cfg.get("dying", False),
                      leak=cfg.get("leak", ()), dispatch_budget=cfg.get("budget", 6),
                      dispatch_reservation=cfg.get("share", 3), wave_width=cfg.get("width", 2))
    for key, hook in cfg.get("hooks", {}).items():
        launcher.hooks[key] = hook
    cfg["launcher"] = launcher
    return launcher, Gates()


def world(tmp_path, names=("n1", "n2"), **cfg):
    repo, root, store = _world(tmp_path, names=names)
    CONFIG.clear()
    CONFIG.update(root=root, repo=repo, store=store, script={n: "ok" for n in names}, **cfg)
    (store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_wave_recovery:build_driver", "path": str(HERE)}))
    return repo, root, store


def drive(root, capsys, session="sA", **cfg):
    CONFIG.update(cfg)
    rc = cli.main(["--root", str(root), "project", "drive", "p", "--session", session])
    seen = capsys.readouterr()
    assert seen.out.strip(), (rc, seen.err[-600:])
    return rc, json.loads(seen.out)


def interrupted(root, capsys, session="sA"):
    """A drive that ends in a `KeyboardInterrupt` (`cli.main` prints "aborted" and returns)."""
    rc = cli.main(["--root", str(root), "project", "drive", "p", "--session", session])
    seen = capsys.readouterr()
    assert rc != 0 and "aborted" in seen.err, (rc, seen.err, seen.out[-300:])


def kill_the_master(store):
    """The interrupted drive's process is gone: its lease names a pid that no longer exists
    and has run out, so a session may take the project over (as after a real crash)."""
    lease = store.dir / "lease.json"
    assert lease.is_file(), sorted(p.name for p in store.dir.iterdir())
    edit(lease, lambda d: d.update(holder_pid=2**22 - 3, expires_at="2020-01-01T00:00:00Z"))


def crash(root, capsys, where="record"):
    """The first drive dies after the record and before RUNNING (`"lease"`: before the
    record). Returns the wave id of the outcome it left."""
    CONFIG["dying"] = where
    interrupted(root, capsys)
    CONFIG["dying"] = False
    kill_the_master(CONFIG["store"])
    found = [o.wave_id for o in outcomes(CONFIG["store"]).values()]
    CONFIG["crashed"] = found[0] if found else None
    return CONFIG["crashed"]


def outcomes(store):
    d = store.dir / OUTCOME_DIR
    if not d.is_dir():
        return {}
    return {p.name: waverecovery.read_outcome(p) for p in sorted(d.iterdir())}


def the_outcome(store, wave_id=None):
    wave_id = wave_id or CONFIG["crashed"]
    assert wave_id, "the interrupted drive left no wave outcome"
    path = store.dir / OUTCOME_DIR / f"wave-{wave_id}.json"
    return path, waverecovery.read_outcome(path)


def snapshot(path):
    """What a rewrite cannot hide: the bytes, the inode and the mtime in nanoseconds."""
    st = os.stat(path)
    return path.read_bytes(), st.st_ino, st.st_mtime_ns


def recovery_steps(out):
    """Every `WAVE_*` step but the degradation note: what a quiet drive must not emit."""
    return [s for s in out["steps"] if s.startswith("WAVE_") and not s.startswith("WAVE_DEGRADED")]


@contextlib.contextmanager
def state_writes_in_recovery():
    """Counts, *inside* `_recover_open_waves` only, the writes of the project state (the claim)
    and the runs the launcher is asked to evaluate (the probe). Yields `(writes, probed)`."""
    store_class = type(CONFIG["store"])
    seen = []
    CONFIG["in_recovery"] = None
    real_write = store_class.write_state
    real_recover = ProjectController._recover_open_waves

    def counting_write(self, *a, **k):
        if inside:
            seen.append(1)
        return real_write(self, *a, **k)

    probed: list = []

    def spy(self, steps_):
        inside.append(1)
        CONFIG["in_recovery"] = probed
        try:
            return real_recover(self, steps_)
        finally:
            inside.clear()
            CONFIG["in_recovery"] = None

    inside: list = []
    store_class.write_state = counting_write
    ProjectController._recover_open_waves = spy
    try:
        yield seen, probed
    finally:
        CONFIG["in_recovery"] = None
        store_class.write_state = real_write
        ProjectController._recover_open_waves = real_recover


def edit(path, fn):
    doc = json.loads(path.read_bytes())
    fn(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")


# --------------------------------------------------------------------------- #
# the recovery
# --------------------------------------------------------------------------- #

def test_a_hold_a_controller_left_before_running_is_ended_at_the_next_drive_and_the_project_closes(
        tmp_path, capsys):
    """Catches the wedge of P3-02s2rf's limit L1: a controller dies between the wave record
    and RUNNING, both holds stay HELD, and every later width-2 drive refuses the runs
    ("a hold this launcher did not take") for good -- on the base the second drive halts
    BUDGET_EXHAUSTED. With the recovery the next drive ends exactly those two holds by id,
    says so (`WAVE_RECOVERED:<wave id>`), and the project closes in that same drive."""
    repo, root, store = world(tmp_path)
    crash(root, capsys)
    assert _held_ids(root) == ["n1", "n2"]
    assert {n.lifecycle for n in _nodes(store).values()} == {Lifecycle.READY}
    rc, out = drive(root, capsys)
    assert out["halt"] == "CLOSED", out
    path, outcome = the_outcome(store)
    assert f"WAVE_RECOVERED:{outcome.wave_id}" in out["steps"]
    assert outcome.phase is waverecovery.OutcomePhase.RECOVERED and outcome.attempts == 1
    assert sorted(h.run_id for h in outcome.holds) == ["n1", "n2"]
    [crashed] = [p for p in (store.dir / "waves").iterdir() if p.name == f"wave-{outcome.wave_id}.json"]
    assert read_record(crashed).phase.value == "ADMITTED"          # the evidence is kept, untouched
    rows = BudgetReservations(root).read()
    assert _held_ids(root) == []
    recovered = [r for r in rows if r.reason.startswith(f"wave {outcome.wave_id}: recovered")]
    assert sorted(r.run_id for r in recovered) == ["n1", "n2"]
    assert {n.lifecycle for n in _nodes(store).values()} == {Lifecycle.MERGED}
    # RECOVERED is terminal: a further drive does not look at it again (bytes, inode and mtime
    # unchanged, no recovery step, no write of the project state by the recovery)
    fixed = snapshot(path)
    with state_writes_in_recovery() as (writes, probed):
        rc, again = drive(root, capsys)
    assert snapshot(path) == fixed and not recovery_steps(again), again["steps"]
    assert writes == [] and probed == []


def test_a_successor_master_recovers_what_its_predecessor_left(tmp_path, capsys):
    """Catches a recovery bound to the *same* session: the predecessor (epoch 1) died; the
    successor session takes the project over at epoch 2 and recovers the holds."""
    repo, root, store = world(tmp_path)
    crash(root, capsys)
    rc, out = drive(root, capsys, session="sB")
    assert out["halt"] == "CLOSED", out
    path, outcome = the_outcome(store)
    assert outcome.epoch == 1 and outcome.phase is waverecovery.OutcomePhase.RECOVERED
    assert f"WAVE_RECOVERED:{outcome.wave_id}" in out["steps"]


def test_a_wave_whose_nodes_reached_running_is_left_to_the_resume_path(
        tmp_path, capsys, monkeypatch):
    """Catches the recovery ending a hold under a node that is no longer READY: the controller
    is interrupted at the authority check that follows the persist of RUNNING, before any
    launch. The outcome is NOT_APPLICABLE (quiet: no recovery step), the holds are left to
    the resume path (the nodes are RUNNING: the recovery does not even probe their runs, so
    it cannot end a hold the resume path reads) and the outcome is then never looked at again."""
    repo, root, store = world(tmp_path)
    original = ProjectController._authorise

    def interrupt_at_dispatch(self, act):
        if act.startswith("dispatch of wave"):
            raise KeyboardInterrupt()
        return original(self, act)

    monkeypatch.setattr(ProjectController, "_authorise", interrupt_at_dispatch)
    CONFIG["dying"] = "gone"            # the give-back is inert: the process is gone
    interrupted(root, capsys)
    CONFIG["dying"] = False
    monkeypatch.setattr(ProjectController, "_authorise", original)
    kill_the_master(store)
    found = [o.wave_id for o in outcomes(store).values()]
    CONFIG["crashed"] = found[0] if found else None
    assert {n.lifecycle for n in _nodes(store).values()} == {Lifecycle.RUNNING}
    assert _held_ids(root) == ["n1", "n2"]
    path, before = the_outcome(store)
    assert before.phase is waverecovery.OutcomePhase.OPEN
    with state_writes_in_recovery() as (writes, probed):
        rc, out = drive(root, capsys)
    path, after = the_outcome(store)
    assert after.phase is waverecovery.OutcomePhase.NOT_APPLICABLE and after.attempts == 1
    assert probed == []                                  # a RUNNING node is read by the resume path, never probed here
    assert not [s for s in out["steps"] if s.startswith("WAVE_RECOVER")], out["steps"]
    fixed = snapshot(path)
    rc, again = drive(root, capsys)
    assert snapshot(path) == fixed and not recovery_steps(again)     # terminal: not looked at again


def test_a_hold_left_before_the_wave_record_existed_is_recovered_too(tmp_path, capsys):
    """Catches a recovery that needs the executor's record: the controller dies in the
    executor's admission, after the holds were adopted and before the record is written
    (only the outcome, written before the executor, names the holds). The next drive
    recovers them from it alone."""
    repo, root, store = world(tmp_path)
    wave_id = crash(root, capsys, where="lease")
    assert not (store.dir / "waves" / f"wave-{wave_id}.json").exists()
    assert _held_ids(root) == ["n1", "n2"]
    rc, out = drive(root, capsys)
    assert out["halt"] == "CLOSED", out
    path, outcome = the_outcome(store)
    assert outcome.phase is waverecovery.OutcomePhase.RECOVERED
    assert f"WAVE_RECOVERED:{wave_id}" in out["steps"]


def test_a_settled_wave_whose_outcome_was_never_closed_gets_its_leak_ended(tmp_path, capsys, monkeypatch):
    """Catches the crash between the end of the wave and the write of its outcome: the
    record says SETTLED, n2's launch could not end its hold, and the outcome is still OPEN.
    The next drive reads the SETTLED record back through `settle_wave` (slice 2's second
    chance, IN_FLIGHT rows skipped) and closes the outcome."""
    assert waverecovery is not None, "hoh.waverecovery does not exist on this tree"
    repo, root, store = world(tmp_path, leak=("n2",))
    real = waverecovery.close_outcome

    def interrupt_close(path, record, **kw):
        raise KeyboardInterrupt()

    monkeypatch.setattr(waverecovery, "close_outcome", interrupt_close)
    interrupted(root, capsys)
    kill_the_master(store)
    found = [o.wave_id for o in outcomes(store).values()]
    CONFIG["crashed"] = found[0] if found else None
    monkeypatch.setattr(waverecovery, "close_outcome", real)
    path, outcome = the_outcome(store)
    assert outcome.phase is waverecovery.OutcomePhase.OPEN
    assert "n2" in _held_ids(root)                                   # the leak
    CONFIG["leak"] = ()
    rc, out = drive(root, capsys)
    path, outcome = the_outcome(store)
    assert outcome.phase is waverecovery.OutcomePhase.CLOSED and "SETTLED" in outcome.note
    assert f"WAVE_SETTLED:{outcome.wave_id}" in out["steps"]
    assert "n2" not in _held_ids(root)


# --------------------------------------------------------------------------- #
# the preconditions: each refusal is its own node, and each leaves the hold alone
# --------------------------------------------------------------------------- #

def _left(root, store, capsys, **drive_kw):
    rc, out = drive(root, capsys, **drive_kw)
    path, outcome = the_outcome(store)
    return out, path, outcome


def test_a_run_another_process_executes_is_never_recovered_and_the_outcome_is_abandoned(
        tmp_path, capsys):
    """Catches a recovery that supersedes a hold of a run that may execute, and a recovery
    that retries for ever. n1's process lock is held (a live process): three drives each
    leave BOTH holds HELD (all or nothing) and spend one attempt; the third attempt is
    the last; the next drive ABANDONS the outcome -- a terminal state, reported once and
    never acted on -- and every drive after that is quiet and changes no byte, inode or mtime."""
    repo, root, store = world(tmp_path)
    crash(root, capsys)
    fd = _hold_process_lock(root, "n1")
    try:
        for attempt in range(1, MAX + 1):
            out, path, outcome = _left(root, store, capsys)
            assert _held_ids(root) == ["n1", "n2"], attempt
            assert outcome.phase is waverecovery.OutcomePhase.OPEN and outcome.attempts == attempt
            assert f"WAVE_RECOVERY_LEFT:{outcome.wave_id}" in out["steps"]
        out, path, outcome = _left(root, store, capsys)
        assert outcome.phase is waverecovery.OutcomePhase.ABANDONED and outcome.attempts == MAX
        assert f"WAVE_RECOVERY_ABANDONED:{outcome.wave_id}" in out["steps"]    # reported once ...
        fixed = snapshot(path)
        for _ in range(2):
            out, path, again = _left(root, store, capsys)
            assert not [s for s in out["steps"] if s.startswith("WAVE_RECOVERY")], out["steps"]   # ... then quiet
            assert snapshot(path) == fixed                          # terminal: nothing is rewritten
        assert _held_ids(root) == ["n1", "n2"]                      # and never ended
    finally:
        _let_go(fd)


class StubAuthority:
    """An authority that passes the first `refuse_after` checks (the start of `run`) and
    refuses the next ones; `refuse_after=None` never refuses."""

    def __init__(self, epoch, refuse_after=1):
        self.epoch, self.session_id, self.calls, self.refuse_after = epoch, "stub", 0, refuse_after

    def check(self):
        self.calls += 1
        if self.refuse_after is not None and self.calls > self.refuse_after:
            raise StaleMaster("this session was superseded")

    def keepalive(self):
        from contextlib import nullcontext
        return nullcontext()

    def stamp(self, state):
        return state

    def renew(self):
        return None


@pytest.mark.parametrize("what", ["well_formed_outcome", "malformed_outcome"])
def test_a_session_that_is_no_longer_the_master_recovers_nothing(tmp_path, capsys, what):
    """Catches a recovery that acts before it authorises (precondition 1): the controller
    passes the check at the start of `run`, is superseded, and is refused at the recovery's
    own check -- the ledger and the outcome are byte for byte what the crash left. With a
    **malformed** outcome the same holds for the set-aside: a superseded session renames and
    replaces nothing (no `.quarantined` file, the bytes are the garbage they were)."""
    repo, root, store = world(tmp_path)
    crash(root, capsys)
    path, _ = the_outcome(store)
    if what == "malformed_outcome":
        path.write_bytes(b"{this is not an outcome")
    before = (path.read_bytes(), (root / "_budget-reservations.json").read_bytes())
    names = sorted(p.name for p in path.parent.iterdir())
    launcher, gates = build_driver(store)
    result = ProjectController(store, launcher, gates, authority=StubAuthority(1), width=2).run()
    assert result.halt is HaltClass.SUPERSEDED, result.reason
    assert (path.read_bytes(), (root / "_budget-reservations.json").read_bytes()) == before
    assert sorted(p.name for p in path.parent.iterdir()) == names


def test_a_claim_that_loses_the_race_ends_nothing(tmp_path, capsys, monkeypatch):
    """Catches a recovery that ends holds without the compare-and-swap of precondition 3:
    the persisted state advanced between its read and its write (`StaleWrite`), so the
    holds are left HELD, an attempt is spent, and the outcome stays OPEN."""
    repo, root, store = world(tmp_path)
    crash(root, capsys)
    real = type(store).write_state
    calls = []

    def racing(self, state, *a, **k):
        calls.append(1)
        raise StaleWrite("another writer advanced the state")

    monkeypatch.setattr(type(store), "write_state", racing)
    launcher, gates = build_driver(store)
    result = ProjectController(store, launcher, gates, authority=StubAuthority(1, None), width=2).run()
    monkeypatch.setattr(type(store), "write_state", real)
    path, outcome = the_outcome(store)
    assert calls and outcome.phase is waverecovery.OutcomePhase.OPEN and outcome.attempts == 1
    assert "advanced" in outcome.note
    assert _held_ids(root) == ["n1", "n2"]
    assert result.halt is not HaltClass.CLOSED


@pytest.mark.parametrize("how", ["newer_epoch", "unfenced_owner"])
def test_an_outcome_of_another_owner_is_not_recovered(tmp_path, capsys, how):
    """Catches the owner binding (precondition 2) missing: a wave that claims to have run
    under a **newer** epoch than this master's, or under no epoch at all (an unfenced
    controller), is left alone and an attempt is spent."""
    repo, root, store = world(tmp_path)
    crash(root, capsys)
    path, _ = the_outcome(store)
    edit(path, lambda d: d.update(epoch=99 if how == "newer_epoch" else None))
    out, path, outcome = _left(root, store, capsys)
    assert outcome.phase is waverecovery.OutcomePhase.OPEN and outcome.attempts == 1
    assert "this master is epoch 2" in outcome.note
    assert _held_ids(root) == ["n1", "n2"]


def _the_owner_is(path, pid, pid_start):
    edit(path, lambda d: d.update(pid=pid, pid_start=pid_start))


@pytest.mark.parametrize("how", ["live_owner_ends", "live_owner_never_ends",
                                 "owner_identity_unreadable", "unrelated_live_pid"])
def test_a_live_owner_is_waited_for_within_a_bound_and_a_reused_pid_is_not_the_owner(
        tmp_path, capsys, how):
    """Catches a wait that never ends (a reused or unrelated live pid keeping the outcome OPEN
    for ever) and a pid taken as proof. The outcome names the pid of this test's parent
    process (alive, not this process).
    `live_owner_ends`: its identity (boot id + start time) matches: the drive waits -- no
    attempt, `waits` 1, the holds HELD -- and once the process is gone the next drive recovers.
    `live_owner_never_ends` / `owner_identity_unreadable` (no recorded identity: the pid alone
    cannot be told from a reused one): three drives wait (`waits` 1, 2, 3, attempts 0), the
    fourth ABANDONS the outcome naming the owner it could not confirm dead, **once**; the
    holds stay HELD and every later drive is quiet and changes no byte.
    `unrelated_live_pid`: the recorded identity is another process's -- the pid was reused:
    the owner is dead for this purpose and the next drive recovers at once."""
    repo, root, store = world(tmp_path)
    crash(root, capsys)
    path, _ = the_outcome(store)
    ppid = os.getppid()
    identity = waverecovery.process_identity(ppid)
    assert identity, "this platform cannot read a process identity from /proc"
    _the_owner_is(path, ppid, {"live_owner_ends": identity, "live_owner_never_ends": identity,
                               "owner_identity_unreadable": None,
                               "unrelated_live_pid": "00000000-0000-0000-0000-000000000000:1"}[how])
    if how == "unrelated_live_pid":
        rc, out = drive(root, capsys)
        path, outcome = the_outcome(store)
        assert outcome.phase is waverecovery.OutcomePhase.RECOVERED and outcome.attempts == 1
        assert outcome.waits == 0 and out["halt"] == "CLOSED", out
        return
    if how == "live_owner_ends":
        rc, out = drive(root, capsys)
        path, outcome = the_outcome(store)
        assert (outcome.phase, outcome.attempts, outcome.waits) == (waverecovery.OutcomePhase.OPEN, 0, 1)
        assert any(s.startswith("WAVE_RECOVERY_LEFT:") for s in out["steps"]) and _held_ids(root) == ["n1", "n2"]
        kill_the_master(store)
        _the_owner_is(path, 2**22 - 5, identity)                    # the process is gone
        rc, out = drive(root, capsys)
        path, outcome = the_outcome(store)
        assert outcome.phase is waverecovery.OutcomePhase.RECOVERED and outcome.attempts == 1
        assert out["halt"] == "CLOSED", out
        return
    for k in range(1, waverecovery.MAX_WAITS + 1):
        rc, out = drive(root, capsys)
        path, outcome = the_outcome(store)
        assert (outcome.phase, outcome.attempts, outcome.waits) == (waverecovery.OutcomePhase.OPEN, 0, k), k
        assert any(s.startswith("WAVE_RECOVERY_LEFT:") for s in out["steps"])
        assert _held_ids(root) == ["n1", "n2"]
        kill_the_master(store)
    rc, out = drive(root, capsys)                                   # the bound: a terminal, named state
    path, outcome = the_outcome(store)
    assert outcome.phase is waverecovery.OutcomePhase.ABANDONED and outcome.waits == waverecovery.MAX_WAITS
    assert str(ppid) in outcome.note and "could not be confirmed dead" in outcome.note, outcome.note
    assert f"WAVE_RECOVERY_ABANDONED:{outcome.wave_id}" in out["steps"]
    assert _held_ids(root) == ["n1", "n2"]
    fixed = snapshot(path)
    for _ in range(2):
        kill_the_master(store)
        rc, again = drive(root, capsys)
        assert not [s for s in again["steps"] if s.startswith("WAVE_RECOVERY")], again["steps"]
        assert snapshot(path) == fixed


@pytest.mark.parametrize("what", ["unknown_id", "other_amount", "other_run", "bad_record", "outcome_lacks_a_hold"])
def test_a_hold_the_ledger_does_not_know_as_the_outcome_says_is_refused_whole(
        tmp_path, capsys, what):
    """Catches `settle_holds` being asked for a hold under the wrong name: the outcome names a
    reservation id the ledger lacks, or an amount or a run other than the ledger's, or the
    wave record names a hold the outcome lacks, or the record is not readable. Neither hold
    is ended and an attempt is spent."""
    repo, root, store = world(tmp_path)
    crash(root, capsys, where="lease" if what == "unknown_id" else "record")
    path, _ = the_outcome(store)
    if what == "bad_record":
        # the executor's record of the wave is not what it wrote: it is not trusted, not skipped
        (store.dir / "waves" / f"wave-{CONFIG['crashed']}.json").write_text("{not json", encoding="utf-8")
        expected = "wave record is not readable"
    elif what == "outcome_lacks_a_hold":
        # the executor's record names a hold the outcome does not: the outcome is not the
        # whole story, so nothing is ended on its word (precondition 4 of the record check)
        edit(path, lambda d: d.update(holds=d["holds"][:1]))
        expected = "names a hold the outcome does not"
    else:
        edits = {"unknown_id": lambda d: d["holds"][0].update(reservation_id="ffffffff-n1"),
                 "other_amount": lambda d: d["holds"][0].update(amount=2),
                 "other_run": lambda d: d["holds"][0].update(run_id="n2x")}
        edit(path, edits[what])
        expected = "could not be ended"
    ledger = (root / "_budget-reservations.json").read_bytes()
    out, path, outcome = _left(root, store, capsys)
    assert outcome.phase is waverecovery.OutcomePhase.OPEN and outcome.attempts == 1
    assert expected in outcome.note, outcome.note
    assert _held_ids(root) == ["n1", "n2"]
    assert (root / "_budget-reservations.json").read_bytes() == ledger


def test_at_most_sixteen_open_outcomes_are_looked_at_per_drive_oldest_first(tmp_path, capsys):
    """Catches an unbounded scan and a wrong order: 17 OPEN outcomes of this master that
    adopted nothing (so each is NOT_APPLICABLE once looked at), named `s0` ... `s16` -- the
    first drive closes the 16 oldest by their numbers (`s9` before `s10`, which a plain
    name sort gets wrong) and leaves `s16` OPEN; the next closes it. None is looked at twice."""
    repo, root, store = world(tmp_path)
    rc, out = drive(root, capsys)                      # a first drive: the project is governed, epoch 1
    assert out["halt"] == "CLOSED", out
    directory = store.dir / OUTCOME_DIR
    for i in range(17):
        waverecovery.open_outcome(directory, f"s{i}", session="sA", epoch=1, holds=())
    rc, out = drive(root, capsys)

    def of(phase):
        return sorted((o.wave_id for n, o in outcomes(store).items()
                       if n.startswith("wave-s") and o.phase.value == phase),
                      key=lambda w: int(w[1:]))

    assert of("OPEN") == ["s16"] and len(of("NOT_APPLICABLE")) == 16, (of("OPEN"), of("NOT_APPLICABLE"))
    rc, out = drive(root, capsys)
    assert of("OPEN") == [] and len(of("NOT_APPLICABLE")) == 17
    assert {o.attempts for n, o in outcomes(store).items() if n.startswith("wave-s")} == {1}


# --------------------------------------------------------------------------- #
# P3-24: the reason is persisted and reads back
# --------------------------------------------------------------------------- #

def test_the_degradation_reason_of_a_wave_is_persisted_in_its_outcome_and_reads_back(
        tmp_path, capsys):
    """Catches a reason that only exists as a derived print: n2's worktree is held by
    someone else when the wave admits it (WORKSPACE), so the wave ran 1 of 2. The outcome
    file the drive wrote says CLOSED with `degradation (2, 1, ('WORKSPACE',))`, equal to
    what the wave record derives; a wave whose record derives none stores None. (The
    controller's own earlier cuts -- capacity, budget, external, in-flight -- are not in it
    yet: P3-24 is registered PARTIAL for that reason.)"""
    keep = []

    def occupy(worktree):
        handle = WorkspaceLease(worktree, "someone-else")
        handle.acquire()
        keep.append(handle)

    repo, root, store = world(tmp_path, hooks={"n2": occupy})
    rc, out = drive(root, capsys)
    degraded = [o for o in outcomes(store).values() if o.degradation is not None]
    assert len(degraded) == 1, outcomes(store)
    first = degraded[0]
    record = read_record(store.dir / "waves" / f"wave-{first.wave_id}.json")
    assert first.phase is waverecovery.OutcomePhase.CLOSED and first.closed_at
    d = record.degradation
    assert first.degradation == (d.ready, d.admitted, tuple(r.value for r in d.reasons))
    assert first.degradation == (2, 1, ("WORKSPACE",))
    assert (first.session, first.epoch) == ("sA", 1) and first.pid == os.getpid()

    sub = tmp_path / "whole"
    sub.mkdir()
    repo, root, store = world(sub)
    rc, out = drive(root, capsys)
    [only] = sorted((store.dir / OUTCOME_DIR).iterdir())
    whole = waverecovery.read_outcome(only)
    assert whole.phase is waverecovery.OutcomePhase.CLOSED and whole.degradation is None

    # with no shared budget at all (the default drive) the outcome is written and read back too
    sub2 = tmp_path / "unbudgeted"
    sub2.mkdir()
    keep.clear()
    repo, root, store = world(sub2, hooks={"n2": occupy}, budget=None, share=None)
    rc, out = drive(root, capsys)
    degraded = [o for o in outcomes(store).values() if o.degradation is not None]
    assert [(o.phase, o.degradation, o.holds) for o in degraded] == [
        (waverecovery.OutcomePhase.CLOSED, (2, 1, ("WORKSPACE",)), ())]


def test_a_wave_that_ran_whole_adds_no_recovery_step_and_writes_no_extra_state(tmp_path, capsys):
    """Catches the recovery being noisy, re-processing a terminal outcome, or writing when
    there is nothing to recover: after a clean drive, two further drives leave the CLOSED
    outcome byte for byte (bytes, inode, mtime), emit no `WAVE_*` step but the degradation
    note, and the recovery does not write the project state (the claim)."""
    repo, root, store = world(tmp_path)
    rc, out = drive(root, capsys)
    assert out["halt"] == "CLOSED", out
    assert not recovery_steps(out), out["steps"]
    [only] = sorted((store.dir / OUTCOME_DIR).iterdir())
    assert waverecovery.read_outcome(only).phase is waverecovery.OutcomePhase.CLOSED
    fixed = snapshot(only)
    for _ in range(2):
        with state_writes_in_recovery() as (writes, probed):
            rc, again = drive(root, capsys)
        assert not recovery_steps(again), again["steps"]
        assert snapshot(only) == fixed and writes == [] and probed == []


# --------------------------------------------------------------------------- #
# termination: every path ends in a state a reader sees (operator directive 2026-10-14)
# --------------------------------------------------------------------------- #

@contextlib.contextmanager
def writes_failing(when):
    """`durable_io.durable_write` raises `OSError` for the outcome files `when(text)` selects."""
    real = durable_io.durable_write

    def failing(path, data, *a, **k):
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
        if Path(path).parent.name == OUTCOME_DIR and when(text):
            raise OSError(28, "No space left on device (injected)")
        return real(path, data, *a, **k)

    durable_io.durable_write = failing
    try:
        yield
    finally:
        durable_io.durable_write = real


def test_an_outcome_that_cannot_be_counted_is_a_named_halt_and_never_a_silent_loop(tmp_path, capsys):
    """Catches a recovery that cannot write its attempt count and so can never be bounded:
    with every write into the outcome directory failing, the drive makes NO attempt (the
    holds stay HELD, attempts stay 0) and halts AMBIGUOUS by name -- `WAVE_RECOVERY_UNBOUNDED`
    -- on every drive, never BUDGET_EXHAUSTED-and-left-for-ever; once the directory is
    writable again the same outcome is recovered and the project closes."""
    repo, root, store = world(tmp_path)
    wave_id = crash(root, capsys)
    path, before = the_outcome(store)
    fixed = snapshot(path)
    with writes_failing(lambda text: True):
        for _ in range(MAX):
            rc, out = drive(root, capsys)
            assert out["halt"] == "AMBIGUOUS", out
            assert out["reason"].startswith("WAVE_RECOVERY_UNBOUNDED"), out["reason"]
            assert f"WAVE_RECOVERY_UNBOUNDED:{wave_id}" in out["steps"]
            assert _held_ids(root) == ["n1", "n2"]
            assert snapshot(path) == fixed and before.attempts == 0
            kill_the_master(store)
    rc, out = drive(root, capsys)
    assert out["halt"] == "CLOSED", out
    assert the_outcome(store)[1].phase is waverecovery.OutcomePhase.RECOVERED


def test_a_crash_inside_the_recovery_attempt_is_counted_and_the_bound_holds(tmp_path, capsys):
    """Catches an attempt count written after the attempt: the probe raises `KeyboardInterrupt`
    (a process killed mid-recovery). Each such drive leaves attempts +1 and the outcome OPEN;
    after the third the next drive ABANDONS it without making another attempt."""
    repo, root, store = world(tmp_path)
    crash(root, capsys)
    CONFIG["evaluate_raises"] = True
    for k in range(1, waverecovery.MAX_WAITS + 1):
        interrupted(root, capsys)
        kill_the_master(store)
        path, outcome = the_outcome(store)
        assert outcome.phase is waverecovery.OutcomePhase.OPEN and outcome.attempts == k
    CONFIG["evaluate_raises"] = False
    rc, out = drive(root, capsys)
    path, outcome = the_outcome(store)
    assert outcome.phase is waverecovery.OutcomePhase.ABANDONED and outcome.attempts == MAX
    assert f"WAVE_RECOVERY_ABANDONED:{outcome.wave_id}" in out["steps"]
    assert _held_ids(root) == ["n1", "n2"]


def test_a_failing_final_write_never_aborts_the_drive_and_the_bound_still_holds(tmp_path, capsys):
    """Catches `OutcomeError` escaping the recovery after the holds were ended (the drive
    aborted AMBIGUOUS "the drive raised OutcomeError"): the write of RECOVERED fails. The
    drive goes on and closes the project, the outcome stays OPEN with its attempt counted,
    and the next drive closes it as NOT_APPLICABLE (every node past READY)."""
    repo, root, store = world(tmp_path)
    crash(root, capsys)
    with writes_failing(lambda text: '"phase": "RECOVERED"' in text):
        rc, out = drive(root, capsys)
    assert out["halt"] == "CLOSED", out
    assert any(s.startswith("WAVE_RECOVERY_LEFT:") for s in out["steps"]), out["steps"]
    path, outcome = the_outcome(store)
    assert outcome.phase is waverecovery.OutcomePhase.OPEN and outcome.attempts == 1
    assert _held_ids(root) == []
    rc, out = drive(root, capsys)
    path, outcome = the_outcome(store)
    assert outcome.phase is waverecovery.OutcomePhase.NOT_APPLICABLE and outcome.attempts == 2


def test_an_unreadable_open_outcome_is_set_aside_once_and_never_re_read(tmp_path, capsys):
    """Catches an unreadable outcome that is re-read and re-reported by every drive with no
    terminal state: the crashed wave's outcome is overwritten with bytes that are not an
    outcome. The next drive names it once (`WAVE_RECOVERY_UNREADABLE`), keeps the file as
    `.quarantined.1`, leaves a terminal ABANDONED outcome, and guesses nothing (the holds
    stay HELD, so the width-2 project halts as before); every later drive is quiet."""
    repo, root, store = world(tmp_path)
    wave_id = crash(root, capsys)
    path, _ = the_outcome(store)
    path.write_bytes(b"{this is not an outcome")
    rc, out = drive(root, capsys)
    assert f"WAVE_RECOVERY_UNREADABLE:{wave_id}" in out["steps"]
    assert out["halt"] != "CLOSED" and _held_ids(root) == ["n1", "n2"]
    kept = path.with_name(path.name + ".quarantined.1")
    assert kept.read_bytes() == b"{this is not an outcome"
    replacement = waverecovery.read_outcome(path)
    assert replacement.phase is waverecovery.OutcomePhase.ABANDONED and kept.name in replacement.note
    fixed = snapshot(path)
    for _ in range(2):
        kill_the_master(store)
        rc, out = drive(root, capsys)
        assert not [s for s in out["steps"] if s.startswith("WAVE_RECOVERY")], out["steps"]
        assert snapshot(path) == fixed


def test_after_an_abandoned_outcome_a_width_1_drive_still_closes_the_project(tmp_path, capsys):
    """Pins the remedy (D7) the bound leaves: an ABANDONED outcome is never acted on, and the
    project it wedged is closed by a width-1 drive (`take_hold` adopts the leftover holds of a
    single-node round) -- the way out is a person's, and it works."""
    repo, root, store = world(tmp_path)
    crash(root, capsys)
    path, _ = the_outcome(store)
    edit(path, lambda d: d.update(attempts=MAX, note="attempts spent"))
    rc, out = drive(root, capsys)                                    # width 2: abandons, stays wedged
    assert the_outcome(store)[1].phase is waverecovery.OutcomePhase.ABANDONED
    assert out["halt"] == "BUDGET_EXHAUSTED" and _held_ids(root) == ["n1", "n2"]
    kill_the_master(store)
    rc, out = drive(root, capsys, width=1)
    assert out["halt"] == "CLOSED", out
    assert _held_ids(root) == []


def test_a_give_back_that_left_the_outcome_open_is_not_reported_as_a_recovery(
        tmp_path, capsys, monkeypatch):
    """Catches `WAVE_RECOVERED ... ended the holds of [n1, n2]` for holds that were already
    ended: the executor refuses the wave whole, the controller gives its holds back itself and
    the outcome stays OPEN. The next drive finds the holds SETTLED (`settle_holds` returns what
    it found), closes the outcome as NOT_APPLICABLE and says nothing of a recovery."""
    repo, root, store = world(tmp_path)

    def refuse(self, *a, **k):
        raise WaveError("injected: the wave could not be run")

    monkeypatch.setattr(orch_module.ParallelWaveExecutor, "run_wave", refuse)
    rc, first = drive(root, capsys)
    monkeypatch.undo()
    assert _held_ids(root) == []                                     # the controller gave them back
    found = [o.wave_id for o in outcomes(store).values()]
    CONFIG["crashed"] = found[0]
    assert the_outcome(store)[1].phase is waverecovery.OutcomePhase.OPEN
    kill_the_master(store)
    rc, out = drive(root, capsys)
    assert out["halt"] == "CLOSED", out
    assert not [s for s in out["steps"] if s.startswith("WAVE_RECOVERED")], out["steps"]
    path, outcome = the_outcome(store)
    assert outcome.phase is waverecovery.OutcomePhase.NOT_APPLICABLE and "already ended" in outcome.note


def test_a_live_controller_whose_holds_a_recovery_claimed_is_fenced_and_launches_nothing(tmp_path):
    """Pins the winner side of the claim (precondition 3): a second controller's recovery
    claims the persisted state inside the first one's admission. The first controller halts
    AMBIGUOUS on its next write, launches nothing, and the holds the recovery ended stay ended."""
    from test_wave_controller import FakeCliLauncher
    repo, root, store = _world(tmp_path)
    a = FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)
    b = FakeCliLauncher(root, repo, {}, dispatch_budget=6, dispatch_reservation=3)
    real_lease, fired, b_steps = a.lease_for, [], []

    def lease_for(node):
        if not fired:
            fired.append(1)
            ProjectController(store, b, Gates(), width=2)._recover_open_waves(b_steps)
        return real_lease(node)

    a.lease_for = lease_for
    result = ProjectController(store, a, Gates(), width=2).run()
    assert [s.kind for s in b_steps] == ["WAVE_RECOVERED"], b_steps
    assert result.halt is HaltClass.AMBIGUOUS and "advanced" in result.reason, result.reason
    assert a.events == []
    assert _held_ids(root) == []
    assert {str(n.lifecycle) for n in store.read_state().nodes} == {"READY"}


# --------------------------------------------------------------------------- #
# the file: strict, fail closed, and never a halt
# --------------------------------------------------------------------------- #

def test_an_outcome_file_that_is_not_one_is_refused_by_the_parser_and_reported_by_the_drive(
        tmp_path, capsys):
    """Catches an outcome read as "none" or trusted when odd: corrupt JSON, a list, an extra
    key, a bad phase, a missing key, NaN, a duplicate key, a lone surrogate, NUL, non-ASCII,
    a link, a FIFO, a directory, an oversize file are each `OutcomeError`; and a drive over
    such a file reports `WAVE_RECOVERY_UNREADABLE` and goes on (the project still closes)."""
    assert waverecovery is not None, "hoh.waverecovery does not exist on this tree"
    repo, root, store = world(tmp_path)
    rc, out = drive(root, capsys)
    [only] = sorted((store.dir / OUTCOME_DIR).iterdir())
    good = json.loads(only.read_bytes())

    def variant(fn):
        d = json.loads(json.dumps(good))
        fn(d)
        return json.dumps(d).encode()

    bad = {
        "not json": b"{", "list": b"[]", "empty": b"", "nan": json.dumps(good).encode().replace(b'"attempts": 0', b'"attempts": NaN'),
        "extra key": variant(lambda d: d.update(extra=1)),
        "missing key": variant(lambda d: d.pop("holds")),
        "phase": variant(lambda d: d.update(phase="DONE")),
        "open with a close time": variant(lambda d: d.update(phase="OPEN", closed_at="2026-10-14T10:00:00Z")),
        "attempts 4": variant(lambda d: d.update(attempts=MAX + 1)),
        "attempts bool": variant(lambda d: d.update(attempts=True)),
        "waits 4": variant(lambda d: d.update(waits=waverecovery.MAX_WAITS + 1)),
        "waits bool": variant(lambda d: d.update(waits=True)),
        "pid_start with a space": variant(lambda d: d.update(pid_start="boot 1")),
        "pid_start number": variant(lambda d: d.update(pid_start=7)),
        "wave id newline": variant(lambda d: d.update(wave_id="w1\n")),
        "time month 13": variant(lambda d: d.update(created_at="2026-13-01T00:00:00Z")),
        "note NUL": variant(lambda d: d.update(note="a\u0000b")),
        "note non-ASCII": variant(lambda d: d.update(note="café")),
        "degradation no reasons": variant(lambda d: d.update(degradation={"ready": 2, "admitted": 1, "reasons": []})),
        "degradation reason lower": variant(lambda d: d.update(
            degradation={"ready": 2, "admitted": 1, "reasons": ["workspace"]})),
        "hold twice": variant(lambda d: d.update(holds=[
            {"node_id": "n1", "run_id": "n1", "reservation_id": "a-n1", "amount": 1}] * 2)),
        "hold amount 0": variant(lambda d: d.update(holds=[
            {"node_id": "n1", "run_id": "n1", "reservation_id": "a-n1", "amount": 0}])),
        "oversize": json.dumps(good).encode() + b" " * (waverecovery.MAX_OUTCOME_BYTES + 1),
        "257 holds": variant(lambda d: d.update(holds=[
            {"node_id": f"n{i}", "run_id": f"n{i}", "reservation_id": f"a{i}-n{i}", "amount": 1} for i in range(257)])),
        "17 reasons": variant(lambda d: d.update(
            degradation={"ready": 2, "admitted": 1, "reasons": ["WIDTH"] * 17})),
    }
    problems = []
    for label, data in bad.items():
        try:
            waverecovery.parse(data)
        except waverecovery.OutcomeError:
            continue
        problems.append(label)
    assert problems == []
    assert waverecovery.parse(only.read_bytes()).phase is waverecovery.OutcomePhase.CLOSED

    directory = store.dir / OUTCOME_DIR
    (directory / "wave-link.json").symlink_to(only)
    os.mkfifo(directory / "wave-fifo.json")
    (directory / "wave-dir.json").mkdir()
    (directory / "wave-x.json.bak").write_bytes(b"{")          # not an outcome's name: not looked at
    (directory / "notes.txt").write_bytes(b"{")
    for name in ("wave-link.json", "wave-fifo.json", "wave-dir.json"):
        with pytest.raises(waverecovery.OutcomeError):
            waverecovery.read_outcome(directory / name)
    assert waverecovery.read_outcome(directory / "wave-none.json") is None
    rc, out = drive(root, capsys)
    unreadable = sorted(s for s in out["steps"] if s.startswith("WAVE_RECOVERY_UNREADABLE:"))
    assert unreadable == ["WAVE_RECOVERY_UNREADABLE:dir", "WAVE_RECOVERY_UNREADABLE:fifo",
                          "WAVE_RECOVERY_UNREADABLE:link"], out["steps"]
    assert out["halt"] == "CLOSED", out
    # quarantined once: set aside under a versioned name (kept), replaced by a terminal
    # outcome that says so -- and then quiet: the next drive neither reads nor reports them
    fixed = {}
    for name in ("link", "fifo", "dir"):
        assert os.path.lexists(directory / f"wave-{name}.json.quarantined.1"), name
        replacement = waverecovery.read_outcome(directory / f"wave-{name}.json")
        assert replacement.phase is waverecovery.OutcomePhase.ABANDONED, name
        assert f"wave-{name}.json.quarantined.1" in replacement.note
        fixed[name] = snapshot(directory / f"wave-{name}.json")
    assert (directory / "wave-link.json.quarantined.1").is_symlink()
    rc, out = drive(root, capsys)
    assert not [s for s in out["steps"] if s.startswith("WAVE_RECOVERY")], out["steps"]
    assert {n: snapshot(directory / f"wave-{n}.json") for n in fixed} == fixed
