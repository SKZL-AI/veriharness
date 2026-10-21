"""The capacity broker: one pure decision, "may this much be taken now?" (plan row P3-17, slice 1).

The registry (`hoh.capacity`, P3-16) records what *exists*: a limit per capacity, of three kinds that are never
added to each other (`dependency`, `machine`, `provider`). Whoever starts work -- the scheduler (P3-19), the
router (P3-41) -- has to turn that record into one answer before it starts anything. This module is that answer
and nothing else: given a registry, a freshness policy, the time, what is wanted and what is already held, it
returns a `Decision` that says `grant`, `wait` or `refuse`, line by line, and quotes the digest of the registry
it judged.

**What this slice is.** A function of its arguments. It reads no file and no clock, writes nothing, runs
nothing, measures nothing and remembers nothing. The registry is handed in (a `capacity.Registry` the reader
produced); the time is the caller's; what is already held is the caller's too and has no default, because an
absent fact is never zero. **What it is not.** It reserves nothing: a `grant` is a judgement over the snapshot
it was given, not an exclusive claim -- two callers that each ask the same snapshot can each be granted the same
headroom. Making a grant exclusive is a reservation (P3-04's `CapacityReservation`), and picking who is asked
first is a scheduler's (P3-19). Where the registry file lives, who writes the holds and how a decision is
recorded durably are later slices.

**Fail closed.** A capacity nobody recorded is `refuse` / `unknown` -- not zero and not unlimited. A fact older
than its policy age is `refuse` / `stale`, and one from after `now` is `refuse` / `future`. A hold in another
unit than the capacity's is `refuse` / `unit`. A request that is itself malformed (a value that breaks a rule,
a registry that is not exactly what the reader produces) raises `BrokerError`; there is no decision then.

**Three answers.** `grant`: every line fits. `wait`: every line could fit once what is held is given back
(`exhausted`) -- the one answer that is worth asking again. `refuse`: some line cannot be granted on this
registry as it stands (`unknown`, `unit`, `stale`, `future`, `exceeds`); asking again with the same facts
changes nothing. One decision, all or nothing: no part of a request is granted when another part is not.

**Storage (plan row P3-39, slice 1).** Bytes and inodes are one more capacity of kind `machine`: the host's
free space is a fact the storage coordinator already observes (`StorageCoordinator.observe()`, the admission
ledger's `status()`), and `decide_with_storage` turns that snapshot -- handed in, like the registry; this module
still reads no file and measures nothing -- into the capacities `storage.bytes` (unit `bytes`) and
`storage.inodes` (unit `items`) beside the registry's own, then judges as `decide` does. The limit is what may
still be taken *above the admission reserve*, so a `grant` is exactly the ledger's own rule
(`effective free - open reservations - estimate >= reserve`); the ledger's open reservations are the holds.
A byte figure the ledger derived from an older host reading is dated from that reading, not from the snapshot,
so the operator's age judges it. A figure the snapshot does not state, or states in a shape this module does not
know, is not a fact: that capacity is absent and a request for it is `refuse` / `unknown`. Never zero, never
unlimited.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import islice

from .capacity import KINDS, SOURCES, CapacityError, Entry, Registry, parse, render

VERDICTS = ("grant", "wait", "refuse")
"""What a decision (and each of its lines) says."""

REASONS = ("ok", "exhausted", "exceeds", "stale", "future", "unknown", "unit")
"""Why. `ok` goes with `grant`, `exhausted` with `wait`, the rest with `refuse`."""

MAX_WANTS = 64
"""Lines one request may have (one per capacity)."""

MAX_HOLDS = 1024
"""Holds one decision looks at; the caller sums nothing for the broker, it lists them."""

MAX_AGE = 366 * 24 * 60 * 60
"""The largest age a policy may allow a fact: 366 days (a leap year), in seconds."""

STORAGE_KIND = "machine"
"""The kind storage is recorded under: what this host can carry."""

STORAGE_BYTES = "storage.bytes"
"""The name of the byte capacity (unit `bytes`): headroom above the admission reserve."""

STORAGE_INODES = "storage.inodes"
"""The name of the inode capacity (unit `items`): free inodes above the inode reserve."""

_EPOCH = "2000-01-01T00:00:00Z"
_TIME = "%Y-%m-%dT%H:%M:%SZ"
_TIME_MICRO = "%Y-%m-%dT%H:%M:%S.%fZ"
_STATES = ("NORMAL", "OPTIMIZE", "PRESSURE", "CRITICAL")
_SNAPSHOT_KEYS = ("state", "observed_utc", "effective_free_bytes", "reserve_bytes", "reserved_bytes",
                  "malformed_reservations", "guest_free_inodes", "reserve_inodes", "inode_state",
                  "host_unknown", "host_reading_utc")
_LEDGER_HOLDS = 2
"""The holds the ledger adds to a decision (bytes and inodes); the caller's own may be this many fewer than
`MAX_HOLDS`, whether or not the snapshot can be read."""
_RANK = {"grant": 0, "wait": 1, "refuse": 2}


class BrokerError(ValueError):
    """A request the broker will not judge. `reason` is `invalid` (a value, a count or a type breaks a rule) or
    `registry` (the object is not exactly what `capacity.parse` produces for its entries)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"capacity broker: {message}")
        self.reason = reason


