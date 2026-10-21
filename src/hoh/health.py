"""Provider health across runs: what the parked runs of one root say together.

V3.3 P3-08, first increment. Until this module a provider failure was a fact
about one run. Run `a` parks on a usage limit, run `b` and `c` of the same
provider and account park on the same limit one by one -- each by spending a
dispatch against a provider that had already said no -- and nothing could say
"these three are one event". This module is that sentence: a **reading** of the
runs under one root, grouped by the provider and account the park receipt
(`dispatchrecovery.RoutingDecision`) names.

It is deliberately the smallest honest step, and says what it is not:

* **A reader.** A function of what is on disk and of `now`. It writes nothing,
  takes no lock, arms no timer, launches nothing and never resumes a run: a run's
  state is read in memory (never `RunStore.read_state`, which may park and
  rewrite), and what cannot be read is a *problem* in the report, not a run that
  is silently absent. It is not a daemon. `read_health` is not a gate either: the
  one thing that acts on it is `ProviderHealthSentinel` below (P3-08, second
  increment), which `resumeloop.resume_runs` consults.
* **Evidence, not health.** A provider appears only where some run is parked on
  it. Absence is "no evidence", never "healthy"; there is no HEALTHY state.
* **A park the loop gave up on is not a wait.** `resumeloop` counts unsuccessful
  resumes and, at `quota.MAX_RESUME_ATTEMPTS`, writes a `RETRIES_EXHAUSTED`
  escalation and leaves the run parked for a person. Such a park (the state's
  `resume_attempts` at the limit, or an escalation naming its park) is `given_up`:
  it says nothing about when the provider answers, and a group of nothing else is
  `GIVEN_UP`, never `RESET_DUE`.
* **Advisory.** A receipt is a digest-bound line, which is integrity, not
  authenticity (`dispatchrecovery.verify_binding` says the same). A consumer that
  will *act* on a reading -- refuse a dispatch, resume a run -- binds each park
  with `verify_binding` first. This module does not.
* **No false correlation.** A park whose provider cannot be named (a legacy park
  with no receipt, a receipt that says `NOT_AVAILABLE`) is listed under
  `unattributed` and never grouped: two unnamed providers are not one.

The classes that count as evidence about a *provider* are a closed table
(`PROVIDER_EVIDENCE`), checked against every `NormalizedFailure`: a role timeout
and a stalled prompt say nothing about the provider (`failures.py`), and an
operator-required class is not an automatic wait, so it does not reach this
reading at all (tracked, not built).
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import quota
from .contracts import Condition
from .dispatchrecovery import (
    RECOVERY_DIR, ROUTING_FILE, ParkVerdict, RoutingDecision, RoutingEscalation, _digest_of,
    stale_master_reason, verify_binding,
)
from .durable_io import read_single_link
from .failures import AUTOMATIC_KINDS, NormalizedFailure, blocked_kind
from .store import RESERVED_PREFIXES, RESERVED_RUN_IDS, RunStore
from .telemetry import NOT_AVAILABLE

SCHEMA = "hoh-provider-health/1"

#: A state or a receipts file larger than this is not read (a problem, never a
#: partial read). A run's state is a few hundred KiB at most; the bound exists so
#: that a file somebody filled cannot make a read-only look allocate without end.
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
#: A provider or model name longer than this is not a name (the receipt's strings
#: are otherwise unbounded, and the report echoes them); a stated retry time is
#: echoed to at most this many characters.
MAX_NAME = 128
MAX_STAMP = 64


class Evidence(StrEnum):
    """What a parked run says about its provider."""

    #: The account is out of quota or rate: it will answer again at a reset time.
    LIMIT = "LIMIT"
    #: The provider or the path to it failed for now.
    AVAILABILITY = "AVAILABILITY"


_N = NormalizedFailure

#: Every class of `NormalizedFailure`, decided here and nowhere else. `None`: the
#: class says nothing about the provider's health (a role that outlived its
#: budget, a pane that did not take the prompt, a local runtime, a credential or
#: model problem that a person must act on, anything that is not infrastructure).
#: A class added to `failures.py` without a row here fails the completeness test.
PROVIDER_EVIDENCE: dict[NormalizedFailure, Evidence | None] = {
    _N.USAGE_LIMIT: Evidence.LIMIT,
    _N.RATE_LIMIT: Evidence.LIMIT,
    _N.PROVIDER_UNAVAILABLE: Evidence.AVAILABILITY,
    _N.NETWORK_TRANSIENT: Evidence.AVAILABILITY,
    _N.ROLE_TIMEOUT: None,
    _N.PROMPT_STALLED: None,
    _N.AUTH_EXPIRED: None,
    _N.ACCOUNT_ACTION_REQUIRED: None,
    _N.MODEL_UNAVAILABLE: None,
    _N.LOCAL_RUNTIME_UNAVAILABLE: None,
    _N.NOT_INFRASTRUCTURE: None,
    _N.UNCLASSIFIED: None,
}


class HealthState(StrEnum):
    #: Some run waits on a limit whose stated retry time has not come.
    LIMITED = "LIMITED"
    #: No limit is pending, some run waits on an availability failure that has not
    #: come due.
    DEGRADED = "DEGRADED"
    #: The stated retry time of every live wait has passed and the runs are still
    #: parked: the loop has not resumed them, or refused to (an unreadable stamp
    #: on the timer path, a held run). Not "healthy" -- the provider has not
    #: answered.
    RESET_DUE = "RESET_DUE"
    #: Every wait of the group is one the resume loop gave up on: the stated
    #: times mean nothing now, a person decides.
    GIVEN_UP = "GIVEN_UP"


@dataclass(frozen=True)
class Wait:
    """One run parked on a provider wait, as the state and its receipt say."""

    run_id: str
    failure: NormalizedFailure
    evidence: Evidence
    retry_at: str | None
    due: bool
    provider: str
    account_handle: str
    model: str
    #: Why no provider is named, for an unattributed wait; empty otherwise.
    unattributed: str = ""
    #: The resume loop gave this park up (or will, on its next look): the state's
    #: `resume_attempts` is at the limit, or a `RETRIES_EXHAUSTED` escalation
    #: names its park.
    given_up: bool = False
    #: The stated retry time read from the **whole** stamp (`retry_at` is only its
    #: echo, cut at `MAX_STAMP`); None when it is no time. Not in `as_dict`.
    when: datetime | None = None
    #: The run's state as parsed in memory, kept so that a consumer that acts can
    #: `verify_binding` the park. Not in `as_dict`, not compared, not echoed.
    state: Any = field(default=None, compare=False, repr=False)

    def as_dict(self) -> dict:
        return {"run_id": self.run_id, "failure": self.failure.value,
                "evidence": self.evidence.value, "retry_at": self.retry_at,
                "due": self.due, "provider": self.provider,
                "account_handle": self.account_handle, "model": self.model,
                "unattributed": self.unattributed, "given_up": self.given_up}


@dataclass(frozen=True)
class ProviderHealth:
    """What the parked runs of one (provider, account) say together."""

    provider: str
    account_handle: str
    state: HealthState
    #: The latest stated retry time among the group's live (not given-up) waits;
    #: None when no stamp is readable, and for a `GIVEN_UP` group. Every pending
    #: wait is later than `now` and every elapsed one is not, so this is the
    #: pending one whenever something is pending.
    not_before: str | None
    runs: tuple[str, ...]
    failures: tuple[tuple[str, int], ...]
    #: The runs of the group the resume loop gave up on (a subset of `runs`);
    #: they do not decide `state` or `not_before`.
    given_up: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"provider": self.provider, "account_handle": self.account_handle,
                "state": self.state.value, "not_before": self.not_before,
                "runs": list(self.runs),
                "failures": {name: n for name, n in self.failures},
                "given_up": list(self.given_up)}


@dataclass(frozen=True)
class HealthReport:
    at: str
    providers: tuple[ProviderHealth, ...]
    unattributed: tuple[Wait, ...]
    #: `(what, why)` for everything under the root that could not be read. A
    #: report with problems is **incomplete**: the evidence it lacks may be about
    #: any provider.
    problems: tuple[tuple[str, str], ...]
    runs_seen: int

    @property
    def complete(self) -> bool:
        return not self.problems

    def for_provider(self, provider: str, account_handle: str = NOT_AVAILABLE
                     ) -> ProviderHealth | None:
        return next((p for p in self.providers
                     if p.provider == provider and p.account_handle == account_handle), None)

    def as_dict(self) -> dict:
        return {"schema": SCHEMA, "at": self.at, "complete": self.complete,
                "runs_seen": self.runs_seen,
                "providers": [p.as_dict() for p in self.providers],
                "unattributed": [w.as_dict() for w in self.unattributed],
                "problems": [{"what": what, "why": why} for what, why in self.problems]}


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

class _Unreadable(Exception):
    """A file this reading refused, with the reason."""


def _read_bounded(path: Path, limit: int) -> bytes | None:
    """The bytes of one regular, single-name file of at most `limit` bytes; None
    when it does not exist. Anything else -- a link (dangling or not), a FIFO, a
    directory, a second name, a file over the bound, no permission -- is
    `_Unreadable`. The open never blocks and never follows a link."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _Unreadable(f"{type(exc).__name__}: {exc.strerror or exc}") from None
    if not stat.S_ISREG(st.st_mode):
        raise _Unreadable("is not a regular file")
    if st.st_size > limit:
        raise _Unreadable(f"is larger than {limit} bytes")
    try:
        data = read_single_link(path)
    except OSError as exc:
        raise _Unreadable(f"{type(exc).__name__}: {exc.strerror or exc}") from None
    if data is None:
        return None
    if len(data) > limit:
        raise _Unreadable(f"is larger than {limit} bytes")
    return data


