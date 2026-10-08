"""Storage pressure observation and reservation-based admission (STORAGE-B2, P2-39).

Every test runs on injected observers (a fake ``statvfs``, a fake host), an
injected clock and injected liveness functions, inside ``tmp_path``. None of
them depends on the real free space of this machine. Each docstring names the
failure (the mutant) it catches; each test carries a negative control that
changes only the condition under test.
"""

from __future__ import annotations

import ast
import json
import multiprocessing
import os
import queue
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from hoh import storage_pressure as sp
from hoh.storage_pressure import (
    GiB,
    GuestObserver,
    NoHost,
    Observation,
    PressureState,
    Refusal,
    Reservation,
    StateTracker,
    StatvfsHostObserver,
    StorageAdmission,
    StoragePolicy,
    StoragePolicyError,
)

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeStatvfs:
    """``os.statvfs`` stand-in: free bytes, inodes and fsid per path (or one
    value). ``None`` as free bytes for a path means it cannot be observed."""

    def __init__(self, free: int, inodes: int = 10_000_000) -> None:
        self.free = free
        self.inodes = inodes
        self.by_path: dict[str, int | None] = {}
        self.fsid_by_path: dict[str, int] = {}
        self.calls: list[str] = []

    def __call__(self, path):
        self.calls.append(str(path))
        free = self.by_path.get(str(path), self.free)
        if free is None:
            raise FileNotFoundError(2, "not mounted", str(path))
        return SimpleNamespace(f_bavail=free, f_frsize=1, f_favail=self.inodes,
                               f_fsid=self.fsid_by_path.get(str(path), 0x6E57))


class FakeStat:
    """``os.stat`` stand-in: only ``st_dev``, per path (default: the guest's)."""

    def __init__(self, dev_by_path: dict[str, int] | None = None) -> None:
        self.dev_by_path = dict(dev_by_path or {})

    def __call__(self, path):
        return SimpleNamespace(st_dev=self.dev_by_path.get(str(path), 2096))


def host_probes(statvfs: FakeStatvfs, paths, *, mounted=True, fsid=True, dev=True):
    """Injected mount and filesystem probes for host ``paths``: each one a
    mount point (``mounted``) with its own fsid (``fsid``) and st_dev (``dev``),
    or else sharing the guest's."""
    stat = FakeStat()
    for k, path in enumerate(paths):
        if fsid:
            statvfs.fsid_by_path[str(path)] = 0x1000 + k
        if dev:
            stat.dev_by_path[str(path)] = 60 + k
    return {"statvfs": statvfs, "stat": stat, "ismount": lambda p: mounted}


class FakeHost:
    """A configured host whose reading the test sets; ``None`` = unobservable."""

    configured = True

    def __init__(self, free: int | None) -> None:
        self.free = free

    def observe(self):
        if self.free is None:
            return None, "fake host unobservable"
        return self.free, "fake host"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _alive(pid: int) -> bool:
    return True


def _start_111(pid: int) -> int:
    return 111


def _boot_a() -> str:
    return "boot-A"


NS_HOST = "pid:[4026532229]"
NS_SANDBOX = "pid:[4026532376]"


def _ns_host() -> str:
    return NS_HOST


def make(
    root: Path,
    *,
    guest: int = 100 * GiB,
    inodes: int = 10_000_000,
    host=None,
    policy: StoragePolicy | None = None,
    clock: Clock | None = None,
    statvfs: FakeStatvfs | None = None,
    claim: bool = True,
    **kw,
):
    """An admission on ``root`` with a fake guest and (optionally) host.

    With ``claim`` (the default) the instance calls ``claim_boot()`` as a host
    entry point (``hoh run``, ``hoh resume-storage``) does before its first
    admission (STORAGE-B3, Revisions 8/9), so the admission tests keep their
    meaning. A refused claim -- this boot already claimed by another
    namespace, an unreadable namespace, a missing record -- is left refused:
    the tests assert what ``reserve()`` says then. ``claim=False`` models a
    caller that never claims."""
    policy = policy or StoragePolicy()
    clock = clock or Clock()
    statvfs = statvfs if statvfs is not None else FakeStatvfs(guest, inodes)
    observer = GuestObserver(root, host, statvfs=statvfs, clock=clock)
    kw.setdefault("pid_alive", _alive)
    kw.setdefault("proc_start_time", _start_111)
    kw.setdefault("boot_id", _boot_a)
    kw.setdefault("pid_namespace", _ns_host)
    adm = StorageAdmission.for_state_root(root, policy, observer, clock=clock, **kw)
    if claim:
        try:
            adm.claim_boot()
        except sp.StorageLedgerError:
            pass
    return adm, statvfs, clock


def obs(free_gib: float, *, inodes: int = 10_000_000, host_unknown: bool = False) -> Observation:
    return Observation(
        guest_free_bytes=int(free_gib * GiB),
        guest_free_inodes=inodes,
        host_unknown=host_unknown,
    )


# --------------------------------------------------------------------------- #
# K1 -- admission by reservation against the reserve
# --------------------------------------------------------------------------- #


def test_reserve_admission_boundary(tmp_path):
    """Catches `>` instead of `>=` and a dropped reserve term: with 100 GiB
    free, a 70 GiB estimate leaves exactly the 30 GiB reserve and is admitted;
    one byte more is refused, and the refusal names the numbers."""
    adm, _, _ = make(tmp_path / "exact")
    exact = adm.reserve("p1", "role_run", estimate_bytes=70 * GiB)
    assert isinstance(exact, Reservation) and exact.admitted

    # Negative control: the same, one byte larger.
    adm2, _, _ = make(tmp_path / "over")
    over = adm2.reserve("p1", "role_run", estimate_bytes=70 * GiB + 1)
    assert isinstance(over, Refusal) and not over.admitted
    assert (over.effective_free, over.reserved, over.estimate, over.reserve) == (
        100 * GiB, 0, 70 * GiB + 1, 30 * GiB)
    assert over.headroom_after == -1
    assert str(70 * GiB + 1) in over.message and str(30 * GiB) in over.message


def test_reserve_admission_sums_open_reservations(tmp_path):
    """Catches open reservations not being summed: 100 GiB free, 60 GiB is
    admitted and a following 20 GiB is refused because the first still
    counts -- while 20 GiB alone on an empty ledger is admitted. 75 GiB alone
    is refused. With two open reservations their sum counts."""
    adm, _, _ = make(tmp_path / "a")
    first = adm.reserve("p1", "role_run", estimate_bytes=60 * GiB)
    assert first.admitted
    second = adm.reserve("p2", "role_run", estimate_bytes=20 * GiB)
    assert isinstance(second, Refusal) and second.reserved == 60 * GiB

    # Negative control: the same 20 GiB without the open 60 GiB.
    fresh, _, _ = make(tmp_path / "b")
    assert fresh.reserve("p2", "role_run", estimate_bytes=20 * GiB).admitted

    alone, _, _ = make(tmp_path / "c")
    assert isinstance(alone.reserve("p1", "role_run", estimate_bytes=75 * GiB), Refusal)

    # Two open reservations: 20 + 20 open, 40 more is refused (20 left), 30 fits.
    two, _, _ = make(tmp_path / "d")
    assert two.reserve("p1", "clone", estimate_bytes=20 * GiB).admitted
    assert two.reserve("p2", "clone", estimate_bytes=20 * GiB).admitted
    refused = two.reserve("p3", "clone", estimate_bytes=40 * GiB)
    assert isinstance(refused, Refusal) and refused.reserved == 40 * GiB
    assert two.reserve("p3", "clone", estimate_bytes=30 * GiB).admitted

    # Releasing the first frees its estimate for admission again.
    adm.release(first, 1 * GiB)
    assert adm.reserve("p2", "role_run", estimate_bytes=20 * GiB).admitted


# --------------------------------------------------------------------------- #
# K2 -- fits vs. crosses, CRITICAL
# --------------------------------------------------------------------------- #


def test_fits_above_reserve_admitted(tmp_path):
    """Catches PRESSURE/OPTIMIZE (or RESERVE) being treated as a global stop:
    a reservation that fits is admitted in every non-CRITICAL state. Negative
    control: in the same PRESSURE state an estimate that does not fit is
    refused, so only the fit decides."""
    pressure, _, _ = make(tmp_path / "pressure", guest=45 * GiB)
    res = pressure.reserve("p1", "reviewer_pair", estimate_bytes=10 * GiB)
    assert res.admitted
    assert pressure.status()["state"] == "PRESSURE"

    optimize, _, _ = make(tmp_path / "optimize", guest=70 * GiB)
    res = optimize.reserve("p1", "durability_evidence", estimate_bytes=30 * GiB)
    assert res.admitted
    assert optimize.status()["state"] == "OPTIMIZE"

    normal, _, _ = make(tmp_path / "normal", guest=200 * GiB)
    assert normal.reserve("p1", "mutation_review", estimate_bytes=100 * GiB).admitted

    control, _, _ = make(tmp_path / "control", guest=45 * GiB)
    refused = control.reserve("p1", "reviewer_pair", estimate_bytes=16 * GiB)
    assert isinstance(refused, Refusal) and refused.state is PressureState.PRESSURE


def test_crosses_reserve_refused(tmp_path):
    """Catches comparing current free space instead of free-minus-estimate:
    90 GiB free is far above the reserve, but a 61 GiB job would leave 29 GiB
    and is refused with the numbers named; 60 GiB (exactly 30 left) is the
    negative control."""
    adm, _, _ = make(tmp_path / "a", guest=90 * GiB)
    refused = adm.reserve("p1", "archive_staging", estimate_bytes=61 * GiB)
    assert isinstance(refused, Refusal)
    assert refused.state is PressureState.NORMAL
    assert "below the reserve" in refused.reason
    for number in (90 * GiB, 61 * GiB, 30 * GiB):
        assert str(number) in refused.message

    control, _, _ = make(tmp_path / "b", guest=90 * GiB)
    assert control.reserve("p1", "archive_staging", estimate_bytes=60 * GiB).admitted


