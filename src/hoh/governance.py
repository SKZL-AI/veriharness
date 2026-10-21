"""The contract a run actually obeys, and the record of what it decided.

P2-24. `hoh.autonomy` answers "what may this programme decide alone"; this
module is what makes that answer happen to a real run. Two reviewers refused
to call the routing proven while nothing consulted it, and they were right: a
pure function with unit tests proves the artefact, not the capability.

Three things happen here:

* **The contract is pinned to the run, once.** `bind()` writes it into the run
  directory the first time and reads it back on every later call. A resumed run
  obeys the contract it started under, not whatever file the operator happens
  to pass on the resume command line -- and a *different* contract offered to a
  bound run is refused rather than silently adopted, because "which rules was
  this run under" must have one answer.
* **Every routing is recorded** in `<run>/autonomy/routings.jsonl`, with the
  rule that produced it. A programme that runs for weeks makes a long list of
  choices nobody watched; the list is only reviewable if each entry says which
  rule decided it.
* **The run acts on the answer.** A block becomes what the contract says it
  should be: a wait for a human, a parked branch with a durable retry time, a
  collected digest item, or a stopped programme. Without this, "Needs-You OFF"
  would be a field nobody read.

What this module does not do is lower a bar. The strictest thing a contract can
do here is refuse to proceed; nothing in it touches isolation, receipts or
acceptance.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import durable_io
from .autonomy import (
    AutonomyContract,
    AutonomyMode,
    Escalation,
    Handling,
    NeedsYou,
    OnLimit,
    Routing,
    coherence,
    continues_past,
    route,
)
from .contracts import Condition, RunState, utcnow
from .decisionfrontier import (
    EXTERNAL_ACTION,
    TIMED_KINDS,
    UNKNOWN,
    UNREADABLE,
    FrontierError,
    derive_frontier,
)
from .frontier import SEMANTIC_DEPENDENCY
from .project import ActionClass, Lifecycle, ProjectState, TaskNode
from .store import RunStore, UnsafeRunId


class ContractRefused(RuntimeError):
    """The contract cannot govern this run, and nothing was assumed instead."""


class RoutingLogUnreadable(RuntimeError):
    """A run's routing log exists and cannot be trusted as a whole. Raised
    rather than answering with the lines that did parse: a log that is read
    partially is read as saying less than it does."""


def _routings_path(run_dir: Path | str) -> Path:
    return Path(run_dir) / "autonomy" / "routings.jsonl"


@dataclass
class Governor:
    """One run, under one pinned contract, with its decisions written down."""

    contract: AutonomyContract
    run_dir: Path

    @property
    def _log(self) -> Path:
        return _routings_path(self.run_dir)

    def decide(self, *, decision: str, kind: str = "decision",
               delegable: bool = True) -> Routing:
        """Route one decision and record it. The record is the point."""
        routing = route(self.contract, decision=decision, kind=kind, delegable=delegable)
        self._log.parent.mkdir(parents=True, exist_ok=True)
        # A routing that could not be written down is not returned as made: a
        # full disk is a `DiskFullError` here, before the caller acts on it.
        durable_io.durable_append(self._log, json.dumps({
            "at": utcnow(),
            "contract_digest": self.contract.digest(),
            "program_epoch": self.contract.program_epoch,
            **json.loads(routing.model_dump_json()),
        }) + "\n")
        return routing

    def routings(self) -> list[dict]:
        if not self._log.is_file():
            return []
        return [json.loads(line) for line in
                self._log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def evidence(self) -> dict:
        return {"contract_id": self.contract.contract_id,
                "digest": self.contract.digest(),
                "mode": self.contract.mode.value,
                "needs_you": self.contract.needs_you.value,
                "program_epoch": self.contract.program_epoch,
                "release_policy": self.contract.release_policy.value,
                "routings": len(self.routings())}


def _witnessed_digest(witness: Path) -> tuple[str | None, bool]:
    """(the digest recorded beside the pin, whether the record was readable).

    Two answers, not one: "no witness was ever written" and "the witness is
    damaged" are different situations, and treating the second as the first is
    what let a reviewer disable the tamper guard by overwriting one file.
    """
    if not witness.exists():
        return None, True
    try:
        return json.loads(witness.read_text(encoding="utf-8")).get("digest"), True
    except (OSError, ValueError, AttributeError):
        return None, False


def _first_logged_digest(run_dir: Path) -> str | None:
    """The contract digest of this run's first recorded decision, if any."""
    log = run_dir / "autonomy" / "routings.jsonl"
    try:
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                return json.loads(line).get("contract_digest")
    except (OSError, ValueError):
        return None
    return None


#: A routing is about half a kilobyte, so this is some sixteen thousand of them
#: in one run. A larger file is refused unread rather than read into memory.
MAX_ROUTING_LOG_BYTES = 8 * 1024 * 1024

#: What `Governor.decide` writes beside the routing's own fields.
_LOG_BOOKKEEPING = frozenset({"at", "contract_digest", "program_epoch"})


