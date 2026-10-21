"""P3-18 slice 2: `hoh run` holds the lease of the run it drives, through the product's own command surface.

Slice 1 (`tests/test_runlease.py`) made a per-run fenced lease record; nothing in production took one. These tests drive
the real parser and the real `cmd_run` / `_run_in` with `--no-herdr` (the real dispatcher is built, no agent runs, nothing
is written outside `tmp_path`); the iteration itself is a stand-in whose body the test decides, so the lease can be read
*while* the run is being driven. A lease another holder has is written with `RunLeases(host="other-host", ...)`: a holder
in another host is LIVE until its expiry whatever its pid says, which is the one case the process lock cannot see.

Mutant ids (M1..) are the spec's table; each test says which it kills.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from test_failure_classes import build  # noqa: F401
from test_failure_classes import repo as repo  # a fixture; the alias form is an explicit re-export
from test_governance import _initialised_storage_root

from hoh import capacitycheck, cli, durable_io, rundrive
from hoh import controller as controller_mod
from hoh.projectstore import ProjectStore
from hoh.runlease import LIVE, LOST, RELEASED, RunLeases, StaleRunLease, render
from hoh.store import RunStore

PAST = "2020-01-01T00:00:00Z"
MARKER_FILES = {".process.lock", "process.json", ".lock"}


def _tree(path: Path, *, skip: set[str] = frozenset()) -> dict[str, int]:
    """Every file under `path` (names and sizes), for "nothing was written" comparisons."""
    return {str(p.relative_to(path)): p.lstat().st_size for p in sorted(path.rglob("*"))
            if p.is_file() and p.name not in skip}


def _bytes(path: Path) -> dict[str, bytes]:
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def _threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name == rundrive.THREAD_NAME and t.is_alive()]


def _wait(condition, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return bool(condition())


class World:
    """One run `r1` in `tmp_path/runs`, a storage root, and a stand-in iteration the test decides."""

    def __init__(self, tmp_path, repo, monkeypatch, capsys):
        _initialised_storage_root(tmp_path, monkeypatch)
        _, self.state, self.store, _ = build(tmp_path, repo)
        self.tmp = tmp_path
        self.repo = repo
        self.root = str(tmp_path / "runs")
        self.capsys = capsys
        self.monkeypatch = monkeypatch
        self.iterations = 0
        self.controllers = 0
        self.hook = lambda n, state: None
        world = self
        real_controller = controller_mod.Controller

        class StandIn(real_controller):
            def __init__(inner, *a, **kw):
                world.controllers += 1
                super().__init__(*a, **kw)

            def run_iteration(inner, state):
                world.iterations += 1
                world.hook(world.iterations, state)
                return types.SimpleNamespace(iteration=world.iterations, accepted=False,
                                             reason="stand-in", waiting_for_approval=False)

        monkeypatch.setattr(controller_mod, "Controller", StandIn)
        monkeypatch.setattr(cli, "_register_trust", lambda *a, **kw: None)

    @property
    def leases(self) -> RunLeases:
        return ProjectStore(self.root, rundrive.LEASE_PROJECT).run_leases()

    @property
    def lease_dir(self) -> Path:
        return Path(self.root) / "projects" / rundrive.LEASE_PROJECT / "run-leases"

    def run(self, *extra, iterations: int = 1, command: str = "run", run_id: str = "r1"):
        argv = ["--root", self.root, command, run_id]
        if command == "run":
            argv += ["--iterations", str(iterations), "--no-herdr"]
        rc = cli.main([*argv, *extra])
        got = self.capsys.readouterr()
        return rc, got.out, got.err

    def take_as_other(self, *, ttl: int = 3600, **environment):
        """`(leases, lease)`: the lease of `r1` as a holder in another host took it."""
        env = dict(host="other-host", pid=4242, pidns=lambda: "pidns:[999]", start=lambda pid: "s1")
        env.update(environment)
        leases = RunLeases(self.lease_dir, **env)
        return leases, leases.acquire("r1", "other-session", ttl_seconds=ttl)


@pytest.fixture
def world(tmp_path, repo, monkeypatch, capsys):
    return World(tmp_path, repo, monkeypatch, capsys)


# --------------------------------------------------------------------------- #
# taken before the run writes, live for the whole iteration, released at the end
# --------------------------------------------------------------------------- #

def test_hoh_run_holds_a_live_run_lease_for_the_whole_iteration_and_releases_it_at_the_end(world, monkeypatch):
    """Kills M1 (no lease is taken), M4 (never released) and, with the entry spy, a lease taken after the
    run body began. The broker's session count (`capacitycheck.live_holds`) is 1 during the iteration, 0 after."""
    seen: dict = {}
    before = _tree(world.store.dir, skip=MARKER_FILES)
    real_run_in = cli._run_in

    def spy_run_in(args, scope):
        lease = world.leases.read("r1")
        seen["at_entry"] = (lease is not None and world.leases.judge(lease)[0],
                            _tree(world.store.dir, skip=MARKER_FILES) == before)
        return real_run_in(args, scope)

    monkeypatch.setattr(cli, "_run_in", spy_run_in)

    def inside(n, state):
        lease = world.leases.read("r1")
        seen["lease"] = lease
        seen["state"] = world.leases.judge(lease)[0]
        seen["holds"] = capacitycheck.live_holds(world.root)
        seen["threads"] = _threads()

    world.hook = inside
    assert not world.lease_dir.exists()
    rc, _out, err = world.run()
    assert rc == 0, err
    assert seen["at_entry"] == (LIVE, True)      # held before `_run_in` wrote a byte of the run
    lease = seen["lease"]
    assert lease.holder == f"hoh-run.{os.getpid()}" and lease.generation == 1
    assert lease.ttl_seconds == 300 and lease.released_at is None and seen["state"] == LIVE
    assert (seen["holds"].sessions, seen["holds"].projects, seen["holds"].lease_files) == (1, 1, 1)
    assert seen["threads"] == [rundrive.THREAD_NAME]
    after = world.leases.read("r1")
    assert after.generation == 1 and after.released_at is not None and after.released_reason == "run ended"
    assert world.leases.judge(after)[0] == RELEASED
    assert capacitycheck.live_holds(world.root).sessions == 0
    assert _threads() == []


