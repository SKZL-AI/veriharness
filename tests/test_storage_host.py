"""WSL and native-Ubuntu storage host adapters (STORAGE-F, P5-32 and P5-33).

No test calls `wsl.exe`, `powershell.exe` or any Windows binary, and none
touches the real `/mnt/c`: every host probe (`statvfs`, `stat`, `ismount`,
`/proc/version`, the environment) is injected (`FakeHost`). The registry
tests use a real `StorageRegistry` on `tmp_path` with injected OS readers
(`FakeOS`), `HOME` and git configuration confined to `tmp_path`, as
`tests/test_storage_registry.py` does; the only real process calls are git in
the fixtures and the one `git fsck` of `post_restart_checks`.

The host's boot is injected too: an inventory is taken in `HOST_BOOT_BEFORE`
and the checks run in `HOST_BOOT_AFTER` (the restart in between), because an
inventory from the current boot is refused (Revision 9 item 3).

Each test name contains the selector it is run by; each docstring names the
failure (the mutant) it catches and the negative control that changes only
the condition under test.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import hashlib
import inspect
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hoh import storage_host as sh
from hoh.contracts import RunState
from hoh.storage_host import (
    HOST_OK,
    HOST_UNKNOWN,
    NO_INVENTORY,
    NOT_CHECKED,
    UNKNOWN_SKIPPED,
    WSL_HOST_RECLAIM_REQUIRED,
    HostConfig,
    NativeLinuxAdapter,
    StorageHostError,
    WslHostAdapter,
    adapter_for_profile,
    detect_host_profile,
    post_restart_checks,
    pre_restart_inventory,
)
from hoh.storage_pressure import (
    GiB,
    GuestObserver,
    NoHost,
    Refusal,
    Reservation,
    StatvfsHostObserver,
    StorageAdmission,
    StoragePolicy,
)
from hoh.storage_registry import OWNED, PROTECTED, UNKNOWN, SessionLease, StorageRegistry

MODULE_PATH = Path(sh.__file__)
REGISTRY_PATH = Path(sh.sr.__file__)
BOOT = "boot-1"
#: The host's boot before and after the operator's restart (injected into
#: `pre_restart_inventory` and `post_restart_checks`, independent of the
#: registry's lease boot `BOOT`).
HOST_BOOT_BEFORE = "host-boot-before"
HOST_BOOT_AFTER = "host-boot-after"

WSL_VERSION = ("Linux version 6.6.87.2-microsoft-standard-WSL2 (root@439a258ad544) "
               "(gcc (GCC) 11.2.0) #1 SMP PREEMPT_DYNAMIC Thu Jun  5 18:30:46 UTC 2025\n")
NATIVE_VERSION = ("Linux version 6.8.0-45-generic (buildd@lcy02-amd64-075) "
                  "(x86_64-linux-gnu-gcc-13 (Ubuntu 13.2.0-23ubuntu4) 13.2.0) #45-Ubuntu SMP\n")
VHDX = "/mnt/c/Users/op/AppData/Local/wsl/{0b1c2d3e}/ext4.vhdx"


# --------------------------------------------------------------------------- #
# Host fakes
# --------------------------------------------------------------------------- #

GUEST_FSID, GUEST_DEV = 0x6E57, 2096


class FakeHost:
    """Injected `statvfs`, `stat` and `ismount`. Any path not declared as a
    host mount or a file is the guest (one filesystem: `GUEST_FSID`,
    `GUEST_DEV`). A host mount has its own fsid and st_dev unless declared
    `same_fs`; free `None` makes its `statvfs` raise. `files` maps a path to
    `(st_blocks, st_size)`; a path under a host mount that is not a file
    raises `FileNotFoundError`. Every call is recorded."""

    def __init__(self, guest_free: int = 300 * GiB, guest_used: int = 200 * GiB) -> None:
        self.guest_free = guest_free
        self.guest_used = guest_used
        self.mounts: dict[str, int | None] = {}
        self.mounted: set[str] = set()
        self.same_fs: set[str] = set()
        self.files: dict[str, tuple[int, int]] = {}
        self.calls: list[tuple[str, str]] = []

    def host(self, path: str, free: int | None, *, mounted: bool = True,
             same_fs: bool = False) -> "FakeHost":
        self.mounts[path] = free
        if mounted:
            self.mounted.add(path)
        if same_fs:
            self.same_fs.add(path)
        return self

    def _index(self, key: str) -> int:
        return list(self.mounts).index(key) + 1

    def _under_mount(self, key: str) -> bool:
        return any(key.startswith(m + "/") for m in self.mounts)

    def statvfs(self, path):
        key = str(path)
        self.calls.append(("statvfs", key))
        if key in self.mounts:
            free = self.mounts[key]
            if free is None:
                raise OSError(5, "Input/output error", key)
            fsid = GUEST_FSID if key in self.same_fs else 0xC000 + self._index(key)
            return SimpleNamespace(f_bavail=free, f_frsize=1, f_favail=10_000_000,
                                   f_fsid=fsid, f_blocks=free * 4, f_bfree=free)
        if self._under_mount(key):
            raise FileNotFoundError(2, "No such file or directory", key)
        return SimpleNamespace(f_bavail=self.guest_free, f_frsize=1, f_favail=10_000_000,
                               f_fsid=GUEST_FSID, f_blocks=self.guest_free + self.guest_used,
                               f_bfree=self.guest_free)

    def stat(self, path):
        key = str(path)
        self.calls.append(("stat", key))
        if key in self.files:
            blocks, size = self.files[key]
            return SimpleNamespace(st_blocks=blocks, st_size=size, st_dev=70)
        if key in self.mounts:
            dev = GUEST_DEV if key in self.same_fs else 60 + self._index(key)
            return SimpleNamespace(st_dev=dev, st_blocks=8, st_size=4096)
        if self._under_mount(key):
            raise FileNotFoundError(2, "No such file or directory", key)
        return SimpleNamespace(st_dev=GUEST_DEV, st_blocks=8, st_size=4096)

    def ismount(self, path) -> bool:
        self.calls.append(("ismount", str(path)))
        return str(path) in self.mounted

    def probes(self) -> dict:
        return {"statvfs": self.statvfs, "stat": self.stat, "ismount": self.ismount}


def wsl(fake: FakeHost, **config) -> WslHostAdapter:
    return WslHostAdapter(HostConfig(**config), **fake.probes())


NS = "pid:[4026532229]"


def admission(root: Path, host, guest: FakeHost, policy: StoragePolicy | None = None
              ) -> StorageAdmission:
    """STORAGE-B2's canonical admission on a `tmp_path` state root, with the
    guest observed through `guest.statvfs` and `host` as the host observer.
    The boot is claimed here, as a host entry point does before its first
    `reserve()` (STORAGE-B3, Revisions 8-9)."""
    root.mkdir(parents=True, exist_ok=True)
    observer = GuestObserver(root, host, statvfs=guest.statvfs)
    adm = StorageAdmission.for_state_root(
        root, policy or StoragePolicy(), observer,
        pid_alive=lambda pid: True, proc_start_time=lambda pid: 111,
        boot_id=lambda: "boot-A", pid_namespace=lambda: NS,
    )
    adm.claim_boot()
    return adm


# --------------------------------------------------------------------------- #
# Registry fakes and fixtures (re-declared, not imported from another test)
# --------------------------------------------------------------------------- #


class FakeOS:
    """Injected OS readers for the registry: a process table `{pid: start
    time}`, a boot id, no mount points, mount id 1 everywhere, real st_dev."""

    def __init__(self) -> None:
        self.procs: dict[int, int] = {100: 5000, 200: 6000, 300: 7000}
        self.boot = BOOT

    def boot_id(self) -> str:
        return self.boot

    def pid_start(self, pid: int) -> int | None:
        return self.procs.get(pid)

    def pid_exists(self, pid: int) -> bool:
        return pid in self.procs

    def is_mount(self, path: Path) -> bool:
        return False

    def mount_id(self, path: Path) -> int | None:
        return 1

    def st_dev(self, path: Path) -> int:
        return os.stat(path).st_dev


@pytest.fixture
def fake() -> FakeOS:
    return FakeOS()


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """HOME and git configuration confined to `tmp_path`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Host Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "host@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Host Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "host@example.invalid")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "WSL_DISTRO_NAME"):
        monkeypatch.delenv(name, raising=False)


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                         env=dict(os.environ), check=True)
    return out.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "README").write_text("project\n", encoding="utf-8")
    git(path, "add", "README")
    git(path, "commit", "-q", "-m", "init")
    return path