def test_critical_refuses_small(tmp_path):
    """Catches a removed CRITICAL check: a 1-byte estimate is refused in
    CRITICAL, both by bytes and by too few free inodes (500 GiB free, so the
    reserve arithmetic alone would admit). Negative control: one inode more
    and the same 1-byte job is admitted."""
    low_bytes, _, _ = make(tmp_path / "bytes", guest=10 * GiB)
    refused = low_bytes.reserve("p1", "role_run", estimate_bytes=1)
    assert isinstance(refused, Refusal)
    assert refused.state is PressureState.CRITICAL and "CRITICAL" in refused.reason

    low_inodes, _, _ = make(tmp_path / "inodes", guest=500 * GiB, inodes=99_999)
    refused = low_inodes.reserve("p1", "role_run", estimate_bytes=1)
    assert isinstance(refused, Refusal)
    assert refused.state is PressureState.CRITICAL and "CRITICAL" in refused.reason

    control, _, _ = make(tmp_path / "control", guest=500 * GiB, inodes=100_000)
    assert control.reserve("p1", "role_run", estimate_bytes=1).admitted


# --------------------------------------------------------------------------- #
# K3 -- states, hysteresis, persistence
# --------------------------------------------------------------------------- #


def test_states_thresholds():
    """Catches wrong boundaries or comparison directions: each threshold is
    checked at the boundary and one byte below it on a fresh tracker; too few
    inodes is CRITICAL; worsening applies at once (no hysteresis downwards)."""
    policy = StoragePolicy()

    def fresh(free: int, inodes: int = 10_000_000) -> PressureState:
        return StateTracker(policy).update(
            Observation(guest_free_bytes=free, guest_free_inodes=inodes))

    assert fresh(80 * GiB) is PressureState.NORMAL
    assert fresh(80 * GiB - 1) is PressureState.OPTIMIZE
    assert fresh(50 * GiB) is PressureState.OPTIMIZE
    assert fresh(50 * GiB - 1) is PressureState.PRESSURE
    assert fresh(15 * GiB) is PressureState.PRESSURE
    assert fresh(15 * GiB - 1) is PressureState.CRITICAL
    assert fresh(500 * GiB, inodes=99_999) is PressureState.CRITICAL
    assert fresh(500 * GiB, inodes=100_000) is PressureState.NORMAL

    tracker = StateTracker(policy)
    assert tracker.update(obs(100)) is PressureState.NORMAL
    assert tracker.update(obs(79.9)) is PressureState.OPTIMIZE
    assert tracker.update(obs(49)) is PressureState.PRESSURE
    assert tracker.update(obs(10)) is PressureState.CRITICAL


def test_hysteresis_no_oscillation():
    """Catches removed hysteresis: 49 GiB -> PRESSURE, 52 GiB -> still
    PRESSURE, 56 GiB -> OPTIMIZE; flapping around 50 GiB stays PRESSURE.
    Negative control: the same sequence with hysteresis 0 moves to OPTIMIZE
    at 52 GiB."""
    tracker = StateTracker(StoragePolicy())
    assert tracker.update(obs(49)) is PressureState.PRESSURE
    assert tracker.update(obs(52)) is PressureState.PRESSURE
    assert tracker.update(obs(56)) is PressureState.OPTIMIZE

    flap = StateTracker(StoragePolicy())
    assert [flap.update(obs(v)) for v in (49, 51, 49, 51, 54)] == [PressureState.PRESSURE] * 5

    up = StateTracker(StoragePolicy())
    up.update(obs(60))
    assert up.update(obs(82)) is PressureState.OPTIMIZE
    assert up.update(obs(85)) is PressureState.NORMAL

    crit = StateTracker(StoragePolicy())
    crit.update(obs(14))
    assert crit.update(obs(17)) is PressureState.CRITICAL
    assert crit.update(obs(20)) is PressureState.PRESSURE

    control = StateTracker(StoragePolicy(hysteresis=0))
    assert control.update(obs(49)) is PressureState.PRESSURE
    assert control.update(obs(52)) is PressureState.OPTIMIZE


def test_state_persisted(tmp_path):
    """Catches a tracker that does not persist (a restarted process would
    oscillate): a new tracker on the same file keeps PRESSURE at 52 GiB.
    Negative control: a new tracker on another file says OPTIMIZE."""
    state_file = tmp_path / "state.json"
    assert StateTracker(StoragePolicy(), state_file).update(obs(49)) is PressureState.PRESSURE
    assert json.loads(state_file.read_text(encoding="utf-8"))["state"] == "PRESSURE"

    restarted = StateTracker(StoragePolicy(), state_file)
    assert restarted.last is PressureState.PRESSURE
    assert restarted.update(obs(52)) is PressureState.PRESSURE

    other = StateTracker(StoragePolicy(), tmp_path / "other.json")
    assert other.update(obs(52)) is PressureState.OPTIMIZE

    # The same through the ledger: a second admission on the same state root.
    clock = Clock()
    adm, statvfs, _ = make(tmp_path / "root", guest=49 * GiB, clock=clock)
    assert adm.status()["state"] == "PRESSURE"
    again, statvfs2, _ = make(tmp_path / "root", guest=52 * GiB, clock=clock)
    assert again.status()["state"] == "PRESSURE"


# --------------------------------------------------------------------------- #
# K4 -- observation only; the host
# --------------------------------------------------------------------------- #


def _raise_directory_read(*_a, **_kw):
    raise AssertionError("observer touched a directory listing")


def _patched_directory_reads(monkeypatch):
    monkeypatch.setattr(os, "listdir", _raise_directory_read)
    monkeypatch.setattr(os, "scandir", _raise_directory_read)
    monkeypatch.setattr(os, "walk", _raise_directory_read)
    monkeypatch.setattr(Path, "iterdir", _raise_directory_read)


def test_observes_only_no_directory_reads(tmp_path):
    """Catches an observer that reads, lists or sizes a directory: with
    os.listdir, os.scandir, os.walk and Path.iterdir patched to raise around
    observe() calls only, the real statvfs observers still work. Low space
    caused entirely by foreign data only lowers admission: the foreign files
    are untouched. Negative controls: the patch really is armed (a sizing
    observer fails under it), and with more free space the same job is
    admitted."""
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "big.bin").write_bytes(b"x" * 4096)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in foreign.iterdir()}

    class SizingObserver(GuestObserver):
        def observe(self):
            sum(len(files) for _, _, files in os.walk(self.path))
            return super().observe()

    # The real syscalls, with the host path reported as a mount point on a
    # filesystem of its own (a plain tmp_path directory is neither, see
    # test_unmounted_host_path_unobservable); the probes only rewrite ids.
    def statvfs_other_fs(path):
        st = os.statvfs(path)
        if Path(path) != foreign:
            return st
        return SimpleNamespace(f_bavail=st.f_bavail, f_frsize=st.f_frsize,
                               f_favail=st.f_favail, f_fsid=st.f_fsid ^ 1)

    def stat_other_fs(path):
        st = os.stat(path)
        return SimpleNamespace(st_dev=st.st_dev ^ 1 if Path(path) == foreign else st.st_dev)

    real = GuestObserver(tmp_path, StatvfsHostObserver(
        [foreign], statvfs=statvfs_other_fs, stat=stat_other_fs,
        ismount=lambda p: Path(p) == foreign))
    real_probes = GuestObserver(tmp_path, StatvfsHostObserver([foreign]))
    low = make(tmp_path / "low", guest=20 * GiB)[0]
    roomy = make(tmp_path / "roomy", guest=200 * GiB)[0]

    with pytest.MonkeyPatch.context() as mp:
        _patched_directory_reads(mp)
        reading = real.observe()
        unmounted_reading = real_probes.observe()
        low_reading = low.observer.observe()
        roomy_reading = roomy.observer.observe()
        with pytest.raises(AssertionError):
            SizingObserver(tmp_path).observe()

    assert isinstance(reading.guest_free_bytes, int) and reading.guest_free_bytes >= 0
    assert isinstance(reading.host_free_bytes, int) and not reading.host_unknown
    assert unmounted_reading.host_unknown and unmounted_reading.host_free_bytes is None
    assert low_reading.effective_free_bytes == 20 * GiB
    assert roomy_reading.effective_free_bytes == 200 * GiB

    refused = low.reserve("p1", "role_run", estimate_bytes=2 * GiB)
    assert isinstance(refused, Refusal) and refused.effective_free == 20 * GiB
    assert roomy.reserve("p1", "role_run", estimate_bytes=2 * GiB).admitted

    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in foreign.iterdir()}
    assert after == before


def test_host_smaller_than_guest_lowers_effective(tmp_path):
    """Catches an ignored host: guest 200 GiB, host 60 GiB -> effective 60 GiB,
    state OPTIMIZE, and a 40 GiB job is refused. Negative control: the same
    with the host at 200 GiB (or no host) admits it. StatvfsHostObserver takes
    the minimum over its paths."""
    adm, _, _ = make(tmp_path / "small", guest=200 * GiB, host=FakeHost(60 * GiB))
    reading, state = adm.observe()
    assert reading.effective_free_bytes == 60 * GiB and state is PressureState.OPTIMIZE
    refused = adm.reserve("p1", "clone", estimate_bytes=40 * GiB)
    assert isinstance(refused, Refusal) and refused.effective_free == 60 * GiB

    big, _, _ = make(tmp_path / "big", guest=200 * GiB, host=FakeHost(200 * GiB))
    assert big.reserve("p1", "clone", estimate_bytes=40 * GiB).admitted
    nohost, _, _ = make(tmp_path / "nohost", guest=200 * GiB, host=NoHost())
    assert nohost.reserve("p1", "clone", estimate_bytes=40 * GiB).admitted

    statvfs = FakeStatvfs(500 * GiB)
    statvfs.by_path = {"/mnt/a": 70 * GiB, "/mnt/b": 40 * GiB}
    probes = host_probes(statvfs, ["/mnt/a", "/mnt/b"])
    free, note = StatvfsHostObserver(["/mnt/a", "/mnt/b"], guest_path="/guest",
                                     **probes).observe()
    assert free == 40 * GiB and "/mnt/b" in note
    assert Observation(200 * GiB, 10**7, host_free_bytes=free).effective_free_bytes == 40 * GiB