@dataclass(frozen=True)
class Amount:
    """`amount` `unit`s of the capacity `kind`/`name` -- what is wanted, or what is already held. The fields obey
    the registry's own grammar: an `Amount` is valid exactly when an `Entry` with the same fields is."""

    kind: str
    name: str
    amount: int
    unit: str

    def __post_init__(self) -> None:
        try:
            Entry(kind=self.kind, name=self.name, limit=self.amount, unit=self.unit, source="declared",
                  as_of=_EPOCH)
        except CapacityError as exc:
            raise BrokerError("invalid", f"not an amount: {exc}") from exc


@dataclass(frozen=True, init=False)
class Policy:
    """How old a fact may be, in whole seconds, for each `(kind, source)` -- all six pairs, no default: a
    measured machine figure is useful for minutes, a declared provider limit for days, and which is which is
    the operator's to say. `rows` is `((kind, source, seconds), ...)` in sorted order."""

    rows: tuple

    def __init__(self, max_age: Mapping) -> None:
        wanted = {(kind, source) for kind in KINDS for source in SOURCES}
        if not isinstance(max_age, Mapping) or not all(
                type(key) is tuple and len(key) == 2 and type(key[0]) is str and type(key[1]) is str
                for key in max_age) or set(max_age) != wanted:
            raise BrokerError("invalid", "max_age is a mapping with exactly the six keys (kind, source)")
        rows = []
        for kind, source in sorted(wanted):
            seconds = max_age[(kind, source)]
            if type(seconds) is not int or not 0 <= seconds <= MAX_AGE:
                raise BrokerError("invalid", f"the age for {kind}/{source} is a whole number of seconds "
                                             f"from 0 to {MAX_AGE}")
            rows.append((kind, source, seconds))
        object.__setattr__(self, "rows", tuple(rows))

    def seconds(self, kind: str, source: str) -> int:
        for row_kind, row_source, seconds in self.rows:
            if (row_kind, row_source) == (kind, source):
                return seconds
        raise BrokerError("invalid", "no such (kind, source)")


@dataclass(frozen=True)
class Line:
    """The judgement of one wanted capacity: what was wanted, what the registry said (`limit`, `age` in seconds;
    `None` where there was no entry), what was held, and the verdict with its reason."""

    kind: str
    name: str
    unit: str
    wanted: int
    limit: int | None
    in_use: int
    age: int | None
    verdict: str
    reason: str

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in ("kind", "name", "unit", "wanted", "limit", "in_use",
                                                       "age", "verdict", "reason")}


@dataclass(frozen=True)
class Decision:
    """The answer: the worst line's `verdict` and `reason` (the first of them in `(kind, name)` order), the
    digest of the registry it was judged on, the `now` it was judged at, and every line."""

    verdict: str
    reason: str
    registry: str
    now: str
    lines: tuple[Line, ...]

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "reason": self.reason, "registry": self.registry, "now": self.now,
                "lines": [line.as_dict() for line in self.lines]}


def _verified(registry: object) -> None:
    """A `Registry` that is exactly what the reader produces: its entries render and parse back to itself, so
    its digest is the digest of real, canonical, valid bytes. A hand-built or edited one is refused."""
    if type(registry) is not Registry:
        raise BrokerError("registry", f"a {type(registry).__name__} is not a capacity.Registry")
    try:
        again = parse(render(registry.entries))
    except (TypeError, ValueError, RecursionError) as exc:
        raise BrokerError("registry", "the entries of the registry are not valid entries") from exc
    if again != registry:
        raise BrokerError("registry", "the registry is not what capacity.parse produces for its entries")


