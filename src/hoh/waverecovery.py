"""The outcome of a wave, and the bounded recovery of the holds a dead controller left
(V3.3 P3-22 slice 3, with the persisted degradation reason of P3-24).

Two things live here.

**The outcome** (`<project>/wave-outcomes/wave-<wave id>.json`): the controller writes
it *before* the executor starts, naming the holds it adopted and who it is (session,
epoch, pid, process identity), and closes it when the wave ends, with the executor's own
degradation reason (`WaveRecord.degradation`). A controller that dies anywhere between
those two writes leaves an OPEN outcome: the only name some hold has on disk.

**The recovery** (`recover_open`): the next drive reads every OPEN outcome and, for each,
ends the holds its dead controller left -- authorised, owner-bound, claimed by
compare-and-swap, proven idle, ended by reservation id. The operator's directive of
2026-10-14 is the shape of this module: *every path terminates in a state a reader sees*.
An attempt is counted **before** it is made, a wait for a live owner is counted before it
is made, each count is bounded (`MAX_ATTEMPTS`, `MAX_WAITS`), a file that is not an
outcome is set aside once (never removed), and a path that cannot be bounded because its
count cannot be written is reported as UNBOUNDED -- the controller then halts by name.

Nothing here removes or rewrites a terminal outcome: CLOSED, RECOVERED, NOT_APPLICABLE
and ABANDONED are evidence and are skipped without a write or a probe.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from . import durable_io
from .contracts import utcnow
from .reservation import BudgetReservations, HoldStatus, ReservationError, check_run_id
from .store import StaleWrite, StoreError
from .succession import StaleMaster, boot_id, pid_alive, pid_start
from .wave import (
    NodeState,
    ParallelismDegradationReason,
    Phase,
    WaveRecord,
    WaveRecordError,
    read_record,
    settle_wave,
)

#: Revision 2 of the outcome adds `cuts` (P3-24 slice 4: the controller's own cuts, per node).
#: `/1` files -- written by slice 3 -- are still read (no cuts); every write is `/2`.
SCHEMA_V1 = "hoh-wave-outcome/1"
SCHEMA = "hoh-wave-outcome/2"

#: Directory of the outcomes, a child of the project directory (never `waves/`: the wave
#: record's schema is pinned and `waves/` is listed by tests and tools, one file per wave).
OUTCOME_DIR = "wave-outcomes"

#: Attempts a recovery may make at one outcome; each is written before it is made.
MAX_ATTEMPTS = 3
#: Drives that may wait for a live owner process; each is written before it is made.
MAX_WAITS = 3
#: OPEN outcomes looked at per drive, oldest first.
MAX_PER_DRIVE = 16
#: What is read of an outcome file (and the most a valid one can be).
MAX_OUTCOME_BYTES = 1 << 20
MAX_HOLDS = 256
MAX_REASONS = 16
#: Cuts one outcome names, and the longest detail of one.
MAX_CUTS = 64
MAX_CUT_DETAIL = 200
#: The outcome of the latest round that launched no wave (a halt before the executor, or every node
#: cut): one file, replaced only when its content changes -- never one file per drive.
ROUND_ID = "latest-cuts"
MAX_NOTE = 600

_WAVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_NODE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RESERVATION_ID = re.compile(r"[A-Za-z0-9._-]{1,300}")
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_IDENTITY = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_FILE = re.compile(r"wave-([A-Za-z0-9][A-Za-z0-9._-]{0,63})\.json")

_KEYS = frozenset({"schema", "wave_id", "phase", "session", "epoch", "pid", "pid_start",
                   "holds", "degradation", "attempts", "waits", "note", "created_at",
                   "closed_at"})
_HOLD_KEYS = frozenset({"node_id", "run_id", "reservation_id", "amount"})
_KEYS_V2 = _KEYS | {"cuts"}
_DEGRADATION_KEYS = frozenset({"ready", "admitted", "reasons"})
_CUT_KEYS = frozenset({"node_id", "reason", "detail"})
class CutReason(StrEnum):
    """Why the **controller** left a node out of a round before the executor was asked
    (P3-24 slice 4). The executor's own reasons are `ParallelismDegradationReason` (its
    vocabulary is pinned by P3-02's tests); a cut that has a reason there (EXTERNAL, BUDGET,
    WIDTH) uses that value, and these are the ones it has not: CAPACITY (the resource
    scheduler did not admit the node), BACKPRESSURE (the provider's pressure narrowed the
    wave), IN_FLIGHT (a live process executes its run), BLOCKED (`prepare` found an obstacle
    and the node was blocked)."""

    CAPACITY = "CAPACITY"
    BACKPRESSURE = "BACKPRESSURE"
    IN_FLIGHT = "IN_FLIGHT"
    BLOCKED = "BLOCKED"


#: The outcome's vocabulary, in its order: the executor's reasons, then the controller's.
_REASON_ORDER = {r.value: i for i, r in enumerate([*ParallelismDegradationReason, *CutReason])}


class OutcomeError(Exception):
    """A file that is not exactly an outcome, or an outcome that cannot be written."""


class OutcomePhase(StrEnum):
    OPEN = "OPEN"
    #: The controller wrote it when the wave ended.
    CLOSED = "CLOSED"
    #: A recovery ended the holds the controller left.
    RECOVERED = "RECOVERED"
    #: A recovery found nothing of its own to do (no holds, or every one already ended or
    #: past READY).
    NOT_APPLICABLE = "NOT_APPLICABLE"
    #: Terminal and never attempted again: a person's (or the double-down's) to read.
    ABANDONED = "ABANDONED"


_TERMINAL = frozenset(OutcomePhase) - {OutcomePhase.OPEN}


@dataclass(frozen=True)
class AdoptedHold:
    node_id: str
    run_id: str
    reservation_id: str
    amount: int


@dataclass(frozen=True)
class Cut:
    """A node the controller left out of a round before the executor was asked, and why."""

    node_id: str
    #: A `ParallelismDegradationReason` or `CutReason` value.
    reason: str
    detail: str


@dataclass(frozen=True)
class Outcome:
    wave_id: str
    phase: OutcomePhase
    session: str | None
    epoch: int | None
    pid: int
    pid_start: str | None
    holds: tuple[AdoptedHold, ...]
    #: `(ready, admitted, reasons)` -- `WaveRecord.degradation` of the finished record --
    #: or None when the record derives none. The executor's own reason only.
    degradation: tuple[int, int, tuple[str, ...]] | None
    attempts: int
    waits: int
    note: str
    created_at: str
    closed_at: str | None
    #: The controller's cuts of the round (P3-24 slice 4); `()` for a `/1` file.
    cuts: tuple[Cut, ...] = ()

    def to_bytes(self) -> bytes:
        body = {
            "schema": SCHEMA, "wave_id": self.wave_id, "phase": str(self.phase),
            "session": self.session, "epoch": self.epoch, "pid": self.pid,
            "pid_start": self.pid_start,
            "holds": [{"node_id": h.node_id, "run_id": h.run_id,
                       "reservation_id": h.reservation_id, "amount": h.amount}
                      for h in self.holds],
            "degradation": (None if self.degradation is None else
                            {"ready": self.degradation[0], "admitted": self.degradation[1],
                             "reasons": list(self.degradation[2])}),
            "attempts": self.attempts, "waits": self.waits, "note": self.note,
            "created_at": self.created_at, "closed_at": self.closed_at,
            "cuts": [{"node_id": c.node_id, "reason": c.reason, "detail": c.detail}
                     for c in self.cuts],
        }
        data = (json.dumps(body, indent=2, ensure_ascii=True) + "\n").encode("ascii")
        if len(data) > MAX_OUTCOME_BYTES:
            raise OutcomeError(f"the outcome of wave {self.wave_id} would be {len(data)} bytes, "
                               f"above {MAX_OUTCOME_BYTES}")
        return data


# --------------------------------------------------------------------------- #
# process identity
# --------------------------------------------------------------------------- #

def process_identity(pid: int) -> str | None:
    """`<boot id>:<start time>` of `pid`, or None where `/proc` cannot say.

    A pid alone is never proof of who a process is: a pid reused after its owner died has
    another start time (and a pid of an earlier boot another boot id)."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    boot, start = boot_id(), pid_start(pid)
    if not boot or not start:
        return None
    token = f"{boot}:{start}"
    return token if _IDENTITY.fullmatch(token) else None