def test_host_unobservable_not_plenty(tmp_path):
    """Catches an unknown host falling back to the guest: with a configured
    host path that cannot be observed, the reading is host_unknown and the
    state is at best PRESSURE even with 500 GiB on the guest. Negative
    controls: the host observable at 500 GiB reads NORMAL; no host configured
    is not 'unknown'."""
    statvfs = FakeStatvfs(500 * GiB)
    statvfs.by_path = {"/mnt/gone": None}
    host = StatvfsHostObserver(["/mnt/gone"], guest_path=tmp_path,
                               **host_probes(statvfs, ["/mnt/gone"]))
    assert host.observe()[0] is None

    unknown = GuestObserver(tmp_path, host, statvfs=statvfs).observe()
    assert unknown.host_unknown and unknown.host_free_bytes is None
    assert StateTracker(StoragePolicy()).update(unknown) is PressureState.PRESSURE

    # The real statvfs on a path that does not exist (no free space involved).
    real_missing = StatvfsHostObserver([tmp_path / "not-mounted"]).observe()
    assert real_missing[0] is None and "not observable" in real_missing[1]

    adm, _, _ = make(tmp_path / "adm", guest=500 * GiB, host=FakeHost(None))
    assert adm.status()["state"] == "PRESSURE"

    known = GuestObserver(tmp_path, FakeHost(500 * GiB), statvfs=statvfs).observe()
    assert StateTracker(StoragePolicy()).update(known) is PressureState.NORMAL
    nohost = GuestObserver(tmp_path, NoHost(), statvfs=statvfs).observe()
    assert not nohost.host_unknown
    assert StateTracker(StoragePolicy()).update(nohost) is PressureState.NORMAL


def test_host_unobservable_refuses_admission(tmp_path):
    """Catches admission on guest numbers when the host is unobservable and
    no host observation was ever persisted: refused, effective free unknown.
    Negative control: the same with the host observable admits."""
    adm, _, _ = make(tmp_path / "a", guest=500 * GiB, host=FakeHost(None))
    refused = adm.reserve("p1", "role_run", estimate_bytes=1 * GiB)
    assert isinstance(refused, Refusal)
    assert refused.effective_free is None and "host" in refused.reason

    control, _, _ = make(tmp_path / "b", guest=500 * GiB, host=FakeHost(500 * GiB))
    assert control.reserve("p1", "role_run", estimate_bytes=1 * GiB).admitted


def test_host_unobservable_uses_last_observation(tmp_path):
    """Catches guest numbers being used, or reservations opened since the last
    host observation not being subtracted: host 100 GiB observed, then the
    host disappears. A 30 GiB job is admitted on the derived 100 GiB; a
    following 20 GiB job sees 100 - 30 (opened since) = 70 GiB effective, minus
    the 30 GiB still open, minus 20 -> 20 GiB < reserve -> refused. Negative
    control: with the host still observable at 100 GiB it is admitted."""
    host = FakeHost(100 * GiB)
    adm, _, clock = make(tmp_path / "a", guest=500 * GiB, host=host)
    adm.observe()
    clock.advance(60)
    host.free = None
    assert adm.status()["effective_free_bytes"] == 100 * GiB
    first = adm.reserve("p1", "role_run", estimate_bytes=30 * GiB)
    assert first.admitted
    clock.advance(60)
    refused = adm.reserve("p2", "role_run", estimate_bytes=20 * GiB)
    assert isinstance(refused, Refusal)
    assert refused.effective_free == 70 * GiB and refused.reserved == 30 * GiB

    host2 = FakeHost(100 * GiB)
    control, _, clock2 = make(tmp_path / "b", guest=500 * GiB, host=host2)
    control.observe()
    clock2.advance(60)
    assert control.reserve("p1", "role_run", estimate_bytes=30 * GiB).admitted
    clock2.advance(60)
    assert control.reserve("p2", "role_run", estimate_bytes=20 * GiB).admitted


def test_host_unobservable_stale_refuses(tmp_path):
    """Catches a removed age check: a host observation as old as
    host_max_age_s (900 s) never admits. Negative control: 899 s old admits."""
    host = FakeHost(100 * GiB)
    adm, _, clock = make(tmp_path / "a", guest=500 * GiB, host=host)
    adm.observe()
    host.free = None
    clock.advance(899)
    assert adm.reserve("p1", "role_run", estimate_bytes=1 * GiB).admitted
    clock.advance(1)
    refused = adm.reserve("p2", "role_run", estimate_bytes=1 * GiB)
    assert isinstance(refused, Refusal) and refused.effective_free is None
    assert "900" in refused.host_note


def test_host_unobservable_subtracts_released(tmp_path):
    """Catches released actuals not being subtracted from the derived host
    term: a reservation released (40 GiB actually written) after the last host
    observation lowers the host term 100 -> 60 GiB, so a 35 GiB job is refused.
    Negative control: the same release *before* that observation admits it."""
    host = FakeHost(100 * GiB)
    adm, _, clock = make(tmp_path / "a", guest=500 * GiB, host=host)
    first = adm.reserve("p1", "clone", estimate_bytes=10 * GiB)
    clock.advance(10)
    adm.observe()                       # host 100 GiB recorded after `first` opened
    clock.advance(10)
    adm.release(first, 40 * GiB)        # released after the observation
    clock.advance(10)
    host.free = None
    refused = adm.reserve("p2", "clone", estimate_bytes=35 * GiB)
    assert isinstance(refused, Refusal) and refused.effective_free == 60 * GiB

    host2 = FakeHost(100 * GiB)
    control, _, clock2 = make(tmp_path / "b", guest=500 * GiB, host=host2)
    first2 = control.reserve("p1", "clone", estimate_bytes=10 * GiB)
    clock2.advance(5)
    control.release(first2, 40 * GiB)   # released before the observation
    clock2.advance(5)
    control.observe()
    clock2.advance(20)
    host2.free = None
    assert control.reserve("p2", "clone", estimate_bytes=35 * GiB).admitted


# --------------------------------------------------------------------------- #
# K5 -- race, stale reservations, estimates
# --------------------------------------------------------------------------- #


def _race_worker(root, name, hold, entered, release_evt, attempting, results):
    def hook():
        if hold:
            entered.set()
            release_evt.wait(20)

    # The boot was claimed up front by the test; claiming here would wait on
    # the ledger lock process 1 holds, before `attempting` is set.
    adm, _, _ = make(root, guest=100 * GiB, claim=False, _before_commit=hook)
    attempting.set()
    res = adm.reserve(name, "role_run", estimate_bytes=50 * GiB)
    results.put((name, res.admitted))


def test_race_single_admission(tmp_path):
    """Catches a lock-free admission: two real processes race for the last
    space (100 GiB free, 50 GiB each). Process 1 is held inside
    _before_commit after reading the open reservations; process 2 tries
    meanwhile. Exactly one is admitted, and process 2 cannot decide while
    process 1 holds the ledger. Negative control: run one after the other, the
    second sees the first and is refused too -- the race result equals the
    serial one."""
    ctx = multiprocessing.get_context("fork")
    root = tmp_path / "race"
    make(root)                                  # create the ledger up front
    entered, release_evt = ctx.Event(), ctx.Event()
    att1, att2 = ctx.Event(), ctx.Event()
    results = ctx.Queue()
    p1 = ctx.Process(target=_race_worker,
                     args=(root, "p1", True, entered, release_evt, att1, results))
    p2 = ctx.Process(target=_race_worker,
                     args=(root, "p2", False, entered, release_evt, att2, results))
    p1.start()
    try:
        assert entered.wait(20), "process 1 never reached _before_commit"
        p2.start()
        assert att2.wait(20), "process 2 never started its attempt"
        try:
            early = results.get(timeout=1.5)
        except queue.Empty:
            early = None
        release_evt.set()
        got = [early] if early is not None else []
        while len(got) < 2:
            got.append(results.get(timeout=20))
    finally:
        release_evt.set()
        p1.join(20)
        p2.join(20)
    assert early is None, f"process 2 decided while process 1 held the ledger: {early}"
    assert sorted(admitted for _, admitted in got) == [False, True]
    assert dict(got)["p1"] is True

    serial, _, _ = make(tmp_path / "serial")
    assert serial.reserve("p1", "role_run", estimate_bytes=50 * GiB).admitted
    assert not serial.reserve("p2", "role_run", estimate_bytes=50 * GiB).admitted


def test_stale_reservation_reused_pid(tmp_path):
    """Catches pid-only liveness and removal of stale files: a reservation
    whose pid is alive but with another start time (reused pid), or from
    another boot, or of a dead pid, is stale -- not counted, its file renamed
    with a .stale suffix and still present with its content. Negative control:
    the same pid, start time and boot keep it counted."""
    root = tmp_path / "reused"
    owner, _, _ = make(root)
    held = owner.reserve("p1", "role_run", estimate_bytes=50 * GiB)
    assert held.admitted
    original = held.path.read_text(encoding="utf-8")

    same, _, _ = make(root)
    assert isinstance(same.reserve("p2", "role_run", estimate_bytes=50 * GiB), Refusal)
    assert held.path.exists()

    reused, _, _ = make(root, proc_start_time=lambda pid: 222)
    assert reused.reserve("p2", "role_run", estimate_bytes=50 * GiB).admitted
    assert not held.path.exists()
    stale = [r for r in reused.stale_reservations() if r.id == held.id]
    assert len(stale) == 1
    assert ".stale.v1." in stale[0].path.name
    assert stale[0].path.name.startswith(held.path.name)
    assert stale[0].path.read_text(encoding="utf-8") == original

    for label, kw in (("boot", {"boot_id": lambda: "boot-B"}),
                      ("dead", {"pid_alive": lambda pid: False})):
        r = tmp_path / label
        first, _, _ = make(r)
        gone = first.reserve("p1", "role_run", estimate_bytes=50 * GiB)
        later, _, _ = make(r, **kw)
        assert later.reserve("p2", "role_run", estimate_bytes=50 * GiB).admitted, label
        names = [p.name for p in gone.path.parent.glob(gone.path.name + ".stale.v*")]
        assert len(names) == 1, label


