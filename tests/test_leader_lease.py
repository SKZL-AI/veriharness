"""P2-09 leader lease: a renewable lease, measured through its own symbol.

The register's probe for P2-09 used to name `LockBusy`, a per-project flock,
and an equivalent saying so ("not a renewable lease"). The renewable lease is
`hoh.succession.MasterAuthority` (acquire, renew, expiry, takeover, refusal of a
second live holder, handoff) and its conditional acquire, which takes one
handover and nothing else. Everything environmental is injected (clock, boot,
pid liveness), so nothing here sleeps, starts a process or touches the machine.
"""

from __future__ import annotations

import contextlib

import pytest

import hoh.succession as S
from hoh.epoch import EpochStore, Reason

START = "2026-10-08T10:00:00Z"


class World:
    """One machine: a clock, a boot id, the set of live pids."""

    def __init__(self, directory):
        self.dir = directory
        self.now = START
        self.live: set[int] = set()

    def advance(self, seconds: int) -> None:
        self.now = S._plus(self.now, seconds)

    def master(self, session, pid, **kw):
        self.live.add(pid)
        return S.MasterAuthority(self.dir, session, clock=lambda: self.now,
                                 boot=lambda: "boot-1", host="h", pid=pid,
                                 alive=lambda p: p in self.live, **kw)

    def snapshot(self):
        """Every byte the authority protocol writes."""
        return {name: (self.dir / name).read_bytes()
                for name in (S.FILENAME, S.LOG, "epochs.json")
                if (self.dir / name).exists()} | {
            "chain": [(e.epoch, e.master_id, e.reason.value)
                      for e in EpochStore(self.dir).chain()]}


def forge(world, **update):
    """Rewrite the lease file with some fields changed (a damaged or forged file: the
    protocol never writes these combinations, and the conditional acquire must not trust them)."""
    probe = world.master("forger", 1)
    probe.lease_path.write_text(
        probe.read_lease().model_copy(update=update).model_dump_json(indent=2) + "\n")


@pytest.fixture
def world(tmp_path):
    return World(tmp_path / "program")


def handed_over(world, *, expires_in=120):
    """sA holds epoch 1 and hands over to sB. Returns (sA, the expectation)."""
    a = world.master("sA", 100, ttl_seconds=600)
    a.acquire()
    digest = a.read_lease().token_sha256
    a.handoff("sB", reason="rotation", expires_in=expires_in)
    return a, S.ExpectedHandover(master_epoch=1, token_sha256=digest)


# --------------------------------------------------------------------------- #
# the lease, end to end
# --------------------------------------------------------------------------- #


def test_p209_the_leader_lease_lifecycle(world):
    # acquire: epoch 1, a lease that names the holder and runs for the ttl
    a = world.master("sA", 100, ttl_seconds=60)
    first = a.acquire()
    assert (first.epoch, first.reason) == (1, Reason.INITIAL) and a.problems() == []
    lease = a.read_lease()
    assert (lease.leader_session_id, lease.master_epoch, lease.holder_pid) == ("sA", 1, 100)
    assert lease.expires_at == S._plus(START, 60) and lease.token_sha256

    # renew: later expiry, the same epoch and token, and the holder stays live
    world.advance(40)
    renewed = a.renew()
    assert renewed.expires_at == S._plus(world.now, 60) and renewed.expires_at > lease.expires_at
    assert (renewed.master_epoch, renewed.token_sha256) == (1, lease.token_sha256)

    # a second live holder is refused, and nothing is written for it
    before = world.snapshot()
    with pytest.raises(S.LeaseHeld):
        world.master("sB", 200).acquire()
    assert world.snapshot() == before

    # expiry: past expires_at the holder may no longer write, although its pid lives
    world.advance(61)
    assert a.problems() and "expired" in a.problems()[0]
    with pytest.raises(S.StaleMaster):
        a.check()

    # takeover after expiry: a new epoch (RECOVERY), the old holder fenced for good
    b = world.master("sB", 200, ttl_seconds=60)
    taken = b.acquire()
    assert (taken.epoch, taken.reason) == (2, Reason.RECOVERY) and b.problems() == []
    with pytest.raises(S.StaleMaster):
        a.renew()

    # ... and sB, live now, is in turn never displaced
    with pytest.raises(S.LeaseHeld):
        world.master("sC", 300).acquire()

    # a conditional handover: sB hands to sC; only sC, only that handover
    digest = b.read_lease().token_sha256
    b.handoff("sC", reason="rotation", expires_in=120)
    c = world.master("sC", 300)
    done = c.acquire(only_handover=S.ExpectedHandover(2, digest))
    assert (done.epoch, done.reason) == (3, Reason.SUCCESSION) and c.problems() == []
    assert b.problems()
    chain = [(e.epoch, e.master_id) for e in EpochStore(world.dir).chain()]
    assert chain == [(1, "sA"), (2, "sB"), (3, "sC")]


