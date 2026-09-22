"""Typed contracts for the orchestration layer above a single run.

`contracts.py` models one run: a plan, its candidates, its evidence, its
verdict. This module models the layer above it -- which runs exist at all,
what order they may go in, what the global gates said about the merged
result, and what a human decided rather than the orchestrator.

Why that layer needs a schema of its own, stated plainly because it is the
whole justification for this file: the campaign that produced this project's
own release ran that layer as an **agent session's behaviour**. It wrote the
specifications, started the runs, read the verdicts, decided the merges, ran
the global gates and turned their failures into repair runs -- and none of
that was written down anywhere a second session could read. The knowledge
lived in one conversation. If that conversation ended, the only recovery was
a person reconstructing intent from git history.

That is the gap this module closes. A new orchestrator session, holding
nothing but this state, must be able to determine what to do next without
guessing. Everything needed for that decision is therefore a field here, and
every orchestrator decision that is not derivable from the tree -- adding a
dependency edge, disposing of a policy finding, releasing a merge, creating a
repair node -- becomes a digest-bound record rather than a remembered
intention.

Two distinctions from the run layer are carried over deliberately, because
this project learned both the hard way:

* **A terminal DAG is not a closed release.** `DAG_TERMINAL != RC_CLOSED`.
  Every planned node being merged says nothing about whether the *combined*
  state is sound. Closure additionally requires every global gate green and
  no new repair node created by the pass that checked them, which makes
  closure a fixpoint rather than the end of a list.
* **A measurement has to name what it measured.** Gate results carry the
  commit they ran against, not just a verdict. A green recorded without its
  subject is indistinguishable from a green that has since gone stale.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from .contracts import Strict, utcnow

PROJECT_SCHEMA_VERSION = 1


def digest_obj(obj: Any) -> str:
    """A stable digest of any JSON-serialisable object.

    Sorted keys and fixed separators: two runs that produced the same decision
    must produce the same digest, or digest-binding is decorative.
    """
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class Lifecycle(StrEnum):
    """Where a task node sits.

    `BLOCKED` is deliberately distinct from `CONTINGENT`: blocked means a
    policy or a human stands in the way and the orchestrator may not proceed
    on its own; contingent means the node only becomes real if some earlier
    result turns out a particular way. Collapsing them would let an
    orchestrator talk itself past a policy gate by re-evaluating a condition.
    """

    READY = "READY"
    RUNNING = "RUNNING"
    MERGED = "MERGED"
    BLOCKED = "BLOCKED"
    CONTINGENT = "CONTINGENT"
    ABANDONED = "ABANDONED"


#: Lifecycle states that no longer block a dependent node.
SETTLED = frozenset({Lifecycle.MERGED, Lifecycle.ABANDONED})


class ActionClass(StrEnum):
    """Whether a node contains an irreversible external action.

    Kept deliberately narrow. A `git merge` into the mainline is local and
    reversible; a `git push` is not. A tag is local; `push --tags` is not.
    Only `EXTERNAL` requires an authority outside the orchestrator, and
    treating anything else as external turns the gate into noise that gets
    routinely waved through.
    """

    INTERNAL = "INTERNAL"
    EXTERNAL = "EXTERNAL"


class GateOutcome(StrEnum):
    GREEN = "GREEN"
    RED = "RED"
    #: Not executed. Never to be read as green -- see the house rule this
    #: project works under, and the dry-run defect that made it necessary to
    #: give "not executed" a value of its own rather than an exit code of 0.
    NOT_RUN = "NOT_RUN"
    #: The gate ran and the environment structurally cannot answer it -- a
    #: checkout with no `runs/` tree, a machine that cannot create the
    #: namespace a sandbox needs. A fact about the environment, not about the
    #: product, and **not** a pass: a project whose gate cannot be evaluated
    #: has not been shown to be closed. The same distinction
    #: `assurance.ResultState` draws, in the vocabulary the closure reads.
    UNSUPPORTED_ENVIRONMENT = "UNSUPPORTED_ENVIRONMENT"


class DecisionKind(StrEnum):
    """Orchestrator decisions that are not derivable from the tree.

    Each of these was, during this project's own campaign, a judgement made
    inside a conversation and recoverable only from it.
    """

    ADD_DEPENDENCY = "ADD_DEPENDENCY"
    SPEC_AMENDMENT = "SPEC_AMENDMENT"
    POLICY_DISPOSITION = "POLICY_DISPOSITION"
    MERGE_RELEASE = "MERGE_RELEASE"
    CREATE_REPAIR_NODE = "CREATE_REPAIR_NODE"
    ABANDON_NODE = "ABANDON_NODE"
    CLOSURE_VERDICT = "CLOSURE_VERDICT"


class GateResult(Strict):
    """One global gate, and the state it actually ran against.

    `subject` is not optional and not decorative. A gate result without the
    commit it measured cannot be distinguished later from one that has gone
    stale, and this project shipped a release whose figures were wrong for
    exactly that reason: every gate agreed with a number because every gate
    ran in the tree the number came from.
    """

    name: str
    outcome: GateOutcome
    subject: str = Field(description="commit or tree the gate ran against")
    exit_code: int | None = None
    detail: str = ""
    at: str = Field(default_factory=utcnow)
    #: The closure pass this result belongs to. Zero for results written before
    #: the field existed, which reads as "an earlier pass" and is right for
    #: them.
    #:
    #: It exists because a gate that is renamed or removed used to leave its
    #: last result standing for ever. `gates_green` read the latest result per
    #: *name*, so a gate called `claims` that was split into four narrower ones
    #: kept its final RED in the record and the project could never close
    #: again -- permanently blocked by a check that no longer runs. Measured on
    #: this project's own self-dogfood campaign, at closure generation 14.
    generation: int = 0

    @property
    def counts_as_green(self) -> bool:
        """Only GREEN is green.

        `NOT_RUN` is not, and neither is `UNSUPPORTED_ENVIRONMENT`: a gate that
        could not be evaluated has shown nothing, and a closure that counted it
        would be reporting the environment rather than the product. This
        property exists so no caller has to remember that, and so the rule is
        testable in one place.
        """
        return self.outcome is GateOutcome.GREEN


class DecisionRecord(Strict):
    """An orchestrator or human decision, bound to a digest of its content.

    `actor` distinguishes the three authorities this project keeps apart:
    an HoH role inside a run, the orchestrating session above it, and a human
    acting as policy authority. Collapsing them is precisely the misdescription
    this project had to correct publicly.
    """

    id: str
    kind: DecisionKind
    actor: str = Field(description="'orchestrator', 'human', or a role name")
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    at: str = Field(default_factory=utcnow)
    payload_digest: str = ""

    @model_validator(mode="after")
    def _bind(self) -> DecisionRecord:
        expected = digest_obj(self.payload)
        if not self.payload_digest:
            object.__setattr__(self, "payload_digest", expected)
        elif self.payload_digest != expected:
            raise ValueError(
                f"decision {self.id}: payload_digest {self.payload_digest} does not "
                f"match its payload ({expected}) -- a rebound record is a new record"
            )
        return self


class ExternalActionRecord(Strict):
    """A repository mutation made outside the loop, written into the record.

    This exists because of a gap the first real end-to-end run exposed. A merge
    conflict was resolved by a person, in git, and nothing about it reached
    project state: the tree changed, a later closure measured the changed tree,
    and the state carried no trace of why it looked the way it did. The report
    disclosed it afterwards, which is better than hiding it and much worse than
    recording it.

    The heads and tree digests on either side are the point. "Someone fixed the
    conflict" is a sentence; `abc1234 -> def5678` is something a later reader
    can check against the repository and either confirm or contradict.

    Nothing here authorises the action. It records one that happened -- which
    is the only honest thing a record can do about a change it did not make.
    """

    action_id: str
    #: 'human', 'operator', or a named tool. Not free text for its own sake:
    #: the three authorities this project keeps apart are exactly what a reader
    #: needs in order to weigh the change.
    actor: str
    reason: str
    action_class: str = Field(
        default="repository-mutation",
        description="what kind of change this was, for later filtering",
    )
    node: str | None = None
    head_before: str = ""
    head_after: str = ""
    tree_digest_before: str = ""
    tree_digest_after: str = ""
    #: The project's write sequence when this was recorded, so the action can
    #: be placed in the state's own history rather than only in wall-clock time.
    write_seq: int = 0
    command_digest: str | None = None
    at: str = Field(default_factory=utcnow)

    @property
    def changed_the_tree(self) -> bool:
        """Whether anything actually moved.

        A recorded action that changed nothing is not a defect -- an aborted
        merge is worth recording too -- but it is a different thing from one
        that did, and a reader should not have to compare digits to find out.
        """
        return bool(self.head_before and self.head_after
                    and self.head_before != self.head_after)


class TaskNode(Strict):
    """One unit of planned work in the project DAG."""

    id: str
    lifecycle: Lifecycle = Lifecycle.READY
    action_class: ActionClass = ActionClass.INTERNAL
    spec_path: str | None = None
    spec_digest: str | None = None
    run_id: str | None = Field(
        default=None,
        description=(
            "The run this node executes as. NOT derivable from the id: a "
            "continued run declares its own (d7-k -> d7-k2), and the branch is "
            "a third quantity again. Deriving it means guessing."
        ),
    )
    branch: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    #: Paths the node declares it will write. Used to measure whether two
    #: candidate-parallel nodes actually conflict, rather than assuming.
    writes: list[str] = Field(default_factory=list)
    #: Paths whose *content* the node's correctness depends on, even though it
    #: does not write them. A node reading what another writes is dependent
    #: even when their write sets are disjoint -- the failure this project hit
    #: twice, where two individually correct runs merged into a broken state.
    semantic_reads: list[str] = Field(default_factory=list)
    produces: list[str] = Field(default_factory=list)
    invalidates: list[str] = Field(default_factory=list)
    rejections: int = 0
    #: What the run had already accepted when this node was last dispatched.
    #: Recorded in state, not held in memory, because it is the only thing that
    #: lets a *fresh* process tell an acceptance from a run that ended where it
    #: started. Without it, a resuming session looking at a CHECKPOINTED run
    #: cannot distinguish "this dispatch accepted something" from "something was
    #: accepted three iterations ago", and merging on the second is a double
    #: apply.
    accepted_before: str | None = None
    #: Which node's gate failure created this one, if any.
    repair_of: str | None = None
    closure_generation: int = 0
    note: str = ""

    @property
    def settled(self) -> bool:
        return self.lifecycle in SETTLED


class ProjectState(Strict):
    """The durable state of one orchestrated project.

    Everything a fresh orchestrator session needs in order to decide what to
    do next, without a conversation to remember.
    """

    schema_version: int = PROJECT_SCHEMA_VERSION
    project_id: str
    repo_path: str
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)

    nodes: list[TaskNode] = Field(default_factory=list)
    decisions: list[DecisionRecord] = Field(default_factory=list)
    gates: list[GateResult] = Field(default_factory=list)
    #: Repository mutations made outside the loop. Kept apart from `decisions`
    #: on purpose: a decision is something this system chose, an external
    #: action is something that happened to it, and collapsing the two would
    #: let the record imply authorship it does not have.
    external_actions: list[ExternalActionRecord] = Field(default_factory=list)

    #: How many times global closure has been attempted. Each failed attempt
    #: that produces repair work increments it, so a fixpoint is visible as a
    #: generation that created no new nodes.
    closure_generation: int = 0
    #: The commit the last closure attempt attested. Named for the same reason
    #: GateResult.subject is.
    measurement_head: str | None = None
    policy_digest: str | None = None

    #: Monotonic; a writer holding an older value is stale and is refused.
    write_seq: int = Field(default=0, ge=0)

    def node(self, node_id: str) -> TaskNode | None:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def unknown_dependencies(self) -> dict[str, list[str]]:
        """Dependencies naming nodes that do not exist.

        Reported rather than ignored: a dependency on a node that was never
        created reads, to a scheduler, exactly like a dependency that is
        already satisfied.
        """
        known = {n.id for n in self.nodes}
        missing_ = {}
        for n in self.nodes:
            open_ = [d for d in n.dependencies if d not in known]
            if open_:
                missing_[n.id] = open_
        return missing_

    def ready(self) -> list[TaskNode]:
        """Nodes that are READY and whose dependencies have all settled.

        A node with an unknown dependency is **not** returned. Treating an
        unresolvable dependency as satisfied is how a scheduler runs work
        whose precondition never happened.
        """
        known = {n.id: n for n in self.nodes}
        open_ = []
        for n in self.nodes:
            if n.lifecycle is not Lifecycle.READY:
                continue
            if any(d not in known for d in n.dependencies):
                continue
            if all(known[d].settled for d in n.dependencies):
                open_.append(n)
        return open_

    def dag_terminal(self) -> bool:
        """Every node settled. Necessary for closure, nowhere near sufficient."""
        return all(n.settled for n in self.nodes)

    def latest_generation(self) -> int:
        return max((g.generation for g in self.gates), default=0)

    def retired_gates(self) -> list[str]:
        """Gates that reported in an earlier pass and not in the latest one.

        Named rather than dropped. A gate disappearing from a closure is either
        a deliberate reconfiguration or a check that quietly stopped running,
        and those look identical from the record unless something says which
        gates went missing.
        """
        now = self.latest_generation()
        if not now:
            return []
        current_ = {g.name for g in self.gates if g.generation == now}
        earlier_ = {g.name for g in self.gates if g.generation and g.generation < now}
        return sorted(earlier_ - current_)

    def gates_green(self) -> bool:
        """Every gate of the **latest closure pass** is GREEN, and one ran.

        Restricted to the latest pass, because a gate that no longer runs is
        not evidence about the current state. Reading the latest result per
        *name* across all of history meant a renamed gate kept its final RED
        for ever -- measured here at closure generation 14, on a gate that had
        been split into four narrower ones. `retired_gates()` names what went
        missing, so a reconfiguration is visible rather than silent.

        Zero gates is not green: a closure that checked nothing has not
        established anything, and returning True for an empty set is the
        purest form of the failure this whole project is about.
        """
        now = self.latest_generation()
        # Results written before `generation` existed all carry 0; falling back
        # to the whole list keeps their behaviour exactly as it was.
        candidates = [g for g in self.gates if g.generation == now] or list(self.gates)
        last_: dict[str, GateResult] = {}
        for g in candidates:
            last_[g.name] = g
        if not last_:
            return False
        return all(g.counts_as_green for g in last_.values())

    def rc_closed(self) -> bool:
        """`DAG_TERMINAL != RC_CLOSED`, expressed once, here.

        Closure needs all three: the DAG terminal, every global gate green,
        and no repair node still outstanding from the pass that checked them.
        """
        if not self.dag_terminal():
            return False
        if not self.gates_green():
            return False
        open_ = [n for n in self.nodes if n.repair_of and not n.settled]
        return not open_

    def touch(self) -> None:
        self.updated_at = utcnow()