def test_estimate_windowed_p95(tmp_path):
    """Catches a ratcheting estimate (max ever, or p95 over all history): an
    actual above the default raises later estimates to safety_factor x p95,
    and once history_window newer actuals exist the outlier ages out and the
    estimate returns to the default. Negative control: an actual below the
    default leaves the default."""
    policy = StoragePolicy(history_window=3, defaults={"role_run": 1 * GiB})
    adm, _, _ = make(tmp_path / "a", guest=10_000 * GiB, policy=policy)

    def run(actual: int, kind: str = "role_run") -> None:
        res = adm.reserve("p1", kind, estimate_bytes=1)
        adm.release(res, actual)

    assert adm.estimate("role_run") == 1 * GiB
    run(GiB // 2)
    assert adm.estimate("role_run") == 1 * GiB          # below the default
    run(10 * GiB)
    assert adm.estimate("role_run") == 15 * GiB         # 1.5 x 10 GiB
    assert adm.estimate("clone") == sp.DEFAULT_ESTIMATES["clone"]
    run(GiB // 2)
    run(GiB // 2)
    assert adm.estimate("role_run") == 15 * GiB         # still in the window of 3
    run(GiB // 2)
    assert adm.estimate("role_run") == 1 * GiB          # aged out

    wide, _, _ = make(tmp_path / "b", guest=100_000 * GiB,
                      policy=StoragePolicy(defaults={"clone": 1}))
    for k in range(1, 21):
        res = wide.reserve("p1", "clone", estimate_bytes=1)
        wide.release(res, k * GiB)
    assert wide.estimate("clone") == int(1.5 * 19 * GiB)   # nearest-rank p95 of 1..20

    # The estimate feeds admission when no estimate is given.
    used = wide.reserve("p2", "clone")
    assert used.admitted and used.estimate == int(1.5 * 19 * GiB)


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


def test_policy_rejects_inconsistent_ordering(tmp_path):
    """Catches a missing ordering check: critical_below >= reserve,
    reserve >= pressure_below and pressure_below >= normal_at are refused,
    by the constructor and by load(). Negative control: the defaults and a
    consistent override are accepted."""
    bad = [
        {"critical_below": 30 * GiB},
        {"critical_below": 31 * GiB},
        {"reserve": 50 * GiB},
        {"pressure_below": 80 * GiB},
        {"pressure_below": 90 * GiB},
    ]
    for case in bad:
        with pytest.raises(ValueError):
            StoragePolicy(**case)
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(case), encoding="utf-8")
        with pytest.raises(StoragePolicyError):
            StoragePolicy.load(path)

    assert StoragePolicy().reserve == 30 * GiB
    assert StoragePolicy(critical_below=29 * GiB).critical_below == 29 * GiB


def test_policy_override_changes_reserve(tmp_path):
    """Catches a per-machine override that is not applied: reserve 20 GiB
    from a JSON file admits a 75 GiB job on 100 GiB free, which the default
    reserve refuses (negative control). Other keys keep their defaults."""
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "reserve": 20 * GiB,
        "host_paths": ["/mnt/host"],
        "defaults": {"clone": 3 * GiB},
        "safety_factor": 2,
    }), encoding="utf-8")
    policy = StoragePolicy.load(path)
    assert policy.reserve == 20 * GiB and policy.pressure_below == 50 * GiB
    assert policy.host_paths == ("/mnt/host",)
    assert policy.defaults["clone"] == 3 * GiB
    assert policy.defaults["role_run"] == sp.DEFAULT_ESTIMATES["role_run"]
    assert policy.safety_factor == 2.0

    override, _, _ = make(tmp_path / "a", policy=policy)
    assert override.reserve("p1", "role_run", estimate_bytes=75 * GiB).admitted
    default, _, _ = make(tmp_path / "b")
    assert isinstance(default.reserve("p1", "role_run", estimate_bytes=75 * GiB), Refusal)


def test_policy_rejects_unknown_key(tmp_path):
    """Catches silently ignored typos: an unknown key, an unknown operation
    kind in defaults, a non-object file and a string for a byte value are
    refused. Negative control: a valid file loads."""
    for body in ({"reserv": 20 * GiB}, {"defaults": {"rolerun": 1}},
                 [1, 2], {"reserve": "20"}):
        path = tmp_path / "p.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        with pytest.raises(StoragePolicyError):
            StoragePolicy.load(path)
    path = tmp_path / "ok.json"
    path.write_text(json.dumps({"reserve": 25 * GiB}), encoding="utf-8")
    assert StoragePolicy.load(path).reserve == 25 * GiB


# --------------------------------------------------------------------------- #
# Ledger bookkeeping
# --------------------------------------------------------------------------- #


def test_release_keeps_closed_file_and_records_history(tmp_path):
    """Catches a release that removes the reservation file or loses the
    record: the file is renamed to a versioned closed name and kept, and
    history.jsonl carries op_kind, project_id, estimate, actual, opened_utc
    and closed_utc. A second release of the same reservation is refused."""
    adm, _, clock = make(tmp_path / "a")
    res = adm.reserve("proj", "regeneration", estimate_bytes=5 * GiB)
    original = res.path.read_text(encoding="utf-8")
    clock.advance(30)
    closed = adm.release(res, 3 * GiB)
    assert not res.path.exists() and closed.exists()
    assert ".closed.v1." in closed.name and closed.read_text(encoding="utf-8") == original
    assert adm.open_reservations() == []
    lines = adm.history_path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[-1])
    assert {k: entry[k] for k in ("op_kind", "project_id", "estimate", "actual")} == {
        "op_kind": "regeneration", "project_id": "proj",
        "estimate": 5 * GiB, "actual": 3 * GiB}
    assert entry["opened_utc"] < entry["closed_utc"]
    with pytest.raises(sp.StorageLedgerError):
        adm.release(res, 3 * GiB)


def test_ledger_keyed_by_fsid_and_shared(tmp_path):
    """Catches a ledger not keyed by the guest filesystem's f_fsid, or two
    admissions on the same state root not seeing each other."""
    a, _, _ = make(tmp_path / "root")
    b, _, _ = make(tmp_path / "root")
    assert a.ledger == b.ledger == tmp_path / "root" / "ledger" / sp.ledger_key(tmp_path / "root")
    fsid = os.statvfs(tmp_path).f_fsid
    if fsid:
        assert a.ledger.name == f"fsid-{fsid:016x}"
    assert a.reserve("p1", "role_run", estimate_bytes=60 * GiB).admitted
    assert isinstance(b.reserve("p2", "role_run", estimate_bytes=20 * GiB), Refusal)
    assert "P3-40" in (StorageAdmission.__doc__ or "")


def test_status_reports_headroom_and_forecast(tmp_path):
    """Catches a status without the reported numbers: state, effective free,
    reserve, open reservations, headroom = effective - reserved - reserve,
    and seconds_to_reserve from a falling slope (None while rising)."""
    adm, statvfs, clock = make(tmp_path / "a", guest=100 * GiB)
    adm.reserve("p1", "clone", estimate_bytes=10 * GiB)
    first = adm.status()
    assert first["state"] == "NORMAL" and first["reserve_bytes"] == 30 * GiB
    assert first["reserved_bytes"] == 10 * GiB and len(first["open_reservations"]) == 1
    assert first["headroom_bytes"] == 60 * GiB
    clock.advance(100)
    statvfs.free = 90 * GiB                    # falling by 0.1 GiB/s
    falling = adm.status()
    assert falling["seconds_to_reserve"] == pytest.approx(600, rel=0.05)

    rising, statvfs2, clock2 = make(tmp_path / "b", guest=100 * GiB)
    rising.status()
    clock2.advance(100)
    statvfs2.free = 110 * GiB
    assert rising.status()["seconds_to_reserve"] is None


def test_module_has_no_removal_call():
    """Catches a removal call in the module (the house rule: nothing is
    removed, stale and closed files are renamed). Scans the AST for calls of
    remove/unlink/rmdir/rmtree/removedirs/truncate and any use of shutil, and
    the text for the literal removal spellings."""
    path = Path(sp.__file__)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"remove", "unlink", "rmdir", "rmtree", "removedirs", "truncate", "ftruncate"}
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            hits.append((node.lineno, node.attr))
        elif isinstance(node, ast.Name) and node.id in forbidden:
            hits.append((node.lineno, node.id))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            if any(n.split(".")[0] == "shutil" for n in names):
                hits.append((node.lineno, "shutil"))
    for token in ("os.remove", "os.unlink", ".unlink(", "rmtree", "os.rmdir", "removedirs"):
        if token in source:
            hits.append((0, token))
    assert hits == []

    # Negative control: the scanner finds a removal call when one is there.
    probe = ast.parse("import os\nos.unlink(p)\n")
    assert any(isinstance(n, ast.Attribute) and n.attr in forbidden for n in ast.walk(probe))


# --------------------------------------------------------------------------- #
# K7 -- Revision 6: helpers
# --------------------------------------------------------------------------- #

NS_NEW_BOOT = "pid:[4026532999]"


class BlockingStatvfs(FakeStatvfs):
    """A guest ``statvfs`` whose first call blocks -- inside the ledger lock,
    since observing happens there -- until the test lets it go."""

    def __init__(self, free: int, inodes: int = 10_000_000) -> None:
        super().__init__(free, inodes)
        self.entered = threading.Event()
        self.go = threading.Event()
        self.armed = True

    def __call__(self, path):
        if self.armed:
            self.armed = False
            self.entered.set()
            assert self.go.wait(20), "the test never released the blocked observer"
        return super().__call__(path)


def _interleaved(blocking: BlockingStatvfs, first, second):
    """Run ``first`` until its observer blocks inside the ledger lock, start
    ``second`` meanwhile, then release ``first``. Returns both results and
    whether ``second`` was still waiting (on the lock) when ``first`` went on."""
    out: dict[str, object] = {}

    def run(name, fn):
        try:
            out[name] = fn()
        except BaseException as exc:  # reported through the result
            out[name] = exc

    t1 = threading.Thread(target=run, args=("first", first))
    t1.start()
    try:
        assert blocking.entered.wait(20), "first call never reached its observer"
        t2 = threading.Thread(target=run, args=("second", second))
        t2.start()
        t2.join(0.5)
        waited = t2.is_alive()
    finally:
        blocking.go.set()
    t1.join(20)
    t2.join(20)
    return out["first"], out["second"], waited


