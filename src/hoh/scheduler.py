"""The resource scheduler: one pure admission of a list of ready nodes (plan row P3-19, slice 1).

Two questions stand between a ready node and a started run, and until now neither was asked *over a list of
nodes*. The capacity broker (`hoh.broker`, P3-17) judges **one request against a snapshot**: two nodes that each
want the one free session are each granted it. The budget ledger (`hoh.reservation`, P3-04) answers "what would a
start be granted now?" for one run id at a time, and its `left` is an advisory read. This module combines the two
and walks the list: `admit` takes the registry, the freshness policy, the time, the candidates in the caller's
order, what is already held and the budget, and returns a `Plan` that says, per candidate, `admit`, `wait` or
`refuse` and why.

**What this slice is.** A function of its arguments. It reads no file and no clock, writes nothing, runs nothing
and remembers nothing; the only project modules it imports are `hoh.broker` and `hoh.capacity`. **What it is
not.** It reserves nothing -- a plan is a judgement over the snapshot it was given, not a claim, and the
launcher's `reserve` stays the authority. It does not choose the order of the candidates (the caller's order is
the priority), does not turn a node into capacity lines, does not look for holds and `admit` is called by nothing
yet.

**Within one call, over one snapshot.** The wants of every candidate admitted earlier count as holds for the
later ones, so per capacity the caller's holds plus the admitted wants never pass the limit of a fresh, known
entry; the shares of the admitted candidates sum to at most `max(budget_left, 0)`. A share above
`budget_ceiling` (what a start could ever be granted once everything held were given back) is refused, never
waited for. A candidate that must wait holds its place: nothing after it is admitted in the call (`behind`). A
candidate that can never run is refused and holds no one up.

**Fail closed.** Nothing has a default: not the holds, not the budget, not the policy, not the time. A request
that is malformed (a wrong type or value, an object made without its constructor) raises `SchedulerError`; there
is no plan then. A refusal of the broker is a `SchedulerError` with the broker's reason.

**Profiles (plan row P3-25, slice 1).** `profile_width` is the first *mode*: how many of the ready candidates, in
the caller's order, a parallelism profile offers to `admit` -- `candidates[:width]` -- and no more. `admit` is not
changed and stays the safe core: a profile only narrows what is offered, never what the registry or the budget
would grant, and a width counts the candidates *offered*, not those admitted (a waiting head still uses its lane
of the offer). The ladder is fixed, narrowest first: `LINEAR` is one lane; `CONSERVATIVE`, `MODERATE`, `HIGH` and
`MAX_SAFE` take at most 40, 70, 90 and 100 percent (rounded down) of the authorized `slots`, the upper end of the
plan's target-utilization ranges. All of them are cut by `ceiling` (the most active work packages, so that
`MAX_SAFE` is never unlimited) and by what `reserve` leaves of the slots, and none by the work that is not there:
`width` never passes `ready`, and unused slots are no permission to invent work. The reserve is **withheld from
this call's candidates**, not served first: the QA, integration and recovery candidates are offered by a second
call whose `slots` is the reserve, so the two pools never add up to more than `slots`. While a slot exists a
profile keeps at least one lane -- a deliberate exception: when `slots` does not exceed `reserve` that lane is a
reserved slot, so a small source still runs one node; with no slot there is no lane. A node that is already
`active` takes a lane; a profile that narrows later starts nothing and ends nothing. Dissolving the reserve when
no QA, integration or recovery work is waiting is the caller's act (it passes `reserve=0`); `AUTO_ADAPTIVE`
(P3-26) is not in the ladder. Nothing calls `profile_width` yet: the adapter and the controller that do are later
slices.

**Adaptation (plan row P3-26, slice 1).** `AUTO_ADAPTIVE` is not a sixth profile but a rule that *moves along the
ladder*: `adapt_profile(state, signal, bounds)` takes the profile in force and the count of consecutive stable
readings (the caller's state: nothing is remembered here), one reading, and the operator's bounds, and returns the
profile for the next wave. The reading is the `reason` of a `hoh.backpressure` decision, as its string. **Narrowing is
immediate, widening is slow**: a reading that halves the wave (`DEGRADED`, `RESET_DUE`) is one step down; a reading
that sends it to the floor (`LIMITED`, `GIVEN_UP`, and `INCOMPLETE`, `STALE`, which say the reading cannot be
trusted) is `LINEAR`; `NONE` is one stable reading, and only `up_after` stable readings in a row are one step up,
after which the count starts again -- so between two steps up there are always `up_after` readings and no flapping
signal climbs. A pressure reading is applied first and the result is then brought under `bounds.top` (the
operator's upper bound), so a floor reading is `LINEAR` from any level; a level above a lowered top is brought to
it by a stable reading (`above_top`). It never leaves the ladder, never goes up on anything but `NONE`, and a
reading it cannot read (not one of the seven strings, whatever its type) is one step down with its own reason,
never an exception and never a step up: the same fail-closed direction as `hoh.backpressure`. A wrong *state* or
*bounds* (a `stable` count above `up_after`) is a `SchedulerError`. A signal is judged by its real type, never by
a `__class__` it claims.
`adapt_profile` only chooses the profile. **The rung is not P3-07's width**: a halving reading is one rung here and
half a wave there, so the wave a caller launches is `wave_width(profile_width(level, ...), decision.width)` --
never more than the decision just obtained -- with `planned_for_decision` giving `decide` the profile in force's
width (1 to 16). The width is still `profile_width`'s (so `ceiling`, `slots` and `reserve` still bound it) and the
admission is still `admit`'s. Nothing calls it yet.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
from itertools import islice

from . import broker
from .broker import Amount, BrokerError, Decision, Policy, decide
from .capacity import MAX_ENTRIES, Registry

VERDICTS = ("admit", "wait", "refuse")
"""What a placement says: start it now, ask again later, or it can never run on what is recorded."""

REASONS = ("ok", "exhausted", "exceeds", "stale", "future", "unknown", "unit", "budget_exhausted",
           "budget_exceeds", "behind")
"""Why. `ok` goes with `admit`; `exhausted`, `budget_exhausted` and `behind` with `wait`; the rest with
`refuse`. The first seven are the broker's, passed through."""