def _owner_alive(outcome: Outcome) -> bool:
    """A *different* process that wrote the outcome still exists and is the one recorded.

    Where the identity is unknown (not recorded, or `/proc` cannot say) the pid alone is
    taken as alive -- bounded by `MAX_WAITS` like every wait."""
    pid = outcome.pid
    if pid == os.getpid() or not pid_alive(pid):
        return False
    if outcome.pid_start is None:
        return True
    now = process_identity(pid)
    return True if now is None else now == outcome.pid_start


# --------------------------------------------------------------------------- #
# the file: strict, fail closed
# --------------------------------------------------------------------------- #

def _no_duplicates(pairs):
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"the key {key!r} is repeated")
        seen[key] = value
    return seen


def _no_constant(name: str):
    raise ValueError(f"{name} is not a number an outcome holds")


def _plain(value: object) -> bool:
    return isinstance(value, str) and all(" " <= c <= "~" for c in value)


def _whole(value: object, lo: int, hi: int | None = None) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool) and value >= lo
            and (hi is None or value <= hi))


def _text(doc: Mapping, key: str, pattern: re.Pattern, *, null: bool = False) -> str | None:
    value = doc[key]
    if value is None and null:
        return None
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise OutcomeError(f"{key} is not what an outcome holds: {str(value)[:40]!r}")
    return value


def _time(doc: Mapping, key: str, *, null: bool = False) -> str | None:
    value = _text(doc, key, _TIME, null=null)
    if value is not None:
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise OutcomeError(f"{key} is not a real time: {value!r}") from exc
    return value


