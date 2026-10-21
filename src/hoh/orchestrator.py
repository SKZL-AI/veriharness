"""The control plane: the loop that decides which runs happen at all.

`controller.py` drives one run -- plan, develop, verify, accept or reject.
This module drives the layer above it: which runs exist, in what order, what
the global gates said about the merged result, and what to do when they say
no. That layer produced this project's own release, and until now it existed
only as an agent session's behaviour plus a prototype script outside the
package. `ProjectState`/`ProjectStore` made the state durable; this makes the
*executor* part of the product.

Three properties are deliberate, and each of them is a lesson rather than a
preference:

**Every halt has a name.** A loop that stops without saying why is
indistinguishable, from outside, from a loop that broke. The prototype's
halt classes are carried over intact (`HaltClass` below), because the count
of `AMBIGUOUS` halts over a campaign is the only honest measure of how far
the automation actually reaches.

**A round persists before it acts.** Lifecycle transitions are written
through `ProjectStore` under its lock and fencing before the next decision is
taken, so a crash between two rounds loses at most the round in flight, and
a fresh session reads the same state this one would have.

**Closure is a fixpoint, not the end of a list.** `DAG_TERMINAL` is not
`RC_CLOSED`. A global gate failure does not stop the release and is not
patched by hand: it becomes a repair node that goes through the ordinary
loop, after which closure is attempted again. Only a pass that finds every
gate green *and* creates no new repair node closes.

The execution side is behind two narrow protocols (`RunLauncher`,
`GateRunner`) so the same controller drives a real Herdr/HoH run and a
synthetic fixture. That is what makes the parity suite against the prototype
possible, and what keeps the fault-injection tests free of agent quota.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from . import backpressure, budgetconfig, waverecovery
from .broker import Policy
from .frontier import SEMANTIC_DEPENDENCY, FrontierPlan, ReadyFrontierPlanner
from .project import (
    ActionClass,
    DecisionKind,
    DecisionRecord,
    GateOutcome,
    GateResult,
    Lifecycle,
    ProjectState,
    TaskNode,
)
from .programlogic import EventKind
from .projectactivity import (
    ActivityLogError, abandon as abandon_dispatch, close as close_dispatch, in_flight_disagreements,
    outcome_digest, start as start_dispatch,
)
from .projectstore import ProjectStore
from .reservation import BudgetReservations, CapacityReservations
from .store import LockBusy, StaleWrite, StoreError
from .succession import MasterAuthority, StaleMaster
from .timers import TimerKind, TimerStore, TimerStoreError, check_time
from .wave import (
    MAX_WAVE_WIDTH, NodeState, ParallelismDegradationReason, ParallelWaveExecutor, WaveBudget,
    WaveError, WaveNotSettled, settle_wave,
)

#: What a launcher has to offer for the project loop to launch a wave through it
#: (`HohRunLauncher` does): the worktree of a prepared node as an observed lease,
#: the hold its `prepare` took, and what each run has spent.
WAVE_LAUNCHER_API = ("lease_for", "release_prepared_hold", "spent_by_run", "take_hold", "prepared_hold")

#: The node-id grammar of the wave record (`wave.py`): a node whose id is outside it runs
#: in the one-node-per-round loop.
_WAVE_NODE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

#: The environment variable an operator sets to widen the loop without editing
#: the project's driver: a whole number from 1 to `MAX_WAVE_WIDTH`.
WAVE_WIDTH_ENV = "HOH_WAVE_WIDTH"

#: P3-30 slice 2. A project registers the autonomy contract it continues under the way it registers its
#: driver: a file in the project directory (the contract file `hoh run --autonomy` takes). The launcher
#: passes it to every run it starts, so the runs are pinned to it; the loop reads it to continue beside a
#: parked branch. Absent: the loop is what it was.
AUTONOMY_FILE = "autonomy-contract.json"

#: Continuation passes one drive makes beside the branches that wait. A pass is one decision
#: (`governance.continue_by_policy`, recorded before anything is dispatched) and, when it says CONTINUE,
#: one restricted drive of the nodes it names. A drive that wants more than this ends with the halt it has;
#: the next drive decides again.
CONTINUATION_PASSES = 3


@contextmanager
def write_lock(store: ProjectStore, *, attempts: int = 50,
               pause: float = 0.1) -> Iterator[None]:
    """The program lock for one short critical section, waiting briefly.

    Non-blocking attempts with a bounded wait rather than a blocking flock: a
    holder that is frozen inside its own critical section must cost the
    waiter a clear `LockBusy`, not an indefinite hang.
    """
    with ExitStack() as stack:
        for i in range(attempts):
            try:
                stack.enter_context(store.lock())
                break
            except LockBusy:
                # Only the *acquisition* is retried. An exception from the body
                # is never mistaken for a busy lock and never re-runs it.
                if i == attempts - 1:
                    raise
                time.sleep(pause)
        yield


def disarm_stale_waits(directory, state: ProjectState) -> list:
    """Withdraw every armed timer of a node that is no longer waiting on one.

    A node that leaves a `usage_limit` park by any route -- a merge
    conflict, a budget stop, a human `unblock` -- must not keep a timer
    that later resumes it as though it were still a provider wait
    (review A, P1 and S5). A timer is left armed only for a node that is
    BLOCKED on `usage_limit` with exactly that retry time.
    """
    timers = TimerStore(directory)
    withdrawn = []
    for t in list(timers.armed()):
        if t.node_id is None:
            continue
        node = state.node(t.node_id)
        if node is None:
            continue
        waiting = (node.lifecycle is Lifecycle.BLOCKED
                   and node.blocked_kind == "usage_limit"
                   and node.retry_after == t.due_at
                   and t.kind is TimerKind.PROVIDER_RETRY)
        # A timer armed for a RUNNING node is the arm-before-park window of
        # this very transition; it becomes a real wait in the same write.
        if not waiting and node.lifecycle is not Lifecycle.RUNNING \
                and t.kind is TimerKind.PROVIDER_RETRY:
            withdrawn.append(timers.disarm(
                t.timer_id, f"node {node.id} is {node.lifecycle.value}"
                            f"/{node.blocked_kind}: no longer this wait"))
    return withdrawn


def node_timer_key(node_id: str) -> str:
    """The `run_id` a project node's timer carries.

    `hoh.resumption` decides per run id; a node is not a run, so its timers
    are keyed `node:<id>` and cannot collide with a real run's.
    """
    return f"node:{node_id}"


class HaltClass(StrEnum):
    """Why the loop stopped. Every exit passes through exactly one of these."""

    #: The only good ending: DAG terminal, every global gate green, no repair
    #: node outstanding from the pass that checked them.
    CLOSED = "CLOSED"
    #: The next node carries an irreversible external action. Correct and
    #: intended -- this is the one class that legitimately needs a human.
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"
    #: Provider quota, rate limit, or outage. Not a rejection: recording it as
    #: one would burn a node's retry budget on someone else's downtime.
    BLOCKED_PROVIDER = "BLOCKED_PROVIDER"
    #: A dependency that names a node which does not exist, or a node that
    #: policy holds. Nothing is runnable and the state says why.
    BLOCKED_DEPENDENCY = "BLOCKED_DEPENDENCY"
    #: The persisted state cannot be read or cannot be trusted.
    CORRUPT_STATE = "CORRUPT_STATE"
    #: Round or repair ceiling reached without a fixpoint. Not an endless spin.
    ROUND_LIMIT = "ROUND_LIMIT"
    #: A node reached the dispatch budget the project set for it. A result,
    #: not a failure of the work -- and the protocol of this project's own
    #: benchmark requires it be recorded as its own outcome rather than folded
    #: into "did not pass".
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    #: A dry run: nothing was executed, so nothing was established. Never a
    #: fixpoint -- the prototype's first version returned 0 from un-run gates
    #: and reported success, which is NOT_RUN counted as passed.
    NOT_RUN = "NOT_RUN"
    #: The candidate was accepted and git refused the merge, saying exactly
    #: why. Kept apart from AMBIGUOUS deliberately: when the tool has named the
    #: conflicting paths, the state is known, and calling it unclassifiable
    #: sends a reader looking for a mystery instead of at a conflict.
    MERGE_CONFLICT = "MERGE_CONFLICT"
    #: The merge could not even be attempted because the working tree was in
    #: the way -- local modifications or untracked files git would overwrite.
    #: Separate from MERGE_CONFLICT because the remedy is different: clear the
    #: tree rather than reconcile content.
    MERGE_OBSTRUCTED = "MERGE_OBSTRUCTED"
    #: This session is no longer the master: its epoch was superseded, its
    #: lease was handed over, released or lost to a successor. Not AMBIGUOUS:
    #: the state is known and healthy, it belongs to somebody else now, and the
    #: correct response is to write nothing (P2-07/08/09, DS6, AC7).
    SUPERSEDED = "SUPERSEDED"
    #: The accepted candidate is not what its branch carries. Known and
    #: classified -- the branch moved, or the verified tree was never committed
    #: -- and nothing lands until a person looks, because landing either the
    #: branch or nothing would be wrong.
    CANDIDATE_MISMATCH = "CANDIDATE_MISMATCH"
    #: A node's run is still executing in another live process (a role run
    #: that survived its master). Nothing is decided and nothing is written:
    #: re-dispatching would duplicate the work, taking a verdict would guess.
    #: The resume loop comes back when the run has finished (finding #9).
    RUN_IN_FLIGHT = "RUN_IN_FLIGHT"
    #: A node's run is parked on storage pressure (STORAGE-B3): admission
    #: refused its next role dispatch. A wait with a durable STORAGE_RECHECK
    #: timer that `hoh resume-storage` consumes -- not a human's decision.
    STORAGE_WAIT = "STORAGE_WAIT"
    #: A wave could not start because capacity is not available or cannot be
    #: known (P3-19 slice 2): the capacity registry or the holds were asked for
    #: the wave's nodes and none may start now. Nothing was prepared or
    #: launched and no node was blocked -- every node stays READY, so the
    #: sentinel has nothing to call stuck -- and driving again asks again. The
    #: reason names why (a limit held, a figure stale, a capacity unrecorded).
    CAPACITY_WAIT = "CAPACITY_WAIT"
    #: Storage admission could not be used at all -- the state root is not
    #: initialised, WSL without an observed host volume, an unwritable ledger,
    #: or a recheck timer that could not be armed. Fail closed: a person is
    #: needed, and the halt names the reason.
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
    #: A state or verdict the controller cannot classify. The defect class:
    #: each one is a place where a human would have to step in.
    AMBIGUOUS = "AMBIGUOUS"


class BudgetExhausted(RuntimeError):
    """A node cannot be started because the shared budget is spent.

    Raised by a launcher's `prepare`, and a **type** rather than a reason
    string because the orchestrator has to tell it apart from every other
    reason a node will not start. It could not: the launcher handed the CLI
    `--max-dispatches 0`, `Budgets` refuses a ceiling below one, the resulting
    validation error came back as "could not start run ...", and the node was
    booked `BLOCKED_DEPENDENCY` -- "a dependency that names a node which does
    not exist". A reader sent looking for a missing dependency at the exact
    moment the budget bound is the ambiguous halt this class exists to
    prevent, and it appeared only in the one case the shared budget was built
    for: a repair node arriving at a spent ceiling.
    """


#: Halt classes that mean "a person is needed", as opposed to "finished".
NEEDS_A_HUMAN = frozenset(
    {
        HaltClass.BLOCKED_EXTERNAL,
        HaltClass.MERGE_CONFLICT,
        HaltClass.MERGE_OBSTRUCTED,
        HaltClass.BLOCKED_PROVIDER,
        HaltClass.BLOCKED_DEPENDENCY,
        HaltClass.CORRUPT_STATE,
        HaltClass.STORAGE_UNAVAILABLE,
        HaltClass.AMBIGUOUS,
    }
)


#: The halts after which a branch may be parked: a node waiting for a decision (`BLOCKED_EXTERNAL`: an
#: external action at the head, a run waiting for an approval) or a run the contract parked, which the
#: loop cannot classify (`AMBIGUOUS`). A continuation pass is made only after one of these, and only
#: when the state holds an open decision (`governance.route_project_decisions` lists them).
PARKED_HALTS = frozenset({HaltClass.BLOCKED_EXTERNAL, HaltClass.AMBIGUOUS})


class RunVerdict(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    #: Quota or provider failure -- explicitly not a rejection.
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    #: Dispatched but its outcome is not yet determinable.
    UNDETERMINED = "UNDETERMINED"
    #: The run is still being executed by a live process that is not this
    #: controller -- typically a role run that outlived the master that
    #: started it. Known, not ambiguous: the verdict comes when it finishes.
    IN_FLIGHT = "IN_FLIGHT"
    #: The run exists and has not begun: no iteration, no candidate, no
    #: receipts. Distinct from UNDETERMINED because it is not unknown at all --
    #: it is the most knowable state a run can be in, and the right response is
    #: to dispatch it. Reporting it as unclassifiable deadlocked an otherwise
    #: unattended run: the controller re-read the same NEW state on every round
    #: and halted on it every time.
    NOT_STARTED = "NOT_STARTED"
    #: Stopped waiting for a human approval -- a trust dialog, a confirmation.
    #: Distinct from UNDETERMINED because it is not unclassifiable at all: it
    #: is known, actionable, and needs exactly one thing, which is a person.
    #: Reporting it as "unclassifiable" would send someone looking for a defect
    #: instead of answering the prompt.
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    #: A dry run performed no dispatch.
    NOT_RUN = "NOT_RUN"
    #: The run reached a ceiling this project set for itself -- iterations,
    #: dispatches, wallclock. Distinct from PROVIDER_UNAVAILABLE, which is
    #: somebody else's downtime: waiting fixes that one and cannot fix this
    #: one, and only a person can decide the work is worth more budget.
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    #: The run is parked on storage pressure (`blocked_kind ==
    #: "storage_pressure"`, STORAGE-B3). Not a provider wait: it never becomes
    #: `usage_limit` or a PROVIDER_RETRY timer, and only `hoh resume-storage`
    #: resumes it.
    STORAGE_WAIT = "STORAGE_WAIT"
    #: The run refused to start, or stopped, because storage admission is not
    #: usable (uninitialised state root, WSL without the host volume, an
    #: unavailable ledger, an unarmable recheck timer). NEEDS_YOU, never
    #: UNDETERMINED.
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


@dataclass
class RunOutcome:
    verdict: RunVerdict
    detail: str = ""
    candidate: str | None = None
    #: For PROVIDER_UNAVAILABLE: the earliest retry the provider named, in the
    #: timer form. With it the node parks on a durable timer that the resume
    #: loop can consume; without it the node keeps a human owner.
    retry_after: str | None = None
    #: For ACCEPTED: the tree object QA verified (`Candidate.tree_digest`). The
    #: accepted identity is this tree, not `candidate` -- for a dirty-tree
    #: candidate `candidate` names the *base* commit, and the verified content
    #: exists only as this tree until it is committed onto the run's branch.
    candidate_tree: str | None = None


class MergeFailure(StrEnum):
    """Why a merge did not land. Only UNKNOWN means the tool did not say."""

    CONFLICT = "CONFLICT"
    OBSTRUCTED = "OBSTRUCTED"
    NO_BRANCH = "NO_BRANCH"
    #: The branch does not carry what was accepted: its head's tree is not the
    #: verified tree (it moved after acceptance, or the accepted work was never
    #: committed onto it). Merging it would land something nobody verified.
    CANDIDATE_MISMATCH = "CANDIDATE_MISMATCH"
    UNKNOWN = "UNKNOWN"


@dataclass
class MergeResult:
    """What happened when the candidate was applied, in enough detail to act on.

    A bare boolean was the first version, and it forced every failure into one
    halt class. Git had already said which paths conflicted; throwing that away
    and reporting "unclassifiable" was the controller discarding evidence it
    had been handed.
    """

    landed: bool
    failure: MergeFailure | None = None
    detail: str = ""
    conflicting_paths: tuple[str, ...] = ()
    branch: str = ""
    target_head_before: str = ""
    merge_base: str = ""
    candidate: str = ""
    #: The commit that was integrated (or found already integrated): the exact
    #: object identity, never a branch name that could have moved since.
    integrated: str = ""
    #: The tree that was accepted, echoed so the receipt binds both.
    accepted_tree: str = ""

    def summary(self) -> str:
        parts = [f"branch {self.branch}" if self.branch else ""]
        if self.target_head_before:
            parts.append(f"target was at {self.target_head_before}")
        if self.merge_base:
            parts.append(f"merge-base {self.merge_base}")
        if self.conflicting_paths:
            parts.append("conflicting: " + ", ".join(self.conflicting_paths[:6]))
        return "; ".join(x for x in parts if x)


class RunLauncher(Protocol):
    """How the controller reaches a single HoH run."""

    def action_class(self, node: TaskNode) -> ActionClass: ...

    def depends_on(self, a: TaskNode, b: TaskNode) -> bool:
        """Whether `a` and `b` may not proceed in parallel.

        Measured from declared writes and semantic reads, never assumed. Two
        runs with disjoint write sets can still conflict when one reads what
        the other writes -- the failure that produced this project's union
        gate.
        """
        ...

    def launch(self, node: TaskNode) -> RunOutcome: ...

    def prepare(self, node: TaskNode) -> str | None:
        """Makes a node runnable, or explains why it is not.

        Returns None when the node is ready to dispatch, otherwise a reason.

        This exists for repair nodes. A gate failure produces a node with no
        specification and no run behind it -- the failure is known, the work to
        fix it is not yet written down. During this project's own campaign a
        person wrote each repair specification by hand, which is exactly the
        step that kept the loop from closing on its own. A launcher that can
        author one from the failure and start a run closes it; one that cannot
        says so here, and the node blocks rather than being dispatched into
        nothing.
        """
        return None

    def accepted_baseline(self, node: TaskNode) -> str | None:
        """The candidate the run had already accepted, before this dispatch.

        Read before launching and written into project state, because after a
        crash it is the only thing that distinguishes "this dispatch accepted
        something" from "something was accepted earlier".
        """
        ...

    def evaluate(self, node: TaskNode) -> RunOutcome:
        """The verdict of an already-dispatched run, **without dispatching**.

        This is what makes a resume exactly-once. A fresh process that finds a
        node marked RUNNING has three possibilities in front of it -- the work
        finished, it crashed, or it is still going -- and re-dispatching to find
        out would re-run an acceptance that may already have happened. Reading
        the run's own recorded state answers the question without spending
        anything, and returns UNDETERMINED when it genuinely cannot.
        """
        ...

    def merge(self, node: TaskNode, outcome: RunOutcome) -> "MergeResult":
        """Applies an accepted candidate, and says what happened if it did not."""
        ...


class GateRunner(Protocol):
    """How the controller reaches the global gates."""

    def subject(self) -> str:
        """The commit or tree the gates will run against."""
        ...

    def run(self, subject: str) -> list[GateResult]: ...


@dataclass
class Step:
    """One thing the controller did, for the decision log."""

    round: int
    kind: str
    node: str | None = None
    detail: str = ""


@dataclass
class Result:
    halt: HaltClass
    reason: str
    steps: list[Step] = field(default_factory=list)
    rounds: int = 0
    repairs_created: int = 0
    #: True only for the end of a drive restricted to a continuation record's nodes (`_allowed`): they are
    #: done (or its rounds are), and the open decisions still wait.
    continuation_spent: bool = False
    #: P3-32 slice 3, revision 1: True only for the halt of a plain round that found a limit of the autonomy contract
    #: reached before it dispatched (`BUDGET_EXHAUSTED`); `_drive_continuing` then lets the contract's policy act.
    limit_held: bool = False

    @property
    def ambiguous_halts(self) -> int:
        """How often the loop could not classify something.

        The measure worth tracking across a campaign: each one is a place a
        human had to be reachable.
        """
        return sum(1 for s in self.steps if s.kind == HaltClass.AMBIGUOUS)


#: Rejections a node may take before it is abandoned. A rejection is input to
#: the next iteration, not an endpoint -- but an unbounded retry is a spin.
MAX_REJECTIONS = 3


class ProjectController:
    """Drives a project to its fixpoint, persisting every transition."""

    def __init__(
        self,
        store: ProjectStore,
        launcher: RunLauncher,
        gates: GateRunner,
        *,
        max_rounds: int = 40,
        max_repairs: int = 5,
        authority: MasterAuthority | None = None,
        planner: ReadyFrontierPlanner | None = None,
        width: int | None = None,
        capacity_policy: Policy | None = None,
        autonomy=None,
    ) -> None:
        self.store = store
        self.launcher = launcher
        self.gates = gates
        #: How many independent READY nodes one round may launch together
        #: (P3-02 slice 2). Chosen, in this order, from `width`, the environment
        #: (`HOH_WAVE_WIDTH`), the launcher's own `wave_width`, else 1 -- LINEAR,
        #: the one-node-per-round loop of before -- until the modes of P3-25 and
        #: P3-26 compute one. Anything that is not a whole number from 1 to
        #: `MAX_WAVE_WIDTH` is a `ValueError`, never a silent 1.
        self.width = self._width_of(width, launcher)
        #: P3-23 (slice 4): the dispatch budget the project declares (`hoh project budget`,
        #: `<project>/dispatch-budget.json`) is bound to the launcher here, once, by the one
        #: place every drive of every path builds a controller. A declaration that cannot be
        #: read or that the launcher refuses is a `ValueError` (the drive halts by name).
        store_dir = getattr(store, "dir", None)
        self.declared_budget = (budgetconfig.bind(store_dir, launcher)
                                if store_dir is not None else None)
        self._wave_unavailable_said = False
        #: The broker policy (ages of declared and measured facts) the wave's
        #: capacity admission judges by, or None for `capacitycheck.policy()`.
        self.capacity_policy = capacity_policy
        self._capacity_opt_out_said = False
        #: Chooses the node of each round from the READY frontier. Injectable
        #: so a test can make it say something else and require the loop to
        #: follow; the default is the product planner.
        self.planner = planner if planner is not None else ReadyFrontierPlanner()
        self.max_rounds = max_rounds
        self.max_repairs = max_repairs
        #: P3-30 slice 2: the contract this controller continues under beside a parked branch -- the one
        #: given, else the one the project registered (`AUTONOMY_FILE`). A registered file that cannot be
        #: loaded is a `ValueError`, never "no contract": a drive does not quietly become an ungoverned one.
        self.autonomy = autonomy if autonomy is not None else self._registered_contract(store)
        #: The READY nodes a continuation pass lets `_drive` dispatch (None: all of them).
        self._allowed: frozenset[str] | None = None
        #: The master authority this controller drives under, or None for the
        #: pre-P2 behaviour (fenced by `write_seq` and the project lock only).
        #: With one, every authoritative act -- a state write, a dispatch that
        #: spends, a merge that lands accepted work -- is preceded by a check
        #: that this session still holds its epoch, and a stale one halts
        #: SUPERSEDED having written nothing.
        self.authority = authority

    def _width_of(self, explicit: object, launcher: object) -> int:
        env = os.environ.get(WAVE_WIDTH_ENV)
        if explicit is not None:
            source, value = "width", explicit
        elif env is not None and env != "":
            source = WAVE_WIDTH_ENV
            value = int(env) if re.fullmatch(r"[0-9]{1,3}", env) else env
        elif (declared := self._declared_width()) is not None:
            source, value = "the parallelism declaration", declared
        else:
            source, value = "the launcher's wave_width", getattr(launcher, "wave_width", 1)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_WAVE_WIDTH:
            raise ValueError(f"{source} must be a whole number from 1 to {MAX_WAVE_WIDTH}, "
                             f"not {value!r}")
        return value

    def _declared_width(self) -> int | None:
        """P3-25/26: the widest wave the project's `parallelism.json` can offer, None without one. A
        declaration that is refused is a `ValueError` (the drive halts by name), never a silent 1."""
        from . import parallelmode

        directory = getattr(self.store, "dir", None)
        if directory is None:
            return None
        try:
            return parallelmode.outer_width(directory)
        except parallelmode.ParallelismError as exc:
            raise ValueError(f"the parallelism declaration is refused: {exc}") from exc

    # -- persistence ------------------------------------------------------- #

    def _persist(self, state: ProjectState) -> ProjectState:
        """Writes and re-reads.

        Re-reading is not paranoia: `write_state` increments `write_seq` on
        the object it is given, and a controller that kept holding the
        pre-write object would fence itself out on the next round.
        """
        if self.authority is None:
            self.store.write_state(state)
            self._disarm_stale_waits(state)
            return self.store.read_state()
        # Under master authority the lease, not a drive-long lock, keeps a
        # second master out, so the program lock is held only for this write:
        # check the epoch, stamp it, write, renew -- one critical section. A
        # frozen or partitioned master therefore holds nothing a successor
        # needs, and when it wakes its next write is refused here.
        with write_lock(self.store):
            self.authority.stamp(state)
            self.store.write_state(state)
            # Only after the checked write: a superseded master withdrew a
            # successor's timer before its own refusal (review A2, c).
            self._disarm_stale_waits(state)
            try:
                self.authority.renew()
            except Exception:                         # noqa: BLE001 - see below
                # The write is durable; the lease is renewed at the next write
                # or by the keepalive. Reporting this as a refusal would record
                # a state change as not having happened (review A, S3).
                pass
            return self.store.read_state()

    def _disarm_stale_waits(self, state: ProjectState) -> None:
        try:
            disarm_stale_waits(self.store.dir, state)
        except TimerStoreError:
            # An unreadable timer store must not crash a controller that did
            # not need it before (review A2, N5); a stale timer it cannot
            # withdraw is refused by the resume loop, which re-checks the node.
            pass

    def _authorise(self, act: str) -> None:
        """Refuse an authoritative act by a session that is not the master.

        Called before the acts whose repetition would be duplicate work: a
        dispatch (spends budget, may produce a second candidate) and a merge
        (lands accepted work). A state write is checked in `_persist`.
        """
        if self.authority is not None:
            try:
                self.authority.check()
            except StaleMaster as exc:
                raise StaleMaster(f"{act} refused: {exc}") from exc

    def _record(
        self,
        state: ProjectState,
        kind: DecisionKind,
        reason: str,
        payload: dict,
        evidence: list[str] | None = None,
    ) -> None:
        """Appends a decision record.

        Everything here is a judgement that cannot be re-derived from the tree
        afterwards. Why a node was abandoned, why an edge was added, why a
        finding was dispositioned -- the tree shows the result and never the
        reason.
        """
        state.decisions.append(
            DecisionRecord(
                id=f"D{len(state.decisions) + 1:04d}",
                kind=kind,
                actor="orchestrator",
                reason=reason,
                payload=payload,
                evidence=evidence or [],
            )
        )

    # -- the loop ---------------------------------------------------------- #

    def run(self) -> Result:
        try:
            if self.authority is not None:
                # The lease is the exclusion; see `_persist`. Holding the
                # program lock for a whole drive would let a frozen master
                # block every successor for as long as it stays frozen.
                self.authority.check()
                with self.authority.keepalive():
                    return self._drive_continuing()
            if (self.store.dir / "epochs.json").is_file():
                # A governed project is driven only by a master. An ungoverned
                # controller re-reads the current epoch from disk and would pass
                # the store fence while a live master believes it is alone
                # (review A, P8).
                return Result(halt=HaltClass.SUPERSEDED, reason=(
                    f"project {self.store.project_id} is governed by a master epoch; "
                    "a controller without authority may not drive it"))
            with self.store.lock():
                return self._drive_continuing()
        except StaleMaster as exc:
            # Before StaleWrite, which it subclasses: a superseded master is a
            # known, healthy state that belongs to a successor. Nothing was
            # written by this session after the refusal.
            return Result(halt=HaltClass.SUPERSEDED, reason=str(exc))
        except LockBusy as exc:
            return Result(
                halt=HaltClass.BLOCKED_DEPENDENCY,
                reason=f"another orchestrator holds this project: {exc}",
            )
        except StaleWrite as exc:
            # Caught before StoreError, which it subclasses, because the two
            # call for opposite remedies and collapsing them hides that. A
            # stale write means the state is fine and *this session* is behind:
            # reload and the work continues. A corrupt state means the file
            # cannot be trusted and nothing should proceed until a person has
            # looked. Reporting the first as the second would send someone
            # inspecting a healthy project.
            return Result(
                halt=HaltClass.AMBIGUOUS,
                reason=(
                    f"another orchestrator advanced this project while this round was "
                    f"in flight: {exc} -- what it did cannot be read from here, so this "
                    "session stops rather than reapplying its own view"
                ),
            )
        except StoreError as exc:
            return Result(halt=HaltClass.CORRUPT_STATE, reason=str(exc))

    @staticmethod
    def _registered_contract(store: ProjectStore):
        path = store.dir / AUTONOMY_FILE
        if not os.path.lexists(path):
            return None
        from .governance import RoutingRefused, load_contract_file

        try:
            return load_contract_file(path)
        except RoutingRefused as exc:
            raise ValueError(f"autonomy contract refused: {exc}") from None

    def _drive_continuing(self) -> Result:
        """`_drive`, then -- only for a project that has a contract and only after a halt beside which a
        branch may be parked -- the continuation passes of `_continue_beside_parked`."""
        result = self._drive()
        if self.autonomy is None or (result.halt not in PARKED_HALTS and not result.limit_held):
            return result
        return self._continue_beside_parked(result)

    def _continue_beside_parked(self, result: Result) -> Result:
        """P3-30 slice 2: the READY work that is independent of a parked branch carries on.

        Per pass, in this order: (1) `governance.route_project_decisions` -- the open decisions of the
        project, and a record for each one the contract hands to a resolver (one that cannot be routed or
        recorded is not hidden: `continue_by_policy` then finds a node with no way out and holds); (2)
        `governance.resolve_routed_decisions` -- the answer, create-only, to each routed EXTERNAL decision
        the contract has pre-authorised (it executes nothing; a failure is a step and does not hold the
        independent work back); (3) `governance.continue_by_policy` -- what the contract allows to
        continue, written down (create-only, one record per state version and contract) **before**
        anything is dispatched; (3b) when that record is a hold for a reached limit whose policy is DEGRADE or
        REPLAN, `governance.act_on_limit` -- the wave at width one (recorded in `limit-acts/`), or the
        replanning step added to the graph and dispatched in the next pass (P3-32 slice 3); (4) when it says
        CONTINUE, `_drive()` restricted to them: the loop's own rounds, sequential or wave, over the nodes the
        record names and no others. There is no second dispatcher.

        Fences, each with a node: only a halt in `PARKED_HALTS` starts a pass; a block no routing explains
        (`_unexplained_blocks`) refuses it; no open decision ends it.

        Bounded: at most `CONTINUATION_PASSES` passes, and a state version that already has its record
        is not decided again (the record is the counter, and it exists before the attempt it counts; the one
        exception is a hold for a reached limit whose act was never recorded, `limit_act_unfinished`). A
        decision that holds, a refusal and a record that cannot be written end the passes with the halt
        in hand and a step that says why; nothing is dispatched on any of them.
        """
        from . import governance

        steps_: list[Step] = list(result.steps)
        runs_root = getattr(self.launcher, "root", None) or self.store.root
        #: the halt of a plain round that found the contract's limit reached: the passes act on the policy, and
        #: the drive ends held at the limit whatever they did (the restricted drives are the policy's, not a
        #: way past it)
        limit_result = result if result.limit_held else None
        for pass_ in range(1, CONTINUATION_PASSES + 1):
            state = self.store.read_state()
            if not any(n.action_class is not ActionClass.EXTERNAL for n in state.ready()):
                break                                    # nothing could carry on
            wave_id = f"c{state.write_seq}-{self.autonomy.digest()}{self._review_suffix(runs_root)}"
            decided = os.path.lexists(self.store.dir / governance.POLICY_DIR / f"{wave_id}.json")
            # the one record that is not final: a hold for a reached limit whose act (below) was never
            # recorded -- the pass ended between the two. The hold is read again (nothing is rewritten).
            unfinished = decided and governance.limit_act_unfinished(self.store, self.autonomy, wave_id)
            if decided and not unfinished:
                steps_.append(Step(pass_, "CONTINUATION_DECIDED", wave_id,
                                   "this state version was decided before; not decided again"))
                break
            unexplained = self._unexplained_blocks(state, runs_root)
            if unexplained:
                steps_.append(Step(pass_, "CONTINUATION_REFUSED", wave_id,
                                   f"blocked for a reason no routing log or decision explains: "
                                   f"{', '.join(unexplained)}; nothing continues beside it"[:300]))
                break
            try:
                with (write_lock(self.store) if self.authority is not None else nullcontext()):
                    self._authorise("routing and continuation of the open decisions")
                    report = governance.route_project_decisions(
                        self.store, self.autonomy, runs_root)
                    if not report.decisions and limit_result is None:
                        break                            # no branch is parked: nothing to continue beside
                    answered = governance.resolve_routed_decisions(
                        self.store, self.autonomy, runs_root)
                    # said at once: an answer that is recorded is never spent invisibly, whatever the
                    # decision of the wave does next (it may refuse)
                    for d in answered.decisions:
                        if d.outcome == governance.ANSWERED and d.recorded:
                            steps_.append(Step(pass_, "RESOLUTION_ANSWERED", d.node_id,
                                               f"decision {d.decision_digest} answered within the "
                                               f"pre-authorisation (recorded now); nothing is executed"))
                        elif d.reason in governance.RESOLUTION_FAILURES:
                            steps_.append(Step(pass_, "RESOLUTION_FAILED", d.node_id,
                                               f"decision {d.decision_digest} not answered: {d.reason}"))
                    plan = governance.continue_by_policy(
                        self.store, self.autonomy, runs_root, wave_id=wave_id,
                        depends_on=self.launcher.depends_on, dry_run=unfinished)
                    if unfinished and not (plan.outcome == "HOLD" and plan.reason == governance.LIMIT_REACHED):
                        # decided again without a record to stand on: nothing is dispatched from it
                        raise governance.ContinuationError(
                            "the limit of the recorded hold is not reached any more; nothing is acted on")
                    act = None
                    if (plan.outcome == "HOLD" and plan.reason == governance.LIMIT_REACHED
                            and self.autonomy.horizon.on_limit in governance.ACTING_POLICIES):
                        act = governance.act_on_limit(
                            self.store, self.autonomy, runs_root, plan,
                            depends_on=self.launcher.depends_on)
            except (governance.RoutingRefused, governance.ContinuationError) as exc:
                steps_.append(Step(pass_, "CONTINUATION_REFUSED", wave_id, str(exc)[:300]))
                break
            steps_.append(Step(pass_, "CONTINUATION", wave_id,
                               f"{plan.outcome}{': ' + plan.reason if plan.reason else ''}; continued "
                               f"{list(plan.continued)}; withheld {[w[0] for w in plan.withheld]}"))
            if act is not None:
                # what the drive prints of a step is `kind:node`, so the act and its reason are the node
                steps_.append(Step(pass_, "LIMIT_ACT", act.kind + (f":{act.reason}" if act.reason else ""),
                                   act.describe()[:300]))
                if act.kind == governance.REPLAN_REQUEST:
                    refused = self._request_replan(act, wave_id)
                    if refused:
                        steps_.append(Step(pass_, "REPLAN_NOT_REQUESTED", act.node.id, refused[:300]))
                        break
                    steps_.append(Step(pass_, "REPLAN_REQUESTED", act.node.id,
                                       "the step is a node of the graph now; the next pass dispatches it"))
                    continue                             # decided again at the state it changed
                plan = act.plan
            if not plan.continued:                       # a HOLD continues nothing
                break
            self._allowed = frozenset(plan.continued)
            try:
                inner = self._drive()
            finally:
                self._allowed = None
            steps_.extend(inner.steps)
            result = inner
            if not (inner.continuation_spent or inner.halt in PARKED_HALTS):
                break                                    # a halt of another kind ends the drive as it is
        else:
            steps_.append(Step(CONTINUATION_PASSES, "CONTINUATION_PASSES_USED", None,
                               f"{CONTINUATION_PASSES} passes made; the next drive decides again"))
        if limit_result is not None:
            if result is not limit_result and not result.continuation_spent:
                # a restricted drive ended on something of its own (a merge conflict, a rejection ...): the drive
                # is held at the limit, and the halt it superseded stays in the reason and as a step, so whoever
                # reads the result (`hoh project drive`, `hoh resume-quota --drive`) is not told only "limit"
                steps_.append(Step(CONTINUATION_PASSES, "LIMIT_SUPERSEDED_HALT", result.halt.value, result.reason[:300]))
                limit_result = replace(
                    limit_result, reason=f"{limit_result.reason}; the last drive restricted to the policy's "
                                         f"nodes ended {result.halt.value}: {result.reason[:240]}")
            result = limit_result
        result.steps = steps_
        return result

    def _limit_held(self, state: ProjectState, ready_: list, round_: int, steps_: list[Step],
                    reparaturen: int) -> Result | None:
        """P3-32 slice 3, revision 1: the contract's limits bind every round, not only the pass beside a parked
        branch. Before a plain round dispatches anything it asks `governance.limit_reached` (the reading
        `continue_by_policy` makes); a limit that is reached, or named and not measurable, ends the drive
        `BUDGET_EXHAUSTED` with `limit_held`, nothing dispatched, and the passes act on the policy (REPLAN: only
        the replanning step; DEGRADE: width one, three waves; PARK, STOP: the hold). Not asked by a drive
        restricted to a continuation record's nodes (the record is the policy's answer), nor when no contract
        governs the project, nor when nothing READY could be dispatched (an EXTERNAL action halts as before)."""
        if self.autonomy is None or self._allowed is not None:
            return None
        if not any(self.launcher.action_class(n) is not ActionClass.EXTERNAL for n in ready_):
            return None
        from . import governance

        runs_root = getattr(self.launcher, "root", None) or self.store.root
        if not governance.limit_reached(state, self.autonomy, runs_root):
            return None
        why = (f"a limit of the autonomy contract is reached (policy {self.autonomy.horizon.on_limit.value}): "
               "the plain rounds dispatch nothing; the policy decides what may continue")
        steps_.append(Step(round_, "LIMIT_HELD", self.autonomy.horizon.on_limit.value, why))
        return Result(HaltClass.BUDGET_EXHAUSTED, why, steps_, round_, reparaturen, limit_held=True)

    def _request_replan(self, act, wave_id: str) -> str:
        """P3-32 slice 3: the replanning step a REPLAN limit policy asks for, added to the graph as a
        READY node (its specification is already written, `governance.act_on_limit`) with the decision
        that says why. "" when it is there (it may already have been: a pass that ended after this),
        else why it is not. The project state is written the way every other write of the loop is."""
        try:
            state = self.store.read_state()
            if any(n.id == act.node.id for n in state.nodes):
                return ""
            if state.write_seq != act.plan.state["write_seq"]:
                return "the project state moved since the limit was decided"
            state.nodes.append(act.node)
            self._record(state, DecisionKind.POLICY_DISPOSITION,
                         "the limit of the autonomy contract was reached and its policy is REPLAN: "
                         "a replanning step is requested",
                         {"node": act.node.id, "wave": wave_id, "policy": "REPLAN",
                          "contract": act.plan.contract["digest"]})
            self._persist(state)
        except StaleMaster:
            raise                                        # a master that lost the lease stops the drive
        except StoreError as exc:
            return f"{type(exc).__name__}: {exc}"
        return ""

    def _review_suffix(self, runs_root) -> str:
        """`-<timer id>` of the earliest REVIEW_DUE timer of this project that is armed and due, else "".

        A hold is decided once per state version (its record exists and moves nothing), so a review that
        falls due while the state stands still would never be read again. A due review is therefore part of
        the wave's name: it is decided once more, once per timer -- `continue_by_policy` finds it due, holds
        the wave (`REVIEW_DUE`) and fires it, so the drive after that is a normal one. The timers are those
        of the runs root, where `governance.apply_mode` arms them, and the filter is governance's own
        (`due_reviews`: this project, this programme epoch, the clock `governance` reads); an unreadable
        store gives no suffix (the review's own reader refuses it)."""
        from . import governance

        ids = governance.due_reviews(self.autonomy, runs_root, self.store.project_id)
        return f"-{ids[0]}" if ids else ""

    @staticmethod
    def _unexplained_blocks(state: ProjectState, runs_root) -> list[str]:
        """The BLOCKED nodes that are neither a timed wait nor an open decision: no routing of the contract
        stopped their run and no decision kind names them (a merge of unknown state, a capability violation,
        a run that ended with nothing logged). The policy would give such a node a human gate whenever the
        contract expects a person, so the pass does not leave it to the policy: it continues beside a branch
        the contract parked, not beside a block nobody has looked at. An unreadable graph is refused too."""
        from pathlib import Path

        from .decisionfrontier import TIMED_KINDS, FrontierError, derive_frontier
        from .governance import _collect_logs

        stops, unreadable = _collect_logs(state, Path(runs_root))
        try:
            frontier = derive_frontier(state, stops=stops, unreadable=unreadable)
        except FrontierError as exc:
            return [f"the graph has no frontier ({str(exc)[:80]})"]
        by_id = {n.id: n for n in state.nodes}
        return [i for i in frontier.other_blocked if by_id[i].blocked_kind not in TIMED_KINDS]

    def _drive(self) -> Result:
        steps_: list[Step] = []
        reparaturen = 0
        if self.declared_budget is not None:
            steps_.append(Step(0, "DISPATCH_BUDGET", None, self.declared_budget.describe()))

        try:
            state = self.store.read_state()
        except StoreError as exc:
            return Result(halt=HaltClass.CORRUPT_STATE, reason=str(exc), steps=steps_)

        # A node whose dependency names something that does not exist reads,
        # to a scheduler, exactly like a node whose dependency is satisfied.
        unknown_ = state.unknown_dependencies()
        if unknown_:
            why = "dependencies naming nodes that do not exist: " + "; ".join(
                f"{k} -> {v}" for k, v in sorted(unknown_.items())
            )
            steps_.append(Step(0, HaltClass.BLOCKED_DEPENDENCY, detail=why))
            return Result(HaltClass.BLOCKED_DEPENDENCY, why, steps_)

        # The dispatch activity log, folded by the pure reducer (P2-15). A log that
        # cannot be folded, or that disagrees with the node lifecycles about what is
        # in flight, is not something this session may drive on: which of the two is
        # right cannot be read from here, and re-dispatching on the wrong one repeats
        # an acceptance or abandons a live run.
        try:
            disagree_ = in_flight_disagreements(state)
        except ActivityLogError as exc:
            steps_.append(Step(0, HaltClass.CORRUPT_STATE, detail=str(exc)))
            return Result(HaltClass.CORRUPT_STATE, str(exc), steps_)
        if disagree_:
            why = ("the activity log and the node lifecycles disagree about what is in "
                   "flight: " + "; ".join(disagree_))
            steps_.append(Step(0, HaltClass.AMBIGUOUS, detail=why))
            return Result(HaltClass.AMBIGUOUS, why, steps_)

        # P3-22 slice 3: the holds a controller that died left (its wave's OPEN outcome names
        # them) are recovered once, here, before the loop decides anything. Bounded on every
        # path: a recovery that cannot be bounded halts by name instead of being left.
        unbounded_ = self._recover_open_waves(steps_)
        if unbounded_ is not None:
            return Result(HaltClass.AMBIGUOUS, unbounded_, steps_)
        try:
            state = self.store.read_state()              # the recovery may have written it
        except StoreError as exc:
            return Result(halt=HaltClass.CORRUPT_STATE, reason=str(exc), steps=steps_)

        # A RUNNING node means a previous session died mid-dispatch. Rather
        # than halting on principle, ask the run's own recorded state what
        # happened -- that is a read, costs nothing, and is the only way a
        # resume can be exactly-once. Re-dispatching to find out would re-run
        # an acceptance that may already have happened.
        laufend = [n for n in state.nodes if n.lifecycle is Lifecycle.RUNNING]
        for n in laufend:
            outcome_ = self.launcher.evaluate(n)
            steps_.append(Step(0, "EVALUATED", n.id, f"{outcome_.verdict}: {outcome_.detail}"))
            if outcome_.verdict is RunVerdict.IN_FLIGHT:
                why = (f"node {n.id}: its run is still executing in another live process "
                       f"({outcome_.detail}); waiting for it rather than guessing")
                steps_.append(Step(0, HaltClass.RUN_IN_FLIGHT, n.id, why))
                return Result(HaltClass.RUN_IN_FLIGHT, why, steps_)
            if outcome_.verdict is RunVerdict.NOT_STARTED:
                # Marked RUNNING, but the run never began -- a process died
                # between persisting the lifecycle and dispatching. Nothing was
                # spent and nothing can be repeated, so it simply becomes work
                # again.
                # The node of the *current* state: an earlier iteration's `_persist`
                # replaced `state`, and a lifecycle written to the stale object is lost.
                reset_ = state.node(n.id)
                assert reset_ is not None
                reset_.lifecycle = Lifecycle.READY
                close_dispatch(state, n.id, EventKind.FAILED,
                               failure=f"NOT_STARTED: {outcome_.detail}"[:300])
                state = self._persist(state)
                steps_.append(Step(0, "RESET_TO_READY", n.id, outcome_.detail))
                continue
            if outcome_.verdict is RunVerdict.UNDETERMINED:
                why = (
                    f"node {n.id} is recorded as RUNNING and its run state does not say "
                    f"what happened: {outcome_.detail}. Re-dispatching could repeat an "
                    "acceptance that already landed, so this is not decided here"
                )
                steps_.append(Step(0, HaltClass.AMBIGUOUS, n.id, why))
                return Result(HaltClass.AMBIGUOUS, why, steps_)
            state = self._settle(state, n.id, outcome_, 0, steps_)
            if isinstance(state, Result):
                state.steps = steps_
                return state

        # A continuation pass (`self._allowed`) dispatches only the nodes its record names, and takes at
        # most that many rounds; every other READY node is left as it is.
        allowed = self._allowed
        rounds_ = self.max_rounds if allowed is None else min(self.max_rounds, len(allowed))
        for round_ in range(1, rounds_ + 1):
            ready_ = state.ready()
            if allowed is not None and ready_:
                ready_ = [n for n in ready_ if n.id in allowed]
                if not ready_:
                    why = ("the nodes the continuation record names are done; the open decisions "
                           "still wait for an answer")
                    steps_.append(Step(round_, "CONTINUATION_SPENT", None, why))
                    return Result(HaltClass.BLOCKED_EXTERNAL, why, steps_, round_, reparaturen,
                                  continuation_spent=True)

            if not ready_:
                result, state, new_ = self._closure(state, round_, steps_, reparaturen)
                reparaturen = new_
                if result is not None:
                    result.steps = steps_
                    result.rounds = round_
                    result.repairs_created = reparaturen
                    return result
                continue

            held_ = self._limit_held(state, ready_, round_, steps_, reparaturen)
            if held_ is not None:
                return held_

            # The planner is handed a snapshot, not the loop's own list: what it
            # returns is checked against `ready_`, and a planner that could add
            # to that list could vouch for its own answer.
            plan = self.planner.plan(tuple(ready_), self.launcher.depends_on)
            task = plan.head
            # The plan is an answer about `ready_`, not a source of nodes: no
            # head, a head that is not one of the nodes just read as READY, or
            # one that is no longer READY would dispatch something the state
            # never offered, so it halts.
            if (not any(task is n for n in ready_)
                    or task.lifecycle is not Lifecycle.READY):
                why = (f"the frontier planner chose "
                       f"{'nothing' if task is None else 'node ' + task.id} from "
                       f"{len(ready_)} ready node(s): not a READY node of the ready "
                       "frontier, so nothing is dispatched")
                steps_.append(Step(round_, HaltClass.AMBIGUOUS,
                                   None if task is None else task.id, why))
                return Result(HaltClass.AMBIGUOUS, why, steps_, round_, reparaturen)

            if self.launcher.action_class(task) is ActionClass.EXTERNAL:
                why = (
                    f"node {task.id} carries an irreversible external action; "
                    "that decision is not delegated to the orchestrator"
                )
                steps_.append(Step(round_, HaltClass.BLOCKED_EXTERNAL, task.id, why))
                return Result(HaltClass.BLOCKED_EXTERNAL, why, steps_, round_, reparaturen)

            # Serialise only where a dependency is actually measured. A blanket
            # rule would be safe and would also throw away every parallel round.
            for later_ in plan.deferred:
                if later_.blocked_by is task and later_.reason == SEMANTIC_DEPENDENCY:
                    steps_.append(
                        Step(round_, "SERIALISED", f"{task.id}<->{later_.node.id}",
                             "measured semantic dependency")
                    )

            # P3-02 slice 2: with a width above one, and a launcher that can take
            # part, the independent nodes of the plan are launched together.
            if self._waves_wanted(plan, round_, steps_):
                out = self._wave_round(state, plan, round_, steps_)
                if isinstance(out, Result):
                    out.steps = steps_
                    out.rounds = round_
                    out.repairs_created = reparaturen
                    return out
                state = out
                continue

            # A node with no run behind it -- a repair node, typically -- has to
            # be made runnable first. If the launcher cannot, it blocks: a
            # dispatch into nothing produces a verdict about nothing.
            try:
                obstacle = self.launcher.prepare(task)
            except BudgetExhausted as empty:
                task.lifecycle = Lifecycle.BLOCKED
                task.blocked_kind = None
                task.retry_after = None
                task.note = str(empty)
                state = self._persist(state)
                why = f"node {task.id} has no budget left: {empty}"
                steps_.append(
                    Step(round_, HaltClass.BUDGET_EXHAUSTED, task.id, why))
                return Result(HaltClass.BUDGET_EXHAUSTED, why, steps_,
                              round_, reparaturen)
            if obstacle:
                task.lifecycle = Lifecycle.BLOCKED
                task.blocked_kind = None
                task.retry_after = None
                task.note = obstacle
                state = self._persist(state)
                why = f"node {task.id} cannot be made runnable: {obstacle}"
                steps_.append(Step(round_, HaltClass.BLOCKED_DEPENDENCY, task.id, why))
                return Result(HaltClass.BLOCKED_DEPENDENCY, why, steps_, round_, reparaturen)

            # Before spending anything, ask whether this node's run has
            # *already* accepted something newer than the baseline this node
            # carries. It can have: a dispatch that succeeded and whose merge
            # then failed leaves exactly that state, and re-dispatching would
            # pay again for work that is already verified -- and could accept a
            # second, different candidate for the same node.
            #
            # Found by the first real end-to-end run: the candidate was
            # accepted, the merge was blocked by untracked build output, and
            # after the obstruction was cleared the node was READY again with
            # its acceptance still sitting in the run record.
            in_advance = self.launcher.evaluate(task)
            if in_advance.verdict is RunVerdict.IN_FLIGHT:
                why = (f"node {task.id} is READY but a live process still executes its "
                       f"run ({in_advance.detail}); dispatching again would duplicate it")
                steps_.append(Step(round_, HaltClass.RUN_IN_FLIGHT, task.id, why))
                return Result(HaltClass.RUN_IN_FLIGHT, why, steps_, round_, reparaturen)
            if in_advance.verdict is RunVerdict.ACCEPTED:
                steps_.append(Step(round_, "ALREADY_ACCEPTED", task.id, in_advance.detail))
                result = self._settle(state, task.id, in_advance, round_, steps_)
                if isinstance(result, Result):
                    result.steps = steps_
                    result.rounds = round_
                    result.repairs_created = reparaturen
                    return result
                state = result
                continue

            task.lifecycle = Lifecycle.RUNNING
            # The dispatch is an activity: its STARTED event is persisted in the
            # same write as RUNNING, before the launch (P2-15).
            start_dispatch(state, task.id)
            # The baseline goes into state before the dispatch, not after: it
            # is what a fresh process needs in order to tell an acceptance from
            # a run that ended where it started, and a process that dies during
            # the dispatch is exactly when it is needed.
            task.accepted_before = self.launcher.accepted_baseline(task)
            try:
                state = self._persist(state)
            except StaleMaster:
                raise
            except StaleWrite as exc:
                why = f"another orchestrator advanced this project: {exc}"
                steps_.append(Step(round_, HaltClass.AMBIGUOUS, task.id, why))
                return Result(HaltClass.AMBIGUOUS, why, steps_, round_, reparaturen)
            task = state.node(task.id)          # the re-read copy
            assert task is not None

            self._authorise(f"dispatch of node {task.id}")
            outcome_ = self.launcher.launch(task)
            result = self._settle(state, task.id, outcome_, round_, steps_)
            if isinstance(result, Result):
                result.steps = steps_
                result.rounds = round_
                result.repairs_created = reparaturen
                return result
            state = result
            continue

        if allowed is not None:
            left = [n.id for n in state.ready() if n.id in allowed]
            why = (f"the {rounds_} round(s) of this continuation pass are used; "
                   f"{', '.join(left)} not dispatched yet" if left else
                   "the nodes the continuation record names are done; the open decisions "
                   "still wait for an answer")
            steps_.append(Step(rounds_, "CONTINUATION_SPENT", None, why))
            return Result(HaltClass.BLOCKED_EXTERNAL, why, steps_, rounds_, reparaturen,
                          continuation_spent=True)
        why = f"{self.max_rounds} rounds without reaching a fixpoint"
        steps_.append(Step(self.max_rounds, HaltClass.ROUND_LIMIT, detail=why))
        return Result(HaltClass.ROUND_LIMIT, why, steps_, self.max_rounds, reparaturen)

    # -- waves (P3-02 slice 2) --------------------------------------------- #

    def _waves_wanted(self, plan: FrontierPlan, round_: int, steps_: list[Step]) -> bool:
        if self.width < 2 or len(plan.selected) < 2:
            return False
        if not all(_WAVE_NODE_ID.fullmatch(n.id) for n in plan.selected[: self.width]):
            return False                   # an id the wave record cannot hold: one node per round
        missing = [a for a in WAVE_LAUNCHER_API if not callable(getattr(self.launcher, a, None))]
        if not missing and getattr(self.launcher, "root", None) is None:
            missing = ["root"]
        if missing:
            if not self._wave_unavailable_said:
                self._wave_unavailable_said = True
                steps_.append(Step(round_, "WAVE_UNAVAILABLE", None,
                                   f"width {self.width} asked for; this launcher lacks "
                                   f"{', '.join(missing)}: one node per round"))
            return False
        return True

    def _block_node(self, state: ProjectState, node: TaskNode, note: str) -> ProjectState:
        node.lifecycle = Lifecycle.BLOCKED
        node.blocked_kind = None
        node.retry_after = None
        node.note = note
        return self._persist(state)

    def _wave_round(self, state: ProjectState, plan: FrontierPlan, round_: int,
                    steps_: list[Step]) -> "ProjectState | Result":
        """One round that launches the independent READY nodes of `plan` together.

        The sequential round's steps, per node and in the same order -- prepare,
        evaluate, persist RUNNING, authorise, launch, settle -- with three
        differences that are the whole slice: the nodes that survive prepare and
        evaluate are admitted and launched by `ParallelWaveExecutor` (each in its
        own thread, on the run's own worktree and hold); RUNNING is persisted
        once for the wave, after its write-ahead record and before the first
        dispatch; and every outcome is settled, in plan order, before the first
        halting one ends the round -- a parked or failed sibling does not stop
        the members that did not need it.
        """
        launcher = self.launcher
        head = plan.head
        wave: list[TaskNode] = []
        #: P3-24 slice 4: every node this round leaves out before the executor is asked, with its
        #: reason of the outcome's vocabulary (`ParallelismDegradationReason` + `CutReason`): written into the wave's outcome
        #: (or, for a round that launches nothing, the project's `latest-cuts` outcome).
        cuts: list[waverecovery.Cut] = []
        cut_ids: set[str] = set()
        ready_total = len(plan.selected) + len(plan.deferred)

        def cut(node_id: str, reason, detail: str) -> None:
            if node_id not in cut_ids:
                cut_ids.add(node_id)
                cuts.append(waverecovery.Cut(
                    node_id, reason.value, waverecovery._ascii(detail, waverecovery.MAX_CUT_DETAIL)))

        def cut_from(mark: int, kind: str, reason) -> None:
            for step in steps_[mark:]:
                if step.kind == kind and step.node:
                    cut(step.node, reason, step.detail)

        width_cut = [ParallelismDegradationReason.WIDTH.value] if len(plan.selected) > self.width else []
        # P3-19 slice 2: before anything is prepared (no hold exists yet to give back) the nodes this
        # round would prepare are put to the resource scheduler; a node it does not admit is skipped
        # below, ahead of `prepared.append`, and stays READY.
        asked = [n for n in plan.selected[: self.width]
                 if n is head or launcher.action_class(n) is not ActionClass.EXTERNAL]
        mark = len(steps_)
        allowed = self._capacity_admission(asked, round_, steps_, state, ready_total)
        cut_from(mark, "CAPACITY_WAIT", waverecovery.CutReason.CAPACITY)
        if isinstance(allowed, Result):
            self._record_round_cuts(cuts, ready_total, width_cut, round_, steps_)
            return allowed
        # P3-07 slice 2: then the providers the runs of this launcher use are asked how wide the wave may be; the
        # nodes past that width are skipped below, like the capacity waits (READY, unprepared, offered again).
        mark = len(steps_)
        allowed = self._backpressure_admission(state, asked, allowed, round_, steps_)
        cut_from(mark, "BACKPRESSURE_WAIT", waverecovery.CutReason.BACKPRESSURE)
        # Every hold taken below -- by `prepare` for a new run, by `take_hold` for a resumed one --
        # is the launcher's until the executor has the node. Whatever ends this block other than
        # "the wave goes on" (a halt, an exception out of `prepare` / `evaluate` / `_block_node`, an
        # interrupt) gives back the holds **taken in this round**; a node left out of the wave gives
        # its own back. A hold the launcher kept from an earlier launch (the run was IN_FLIGHT: another
        # process executes it) is not this round's and is never released here -- `prepared_hold` read
        # before the node is touched is what tells the two apart.
        prepared: list[tuple[TaskNode, str | None]] = []
        proceed = False
        try:
            for node in plan.selected[: self.width]:
                is_head = node is head
                if not is_head and launcher.action_class(node) is ActionClass.EXTERNAL:
                    cut(node.id, ParallelismDegradationReason.EXTERNAL,
                        "carries an irreversible external action: not decided here, the head of a later round")
                    continue                  # not decided here; it is the head of a later round
                if allowed is not None and node.id not in allowed:
                    continue                  # waits for capacity: not prepared, no hold, not blocked, READY
                # before `prepare`: it can take a hold and then raise
                prepared.append((node, launcher.prepared_hold(node)))
                try:
                    obstacle = launcher.prepare(node)
                except BudgetExhausted as empty:
                    state = self._block_node(state, node, str(empty))
                    why = f"node {node.id} has no budget left: {empty}"
                    steps_.append(Step(round_, HaltClass.BUDGET_EXHAUSTED, node.id, why))
                    cut(node.id, ParallelismDegradationReason.BUDGET, why)
                    if is_head:
                        self._record_round_cuts(cuts, ready_total, width_cut, round_, steps_)
                        return Result(HaltClass.BUDGET_EXHAUSTED, why)
                    continue                  # a sibling is blocked alone; the others still run
                if obstacle:
                    state = self._block_node(state, node, obstacle)
                    why = f"node {node.id} cannot be made runnable: {obstacle}"
                    steps_.append(Step(round_, HaltClass.BLOCKED_DEPENDENCY, node.id, why))
                    cut(node.id, waverecovery.CutReason.BLOCKED, why)
                    if is_head:
                        self._record_round_cuts(cuts, ready_total, width_cut, round_, steps_)
                        return Result(HaltClass.BLOCKED_DEPENDENCY, why)
                    continue
                in_advance = launcher.evaluate(node)
                if in_advance.verdict is RunVerdict.IN_FLIGHT:
                    why = (f"node {node.id} is READY but a live process still executes its "
                           f"run ({in_advance.detail}); dispatching again would duplicate it")
                    cut(node.id, waverecovery.CutReason.IN_FLIGHT, why)
                    if is_head:
                        steps_.append(Step(round_, HaltClass.RUN_IN_FLIGHT, node.id, why))
                        self._record_round_cuts(cuts, ready_total, width_cut, round_, steps_)
                        return Result(HaltClass.RUN_IN_FLIGHT, why)
                    steps_.append(Step(round_, "WAVE_SKIPPED", node.id, why))
                    continue
                if in_advance.verdict is RunVerdict.ACCEPTED:
                    # Settled alone, exactly as the sequential round does; the nodes prepared
                    # before it give their holds back and the next round plans again.
                    steps_.append(Step(round_, "ALREADY_ACCEPTED", node.id, in_advance.detail))
                    self._record_round_cuts(cuts, ready_total, width_cut, round_, steps_)
                    return self._settle(state, node.id, in_advance, round_, steps_)
                # A run that already existed took no hold in `prepare`: it takes one now, so no
                # node is admitted to a budgeted wave without a share held for it. A node the
                # budget cannot hold for is left out of this wave (it stays READY) -- or ends
                # the round when it is the head, as a new run does at `prepare`.
                try:
                    launcher.take_hold(node)
                except BudgetExhausted as empty:
                    why = f"node {node.id} has no budget to hold: {empty}"
                    cut(node.id, ParallelismDegradationReason.BUDGET, why)
                    if is_head:
                        steps_.append(Step(round_, HaltClass.BUDGET_EXHAUSTED, node.id, why))
                        self._record_round_cuts(cuts, ready_total, width_cut, round_, steps_)
                        return Result(HaltClass.BUDGET_EXHAUSTED, why)
                    steps_.append(Step(round_, "WAVE_SKIPPED", node.id, why))
                    continue
                wave.append(node)

            if not wave:
                self._record_round_cuts(cuts, ready_total, width_cut, round_, steps_)
                return state
            proceed = True
        finally:
            self._release_all(
                [n for n, before in prepared
                 if (not proceed or all(n is not w for w in wave))
                 and self._taken_this_round(launcher, n, before)],
                launcher, "the wave was not run for this node")

        budget_ceiling = getattr(launcher, "dispatch_budget", None)
        budget = (None if budget_ceiling is None else WaveBudget(
            BudgetReservations(launcher.root), budget_ceiling, launcher.spent_by_run, 1,
            adopt=True))
        waves_dir = self.store.dir / "waves"
        wave_id = f"w{state.write_seq}-{round_}-{secrets.token_hex(2)}"
        running: dict[str, TaskNode] = {}
        persisted: list[ProjectState] = []

        def make_running(admitted: tuple[TaskNode, ...]) -> None:
            for admitted_node in admitted:
                member = state.node(admitted_node.id)
                assert member is not None
                # what `prepare` set on the node object, in case a sibling's block was
                # persisted since and replaced the state this node belongs to
                member.branch = admitted_node.branch or member.branch
                member.run_id = admitted_node.run_id or member.run_id
                member.spec_path = admitted_node.spec_path or member.spec_path
                member.lifecycle = Lifecycle.RUNNING
                start_dispatch(state, member.id)
                member.accepted_before = launcher.accepted_baseline(member)
            fresh = self._persist(state)
            persisted.append(fresh)
            for admitted_node in admitted:
                member = fresh.node(admitted_node.id)
                assert member is not None
                running[member.id] = member
            self._authorise("dispatch of wave " + wave_id)

        def launch(node: TaskNode, admission) -> RunOutcome:
            return launcher.launch(running[node.id])

        try:
            waves_dir.mkdir(exist_ok=True)
            # P3-22 slice 3: the wave's outcome is written before the executor starts, naming the
            # holds adopted from the launcher and who the controller is: a crash at any later
            # point leaves a name for every hold. A wave whose holds cannot be named is not run.
            outcome_path = waverecovery.open_outcome(
                self.store.dir / waverecovery.OUTCOME_DIR, wave_id,
                session=getattr(self.authority, "session_id", None),
                epoch=getattr(self.authority, "epoch", None),
                holds=self._adopted_holds(wave), cuts=cuts)
            executor = ParallelWaveExecutor(
                waves_dir, width=self.width, budget=budget, lease_for=launcher.lease_for,
                depends_on=launcher.depends_on, action_class=launcher.action_class)
            result = executor.run_wave(
                wave_id, FrontierPlan(tuple(wave), plan.deferred), launch,
                on_admitted=make_running)
            note = ""
        except WaveNotSettled as exc:
            result, note = exc.result, f" (the final wave record could not be written: {exc})"
        except StaleWrite as exc:
            # The executor gave back its leases; the holds adopted from the launcher are the
            # launcher's to end, here, whatever stopped the wave -- and `StaleMaster` is a
            # `StaleWrite`, so it is told apart only by being re-raised.
            self._release_all(wave, launcher, f"the wave stopped: {type(exc).__name__}")
            if isinstance(exc, StaleMaster):
                raise
            why = f"another orchestrator advanced this project: {exc}"
            steps_.append(Step(round_, HaltClass.AMBIGUOUS, None, why))
            return Result(HaltClass.AMBIGUOUS, why)
        except (WaveError, OSError, waverecovery.OutcomeError) as exc:
            # Refused whole, before any node ran. RUNNING may already be persisted (a raise
            # after the replace): the next drive evaluates that node as it does for a
            # controller that died between the write and the dispatch.
            return self._give_back_prepared(
                wave, launcher, state, f"the wave could not be run: {type(exc).__name__}: {exc}"[:300])
        except BaseException as exc:                    # KeyboardInterrupt, SystemExit, a bug
            self._release_all(wave, launcher, f"the wave stopped: {type(exc).__name__}")
            raise

        if persisted:
            state = persisted[-1]
        record = result.record
        self._close_wave_outcome(outcome_path, record, round_, steps_,
                                 ready=ready_total, extra_reasons=width_cut)
        refused = [n for n in record.nodes if n.state is NodeState.REFUSED]
        steps_.append(Step(
            round_, "WAVE", wave_id,
            f"width {self.width}: launched {[n.node_id for n in record.nodes if n.state is not NodeState.REFUSED]}"
            f"; refused {[(n.node_id, n.refusal) for n in refused]}{note}"))
        for node in wave:
            row = record.node(node.id)
            if row is not None and row.state is NodeState.REFUSED:
                launcher.release_prepared_hold(node, f"the wave refused the node: {row.refusal}")
        self._report_wave_end(launcher, record, waves_dir / f"wave-{wave_id}.json", round_, steps_)
        admitted = [n for n in record.nodes if n.state is not NodeState.REFUSED]
        if not admitted:
            why = ("the wave admitted none of its nodes: "
                   + "; ".join(f"{n.node_id}: {n.refusal} ({n.detail})" for n in refused))[:400]
            steps_.append(Step(round_, HaltClass.BLOCKED_DEPENDENCY, None, why))
            return Result(HaltClass.BLOCKED_DEPENDENCY, why)

        first_halt: Result | None = None
        for row in admitted:
            outcome = result.outcomes.get(row.node_id)
            if row.state is NodeState.ERRORED or outcome is None:
                # The dispatch raised: the node stays RUNNING with its activity open, and the
                # next drive evaluates it, as for a controller that died mid-dispatch.
                why = f"node {row.node_id}: the dispatch raised: {row.detail}"
                steps_.append(Step(round_, HaltClass.AMBIGUOUS, row.node_id, why))
                first_halt = first_halt or Result(HaltClass.AMBIGUOUS, why)
                continue
            if outcome.verdict is RunVerdict.ACCEPTED and not row.usable:
                # Accepted, and the worktree was written by someone else while the run ran:
                # it is not merged on this evidence.
                task = state.node(row.node_id)
                assert task is not None
                close_dispatch(state, row.node_id, EventKind.FAILED,
                               failure="accepted, but the worktree's lease was not held at the end"[:300])
                state = self._block_node(
                    state, task, "accepted, but the worktree was written by another holder while "
                                 "the run executed; not merged")
                why = f"node {row.node_id}: accepted but its worktree lease was not held at the end"
                steps_.append(Step(round_, HaltClass.AMBIGUOUS, row.node_id, why))
                first_halt = first_halt or Result(HaltClass.AMBIGUOUS, why)
                continue
            settled = self._settle(state, row.node_id, outcome, round_, steps_)
            if isinstance(settled, Result):
                first_halt = first_halt or settled
                state = self.store.read_state()
            else:
                state = settled
        return first_halt if first_halt is not None else state

    @staticmethod
    def _report_wave_end(launcher, record, path, round_: int, steps_: list[Step]) -> None:
        """What a finished wave says about itself, and the second chance of its holds.

        `WAVE_DEGRADED` (node = `<wave id>:<reasons>`, so the reasons reach the
        `steps` of `hoh project drive`) when the wave admitted fewer nodes than
        the plan was made from; the reasons are those the persisted record
        yields (`WaveRecord.degradation`). Then, with a shared budget, the wave's
        record is read back and every hold it names that is still open is ended
        by its id (`settle_wave`): the launcher's `launch` ends its own hold, and
        one that it could not end (a busy ledger) would otherwise stay open for a
        dispatch that is over. A node whose launch came back IN_FLIGHT is skipped
        there -- another process still executes it. `WAVE_SETTLED` is reported
        only when a hold was ended, so a wave in which every launch ended its own
        adds no step. Nothing here halts the loop: a refusal is a note.
        """
        degraded = record.degradation
        if degraded is not None:
            steps_.append(Step(
                round_, "WAVE_DEGRADED",
                f"{record.wave_id}:{','.join(r.value for r in degraded.reasons)}",
                f"{degraded.admitted} of {degraded.ready} ready nodes ran"))
        ProjectController._settle_capacity(launcher, record, round_, steps_)
        if getattr(launcher, "dispatch_budget", None) is None or getattr(launcher, "dry_run", False):
            return
        try:
            settlement = settle_wave(path, BudgetReservations(launcher.root), launcher.spent_by_run)
        except Exception as exc:                          # noqa: BLE001 - a note, never a halt
            steps_.append(Step(round_, "WAVE_SETTLE_FAILED", record.wave_id,
                               f"{type(exc).__name__}: {exc}"[:300]))
            return
        ended = [n.node_id for n in settlement.nodes if n.state == "ENDED"]
        if ended:
            steps_.append(Step(
                round_, "WAVE_SETTLED", record.wave_id,
                f"ended the hold of {ended}, which the launch could not end"))

    @staticmethod
    def _settle_capacity(launcher, record, round_: int, steps_: list[Step]) -> None:
        """P3-04 (C4b): the capacity the gate admitted this controller's wave against is given back when the wave
        ends, whatever its outcome (settled, partial, failed, halted: the executor has returned and no run of the
        wave is left to start). Every open row of this controller -- its `(pid, thread)` -- is ended `SETTLED`, **except**
        those of a node whose launch came back IN_FLIGHT (another process still executes the run) or raised (the
        process may be alive): they stay open until the controller's next ask, its death or the ttl. A
        second call finds none and writes nothing, and a host with no capacity ledger gets none (nothing is created).
        A ledger that cannot be read or written is **not** a silent release: the rows stay as they are, the step
        `WAVE_CAPACITY_UNSETTLED` says why, and the next ask of the gate refuses on the same ledger. Never a halt."""
        root = getattr(launcher, "root", None)
        if root is None:
            return
        wave_id = record.wave_id
        maybe_alive = [n.node_id for n in record.nodes
                       if n.state is NodeState.ERRORED or n.verdict == RunVerdict.IN_FLIGHT.value]
        try:
            CapacityReservations(root).settle_owned(f"wave {wave_id} ended: its capacity is given back",
                                                    keep=maybe_alive)
        except Exception as exc:                          # noqa: BLE001 - visible, never a halt
            steps_.append(Step(round_, "WAVE_CAPACITY_UNSETTLED", wave_id,
                               f"{type(exc).__name__}: {exc}"[:300]))

    def _adopted_holds(self, wave: list[TaskNode]) -> tuple:
        """The open hold of each node's run, as the executor will adopt it (`WaveBudget.adopt`).

        Nothing with no shared budget. A node whose run holds none, or more than one, is not
        named (the executor admits the first without a hold and refuses the second). A ledger
        that cannot be read is `OutcomeError`: a wave whose holds cannot be named is not run,
        because a crash would leave them with no name on disk."""
        launcher = self.launcher
        if getattr(launcher, "dispatch_budget", None) is None or getattr(launcher, "dry_run", False):
            return ()
        try:
            open_ = BudgetReservations(launcher.root).held()
        except Exception as exc:                          # noqa: BLE001 - fail closed
            raise waverecovery.OutcomeError(
                f"the holds of the wave cannot be named: {type(exc).__name__}: {exc}"[:300]) from exc
        named = []
        for node in wave:
            run_id = node.run_id or node.id
            mine = [r for r in open_ if r.run_id == run_id]
            if len(mine) == 1:
                named.append(waverecovery.AdoptedHold(
                    node.id, run_id, mine[0].reservation_id, mine[0].amount))
        return tuple(named)

    @staticmethod
    def _close_wave_outcome(path, record, round_: int, steps_: list[Step], *,
                            ready: int | None = None, extra_reasons=()) -> None:
        """Closes the wave's outcome with the round's degradation reason: the executor's own
        (`WaveRecord.degradation`), the controller's cuts the outcome was opened with and the
        width. A failure is a note, never a halt: the outcome stays OPEN and the next drive's
        recovery bounds it."""
        try:
            waverecovery.close_outcome(path, record, ready=ready, extra_reasons=extra_reasons)
        except Exception as exc:                          # noqa: BLE001 - a note, never a halt
            steps_.append(Step(round_, "WAVE_OUTCOME_UNWRITTEN", record.wave_id,
                               f"{type(exc).__name__}: {exc}"[:300]))

    def _record_round_cuts(self, cuts: list, ready: int, extra_reasons, round_: int,
                           steps_: list[Step]) -> None:
        """P3-24 slice 4: a round that launches no wave (it halted before the executor, or every
        node was cut) still says why in `<project>/wave-outcomes/wave-latest-cuts.json`. One
        file, replaced only when its content changes. A failure is a note, never a halt."""
        if not cuts:
            return
        try:
            waverecovery.record_round(
                self.store.dir / waverecovery.OUTCOME_DIR,
                session=getattr(self.authority, "session_id", None),
                epoch=getattr(self.authority, "epoch", None),
                ready=ready, cuts=cuts, extra_reasons=extra_reasons)
        except Exception as exc:                          # noqa: BLE001 - a note, never a halt
            steps_.append(Step(round_, "WAVE_OUTCOME_UNWRITTEN", waverecovery.ROUND_ID,
                               f"{type(exc).__name__}: {exc}"[:300]))

    _RECOVERY_STEPS = {
        "RECOVERED": "WAVE_RECOVERED", "SETTLED": "WAVE_SETTLED", "LEFT": "WAVE_RECOVERY_LEFT",
        "ABANDONED": "WAVE_RECOVERY_ABANDONED", "UNREADABLE": "WAVE_RECOVERY_UNREADABLE",
        "UNBOUNDED": "WAVE_RECOVERY_UNBOUNDED",
        "CAPACITY_SETTLED": "WAVE_CAPACITY_SETTLED", "CAPACITY_UNSETTLED": "WAVE_CAPACITY_UNSETTLED",
    }

    def _recover_open_waves(self, steps_: list[Step]) -> str | None:
        """P3-22 slice 3: ends the holds a dead controller left, once per drive.

        Looks only at OPEN outcomes (`<project>/wave-outcomes/`); a project with none writes
        no state and probes nothing. The master check, the claim (the persisted state read
        and written back through `_persist`: master stamp and `write_seq`
        compare-and-swap) and the idleness proof (the launcher's own `evaluate`: a live
        process or a busy run lock is IN_FLIGHT, an unreadable state UNDETERMINED) are the
        preconditions, in `waverecovery.recover_open`. Each outcome of the pass is a step
        (`kind:wave id`). Returns None, or -- a path that cannot be bounded -- the reason of
        the AMBIGUOUS halt, which starts `WAVE_RECOVERY_UNBOUNDED:`. `StaleMaster` propagates."""
        launcher = self.launcher
        directory = self.store.dir / waverecovery.OUTCOME_DIR
        if not directory.is_dir():
            return None
        budgeted = (getattr(launcher, "dispatch_budget", None) is not None
                    and not getattr(launcher, "dry_run", False)
                    and getattr(launcher, "root", None) is not None)

        def claim() -> ProjectState:
            return self._persist(self.store.read_state())

        def probe(node: TaskNode) -> str | None:
            seen = launcher.evaluate(node)
            verdict = getattr(seen, "verdict", None)
            if not isinstance(verdict, RunVerdict):
                return f"the run's state gave no verdict ({type(seen).__name__})"
            if verdict in (RunVerdict.IN_FLIGHT, RunVerdict.UNDETERMINED):
                return f"its run is {verdict.value}: {getattr(seen, 'detail', '')}"[:300]
            return None

        capacity_root = getattr(launcher, "root", None)

        def capacity(outcome, reason: str) -> int:
            """The gone controller's open capacity rows (its pid and process identity; in this very process, this
            thread's only -- another thread of it may be a live controller of another project)."""
            if capacity_root is None:
                return 0
            if outcome.pid != os.getpid() and outcome.pid_start is None:
                return 0            # a foreign pid whose identity was not recorded cannot be told from a reused one: left
            return CapacityReservations(capacity_root).settle_owned(
                reason, pid=outcome.pid, thread=threading.get_ident() if outcome.pid == os.getpid() else None,
                identity=outcome.pid_start)

        report = waverecovery.recover_open(
            directory,
            authorise=lambda: self._authorise("recovery of a wave's holds"),
            session=getattr(self.authority, "session_id", None),
            epoch=getattr(self.authority, "epoch", None),
            claim=claim, probe=probe,
            reservations=BudgetReservations(launcher.root) if budgeted else None,
            spent=launcher.spent_by_run if budgeted else None,
            waves_dir=self.store.dir / "waves", capacity=capacity)
        for event in report.events:
            steps_.append(Step(0, self._RECOVERY_STEPS[event.kind], event.wave_id, event.detail))
        if report.unbounded is not None:
            event = report.unbounded
            steps_.append(Step(0, self._RECOVERY_STEPS["UNBOUNDED"], event.wave_id, event.detail))
            return (f"WAVE_RECOVERY_UNBOUNDED: wave {event.wave_id}: {event.detail} -- a recovery "
                    "that cannot be bounded is not attempted; this needs a person")[:600]
        return None

    @staticmethod
    def _taken_this_round(launcher, node: TaskNode, before: str | None) -> bool:
        """The launcher holds a share for the node's run that it did not hold when
        the round began. Fail closed: an error is "not this round's", so nothing
        that may belong to a running process is ended on a guess."""
        try:
            now = launcher.prepared_hold(node)
        except Exception:                              # noqa: BLE001
            return False
        return now is not None and now != before

    def _capacity_admission(self, asked: list[TaskNode], round_: int, steps_: list[Step],
                            state: ProjectState, ready: int) -> "set[str] | Result | None":
        """P3-19 slice 2: the wave's nodes go through the resource scheduler before any is prepared.

        None -- nothing is configured (no capacity registry at the state root the operator's `hoh capacity`
        commands use): every node is asked for as before, and the opt-out is said once. A set -- the ids that may
        be prepared and launched; each other node waits (a `CAPACITY_WAIT` step with its reason), stays READY and
        is asked again next round. A `Result` (`CAPACITY_WAIT`) -- none may start now, or capacity cannot be
        known: nothing was prepared, nothing is blocked, the round ends.

        The state root is resolved by `cli.resolve_storage_root`, the resolver of `hoh capacity set` and
        `check`, from the launcher's own configured root (`--storage-root`'s place), else `$HOH_STATE_ROOT`,
        else `$XDG_STATE_HOME/veriharness`: one rule, so a registry the operator declared is the one asked.

        P3-39: with a `storage-policy.json` in that state root the nodes also want the bytes and inodes of a run
        (`capacitycheck.storage_reader`, over the launcher's own admission factory when it has one), and the admitted
        nodes hold them in the capacity ledger with their sessions.

        P3-17 slice 3 / P3-19 slice 3 / P3-16 slice 4: the admission is **recorded** (`<project>/capacity/capacity-<id>.json`,
        create-only, bound by its own digest, written before the nodes are held; a record that cannot be written admits
        nobody, so the round is a `CAPACITY_WAIT`); the nodes that may take the operator's reserve are the **critical** ones
        -- a repair node (`repair_of`) and a node whose run already started (`run_id`); the providers the launcher's runs
        use (`backpressure.providers_of`) are the ones whose declared session capacity a node wants one of, so a provider
        that is limited or unreadable admits none; and the capacity source's snapshot is what the decision is made on.
        `ready` is how many nodes were ready, quoted in the record beside the controller's width."""
        from . import capacitycheck, capacityrecord
        from .cli import resolve_storage_root
        from .contracts import utcnow

        launcher = self.launcher
        try:
            configured = getattr(launcher, "storage_root", None)
            state_root = resolve_storage_root(str(configured) if configured else None)
            runs_root = launcher.root
        except Exception as exc:                          # noqa: BLE001 - fail closed, never into the loop
            why = f"capacity cannot be judged ({type(exc).__name__}): the state or runs root is not known"
            for n in asked:
                steps_.append(Step(round_, "CAPACITY_WAIT", n.id, why))
            return Result(HaltClass.CAPACITY_WAIT, why)
        gate = capacitycheck.admit_wave(
            [n.id for n in asked], runs_root=runs_root, state_root=state_root, now=utcnow,
            policy_=self.capacity_policy,
            storage=capacitycheck.storage_reader(
                state_root, factory=getattr(launcher, "_storage_admission_factory", None)),
            critical=[n.id for n in asked if n.repair_of is not None or n.run_id is not None],
            providers=backpressure.providers_of(launcher),
            record_dir=self.store.dir / capacityrecord.DIRNAME,
            decision_id=f"c{state.write_seq}-{round_}-{secrets.token_hex(3)}",
            width=self.width, ready=ready)
        if not gate.configured:
            if not self._capacity_opt_out_said:
                self._capacity_opt_out_said = True
                steps_.append(Step(round_, "CAPACITY_OPT_OUT", None, gate.why))
            return None
        if gate.record is not None:
            steps_.append(Step(round_, "ADMISSION_RECORD", None, f"{capacityrecord.DIRNAME}/{gate.record}"))
        for node_id, why in gate.waits:
            steps_.append(Step(round_, "CAPACITY_WAIT", node_id, why))
        if not gate.admitted:
            return Result(HaltClass.CAPACITY_WAIT, gate.why)
        steps_.append(Step(round_, "CAPACITY", None, gate.why))
        return set(gate.admitted)

    def _fenced_write(self, write) -> None:
        """P3-25/26: runs `write` (the replacement of the adaptation's state file) as an authoritative act: under a
        master authority, inside the program lock and after the epoch check, so a superseded master (`StaleMaster`)
        writes nothing; without one, as it is (the drive already holds the project lock)."""
        if self.authority is None:
            write()
            return
        with write_lock(self.store):
            self._authorise("the write of the parallelism state")
            write()

    def _backpressure_admission(self, state: ProjectState, asked: list[TaskNode],
                                allowed: "set[str] | None", round_: int,
                                steps_: list[Step]) -> "set[str] | None":
        """P3-07 slice 2: the nodes the wave would prepare (those of `asked` the capacity gate admits) are cut to
        the width `backpressure.admit_wave` allows for the providers of this launcher's runs.

        Returns `allowed` unchanged when nothing is cut, else the ids that may be prepared. **It never halts
        the project and never leaves a round empty**: the width is at least 1 and the first node offered always
        passes, so a narrowed round is the sequential round; a node it cuts is a `BACKPRESSURE_WAIT` step, stays
        READY with no hold, and is offered again next round (the rounds are `max_rounds`-bounded and each one
        runs at least its head, so the frontier drains). A pressure that is read is a `BACKPRESSURE` step; a
        gate that could not judge is `BACKPRESSURE_UNJUDGED` and is the floor. The decision is on disk
        (`<project>/backpressure/backpressure-<id>.json`) before it is used."""
        from . import parallelmode

        candidates = [n.id for n in asked if allowed is None or n.id in allowed]
        if not candidates:
            return allowed
        # P3-25/26: the project's declared parallelism mode offers the gate at most its profile's width of the
        # candidates (a prefix); without a declaration the gate is asked about all of them, as before.
        modal = parallelmode.admit_wave(
            candidates, project_dir=self.store.dir,
            gate=lambda offered, decision_id: backpressure.admit_wave(
                offered, launcher=self.launcher, record_dir=self.store.dir / "backpressure",
                decision_id=decision_id),
            fenced=self._fenced_write,
            decision_id=f"b{state.write_seq}-{round_}-{secrets.token_hex(2)}",
            active=sum(1 for n in state.nodes if n.lifecycle is Lifecycle.RUNNING))
        for kind, node_id, text in modal.notes:
            steps_.append(Step(round_, kind, node_id, text))
        gate = modal.gate
        if gate is not None and not gate.judged:
            steps_.append(Step(round_, "BACKPRESSURE_UNJUDGED", None, gate.why))
        elif gate is not None and gate.decision is not None and gate.decision.reason is not backpressure.Reason.NONE:
            steps_.append(Step(round_, "BACKPRESSURE", None, gate.why))
        for node_id, why in (gate.waits if gate is not None else ()):
            steps_.append(Step(round_, "BACKPRESSURE_WAIT", node_id, why))
        keep = list(modal.admitted) or candidates[:1]
        return allowed if keep == candidates else set(keep)

    @staticmethod
    def _release_all(wave: list[TaskNode], launcher, why: str) -> None:
        """Ends the holds `prepare`/`take_hold` took for the wave's nodes; never
        raises (a hold that cannot be ended stays open: smaller, never larger)."""
        for node in wave:
            try:
                launcher.release_prepared_hold(node, why[:200])
            except Exception:                          # noqa: BLE001 - the original error stands
                pass

    def _give_back_prepared(self, wave: list[TaskNode], launcher, state: ProjectState,
                            why: str) -> "ProjectState | Result":
        for node in wave:
            launcher.release_prepared_hold(node, why[:200])
        return Result(HaltClass.BLOCKED_DEPENDENCY, why)

    def _settle_storage_wait(
        self, state: ProjectState, task, node_id: str, outcome_: RunOutcome,
        round_: int, steps_: list[Step],
    ) -> "Result":
        """A node whose run parked on storage pressure (STORAGE-B3).

        Same discipline as the provider wait, its own class: under the program
        lock and the authority check, a STORAGE_RECHECK timer is armed
        **first**, any superseded STORAGE_RECHECK timer of the node is then
        disarmed by its id, and only then is the node persisted BLOCKED with
        ``blocked_kind = "storage_pressure"`` -- never ``usage_limit``, never
        a PROVIDER_RETRY timer. If the timer cannot be armed the node is not
        parked and the halt is STORAGE_UNAVAILABLE (NEEDS_YOU).
        """
        from datetime import UTC, datetime

        from .storage_park import ARM_FAILED, ParkPolicy, STORAGE_CLASS
        from .timers import in_seconds

        due = outcome_.retry_after
        if due:
            try:
                check_time(due)
            except ValueError:
                due = None
        if not due:
            due = in_seconds(ParkPolicy().recheck_interval_s, now=datetime.now(UTC))
        try:
            with (write_lock(self.store) if self.authority is not None
                    else nullcontext()):
                self._authorise(f"arming the storage recheck timer of node {node_id}")
                timers = TimerStore(self.store.dir)
                superseded = [old.timer_id for old in timers.armed()
                              if old.node_id == node_id
                              and old.kind is TimerKind.STORAGE_RECHECK]
                new = timers.arm_new(
                    kind=TimerKind.STORAGE_RECHECK, due_at=due,
                    reason=f"storage wait for node {node_id}: {outcome_.detail}"[:300],
                    program_id=state.project_id,
                    program_epoch=state.program_generation,
                    session_epoch=(self.authority.epoch if self.authority is not None
                                   else state.master_epoch) or 0,
                    run_id=node_timer_key(node_id), node_id=node_id)
                for timer_id in superseded:
                    timers.disarm(timer_id, f"superseded by storage recheck {new.timer_id}")
        except TimerStoreError as exc:
            task.lifecycle = Lifecycle.BLOCKED
            task.blocked_kind = None
            task.retry_after = None
            task.note = f"{outcome_.detail} | {ARM_FAILED} ({exc}); a person resumes this node"
            self._persist(state)
            why = f"node {node_id}: {ARM_FAILED} ({exc}), NEEDS_YOU"
            steps_.append(Step(round_, HaltClass.STORAGE_UNAVAILABLE, node_id, why))
            return Result(HaltClass.STORAGE_UNAVAILABLE, why)
        task.lifecycle = Lifecycle.BLOCKED
        task.blocked_kind = STORAGE_CLASS
        task.retry_after = due
        task.note = outcome_.detail
        self._persist(state)
        why = (f"node {node_id} waits on storage pressure (recheck {due}, "
               f"`hoh resume-storage`): {outcome_.detail}")
        steps_.append(Step(round_, HaltClass.STORAGE_WAIT, node_id, why))
        return Result(HaltClass.STORAGE_WAIT, why)

    def _settle(
        self, state: ProjectState, node_id: str, outcome_: RunOutcome,
        round_: int, steps_: list[Step],
    ) -> "ProjectState | Result":
        """Turns one run outcome into a persisted lifecycle transition.

        Shared by the main loop and the resume path on purpose. Two copies of
        this would be two chances for a resumed session to decide differently
        from the session it is resuming -- and "differently" here means merging
        twice or discarding verified work.
        """
        task = state.node(node_id)
        assert task is not None

        # The dispatch activity ends with the outcome, in the same write as the
        # lifecycle transition below. IN_FLIGHT is the one verdict that writes
        # nothing: the run is still executing, so its activity stays open.
        if outcome_.verdict is RunVerdict.ACCEPTED:
            close_dispatch(state, node_id, EventKind.SUCCEEDED,
                           result_digest=outcome_digest(candidate=outcome_.candidate,
                                                        tree=outcome_.candidate_tree))
        elif outcome_.verdict is not RunVerdict.IN_FLIGHT:
            close_dispatch(state, node_id, EventKind.FAILED,
                           failure=f"{outcome_.verdict.value}: {outcome_.detail}"[:300])

        if outcome_.verdict is RunVerdict.PROVIDER_UNAVAILABLE:
            task.lifecycle = Lifecycle.BLOCKED
            task.note = outcome_.detail
            task.blocked_kind = None
            task.retry_after = None
            if outcome_.retry_after:
                # DS3: a usage limit is a wait with a time, not a verdict. The
                # node records it and a durable timer is armed under the
                # programme generation, so whoever runs next -- this session,
                # a successor, the systemd timer -- can resume it without a
                # person, and a successor master resumes what this one armed.
                task.blocked_kind = "usage_limit"
                task.retry_after = check_time(outcome_.retry_after)
            if task.retry_after:
                # Armed *before* the state records the wait: a crash in between
                # leaves a timer for a node that is not blocked, which the
                # resume loop refuses harmlessly (RUN_NOT_PARKED). The other
                # order would leave a blocked node that nothing will wake.
                try:
                    with (write_lock(self.store) if self.authority is not None
                            else nullcontext()):
                        # Checked under the program lock: a successor cannot
                        # acquire between the check and the arm (review A, S1).
                        self._authorise(f"arming the retry timer of node {node_id}")
                        timers = TimerStore(self.store.dir)
                        for old in timers.armed():
                            if old.node_id == node_id and old.kind is TimerKind.PROVIDER_RETRY:
                                timers.disarm(old.timer_id, "superseded by a new provider wait")
                        timers.arm_new(
                            kind=TimerKind.PROVIDER_RETRY, due_at=task.retry_after,
                            reason=f"provider wait for node {node_id}: {outcome_.detail}"[:300],
                            program_id=state.project_id,
                            program_epoch=state.program_generation,
                            session_epoch=(self.authority.epoch if self.authority is not None
                                           else state.master_epoch) or 0,
                            run_id=node_timer_key(node_id), node_id=node_id)
                except TimerStoreError as exc:
                    # No timer, so no timed park: a parked state is never durable
                    # without its wake-up. The node keeps a human owner instead
                    # (review A3, F4).
                    task.blocked_kind = None
                    task.retry_after = None
                    task.note = (f"{outcome_.detail} | the retry timer could not be "
                                 f"armed ({exc}); a person resumes this node")
            state = self._persist(state)
            why = f"node {node_id}: {outcome_.detail or 'provider unavailable'}"
            steps_.append(Step(round_, HaltClass.BLOCKED_PROVIDER, node_id, why))
            return Result(HaltClass.BLOCKED_PROVIDER, why)

        if outcome_.verdict is RunVerdict.STORAGE_WAIT:
            return self._settle_storage_wait(state, task, node_id, outcome_, round_, steps_)

        if outcome_.verdict is RunVerdict.STORAGE_UNAVAILABLE:
            task.lifecycle = Lifecycle.BLOCKED
            task.blocked_kind = None
            task.retry_after = None
            task.note = outcome_.detail
            self._persist(state)
            why = f"node {node_id}: storage admission unavailable, NEEDS_YOU: {outcome_.detail}"
            steps_.append(Step(round_, HaltClass.STORAGE_UNAVAILABLE, node_id, why))
            return Result(HaltClass.STORAGE_UNAVAILABLE, why)

        if outcome_.verdict is RunVerdict.IN_FLIGHT:
            # The run this dispatch meant to start is already being executed
            # by another live process. The node stays RUNNING, as persisted
            # before the dispatch, and nothing is written: the resume loop
            # waits for that run and settles it when it has finished (review
            # A7 -- the unclassified branch below made it a BLOCKED node that
            # nothing resumed).
            why = f"node {node_id}: its run is being executed elsewhere ({outcome_.detail})"
            steps_.append(Step(round_, HaltClass.RUN_IN_FLIGHT, node_id, why))
            return Result(HaltClass.RUN_IN_FLIGHT, why)

        if outcome_.verdict is RunVerdict.NOT_STARTED:
            task.lifecycle = Lifecycle.READY
            steps_.append(Step(round_, "RESET_TO_READY", node_id, outcome_.detail))
            return self._persist(state)

        if outcome_.verdict is RunVerdict.NEEDS_APPROVAL:
            task.lifecycle = Lifecycle.BLOCKED
            task.blocked_kind = None
            task.retry_after = None
            task.note = outcome_.detail
            self._persist(state)
            why = f"node {node_id} is waiting for a human approval: {outcome_.detail}"
            steps_.append(Step(round_, HaltClass.BLOCKED_EXTERNAL, node_id, why))
            return Result(HaltClass.BLOCKED_EXTERNAL, why)

        if outcome_.verdict is RunVerdict.BUDGET_EXHAUSTED:
            task.lifecycle = Lifecycle.BLOCKED
            task.blocked_kind = None
            task.retry_after = None
            task.note = outcome_.detail
            self._persist(state)
            why = f"node {node_id} spent its budget: {outcome_.detail}"
            steps_.append(Step(round_, HaltClass.BUDGET_EXHAUSTED, node_id, why))
            return Result(HaltClass.BUDGET_EXHAUSTED, why)

        if outcome_.verdict is RunVerdict.NOT_RUN:
            task.lifecycle = Lifecycle.READY
            self._persist(state)
            why = f"dry run: node {node_id} was not dispatched, so nothing was established"
            steps_.append(Step(round_, HaltClass.NOT_RUN, node_id, why))
            return Result(HaltClass.NOT_RUN, why)

        if outcome_.verdict is RunVerdict.ACCEPTED:
            with (write_lock(self.store) if self.authority is not None
                  else nullcontext()):
                # The check and the merge in one critical section: a successor
                # acquires only after the merge returned, and then finds it
                # already contained (review A, S1).
                self._authorise(f"merge of node {node_id}")
                m = self.launcher.merge(task, outcome_)
            if not isinstance(m, MergeResult):        # a bool, from an older launcher
                m = MergeResult(landed=bool(m), failure=None if m else MergeFailure.UNKNOWN)
            if not m.landed:
                # Accepted, and the candidate did not land. Nothing here is
                # resolved automatically -- re-merging risks applying a
                # candidate twice, and abandoning discards verified work. What
                # *is* decided here is which of those states it is, because git
                # usually said.
                class_name = {
                    MergeFailure.CONFLICT: HaltClass.MERGE_CONFLICT,
                    MergeFailure.OBSTRUCTED: HaltClass.MERGE_OBSTRUCTED,
                    MergeFailure.CANDIDATE_MISMATCH: HaltClass.CANDIDATE_MISMATCH,
                }.get(m.failure, HaltClass.AMBIGUOUS)
                was = {
                    HaltClass.MERGE_CONFLICT: "git reported a content conflict",
                    HaltClass.MERGE_OBSTRUCTED: "the working tree was in the way",
                    HaltClass.CANDIDATE_MISMATCH: "its branch does not carry the "
                                                  "accepted tree",
                }.get(class_name, "the merge failed and git did not say why")
                why = (
                    f"node {node_id} was accepted but its candidate did not land: "
                    f"{was}. {m.summary()}"
                    + (f" -- {m.detail}" if m.detail else "")
                )
                task.lifecycle = Lifecycle.BLOCKED
                task.blocked_kind = None
                task.retry_after = None
                task.note = why
                self._persist(state)
                steps_.append(Step(round_, class_name, node_id, why))
                return Result(class_name, why)
            task.lifecycle = Lifecycle.MERGED
            # The receipt binds what was accepted to what landed: the accepted
            # tree, the base the candidate named, and the exact commit that was
            # integrated. "landed on <base>" was what this said before, which
            # for a dirty-tree candidate named a commit that did not contain
            # the accepted work at all (evidence audit, 2026-09-25).
            integrated = getattr(m, "integrated", "") or ""
            self._record(
                state, DecisionKind.MERGE_RELEASE,
                f"every criterion passed and commit "
                f"{integrated or outcome_.candidate or '(unnamed)'} was integrated",
                {"node": node_id, "candidate": outcome_.candidate,
                 "accepted_tree": outcome_.candidate_tree,
                 "integrated": integrated or None},
            )
            fresh_state = self._persist(state)
            steps_.append(Step(round_, "MERGED", node_id))
            return fresh_state

        if outcome_.verdict is RunVerdict.REJECTED:
            task.rejections += 1
            if task.rejections > MAX_REJECTIONS:
                task.lifecycle = Lifecycle.ABANDONED
                abandon_dispatch(state, node_id, f"{task.rejections} rejections without progress")
                self._record(
                    state, DecisionKind.ABANDON_NODE,
                    f"{task.rejections} rejections without progress",
                    {"node": node_id},
                )
                steps_.append(Step(round_, "ABANDONED", node_id, f"{task.rejections} rejections"))
            else:
                task.lifecycle = Lifecycle.READY
                steps_.append(Step(round_, "REJECTED", node_id, f"attempt {task.rejections}"))
            return self._persist(state)

        # The detail is carried. It was dropped here and nowhere else, so the
        # one branch that by definition cannot explain itself was also the one
        # that threw away the launcher's explanation -- a benchmark cell halted
        # AMBIGUOUS and the project state said only "unclassifiable", with the
        # reason sitting unused in `ausgang.detail`.
        why = (
            f"node {node_id}: unclassifiable verdict "
            f"{outcome_.verdict.value}"
            + (f" -- {outcome_.detail}" if outcome_.detail else
               " (and the launcher gave no detail)")
        )
        task.lifecycle = Lifecycle.BLOCKED
        task.blocked_kind = None
        task.retry_after = None
        task.note = why
        self._persist(state)
        steps_.append(Step(round_, HaltClass.AMBIGUOUS, node_id, why))
        return Result(HaltClass.AMBIGUOUS, why)

    # -- global closure ---------------------------------------------------- #

    def _closure(
        self, state: ProjectState, round_: int, steps_: list[Step], reparaturen: int
    ) -> tuple[Result | None, ProjectState, int]:
        """One closure attempt. Returns a Result only when the loop must stop."""
        if not state.dag_terminal():
            # Nothing runnable and not terminal: something is blocked or
            # contingent, and the state does not say what to do next.
            blocked_ = [n.id for n in state.nodes if n.lifecycle is Lifecycle.BLOCKED]
            storage_ = [n.id for n in state.nodes if n.lifecycle is Lifecycle.BLOCKED
                        and n.blocked_kind == "storage_pressure"]
            if blocked_ and storage_ == blocked_:
                # Every block is a storage wait with its own recheck timer
                # (STORAGE-B3): a wait, never a halt that needs a person.
                why = ("no node is runnable; waiting on storage pressure: "
                       + ", ".join(storage_) + " (`hoh resume-storage` resumes them)")
                steps_.append(Step(round_, HaltClass.STORAGE_WAIT, detail=why))
                return Result(HaltClass.STORAGE_WAIT, why), state, reparaturen
            why = (
                "no node is runnable and the DAG is not terminal"
                + (f"; blocked: {', '.join(blocked_)}" if blocked_ else "")
            )
            steps_.append(Step(round_, HaltClass.BLOCKED_DEPENDENCY, detail=why))
            return Result(HaltClass.BLOCKED_DEPENDENCY, why), state, reparaturen

        subject_ = self.gates.subject()
        results = self.gates.run(subject_)

        if any(g.outcome is GateOutcome.NOT_RUN for g in results) and not any(
            g.outcome is GateOutcome.RED for g in results
        ):
            # A dry run, or gates that could not execute. Not a fixpoint, and
            # explicitly not green.
            why = (
                "global gates did not execute (NOT_RUN); a closure that checked "
                "nothing has established nothing"
            )
            state.gates.extend(results)
            state = self._persist(state)
            steps_.append(Step(round_, HaltClass.NOT_RUN, detail=why))
            return Result(HaltClass.NOT_RUN, why), state, reparaturen

        # Stamped with the pass they belong to, before the counter moves, so a
        # gate that stops running in a later pass cannot keep voting.
        next_ = state.closure_generation + 1
        for g in results:
            g.generation = next_
        state.gates.extend(results)
        state.measurement_head = subject_
        state.closure_generation = next_
        state = self._persist(state)

        if state.rc_closed():
            why = (
                f"DAG terminal, every global gate green at {subject_}, no outstanding "
                f"repair node; closure generation {state.closure_generation}"
            )
            self._record(state, DecisionKind.CLOSURE_VERDICT, why,
                         {"subject": subject_, "generation": state.closure_generation})
            state = self._persist(state)
            # DS4: a terminal project disarms every timer it armed, so nothing
            # it scheduled while alive can wake it -- or spend -- afterwards.
            # Under the program lock after a check, like every other write.
            try:
                with (write_lock(self.store) if self.authority is not None
                      else nullcontext()):
                    self._authorise("disarming the closed project's timers")
                    disarmed = TimerStore(self.store.dir).disarm_all(
                        f"project {state.project_id} closed: {why}"[:300])
            except TimerStoreError:
                disarmed = []
                steps_.append(Step(round_, "TIMERS_UNREADABLE",
                                   detail="the timer store could not be read to disarm"))
            if disarmed:
                steps_.append(Step(round_, "TIMERS_DISARMED",
                                   detail=f"{len(disarmed)} timer(s)"))
            steps_.append(Step(round_, HaltClass.CLOSED, detail=why))
            return Result(HaltClass.CLOSED, why), state, reparaturen

        red = [g for g in results if g.outcome is GateOutcome.RED]
        if not red:
            # Terminal, gates green, yet not closed: a repair node is still
            # outstanding. It will be picked up as READY on the next round.
            open_ = [n.id for n in state.nodes if n.repair_of and not n.settled]
            why = f"gates green but repair nodes outstanding: {', '.join(open_)}"
            steps_.append(Step(round_, "CLOSURE_PENDING", detail=why))
            return None, state, reparaturen

        if reparaturen >= self.max_repairs:
            why = (
                f"repair ceiling of {self.max_repairs} reached and the global gates are "
                f"still red: {', '.join(g.name for g in red)}"
            )
            steps_.append(Step(round_, HaltClass.ROUND_LIMIT, detail=why))
            return Result(HaltClass.ROUND_LIMIT, why), state, reparaturen

        reparaturen += 1
        predecessor_ = [n.id for n in state.nodes if n.lifecycle is Lifecycle.MERGED]
        task = TaskNode(
            id=f"repair-{state.closure_generation}-{reparaturen}",
            lifecycle=Lifecycle.READY,
            action_class=ActionClass.INTERNAL,
            dependencies=predecessor_[-1:],
            repair_of=predecessor_[-1] if predecessor_ else None,
            closure_generation=state.closure_generation,
            note="; ".join(f"{g.name}: {g.detail or 'red'}" for g in red),
        )
        state.nodes.append(task)
        self._record(
            state, DecisionKind.CREATE_REPAIR_NODE,
            f"global gates red at {subject_}: {', '.join(g.name for g in red)}",
            {"node": task.id, "gates": [g.name for g in red], "subject": subject_},
            evidence=[f"gate:{g.name}@{g.subject}" for g in red],
        )
        state = self._persist(state)
        steps_.append(Step(round_, "REPAIR_CREATED", task.id, task.note))
        return None, state, reparaturen
