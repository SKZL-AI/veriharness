"""ParallelWaveExecutor and WaveRecord: run the independent READY nodes of one
wave together, each under its own budget hold and its own workspace lease
(V3.3 P3-02, slice 1).

The orchestrator takes one node per round. Three pieces now exist for taking
more: the frontier (`hoh.frontier`: which READY nodes may proceed side by
side), the reservation (`hoh.reservation`: a hold on the shared budget, taken
atomically) and the workspace lease (`hoh.wslease`: one writer per worktree).
This module is the step that puts them in one order and writes it down.

What one wave does, in this order, and why the order matters:

1. **The plan is checked, not trusted.** A `FrontierPlan` is an answer about
   the ready nodes, and a plan that was built by hand, or by a planner that was
   swapped in, can name two nodes that conflict. The selected nodes are planned
   again with the same rule (`ReadyFrontierPlanner`); anything the second pass
   would defer refuses the whole wave before a hold or a lease exists.
2. **Every node is admitted or refused, one at a time, in plan order.** A node
   is admitted only with *both* a workspace lease and a budget hold, taken in
   that order: the lease first, because `reserve` supersedes any open hold of
   the same run id, and a node that cannot have the worktree must not have
   touched a hold that another run (or an earlier wave) is living on. Not
   READY, not INTERNAL, past the width, no run id, a worktree somebody else
   holds, a run id that already holds budget, nothing left to hold: each is a
   refusal that is written down with its reason, and a lease taken for a node
   whose hold then failed is released again at once. A refused node never
   releases, supersedes or settles anything it did not take; it is not an error
   of the wave, the others go on.
3. **The record is on disk before the first launch.** `WaveRecord` phase
   ADMITTED is created (never replaced) with the plan, the deferred nodes and
   every node's admission. A wave whose record cannot be written launches
   nothing and gives everything back.
4. **The admitted nodes are launched together**, one thread each, and the
   caller's `launch(node, admission)` is the only thing that runs in them. A
   launch that raises, or returns something with no verdict, is that node's
   ERRORED row; it does not stop its siblings and it does not skip the cleanup.
5. **Everything is given back, whatever happened**: when a node's `launch`
   ends -- in that node's own thread, so the judgement is of *that* moment and
   not of the wave's end -- its lease is checked once (was it still this
   handle's when the run ended?), then released, and its hold is ended. Then
   the record is replaced with phase SETTLED. A node whose lease was lost
   during its run is reported `lease_held_at_end: false` and is not `usable`:
   a second writer may have been in that worktree.

Three things are added to that order (V3.3 P3-22/23/24, slice 1):

- **A QA reserve** (`WaveBudget.qa_reserve`, `is_qa`): the nodes that are not QA
  hold with `keep=qa_reserve`, so the last `qa_reserve` dispatches of the
  ceiling are never granted to them; a node that `is_qa` names holds with
  `keep=0` and may use them. A node refused for that reason is `QA_RESERVE`, not
  `BUDGET`.
- **A degradation reason** (`WaveRecord.degradation`): a wave that admitted fewer
  nodes than the plan was made from says why, in a fixed vocabulary, from the
  record alone -- the refusals and the deferrals it already carries.
- **Settlement of a finished wave** (`settle_wave`): reads the record back
  (strictly, and refuses one that is not SETTLED), ends by reservation id every
  hold the record names that is still open -- the ones a node's own settlement
  could not end -- and reports, per node, what the run used of its hold and what
  went back. It touches nothing else; it is the second chance, not a recovery.

What this module does not do, and the callers' business: it does not read or
change project state (the controller persists RUNNING and settles outcomes, as
it does for one node), does not create worktrees or runs, does not renew a lease
while a node runs (a run longer than its lease reads `lease_held_at_end:
false`), does not retry, and does not decide the width -- it only enforces the
one it is given. Nothing here is called by the orchestrator yet.

Fail closed. A plan, a run id, a lease handle, a ledger, a lease file or a
record directory that is not exactly what was expected is a refusal, never a
node that runs without the hold or the lease it should have had. A record file
that is not exactly what this module writes is `WaveRecordError`, never "no
wave".
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from . import durable_io
from .contracts import utcnow
from .frontier import FrontierPlan, ReadyFrontierPlanner
from .project import ActionClass, Lifecycle, TaskNode
from .reservation import BudgetReservations, check_run_id
from .wslease import WorkspaceLease

SCHEMA = "hoh-wave-record/1"

#: The most nodes one wave runs at once. A wave is a few nodes side by side; a
#: width above this is a number that came from a typo or a loop, and sixteen
#: threads each waiting on a provider is already more than one machine's
#: operator can follow.
MAX_WAVE_WIDTH = 16
#: The most nodes (selected) and deferrals one plan may carry into a record. A
#: plan above either is refused, not truncated: a record that silently left out
#: nodes is not the plan.
MAX_NODES = 256
MAX_DEFERRED = 1024
#: A record is written whole and read whole; a file above this is not ours.
MAX_RECORD_BYTES = 1 << 20

_WAVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_NODE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,255}")
_RESERVATION_ID = re.compile(r"[A-Za-z0-9._-]{1,300}")
_VERDICT = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_TEXT = re.compile(r"[\x20-\x7e]{0,300}")
_PATH = re.compile(r"[\x20-\x7e]{1,512}")
_DEFERRAL_REASON = re.compile(r"[A-Z_]{1,40}")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")

_TOP_KEYS = ("schema", "wave_id", "phase", "created_at", "settled_at", "width", "ceiling",
             "nodes", "deferred")
_NODE_KEYS = ("node_id", "run_id", "state", "refusal", "detail", "reservation_id", "amount",
              "worktree", "generation", "verdict", "lease_held_at_end", "lease_released",
              "holds_ended")
_DEFERRED_KEYS = ("node_id", "blocked_by", "reason")


class WaveError(RuntimeError):
    """A wave that is refused as a whole: nothing was held, leased or launched
    (or, for a record that could not be written, everything was given back)."""


class WaveRecordError(WaveError):
    """A wave record that is not exactly what this module writes."""


class WaveNotSettled(WaveError):
    """The nodes ran and everything was given back, but the final record write
    raised. The file says ADMITTED (the replace never happened) or SETTLED (the
    replace happened and only the sync of its directory failed): read its
    phase, never assume it. `.result` is the in-memory outcome; a file that
    says ADMITTED is the safe reading (outcome unknown)."""

    def __init__(self, message: str, result: "WaveResult") -> None:
        super().__init__(message)
        self.result = result


class Phase(StrEnum):
    ADMITTED = "ADMITTED"
    SETTLED = "SETTLED"


class NodeState(StrEnum):
    #: Never launched; `refusal` says why.
    REFUSED = "REFUSED"
    #: Held and leased, record written, not yet launched or not yet settled.
    ADMITTED = "ADMITTED"
    #: `launch` returned an outcome with a verdict. Not an acceptance: the
    #: verdict is the launcher's word and is in `verdict`.
    RETURNED = "RETURNED"
    #: `launch` raised, or returned something that is not an outcome.
    ERRORED = "ERRORED"


class Refusal(StrEnum):
    NOT_READY = "NOT_READY"
    EXTERNAL = "EXTERNAL"
    WIDTH = "WIDTH"
    RUN_ID = "RUN_ID"
    BUDGET = "BUDGET"
    WORKSPACE = "WORKSPACE"
    #: Budget was left, and all of it is the QA reserve this node may not use.
    QA_RESERVE = "QA_RESERVE"


class ParallelismDegradationReason(StrEnum):
    """Why a wave ran narrower than the plan it was made from. The values are the
    `Refusal` values (a refused node is its own reason) and `CONFLICT` (the plan
    deferred a ready node behind another)."""

    NOT_READY = "NOT_READY"
    EXTERNAL = "EXTERNAL"
    WIDTH = "WIDTH"
    RUN_ID = "RUN_ID"
    BUDGET = "BUDGET"
    QA_RESERVE = "QA_RESERVE"
    WORKSPACE = "WORKSPACE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class ParallelismDegradation:
    """`admitted` nodes ran of the `ready` the plan was made from, and `reasons`
    (each once, in the enum's order) is why not more."""

    ready: int
    admitted: int
    reasons: tuple[ParallelismDegradationReason, ...]


@dataclass(frozen=True)
class WaveNode:
    """One selected node of a wave and what became of it."""

    node_id: str
    run_id: str | None
    state: NodeState
    refusal: str = ""
    detail: str = ""
    reservation_id: str | None = None
    amount: int | None = None
    worktree: str | None = None
    generation: int | None = None
    verdict: str = ""
    lease_held_at_end: bool | None = None
    lease_released: bool | None = None
    holds_ended: int | None = None

    @property
    def usable(self) -> bool:
        """An outcome that may be acted on: a verdict came back **and** the
        node still held its worktree when it did."""
        return self.state is NodeState.RETURNED and self.lease_held_at_end is True


@dataclass(frozen=True)
class WaveDeferral:
    node_id: str
    blocked_by: str
    reason: str


@dataclass(frozen=True)
class WaveRecord:
    """The plan of one wave and its settlement. Replaced, never edited."""

    wave_id: str
    phase: Phase
    created_at: str
    settled_at: str | None
    width: int
    #: The shared dispatch budget the wave was admitted under; None when no
    #: shared budget is configured (then no node holds anything).
    ceiling: int | None
    nodes: tuple[WaveNode, ...]
    deferred: tuple[WaveDeferral, ...] = ()

    def node(self, node_id: str) -> WaveNode | None:
        return next((n for n in self.nodes if n.node_id == node_id), None)

    @property
    def usable_ids(self) -> tuple[str, ...]:
        return tuple(n.node_id for n in self.nodes if n.usable)

    @property
    def degradation(self) -> ParallelismDegradation | None:
        """None for a wave that admitted every node the plan was made from;
        otherwise how many ran and why not more. Read from the record alone: a
        refused node names its reason, a deferred node is a `CONFLICT`. A node
        that was admitted and then failed or lost its lease is not a narrower
        wave (its row says what happened)."""
        ready = {n.node_id for n in self.nodes} | {d.node_id for d in self.deferred}
        admitted = sum(1 for n in self.nodes if n.state is not NodeState.REFUSED)
        if admitted >= len(ready) and not self.deferred:
            return None
        found = {ParallelismDegradationReason(n.refusal) for n in self.nodes
                 if n.state is NodeState.REFUSED}
        if self.deferred:
            found.add(ParallelismDegradationReason.CONFLICT)
        return ParallelismDegradation(
            ready=len(ready), admitted=admitted,
            reasons=tuple(r for r in ParallelismDegradationReason if r in found))

    def to_bytes(self) -> bytes:
        body = {"schema": SCHEMA, **asdict(self)}
        body["phase"] = str(self.phase)
        body["nodes"] = [{**asdict(n), "state": str(n.state)} for n in self.nodes]
        body["deferred"] = [asdict(d) for d in self.deferred]
        return (json.dumps(body, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode("ascii")


@dataclass(frozen=True)
class WaveBudget:
    """How a wave holds the shared dispatch budget: one hold of `want` per
    admitted node, under `ceiling`, against what `spent` reports. A node that is
    not QA never takes the last `qa_reserve` of the ceiling (0: no reserve)."""

    reservations: BudgetReservations
    ceiling: int
    spent: Callable[[], Mapping[str, int]]
    want: int
    qa_reserve: int = 0
    #: True: the hold of a run id was taken by the one that prepared the run
    #: (the launcher, at `hoh start`, or `take_hold` for a resumed run). The
    #: executor then **adopts** it -- the row names it, nothing is reserved a
    #: second time and the executor never ends it (the launcher ends it when its
    #: dispatch returns, and keeps it for a run that is still executing) -- and a
    #: run with no open hold is admitted without one. The single-hold rule of
    #: P3-04 is then kept by construction instead of by supersession. A QA
    #: reserve is the reserving party's (`keep=`), so an adopting executor
    #: refuses a `qa_reserve`: it could not keep it.
    adopt: bool = False


@dataclass(frozen=True)
class Admission:
    """What a launched node was admitted with; the second argument of `launch`."""

    wave_id: str
    node: TaskNode
    run_id: str
    #: None: the node holds nothing (no shared budget, or a run that took no hold).
    reservation_id: str | None
    amount: int | None
    worktree: Path
    generation: int


@dataclass(frozen=True)
class WaveResult:
    record: WaveRecord
    #: The object `launch` returned, by node id, for the RETURNED nodes only.
    outcomes: Mapping[str, object]

    @property
    def degradation(self) -> ParallelismDegradation | None:
        return self.record.degradation


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


def _refuse_constant(name: str) -> None:
    raise ValueError(f"{name} is not a JSON number")


def _bad(what: str, value: object) -> WaveRecordError:
    return WaveRecordError(f"{what} is not an acceptable value: {value!r}"[:300])


def _stamp(value: object, what: str) -> str:
    if not isinstance(value, str) or _TIME.fullmatch(value) is None:
        raise _bad(what, value)
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise _bad(what, value) from exc
    return value


def _text(obj: Mapping, key: str, pattern: re.Pattern, *, null: bool = False) -> str | None:
    value = obj[key]
    if value is None and null:
        return None
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise _bad(key, value)
    return value


def _int(obj: Mapping, key: str, low: int, high: int, *, null: bool = False) -> int | None:
    value = obj[key]
    if value is None and null:
        return None
    # `bool` is an `int`; `True` is not a count.
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _bad(key, value)
    return value


def _bool(obj: Mapping, key: str) -> bool | None:
    value = obj[key]
    if value is not None and not isinstance(value, bool):
        raise _bad(key, value)
    return value


def _keys(obj: object, expected: tuple[str, ...], what: str) -> Mapping:
    if not isinstance(obj, dict) or set(obj) != set(expected):
        got = sorted(obj) if isinstance(obj, dict) else type(obj).__name__
        raise WaveRecordError(f"{what} has the fields {got}, not {sorted(expected)}"[:300])
    return obj


def _node(raw: object, phase: Phase) -> WaveNode:
    obj = _keys(raw, _NODE_KEYS, "a node row")
    try:
        state = NodeState(obj["state"])
    except ValueError as exc:
        raise _bad("state", obj["state"]) from exc
    node = WaveNode(
        node_id=_text(obj, "node_id", _NODE_ID), run_id=_text(obj, "run_id", _RUN_ID, null=True),
        state=state, refusal=_text(obj, "refusal", re.compile(r"[A-Z_]{0,20}")),
        detail=_text(obj, "detail", _TEXT),
        reservation_id=_text(obj, "reservation_id", _RESERVATION_ID, null=True),
        amount=_int(obj, "amount", 1, 2**31, null=True),
        worktree=_text(obj, "worktree", _PATH, null=True),
        generation=_int(obj, "generation", 1, 2**40, null=True),
        verdict=_text(obj, "verdict", re.compile(r"[A-Z0-9_]{0,64}")),
        lease_held_at_end=_bool(obj, "lease_held_at_end"),
        lease_released=_bool(obj, "lease_released"),
        holds_ended=_int(obj, "holds_ended", 0, 2**31, null=True))
    bare = (node.reservation_id, node.amount, node.worktree, node.generation)
    ended = (node.lease_held_at_end, node.lease_released)
    if state is NodeState.REFUSED:
        if node.refusal not in {r.value for r in Refusal}:
            raise _bad("refusal", node.refusal)
        if (any(v is not None for v in (*bare, *ended, node.holds_ended))
                or node.verdict):
            raise WaveRecordError(f"a refused node {node.node_id!r} carries a hold, a lease or an outcome")
        return node
    # A hold is a pair: both named, or neither (a node of a wave with no shared budget, or a run that
    # took no hold); the worktree and the lease generation are always there.
    if ((node.reservation_id is None) != (node.amount is None) or node.worktree is None
            or node.generation is None or node.refusal or node.run_id is None):
        raise WaveRecordError(f"an admitted node {node.node_id!r} lacks its hold or lease, or carries a refusal")
    if state is NodeState.ADMITTED:
        if phase is not Phase.ADMITTED:
            raise WaveRecordError(f"a settled record holds the unsettled node {node.node_id!r}")
        if any(v is not None for v in (*ended, node.holds_ended)) or node.verdict:
            raise WaveRecordError(f"an unsettled node {node.node_id!r} carries a settlement")
        return node
    if phase is not Phase.SETTLED:
        raise WaveRecordError(f"an unsettled record holds the finished node {node.node_id!r}")
    if any(v is None for v in ended):
        raise WaveRecordError(f"a finished node {node.node_id!r} has no lease settlement")
    if (state is NodeState.RETURNED) != bool(node.verdict):
        raise WaveRecordError(f"node {node.node_id!r}: a verdict belongs to RETURNED and only to it")
    if _VERDICT.fullmatch(node.verdict) is None and node.verdict:
        raise _bad("verdict", node.verdict)
    return node


def parse(data: bytes) -> WaveRecord:
    """The record these bytes hold, or `WaveRecordError` saying why not."""
    if not isinstance(data, bytes):
        raise WaveRecordError("a wave record is bytes")
    if len(data) > MAX_RECORD_BYTES:
        raise WaveRecordError(f"a wave record is at most {MAX_RECORD_BYTES} bytes, "
                              f"this one has {len(data)}")
    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates,
                         parse_constant=_refuse_constant)
    except (ValueError, RecursionError) as exc:      # UnicodeDecodeError is a ValueError
        raise WaveRecordError(f"not a JSON document: {exc}"[:300]) from exc
    top = _keys(obj, _TOP_KEYS, "a wave record")
    if top["schema"] != SCHEMA:
        raise _bad("schema", top["schema"])
    try:
        phase = Phase(top["phase"])
    except ValueError as exc:
        raise _bad("phase", top["phase"]) from exc
    created = _stamp(top["created_at"], "created_at")
    settled = top["settled_at"]
    if (phase is Phase.SETTLED) != (settled is not None):
        raise WaveRecordError("settled_at is set exactly when the phase is SETTLED")
    if settled is not None:
        _stamp(settled, "settled_at")
    if not isinstance(top["nodes"], list) or not isinstance(top["deferred"], list):
        raise WaveRecordError("nodes and deferred are lists")
    if len(top["nodes"]) > MAX_NODES or len(top["deferred"]) > MAX_DEFERRED:
        raise WaveRecordError("a record carries more nodes or deferrals than a plan may")
    nodes = tuple(_node(n, phase) for n in top["nodes"])
    if len({n.node_id for n in nodes}) != len(nodes):
        raise WaveRecordError("a node appears twice in one wave")
    deferred = []
    for raw in top["deferred"]:
        obj_ = _keys(raw, _DEFERRED_KEYS, "a deferral row")
        deferred.append(WaveDeferral(
            node_id=_text(obj_, "node_id", _NODE_ID), blocked_by=_text(obj_, "blocked_by", _NODE_ID),
            reason=_text(obj_, "reason", _DEFERRAL_REASON)))
    rec = WaveRecord(
        wave_id=_text(top, "wave_id", _WAVE_ID), phase=phase, created_at=created,
        settled_at=settled, width=_int(top, "width", 1, MAX_WAVE_WIDTH),
        ceiling=_int(top, "ceiling", 1, 2**31, null=True), nodes=nodes, deferred=tuple(deferred))
    if rec.ceiling is None and any(n.reservation_id is not None for n in nodes):
        raise WaveRecordError("a wave with no shared budget cannot carry a hold")
    return rec


def read_record(path: Path | str) -> WaveRecord | None:
    """The record at `path`; None only when there is no such file. A link, a
    FIFO, a directory, a second name, an unreadable or oversize file, or bytes
    that are not a record is `WaveRecordError` -- never "no wave"."""
    try:
        kind = os.lstat(path)
        if stat.S_ISREG(kind.st_mode) and kind.st_size > MAX_RECORD_BYTES:
            raise WaveRecordError(f"{path} is {kind.st_size} bytes; a wave record is at most "
                                  f"{MAX_RECORD_BYTES}")
        data = durable_io.read_single_link(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WaveRecordError(f"{path} cannot be read: {exc}"[:300]) from exc
    return None if data is None else parse(data)


# --------------------------------------------------------------------------- #
# The executor
# --------------------------------------------------------------------------- #

def _ascii(value: object, limit: int = 300) -> str:
    """What this module writes as free text: printable ASCII, bounded."""
    return "".join(c if " " <= c <= "~" else "?" for c in str(value))[:limit]


def _reason(exc: BaseException) -> str:
    return _ascii(f"{type(exc).__name__}: {exc}")


def _count(value: object, what: str, *, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise WaveError(f"{what} must be an integer from {low} to {high}, not {value!r}")
    return value


def _own_run_id(node: TaskNode) -> str:
    """The launcher's rule (`HohRunLauncher.prepare`): the node's run, else its id."""
    return node.run_id or node.id


def _never_qa(node: TaskNode) -> bool:
    return False


@dataclass
class _Held:
    """One admitted node: everything that has to be given back."""

    node: TaskNode
    run_id: str
    handle: WorkspaceLease
    admission: Admission
    row: WaveNode
    #: `(lease held at the end, lease released, holds ended)` once given back.
    settled: tuple[bool, bool, int | None] | None = None


class ParallelWaveExecutor:
    """Runs the nodes of one `FrontierPlan` together. Holds no state between
    waves: every wave is one `run_wave` call and one record file."""

    def __init__(self, record_dir: Path | str, *, width: int, budget: WaveBudget | None,
                 lease_for: Callable[[TaskNode], WorkspaceLease],
                 depends_on: Callable[[TaskNode, TaskNode], object],
                 action_class: Callable[[TaskNode], ActionClass],
                 run_id_of: Callable[[TaskNode], str] = _own_run_id,
                 clock: Callable[[], str] = utcnow,
                 is_qa: Callable[[TaskNode], bool] = _never_qa) -> None:
        try:
            self.record_dir = Path(record_dir).expanduser()
        except Exception as exc:                          # None, an int, "~nobody", ...
            raise WaveError(f"record_dir is not a path: {_reason(exc)}") from exc
        self.width = _count(width, "width", low=1, high=MAX_WAVE_WIDTH)
        if budget is not None:                            # None: no shared budget is configured
            if (not isinstance(budget, WaveBudget)
                    or not isinstance(budget.reservations, BudgetReservations)):
                raise WaveError("budget must be a WaveBudget over BudgetReservations, or None")
            _count(budget.ceiling, "the ceiling", low=1, high=2**31)
            _count(budget.want, "the amount held per node", low=1, high=2**31)
            # A reserve of the whole ceiling would refuse every node that is not QA.
            _count(budget.qa_reserve, "the QA reserve", low=0, high=budget.ceiling - 1)
            if not callable(budget.spent):
                raise WaveError("the spend is a callable returning dispatches by run id")
            if not isinstance(budget.adopt, bool):
                raise WaveError("adopt is a bool")
            if budget.adopt and budget.qa_reserve:
                raise WaveError("a QA reserve cannot be kept on an adopted hold: the hold was "
                                "taken by the one that prepared the run, not by this executor")
        self.budget = budget
        for name, fn in (("lease_for", lease_for), ("depends_on", depends_on),
                         ("action_class", action_class), ("run_id_of", run_id_of),
                         ("clock", clock), ("is_qa", is_qa)):
            if not callable(fn):
                raise WaveError(f"{name} must be callable")
        self.lease_for, self.depends_on, self.action_class = lease_for, depends_on, action_class
        self.run_id_of, self.clock, self.is_qa = run_id_of, clock, is_qa

    # -- before anything is taken ------------------------------------------ #

    def _now(self) -> str:
        try:
            return _stamp(self.clock(), "the clock")
        except Exception as exc:                          # raised, or no timestamp
            raise WaveError(f"the clock is not a UTC timestamp source: {_reason(exc)}") from exc

    def _record_path(self, wave_id: object) -> Path:
        if not isinstance(wave_id, str) or _WAVE_ID.fullmatch(wave_id) is None:
            raise WaveError(f"a wave id is 1 to 64 characters of letters, digits, '.', '_' and "
                            f"'-': {wave_id!r}")
        try:
            kind = os.lstat(self.record_dir)
        except OSError as exc:
            raise WaveError(f"{self.record_dir} cannot be looked at: {exc}") from exc
        if not stat.S_ISDIR(kind.st_mode):
            raise WaveError(f"{self.record_dir} is not a directory of its own")
        path = self.record_dir / f"wave-{wave_id}.json"
        if os.path.lexists(path):
            raise WaveError(f"{path} exists: a wave id is used once")
        return path

    def _check_plan(self, plan: object) -> tuple[TaskNode, ...]:
        if not isinstance(plan, FrontierPlan):
            raise WaveError(f"the plan must be a FrontierPlan, not {type(plan).__name__}")
        selected = plan.selected
        if len(selected) > MAX_NODES or len(plan.deferred) > MAX_DEFERRED:
            raise WaveError("the plan carries more nodes or deferrals than a record may")
        if not all(isinstance(n, TaskNode) for n in selected):
            raise WaveError("the plan selects something that is not a TaskNode")
        ids = [n.id for n in selected]
        if any(_NODE_ID.fullmatch(i) is None for i in ids) or len(set(ids)) != len(ids):
            raise WaveError("the selected nodes do not have distinct ids a record can hold")
        for d in plan.deferred:
            node_, by_, why_ = (getattr(d, k, None) for k in ("node", "blocked_by", "reason"))
            if (not isinstance(node_, TaskNode) or not isinstance(by_, TaskNode)
                    or _NODE_ID.fullmatch(node_.id) is None or _NODE_ID.fullmatch(by_.id) is None
                    or not isinstance(why_, str) or _DEFERRAL_REASON.fullmatch(why_) is None):
                raise WaveError("the plan defers a node, or for a reason, that a record cannot name")
        # The answer is checked, not trusted: the same rule over the selected
        # nodes must defer none of them.
        again = ReadyFrontierPlanner().plan(selected, self.depends_on)
        if again.deferred or len(again.selected) != len(selected):
            raise WaveError("the selected nodes are not pairwise independent: "
                            + ", ".join(f"{d.node.id} behind {d.blocked_by.id}"
                                        for d in again.deferred)[:300])
        return selected

    def _run_ids(self, selected: tuple[TaskNode, ...]) -> list[tuple[str | None, str]]:
        out: list[tuple[str | None, str]] = []
        for node in selected:
            try:
                out.append((check_run_id(self.run_id_of(node)), ""))
            except Exception as exc:                      # fail closed: this node only
                out.append((None, _reason(exc)))
        known = [r for r, _ in out if r is not None]
        if len(set(known)) != len(known):
            raise WaveError("two selected nodes would run as one run id: one hold would "
                            "stand for both")
        return out

    # -- admission ---------------------------------------------------------- #

    def _refuse(self, node: TaskNode, run_id: str | None, why: Refusal, detail: str) -> WaveNode:
        return WaveNode(node_id=node.id, run_id=run_id, state=NodeState.REFUSED,
                        refusal=why.value, detail=_ascii(detail))

    def _kept(self, node: TaskNode) -> int:
        """What this node's hold must leave: nothing for a QA node, the reserve
        for every other. Anything but a plain True -- a raise, a truthy non-bool
        -- is not QA: the failure keeps the reserve, it never spends it."""
        try:
            qa = self.is_qa(node) is True
        except Exception:
            qa = False
        return 0 if qa else self.budget.qa_reserve

    def _end_hold(self, run_id: str, reason: str) -> int | None:
        if self.budget is None or self.budget.adopt:      # not this executor's hold to end
            return None
        try:
            return self.budget.reservations.settle(run_id, reason)
        except Exception:                                 # the hold stays open: smaller, never larger
            return None

    def _admit(self, wave_id: str, node: TaskNode, run_id: str | None, run_error: str,
               taken: int, used: set[Path]) -> tuple[WaveNode, _Held | None]:
        if node.lifecycle is not Lifecycle.READY:
            return self._refuse(node, run_id, Refusal.NOT_READY,
                                f"the node is {node.lifecycle}, not READY"), None
        try:
            klass = self.action_class(node)
        except Exception as exc:
            return self._refuse(node, run_id, Refusal.EXTERNAL,
                                f"the action class cannot be read: {_reason(exc)}"), None
        if klass is not ActionClass.INTERNAL:
            return self._refuse(node, run_id, Refusal.EXTERNAL,
                                "an action that is not INTERNAL is not decided here"), None
        if taken >= self.width:
            return self._refuse(node, run_id, Refusal.WIDTH,
                                f"the wave is {self.width} wide and full"), None
        if run_id is None:
            return self._refuse(node, None, Refusal.RUN_ID, run_error), None
        # The lease first: `reserve` supersedes an open hold of the same run id, so
        # a node that cannot have its worktree must not have been near the ledger.
        calling, before_generation, token_before = False, 0, None
        try:
            handle = self.lease_for(node)
            if not isinstance(handle, WorkspaceLease):
                raise WaveError(f"lease_for returned {type(handle).__name__}, not a WorkspaceLease")
            # The record names the worktree it leases, or the node is not admitted:
            # a path it would have to garble is a lease a recovery cannot find.
            where = str(handle.worktree)
            if _PATH.fullmatch(where) is None:
                raise WaveError("the worktree path is not printable ASCII of 1 to 512 characters, "
                                "so the record could not name it")
            # A handle that already holds its lease "gets it back unchanged": one
            # handle for two nodes would admit both into one worktree.
            if handle.worktree in used:
                raise WaveError(f"{handle.worktree} is already the worktree of another "
                                f"node of this wave")
            before_generation, token_before = self._generation(handle), handle.token
            calling = True
            lease = handle.acquire()
            if token_before is not None and lease.generation == before_generation:
                # `acquire` handed back a lease this handle already held (another wave's
                # node, or the caller's): not this wave's to take, run in or give back.
                raise WaveError(f"{handle.worktree} is already held through this handle")
            used.add(handle.worktree)
        except BaseException as exc:
            # `acquire` writes the lease before its log line and before it returns:
            # a raise (an interrupt included) can leave that lease on disk.
            note = (self._undo_acquire(handle, wave_id, before_generation, token_before)
                    if calling else "")
            if not isinstance(exc, Exception):
                raise
            return self._refuse(node, run_id, Refusal.WORKSPACE,
                                _reason(exc)[:300 - len(note)] + note), None
        # From here a refusal, an error or an interrupt gives the lease back.
        before: set[str] | None = None
        hold = None
        try:
            if self.budget is None:
                pass                                       # no shared budget: nothing to hold
            elif self.budget.adopt:
                # The run was prepared by someone who took its hold: record it, take none.
                open_ = [r for r in self.budget.reservations.held() if r.run_id == run_id]
                if len(open_) > 1:
                    raise WaveError(f"run {run_id} has {len(open_)} open holds: not one to adopt")
                hold = open_[0] if open_ else None
            else:
                held_now = self.budget.reservations.held()
                before = {r.reservation_id for r in held_now}
                already = [r for r in held_now if r.run_id == run_id]
                if already:
                    raise WaveError(f"run {run_id} already holds {already[0].reservation_id}: it is "
                                    f"not this wave's to supersede")
                hold = self.budget.reservations.reserve(
                    run_id, ceiling=self.budget.ceiling, spent=self.budget.spent,
                    want=self.budget.want, keep=self._kept(node))
        except BaseException as exc:
            # `reserve` writes the hold before it returns: a raise after the replace
            # (a failed directory sync, an interrupt) leaves a HELD row nobody owns.
            try:
                stranded = self._end_stranded(wave_id, run_id, before)
            finally:                           # a second interrupt there still gives the lease back
                gave = self._release_lease(
                    handle, wave_id,
                    "the budget was refused" if isinstance(exc, Exception) else "interrupted")
                used.discard(handle.worktree)
            if not isinstance(exc, Exception):
                raise
            note = "" if gave else " (its lease could not be released and stays until it runs out)"
            tail = stranded + note
            why = (Refusal.QA_RESERVE if getattr(exc, "reserve_kept", False) is True
                   else Refusal.BUDGET)
            return self._refuse(node, run_id, why,
                                _reason(exc)[:300 - len(tail)] + tail), None
        hold_id = hold.reservation_id if hold is not None else None
        hold_amount = hold.amount if hold is not None else None
        admission = Admission(wave_id=wave_id, node=node, run_id=run_id,
                              reservation_id=hold_id, amount=hold_amount,
                              worktree=handle.worktree, generation=lease.generation)
        row = WaveNode(node_id=node.id, run_id=run_id, state=NodeState.ADMITTED,
                       reservation_id=hold_id, amount=hold_amount,
                       worktree=where, generation=lease.generation)
        return row, _Held(node, run_id, handle, admission, row)

    @staticmethod
    def _generation(handle: WorkspaceLease) -> int:
        """The generation on disk before `acquire`, or 0 -- best effort, never raises."""
        try:
            lease = handle.read()
            return 0 if lease is None else lease.generation
        except Exception:
            return 0

    def _undo_acquire(self, handle: WorkspaceLease, wave_id: str, before_generation: int,
                      token_before: str | None) -> str:
        """After an `acquire` that raised: release what it wrote if the handle
        knows it has it; else say whether a lease of this holder is still on
        disk (the token is set only after the write, so a failed sync of the
        write leaves one the handle cannot release: limit L13). Never raises."""
        if handle.token is not None:
            if handle.token == token_before and self._generation(handle) <= before_generation:
                return ""                      # this call wrote nothing: the lease is not ours to give back
            if self._release_lease(handle, wave_id, "the lease was refused"):
                return ""
            return " (its lease could not be released and stays until it runs out)"
        try:
            now = handle.read()
        except Exception:
            return ""
        if (now is not None and not now.released_at and now.holder == handle.holder
                and now.holder_pid == handle.pid and now.generation > before_generation):
            return (" (a live lease of this holder is on disk and could not be released: "
                    "it stays until it runs out)")
        return ""

    def _end_stranded(self, wave_id: str, run_id: str, before: set[str] | None) -> str:
        """After a `reserve` that raised: settle a HELD row of `run_id` that was
        not there before it, and say what the ledger reads afterwards (not what
        `settle` raised: its row can be on disk although its sync failed)."""
        if before is None:                                # the ledger was never read
            return ""

        def stranded() -> list:
            return [r for r in self.budget.reservations.held()
                    if r.run_id == run_id and r.reservation_id not in before]

        try:
            if not stranded():
                return ""
        except Exception:
            return " (the ledger could not be read afterwards: a hold of this run may be open)"
        self._end_hold(run_id, f"wave {wave_id}: reserve raised after it wrote the hold")
        try:
            left = stranded()
        except Exception:
            return " (a hold written before the failure may stay open: the ledger cannot be read)"
        if left:
            return " (a hold written before the failure could not be ended and stays open)"
        return " (a hold written before the failure was ended)"

    # -- settlement --------------------------------------------------------- #

    @staticmethod
    def _release_lease(handle: WorkspaceLease, wave_id: str, why: str) -> bool:
        try:
            handle.release(reason=f"wave {wave_id}: {why}")
            return True
        except Exception:
            try:                               # the release record can be on disk although its log line failed
                now = handle.read()
            except Exception:
                return False
            return now is not None and bool(now.released_at) and now.holder == handle.holder

    def _give_back(self, wave_id: str, held: _Held) -> tuple[bool, bool, int | None]:
        """`(lease held at the end, lease released, holds ended)`; never raises,
        and gives back once: a second call returns the first answer."""
        if held.settled is not None:
            return held.settled
        try:
            held.handle.check()
            kept = True
        except Exception:
            kept = False
        released = self._release_lease(held.handle, wave_id, "settled")
        held.settled = (kept, released, self._end_hold(held.run_id, f"wave {wave_id} settled"))
        return held.settled

    @staticmethod
    def _verdict_of(outcome: object) -> str:
        """The verdict name of what `launch` returned, or "" -- never raises."""
        try:
            raw = getattr(outcome, "verdict", None)
            return "" if raw is None else str(getattr(raw, "value", raw))
        except Exception:
            return ""

    def _call(self, launch: Callable, wave_id: str, held: _Held) -> tuple[str, object]:
        """Runs one node in its own thread and gives its resources back there,
        so the lease is judged when **this** node ended."""
        try:
            out: tuple[str, object] = ("returned", launch(held.node, held.admission))
        except Exception as exc:
            out = ("error", _reason(exc))
        except BaseException as exc:                     # SystemExit, KeyboardInterrupt
            out = ("raise", exc)
        self._give_back(wave_id, held)
        return out

    # -- the wave ----------------------------------------------------------- #

    def run_wave(self, wave_id: str, plan: FrontierPlan,
                 launch: Callable[[TaskNode, Admission], object],
                 on_admitted: Callable[[tuple[TaskNode, ...]], object] | None = None) -> WaveResult:
        """Runs the selected nodes of `plan` together; see the module docstring.

        `on_admitted(nodes)` is called once, in the calling thread, after the
        write-ahead record is on disk and before the first launch, with the
        nodes that will run: the caller persists what has to be durable before
        a dispatch (the controller's RUNNING). If it raises, every lease this
        executor took is given back, nothing is launched and the exception
        propagates. A hold the executor **adopted** (`WaveBudget.adopt`) is not
        ended here, on this path or any other: it is the caller's, who ends it
        on every exception that leaves `run_wave`.

        `launch(node, admission)` runs in its own thread and returns the node's
        outcome (an object with a `verdict`). Raises `WaveError` -- having held,
        leased and launched nothing -- for a plan, an argument or a record
        directory that is not acceptable; `WaveNotSettled` when the nodes ran
        and were given back but the final record could not be written.
        """
        if not callable(launch):
            raise WaveError("launch must be callable")
        path = self._record_path(wave_id)
        try:
            selected = self._check_plan(plan)
        except WaveError:
            raise
        except Exception as exc:                          # depends_on raised, a plan of no shape
            raise WaveError(f"the plan could not be checked: {_reason(exc)}") from exc
        run_ids = self._run_ids(selected)
        created = self._now()

        rows: list[WaveNode] = []
        held: list[_Held] = []
        used: set[Path] = set()
        try:
            for node, (run_id, run_error) in zip(selected, run_ids, strict=True):
                row, taken = self._admit(wave_id, node, run_id, run_error, len(held), used)
                rows.append(row)
                if taken is not None:
                    held.append(taken)
        except BaseException:                            # an interrupt between two admissions
            for h in held:
                self._give_back(wave_id, h)
            raise

        deferred = tuple(WaveDeferral(d.node.id, d.blocked_by.id, d.reason)
                         for d in plan.deferred)
        record = WaveRecord(wave_id=wave_id, phase=Phase.ADMITTED, created_at=created,
                            settled_at=None, width=self.width,
                            ceiling=self.budget.ceiling if self.budget is not None else None,
                            nodes=tuple(rows), deferred=deferred)
        # The write-ahead record: no node starts that this file does not name.
        try:
            durable_io.durable_create(path, record.to_bytes(), mode=0o600)
        except BaseException as exc:
            for h in held:
                self._give_back(wave_id, h)
            if isinstance(exc, Exception):
                raise WaveError(f"the wave record could not be written, nothing was "
                                f"launched: {_reason(exc)}") from exc
            raise

        if on_admitted is not None and held:
            try:
                on_admitted(tuple(h.node for h in held))
            except BaseException:                        # the caller could not make it durable
                for h in held:
                    self._give_back(wave_id, h)
                raise

        results: dict[int, tuple[str, object]] = {}
        try:
            if held:
                with ThreadPoolExecutor(max_workers=len(held),
                                        thread_name_prefix=f"wave-{wave_id}"[:30]) as pool:
                    futures = [pool.submit(self._call, launch, wave_id, h) for h in held]
                    for i, future in enumerate(futures):
                        results[i] = future.result()
        except BaseException:                            # no thread could be started, say
            for h in held:
                self._give_back(wave_id, h)
            raise

        interrupt: BaseException | None = None
        final: dict[str, WaveNode] = {}
        outcomes: dict[str, object] = {}
        for i, h in enumerate(held):
            kind, value = results[i]
            kept, released, ended = self._give_back(wave_id, h)    # already done in the node's thread
            state, verdict, detail = NodeState.ERRORED, "", ""
            if kind == "returned":
                name = self._verdict_of(value)
                if _VERDICT.fullmatch(name):
                    state, verdict = NodeState.RETURNED, name
                    outcomes[h.node.id] = value
                else:
                    detail = "launch returned no verdict"
            elif kind == "error":
                detail = str(value)
            else:
                interrupt = interrupt or value            # type: ignore[assignment]
                detail = _reason(value)                   # type: ignore[arg-type]
            final[h.node.id] = replace(
                h.row, state=state, verdict=verdict, detail=_ascii(detail),
                lease_held_at_end=kept, lease_released=released, holds_ended=ended)

        settled = replace(record, phase=Phase.SETTLED, settled_at=self._safe_now(created),
                          nodes=tuple(final.get(r.node_id, r) for r in rows))
        result = WaveResult(settled, outcomes)
        try:
            durable_io.durable_write(path, settled.to_bytes(), mode=0o600)
        except Exception as exc:
            if interrupt is not None:
                raise interrupt
            raise WaveNotSettled(
                f"the nodes ran and were given back, but the final wave record could not "
                f"be written: {_reason(exc)}", result) from exc
        if interrupt is not None:
            raise interrupt
        return result

    def _safe_now(self, fallback: str) -> str:
        try:
            return self._now()
        except Exception:
            return fallback


# --------------------------------------------------------------------------- #
# Settlement of a finished wave
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class NodeSettlement:
    """What became of one admitted node's hold. `used` is what the run had
    dispatched when the spend was read. `state` is ENDED (this call ended the
    hold), ALREADY_ENDED (the node's own settlement had), SUPERSEDED (a later
    hold of the same run replaced it: this call ended nothing for the node, and
    the later hold, which may still be open, has another id) or IN_FLIGHT (the
    launch came back IN_FLIGHT: another process still executes the run and can
    still dispatch under the hold, so it was **not** looked at, let alone ended).

    `released` is what of the hold the run did not use and that the pool has
    again because **this record's hold ended**: `max(amount - used, 0)` for ENDED
    and ALREADY_ENDED, and 0 for SUPERSEDED and IN_FLIGHT -- nothing of this
    record's hold ended, and reporting its unused part as released would say the
    pool got back what it did not."""

    node_id: str
    run_id: str
    reservation_id: str
    amount: int
    used: int
    released: int
    state: str


@dataclass(frozen=True)
class WaveSettlement:
    wave_id: str
    nodes: tuple[NodeSettlement, ...]
    degradation: ParallelismDegradation | None

    @property
    def released(self) -> int:
        return sum(n.released for n in self.nodes)


def _spend(spent: object) -> dict[str, int]:
    """The dispatches each run has spent, or `WaveError`: a spend that cannot be
    read is not a spend of zero (it would call the whole hold released)."""
    if not callable(spent):
        raise WaveError("the spend is a callable returning dispatches by run id")
    try:
        mapping = spent()
    except Exception as exc:
        raise WaveError(f"the spend cannot be read: {_reason(exc)}") from exc
    if not isinstance(mapping, Mapping):
        raise WaveError("the spend is not a mapping of run id to dispatches")
    out: dict[str, int] = {}
    for run_id, n in mapping.items():
        if not isinstance(run_id, str) or isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise WaveError(f"the spend of {_ascii(run_id, 60)} is not a count of dispatches")
        out[run_id] = n
    return out


#: The verdict name of a launch that found another process executing the run
#: (`RunVerdict.IN_FLIGHT.value`; pinned by a test, this module may not import the
#: launcher). `HohRunLauncher.launch` keeps the hold of such a run, and so does
#: `settle_wave`: it is the one verdict whose hold is not the launcher's to end.
_IN_FLIGHT = "IN_FLIGHT"


def settle_wave(record_path: Path | str, reservations: BudgetReservations,
                spent: Callable[[], Mapping[str, int]]) -> WaveSettlement:
    """Ends the holds a finished wave left open, and says what each run used.

    The record is read back strictly (`read_record`) and must be SETTLED: a
    record that is missing, not exactly what this module writes, or still
    ADMITTED (the nodes may be running, or the process died) is a `WaveError`
    and ends nothing. Then every hold the record names is ended **by its
    reservation id** in one ledger write, so a hold the node's own settlement
    could not end is ended now, and a hold of the same run that a later wave took
    is never touched. A hold the ledger does not know, or that is of another run
    or amount than the record says, refuses the whole call. Calling it again is a
    no-op that writes nothing.

    **A node whose launch came back IN_FLIGHT is skipped** (state IN_FLIGHT,
    nothing ended): another process still executes that run and can still
    dispatch under its hold, which is why `HohRunLauncher.launch` keeps it. A
    record cannot say whether that process has finished since, so this function
    never decides it; the launcher's next evaluation of the run does.

    A controller wave **adopts** the launcher's hold (`WaveBudget.adopt`): the
    record names the hold the launcher took and the launch ended, so the usual
    row is ALREADY_ENDED, and ENDED is a hold the launch could not end (a ledger
    that was busy). A row is SUPERSEDED only for an executor that reserved itself
    and whose hold something else replaced; it releases nothing and the later
    hold stays open.
    """
    if not isinstance(reservations, BudgetReservations):
        raise WaveError("reservations must be a BudgetReservations")
    record = read_record(record_path)
    if record is None:
        raise WaveError(f"{record_path} does not exist: there is no wave to settle")
    if record.phase is not Phase.SETTLED:
        raise WaveError(f"wave {record.wave_id} is {record.phase}, not SETTLED: its nodes may "
                        f"still be running, and recovering that is not this function's")
    usage = _spend(spent)
    held = [n for n in record.nodes if n.reservation_id is not None]
    live = {n.node_id for n in held
            if n.state is NodeState.RETURNED and n.verdict == _IN_FLIGHT}
    ending = [n for n in held if n.node_id not in live]
    try:
        found = reservations.settle_holds(
            [n.reservation_id for n in ending], f"wave {record.wave_id} settled",
            expect={n.reservation_id: (n.run_id, n.amount) for n in ending})
    except Exception as exc:
        raise WaveError(f"the holds of wave {record.wave_id} could not be ended: "
                        f"{_reason(exc)}") from exc
    rows = []
    for n in held:
        used = usage.get(n.run_id, 0)
        if n.node_id in live:
            rows.append(NodeSettlement(
                node_id=n.node_id, run_id=n.run_id, reservation_id=n.reservation_id,
                amount=n.amount, used=used, released=0, state="IN_FLIGHT"))
            continue
        status = found[n.reservation_id]
        state = {"HELD": "ENDED", "SETTLED": "ALREADY_ENDED",
                 "SUPERSEDED": "SUPERSEDED"}[str(status)]
        rows.append(NodeSettlement(
            node_id=n.node_id, run_id=n.run_id, reservation_id=n.reservation_id,
            amount=n.amount, used=used,
            released=0 if state == "SUPERSEDED" else max(n.amount - used, 0),
            state=state))
    return WaveSettlement(record.wave_id, tuple(rows), record.degradation)


__all__ = ["MAX_DEFERRED", "MAX_NODES", "MAX_RECORD_BYTES", "MAX_WAVE_WIDTH", "SCHEMA",
           "Admission", "NodeSettlement", "NodeState", "ParallelWaveExecutor",
           "ParallelismDegradation", "ParallelismDegradationReason", "Phase", "Refusal",
           "WaveBudget", "WaveDeferral", "WaveError", "WaveNode", "WaveNotSettled", "WaveRecord",
           "WaveRecordError", "WaveResult", "WaveSettlement", "parse", "read_record",
           "settle_wave"]
