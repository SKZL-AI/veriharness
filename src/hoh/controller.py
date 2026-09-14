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

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from . import sandbox as sandbox_mod

from . import roles, stages
from .contracts import (
    AcceptanceCheck,
    Budgets,
    Candidate,
    CheckVerdict,
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
from .capability import CapabilityViolation
from .store import RunStore
from .telemetry import NOT_AVAILABLE, TelemetryLog, record_from_role
from .workspace import commit_candidate, materialize, snapshot, unchanged


#: Prefix by which `stages.reject_candidate` tells a QA **outage** apart from
#: a substantive no. It is produced when no usable QA answer arrives, and
#: checked a few lines further down via `startswith`. Kept as a constant
#: rather than as text typed twice: the same coupling over prose already
#: carried a behavior in `cli`/`dispatchers` that not a single line of test
#: covered (see `WaitingForApproval`). Whoever changes the wording changes it
#: here -- and both sides move along with it.
NO_QA_VERDICT = "NO QA VERDICT"


class DispatchError(RuntimeError):
    """The role run did not come about. Transient or final -- the controller
    decides based on `transient`."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


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


class Controller:
    def __init__(
        self,
        store: RunStore,
        dispatcher: RoleDispatcher,
        *,
        spec_path: Path | str,
        scratch_dir: Path | None = None,
        isolation: "sandbox_mod.Isolation | None" = None,
    ) -> None:
        self.store = store
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
        with self.store.lock():
            try:
                return self._run_iteration_locked(state)
            except CapabilityViolation as exc:
                # Fail closed, and as a *result* rather than as a crash. A
                # violation is not an outage and must not be re-read as a
                # missing verdict -- that path would let the loop treat it as
                # an ordinary rejection and carry on. It blocks the run, keeps
                # the reason, and leaves the evidence where it is.
                stages.block(state, f"capability violation: {exc}")
                self.store.write_state(state)
                return LoopOutcome(state.iteration, False,
                                   state.blocked_reason or str(exc))

    def _run_iteration_locked(self, state: RunState) -> LoopOutcome:
        if state.started_at_wall is None:
            state.started_at_wall = utcnow()
        if state.arena_secret is None:
            import secrets

            state.arena_secret = secrets.token_hex(8)
        exhausted = state.budget_exhausted()
        if exhausted:
            stages.block(state, exhausted)
            self.store.write_state(state)
            return LoopOutcome(state.iteration, False, exhausted)

        evidence = self.store.read_evidence(expect_digest=state.evidence_digest)
        spec_text = roles.read_spec(self.spec_path)

        # An amendment is the one way the text may move, and it moves the
        # run's own digest with it. Read before the drift check rather than
        # after: the chain is what tells a silent edit apart from a recorded
        # one, and without it every amendment would look like drift.
        kette = self._amendment_chain(state)
        gesehen = len(kette.amendments) if kette is not None else 0
        if gesehen < state.amendments_seen:
            # The chain got shorter. Whatever did that -- a rename, a prune, a
            # hand -- the obligation an amendment created does not evaporate
            # with the file that recorded it.
            stages.block(
                state,
                f"the amendment chain has {gesehen} entries and this run has "
                f"seen {state.amendments_seen}; the record of what was amended "
                "is incomplete"
            )
            self.store.write_state(state)
            return LoopOutcome(state.iteration, False,
                               state.blocked_reason or "amendment chain shrank")
        if gesehen > state.amendments_seen:
            state.amendments_seen = gesehen
            self.store.write_state(state)
        if kette is not None and kette.current_digest() == digest(spec_text):
            if state.spec_digest != kette.current_digest():
                state.note(
                    f"specification amended: {state.spec_digest} -> "
                    f"{kette.current_digest()} through "
                    f"{len(kette.amendments)} amendment(s)"
                )
                state.spec_digest = kette.current_digest()
                self.store.write_state(state)
            self._reopen_amended_criteria(kette, state)

        # The specification must not change silently underneath a running run
        # -- otherwise two iterations plan and verify against different truths.
        if digest(spec_text) != state.spec_digest:
            stages.block(
                state,
                f"specification has changed (expected {state.spec_digest}, "
                f"found {digest(spec_text)})",
            )
            self.store.write_state(state)
            return LoopOutcome(state.iteration, False, state.blocked_reason or "spec drift")

        halt = self._honor_stop_request(state)
        if halt is not None:
            return halt

        # --- PLANNING ------------------------------------------------------- #
        stages.begin_iteration(state, reason="new iteration")
        iteration = state.iteration
        self.store.write_state(state)

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
        candidate = snapshot(
            state.repo_path,
            f"{state.run_id}-i{state.iteration}",
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
            stages.block(state, f"candidate binding violated: {exc}")
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
        offen = self._revalidation_offen(kette, state, plan, result)
        if offen and result.summary.startswith(NO_QA_VERDICT):
            # A QA outage is a missing verdict, not stalled substance. Reporting
            # it as "the amendment's criterion was not measured" would book a
            # quota problem as a failure to do the work -- the distinction
            # `reject_candidate` exists to keep.
            offen = set()
        if offen:
            grund = ("acceptance withheld: the specification was amended and "
                     + ", ".join(sorted(offen))
                     + " has not been measured against the new text")
            state.note(grund)
            stages.reject_candidate(state, grund)
            self.store.write_state(state)
            return LoopOutcome(iteration, False, grund, candidate.candidate_id,
                               result.verdicts)

        if result.accepted():
            # The obligation an amendment created is discharged here, once,
            # and recorded. Recomputing it from the chain every iteration meant
            # a criterion re-measured in iteration 2 was demanded again in
            # iteration 3 -- and forever.
            erledigt = set(kette.revalidation_needed()) if kette is not None else set()
            if erledigt:
                state.revalidated = sorted(set(state.revalidated) | erledigt)
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
                )
                state.note(f"candidate {candidate.candidate_id} committed as {sha[:12]}.")
            except Exception as exc:
                state.note(
                    f"candidate {candidate.candidate_id} accepted, but NOT "
                    f"committed: {exc}. The state lives only in the working "
                    "tree and cannot be delivered."
                )
            stages.accept_checkpoint(state, candidate)
            self.store.write_state(state)
            return LoopOutcome(
                iteration, True, "all criteria demonstrated", candidate.candidate_id,
                result.verdicts,
            )

        open_ids = [v.check_id for v in result.verdicts if v.outcome is not Outcome.PASS]
        # "Failed" and "never checked at all" must not sound the same. If QA
        # drops out -- quota exhausted, agent crashed, dialog open -- then no
        # criterion has been refuted; none has been assessed.
        if result.summary.startswith(NO_QA_VERDICT):
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
            reopened=getattr(self, "_wieder_offen", None),
        )
        self._beginne_zaehlung()
        started_at = utcnow()
        # 'ok' is earned only once the plan has answered, schema-parsed, AND
        # bound correctly to this run/iteration/spec/base -- a plan that
        # parses but is bound to the wrong run is rejected by `_plan` itself
        # (see `_assert_plan_bound`), and a telemetry record claiming success
        # for it would contradict the very rejection it describes.
        try:
            raw = self._dispatch_with_repair(Role.PLANNER, prompt, state)
            plan = roles.parse_plan(raw)
            self._assert_plan_bound(plan, state, base)
        except Exception as exc:
            self.note_dispatch(
                role=Role.PLANNER.value, run_id=state.run_id, iteration=state.iteration,
                attempt=state.attempt, started_at=started_at, ended_at=utcnow(),
                backend=type(self.dispatcher).__name__, outcome="failed",
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
        self._beginne_zaehlung()
        started_at = utcnow()
        try:
            self._dispatch(Role.DEVELOPER, prompt, state)
        except Exception as exc:
            self.note_dispatch(
                role=Role.DEVELOPER.value, run_id=state.run_id, iteration=state.iteration,
                attempt=state.attempt, started_at=started_at, ended_at=utcnow(),
                backend=type(self.dispatcher).__name__, outcome="failed",
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
        self._beginne_zaehlung()
        started_at = utcnow()
        try:
            raw = self._dispatch_with_repair(Role.QA, prompt, state)
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

        # What the verification produced belongs on the verification's own
        # record. It was left at 0 -- a missing measurement wearing a number,
        # on a run that had written two receipts (O128).
        self.note_dispatch(
            role=Role.QA.value, run_id=state.run_id, iteration=state.iteration,
            attempt=state.attempt, started_at=started_at, ended_at=utcnow(),
            backend=type(self.dispatcher).__name__,
            outcome="failed" if outage else "ok",
            detail=outage, usage={}, state=state, exc=qa_exc,
            receipts=self._quittungen_geschrieben(state),
            discriminating=len(discriminating),
            artefactual=len(getattr(self, "_artefactual", set()) & set(discriminating)),
        )

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
                receipt, log = run_check(
                    check, probe, run_id=state.run_id, iteration=state.iteration,
                    attempt=state.attempt, cwd=arena, timeout=DEFAULT_CHECK_TIMEOUT,
                    receipt_suffix="basis",
                    isolation=self.isolation,
                )
            except (HouseRuleViolation, ArenaEscape, PolicyUnavailable) as exc:
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
                grund = artefactual_reason(receipt.exit_code, log)
                if grund:
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
                    artefactual[check.check_id] = grund
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
        jetzt = utcnow()
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
                started_at=jetzt,
                ended_at=jetzt,
                stdout_digest=digest(text),
                stdout_path=f"logs/{receipt_id}.txt",
                runner_identity=runner_identity(),
                runner_ok=False,
            )
            self.store.write_receipt(receipt_id, receipt)
            self.store.write_log(receipt_id, text)
        except Exception as schreibfehler:       # pragma: no cover - disk shapes
            state.note(f"refusal receipt for {check.check_id} not writable: {schreibfehler}")

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
        raw_name = f"{state.arena_secret}:{state.iteration}:{state.attempt}:{suffix}"
        name = digest(raw_name)[:12]
        arena = Path(base) / name
        if arena.exists():
            # No deleting: the previous run is parked with a version suffix.
            # The timestamp makes the name unique -- a mere counter collided
            # after a prune(), because it jumps back.
            arena.rename(arena.with_name(f"{arena.name}.v{utcnow().replace(':', '-')}"))
        arena.mkdir(parents=True, exist_ok=True)
        return arena

    def _execute(
        self, state: RunState, check: AcceptanceCheck, candidate: Candidate
    ) -> tuple[Receipt, str]:
        try:
            return run_check(
                check,
                candidate,
                run_id=state.run_id,
                iteration=state.iteration,
                attempt=state.attempt,
                cwd=candidate.repo_path,  # is the arena (probe), never the live tree
                isolation=self.isolation,
            )
        except PolicyUnavailable:
            # Without a policy nothing is executed -- and that is a visible
            # block, not a traceback in the middle of VERIFYING.
            raise
        except (HouseRuleViolation, ArenaEscape) as exc:
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
            kind = "ARENA ESCAPE" if isinstance(exc, ArenaEscape) else "HOUSE RULE VIOLATION"
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

            versender = getattr(self, "dispatcher", None)
            try:
                schluessel = role if isinstance(role, Role) else Role(role)
            except ValueError:
                # "gate", "merge", "closure" are documented roles of this
                # record and are not dispatch roles. They keep their string
                # key rather than losing their identity: writing
                # NOT_AVAILABLE while the answer sits in the same object is
                # the false statement this whole block exists to remove.
                schluessel = role
            # The dispatcher was *given* these -- `cli.py` fills them from
            # --role-model and --role-effort and hands them to the agent CLI.
            # Writing NOT_AVAILABLE while the answer sits in the same object
            # is not an unknown, it is a false statement.
            def _konfiguriert(name: str) -> str:
                tabelle = getattr(versender, name, None)
                if not isinstance(tabelle, dict):
                    return ""
                return tabelle.get(schluessel) or ""
            rest.setdefault("provider",
                            _konfiguriert("profiles") or NOT_AVAILABLE)
            rest.setdefault("model", _konfiguriert("models") or NOT_AVAILABLE)
            rest.setdefault("effort", _konfiguriert("efforts") or NOT_AVAILABLE)
            if outcome != "ok":
                rest.setdefault("failure_class",
                                classify_failure(exc, detail=detail))
            umfang = getattr(self, "_zeuge_umfang", None)
            if umfang is not None:
                rest.setdefault("witnessed_trees", umfang[0])
                rest.setdefault("witnessed_listings", umfang[1])
            # Retries were counted in `state.usage` and nowhere else, so every
            # record read `retries: 0` on a run that had retried twice -- and
            # the log's stated purpose is "how much did that take".
            rest.setdefault("retries", getattr(self, "_letzte_retries", 0))
            # The number this record contributes to "what did this run cost".
            # Not derivable from the line's existence in either direction.
            #
            # Only for the three dispatch roles. "gate", "merge" and "closure"
            # are documented roles of this record that reach no provider, and
            # they do not reset the counter either -- so filling it for them
            # would attach the *previous* role's calls to a line that made
            # none, and the sum over the log would count those calls twice.
            if isinstance(schluessel, Role):
                rest.setdefault("provider_calls",
                                getattr(self, "_letzte_aufrufe", 0))
            else:
                rest.setdefault("provider_calls", 0)
            # `DispatchRecord` forbids extra fields, and `rest` was passed
            # straight through -- so one plausible-but-wrong keyword name made
            # the whole line disappear, silently when no `state` was given. A
            # record lost to a typo is the same defect as a well-typed blank.
            from .telemetry import DispatchRecord

            bekannt = set(DispatchRecord.model_fields)
            fremd = sorted(k for k in rest if k not in bekannt)
            for k in fremd:
                rest.pop(k)
            if fremd and state is not None:
                state.note(
                    f"telemetry: {role} dispatch record does not carry "
                    + ", ".join(fremd) + " -- no such field, dropped"
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

    def _quittungen_geschrieben(self, state: RunState) -> int:
        """Receipt files this iteration and attempt actually produced.

        Counted on disk rather than from the candidate loop's own list. That
        list holds only the candidate's receipts; the baseline receipts
        `_discriminates` writes and any refusal receipt are written during the
        same verification and were not counted -- a run with two receipt files
        reported one, which is the same shape as reporting zero, one step
        smaller.
        """
        verzeichnis = self.store.dir / "receipts"
        if not verzeichnis.is_dir():
            return 0
        praefix = f"{state.run_id}-i{state.iteration}-a{state.attempt}-"
        return sum(1 for f in verzeichnis.glob("*.json")
                   if f.name.startswith(praefix))

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
            kette = self.store.read_amendments(origin_digest=state.spec_digest)
        except Exception as exc:                 # noqa: BLE001 - a chain
            # that cannot be read must not stop a run; it must not be applied
            # either, and the note is how a reader finds out which happened.
            state.note(f"amendment chain unreadable, ignored: {exc}")
            return None
        if not kette.amendments:
            return None
        if kette.run_id != state.run_id:
            state.note(
                f"amendment chain names run {kette.run_id!r}, not "
                f"{state.run_id!r} -- ignored"
            )
            return None
        for a in kette.amendments:
            alt = Path(a.from_path) if a.from_path else None
            if alt is None or not alt.is_file():
                state.note(
                    f"{a.amendment_id}: the superseded text it names is not "
                    f"there ({a.from_path or 'unnamed'}) -- chain ignored"
                )
                return None
            if text_digest(alt.read_text(encoding="utf-8")) != a.from_digest:
                state.note(
                    f"{a.amendment_id}: the parked text does not hash to "
                    f"{a.from_digest} -- chain ignored"
                )
                return None
        return kette

    def _reopen_amended_criteria(self, kette, state: RunState) -> None:
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
        noetig = set(kette.revalidation_needed()) - set(state.revalidated)
        if not noetig:
            # Cleared, not left standing: the planner prompt would otherwise
            # keep naming criteria that have already been answered, and a
            # prompt that asks for work already done is a prompt a role has to
            # guess its way past.
            self._wieder_offen = None
            return
        suite = self.store.read_checks()
        entfernt = sorted(noetig & set(suite))
        if not entfernt:
            return
        for cid in entfernt:
            del suite[cid]
        self.store.write_checks(suite)
        state.note(
            "reopened by amendment, removed from the preservation suite so the "
            "new text can be measured: " + ", ".join(entfernt)
        )
        self._wieder_offen = sorted(noetig)

    def _revalidation_offen(self, kette, state: RunState,
                            plan: DevelopmentPlan, result) -> set[str]:
        """Criteria an amendment reopened that this iteration has not answered.

        A criterion counts as answered when this iteration both **planned** it
        and produced a verdict for it. Measuring it is not enough on its own:
        a plan that quietly drops the criterion would otherwise satisfy the
        requirement by never asking, which is the shape of the missing
        preservation suite that O129 rode to a false checkpoint.
        """
        noetig = set(kette.revalidation_needed()) if kette is not None else set()
        noetig -= set(state.revalidated)
        if not noetig:
            return set()
        geplant = {c.check_id for c in plan.acceptance_checks}
        beantwortet = {v.check_id for v in result.verdicts
                       if v.outcome.value in ("PASS", "FAIL")}
        return {c for c in noetig if c not in geplant or c not in beantwortet}

    def _beginne_zaehlung(self) -> None:
        """Start counting this role's provider calls at zero.

        Called by the role methods rather than by `_dispatch`, because one role
        attempt can be several dispatches: a schema repair is a second call for
        the same answer, and resetting inside `_dispatch` would throw the first
        one away. Reset rather than left standing, so a role that never reaches
        a dispatch does not report the previous role's number.
        """
        self._letzte_retries = 0
        # How many calls actually reached the provider for this role. Recorded
        # because the telemetry log was being *counted* elsewhere as one line
        # per dispatch, and a line is neither: a role that retried twice wrote
        # one line for three calls, and a role refused at the budget wrote one
        # line for none. The benchmark's cost figure was read off those lines.
        self._letzte_aufrufe = 0
        # Cleared with it: a dispatch refused at the budget check never takes a
        # witness, and the record then carried the *previous* dispatch's
        # coverage. A fabricated measurement in the evidence log is the class
        # of defect the log exists to remove.
        self._zeuge_umfang = None

    def _antwortdatei(self, role: Role, state: RunState) -> Path:
        """The one file this role's answer may come from, for this attempt.

        Asked of the dispatcher where it can answer, because the dispatcher is
        what writes it; the fallback reproduces the same name rather than
        falling back to a directory. A policy whose `output_channel` names a
        file that never exists -- which is what `answers/{role}.json` was --
        cannot decide anything, and `_answered()` relies on it.
        """
        holen = getattr(self.dispatcher, "_answer_path", None)
        if callable(holen):
            return Path(holen(role, state))
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

        antwort = self._antwortdatei(role, state)
        arenen = self.store.arenas_dir
        lauf = self.store.dir

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
            lauf / "state.json", lauf / "checks.json", lauf / "evidence.json",
            lauf / "amendments.json",
            lauf / "receipts", lauf / "results", lauf / "logs",
        )
        # The arena root gains a child every iteration, so it cannot be
        # digested whole around a dispatch -- but nothing legitimate creates
        # one *while* a role runs, and a staging directory made then and moved
        # in afterwards is the attack a full digest is designed to miss.
        flach = (arenen,) if arenen.is_dir() else ()
        repo = Path(state.repo_path)

        def andere_arenen(ausser: Path | None = None) -> tuple[Path, ...]:
            if not arenen.is_dir():
                return ()
            return tuple(
                d for d in sorted(arenen.glob("*"))
                if d.is_dir() and d.name != "planner"
                # The scratch siblings are the check commands' `$HOME` and
                # `$TMPDIR` -- pip and pytest caches, measured at up to 118 MB
                # each. They are not evidence about anything, and digesting
                # them twice per dispatch is most of what a witness costs.
                and ".scratch" not in d.name
                and not d.name.startswith(".hoh-scratch-")
                and (ausser is None or d.resolve() != ausser.resolve())
            )

        if role is Role.PLANNER:
            # The repository belongs here and did not before: `snapshot()`
            # binds the **working tree**, so a file the planner wrote there
            # during its dispatch landed inside the accepted candidate.
            return cap.planner_policy(
                read_copy=arenen / "planner", answer=antwort,
                protected=andere_arenen() + evidenz + (repo,),
                shallow=flach,
            )
        if role is Role.DEVELOPER:
            # The repository is the developer's workspace, so it is the one
            # role for which it is not protected. The arenas of iterations
            # already measured still are.
            return cap.developer_policy(
                arena=repo, answer=antwort,
                protected=andere_arenen() + evidenz,
                shallow=flach,
            )
        if role is Role.QA:
            eigene = getattr(self, "_qa_arena", None)
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
            return cap.qa_policy(
                arena=arenen, answer=antwort,
                protected=andere_arenen(eigene) + evidenz + (repo,),
            )
        return cap.denied_policy(
            role.value, protected=andere_arenen() + evidenz + (repo,))

    def _budget_oder_absage(self, state: RunState) -> None:
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
        erschoepft = state.budget_exhausted()
        if erschoepft:
            raise DispatchError(f"dispatch refused: {erschoepft}")
        state.usage.dispatches += 1
        self._letzte_aufrufe = getattr(self, "_letzte_aufrufe", 0) + 1
        self.store.write_state(state)

    def _dispatch(self, role: Role, prompt: str, state: RunState) -> str:
        # Handoff §8: "Check budget and deadline before every dispatch; count
        # retries and supervision in." Previously it was counted only once per
        # iteration and checked only at the start of the iteration.
        self._budget_oder_absage(state)

        ref = self._register_intent(role, state)
        # O125: what the role may touch, witnessed before it runs. A prompt
        # that says "edit nothing" is a request; this is the part a machine
        # checks afterwards.
        from .capability import CapabilityViolation, CapabilityWitness

        policy = self._policy_for(role, state)
        zeuge = CapabilityWitness.take(policy)
        # What the witness actually covered, on the record. The protected set
        # is built from what exists when the dispatch starts, so it is a
        # property of this dispatch and not of the source.
        self._zeuge_umfang = (len(policy.protected), len(policy.protected_shallow))
        attempts = 0
        while True:
            try:
                answer = self.dispatcher.dispatch(role, prompt, state=state)
                verletzt = zeuge.violations()
                if verletzt:
                    # Fail closed, and loudly. A run whose planner wrote into
                    # the tree its acceptance checks measure has not produced
                    # a verdict about the product; continuing would attach a
                    # result to a measurement that was interfered with.
                    state.note(
                        f"capability violation by {role.value}: "
                        + "; ".join(verletzt)
                    )
                    raise CapabilityViolation(
                        f"{policy.summary()} -- " + "; ".join(verletzt)
                    )
                self._retire_task(
                    ref, state, endpoint=self.dispatcher.endpoint_evidence(role)
                )
                return answer
            except DispatchError as exc:
                if exc.transient and attempts < state.budgets.max_transient_retries:
                    # Checked here, before the controller writes anything of
                    # its own, and for two reasons. It closes the window the
                    # persist below would otherwise open -- a role that wrote
                    # into a protected tree and *then* failed transiently
                    # would have that write folded into the fresh witness --
                    # and it fails closed one attempt earlier than waiting
                    # for the last one to return.
                    vorher_verletzt = zeuge.violations()
                    if vorher_verletzt:
                        state.note(
                            f"capability violation by {role.value} on a failed "
                            f"attempt: " + "; ".join(vorher_verletzt))
                        raise CapabilityViolation(
                            f"{policy.summary()} -- " + "; ".join(vorher_verletzt)
                        ) from exc
                    attempts += 1
                    self._letzte_retries = attempts
                    state.usage.transient_retries += 1
                    # A retry calls the provider again, so it spends a
                    # dispatch. It did not: the counter sat outside this loop,
                    # and a run could re-call the provider past a budget that
                    # had already refused it. Charged through the same gate as
                    # the first call, which is what "count retries in" means.
                    try:
                        self._budget_oder_absage(state)
                    except DispatchError as absage:
                        state.note(
                            f"transient error on {role.value}, and the budget "
                            f"is spent: {absage}")
                        raise absage from exc
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
                    zeuge.neu_bezeugen([self.store.state_path])
                    continue
                raise

    def _dispatch_with_repair(self, role: Role, prompt: str, state: RunState) -> str:
        """Exactly one schema repair per role output (handoff §8).

        The repaired output is validated as well -- otherwise a role run just
        passes a second broken output along, and the error only materializes
        deep in the loop as a raw parse error.
        """

        repairs_this_output = 0

        def validate(text: str) -> None:
            if role is Role.PLANNER:
                roles.parse_plan(text)
            else:
                roles.extract_json(text)

        raw = self._dispatch(role, prompt, state)
        try:
            validate(raw)
            return raw
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
            self._letzte_retries = getattr(self, "_letzte_retries", 0) + 1
            state.note(f"schema repair for {role.value}: {exc}")
            repaired = self._dispatch(role, roles.repair_prompt(role, str(exc), raw), state)

        try:
            validate(repaired)
        except roles.RoleOutputError as exc:
            raise DispatchError(
                f"{role.value} violates the output contract even after the "
                f"repair; the repair budget is exhausted: {exc}"
            ) from exc
        return repaired


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
