"""Which open decisions hold which ready work (V3.3 P3-27, first slice).

`hoh.autonomy.route()` answers what happens to a decision the programme may not
take: asked, collected for a digest, or *parked* -- "this branch waits;
independent READY work continues". `hoh.governance` records the answer and
parks the branch. Nothing reads the answer back at the level of the project
graph: the orchestrator takes `ready()[0]`, and a node that is parked, a node
that waits for a person and a node that is an external action all look the same
to the code that decides what runs next. This module is that reading.

It is a **read model**. Given the project's nodes and the routings that stopped
their branches, it answers three questions and nothing else:

* which decisions are open (`DecisionFrontier.decisions`), each with what it
  `holds` (READY work that cannot run until it is answered) and what it `frees`
  (the held work whose *only* open blocker it is) -- so a person or a policy can
  answer the decision that unblocks most first;
* which work can run now, beside every open decision (`runnable`); and
* what is blocked for another reason (`other_blocked`: provider limit, storage,
  a merge that did not land) or can never run as the graph stands
  (`unrunnable`: an unknown dependency, a cycle) -- named, never counted as a
  decision and never counted as independent.

Two frontiers are never one queue (`hoh.decisions`): this is not the Decision
*Map* of P2-19 (questions that must become clear before work packages exist; its
`decision_frontier` lists questions that can be resolved now) and it is not the
Execution Frontier either (`ProjectState.ready()` and the planner of P3-01). It
reads what the contract parked and what waits for a person, and says which
ready work stands behind it. It executes nothing, claims nothing, resolves
nothing and starts nothing; the loop that would act on it is a later slice.
Pure: no filesystem, git, clock or network.

Fail-closed in one direction: whatever this module cannot classify is **held**,
never released. A BLOCKED node it cannot explain stays out of `runnable` and out
of `frees`; so does everything behind it.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .autonomy import Handling, Routing
from .failures import AUTOMATIC_KINDS
from .project import ActionClass, Lifecycle, ProjectState

#: `blocked_kind` values `governance.block_under_contract` writes for a branch
#: waiting for an answer rather than for a time.
DECISION_KINDS = frozenset({"parked_by_contract", "collected_for_digest", "human"})

#: `blocked_kind` values that wait for a time. Wins over a routing found in the
#: node's log: the log is history, the kind is what the node waits for now.
TIMED_KINDS = frozenset({"usage_limit", "storage_pressure"}) | AUTOMATIC_KINDS

#: Handlings that end a branch. Nobody answers them; they are listed so a
#: reader sees what is held behind an abandoned branch.
TERMINAL_HANDLINGS = frozenset({Handling.ABORT_BRANCH.value, Handling.STOP_PROGRAM.value})

#: `blocked_kind` values of a branch the contract ended (`block_under_contract`).
#: Nobody answers these whatever the log says or fails to say.
TERMINAL_KINDS = frozenset({"contract_abort", "contract_stop"})

#: `OpenDecision.handling` when the evidence is not a routing.
EXTERNAL_ACTION = "EXTERNAL_ACTION"
UNKNOWN = "UNKNOWN"
UNREADABLE = "UNREADABLE"


class FrontierError(ValueError):
    """The input is not a project graph this module can describe."""


@dataclass(frozen=True)
class OpenDecision:
    """One node that waits for an answer, and what waits behind it."""

    node_id: str
    #: A `Handling` value, or EXTERNAL_ACTION / UNKNOWN / UNREADABLE.
    handling: str
    kind: str
    decision: str
    why: str
    retry_after: str | None
    #: False for a branch the contract ended (by its routing or by its
    #: `blocked_kind`); nothing answers it, so it frees nothing.
    resolvable: bool
    #: READY nodes that cannot run while this is open, in graph order: internal
    #: work, and EXTERNAL nodes that are not yet reachable themselves.
    holds: tuple[str, ...]
    #: The internal part of `holds` that runs as soon as this is answered and its
    #: already-queued predecessors have run: nothing else open stands behind it.
    frees: tuple[str, ...]


@dataclass(frozen=True)
class DecisionFrontier:
    decisions: tuple[OpenDecision, ...] = ()
    runnable: tuple[str, ...] = ()
    other_blocked: tuple[str, ...] = ()
    unrunnable: tuple[str, ...] = ()


def _stops(node_id: str, value: object) -> tuple[Routing, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise FrontierError(f"stops[{node_id!r}] must be a sequence of Routing")
    for r in value:
        if not isinstance(r, Routing):
            raise FrontierError(f"stops[{node_id!r}] holds a {type(r).__name__}, "
                                f"not a Routing")
    return tuple(r for r in value if r.blocks_branch)


def derive_frontier(state: ProjectState, *,
                    stops: Mapping[str, Sequence[Routing]] | None = None,
                    unreadable: Mapping[str, str] | None = None) -> DecisionFrontier:
    """The open decisions of `state` and the work each one holds.

    `stops[node_id]` are the routings, in log order, that stopped that node's
    branch (`governance.read_branch_stops`); `unreadable[node_id]` says why a
    node's log could not be read. Both are keyed by node id and refused if they
    name a node the state does not have.
    """
    nodes = list(state.nodes)
    ids = [n.id for n in nodes]
    if len(set(ids)) != len(ids):
        raise FrontierError("the project has two nodes with one id; a graph "
                            "whose nodes cannot be told apart has no frontier")
    known = {n.id: n for n in nodes}
    stops = stops or {}
    unreadable = unreadable or {}
    for name, given in (("stops", stops), ("unreadable", unreadable)):
        if stray := sorted(k for k in given if k not in known):
            raise FrontierError(f"{name} names nodes the state does not have: {stray}")
    routed = {k: _stops(k, v) for k, v in stops.items()}
    for k, why in unreadable.items():
        if not isinstance(why, str):
            raise FrontierError(f"unreadable[{k!r}] must say why, as text")

    # Which nodes can never run on the graph's own account: Kahn's order, with
    # an unknown dependency as an edge nothing ever satisfies. A node it cannot
    # emit is on a cycle, depends on an unknown node, or stands behind one.
    pending = {n.id: len(n.dependencies) for n in nodes}
    dependents: dict[str, list[str]] = {n.id: [] for n in nodes}
    for n in nodes:
        for d in n.dependencies:
            if d in known:
                dependents[d].append(n.id)
    queue = deque(i for i, c in pending.items() if c == 0)
    emitted: set[str] = set()
    while queue:
        i = queue.popleft()
        emitted.add(i)
        for j in dependents[i]:
            pending[j] -= 1
            if pending[j] == 0:
                queue.append(j)
    structural = set(ids) - emitted

    # An EXTERNAL node is a decision once nothing stands in front of it: exactly
    # when it would be the loop's head. Before that it is queued, like any other
    # READY node behind a running or a parked one.
    reachable = {n.id for n in state.ready()}

    # What each node is, as a waiting thing.
    decisions: dict[str, OpenDecision] = {}
    other_blocked: list[str] = []
    seeds: dict[str, dict] = {}
    for n in nodes:
        if n.lifecycle is Lifecycle.BLOCKED:
            kind = n.blocked_kind or ""
            recs = routed.get(n.id, ())
            if kind in TIMED_KINDS:
                other_blocked.append(n.id)
            elif kind in DECISION_KINDS or recs or n.id in unreadable:
                if n.id in unreadable:
                    seeds[n.id] = dict(handling=UNREADABLE, kind=kind, decision=n.note,
                                       why=f"the routing log cannot be read: {unreadable[n.id]}")
                elif recs:
                    last = recs[-1]
                    seeds[n.id] = dict(handling=last.handling.value, kind=last.kind,
                                       decision=last.decision, why=last.why)
                else:
                    seeds[n.id] = dict(handling=UNKNOWN, kind=kind, decision=n.note,
                                       why=f"blocked_kind={kind!r} and no stopping routing "
                                           f"was supplied")
            else:
                other_blocked.append(n.id)
        elif (n.lifecycle is Lifecycle.READY and n.action_class is ActionClass.EXTERNAL
              and n.id in reachable):
            seeds[n.id] = dict(handling=EXTERNAL_ACTION, kind=ActionClass.EXTERNAL.value,
                               decision=n.note,
                               why="an irreversible external action; the orchestrator "
                                   "does not take that decision")

    # What stands behind each open blocker. A settled node never blocks and is
    # not walked through. A queued READY internal node is work, not a cause; an
    # EXTERNAL node is one whether or not it can be answered yet.
    causes = {n.id for n in nodes if not n.settled and (
        n.lifecycle is not Lifecycle.READY or n.action_class is ActionClass.EXTERNAL)}
    behind: dict[str, set[str]] = {i: set() for i in ids}
    for c in causes:
        seen = {c}
        walk = deque([c])
        while walk:
            for j in dependents[walk.popleft()]:
                if j in seen or known[j].settled:
                    continue
                seen.add(j)
                behind[j].add(c)
                walk.append(j)

    queued = [n.id for n in nodes if n.lifecycle is Lifecycle.READY]
    internal = {n.id for n in nodes if n.action_class is not ActionClass.EXTERNAL}
    for node_id, seed in seeds.items():
        resolvable = (seed["handling"] not in TERMINAL_HANDLINGS
                      and known[node_id].blocked_kind not in TERMINAL_KINDS)
        holds = tuple(w for w in queued if node_id in behind[w])
        frees = tuple(w for w in holds if w in internal and behind[w] == {node_id}
                      and w not in structural) if resolvable else ()
        decisions[node_id] = OpenDecision(
            node_id=node_id, resolvable=resolvable, holds=holds, frees=frees,
            retry_after=known[node_id].retry_after, **seed)

    order = {i: k for k, i in enumerate(ids)}
    ranked = sorted(decisions.values(), key=lambda d: (
        not d.resolvable, -len(d.frees), -len(d.holds), order[d.node_id]))
    return DecisionFrontier(
        decisions=tuple(ranked),
        runnable=tuple(n.id for n in state.ready()
                       if n.action_class is not ActionClass.EXTERNAL),
        other_blocked=tuple(other_blocked),
        unrunnable=tuple(w for w in queued if w in structural))
