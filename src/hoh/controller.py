"""The HoH controller: drives the loop and decides on evidence acceptance.

Ownership split (handoff §2): HoH decides deterministically about workflow
transitions and evidence acceptance. firstmate remains the owner of worker
lifecycle, worktrees and delivery. A model delivers a plan or a QA finding --
it never replaces a technical approval check.

Dispatch is encapsulated behind `RoleDispatcher`. Per handoff §11, unit and
error tests may use fake adapters; A01/A02/A12 require real execution.

**Status:** so far the only implementation of this protocol is the test
suite's `FakeDispatcher`. An adapter that really starts firstmate crewmates in
Herdr does not exist yet -- `firstmate.py` and `herdr.py` are written, but not
imported by a single line. As long as that is the case, A01, A02 and A12 are
**not** demonstrated, and nothing here may present them as demonstrated.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from . import sandbox as sandbox_mod

from . import dispatchrecovery, failures, roles, spec_criteria, stages
from .contracts import (
    AcceptanceCheck,
    Budgets,
    Candidate,
    CheckVerdict,
    Condition,
    DevelopmentPlan,
    Outcome,
    Receipt,
    Role,
    RoleResult,
    RunState,
    Stage,
    TaskRef,
    digest,
    utcnow,
)
from .evidence import EvidenceBundle, normalize
from .runner import (
    DEFAULT_TIMEOUT as DEFAULT_CHECK_TIMEOUT,
    FOREIGN_RUNNER,
    ArenaEscape,
    HouseRuleViolation,
    PolicyUnavailable,
    artefactual_reason,
    run_check,
    runner_identity,
    verify_log,
    verify_receipt,
)
from .capability import CapabilityViolation, SealFailed
from .store import RunStore
from .telemetry import NOT_AVAILABLE, TelemetryLog, record_from_role
from .workspace import (
    WorkspaceError,
    commit_candidate,
    has_uncommitted_work,
    is_git_repo,
    materialize,
    snapshot,
    unchanged,
    working_tree,
)
from .wslease import (
    DEFAULT_TTL_SECONDS as WORKSPACE_LEASE_TTL,
    WorkspaceHeld,
    WorkspaceLease,
    WorkspaceLeaseError,
    WorkspaceLeaseLost,
    holder_name,
)

#: The `blocked_kind` of a run that waits for its worktree (P3-06, slice 2): the lease is
#: held by another writer, it cannot be read, or the worktree carries changes that are not this
#: run's. `human` is the product's own word for "a person owns this wait": `sentinel.stuck_run`
#: reads the owner from `blocked_reason` for exactly that kind, so `hoh stuck` does not call the
#: wait a control-plane defect, and `hoh unblock` is the way out. Not a provider class, not a
#: storage class and not routed through the contract (nobody is asked to decide anything).
WORKSPACE_WAIT_KIND = "human"

#: The longest the end of an iteration waits for its keepalive thread. A renewal in flight ends
#: within the lock's own half second and one fsync; a file system that hangs must not hold the
#: iteration's end (and the worktree's release) with it. The thread is a daemon: left behind, it
#: finds the lease released and ends.
KEEPALIVE_JOIN_SECONDS = 5.0


#: Prefix by which `stages.reject_candidate` tells a QA **outage** apart from
#: a substantive no. It is produced when no usable QA answer arrives, and
#: checked a few lines further down via `startswith`. Kept as a constant
#: rather than as text typed twice: the same coupling over prose already
#: carried a behavior in `cli`/`dispatchers` that not a single line of test
#: covered (see `WaitingForApproval`). Whoever changes the wording changes it
#: here -- and both sides move along with it.
NO_QA_VERDICT = "NO QA VERDICT"
#: P3-41 slice 4: the prefix of a rejection that withholds the acceptance of a candidate whose
#: criteria are all green, because a role runs on a weaker route and the policy's verification is
#: not met (`Controller._weaker_route_unmet`).
WEAKER_ROUTE_HOLD = "acceptance withheld on a weaker route"

#: The seal ledger, in the run directory: one JSON line per seal event of a
#: candidate arena (`planned`, `sealed`, `kept`, `touched`, `releasing`,
#: `released`). Append-only and durable, written before the first `chmod`, and
#: protected during every dispatch like the rest of the run's evidence. It is
#: what restores an arena a process left sealed, and what tells a later
#: verification which arenas are still sealed and what they looked like.
SEAL_LEDGER = "seals.jsonl"


def arena_name(secret: str | None, iteration: int, attempt: int, suffix: str = "") -> str:
    """The directory name `Controller._arena` gives an arena.

    One function for `_arena` and for the ledger check in `_open_seals`, so the
    two cannot drift: a `planned` line is honoured only for the arena whose
    name this run's secret, and the iteration and attempt on the line, give.
    """
    return digest(f"{secret}:{iteration}:{attempt}:{suffix}")[:12]


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class DispatchError(RuntimeError):
    """The role run did not come about. Transient or final -- the controller
    decides based on `transient`."""

    #: Whether the role's pane stays open after this error, so the agent in it
    #: may still act. `_verify` keeps QA's candidate arena sealed for such an
    #: error rather than unsealing it under a pane that can still write.
    pane_kept_open = False

    def __init__(self, message: str, *, transient: bool = False,
                 retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.transient = transient
        #: Seconds the controller waits before a transient retry. Zero for
        #: every error but a stalled prompt: Herdr gave up after 5 s on a pane
        #: that had not started its turn yet, and asking again at once asks
        #: the same pane in the same state.
        self.retry_after = retry_after


class WaitingForApproval(DispatchError):
    """An agent is stuck on a permission dialog in the pane.

    Its own type, because the caller **has to treat this case differently**:
    the role tabs stay open so that the captain can grant the approval.
    Previously `cli.cmd_run` recognized the case by the German word
    "Freigabe" appearing in the error message -- a coupling to German prose
    text across two modules that not a single line of test covers.
    Translating the source into English would have torn it apart silently:
    the message becomes English, the comparison keeps looking for the German
    word, and the window in which the captain could have answered disappears
    again -- exactly the loss that the comment at the site of the finding
    describes as having already happened twice.
    """

    pane_kept_open = True


class AnswerMissing(DispatchError):
    """A structured role's turn ended, and its answer file is missing or empty.

    Idle is not done. Four iterations were lost to it: QA started its full
    suite in the background, ended its turn saying it would write the answer
    when the suite finished, `herdr agent prompt --wait` returned on the idle
    pane, and the dispatch was booked as an outage. In `spark` the answer
    arrived 6 min 42 s after the rejection, into an iteration already closed.

    Its own type so the controller can ask once more -- one charged,
    witnessed follow-up -- instead of failing the iteration. Non-transient:
    re-sending the whole prompt is not what the role needs.

    Everything it carries comes from the dispatch that sent `full`, held in
    memory; none of it is ever re-read from a marker or any other file.
    """

    def __init__(self, message: str, *, answer_path: Path, target: str,
                 prompt_sha256: str, prompt_bytes: int, prompt_file: str | None,
                 sent_at: float, followed_up: bool = False) -> None:
        super().__init__(message, transient=False)
        self.answer_path = Path(answer_path)
        self.target = target
        #: sha256 and UTF-8 length of `full`, the prompt of record.
        self.prompt_sha256 = prompt_sha256
        self.prompt_bytes = prompt_bytes
        #: The prompt file's name in the answers directory, or None when the
        #: prompt went inline.
        self.prompt_file = prompt_file
        #: `time.monotonic()` just before the prompt was sent: the role
        #: timeout is counted from here.
        self.sent_at = sent_at
        self.followed_up = followed_up


class PaneNotClean(DispatchError):
    """A role's pane is not proven free to take a prompt, so none was sent.

    Run padopt lost two QA iterations to it: a prompt stayed unsent in the
    agent's input box, the stall retry was sent on top of it, and the joined
    text began with a slash command the agent did not know. The dispatcher
    now reads the pane after a prompt that did not return, and checks a pane
    Herdr restored, before anything goes into it; whatever it cannot prove
    free raises this.

    Non-transient: re-sending is exactly what must not happen. Deliberately
    not a `WaitingForApproval` (taxonomy walks the MRO) -- there is no dialog
    to answer, there is a pane to inspect. `_verify` records it and lets it
    through, so the run blocks once instead of booking a QA outage, and
    `cli.cmd_run` keeps the role tabs open for it, by type.
    """

    #: `cli.cmd_run` keeps the role tabs open for it, so the QA pane may still
    #: act: the candidate arena stays sealed.
    pane_kept_open = True

    def __init__(self, message: str) -> None:
        super().__init__(message, transient=False)


class RoleNotQuiescent(PaneNotClean):
    """The developer ended its turn, but its session could not be ended, or what
    it started could not be shown to be gone: the candidate is not frozen.

    Closing a role's pane ends its agent and its shell, not the commands it
    started in the background (live smoke, 2026-10-06), and a developer that can
    still wake edits the tree after the freeze (runs qaw and qaw6). So before
    the freeze `HerdrDispatcher.release_role` ends the session and the
    processes the role left, and anything it cannot prove stops the developer's
    step here.

    A `PaneNotClean`, so `cli._waiting_for_approval` keeps the tabs open **by
    type** and the exception path blocks the run once. Not transient. Its first
    line names none of the words `taxonomy.classify_failure` reads. When the
    pane could not be closed, `hoh unblock` and `hoh run` re-attach to the
    still-open pane through the existing adoption; when it is the processes
    that are the problem, the pane is already closed and the replanned
    iteration starts a fresh, marked one.
    """


class RoleDispatcher(Protocol):
    """Runs a role prompt and returns the raw text."""

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str: ...

    def endpoint_evidence(self, role: Role) -> str:
        """Provable identity of the run (A01)."""
        ...


@dataclass
class LoopOutcome:
    """What one iteration produced."""

    iteration: int
    accepted: bool
    reason: str
    candidate_id: str | None = None
    verdicts: list[CheckVerdict] = field(default_factory=list)
    #: A role is sitting at an unanswered permission dialog in its pane.
    #:
    #: Found on 2026-09-08 by the dogfood run itself, in `d1` iteration 1 --
    #: the first finding this project produced about itself rather than
    #: through inspection. `cli.cmd_run` keeps the role tabs open when an
    #: iteration ends in an **exception** carrying `WaitingForApproval`, so
    #: the captain can still answer. But `_verify` converts exactly that
    #: exception into an *outage* string, which is right: an unanswered QA is
    #: a missing verdict, not a failed run. The consequence was that the
    #: blocked dialog took the **normal** exit, where `close_own()` closes the
    #: tabs -- and with them the one window in which the dialog could have
    #: been answered.
    #:
    #: The protection guarded one route while the traffic took the other. This
    #: flag is the route-independent form: a boolean out of the controller,
    #: not a word in a message.
    waiting_for_approval: bool = False


def keepalive_period(ttl_seconds: int) -> float:
    """How often the holder of a workspace lease renews it: a third of the lease, never under a
    second (`succession.MasterAuthority.keepalive` renews on the same rule)."""
    return max(1.0, ttl_seconds / 3)


class _WorkspaceHold:
    """The lease of one iteration, kept alive while the iteration runs.

    A role's dispatch can take longer than the lease; a holder whose only renewal is the next
    write loses it in the middle of the work and is displaced while still the writer. The thread
    renews every `period` seconds. It ends at the first refusal (the lease is not this handle's
    any more: renewing again would not change that, and the commit's own check is what refuses)
    and when the iteration ends; a transient error is retried at the next period."""

    def __init__(self, lease: WorkspaceLease, period: float) -> None:
        self.lease = lease
        self.period = period
        #: Why the keepalive saw the lease go, or None.
        self.lost: str | None = None
        #: Renewals that failed with anything but a lost lease (a full disk, a lock that stayed
        #: busy), and the last of them: retried at the next period, never silent (`errors` is
        #: written into the run's history once the commit has been tried, `_note_keepalive_errors`).
        self.errors = 0
        self.last_error: str | None = None
        self.noted = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hoh-workspace-keepalive",
                                        daemon=True)
        self._started = False

    def start(self) -> None:
        self._started = True
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.period):
            try:
                self.lease.renew()
            except WorkspaceLeaseLost as exc:
                self.lost = str(exc)[:300]
                return
            except Exception as exc:                  # noqa: BLE001 - retried at the next period
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                continue

    def close(self, reason: str) -> None:
        """Stop renewing, then give the worktree up. A lease that is no longer this handle's is
        left exactly as it is (the holder that replaced it is not touched); it is never raised."""
        self._stop.set()
        if self._started:
            self._thread.join(timeout=KEEPALIVE_JOIN_SECONDS)
        try:
            self.lease.release(reason=reason)
        except WorkspaceLeaseError:
            pass


class _ScratchRefused(RuntimeError):
    """`run_check(scratch=)` refused the workspace the controller gave it (a
    `ValueError` of the runner's guard): the check did not run. Only a
    `ValueError` out of `run_check` itself becomes one -- never one out of making
    the workspace."""


#: P3-INV1: what a run says when it is stopped at the ceiling `hoh run --dispatch-ceiling` told it. The launcher
#: reads a run that stopped with it as the budget's stop (`RunVerdict.BUDGET_EXHAUSTED`), so the words are one constant.
DISPATCH_CEILING_REACHED = "dispatch ceiling of this launch reached"


class Controller:
    """Drives one run's iterations.

    Storage admission (STORAGE-B3, P2-40) is explicit: with a
    ``storage_admission`` (a ``StorageAdmission`` -- `hoh run` always builds
    one from the resolved state root and claims the boot first), every role
    dispatch first reserves a ``role_run``; a refusal parks the run durably
    as ``storage_pressure`` (`hoh.storage_park`) and no further role starts in
    that iteration, and an admitted reservation is released after the
    dispatch with the bytes it wrote under the run directory and its arena. A
    Controller built **without** it -- every library caller and test that
    does not pass one -- performs no admission and writes no storage file; it
    records one note ``storage admission: not configured (library use)``.

    Temporary workspaces (P2-42) are explicit too: with ``ephemeral_roots`` (the
    `EphemeralRoots` of the run process's registered session -- `hoh run` builds
    it for a registered project) every acceptance check, candidate and baseline,
    runs with a workspace of its own as its ``HOME`` and ``TMPDIR``
    (``scratch=``), and every iteration runs with a workspace as the default
    temporary directory of the process (`workspace.snapshot` and `materialize`
    make their temporary files there). A refused admission of either parks the
    run exactly like a refused role dispatch. A Controller built **without** it
    -- every library caller and test that does not pass one -- is unchanged: the
    runner's sibling scratch and the host's default temporary directory.

    One writer per worktree (P3-06, slice 2): every iteration takes the lease of
    ``state.repo_path`` (`hoh.wslease`, the run id as holder) before its first role,
    renews it from a keepalive thread, hands it to `commit_candidate`, looks at it
    again after the commit and releases it on every way out of the iteration -- the
    return, an exception, an interrupt and a park. A worktree another live writer
    holds is a **wait** that names the holder (the run is BLOCKED, owned by a person --
    ``blocked_kind`` is ``human`` --, nothing is charged and no role starts), never a failed
    criterion and never a quota block; a worktree that carries uncommitted changes and was last
    held by another run is refused the same way, so one run's leftovers are never committed
    as another's. A path that is not the root of a git worktree
    gets no lease (the iteration's own `snapshot` refuses it) and says so.
    """

    def __init__(
        self,
        store: RunStore,
        dispatcher: RoleDispatcher,
        *,
        spec_path: Path | str,
        scratch_dir: Path | None = None,
        isolation: "sandbox_mod.Isolation | None" = None,
        storage_admission=None,
        park_policy=None,
        steward=None,
        recovery: "dispatchrecovery.RecoveryContext | None" = None,
        ephemeral_roots=None,
        workspace_lease_ttl: int = WORKSPACE_LEASE_TTL,
        workspace_keepalive: float | None = None,
        dispatch_ceiling: int | None = None,
    ) -> None:
        self.store = store
        #: P3-INV1: the most dispatches (`usage.dispatches`, the run's total) this process may
        #: reach, told by `hoh run --dispatch-ceiling`: the shared budget's hold for this launch.
        #: It is never written to the run's state (the run's own `budgets` stay as they are); both
        #: bounds apply, the lower one first.
        self.dispatch_ceiling = dispatch_ceiling
        #: P3-06: how long one iteration's lease of the worktree runs without a renewal, and how
        #: often it is renewed (None: `keepalive_period(workspace_lease_ttl)`, a third of it).
        self.workspace_lease_ttl = workspace_lease_ttl
        self._workspace_keepalive = workspace_keepalive
        #: The `_WorkspaceHold` of the iteration that is running, or None.
        self._workspace_hold: _WorkspaceHold | None = None
        #: `workspace lease: not taken` is said once per controller, not once per iteration.
        self._lease_not_taken_noted = False
        #: P2-42: the `EphemeralRoots` of this process's registered session, or
        #: None (library use, or a project that is not registered).
        self.ephemeral_roots = ephemeral_roots
        #: The `ExitStack` of the iteration that is running (see `_run_iteration_guarded`).
        self._iteration_temp = None
        #: P2-43b: the accounts and data class the caller declared, for the
        #: provenance of every dispatch. None (library use) records
        #: `NOT_AVAILABLE` for the account and `UNSPECIFIED` for the data class.
        self.recovery = recovery
        #: STORAGE-D: the project's storage steward, or None. It is only told
        #: (`on_event`, which records one bounded line and returns) about a
        #: terminal transition and -- through the park -- about a refused
        #: admission; a controller without one emits no event.
        self.steward = steward
        #: STORAGE-B3: the admission every role dispatch asks first, or None
        #: (library use: no admission, no storage file, one note).
        self.storage_admission = storage_admission
        self.park_policy = park_policy
        self._storage_noted = False
        # The digest `_seal_candidate` wrote into each `sealed` line, per arena,
        # for `_release_seal` to compare with (it is in memory only: a process
        # that ends mid-dispatch leaves the ledger's own digest to the next check).
        self._sealed_digests: dict[str, str] = {}
        #: The contract this run is governed by, if one was pinned. Read once
        #: per controller: every block below routes through it, so what a
        #: capability violation, a shrunken amendment chain, a spec drift, an
        #: exhausted budget or a broken candidate binding *means* is the
        #: contract's decision and not a constant. A reviewer counted five
        #: block sites here that bypassed the contract entirely, which made
        #: "the run obeys its contract" true only of the two sites in the CLI.
        self._governor = None
        self.dispatcher = dispatcher
        self.spec_path = Path(spec_path).expanduser()
        self.scratch_dir = scratch_dir
        # How acceptance checks are executed. `None` means the historical
        # path, and it stays the default: adding a sandbox must not silently
        # change what existing runs do.
        #
        # It is set once, for the whole run, and applies to the candidate
        # check *and* the baseline check. Letting the two differ would be a
        # quiet way to compare two states measured under different rules,
        # which is the failure mode the baseline check exists to prevent.
        self.isolation = isolation

    # -- Creating and loading ------------------------------------------------ #

    @staticmethod
    def new_state(
        *,
        run_id: str,
        repo_path: Path | str,
        project_name: str,
        spec_path: Path | str,
        budgets: Budgets | None = None,
        delivery_mode: str = "local-only",
    ) -> RunState:
        spec_text = roles.read_spec(spec_path)
        return RunState(
            run_id=run_id,
            repo_path=str(Path(repo_path).expanduser().resolve()),
            project_name=project_name,
            spec_path=str(Path(spec_path).expanduser().resolve()),
            spec_digest=digest(spec_text),
            policy_digest=digest(_policy_text()),
            profile_digest=digest("controller=v1"),
            budgets=budgets or Budgets(),
            delivery_mode=delivery_mode,  # type: ignore[arg-type]
            yolo="off",
        )

    # -- The loop ------------------------------------------------------------ #

    def run_iteration(self, state: RunState) -> LoopOutcome:
        """One complete iteration under the controller lock.

        The lock had been built and tested before -- and used only in the CLI.
        `run_iteration` wrote six times without it.

        **The lock alone is not enough, and A07 is therefore not yet
        fulfilled.** It serializes writers, but does not detect a *stale*
        state: a controller holding an old `RunState` object gets the lock in
        turn and thereby overwrites a checkpoint that has been accepted in the
        meantime. An adversarial review reproduced exactly that. The only
        remedy is a version comparison on write (fencing) -- see
        RunState.write_seq and RunStore.write_state.
        """
        before = (state.stage, state.condition)
        with self.store.lock():
            outcome = self._run_iteration_guarded(state)
        self._tell_steward_terminal(state, before)
        return outcome

    def _tell_steward_terminal(self, state: RunState, before: tuple) -> None:
        """STORAGE-D: after the iteration (the run lock released), tell the
        steward about a terminal transition -- a persisted state whose stage
        is CHECKPOINTED or READY_FOR_DELIVERY or whose condition is FAILED or
        CANCELLED, reached in this iteration. Isolated: whatever the steward
        raises is noted, never propagated. Without a steward, nothing."""
        steward = getattr(self, "steward", None)
        if steward is None:
            return
        try:
            persisted = self.store.read_state()
        except Exception:                    # noqa: BLE001 - nothing provably persisted
            return
        terminal = (persisted.stage in (Stage.CHECKPOINTED, Stage.READY_FOR_DELIVERY)
                    or persisted.condition in (Condition.FAILED, Condition.CANCELLED))
        if not terminal or (persisted.stage, persisted.condition) == before:
            return
        try:
            steward.on_event("terminal_transition")
        except Exception as exc:             # noqa: BLE001 - never fails the run
            state.note(f"storage steward: recording terminal_transition failed: "
                       f"{type(exc).__name__}: {exc}"[:400])

    def _run_iteration_guarded(self, state: RunState) -> LoopOutcome:
        from .storage_park import StorageParked

        try:
            with ExitStack() as temp:
                # The iteration's workspace is entered by `_enter_run_temp`, **after** every
                # gate of `_run_iteration_locked` (a stop request, the budget, a drifted
                # specification, a halted run): a run that would not have started is never
                # parked for storage. It is released here, on every way out.
                self._iteration_temp = temp
                try:
                    return self._run_iteration_locked(state)
                finally:
                    self._iteration_temp = None
        except StorageParked as parked:
            # The state was persisted by the park (or the NEEDS_YOU stop)
            # itself, timer first. Nothing else is written and no further
            # role starts in this iteration; the run is no longer ACTIVE,
            # so `hoh run` stops looping.
            return LoopOutcome(state.iteration, False, parked.reason)
        except DispatchError as exc:
            # P2-43a: every role's failed dispatch ends here (the planner, the
            # developer, a transient retry that ran out, the follow-up for a
            # missing answer; QA re-raises from `_verify`). A wait or an
            # operator action is not a verdict and not an iteration; anything
            # else leaves exactly as before, to the CLI's generic block.
            failure = failures.classify(exc)
            if not failures.is_infrastructure(failure):
                raise
            return self._failure_wait(state, exc, failure)
        except CapabilityViolation as exc:
            # Fail closed, and as a *result* rather than as a crash. A
            # violation is not an outage and must not be re-read as a
            # missing verdict -- that path would let the loop treat it as
            # an ordinary rejection and carry on. It blocks the run, keeps
            # the reason, and leaves the evidence where it is.
            # The kind is what keeps it blocked: sticky through a re-block,
            # a pause and a resume, until a person runs `hoh unblock`.
            state.blocked_kind = stages.CAPABILITY_VIOLATION
            self._block(state, f"capability violation: {exc}")
            self.store.write_state(state)
            return LoopOutcome(state.iteration, False,
                               state.blocked_reason or str(exc))
        except SealFailed as exc:
            # QA never started: an arena that could not be made read-only is
            # rolled back and the run waits for a person, rather than letting
            # QA review a candidate it could write.
            self._block(state, f"the candidate arena could not be sealed, "
                               f"QA was not started: {exc}", scan_for_quota=False)
            self.store.write_state(state)
            return LoopOutcome(state.iteration, False,
                               state.blocked_reason or str(exc))

    def _run_iteration_locked(self, state: RunState) -> LoopOutcome:
        if state.started_at_wall is None:
            state.started_at_wall = utcnow()
        if state.arena_secret is None:
            import secrets

            state.arena_secret = secrets.token_hex(8)
        exhausted = self._dispatches_exhausted(state)
        if exhausted:
            self._block(state, exhausted)
            self.store.write_state(state)
            return LoopOutcome(state.iteration, False, exhausted)

        evidence = self.store.read_evidence(expect_digest=state.evidence_digest)
        spec_text = roles.read_spec(self.spec_path)

        # An amendment is the one way the text may move, and it moves the
        # run's own digest with it. Read before the drift check rather than
        # after: the chain is what tells a silent edit apart from a recorded
        # one, and without it every amendment would look like drift.
        chain = self._amendment_chain(state)
        self._pin_needed: dict[str, set[str]] = {}
        seen_ = len(chain.amendments) if chain is not None else 0
        if seen_ < state.amendments_seen:
            # The chain got shorter. Whatever did that -- a rename, a prune, a
            # hand -- the obligation an amendment created does not evaporate
            # with the file that recorded it.
            self._block(
                state,
                f"the amendment chain has {seen_} entries and this run has "
                f"seen {state.amendments_seen}; the record of what was amended "
                "is incomplete"
            )
            self.store.write_state(state)
            return LoopOutcome(state.iteration, False,
                               state.blocked_reason or "amendment chain shrank")
        if seen_ > state.amendments_seen:
            state.amendments_seen = seen_
            self.store.write_state(state)
        if chain is not None and chain.current_digest() == digest(spec_text):
            if state.spec_digest != chain.current_digest():
                state.note(
                    f"specification amended: {state.spec_digest} -> "
                    f"{chain.current_digest()} through "
                    f"{len(chain.amendments)} amendment(s)"
                )
                state.spec_digest = chain.current_digest()
                self.store.write_state(state)
            self._reopen_amended_criteria(chain, state)
            self._reopen_pin_amended(chain, state, spec_text)

        # The specification must not change silently underneath a running run
        # -- otherwise two iterations plan and verify against different truths.
        if digest(spec_text) != state.spec_digest:
            self._block(
                state,
                f"specification has changed (expected {state.spec_digest}, "
                f"found {digest(spec_text)})",
            )
            self.store.write_state(state)
            return LoopOutcome(state.iteration, False, state.blocked_reason or "spec drift")

        halt = self._honor_stop_request(state)
        if halt is not None:
            return halt

        # P3-06: the worktree is leased from here -- after every gate above, before the first
        # role, before the iteration is charged -- and given back on every way out of the
        # iteration (`_run_iteration_guarded`'s scope). A worktree another writer holds is a
        # wait: nothing below runs and nothing is spent.
        waiting = self._hold_workspace(state)
        if waiting is not None:
            return waiting

        # --- PLANNING ------------------------------------------------------- #
        stages.begin_iteration(state, reason="new iteration")
        iteration = state.iteration
        self.store.write_state(state)
        # P2-42: the iteration's temporary workspace, asked for where the planner's own
        # storage gate is -- after every gate above, so a refusal parks a run that was
        # going to run (the iteration is charged as it is for a refused planner dispatch).
        self._enter_run_temp(state)

        base = state.last_accepted_candidate or state.base_candidate
        if base is None:
            base = snapshot(state.repo_path, f"{state.run_id}-base")
            state.base_candidate = base
            self.store.write_state(state)

        # A03: the planner gets a copy of its own, not the live tree.
        # Handoff §5 explicitly rejects prompt-only: "writing read-only into a
        # prompt is not enforcement when a shell is freely available." If it
        # writes anyway, it hits the copy -- the artifact stays untouched, and
        # the developer remains the only writer.
        # O125: the planner's copy gets a parent of its own.
        #
        # It used to sit directly under the arena root, which made the
        # **candidate arena of every iteration and attempt** its sibling --
        # the trees the acceptance checks run in. The placement had a reason
        # (one directory up, the harness asks for trust once per run instead
        # of once per iteration) and the cost of it was never written next to
        # the reason. Now the planner sees its copy and nothing else, and the
        # trust question is still asked once, for this directory.
        planner_root = self.store.arenas_dir / "planner"
        planner_root.mkdir(parents=True, exist_ok=True)
        read_copy = self._arena(state, suffix="planner", base=planner_root)
        # Reset before the attempt, not only on success. Until 2026-09-08 this
        # attribute was set inside the `try` and never cleared: if the copy
        # succeeded in iteration 1 and failed in iteration 2, the state
        # recorded "falling back to live path" while the planner prompt still
        # carried **iteration 1's** relative path. The message and the
        # behaviour said different things, which is the one thing an evidence
        # trail must never do.
        self._planner_relpath = None
        try:
            materialize(base, read_copy)
            # The working directory is the **run directory**, not the
            # iteration arena: a harness asks for approval once for every new
            # folder, and one dialog per iteration would be unusable in
            # operation. This way it asks once per run; the copy sits
            # underneath it and is named relatively in the prompt.
            if hasattr(self.dispatcher, "role_cwd"):
                # The working directory is the **arena root**, not the run
                # directory. Previously the reading role sat exactly where
                # `state.json`, `checks.json`, `receipts/` and `logs/` live --
                # that is, with write access to the very evidence it is about
                # to judge. A prompt forbids that; handoff §5 says explicitly
                # that a prompt is not enforcement.
                self.dispatcher.role_cwd[Role.PLANNER] = str(planner_root)
                self._planner_relpath = str(read_copy.relative_to(planner_root))
        except Exception as exc:
            state.note(f"planner read copy not creatable, falling back to live path: {exc}")

        plan = self._plan(state, spec_text=spec_text, evidence=evidence, base=base)
        # The attempt belongs in the name: otherwise a repeated iteration
        # collided with its own artifacts from the failed attempt, and the
        # immutability boundary blocked the restart.
        self.store.write_result(f"plan-i{state.iteration}-a{state.attempt + 1}", plan)

        # --- DEVELOPING ----------------------------------------------------- #
        stages.transition(state, Stage.DEVELOPING, reason=f"Plan {plan.plan_digest()}")
        state.attempt += 1
        self.store.write_state(state)

        halt = self._honor_stop_request(state)
        if halt is not None:
            return halt

        self._develop(state, spec_text=spec_text, plan=plan, evidence=evidence)

        # --- FREEZE: persisted boundary, not a prompt ----------------------- #
        # The attempt belongs in the id, as in every other per-attempt name:
        # the resumed attempt of an aborted iteration is another candidate,
        # and its anchor must not be the earlier attempt's (run qaw4).
        candidate = snapshot(
            state.repo_path,
            f"{state.run_id}-i{state.iteration}-a{state.attempt}",
            note=f"after developer run, plan {plan.plan_digest()}",
        )
        stages.freeze(state, plan_digest=plan.plan_digest(), candidate=candidate)
        self.store.write_state(state)

        halt = self._honor_stop_request(state)
        if halt is not None:
            return halt

        # --- VERIFYING ------------------------------------------------------ #
        stages.transition(state, Stage.VERIFYING, reason="candidate frozen")
        self.store.write_state(state)

        try:
            result = self._verify(state, spec_text=spec_text, plan=plan, candidate=candidate)
        except stages.FreezeError as exc:
            # Sources changed between freeze and verdict (A05).
            self._block(state, f"candidate binding violated: {exc}", scan_for_quota=False)
            self.store.write_state(state)
            return LoopOutcome(iteration, False, str(exc))

        self.store.write_result(f"qa-i{state.iteration}-a{state.attempt}", result)

        # --- Write the evidence first, then point at it (handoff §7) -------- #
        bundle = normalize(result, prior=evidence)
        evidence_digest = self.store.write_evidence(bundle)
        state.evidence_ref = "evidence.json"
        state.evidence_digest = evidence_digest
        self.store.write_state(state)

        # --- Acceptance or rejection ---------------------------------------- #
        # An acceptance-affecting amendment invalidates the evidence that
        # rested on the old text. Checked here, on the acceptance itself,
        # because that is the decision it changes: a criterion the amendment
        # renamed the meaning of has been measured against a question that no
        # longer exists, and a verdict carrying it forward would be an
        # acceptance nobody re-earned.
        open_ = self._revalidation_open(chain, state, plan, result)
        if open_ and result.summary.startswith(NO_QA_VERDICT):
            # A QA outage is a missing verdict, not stalled substance. Reporting
            # it as "the amendment's criterion was not measured" would book a
            # quota problem as a failure to do the work -- the distinction
            # `reject_candidate` exists to keep.
            open_ = set()
        if open_:
            why = ("acceptance withheld: the specification was amended and "
                     + ", ".join(sorted(open_))
                     + " has not been measured against the new text")
            state.note(why)
            stages.reject_candidate(state, why)
            self.store.write_state(state)
            return LoopOutcome(iteration, False, why, candidate.candidate_id,
                               result.verdicts)

        if result.accepted():
            # P3-41 slice 4: a run with a role on a weaker route (a failover the routing policy
            # declared) is accepted only when the verification the policy asks for is met. The
            # route receipt says "additional verification required"; this is where it is acted on.
            unmet = self._weaker_route_unmet(result)
            if unmet:
                why = (f"{WEAKER_ROUTE_HOLD}: " + "; ".join(unmet))
                stages.reject_candidate(state, why)        # notes "candidate rejected: <why>"
                state.working_candidate = candidate
                self.store.write_state(state)
                return LoopOutcome(iteration, False, why, candidate.candidate_id,
                                   result.verdicts)
            # FID1: the pins are read again at the acceptance itself, from the working tree as it
            # is now -- QA's verdict, or anything that touched the tree after QA, is not the
            # decision. A pinned test file that is not the pinned bytes is no acceptance.
            drift = self._live_pin_findings(state, spec_text, candidate)
            if drift:
                why = spec_criteria.refusal(drift)
                state.note(why)
                stages.reject_candidate(state, why)
                state.working_candidate = candidate
                self.store.write_state(state)
                return LoopOutcome(iteration, False, why, candidate.candidate_id,
                                   result.verdicts)
            # The obligation an amendment created is discharged here, once,
            # and recorded. Recomputing it from the chain every iteration meant
            # a criterion re-measured in iteration 2 was demanded again in
            # iteration 3 -- and forever.
            done_ = set(chain.revalidation_needed()) if chain is not None else set()
            # FID1: a criterion an amendment of the test-pins block reopened is answered by
            # the same acceptance; the key names the amendment, so a later one asks again.
            done_ |= {key for keys in self._pin_needed.values() for key in keys}
            if done_:
                state.revalidated = sorted(set(state.revalidated) | done_)
            self._record_preserved(self._checks_for(plan, state), result.verdicts)
            # Make the accepted state durable before the run state records it
            # as accepted. Without this the checkpoint lived only as a dirty
            # working tree: `git checkout` would have destroyed it, and
            # `deliver` rightly refused with "working tree is not clean".
            # If the commit fails, that is no reason to overturn the
            # acceptance -- the verdict stands; only the durability is missing
            # and gets noted visibly.
            try:
                sha = commit_candidate(
                    state.repo_path, candidate,
                    run_id=state.run_id, iteration=iteration,
                    lease=self._workspace_hold.lease if self._workspace_hold else None,
                )
                state.note(f"candidate {candidate.candidate_id} committed as {sha[:12]}.")
                self._lease_after_commit(state, sha)
            except Exception as exc:
                state.note(
                    f"candidate {candidate.candidate_id} accepted, but NOT "
                    f"committed: {exc}. The state lives only in the working "
                    "tree and cannot be delivered."
                )
            # Whether the commit was made or refused (a lease that ran out under failing renewals
            # is refused): the reason the lease lapsed is written down either way.
            self._note_keepalive_errors(state)
            stages.accept_checkpoint(state, candidate)
            self.store.write_state(state)
            reason = "all criteria demonstrated"
            if self._weaker_route_required() is not None:
                reason += " -- weaker-route verification met"
            if getattr(self, "_fidelity_not_run", False):
                # an unrun check is NOT_RUN, never passed: the acceptance says it
                reason += f" -- {spec_criteria.NOT_DECLARED}"
            if getattr(self, "_pins_not_run", False):
                reason += f" -- {spec_criteria.NO_TEST_PINS}"
            return LoopOutcome(
                iteration, True, reason, candidate.candidate_id,
                result.verdicts,
            )

        open_ids = [v.check_id for v in result.verdicts if v.outcome is not Outcome.PASS]
        # "Failed" and "never checked at all" must not sound the same. If QA
        # drops out -- quota exhausted, agent crashed, dialog open -- then no
        # criterion has been refuted; none has been assessed.
        if result.summary.startswith(spec_criteria.TEST_CONTENT_FIDELITY):
            reason = result.summary
        elif result.summary.startswith(NO_QA_VERDICT):
            reason = f"{stages.UNVERIFIED}: {result.summary}"
        elif open_ids:
            reason = f"not accepted: {', '.join(open_ids)}"
        elif result.verdicts:
            # All criteria green, still no acceptance: then the proof that
            # something has changed is missing. The old message said "no
            # verdicts" here even though verdicts were present -- it described
            # the wrong defect.
            reason = (
                "not accepted: no criterion demonstrates the increment -- all "
                "of them were already green on the predecessor state or were "
                "not run against it"
            )
        else:
            reason = "not accepted: no verdicts"
        state.working_candidate = candidate
        stages.reject_candidate(state, reason)
        self.store.write_state(state)
        return LoopOutcome(
            iteration, False, reason, candidate.candidate_id, result.verdicts,
            waiting_for_approval=getattr(self, "_waiting_for_approval", False),
        )

    def _weaker_route_required(self) -> dict | None:
        """The weaker-route verification the routing the dispatcher was built on asks for, or None
        (no routing, no failover, every role on an equivalent route). `{"measures": {...},
        "roles": [...]}`; a value this does not understand is `{"measures": None}`: asked for and
        not readable, which `_weaker_route_unmet` holds."""
        evidence = getattr(self.dispatcher, "routing_evidence", None)
        asked = evidence.get("verification") if isinstance(evidence, dict) else None
        if asked is None:
            return None
        if (not isinstance(asked, dict) or asked.get("required") is not True
                or not isinstance(asked.get("measures"), dict)
                or not isinstance(asked.get("roles"), list)):
            return {"measures": None, "roles": []}
        return {"measures": asked["measures"], "roles": [str(r) for r in asked["roles"]]}

    def _weaker_route_unmet(self, result: RoleResult) -> list[str]:
        """Why a candidate whose every criterion is green is not accepted: what the policy's
        weaker-route verification asks for and the result does not have. Empty when nothing is
        asked or everything is met. `min_discriminating`: at least that many criteria that PASS
        and demonstrate the increment; `no_open_gaps`: QA reported no open gap."""
        asked = self._weaker_route_required()
        if asked is None:
            return []
        measures, roles = asked["measures"], ", ".join(asked["roles"]) or "a role"
        if measures is None:
            return [f"a route receipt asks for verification this controller cannot read ({roles} "
                    "runs on a weaker route)"]
        unmet = []
        need = measures.get("min_discriminating")
        have = sum(1 for v in result.verdicts if v.outcome is Outcome.PASS and v.discriminates)
        if need is not None and (isinstance(need, bool) or not isinstance(need, int)
                                 or have < need):
            unmet.append(f"{roles} runs on a weaker route and the routing policy asks for at "
                         f"least {need} criteria that demonstrate the increment ({have} here)")
        if measures.get("no_open_gaps") is True and result.open_gaps:
            unmet.append(f"{roles} runs on a weaker route and the routing policy asks QA for no "
                         f"open gap ({len(result.open_gaps)} reported)")
        unknown = set(measures) - {"min_discriminating", "no_open_gaps"}
        if unknown:
            unmet.append("the routing policy asks for a measure this controller does not know")
        return unmet

    def _honor_stop_request(self, state: RunState) -> LoopOutcome | None:
        """Evaluates a stop request that was filed without holding the lock.

        Called at the documented safe boundaries between two phases -- never
        in the middle of a running check. Handoff §7: *"`pause` stops further
        dispatches at a documented safe boundary."*
        """
        request = self.store.read_stop_request()
        if request is None:
            return None

        kind = request.get("kind", "pause")
        reason = request.get("reason") or "requested by the captain"
        self.store.clear_stop_request()

        if kind == "cancel":
            stages.cancel(state, reason)
        else:
            stages.pause(state, reason)
        self.store.write_state(state)
        return LoopOutcome(state.iteration, False, f"{kind}: {reason}")

    # -- Roles --------------------------------------------------------------- #

    def _plan(
        self, state: RunState, *, spec_text: str, evidence: EvidenceBundle, base: Candidate
    ) -> DevelopmentPlan:
        prompt = roles.planner_prompt(
            iteration=state.iteration,
            spec_text=spec_text,
            evidence=evidence,
            repo_path=getattr(self, "_planner_relpath", None) or state.repo_path,
            base_candidate_id=base.candidate_id,
            spec_digest=state.spec_digest,
            run_id=state.run_id,
            reopened=getattr(self, "_reopened", None),
        )
        # The specification's own criteria, read before any role starts: a block
        # that cannot be read ends the iteration here, with nothing dispatched. A
        # specification without one is not compared, and the state says so -- and so
        # does the acceptance, should it come (`_fidelity_not_run`).
        declared = spec_criteria.declared_criteria(spec_text)
        # FID1: the test files the specification pins are read at the same point, for the same
        # reason; a block that cannot be read, or that leaves a file a criterion runs unpinned,
        # ends the iteration before any role starts.
        pins = spec_criteria.declared_pins(spec_text)
        self._pins_not_run = bool(declared) and not pins
        if self._pins_not_run:
            state.note(spec_criteria.NO_TEST_PINS)
        fidelity = None
        self._fidelity_not_run = not declared
        if declared:
            fidelity = partial(spec_criteria.assert_faithful, declared,
                               frozen=set(self.store.read_checks()))
        else:
            state.note(spec_criteria.NOT_DECLARED)
        self._begin_count()
        started_at = utcnow()
        # 'ok' is earned only once the plan has answered, schema-parsed, AND
        # bound correctly to this run/iteration/spec/base -- a plan that
        # parses but is bound to the wrong run is rejected by `_plan` itself
        # (see `_assert_plan_bound`), and a telemetry record claiming success
        # for it would contradict the very rejection it describes.
        try:
            raw = self._dispatch_with_repair(Role.PLANNER, prompt, state, plan_check=fidelity)
            plan = roles.parse_plan(raw)
            self._assert_plan_bound(plan, state, base)
        except Exception as exc:
            self.note_dispatch(
                role=Role.PLANNER.value, run_id=state.run_id, iteration=state.iteration,
                attempt=state.attempt, started_at=started_at, ended_at=utcnow(),
                backend=type(self.dispatcher).__name__, outcome=_failed_outcome(exc),
                detail=str(exc), usage={}, state=state, exc=exc,
            )
            raise
        self.note_dispatch(
            role=Role.PLANNER.value, run_id=state.run_id, iteration=state.iteration,
            attempt=state.attempt, started_at=started_at, ended_at=utcnow(),
            backend=type(self.dispatcher).__name__, outcome="ok", usage={}, state=state,
        )
        return plan

    @staticmethod
    def _assert_plan_bound(plan: DevelopmentPlan, state: RunState, base: Candidate) -> None:
        """Handoff §6: mapping of run, attempt, candidate, plan and results.

        Previously the plan was taken as it came. A plan with a foreign
        `run_id`, `iteration=99` and an invented `spec_digest` ran all the way
        through to the checkpoint -- the spec binding was pure declaration.
        """
        deviations = []
        if plan.run_id != state.run_id:
            deviations.append(f"run_id {plan.run_id!r} instead of {state.run_id!r}")
        if plan.iteration != state.iteration:
            deviations.append(f"iteration {plan.iteration} instead of {state.iteration}")
        if plan.spec_digest != state.spec_digest:
            deviations.append(
                f"spec_digest {plan.spec_digest!r} instead of {state.spec_digest!r}"
            )
        if plan.base_candidate_id != base.candidate_id:
            deviations.append(
                f"base_candidate_id {plan.base_candidate_id!r} instead of {base.candidate_id!r}"
            )
        if deviations:
            raise roles.RoleOutputError(
                "The plan is not bound to this run: " + "; ".join(deviations)
            )

    def _develop(
        self,
        state: RunState,
        *,
        spec_text: str,
        plan: DevelopmentPlan,
        evidence: EvidenceBundle,
    ) -> None:
        prompt = roles.developer_prompt(
            iteration=state.iteration,
            attempt=state.attempt,
            spec_text=spec_text,
            plan=plan,
            workspace=state.repo_path,
            warm_start=state.last_accepted_candidate is not None,
            evidence=evidence,
        )
        self._begin_count()
        started_at = utcnow()
        try:
            self._dispatch(Role.DEVELOPER, prompt, state)
            # Inside the same `try`: a developer whose session cannot be ended
            # is booked as a failed dispatch, **before** the "ok" below and
            # therefore before the freeze.
            self._finish_turn(Role.DEVELOPER, state)
        except Exception as exc:
            self.note_dispatch(
                role=Role.DEVELOPER.value, run_id=state.run_id, iteration=state.iteration,
                attempt=state.attempt, started_at=started_at, ended_at=utcnow(),
                backend=type(self.dispatcher).__name__, outcome=_failed_outcome(exc),
                detail=str(exc), usage={}, state=state, exc=exc,
            )
            raise
        self.note_dispatch(
            role=Role.DEVELOPER.value, run_id=state.run_id, iteration=state.iteration,
            attempt=state.attempt, started_at=started_at, ended_at=utcnow(),
            backend=type(self.dispatcher).__name__, outcome="ok", usage={}, state=state,
        )

    def _verify(
        self,
        state: RunState,
        *,
        spec_text: str,
        plan: DevelopmentPlan,
        candidate: Candidate,
    ) -> RoleResult:
        """The runner executes, QA assesses. The binding is checked before AND
        after the assessment."""
        stages.assert_binding_intact(state, candidate)
        # FID1: before any check runs and before QA is asked, the candidate's test files are
        # compared with the specification's pins -- the object database, not the arena. A
        # candidate whose tests are not the pinned ones is refused here: no receipt is written
        # for tests nobody reviewed, and QA is not asked to assess them.
        findings = self._pin_findings(state, spec_text, candidate.tree_digest)
        if findings:
            return self._pin_refusal(state, plan, candidate, findings)
        # An arena an earlier QA dispatch left sealed -- its pane was kept
        # open, or the process was interrupted -- is compared here, at every
        # verification, and never unsealed: the pane may still be open.
        self._check_open_seals(state)

        # Isolated copy: the verification never touches the live workspace.
        # Otherwise even a __pycache__ or a build folder breaks the binding,
        # and worse: QA could repair the very candidate it is assessing.
        arena = self._arena(state)
        materialize(candidate, arena)
        probe = candidate.model_copy(update={"repo_path": str(arena)})

        # Preservation requirements: every previously validated check runs
        # again on EVERY further candidate. Without that it was enough to drop
        # it from the next plan to get a real regression green.
        to_check = self._checks_for(plan, state)
        if getattr(self, "_disarm_attempts", None):
            state.note(
                "preservation criteria protected against redefinition: "
                + "; ".join(self._disarm_attempts)
            )

        # New criteria have to actually demonstrate the increment.
        preserved = set(self.store.read_checks())
        fresh = [c for c in to_check if c.check_id not in preserved]
        baseline = state.last_accepted_candidate or state.base_candidate
        discriminating, blind = (
            self._discriminates(fresh, state, baseline) if baseline else (set(), {})
        )
        if blind:
            state.note(
                "non-discriminating criteria: "
                + ", ".join(f"{cid} ({reason})" for cid, reason in blind.items())
            )

        receipts: list[Receipt] = []
        runner_verdicts: dict[str, CheckVerdict] = {}

        for check in to_check:
            # Be able to stop between the checks. Otherwise a preservation
            # suite with 15 criteria runs for up to two and a half hours under
            # the exclusive lock, during which `hoh pause` and `hoh cancel`
            # can only file a request -- the boundaries were so far only
            # evaluated at phase transitions.
            if self.store.read_stop_request():
                state.note(
                    f"stop request honored during verification (after "
                    f"{len(receipts)} of {len(to_check)} criteria)"
                )
                break
            receipt, log = self._execute(state, check, probe)
            receipts.append(receipt)
            self.store.write_receipt(receipt.receipt_id, receipt)
            self.store.write_log(receipt.receipt_id, log)
            runner_verdicts[check.check_id] = CheckVerdict(
                check_id=check.check_id,
                outcome=receipt.outcome(check.expect_exit),
                receipt_id=receipt.receipt_id if receipt.exit_code == check.expect_exit else None,
                note=f"Runner: exit {receipt.exit_code}",
            )

        # QA works IN the arena, not merely with its address in the prompt.
        # Handoff §5: "writing read-only into a prompt is not enforcement when
        # a shell is freely available." Previously its own artifacts broke the
        # candidate binding and cost the iteration.
        if hasattr(self.dispatcher, "role_cwd"):
            # See the planner: the arena root, not the evidence directory.
            self.dispatcher.role_cwd[Role.QA] = str(self.store.arenas_dir)
        # QA's working directory is the arena root, so every other arena is a
        # sibling it can reach by name -- the shape O125 objected to for the
        # planner. Naming the one it is reviewing lets the policy protect all
        # the others instead of hoping.
        self._qa_arena = arena

        prompt = roles.qa_prompt(
            iteration=state.iteration,
            spec_text=spec_text,
            plan=plan.model_copy(update={"acceptance_checks": to_check}),
            candidate_path=str(arena.relative_to(self.store.arenas_dir)),
            candidate_binding=candidate.binding(),
            receipts_summary=_receipts_summary(receipts),
        )
        # QA gets its repair round like the other roles. It was the only one
        # that used _dispatch instead of _dispatch_with_repair -- handoff §8
        # provides for one schema repair per role output, without exempting
        # any role.
        outage = ""
        qa_exc: Exception | None = None
        interrupted: BaseException | None = None
        witnessed = False
        self._begin_count()
        started_at = utcnow()
        from .storage_park import StorageParked

        # QA's view of the candidate is read-only on disk for its dispatch,
        # and the plan that restores it is on the ledger before the first
        # `chmod`. A seal that fails is rolled back and QA never starts.
        sealed = self._seal_candidate(state, arena)
        try:
            raw = self._dispatch_with_repair(Role.QA, prompt, state)
        except StorageParked as parked:
            # Not an outage and not a verdict: the run parked (or stopped
            # NEEDS_YOU) before QA started. Recorded as what it is, then the
            # park ends the iteration (`run_iteration`).
            self.note_dispatch(
                role=Role.QA.value, run_id=state.run_id, iteration=state.iteration,
                attempt=state.attempt, started_at=started_at, ended_at=utcnow(),
                backend=type(self.dispatcher).__name__, outcome=_failed_outcome(parked),
                detail=str(parked), usage={}, state=state, exc=parked,
            )
            raise
        except PaneNotClean as exc:
            # Not an outage and not a verdict: QA was never prompted, because
            # its pane was not proven free. Recorded, then the run blocks once
            # with the tabs open, so the pane can be inspected; a resumed run
            # checks it again before any prompt.
            state.note(f"qa not prompted, its pane is not proven free; the run "
                       f"blocks: {str(exc)[:400]}")
            self.note_dispatch(
                role=Role.QA.value, run_id=state.run_id, iteration=state.iteration,
                attempt=state.attempt, started_at=started_at, ended_at=utcnow(),
                backend=type(self.dispatcher).__name__, outcome=_failed_outcome(exc),
                detail=str(exc), usage={}, state=state, exc=exc,
            )
            # Its tabs stay open, so the `finally` must see it and keep the
            # candidate arena sealed under that pane.
            qa_exc = exc
            raise
        except DispatchError as exc:
            # A pending permission dialog is remembered as a **type**, not as
            # a word in the reason: `cli.cmd_run` has to keep the role tabs
            # open for it, and prose is what the coupling in `WaitingForApproval`
            # was built to remove.
            if isinstance(exc, WaitingForApproval):
                self._waiting_for_approval = True
            # Unusable even after the repair: that is no reason to overturn
            # the run -- it is a missing verdict, and _parse_qa treats it as
            # such (the runner may refute, never confirm).
            #
            # The **reason** must not get lost in the process. An earlier
            # version discarded it: the run then reported "not accepted:
            # K1..K9", as if QA had checked and acknowledged nothing -- while
            # in truth QA had never answered. That is the same mistake as a
            # test that was never executed counting as "passed", only with the
            # sign reversed: a statement about a verification that never
            # happened.
            raw = ""
            outage = str(exc)
            qa_exc = exc
        except BaseException as exc:
            # An interrupt (`KeyboardInterrupt`, `SystemExit`) ends the
            # dispatch without the witness having been read. The arena stays
            # sealed, and the next verification or the release compares it.
            if not isinstance(exc, Exception):
                interrupted = exc
            elif isinstance(exc, CapabilityViolation):
                # The dispatch's own witness found it; the release neither
                # repeats the violation nor replaces it.
                witnessed = True
            raise
        finally:
            if interrupted is not None:
                keep = f"interrupted by {type(interrupted).__name__}"
            elif getattr(qa_exc, "pane_kept_open", False):
                keep = "the QA pane is kept open"
            else:
                keep = ""
            self._release_seal(state, arena, sealed, keep=keep, witnessed=witnessed)

        # What the verification produced belongs on the verification's own
        # record. It was left at 0 -- a missing measurement wearing a number,
        # on a run that had written two receipts (O128).
        self.note_dispatch(
            role=Role.QA.value, run_id=state.run_id, iteration=state.iteration,
            attempt=state.attempt, started_at=started_at, ended_at=utcnow(),
            backend=type(self.dispatcher).__name__,
            outcome="failed" if outage else "ok",
            detail=outage, usage={}, state=state, exc=qa_exc,
            receipts=self._receipts_written(state),
            discriminating=len(discriminating),
            artefactual=len(getattr(self, "_artefactual", set()) & set(discriminating)),
        )

        # A wait or an operator action in QA (a usage limit, an expired
        # credential, ...) is no verdict and no rejection: it leaves through
        # `_run_iteration_guarded`. A QA turn without an answer file and
        # without a recognised cause keeps the outage route below.
        if qa_exc is not None and failures.is_infrastructure(failures.classify(qa_exc)):
            raise qa_exc

        verdicts, gaps, summary = self._parse_qa(
            raw, plan=plan, receipts=receipts, state=state, candidate=candidate,
            runner_verdicts=runner_verdicts, checked=to_check, blind=blind,
            discriminating=discriminating, outage=outage,
        )

        # After the assessment: has the object under verification changed?
        if not unchanged(state.repo_path, candidate):
            raise stages.FreezeError(
                "The working tree was modified during QA -- the verdict "
                "refers to a different state than the frozen one."
            )

        return RoleResult(
            run_id=state.run_id,
            iteration=state.iteration,
            attempt=state.attempt,
            role=Role.QA,
            candidate_id=candidate.candidate_id,
            plan_digest=plan.plan_digest(),
            spec_digest=state.spec_digest,
            verdicts=verdicts,
            receipts=receipts,
            open_gaps=gaps,
            summary=summary,
        )

    def _pin_findings(self, state: RunState, spec_text: str, tree: str) -> list[str]:
        """The candidate tree `tree` against the specification's test pins (`[]` when none)."""
        base = state.base_candidate
        return spec_criteria.pin_findings(
            spec_text, repo=state.repo_path, tree=tree,
            base_tree=base.tree_digest if base is not None else None)

    def _live_pin_findings(
        self, state: RunState, spec_text: str, candidate: Candidate
    ) -> list[str]:
        """The pins against the working tree as it is at the acceptance. A tree that is not the
        frozen candidate is a finding in itself, whatever the pins say."""
        if not spec_criteria.declared_pins(spec_text):
            return []
        try:
            live = working_tree(state.repo_path)
        except Exception as exc:  # noqa: BLE001 - fail closed
            return [f"{spec_criteria.TEST_CONTENT_FIDELITY}: the working tree could not be "
                    f"read at the acceptance: {type(exc).__name__}: {str(exc)[:200]}"]
        found = self._pin_findings(state, spec_text, live)
        if live != candidate.tree_digest:
            found.insert(0, f"the working tree carries {live}, not the frozen candidate "
                            f"{candidate.tree_digest}")
        return found

    def _pin_refusal(
        self, state: RunState, plan: DevelopmentPlan, candidate: Candidate, findings: list[str]
    ) -> RoleResult:
        """The verification's answer for a candidate whose test files are not the pinned ones:
        nothing ran, nothing is demonstrated, and the findings are the open gaps the next plan
        is shown."""
        why = spec_criteria.refusal(findings)
        return RoleResult(
            run_id=state.run_id, iteration=state.iteration, attempt=state.attempt,
            role=Role.QA, candidate_id=candidate.candidate_id,
            plan_digest=plan.plan_digest(), spec_digest=state.spec_digest,
            verdicts=[
                CheckVerdict(check_id=c.check_id, outcome=Outcome.INCONCLUSIVE,
                             note=f"{spec_criteria.TEST_CONTENT_FIDELITY}: not run")
                for c in self._checks_for(plan, state)
            ],
            receipts=[],
            open_gaps=[f"{spec_criteria.TEST_CONTENT_FIDELITY}: {f}" for f in findings],
            summary=why,
        )

    def _discriminates(
        self,
        fresh: list[AcceptanceCheck],
        state: RunState,
        baseline: Candidate,
    ) -> tuple[set[str], dict[str, str]]:
        """Runs every NEW check against the predecessor state.

        The largest gap in the whole setup, named by a review: the developer
        cannot write itself green -- but the **planner can plan itself
        green**. The acceptance criteria are written by the planner model;
        runner and QA execute them faithfully. The entire apparatus of proof
        therefore sat below the one decision that determines the verdict.

        Countermove: a criterion that was already green on the last accepted
        state does not demonstrate the increment. It is not discarded -- it
        just does not carry a PASS. This turns the planner channel from
        *trusted* into *verified*, and the base candidate binding finally
        carries something.

        Returns: (**discriminating**, blind).

        `discriminating` is the set of criteria for which it was actually
        measured that they are **red** on the predecessor state. Only these
        demonstrate an increment. It is an allow list, and that is the point:
        an earlier version stamped everything as discriminating that was not
        in `blind` -- whereby preservation criteria that never ran against the
        base automatically counted as proof of an increment. From the second
        accepted iteration on, a candidate could thereby pass **without any
        change at all**: exactly the abuse case the rule was built against. A
        pair of reviewers reproduced that empirically.

        `blind` carries the reasons for criteria that were checked but were
        already green. A criterion can be in **neither** of the two -- when
        the measurement did not come about (timeout, house rule violation,
        comparison state not creatable). Even then it is not discriminating:
        not measured means not demonstrated.
        """
        if not fresh:
            return set(), {}

        arena = self._arena(state, suffix="basis")
        try:
            materialize(baseline, arena)
        except Exception as exc:
            # No reason to overturn the run: without a comparison state the
            # check drops out, and that gets noted visibly.
            return set(), {
                c.check_id: f"comparison state not creatable: {exc}" for c in fresh
            }

        probe = baseline.model_copy(update={"repo_path": str(arena)})
        discriminating: set[str] = set()
        blind: dict[str, str] = {}
        artefactual: dict[str, str] = {}
        for check in fresh:
            try:
                # The same deadline as in the candidate run. With 120 s
                # against 600 s, a slow criterion that was green on both
                # states ran into the timeout here, thereby counted as "not
                # green" and was credited as proof of an increment.
                with self._check_workspace(state) as scratch:
                    try:
                        receipt, log = run_check(
                            check, probe, run_id=state.run_id, iteration=state.iteration,
                            attempt=state.attempt, cwd=arena, timeout=DEFAULT_CHECK_TIMEOUT,
                            receipt_suffix="basis",
                            isolation=self.isolation,
                            scratch=scratch,
                        )
                    except ValueError as exc:
                        if scratch is None:
                            raise
                        raise _ScratchRefused(str(exc)) from exc
            except (HouseRuleViolation, ArenaEscape, PolicyUnavailable, _ScratchRefused) as exc:
                # Not measured means not demonstrated: the criterion ends up
                # in neither of the two sets and therefore carries no
                # discriminates.
                blind[check.check_id] = f"base check not executed: {exc}"
                # And it leaves a receipt saying so. A refused *candidate*
                # check produced one (exit 126); a refused baseline check
                # produced nothing at all, so an iteration where the guard
                # rejected everything looked, from the receipts alone, like an
                # iteration where no baseline was ever attempted. A reviewer
                # found exactly that gap in a real run: three candidate
                # receipts, zero baseline receipts, and no way to tell
                # "refused" from "never tried".
                self._refusal_receipt(state, check, probe, exc)
                continue

            # The evidence of the base check gets written. Previously it was
            # discarded -- the mechanism that is supposed to prove the
            # increment left no trace itself, contradicting its own principle
            # that evidence comes into being in the runner.
            try:
                self.store.write_receipt(receipt.receipt_id, receipt)
                self.store.write_log(receipt.receipt_id, log)
            except Exception as exc:
                # Do not swallow. An `except: pass` hid exactly here that the
                # call had the wrong signature and that **not a single** base
                # receipt was written -- while the docstring promised the
                # opposite. An addition that fails silently is worse than
                # none.
                state.note(f"base receipt for {check.check_id} not writable: {exc}")

            result = receipt.outcome(check.expect_exit)
            if result is Outcome.PASS:
                blind[check.check_id] = (
                    "was already green on the last accepted state and "
                    "therefore does not demonstrate the increment"
                )
            elif result is Outcome.FAIL:
                discriminating.add(check.check_id)
                why = artefactual_reason(receipt.exit_code, log)
                if why:
                    # The criterion is red on the predecessor and green on the
                    # candidate, so it satisfies the letter of "demonstrates an
                    # increment" -- but the predecessor is red because the
                    # criterion's own new file is not there yet, not because
                    # the behaviour is missing. Every new test file
                    # discriminates in that sense, which makes the measure
                    # cheap to satisfy and nearly uninformative.
                    #
                    # Recorded rather than subtracted: changing what counts as
                    # an increment is a semantic change to acceptance, and it
                    # belongs in a specification rather than in a bug fix. So
                    # the weakness is visible in the evidence and to the gate
                    # that reads it, and `docs/LIMITATIONS.md` carries it as a
                    # named, tracked limitation.
                    artefactual[check.check_id] = why
            else:
                blind[check.check_id] = (
                    f"base check not evaluable ({result.value}, "
                    f"exit {receipt.exit_code}) -- not measured means not demonstrated"
                )
        if artefactual:
            state.note(
                "artefactual discrimination: "
                + "; ".join(f"{k}: {v}" for k, v in sorted(artefactual.items()))
            )
        # Kept for the dispatch record. Counting artefactual discrimination as
        # ordinary discrimination overstates what an iteration demonstrated
        # (O112), and a telemetry record that cannot tell them apart cannot
        # report a campaign's convergence honestly.
        self._artefactual = set(artefactual)
        return discriminating, blind

    def _refusal_receipt(
        self, state: RunState, check: AcceptanceCheck, candidate: Candidate, exc: Exception
    ) -> None:
        """Writes the evidence that a baseline check was refused before running.

        Exit 126 -- found, but not executable -- and `runner_ok=False`, which
        is what the candidate side already produced for the same refusal. The
        point is symmetry: absence of a receipt has to mean absence of an
        attempt, or the receipts cannot be counted.
        """
        receipt_id = (
            f"{state.run_id}-i{state.iteration}-a{state.attempt}-"
            f"{check.check_id}-basis"
        )
        now = utcnow()
        text = (
            f"$ {check.command}\n# refused before execution: {exc}\n"
            f"--- output ---\n\n"
        )
        try:
            receipt = Receipt(
                receipt_id=receipt_id,
                run_id=state.run_id,
                iteration=state.iteration,
                attempt=state.attempt,
                check_id=check.check_id,
                candidate_binding=candidate.binding(),
                command=check.command,
                exit_code=126,
                started_at=now,
                ended_at=now,
                stdout_digest=digest(text),
                stdout_path=f"logs/{receipt_id}.txt",
                runner_identity=runner_identity(),
                runner_ok=False,
            )
            self.store.write_receipt(receipt_id, receipt)
            self.store.write_log(receipt_id, text)
        except Exception as write_error:       # pragma: no cover - disk shapes
            state.note(f"refusal receipt for {check.check_id} not writable: {write_error}")

    def _checks_for(self, plan: DevelopmentPlan, state: RunState) -> list[AcceptanceCheck]:
        """Plan checks plus the persisted preservation suite.

        A check that was validated once stays a preservation requirement and
        runs again on every further candidate (handoff §6).

        **A validated version is frozen.** Previously the plan's version of
        the same `check_id` won, on the grounds that the planner may
        "sharpen, but not drop". Two independent reviewers showed that this is
        the same thing: the plan names `K1` again, sets `command` to `true`,
        and the regression protection is gone -- permanently, because
        `_record_preserved` writes the watered-down version back. And because
        the `check_id` is not new, it moreover never runs against the base. A
        real regression passed as accepted that way.

        Sharpening remains possible, but under a **new** `check_id` -- then
        the discrimination check applies, and the old requirement keeps
        running alongside. That is the only version in which "sharpening" is
        verifiably distinguishable from "disarming".
        """
        preserved = self.store.read_checks()
        combined: dict[str, AcceptanceCheck] = {}
        for cid, check in preserved.items():
            combined[cid] = check.model_copy(update={"preserves": True})
        self._disarm_attempts: list[str] = []
        for check in plan.acceptance_checks:
            validated = preserved.get(check.check_id)
            if validated is not None:
                if (check.command, check.expect_exit) != (
                    validated.command, validated.expect_exit
                ):
                    self._disarm_attempts.append(
                        f"{check.check_id}: plan wanted {check.command!r} "
                        f"(exit {check.expect_exit}), validated is {validated.command!r} "
                        f"(exit {validated.expect_exit}) -- the validated version applies"
                    )
                continue                    # frozen, the plan's version discarded
            combined[check.check_id] = check
        # Preservation first: a regression should show up early.
        return sorted(combined.values(), key=lambda c: (not c.preserves, c.check_id))

    def _record_preserved(
        self, checks: list[AcceptanceCheck], verdicts: list[CheckVerdict]
    ) -> None:
        """After an acceptance, all passed checks become preservation
        requirements."""
        passed = {v.check_id for v in verdicts if v.outcome is Outcome.PASS}
        suite = self.store.read_checks()
        for check in checks:
            if check.check_id not in passed:
                continue
            if check.check_id in suite:
                # Already validated: the version stays as it is. Overwriting
                # it was the second half of the disarming path -- the
                # watered-down version got frozen in permanently.
                continue
            suite[check.check_id] = check.model_copy(update={"preserves": True})
        if suite:
            self.store.write_checks(suite)

    @staticmethod
    def _downgrade(verdict: CheckVerdict, *, reason: str = "not assessed by QA") -> CheckVerdict:
        """A runner result without a QA assessment can refute, not confirm."""
        if verdict.outcome is Outcome.PASS:
            return CheckVerdict(
                check_id=verdict.check_id,
                outcome=Outcome.INCONCLUSIVE,
                note=(verdict.note or "") + f" -- green from the runner, but {reason}",
            )
        return verdict

    def _pass_supported(
        self,
        *,
        check_id: str,
        receipt_id: str | None,
        known: dict[str, Receipt],
        checked: list[AcceptanceCheck],
        state: RunState,
        candidate: Candidate,
    ) -> str | None:
        """Does the evidence carry the claimed PASS? Returns the rejection reason.

        Two separate questions that were previously wrongly treated as one:
        1. Is the evidence **genuine** -- from this runner, for this
           candidate, this iteration, this attempt?
        2. Does it **support** the claim -- does the exit code match the
           expected one?

        Handoff §6 says it explicitly: *"A valid schema or a hash alone proves
        no functional correctness."*
        """
        if not receipt_id:
            return "PASS without evidence"

        receipt = known.get(receipt_id)
        if receipt is None:
            return FOREIGN_RUNNER.format(receipt_id=receipt_id)

        # A05 names "manipulated log" explicitly. verify_log had been built,
        # but without a caller -- a decorative protection is worse than none.
        log_path = self.store.logs_dir / f"{receipt.receipt_id}.txt"
        if log_path.exists():
            deviation = verify_log(receipt, log_path.read_text(encoding="utf-8"))
            if deviation:
                return deviation

        authenticity = verify_receipt(
            receipt,
            run_id=state.run_id,
            iteration=state.iteration,
            attempt=state.attempt,
            binding=candidate.binding(),
            known_ids=set(known),
        )
        if authenticity:
            return authenticity

        if receipt.check_id != check_id:
            return f"evidence belongs to check {receipt.check_id}, not to {check_id}"

        check = next((c for c in checked if c.check_id == check_id), None)
        expected = check.expect_exit if check else 0
        if receipt.exit_code != expected:
            return (
                f"evidence shows exit code {receipt.exit_code}, expected {expected} -- "
                "it refutes the claim instead of supporting it"
            )
        return None

    # -- The seal of QA's candidate arena ------------------------------------ #

    def _seal_ledger(self, event: str, state: RunState | None = None,
                     **fields) -> None:
        """Append one event to the seal ledger, durably, or raise."""
        from .durable_io import durable_append

        line: dict = {"event": event, "at": utcnow()}
        if state is not None:
            line.update(run_id=state.run_id, iteration=state.iteration,
                        attempt=state.attempt)
        line.update(fields)
        durable_append(self.store.dir / SEAL_LEDGER,
                       json.dumps(line, sort_keys=True, ensure_ascii=True),
                       single_link=True)

    def _seal_candidate(self, state: RunState, arena: Path) -> dict:
        """Seal the candidate arena read-only for QA's dispatch; return the plan.

        `planned` is on the ledger before the first `chmod`, so a process
        that ends mid-seal leaves the record that restores it. A failing seal
        is rolled back, recorded as `released` with `failed`, and raises
        `SealFailed`.
        """
        from . import capability as cap

        try:
            plan = cap.seal_plan(arena)
        except OSError as exc:
            raise SealFailed(f"planning the seal of {arena} failed: {exc}") from exc
        try:
            self._seal_ledger("planned", state, arena=str(arena),
                              entries={rel: list(entry) for rel, entry in plan.items()})
        except Exception as exc:
            raise SealFailed(f"the seal ledger could not be written, nothing "
                             f"was sealed: {exc}") from exc
        # An interrupt passes through untouched: the ledger's `planned` line is
        # what restores a seal a process left half done.
        try:
            cap.apply_seal(arena, plan)
            digest = cap.frozen_digest(arena)
            self._seal_ledger("sealed", state, arena=str(arena), frozen=digest)
            self._sealed_digests[str(arena)] = digest
        except Exception as exc:
            missed: list[str] = []
            if isinstance(exc, SealFailed):
                failure = exc                 # `apply_seal` rolled itself back
            else:
                # Sealed, but the ledger cannot say so: undone, not used.
                missed = cap.unseal(arena, plan)
                failure = SealFailed(f"the seal of {arena} could not be recorded: {exc}"
                                     + (f"; not restored: {missed}" if missed else ""))
            try:
                self._seal_ledger("released", state, arena=str(arena),
                                  not_restored=missed, failed=str(failure)[:400],
                                  on_close=False, touched=False)
            except Exception as ledger_exc:   # noqa: BLE001 - noted, the failure travels
                state.note(f"seal ledger: recording the failed seal of {arena} "
                           f"failed: {ledger_exc}"[:400])
            if failure is exc:
                raise
            raise failure from exc
        return plan

    def _seal_releasing(self, state: RunState | None, arena: Path) -> None:
        """Announce an unseal. Its own failure is noted, never raised."""
        try:
            self._seal_ledger("releasing", state, arena=str(arena))
        except Exception as exc:              # noqa: BLE001 - noted
            if state is not None:
                state.note(f"seal ledger: announcing the release of {arena} "
                           f"failed: {exc}"[:400])

    def _seal_released(self, state: RunState | None, arena: Path, plan: dict, *,
                       on_close: bool = False, touched: bool = False) -> list[str]:
        """Unseal through the recorded plan and record it; never raises."""
        from . import capability as cap

        missed = cap.unseal(arena, plan)
        if missed and state is not None:
            state.note(f"the candidate arena {arena} was unsealed, but these "
                       f"entries were refused or not reached and keep their "
                       f"sealed mode: {missed[:20]}"[:600])
        try:
            self._seal_ledger("released", state, arena=str(arena),
                              not_restored=missed, failed=None,
                              on_close=on_close, touched=touched)
        except Exception as exc:              # noqa: BLE001 - noted
            if state is not None:
                state.note(f"seal ledger: recording the release of {arena} "
                           f"failed: {exc}"[:400])
        return missed

    def _release_seal(self, state: RunState, arena: Path, plan: dict, *,
                      keep: str = "", witnessed: bool = False) -> None:
        """End of QA's dispatch: unseal, or keep the seal for a stated reason.

        Runs in a `finally`. It raises only a `CapabilityViolation`, and only
        after the arena is restored: with no `keep` reason it first compares
        the arena with the digest it was sealed with (`witnessed` is true when
        the dispatch's own witness has already raised a violation, which the
        comparison neither repeats nor replaces). Every dispatch takes its
        witness when it starts, so a touch before the first dispatch, between
        two of them, or after the last one's check and before this unseal is
        in no witness; this comparison is what sees it.
        """
        from . import capability as cap

        sealed_digest = self._sealed_digests.pop(str(arena), None)
        if keep:
            state.note(f"the candidate arena {arena} stays sealed: {keep}")
            try:
                self._seal_ledger("kept", state, arena=str(arena), why=keep)
            except Exception as exc:          # noqa: BLE001 - noted
                state.note(f"seal ledger: recording the kept seal of {arena} "
                           f"failed: {exc}"[:400])
            return
        violation: str | None = None
        if sealed_digest is not None and not witnessed:
            record: dict = {}
            try:
                now = cap.frozen_digest(arena)
            except OSError as exc:
                # The arena cannot be shown untouched, so it is taken as
                # touched (a pane cannot cause this: a mode-000 file, a FIFO, a
                # socket or a symlink all give a different digest instead).
                record = {"unreadable": f"{type(exc).__name__}: {exc}"[:300]}
                violation = (f"a frozen tree could not be compared with its seal "
                             f"digest ({exc}), so it is taken as touched: {arena}")
            else:
                if now != sealed_digest:
                    record = {"frozen": now}
                    violation = ("a frozen tree was touched after it was sealed, "
                                 "outside every dispatch's witness (contents, "
                                 f"modes or change times): {arena}")
            if violation is not None:
                try:
                    self._seal_ledger("touched", state, arena=str(arena), **record)
                except Exception as exc:      # noqa: BLE001 - the violation travels
                    state.note(f"seal ledger: recording the touch of {arena} "
                               f"failed: {exc}"[:400])
        self._seal_releasing(state, arena)
        self._seal_released(state, arena, plan, touched=violation is not None)
        if violation is not None:
            state.note(violation)
            raise CapabilityViolation(violation)

    def _open_seals(self, state: RunState | None = None) -> dict[str, dict]:
        """The arenas the ledger leaves sealed: plan, digest, and `releasing`.

        Only the first `planned` line of an open seal counts, and only the
        first usable `sealed` line sets its digest; a `touched` line is a
        record of the last touched state found (`touched`), never the
        baseline. A line that does not parse is skipped. A line naming anything other
        than a direct child of this run's arena directory (or its
        `scratch_dir`) is tampering: it raises `CapabilityViolation`, and no
        `chmod` follows from it. So is a ledger that is a symbolic link, is
        not a regular file, has more than one name or cannot be read
        (`durable_io.read_single_link`), and so is the `planned` line that
        opens a seal for a name this run's `_arena` does not give to the
        iteration and attempt on that line (`arena_name`): a plan is honoured
        only for the arena it was made for. `state` is the run's state (read
        from the store when not given): it holds the run's arena secret.
        """
        from .capability import SealEntry
        from .durable_io import read_single_link

        path = self.store.dir / SEAL_LEDGER
        try:
            raw = read_single_link(path)
        except OSError as exc:
            raise CapabilityViolation(
                f"the seal ledger {path} cannot be trusted ({exc}); nothing was "
                f"unsealed") from exc
        if raw is None:
            return {}
        text = raw.decode("utf-8", errors="replace")
        if state is None:
            state = self.store.read_state()
        bases = {self.store.arenas_dir.resolve()}
        if getattr(self, "scratch_dir", None):
            bases.add(Path(self.scratch_dir).resolve())
        seals: dict[str, dict] = {}
        for line in text.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            event, arena = rec.get("event"), rec.get("arena")
            if event not in ("planned", "sealed", "kept", "touched",
                             "releasing", "released") or not isinstance(arena, str):
                continue
            if not Path(arena).is_absolute() or Path(arena).resolve().parent not in bases:
                raise CapabilityViolation(
                    f"the seal ledger names {arena}, which is not an arena of "
                    f"this run; nothing was unsealed")
            if event == "planned":
                try:
                    plan = {str(rel): SealEntry(*(int(v) for v in entry))
                            for rel, entry in rec["entries"].items()}
                except (KeyError, TypeError, ValueError, AttributeError) as exc:
                    raise CapabilityViolation(
                        f"the seal ledger's plan for {arena} is malformed: "
                        f"{exc}") from exc
                if arena in seals:
                    continue              # only the first `planned` of an open seal counts
                iteration, attempt = rec.get("iteration"), rec.get("attempt")
                if (state.arena_secret is None
                        or not _is_count(iteration) or not _is_count(attempt)
                        or Path(arena).name != arena_name(
                            state.arena_secret, iteration, attempt)):
                    raise CapabilityViolation(
                        f"the seal ledger's plan for {arena} is not bound to an "
                        f"arena this run sealed (iteration {iteration!r}, attempt "
                        f"{attempt!r} do not give that name); nothing was unsealed")
                seals[arena] = {"plan": plan, "frozen": None, "touched": None,
                                "releasing": False}
            elif arena not in seals:
                continue
            elif event == "sealed":
                # Only the first usable digest is the seal's. A later line,
                # or one with an empty or missing digest, cannot replace it.
                frozen = rec.get("frozen")
                if seals[arena]["frozen"] is None and isinstance(frozen, str) and frozen:
                    seals[arena]["frozen"] = frozen
            elif event == "touched":
                # A record, never a baseline: the touched tree is compared
                # against the seal's own digest at every later comparison.
                frozen = rec.get("frozen")
                if isinstance(frozen, str) and frozen:
                    seals[arena]["touched"] = frozen
            elif event == "releasing":
                seals[arena]["releasing"] = True
            elif event == "released":
                seals.pop(arena)
        return seals

    def _compare_open(self, state: RunState | None, seals: dict) -> list[str]:
        """Compare every open seal that can be compared; return the touched arenas.

        Never unseals. A seal marked `releasing` was already compared (or
        needed no comparison), and one with no digest was sealed mid-way, so
        QA never started on it: neither is compared. A touch is **reported at
        every comparison** (the digest it is compared with never moves); the
        ledger gets one `touched` line per distinct state found, so repeated
        comparisons do not grow it.
        """
        from . import capability as cap

        touched: list[str] = []
        for arena, seal in seals.items():
            if seal["releasing"] or seal["frozen"] is None:
                continue
            now = cap.frozen_digest(arena)
            if now == seal["frozen"]:
                continue
            touched.append(arena)
            if now != seal["touched"]:
                try:
                    self._seal_ledger("touched", state, arena=arena, frozen=now)
                except Exception as exc:      # noqa: BLE001 - the violation travels
                    if state is not None:
                        state.note(f"seal ledger: recording the touch of {arena} "
                                   f"failed: {exc}"[:400])
        return touched

    def _check_open_seals(self, state: RunState) -> None:
        """Compare every arena still sealed; a touch is a violation.

        A seal whose last event is `releasing` was already compared, or
        needed no comparison: its unseal was cut short, and it is finished
        here. Every other open seal is compared against its recorded digest
        and **stays sealed** -- the pane that may write into it may still be
        open. A touch is recorded (`touched`) and raises
        `CapabilityViolation`.
        """
        seals = self._open_seals(state)
        for arena, seal in seals.items():
            if seal["releasing"]:
                self._seal_released(state, Path(arena), seal["plan"])
        touched = self._compare_open(state, seals)
        if touched:
            why = ("a sealed candidate arena was touched while it stayed sealed "
                   "after QA's dispatch: " + ", ".join(touched))
            state.note(f"capability violation: {why}")
            raise CapabilityViolation(why)

    def compare_open_seals(self) -> dict:
        """Compare every open seal, without unsealing any; a touch blocks the run.

        For the end of a `hoh run` that leaves its seals in place (tabs kept
        open, a tab not confirmed closed, a release that failed): a touch is
        found then, not at some later verification, and the run is blocked
        with the capability-violation kind. A ledger that names a path
        outside this run's arenas blocks it too.
        """
        with self.store.lock():
            state = self.store.read_state()
            try:
                seals = self._open_seals(state)
            except CapabilityViolation as exc:
                state.note(f"capability violation: {exc}")
                self._block_violation(state, f"capability violation: {exc}")
                self.store.write_state(state)
                return {"touched": [], "open": [], "refused": str(exc)}
            touched = self._compare_open(state, seals)
            if touched:
                why = ("a sealed candidate arena was touched while its seal "
                       "stayed in place: " + ", ".join(touched))
                state.note(f"capability violation: {why}")
                self._block_violation(state, f"capability violation: {why}")
            if touched:
                self.store.write_state(state)
            return {"touched": touched, "open": list(seals)}

    def release_open_seals(self) -> dict:
        """Unseal every arena the ledger leaves sealed, comparing each first.

        Called by `hoh run` only after its own tabs closed and none is left
        unconfirmed (`cli._close_own_and_release`). A dispatcher that can say
        whether the run's QA agent is gone (`role_gone`) is asked as well; while
        it is not gone, nothing is lifted. A touch, or a ledger that names a
        path outside this run's arenas, blocks the run.
        """
        from . import capability as cap

        with self.store.lock():
            state = self.store.read_state()
            try:
                seals = self._open_seals(state)
            except CapabilityViolation as exc:
                state.note(f"capability violation: {exc}")
                self._block_violation(state, f"capability violation: {exc}")
                self.store.write_state(state)
                return {"released": [], "touched": [], "refused": str(exc)}
            if not seals:
                return {"released": [], "touched": []}
            gone = getattr(self.dispatcher, "role_gone", None)
            if callable(gone) and not gone(Role.QA, state):
                # The agent may still act, so nothing is lifted -- but a touch
                # already made is not hidden by its presence.
                present = self._compare_open(state, seals)
                if present:
                    why = ("a sealed candidate arena was touched while the QA "
                           "agent was still present: " + ", ".join(present))
                    state.note(f"capability violation: {why}")
                    self._block_violation(state, f"capability violation: {why}")
                    self.store.write_state(state)
                return {"released": [], "touched": present, "kept": list(seals)}
            released, touched = [], []
            for arena, seal in seals.items():
                hit = (not seal["releasing"] and seal["frozen"] is not None
                       and cap.frozen_digest(arena) != seal["frozen"])
                if hit:
                    touched.append(arena)
                self._seal_releasing(state, Path(arena))
                self._seal_released(state, Path(arena), seal["plan"],
                                    on_close=True, touched=hit)
                released.append(arena)
            state.note(f"candidate arena seal(s) released after the tabs closed: "
                       f"{len(released)}")
            if touched:
                why = ("a sealed candidate arena was touched before the QA pane "
                       "closed: " + ", ".join(touched))
                state.note(f"capability violation: {why}")
                self._block_violation(state, f"capability violation: {why}")
            self.store.write_state(state)
            return {"released": released, "touched": touched}

    def _arena(self, state: RunState, *, suffix: str = "",
               base: Path | None = None) -> Path:
        """A separate verification directory per iteration and attempt.

        The arena lies **next to** the run directory, not inside it.
        Previously it lay under `runs/<id>/arena/`; from there a check command
        reached the preservation suite with `../../checks.json`, the state
        with `../../state.json` and the evidence with `../../receipts/`. A
        reviewer emptied the suite from inside the object under verification
        -- the next iteration ran without regression protection and accepted a
        real regression.

        `assert_stays_in_arena` rejects `..` by now. This separation is the
        second line: it makes the class of attack structurally impossible
        instead of closing it pattern by pattern. Above an arena there are now
        only other arenas.
        """
        base = base or self.scratch_dir or (self.store.arenas_dir)
        # The directory name does **not** reveal whether this is the candidate
        # or the control state. Previously it was called "iN-aM-basis", and a
        # criterion could key on that:
        #
        #     case "$PWD" in *-basis) exit 1 ;; *) exit 0 ;; esac
        #
        # That left the mechanism which is supposed to prove that something
        # has changed blind against exactly the attacker it was built against
        # -- a reviewer got an iteration accepted without any change at all.
        # The name is now a digest over a run secret; it is stable (repeatable
        # runs) and indistinguishable.
        name = arena_name(state.arena_secret, state.iteration, state.attempt, suffix)
        arena = Path(base) / name
        if arena.exists():
            # No deleting: the previous run is parked with a version suffix.
            # The timestamp makes the name unique -- a mere counter collided
            # after a prune(), because it jumps back.
            arena.rename(arena.with_name(f"{arena.name}.v{utcnow().replace(':', '-')}"))
        arena.mkdir(parents=True, exist_ok=True)
        # STORAGE-B3: remembered (in memory only) so that dispatch sizing can
        # measure this run's own arenas under a `scratch_dir` one by one --
        # never the scratch_dir itself, which another run may share.
        if not hasattr(self, "_own_arenas"):
            self._own_arenas = []
        if arena not in self._own_arenas:
            self._own_arenas.append(arena)
        return arena

    def _execute(
        self, state: RunState, check: AcceptanceCheck, candidate: Candidate
    ) -> tuple[Receipt, str]:
        try:
            with self._check_workspace(state) as scratch:
                try:
                    return run_check(
                        check,
                        candidate,
                        run_id=state.run_id,
                        iteration=state.iteration,
                        attempt=state.attempt,
                        cwd=candidate.repo_path,  # is the arena (probe), never the live tree
                        isolation=self.isolation,
                        scratch=scratch,
                    )
                except ValueError as exc:
                    if scratch is None:
                        raise
                    raise _ScratchRefused(str(exc)) from exc
        except PolicyUnavailable:
            # Without a policy nothing is executed -- and that is a visible
            # block, not a traceback in the middle of VERIFYING.
            raise
        except (HouseRuleViolation, ArenaEscape, _ScratchRefused) as exc:
            # A criterion that tried to bypass the guards is not a criterion.
            #
            # `ArenaEscape` was missing here and escaped `run_iteration` as an
            # uncaught exception -- the run stalled in VERIFYING/ACTIVE,
            # without a verdict and without a block. The sibling case was
            # cleanly recorded as runner_ok=False; a pair of reviewers
            # reproduced the difference.
            receipt = Receipt(
                receipt_id=f"{state.run_id}-i{state.iteration}-a{state.attempt}-{check.check_id}",
                run_id=state.run_id,
                iteration=state.iteration,
                attempt=state.attempt,
                check_id=check.check_id,
                candidate_binding=candidate.binding(),
                command=check.command,
                exit_code=126,  # "found, but not executable"
                started_at=utcnow(),
                ended_at=utcnow(),
                stdout_digest=digest(str(exc)),
                runner_identity="guard",
                runner_ok=False,   # the check never ran -- never a product verdict
            )
            kind = ("ARENA ESCAPE" if isinstance(exc, ArenaEscape)
                    else "SCRATCH REFUSED" if isinstance(exc, _ScratchRefused)
                    else "HOUSE RULE VIOLATION")
            log = f"{kind}, not executed:\n{exc}\n"
            # The evidence carries the digest **of the log**, not that of the
            # exception. Previously the two diverged, and `verify_log`
            # reported "log deviates from the evidence" for every blocked
            # check -- that is, "manipulated log" instead of the true cause.
            receipt = receipt.model_copy(update={"stdout_digest": digest(log)})
            return receipt, log

    def _parse_qa(
        self,
        raw: str,
        *,
        plan: DevelopmentPlan,
        receipts: list[Receipt],
        state: RunState,
        candidate: Candidate,
        runner_verdicts: dict[str, CheckVerdict],
        checked: list[AcceptanceCheck],
        blind: dict[str, str],
        discriminating: set[str] | None = None,
        outage: str = "",
    ) -> tuple[list[CheckVerdict], list[str], str]:
        """Evaluates the QA output -- and does not trust it.

        Every claimed PASS has to be covered by a receipt that *this* runner
        produced for *this* candidate. A PASS without evidence is downgraded
        to INCONCLUSIVE, not discarded: the observation stays visible, but the
        claim does not carry.
        """
        known = {r.receipt_id: r for r in receipts}
        try:
            data = roles.extract_json(raw)
        except roles.RoleOutputError as exc:
            # No usable QA output. Previously the runner verdicts fell through
            # here -- and for green checks those are PASS. A crashed,
            # refusing or merely prosaic QA agent thereby forced acceptance by
            # not answering. The runner evidence can **refute**, never
            # confirm: acceptance presupposes a spoken assessment by the
            # independent role (paper §3.4.3).
            no_verdict_reason = (
                f"QA outage, no verdict: {outage}" if outage else "not assessed by QA"
            )
            return (
                [
                    self._downgrade(v, reason=no_verdict_reason)
                    for v in runner_verdicts.values()
                ],
                [
                    f"QA did not answer: {outage}"
                    if outage
                    else f"QA delivered no evaluable report: {exc}"
                ]
                + [f"{cid}: {r}" for cid, r in blind.items()],
                (
                    f"{NO_QA_VERDICT} -- QA did not answer ({outage}). "
                    "The criteria are therefore unverified, not failed."
                    if outage
                    else "QA output unreadable; no assessment, only runner evidence"
                ),
            )

        verdicts: list[CheckVerdict] = []
        seen: set[str] = set()
        for row in data.get("verdicts", []) or []:
            if not isinstance(row, dict):
                continue
            check_id = str(row.get("check_id", ""))
            if check_id not in {c.check_id for c in checked}:
                continue
            seen.add(check_id)
            claimed = str(row.get("outcome", "INCONCLUSIVE")).upper()
            receipt_id = row.get("receipt_id") or None
            note = row.get("note") or None
            repro = row.get("reproduction") or None

            outcome = Outcome.INCONCLUSIVE
            if claimed == "PASS":
                reason = self._pass_supported(
                    check_id=check_id,
                    receipt_id=receipt_id,
                    known=known,
                    checked=checked,
                    state=state,
                    candidate=candidate,
                )
                if reason is None:
                    outcome = Outcome.PASS
                else:
                    note = f"PASS rejected ({reason}). {note or ''}".strip()
                    receipt_id = None
            elif claimed == "FAIL":
                outcome = Outcome.FAIL
                receipt_id = receipt_id if receipt_id in known else None

            if outcome is not Outcome.PASS:
                receipt_id = receipt_id if receipt_id in known else None

            verdicts.append(
                CheckVerdict(
                    check_id=check_id, outcome=outcome, receipt_id=receipt_id,
                    reproduction=repro, note=note,
                )
            )

        # Criteria on which QA stayed silent are open -- genuinely so.
        # Previously there was a fallback to runner_verdicts here that
        # silently turned a green runner result into a PASS: code and comment
        # contradicted each other, and a planner could win by omission.
        # Silence and outage are not the same thing. "QA checked and said
        # nothing about this criterion" is a finding; "QA never answered" is
        # not. The difference belongs in the verdict.
        no_verdict_reason = (
            f"QA outage, no verdict: {outage}" if outage else "not assessed by QA"
        )
        for check in checked:
            if check.check_id not in seen:
                runner = runner_verdicts.get(check.check_id)
                verdicts.append(
                    self._downgrade(runner, reason=no_verdict_reason)
                    if runner is not None
                    else CheckVerdict(
                        check_id=check.check_id,
                        outcome=Outcome.INCONCLUSIVE,
                        note=no_verdict_reason,
                    )
                )

        # A criterion that was already green on the predecessor state may stay
        # green -- but it demonstrates no increment. It is therefore marked as
        # a preservation criterion, not devalued. Whether the run can be
        # accepted with it is decided by `RoleResult.accepted()`: that
        # requires at least one discriminating criterion.
        final_verdicts: list[CheckVerdict] = []
        for v in verdicts:
            blind_reason = blind.get(v.check_id)
            if blind_reason and v.outcome is Outcome.PASS:
                final_verdicts.append(
                    CheckVerdict(
                        check_id=v.check_id,
                        outcome=v.outcome,
                        receipt_id=v.receipt_id,
                        reproduction=v.reproduction,
                        discriminates=False,
                        note=(
                            f"preservation criterion, demonstrates no increment: "
                            f"{blind_reason}. {v.note or ''}"
                        ).strip(),
                    )
                )
            else:
                # `discriminates` is **earned**, not set by default: only
                # where the base check really saw the criterion red.
                # Everything else -- preservation criteria, unmeasured ones,
                # non-evaluable ones -- carries False.
                final_verdicts.append(v.model_copy(update={
                    "discriminates": v.check_id in (discriminating or set())
                }))

        gaps = [str(g) for g in (data.get("open_gaps") or []) if str(g).strip()]
        # A preservation criterion is not a gap. The situation only becomes a
        # gap when **not a single** criterion demonstrates the increment --
        # then the plan measures nothing that has changed.
        if not any(v.discriminates for v in final_verdicts):
            gaps.append(
                "No criterion demonstrates the increment: all of them were "
                "already green on the predecessor state. The plan has to measure "
                "what has changed. "
                + "; ".join(f"{cid}: {reason}" for cid, reason in blind.items())
            )
        return final_verdicts, gaps, str(data.get("summary") or "")

    # -- Telemetry ------------------------------------------------------------ #

    def telemetry(self) -> TelemetryLog:
        """The append-only dispatch log for this run, beside its other evidence."""
        return TelemetryLog(self.store.dir / "telemetry.jsonl")

    def note_dispatch(
        self,
        *,
        role: str,
        run_id: str,
        iteration: int,
        attempt: int,
        started_at: str,
        ended_at: str,
        backend: str = "",
        outcome: str = "ok",
        detail: str = "",
        usage: dict | None = None,
        state: RunState | None = None,
        exc: BaseException | None = None,
        **rest,
    ) -> None:
        """Records one role dispatch. Never raises: telemetry is a record of
        the work, not a precondition for it (see `telemetry.py`). A write
        failure is noted on `state`, when one was given, and swallowed.

        Three fields are filled here rather than left at their defaults, all
        for the same reason: a record model that refuses to lie, wired to a
        caller that passes it nothing, produces a log of well-typed blanks
        (O128). `provider` is the harness profile the role actually ran under;
        `model` is `NOT_AVAILABLE` when the backend cannot report one, which
        is an answer, where an empty string was not; and a failed dispatch
        gets a class from the taxonomy instead of a `None` that reads as a
        failure nobody looked at.
        """
        try:
            from .taxonomy import classify_failure

            sender = getattr(self, "dispatcher", None)
            try:
                lookup_key = role if isinstance(role, Role) else Role(role)
            except ValueError:
                # "gate", "merge", "closure" are documented roles of this
                # record and are not dispatch roles. They keep their string
                # key rather than losing their identity: writing
                # NOT_AVAILABLE while the answer sits in the same object is
                # the false statement this whole block exists to remove.
                lookup_key = role
            # The dispatcher was *given* these -- `cli.py` fills them from
            # --role-model and --role-effort and hands them to the agent CLI.
            # Writing NOT_AVAILABLE while the answer sits in the same object
            # is not an unknown, it is a false statement.
            def _configured(name: str) -> str:
                table_ = getattr(sender, name, None)
                if not isinstance(table_, dict):
                    return ""
                return table_.get(lookup_key) or ""
            rest.setdefault("provider",
                            _configured("profiles") or NOT_AVAILABLE)
            rest.setdefault("model", _configured("models") or NOT_AVAILABLE)
            rest.setdefault("effort", _configured("efforts") or NOT_AVAILABLE)
            normalized = None
            if outcome != "ok":
                normalized = failures.classify(exc, detail=detail)
                rest.setdefault("normalized_failure", normalized)
                rest.setdefault("failure_class",
                                failures.TAXONOMY.get(normalized)
                                or classify_failure(exc, detail=detail))
            # P2-43b: who ran this, under which account and policy -- on every
            # dispatch of a dispatch role, failed or not.
            prov = dispatchrecovery.provenance_for(
                lookup_key, provider=rest["provider"], model=rest["model"],
                context=getattr(self, "recovery", None), failure=normalized)
            rest.setdefault("provenance",
                            prov.model_dump(mode="json") if prov is not None else None)
            scope_ = getattr(self, "_witness_scope", None)
            if scope_ is not None:
                rest.setdefault("witnessed_trees", scope_[0])
                rest.setdefault("witnessed_listings", scope_[1])
            # Retries were counted in `state.usage` and nowhere else, so every
            # record read `retries: 0` on a run that had retried twice -- and
            # the log's stated purpose is "how much did that take".
            rest.setdefault("retries", getattr(self, "_last_retries", 0))
            # The number this record contributes to "what did this run cost".
            # Not derivable from the line's existence in either direction.
            #
            # Only for the three dispatch roles. "gate", "merge" and "closure"
            # are documented roles of this record that reach no provider, and
            # they do not reset the counter either -- so filling it for them
            # would attach the *previous* role's calls to a line that made
            # none, and the sum over the log would count those calls twice.
            if isinstance(lookup_key, Role):
                rest.setdefault("provider_calls",
                                getattr(self, "_last_calls", 0))
            else:
                rest.setdefault("provider_calls", 0)
            # `DispatchRecord` forbids extra fields, and `rest` was passed
            # straight through -- so one plausible-but-wrong keyword name made
            # the whole line disappear, silently when no `state` was given. A
            # record lost to a typo is the same defect as a well-typed blank.
            from .telemetry import DispatchRecord

            known_nodes = set(DispatchRecord.model_fields)
            foreign = sorted(k for k in rest if k not in known_nodes)
            for k in foreign:
                rest.pop(k)
            if foreign and state is not None:
                state.note(
                    f"telemetry: {role} dispatch record does not carry "
                    + ", ".join(foreign) + " -- no such field, dropped"
                )
            record = record_from_role(
                role=role, run_id=run_id, iteration=iteration, attempt=attempt,
                started_at=started_at, ended_at=ended_at, backend=backend,
                outcome=outcome, detail=detail, usage=usage, **rest,
            )
            self.telemetry().append(record)
        except Exception as exc:
            if state is not None:
                state.note(f"telemetry not written for {role} dispatch: {exc}")

    # -- Dispatch with one repair round -------------------------------------- #

    def _register_intent(self, role: Role, state: RunState) -> TaskRef:
        """Persist the intent to start **before** the dispatch.

        Handoff §7: *"A unique attempt key and a persisted intent to start
        prevent duplicate worker starts after a crash."* Without it
        `active_tasks` was a field without a writer: after a crash there was
        no way to decide whether a role had already started running.
        """
        key = digest(f"{state.run_id}:{state.iteration}:{state.attempt}:{role.value}")
        ref = TaskRef(
            task_id=f"{state.run_id}-i{state.iteration}-a{state.attempt}-{role.value}",
            role=role,
            harness=getattr(self.dispatcher, "profiles", {}).get(role, "unknown"),
            attempt_key=key,
            intent_recorded_at=utcnow(),
        )
        state.active_tasks = [t for t in state.active_tasks if t.attempt_key != key]
        state.active_tasks.append(ref)
        self.store.write_state(state)
        return ref

    def _retire_task(self, ref: TaskRef, state: RunState, *, endpoint: str) -> None:
        for t in state.active_tasks:
            if t.attempt_key == ref.attempt_key:
                t.confirmed_running = False
                t.herdr_pane_id = endpoint
        state.active_tasks = [t for t in state.active_tasks if t.attempt_key != ref.attempt_key]
        self.store.write_state(state)

    def _receipts_written(self, state: RunState) -> int:
        """Receipt files this iteration and attempt actually produced.

        Counted on disk rather than from the candidate loop's own list. That
        list holds only the candidate's receipts; the baseline receipts
        `_discriminates` writes and any refusal receipt are written during the
        same verification and were not counted -- a run with two receipt files
        reported one, which is the same shape as reporting zero, one step
        smaller.
        """
        directory = self.store.dir / "receipts"
        if not directory.is_dir():
            return 0
        prefix_ = f"{state.run_id}-i{state.iteration}-a{state.attempt}-"
        return sum(1 for f in directory.glob("*.json")
                   if f.name.startswith(prefix_))

    def _amendment_chain(self, state: RunState):
        """The run's amendment chain, or `None` if it does not belong to it.

        Provenance is checked, not assumed. The chain is an unsigned file in a
        directory the roles can reach, and it decides both what the run is
        measured against and whether an acceptance may stand -- so a chain
        naming a different run, or one whose parked predecessor is missing or
        does not hash to what it claims, is not this run's chain and is
        ignored with a note rather than applied.
        """
        from .amendment import text_digest

        try:
            chain = self.store.read_amendments(origin_digest=state.spec_digest)
        except Exception as exc:                 # noqa: BLE001 - a chain
            # that cannot be read must not stop a run; it must not be applied
            # either, and the note is how a reader finds out which happened.
            state.note(f"amendment chain unreadable, ignored: {exc}")
            return None
        if not chain.amendments:
            return None
        if chain.run_id != state.run_id:
            state.note(
                f"amendment chain names run {chain.run_id!r}, not "
                f"{state.run_id!r} -- ignored"
            )
            return None
        for a in chain.amendments:
            old = Path(a.from_path) if a.from_path else None
            if old is None or not old.is_file():
                state.note(
                    f"{a.amendment_id}: the superseded text it names is not "
                    f"there ({a.from_path or 'unnamed'}) -- chain ignored"
                )
                return None
            if text_digest(old.read_text(encoding="utf-8")) != a.from_digest:
                state.note(
                    f"{a.amendment_id}: the parked text does not hash to "
                    f"{a.from_digest} -- chain ignored"
                )
                return None
        return chain

    def _contract_governor(self, state):
        """The pinned contract's governor, bound once per controller, or None."""
        from .governance import ContractRefused, bind

        if self._governor is None:
            try:
                self._governor = bind(self.store.dir)
            except ContractRefused as exc:
                state.note(f"autonomy: the pinned contract is unusable ({exc}); "
                           f"blocking without it")
                self._governor = False          # asked once, answered once
        return self._governor or None

    def _failure_wait(self, state, exc: BaseException,
                      failure: failures.NormalizedFailure) -> LoopOutcome:
        """A failed dispatch that is a wait or an operator action (P2-43a).

        Not a verdict and not an iteration: the candidate is not rejected, the
        working tree is not touched, and a PLANNING charge is handed back
        (resuming charges it again through `stages.begin_iteration`). An
        automatic class blocks durably with its own `blocked_kind`, a
        `retry_after` and a PROVIDER_RETRY timer, outside the contract's
        decision routing; an operator-required class blocks for a person.
        """
        mode = failures.RETRY_MODE[failure]
        role = {Stage.PLANNING: Role.PLANNER, Stage.DEVELOPING: Role.DEVELOPER,
                Stage.VERIFYING: Role.QA}.get(state.stage)
        role_name = role.value if role is not None else state.stage.value.lower()
        first = (str(exc).strip().splitlines() or [""])[0][:300]
        if state.stage is Stage.PLANNING and state.usage.iterations > 0:
            state.usage.iterations -= 1
            state.note(f"iteration {state.iteration}: charge returned -- it ended "
                       f"in {failure.value} before a plan existed")
        state.note(f"failure class {failure.value} ({mode.value}) in {role_name}: {first}")
        held = stages.violation_unresolved(state)
        automatic = mode is failures.RetryMode.AUTOMATIC and not held
        retry_at = (failures.next_attempt(failure, str(exc)).isoformat()
                    if automatic else None)
        # P2-43b: the receipt first, then the timer, then the parked state --
        # a parked state is never durable without its record and its wake-up.
        # A park that cannot be recorded cannot be bound, and a wait nobody can
        # bind must not wake itself: it needs a person.
        recorded = self._record_park(state, failure, role, retry_at=retry_at,
                                     held=held, mode=mode)
        if automatic and recorded is None:
            # No class name in the reason: `stages.block` reads a quota phrase
            # anywhere in a reason, `RATE_LIMIT` is one, and it would arm the very
            # timer this branch exists to refuse. The class is in the note above.
            reason = (f"a provider wait in {role_name}; the recovery record could not "
                      f"be written, so no automatic retry")
            self._block(state, reason)
            self._refuse_automatic_kind(state)
        elif automatic:
            reason = f"{failure.value} in {role_name}, automatic retry"
            state.condition = Condition.BLOCKED
            state.blocked_reason = state.stop_reason = reason
            state.blocked_kind = failures.blocked_kind(failure)
            state.retry_after = retry_at
            state.note(f"BLOCKED ({state.blocked_kind}, earliest retry "
                       f"{state.retry_after}): not a decision and not a verdict; "
                       f"`hoh resume-quota` resumes it")
            self._arm_quota_timer(state, self._contract_governor(state))
        elif mode is failures.RetryMode.AUTOMATIC:
            reason = (f"a provider wait in {role_name}; a capability violation "
                      f"still holds this run, so no automatic retry")
            self._block(state, reason)
        else:
            # The provider's text is in the note, not in the reason:
            # `stages.block` reads a quota phrase anywhere in a reason.
            reason = f"{failure.value} in {role_name}, operator action required"
            self._block(state, reason)
        self.store.write_state(state)
        return LoopOutcome(state.iteration, False, f"no verdict -- {reason}")

    @staticmethod
    def _refuse_automatic_kind(state) -> None:
        """Belt and braces for the unrecordable park: whatever `_block` made of
        the reason, the run must not carry an automatic kind or a `retry_after`
        (a state that reads as a wait with no receipt behind it)."""
        if state.blocked_kind in failures.AUTOMATIC_KINDS | {"usage_limit"}:
            state.blocked_kind = None
        state.retry_after = None

    def _record_park(self, state, failure, role, *, retry_at, held, mode):
        """Append the park's receipt (`RoutingDecision` for an automatic wait,
        `RoutingEscalation` for a person) beside the run state. Returns the
        record, or None -- noted on `state` -- when it could not be written."""
        try:
            governor = self._contract_governor(state)
            binding = dispatchrecovery.park_binding(
                state, governor=governor,
                master=dispatchrecovery.read_master(self.store.dir))
            provider, model = self._role_identity(role)
            prov = dispatchrecovery.provenance_for(
                role, provider=provider, model=model, context=getattr(self, "recovery", None),
                failure=failure)
            if prov is None:
                raise dispatchrecovery.RecoveryError(
                    f"stage {state.stage.value} has no dispatch role")
            pid = dispatchrecovery.park_id(binding, failure, retry_at)
            if retry_at is not None:
                record = dispatchrecovery.RoutingDecision(
                    park_id=pid, reason=failure.value, binding=binding,
                    provenance=prov, retry_at=retry_at)
            else:
                record = dispatchrecovery.RoutingEscalation(
                    park_id=pid, reason=failure.value, binding=binding, provenance=prov,
                    escalation="HELD_BY_VIOLATION" if held else "OPERATOR_REQUIRED")
            dispatchrecovery.append_record(self.store.dir, record)
            state.note(f"routing receipt {record.kind} {pid[:12]} "
                       f"(policy {record.route_policy_id}/{record.route_policy_version}, "
                       f"master {binding.master.kind})")
            return record
        except Exception as rec_exc:              # noqa: BLE001 - noted, never fatal here
            # A validation error quotes its input; the type is enough.
            why = ("" if isinstance(rec_exc, ValueError) and not isinstance(
                rec_exc, dispatchrecovery.RecoveryError) else f": {str(rec_exc)[:200]}")
            state.note(f"recovery record NOT written ({type(rec_exc).__name__}{why})")
            return None

    def _role_identity(self, role) -> tuple[str, str]:
        """(provider, model) the dispatcher was configured with for a role."""
        sender = getattr(self, "dispatcher", None)

        def configured(name: str) -> str:
            table_ = getattr(sender, name, None)
            return (table_.get(role) or "") if isinstance(table_, dict) else ""
        return (configured("profiles") or NOT_AVAILABLE,
                configured("models") or NOT_AVAILABLE)

    def _block_violation(self, state, reason: str):
        """Block for a capability violation, with the kind that keeps it held."""
        state.blocked_kind = stages.CAPABILITY_VIOLATION
        return self._block(state, reason)

    def _block(self, state, reason: str, *, scan_for_quota: bool = True):
        """Block, under the contract if this run has one.

        ``scan_for_quota=False`` for a reason that carries text the product did
        not write -- an exception whose message names a path a role created or a
        file name a role chose (`rate_limit.py` is a quota phrase to
        `stages.block`)."""
        from .governance import block_under_contract

        governor = self._contract_governor(state)
        routing = block_under_contract(state, reason, governor,
                                       scan_for_quota=scan_for_quota)
        if state.blocked_kind == "usage_limit" and state.retry_after:
            self._arm_quota_timer(state, governor)
        return routing

    def _arm_quota_timer(self, state, governor) -> None:
        """A durable PROVIDER_RETRY timer for a quota wait (O211, DS3).

        `retry_after` alone made the wait visible only to a scan of run states;
        the resume loop consumes *timers*, exactly once, under the programme
        epoch the run belongs to. A failure to arm is noted and not fatal: the
        run keeps `retry_after`, which the loop's legacy path still honours.
        """
        from .resumeloop import _timer_form
        from .timers import TimerKind, TimerStore

        due = _timer_form(state.retry_after)
        try:
            TimerStore(self.store.dir.parent).arm_new(
                kind=TimerKind.PROVIDER_RETRY, due_at=due,
                reason=(f"{(state.blocked_kind or 'usage_limit').replace('_', ' ')}: "
                        f"{(state.blocked_reason or '')[:200]}"),
                program_id=(governor.contract.contract_id if governor else ""),
                program_epoch=(governor.contract.program_epoch if governor else 0),
                run_id=state.run_id)
        except Exception as exc:                  # noqa: BLE001 - noted, not fatal
            state.note(f"quota: the retry timer could not be armed ({exc}); the "
                       f"retry_after path still applies")

    def _reopen_amended_criteria(self, chain, state: RunState) -> None:
        """Drop the criteria an amendment reopened from the preservation suite.

        Without this the gate is satisfiable only by the *old* measurement.
        `_checks_for` freezes a preserved criterion against redefinition -- for
        good reason, it is how a plan is stopped from watering one down -- and
        an amendment's affected criteria are by construction ones that already
        passed, so they are all preserved. The planner would re-plan the
        criterion with the new command, the frozen version would silently
        apply, and the receipt would record the pre-amendment command. An
        adversarial reviewer rode exactly that to an acceptance in which the
        new requirement was never checked.

        Reopening means the criterion goes back to being a *fresh* one: it has
        to discriminate again, and its new definition is the one that counts.
        """
        needed = set(chain.revalidation_needed()) - set(state.revalidated)
        if not needed:
            # Cleared, not left standing: the planner prompt would otherwise
            # keep naming criteria that have already been answered, and a
            # prompt that asks for work already done is a prompt a role has to
            # guess its way past.
            self._reopened = None
            return
        suite = self.store.read_checks()
        removed_ = sorted(needed & set(suite))
        if not removed_:
            return
        for cid in removed_:
            del suite[cid]
        self.store.write_checks(suite)
        state.note(
            "reopened by amendment, removed from the preservation suite so the "
            "new text can be measured: " + ", ".join(removed_)
        )
        self._reopened = sorted(needed)

    def _reopen_pin_amended(self, chain, state: RunState, spec_text: str) -> None:
        """An amendment that changes the specification's `test-pins` block is acceptance-affecting
        whatever its declared kind (FID1): the tests the criteria run are now other bytes, so
        every criterion is planned and measured again before an acceptance stands, and the new
        pins are never adopted silently by a `clarify`.

        Which amendments changed the block is read from the parked texts the chain already
        proves (`_amendment_chain` hashed each of them); a text that cannot be read counts as
        changed. The reopened criteria leave the preservation suite exactly as
        `_reopen_amended_criteria` does, and are answered once per amendment
        (`state.revalidated` holds `<amendment id>:<check id>`)."""
        self._pin_needed = {}
        if chain is None:
            return
        try:
            texts = [Path(a.from_path).read_text(encoding="utf-8") for a in chain.amendments]
            changed = [a.amendment_id
                       for a, before, after in zip(chain.amendments, texts, texts[1:] + [spec_text])
                       if spec_criteria.pins_block(before) != spec_criteria.pins_block(after)]
        except (OSError, ValueError):
            changed = [a.amendment_id for a in chain.amendments]
        if not changed:
            return
        try:
            declared = {c.criterion_id for c in spec_criteria.declared_criteria(spec_text)}
        except spec_criteria.CriteriaBlockError:
            declared = set()
        suite = self.store.read_checks()
        needed: dict[str, set[str]] = {}
        for cid in declared | set(suite):
            keys = {f"{aid}:{cid}" for aid in changed} - set(state.revalidated)
            if keys:
                needed[cid] = keys
        if not needed:
            return
        self._pin_needed = needed
        removed = sorted(set(needed) & set(suite))
        for cid in removed:
            del suite[cid]
        if removed:
            self.store.write_checks(suite)
        state.note("test-pins block amended (" + ", ".join(changed) + "): every criterion is "
                   "measured again before an acceptance stands: " + ", ".join(sorted(needed)))
        self._reopened = sorted(set(getattr(self, "_reopened", None) or []) | set(needed))

    def _revalidation_open(self, chain, state: RunState,
                            plan: DevelopmentPlan, result) -> set[str]:
        """Criteria an amendment reopened that this iteration has not answered.

        A criterion counts as answered when this iteration both **planned** it
        and produced a verdict for it. Measuring it is not enough on its own:
        a plan that quietly drops the criterion would otherwise satisfy the
        requirement by never asking, which is the shape of the missing
        preservation suite that O129 rode to a false checkpoint.
        """
        needed = set(chain.revalidation_needed()) if chain is not None else set()
        needed -= set(state.revalidated)
        needed |= set(getattr(self, "_pin_needed", {}))     # FID1: reopened by a pins amendment
        if not needed:
            return set()
        planned_ = {c.check_id for c in plan.acceptance_checks}
        answered_ = {v.check_id for v in result.verdicts
                       if v.outcome.value in ("PASS", "FAIL")}
        return {c for c in needed if c not in planned_ or c not in answered_}

    def _begin_count(self) -> None:
        """Start counting this role's provider calls at zero.

        Called by the role methods rather than by `_dispatch`, because one role
        attempt can be several dispatches: a schema repair is a second call for
        the same answer, and resetting inside `_dispatch` would throw the first
        one away. Reset rather than left standing, so a role that never reaches
        a dispatch does not report the previous role's number.
        """
        self._last_retries = 0
        # How many calls actually reached the provider for this role. Recorded
        # because the telemetry log was being *counted* elsewhere as one line
        # per dispatch, and a line is neither: a role that retried twice wrote
        # one line for three calls, and a role refused at the budget wrote one
        # line for none. The benchmark's cost figure was read off those lines.
        self._last_calls = 0
        # Cleared with it: a dispatch refused at the budget check never takes a
        # witness, and the record then carried the *previous* dispatch's
        # coverage. A fabricated measurement in the evidence log is the class
        # of defect the log exists to remove.
        self._witness_scope = None

    def _answer_file(self, role: Role, state: RunState) -> Path:
        """The one file this role's answer may come from, for this attempt.

        Asked of the dispatcher where it can answer, because the dispatcher is
        what writes it; the fallback reproduces the same name rather than
        falling back to a directory. A policy whose `output_channel` names a
        file that never exists -- which is what `answers/{role}.json` was --
        cannot decide anything, and `_answered()` relies on it.
        """
        fetch_ = getattr(self.dispatcher, "_answer_path", None)
        if callable(fetch_):
            return Path(fetch_(role, state))
        return (self.store.dir / "answers"
                / f"i{state.iteration}-a{state.attempt}-{role.value}.json")

    def _policy_for(self, role: Role, state: RunState):
        """The capability policy this role runs under, fail-closed by default.

        A role with no entry here gets `denied_policy`: no writes, no
        capabilities, and every protected tree still verified. Adding a role
        without deciding what it may do therefore stops the run rather than
        inheriting whatever the last role was allowed.
        """
        from . import capability as cap

        reply = self._answer_file(role, state)
        arenas = self.store.arenas_dir
        run = self.store.dir

        # The run's own evidence. Two independent reviews found the same hole
        # here: only `receipts/` was protected, so a planner that emptied
        # `checks.json` during its own dispatch removed the preservation suite
        # and a genuine regression was accepted as a checkpoint -- reproduced
        # end to end, with zero violations noted. `answers/` is excluded
        # because it is how roles answer, and `telemetry.jsonl` because it is
        # appended after the witness is checked.
        # Listed whether or not they exist yet. `tree_digest` returns the
        # empty string for an absent path, and "absent -> present" is a
        # difference the witness reports -- which is the whole point here: a
        # role that *creates* `amendments.json` has written itself a
        # specification change, and filtering by existence let it do exactly
        # that on any run that had never been amended.
        evidenz = (
            run / "state.json", run / "checks.json", run / "evidence.json",
            run / "amendments.json",
            run / "receipts", run / "results", run / "logs",
            # The seal ledger restores and checks QA's sealed arenas; a role
            # that forged a line in it could aim an unseal or hide a touch.
            run / SEAL_LEDGER,
        )
        # The arena root gains a child every iteration, so it cannot be
        # digested whole around a dispatch -- but nothing legitimate creates
        # one *while* a role runs, and a staging directory made then and moved
        # in afterwards is the attack a full digest is designed to miss.
        flat_ = (arenas,) if arenas.is_dir() else ()
        repo = Path(state.repo_path)

        def other_arenas(except_: Path | None = None) -> tuple[Path, ...]:
            if not arenas.is_dir():
                return ()
            return tuple(
                d for d in sorted(arenas.glob("*"))
                if d.is_dir() and d.name != "planner"
                # The scratch siblings are the check commands' `$HOME` and
                # `$TMPDIR` -- pip and pytest caches, measured at up to 118 MB
                # each. They are not evidence about anything, and digesting
                # them twice per dispatch is most of what a witness costs.
                and ".scratch" not in d.name
                and not d.name.startswith(".hoh-scratch-")
                and (except_ is None or d.resolve() != except_.resolve())
            )

        if role is Role.PLANNER:
            # The repository belongs here and did not before: `snapshot()`
            # binds the **working tree**, so a file the planner wrote there
            # during its dispatch landed inside the accepted candidate.
            return cap.planner_policy(
                read_copy=arenas / "planner", answer=reply,
                protected=other_arenas() + evidenz + (repo,),
                shallow=flat_,
            )
        if role is Role.DEVELOPER:
            # The repository is the developer's workspace, so it is the one
            # role for which it is not protected. The arenas of iterations
            # already measured still are.
            return cap.developer_policy(
                arena=repo, answer=reply,
                protected=other_arenas() + evidenz,
                shallow=flat_,
            )
        if role is Role.QA:
            own_ = getattr(self, "_qa_arena", None)
            # No shallow watch on the arena root for QA, and the reason is that
            # the arena root **is** QA's working directory. A reviewer ran
            # `pytest` against the candidate from there -- which is the work QA
            # is sitting there to do -- and `.pytest_cache` appearing beside
            # the arenas failed the iteration. Watching the listing of a role's
            # own cwd is a check that fires on the role doing its job, and the
            # alternative, a list of tool names to ignore, is the growing
            # exclusion list this project rejects elsewhere. Every existing
            # arena except QA's own is still protected in full; what is given
            # up is noticing a *new* directory created during QA's dispatch.
            # Tracked as limit 12d.
            # The arena it reviews is frozen: sealed read-only on disk for the
            # dispatch, and witnessed down to every entry's change time.
            return cap.qa_policy(
                arena=arenas, answer=reply,
                protected=other_arenas(own_) + evidenz + (repo,),
                frozen=(own_,) if own_ is not None else (),
            )
        return cap.denied_policy(
            role.value, protected=other_arenas() + evidenz + (repo,))

    def _dispatches_exhausted(self, state: RunState) -> str | None:
        """Why no further dispatch may be made, or None: the run's own budgets
        (`RunState.budget_exhausted`) first, then the ceiling this process was told
        (`dispatch_ceiling`, P3-INV1). `getattr`: a controller assembled with `__new__`
        (several tests do) has none."""
        own = state.budget_exhausted()
        if own:
            return own
        ceiling = getattr(self, "dispatch_ceiling", None)
        if ceiling is not None and state.usage.dispatches >= ceiling:
            return (f"{DISPATCH_CEILING_REACHED} ({state.usage.dispatches}/{ceiling}): "
                    "the shared dispatch budget holds nothing more for this run")
        return None

    def _budget_or_refusal(self, state: RunState) -> None:
        """Charges one provider call against the budget, or refuses it.

        The order is the whole content of this method, and it was wrong. It
        incremented first and asked afterwards, so `max_dispatches=9` bought
        **eight** calls: the ninth raised its own counter to 9, read `9/9`,
        and refused the call it had just charged for. The advisor's wording is
        the test -- "on spending dispatch 9 there is no tenth" -- so the
        question has to be asked about the state *before* the call: am I
        already at the ceiling? Found by `tools/budget_evidence.py` on a
        fixture that wanted more dispatches than it could have.

        The charge is written to disk before the provider is called. A count
        that only reaches disk on a clean return is a count a crash refunds:
        kill the process mid-dispatch and the next one reads the old number
        and calls again. The budget is enforced across processes out of this
        file, so this file has to know about a call that was *started*.
        """
        exhausted_ = self._dispatches_exhausted(state)
        if exhausted_:
            raise DispatchError(f"dispatch refused: {exhausted_}")
        state.usage.dispatches += 1
        self._last_calls = getattr(self, "_last_calls", 0) + 1
        self.store.write_state(state)

    def _storage_gate(self, state: RunState):
        """STORAGE-B3: ask admission before a role dispatch, before the budget
        is charged. Returns the admitted reservation, or None without an
        admission (library use, noted once). A refusal parks the run
        (`storage_park.park_for_storage_pressure`, timer first) and raises
        `StorageParked`; an admission that cannot answer at all, or refuses
        for a reason that is not capacity, stops the run NEEDS_YOU ("storage
        admission unavailable") -- fail closed, never skipped."""
        # getattr: a controller assembled with `__new__` (several tests do)
        # has no admission, exactly like one built without it.
        admission = getattr(self, "storage_admission", None)
        if admission is None:
            if not getattr(self, "_storage_noted", False):
                state.note("storage admission: not configured (library use)")
                self._storage_noted = True
            return None
        project_id = state.project_name or state.run_id
        try:
            decision = admission.reserve(project_id, op_kind="role_run")
        except Exception as exc:              # noqa: BLE001 - fail closed, classified
            self._admission_failed(state, exc)
        if getattr(decision, "admitted", False):
            return decision
        self._refused(state, decision)

    def _admission_failed(self, state: RunState, exc: BaseException) -> NoReturn:
        """An admission that cannot answer at all stops the run NEEDS_YOU ("storage
        admission unavailable") and raises `StorageParked(parked=False)` -- fail
        closed, never skipped. Shared by the role dispatch and the temporary
        workspaces."""
        from .storage_park import ADMISSION_UNAVAILABLE, StorageParked, stop_needs_you

        reason = f"{ADMISSION_UNAVAILABLE}: {type(exc).__name__}: {exc}"[:400]
        stop_needs_you(state, self.store, reason)
        raise StorageParked(reason, parked=False) from exc

    def _refused(self, state: RunState, decision) -> NoReturn:
        """A refused admission ends the operation that asked: a capacity
        refusal parks the run (`storage_park.park_for_storage_pressure`, timer
        first) and raises `StorageParked`; any other refusal -- the ledger
        itself, not free space -- stops the run NEEDS_YOU and raises it with
        ``parked=False``. Shared by the role dispatch (`_storage_gate`) and the
        temporary workspaces (`_workspace`): one refusal, one route."""
        from .storage_park import (
            ADMISSION_UNAVAILABLE,
            ParkPolicy,
            StorageParked,
            is_capacity_refusal,
            park_for_storage_pressure,
            stop_needs_you,
        )

        if not is_capacity_refusal(decision):
            # Not free space: the ledger itself refused (another namespace, an
            # unclaimed boot, a malformed file). Waiting does not fix that.
            reason = f"{ADMISSION_UNAVAILABLE}: {getattr(decision, 'reason', decision)}"[:400]
            stop_needs_you(state, self.store, reason)
            raise StorageParked(reason, parked=False)
        from datetime import UTC, datetime

        parked = park_for_storage_pressure(
            state, decision, store=self.store, governor=self._contract_governor(state),
            policy=getattr(self, "park_policy", None) or ParkPolicy(), now=datetime.now(UTC),
            steward=getattr(self, "steward", None))
        reason = state.blocked_reason if parked else (state.stop_reason or "")
        raise StorageParked(reason or str(decision), parked=parked)

    # -- P2-42: registered temporary workspaces ------------------------------- #

    @contextmanager
    def _workspace(self, state: RunState, *, creator: str, purpose: str):
        """A registered temporary workspace of this run (`EphemeralRoots.create`:
        admitted, recorded before any content, leased to this process) for the
        body, released -- the creator's own cleanup -- on every way out, also
        when the body raises. Yields its path, or None without
        ``ephemeral_roots`` (library use, an unregistered project): the caller
        then keeps the behaviour it had. A refused admission parks the run
        (`_refused`) and an admission that cannot answer stops it NEEDS_YOU
        (`_admission_failed`), exactly as for a role dispatch: nothing is created
        and nothing falls back to a sibling or host directory. A workspace that
        could not be removed is noted."""
        roots = getattr(self, "ephemeral_roots", None)
        if roots is None:
            yield None
            return
        from .storage_ephemeral import (TempAdmissionRefused, TempAdmissionUnavailable,
                                        name_for_run)

        try:
            ws = roots.create(creator=creator, purpose=purpose, run_id=name_for_run(state.run_id))
        except TempAdmissionRefused as exc:
            self._refused(state, exc.refusal)
        except TempAdmissionUnavailable as exc:
            self._admission_failed(state, exc.cause)
        try:
            yield ws.path
        finally:
            released = roots.release(ws)
            if not released.get("removed"):
                state.note(f"temporary workspace {ws.workspace_id} stays: "
                           f"{released.get('note', '')}"[:400])
                try:
                    # the iteration's workspace is released after the iteration's last write:
                    # the note reaches the disk here, best effort (the workspace's own record
                    # on disk says it too)
                    self.store.write_state(state)
                except Exception:                  # noqa: BLE001 - never hides the real outcome
                    pass

    def _check_workspace(self, state: RunState):
        """The workspace of one acceptance check (candidate or baseline): its
        ``HOME`` and ``TMPDIR`` -- one per check, as the runner requires."""
        return self._workspace(state, creator="controller.run_check", purpose="check")

    def _enter_run_temp(self, state: RunState) -> None:
        """Make the iteration's workspace part of the iteration that is running; it is
        released when `_run_iteration_guarded` leaves. Without an iteration scope
        (a caller of `_run_iteration_locked` that did not come through it) nothing."""
        temp = getattr(self, "_iteration_temp", None)
        if temp is not None:
            temp.enter_context(self._run_temp(state))

    def _hold_workspace(self, state: RunState) -> LoopOutcome | None:
        """Take the lease of the run's worktree for this iteration, or return the wait.

        None: the iteration goes on -- holding the lease (released with the iteration's scope), or
        without one where there is nothing to lease (no iteration scope: a caller that did not
        come through `_run_iteration_guarded`; a halted run, which `begin_iteration` refuses next;
        not the root of a git worktree: `snapshot` refuses it below, and the note says so). A `WorkspaceHeld` is the wait that names the
        holder; any other refusal of the lease (one that cannot be read, a lock that stays busy)
        blocks the run for a person with the lease's own words. Neither is a verdict, neither
        reads the reason as a quota statement, neither goes through the contract's routing (it is
        not a decision someone is asked to make) and nothing has been charged.
        """
        self._workspace_hold = None
        scope = getattr(self, "_iteration_temp", None)
        if scope is None:
            return None
        if state.condition in stages.HALTED:
            return None             # `begin_iteration` refuses a halted run; it takes no lease
        if not is_git_repo(state.repo_path):
            if not self._lease_not_taken_noted:
                self._lease_not_taken_noted = True
                state.note(f"workspace lease: not taken -- {state.repo_path} is not the root "
                           "of a git worktree")
            return None
        holder = holder_name(state.run_id)
        try:
            lease = WorkspaceLease(state.repo_path, holder, ttl_seconds=self.workspace_lease_ttl)
        except WorkspaceLeaseError as exc:
            return self._workspace_blocked(
                state, f"the workspace lease is refused ({exc.reason}): {exc}")
        period = (self._workspace_keepalive if self._workspace_keepalive is not None
                  else keepalive_period(self.workspace_lease_ttl))
        hold = _WorkspaceHold(lease, period)
        # Registered **before** the lease is taken: an interrupt between the `acquire` and the
        # next line must still end in a release (the callback of a handle that holds nothing
        # releases nothing).
        scope.callback(self._release_workspace, hold)
        try:
            lease.acquire(admit=self._admit_worktree(state.repo_path, holder))
        except WorkspaceHeld as exc:
            return self._workspace_blocked(state, f"waiting for the workspace: {exc}")
        except WorkspaceError as exc:
            reason = getattr(exc, "reason", None)
            what = ("the workspace carries changes that are not this run's" if reason == "dirty"
                    else "the workspace could not be examined" if reason == "unexamined"
                    else f"the workspace lease is refused ({reason or 'git'})")
            return self._workspace_blocked(state, f"{what}: {exc}")
        self._workspace_hold = hold
        if (state.condition is Condition.ACTIVE and state.blocked_kind == WORKSPACE_WAIT_KIND
                and not state.blocked_reason):
            # The wait was lifted (`hoh unblock` clears the reason, not the kind): a stale owner
            # on an ACTIVE run is what `hoh stuck` reads as a run nobody owns.
            state.blocked_kind = None
        hold.start()
        return None

    @staticmethod
    def _admit_worktree(repo, holder: str):
        """What `WorkspaceLease.acquire(admit=)` asks, under the lease's lock: may this run have
        the worktree as it stands? Changes that are in it were made by whoever held it last; a
        run takes them over only if that was itself. Another holder's uncommitted work would be
        verified as this run's own and committed under its name (`commit_candidate` takes the
        whole tree), which is the failure the lease exists for -- so it is refused, written
        nowhere, and a person decides what the changes are. No previous lease (the first one on
        this worktree) is no evidence either way and proceeds as before.

        "Another run" is another **run id**: the holder name is the id alone, not (root, id), so
        the same id under a second `--root` is the same holder (the record has no root field)."""
        def admit(previous) -> None:
            if previous is None or previous.holder == holder:
                return
            # The commit's own question, not `git status`'s: status follows configuration
            # (`status.showUntrackedFiles=no`) that `git add -A` does not.
            try:
                dirty = has_uncommitted_work(repo)
            except WorkspaceLeaseError:
                raise
            except WorkspaceError as exc:
                # git could not answer (a foreign `index.lock` held by any concurrent git command, a
                # damaged index): never "clean", never a crash -- a named refusal for a person.
                raise WorkspaceLeaseError(
                    "unexamined", f"git could not answer for {repo}: {exc}; if another git command "
                                  f"is running there, wait for it, then unblock this run") from exc
            if not dirty:
                return
            left = (f"released at {previous.released_at}" if previous.released_at
                    else "not released")
            raise WorkspaceLeaseError(
                "dirty", f"{repo} has uncommitted changes and was last held by "
                         f"{previous.holder!r} (generation {previous.generation}, {left}); they "
                         f"are not this run's -- commit, stash or move them, then unblock this run")
        return admit

    def _workspace_blocked(self, state: RunState, reason: str) -> LoopOutcome:
        stages.block(state, reason, scan_for_quota=False)
        if state.blocked_kind != stages.CAPABILITY_VIOLATION:
            # `stages.block` leaves a plain block with no kind; a violation hold keeps its own.
            state.blocked_kind = WORKSPACE_WAIT_KIND
        self.store.write_state(state)
        return LoopOutcome(state.iteration, False, state.blocked_reason or reason)

    def _release_workspace(self, hold: "_WorkspaceHold") -> None:
        """The end of the iteration's scope: stop renewing, release. Never raises (it runs while
        an exception may be on its way out)."""
        if self._workspace_hold is hold:
            self._workspace_hold = None
        try:
            hold.close("iteration ended")
        except Exception:                             # noqa: BLE001 - nothing may mask the way out
            pass

    def _note_keepalive_errors(self, state: RunState) -> None:
        """One history note, at most, for the renewals that failed with anything but a lost lease
        (a disk that keeps failing lets the lease lapse unannounced; it must not be silent). Called
        after the commit whichever way it went -- a refused commit under a lapsed lease is exactly
        where the reason is wanted."""
        hold = self._workspace_hold
        if hold is None or not hold.errors or hold.noted:
            return
        hold.noted = True
        state.note(f"workspace lease: the keepalive's renewal failed {hold.errors} time(s), "
                   f"the last with {hold.last_error}")

    def _lease_after_commit(self, state: RunState, sha: str) -> None:
        """The second look: is the lease still this iteration's now the commit is made? A lease
        that ran out or was taken between the check and the commit means two writers were
        possible for that moment; the committed tree was compared with the candidate by
        `commit_candidate` itself, so the verdict stands and the fact is written down."""
        hold = self._workspace_hold
        if hold is None:
            return
        try:
            hold.lease.check(state.repo_path)
        except Exception as exc:                      # noqa: BLE001 - noted, never fatal here
            seen = f" (the keepalive saw it go: {hold.lost})" if hold.lost else ""
            state.note(f"workspace lease LOST around the commit {sha[:12]}: {exc}{seen}; "
                       "the commit was compared with the verified candidate, but the worktree "
                       "was not exclusively this run's for that moment")

    @contextmanager
    def _run_temp(self, state: RunState):
        """The iteration's workspace is the default temporary directory of this
        process for the iteration (`tempfile.tempdir`, restored on every way
        out): the two temporary files `workspace.py` makes -- the git index of
        `snapshot` and the tar of `materialize` -- land in a registered,
        admitted, leased workspace that this iteration removes. Entered after the
        iteration's gates (`_enter_run_temp`), before anything is spent."""
        with self._workspace(state, creator="controller.run_iteration", purpose="run") as path:
            if path is None:
                yield
                return
            previous = tempfile.tempdir
            tempfile.tempdir = str(path)
            dispatcher = getattr(self, "dispatcher", None)
            bind = getattr(dispatcher, "bind_role_scratch", None)
            try:
                # The roles' scratch (the `TMPDIR` of their panes) is made inside this workspace,
                # before the first dispatch of the iteration and out of the arena root: a
                # registered, admitted, leased directory that this iteration removes. A refusal
                # is a `DispatchError` (the iteration's own handling), never a fall-back.
                if callable(bind):
                    bind(path)
                yield
            finally:
                tempfile.tempdir = previous
                unbind = getattr(dispatcher, "unbind_role_scratch", None)
                if callable(unbind):
                    unbind()

    def _dispatch(self, role: Role, prompt: str, state: RunState) -> str:
        reservation = self._storage_gate(state)
        if reservation is None:
            return self._dispatch_admitted(role, prompt, state)
        from .storage_park import measure_owned_bytes, measure_owned_inodes

        # Only what this run owns is sized -- never any other path, and no
        # link is followed (`measure_owned_bytes`): the run directory, the
        # store's arena directory (the planner's read copy and, without a
        # `scratch_dir`, every arena), and -- with a `scratch_dir` -- each
        # arena `_arena` created for this run, one by one. The scratch_dir
        # root is never sized: another run may share it. An arena created
        # during the dispatch is added afterwards and counts 0 before.
        owned_before = self._owned_for_sizing()
        before = measure_owned_bytes(owned_before)
        inodes_before = measure_owned_inodes(owned_before)       # what the owned trees hold when the dispatch starts, arena copies included
        try:
            return self._dispatch_admitted(role, prompt, state)
        finally:
            owned_after = owned_before + [p for p in self._owned_for_sizing()
                                          if p not in owned_before]
            actual = max(0, measure_owned_bytes(owned_after) - before)
            try:
                self.storage_admission.release(reservation, actual)
            except Exception as exc:          # noqa: BLE001 - noted, never silent
                state.note(f"storage admission: releasing {reservation.id} "
                           f"({actual} B) failed: {exc}"[:400])
            self._record_inodes(reservation, state, inodes_before, measure_owned_inodes(owned_after))

    def _record_inodes(self, reservation, state: RunState, before: int | None, after: int | None) -> None:
        """P3-39: the inodes the run holds while this dispatch runs go into the admission ledger's inode history, from
        which the wave's gate predicts a run's inodes. **A footprint, the peak, not a net:** the entries the owned trees
        hold -- the arena copies of the candidate and the planner's read copy, which the controller makes before the
        dispatch starts, included -- at the larger of the start and the end of the dispatch. (A transient inside the
        dispatch that is gone by its end is not seen; the policy's safety factor and the gate's floor stand for it.)
        **Nothing is recorded, and the dispatch's state says why,** when either walk was unmeasurable or the count is 0
        (a run whose arenas exist holds entries; 0 measured nothing); an admission with no inode record records nothing;
        a record that cannot be written never hides the dispatch's own outcome."""
        record = getattr(self.storage_admission, "record_inodes", None)
        if not callable(record):
            return
        if before is None or after is None or max(before, after) == 0:
            state.note(f"storage admission: no inode count recorded for {reservation.id} "
                       f"({'unmeasurable' if before is None or after is None else 'a count of 0 is not a measurement'})"[:400])
            return
        try:
            record(reservation, max(before, after))
        except Exception:                     # noqa: BLE001 - the dispatch's outcome stands
            pass

    def _owned_for_sizing(self) -> list[Path]:
        """The directories one dispatch's written bytes are measured over."""
        owned = [self.store.dir, self.store.arenas_dir]
        if getattr(self, "scratch_dir", None):
            for arena in getattr(self, "_own_arenas", []):
                inside = any(arena == base or base in arena.parents for base in owned[:2])
                if not inside and arena not in owned:
                    owned.append(arena)
        return owned

    def _dispatch_admitted(self, role: Role, prompt: str, state: RunState) -> str:
        # The role's scratch directory (`TMPDIR` of its pane) must exist, and it must
        # exist *before* the witness below is taken: made during the dispatch it would
        # be a new child of the arena root. Nothing is charged for a refusal.
        ensure_scratch_ = getattr(self.dispatcher, "ensure_role_scratch", None)
        if callable(ensure_scratch_):
            ensure_scratch_(role)
        # Handoff §8: "Check budget and deadline before every dispatch; count
        # retries and supervision in." Previously it was counted only once per
        # iteration and checked only at the start of the iteration.
        self._budget_or_refusal(state)

        ref = self._register_intent(role, state)
        # O125: what the role may touch, witnessed before it runs. A prompt
        # that says "edit nothing" is a request; this is the part a machine
        # checks afterwards.
        from .capability import CapabilityViolation, CapabilityWitness

        policy = self._policy_for(role, state)
        witness_ = CapabilityWitness.take(policy)
        # What the witness actually covered, on the record. The protected set
        # is built from what exists when the dispatch starts, so it is a
        # property of this dispatch and not of the source.
        self._witness_scope = (len(policy.protected), len(policy.protected_shallow))

        def delivered(answer: str) -> str:
            violated = witness_.violations()
            if violated:
                # Fail closed, and loudly. A run whose planner wrote into
                # the tree its acceptance checks measure has not produced
                # a verdict about the product; continuing would attach a
                # result to a measurement that was interfered with.
                state.note(
                    f"capability violation by {role.value}: "
                    + "; ".join(violated)
                )
                raise CapabilityViolation(
                    f"{policy.summary()} -- " + "; ".join(violated)
                )
            self._retire_task(
                ref, state, endpoint=self.dispatcher.endpoint_evidence(role)
            )
            return answer

        def violation(when: str, cause: BaseException) -> CapabilityViolation | None:
            """A recorded violation to raise in place of `cause`, if any."""
            violated = witness_.violations()
            if not violated:
                return None
            state.note(f"capability violation by {role.value} {when}: "
                       + "; ".join(violated))
            found_ = CapabilityViolation(f"{policy.summary()} -- " + "; ".join(violated))
            found_.__cause__ = cause
            return found_

        def followed_up_answer(follow_up, missing: AnswerMissing) -> str:
            """The dispatcher's one follow-up for `missing`, witnessed.

            The witness is checked before the follow-up; inside the charge,
            before the budget is touched (otherwise a role's write to
            `state.json` during a passive wait is folded into the re-baseline
            below and never recorded); and before any error leaves the
            follow-up. A violation takes precedence over every other outcome,
            because the run's evidence is no longer intact. An answer it
            returns goes through `delivered` like any answer.
            """
            found = violation("on a turn that ended without the answer file", missing)
            if found is not None:
                raise found

            def charge() -> None:
                found_ = violation("before the follow-up charge", missing)
                if found_ is not None:
                    raise found_
                try:
                    self._budget_or_refusal(state)
                except DispatchError as refusal_:
                    state.note(
                        f"follow-up to {role.value} not sent: the turn ended "
                        f"without the answer file, and the budget is spent: {refusal_}")
                    raise refusal_ from missing
                # A second provider call for the same answer, counted as a
                # repair is; `_last_calls` counted it through the charge.
                self._last_retries = getattr(self, "_last_retries", 0) + 1
                state.note(
                    f"follow-up to {role.value}: the turn ended without the "
                    f"answer file {missing.answer_path.name}; one follow-up "
                    "prompt is sent")
                # The charge wrote `state.json`; only that is re-baselined.
                witness_.witness_again([self.store.state_path])

            try:
                return follow_up(role, state, missing, charge=charge)
            except CapabilityViolation:
                raise
            except Exception as exc:
                found = violation("during the follow-up", exc)
                if found is not None:
                    raise found from exc
                raise

        attempts = 0
        followed_up = False
        while True:
            try:
                return delivered(self.dispatcher.dispatch(role, prompt, state=state))
            except DispatchError as exc:
                follow_up = getattr(self.dispatcher, "follow_up", None)
                if (isinstance(exc, AnswerMissing) and not exc.followed_up
                        and not followed_up and callable(follow_up)):
                    # Idle is not done: the turn ended without the answer
                    # file. At most one follow-up per call, charged like any
                    # provider call and witnessed on every way out.
                    followed_up = True
                    return delivered(followed_up_answer(follow_up, exc))
                if exc.transient and attempts < state.budgets.max_transient_retries:
                    # Checked here, before the controller writes anything of
                    # its own, and for two reasons. It closes the window the
                    # persist below would otherwise open -- a role that wrote
                    # into a protected tree and *then* failed transiently
                    # would have that write folded into the fresh witness --
                    # and it fails closed one attempt earlier than waiting
                    # for the last one to return.
                    previously_violated = witness_.violations()
                    if previously_violated:
                        state.note(
                            f"capability violation by {role.value} on a failed "
                            f"attempt: " + "; ".join(previously_violated))
                        raise CapabilityViolation(
                            f"{policy.summary()} -- " + "; ".join(previously_violated)
                        ) from exc
                    attempts += 1
                    self._last_retries = attempts
                    state.usage.transient_retries += 1
                    # A retry calls the provider again, so it spends a
                    # dispatch. It did not: the counter sat outside this loop,
                    # and a run could re-call the provider past a budget that
                    # had already refused it. Charged through the same gate as
                    # the first call, which is what "count retries in" means.
                    try:
                        self._budget_or_refusal(state)
                    except DispatchError as refusal_:
                        state.note(
                            f"transient error on {role.value}, and the budget "
                            f"is spent: {refusal_}")
                        raise refusal_ from exc
                    state.note(f"transient error on {role.value}, attempt {attempts}: {exc}")
                    # Only `state.json`, which this process just wrote. A
                    # fresh witness over *everything* was the first version
                    # and an adversarial review broke it: a role that wrote
                    # into `checks.json` during the same window had that
                    # write folded into the new baseline, the run continued
                    # with a hijacked acceptance suite, and nothing was
                    # recorded. Every other protected tree keeps the baseline
                    # it was taken with, so a write anywhere else is still
                    # caught when this attempt returns.
                    witness_.witness_again([self.store.state_path])
                    # The back-off waits only now, after the re-baseline: a
                    # wait before it would fold a role's write to
                    # `state.json` during those seconds into the new
                    # baseline, and it would never be recorded. A refused
                    # charge has raised above, before any wait.
                    if exc.retry_after > 0:
                        time.sleep(exc.retry_after)
                    continue
                # The final way out of a failed dispatch is witnessed too: a
                # write the role undid before it failed must not be absorbed
                # as an outage or an approval wait.
                found = violation("on a failed dispatch", exc)
                if found is not None:
                    raise found from exc
                raise
            except CapabilityViolation:
                raise
            except Exception as exc:
                # And so is a crash of the dispatcher itself.
                found = violation("on a dispatch that crashed", exc)
                if found is not None:
                    raise found from exc
                raise

    def _dispatch_with_repair(
        self, role: Role, prompt: str, state: RunState, *, plan_check=None
    ) -> str:
        """Exactly one schema repair per role output (handoff §8).

        The repaired output is validated as well -- otherwise a role run just
        passes a second broken output along, and the error only materializes
        deep in the loop as a raw parse error.
        """

        repairs_this_output = 0

        def validate(text: str) -> None:
            if role is Role.PLANNER:
                parsed = roles.parse_plan(text)
                if plan_check is not None:
                    plan_check(parsed)
            else:
                roles.extract_json(text)

        raw = self._dispatch(role, prompt, state)
        try:
            validate(raw)
        except roles.RoleOutputError as exc:
            # Handoff §8 means "one schema repair **per role output**". The
            # counter was a run counter and was never reset: after ONE repair
            # in the whole run, every further malformed output immediately
            # became a hard abort. That is exactly what happened in the
            # second A02 attempt.
            if repairs_this_output >= state.budgets.max_schema_repairs:
                raise DispatchError(
                    f"{role.value} violates the output contract and the "
                    f"repair budget for this output is exhausted: {exc}"
                ) from exc
            repairs_this_output += 1
            state.usage.schema_repairs += 1
            # The repair is a second provider call for the same answer. The
            # record covered one dispatch and reported one attempt, so the log
            # under-counted the calls it exists to count.
            self._last_retries = getattr(self, "_last_retries", 0) + 1
            state.note(f"schema repair for {role.value}: {exc}")
            repaired = self._dispatch(role, roles.repair_prompt(role, str(exc), raw), state)

            try:
                validate(repaired)
            except roles.RoleOutputError as exc2:
                raise DispatchError(
                    f"{role.value} violates the output contract even after the "
                    f"repair; the repair budget is exhausted: {exc2}"
                ) from exc2
            # The repair went to the same pane: it is released once, after it.
            self._finish_turn(role, state)
            return repaired
        # The first answer validated.
        self._finish_turn(role, state)
        return raw

    def _finish_turn(self, role: Role, state: RunState) -> None:
        """End the role's session: its turn is over and the run will not use
        this pane again (`HerdrDispatcher.release_role`).

        An optional hook, like `follow_up` and `role_gone`: a dispatcher
        without it (the subprocess adapter, a test double) has no session to
        end. Only the developer's refusal stops the run -- its pane, or what it
        left running, could still change the tree after the freeze; for the
        planner and QA it is recorded and the run goes on. Tested on
        `.closed`, never on the truthiness of the answer.
        """
        release = getattr(self.dispatcher, "release_role", None)
        if not callable(release):
            return
        answer = release(role, state)
        if role is Role.DEVELOPER and not answer.closed:
            # The reason goes on the second line: only the first line of a
            # failure is read for its class (`taxonomy.classify_failure`), and
            # a reason may quote a dialog or a timeout.
            raise RoleNotQuiescent(
                f"{role.value}: its session could not be ended before the "
                f"candidate was frozen.\n{answer.why}")


def open_seal_arenas(run_dir: Path | str) -> list[str]:
    """The arenas a run's seal ledger leaves sealed, for the gates that read it.

    The same first-`planned` rule as `Controller._open_seals`: a `planned`
    line opens an arena, a `released` line closes it, and a later `planned`
    line of an arena that is already open changes nothing. Lines that do not
    parse are skipped. A ledger that cannot be read counts as **one** open
    seal, named by the ledger's own path (fail closed): a gate that cannot
    tell whether an arena is still sealed holds. A ledger that is a symbolic
    link, is not a regular file or has more than one name cannot be told
    either (`durable_io.read_single_link`): a link to an empty file must not
    hide an open seal.
    """
    from .durable_io import read_single_link

    path = Path(run_dir) / SEAL_LEDGER
    try:
        raw = read_single_link(path)
        if raw is None:
            return []
        text = raw.decode("utf-8")
    except (OSError, UnicodeError):
        return [str(path)]
    opened: dict[str, bool] = {}
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        event, arena = rec.get("event"), rec.get("arena")
        if not isinstance(arena, str):
            continue
        if event == "planned":
            opened.setdefault(arena, True)
        elif event == "released":
            opened.pop(arena, None)
    return list(opened)


def _failed_outcome(exc: BaseException) -> str:
    """The telemetry outcome of a dispatch that did not return.

    A storage park is not a failed dispatch: no role was started, nothing was
    spent. It is booked `storage_parked` (parked, a recheck is armed) or
    `storage_unavailable` (stopped NEEDS_YOU); everything else stays `failed`.
    """
    from .storage_park import StorageParked

    if isinstance(exc, StorageParked):
        return "storage_parked" if exc.parked else "storage_unavailable"
    return "failed"


def _receipts_summary(receipts: list[Receipt]) -> str:
    if not receipts:
        return "(no evidence)"
    return "\n".join(
        f"  - {r.receipt_id}: check={r.check_id} exit={r.exit_code} "
        f"({r.started_at} -> {r.ended_at}), log={r.stdout_path}"
        for r in receipts
    )


def _policy_text() -> str:
    """Digest over the policy in force, so that a change becomes visible."""
    rules = Path.home() / ".agents/rules/house-rules.md"
    try:
        return rules.read_text(encoding="utf-8")
    except OSError:
        return "(no global house rules found)"


def new_run_id(prefix: str = "hoh") -> str:
    return f"{prefix}-{utcnow().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:6]}"
