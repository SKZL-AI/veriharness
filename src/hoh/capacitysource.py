"""The capacity source: one snapshot of what the scheduler may count on, each fact with its time and its source
(plan row P3-16, slice 3).

The registry (`hoh.capacity`) records the limits an operator *declared*. The plan's Capacity Source is more: it also
holds what was *observed* (a measured cap), what a provider's own refusals say right now (quota, rate limit, health,
the reset time) and how many slots are in use or reserved -- and it says how old each of those is. This module is that
snapshot and nothing else. It reads three records the product already keeps and probes nothing:

* **caps** -- every entry of the capacity registry: `declared_cap` (a limit the operator stated) or `observed_cap`
  (a limit a probe measured), observed at the entry's `as_of`, bounded by the broker's own policy age for its
  `(kind, source)`;
* **provider state** -- `health.read_health` over the runs root: for each `(provider, account)` the parked runs name,
  its `health_state`, `reset_at` (the latest stated reset) and `failures` (how many parks of which class: a usage limit
  is a quota, a rate limit is a rate), plus one `problems` fact (what could not be read) and one `unnamed` fact (pending
  limits no provider could be named for); and, for every `provider` capacity of the registry, the `observed_safe_cap`
  that state leaves of it (the limit less what `decide` would hold back);
* **slots** -- `capacitycheck.live_holds` over the same root: `active` sessions (live run leases) and `reserved`
  dispatches (open budget reservations); and, only while a wave holds any, the `reserved` bytes and inodes of
  `machine/storage.bytes` and `machine/storage.inodes` (P3-39: what the waves in flight were admitted against).

**Every fact is a claim about a time.** Provider state and slots are observed when the snapshot is taken (`now`, the
caller's clock: the module reads none); a fact is judged against a later `now` by `facts`: older than its bound, from
after `now`, or not readable at all, it is **UNKNOWN** -- its value is withheld, never kept as the last known number.
An absent fact is not zero and not unlimited.

**What the snapshot narrows (`decide`).** The broker (`broker.decide`) still makes the judgement; the snapshot decides
what it is handed. A stale or future cap is the broker's own `refuse` / `stale` / `future` (the same rule, pinned
against the broker at its boundary). Slots that are UNKNOWN make **no decision** (`CheckError`, as an unreadable lease
already did): a hold that cannot be known is never a hold of zero. Provider state narrows a wanted `provider`
capacity by **holding part of it**, so the registry digest the decision quotes stays the record's own and the broker's
answer is `wait` / `exhausted`, with the reason in `narrowed`: a pending limit (`LIMITED`) or a park the resume loop gave
up on (`GIVEN_UP`) holds the whole capacity, `DEGRADED` and `RESET_DUE` hold half (rounded up) -- the table
backpressure already applies to a wave's width (`HOLD_RULE`, one row per `HealthState`) -- and a provider reading that is
incomplete or UNKNOWN holds the whole of every provider capacity (the evidence it lacks may be about any provider).
A provider capacity belongs to a provider by its name: `<provider>` or `<provider>.<anything>` (`claude.sessions`).
Absence is no evidence: a provider no parked run names narrows nothing.

**Not here.** No probe writes a `measured` entry (a hand-written one is read), no reservation is made (a grant is still a
judgement at one moment), nothing is written or locked, and `last_success`, `last_failure`, `quota_remaining_hint` and a
reliability score of the plan's data model are not built. The runs root is read as `capacity check` already reads it.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from .broker import STORAGE_BYTES, STORAGE_INODES, STORAGE_KIND, Amount, Decision, Policy, decide as broker_decide
from .capacity import Registry
from .capacitycheck import DISPATCHES, SESSIONS, CheckError, Holds, live_holds, policy as default_policy
from .health import HealthReport, HealthState, read_health

_TIME = "%Y-%m-%dT%H:%M:%SZ"

HEALTH_AGE = 5 * 60
"""How old a reading of the provider waits may be before it is UNKNOWN: five minutes, the age of a measured figure."""

HOLDS_AGE = 60
"""How old a reading of the leases and reservations may be before it is UNKNOWN: a minute (they change by the second)."""

MAX_GROUPS = 256
"""`(provider, account)` groups one snapshot lists; more is an incomplete reading, not a longer list."""

MAX_WHY = 220
"""Characters of a reason that a fact carries."""

FRESH, UNKNOWN = "FRESH", "UNKNOWN"
FULL, HALF = "FULL", "HALF"

HOLD_RULE: dict[HealthState, str] = {
    HealthState.LIMITED: FULL,
    HealthState.GIVEN_UP: FULL,
    HealthState.DEGRADED: HALF,
    HealthState.RESET_DUE: HALF,
}
"""How much of a provider's capacity each health state holds back: all of it, or half rounded up. A state added to
`health.py` without a row here fails the completeness test."""

UNNAMED_RULE = HALF
"""What a pending limit whose provider cannot be named holds back from every provider capacity (it may be any of them)."""

_PROVIDER = "provider"


@dataclass(frozen=True)
class Observation:
    """One thing read: `value` `unit` of `family`/`kind`/`name` (`account` for provider state) as `field`, from `source`,
    at `observed_at` (UTC, `...Z`), good for `bound` seconds. `why` is not empty when the read itself failed (the value
    is then `None`)."""

    family: str
    kind: str
    name: str
    account: str
    field: str
    value: int | str | None
    unit: str
    source: str
    observed_at: str
    bound: int
    why: str = ""


@dataclass(frozen=True)
class Fact:
    """An observation judged at a time: `age` seconds old (observed_at to `now`), and `status` FRESH or UNKNOWN. An
    UNKNOWN fact has no value."""

    family: str
    kind: str
    name: str
    account: str
    field: str
    value: int | str | None
    unit: str
    source: str
    observed_at: str
    age: int
    bound: int
    status: str
    why: str

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in (
            "family", "kind", "name", "account", "field", "value", "unit", "source", "observed_at", "age", "bound",
            "status", "why")}


@dataclass(frozen=True)
class Snapshot:
    """What was read at `taken_at`: the registry as the record said it (its digest is quoted by a decision), the
    observations, the holds (`None` when they could not be read, with `holds_error` saying why) and the policy the caps
    are judged by."""

    taken_at: str
    runs_root: str
    registry: Registry
    policy: Policy
    observations: tuple[Observation, ...]
    holds: Holds | None
    holds_error: CheckError | None


@dataclass(frozen=True)
class Narrowing:
    """One provider capacity that the snapshot held back from a decision: `held` of its `limit`, because of `by`."""

    kind: str
    name: str
    unit: str
    rule: str
    held: int
    by: str

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in ("kind", "name", "unit", "rule", "held", "by")}


def _moment(text: str) -> datetime:
    return datetime.strptime(text, _TIME).replace(tzinfo=timezone.utc)


def _why(error: Exception) -> str:
    return str(error)[:MAX_WHY]


def take(registry: Registry, *, runs_root, now: str, policy: Policy | None = None, health_age: int = HEALTH_AGE,
         holds_age: int = HOLDS_AGE, **environment) -> Snapshot:
    """The snapshot of `registry` and of what the runs root `runs_root` records, observed at `now`. Reads, never
    probes: the leases and reservations under the root (`capacitycheck.live_holds`; `environment` is its injectable
    facts) and the provider waits of its runs (`health.read_health`). Never raises for what is on disk: a read that
    fails is an observation with a `why` and no value. Reads no clock; writes nothing."""
    used = default_policy() if policy is None else policy
    moment = _moment(now)                             # a malformed `now` is the caller's error, not an UNKNOWN reading
    observations: list[Observation] = []
    for entry in registry.entries:
        field = "declared_cap" if entry.source == "declared" else "observed_cap"
        observations.append(Observation(
            "cap", entry.kind, entry.name, "", field, entry.limit, entry.unit, f"registry:{entry.source}",
            entry.as_of, used.seconds(entry.kind, entry.source)))
    try:
        report: HealthReport | None = read_health(runs_root, now=moment)
        health_why = ""
    except Exception as exc:                          # noqa: BLE001 - fail closed: the reading is UNKNOWN
        report, health_why = None, f"the provider waits cannot be read ({type(exc).__name__}): {_why(exc)}"
    source = "health.read_health"

    def reading(field: str, value, why: str = "") -> Observation:
        return Observation("provider", _PROVIDER, "", "", field, value, "items", source, now, health_age, why)

    if report is None:
        observations += [reading("problems", None, health_why), reading("unnamed", None, health_why)]
    else:
        listed = report.providers[:MAX_GROUPS]
        for group in listed:
            observations.append(Observation("provider", _PROVIDER, group.provider, group.account_handle, "health_state",
                                            group.state.value, "", source, now, health_age))
            observations.append(Observation("provider", _PROVIDER, group.provider, group.account_handle, "reset_at",
                                            group.not_before, "", source, now, health_age))
            observations.append(Observation("provider", _PROVIDER, group.provider, group.account_handle, "failures",
                                            ",".join(f"{name}={count}" for name, count in group.failures), "", source,
                                            now, health_age))
        if len(report.providers) > MAX_GROUPS:
            observations.append(reading("problems", None, f"more than {MAX_GROUPS} provider groups: not all listed"))
        else:
            observations.append(reading("problems", len(report.problems)))
        pending = sum(1 for wait in report.unattributed if not wait.due and not wait.given_up)
        observations.append(reading("unnamed", pending))
    holds: Holds | None
    try:
        holds, error = live_holds(runs_root, **environment), None
    except CheckError as exc:
        holds, error = None, exc
    holds_source = "capacitycheck.live_holds"
    if holds is None:
        bad = _why(error)
        observations.append(Observation("slots", SESSIONS[0], SESSIONS[1], "", "active", None, SESSIONS[2],
                                        holds_source, now, holds_age, bad))
        observations.append(Observation("slots", DISPATCHES[0], DISPATCHES[1], "", "reserved", None, DISPATCHES[2],
                                        holds_source, now, holds_age, bad))
    else:
        observations.append(Observation("slots", SESSIONS[0], SESSIONS[1], "", "active", holds.sessions, SESSIONS[2],
                                        holds_source, now, holds_age))
        observations.append(Observation("slots", DISPATCHES[0], DISPATCHES[1], "", "reserved", holds.dispatches,
                                        DISPATCHES[2], holds_source, now, holds_age))
        if holds.storage_bytes or holds.storage_inodes:           # P3-39: what the waves in flight hold of the storage
            for name, value, unit in ((STORAGE_BYTES, holds.storage_bytes, "bytes"),
                                      (STORAGE_INODES, holds.storage_inodes, "items")):
                observations.append(Observation("slots", STORAGE_KIND, name, "", "reserved", value, unit,
                                                holds_source, now, holds_age))
    return Snapshot(now, str(runs_root), registry, used, tuple(observations), holds, error)


def _judged(item: Observation, at: datetime) -> Fact:
    age = int((at - _moment(item.observed_at)).total_seconds())
    if item.why:
        status, why = UNKNOWN, item.why
    elif age < 0:
        status, why = UNKNOWN, "observed after now"
    elif age > item.bound:
        status, why = UNKNOWN, f"{age} s old; at most {item.bound} s"
    else:
        status, why = FRESH, ""
    return Fact(item.family, item.kind, item.name, item.account, item.field,
                item.value if status == FRESH else None, item.unit, item.source, item.observed_at, age, item.bound,
                status, why)


def _held(rule: str, limit: int) -> int:
    return limit if rule == FULL else limit - limit // 2


def _causes(judged: tuple[Fact, ...], name: str) -> list[tuple[str, str]]:
    """Why the provider capacity `name` is held back, as `(rule, by)` in the order of the facts: the provider reading as a
    whole (incomplete or UNKNOWN: all of it; a pending limit that names no provider: half), then each provider group
    whose name is `name` or a prefix `<provider>.` of it."""
    reading = {f.field: f for f in judged if f.family == "provider" and f.name == ""}
    found: list[tuple[str, str]] = []
    problems = reading.get("problems")
    if problems is None or problems.status == UNKNOWN or problems.value:
        found.append((FULL, "the provider reading is incomplete or UNKNOWN"))
    unnamed = reading.get("unnamed")
    if unnamed is None or unnamed.status == UNKNOWN:
        found.append((FULL, "the pending limits without a named provider are UNKNOWN"))
    elif unnamed.value:
        found.append((UNNAMED_RULE, f"{unnamed.value} pending limit(s) name no provider"))
    for group in judged:
        if group.field == "health_state" and (name == group.name or name.startswith(group.name + ".")):
            state = "UNKNOWN" if group.status == UNKNOWN else group.value
            rule = FULL if group.status == UNKNOWN else HOLD_RULE[HealthState(group.value)]
            found.append((rule, f"{group.name}/{group.account} is {state}"))
    return found


def _worst(causes: list[tuple[str, str]]) -> tuple[str, str]:
    return next((c for c in causes if c[0] == FULL), causes[0])


def facts(snapshot: Snapshot, *, now: str) -> tuple[Fact, ...]:
    """Every observation of `snapshot` judged at `now` (YYYY-MM-DDTHH:MM:SSZ), in the order they were taken: caps in the
    record's order (each `provider` cap followed by its `observed_safe_cap`: the limit less what the provider state holds
    back, the reason in `why`; UNKNOWN when the cap is), provider state in name order, then the two slot counts. FRESH or
    UNKNOWN, never a stale value."""
    at = _moment(now)
    judged = tuple(_judged(item, at) for item in snapshot.observations)
    out: list[Fact] = []
    for fact in judged:
        out.append(fact)
        if fact.family == "cap" and fact.kind == _PROVIDER:
            out.append(_safe(fact, judged, snapshot))
    return tuple(out)


def _safe(cap: Fact, judged: tuple[Fact, ...], snapshot: Snapshot) -> Fact:
    """The `observed_safe_cap` of a provider cap: what the sentinel's records leave of it."""
    reading = next(f for f in judged if f.family == "provider" and f.name == "" and f.field == "problems")
    common = ("cap", cap.kind, cap.name, "", "observed_safe_cap")
    if cap.status == UNKNOWN:
        return Fact(*common, None, cap.unit, "health.read_health", reading.observed_at, reading.age, reading.bound,
                    UNKNOWN, "the cap is UNKNOWN")
    causes = _causes(judged, cap.name)
    rule, by = _worst(causes) if causes else ("", "")
    held = _held(rule, cap.value) if causes else 0
    return Fact(*common, cap.value - held, cap.unit, "health.read_health", reading.observed_at, reading.age, reading.bound,
                FRESH, by)


