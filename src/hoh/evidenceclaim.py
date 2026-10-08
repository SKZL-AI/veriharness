"""The EvidenceClaim schema and its linkage into the digital thread (V3.3 P2-32).

The plan's sentence this module turns on:

    Evidence must prove a specific claim about a specific subject under specific
    conditions.

An `EvidenceClaim` names its subject, the candidate and spec and policy it was
taken under, the records that evidence it, and whether it is still fresh. Until
this module the register measured P2-32 against `EvidenceItem`, which is a stand-in:
an item names a check and some receipts, but no subject, no candidate, no digest of
the spec or the policy, and no freshness.

Three rules carry the weight, and each was a reviewer's finding before it was code:

- An evidence reference has a closed grammar, `<kind>:<target>`, with four kinds
  (ecspec-a F2). A path, a citation or a reference in any other spelling is refused
  whatever the claim's status, so nothing outside the grammar can be read as
  evidence later.
- A VERIFIED claim needs at least one reference that records an *execution*:
  `receipt:` or `run:`. `file:` and `test:` record that something exists, and the
  plan says "file existence alone never implies VERIFIED" -- never, so this holds
  for `IMPLEMENTATION_EXISTS` as well (ecspec-a F3, Q11).
- `claim_from_item` refuses an item that the iteration it is bound to did not
  re-observe (ecspec-a F1). `carry_forward` keeps such items with their old
  receipts, and binding one to a new candidate would make old evidence FRESH on a
  tree it was never taken on.

`link_into` joins claims to the thread as CHECKS_RECEIPTS nodes, one
`evidenced_by` edge from the candidate each claim names. Like `hoh.thread`, the
thread stays a projection: nothing here stores a status that a record does not
already hold.

The module is pure, like `hoh.thread`: no filesystem, no clock, no git, and
nothing handed in is mutated.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .evidence import EvidenceItem, EvidenceStatus
from .thread import (
    AmbiguousIdentifier,
    DigitalThread,
    ThreadEdge,
    ThreadNode,
    ThreadStage,
    UnknownIdentifier,
)

__all__ = [
    "EXECUTION_KINDS",
    "PLAN_FIELDS",
    "PLAN_REQUIRED",
    "REF_RE",
    "ClaimType",
    "EvidenceClaim",
    "Freshness",
    "claim_from_item",
    "invalidated",
    "link_into",
    "thread_node",
]

# The plan's contract, carried as data. Both come from the pinned plan package
# (internal, not exported):
#   specs/full_stack_contracts_r1/04_EVIDENCE_CLAIM_AND_INVALIDATION.md
#     sha256 fc580ba66b6d93c1106d553ab9a298bf8d2a943284ddafa530264270d343cfd0
#     -- the 20 fields below, in its order, and the seven example claim classes
#   specs/full_stack_contracts_r1/schemas/evidence-claim.schema.json
#     sha256 1e566077daa9b78042de5099754e8fe240a5dd74d28e75f15c8a9a419ed6e4b9
#     -- the 9 required fields below
PLAN_FIELDS: tuple[str, ...] = (
    "claim_id",
    "claim_type",
    "subject_type",
    "subject_id",
    "candidate_sha",
    "base_sha",
    "requirement_ref",
    "criterion_ref",
    "evidence_refs",
    "read_set",
    "dependency_set",
    "environment_digest",
    "check_version",
    "spec_digest",
    "policy_digest",
    "status",
    "freshness",
    "created_at",
    "invalidated_at",
    "invalidation_reason",
)

PLAN_REQUIRED: tuple[str, ...] = (
    "claim_id",
    "claim_type",
    "subject_type",
    "subject_id",
    "evidence_refs",
    "spec_digest",
    "policy_digest",
    "status",
    "freshness",
)

#: The closed grammar of an evidence reference. The kind is lower case and one of
#: four, the match is case-sensitive, and the target is a non-empty run of ASCII
#: token characters -- so whitespace, and an invisible character such as U+200B, is
#: refused rather than carried into a reference that looks well formed. The kind
#: names are the subset of `tools/check_claims.py`'s vocabulary that names a single
#: record; the syntax is this module's own (Revision 2a, ecspec-b r2 N1/N3).
REF_RE = re.compile(r"^(?P<kind>receipt|run|file|test):(?P<target>[A-Za-z0-9._/:@+-]+)$")

#: The kinds that record that something was *run*. `file` and `test` record that
#: something exists, not that it was run and passed.
EXECUTION_KINDS = frozenset({"receipt", "run"})


class ClaimType(StrEnum):
    """Exactly the plan's seven example claim classes, and no other."""

    IMPLEMENTATION_EXISTS = "IMPLEMENTATION_EXISTS"
    CRITERION_PASSES = "CRITERION_PASSES"
    NEGATIVE_CONTROL_DISCRIMINATES = "NEGATIVE_CONTROL_DISCRIMINATES"
    PRESERVATION_PASSES = "PRESERVATION_PASSES"
    INTEGRATION_PASSES = "INTEGRATION_PASSES"
    RELEASE_ARTIFACT_MATCHES_ACCEPTED_TREE = "RELEASE_ARTIFACT_MATCHES_ACCEPTED_TREE"
    AUTONOMY_EVENT_ATTRIBUTION = "AUTONOMY_EVENT_ATTRIBUTION"


