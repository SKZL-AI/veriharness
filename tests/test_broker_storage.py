"""P3-39 slice 1: storage as one more capacity of the broker (bytes and inodes of the admission ledger).

Every test names what it catches. The broker stays pure: the storage snapshot is a dict handed in (what
`StorageCoordinator.observe()` returns), and nothing here reads the host's free space -- the admission under test
runs on a fake `statvfs`, in `tmp_path`, with `HOME` confined there. The limits are asserted with literal numbers
(10**15, 256 entries), not with the module's constants. Nothing starts Herdr or an agent, or touches the network.
"""
from __future__ import annotations

import contextlib
import copy
import dataclasses
import importlib
import inspect
import math
import signal
from datetime import datetime, timedelta
from types import MappingProxyType

import pytest

from test_storage_coordinator import FakeOS, World, isolated_env  # noqa: F401  (isolated_env: autouse)
from test_storage_pressure import FakeHost, FakeStatvfs, make
from hoh.storage_pressure import GiB, PressureState, StateTracker, StoragePolicy


def _broker():
    """`hoh.broker`, imported inside each test: a tree without the slice fails each test with its own error."""
    return importlib.import_module("hoh.broker")


def _capacity():
    return importlib.import_module("hoh.capacity")


NOW = "2026-10-12T09:00:00Z"
_FMT = "%Y-%m-%dT%H:%M:%SZ"
BYTES, INODES = "storage.bytes", "storage.inodes"


def ago(seconds: int) -> str:
    return (datetime.strptime(NOW, _FMT) - timedelta(seconds=seconds)).strftime(_FMT)


def snap(**over) -> dict:
    """A snapshot in the shape of `StorageAdmission.status()`: 700 free bytes against a reserve of 300 with 100
    reserved (limit 400, headroom 300 after the hold); 5000 free inodes against a floor of 1000 (limit 4000)."""
    base = {"state": "NORMAL", "observed_utc": "2026-10-12T08:59:59.250000Z", "effective_free_bytes": 700,
            "reserve_bytes": 300, "reserved_bytes": 100, "malformed_reservations": [],
            "guest_free_inodes": 5000, "reserve_inodes": 1000, "inode_state": "NORMAL",
            "guest_total_inodes": 100000, "host_unknown": False, "host_reading_utc": None}
    base.update(over)
    return base


def amount(name=BYTES, n=1, unit="bytes", kind="machine"):
    return _broker().Amount(kind=kind, name=name, amount=n, unit=unit)


def entry(kind="machine", name="host.sessions", limit=8, unit="sessions", source="measured", as_of=NOW):
    return _capacity().Entry(kind=kind, name=name, limit=limit, unit=unit, source=source, as_of=as_of)


def registry(*entries):
    cap = _capacity()
    return cap.parse(cap.render(entries))


def policy(default=3600, **ages):
    table = {(k, s): default for k in ("dependency", "machine", "provider") for s in ("declared", "measured")}
    for key, seconds in ages.items():
        kind, source = key.split("_")
        table[(kind, source)] = seconds
    return _broker().Policy(max_age=table)


def judge(wants, snapshot, holds=(), reg=None, pol=None, now=NOW):
    return _broker().decide_with_storage(reg if reg is not None else registry(), pol or policy(), now,
                                         wants, holds, snapshot)


def line(decision, name):
    [found] = [ln for ln in decision.lines if ln.name == name]
    return found


