"""The global storage coordinator (STORAGE-E, P3-40): asks, validates, never acts.

Every test creates everything under pytest's `tmp_path`: `HOME` is pointed
there, every registry gets explicit `forbidden_roots`, projects are plain
registered directories, and the admission runs on a fake `statvfs` with
injected liveness probes -- no test depends on this machine's free space. The
registry's boot id, pid start times and pid existence are injected (`FakeOS`)
except in the two-process isolation test, which runs real processes and
therefore uses the real readers. A coordinator takes its pid only from its
injected `getpid` (`w.coordinator(..., getpid=lambda: 100)`); without one it
is `os.getpid()`.

The eleven survivors of the scoord-a mutation review (Revision 5 item 6) are
each killed by a `test_mutant_<id>`; none is claimed equivalent.

OWNED items are real files inside a registry scratch directory of a live
session; ownership is only ever the real `owner_of`'s answer. The fake
stewards only *propose* paths.

Each test names the failure it catches (for K1-K4 the mutant it kills) and
carries a negative control that changes only the condition under test.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import multiprocessing
import os
import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from hoh import storage_coordinator as sc
from hoh import storage_registry as sr
from hoh.storage_coordinator import (
    NEEDS_YOU,
    NEEDS_YOU_PREFIX,
    NONE_NEEDED,
    SUFFICIENT,
    CandidatePlan,
    PlanItem,
    ProjectSteward,
    StorageCoordinator,
    StorageCoordinatorError,
)
from hoh.storage_pressure import (
    GiB,
    GuestObserver,
    StorageAdmission,
    StoragePolicy,
)
from hoh.storage_registry import (
    OWNED,
    PROTECTED,
    UNKNOWN,
    StorageOwnershipError,
    StorageRegistry,
)

BOOT = "boot-1"
NS = "pid:[4026532229]"
SETUP_PID = 900

# --------------------------------------------------------------------------- #
# Fakes and helpers
# --------------------------------------------------------------------------- #


class FakeOS:
    """Injected registry readers: a process table `{pid: start time}` and a
    boot id. Every path lies on mount 1; `st_dev` is the real one."""

    def __init__(self) -> None:
        self.procs: dict[int, int] = {100: 5000, 200: 6000, 300: 7000, SETUP_PID: 9000}
        self.boot = BOOT

    def boot_id(self) -> str:
        return self.boot

    def pid_start(self, pid: int) -> int | None:
        return self.procs.get(pid)

    def pid_exists(self, pid: int) -> bool:
        return pid in self.procs


class FakeStatvfs:
    """`os.statvfs` stand-in for the admission's guest observer."""

    def __init__(self, free: int) -> None:
        self.free = free

    def __call__(self, path):
        return SimpleNamespace(f_bavail=self.free, f_frsize=1, f_favail=10_000_000,
                               f_fsid=0x6E57)


class FixedClock:
    def __call__(self) -> datetime:
        return datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeHost:
    """A configured host that cannot be observed."""

    configured = True

    def observe(self):
        return None, "fake host unobservable"


class FakeSteward:
    """Proposes exactly the items it is given; records each call."""

    def __init__(self, project_id: str, items=()) -> None:
        self.project_id = project_id
        self.items = list(items)
        self.calls: list[dict] = []

    def candidate_plan(self, pressure):
        self.calls.append(pressure)
        return CandidatePlan(self.project_id, tuple(self.items))


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """HOME and git configuration confined to `tmp_path`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)


def new_registry(tmp_path: Path, fake: FakeOS | None) -> StorageRegistry:
    state = tmp_path / "vh" / "state"
    state.parent.mkdir(parents=True, exist_ok=True)
    forbidden = [tmp_path / "home" / "user"]
    if fake is None:                            # real readers (real processes)
        return StorageRegistry(state, forbidden_roots=forbidden)
    return StorageRegistry(
        state,
        forbidden_roots=forbidden,
        boot_id_reader=fake.boot_id,
        pid_start_reader=fake.pid_start,
        pid_exists=fake.pid_exists,
        mount_check=lambda p: False,
        mount_id_reader=lambda p: 1,
    )


def new_admission(state: Path, free: int, *, host=None) -> tuple[StorageAdmission, FakeStatvfs]:
    statvfs = FakeStatvfs(free)
    clock = FixedClock()
    observer = GuestObserver(state, host, statvfs=statvfs, clock=clock)
    admission = StorageAdmission.for_state_root(
        state, StoragePolicy(), observer, clock=clock,
        pid_alive=lambda pid: True, proc_start_time=lambda pid: 111,
        boot_id=lambda: "boot-A", pid_namespace=lambda: NS,
    )
    # A host entry point claims the boot before its first reserve()
    # (STORAGE-B3, Revisions 8-9).
    admission.claim_boot()
    return admission, statvfs


class World:
    """One state root with real registered projects, each with a scratch
    directory of a live setup session holding two owned files."""

    def __init__(self, tmp_path: Path, *, free: int, fake: FakeOS | None,
                 projects=("pa", "pb"), host=None) -> None:
        self.tmp = tmp_path
        self.fake = fake
        self.registry = new_registry(tmp_path, fake)
        self.registry.init()
        self.state = self.registry.state_root
        pid = SETUP_PID if fake is not None else os.getpid()
        boot = BOOT if fake is not None else sr._read_boot_id()
        self.scratch: dict[str, Path] = {}
        self.files: dict[str, list[Path]] = {}
        for project in projects:
            root = tmp_path / f"proj-{project}"
            root.mkdir()
            (root / "README").write_text("project\n", encoding="utf-8")
            self.registry.register_project(project, root)
            self.registry.open_session(project, "setup", pid=pid, boot_id=boot)
            directory = self.registry.scratch_dir(project, "setup", "scratch")
            self.scratch[project] = directory
            self.files[project] = []
            for name in ("f1.bin", "f2.bin"):
                path = directory / name
                path.write_bytes(f"{project}:{name}".encode())
                self.files[project].append(path)
        self.admission, self.statvfs = new_admission(self.state, free, host=host)
        self.foreign = tmp_path / "foreign"
        self.foreign.mkdir()
        self.foreign_file = self.foreign / "secret.bin"
        self.foreign_file.write_bytes(b"not ours")

    def coordinator(self, *stewards, **kw) -> StorageCoordinator:
        return StorageCoordinator(self.registry, self.admission, stewards, **kw)

    def driver(self, project: str) -> Path:
        return self.state / "coordinator" / project / "DRIVER.json"


def item(path, n=1, cls="REPRODUCIBLE_SCRATCH") -> PlanItem:
    return PlanItem(path=path, bytes=n, classification=cls)


def snapshot(*roots: Path) -> dict[str, bytes]:
    """Test-side inventory (the module never lists): every regular file's bytes."""
    out: dict[str, bytes] = {}
    for root in roots:
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                path = Path(dirpath) / name
                if path.is_file() and not path.is_symlink():
                    out[str(path)] = path.read_bytes()
    return out


@pytest.fixture
def fake() -> FakeOS:
    return FakeOS()


# --------------------------------------------------------------------------- #
# K1 -- ownership validation (S1, S3)
# --------------------------------------------------------------------------- #


def test_rejects_other_project_path(tmp_path, fake):
    """Catches acceptance of another project's owned path. Steward `pa`
    proposes `pb`'s owned file, once plainly and once spelt through `pa`'s own
    scratch directory (`<pa scratch>/../../../pb/...`), which `owner_of`
    resolves to `pb`. Both are rejected with `OWNED` by `pb`, the reason names
    `pb`, and `pa`'s plan is untrusted. Kills the mutant *validation by path
    prefix instead of owner_of*: the spelt path starts with `pa`'s prefix.
    Negative control: `pb`'s own steward proposing the same file is accepted."""
    w = World(tmp_path, free=25 * GiB, fake=fake)
    pb_file = w.files["pb"][0]
    spelt = f"{w.scratch['pa']}/../../../pb/scratch/setup/{pb_file.name}"
    assert spelt.startswith(str(w.scratch["pa"]))
    for path in (spelt, pb_file):
        pa = FakeSteward("pa", [item(w.files["pa"][0]), item(path)])
        decision = w.coordinator(pa).on_pressure()
        assert decision.untrusted == ("pa",)
        assert decision.accepted == ()
        assert decision.reclaimable_bytes == 0
        [rej] = decision.rejected
        assert rej.path == str(path)
        assert rej.ownership_status == OWNED and rej.owner_project_id == "pb"
        assert "'pb'" in rej.reason
        assert decision.acted is False

    control = w.coordinator(FakeSteward("pb", [item(pb_file, 7)])).on_pressure()
    assert control.untrusted == () and control.rejected == ()
    assert [p.project_id for p in control.accepted] == ["pb"]
    assert control.reclaimable_bytes == 7


def test_rejects_foreign_path(tmp_path, fake):
    """Catches acceptance of a foreign path: a file and a directory outside
    every registered root, the project's own repository (registered as the
    project root, but not owned scratch), and a hard link inside `pa`'s
    scratch directory to a foreign file (its other name is foreign). Each is
    rejected as `UNKNOWN` and the plan is untrusted. Kills the mutant
    *validation by path prefix instead of owner_of*: the hard link lies inside
    `pa`'s scratch prefix. Negative control: the same plan without the
    foreign item is accepted."""
    w = World(tmp_path, free=25 * GiB, fake=fake)
    hard = w.scratch["pa"] / "hardlink.bin"
    os.link(w.foreign_file, hard)
    owned = item(w.files["pa"][0], 3)
    for path in (w.foreign_file, w.foreign, tmp_path / "proj-pa" / "README", hard):
        decision = w.coordinator(FakeSteward("pa", [owned, item(path)])).on_pressure()
        assert decision.untrusted == ("pa",), path
        [rej] = decision.rejected
        assert rej.path == str(path) and rej.ownership_status == UNKNOWN
        assert decision.reclaimable_bytes == 0 and decision.accepted == ()

    control = w.coordinator(FakeSteward("pa", [owned])).on_pressure()
    assert control.untrusted == () and control.reclaimable_bytes == 3


def test_rejects_unknown_path(tmp_path, fake):
    """Catches acceptance of an `UNKNOWN` path inside the steward's own
    namespace: a file that does not exist, a symlink in `pa`'s scratch
    directory pointing to a foreign file, and a file in an unindexed
    directory under `projects/pa/scratch/`. Each is rejected as `UNKNOWN` and
    the plan is untrusted. Kills the mutant *validation by path prefix
    instead of owner_of*: every one of them carries `pa`'s prefix. Negative
    control: the owned file alone is accepted."""
    w = World(tmp_path, free=25 * GiB, fake=fake)
    missing = w.scratch["pa"] / "ghost.bin"
    link = w.scratch["pa"] / "link.bin"
    os.symlink(w.foreign_file, link)
    rogue = w.state / "projects" / "pa" / "scratch" / "rogue"
    rogue.mkdir()
    (rogue / "file.bin").write_bytes(b"x")
    owned = item(w.files["pa"][1], 2)
    for path in (missing, link, rogue / "file.bin"):
        assert str(path).startswith(str(w.state / "projects" / "pa"))
        decision = w.coordinator(FakeSteward("pa", [owned, item(path)])).on_pressure()
        assert decision.untrusted == ("pa",), path
        [rej] = decision.rejected
        assert rej.ownership_status == UNKNOWN and rej.reason.startswith("UNKNOWN")
        assert decision.reclaimable_bytes == 0

    control = w.coordinator(FakeSteward("pa", [owned])).on_pressure()
    assert control.untrusted == () and control.reclaimable_bytes == 2