def test_a_run_refused_by_the_process_lock_takes_no_lease(world):
    """Kills M10 (the lease is taken before the process lock): a refused second driver on this host leaves
    no lease directory at all, and says what it always said (the words the launcher reads as IN_FLIGHT)."""
    with RunStore(world.root, "r1").process_lock():
        rc, _out, err = world.run()
    assert rc == 2 and err.startswith("Run r1 is already being executed by another process")
    assert not (Path(world.root) / "projects").exists()
    assert world.controllers == 0


# --------------------------------------------------------------------------- #
# another holder: refused by name; a lease that ended is taken over only by the fenced takeover
# --------------------------------------------------------------------------- #

def test_a_second_driver_is_refused_by_name_while_another_holder_has_a_live_lease(world):
    """Kills M2 (a live lease of another holder is ignored). Nothing is dispatched, no byte of the run or of
    the lease changes, no parked copy appears, and the words begin as the process lock's refusal does (the
    launcher compares `Run <id> is already being executed` and reads the run as in flight)."""
    _leases, _lease = world.take_as_other()
    state_before = world.store.state_path.read_bytes()
    lease_dir_before = _bytes(world.lease_dir)
    run_tree = _tree(world.store.dir, skip=MARKER_FILES)
    rc, _out, err = world.run()
    assert rc == 2
    assert err.startswith("Run r1 is already being executed")
    assert "other-session" in err and "generation 1" in err and "pid 4242" in err
    assert "no run state was written" in err
    assert world.controllers == 0 and world.iterations == 0
    assert world.store.state_path.read_bytes() == state_before
    assert _bytes(world.lease_dir) == lease_dir_before
    assert _tree(world.store.dir, skip=MARKER_FILES) == run_tree
    assert _threads() == []