def _reservation_file(adm: StorageAdmission, rid: str, **fields) -> Path:
    """A reservation file written into the ledger as another process would
    have written it (fields override a well-formed default)."""
    data = {
        "id": rid, "project_id": "other", "op_kind": "clone", "estimate": 50 * GiB,
        "pid": 4242, "pid_start_time": 111, "pid_ns": NS_HOST, "boot_id": "boot-A",
        "opened_utc": "2026-10-01T11:00:00.000000Z",
    }
    data.update(fields)
    path = adm.reservations_dir / f"{rid}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _operator_rename(path: Path) -> Path:
    """What an operator does with a file that has to go: a versioned rename."""
    target = path.with_name(f"{path.name}.v1.20261001T120000Z")
    os.rename(path, target)
    return target


# --------------------------------------------------------------------------- #
# K7 -- host paths must be real, separate mounts (sadmit-a F1, items 1, 17)
# --------------------------------------------------------------------------- #


def test_unmounted_host_path_unobservable(tmp_path):
    """Catches a host path accepted without the mount check (M-mount): an
    unmounted WSL drive is a plain guest directory whose statvfs succeeds, so
    without ismount it would read as an observed host. With ismount false the
    reading is host_unknown and admission refuses. Negative control: the same
    path, the same filesystem ids, but mounted -- observed."""
    statvfs = FakeStatvfs(500 * GiB)
    statvfs.by_path = {"/mnt/e": 40 * GiB}
    unmounted = StatvfsHostObserver(["/mnt/e"], guest_path=tmp_path,
                                    **host_probes(statvfs, ["/mnt/e"], mounted=False))
    free, note = unmounted.observe()
    assert free is None and "not a mount point" in note
    reading = GuestObserver(tmp_path, unmounted, statvfs=statvfs).observe()
    assert reading.host_unknown and reading.effective_free_bytes == 500 * GiB
    adm, _, _ = make(tmp_path / "adm", guest=500 * GiB, host=unmounted)
    assert adm.status()["state"] == "PRESSURE"
    assert isinstance(adm.reserve("p1", "role_run", estimate_bytes=1 * GiB), Refusal)

    mounted = StatvfsHostObserver(["/mnt/e"], guest_path=tmp_path,
                                  **host_probes(statvfs, ["/mnt/e"], mounted=True))
    assert mounted.observe()[0] == 40 * GiB
    control, _, _ = make(tmp_path / "control", guest=500 * GiB, host=mounted)
    assert control.status()["host_unknown"] is False


def test_host_on_guest_fs_unobservable(tmp_path):
    """Catches a dropped filesystem comparison (M-samefs): a mount point that
    shares the guest's f_fsid, or its st_dev, or both, is the guest's own
    filesystem and is unobservable as a host. Negative control: a mount point
    whose fsid and st_dev both differ is observed."""
    for label, fsid, dev in (("same fsid", False, True),
                             ("same st_dev", True, False),
                             ("same both", False, False)):
        statvfs = FakeStatvfs(500 * GiB)
        statvfs.by_path = {"/mnt/c": 40 * GiB}
        host = StatvfsHostObserver(["/mnt/c"], guest_path=tmp_path,
                                   **host_probes(statvfs, ["/mnt/c"], fsid=fsid, dev=dev))
        free, note = host.observe()
        assert free is None and "guest's filesystem" in note, label
        assert GuestObserver(tmp_path, host, statvfs=statvfs).observe().host_unknown, label

    statvfs = FakeStatvfs(500 * GiB)
    statvfs.by_path = {"/mnt/c": 40 * GiB}
    control = StatvfsHostObserver(["/mnt/c"], guest_path=tmp_path,
                                  **host_probes(statvfs, ["/mnt/c"]))
    assert control.observe()[0] == 40 * GiB
    reading = GuestObserver(tmp_path, control, statvfs=statvfs).observe()
    assert not reading.host_unknown and reading.effective_free_bytes == 40 * GiB


def test_unobservable_host_persists_nothing(tmp_path):
    """Catches an unobservable host persisted as the last good host reading
    (M-persist-unobs): an unmounted host path writes no host_last.json, and a
    derived host term never refreshes the persisted reading -- so 900 s after
    the last real reading admission refuses. Negative controls: a mounted host
    is persisted; with the host still observable the same job is admitted."""
    statvfs = FakeStatvfs(500 * GiB)
    statvfs.by_path = {"/mnt/e": 400 * GiB}
    unmounted = StatvfsHostObserver(["/mnt/e"],
                                    **host_probes(statvfs, ["/mnt/e"], mounted=False))
    adm, _, _ = make(tmp_path / "unmounted", guest=500 * GiB, host=unmounted)
    adm.observe()
    adm.status()
    assert isinstance(adm.reserve("p1", "role_run", estimate_bytes=1 * GiB), Refusal)
    assert not adm.host_path.exists()

    statvfs2 = FakeStatvfs(500 * GiB)
    statvfs2.by_path = {"/mnt/e": 400 * GiB}
    mounted = StatvfsHostObserver(["/mnt/e"], **host_probes(statvfs2, ["/mnt/e"]))
    control, _, _ = make(tmp_path / "mounted", guest=500 * GiB, host=mounted)
    control.observe()
    assert json.loads(control.host_path.read_text(encoding="utf-8"))["host_free_bytes"] \
        == 400 * GiB

    # A derived term (host gone, last reading fresh) is never persisted.
    host = FakeHost(100 * GiB)
    derived, _, clock = make(tmp_path / "derived", guest=500 * GiB, host=host)
    derived.observe()
    persisted = derived.host_path.read_bytes()
    host.free = None
    clock.advance(100)
    derived.observe()
    derived.status()
    assert derived.reserve("p1", "role_run", estimate_bytes=1 * GiB).admitted
    assert derived.host_path.read_bytes() == persisted
    clock.advance(800)                          # 900 s after the real reading
    assert isinstance(derived.reserve("p2", "role_run", estimate_bytes=1 * GiB), Refusal)

    host2 = FakeHost(100 * GiB)
    live, _, clock2 = make(tmp_path / "live", guest=500 * GiB, host=host2)
    live.observe()
    clock2.advance(900)
    assert live.reserve("p2", "role_run", estimate_bytes=1 * GiB).admitted


# --------------------------------------------------------------------------- #
# K7 -- the tracker under the ledger lock (sadmit-a F2, items 2, 12)
# --------------------------------------------------------------------------- #

#: A valid policy in which the tracker's memory decides admission: at 17 GiB
#: a fresh tracker says PRESSURE (17 - 1 B >= 16 GiB: admitted), a tracker
#: that remembers CRITICAL stays CRITICAL (17 - 5 < 15 GiB: refused).
_TIGHT = StoragePolicy(critical_below=15 * GiB, reserve=16 * GiB, defaults={"role_run": 1})


def test_tracker_loaded_under_lock(tmp_path):
    """Catches a tracker loaded once at construction, or loaded before the
    ledger lock is taken (M-trk-lock): two instances built before either
    updates; A sees 14 GiB and persists CRITICAL, B at 17 GiB must then
    still refuse as CRITICAL. The interleaved half holds A inside the lock
    (its observer blocks) while B starts, so a load before the lock reads the
    old state. Negative control: B alone on a fresh ledger at 17 GiB admits."""
    a, _, _ = make(tmp_path / "seq", guest=14 * GiB, policy=_TIGHT)
    b, _, _ = make(tmp_path / "seq", guest=17 * GiB, policy=_TIGHT)
    refused_a = a.reserve("pa", "role_run", estimate_bytes=1)
    assert isinstance(refused_a, Refusal) and refused_a.state is PressureState.CRITICAL
    refused_b = b.reserve("pb", "role_run", estimate_bytes=1)
    assert isinstance(refused_b, Refusal) and refused_b.state is PressureState.CRITICAL

    blocking = BlockingStatvfs(14 * GiB)
    a2, _, _ = make(tmp_path / "held", policy=_TIGHT, statvfs=blocking)
    b2, _, _ = make(tmp_path / "held", guest=17 * GiB, policy=_TIGHT)
    first, second, waited = _interleaved(
        blocking,
        lambda: a2.reserve("pa", "role_run", estimate_bytes=1),
        lambda: b2.reserve("pb", "role_run", estimate_bytes=1),
    )
    assert waited, "B decided while A held the ledger lock"
    assert isinstance(first, Refusal) and first.state is PressureState.CRITICAL
    assert isinstance(second, Refusal) and second.state is PressureState.CRITICAL

    control, _, _ = make(tmp_path / "control", guest=17 * GiB, policy=_TIGHT)
    assert control.reserve("pb", "role_run", estimate_bytes=1).admitted


def test_tracker_observe_status_under_lock(tmp_path):
    """Catches observe() or status() reporting from a tracker not reloaded
    under the lock (M-trk-lock): two instances built before either updates;
    one persists PRESSURE at 49 GiB, the other then reports PRESSURE at
    52 GiB through status() and observe() -- also when it started while the
    first held the lock. Negative control: a fresh ledger at 52 GiB reports
    OPTIMIZE."""
    a, _, _ = make(tmp_path / "seq", guest=49 * GiB)
    b, _, _ = make(tmp_path / "seq", guest=52 * GiB)
    assert a.observe()[1] is PressureState.PRESSURE
    assert b.status()["state"] == "PRESSURE"
    assert b.observe()[1] is PressureState.PRESSURE

    for label, call in (("status", lambda adm: adm.status()["state"]),
                        ("observe", lambda adm: adm.observe()[1].value)):
        blocking = BlockingStatvfs(49 * GiB)
        a2, _, _ = make(tmp_path / f"held-{label}", statvfs=blocking)
        b2, _, _ = make(tmp_path / f"held-{label}", guest=52 * GiB)
        first, second, waited = _interleaved(blocking, lambda: a2.observe(),
                                             lambda: call(b2))
        assert waited, label
        assert first[1] is PressureState.PRESSURE, label
        assert second == "PRESSURE", label

    control, _, _ = make(tmp_path / "control", guest=52 * GiB)
    assert control.status()["state"] == "OPTIMIZE"
    assert control.observe()[1] is PressureState.OPTIMIZE