def corrupt_tree_object(repo: Path) -> Path:
    """Overwrite the loose object of HEAD's tree with garbage (a corrupt git
    object `git fsck --connectivity-only` reads)."""
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    obj = repo / ".git" / "objects" / tree[:2] / tree[2:]
    obj.chmod(0o644)
    obj.write_bytes(b"garbage, not zlib")
    return obj


def run_state(run_id: str) -> str:
    return RunState(
        run_id=run_id, repo_path="/repo", project_name="demo", spec_path="spec.md",
        spec_digest="s" * 8, policy_digest="p" * 8, profile_digest="f" * 8,
    ).model_dump_json()


def add_runs(base: Path, *run_ids: str) -> Path:
    for run_id in run_ids:
        (base / run_id).mkdir()
        (base / run_id / "state.json").write_text(run_state(run_id), encoding="utf-8")
    return base


def truncate(path: Path) -> None:
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])


def new_registry(base: Path, fake: FakeOS) -> StorageRegistry:
    state = base / "vh" / "state"
    state.parent.mkdir(parents=True, exist_ok=True)
    return StorageRegistry(
        state,
        forbidden_roots=[base / "home" / "user"],
        boot_id_reader=fake.boot_id,
        pid_start_reader=fake.pid_start,
        pid_exists=fake.pid_exists,
        mount_check=fake.is_mount,
        mount_id_reader=fake.mount_id,
        st_dev_reader=fake.st_dev,
    )


def admit(reg: StorageRegistry, *parents: Path) -> None:
    """The operator's `CONFIG.json` naming admissible external parents."""
    path = reg.state_root / "CONFIG.json"
    tmp = reg.state_root / ".CONFIG.json.tmp.operator"
    tmp.write_text(json.dumps({"schema": "veriharness.storage-config/1", "sequence": 1,
                               "admissible_external_parents": [str(p) for p in parents]}),
                   encoding="utf-8")
    os.replace(tmp, path)


class World:
    """A registry with project `a` (repository `proj-a`) and a runs base
    `ext/runs` the registry created for `a`, holding runs `r1` and `r2`."""

    def __init__(self, base: Path, fake: FakeOS) -> None:
        base.mkdir(parents=True, exist_ok=True)
        self.base = base
        self.fake = fake
        self.reg = new_registry(base, fake)
        self.reg.init()
        self.repo = make_repo(base / "proj-a")
        self.reg.register_project("a", self.repo)
        (base / "ext").mkdir()
        admit(self.reg, base / "ext")
        self.runs = base / "ext" / "runs"
        self.reg.create_external_root("a", self.runs, "runs_base")
        add_runs(self.runs, "r1", "r2")
        self._n = 0

    def inventory(self, runs_bases=None, *, project="a", boot=HOST_BOOT_BEFORE,
                  clock=None) -> Path:
        """The runbook's step 2: own session, inventory, close the session --
        in the host boot `boot` (before the restart)."""
        self._n += 1
        sid = f"inv{self._n}"
        lease = self.reg.open_session(project, sid, pid=100, boot_id=BOOT)
        kw = {"clock": clock} if clock is not None else {}
        try:
            return pre_restart_inventory(self.reg, project, lease,
                                         runs_bases if runs_bases is not None else [self.runs],
                                         boot_id=lambda: boot, **kw)
        finally:
            self.reg.close_session(project, sid, lease)

    def check(self, *, project_roots=None, runs_bases=None, boot=HOST_BOOT_AFTER, **kw) -> dict:
        """The runbook's step 7, in the host boot `boot` (after the restart)."""
        return post_restart_checks(
            self.reg, self.reg.state_root,
            project_roots if project_roots is not None else [self.repo],
            runs_bases if runs_bases is not None else [self.runs],
            boot_id=lambda: boot, **kw)


def failing(report: dict) -> list[tuple[str, str, str]]:
    return [(c["check"], Path(c["path"]).name, c["status"]) for c in report["failing"]]


def snapshot(root: Path) -> dict[str, tuple]:
    """Every path under `root`: type, size, mode, mtime and content hash."""
    out: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            st = os.lstat(path)
            if path.is_symlink():
                digest = os.readlink(path)
            elif path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                digest = "dir"
            out[str(path.relative_to(root))] = (st.st_mode, st.st_size, st.st_mtime_ns,
                                                st.st_nlink, digest)
    return out


# --------------------------------------------------------------------------- #
# Profile detection
# --------------------------------------------------------------------------- #


def test_profile_detection_both_ways():
    """Catches a detector that always answers one profile or reads the real
    machine: the WSL kernel string or `WSL_DISTRO_NAME` give `wsl2`; the
    Ubuntu kernel string without the variable gives `native-linux` (negative
    control, only the string changes); unreadable or empty `/proc/version`
    gives `unknown`, and `unknown` gets no adapter."""
    assert detect_host_profile(proc_version=WSL_VERSION, env={}) == "wsl2"
    assert detect_host_profile(proc_version=NATIVE_VERSION, env={}) == "native-linux"
    assert detect_host_profile(proc_version=NATIVE_VERSION,
                               env={"WSL_DISTRO_NAME": "Ubuntu-24.04"}) == "wsl2"
    assert detect_host_profile(proc_version=lambda: NATIVE_VERSION, env={}) == "native-linux"

    def unreadable() -> str:
        raise OSError(2, "No such file or directory", "/proc/version")

    assert detect_host_profile(proc_version=unreadable, env={}) == "unknown"
    assert detect_host_profile(proc_version="", env={}) == "unknown"
    assert detect_host_profile(proc_version="Darwin Kernel Version 23", env={}) == "unknown"

    assert isinstance(adapter_for_profile("wsl2"), WslHostAdapter)
    assert isinstance(adapter_for_profile("native-linux"), NativeLinuxAdapter)
    with pytest.raises(StorageHostError):
        adapter_for_profile("unknown")


# --------------------------------------------------------------------------- #
# K1 -- the WSL adapter observes the host read-only and feeds admission
# --------------------------------------------------------------------------- #


def test_wsl_observes_host_free():
    """Catches `observe()` returning the guest's numbers (K1 mutant) or the
    first path it can read: guest 300 GiB, `/mnt/c` 40 GiB -> 40 GiB, read
    with `statvfs('/mnt/c')`. Negative control: only the host figure changes
    (70 GiB) and the reading follows it."""
    fake = FakeHost(guest_free=300 * GiB).host("/mnt/c", 40 * GiB)
    free, note = wsl(fake).observe()
    assert free == 40 * GiB and "/mnt/c" in note
    assert ("statvfs", "/mnt/c") in fake.calls

    control = FakeHost(guest_free=300 * GiB).host("/mnt/c", 70 * GiB)
    assert wsl(control).observe()[0] == 70 * GiB

    # A configured mount elsewhere is read there.
    other = FakeHost().host("/mnt/d", 11 * GiB)
    assert wsl(other, windows_mount="/mnt/d").observe()[0] == 11 * GiB


def test_wsl_observes_unobservable_host_none_with_note():
    """Catches an unobservable host read as the guest's (plenty): `/mnt/c`
    not mounted, unreadable, or on the guest's own filesystem gives `None`
    and a note saying why. Negative control: the same mount observable reads
    its figure."""
    for fake, words in (
        (FakeHost().host("/mnt/c", 40 * GiB, mounted=False), "not a mount point"),
        (FakeHost().host("/mnt/c", None), "Input/output error"),
        (FakeHost().host("/mnt/c", 40 * GiB, same_fs=True), "guest's filesystem"),
    ):
        free, note = wsl(fake).observe()
        assert free is None and words in note, note
    assert wsl(FakeHost().host("/mnt/c", 40 * GiB)).observe()[0] == 40 * GiB
    assert WslHostAdapter.configured is True


