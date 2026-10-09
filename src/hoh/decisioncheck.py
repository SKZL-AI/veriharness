"""The decision graph fsck (V3.3 P2-21): structure, not truth.

    A long-lived planning graph can drift even when each local edit seems valid.

`check` is a consistency validation over a sequence of `DecisionMap`s,
conceptually like a filesystem check. It does not replace candidate checks, the
union gate, global closure or evidence validation: it validates **structure**,
and its own result is a receipt -- `CheckResult` binds the verdict to the
graph's generation, the graph's digest and the validator's version.

Two rules the whole module turns on:

* **Warnings do not silently become failures.** `CheckResult.status` is computed
  from `errors` alone, it cannot be handed in, and a validator refuses a
  WARNING finding in `errors` (or an ERROR in `warnings`). No code path in
  `check` moves a warning into the error list.
* **An impossible check is recorded, not omitted.** `decisions.py` refuses
  several malformed graphs at construction, so a `DecisionMap` cannot carry
  them. Those checks are listed in `ENFORCED_AT_CONSTRUCTION` with the
  validator that makes them unreachable; everything `check` itself can emit is
  in `CHECK_CODES`. A check in neither is a hole.

Decision ids are global across the sequence handed in: the same id in two maps
is the same question, which is what makes the cross-map checks meaningful.

Pure: no filesystem, git or network access. The graph and the clock are handed
in. The command surface is `hoh decision check` in `hoh.cli`, which reads the
files and may write the receipt; nothing here does.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, computed_field, field_validator, model_validator

from hoh.decisions import (
    UNRESOLVED,
    Checked,
    DecisionMap,
    DecisionNode,
    DecisionStatus,
    ResolutionRecord,
    graduation_blockers,
)

#: Bound into every receipt (`CheckResult.validator_version`) by `hoh decision check`.
#: Bump it when a check is added, removed or changes what it reports.
VALIDATOR_VERSION = "decisioncheck/1"

#: `unusually-large-map` fires above this many nodes (while any is unresolved).
LARGE_MAP_THRESHOLD = 50

#: A source ref is *non-primary* when it starts with one of these prefixes: it
#: points at a restatement (a summary, a derived artefact, a chat log, a model's
#: output) rather than at the thing itself. Any other ref counts as primary.
NON_PRIMARY_PREFIXES = ("secondary:", "summary:", "derived:", "chat:", "llm:")

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class Severity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"


#: Every finding code `check` can emit, with its severity.
_CODE_SEVERITY: dict[str, Severity] = {
    "duplicate-map-id": Severity.ERROR,
    "cross-map-blocker-unresolved": Severity.ERROR,
    "resolution-id-mismatch": Severity.ERROR,
    "resolution-digest-mismatch": Severity.ERROR,
    "resolution-without-source": Severity.ERROR,
    "out-of-scope-blocks-active": Severity.ERROR,
    "out-of-scope-as-provenance": Severity.ERROR,
    "lease-expired-treated-active": Severity.WARNING,
    "two-valid-leases": Severity.ERROR,
    "fog-shadows-decision": Severity.WARNING,
    "fog-clears-with-missing": Severity.ERROR,
    "undermined-shown-as-settled": Severity.ERROR,
    "graduated-with-blockers": Severity.ERROR,
    "program-ref-unknown-map": Severity.ERROR,
    "unresolved-as-settled": Severity.ERROR,
    "unusually-large-map": Severity.WARNING,
    "resolution-lacks-primary-source": Severity.WARNING,
}

CHECK_CODES: frozenset[str] = frozenset(_CODE_SEVERITY)

#: Plan checks that `decisions.py` makes unreachable: a `DecisionMap` carrying
#: the condition cannot be constructed, so `check` never sees one. Each note
#: names the validator that refuses it.
ENFORCED_AT_CONSTRUCTION: dict[str, str] = {
    "duplicate-decision-id":
        "DecisionMap._structural_rules refuses duplicate decision ids within a map",
    "duplicate-fog-id":
        "DecisionMap._structural_rules refuses duplicate fog ids within a map",
    "decision-fog-id-clash":
        "DecisionMap._structural_rules refuses one id used for a decision and a fog patch",
    "self-block":
        "DecisionNode._structural_rules refuses a node listing itself in blocked_by",
    "dangling-blocked-by":
        "DecisionMap._structural_rules refuses a blocked_by id the map does not contain",
    "dangling-clears-with":
        "DecisionMap._structural_rules refuses a clears_with id the map does not contain",
    "dependency-cycle":
        "DecisionMap._structural_rules refuses a cycle in blocked_by (via _cycle)",
    "resolved-without-record":
        "DecisionNode._structural_rules refuses RESOLVED without a ResolutionRecord",
    "claimed-without-lease":
        "DecisionNode._structural_rules refuses CLAIMED without claimed_by and claim_expires_at",
    "claim-fields-without-claim":
        "DecisionNode._structural_rules refuses claim fields on a node that is not CLAIMED",
    "naive-claim-expiry":
        "DecisionNode._structural_rules refuses a claim_expires_at without a timezone",
}


class Finding(Checked):
    """One structural defect. `code` is a stable slug a caller can act on."""

    code: str
    severity: Severity
    message: str
    decision_ids: list[str] = Field(default_factory=list)
    fog_ids: list[str] = Field(default_factory=list)
    map_ids: list[str] = Field(default_factory=list)

    @field_validator("code")
    @classmethod
    def _code_is_slug(cls, value: str) -> str:
        if not _SLUG.match(value):
            raise ValueError(f"finding code {value!r} is not a lowercase slug")
        return value

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str) -> str:
        # `Checked` strips whitespace first, so a whitespace-only message lands here empty.
        if not value:
            raise ValueError("a finding must say what is wrong: message must not be blank")
        return value


class ProgramRef(Checked):
    """How a programme refers to a decision map: graduated or not, and on what premises."""

    program_id: str
    map_id: str
    graduated: bool = False
    #: The execution scope the map graduated into; handed to `graduation_blockers`.
    scope_refs: list[str] = Field(default_factory=list)
    #: Decision ids the programme treats as settled premises.
    settled_premises: list[str] = Field(default_factory=list)


class CheckResult(Checked):
    """The receipt of one `check` run.

    `status` is derived from `errors` alone -- PASS iff there are none -- and is
    not a field, so it cannot be handed in (`extra="forbid"` refuses it). It is
    still part of `model_dump()`. `checked_generation` is the highest
    `generation` over the maps checked, 0 for an empty sequence.
    """

    errors: list[Finding] = Field(default_factory=list)
    warnings: list[Finding] = Field(default_factory=list)
    checked_generation: int
    decision_graph_digest: str
    validator_version: str

    @field_validator("decision_graph_digest", "validator_version")
    @classmethod
    def _not_blank(cls, value: str, info) -> str:
        if not value:
            raise ValueError(f"{info.field_name} must not be blank")
        return value

    @model_validator(mode="after")
    def _severities_in_their_lists(self) -> CheckResult:
        if wrong := [f.code for f in self.errors if f.severity is not Severity.ERROR]:
            raise ValueError(f"non-ERROR findings in errors: {wrong}")
        if wrong := [f.code for f in self.warnings if f.severity is not Severity.WARNING]:
            raise ValueError(f"non-WARNING findings in warnings: {wrong}")
        return self

    @computed_field
    @property
    def status(self) -> Literal["PASS", "FAIL"]:
        return "FAIL" if self.errors else "PASS"


# --------------------------------------------------------------------------- #
# Digests
# --------------------------------------------------------------------------- #


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def resolution_digest(answer: str, source_refs: Iterable[str]) -> str:
    """The digest a `ResolutionRecord` must carry: over its answer and sorted source refs."""
    payload = {"answer": answer, "source_refs": sorted(source_refs)}
    return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


def _node_content(node: DecisionNode) -> dict:
    return {
        "decision_id": node.decision_id,
        "status": str(node.status),
        "blocked_by": sorted(node.blocked_by),
        "scope_refs": sorted(node.scope_refs),
        "resolution_digest": (node.resolution.resolution_digest
                              if node.resolution is not None else None),
        "claimed_by": node.claimed_by,
        "claim_expires_at": (node.claim_expires_at.isoformat()
                             if node.claim_expires_at is not None else None),
    }


def _map_content(dmap: DecisionMap) -> dict:
    return {
        "map_id": dmap.map_id,
        "destination": dmap.destination,
        "generation": dmap.generation,
        "program_ref": dmap.program_ref,
        "nodes": sorted((_node_content(n) for n in dmap.nodes),
                        key=lambda n: n["decision_id"]),
        "fog": sorted(({"fog_id": f.fog_id, "title": f.title,
                        "clears_with": sorted(f.clears_with),
                        "out_of_scope": f.out_of_scope,
                        "deferred_reason": f.deferred_reason} for f in dmap.fog),
                      key=lambda f: f["fog_id"]),
    }


def graph_digest(maps: Sequence[DecisionMap]) -> str:
    """A digest over the graph's content, independent of the order of `maps`.

    Covers ids, statuses, blockers, scopes, resolution digests, leases and fog.
    Maps are sorted by `map_id` (ties by their canonical content), nodes by
    `decision_id`, fog by `fog_id`, and id lists are sorted: none of those
    orders carries meaning.
    """
    contents = sorted((_map_content(m) for m in maps),
                      key=lambda c: (c["map_id"], _canonical(c)))
    return "sha256:" + hashlib.sha256(_canonical(contents)).hexdigest()


# --------------------------------------------------------------------------- #
# The check
# --------------------------------------------------------------------------- #


def _title_key(title: str) -> str:
    return " ".join(title.casefold().split())


def _non_primary(ref: str) -> bool:
    return ref.startswith(NON_PRIMARY_PREFIXES)


def _finding(code: str, message: str, *, decision_ids=(), fog_ids=(),
             map_ids=()) -> Finding:
    return Finding(code=code, severity=_CODE_SEVERITY[code], message=message,
                   decision_ids=sorted(set(decision_ids)),
                   fog_ids=sorted(set(fog_ids)), map_ids=sorted(set(map_ids)))


def _check_record(dmap: DecisionMap, node: DecisionNode,
                  rec: ResolutionRecord) -> list[Finding]:
    out: list[Finding] = []
    where = {"decision_ids": [node.decision_id], "map_ids": [dmap.map_id]}
    # Also refused by DecisionNode._structural_rules. Re-checked here on purpose:
    # the checker must not trust that every object it is handed went through
    # validation (`model_construct` does not).
    if rec.decision_id != node.decision_id:
        out.append(_finding(
            "resolution-id-mismatch",
            f"decision {node.decision_id!r} in map {dmap.map_id!r} carries a "
            f"resolution record for {rec.decision_id!r}", **where))
    expected = resolution_digest(rec.answer, rec.source_refs)
    if rec.resolution_digest != expected:
        out.append(_finding(
            "resolution-digest-mismatch",
            f"resolution of {node.decision_id!r} in map {dmap.map_id!r} carries "
            f"digest {rec.resolution_digest!r}, but its answer and sources digest "
            f"to {expected!r}", **where))
    if not rec.source_refs:
        out.append(_finding(
            "resolution-without-source",
            f"resolution of {node.decision_id!r} in map {dmap.map_id!r} names no "
            "source: an answer without a source is an assertion", **where))
    elif all(_non_primary(r) for r in rec.source_refs):
        out.append(_finding(
            "resolution-lacks-primary-source",
            f"resolution of {node.decision_id!r} in map {dmap.map_id!r} rests only "
            f"on non-primary sources {sorted(rec.source_refs)}", **where))
    return out


def check(maps: Sequence[DecisionMap], *, validator_version: str,
          program_refs: Iterable[ProgramRef] = (),
          settled_refs: Iterable[str] = (),
          now: datetime | None = None,
          large_map_threshold: int = LARGE_MAP_THRESHOLD) -> CheckResult:
    """Validate the structure of the decision graph formed by `maps`.

    `settled_refs` are decision ids offered as execution provenance -- premises
    some execution relies on as settled. `program_refs` say which maps a
    programme graduated and which decisions it treats as settled. `now` is only
    needed by the lease checks; without it `lease-expired-treated-active` is not
    evaluated and every CLAIMED node counts as holding a lease.

    Checks, by the plan's groups (codes in `CHECK_CODES`):

    * Identity: `duplicate-map-id`.
    * Dependencies: `cross-map-blocker-unresolved` -- a node blocked by D while D
      also lives in another map of the sequence with an unresolved status.
      (Construction forces every `blocked_by` id into the node's own map, so the
      cross-map case is exactly "the same id lives elsewhere, unresolved".)
    * Resolution: `resolution-id-mismatch`, `resolution-digest-mismatch`
      (recomputed with `resolution_digest`), `resolution-without-source`, and
      the warning `resolution-lacks-primary-source` -- every source ref starts
      with one of `NON_PRIMARY_PREFIXES`. Checked on every record present.
    * Scope: `out-of-scope-blocks-active` (an id OUT_OF_SCOPE in any map is in
      some node's `blocked_by`), `out-of-scope-as-provenance` (in `settled_refs`).
    * Claims: `lease-expired-treated-active` (WARNING: CLAIMED with an expiry at
      or before `now`), `two-valid-leases` (one id CLAIMED with a live lease in
      two or more maps).
    * Fog: `fog-shadows-decision` (WARNING: a fog title equals an unresolved
      decision's title after casefolding and collapsing whitespace, across
      maps), `fog-clears-with-missing` (a `clears_with` id in no map at all).
    * Undermining: `undermined-shown-as-settled` (an UNDERMINED id in
      `settled_refs`).
    * Graduation: `graduated-with-blockers` (a graduated `ProgramRef` whose map
      still has `graduation_blockers` for the ref's scope),
      `program-ref-unknown-map`, and `unresolved-as-settled` (a settled premise
      that is unresolved in some map, or exists in no map).
    * Size: `unusually-large-map` (WARNING: more than `large_map_threshold`
      nodes while at least one is unresolved).

    Findings are sorted by code, then ids, so equal graphs give equal receipts.
    """
    maps = list(maps)
    program_refs = list(program_refs)
    settled = sorted(set(settled_refs))
    findings: list[Finding] = []

    # Where each decision id lives, and with what status: ids are global.
    by_id: dict[str, list[tuple[DecisionMap, DecisionNode]]] = defaultdict(list)
    for dmap in maps:
        for node in dmap.nodes:
            by_id[node.decision_id].append((dmap, node))

    def statuses(decision_id: str) -> set[DecisionStatus]:
        return {n.status for _, n in by_id.get(decision_id, ())}

    # Identity
    map_counts: dict[str, int] = defaultdict(int)
    for dmap in maps:
        map_counts[dmap.map_id] += 1
    for map_id in sorted(k for k, v in map_counts.items() if v > 1):
        findings.append(_finding(
            "duplicate-map-id",
            f"map id {map_id!r} appears {map_counts[map_id]} times in the graph",
            map_ids=[map_id]))

    out_of_scope = {d for d in by_id if DecisionStatus.OUT_OF_SCOPE in statuses(d)}
    for dmap in maps:
        for node in dmap.nodes:
            for blocker in node.blocked_by:
                # Dependencies, across maps
                elsewhere = sorted(m.map_id for m, n in by_id.get(blocker, ())
                                   if m is not dmap and n.status in UNRESOLVED)
                if elsewhere:
                    findings.append(_finding(
                        "cross-map-blocker-unresolved",
                        f"decision {node.decision_id!r} in map {dmap.map_id!r} is "
                        f"blocked by {blocker!r}, which is unresolved in maps "
                        f"{elsewhere}",
                        decision_ids=[node.decision_id, blocker],
                        map_ids=[dmap.map_id, *elsewhere]))
                # Scope
                if blocker in out_of_scope:
                    findings.append(_finding(
                        "out-of-scope-blocks-active",
                        f"decision {node.decision_id!r} in map {dmap.map_id!r} is "
                        f"blocked by {blocker!r}, which is OUT_OF_SCOPE: out-of-scope "
                        "work cannot be a live blocker",
                        decision_ids=[node.decision_id, blocker],
                        map_ids=[dmap.map_id]))
            # Resolution
            if node.resolution is not None:
                findings.extend(_check_record(dmap, node, node.resolution))

    # Scope and undermining: what is offered as settled provenance
    for ref in settled:
        if DecisionStatus.OUT_OF_SCOPE in statuses(ref):
            findings.append(_finding(
                "out-of-scope-as-provenance",
                f"{ref!r} is OUT_OF_SCOPE and is offered as execution provenance",
                decision_ids=[ref]))
        if DecisionStatus.UNDERMINED in statuses(ref):
            findings.append(_finding(
                "undermined-shown-as-settled",
                f"{ref!r} is UNDERMINED and is shown as a settled premise",
                decision_ids=[ref]))

    # Claims
    for decision_id in sorted(by_id):
        claimed = [(m, n) for m, n in by_id[decision_id]
                   if n.status is DecisionStatus.CLAIMED]
        if now is not None:
            for dmap, node in claimed:
                if node.claim_expires_at <= now:
                    findings.append(_finding(
                        "lease-expired-treated-active",
                        f"decision {decision_id!r} in map {dmap.map_id!r} is CLAIMED "
                        f"by {node.claimed_by!r} but the lease expired at "
                        f"{node.claim_expires_at.isoformat()}",
                        decision_ids=[decision_id], map_ids=[dmap.map_id]))
            live = [(m, n) for m, n in claimed if n.claim_expires_at > now]
        else:
            live = claimed
        if len(live) > 1:
            findings.append(_finding(
                "two-valid-leases",
                f"decision {decision_id!r} holds {len(live)} valid leases, by "
                f"{sorted(str(n.claimed_by) for _, n in live)}",
                decision_ids=[decision_id], map_ids=[m.map_id for m, _ in live]))

    # Fog
    unresolved_titles: dict[str, list[str]] = defaultdict(list)
    for dmap in maps:
        for node in dmap.nodes:
            if node.status in UNRESOLVED:
                unresolved_titles[_title_key(node.title)].append(node.decision_id)
    for dmap in maps:
        for patch in dmap.fog:
            key = _title_key(patch.title)
            if key and (shadowed := unresolved_titles.get(key)):
                findings.append(_finding(
                    "fog-shadows-decision",
                    f"fog {patch.fog_id!r} in map {dmap.map_id!r} has the same title "
                    f"as unresolved decisions {sorted(set(shadowed))}",
                    decision_ids=shadowed, fog_ids=[patch.fog_id],
                    map_ids=[dmap.map_id]))
            if missing := sorted(c for c in patch.clears_with if c not in by_id):
                findings.append(_finding(
                    "fog-clears-with-missing",
                    f"fog {patch.fog_id!r} in map {dmap.map_id!r} clears with "
                    f"{missing}, which no map of the graph contains",
                    decision_ids=missing, fog_ids=[patch.fog_id],
                    map_ids=[dmap.map_id]))

    # Graduation
    for ref in program_refs:
        targets = [m for m in maps if m.map_id == ref.map_id]
        if not targets:
            findings.append(_finding(
                "program-ref-unknown-map",
                f"programme {ref.program_id!r} refers to map {ref.map_id!r}, which "
                "the graph does not contain", map_ids=[ref.map_id]))
        if ref.graduated:
            for dmap in targets:
                if blockers := graduation_blockers(dmap, scope_refs=ref.scope_refs):
                    findings.append(_finding(
                        "graduated-with-blockers",
                        f"programme {ref.program_id!r} graduated map {dmap.map_id!r} "
                        f"for scope {sorted(ref.scope_refs)}, but it is blocked: "
                        + "; ".join(blockers),
                        map_ids=[dmap.map_id]))
        for premise in sorted(set(ref.settled_premises)):
            found = statuses(premise)
            if not found or found & UNRESOLVED:
                what = (sorted(str(s) for s in found & UNRESOLVED) if found
                        else "in no map of the graph")
                findings.append(_finding(
                    "unresolved-as-settled",
                    f"programme {ref.program_id!r} treats {premise!r} as a settled "
                    f"premise, but it is {what}",
                    decision_ids=[premise], map_ids=[ref.map_id]))

    # Size
    for dmap in maps:
        if (len(dmap.nodes) > large_map_threshold
                and any(n.status in UNRESOLVED for n in dmap.nodes)):
            findings.append(_finding(
                "unusually-large-map",
                f"map {dmap.map_id!r} holds {len(dmap.nodes)} nodes (threshold "
                f"{large_map_threshold}) and is not yet resolved: consider splitting it",
                map_ids=[dmap.map_id]))

    findings.sort(key=lambda f: (f.code, f.decision_ids, f.fog_ids, f.map_ids, f.message))
    return CheckResult(
        errors=[f for f in findings if f.severity is Severity.ERROR],
        warnings=[f for f in findings if f.severity is Severity.WARNING],
        checked_generation=max((m.generation for m in maps), default=0),
        decision_graph_digest=graph_digest(maps),
        validator_version=validator_version,
    )