@pytest.mark.parametrize("how", ["expired", "dead-pid"])
def test_a_lease_that_ended_is_taken_over_through_the_fenced_takeover(world, how):
    """Kills M2 on the other side (every existing lease counts as held) and a takeover that writes over the
    record: the next generation begins, the replaced record is parked byte for byte, and the old holder's own
    handle is fenced."""
    if how == "expired":
        old_leases, old = world.take_as_other(ttl=60, clock=lambda: PAST)
    else:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        base = RunLeases(world.lease_dir)
        old_leases = RunLeases(world.lease_dir, pid=child.pid, start=lambda pid: "s1")
        old = old_leases.acquire("r1", "other-session", ttl_seconds=3600)
        assert base.judge(old)[0] == LOST        # the holder's process is gone: lost long before its ttl
    old_bytes = (world.lease_dir / "r1.json").read_bytes()
    seen = {}

    def inside(n, state):
        seen["lease"] = world.leases.read("r1")

    world.hook = inside
    rc, _out, err = world.run()
    assert rc == 0, err
    assert seen["lease"].generation == 2 and seen["lease"].holder == f"hoh-run.{os.getpid()}"
    assert (world.lease_dir / "r1.json.g1").read_bytes() == old_bytes
    with pytest.raises(StaleRunLease):
        old_leases.renew(old)
    assert world.leases.read("r1").released_at is not None


# --------------------------------------------------------------------------- #
# the keepalive: renews while the run is driven, stops with it, is bounded
# --------------------------------------------------------------------------- #

def test_the_keepalive_renews_the_lease_while_the_iteration_runs_and_is_silent_after_it(world, monkeypatch):
    """Kills M5 (no thread), M6 (the thread outlives the run: it would renew a released lease)."""
    monkeypatch.setattr(rundrive, "keepalive_period", lambda ttl: 0.02)
    renewals: list[str] = []
    real_renew = RunLeases.renew

    def counting(self, lease):
        renewals.append(lease.run_id)
        return real_renew(self, lease)

    monkeypatch.setattr(RunLeases, "renew", counting)
    seen = {}

    def inside(n, state):
        seen["renewed"] = _wait(lambda: len(renewals) >= 3)

    world.hook = inside
    rc, _out, err = world.run()
    assert rc == 0, err
    assert seen["renewed"] and set(renewals) == {"r1"}
    count, record = len(renewals), _bytes(world.lease_dir)
    time.sleep(0.3)                               # fifteen periods
    assert len(renewals) == count and _bytes(world.lease_dir) == record
    assert _threads() == []


class FakeLeases:
    """The three calls the handle makes. `outcomes` are the answers of successive renewals: an exception is
    raised, anything else is a success; when they run out every renewal succeeds."""

    pid = 4242

    def __init__(self, outcomes=(), *, always=None, block=None):
        self.outcomes = list(outcomes)
        self.always = always
        self.block = block
        self.renews = 0
        self.released: list[str] = []

    def acquire(self, name, holder, *, ttl_seconds):
        return types.SimpleNamespace(run_id=name, holder=holder, ttl_seconds=ttl_seconds, expires_at="later")

    def renew(self, lease):
        self.renews += 1
        if self.block is not None:
            self.block.wait(10)
        outcome = self.outcomes.pop(0) if self.outcomes else self.always
        if isinstance(outcome, BaseException):
            raise outcome
        return lease

    def release(self, lease, *, reason):
        self.released.append(reason)
        return lease


def test_the_keepalive_gives_up_at_the_third_failure_in_a_row_and_a_success_resets_the_count(tmp_path, monkeypatch):
    """Kills M7 (the bound 3 -> 4, or a count that is not reset by a success): exactly three attempts, then the
    thread is gone and `lost` says why; two failures, a success, two failures never reach the bound."""
    monkeypatch.setattr(rundrive, "keepalive_period", lambda ttl: 0.01)
    failing = FakeLeases(always=OSError("disk"))
    with rundrive.drive_lease(tmp_path, "r1", leases=failing) as drive:
        assert _wait(lambda: not drive._thread.is_alive())
        assert failing.renews == rundrive.MAX_RENEW_FAILURES == 3
        assert "3 renewals in a row failed" in drive.lost and "OSError: disk" in drive.lost
    assert failing.released == ["run ended"]
    flaky = FakeLeases([OSError("a"), OSError("b"), None] * 3)
    with rundrive.drive_lease(tmp_path, "r1", leases=flaky) as drive:
        assert _wait(lambda: flaky.renews >= 12)
        assert drive.lost is None and drive._thread.is_alive()
    assert not drive._thread.is_alive()