# --------------------------------------------------------------------------- #
# the conditional acquire
# --------------------------------------------------------------------------- #


def test_p209_a_conditional_acquire_takes_exactly_the_handover_it_was_given(world):
    _, want = handed_over(world)
    b = world.master("sB", 200)
    epoch = b.acquire(only_handover=want)
    assert (epoch.epoch, epoch.reason) == (2, Reason.SUCCESSION)
    assert b.problems() == []
    lease = b.read_lease()
    assert (lease.leader_session_id, lease.master_epoch, lease.released_at) == ("sB", 2, None)
    assert lease.token_sha256 == S.token_digest(b.token) != want.token_sha256


def test_p209_a_conditional_acquire_refuses_every_other_lease_and_writes_nothing(world):
    _, want = handed_over(world)
    wrong = {
        "another token": S.ExpectedHandover(1, "0" * 64),
        "another epoch": S.ExpectedHandover(2, want.token_sha256),
    }
    for name, expectation in wrong.items():
        b = world.master("sB", 200)
        before = world.snapshot()
        with pytest.raises(S.HandoverGone):
            b.acquire(only_handover=expectation)
        assert world.snapshot() == before, name
        assert b.epoch is None and b.problems(), name

    # another session than the one the handover names
    c = world.master("sC", 300)
    before = world.snapshot()
    with pytest.raises(S.HandoverGone):
        c.acquire(only_handover=want)
    assert world.snapshot() == before and c.epoch is None

    # a handover that has lapsed (the named successor, the right token, too late)
    world.advance(121)
    b = world.master("sB", 200)
    before = world.snapshot()
    with pytest.raises(S.HandoverGone, match="lapsed"):
        b.acquire(only_handover=want)
    assert world.snapshot() == before and b.epoch is None
    # ... although an unconditional acquire by the same session still goes through
    assert world.master("sB", 200).acquire().epoch == 2


def test_p209_a_conditional_acquire_refuses_a_lease_that_is_not_a_handover(world):
    a = world.master("sA", 100)
    a.acquire()
    digest = a.read_lease().token_sha256
    want = S.ExpectedHandover(1, digest)
    # a live lease, even the acquiring session's own, is not a handover
    for who in (world.master("sB", 200), world.master("sA", 100)):
        before = world.snapshot()
        with pytest.raises(S.HandoverGone):
            who.acquire(only_handover=want)
        assert world.snapshot() == before
    # a released lease with no successor is not a handover either
    a.release(reason="done")
    before = world.snapshot()
    with pytest.raises(S.HandoverGone):
        world.master("sB", 200).acquire(only_handover=want)
    assert world.snapshot() == before
    # and no lease at all is nothing to take
    empty = World(world.dir.parent / "empty")
    with pytest.raises(S.HandoverGone):
        empty.master("sB", 200).acquire(only_handover=want)
    # no lease, log or epoch write (the lock file and the directory may exist)
    assert not [n for n in (S.FILENAME, S.LOG, "epochs.json") if (empty.dir / n).exists()]


def test_p209_a_conditional_acquire_judges_the_lease_field_by_field(world):
    """Each field alone, on a lease that is otherwise exactly the expected handover."""
    _, want = handed_over(world)
    for update, why in (({"master_epoch": 7}, "epoch 7"),
                        ({"released_at": None, "released_reason": ""}, "not handed over"),
                        ({"successor": "sZ"}, "'sZ'"),
                        ({"token_sha256": "f" * 64}, "another token")):
        forge(world, **update)
        b = world.master("sB", 200)
        before = world.snapshot()
        with pytest.raises(S.HandoverGone, match=why):
            b.acquire(only_handover=want)
        assert world.snapshot() == before and b.epoch is None
        forge(world, master_epoch=1, released_at="2026-10-08T10:00:00Z", successor="sB",
              token_sha256=want.token_sha256, released_reason="handoff: rotation")
    assert world.master("sB", 200).acquire(only_handover=want).epoch == 2   # the repaired file is taken


