"""A `RunLauncher` bound to real Herdr and real HoH runs.

`orchestrator.py` defines the control loop against two narrow protocols so it
can be exercised without agent quota. This module is the other side: the
implementation that actually dispatches, reads back a real `RunState`, and
decides what the run's outcome was.

The whole difficulty is in that last step, and it is worth being explicit
about why, because getting it wrong is how an orchestrator merges something
that was never accepted. A run's state does not carry a field saying
"accepted". It carries a stage, a condition, and a `last_accepted_candidate`
that may be left over from an *earlier* iteration. So:

* `CHECKPOINTED` with a `last_accepted_candidate` that advanced during this
  dispatch is an acceptance.
* `CHECKPOINTED` with the same candidate as before the dispatch is not: the
  run ended where it started, and treating that as acceptance would merge a
  candidate twice.
* `VERIFYING` or `DEVELOPING` still active is not a verdict at all -- the run
  is unfinished, and saying anything else about it is guessing.
* `BLOCKED` with a quota reason is a provider outage, not a rejection. The
  distinction matters because recording it as a rejection burns a node's
  retry budget on somebody else's downtime.

Every one of those cases is a halt class the controller already knows how to
name, so the mapping is a translation rather than a judgement.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .approval import Approval, ApprovalProvider, NoApprovalProvider
from .contracts import Condition, Stage
from .orchestrator import (
    AUTONOMY_FILE, BudgetExhausted, MergeFailure, MergeResult, RunLauncher, RunOutcome,
    RunVerdict,
)
from .failures import AUTOMATIC_KINDS
from .capability import walk_tree
from .controller import open_seal_arenas
from .durable_io import DurableWriteNotSynced, UnsafeRecordFile, durable_create, read_single_link
from .preflightgate import (MENDABLE, PreflightRefused, PreflightRequirement, check_preflight,
                            default_requirement, interpreter_identity, measure_default, run_tool,
                            shown_place)
from .project import ActionClass, Lifecycle, TaskNode
from .reservation import BudgetReservations, ReservationError
from .stages import violation_unresolved
from .store import LockBusy, RunStore, StoreError
from .wave import MAX_WAVE_WIDTH
from .wslease import (
    MAX_GENERATION, Lease, WorkspaceHeld, WorkspaceLease, WorkspaceLeaseError,
    WorkspaceLeaseLost, holder_name,
)

#: Blocked kinds that mean the provider, not the work.
PROVIDER_BLOCKED = frozenset({"usage_quota", "quota", "rate_limit", "provider",
                              # What `stages.block` actually writes for an
                              # exhausted quota. Missing here, a real quota wait
                              # read as UNDETERMINED and halted the project
                              # AMBIGUOUS instead of parking it (found 2026-09-25
                              # once the durability fixture stopped re-implementing
                              # this mapping).
                              "usage_limit"}) | AUTOMATIC_KINDS

#: The most bytes of one run state the shared budget reads. A state above it
#: is refused, not parsed: it is not a run state, and the budget it belongs to
#: cannot be computed from it.
MAX_STATE_BYTES = 16 * 1024 * 1024

#: Phrases with which `hoh run` refuses to start, or a run stops, because
#: storage admission is not usable (STORAGE-B3). A run that says one of them
#: is STORAGE_UNAVAILABLE (NEEDS_YOU), never UNDETERMINED. The blocked kind of
#: a storage park, `storage_pressure`, is deliberately *not* in
#: PROVIDER_BLOCKED: it is its own class with its own consumer.
STORAGE_REFUSED = ("storage state root is not initialised", "WSL: configure host_paths",
                   "WSL: the VHDX volume is not observed", "storage admission unavailable",
                   "storage pressure: the recheck timer could not be armed")

#: Phrases in a stop reason that mean a person has to answer something. Matched
#: on the reason text rather than a code because HoH records this case in prose:
#: `blocked_kind` was None for the trust dialog that first exposed it.
APPROVAL_WAITING = frozenset({"waits for an approval", "trust this folder",
                              "blocked dialog", "awaiting approval"})


def accepted_identity_problem(git, sha: str, outcome: RunOutcome) -> str | None:
    """Why commit `sha` is not the accepted candidate, or None when it is.

    The accepted identity is the verified *tree* when the outcome carries one:
    `sha` must have exactly that tree. Without a tree (an older launcher) it is
    the candidate commit itself. `git` is a callable running git in the
    repository and returning a CompletedProcess.
    """
    if outcome.candidate_tree:
        tree = git("rev-parse", "--verify", "--quiet", sha + "^{tree}").stdout.strip()
        if tree != outcome.candidate_tree:
            return (f"branch head {sha[:12]} carries tree {tree[:12] or '?'}, but the "
                    f"accepted tree is {outcome.candidate_tree[:12]}: the branch moved "
                    f"after acceptance, or the accepted work was never committed onto it")
        return None
    if outcome.candidate and outcome.candidate != sha:
        return (f"branch head {sha[:12]} is not the accepted candidate "
                f"{outcome.candidate[:12]}, and no verified tree was recorded to "
                f"show they carry the same content")
    return None


def _timer_form(stamp: str | None) -> str | None:
    """`RunState.retry_after` (ISO 8601 with offset) in the timer form, or None.

    None, not a guess, when it cannot be read: the node then keeps a human
    owner instead of parking on a time nobody can check.

    A fractional moment is rounded *up* to the next whole second. The timer
    form has no fractions, and cutting them off made the timer due up to one
    second before the provider's reset: the tick resumed a run the provider
    still refused, while `quota.is_due` said not due (O237). The resume loop's
    copy calls this one, so both round the same way.
    """
    if not stamp:
        return None
    from datetime import UTC, datetime, timedelta

    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    moment = moment.astimezone(UTC)
    if moment.microsecond:
        moment = moment.replace(microsecond=0) + timedelta(seconds=1)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def storage_refusal(text: str | None) -> str | None:
    """The first line of `text` that names a storage refusal, or None."""
    for line in (text or "").splitlines():
        if any(phrase in line for phrase in STORAGE_REFUSED):
            return line.strip()[:300]
    return None


#: P3-20 slice 3. A project declares the routing of the runs its launcher starts the way it
#: registers its driver: a file in the project directory beside `driver.json`
#: (`hoh-routing-policy/1`, the schema `hoh run --routing-policy` takes). The first drive that
#: binds it pins its digest, create-only, in the same directory; a later drive that finds another
#: digest, or the policy gone, halts by name before any run is started.
ROUTING_POLICY_FILE = "routing-policy.json"
ROUTING_PIN_FILE = "routing-pin.json"
ROUTING_PIN_SCHEMA = "hoh-project-routing-pin/1"
_ROUTING_PIN_MAX_BYTES = 64 * 1024

#: How `hoh run` begins the line on which it refuses the autonomy contract it was offered (P3-30 slice 2):
#: the run is pinned to another contract, or the pin is damaged. Nothing ran.
_AUTONOMY_REFUSAL = "autonomy contract refused"


def autonomy_refusal(text: str | None) -> str | None:
    """The first line of `text` that is an autonomy refusal of `hoh run`, or None."""
    for line in (text or "").splitlines():
        if line.startswith(_AUTONOMY_REFUSAL):
            return line.strip()[:300]
    return None


#: How `hoh run` / `hoh start` begin the line on which they refuse a routing policy (slice 2).
_ROUTING_REFUSALS = ("routing policy changed", "routing policy refused", "routing.json")


#: P3-18 slice 3. How `hoh run` begins the line on which it refuses to drive a run for its lease (the record cannot be
#: read exactly, the keepalive thread cannot be made; `rundrive.DriveRefused`): nothing ran, and a person looks at the
#: record. A lease another driver *holds* is not this: `hoh run` says `Run <id> is already being executed`, which is
#: IN_FLIGHT. A lease lost *during* a run (`run lease lost:`) is not this either: iterations ran, and the state tells.
_LEASE_REFUSAL = "run lease refused ("


def lease_refusal(text: str | None) -> str | None:
    """The first line of `text` that is a run-lease refusal of `hoh run`, or None."""
    for line in (text or "").splitlines():
        if line.startswith(_LEASE_REFUSAL):
            return line.strip()[:300]
    return None


def routing_refusal(text: str | None) -> str | None:
    """The first line of `text` that is a routing refusal of `hoh run` / `hoh start`, or None."""
    for line in (text or "").splitlines():
        if line.startswith(_ROUTING_REFUSALS):
            return line.strip()[:300]
    return None


#: P3-11 slice 2. A project declares the environment requirement of the runs its launcher starts
#: the way it declares a routing policy: one file in the project directory. `{"schema":
#: "hoh-preflight-requirement/1", "report": <absolute path>, "profile": <name>,
#: "max_age_seconds": <int>}` -- exactly those keys. It is read fail-closed at every bind (a file
#: that is there and is not that is never "no requirement") and handed to every `hoh start` /
#: `hoh run` the launcher types, where the CLI asks the report again at the start of the process.
PREFLIGHT_FILE = "preflight.json"
PREFLIGHT_SCHEMA = "hoh-preflight-requirement/1"
_PREFLIGHT_FILE_MAX_BYTES = 4096
#: P3-11 slice 3. A project that declares nothing is gated by the default requirement
#: (`preflightgate.default_requirement`). The same file can instead be a **waiver**:
#: `{"schema": "hoh-preflight-waiver/1", "reason": <text>, "decided_by": <who>, "decided_at":
#: <UTC time>}` -- exactly those keys. A waiver is a recorded decision of a person (the product
#: writes none), read fail-closed at every bind and exposed as `HohRunLauncher.preflight_waiver`.
PREFLIGHT_WAIVER_SCHEMA = "hoh-preflight-waiver/1"
_WAIVER_TEXT_MAX = 400
_WAIVER_TIME = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


@dataclass(frozen=True)
class PreflightWaiver:
    """A project's recorded decision not to be gated by the environment preflight."""

    reason: str
    decided_by: str
    decided_at: str

#: How `hoh run` / `hoh start` begin the lines on which they refuse an environment requirement.
_PREFLIGHT_REFUSALS = ("the environment preflight does not allow a start",
                       "environment preflight refused")


def preflight_refusal(text: str | None) -> str | None:
    """The first line of `text` that is an environment refusal of `hoh run` / `hoh start`, or None."""
    for line in (text or "").splitlines():
        if line.startswith(_PREFLIGHT_REFUSALS):
            return line.strip()[:600]
    return None


def _no_duplicate_keys(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"repeated key {key!r}")
        out[key] = value
    return out


def _waiver_of(doc: dict) -> PreflightWaiver:
    if set(doc) != {"schema", "reason", "decided_by", "decided_at"}:
        raise ValueError(f"{PREFLIGHT_FILE} (a waiver) must have exactly the keys schema, "
                         "reason, decided_by and decided_at")
    for key in ("reason", "decided_by"):
        value = doc[key]
        if (not isinstance(value, str) or not value.strip() or len(value) > _WAIVER_TEXT_MAX
                or not value.isprintable()):
            raise ValueError(f"{PREFLIGHT_FILE} (a waiver) needs a printable `{key}` of 1 to "
                             f"{_WAIVER_TEXT_MAX} characters: a waiver without a reason and a "
                             "name is no record")
    when = doc["decided_at"]
    try:
        if not (isinstance(when, str) and _WAIVER_TIME.match(when)):
            raise ValueError("shape")
        datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ")        # a date that exists
    except ValueError:
        raise ValueError(f"{PREFLIGHT_FILE} (a waiver) needs `decided_at` as a UTC time "
                         "YYYY-MM-DDTHH:MM:SSZ") from None
    return PreflightWaiver(reason=doc["reason"], decided_by=doc["decided_by"], decided_at=when)


def _load_preflight_file(path: Path) -> "PreflightRequirement | PreflightWaiver":
    """The requirement (or the recorded waiver) a project declares, or a `ValueError` with the
    words of the refusal.
    Never "none": a link, a directory, a FIFO, a second name, oversize, not UTF-8, not one JSON
    object with exactly the four keys, another schema, or values the requirement refuses."""
    try:
        raw = read_single_link(path)
    except UnsafeRecordFile:
        raise ValueError(f"{PREFLIGHT_FILE} is a link or not one regular file") from None
    except OSError as exc:
        raise ValueError(f"{PREFLIGHT_FILE} cannot be read ({type(exc).__name__})") from None
    if raw is None:
        raise ValueError(f"{PREFLIGHT_FILE} is gone")
    if len(raw) > _PREFLIGHT_FILE_MAX_BYTES:
        raise ValueError(f"{PREFLIGHT_FILE} is larger than {_PREFLIGHT_FILE_MAX_BYTES} bytes")
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError(f"{PREFLIGHT_FILE} is not one UTF-8 JSON object") from None
    if isinstance(doc, dict) and doc.get("schema") == PREFLIGHT_WAIVER_SCHEMA:
        return _waiver_of(doc)
    if not (isinstance(doc, dict) and doc.get("schema") == PREFLIGHT_SCHEMA):
        raise ValueError(f"{PREFLIGHT_FILE} is not a preflight requirement or waiver of this "
                         f"version ({PREFLIGHT_SCHEMA}, {PREFLIGHT_WAIVER_SCHEMA})")
    if set(doc) != {"schema", "report", "profile", "max_age_seconds"}:
        raise ValueError(f"{PREFLIGHT_FILE} must have exactly the keys schema, report, profile "
                         "and max_age_seconds")
    if not (isinstance(doc["report"], str) and os.path.isabs(doc["report"])):
        # a relative path would be made absolute against the cwd of whoever reads it: two
        # processes (the launcher, the `hoh run` it types) would read two files
        raise ValueError(f"{PREFLIGHT_FILE} must name the report by an absolute path")
    try:
        return PreflightRequirement(report=doc["report"], profile=doc["profile"],
                                    max_age_seconds=doc["max_age_seconds"])
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{PREFLIGHT_FILE} declares a requirement that is refused: {exc}") from None


