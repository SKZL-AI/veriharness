"""BackpressureController: how wide a wave may be while a provider is under pressure
(V3.3 P3-07, slices 1 and 2).

`ParallelWaveExecutor` (`hoh.wave`) runs as many nodes side by side as the
`width` it is given and "does not decide the width". `read_health`
(`hoh.health`) says what the parked runs of a root say about their providers,
and says that nothing consults it yet. This module is the step between them: one
**decision** that takes the width the planner wants and the reading, and gives
back a width that is never larger and, with the reason, is written down before
anything may use it.

The decision, and why each line of it is what it is:

* **Never wider.** The result is from 1 to the planned width; it equals the
  planned width only for `Reason.NONE`. The module has no way to raise a width,
  to start a node or to resume a run: a wider wave than the planner asked for
  would be a decision this module has no evidence for.
* **Evidence narrows, absence does nothing.** `health` has no HEALTHY state
  (absence is "no evidence"), so `NONE` means "no parked run says anything and
  the reading is whole and fresh", never "the provider is fine".
* **A closed table.** Each `HealthState` has one reason and each reason one
  rule (`STATE_REASON`, `WIDTH_RULE`): `LIMITED` and `GIVEN_UP` go to the
  sequential floor of 1 -- a limit that is pending refuses every dispatch until
  its reset, and a park the resume loop gave up on has been refused
  `quota.MAX_RESUME_ATTEMPTS` times; `DEGRADED` and `RESET_DUE` halve (rounded
  up), because the provider failed for now or has not answered yet. The floor is
  1, not 0: width 1 is exactly the sequential controller of today, so a
  narrowed wave is never worse than no wave. Whether to launch at all is the
  caller's decision, with this reason.
* **Fail closed.** What cannot be trusted narrows to the floor, with its own
  reason: a reading with problems is `INCOMPLETE` (the evidence it lacks may be
  about any provider, `health.HealthReport.complete`), a reading older than
  `MAX_REPORT_AGE_SECONDS` or from the future or with no readable moment is
  `STALE`, and a provider name, state or wait that is not what `health` builds
  is `INCOMPLETE`. An argument that is not a width, a reading or a moment is a
  `BackpressureError`, not a decision.
* **Unattributed waits count.** A park whose provider cannot be named may be the
  provider the wave would use; it narrows by what it says, and names no provider.
* **Written before it is returned.** `BackpressureController.admit_width` creates
  `backpressure-<id>.json` (create-only, 0600) and only then returns; a decision
  that could not be written is `BackpressureError`, never a width somebody could
  use without its reason on disk.

**Slice 2: the wave's admission.** `admit_wave` is the production caller: the project
loop (`orchestrator.ProjectController._wave_round`) hands it the nodes a wave would
prepare and launch, after the capacity gate (`capacitycheck.admit_wave`) has had its
say, and it answers which of them may (a prefix, in the order offered) and why each of
the others waits. It reads the health of the runs root of the launcher
(`health.read_health`), scopes the decision to the providers the launcher's runs
execute their roles on (`providers_of`: the harness profile of each role, replaced by
the provider of the routing policy where the policy names one), decides, writes the
record and only then answers. **It cannot stop a project**: the width it gives is at
least 1 and the first node offered is always admitted, so a round it narrows is
exactly the sequential controller's round -- never an empty one -- and the nodes it
cuts stay READY, unprepared, and are offered again next round. It never raises: what
it cannot judge (a root that is not a directory, a health that cannot be read, a
record that cannot be written) is the floor with `judged=False` and its reason.
`capacitycheck` and this module do not share a reading: capacity is the operator's
declared limits against the holds (opt-in by a registry); this is the providers'
own recent refusals (evidence, on whenever width is above 1). Both only narrow, in
the same order, so the wave that starts is the shorter prefix.

What this module does not do, and later slices' business: it does not know the
account a node would use (a provider is in scope with every account), does not follow
a failover to a declared fallback route (`routing.resolve_failover` has no caller),
does not widen again by itself, does not write into a `WaveRecord`, and does not
consult the parallelism profiles (`scheduler.profile_width`: the planned width is the
controller's `width`).
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from . import durable_io
from .contracts import Role, utcnow
from .health import Evidence, HealthReport, HealthState, ProviderHealth, Wait, read_health
from .routing import load as load_routing_policy
from .wave import MAX_WAVE_WIDTH

SCHEMA = "hoh-backpressure-decision/2"

#: A reading older than this many whole seconds (or read after `now`) is not the
#: state of the providers now. The caller reads the health and decides in one
#: step; a cached reading is the thing this refuses.
MAX_REPORT_AGE_SECONDS = 60
#: A decision file is a dozen short fields; a file above this is not ours.
MAX_RECORD_BYTES = 4096
#: Counts of groups, unattributed waits and problems in a record.
MAX_COUNT = 2**31
#: Providers a decision may be scoped to (the three roles of a run name at most three).
MAX_SCOPE = 16

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_NAME = re.compile(r"[\x20-\x7e]{1,128}")
_NAME_OR_EMPTY = re.compile(r"[\x20-\x7e]{0,128}")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_KEYS = ("schema", "decision_id", "decided_at", "report_at", "planned_width", "width", "reason",
         "provider", "account_handle", "groups", "unattributed", "problems", "scope", "ignored")


class BackpressureError(RuntimeError):
    """An argument, a record directory or a record file that is not acceptable.
    Never a width."""


class Reason(StrEnum):
    #: Nothing parked says anything and the reading is whole and fresh.
    NONE = "NONE"
    LIMITED = "LIMITED"
    DEGRADED = "DEGRADED"
    RESET_DUE = "RESET_DUE"
    GIVEN_UP = "GIVEN_UP"
    #: The reading has problems, or something in it is not what `health` builds.
    INCOMPLETE = "INCOMPLETE"
    #: The reading is too old, from the future, or has no readable moment.
    STALE = "STALE"


KEEP, HALF, FLOOR = "KEEP", "HALF", "FLOOR"

#: Every `HealthState`, decided here and nowhere else. A state added to
#: `health.py` without a row fails the completeness test.
STATE_REASON: dict[HealthState, Reason] = {
    HealthState.LIMITED: Reason.LIMITED,
    HealthState.DEGRADED: Reason.DEGRADED,
    HealthState.RESET_DUE: Reason.RESET_DUE,
    HealthState.GIVEN_UP: Reason.GIVEN_UP,
}

#: What each reason does to the planned width.
WIDTH_RULE: dict[Reason, str] = {
    Reason.NONE: KEEP,
    Reason.LIMITED: FLOOR,
    Reason.GIVEN_UP: FLOOR,
    Reason.INCOMPLETE: FLOOR,
    Reason.STALE: FLOOR,
    Reason.DEGRADED: HALF,
    Reason.RESET_DUE: HALF,
}

#: The reasons a group or a wait can carry, **in the order they are named** when
#: several hold (most restrictive first; LIMITED and GIVEN_UP are both the floor, so
#: for them the order only decides what the operator reads first). STALE and
#: INCOMPLETE are decided before any group is compared.
_EVIDENCE_REASONS = (Reason.LIMITED, Reason.GIVEN_UP, Reason.DEGRADED, Reason.RESET_DUE)


def narrowed(reason: Reason, planned: int) -> int:
    """The width the rule of `reason` gives for `planned` (1 to 16)."""
    rule = WIDTH_RULE[reason]
    if rule == KEEP:
        return planned
    if rule == HALF:
        return (planned + 1) // 2
    return 1


@dataclass(frozen=True)
class WidthDecision:
    planned: int
    width: int
    reason: Reason
    #: The group that decided; empty for NONE, INCOMPLETE and STALE and for an
    #: unattributed wait (which names no provider).
    provider: str = ""
    account_handle: str = ""
    groups: int = 0
    unattributed: int = 0
    problems: int = 0
    #: The reading's moment, whole seconds UTC (`...Z`); None when it has none.
    report_at: str | None = None
    #: The providers the decision was scoped to, sorted; None: every provider counted.
    #: `groups` counts the groups inside the scope, `ignored` those outside it.
    scope: tuple[str, ...] | None = None
    ignored: int = 0

    @property
    def narrows(self) -> bool:
        return self.width < self.planned

    def as_dict(self) -> dict:
        return {"planned_width": self.planned, "width": self.width,
                "reason": self.reason.value, "provider": self.provider,
                "account_handle": self.account_handle, "groups": self.groups,
                "unattributed": self.unattributed, "problems": self.problems,
                "report_at": self.report_at,
                "scope": None if self.scope is None else list(self.scope), "ignored": self.ignored}


@dataclass(frozen=True)
class BackpressureRecord:
    """One decision as it was written: the decision and when it was taken."""

    decision_id: str
    decided_at: str
    decision: WidthDecision

    def to_bytes(self) -> bytes:
        d = self.decision
        body = {"schema": SCHEMA, "decision_id": self.decision_id, "decided_at": self.decided_at,
                **d.as_dict()}
        return (json.dumps(body, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode("ascii")


# --------------------------------------------------------------------------- #
# The decision: pure
# --------------------------------------------------------------------------- #

def _planned(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_WAVE_WIDTH:
        raise BackpressureError(f"the planned width is an integer from 1 to {MAX_WAVE_WIDTH}, "
                                f"not {value!r}"[:300])
    return value


def _moment(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BackpressureError("now must be a timezone-aware datetime")
    try:
        return value.astimezone(UTC).replace(microsecond=0)
    except (ValueError, OverflowError):              # a moment that has no UTC form (year 1 / 9999)
        raise BackpressureError("now must be a moment with a UTC form") from None


def _stamp(moment: datetime) -> str:
    """`...Z` whole seconds, the year always four digits (`strftime("%Y")` writes `5`)."""
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_moment(text: object) -> datetime | None:
    """The reading's moment in whole seconds UTC, or None when it is no moment."""
    if not isinstance(text, str):
        return None
    try:
        at = datetime.fromisoformat(text)
        if at.tzinfo is None:
            return None
        return at.astimezone(UTC).replace(microsecond=0)
    except (ValueError, OverflowError):              # no such moment, or none in UTC (year 1 / 9999)
        return None


