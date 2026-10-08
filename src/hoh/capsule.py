"""Context capsule: a digest-bound, role-scoped brief assembled at spawn time
(V3.3 P2-20, with P2-13).

Two sentences from the plan decide the design:

    Context Capsule is a projection. It cannot replace Goalbook, Decision Store,
    Program State, Evidence Store.

    Before dispatch: if any bound source generation/digest changed, rebuild the
    capsule. Do not reuse stale briefs.

So a capsule is *derived*: every item points back at the exact source it was
built from (`SourceRef`: id, kind, digest, generation), the capsule is frozen,
and its `capsule_digest` is computed over the policy version, the role, the
programme generation and the ordered ``(source_id, digest)`` pairs -- never over
the prose. Compression is allowed; losing the reference to the exact source is
not. `stale_sources` and `may_dispatch` refuse a capsule once anything it was
built from has moved, and a source that has *disappeared* counts as moved.

The layers are the plan's three:

    Layer 0  COMPACT    compact low-resolution context -- never dropped for budget
    Layer 1  RELEVANT   relevant decisions / evidence
    Layer 2  ON_DEMAND  full source available on demand

Budget. `token_budget` is a budget over the declared per-item `cost`, not a
count from a real tokenizer. Whoever builds a `CapsuleItem` declares what it
costs; this module only adds those numbers up.

Purity. The module reads nothing: no filesystem, no git, no network, no
subprocess. Sources arrive as records handed in by the caller -- that is what
keeps the capsule a projection rather than a second store, and what makes the
freshness rule testable. The only ambient input is the wall clock for
`assembled_at`, and `assemble` accepts an explicit `assembled_at` to remove it.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import ConfigDict, Field, field_validator, model_validator

from hoh.contracts import Strict

__all__ = [
    "ASSEMBLY_DIGEST_SCHEME",
    "CODE_KINDS",
    "CapsuleBudgetError",
    "CapsuleItem",
    "ContextCapsule",
    "DroppedItem",
    "DropReason",
    "Layer",
    "Role",
    "SourceRef",
    "assemble",
    "compute_capsule_digest",
    "may_dispatch",
    "stale_sources",
    "view_for",
]


class Layer(StrEnum):
    """The plan's layers 0, 1 and 2, in that order."""

    COMPACT = "COMPACT"
    RELEVANT = "RELEVANT"
    ON_DEMAND = "ON_DEMAND"


class Role(StrEnum):
    PLANNER = "PLANNER"
    DEVELOPER = "DEVELOPER"
    QA = "QA"
    ARCHIVIST = "ARCHIVIST"


class DropReason(StrEnum):
    #: The role's view does not include the item at all (see `view_for`).
    NOT_IN_ROLE_VIEW = "not_in_role_view"
    #: Visible to the role but not required for it, and the budget ran out.
    OVER_BUDGET_OPTIONAL = "over_budget_optional"
    #: Required for the role, and the budget still ran out.
    OVER_BUDGET_REQUIRED = "over_budget_required"


#: Source kinds that are the codebase itself. The archivist gets events and
#: source refs, not the codebase, so these never enter an ARCHIVIST view.
CODE_KINDS = frozenset({"code", "source_code", "source_file", "codebase", "diff", "patch"})

#: Identifies how `capsule_digest` is computed, so a change to the recipe is
#: itself visible in the digest.
ASSEMBLY_DIGEST_SCHEME = "hoh.capsule/digest-v1"