class Freshness(StrEnum):
    FRESH = "FRESH"
    """Taken on the subject as it is."""

    STALE = "STALE"
    """Something the claim depends on may have moved; revalidate before relying."""

    INVALIDATED = "INVALIDATED"
    """Withdrawn, with a time and a reason."""


class _Frozen(BaseModel):
    """The project's `Strict` rules (no unknown fields, whitespace stripped), plus
    frozen. `hoh.contracts.Strict` is deliberately mutable and `hoh.thread._Frozen`
    is that module's own, so this module carries its own config too."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class EvidenceClaim(_Frozen):
    """One claim about one subject, and the records that evidence it.

    Fields are declared in the plan's order (`PLAN_FIELDS`). Required are the
    plan's nine (`PLAN_REQUIRED`) and `created_at`, because a claim whose time is
    unknown cannot be ordered against the change that invalidates it. Every
    optional string is absent or non-empty: an empty `candidate_sha` would read as
    bound to a candidate and name none.

    `holds()` is a method, not a field, so it cannot be handed in.
    """

    claim_id: str = Field(min_length=1)
    claim_type: ClaimType
    subject_type: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    candidate_sha: str | None = Field(default=None, min_length=1)
    base_sha: str | None = Field(default=None, min_length=1)
    requirement_ref: str | None = Field(default=None, min_length=1)
    criterion_ref: str | None = Field(default=None, min_length=1)
    evidence_refs: tuple[str, ...]
    read_set: tuple[str, ...] = ()
    dependency_set: tuple[str, ...] = ()
    environment_digest: str | None = Field(default=None, min_length=1)
    check_version: str | None = Field(default=None, min_length=1)
    spec_digest: str = Field(min_length=1)
    policy_digest: str = Field(min_length=1)
    status: EvidenceStatus
    freshness: Freshness
    created_at: str = Field(min_length=1)
    invalidated_at: str | None = Field(default=None, min_length=1)
    invalidation_reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _the_claim_is_coherent(self) -> EvidenceClaim:
        # 1. The grammar, whatever the status: a GAP that cites `src/hoh/a.py` is
        #    refused too, so nothing outside the grammar can be promoted later.
        for ref in self.evidence_refs:
            if not REF_RE.fullmatch(ref):
                raise ValueError(
                    f"evidence reference {ref!r} is not of the form <kind>:<target> "
                    f"with kind one of receipt, run, file, test and a non-empty "
                    f"target of [A-Za-z0-9._/:@+-]")
        # 2. An invalidation has a time and a reason, or neither.
        if (self.invalidated_at is None) != (self.invalidation_reason is None):
            raise ValueError(
                "invalidated_at and invalidation_reason are set together or not at "
                f"all (invalidated_at={self.invalidated_at!r}, "
                f"invalidation_reason={self.invalidation_reason!r})")
        # 3. INVALIDATED exactly when an invalidation is recorded.
        if (self.freshness is Freshness.INVALIDATED) != (self.invalidated_at is not None):
            raise ValueError(
                f"freshness {self.freshness} does not match invalidated_at "
                f"{self.invalidated_at!r}: a claim is INVALIDATED exactly when its "
                "invalidation is recorded")
        if self.status is EvidenceStatus.VERIFIED:
            # 4. VERIFIED rests on something.
            if not self.evidence_refs:
                raise ValueError(
                    f"claim {self.claim_id!r} is VERIFIED and names no evidence")
            # 5. ... and on an execution, for every class (the plan says "never").
            kinds = {REF_RE.fullmatch(r).group("kind") for r in self.evidence_refs}
            if not kinds & EXECUTION_KINDS:
                raise ValueError(
                    f"claim {self.claim_id!r} is VERIFIED on {sorted(kinds)} only: "
                    "file existence alone never implies VERIFIED, so at least one "
                    "receipt: or run: reference is required")
        return self

    def holds(self) -> bool:
        """VERIFIED and FRESH; anything else is not something to rely on."""
        return self.status is EvidenceStatus.VERIFIED and self.freshness is Freshness.FRESH

    def digest(self) -> str:
        """sha256 over the canonical JSON of every field."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def invalidated(claim: EvidenceClaim, *, at: str, reason: str) -> EvidenceClaim:
    """A new claim: `claim`, INVALIDATED at `at` for `reason`. `claim` is unchanged.

    Built through `EvidenceClaim.model_validate`, never `model_copy(update=...)`,
    which skips validation -- the same way O214 found `Trace.complete` forgeable --
    so an `at` or `reason` that is `None` or empty is refused here like anywhere
    else. A claim that is already invalidated is refused: a second invalidation
    would overwrite the first reason, and the record of why is the point.
    """
    if claim.invalidated_at is not None or claim.freshness is Freshness.INVALIDATED:
        raise ValueError(
            f"claim {claim.claim_id!r} is already invalidated at "
            f"{claim.invalidated_at!r} ({claim.invalidation_reason!r}); its reason "
            "is not overwritten")
    data = claim.model_dump()
    data.update(freshness=Freshness.INVALIDATED, invalidated_at=at,
                invalidation_reason=reason)
    return EvidenceClaim.model_validate(data)