def test_untrusted_plan_counts_zero(tmp_path, fake):
    """Catches an untrusted plan whose accepted items still count (Revision 2
    item 1). Free 25 GiB, reserve 30 GiB: shortfall 5 GiB. `pa` proposes 8 GiB
    of owned files plus one missing file inside its own scratch directory;
    `pb` proposes 1 GiB. `pa` is untrusted and counts zero: reclaimable 1 GiB,
    `NEEDS_YOU`, 4 GiB missing. Kills the mutant *validation by path prefix
    instead of owner_of* (the missing file carries `pa`'s prefix, so the
    mutant trusts `pa` and reports SUFFICIENT). Negative control: without the
    missing file `pa` counts, 9 GiB, SUFFICIENT."""
    w = World(tmp_path, free=25 * GiB, fake=fake)
    good = [item(w.files["pa"][0], 4 * GiB), item(w.files["pa"][1], 4 * GiB)]
    bad = item(w.scratch["pa"] / "ghost.bin", 0)
    pb = FakeSteward("pb", [item(w.files["pb"][0], 1 * GiB)])

    decision = w.coordinator(FakeSteward("pa", [*good, bad]), pb).on_pressure()
    assert decision.untrusted == ("pa",)
    assert [p.project_id for p in decision.accepted] == ["pb"]
    assert decision.reclaimable_bytes == 1 * GiB
    assert decision.shortfall_bytes == 5 * GiB
    assert decision.verdict == NEEDS_YOU and not decision.restores_reserve
    assert f"missing {4 * GiB} bytes" in decision.message

    control = w.coordinator(FakeSteward("pa", good), pb).on_pressure()
    assert control.untrusted == ()
    assert [p.project_id for p in control.accepted] == ["pa", "pb"]
    assert control.reclaimable_bytes == 9 * GiB
    assert control.verdict == SUFFICIENT and control.restores_reserve


def test_rejects_registry_root(tmp_path, fake):
    """Catches acceptance of the registry's own layout: the state root, the
    project directory, its `PROJECT.json`, a lease record, the scratch
    directory itself, its owner record and the admission ledger are all
    `PROTECTED`, rejected, and the plan untrusted. Negative control: a file
    inside the same scratch directory is accepted."""
    w = World(tmp_path, free=25 * GiB, fake=fake)
    pdir = w.state / "projects" / "pa"
    protected = [
        w.state, pdir, pdir / "PROJECT.json", pdir / "sessions" / "setup" / "LEASE.json",
        w.scratch["pa"], w.scratch["pa"] / ".vh-owner.json", w.admission.ledger,
    ]
    for path in protected:
        decision = w.coordinator(FakeSteward("pa", [item(path)])).on_pressure()
        assert decision.untrusted == ("pa",), path
        [rej] = decision.rejected
        assert rej.ownership_status == PROTECTED and rej.reason.startswith("PROTECTED")

    control = w.coordinator(FakeSteward("pa", [item(w.files["pa"][0])])).on_pressure()
    assert control.untrusted == () and control.reclaimable_bytes == 1


def test_all_owned_plan_accepted(tmp_path, fake):
    """Catches a coordinator that rejects or reorders valid plans, or acts:
    two all-owned plans are accepted, ordered by reclaimable bytes descending,
    the reserve is restored, `acted` is False and no planned file changed.
    Negative control (ordering): swapping the byte claims swaps the order."""
    w = World(tmp_path, free=25 * GiB, fake=fake)
    before = snapshot(w.state, w.tmp / "proj-pa", w.tmp / "proj-pb")
    pa = FakeSteward("pa", [item(w.files["pa"][0], 3 * GiB), item(w.files["pa"][1], 2 * GiB)])
    pb = FakeSteward("pb", [item(w.files["pb"][0], 7 * GiB)])
    decision = w.coordinator(pa, pb).on_pressure()
    assert [p.project_id for p in decision.accepted] == ["pb", "pa"]
    assert decision.rejected == () and decision.untrusted == ()
    assert decision.reclaimable_bytes == 12 * GiB
    assert decision.verdict == SUFFICIENT and decision.acted is False
    after = snapshot(w.state, w.tmp / "proj-pa", w.tmp / "proj-pb")
    assert {k: v for k, v in after.items() if "/ledger/" not in k} == \
        {k: v for k, v in before.items() if "/ledger/" not in k}

    pb.items = [item(w.files["pb"][0], 1 * GiB)]
    swapped = w.coordinator(pa, pb).on_pressure()
    assert [p.project_id for p in swapped.accepted] == ["pa", "pb"]


def test_rejects_duplicate_and_bad_bytes(tmp_path, fake):
    """Catches double counting and invalid byte claims: the same owned file
    twice (once through `..`), a negative claim and a bool claim each make the
    plan untrusted. Negative control: the file once with an int claim."""
    w = World(tmp_path, free=25 * GiB, fake=fake)
    f = w.files["pa"][0]
    again = f"{w.scratch['pa']}/../setup/{f.name}"
    for items in ([item(f, 5), item(again, 5)], [item(f, -1)], [item(f, True)]):
        decision = w.coordinator(FakeSteward("pa", items)).on_pressure()
        assert decision.untrusted == ("pa",) and decision.reclaimable_bytes == 0
    control = w.coordinator(FakeSteward("pa", [item(f, 5)])).on_pressure()
    assert control.untrusted == () and control.reclaimable_bytes == 5


def test_failing_steward_untrusted(tmp_path, fake):
    """Catches a coordinator that trusts a broken steward: one that raises,
    one that returns no `CandidatePlan`, and one whose plan names another
    project are each untrusted with a reason, and the others still count.
    Negative control: the well-behaved steward alone is accepted."""

    class Raising(FakeSteward):
        def candidate_plan(self, pressure):
            raise RuntimeError("boom")

    class NotAPlan(FakeSteward):
        def candidate_plan(self, pressure):
            return [item(w.files["pb"][0])]

    class OtherProject(FakeSteward):
        def candidate_plan(self, pressure):
            return CandidatePlan("pb", (item(w.files["pb"][0]),))

    w = World(tmp_path, free=25 * GiB, fake=fake)
    good = FakeSteward("pb", [item(w.files["pb"][1], 4)])
    for broken in (Raising("pa"), NotAPlan("pa"), OtherProject("pa")):
        decision = w.coordinator(broken, good).on_pressure()
        assert decision.untrusted == ("pa",)
        assert decision.rejected and decision.rejected[0].project_id == "pa"
        assert decision.reclaimable_bytes == 4
    assert w.coordinator(good).on_pressure().untrusted == ()


# --------------------------------------------------------------------------- #
# K2 -- sessions and the driver lease (S4)
# --------------------------------------------------------------------------- #


def lease_path(w: World, project: str, session: str) -> Path:
    return w.state / "projects" / project / "sessions" / session / "LEASE.json"


