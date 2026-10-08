"""The digital thread: one walkable chain from requirement to release (V3.3 P2-31).

The chain an auditor has to be able to walk end to end:

    Requirement -> Project Contract / Goal -> Work Package -> Checklist
    Requirement -> Task / Spec / Amendment -> Policy + Routing Decision -> Run /
    Agent / Tools -> Candidate -> Checks / Receipts / EvidenceClaim -> QA /
    Negative Controls -> Repair / Revalidation -> Acceptance -> Integration /
    Closure -> Commit / Merge -> Milestone -> Release / Export Manifest

The sentence the whole design turns on:

    This is a projection over authoritative records, not another source of truth.

So nothing here stores a status. A `DigitalThread` is built from records handed
to it -- every node names the `record_ref` where its authority lives -- and the
functions below only report what those links add up to, and, loudly, where they
do not: `fsck()` lists every structural defect, and a `Trace` is `complete` only
when no stage between its endpoints is missing *and* `fsck()` found nothing.
An unknown or ambiguous identifier raises; it is never an empty, successful
answer.

The module is pure: no filesystem, no git, no ledger parsing, no clock. Equal
inputs give equal answers, and nothing handed in is mutated.
"""

from __future__ import annotations

from collections import deque
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

__all__ = [
    "AmbiguousIdentifier",
    "DigitalThread",
    "ThreadEdge",
    "ThreadLookupError",
    "ThreadNode",
    "ThreadStage",
    "Trace",
    "UnknownIdentifier",
    "fsck",
    "release_blocking_trace",
    "trace",
    "trace_by",
    "why",
]


class ThreadStage(StrEnum):
    """The plan's chain, in the plan's order. Stage order is declaration order
    and nothing else -- `_rank()` reads it from here."""

    REQUIREMENT = "REQUIREMENT"
    PROJECT_CONTRACT = "PROJECT_CONTRACT"
    WORK_PACKAGE = "WORK_PACKAGE"
    CHECKLIST_REQUIREMENT = "CHECKLIST_REQUIREMENT"
    TASK_SPEC_AMENDMENT = "TASK_SPEC_AMENDMENT"
    POLICY_ROUTING_DECISION = "POLICY_ROUTING_DECISION"
    RUN = "RUN"
    CANDIDATE = "CANDIDATE"
    CHECKS_RECEIPTS = "CHECKS_RECEIPTS"
    QA_NEGATIVE_CONTROLS = "QA_NEGATIVE_CONTROLS"
    REPAIR_REVALIDATION = "REPAIR_REVALIDATION"
    ACCEPTANCE = "ACCEPTANCE"
    INTEGRATION_CLOSURE = "INTEGRATION_CLOSURE"
    COMMIT_MERGE = "COMMIT_MERGE"
    MILESTONE = "MILESTONE"
    RELEASE_EXPORT = "RELEASE_EXPORT"


_ORDER: tuple[ThreadStage, ...] = tuple(ThreadStage)
_RANK: dict[ThreadStage, int] = {s: i for i, s in enumerate(_ORDER)}


def _rank(stage: ThreadStage) -> int:
    return _RANK[stage]


class ThreadLookupError(LookupError):
    """An identifier did not resolve to exactly one node."""


class UnknownIdentifier(ThreadLookupError):
    """The thread holds no node for this identifier. Never an empty answer."""


class BrokenProjection(ThreadLookupError):
    """The projection's edges name nodes it does not contain.

    Raised by `why`, whose whole answer is a list of causes: reviewer B F5 found
    it silently omitting the causes behind a broken reference. `trace` does not
    raise -- it carries `fsck` in its result, so the defect is in the answer.
    """


class AmbiguousIdentifier(ThreadLookupError):
    """More than one node matches; the projection does not pick one."""