def test_the_keepalive_ends_at_the_first_refusal_of_a_renewal(tmp_path, monkeypatch):
    """Kills M7b (a fenced holder keeps renewing): one attempt, `lost` names the refusal."""
    monkeypatch.setattr(rundrive, "keepalive_period", lambda ttl: 0.01)
    fenced = FakeLeases([StaleRunLease("taken by another")])
    with rundrive.drive_lease(tmp_path, "r1", leases=fenced) as drive:
        assert _wait(lambda: not drive._thread.is_alive())
        assert fenced.renews == 1 and "not this run's any more" in drive.lost


def test_the_end_of_a_run_waits_at_most_the_join_bound_for_a_renewal_that_hangs(tmp_path, monkeypatch):
    """Kills M12b (no join bound): a renewal stuck in the file system does not hold the end of the run."""
    monkeypatch.setattr(rundrive, "keepalive_period", lambda ttl: 0.01)
    monkeypatch.setattr(rundrive, "JOIN_SECONDS", 0.2)
    release = threading.Event()
    stuck = FakeLeases(block=release)
    started = time.monotonic()
    try:
        with rundrive.drive_lease(tmp_path, "r1", leases=stuck):
            assert _wait(lambda: stuck.renews >= 1)
        assert time.monotonic() - started < 3.0
        assert stuck.released == ["run ended"]
    finally:
        release.set()


def test_a_lost_lease_starts_no_further_iteration_and_ends_the_command_with_2(world, monkeypatch):
    """Kills M8 (the loop does not look at `lost`): three iterations were asked for, one runs."""
    monkeypatch.setattr(rundrive, "keepalive_period", lambda ttl: 0.02)

    refusing = threading.Event()
    real_renew = RunLeases.renew

    def refuse(self, lease):
        if refusing.is_set():
            raise OSError("the file system refuses")
        return real_renew(self, lease)

    monkeypatch.setattr(RunLeases, "renew", refuse)

    def inside(n, state):
        refusing.set()                            # from the first iteration on, no renewal is written
        _wait(lambda: _threads() == [])

    world.hook = inside
    rc, _out, err = world.run(iterations=3)
    assert rc == 2 and world.iterations == 1
    assert "run lease lost: 3 renewals in a row failed" in err and "no further iteration is started" in err
    assert world.leases.read("r1").released_at is not None


# --------------------------------------------------------------------------- #
# every way out releases
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("how", ["refusal", "iteration-raises", "unwinds"])
def test_the_lease_is_released_on_every_way_out(world, monkeypatch, how):
    """Kills M4b (released on the happy path only): a refused invocation, an iteration that raises (exit 1) and
    an exception that unwinds the command each leave a RELEASED lease and no thread."""
    reason = "run ended"
    if how == "refusal":
        rc, _out, err = world.run("--routing-policy", str(world.tmp / "nowhere.json"))
        assert rc == 2 and "routing policy refused" in err
    elif how == "iteration-raises":
        def boom(n, state):
            raise RuntimeError("the iteration broke")

        world.hook = boom
        rc, _out, err = world.run()
        assert rc == 1 and "Iteration aborted and run blocked" in err
    else:
        def unwinds(args):
            raise KeyError("unexpected")

        monkeypatch.setattr(cli, "_cmd_run", unwinds)
        with pytest.raises(KeyError):
            world.run()
        reason = "run raised an exception"
    lease = world.leases.read("r1")
    assert lease.released_at is not None and lease.released_reason == reason
    assert world.leases.judge(lease)[0] == RELEASED
    assert _threads() == []