def test_live_lease_not_invalidated(tmp_path, fake):
    """Catches a takeover of a live lease: while pid 100 holds session `s1`
    and the driver lease of `pa`, another coordinator cannot open `s1` from
    pid 200 (the registry refuses), cannot coordinate `s1` (it holds no lease)
    and cannot drive `pa` through its own session `s2` (the driver is live).
    Both `LEASE.json` and `DRIVER.json` stay byte-identical and the holder
    keeps driving. The K2 mutant *liveness by pid only* is killed by
    `test_reused_pid_is_stale`. Negative control: the holder itself
    continues (sequence 2)."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    c1 = w.coordinator(FakeSteward("pa"), getpid=lambda: 100)
    c1.acquire_session("pa", "s1", BOOT)
    assert c1.coordinate("pa", "s1", lambda: "ran")[0] == 1
    lease_before = lease_path(w, "pa", "s1").read_bytes()
    driver_before = w.driver("pa").read_bytes()

    c2 = w.coordinator(FakeSteward("pa"), getpid=lambda: 200)
    with pytest.raises(StorageOwnershipError):
        c2.acquire_session("pa", "s1", BOOT)
    with pytest.raises(StorageCoordinatorError):
        c2.coordinate("pa", "s1", lambda: None)
    c2.acquire_session("pa", "s2", BOOT)
    ran: list[int] = []
    with pytest.raises(StorageCoordinatorError, match="driven by live session 's1'"):
        c2.coordinate("pa", "s2", lambda: ran.append(1))
    assert ran == []
    assert lease_path(w, "pa", "s1").read_bytes() == lease_before
    assert w.driver("pa").read_bytes() == driver_before

    assert c1.coordinate("pa", "s1", lambda: "ran") == (2, "ran")


def test_stale_lease_superseded_and_kept(tmp_path, fake):
    """Catches removal or refusal of a stale lease: after pid 100 (holder of
    `s1` and of `pa`'s driver lease) dies, pid 200 reopens `s1`; the old
    `LEASE.json` is kept as `LEASE.json.v1.<UTC>` (by the registry) and the
    old `DRIVER.json` as `DRIVER.json.v1.<UTC>` (by the coordinator), both
    byte-identical, and the sequence carries on at 2. Negative control: while
    pid 100 lives, pid 200 is refused and nothing is renamed."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    c1 = w.coordinator(FakeSteward("pa"), getpid=lambda: 100)
    c1.acquire_session("pa", "s1", BOOT)
    assert c1.coordinate("pa", "s1", lambda: None)[0] == 1
    old_lease = lease_path(w, "pa", "s1").read_bytes()
    old_driver = w.driver("pa").read_bytes()

    c2 = w.coordinator(FakeSteward("pa"), getpid=lambda: 200)
    with pytest.raises(StorageOwnershipError):
        c2.acquire_session("pa", "s1", BOOT)
    assert list(w.driver("pa").parent.glob("DRIVER.json.v*")) == []

    del fake.procs[100]
    c2.acquire_session("pa", "s1", BOOT)
    [kept_lease] = lease_path(w, "pa", "s1").parent.glob("LEASE.json.v1.*")
    assert kept_lease.read_bytes() == old_lease
    assert c2.coordinate("pa", "s1", lambda: None)[0] == 2
    [kept_driver] = w.driver("pa").parent.glob("DRIVER.json.v1.*")
    assert kept_driver.read_bytes() == old_driver
    new = json.loads(w.driver("pa").read_text())
    assert (new["pid"], new["sequence"], new["superseded"]) == (200, 2, 1)


def test_reused_pid_is_stale(tmp_path, fake):
    """Catches liveness by pid only: pid 100 still exists but its start time
    changed (another process reused the pid), so `s1`'s driver lease is
    stale; session `s2` of pid 200 supersedes it (kept as `DRIVER.json.v1.*`)
    and drives with sequence 2, while `s1` can no longer coordinate. Kills
    the mutant *liveness by pid only*, which keeps the reused pid live and
    refuses `s2`. Negative control: with the original start time `s2` is
    refused."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    c1 = w.coordinator(FakeSteward("pa"), getpid=lambda: 100)
    c1.acquire_session("pa", "s1", BOOT)
    assert c1.coordinate("pa", "s1", lambda: None)[0] == 1
    old_driver = w.driver("pa").read_bytes()
    c2 = w.coordinator(FakeSteward("pa"), getpid=lambda: 200)
    c2.acquire_session("pa", "s2", BOOT)
    with pytest.raises(StorageCoordinatorError):
        c2.coordinate("pa", "s2", lambda: None)

    fake.procs[100] = 5001                      # the same pid, another process
    assert c2.coordinate("pa", "s2", lambda: None)[0] == 2
    [kept] = w.driver("pa").parent.glob("DRIVER.json.v1.*")
    assert kept.read_bytes() == old_driver
    assert json.loads(w.driver("pa").read_text())["session_id"] == "s2"
    with pytest.raises(StorageCoordinatorError):
        c1.coordinate("pa", "s1", lambda: None)


def test_second_session_refused_while_driver_live(tmp_path, fake):
    """Catches a second driving session (Revision 2 item 2): while session
    `s1` of pid 100 drives `pa`, session `s2` -- of another process, and also
    of the same process -- is refused at once, `fn` is not run and
    `DRIVER.json` is byte-identical. Kills *coordinate() without the driver
    lease*. Negative control: once pid 100 is dead, `s2` drives with
    sequence 2."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    c1 = w.coordinator(FakeSteward("pa"), getpid=lambda: 100)
    c1.acquire_session("pa", "s1", BOOT)
    c1.coordinate("pa", "s1", lambda: None)
    before = w.driver("pa").read_bytes()

    c1.acquire_session("pa", "s3", BOOT)
    c2 = w.coordinator(FakeSteward("pa"), getpid=lambda: 200)
    c2.acquire_session("pa", "s2", BOOT)
    ran: list[str] = []
    for coord, session in ((c2, "s2"), (c1, "s3")):
        with pytest.raises(StorageCoordinatorError):
            coord.coordinate("pa", session, lambda: ran.append(session))
    assert ran == [] and w.driver("pa").read_bytes() == before

    del fake.procs[100]
    assert c2.coordinate("pa", "s2", lambda: "ok") == (2, "ok")


def test_stale_driver_other_boot_superseded(tmp_path, fake):
    """Catches liveness that ignores the boot id: after a reboot the same
    pid with the same start time is another process; the driver lease of the
    previous boot is superseded (kept as `DRIVER.json.v1.*`) and the sequence
    carries on. Negative control: on the same boot session `s2` is refused."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    c1 = w.coordinator(FakeSteward("pa"), getpid=lambda: 100)
    c1.acquire_session("pa", "s1", BOOT)
    c1.coordinate("pa", "s1", lambda: None)
    old = w.driver("pa").read_bytes()
    c2 = w.coordinator(FakeSteward("pa"), getpid=lambda: 200)
    c2.acquire_session("pa", "s2", BOOT)
    with pytest.raises(StorageCoordinatorError):
        c2.coordinate("pa", "s2", lambda: None)

    fake.boot = "boot-2"
    c3 = w.coordinator(FakeSteward("pa"), getpid=lambda: 100)
    c3.acquire_session("pa", "s2", "boot-2")
    assert c3.coordinate("pa", "s2", lambda: None)[0] == 2
    [kept] = w.driver("pa").parent.glob("DRIVER.json.v1.*")
    assert kept.read_bytes() == old
    assert json.loads(w.driver("pa").read_text())["boot_id"] == "boot-2"


def test_sequence_grows_by_one(tmp_path, fake):
    """Catches a sequence that skips, repeats or is not persisted: three
    calls give 1, 2, 3 and `DRIVER.json` holds 3; `fn`'s result is returned;
    an unreadable `DRIVER.json` is refused (it cannot be proven dead).
    Negative control: a fresh project starts again at 1 -- its own
    sequence."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    c = w.coordinator(FakeSteward("pa"), FakeSteward("pb"), getpid=lambda: 100)
    c.acquire_session("pa", "s1", BOOT)
    assert [c.coordinate("pa", "s1", lambda: k) for k in "abc"] == [(1, "a"), (2, "b"),
                                                                    (3, "c")]
    assert json.loads(w.driver("pa").read_text())["sequence"] == 3
    c.acquire_session("pb", "s1", BOOT)
    assert c.coordinate("pb", "s1", lambda: None)[0] == 1

    w.driver("pb").write_text("{ not json", encoding="utf-8")
    with pytest.raises(StorageCoordinatorError, match="cannot be proven dead"):
        c.coordinate("pb", "s1", lambda: None)
    assert w.driver("pb").read_text() == "{ not json"


def test_owner_marker_written_first_and_unknown(tmp_path, fake, monkeypatch):
    """Catches a coordinator directory whose marker is missing, late or
    treated as ownership (Revision 4): the first record written into
    `coordinator/pa/` is `.vh-owner.json` (owner, project_id, kind
    `coordinator`, created_utc), then `DRIVER.json`; `owner_of` answers
    `UNKNOWN` for the directory, the marker and the driver lease. A marker
    naming another project is refused, never adopted. Negative control: a
    file in the scratch directory is `OWNED`, so the `UNKNOWN` answers are
    not vacuous."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    order: list[str] = []
    real_atomic, real_excl = sc._write_atomic, sc._create_exclusive
    monkeypatch.setattr(sc, "_write_atomic",
                        lambda p, r: (order.append(Path(p).name), real_atomic(p, r)))
    monkeypatch.setattr(sc, "_create_exclusive",
                        lambda p, r: (order.append(Path(p).name), real_excl(p, r)))
    c = w.coordinator(FakeSteward("pa"), FakeSteward("pb"), getpid=lambda: 100)
    c.acquire_session("pa", "s1", BOOT)
    c.coordinate("pa", "s1", lambda: None)
    assert order == [".vh-owner.json", "DRIVER.json"]
    directory = w.state / "coordinator" / "pa"
    marker = json.loads((directory / ".vh-owner.json").read_text())
    assert {k: marker[k] for k in ("owner", "project_id", "kind")} == {
        "owner": "veriharness", "project_id": "pa", "kind": "coordinator"}
    assert marker["created_utc"]
    for path in (directory, directory / ".vh-owner.json", directory / "DRIVER.json"):
        assert w.registry.owner_of(path).status == UNKNOWN
    assert w.registry.owner_of(w.files["pa"][0]).status == OWNED

    pb_dir = w.state / "coordinator" / "pb"
    pb_dir.mkdir()
    (pb_dir / ".vh-owner.json").write_text(json.dumps(
        {**marker, "project_id": "pa"}), encoding="utf-8")
    c.acquire_session("pb", "s1", BOOT)
    with pytest.raises(StorageCoordinatorError, match="not this coordinator's marker"):
        c.coordinate("pb", "s1", lambda: None)
    assert not (pb_dir / "DRIVER.json").exists()


def test_invalid_project_id_refused(tmp_path, fake):
    """Catches a project id used in a path before it is validated (Revision
    4 item 1): ids that fail the registry's pattern (`..`, `../pb`, `A`,
    `a/b`) are refused by the constructor, `acquire_session`, `coordinate`,
    `share` and `weights`, and no `coordinator/` directory is created.
    Negative control: `pa` passes everywhere."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    for bad in ("..", "../pb", "A", "a/b", ""):
        with pytest.raises(StorageCoordinatorError):
            w.coordinator(FakeSteward(bad))
        c = w.coordinator(FakeSteward("pa"), getpid=lambda: 100)
        with pytest.raises(StorageCoordinatorError):
            c.acquire_session(bad, "s1", BOOT)
        with pytest.raises(StorageCoordinatorError):
            c.coordinate(bad, "s1", lambda: None)
        with pytest.raises(StorageCoordinatorError):
            c.share(bad)
        with pytest.raises(StorageCoordinatorError):
            w.coordinator(FakeSteward("pa"), weights={bad: 1})
    assert not (w.state / "coordinator").exists()
    with pytest.raises(StorageCoordinatorError, match="two stewards"):
        w.coordinator(FakeSteward("pa"), FakeSteward("pa"))

    c = w.coordinator(FakeSteward("pa"), getpid=lambda: 100)
    c.acquire_session("pa", "s1", BOOT)
    assert c.coordinate("pa", "s1", lambda: None)[0] == 1
    assert c.share("pa") >= 0


# --------------------------------------------------------------------------- #
# K3 -- two real projects, two real processes (S21)
# --------------------------------------------------------------------------- #

ROUNDS = 5


class AlternatingSteward:
    """Odd calls: only its own owned files. Even calls: its own files plus
    one owned file of the other project (it may only *propose*)."""

    def __init__(self, project_id: str, own: list[Path], other: Path) -> None:
        self.project_id = project_id
        self.own = own
        self.other = other
        self.calls = 0

    def candidate_plan(self, pressure):
        self.calls += 1
        items = [item(p, 1 * GiB) for p in self.own]
        if self.calls % 2 == 0:
            items.append(item(self.other, 1 * GiB))
        return CandidatePlan(self.project_id, tuple(items))


def _k3_child(tmp, project, own, other, barrier, results):
    try:
        registry = new_registry(Path(tmp), None)
        admission, _ = new_admission(registry.state_root, 60 * GiB)
        coord = StorageCoordinator(registry, admission,
                                   [AlternatingSteward(project, own, other)])
        boot = sr._read_boot_id()
        barrier.wait(60)
        coord.acquire_session(project, "drv", boot)
        rounds = []
        for _ in range(ROUNDS):
            seq, d = coord.coordinate(project, "drv", coord.on_pressure)
            rounds.append({
                "seq": seq,
                "accepted": [(p.project_id, [os.fspath(i.path) for i in p.items])
                             for p in d.accepted],
                "rejected": [(r.path, r.ownership_status, r.owner_project_id, r.reason)
                             for r in d.rejected],
                "untrusted": list(d.untrusted),
                "acted": d.acted,
            })
        coord.acquire_session(project, "intruder", boot)
        try:
            coord.coordinate(project, "intruder", lambda: None)
            intruder = "allowed"
        except StorageCoordinatorError:
            intruder = "refused"
        results.put((project, os.getpid(), rounds, intruder, None))
    except BaseException:
        results.put((project, os.getpid(), None, None, traceback.format_exc()))


def test_two_projects_isolated_concurrently(tmp_path):
    """Catches cross-project leakage under concurrency (S21): one state root,
    two real registered projects `pa` and `pb` with real scratch directories
    and real `owner_of`; two real processes (fork) start together at a
    barrier and each drives its own project for 5 rounds while its steward
    proposes, every other round, one of the other project's owned files.
    Each process accepts only its own paths, rejects the other's (`OWNED` by
    the other project, named in the reason, plan untrusted), numbers its
    rounds 1..5 in its own `coordinator/<p>/DRIVER.json` with its own pid,
    and refuses a second session of its project while it drives. The
    coordinator writes nothing but `coordinator/pa/` and `coordinator/pb/`
    (plus the registry's lease records and the admission's ledger, written
    by those modules). Kills the mutants *a shared sequence file for all
    projects* (sequences would not both be 1..5; an extra file would appear
    under `coordinator/`) and *coordinate() without the driver lease* (the
    intruder would be allowed)."""
    w = World(tmp_path, free=60 * GiB, fake=None)
    before = snapshot(tmp_path)
    ctx = multiprocessing.get_context("fork")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    other = {"pa": w.files["pb"][0], "pb": w.files["pa"][0]}
    procs = [
        ctx.Process(target=_k3_child,
                    args=(str(tmp_path), p, w.files[p], other[p], barrier, results))
        for p in ("pa", "pb")
    ]
    for proc in procs:
        proc.start()
    got = {}
    try:
        for _ in procs:
            project, pid, rounds, intruder, error = results.get(timeout=120)
            assert error is None, f"child {project} failed:\n{error}"
            got[project] = (pid, rounds, intruder)
    finally:
        for proc in procs:
            proc.join(30)
    assert sorted(got) == ["pa", "pb"]

    for project, mine in (("pa", "pa"), ("pb", "pb")):
        theirs = "pb" if mine == "pa" else "pa"
        pid, rounds, intruder = got[project]
        assert [r["seq"] for r in rounds] == list(range(1, ROUNDS + 1))
        assert intruder == "refused"
        for n, r in enumerate(rounds, start=1):
            assert r["acted"] is False
            for owner, paths in r["accepted"]:
                assert owner == mine
                for path in paths:
                    assert w.registry.owner_of(path).project_id == mine
            if n % 2:
                assert r["untrusted"] == [] and [o for o, _ in r["accepted"]] == [mine]
                assert r["rejected"] == []
            else:
                assert r["untrusted"] == [mine] and r["accepted"] == []
                [(path, status, owner, reason)] = r["rejected"]
                assert path == str(other[mine])
                assert (status, owner) == (OWNED, theirs) and repr(theirs) in reason
        driver = json.loads(w.driver(mine).read_text())
        assert (driver["project_id"], driver["sequence"], driver["pid"],
                driver["session_id"]) == (mine, ROUNDS, pid, "drv")

    coord_root = w.state / "coordinator"
    assert sorted(os.listdir(coord_root)) == ["pa", "pb"]
    for project in ("pa", "pb"):
        assert sorted(os.listdir(coord_root / project)) == [
            ".lock", ".vh-owner.json", "DRIVER.json"]

    after = snapshot(tmp_path)
    changed = {k for k in after if before.get(k) != after[k]}
    assert set(before) <= set(after)
    allowed_prefixes = [str(coord_root / "pa") + "/", str(coord_root / "pb") + "/",
                        str(w.admission.ledger) + "/"]
    allowed_exact = {str(w.state / "projects" / p / "SEQUENCE.json") for p in ("pa", "pb")}
    allowed_exact |= {str(lease_path(w, p, s)) for p in ("pa", "pb")
                      for s in ("drv", "intruder")}
    stray = sorted(k for k in changed
                   if k not in allowed_exact
                   and not any(k.startswith(prefix) for prefix in allowed_prefixes))
    assert stray == []
    for project in ("pa", "pb"):
        for path in w.files[project]:
            assert after[str(path)] == before[str(path)]