class _Frozen(BaseModel):
    """The project's `Strict` rules (no unknown fields, whitespace stripped),
    plus frozen. `hoh.contracts.Strict` is deliberately mutable, so the thread
    carries its own config rather than changing the shared base."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ThreadNode(_Frozen):
    """One authoritative record, as referenced -- not the record itself.

    `record_ref` is where the authority actually lives. A node without one is
    refused: a projection element that cannot be traced back to its record is
    decoration.
    """

    node_id: str = Field(min_length=1)
    stage: ThreadStage
    record_ref: str = Field(min_length=1)
    digest: str = Field(min_length=1)
    label: str = ""


class ThreadEdge(_Frozen):
    """A link that already exists in the records, from upstream to downstream.

    Every edge carries a stable digest, so an empty one is refused; an edge
    from a node to itself links nothing and is refused too.
    """

    from_id: str = Field(min_length=1)
    to_id: str = Field(min_length=1)
    relation: str = Field(min_length=1)
    digest: str = Field(min_length=1)

    @model_validator(mode="after")
    def _not_a_self_edge(self) -> ThreadEdge:
        if self.from_id == self.to_id:
            raise ValueError(f"edge from {self.from_id!r} to itself links nothing")
        return self


class DigitalThread(_Frozen):
    """The digital thread for one scope, at one generation.

    This is a projection over authoritative records, not a source of truth.
    It is frozen because a projection that can be edited in place would itself
    become a source of truth: a status set here instead of in the record it
    projects would disagree with that record, and nothing would say which one
    is right. To change what the thread says, change the records and project
    again (a new `generation`).

    `projected_from` names the record sets this projection was derived from.
    Nodes and edges are held as tuples, so nothing can be appended in place.
    Duplicate `node_id`s are refused; structural defects between nodes and
    edges are not refused here but reported by `fsck()`, so a broken thread
    can still be looked at -- it just never traces complete.
    """

    thread_id: str = Field(min_length=1)
    generation: int = Field(ge=0)
    nodes: tuple[ThreadNode, ...]
    edges: tuple[ThreadEdge, ...] = ()
    projected_from: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_node_ids_and_named_sources(self) -> DigitalThread:
        seen: set[str] = set()
        dupes = sorted({n.node_id for n in self.nodes
                        if n.node_id in seen or seen.add(n.node_id)})
        if dupes:
            raise ValueError(f"duplicate node_id(s): {dupes}")
        if any(not ref for ref in self.projected_from):
            raise ValueError("projected_from holds an empty record-set reference")
        return self

    def node(self, node_id: str) -> ThreadNode:
        """The node with this id; an unknown id raises `UnknownIdentifier`."""
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        raise UnknownIdentifier(f"thread {self.thread_id!r} has no node {node_id!r}")


class Trace(_Frozen):
    """What a walk from one node reached, and what it did not.

    `stages` and `path` are in `ThreadStage` order. `missing` lists the stages
    from REQUIREMENT up to the last reached stage that no reached node covers --
    anchored at the start of the chain the plan names, because a trace that never
    reaches a requirement is missing the requirement, not missing nothing
    (reviewer B F1). `unwalkable` names the consecutive covered stages that no
    edge connects, so a stage covered by a node hanging off the side of the graph
    cannot stand in for one on the chain (reviewer B F2). `fsck` holds the
    thread's structural defects. `complete` is computed from the three and cannot
    be handed in: a gap, an unwalkable step or a defect is never complete.
    """

    thread_id: str
    generation: int
    origin: str
    stages: tuple[ThreadStage, ...]
    path: tuple[str, ...]
    missing: tuple[ThreadStage, ...]
    unwalkable: tuple[str, ...] = ()
    fsck: tuple[str, ...]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def complete(self) -> bool:
        return not self.missing and not self.unwalkable and not self.fsck


# --------------------------------------------------------------------------- #
# graph helpers -- only over edges whose endpoints are both nodes
# --------------------------------------------------------------------------- #


def _adjacency(thread: DigitalThread) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    ids = {n.node_id for n in thread.nodes}
    down: dict[str, list[str]] = {i: [] for i in ids}
    up: dict[str, list[str]] = {i: [] for i in ids}
    for e in thread.edges:
        if e.from_id in ids and e.to_id in ids:
            down[e.from_id].append(e.to_id)
            up[e.to_id].append(e.from_id)
    for m in (down, up):
        for k in m:
            m[k] = sorted(set(m[k]))
    return down, up


def _bfs(start: str, adj: dict[str, list[str]]) -> list[str]:
    """Nodes reachable from `start` (excluding it unless a cycle returns to
    it), nearest first, ties by id."""
    seen: set[str] = set()
    order: list[str] = []
    queue = deque([start])
    while queue:
        cur = queue.popleft()
        for nxt in adj[cur]:
            if nxt not in seen:
                seen.add(nxt)
                order.append(nxt)
                queue.append(nxt)
    return order


def _unwalkable(thread: DigitalThread, reached: set[str],
                stages: tuple[ThreadStage, ...]) -> tuple[str, ...]:
    """Consecutive covered stages that no edge connects. Reviewer B F2.

    Coverage alone is not traceability. The reviewer built a thread carrying all
    sixteen stages where the chain ran PROJECT_CONTRACT -> CHECKLIST_REQUIREMENT,
    skipping WORK_PACKAGE, and a work-package node hung off the side with one edge
    into the middle of the chain. Every stage was "reached", `fsck` was silent
    because its skipped-stage rule looks at a narrower node set, and the trace
    reported a complete requirement-to-release chain that no walk realises.

    So for each adjacent pair of covered stages this asks the plain question: is
    there an edge from a reached node of the earlier stage to a reached node of
    the later one? Edges whose endpoints are not nodes are ignored here; they are
    `fsck`'s business and `complete` already accounts for them.
    """
    by_id = {n.node_id: n for n in thread.nodes}
    at: dict[ThreadStage, set[str]] = {}
    for i in reached:
        at.setdefault(by_id[i].stage, set()).add(i)
    links: set[tuple[ThreadStage, ThreadStage]] = set()
    for e in thread.edges:
        if e.from_id in reached and e.to_id in reached:
            links.add((by_id[e.from_id].stage, by_id[e.to_id].stage))
    gaps: list[str] = []
    for earlier, later in zip(stages, stages[1:]):
        if (earlier, later) not in links:
            gaps.append(
                f"{earlier} and {later} are both covered but no edge joins them: "
                f"{sorted(at[earlier])} -> {sorted(at[later])} is not a walk")
    return tuple(gaps)


# --------------------------------------------------------------------------- #
# fsck
# --------------------------------------------------------------------------- #


def fsck(thread: DigitalThread) -> list[str]:
    """Every structural way this projection is not trustworthy, or `[]`."""
    problems: list[str] = []
    by_id = {n.node_id: n for n in thread.nodes}
    down, up = _adjacency(thread)
    desc = {i: set(_bfs(i, down)) for i in by_id}
    anc = {i: set(_bfs(i, up)) for i in by_id}

    # broken references -- reported, never silently dropped
    for e in thread.edges:
        for end, ref in (("from_id", e.from_id), ("to_id", e.to_id)):
            if ref not in by_id:
                problems.append(
                    f"broken reference: edge {e.from_id}->{e.to_id} ({e.relation}) "
                    f"{end} {ref!r} is not a node in the thread")

    # direction and skipped stages
    for e in thread.edges:
        if e.from_id not in by_id or e.to_id not in by_id:
            continue
        a, b = by_id[e.from_id], by_id[e.to_id]
        ra, rb = _rank(a.stage), _rank(b.stage)
        if rb < ra:
            problems.append(
                f"backwards edge: {a.node_id} ({a.stage}) -> {b.node_id} ({b.stage}) "
                f"runs against ThreadStage order")
        elif rb > ra + 1:
            on_path = desc[a.node_id] | anc[b.node_id]
            skipped = sorted(
                {by_id[w].stage for w in on_path
                 if ra < _rank(by_id[w].stage) < rb}, key=_rank)
            if skipped:
                problems.append(
                    f"skipped stage: edge {a.node_id} ({a.stage}) -> {b.node_id} "
                    f"({b.stage}) jumps over {[str(s) for s in skipped]}, which "
                    f"this path has nodes for")

    # same endpoints and relation, different digests
    digests: dict[tuple[str, str, str], set[str]] = {}
    for e in thread.edges:
        digests.setdefault((e.from_id, e.to_id, e.relation), set()).add(e.digest)
    for (f, t, rel), ds in sorted(digests.items()):
        if len(ds) > 1:
            problems.append(
                f"conflicting edge digests: {f}->{t} ({rel}) carries {sorted(ds)}")

    # one stage twice on one path with different digests
    ids = sorted(by_id)
    for i, u in enumerate(ids):
        for v in ids[i + 1:]:
            nu, nv = by_id[u], by_id[v]
            if nu.stage != nv.stage or nu.digest == nv.digest:
                continue
            if v in desc[u] or u in desc[v]:
                problems.append(
                    f"conflicting stage digests: {u} and {v} are both {nu.stage} on "
                    f"one path with digests {nu.digest!r} != {nv.digest!r}")
                continue
            # Reviewer B F3: two nodes of one stage that neither reaches the other
            # were treated as harmless siblings -- but when a later node descends
            # from both, the chain passes through both and which digest it rests on
            # is exactly undecided. Pure siblings that never reconverge stay
            # exempt, which `tests/test_thread.py` pins deliberately.
            if shared := sorted(desc[u] & desc[v]):
                problems.append(
                    f"conflicting stage digests: {u} and {v} are both {nu.stage} "
                    f"with digests {nu.digest!r} != {nv.digest!r} and both are "
                    f"upstream of {shared[0]}, so which one it rests on is undecided")

    # cycles, one message per strongly connected component
    reported: set[str] = set()
    for u in ids:
        if u in reported or u not in desc[u]:
            continue
        component = sorted(v for v in desc[u] if u in desc[v])
        reported.update(component)
        problems.append(f"cycle: {component} reach each other")

    return problems


# --------------------------------------------------------------------------- #
# queries
# --------------------------------------------------------------------------- #


def trace(thread: DigitalThread, *, node_id: str) -> Trace:
    """Walk from `node_id` to both ends of the chain: everything upstream of it
    and everything downstream. An unknown id raises `UnknownIdentifier`."""
    thread.node(node_id)
    by_id = {n.node_id: n for n in thread.nodes}
    down, up = _adjacency(thread)
    reached = {node_id} | set(_bfs(node_id, up)) | set(_bfs(node_id, down))
    path = tuple(sorted(reached, key=lambda i: (_rank(by_id[i].stage), i)))
    stages = tuple(sorted({by_id[i].stage for i in reached}, key=_rank))
    # Reviewer B F1. `missing` used to be measured between the *reached*
    # endpoints, so a trace that never reaches REQUIREMENT had nothing it could
    # be missing: an orphan RELEASE_EXPORT node reported complete=True, and an
    # Evidence Pack would have carried a green release-blocking trace for a
    # release with no traceability at all. The chain the plan names starts at
    # Requirement, so the low end is anchored there rather than wherever the walk
    # happened to stop. The high end stays where the walk reached: a requirement
    # that has not been released yet is not missing its release, it simply has
    # not got there, and `tests/test_thread.py` pins that distinction.
    hi = _rank(stages[-1])
    missing = tuple(s for s in _ORDER[0:hi + 1] if s not in stages)
    # Reviewer B F2. Stage coverage came from the origin's whole bow-tie, so a
    # node attached anywhere in it counted as covering its stage even when the
    # actual chain skipped that stage -- a 16-stage complete=True trace with no
    # walkable requirement-to-release path. Coverage now has to be *walkable*:
    # between two consecutive covered stages there must be an edge from a reached
    # node of the earlier one to a reached node of the later one.
    unwalkable = _unwalkable(thread, reached, stages)
    return Trace(thread_id=thread.thread_id, generation=thread.generation,
                 origin=node_id, stages=stages, path=path, missing=missing,
                 unwalkable=unwalkable, fsck=tuple(fsck(thread)))


def _find(thread: DigitalThread, *, stage: ThreadStage, record_ref: str) -> ThreadNode:
    # Reviewer B F7: the plan's query surface is `trace requirement <id>`, and a
    # lower-case stage name escaped the declared error hierarchy as a bare
    # ValueError from the enum.
    try:
        stage = ThreadStage(stage)
    except ValueError as exc:
        raise UnknownIdentifier(
            f"{stage!r} is not a stage of the thread; the stages are "
            f"{[s.value for s in ThreadStage]}") from exc
    hits = [n for n in thread.nodes if n.stage == stage and n.record_ref == record_ref]
    if not hits:
        raise UnknownIdentifier(
            f"thread {thread.thread_id!r} has no {stage} node for {record_ref!r}")
    if len(hits) > 1:
        raise AmbiguousIdentifier(
            f"thread {thread.thread_id!r} has {len(hits)} {stage} nodes for "
            f"{record_ref!r}: {sorted(n.node_id for n in hits)}")
    return hits[0]


def trace_by(thread: DigitalThread, *, stage: ThreadStage, record_ref: str) -> Trace:
    """`trace requirement <id>`, `trace candidate <sha>`: find the one node at
    `stage` with `record_ref` and trace it. No match raises
    `UnknownIdentifier`; two or more raise `AmbiguousIdentifier`."""
    return trace(thread, node_id=_find(thread, stage=stage, record_ref=record_ref).node_id)


def why(thread: DigitalThread, *, node_id: str) -> list[ThreadNode]:
    """Why this state exists: the upstream chain only, nearest cause first.

    An unknown id raises `UnknownIdentifier`. A thread whose edges name nodes it
    does not contain raises `BrokenProjection`: reviewer B F5 found that
    `_adjacency` skips such edges, so `why` answered with the causes it could see
    and never mentioned the one it could not -- "a broken reference fails the
    check, it is never silently dropped" held for `trace`, which carries `fsck`
    in its result, and not for the query whose whole job is naming causes.
    """
    thread.node(node_id)
    if broken := [p for p in fsck(thread) if p.startswith("broken reference")]:
        raise BrokenProjection(
            f"thread {thread.thread_id!r} has {len(broken)} broken reference(s), so "
            f"its causes cannot be listed: {broken[0]}")
    _, up = _adjacency(thread)
    return [thread.node(i) for i in _bfs(node_id, up) if i != node_id]


def release_blocking_trace(thread: DigitalThread, *, release_ref: str) -> Trace:
    """The trace the Evidence Pack has to include or link: that of the
    RELEASE_EXPORT node with `release_ref`. A release that is not in the
    projection raises -- "no trace found" must not read as "nothing blocks"."""
    return trace_by(thread, stage=ThreadStage.RELEASE_EXPORT, record_ref=release_ref)