def _wait_reason(wait: object) -> Reason:
    if not isinstance(wait, Wait):
        raise TypeError("an unattributed wait that is not a Wait")
    if wait.given_up is True:
        return Reason.GIVEN_UP
    if wait.due is True:
        return Reason.RESET_DUE
    if wait.evidence is Evidence.LIMIT:
        return Reason.LIMITED
    if wait.evidence is Evidence.AVAILABILITY:
        return Reason.DEGRADED
    raise TypeError("an unattributed wait with no evidence class")


def _group_reason(group: object) -> tuple[Reason, str, str]:
    if not isinstance(group, ProviderHealth):
        raise TypeError("a provider group that is not a ProviderHealth")
    reason = STATE_REASON[group.state]               # KeyError for a state with no row
    if (not isinstance(group.provider, str) or _NAME.fullmatch(group.provider) is None
            or not isinstance(group.account_handle, str)
            or _NAME.fullmatch(group.account_handle) is None):
        raise TypeError("a provider or account that is not a name")
    return reason, group.provider, group.account_handle


def _scope(providers: object) -> tuple[str, ...] | None:
    """The sorted, distinct provider names of a scope, or None for "every provider". A scope is a
    non-empty collection (a set, tuple or list; not a string) of 1 to `MAX_SCOPE` names; an empty
    one is refused, not read as "nothing counts"."""
    if providers is None:
        return None
    if isinstance(providers, (str, bytes)) or not isinstance(providers, Collection):
        raise BackpressureError("the scope is a collection of provider names or None")
    names = list(providers)
    if (not names or len(names) > MAX_SCOPE
            or any(not isinstance(n, str) or _NAME.fullmatch(n) is None for n in names)):
        raise BackpressureError(f"the scope is 1 to {MAX_SCOPE} provider names of printable ASCII "
                                f"(1-128 characters)")
    return tuple(sorted(set(names)))