def test_wsl_vhdx_allocation_from_config(monkeypatch):
    """Catches the apparent size instead of the allocation, a discovered
    path, and an exception on a missing VHDX: the configured path gives
    `st_blocks * 512` (not `st_size`); unconfigured gives `None` without any
    probe of a VHDX; unreadable or outside the Windows mount gives `None`;
    nothing is scanned (directory listing patched to fail). Negative
    control: only the configuration changes between `None` and a figure."""
    def no_scan(*args, **kwargs):
        raise AssertionError("the VHDX path must be configured, never discovered by scanning")

    for name in ("scandir", "listdir", "walk"):
        monkeypatch.setattr(os, name, no_scan)
    for name in ("iterdir", "glob", "rglob"):
        monkeypatch.setattr(Path, name, no_scan)

    fake = FakeHost().host("/mnt/c", 40 * GiB)
    fake.files[VHDX] = (1_000_000, 1024 * GiB)
    assert wsl(fake, vhdx_path=VHDX).vhdx_allocation() == 1_000_000 * 512

    unconfigured = FakeHost().host("/mnt/c", 40 * GiB)
    unconfigured.files[VHDX] = (1_000_000, 1024 * GiB)
    assert wsl(unconfigured).vhdx_allocation() is None
    assert not [c for c in unconfigured.calls if c[1].endswith(".vhdx")]

    missing = FakeHost().host("/mnt/c", 40 * GiB)
    assert wsl(missing, vhdx_path=VHDX).vhdx_allocation() is None
    outside = FakeHost().host("/mnt/c", 40 * GiB)
    outside.files["/srv/ext4.vhdx"] = (1_000_000, 1024 * GiB)
    assert wsl(outside, vhdx_path="/srv/ext4.vhdx").vhdx_allocation() is None
    assert not [c for c in outside.calls if c[1] == "/srv/ext4.vhdx"]


def test_wsl_small_host_lowers_effective(tmp_path):
    """Catches a WSL host observer that does not reach admission: guest
    200 GiB, Windows volume 60 GiB -> STORAGE-B2's effective free is 60 GiB
    and a 40 GiB job is refused. Negative control: only the host changes
    (500 GiB) -> effective is the guest's 200 GiB and the job is admitted."""
    fake = FakeHost(guest_free=200 * GiB).host("/mnt/c", 60 * GiB)
    host = wsl(fake).host_observer(StoragePolicy())
    assert isinstance(host, StatvfsHostObserver) and host.configured
    adm = admission(tmp_path / "small", host, fake)
    status = adm.status()
    assert status["effective_free_bytes"] == 60 * GiB < status["guest_free_bytes"] == 200 * GiB
    assert not status["host_unknown"]
    refused = adm.reserve("a", "clone", estimate_bytes=40 * GiB)
    assert isinstance(refused, Refusal) and refused.effective_free == 60 * GiB

    big = FakeHost(guest_free=200 * GiB).host("/mnt/c", 500 * GiB)
    adm2 = admission(tmp_path / "big", wsl(big).host_observer(StoragePolicy()), big)
    assert adm2.status()["effective_free_bytes"] == 200 * GiB
    assert isinstance(adm2.reserve("a", "clone", estimate_bytes=40 * GiB), Reservation)

    # policy.host_paths are observed too: the minimum over all of them counts.
    extra = FakeHost(guest_free=200 * GiB).host("/mnt/c", 500 * GiB).host("/mnt/e", 45 * GiB)
    policy = StoragePolicy(host_paths=("/mnt/e",))
    observer = wsl(extra).host_observer(policy)
    assert [str(p) for p in observer.paths] == ["/mnt/c", "/mnt/e"]
    adm3 = admission(tmp_path / "extra", observer, extra, policy)
    assert adm3.status()["effective_free_bytes"] == 45 * GiB


def test_wsl_unobservable_refuses_admission(tmp_path):
    """Catches admission on guest numbers when the Windows volume cannot be
    observed: through `host_observer()` and `StorageAdmission`, with
    `/mnt/c` not mounted and no persisted host reading, a 1 GiB job is
    refused with effective free unknown. Negative control: only the mount
    changes (mounted, 500 GiB) and the job is admitted."""
    fake = FakeHost(guest_free=500 * GiB).host("/mnt/c", 500 * GiB, mounted=False)
    adm = admission(tmp_path / "gone", wsl(fake).host_observer(StoragePolicy()), fake)
    refused = adm.reserve("a", "role_run", estimate_bytes=1 * GiB)
    assert isinstance(refused, Refusal)
    assert refused.effective_free is None and "host" in refused.reason
    assert adm.status()["host_unknown"] is True

    ok = FakeHost(guest_free=500 * GiB).host("/mnt/c", 500 * GiB)
    adm2 = admission(tmp_path / "there", wsl(ok).host_observer(StoragePolicy()), ok)
    assert isinstance(adm2.reserve("a", "role_run", estimate_bytes=1 * GiB), Reservation)


# --------------------------------------------------------------------------- #
# K2 -- WSL_HOST_RECLAIM_REQUIRED and S23 (never runs a host command)
# --------------------------------------------------------------------------- #


