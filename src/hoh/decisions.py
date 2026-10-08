"""The decision exploration plane (V3.3 P2-19): questions before work packages.

Some programmes start with a clear destination but no honest route. Turning an
unanswered question into a work package invents work nobody can do yet, so the
plan puts an optional plane between the Goalbook and the Program DAG:

    North Star / Goalbook -> Decision Map -> Decision Frontier
      -> Resolution Records -> Graduation Gate -> Spec / Program DAG

The rule the whole plane turns on:

    VeriHarness has two frontiers. The Decision Frontier holds questions that
    can be resolved now. The Execution Frontier holds work packages that can
    run now. Never merge these concepts into one queue.

What this module enforces, and where:

* Structural rules live at construction. A node that says RESOLVED without a
  `ResolutionRecord` is refused -- resolved-by-assertion is the exact failure
  this plane exists to prevent. So is a node that blocks itself, a map with
  duplicate ids, and a `blocked_by` naming a node the map does not contain.
* Fog is known scope that is not yet precise enough to be a question. It has no
  status and no claim fields, `decision_frontier` never returns it, and
  `claim` refuses a fog id by name as a category error.
* An UNDERMINED premise is not an answer: a node blocked by one stays off the
  frontier, and an in-scope undermined node blocks graduation.
* `claim` and `resolve` are pure: they return a new map and never mutate the
  one they were handed. A resolution record is never overwritten in place;
  correction is by a new generation.

Out of scope here: the frontier scheduler, provider calls, real-clock leases
and `decision check` (P2-21). Time is always passed in as `now`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from pydantic import ConfigDict, Field, field_validator, model_validator

from hoh.contracts import Strict


class Checked(Strict):
    """`Strict`, plus immutability, so a rule cannot be undone after the fact.

    Reviewer A's first finding, and the one that mattered most: the project's
    `Strict` base is `frozen=False` with no `validate_assignment`, so a node that
    had been refused at construction could be produced by one assignment
    afterwards --

        m.node("A").status = DecisionStatus.RESOLVED   # no record anywhere

    -- after which the node unblocked its dependents and the graduation gate
    returned `[]`. Every structural rule in this module was a rule about
    *construction*, and the docstrings claimed they were rules about nodes.

    `validate_assignment=True` was the first fix and it was not enough, which one
    of the repair tests caught: pydantic sets the attribute *before* running the
    `mode="after"` model validator, so the assignment raised **and** left the
    object mutated and invalid. A rule that reports a violation after permitting
    it is not an invariant.

    So these models are frozen. Assignment raises and changes nothing, which is
    also what the module's own language always claimed -- "a record is never
    overwritten in place". It costs nothing, because `claim` and `resolve` no
    longer assign: `_with_node` rebuilds the map from plain data and validates the
    whole result, exactly as a map read from disk would be.
    """

    model_config = ConfigDict(extra="forbid", frozen=True,
                              str_strip_whitespace=True,
                              validate_assignment=True)


class DecisionStatus(StrEnum):
    OPEN = "OPEN"
    CLAIMED = "CLAIMED"
    RESOLVED = "RESOLVED"
    UNDERMINED = "UNDERMINED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class ResolutionPath(StrEnum):
    RESEARCH = "RESEARCH"
    PROTOTYPE = "PROTOTYPE"
    HUMAN_DECISION = "HUMAN_DECISION"
    EXTERNAL_PREREQUISITE = "EXTERNAL_PREREQUISITE"


#: Statuses that mean "this question is not answered".
UNRESOLVED = frozenset({DecisionStatus.OPEN, DecisionStatus.CLAIMED,
                        DecisionStatus.UNDERMINED})


class DecisionError(ValueError):
    """A request against a decision map that the plane refuses."""


def _require_text(value: str, name: str) -> str:
    # `Strict` strips whitespace first, so a blank-but-spaced value lands here empty.
    if not value:
        raise ValueError(f"{name} must not be blank")
    return value


def _duplicates(values: Iterable[str]) -> list[str]:
    return sorted(v for v, n in Counter(values).items() if n > 1)


def _cycle(nodes: list[DecisionNode]) -> list[str] | None:
    """One cycle in the blocking graph, or None. Reviewer A F8.

    Depth-first with a colour per node: grey means "on the current path", so an
    edge into a grey node closes a cycle and the path from that node is the cycle
    to report. Iterative rather than recursive, because a long dependency chain
    in a deserialized map should not be able to exhaust the stack.
    """
    blocked = {n.decision_id: list(n.blocked_by) for n in nodes}
    colour: dict[str, int] = {}                      # 0 unseen, 1 grey, 2 done
    for start in blocked:
        if colour.get(start):
            continue
        path: list[str] = []
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            node_id, leaving = stack.pop()
            if leaving:
                colour[node_id] = 2
                path.pop()
                continue
            if colour.get(node_id) == 1:
                return path[path.index(node_id):] + [node_id]
            if colour.get(node_id) == 2:
                continue
            colour[node_id] = 1
            path.append(node_id)
            stack.append((node_id, True))
            for nxt in blocked.get(node_id, ()):
                if colour.get(nxt) != 2:
                    stack.append((nxt, False))
    return None


class ResolutionRecord(Checked):
    """What makes a question answered rather than assumed."""

    decision_id: str
    path: ResolutionPath
    answer: str
    source_refs: list[str] = Field(default_factory=list)
    resolution_digest: str
    resolved_by: str
    resolved_at: datetime

    @field_validator("decision_id", "answer", "resolution_digest", "resolved_by")
    @classmethod
    def _not_blank(cls, value: str, info) -> str:
        return _require_text(value, info.field_name)


class DecisionNode(Checked):
    """One open question on the map. RESOLVED is only ever backed by a record."""

    decision_id: str
    title: str
    question: str
    status: DecisionStatus = DecisionStatus.OPEN
    blocked_by: list[str] = Field(default_factory=list)
    scope_refs: list[str] = Field(default_factory=list)
    resolution: ResolutionRecord | None = None
    claimed_by: str | None = None
    claim_expires_at: datetime | None = None

    @field_validator("decision_id", "title", "question")
    @classmethod
    def _id_not_blank(cls, value: str, info) -> str:
        # Reviewer A F9: a node whose question is blank is not a question. The
        # map's whole purpose is to hold what must become clear.
        return _require_text(value, info.field_name)

    @model_validator(mode="after")
    def _structural_rules(self) -> DecisionNode:
        if self.status is DecisionStatus.RESOLVED and self.resolution is None:
            raise ValueError(
                f"decision {self.decision_id!r} is RESOLVED without a ResolutionRecord: "
                "resolution cannot be asserted, it must be recorded")
        if self.resolution is not None and self.resolution.decision_id != self.decision_id:
            raise ValueError(
                f"decision {self.decision_id!r} carries a ResolutionRecord for "
                f"{self.resolution.decision_id!r}")
        if self.decision_id in self.blocked_by:
            raise ValueError(f"decision {self.decision_id!r} lists itself in blocked_by")
        if self.status is DecisionStatus.CLAIMED and (
                not self.claimed_by or self.claim_expires_at is None):
            raise ValueError(
                f"decision {self.decision_id!r} is CLAIMED without an owner and an expiry")
        # Reviewer A F3, the converse. `_live_claim` only looks at CLAIMED nodes,
        # so claim fields on an OPEN node were invisible: the node appeared on the
        # frontier and a second worker's claim overwrote the first one's lease
        # with no error. A lease that is not visible to the code that guards
        # leases is worse than no lease.
        if self.status is not DecisionStatus.CLAIMED and (
                self.claimed_by or self.claim_expires_at is not None):
            raise ValueError(
                f"decision {self.decision_id!r} is {self.status} but carries claim "
                "fields; only a CLAIMED node holds a lease")
        # Reviewer A F7: a naive datetime here crashes the frontier when it meets
        # an aware `now`, and a map deserialized from JSON without offsets was
        # legal. An instant without a zone is not an instant.
        if self.claim_expires_at is not None and self.claim_expires_at.tzinfo is None:
            raise ValueError(
                f"decision {self.decision_id!r} has a claim expiry without a timezone")
        return self


class FogPatch(Checked):
    """Known to be in scope, but not precise enough to formulate as a decision node.

    Fog is not a backlog, not a task and not hidden execution work. It has no
    status and no claim: it cannot be leased or run.
    """

    fog_id: str
    title: str
    note: str
    clears_with: list[str] = Field(default_factory=list)
    out_of_scope: bool = False
    #: Why this fog does not block graduation although it is in scope. The source
    #: spec's section 7 allows three affirmative justifications -- out of scope,
    #: deferred by policy, or not required for the current epoch -- and the
    #: derived spec reduced them to `out_of_scope` plus `clears_with`. Reviewer A
    #: F4(a): the vaguest fog there is, in scope with nothing identified that
    #: would make it formulable, then graduated the map, because an empty
    #: `clears_with` produced no blocker. Absence of a reason is not a reason, so
    #: the justification is a field that has to be written.
    deferred_reason: str = ""

    @field_validator("fog_id")
    @classmethod
    def _id_not_blank(cls, value: str) -> str:
        return _require_text(value, "fog_id")


class DecisionMap(Checked):
    """The questions that must become clear before an effort can graduate."""

    map_id: str
    destination: str
    generation: int = 1
    nodes: list[DecisionNode] = Field(default_factory=list)
    fog: list[FogPatch] = Field(default_factory=list)
    program_ref: str | None = None

    @field_validator("map_id", "destination")
    @classmethod
    def _not_blank(cls, value: str, info) -> str:
        # Reviewer A F9: the Destination is the concise local target -- "what must
        # become clear before this effort can graduate into execution?" -- and a
        # map without one is not a Decision Map.
        return _require_text(value, info.field_name)

    @model_validator(mode="after")
    def _structural_rules(self) -> DecisionMap:
        node_ids = [n.decision_id for n in self.nodes]
        fog_ids = [f.fog_id for f in self.fog]
        if dup := _duplicates(node_ids):
            raise ValueError(f"map {self.map_id!r} has duplicate decision ids: {dup}")
        if dup := _duplicates(fog_ids):
            raise ValueError(f"map {self.map_id!r} has duplicate fog ids: {dup}")
        if clash := sorted(set(node_ids) & set(fog_ids)):
            raise ValueError(
                f"map {self.map_id!r} uses the same id for a decision and a fog patch: {clash}")
        known = set(node_ids)
        for node in self.nodes:
            if dangling := [b for b in node.blocked_by if b not in known]:
                raise ValueError(
                    f"decision {node.decision_id!r} is blocked_by {dangling}, "
                    f"which map {self.map_id!r} does not contain")
        # Reviewer A F6: `clears_with` had no integrity check, so a typo made a
        # fog patch wait forever on an id the map does not contain -- and the
        # gate reported it as a blocker with no way to clear it. Fail-closed,
        # but permanently, which is its own kind of wrong.
        for patch in self.fog:
            if dangling := [c for c in patch.clears_with if c not in known]:
                raise ValueError(
                    f"fog {patch.fog_id!r} clears_with {dangling}, which map "
                    f"{self.map_id!r} does not contain")
        # Reviewer A F8: a cycle in blocked_by is permanently unresolvable -- no
        # node in it can ever reach the frontier, so the map can never graduate
        # and nothing says why.
        if cycle := _cycle(self.nodes):
            raise ValueError(
                f"map {self.map_id!r} has a cycle in blocked_by: {cycle} -- no "
                "question in it can ever reach the frontier")
        return self

    def node(self, decision_id: str) -> DecisionNode | None:
        return next((n for n in self.nodes if n.decision_id == decision_id), None)

    def fog_patch(self, fog_id: str) -> FogPatch | None:
        return next((f for f in self.fog if f.fog_id == fog_id), None)


# --------------------------------------------------------------------------- #
# The Decision Frontier
# --------------------------------------------------------------------------- #


def _with_node(dmap: DecisionMap, decision_id: str, *, generation: int | None = None,
               **fields) -> DecisionMap:
    """A new map with one node's fields replaced, built and validated in one step.

    Written this way because of what reviewer A's F1 fix exposed. With
    `validate_assignment=True` the old implementation could not work: it set
    `status = CLAIMED` and only then the owner and the expiry, so the node passed
    through a state the validators refuse -- CLAIMED with no lease. Assigning the
    lease first fails the other way, on the converse rule. There is no order of
    three assignments that is valid at every step, which is the point: a node is
    only meaningful as a whole, so the whole node is replaced.

    The map is rebuilt from plain data so every rule -- the node's and the map's
    -- runs over the result exactly as it would over a map read from disk.
    """
    raw = dmap.model_dump(mode="json")
    for node in raw["nodes"]:
        if node["decision_id"] == decision_id:
            node.update({k: (v.value if isinstance(v, StrEnum) else v)
                         for k, v in fields.items()})
    if generation is not None:
        raw["generation"] = generation
    return DecisionMap.model_validate(raw)


def _live_claim(node: DecisionNode, now: datetime) -> bool:
    return (node.status is DecisionStatus.CLAIMED
            and node.claim_expires_at is not None and node.claim_expires_at > now)


def _on_frontier(dmap: DecisionMap, node: DecisionNode, now: datetime) -> bool:
    if node.status is DecisionStatus.OPEN:
        pass
    elif node.status is DecisionStatus.CLAIMED and not _live_claim(node, now):
        pass  # an expired claim puts the question back on the frontier
    else:
        return False
    status = {n.decision_id: n.status for n in dmap.nodes}
    # Only RESOLVED unblocks: an UNDERMINED premise is not an answer.
    return all(status[b] is DecisionStatus.RESOLVED for b in node.blocked_by)


def decision_frontier(dmap: DecisionMap, *, now: datetime) -> list[DecisionNode]:
    """The questions that can be resolved now, in map order. Never fog."""
    return [n for n in dmap.nodes if _on_frontier(dmap, n, now)]


def claim(dmap: DecisionMap, decision_id: str, *, owner: str,
          expires_at: datetime, now: datetime) -> DecisionMap:
    """Move a frontier node to CLAIMED on a new map; the input is not mutated."""
    if dmap.fog_patch(decision_id) is not None:
        raise DecisionError(
            f"{decision_id!r} is a fog patch: fog cannot be claimed, leased or run. "
            "Formulate it as a decision node first.")
    node = dmap.node(decision_id)
    if node is None:
        raise DecisionError(f"decision {decision_id!r} is not in map {dmap.map_id!r}")
    if _live_claim(node, now):
        raise DecisionError(
            f"decision {decision_id!r} is held by a live claim of {node.claimed_by!r} "
            f"until {node.claim_expires_at.isoformat()}")
    if not _on_frontier(dmap, node, now):
        raise DecisionError(
            f"decision {decision_id!r} ({node.status}) is not on the decision frontier")
    if not owner or not owner.strip():
        raise DecisionError("a claim needs an owner")
    if expires_at <= now:
        raise DecisionError(f"a claim on {decision_id!r} must expire after now")
    return _with_node(dmap, decision_id,
                      status=DecisionStatus.CLAIMED,
                      claimed_by=owner,
                      claim_expires_at=expires_at)


def resolve(dmap: DecisionMap, record: ResolutionRecord, *, now: datetime) -> DecisionMap:
    """Attach `record`, set the node RESOLVED and bump the generation.

    Returns a new map; the input is not mutated. `now` is taken for symmetry
    with `claim` and so a later lease policy has the clock it needs.
    """
    if dmap.fog_patch(record.decision_id) is not None:
        raise DecisionError(
            f"{record.decision_id!r} is a fog patch: fog cannot be resolved, "
            "only formulated as a decision node")
    node = dmap.node(record.decision_id)
    if node is None:
        raise DecisionError(
            f"record names decision {record.decision_id!r}, which map {dmap.map_id!r} "
            "does not contain")
    if node.status is DecisionStatus.OUT_OF_SCOPE:
        raise DecisionError(f"decision {record.decision_id!r} is OUT_OF_SCOPE")
    if node.status is DecisionStatus.RESOLVED or node.resolution is not None:
        raise DecisionError(
            f"decision {record.decision_id!r} is already resolved; a record is never "
            "overwritten in place -- correct it in a new generation")
    # Reviewer A F2, the worst finding in the review and the one this plane exists
    # to prevent. `decision_frontier` and `claim` both refuse a node whose premise
    # is unresolved or UNDERMINED; `resolve` -- the one function that actually sets
    # a node RESOLVED -- checked only fog, unknown id, OUT_OF_SCOPE and
    # already-resolved. So a question standing on an UNDERMINED premise could be
    # answered, and since `graduation_blockers` never reads `blocked_by`, the map
    # then graduated. An answer built on a question nobody has answered is exactly
    # "how a graph drifts while every local edit looks valid".
    #
    # The derived spec is where this was lost: dogfood/p2-19/spec.md listed three
    # refusals for `resolve` and the source spec's sections 4 and 7 permit no gate
    # that opens on an undermined premise. The spec under-specified; the module
    # obeyed it.
    status = {n.decision_id: n.status for n in dmap.nodes}
    if standing := [b for b in node.blocked_by
                    if status.get(b) is not DecisionStatus.RESOLVED]:
        raise DecisionError(
            f"decision {record.decision_id!r} is blocked by {standing}, which "
            "nothing has resolved: an answer standing on an unanswered or "
            "undermined premise is not an answer")
    if node.status is DecisionStatus.UNDERMINED:
        raise DecisionError(
            f"decision {record.decision_id!r} is UNDERMINED: it must be reopened "
            "before it can be answered, so that the reopening is on the record")
    # Reviewer A F2, sub-finding: `claim` guards the lease and `resolve` did not,
    # so a second worker could answer a question someone else held and the lease
    # vanished with it. The holder may of course answer their own claim.
    if _live_claim(node, now) and node.claimed_by != record.resolved_by:
        raise DecisionError(
            f"decision {record.decision_id!r} is held by a live claim of "
            f"{node.claimed_by!r} until {node.claim_expires_at.isoformat()}; "
            f"{record.resolved_by!r} may not answer it")
    return _with_node(dmap, record.decision_id,
                      generation=dmap.generation + 1,
                      resolution=record.model_dump(mode="json"),
                      status=DecisionStatus.RESOLVED,
                      claimed_by=None,
                      claim_expires_at=None)


# --------------------------------------------------------------------------- #
# The Graduation Gate
# --------------------------------------------------------------------------- #


def graduation_blockers(dmap: DecisionMap, *, scope_refs: Iterable[str]) -> list[str]:
    """Why `dmap` may not graduate into execution for `scope_refs`; [] if it may."""
    scope = {s for s in scope_refs if s}
    if not scope:
        return ["no execution scope named: graduate into what? "
                "A map that graduates into nothing has not been gated"]
    blockers: list[str] = []
    for node in dmap.nodes:
        if node.status not in UNRESOLVED:
            continue
        if not node.scope_refs:
            # Reviewer A F4(b): an unresolved question that names no scope blocked
            # nothing, for any scope. But a question whose scope nobody wrote down
            # has not been shown to be outside this one -- unknown scope is not
            # out of scope, and OUT_OF_SCOPE is the status that says so.
            blockers.append(
                f"decision {node.decision_id!r} is {node.status} and names no "
                "scope, so it cannot be shown not to block this one; mark it "
                "OUT_OF_SCOPE or say what it blocks")
            continue
        if hit := sorted(scope & set(node.scope_refs)):
            blockers.append(
                f"decision {node.decision_id!r} is {node.status} and blocks scope {hit}")
    status = {n.decision_id: n.status for n in dmap.nodes}
    for patch in dmap.fog:
        if patch.out_of_scope or patch.deferred_reason:
            continue
        pending = [c for c in patch.clears_with if status.get(c) is not DecisionStatus.RESOLVED]
        if pending:
            blockers.append(
                f"fog {patch.fog_id!r} is in scope and waits on unresolved decisions {pending}")
        elif not patch.clears_with:
            # Reviewer A F4(a). Fog that is in scope, identifies nothing that
            # would make it formulable, and carries no affirmative deferral is the
            # vaguest thing the map can hold. Section 7 requires the remaining fog
            # to be justified, not merely quiet.
            blockers.append(
                f"fog {patch.fog_id!r} is in scope and names nothing that would "
                "clear it: say what it waits on, mark it out of scope, or give a "
                "deferred_reason -- an unjustified patch of fog is not a "
                "graduated map")
    return blockers
