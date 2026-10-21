"""The ready frontier: which of the READY nodes may be taken together.

`ProjectState.ready()` answers "which nodes have all their dependencies
settled". That is a list, and a list is not a plan: two nodes in it can still
write the same path, or one can read what the other writes (the failure that
produced this project's union gate), and then they must not proceed side by
side. Until this module the orchestrator took the first element of that list
and recorded, for the others, whether they conflicted with it.

`ReadyFrontierPlanner` is the pure half of the answer. Given the ready nodes
in their persisted order and the launcher's measured `depends_on`, it returns
a `FrontierPlan`: the nodes that are pairwise independent (`selected`, the
frontier) and, for every other ready node, the selected node it conflicts with
and why (`deferred`). It does no I/O, holds no state, launches nothing and
writes nothing.

Three properties are deliberate:

**The first ready node is always selected.** The plan is greedy in the given
order, so the node the orchestrator dispatched before this module existed is
still the one it dispatches (`plan.head`). The increment changes who decides,
not what is decided.

**Not provably independent means dependent.** A pair is independent only when
`depends_on` answers exactly `False` in both directions. `None` (a protocol
stub that was never overridden), any other falsy or truthy value, or one
direction saying `True` all serialise. A scheduler that rounds "unknown" to
"independent" runs work whose precondition never happened.

**The plan is deterministic.** Same nodes in the same order and the same
answers give the same plan; nothing here reads a clock, a random source or the
environment. Nodes are compared by object, never by id: the project state does
not guarantee that ids are unique.

What the plan is *not*: it does not look at budget, workspace exclusivity,
provider health or capacity. Those narrow the frontier in later slices, each
behind its own contract; this one only knows what the project state itself
declares (`writes`, `semantic_reads`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .project import TaskNode

#: The only reason a node is deferred by this planner. A later slice that adds
#: a width, budget or lease limit adds its own spelling next to it.
SEMANTIC_DEPENDENCY = "SEMANTIC_DEPENDENCY"

DependsOn = Callable[[TaskNode, TaskNode], bool]


@dataclass(frozen=True)
class Deferral:
    """A ready node that is not in this frontier, and what it waits behind."""

    #: The deferred node and the selected node it conflicts with: the objects
    #: that were passed in, so a duplicate id cannot be mistaken for either.
    node: TaskNode
    blocked_by: TaskNode
    reason: str = SEMANTIC_DEPENDENCY


@dataclass(frozen=True)
class FrontierPlan:
    """The frontier chosen from one list of ready nodes."""

    #: Pairwise independent, in the order they were given. The same node
    #: objects that were passed in, not copies.
    selected: tuple[TaskNode, ...] = ()
    #: Every ready node that is not selected, in the order it was given.
    deferred: tuple[Deferral, ...] = ()

    @property
    def head(self) -> TaskNode | None:
        """The node a one-at-a-time loop dispatches; None for an empty frontier."""
        return self.selected[0] if self.selected else None

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(n.id for n in self.selected)


class ReadyFrontierPlanner:
    """Selects the independent subset of the READY nodes. Stateless."""

    def plan(self, ready: Sequence[TaskNode], depends_on: DependsOn) -> FrontierPlan:
        selected: list[TaskNode] = []
        deferred: list[Deferral] = []
        for node in ready:
            blocker = self._first_conflict(selected, node, depends_on)
            if blocker is None:
                selected.append(node)
            else:
                deferred.append(Deferral(node, blocker))
        return FrontierPlan(tuple(selected), tuple(deferred))

    @staticmethod
    def _first_conflict(selected: Sequence[TaskNode], node: TaskNode,
                        depends_on: DependsOn) -> TaskNode | None:
        for chosen in selected:
            if depends_on(chosen, node) is not False \
                    or depends_on(node, chosen) is not False:
                return chosen
        return None