def parse(data: bytes) -> Outcome:
    """The outcome these bytes are, or `OutcomeError` saying why not."""
    if not isinstance(data, (bytes, bytearray)):
        raise OutcomeError("an outcome is bytes")
    if len(data) > MAX_OUTCOME_BYTES:
        raise OutcomeError(f"{len(data)} bytes, above the {MAX_OUTCOME_BYTES} of an outcome")
    try:
        text = bytes(data).decode("ascii")
        doc = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise OutcomeError(f"not an outcome: {exc}"[:200]) from exc
    if not isinstance(doc, dict) or doc.get("schema") not in (SCHEMA_V1, SCHEMA):
        raise OutcomeError(f"schema is {str(doc.get('schema') if isinstance(doc, dict) else doc)[:40]!r},"
                           f" not {SCHEMA!r} or {SCHEMA_V1!r}")
    if set(doc) != (_KEYS_V2 if doc["schema"] == SCHEMA else _KEYS):
        raise OutcomeError("not exactly the keys of an outcome")
    wave_id = _text(doc, "wave_id", _WAVE_ID)
    try:
        phase = OutcomePhase(doc["phase"]) if isinstance(doc["phase"], str) else None
    except ValueError:
        phase = None
    if phase is None:
        raise OutcomeError(f"phase is not one of {[p.value for p in OutcomePhase]}")
    session = _text(doc, "session", _SESSION_ID, null=True)
    epoch = doc["epoch"]
    if epoch is not None and not _whole(epoch, 0):
        raise OutcomeError("epoch is a whole number or null")
    pid = doc["pid"]
    if not _whole(pid, 1):
        raise OutcomeError("pid is a positive whole number")
    identity = _text(doc, "pid_start", _IDENTITY, null=True)
    if not _whole(doc["attempts"], 0, MAX_ATTEMPTS):
        raise OutcomeError(f"attempts is a whole number from 0 to {MAX_ATTEMPTS}")
    if not _whole(doc["waits"], 0, MAX_WAITS):
        raise OutcomeError(f"waits is a whole number from 0 to {MAX_WAITS}")
    note = doc["note"]
    if not _plain(note) or len(note) > MAX_NOTE:
        raise OutcomeError("note is printable ASCII and bounded")
    created = _time(doc, "created_at")
    closed = _time(doc, "closed_at", null=True)
    if (closed is None) != (phase is OutcomePhase.OPEN):
        raise OutcomeError("closed_at is set exactly when the phase is not OPEN")

    raw_holds = doc["holds"]
    if not isinstance(raw_holds, list) or len(raw_holds) > MAX_HOLDS:
        raise OutcomeError(f"holds is a list of at most {MAX_HOLDS}")
    holds: list[AdoptedHold] = []
    for row in raw_holds:
        if not isinstance(row, dict) or set(row) != _HOLD_KEYS:
            raise OutcomeError("a hold is exactly node_id, run_id, reservation_id, amount")
        if not _whole(row["amount"], 1):
            raise OutcomeError("a hold's amount is a whole number of at least 1")
        try:
            run_id = check_run_id(row["run_id"])
        except ReservationError as exc:
            raise OutcomeError(str(exc)) from exc
        holds.append(AdoptedHold(_text(row, "node_id", _NODE_ID), run_id,
                                 _text(row, "reservation_id", _RESERVATION_ID), row["amount"]))
    if (len({h.node_id for h in holds}) != len(holds)
            or len({h.reservation_id for h in holds}) != len(holds)):
        raise OutcomeError("a node or a reservation id is named twice")

    raw = doc["degradation"]
    degradation = None
    if raw is not None:
        if not isinstance(raw, dict) or set(raw) != _DEGRADATION_KEYS:
            raise OutcomeError("degradation is exactly ready, admitted, reasons")
        reasons = raw["reasons"]
        if (not _whole(raw["ready"], 0) or not _whole(raw["admitted"], 0)
                or raw["admitted"] > raw["ready"]):
            raise OutcomeError("degradation counts are whole numbers, admitted <= ready")
        if (not isinstance(reasons, list) or not 1 <= len(reasons) <= MAX_REASONS
                or not all(isinstance(r, str) and r in _REASON_ORDER for r in reasons)):
            raise OutcomeError(f"degradation names 1 to {MAX_REASONS} reasons of the vocabulary")
        order = [_REASON_ORDER[r] for r in reasons]
        if order != sorted(set(order)):
            raise OutcomeError("degradation reasons are each once, in the vocabulary's order")
        degradation = (raw["ready"], raw["admitted"], tuple(reasons))
    cuts: list[Cut] = []
    if doc["schema"] == SCHEMA:
        raw_cuts = doc["cuts"]
        if not isinstance(raw_cuts, list) or len(raw_cuts) > MAX_CUTS:
            raise OutcomeError(f"cuts is a list of at most {MAX_CUTS}")
        for row in raw_cuts:
            if not isinstance(row, dict) or set(row) != _CUT_KEYS:
                raise OutcomeError("a cut is exactly node_id, reason, detail")
            reason = row["reason"]
            if not isinstance(reason, str) or reason not in _REASON_ORDER:
                raise OutcomeError("a cut's reason is one of the degradation vocabulary")
            detail = row["detail"]
            if not _plain(detail) or len(detail) > MAX_CUT_DETAIL:
                raise OutcomeError(f"a cut's detail is printable ASCII, at most {MAX_CUT_DETAIL}")
            cuts.append(Cut(_text(row, "node_id", _NODE_ID), reason, detail))
        if len({c.node_id for c in cuts}) != len(cuts):
            raise OutcomeError("a node is cut once in one outcome")
    return Outcome(wave_id=wave_id, phase=phase, session=session, epoch=epoch, pid=pid,
                   pid_start=identity, holds=tuple(holds), degradation=degradation,
                   attempts=doc["attempts"], waits=doc["waits"], note=note,
                   created_at=created, closed_at=closed, cuts=tuple(cuts))


