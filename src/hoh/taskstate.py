"""Compact task state (V3.3 P2-13): Layer 0 of the context capsule.

A fresh agent, a resumed controller or a successor session needs to know where
a long-running programme stands without being handed its whole history:

    Layer 0  compact low-resolution context        <- this module
    Layer 1  relevant decisions / evidence
    Layer 2  full source available on demand

The property this module exists for: a compact state is a **projection that
cannot lie by omission**. It is smaller than the history, so it is explicit
about what it left out (`omissions`, `truncated`), about the whole programme
rather than the excerpt (`counts`, always over every node) and about how
current it is (`source_digests`, `program_generation`, `stale_against`).

Everything here is pure: the nodes arrive as in-memory records, nothing is
read from the run store, the ledger, git or the filesystem, inputs are never
mutated and equal inputs give an equal `CompactState`.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

from hoh.contracts import Strict
from hoh.project import Lifecycle, ProjectState, TaskNode, digest_obj


class Health(StrEnum):
    """How a node stands.

    WAITING_FOR_DECISION is its own member, not a kind of BLOCKED: the plan's
    rule is BLOCKED(run) != BLOCKED(project), and a node waiting on an
    unanswered question is not a node whose run broke.
    """

    READY = "READY"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    WAITING_FOR_DECISION = "WAITING_FOR_DECISION"
    DONE = "DONE"
    FAILED = "FAILED"


#: Attention order: what a reader must see first comes first. A summary that
#: truncated alphabetically and dropped the failures would be worse than none.
ATTENTION_ORDER: tuple[Health, ...] = (
    Health.FAILED,
    Health.BLOCKED,
    Health.WAITING_FOR_DECISION,
    Health.RUNNING,
    Health.READY,
    Health.DONE,
)
_RANK = {h: i for i, h in enumerate(ATTENTION_ORDER)}

#: States that *need attention* and are therefore never dropped by the budget.
#: The spec leaves open whether WAITING_FOR_DECISION belongs here; this module
#: says yes. A question nobody knows is pending is never answered, so hiding
#: one is the same lie by omission as hiding a failure -- only the reason
#: differs (the project waits, the run did not break).
NEEDS_ATTENTION = frozenset({Health.FAILED, Health.BLOCKED, Health.WAITING_FOR_DECISION})

#: States that must carry a `blocked_reason`.
_NEEDS_REASON = frozenset({Health.FAILED, Health.BLOCKED})

#: Omission kinds.
DROPPED = "dropped"
BUDGET_EXCEEDED = "budget_exceeded"


class NodeLine(Strict):
    """One line per node, deliberately small.

    A BLOCKED or FAILED line without a `blocked_reason` is refused: a blocked
    node nobody can act on is the omission this component exists to prevent.
    Frozen, so a line inside a frozen `CompactState` cannot be edited either.
    """

    model_config = ConfigDict(frozen=True)

    node_id: str
    title: str
    health: Health
    attempts: int = Field(default=0, ge=0)
    #: ISO-8601 timestamp of the last event, or None if there was none.
    last_event_at: str | None = None
    blocked_reason: str | None = None
    waiting_on: str | None = None

    @field_validator("node_id")
    @classmethod
    def _id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("node_id must not be blank")
        return value

    @model_validator(mode="after")
    def _blocked_needs_reason(self) -> NodeLine:
        if self.health in _NEEDS_REASON and not (self.blocked_reason or "").strip():
            raise ValueError(
                f"node {self.node_id} is {self.health.value} but names no blocked_reason; "
                "a blocked node nobody can act on is refused")
        return self


class Omission(Strict):
    """What was left out of a compact state, and why.

    kind `dropped`: `count` nodes were left out of `lines`.
    kind `budget_exceeded`: `count` lines more than `max_lines` were kept,
    because they could not be dropped.
    """

    model_config = ConfigDict(frozen=True)

    kind: str
    count: int = Field(ge=1)
    why: str

    @field_validator("kind", "why")
    @classmethod
    def _not_blank(cls, value: str, info) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must not be blank")
        return value


def _frozen_map(value: Mapping) -> MappingProxyType:
    return MappingProxyType(dict(value))


class CompactState(Strict):
    """A frozen projection of a programme's task state.

    `counts` maps every `Health` to the **total** number of nodes in that
    state before any truncation, so the numbers are about the programme and
    not about the excerpt. `state_digest` covers the generation, `as_of`, the
    ordered (node_id, health) pairs and the counts -- not the prose.

    Refused at construction: a state whose lines plus dropped omissions do not
    add up to its counts (something vanished without a record), a truncated
    state without a `dropped` omission, an untruncated one with one, and a
    `state_digest` that is not the digest of what the state shows.
    """

    model_config = ConfigDict(frozen=True)

    program_id: str
    program_generation: int
    as_of: str
    lines: tuple[NodeLine, ...]
    omissions: tuple[Omission, ...] = ()
    counts: Mapping[Health, int]
    source_digests: Mapping[str, str] = Field(default_factory=dict)
    state_digest: str
    truncated: bool

    @field_validator("counts", mode="after")
    @classmethod
    def _counts_cover_every_health(cls, value: Mapping[Health, int]) -> MappingProxyType:
        full = {h: int(value.get(h, 0)) for h in Health}
        if any(n < 0 for n in full.values()):
            raise ValueError("counts must not be negative")
        return _frozen_map(full)

    @field_validator("source_digests", mode="after")
    @classmethod
    def _freeze_sources(cls, value: Mapping[str, str]) -> MappingProxyType:
        return _frozen_map(value)

    @field_serializer("counts", "source_digests")
    def _serialize_map(self, value: Mapping) -> dict:
        return {str(k): v for k, v in value.items()}

    @model_validator(mode="after")
    def _cannot_lie_by_omission(self) -> CompactState:
        dropped = sum(o.count for o in self.omissions if o.kind == DROPPED)
        total = sum(self.counts.values())
        if len(self.lines) + dropped != total:
            raise ValueError(
                f"{len(self.lines)} line(s) and {dropped} recorded as dropped do not add up "
                f"to the {total} node(s) in counts; a reduction without an omission is refused")
        if self.truncated != (dropped > 0):
            raise ValueError(
                "truncated must be true exactly when a `dropped` omission is recorded "
                f"(truncated={self.truncated}, dropped={dropped})")
        if self.state_digest != self.compute_digest():
            raise ValueError("state_digest does not match the generation, as_of, lines and counts")
        return self

    def compute_digest(self) -> str:
        return state_digest(self.program_generation, self.as_of, self.lines, self.counts)


def state_digest(generation: int, as_of: str, lines: Iterable[NodeLine],
                 counts: Mapping[Health, int]) -> str:
    """sha256 over canonical JSON of generation, as_of, ordered (id, health) and counts.

    Titles, reasons and omission prose are left out on purpose: rewording a
    line is not a change of state, a health moving is.
    """
    body = {
        "generation": generation,
        "as_of": as_of,
        "nodes": [[line.node_id, line.health.value] for line in lines],
        "counts": {h.value: int(counts.get(h, 0)) for h in Health},
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# compact
# --------------------------------------------------------------------------- #


def compact(
    nodes: Iterable[NodeLine],
    *,
    program_id: str,
    generation: int,
    as_of: str,
    max_lines: int,
    keep: Iterable[str] = (),
    source_digests: Mapping[str, str] | None = None,
) -> CompactState:
    """Project `nodes` onto at most `max_lines` lines -- except where that would lie.

    **A budget cannot suppress a failure.** Every FAILED, BLOCKED and
    WAITING_FOR_DECISION node (the states that need attention; see
    `NEEDS_ATTENTION` for why a pending decision counts) and every node named
    in `keep` is kept whatever `max_lines` says. If those alone exceed the
    budget, the overrun is recorded as an `Omission` of kind
    `budget_exceeded` instead of dropping one of them.

    Rules:

    1. `counts` is computed over all nodes, before any truncation.
    2. Lines are in attention order -- FAILED, BLOCKED, WAITING_FOR_DECISION,
       RUNNING, READY, DONE -- stable within one state (input order).
    3. Node ids in `keep` are never dropped.
    4. The remaining budget is filled in attention order; each health state
       that loses nodes gets one `dropped` Omission naming how many and why,
       and `truncated` is true. Nothing dropped means no `dropped` omission
       and `truncated` false. A `budget_exceeded` omission can appear on its
       own: then nothing was dropped, but the budget was not honoured, and
       the reader is told so.
    5. `max_lines` of zero or less raises ValueError.

    Duplicate node ids and `keep` ids that name no node raise ValueError: the
    first would make counts disagree with what a reader can look up, the
    second is a request to keep something this state cannot show.
    """
    if max_lines <= 0:
        raise ValueError(f"max_lines must be at least 1, got {max_lines}")

    lines = [n if isinstance(n, NodeLine) else NodeLine.model_validate(n) for n in nodes]
    duplicates = sorted(i for i, n in Counter(l.node_id for l in lines).items() if n > 1)
    if duplicates:
        raise ValueError(f"duplicate node_id(s): {', '.join(duplicates)}")
    keep_ids = set(keep)
    unknown = sorted(keep_ids - {l.node_id for l in lines})
    if unknown:
        raise ValueError(f"keep names node(s) that were not handed in: {', '.join(unknown)}")

    # Rule 1: counts over everything, before any truncation.
    tally = Counter(l.health for l in lines)
    counts = {h: tally.get(h, 0) for h in Health}

    # Rule 2: attention order, stable.
    ordered = sorted(lines, key=lambda l: _RANK[l.health])

    # Rules 3 and 5: what can never be dropped.
    must = {l.node_id for l in ordered
            if l.health in NEEDS_ATTENTION or l.node_id in keep_ids}
    room = max_lines - len(must)
    chosen = set(must)
    for line in ordered:
        if room <= 0:
            break
        if line.node_id not in chosen:
            chosen.add(line.node_id)
            room -= 1

    kept = tuple(l for l in ordered if l.node_id in chosen)
    omissions: list[Omission] = []
    if len(must) > max_lines:
        overrun = len(must) - max_lines
        held = Counter(l.health.value for l in ordered if l.node_id in must)
        detail = ", ".join(f"{n} {h}" for h, n in held.items())
        omissions.append(Omission(
            kind=BUDGET_EXCEEDED, count=overrun,
            why=(f"budget_exceeded: max_lines={max_lines} but {len(must)} line(s) cannot be "
                 f"dropped ({detail}); {overrun} line(s) over budget rather than hide them")))
    for health in ATTENTION_ORDER:
        dropped = [l.node_id for l in ordered
                   if l.health is health and l.node_id not in chosen]
        if dropped:
            omissions.append(Omission(
                kind=DROPPED, count=len(dropped),
                why=(f"dropped {len(dropped)} of {counts[health]} {health.value} node(s) "
                     f"to fit max_lines={max_lines}")))

    truncated = any(o.kind == DROPPED for o in omissions)
    return CompactState(
        program_id=program_id,
        program_generation=generation,
        as_of=as_of,
        lines=kept,
        omissions=tuple(omissions),
        counts=counts,
        source_digests=dict(source_digests or {}),
        state_digest=state_digest(generation, as_of, kept, counts),
        truncated=truncated,
    )


# --------------------------------------------------------------------------- #
# from_project -- the product's own source of nodes
# --------------------------------------------------------------------------- #

#: How many lines `from_project` keeps unless told otherwise. A budget over lines,
#: not a promise about size; FAILED, BLOCKED and WAITING_FOR_DECISION nodes are
#: kept past it (see `compact`).
DEFAULT_MAX_LINES = 40

#: A compact state is small by construction, so free text from a node is clipped:
#: a title to TITLE_MAX characters, a reason or a wait to DETAIL_MAX. Whitespace
#: runs (newlines included) become single spaces, so a line stays one line.
TITLE_MAX = 120
DETAIL_MAX = 200

#: What a project's compact state covers, said wherever it is printed. It is a
#: projection of the nodes: gates are judged by the verdict, the reason and
#: `gates_green` beside it, and a project state has no failed node, so the FAILED
#: count is 0 by construction.
PROJECT_SCOPE = ("nodes only; gates are judged by the verdict, the reason and "
                 "gates_green, and a project state has no failed node (FAILED is 0 here)")

#: The project lifecycles that have no `Health` of their own. They are mapped to
#: the nearest member and the original word is put in front of the line's title,
#: so a *kept* line still says which it was. The counts do not: once lines are
#: dropped, DONE no longer separates merged from abandoned.
_COLLAPSED = frozenset({Lifecycle.MERGED, Lifecycle.ABANDONED, Lifecycle.CONTINGENT})


def _clip(text: str, limit: int) -> str:
    one_line = " ".join(str(text).split())
    return one_line if len(one_line) <= limit else one_line[:limit - 3] + "..."


def line_for_node(node: TaskNode, *, open_dependencies: Iterable[str] = ()) -> NodeLine:
    """One `TaskNode` of a project's state as one `NodeLine`.

    READY and RUNNING map to themselves; MERGED and ABANDONED to DONE (settled);
    CONTINGENT -- a node that only becomes real if an earlier result turns out a
    particular way -- to WAITING_FOR_DECISION, because READY would invite a
    dispatch of something that may never be real and the attention states are
    never dropped. A BLOCKED node whose `blocked_kind` is "human" waits for a
    decision; every other BLOCKED node (a usage limit, a capability violation,
    an unclassified block) stays BLOCKED and names its kind. A READY node that
    still has `open_dependencies` (unsettled, or naming no node) is not runnable
    -- `ProjectState.ready()` excludes it -- so its line says so: the title ends
    in "(dependency open)" and `waiting_on` names them. Nothing in a project's
    state is a FAILED node, so none is produced. A lifecycle this mapping does
    not know raises ValueError: a new lifecycle must be mapped on purpose, not
    read as BLOCKED by default.
    """
    life = node.lifecycle
    base = (node.note or "").strip() or node.id
    title = _clip(f"{life.value}: {base}" if life in _COLLAPSED else base, TITLE_MAX)
    reason = waiting = None
    if life is Lifecycle.READY:
        health = Health.READY
        pending = list(open_dependencies)
        if pending:
            title += " (dependency open)"
            waiting = _clip(", ".join(pending), DETAIL_MAX)
    elif life is Lifecycle.RUNNING:
        health = Health.RUNNING
    elif life in (Lifecycle.MERGED, Lifecycle.ABANDONED):
        health = Health.DONE
    elif life is Lifecycle.CONTINGENT:
        health = Health.WAITING_FOR_DECISION
        waiting = "the result of an earlier node (contingent)"
    elif life is Lifecycle.BLOCKED and node.blocked_kind == "human":
        health = Health.WAITING_FOR_DECISION
        waiting = _clip((node.note or "").strip() or "a human decision", DETAIL_MAX)
    elif life is Lifecycle.BLOCKED:
        health = Health.BLOCKED
        text = node.blocked_kind or "unclassified"
        if node.retry_after:
            text += f", retry after {node.retry_after}"
        if (node.note or "").strip():
            text += f": {node.note.strip()}"
        reason = _clip(text, DETAIL_MAX)
    else:
        raise ValueError(f"node {node.id}: lifecycle {life!r} has no compact mapping")
    return NodeLine(node_id=node.id, title=title, health=health, attempts=node.rejections,
                    blocked_reason=reason, waiting_on=waiting)


def lines_for_state(state: ProjectState) -> list[NodeLine]:
    """Every node of a project's state as a line, each READY node told which of
    its dependencies are still open (a dependency naming no node is open)."""
    known = {n.id: n for n in state.nodes}
    lines = []
    for node in state.nodes:
        pending = [d if d in known else f"{d} (missing)"
                   for d in node.dependencies if d not in known or not known[d].settled]
        lines.append(line_for_node(node, open_dependencies=pending))
    return lines


def from_project(state: ProjectState, *, max_lines: int = DEFAULT_MAX_LINES) -> CompactState:
    """The compact task state of a project: Layer 0 for a session that holds
    nothing but the persisted state.

    Pure: the state is handed in, nothing is read or written. The programme
    generation is the state's, `as_of` is its `updated_at`, and the one source
    it names is a digest of every node it was built from (the nodes only: the
    gates, the decisions and any activity log are not part of it), so a reader
    holding the state can tell whether this projection is still of its nodes.
    """
    return compact(
        lines_for_state(state), program_id=state.project_id,
        generation=state.program_generation, as_of=state.updated_at, max_lines=max_lines,
        source_digests={"project-nodes": digest_obj(
            [n.model_dump(mode="json") for n in state.nodes])})


# --------------------------------------------------------------------------- #
# stale_against
# --------------------------------------------------------------------------- #


def stale_against(
    state: CompactState,
    *,
    current: Mapping[str, str],
    current_generation: int | None = None,
) -> list[str]:
    """Why `state` may not be relied on; `[]` when it is current.

    `current` maps each source name to its digest now. The programme's
    generation now is passed as `current_generation`. Reported:

    - a source whose digest differs from `current`;
    - a source the state names that is **missing** from `current` --
      absence is not agreement;
    - a source in `current` the state does not name -- the state never saw it;
    - a `program_generation` that differs from `current_generation`;
    - a `current_generation` that was not supplied -- fail closed: a check
      that was not made is not a generation that did not move.
    """
    reasons: list[str] = []
    for name, digest in sorted(state.source_digests.items()):
        if name not in current:
            reasons.append(f"source {name} is missing from current; absence is not agreement")
        elif current[name] != digest:
            reasons.append(
                f"source {name} changed: state has {digest}, current is {current[name]}")
    for name in sorted(set(current) - set(state.source_digests)):
        reasons.append(f"source {name} is in current but not in this state")
    if current_generation is None:
        reasons.append(
            "current_generation not supplied; cannot confirm the generation "
            f"{state.program_generation} has not moved")
    elif current_generation != state.program_generation:
        reasons.append(
            f"program generation moved: state is generation {state.program_generation}, "
            f"current is {current_generation}")
    return reasons


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #


def render(state: CompactState) -> str:
    """The human, line-oriented form. The omissions come last and are never left out."""
    out = [
        f"programme {state.program_id} · generation {state.program_generation} "
        f"· as of {state.as_of}"
        + (" · TRUNCATED" if state.truncated else ""),
        "counts (all nodes, before truncation): "
        + ", ".join(f"{h.value}={state.counts[h]}" for h in ATTENTION_ORDER),
        f"lines ({len(state.lines)}):",
    ]
    for line in state.lines:
        text = f"  [{line.health.value}] {line.node_id} {line.title} (attempts {line.attempts}"
        if line.last_event_at:
            text += f", last event {line.last_event_at}"
        text += ")"
        if line.blocked_reason:
            text += f" -- reason: {line.blocked_reason}"
        if line.waiting_on:
            text += f" -- waiting on: {line.waiting_on}"
        out.append(text)
    out.append("omissions:")
    if state.omissions:
        out.extend(f"  {o.kind} ({o.count}): {o.why}" for o in state.omissions)
    else:
        out.append("  none")
    return "\n".join(out) + "\n"
