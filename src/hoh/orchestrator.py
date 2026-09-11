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

        # A RUNNING node means a previous session died mid-dispatch. Whether
        # that work finished, crashed, or is still live cannot be read here.
        laufend = [n for n in state.nodes if n.lifecycle is Lifecycle.RUNNING]
        if laufend:
            grund = (
                "nodes recorded as RUNNING with no live controller: "
                + ", ".join(n.id for n in laufend)
                + " -- whether that work finished, crashed, or is still going has to be "
                "established against the run record before anything else is decided"
            )
            schritte.append(Step(0, HaltClass.AMBIGUOUS, detail=grund))
            return Result(HaltClass.AMBIGUOUS, grund, schritte)

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

            knoten.lifecycle = Lifecycle.RUNNING
            try:
                state = self._persist(state)
            except StaleWrite as exc:
                grund = f"another orchestrator advanced this project: {exc}"
                schritte.append(Step(runde, HaltClass.AMBIGUOUS, knoten.id, grund))
                return Result(HaltClass.AMBIGUOUS, grund, schritte, runde, reparaturen)
            knoten = state.node(knoten.id)          # the re-read copy
            assert knoten is not None

            ausgang = self.launcher.launch(knoten)

            if ausgang.verdict is RunVerdict.PROVIDER_UNAVAILABLE:
                knoten.lifecycle = Lifecycle.BLOCKED
                knoten.note = ausgang.detail
                state = self._persist(state)
                grund = f"node {knoten.id}: {ausgang.detail or 'provider unavailable'}"
                schritte.append(Step(runde, HaltClass.BLOCKED_PROVIDER, knoten.id, grund))
                return Result(HaltClass.BLOCKED_PROVIDER, grund, schritte, runde, reparaturen)

            if ausgang.verdict is RunVerdict.NOT_RUN:
                knoten.lifecycle = Lifecycle.READY
                state = self._persist(state)
                grund = f"dry run: node {knoten.id} was not dispatched, so nothing was established"
                schritte.append(Step(runde, HaltClass.NOT_RUN, knoten.id, grund))
                return Result(HaltClass.NOT_RUN, grund, schritte, runde, reparaturen)

            if ausgang.verdict is RunVerdict.ACCEPTED:
                gelandet = self.launcher.merge(knoten, ausgang)
                if not gelandet:
                    # Accepted but the merge did not land: exactly the state
                    # that must not be guessed at. Re-merging risks applying a
                    # candidate twice; abandoning discards verified work.
                    grund = (
                        f"node {knoten.id} was accepted but its candidate did not land; "
                        "re-merging risks applying it twice and abandoning discards "
                        "verified work, so this is not decided here"
                    )
                    knoten.lifecycle = Lifecycle.BLOCKED
                    knoten.note = grund
                    state = self._persist(state)
                    schritte.append(Step(runde, HaltClass.AMBIGUOUS, knoten.id, grund))
                    return Result(HaltClass.AMBIGUOUS, grund, schritte, runde, reparaturen)
                knoten.lifecycle = Lifecycle.MERGED
                self._record(
                    state, DecisionKind.MERGE_RELEASE,
                    f"every criterion passed and the candidate landed on {ausgang.candidate or 'the mainline'}",
                    {"node": knoten.id, "candidate": ausgang.candidate},
                )
                state = self._persist(state)
                schritte.append(Step(runde, "MERGED", knoten.id))
                continue

            if ausgang.verdict is RunVerdict.REJECTED:
                knoten.rejections += 1
                if knoten.rejections > MAX_REJECTIONS:
                    knoten.lifecycle = Lifecycle.ABANDONED
                    self._record(
                        state, DecisionKind.ABANDON_NODE,
                        f"{knoten.rejections} rejections without progress",
                        {"node": knoten.id},
                    )
                    schritte.append(Step(runde, "ABANDONED", knoten.id,
                                         f"{knoten.rejections} rejections"))
                else:
                    knoten.lifecycle = Lifecycle.READY
                    schritte.append(Step(runde, "REJECTED", knoten.id,
                                         f"attempt {knoten.rejections}"))
                state = self._persist(state)
                continue

            grund = f"node {knoten.id}: unclassifiable verdict {ausgang.verdict!r}"
            knoten.lifecycle = Lifecycle.BLOCKED
            knoten.note = grund
            state = self._persist(state)
            schritte.append(Step(runde, HaltClass.AMBIGUOUS, knoten.id, grund))
            return Result(HaltClass.AMBIGUOUS, grund, schritte, runde, reparaturen)

        grund = f"{self.max_rounds} rounds without reaching a fixpoint"
        schritte.append(Step(self.max_rounds, HaltClass.ROUND_LIMIT, detail=grund))
        return Result(HaltClass.ROUND_LIMIT, grund, schritte, self.max_rounds, reparaturen)

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