def _amounts(items: Iterable, limit: int, what: str, needed: bool) -> list[Amount]:
    try:
        iterator = iter(items)
    except TypeError as exc:
        raise BrokerError("invalid", f"{what} is an iterable of Amount, not a {type(items).__name__}") from exc
    got = list(islice(iterator, limit + 1))        # what the caller's own iterable raises is not caught here
    if len(got) > limit:
        raise BrokerError("invalid", f"{what}: more than {limit}")
    if needed and not got:
        raise BrokerError("invalid", f"{what}: at least one is needed")
    for item in got:
        if type(item) is not Amount:
            raise BrokerError("invalid", f"{what} may contain only Amount objects, not a {type(item).__name__}")
    return [Amount(item.kind, item.name, item.amount, item.unit) for item in got]   # again: it may be edited


def _time(now: object) -> datetime:
    try:
        Entry(kind="machine", name="clock", limit=0, unit="items", source="declared", as_of=now)
    except CapacityError as exc:
        raise BrokerError("invalid", f"now is not YYYY-MM-DDTHH:MM:SSZ: {exc}") from exc
    return datetime.strptime(now, _TIME)


def _seconds(delta: timedelta) -> int:
    """Whole seconds of a difference, in integers: `days` and `seconds` are both parts of it."""
    return delta.days * 86400 + delta.seconds


def _judge(want: Amount, entry: Entry | None, in_use: int, held_units: set, policy: Policy,
           at: datetime) -> Line:
    """One line, by the stated order: no entry (`unknown`), a unit that differs (`unit`), a fact from after now
    (`future`), a fact older than its policy age (`stale`), a request larger than the limit itself (`exceeds`),
    a request that does not fit beside what is held (`exhausted`), else `ok`."""
    if entry is None:
        return Line(want.kind, want.name, want.unit, want.amount, None, in_use, None, "refuse", "unknown")
    age = _seconds(at - datetime.strptime(entry.as_of, _TIME))

    def line(verdict: str, reason: str) -> Line:
        return Line(want.kind, want.name, want.unit, want.amount, entry.limit, in_use, age, verdict, reason)

    if want.unit != entry.unit or held_units - {entry.unit}:
        return line("refuse", "unit")
    if age < 0:
        return line("refuse", "future")
    if age > policy.seconds(entry.kind, entry.source):
        return line("refuse", "stale")
    if want.amount > entry.limit:
        return line("refuse", "exceeds")
    if in_use + want.amount > entry.limit:
        return line("wait", "exhausted")
    return line("grant", "ok")


def decide(registry: Registry, policy: Policy, now: str, wants: Iterable[Amount],
           holds: Iterable[Amount]) -> Decision:
    """May all of `wants` be taken at `now`, beside `holds`, on `registry`? Every argument is required.

    `holds` lists what is already taken (one `Amount` per holder; amounts of one capacity are summed), and is
    never defaulted: an empty tuple says "nothing is held". Holds on a capacity that is not wanted, or that the
    registry does not know, are not looked at. `now` is read after the registry was: a fact written after
    `now` is `future`. Pure; returns a `Decision` or raises `BrokerError`."""
    _verified(registry)
    if type(policy) is not Policy:
        raise BrokerError("invalid", f"a {type(policy).__name__} is not a Policy")
    try:
        policy = Policy({(kind, source): seconds for kind, source, seconds in policy.rows})   # again: edited?
    except (TypeError, ValueError) as exc:
        raise BrokerError("invalid", "the rows of the policy are not a policy") from exc
    at = _time(now)
    wanted = _amounts(wants, MAX_WANTS, "wants", True)
    held = _amounts(holds, MAX_HOLDS, "holds", False)
    keys = [(want.kind, want.name) for want in wanted]
    if len(set(keys)) != len(keys):
        raise BrokerError("invalid", "a capacity is wanted twice; ask for the sum in one line")
    entries = {(entry.kind, entry.name): entry for entry in registry.entries}
    taken: dict[tuple[str, str], int] = {}
    units: dict[tuple[str, str], set] = {}
    for hold in held:
        key = (hold.kind, hold.name)
        taken[key] = taken.get(key, 0) + hold.amount
        units.setdefault(key, set()).add(hold.unit)
    lines = tuple(
        _judge(want, entries.get((want.kind, want.name)), taken.get((want.kind, want.name), 0),
               units.get((want.kind, want.name), set()), policy, at)
        for want in sorted(wanted, key=lambda w: (w.kind, w.name)))
    worst = lines[0]
    for line in lines[1:]:
        if _RANK[line.verdict] > _RANK[worst.verdict]:
            worst = line
    return Decision(verdict=worst.verdict, reason=worst.reason, registry=registry.digest, now=now, lines=lines)