def _read_bytes(path: Path) -> bytes | None:
    """The bytes of an outcome file; None when there is none; `OutcomeError` for a link, a
    FIFO, a directory, a second name, an oversize or an unreadable file."""
    try:
        kind = os.lstat(path)
        if stat.S_ISREG(kind.st_mode) and kind.st_size > MAX_OUTCOME_BYTES:
            raise OutcomeError(f"{path.name} is {kind.st_size} bytes; an outcome is at most "
                               f"{MAX_OUTCOME_BYTES}")
        return durable_io.read_single_link(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OutcomeError(f"{path.name} cannot be read: {exc}"[:300]) from exc


def read_outcome(path: Path | str) -> Outcome | None:
    """The outcome at `path`; None only when there is no such file. Anything else that is
    not exactly an outcome is `OutcomeError` -- never "no outcome"."""
    path = Path(path)
    data = _read_bytes(path)
    if data is None:
        return None
    outcome = parse(data)
    named = _FILE.fullmatch(path.name)
    if named is not None and named.group(1) != outcome.wave_id:
        raise OutcomeError(f"{path.name} holds the outcome of wave {outcome.wave_id}")
    return outcome


def outcome_path(directory: Path | str, wave_id: str) -> Path:
    if not isinstance(wave_id, str) or _WAVE_ID.fullmatch(wave_id) is None:
        raise OutcomeError(f"not a wave id: {str(wave_id)[:40]!r}")
    return Path(directory) / f"wave-{wave_id}.json"


def _ascii(value: object, limit: int = MAX_NOTE) -> str:
    return "".join(c if " " <= c <= "~" else "?" for c in str(value))[:limit]


def _write(path: Path, outcome: Outcome) -> bytes:
    """Replaces the file whole; returns the bytes written. A failure is `OutcomeError`."""
    data = outcome.to_bytes()
    try:
        durable_io.durable_write(path, data, mode=0o600)
    except OSError as exc:
        raise OutcomeError(f"{path.name} could not be written: {exc}"[:300]) from exc
    return data


# --------------------------------------------------------------------------- #
# the controller's two writes
# --------------------------------------------------------------------------- #

def open_outcome(directory: Path | str, wave_id: str, *, session: str | None,
                 epoch: int | None, holds: Sequence[AdoptedHold] = (),
                 pid: int | None = None, pid_start_token: str | None = None,
                 cuts: Sequence[Cut] = ()) -> Path:
    """Writes the OPEN outcome of a wave **before** its executor starts. Never replaces an
    existing file. Returns its path."""
    directory = Path(directory)
    path = outcome_path(directory, wave_id)
    pid = os.getpid() if pid is None else pid
    outcome = Outcome(
        wave_id=wave_id, phase=OutcomePhase.OPEN, session=session, epoch=epoch, pid=pid,
        pid_start=process_identity(pid) if pid_start_token is None else pid_start_token,
        holds=tuple(holds), degradation=None, attempts=0, waits=0, note="",
        created_at=utcnow(), closed_at=None, cuts=tuple(cuts)[:MAX_CUTS])
    data = outcome.to_bytes()
    parse(data)                                     # what is written is what is read back
    try:
        directory.mkdir(exist_ok=True)
        durable_io.durable_create(path, data, mode=0o600)
    except OSError as exc:
        raise OutcomeError(f"the outcome of wave {wave_id} could not be opened: {exc}"[:300]) from exc
    return path


def _degradation_of(record: WaveRecord) -> tuple[int, int, tuple[str, ...]] | None:
    found = record.degradation
    if found is None:
        return None
    return (found.ready, found.admitted, tuple(r.value for r in found.reasons))


def _round_degradation(record: WaveRecord, cuts: Sequence[Cut], ready: int | None,
                       extra: Sequence[str]) -> tuple[int, int, tuple[str, ...]] | None:
    """Why the round ran narrower than the plan it was made from: the executor's own reasons
    (`WaveRecord.degradation`) and the controller's cuts and width, over the nodes the planner saw
    (`ready`; the record's own count when the caller does not say). None when every one ran."""
    admitted = sum(1 for n in record.nodes if n.state is not NodeState.REFUSED)
    own = record.degradation
    ready_total = max(ready or 0, len(record.nodes) + len(record.deferred))
    reasons = {r.value for r in own.reasons} if own is not None else set()
    reasons |= {c.reason for c in cuts} | set(extra)
    if admitted >= ready_total and not reasons:
        return None
    if not reasons:
        return None
    return (ready_total, admitted, tuple(r for r in _REASON_ORDER if r in reasons))


def close_outcome(path: Path | str, record: WaveRecord, *, note: str = "",
                  ready: int | None = None, extra_reasons: Sequence[str] = ()) -> Outcome:
    """Closes the OPEN outcome at `path` when the wave ends, storing the degradation reason
    its record derives. An outcome that is no longer OPEN (a recovery abandoned it: the
    owner was waited for and could not be confirmed dead) is `OutcomeError` and is left as
    it is -- the caller reports it, it is not a halt."""
    path = Path(path)
    current = read_outcome(path)
    if current is None:
        raise OutcomeError(f"{path.name} does not exist: there is no outcome to close")
    if current.phase is not OutcomePhase.OPEN:
        raise OutcomeError(f"{path.name} is {current.phase}, not OPEN: it was not closed by "
                           "its controller")
    if record.wave_id != current.wave_id:
        raise OutcomeError(f"the record is of wave {record.wave_id}, the outcome of "
                           f"{current.wave_id}")
    if ready is None and not current.cuts and not extra_reasons:
        degradation = _degradation_of(record)           # slice 3: the executor's reason, as it was
    else:
        degradation = _round_degradation(record, current.cuts, ready, extra_reasons)
    closed = replace(current, phase=OutcomePhase.CLOSED, degradation=degradation,
                     note=_ascii(note), closed_at=utcnow())
    _write(path, closed)
    return closed


def record_round(directory: Path | str, *, session: str | None, epoch: int | None,
                 ready: int, cuts: Sequence[Cut], extra_reasons: Sequence[str] = (),
                 pid: int | None = None, pid_start_token: str | None = None) -> bool:
    """The outcome of a round that launched **no wave** (a halt before the executor, or every
    node cut): `<directory>/wave-latest-cuts.json`, CLOSED, no holds, `admitted` 0, the cuts
    and their reasons. One file for the project, replaced only when its content (the counts,
    the reasons and the (node, reason) pairs of the cuts) differs from what is there -- a project that waits on capacity
    and is driven again every minute writes it once, not once per drive. Returns whether it
    wrote. A failure is `OutcomeError`; the caller reports it, it is not a halt."""
    cuts = tuple(cuts)[:MAX_CUTS]
    reasons = {c.reason for c in cuts} | set(extra_reasons)
    if not reasons:
        return False
    directory = Path(directory)
    path = outcome_path(directory, ROUND_ID)
    degradation = (max(ready, len(cuts)), 0, tuple(r for r in _REASON_ORDER if r in reasons))
    try:
        current = read_outcome(path)
    except OutcomeError:
        current = None                                  # unreadable: replaced (the file is a note, not a ledger)
    # the same round, said again: the counts, the reasons and the (node, reason) pairs. The
    # detail is **not** compared: it quotes figures that move with the clock ("stated 864000 s
    # ago"), and a file rewritten because a second passed is a file rewritten on every drive.
    if (current is not None and current.phase is OutcomePhase.CLOSED
            and current.degradation == degradation
            and [(c.node_id, c.reason) for c in current.cuts] == [(c.node_id, c.reason) for c in cuts]):
        return False
    pid = os.getpid() if pid is None else pid
    now = utcnow()
    outcome = Outcome(
        wave_id=ROUND_ID, phase=OutcomePhase.CLOSED, session=session, epoch=epoch, pid=pid,
        pid_start=process_identity(pid) if pid_start_token is None else pid_start_token,
        holds=(), degradation=degradation, attempts=0, waits=0,
        note="the latest round that launched no wave; later waves have outcomes of their own",
        created_at=now, closed_at=now, cuts=cuts)
    try:
        directory.mkdir(exist_ok=True)
    except OSError as exc:
        raise OutcomeError(f"{directory.name} cannot be made: {exc}"[:300]) from exc
    parse(outcome.to_bytes())
    _write(path, outcome)
    return True


# --------------------------------------------------------------------------- #
# the recovery
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Event:
    """What a drive says of one outcome. `kind` is RECOVERED, SETTLED, LEFT, ABANDONED,
    UNREADABLE, UNBOUNDED, or -- the capacity rows of the gone controller (P3-04 C4b) --
    CAPACITY_SETTLED / CAPACITY_UNSETTLED; `wave_id` becomes the node of the step."""

    kind: str
    wave_id: str
    detail: str


@dataclass
class Report:
    events: list[Event] = field(default_factory=list)
    #: Set when a path could not be bounded: the controller halts AMBIGUOUS by name.
    unbounded: Event | None = None


class _Unbounded(Exception):
    """A count, an abandonment or a set-aside that cannot be written: the path is not
    bounded, so the drive says so and stops."""


class _Moved(Exception):
    """The file changed between the read and the replace: it is left alone."""


@dataclass
class _Context:
    directory: Path
    waves_dir: Path
    authorise: Callable[[], None]
    session: str | None
    epoch: int | None
    claim: Callable[[], object]
    probe: Callable[[object], str | None]
    reservations: BudgetReservations | None
    spent: Callable[[], Mapping[str, int]] | None
    #: `capacity(outcome, reason)` ends the capacity rows the gone controller left and returns how many (P3-04 C4b);
    #: None where no runs root is known. It raises where the ledger cannot be used.
    capacity: Callable[[Outcome, str], int] | None = None


def _natural(name: str):
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"([0-9]+)", name) if p]