def assess(*, allocation: int | None, used: int | None, host: int | None, **config) -> dict:
    fake = FakeHost()
    fake.host("/mnt/c", host)
    if allocation is not None:
        fake.files[VHDX] = (allocation // 512, 1024 * GiB)
    return wsl(fake, vhdx_path=VHDX, **config).reclaim_assessment(used)


def test_reclaim_required_thresholds():
    """Catches `>` for `>=` on the reclaimable threshold, `<=` for `<` on the
    host floor, an `or` for the `and`, and a missing input read as zero:
    WSL_HOST_RECLAIM_REQUIRED exactly when the VHDX holds at least 50 GiB
    more than the guest uses and the host is below 100 GiB; each negative
    control moves one number by one step; any unknown input is HOST_UNKNOWN."""
    used = 400 * GiB
    T, F = 50 * GiB, 100 * GiB
    at = assess(allocation=used + T, used=used, host=F - 512)
    assert at["state"] == WSL_HOST_RECLAIM_REQUIRED
    assert at["reclaimable_bytes"] == T and at["host_free_bytes"] == F - 512
    assert at["vhdx_allocation_bytes"] == used + T and at["guest_used_bytes"] == used
    assert at["reclaimable_threshold"] == T and at["host_floor"] == F
    assert at["runbook"].endswith("STORAGE_HOST_RUNBOOK.md") and "operator" in at["note"]
    assert assess(allocation=used + T + 512, used=used, host=10 * GiB)["state"] \
        == WSL_HOST_RECLAIM_REQUIRED

    # Below the reclaimable threshold by one block: HOST_OK.
    assert assess(allocation=used + T - 512, used=used, host=F - 512)["state"] == HOST_OK
    # Host exactly at the floor, or above it: HOST_OK.
    assert assess(allocation=used + T, used=used, host=F)["state"] == HOST_OK
    assert assess(allocation=used + T, used=used, host=F + 512)["state"] == HOST_OK
    # Neither condition: HOST_OK.
    assert assess(allocation=used, used=used, host=F + 512)["state"] == HOST_OK

    # Configured thresholds are used, not the defaults.
    custom = assess(allocation=used + 10 * GiB, used=used, host=150 * GiB,
                    reclaimable_threshold=10 * GiB, host_floor=200 * GiB)
    assert custom["state"] == WSL_HOST_RECLAIM_REQUIRED

    # Unknown inputs: HOST_UNKNOWN, each one alone.
    for kw in ({"allocation": None, "used": used, "host": 1 * GiB},
               {"allocation": used + T, "used": None, "host": 1 * GiB},
               {"allocation": used + T, "used": used, "host": None}):
        report = assess(**kw)
        assert report["state"] == HOST_UNKNOWN and report["reclaimable_bytes"] is None, kw
    unconfigured = FakeHost().host("/mnt/c", 1 * GiB)
    assert wsl(unconfigured).reclaim_assessment(used)["state"] == HOST_UNKNOWN
    with pytest.raises(ValueError):
        assess(allocation=used + T, used=-1, host=1 * GiB)


SPAWNERS = (
    (subprocess, "run"), (subprocess, "Popen"), (subprocess, "call"),
    (subprocess, "check_call"), (subprocess, "check_output"),
    (os, "system"), (os, "popen"), (os, "execv"), (os, "execve"), (os, "execvp"),
    (os, "execvpe"), (os, "execl"), (os, "execlp"), (os, "posix_spawn"),
    (os, "posix_spawnp"), (os, "spawnv"), (os, "spawnl"), (os, "spawnve"),
    (os, "fork"), (asyncio, "create_subprocess_exec"), (asyncio, "create_subprocess_shell"),
)


def public_surface() -> set[str]:
    """Every public function and public method of the module."""
    names: set[str] = set()
    for name, value in vars(sh).items():
        if name.startswith("_") or getattr(value, "__module__", None) != sh.__name__:
            continue
        if inspect.isfunction(value):
            names.add(name)
        elif inspect.isclass(value) and not issubclass(value, BaseException):
            names.add(name)
            for attr, member in vars(value).items():
                if not attr.startswith("_") and callable(member):
                    names.add(f"{name}.{attr}")
    return names


def test_never_runs_a_host_command(tmp_path, fake, monkeypatch):
    """S23. Catches a process call in any public function or method except
    `post_restart_checks` -- e.g. a `subprocess.run` in `runbook()` (K2
    mutant) or a `wsl --shutdown` in `reclaim_assessment`: with every
    process-spawning call patched to fail, the whole public surface is
    called, including a `WSL_HOST_RECLAIM_REQUIRED` assessment and the
    runbook, and nothing is spawned. The surface is introspected, so a new
    public method fails this test until it is covered here. Negative
    control: the patch does fire on a direct call."""
    world = World(tmp_path / "w", fake)            # git runs here, before the patch
    lease = world.reg.open_session("a", "inv", pid=100, boot_id=BOOT)
    spawned: list[str] = []

    def refuse(name):
        def call(*args, **kwargs):
            spawned.append(name)
            raise AssertionError(f"{name} called: the product never runs a host command")
        return call

    for module, name in SPAWNERS:
        monkeypatch.setattr(module, name, refuse(f"{module.__name__}.{name}"))

    called: set[str] = set()
    profile = detect_host_profile(proc_version=WSL_VERSION, env={})
    detect_host_profile(proc_version=NATIVE_VERSION, env={})
    called.add("detect_host_profile")

    host = FakeHost(guest_free=50 * GiB).host("/mnt/c", 20 * GiB)
    host.files[VHDX] = (900 * GiB // 512, 1024 * GiB)
    config = HostConfig(vhdx_path=VHDX, workspace_volume="/mnt/c")
    called.add("HostConfig")
    adapter = adapter_for_profile(profile, config, **host.probes())
    called.add("adapter_for_profile")
    assert isinstance(adapter, WslHostAdapter)
    called.add("WslHostAdapter")
    adapter.observe()
    adapter.vhdx_allocation()
    adapter.host_observer(StoragePolicy())
    used = adapter.guest_used_bytes()
    report = adapter.reclaim_assessment(used)
    assert report["state"] == WSL_HOST_RECLAIM_REQUIRED
    text = adapter.runbook()
    assert "wsl --shutdown" in text
    called |= {f"WslHostAdapter.{m}" for m in (
        "observe", "vhdx_allocation", "host_observer", "guest_used_bytes",
        "reclaim_assessment", "runbook")}

    native = NativeLinuxAdapter(config, statvfs=host.statvfs, ismount=host.ismount)
    called.add("NativeLinuxAdapter")
    native.observe()
    native.host_observer()
    native.reclaim_assessment(used)
    native.workspace_volume()
    called |= {f"NativeLinuxAdapter.{m}" for m in (
        "observe", "host_observer", "reclaim_assessment", "workspace_volume")}

    inventory = pre_restart_inventory(world.reg, "a", lease, [world.runs],
                                      boot_id=lambda: HOST_BOOT_BEFORE)
    assert inventory.exists()
    called.add("pre_restart_inventory")

    assert spawned == []
    assert public_surface() - {"post_restart_checks"} == called

    with pytest.raises(AssertionError):
        subprocess.run(["true"])
    assert spawned == ["subprocess.run"]


SPAWN_ATTRS = frozenset({
    "run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput",
    "system", "popen", "fork", "forkpty", "create_subprocess_exec", "create_subprocess_shell",
})
PROCESS_MODULES = frozenset({"subprocess", "os", "asyncio", "pty"})
REMOVAL_ATTRS = frozenset({"remove", "unlink", "rmdir", "rmtree", "removedirs", "truncate"})


def _spawn_attr(attr: str) -> bool:
    return attr in SPAWN_ATTRS or attr.startswith(("exec", "spawn", "posix_spawn"))


def process_calls(source: str) -> list[tuple[ast.Call, list[str]]]:
    """Every call into a process-spawning API, with its enclosing function
    names; also refuses import forms that would hide such a call."""
    tree = ast.parse(source)
    found: list[tuple[ast.Call, list[str]]] = []

    def visit(node: ast.AST, stack: list[str]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack = stack + [node.name]
        if isinstance(node, ast.ImportFrom) and node.module in PROCESS_MODULES | {"shutil"}:
            found.append((node, stack))         # from subprocess import run, ...
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"pty", "shutil", "multiprocessing", "ctypes", "importlib"} \
                        or (alias.name in PROCESS_MODULES and alias.asname):
                    found.append((node, stack))
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and _spawn_attr(func.attr) \
                    and isinstance(func.value, ast.Name) and func.value.id in PROCESS_MODULES:
                found.append((node, stack))
            if isinstance(func, ast.Name) and func.id in {"__import__", "eval", "exec"}:
                found.append((node, stack))
        for child in ast.iter_child_nodes(node):
            visit(child, stack)

    visit(tree, [])
    return found


def removal_calls(source: str) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in REMOVAL_ATTRS:
                out.append(name)
    return out


def test_only_process_call_is_git_fsck():
    """S23, static. Catches any second process call in the module -- e.g. a
    `subprocess.run` in `runbook()` (K2 mutant), an `os.system`, a hidden
    `from subprocess import run` -- and a `git fsck` that is not
    connectivity-only or lives outside `post_restart_checks`: the AST holds
    exactly one call into a process API, `subprocess.run`, inside
    `post_restart_checks`, with `git fsck --connectivity-only` in its argv.
    Negative control: the same checker flags the mutant source."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    calls = process_calls(source)
    assert len(calls) == 1, [(ast.dump(c)[:80], s) for c, s in calls]
    call, stack = calls[0]
    assert isinstance(call, ast.Call) and "post_restart_checks" in stack
    assert isinstance(call.func, ast.Attribute) and call.func.attr == "run" \
        and call.func.value.id == "subprocess"
    argv = call.args[0]
    assert isinstance(argv, ast.List)
    words = [e.value for e in argv.elts if isinstance(e, ast.Constant)]
    assert words[0] == "git" and "fsck" in words and "--connectivity-only" in words

    mutant = source.replace(
        "        try:\n            return Path(self.config.runbook_path).read_text",
        "        subprocess.run([\"wsl.exe\", \"--shutdown\"])\n"
        "        try:\n            return Path(self.config.runbook_path).read_text", 1)
    assert mutant != source
    flagged = process_calls(mutant)
    assert len(flagged) == 2 and any("runbook" in s for _, s in flagged)
    assert len(process_calls(source + "\nfrom subprocess import run\n")) == 2


def test_module_has_no_removal_call():
    """Catches a removal path in the module (`os.remove`, `Path.unlink`,
    `os.rmdir`, `shutil.rmtree`, ...): the AST holds no such call and no
    `shutil` import. Negative control: the checker flags an added
    `os.unlink`."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert removal_calls(source) == []
    assert "import shutil" not in source
    assert removal_calls(source + "\nos.unlink('x')\n") == ["unlink"]


# --------------------------------------------------------------------------- #
# K3 -- native Ubuntu
# --------------------------------------------------------------------------- #


def test_native_has_no_host_reclaim_state():
    """Catches a native adapter with a reclaim state or a host reading:
    whatever the numbers, `reclaim_assessment` is HOST_OK with "no
    host-reclaim state on a native host"; `observe()` is `(None, "native:
    the guest is the host")`; `host_observer()` is None (NoHost), and it is
    not configured. Negative control: the WSL adapter with the same numbers
    reports WSL_HOST_RECLAIM_REQUIRED."""
    native = NativeLinuxAdapter(HostConfig(vhdx_path=VHDX))
    for used in (None, 0, 400 * GiB):
        report = native.reclaim_assessment(used)
        assert report["state"] == HOST_OK
        assert report["note"] == "no host-reclaim state on a native host"
    assert native.observe() == (None, "native: the guest is the host")
    assert native.host_observer() is None and native.host_observer(StoragePolicy()) is None
    assert NativeLinuxAdapter.configured is False

    assert assess(allocation=900 * GiB, used=400 * GiB, host=1 * GiB)["state"] \
        == WSL_HOST_RECLAIM_REQUIRED


def test_native_admits_on_guest_numbers(tmp_path):
    """Catches a native profile that reports a host figure or reads as an
    unobservable host: the native profile's observer is None, so admission
    runs with NoHost on the guest's 200 GiB -- not host-unknown -- and a
    150 GiB job is admitted. Negative control: the WSL profile with a 60 GiB
    Windows volume, same guest, refuses it."""
    profile = detect_host_profile(proc_version=NATIVE_VERSION, env={})
    adapter = adapter_for_profile(profile, HostConfig())
    assert isinstance(adapter, NativeLinuxAdapter) and adapter.host_observer() is None
    guest = FakeHost(guest_free=200 * GiB)
    adm = admission(tmp_path / "native", adapter.host_observer(), guest)
    assert isinstance(adm.observer.host, NoHost)
    status = adm.status()
    assert status["effective_free_bytes"] == 200 * GiB and status["host_unknown"] is False
    assert isinstance(adm.reserve("a", "clone", estimate_bytes=150 * GiB), Reservation)

    small = FakeHost(guest_free=200 * GiB).host("/mnt/c", 60 * GiB)
    wsl_adapter = adapter_for_profile(detect_host_profile(proc_version=WSL_VERSION, env={}),
                                      HostConfig(), **small.probes())
    adm2 = admission(tmp_path / "wsl", wsl_adapter.host_observer(StoragePolicy()), small)
    assert isinstance(adm2.reserve("a", "clone", estimate_bytes=150 * GiB), Refusal)


def test_native_workspace_volume_reported_separately():
    """Catches a workspace volume folded into the guest or read when it is
    not a mount: a configured, mounted volume reports its own free bytes;
    unconfigured, unmounted or unreadable gives None and a note."""
    fake = FakeHost(guest_free=300 * GiB).host("/srv/work", 7 * GiB)
    native = NativeLinuxAdapter(HostConfig(workspace_volume="/srv/work"),
                                statvfs=fake.statvfs, ismount=fake.ismount)
    assert native.workspace_volume() == (7 * GiB, "workspace volume /srv/work")
    assert NativeLinuxAdapter(HostConfig()).workspace_volume()[0] is None
    unmounted = FakeHost().host("/srv/work", 7 * GiB, mounted=False)
    assert NativeLinuxAdapter(HostConfig(workspace_volume="/srv/work"),
                              statvfs=unmounted.statvfs,
                              ismount=unmounted.ismount).workspace_volume()[0] is None
    broken = FakeHost().host("/srv/work", None)
    assert NativeLinuxAdapter(HostConfig(workspace_volume="/srv/work"),
                              statvfs=broken.statvfs,
                              ismount=broken.ismount).workspace_volume()[0] is None


# --------------------------------------------------------------------------- #
# K4 -- pre-restart inventory and post-restart checks
# --------------------------------------------------------------------------- #


def test_pre_restart_inventory_needs_session_lease(tmp_path, fake):
    """Catches an inventory written without the holder's live lease (K4
    mutant): a lease rebuilt from the public record (no token), a lease of a
    dead process, a superseded lease, a live lease that is not the one on
    record, a lease of another project, and a runs base of another project
    are all refused, and nothing is written. Negative control: the live
    lease returned by `open_session` writes an OWNED receipts file listing
    the owned runs with the SHA-256 of their `state.json`."""
    world = World(tmp_path / "w", fake)
    reg = world.reg
    reg.register_project("b", make_repo(tmp_path / "w" / "proj-b"))
    lease = reg.open_session("a", "s1", pid=100, boot_id=BOOT)
    receipts = reg.state_root / "projects" / "a" / "receipts"

    def written() -> list[Path]:
        return sorted(reg.state_root.rglob("pre-restart-inventory*"))

    rebuilt = reg._read_lease("a", "s1")
    assert rebuilt == lease and rebuilt.token is None
    forged = SessionLease(project_id="a", session_id="s1", pid=100, pid_start=5000,
                          boot_id=BOOT, since_utc="2026-10-01T00:00:00+00:00",
                          sequence=lease.sequence + 99, token="f" * 64)
    wrong_token = SessionLease(**{**lease.__dict__, "token": "0" * 64})
    # The holder's real token on a lease whose process fields were altered to
    # another live process: only the comparison with the record refuses it.
    tampered = SessionLease(**{**lease.__dict__, "pid": 200, "pid_start": 6000})
    assert reg.lease_is_live(tampered) and tampered.token == lease.token
    for bad, project in ((rebuilt, "a"), (forged, "a"), (wrong_token, "a"), (tampered, "a"),
                         (lease, "b"), ("s1", "a")):
        with pytest.raises(StorageHostError):
            pre_restart_inventory(reg, project, bad, [world.runs])
    b_lease = reg.open_session("b", "sb", pid=300, boot_id=BOOT)
    with pytest.raises(StorageHostError):
        pre_restart_inventory(reg, "b", b_lease, [world.runs])   # a's runs base
    assert not receipts.exists() and written() == []
    assert not (reg.state_root / "projects" / "b" / "receipts").exists()

    # A dead holder, and then a superseded lease: refused too.
    fake.procs.pop(100)
    with pytest.raises(StorageHostError):
        pre_restart_inventory(reg, "a", lease, [world.runs])
    successor = reg.open_session("a", "s1", pid=200, boot_id=BOOT)
    fake.procs[100] = 5000
    with pytest.raises(StorageHostError):
        pre_restart_inventory(reg, "a", lease, [world.runs])
    assert not receipts.exists() and written() == []

    path = pre_restart_inventory(reg, "a", successor, [world.runs])
    own = reg.owner_of(path)
    assert own.status == OWNED and own.kind == "receipts" and own.project_id == "a"
    assert path.parent == receipts / "s1" and written() == [path]
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["project_id"] == "a" and record["session_id"] == "s1"
    assert record["boot_id"] and isinstance(record["created_ns"], int)
    assert record["created_utc"].endswith("+00:00")
    [base] = record["runs_bases"]
    assert base["path"] == str(world.runs.resolve())
    assert set(base["runs"]) == {"r1", "r2"}
    assert base["runs"]["r1"]["state_sha256"] == hashlib.sha256(
        (world.runs / "r1" / "state.json").read_bytes()).hexdigest()


def test_post_restart_detects_broken_state(tmp_path, fake):
    """Catches post-restart checks that do not look: a truncated
    `state.json` (in the registry's runs base, and in a scratch runs
    directory where the run stays OWNED and must fail to parse) and a
    corrupt git object each make the result FAIL and name the failing check.
    Negative control: the untouched world passes."""
    ok = World(tmp_path / "ok", fake)
    report = ok.check(inventory=ok.inventory())
    assert report["result"] == "PASS", report["failing"]
    assert {c["check"] for c in report["checks"]} >= {"state_root", "git_fsck", "run"}

    trunc = World(tmp_path / "trunc", fake)
    inv = trunc.inventory()
    truncate(trunc.runs / "r2" / "state.json")
    report = trunc.check(inventory=inv)
    assert report["result"] == "FAIL" and failing(report) == [("run", "r2", "FAIL")]

    scratch = World(tmp_path / "scratch", fake)
    scratch.reg.open_session("a", "runner", pid=200, boot_id=BOOT)
    runs_dir = add_runs(scratch.reg.scratch_dir("a", "runner", "runs"), "r3")
    inv = scratch.inventory([scratch.runs, runs_dir])
    truncate(runs_dir / "r3" / "state.json")
    assert scratch.reg.owner_of(runs_dir / "r3").status == OWNED
    report = scratch.check(runs_bases=[scratch.runs, runs_dir], inventory=inv)
    assert failing(report) == [("run", "r3", "FAIL")]
    assert "does not parse" in report["failing"][0]["detail"]

    broken = World(tmp_path / "git", fake)
    inv = broken.inventory()
    corrupt_tree_object(broken.repo)
    report = broken.check(inventory=inv)
    assert failing(report) == [("git_fsck", "proj-a", "FAIL")]
    assert "corrupt" in report["failing"][0]["detail"] or "error" in \
        report["failing"][0]["detail"]


def test_post_restart_read_only(tmp_path, fake):
    """Catches a check that writes, repairs, renames or removes anything:
    the whole tree (paths, types, sizes, modes, mtimes, link counts, content
    hashes) is identical before and after passing and failing runs -- a
    healthy world, a truncated state, a corrupt git object, an unowned root.
    Negative control: the snapshot does see a one-byte change."""
    world = World(tmp_path / "w", fake)
    inv = world.inventory()
    foreign = make_repo(tmp_path / "w" / "foreign")
    root = tmp_path / "w"

    before = snapshot(root)
    assert world.check(inventory=inv)["result"] == "PASS"
    assert world.check(inventory=None)["result"] == "FAIL"
    assert snapshot(root) == before

    truncate(world.runs / "r1" / "state.json")
    corrupt_tree_object(world.repo)
    broken = snapshot(root)
    report = world.check(project_roots=[world.repo, foreign], inventory=inv)
    assert report["result"] == "FAIL" and len(report["failing"]) == 3
    assert snapshot(root) == broken

    (world.runs / "timers.json").write_text("{}", encoding="utf-8")
    assert snapshot(root) != broken


def test_post_restart_checks_protected_and_project_roots(tmp_path, fake):
    """Catches checks that accept only OWNED roots (Revision 2 as written):
    the registered project repository (which `owner_of` reports UNKNOWN), a
    PROTECTED scratch directory holding a repository, the PROTECTED runs
    base and the PROTECTED state root of the calling project are all checked
    -- fsck runs on both repositories -- and the result passes. Negative
    control: the same kinds of roots of project `b` are NOT_CHECKED, fsck
    never sees them, and the result fails."""
    world = World(tmp_path / "w", fake)
    reg = world.reg
    reg.open_session("a", "dev", pid=200, boot_id=BOOT)
    scratch_repo = reg.scratch_dir("a", "dev", "scratch")
    git(scratch_repo, "init", "-q")
    (scratch_repo / "f").write_text("x", encoding="utf-8")
    git(scratch_repo, "add", "f")
    git(scratch_repo, "commit", "-q", "-m", "x")
    assert reg.owner_of(world.repo).status == UNKNOWN
    assert reg.owner_of(scratch_repo).status == PROTECTED
    assert reg.owner_of(world.runs).status == PROTECTED
    assert reg.owner_of(reg.state_root).status == PROTECTED

    seen: list[Path] = []

    def fsck(repo: Path):
        seen.append(repo)
        return True, "recorded"

    inv = world.inventory()
    report = world.check(project_roots=[world.repo, scratch_repo], inventory=inv,
                         git_fsck=fsck)
    assert report["result"] == "PASS", report["failing"]
    assert seen == [world.repo.resolve(), scratch_repo.resolve()]
    assert [c["status"] for c in report["checks"] if c["check"] == "state_root"] == ["PASS"]

    repo_b = make_repo(tmp_path / "w" / "proj-b")
    reg.register_project("b", repo_b)
    reg.open_session("b", "dev", pid=300, boot_id=BOOT)
    scratch_b = reg.scratch_dir("b", "dev", "scratch")
    git(scratch_b, "init", "-q")
    seen.clear()
    report = world.check(project_roots=[world.repo, repo_b, scratch_b], inventory=inv,
                         git_fsck=fsck)
    assert report["result"] == "FAIL"
    assert failing(report) == [("project_root", "proj-b", NOT_CHECKED),
                               ("project_root", "dev", NOT_CHECKED)]
    assert seen == [world.repo.resolve()]


def test_post_restart_not_checked_fails(tmp_path, fake):
    """Catches NOT_CHECKED counted as pass (K4 mutant): an asked-for root the
    registry does not attribute to the project -- an unregistered
    repository, a foreign runs directory, a state root that is not the
    registry's -- is NOT_CHECKED, never read, and the result fails.
    Negative control: the same call without that root passes."""
    world = World(tmp_path / "w", fake)
    inv = world.inventory()
    foreign_repo = make_repo(tmp_path / "w" / "foreign-repo")
    (tmp_path / "w" / "foreign-runs").mkdir()
    foreign_runs = add_runs(tmp_path / "w" / "foreign-runs", "x1")
    (foreign_runs / "x1" / "state.json").write_text("not json", encoding="utf-8")
    seen: list[Path] = []

    def fsck(repo: Path):
        seen.append(repo)
        return True, "recorded"

    assert world.check(inventory=inv, git_fsck=fsck)["result"] == "PASS"

    seen.clear()
    report = world.check(project_roots=[world.repo, foreign_repo], inventory=inv,
                         git_fsck=fsck)
    assert report["result"] == "FAIL"
    assert failing(report) == [("project_root", "foreign-repo", NOT_CHECKED)]
    assert foreign_repo.resolve() not in seen

    report = world.check(runs_bases=[world.runs, foreign_runs], inventory=inv)
    assert failing(report) == [("runs_base", "foreign-runs", NOT_CHECKED)]

    other_root = tmp_path / "w" / "other-state"
    other_root.mkdir()
    (other_root / "OWNER.json").write_text("{}", encoding="utf-8")
    report = post_restart_checks(world.reg, other_root, [world.repo], [world.runs],
                                 inventory=inv)
    assert failing(report) == [("state_root", "other-state", NOT_CHECKED)]


class OpenRecorder:
    """Records every open of a file while installed: `builtins.open`,
    `io.open`, `os.open`, `Path.open`, `Path.read_text`, `Path.read_bytes`.
    Only the outermost call is recorded (`Path.read_text` opening through
    `Path.open` and `io.open` is one read), as `(path, calling function,
    calling file)`; a path is normalised by resolving its parent only, so a
    symlink is recorded as itself, not as its target."""

    TARGETS = ((builtins, "open"), (io, "open"), (os, "open"),
               (Path, "open"), (Path, "read_text"), (Path, "read_bytes"))

    def __init__(self) -> None:
        self.opened: list[tuple[str, str, str]] = []
        self._depth = 0

    def install(self, mp: pytest.MonkeyPatch) -> "OpenRecorder":
        for owner, name in self.TARGETS:
            mp.setattr(owner, name, self._wrap(getattr(owner, name)))
        return self

    def _wrap(self, real):
        def call(file, *args, **kwargs):
            if self._depth == 0:
                frame = sys._getframe(1)
                where = f"fd:{file}" if isinstance(file, int) else norm(os.fspath(file))
                self.opened.append((where, frame.f_code.co_name, frame.f_code.co_filename))
            self._depth += 1
            try:
                return real(file, *args, **kwargs)
            finally:
                self._depth -= 1
        return call

    def below(self, path: Path) -> list[tuple[str, str, str]]:
        """The opens of `path` itself or of anything below it."""
        top = norm(str(path))
        return [o for o in self.opened if o[0] == top or o[0].startswith(top + os.sep)]


def norm(path: str) -> str:
    p = Path(path)
    return str(Path(os.path.realpath(p.parent)) / p.name)


def recording(call):
    """Run `call()` with every file open recorded; (result, recorder)."""
    recorder = OpenRecorder()
    with pytest.MonkeyPatch.context() as mp:
        recorder.install(mp)
        result = call()
    return result, recorder


def test_post_restart_realistic_runs_base(tmp_path, fake):
    """Catches a check that reads what a real runs base holds beside its runs
    (mutant `unknown_child_opened`) or fails on it: an owned run directory
    plus `timers.json`, `.resume.lock`, `goalbook.json`, a foreign file, a
    `tmp.*` directory whose `state.json` is a symlink to the foreign file, and
    a directory `x9` whose regular `state.json` names another run -- the
    result passes and every UNKNOWN child is listed UNKNOWN_SKIPPED by name.
    The claim is the narrowed one (Revision 9 item 2): an UNKNOWN child is
    never opened **by this module**; the registry's classification reads
    `<child>/state.json` (a regular file, `lstat`-checked) to decide
    ownership. Every open (`open`, `io.open`, `os.open`, `Path.open`,
    `read_text`, `read_bytes`) below an UNKNOWN child is therefore exactly
    that one read, by the registry's `_valid_run_dir`: once for `x9`, never
    for a file child or the symlinked `state.json`, and nothing else.
    Negative control: the owned run's `state.json` is opened by this module."""
    world = World(tmp_path / "w", fake)
    runs = world.runs
    extras = {
        "timers.json": '{"t": 1}', ".resume.lock": "", "goalbook.json": "{}",
        "foreign.txt": "someone else's",
    }
    for name, text in extras.items():
        (runs / name).write_text(text, encoding="utf-8")
    (runs / "tmp.a1b2").mkdir()
    (runs / "tmp.a1b2" / "partial.bin").write_bytes(b"half")
    (runs / "tmp.a1b2" / "state.json").symlink_to(runs / "foreign.txt")
    (runs / "x9").mkdir()
    (runs / "x9" / "state.json").write_text(run_state("other-run"), encoding="utf-8")
    (runs / "x9" / "notes.txt").write_text("not ours", encoding="utf-8")
    inv = world.inventory()

    report, rec = recording(lambda: world.check(inventory=inv))

    assert report["result"] == "PASS", report["failing"]
    unknown = ("timers.json", ".resume.lock", "goalbook.json", "foreign.txt", "tmp.a1b2", "x9")
    skipped = {s["name"]: s["status"] for s in report["unknown_skipped"]}
    assert skipped == {name: UNKNOWN for name in unknown}
    assert [c["status"] for c in report["checks"]
            if Path(c["path"]).name == "foreign.txt"] == [UNKNOWN_SKIPPED]
    for name in unknown:
        below = rec.below(runs / name)
        expected = 1 if name == "x9" else 0
        assert len(below) == expected, (name, below)
        for path, function, filename in below:
            assert path == norm(str(runs / name / "state.json")), (name, path)
            assert function == "_valid_run_dir" and Path(filename) == REGISTRY_PATH, below
    assert not [o for o in rec.opened if Path(o[2]) == MODULE_PATH
                and any(o in rec.below(runs / n) for n in unknown)]
    mine = [o for o in rec.below(runs / "r1") if Path(o[2]) == MODULE_PATH]
    assert [o[0] for o in mine] == [norm(str(runs / "r1" / "state.json"))]


def test_post_restart_truncated_state_fails_with_real_registry(tmp_path, fake):
    """Catches Revision 5 as written (K4 mutant): with the real registry, a
    run whose `state.json` was truncated after the inventory turns UNKNOWN
    (it no longer validates) -- it must be FAIL, never UNKNOWN_SKIPPED.
    Negative control: the same world without the truncation passes."""
    world = World(tmp_path / "w", fake)
    inv = world.inventory()
    assert world.check(inventory=inv)["result"] == "PASS"

    truncate(world.runs / "r1" / "state.json")
    assert world.reg.owner_of(world.runs / "r1").status == UNKNOWN
    report = world.check(inventory=inv)
    assert report["result"] == "FAIL"
    assert failing(report) == [("run", "r1", "FAIL")]
    assert "no longer OWNED" in report["failing"][0]["detail"]
    assert "r1" not in {s["name"] for s in report["unknown_skipped"]}

    # A run that disappeared after the inventory fails as well (renamed, kept).
    second = World(tmp_path / "w2", fake)
    inv2 = second.inventory()
    os.replace(second.runs / "r2", tmp_path / "w2" / "r2.parked")
    assert failing(second.check(inventory=inv2)) == [("run", "r2", "FAIL")]


def test_post_restart_without_inventory_cannot_pass(tmp_path, fake):
    """Catches runs bases passing on the registry's word alone: a healthy
    world without an inventory is FAIL with NO_INVENTORY; an inventory that
    does not cover an asked runs base, or is not an owned receipt, fails
    too. Negative controls: with the inventory it passes, and with no runs
    bases at all no inventory is needed."""
    world = World(tmp_path / "w", fake)
    report = world.check(inventory=None, project_id="a")
    assert report["result"] == "FAIL" and report["inventory"] == NO_INVENTORY
    assert failing(report) == [("inventory", "-", NO_INVENTORY)]
    # Without project_id or inventory the calling project is unknown: fail closed.
    anonymous = world.check(inventory=None)
    assert ("project_root", "proj-a", NOT_CHECKED) in failing(anonymous)
    assert ("runs_base", "runs", NOT_CHECKED) in failing(anonymous)

    inv = world.inventory()
    assert world.check(inventory=inv)["result"] == "PASS"
    assert world.check(runs_bases=[], inventory=None, project_id="a")["result"] == "PASS"

    empty = world.inventory(runs_bases=[])
    assert failing(world.check(inventory=empty)) == [("runs_base", "runs", "FAIL")]

    forged = tmp_path / "w" / "forged-inventory.json"
    forged.write_text(inv.read_text(encoding="utf-8"), encoding="utf-8")
    report = world.check(inventory=forged)
    assert report["result"] == "FAIL"
    assert ("inventory", "forged-inventory.json", "FAIL") in failing(report)


# --------------------------------------------------------------------------- #
# K9 -- Revision 9 items 2-4 and 9b item 3
# --------------------------------------------------------------------------- #


def inventory_check(report: dict) -> dict:
    [check] = [c for c in report["checks"] if c["check"] == "inventory"]
    return check


def test_stale_inventory_refused(tmp_path, fake):
    """Catches an inventory accepted although it cannot vouch for this
    restart (Revision 9 item 3, shost-a M1): one taken in the current boot is
    refused; an older one is refused once a newer inventory of the project
    exists in its receipts (another session); one dated after the current
    wall clock is refused (the clock stepped back); an unreadable boot is a
    refusal, not a pass. The age is reported. Negative controls changing only
    that condition: the same inventory checked in the next boot passes, and
    the newest inventory from the previous boot passes."""
    world = World(tmp_path / "w", fake)
    t0 = time.time_ns()
    first = world.inventory(clock=lambda: t0)

    same_boot = world.check(inventory=first, boot=HOST_BOOT_BEFORE,
                            clock=lambda: t0 + 90 * 10**9)
    assert same_boot["result"] == "FAIL"
    assert failing(same_boot) == [("inventory", first.name, "FAIL")]
    assert "current boot" in inventory_check(same_boot)["detail"]

    ok = world.check(inventory=first, clock=lambda: t0 + 90 * 10**9)
    assert ok["result"] == "PASS", ok["failing"]
    assert ok["inventory_age_seconds"] == 90.0
    assert ok["inventory_boot_id"] == HOST_BOOT_BEFORE
    assert ok["current_boot_id"] == HOST_BOOT_AFTER

    second = world.inventory(clock=lambda: t0 + 10**9)
    assert second.parent != first.parent                 # another session's receipts
    stale = world.check(inventory=first, clock=lambda: t0 + 90 * 10**9)
    assert failing(stale) == [("inventory", first.name, "FAIL")]
    detail = inventory_check(stale)["detail"]
    assert "newer inventory" in detail and second.name in detail
    assert stale["inventory_age_seconds"] == 90.0

    newest = world.check(inventory=second, clock=lambda: t0 + 90 * 10**9)
    assert newest["result"] == "PASS", newest["failing"]
    assert newest["inventory_age_seconds"] == 89.0

    stepped_back = world.check(inventory=second, clock=lambda: t0)
    assert failing(stepped_back) == [("inventory", second.name, "FAIL")]
    assert "stepped back" in inventory_check(stepped_back)["detail"]

    def no_boot() -> str:
        raise OSError(2, "No such file or directory", "/proc/sys/kernel/random/boot_id")

    blind = post_restart_checks(world.reg, world.reg.state_root, [world.repo], [world.runs],
                                inventory=second, boot_id=no_boot,
                                clock=lambda: t0 + 90 * 10**9)
    assert failing(blind) == [("inventory", second.name, "FAIL")]
    assert "boot id cannot be read" in inventory_check(blind)["detail"]


def test_child_newer_than_inventory_fails(tmp_path, fake):
    """Catches a stale inventory vouching for children it never saw
    (Revision 9b item 3, 9c): after the inventory, a new run directory `r9`,
    an UNKNOWN file `late.txt` and a rewritten `timers.json` appear in the
    runs base -- each is FAIL, judged on `lstat` metadata alone, and nothing
    below any of them is opened (not even by the registry's
    classification); the registry's owned `_arenas/`, also new, does not
    fail. Negative control changing only the time: the same children, with
    the inventory dated after them, pass (`r9` is an owned run that parses)
    or are UNKNOWN_SKIPPED."""
    def world_with_newcomers(name: str, offset_s: int):
        world = World(tmp_path / name, fake)
        inv = world.inventory(clock=lambda: time.time_ns() + offset_s * 10**9)
        add_runs(world.runs, "r9")
        (world.runs / "late.txt").write_text("late", encoding="utf-8")
        (world.runs / "timers.json").write_text("{}", encoding="utf-8")
        (world.runs / "_arenas").mkdir()
        (world.runs / "_arenas" / "a1").mkdir()
        assert world.reg.owner_of(world.runs / "_arenas").status == OWNED
        assert world.reg.owner_of(world.runs / "r9").status == OWNED
        return world, inv

    newer, inv = world_with_newcomers("newer", -5)      # inventory 5 s before them
    report, rec = recording(lambda: newer.check(inventory=inv))
    assert report["result"] == "FAIL"
    assert failing(report) == [("run", "late.txt", "FAIL"), ("run", "r9", "FAIL"),
                               ("run", "timers.json", "FAIL")]
    assert all("not in the inventory" in c["detail"] for c in report["failing"])
    for name in ("r9", "late.txt", "timers.json"):
        assert rec.below(newer.runs / name) == [], name
    arenas = [c for c in report["checks"] if Path(c["path"]).name == "_arenas"]
    assert [c["status"] for c in arenas] == ["PASS"]

    older, inv = world_with_newcomers("older", +5)      # inventory dated after them
    report = older.check(inventory=inv, clock=lambda: time.time_ns() + 60 * 10**9)
    assert report["result"] == "PASS", report["failing"]
    assert {s["name"] for s in report["unknown_skipped"]} == {"late.txt", "timers.json"}
    r9 = [c for c in report["checks"] if Path(c["path"]).name == "r9"]
    assert [(c["status"], c["detail"]) for c in r9] == [("PASS", "state.json parses")]


def test_mutant_inventory_project_mismatch_ok(tmp_path, fake):
    """Kills mutant `inventory_project_mismatch_ok` (shost-a M2): an inventory
    of project `b` handed to a project-`a` caller is refused, the calling
    project stays `a`, and none of `b`'s roots is read -- `b`'s repository
    never reaches `git fsck`, nothing below `b`'s runs base is opened, both
    are NOT_CHECKED. Negative control changing only the caller: the same
    inventory with `project_id="b"` is accepted and `b`'s roots are checked."""
    world = World(tmp_path / "w", fake)
    reg = world.reg
    repo_b = make_repo(tmp_path / "w" / "proj-b")
    reg.register_project("b", repo_b)
    runs_b = tmp_path / "w" / "ext" / "runs-b"
    reg.create_external_root("b", runs_b, "runs_base")
    add_runs(runs_b, "b1")
    inv_b = world.inventory([runs_b], project="b")
    seen: list[Path] = []

    def fsck(repo: Path):
        seen.append(repo)
        return True, "recorded"

    report, rec = recording(lambda: world.check(
        project_roots=[world.repo, repo_b], runs_bases=[world.runs, runs_b],
        inventory=inv_b, project_id="a", git_fsck=fsck))
    assert report["result"] == "FAIL" and report["project_id"] == "a"
    check = inventory_check(report)
    assert check["status"] == "FAIL" and "belongs to project 'b'" in check["detail"]
    assert ("project_root", "proj-b", NOT_CHECKED) in failing(report)
    assert ("runs_base", "runs-b", NOT_CHECKED) in failing(report)
    assert seen == [world.repo.resolve()]
    assert rec.below(runs_b) == [] and rec.below(repo_b) == []

    seen.clear()
    control = world.check(project_roots=[repo_b], runs_bases=[runs_b], inventory=inv_b,
                          project_id="b", git_fsck=fsck)
    assert control["result"] == "PASS", control["failing"]
    assert inventory_check(control)["status"] == "PASS"
    assert seen == [repo_b.resolve()]


def test_mutant_state_root_any(tmp_path, fake):
    """Kills mutant `state_root_any` (shost-a M2): a path that is not the
    registry's state root is not accepted as the state root, and its
    `OWNER.json` is never opened -- a foreign directory, the project's
    PROTECTED runs base and the project's registered repository each carry
    an `OWNER.json` and each is NOT_CHECKED. Negative control changing only
    the path: the registry's own state root passes and its `OWNER.json` is
    read."""
    world = World(tmp_path / "w", fake)
    foreign = tmp_path / "w" / "other-state"
    foreign.mkdir()
    impostors = (foreign, world.runs, world.repo)
    for root in impostors:
        (root / "OWNER.json").write_text('{"schema": "planted"}', encoding="utf-8")
    inv = world.inventory()

    for root in impostors:
        report, rec = recording(lambda: post_restart_checks(
            world.reg, root, [world.repo], [world.runs], inventory=inv,
            boot_id=lambda: HOST_BOOT_AFTER))
        assert failing(report) == [("state_root", root.name, NOT_CHECKED)], root
        assert rec.below(root / "OWNER.json") == [], root

    report, rec = recording(lambda: world.check(inventory=inv))
    assert report["result"] == "PASS", report["failing"]
    assert [c["status"] for c in report["checks"] if c["check"] == "state_root"] == ["PASS"]
    assert rec.below(world.reg.state_root / "OWNER.json")


def test_mutant_git_env_inherited(tmp_path, fake, monkeypatch):
    """Kills mutant `git_env_inherited` (shost-a M2): with `GIT_DIR`,
    `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY` and
    `GIT_COMMON_DIR` in the environment pointing at a second, healthy
    repository, the project root with a corrupt object still fails the
    default `git fsck` -- it runs on the asked root with a scrubbed
    environment. Negative control changing only the environment handling:
    the same `git fsck` with the environment inherited passes on the corrupt
    repository, so the redirect would hide the corruption."""
    world = World(tmp_path / "w", fake)
    inv = world.inventory()
    healthy = make_repo(tmp_path / "w" / "healthy")
    corrupt_tree_object(world.repo)
    redirect = {
        "GIT_DIR": str(healthy / ".git"), "GIT_WORK_TREE": str(healthy),
        "GIT_INDEX_FILE": str(healthy / ".git" / "index"),
        "GIT_OBJECT_DIRECTORY": str(healthy / ".git" / "objects"),
        "GIT_COMMON_DIR": str(healthy / ".git"),
    }
    inherited = subprocess.run(
        ["git", "-C", str(world.repo), "fsck", "--connectivity-only", "--no-dangling"],
        capture_output=True, text=True, env={**os.environ, **redirect}, check=False)
    assert inherited.returncode == 0, inherited.stderr

    for name, value in redirect.items():
        monkeypatch.setenv(name, value)
    report = world.check(inventory=inv)
    assert report["result"] == "FAIL"
    assert failing(report) == [("git_fsck", "proj-a", "FAIL")]