# --------------------------------------------------------------------------- #
# K7 -- inode hysteresis (sadmit-a F3, item 3)
# --------------------------------------------------------------------------- #


def test_inode_hysteresis():
    """Catches inode_hysteresis being ignored (M-inode-hyst): after 99 999
    free inodes (CRITICAL), 105 000 and 109 999 are still CRITICAL and only
    110 000 leaves it; flapping around 100 000 stays CRITICAL. Negative
    control: the same with inode_hysteresis=0 leaves CRITICAL at 100 000.
    The key is validated like the other keys."""
    def seq(policy, values):
        tracker = StateTracker(policy)
        return [tracker.update(obs(500, inodes=v)) for v in values]

    C, N = PressureState.CRITICAL, PressureState.NORMAL
    assert StoragePolicy().inode_hysteresis == 10_000
    assert seq(StoragePolicy(), [99_999, 105_000, 109_999, 110_000]) == [C, C, C, N]
    assert seq(StoragePolicy(), [99_999, 100_000, 99_999, 100_000]) == [C, C, C, C]
    # Worsening stays immediate.
    assert seq(StoragePolicy(), [200_000, 99_999]) == [N, C]

    assert seq(StoragePolicy(inode_hysteresis=0), [99_999, 100_000]) == [C, N]

    for bad in (-1, "10000", 1.5):
        with pytest.raises(ValueError):
            StoragePolicy(inode_hysteresis=bad)


# --------------------------------------------------------------------------- #
# K7 -- a caller cannot undercut the history (sadmit-a F4, item 4)
# --------------------------------------------------------------------------- #


def test_caller_estimate_cannot_undercut(tmp_path):
    """Catches a caller's estimate_bytes used verbatim (M-undercut): once
    history raised the role_run estimate to 60 GiB, estimate_bytes=0 still
    reserves 60 GiB, so on 80 GiB free it is refused. Negative controls: a
    caller estimate above the history (65 GiB) is used as given, and with
    enough space the 0-byte request is admitted -- at 60 GiB."""
    policy = StoragePolicy(defaults={"role_run": 1 * GiB})
    adm, statvfs, _ = make(tmp_path / "a", guest=100 * GiB, policy=policy)
    first = adm.reserve("p1", "role_run", estimate_bytes=1)
    assert first.admitted and first.estimate == 1 * GiB      # the default, not 1 B
    adm.release(first, 40 * GiB)
    assert adm.estimate("role_run") == 60 * GiB

    statvfs.free = 80 * GiB
    refused = adm.reserve("p2", "role_run", estimate_bytes=0)
    assert isinstance(refused, Refusal) and refused.estimate == 60 * GiB

    statvfs.free = 100 * GiB
    admitted = adm.reserve("p2", "role_run", estimate_bytes=0)
    assert admitted.admitted and admitted.estimate == 60 * GiB
    adm.release(admitted, 40 * GiB)
    larger = adm.reserve("p3", "role_run", estimate_bytes=65 * GiB)
    assert larger.admitted and larger.estimate == 65 * GiB


# --------------------------------------------------------------------------- #
# K7 -- malformed reservations fail closed (sadmit-a F5, items 5, 9, 15, 19)
# --------------------------------------------------------------------------- #


def test_unparsable_reservation_fails_closed(tmp_path):
    """Catches an unparsable reservation file being ignored (M-malformed-skip):
    a torn reservation file (not JSON) refuses every admission, even 1 GiB on
    500 GiB, names the file, and the module leaves it exactly as it is (no
    rename, no removal). Negative control: the same ledger after an operator's
    versioned rename of the file admits."""
    adm, _, _ = make(tmp_path / "a", guest=500 * GiB)
    torn = adm.reservations_dir / "20261001T110000000000Z-4242-abcd.json"
    torn.write_bytes(b'{"id": "20261001T110000000000Z-4242-abcd", "estimate": 6')
    refused = adm.reserve("p1", "role_run", estimate_bytes=1 * GiB)
    assert isinstance(refused, Refusal) and torn.name in refused.reason
    assert "malformed" in refused.reason
    assert torn.read_bytes().endswith(b'"estimate": 6')
    assert sorted(p.name for p in adm.reservations_dir.iterdir()) == [torn.name]

    _operator_rename(torn)
    assert adm.reserve("p1", "role_run", estimate_bytes=1 * GiB).admitted


def test_malformed_reservation_refuses_admission(tmp_path):
    """Catches a well-formed JSON file with wrong fields being skipped
    (M-malformed-skip): an estimate given as a string and a negative estimate
    each make reserve() refuse, naming the files; nothing is renamed by the
    module. Negative control: a well-formed file from another live process is
    counted, not refused as malformed."""
    adm, _, _ = make(tmp_path / "a", guest=500 * GiB)
    bad1 = _reservation_file(adm, "r-string", estimate="lots")
    bad2 = _reservation_file(adm, "r-negative", estimate=-5)
    refused = adm.reserve("p1", "role_run", estimate_bytes=1 * GiB)
    assert isinstance(refused, Refusal)
    assert bad1.name in refused.reason and bad2.name in refused.reason
    assert bad1.exists() and bad2.exists()

    _operator_rename(bad1)
    still = adm.reserve("p1", "role_run", estimate_bytes=1 * GiB)
    assert isinstance(still, Refusal) and bad2.name in still.reason
    _operator_rename(bad2)
    good = _reservation_file(adm, "r-good", estimate=50 * GiB)
    res = adm.reserve("p1", "role_run", estimate_bytes=1 * GiB)
    assert res.admitted and good.exists()
    assert any(r.id == "r-good" for r in adm.open_reservations())


def test_status_lists_malformed_reservation(tmp_path):
    """Catches a malformed reservation file missing from status()
    (M-status-malformed): status() names it under malformed_reservations.
    Negative control: a ledger with only well-formed reservations lists none."""
    adm, _, _ = make(tmp_path / "a", guest=500 * GiB)
    adm.reserve("p1", "role_run", estimate_bytes=1 * GiB)
    bad = adm.reservations_dir / "broken.json"
    bad.write_text("not json", encoding="utf-8")
    report = adm.status()
    assert report["malformed_reservations"] == ["broken.json"]
    assert len(report["open_reservations"]) == 1

    clean, _, _ = make(tmp_path / "b", guest=500 * GiB)
    clean.reserve("p1", "role_run", estimate_bytes=1 * GiB)
    assert clean.status()["malformed_reservations"] == []


# --------------------------------------------------------------------------- #
# K7 -- the history survives a torn line (sadmit-a F6, items 6, 16)
# --------------------------------------------------------------------------- #


def _history_line(actual: int, kind: str = "role_run") -> str:
    return json.dumps({"op_kind": kind, "project_id": "p", "estimate": 1, "actual": actual,
                       "opened_utc": "2026-10-01T10:00:00.000000Z",
                       "closed_utc": "2026-10-01T10:30:00.000000Z", "id": "h"}) + "\n"


def test_history_torn_line_keeps_next_record(tmp_path):
    """Catches a history reader that stops at, or glues, a torn line (M-torn):
    a torn fragment between two records is skipped and reported, and the
    record after it (100 GiB) still raises the estimate to 150 GiB.
    Negative control: the same history without the torn line gives the same
    estimate and reports no malformed line."""
    policy = StoragePolicy(defaults={"role_run": 1 * GiB})
    adm, _, _ = make(tmp_path / "torn", guest=10_000 * GiB, policy=policy)
    adm.history_path.write_text(
        _history_line(2 * GiB) + '{"op_kind": "role_run", "act\n' + _history_line(100 * GiB),
        encoding="utf-8")
    assert adm.estimate("role_run") == 150 * GiB
    assert adm.status()["history_malformed_lines"] == 1

    control, _, _ = make(tmp_path / "whole", guest=10_000 * GiB, policy=policy)
    control.history_path.write_text(_history_line(2 * GiB) + _history_line(100 * GiB),
                                    encoding="utf-8")
    assert control.estimate("role_run") == 150 * GiB
    assert control.status()["history_malformed_lines"] == 0


def test_torn_history_line_newline_added(tmp_path, monkeypatch):
    """Catches the newline guard dropped (M-no-newline): when history.jsonl
    ends in a torn fragment (no newline), release() first ends that line, so
    its record stays a line of its own and is not lost -- in one os.write.
    Negative control: a history that ends with a newline gets no blank line."""
    policy = StoragePolicy(defaults={"role_run": 1 * GiB})
    adm, _, _ = make(tmp_path / "torn", guest=10_000 * GiB, policy=policy)
    prior = (_history_line(2 * GiB) + '{"op_kind": "role_run", "actu').encode("utf-8")
    adm.history_path.write_bytes(prior)
    res = adm.reserve("p1", "role_run", estimate_bytes=1)

    writes: list[int] = []
    real_write = os.write

    def counting_write(fd, data):
        writes.append(len(data))
        return real_write(fd, data)

    monkeypatch.setattr(sp.os, "write", counting_write)
    adm.release(res, 400 * GiB)
    monkeypatch.undo()
    assert len(writes) == 1

    content = adm.history_path.read_bytes()
    assert content.startswith(prior + b"\n")
    record = json.loads(content[len(prior) + 1:].decode("utf-8"))
    assert record["actual"] == 400 * GiB and content.endswith(b"\n")
    assert adm.estimate("role_run") == 600 * GiB

    control, _, _ = make(tmp_path / "whole", guest=10_000 * GiB, policy=policy)
    whole = _history_line(2 * GiB).encode("utf-8")
    control.history_path.write_bytes(whole)
    res2 = control.reserve("p1", "role_run", estimate_bytes=1)
    control.release(res2, 400 * GiB)
    content2 = control.history_path.read_bytes()
    assert content2.startswith(whole) and not content2[len(whole):].startswith(b"\n")
    assert json.loads(content2[len(whole):].decode("utf-8"))["actual"] == 400 * GiB


# --------------------------------------------------------------------------- #
# K7 -- the opened-since term (M35/M36, item 7)
# --------------------------------------------------------------------------- #


