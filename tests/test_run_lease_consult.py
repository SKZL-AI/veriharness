"""P3-18 slice 3: the run lease gets its consumers -- the project loop reads it, it is listed, it names the master
epoch, and the old ones are archived.

Slice 2 made `hoh run` hold the lease of the run it drives. Nothing that decides *about* a run read it. These tests drive
the readers (`rundrive.consult`, the launcher's `evaluate`, `resume_decision` through `hoh project resume / status / list`),
the listing and the archive (`hoh project leases [--archive]`) and the record's new key (`hoh run --master-epoch`). A lease
another holder has is written with `RunLeases(host="other-host", ...)`: a holder in another host is LIVE until its expiry
whatever its pid says -- the one case a process lock cannot see. A lease of a driver that is gone is an expired one
(`clock` in the past). No test starts Herdr or an agent, fills a disk or reads outside `tmp_path`.

Mutant ids (M1..) are the spec's table (`mutants.py`); each test says which it kills.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_failure_classes import repo as repo  # a fixture; the alias form is an explicit re-export
from test_run_lease_drive import World  # the fixture class: one run `r1`, a stand-in iteration

from hoh import cli, durable_io, leaseadmin, rundrive, runlease
from hoh.contracts import RunState
from hoh.launcher import Dispatch, HohRunLauncher, lease_refusal
from hoh.orchestrator import RunVerdict
from hoh.project import Lifecycle, ProjectState, TaskNode
from hoh.projectstore import BLOCK, EVALUATE, WAIT, ProjectStore, resume_decision
from hoh.leaseadmin import UNREADABLE, archive_ended, snapshot
from hoh.runlease import LIVE, LOST, RELEASED, RunLease, RunLeaseBusy, RunLeaseError, RunLeases, StaleRunLease, parse, render
from hoh.store import RunStore

OTHER = dict(host="other-host", pid=4242, pidns=lambda: "pidns:[999]", start=lambda pid: "s1",
             boot=lambda: "boot-of-the-other-machine")      # another machine has its own boot id (review B1)
DAY = 86400


def _stamp(days: float = 0, hours: float = 0, seconds: float = 0) -> str:
    moment = datetime.now(UTC) - timedelta(days=days, hours=hours, seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
    """A clock a test sets: the records it writes carry the times it says."""

    def __init__(self, now: str) -> None:
        self.now = now

    def __call__(self) -> str:
        return self.now


def _dir(root: Path) -> Path:
    return Path(root) / "projects" / rundrive.LEASE_PROJECT / "run-leases"


def _archive_dir(root: Path) -> Path:
    return Path(root) / "projects" / rundrive.LEASE_PROJECT / "run-leases-archive"


def _leases(root: Path, **environment) -> RunLeases:
    return ProjectStore(root, rundrive.LEASE_PROJECT).run_leases(**environment)


def _held(root: Path, run_id: str = "r1", *, ttl: int = 3600, epoch: int | None = None) -> RunLease:
    """The lease of `run_id` as a live holder in another host took it."""
    return _leases(root, **OTHER).acquire(run_id, "other-session", ttl_seconds=ttl, master_epoch=epoch)


def _ended(root: Path, run_id: str, *, released_days: float, takeovers: int = 0) -> RunLease:
    """A lease that was released `released_days` ago; with `takeovers`, a lease that was taken over that many times
    (each one parks the record it replaced), the last of them released `released_days` ago."""
    clock = Clock(_stamp(days=released_days + 1 + takeovers))
    leases = _leases(root, clock=clock, **OTHER)
    lease = leases.acquire(run_id, "other-session", ttl_seconds=300)
    for step in range(takeovers):
        clock.now = _stamp(days=released_days + 1 + takeovers - step - 0.5)
        lease = leases.release(lease, reason="run ended")
        clock.now = _stamp(days=released_days + 1 + takeovers - step - 0.25)
        lease = leases.acquire(run_id, "other-session", ttl_seconds=300)
    clock.now = _stamp(days=released_days)
    return leases.release(lease, reason="run ended")


def _lapsed(root: Path, run_id: str, *, days: float) -> RunLease:
    """A lease whose holder never released it and whose ttl ran out `days` ago: LOST."""
    return _leases(root, clock=Clock(_stamp(days=days, seconds=300 + 5)), **OTHER).acquire(
        run_id, "other-session", ttl_seconds=300)


def _bytes(path: Path) -> dict[str, bytes]:
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def _names(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def _damage(root: Path, how: str, run_id: str = "r1") -> Path:
    directory = _dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}.json"
    if how == "garbage":
        path.write_bytes(b"this is not json\n")
    elif how == "not-an-object":
        path.write_bytes(b"[]\n")
    elif how == "oversize":
        path.write_bytes(b"{" + b" " * 5000 + b"}\n")
    elif how == "utf-16":
        path.write_bytes("{}".encode("utf-16"))
    elif how == "nul":
        path.write_bytes(b"{\x00}\n")
    elif how == "latin-1":
        path.write_bytes('{"holder": "caf\xe9"}\n'.encode("latin-1"))
    elif how == "noncanonical":
        path.write_bytes(render(_held(root, "other")) + b"\n")
        os.replace(directory / "other.json", directory / "other.json.moved")
    elif how == "directory":
        path.mkdir()
    elif how == "dangling-link":
        path.symlink_to(root / "nowhere")
    elif how == "fifo":
        os.mkfifo(path)
    elif how == "no-permission":
        if os.geteuid() == 0:
            pytest.skip("root reads a mode-000 file")
        path.write_bytes(b"{}\n")
        path.chmod(0)
    return path


DAMAGE = ["garbage", "not-an-object", "oversize", "utf-16", "nul", "latin-1", "directory", "dangling-link", "fifo",
          "no-permission"]


def _project(root: Path, *nodes: TaskNode, pid: str = "p") -> ProjectStore:
    store = ProjectStore(root, pid)
    store.create(ProjectState(project_id=pid, repo_path=str(root), nodes=list(nodes)))
    return store


def _running(node_id: str = "a", run_id: str | None = "r1") -> TaskNode:
    return TaskNode(id=node_id, lifecycle=Lifecycle.RUNNING, run_id=run_id)


def _run_state(root: Path, run_id: str = "r1") -> RunStore:
    store = RunStore(root, run_id)
    store.write_state(RunState(run_id=run_id, repo_path=str(root), project_name="p", spec_path="/x/s.md",
                               spec_digest="a" * 16, policy_digest="b" * 16, profile_digest="c" * 16))
    return store


# --------------------------------------------------------------------------- #
# the record: the master epoch
# --------------------------------------------------------------------------- #

def _lease(**kw) -> RunLease:
    base = dict(run_id="r1", holder="h1", generation=1, ttl_seconds=300, acquired_at="2026-01-01T00:00:00Z",
                renewed_at="2026-01-01T00:00:00Z", expires_at="2026-01-01T00:05:00Z", holder_pid=100,
                holder_boot="b", holder_host="h", holder_pidns="ns", holder_start="s")
    base.update(kw)
    return RunLease(**base)


def test_a_lease_without_an_epoch_is_spelled_exactly_as_before_and_one_with_an_epoch_says_so():
    """Kills M17 (a /2 spelling for every lease: the bytes of every record of the base change), M18 (a /1 record that carries the key is read). The /1 literal is a record of the base, byte for byte; a /2 record without the key, `null`, a bool, a float, a string, 0 and 10**9+1 are refused, 1 and 10**9 are read."""
    one = (b'{\n "acquired_at": "2026-01-01T00:00:00Z",\n "expires_at": "2026-01-01T00:05:00Z",\n "generation": 1,\n'
           b' "holder": "h1",\n "holder_boot": "b",\n "holder_host": "h",\n "holder_pid": 100,\n "holder_pidns": "ns",\n'
           b' "holder_start": "s",\n "released_at": null,\n "released_reason": "",\n "renewed_at": "2026-01-01T00:00:00Z",\n'
           b' "run_id": "r1",\n "schema": "veriharness.run-lease/1",\n "ttl_seconds": 300\n}\n')
    assert render(_lease()) == one and parse(one) == _lease() and _lease().master_epoch is None
    two = render(_lease(master_epoch=7))
    assert runlease.SCHEMA_EPOCH == "veriharness.run-lease/2"
    assert b'"schema": "veriharness.run-lease/2"' in two and b'"master_epoch": 7' in two
    assert parse(two) == _lease(master_epoch=7) and parse(two).master_epoch == 7
    document = json.loads(two)

    def reason(doc: dict) -> str:
        with pytest.raises(RunLeaseError) as err:
            parse((json.dumps(doc, sort_keys=True, indent=1) + "\n").encode())
        return err.value.reason

    no_key = {k: v for k, v in document.items() if k != "master_epoch"}
    assert reason(no_key) == "schema"                                         # /2 without the key
    assert reason({**json.loads(one), "master_epoch": 7}) == "schema"          # /1 with it
    assert reason({**document, "master_epoch": None}) == "noncanonical"         # valid content, not what render writes
    assert reason({**document, "schema": "veriharness.run-lease/3"}) == "schema"
    for bad in (0, -1, True, 1.0, "7", [7], 10 ** 9 + 1):
        assert reason({**document, "master_epoch": bad}) == "invalid", bad
    assert parse(render(_lease(master_epoch=1))).master_epoch == 1
    assert parse(render(_lease(master_epoch=10 ** 9))).master_epoch == 10 ** 9


def test_the_epoch_is_part_of_the_grant_a_renewal_keeps_it_and_another_epoch_is_fenced(tmp_path):
    """Kills M15 (`_current` ignores the epoch: a handle with another epoch renews or releases), M19 (`acquire` ignores its argument), M20 (a bool is an epoch)."""
    leases = RunLeases(tmp_path / "leases", **OTHER)
    taken = leases.acquire("r1", "h1", ttl_seconds=300, master_epoch=5)
    assert taken.master_epoch == 5 and leases.read("r1").master_epoch == 5
    renewed = leases.renew(taken)
    assert renewed.master_epoch == 5 and parse((tmp_path / "leases" / "r1.json").read_bytes()).master_epoch == 5
    for other in (replace(renewed, master_epoch=6), replace(renewed, master_epoch=None)):
        with pytest.raises(StaleRunLease):
            leases.renew(other)
        with pytest.raises(StaleRunLease):
            leases.release(other, reason="x")
    released = leases.release(renewed, reason="run ended")
    assert released.master_epoch == 5 and leases.read("r1").master_epoch == 5
    for bad in (0, True, 1.5, "3", 10 ** 9 + 1):
        with pytest.raises(RunLeaseError) as err:
            leases.acquire("r2", "h1", ttl_seconds=300, master_epoch=bad)
        assert err.value.reason == "invalid"
    assert leases.read("r2") is None and not (tmp_path / "leases" / "r2.json").exists()


def test_hoh_run_records_the_master_epoch_it_was_started_under_and_a_bare_run_records_none(tmp_path, repo, monkeypatch,
                                                                                            capsys):
    """Kills M16 (`cmd_run` does not forward `--master-epoch`): the lease read *while the iteration runs* names the epoch and is spelled /2; the same run driven bare is /1 with no epoch. Through `cli.main`, the real parser."""
    world = World(tmp_path, repo, monkeypatch, capsys)
    seen: dict = {}

    def inside(n, state):
        seen["bytes"] = (world.lease_dir / "r1.json").read_bytes()
        seen["lease"] = world.leases.read("r1")

    world.hook = inside
    rc, _out, err = world.run("--master-session", "m1", "--master-epoch", "7")
    assert rc == 0, err
    assert seen["lease"].master_epoch == 7 and b'"master_epoch": 7' in seen["bytes"]
    assert world.leases.read("r1").master_epoch == 7 and world.leases.read("r1").released_at is not None
    rc, _out, err = world.run("--master-session", "m1", "--master-epoch", "9")
    assert rc == 0, err
    assert seen["lease"].master_epoch == 9 and seen["lease"].generation == 2
    rc, _out, err = world.run()
    assert rc == 0, err
    assert seen["lease"].master_epoch is None and b"master_epoch" not in seen["bytes"]
    assert b"veriharness.run-lease/1" in seen["bytes"] and seen["lease"].generation == 3


# --------------------------------------------------------------------------- #
# consult: what the lease of a run says, read-only, fail closed
# --------------------------------------------------------------------------- #

def test_consult_says_none_live_lost_released_and_unreadable_and_writes_nothing(tmp_path):
    """Kills M8 (an error reads as NONE). An absent record is NONE, a live one LIVE (with the epoch said), an expired one LOST, a released one RELEASED; read-only: the runs root is byte-for-byte what it was and an absent lease directory stays absent; an id the lease grammar refuses is looked for under the hashed name `hoh run` keeps it under."""
    root = tmp_path / "runs"
    root.mkdir()
    before = _names(root)
    for run_id in ("r1", "-a-legal-run-id-that-starts-with-a-dash", "x" * 80):
        fact = rundrive.consult(root, run_id)
        assert (fact.state, fact.lease, fact.alive, fact.reclaimable) == ("NONE", None, False, False)
    assert _names(root) == before                                # nothing was made: no projects/, no lock
    _held(root, "live", epoch=3)
    _ended(root, "ended", released_days=2)
    _lapsed(root, "lapsed", days=1)
    snapshot = _bytes(root)
    live = rundrive.consult(root, "live")
    assert (live.state, live.alive, live.reclaimable) == ("LIVE", True, False)
    assert "other-session" in live.why and "master epoch 3" in live.why
    ended = rundrive.consult(root, "ended")
    assert (ended.state, ended.alive, ended.reclaimable) == ("RELEASED", False, True) and "master epoch" not in ended.why
    lapsed = rundrive.consult(root, "lapsed")
    assert (lapsed.state, lapsed.alive, lapsed.reclaimable) == ("LOST", False, True) and "expired" in lapsed.why
    assert rundrive.consult(root, "nothing-here").state == "NONE"
    assert _bytes(root) == snapshot and sorted(os.listdir(_dir(root))) == [".lock", "ended.json", "lapsed.json", "live.json"]
    odd = "-a-legal-run-id-that-starts-with-a-dash"
    _leases(root, **OTHER).acquire(rundrive.lease_name(odd), "other-session", ttl_seconds=3600)
    assert rundrive.consult(root, odd).state == "LIVE"           # the same hashed name `hoh run` keeps it under


@pytest.mark.parametrize("how", DAMAGE)
def test_a_lease_record_that_cannot_be_read_is_unreadable_not_free(tmp_path, how):
    """Kills M8 (an error reads as NONE): every damaged entry is UNREADABLE, says why, and is left exactly as it was (no hang on a FIFO, no write through a link)."""
    root = tmp_path / "runs"
    path = _damage(root, how)
    kind = (path.is_symlink(), path.is_dir(), path.exists() and path.stat().st_mode)
    fact = rundrive.consult(root, "r1")
    assert fact.state == "UNREADABLE" and fact.lease is None and not fact.alive and not fact.reclaimable
    assert fact.why and len(fact.why) <= 300
    assert (path.is_symlink(), path.is_dir(), path.exists() and path.stat().st_mode) == kind
    if how == "no-permission":
        path.chmod(0o600)


@pytest.mark.parametrize("how", ["file", "link"])
def test_a_lease_directory_that_is_not_a_directory_of_its_own_is_unreadable(tmp_path, how):
    root = tmp_path / "runs"
    directory = _dir(root)
    directory.parent.mkdir(parents=True)
    if how == "file":
        directory.write_bytes(b"not a directory\n")
    else:
        (tmp_path / "elsewhere").mkdir()
        directory.symlink_to(tmp_path / "elsewhere")
    assert rundrive.consult(root, "r1").state == "UNREADABLE"
    assert not list((tmp_path / "elsewhere").iterdir()) if how == "link" else True


# --------------------------------------------------------------------------- #
# the launcher: evaluate reads the lease; the refusal is classified
# --------------------------------------------------------------------------- #

def test_the_launcher_reads_a_live_lease_of_a_run_whose_lock_is_free_as_in_flight(tmp_path):
    """Kills M1 (`evaluate` does not consult the lease), M4 (the lease is looked for under `node.id`, not the run's name). The run's process lock and marker are free (the holder is in another host), so only the lease can say the run is alive; a run without a lease is evaluated as before (the control)."""
    root = tmp_path / "runs"
    _run_state(root)
    node = TaskNode(id="n", run_id="r1")
    free = HohRunLauncher(root, tmp_path).evaluate(node)
    assert free.verdict is not RunVerdict.IN_FLIGHT                       # the control: nothing held, nothing alive
    _held(root, epoch=4)
    held = _bytes(_dir(root))
    out = HohRunLauncher(root, tmp_path).evaluate(node)
    assert out.verdict is RunVerdict.IN_FLIGHT, out
    assert "live run lease" in out.detail and "other-session" in out.detail and "master epoch 4" in out.detail
    assert HohRunLauncher(root, tmp_path).evaluate(TaskNode(id="r1")).verdict is RunVerdict.IN_FLIGHT   # run_id absent: node id
    assert _bytes(_dir(root)) == held and len(out.detail) <= 300                # read, not touched


def test_the_launcher_does_not_believe_a_lease_nobody_holds_but_says_it_is_reclaimable(tmp_path):
    """Kills M3 (a RELEASED lease is said on every outcome: a normal end is no news), M1. The verdict is the one the run's own state gives (control: no lease); the detail carries the lease only for a lease whose holder is gone; the lease directory is not touched."""
    root = tmp_path / "runs"
    _run_state(root)
    node = TaskNode(id="n", run_id="r1")
    control = HohRunLauncher(root, tmp_path).evaluate(node)
    _lapsed(root, "r1", days=1)
    held = _bytes(_dir(root))
    lost = HohRunLauncher(root, tmp_path).evaluate(node)
    assert lost.verdict is control.verdict and lost.verdict is not RunVerdict.IN_FLIGHT
    assert lost.detail.startswith(control.detail) and "run lease LOST" in lost.detail
    assert " -- reclaimable by the next 'hoh run'" in lost.detail and "expired" in lost.detail
    assert _bytes(_dir(root)) == held
    _ended(root, "r2", released_days=2)
    _run_state(root, "r2")
    released = HohRunLauncher(root, tmp_path).evaluate(TaskNode(id="m", run_id="r2"))
    assert released.verdict is control.verdict and released.detail == control.detail


@pytest.mark.parametrize("how", ["garbage", "directory", "fifo", "oversize"])
def test_the_launcher_reads_a_lease_it_cannot_read_as_undetermined(tmp_path, how):
    """Kills M2 (an unreadable lease falls through to the run's state): not a free run, a person looks."""
    root = tmp_path / "runs"
    _run_state(root)
    _damage(root, how)
    out = HohRunLauncher(root, tmp_path).evaluate(TaskNode(id="n", run_id="r1"))
    assert out.verdict is RunVerdict.UNDETERMINED and "run lease of r1 cannot be read" in out.detail


def test_a_run_that_was_never_driven_by_hoh_run_is_evaluated_as_before(tmp_path):
    """Compatibility (the lease is the third witness, not a precondition): no lease directory at all, and a lease
    directory that holds other runs' leases only -- the verdict is the run's own and nothing is made."""
    root = tmp_path / "runs"
    _run_state(root)
    node = TaskNode(id="n", run_id="r1")
    plain = HohRunLauncher(root, tmp_path).evaluate(node)
    assert not (root / "projects").exists()
    _held(root, "somebody-else")
    again = HohRunLauncher(root, tmp_path).evaluate(node)
    assert (again.verdict, again.detail) == (plain.verdict, plain.detail)


def test_the_generic_run_lease_refusal_of_hoh_run_is_a_named_halt_not_a_rejection(tmp_path, monkeypatch):
    """Kills M5 (classified for any exit code), M6 (never classified), M7 (a looser prefix). Nothing ran, so the verdict is UNDETERMINED with the line `hoh run` printed; the words of a lease another driver holds are still IN_FLIGHT, and a lease lost *during* the run (iterations ran) is read from the run's state."""
    root = tmp_path / "runs"
    _run_state(root)
    node = TaskNode(id="n", run_id="r1")
    refused = ("run lease refused (unreadable): run lease: /x/r1.json: Is a directory; no run state was written. "
               "Look at the record, then move it aside (a versioned rename) to go on")

    def launcher_for(exit_code: int, stderr: str) -> HohRunLauncher:
        instance = HohRunLauncher(root, tmp_path)
        monkeypatch.setattr(instance, "_run_cli", lambda argv: Dispatch(exit_code, "", stderr))
        return instance

    out = launcher_for(2, "noise\n" + refused + "\n").launch(node)
    assert out.verdict is RunVerdict.UNDETERMINED and out.detail == refused[:300]
    assert lease_refusal(f"x\n{refused}\ny") == refused[:300] and lease_refusal("") is None and lease_refusal(None) is None
    assert lease_refusal("not run lease refused (x)") is None and lease_refusal(" run lease refused (x)") is None
    control = launcher_for(2, "an exit 2 that says nothing about leases\n").launch(node)
    assert control.verdict is RunVerdict.NOT_STARTED                      # this run has not begun: the state's own verdict
    assert launcher_for(1, refused).launch(node) == control               # exit 1 is not this refusal
    held = launcher_for(2, "Run r1 is already being executed (held by 'x' ...); no run state was written\n").launch(node)
    assert held.verdict is RunVerdict.IN_FLIGHT
    lost = launcher_for(2, "run lease lost: 3 renewals in a row failed; no further iteration is started\n").launch(node)
    assert lost == control                                                # iterations ran: the state tells, not the line
    keepalive = ("run lease refused (keepalive): the keepalive thread could not be started (can't start new thread); "
                 "no run state was written")
    assert launcher_for(2, keepalive).launch(node).detail == keepalive


# --------------------------------------------------------------------------- #
# resume_decision: pure, with the leases a caller read
# --------------------------------------------------------------------------- #

def _state(tmp_path, *nodes: TaskNode) -> ProjectState:
    return ProjectState(project_id="p", repo_path=str(tmp_path), nodes=list(nodes))


def test_resume_decision_without_leases_is_what_it_was(tmp_path):
    """Compatibility: no argument, an empty mapping, and every node NONE give the same answer, word for word."""
    state = _state(tmp_path, _running("a"), _running("b", None))
    base = resume_decision(state)
    assert base[0] == EVALUATE
    assert base[1] == ("nodes recorded as RUNNING: a, b. Whether that work finished, crashed, or is still live cannot "
                       "be read from this state -- it has to be established against the run record before anything "
                       "else is decided.")
    assert resume_decision(state, {}) == base and resume_decision(state, None) == base
    assert resume_decision(state, {"a": ("NONE", "x"), "b": ("NONE", "y")}) == base
    assert resume_decision(state, {"zzz": ("LIVE", "not a running node")}) == base


def test_resume_decision_waits_when_every_running_node_holds_a_live_lease_and_evaluates_otherwise(tmp_path):
    """Kills M9 (any LIVE node waits), M14 (the lease note names no cause). A node that is missing from the facts is not alive; READY nodes are not asked."""
    state = _state(tmp_path, _running("a"), _running("b"))
    live = {"a": ("LIVE", "held by 'x' until T1"), "b": ("LIVE", "held by 'y' until T2")}
    verdict, why = resume_decision(state, live)
    assert verdict == WAIT == "wait"
    assert "a: held by 'x' until T1" in why and "b: held by 'y' until T2" in why and "not evaluated" in why
    mixed = resume_decision(state, {"a": live["a"], "b": ("LOST", "lease of 'y' expired at T3")})
    assert mixed[0] == EVALUATE and "a lease LIVE (held by 'x' until T1)" in mixed[1]
    assert "b lease LOST (lease of 'y' expired at T3) -- reclaimable by the next 'hoh run'" in mixed[1]
    assert "Run leases: a lease LIVE (held by 'x' until T1); b lease LOST" in mixed[1]
    assert resume_decision(state, {"a": live["a"]})[0] == EVALUATE                  # b has no lease: not alive
    released = resume_decision(state, {"a": ("RELEASED", "released by 'x' at T4: run ended"), "b": ("NONE", "")})
    assert released[0] == EVALUATE and "a lease RELEASED (released by 'x' at T4: run ended) -- reclaimable" in released[1]
    assert "b lease" not in released[1]
    settled = _state(tmp_path, TaskNode(id="a", lifecycle=Lifecycle.READY), _running("b"))
    assert resume_decision(settled, {"a": ("UNREADABLE", "x"), "b": live["b"]})[0] == WAIT      # READY nodes are not asked


def test_resume_decision_blocks_on_a_lease_that_cannot_be_read(tmp_path):
    """Kills M10 (UNREADABLE is evaluated): a record that cannot be read is not a free run, so the node is neither evaluated as dead nor relaunched; the reason names the node and the cause, and UNREADABLE wins over a LIVE neighbour."""
    state = _state(tmp_path, _running("a"), _running("b"))
    verdict, why = resume_decision(state, {"a": ("UNREADABLE", "unreadable: /x/r1.json: Is a directory"),
                                           "b": ("LIVE", "held by 'y'")})
    assert verdict == BLOCK and "a cannot be read (unreadable: /x/r1.json: Is a directory)" in why
    assert "not a free run" in why and "b cannot be read" not in why


def test_the_closed_ready_and_damaged_answers_do_not_read_leases(tmp_path):
    """The leases change RUNNING nodes' answers only: a damage verdict stays a damage verdict and a READY node resumes, whatever a mapping says."""
    ready = _state(tmp_path, TaskNode(id="a"))
    assert resume_decision(ready, {"a": ("LIVE", "x")}) == resume_decision(ready)
    damaged = _state(tmp_path, _running("a"), TaskNode(id="b", dependencies=["missing"]))
    assert resume_decision(damaged, {"a": ("LIVE", "x")})[0] == BLOCK
    assert resume_decision(damaged, {"a": ("LIVE", "x")}) == resume_decision(damaged)


# --------------------------------------------------------------------------- #
# the project loop's commands: status, resume and list read the lease of a RUNNING node
# --------------------------------------------------------------------------- #

def _resume(root: Path, capsys, command: str = "resume") -> tuple[int, str]:
    capsys.readouterr()
    rc = cli.main(["--root", str(root), "project", command, "p"])
    return rc, capsys.readouterr().out


def test_the_project_loop_reads_the_run_lease_of_a_running_node_through_the_cli_and_the_launcher(tmp_path, capsys):
    """THE PROBE. Kills M11 (`project resume`/`status` do not pass the leases), M12 (`project list` does not), M1 and M4 (the launcher). One RUNNING node `a` whose run is `r1`: with no lease the answer is what it always was (evaluate, nothing made); with a live lease of a driver in another host it is `wait` -- in `resume` (JSON), in `status` and in `list` -- and the launcher's `evaluate` says in flight with the epoch; after the driver's lease ran out the answer is evaluate again, saying the lease is reclaimable; a lease that cannot be read blocks."""
    root = tmp_path / "runs"
    store = _project(root, _running("a", "r1"))
    _run_state(root)
    rc, out = _resume(root, capsys)
    assert rc == 0 and json.loads(out)["verdict"] == "evaluate" and "Run leases" not in json.loads(out)["reason"]
    assert not (root / "projects" / rundrive.LEASE_PROJECT).exists()           # asking made nothing
    _held(root, epoch=6)
    held = _bytes(_dir(root))
    rc, out = _resume(root, capsys)
    body = json.loads(out)
    assert rc == 0 and body["verdict"] == "wait" and "other-session" in body["reason"] and "master epoch 6" in body["reason"]
    rc, out = _resume(root, capsys, "status")
    assert rc == 0 and "verdict          wait" in out
    capsys.readouterr()
    assert cli.main(["--root", str(root), "project", "list"]) == 0
    assert "p  wait " in capsys.readouterr().out
    node = store.read_state().nodes[0]
    verdict = HohRunLauncher(root, tmp_path).evaluate(node)
    assert verdict.verdict is RunVerdict.IN_FLIGHT and "master epoch 6" in verdict.detail
    assert _bytes(_dir(root)) == held
    lease_dir = _dir(root)
    os.replace(lease_dir / "r1.json", tmp_path / "held.json")
    _lapsed(root, "r1", days=1)
    rc, out = _resume(root, capsys)
    body = json.loads(out)
    assert rc == 0 and body["verdict"] == "evaluate" and "lease LOST" in body["reason"] and "reclaimable" in body["reason"]
    os.replace(lease_dir / "r1.json", tmp_path / "lapsed.json")
    _damage(root, "garbage")
    rc, out = _resume(root, capsys)
    assert rc == 0 and json.loads(out)["verdict"] == "block" and "cannot be read" in json.loads(out)["reason"]


def test_consult_nodes_asks_the_running_nodes_only_and_under_the_name_the_launcher_uses(tmp_path):
    """Kills M13 (every node is asked), M42 (a node's run is looked for under its id): a READY or a settled node is not RUNNING, whatever lease its run left; a node without a `run_id` is asked under its own id."""
    root = tmp_path / "runs"
    state = _state(tmp_path, TaskNode(id="ready"), _running("a", "run-a"), _running("b", None),
                   TaskNode(id="done", lifecycle=Lifecycle.MERGED, run_id="gone"))
    _held(root, "run-a")
    _held(root, "ready")
    _held(root, "gone")
    facts = rundrive.consult_nodes(root, state)
    assert sorted(facts) == ["a", "b"] and facts["a"][0] == "LIVE" and facts["b"][0] == "NONE"
    _held(root, "b")
    assert rundrive.consult_nodes(root, state)["b"][0] == "LIVE"


def test_a_node_without_a_run_id_is_asked_under_its_own_id(tmp_path, capsys):
    """Through `hoh project resume`: the launcher names a node's run `run_id or id`; the project commands ask the lease of that name (kills M42)."""
    root = tmp_path / "runs"
    _project(root, _running("a", None))
    _held(root, "a")
    rc, out = _resume(root, capsys)
    assert rc == 0 and json.loads(out)["verdict"] == "wait"


# --------------------------------------------------------------------------- #
# enumeration: RunLeases.snapshot and `hoh project leases`
# --------------------------------------------------------------------------- #

def test_snapshot_lists_every_lease_judged_now_and_every_unreadable_one_and_writes_nothing(tmp_path):
    """Kills M21 (an unreadable file is skipped), M23 (not sorted), M41 (a listing makes the directory). Parked copies are counted, not listed as runs; the lock is not a lease; states are `judge`'s."""
    leases = RunLeases(tmp_path / "leases")
    assert snapshot(leases) == [] and not (tmp_path / "leases").exists()
    root = tmp_path / "runs"
    _held(root, "c-live", epoch=2)
    _ended(root, "b-ended", released_days=3, takeovers=2)
    _lapsed(root, "d-lapsed", days=1)
    _damage(root, "garbage", "a-broken")
    _damage(root, "directory", "e-dir")
    before = _bytes(_dir(root)), _names(_dir(root))
    rows = snapshot(_leases(root))
    assert [r.run_id for r in rows] == ["a-broken", "b-ended", "c-live", "d-lapsed", "e-dir"]
    assert [r.state for r in rows] == [UNREADABLE, RELEASED, LIVE, LOST, UNREADABLE]
    assert [r.reclaimable for r in rows] == [False, True, False, True, False]
    assert [r.parked for r in rows] == [0, 2, 0, 0, 0]
    assert rows[0].lease is None and rows[0].why and rows[1].lease.generation == 3 and rows[2].lease.master_epoch == 2
    assert (_bytes(_dir(root)), _names(_dir(root))) == before


def test_snapshot_refuses_what_it_cannot_list_and_more_files_than_its_bound(tmp_path, monkeypatch):
    """Kills M22 (the bound is `>=`): exactly the bound is read, one more refuses; a file in place of the directory refuses."""
    (tmp_path / "file").write_bytes(b"x")
    with pytest.raises(RunLeaseError) as err:
        snapshot(RunLeases(tmp_path / "file"))
    assert err.value.reason == "unreadable"
    monkeypatch.setattr(leaseadmin, "MAX_SNAPSHOT", 3)
    root = tmp_path / "runs"
    for name in ("a", "b", "c"):
        _ended(root, name, released_days=1)
    assert [r.run_id for r in snapshot(_leases(root))] == ["a", "b", "c"]            # exactly the bound is read
    _ended(root, "d", released_days=1)
    with pytest.raises(RunLeaseError) as err:
        snapshot(_leases(root))
    assert err.value.reason == "invalid" and "archive" in str(err.value)


def test_hoh_project_leases_lists_the_leases_one_line_each(tmp_path, capsys):
    """Kills M37 (an unreadable lease does not change the exit status: a script must not see 0). The listing makes nothing; an unlistable directory is exit 4."""
    root = tmp_path / "runs"
    root.mkdir()
    assert cli.main(["--root", str(root), "project", "leases"]) == 0
    assert capsys.readouterr().out == "0 lease(s): none\n" and not (root / "projects").exists()
    _held(root, "live", epoch=8)
    _ended(root, "ended", released_days=2, takeovers=1)
    _lapsed(root, "lapsed", days=1)
    held = _bytes(root)
    assert cli.main(["--root", str(root), "project", "leases"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:3] == [
        "ended  RELEASED   gen=2 holder=other-session epoch=- parked=1  reclaimable",
        "lapsed  LOST       gen=1 holder=other-session epoch=- parked=0  reclaimable",
        "live  LIVE       gen=1 holder=other-session epoch=8 parked=0",
    ]
    assert lines[3] == "3 lease(s): LIVE 1, LOST 1, RELEASED 1" and len(lines) == 4
    assert _bytes(root) == held
    _damage(root, "garbage", "broken")
    assert cli.main(["--root", str(root), "project", "leases"]) == 3
    out = capsys.readouterr().out
    assert "broken  UNREADABLE" in out and out.splitlines()[-1] == "4 lease(s): LIVE 1, LOST 1, RELEASED 1, UNREADABLE 1"
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "projects").mkdir()
    (bad / "projects" / "hoh-runs").mkdir()
    (bad / "projects" / "hoh-runs" / "run-leases").write_bytes(b"x")
    assert cli.main(["--root", str(bad), "project", "leases"]) == 4
    assert capsys.readouterr().err.startswith("refused: run lease: ")


# --------------------------------------------------------------------------- #
# archive: old ended leases are moved aside, never removed
# --------------------------------------------------------------------------- #

def _fleet(root: Path) -> dict[str, bytes]:
    """Five runs: `old` released 10 days ago with a takeover behind it (2 files), `lapsed` expired 9 days ago, `young`
    released 2 days ago, `live` held, `broken` unreadable. Returns the bytes of every file."""
    _ended(root, "old", released_days=10, takeovers=1)
    _lapsed(root, "lapsed", days=9)
    _ended(root, "young", released_days=2)
    _held(root, "live")
    _damage(root, "garbage", "broken")
    return _bytes(_dir(root))


def test_archive_moves_the_ended_leases_with_their_parked_copies_and_a_manifest_and_removes_nothing(tmp_path):
    """Kills M28 (the manifest after the moves), M29 (current record first), M38. LIVE, young and unreadable leases stay; the union of the live directory and the archive is exactly what there was; the manifest has the digests; COMPLETE closes the pass."""
    root = tmp_path / "runs"
    original = _fleet(root)
    assert sorted(original) == [".lock", "broken.json", "lapsed.json", "live.json", "old.json", "old.json.g1",
                                "young.json"]
    done = archive_ended(_leases(root), older_than_seconds=7 * DAY)
    assert sorted(done.moved) == ["lapsed.json", "old.json", "old.json.g1"] and done.runs == ("lapsed", "old")
    assert done.deferred == () and [run for run, _why in done.skipped] == ["broken"]
    assert done.directory.parent == _archive_dir(root) and done.directory.is_dir()
    live_now = _bytes(_dir(root))
    assert sorted(live_now) == [".lock", "broken.json", "live.json", "young.json"]
    archived = _bytes(done.directory)
    assert sorted(archived) == ["COMPLETE.json", "MANIFEST.json", "lapsed.json", "old.json", "old.json.g1"]
    merged = {**live_now, **{k: v for k, v in archived.items() if k not in ("COMPLETE.json", "MANIFEST.json")}}
    assert merged == original                                               # nothing removed, nothing changed
    manifest = json.loads(archived["MANIFEST.json"])
    assert manifest["schema"] == leaseadmin.ARCHIVE_SCHEMA and manifest["older_than_seconds"] == 7 * DAY
    assert manifest["source"] == str(_dir(root))
    files = {f["name"]: f for f in manifest["files"]}
    assert sorted(files) == ["lapsed.json", "old.json", "old.json.g1"]
    for name, entry in files.items():
        assert entry["sha256"] == hashlib.sha256(original[name]).hexdigest() and entry["size"] == len(original[name])
    assert (files["old.json"]["kind"], files["old.json"]["state"], files["old.json"]["generation"]) == ("current", RELEASED, 2)
    assert (files["old.json.g1"]["kind"], files["old.json.g1"]["generation"]) == ("parked", 1)
    assert files["lapsed.json"]["state"] == LOST
    assert json.loads(archived["COMPLETE.json"]) == {"schema": leaseadmin.ARCHIVE_SCHEMA, "moved": 3}


def test_a_second_archive_pass_is_quiet(tmp_path):
    """Termination (4); kills M33 (a pass with an empty plan takes the lock, hence makes the directory, a manifest and a pass directory). After the pass nothing is eligible, so the next one makes no directory and writes no byte; nothing in the live directory or the archive changes."""
    root = tmp_path / "runs"
    _fleet(root)
    leases = _leases(root)
    archive_ended(leases, older_than_seconds=7 * DAY)
    after = (_bytes(_dir(root)), _bytes(_archive_dir(root)), _names(_archive_dir(root)))
    again = archive_ended(leases, older_than_seconds=7 * DAY)
    assert again.directory is None and again.moved == () and again.runs == () and again.deferred == ()
    assert [run for run, _why in again.skipped] == ["broken"]                    # still said, still left
    assert (_bytes(_dir(root)), _bytes(_archive_dir(root)), _names(_archive_dir(root))) == after
    assert archive_ended(_leases(tmp_path / "empty"), older_than_seconds=7 * DAY).directory is None
    assert not (tmp_path / "empty").exists()


def test_archive_ages_are_bounded_and_the_cutoff_is_inclusive(tmp_path):
    """Kills M26 (the minimum age is not enforced), M25 (`<` for `<=` at the cutoff). A bool, a float, a string and None are not an age."""
    root = tmp_path / "runs"
    leases = _leases(root)
    for bad in (3599, 0, -1, True, 1.5, "7", None, 3650 * DAY + 1):
        with pytest.raises(RunLeaseError) as err:
            archive_ended(leases, older_than_seconds=bad)
        assert err.value.reason == "invalid", bad
    assert not root.exists()
    clock = Clock("2030-01-10T00:00:00Z")
    stamped = _leases(root, clock=clock, **OTHER)
    lease = stamped.acquire("edge", "other-session", ttl_seconds=300)
    clock.now = "2030-01-09T00:00:00Z"                                        # released one day *before* the next clock
    clock.now = "2030-01-10T00:00:01Z"
    stamped.release(lease, reason="run ended")
    clock.now = "2030-01-11T00:00:01Z"                                        # exactly one day after the release
    assert archive_ended(stamped, older_than_seconds=DAY).runs == ("edge",)
    lease = stamped.acquire("edge2", "other-session", ttl_seconds=300)
    clock.now = "2030-01-12T00:00:00Z"
    stamped.release(lease, reason="run ended")
    clock.now = "2030-01-12T23:59:59Z"                                        # a second short of a day
    assert archive_ended(stamped, older_than_seconds=DAY).runs == ()
    assert archive_ended(stamped, older_than_seconds=3600).runs == ("edge2",)


def test_an_archive_that_cannot_write_its_manifest_moves_nothing(tmp_path, monkeypatch):
    """Termination (3); kills M28 (a file is moved before the manifest is durable): a `RunLeaseError`, the live directory byte-for-byte what it was; the empty pass directory is the only thing made."""
    root = tmp_path / "runs"
    _fleet(root)
    before = _bytes(_dir(root))
    real = durable_io.durable_create

    def refuse(path, payload, **kw):
        if Path(path).name == leaseadmin.MANIFEST:
            raise OSError(28, "No space left on device")
        return real(path, payload, **kw)

    monkeypatch.setattr(durable_io, "durable_create", refuse)
    with pytest.raises(RunLeaseError) as err:
        archive_ended(_leases(root), older_than_seconds=7 * DAY)
    assert err.value.reason == "unreadable" and "MANIFEST.json" in str(err.value)
    assert _bytes(_dir(root)) == before
    assert [p.name for p in _archive_dir(root).iterdir()] != [] and not any(_bytes(_archive_dir(root)))


def test_an_archive_whose_complete_record_cannot_be_written_says_what_moved_and_is_not_retried(tmp_path, monkeypatch):
    """Termination (3); kills M39 (no COMPLETE.json, or one under another name): the moves are made and in the pass directory with the manifest; the closing record fails -- the pass raises once, naming the number of files moved, and does not try again; the next pass has nothing left to move."""
    root = tmp_path / "runs"
    _ended(root, "a", released_days=10, takeovers=1)
    original = _bytes(_dir(root))
    real = durable_io.durable_create
    calls: list[str] = []

    def refuse(path, payload, **kw):
        calls.append(Path(path).name)
        if Path(path).name == leaseadmin.COMPLETE:
            raise OSError(28, "No space left on device")
        return real(path, payload, **kw)

    monkeypatch.setattr(durable_io, "durable_create", refuse)
    with pytest.raises(RunLeaseError) as err:
        archive_ended(_leases(root), older_than_seconds=7 * DAY)
    assert "2 file(s) were moved" in str(err.value) and calls.count(leaseadmin.COMPLETE) == 1
    pass_dir = next(_archive_dir(root).iterdir())
    assert sorted(_bytes(pass_dir)) == ["MANIFEST.json", "a.json", "a.json.g1"] and sorted(_bytes(_dir(root))) == [".lock"]
    assert {k: v for k, v in _bytes(pass_dir).items() if k != "MANIFEST.json"} == {
        k: v for k, v in original.items() if k != ".lock"}
    again = archive_ended(_leases(root), older_than_seconds=7 * DAY)
    assert again.directory is None and calls.count(leaseadmin.COMPLETE) == 1


def test_a_file_that_reads_differently_after_its_move_stops_the_pass_and_says_so(tmp_path, monkeypatch):
    """Kills M38 (the read-back against the manifest's digest is gone): a file system that changes a byte on the way is
    caught at the first file, the pass stops there with the count of what had moved, and nothing is retried."""
    root = tmp_path / "runs"
    _ended(root, "a", released_days=10, takeovers=1)
    real, calls = os.rename, []

    def lying(src, dst):
        calls.append(dst)
        real(src, dst)
        Path(dst).write_bytes(Path(dst).read_bytes() + b" ")

    monkeypatch.setattr(leaseadmin.os, "rename", lying)
    with pytest.raises(RunLeaseError) as err:
        archive_ended(_leases(root), older_than_seconds=7 * DAY)
    assert err.value.reason == "conflict" and "reads differently after it was moved" in str(err.value)
    assert "1 file(s) were moved" in str(err.value) and len(calls) == 1
    live = sorted(os.listdir(_dir(root)))
    assert "a.json" in live and "a.json.g1" not in live                 # the current record (the generation) was not moved


def test_an_archive_that_stops_half_way_leaves_every_unfinished_run_with_its_current_record(tmp_path, monkeypatch):
    """Termination (2); kills M29 (the current record moves before the parked copies: a run left with a parked copy and no record would begin again at generation 1 and collide with its own `.g1`). The pass stops at its third rename; the next pass moves the rest into a pass directory of its own and leaves the first alone."""
    root = tmp_path / "runs"
    _ended(root, "a", released_days=10, takeovers=2)           # a.json.g1, a.json.g2, a.json
    _ended(root, "b", released_days=10)
    original = _bytes(_dir(root))
    real, count = os.rename, {"n": 0}

    def flaky(src, dst):
        count["n"] += 1
        if count["n"] == 3:
            raise OSError(5, "Input/output error")
        return real(src, dst)

    monkeypatch.setattr(leaseadmin.os, "rename", flaky)
    with pytest.raises(RunLeaseError) as err:
        archive_ended(_leases(root), older_than_seconds=7 * DAY)
    assert "2 file(s) were moved" in str(err.value)
    live = _bytes(_dir(root))
    assert "a.json" in live and "a.json.g1" not in live and "a.json.g2" not in live and "b.json" in live
    first = next(_archive_dir(root).iterdir())
    assert sorted(_bytes(first)) == ["MANIFEST.json", "a.json.g1", "a.json.g2"]          # no COMPLETE: interrupted
    assert len(json.loads((first / "MANIFEST.json").read_bytes())["files"]) == 4
    first_bytes = _bytes(first)
    monkeypatch.setattr(leaseadmin.os, "rename", real)
    done = archive_ended(_leases(root), older_than_seconds=7 * DAY)
    assert sorted(done.moved) == ["a.json", "b.json"] and done.directory != first
    assert _bytes(first) == first_bytes and sorted(_bytes(_dir(root))) == [".lock"]
    merged = {**_bytes(first), **_bytes(done.directory)}
    assert {k: v for k, v in merged.items() if k not in ("MANIFEST.json", "COMPLETE.json")} == {
        k: v for k, v in original.items() if k != ".lock"}


@pytest.mark.parametrize("how", ["garbage", "fifo", "dangling-link", "directory", "oversize", "another-run",
                                 "another-generation", "second-name"])
def test_an_archive_leaves_a_run_whose_parked_copy_cannot_be_read_exactly(tmp_path, how):
    """Termination (3); kills M30 (an unreadable parked copy is ignored and the rest of the run moves), M31 (a copy of another run or generation is accepted). The whole run stays, current record included, and is said; a run beside it that is fine is archived."""
    root = tmp_path / "runs"
    _ended(root, "bad", released_days=10, takeovers=1)
    _ended(root, "fine", released_days=10)
    _ended(root, "other", released_days=10, takeovers=1)
    directory = _dir(root)
    copy = directory / "bad.json.g1"
    good = copy.read_bytes()
    os.replace(copy, tmp_path / "held-copy")
    if how == "garbage":
        copy.write_bytes(b"garbage\n")
    elif how == "fifo":
        os.mkfifo(copy)
    elif how == "dangling-link":
        copy.symlink_to(tmp_path / "nowhere")
    elif how == "directory":
        copy.mkdir()
    elif how == "oversize":
        copy.write_bytes(b"{" + b" " * 5000 + b"}\n")
    elif how == "another-run":
        copy.write_bytes((directory / "other.json.g1").read_bytes())
    elif how == "another-generation":
        copy.write_bytes(good.replace(b'"generation": 1', b'"generation": 5'))
    elif how == "second-name":
        copy.write_bytes(good)
        os.link(copy, tmp_path / "alias")
    before = _bytes(directory) if how not in ("fifo",) else None
    done = archive_ended(_leases(root), older_than_seconds=7 * DAY)
    assert sorted(run for run, _why in done.skipped) == ["bad"] and sorted(done.runs) == ["fine", "other"]
    live = sorted(os.listdir(directory))
    assert "bad.json" in live and "bad.json.g1" in live and "fine.json" not in live and "other.json" not in live
    assert "other.json.g1" not in live
    if before is not None:
        assert {k: v for k, v in _bytes(directory).items() if k.startswith("bad")} == {
            k: v for k, v in before.items() if k.startswith("bad")}


def test_an_archive_pass_is_bounded_and_says_what_it_left(tmp_path, monkeypatch):
    """Termination (1); kills M32 (a run that does not fit is dropped silently). One pass, at most MAX_ARCHIVE_FILES files; the first run that does not fit is named and ends the scan, a further pass continues; a run with more files than a pass moves is skipped, not moved in part."""
    monkeypatch.setattr(leaseadmin, "MAX_ARCHIVE_FILES", 3)
    root = tmp_path / "runs"
    _ended(root, "a", released_days=10, takeovers=1)           # 2 files
    _ended(root, "b", released_days=10, takeovers=1)           # 2 files
    _ended(root, "c", released_days=10)                        # 1 file
    _ended(root, "d", released_days=10, takeovers=3)           # 4 files: more than a pass moves
    leases = _leases(root)
    first = archive_ended(leases, older_than_seconds=7 * DAY)
    assert first.runs == ("a",) and len(first.moved) == 2 and first.deferred == ("b",) and first.skipped == ()
    second = archive_ended(leases, older_than_seconds=7 * DAY)
    assert second.runs == ("b", "c") and len(second.moved) == 3 and second.deferred == () and second.skipped == ()
    third = archive_ended(leases, older_than_seconds=7 * DAY)
    assert third.directory is None and [run for run, _w in third.skipped] == ["d"] and "at most 3" in third.skipped[0][1]
    assert "d.json" in _bytes(_dir(root)) and "d.json.g1" in _bytes(_dir(root))          # untouched, parked copies too
    fourth = archive_ended(leases, older_than_seconds=7 * DAY)
    assert fourth.directory is None and fourth.skipped == third.skipped


def test_an_archive_is_serialised_with_the_protocol_and_a_driven_again_run_begins_cleanly(tmp_path):
    """Kills M27 (the pass does not take the short lock). While another process holds the lock the pass is `busy` and moves nothing. Afterwards the run can be driven again: generation 1, and the next takeover parks `.g1` without a collision; the old grant's holder is fenced."""
    root = tmp_path / "runs"
    old = _ended(root, "r1", released_days=10, takeovers=1)
    before = _bytes(_dir(root))
    holder = _leases(root, **OTHER)
    with holder._locked():
        with pytest.raises(RunLeaseBusy):
            archive_ended(_leases(root, lock_wait_seconds=0, **OTHER), older_than_seconds=7 * DAY)
    assert _bytes(_dir(root)) == before
    archive_ended(_leases(root), older_than_seconds=7 * DAY)
    assert sorted(_bytes(_dir(root))) == [".lock"]
    with pytest.raises(StaleRunLease):
        _leases(root, **OTHER).renew(old)
    with pytest.raises(StaleRunLease):
        _leases(root, **OTHER).release(old, reason="x")
    again = _leases(root, **OTHER).acquire("r1", "other-session", ttl_seconds=300)
    assert again.generation == 1
    _leases(root, **OTHER).release(again, reason="run ended")
    second = _leases(root, **OTHER).acquire("r1", "other-session", ttl_seconds=300)
    assert second.generation == 2 and (_dir(root) / "r1.json.g1").is_file()


def test_hoh_project_leases_archive_moves_what_ended_long_enough_ago(tmp_path, capsys):
    """Kills M35 (`rundrive.archive` ignores its age), M36 (`--older-than-days` in hours). Through `cli.main`, the real parser: 20 days -> nothing; the default (7) -> the three files of the two old runs; 1 -> the two-day-old one; 0, 3651, -3, x -> exit 2; a lease directory that is a file -> exit 4."""
    root = tmp_path / "runs"
    _fleet(root)
    before = _bytes(_dir(root))
    assert cli.main(["--root", str(root), "project", "leases"]) == 3
    capsys.readouterr()
    assert _bytes(_dir(root)) == before and not _archive_dir(root).exists()      # listing moves nothing
    assert cli.main(["--root", str(root), "project", "leases", "--archive", "--older-than-days", "20"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "nothing to archive: no lease ended 20 day(s) ago or more"
    assert "skipped broken: " in out and _bytes(_dir(root)) == before
    assert cli.main(["--root", str(root), "project", "leases", "--archive"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("archived 3 file(s) of 2 run(s) to ") and str(_archive_dir(root)) in out[0]
    assert sorted(_bytes(_dir(root))) == [".lock", "broken.json", "live.json", "young.json"]
    assert cli.main(["--root", str(root), "project", "leases", "--archive", "--older-than-days", "1"]) == 0
    assert capsys.readouterr().out.startswith("archived 1 file(s) of 1 run(s) to ")          # `young`, two days old
    assert sorted(_bytes(_dir(root))) == [".lock", "broken.json", "live.json"]
    for bad in ("0", "3651", "-3", "x"):
        with pytest.raises(SystemExit) as exit_:
            cli.main(["--root", str(root), "project", "leases", "--archive", "--older-than-days", bad])
        assert exit_.value.code == 2
    capsys.readouterr()
    blocked = tmp_path / "blocked"
    (blocked / "projects" / "hoh-runs").mkdir(parents=True)
    (blocked / "projects" / "hoh-runs" / "run-leases").write_bytes(b"x")
    assert cli.main(["--root", str(blocked), "project", "leases", "--archive"]) == 4
    assert capsys.readouterr().err.startswith("refused: ")


def test_hoh_project_leases_is_one_command_among_the_project_commands(tmp_path, capsys):
    """The parser keeps its other commands: `project list` and `project resume` still run, and `project leases` takes no
    project id (a project id is refused as an unknown argument)."""
    root = tmp_path / "runs"
    _project(root, TaskNode(id="a"))
    capsys.readouterr()
    assert cli.main(["--root", str(root), "project", "list"]) == 0
    assert capsys.readouterr().out.startswith("p  resume ")
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--root", str(root), "project", "leases", "p"])
    assert exit_.value.code == 2


def test_the_stated_bounds_are_the_ones_in_force():
    """Kills M34 (`MAX_ARCHIVE_FILES` moved): the numbers the spec states are the numbers in the code, and the listing's ceiling is the broker's."""
    from hoh import capacitycheck
    assert leaseadmin.MAX_SNAPSHOT == 16384 == capacitycheck.MAX_SCANNED
    assert leaseadmin.MIN_ARCHIVE_AGE_SECONDS == 3600 and leaseadmin.MAX_ARCHIVE_AGE_SECONDS == 3650 * DAY
    assert leaseadmin.MAX_ARCHIVE_FILES == 512 and leaseadmin.ARCHIVE_SUFFIX == "-archive"
    assert (leaseadmin.MANIFEST, leaseadmin.COMPLETE) == ("MANIFEST.json", "COMPLETE.json")
    assert runlease.MAX_EPOCH == 10 ** 9 and runlease.SCHEMA == "veriharness.run-lease/1"
    assert launcher_module_refusal_prefix() == "run lease refused ("


def launcher_module_refusal_prefix() -> str:
    from hoh import launcher
    return launcher._LEASE_REFUSAL


# --------------------------------------------------------------------------- #
# revision 1: a remote holder is alive by its ttl; the archive works above the listing's ceiling
# --------------------------------------------------------------------------- #

def test_a_holder_in_another_host_is_alive_by_its_ttl_whatever_its_boot_id(tmp_path):
    """Review B1; kills M43 (the boot check is not limited to this host). Another machine has another boot id, so a
    lease whose holder is in another host and whose boot id differs from the reader's is LIVE until its ttl runs out
    -- in `judge`, `consult` and the launcher -- while a holder in *this* host on another boot is LOST (the machine
    restarted)."""
    root = tmp_path / "runs"
    _run_state(root)
    remote = _held(root, "r1", epoch=2)
    assert remote.holder_boot == "boot-of-the-other-machine" and remote.holder_host == "other-host"
    reader = _leases(root)
    assert reader.boot() != remote.holder_boot and reader.host != remote.holder_host
    assert reader.judge(remote)[0] == LIVE
    fact = rundrive.consult(root, "r1")
    assert fact.state == "LIVE" and not fact.reclaimable
    assert HohRunLauncher(root, tmp_path).evaluate(TaskNode(id="n", run_id="r1")).verdict is RunVerdict.IN_FLIGHT
    expired = _leases(root, clock=Clock(_stamp(days=1)), **OTHER).acquire("r2", "other-session", ttl_seconds=300)
    assert reader.judge(expired)[0] == LOST and "expired" in reader.judge(expired)[1]       # its ttl decides, both ways
    here = RunLeases(tmp_path / "here", boot=lambda: "the-boot-before-the-restart", pid=os.getpid()).acquire(
        "r3", "this-host-session", ttl_seconds=3600)
    state, why = RunLeases(tmp_path / "here").judge(here)
    assert state == LOST and "the machine restarted" in why


def _synthetic_directory(directory: Path, count: int) -> None:
    """`count` lease records of long-released runs `r00000`... (valid, canonical, written without the protocol)."""
    directory.mkdir(parents=True)
    template = RunLease(run_id="r00000", holder="h1", generation=1, ttl_seconds=300, acquired_at="2020-01-01T00:00:00Z",
                        renewed_at="2020-01-01T00:00:00Z", expires_at="2020-01-01T00:05:00Z", holder_pid=100,
                        holder_boot="b", holder_host="other-host", holder_pidns="ns", holder_start="s",
                        released_at="2020-01-01T00:01:00Z", released_reason="run ended")
    for index in range(count):
        (directory / f"r{index:05d}.json").write_bytes(render(replace(template, run_id=f"r{index:05d}")))


def test_an_archive_works_above_the_listing_ceiling_and_holds_the_lock_for_its_budget_only(tmp_path, monkeypatch):
    """Review B2 and A M1; kills M44 (the plan is not re-checked under the lock), M45 (the whole directory is read under
    the lock), M46 (the archive refuses above `MAX_SNAPSHOT`). 16,385 lease files: the listing refuses (the state the
    archive is for), a pass moves exactly its 512-file budget, reads 512 records without the lock and the same 512 under it
    (not 16,385), the listing works again, and the next pass continues where the first stopped."""
    root = tmp_path / "runs"
    count = leaseadmin.MAX_SNAPSHOT + 1
    _synthetic_directory(_dir(root), count)
    leases = _leases(root)
    with pytest.raises(RunLeaseError) as err:
        snapshot(leases)
    assert err.value.reason == "invalid" and "archive the ended ones" in str(err.value)
    seen = {"locked": False, "inside": 0, "all": 0}
    real_locked, real_read = RunLeases._locked, RunLeases.read

    def locked(self):
        manager = real_locked(self)

        class Wrapped:
            def __enter__(inner):
                manager.__enter__()
                seen["locked"] = True

            def __exit__(inner, *exc):
                seen["locked"] = False
                return manager.__exit__(*exc)

        return Wrapped()

    def counting(self, run_id):
        seen["all"] += 1
        seen["inside"] += seen["locked"]
        return real_read(self, run_id)

    monkeypatch.setattr(RunLeases, "_locked", locked)
    monkeypatch.setattr(RunLeases, "read", counting)
    first = archive_ended(leases, older_than_seconds=7 * DAY)
    assert len(first.moved) == leaseadmin.MAX_ARCHIVE_FILES == 512 and first.deferred == () and first.skipped == ()
    assert first.runs == tuple(f"r{index:05d}" for index in range(512))
    assert seen["inside"] == 512 and seen["all"] == 1024                 # the plan 512 + the re-check 512, not 16,385
    monkeypatch.undo()
    assert len(snapshot(leases)) == count - 512                          # below the ceiling the listing works again
    second = archive_ended(leases, older_than_seconds=7 * DAY)
    assert second.runs == tuple(f"r{index:05d}" for index in range(512, 1024)) and second.directory != first.directory
    assert len(os.listdir(_dir(root))) == count - 1024 + 1               # the records that are left and the lock file


def test_a_run_taken_over_between_the_plan_and_the_lock_is_not_archived(tmp_path, monkeypatch):
    """Review B2 (the plan is read without the lock); kills M44 (the plan is not re-checked under the lock): `a` is planned
    as ended, then taken over by a live holder before the pass has the lock -- it is skipped, never moved; `b` is archived."""
    root = tmp_path / "runs"
    _ended(root, "a", released_days=10)
    _ended(root, "b", released_days=10)
    real_locked = RunLeases._locked
    done = {"taken": False}

    def locked(self):
        if not done["taken"]:
            done["taken"] = True
            _leases(root, **OTHER).acquire("a", "new-session", ttl_seconds=3600)
        return real_locked(self)

    monkeypatch.setattr(RunLeases, "_locked", locked)
    result = archive_ended(_leases(root), older_than_seconds=7 * DAY)
    assert result.runs == ("b",) and [run for run, _why in result.skipped] == ["a"]
    assert "no longer eligible" in result.skipped[0][1]
    live = _bytes(_dir(root))
    assert "a.json" in live and "b.json" not in live and _leases(root).read("a").generation == 2


def test_a_pass_whose_whole_plan_changed_before_the_lock_makes_no_directory(tmp_path, monkeypatch):
    """Termination (4); kills M40 (the pass goes on with an empty plan after the re-check): the only planned run is taken
    over before the pass has the lock, so nothing is moved, no pass directory or manifest is made, and the run is said."""
    root = tmp_path / "runs"
    _ended(root, "a", released_days=10)
    real_locked = RunLeases._locked
    done = {"taken": False}

    def locked(self):
        if not done["taken"]:
            done["taken"] = True
            _leases(root, **OTHER).acquire("a", "new-session", ttl_seconds=3600)
        return real_locked(self)

    monkeypatch.setattr(RunLeases, "_locked", locked)
    result = archive_ended(_leases(root), older_than_seconds=7 * DAY)
    assert result.directory is None and result.moved == () and [run for run, _why in result.skipped] == ["a"]
    assert not _archive_dir(root).exists() and [n for n in _names(root) if "archive" in n] == []


def test_the_register_probes_the_readers_of_the_run_lease():
    """Review B1 (testability); kills M47 (the register edit is forgotten): the P3-18 row's probe is the reader
    `rundrive.consult` with this file's probe node and no `equivalent`; slice 2's probe is kept by addition."""
    import json as _json
    row = next(r for r in _json.loads((Path(__file__).resolve().parent.parent / "program" / "v3_3" /
                                       "REQUIREMENTS.json").read_text())["requirements"] if r["id"] == "P3-18")
    probe = row["probe"]
    assert (probe["kind"], probe["symbol"], probe["module"]) == ("symbol", "consult", "rundrive.py")
    assert "equivalent" not in probe
    prefix, node = probe["test"].split("::")
    assert prefix == "tests/test_run_lease_consult.py" and node in globals()
    before = row["probe_before_p318s3"]
    assert before["symbol"] == "drive_lease" and "equivalent" in before and before["replaced_on"] == "2026-10-19"


@pytest.mark.parametrize("error", [OSError(5, "Input/output error"), ValueError("embedded null byte")])
def test_a_runs_root_the_store_refuses_is_unreadable_not_free(tmp_path, monkeypatch, error):
    """Review B3; kills M48 (the except set is narrowed to the lease's own error): an `OSError` or a `ValueError` out of
    the store/file system while the lease directory is built is UNREADABLE, never NONE."""
    def refuse(root, **environment):
        raise error

    monkeypatch.setattr(rundrive, "_leases", refuse)
    fact = rundrive.consult(tmp_path, "r1")
    assert fact.state == "UNREADABLE" and fact.why.startswith("unreadable: ") and not fact.reclaimable
