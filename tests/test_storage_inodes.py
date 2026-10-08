"""The inode axis of storage pressure (P2-42, lesson of 2026-10-05).

On 2026-10-05 pytest trees of 15.5 M entries used up the 67 M inodes of the root
filesystem while 100 GB were free, and the one inode rule -- an absolute floor of
100 000 free inodes -- said NORMAL until the very end. These tests pin the second
axis: when the filesystem reports its inode total, the share of free inodes is
classified like free bytes, the state is the worse of the two axes, a filesystem
without a fixed inode table is not an emergency -- and says so (NOT_MEASURED), so
an observer that reads 0 by mistake is visible, not quiet.

Free space is never this machine's: every observer is a fake ``statvfs``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import hoh.storage_pressure as sp
from hoh.storage_pressure import (
    Observation,
    PressureState,
    StoragePolicy,
    StateTracker,
)
from test_storage_pressure import make
from test_storage_tiers import GiB, isolated_env  # noqa: F401  (isolated_env: autouse)

TOTAL = 67_108_864              # the inodes of the root filesystem on 2026-10-05
N, O, P, C = (PressureState.NORMAL, PressureState.OPTIMIZE, PressureState.PRESSURE,
              PressureState.CRITICAL)


class Statvfs:
    """``os.statvfs`` stand-in with the inode total (``f_files``)."""

    def __init__(self, free_bytes: int, free_inodes: int, total_inodes: int | None) -> None:
        self.free_bytes, self.free_inodes, self.total = free_bytes, free_inodes, total_inodes

    def __call__(self, path):
        fields = dict(f_bavail=self.free_bytes, f_frsize=1, f_favail=self.free_inodes,
                      f_fsid=0x6E57)
        if self.total is not None:
            fields["f_files"] = self.total
        return SimpleNamespace(**fields)


@pytest.mark.parametrize("free_gib,free_inodes,total,expected", [
    (500, 15_000_000, TOTAL, N),         # 22 % free
    (500, 10_000_000, TOTAL, O),         # 14.9 %
    (500, 5_000_000, TOTAL, P),          # 7.4 %
    (500, 1_500_000, TOTAL, C),          # 2.2 % -- the root filesystem on 2026-10-05
    (500, 99_999, TOTAL, C),             # the absolute floor still holds
    (500, 100_000, TOTAL, C),            # 0.15 % is below the share, not the floor
    (500, 5_000_000, None, N),           # no total reported: the floor alone, as before
    (500, 99_999, None, C),
    (500, 0, 0, N),                      # no fixed inode table: the axis is off
    (10, 0, 0, C),                       # ... and the byte axis still decides
    (40, 15_000_000, TOTAL, P),          # bytes worse than inodes: bytes decide
    (500, 5_000_000, TOTAL, P),
    (20, 1_500_000, TOTAL, C),           # both: the worse
    (500, 10_000_000, 100_000_000, O),   # exactly 10 %: not below the pressure share
    (500, 9_999_999, 100_000_000, P),
    (500, 20_000_000, 100_000_000, N),   # exactly 20 %
    (500, 19_999_999, 100_000_000, O),
    (500, 3_000_000, 100_000_000, P),    # exactly 3 %
    (500, 2_999_999, 100_000_000, C),
    (500, 99_999, 1_000_000, C),         # 10 % free, but below the absolute floor
    (500, 100_000, 1_000_000, O),        # on the floor: not below it
])
def test_inode_axis_states_by_share_of_free_inodes(free_gib, free_inodes, total, expected):
    """P1. Catches an inode rule that waits for the last 100 000: the share of
    free inodes is classified (>= 20 % NORMAL, >= 10 % OPTIMIZE, >= 3 %
    PRESSURE, below CRITICAL), the absolute floor still makes CRITICAL, an
    observer that reports no total gets the floor alone, a total of 0 (no fixed
    table) switches the axis off, and the state is the worse of bytes and
    inodes -- through `classify` and through `update` of an `Observation`.
    Negative control: the same inodes with the total unreported are NORMAL."""
    tracker = StateTracker(StoragePolicy(), None)
    assert tracker.classify(int(free_gib * GiB), free_inodes, None, total) is expected
    fresh = StateTracker(StoragePolicy(), None)
    assert fresh.update(Observation(guest_free_bytes=int(free_gib * GiB),
                                    guest_free_inodes=free_inodes,
                                    guest_total_inodes=total)) is expected


def test_inode_axis_hysteresis_by_share():
    """P2. Catches an inode state that oscillates: out of CRITICAL (below 3 %)
    only once the share is 2 points above the boundary (5 %), out of PRESSURE
    only once it is 2 points above 10 % (12 %), out of OPTIMIZE only at 22 %; a
    worse state applies at once. Negative control: with ``inode_ratio_hysteresis=0`` every
    boundary releases at once."""
    total = 100_000_000

    def run(policy, shares):
        tracker = StateTracker(policy, None)
        return [tracker.update(Observation(guest_free_bytes=500 * GiB,
                                           guest_free_inodes=int(total * s),
                                           guest_total_inodes=total)) for s in shares]

    shares = [0.02, 0.04, 0.05, 0.09, 0.11, 0.12, 0.19, 0.21, 0.22, 0.04]
    assert run(StoragePolicy(), shares) == [C, C, P, P, P, O, O, O, N, P]
    assert StoragePolicy().inode_ratio_hysteresis == 0.02
    assert run(StoragePolicy(inode_ratio_hysteresis=0), [0.02, 0.03, 0.10, 0.20]) == [C, P, O, N]


def test_a_filesystem_without_an_inode_table_is_not_an_inode_emergency(tmp_path):
    """P3. Catches a zero ``f_favail`` read as exhaustion on a filesystem that
    has no fixed inode table (``f_files == 0``, as on btrfs): with 500 GiB
    free the state is NORMAL, ``status()`` reports the axis NOT_MEASURED with no
    ratio, the persisted state records the total (0), a reservation is
    admitted, and 10 GiB free is still CRITICAL by bytes; a total that is
    **not reported** (None) is not "no table": the axis judges the floor alone
    and reports it, not NOT_MEASURED. Negative control: the same zero free
    inodes with a real total of 67 M is CRITICAL and refused."""
    adm, _, _ = make(tmp_path / "dynamic", statvfs=Statvfs(500 * GiB, 0, 0))
    status = adm.status()
    assert status["state"] == "NORMAL" and status["guest_total_inodes"] == 0
    assert status["inode_free_ratio"] is None and status["inode_state"] == "NOT_MEASURED"
    assert adm.reserve("p1", "role_run", estimate_bytes=1).admitted
    assert json.loads((adm.ledger / "state.json").read_text())["total_inodes"] == 0
    low, _, _ = make(tmp_path / "dynamic-low", statvfs=Statvfs(10 * GiB, 0, 0))
    assert low.status()["state"] == "CRITICAL"             # the byte axis is untouched
    unreported, _, _ = make(tmp_path / "unreported", statvfs=Statvfs(500 * GiB, 5_000_000, None))
    status = unreported.status()                       # an observer that cannot say is not "table-less"
    assert status["guest_total_inodes"] is None and status["inode_free_ratio"] is None
    assert status["inode_state"] == "NORMAL" and status["state"] == "NORMAL"
    exhausted, _, _ = make(tmp_path / "fixed", statvfs=Statvfs(500 * GiB, 0, TOTAL))
    assert exhausted.status()["state"] == "CRITICAL"
    assert not exhausted.reserve("p1", "role_run", estimate_bytes=1).admitted


def test_the_lesson_of_2026_10_05_a_lot_of_bytes_and_few_inodes(tmp_path):
    """P4. Catches the 2026-10-05 failure: 500 GiB free and 67 108 864 inodes of
    which 2.2 % are free is CRITICAL, ``status()`` reports the inode figures
    and the inode axis, the persisted tracker state carries the total, and
    every reservation is refused; at 7.4 % the state is PRESSURE and a
    ``tmp_workspace`` reservation is admitted (a job that fits is admitted in
    PRESSURE, as for bytes); at 14.9 % OPTIMIZE; at 22 % NORMAL. Negative
    control: the bytes are fine in every row."""
    rows = [(1_500_000, "CRITICAL"), (5_000_000, "PRESSURE"), (10_000_000, "OPTIMIZE"),
            (15_000_000, "NORMAL")]
    for free_inodes, state in rows:
        adm, _, _ = make(tmp_path / state, statvfs=Statvfs(500 * GiB, free_inodes, TOTAL))
        status = adm.status()
        assert status["state"] == state and status["inode_state"] == state
        assert status["guest_free_inodes"] == free_inodes
        assert status["guest_total_inodes"] == TOTAL
        assert status["inode_free_ratio"] == pytest.approx(free_inodes / TOTAL)
        assert status["effective_free_bytes"] == 500 * GiB
        persisted = json.loads((adm.ledger / "state.json").read_text())
        assert persisted["state"] == state and persisted["total_inodes"] == TOTAL
        answer = adm.reserve("p1", "tmp_workspace")
        assert answer.admitted is (state != "CRITICAL"), (state, answer)
        if state == "CRITICAL":
            assert "CRITICAL" in answer.reason


def test_policy_inode_ratios_have_defaults_and_an_order():
    """P5. Catches thresholds that can be set against each other: the defaults
    are 0.20 / 0.10 / 0.03 with a hysteresis of 0.02, and a policy whose
    critical share is not below its pressure share, which is not below its
    normal share, is refused with the inode ordering named. Negative control:
    a consistent override is taken."""
    policy = StoragePolicy()
    assert (policy.inode_normal_ratio, policy.inode_pressure_ratio,
            policy.inode_critical_ratio, policy.inode_ratio_hysteresis) == (0.20, 0.10, 0.03,
                                                                            0.02)
    for bad in ({"inode_critical_ratio": 0.1}, {"inode_pressure_ratio": 0.2},
                {"inode_normal_ratio": 0.05}):
        with pytest.raises(ValueError, match="inode ordering"):
            StoragePolicy(**bad)
    for bad in ({"inode_normal_ratio": 1.5}, {"inode_pressure_ratio": 1.2, "inode_normal_ratio": 1.5},
                {"inode_critical_ratio": -0.1}, {"inode_ratio_hysteresis": 1.5},
                {"inode_normal_ratio": 0}):
        with pytest.raises(ValueError):
            StoragePolicy(**bad)                        # a share is between 0 and 1
    assert StoragePolicy(inode_normal_ratio=1.0, inode_pressure_ratio=0.5,
                         inode_critical_ratio=0.0).inode_normal_ratio == 1.0
    assert StoragePolicy(inode_normal_ratio=0.5, inode_pressure_ratio=0.4,
                         inode_critical_ratio=0.1).inode_normal_ratio == 0.5


def test_the_observer_reads_the_inode_total_only_when_it_is_a_count(tmp_path):
    """P7. Catches an inode total taken from anything: ``f_files`` that is a
    count (including 0) is the total; a bool (``True`` is an ``int`` in Python),
    a negative number, a string or a missing field is **not reported** (None) --
    the axis then judges the floor alone. Negative control: the real count."""
    from hoh.storage_pressure import GuestObserver, NoHost

    missing = object()

    def total(value):
        def statvfs(path):
            fields = dict(f_bavail=500 * GiB, f_frsize=1, f_favail=5_000_000, f_fsid=1)
            if value is not missing:
                fields["f_files"] = value
            return SimpleNamespace(**fields)
        return GuestObserver("/", NoHost(), statvfs=statvfs).observe().guest_total_inodes

    assert total(TOTAL) == TOTAL and total(0) == 0
    assert total(True) is None and total(-5) is None and total("67108864") is None
    assert total(missing) is None


def test_tmp_workspace_is_an_admitted_operation_kind(tmp_path):
    """P6. Catches temporary workspaces that cannot be admitted: ``tmp_workspace``
    is an operation kind with a 1 GiB default estimate that a policy may
    override, `estimate` answers it, and a kind that is not listed is still
    refused. Negative control: the other kinds keep their defaults."""
    assert "tmp_workspace" in sp.OP_KINDS and sp.DEFAULT_ESTIMATES["tmp_workspace"] == GiB
    assert StoragePolicy(defaults={"tmp_workspace": 5 * GiB}).defaults["tmp_workspace"] == 5 * GiB
    adm, _, _ = make(tmp_path / "k", statvfs=Statvfs(500 * GiB, 20_000_000, TOTAL))
    assert adm.estimate("tmp_workspace") == GiB and adm.estimate("clone") == 2 * GiB
    with pytest.raises(ValueError):
        adm.estimate("tmp_scratch")