def _quarantine(path: Path) -> Path:
    """Renames a file that is not an outcome to `<name>.quarantined.<n>` (kept; nothing is
    removed) and returns the new name."""
    n = 1
    while os.path.lexists(f"{path}.quarantined.{n}"):
        n += 1
    target = Path(f"{path}.quarantined.{n}")
    os.rename(path, target)
    return target


def recover_open(
    directory: Path | str, *, authorise: Callable[[], None], session: str | None,
    epoch: int | None, claim: Callable[[], object],
    probe: Callable[[object], str | None],
    reservations: BudgetReservations | None,
    spent: Callable[[], Mapping[str, int]] | None,
    waves_dir: Path | str | None = None,
    capacity: Callable[[Outcome, str], int] | None = None,
) -> Report:
    """One drive's recovery of the OPEN outcomes in `directory`.

    `authorise()` raises for a session that is not the master (it propagates). `claim()`
    reads the persisted project state and writes it back -- the compare-and-swap -- and
    returns the claimed state; a lost race is a `StaleWrite`. `probe(node)` is the idleness
    proof: None when the node's run is idle, else why not. `reservations` / `spent` end
    and measure holds (None when no shared budget is configured).

    Only OPEN outcomes (and files that are not outcomes) are looked at; at most
    `MAX_PER_DRIVE`, oldest first by number. Every path ends in an event a reader sees; a
    path that cannot be bounded sets `Report.unbounded` and stops the pass. Never raises
    for an outcome's own failure; `StaleMaster` and `BaseException` propagate."""
    directory = Path(directory)
    report = Report()
    ctx = _Context(directory, Path(waves_dir) if waves_dir is not None
                   else directory.parent / "waves", authorise, session, epoch, claim, probe,
                   reservations, spent, capacity)
    try:
        names = [n for n in os.listdir(directory) if _FILE.fullmatch(n)]
    except FileNotFoundError:
        return report
    except OSError as exc:
        report.unbounded = Event("UNBOUNDED", "-", f"the outcome directory cannot be listed: {_ascii(exc, 200)}")
        return report

    candidates: list[tuple[str, Path, bytes | None, Outcome | None, str]] = []
    for name in names:
        path = directory / name
        try:
            raw = _read_bytes(path)
            outcome = None if raw is None else read_outcome(path)
        except OutcomeError as exc:
            candidates.append((name, path, None, None, _ascii(exc, 200)))
            continue
        if outcome is not None and outcome.phase is OutcomePhase.OPEN:
            candidates.append((name, path, raw, outcome, ""))
    candidates.sort(key=lambda c: _natural(c[0]))

    for name, path, raw, outcome, why in candidates[:MAX_PER_DRIVE]:
        wave_id = _FILE.fullmatch(name).group(1)
        try:
            if outcome is None:
                events = _set_aside(ctx, path, wave_id, why)
            else:
                events = _recover_one(ctx, path, raw, outcome)
        except StaleMaster:
            raise
        except _Unbounded as exc:
            report.unbounded = Event("UNBOUNDED", wave_id, _ascii(exc, 300))
            return report
        except _Moved:
            continue
        except Exception as exc:                     # noqa: BLE001 - an unforeseen path is still named
            report.unbounded = Event(
                "UNBOUNDED", wave_id,
                f"the recovery of this outcome failed unexpectedly: {_ascii(type(exc).__name__ + ': ' + str(exc), 250)}")
            return report
        report.events.extend(events)
    return report