def test_opened_since_keeps_closed_and_stale(tmp_path):
    """Catches closed (M35) or stale (M36) reservations dropped from the
    'opened since the last host observation' term: host 100 GiB observed,
    then one 10 GiB reservation is opened and released (actual 0) and another
    10 GiB is opened and goes stale; with the host gone the derived host term
    is 100 - 10 - 10 = 80 GiB. Negative control: the same two reservations
    opened before the observation leave the term at 100 GiB."""
    host = FakeHost(100 * GiB)
    root = tmp_path / "since"
    adm, _, clock = make(root, guest=500 * GiB, host=host)
    adm.observe()
    clock.advance(10)
    closed = adm.reserve("p1", "clone", estimate_bytes=10 * GiB)
    adm.release(closed, 0)
    stale = adm.reserve("p2", "clone", estimate_bytes=10 * GiB)
    clock.advance(10)
    host.free = None
    judge, _, _ = make(root, guest=500 * GiB, host=host, clock=clock,
                       pid_alive=lambda pid: False)
    report = judge.status()
    assert not stale.path.exists()
    assert len(list(stale.path.parent.glob(stale.path.name + ".stale.v*"))) == 1
    assert report["reserved_bytes"] == 0
    assert report["host_free_bytes"] == 80 * GiB
    assert report["effective_free_bytes"] == 80 * GiB

    host2 = FakeHost(100 * GiB)
    root2 = tmp_path / "before"
    adm2, _, clock2 = make(root2, guest=500 * GiB, host=host2)
    closed2 = adm2.reserve("p1", "clone", estimate_bytes=10 * GiB)
    adm2.release(closed2, 0)
    adm2.reserve("p2", "clone", estimate_bytes=10 * GiB)
    clock2.advance(10)
    adm2.observe()
    clock2.advance(10)
    host2.free = None
    judge2, _, _ = make(root2, guest=500 * GiB, host=host2, clock=clock2,
                        pid_alive=lambda pid: False)
    assert judge2.status()["host_free_bytes"] == 100 * GiB


# --------------------------------------------------------------------------- #
# K7 -- an unreadable start time is never stale (sadmit-b M3/m2, items 10, 13)
# --------------------------------------------------------------------------- #


def test_unreadable_start_time_counted(tmp_path):
    """Catches a reservation recorded without a start time being treated as
    stale (M-starttime-stale): the start time could not be read when the
    reservation was taken, so the file records null; a judge that reads
    another start time for that pid still counts it. Negative control: a
    recorded start time 111 judged as 222 is a reused pid -- stale."""
    root = tmp_path / "null"
    owner, _, _ = make(root, proc_start_time=lambda pid: None)
    held = owner.reserve("p1", "role_run", estimate_bytes=50 * GiB)
    assert json.loads(held.path.read_text(encoding="utf-8"))["pid_start_time"] is None
    judge, _, _ = make(root, proc_start_time=lambda pid: 222)
    refused = judge.reserve("p2", "role_run", estimate_bytes=50 * GiB)
    assert isinstance(refused, Refusal) and refused.reserved == 50 * GiB
    assert held.path.exists()

    root2 = tmp_path / "recorded"
    owner2, _, _ = make(root2)
    held2 = owner2.reserve("p1", "role_run", estimate_bytes=50 * GiB)
    judge2, _, _ = make(root2, proc_start_time=lambda pid: 222)
    assert judge2.reserve("p2", "role_run", estimate_bytes=50 * GiB).admitted
    assert not held2.path.exists()


def test_unreadable_start_time_judged_live(tmp_path):
    """Catches a start time that cannot be read when judging being taken as
    'reused pid' (M-starttime-stale): recorded 111, unreadable now -> the
    reservation is live and counted, its file stays. Negative control: the
    same reservation judged with a readable, different start time is stale."""
    root = tmp_path / "unreadable"
    owner, _, _ = make(root)
    held = owner.reserve("p1", "role_run", estimate_bytes=50 * GiB)
    judge, _, _ = make(root, proc_start_time=lambda pid: None)
    refused = judge.reserve("p2", "role_run", estimate_bytes=50 * GiB)
    assert isinstance(refused, Refusal) and refused.reserved == 50 * GiB
    assert held.path.exists() and judge.stale_reservations() == []

    root2 = tmp_path / "readable"
    owner2, _, _ = make(root2)
    owner2.reserve("p1", "role_run", estimate_bytes=50 * GiB)
    judge2, _, _ = make(root2, proc_start_time=lambda pid: 222)
    assert judge2.reserve("p2", "role_run", estimate_bytes=50 * GiB).admitted


# --------------------------------------------------------------------------- #
# K7 -- pid namespaces and boots (items 11, 14, 18, 19)
# --------------------------------------------------------------------------- #


def test_foreign_namespace_reservation_counted(tmp_path):
    """Catches a reservation from another pid namespace being judged by pid
    (and dropped) (M-foreign-skip): its pid means nothing here, so it is
    counted and listed as foreign_namespace -- both when a sandboxed process
    judges the host's reservation and when the host judges one recorded in a
    sandbox namespace whose pid it cannot see. Negative control: the same
    file recorded in the judge's own namespace with a dead pid is stale."""
    root = tmp_path / "sandboxed-judge"
    owner, _, _ = make(root)
    held = owner.reserve("p1", "role_run", estimate_bytes=50 * GiB)
    inside, _, _ = make(root, pid_namespace=lambda: NS_SANDBOX, pid_alive=lambda pid: False)
    report = inside.status()
    assert report["reserved_bytes"] == 50 * GiB
    assert [f["file"] for f in report["foreign_namespace"]] == [held.path.name]
    assert held.path.exists()

    host_judge, _, _ = make(tmp_path / "host-judge", pid_alive=lambda pid: False)
    foreign = _reservation_file(host_judge, "r-sandbox", pid_ns=NS_SANDBOX)
    refused = host_judge.reserve("p2", "role_run", estimate_bytes=60 * GiB)
    assert isinstance(refused, Refusal) and refused.reserved == 50 * GiB
    assert foreign.exists()
    assert [f["file"] for f in host_judge.status()["foreign_namespace"]] == [foreign.name]

    control, _, _ = make(tmp_path / "own-ns", pid_alive=lambda pid: False)
    own = _reservation_file(control, "r-own", pid_ns=NS_HOST)
    assert control.reserve("p2", "role_run", estimate_bytes=60 * GiB).admitted
    assert not own.exists()
    assert control.status()["foreign_namespace"] == []


def test_reserve_refused_from_other_namespace(tmp_path):
    """Catches the caller's namespace not compared with the ledger's record
    (M-ns-nocheck): a caller in a sandbox namespace is refused ('reserve
    outside the sandbox') even for 1 GiB on 100 GiB free, and writes nothing;
    so is a caller whose namespace cannot be read. Negative control: the
    same request from the recorded namespace is admitted."""
    root = tmp_path / "ns"
    make(root)
    sandboxed, _, _ = make(root, pid_namespace=lambda: NS_SANDBOX)
    refused = sandboxed.reserve("p1", "role_run", estimate_bytes=1 * GiB)
    assert isinstance(refused, Refusal) and "reserve outside the sandbox" in refused.reason
    assert list(sandboxed.reservations_dir.glob("*.json")) == []
    record = json.loads(sandboxed.namespace_path.read_text(encoding="utf-8"))
    assert record["pid_ns"] == NS_HOST

    unknown, _, _ = make(root, pid_namespace=lambda: None)
    assert isinstance(unknown.reserve("p1", "role_run", estimate_bytes=1 * GiB), Refusal)

    host, _, _ = make(root)
    assert host.reserve("p1", "role_run", estimate_bytes=1 * GiB).admitted


def test_namespace_rerecorded_after_boot_change(tmp_path):
    """Catches a namespace record not re-recorded on a new boot
    (M-boot-norerecord) -- under the STORAGE-B3 rule (Revisions 8/9), which
    changed this test: ``reserve()`` no longer re-records the namespace on a
    new boot (the first sandboxed caller of a boot could otherwise claim it);
    only ``claim_boot()`` does. After a reboot the pid namespace number
    changed: ``reserve()`` from the new namespace is refused ('boot not
    claimed') and the record keeps the old boot; after ``claim_boot()`` the
    record names the new boot and namespace (the old record kept inside the
    new one) and the same request is admitted; from then on the old namespace
    is refused. Negative control: on the same boot the new namespace is
    refused and the record stays."""
    same = tmp_path / "same-boot"
    make(same)
    other_ns, _, _ = make(same, pid_namespace=lambda: NS_NEW_BOOT)
    assert isinstance(other_ns.reserve("p1", "role_run", estimate_bytes=1 * GiB), Refusal)
    assert json.loads(other_ns.namespace_path.read_text(encoding="utf-8"))["boot_id"] == "boot-A"

    root = tmp_path / "rebooted"
    make(root)
    after, _, _ = make(root, boot_id=lambda: "boot-B", pid_namespace=lambda: NS_NEW_BOOT,
                       claim=False)
    refused = after.reserve("p1", "role_run", estimate_bytes=1 * GiB)
    assert isinstance(refused, Refusal) and "boot not claimed" in refused.reason
    assert json.loads(after.namespace_path.read_text(encoding="utf-8"))["boot_id"] == "boot-A"

    after.claim_boot()
    assert after.reserve("p1", "role_run", estimate_bytes=1 * GiB).admitted
    record = json.loads(after.namespace_path.read_text(encoding="utf-8"))
    assert (record["boot_id"], record["pid_ns"], record["claimed"]) == (
        "boot-B", NS_NEW_BOOT, True)
    assert [(r["boot_id"], r["pid_ns"]) for r in record["previous"]][-1] == ("boot-A", NS_HOST)
    old_ns, _, _ = make(root, boot_id=lambda: "boot-B")
    refused = old_ns.reserve("p2", "role_run", estimate_bytes=1 * GiB)
    assert isinstance(refused, Refusal) and "reserve outside the sandbox" in refused.reason


