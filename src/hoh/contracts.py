"""Typed contracts for the HoH workflow.

Handoff §6 requires RunState, DevelopmentPlan and RoleResult/Evidence to be
schema-validated. Everything here is strict: unknown fields are rejected, so
that a hallucinated role output does not slip through silently (A05 checks
exactly that).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1


def utcnow() -> str:
    """Timestamp in a form that is sortable and unambiguously UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def digest(text: str) -> str:
    """Short, stable digest for spec, plan and policy binding."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class Strict(BaseModel):
    """Base: no unknown fields, no silent type reinterpretations."""

    model_config = ConfigDict(extra="forbid", frozen=False, str_strip_whitespace=True)


# --------------------------------------------------------------------------- #
# State model (Handoff §6)
# --------------------------------------------------------------------------- #


class Stage(StrEnum):
    NEW = "NEW"
    PLANNING = "PLANNING"
    DEVELOPING = "DEVELOPING"
    VERIFYING = "VERIFYING"
    CHECKPOINTED = "CHECKPOINTED"
    READY_FOR_DELIVERY = "READY_FOR_DELIVERY"


class Condition(StrEnum):
    """Cross-cutting conditions that any active stage can take on."""

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Role(StrEnum):
    PLANNER = "planner"
    DEVELOPER = "developer"
    QA = "qa"


class Outcome(StrEnum):
    """QA classifies per acceptance criterion. Handoff §5:
    an infrastructure error is never PASS."""

    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class CacheStatus(StrEnum):
    """K1 verdict, deliberately separate from the HoH approval path (Handoff §12).

    **Nothing reads this.** Measured 2026-09-08 by walking the AST of every
    file under `src/hoh/` and `tests/`: zero uses outside this definition,
    while the next-least-used contract in this module has four. It was
    declared for a KV-cache decision that this project does not make and does
    not measure -- `docs/LIMITATIONS.md` says so among the limitations.

    It stays because nothing here is deleted. But a type that names a
    capability the code does not have is a claim like any other, and this
    project's entire argument is against claims that look supported and are
    not. So the status is written down rather than left to be inferred, and
    `tests/test_source_hygiene.py` carries a detector that fails if a *second*
    unused contract appears -- with this one on an explicit allowlist naming
    this reason. One documented remnant is a fact; a growing collection of
    them would be a habit.
    """

    ENABLED_VALIDATED = "ENABLED_VALIDATED"
    DISABLED_NO_BENEFIT = "DISABLED_NO_BENEFIT"
    DEFERRED_NO_LOCAL_BACKEND = "DEFERRED_NO_LOCAL_BACKEND"
    DEFERRED_COMPATIBILITY = "DEFERRED_COMPATIBILITY"


# --------------------------------------------------------------------------- #
# Candidate binding (Handoff §6)
# --------------------------------------------------------------------------- #


class Candidate(Strict):
    """Binds a verification to exactly the content that was tested.

    A commit alone is not enough: a dirty tree, different locks or changed
    test definitions turn the same commit ID into a different object of
    verification. `tree_digest` covers exactly that.
    """

    candidate_id: str
    repo_path: str
    commit: str
    tree_clean: bool
    tree_digest: str = Field(description="Digest over tracked contents + relevant locks")
    created_at: str = Field(default_factory=utcnow)
    note: str | None = None

    def binding(self) -> str:
        """The value that is checked for being unchanged before and after QA."""
        return f"{self.commit}:{self.tree_digest}:{'clean' if self.tree_clean else 'dirty'}"


#: Exit codes that mean an *infrastructure* error, never a product verdict.
#: 124 = timeout (coreutils convention), 126 = found but not executable
#: (also: refused by the guard), 127 = command not found.
INFRA_EXIT_CODES: frozenset[int] = frozenset({124, 126, 127})


class AcceptanceCheck(Strict):
    """A verifiable acceptance criterion. Without an executable command a
    criterion cannot be demonstrated mechanically and is therefore no gate.

    `check_id` is restricted to a character set because it becomes a file
    name: an exotic value used to make the run crash in the middle of
    VERIFYING with a StoreError, instead of failing cleanly as a contract
    violation.

    `expect_exit` must not expect an infrastructure code. Otherwise a plan
    with `expect_exit: 126` turns a check **refused** by the guard into a
    PASS -- and with 127 a process that never started.
    """

    check_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    description: str
    command: str
    expect_exit: int = Field(default=0, ge=0, le=125)
    expect_reason: str | None = Field(
        default=None,
        description="Mandatory when expect_exit != 0 -- an expected failure has "
        "to be justified, otherwise it is just a relabeled success.",
    )
    preserves: bool = Field(
        default=False,
        description="True = preservation requirement from an earlier checkpoint",
    )

    @model_validator(mode="after")
    def _nonzero_needs_reason(self) -> "AcceptanceCheck":
        # ge/le alone is not enough: 124 (timeout) lies inside 0..125 and would
        # otherwise have been admissible as an expected exit code.
        if self.expect_exit in INFRA_EXIT_CODES:
            raise ValueError(
                f"check {self.check_id}: expect_exit={self.expect_exit} is an "
                "infrastructure code (timeout/not executable/not found) and "
                "cannot expect a product verdict"
            )
        if self.expect_exit != 0 and not (self.expect_reason or "").strip():
            raise ValueError(
                f"check {self.check_id}: expect_exit={self.expect_exit} requires "
                "a justification in expect_reason"
            )
        return self


class DevelopmentPlan(Strict):
    """Delivered by the planner, frozen before DEVELOPING."""

    run_id: str
    iteration: int = Field(ge=1)
    base_candidate_id: str
    spec_digest: str
    objective: str = Field(min_length=1)
    targets: list[str] = Field(min_length=1)
    preserve: list[str] = Field(default_factory=list)
    acceptance_checks: list[AcceptanceCheck] = Field(min_length=1)
    out_of_scope: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    repair_only: bool = False
    repair_reason: str | None = None
    created_at: str = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _repair_needs_reason(self) -> DevelopmentPlan:
        # Handoff §5: a pure repair loop is allowed, but it has to be justified.
        if self.repair_only and not (self.repair_reason or "").strip():
            raise ValueError("repair_only requires a justification (repair_reason)")
        return self

    @model_validator(mode="after")
    def _unique_check_ids(self) -> DevelopmentPlan:
        ids = [c.check_id for c in self.acceptance_checks]
        if len(ids) != len(set(ids)):
            raise ValueError("acceptance_checks contain a duplicate check_id")
        return self

    def plan_digest(self) -> str:
        return digest(self.model_dump_json(exclude={"created_at"}))


# --------------------------------------------------------------------------- #
# Evidence (Handoff §6: the runner writes it, not the model)
# --------------------------------------------------------------------------- #


#: `IsolationRecord.effective` when isolation was requested, could not be
#: provided, and the command was therefore never started. Distinct from both
#: "none" and the requested value, because both of those read as a completed
#: execution and no execution took place.
ISOLATION_REFUSED = "refused"

#: `IsolationRecord.effective` when the command ran but left no proof that it
#: ran isolated. From outside, "bubblewrap failed during setup" and "the
#: command started and wrote no marker" look identical -- bubblewrap 0.9 exits
#: 1 for its own setup failures and for a check that exits 1 -- so this value
#: covers both and claims neither. It is never a pass: `honoured()` is False
#: and the runner records an infrastructure error.
ISOLATION_UNVERIFIED = "unverified"


class IsolationRecord(Strict):
    """How a check was executed, as measured rather than as configured.

    Written by the runner, like everything else on a `Receipt`. The reason it
    exists as structured fields instead of a sentence in `note`: the claim
    "this check ran sandboxed" was, until this record, only checkable by
    reading the configuration that *asked* for a sandbox. That is precisely
    the inference this project refuses everywhere else -- a configuration
    states an intention, and an intention is not a measurement.

    `effective` is what actually happened, and it has three shapes, not two:
    the isolation that was applied, `"none"` when the command ran without any,
    and `REFUSED` when isolation was requested, could not be provided, and the
    command therefore **never ran at all**. That third value exists because the
    other two both read as a completed execution. A refused run reporting
    `"none"` looks like a successful unsandboxed check; reporting `"strict"`
    looks like a successful sandboxed one. Neither happened.

    `fallback_to_none` stays a separate boolean for the remaining case: a
    backend that ran the command and returned less isolation than was asked
    for. A reader looking for exactly one thing finds exactly one thing.
    """

    #: What the caller asked for, by name.
    requested: str
    #: What was actually applied -- or `REFUSED`, when nothing was.
    effective: str
    #: The backend that provided it: "bubblewrap", "none", or "" when none was
    #: reached at all.
    backend: str = ""
    #: What the backend's own availability probe said. Empty means usable;
    #: otherwise the reason, in the backend's words.
    backend_probe: str = ""
    #: Whether isolation was requested and the run happened without it. False
    #: is the only value a sandboxed acceptance may carry.
    fallback_to_none: bool = False
    #: "denied" / "allowed" -- the network namespace policy actually applied.
    network_policy: str = ""
    #: "read-only" / "read-write" -- how the candidate tree was mounted.
    candidate_mount_mode: str = ""
    #: The resource ceilings applied, rendered: "as=<bytes>,nofile=<n>,timeout=<s>".
    #: "none" where nothing was limited, which is a statement, not an omission.
    #: These are the ceilings **after** clamping against the limits the runner
    #: itself inherited, not the ones that were asked for -- a receipt
    #: promising 600 CPU-seconds to a check killed by SIGXCPU at 60 is worse
    #: than no receipt.
    resource_limit_policy: str = ""
    #: Whether the isolation was confirmed from **inside** the sandbox -- the
    #: launched command recorded its own namespace ids and the candidate's
    #: writability before the check ran, and they were compared with the
    #: runner's. False means every other field here rests on the backend's
    #: self-report, which is the weaker claim and should read as one.
    verified_from_inside: bool = False
    #: The two sides of that comparison, kept so a third party can redo it.
    #:
    #: `verified_from_inside` is this runner's *verdict* on the numbers below.
    #: A reader who does not want to take the verdict on trust needs the
    #: numbers, and a reviewer said so plainly: without them the field is one
    #: more boolean whose derivation lives only in code that has already been
    #: wrong about exactly this. Empty on the unsandboxed path, where there is
    #: no claim to check.
    observed_namespaces: dict[str, str] = Field(default_factory=dict)
    runner_namespaces: dict[str, str] = Field(default_factory=dict)
    #: What the runner found wrong with the measurement, or empty.
    #:
    #: This field is the answer to a specific hole a reviewer walked through.
    #: `honoured()` used to be assembled from three fields, and there were
    #: branches where all three looked right while the runner had already
    #: refused the run: a short or junk proof left the mount mode "unknown",
    #: which produced a complaint and `runner_ok=False` -- and `effective`
    #: still equalled `requested`, `fallback_to_none` was still False, and
    #: `verified_from_inside` was still True. So the predicate said yes about
    #: a run the runner had said no about.
    #:
    #: Deriving the verdict from one field the runner writes when it objects
    #: removes the class, rather than adding a fourth condition to the three
    #: that were already not enough.
    complaint: str = ""

    def honoured(self) -> bool:
        """Did the run get the isolation it asked for?

        False for a refusal, which is the point: `runner_ok=False` already
        says the check produced no verdict, and this says why in one word.
        """
        if self.complaint:
            return False
        if self.effective == ISOLATION_REFUSED:
            return False
        if self.effective != self.requested or self.fallback_to_none:
            return False
        # Isolation that was requested has to have been *shown*, not reported.
        # The one case where nothing needs showing is a request for none:
        # there is no claim to verify.
        if self.requested != "none" and not self.verified_from_inside:
            return False
        return True


class Receipt(Strict):
    """An execution record produced by the runner.

    Deliberately NOT fillable by the model: `exit_code`, `started_at`,
    `ended_at` and `stdout_digest` come into being inside the runner. A
    self-report of 'all tests green' is not a receipt (Handoff §5).
    """

    receipt_id: str
    run_id: str
    iteration: int
    attempt: int
    check_id: str
    candidate_binding: str
    command: str
    exit_code: int
    started_at: str
    ended_at: str
    stdout_digest: str
    stdout_path: str | None = None
    runner_identity: str = Field(description="Who executed it (host/PID/version)")
    runner_ok: bool = Field(
        default=True,
        description="False = the check never got to run at all (guard, timeout, "
        "process not startable). Then the result is never PASS.",
    )
    truncated: bool = Field(
        default=False, description="True = output was cut off at the size limit"
    )
    isolation: IsolationRecord | None = Field(
        default=None,
        description=(
            "How the check was executed. Optional because receipts written "
            "before this record existed do not have it -- and a missing "
            "record means unknown, never 'it was isolated'."
        ),
    )

    def infrastructure_error(self) -> bool:
        return not self.runner_ok or self.exit_code in INFRA_EXIT_CODES

    def outcome(self, expect_exit: int) -> Outcome:
        """Handoff §5: an infrastructure error is not automatically a product
        defect, but it is also **never** PASS."""
        if self.infrastructure_error():
            return Outcome.INCONCLUSIVE
        return Outcome.PASS if self.exit_code == expect_exit else Outcome.FAIL


class CheckVerdict(Strict):
    check_id: str
    outcome: Outcome
    receipt_id: str | None = None
    reproduction: str | None = None
    note: str | None = None
    discriminates: bool = Field(
        default=False,
        description=(
            "Does this criterion demonstrate the increment -- is it red on "
            "the predecessor state and green on the candidate? Only such "
            "criteria prove progress. Preservation criteria are green on "
            "both states; that is their job, not a defect."
        ),
    )

    @model_validator(mode="after")
    def _pass_needs_receipt(self) -> CheckVerdict:
        # No PASS without a receipt -- the core defense against invented
        # successes.
        if self.outcome is Outcome.PASS and not self.receipt_id:
            raise ValueError(
                f"PASS without a receipt is inadmissible (check {self.check_id})"
            )
        return self


class RoleResult(Strict):
    """Result of a role run, with complete binding."""

    run_id: str
    iteration: int
    attempt: int
    role: Role
    candidate_id: str
    plan_digest: str
    spec_digest: str
    verdicts: list[CheckVerdict] = Field(default_factory=list)
    receipts: list[Receipt] = Field(default_factory=list)
    open_gaps: list[str] = Field(default_factory=list)
    summary: str = ""
    created_at: str = Field(default_factory=utcnow)

    def accepted(self) -> bool:
        """Every criterion PASS -- and at least one must demonstrate the increment.

        The first version required only "all PASS". That was too weak: a plan
        made up entirely of criteria that were already green on the
        predecessor state would have forced an acceptance without anything
        having changed. The countermeasure -- downgrading every
        non-discriminating criterion to INCONCLUSIVE -- was then too sharp: it
        also punished the **preservation criteria**, whose job consists of
        exactly that, being green on both states. A run whose increment was
        proven and whose regression protection held was rejected because of it.

        The separation lies between two questions: "does what already exists
        still hold?" (that is what preservation criteria deliver) and "has
        something been added?" (that is what discriminating ones deliver).
        Both are necessary; one must not confuse them.
        """
        if not self.verdicts:
            return False
        if not all(v.outcome is Outcome.PASS for v in self.verdicts):
            return False
        return any(v.discriminates for v in self.verdicts)


# --------------------------------------------------------------------------- #
# Budgets and run state
# --------------------------------------------------------------------------- #


class Budgets(Strict):
    """Conservative suggested values from Handoff §8. Operating limits, not
    demonstration limits; stricter local limits take precedence."""

    max_iterations: int = Field(default=10, ge=1)
    max_schema_repairs: int = Field(default=1, ge=0)
    max_transient_retries: int = Field(default=2, ge=0)
    max_loops_without_progress: int = Field(default=3, ge=1)
    deadline: str | None = None

    #: Handoff §8 demands money, token and wallclock limits by name.
    #: `None` means: no approval granted -- then nothing is guessed, instead
    #: the chargeable share stays declared as open.
    max_wallclock_seconds: int | None = Field(default=None, ge=1)
    max_dispatches: int | None = Field(default=None, ge=1)
    cost_budget_note: str | None = Field(
        default=None,
        description="Wording of the captain's cost approval. If it is missing, "
        "HoH does NOT book unknown usage as zero.",
    )


class Usage(Strict):
    iterations: int = 0
    schema_repairs: int = 0
    transient_retries: int = 0
    loops_without_progress: int = 0
    #: O21: iterations that ended without any verdict because an outage
    #: swallowed one -- an exhausted quota, an unanswered QA. Counted
    #: separately because it must not be *subtracted* from the iteration
    #: budget: the planner had already run and the money was already spent,
    #: so the spend bound is right to count it. What was wrong was the
    #: **message**: "iteration budget exhausted (3/3)" reads as "we tried
    #: three times and failed on the substance" when the truth may be "the
    #: infrastructure fell over three times". A tool built against claims
    #: that look supported must not make that one itself.
    outages: int = 0
    dispatches: int = 0


class TaskRef(Strict):
    """Reference to firstmate/Herdr identities. HoH stores references only,
    the owner of the metadata stays firstmate."""

    task_id: str
    role: Role
    harness: str
    herdr_pane_id: str | None = None
    herdr_workspace_id: str | None = None
    spawned_at: str = Field(default_factory=utcnow)
    attempt_key: str = Field(description="Unique per start intent -- prevents double starts")
    intent_recorded_at: str | None = None
    confirmed_running: bool = False


class RunState(Strict):
    """The one authoritative workflow state of a run."""

    schema_version: int = SCHEMA_VERSION
    run_id: str
    repo_path: str
    project_name: str
    stage: Stage = Stage.NEW
    condition: Condition = Condition.ACTIVE
    iteration: int = 0
    attempt: int = 0

    spec_path: str
    spec_digest: str
    policy_digest: str
    profile_digest: str
    #: The isolation this run's acceptance checks execute under, remembered
    #: across invocations.
    #:
    #: It lives on the state rather than in the command line because
    #: `hoh run <id>` *continues* an existing run. Without it, `hoh run X
    #: --isolation strict --iterations 1` followed by a plain `hoh run X` --
    #: after a pause, an approval, or simply a second invocation -- ran the
    #: remaining iterations unsandboxed, and the receipts said so quietly
    #: while the run as a whole still looked like a sandboxed one. A property
    #: of a run belongs to the run.
    isolation: str = "none"

    base_candidate: Candidate | None = None
    working_candidate: Candidate | None = None
    last_accepted_candidate: Candidate | None = None

    frozen_plan_digest: str | None = None
    frozen_at: str | None = None
    frozen_binding: str | None = None

    active_tasks: list[TaskRef] = Field(default_factory=list)
    budgets: Budgets = Field(default_factory=Budgets)
    usage: Usage = Field(default_factory=Usage)

    #: How many amendments this run has seen. Kept on the state because the
    #: chain itself is one file in a directory: renaming it aside would
    #: otherwise erase the obligation it created, and a thinner chain would be
    #: indistinguishable from no chain at all.
    amendments_seen: int = 0
    #: Criteria an amendment reopened and that a later iteration has already
    #: answered. Recorded rather than recomputed: `revalidation_needed()` reads
    #: the whole chain every time, so without this the obligation never
    #: cleared and a criterion re-measured in iteration 2 was demanded again in
    #: iteration 3, and forever.
    revalidated: list[str] = Field(default_factory=list)
    evidence_ref: str | None = Field(
        default=None,
        description="Pointer to the most recently validated evidence.json "
        "(E_t). The bundle lives in a file of its own; the state points at it "
        "only once that file has been durably written and validated "
        "(Handoff §7).",
    )
    evidence_digest: str | None = None

    stop_reason: str | None = None
    blocked_reason: str | None = None
    blocked_kind: str | None = Field(
        default=None,
        description=(
            "Machine-readable kind of the blockage, e.g. 'usage_limit'. The "
            "free text in `blocked_reason` is for humans; an automatic "
            "restart needs something it can rely on."
        ),
    )
    retry_after: str | None = Field(
        default=None,
        description="Earliest sensible restart (ISO-8601, UTC).",
    )
    resume_attempts: int = Field(
        default=0, ge=0,
        description=(
            "Unsuccessful automatic resumes in a row. A restart without a "
            "limit costs money without getting anywhere."
        ),
    )
    delivery_mode: Literal["no-mistakes", "direct-PR", "local-only"] = "local-only"
    yolo: Literal["on", "off"] = "off"

    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
    history: list[str] = Field(default_factory=list)

    write_seq: int = Field(
        default=0,
        description="Fencing token. Every write increments it; the store "
        "rejects a writer whose sequence is behind what lies on disk. A lock "
        "alone was not enough: it serializes writers, but it does not detect "
        "a stale state -- a controller holding an old RunState got the lock "
        "in its turn and overwrote an accepted checkpoint.",
    )

    def note(self, text: str) -> None:
        self.history.append(f"{utcnow()} {text}")
        self.updated_at = utcnow()

    started_at_wall: str | None = Field(
        default=None,
        description=(
            "Start time of this run segment as UTC ISO, for the wallclock "
            "limit. This used to hold `time.monotonic()` -- which counts from "
            "boot and was persisted anyway. After a reboot the difference came "
            "out negative (measured: 6762 - 82007 = -75245), and the limit "
            "could **never again** fire. Silently, without a message."
        ),
    )
    arena_secret: str | None = Field(
        default=None,
        description=(
            "Random value per run. The arena directory names are derived from "
            "it, so that a check command cannot recognize the control run "
            "from the path. With `case \"$PWD\" in *-basis) exit 1` a reviewer "
            "got an iteration accepted without any change at all."
        ),
    )

    def budget_exhausted(self) -> str | None:
        u, b = self.usage, self.budgets
        if u.iterations >= b.max_iterations:
            msg = f"iteration budget exhausted ({u.iterations}/{b.max_iterations})"
            if u.outages:
                msg += (
                    f" -- {u.outages} of them ended in an outage with no verdict,"
                    " so this bound was reached without that many substantive"
                    " attempts"
                )
            return msg
        if b.max_dispatches is not None and u.dispatches >= b.max_dispatches:
            return f"dispatch budget exhausted ({u.dispatches}/{b.max_dispatches})"
        if u.loops_without_progress >= b.max_loops_without_progress:
            return (
                f"{u.loops_without_progress} loops without traceable progress "
                f"(limit {b.max_loops_without_progress})"
            )
        if b.deadline and utcnow() > b.deadline:
            return f"deadline exceeded ({b.deadline})"
        if b.max_wallclock_seconds is not None and self.started_at_wall is not None:
            from datetime import datetime, timezone

            try:
                start = datetime.fromisoformat(self.started_at_wall.replace("Z", "+00:00"))
            except ValueError:
                start = None
            if start is not None:
                elapsed = (datetime.now(timezone.utc) - start).total_seconds()
                if elapsed >= b.max_wallclock_seconds:
                    return (
                        f"wallclock budget exhausted ({int(elapsed)}s/"
                        f"{b.max_wallclock_seconds}s)"
                    )

        return None

    def cost_disclosure(self) -> str:
        """What can honestly be said about cost.

        Handoff §8: *"If a cost approval is missing, do not invent a new one ...
        Do not book unknown usage as zero. Provider requests that cannot be
        canceled can still incur cost despite a local stop."*
        """
        if self.budgets.cost_budget_note:
            return (
                f"Captain's cost approval: {self.budgets.cost_budget_note}. "
                f"{self.usage.dispatches} role runs dispatched; the actual "
                "token and money cost sits with the providers, HoH does not "
                "measure it."
            )
        return (
            "No cost approval on file. HoH does not invent one -- the "
            f"chargeable share stays declared as open: {self.usage.dispatches} "
            "role runs dispatched, actual token and money cost unknown. A "
            "provider request that has already been dispatched can still "
            "incur cost even after a local stop."
        )