@dataclass(frozen=True)
class StorageFacts:
    """What a storage snapshot states: the capacity `entries` (zero, one or two: bytes, inodes) and the `holds`
    the ledger already has on them. Both empty when the snapshot cannot be read."""

    entries: tuple[Entry, ...]
    holds: tuple[Amount, ...]


def _count(value: object) -> int | None:
    """A whole count that is not a bool, or `None`."""
    return value if type(value) is int and value >= 0 else None


def _observed(value: object) -> str | None:
    """`YYYY-MM-DDTHH:MM:SSZ` for the snapshot's `observed_utc` (the ledger writes microseconds; they are cut,
    so the fact is at most a second older than it was), or `None` for anything but exactly those spellings."""
    if type(value) is not str:
        return None
    for spelling in (_TIME_MICRO, _TIME):
        try:
            parsed = datetime.strptime(value, spelling)
        except ValueError:
            continue
        if parsed.strftime(spelling) == value:              # strptime is lenient; only the exact form counts
            return parsed.strftime(_TIME)
    return None


def _reading_time(host_unknown: object, reading: object, observed: str) -> str | None:
    """The time the byte figure rests on, or `None` when it cannot be stated. A host read now: the snapshot's own
    time. A figure the ledger derived from a persisted host reading: that reading's time, which must be an exact
    spelling and not after the snapshot's. Anything else (no `host_unknown`, a non-bool) states nothing."""
    if host_unknown is False:
        return observed
    if host_unknown is not True:
        return None
    when = _observed(reading)
    return when if when is not None and when <= observed else None


def storage_facts(storage: object) -> StorageFacts:
    """The capacities and holds a storage snapshot states -- the dict `StorageCoordinator.observe()` returns --
    or nothing at all when it cannot be read. Pure; never raises for a bad snapshot (a bad snapshot is an
    absent fact, and the broker answers an absent fact `unknown`).

    The snapshot is read whole or not at all when it is not a mapping, has no exact `observed_utc`, has a
    `state` other than NORMAL / OPTIMIZE / PRESSURE / CRITICAL, or its `malformed_reservations` is missing,
    not a list or not empty (the ledger cannot be judged; admission refuses the same). Then, per axis:

    * `storage.bytes`: needs `effective_free_bytes` (None when the host is unobservable and no persisted host
      reading under the ledger's own age limit exists), `reserve_bytes`, `reserved_bytes` (whole counts) and
      `host_unknown` (a bool). Limit `effective_free_bytes - reserve_bytes`, at least 0, and 0 in CRITICAL; hold
      `reserved_bytes`. **Its time is the age of the reading it rests on**: `observed_utc` when the host was
      read now (`host_unknown` False); when the ledger derived the figure from a persisted host reading
      (`host_unknown` True, up to its `host_max_age_s` old) the time of that reading, `host_reading_utc`, which
      must be exact and not after `observed_utc` or the axis is absent. So the operator's age decides, and a
      derived figure is never stated as fresher than its reading.
    * `storage.inodes`: needs `guest_free_inodes`, `reserve_inodes` (whole counts) and an `inode_state` that
      is one of the four states -- `NOT_MEASURED` (a filesystem with no fixed inode table) is absent. Limit
      `guest_free_inodes - reserve_inodes`, at least 0, and 0 in CRITICAL (either state); the ledger holds no
      inodes, so the hold is 0.

    An axis whose figures the registry cannot hold (above its ceiling) is absent too. Time of both facts is
    the snapshot's own `observed_utc`, source `measured`: staleness and `future` are judged by `decide`."""
    none = StorageFacts((), ())
    if not isinstance(storage, Mapping):
        return none
    try:                                       # each key once: a figure is never read twice and disagrees
        seen = {key: storage.get(key) for key in _SNAPSHOT_KEYS}
    except Exception:                          # a snapshot that cannot be read is no fact, not a crash
        return none
    as_of = _observed(seen["observed_utc"])
    state, malformed, inode_state = seen["state"], seen["malformed_reservations"], seen["inode_state"]
    if as_of is None or type(state) is not str or state not in _STATES:
        return none
    if type(malformed) is not list or malformed:
        return none
    entries: list[Entry] = []
    holds: list[Amount] = []
    free, reserve, reserved = (_count(seen[key]) for key in
                               ("effective_free_bytes", "reserve_bytes", "reserved_bytes"))
    bytes_as_of = _reading_time(seen["host_unknown"], seen["host_reading_utc"], as_of)
    if free is not None and reserve is not None and reserved is not None and bytes_as_of is not None:
        limit = 0 if state == "CRITICAL" else max(0, free - reserve)
        try:
            entries.append(Entry(kind=STORAGE_KIND, name=STORAGE_BYTES, limit=limit, unit="bytes",
                                 source="measured", as_of=bytes_as_of))
            holds.append(Amount(STORAGE_KIND, STORAGE_BYTES, reserved, "bytes"))
        except (CapacityError, BrokerError):
            entries, holds = [], []
    free_inodes, floor = (_count(seen[key]) for key in ("guest_free_inodes", "reserve_inodes"))
    if free_inodes is not None and floor is not None and type(inode_state) is str \
            and inode_state in _STATES:
        limit = 0 if "CRITICAL" in (state, inode_state) else max(0, free_inodes - floor)
        try:
            entry = Entry(kind=STORAGE_KIND, name=STORAGE_INODES, limit=limit, unit="items",
                          source="measured", as_of=as_of)
            hold = Amount(STORAGE_KIND, STORAGE_INODES, 0, "items")
        except (CapacityError, BrokerError):
            pass
        else:
            entries.append(entry)
            holds.append(hold)
    return StorageFacts(tuple(entries), tuple(holds))