def unbound_preflight_refusal(launcher: object, project_dir: "Path | str") -> str | None:
    """P3-11 slice 4. The words with which the governed drive refuses a project whose launcher cannot
    be bound to its environment preflight, or None when nothing is wrong.

    A launcher with a callable `bind_routing` binds itself (`bind_routing` calls `bind_preflight`:
    the product default, the project's requirement or its recorded waiver) -> None. One without it
    would start runs on no report at all, whatever the project says, so the project must have said
    so: only a recorded waiver (`PREFLIGHT_WAIVER_SCHEMA`) lets it through. No declaration, a
    requirement the launcher cannot hold and a declaration that cannot be read are refusals by name
    (the same file, read by the same loader, as at a bind)."""
    if callable(getattr(launcher, "bind_routing", None)):
        return None
    who = type(launcher).__name__
    declared = Path(os.path.abspath(os.path.expanduser(str(project_dir)))) / PREFLIGHT_FILE
    how = (f"record a waiver in {PREFLIGHT_FILE} (schema {PREFLIGHT_WAIVER_SCHEMA}: reason, "
           "decided_by, decided_at) or use a launcher that binds")
    if not os.path.lexists(declared):
        return (f"preflight refused: the launcher ({who}) has no bind_routing, so this project's "
                f"execution preflight (the default gate) cannot be applied and its runs would start "
                f"on no report; {how}")
    try:
        found = _load_preflight_file(declared)
    except ValueError as exc:
        return f"preflight requirement refused: {exc}; the launcher ({who}) cannot be bound to it either"
    if isinstance(found, PreflightWaiver):
        return None
    return (f"preflight refused: this project declares a requirement ({PREFLIGHT_FILE}, profile "
            f"{found.profile!r}) and the launcher ({who}) has no bind_routing, so it cannot hold it "
            f"and its runs would start on no report; {how}")


def _read_project_routing_pin(project_dir: Path) -> dict | None:
    """The project's routing pin, or None when it has none. A pin that is there and is not one
    small regular, link-free, single-name UTF-8 JSON object of this schema with a sha256 digest
    and a `pinned_at` is a `ValueError`: a damaged pin is never "no pin"."""
    path = project_dir / ROUTING_PIN_FILE
    try:
        raw = read_single_link(path)
    except UnsafeRecordFile:
        raise ValueError(f"{ROUTING_PIN_FILE} is a link or not one regular file") from None
    except OSError as exc:
        raise ValueError(f"{ROUTING_PIN_FILE} cannot be read ({type(exc).__name__})") from None
    if raw is None:
        return None
    if len(raw) > _ROUTING_PIN_MAX_BYTES:
        raise ValueError(f"{ROUTING_PIN_FILE} is larger than {_ROUTING_PIN_MAX_BYTES} bytes")
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError(f"{ROUTING_PIN_FILE} is not one UTF-8 JSON document") from None
    if not (isinstance(rec, dict) and rec.get("schema") == ROUTING_PIN_SCHEMA
            and isinstance(rec.get("digest"), str)
            and re.fullmatch(r"[0-9a-f]{64}", rec["digest"])
            and isinstance(rec.get("pinned_at"), str) and rec["pinned_at"]):
        raise ValueError(f"{ROUTING_PIN_FILE} is not a project routing pin of this version")
    return rec


class DelegatedLease(WorkspaceLease):
    """A worktree whose lease the node's own controller takes: the wave executor
    only **observes** it (P3-02 slice 2).

    `hoh run` leases `state.repo_path` at every iteration (P3-06 slice 2), so a
    lease the executor held across the launch would make the run wait on its own
    executor (that slice's Q1). Admission therefore asks the question the lease
    answers -- "is anyone writing in this worktree?" -- and takes nothing: a
    live holder or an unreadable record refuses the node, exactly as `acquire`
    would; a free worktree is admitted with the generation its run is expected
    to take (the one on disk plus one; a run takes one more per iteration). The
    judgement at the end is the same question again: nobody else's lease record
    may stand where the run's was.

    Nothing is written by this class, ever: no record, no log line.
    """

    _seen: Lease | None = None
    _observed: Lease | None = None

    def acquire(self) -> Lease:
        now = self._now()
        seen = self.read()               # an unreadable record raises: never read as "free"
        state, why = self.holder_state(seen, now)
        if state == "LIVE":
            raise WorkspaceHeld("held", f"cannot take {self.worktree}: {why}")
        if seen is not None and seen.generation >= MAX_GENERATION:
            raise WorkspaceLeaseError(
                "invalid", f"{self.lease_path} is at generation {seen.generation}, the last one "
                           f"a record may carry")
        self._seen = seen
        self._observed = Lease(
            holder=self.holder, generation=(seen.generation if seen is not None else 0) + 1,
            token_sha256="0" * 64, acquired_at=now, renewed_at=now, expires_at=now,
            holder_pid=self.pid, holder_boot_id="", holder_host="", holder_pidns="",
            holder_start="")
        return self._observed

    def check(self, worktree=None) -> Lease:
        if self._observed is None:
            raise WorkspaceLeaseLost("lost", f"{self.holder!r} never observed {self.worktree}")
        cur = self.read()
        seen = self._seen
        unchanged = (seen is not None and cur is not None
                     and (cur.holder, cur.generation) == (seen.holder, seen.generation))
        if cur is None or cur.holder == self.holder or unchanged:
            return cur if cur is not None else self._observed
        raise WorkspaceLeaseLost(
            "lost", f"{self.worktree} carries the lease of {cur.holder!r} (generation "
                    f"{cur.generation}), not this node's run")

    def release(self, *, reason: str = "released") -> Lease:
        cur = self.read()
        if cur is not None and not cur.released_at:
            raise WorkspaceLeaseLost(
                "held", f"{self.worktree} still carries a live lease of {cur.holder!r}: it ends "
                        f"at {cur.expires_at}")
        assert self._observed is not None
        return self._observed


@dataclass
class Dispatch:
    """One `hoh run` invocation's raw result."""

    exit_code: int
    stdout: str
    stderr: str


def hoh_command() -> list[str]:
    """How this launcher starts `hoh` itself: the interpreter that is running
    it (`sys.executable`), or `$HOH_PYTHON` when the operator names one.

    The bare `python3` found on `PATH` is not the interpreter HoH was
    installed into on Nix profiles, pipx installs or any host with several
    Pythons: it lacks `hoh` and `pydantic`, and every dispatched node fails.
    """
    return [os.environ.get("HOH_PYTHON") or sys.executable, "-m", "hoh.cli"]


def _declares_a_path(node: TaskNode) -> bool:
    """Whether the node declares a write or a read. A declaration that cannot even be asked
    for (an attribute that raises) counts as declared: the footprint then reports it."""
    try:
        return bool(node.writes or node.semantic_reads)
    except Exception:                                          # noqa: BLE001 - fail closed
        return True