@contextlib.contextmanager
def alarm(seconds: int = 10):
    """An endless iterable that the broker consumes fails the test instead of hanging it."""
    def fire(signum, frame):
        raise AssertionError(f"blocked for {seconds}s")
    old = signal.signal(signal.SIGALRM, fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def refused(call, *args, **kw) -> str:
    with pytest.raises(_broker().BrokerError) as caught:
        call(*args, **kw)
    return caught.value.reason


# --------------------------------------------------------------------------- the facts a snapshot states

def test_a_good_snapshot_states_two_capacities_and_the_ledgers_hold():
    """Catches the arithmetic of the limit and the hold: bytes limit is `effective_free - reserve` (400), the
    hold is `reserved_bytes` (100), inodes limit `free - reserve_inodes` (4000) with a zero hold; both are
    `machine`, source `measured`, as of the snapshot's own second (microseconds cut, not rounded up)."""
    facts = _broker().storage_facts(snap())
    assert [(e.kind, e.name, e.limit, e.unit, e.source, e.as_of) for e in facts.entries] == [
        ("machine", BYTES, 400, "bytes", "measured", "2026-10-12T08:59:59Z"),
        ("machine", INODES, 4000, "items", "measured", "2026-10-12T08:59:59Z")]
    assert [(h.kind, h.name, h.amount, h.unit) for h in facts.holds] == [
        ("machine", BYTES, 100, "bytes"), ("machine", INODES, 0, "items")]
    assert dataclasses.is_dataclass(facts) and dataclasses.fields(facts)[0].name == "entries"
    with pytest.raises(dataclasses.FrozenInstanceError):
        facts.entries = ()
    assert _broker().storage_facts(snap(observed_utc="2026-10-12T09:00:00Z")).entries[0].as_of == NOW
    assert _broker().storage_facts(snap(observed_utc="2026-10-12T09:00:00.999999Z")).entries[0].as_of == NOW


def test_critical_is_a_limit_of_zero_and_a_zero_limit_is_not_a_missing_fact():
    """Catches a CRITICAL snapshot that still offers headroom (admission refuses every write-heavy operation
    there), and the other mistake: a limit of 0 read as 'no entry'. `state` CRITICAL zeroes both limits; so
    does `inode_state` CRITICAL alone; free below the reserve is 0, never negative. Negative control: the same
    figures in OPTIMIZE and PRESSURE keep their limits."""
    for over in ({"state": "CRITICAL"}, {"inode_state": "CRITICAL"}):
        facts = _broker().storage_facts(snap(**over))
        limits = {e.name: e.limit for e in facts.entries}
        assert limits[INODES] == 0
        assert limits[BYTES] == (0 if over.get("state") == "CRITICAL" else 400)
    assert {e.name: e.limit for e in _broker().storage_facts(snap(effective_free_bytes=200)).entries}[BYTES] == 0
    for state in ("OPTIMIZE", "PRESSURE"):
        assert {e.name: e.limit for e in _broker().storage_facts(snap(state=state)).entries} == \
            {BYTES: 400, INODES: 4000}
    decision = judge([amount(n=1)], snap(state="CRITICAL"))
    assert (decision.verdict, decision.reason) == ("refuse", "exceeds") and line(decision, BYTES).limit == 0
    # A want of 0 takes nothing, but it still waits behind open reservations when the limit is 0: `wait`, never
    # `grant`, while the ledger holds bytes (fail closed); with nothing reserved it is a grant.
    waiting = judge([amount(n=0)], snap(state="CRITICAL"))
    assert (waiting.verdict, waiting.reason) == ("wait", "exhausted") and line(waiting, BYTES).in_use == 100
    assert judge([amount(n=0)], snap(state="CRITICAL", reserved_bytes=0)).verdict == "grant"


class Str(str):
    """A str subclass: a spelling that compares equal to a good one and must still be refused."""


BAD_SNAPSHOTS = [
    pytest.param(None, id="none"), pytest.param([], id="list"), pytest.param("status", id="str"),
    pytest.param({}, id="empty"), pytest.param(object(), id="object"),
    pytest.param(snap(observed_utc=None), id="no-time"),
    pytest.param(snap(observed_utc="2026-10-12T09:00:00"), id="time-no-z"),
    pytest.param(snap(observed_utc="2026-10-12T09:00:00Z\n"), id="time-newline"),
    pytest.param(snap(observed_utc="2026-10-12 09:00:00Z"), id="time-space"),
    pytest.param(snap(observed_utc="2026-10-12T09:00:00.25Z"), id="time-short-micro"),
    pytest.param(snap(observed_utc="2026-1-2T09:00:00Z"), id="time-unpadded"),
    pytest.param(snap(observed_utc="2026-10-12T09:00:00z"), id="time-lowercase"),
    pytest.param(snap(observed_utc="2026-02-30T09:00:00Z"), id="time-not-a-day"),
    pytest.param(snap(observed_utc="\u0662\u0660\u0662\u0666-10-12T09:00:00Z"), id="time-arabic-digits"),
    pytest.param(snap(observed_utc=1760000000), id="time-int"),
    pytest.param(snap(observed_utc=Str("2026-10-12T09:00:00Z")), id="time-str-subclass"),
    pytest.param(snap(observed_utc="2026-10-12T09:00:00.\ud800Z"), id="time-lone-surrogate"),
    pytest.param(snap(observed_utc="2026-10-12T09:00:00\x00Z"), id="time-nul"),
    pytest.param(snap(observed_utc="2026-10-12T09:00:00Z".encode("utf-16")), id="time-utf16-bytes"),
    pytest.param(snap(state=Str("NORMAL")), id="state-str-subclass"),
    pytest.param(snap(state="NORMAL\x00"), id="state-nul"),
    pytest.param(snap(state=["NORMAL"]), id="state-list"),
    pytest.param(snap(state=None), id="state-none"), pytest.param(snap(state="normal"), id="state-case"),
    pytest.param(snap(state="NORMAL "), id="state-space"), pytest.param(snap(state="BROKEN"), id="state-odd"),
    pytest.param(snap(state=PressureState.NORMAL), id="state-enum"),
    pytest.param(snap(malformed_reservations=["r.json"]), id="malformed-ledger"),
    pytest.param(snap(malformed_reservations=None), id="malformed-none"),
    pytest.param(snap(malformed_reservations=()), id="malformed-tuple"),
    pytest.param({k: v for k, v in snap().items() if k != "malformed_reservations"}, id="malformed-missing"),
]


@pytest.mark.parametrize("bad", BAD_SNAPSHOTS)
def test_a_snapshot_that_cannot_be_read_states_nothing_and_asking_for_it_is_unknown(bad):
    """Catches fail-open on an odd snapshot: a missing, mistyped or oddly spelt time, an unknown state, a ledger
    with a malformed reservation (admission refuses until an operator renames it), a non-mapping. The facts are
    empty -- not zero, not unlimited -- and a request for the bytes is `refuse` / `unknown` with no limit."""
    facts = _broker().storage_facts(bad)
    assert facts.entries == () and facts.holds == ()
    decision = judge([amount(n=1)], bad)
    assert (decision.verdict, decision.reason) == ("refuse", "unknown")
    assert (line(decision, BYTES).limit, line(decision, BYTES).age) == (None, None)


class Hostile(dict):
    """A mapping that is a dict but raises when read, and one that answers differently every time."""

    def get(self, key, default=None):
        raise RuntimeError("unreadable")


class Flapping(dict):
    """A mapping that counts how often each key is read and answers `state` differently on every read."""

    def __init__(self):
        super().__init__(snap())
        self.reads: dict = {}

    def get(self, key, default=None):
        self.reads[key] = self.reads.get(key, 0) + 1
        value = super().get(key, default)
        return value if key != "state" or self.reads[key] % 2 else "CRITICAL"


def test_a_mapping_that_raises_is_unreadable_not_an_exception_and_is_read_once():
    """Catches a snapshot object that raises out of the broker (a fail-open of a different kind: the caller sees
    a crash, not a refusal) and one that changes between reads (a figure read twice can disagree with itself):
    the first reads as no facts, and the second is read at most once per key and its first answer stands."""
    assert _broker().storage_facts(Hostile(snap())).entries == ()
    flapping = Flapping()
    facts = _broker().storage_facts(flapping)
    assert flapping.reads and max(flapping.reads.values()) == 1
    assert {e.name: e.limit for e in facts.entries} == {BYTES: 400, INODES: 4000}


FIGURES = [(None, "none"), (-1, "negative"), (True, "bool"), (1.0, "float"), ("5", "str"), (10 ** 15 + 1, "ceiling")]


def _cases(keys, ceiling_keys):
    """`(key, value)` pairs; the over-ceiling figure only for the keys where it is a limit or a hold (a reserve
    above the ceiling is a limit of 0, which is a fact)."""
    return [pytest.param(k, v, id=f"{k}-{name}") for k in keys for v, name in FIGURES
            if name != "ceiling" or k in ceiling_keys]


@pytest.mark.parametrize("key,value", _cases(["effective_free_bytes", "reserve_bytes", "reserved_bytes"],
                                              ["effective_free_bytes", "reserved_bytes"]))
def test_a_byte_figure_that_is_not_a_whole_count_removes_the_byte_axis_only(key, value):
    """Catches a coerced figure (`True` as 1, `1.0` as 1, `'5'`), a missing `effective_free_bytes` (None is
    what an unobservable host reports) read as 0, and a figure above the registry's 10**15 read as a limit: the
    byte axis is absent (`unknown`), the inode axis is untouched."""
    over = {key: value}
    if key == "effective_free_bytes" and value == 10 ** 15 + 1:
        over = {key: 10 ** 15 + 1 + 300}           # a limit of 10**15 + 1 after the reserve
    facts = _broker().storage_facts(snap(**over))
    assert [e.name for e in facts.entries] == [INODES] and [h.name for h in facts.holds] == [INODES]
    assert (judge([amount(n=1)], snap(**over)).reason) == "unknown"
    assert line(judge([amount(n=1), amount(INODES, 1, "items")], snap(**over)), INODES).reason == "ok"


@pytest.mark.parametrize("key,value", _cases(["guest_free_inodes", "reserve_inodes"], ["guest_free_inodes"]))
def test_an_inode_figure_that_is_not_a_whole_count_removes_the_inode_axis_only(key, value):
    """Catches the same coercions on the inode axis, and a status without `reserve_inodes` (a snapshot of an
    older ledger) read as a floor of 0: the inode axis is absent, the byte axis is untouched."""
    over = {key: value}
    if value == 10 ** 15 + 1 and key == "guest_free_inodes":
        over = {key: 10 ** 15 + 1 + 1000}
    facts = _broker().storage_facts(snap(**over))
    assert [e.name for e in facts.entries] == [BYTES]
    missing = {k: v for k, v in snap().items() if k != key}
    assert [e.name for e in _broker().storage_facts(missing).entries] == [BYTES]
    assert judge([amount(n=1), amount(INODES, 1, "items")], snap(**over)).lines[1].reason == "unknown"


def test_an_inode_state_that_is_not_measured_is_not_a_fact_and_the_ceilings_are_exact():
    """Catches `NOT_MEASURED` (a filesystem with no inode table: free inodes read 0) taken as 0 free inodes,
    an unknown inode state taken as NORMAL, and the registry's ceiling moved: a limit of exactly 10**15 is a
    fact, one above it is not. Negative control: NORMAL is a fact."""
    for state in ("NOT_MEASURED", None, "normal", 3, Str("NORMAL"), "NORMAL\x00"):
        names = [e.name for e in _broker().storage_facts(snap(inode_state=state)).entries]
        assert names == [BYTES], state
    assert [e.name for e in _broker().storage_facts(snap(inode_state="NORMAL")).entries] == [BYTES, INODES]
    top = _broker().storage_facts(snap(effective_free_bytes=10 ** 15 + 300))
    assert top.entries[0].limit == 10 ** 15
    over = _broker().storage_facts(snap(effective_free_bytes=10 ** 15 + 301))
    assert [e.name for e in over.entries] == [INODES]
    huge_reserve = _broker().storage_facts(snap(reserve_bytes=10 ** 15 + 1, reserve_inodes=10 ** 15 + 1))
    assert [(e.name, e.limit) for e in huge_reserve.entries] == [(BYTES, 0), (INODES, 0)]
    held = _broker().storage_facts(snap(reserved_bytes=10 ** 15))
    assert held.holds[0].amount == 10 ** 15
    assert [h.name for h in _broker().storage_facts(snap(reserved_bytes=10 ** 15 + 1)).holds] == [INODES]


# --------------------------------------------------------------------------- the decision

def test_the_decision_is_the_ledgers_rule_with_wait_for_a_reservation_and_refuse_for_never():
    """Catches the three answers on storage: want 300 beside a hold of 100 and a limit of 400 is `grant`, 301 is
    `wait` / `exhausted` (it fits once the reservation is released), 401 is `refuse` / `exceeds` (it does not
    fit even then). The boundary is `effective - reserved - want >= reserve`, nothing else."""
    snapshot = snap()
    assert judge([amount(n=300)], snapshot).verdict == "grant"
    waiting = judge([amount(n=301)], snapshot)
    assert (waiting.verdict, waiting.reason) == ("wait", "exhausted")
    assert (line(waiting, BYTES).limit, line(waiting, BYTES).in_use, line(waiting, BYTES).wanted) == (400, 100, 301)
    never = judge([amount(n=401)], snapshot)
    assert (never.verdict, never.reason) == ("refuse", "exceeds")
    assert judge([amount(n=400)], snap(reserved_bytes=0)).verdict == "grant"
    assert judge([amount(n=401)], snap(reserved_bytes=0)).reason == "exceeds"


def test_the_byte_rule_is_the_admission_ledgers_rule_on_a_real_status(tmp_path):
    """Catches a broker that disagrees with `StorageAdmission` about the same ledger: with 100 GiB free and the
    default 30 GiB reserve, 70 GiB is admitted by `would_admit` and granted, 70 GiB + 1 is refused by both
    (`exceeds`); after a 10 GiB reservation is open, 60 GiB is admitted and granted and 60 GiB + 1 is refused by
    both (`wait`: the reservation is the hold); after it is released the answer is back. The snapshot is the
    real `status()` of a real admission on a fake `statvfs`."""
    adm, _, _ = make(tmp_path / "ledger", guest=100 * GiB)

    def both(estimate):
        status = adm.status()
        at = status["observed_utc"][:19] + "Z"             # the ledger's own clock, not this machine's
        decision = judge([amount(n=estimate)], status, now=at)
        return adm.would_admit("role_run", estimate_bytes=estimate), decision.verdict, decision.reason

    assert both(70 * GiB) == (True, "grant", "ok")
    assert both(70 * GiB + 1) == (False, "refuse", "exceeds")
    held = adm.reserve("p1", "role_run", estimate_bytes=10 * GiB)
    assert adm.status()["reserved_bytes"] == 10 * GiB
    assert both(60 * GiB) == (True, "grant", "ok")
    assert both(60 * GiB + 1) == (False, "wait", "exhausted")
    adm.release(held, 10 * GiB)
    assert both(70 * GiB) == (True, "grant", "ok")


def test_a_coordinators_observe_is_a_readable_snapshot(tmp_path):
    """Catches the key set the broker reads drifting from what the coordinator hands out: the dict
    `StorageCoordinator.observe()` returns (the admission's status plus `project_reservations`) states both
    capacities, reads the same limits as `status()`, and the extra key changes nothing."""
    world = World(tmp_path, free=100 * GiB, fake=FakeOS())
    observed = world.coordinator().observe()
    assert "project_reservations" in observed
    facts = _broker().storage_facts(observed)
    assert {e.name: e.limit for e in facts.entries}[BYTES] == 100 * GiB - 30 * GiB
    assert {e.name for e in facts.entries} == {BYTES, INODES}
    assert _broker().storage_facts(world.admission.status()).entries[0].limit == 70 * GiB
    assert facts.entries[0].as_of == _broker().storage_facts(observed).entries[0].as_of


def test_storage_is_judged_beside_the_registrys_own_capacities_and_the_worst_line_decides():
    """Catches a merge that drops, reorders or mis-digests: the decision has one line per want in
    `(kind, name)` order, quotes the digest of the registry *with* the storage entries (parse of render of
    both), and the worst line decides -- a `wait` on storage beside a `grant` on sessions is `wait`, a
    `refuse` beats it. Negative control: the same wants without the storage line are `grant`."""
    reg = registry(entry(name="host.sessions", limit=8, as_of=ago(10)),
                   entry(kind="provider", name="prov.requests", limit=50, unit="requests", source="declared"))
    wants = [amount("host.sessions", 2, "sessions"), amount(BYTES, 301)]
    decision = judge(wants, snap(), reg=reg)
    assert (decision.verdict, decision.reason) == ("wait", "exhausted")
    assert [ln.name for ln in decision.lines] == ["host.sessions", BYTES]
    assert [ln.verdict for ln in decision.lines] == ["grant", "wait"]
    cap = _capacity()
    facts = _broker().storage_facts(snap())
    assert decision.registry == cap.parse(cap.render(reg.entries + facts.entries)).digest
    assert decision.registry != reg.digest
    assert decision.now == NOW
    refusing = judge(wants + [amount("prov.requests", 99, "requests", "provider")], snap(), reg=reg)
    assert (refusing.verdict, refusing.reason) == ("refuse", "exceeds")
    assert judge([amount(n=0)], snap(), reg=reg).verdict == "grant"


def test_the_age_of_the_snapshot_is_judged_by_the_measured_machine_policy():
    """Catches a snapshot treated as fresh because its time was taken from the call: a figure older than the
    `machine`/`measured` policy age is `stale` (at the age exactly, still `ok`), a figure from after `now` is
    `future`, the `declared` and other kinds' ages change nothing, and cutting the microseconds makes a
    snapshot at most a second older, never newer. Negative control: the same snapshot, age within policy."""
    snapshot = snap(observed_utc="2026-10-12T08:58:59.900000Z")        # cut to :59, 61 s before NOW
    fresh = policy(machine_measured=61)
    assert judge([amount(n=1)], snapshot, pol=fresh).verdict == "grant"
    assert line(judge([amount(n=1)], snapshot, pol=fresh), BYTES).age == 61
    stale = judge([amount(n=1)], snapshot, pol=policy(machine_measured=60))
    assert (stale.verdict, stale.reason) == ("refuse", "stale")
    assert judge([amount(n=1)], snapshot, pol=policy(machine_declared=0, provider_measured=0,
                                                     dependency_measured=0, machine_measured=61)).verdict == "grant"
    future = judge([amount(n=1)], snap(observed_utc="2026-10-12T09:00:01.000000Z"))
    assert (future.verdict, future.reason) == ("refuse", "future")
    assert judge([amount(n=1)], snap(observed_utc="2026-10-12T09:00:00.999999Z")).verdict == "grant"


def test_an_unreadable_snapshot_cannot_be_walked_around_and_the_inputs_are_not_edited():
    """Catches the loopholes of a storage-aware decision: leaving `storage.bytes` out of the wants (an
    unreadable snapshot would then be a silent `grant`), listing a hold on `storage.bytes` (the ledger's
    reservations are that hold; a second would count the same bytes twice), and a registry that records a
    storage name itself (one writer per capacity -- neither a declared limit nor the snapshot may override the
    other, and an unreadable snapshot must not fall back to a declared figure: that would be a grant without a
    measurement). Also: nothing handed in is edited, and the answer is deterministic. Negative controls: a hold on
    `storage.inodes`, and `provider`/`storage.bytes`, are allowed."""
    other = [amount("host.sessions", 1, "sessions")]
    reg = registry(entry())
    assert refused(judge, other, snap(), reg=reg) == "invalid"
    assert refused(judge, [amount(n=1)], snap(), holds=[amount(n=1)]) == "invalid"
    for name, unit in ((BYTES, "bytes"), (INODES, "items")):
        recorded = registry(entry(name=name, unit=unit))
        assert refused(judge, [amount(n=1)], snap(), reg=recorded) == "registry"
        assert refused(judge, [amount(n=1)], None, reg=recorded) == "registry"   # not a declared fallback
    assert judge([amount(n=1)], snap(), holds=[amount(INODES, 1, "items")]).verdict == "grant"
    assert judge([amount(n=1)], snap(), reg=registry(entry(kind="provider", name=BYTES, unit="bytes"))
                 ).verdict == "grant"
    for not_a_registry in (None, "registry", [entry()], registry(entry()).entries):
        for storage in (snap(), None):
            assert refused(_broker().decide_with_storage, not_a_registry, policy(), NOW, [amount(n=1)], [],
                           storage) == "registry"
    snapshot, wants, holds = snap(), [amount(n=1), amount(INODES, 4000, "items")], [amount(INODES, 1, "items")]
    before = (copy.deepcopy(snapshot), list(wants), list(holds), reg.digest)
    first = judge(wants, snapshot, holds=holds, reg=reg)
    assert judge(list(reversed(wants)), MappingProxyType(snapshot), holds=holds, reg=reg) == first
    assert (snapshot, wants, holds, reg.digest) == before
    assert first.verdict == "wait" and line(first, INODES).in_use == 1


def test_the_registry_and_storage_must_fit_one_registry():
    """Catches an overflow past 256 entries surfacing as a raw `CapacityError` (a plain `ValueError` with no
    `reason` of the broker's): 254 recorded entries plus the two storage ones is exactly 256 and is judged; 255
    plus two is refused as `registry`. With no readable snapshot only the recorded 255 remain and are judged
    (storage is `unknown`, the registry is full of valid entries)."""
    names = [f"n{i:03d}" for i in range(255)]
    full = registry(*[entry(name=n) for n in names[:254]])
    assert judge([amount(n=1)], snap(), reg=full).verdict == "grant"
    over = registry(*[entry(name=n) for n in names])
    assert refused(judge, [amount(n=1)], snap(), reg=over) == "registry"
    assert judge([amount(n=1)], None, reg=over).reason == "unknown"


def test_decide_itself_and_the_registrys_purity_are_unchanged():
    """Catches an edit of `decide` for storage's sake: its parameters are still the five required ones in the
    same order, `decide_with_storage` has the same five and a sixth, nothing has a default, `storage_facts` takes
    one argument, and `decide` still answers `unknown` for storage nobody recorded."""
    b = _broker()
    assert list(inspect.signature(b.decide).parameters) == ["registry", "policy", "now", "wants", "holds"]
    assert list(inspect.signature(b.decide_with_storage).parameters) == [
        "registry", "policy", "now", "wants", "holds", "storage"]
    assert list(inspect.signature(b.storage_facts).parameters) == ["storage"]
    for fn in (b.decide, b.decide_with_storage, b.storage_facts):
        assert all(p.default is inspect.Parameter.empty for p in inspect.signature(fn).parameters.values())
    plain = b.decide(registry(entry()), policy(), NOW, [amount(n=1)], [])
    assert (plain.verdict, plain.reason) == ("refuse", "unknown")
    assert (b.STORAGE_KIND, b.STORAGE_BYTES, b.STORAGE_INODES) == ("machine", "storage.bytes", "storage.inodes")


# --------------------------------------------------------------------------- the ledger's inode line

class SizedStatvfs(FakeStatvfs):
    """`FakeStatvfs` that also reports `f_files` (`None`: not reported)."""

    def __init__(self, free, inodes, total):
        super().__init__(free, inodes)
        self.total = total

    def __call__(self, path):
        answer = super().__call__(path)
        if self.total is not None:
            answer.f_files = self.total
        return answer


def test_status_states_the_inode_line_the_axis_calls_critical(tmp_path):
    """Catches `reserve_inodes` that disagrees with the axis it summarises: with a reported total of 67 108 864
    the line is `ceil(total * 0.03)` = 2 013 266 (above the absolute 100 000), with no total or a total of 0 the
    absolute floor, with a small total the floor again; free inodes one below the line are CRITICAL on the
    axis, free inodes at the line are not. And the broker reads it: free 5 000 000 gives `5 000 000 - line`.
    Negative control: a changed ratio moves the line."""
    total = 67_108_864
    cases = [(total, StoragePolicy(), 2_013_266), (None, StoragePolicy(), 100_000), (0, StoragePolicy(), 100_000),
             (1_000_000, StoragePolicy(), 100_000), (100_000_000, StoragePolicy(), 3_000_000),
             (total, StoragePolicy(inode_critical_ratio=0.05, inode_pressure_ratio=0.10), 3_355_444)]
    statuses = []
    for k, (size, pol, expected) in enumerate(cases):
        adm, _, _ = make(tmp_path / f"ledger{k}", statvfs=SizedStatvfs(500 * GiB, 5_000_000, size), policy=pol)
        status = adm.status()
        assert status["reserve_inodes"] == expected, (size, status["reserve_inodes"])
        tracker = StateTracker(pol, None)
        assert tracker.inode_floor(size) == expected
        if size:
            assert tracker.inode_axis(expected - 1, size) is PressureState.CRITICAL
            assert tracker.inode_axis(expected, size) is not PressureState.CRITICAL
        statuses.append(status)
    assert {e.name: e.limit for e in _broker().storage_facts(statuses[0]).entries}[INODES] == 5_000_000 - 2_013_266
    assert statuses[2]["inode_state"] == "NOT_MEASURED"
    assert INODES not in [e.name for e in _broker().storage_facts(statuses[2]).entries]


def test_the_inode_line_is_a_whole_count_for_every_policy_the_ledger_takes():
    """Catches a line that is a float or negative (the registry refuses either as `invalid`, which would drop
    the inode axis quietly): across totals and ratios the line is an `int`, never below the absolute floor, and
    exactly `max(floor, ceil(total * ratio))`."""
    for total in (None, 0, 1, 99, 1_000, 67_108_864, 10 ** 12):
        for ratio in (0.0, 0.03, 0.5, 0.99):
            pol = StoragePolicy(inode_critical_ratio=ratio, inode_pressure_ratio=0.995, inode_normal_ratio=1.0)
            floor = StateTracker(pol, None).inode_floor(total)
            assert type(floor) is int
            assert floor == max(100_000, math.ceil(total * ratio) if total else 0)


# --------------------------------------------------------------------------- the age of a derived figure

def test_a_figure_derived_from_an_old_host_reading_is_as_old_as_the_reading(tmp_path):
    """Catches a derived host figure stated as fresh (review A-F1): the host is read at T and then unobservable;
    899 s later the ledger still admits on the term it derives (up to its 900 s limit), `status()` says
    `host_unknown` and names the reading, and the broker dates the byte fact from that reading -- so a 60 s
    `machine`/`measured` policy answers `stale` (age 899) where a 900 s one grants, and the inode fact, a guest
    figure read now, keeps age 0. Negative control: the same admission with the host observable is age 0 and
    `host_reading_utc` is None; and at 900 s admission itself has no figure and the byte axis is absent."""
    host = FakeHost(100 * GiB)
    adm, _, clock = make(tmp_path / "a", guest=500 * GiB, host=host)
    adm.observe()
    fresh = adm.status()
    assert fresh["host_unknown"] is False and fresh["host_reading_utc"] is None
    at_fresh = fresh["observed_utc"][:19] + "Z"
    assert line(judge([amount(n=1)], fresh, pol=policy(machine_measured=0), now=at_fresh), BYTES).age == 0
    recorded = fresh["observed_utc"]
    host.free = None
    clock.advance(899)
    derived = adm.status()
    assert derived["host_unknown"] is True and derived["effective_free_bytes"] == 100 * GiB
    assert derived["host_reading_utc"] == recorded
    assert adm.would_admit("role_run", estimate_bytes=1 * GiB) is True          # admission's own, wider, age limit
    now = derived["observed_utc"][:19] + "Z"
    stale = judge([amount(n=1), amount(INODES, 1, "items")], derived, pol=policy(machine_measured=60), now=now)
    assert (line(stale, BYTES).verdict, line(stale, BYTES).reason, line(stale, BYTES).age) == ("refuse", "stale", 899)
    assert (line(stale, INODES).verdict, line(stale, INODES).age) == ("grant", 0)
    assert (stale.verdict, stale.reason) == ("refuse", "stale")
    assert judge([amount(n=1)], derived, pol=policy(machine_measured=899), now=now).verdict == "grant"
    assert judge([amount(n=1)], derived, pol=policy(machine_measured=898), now=now).reason == "stale"
    clock.advance(1)
    gone = adm.status()
    assert gone["effective_free_bytes"] is None and gone["host_reading_utc"] is None
    assert [e.name for e in _broker().storage_facts(gone).entries] == [INODES]


@pytest.mark.parametrize("over", [
    pytest.param({"host_unknown": None}, id="unknown-none"),
    pytest.param({"host_unknown": 1}, id="unknown-int"),
    pytest.param({"host_unknown": "False"}, id="unknown-str"),
    pytest.param({"host_unknown": True}, id="derived-without-reading"),
    pytest.param({"host_unknown": True, "host_reading_utc": "2026-10-12T08:00:00"}, id="reading-no-z"),
    pytest.param({"host_unknown": True, "host_reading_utc": "2026-10-12T08:00:00.5Z"}, id="reading-short-micro"),
    pytest.param({"host_unknown": True, "host_reading_utc": Str("2026-10-12T08:00:00.000000Z")}, id="reading-subclass"),
    pytest.param({"host_unknown": True, "host_reading_utc": 1760000000}, id="reading-int"),
    pytest.param({"host_unknown": True, "host_reading_utc": "2026-10-12T09:00:00.000000Z"}, id="reading-after-observed"),
])
def test_a_derived_figure_without_a_readable_reading_time_removes_the_byte_axis_only(over):
    """Catches a derived figure whose age cannot be known taken as fresh: `host_unknown` missing or not a bool, or
    True with no reading time, an odd spelling, a subclass, an int, or a reading from after the snapshot -- the
    byte axis is absent (`unknown`), the inode axis (a guest figure) is untouched. A snapshot with no
    `host_unknown` key at all (an older ledger) is the same. Negative controls: a reading before the snapshot,
    equal to it, and `host_unknown` False are facts."""
    snapshot = snap(**over)
    assert [e.name for e in _broker().storage_facts(snapshot).entries] == [INODES]
    assert judge([amount(n=1)], snapshot).reason == "unknown"
    missing = {k: v for k, v in snap().items() if k != "host_unknown"}
    assert [e.name for e in _broker().storage_facts(missing).entries] == [INODES]
    for good in ("2026-10-12T08:00:00.000000Z", "2026-10-12T08:59:59.250000Z", "2026-10-12T08:59:59Z"):
        facts = _broker().storage_facts(snap(host_unknown=True, host_reading_utc=good))
        assert [e.name for e in facts.entries] == [BYTES, INODES]
        assert facts.entries[0].as_of == good[:19] + "Z" and facts.entries[1].as_of == "2026-10-12T08:59:59Z"


def test_the_callers_holds_are_at_most_1022_because_the_ledger_adds_two():
    """Catches the unstated bound (review A-F3, B-F2): `decide` takes 1024 holds and the ledger adds one per
    axis, so a readable snapshot used to refuse 1023 caller holds as `invalid` while an unreadable one granted
    them. Now the bound is the same whether or not the snapshot is readable: 1022 holds are judged, 1023 are
    `invalid` (`more than 1022`). An endless iterable is not consumed past 1023 items. Negative control: plain
    `decide` still takes 1024."""
    many = lambda n: [amount(INODES, 1, "items") for _ in range(n)]
    for storage in (snap(), None):
        assert judge([amount(n=1)], storage, holds=many(1022)).reason in ("ok", "unknown")
        assert refused(judge, [amount(n=1)], storage, holds=many(1023)) == "invalid"
        assert refused(judge, [amount(n=1)], storage, holds=many(1024)) == "invalid"
    taken = []

    def endless():
        while True:
            taken.append(1)
            yield amount(INODES, 1, "items")

    with alarm():
        assert refused(judge, [amount(n=1)], snap(), holds=endless()) == "invalid"
    assert len(taken) == 1023
    plain = _broker().decide(registry(entry()), policy(), NOW, [amount("host.sessions", 1, "sessions")],
                             [amount("host.sessions", 0, "sessions") for _ in range(1024)])
    assert plain.verdict == "grant"