MAX_CANDIDATES = 64
"""Candidates one call may judge."""

MAX_HOLDS = broker.MAX_HOLDS - MAX_ENTRIES
"""Holds the caller may list: the broker's bound less the one summed claim per capacity (at most one per registry
entry) that the scheduler adds for the candidates it admitted, so the broker's own bound is never broken."""

MAX_BUDGET = 10 ** 9
"""The largest budget figure and the largest share."""

PROFILES = ("LINEAR", "CONSERVATIVE", "MODERATE", "HIGH", "MAX_SAFE")
"""The fixed parallelism ladder, narrowest first. `AUTO_ADAPTIVE` chooses among these at run time (P3-26)."""

PERCENT = {"CONSERVATIVE": 40, "MODERATE": 70, "HIGH": 90, "MAX_SAFE": 100}
"""The share of the authorized slots a profile may fill, in whole percent, rounded down. `LINEAR` is one lane."""

MAX_LANES = MAX_CANDIDATES
"""The largest slot count, reserve, ceiling or active count, and the largest `ready`: one call of `admit` judges
no more candidates, so `candidates[:width]` always fits it."""

_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_KEPT_NODES = ("backups", "projects")        # the names the runs root keeps (`store.RESERVED_RUN_IDS`)
_NAME_CUT = 40