def claim_from_item(item: EvidenceItem, *, iteration: int, subject_type: str,
                    subject_id: str, candidate_sha: str, spec_digest: str,
                    policy_digest: str, created_at: str,
                    requirement_ref: str | None = None) -> EvidenceClaim:
    """Project one evidence item of `iteration` into a CRITERION_PASSES claim.

    `iteration` is the iteration whose candidate is `candidate_sha`. The caller
    asserts that pairing; this function cannot see it. Checking it against the
    receipts' own candidate binding is P4-16 and is not done here (Q12).

    Refused, with `ValueError`:
    - an item that names no check -- there is no criterion to claim about;
    - an item whose `last_seen_iteration` is not `iteration` (ecspec-a F1).
      `carry_forward` keeps items that were not re-observed, with their old
      receipts. Binding one to this candidate would make old evidence FRESH on a
      tree it was never taken on, and marking it STALE instead would still name a
      candidate its evidence was never taken on. `first_seen_iteration` does not
      matter: an item first seen earlier and re-observed now is current.

    Otherwise the claim is `<item_id>@<candidate_sha>`, its criterion is the
    item's check, its references are the item's receipts, and it is FRESH. A
    VERIFIED item without receipts becomes INSUFFICIENT: nothing evidences it.
    """
    if not item.check_id:
        raise ValueError(
            f"evidence item {item.item_id!r} names no check, so it is not a claim "
            "about any criterion")
    if item.last_seen_iteration != iteration:
        raise ValueError(
            f"evidence item {item.item_id!r} was last observed in iteration "
            f"{item.last_seen_iteration}, not in iteration {iteration}, so its "
            f"evidence was not taken on candidate {candidate_sha!r}")
    refs = tuple(f"receipt:{r}" for r in item.receipt_ids)
    status = item.status
    if status is EvidenceStatus.VERIFIED and not refs:
        status = EvidenceStatus.INSUFFICIENT
    return EvidenceClaim.model_validate({
        "claim_id": f"{item.item_id}@{candidate_sha}",
        "claim_type": ClaimType.CRITERION_PASSES,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "candidate_sha": candidate_sha,
        "requirement_ref": requirement_ref,
        "criterion_ref": item.check_id,
        "evidence_refs": refs,
        "spec_digest": spec_digest,
        "policy_digest": policy_digest,
        "status": status,
        "freshness": Freshness.FRESH,
        "created_at": created_at,
    })


