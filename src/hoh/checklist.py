"""ProgramChecklist: what must be proven before a phase or a release closes.

The rule the design turns on (V3.3 P2-26):

    Supervisor can FIND.  Reconciler can VERIFY.

`reconcile()` is the only thing that decides an item is VERIFIED. The status an
item is handed in with is never trusted: an item constructed with
`status=VERIFIED` and reconciled against no evidence comes out
EVIDENCE_PENDING or REOPENED, not VERIFIED. A checkbox in a document is a
rendering of what this module decided, never an input to it.

Everything here fails closed. Missing evidence, stale evidence, a digest the
item does not name, an open confirmed finding and an incomplete waiver each
keep an item from counting as done -- and every status change carries the
reason it happened.

The reconciler is pure: no I/O, no clock, inputs are never mutated, equal
inputs give an equal `Snapshot`. Waivers are an input only; nothing in this
module creates one.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from enum import StrEnum

from pydantic import ConfigDict, Field, field_validator

from hoh.contracts import Strict


class Status(StrEnum):
    PLANNED = "PLANNED"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    EVIDENCE_PENDING = "EVIDENCE_PENDING"
    VERIFIED = "VERIFIED"
    REOPENED = "REOPENED"
    WAIVED = "WAIVED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Criticality(StrEnum):
    P0_RELEASE_BLOCKING = "P0_RELEASE_BLOCKING"
    P1_PHASE_BLOCKING = "P1_PHASE_BLOCKING"
    P2_NONBLOCKING = "P2_NONBLOCKING"


#: Criticalities that keep a phase from closing.
BLOCKING = frozenset({Criticality.P0_RELEASE_BLOCKING, Criticality.P1_PHASE_BLOCKING})


def _require_text(value: str, name: str) -> str:
    if not value:
        raise ValueError(f"{name} must not be blank")
    return value


class ChecklistItem(Strict):
    """One thing that has to be proven. `status` is an output of `reconcile`."""

    item_id: str
    phase: str
    title: str
    requirement_ref: str
    source_digest: str
    criticality: Criticality
    status: Status = Status.PLANNED
    evidence_refs: list[str] = Field(default_factory=list)
    finding_refs: list[str] = Field(default_factory=list)
    last_reconciled_generation: int | None = None
    reopen_reason: str | None = None
    waiver_ref: str | None = None

    @field_validator("item_id", "phase", "source_digest")
    @classmethod
    def _not_blank(cls, value: str, info) -> str:
        return _require_text(value, info.field_name)


class ItemEvidence(Strict):
    """What the programme has measured for one item.

    `present_refs` are the evidence references that exist; `stale_refs` are
    references that exist but no longer hold (their producer re-ran, their
    input moved). `source_digest` is the digest the evidence was produced
    against. `open_findings` are confirmed findings still open against the
    item -- a suspicion that has not been confirmed does not belong here.
    """

    present_refs: list[str] = Field(default_factory=list)
    stale_refs: list[str] = Field(default_factory=list)
    source_digest: str
    open_findings: list[str] = Field(default_factory=list)

    @field_validator("source_digest")
    @classmethod
    def _digest_not_blank(cls, value: str) -> str:
        return _require_text(value, "source_digest")


class Waiver(Strict):
    """A decision, taken outside this module, that an item need not be proven.

    Every field may be absent so that an incomplete waiver can be *handed in*
    and then refused -- a model that rejected it at construction would turn
    the refusal into a crash instead of a verdict. `scope` names the item ids
    and/or phases the waiver covers.
    """

    waiver_id: str
    authority: str | None = None
    actor: str | None = None
    reason: str | None = None
    scope: list[str] = Field(default_factory=list)

    def defects(self, item: ChecklistItem) -> list[str]:
        """Why this waiver does not waive `item`; empty when it does."""
        out = [
            f"waiver {self.waiver_id} names no {name}"
            for name in ("authority", "actor", "reason")
            if not (getattr(self, name) or "").strip()
        ]
        scope = [s.strip() for s in self.scope if s.strip()]
        if not scope:
            out.append(f"waiver {self.waiver_id} names no scope")
        elif (item.criticality is Criticality.P0_RELEASE_BLOCKING
                and item.item_id not in scope):
            # Decided 2026-09-23, after a reviewer asked: a waiver scoped to a
            # whole phase waived a release-blocking item. Releasing without
            # proving something is the one decision that should never be made
            # in passing, so a P0 item must be named by its own id -- a phase
            # is not specific enough to be deliberate.
            out.append(
                f"waiver {self.waiver_id} covers phase {item.phase} but does not name "
                f"{item.item_id}, and a P0_RELEASE_BLOCKING item is waived only by a "
                f"waiver that names it")
        elif item.item_id not in scope and item.phase not in scope:
            out.append(
                f"waiver {self.waiver_id} does not cover item {item.item_id} "
                f"or phase {item.phase} (scope: {', '.join(scope)})"
            )
        return out


class Transition(Strict):
    """A status change `reconcile` made, and why. A change without a reason is refused."""

    model_config = ConfigDict(populate_by_name=True)

    item_id: str
    from_status: Status = Field(alias="from")
    to_status: Status = Field(alias="to")
    why: str

    @field_validator("why")
    @classmethod
    def _why_not_blank(cls, value: str) -> str:
        return _require_text(value, "why")


class Snapshot(Strict):
    """What one reconciliation decided.

    `seal` is set by `reconcile` only: a digest over the canonical JSON of
    everything else in the snapshot. `phase_gate` recomputes it, so a snapshot
    built by hand, or edited after `reconcile` returned it, does not close a
    phase. It catches edits and forgeries by omission, not a caller who
    deliberately recomputes the seal -- `compute_seal` is not a secret.
    """

    items: list[ChecklistItem]
    generation: int
    transitions: list[Transition] = Field(default_factory=list)
    seal: str | None = None

    def compute_seal(self) -> str:
        body = self.model_dump(mode="json", by_alias=True, exclude={"seal"})
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def is_sealed(self) -> bool:
        return self.seal is not None and self.seal == self.compute_seal()


class Blocker(Strict):
    item_id: str
    why: str

    @field_validator("why")
    @classmethod
    def _why_not_blank(cls, value: str) -> str:
        return _require_text(value, "why")


#: Lines of live state a closed phase's verdict carries beside the closure: what the
#: closure stopped holding shut stays visible, but never as a wall of text.
MAX_WATCH_LINES = 8
_COMMIT = re.compile(r"[0-9a-f]{40}")


class Closure(Strict):
    """A phase closed at a commit (amendment A7), as the caller who verified it hands it in.

    This module does not verify a closure -- it has no I/O. `hoh.phases` re-derives one from the
    blobs at `closed_at` and passes the result here; `phase_gate` then reads the phase as CLOSED
    whatever the live snapshot says, and keeps what the snapshot says as the watch.
    """

    phase: str
    closed_at: str
    ref: str
    #: Lines about the closure's own regression watch (stale evidence, the last re-run, an
    #: open reopen request), already worded by the caller.
    watch: list[str] = Field(default_factory=list, max_length=MAX_WATCH_LINES)

    @field_validator("phase", "ref")
    @classmethod
    def _text(cls, value: str, info) -> str:
        return _require_text(value, info.field_name)

    @field_validator("closed_at")
    @classmethod
    def _full_commit(cls, value: str) -> str:
        if not _COMMIT.fullmatch(value):
            raise ValueError("closed_at must be one full lower-case commit id")
        return value


class GateVerdict(Strict):
    phase: str
    closed: bool
    blockers: list[Blocker] = Field(default_factory=list)
    #: True when the verdict was re-derived from evidence, False when it rests
    #: on the snapshot's seal alone. The seal catches an edit, not a forgery, so
    #: a reader has to be able to tell which question was answered -- a
    #: reviewer pointed out that the stronger mode being opt-in meant every
    #: caller who forgot was back to trusting a reseal, with nothing in the
    #: record saying so.
    derived_from_evidence: bool = False
    #: Set only when the phase reads closed because a verified `Closure` was handed in: the
    #: commit it was closed at and the records entry that says so. `blockers` is then empty
    #: and what the live snapshot would have blocked on is in `watch`.
    closed_at: str | None = None
    closure_ref: str | None = None
    watch: list[str] = Field(default_factory=list)

    def reading(self) -> str:
        """The one line a person reads: CLOSED at a commit with its watch, closed live, or open."""
        if self.closed_at is not None:
            watch = "; ".join(self.watch) if self.watch else "nothing to report"
            ref = f" ({self.closure_ref})" if self.closure_ref else ""
            return f"CLOSED at {self.closed_at[:12]}{ref}; watch: {watch}"
        if self.closed:
            return "closed (live gate, no closure record)"
        return f"OPEN: {len(self.blockers)} blocker(s)"


# --------------------------------------------------------------------------- #
# reconcile
# --------------------------------------------------------------------------- #

#: Statuses from which a failing check means *reopened* rather than *pending*:
#: the item was proven once, or was already reopened and has not been proven
#: since. Keeping REOPENED here is what makes reconciling twice against the
#: same evidence a no-op.
_ONCE_PROVEN = frozenset({Status.VERIFIED, Status.REOPENED})


def _refs(values: Iterable[str]) -> list[str]:
    """Evidence references, with the ones that name nothing removed."""
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


def _evidence_verdict(
    item: ChecklistItem, ev: ItemEvidence | None
) -> tuple[Status, str]:
    """The status the evidence alone supports, and the reason for it."""
    proven_before = item.status in _ONCE_PROVEN
    # Blank and whitespace-only references are dropped, here and on the
    # evidence side. A reviewer verified an item by naming the empty string in
    # both places: the "an item that names nothing to prove can never be
    # proven" rule was bypassed by naming nothing in a way that counted, and
    # the empty parentheses in the reason were the only trace.
    required = list(dict.fromkeys(_refs(item.evidence_refs)))

    if ev is None:
        if proven_before:
            return Status.REOPENED, (
                "stale evidence: no evidence is recorded any more for "
                f"{', '.join(required) or 'this item'}"
            )
        return Status.EVIDENCE_PENDING, "missing evidence: nothing is recorded for this item"

    reopen: list[str] = []
    pending: list[str] = []

    if ev.source_digest != item.source_digest:
        if item.status is Status.VERIFIED:
            reopen.append(
                f"source digest changed: the item now names {item.source_digest}, "
                f"the evidence was produced against {ev.source_digest}"
            )
        else:
            # Rule 3: never VERIFIED, and reopened even if it never was.
            reopen.append(
                f"unknown source digest: the evidence was produced against "
                f"{ev.source_digest}, which the item does not name "
                f"(it names {item.source_digest})"
            )

    present_refs, stale_refs = _refs(ev.present_refs), _refs(ev.stale_refs)
    stale = [r for r in required if r in stale_refs]
    missing = [r for r in required if r not in present_refs and r not in stale]
    if stale:
        (reopen if proven_before else pending).append(
            f"stale evidence: {', '.join(stale)}"
        )
    if missing:
        (reopen if proven_before else pending).append(
            f"{'stale' if proven_before else 'missing'} evidence: "
            f"{', '.join(missing)} not present"
        )
    if not required:
        # An item that names nothing to prove can never be proven.
        pending.append("missing evidence: the item names no required evidence reference")

    findings = sorted(set(ev.open_findings))
    if findings:
        text = f"confirmed finding open: {', '.join(findings)}"
        if proven_before:
            reopen.append(text)
        else:
            pending.insert(0, text)

    if reopen:
        return Status.REOPENED, "; ".join(reopen + pending)
    if findings:
        return Status.BLOCKED, "; ".join(pending)
    if pending:
        return Status.EVIDENCE_PENDING, "; ".join(pending)
    return Status.VERIFIED, (
        f"all required evidence present ({', '.join(required)}) against "
        f"{item.source_digest}, no confirmed finding open"
    )


def _as_evidence(value, model):
    return value if isinstance(value, model) else model.model_validate(value)


def reconcile(
    items: Iterable[ChecklistItem],
    *,
    evidence: Mapping[str, ItemEvidence],
    generation: int,
    waivers: Mapping[str, Waiver] | None = None,
) -> Snapshot:
    """Decide every item's status from evidence. Pure, deterministic, fail-closed.

    `evidence` maps item_id to an `ItemEvidence` (or a dict of its fields); an
    item with no entry has no evidence at all. `waivers` maps waiver_id to a
    `Waiver`; an item is WAIVED only if its `waiver_ref` resolves there to a
    complete waiver that covers it, and only if it is not VERIFIED on its own.

    Duplicate item_ids raise ValueError: two items under one id would both be
    reconciled against the one evidence entry that id names, and which of
    them a reader meant could not be told apart afterwards.
    """
    waivers = waivers or {}
    copies = [_as_evidence(original, ChecklistItem).model_copy(deep=True) for original in items]
    duplicates = sorted(i for i, n in Counter(c.item_id for c in copies).items() if n > 1)
    if duplicates:
        raise ValueError(
            f"duplicate item_id(s) in the checklist: {', '.join(duplicates)}"
        )
    out: list[ChecklistItem] = []
    transitions: list[Transition] = []

    for item in copies:
        before = item.status

        if before is Status.NOT_APPLICABLE:
            item.last_reconciled_generation = generation
            out.append(item)
            continue

        ev = evidence.get(item.item_id)
        ev = None if ev is None else _as_evidence(ev, ItemEvidence)
        status, why = _evidence_verdict(item, ev)

        if status is not Status.VERIFIED and item.waiver_ref is not None:
            waiver = waivers.get(item.waiver_ref)
            if waiver is None:
                defects = [f"waiver {item.waiver_ref} was not handed in"]
            else:
                waiver = _as_evidence(waiver, Waiver)
                defects = waiver.defects(item)
            if not defects:
                status, why = Status.WAIVED, (
                    f"waived by {waiver.waiver_id}: authority {waiver.authority}, "
                    f"actor {waiver.actor}, reason: {waiver.reason}"
                )
            else:
                why = f"{why}; not waived: {'; '.join(defects)}"

        # finding_refs names the confirmed findings open *now*. Without an
        # evidence record nothing is known about them, so they are left as
        # they were; a finding that closed is named in the transition instead
        # of lingering in the field.
        closed_findings: list[str] = []
        if ev is not None:
            open_now = sorted(set(ev.open_findings))
            closed_findings = sorted(set(item.finding_refs) - set(open_now))
            item.finding_refs = open_now
        item.status = status
        item.reopen_reason = why if status is Status.REOPENED else None
        item.last_reconciled_generation = generation
        out.append(item)

        if closed_findings:
            why = f"{why}; finding(s) closed: {', '.join(closed_findings)}"
        # A transition is recorded when the status moved *or* when a finding was
        # cleared. Without the second half, clearing a stale finding against an
        # item that stayed VERIFIED left no trace in the snapshot -- this
        # module's own QA found that, and a reconciliation nobody can see is
        # the thing this record exists to prevent.
        if status is not before or closed_findings:
            transitions.append(
                Transition(item_id=item.item_id, from_status=before,
                           to_status=status, why=why)
            )

    transitions.sort(key=lambda t: t.item_id)
    snapshot = Snapshot(items=out, generation=generation, transitions=transitions)
    snapshot.seal = snapshot.compute_seal()
    return snapshot


# --------------------------------------------------------------------------- #
# phase gate
# --------------------------------------------------------------------------- #


#: Blocker ids for verdicts that concern no single item.
SNAPSHOT_BLOCKER = "(snapshot)"
PHASE_BLOCKER = "(phase)"
CLOSURE_BLOCKER = "(closure)"


def phase_gate(snapshot: Snapshot, phase: str, *,
               evidence: Mapping[str, object] | None = None,
               waivers: Mapping[str, Waiver] | None = None,
               closure: Closure | None = None) -> GateVerdict:
    """A phase closes only when each of its blocking items is VERIFIED or WAIVED.

    With a `closure` (a verified closure of this phase, amendment A7) the phase reads CLOSED at
    the commit it names whatever the snapshot says: the live blockers become `watch` lines and
    the verdict carries `closed_at` and `closure_ref`. A closure of another phase is not
    applied -- it is a blocker of its own, because a caller that hands in the wrong closure has
    not verified the one it needs. This function verifies nothing about the closure itself.

    Fail-closed on two things besides the items themselves. The snapshot must
    carry an intact seal from `reconcile`: a hand-built snapshot, or one whose
    items were edited afterwards, does not close anything -- that is what makes
    VERIFIED and WAIVED trustworthy here, since only `reconcile` sets them and
    only against evidence or a complete waiver. And the phase must contain at
    least one blocking item: a misspelt phase name has nothing to check and
    must not close by default. P2_NONBLOCKING items and other phases are
    ignored.
    """
    # The seal catches an edit, not a forgery: `compute_seal` is not a secret,
    # so a caller who wants to hand in a VERIFIED item can reseal it. That was
    # this module's own QA finding about itself, and the honest remedy is not a
    # better digest -- it is not needing to trust the snapshot. Given the
    # evidence, the verdict is re-derived here and the handed-in statuses are
    # ignored entirely.
    if evidence is not None:
        # Waivers travel with the evidence. Without them the stricter mode
        # refused a legitimately waived item -- two opposite verdicts on one
        # snapshot, with the safer call path the one that was wrong.
        snapshot = reconcile(snapshot.items, evidence=evidence,
                            generation=snapshot.generation, waivers=waivers)
    blockers: list[Blocker] = []
    if not snapshot.is_sealed():
        blockers.append(Blocker(
            item_id=SNAPSHOT_BLOCKER,
            why=(
                "the snapshot was not produced by reconcile or was altered "
                "afterwards: its seal is "
                + ("missing" if snapshot.seal is None else "not the digest of its content")
            ),
        ))
    of_phase = [item for item in snapshot.items if item.phase == phase]
    in_phase = [item for item in of_phase if item.criticality in BLOCKING]
    # A phase the snapshot does not know at all is a misspelt name and must not
    # close by default. A phase it knows, all of whose items are
    # P2_NONBLOCKING, closes -- "P2_NONBLOCKING items never block" is the
    # spec's own sentence, and blocking on the *absence* of blocking items went
    # further than that. The product's QA flagged the difference as a decision
    # to confirm; this is the confirmation.
    if not of_phase:
        blockers.append(Blocker(
            item_id=PHASE_BLOCKER,
            why=(
                f"phase {phase!r} is not in this snapshot at all, so nothing "
                "proves it may close -- a misspelt phase name must not pass by "
                "having no items to check"
            ),
        ))
    blockers += [
        Blocker(
            item_id=item.item_id,
            why=(
                f"{item.criticality.value} item is {item.status.value}"
                + (f": {item.reopen_reason}" if item.reopen_reason else "")
            ),
        )
        for item in sorted(in_phase, key=lambda i: i.item_id)
        if item.status not in (Status.VERIFIED, Status.WAIVED)
    ]
    if closure is not None and closure.phase != phase:
        blockers.append(Blocker(
            item_id=CLOSURE_BLOCKER,
            why=f"the closure handed in is about phase {closure.phase!r}, not {phase!r}, "
                "so it closes nothing here"))
    elif closure is not None:
        live = [f"live: {b.item_id}: {b.why}" for b in blockers[:MAX_WATCH_LINES]]
        if len(blockers) > MAX_WATCH_LINES:
            live.append(f"live: and {len(blockers) - MAX_WATCH_LINES} more")
        return GateVerdict(phase=phase, closed=True, blockers=[],
                           derived_from_evidence=False,       # the closure closed it, not evidence
                           closed_at=closure.closed_at, closure_ref=closure.ref,
                           watch=[*closure.watch, *live])
    return GateVerdict(phase=phase, closed=not blockers, blockers=blockers,
                       derived_from_evidence=evidence is not None)


# --------------------------------------------------------------------------- #
# fsck (P2-28)
# --------------------------------------------------------------------------- #

def fsck(items: Iterable[ChecklistItem], *, snapshot: Snapshot | None = None,
         requirements: Iterable[str] | None = None,
         known_digests: Iterable[str] | None = None,
         waivers: Mapping[str, Waiver] | None = None,
         release_blocking_findings: Iterable[str] | None = None) -> list[str]:
    """What is structurally wrong with this checklist, or [].

    The reconciler answers "is this item proven". This answers the question
    underneath it: *is the checklist itself sound enough for that answer to
    mean anything*. The plan's nine checks, and each one exists because the
    failure it catches makes every status above it worthless:

    * a release-blocking requirement with **no item** -- the checklist is green
      about a requirement it never mentioned, which is the worst kind of green;
    * **duplicate ids** -- two items, one identity, and whichever is read last
      wins;
    * a **requirement_ref that does not resolve** -- an item proving something
      nobody asked for;
    * an **unknown source digest** -- a status about a revision nobody can name;
    * a **VERIFIED item with no evidence references** -- verified by nothing;
    * a **WAIVED item whose waiver is missing or incomplete** -- excused by
      nobody;
    * a **phase that is blank** -- an item outside every gate;
    * a snapshot **generation** that does not match the items' own, so the gate
      would be deciding about a reconciliation that is not this one;
    * an **unresolved release-blocking finding**, which no phase may close over.

    Returns strings rather than raising: an fsck that stops at the first
    problem makes the second one invisible, and a checklist is usually wrong in
    more than one way at once.
    """
    items = list(items)
    problems: list[str] = []
    seen: dict[str, int] = {}
    for item in items:
        seen[item.item_id] = seen.get(item.item_id, 0) + 1
    problems += [f"duplicate item id {item_id!r}: two items share one identity, and "
                 f"whichever is read last wins" for item_id, n in sorted(seen.items())
                 if n > 1]

    wanted = set(requirements or ())
    if wanted:
        covered = {item.requirement_ref for item in items}
        problems += [f"release-blocking requirement {ref!r} has no checklist item, so "
                     f"nothing about it can be proven or refused"
                     for ref in sorted(wanted - covered)]
        problems += [f"item {item.item_id} names requirement {item.requirement_ref!r}, "
                     f"which the programme does not have" for item in items
                     if item.requirement_ref not in wanted]

    digests = set(known_digests or ())
    if digests:
        problems += [f"item {item.item_id} names source digest "
                     f"{item.source_digest!r}, which is not a revision this programme "
                     f"knows" for item in items if item.source_digest not in digests]

    for item in items:
        if not item.phase.strip():
            problems.append(f"item {item.item_id} has no phase, so no gate covers it")
        if item.status is Status.VERIFIED and not _refs(item.evidence_refs):
            problems.append(f"item {item.item_id} is VERIFIED and names no evidence: "
                            f"verified by nothing is not verified")
        if item.status is Status.WAIVED:
            waiver = (waivers or {}).get(item.waiver_ref or "")
            if waiver is None:
                problems.append(f"item {item.item_id} is WAIVED and its waiver "
                                f"{item.waiver_ref!r} is not among the waivers given: "
                                f"excused by nobody")
            else:
                problems += [f"item {item.item_id} is WAIVED but {d}"
                             for d in waiver.defects(item)]

    if snapshot is not None:
        if not snapshot.is_sealed():
            problems.append("the snapshot carries no intact seal, so a gate deciding on "
                            "it would be deciding about something reconcile did not say")
        stale = sorted({item.item_id for item in snapshot.items
                        if item.last_reconciled_generation != snapshot.generation})
        problems += [f"item {item_id} was last reconciled at a different generation than "
                     f"the snapshot claims ({snapshot.generation}), so the gate would "
                     f"read a status from another reconciliation" for item_id in stale]

    open_findings = sorted(set(release_blocking_findings or ()))
    problems += [f"release-blocking finding {finding} is unresolved, and no phase closes "
                 f"over one" for finding in open_findings]
    return problems