class SchedulerError(ValueError):
    """A request the scheduler will not judge. `reason` is `invalid` (a value, a count or a type breaks a rule) or
    `registry` (the broker found the registry is not what `capacity.parse` produces). A message never carries an
    unbounded value: class names are cut."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"resource scheduler: {message}")
        self.reason = reason


def _kind(value: object) -> str:
    """A class name for a message, bounded."""
    return type(value).__name__[:_NAME_CUT]


def _invalid(message: str) -> SchedulerError:
    return SchedulerError("invalid", message)


def _amount(item: object, what: str) -> Amount:
    """The `Amount` again from its four fields: exactly the class, and valid now (a frozen value may have been
    edited in place, and an object made without its constructor has no fields at all)."""
    if type(item) is not Amount:
        raise _invalid(f"{what} may contain only Amount objects, not a {_kind(item)}")
    try:
        return Amount(item.kind, item.name, item.amount, item.unit)
    except (BrokerError, AttributeError, TypeError, ValueError) as exc:
        raise _invalid(f"{what} holds an Amount that is not valid") from exc


def _whole(value: object, low: int, high: int, what: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise _invalid(f"{what} is a whole number from {low} to {high}")
    return value


@dataclass(frozen=True)
class Candidate:
    """One ready node asking to start: `node` (its id), `wants` (a tuple of 1 to 64 `Amount`, at most one per
    capacity -- what it needs of the registry) and `budget` (the share of the dispatch budget the launcher would
    pass as `dispatch_reservation`, 1 to 10**9). Not a run id: the adapter maps one to the other."""

    node: str
    wants: tuple
    budget: int

    def __post_init__(self) -> None:
        if (type(self.node) is not str or _NODE.fullmatch(self.node) is None or self.node in _KEPT_NODES):
            raise _invalid("a node id is [A-Za-z0-9][A-Za-z0-9._-]{0,127} and not backups or projects")
        if type(self.wants) is not tuple or not 1 <= len(self.wants) <= 64:
            raise _invalid("wants is a tuple of 1 to 64 Amount")
        keys = [(want.kind, want.name) for want in (_amount(item, "wants") for item in self.wants)]
        if len(set(keys)) != len(keys):
            raise _invalid("a capacity is wanted twice; ask for the sum in one line")
        _whole(self.budget, 1, MAX_BUDGET, "budget")


@dataclass(frozen=True)
class Placement:
    """What the plan says about one candidate: `verdict` and `reason`, the `budget` share it asked for, and the
    broker's own `decision` for its wants (beside the holds and the claims of the candidates admitted before)."""

    node: str
    verdict: str
    reason: str
    budget: int
    decision: Decision

    def as_dict(self) -> dict:
        return {"node": self.node, "verdict": self.verdict, "reason": self.reason, "budget": self.budget,
                "decision": self.decision.as_dict()}


@dataclass(frozen=True)
class Plan:
    """The answer: what was judged (`registry` digest, `now`, the budget `budget_ceiling`, `budget_left` and
    what is `budget_after` the admitted shares) and one `Placement` per candidate **in the order given**."""

    registry: str
    now: str
    budget_ceiling: int
    budget_left: int
    budget_after: int
    placements: tuple[Placement, ...]

    @property
    def admitted(self) -> tuple[str, ...]:
        """The node ids that may start, in the order given."""
        return tuple(p.node for p in self.placements if p.verdict == "admit")

    def as_dict(self) -> dict:
        return {"registry": self.registry, "now": self.now, "budget_ceiling": self.budget_ceiling,
                "budget_left": self.budget_left, "budget_after": self.budget_after,
                "admitted": list(self.admitted), "placements": [p.as_dict() for p in self.placements]}


def _taken(items: object, limit: int, what: str, needed: bool) -> list:
    """At most `limit` items of `items`, or a refusal. One more than `limit` is pulled to see that there are
    too many; what the caller's own iterable raises while it is made or consumed is not caught here -- that
    includes the `TypeError` CPython raises when an `__iter__` returns something that is not an iterator (the
    object's own error, not "not an iterable").

    Taken: a list, a tuple, an iterator, a generator, `dict.values()`. Refused: a string, bytes, a mapping and
    **anything that is a `collections.abc.Set`** -- a set, a frozenset and the dict views `keys()` and `items()`,
    insertion-ordered ones included: the rule is the type, not the order a given object happens to have (a set
    would make the plan depend on the hash seed, and a set of holds would count two equal holds once)."""
    if isinstance(items, (str, bytes, bytearray, Mapping, Set)):
        raise _invalid(f"{what} is an ordered iterable of {'Candidate' if needed else 'Amount'}, "
                       f"not a {_kind(items)}")
    try:
        iterator = iter(items)
    except TypeError as exc:
        if getattr(type(items), "__iter__", None) is not None:      # the iterable's own error, not "not iterable"
            raise
        raise _invalid(f"{what} is an iterable, not a {_kind(items)}") from exc
    got = list(islice(iterator, limit + 1))
    if len(got) > limit:
        raise _invalid(f"{what}: more than {limit}")
    if needed and not got:
        raise _invalid(f"{what}: at least one is needed")
    return got


def _candidate(item: object) -> Candidate:
    if type(item) is not Candidate:
        raise _invalid(f"candidates may contain only Candidate objects, not a {_kind(item)}")
    try:                                         # the constructor judges it again, edited or forged
        return Candidate(item.node, item.wants, item.budget)
    except (AttributeError, TypeError) as exc:
        raise _invalid("a candidate is not valid") from exc