def test_a_finished_lease_is_quiet_a_resume_and_a_read_write_nothing_to_it(world, monkeypatch):
    """Terminal records are quiet: after the run, `hoh resume`, the broker's read and several periods leave the
    lease directory byte for byte as the release left it."""
    monkeypatch.setattr(rundrive, "keepalive_period", lambda ttl: 0.02)
    assert world.run()[0] == 0
    record = _bytes(world.lease_dir)
    assert world.run(command="resume")[0] == 0
    assert capacitycheck.live_holds(world.root).sessions == 0
    time.sleep(0.2)
    assert _bytes(world.lease_dir) == record
    assert sorted(record) == [".lock", "r1.json"]


# --------------------------------------------------------------------------- #
# hoh resume
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("force", [False, True])
def test_hoh_resume_is_refused_while_another_holder_has_a_live_lease_force_or_not(world, force):
    """Kills M9 (the check is skipped, or sits behind `--force`). The control: no lease, and a released one,
    resume as before -- and a resume with no lease creates no lease directory."""
    state_before = world.store.state_path.read_bytes()
    assert world.run(command="resume")[0] == 0 and not (Path(world.root) / "projects").exists()
    world.store.state_path.write_bytes(state_before)
    others, held = world.take_as_other()
    rc, _out, err = world.run(*(["--force"] if force else []), command="resume")
    assert rc == 2 and err.startswith("Run r1 is already being executed")
    assert "other-session" in err and "'hoh resume' does not touch" in err
    assert world.store.state_path.read_bytes() == state_before
    others.release(held, reason="gave up")
    assert world.run(command="resume")[0] == 0


# --------------------------------------------------------------------------- #
# fail closed: a lease record that cannot be read is not a free run
# --------------------------------------------------------------------------- #

def _damage(world, how: str) -> Path:
    path = world.lease_dir / "r1.json"
    if how not in ("noncanonical", "second-name"):
        world.lease_dir.mkdir(parents=True)
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
    elif how == "noncanonical":
        path.write_bytes(render(world.take_as_other()[1]) + b"\n")
    elif how == "second-name":
        path.write_bytes(render(world.take_as_other()[1]))
        os.link(path, world.lease_dir / "alias.json")
    elif how == "directory":
        path.mkdir()
    elif how == "dangling-link":
        path.symlink_to(world.tmp / "nowhere")
    elif how == "fifo":
        os.mkfifo(path)
    elif how == "no-permission":
        if os.geteuid() == 0:
            pytest.skip("root reads a mode-000 file")
        path.write_bytes(b"{}\n")
        path.chmod(0)
    return path


@pytest.mark.parametrize("how", ["garbage", "not-an-object", "oversize", "utf-16", "nul", "noncanonical",
                                 "second-name", "directory", "dangling-link", "fifo", "no-permission"])
def test_a_lease_record_that_cannot_be_read_refuses_run_and_resume(world, how):
    """Kills M3 (a damaged record is read as free) and the resume twin (`refusal_for_resume` answers None on
    an error). Nothing is dispatched or written, the damaged entry stays as it was."""
    path = _damage(world, how)
    kind = (path.is_symlink(), path.is_dir(), path.exists() and path.stat().st_mode)
    state_before = world.store.state_path.read_bytes()
    rc, _out, err = world.run()
    assert rc == 2 and "run lease refused (" in err and "no run state was written" in err
    assert world.controllers == 0
    rc, _out, err = world.run(command="resume")
    assert rc == 2 and "run lease refused (" in err
    assert world.store.state_path.read_bytes() == state_before
    assert (path.is_symlink(), path.is_dir(), path.exists() and path.stat().st_mode) == kind
    path.chmod(0o600) if how == "no-permission" else None


@pytest.mark.parametrize("how", ["file", "link-to-a-directory"])
def test_a_lease_directory_that_is_not_a_directory_of_its_own_refuses_the_run(world, how):
    """The directory the leases live in is a regular file, or a link to a real directory: unreadable, refused,
    nothing dispatched."""
    world.lease_dir.parent.mkdir(parents=True)
    if how == "file":
        world.lease_dir.write_bytes(b"not a directory\n")
    else:
        (world.tmp / "elsewhere").mkdir()
        world.lease_dir.symlink_to(world.tmp / "elsewhere")
    rc, _out, err = world.run()
    assert rc == 2 and "run lease refused (unreadable)" in err and world.controllers == 0
    if how != "file":
        assert not list((world.tmp / "elsewhere").iterdir())      # nothing was made behind the link