def _put(path: Path, expected: bytes, outcome: Outcome) -> bytes:
    """Replaces the file with `outcome` if it still holds `expected`; returns the new bytes.
    A file that moved is `_Moved` (left alone); a failing write is `OutcomeError`."""
    try:
        now = _read_bytes(path)
    except OutcomeError as exc:
        raise _Moved() from exc
    if now != expected:
        raise _Moved()
    return _write(path, outcome)


def _set_aside(ctx: _Context, path: Path, wave_id: str, why: str) -> list[Event]:
    ctx.authorise()                                  # before anything is renamed or replaced
    try:
        kept = _quarantine(path)
    except OSError as exc:
        raise _Unbounded(f"the file {path.name} is not an outcome ({why}) and cannot be set "
                         f"aside: {_ascii(exc, 150)}") from exc
    replacement = Outcome(
        wave_id=wave_id, phase=OutcomePhase.ABANDONED, session=ctx.session, epoch=ctx.epoch,
        pid=os.getpid(), pid_start=process_identity(os.getpid()), holds=(), degradation=None,
        attempts=0, waits=0,
        note=_ascii(f"the file was not an outcome ({why}); kept as {kept.name}"),
        created_at=utcnow(), closed_at=utcnow())
    try:
        durable_io.durable_create(path, replacement.to_bytes(), mode=0o600)
    except OSError as exc:
        raise _Unbounded(f"{path.name} was set aside as {kept.name} but its replacement cannot "
                         f"be written: {_ascii(exc, 150)}") from exc
    return [Event("UNREADABLE", wave_id, f"{path.name} is not an outcome ({why}); kept as {kept.name}")]