def admit(registry: Registry, policy: Policy, now: str, candidates: Iterable[Candidate],
          holds: Iterable[Amount], budget_ceiling: int, budget_left: int) -> Plan:
    """Which of `candidates` may start now? Every argument is required.

    `candidates` is 1 to 64 `Candidate` in the caller's order, which is the priority (an ordered iterable: a list, a
    tuple, an iterator or a generator; a string, bytes, a mapping, a set, a frozenset or a dict view is refused).
    `holds` is what is already
    taken (0 to 768 `Amount`, one per holder), never defaulted: an empty tuple says "nothing is held".
    `budget_ceiling` is the most a start could still be granted once everything held were given back (the
    shared budget less what is spent), `budget_left` what a start of a node that holds nothing yet would be
    granted now (`BudgetReservations.left`). Pure; returns a `Plan` or raises `SchedulerError`."""
    if type(registry) is not Registry:           # before any attribute is read: a property may raise anything
        raise SchedulerError("registry", f"a {_kind(registry)} is not a capacity.Registry")
    if type(policy) is not Policy:
        raise _invalid(f"a {_kind(policy)} is not a broker.Policy")
    try:                                         # an object made without its constructor has no fields
        _ = (registry.entries, registry.digest)
    except AttributeError as exc:
        raise SchedulerError("registry", f"a {_kind(registry)} is not a capacity.Registry") from exc
    try:
        _ = policy.rows
    except AttributeError as exc:
        raise _invalid(f"a {_kind(policy)} is not a broker.Policy") from exc
    ceiling = _whole(budget_ceiling, 0, MAX_BUDGET, "budget_ceiling")
    left = _whole(budget_left, -MAX_BUDGET, ceiling, "budget_left")
    asked = [_candidate(item) for item in _taken(candidates, MAX_CANDIDATES, "candidates", True)]
    nodes = [candidate.node for candidate in asked]
    if len(set(nodes)) != len(nodes):
        raise _invalid("a node is offered twice")
    held = tuple(_amount(item, "holds") for item in _taken(holds, MAX_HOLDS, "holds", False))

    claimed: dict[tuple[str, str], tuple[int, str]] = {}      # per capacity: the sum admitted so far, its unit
    remaining = left
    waiting = False
    placements = []
    for candidate in asked:
        claims = tuple(Amount(kind, name, total, unit) for (kind, name), (total, unit) in claimed.items())
        try:
            decision = decide(registry, policy, now, candidate.wants, held + claims)
        except BrokerError as exc:
            raise SchedulerError(exc.reason, f"the broker refuses the request ({str(exc)[:120]})") from exc
        if decision.verdict == "refuse":
            verdict, reason = "refuse", decision.reason
        elif candidate.budget > ceiling:
            verdict, reason = "refuse", "budget_exceeds"
        elif decision.verdict == "wait":
            verdict, reason = "wait", "exhausted"
        elif candidate.budget > remaining:
            verdict, reason = "wait", "budget_exhausted"
        elif waiting:
            verdict, reason = "wait", "behind"
        else:
            verdict, reason = "admit", "ok"
        if verdict == "wait":
            waiting = True
        elif verdict == "admit":
            remaining -= candidate.budget
            for want in candidate.wants:
                key = (want.kind, want.name)
                total, _ = claimed.get(key, (0, want.unit))
                claimed[key] = (total + want.amount, want.unit)
        placements.append(Placement(candidate.node, verdict, reason, candidate.budget, decision))
    return Plan(registry=registry.digest, now=now, budget_ceiling=ceiling, budget_left=left,
                budget_after=remaining, placements=tuple(placements))


@dataclass(frozen=True)
class Lanes:
    """What bounds the active nodes: `slots` (0 to 64, the authorized safe concurrent slots -- the lesser of what
    is authorized and what the machine and the provider carry), `reserve` (0 to 64, slots held back for QA,
    integration and recovery), `ceiling` (1 to 64, the most active work packages) and `active` (0 to 64, the nodes
    that are running now)."""

    slots: int
    reserve: int
    ceiling: int
    active: int

    def __post_init__(self) -> None:
        _whole(self.slots, 0, MAX_LANES, "slots")
        _whole(self.reserve, 0, MAX_LANES, "reserve")
        _whole(self.ceiling, 1, MAX_LANES, "ceiling")
        _whole(self.active, 0, MAX_LANES, "active")


@dataclass(frozen=True)
class Width:
    """What a profile says: `cap` (the active nodes it allows in all), `active` and `ready` as judged, and
    `width` (how many of the first `ready` candidates to offer to `admit`: `cap` less `active`, at most `ready`)."""

    profile: str
    cap: int
    active: int
    ready: int
    width: int

    def as_dict(self) -> dict:
        return {"profile": self.profile, "cap": self.cap, "active": self.active, "ready": self.ready,
                "width": self.width}


