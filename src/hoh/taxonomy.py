"""Why a dispatch ended, and what each reason licenses the orchestrator to do.

Until this file existed the project had two ways of talking about failure. A
run carried `Condition`, `stop_reason` and a handful of halt classes; a node
carried `RunVerdict`. Both are about *state*. Neither says **why**, and the
difference between "the provider is rate-limited" and "the role broke its
contract" is the difference between waiting four minutes and never retrying at
all.

The cost of not having it is on the record. A usage-quota exhaustion and a
role that returned malformed JSON were indistinguishable (`docs/LIMITATIONS.md`
limit 16), so the orchestrator could only choose one behaviour for both: it
retried the broken contract and it gave up on the quota, or the reverse.
Neither is right, and no amount of care in the retry loop fixes a loop that
cannot tell what it is retrying.

## What a class is

A class is not a label on an error message. It is a **policy**, and every
field of `Disposition` answers a question the orchestrator would otherwise
have to guess:

* may this be retried at all, and how many times;
* does the retry budget belong to the node, the run, or the campaign -- a
  provider outage that costs a node its whole budget converts an outage into a
  permanent failure;
* may a *fresh process* resume it unattended, or does resuming require someone
  to have looked;
* what evidence has to exist before the class may be assigned, so that
  "PROVIDER_QUOTA" cannot become the place unexplained failures go.

## What it deliberately is not

It does not classify by exception type or by string matching on a provider's
message. Those change without notice and across providers. Classification
happens where the information is: at the dispatch boundary, from an exit code,
an HTTP status, a contract validation, or an explicit signal -- and anything
that cannot be classified from those is `UNKNOWN`, which is a real state with
a real policy (stop, keep the evidence, ask) rather than a synonym for
"probably transient".
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .contracts import Strict


class FailureClass(StrEnum):
    """Why a dispatch or an orchestration step did not succeed."""

    #: The provider accepted the request and asked for it later. Transient by
    #: construction, and the one class where waiting is the correct response.
    PROVIDER_RATE_LIMIT = "PROVIDER_RATE_LIMIT"
    #: The account's allowance is spent. Retrying inside this run cannot help;
    #: retrying in an hour might. Distinct from a rate limit because the wait
    #: is of a different order and the remedy may be a human's.
    PROVIDER_QUOTA = "PROVIDER_QUOTA"
    #: A timeout, a dropped connection, a 5xx. Retry, with a ceiling.
    PROVIDER_TRANSIENT = "PROVIDER_TRANSIENT"
    #: Credentials rejected or expired. Never retryable by a machine: the same
    #: call will be rejected the same way until a person changes something.
    #: This project lost a whole campaign's QA verdicts to an expired
    #: credential that presented as an ordinary dispatch.
    PROVIDER_AUTH = "PROVIDER_AUTH"
    #: The role answered, and the answer does not satisfy the contract --
    #: malformed JSON, a missing field, a receipt id that does not exist. A
    #: retry is reasonable once: models are not deterministic. More than that
    #: is the loop hoping.
    CONTRACT_INVALID = "CONTRACT_INVALID"
    #: The role answered, the answer is well-formed, and it says the work
    #: failed. Not an infrastructure problem: the product did not pass.
    ROLE_FAILED = "ROLE_FAILED"
    #: Something outside the system has to agree first -- a trust prompt, a
    #: policy disposition. Not a failure at all, and never retried: retrying
    #: would ask the same question again without anyone having answered it.
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    #: A merge that git refused because the two sides changed the same thing.
    #: Automatic retry cannot help; a repair node can.
    MERGE_CONFLICT = "MERGE_CONFLICT"
    #: A merge that git refused for a reason that is *not* about content --
    #: untracked files in the way, a dirty tree, a lock. Often fixable without
    #: a human, and importantly not a conflict, which is why it has its own
    #: class.
    MERGE_OBSTRUCTED = "MERGE_OBSTRUCTED"
    #: The machinery under the work failed: no disk, no interpreter, a killed
    #: process group, a sandbox that could not be provided. Never a product
    #: verdict -- this is what `INFRA_EXIT_CODES` becomes at this layer.
    INFRASTRUCTURE = "INFRASTRUCTURE"
    #: The persisted state cannot be read or contradicts itself. Stop. A
    #: retry on a corrupt state is how one bad write becomes several.
    CORRUPT_STATE = "CORRUPT_STATE"
    #: The run's own budget is spent -- iterations, dispatches, wallclock. Not
    #: a provider problem and not a product verdict: a policy limit this
    #: project set for itself, reached. It was classified UNKNOWN, which sends
    #: a reader looking for a defect instead of a decision about a ceiling.
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    #: A role touched a tree its policy protects. The state is perfectly
    #: readable -- which is why this is **not** CORRUPT_STATE, where it briefly
    #: lived: the defect is a boundary crossing, not a bad write, and filing it
    #: under the state's own integrity would make two different situations
    #: share one word in the ledger.
    CAPABILITY_VIOLATION = "CAPABILITY_VIOLATION"
    #: Could not be classified from the evidence available. A real state with
    #: a real policy, not a synonym for "probably transient".
    UNKNOWN = "UNKNOWN"


class BudgetScope(StrEnum):
    """Whose allowance a retry spends.

    The distinction is not bookkeeping. A provider outage that spends the
    *node's* iteration budget converts an outage into a permanent failure for
    that node: the work never got a chance and the budget is gone. Attempts
    against the infrastructure belong to the attempt counter, which is
    separate from the iteration counter for exactly this reason.
    """

    #: Spends the node's iterations. For failures that are *about* the work.
    NODE = "node"
    #: Spends only the attempt counter within one iteration. For failures that
    #: are about the machinery, where the work has not been tried yet.
    ATTEMPT = "attempt"
    #: Spends nothing. For states where retrying is not the response at all.
    NONE = "none"


class Disposition(Strict):
    """What the orchestrator may do about one class of failure."""

    failure_class: FailureClass
    #: How many times this may be retried. 0 means never.
    max_retries: int = 0
    #: Whose allowance those retries spend.
    budget: BudgetScope = BudgetScope.NONE
    #: Seconds to wait before the first retry; doubled each time, capped at
    #: `backoff_ceiling`. 0 means retry immediately.
    backoff_seconds: int = 0
    backoff_ceiling: int = 3600
    #: May a fresh process pick this up with nobody watching? False does not
    #: mean "broken": `NEEDS_APPROVAL` is a perfectly healthy state that
    #: simply cannot be advanced by a machine.
    auto_resume: bool = False
    #: Must a person act before this can proceed?
    human_gate: bool = False
    #: What has to be on the record before this class may be assigned. The
    #: point is to stop a class becoming the place unexplained failures go:
    #: assigning `PROVIDER_QUOTA` without the provider having said so is a
    #: guess wearing a label.
    required_evidence: str = ""
    #: Why this policy and not another. Kept in the data because a retry
    #: policy without its reasoning gets "tuned" by whoever meets it next.
    rationale: str = ""


#: The policy table. Read it as the answer to "what does the orchestrator do
#: when it sees this", and note how few classes are actually retryable: the
#: cases where retrying is right are the minority, and treating every failure
#: as transient is how a loop spends a budget without doing any work.
POLICY: dict[FailureClass, Disposition] = {
    FailureClass.PROVIDER_RATE_LIMIT: Disposition(
        failure_class=FailureClass.PROVIDER_RATE_LIMIT,
        max_retries=5, budget=BudgetScope.ATTEMPT,
        backoff_seconds=30, backoff_ceiling=900,
        auto_resume=True,
        required_evidence="a 429, a Retry-After header, or the provider's own "
                          "rate-limit signal -- not an inference from slowness",
        rationale="the provider said 'later', so later is the whole remedy. "
                  "The attempt counter pays, not the iteration counter: the "
                  "work has not been tried yet",
    ),
    FailureClass.PROVIDER_QUOTA: Disposition(
        failure_class=FailureClass.PROVIDER_QUOTA,
        max_retries=3, budget=BudgetScope.ATTEMPT,
        backoff_seconds=900, backoff_ceiling=3600,
        auto_resume=True,
        required_evidence="the provider named the allowance as exhausted, or a "
                          "quota probe the deployment configured said so",
        rationale="waiting can help and may not; the ceiling keeps a spent "
                  "allowance from turning into an unbounded loop. A human may "
                  "have to raise it, which is why the retries are few",
    ),
    FailureClass.PROVIDER_TRANSIENT: Disposition(
        failure_class=FailureClass.PROVIDER_TRANSIENT,
        max_retries=3, budget=BudgetScope.ATTEMPT,
        backoff_seconds=10, backoff_ceiling=300,
        auto_resume=True,
        required_evidence="a timeout, a dropped connection or a 5xx from the "
                          "provider",
        rationale="the classic retryable failure, and the only reason for the "
                  "ceiling is that an indefinite one is indistinguishable from "
                  "a hang",
    ),
    FailureClass.PROVIDER_AUTH: Disposition(
        failure_class=FailureClass.PROVIDER_AUTH,
        max_retries=0, budget=BudgetScope.NONE,
        auto_resume=False, human_gate=True,
        required_evidence="a 401/403, or a harness reporting the credential as "
                          "rejected or expired",
        rationale="the same call will be rejected identically until a person "
                  "changes something. This project lost a campaign's QA "
                  "verdicts to an expired credential that presented as an "
                  "ordinary dispatch, so silence here is expensive",
    ),
    FailureClass.CONTRACT_INVALID: Disposition(
        failure_class=FailureClass.CONTRACT_INVALID,
        max_retries=1, budget=BudgetScope.ATTEMPT,
        backoff_seconds=0,
        auto_resume=True,
        required_evidence="the validation error, naming the field, and the "
                          "answer that failed it",
        rationale="models are not deterministic, so one more attempt is "
                  "reasonable. A second is the loop hoping, and a role that "
                  "cannot satisfy its contract twice is a finding, not a retry",
    ),
    FailureClass.ROLE_FAILED: Disposition(
        failure_class=FailureClass.ROLE_FAILED,
        max_retries=0, budget=BudgetScope.NODE,
        auto_resume=True,
        required_evidence="a well-formed role result whose verdict is negative, "
                          "with the receipts it rests on",
        rationale="not an infrastructure problem: the product did not pass. "
                  "The next iteration is the retry, and it costs an iteration "
                  "because the work really was attempted",
    ),
    FailureClass.NEEDS_APPROVAL: Disposition(
        failure_class=FailureClass.NEEDS_APPROVAL,
        max_retries=0, budget=BudgetScope.NONE,
        auto_resume=False, human_gate=True,
        required_evidence="the prompt or policy that is waiting, named",
        rationale="retrying asks the same question again without anyone having "
                  "answered it. A configured approval authority can answer it; "
                  "absent one, a person must",
    ),
    FailureClass.MERGE_CONFLICT: Disposition(
        failure_class=FailureClass.MERGE_CONFLICT,
        max_retries=0, budget=BudgetScope.NONE,
        auto_resume=True,
        required_evidence="git's own conflict report, naming the paths",
        rationale="repeating the merge produces the same conflict. The remedy "
                  "is a repair node that resolves it as work, which is "
                  "automatic and is not a retry",
    ),
    FailureClass.MERGE_OBSTRUCTED: Disposition(
        failure_class=FailureClass.MERGE_OBSTRUCTED,
        max_retries=2, budget=BudgetScope.ATTEMPT,
        backoff_seconds=0,
        auto_resume=True,
        required_evidence="git refused for a reason that names no conflicting "
                          "content -- untracked files in the way, a dirty tree, "
                          "an index lock",
        rationale="often the obstruction is transient and removable without a "
                  "human. Kept apart from a conflict because merging into it "
                  "again can genuinely work",
    ),
    FailureClass.INFRASTRUCTURE: Disposition(
        failure_class=FailureClass.INFRASTRUCTURE,
        max_retries=2, budget=BudgetScope.ATTEMPT,
        backoff_seconds=5, backoff_ceiling=60,
        auto_resume=True,
        required_evidence="an infrastructure exit code (124, 126, 127), a "
                          "refused sandbox, or an OSError from the runner",
        rationale="never a product verdict. The attempt counter pays, because "
                  "a check that could not run has measured nothing about the "
                  "work",
    ),
    FailureClass.CORRUPT_STATE: Disposition(
        failure_class=FailureClass.CORRUPT_STATE,
        max_retries=0, budget=BudgetScope.NONE,
        auto_resume=False, human_gate=True,
        required_evidence="the read error or the invariant that the state "
                          "violates, quoted",
        rationale="a retry on a corrupt state is how one bad write becomes "
                  "several. Stop, keep everything, let someone look",
    ),
    FailureClass.BUDGET_EXHAUSTED: Disposition(
        failure_class=FailureClass.BUDGET_EXHAUSTED,
        max_retries=0, budget=BudgetScope.NONE,
        auto_resume=False, human_gate=True,
        required_evidence="the budget that was reached and the usage against "
                          "it, both from the run's own state",
        rationale="a retry cannot create budget, and raising one is a decision "
                  "about how much this work is worth -- which is a person's",
    ),
    FailureClass.CAPABILITY_VIOLATION: Disposition(
        failure_class=FailureClass.CAPABILITY_VIOLATION,
        max_retries=0, budget=BudgetScope.NONE,
        auto_resume=False, human_gate=True,
        required_evidence="the protected tree that changed, with the digest "
                          "before and after, as the witness recorded it",
        rationale="the measurement was interfered with, so a retry would "
                  "produce a second verdict about the same disturbed state. "
                  "A person has to decide whether the run is salvageable",
    ),
    FailureClass.UNKNOWN: Disposition(
        failure_class=FailureClass.UNKNOWN,
        max_retries=0, budget=BudgetScope.NONE,
        auto_resume=False, human_gate=True,
        required_evidence="none -- this is the class for when the evidence did "
                          "not support any other",
        rationale="a real state with a real policy. Treating the unclassified "
                  "as transient is how a loop burns an allowance on a failure "
                  "it never understood, and this project has watched a run do "
                  "exactly that",
    ),
}


class Attempt(Strict):
    """One try, and what it cost."""

    number: int = Field(ge=1)
    started_at: str
    ended_at: str = ""
    failure_class: FailureClass | None = None
    detail: str = ""
    waited_seconds: int = 0


def disposition(fc: FailureClass) -> Disposition:
    """The policy for a class. Every class has one, by construction."""
    return POLICY[fc]


def may_retry(fc: FailureClass, attempts_so_far: int) -> bool:
    """Is another attempt permitted?

    `attempts_so_far` counts attempts already made, so the first retry asks
    with 1. A class with `max_retries=0` answers False from the start, which
    is the point: `PROVIDER_AUTH` and `CORRUPT_STATE` never get a second try
    from a machine.
    """
    return attempts_so_far <= disposition(fc).max_retries


def backoff_for(fc: FailureClass, attempt: int) -> int:
    """Seconds to wait before attempt number `attempt` (1 = the first retry).

    Doubling, capped. Returns 0 where the class does not wait -- and a class
    with `max_retries=0` returns 0 because it never reaches a retry at all.
    """
    d = disposition(fc)
    if d.max_retries == 0 or d.backoff_seconds == 0 or attempt < 1:
        return 0
    return min(d.backoff_seconds * (2 ** (attempt - 1)), d.backoff_ceiling)


def classify_failure(exc: BaseException | None, *, detail: str = "") -> FailureClass:
    """The class of a dispatch failure, from the exception that ended it.

    Never returns `None`: a dispatch that failed has a class, and `UNKNOWN` is
    a real one with a real policy ("stop rather than hope"). A record left
    without a class is worse than one classified as unknown -- it reads as a
    failure nobody looked at, which is what the dispatch log was full of until
    `tools/telemetry_audit.py` counted them.

    Classification is by **type first**, and only then by the message. The
    types are this project's own and are reliable; the message is the
    provider's and is not. Where neither settles it, the answer is UNKNOWN
    rather than a plausible guess -- a mislabelled rate limit and a
    mislabelled quota have different remedies.
    """
    # --- by type, which is this project's own and is reliable ------------- #
    for k in type(exc).__mro__ if exc is not None else ():
        nach_typ = {
            "CapabilityViolation": FailureClass.CAPABILITY_VIOLATION,
            "WaitingForApproval": FailureClass.NEEDS_APPROVAL,
            "RoleOutputError": FailureClass.CONTRACT_INVALID,
            "StoreError": FailureClass.CORRUPT_STATE,
            "FreezeError": FailureClass.CORRUPT_STATE,
        }.get(k.__name__)
        if nach_typ is not None:
            return nach_typ
    if getattr(exc, "transient", False):
        # `DispatchError.transient` is set where the failure happened, by the
        # code that saw it. Nothing in a message beats that.
        return FailureClass.PROVIDER_TRANSIENT

    # --- only then by the message, and only its first line ----------------- #
    # A dispatch failure's detail carries the agent's own terminal output --
    # forty lines of whatever it was doing. Searching all of it for "approval"
    # or "contract" classifies a network timeout by what happened to be on
    # screen, and this project dogfoods on a tree full of files called
    # `approval.py`, `contracts.py` and `trust.py`. Only the first line is the
    # failure's own sentence.
    erste = (detail or str(exc or "")).strip().splitlines()
    text = erste[0].lower() if erste else ""
    for muster, klasse in (
        ("capability violation", FailureClass.CAPABILITY_VIOLATION),
        ("dispatch refused", FailureClass.BUDGET_EXHAUSTED),
        ("budget exhausted", FailureClass.BUDGET_EXHAUSTED),
        ("waits for an approval", FailureClass.NEEDS_APPROVAL),
        ("needs approval", FailureClass.NEEDS_APPROVAL),
        # Quota before rate limit: a message naming both is about the ceiling,
        # not the pace, and the two wait times differ by an order of magnitude.
        ("quota", FailureClass.PROVIDER_QUOTA),
        ("usage limit", FailureClass.PROVIDER_QUOTA),
        ("rate limit", FailureClass.PROVIDER_RATE_LIMIT),
        ("429", FailureClass.PROVIDER_RATE_LIMIT),
        ("output contract", FailureClass.CONTRACT_INVALID),
        ("schema repair", FailureClass.CONTRACT_INVALID),
        ("credential", FailureClass.PROVIDER_AUTH),
        ("unauthor", FailureClass.PROVIDER_AUTH),
        ("401", FailureClass.PROVIDER_AUTH),
        ("timed out", FailureClass.PROVIDER_TRANSIENT),
        ("timeout", FailureClass.PROVIDER_TRANSIENT),
        ("connection", FailureClass.PROVIDER_TRANSIENT),
    ):
        if muster in text:
            return klasse
    return FailureClass.UNKNOWN


def classify_exit(code: int) -> FailureClass:
    """The class implied by a runner exit code, for the cases where one is.

    Only the infrastructure codes map here. Everything else is a product
    outcome and is not this function's business -- returning `UNKNOWN` for a
    plain non-zero exit is correct, because a failing check is not a failure
    of the dispatch.
    """
    from .contracts import INFRA_EXIT_CODES

    return FailureClass.INFRASTRUCTURE if code in INFRA_EXIT_CODES else FailureClass.UNKNOWN


def auto_resumable(fc: FailureClass) -> bool:
    """May an unattended process carry this forward?"""
    return disposition(fc).auto_resume


def needs_human(fc: FailureClass) -> bool:
    return disposition(fc).human_gate