# --------------------------------------------------------------------------- #
# K4 -- NEEDS_YOU, and no touching
# --------------------------------------------------------------------------- #


def test_needs_you_when_owned_plans_insufficient(tmp_path, fake):
    """Catches a decision that claims the reserve is restored when owned
    plans cannot do it, or that acts: free 25 GiB, reserve 30 GiB, shortfall
    5 GiB, owned plans 3 GiB -> `NEEDS_YOU`, the message starts with
    `NEEDS_YOU: owned optimisation cannot restore the reserve` and names the
    2 GiB missing, `acted` is False and no planned file changed. With the
    free space unobservable the answer fails closed (`NEEDS_YOU`, shortfall
    unknown) however large the plans. The K4 mutant *a listdir in
    on_pressure* is killed by `test_no_touch_across_public_api`. Negative
    control: owned plans of 6 GiB give SUFFICIENT."""
    w = World(tmp_path, free=25 * GiB, fake=fake)
    before = snapshot(w.scratch["pa"], w.scratch["pb"])
    pa = FakeSteward("pa", [item(w.files["pa"][0], 1 * GiB)])
    pb = FakeSteward("pb", [item(w.files["pb"][0], 2 * GiB)])
    decision = w.coordinator(pa, pb).on_pressure()
    assert decision.verdict == NEEDS_YOU and decision.restores_reserve is False
    assert decision.message.startswith(NEEDS_YOU_PREFIX)
    assert NEEDS_YOU_PREFIX == "NEEDS_YOU: owned optimisation cannot restore the reserve"
    assert f"missing {2 * GiB} bytes" in decision.message
    assert decision.shortfall_bytes == 5 * GiB and decision.reclaimable_bytes == 3 * GiB
    assert decision.state == "PRESSURE" and decision.acted is False
    assert (len(pa.calls), len(pb.calls)) == (1, 1)
    assert snapshot(w.scratch["pa"], w.scratch["pb"]) == before

    pb.items = [item(w.files["pb"][0], 5 * GiB)]
    control = w.coordinator(pa, pb).on_pressure()
    assert control.verdict == SUFFICIENT and control.restores_reserve

    blind = World(tmp_path / "blind", free=500 * GiB, fake=FakeOS(), host=FakeHost())
    huge = FakeSteward("pa", [item(blind.files["pa"][0], 10 ** 15)])
    unknown = blind.coordinator(huge).on_pressure()
    assert unknown.shortfall_bytes is None and unknown.verdict == NEEDS_YOU
    assert unknown.message.startswith(NEEDS_YOU_PREFIX)


class NoTouchGuard:
    """Wraps the listing and removing primitives. A removal is always a
    violation. A listing is allowed only when it originates (first frame in
    `hoh/` or in this test file) in one of the named record keepers, inside
    its own record directory:

    * `hoh/storage_registry.py` inside `<state_root>/projects/`: the
      registry's own record tree -- `owner_of` enumerates `projects/`, and
      `open_session` (which `acquire_session` must delegate to) scans every
      record of the project for its sequence floor (`projects/<p>/`,
      `sessions/`, `sessions/<s>/`, `<kind>/`, `<kind>/<s>/` for
      `.vh-owner.json`);
    * `hoh/storage_pressure.py` inside the admission ledger
      (`ledger/<fsid>/reservations/*.json` and its versions).

    Everything else -- above all any call from `storage_coordinator.py`, and
    any path in a scratch directory, project root, foreign directory or
    `coordinator/<p>/` from anyone else -- is recorded and raises."""

    LISTING = ("listdir", "scandir", "walk", "iterdir", "glob", "rglob")

    def __init__(self, monkeypatch, allow: dict[str, Path]) -> None:
        self.allow = {name: Path(root).resolve() for name, root in allow.items()}
        self.violations: list[tuple[str, str, str]] = []
        self.allowed: list[tuple[str, str, str]] = []
        self.hoh_dir = Path(sc.__file__).resolve().parent
        self.this = Path(__file__).resolve()
        self.codes: set = set()
        targets = [
            (os, "listdir"), (os, "scandir"), (os, "walk"), (os, "remove"),
            (os, "unlink"), (os, "rmdir"), (shutil, "rmtree"),
            (Path, "iterdir"), (Path, "glob"), (Path, "rglob"),
        ]
        for owner, name in targets:
            monkeypatch.setattr(owner, name, self._wrap(name, getattr(owner, name)))

    def _wrap(self, name, real):
        guard = self

        def wrapper(*args, **kwargs):
            guard.check(name, args[0] if args else kwargs.get("path", kwargs.get("top", ".")))
            return real(*args, **kwargs)

        self.codes.add(wrapper.__code__)
        return wrapper

    def _origin(self) -> Path | None:
        frame = sys._getframe(2)
        while frame is not None:
            if frame.f_code not in self.codes:
                filename = Path(frame.f_code.co_filename)
                if filename.parent == self.hoh_dir or filename == self.this:
                    return filename
            frame = frame.f_back
        return None

    def check(self, name: str, target) -> None:
        origin = self._origin()
        where = os.path.abspath(os.fspath(target)) if isinstance(target, (str, os.PathLike)) \
            else repr(target)
        entry = (name, origin.name if origin else "?", where)
        root = self.allow.get(origin.name) if origin else None
        if name in self.LISTING and root is not None and isinstance(target, (str, os.PathLike)):
            resolved = Path(where).resolve()
            if resolved == root or resolved.is_relative_to(root):
                self.allowed.append(entry)
                return
        self.violations.append(entry)
        raise AssertionError(f"forbidden {name} from {entry[1]} on {where}")


def test_no_touch_across_public_api(tmp_path, fake, monkeypatch):
    """Catches any listing, walking, globbing or removal by the coordinator
    (spec item 5, Revision 2 item 3): `os.listdir`, `os.scandir`, `os.walk`,
    `Path.iterdir`, `Path.glob`, `Path.rglob`, `shutil.rmtree`, `os.remove`,
    `os.unlink` and `os.rmdir` are patched while every public method of
    `StorageCoordinator` is driven (the set of public methods is checked, so
    a new one cannot escape; `close_session` and `release_driver` each
    release a held driver lease, and `share` enumerates the registry's
    projects). Allowed and named: listings by the registry in its record tree
    `<state_root>/projects/` and by the admission in its ledger directory --
    nothing else, no removal at all. Kills the mutant *a listdir in
    on_pressure* (a listing of a planned path's parent: origin
    `storage_coordinator.py`, recorded even if an `except` swallowed it).
    Controls: the patch fires (a listing of a scratch directory from here
    raises, and so does an unlink of a neutral path that never exists under
    `tmp_path` -- never a real fixture -- and a listing by the registry
    outside its tree), and the allowlist is exercised (the registry and the
    admission did list)."""
    w = World(tmp_path, free=25 * GiB, fake=fake)
    neutral = tmp_path / "never-created" / "neutral.bin"   # never exists: no real fixture
    pa = FakeSteward("pa", [item(w.files["pa"][0], 1 * GiB)])
    pb = FakeSteward("pb", [item(w.files["pb"][0], 1 * GiB), item(w.foreign_file)])
    coord = w.coordinator(pa, pb, getpid=lambda: 100)
    public = {name for name, _ in inspect.getmembers(StorageCoordinator, inspect.isfunction)
              if not name.startswith("_")}
    driven = {"observe", "on_pressure", "acquire_session", "coordinate", "share",
              "release_driver", "close_session"}
    assert public == driven

    with monkeypatch.context() as m:
        guard = NoTouchGuard(m, {"storage_registry.py": w.state / "projects",
                                 "storage_pressure.py": w.admission.ledger})
        observed = coord.observe()
        decision = coord.on_pressure()
        lease = coord.acquire_session("pa", "s1", BOOT)
        seq, _ = coord.coordinate("pa", "s1", lambda: None)
        coord.close_session(lease)              # releases the driver, closes the lease
        again = coord.acquire_session("pa", "s1", BOOT)
        assert coord.coordinate("pa", "s1", lambda: None)[0] == 2
        coord.release_driver(again)
        shares = coord.share("pa"), coord.share("pb")
        _ = coord.project_ids
        assert guard.violations == []

        with pytest.raises(AssertionError, match="forbidden listdir"):
            os.listdir(w.scratch["pa"])
        with pytest.raises(AssertionError, match="forbidden unlink"):
            os.unlink(neutral)                  # the guard raises before the real call
        assert guard.violations[-2:] == [
            ("listdir", Path(__file__).name, str(w.scratch["pa"])),
            ("unlink", Path(__file__).name, str(neutral)),
        ]
        origins = {origin for _, origin, _ in guard.allowed}
        assert origins == {"storage_registry.py", "storage_pressure.py"}

    assert w.foreign_file.exists() and not os.path.lexists(neutral.parent)
    assert observed["project_reservations"] == {"pa": [], "pb": []}
    assert decision.untrusted == ("pb",) and decision.verdict == NEEDS_YOU
    assert seq == 1 and shares == (0, 0)


# --------------------------------------------------------------------------- #
# Negative controls: states and shares
# --------------------------------------------------------------------------- #


def test_sufficient_plans_not_needs_you(tmp_path, fake):
    """Catches an off-by-one at the reserve: owned plans exactly equal to the
    5 GiB shortfall restore it (SUFFICIENT); one byte less is `NEEDS_YOU`
    with exactly 1 byte missing."""
    w = World(tmp_path, free=25 * GiB, fake=fake)
    pa = FakeSteward("pa", [item(w.files["pa"][0], 5 * GiB)])
    assert w.coordinator(pa).on_pressure().verdict == SUFFICIENT
    pa.items = [item(w.files["pa"][0], 5 * GiB - 1)]
    short = w.coordinator(pa).on_pressure()
    assert short.verdict == NEEDS_YOU and "missing 1 bytes" in short.message