def profile_width(profile: str, lanes: Lanes, ready: int) -> Width:
    """How many of `ready` candidates may be offered to `admit` under `profile`? Every argument is required.

    `profile` is exactly one of `PROFILES`; `lanes` is a `Lanes`; `ready` is the number of candidates (0 to 64).
    Pure; returns a `Width` or raises `SchedulerError`."""
    if type(profile) is not str or profile not in PROFILES:
        raise _invalid("a profile is exactly one of " + ", ".join(PROFILES))
    if type(lanes) is not Lanes:
        raise _invalid(f"lanes is a Lanes, not a {_kind(lanes)}")
    try:                                         # the constructor judges it again, edited or forged
        given = Lanes(lanes.slots, lanes.reserve, lanes.ceiling, lanes.active)
    except (AttributeError, TypeError) as exc:
        raise _invalid("lanes is not valid") from exc
    count = _whole(ready, 0, MAX_LANES, "ready")
    if given.slots == 0:
        cap = 0
    else:
        share = 1 if profile == "LINEAR" else given.slots * PERCENT[profile] // 100
        cap = max(1, min(share, given.ceiling, given.slots - given.reserve))
    free = max(cap - given.active, 0)
    return Width(profile, cap, given.active, count, min(free, count))


ADAPT_SIGNALS = {"NONE": "stable", "DEGRADED": "down", "RESET_DUE": "down", "LIMITED": "floor",
                 "GIVEN_UP": "floor", "INCOMPLETE": "floor", "STALE": "floor"}
"""The seven reasons of a `hoh.backpressure` decision and what each says to the adaptation: `stable` (keeps the
width), `down` (halves it: one step down) or `floor` (the sequential floor: `LINEAR`). A test holds this table to
`backpressure.Reason` and `backpressure.WIDTH_RULE`, so a reason added there without a row here fails."""

ADAPT_REASONS = ("stable", "up", "top", "down", "floor", "unreadable", "above_top")
"""Why a reading gave what it gave: `stable` (counted, below `up_after`), `up` (one step), `top` (stable and
already at the bound), `down`, `floor`, `unreadable` (one step down) and `above_top` (the profile in force is above
the bound: it is brought to the bound whatever the reading)."""

ADAPT_UP_AFTER = 3
"""The recommended `up_after`: three stable readings in a row for one step up. Never a default of a call."""

ADAPT_START = "MODERATE"
"""Where an adaptive project starts, lowered to `bounds.top` when that is narrower."""

MAX_UP_AFTER = 16
"""The largest `up_after`."""


@dataclass(frozen=True)
class Adaptive:
    """The adaptation's state, kept by the caller: `level` (the profile in force, one of `PROFILES`) and `stable`
    (consecutive stable readings counted since the last step, 0 to 64)."""

    level: str
    stable: int

    def __post_init__(self) -> None:
        if type(self.level) is not str or self.level not in PROFILES:
            raise _invalid("a level is exactly one of " + ", ".join(PROFILES))
        _whole(self.stable, 0, MAX_LANES, "stable")


@dataclass(frozen=True)
class Bounds:
    """The operator's bounds: `top` (the widest profile the adaptation may reach, one of `PROFILES`) and `up_after`
    (1 to 16, the stable readings in a row that make one step up)."""

    top: str
    up_after: int

    def __post_init__(self) -> None:
        if type(self.top) is not str or self.top not in PROFILES:
            raise _invalid("a top is exactly one of " + ", ".join(PROFILES))
        _whole(self.up_after, 1, MAX_UP_AFTER, "up_after")


@dataclass(frozen=True)
class Adaptation:
    """What one reading did: the new `level` and `stable`, `reason` (one of `ADAPT_REASONS`) and whether the
    profile `changed`."""

    level: str
    stable: int
    reason: str
    changed: bool

    def as_dict(self) -> dict:
        return {"level": self.level, "stable": self.stable, "reason": self.reason, "changed": self.changed}

    @property
    def state(self) -> Adaptive:
        """The state to keep for the next reading."""
        return Adaptive(self.level, self.stable)


def _bounds(bounds: object) -> Bounds:
    if type(bounds) is not Bounds:
        raise _invalid(f"bounds is a Bounds, not a {_kind(bounds)}")
    try:                                         # judged again, edited or forged
        return Bounds(bounds.top, bounds.up_after)
    except (AttributeError, TypeError) as exc:
        raise _invalid("bounds is not valid") from exc