def test_a_release_that_cannot_be_written_is_said_once_and_does_not_mask_the_way_out(tmp_path, capsys):
    """Kills M18 (a failing release raises out of `close`): the run's own outcome stands, the stderr line says
    when the lease lapses, and nothing is retried (one release call)."""
    class Failing(FakeLeases):
        def release(self, lease, *, reason):
            self.released.append(reason)
            raise OSError("no space left")

    leases = Failing()
    with pytest.raises(KeyError):
        with rundrive.drive_lease(tmp_path, "r1", leases=leases):
            raise KeyError("the run's own error")
    err = capsys.readouterr().err
    assert leases.released == ["run raised an exception"]
    assert "not released (no space left); it lapses at later" in err and err.count("not released") == 1


# --------------------------------------------------------------------------- #
# names and bounds
# --------------------------------------------------------------------------- #

def test_the_lease_of_a_run_id_the_lease_grammar_refuses_is_kept_under_a_stable_hashed_name(tmp_path):
    """Kills M11 (`lease_name` returns the id): an 80-character id and an id starting with `-` are legal run ids
    and run under `run-<32 hex>`; two such ids never share a lease; a plain id is kept."""
    assert rundrive.lease_name("r1") == "r1"
    long_id, dashed = "x" * 80, "-leading-dash"
    for run_id in (long_id, dashed):
        name = rundrive.lease_name(run_id)
        assert name.startswith("run-") and len(name) == 36 and name == rundrive.lease_name(run_id)
        with rundrive.drive_lease(tmp_path, run_id) as drive:
            assert drive.lease.run_id == name
            assert ProjectStore(tmp_path, rundrive.LEASE_PROJECT).run_leases().read(name).holder.startswith("hoh-run.")
    assert rundrive.lease_name("y" * 80) != rundrive.lease_name(long_id)


def test_the_stated_bounds_are_the_ones_in_force(tmp_path):
    """Kills M12 (ttl 300 -> 301, period ttl/3 -> ttl/2, the bounds of the keepalive, the namespace)."""
    assert (rundrive.LEASE_TTL, rundrive.MAX_RENEW_FAILURES, rundrive.JOIN_SECONDS) == (300, 3, 5.0)
    assert rundrive.LEASE_PROJECT == "hoh-runs"
    with rundrive.drive_lease(tmp_path, "r1") as drive:
        assert drive.lease.ttl_seconds == 300 and drive.period == 100.0
        assert drive.lease.expires_at > drive.lease.renewed_at
    assert (tmp_path / "projects" / "hoh-runs" / "run-leases" / "r1.json").is_file()


def test_hoh_start_takes_no_lease(world, repo):
    """`hoh start` writes the run's state and drives nothing: it creates no lease directory."""
    spec = world.tmp / "spec2.md"
    spec.write_text("# s\n", encoding="utf-8")
    rc = cli.main(["--root", world.root, "start", "--repo", str(repo), "--spec", str(spec), "--run-id", "r2"])
    world.capsys.readouterr()
    assert rc == 0 and RunStore(world.root, "r2").state_exists()
    assert not (Path(world.root) / "projects").exists()


# --------------------------------------------------------------------------- #
# revision 1: what the first review pair found
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("how", ["expired", "dead-pid"])
def test_hoh_resume_goes_on_over_a_lease_whose_holder_ended_without_releasing_it(world, how):
    """Kills M19 (`refusal_for_resume` refuses anything but a RELEASED lease): a crashed driver (its pid gone,
    LOST at once) or one that ran out its ttl must be resumable, and the resume only reads the lease."""
    if how == "expired":
        world.take_as_other(ttl=60, clock=lambda: PAST)
    else:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        RunLeases(world.lease_dir, pid=child.pid, start=lambda pid: "s1").acquire("r1", "o", ttl_seconds=3600)
    record = _bytes(world.lease_dir)
    rc, _out, err = world.run(command="resume")
    assert rc == 0, err
    assert _bytes(world.lease_dir) == record