def decide(planned_width: int, report: HealthReport, *, now: datetime,
           providers: Collection[str] | None = None) -> WidthDecision:
    """The width a wave of `planned_width` may have given what `report` says.

    Pure: reads nothing, writes nothing, takes no lock. Raises `BackpressureError`
    for a planned width that is not an integer from 1 to 16, a `report` that is not
    a `HealthReport`, a `now` that is not timezone-aware and a `providers` that is
    not None or 1 to 16 provider names; everything wrong *inside* a reading narrows
    to the floor with its reason (`INCOMPLETE`, `STALE`), never raises and never
    widens.

    `providers` scopes the evidence: a group of a provider **not** named is
    ignored (counted in `ignored`, still checked for being a group `health` builds);
    names are matched exactly, so a spelling that is not one of them is outside.
    Unattributed waits, problems and a stale reading count whatever the scope.
    """
    planned = _planned(planned_width)
    scope = _scope(providers)
    if not isinstance(report, HealthReport):
        raise BackpressureError(f"the reading must be a HealthReport, not {type(report).__name__}")
    moment = _moment(now)

    at = _read_moment(getattr(report, "at", None))
    report_at = _stamp(at) if at is not None else None
    if at is None or at > moment or (moment - at).total_seconds() > MAX_REPORT_AGE_SECONDS:
        return WidthDecision(planned, narrowed(Reason.STALE, planned), Reason.STALE,
                             report_at=report_at, scope=scope)

    try:
        groups, loose, problems = report.providers, report.unattributed, report.problems
        if not (isinstance(groups, tuple) and isinstance(loose, tuple)
                and isinstance(problems, tuple)):
            raise TypeError("a reading whose parts are not tuples")
        found: list[tuple[Reason, str, str]] = []
        inside = ignored = 0
        for group in groups:
            reason, provider, account = _group_reason(group)
            if scope is not None and provider not in scope:
                ignored += 1
                continue
            inside += 1
            found.append((reason, provider, account))
        found += [(_wait_reason(w), "", "") for w in loose]
        if problems:
            return WidthDecision(planned, narrowed(Reason.INCOMPLETE, planned), Reason.INCOMPLETE,
                                 groups=inside, unattributed=len(loose), problems=len(problems),
                                 report_at=report_at, scope=scope, ignored=ignored)
    except Exception:                                # anything the walk meets: the reading is not trusted
        return WidthDecision(planned, narrowed(Reason.INCOMPLETE, planned), Reason.INCOMPLETE,
                             report_at=report_at, scope=scope)

    if not found:
        return WidthDecision(planned, planned, Reason.NONE, report_at=report_at, scope=scope,
                             ignored=ignored)
    reason, provider, account = min(found, key=lambda f: (_EVIDENCE_REASONS.index(f[0]), f[1], f[2]))
    return WidthDecision(planned, narrowed(reason, planned), reason, provider=provider,
                         account_handle=account, groups=inside, unattributed=len(loose),
                         report_at=report_at, scope=scope, ignored=ignored)