def read_branch_stops(run_dir: Path | str) -> list[Routing]:
    """The routings of this run that stopped its branch, in log order.

    Read-only: nothing is created, pinned or witnessed, and no contract is
    needed. `[]` means the run never routed anything (no log). Anything else
    that is not a clean log is `RoutingLogUnreadable`, the whole file: a link in
    place of the directory or the file, a file that is not regular (a FIFO would
    hang a plain `open`), one over `MAX_ROUTING_LOG_BYTES`, bytes that are not
    UTF-8, a line that is not one JSON object, and a record that is neither a
    routing nor a `record_override` entry.

    A torn fragment left by a failed append (`durable_io.durable_append`) is
    such a line. It is refused, not skipped: what the fragment said is not known.
    """
    path = _routings_path(run_dir)
    try:
        if stat.S_ISLNK(os.lstat(path.parent).st_mode):
            raise RoutingLogUnreadable(f"{path.parent} is a link, not the run's directory")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RoutingLogUnreadable(f"{path} cannot be opened: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RoutingLogUnreadable(f"{path} is not a regular file")
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_ROUTING_LOG_BYTES:
            chunk = os.read(fd, min(1 << 20, MAX_ROUTING_LOG_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError as exc:
        raise RoutingLogUnreadable(f"{path} cannot be read: {exc}") from exc
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > MAX_ROUTING_LOG_BYTES:
        raise RoutingLogUnreadable(
            f"{path} is larger than {MAX_ROUTING_LOG_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RoutingLogUnreadable(f"{path} is not UTF-8: {exc}") from exc
    stops: list[Routing] = []
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip(" \t\r"):
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("not a JSON object")
            if "override_of" in record and "handling" not in record:
                continue
            routing = Routing.model_validate(
                {k: v for k, v in record.items() if k not in _LOG_BOOKKEEPING})
        except (ValueError, RecursionError) as exc:
            raise RoutingLogUnreadable(f"{path}:{number}: {exc}") from exc
        if routing.blocks_branch:
            stops.append(routing)
    return stops


def pinned_path(run_dir: Path | str) -> Path:
    return Path(run_dir) / "autonomy" / "contract.json"


def bind(run_dir: Path | str, *, contract: AutonomyContract | None = None,
         program_epoch: int | None = None, write: bool = True) -> Governor | None:
    """The contract this run is under (`write=False`: decide only, write nothing).

    First call with a contract pins it; later calls read the pinned one back.
    Offering a *different* contract to a bound run is refused: a run that
    changed its rules halfway has no answer to "which rules was it under", and
    the plan's whole point is that the epoch is where such a change shows.

    Returns None when no contract was ever pinned and none is offered -- a run
    without a contract is the behaviour every run had before this existed, and
    it stays legal rather than becoming a silent default.
    """
    path = pinned_path(run_dir)
    witness = Path(run_dir) / "approval" / "autonomy.json"
    if path.is_symlink() or path.parent.is_symlink():
        raise ContractRefused(
            f"{path} is a link; the contract a run is judged against lives in the run "
            f"directory, not wherever a link points")
    if path.is_file():
        try:
            pinned = AutonomyContract.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:                      # noqa: BLE001 - one refusal type
            # A reviewer overwrote the pinned file with junk and got a
            # traceback out of `hoh deliver` and `hoh run` -- and, worse, out
            # of the abort handler whose whole job is to end bounded. One
            # exception type leaves this module, always.
            raise ContractRefused(
                f"the pinned contract in {path} cannot be read: {exc}") from exc
        # Bound to a digest recorded outside the file it protects: editing the
        # pinned contract into something wider is coherent, so `coherence()`
        # cannot catch it. A reviewer turned NEVER_AUTO_PUBLISH into
        # AUTO_PUBLISH_PREAUTHORIZED_TARGET this way and the delivery gate
        # opened.
        recorded, readable = _witnessed_digest(witness)
        if witness.exists() and not readable:
            raise ContractRefused(
                f"{witness} records which contract this run was bound to and cannot be "
                f"read. A guard that switches itself off when its own record is damaged "
                f"is not a guard; restore it or start a new run.")
        if recorded is None:
            # No witness: either a run pinned before `bind` wrote one itself, or
            # a witness that has been removed. The routing log is a second,
            # append-only record of the digest and answers the difference --
            # except on a freshly pinned run, which has no routing yet. That
            # gap is why the witness is written here rather than by the caller.
            logged = _first_logged_digest(Path(run_dir))
            if logged is None:
                raise ContractRefused(
                    f"{path} is pinned but neither {witness.name} nor a recorded "
                    f"decision says which contract this run was bound to. For a run "
                    f"pinned by this version both exist, so this is a tree somebody "
                    f"edited; restore it or start a new run.")
            if logged != pinned.digest():
                raise ContractRefused(
                    f"the pinned contract digests {pinned.digest()}, but this run's "
                    f"first recorded decision was made under {logged}. The contract "
                    f"file has been changed since the run started.")
        elif recorded != pinned.digest():
            raise ContractRefused(
                f"the pinned contract in {path} digests {pinned.digest()}, but this run "
                f"was bound to {recorded}. The contract file has been changed since the "
                f"run started; restore it or start a new run.")
        # Compared at the same epoch: the same contract offered again is the
        # same contract, whether or not the caller passed the epoch it was
        # pinned to. What is refused is a *different* set of rules.
        if contract is not None and \
                contract.pin(pinned.program_epoch).canonical() != pinned.canonical():
            raise ContractRefused(
                f"this run is pinned to contract {pinned.contract_id} "
                f"({pinned.digest()}, epoch {pinned.program_epoch}); it cannot be "
                f"resumed under {contract.contract_id} ({contract.digest()}). Start a "
                f"new run, or resume this one under the contract it began with.")
        return Governor(contract=pinned, run_dir=Path(run_dir))
    if contract is None:
        recorded, _ = _witnessed_digest(witness)
        if recorded:
            raise ContractRefused(
                f"this run was bound to contract {recorded}, and the pinned file "
                f"{path} is gone. A governed run does not quietly become an ungoverned "
                f"one; restore the contract or start a new run.")
        return None
    problems = coherence(contract)
    if problems:
        raise ContractRefused("; ".join(problems))
    pinned = contract if program_epoch is None else contract.pin(program_epoch)
    # A witness that records another contract, with the pin missing: one
    # ordinary fault (witness written, pin not) plus a different contract
    # offered would pin the other one and overwrite the record of what this
    # run was bound to. The same contract still repairs the run.
    recorded, _ = _witnessed_digest(witness)
    if recorded and recorded != pinned.digest():
        raise ContractRefused(
            f"this run was bound to contract {recorded}, its pinned file {path} is "
            f"missing, and the contract offered digests {pinned.digest()}. Restore "
            f"the pinned file, offer the contract the run was bound to, or start a "
            f"new run.")
    governor = Governor(contract=pinned, run_dir=Path(run_dir))
    if not write:
        # A caller that only asks "would this be accepted" (`hoh run --detach` before it opens a
        # tab): every refusal above is the same, nothing is pinned and nothing is witnessed.
        return governor
    # The witness is written here, not by the caller: a run pinned a moment ago
    # has no routing yet, so a caller that wrote it afterwards left a window in
    # which a swapped pin had nothing to be compared against. And it is written
    # *first*: a full disk between the two used to leave a pin with no witness,
    # which every later call refuses; a witness with no pin is the state this
    # function already describes for a deleted pin, and offering the contract
    # again pins it.
    path.parent.mkdir(parents=True, exist_ok=True)
    witness.parent.mkdir(parents=True, exist_ok=True)
    durable_io.durable_write(witness, json.dumps(governor.evidence(), indent=2) + "\n",
                             mode=None)
    durable_io.durable_write(path, pinned.model_dump_json(indent=2) + "\n", mode=None)
    return governor


#: Noted when a contract's STOP or ABORT cancels a run that a capability
#: violation holds: the routing is recorded, the hold is not replaced.
_HELD_NOTE = ("autonomy: the run stays held by its capability violation; the "
              "contract's routing cancels it but does not replace the kind, and "
              "only a person's hoh unblock lifts the hold")


#: What a block means to the contract. A run blocks because it met something it
#: could not decide, so the decision is never delegable -- that is what being
#: blocked *is*.
def block_under_contract(state: RunState, reason: str, governor: Governor | None,
                         *, kind: str = "decision",
                         scan_for_quota: bool = True) -> Routing | None:
    """Block, then do what the contract says about blocking.

    Without a contract this is exactly the old behaviour: the run stays BLOCKED
    and waits for a person. With one, the four answers the plan names become
    four different states, and each of them is recorded:

    * **ask** -- BLOCKED, as before, because someone is expected;
    * **park** -- BLOCKED with `retry_after`, so the never-stuck invariant holds
      and a sentinel sweep can pick the branch up again;
    * **collect** -- BLOCKED as a digest item, with a retry time for the same
      reason;
    * **stop** -- the programme is over, and says so.

    The branch is what stops in every case. Whether *other* work continues is
    the scheduler's business, and a run that is one branch has nothing else to
    do -- which is why this records `blocks_branch` rather than deciding it.
    """
    from . import stages

    stages.block(state, reason, scan_for_quota=scan_for_quota)
    if governor is None:
        return None
    routing = governor.decide(decision=reason, kind=kind, delegable=False)
    state.note(f"autonomy: {routing.handling.value} -- {routing.why}")
    # `stages.block` has kept the kind of a capability violation (or adopted it
    # from a legacy reason). That hold has one carrier, `blocked_kind`, and this
    # function is its second writer: STOP and ABORT used to overwrite it, so the
    # next block (which rewrites the reason) lost the hold and `hoh deliver`
    # fast-forwarded a violation-held candidate (run qaw7, B1).
    held = state.blocked_kind == stages.CAPABILITY_VIOLATION
    if routing.handling is Handling.STOP_PROGRAM:
        state.condition = Condition.CANCELLED
        if held:
            state.note(_HELD_NOTE)
        else:
            state.blocked_kind = "contract_stop"
        state.stop_reason = f"the contract stops the programme: {reason}"
    elif routing.handling is Handling.ABORT_BRANCH:
        # Terminal, like STOP. A run *is* one branch, so an abandoned branch
        # with no retry time and no owner was a state `sentinel.stuck_run`
        # calls a control-plane defect -- which a reviewer found because the
        # profile sweep never routes to ABORT.
        state.condition = Condition.CANCELLED
        if held:
            state.note(_HELD_NOTE)
        else:
            state.blocked_kind = "contract_abort"
        state.stop_reason = f"the contract abandons this branch: {reason}"
    elif routing.handling in (Handling.PARK_BRANCH, Handling.COLLECT_FOR_DIGEST):
        parked = ("parked_by_contract" if routing.handling is Handling.PARK_BRANCH
                  else "collected_for_digest")
        if not state.retry_after:
            # A parked branch with no time to look again is the never-stuck
            # defect O209's neighbour module measures. The contract's review
            # interval is that time; without one, the branch keeps a human
            # owner instead, which is the third way out.
            seconds = (governor.contract.horizon.review_after_seconds
                       or governor.contract.horizon.max_wallclock_seconds)
            if seconds:
                from .timers import in_seconds

                state.retry_after = in_seconds(seconds)
                # And a durable timer, so the due branch surfaces in
                # `hoh timers` and `hoh sentinel`. Writing `retry_after` alone
                # silenced the never-stuck detector for a promise no component
                # kept -- green because a field was set, which is the shape
                # this project refuses everywhere else.
                _arm_review_timer(governor, state, state.retry_after, reason)
                # `_arm_review_timer` clears `retry_after` when it could not
                # arm, so the classification below sees the real state.
        # Three ways out, and the branch must keep one. A reviewer measured the
        # case this guards: a stock PLAN_AUTONOMOUS contract carries no review
        # interval, so a collected question had no retry time -- and
        # `sentinel.stuck_run` only reads a human owner from
        # `blocked_kind == "human"`. The result was a run this project's own
        # detector calls a control-plane defect, created inside a passing test.
        # The *cause* of the block stays in `blocked_kind`. A reviewer measured
        # what overwriting it did: a quota wait under an unattended contract
        # stopped being `usage_limit`, so `hoh resume-quota` would never pick it
        # up -- the one mode that must not depend on a person became the one
        # that needed `hoh unblock`, while a supervised run resumed by itself.
        # Exactly backwards.
        handling_kind = parked if state.retry_after else "human"
        cause = state.blocked_kind or "unclassified"
        if state.blocked_kind in (None, "", "human"):
            state.blocked_kind = handling_kind
        # Read before the assignment: a fresh park used to log "parked (cause:
        # parked)", which states nothing.
        state.note(f"autonomy: {handling_kind} (cause: {cause})")
        if not state.retry_after:
            state.note(f"autonomy: {parked} without a review interval, so this branch "
                       f"keeps a human owner rather than waiting for nobody")
    else:
        state.blocked_kind = state.blocked_kind or "human"
    return routing


def _arm_review_timer(governor: Governor, state: RunState, due_at: str,
                      reason: str) -> None:
    """A timer for a parked branch, in the run's own timer store."""
    from .timers import TimerKind, TimerStore

    try:
        TimerStore(governor.run_dir.parent).arm_new(
            kind=TimerKind.EXTERNAL_WAIT, due_at=due_at,
            reason=f"parked by the autonomy contract: {reason}",
            program_id=governor.contract.contract_id,
            program_epoch=governor.contract.program_epoch,
            run_id=state.run_id)
    except Exception as exc:                          # noqa: BLE001 - reported, not fatal
        # And the retry time goes with it: `retry_after` is what silences the
        # never-stuck detector, so leaving it standing after the timer failed
        # would restore exactly the shape arming the timer removed.
        state.retry_after = None
        state.note(f"autonomy: the review timer could not be armed ({exc}), so this "
                   f"branch keeps a human owner instead")


def record_override(governor: Governor | None, *, routing: Routing, by: str,
                    what: str) -> None:
    """A human went past a blocking routing. That is allowed; leaving no record
    of it is not.

    Without this the log said "parked" while the release happened -- a reviewer
    found the pair and it is the worst kind of record, one that is wrong in the
    direction of looking safer.
    """
    if governor is None:
        return
    governor._log.parent.mkdir(parents=True, exist_ok=True)
    durable_io.durable_append(governor._log, json.dumps({
        "at": utcnow(), "contract_digest": governor.contract.digest(),
        "program_epoch": governor.contract.program_epoch,
        "override_of": routing.handling.value, "kind": routing.kind,
        "decision": what, "by": by,
        "why": f"a human authority overrode a {routing.handling.value} routing",
        # The routing's own words, kept: what was overridden matters as
        # much as that it was.
        "routing_why": routing.why,
    }) + "\n")


def forbidden_outright(routing: Routing | None) -> bool:
    """Is this a refusal a human at the keyboard may not wave through?

    A contract that does not allow external actions **at all** is not asking
    for approval; it is saying this programme does not do that. `--approve` is
    the captain's authority over a gate the contract *has*, not permission to
    leave the envelope it was given.
    """
    return routing is not None and routing.kind == ActionClass.EXTERNAL.value \
        and routing.blocks_branch


#: Delivery modes that stay inside this machine. `local-only` is a guarded
#: fast-forward of one local branch into another, which `project.ActionClass`
#: calls INTERNAL in as many words: "a `git merge` into the mainline is local
#: and reversible; a `git push` is not… treating anything else as external
#: turns the gate into noise that gets routinely waved through." Routing it as
#: EXTERNAL made every governed run unable to deliver locally, in every
#: profile, un-overridably -- a gate that refuses correct work, which is how a
#: gate teaches people to switch it off.
LOCAL_MODES = frozenset({"local-only"})


def may_release(governor: Governor | None, *, what: str,
                mode: str = "local-only") -> Routing | None:
    """Does the contract allow this delivery to proceed without a human?

    Returns the routing; the caller refuses unless it is resolved by policy.
    `None` means no contract governs this run, and the delivery rules that
    existed before decide alone.
    """
    if governor is None:
        return None
    if mode not in LOCAL_MODES and not governor.contract.may(ActionClass.EXTERNAL):
        # Leaving this machine, under a contract that does not allow it.
        return governor.decide(decision=what, kind=ActionClass.EXTERNAL.value)
    return governor.decide(decision=what, kind="release")


# --- Continuation in waves (V3.3 P3-28, first slice) -----------------------------

CONTINUATION_SCHEMA = "hoh-wave-continuation/1"

#: A record is written whole and read whole; a bigger one is refused, never cut.
MAX_CONTINUATION_BYTES = 1 << 20

CONTINUE, HOLD = "CONTINUE", "HOLD"
#: Why a wave continues nothing, in the order they are decided.
CONTRACT_INCOHERENT = "CONTRACT_INCOHERENT"
PROGRAMME_STOPPED = "PROGRAMME_STOPPED"
ROUTING_LOG_UNREADABLE = "ROUTING_LOG_UNREADABLE"
HOLD_REASONS = (CONTRACT_INCOHERENT, PROGRAMME_STOPPED, ROUTING_LOG_UNREADABLE)

_WAVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_NODE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_CONTRACT_ID = re.compile(r"[\x20-\x7e]{1,128}")
_DIGEST = re.compile(r"[0-9a-f]{16}")
_MAX_EPOCH = 2**40
_HANDLINGS = frozenset({h.value for h in Handling} | {EXTERNAL_ACTION, UNKNOWN, UNREADABLE})
_MODES = tuple(m.value for m in AutonomyMode)
_WITHHELD_REASONS = (SEMANTIC_DEPENDENCY, *HOLD_REASONS)


def _show(value: object) -> str:
    """A value for an error message, at most 80 characters, whatever it is.

    `str(10**5000)` raises `ValueError` (Python's integer-string limit): a message
    that names a value must not be where the refusal escapes as another exception.
    """
    try:
        return repr(value)[:80]
    except Exception:                                 # noqa: BLE001 - a message, never a failure
        return f"<{type(value).__name__}>"


def _a_path(value: object, what: str) -> None:
    """A directory argument is a `str` or a path-like. An `int` is not a path: `os.stat`
    reads it as a file descriptor (and an open directory's would be accepted)."""
    if not isinstance(value, (str, os.PathLike)):
        raise ContinuationError(f"{what} must be a path, not {type(value).__name__}")
_TOP_KEYS = ("schema", "wave_id", "contract_id", "contract_digest", "mode", "program_epoch",
             "outcome", "reason", "causes", "continued", "withheld", "decisions")
_WITHHELD_KEYS = ("node_id", "reason", "blocked_by")
_DECISION_KEYS = ("node_id", "handling")


class ContinuationError(RuntimeError):
    """A continuation that is refused as a whole: nothing was recorded, and the
    caller continues nothing. Also a record file that is not exactly what this
    module writes -- never "no continuation"."""


@dataclass(frozen=True)
class Withheld:
    """A READY, independent-by-graph node that this wave does not continue."""

    node_id: str
    #: SEMANTIC_DEPENDENCY for one node, or the wave's own reason (HOLD_REASONS).
    reason: str
    #: The BLOCKED, RUNNING or EXTERNAL node it is not provably independent of;
    #: "" for a wave-level reason.
    blocked_by: str = ""


@dataclass(frozen=True)
class StoppedDecision:
    """One open decision the wave stands beside (`decisionfrontier.OpenDecision`)."""

    node_id: str
    #: A `Handling` value, or EXTERNAL_ACTION / UNKNOWN / UNREADABLE.
    handling: str


@dataclass(frozen=True)
class WaveContinuation:
    """What one wave continues beside the branches that wait, and why not the rest.

    `continued` is what may enter the wave's frontier plan; whether and how it ran
    is the wave record's business (`hoh.wave`), not this file's.
    """

    wave_id: str
    contract_id: str
    contract_digest: str
    mode: str
    program_epoch: int
    outcome: str
    reason: str = ""
    #: The nodes behind a wave-level reason: whoever stopped the programme, or
    #: whose routing log could not be read. Empty for CONTRACT_INCOHERENT.
    causes: tuple[str, ...] = ()
    continued: tuple[str, ...] = ()
    withheld: tuple[Withheld, ...] = ()
    decisions: tuple[StoppedDecision, ...] = ()

    def __post_init__(self) -> None:
        def bad(what: str) -> ContinuationError:
            return ContinuationError(f"a wave continuation with {what}")

        # Exact types throughout: `type(x) is str`, never `isinstance` (a subclass of
        # `str` is not the string the writer writes) and never a truth test or a set
        # lookup on a value whose type was not looked at first (`[]` is falsy, and it
        # is not hashable).
        def text(value: object, pattern: re.Pattern, what: str) -> None:
            if type(value) is not str or pattern.fullmatch(value) is None:
                raise bad(f"{what} {_show(value)} that a record cannot hold")

        def word(value: object, allowed: tuple[str, ...] | frozenset[str], what: str) -> None:
            if type(value) is not str or value not in allowed:
                raise bad(f"{what} {_show(value)}")

        text(self.wave_id, _WAVE_ID, "the wave id")
        text(self.contract_id, _CONTRACT_ID, "the contract id")
        text(self.contract_digest, _DIGEST, "the contract digest")
        word(self.mode, _MODES, "the mode")
        word(self.outcome, (CONTINUE, HOLD), "the outcome")
        if (type(self.program_epoch) is not int
                or not 0 <= self.program_epoch <= _MAX_EPOCH):
            raise bad(f"the programme epoch {_show(self.program_epoch)}")
        for name in ("causes", "continued", "withheld", "decisions"):
            if type(getattr(self, name)) is not tuple:
                raise bad(f"{name} that is not a tuple")
        for w in self.withheld:
            if type(w) is not Withheld:
                raise bad("a withheld row that is not a Withheld")
        for d in self.decisions:
            if type(d) is not StoppedDecision:
                raise bad("a decision row that is not a StoppedDecision")
        for what, ids in (("causes", self.causes), ("continued", self.continued),
                          ("withheld", [w.node_id for w in self.withheld]),
                          ("decisions", [d.node_id for d in self.decisions])):
            for node_id in ids:
                text(node_id, _NODE_ID, f"a node id in {what}")
            if len(set(ids)) != len(ids):
                raise bad(f"a node twice in {what}")
        if set(self.continued) & {w.node_id for w in self.withheld}:
            raise bad("a node both continued and withheld")
        for w in self.withheld:
            word(w.reason, _WITHHELD_REASONS, "the withheld reason")
            if type(w.blocked_by) is not str:
                raise bad(f"{w.node_id!r} with a blocker that is not a string")
            if (w.reason == SEMANTIC_DEPENDENCY) != (w.blocked_by != ""):
                raise bad(f"{w.node_id!r} naming a blocker exactly when it is not a "
                          f"semantic dependency")
            if w.blocked_by != "":
                text(w.blocked_by, _NODE_ID, "a blocker")
        for d in self.decisions:
            word(d.handling, _HANDLINGS, "a decision handling")
        if type(self.reason) is not str:
            raise bad(f"a reason that is not a string: {_show(self.reason)}")
        if self.outcome == CONTINUE:
            if self.reason != "" or self.causes != ():
                raise bad("outcome CONTINUE and a reason or causes")
            if any(w.reason != SEMANTIC_DEPENDENCY for w in self.withheld):
                raise bad("outcome CONTINUE and a node withheld for a wave-level reason")
        elif self.outcome == HOLD:
            if self.reason not in HOLD_REASONS:
                raise bad(f"outcome HOLD and the reason {_show(self.reason)}")
            if self.continued != ():
                raise bad("outcome HOLD and nodes continued")
            if (self.causes != ()) != (self.reason != CONTRACT_INCOHERENT):
                raise bad(f"the reason {self.reason} and its causes disagreeing")
            if any(w.reason != self.reason for w in self.withheld):
                raise bad("outcome HOLD and a node withheld for another reason")

    def to_bytes(self) -> bytes:
        body = {
            "schema": CONTINUATION_SCHEMA, "wave_id": self.wave_id,
            "contract_id": self.contract_id, "contract_digest": self.contract_digest,
            "mode": self.mode, "program_epoch": self.program_epoch,
            "outcome": self.outcome, "reason": self.reason, "causes": list(self.causes),
            "continued": list(self.continued),
            "withheld": [{"node_id": w.node_id, "reason": w.reason, "blocked_by": w.blocked_by}
                         for w in self.withheld],
            "decisions": [{"node_id": d.node_id, "handling": d.handling}
                          for d in self.decisions],
        }
        return (json.dumps(body, sort_keys=True, indent=1, ensure_ascii=True)
                + "\n").encode("ascii")


def _run_dir_stops(runs_root: Path, node: TaskNode) -> list[Routing]:
    """The stopping routings of the run behind `node`, by the launcher's rule
    (the node's run, else its id). `[]` when that run never existed."""
    try:
        run_dir = RunStore(runs_root, node.run_id or node.id).dir
    except UnsafeRunId as exc:
        raise RoutingLogUnreadable(f"not a run id: {exc}") from exc
    try:
        kind = os.lstat(run_dir)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RoutingLogUnreadable(f"{run_dir} cannot be looked at: {exc}") from exc
    if not stat.S_ISDIR(kind.st_mode):
        raise RoutingLogUnreadable(f"{run_dir} is not a directory of its own")
    return read_branch_stops(run_dir)


def _collect_logs(state: ProjectState, runs_root: Path
                  ) -> tuple[dict[str, list[Routing]], dict[str, str]]:
    """`(stops, unreadable)` for every BLOCKED or RUNNING node of `state`: the stopping
    routings of its run's log, or why the log cannot be read. A timed wait whose log
    cannot be read is in neither (its kind is its own testimony)."""
    stops: dict[str, list[Routing]] = {}
    unreadable: dict[str, str] = {}
    for node in state.nodes:
        if node.lifecycle not in (Lifecycle.BLOCKED, Lifecycle.RUNNING):
            continue
        try:
            stops[node.id] = _run_dir_stops(runs_root, node)
        except (RoutingLogUnreadable, OSError) as exc:
            waits_for_a_time = (node.lifecycle is Lifecycle.BLOCKED
                                and node.blocked_kind in TIMED_KINDS)
            if not waits_for_a_time:
                unreadable[node.id] = str(exc)
    return stops, unreadable


def _provably_independent(depends_on: Callable[[TaskNode, TaskNode], object],
                          a: TaskNode, b: TaskNode) -> bool:
    """Exactly `False` in both directions, as `hoh.frontier` decides it: an
    answer that is not `False`, or a callable that raises, is a dependency."""
    try:
        return depends_on(a, b) is False and depends_on(b, a) is False
    except Exception:                                 # noqa: BLE001 - fail closed
        return False


def plan_continuation(state: ProjectState, contract: AutonomyContract, *, wave_id: str,
                      runs_dir: Path | str,
                      depends_on: Callable[[TaskNode, TaskNode], object]) -> WaveContinuation:
    """Which READY nodes of this wave carry on beside the branches that wait.

    Reads the routing log under `runs_dir` (read-only, through
    `read_branch_stops`) of every BLOCKED and every RUNNING node -- a run that
    stopped the programme is left RUNNING by the resume loop -- and decides,
    in this order:

    1. a contract that does not hold together decides nothing: HOLD (stricter
       than `route()`, which parks the one branch it was asked about);
    2. a node whose routing says STOP_PROGRAM (`continues_past` is False), in
       any position of its log and behind any `blocked_kind`, or whose node
       says `contract_stop`: HOLD -- the whole programme stops;
    3. a routing log that cannot be read (of a RUNNING node, or of a BLOCKED
       node that is not waiting for a time): HOLD -- what it hid may be a
       stop. A timed wait's kind is its own testimony that it was not
       stopped, so a log of it that cannot be read holds nothing;
    4. otherwise the READY nodes beside every open decision
       (`DecisionFrontier.runnable`) continue, each one only if it is provably
       independent (`depends_on` answers exactly False both ways) of every
       node that is not settled and is not itself runnable: BLOCKED (any
       kind), RUNNING, CONTINGENT, READY and held behind something, READY and
       EXTERNAL. The rest are withheld with the first such node, in graph
       order, that they are not independent of.

    Raises `ContinuationError` for an argument that cannot be a wave -- a wave
    id, a contract id or an epoch the record cannot name included -- before
    anything is read, and `FrontierError` for a graph that has no frontier.
    """
    if type(wave_id) is not str or _WAVE_ID.fullmatch(wave_id) is None:
        raise ContinuationError(f"a wave id is 1 to 64 characters of letters, digits, '.', "
                                f"'_' and '-': {_show(wave_id)}")
    if not isinstance(contract, AutonomyContract):
        raise ContinuationError("the contract must be an AutonomyContract")
    if _CONTRACT_ID.fullmatch(contract.contract_id) is None \
            or not 0 <= contract.program_epoch <= _MAX_EPOCH:
        raise ContinuationError(
            f"contract {contract.contract_id[:80]!r} at epoch "
            f"{_show(contract.program_epoch)} "
            f"cannot be named by a record: an id of 1 to 128 printable ASCII characters "
            f"and an epoch of 0 to {_MAX_EPOCH}")
    if not callable(depends_on):
        raise ContinuationError("depends_on must be callable")
    if not isinstance(state, ProjectState):
        raise ContinuationError("the state must be a ProjectState")
    _a_path(runs_dir, "the runs directory")
    try:
        if not stat.S_ISDIR(os.stat(runs_dir).st_mode):
            raise ContinuationError(f"{runs_dir!r} is not a directory")
        runs_root = Path(runs_dir)
    except (OSError, TypeError, ValueError) as exc:
        # A `None`, a number, a list, bytes or a name with a NUL is an argument that is
        # not a directory, said in this module's own words, not by `os` or `pathlib`.
        raise ContinuationError(f"{_show(runs_dir)} cannot be looked at: "
                                f"{str(exc)[:200]}") from exc

    stops, unreadable = _collect_logs(state, runs_root)

    frontier = derive_frontier(state, stops=stops, unreadable=unreadable)
    stoppers = tuple(
        n.id for n in state.nodes
        if (n.lifecycle is Lifecycle.BLOCKED and n.blocked_kind == "contract_stop")
        or any(not continues_past(r.handling) for r in stops.get(n.id, ())))
    broken = tuple(n.id for n in state.nodes if n.id in unreadable)
    reason, causes = "", ()
    if coherence(contract):
        reason = CONTRACT_INCOHERENT
    elif stoppers:
        reason, causes = PROGRAMME_STOPPED, stoppers
    elif broken:
        reason, causes = ROUTING_LOG_UNREADABLE, broken

    head = {"wave_id": wave_id, "contract_id": contract.contract_id,
            "contract_digest": contract.digest(), "mode": contract.mode.value,
            "program_epoch": contract.program_epoch, "causes": causes,
            "decisions": tuple(StoppedDecision(d.node_id, d.handling)
                               for d in frontier.decisions)}
    if reason:
        return WaveContinuation(
            outcome=HOLD, reason=reason, continued=(),
            withheld=tuple(Withheld(i, reason) for i in frontier.runnable), **head)
    runnable = set(frontier.runnable)
    others = [n for n in state.nodes if not n.settled and n.id not in runnable]
    by_id = {n.id: n for n in state.nodes}
    continued: list[str] = []
    withheld: list[Withheld] = []
    for node_id in frontier.runnable:
        blocker = next((o.id for o in others
                        if not _provably_independent(depends_on, o, by_id[node_id])), None)
        if blocker is None:
            continued.append(node_id)
        else:
            withheld.append(Withheld(node_id, SEMANTIC_DEPENDENCY, blocker))
    return WaveContinuation(outcome=CONTINUE, continued=tuple(continued),
                            withheld=tuple(withheld), **head)


def _continuation_path(record_dir: Path | str, wave_id: object) -> Path:
    if type(wave_id) is not str or _WAVE_ID.fullmatch(wave_id) is None:
        raise ContinuationError(f"a wave id is 1 to 64 characters of letters, digits, '.', "
                                f"'_' and '-': {_show(wave_id)}")
    _a_path(record_dir, "the record directory")
    try:
        kind = os.lstat(record_dir)
        path = Path(record_dir) / f"continuation-{wave_id}.json"
    except (OSError, TypeError, ValueError) as exc:
        raise ContinuationError(f"{_show(record_dir)} cannot be looked at: "
                                f"{str(exc)[:200]}") from exc
    if not stat.S_ISDIR(kind.st_mode):
        raise ContinuationError(f"{record_dir} is not a directory of its own")
    return path


def record_continuation(record_dir: Path | str, continuation: WaveContinuation) -> Path:
    """Writes the record of one wave, once. Never replaces."""
    if not isinstance(continuation, WaveContinuation):
        raise ContinuationError("only a WaveContinuation is recorded")
    path = _continuation_path(record_dir, continuation.wave_id)
    data = continuation.to_bytes()
    if len(data) > MAX_CONTINUATION_BYTES:
        raise ContinuationError(f"the record is {len(data)} bytes; at most "
                                f"{MAX_CONTINUATION_BYTES} are recorded")
    try:
        durable_io.durable_create(path, data, mode=0o600)
    except FileExistsError as exc:
        raise ContinuationError(f"{path} exists: a wave's continuation is recorded "
                                f"once") from exc
    except OSError as exc:
        raise ContinuationError(f"the continuation could not be recorded: "
                                f"{str(exc)[:200]}") from exc
    return path


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"repeated key {key!r}")
        out[key] = value
    return out


def _refuse_constant(name: str) -> None:
    raise ValueError(f"{name} is not a JSON number")


def _keys(obj: object, expected: tuple[str, ...], what: str) -> dict:
    if not isinstance(obj, dict) or set(obj) != set(expected):
        got = sorted(obj) if isinstance(obj, dict) else type(obj).__name__
        raise ContinuationError(f"{what} has the fields {got}, not {sorted(expected)}"[:300])
    return obj


def parse_continuation(data: bytes) -> WaveContinuation:
    """The record these bytes hold, or `ContinuationError` saying why not."""
    if not isinstance(data, bytes):
        raise ContinuationError("a continuation record is bytes")
    if len(data) > MAX_CONTINUATION_BYTES:
        raise ContinuationError(f"a continuation record is at most "
                                f"{MAX_CONTINUATION_BYTES} bytes, this one has {len(data)}")
    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates,
                         parse_constant=_refuse_constant)
    except (ValueError, RecursionError) as exc:      # UnicodeDecodeError is a ValueError
        raise ContinuationError(f"not a JSON document: {exc}"[:300]) from exc
    top = _keys(obj, _TOP_KEYS, "a continuation record")
    if top["schema"] != CONTINUATION_SCHEMA:
        raise ContinuationError(f"the schema is {_show(top['schema'])}")
    lists = [top[k] for k in ("causes", "continued", "withheld", "decisions")]
    if not all(isinstance(v, list) for v in lists):
        raise ContinuationError("causes, continued, withheld and decisions are lists")
    withheld = []
    for row in top["withheld"]:
        row = _keys(row, _WITHHELD_KEYS, "a withheld row")
        withheld.append(Withheld(row["node_id"], row["reason"], row["blocked_by"]))
    decisions = []
    for row in top["decisions"]:
        row = _keys(row, _DECISION_KEYS, "a decision row")
        decisions.append(StoppedDecision(row["node_id"], row["handling"]))
    record = WaveContinuation(
        wave_id=top["wave_id"], contract_id=top["contract_id"],
        contract_digest=top["contract_digest"], mode=top["mode"],
        program_epoch=top["program_epoch"], outcome=top["outcome"], reason=top["reason"],
        causes=tuple(top["causes"]), continued=tuple(top["continued"]),
        withheld=tuple(withheld), decisions=tuple(decisions))
    # The last line of the reader: what it accepts is exactly what the writer writes.
    # Values are exact above; this closes what a value check cannot see -- key order,
    # indentation, an escaped spelling, `-0`, a missing or doubled final newline.
    if record.to_bytes() != data:
        raise ContinuationError("a continuation record that is valid but is not the "
                                "bytes the writer writes for those values")
    return record


def read_continuation(record_dir: Path | str, wave_id: str) -> WaveContinuation | None:
    """The record of this wave; None only when there is no such file. A link, a
    FIFO, a directory, a second name, an unreadable or oversize file, bytes that
    are not a record, or a record of another wave is `ContinuationError`."""
    path = _continuation_path(record_dir, wave_id)
    try:
        kind = os.lstat(path)
        if stat.S_ISREG(kind.st_mode) and kind.st_size > MAX_CONTINUATION_BYTES:
            raise ContinuationError(f"{path} is {kind.st_size} bytes; a record is at most "
                                    f"{MAX_CONTINUATION_BYTES}")
        data = durable_io.read_single_link(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ContinuationError(f"{path} cannot be read: {str(exc)[:200]}") from exc
    if data is None:
        return None
    record = parse_continuation(data)
    if record.wave_id != wave_id:
        raise ContinuationError(f"{path} records wave {record.wave_id!r}, not {wave_id!r}")
    return record


def continue_wave(state: ProjectState, contract: AutonomyContract, *, wave_id: str,
                  runs_dir: Path | str, depends_on: Callable[[TaskNode, TaskNode], object],
                  record_dir: Path | str) -> WaveContinuation:
    """`plan_continuation`, written down before it is returned.

    The caller continues exactly `continued` and only after this returns: a
    record that could not be written is a `ContinuationError`, and nothing
    continues without it.
    """
    continuation = plan_continuation(state, contract, wave_id=wave_id, runs_dir=runs_dir,
                                     depends_on=depends_on)
    record_continuation(record_dir, continuation)
    return continuation



# --- Routing the open external decisions of a project (V3.3 P3-31, first slice) ---------

ROUTING_SCHEMA = "hoh-decision-routing/1"
#: Where a project keeps the routings, one create-only file each, beside its state.
ROUTINGS_DIR = "decision-routings"
#: The class of open decision this slice routes: a READY node of the EXTERNAL action class
#: that nothing stands in front of (`decisionfrontier.EXTERNAL_ACTION`). It is the one class of
#: programme-level decision that **no contract has routed yet** -- the loop halts
#: `BLOCKED_EXTERNAL` without asking -- and the one for which `coherence` already demands a
#: decided route ("an irreversible action needs a decided route, not a default"). A decision a
#: run parked was routed when it parked (and `block_under_contract` marks it not delegable,
#: which `route` never approves); a person's block carries no class at all.
#:
#: What a record says of such a decision is **not** a `Handling`. `route` answers
#: RESOLVED_BY_POLICY for an escalation of SYSTEM_TWO, and every existing reader takes that word
#: as "proceed" (`may_release`: "refuses unless resolved by policy"). The record therefore says
#: HANDED_TO_RESOLVER under the key `disposition`, and carries no `Handling` value anywhere: a
#: reader that looks for one finds none, and the only word it can read as approval is
#: `approves`, which is false.
HANDED_TO_RESOLVER = "HANDED_TO_RESOLVER"
#: The escalations that name an automatic resolver, and what the record calls it. AUTO_POLICY
#: is not here on purpose: `coherence` refuses it for EXTERNAL actions, and a programme that
#: approved an irreversible action itself would be the fabricated approval `route` exists to
#: refuse.
RESOLVERS = {Escalation.SYSTEM_TWO: "SYSTEM_TWO",
             Escalation.SYSTEM_ONE_THEN_SYSTEM_TWO: "SYSTEM_ONE_THEN_SYSTEM_TWO"}
MAX_CONTRACT_FILE_BYTES = 1 << 20
MAX_ROUTING_RECORD_BYTES = 1 << 14

ROUTED, LEFT_OPEN = "ROUTED", "LEFT_OPEN"
#: Why a decision stays open. The first group is the contract's own answer or a fact about
#: the decision (exit 0); the second is a failure to read or to record (exit 1).
NOT_COVERED = "NOT_COVERED"
ALREADY_ROUTED = "ALREADY_ROUTED"
NOT_DELEGABLE = "NOT_DELEGABLE"
NODE_NOT_NAMEABLE = "NODE_NOT_NAMEABLE"
LOG_UNREADABLE = "LOG_UNREADABLE"
RECORD_UNREADABLE = "RECORD_UNREADABLE"
RECORD_CONFLICT = "RECORD_CONFLICT"
RECORD_NOT_WRITTEN = "RECORD_NOT_WRITTEN"
FAILURE_REASONS = frozenset({LOG_UNREADABLE, RECORD_UNREADABLE, RECORD_CONFLICT,
                             RECORD_NOT_WRITTEN, NODE_NOT_NAMEABLE})


class RoutingRefused(RuntimeError):
    """The command cannot say anything about this project: nothing was read of a log and
    nothing was written (a contract file that is not a contract, a digest that is not the
    one expected, a graph that has no frontier)."""


@dataclass(frozen=True)
class RoutedDecision:
    """One open decision of the project and what became of it."""

    node_id: str
    decision_digest: str
    outcome: str
    #: "" for ROUTED; for LEFT_OPEN one of the reasons above.
    reason: str = ""
    #: The `Handling` the contract's route gave for a decision that stays open (or the one a
    #: parked branch already has); "" for a ROUTED one, whose record is not a `Handling`.
    handling: str = ""
    #: HANDED_TO_RESOLVER for a ROUTED decision, else "".
    disposition: str = ""
    resolver: str = ""
    #: True only when this call created the record (a record that was already there, and
    #: any dry run, are False).
    recorded: bool = False


@dataclass(frozen=True)
class RoutingReport:
    project_id: str
    contract_id: str
    contract_digest: str
    mode: str
    program_epoch: int
    dry_run: bool
    decisions: tuple[RoutedDecision, ...]

    @property
    def failed(self) -> bool:
        return any(d.reason in FAILURE_REASONS for d in self.decisions)

    def as_json(self) -> dict:
        return {"project_id": self.project_id,
                "contract": {"id": self.contract_id, "digest": self.contract_digest,
                             "mode": self.mode, "program_epoch": self.program_epoch},
                "dry_run": self.dry_run,
                "decisions": [{"node": d.node_id, "decision_digest": d.decision_digest,
                               "outcome": d.outcome, "reason": d.reason, "handling": d.handling,
                               "disposition": d.disposition, "resolver": d.resolver,
                               "recorded": d.recorded}
                              for d in self.decisions]}


def load_contract_file(path: object, *, program_epoch: int | None = None) -> AutonomyContract:
    """The contract in a file, pinned to `program_epoch` when one is given, or
    `RoutingRefused`. The file is a regular file (a link to one is read), at most
    `MAX_CONTRACT_FILE_BYTES`, UTF-8, one JSON object that `AutonomyContract` accepts and
    `coherence` finds sound; a FIFO is refused by its type, never opened for reading."""
    if not isinstance(path, (str, os.PathLike)):
        raise RoutingRefused(f"the contract file must be a path, not {type(path).__name__}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, TypeError, ValueError) as exc:
        raise RoutingRefused(f"{_show(path)} cannot be opened: {str(exc)[:200]}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RoutingRefused(f"{_show(path)} is not a regular file")
        data = os.read(fd, MAX_CONTRACT_FILE_BYTES + 1)
    except OSError as exc:
        raise RoutingRefused(f"{_show(path)} cannot be read: {str(exc)[:200]}") from exc
    finally:
        os.close(fd)
    if len(data) > MAX_CONTRACT_FILE_BYTES:
        raise RoutingRefused(f"{_show(path)} is larger than {MAX_CONTRACT_FILE_BYTES} bytes")
    try:
        contract = AutonomyContract.model_validate_json(data.decode("utf-8"))
        if program_epoch is not None:
            if type(program_epoch) is not int or not 0 <= program_epoch <= _MAX_EPOCH:
                raise ValueError(f"--program-epoch must be an integer from 0 to {_MAX_EPOCH}")
            contract = contract.pin(program_epoch)
    except Exception as exc:
        raise RoutingRefused(f"{_show(path)} is not a usable contract: "
                             f"{str(exc)[:200]}") from exc
    return contract


def _decision_digest(project_id: str, node: TaskNode) -> str:
    """What identifies the decision: the node as the graph states it. A note edited, a
    dependency added or a node re-declared is another decision, and a record of the old
    one binds nothing about the new."""
    body = json.dumps({"project": project_id, "node": node.id, "class": node.action_class.value,
                       "note": node.note, "run": node.run_id,
                       "dependencies": list(node.dependencies)},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(body.encode("ascii")).hexdigest()[:16]


def _routing_record(project_id: str, node: TaskNode, digest: str, contract: AutonomyContract,
                    verdict: Routing, resolver: str) -> bytes:
    body = {"schema": ROUTING_SCHEMA, "project_id": project_id, "node_id": node.id,
            "decision_digest": digest, "kind": node.action_class.value.lower(),
            "disposition": HANDED_TO_RESOLVER, "escalation": verdict.escalation.value,
            "resolver": resolver, "approves": False,
            "contract_id": contract.contract_id, "contract_digest": contract.digest(),
            "mode": contract.mode.value, "program_epoch": contract.program_epoch}
    return (json.dumps(body, sort_keys=True, indent=1, ensure_ascii=True)
            + "\n").encode("ascii")


def _write_routing(directory: Path, name: str, data: bytes, *, dry_run: bool
                   ) -> tuple[str, bool]:
    """`("", recorded)` or `(reason, False)`. Never replaces: a record that is there is
    compared with what would be written and is `RECORD_CONFLICT` when it differs."""
    try:
        kind = os.lstat(directory)
    except FileNotFoundError:
        kind = None
    except OSError:
        return RECORD_NOT_WRITTEN, False
    if kind is not None and not stat.S_ISDIR(kind.st_mode):
        return RECORD_NOT_WRITTEN, False
    path = directory / name
    if kind is not None:
        try:
            if os.lstat(path).st_size > MAX_ROUTING_RECORD_BYTES:
                return RECORD_UNREADABLE, False
            there = durable_io.read_single_link(path)
        except FileNotFoundError:
            there = None
        except OSError:
            return RECORD_UNREADABLE, False
        if there is not None:
            return ("", False) if there == data else (RECORD_CONFLICT, False)
    if dry_run:
        return "", False
    try:
        if kind is None:
            os.mkdir(directory, 0o700)
        durable_io.durable_create(path, data, mode=0o600)
    except FileExistsError:
        return RECORD_CONFLICT, False
    except OSError:
        return RECORD_NOT_WRITTEN, False
    return "", True


def route_project_decisions(store, contract: AutonomyContract, runs_dir: Path | str, *,
                            expect_digest: str | None = None,
                            dry_run: bool = False) -> RoutingReport:
    """Routes the open decisions of one project through `contract` and records the ones the
    contract hands to an automatic resolver.

    For each open decision of the project (`decisionfrontier`, in its ranking):

    * a READY EXTERNAL node nothing stands in front of is **routed**: `route()` is asked
      with kind `external` and the node's own text. If it answers RESOLVED_BY_POLICY by
      escalation SYSTEM_TWO or SYSTEM_ONE_THEN_SYSTEM_TWO, a record naming the decision's
      digest, the contract's digest and the resolver is created -- once, never replaced --
      and says `approves: false`: it hands the decision to a resolver, it approves
      nothing. Any other answer (ask, collect, park, abort, stop, a class the contract does
      not allow) is the contract's decided route to a person or an end: the decision stays
      open, nothing is recorded;
    * a branch a contract already parked is `ALREADY_ROUTED`; a person's block is
      `NOT_DELEGABLE`; a log that cannot be read is `LOG_UNREADABLE`. None is re-routed.

    `expect_digest` is the contract digest the caller planned under: another is
    `RoutingRefused` before anything is read, and so is a contract the record cannot name
    (an id outside 1 to 128 printable ASCII characters, an epoch outside 0 to 2**40). A node
    id the record cannot name and an incoherent contract leave the decision open.
    """
    if not isinstance(contract, AutonomyContract):
        raise RoutingRefused("the contract must be an AutonomyContract")
    if expect_digest is not None and (type(expect_digest) is not str
                                      or expect_digest != contract.digest()):
        raise RoutingRefused(f"the contract offered digests {contract.digest()}, not the "
                             f"{_show(expect_digest)} that was expected")
    if (type(contract.contract_id) is not str
            or _CONTRACT_ID.fullmatch(contract.contract_id) is None
            or type(contract.program_epoch) is not int
            or not 0 <= contract.program_epoch <= _MAX_EPOCH):
        raise RoutingRefused(
            f"contract {contract.contract_id[:80]!r} at epoch "
            f"{_show(contract.program_epoch)} cannot be named by a record: an id of 1 to 128 "
            f"printable ASCII characters and an epoch of 0 to {_MAX_EPOCH}")
    if not isinstance(runs_dir, (str, os.PathLike)):
        raise RoutingRefused(f"the runs directory must be a path, not {type(runs_dir).__name__}")
    try:
        if not stat.S_ISDIR(os.stat(runs_dir).st_mode):
            raise RoutingRefused(f"{_show(runs_dir)} is not a directory")
    except (OSError, TypeError, ValueError) as exc:
        raise RoutingRefused(f"{_show(runs_dir)} cannot be looked at: {str(exc)[:200]}") from exc
    state = store.read_state()
    stops, unreadable = _collect_logs(state, Path(runs_dir))
    try:
        frontier = derive_frontier(state, stops=stops, unreadable=unreadable)
    except FrontierError as exc:
        raise RoutingRefused(f"the project graph has no frontier: {str(exc)[:200]}") from exc
    incoherent = bool(coherence(contract))
    by_id = {n.id: n for n in state.nodes}
    done: list[RoutedDecision] = []
    for d in frontier.decisions:
        node = by_id[d.node_id]
        digest = _decision_digest(store.project_id, node)

        def left(reason: str, handling: str = "", _id=d.node_id, _digest=digest):
            return RoutedDecision(_id, _digest, LEFT_OPEN, reason, handling)

        if d.handling == UNREADABLE:
            done.append(left(LOG_UNREADABLE))
        elif d.handling == UNKNOWN:
            done.append(left(NOT_DELEGABLE))
        elif d.handling != EXTERNAL_ACTION:
            done.append(left(ALREADY_ROUTED, d.handling))
        elif _NODE_ID.fullmatch(node.id) is None:
            done.append(left(NODE_NOT_NAMEABLE))
        elif incoherent:
            done.append(left(NOT_COVERED, "CONTRACT_INCOHERENT"))
        else:
            verdict = route(contract, decision=node.note or f"external action {node.id}",
                            kind=ActionClass.EXTERNAL.value, delegable=True)
            resolver = RESOLVERS.get(verdict.escalation) if verdict.escalation else None
            if verdict.handling is not Handling.RESOLVED_BY_POLICY or resolver is None:
                done.append(left(NOT_COVERED, verdict.handling.value))
                continue
            reason, recorded = _write_routing(
                store.dir / ROUTINGS_DIR, f"{digest}-{contract.digest()}.json",
                _routing_record(store.project_id, node, digest, contract, verdict, resolver),
                dry_run=dry_run)
            done.append(RoutedDecision(
                node.id, digest, LEFT_OPEN if reason else ROUTED, reason,
                "", HANDED_TO_RESOLVER if not reason else "", resolver, recorded))
    return RoutingReport(project_id=store.project_id, contract_id=contract.contract_id,
                         contract_digest=contract.digest(), mode=contract.mode.value,
                         program_epoch=contract.program_epoch, dry_run=dry_run,
                         decisions=tuple(done))



# --- Policy-backed continuation (V3.3 P3-32, first slice) -----------------------------------

POLICY_SCHEMA = "hoh-policy-continuation/1"
#: Where a project keeps what a wave was allowed to continue under, one create-only file per
#: wave id, beside its state.
POLICY_DIR = "policy-continuations"
MAX_POLICY_RECORD_BYTES = 1 << 20

#: The ways out of a non-terminal state (plan 04, section 8, "the long-horizon invariant"). A
#: node with none is `PROGRAM_STUCK`. `next_action` and `retry_at` are the two the product's
#: never-stuck check (`sentinel.stuck_nodes`) already knows; the other three are the plan's.
NEXT_ACTION, RETRY_AT = "next_action", "retry_at"
MACHINE_DECISION_PATH = "machine_decision_path"
PARKED_BRANCH_REASON = "parked_branch_reason"
HUMAN_GATE, TERMINAL = "human_gate", "terminal"

LIMIT_REACHED = "LIMIT_REACHED"
PROGRAM_STUCK = "PROGRAM_STUCK"
#: A contract that routes an open EXTERNAL decision to STOP_PROGRAM stops the whole programme
#: (`Handling.STOP_PROGRAM`: "the whole programme stops"), as a logged stop does.
PROGRAMME_STOPPED_ROUTE = "PROGRAMME_STOPPED"
CONTRACT_NOT_PINNED = "CONTRACT_NOT_PINNED"
CONTRACT_PIN_MISMATCH = "CONTRACT_PIN_MISMATCH"
STATE_MOVED = "STATE_MOVED"
#: A review that came due (or whose timers cannot be read) holds the wave: never a silent continue.
REVIEW_DUE = "REVIEW_DUE"
REVIEW_UNREADABLE = "REVIEW_UNREADABLE"
#: The timer file of the root is read whole; a bigger one is not read (the hold stays a hold).
MAX_TIMER_FILE_BYTES = 1 << 24


@dataclass(frozen=True)
class ModeWave:
    """What a mode does to the continuation beside a parked branch (V3.3 P3-28, P3-29)."""

    #: The most nodes the mode has in flight at once, whatever the contract's horizon allows;
    #: `None` leaves it to `horizon.max_concurrency`.
    ceiling: int | None
    #: A HOLD arms a REVIEW_DUE timer after `horizon.review_after_seconds`: in a mode where
    #: nobody is expected to look, a hold that waits for a person is a hold nobody sees.
    hold_review: bool


#: One row per mode, decided on purpose (a test pins that the keys are exactly `AutonomyMode`, so
#: a mode added later fails a test instead of falling through). SUPERVISED: the captain steers,
#: one node at a time. The three that grant autonomy run what the horizon allows; only the
#: unattended one has no one to read a hold, so only it arms the review.
MODE_WAVE: dict[AutonomyMode, ModeWave] = {
    AutonomyMode.SUPERVISED: ModeWave(ceiling=1, hold_review=False),
    AutonomyMode.POLICY_GOVERNED: ModeWave(ceiling=None, hold_review=False),
    AutonomyMode.PLAN_AUTONOMOUS: ModeWave(ceiling=None, hold_review=False),
    AutonomyMode.UNATTENDED_LONG_HORIZON: ModeWave(ceiling=None, hold_review=True),
}
#: The row a profile that cannot be read is held to.
MOST_CONSERVATIVE_MODE = AutonomyMode.SUPERVISED


def _profile(contract) -> tuple[str, ModeWave, int, bool]:
    """`(mode, its row, nodes in flight, fell back)` of a contract, fail closed.

    A mode the table has no row for, or a concurrency that is not a whole number of at least
    one, is read as the most conservative mode's row and one node: a profile that cannot be
    read never gets more than the strictest mode would give."""
    mode = getattr(contract, "mode", None)
    row = MODE_WAVE.get(mode) if type(mode) is AutonomyMode else None
    fell_back = row is None
    if row is None:
        mode, row = MOST_CONSERVATIVE_MODE, MODE_WAVE[MOST_CONSERVATIVE_MODE]
    width = getattr(getattr(contract, "horizon", None), "max_concurrency", None)
    if type(width) is not int or width < 1:
        width, fell_back = 1, True
    if row.ceiling is not None:
        width = min(width, row.ceiling)
    return mode.value, row, width, fell_back


def mode_width(contract) -> int:
    """How many nodes a wave continues in one pass beside a parked branch under this contract:
    its horizon's `max_concurrency` through its mode's row (`MODE_WAVE`), 1 when unreadable."""
    return _profile(contract)[2]


@dataclass(frozen=True)
class PolicyContinuation:
    """What one wave may continue under the contract, and why not the rest. `to_bytes` is the
    record: ASCII, sorted keys, no free text of a node or of the contract."""

    project_id: str
    wave_id: str
    at: str
    contract: dict
    state: dict
    outcome: str
    reason: str
    limit_policy: str
    continued: tuple[str, ...]
    withheld: tuple[tuple[str, str, str], ...]
    routings: tuple[tuple[str, str, str], ...]
    ways_out: dict
    stuck: tuple[tuple[str, str], ...]
    limits: dict
    #: node -> the `Handling` value (or HANDED_TO_RESOLVER) the way out rests on, where one does.
    handlings: dict
    #: nodes whose way out is only what the contract *would* do -- nothing parks, aborts, asks
    #: or triggers them yet -- and why.
    not_enacted: tuple[tuple[str, str], ...]
    #: The mode's row as it was applied: the mode, the width, and the state of the review timer.
    mode_wave: dict = field(default_factory=dict)
    #: P3-32 slice 3: what the loop did about a reached limit (`act_on_limit`); empty, and then not
    #: in the record at all, for every record that is not that.
    act: dict = field(default_factory=dict)

    def to_bytes(self) -> bytes:
        body = {
            "schema": POLICY_SCHEMA, "project_id": self.project_id, "wave_id": self.wave_id,
            "at": self.at, "contract": self.contract, "state": self.state,
            "outcome": self.outcome, "reason": self.reason, "limit_policy": self.limit_policy,
            "continued": list(self.continued),
            "withheld": [{"node": n, "reason": r, "blocked_by": b} for n, r, b in self.withheld],
            "routings": [{"node": n, "decision_digest": d, "file": f}
                         for n, d, f in self.routings],
            "ways_out": self.ways_out, "handlings": self.handlings,
            "not_enacted": [{"node": n, "why": w} for n, w in self.not_enacted],
            "stuck": [{"node": n, "why": w} for n, w in self.stuck],
            "limits": self.limits, "mode_wave": self.mode_wave, "approves": False,
        }
        if self.act:
            body["act"] = self.act
        return (json.dumps(body, sort_keys=True, indent=1, ensure_ascii=True)
                + "\n").encode("ascii")

    def as_json(self) -> dict:
        return json.loads(self.to_bytes())


def _pinned_digests(state: ProjectState, runs_root: Path) -> tuple[set[str], str]:
    """`(the digests the project's runs are pinned to, "")`, or `(set(), why)` when a pin
    cannot be read. A node's run is `node.run_id or node.id` -- the launcher's rule and the one
    `_run_dir_stops` reads the logs by -- so a node with no `run_id` whose directory is pinned
    to another contract is a mismatch like any other. A run with no `autonomy/contract.json`
    is not pinned and contributes nothing; a pin that is damaged, linked or digest-mismatched
    (`bind`) is a refusal. An explicit `run_id` that is not a run id is a refusal; a node *id*
    that is not one (it is not a run, then) is skipped."""
    out: set[str] = set()
    for node in state.nodes:
        run_id = node.run_id or node.id
        try:
            run_dir = RunStore(runs_root, run_id).dir
        except UnsafeRunId:
            if node.run_id:
                return set(), f"{_show(node.run_id)} is not a run id"
            continue
        if not os.path.lexists(run_dir):
            continue
        try:
            governor = bind(run_dir)
        except ContractRefused as exc:
            return set(), f"the pin of run {_show(run_id)} is refused: {str(exc)[:200]}"
        if governor is not None:
            out.add(governor.contract.digest())
    return out, ""


def _spent_dispatches(runs_root: Path) -> int:
    """Dispatches every run under the root has spent, from the persisted run states, by the
    launcher's own reader (`_dispatches_in`: no link, no FIFO, a whole number). A state that
    cannot be read raises `OSError`/`ValueError`: a spend that cannot be read is not zero."""
    from .launcher import HohRunLauncher

    total = 0
    for path in HohRunLauncher._state_files(runs_root):
        total += HohRunLauncher._dispatches_in(path)
    return total


def _seconds_since(stamp: str, now: str) -> int:
    from datetime import UTC, datetime

    fmt = "%Y-%m-%dT%H:%M:%SZ"
    a = datetime.strptime(stamp, fmt).replace(tzinfo=UTC)
    b = datetime.strptime(now, fmt).replace(tzinfo=UTC)
    seconds = int((b - a).total_seconds())
    if seconds < 0:
        raise ValueError("the creation time is in the future")
    return seconds


def _least(*values: int | None) -> int | None:
    """The smallest of those that are set: the horizon's and the budget's wallclock are both
    limits, and the stricter one binds."""
    named = [v for v in values if v is not None]
    return min(named) if named else None


def _state_binding(store, state: ProjectState) -> dict:
    """What the record is bound to: the three counters of the project state and the epoch of
    the authority's own chain. `state.master_epoch` is the epoch of the last *writer* of the
    state, not of the lease; the lease's is the chain's last (`EpochStore`), 0 for a project
    that has none."""
    from .epoch import EpochError, EpochStore

    try:
        current = EpochStore(store.dir).current()
    except EpochError as exc:
        raise RoutingRefused(f"the epoch chain cannot be read: {str(exc)[:200]}") from exc
    return {"write_seq": state.write_seq, "master_epoch": state.master_epoch,
            "program_generation": state.program_generation,
            "authority_epoch": current.epoch if current is not None else 0}


def _human_gate_allowed(contract: AutonomyContract, kind: str) -> bool:
    """A person is a legitimate way out when one is expected (Needs-You ON or DIGEST_ONLY), or
    when the contract names this kind a hard human gate (plan 04: "unless the AutonomyContract
    explicitly permits a hard human gate")."""
    return contract.needs_you is not NeedsYou.OFF or kind in contract.hard_human_gates


def _way_out(node: TaskNode, contract: AutonomyContract, *, stops, unreadable: dict,
             reachable: set[str], routed_valid: dict[str, str]) -> tuple[str, str, str, str]:
    """`(way, detail, handling, not_enacted)`: how this non-terminal node gets out, or `""` and
    why not. `handling` is the `Handling` (or HANDED_TO_RESOLVER) the way rests on, where it
    rests on one; `not_enacted` says the way is only what the contract *would* do (nothing
    parks, aborts, asks or triggers the node yet) -- "" for a way that is already enacted."""
    if node.lifecycle is Lifecycle.RUNNING:
        return NEXT_ACTION, "the run", "", ""
    if node.lifecycle is Lifecycle.CONTINGENT:
        return NEXT_ACTION, "its trigger", "", "NO_TRIGGER_CHECKED"
    if node.lifecycle is Lifecycle.READY:
        if node.action_class is not ActionClass.EXTERNAL or node.id not in reachable:
            return NEXT_ACTION, "the frontier", "", ""
        # an EXTERNAL decision nothing stands in front of: the contract's decided route
        if node.id in routed_valid:
            return MACHINE_DECISION_PATH, routed_valid[node.id], HANDED_TO_RESOLVER, ""
        verdict = route(contract, decision=node.note or f"external action {node.id}",
                        kind=ActionClass.EXTERNAL.value, delegable=True)
        if verdict.handling is Handling.RESOLVED_BY_POLICY:
            return ("", ("handed to a resolver by the contract but no valid routing record "
                         "exists"), verdict.handling.value, "")
        way, detail = _handling_way(verdict.handling, contract, ActionClass.EXTERNAL.value)
        return way, detail, verdict.handling.value, _WOULD.get(verdict.handling, "") if way else ""
    # BLOCKED
    if node.blocked_kind in TIMED_KINDS:
        if node.retry_after:
            return RETRY_AT, "a timed wait", "", ""
        return "", f"a timed wait ({node.blocked_kind}) with no retry time", "", ""
    if node.id in unreadable:
        return "", "its routing log cannot be read", "", ""
    recs = stops.get(node.id) or []
    if recs:
        way, detail = _handling_way(recs[-1].handling, contract, recs[-1].kind)
        return way, detail, recs[-1].handling.value, ""
    if _human_gate_allowed(contract, "decision") and node.note:
        return HUMAN_GATE, "a person blocked it and the contract allows one", "", ""
    return "", "blocked with no routing, no timer and no human gate the contract allows", "", ""


#: What a way out that rests on the contract's *route* (no run logged it) amounts to.
_WOULD = {Handling.PARK_BRANCH: "CONTRACT_WOULD_PARK", Handling.ABORT_BRANCH: "CONTRACT_WOULD_ABORT",
          Handling.STOP_PROGRAM: "CONTRACT_WOULD_STOP", Handling.ASK_NOW: "CONTRACT_WOULD_ASK",
          Handling.COLLECT_FOR_DIGEST: "CONTRACT_WOULD_COLLECT"}


def _handling_way(handling: Handling, contract: AutonomyContract, kind: str) -> tuple[str, str]:
    if handling is Handling.PARK_BRANCH:
        return PARKED_BRANCH_REASON, handling.value
    if handling in (Handling.ABORT_BRANCH, Handling.STOP_PROGRAM):
        return TERMINAL, handling.value
    if handling in (Handling.ASK_NOW, Handling.COLLECT_FOR_DIGEST):
        if _human_gate_allowed(contract, kind):
            return HUMAN_GATE, handling.value
        return "", f"{handling.value} but the contract allows no human gate"
    return "", f"{handling.value} is not a way out for a waiting node"


def _read_routing_records(directory: Path) -> dict[str, tuple[str, bytes]]:
    """`{file name: (name, bytes)}` of the routing records beside the state. Names that are not
    `<16 hex>-<16 hex>.json`, links, FIFOs, second names and oversize files are not records:
    each is refused by omission *and* listed by the caller as not valid."""
    out: dict[str, tuple[str, bytes]] = {}
    try:
        kind = os.lstat(directory)
    except FileNotFoundError:
        return out
    if not stat.S_ISDIR(kind.st_mode):
        raise RoutingRefused(f"{_show(str(directory))} is not a directory of its own")
    for entry in sorted(os.listdir(directory)):
        if re.fullmatch(r"[0-9a-f]{16}-[0-9a-f]{16}\.json", entry) is None:
            continue
        path = directory / entry
        try:
            if os.lstat(path).st_size > MAX_ROUTING_RECORD_BYTES:
                continue
            data = durable_io.read_single_link(path)
        except OSError:
            continue
        if data is not None:
            out[entry] = (entry, data)
    return out


def continue_by_policy(store, contract: AutonomyContract, runs_dir: Path | str, *,
                       wave_id: str, depends_on: Callable[[TaskNode, TaskNode], object] | None = None,
                       dry_run: bool = False) -> PolicyContinuation:
    """What this wave may continue under `contract`, written down before it is returned.

    The plan's words (04, "Long-horizon invariant", "Budget/horizon", "True non-delegable
    gates"): every non-terminal state has a way out, a limit is met by the policy's
    `on_limit`, and unrelated READY work continues beside a branch that waits. In this order,
    the first that applies decides, and a mismatch is never read as permission:

    1. **The contract is pinned.** The runs of the project that are pinned (`bind`) must all be
       pinned to this contract's digest, and at least one must be: the offered file is not an
       authority, the runs' own pins are. `CONTRACT_NOT_PINNED`, `CONTRACT_PIN_MISMATCH`.
    2. **The limits.** Dispatches spent under the root (persisted run states) against
       `budgets.max_dispatches`; wallclock since the project's record was created against
       `horizon.max_wallclock_seconds` (else `budgets.max_wallclock_seconds`). Reached or
       unreadable: HOLD `LIMIT_REACHED` carrying `horizon.on_limit` (REPLAN, DEGRADE, PARK,
       STOP) -- nothing continues; what replan or degrade then do is a later slice.
    3. **A way out for every non-terminal node.** The routing records of P3-31 count as a
       `machine_decision_path` only when they are exactly what the contract would write for
       the decision as it is now (`_routing_record`: decision digest re-derived, contract
       digest in the name, `approves` false). A node with no way out: HOLD `PROGRAM_STUCK`.
    4. **The wave.** `plan_continuation` (P3-28): a stop, an unreadable log or an incoherent
       contract holds it; otherwise what is independent of every node that waits continues,
       at most `horizon.max_concurrency` of it, through the mode's row of `MODE_WAVE`
       (`mode_width`).
    5. **The mode.** `apply_mode` (P3-28, P3-29): UNATTENDED_LONG_HORIZON keeps a REVIEW_DUE
       timer for its holds; a review that is due, or timers that cannot be read, hold a wave
       that would continue.

    A routed decision is **never** continued: an EXTERNAL node is not runnable work, and the
    record approves nothing. Raises `RoutingRefused` (nothing read or written) for arguments
    that cannot be a wave; `ContinuationError` when the record cannot be written, in which
    case nothing continues.
    """
    if type(wave_id) is not str or _WAVE_ID.fullmatch(wave_id) is None:
        raise RoutingRefused(f"a wave id is 1 to 64 characters of letters, digits, '.', '_' "
                             f"and '-': {_show(wave_id)}")
    if not isinstance(contract, AutonomyContract):
        raise RoutingRefused("the contract must be an AutonomyContract")
    if (type(contract.contract_id) is not str
            or _CONTRACT_ID.fullmatch(contract.contract_id) is None
            or type(contract.program_epoch) is not int
            or not 0 <= contract.program_epoch <= _MAX_EPOCH):
        raise RoutingRefused(f"contract {contract.contract_id[:80]!r} at epoch "
                             f"{_show(contract.program_epoch)} cannot be named by a record")
    if not isinstance(runs_dir, (str, os.PathLike)):
        raise RoutingRefused(f"the runs directory must be a path, not {type(runs_dir).__name__}")
    try:
        if not stat.S_ISDIR(os.stat(runs_dir).st_mode):
            raise RoutingRefused(f"{_show(runs_dir)} is not a directory")
    except (OSError, TypeError, ValueError) as exc:
        raise RoutingRefused(f"{_show(runs_dir)} cannot be looked at: {str(exc)[:200]}") from exc
    runs_root = Path(runs_dir)
    if depends_on is None:
        from .launcher import HohRunLauncher

        def depends_on(a: TaskNode, b: TaskNode) -> object:       # the launcher's rule
            return HohRunLauncher.depends_on(None, a, b)

    state = store.read_state()
    now = utcnow()
    for node in state.nodes:
        if not node.settled and _NODE_ID.fullmatch(node.id) is None:
            raise RoutingRefused(f"node {_show(node.id)} cannot be named by a record")
    project_id = store.project_id
    bound = _state_binding(store, state)
    head = {"id": contract.contract_id, "digest": contract.digest(),
            "mode": contract.mode.value, "program_epoch": contract.program_epoch}
    limits: dict = {"dispatches": {"max": contract.budgets.max_dispatches, "spent": None},
                    "wallclock_seconds": {
                        "max": _least(contract.horizon.max_wallclock_seconds,
                                      contract.budgets.max_wallclock_seconds),
                        "horizon": contract.horizon.max_wallclock_seconds,
                        "budget": contract.budgets.max_wallclock_seconds, "elapsed": None},
                    "concurrency": {"max": contract.horizon.max_concurrency, "running": None}}

    def hold(reason: str, *, limit_policy: str = "", ways=None, stuck=(), withheld=(),
             routings=(), handlings=None, not_enacted=()) -> PolicyContinuation:
        return PolicyContinuation(
            project_id=project_id, wave_id=wave_id, at=now, contract=head, state=bound,
            outcome="HOLD", reason=reason, limit_policy=limit_policy, continued=(),
            withheld=tuple(withheld), routings=tuple(routings), ways_out=ways or {},
            stuck=tuple(stuck), limits=limits, handlings=handlings or {},
            not_enacted=tuple(not_enacted))

    result: PolicyContinuation
    pins, why = _pinned_digests(state, runs_root)
    if why:
        result = hold(CONTRACT_PIN_MISMATCH)
    elif not pins:
        result = hold(CONTRACT_NOT_PINNED)
    elif pins != {contract.digest()}:
        result = hold(CONTRACT_PIN_MISMATCH)
    else:
        result = _continue_under_pin(store, state, contract, runs_root, wave_id, depends_on,
                                     now, hold, limits, head, bound, project_id)
    result, due = apply_mode(contract, runs_root, result, now, dry_run=dry_run)
    if dry_run:
        return result
    _write_policy_record(store, state, result)
    _fire_reviews(runs_root, due)
    return result


def _read_timers(runs_root: Path):
    """`(store, its timers, "")` of the root, or `(store, [], why)` when they cannot be read.
    An absent file is no timers; a link, a FIFO, a directory, a second name, a file over
    `MAX_TIMER_FILE_BYTES` or a store `TimerStore` refuses is *unreadable*, never empty."""
    from .timers import TimerStore, TimerStoreError

    timers = TimerStore(runs_root)
    try:
        try:
            size = os.lstat(timers.path).st_size
        except FileNotFoundError:
            return timers, [], ""
        if size > MAX_TIMER_FILE_BYTES:
            return timers, [], f"{size} bytes: at most {MAX_TIMER_FILE_BYTES} are read"
        durable_io.read_single_link(timers.path)
        return timers, timers.read(), ""
    except (OSError, TimerStoreError, ValueError, RecursionError) as exc:
        return timers, [], f"{type(exc).__name__}: {str(exc)[:100]}"


def _later(stamp: str, seconds: int) -> str:
    from datetime import UTC, datetime

    from .timers import in_seconds

    moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return in_seconds(seconds, now=moment)


def _reviews_of(contract: AutonomyContract, project_id: str, timers) -> list:
    """The armed REVIEW_DUE timers of this project and this programme epoch."""
    from .timers import TimerKind, TimerStatus

    return [t for t in timers
            if t.kind is TimerKind.REVIEW_DUE and t.status is TimerStatus.ARMED
            and t.program_id == project_id and t.program_epoch == contract.program_epoch]


def _is_due(timer, now: str) -> bool:
    from .timers import check_time

    try:
        check_time(timer.due_at)
    except ValueError:                      # a time that cannot be read cannot be said not to
        return True                         # have passed
    return timer.is_due(now)


def review_is_due(contract: AutonomyContract, runs_dir: Path | str, project_id: str,
                  now: str | None = None) -> bool:
    """Must a pass decide this wave again although a record of it exists? True when the mode
    reviews its holds and a review of this project is due or the timers cannot be read -- the
    two cases in which `apply_mode` would hold a wave that would continue. Reads, writes nothing.

    A caller that names a wave by the state it decided (P3-30's `c<write_seq>-<digest>`) and
    skips a wave whose record exists never reaches `apply_mode` again at that state, and a
    review that comes due while the programme sits held would never be seen. Such a caller asks
    this first, and decides again under a new wave id when it answers True."""
    _mode, row, _width, _fell_back = _profile(contract)
    if not row.hold_review:
        return False
    _timers_store, timers, unreadable = _read_timers(Path(runs_dir))
    moment = now if now is not None else utcnow()
    due = [t for t in _reviews_of(contract, project_id, timers) if _is_due(t, moment)]
    return bool(unreadable) or bool(due)


def apply_mode(contract: AutonomyContract, runs_root: Path, result: PolicyContinuation,
               now: str, *, dry_run: bool) -> tuple[PolicyContinuation, list[str]]:
    """The mode's row applied to a decision (V3.3 P3-28, P3-29): `(result, due)`, `due` the ids
    of the review timers this pass found due, to be fired once the record is written.

    A mode whose row has `hold_review` (UNATTENDED_LONG_HORIZON) keeps one REVIEW_DUE timer
    per project and programme epoch in the root's timer file, armed by the first HOLD after
    `horizon.review_after_seconds` (a later hold does not move it), and read by every pass:

    * a timer that is due holds a wave that would continue (`REVIEW_DUE`) and is fired after
      the record is written -- once, so the pass after it is a normal one;
    * timers that cannot be read hold a wave that would continue (`REVIEW_UNREADABLE`);
    * a hold whose timer could not be armed says so (`NOT_ARMED`) and asks for a person.

    Other modes read no timer and write none. Nothing here lets a wave continue that the
    decision above held."""
    mode, row, width, fell_back = _profile(contract)
    review = {"state": "NONE", "timer": "", "due_at": "", "needs_you": False, "detail": ""}
    block = {"mode": mode, "fell_back": fell_back, "width": width, "review": review}
    if not row.hold_review:
        return replace(result, mode_wave=block), []
    from .timers import TimerKind, TimerStoreError

    timers_store, timers, unreadable = _read_timers(runs_root)
    mine = _reviews_of(contract, result.project_id, timers)
    due = sorted((t for t in mine if _is_due(t, now)), key=lambda t: (t.due_at, t.timer_id))
    if unreadable:
        review.update(state="UNREADABLE", needs_you=True, detail=unreadable)
        return _hold_over(result, REVIEW_UNREADABLE, block), []
    if due:
        review.update(state="DUE", timer=due[0].timer_id, due_at=due[0].due_at, needs_you=True)
        return _hold_over(result, REVIEW_DUE, block), [t.timer_id for t in due]
    if mine:
        review.update(state="PENDING", timer=mine[0].timer_id, due_at=mine[0].due_at)
    elif result.outcome != "HOLD":
        review.update(state="IDLE")
    elif dry_run:
        review.update(state="WOULD_ARM")
    else:
        try:
            seconds = contract.horizon.review_after_seconds
            if type(seconds) is not int or seconds < 1:
                raise ValueError("the contract names no review interval")
            due_at = _later(now, seconds)
            armed = timers_store.arm_new(
                kind=TimerKind.REVIEW_DUE, due_at=due_at, program_id=result.project_id,
                program_epoch=contract.program_epoch,
                reason=f"review of the hold of wave {result.wave_id}: {result.reason}")
            review.update(state="ARMED", timer=armed.timer_id, due_at=due_at)
        except (TimerStoreError, OSError, ValueError, OverflowError) as exc:
            review.update(state="NOT_ARMED", needs_you=True,
                          detail=f"{type(exc).__name__}: {str(exc)[:100]}")
    return replace(result, mode_wave=block), []


def _hold_over(result: PolicyContinuation, reason: str, block: dict) -> PolicyContinuation:
    """`result`, held for `reason` if it would have continued; a hold keeps its own reason."""
    if result.outcome == "HOLD":
        return replace(result, mode_wave=block)
    return replace(result, outcome="HOLD", reason=reason, continued=(), mode_wave=block,
                   withheld=result.withheld + tuple((n, reason, "") for n in result.continued))


def _fire_reviews(runs_root: Path, due: list[str]) -> None:
    """Fire the review timers the record just named. A timer that cannot be fired stays armed
    and due: the next pass holds again (`REVIEW_DUE`), loudly, which is the safe direction."""
    from .timers import TimerStore, TimerStoreError

    store = TimerStore(runs_root)
    for timer_id in due:
        try:
            store.fire(timer_id)
        except (TimerStoreError, OSError, ValueError):
            continue


def _limits_reached(state: ProjectState, now: str, runs_root: Path, limits: dict) -> bool:
    """Is a dispatch or a wallclock limit of the record's `limits` reached? Fills `spent` and `elapsed` as it reads
    them. A spend that cannot be read, and an elapsed time that cannot be (a creation time that is unparsable or lies in
    the future), are reached: a limit that is named and cannot be measured is not "not reached"."""
    spent_known = True
    try:
        spent = _spent_dispatches(runs_root)
        limits["dispatches"]["spent"] = spent
    except (OSError, ValueError, RecursionError):
        spent_known, spent = False, 0
    try:
        elapsed = _seconds_since(state.created_at, now)
        limits["wallclock_seconds"]["elapsed"] = elapsed
    except ValueError:
        elapsed = None                      # unparsable or in the future: not "just created"
    max_d = limits["dispatches"]["max"]
    max_w = limits["wallclock_seconds"]["max"]
    return ((max_d is not None and (not spent_known or spent >= max_d))
            or (max_w is not None and (elapsed is None or elapsed >= max_w)))


def limit_reached(state: ProjectState, contract: AutonomyContract, runs_dir: Path | str) -> bool:
    """P3-32 slice 3, revision 1: is a dispatch or wallclock limit of the contract reached *now*? The reading
    `continue_by_policy` makes (the same `_limits_reached`), for the loop's plain rounds: they ask it before every
    dispatch, so the limits bind the whole loop and not only the pass beside a parked branch. Reads, writes nothing.
    True also when a limit is named and cannot be measured. A contract that names no limit has none to reach."""
    limits: dict = {"dispatches": {"max": contract.budgets.max_dispatches, "spent": None},
                    "wallclock_seconds": {"max": _least(contract.horizon.max_wallclock_seconds,
                                                        contract.budgets.max_wallclock_seconds), "elapsed": None}}
    return _limits_reached(state, utcnow(), Path(runs_dir), limits)


def _continue_under_pin(store, state, contract, runs_root, wave_id, depends_on, now, hold,
                        limits, head, bound, project_id, *, waive_limits: bool = False,
                        width_cap: int | None = None,
                        only: frozenset[str] | None = None) -> PolicyContinuation:
    """The decision under a pin that holds. `waive_limits`, `width_cap` and `only` are the three
    things `act_on_limit` changes about it (the limit check is not made, the wave is at most that
    narrow, only those nodes may continue); a call without them is the decision of slice 1."""
    # 2. the limits
    reached = _limits_reached(state, now, runs_root, limits)
    if reached and not waive_limits:
        return hold(LIMIT_REACHED, limit_policy=contract.horizon.on_limit.value)

    # 3. a way out for every non-terminal node
    stops, unreadable = _collect_logs(state, runs_root)
    try:
        frontier = derive_frontier(state, stops=stops, unreadable=unreadable)
    except FrontierError as exc:
        raise RoutingRefused(f"the project graph has no frontier: {str(exc)[:200]}") from exc
    reachable = {n.id for n in state.ready()}
    by_id = {n.id: n for n in state.nodes}
    records = _read_routing_records(store.dir / ROUTINGS_DIR)
    routed_valid: dict[str, str] = {}
    routings: list[tuple[str, str, str]] = []
    if not (coherence(contract)):
        for d in frontier.decisions:
            if d.handling != EXTERNAL_ACTION:
                continue
            node = by_id[d.node_id]
            digest = _decision_digest(project_id, node)
            verdict = route(contract, decision=node.note or f"external action {node.id}",
                            kind=ActionClass.EXTERNAL.value, delegable=True)
            resolver = RESOLVERS.get(verdict.escalation) if verdict.escalation else None
            if verdict.handling is not Handling.RESOLVED_BY_POLICY or resolver is None:
                continue
            name = f"{digest}-{contract.digest()}.json"
            expected = _routing_record(project_id, node, digest, contract, verdict, resolver)
            if name in records and records[name][1] == expected:
                routed_valid[node.id] = name
                routings.append((node.id, digest, name))
    ways: dict[str, str] = {}
    handlings: dict[str, str] = {}
    not_enacted: list[tuple[str, str]] = []
    stuck: list[tuple[str, str]] = []
    for node in state.nodes:
        if node.settled:
            continue
        way, detail, handling, would = _way_out(
            node, contract, stops=stops, unreadable=unreadable, reachable=reachable,
            routed_valid=routed_valid)
        if way:
            ways[node.id] = way
            if handling:
                handlings[node.id] = handling
            if would:
                not_enacted.append((node.id, would))
        else:
            stuck.append((node.id, detail[:120]))
            if handling:
                handlings[node.id] = handling
    if stuck:
        return hold(PROGRAM_STUCK, ways=ways, stuck=stuck, routings=routings,
                    handlings=handlings, not_enacted=not_enacted)
    if any(handlings.get(n) == Handling.STOP_PROGRAM.value and ways.get(n) == TERMINAL
           for n in ways):
        # a contract that routes an external decision to STOP_PROGRAM: the whole programme stops
        return hold(PROGRAMME_STOPPED_ROUTE, ways=ways, routings=routings, handlings=handlings,
                    not_enacted=not_enacted,
                    withheld=[(n, PROGRAMME_STOPPED_ROUTE, "") for n in plan_runnable(state)])

    # 4. the wave
    plan = plan_continuation(state, contract, wave_id=wave_id, runs_dir=runs_root,
                             depends_on=depends_on)
    if plan.outcome == HOLD:
        return hold(plan.reason, ways=ways, routings=routings, handlings=handlings,
                    not_enacted=not_enacted,
                    withheld=[(w.node_id, w.reason, w.blocked_by) for w in plan.withheld])
    # `max_concurrency` is how many nodes may be in flight, through the mode's row: what is
    # already RUNNING counts
    cap = mode_width(contract)
    if width_cap is not None:
        cap = min(cap, width_cap)
    running = sum(1 for n in state.nodes if n.lifecycle is Lifecycle.RUNNING)
    limits["concurrency"]["running"] = running
    room = max(cap - running, 0)
    pool = plan.continued if only is None else tuple(n for n in plan.continued if n in only)
    continued = tuple(pool[:room])
    withheld = [(w.node_id, w.reason, w.blocked_by) for w in plan.withheld]
    withheld += [(n, "CONCURRENCY_CAP", "") for n in pool[room:]]
    if only is not None:
        withheld += [(n, REPLAN_PENDING, "") for n in plan.continued if n not in only]
    return PolicyContinuation(
        project_id=project_id, wave_id=wave_id, at=now, contract=head, state=bound,
        outcome="CONTINUE", reason="", limit_policy="", continued=continued,
        withheld=tuple(withheld), routings=tuple(routings), ways_out=ways, stuck=(),
        limits=limits, handlings=handlings, not_enacted=tuple(not_enacted))


def plan_runnable(state: ProjectState) -> list[str]:
    """The READY nodes of the graph that are not EXTERNAL (what a stop of the programme
    withholds)."""
    return [n.id for n in state.ready() if n.action_class is not ActionClass.EXTERNAL]


def _write_policy_record(store, state: ProjectState, result: PolicyContinuation, *,
                         directory: str = POLICY_DIR, name: str | None = None) -> None:
    """Creates the wave's record once, after looking at the state again: a state that moved
    since it was read (another writer under the same lock discipline is impossible; a
    process outside it is not) is `STATE_MOVED` and nothing is written. `directory` and `name`
    are where the record of an act on a limit goes (`act_on_limit`)."""
    data = result.to_bytes()
    if len(data) > MAX_POLICY_RECORD_BYTES:
        raise ContinuationError(f"the record is {len(data)} bytes; at most "
                                f"{MAX_POLICY_RECORD_BYTES} are recorded")
    again = store.read_state()
    if _state_binding(store, again) != result.state:
        raise ContinuationError(f"{STATE_MOVED}: the project state changed while the wave was "
                                f"planned; nothing was recorded")
    directory = store.dir / directory
    path = directory / (name or f"{result.wave_id}.json")
    try:
        try:
            kind = os.lstat(directory)
        except FileNotFoundError:
            os.mkdir(directory, 0o700)
        else:
            if not stat.S_ISDIR(kind.st_mode):
                raise ContinuationError(f"{_show(str(directory))} is not a directory of its own")
        durable_io.durable_create(path, data, mode=0o600)
    except FileExistsError as exc:
        raise ContinuationError(f"{_show(str(path))} exists: a wave's continuation is "
                                f"recorded once") from exc
    except OSError as exc:
        raise ContinuationError(f"the continuation could not be recorded: "
                                f"{str(exc)[:200]}") from exc


# --- Acting on the limit policies (V3.3 P3-32, third slice) ------------------------------------

#: Where a project keeps what the loop did about a reached limit whose policy is DEGRADE or REPLAN, one
#: create-only file per (wave id, kind) beside its state: `<wave id>.degrade.json`, `.replan.json`,
#: `.none.json`. The names are the counters (`_acts_made`): a file exists before what it counts.
LIMIT_ACTS_DIR = "limit-acts"
#: A directory with more entries than this is not read: it is not a project's own.
MAX_LIMIT_ACT_ENTRIES = 4096
#: What a limit whose policy is DEGRADE allows: waves of this width, at most this many of them per
#: contract (the digest includes the programme epoch). Then the limit holds as PARK does.
DEGRADED_WIDTH = 1
MAX_DEGRADED_WAVES = 3
#: What a limit whose policy is REPLAN asks for: one node `replan-<contract digest>` whose run writes the
#: revised plan, its specification kept in `replan-specs/` beside the state.
REPLAN_PREFIX = "replan-"
REPLAN_SPECS_DIR = "replan-specs"
MAX_REPLAN_LISTED = 200
ACTING_POLICIES = frozenset({OnLimit.DEGRADE, OnLimit.REPLAN})

DEGRADE, REPLAN_REQUEST, REPLAN_STEP, NO_ACT = "DEGRADE", "REPLAN_REQUEST", "REPLAN_STEP", "NONE"
#: Why a limit is not acted on (the record of the act says `NONE` and one of these).
NOT_A_LIMIT_ACT = "NOT_A_LIMIT_ACT"
LIMIT_UNREADABLE = "LIMIT_UNREADABLE"
REVIEW_PENDING = "REVIEW_PENDING"
NOTHING_TO_NARROW = "NOTHING_TO_NARROW"
DEGRADE_SPENT = "DEGRADE_SPENT"
REPLAN_SPENT = "REPLAN_SPENT"
REPLAN_NOT_READY = "REPLAN_NOT_READY"
REPLAN_PENDING = "REPLAN_PENDING"
NOTHING_TO_CONTINUE = "NOTHING_TO_CONTINUE"
ACTS_UNREADABLE = "ACTS_UNREADABLE"


@dataclass(frozen=True)
class LimitAct:
    """What `act_on_limit` decided. `plan` is the record of it (the one written; for a REPLAN_REQUEST,
    the hold it rests on); `node` is the replanning step the caller must add to the graph."""

    kind: str
    reason: str
    plan: PolicyContinuation
    node: TaskNode | None = None

    @property
    def continued(self) -> tuple[str, ...]:
        """The nodes the pass may dispatch: those of a DEGRADE or a REPLAN_STEP, else none."""
        return self.plan.continued if self.kind in (DEGRADE, REPLAN_STEP) else ()

    def describe(self) -> str:
        return (f"{self.kind}{': ' + self.reason if self.reason else ''}; continued "
                f"{list(self.continued)}; withheld {[w[0] for w in self.plan.withheld]}")


def _acts_made(directory: Path, digest: str) -> tuple[dict[str, int], str]:
    """`({kind: count}, "")` of the acts recorded under this contract digest, counted by name, or
    `({}, why)` when the directory cannot be read (a link, a file, too many entries). A name that
    matches is counted whatever it is: a file put there by someone else only makes the allowance smaller."""
    out = {"degrade": 0, "replan": 0, "none": 0}
    try:
        kind = os.lstat(directory)
    except FileNotFoundError:
        return out, ""
    except OSError as exc:
        return {}, f"{type(exc).__name__}"
    if not stat.S_ISDIR(kind.st_mode):
        return {}, f"{_show(str(directory))} is not a directory of its own"
    try:
        entries = os.listdir(directory)
    except OSError as exc:
        return {}, f"{type(exc).__name__}"
    if len(entries) > MAX_LIMIT_ACT_ENTRIES:
        return {}, f"more than {MAX_LIMIT_ACT_ENTRIES} entries"
    pattern = re.compile(rf"c[0-9]+-{digest}(?:-[A-Za-z0-9._-]+)?\.(degrade|replan|none)\.json")
    for entry in entries:
        found = pattern.fullmatch(entry)
        if found is not None:
            out[found.group(1)] += 1
    return out, ""


def limit_act_unfinished(store, contract: AutonomyContract, wave_id: str) -> bool:
    """Is this wave's record a hold for a reached limit whose policy the loop acts on, with no act of
    the wave recorded? True only then. It is the crash window of the pass: the hold is written before
    the act (`act_on_limit`), and a state version with a record is otherwise decided once and never
    again. Anything that cannot be read as that exact record is False -- nothing acts on a record that
    is not plainly its own."""
    policy = getattr(getattr(contract, "horizon", None), "on_limit", None)
    if policy not in ACTING_POLICIES or type(wave_id) is not str or _WAVE_ID.fullmatch(wave_id) is None:
        return False
    path = store.dir / POLICY_DIR / f"{wave_id}.json"
    try:
        if os.lstat(path).st_size > MAX_POLICY_RECORD_BYTES:
            return False
        data = durable_io.read_single_link(path)
        body = json.loads(data.decode("ascii")) if data is not None else None
    except (OSError, ValueError, RecursionError):
        return False
    if (type(body) is not dict or body.get("schema") != POLICY_SCHEMA
            or body.get("project_id") != store.project_id or body.get("wave_id") != wave_id
            or body.get("outcome") != "HOLD" or body.get("reason") != LIMIT_REACHED
            or body.get("limit_policy") != policy.value
            or (body.get("contract") or {}).get("digest") != contract.digest()):
        return False
    return not any(os.path.lexists(store.dir / LIMIT_ACTS_DIR / f"{wave_id}.{kind}.json")
                   for kind in ("degrade", "replan", "none"))


def _replan_spec(state: ProjectState, contract: AutonomyContract, node_id: str, document: str) -> str:
    """The specification of the replanning step: ASCII, a function of the graph and the contract and of
    nothing else (not the time, not the spend), so that the same state version always gives the same
    bytes. Every name in it is a node id the record can name."""
    open_nodes = [n for n in state.nodes if not n.settled and n.id != node_id]
    listed = [f"- {n.id} ({n.lifecycle.value})" for n in open_nodes[:MAX_REPLAN_LISTED]]
    if len(open_nodes) > MAX_REPLAN_LISTED:
        listed.append(f"- ... and {len(open_nodes) - MAX_REPLAN_LISTED} more, not listed")
    limits = []
    if contract.budgets.max_dispatches is not None:
        limits.append(f"- dispatches: at most {contract.budgets.max_dispatches}")
    for what, value in (("horizon", contract.horizon.max_wallclock_seconds),
                        ("budget", contract.budgets.max_wallclock_seconds)):
        if value is not None:
            limits.append(f"- wallclock ({what}): at most {value} seconds")
    return "\n".join([
        f"# Replan: {node_id}", "",
        "A limit of the programme's autonomy contract was reached, and the contract's policy for that "
        "is REPLAN. This run is the replanning step the policy asks for; no gate failed and nothing is "
        "to be repaired.", "",
        "## The limits", "", *limits, "",
        "## The work that remains", "", *listed, "",
        "## What this run must do", "",
        f"Write `{document}` at the root of the repository: for every node listed above, in the order "
        "they should now run, say whether to keep it, drop it or split it, and why, so that what "
        "remains fits inside the limits. Change no code, no node, no limit and no contract.", "",
        "## Acceptance criteria", "",
        f"1. `{document}` exists and names every node listed above exactly once.",
        "2. No other file is changed.",
        "3. The existing test suite still passes.", ""])


def _replan_request(store, state: ProjectState, contract: AutonomyContract,
                    node_id: str) -> tuple[TaskNode, Path, bytes]:
    document = f"PLAN_REVISION_{contract.digest()}.md"
    data = _replan_spec(state, contract, node_id, document).encode("ascii")
    path = store.dir / REPLAN_SPECS_DIR / f"{node_id}.md"
    node = TaskNode(
        id=node_id, lifecycle=Lifecycle.READY, action_class=ActionClass.INTERNAL,
        spec_path=str(path), spec_digest=hashlib.sha256(data).hexdigest(), writes=[document],
        note=f"replan: the limit of contract {contract.digest()} was reached and its policy is REPLAN")
    return node, path, data


def _write_replan_spec(path: Path, data: bytes) -> None:
    """Creates the specification once. One that is already there with the same bytes is the same
    request made again (the crash window); any other is refused."""
    directory = path.parent
    try:
        try:
            kind = os.lstat(directory)
        except FileNotFoundError:
            os.mkdir(directory, 0o700)
        else:
            if not stat.S_ISDIR(kind.st_mode):
                raise ContinuationError(f"{_show(str(directory))} is not a directory of its own")
        try:
            durable_io.durable_create(path, data, mode=0o600)
        except FileExistsError:
            if durable_io.read_single_link(path) != data:
                raise ContinuationError(f"{_show(str(path))} exists with other content") from None
    except ContinuationError:
        raise
    except OSError as exc:
        raise ContinuationError(f"the replanning specification could not be written: "
                                f"{str(exc)[:200]}") from exc


def act_on_limit(store, contract: AutonomyContract, runs_dir: Path | str, held: PolicyContinuation, *,
                 depends_on: Callable[[TaskNode, TaskNode], object] | None = None) -> LimitAct:
    """What the loop does about a limit the record `held` says was reached (HOLD `LIMIT_REACHED`),
    when the contract's `horizon.on_limit` is DEGRADE or REPLAN -- the two policies slice 1 recorded and
    did not act on. PARK and STOP are not acted on: the hold is what they are.

    **DEGRADE**: the wave continues, at width `DEGRADED_WIDTH` (the mode's width must be wider, else
    there is nothing to narrow), at most `MAX_DEGRADED_WAVES` times per contract; the allowance is
    counted from the acts recorded before this one (`_acts_made`), and the act is recorded before the
    caller dispatches anything. **REPLAN**: nothing but a replanning step continues. The step is a
    node, `replan-<contract digest>`, that the caller adds to the graph (`kind` REPLAN_REQUEST; its
    specification is written here, create-only) and dispatches in the next wave (REPLAN_STEP); once it
    is settled the limit holds again (REPLAN_SPENT). One step per contract: the node's existence is
    the counter.

    In both, the hold of the record stands for everything else: an unreadable spend or elapsed time,
    a review that is due or cannot be read, a wave that is itself held (`PROGRAM_STUCK` ...), a graph
    with no READY node to continue -- each is recorded as `NONE` with its reason, create-only. The
    record of an act is beside the state (`limit-acts/`), bound to it as the hold is; a state that moved
    is `STATE_MOVED`. Raises `ContinuationError` for a record that is not a hold for such a limit of
    this contract and project, and when the act cannot be recorded -- nothing is then acted on.

    This is the loop's. `hoh project continue` records the hold and acts on nothing."""
    if not isinstance(held, PolicyContinuation) or not isinstance(contract, AutonomyContract):
        raise ContinuationError("the held decision must be a PolicyContinuation and the contract an "
                                "AutonomyContract")
    policy = contract.horizon.on_limit
    if (policy not in ACTING_POLICIES or held.outcome != "HOLD" or held.reason != LIMIT_REACHED
            or held.limit_policy != policy.value or held.project_id != store.project_id
            or (held.contract or {}).get("digest") != contract.digest()
            or _WAVE_ID.fullmatch(held.wave_id if type(held.wave_id) is str else "") is None):
        raise ContinuationError(f"{NOT_A_LIMIT_ACT}: the record is not a hold for a reached limit "
                                f"of this project and contract whose policy is acted on")
    if not isinstance(runs_dir, (str, os.PathLike)):
        raise ContinuationError(f"the runs directory must be a path, not {type(runs_dir).__name__}")
    try:
        if not stat.S_ISDIR(os.stat(runs_dir).st_mode):
            raise ContinuationError(f"{_show(runs_dir)} is not a directory")
    except (OSError, TypeError, ValueError) as exc:
        raise ContinuationError(f"{_show(runs_dir)} cannot be looked at: {str(exc)[:200]}") from exc
    runs_root = Path(runs_dir)
    state = store.read_state()
    if _state_binding(store, state) != held.state:
        raise ContinuationError(f"{STATE_MOVED}: the project state changed since the limit was decided; "
                                f"nothing was acted on")
    pins, why = _pinned_digests(state, runs_root)
    if why or pins != {contract.digest()}:
        raise ContinuationError(f"{CONTRACT_PIN_MISMATCH}: the runs are no longer pinned to this contract")
    wave_id, now, digest = held.wave_id, utcnow(), contract.digest()
    if depends_on is None:
        from .launcher import HohRunLauncher

        def depends_on(a: TaskNode, b: TaskNode) -> object:       # the launcher's rule
            return HohRunLauncher.depends_on(None, a, b)

    def record(result: PolicyContinuation, name_kind: str) -> PolicyContinuation:
        _write_policy_record(store, state, result, directory=LIMIT_ACTS_DIR,
                             name=f"{wave_id}.{name_kind}.json")
        return result

    def none(reason: str, stuck=()) -> LimitAct:
        return LimitAct(NO_ACT, reason, record(replace(
            held, at=now, stuck=tuple(stuck), act={"kind": NO_ACT, "reason": reason, "policy": policy.value}),
            "none"))

    def wave(**kw) -> PolicyContinuation:
        def hold(reason: str, *, limit_policy: str = "", ways=None, stuck=(), withheld=(), routings=(),
                 handlings=None, not_enacted=()) -> PolicyContinuation:
            return PolicyContinuation(
                project_id=store.project_id, wave_id=wave_id, at=now, contract=held.contract,
                state=held.state, outcome="HOLD", reason=reason, limit_policy=limit_policy,
                continued=(), withheld=tuple(withheld), routings=tuple(routings), ways_out=ways or {},
                stuck=tuple(stuck), limits=json.loads(json.dumps(held.limits)),
                handlings=handlings or {}, not_enacted=tuple(not_enacted))

        return _continue_under_pin(
            store, state, contract, runs_root, wave_id, depends_on, now, hold,
            json.loads(json.dumps(held.limits)), held.contract, held.state, store.project_id,
            waive_limits=True, **kw)

    lim = held.limits
    try:
        spent, max_d = lim["dispatches"]["spent"], lim["dispatches"]["max"]
        elapsed, max_w = lim["wallclock_seconds"]["elapsed"], lim["wallclock_seconds"]["max"]
        readable = ((max_d is None or type(spent) is int) and (max_w is None or type(elapsed) is int))
    except (KeyError, TypeError):
        raise ContinuationError(f"{NOT_A_LIMIT_ACT}: the record carries no limits") from None
    if readable and not ((max_d is not None and spent >= max_d) or (max_w is not None and elapsed >= max_w)):
        raise ContinuationError(f"{NOT_A_LIMIT_ACT}: no limit of the record is reached")
    if not readable:
        return none(LIMIT_UNREADABLE)
    review = (held.mode_wave or {}).get("review")
    if type(review) is not dict or review.get("needs_you") is not False:
        return none(REVIEW_PENDING)                 # a person is called; nothing is spent meanwhile

    if policy is OnLimit.DEGRADE:
        made, unreadable = _acts_made(store.dir / LIMIT_ACTS_DIR, digest)
        if unreadable:
            raise ContinuationError(f"{ACTS_UNREADABLE}: {unreadable}")
        if made["degrade"] >= MAX_DEGRADED_WAVES:
            return none(DEGRADE_SPENT)
        width = mode_width(contract)
        if width <= DEGRADED_WIDTH:
            return none(NOTHING_TO_NARROW)
        narrowed = wave(width_cap=DEGRADED_WIDTH)
        if narrowed.outcome != "CONTINUE" or not narrowed.continued:
            return none(narrowed.reason or NOTHING_TO_CONTINUE, narrowed.stuck)
        act = {"kind": DEGRADE, "from_width": width, "width": DEGRADED_WIDTH,
               "degraded_wave": made["degrade"] + 1, "max_degraded_waves": MAX_DEGRADED_WAVES}
        return LimitAct(DEGRADE, "", record(replace(
            narrowed, limit_policy=policy.value, mode_wave=held.mode_wave, act=act), "degrade"))

    node_id = f"{REPLAN_PREFIX}{digest}"
    step = next((n for n in state.nodes if n.id == node_id), None)
    if step is None:
        node, path, data = _replan_request(store, state, contract, node_id)
        _write_replan_spec(path, data)
        return LimitAct(REPLAN_REQUEST, "", held, node)
    if step.settled:
        return none(REPLAN_SPENT)
    if step.lifecycle is not Lifecycle.READY:
        return none(REPLAN_NOT_READY)
    only = wave(only=frozenset({node_id}))
    if only.outcome != "CONTINUE" or node_id not in only.continued:
        return none(only.reason or NOTHING_TO_CONTINUE, only.stuck)
    act = {"kind": REPLAN_STEP, "node": node_id}
    return LimitAct(REPLAN_STEP, "", record(replace(
        only, limit_policy=policy.value, mode_wave=held.mode_wave, act=act), "replan"))


# --- Answering the routed decisions (V3.3 P3-31, second slice) ---------------------------------

RESOLUTION_SCHEMA = "hoh-decision-resolution/1"
#: Where a project keeps what a resolver answered, one create-only file per (decision, contract).
RESOLUTIONS_DIR = "decision-resolutions"
MAX_RESOLUTION_RECORD_BYTES = 1 << 14
#: A directory with more resolution records than this is not read: it is not a project's own.
MAX_RESOLUTION_RECORDS = 4096

ANSWERED = "ANSWERED"
#: What a record says of an answered decision. It is not a `Handling` and not an approval: it says
#: that the number of external actions the contract pre-authorises (`horizon.max_external_actions`)
#: still had room for this one, and that nothing was done about it.
ANSWERED_BY_POLICY = "ANSWERED_BY_POLICY"
WITHIN_PREAUTHORIZATION = "WITHIN_PREAUTHORIZATION"
#: Why a decision is left open by the resolver (the first group is a fact about the decision or the
#: contract, exit 0; the second is a failure to read or to record, as for the routing).
NOT_EXTERNAL_ACTION = "NOT_EXTERNAL_ACTION"
NOT_ROUTED = "NOT_ROUTED"
NO_PREAUTHORIZATION = "NO_PREAUTHORIZATION"
PREAUTHORIZATION_SPENT = "PREAUTHORIZATION_SPENT"
RESOLUTIONS_UNREADABLE = "RESOLUTIONS_UNREADABLE"
RESOLUTION_FAILURES = frozenset({RESOLUTIONS_UNREADABLE, RECORD_CONFLICT, RECORD_NOT_WRITTEN,
                                 RECORD_UNREADABLE, NODE_NOT_NAMEABLE})


@dataclass(frozen=True)
class ResolvedDecision:
    """One open decision of the project and what the resolver did with it."""

    node_id: str
    decision_digest: str
    outcome: str                  # ANSWERED or LEFT_OPEN
    reason: str = ""
    handling: str = ""
    #: True only when this call created the record (one that was already there, and a dry run, are False).
    recorded: bool = False


@dataclass(frozen=True)
class ResolutionReport:
    project_id: str
    contract_id: str
    contract_digest: str
    mode: str
    program_epoch: int
    dry_run: bool
    decisions: tuple[ResolvedDecision, ...]

    @property
    def failed(self) -> bool:
        return any(d.reason in RESOLUTION_FAILURES for d in self.decisions)

    def as_json(self) -> dict:
        return {"project_id": self.project_id,
                "contract": {"id": self.contract_id, "digest": self.contract_digest,
                             "mode": self.mode, "program_epoch": self.program_epoch},
                "dry_run": self.dry_run,
                "decisions": [{"node": d.node_id, "decision_digest": d.decision_digest,
                               "outcome": d.outcome, "reason": d.reason, "handling": d.handling,
                               "recorded": d.recorded} for d in self.decisions]}


def _resolution_record(project_id: str, node: TaskNode, digest: str, routing_name: str,
                       contract: AutonomyContract, verdict: Routing, resolver: str) -> bytes:
    body = {"schema": RESOLUTION_SCHEMA, "project_id": project_id, "node_id": node.id,
            "decision_digest": digest, "kind": node.action_class.value.lower(),
            "disposition": ANSWERED_BY_POLICY, "answer": WITHIN_PREAUTHORIZATION,
            "rule": "horizon.max_external_actions", "routing": routing_name,
            "escalation": verdict.escalation.value, "resolver": resolver,
            "approves": False, "executes": False,
            "contract_id": contract.contract_id, "contract_digest": contract.digest(),
            "mode": contract.mode.value, "program_epoch": contract.program_epoch}
    return (json.dumps(body, sort_keys=True, indent=1, ensure_ascii=True)
            + "\n").encode("ascii")


def _read_resolutions(directory: Path, project_id: str) -> tuple[dict[str, bytes], dict[int, int], str]:
    """`(bytes by file name, records by programme epoch, "")` of the resolutions beside the state, or
    `({}, {}, why)`. A name of the shape `<16 hex>-<16 hex>.json` is a record and must be one: a link,
    a FIFO, a second name, a file over `MAX_RESOLUTION_RECORD_BYTES`, bytes that are not one JSON object
    of this schema, project and file name make the whole directory unreadable -- a count taken from part of
    a directory is a count that can be lowered by damaging a file. Other names are not records."""
    found: dict[str, bytes] = {}
    epochs: dict[int, int] = {}
    try:
        kind = os.lstat(directory)
    except FileNotFoundError:
        return found, epochs, ""
    except OSError as exc:
        return {}, {}, f"{type(exc).__name__}"
    if not stat.S_ISDIR(kind.st_mode):
        return {}, {}, "not a directory of its own"
    try:
        entries = sorted(os.listdir(directory))
    except OSError as exc:
        return {}, {}, f"{type(exc).__name__}"
    for entry in entries:
        if re.fullmatch(r"[0-9a-f]{16}-[0-9a-f]{16}\.json", entry) is None:
            continue
        if len(found) >= MAX_RESOLUTION_RECORDS:
            return {}, {}, f"more than {MAX_RESOLUTION_RECORDS} records"
        path = directory / entry
        try:
            if os.lstat(path).st_size > MAX_RESOLUTION_RECORD_BYTES:
                return {}, {}, f"{entry} is larger than {MAX_RESOLUTION_RECORD_BYTES} bytes"
            data = durable_io.read_single_link(path)
        except OSError as exc:
            return {}, {}, f"{entry}: {type(exc).__name__}"
        try:
            body = json.loads(data.decode("ascii")) if data is not None else None
        except (ValueError, UnicodeDecodeError):
            body = None
        if (type(body) is not dict or body.get("schema") != RESOLUTION_SCHEMA
                or body.get("project_id") != project_id
                or entry != f"{body.get('decision_digest')}-{body.get('contract_digest')}.json"
                or type(body.get("program_epoch")) is not int):
            return {}, {}, f"{entry} is not a resolution record of this project"
        found[entry] = data
        epochs[body["program_epoch"]] = epochs.get(body["program_epoch"], 0) + 1
    return found, epochs, ""


def resolve_routed_decisions(store, contract: AutonomyContract, runs_dir: Path | str, *,
                             dry_run: bool = False) -> ResolutionReport:
    """Answers the open EXTERNAL decisions the contract hands to a resolver and has pre-authorised.

    The decisions are those of `derive_frontier`, in its ranking. Only a READY EXTERNAL node nothing
    stands in front of (`EXTERNAL_ACTION`) can be answered; a branch a run parked, a person's block, a
    branch the contract ended and a log that cannot be read are not this resolver's (`NOT_EXTERNAL_ACTION`:
    they stay in front of a person). For an EXTERNAL decision, in this order, the first that fails leaves
    it open and records nothing:

    1. the contract holds together, names the node, and `route()` still answers RESOLVED_BY_POLICY by an
       escalation that names a resolver (`NOT_COVERED`);
    2. the decision was routed, and the routing record is exactly what the contract would write for the
       decision as it is now (`NOT_ROUTED`; the record P3-31's first slice wrote, checked as P3-32 checks it);
    3. the contract pre-authorises external actions: `horizon.max_external_actions` is a whole number of at
       least one (`NO_PREAUTHORIZATION`), and the answers already recorded for this project and programme
       epoch are fewer than it (`PREAUTHORIZATION_SPENT`).

    Then `decision-resolutions/<decision digest>-<contract digest>.json` is created -- once, never
    replaced -- and says `answer: WITHIN_PREAUTHORIZATION`, `approves: false`, `executes: false`. **An answer
    is not an approval and not an action**: nothing is launched, unblocked or recorded as done; the answer
    is the evidence, and the counter of the pre-authorisation, for whoever carries the action out. A
    directory of resolutions that cannot be read answers nothing (`RESOLUTIONS_UNREADABLE`).

    Raises `RoutingRefused` (nothing read or written) for a contract or a runs directory that cannot be
    one, and for a graph without a frontier.
    """
    if not isinstance(contract, AutonomyContract):
        raise RoutingRefused("the contract must be an AutonomyContract")
    if (type(contract.contract_id) is not str
            or _CONTRACT_ID.fullmatch(contract.contract_id) is None
            or type(contract.program_epoch) is not int
            or not 0 <= contract.program_epoch <= _MAX_EPOCH):
        raise RoutingRefused(
            f"contract {contract.contract_id[:80]!r} at epoch "
            f"{_show(contract.program_epoch)} cannot be named by a record")
    if not isinstance(runs_dir, (str, os.PathLike)):
        raise RoutingRefused(f"the runs directory must be a path, not {type(runs_dir).__name__}")
    try:
        if not stat.S_ISDIR(os.stat(runs_dir).st_mode):
            raise RoutingRefused(f"{_show(runs_dir)} is not a directory")
    except (OSError, TypeError, ValueError) as exc:
        raise RoutingRefused(f"{_show(runs_dir)} cannot be looked at: {str(exc)[:200]}") from exc
    state = store.read_state()
    stops, unreadable = _collect_logs(state, Path(runs_dir))
    try:
        frontier = derive_frontier(state, stops=stops, unreadable=unreadable)
    except FrontierError as exc:
        raise RoutingRefused(f"the project graph has no frontier: {str(exc)[:200]}") from exc
    incoherent = bool(coherence(contract))
    try:
        routings = _read_routing_records(store.dir / ROUTINGS_DIR)
    except RoutingRefused:
        routings = {}
    answers, epochs, why_unreadable = _read_resolutions(store.dir / RESOLUTIONS_DIR, store.project_id)
    used = epochs.get(contract.program_epoch, 0)
    cap = contract.horizon.max_external_actions
    by_id = {n.id: n for n in state.nodes}
    done: list[ResolvedDecision] = []
    for d in frontier.decisions:
        node = by_id[d.node_id]
        digest = _decision_digest(store.project_id, node)

        def left(reason: str, handling: str = "", _id=d.node_id, _digest=digest):
            return ResolvedDecision(_id, _digest, LEFT_OPEN, reason, handling)

        if d.handling != EXTERNAL_ACTION:
            done.append(left(NOT_EXTERNAL_ACTION, d.handling))
            continue
        if _NODE_ID.fullmatch(node.id) is None:
            done.append(left(NODE_NOT_NAMEABLE))
            continue
        if incoherent:
            done.append(left(NOT_COVERED, "CONTRACT_INCOHERENT"))
            continue
        verdict = route(contract, decision=node.note or f"external action {node.id}",
                        kind=ActionClass.EXTERNAL.value, delegable=True)
        resolver = RESOLVERS.get(verdict.escalation) if verdict.escalation else None
        if verdict.handling is not Handling.RESOLVED_BY_POLICY or resolver is None:
            done.append(left(NOT_COVERED, verdict.handling.value))
            continue
        routing_name = f"{digest}-{contract.digest()}.json"
        if (routing_name not in routings or routings[routing_name][1]
                != _routing_record(store.project_id, node, digest, contract, verdict, resolver)):
            done.append(left(NOT_ROUTED))
            continue
        if why_unreadable:
            done.append(left(RESOLUTIONS_UNREADABLE))
            continue
        data = _resolution_record(store.project_id, node, digest, routing_name, contract,
                                  verdict, resolver)
        if routing_name in answers:
            same = answers[routing_name] == data
            done.append(ResolvedDecision(node.id, digest, ANSWERED) if same
                        else left(RECORD_CONFLICT))
            continue
        if type(cap) is not int or cap < 1:
            done.append(left(NO_PREAUTHORIZATION))
            continue
        if used >= cap:
            done.append(left(PREAUTHORIZATION_SPENT))
            continue
        reason, recorded = _write_routing(store.dir / RESOLUTIONS_DIR, routing_name, data,
                                          dry_run=dry_run)
        if reason:
            done.append(left(reason))
            continue
        used += 1
        done.append(ResolvedDecision(node.id, digest, ANSWERED, "", "", recorded))
    return ResolutionReport(project_id=store.project_id, contract_id=contract.contract_id,
                            contract_digest=contract.digest(), mode=contract.mode.value,
                            program_epoch=contract.program_epoch, dry_run=dry_run,
                            decisions=tuple(done))


@dataclass(frozen=True)
class AnsweredDecision:
    """An EXTERNAL decision the resolver answered, as the executor needs it (P3-32 slice 4)."""

    node_id: str
    #: The node's note: what the decision is about, and where a node declares its action.
    note: str
    decision_digest: str
    #: The resolution record that answers it, and the digest of its bytes.
    record_name: str
    record_sha256: str


def answered_external_decisions(store, contract: AutonomyContract, runs_dir: Path | str
                                ) -> tuple[AnsweredDecision, ...]:
    """The open EXTERNAL decisions whose answer is on disk and is exactly what `resolve_routed_decisions`
    would write for them now: the contract still holds together, still hands the decision to a resolver,
    the routing record is still the one the contract would write, and the resolution record is there, one
    file, as written. Read-only -- the resolver runs as a dry run, so nothing is created, and a decision
    it would only answer in a dry run (no record yet) is not here. Raises what the resolver raises
    (`RoutingRefused`: nothing was read of a log)."""
    report = resolve_routed_decisions(store, contract, runs_dir, dry_run=True)
    state = store.read_state()
    found: list[AnsweredDecision] = []
    for d in report.decisions:
        node = state.node(d.node_id)
        # the note is read from a second read of the state: the digest the resolver answered must still be the node's
        if (d.outcome != ANSWERED or node is None
                or _decision_digest(store.project_id, node) != d.decision_digest):
            continue
        name = f"{d.decision_digest}-{contract.digest()}.json"
        path = store.dir / RESOLUTIONS_DIR / name
        try:
            if os.lstat(path).st_size > MAX_RESOLUTION_RECORD_BYTES:
                continue
            data = durable_io.read_single_link(path)
        except OSError:
            continue
        if data is None:
            continue
        found.append(AnsweredDecision(node.id, node.note, d.decision_digest, name,
                                      hashlib.sha256(data).hexdigest()))
    return tuple(found)


def programme_stoppers(store, runs_dir: Path | str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`(stoppers, unreadable)`: the nodes whose branch stops the whole programme -- a node blocked
    `contract_stop`, or one whose run routed a handling that does not continue past it -- and the nodes whose
    routing log cannot be read. The reading `plan_continuation` makes for `PROGRAMME_STOPPED` and
    `ROUTING_LOG_UNREADABLE`, read-only: the one durable stop this product has (the contract's `kill_switch` is a
    declaration of channels; nothing in the running programme sets it). Raises what the state read raises."""
    state = store.read_state()
    stops, unreadable = _collect_logs(state, Path(runs_dir))
    stoppers = tuple(
        n.id for n in state.nodes
        if (n.lifecycle is Lifecycle.BLOCKED and n.blocked_kind == "contract_stop")
        or any(not continues_past(r.handling) for r in stops.get(n.id, ())))
    return stoppers, tuple(n.id for n in state.nodes if n.id in unreadable)


def due_reviews(contract: AutonomyContract, runs_dir: Path | str, project_id: str,
                now: str | None = None) -> tuple[str, ...]:
    """The ids of the armed REVIEW_DUE timers of this project and programme epoch that are due, the
    earliest first -- the timers `apply_mode` arms in the root's timer file and reads, by its own filter.
    Empty when the mode does not review its holds, when none is due and when the timers cannot be read
    (`review_is_due` is the question that also answers True for an unreadable file; a caller that names a
    wave after a timer needs the timer). Reads, writes nothing."""
    _mode, row, _width, _fell_back = _profile(contract)
    if not row.hold_review:
        return ()
    _timers_store, timers, unreadable = _read_timers(Path(runs_dir))
    if unreadable:
        return ()
    moment = now if now is not None else utcnow()
    due = sorted((t for t in _reviews_of(contract, project_id, timers) if _is_due(t, moment)),
                 key=lambda t: (t.due_at, t.timer_id))
    return tuple(t.timer_id for t in due)