def thread_node(claim: EvidenceClaim, *, record_set: str) -> ThreadNode:
    """The claim as a CHECKS_RECEIPTS node; its authority lives in `record_set`."""
    return ThreadNode(
        node_id=f"evidence-claim:{claim.claim_id}",
        stage=ThreadStage.CHECKS_RECEIPTS,
        record_ref=f"{record_set}#{claim.claim_id}",
        digest=claim.digest(),
        label=str(claim.claim_type),
    )


def _candidate(thread: DigitalThread, sha: str) -> ThreadNode:
    hits = [n for n in thread.nodes
            if n.stage is ThreadStage.CANDIDATE and n.record_ref == sha]
    if not hits:
        raise UnknownIdentifier(
            f"thread {thread.thread_id!r} has no CANDIDATE node for {sha!r}")
    if len(hits) > 1:
        raise AmbiguousIdentifier(
            f"thread {thread.thread_id!r} has {len(hits)} CANDIDATE nodes for "
            f"{sha!r}: {sorted(n.node_id for n in hits)}")
    return hits[0]


def link_into(thread: DigitalThread, claims: Iterable[EvidenceClaim], *,
              record_set: str) -> DigitalThread:
    """A new thread, one generation on, with `claims` as CHECKS_RECEIPTS nodes.

    A claim that names a candidate gets one `evidenced_by` edge from the one
    CANDIDATE node whose `record_ref` is that sha, with digest
    `sha256("<candidate digest>-><claim node digest>")`. No such node raises
    `UnknownIdentifier` and more than one raises `AmbiguousIdentifier` -- a claim
    is never attached to a candidate by guess. A claim without a candidate is
    added unlinked, so it never traces complete. `record_set` joins
    `projected_from` unless it is already there. `thread` is unchanged.
    """
    nodes = list(thread.nodes)
    edges = list(thread.edges)
    for claim in list(claims):
        node = thread_node(claim, record_set=record_set)
        nodes.append(node)
        if claim.candidate_sha is None:
            continue
        cand = _candidate(thread, claim.candidate_sha)
        edges.append(ThreadEdge(
            from_id=cand.node_id,
            to_id=node.node_id,
            relation="evidenced_by",
            digest=hashlib.sha256(
                f"{cand.digest}->{node.digest}".encode("utf-8")).hexdigest(),
        ))
    projected = tuple(thread.projected_from)
    if record_set not in projected:
        projected += (record_set,)
    return DigitalThread(
        thread_id=thread.thread_id,
        generation=thread.generation + 1,
        nodes=tuple(nodes),
        edges=tuple(edges),
        projected_from=projected,
    )
