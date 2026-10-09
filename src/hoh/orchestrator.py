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

import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

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
from .store import LockBusy, StaleWrite, StoreError
from .succession import MasterAuthority, StaleMaster
from .timers import TimerKind, TimerStore, TimerStoreError, check_time


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
    ) -> None:
        self.store = store
        self.launcher = launcher
        self.gates = gates
        self.max_rounds = max_rounds
        self.max_repairs = max_repairs
        #: The master authority this controller drives under, or None for the
        #: pre-P2 behaviour (fenced by `write_seq` and the project lock only).
        #: With one, every authoritative act -- a state write, a dispatch that
        #: spends, a merge that lands accepted work -- is preceded by a check
        #: that this session still holds its epoch, and a stale one halts
        #: SUPERSEDED having written nothing.
        self.authority = authority

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
                    return self._drive()
            if (self.store.dir / "epochs.json").is_file():
                # A governed project is driven only by a master. An ungoverned
                # controller re-reads the current epoch from disk and would pass
                # the store fence while a live master believes it is alone
                # (review A, P8).
                return Result(halt=HaltClass.SUPERSEDED, reason=(
                    f"project {self.store.project_id} is governed by a master epoch; "
                    "a controller without authority may not drive it"))
            with self.store.lock():
                return self._drive()
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

    def _drive(self) -> Result:
        steps_: list[Step] = []
        reparaturen = 0

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

        for round_ in range(1, self.max_rounds + 1):
            ready_ = state.ready()

            if not ready_:
                result, state, new_ = self._closure(state, round_, steps_, reparaturen)
                reparaturen = new_
                if result is not None:
                    result.steps = steps_
                    result.rounds = round_
                    result.repairs_created = reparaturen
                    return result
                continue

            task = ready_[0]

            if self.launcher.action_class(task) is ActionClass.EXTERNAL:
                why = (
                    f"node {task.id} carries an irreversible external action; "
                    "that decision is not delegated to the orchestrator"
                )
                steps_.append(Step(round_, HaltClass.BLOCKED_EXTERNAL, task.id, why))
                return Result(HaltClass.BLOCKED_EXTERNAL, why, steps_, round_, reparaturen)

            # Serialise only where a dependency is actually measured. A blanket
            # rule would be safe and would also throw away every parallel round.
            for other_ in ready_[1:]:
                if self.launcher.depends_on(task, other_):
                    steps_.append(
                        Step(round_, "SERIALISED", f"{task.id}<->{other_.id}",
                             "measured semantic dependency")
                    )

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

        why = f"{self.max_rounds} rounds without reaching a fixpoint"
        steps_.append(Step(self.max_rounds, HaltClass.ROUND_LIMIT, detail=why))
        return Result(HaltClass.ROUND_LIMIT, why, steps_, self.max_rounds, reparaturen)

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