class HohRunLauncher(RunLauncher):
    """Drives real runs through the `hoh` CLI and the ordinary git mainline."""

    def __init__(
        self,
        root: Path | str,
        repo_path: Path | str,
        *,
        mainline: str = "master",
        iterations: int = 2,
        planner: str = "claude",
        developer: str = "claude",
        qa: str = "claude",
        timeout: int = 7200,
        dry_run: bool = False,
        approvals: "ApprovalProvider | None" = None,
        isolation: str = "none",
        dispatch_budget: int | None = None,
        dispatch_reservation: int | None = None,
        storage_root: Path | str | None = None,
        storage_admission_factory=None,
        master_session: str | None = None,
        master_epoch: int | None = None,
        role_accounts: dict[str, str] | None = None,
        wave_width: int = 1,
        routing_policy: "Path | str | None" = None,
        qa_reserve: int = 0,
        preflight: PreflightRequirement | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        #: P3-02 slice 2: how many independent READY nodes the project loop may
        #: launch together through the wave executor. 1 -- the default -- is the
        #: one-node-per-round loop of before (LINEAR); a larger width is this
        #: deployment's choice until the modes of P3-25/P3-26 compute one.
        if (isinstance(wave_width, bool) or not isinstance(wave_width, int)
                or not 1 <= wave_width <= MAX_WAVE_WIDTH):
            raise ValueError(f"wave_width must be a whole number from 1 to {MAX_WAVE_WIDTH}, "
                             f"not {wave_width!r}")
        self.wave_width = wave_width
        #: run id -> the id of the hold `prepare` took for it, so that a node a wave
        #: refuses after its `prepare` gives back that hold and no other.
        self._prepared_holds: dict[str, str] = {}
        #: P3-INV1: run id -> the amount of the open hold this launcher took for it, which the
        #: run's `hoh run` is told as `--dispatch-ceiling` (a hold is what the run may spend, so
        #: the run is stopped there). Dropped when the hold is ended; kept while it is open.
        self._ceilings: dict[str, int] = {}
        #: The wave executor launches nodes from several threads; the boot claim of
        #: the storage admission is one act per boot, never two at once.
        self._boot_lock = threading.Lock()
        #: P2-43b: the governed master this launcher works for, passed to every
        #: `hoh run` it starts (`--master-session`, `--master-epoch`) so the
        #: run's parks are bound to it. None: the runs record `master: NONE`.
        self.master_session = master_session
        self.master_epoch = master_epoch
        #: role name -> operator-registered account handle (`--role-account`).
        from .contracts import Role
        from .dispatchrecovery import RecoveryError, check_account_handle

        known = {r.value for r in Role}
        if any(str(r) not in known for r in (role_accounts or {})):
            raise RecoveryError("role_accounts names a role that is not planner, "
                                "developer or qa (the name is not shown)")
        self.role_accounts = {str(r): check_account_handle(h)
                              for r, h in (role_accounts or {}).items()}
        #: STORAGE-B3: the storage state root every spawned `hoh run` is given
        #: (`--storage-root`): this launcher's own configuration, else
        #: `$HOH_STATE_ROOT` as resolved now, at construction. None leaves the
        #: child to resolve its own default -- which refuses an uninitialised
        #: root rather than inventing one.
        configured = storage_root or os.environ.get("HOH_STATE_ROOT") or None
        self.storage_root = Path(configured).expanduser() if configured else None
        #: Builds the admission whose boot the launcher claims before it
        #: spawns (Revision 9 item 7); injectable for tests.
        self._storage_admission_factory = storage_admission_factory
        self.repo_path = Path(repo_path).expanduser().resolve()
        self.mainline = mainline
        self.iterations = iterations
        #: A ceiling on role dispatches shared by **every run this launcher
        #: starts**, node and repair nodes alike. Without it each run got its
        #: own ceiling, so a node that spawned one repair spent twice what a
        #: single run may -- which is how a benchmark whose premise is a
        #: matched budget handed one arm double the resource. `None` keeps the
        #: old behaviour: no ceiling at all.
        self.dispatch_budget = dispatch_budget
        #: The most dispatches one started run holds out of the shared budget
        #: (P3-04). `None` holds everything that is left, which is what a run
        #: was handed before reservations existed; a number lets that many
        #: runs be prepared at once without any of them taking the others'
        #: share. Meaningless without a budget, so it is refused without one.
        if dispatch_reservation is not None:
            if dispatch_budget is None:
                raise ValueError("dispatch_reservation needs a dispatch_budget "
                                 "to hold a share of")
            if (isinstance(dispatch_reservation, bool)
                    or not isinstance(dispatch_reservation, int)
                    or dispatch_reservation < 1):
                raise ValueError(f"dispatch_reservation must be a whole number of at "
                                 f"least 1, not {dispatch_reservation!r}")
        self.dispatch_reservation = dispatch_reservation
        #: P3-23: of the shared budget, this many dispatches are never granted to
        #: a node that is not QA/integration work -- a node that is not a repair
        #: (`TaskNode.repair_of`, the node a failed gate creates) holds with
        #: `keep=qa_reserve`, so a wave of developers cannot leave nothing for the
        #: repair of what integration found wrong. 0: no reserve.
        if (isinstance(qa_reserve, bool) or not isinstance(qa_reserve, int)
                or qa_reserve < 0):
            raise ValueError(f"qa_reserve must be a whole number of at least 0, "
                             f"not {qa_reserve!r}")
        if qa_reserve:
            if dispatch_budget is None:
                raise ValueError("qa_reserve needs a dispatch_budget (nothing to keep it from)")
            if qa_reserve >= dispatch_budget:
                raise ValueError(f"qa_reserve {qa_reserve} must be below the dispatch_budget "
                                 f"{dispatch_budget}: a reserve of the whole budget refuses "
                                 f"every node that is not a repair")
        self.qa_reserve = qa_reserve
        #: Holds this launcher could not end after a dispatch (the ledger was
        #: unreadable or the disk full). The hold stays open -- a budget that
        #: cannot be known is not a budget of zero -- and the verdict of the
        #: run is not changed by it, so the reason is kept here to be quoted.
        self.reservation_notes: list[str] = []
        #: P3-11: the environment report a start has to be allowed by -- which
        #: file, for which profile, how old at most. `None` asks nothing, which
        #: is what every launcher did before. Read afresh at every `prepare`, so
        #: a re-measurement is seen by the next start without a restart.
        if preflight is not None and not isinstance(preflight, PreflightRequirement):
            raise ValueError("preflight must be a PreflightRequirement or None, "
                             f"not {type(preflight).__name__}")
        self.preflight = preflight
        #: what the constructor was given, kept apart: `bind_preflight` recomputes the effective
        #: requirement of each project from it (nothing is carried from one project to another).
        self._ctor_preflight = preflight
        #: why the last bind refused, if it did: a launcher whose bind failed starts nothing.
        self._preflight_bind_failed: str | None = None
        #: P3-11 slice 3: the project's recorded waiver, if it declares one (`bind_preflight`).
        self.preflight_waiver: PreflightWaiver | None = None
        #: P3-11 slice 3: whether the effective requirement is the product default (the only one the
        #: launcher mends with a measurement of its own), why the last measurement did not give a
        #: usable report, and who the interpreter the typed commands use is (asked once per path).
        self._preflight_default = False
        self._measure_note: str | None = None
        self._identity_cache: dict[str, dict] = {}
        # Named explicitly, never defaulted. `hoh run --qa` defaults to a
        # harness whose credential expired here, and the resulting run produced
        # no QA verdict at all while looking like an ordinary dispatch.
        self.planner, self.developer, self.qa = planner, developer, qa
        #: P3-20 slice 3: the routing policy file of the runs this launcher starts, and its
        #: digest at the bind. `None`: no routing (every run started as before).
        self.routing_policy: Path | None = None
        self._routing_digest: str | None = None
        #: what the constructor was given, kept apart: `bind_routing` recomputes the effective
        #: policy of each project from it, so one project's declaration is never carried to
        #: another project this launcher object is bound to.
        self._ctor_policy: Path | None = None
        self._ctor_digest: str | None = None
        #: why the last bind refused, if it did: a launcher whose bind failed starts nothing.
        self._routing_bind_failed: str | None = None
        #: P3-30 slice 2: the autonomy contract file of the project this launcher is bound to
        #: (`orchestrator.AUTONOMY_FILE`), passed to every `hoh run` it starts so the runs are
        #: pinned to it. `None`: the runs are started without one, as before.
        self.autonomy_file: Path | None = None
        if routing_policy is not None:
            self._ctor_policy, self._ctor_digest = self._load_routing_file(routing_policy)
            self.routing_policy, self._routing_digest = self._ctor_policy, self._ctor_digest
        self.timeout = timeout
        self.dry_run = dry_run
        # How the acceptance checks of every run this launcher starts are
        # executed. Passed on the command line and then persisted on the run,
        # so a resumed run keeps it -- a project whose nodes were verified
        # under different isolation regimes would carry one word for two
        # different claims.
        self.isolation = isolation
        # Granting a worktree trust is an authority a deployment may hold, and
        # HoH deliberately does not: trusting on request would let anything
        # that can name a path obtain an agent's access to it. Absent by
        # default -- with no provider the run blocks and a person answers,
        # which is what happened before this existed.
        self.approvals = approvals or NoApprovalProvider()
        #: Every answer the authority gave, so a halt can quote it.
        self.approvals_given: list[Approval] = []

    # -- P3-20 slice 3: the routing of the runs this launcher starts ---------- #

    @staticmethod
    def _load_routing_file(path: "Path | str") -> "tuple[Path, str]":
        """`(absolute file, digest)` of a routing policy, loaded fail-closed. `ValueError` when it
        cannot be loaded (the words of the slice-1 reader, never a value read)."""
        from .routing import RoutingRefused, load

        file = Path(os.path.abspath(os.path.expanduser(str(path))))
        try:
            policy = load(file)
        except RoutingRefused as exc:
            raise ValueError(f"routing policy refused: {exc}") from None
        return file, policy.digest

    def bind_preflight(self, project_dir: "Path | str") -> None:
        """Bind the environment requirement of one project (P3-11 slices 2 and 3).

        The effective requirement is **recomputed at every bind**: the project's
        `<project>/preflight.json` when it declares one, else the one the constructor was given,
        else -- for a launcher that starts runs (slice 3) -- the product default
        (`preflightgate.default_requirement`: the `unattended` profile, a report of at most a day
        at `<project>/preflight-report.json`). The file may instead be a recorded **waiver**
        (`PREFLIGHT_WAIVER_SCHEMA`): the project is then gated by nothing, and
        `preflight_waiver` carries who decided it, when and why. A declaration that is there and
        cannot be read, that differs from the constructor's requirement, or that is a waiver
        where the constructor was given a requirement, is a `ValueError`: the drive halts with
        the reason before any run is started, and this launcher starts nothing (`prepare` and
        `launch` refuse). A dry-run launcher starts nothing and is gated by nothing. When the **default**
        applies and its report is absent, old or for another place, the bind measures a new one
        (`preflightgate.measure_default`: once per project and UTC hour, bounded, recorded before it
        is made); a requirement the constructor or the project declared is never mended that way.
        The report
        itself is read afresh at every `prepare` and, in the process the launcher types, at every
        `hoh start` / `hoh run`. `bind_routing` calls this first: the governed drive calls one
        bind, and it is the only place the project directory reaches the launcher."""
        self._preflight_bind_failed = None
        self.preflight_waiver = None
        self._preflight_default = False
        self._measure_note = None
        project_dir = Path(os.path.abspath(os.path.expanduser(str(project_dir))))
        declared = project_dir / PREFLIGHT_FILE
        self.preflight = self._ctor_preflight
        if not os.path.lexists(declared):
            if self._ctor_preflight is None and not self.dry_run:
                self.preflight = default_requirement(project_dir)
                self._preflight_default = self.preflight is not None
                if self._preflight_default:
                    self._mend_default()
            return
        try:
            requirement = _load_preflight_file(declared)
            if isinstance(requirement, PreflightWaiver):
                if self._ctor_preflight is not None:
                    raise ValueError(
                        f"preflight requirement changed: this launcher is bound to profile "
                        f"{self._ctor_preflight.profile!r}, but {declared} waives the "
                        "preflight; a project file may not loosen what the launcher was built with")
                self.preflight = None
                self.preflight_waiver = requirement
                return
            if self._ctor_preflight is not None and requirement != self._ctor_preflight:
                raise ValueError(
                    f"preflight requirement changed: this launcher is bound to profile "
                    f"{self._ctor_preflight.profile!r}, report "
                    f"{ascii(str(self._ctor_preflight.report))}, at most "
                    f"{self._ctor_preflight.max_age_seconds} s, but {declared} declares "
                    f"profile {requirement.profile!r}, report {ascii(str(requirement.report))}, "
                    f"at most {requirement.max_age_seconds} s")
        except ValueError as exc:
            self.preflight = None
            self._preflight_bind_failed = f"preflight requirement refused: {exc}"
            raise ValueError(self._preflight_bind_failed) from None
        self.preflight = requirement

    def _preflight_options(self) -> list[str]:
        """The three options that hand the requirement to the `hoh start` / `hoh run` this
        launcher types: the CLI asks the report again, at the start of that process (which is
        what closes slice 1's L2: a `launch` that was never `prepare`d is gated too)."""
        if self.preflight is None:
            return []
        return ["--preflight-report", str(self.preflight.report),
                "--preflight-profile", self.preflight.profile,
                "--preflight-max-age", str(self.preflight.max_age_seconds)]

    def _run_measurement(self, argv: list[str], timeout: int):
        """The process boundary of the measurement the default is mended with: `(exit code,
        stdout, stderr)` of `argv`, killed after `timeout` seconds. Replaced in tests."""
        return run_tool(argv, timeout)

    def _typed_identity(self) -> dict:
        """Who the interpreter is that the commands this launcher types run under
        (`hoh_command()[0]`: `$HOH_PYTHON`, else this process's)."""
        python = hoh_command()[0]
        if python not in self._identity_cache:
            self._identity_cache[python] = interpreter_identity(python)
        return self._identity_cache[python]

    def _ask_preflight(self):
        """The gate, asked for this launcher's places and the interpreter its commands use."""
        req = self.preflight
        places = req.bind_places
        return check_preflight(req, repo=self.repo_path if places else None,
                               root=self.root if places else None, identity=self._typed_identity())

    def _mend_default(self) -> None:
        """The default's report is absent, old or for another place: measure a new one, once.
        Whatever else is wrong with a report (a verdict, an unreadable file) is evidence this
        does not replace."""
        try:
            self._ask_preflight()
        except PreflightRefused as exc:
            if exc.kind not in MENDABLE:
                return
        else:
            return
        self._measure_note = measure_default(
            self.preflight, python=hoh_command()[0], repo=self.repo_path, root=self.root,
            runner=self._run_measurement)

    def bind_routing(self, project_dir: "Path | str") -> None:
        """Bind the routing of one project (the governed drive calls this once the lease is held,
        like `bind_master`).

        The effective policy is **recomputed at every bind**: the project's
        `<project>/routing-policy.json` when it declares one, else the one the constructor was
        given, else none -- nothing is carried over from a project this object was bound to
        before. It is loaded fail-closed and its digest is pinned, create-only, in
        `<project>/routing-pin.json` at the first bind (a dry-run launcher binds and pins
        nothing). A later bind that finds another digest, a policy that is gone while the pin
        stays, a damaged pin, or a project policy that is not the constructor's, is a
        `ValueError`: the drive halts with the reason before any run is started, and this
        launcher starts nothing. A project that declares nothing, has no pin and was given no
        constructor policy binds nothing (the runs are what they were)."""
        self.bind_preflight(project_dir)          # a refusal here halts the drive, by name
        self._routing_bind_failed = None
        try:
            self._bind_routing(project_dir)
        except ValueError as exc:
            self.routing_policy, self._routing_digest = None, None
            self._routing_bind_failed = str(exc)
            raise
        # P3-30 slice 2: the project's autonomy contract, bound anew at every bind like the routing.
        self.autonomy_file = None
        try:
            self._bind_autonomy(project_dir)
        except ValueError as exc:
            self._routing_bind_failed = str(exc)
            raise

    def _bind_routing(self, project_dir: "Path | str") -> None:
        project_dir = Path(os.path.abspath(os.path.expanduser(str(project_dir))))
        declared = project_dir / ROUTING_POLICY_FILE
        pinned = _read_project_routing_pin(project_dir)
        self.routing_policy, self._routing_digest = self._ctor_policy, self._ctor_digest
        if os.path.lexists(declared):
            file, digest = self._load_routing_file(declared)
            if self._ctor_digest is not None and digest != self._ctor_digest:
                raise ValueError(
                    f"routing policy changed: this launcher is bound to policy "
                    f"{self._ctor_digest[:12]} ({self._ctor_policy}), but {declared} declares "
                    f"{digest[:12]}")
            self.routing_policy, self._routing_digest = file, digest
        if self._routing_digest is None:
            if pinned is not None:
                raise ValueError(
                    f"routing policy changed: this project is bound to policy "
                    f"{pinned['digest'][:12]} but {declared} is gone")
            return
        if pinned is not None and pinned["digest"] != self._routing_digest:
            raise ValueError(
                f"routing policy changed: this project is bound to policy "
                f"{pinned['digest'][:12]}, but {self.routing_policy} declares "
                f"{self._routing_digest[:12]}; a bound project is not re-routed")
        if pinned is None and not self.dry_run:
            from .contracts import utcnow

            record = {"schema": ROUTING_PIN_SCHEMA, "digest": self._routing_digest,
                      "source": str(self.routing_policy), "pinned_at": utcnow()}
            try:
                durable_create(project_dir / ROUTING_PIN_FILE,
                               json.dumps(record, indent=2) + "\n", mode=None)
            except FileExistsError:
                # another drive pinned between our read and our write: it must be the same
                again = _read_project_routing_pin(project_dir)
                if again is None or again["digest"] != self._routing_digest:
                    raise ValueError(
                        "routing policy changed: another drive pinned this project to a "
                        "different policy while this one bound it") from None
            except DurableWriteNotSynced:
                pass                      # in place and whole; only its directory entry is unsure

    def _bind_autonomy(self, project_dir: "Path | str") -> None:
        """The project's registered autonomy contract, if it has one, loaded fail-closed: a file that
        is there and cannot be loaded is a `ValueError` (the drive halts before any run is started),
        never "no contract". The contract itself is `hoh run`'s to pin; this only names the file."""
        declared = Path(os.path.abspath(os.path.expanduser(str(project_dir)))) / AUTONOMY_FILE
        if not os.path.lexists(declared):
            return
        from .governance import RoutingRefused, load_contract_file

        try:
            load_contract_file(declared)
        except RoutingRefused as exc:
            raise ValueError(f"autonomy contract refused: {exc}") from None
        self.autonomy_file = declared

    def _routing_now(self):
        """`(policy | None, reason | None)`: the bound policy re-read **now**, so that a policy
        file edited, replaced or damaged since the bind refuses the node that would start under
        it. `(None, None)` for a launcher with no policy -- what every launcher was."""
        if self._routing_bind_failed is not None:
            return None, f"{self._routing_bind_failed}; nothing is started under it"
        if self.routing_policy is None:
            return None, None
        from .routing import RoutingRefused, load

        try:
            policy = load(self.routing_policy)
        except RoutingRefused as exc:
            return None, (f"routing policy refused: {self.routing_policy} cannot be loaded "
                          f"({exc}); nothing is started under it")
        if policy.digest != self._routing_digest:
            return None, (f"routing policy changed: {self.routing_policy} declares "
                          f"{policy.digest[:12]} but this launcher is bound to "
                          f"{(self._routing_digest or '')[:12]}; nothing is started under it")
        return policy, None

    def _role_options(self, policy) -> list[str]:
        """`--planner/--developer/--qa` for the roles the policy gives no provider: the launcher's
        named harness is typed only there, so a routed role takes the policy's provider (`hoh run`
        fills an unset profile from it and refuses a typed different one)."""
        from .contracts import Role

        out: list[str] = []
        for role, flag, word in ((Role.PLANNER, "--planner", self.planner),
                                 (Role.DEVELOPER, "--developer", self.developer),
                                 (Role.QA, "--qa", self.qa)):
            route = policy.roles.get(role) if policy is not None else None
            if route is None or route.provider is None:
                out += [flag, word]
        return out

    def bind_master(self, session: str, epoch: int) -> None:
        """Tell the launcher which master it launches for (the governed drive
        calls this once the lease is held)."""
        self.master_session, self.master_epoch = session, int(epoch)

    # -- classification ---------------------------------------------------- #

    def action_class(self, node: TaskNode) -> ActionClass:
        """The node's declared class. Not re-derived here on purpose.

        Whether a node is externally irreversible is a policy decision that
        belongs in the state, where a human can see it and a restart preserves
        it -- not something re-inferred from a spec on every round, where a
        wording change could quietly downgrade it.
        """
        return node.action_class

    def depends_on(self, a: TaskNode, b: TaskNode) -> bool:
        """Whether two nodes may not proceed in parallel.

        `conflict.detect` over each node's **declared** footprint plus what is derived from it:
        the **derived reads** of its Python files (P3-21 slices 2-3, `derivedreads`: the
        repository files they import -- to a bounded closure --, the data files they open by a
        literal path and, for an outside package, the dependency manifests) and the **candidate
        changes** of the node (slice 3: the files its branch already changed against the
        mainline, counted as writes). A real `bool`, because the frontier planner treats only an
        exact `False` as independence. A node whose declaration, Python sources or branch cannot
        be trusted (a file that does not parse, a closure over its bound, a directory that cannot be
        listed or is too big, a git that fails) conflicts with everything; it is still chosen
        when it is first in the frontier, so the plan always makes progress. A declared directory or
        glob is expanded over the existing Python files beneath it, each read like a declared file.
        The planner and the wave executor both consult this method before two nodes are admitted
        together.

        Two things stay as they were: a node that declares neither a write nor a read **and has
        changed nothing on its branch** is independent of everything (it names nothing that could
        meet; most project nodes are written so), and a call without a launcher
        (`HohRunLauncher.depends_on(None, a, b)`, the tests' stand-in for "the product's rule")
        compares the declarations only, because there is no repository to read sources or branches
        from. A real launcher always has one.

        The old rule -- exact-string intersection of `writes` and `semantic_reads` -- is a
        strict subset of this one for any declaration the new reader accepts.
        """
        from .conflict import detect

        changed_a = HohRunLauncher._candidate(self, a)
        changed_b = HohRunLauncher._candidate(self, b)
        if not (_declares_a_path(a) or changed_a[0] or changed_a[1]) \
                or not (_declares_a_path(b) or changed_b[0] or changed_b[1]):
            return False
        footprint = HohRunLauncher._footprint
        return bool(detect(footprint(self, a, changed_a), footprint(self, b, changed_b)))

    #: P3-21 slice 2. Parsed imports by (path, sha256 of the text): re-parsing every declared
    #: file for every pair would cost seconds per planning round. Bounded; cleared when full.
    _IMPORT_CACHE_MAX = 256
    #: P3-21 slice 3. Files a node's branch may have changed before the node is held apart from
    #: everything, and the seconds each git question may take.
    CANDIDATE_MAX_FILES = 256
    CANDIDATE_GIT_TIMEOUT = 30

    def _candidate_changes(self, node: TaskNode) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(files, problems) of `_candidate`: what the node's branch has already changed against the mainline."""
        return HohRunLauncher._candidate(self, node)[:2]

    def _candidate(self, node: TaskNode) -> tuple[tuple[str, ...], tuple[str, ...], str]:
        """(files, problems, tip): what the node's branch has already changed against the mainline, and
        the commit id of the branch (`""` when there is no branch or the answer is a problem).

        The branch is the one `merge` would integrate (`node.branch`, else `hoh-<run id or node
        id>`); a branch that does not exist is a node that has not started: nothing, no problem.
        The changes are the files of `git diff <mainline>...<branch>` (the work since the merge
        base, renames as a delete and an add), asked of git by commit id, read-only. Anything else
        is a problem, never a partial list: a git that fails or times out, a mainline that cannot
        be resolved while the branch exists, output that is not text, more than
        `CANDIDATE_MAX_FILES` files, a name that starts with `-`. A launcher without a repository
        or a mainline (the tests' stand-in), and a repository path with no `.git` entry (a plain
        directory holds no branches; every real launcher works in a git work tree), read nothing --
        but a path that *has* a `.git` entry and cannot be asked is a problem.
        """
        repo, mainline = getattr(self, "repo_path", None), getattr(self, "mainline", None)
        if repo is None or mainline is None:
            return (), (), ""
        if not os.path.lexists(f"{repo}{os.sep}.git"):
            return (), (), ""                       # not a git work tree: it holds no branches to read
        try:
            branch = node.branch or f"hoh-{node.run_id or node.id}"
            if (not isinstance(branch, str) or not isinstance(mainline, str)
                    or not branch or not mainline or branch.startswith("-") or mainline.startswith("-")):
                return (), ("the branch of this node, or the mainline, is not a usable name",), ""
            label = f"candidate changes of branch {ascii(branch)[:60]}"
            timeout = self.CANDIDATE_GIT_TIMEOUT
            tip = self._git("rev-parse", "--verify", "--quiet", branch + "^{commit}", timeout=timeout)
            if tip.returncode == 1 and not (tip.stdout or "").strip():
                return (), (), ""                                  # no such branch: nothing started
            if tip.returncode != 0:
                return (), (f"{label}: git could not resolve the branch (exit {tip.returncode})",), ""
            base = self._git("rev-parse", "--verify", "--quiet", mainline + "^{commit}", timeout=timeout)
            if base.returncode != 0:
                return (), (f"{label}: git could not resolve the mainline (exit {base.returncode})",), ""
            tip_id, base_id = tip.stdout.strip(), base.stdout.strip()
            if not (re.fullmatch(r"[0-9a-f]{40,64}", tip_id) and re.fullmatch(r"[0-9a-f]{40,64}", base_id)):
                return (), (f"{label}: git did not answer with commit ids",), ""
            diff = self._git("diff", "--name-only", "-z", "--no-renames", "--no-ext-diff",
                             f"{base_id}...{tip_id}", "--", timeout=timeout)
            if diff.returncode != 0:
                return (), (f"{label}: git could not list the changes (exit {diff.returncode})",), ""
            files = tuple(name for name in diff.stdout.split("\0") if name)
            if len(files) > self.CANDIDATE_MAX_FILES:
                return (), (f"{label}: more than {self.CANDIDATE_MAX_FILES} files changed",), ""
            return files, (), tip_id
        except Exception:                                      # noqa: BLE001 - fail closed
            return (), ("the candidate changes of this node could not be read",), ""

    #: P3-21 slice 3. Branch sources fetched per node: the ceiling of declared Python files plus one,
    #: so that more of them is the problem `derive` reports and not a truncated read.
    BRANCH_MAX_PY = 65

    def _git_bytes(self, *args: str, stdin: bytes) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", *args], input=stdin, capture_output=True, cwd=str(self.repo_path),
                timeout=self.CANDIDATE_GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(["git", *args], 124, b"", b"timed out")

    def _branch_view(self, tip: str, files: tuple) -> tuple[dict, dict, tuple]:
        """(exists, sources, problems) for the files a branch changed, as the branch has them: whether
        each exists at commit `tip` (a file the branch deleted does not) and the text of each Python
        file (an exception for one that cannot be read as source). Two read-only `git cat-file --batch`
        questions, by commit id; anything unexpected -- a file name with a newline, a failing git, an
        answer that does not match the question -- is a problem, never a guess."""
        from .derivedreads import MAX_SOURCE_BYTES

        label = "the branch's own content of the changed files"
        if any("\n" in name for name in files):
            return {}, {}, (f"{label}: a changed file name holds a newline",)
        check = self._git_bytes("cat-file", "--batch-check",
                                stdin="".join(f"{tip}:{name}\n" for name in files).encode("utf-8"))
        if check.returncode != 0:
            return {}, {}, (f"{label}: git could not describe them (exit {check.returncode})",)
        rows = check.stdout.decode("utf-8").split("\n")
        if rows and rows[-1] == "":
            rows.pop()
        if len(rows) != len(files):
            return {}, {}, (f"{label}: git answered {len(rows)} rows for {len(files)} files",)
        exists: dict[str, bool] = {}
        sizes: dict[str, int] = {}
        for name, row in zip(files, rows, strict=True):
            if row.endswith(" missing"):
                exists[name] = False
                continue
            fields = row.split(" ")
            if len(fields) != 3 or not re.fullmatch(r"[0-9a-f]{40,64}", fields[0]) \
                    or not fields[2].isdigit():
                return {}, {}, (f"{label}: git described a file in a way that is not understood",)
            exists[name] = True
            if fields[1] == "blob":
                sizes[name] = int(fields[2])
        sources: dict[str, object] = {}
        fetch = []
        for name in files:
            if name.endswith(".py") and name in sizes and len(fetch) < self.BRANCH_MAX_PY:
                if sizes[name] > MAX_SOURCE_BYTES:
                    sources[name] = ValueError("larger than the source ceiling")
                else:
                    fetch.append(name)
        if fetch:
            blobs = self._git_bytes("cat-file", "--batch",
                                    stdin="".join(f"{tip}:{name}\n" for name in fetch).encode("utf-8"))
            if blobs.returncode != 0:
                return {}, {}, (f"{label}: git could not read them (exit {blobs.returncode})",)
            data, at = blobs.stdout, 0
            for name in fetch:
                end = data.find(b"\n", at)
                header = data[at:end].decode("utf-8", "replace") if end >= 0 else ""
                found = re.fullmatch(r"[0-9a-f]{40,64} blob (\d+)", header)
                if found is None or int(found.group(1)) != sizes[name]:
                    return {}, {}, (f"{label}: git answered with something that is not the blob asked for",)
                start = end + 1
                body = data[start:start + sizes[name]]
                at = start + sizes[name] + 1
                try:
                    sources[name] = body.decode("utf-8")
                except UnicodeDecodeError as exc:
                    sources[name] = exc
        return exists, sources, ()

    def _footprint(self, node: TaskNode, changed: tuple | None = None):
        """The node's declared footprint with its derived reads and candidate changes; a node that
        cannot be read is an UNDECIDABLE footprint, never an exception. `changed` is
        `_candidate(node)` when the caller has it already.

        The Python files a branch changed are read **from the branch** (`_branch_view`: `git cat-file`
        by commit id), not from the repository's checkout: what the branch's version imports is what the
        merge will bring, and a file that is new on the branch exists for the derivation although the
        checkout has not got it yet. Everything else (the imported modules the branch did not touch, the
        data files, the declared directories) is read from the checkout, as in slice 2."""
        import dataclasses

        from . import conflict, derivedreads

        try:
            declared = conflict.footprint_of(node)
            files, change_problems, tip = (HohRunLauncher._candidate(self, node)
                                           if changed is None else (*changed, "")[:3])
            writes = [*(s for _, s in declared.writes), *files]
            paths = [*(spelling for _, spelling in (*declared.writes, *declared.reads)), *files]
            view_exists: dict[str, bool] = {}
            view_sources: dict[str, object] = {}
            if files and tip and not change_problems:
                view_exists, view_sources, view_problems = self._branch_view(tip, files)
                change_problems = (*change_problems, *view_problems)

            def read_text(path: str):
                if path in view_exists:
                    if not view_exists[path]:
                        return None                    # deleted on the branch
                    found = view_sources.get(path)
                    if found is None or isinstance(found, BaseException):
                        raise found if found is not None else ValueError(
                            "the branch's version of this file was not read")
                    return found
                return self._read_source(path)

            def exists(path: str) -> bool:
                return view_exists[path] if path in view_exists else self._source_exists(path)

            if getattr(self, "repo_path", None) is None:
                derived = derivedreads.Derived(())     # no repository: declarations only
            else:
                walked = [0]                           # directory entries examined for this node
                derived = derivedreads.derive(
                    paths, read_text, exists,
                    lambda path: self._list_python(path, walked), self._parsed_imports)
            built = conflict.footprint(
                declared.item, writes, [*(s for _, s in declared.reads), *derived.reads])
            # what `conflict.footprint` refused of the derived paths is a problem too (slice 2 replaced the
            # problems of `built` and lost it: a derived read it could not take was dropped, not reported)
            refused = tuple(p for p in built.problems if p not in declared.problems)
            problems = (*declared.problems, *change_problems, *derived.problems, *refused)
            if len(problems) > conflict.MAX_PROBLEMS:
                problems = (*problems[:conflict.MAX_PROBLEMS],
                            f"{len(problems) - conflict.MAX_PROBLEMS} more problems not listed")
            return dataclasses.replace(built, problems=problems)
        except Exception:                                      # noqa: BLE001 - fail closed
            return conflict.Footprint(item="", writes=(), reads=(), problems=(
                "the footprint of this node could not be derived",))

    def _read_source(self, path: str) -> str | None:
        """The text of the repository file `path` (a canonical relative spelling), or None when
        it does not exist. Opened without following a link and without blocking, regular
        files only, at most `derivedreads.MAX_SOURCE_BYTES` + 1 bytes; anything else raises
        (the caller turns that into a problem of the node)."""
        from .derivedreads import MAX_SOURCE_BYTES

        repo = getattr(self, "repo_path", None)
        if repo is None:
            raise RuntimeError("this launcher has no repository to read declared sources from")
        try:
            fd = os.open(repo / path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except (FileNotFoundError, NotADirectoryError):
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("not a regular file")
            raw = b""
            while len(raw) <= MAX_SOURCE_BYTES:
                chunk = os.read(fd, MAX_SOURCE_BYTES + 1 - len(raw))
                if not chunk:
                    break
                raw += chunk
        finally:
            os.close(fd)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError("larger than the source ceiling")
        return raw.decode("utf-8")

    def _list_python(self, path: str, walked: list[int]) -> tuple[str, ...] | None:
        """The existing Python files beneath the directory `path` (`''`: the repository root), as
        canonical relative paths, or None when `path` is not a directory (a file that is not
        Python, or nothing yet). Never follows a link; `.git` and `__pycache__` are skipped;
        `walked[0]` counts the entries examined for the node and everything beyond
        `derivedreads.MAX_WALK_ENTRIES` or `MAX_WALK_DEPTH` raises (the caller turns it into a
        problem of the node; `derive` bounds the number of Python files)."""
        from .derivedreads import MAX_WALK_DEPTH, MAX_WALK_ENTRIES, SKIPPED_DIRECTORIES

        root = self.repo_path / path if path else self.repo_path
        try:
            mode = os.lstat(root).st_mode
        except (FileNotFoundError, NotADirectoryError):
            return None
        if stat.S_ISLNK(mode):
            raise ValueError("a link is not followed")
        if not stat.S_ISDIR(mode):
            return None
        found: list[str] = []
        stack = [(str(root), path, 0)]
        while stack:
            directory, rel, depth = stack.pop()
            if depth > MAX_WALK_DEPTH:
                raise ValueError(f"deeper than {MAX_WALK_DEPTH} levels")
            with os.scandir(directory) as entries:
                for entry in entries:
                    walked[0] += 1
                    if walked[0] > MAX_WALK_ENTRIES:
                        raise ValueError(f"more than {MAX_WALK_ENTRIES} directory entries")
                    name = entry.name
                    child = f"{rel}/{name}" if rel else name
                    if entry.is_symlink():
                        if name.endswith(".py"):
                            found.append(child)        # reading it refuses a link, by name
                        elif os.path.isdir(entry.path):
                            raise ValueError("a link to a directory is not followed")
                    elif entry.is_dir(follow_symlinks=False):
                        if name not in SKIPPED_DIRECTORIES:
                            stack.append((entry.path, child, depth + 1))
                    elif name.endswith(".py"):
                        found.append(child)
        return tuple(found)

    def _source_exists(self, path: str) -> bool:
        repo = getattr(self, "repo_path", None)
        return repo is not None and os.path.isfile(f"{repo}{os.sep}{path}")

    def _parsed_imports(self, path: str, text):
        import hashlib

        from .derivedreads import parse_imports

        if not isinstance(text, str):
            return parse_imports(path, text)
        cache = self.__dict__.setdefault("_import_cache", {})
        key = (path, hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest())
        found = cache.get(key)
        if found is None:
            if len(cache) >= self._IMPORT_CACHE_MAX:
                cache.clear()
            found = cache[key] = parse_imports(path, text)
        return found

    # -- dispatch ---------------------------------------------------------- #

    def _run_cli(self, argv: list[str]) -> Dispatch:
        env = dict(os.environ)
        env.setdefault("HERDR_ENV", "1")
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=self.timeout,
            cwd=str(self.repo_path), env=env,
        )
        return Dispatch(p.returncode, p.stdout, p.stderr)

    #: A merge runs under the program lock (it must not race a successor's),
    #: so a git that hangs -- a hook, a signing prompt, a credential helper --
    #: must not hold that lock for ever (review A3, N3).
    MERGE_TIMEOUT = 600

    def _git(self, *args: str, cwd: Path | None = None,
             timeout: float | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", *args], capture_output=True, text=True,
                cwd=str(cwd or self.repo_path), timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(["git", *args], 124, "",
                                               f"git timed out after {timeout} s")

    def prepare(self, node: TaskNode) -> str | None:
        """Creates the worktree, specification and run for a node that has none.

        A repair node arrives from a gate failure carrying what went wrong and
        nothing else. Turning that into runnable work means writing a
        specification and starting a run, and during this project's own
        campaign a person did that by hand every time -- which is precisely
        where the loop stopped being able to continue on its own.

        The generated specification is deliberately thin: it states the failing
        gate, its output, and that the fix must make that gate green without
        changing what the gate measures. It does not try to diagnose. Guessing
        at a cause and writing it into a specification would hand the planner a
        conclusion instead of a problem.
        """
        if self.dry_run:
            return "dry run: nothing is prepared"
        # The environment is asked before anything else, the budget included
        # and a hold on it above all (P3-04): a refusal between the hold and
        # the start would leave the hold open for a run that never began. It
        # covers a run that exists as well as a new one: that run is
        # dispatched next, unless the orchestrator's `evaluate` then reads it
        # as accepted or in flight -- and then this refusal also holds back
        # that settlement until the report is fresh and the node unblocked.
        unfit = self._refuse_on_preflight()
        if unfit is not None:
            return unfit
        # P3-20 slice 3: the routing policy, re-read now. A reason, like the preflight's: the
        # node is BLOCKED with it and nothing is created.
        routing_policy, unrouted = self._routing_now()
        if unrouted is not None:
            return unrouted
        # Asked before anything is created. `budget_argumente` raises this
        # too, but by then a worktree exists and a trust approval has been
        # granted for a run that will never start -- side effects for work
        # that was refused.
        self.budget_argumente()
        run_id = node.run_id or node.id
        branch_ = node.branch or f"hoh-{run_id}"
        store = RunStore(self.root, run_id)

        if store.state_exists():
            # The run exists, but that does not mean it can proceed. Blocks
            # come in a chain: the project node, the run, and the worktree's
            # trust. Measured the first time a repair node was dispatched for
            # real -- the node was unblocked, the run was still BLOCKED from
            # its previous attempt, and `hoh run` refused to start at all.
            return self._ensure_runnable(run_id, branch_, store)
        # Before the specification, the worktree and the trust approval: a new
        # run that the other runs' holds leave nothing for is refused here, like
        # a spent budget at the top (the hold itself is taken, under the lock,
        # just before `hoh start`). A run that exists took no hold and is not
        # refused here.
        self._refuse_if_held(run_id, self._kept(node))
        # A repair node's specification belongs to the **run**, not to the
        # repository under test. It used to be written to
        # `<repo>/.hoh-repair-<id>.md`, where it is untracked, unprotected and
        # in the way: this project's own export refused it as an unclassified
        # path, the cleanup that followed moved it aside, and a repair run that
        # was live at the time blocked on "specification is missing". A file
        # that a run depends on must not sit where tidying the repository can
        # remove it.
        #
        # Under `--root` it travels with the run's receipts and state, is
        # readable afterwards, and no repository ever has to ignore it.
        spec_file = Path(node.spec_path) if node.spec_path else (
            self.root / run_id / f"repair-{run_id}.md"
        )
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        if not spec_file.exists():
            if not node.repair_of:
                return (
                    f"node {run_id} has no run and no specification at "
                    f"{spec_file}; there is nothing to dispatch"
                )
            try:
                durable_create(spec_file, self._repair_spec(node), mode=None)
            except FileExistsError:
                if not spec_file.exists():      # a dangling link holds the name
                    return (f"the repair specification path {spec_file} is a dangling "
                            f"link; nothing to dispatch for {run_id}")
            except DurableWriteNotSynced:
                # The specification is in place and whole; only its directory
                # entry is not known to be durable.
                pass
            except OSError as exc:
                return f"could not write the repair specification {spec_file} for {run_id}: {exc}"

        wt = self._run_cli([
            *hoh_command(), "--root", str(self.root),
            "worktree", "--repo", str(self.repo_path), "--branch", branch_,
        ])
        if wt.exit_code != 0 and "already exists" not in (wt.stdout + wt.stderr):
            return f"could not create the worktree for {run_id}: {(wt.stderr or wt.stdout)[-200:]}"
        worktree = self._worktree_path(wt.stdout) or self._worktree_for(branch_)
        if not worktree.is_dir():
            return f"no worktree at {worktree} for {run_id}"

        # Without an approval the run starts and then stops at a trust dialog,
        # which HoH will not answer. Measured the first time a repair node was
        # dispatched for real: the run reached DEVELOPING and blocked on
        # "Yes, I trust this folder".
        release_ = self._approve(worktree)
        if not release_.granted:
            return release_.as_reason()

        argumente = [
            *hoh_command(), "--root", str(self.root), "start",
            "--repo", str(worktree), "--spec", str(spec_file), "--run-id", run_id,
        ]
        if routing_policy is not None:
            argumente += ["--routing-policy", str(self.routing_policy)]
        argumente += self._preflight_options()
        argumente += self._hold_arguments(run_id, self._kept(node))
        try:
            st = self._run_cli(argumente)
        except BaseException:
            self._settle(run_id, "the run could not be started")
            raise
        if st.exit_code != 0:
            self._settle(run_id, "the run could not be started")
            return f"could not start run {run_id}: {(st.stderr or st.stdout)[-200:]}"
        node.branch = branch_
        node.run_id = run_id
        node.spec_path = str(spec_file)
        return None

    def spent_budget(self) -> int:
        """Dispatches every run under this root has already spent.

        Read from the **persisted run states**, not from a counter in this
        process. A counter in memory is reset by a restart, and a budget a
        restart resets is not a budget: a node that crashed after eight
        dispatches would come back with nine more. The runs count their own
        dispatches and write them down, so the number survives the process
        that produced it and cannot be lowered by starting a new one.
        """
        return sum(self._spent_by_run().values())

    def _spent_by_run(self) -> dict[str, int]:
        """The dispatches each run has spent, keyed by the run's directory name.

        `spent_budget` is its sum. A run whose state cannot be read as a count
        is `BudgetExhausted`, never a run that spent nothing.
        """
        spent: dict[str, int] = {}
        if not self.root.is_dir():
            return spent
        try:
            states = self._state_files(self.root)
        except OSError as exc:
            # A directory that cannot be listed may hold the state of the one run
            # that spent the budget: not a spend of zero (`TreeWalkRefused`).
            raise BudgetExhausted(
                f"the shared dispatch budget cannot be computed: {exc}. Nothing "
                f"further is started, because a spend that cannot be read is not "
                f"a spend of zero") from exc
        for verdict_state in states:
            try:
                n = self._dispatches_in(verdict_state)
            except (OSError, ValueError, RecursionError) as exc:
                # Fails **closed**. Skipping an unreadable state counted it as
                # zero, which is the most dangerous possible reading: the one
                # run whose record cannot be read is exactly the one that may
                # have spent the budget. A budget that cannot be computed is
                # not a budget, so nothing more is handed out.
                raise BudgetExhausted(
                    f"the shared dispatch budget cannot be computed: "
                    f"{verdict_state} is unreadable ({exc}). Nothing further is "
                    f"started, because a spend that cannot be read is not a "
                    f"spend of zero") from exc
            key = verdict_state.parent.name
            spent[key] = spent.get(key, 0) + n
        return spent

    @staticmethod
    def _state_files(root: Path) -> list[Path]:
        """Every entry named `state.json` below `root`, at any depth, in path order.

        Not `Path.rglob`, and not a one-level glob. A one-level glob sees only
        runs sitting directly under the root, and a run one directory deeper --
        which nothing in this file forbids -- would have spent dispatches that
        the next run was then handed again. `rglob` sees every depth but its
        answer depends on the interpreter: 3.11 omits a *dangling* `state.json`
        link (so a run whose state is a link counted as zero: the budget failed
        open on the oldest supported Python) and raises `RecursionError` on a
        deep tree. `walk_tree` lists every entry by name on all of them and
        follows no link; whether an entry is a usable state is
        `_dispatches_in`'s question, which refuses a link, a FIFO, a directory.
        A directory that cannot be listed is `TreeWalkRefused` (an `OSError`).
        """
        return [Path(full) for _rel, full in walk_tree(root, skip_denied=False)
                if os.path.basename(full) == "state.json"]

    @staticmethod
    def _dispatches_in(path: Path) -> int:
        """The `usage.dispatches` of one run state, or `ValueError`/`OSError`.

        Opened without following a link and without blocking, and required to
        be a regular file *on the descriptor*: a state that is a FIFO would hang
        the read, a link would be read from wherever it points. The shape is
        checked too -- a state that is not an object, or whose count is not a
        whole number of zero or more, is not a spend of zero.
        """
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("it is not a regular file")
            with os.fdopen(fd, "rb", closefd=False) as fh:
                raw = fh.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise ValueError(f"it is larger than {MAX_STATE_BYTES} bytes")
            data_ = json.loads(raw.decode("utf-8"))
        finally:
            os.close(fd)
        if not isinstance(data_, dict):
            raise ValueError("it is not a JSON object")
        usage = data_.get("usage")
        if usage is None:
            return 0
        if not isinstance(usage, dict):
            raise ValueError("`usage` is not an object")
        n = usage.get("dispatches", 0)
        if n is None:
            return 0
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ValueError(f"`usage.dispatches` is {n!r}, not a whole number of 0 or more")
        return n

    def remaining_budget(self) -> int:
        """What a run started now may still spend. Never negative."""
        if self.dispatch_budget is None:           # pragma: no cover - guarded
            return 0
        return max(self.dispatch_budget - self.spent_budget(), 0)

    def budget_argumente(self) -> list[str]:
        """The ceiling the next run is started with, as CLI arguments.

        A named method rather than two lines inside `prepare` so that the
        instrument control can measure the *wiring* and not only the
        arithmetic: `remaining_budget` returning the right number proves
        nothing if `prepare` passes a constant.

        Raises `BudgetExhausted` when nothing is left. It used to return
        `--max-dispatches 0`, which `Budgets` refuses (a ceiling of zero is
        not a budget, it is a stop), and the run then failed to start with a
        validation error that the orchestrator booked as a missing
        dependency. That is the ambiguous halt at precisely the moment the
        shared budget binds -- the case the shared budget was built for.

        This is the *read*: what has been spent, holds not subtracted. It is
        the early refusal at the top of `prepare`; the ceiling a run is
        actually started with is taken by `_hold_arguments`, under the lock.
        """
        if self.dispatch_budget is None:
            return []
        remaining_ = self.remaining_budget()
        if remaining_ <= 0:
            raise BudgetExhausted(
                f"the shared dispatch budget is spent: "
                f"{self.spent_budget()}/{self.dispatch_budget} used by "
                f"the runs under {self.root}")
        return ["--max-dispatches", str(remaining_)]

    def _refuse_on_preflight(self) -> str | None:
        """Why the environment report does not allow a start, or None.

        A reason, not an exception: the orchestrator turns a returned reason
        into a BLOCKED node and a halt that names it (`orchestrator.py`, the
        `obstacle` branch after `prepare`), and an exception of another type
        would end the session instead.
        """
        if self._preflight_bind_failed is not None:
            return f"{self._preflight_bind_failed}; nothing is started under it"
        if self.preflight is None:
            return None
        try:
            self._ask_preflight()
        except PreflightRefused as exc:
            places = self.preflight.bind_places
            # REPO and ROOT are this launcher's repository and runs directory, which the reason
            # above names when it is they that differ (the report is for the places it names)
            where = " --repo REPO --root ROOT" if places else ""
            deep = " --deep" if self.preflight.profile == "unattended" else ""
            note = f"; {self._measure_note}" if self._measure_note else ""
            return (f"the environment preflight does not allow a start: {exc}{note}. "
                    f"`tools/preflight.py --profile {self.preflight.profile} "
                    f"--out {shown_place(self.preflight.report)}{deep}{where}` "
                    "writes a new report")[:600]
        return None

    def bind_budget(self, dispatch_budget: int, dispatch_reservation: int | None,
                    qa_reserve: int) -> None:
        """P3-23: take the dispatch budget the project declares (`budgetconfig`, the file
        `hoh project budget` writes). The values are checked by the rules of `__init__`
        (one statement, `budgetconfig.check_values`). A launcher that already sets a budget of
        its own is not overridden in silence: the same values are a no-op, any other is a
        `BudgetConfigError` -- two sources of one ceiling is a refusal, by name."""
        from .budgetconfig import BudgetConfigError, check_values

        check_values(dispatch_budget, dispatch_reservation, qa_reserve)
        wanted = (dispatch_budget, dispatch_reservation, qa_reserve)
        own = (self.dispatch_budget, self.dispatch_reservation, self.qa_reserve)
        if own == wanted:
            return
        if own != (None, None, 0):
            raise BudgetConfigError(
                f"the project declares dispatch_budget={dispatch_budget}, "
                f"dispatch_reservation={dispatch_reservation}, qa_reserve={qa_reserve} and this "
                f"launcher sets {own[0]}, {own[1]}, {own[2]} itself: one source of the ceiling "
                "only (remove one)")
        self.dispatch_budget, self.dispatch_reservation, self.qa_reserve = wanted

    def _kept(self, node: TaskNode) -> int:
        """The reserve this node's hold must leave: nothing for a repair node
        (the QA/integration work the reserve exists for), `qa_reserve` for every
        other."""
        return 0 if node.repair_of else self.qa_reserve

    def _refuse_if_held(self, run_id: str, keep: int = 0) -> None:
        """`BudgetExhausted` when the spend and the other runs' open holds leave
        nothing for a new run beyond the reserve it must keep (an unlocked read:
        `_hold_arguments` decides)."""
        if self.dispatch_budget is None:
            return
        try:
            left = BudgetReservations(self.root).left(
                run_id, ceiling=self.dispatch_budget, spent=self._spent_by_run)
        except ReservationError as exc:
            raise BudgetExhausted(
                f"the shared dispatch budget cannot be reserved for {run_id}: {exc}"
            ) from exc
        if left <= 0:
            raise BudgetExhausted(
                f"the shared dispatch budget cannot be reserved for {run_id}: "
                f"nothing is left of {self.dispatch_budget} (spent or held by the "
                f"runs under {self.root})")
        if left <= keep:
            raise BudgetExhausted(
                f"the shared dispatch budget cannot be reserved for {run_id}: "
                f"{left} of {self.dispatch_budget} is left and all of it is the QA "
                f"reserve of {keep}, which only a repair node may use")

    def _hold_arguments(self, run_id: str, keep: int = 0, *, resumed: bool = False) -> list[str]:
        """Holds this run's share of the shared budget and returns the ceiling it
        is started with, as CLI arguments.

        `budget_argumente` reads what is left and a second start reads the same
        number; this takes the share under a lock and writes it down first, so
        the second start is granted what the first did not. With no budget
        there is nothing to hold and no ceiling is passed.

        `resumed` (P3-INV1): the run exists and has dispatched before. A hold's amount is
        compared by the ledger with the run's TOTAL `usage.dispatches`, so the run's own
        spend is not subtracted from what it is granted: it holds what the OTHER runs leave
        (their spend and the unspent part of their holds), its own past dispatches included,
        and its `hoh run` is told that as its ceiling. A run whose past spend already reaches
        that has nothing left to dispatch: the hold is ended and the start is refused.

        Raises `BudgetExhausted` when nothing can be held -- spent, held by
        other runs, or a ledger that cannot be read.
        """
        if self.dispatch_budget is None:
            return []
        own: dict[str, int] = {}

        def spent() -> dict[str, int]:
            every = self._spent_by_run()
            if not resumed:
                return every
            own["spent"] = every.get(run_id, 0)
            return {key: n for key, n in every.items() if key != run_id}

        try:
            hold = BudgetReservations(self.root).reserve(
                run_id, ceiling=self.dispatch_budget, spent=spent,
                want=self.dispatch_reservation, keep=keep)
        except ReservationError as exc:
            if getattr(exc, "reserve_kept", False) is True:
                raise BudgetExhausted(
                    f"the shared dispatch budget cannot be reserved for {run_id}: what is "
                    f"left is the QA reserve of {keep}, which only a repair node may use "
                    f"({exc})") from exc
            raise BudgetExhausted(
                f"the shared dispatch budget cannot be reserved for {run_id}: {exc}"
            ) from exc
        if resumed and hold.amount <= own["spent"]:
            self._settle(run_id, "nothing is left for a run that has dispatched before")
            raise BudgetExhausted(
                f"the shared dispatch budget cannot be reserved for {run_id}: the other runs "
                f"leave it {hold.amount} dispatches in all and it has dispatched {own['spent']}")
        self._prepared_holds[run_id] = hold.reservation_id
        self._ceilings[run_id] = hold.amount
        return ["--max-dispatches", str(hold.amount)]

    def spent_by_run(self) -> dict[str, int]:
        """What each run under this root has dispatched (the `spent` of a reservation)."""
        return self._spent_by_run()

    def take_hold(self, node: TaskNode) -> bool:
        """Makes sure the node's run holds a share of the shared budget before a
        wave admits it. A run `prepare` just started took one (`_hold_arguments`);
        a run that already existed took none, and a wave must not admit it
        without one: N concurrent resumes would each spend against a budget
        nothing was held from. True when this call took a hold, False when the
        run already holds the one this launcher took or there is no budget.

        Raises `BudgetExhausted` when nothing is left to hold, when the ledger
        cannot be read, or when the run holds a share this launcher did not
        take (another process's hold is not adopted or superseded here: no state
        of the run is read to decide, nothing depends on a proof of idleness --
        the sequential round's `launch` ends a hold a launcher that is gone left,
        by the run's lease, before it asks: `_end_stale_hold`)."""
        run_id = node.run_id or node.id
        if self.dispatch_budget is None or self.dry_run:
            return False
        try:
            open_ = [r.reservation_id for r in BudgetReservations(self.root).held()
                     if r.run_id == run_id]
        except ReservationError as exc:
            raise BudgetExhausted(
                f"the shared dispatch budget cannot be read for {run_id}: {exc}") from exc
        if open_:
            if open_ == [self._prepared_holds.get(run_id)]:
                return False
            raise BudgetExhausted(
                f"run {run_id} already holds a share of the shared dispatch budget that "
                f"this launcher did not take: it is not superseded by a wave")
        self._hold_arguments(run_id, self._kept(node),
                             resumed=RunStore(self.root, run_id).state_exists())
        return True

    def prepared_hold(self, node: TaskNode) -> str | None:
        """The id of the hold this launcher took for the node's run, or None. It
        is what lets a caller tell a hold taken **in this round** from one the
        launcher kept from an earlier launch (an IN_FLIGHT run's: `launch` keeps
        it and `_prepared_holds` keeps the id)."""
        return self._prepared_holds.get(node.run_id or node.id)

    def release_prepared_hold(self, node: TaskNode, reason: str) -> bool:
        """Ends the hold that `prepare` took for this node's run **and no other**:
        a node a wave refused after its `prepare` would otherwise keep a share of
        the budget for a dispatch that did not happen. A hold of the same run id
        that this launcher did not take (another process's, an earlier launch's)
        is left alone. True when a hold was ended."""
        run_id = node.run_id or node.id
        mine = self._prepared_holds.pop(run_id, None)
        if mine is None or self.dispatch_budget is None or self.dry_run:
            return False
        try:
            open_ = [r.reservation_id for r in BudgetReservations(self.root).held()
                     if r.run_id == run_id]
        except ReservationError:
            return False
        if open_ != [mine]:
            return False
        self._settle(run_id, reason)
        return True

    # -- the wave executor's view of a node ---------------------------------- #

    def worktree_of(self, node: TaskNode) -> Path | None:
        """The worktree the node's run writes in: `repo_path` of the run's own
        state -- the path its controller leases -- or None when the run has no
        readable state (nothing was prepared)."""
        try:
            return Path(RunStore(self.root, node.run_id or node.id).read_state().repo_path)
        except Exception:                                 # noqa: BLE001 - "unknown" is None
            return None

    def lease_for(self, node: TaskNode) -> DelegatedLease:
        """The observed lease of a prepared node (see `DelegatedLease`); raises
        `WorkspaceLeaseError` when its worktree is not known."""
        where = self.worktree_of(node)
        if where is None:
            raise WorkspaceLeaseError(
                "argument", f"node {node.id} has no readable run state, so no worktree to observe")
        return DelegatedLease(where, holder_name(node.run_id or node.id))

    def _settle(self, run_id: str, reason: str) -> None:
        """Ends this run's hold. A hold that cannot be ended stays open (the
        budget stays smaller, never larger) and is noted, not raised: the run
        it belongs to has already happened and its verdict stands."""
        if self.dispatch_budget is None or self.dry_run:
            return
        try:
            BudgetReservations(self.root).settle(run_id, reason)
        except ReservationError as exc:
            self.reservation_notes.append(
                f"the hold of {run_id} stays open: {exc}"[:300])
        else:
            self._ceilings.pop(run_id, None)           # P3-INV1: the hold is over, so is its ceiling

    def _ensure_runnable(self, run_id: str, branch_: str, store: "RunStore") -> str | None:
        """Clears what an earlier attempt left in the way, or says what remains.

        Only two things are cleared, and both are operator actions rather than
        HoH's: granting the worktree trust, and lifting a run block that was
        *caused by* that missing trust. A block for any other reason is
        reported, not cleared -- unblocking a run whose block nobody
        understands is how a controller talks itself past a real problem.
        """
        try:
            state = store.read_state()
        except StoreError as exc:
            return f"run {run_id} has an unreadable state: {exc}"
        # Before any approval or unblock: a capability violation is lifted by
        # a person who has looked, never by this launcher -- whatever else the
        # block reason happens to mention.
        if violation_unresolved(state):
            reason = (state.blocked_reason or state.stop_reason or "").splitlines()
            return (f"run {run_id} is blocked by a capability violation; only a "
                    f"person lifts that (hoh unblock): "
                    f"{reason[0][:200] if reason else 'no reason recorded'}")
        if state.condition is not Condition.BLOCKED:
            return None

        reason = (state.blocked_reason or state.stop_reason or "")
        if not any(k in reason.lower() for k in APPROVAL_WAITING):
            return (
                f"run {run_id} is blocked for a reason this launcher does not "
                f"clear: {reason.splitlines()[0][:160] if reason else 'no reason recorded'}"
            )

        worktree = self._worktree_for(branch_)
        release_ = self._approve(worktree)
        if not release_.granted:
            return f"run {run_id} waits on a trust approval -- {release_.as_reason()}"

        ub = self._run_cli([
            *hoh_command(), "--root", str(self.root), "unblock", run_id,
        ])
        if ub.exit_code != 0:
            return f"trust granted but run {run_id} stayed blocked: {(ub.stderr or ub.stdout)[-160:]}"
        return None

    def _approve(self, worktree: Path) -> Approval:
        """Asks the configured authority, and records what it answered."""
        release_ = self.approvals.approve(worktree, self.repo_path)
        self.approvals_given.append(release_)
        return release_

    def _worktree_for(self, branch_: str) -> Path:
        """Where Herdr puts a worktree for this repository and branch."""
        return Path.home() / ".herdr" / "worktrees" / self.repo_path.name / branch_

    @staticmethod
    def _worktree_path(stdout: str) -> Path | None:
        try:
            data_ = json.loads(stdout)
        except Exception:
            return None

        def search_(obj):
            if isinstance(obj, dict):
                p = obj.get("path")
                if isinstance(p, str) and "worktree" in p:
                    return p
                for v in obj.values():
                    found = search_(v)
                    if found:
                        return found
            return None

        p = search_(data_)
        return Path(p) if p else None

    @staticmethod
    def _repair_spec(node: TaskNode) -> str:
        return (
            f"# Repair: {node.id}\n\n"
            f"A global gate failed on the merged state after every planned run "
            f"had been accepted. The individual runs were not wrong -- each was "
            f"checked against its own base and its own scope. What failed is the "
            f"combination.\n\n"
            f"## What failed\n\n```\n{node.note or '(no detail recorded)'}\n```\n\n"
            f"## What this run must do\n\n"
            f"Make that gate pass on the merged state, without changing what the "
            f"gate measures. Weakening the check instead of fixing the state would "
            f"satisfy this specification and defeat its purpose.\n\n"
            f"## Acceptance criteria\n\n"
            f"1. The command the gate runs exits 0.\n"
            f"2. The existing test suite still passes.\n\n"
            f"No cause is stated above on purpose: diagnosing it is this run's "
            f"work, and a specification that guesses hands the planner a "
            f"conclusion instead of a problem.\n"
        )

    def accepted_baseline(self, node: TaskNode) -> str | None:
        """The candidate id the run had already accepted before a dispatch."""
        store = RunStore(self.root, node.run_id or node.id)
        if not store.state_exists():
            return None
        try:
            k = store.read_state().last_accepted_candidate
        except StoreError:
            return None
        return k.candidate_id if k else None

    def evaluate(self, node: TaskNode) -> RunOutcome:
        """The verdict of an already-dispatched run, read rather than re-run.

        A verdict that the orchestrator acts on without a launch ends the
        node's hold on the shared budget (P3-04), because this is the last call
        that sees the run: an acceptance, and -- for a node a dead session left
        RUNNING -- any verdict but IN_FLIGHT (still executing), NOT_STARTED (it
        becomes work again, and its launch ends the hold) and UNDETERMINED (a
        person decides; a run that may still dispatch keeps what it holds). A
        READY node's evaluation is followed by its launch, which ends the hold.
        """
        outcome = self._evaluate(node)
        verdict = getattr(outcome, "verdict", None)
        if isinstance(verdict, RunVerdict) and (
                verdict is RunVerdict.ACCEPTED
                or (node.lifecycle is Lifecycle.RUNNING
                    and verdict not in (RunVerdict.IN_FLIGHT, RunVerdict.NOT_STARTED,
                                        RunVerdict.UNDETERMINED))):
            self._settle(node.run_id or node.id, f"the run was read as {verdict.value}")
        return outcome

    def _evaluate(self, node: TaskNode) -> RunOutcome:
        """The verdict of an already-dispatched run, read rather than re-run.

        `node.accepted_before` is the baseline the dispatching session wrote
        into project state before it launched. Comparing against it is what
        makes a resume exactly-once: without it, a CHECKPOINTED run looks the
        same whether this dispatch accepted something or something was accepted
        three iterations earlier.
        """
        if self.dry_run:
            return RunOutcome(RunVerdict.NOT_RUN, "dry run: nothing to evaluate")
        store = RunStore(self.root, node.run_id or node.id)
        if not store.state_exists():
            return RunOutcome(
                RunVerdict.UNDETERMINED,
                f"node {node.id} is marked RUNNING but no run state exists -- the "
                "dispatch may never have started",
            )
        try:
            state = store.read_state()
        except StoreError as exc:
            return RunOutcome(RunVerdict.UNDETERMINED, f"run state unreadable: {exc}")
        # A run still being executed by a live process is in flight -- not
        # crashed, and not something to take a verdict from. Two witnesses:
        # the process marker `hoh run` keeps for its whole lifetime (the run
        # lock alone is free between iterations, review B3 C), and the lock
        # itself for a run started by a version without the marker. Only a
        # busy lock counts; any other error probing it is UNDETERMINED, since
        # an unreadable lock file is not a live controller (review A3, F2).
        live = store.running_process()
        if live is not None:
            return RunOutcome(
                RunVerdict.IN_FLIGHT,
                f"run {node.run_id or node.id} is still executed by pid {live.get('pid')} "
                f"(since {live.get('since')})")
        try:
            with store.lock():
                pass
        except LockBusy as exc:
            return RunOutcome(RunVerdict.IN_FLIGHT, str(exc)[:200])
        except Exception as exc:                          # noqa: BLE001 - classified
            return RunOutcome(RunVerdict.UNDETERMINED,
                              f"the run lock could not be probed: {exc}"[:200])
        # P3-18 slice 3: the run's lease is the third witness, and the only one that sees a driver in another
        # host or pid namespace that shares this root (its process lock is not ours to probe). A LIVE lease is a
        # run that is alive; a record that cannot be read is not a free run; a lease whose holder is gone (LOST)
        # is read, not touched -- the next `hoh run` takes it over -- and the verdict it is read under says so.
        from . import rundrive

        fact = rundrive.consult(self.root, node.run_id or node.id)
        if fact.state == "LIVE":
            return RunOutcome(RunVerdict.IN_FLIGHT,
                              f"run {node.run_id or node.id} holds a live run lease: {fact.why}"[:300])
        if fact.state == "UNREADABLE":
            return RunOutcome(RunVerdict.UNDETERMINED,
                              f"the run lease of {node.run_id or node.id} cannot be read: {fact.why}"[:300])
        outcome = self._verdict_from_baseline(
            state, node.accepted_before,
            open_seals=bool(open_seal_arenas(store.dir)))
        if fact.state == "LOST":
            outcome.detail = (f"{outcome.detail} | " if outcome.detail else "") + (
                f"run lease LOST ({fact.why}) -- reclaimable by the next 'hoh run'")[:300]
        return outcome

    def launch(self, node: TaskNode) -> RunOutcome:
        """Dispatches the node's run and returns its verdict.

        Whatever the dispatch does -- returns, is refused, times out -- the
        node's hold on the shared budget ends with it (P3-04): from here the
        run's dispatches are in its own state and nothing is held for it.
        """
        # P3-INV1: a run that exists and holds nothing takes its hold now (a wave took it at
        # admission, `take_hold`; the sequential round has not). Before the `try`: a refusal
        # started nothing, so the `finally` below must not end a hold that is not this
        # launch's.
        refused = self._hold_for_launch(node)
        if refused is not None:
            return refused
        outcome = None
        try:
            outcome = self._launch(node)
            return outcome
        finally:
            # A run that is still executed by another process (IN_FLIGHT) can
            # still dispatch: its hold stays until a later launch or the
            # evaluation of its acceptance sees it finished.
            if getattr(outcome, "verdict", None) is not RunVerdict.IN_FLIGHT:
                self._settle(node.run_id or node.id, "the dispatch returned")

    def _hold_for_launch(self, node: TaskNode) -> RunOutcome | None:
        """P3-INV1: why a budgeted launch of an existing run starts nothing, or None.

        A run `prepare` just started took its hold there; a run a wave admitted took it in
        `take_hold`; a run that existed in the sequential round has none, and its `hoh run`
        would dispatch up to its own, older ceiling whatever the other runs have spent
        meanwhile. It takes the hold here (`take_hold`, which counts the run's earlier
        dispatches as part of what it holds), and `_launch` types the hold's amount as
        `--dispatch-ceiling`. A budget with nothing left for it is the run's verdict
        (`BUDGET_EXHAUSTED`), not an exception out of `launch`. No budget, a dry run and a
        node with no run behind it hold nothing."""
        if self.dispatch_budget is None or self.dry_run:
            return None
        if not RunStore(self.root, node.run_id or node.id).state_exists():
            return None
        try:
            self._end_stale_hold(node.run_id or node.id)
            self.take_hold(node)
        except BudgetExhausted as empty:
            return RunOutcome(RunVerdict.BUDGET_EXHAUSTED, str(empty)[:300])
        return None

    def _end_stale_hold(self, run_id: str) -> None:
        """P3-INV1 (review B1): ends the open hold of `run_id` that this launcher did not take, when the run's
        lease says nobody drives the run. Such a hold is what a launcher that died left behind (between `prepare`
        and the end of its `launch`, after `take_hold`, an IN_FLIGHT hold of a process that is gone); on the base
        the `finally` of every `launch` ended it, and without this the node would halt for good, for no budget
        change clears it. The lease is the witness (`rundrive.consult`, read-only): NONE, LOST and RELEASED mean
        nobody drives the run, and the hold is settled **by its id** (the row stays, SETTLED, with the reason);
        LIVE keeps it (nothing is taken from a run that is being driven) and so does UNREADABLE (a run that cannot
        be shown to be free is not free): `BudgetExhausted` naming the lease. A hold this launcher took is not
        stale. No hold, nothing to do. A ledger that cannot be read is left to `take_hold`, which refuses it."""
        ledger = BudgetReservations(self.root)
        try:
            open_ = [r.reservation_id for r in ledger.held() if r.run_id == run_id]
        except ReservationError:
            return
        if not open_ or open_ == [self._prepared_holds.get(run_id)]:
            return
        from . import rundrive

        fact = rundrive.consult(self.root, run_id)
        if fact.state not in ("NONE", "LOST", "RELEASED"):
            raise BudgetExhausted(
                f"run {run_id} already holds a share of the shared dispatch budget that this launcher "
                f"did not take, and its run lease is {fact.state} ({fact.why}): it is not taken over")
        try:
            ledger.settle_holds(open_, f"the launcher that took it is gone (the run lease is {fact.state})")
        except ReservationError as exc:
            raise BudgetExhausted(
                f"the stale hold of {run_id} cannot be ended: {exc}") from exc

    def _launch(self, node: TaskNode) -> RunOutcome:
        if self.dry_run:
            return RunOutcome(RunVerdict.NOT_RUN, "dry run: nothing dispatched")

        run_id = node.run_id or node.id
        store = RunStore(self.root, run_id)

        # The baseline the controller recorded before this dispatch. Falling
        # back to reading it here covers a launcher used without a controller.
        before = node.accepted_before or self.accepted_baseline(node)

        # P3-20 slice 3: a policy that cannot be loaded, or is not the one bound, starts nothing.
        routing_policy, unrouted = self._routing_now()
        if unrouted is not None:
            return RunOutcome(RunVerdict.UNDETERMINED, unrouted)
        # P3-11 slice 2: a requirement that could not be bound starts nothing, whoever calls
        # `launch` (a caller that never `prepare`d included).
        if self._preflight_bind_failed is not None:
            return RunOutcome(RunVerdict.UNDETERMINED,
                              f"{self._preflight_bind_failed}; nothing is started under it")
        argv = [
            *hoh_command(), "--root", str(self.root), "run", run_id,
            "--iterations", str(self.iterations),
            *self._role_options(routing_policy),
            "--isolation", self.isolation,
        ]
        if routing_policy is not None:
            argv += ["--routing-policy", str(self.routing_policy)]
        argv += self._preflight_options()
        if self.dispatch_budget is not None:
            # P3-INV1: the run is stopped where its hold ends. No ceiling to type is a launch
            # nothing held for, and it starts nothing.
            if (ceiling := self._ceilings.get(run_id)) is None:
                return RunOutcome(RunVerdict.UNDETERMINED,
                                  f"run {run_id} holds no share of the shared dispatch budget; "
                                  "nothing is started under it")
            argv += ["--dispatch-ceiling", str(ceiling)]
        if self.master_session is not None and self.master_epoch is not None:
            argv += ["--master-session", str(self.master_session),
                     "--master-epoch", str(self.master_epoch)]
        if self.autonomy_file is not None:
            argv += ["--autonomy", str(self.autonomy_file)]
        for role_name, handle in sorted(self.role_accounts.items()):
            argv += ["--role-account", f"{role_name}={handle}"]
        if self.storage_root is not None:
            argv += ["--storage-root", str(self.storage_root)]
            with self._boot_lock:
                refused = self._claim_storage_boot()
            if refused is not None:
                return RunOutcome(RunVerdict.STORAGE_UNAVAILABLE, refused)
        d = self._run_cli(argv)
        if d.exit_code == 2 and (d.stderr or "").lstrip().startswith(
                f"Run {run_id} is already being executed"):
            # Another process executes this run (its process lock refused ours):
            # the run is in flight, not rejected by this dispatch (review A5, M1).
            return RunOutcome(RunVerdict.IN_FLIGHT, d.stderr.strip()[:200])
        if d.exit_code != 0:
            if d.exit_code == 2 and (unfit := preflight_refusal(d.stderr)) is not None:
                # `hoh run` refused to start for the environment (the report is not READY,
                # is stale or unreadable): nothing ran. A terminal, named halt for a person:
                # never a rejection of the work and never a retry.
                return RunOutcome(RunVerdict.UNDETERMINED, unfit)
            if d.exit_code == 2 and (leased := lease_refusal(d.stderr)) is not None:
                # P3-18 slice 3. `hoh run` refused to drive the run for its lease (a record that cannot
                # be read exactly is not a free run): nothing ran, and a person looks at the record.
                # Never a rejection of the work and never a retry.
                return RunOutcome(RunVerdict.UNDETERMINED, leased)
            refused = routing_refusal(d.stderr) if d.exit_code == 2 else None
            if refused is not None:
                # `hoh run` refused the policy (the run is pinned to another one, its pin is
                # damaged): nothing ran, and a person decides. Never a rejection.
                return RunOutcome(RunVerdict.UNDETERMINED, refused)
            said = storage_refusal(d.stderr)
            if said is not None:
                # `hoh run` refused to start for its storage configuration:
                # nothing ran, and a person has to fix the configuration.
                return RunOutcome(RunVerdict.STORAGE_UNAVAILABLE, said)
            if d.exit_code == 2 and (unpinned := autonomy_refusal(d.stderr)) is not None:
                # `hoh run` refused the autonomy contract (the run is pinned to another one, its pin
                # is damaged): nothing ran, and a person decides. Never a rejection.
                return RunOutcome(RunVerdict.UNDETERMINED, unpinned)

        try:
            state = store.read_state()
        except StoreError as exc:
            return RunOutcome(
                RunVerdict.UNDETERMINED,
                f"run {run_id} left no readable state (cli exit {d.exit_code}): {exc}",
            )

        return self._verdict(state, before, d,
                             open_seals=bool(open_seal_arenas(store.dir)))

    def _claim_storage_boot(self) -> str | None:
        """Claim the boot on the storage ledger before spawning (Revision 9
        item 7: every host entry point that starts runs claims first). Returns
        a refusal text, or None when claimed. An uninitialised root is left
        for the child to refuse, in its own words."""
        root = self.storage_root
        if root is None or not (root / "OWNER.json").is_file():
            return None
        try:
            if self._storage_admission_factory is not None:
                admission = self._storage_admission_factory(root)
            else:
                from .storage_pressure import StorageAdmission, StoragePolicy

                policy_file = root / "storage-policy.json"
                policy = (StoragePolicy.load(policy_file) if policy_file.is_file()
                          else StoragePolicy())
                admission = StorageAdmission.for_state_root(root, policy)
            admission.claim_boot()
        except Exception as exc:                          # noqa: BLE001 - classified
            return f"storage admission unavailable: the launcher cannot claim the boot: {exc}"
        return None

    def _open_seals_of(self, state) -> bool:
        """Does this run's own seal ledger leave a candidate arena sealed?

        For a caller that did not say (`open_seals=None`). A missing ledger is
        "no". An unreadable ledger, or a run id that names no directory of the
        root, is "yes": a gate that cannot tell holds.
        """
        try:
            run_dir = RunStore(self.root, state.run_id).dir
        except Exception:                                 # noqa: BLE001 - fail closed
            return True
        return bool(open_seal_arenas(run_dir))

    def _ceiling_reached(self, state) -> str | None:
        """P3-INV1: why the run stopped at the ceiling this launcher told it (`--dispatch-ceiling`), or
        None. The run's own `budget_exhausted` does not know the told ceiling: a run stopped there is
        ACTIVE in its stage, and read as REJECTED it would be retried until it is abandoned."""
        from .controller import DISPATCH_CEILING_REACHED

        ceiling = self._ceilings.get(state.run_id)
        if ceiling is None or state.usage.dispatches < ceiling:
            return None
        return f"{DISPATCH_CEILING_REACHED} ({state.usage.dispatches}/{ceiling})"

    def _verdict_from_baseline(self, state, previous_id: str | None, *,
                               open_seals: bool | None = None) -> RunOutcome:
        return self._verdict(state, previous_id, Dispatch(0, "", ""),
                             open_seals=open_seals)

    def _verdict(self, state, before, d: Dispatch, *,
                 open_seals: bool | None = None) -> RunOutcome:
        # `vorher` is a candidate id (from project state) or a Candidate (from a
        # direct read). Normalised here so both callers share one comparison.
        previous_id = getattr(before, "candidate_id", before)
        after = state.last_accepted_candidate

        # Ahead of the rule that an acceptance is durable: a run held by a
        # capability violation has evidence that was interfered with, so
        # nothing it accepted is merged until a person has looked. UNDETERMINED
        # halts the node for that person.
        if violation_unresolved(state):
            return RunOutcome(
                RunVerdict.UNDETERMINED,
                f"run blocked by a capability violation; nothing it accepted is "
                f"merged until a person has looked: "
                f"{state.blocked_reason or state.stop_reason or ''}"[:400],
            )

        # A candidate arena still sealed for QA has not been compared at the
        # release: whether it was touched is not known yet, so nothing is
        # merged until a `hoh run` has closed the tabs and released it. The
        # flag is read from the run's own ledger when the caller did not say.
        if open_seals is None:
            open_seals = self._open_seals_of(state)
        if open_seals:
            return RunOutcome(
                RunVerdict.UNDETERMINED,
                "a candidate arena sealed for QA is still open; nothing is "
                "merged until a later 'hoh run' has closed the tabs and "
                "released it (compared first)",
            )

        # STORAGE-B3, checked before the generic fallbacks. A recorded
        # acceptance still wins (it is durable, below); otherwise a storage
        # park is a wait of its own class and a storage refusal needs a
        # person. Neither ever reads as a provider wait or as UNDETERMINED.
        new_acceptance = after is not None and (
            previous_id is None or after.candidate_id != previous_id)
        if not new_acceptance:
            if state.condition is Condition.BLOCKED \
                    and (state.blocked_kind or "") == "storage_pressure":
                return RunOutcome(
                    RunVerdict.STORAGE_WAIT,
                    f"parked on storage pressure: {state.blocked_reason or ''}"[:300],
                    retry_after=_timer_form(state.retry_after),
                )
            said = storage_refusal(state.stop_reason) if state.condition in (
                Condition.PAUSED, Condition.BLOCKED) else None
            if said is not None:
                return RunOutcome(RunVerdict.STORAGE_UNAVAILABLE, said)

        # Handled first, and deliberately: an acceptance that a run recorded is
        # a fact about that run, and nothing that happens afterwards -- a
        # block, a spent budget, another iteration -- un-records it.
        if state.condition is Condition.BLOCKED and after is None:
            art = (state.blocked_kind or "").lower()
            reason_text = (state.blocked_reason or state.stop_reason or "").lower()
            # P3-INV1: the process was stopped at the ceiling of its hold: the shared budget is
            # what ended it, a decision for a person, never "blocked, unclassified".
            from .controller import DISPATCH_CEILING_REACHED

            if DISPATCH_CEILING_REACHED.lower() in reason_text:
                return RunOutcome(RunVerdict.BUDGET_EXHAUSTED,
                                  f"stopped at the ceiling of its hold: {state.blocked_reason}"[:300])
            # A trust dialog is not an unclassifiable state. HoH deliberately
            # never answers one itself -- granting trust to a directory it was
            # merely pointed at is exactly the capability it refuses to take --
            # so the run stops and waits. Naming that separately is the
            # difference between "answer the prompt" and "go find the defect".
            if any(k in reason_text for k in APPROVAL_WAITING):
                return RunOutcome(
                    RunVerdict.NEEDS_APPROVAL,
                    (state.blocked_reason or state.stop_reason or "awaiting approval")
                    .splitlines()[0][:200],
                )
            if any(k in art for k in PROVIDER_BLOCKED):
                return RunOutcome(
                    RunVerdict.PROVIDER_UNAVAILABLE,
                    f"blocked on the provider: {state.blocked_reason or art}",
                    retry_after=_timer_form(state.retry_after),
                )
            # Blocked with nothing accepted is not a rejection either -- it is
            # a state nobody has classified, and guessing is what this whole
            # design exists to avoid. A block *after* an acceptance is handled
            # above, by the rule that an acceptance is durable.
            return RunOutcome(
                RunVerdict.UNDETERMINED,
                f"run blocked ({state.blocked_kind}): {state.blocked_reason}",
            )

        if state.condition in (Condition.FAILED, Condition.CANCELLED):
            return RunOutcome(RunVerdict.UNDETERMINED,
                              f"run ended {state.condition.value}: {state.stop_reason}")

        if after is not None:
            if previous_id is not None and after.candidate_id == previous_id:
                # The run ended where it started. Merging on this would apply
                # the same candidate a second time.
                return RunOutcome(
                    RunVerdict.REJECTED,
                    f"no new candidate: still {after.candidate_id}",
                    candidate=after.commit, candidate_tree=after.tree_digest,
                )
            # **An acceptance is durable.** It does not depend on the stage the
            # run happened to stop in afterwards. This check used to live
            # inside the `CHECKPOINTED` branch, so a run that accepted a
            # candidate and then kept iterating -- because the budget allowed
            # it -- was reported by the stage it ran out in: `REJECTED` from
            # PLANNING, "iterations spent without a checkpoint". The candidate
            # was in the state the whole time.
            #
            # Measured twice in the benchmark, on two different branches of
            # this function (O122). A verdict that discards a recorded
            # acceptance costs the work, not just the report.
            postscript = ""
            if state.stage not in (Stage.CHECKPOINTED, Stage.READY_FOR_DELIVERY):
                postscript = (
                    f", and the run then continued to "
                    f"{state.stage.value}/{state.condition.value}"
                )
                if state.condition is Condition.BLOCKED:
                    postscript += f": {state.blocked_reason}"
            return RunOutcome(RunVerdict.ACCEPTED,
                              f"accepted {after.candidate_id}{postscript}",
                              candidate=after.commit, candidate_tree=after.tree_digest)

        if state.stage in (Stage.CHECKPOINTED, Stage.READY_FOR_DELIVERY):
            return RunOutcome(RunVerdict.UNDETERMINED,
                              "run checkpointed with no accepted candidate recorded")

        # A run that exists and has not begun. The most knowable state there
        # is, and reporting it as unclassifiable deadlocked an unattended run:
        # a process died between marking the node RUNNING and dispatching it,
        # and every later round re-read the same NEW state and halted on it.
        if state.stage is Stage.NEW and not state.iteration and after is None:
            return RunOutcome(
                RunVerdict.NOT_STARTED,
                "the run exists and has not begun: no iteration, no candidate",
            )

        if state.stage in (Stage.PLANNING, Stage.DEVELOPING, Stage.VERIFYING):
            if state.budget_exhausted() or self._ceiling_reached(state):
                # A budget this project set for itself is not the provider
                # being unavailable. Reporting it as one put a policy ceiling
                # and somebody else's downtime under the same word, and the
                # two want opposite responses: wait, or decide whether the
                # work is worth more budget.
                return RunOutcome(RunVerdict.BUDGET_EXHAUSTED,
                                  f"budget exhausted in {state.stage.value}: "
                                  f"{state.budget_exhausted() or self._ceiling_reached(state)}")
            return RunOutcome(
                RunVerdict.REJECTED,
                f"iterations spent without a checkpoint (stage {state.stage.value})",
            )

        return RunOutcome(RunVerdict.UNDETERMINED,
                          f"stage {state.stage.value}/{state.condition.value} "
                          f"(cli exit {d.exit_code})")

    # -- merge ------------------------------------------------------------- #

    def merge(self, node: TaskNode, outcome: RunOutcome) -> MergeResult:
        """Applies an accepted candidate, idempotently, and reports the failure.

        Nothing here resolves a conflict. What it does is refuse to throw away
        what git already said: the conflicting paths, the head the target was
        at, and the merge base are all recorded, so the halt names a state
        rather than a mystery.

        Classification is by what git reports, and only three shapes are
        claimed. Anything else stays UNKNOWN rather than being squeezed into
        the nearest label -- a misclassified failure is worse than an
        unclassified one, because it sends the reader somewhere specific and
        wrong.
        """
        branch_ = node.branch or f"hoh-{node.run_id or node.id}"
        result = MergeResult(landed=False, branch=branch_, candidate=outcome.candidate or "",
                             accepted_tree=outcome.candidate_tree or "")
        if self.dry_run:
            result.failure = MergeFailure.UNKNOWN
            result.detail = "dry run: no merge attempted"
            return result

        head = self._git("rev-parse", "--verify", "--quiet", branch_ + "^{commit}")
        if head.returncode != 0:
            result.failure = MergeFailure.NO_BRANCH
            result.detail = f"branch {branch_} does not exist in {self.repo_path}"
            return result
        # Resolved once, here, and used by identity from now on: the branch is
        # a mutable name, and everything after this line must be about the one
        # commit that was checked against the acceptance.
        sha = head.stdout.strip()
        problem = accepted_identity_problem(self._git, sha, outcome)
        if problem:
            result.failure = MergeFailure.CANDIDATE_MISMATCH
            result.detail = problem
            return result
        result.integrated = sha

        result.target_head_before = self._git(
            "rev-parse", "--short", self.mainline).stdout.strip()
        result.merge_base = self._git(
            "merge-base", sha, self.mainline).stdout.strip()[:12]

        # Already contained: a resumed session must not merge twice, and the
        # cheapest way to know is to ask whether the accepted commit is an
        # ancestor of the mainline.
        if self._git("merge-base", "--is-ancestor", sha, self.mainline).returncode == 0:
            result.landed = True
            result.detail = "already contained in the mainline"
            return result

        if self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != self.mainline:
            out = self._git("checkout", self.mainline)
            if out.returncode != 0:
                result.failure = MergeFailure.OBSTRUCTED
                result.detail = (out.stderr or out.stdout).strip().splitlines()[:1][0][:200] \
                    if (out.stderr or out.stdout).strip() else "checkout failed"
                return result

        # Taken on the mainline, after any switch to it: the switch alone must
        # not read as something the merge left behind (review A6, 3).
        status_before = self._git("status", "--porcelain",
                                  "--untracked-files=no").stdout.strip()
        p = self._git("merge", "--no-ff", sha, "-m",
                      f"Take accepted candidate {node.id} ({sha}): {outcome.detail}",
                      timeout=self.MERGE_TIMEOUT)
        if p.returncode == 0:
            result.landed = True
            return result

        text = (p.stdout or "") + (p.stderr or "")
        result.conflicting_paths = self._conflicting_paths(text)
        result.detail = self._first_meaningful(text)
        result.failure = self._classify(text)
        # Leave no half-merge behind for the next session to puzzle over --
        # where git lets us. `merge --abort` needs MERGE_HEAD; a git killed by
        # the timeout (inside a hook, say) may have staged the candidate
        # without writing it, and then nothing here may guess at a cleanup of
        # the integration checkout. That is said, not hidden (review A4, G3).
        merge_head = self._git("rev-parse", "-q", "--verify", "MERGE_HEAD")
        if merge_head.returncode == 0:
            self._git("merge", "--abort", timeout=self.MERGE_TIMEOUT)
        leftover = self._git("status", "--porcelain", "--untracked-files=no").stdout.strip()
        # Only what the merge itself left: changes that were there before it
        # (an operator's edit in the way) keep their own classification
        # (review A5, M2).
        if leftover and leftover != status_before:
            result.failure = MergeFailure.UNKNOWN
            result.detail = (f"{result.detail or 'merge failed'}; the mainline checkout "
                             f"is left with changes git did not undo ("
                             f"{', '.join(leftover.splitlines()[:4])}) -- a person must "
                             f"inspect it before any further merge")
        return result

    @staticmethod
    def _classify(text: str) -> MergeFailure:
        """What git said, mapped to a shape -- or UNKNOWN.

        The obstruction check comes first: git prints "would be overwritten by
        merge" for a dirty or occupied working tree *before* it attempts any
        content merge, so that message never accompanies a real conflict.
        """
        further_down = text.lower()
        if ("would be overwritten by merge" in further_down
                or "your local changes to the following files" in further_down
                or "please commit your changes or stash them" in further_down):
            return MergeFailure.OBSTRUCTED
        if "conflict (" in further_down or "automatic merge failed" in further_down:
            return MergeFailure.CONFLICT
        return MergeFailure.UNKNOWN

    @staticmethod
    def _conflicting_paths(text: str) -> tuple[str, ...]:
        paths: list[str] = []
        for line in text.splitlines():
            s = line.strip()
            m = re.match(r"CONFLICT \([^)]*\): (.+?) (deleted in|added in|merge conflict)", s)
            if m:
                paths.append(m.group(1))
                continue
            if s and not s.startswith(("error:", "CONFLICT", "Auto", "Please", "Aborting",
                                       "Merge", "warning:", "hint:")) and "/" in s or (
                    s.endswith((".py", ".txt", ".md", ".json", ".pyc"))):
                if s not in paths and len(s) < 200 and " " not in s:
                    paths.append(s)
        return tuple(dict.fromkeys(paths))

    @staticmethod
    def _first_meaningful(text: str) -> str:
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith(("hint:", "warning:")):
                return s[:200]
        return "git produced no output"