def test_the_lease_is_released_after_renewals_that_changed_the_record(world, monkeypatch):
    """A release after real renewals moved the record: the clock has one-second resolution, so the iteration
    waits (on the record, not on a fixed time) until a renewal has changed it."""
    monkeypatch.setattr(rundrive, "keepalive_period", lambda ttl: 0.1)
    seen = {}

    def inside(n, state):
        first = world.leases.read("r1")
        seen["moved"] = _wait(lambda: world.leases.read("r1").renewed_at != first.renewed_at, 10.0)
        seen["lease"] = world.leases.read("r1")

    world.hook = inside
    rc, _out, err = world.run()
    assert rc == 0 and "not released" not in err, err
    assert seen["moved"] and seen["lease"].renewed_at > seen["lease"].acquired_at
    lease = world.leases.read("r1")
    assert lease.released_at is not None and world.leases.judge(lease)[0] == RELEASED


def test_an_oserror_at_acquire_is_a_refusal(world, monkeypatch):
    """Kills M20 (only a `RunLeaseError` is a refusal at the start): a full disk is not a traceback."""
    def refuse(self, *a, **kw):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(RunLeases, "acquire", refuse)
    rc, _out, err = world.run()
    assert rc == 2 and "run lease refused (unreadable)" in err and world.controllers == 0


def test_a_run_with_no_state_takes_no_lease(world):
    """Kills M21 (the `state_exists()` guard of `cmd_run` is gone): nothing is driven, nothing is leased."""
    rc, _out, err = world.run(run_id="nosuch")
    assert rc == 2 and not (Path(world.root) / "projects").exists(), err


def test_a_keyboard_interrupt_that_unwinds_is_released_with_its_reason(world, monkeypatch):
    """Kills M23 (`except Exception` instead of `except BaseException`): `cli.main` catches Ctrl-C, so the
    reason is the one the module states only because of that clause."""
    def unwinds(args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "_cmd_run", unwinds)
    world.run()
    lease = world.leases.read("r1")
    assert lease.released_at is not None and lease.released_reason == "run raised an exception"


def _lost_from_the_start(world, monkeypatch):
    """Every renewal fails from the first iteration on; the iteration waits until the keepalive has given up."""
    monkeypatch.setattr(rundrive, "keepalive_period", lambda ttl: 0.02)
    refusing = threading.Event()
    real_renew = RunLeases.renew

    def renew(self, lease):
        if refusing.is_set():
            raise OSError("the file system refuses")
        return real_renew(self, lease)

    monkeypatch.setattr(RunLeases, "renew", renew)

    def inside(n, state):
        refusing.set()
        _wait(lambda: _threads() == [])

    world.hook = inside


def test_a_loss_during_the_only_iteration_is_said_and_ends_the_command_with_2(world, monkeypatch):
    """Kills M26 (`lost` is looked at only at the top of a pass): with `--iterations 1`, the default, there is
    no next pass, and the run would end 0 with a lease it no longer had."""
    _lost_from_the_start(world, monkeypatch)
    rc, _out, err = world.run(iterations=1)
    assert world.iterations == 1 and rc == 2
    assert "run lease lost: 3 renewals in a row failed" in err and "the run ended under it" in err


def test_a_lost_lease_does_not_hide_a_nonzero_end_of_the_run(world, monkeypatch):
    """Kills M22 (`return 2 if lease_lost else rc`): the end of the run said 3, and the loss does not lower it."""
    _lost_from_the_start(world, monkeypatch)
    real = cli._finish_run
    monkeypatch.setattr(cli, "_finish_run", lambda *a, **kw: (real(*a, **kw)[0], 3))
    rc, _out, err = world.run(iterations=3)
    assert rc == 3, err