def test_p209_a_conditional_acquire_refuses_when_the_chain_moved_past_the_handover(world):
    _, want = handed_over(world)
    EpochStore(world.dir).begin("intruder", reason=Reason.RECOVERY, now=world.now, note="x")
    b = world.master("sB", 200)
    before = world.snapshot()
    with pytest.raises(S.HandoverGone, match="epoch chain"):
        b.acquire(only_handover=want)
    assert world.snapshot() == before and b.epoch is None


def test_p209_the_expectation_is_judged_inside_the_acquires_own_lock(world):
    """The predecessor reclaims after the successor's last look and before its acquire
    takes the lock: the hook runs exactly there (`serialize` is entered first)."""
    _, want = handed_over(world)
    reclaimed = {}

    class ReclaimBeforeTheLock:
        def __enter__(self):
            if not reclaimed:
                world.advance(200)                    # the window passes ...
                reclaimed["epoch"] = world.master("sA", 999).acquire(
                    reason=Reason.RECOVERY, note="lapsed").epoch

        def __exit__(self, *exc):
            return False

    b = world.master("sB", 200, serialize=ReclaimBeforeTheLock)
    with pytest.raises(S.HandoverGone):
        b.acquire(only_handover=want)
    assert reclaimed == {"epoch": 2}
    assert [(e.epoch, e.master_id) for e in EpochStore(world.dir).chain()] == [(1, "sA"), (2, "sA")]
    assert b.epoch is None
    assert world.master("probe", 1).read_lease().leader_session_id == "sA"


def test_p209_the_expectation_is_judged_after_the_flock_is_held(tmp_path):
    """The same race with the hook on the real flock: the reclaim lands after `serialize()` was
    entered and before `_locked()` takes the flock. Values read before the flock, even after
    `serialize()`, are stale here; only a judgement made once the flock is held refuses."""
    world = World(tmp_path / "program")
    _, want = handed_over(world)
    b = world.master("sB", 200)
    real, done = b._locked, {}

    @contextlib.contextmanager
    def reclaim_then_lock():
        if not done:
            world.advance(200)
            done["epoch"] = world.master("sA", 999).acquire(
                reason=Reason.RECOVERY, note="lapsed").epoch
            world.live.discard(999)               # the predecessor's command exits: lease reads LOST
        with real():
            yield

    b._locked = reclaim_then_lock
    with pytest.raises(S.HandoverGone):
        b.acquire(only_handover=want)
    assert done == {"epoch": 2}
    assert [(e.epoch, e.master_id) for e in EpochStore(world.dir).chain()] == [(1, "sA"), (2, "sA")]
    assert b.epoch is None


def test_p209_a_conditional_acquire_is_refused_before_any_io_when_it_is_malformed(world):
    b = world.master("sB", 200)
    _, want = handed_over(world)
    forge(world, token_sha256="")                 # a lease with no token digest ...
    for bad in (S.ExpectedHandover(1, ""),        # ... is not matched by an empty expectation
                S.ExpectedHandover(True, "d"), S.ExpectedHandover("1", "d")):
        with pytest.raises(S.LeaseError) as caught:
            b.acquire(only_handover=bad)
        assert not isinstance(caught.value, S.HandoverGone)       # refused before the lock
    forge(world, token_sha256=want.token_sha256)
    for not_a_handover in (("1", "d"), {"master_epoch": 1, "token_sha256": want.token_sha256},
                           want.token_sha256, 1):
        with pytest.raises(S.LeaseError) as caught:   # a LeaseError, not an AttributeError or a TypeError
            b.acquire(only_handover=not_a_handover)
        assert not isinstance(caught.value, S.HandoverGone)
    with pytest.raises(S.LeaseError, match="SUCCESSION"):
        b.acquire(only_handover=want, reason=Reason.RECOVERY)
    assert b.epoch is None and world.snapshot()["chain"] == [(1, "sA", "INITIAL")]
