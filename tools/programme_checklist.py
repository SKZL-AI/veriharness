#!/usr/bin/env python3
"""This programme's own checklist, decided by the product's own reconciler.

V3.3 P2-28's phase-gate binding, and the reason P2-26 counts as anything: a
reconciler nothing consults is a component with tests, not a capability. So the
checklist this programme runs on is built here from the authoritative register
and the measured capability matrix, handed to `hoh.checklist.reconcile`, and the
phase gates are whatever `hoh.checklist.phase_gate` says they are.

Three properties worth stating, because each is a way this could have been
theatre:

* **The statuses are not copied, they are derived.** An item's evidence is the
  matrix's own evidence strings for that requirement, and `reconcile` decides
  the status from them. Nothing here writes VERIFIED.
* **The gate re-derives.** `phase_gate` is called *with* the evidence, so the
  verdict does not rest on the snapshot it was handed -- the mode is recorded in
  the verdict and asserted in the tests.
* **`fsck` runs first and its problems are fatal.** A checklist that is
  structurally unsound cannot produce a meaningful verdict, and reporting the
  verdict anyway would be the false green this whole file exists to prevent.

Usage:
    python3 tools/programme_checklist.py                 # report
    python3 tools/programme_checklist.py --out PATH      # write the snapshot
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent

#: A preserved review directory's name is a UTC stamp, four or six digits after the `T`.
_STAMP = re.compile(r"^\d{8}T\d{4}(?:\d{2})?Z$")
sys.path.insert(0, str(HOH / "src"))

from hoh.checklist import (  # noqa: E402 - after the path is set
    ChecklistItem,
    Criticality,
    ItemEvidence,
    fsck,
    phase_gate,
    reconcile,
)

REGISTER = HOH / "program/v3_3/REQUIREMENTS.json"
MATRIX = HOH / "program/v3_3/VERIHARNESS_CAPABILITY_MATRIX.json"
def declared_requirement_refs(register: dict) -> set[str]:
    """The plan items the register *declares* this checklist covers.

    O220/F9, second half. `fsck`'s requirement-resolution check used to be handed a
    set built from the items' own `requirement_ref`s, which made it unanswerable: an
    item always resolves against a set derived from itself.

    This reads the register instead -- each requirement's `authoritative_item` plus
    the extra items it declares under `also_covers` (O204). That is an external set,
    so the check can fail, and it is the *right* external set: it asks whether every
    plan item the register says it covers really has an item on the checklist.

    It deliberately is **not** every item in the pinned plan's inventory. Handing
    `fsck` all 306 of those asserts that the checklist must cover the plan's
    deliverables, gates and acceptance paths as well, which is a larger claim than
    this checklist was built to make -- and when it was tried here it produced 122
    problems that are real but belong to O220/F2, not to this repair. The gap is
    tracked there rather than papered over by a set that makes it invisible.
    """
    refs: set[str] = set()
    for requirement in register.get("requirements") or []:
        primary = str(requirement.get("authoritative_item") or "").strip()
        if primary:
            refs.add(primary)
        for extra in requirement.get("also_covers") or []:
            if str(extra).strip():
                refs.add(str(extra).strip())
    return refs

#: The matrix's statuses that count as evidence of the capability existing.
#: Only PROVEN does. PARTIAL means something stands in for the thing and
#: `capability_matrix` says so in as many words -- treating it as evidence here
#: would import a distinction the matrix is careful about and drop it.
PROVEN = "PROVEN"


def criticality_of(requirement: dict) -> Criticality:
    """How much a requirement blocks, from what the register already says.

    `core_required_1_0` is the release-blocking set -- those are the
    capabilities 1.0 is defined by. Everything else in the register blocks its
    own phase, because the programme promised it; only an explicitly optional
    provider does not block a gate.
    """
    if requirement.get("core_required_1_0"):
        return Criticality.P0_RELEASE_BLOCKING
    if requirement.get("optional_provider"):
        return Criticality.P2_NONBLOCKING
    # Everything else the register carries blocks its own phase. The first
    # version of this mapping made anything without a 1.0 flag non-blocking,
    # and P9 through P12 came out "closed" with every item EVIDENCE_PENDING --
    # eighteen unbuilt UI requirements reported as a closed gate. A requirement
    # is in the register because the programme promised it; only an explicitly
    # optional provider is exempt.
    return Criticality.P1_PHASE_BLOCKING


#: The keys a register may carry its plan revision under. `scope_plan_sha256` is the
#: one the real register actually uses; the other two were guesses, and the guesses
#: were all this read for as long as it mattered.
PLAN_DIGEST_KEYS = ("scope_plan_sha256", "plan_sha256", "plan_digest")


def plan_revision(register: dict) -> str:
    """The plan revision this register was reconciled against.

    O220/F9, found by the P2 gate's own supervisor review. This used to fall back to
    the literal string `plan-digest-unknown`, and because the real register carries
    `scope_plan_sha256` rather than `plan_sha256`, *every* checklist item in the
    programme's history carried that string. An item's status is meant to be a
    statement about a revision of the plan; none of them named one, and nothing
    noticed because a string is a perfectly good string.

    So there is no fallback any more. A register with no plan revision cannot produce
    a checklist whose statuses mean anything, which is the same argument that makes
    `fsck` fatal before a verdict is reported.
    """
    for key in PLAN_DIGEST_KEYS:
        value = str(register.get(key) or "").strip()
        if value:
            return value
    raise ValueError(
        "the register carries no plan revision under any of "
        f"{PLAN_DIGEST_KEYS}, so an item's status could not be a statement about a "
        "revision of the plan. Refusing to build a checklist that would say it is")


def build(register: dict, matrix: dict) -> tuple[list[ChecklistItem], dict[str, ItemEvidence]]:
    """The programme's checklist and the evidence for it, both derived.

    The source digest is the plan digest the register was checked against: an
    item's status is a statement about a revision of the plan, and the digest is
    what makes it one. A register that carries none is refused -- see
    `plan_revision`.

    **One checklist item per plan item covered, not per requirement.** O204 let a
    requirement declare extra plan items under `also_covers`, and this function went
    on emitting exactly one item per requirement, whose `requirement_ref` was the
    primary. So the two extra items of the acceptance path were declared covered and
    had nothing on the checklist. Nothing noticed until O220/F9 made `fsck`'s
    requirement-resolution check reachable, and it named them on the first run.

    The extra items share the requirement's measurement, because that is what
    `also_covers` means -- one run proved the whole path -- and they carry their own
    ids so `fsck` can still refuse duplicates.
    """
    plan_digest = plan_revision(register)
    measured = {c["id"]: c for c in matrix.get("capabilities") or []}
    items: list[ChecklistItem] = []
    evidence: dict[str, ItemEvidence] = {}
    for requirement in register.get("requirements") or []:
        rid = requirement["id"]
        seen = measured.get(rid)
        refs = [f"matrix:{rid}"]
        items.append(ChecklistItem(
            item_id=rid,
            phase=requirement["target_phase"],
            title=requirement["name"],
            requirement_ref=requirement.get("authoritative_item") or rid,
            source_digest=plan_digest,
            criticality=criticality_of(requirement),
            evidence_refs=refs,
        ))
        covered = [str(x).strip() for x in (requirement.get("also_covers") or [])
                   if str(x).strip()]
        for extra in covered:
            items.append(ChecklistItem(
                item_id=f"{rid}+{extra}",
                phase=requirement["target_phase"],
                title=f"{requirement['name']} -- {extra}",
                requirement_ref=extra,
                source_digest=plan_digest,
                criticality=criticality_of(requirement),
                evidence_refs=refs,
            ))
        if seen is None:
            continue                      # no measurement: no evidence, and reconcile says so
        present = refs if seen.get("status") == PROVEN else []
        # O228/K2. `open_findings=[]` was hard-coded here with no comment, and it is
        # the product's designed route for a finding to hold an item open:
        # `src/hoh/checklist.py` says "a finding that should keep an item from verifying
        # must reach it as a confirmed finding in the item's `ItemEvidence.open_findings`.
        # The supervisor finds; the reconciler verifies." Feeding it an empty list from
        # this programme's own binding made that route dead, and made `fsck`'s ninth
        # check -- "an unresolved release-blocking finding, which no phase may close
        # over" -- unreachable by construction.
        #
        # It stays a literal for now, and the literal is now *explained*: this tool
        # derives the checklist from the register and the matrix and has no supervisor
        # findings in hand. The binding that does have them is
        # `tools/readiness.py:row_programme_supervisor`, which holds the phase open on
        # them directly. Wiring the two together is the next increment, tracked, because
        # it changes what `reconcile` may verify and belongs in its own review rather
        # than at the end of a long session.
        measurement = ItemEvidence(
            present_refs=present,
            stale_refs=[] if present else refs,
            source_digest=plan_digest,
            open_findings=[],
        )
        evidence[rid] = measurement
        for extra in covered:
            evidence[f"{rid}+{extra}"] = measurement
    return items, evidence


def _build_plan():
    """`tools/build_plan.py`, loaded once as `build_plan` -- the module that reads
    the programme inventory and binds its gate items to the register, reused rather
    than re-derived here (the pattern of `programme_supervisor._load_programme_checklist`)."""
    name = "build_plan"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parent / "build_plan.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _programme_scope() -> tuple[dict | None, str]:
    """The merged programme inventory (the plan's plus its amendments), or why not.

    Without it nothing here can say which gate conditions a phase has, so every
    verdict is reported as not evaluating the phase exit -- never as covering it. An
    amendments document with problems (`ValueError`) or an unreadable inventory
    (`OSError`) is reported rather than raised.
    """
    try:
        scope = _build_plan()._scope()
    except (ValueError, OSError) as exc:
        return None, f"programme inventory unreadable: {exc}"
    if scope is None:
        return None, "no programme inventory in this tree"
    return scope, ""


def phase_exit(scope: dict, register: dict, matrix: dict) -> dict[str, dict[str, list[str]]]:
    """Each phase's gate conditions, by whether the checklist's verdict covers them.

    O220/F2 and M5. A phase whose capability items are all VERIFIED reads `closed`,
    but its exit also carries the plan's gate-kind items -- exit tests, invariants,
    validation runs -- and those are on the checklist only through a requirement
    that claims them. Placed by `build_plan.gate_bindings` and the matrix:

    * `unbound`: claimed by no requirement of its phase, so nothing here evaluates it;
    * `not_held`: claimed, but at least one claimer is non-blocking and not PROVEN.
      `phase_gate` passes over a non-blocking item, so nothing holds the phase on it,
      and one held claim does not cover an unheld one;
    * `bound`: every other claimed item. It is on the checklist through a
      phase-blocking requirement or a PROVEN one.
    """
    requirements = register.get("requirements") or []
    by_id = {r["id"]: r for r in requirements}
    status = {c["id"]: c.get("status") for c in matrix.get("capabilities") or []}
    bindings = _build_plan().gate_bindings(scope.get("items") or [], requirements)
    out: dict[str, dict[str, list[str]]] = {}
    for item in scope.get("items") or []:
        if item["id"] not in bindings:
            continue
        entry = out.setdefault(item["phase"], {"bound": [], "unbound": [], "not_held": []})
        claimers = bindings[item["id"]]
        if not claimers:
            entry["unbound"].append(item["id"])
        elif any(criticality_of(by_id[rid]) is Criticality.P2_NONBLOCKING
                 and status.get(rid) != PROVEN for rid in claimers):
            entry["not_held"].append(item["id"])
        else:
            entry["bound"].append(item["id"])
    return out


def _exit_note(exits: dict | None, phase: str, why: str) -> str:
    """What a verdict line adds when the verdict does not cover the phase exit."""
    if exits is None:
        return f"; phase exit not evaluated ({why})"
    entry = exits.get(phase) or {}
    parts = []
    if entry.get("unbound"):
        parts.append(f"{len(entry['unbound'])} gate condition(s) bound to no requirement")
    if entry.get("not_held"):
        parts.append(f"{len(entry['not_held'])} gate condition(s) bound only through a "
                     "non-blocking requirement not PROVEN")
    return f"; phase exit not evaluated ({', '.join(parts)})" if parts else ""


class ReviewRootOutside(Exception):
    """`dogfood/p2-gate` resolves outside the repository, or cannot be resolved (T-PSD6).

    Its findings would then be some other tree's, and an empty foreign review would
    silence `fsck`'s ninth check. So this is raised, never answered with `[]`."""


_REVIEW_ROOT_OUTSIDE = ("the preserved review root dogfood/p2-gate resolves outside the "
                        "repository, so no finding of this tree can be read; no phase closes")


def _preserved_release_blocking_findings() -> list[str]:
    """The ids of findings the newest preserved P2 review returned. (O228/K2)

    `fsck`'s ninth check refuses a phase that closes over an unresolved
    release-blocking finding, and nothing was passing it anything, so it could not
    fail. Read from the preserved review directory rather than from a record, for the
    reason five rounds of review established: a record is written by the party the gate
    is checking.

    Empty when nothing is preserved, which is honest -- and is not a statement that
    there are no findings, only that this tool has none in hand.
    """
    # O229/L3, and it is O228/J4's own defect sitting in the function O228/K2 added --
    # written into this file in the same entry that repaired it next door. No stamp shape
    # was required and the variable was already called `stamped`, so one `mkdir` of a
    # lowercase name erased the fourteen from `fsck`'s ninth check: ASCII puts every
    # lowercase name after every timestamp. It also followed symlinks (O229/L1) and asked
    # only the newest directory (O229/L2).
    #
    # One rule now, shared in spirit with the gate's: stamped, in-tree, and **accumulated**
    # across every preserved review, so adding a directory can raise or resolve and never
    # forget.
    root = HOH / "dogfood/p2-gate"
    # T-PSD6: each review directory was checked only against `root.resolve()`, so a
    # symlinked root leading out of the repository fed a foreign review's findings in.
    # Present at all -- even as a dangling or looping link -- it must resolve inside;
    # anything else fails closed (Q9). Only a root that is not there reads as empty.
    if root.is_symlink() or root.exists():
        try:
            inside = root.resolve(strict=True).is_relative_to(HOH.resolve())
        except (OSError, ValueError, RuntimeError):
            inside = False
        if not inside:
            raise ReviewRootOutside(_REVIEW_ROOT_OUTSIDE)
    if not root.is_dir():
        return []
    reviews: list[tuple[str, list]] = []
    for d in sorted(root.iterdir(), key=lambda x: x.name):
        if d.is_symlink() or not d.is_dir() or not _STAMP.match(d.name):
            continue
        try:
            if not d.resolve().is_relative_to(root.resolve()):
                continue
            raw = json.loads((d / "findings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        returned = raw.get("findings", []) if isinstance(raw, dict) else raw
        reviews.append((d.name, [f for f in returned if isinstance(f, dict)]))
    # Imported here rather than at module scope: `programme_supervisor` binds this
    # module's `build` at import time, so a top-level import is a cycle -- which I wrote,
    # and which the checklist row caught by returning a traceback instead of a verdict.
    # Its directory goes on the path first: this module is loaded by file, and the import
    # used to work only when some earlier import had put `tools/` there as a side effect.
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    from programme_supervisor import resolution_state
    still_open, _resolved = resolution_state(reviews)
    return sorted(still_open)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=None,
                    help="write the reconciled snapshot here")
    ap.add_argument("--json", action="store_true", help="print the verdicts as JSON")
    args = ap.parse_args(argv)

    if not REGISTER.is_file() or not MATRIX.is_file():
        print("the register or the capability matrix is not in this tree, so this "
              "programme's checklist cannot be derived from it", file=sys.stderr)
        return 3
    register = json.loads(REGISTER.read_text(encoding="utf-8"))
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    try:
        items, evidence = build(register, matrix)
    except ValueError as exc:
        # `plan_revision` refuses a register with no plan revision, and that refusal
        # used to escape as a traceback -- replacing two designed exit codes with a
        # stack trace, and turning the board's reason line into a traceback header.
        # A register that cannot say which plan revision it was reconciled against is
        # an environment gap, which this tool already has a code for.
        print(exc, file=sys.stderr)
        return 3

    # O220/F9, second half. These two sets used to be derived from the very items
    # being checked, which made two of `fsck`'s nine structural checks unreachable by
    # construction: an item can always resolve against a set built from its own
    # requirement_ref, and its digest is always "known" if the known set is the
    # digests present. They now come from outside the items -- the authoritative
    # plan inventory for requirement refs, and the register's declared revision for
    # digests -- so "an unresolvable requirement ref" and "an unknown source digest"
    # are questions that can fail.
    requirements = declared_requirement_refs(register)
    # O228/K2: `release_blocking_findings` was passed by neither programme tool, so the
    # ninth of `fsck`'s checks could not fail. The findings this programme actually holds
    # live in the preserved P2 review, so they are read from there rather than invented.
    root_problem = None
    try:
        blocking = _preserved_release_blocking_findings()
    except ReviewRootOutside as exc:
        # T-PSD6, fail closed: no finding of this tree can be read, so no phase closes.
        # Said first, because the first stdout line is what the readiness row reads; the
        # verdict and the snapshot are still produced, and the exit code says it.
        root_problem = str(exc)
        blocking = []
        if not args.json:
            print(root_problem, flush=True)
    broken = fsck(items, requirements=requirements,
                  known_digests={plan_revision(register)})
    if broken:
        print(f"the checklist is not sound, so no verdict is reported "
              f"({len(broken)} problem(s)):", file=sys.stderr)
        for problem in broken[:10]:
            print(f"  {problem}", file=sys.stderr)
        return 1

    snapshot = reconcile(items, evidence=evidence, generation=1)
    after = fsck(snapshot.items, snapshot=snapshot, requirements=requirements,
                 known_digests={plan_revision(register)})
    if after:
        print("the reconciled checklist is not sound:", file=sys.stderr)
        for problem in after[:10]:
            print(f"  {problem}", file=sys.stderr)
        return 1

    # O228/K2, second half. `fsck`'s ninth check -- "an unresolved release-blocking
    # finding, which no phase may close over" -- was unreachable because nothing passed
    # it anything. It is reachable now, and it is asked *separately* from the two calls
    # above, because the two questions are different and running them together made an
    # open finding look like a corrupt checklist:
    #
    #   * the calls above ask whether the checklist is *sound* -- whether a verdict may
    #     be reported about it at all;
    #   * this one asks whether a phase may *close*, which is a fact about the programme
    #     and not about the data structure.
    #
    # So an open finding no longer suppresses the verdict; it is printed beside it, and
    # the closure claim is withheld rather than the whole report.
    closure = fsck(snapshot.items, snapshot=snapshot, requirements=requirements,
                   release_blocking_findings=blocking,
                   known_digests={plan_revision(register)})
    open_blocking = [p for p in closure if p not in after]
    if open_blocking:
        # O229/L3, second half: this printed to stderr and left the return code at 0, so
        # the board published `programme_checklist | PASS | 186 item(s) reconciled` with
        # fourteen unresolved release-blocking findings in hand. O228/K2 claimed "the check
        # fires -- for all fourteen", which was true of the tool's stderr and false of the
        # surface this programme publishes as its status. That is O228/K5's "dead code that
        # looks like a safety check" with one extra step, and it was mine.
        print(f"{len(open_blocking)} unresolved release-blocking finding(s); no phase "
              "closes over one:", file=sys.stdout)
        for problem in open_blocking[:5]:
            print(f"  {problem}", file=sys.stdout)
        # The code is returned at the *end*, after the snapshot is written: returning here
        # skipped the `--out` write, so the caller got a failure and no artefact. A gate
        # that withholds its own evidence when it fails is the opposite of the point.

    phases = sorted({item.phase for item in snapshot.items},
                    key=lambda p: (len(p), p))
    verdicts = {}
    for phase in phases:
        verdict = phase_gate(snapshot, phase, evidence=evidence)
        verdicts[phase] = verdict
    # O220/F2, M5: a verdict over capability coverage is not a phase exit. Each phase's
    # gate conditions come from the inventory; where the verdict does not cover one, the
    # line says so, and a phase with gate conditions but no checklist item is shown.
    scope, scope_why = _programme_scope()
    exits = phase_exit(scope, register, matrix) if scope is not None else None
    shown = set(phases)
    for phase, entry in (exits or {}).items():
        if entry["unbound"] or entry["not_held"]:
            shown.add(phase)
    shown_phases = sorted(shown, key=lambda p: (len(p), p))

    def evaluated(phase: str) -> bool:
        if exits is None:
            return False
        entry = exits.get(phase) or {}
        return not (entry.get("unbound") or entry.get("not_held"))

    if exits is None:
        exit_report = {p: {"evaluated": False, "why": scope_why} for p in shown_phases}
    else:
        empty = {"bound": [], "unbound": [], "not_held": []}
        exit_report = {p: {"evaluated": evaluated(p), **(exits.get(p) or empty)}
                       for p in shown_phases}
    if args.out:
        args.out.write_text(json.dumps({
            "what": "this programme's checklist, reconciled by hoh.checklist",
            "generation": snapshot.generation,
            "seal": snapshot.seal,
            "gates": {p: {"closed": v.closed,
                          "derived_from_evidence": v.derived_from_evidence,
                          "blockers": [b.item_id for b in v.blockers]}
                      for p, v in verdicts.items()},
            "phase_exit": exit_report,
            "items": [json.loads(i.model_dump_json()) for i in snapshot.items],
        }, indent=2) + "\n", encoding="utf-8")
    if args.json:
        report: dict = {}
        for p in shown_phases:
            v = verdicts.get(p)
            report[p] = {"closed": bool(v and v.closed),
                         "blockers": len(v.blockers) if v else 0,
                         "phase_exit_evaluated": evaluated(p)}
        if root_problem:
            report["review_root_problem"] = root_problem
        print(json.dumps(report, indent=2))
        return 1 if (open_blocking or root_problem) else 0
    counts: dict[str, int] = {}
    for item in snapshot.items:
        counts[item.status.value] = counts.get(item.status.value, 0) + 1
    print(f"{len(snapshot.items)} item(s) reconciled by hoh.checklist: "
          + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
    for phase in shown_phases:
        note = _exit_note(exits, phase, scope_why)
        verdict = verdicts.get(phase)
        if verdict is None:
            print(f"  {phase}: no checklist item{note}")
            continue
        if verdict.closed:
            mark = "capability coverage closed" if note else "closed"
        else:
            k = len(verdict.blockers)
            mark = f"capability coverage: {k} blocking" if note else f"{k} blocking"
        print(f"  {phase}: {mark}{note}"
              + ("" if verdict.derived_from_evidence else "  (seal only)"))
    # O229/L3: exit 1 when a preserved review has left release-blocking findings open. The
    # snapshot and the verdicts are written and printed either way -- the code says the
    # phase may not close, not that the report is unavailable. T-PSD6: likewise when the
    # preserved review root cannot be read as this tree's.
    return 1 if (open_blocking or root_problem) else 0


if __name__ == "__main__":
    raise SystemExit(main())