def adaptive_start(bounds: Bounds) -> Adaptive:
    """The state of a project that starts adaptive: `ADAPT_START`, or `bounds.top` when that is narrower."""
    given = _bounds(bounds)
    level = ADAPT_START if PROFILES.index(given.top) >= PROFILES.index(ADAPT_START) else given.top
    return Adaptive(level, 0)


def adapt_profile(state: Adaptive, signal: object, bounds: Bounds) -> Adaptation:
    """The profile for the next wave given the one in force, one reading and the bounds. Every argument is required.

    `state` is an `Adaptive`, `bounds` a `Bounds`; `signal` is the `reason` of a backpressure decision (one of the
    seven strings of `ADAPT_SIGNALS`; a `str` subclass is read by its text). A `signal` that is anything else is not an
    error: it is one step down with the reason `unreadable`. A pressure reading is applied first and the result is
    then brought under `bounds.top`, so a floor reading is `LINEAR` from any level above or below the top. Pure;
    returns an `Adaptation` or raises `SchedulerError` for a wrong `state` or `bounds` (a `stable` count above
    `up_after` is a wrong state)."""
    if type(state) is not Adaptive:
        raise _invalid(f"state is an Adaptive, not a {_kind(state)}")
    try:
        held = Adaptive(state.level, state.stable)
    except (AttributeError, TypeError) as exc:
        raise _invalid("state is not valid") from exc
    given = _bounds(bounds)
    rank, top = PROFILES.index(held.level), PROFILES.index(given.top)
    if held.stable > given.up_after:
        raise _invalid("stable is a count of readings since the last step: at most up_after")
    text = str.__str__(signal) if issubclass(type(signal), str) else None   # the real type, not a spoofed __class__
    kind = ADAPT_SIGNALS.get(text) if text is not None else None
    if kind is None or kind != "stable":
        if kind is None:
            lower, reason = max(rank - 1, 0), "unreadable"
        elif kind == "floor":
            lower, reason = 0, "floor"
        else:
            lower, reason = max(rank - 1, 0), "down"
        lower = min(lower, top)                  # a pressure reading, applied first; then under the bound
        return Adaptation(PROFILES[lower], 0, reason, lower != rank)
    if rank > top:
        return Adaptation(given.top, 0, "above_top", True)
    stable = held.stable + 1
    if stable < given.up_after:
        return Adaptation(held.level, stable, "stable", False)
    if rank < top:
        return Adaptation(PROFILES[rank + 1], 0, "up", True)
    return Adaptation(held.level, given.up_after, "top", False)


MAX_WAVE = 16
"""The widest wave a backpressure decision plans (`hoh.backpressure` and `hoh.wave`: 16); a test holds it to them."""


def _width(item: object) -> Width:
    """The `Width` again from its fields: exactly the class, and valid now."""
    if type(item) is not Width:
        raise _invalid(f"a width is a Width, not a {_kind(item)}")
    try:
        if type(item.profile) is not str or item.profile not in PROFILES:
            raise _invalid("a width has a profile of the ladder")
        for what in ("cap", "active", "ready", "width"):
            _whole(getattr(item, what), 0, MAX_LANES, what)
        if item.profile == "LINEAR" and item.cap > 1:
            raise _invalid("a LINEAR width has at most one lane")
        if item.width != min(max(item.cap - item.active, 0), item.ready):
            raise _invalid("a width is the free lanes of its cap, at most ready")
    except (AttributeError, TypeError) as exc:
        raise _invalid("a width is not valid") from exc
    return item


def planned_for_decision(width: Width) -> int:
    """The planned width to hand `hoh.backpressure.decide` for the profile in force: its width, from 1 to `MAX_WAVE`
    (`decide` takes 1 to 16; a profile may have 0 free lanes or up to 64). The reason of the decision does not
    depend on it; the decision's width does, and `wave_width` is the bound."""
    return min(max(_width(width).width, 1), MAX_WAVE)


def wave_width(width: Width, decision_width: int) -> int:
    """How many nodes the wave may have: the width of the adapted profile, **never more than the decision just
    obtained** (`decision.width`, 1 to 16), and 0 when the profile has no free lane. This is the bound a caller
    applies after `adapt_profile`: a halving or floor reading narrows the wave by P3-07's rule, whatever rung the
    adaptation chose."""
    given = _width(width)
    cap = _whole(decision_width, 1, MAX_WAVE, "decision_width")
    return 0 if given.width == 0 else min(given.width, cap)