def test_normal_state_asks_no_steward(tmp_path, fake):
    """Catches a coordinator that asks stewards without pressure: in NORMAL
    (100 GiB free) no steward is called and the verdict is `NONE_NEEDED`.
    Negative control: at 60 GiB (OPTIMIZE) every steward is asked once and
    receives the admission status."""
    w = World(tmp_path, free=100 * GiB, fake=fake)
    pa, pb = FakeSteward("pa"), FakeSteward("pb")
    decision = w.coordinator(pa, pb).on_pressure()
    assert decision.verdict == NONE_NEEDED and decision.state == "NORMAL"
    assert pa.calls == [] and pb.calls == []

    w.statvfs.free = 60 * GiB
    optimize = w.coordinator(pa, pb).on_pressure()
    assert optimize.state == "OPTIMIZE" and optimize.verdict == SUFFICIENT
    assert len(pa.calls) == 1 and len(pb.calls) == 1
    assert pa.calls[0]["state"] == "OPTIMIZE"


def test_share_equal_weights(tmp_path, fake):
    """Catches a wrong headroom split: 100 GiB free, reserve 30 GiB, an open
    reservation of 10 GiB -> headroom 60 GiB, split equally over three
    stewards (20 GiB each); a negative headroom gives 0. Negative control:
    without the reservation, 70 GiB split three ways (rounded down)."""
    w = World(tmp_path, free=100 * GiB, fake=fake, projects=("pa", "pb", "pc"))
    c = w.coordinator(FakeSteward("pa"), FakeSteward("pb"), FakeSteward("pc"))
    assert c.share("pa") == c.share("pc") == (70 * GiB) // 3
    res = w.admission.reserve("pa", "clone", estimate_bytes=10 * GiB)
    assert res.admitted
    assert [c.share(p) for p in ("pa", "pb", "pc")] == [20 * GiB] * 3
    assert c.observe()["project_reservations"]["pa"][0]["estimate"] == 10 * GiB
    w.statvfs.free = 25 * GiB
    assert c.share("pa") == 0
    with pytest.raises(StorageCoordinatorError):
        c.share("pd")


def test_share_configured_weights(tmp_path, fake):
    """Catches weights that are ignored or misapplied: 70 GiB headroom split
    3:1 gives 52.5 and 17.5 GiB; non-positive, non-numeric, bool and unknown
    weights are refused. Negative control: weights 1:1 equal the default."""
    w = World(tmp_path, free=100 * GiB, fake=fake)
    pa, pb = FakeSteward("pa"), FakeSteward("pb")
    c = w.coordinator(pa, pb, weights={"pa": 3, "pb": 1})
    assert (c.share("pa"), c.share("pb")) == (int(52.5 * GiB), int(17.5 * GiB))
    for bad in ({"pa": 0}, {"pa": -1}, {"pa": True}, {"pa": "2"}, {"pc": 1},
                {"pa": float("inf")}):
        with pytest.raises(StorageCoordinatorError):
            w.coordinator(pa, pb, weights=bad)
    even = w.coordinator(pa, pb, weights={"pa": 1, "pb": 1})
    plain = w.coordinator(pa, pb)
    assert (even.share("pa"), even.share("pb")) == (plain.share("pa"), plain.share("pb"))


def test_share_is_advisory_not_enforced(tmp_path, fake):
    """Catches a share that is enforced, or that replaces the admission as
    authority: `pa`'s share is 35 GiB, yet the admission admits a 50 GiB
    reservation for `pa` (the share is reported, not enforced) and then
    refuses 30 GiB for `pb`, which would cross the reserve (the admission
    stays the authority). The docstring says so and names P3-39."""
    w = World(tmp_path, free=100 * GiB, fake=fake)
    c = w.coordinator(FakeSteward("pa"), FakeSteward("pb"))
    assert c.share("pa") == 35 * GiB
    assert w.admission.reserve("pa", "clone", estimate_bytes=50 * GiB).admitted
    assert not w.admission.reserve("pb", "clone", estimate_bytes=30 * GiB).admitted
    doc = " ".join(StorageCoordinator.share.__doc__.split())
    assert "advisory" in doc and "not enforced" in doc
    assert "P3-39" in doc and "StorageAdmission.reserve" in doc


def test_steward_protocol_runtime_checkable():
    """Catches a `ProjectSteward` protocol that cannot be checked: a fake
    steward satisfies it; an object without `candidate_plan` does not."""
    assert isinstance(FakeSteward("pa"), ProjectSteward)
    assert not isinstance(SimpleNamespace(project_id="pa"), ProjectSteward)


# --------------------------------------------------------------------------- #
# K10a -- driver liveness: unknown refuses, the holder's session lease counts
# --------------------------------------------------------------------------- #


def drive(w: World, pid: int, session: str = "s1", project: str = "pa", **kw):
    """A coordinator for `pid` that acquires `session` and drives once."""
    c = w.coordinator(FakeSteward(project), getpid=lambda: pid, **kw)
    lease = c.acquire_session(project, session, BOOT)
    c.coordinate(project, session, lambda: None)
    return c, lease


def versions(w: World, project: str = "pa") -> list[Path]:
    return sorted(w.driver(project).parent.glob("DRIVER.json.v*"))


def test_unreadable_start_time_refuses_takeover(tmp_path, fake):
    """Catches a takeover of a driver whose liveness cannot be judged
    (Revision 5a item 1, scoord-a M1): pid 100 drives `pa` and still exists,
    but its start time reads None, then the reader raises, then it returns a
    non-int. Each time session `s2` of pid 200 is refused, `fn` does not run
    and `DRIVER.json` is byte-identical, nothing is versioned. Kills the
    mutant *unknown treated as dead* (it would hand `s2` sequence 2: two live
    drivers). Negative control, changing only the start-time reading: once
    pid 100 is absent, `s2` supersedes the driver and drives with sequence
    2."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    drive(w, 100)
    before = w.driver("pa").read_bytes()
    c2 = w.coordinator(FakeSteward("pa"), getpid=lambda: 200)
    c2.acquire_session("pa", "s2", BOOT)

    def raising(pid):
        if pid == 100:
            raise PermissionError("hidepid")
        return fake.pid_start(pid)

    ran: list[str] = []
    readers = [None, raising, lambda pid: "5000" if pid == 100 else fake.pid_start(pid)]
    for reader in readers:
        if reader is None:
            fake.procs[100] = None              # pid 100 exists, start time unreadable
            coord = c2
        else:
            fake.procs[100] = 5000
            coord = w.coordinator(FakeSteward("pa"), getpid=lambda: 200,
                                  pid_start_reader=reader)
            coord._sessions.update(c2._sessions)
        with pytest.raises(StorageCoordinatorError, match="cannot be proven dead"):
            coord.coordinate("pa", "s2", lambda: ran.append("s2"))
        assert ran == [] and w.driver("pa").read_bytes() == before
        assert versions(w) == []

    del fake.procs[100]
    assert c2.coordinate("pa", "s2", lambda: "ok") == (2, "ok")
    [kept] = versions(w)
    assert kept.read_bytes() == before


def test_unreadable_start_time_refuses_even_if_session_superseded(tmp_path, fake):
    """Catches the lease rule being judged before the unreadable-start rule
    (Revision 5c, the fixspec-b probe). Leg 1: pid 100 drives `pa` through
    `s1`, its start time becomes unreadable, and pid 200 reopens `s1` (the
    registry supersedes the lease it cannot prove live); pid 200 is still
    refused the driver. Leg 2: the registry closes `s1` outright (no
    `LEASE.json` at all) while pid 100's start time is unreadable; session
    `s2` is refused too. In both legs `DRIVER.json` is byte-identical. Kills
    the mutants *lease rule judged before the unreadable-start rule* (leg 2:
    the absent lease would read DEAD) and *holder's lease not on record means
    dead* (leg 1). Negative control, changing only the start time: with pid
    100 absent, the same coordinator takes over in each leg."""
    for leg in (1, 2):
        fake = FakeOS()
        w = World(tmp_path / f"leg{leg}", free=60 * GiB, fake=fake)
        c1, lease = drive(w, 100)
        before = w.driver("pa").read_bytes()
        fake.procs[100] = None
        if leg == 1:
            c2 = w.coordinator(FakeSteward("pa"), getpid=lambda: 200)
            c2.acquire_session("pa", "s1", BOOT)  # the registry superseded pid 100's lease
            assert json.loads(lease_path(w, "pa", "s1").read_text())["pid"] == 200
            session = "s1"
        else:
            w.registry.close_session("pa", "s1", lease)
            assert not os.path.lexists(lease_path(w, "pa", "s1"))
            c2 = w.coordinator(FakeSteward("pa"), getpid=lambda: 200)
            c2.acquire_session("pa", "s2", BOOT)
            session = "s2"
        ran: list[int] = []
        with pytest.raises(StorageCoordinatorError, match="start time of pid 100 is unreadable"):
            c2.coordinate("pa", session, lambda: ran.append(1))
        assert ran == [] and w.driver("pa").read_bytes() == before and versions(w) == []

        del fake.procs[100]
        assert c2.coordinate("pa", session, lambda: "ok") == (2, "ok")


def test_driver_needs_holder_session_lease(tmp_path, fake):
    """Catches a driver that stays alive without its holder's session lease
    (Revision 5b item 3, 5c rules 6 and 7). Rule 6: pid 100 still runs with
    its start time, but the registry closed `s1` (its `LEASE.json` is
    absent, kept as a version): the driver is dead and session `s2` of pid
    200 supersedes it (kept as `DRIVER.json.v1.*`) with sequence 2. Rule 7:
    `s1` was reopened by pid 300, so `LEASE.json` names another holder:
    unknown, refused. An unreadable `LEASE.json`: unknown, refused. Refusals
    leave `DRIVER.json` byte-identical. Kills the mutant *session lease not
    consulted* (rule 6 would refuse `s2`, as for a live driver). Negative
    control, changing only the lease record: with `s1`'s lease on record,
    `s2` is refused as driven by a live session."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    c1, lease = drive(w, 100)
    before = w.driver("pa").read_bytes()
    c2 = w.coordinator(FakeSteward("pa"), getpid=lambda: 200)
    c2.acquire_session("pa", "s2", BOOT)
    with pytest.raises(StorageCoordinatorError, match="driven by live session 's1'"):
        c2.coordinate("pa", "s2", lambda: None)

    w.registry.close_session("pa", "s1", lease)
    assert not os.path.lexists(lease_path(w, "pa", "s1"))
    assert c2.coordinate("pa", "s2", lambda: "ok") == (2, "ok")
    [kept] = versions(w)
    assert kept.read_bytes() == before

    # Rule 7 and an unreadable LEASE.json, on a fresh world.
    for case in ("other-holder", "unreadable"):
        fake2 = FakeOS()
        w2 = World(tmp_path / case, free=60 * GiB, fake=fake2)
        c1, lease = drive(w2, 100)
        before = w2.driver("pa").read_bytes()
        w2.registry.close_session("pa", "s1", lease)
        if case == "other-holder":
            c3 = w2.coordinator(FakeSteward("pa"), getpid=lambda: 300)
            c3.acquire_session("pa", "s1", BOOT)
            match = "names pid 300"
        else:
            lease_path(w2, "pa", "s1").write_text("{ not json", encoding="utf-8")
            match = "lease of session 's1' is unreadable"
        c2 = w2.coordinator(FakeSteward("pa"), getpid=lambda: 200)
        c2.acquire_session("pa", "s2", BOOT)
        with pytest.raises(StorageCoordinatorError, match=match):
            c2.coordinate("pa", "s2", lambda: None)
        assert w2.driver("pa").read_bytes() == before and versions(w2) == []


# --------------------------------------------------------------------------- #
# K10b -- nested plan items
# --------------------------------------------------------------------------- #