def _recover_one(ctx: _Context, path: Path, raw: bytes, outcome: Outcome) -> list[Event]:
    wave_id = outcome.wave_id
    ctx.authorise()                                                          # 1

    def put(new: Outcome, what: str) -> bytes:
        try:
            return _put(path, raw_now[0], new)
        except OutcomeError as exc:
            raise _Unbounded(f"{what} cannot be written: {_ascii(exc, 200)}") from exc

    raw_now = [raw]

    def abandon(note: str) -> list[Event]:                                   # terminal
        done = replace(current[0], phase=OutcomePhase.ABANDONED, note=_ascii(note),
                       closed_at=utcnow())
        raw_now[0] = put(done, f"the abandonment of wave {wave_id}")
        return [Event("ABANDONED", wave_id, _ascii(note, 300))]

    current = [outcome]

    if outcome.attempts >= MAX_ATTEMPTS:                                     # 2
        return abandon(f"{MAX_ATTEMPTS} attempts were made and none ended its holds: a "
                       "person's decision (a width-1 drive ends what is left)")

    if _owner_alive(outcome):                                                # 3
        if outcome.waits >= MAX_WAITS:
            return abandon(f"the process that opened this outcome (pid {outcome.pid}) is alive "
                           f"and could not be confirmed dead after {MAX_WAITS} waits: a "
                           "person's decision")
        waited = replace(outcome, waits=outcome.waits + 1,
                         note=_ascii(f"waiting for the live owner (pid {outcome.pid}): wait "
                                     f"{outcome.waits + 1} of {MAX_WAITS}"))
        raw_now[0] = put(waited, f"the wait count of wave {wave_id}")
        return [Event("LEFT", wave_id, f"the process that opened this outcome (pid "
                      f"{outcome.pid}) is alive: wait {waited.waits} of {MAX_WAITS}")]

    counted = replace(outcome, attempts=outcome.attempts + 1)                # 4: before the attempt
    raw_now[0] = put(counted, f"the attempt count of wave {wave_id}")
    current[0] = counted
    n = counted.attempts

    def refuse(why: str) -> list[Event]:                                     # OPEN, with a note
        why = _ascii(why, 400)
        try:
            raw_now[0] = _put(path, raw_now[0], replace(current[0], note=why))
        except (OutcomeError, _Moved):
            pass                                  # the count is on disk; the note is a courtesy
        return [Event("LEFT", wave_id, f"attempt {n} of {MAX_ATTEMPTS}: {why}")]

    def finish(phase: OutcomePhase, note: str, events: list[Event]) -> list[Event]:
        # P3-04 C4b: the capacity rows of the gone controller are ended with its holds, once, after the budget's
        # decision and never instead of it: a ledger that cannot be used is named in the outcome and in an event,
        # and does not keep the budget's holds waiting (the rows end by their owner's death or the ttl anyway).
        # Idempotent: a retry after a failed final write finds none.
        if ctx.capacity is not None:
            try:
                n = ctx.capacity(outcome, f"wave {wave_id}: recovered after its controller left (pid {outcome.pid})")
            except Exception as exc:                  # noqa: BLE001 - named, not hidden
                why = _ascii(f"{type(exc).__name__}: {exc}", 200)
                note = f"{note}; its capacity rows were NOT settled ({why})"
                events = events + [Event("CAPACITY_UNSETTLED", wave_id, why)]
            else:
                if n:
                    note = f"{note}; settled {n} capacity row(s)"
                    events = events + [Event("CAPACITY_SETTLED", wave_id, f"ended {n} capacity row(s) of the controller that is gone")]
        done = replace(current[0], phase=phase, note=_ascii(note), closed_at=utcnow())
        try:
            raw_now[0] = _put(path, raw_now[0], done)
        except OutcomeError as exc:                # guarded: the outcome stays OPEN, counted
            return events + [Event("LEFT", wave_id, f"attempt {n} of {MAX_ATTEMPTS}: the final "
                                   f"write failed ({_ascii(exc, 150)}); the attempt is counted "
                                   "and the next drive reads the holds again")]
        except _Moved:
            return events
        return events

    # 5: owner binding
    if outcome.epoch is None:
        bound = ctx.epoch is None
    else:
        bound = ctx.epoch is not None and outcome.epoch <= ctx.epoch
    if not bound:
        mine = "unfenced" if ctx.epoch is None else f"epoch {ctx.epoch}"
        theirs = "no epoch (an unfenced controller)" if outcome.epoch is None else f"epoch {outcome.epoch}"
        return refuse(f"the outcome was written under {theirs}; this master is {mine}")

    # 6: what the outcome and the wave record say
    if not outcome.holds:
        return finish(OutcomePhase.NOT_APPLICABLE, "the wave adopted no holds", [])
    try:
        record = read_record(ctx.waves_dir / f"wave-{wave_id}.json")
    except (WaveRecordError, OSError) as exc:
        return refuse(f"the wave record is not readable: {_ascii(exc, 200)}")
    if record is not None:
        named = {h.reservation_id for h in outcome.holds}
        extra = sorted({r.reservation_id for r in record.nodes if r.reservation_id} - named)
        if extra:
            return refuse(f"the wave record names a hold the outcome does not: {extra[0]}")
        if record.phase is Phase.SETTLED:
            return _settled(ctx, path, record, finish, refuse)

    # 7: claim
    try:
        state = ctx.claim()
    except StaleMaster:
        raise
    except (StaleWrite, StoreError) as exc:
        return refuse(f"the claim lost the race: another writer advanced the project: "
                      f"{_ascii(exc, 150)}")
    except Exception as exc:                          # noqa: BLE001 - refused, counted
        return refuse(f"the project state could not be claimed: {_ascii(type(exc).__name__ + ': ' + str(exc), 150)}")

    # 8: idleness, per adopted hold, in the claimed state
    idle: list[AdoptedHold] = []
    for hold in outcome.holds:
        node = state.node(hold.node_id)
        if node is None or str(getattr(node.lifecycle, "value", node.lifecycle)) != "READY":
            continue                                  # its resume path's: not probed
        try:
            busy = ctx.probe(node)
        except Exception as exc:                      # noqa: BLE001 - refused whole
            return refuse(f"the run of node {hold.node_id} could not be probed: "
                          f"{_ascii(type(exc).__name__ + ': ' + str(exc), 150)}")
        if busy is not None:
            return refuse(f"node {hold.node_id}: {busy}")
        idle.append(hold)

    # 9: end the holds by id
    ended: list[AdoptedHold] = []
    if idle:
        if ctx.reservations is None:
            return refuse("no shared budget is configured here: the holds cannot be ended")
        try:
            found = ctx.reservations.settle_holds(
                [h.reservation_id for h in idle],
                f"wave {wave_id}: recovered after its controller left the hold (pid {outcome.pid})",
                expect={h.reservation_id: (h.run_id, h.amount) for h in idle})
        except Exception as exc:                      # noqa: BLE001 - refused whole
            return refuse(f"the holds could not be ended: {_ascii(exc, 200)}")
        ended = [h for h in idle if found.get(h.reservation_id) is HoldStatus.HELD]

    # 10
    if ended:
        names = [h.node_id for h in ended]
        return finish(OutcomePhase.RECOVERED, f"ended the holds of {names}",
                      [Event("RECOVERED", wave_id, f"ended the holds of {names}, which a "
                             "controller that is gone left")])
    if idle:
        return finish(OutcomePhase.NOT_APPLICABLE,
                      f"already ended: the holds of {[h.node_id for h in idle]} were ended by "
                      "their controller", [])
    return finish(OutcomePhase.NOT_APPLICABLE,
                  "every node had moved past READY: its holds are the resume path's", [])