def test_an_acquire_that_wrote_and_then_raised_is_given_back_and_the_run_refused(world, monkeypatch):
    """Kills M24 (no give-back): the record is in place, the directory entry is in doubt
    (`DurableWriteNotSynced`). The run is refused, the lease is released at once, and the next try is not
    refused against the lease of the try before."""
    real = durable_io.durable_write
    raised = []

    def write_then_raise(path, payload, **kw):
        real(path, payload, **kw)
        if "run-leases" in str(path) and not raised:
            raised.append(path)
            raise durable_io.DurableWriteNotSynced(path, OSError(errno.ENOSPC, "no space"))

    monkeypatch.setattr(durable_io, "durable_write", write_then_raise)
    rc, _out, err = world.run()
    assert raised and rc == 2 and "run lease refused (unreadable)" in err and world.controllers == 0
    lease = world.leases.read("r1")
    assert lease.released_at is not None and lease.released_reason == "acquire did not finish"
    assert world.leases.judge(lease)[0] == RELEASED
    rc, _out, err = world.run()
    assert rc == 0, err


def test_a_give_back_that_fails_says_that_the_lease_stays_until_it_lapses(tmp_path):
    """Kills M29 (the sentence is dropped) and, with the second leg, a give-back of a record that is not this
    process's: an `acquire` that fails with a foreign or an absent record releases nothing."""
    ours = types.SimpleNamespace(released_at=None, holder="hoh-run.4242", holder_pid=4242, holder_host="h",
                                 expires_at="later")

    class Failing(FakeLeases):
        host = "h"

        def __init__(self, record):
            super().__init__()
            self.record = record

        def acquire(self, name, holder, *, ttl_seconds):
            raise OSError(errno.ENOSPC, "no space")

        def read(self, name):
            return self.record

        def release(self, lease, *, reason):
            self.released.append(reason)
            raise OSError("still no room")

    mine = Failing(ours)
    with pytest.raises(rundrive.DriveRefused) as caught:
        with rundrive.drive_lease(tmp_path, "r1", leases=mine):
            pass
    assert "stays until it lapses at later" in str(caught.value) and mine.released == ["acquire did not finish"]
    for record in (None, types.SimpleNamespace(**{**vars(ours), "holder": "other-session"}),
                   types.SimpleNamespace(**{**vars(ours), "released_at": "earlier"})):
        other = Failing(record)
        with pytest.raises(rundrive.DriveRefused) as caught:
            with rundrive.drive_lease(tmp_path, "r1", leases=other):
                pass
        assert other.released == [] and "stays until" not in str(caught.value)


def test_a_keepalive_thread_that_cannot_be_started_refuses_the_run_and_gives_the_lease_back(world, monkeypatch):
    """Kills M27 (the failure of `Thread.start` is not a refusal): the run is refused before it writes, the
    lease is released, no thread is left."""
    class NoThread(threading.Thread):
        def start(self):
            if self.name == rundrive.THREAD_NAME:
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(rundrive.threading, "Thread", NoThread)
    rc, _out, err = world.run()
    assert rc == 2 and "run lease refused (keepalive)" in err and world.controllers == 0
    lease = world.leases.read("r1")
    assert lease.released_at is not None and world.leases.judge(lease)[0] == RELEASED
    assert _threads() == []


def test_close_never_raises_for_a_thread_that_never_started(tmp_path):
    """Kills M25 (`close` joins a thread that was never started and raises before it releases)."""
    leases = FakeLeases()
    drive = rundrive.DriveLease(leases, "r1", ttl_seconds=300, period=100.0)
    drive.acquire("r1")
    drive.close("run ended")
    assert leases.released == ["run ended"]


def test_the_time_a_failed_release_lapses_at_is_the_last_renewed_one(tmp_path, monkeypatch, capsys):
    """Kills M13 (the result of a renewal is dropped, so the handle stays the one `acquire` returned): the
    line a failed release prints names the expiry of the last renewal."""
    monkeypatch.setattr(rundrive, "keepalive_period", lambda ttl: 0.01)

    class Renewing(FakeLeases):
        def renew(self, lease):
            self.renews += 1
            return types.SimpleNamespace(**{**vars(lease), "expires_at": f"renewal-{self.renews}"})

        def release(self, lease, *, reason):
            raise OSError("no room")

    leases = Renewing()
    with rundrive.drive_lease(tmp_path, "r1", leases=leases):
        assert _wait(lambda: leases.renews >= 3)
    err = capsys.readouterr().err
    assert f"it lapses at renewal-{leases.renews}" in err