class _Frozen(Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _require_text(value: str, name: str) -> str:
    if not value:
        raise ValueError(f"{name} must not be blank")
    return value


class SourceRef(_Frozen):
    """The exact source an item was built from. Without a `source_id` and a
    `digest` an item cannot be traced back, which is the summary-without-
    provenance this component exists to replace -- so both are refused blank."""

    source_id: str
    kind: str
    digest: str
    generation: int = Field(ge=0)
    exact_text: str | None = None

    @field_validator("source_id", "digest", "kind")
    @classmethod
    def _not_blank(cls, value: str, info) -> str:
        return _require_text(value, info.field_name)


class CapsuleItem(_Frozen):
    """One entry of a brief. `cost` is the declared budget cost of the item
    (not a tokenizer count). An empty `gist` is refused: an item nobody can
    read is budget spent on nothing."""

    item_id: str
    layer: Layer
    title: str
    gist: str
    source: SourceRef
    cost: int = Field(ge=0)
    required_for_roles: frozenset[Role] = frozenset()

    @field_validator("item_id", "gist")
    @classmethod
    def _not_blank(cls, value: str, info) -> str:
        return _require_text(value, info.field_name)


class DroppedItem(_Frozen):
    """An omission, named. A capsule that quietly lost half its context is
    "too little context" arrived at by accident."""

    item_id: str
    layer: Layer
    source_id: str
    reason: DropReason


def compute_capsule_digest(*, policy_version: str, role: Role, generation: int,
                           sources: Iterable[tuple[str, str]]) -> str:
    """sha256 over the policy version, the role, the programme generation and
    the (source_id, digest) pairs sorted by source_id -- never over prose."""
    payload = {
        "scheme": ASSEMBLY_DIGEST_SCHEME,
        "policy_version": policy_version,
        "role": Role(role).value,
        "generation": generation,
        "sources": sorted([list(pair) for pair in sources]),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _bound_sources(items: Iterable[CapsuleItem]) -> dict[str, SourceRef]:
    """The distinct sources of `items`, keyed by id. Two items naming the same
    source id with a different digest or generation are refused: a capsule
    cannot be bound to two versions of one source at once."""
    bound: dict[str, SourceRef] = {}
    for item in items:
        ref = item.source
        seen = bound.get(ref.source_id)
        if seen is None:
            bound[ref.source_id] = ref
        elif (seen.digest, seen.generation) != (ref.digest, ref.generation):
            raise ValueError(
                f"source {ref.source_id!r} is bound at two versions: "
                f"({seen.digest}, g{seen.generation}) and ({ref.digest}, g{ref.generation})")
    return bound


def _duplicates(ids: Iterable[str]) -> list[str]:
    return sorted(i for i, n in Counter(ids).items() if n > 1)


class ContextCapsule(_Frozen):
    """The assembled brief. Frozen: it is a projection of the Goalbook, the
    Decision Store, Program State and the Evidence Store, and a projection that
    could be edited in place would become a second source of truth.

    The bound sources (`source_ids`, `source_digests`, `source_generations`,
    aligned and sorted by id) are those of the kept `items`. The validator
    re-derives them and `capsule_digest` from the items, so a capsule whose
    binding does not match its content cannot be constructed."""

    capsule_id: str
    program_id: str
    program_generation: int = Field(ge=0)
    role: Role
    items: tuple[CapsuleItem, ...]
    token_budget: int = Field(ge=0)
    spent: int = Field(ge=0)
    dropped: tuple[DroppedItem, ...] = ()
    source_ids: tuple[str, ...]
    source_digests: tuple[str, ...]
    source_generations: tuple[int, ...]
    assembly_policy_version: str
    capsule_digest: str
    assembled_at: datetime

    @field_validator("capsule_id", "program_id", "assembly_policy_version")
    @classmethod
    def _not_blank(cls, value: str, info) -> str:
        return _require_text(value, info.field_name)

    @model_validator(mode="after")
    def _consistent(self) -> ContextCapsule:
        dup = _duplicates([i.item_id for i in self.items] + [d.item_id for d in self.dropped])
        if dup:
            raise ValueError(f"duplicate item_id(s): {dup}")
        if not any(i.layer is Layer.COMPACT for i in self.items):
            raise ValueError("a capsule without its COMPACT layer is a capsule about nothing")
        spent = sum(i.cost for i in self.items)
        if self.spent != spent:
            raise ValueError(f"spent={self.spent} but kept items cost {spent}")
        if self.spent > self.token_budget:
            raise ValueError(f"spent={self.spent} exceeds token_budget={self.token_budget}")
        bound = _bound_sources(self.items)
        ids = tuple(sorted(bound))
        if (self.source_ids, self.source_digests, self.source_generations) != (
                ids, tuple(bound[s].digest for s in ids),
                tuple(bound[s].generation for s in ids)):
            raise ValueError("source_ids/source_digests/source_generations do not match the items")
        expected = compute_capsule_digest(
            policy_version=self.assembly_policy_version, role=self.role,
            generation=self.program_generation,
            sources=zip(self.source_ids, self.source_digests))
        if self.capsule_digest != expected:
            raise ValueError("capsule_digest does not match the bound sources")
        return self

    def bound(self) -> dict[str, tuple[str, int]]:
        """`{source_id: (digest, generation)}` of the bound sources (a copy)."""
        return {s: (d, g) for s, d, g in
                zip(self.source_ids, self.source_digests, self.source_generations)}


# --------------------------------------------------------------------------- #
# role views
# --------------------------------------------------------------------------- #


def _visible(role: Role, item: CapsuleItem) -> bool:
    if role is Role.ARCHIVIST and item.source.kind.lower() in CODE_KINDS:
        return False                     # events and source refs, not the codebase
    if item.layer is Layer.COMPACT or role in item.required_for_roles:
        return True
    if role is Role.PLANNER:
        return True                      # the planner plans over the whole chain
    if role is Role.DEVELOPER:
        return item.layer is Layer.ON_DEMAND   # full source on demand
    return False                         # QA and ARCHIVIST: only what they need


def view_for(role: Role, items: Iterable[CapsuleItem]) -> list[CapsuleItem]:
    """The items `role` may see, in input order.

    - COMPACT items are visible to every role (except code-kind sources for the
      ARCHIVIST, below), and so is every item that names the role in
      `required_for_roles`.
    - PLANNER additionally sees every other item: it plans over the chain.
    - DEVELOPER additionally sees ON_DEMAND items: full source on demand.
    - QA sees nothing more: the criterion, the candidate, the evidence and the
      minimal decision context needed to judge -- which is what is marked
      required for QA.
    - ARCHIVIST sees nothing more either, and never an item whose source kind is
      in `CODE_KINDS`: it gets events and source refs, not the codebase.
    """
    role = Role(role)
    return [i for i in items if _visible(role, i)]


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #


class CapsuleBudgetError(ValueError):
    """The COMPACT layer alone does not fit the budget. Raised rather than
    returning a capsule that omits layer 0."""


def _drop_priority(role: Role, item: CapsuleItem) -> int:
    """Lower drops first: optional before required (rule 2), and within that
    ON_DEMAND before RELEVANT (rule 3). COMPACT is never dropped for budget."""
    required = role in item.required_for_roles
    on_demand = item.layer is Layer.ON_DEMAND
    return (2 if required else 0) + (0 if on_demand else 1)


def assemble(items: Iterable[CapsuleItem], *, program_id: str, generation: int,
             role: Role, token_budget: int, policy_version: str,
             assembled_at: datetime | None = None) -> ContextCapsule:
    """Assemble a capsule for `role` within `token_budget` (declared cost).

    1. COMPACT items are never dropped for budget; if they alone exceed the
       budget, `CapsuleBudgetError` is raised. A capsule with no COMPACT item
       in the role's view is refused as well.
    2. Items outside the role's view are dropped first (`not_in_role_view`);
       then, while over budget, items visible but not required for the role
       are dropped before items that are required.
    3. Within each of those tiers ON_DEMAND is dropped before RELEVANT, and
       within one tier and layer the later item in input order goes first.
    4. Every dropped item is recorded in `dropped` with its reason.
    5. `spent` is the summed cost of what was kept, and `spent <= token_budget`.

    Kept items keep their input order.
    """
    items = list(items)
    role = Role(role)
    if token_budget < 0:
        raise CapsuleBudgetError(f"token_budget must be >= 0, got {token_budget}")
    dup = _duplicates(i.item_id for i in items)
    if dup:
        raise ValueError(f"duplicate item_id(s): {dup}")

    view = view_for(role, items)
    visible_ids = {i.item_id for i in view}
    dropped: list[DroppedItem] = [
        DroppedItem(item_id=i.item_id, layer=i.layer, source_id=i.source.source_id,
                    reason=DropReason.NOT_IN_ROLE_VIEW)
        for i in items if i.item_id not in visible_ids]

    compact = [i for i in view if i.layer is Layer.COMPACT]
    if not compact:
        raise ValueError(f"no COMPACT item in the {role.value} view: a capsule "
                         "without layer 0 is a capsule about nothing")
    compact_cost = sum(i.cost for i in compact)
    if compact_cost > token_budget:
        raise CapsuleBudgetError(
            f"the COMPACT layer alone costs {compact_cost}, over token_budget={token_budget}")

    spent = sum(i.cost for i in view)
    order = {i.item_id: n for n, i in enumerate(view)}
    candidates = sorted((i for i in view if i.layer is not Layer.COMPACT),
                        key=lambda i: (_drop_priority(role, i), -order[i.item_id]))
    over_budget: set[str] = set()
    for item in candidates:
        if spent <= token_budget:
            break
        spent -= item.cost
        over_budget.add(item.item_id)
        dropped.append(DroppedItem(
            item_id=item.item_id, layer=item.layer, source_id=item.source.source_id,
            reason=(DropReason.OVER_BUDGET_REQUIRED if role in item.required_for_roles
                    else DropReason.OVER_BUDGET_OPTIONAL)))

    kept = tuple(i for i in view if i.item_id not in over_budget)
    bound = _bound_sources(kept)
    ids = tuple(sorted(bound))
    digests = tuple(bound[s].digest for s in ids)
    capsule_digest = compute_capsule_digest(
        policy_version=policy_version, role=role, generation=generation,
        sources=zip(ids, digests))
    when = assembled_at or datetime.now(timezone.utc)
    capsule_id = "cap-" + hashlib.sha256(
        f"{program_id}|{capsule_digest}|{when.isoformat()}".encode("utf-8")).hexdigest()[:16]
    return ContextCapsule(
        capsule_id=capsule_id, program_id=program_id, program_generation=generation,
        role=role, items=kept, token_budget=token_budget, spent=spent,
        dropped=tuple(dropped), source_ids=ids, source_digests=digests,
        source_generations=tuple(bound[s].generation for s in ids),
        assembly_policy_version=policy_version, capsule_digest=capsule_digest,
        assembled_at=when)


# --------------------------------------------------------------------------- #
# freshness
# --------------------------------------------------------------------------- #


def stale_sources(capsule: ContextCapsule, *,
                  current: Mapping[str, tuple[str, int]]) -> list[str]:
    """The capsule's bound source ids that have moved, sorted.

    `current` is `{source_id: (digest, generation)}`. A source is stale when its
    digest or its generation differs -- or when it is missing from `current`:
    a source that has disappeared is not an unchanged source."""
    stale = []
    for source_id, (digest, generation) in capsule.bound().items():
        now = current.get(source_id)
        if now is None or tuple(now) != (digest, generation):
            stale.append(source_id)
    return sorted(stale)


def may_dispatch(capsule: ContextCapsule, *, current: Mapping[str, tuple[str, int]],
                 program_generation: int) -> list[str]:
    """Why `capsule` may not be dispatched, or `[]`. This is a refusal, not a
    warning: the plan says do not reuse stale briefs."""
    reasons = []
    if capsule.program_generation != program_generation:
        reasons.append(f"program generation moved: capsule built at "
                       f"{capsule.program_generation}, programme is at {program_generation}")
    bound = capsule.bound()
    for source_id in stale_sources(capsule, current=current):
        digest, generation = bound[source_id]
        now = current.get(source_id)
        if now is None:
            reasons.append(f"source {source_id!r} is missing from the current sources")
        else:
            reasons.append(f"source {source_id!r} moved: bound ({digest}, g{generation}), "
                           f"current ({now[0]}, g{now[1]})")
    return reasons
