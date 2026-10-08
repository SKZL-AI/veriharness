"""The checklist supervisor: typed findings, a bounded review capsule, and the
validation Core applies before a finding may touch programme state (V3.3 P2-27).

The rule this module turns on, from the plan:

    Supervisor can FIND.  Reconciler can VERIFY.

A supervisor asks what per-run QA never asks -- are we building everything the
programme promised, and is every claimed completion still justified? -- and
answers with `SupervisorFinding`s and nothing else. It may not set a status,
waive an item, accept a candidate or publish anything.

- `ReviewCapsule` is what a supervisor is *given*: the phase under review, the
  checklist snapshot, the open findings, the changed source digests and a
  short list of evidence summaries. It is bounded and fails closed when asked
  to carry more, naming what would have been dropped. "Do not dump historical
  chat."
- `validate` says why Core may not act on a finding (or `[]` when it may).
- `apply_finding` is the only path from a finding to a checklist, and it does
  exactly one thing: mark the named items REOPENED with the finding's reason.
  Its result is unsealed, so no phase gate closes on it.
- `reject_finding` records that a finding was false (SV-05).

Everything here is pure: no I/O, no clock, inputs are never mutated. Nothing in
this module computes or sets a seal -- `reconcile` stays the only producer of a
sealed `Snapshot`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum

from pydantic import Field, ValidationError, field_validator, model_validator

from hoh.checklist import Snapshot, Status, Transition
from hoh.contracts import Strict


class FindingKind(StrEnum):
    NO_ACTION = "NO_ACTION"
    PROPOSE_REOPEN = "PROPOSE_REOPEN"
    PROPOSE_REVALIDATION = "PROPOSE_REVALIDATION"
    PROPOSE_REPAIR_NODE = "PROPOSE_REPAIR_NODE"
    PROPOSE_MISSING_WORK_ITEM = "PROPOSE_MISSING_WORK_ITEM"
    PROPOSE_DEPENDENCY = "PROPOSE_DEPENDENCY"
    PHASE_NOT_READY = "PHASE_NOT_READY"
    RELEASE_NOT_READY = "RELEASE_NOT_READY"


class FindingStatus(StrEnum):
    OPEN = "OPEN"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"


#: Statuses in which a finding has already been decided and is no input any more.
DECIDED = frozenset({FindingStatus.REJECTED, FindingStatus.APPLIED})

#: Kinds whose verdict concerns a phase (or the release) rather than single
#: items, so `item_ids` may name a phase of the snapshot instead of an item.
PHASE_LEVEL = frozenset({FindingKind.PHASE_NOT_READY, FindingKind.RELEASE_NOT_READY})

#: The only checklist status a supervisor may set.
SUPERVISOR_STATUS = Status.REOPENED

#: Statuses a supervisor may never set; named in the refusal.
FORBIDDEN_TO_SUPERVISOR = (Status.VERIFIED, Status.WAIVED, Status.NOT_APPLICABLE)


def _require_text(value: str, name: str) -> str:
    # Strict strips whitespace first, so whitespace-only arrives here as "".
    if not value:
        raise ValueError(f"{name} must not be blank")
    return value


def _one_line(text: str) -> str:
    return " ".join(text.split())


class SupervisorFinding(Strict):
    """One thing a supervisor found. A finding proposes; it never decides.

    `reason` must say what is wrong: a finding nobody can act on is noise, and
    noise is what makes a review ignorable, so a blank reason is refused at
    construction. `status` starts OPEN; `reject_finding` moves it to
    REJECTED and records who rejected it and why.
    """

    finding_id: str
    review_id: str
    kind: FindingKind
    severity: str
    item_ids: list[str] = Field(default_factory=list)
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)
    proposed_action: str | None = None
    supervisor_provider: str
    status: FindingStatus = FindingStatus.OPEN
    rejection_reason: str | None = None
    rejected_by: str | None = None

    @field_validator("finding_id", "review_id", "severity", "reason", "supervisor_provider")
    @classmethod
    def _not_blank(cls, value: str, info) -> str:
        return _require_text(value, info.field_name)

    @model_validator(mode="after")
    def _rejection_is_recorded(self) -> SupervisorFinding:
        # A REJECTED finding without the why and the who is a rejection nobody
        # can audit -- SV-05 asks that the rejection be *recorded*.
        if self.status is FindingStatus.REJECTED and not (
                (self.rejection_reason or "").strip() and (self.rejected_by or "").strip()):
            raise ValueError(
                "a REJECTED finding must record rejection_reason and rejected_by")
        return self


class ReviewCapsule(Strict):
    """What a supervisor is given -- bounded on purpose.

    `max_items` bounds the checklist items in `snapshot`, the `open_findings`
    and the `changed_source_digests` (item_id -> new digest), each on its own;
    `max_evidence` bounds `evidence_summaries`, and `max_summary_chars` the
    length of each summary. Asking for more fails closed with a message naming
    how many entries, and which, would have been dropped: nothing is
    truncated silently, because a supervisor that reviewed half the checklist
    without knowing it would report the half as the whole.
    """

    phase: str
    snapshot: Snapshot
    open_findings: list[SupervisorFinding] = Field(default_factory=list)
    changed_source_digests: dict[str, str] = Field(default_factory=dict)
    evidence_summaries: list[str] = Field(default_factory=list)
    max_items: int = Field(default=50, ge=1)
    max_evidence: int = Field(default=10, ge=1)
    max_summary_chars: int = Field(default=500, ge=1)

    @field_validator("phase")
    @classmethod
    def _phase_not_blank(cls, value: str) -> str:
        return _require_text(value, "phase")

    @model_validator(mode="after")
    def _about_something_that_exists(self) -> ReviewCapsule:
        """The capsule's own QA asked for this: a capsule for a phase the
        snapshot does not hold, or one carrying findings that have already been
        decided, sends a supervisor to review something that is not there.
        Refused rather than reviewed."""
        phases = {item.phase for item in self.snapshot.items}
        if phases and self.phase not in phases:
            raise ValueError(
                f"the capsule is about phase {self.phase!r} and its snapshot holds "
                f"{', '.join(sorted(phases))}: a review of a phase that is not there "
                f"would report about nothing")
        decided = [f.finding_id for f in self.open_findings
                   if f.status is not FindingStatus.OPEN]
        if decided:
            raise ValueError(
                f"the capsule carries findings that have already been decided "
                f"({', '.join(decided)}); a supervisor asked to weigh a closed finding "
                f"is being asked to reopen a decision it may not take")
        return self

    @model_validator(mode="after")
    def _bounded(self) -> ReviewCapsule:
        over: list[str] = []

        def check(what: str, names: list[str], bound: int, bound_name: str) -> None:
            if len(names) > bound:
                dropped = names[bound:]
                over.append(
                    f"{what}: {len(names)} exceed {bound_name}={bound}; "
                    f"{len(dropped)} would be dropped: {', '.join(dropped)}")

        check("checklist items", [i.item_id for i in self.snapshot.items],
              self.max_items, "max_items")
        check("open findings", [f.finding_id for f in self.open_findings],
              self.max_items, "max_items")
        check("changed source digests", list(self.changed_source_digests),
              self.max_items, "max_items")
        check("evidence summaries",
              [f"#{n} {_one_line(s)[:40]!r}" for n, s in enumerate(self.evidence_summaries)],
              self.max_evidence, "max_evidence")
        too_long = [f"#{n} ({len(s)} chars)" for n, s in enumerate(self.evidence_summaries)
                    if len(s) > self.max_summary_chars]
        if too_long:
            over.append(
                f"evidence summaries longer than max_summary_chars="
                f"{self.max_summary_chars} would be cut: {', '.join(too_long)}")
        if over:
            raise ValueError(
                "review capsule over its bound, refused rather than truncated -- "
                + " | ".join(over))
        return self


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #


def _names_phase(text: str, phase: str) -> bool:
    """`phase` appears in `text` as a whole token: 'P1' is not named by 'P10'."""
    return re.search(rf"(?<![\w-]){re.escape(phase)}(?![\w-])", text) is not None


def _schema_checked(finding) -> tuple[SupervisorFinding | None, list[str]]:
    """Re-validate the finding against the schema (SV-02).

    A dict is validated; an instance is re-validated from its dump, because
    the model does not validate assignment and a reason blanked after
    construction must not slip through as a finding.
    """
    try:
        if isinstance(finding, SupervisorFinding):
            return SupervisorFinding.model_validate(finding.model_dump()), []
        if isinstance(finding, Mapping):
            return SupervisorFinding.model_validate(dict(finding)), []
    except ValidationError as exc:
        errors = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(finding)'}: {e['msg']}"
            for e in exc.errors())
        return None, [f"finding is not schema-valid: {errors}"]
    return None, [f"finding is not a SupervisorFinding: {type(finding).__name__}"]


def validate(finding, *, snapshot: Snapshot) -> list[str]:
    """Why Core may not act on `finding`, one reason per defect; `[]` when it may.

    1. every entry of `item_ids` must be an item of `snapshot` -- or, for
       PHASE_NOT_READY / RELEASE_NOT_READY, a phase of it;
    2. NO_ACTION must carry no proposed action, every other kind must carry one;
    3. a finding already REJECTED or APPLIED has been decided and is no input;
    4. PHASE_NOT_READY must name a phase of the snapshot in `item_ids` or in
       `proposed_action`.

    `finding` may be a `SupervisorFinding` or a mapping of its fields; either
    is schema-validated first, and a schema failure is the only reason given.
    """
    checked, problems = _schema_checked(finding)
    if checked is None:
        return problems
    f = checked
    out: list[str] = []
    item_ids = {i.item_id for i in snapshot.items}
    phases = {i.phase for i in snapshot.items}

    # Rule 1
    allowed = item_ids | (phases if f.kind in PHASE_LEVEL else set())
    unknown = [i for i in dict.fromkeys(f.item_ids) if i not in allowed]
    for i in unknown:
        out.append(
            f"finding {f.finding_id} names {i!r}, which is not an item"
            + (" or a phase" if f.kind in PHASE_LEVEL else "")
            + f" of the snapshot (generation {snapshot.generation})")

    # Rule 2
    action = (f.proposed_action or "").strip()
    if f.kind is FindingKind.NO_ACTION and action:
        out.append(
            f"finding {f.finding_id} is NO_ACTION but proposes an action "
            f"({action!r}): a finding that proposes something is not NO_ACTION")
    elif f.kind is not FindingKind.NO_ACTION and not action:
        out.append(
            f"finding {f.finding_id} is {f.kind.value} but proposes no action: "
            "a finding nobody can act on is not actionable")

    # Rule 3
    if f.status in DECIDED:
        out.append(
            f"finding {f.finding_id} is already {f.status.value}: it has been "
            "decided and is not an input any more")

    # Rule 4
    if f.kind is FindingKind.PHASE_NOT_READY:
        named = sorted(p for p in phases
                       if p in f.item_ids or _names_phase(action, p))
        if not named:
            out.append(
                f"finding {f.finding_id} is PHASE_NOT_READY but names no phase of the "
                f"snapshot in item_ids or proposed_action (phases: "
                f"{', '.join(sorted(phases)) or 'none'})")
    return out


# --------------------------------------------------------------------------- #
# apply_finding
# --------------------------------------------------------------------------- #


def apply_finding(finding, *, snapshot: Snapshot,
                  to_status: Status = SUPERVISOR_STATUS) -> tuple[Snapshot, str]:
    """The only way a finding reaches the checklist: reopen the named items.

    Returns a *new* snapshot and a one-line description of what changed. Each
    checklist item the finding names is set to REOPENED with
    `reopen_reason = finding.reason`, the finding id is added to its
    `finding_refs`, and one `Transition` per item records the change. The
    input snapshot is not mutated.

    The returned snapshot is deliberately **unsealed** (`seal=None`): it was
    not produced by `reconcile`, so `phase_gate` refuses to close on it. Only
    a fresh `reconcile` against evidence can seal a snapshot again -- and that
    reconciliation decides from evidence alone, so a finding that should keep
    an item from verifying must reach it as a confirmed finding in the item's
    `ItemEvidence.open_findings`. The supervisor finds; the reconciler verifies.

    Refused with ValueError:
    - any `to_status` other than REOPENED -- VERIFIED, WAIVED and
      NOT_APPLICABLE are never a supervisor's to set;
    - any finding `validate()` rejects, with its reasons.

    Entries of `item_ids` that name a phase rather than an item (allowed for
    PHASE_NOT_READY and RELEASE_NOT_READY) reopen nothing. A NO_ACTION finding,
    or one naming no checklist item at all, changes no item: the result is the
    same items, still unsealed, and the description says nothing was reopened.
    No path through this function sets a closing status.
    """
    if to_status != SUPERVISOR_STATUS:
        raise ValueError(
            f"a supervisor may only set {SUPERVISOR_STATUS.value}, not {to_status}: "
            f"{', '.join(s.value for s in FORBIDDEN_TO_SUPERVISOR)} are set by "
            "reconcile against evidence or a complete waiver, never by a finding")
    problems = validate(finding, snapshot=snapshot)
    if problems:
        raise ValueError("finding refused: " + "; ".join(problems))
    f, _ = _schema_checked(finding)

    named = set(f.item_ids) if f.kind is not FindingKind.NO_ACTION else set()
    items = []
    transitions = [t.model_copy(deep=True) for t in snapshot.transitions]
    reopened: list[str] = []
    for original in snapshot.items:
        item = original.model_copy(deep=True)
        if item.item_id in named:
            before = item.status
            item.status = Status.REOPENED
            item.reopen_reason = f.reason
            if f.finding_id not in item.finding_refs:
                item.finding_refs = [*item.finding_refs, f.finding_id]
            transitions.append(Transition(
                item_id=item.item_id, from_status=before, to_status=Status.REOPENED,
                why=f"supervisor finding {f.finding_id} ({f.kind.value}): {f.reason}"))
            reopened.append(item.item_id)
        items.append(item)

    new = Snapshot(items=items, generation=snapshot.generation,
                   transitions=transitions, seal=None)
    if reopened:
        desc = (f"finding {f.finding_id} ({f.kind.value}) reopened "
                f"{', '.join(reopened)}: {f.reason}")
    else:
        desc = (f"finding {f.finding_id} ({f.kind.value}) names no checklist item; "
                "nothing reopened, snapshot left unsealed")
    return new, _one_line(desc)


# --------------------------------------------------------------------------- #
# reject_finding (SV-05)
# --------------------------------------------------------------------------- #


def reject_finding(finding: SupervisorFinding, *, reason: str,
                   rejected_by: str) -> SupervisorFinding:
    """Record that `finding` is false: a copy with status REJECTED, the reason
    and the actor. The input is not mutated.

    Refused with ValueError: a blank reason or actor (a rejection nobody can
    audit is a silent drop), and a finding already REJECTED or APPLIED. The
    rejected copy is then refused by `validate` and `apply_finding`.
    """
    if not (reason or "").strip():
        raise ValueError("a rejection must give a reason")
    if not (rejected_by or "").strip():
        raise ValueError("a rejection must name who rejected the finding")
    if finding.status in DECIDED:
        raise ValueError(
            f"finding {finding.finding_id} is already {finding.status.value}")
    return SupervisorFinding.model_validate({
        **finding.model_dump(),
        "status": FindingStatus.REJECTED,
        "rejection_reason": reason,
        "rejected_by": rejected_by,
    })