def decide_with_storage(registry: Registry, policy: Policy, now: str, wants: Iterable[Amount],
                        holds: Iterable[Amount], storage: object) -> Decision:
    """`decide`, with the storage the snapshot states judged as one more capacity. Every argument is required.

    `wants` must name `machine`/`storage.bytes` (a launch that may write asks for the bytes it will write; a
    storage-aware decision cannot be asked without it, so an unreadable snapshot cannot be walked around by
    leaving storage out) and may name `machine`/`storage.inodes`. `holds` lists no `machine`/`storage.bytes`:
    the ledger's open reservations are the holds on it, and a caller's own would count the same bytes twice.
    The registry must not record either storage name itself -- one writer per capacity; a registry that does
    is refused, not overridden either way. The caller lists at most 1022 holds (`decide`'s 1024 less the two the
    ledger adds), whether or not the snapshot can be read. The `Decision` quotes the digest of the registry as it
    was judged, the storage entries included. Pure; raises `BrokerError` where `decide` does, and for the rules
    above."""
    wanted = _amounts(wants, MAX_WANTS, "wants", True)
    held = _amounts(holds, MAX_HOLDS - _LEDGER_HOLDS, "holds", False)
    if (STORAGE_KIND, STORAGE_BYTES) not in {(want.kind, want.name) for want in wanted}:
        raise BrokerError("invalid", f"a storage-aware decision wants {STORAGE_KIND}/{STORAGE_BYTES}")
    if any((hold.kind, hold.name) == (STORAGE_KIND, STORAGE_BYTES) for hold in held):
        raise BrokerError("invalid", f"{STORAGE_KIND}/{STORAGE_BYTES} is held by the ledger; list no hold on it")
    _verified(registry)
    recorded = {(entry.kind, entry.name) for entry in registry.entries}
    if {(STORAGE_KIND, STORAGE_BYTES), (STORAGE_KIND, STORAGE_INODES)} & recorded:
        raise BrokerError("registry", "the registry records a storage capacity itself; the snapshot states it")
    facts = storage_facts(storage)
    try:
        merged = parse(render(registry.entries + facts.entries))
    except CapacityError as exc:
        raise BrokerError("registry", f"the registry and storage do not fit one registry: {exc}") from exc
    return decide(merged, policy, now, wanted, held + list(facts.holds))