def view(snapshot: Snapshot, *, now: str) -> dict:
    """What a person is shown: when the snapshot was taken and judged, the registry digest it was judged on, whether every
    fact is FRESH, and the facts."""
    judged = facts(snapshot, now=now)
    return {"taken_at": snapshot.taken_at, "judged_at": now, "runs_root": snapshot.runs_root,
            "registry": snapshot.registry.digest, "complete": all(f.status == FRESH for f in judged),
            "facts": [f.as_dict() for f in judged]}


def narrowing(snapshot: Snapshot, wants: Iterable[Amount], *, now: str) -> tuple[Narrowing, ...]:
    """The provider capacities among `wants` that the provider state holds back at `now`, each with the largest rule
    that applies and what it is from. Only a wanted `provider` capacity the registry knows is looked at."""
    judged = facts(snapshot, now=now)
    out = []
    for want in sorted(wants, key=lambda w: (w.kind, w.name)):
        entry = next((e for e in snapshot.registry.entries if (e.kind, e.name) == (want.kind, want.name)), None)
        if want.kind != _PROVIDER or entry is None:
            continue
        causes = _causes(judged, want.name)
        if causes:
            rule, by = _worst(causes)
            out.append(Narrowing(want.kind, want.name, entry.unit, rule, _held(rule, entry.limit), by))
    return tuple(out)


def decide(snapshot: Snapshot, wants: Iterable[Amount], *, now: str) -> tuple[Decision, tuple[Narrowing, ...]]:
    """`(decision, narrowed)`: `broker.decide` at `now` on the snapshot's registry and policy, beside the snapshot's
    holds and what `narrowing` holds back. `CheckError` (`holds`) when the slots are UNKNOWN at `now` -- no decision is
    taken on a hold that cannot be known. `BrokerError` where `decide` raises. Reads no clock; writes nothing."""
    wanted = list(wants)
    if snapshot.holds is None:
        raise snapshot.holds_error
    stale = next((f for f in facts(snapshot, now=now) if f.family == "slots" and f.status == UNKNOWN), None)
    if stale is not None:
        raise CheckError("holds", f"the holds are UNKNOWN: {stale.why}")
    narrowed = narrowing(snapshot, wanted, now=now)
    extra = [Amount(item.kind, item.name, item.held, item.unit) for item in narrowed if item.held]
    decision = broker_decide(snapshot.registry, snapshot.policy, now, wanted, (*snapshot.holds.amounts, *extra))
    return decision, narrowed