def _receipts(run_dir: Path) -> tuple[list[RoutingDecision], set[str], list[str]]:
    """The valid `RoutingDecision`s of a run in file order, the park ids the resume
    loop gave up on (`RETRIES_EXHAUSTED` escalations), and the problems of its lines -- the rule of `dispatchrecovery.read_records`, over bytes read
    safely (that function follows a link, reads a second name, and raises
    `UnicodeDecodeError` on bytes that are not UTF-8)."""
    data = _read_bounded(run_dir / RECOVERY_DIR / ROUTING_FILE, MAX_RECEIPT_BYTES)
    if data is None:
        return [], set(), []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _Unreadable("is not UTF-8") from None
    found: list[RoutingDecision] = []
    exhausted: set[str] = set()
    problems: list[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                problems.append(f"line {n}: not a JSON object")
                continue
            claimed = raw.pop("digest")
            if claimed != _digest_of(raw):
                problems.append(f"line {n}: digest mismatch")
                continue
            cls = {"ROUTING_DECISION": RoutingDecision,
                   "ROUTING_ESCALATION": RoutingEscalation}[raw["kind"]]
            rec = cls.model_validate(raw)
            if isinstance(rec, RoutingDecision):
                found.append(rec)
            elif rec.escalation == "RETRIES_EXHAUSTED":
                exhausted.add(rec.park_id)
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            problems.append(f"line {n}: {type(exc).__name__}")
    return found, exhausted, problems


def _when(stamp: str | None) -> datetime | None:
    """`quota.is_due`'s reading of a stamp (`Z` accepted, naive taken as UTC), or
    None when it is no time."""
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _describes(rec: RoutingDecision, state) -> bool:
    """Does this receipt describe the park the state is in? The state's own
    words only: the stated retry time, the class, and the run, iteration, attempt
    and stage the receipt binds. (`verify_binding` also checks the programme, the
    candidate and the master; this reading does not act, so it does not need them.)"""
    try:
        if blocked_kind(NormalizedFailure(rec.reason)) != state.blocked_kind:
            return False
    except ValueError:
        return False
    b = rec.binding
    return (rec.retry_at == state.retry_after and b.run_id == state.run_id
            and b.iteration == state.iteration and b.attempt == state.attempt
            and b.stage == state.stage.value)


def _attribute(run_dir: Path, state) -> tuple[RoutingDecision | None, str, set[str]]:
    """The newest valid receipt that describes this park, or None and why; and the
    park ids the loop gave up on."""
    try:
        decisions, exhausted, problems = _receipts(run_dir)
    except _Unreadable as exc:
        return None, f"the routing receipts {exc}", set()
    for rec in reversed(decisions):
        if _describes(rec, state):
            return rec, "", exhausted
    if problems:
        return (None, f"no receipt describes this park; {len(problems)} receipt line(s) "
                      "unreadable", exhausted)
    return None, "no receipt describes this park", exhausted


def _wait_of(store: RunStore, state, now: datetime) -> Wait | None:
    """The provider wait a run is parked on, or None when it is not parked on one."""
    if state.condition is not Condition.BLOCKED or state.blocked_kind not in AUTOMATIC_KINDS:
        return None
    failure = NormalizedFailure(state.blocked_kind.upper())
    evidence = PROVIDER_EVIDENCE[failure]
    if evidence is None:
        return None
    due = quota.is_due(state.retry_after, now=now)
    rec, why, exhausted = _attribute(store.dir, state)
    given_up = (state.resume_attempts >= quota.MAX_RESUME_ATTEMPTS
                or (rec is not None and rec.park_id in exhausted))
    provider = account = model = NOT_AVAILABLE
    if rec is not None:
        provider = rec.provenance.provider
        account = rec.provenance.account_handle
        model = rec.provenance.model
        if provider == NOT_AVAILABLE:
            rec, why = None, "the receipt does not name the provider"
        elif len(provider) > MAX_NAME or len(model) > MAX_NAME:
            rec, why = None, f"the receipt names a provider or model of over {MAX_NAME} characters"
    if rec is None:
        provider = account = model = NOT_AVAILABLE      # nothing unverified is echoed
    retry_at = state.retry_after[:MAX_STAMP] if state.retry_after is not None else None
    return Wait(run_id=store.run_id, failure=failure, evidence=evidence,
                retry_at=retry_at, due=due, provider=provider,
                account_handle=account, model=model,
                unattributed="" if rec is not None else why,
                given_up=given_up, when=_when(state.retry_after), state=state)


def _state_of(store: RunStore):
    """The run's state parsed in memory, or None when the directory has no
    `state.json` (it is no run). `_Unreadable` for everything else."""
    try:
        data = _read_bounded(store.state_path, MAX_STATE_BYTES)
    except _Unreadable as exc:
        raise _Unreadable(f"state.json {exc}") from None
    if data is None:
        return None
    try:
        state = RunStore._parse_state(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise _Unreadable("state.json is not UTF-8") from None
    except ValueError as exc:                      # pydantic's and json's errors
        raise _Unreadable(f"state.json is damaged or schema-foreign: {type(exc).__name__}") from None
    except Exception as exc:                       # noqa: BLE001 - StoreError, MigrationError
        raise _Unreadable(f"state.json is not readable at this schema: {type(exc).__name__}") from None
    if str(state.run_id) != store.run_id:
        # A restored or renamed copy keeps its run id; read here it would count the
        # same park twice (`dispatchrecovery._verify` refuses it for the same reason).
        raise _Unreadable(f"state.json names run {str(state.run_id)[:64]!r}, not this directory")
    return state


def _fold(waits: list[Wait]) -> list[ProviderHealth]:
    """One `ProviderHealth` per (provider, account), in name order. A wait the
    resume loop gave up on is listed (`given_up`) and decides nothing; a group of
    nothing else is `GIVEN_UP`. A stated retry time that is no time is a wait that
    has come due (`quota.is_due`'s rule) and contributes no `not_before`; the time
    is read from the whole stamp, not from its echo."""
    groups: dict[tuple[str, str], list[Wait]] = {}
    for w in waits:
        groups.setdefault((w.provider, w.account_handle), []).append(w)
    out = []
    for (provider, account), members in sorted(groups.items()):
        live = [w for w in members if not w.given_up]
        pending = [w for w in live if not w.due]
        if not live:
            state = HealthState.GIVEN_UP
        elif not pending:
            state = HealthState.RESET_DUE
        elif any(w.evidence is Evidence.LIMIT for w in pending):
            state = HealthState.LIMITED
        else:
            state = HealthState.DEGRADED
        stamps = [w.when for w in live if w.when is not None]
        counts: dict[str, int] = {}
        for w in members:
            counts[w.failure.value] = counts.get(w.failure.value, 0) + 1
        out.append(ProviderHealth(
            provider=provider, account_handle=account, state=state,
            not_before=max(stamps).isoformat() if stamps else None,
            runs=tuple(sorted(w.run_id for w in members)),
            failures=tuple(sorted(counts.items())),
            given_up=tuple(sorted(w.run_id for w in members if w.given_up))))
    return out


def _collect(root: Path | str, now: datetime, exclude: Collection[str] = ()
             ) -> tuple[list[Wait], list[tuple[str, str]], int]:
    """Every provider wait under a runs root, the problems of reading it, and the
    number of runs seen. `now` is aware (the callers check). A run named in `exclude`
    is not read at all: neither its waits nor its damage count (the run that is asking
    is not evidence about itself)."""
    base = Path(root).expanduser()
    problems: list[tuple[str, str]] = []
    waits: list[Wait] = []
    seen = 0
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        names = []                          # no runs root yet: no runs, nothing unread
    except OSError as exc:
        names = []
        problems.append(("root", f"{type(exc).__name__}: {exc.strerror or exc}"))
    for name in names:
        if name.startswith(RESERVED_PREFIXES) or name in RESERVED_RUN_IDS:
            continue                        # names the runs root keeps for itself
        if name in exclude:
            continue
        entry = base / name
        try:
            kind = os.lstat(entry).st_mode
        except OSError as exc:
            problems.append((name, f"{type(exc).__name__}: {exc.strerror or exc}"))
            continue
        if stat.S_ISLNK(kind):
            problems.append((name, "is a symbolic link: not read"))
            continue
        if not stat.S_ISDIR(kind):
            continue                        # timers.json, .resume.lock, loose files
        try:
            store = RunStore(base, name)
        except Exception as exc:            # noqa: BLE001 - UnsafeRunId and friends
            problems.append((name, f"not a usable run id: {type(exc).__name__}"))
            continue
        try:
            state = _state_of(store)
        except _Unreadable as exc:
            problems.append((name, str(exc)))
            continue
        if state is None:
            continue                        # a directory without a state is no run
        seen += 1
        wait = _wait_of(store, state, now)
        if wait is not None:
            waits.append(wait)
    return waits, problems, seen


def read_health(root: Path | str, *, now: datetime | None = None,
                exclude_runs: Collection[str] = (), verified: bool = False) -> HealthReport:
    """The cross-run reading of the provider waits under a runs root.

    `now` must be timezone-aware (a naive moment is refused with `ValueError`:
    comparing it with a stated reset is a guess). Never raises for what is on
    disk: every run that cannot be read is a problem of the report.

    `exclude_runs`: runs that are not read (a consumer that asks about its own run
    leaves that run's park out: it is not evidence about itself). `verified`: only a
    wait that the sentinel would count -- its park still bound (a governed park only
    while its master is live and current), its stated reset within `MAX_HOLD` -- is
    evidence; the others are dropped, not reported. A consumer that *acts* on the
    reading asks for it (see `read_verified_health`).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    waits, problems, seen = _collect(root, now, frozenset(exclude_runs))
    if verified:
        sentinel = ProviderHealthSentinel(root, now=now)
        ceiling = now + MAX_HOLD
        waits = [w for w in waits
                 if w.unattributed or w.given_up
                 or ((w.when is None or w.when <= ceiling) and sentinel._binds(w))]
    attributed = [w for w in waits if not w.unattributed]
    return HealthReport(
        at=now.isoformat(), providers=tuple(_fold(attributed)),
        unattributed=tuple(w for w in waits if w.unattributed),
        problems=tuple(problems), runs_seen=seen)


def read_verified_health(root: Path | str, *, now: datetime | None = None,
                         exclude_runs: Collection[str] = ()) -> HealthReport:
    """`read_health` for a consumer that acts on it: `verified=True`. The reading a launched
    run's failover decision is made from (`routing.plan_failover(reader=...)`)."""
    return read_health(root, now=now, exclude_runs=exclude_runs, verified=True)


# --------------------------------------------------------------------------- #
# Before a neighbour's park is verified: never open what could block
# --------------------------------------------------------------------------- #

#: The files `dispatchrecovery.verify_binding` (and `governance.bind` under it) opens
#: for a run, relative to the run directory, with plain `read_text`: a FIFO there
#: blocks the opener for ever. The sentinel verifies *other* runs' parks, which the
#: resume loop never opened before, so it looks at each of them first (`lstat`, no
#: open) and treats anything but a regular single-name file within bound as "this
#: neighbour is not evidence".
_VERIFY_FILES = (
    ("recovery", "master.json"), ("recovery", "routing.jsonl"),
    ("autonomy", "contract.json"), ("autonomy", "routings.jsonl"),
    ("approval", "autonomy.json"),
)
_VERIFY_DIRS = ("recovery", "autonomy", "approval")
_CLAIMS = ("recovery", "claims")
#: A run has a handful of claims; a claims directory with more entries than this
#: is not read through (a problem, never a partial look).
MAX_CLAIM_ENTRIES = 1024


def _regular(path: Path, *, limit: int = MAX_RECEIPT_BYTES) -> bool | None:
    """None when `path` does not exist, True for one regular single-name file of at
    most `limit` bytes, False for anything else (link, FIFO, directory, second name,
    over bound, unreadable metadata). Never opens it."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_size <= limit


def _unsafe_to_verify(run_dir: Path) -> str | None:
    """Why `verify_binding` must not be run on this run's park, or None when every
    file it would open is absent or a regular single-name file."""
    for name in _VERIFY_DIRS:
        try:
            st = os.lstat(run_dir / name)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return f"{name}/ is not readable ({type(exc).__name__})"
        if not stat.S_ISDIR(st.st_mode):
            return f"{name}/ is not a plain directory"
    for parts in _VERIFY_FILES:
        if _regular(run_dir.joinpath(*parts)) is False:
            return f"{'/'.join(parts)} is not a regular single-name file within bound"
    claims = run_dir.joinpath(*_CLAIMS)
    try:
        st = os.lstat(claims)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"{'/'.join(_CLAIMS)} is not readable ({type(exc).__name__})"
    if not stat.S_ISDIR(st.st_mode):
        return f"{'/'.join(_CLAIMS)} is not a plain directory"
    try:
        with os.scandir(claims) as entries:
            for n, entry in enumerate(entries, 1):
                if n > MAX_CLAIM_ENTRIES:
                    return f"{'/'.join(_CLAIMS)} holds more than {MAX_CLAIM_ENTRIES} entries"
                if _regular(Path(entry.path)) is False:
                    return f"a claim ({entry.name[:64]!r}) is not a regular single-name file"
    except OSError as exc:
        return f"{'/'.join(_CLAIMS)} is not readable ({type(exc).__name__})"
    return None


def _is_governed(run_dir: Path) -> bool | None:
    """Does the run declare a governed master (`recovery/master.json` kind GOVERNED)?
    False for a run with no record (a plain run); None when the record is there and
    cannot be read safely or does not say (the run is then neither consulted nor
    evidence). Read through the bounded reader, never `read_text`."""
    try:
        data = _read_bounded(run_dir / "recovery" / "master.json", MAX_STATE_BYTES)
    except _Unreadable:
        return None
    if data is None:
        return False
    try:
        raw = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    return True if kind == "GOVERNED" else (False if kind == "NONE" else None)


#: `dispatchrecovery.master_standing` (under `verify_binding` of a governed park)
#: reads the state of **every** project under the root (`<root>/projects/<id>/
#: project.json`) with a plain `read_text` after `exists()`: a FIFO there blocks.
#: A governed neighbour is therefore only verified after each project's
#: `project.json` has been `lstat`ed (a project's lease and epoch chain are read
#: behind `is_file()`, which a FIFO fails). A project entry that is a link, and
#: `projects/` itself being a link, are refused: `list_projects` follows them.
MAX_PROJECTS = 1024
_PROJECT_FILES = ("project.json",)


def _unsafe_projects(root: Path) -> str | None:
    """Why the projects under `root` must not be asked about a master, or None."""
    base = root / "projects"
    try:
        st = os.lstat(base)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"projects/ is not readable ({type(exc).__name__})"
    if not stat.S_ISDIR(st.st_mode):
        return "projects/ is not a plain directory"
    try:
        with os.scandir(base) as entries:
            for n, entry in enumerate(entries, 1):
                if n > MAX_PROJECTS:
                    return f"projects/ holds more than {MAX_PROJECTS} entries"
                try:
                    kind = os.lstat(entry.path).st_mode
                except OSError:
                    return f"project {entry.name[:64]!r} is not readable"
                if stat.S_ISLNK(kind):
                    # `list_projects` follows a link (`iterdir` + `exists()`), and
                    # `read_state` then reads through it: a link is never a project
                    # to look past, whatever it points at.
                    return f"project {entry.name[:64]!r} is a symbolic link"
                if not stat.S_ISDIR(kind):
                    continue                    # a loose file is no project
                for name in _PROJECT_FILES:
                    if _regular(Path(entry.path) / name) is False:
                        return (f"project {entry.name[:64]!r}: {name} is not a regular "
                                "single-name file within bound")
    except OSError as exc:
        return f"projects/ is not readable ({type(exc).__name__})"
    return None


# --------------------------------------------------------------------------- #
# The sentinel: the one consumer that acts on the reading
# --------------------------------------------------------------------------- #

#: A stated reset further away than this is not evidence for a hold (a forged or
#: mistyped stamp must not park a provider's runs for good). It is the longest
#: wait `quota` reads from a banner (`quota._MAX_STATED_WAIT`).
MAX_HOLD = timedelta(hours=72)


class Action(StrEnum):
    #: The sentinel has no verified reason to keep this run waiting.
    PROCEED = "PROCEED"
    #: Other runs of the same provider, account and model state a limit that is
    #: still pending: dispatching now would spend a turn on it.
    HOLD = "HOLD"


@dataclass(frozen=True)
class Consultation:
    """What `ProviderHealthSentinel.consult` says about one run."""

    action: Action
    reason: str
    #: The latest verified pending reset (ISO, UTC) a `HOLD` waits for; else None.
    not_before: str | None = None
    #: The runs whose verified pending limit is the evidence of a `HOLD`.
    evidence: tuple[str, ...] = ()
    #: False when the reading itself failed: `PROCEED` is then "no opinion", said.
    available: bool = True

    def as_dict(self) -> dict:
        return {"action": self.action.value, "reason": self.reason,
                "not_before": self.not_before, "evidence": list(self.evidence),
                "available": self.available}


class ProviderHealthSentinel:
    """Correlates provider limits across the runs of one root, for a consumer
    that is about to put a parked run back to work.

    A run parked on a limit keeps its own stated reset. When another run of the
    **same provider, account and model** states a later reset that is still
    pending, the account is limited until then, and putting this run back to
    work earlier only spends a turn on the same limit. `consult` says so: HOLD
    until the latest such reset, so that the runs of one account wake together.

    What it does and does not do:

    * **It never makes a run go.** `PROCEED` means "nothing here says wait", not
      "resume": the consumer's own refusals (a hold, a binding, the attempt
      limit) come first and stay its own.
    * **Evidence is verified.** A wait counts only if `dispatchrecovery.
      verify_binding` says its park is still the park its receipt describes
      (BOUND): a receipt is integrity, not authenticity (Q4 of the first
      increment).
    * **Governed runs count only under a live master.** A run that declares a
      governed master is consulted like any other (the caller has already judged
      its own park). As *evidence* it counts only if its park is `DEFERRED` to a
      master that is live **and current**: the declared session holds the
      project's current epoch and its lease is live and names it
      (`stale_master_reason` is None). A park of a superseded master, of a
      released, lost or expired lease, of a master the project never held, or of a
      record that does not say, is no evidence.
    * **A neighbour is looked at before it is opened.** Verifying another run's
      park opens files the resume loop never opened for a run that is not due; a
      FIFO there would block the unattended pass for ever. Every such file is
      `lstat`ed first (`_unsafe_to_verify`); anything but a regular single-name
      file within bound makes that neighbour no evidence.
    * **Only a limit, only pending, only this model.** An availability failure,
      an elapsed or unreadable reset, a park the resume loop gave up on, an
      unattributed park and another model's limit are not evidence; a reset
      beyond `MAX_HOLD` is not either.
    * **Fail open.** It writes nothing, takes no lock, and a reading that fails
      is `PROCEED` with `available=False` and the reason: the consumer then does
      what it did before this module existed.
    * **One reading per sentinel.** The runs are read once, at the first
      `consult`; a consumer that wants a newer reading builds a new sentinel.
    """

    def __init__(self, root: Path | str, *, now: datetime | None = None) -> None:
        if now is None:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        self._root = Path(root).expanduser()
        self._now = now
        self._waits: list[Wait] | None = None
        self._verified: dict[str, bool] = {}

    def consult(self, run_id: str) -> Consultation:
        """The sentinel's word about putting `run_id` back to work now."""
        try:
            return self._consult(run_id)
        except Exception as exc:                # noqa: BLE001 - fail open, said
            return Consultation(
                Action.PROCEED,
                f"provider health could not be read ({type(exc).__name__}); "
                "no cross-run opinion", available=False)

    def _consult(self, run_id: str) -> Consultation:
        if self._waits is None:
            self._waits = _collect(self._root, self._now)[0]
        mine = next((w for w in self._waits if w.run_id == run_id), None)
        if mine is None or mine.unattributed or mine.given_up:
            return Consultation(Action.PROCEED, "no cross-run evidence about this "
                                "run's provider")
        if _is_governed(RunStore(self._root, run_id).dir) is None:
            return Consultation(Action.PROCEED, "the run's master record does not say "
                                "whether it is governed; not consulted", available=False)
        key = (mine.provider, mine.account_handle, mine.model)
        ceiling = self._now + MAX_HOLD
        pending = [
            w for w in self._waits
            if w.run_id != run_id and not w.unattributed and not w.given_up
            and w.evidence is Evidence.LIMIT and not w.due and w.when is not None
            and w.when <= ceiling
            and (w.provider, w.account_handle, w.model) == key]
        proven = [w for w in sorted(pending, key=lambda w: w.run_id) if self._binds(w)]
        if not proven:
            return Consultation(Action.PROCEED, "no verified pending limit of this "
                                "provider, account and model")
        until = max(w.when for w in proven)
        names = tuple(w.run_id for w in proven)
        return Consultation(
            Action.HOLD,
            f"{len(names)} other run(s) of {mine.provider!r}/{mine.account_handle!r}/"
            f"{mine.model!r} state a verified limit that resets at {until.isoformat()}; "
            "the runs of one account wake together",
            not_before=until.isoformat(), evidence=names)

    def _binds(self, wait: Wait) -> bool:
        """Is this wait's park safe to look at and still the park its receipt
        describes -- for a plain run BOUND, for a governed one DEFERRED to a live,
        current master? Memoised. Any failure is "no evidence"."""
        if wait.run_id not in self._verified:
            self._verified[wait.run_id] = self._verify(wait)
        return self._verified[wait.run_id]

    def _verify(self, wait: Wait) -> bool:
        run_dir = RunStore(self._root, wait.run_id).dir
        try:
            governed = _is_governed(run_dir)
            if governed is None or _unsafe_to_verify(run_dir) is not None:
                return False
            if governed and _unsafe_projects(self._root) is not None:
                return False
            moment = self._now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            check = verify_binding(run_dir, wait.state, now=moment)
            if not governed:
                return check.verdict is ParkVerdict.BOUND
            return (check.verdict is ParkVerdict.DEFERRED
                    and stale_master_reason(run_dir, check.master, now=moment) is None)
        except Exception:                       # noqa: BLE001 - no evidence, never a raise
            return False