def test_nested_plan_items_rejected(tmp_path, fake):
    """Catches double counting of nested items (Revision 5 item 2, 5a item
    3, scoord-a M2): at 12 GiB free the shortfall is 18 GiB; a plan of a
    10 GiB directory plus a file inside it (10 GiB) must read `NEEDS_YOU`
    with the plan untrusted, never SUFFICIENT at 20 GiB -- in both orders,
    and also when the file is reached through a symlinked alias of the
    directory (`alias/inner.bin`, which `owner_of` resolves into the
    directory) or the directory through the alias. Kills the mutants
    *ancestry not checked* (every case reads SUFFICIENT) and *ancestry checked
    lexically* (the alias cases read SUFFICIENT: `alias/` is no prefix of
    `big/`). Negative control, changing only the nesting: the directory plus
    a sibling file outside it is accepted, 20 GiB, SUFFICIENT."""
    w = World(tmp_path, free=12 * GiB, fake=fake)
    big = w.scratch["pa"] / "big"
    big.mkdir()
    inner = big / "inner.bin"
    inner.write_bytes(b"inner")
    alias = w.scratch["pa"] / "alias"
    os.symlink("big", alias)
    assert w.registry.owner_of(alias / "inner.bin").status == OWNED
    assert w.registry.owner_of(alias / "inner.bin").resolved == inner.resolve()
    assert not str(alias / "inner.bin").startswith(str(big))

    cases = [
        (big, inner), (inner, big),
        (big, alias / "inner.bin"), (alias / "inner.bin", big),
        (alias, inner),
    ]
    for first, second in cases:
        plan = [item(first, 10 * GiB), item(second, 10 * GiB)]
        decision = w.coordinator(FakeSteward("pa", plan)).on_pressure()
        assert decision.shortfall_bytes == 18 * GiB
        assert decision.verdict == NEEDS_YOU, (first, second)
        assert decision.untrusted == ("pa",) and decision.reclaimable_bytes == 0
        assert len(decision.rejected) == 2
        assert all("inside another item" in r.reason or "contains another item" in r.reason
                   for r in decision.rejected)

    sibling = [item(big, 10 * GiB), item(w.files["pa"][0], 10 * GiB)]
    control = w.coordinator(FakeSteward("pa", sibling)).on_pressure()
    assert control.untrusted == () and control.reclaimable_bytes == 20 * GiB
    assert control.verdict == SUFFICIENT


# --------------------------------------------------------------------------- #
# K10c -- release of the driver lease, own pid
# --------------------------------------------------------------------------- #


def test_close_session_releases_driver(tmp_path, fake):
    """Catches a driver lease that outlives its session (Revision 5 item 3,
    scoord-a M4): `close_session` of the driving session `s1` (pid 100, still
    running) leaves `DRIVER.json` marked `released` with sequence 1, keeps
    the held record as `DRIVER.json.v1.*`, closes `s1`'s registry lease (kept
    as `LEASE.json.v1.*`) and forgets the session, so `s1` can no longer
    coordinate; session `s2` of pid 200 then drives with sequence 2. Kills
    the mutant *no release* (`DRIVER.json` would still name `s1`, not
    released). Negative control, changing only which session closes:
    closing `s3`, which does not drive, leaves `DRIVER.json` byte-identical."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    c1, lease = drive(w, 100)
    held = w.driver("pa").read_bytes()
    lease3 = c1.acquire_session("pa", "s3", BOOT)
    c1.close_session(lease3)
    assert w.driver("pa").read_bytes() == held and versions(w) == []

    c1.close_session(lease)
    record = json.loads(w.driver("pa").read_text())
    assert record["released"] is True and record["sequence"] == 1
    [kept] = versions(w)
    assert kept.name.startswith("DRIVER.json.v1.") and kept.read_bytes() == held
    assert not os.path.lexists(lease_path(w, "pa", "s1"))
    assert list(lease_path(w, "pa", "s1").parent.glob("LEASE.json.v1.*"))
    with pytest.raises(StorageCoordinatorError, match="holds no lease"):
        c1.coordinate("pa", "s1", lambda: None)

    c2 = w.coordinator(FakeSteward("pa"), getpid=lambda: 200)
    c2.acquire_session("pa", "s2", BOOT)
    assert c2.coordinate("pa", "s2", lambda: "ok") == (2, "ok")


def test_driver_pid_is_own(tmp_path, fake):
    """Catches a caller-supplied pid (Revision 5 item 3, 5b item 2): no
    method of `StorageCoordinator` takes a `pid` parameter, `acquire_session`
    takes `(project_id, session_id, boot_id=None)`, and the session and
    driver leases name the injected `getpid`'s pid; without injection it is
    `os.getpid()`. Kills the mutant *caller pid accepted*
    (`acquire_session(..., pid)`). Negative control, changing only the
    injected `getpid`: 300 instead of 100 is what the leases then name."""
    for name, fn in inspect.getmembers(StorageCoordinator, inspect.isfunction):
        assert "pid" not in inspect.signature(fn).parameters, name
    params = list(inspect.signature(StorageCoordinator.acquire_session).parameters)
    assert params == ["self", "project_id", "session_id", "boot_id"]

    w = World(tmp_path, free=60 * GiB, fake=fake, projects=("pa", "pb", "pc"))
    for project, pid in (("pa", 100), ("pb", 300)):
        c, lease = drive(w, pid, project=project)
        assert lease.pid == pid
        assert json.loads(lease_path(w, project, "s1").read_text())["pid"] == pid
        assert json.loads(w.driver(project).read_text())["pid"] == pid

    fake.procs[os.getpid()] = 4242
    plain = w.coordinator(FakeSteward("pc"))
    lease = plain.acquire_session("pc", "s1")   # boot id from the reader, pid from os.getpid
    assert (lease.pid, lease.boot_id) == (os.getpid(), BOOT)
    plain.coordinate("pc", "s1", lambda: None)
    assert json.loads(w.driver("pc").read_text())["pid"] == os.getpid()


def test_release_driver_refuses_foreign_lease(tmp_path, fake):
    """Catches a release by anyone but the holder (Revision 5a item 2):
    while `s1` (pid 100) drives `pa`, these are refused and leave
    `DRIVER.json` byte-identical with nothing versioned: `s2` of another
    coordinator (pid 200) releasing with its own lease; `s3` of the holder's
    own coordinator releasing with its lease; a public copy of `s1`'s lease
    read back from the registry (no token); the holder's lease with another
    start time; and a release by a coordinator that does not hold `s1`.
    Kills the mutant *no identity check* (the `s2` and `s3` releases would
    succeed). Negative control, changing only the lease: `s1`'s own lease
    releases, and a second release is refused as already released."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    c1, lease = drive(w, 100)
    before = w.driver("pa").read_bytes()
    lease3 = c1.acquire_session("pa", "s3", BOOT)
    c2 = w.coordinator(FakeSteward("pa"), getpid=lambda: 200)
    lease2 = c2.acquire_session("pa", "s2", BOOT)
    public = w.registry._read_lease("pa", "s1")
    other_start = dataclasses.replace(lease, pid_start=lease.pid_start + 1)
    for coord, candidate in ((c2, lease2), (c1, lease3), (c1, public), (c1, other_start),
                             (c2, lease)):
        with pytest.raises(StorageCoordinatorError):
            coord.release_driver(candidate)
        assert w.driver("pa").read_bytes() == before and versions(w) == []

    c1.release_driver(lease)
    assert json.loads(w.driver("pa").read_text())["released"] is True
    with pytest.raises(StorageCoordinatorError, match="already released"):
        c1.release_driver(lease)


