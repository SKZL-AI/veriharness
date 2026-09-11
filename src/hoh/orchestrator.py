"""The control plane: the loop that decides which runs happen at all.

`controller.py` drives one run -- plan, develop, verify, accept or reject.
This module drives the layer above it: which runs exist, in what order, what
the global gates said about the merged result, and what to do when they say
no. That layer produced this project's own release, and until now it existed
only as an agent session's behaviour plus a prototype script outside the
package. `ProjectState`/`ProjectStore` made the state durable; this makes the
*executor* part of the product.

Three properties are deliberate, and each of them is a lesson rather than a
preference:

**Every halt has a name.** A loop that stops without saying why is
indistinguishable, from outside, from a loop that broke. The prototype's
halt classes are carried over intact (`HaltClass` below), because the count
of `AMBIGUOUS` halts over a campaign is the only honest measure of how far
the automation actually reaches.

**A round persists before it acts.** Lifecycle transitions are written
through `ProjectStore` under its lock and fencing before the next decision is
taken, so a crash between two rounds loses at most the round in flight, and
a fresh session reads the same state this one would have.

**Closure is a fixpoint, not the end of a list.** `DAG_TERMINAL` is not
`RC_CLOSED`. A global gate failure does not stop the release and is not
patched by hand: it becomes a repair node that goes through the ordinary
loop, after which closure is attempted again. Only a pass that finds every
gate green *and* creates no new repair node closes.

The execution side is behind two narrow protocols (`RunLauncher`,
`GateRunner`) so the same controller drives a real Herdr/HoH run and a
synthetic fixture. That is what makes the parity suite against the prototype
possible, and what keeps the fault-injection tests free of agent quota.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from .project import (
    ActionClass,
    DecisionKind,
    DecisionRecord,
    GateOutcome,
    GateResult,
    Lifecycle,
    ProjectState,
    TaskNode,
)
from .projectstore import ProjectStore
from .store import LockBusy, StaleWrite, StoreError


class HaltClass(StrEnum):
    """Why the loop stopped. Every exit passes through exactly one of these."""

    #: The only good ending: DAG terminal, every global gate green, no repair
    #: node outstanding from the pass that checked them.
    CLOSED = "CLOSED"
    #: The next node carries an irreversible external action. Correct and
    #: intended -- this is the one class that legitimately needs a human.
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"
    #: Provider quota, rate limit, or outage. Not a rejection: recording it as
    #: one would burn a node's retry budget on someone else's downtime.
    BLOCKED_PROVIDER = "BLOCKED_PROVIDER"
    #: A dependency that names a node which does not exist, or a node that
    #: policy holds. Nothing is runnable and the state says why.
    BLOCKED_DEPENDENCY = "BLOCKED_DEPENDENCY"
    #: The persisted state cannot be read or cannot be trusted.
    CORRUPT_STATE = "CORRUPT_STATE"
    #: Round or repair ceiling reached without a fixpoint. Not an endless spin.
    ROUND_LIMIT = "ROUND_LIMIT"
    #: A dry run: nothing was executed, so nothing was established. Never a
    #: fixpoint -- the prototype's first version returned 0 from un-run gates
    #: and reported success, which is NOT_RUN counted as passed.
    NOT_RUN = "NOT_RUN"
    #: A state or verdict the controller cannot classify. The defect class:
    #: each one is a place where a human would have to step in.
    AMBIGUOUS = "AMBIGUOUS"


#: Halt classes that mean "a person is needed", as opposed to "finished".
NEEDS_A_HUMAN = frozenset(
    {
        HaltClass.BLOCKED_EXTERNAL,
        HaltClass.BLOCKED_PROVIDER,
        HaltClass.BLOCKED_DEPENDENCY,
        HaltClass.CORRUPT_STATE,
        HaltClass.AMBIGUOUS,
    }
)


class RunVerdict(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    #: Quota or provider failure -- explicitly not a rejection.
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    #: Dispatched but its outcome is not yet determinable.
    UNDETERMINED = "UNDETERMINED"
    #: A dry run performed no dispatch.
    NOT_RUN = "NOT_RUN"


@dataclass
class RunOutcome:
    verdict: RunVerdict
    detail: str = ""
    candidate: str | None = None


class RunLauncher(Protocol):
    """How the controller reaches a single HoH run."""

    def action_class(self, node: TaskNode) -> ActionClass: ...

    def depends_on(self, a: TaskNode, b: TaskNode) -> bool:
        """Whether `a` and `b` may not proceed in parallel.

        Measured from declared writes and semantic reads, never assumed. Two
        runs with disjoint write sets can still conflict when one reads what
        the other writes -- the failure that produced this project's union
        gate.
        """
        ...

    def launch(self, node: TaskNode) -> RunOutcome: ...

    def accepted_baseline(self, node: TaskNode) -> str | None:
        """The candidate the run had already accepted, before this dispatch.

        Read before launching and written into project state, because after a
        crash it is the only thing that distinguishes "this dispatch accepted
        something" from "something was accepted earlier".
        """
        ...

    def evaluate(self, node: TaskNode) -> RunOutcome:
        """The verdict of an already-dispatched run, **without dispatching**.

        This is what makes a resume exactly-once. A fresh process that finds a
        node marked RUNNING has three possibilities in front of it -- the work
        finished, it crashed, or it is still going -- and re-dispatching to find
        out would re-run an acceptance that may already have happened. Reading
        the run's own recorded state answers the question without spending
        anything, and returns UNDETERMINED when it genuinely cannot.
        """
        ...

    def merge(self, node: TaskNode, outcome: RunOutcome) -> bool:
        """Applies an accepted candidate. Returns whether it actually landed."""
        ...


class GateRunner(Protocol):
    """How the controller reaches the global gates."""

    def subject(self) -> str:
        """The commit or tree the gates will run against."""
        ...

    def run(self, subject: str) -> list[GateResult]: ...


@dataclass
class Step:
    """One thing the controller did, for the decision log."""

    round: int
    kind: str
    node: str | None = None
    detail: str = ""


@dataclass
class Result:
    halt: HaltClass
    reason: str
    steps: list[Step] = field(default_factory=list)
    rounds: int = 0
    repairs_created: int = 0

    @property
    def ambiguous_halts(self) -> int:
        """How often the loop could not classify something.

        The measure worth tracking across a campaign: each one is a place a
        human had to be reachable.
        """
        return sum(1 for s in self.steps if s.kind == HaltClass.AMBIGUOUS)


#: Rejections a node may take before it is abandoned. A rejection is input to
#: the next iteration, not an endpoint -- but an unbounded retry is a spin.
MAX_REJECTIONS = 3


class ProjectController:
    """Drives a project to its fixpoint, persisting every transition."""

    def __init__(
        self,
        store: ProjectStore,
        launcher: RunLauncher,
        gates: GateRunner,
        *,
        max_rounds: int = 40,
        max_repairs: int = 5,
    ) -> None:
        self.store = store
        self.launcher = launcher
        self.gates = gates
        self.max_rounds = max_rounds
        self.max_repairs = max_repairs

    # -- persistence ------------------------------------------------------- #

    def _persist(self, state: ProjectState) -> ProjectState:
        """Writes and re-reads.

        Re-reading is not paranoia: `write_state` increments `write_seq` on
        the object it is given, and a controller that kept holding the
        pre-write object would fence itself out on the next round.
        """
        self.store.write_state(state)
        return self.store.read_state()

    def _record(
        self,
        state: ProjectState,
        kind: DecisionKind,
        reason: str,
        payload: dict,
        evidence: list[str] | None = None,
    ) -> None:
        """Appends a decision record.

        Everything here is a judgement that cannot be re-derived from the tree
        afterwards. Why a node was abandoned, why an edge was added, why a
        finding was dispositioned -- the tree shows the result and never the
        reason.
        """
        state.decisions.append(
            DecisionRecord(
                id=f"D{len(state.decisions) + 1:04d}",
                kind=kind,
                actor="orchestrator",
                reason=reason,
                payload=payload,
                evidence=evidence or [],
            )
        )

    # -- the loop ---------------------------------------------------------- #

    def run(self) -> Result:
        try:
            with self.store.lock():
                return self._drive()
        except LockBusy as exc:
            return Result(
                halt=HaltClass.BLOCKED_DEPENDENCY,
                reason=f"another orchestrator holds this project: {exc}",
            )
        except StaleWrite as exc:
            # Caught before StoreError, which it subclasses, because the two
            # call for opposite remedies and collapsing them hides that. A
            # stale write means the state is fine and *this session* is behind:
            # reload and the work continues. A corrupt state means the file
            # cannot be trusted and nothing should proceed until a person has
            # looked. Reporting the first as the second would send someone
            # inspecting a healthy project.
            return Result(
                halt=HaltClass.AMBIGUOUS,
                reason=(
                    f"another orchestrator advanced this project while this round was "
                    f"in flight: {exc} -- what it did cannot be read from here, so this "
                    "session stops rather than reapplying its own view"
                ),
            )
        except StoreError as exc:
            return Result(halt=HaltClass.CORRUPT_STATE, reason=str(exc))

    def _drive(self) -> Result:
        schritte: list[Step] = []
        reparaturen = 0

        try:
            state = self.store.read_state()
        except StoreError as exc:
            return Result(halt=HaltClass.CORRUPT_STATE, reason=str(exc), steps=schritte)

        # A node whose dependency names something that does not exist reads,
        # to a scheduler, exactly like a node whose dependency is satisfied.
        unbekannt = state.unknown_dependencies()
        if unbekannt:
            grund = "dependencies naming nodes that do not exist: " + "; ".join(
                f"{k} -> {v}" for k, v in sorted(unbekannt.items())
            )
            schritte.append(Step(0, HaltClass.BLOCKED_DEPENDENCY, detail=grund))
            return Result(HaltClass.BLOCKED_DEPENDENCY, grund, schritte)

        # A RUNNING node means a previous session died mid-dispatch. Rather
        # than halting on principle, ask the run's own recorded state what
        # happened -- that is a read, costs nothing, and is the only way a
        # resume can be exactly-once. Re-dispatching to find out would re-run
        # an acceptance that may already have happened.
        laufend = [n for n in state.nodes if n.lifecycle is Lifecycle.RUNNING]
        for n in laufend:
            ausgang = self.launcher.evaluate(n)
            schritte.append(Step(0, "EVALUATED", n.id, f"{ausgang.verdict}: {ausgang.detail}"))
            if ausgang.verdict is RunVerdict.UNDETERMINED:
                grund = (
                    f"node {n.id} is recorded as RUNNING and its run state does not say "
                    f"what happened: {ausgang.detail}. Re-dispatching could repeat an "
                    "acceptance that already landed, so this is not decided here"
                )
                schritte.append(Step(0, HaltClass.AMBIGUOUS, n.id, grund))
                return Result(HaltClass.AMBIGUOUS, grund, schritte)
            state = self._settle(state, n.id, ausgang, 0, schritte)
            if isinstance(state, Result):
                state.steps = schritte
                return state

        for runde in range(1, self.max_rounds + 1):
            bereit = state.ready()

            if not bereit:
                ergebnis, state, neue = self._closure(state, runde, schritte, reparaturen)
                reparaturen = neue
                if ergebnis is not None:
                    ergebnis.steps = schritte
                    ergebnis.rounds = runde
                    ergebnis.repairs_created = reparaturen
                    return ergebnis
                continue

            knoten = bereit[0]

            if self.launcher.action_class(knoten) is ActionClass.EXTERNAL:
                grund = (
                    f"node {knoten.id} carries an irreversible external action; "
                    "that decision is not delegated to the orchestrator"
                )
                schritte.append(Step(runde, HaltClass.BLOCKED_EXTERNAL, knoten.id, grund))
                return Result(HaltClass.BLOCKED_EXTERNAL, grund, schritte, runde, reparaturen)

            # Serialise only where a dependency is actually measured. A blanket
            # rule would be safe and would also throw away every parallel round.
            for anderer in bereit[1:]:
                if self.launcher.depends_on(knoten, anderer):
                    schritte.append(
                        Step(runde, "SERIALISED", f"{knoten.id}<->{anderer.id}",
                             "measured semantic dependency")
                    )

            # Before spending anything, ask whether this node's run has
            # *already* accepted something newer than the baseline this node
            # carries. It can have: a dispatch that succeeded and whose merge
            # then failed leaves exactly that state, and re-dispatching would
            # pay again for work that is already verified -- and could accept a
            # second, different candidate for the same node.
            #
            # Found by the first real end-to-end run: the candidate was
            # accepted, the merge was blocked by untracked build output, and
            # after the obstruction was cleared the node was READY again with
            # its acceptance still sitting in the run record.
            vorab = self.launcher.evaluate(knoten)
            if vorab.verdict is RunVerdict.ACCEPTED:
                schritte.append(Step(runde, "ALREADY_ACCEPTED", knoten.id, vorab.detail))
                ergebnis = self._settle(state, knoten.id, vorab, runde, schritte)
                if isinstance(ergebnis, Result):
                    ergebnis.steps = schritte
                    ergebnis.rounds = runde
                    ergebnis.repairs_created = reparaturen
                    return ergebnis
                state = ergebnis
                continue

            knoten.lifecycle = Lifecycle.RUNNING
            # The baseline goes into state before the dispatch, not after: it
            # is what a fresh process needs in order to tell an acceptance from
            # a run that ended where it started, and a process that dies during
            # the dispatch is exactly when it is needed.
            knoten.accepted_before = self.launcher.accepted_baseline(knoten)
            try:
                state = self._persist(state)
            except StaleWrite as exc:
                grund = f"another orchestrator advanced this project: {exc}"
                schritte.append(Step(runde, HaltClass.AMBIGUOUS, knoten.id, grund))
                return Result(HaltClass.AMBIGUOUS, grund, schritte, runde, reparaturen)
            knoten = state.node(knoten.id)          # the re-read copy
            assert knoten is not None

            ausgang = self.launcher.launch(knoten)
            ergebnis = self._settle(state, knoten.id, ausgang, runde, schritte)
            if isinstance(ergebnis, Result):
                ergebnis.steps = schritte
                ergebnis.rounds = runde
                ergebnis.repairs_created = reparaturen
                return ergebnis
            state = ergebnis
            continue

        grund = f"{self.max_rounds} rounds without reaching a fixpoint"
        schritte.append(Step(self.max_rounds, HaltClass.ROUND_LIMIT, detail=grund))
        return Result(HaltClass.ROUND_LIMIT, grund, schritte, self.max_rounds, reparaturen)

    def _settle(
        self, state: ProjectState, node_id: str, ausgang: RunOutcome,
        runde: int, schritte: list[Step],
    ) -> "ProjectState | Result":
        """Turns one run outcome into a persisted lifecycle transition.

        Shared by the main loop and the resume path on purpose. Two copies of
        this would be two chances for a resumed session to decide differently
        from the session it is resuming -- and "differently" here means merging
        twice or discarding verified work.
        """
        knoten = state.node(node_id)
        assert knoten is not None

        if ausgang.verdict is RunVerdict.PROVIDER_UNAVAILABLE:
            knoten.lifecycle = Lifecycle.BLOCKED
            knoten.note = ausgang.detail
            self._persist(state)
            grund = f"node {node_id}: {ausgang.detail or 'provider unavailable'}"
            schritte.append(Step(runde, HaltClass.BLOCKED_PROVIDER, node_id, grund))
            return Result(HaltClass.BLOCKED_PROVIDER, grund)

        if ausgang.verdict is RunVerdict.NOT_RUN:
            knoten.lifecycle = Lifecycle.READY
            self._persist(state)
            grund = f"dry run: node {node_id} was not dispatched, so nothing was established"
            schritte.append(Step(runde, HaltClass.NOT_RUN, node_id, grund))
            return Result(HaltClass.NOT_RUN, grund)

        if ausgang.verdict is RunVerdict.ACCEPTED:
            gelandet = self.launcher.merge(knoten, ausgang)
            if not gelandet:
                # Accepted but the merge did not land: exactly the state that
                # must not be guessed at. Re-merging risks applying a candidate
                # twice; abandoning discards verified work.
                grund = (
                    f"node {node_id} was accepted but its candidate did not land; "
                    "re-merging risks applying it twice and abandoning discards "
                    "verified work, so this is not decided here"
                )
                knoten.lifecycle = Lifecycle.BLOCKED
                knoten.note = grund
                self._persist(state)
                schritte.append(Step(runde, HaltClass.AMBIGUOUS, node_id, grund))
                return Result(HaltClass.AMBIGUOUS, grund)
            knoten.lifecycle = Lifecycle.MERGED
            self._record(
                state, DecisionKind.MERGE_RELEASE,
                f"every criterion passed and the candidate landed on "
                f"{ausgang.candidate or 'the mainline'}",
                {"node": node_id, "candidate": ausgang.candidate},
            )
            neu_state = self._persist(state)
            schritte.append(Step(runde, "MERGED", node_id))
            return neu_state

        if ausgang.verdict is RunVerdict.REJECTED:
            knoten.rejections += 1
            if knoten.rejections > MAX_REJECTIONS:
                knoten.lifecycle = Lifecycle.ABANDONED
                self._record(
                    state, DecisionKind.ABANDON_NODE,
                    f"{knoten.rejections} rejections without progress",
                    {"node": node_id},
                )
                schritte.append(Step(runde, "ABANDONED", node_id, f"{knoten.rejections} rejections"))
            else:
                knoten.lifecycle = Lifecycle.READY
                schritte.append(Step(runde, "REJECTED", node_id, f"attempt {knoten.rejections}"))
            return self._persist(state)

        grund = f"node {node_id}: unclassifiable verdict {ausgang.verdict!r}"
        knoten.lifecycle = Lifecycle.BLOCKED
        knoten.note = grund
        self._persist(state)
        schritte.append(Step(runde, HaltClass.AMBIGUOUS, node_id, grund))
        return Result(HaltClass.AMBIGUOUS, grund)

    # -- global closure ---------------------------------------------------- #

    def _closure(
        self, state: ProjectState, runde: int, schritte: list[Step], reparaturen: int
    ) -> tuple[Result | None, ProjectState, int]:
        """One closure attempt. Returns a Result only when the loop must stop."""
        if not state.dag_terminal():
            # Nothing runnable and not terminal: something is blocked or
            # contingent, and the state does not say what to do next.
            blockiert = [n.id for n in state.nodes if n.lifecycle is Lifecycle.BLOCKED]
            grund = (
                "no node is runnable and the DAG is not terminal"
                + (f"; blocked: {', '.join(blockiert)}" if blockiert else "")
            )
            schritte.append(Step(runde, HaltClass.BLOCKED_DEPENDENCY, detail=grund))
            return Result(HaltClass.BLOCKED_DEPENDENCY, grund), state, reparaturen

        subjekt = self.gates.subject()
        ergebnisse = self.gates.run(subjekt)

        if any(g.outcome is GateOutcome.NOT_RUN for g in ergebnisse) and not any(
            g.outcome is GateOutcome.RED for g in ergebnisse
        ):
            # A dry run, or gates that could not execute. Not a fixpoint, and
            # explicitly not green.
            grund = (
                "global gates did not execute (NOT_RUN); a closure that checked "
                "nothing has established nothing"
            )
            state.gates.extend(ergebnisse)
            state = self._persist(state)
            schritte.append(Step(runde, HaltClass.NOT_RUN, detail=grund))
            return Result(HaltClass.NOT_RUN, grund), state, reparaturen

        state.gates.extend(ergebnisse)
        state.measurement_head = subjekt
        state.closure_generation += 1
        state = self._persist(state)

        if state.rc_closed():
            grund = (
                f"DAG terminal, every global gate green at {subjekt}, no outstanding "
                f"repair node; closure generation {state.closure_generation}"
            )
            self._record(state, DecisionKind.CLOSURE_VERDICT, grund,
                         {"subject": subjekt, "generation": state.closure_generation})
            state = self._persist(state)
            schritte.append(Step(runde, HaltClass.CLOSED, detail=grund))
            return Result(HaltClass.CLOSED, grund), state, reparaturen

        rot = [g for g in ergebnisse if g.outcome is GateOutcome.RED]
        if not rot:
            # Terminal, gates green, yet not closed: a repair node is still
            # outstanding. It will be picked up as READY on the next round.
            offen = [n.id for n in state.nodes if n.repair_of and not n.settled]
            grund = f"gates green but repair nodes outstanding: {', '.join(offen)}"
            schritte.append(Step(runde, "CLOSURE_PENDING", detail=grund))
            return None, state, reparaturen

        if reparaturen >= self.max_repairs:
            grund = (
                f"repair ceiling of {self.max_repairs} reached and the global gates are "
                f"still red: {', '.join(g.name for g in rot)}"
            )
            schritte.append(Step(runde, HaltClass.ROUND_LIMIT, detail=grund))
            return Result(HaltClass.ROUND_LIMIT, grund), state, reparaturen

        reparaturen += 1
        vorgaenger = [n.id for n in state.nodes if n.lifecycle is Lifecycle.MERGED]
        knoten = TaskNode(
            id=f"repair-{state.closure_generation}-{reparaturen}",
            lifecycle=Lifecycle.READY,
            action_class=ActionClass.INTERNAL,
            dependencies=vorgaenger[-1:],
            repair_of=vorgaenger[-1] if vorgaenger else None,
            closure_generation=state.closure_generation,
            note="; ".join(f"{g.name}: {g.detail or 'red'}" for g in rot),
        )
        state.nodes.append(knoten)
        self._record(
            state, DecisionKind.CREATE_REPAIR_NODE,
            f"global gates red at {subjekt}: {', '.join(g.name for g in rot)}",
            {"node": knoten.id, "gates": [g.name for g in rot], "subject": subjekt},
            evidence=[f"gate:{g.name}@{g.subject}" for g in rot],
        )
        state = self._persist(state)
        schritte.append(Step(runde, "REPAIR_CREATED", knoten.id, knoten.note))
        return None, state, reparaturen