def test_previous_boot_reservation_stale_before_namespace(tmp_path):
    """Catches the namespace check running before the boot check
    (M-ns-before-boot): a reservation from the previous boot, recorded in
    the previous boot's namespace, is stale on the new boot -- renamed, not
    counted, not listed as foreign -- so 60 GiB is admitted. Negative
    control: on the same boot a judge in another namespace counts it as
    foreign."""
    root = tmp_path / "reboot"
    owner, _, _ = make(root)
    held = owner.reserve("p1", "role_run", estimate_bytes=60 * GiB)
    after, _, _ = make(root, boot_id=lambda: "boot-B", pid_namespace=lambda: NS_NEW_BOOT)
    assert after.reserve("p2", "role_run", estimate_bytes=60 * GiB).admitted
    assert not held.path.exists()
    assert [r.id for r in after.stale_reservations()] == [held.id]
    assert after.status()["foreign_namespace"] == []

    root2 = tmp_path / "same-boot"
    owner2, _, _ = make(root2)
    held2 = owner2.reserve("p1", "role_run", estimate_bytes=60 * GiB)
    judge, _, _ = make(root2, pid_namespace=lambda: NS_NEW_BOOT)
    report = judge.status()
    assert [f["file"] for f in report["foreign_namespace"]] == [held2.path.name]
    assert report["reserved_bytes"] == 60 * GiB and held2.path.exists()


def test_missing_namespace_record_refuses(tmp_path):
    """Catches a missing namespace record being tolerated (M-record-optional):
    a ledger whose record was renamed away refuses ('ledger namespace record
    missing'), also on a new boot, and constructing another instance does not
    invent one; neither does a ledger directory that existed without a
    record. Negative controls: a newly created ledger writes its record and
    admits; restoring the record admits again."""
    root = tmp_path / "lost"
    adm, _, _ = make(root)
    assert adm.namespace_path.exists()
    assert adm.reserve("p1", "role_run", estimate_bytes=1 * GiB).admitted
    parked = _operator_rename(adm.namespace_path)

    refused = adm.reserve("p2", "role_run", estimate_bytes=1 * GiB)
    assert isinstance(refused, Refusal) and "ledger namespace record missing" in refused.reason
    again, _, _ = make(root)
    assert not again.namespace_path.exists()
    assert isinstance(again.reserve("p2", "role_run", estimate_bytes=1 * GiB), Refusal)
    rebooted, _, _ = make(root, boot_id=lambda: "boot-B")
    assert isinstance(rebooted.reserve("p2", "role_run", estimate_bytes=1 * GiB), Refusal)
    assert not again.namespace_path.exists()

    bare_root = tmp_path / "bare"
    (bare_root / "ledger" / sp.ledger_key(tmp_path) / "reservations").mkdir(parents=True)
    bare, _, _ = make(bare_root)
    assert not bare.namespace_path.exists()
    assert isinstance(bare.reserve("p1", "role_run", estimate_bytes=1 * GiB), Refusal)

    os.rename(parked, adm.namespace_path)
    assert adm.reserve("p2", "role_run", estimate_bytes=1 * GiB).admitted


# --------------------------------------------------------------------------- #
# K8 -- boot claiming and ledger hardening (STORAGE-B3, Revisions 8 and 9)
# --------------------------------------------------------------------------- #


def test_reserve_never_claims_unclaimed_boot(tmp_path):
    """Catches ``reserve()`` claiming or re-recording the namespace itself
    (mutant: "reserve() re-records the namespace" -- the STORAGE-B2 behaviour
    at the pin, so this test also fails there for its property): on a fresh
    ledger nobody has claimed, and on a new boot after a claimed one,
    ``reserve()`` is refused ('boot not claimed') for 1 GiB on 100 GiB free,
    writes no reservation and leaves the record unclaimed / on the old boot.
    Negative control: after ``claim_boot()`` the same request is admitted."""
    fresh, _, _ = make(tmp_path / "fresh", claim=False)
    refused = fresh.reserve("p1", "role_run", estimate_bytes=1 * GiB)
    assert isinstance(refused, Refusal) and "boot not claimed" in refused.reason
    assert list(fresh.reservations_dir.glob("*.json")) == []
    record = json.loads(fresh.namespace_path.read_text(encoding="utf-8"))
    assert record["claimed"] is False

    root = tmp_path / "rebooted"
    make(root)                                       # boot-A, claimed by the host
    before = json.loads((root / "ledger" / sp.ledger_key(root) / "namespace.json")
                        .read_text(encoding="utf-8"))
    first, _, _ = make(root, boot_id=lambda: "boot-B", pid_namespace=lambda: NS_SANDBOX,
                       claim=False)
    refused = first.reserve("p1", "role_run", estimate_bytes=1 * GiB)
    assert isinstance(refused, Refusal) and "boot not claimed" in refused.reason
    assert json.loads(first.namespace_path.read_text(encoding="utf-8")) == before
    assert first.would_admit("role_run") is False

    host, _, _ = make(root, boot_id=lambda: "boot-B", claim=False)
    host.claim_boot()
    assert host.reserve("p1", "role_run", estimate_bytes=1 * GiB).admitted
    late = first.reserve("p2", "role_run", estimate_bytes=1 * GiB)
    assert isinstance(late, Refusal) and "reserve outside the sandbox" in late.reason


def test_claim_boot_refuses_unreadable_namespace(tmp_path):
    """Catches ``claim_boot()`` recording a ``null`` namespace (mutant:
    "claim_boot records null") and an unreadable caller namespace being
    tolerated by ``reserve()`` (mutant ``I14_unreadable_ns_ok``, killed by
    the reason, not only the outcome): a caller whose ``/proc/self/ns/pid``
    cannot be read is refused by ``claim_boot()`` (StorageLedgerError,
    'cannot be read') and the record stays unclaimed and null-free; on a boot
    the host has claimed, its ``reserve()`` is refused with a reason that
    says 'cannot be read'. Negative control: the host's claim succeeds."""
    root = tmp_path / "ns"
    make(root, claim=False)
    unknown, _, _ = make(root, pid_namespace=lambda: None, claim=False)
    with pytest.raises(sp.StorageLedgerError, match="cannot be read"):
        unknown.claim_boot()
    record = json.loads(unknown.namespace_path.read_text(encoding="utf-8"))
    assert record["claimed"] is False and record["pid_ns"] is not None
    assert all(h["pid_ns"] is not None for h in record["history"])

    host, _, _ = make(root, claim=False)
    claimed = host.claim_boot()
    assert (claimed["boot_id"], claimed["pid_ns"], claimed["claimed"]) == (
        "boot-A", NS_HOST, True)
    refused = unknown.reserve("p1", "role_run", estimate_bytes=1 * GiB)
    assert isinstance(refused, Refusal) and "cannot be read" in refused.reason
    assert host.reserve("p1", "role_run", estimate_bytes=1 * GiB).admitted


def test_ledger_creation_refuses_unreadable_namespace(tmp_path):
    """Catches ledger creation recording a ``null`` namespace (mutant:
    "creation records null"; at the pin ``_create_ledger`` wrote
    ``pid_ns: null``, so this test fails there for its property): a creator
    whose namespace link cannot be read is refused (StorageLedgerError) and
    no ledger directory and no namespace record are left behind. Negative
    control: a readable creator creates the ledger, records its namespace in
    the history ('created') and leaves the boot unclaimed."""
    root = tmp_path / "unreadable"
    with pytest.raises(sp.StorageLedgerError, match="cannot be read"):
        make(root, pid_namespace=lambda: None, claim=False)
    ledger = root / "ledger" / sp.ledger_key(root)
    assert not ledger.exists()
    assert not (ledger / "namespace.json").exists()

    adm, _, _ = make(tmp_path / "readable", claim=False)
    record = json.loads(adm.namespace_path.read_text(encoding="utf-8"))
    assert record["claimed"] is False and record["pid_ns"] == NS_HOST
    assert [(h["event"], h["pid_ns"]) for h in record["history"]] == [("created", NS_HOST)]


def test_claim_boot_idempotent_same_namespace_refused_other(tmp_path):
    """Catches a claim that is not idempotent for its own namespace (mutant:
    "refuses the same namespace") or that another namespace can take over on
    the same boot (mutant: "accepts another namespace"). Several controllers
    share one ledger (directive D): the host claims, claims again (accepted,
    the record unchanged), a sandbox namespace's claim is refused and the
    record still names the host. Negative control: on a new boot the other
    namespace may claim -- the rule is per boot."""
    root = tmp_path / "claim"
    host, _, _ = make(root, claim=False)
    first = host.claim_boot()
    again_host, _, _ = make(root, claim=False)
    second = again_host.claim_boot()
    assert first == second
    assert json.loads(host.namespace_path.read_text(encoding="utf-8")) == first

    sandbox, _, _ = make(root, pid_namespace=lambda: NS_SANDBOX, claim=False)
    with pytest.raises(sp.StorageLedgerError, match="already claimed"):
        sandbox.claim_boot()
    assert json.loads(host.namespace_path.read_text(encoding="utf-8"))["pid_ns"] == NS_HOST

    rebooted, _, _ = make(root, boot_id=lambda: "boot-B", pid_namespace=lambda: NS_NEW_BOOT,
                          claim=False)
    assert rebooted.claim_boot()["pid_ns"] == NS_NEW_BOOT


def test_second_release_of_stale_refused(tmp_path):
    """Catches a second release of a reservation that was already judged
    stale (mutant: no second-release check on the stale path): the owner's
    first release of its stale reservation is recorded (the bytes were
    written), a second one is refused (StorageLedgerError) and adds no
    history line. Negative control: the first release is accepted and its
    history line carries the reservation's id."""
    root = tmp_path / "stale"
    owner, _, _ = make(root)
    held = owner.reserve("p1", "role_run", estimate_bytes=10 * GiB)
    assert held.admitted
    judge, _, _ = make(root, proc_start_time=lambda pid: 222)   # reused pid -> stale
    assert [r.id for r in judge.stale_reservations()] == [held.id]

    target = owner.release(held, 3 * GiB)
    assert ".stale.v1." in target.name
    lines = owner.history_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["id"] == held.id and len(lines) == 1
    with pytest.raises(sp.StorageLedgerError, match="already released"):
        owner.release(held, 3 * GiB)
    assert owner.history_path.read_text(encoding="utf-8").splitlines() == lines