def test_sequence_continues_after_release(tmp_path, fake):
    """Catches a release that restarts the sequence (Revision 5b item 1,
    fixspec-b B1): `s1` drives three times (sequence 3) and releases;
    `DRIVER.json` is marked `released` and keeps `sequence` 3 and
    `superseded` 0, the held record is kept byte-identical as `v1`; session
    `s2` of pid 200 -- while pid 100 still runs -- takes it with sequence 4,
    keeping the released record as `v2`; after `s2` releases, `s1` drives
    again with sequence 5. Kills the mutant *release by rename only*
    (`DRIVER.json` gone, `s2` would restart at 1). Negative control,
    changing only the release: without it `s2` is refused while pid 100
    lives."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    c1 = w.coordinator(FakeSteward("pa"), getpid=lambda: 100)
    lease = c1.acquire_session("pa", "s1", BOOT)
    assert [c1.coordinate("pa", "s1", lambda: None)[0] for _ in range(3)] == [1, 2, 3]
    c2 = w.coordinator(FakeSteward("pa"), getpid=lambda: 200)
    lease2 = c2.acquire_session("pa", "s2", BOOT)
    with pytest.raises(StorageCoordinatorError, match="driven by live session 's1'"):
        c2.coordinate("pa", "s2", lambda: None)

    held = w.driver("pa").read_bytes()
    kept = c1.release_driver(lease)
    assert kept.name.startswith("DRIVER.json.v1.") and kept.read_bytes() == held
    released = json.loads(w.driver("pa").read_text())
    assert (released["released"], released["sequence"], released["superseded"]) == (True, 3, 0)
    released_bytes = w.driver("pa").read_bytes()

    assert c2.coordinate("pa", "s2", lambda: "ok") == (4, "ok")
    [v1, v2] = versions(w)
    assert v1 == kept and v2.name.startswith("DRIVER.json.v2.")
    assert v2.read_bytes() == released_bytes
    now = json.loads(w.driver("pa").read_text())
    assert (now["session_id"], now["sequence"], now["superseded"]) == ("s2", 4, 2)
    assert "released" not in now

    c2.release_driver(lease2)
    assert c1.coordinate("pa", "s1", lambda: None)[0] == 5
    assert len(versions(w)) == 4


# --------------------------------------------------------------------------- #
# K10d -- shares over the registry, item sequences
# --------------------------------------------------------------------------- #


def test_shares_independent_of_instance(tmp_path, fake):
    """Catches shares that depend on which stewards one coordinator instance
    holds (Revision 5 item 4, scoord-a M3): 200 GiB free, reserve 30 GiB ->
    headroom 170 GiB over the two registered projects; a coordinator with
    only `pa`'s steward, one with only `pb`'s, one with both and one with
    none all report 85 GiB for each project (a project without a steward
    gets a share and is not asked for a plan). A weight for a registered
    project without a steward is accepted; a weight or a steward for an
    unregistered project is refused. Kills the mutant *per-instance project
    set* (the single-steward instances would report 170 GiB). Negative
    control, changing only the registry: registering a third project
    changes every instance's shares alike (170/3 GiB)."""
    w = World(tmp_path, free=200 * GiB, fake=fake)
    pa, pb = FakeSteward("pa"), FakeSteward("pb")
    instances = [w.coordinator(pa), w.coordinator(pb), w.coordinator(pa, pb),
                 w.coordinator()]
    for c in instances:
        assert (c.share("pa"), c.share("pb")) == (85 * GiB, 85 * GiB)
    w.statvfs.free = 60 * GiB
    instances[0].on_pressure()
    assert len(pa.calls) == 1 and pb.calls == []
    w.statvfs.free = 200 * GiB

    weighted = w.coordinator(pa, weights={"pb": 3})
    assert (weighted.share("pa"), weighted.share("pb")) == (int(42.5 * GiB), int(127.5 * GiB))
    for bad in ({"stewards": (FakeSteward("pz"),)}, {"weights": {"pz": 1}}):
        with pytest.raises(StorageCoordinatorError, match="does not know"):
            w.coordinator(*bad.get("stewards", ()), weights=bad.get("weights"))

    root = tmp_path / "proj-pc"
    root.mkdir()
    w.registry.register_project("pc", root)
    for c in instances:
        assert [c.share(p) for p in ("pa", "pb", "pc")] == [(170 * GiB) // 3] * 3


class ListSteward(FakeSteward):
    """Hands over its items exactly as stored (a list, a string, ...)."""

    def __init__(self, project_id: str, items) -> None:
        super().__init__(project_id)
        self.raw = items

    def candidate_plan(self, pressure):
        self.calls.append(pressure)
        return CandidatePlan(self.project_id, self.raw)


def test_list_items_counted(tmp_path, fake):
    """Catches a plan whose items are a list being counted as zero
    (Revision 5 item 5, 5b item 6, scoord-b gap 4): `pa` hands over its two
    owned files as a list (4 + 3 GiB); the plan is accepted, counts 7 GiB,
    and the accepted plan holds the items frozen to a tuple with each
    classification kept. A string, a mapping and an iterator as items are
    untrusted. Kills the mutant *a list counts zero* (the list plan would be
    untrusted or count 0). Negative control, changing only the container: the
    same items as a tuple give the same decision."""
    w = World(tmp_path, free=25 * GiB, fake=fake)
    items = [item(w.files["pa"][0], 4 * GiB, "VERIFIED_DUPLICATE"),
             item(w.files["pa"][1], 3 * GiB)]
    decision = w.coordinator(ListSteward("pa", list(items))).on_pressure()
    assert decision.untrusted == () and decision.reclaimable_bytes == 7 * GiB
    [plan] = decision.accepted
    assert isinstance(plan.items, tuple) and plan.items == tuple(items)
    assert [i.classification for i in plan.items] == ["VERIFIED_DUPLICATE",
                                                      "REPRODUCIBLE_SCRATCH"]
    assert plan.total_bytes == 7 * GiB and decision.verdict == SUFFICIENT

    for raw in (str(w.files["pa"][0]), {"a": items[0]}, iter(items)):
        bad = w.coordinator(ListSteward("pa", raw)).on_pressure()
        assert bad.untrusted == ("pa",) and bad.reclaimable_bytes == 0, type(raw)

    control = w.coordinator(ListSteward("pa", tuple(items))).on_pressure()
    assert control.accepted == decision.accepted
    assert control.reclaimable_bytes == decision.reclaimable_bytes


# --------------------------------------------------------------------------- #
# The eleven scoord-a survivors (Revision 5 item 6), each killed by a test
# --------------------------------------------------------------------------- #


def test_mutant_owner_of_error_accepted(tmp_path, fake, monkeypatch):
    """Catches an item whose `owner_of` raises being skipped yet counted:
    `owner_of` raises for one of `pa`'s two owned files; the item is
    rejected as `UNKNOWN: owner_of failed`, the plan is untrusted and counts
    zero. Kills *owner_of_error_accepted*. Negative control, changing only
    the raise: both files are accepted, 2 bytes."""
    w = World(tmp_path, free=25 * GiB, fake=fake)
    real = w.registry.owner_of
    failing = w.files["pa"][1]

    def owner_of(path):
        if Path(path) == failing:
            raise OSError("EIO")
        return real(path)

    plan = [item(w.files["pa"][0]), item(failing)]
    monkeypatch.setattr(w.registry, "owner_of", owner_of)
    decision = w.coordinator(FakeSteward("pa", plan)).on_pressure()
    assert decision.untrusted == ("pa",) and decision.reclaimable_bytes == 0
    [rej] = decision.rejected
    assert rej.path == str(failing) and rej.reason.startswith("UNKNOWN: owner_of failed")
    monkeypatch.setattr(w.registry, "owner_of", real)
    assert w.coordinator(FakeSteward("pa", plan)).on_pressure().reclaimable_bytes == 2


def test_mutant_plan_project_check_removed(tmp_path, fake):
    """Catches a plan that names another project being judged as the
    steward's: `pa`'s steward returns a plan labelled `pb` whose items are
    `pa`'s own files (so every item would pass `owner_of`); it is untrusted
    with a reason naming `pb`. Kills *plan_project_check_removed*. Negative
    control, changing only the label: the plan labelled `pa` is accepted."""

    class Labelled(FakeSteward):
        label = "pb"

        def candidate_plan(self, pressure):
            return CandidatePlan(self.label, tuple(self.items))

    w = World(tmp_path, free=25 * GiB, fake=fake)
    steward = Labelled("pa", [item(w.files["pa"][0], 5)])
    decision = w.coordinator(steward).on_pressure()
    assert decision.untrusted == ("pa",) and decision.reclaimable_bytes == 0
    assert "'pb'" in decision.rejected[0].reason
    steward.label = "pa"
    assert w.coordinator(steward).on_pressure().reclaimable_bytes == 5


def test_mutant_dup_check_removed(tmp_path, fake):
    """Catches the same object counted twice: one owned file proposed under
    two names (`sub/x.bin` and `alias/x.bin`, `alias` a symlink to `sub`
    inside the same scratch directory, both `OWNED` with one resolved path);
    both items are rejected as appearing more than once and the plan counts
    zero. Kills *dup_check_removed* (equal paths are neither inside nor
    ancestors of each other, so only the equality rule catches them; the
    standing `..` spelling is `UNKNOWN` to `owner_of` and never reached this
    rule). Negative control, changing only the second name: two distinct
    owned files are accepted, 10 bytes."""
    w = World(tmp_path, free=25 * GiB, fake=fake)
    sub = w.scratch["pa"] / "sub"
    sub.mkdir()
    f = sub / "x.bin"
    f.write_bytes(b"x")
    os.symlink("sub", w.scratch["pa"] / "alias")
    again = w.scratch["pa"] / "alias" / "x.bin"
    assert w.registry.owner_of(again).status == OWNED
    twice = [item(f, 5), item(again, 5)]
    decision = w.coordinator(FakeSteward("pa", twice)).on_pressure()
    assert decision.untrusted == ("pa",) and decision.reclaimable_bytes == 0
    assert len(decision.rejected) == 2
    assert all("more than once" in r.reason for r in decision.rejected)
    distinct = [item(f, 5), item(w.files["pa"][1], 5)]
    assert w.coordinator(FakeSteward("pa", distinct)).on_pressure().reclaimable_bytes == 10


def test_mutant_status_not_copied(tmp_path, fake):
    """Catches a steward that can rewrite the coordinator's view of the
    pressure: the steward overwrites the status it is handed (free space
    10**15, reserve 0, no reservations); the decision still computes the
    5 GiB shortfall from the admission's own status: `NEEDS_YOU`, 4 GiB
    missing. Kills *status_not_copied*. Negative control, changing only the
    rewrite: an honest steward gets the same decision."""

    class Rewriting(FakeSteward):
        rewrite = True

        def candidate_plan(self, pressure):
            if self.rewrite:
                pressure.update(effective_free_bytes=10 ** 15, reserve_bytes=0,
                                reserved_bytes=0)
            return super().candidate_plan(pressure)

    w = World(tmp_path, free=25 * GiB, fake=fake)
    steward = Rewriting("pa", [item(w.files["pa"][0], 1 * GiB)])
    decision = w.coordinator(steward).on_pressure()
    assert decision.shortfall_bytes == 5 * GiB and decision.verdict == NEEDS_YOU
    assert f"missing {4 * GiB} bytes" in decision.message
    steward.rewrite = False
    honest = w.coordinator(steward).on_pressure()
    assert (honest.shortfall_bytes, honest.verdict, honest.message) == (
        decision.shortfall_bytes, decision.verdict, decision.message)


def test_mutant_shortfall_ignores_reserved(tmp_path, fake):
    """Catches a shortfall that ignores open reservations: 100 GiB free
    admits a 10 GiB reservation, then free drops to 35 GiB: the shortfall is
    reserve 30 + reserved 10 - 35 = 5 GiB, so a 1 GiB plan is `NEEDS_YOU`
    with 4 GiB missing. Kills *shortfall_ignores_reserved* (shortfall 0,
    SUFFICIENT). Negative control, changing only the reservation: in a world
    without it, 35 GiB free means no shortfall and SUFFICIENT."""
    for reserved in (True, False):
        w = World(tmp_path / str(reserved), free=100 * GiB, fake=FakeOS())
        if reserved:
            assert w.admission.reserve("pa", "clone", estimate_bytes=10 * GiB).admitted
        w.statvfs.free = 35 * GiB
        steward = FakeSteward("pa", [item(w.files["pa"][0], 1 * GiB)])
        decision = w.coordinator(steward).on_pressure()
        if reserved:
            assert decision.shortfall_bytes == 5 * GiB and decision.verdict == NEEDS_YOU
            assert f"missing {4 * GiB} bytes" in decision.message
        else:
            assert decision.shortfall_bytes == 0 and decision.verdict == SUFFICIENT


def test_mutant_session_lease_not_checked(tmp_path, fake):
    """Catches `coordinate` running for a session whose registry lease is
    gone: after `s1` drove once, the registry closes `s1` directly; the
    coordinator still has `s1` in memory, yet `coordinate` is refused, `fn`
    does not run and `DRIVER.json` is byte-identical. Kills
    *session_lease_not_checked* (the driver would read dead by rule 6 and
    `s1` would supersede it and run). Negative control, changing only the
    lease: before the close, `s1` coordinates (sequence 2)."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    c1, lease = drive(w, 100)
    assert c1.coordinate("pa", "s1", lambda: None)[0] == 2
    before = w.driver("pa").read_bytes()
    w.registry.close_session("pa", "s1", lease)
    ran: list[int] = []
    with pytest.raises(StorageCoordinatorError, match="not live or no longer the one"):
        c1.coordinate("pa", "s1", lambda: ran.append(1))
    assert ran == [] and w.driver("pa").read_bytes() == before and versions(w) == []


def test_mutant_no_excl(tmp_path):
    """Catches a first creation that overwrites: `_create_exclusive` on an
    existing record is refused and leaves its bytes as they were. Kills
    *no_excl*. Negative control, changing only the existence: on a fresh
    name it creates the record."""
    path = tmp_path / "DRIVER.json"
    path.write_text("held by someone else, much longer than the new record\n")
    with pytest.raises(StorageCoordinatorError, match="appeared concurrently"):
        sc._create_exclusive(path, {"sequence": 1})
    assert path.read_text() == "held by someone else, much longer than the new record\n"
    fresh = tmp_path / "fresh.json"
    sc._create_exclusive(fresh, {"sequence": 1})
    assert json.loads(fresh.read_text()) == {"sequence": 1}


def test_mutant_marker_any_accepted(tmp_path, fake):
    """Catches a marker adopted without checking it: markers with another
    schema, owner, kind or project, an unreadable one and a symlink to a
    valid marker are each refused, and no `DRIVER.json` appears. Kills
    *marker_any_accepted*. Negative control, changing only the marker: a
    faithful copy of the coordinator's own marker is accepted."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    good = {"schema": sc.MARKER_SCHEMA, "owner": "veriharness", "project_id": "pa",
            "kind": "coordinator", "created_utc": "2026-10-02T00:00:00+00:00"}
    directory = w.state / "coordinator" / "pa"
    directory.mkdir(parents=True)
    marker = directory / ".vh-owner.json"
    elsewhere = tmp_path / "valid-marker.json"
    elsewhere.write_text(json.dumps(good), encoding="utf-8")
    c = w.coordinator(FakeSteward("pa"), getpid=lambda: 100)
    c.acquire_session("pa", "s1", BOOT)
    bad = [{**good, "schema": "x"}, {**good, "owner": "x"}, {**good, "kind": "x"},
           {**good, "project_id": "pb"}, "{ not json", "symlink"]
    for k, content in enumerate(bad):
        target = directory / f".marker-{k}"
        if content == "symlink":
            os.symlink(elsewhere, target)
        else:
            target.write_text(content if isinstance(content, str) else json.dumps(content),
                              encoding="utf-8")
        if os.path.lexists(marker):
            os.rename(marker, directory / f".marker-old-{k}")
        os.rename(target, marker)
        with pytest.raises(StorageCoordinatorError, match="not this coordinator's marker"):
            c.coordinate("pa", "s1", lambda: None)
        assert not os.path.lexists(w.driver("pa"))
    os.rename(marker, directory / ".marker-old-last")
    marker.write_text(json.dumps(good), encoding="utf-8")
    assert c.coordinate("pa", "s1", lambda: None)[0] == 1