def _settled(ctx: _Context, path: Path, record: WaveRecord, finish, refuse) -> list[Event]:
    """The wave ended and its record says SETTLED: only its controller's write of the
    outcome was missing. Ends the leak the node's own settlement could not, by id."""
    if ctx.reservations is None or ctx.spent is None:
        return refuse("the wave record is SETTLED and no shared budget is configured here to "
                      "settle it against")
    try:
        settlement = settle_wave(ctx.waves_dir / f"wave-{record.wave_id}.json",
                                 ctx.reservations, ctx.spent)
    except Exception as exc:                          # noqa: BLE001 - refused, counted
        return refuse(f"the settled wave could not be settled: {_ascii(exc, 200)}")
    ended = [n.node_id for n in settlement.nodes if n.state == "ENDED"]
    events = ([Event("SETTLED", record.wave_id,
                     f"ended the hold of {ended}, which the launch could not end")]
              if ended else [])
    note = (f"the wave record is SETTLED; ended the hold of {ended}" if ended
            else "the wave record is SETTLED; no hold was open")
    return finish(OutcomePhase.CLOSED, note, events)


__all__ = ["MAX_ATTEMPTS", "MAX_CUTS", "MAX_NOTE", "MAX_OUTCOME_BYTES", "MAX_PER_DRIVE",
           "MAX_WAITS", "OUTCOME_DIR", "ROUND_ID", "SCHEMA", "SCHEMA_V1", "AdoptedHold", "Cut",
           "CutReason", "Event", "Outcome", "OutcomeError", "OutcomePhase", "Report", "close_outcome",
           "open_outcome", "outcome_path", "parse", "process_identity", "read_outcome",
           "record_round", "recover_open"]