# --------------------------------------------------------------------------- #
# The record: a strict, fail-closed parse
# --------------------------------------------------------------------------- #

def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"repeated key {key!r}")
        out[key] = value
    return out


def _bad(what: str, value: object) -> BackpressureError:
    return BackpressureError(f"{what} is not an acceptable value: {value!r}"[:300])


def _stamp_field(value: object, what: str) -> str:
    if not isinstance(value, str) or _TIME.fullmatch(value) is None:
        raise _bad(what, value)
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise _bad(what, value) from exc
    return value


def _int(obj: dict, key: str, low: int, high: int) -> int:
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _bad(key, value)
    return value


def parse(data: bytes) -> BackpressureRecord:
    """The record these bytes hold, or `BackpressureError` saying why not."""
    if not isinstance(data, bytes):
        raise BackpressureError("a decision record is bytes")
    if len(data) > MAX_RECORD_BYTES:
        raise BackpressureError(f"a decision record is at most {MAX_RECORD_BYTES} bytes, "
                                f"this one has {len(data)}")
    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (ValueError, RecursionError) as exc:      # UnicodeDecodeError is a ValueError
        raise BackpressureError(f"not a JSON document: {exc}"[:300]) from exc
    if not isinstance(obj, dict) or set(obj) != set(_KEYS):
        got = sorted(obj) if isinstance(obj, dict) else type(obj).__name__
        raise BackpressureError(f"a decision record has the fields {got}, not {sorted(_KEYS)}"[:300])
    if obj["schema"] != SCHEMA:
        raise _bad("schema", obj["schema"])
    decision_id = obj["decision_id"]
    if not isinstance(decision_id, str) or _ID.fullmatch(decision_id) is None:
        raise _bad("decision_id", decision_id)
    decided_at = _stamp_field(obj["decided_at"], "decided_at")
    report_at = None if obj["report_at"] is None else _stamp_field(obj["report_at"], "report_at")
    planned = _int(obj, "planned_width", 1, MAX_WAVE_WIDTH)
    width = _int(obj, "width", 1, planned)
    try:
        reason = Reason(obj["reason"])
    except ValueError as exc:
        raise _bad("reason", obj["reason"]) from exc
    provider, account = obj["provider"], obj["account_handle"]
    for what, value in (("provider", provider), ("account_handle", account)):
        if not isinstance(value, str) or _NAME_OR_EMPTY.fullmatch(value) is None:
            raise _bad(what, value)
    groups = _int(obj, "groups", 0, MAX_COUNT)
    loose = _int(obj, "unattributed", 0, MAX_COUNT)
    problems = _int(obj, "problems", 0, MAX_COUNT)
    ignored = _int(obj, "ignored", 0, MAX_COUNT)
    raw_scope = obj["scope"]
    scope: tuple[str, ...] | None = None
    if raw_scope is not None:
        if (not isinstance(raw_scope, list) or not 1 <= len(raw_scope) <= MAX_SCOPE
                or any(not isinstance(n, str) or _NAME.fullmatch(n) is None for n in raw_scope)
                or raw_scope != sorted(set(raw_scope))):
            raise _bad("scope", raw_scope)
        scope = tuple(raw_scope)
    if width != narrowed(reason, planned):
        raise BackpressureError(f"width {width} is not what reason {reason.value} gives for "
                                f"{planned}")
    if bool(provider) != bool(account):
        raise BackpressureError("a provider and its account are named together or not at all")
    if reason not in _EVIDENCE_REASONS and (provider or account):
        raise BackpressureError(f"reason {reason.value} names no provider")
    if provider and groups < 1:
        raise BackpressureError("a named provider is one of the groups counted")
    if provider and scope is not None and provider not in scope:
        raise BackpressureError("a named provider is inside the scope it was decided for")
    if scope is None and ignored:
        raise BackpressureError("groups are ignored only by a scope")
    if reason is Reason.STALE and (groups or loose or problems or ignored):
        raise BackpressureError("a stale reading was not walked")
    if reason is Reason.NONE and (groups or loose or problems):
        raise BackpressureError("no pressure with evidence counted")
    if reason in _EVIDENCE_REASONS and groups + loose < 1:
        raise BackpressureError(f"reason {reason.value} without a group or wait to say it")
    if report_at is None and reason is not Reason.STALE:
        raise BackpressureError("only a stale reading has no moment")
    decision = WidthDecision(planned, width, reason, provider=provider, account_handle=account,
                             groups=groups, unattributed=loose, problems=problems,
                             report_at=report_at, scope=scope, ignored=ignored)
    return BackpressureRecord(decision_id, decided_at, decision)