def test_mutant_driver_without_marker_adopted(tmp_path, fake):
    """Catches a `DRIVER.json` adopted although no marker vouches for it: a
    well-formed driver record in `coordinator/pa/` without
    `.vh-owner.json` is refused, stays byte-identical, and no marker is
    written. Kills *driver_without_marker_adopted*. Negative control,
    changing only the stray record: a fresh project directory gets its
    marker and driver lease."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    directory = w.state / "coordinator" / "pa"
    directory.mkdir(parents=True)
    stray = {"schema": sc.DRIVER_SCHEMA, "project_id": "pa", "session_id": "s9",
             "pid": 999, "pid_start": 1, "boot_id": "boot-0", "since_utc": "x",
             "sequence": 7, "superseded": 0}
    w.driver("pa").write_text(json.dumps(stray), encoding="utf-8")
    before = w.driver("pa").read_bytes()
    c = w.coordinator(FakeSteward("pa"), FakeSteward("pb"), getpid=lambda: 100)
    c.acquire_session("pa", "s1", BOOT)
    with pytest.raises(StorageCoordinatorError, match="without a marker"):
        c.coordinate("pa", "s1", lambda: None)
    assert w.driver("pa").read_bytes() == before
    assert not os.path.lexists(directory / ".vh-owner.json")
    c.acquire_session("pb", "s1", BOOT)
    assert c.coordinate("pb", "s1", lambda: None)[0] == 1


def test_mutant_symlink_dir_accepted(tmp_path, fake):
    """Catches a coordinator directory reached through a symlink:
    `coordinator/pa` as a symlink to a directory elsewhere, and
    `coordinator/` itself as one, are refused and nothing is written at the
    targets. Kills *symlink_dir_accepted*. Negative control, changing only
    the symlink: a plain `coordinator/pb/` is used."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (w.state / "coordinator").mkdir()
    os.symlink(target, w.state / "coordinator" / "pa")
    c = w.coordinator(FakeSteward("pa"), FakeSteward("pb"), getpid=lambda: 100)
    c.acquire_session("pa", "s1", BOOT)
    with pytest.raises(StorageCoordinatorError, match="not a plain directory"):
        c.coordinate("pa", "s1", lambda: None)
    assert snapshot(target) == {} and not any(target.iterdir())

    c.acquire_session("pb", "s1", BOOT)
    assert c.coordinate("pb", "s1", lambda: None)[0] == 1

    w2 = World(tmp_path / "w2", free=60 * GiB, fake=FakeOS())
    target2 = tmp_path / "elsewhere2"
    target2.mkdir()
    os.symlink(target2, w2.state / "coordinator")
    c2 = w2.coordinator(FakeSteward("pa"), getpid=lambda: 100)
    c2.acquire_session("pa", "s1", BOOT)
    with pytest.raises(StorageCoordinatorError, match="not a plain directory"):
        c2.coordinate("pa", "s1", lambda: None)
    assert not any(target2.iterdir())


def test_mutant_reader_error_live(tmp_path, fake):
    """Catches a liveness reader that raises being read as an answer: while
    pid 100 drives `pa`, a coordinator whose boot-id reader, pid-existence
    reader or start-time reader raises refuses session `s2`, with
    `DRIVER.json` byte-identical -- a reader error is UNKNOWN and refuses
    takeover (it neither keeps the driver "live" for its holder nor hands
    it over as dead). Kills *reader_error_live* in its Revision 5c sense
    (reader error treated as dead would hand `s2` the driver). Negative
    control, changing only the reader: one that answers "absent" lets `s2`
    take over with sequence 2."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    drive(w, 100)
    before = w.driver("pa").read_bytes()
    c2 = w.coordinator(FakeSteward("pa"), getpid=lambda: 200)
    c2.acquire_session("pa", "s2", BOOT)

    def boom(*args):
        raise OSError("reader failed")

    for kw in ({"boot_id_reader": boom}, {"pid_exists": boom}, {"pid_start_reader": boom}):
        coord = w.coordinator(FakeSteward("pa"), getpid=lambda: 200, **kw)
        coord._sessions.update(c2._sessions)
        with pytest.raises(StorageCoordinatorError, match="cannot be proven dead"):
            coord.coordinate("pa", "s2", lambda: None)
        assert w.driver("pa").read_bytes() == before and versions(w) == []

    absent = w.coordinator(FakeSteward("pa"), getpid=lambda: 200,
                           pid_exists=lambda pid: pid != 100 and fake.pid_exists(pid))
    absent._sessions.update(c2._sessions)
    assert absent.coordinate("pa", "s2", lambda: None)[0] == 2


# --------------------------------------------------------------------------- #
# STORAGE-D Revision 11 item 1 -- explicit refusal reasons, an atomic DRIVER.json
# --------------------------------------------------------------------------- #


def _race_record(project: str, session: str, pid: int, start: int) -> dict:
    return {"schema": sc.DRIVER_SCHEMA, "project_id": project, "session_id": session,
            "pid": pid, "pid_start": start, "boot_id": BOOT,
            "since_utc": "2026-10-02T00:00:00Z", "sequence": 7, "superseded": 0}


def test_eexist_race_live_only_if_record_live(tmp_path, fake, monkeypatch):
    """Catches an ``EEXIST`` race on `DRIVER.json` always read as a live
    holder (STORAGE-D Revision 11 item 1): while session `s1` of pid 100
    writes its new record's temporary file, another record takes the name
    `DRIVER.json` -- the publish (``RENAME_NOREPLACE``) finds it taken.
    `_advance_driver` re-reads that record: one naming live session `s2` of
    pid 200 is a refusal with reason ``LIVE_HOLDER``; one naming a dead pid
    is a refusal of another reason (``EXISTS``, never ``LIVE_HOLDER``). In
    both, `fn` does not run and the record that took the name is not
    replaced. Mutant: ``EEXIST`` always ``LIVE_HOLDER`` (the dead case).
    Negative control, without a race: `s1` drives with sequence 1."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    other = w.coordinator(FakeSteward("pa"), getpid=lambda: 200)
    other.acquire_session("pa", "s2", BOOT)      # s2's registry lease names pid 200
    real_write = sc._write_whole
    racing: dict = {}

    def racing_write(fd, data):
        record = racing.pop("record", None)
        if record is not None:                  # the other record takes the name now
            w.driver(racing["project"]).write_text(json.dumps(record), encoding="utf-8")
        real_write(fd, data)

    monkeypatch.setattr(sc, "_write_whole", racing_write)
    cases = (("pa", _race_record("pa", "s2", 200, fake.procs[200]), sc.LIVE_HOLDER),
             ("pb", _race_record("pb", "s9", 999, 1234), sc.EXISTS))
    for project, record, reason in cases:
        c = w.coordinator(FakeSteward(project), getpid=lambda: 100)
        c.acquire_session(project, "s1", BOOT)
        racing.update(record=record, project=project)
        ran: list[int] = []
        with pytest.raises(StorageCoordinatorError, match="appeared concurrently") as caught:
            c.coordinate(project, "s1", lambda ran=ran: ran.append(1))
        assert caught.value.reason == reason, (project, caught.value.reason)
        assert (caught.value.reason == sc.LIVE_HOLDER) is (reason == sc.LIVE_HOLDER)
        assert ran == []
        assert json.loads(w.driver(project).read_text(encoding="utf-8")) == record
    # negative control: no race
    third = World(tmp_path / "control", free=60 * GiB, fake=FakeOS())
    c = third.coordinator(FakeSteward("pa"), getpid=lambda: 100)
    c.acquire_session("pa", "s1", BOOT)
    assert c.coordinate("pa", "s1", lambda: "ran") == (1, "ran")


def test_driver_write_without_renameat2_fails(tmp_path, fake, monkeypatch):
    """Catches a link fallback when ``renameat2`` is not available (Revision
    11 item 1): with `archive_bundle._renameat2` answering None, `coordinate`
    is refused with reason ``OS_ERROR`` (never ``LIVE_HOLDER``), `fn` does not
    run, no `DRIVER.json` exists, and the temporary file stays with a single
    link (no second name was made). Mutant: a link fallback (`DRIVER.json`
    would appear as a second link of the temporary file). Negative control,
    changing only the availability: with ``renameat2`` back, the same session
    drives with sequence 1."""
    import hoh.archive_bundle as ab

    w = World(tmp_path, free=60 * GiB, fake=fake)
    c = w.coordinator(FakeSteward("pa"), getpid=lambda: 100)
    c.acquire_session("pa", "s1", BOOT)
    real = ab._renameat2
    monkeypatch.setattr(ab, "_renameat2", lambda: None)
    ran: list[int] = []
    with pytest.raises(StorageCoordinatorError, match="renameat2") as caught:
        c.coordinate("pa", "s1", lambda: ran.append(1))
    assert caught.value.reason == sc.OS_ERROR and ran == []
    assert not os.path.lexists(w.driver("pa"))
    [temp] = list(w.driver("pa").parent.glob(".DRIVER.json.tmp-*"))
    assert os.lstat(temp).st_nlink == 1
    monkeypatch.setattr(ab, "_renameat2", real)
    assert c.coordinate("pa", "s1", lambda: None)[0] == 1
    assert json.loads(w.driver("pa").read_text())["sequence"] == 1


def test_driver_record_written_atomically(tmp_path, fake, monkeypatch):
    """Catches `DRIVER.json` written in place (Revision 11 item 1): a write
    that stops halfway -- half the record written, then ENOSPC -- is refused
    with reason ``OS_ERROR``, leaves no `DRIVER.json` (so no torn record that
    would later read as malformed), and the half-written temporary file
    stays. Mutant: written in place under the final name (a torn
    `DRIVER.json` remains). Negative control, changing only the write: the
    next `coordinate` publishes a whole record (sequence 1) that parses."""
    w = World(tmp_path, free=60 * GiB, fake=fake)
    c = w.coordinator(FakeSteward("pa"), getpid=lambda: 100)
    c.acquire_session("pa", "s1", BOOT)
    real_write = sc._write_whole

    def half_then_full(fd, data):
        real_write(fd, data[: len(data) // 2])
        raise OSError(28, "No space left on device (simulated)")

    monkeypatch.setattr(sc, "_write_whole", half_then_full)
    with pytest.raises(StorageCoordinatorError, match="No space left") as caught:
        c.coordinate("pa", "s1", lambda: None)
    assert caught.value.reason == sc.OS_ERROR
    assert not os.path.lexists(w.driver("pa"))
    [temp] = list(w.driver("pa").parent.glob(".DRIVER.json.tmp-*"))
    assert 0 < temp.stat().st_size and not temp.read_text().rstrip().endswith("}")
    monkeypatch.setattr(sc, "_write_whole", real_write)
    assert c.coordinate("pa", "s1", lambda: None)[0] == 1
    assert json.loads(w.driver("pa").read_text())["sequence"] == 1