def read_decision(path: Path | str) -> BackpressureRecord | None:
    """The record at `path`; None only when there is no such file. A link, a FIFO,
    a directory, a second name, an unreadable or oversize file, or bytes that are
    not a record is `BackpressureError` -- never "no decision"."""
    try:
        kind = os.lstat(path)
        if stat.S_ISREG(kind.st_mode) and kind.st_size > MAX_RECORD_BYTES:
            raise BackpressureError(f"{path} is {kind.st_size} bytes; a decision record is at "
                                    f"most {MAX_RECORD_BYTES}")
        data = durable_io.read_single_link(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:             # ValueError: a NUL in the path
        raise BackpressureError(f"{path} cannot be read: {exc}"[:300]) from exc
    return None if data is None else parse(data)


# --------------------------------------------------------------------------- #
# The controller: decide, write, then return
# --------------------------------------------------------------------------- #

class BackpressureController:
    """Takes one decision per call and writes it down first. Holds no state
    between calls: every decision is one id and one record file."""

    def __init__(self, record_dir: Path | str, *, clock: Callable[[], str] = utcnow) -> None:
        if not isinstance(record_dir, (str, os.PathLike)):
            raise BackpressureError("the record directory is a path")
        self.record_dir = Path(record_dir).expanduser()
        if not callable(clock):
            raise BackpressureError("clock must be callable")
        self.clock = clock

    def _record_path(self, decision_id: object) -> Path:
        if not isinstance(decision_id, str) or _ID.fullmatch(decision_id) is None:
            raise BackpressureError("a decision id is 1 to 64 characters of letters, digits, "
                                    f"'.', '_' and '-': {decision_id!r}"[:300])
        try:
            kind = os.lstat(self.record_dir)
        except (OSError, ValueError) as exc:         # ValueError: a NUL in the path
            raise BackpressureError(f"the record directory cannot be looked at ({type(exc).__name__}): "
                                    f"{self.record_dir}"[:300]) from exc
        if not stat.S_ISDIR(kind.st_mode):
            raise BackpressureError(f"the record directory is not a directory of its own: {self.record_dir}"[:300])
        path = self.record_dir / f"backpressure-{decision_id}.json"
        if os.path.lexists(path):
            raise BackpressureError(f"a decision id is used once, and this one exists: {path}"[:300])
        return path

    def admit_width(self, decision_id: str, planned_width: int, report: HealthReport,
                    providers: Collection[str] | None = None) -> WidthDecision:
        """`decide` at the clock's moment, written to `backpressure-<id>.json`
        before it is returned. Raises `BackpressureError` for everything that
        `decide` refuses, for an id that is not acceptable or already used, for a
        record directory that is not a directory of its own and for a record that
        could not be written -- in every case nothing is returned."""
        path = self._record_path(decision_id)
        _planned(planned_width)
        try:
            stamp = self.clock()
            now = datetime.strptime(_stamp_field(stamp, "the clock"), "%Y-%m-%dT%H:%M:%SZ"
                                    ).replace(tzinfo=UTC)
        except BackpressureError:
            raise
        except Exception as exc:
            raise BackpressureError(f"the clock cannot be read: {type(exc).__name__}") from exc
        decision = decide(planned_width, report, now=now, providers=providers)
        record = BackpressureRecord(decision_id, _stamp(now), decision)
        try:
            durable_io.durable_create(path, record.to_bytes(), mode=0o600)
        except Exception as exc:
            raise BackpressureError(f"the decision could not be written, so it is not "
                                    f"returned: {type(exc).__name__}: {exc}"[:300]) from exc
        return decision


# --------------------------------------------------------------------------- #
# The wave's admission (slice 2): the production caller
# --------------------------------------------------------------------------- #

MAX_WAVE_NODES = MAX_WAVE_WIDTH
"""Nodes one admission judges: a wave is never wider than `wave.MAX_WAVE_WIDTH`."""


def providers_of(launcher: object) -> tuple[str, ...] | None:
    """The providers the runs `launcher` starts execute their roles on, sorted and distinct; None when that
    cannot be told (the decision is then unscoped: every provider counts).

    A role runs on the launcher's named harness profile (`planner`, `developer`, `qa`), replaced by the
    provider the launcher's routing policy names for that role (`launcher._role_options` does the same: a
    routed role takes the policy's provider). A policy that cannot be read, or a profile that is not a name,
    is None -- never a guess. The account is not read: every account of a provider is in scope."""
    try:
        policy = None
        path = getattr(launcher, "routing_policy", None)
        if path is not None:
            policy = load_routing_policy(path)
        words = []
        for role, attribute in ((Role.PLANNER, "planner"), (Role.DEVELOPER, "developer"),
                                (Role.QA, "qa")):
            route = policy.roles.get(role) if policy is not None else None
            word = route.provider if route is not None and route.provider is not None \
                else getattr(launcher, attribute, None)
            if not isinstance(word, str) or _NAME.fullmatch(word) is None:
                return None
            words.append(word)
        return tuple(sorted(set(words)))
    except Exception:                                # a policy that cannot be read: unscoped
        return None


@dataclass(frozen=True)
class WaveGate:
    """What the wave's admission answered. `admitted` are the node ids that may be prepared (a prefix of the
    ids offered, never empty when any was offered), `waits` is `(node id, reason)` for each other one, `why`
    one bounded line. `judged` is False when no decision could be taken (the floor, width 1, applied and
    `decision` is None); `record` is the file name of the decision written, else None."""

    admitted: tuple[str, ...]
    waits: tuple[tuple[str, str], ...]
    why: str
    judged: bool
    decision: WidthDecision | None = None
    record: str | None = None


def _floor_gate(ids: tuple[str, ...], why: str) -> WaveGate:
    """Width 1: the sequential controller's round. The first node offered runs, the rest wait."""
    waits = tuple((n, f"BACKPRESSURE/unjudged: {why}"[:300]) for n in ids[1:])
    return WaveGate(ids[:1], waits, f"backpressure cannot be judged: {why}"[:400], False)


def admit_wave(node_ids: Iterable[str], *, launcher: object, record_dir: Path | str, decision_id: str,
               now: Callable[[], datetime] | None = None) -> WaveGate:
    """Which of the nodes `node_ids` (the wave's candidates, in priority order) may be prepared and launched
    while the providers of `launcher`'s runs are under pressure? See the module text.

    **Never raises and never admits nobody of a list it was given**: whatever cannot be known -- the runs
    root, the health reading, the record -- is the floor (`judged=False`, the first node alone); ids that are
    not a list of strings are an empty answer (`admitted=()`, `judged=False`). Reads the health of
    `launcher.root` once (`health.read_health` at `now()`, the clock of the whole admission), decides with
    `planned_width` the number of nodes offered, scoped to `providers_of(launcher)`, and writes the record
    `backpressure-<decision_id>.json` into `record_dir` (created if it is missing) before it answers."""
    try:
        ids = tuple(node_ids)
    except Exception:                                # noqa: BLE001 - nothing to wait for: a bad request
        return WaveGate((), (), "the nodes of the wave cannot be listed", False)
    if not ids or any(type(n) is not str for n in ids):
        return WaveGate((), (), "no node offered, or a node id that is not a string", False)
    if len(ids) > MAX_WAVE_NODES or len(set(ids)) != len(ids):
        return _floor_gate(ids, f"{len(ids)} node(s) offered; a wave judges 1 to {MAX_WAVE_NODES} "
                                f"distinct ones")
    try:
        moment = datetime.now(UTC) if now is None else now()
        root = os.fspath(getattr(launcher, "root"))
        kind = os.lstat(root)                        # a runs root that is not there is not "nothing parked"
        if not stat.S_ISDIR(kind.st_mode):
            raise BackpressureError("the runs root is not a directory")
        report = read_health(root, now=moment)
        scope = providers_of(launcher)
        try:
            os.mkdir(record_dir, 0o700)
        except FileExistsError:
            pass
        stamp = _stamp(_moment(moment))
        controller = BackpressureController(record_dir, clock=lambda: stamp)
        decision = controller.admit_width(decision_id, len(ids), report, providers=scope)
    except Exception as exc:                         # noqa: BLE001 - fail closed: the floor
        return _floor_gate(ids, f"{type(exc).__name__}: {str(exc)[:200]}")
    width = decision.width
    admitted, cut = ids[:width], ids[width:]
    named = f" {decision.provider}/{decision.account_handle}" if decision.provider else ""
    scope_text = "every provider" if decision.scope is None else "providers " + ", ".join(decision.scope)
    why = (f"backpressure: width {width} of {decision.planned} ({decision.reason.value}{named}); {scope_text}"
           f"{f'; {decision.ignored} group(s) outside the scope' if decision.ignored else ''}"
           f"; record backpressure-{decision_id}.json")[:400]
    reason = f"BACKPRESSURE/{decision.reason.value}: width {width} of {decision.planned}{named}"
    return WaveGate(admitted, tuple((n, reason) for n in cut), why, True, decision,
                    f"backpressure-{decision_id}.json")


__all__ = ["KEEP", "HALF", "FLOOR", "MAX_COUNT", "MAX_RECORD_BYTES", "MAX_REPORT_AGE_SECONDS",
           "SCHEMA", "STATE_REASON", "WIDTH_RULE", "BackpressureController", "BackpressureError",
           "BackpressureRecord", "MAX_SCOPE", "MAX_WAVE_NODES", "Reason", "WaveGate",
           "WidthDecision", "admit_wave", "decide", "narrowed", "parse", "providers_of",
           "read_decision"]
