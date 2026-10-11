#!/usr/bin/env python3
"""The order the remaining work can actually be done in, derived.

V3.3 P0. Two artifacts the plan asks for, and they are the same question
asked twice: the implementation DAG is what depends on what, and the build
checklist is what is left with its current status beside it. Deriving both
from one register is the point -- a hand-written order and a hand-written
checklist drift apart, and then a phase gate is reading one while somebody
works from the other.

What this refuses to do:

* **Invent an order.** Edges come from `depends_on` in the register. A
  requirement with no declared dependency is ready now, and says so, rather
  than being given a plausible predecessor.
* **Call a cycle a plan.** A dependency cycle is reported as a cycle and the
  exit code says so. A topological sort that silently drops an edge to make
  progress is how a build order comes to contain an impossible step.
* **Treat a proven capability as work.** The checklist carries only what is
  not PROVEN, and every item names the probe that will close it.

Usage:
    python3 tools/build_plan.py
    python3 tools/build_plan.py --dag program/v3_3/VERIHARNESS_IMPLEMENTATION_DAG.json
    python3 tools/build_plan.py --checklist program/v3_3/VERIHARNESS_BUILD_CHECKLIST.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent


def _matrix():
    spec = importlib.util.spec_from_file_location(
        "capability_matrix", HERE / "capability_matrix.py")
    cm = importlib.util.module_from_spec(spec)
    sys.modules["capability_matrix"] = cm
    spec.loader.exec_module(cm)
    return cm


def _baseline_docs():
    spec = importlib.util.spec_from_file_location("baseline_docs",
                                                  HERE / "baseline_docs.py")
    bd = importlib.util.module_from_spec(spec)
    sys.modules["baseline_docs"] = bd
    spec.loader.exec_module(bd)
    return bd


def measurement_status(item_id: str) -> str:
    """The status of a P0 `measurement` item, read back from the generated
    current baseline: its section is there, sourced, honest about
    NOT_MEASURED, and every line it cites exists. Not a state somebody sets;
    and not a freshness claim -- that is the board's `baseline_docs` row."""
    bd = _baseline_docs()
    title = bd.MEASUREMENT_SECTIONS.get(item_id)
    if title is None:
        return ("NOT MEASURED: no section of the baseline is defined for "
                "this item")
    path = HOH / "program/v3_3" / bd.CURRENT_BASELINE
    text = path.read_text(encoding="utf-8") if path.is_file() else None
    ok, reason = bd.section_status(text, title, HOH)
    if not ok:
        return f"NOT MEASURED: {reason}"
    return f"measured: {bd.CURRENT_BASELINE}#{'-'.join(title.lower().split())}"


def _program_scope():
    spec = importlib.util.spec_from_file_location("program_scope",
                                                  HERE / "program_scope.py")
    ps = importlib.util.module_from_spec(spec)
    sys.modules["program_scope"] = ps
    spec.loader.exec_module(ps)
    return ps


def _scope() -> dict | None:
    """The programme inventory: the pinned plan's, plus the pinned operator
    amendments (A1). Read through program_scope so there is one merge rule."""
    ps = _program_scope()
    return ps.load_merged_scope(HOH / "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json",
                                HOH / "program/v3_3/PROGRAMME_AMENDMENTS.json")


def _pulled_forward(r: dict) -> bool:
    """A requirement the operator pulled ahead of its phase (A1). It may start
    before the gates in front of its phase open -- IMPLEMENTED EARLY -- but it
    still belongs to its phase and still holds that phase's own gate shut
    until it is satisfied."""
    return isinstance(r.get("pulled_forward"), dict)


def layers(nodes: dict[str, list[str]]) -> tuple[list[list[str]], list[str]]:
    """Kahn's algorithm, reporting what it could not place.

    Returns (waves, unplaced). `unplaced` is non-empty exactly when there is a
    cycle or a dangling dependency, and the caller is expected to treat that
    as a failure rather than as a shorter plan.
    """
    remaining = {k: set(v) for k, v in nodes.items()}
    waves: list[list[str]] = []
    placed: set[str] = set()
    while True:
        ready = sorted(k for k, deps in remaining.items()
                       if not (deps - placed) and k not in placed)
        if not ready:
            break
        waves.append(ready)
        placed |= set(ready)
        for k in ready:
            remaining.pop(k, None)
    return waves, sorted(remaining)


def _satisfied(r: dict) -> bool:
    """PROVEN, or waived with a document behind the waiver. The plan's phase
    gate reads "VERIFIED or validly WAIVED"; a waiver with no document is not
    valid and does not satisfy anything."""
    waiver = r.get("waiver") or {}
    return r["status"] == "PROVEN" or bool(waiver.get("document") and waiver.get("reason"))


def _blocking(r: dict) -> bool:
    """Does this requirement hold its phase's gate shut?

    System-One items start in SHADOW and the early UI is explicitly not
    authoritative (P3), so neither blocks a phase. Everything else does.
    """
    return not (r.get("optional_provider") or r.get("ui_only"))


def _ready_now(open_: dict, phase_order: list[str] | None,
               gate_state: dict) -> list[str]:
    """Items that could start now: own dependencies satisfied, every earlier
    phase gate open. Computed directly rather than read off the first layer,
    because a gate node occupies a layer of its own and made an unblocked
    item look one wave further away than it is."""
    def gate_open(ph: str) -> bool:
        g = gate_state.get(ph) or {}
        return not g.get("blocking_open") and g.get("external_met") is not False
    ready = []
    for rid, r in open_.items():
        if any(d in open_ for d in (r.get("depends_on") or [])):
            continue
        if phase_order and r["target_phase"] in phase_order and not _pulled_forward(r):
            i = phase_order.index(r["target_phase"])
            if not all(gate_open(ph) for ph in phase_order[:i]):
                continue
        ready.append(rid)
    return sorted(ready)


def build(body: dict, phase_order: list[str] | None = None,
          phase_gates: dict[str, tuple[bool, str]] | None = None) -> dict:
    """The DAG and the checklist, from one measured matrix.

    With `phase_order`, the plan's sequence is part of the graph: every
    requirement in phase N waits on a gate node for phase N-1, and that gate
    waits on every blocking requirement of N-1 that is not yet satisfied.
    `phase_gates` supplies the answer for phases that have no requirements to
    wait on -- P0's deliverables exist or they do not; P7's product proof has
    been run or it has not. A phase absent from it with no open blocking work
    is open.
    """
    caps = {r["id"]: r for r in body["capabilities"]}
    open_ = {rid: r for rid, r in caps.items() if not _satisfied(r)}
    nodes: dict[str, list[str]] = {}
    dangling: dict[str, list[str]] = {}
    for rid, r in open_.items():
        deps = [d for d in (r.get("depends_on") or [])]
        # A dependency that is already satisfied is not a step.
        unmet = [d for d in deps if d in open_]
        unknown = [d for d in deps if d not in caps]
        if unknown:
            dangling[rid] = unknown
        nodes[rid] = unmet
    gate_state: dict[str, dict] = {}
    if phase_order:
        gates = phase_gates or {}
        for i, ph in enumerate(phase_order):
            blockers = sorted(rid for rid, r in open_.items()
                              if r["target_phase"] == ph and _blocking(r))
            given = gates.get(ph)
            gate_deps = list(blockers)
            if given is not None and not given[0]:
                # A gate nothing in the register can close -- an unmeasured
                # validation phase -- waits on a node that never resolves.
                gate_deps.append(f"unmet:{ph}")
            if i:
                gate_deps.append(f"gate:{phase_order[i - 1]}")
            nodes[f"gate:{ph}"] = gate_deps
            gate_state[ph] = {"blocking_open": blockers,
                              "external": (given[1] if given else None),
                              "external_met": (given[0] if given else None)}
        for rid, r in open_.items():
            i = phase_order.index(r["target_phase"]) if r["target_phase"] in phase_order else -1
            if i > 0 and not _pulled_forward(r):
                nodes[rid] = nodes[rid] + [f"gate:{phase_order[i - 1]}"]
        for ph, (met, _) in gates.items():
            if not met:
                nodes[f"unmet:{ph}"] = [f"unmet:{ph}"]     # a self-loop: never placed
    waves, unplaced = layers(nodes)
    # A gate is open *now* when its phase has no open blocking requirement, no
    # unmet external condition, and every gate before it is open. Being placed
    # in a wave is not the same thing: the layering places a gate after the
    # work it waits for, whether that work is done or not. (A1 needs the real
    # answer to say IMPLEMENTED EARLY rather than PHASE REQUIREMENT SATISFIED.)
    open_gates: list[str] = []
    for ph in phase_order or []:
        g = gate_state.get(ph) or {}
        if g.get("blocking_open") or g.get("external_met") is False:
            break
        open_gates.append(ph)
    real_unplaced = [u for u in unplaced if not u.startswith(("gate:", "unmet:"))]
    held = sorted(u for u in real_unplaced
                  if any(d.startswith(("gate:", "unmet:")) for d in nodes.get(u, [])))
    unplaced = [u for u in real_unplaced if u not in held]
    waves = [[w for w in wave if not w.startswith(("gate:", "unmet:"))] for wave in waves]
    waves = [w for w in waves if w]
    held_items = [
        {"id": rid, "name": caps[rid]["name"], "phase": caps[rid]["target_phase"],
         "status": caps[rid]["status"],
         "core_required_1_0": caps[rid]["core_required_1_0"],
         "phase_blocking": _blocking(caps[rid]),
         "depends_on": [d for d in nodes[rid]
                        if not d.startswith(("gate:", "unmet:"))],
         "held_by": sorted({d.split(":", 1)[1] for d in nodes[rid]
                            if d.startswith("gate:")})}
        for rid in held]
    return {
        "held_items": held_items,
        "what": ("Implementation DAG and build checklist, derived from the "
                 "capability matrix. Waves are dependency layers, not a "
                 "schedule: everything in one wave is unblocked, nothing in "
                 "it is promised to be done at the same time."),
        "measured_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open_count": len(open_),
        "waves": [
            {"wave": i + 1,
             "items": [{"id": rid, "name": caps[rid]["name"],
                        "phase": caps[rid]["target_phase"],
                        "status": caps[rid]["status"],
                        "core_required_1_0": caps[rid]["core_required_1_0"],
                        "pulled_forward": caps[rid].get("pulled_forward"),
                        "depends_on": [d for d in nodes[rid]
                                       if not d.startswith(("gate:", "unmet:"))]}
                       for rid in wave]}
            for i, wave in enumerate(waves)
        ],
        "unplaceable": unplaced,
        "held_by_phase_gate": held,
        "phase_gates": gate_state,
        "open_gates": open_gates,
        "ready_now": _ready_now(open_, phase_order, gate_state),
        "dangling_dependencies": dangling,
    }


def checklist(plan: dict) -> dict:
    """One item per open requirement, with what closes it.

    Phase-blocking is read from the register's own columns rather than
    decided here: a core-required capability blocks its phase, an optional
    provider does not.
    """
    items = []
    ready = set(plan.get("ready_now") or [])
    for wave in plan["waves"]:
        for item in wave["items"]:
            items.append({
                "item_id": item["id"],
                "title": item["name"],
                "phase": item["phase"],
                "wave": wave["wave"],
                "state": item["status"],
                "ready_now": item["id"] in ready,
                "phase_blocking": bool(item["core_required_1_0"]),
                "blocked_by": item["depends_on"],
                "pulled_forward": item.get("pulled_forward"),
                "closes_when": ("the probe in program/v3_3/REQUIREMENTS.json "
                                "reports PROVEN"),
            })
    # Held behind a phase gate is *represented*, not absent (O196). A
    # checklist that listed only what could start would lose most of the
    # programme the moment an early gate stayed shut.
    for item in plan.get("held_items") or []:
        items.append({
            "item_id": item["id"],
            "title": item["name"],
            "phase": item["phase"],
            "wave": None,
            "state": item["status"],
            "ready_now": False,
            "phase_blocking": bool(item["phase_blocking"]),
            "blocked_by": item["depends_on"],
            "held_by_phase_gate": item["held_by"],
            "closes_when": ("the probe in program/v3_3/REQUIREMENTS.json "
                            "reports PROVEN"),
        })
    return {
        "what": ("Build checklist, one item per capability that is not "
                 "PROVEN. Every state here is re-derived by "
                 "`tools/capability_matrix.py`; nothing in this file is a "
                 "state somebody set."),
        "measured_at_utc": plan["measured_at_utc"],
        "open_count": plan["open_count"],
        "blocking_count": sum(1 for i in items if i["phase_blocking"]),
        "items": items,
    }


def render(plan: dict, check: dict) -> str:
    out = ["# Build checklist (V3.3 P0)", "",
           "Generated by `python3 tools/build_plan.py`. Every state is",
           "re-derived from the capability matrix; nothing here is a state",
           "somebody set by hand. Waves are dependency layers, not dates.", "",
           f"Measured at {plan['measured_at_utc']}.", "",
           f"{check['open_count']} open, {check['blocking_count']} of them "
           f"phase-blocking.", ""]
    for wave in plan["waves"]:
        ready = "ready now" if wave["wave"] == 1 else (
            f"unblocked once wave {wave['wave'] - 1} is done")
        out += [f"## Wave {wave['wave']} — {ready}", "",
                "| id | requirement | phase | status | blocking | waits for |",
                "|---|---|---|---|---|---|"]
        for item in wave["items"]:
            early = (f" — pulled forward ({item['pulled_forward']['amendment']})"
                     if item.get("pulled_forward") else "")
            out.append(
                f"| `{item['id']}` | {item['name']}{early} | {item['phase']} | "
                f"{item['status']} | "
                f"{'yes' if item['core_required_1_0'] else 'no'} | "
                f"{', '.join(item['depends_on']) or '—'} |")
        out.append("")
    held = plan.get("held_items") or []
    if held:
        out += ["## Held behind a phase gate", "",
                "Represented, not ready. Each waits for the gate of the phase "
                "before it; the gate's reason is in `phase_gates` of the DAG.",
                "", "| id | requirement | phase | status | waits for gate |",
                "|---|---|---|---|---|"]
        for item in sorted(held, key=lambda i: (int(i["phase"][1:]), i["id"])):
            out.append(f"| `{item['id']}` | {item['name'][:70]} | {item['phase']} "
                       f"| {item['status']} | {', '.join(item['held_by'])} |")
        out.append("")
    if plan["unplaceable"]:
        out += ["## Unplaceable", "",
                "These could not be given a wave, which means a dependency "
                "cycle or a dependency that is not a requirement. A plan that "
                "dropped them to stay acyclic would be a plan with an "
                "impossible step in it.", ""]
        out += [f"* `{rid}`" for rid in plan["unplaceable"]] + [""]
    return "\n".join(out)


#: Item kinds the register does not track but a phase's gate still needs:
#: the plan's exit tests, invariants, validation runs and release gates.
_GATE_KINDS = frozenset({"gate", "invariant", "validation", "release_gate",
                         "release_property"})


def gate_bindings(items: list[dict], requirements: list[dict]) -> dict[str, list[str]]:
    """Which requirements claim each gate-kind plan item. Pure.

    Every inventory item whose kind is in `_GATE_KINDS` is a key; its value is
    the sorted ids of the requirements that claim it, as `authoritative_item`
    or under `also_covers` -- the claim `program_scope.dispositions` reads
    (O204). Only a requirement of the item's own phase binds it: a claim across
    phases is the completeness gate's refusal, not a measurement of this
    phase's exit. An unclaimed item maps to `[]`. Pass live requirements only;
    retired entries claim nothing.
    """
    claims: dict[str, set[tuple[str, str]]] = {}
    for r in requirements:
        refs = [r.get("authoritative_item")] + list(r.get("also_covers") or [])
        for ref in refs:
            ref = str(ref or "").strip()
            if ref:
                claims.setdefault(ref, set()).add((r["id"], r.get("target_phase")))
    return {i["id"]: sorted({rid for rid, ph in claims.get(i["id"], ())
                             if ph == i["phase"]})
            for i in items if i["kind"] in _GATE_KINDS}


def _register_requirements() -> list[dict]:
    """The register's live requirements, or `[]` without a register -- in
    which case every gate item is unbound, the pin's fail-closed answer."""
    path = HOH / "program/v3_3/REQUIREMENTS.json"
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("requirements") or []


#: P2's three "Checklist gate:" exit conditions (AUTHORITATIVE_PROGRAM_SCOPE lines
#: 107-109) and the programme instrument that answers each. No requirement claims
#: them, and a claim must not be added to meet them: it would read the condition met
#: when a requirement is PROVEN, with no supervisor review taken or current. They are
#: measured instead, from the register, the committed matrix and the preserved
#: reviews, by the tools that exist for the purpose; a claim, if one were made,
#: would still be required to be PROVEN besides, never stand in. An id missing here
#: is an unclaimed item, which stays unmeasured and unmet.
CHECKLIST_GATE_ITEMS = {
    "P2.checklist-gate.all-phase-blocking-checklist-items-verified-or-validly-waive":
        "items",
    "P2.checklist-gate.current-independent-checklist-supervisor-review-complete":
        "review",
    "P2.checklist-gate.checklistfsck-pass": "fsck",
}
_GATE_LABELS = {"items": "checklist items", "review": "supervisor review",
                "fsck": "ChecklistFsck"}
#: The phase those items belong to, and where its supervisor review is kept (the
#: board's row reads the same files).
CHECKLIST_GATE_PHASE = "P2"
REVIEW_RECORD = "dogfood/p2-gate/REVIEW.json"
REVIEW_ROOT = "dogfood/p2-gate"
#: Ceilings on what the gate reads and prints. A register, a matrix or a review
#: record beyond the first is not read; a reason beyond the second is cut.
_GATE_INPUT_MAX_BYTES = 32 * 1024 * 1024
_GATE_REASON_MAX = 240


def _bound(text, limit: int = _GATE_REASON_MAX) -> str:
    """One printable line of at most `limit` characters, for the DAG and the console."""
    one = "".join(c if c.isprintable() else "?" for c in " ".join(str(text).split()))
    return one if len(one) <= limit else one[:limit - 3] + "..."


def _gate_input(path: Path, what: str, *, kinds: tuple = (dict,),
                shape: str = "a JSON object") -> tuple[object | None, str]:
    """JSON of one of `kinds` from a regular file inside this repository, or why not.

    Never raises. A dangling link, a directory, a FIFO (never opened: reading one
    blocks), a file that resolves outside the repository, one over the ceiling,
    bytes that are not UTF-8, text that is not JSON (nesting deep enough to
    exhaust the parser is a `RuntimeError`) and JSON of another shape are each a
    reason, never an answer."""
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(HOH.resolve()):
            return None, f"{what} resolves outside the repository"
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode):
            return None, f"{what} is not a regular file"
        if info.st_size > _GATE_INPUT_MAX_BYTES:
            return None, f"{what} is larger than {_GATE_INPUT_MAX_BYTES} bytes"
        body = json.loads(resolved.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return None, f"{what} is missing"
    except (OSError, ValueError, RuntimeError) as exc:
        return None, f"{what} cannot be read ({type(exc).__name__})"
    if not isinstance(body, kinds):
        return None, f"{what} is not {shape}"
    return body, ""


def _programme_supervisor():
    """`tools/programme_supervisor.py`, which carries the checklist binding
    (`.programme_checklist`) with it. Loaded per call, so a tree is never judged
    by a stale copy."""
    spec = importlib.util.spec_from_file_location(
        "programme_supervisor", HERE / "programme_supervisor.py")
    ps = importlib.util.module_from_spec(spec)
    sys.modules["programme_supervisor"] = ps
    spec.loader.exec_module(ps)
    return ps


def _readiness():
    """`tools/readiness.py`, loaded for one row: the board's supervisor-review
    check is the one instrument that holds every honesty rule about a review
    (what the supervisor returned, the brief it was given, the preserved history,
    a vouched-for register). The gate asks it rather than copy it."""
    spec = importlib.util.spec_from_file_location(
        "readiness_for_build_plan", HERE / "readiness.py")
    rd = importlib.util.module_from_spec(spec)
    sys.modules["readiness_for_build_plan"] = rd
    spec.loader.exec_module(rd)
    return rd


def _other_tree(*tools) -> str:
    """Why the loaded `tools` do not all judge this repository, or `""`.

    The checklist binding and the board's row each derive their own `HOH`, and read the
    preserved reviews and the review record below it. The gate's register, matrix and
    record come from build_plan's `HOH`; a tool that judges another tree would give the
    three conditions two different trees' answers, so none is accepted. A tool with no
    usable `HOH` counts as another tree."""
    here = HOH.resolve()
    for tool in tools:
        try:
            same = Path(tool.HOH).resolve() == here
        except (AttributeError, TypeError, OSError, ValueError, RuntimeError):
            same = False
        if not same:
            name = getattr(tool, "__name__", "a loaded tool")
            return f"{name} judges another tree than this repository"
    return ""


def _preserved_review_problems(pc) -> list[str]:
    """Preserved reviews whose findings the checklist binding would skip unread.

    `programme_checklist` passes over a review directory whose `findings.json` does not
    parse, and over entries of it that are not objects -- so an open finding could
    be hidden by spoiling the file that raised it. The gate refuses to close over a
    review it cannot read. Same directories as the binding reads (stamped, in this
    tree, not links); a missing `findings.json` is not a review and is not a problem."""
    root = HOH / REVIEW_ROOT
    if root.is_symlink() or root.exists():
        try:
            inside = root.resolve(strict=True).is_relative_to(HOH.resolve())
        except (OSError, ValueError, RuntimeError):
            inside = False
        if not inside:
            return [f"{REVIEW_ROOT} resolves outside the repository"]
    if not root.is_dir():
        return []
    problems = []
    for directory in sorted(root.iterdir(), key=lambda d: d.name):
        if (directory.is_symlink() or not directory.is_dir()
                or not pc._STAMP.match(directory.name)):
            continue
        file = directory / "findings.json"
        if not (file.exists() or file.is_symlink()):
            continue
        shown = f"{directory.name}/findings.json"
        raw, why = _gate_input(file, shown, kinds=(dict, list),
                               shape="a JSON object or list")
        if raw is None:
            problems.append(why)
            continue
        if isinstance(raw, dict) and not isinstance(raw.get("findings"), list):
            problems.append(f"{shown} is an object without a findings list")
            continue
        returned = raw["findings"] if isinstance(raw, dict) else raw
        if any(not isinstance(f, dict) for f in returned):
            problems.append(f"{shown} does not hold a list of finding objects")
        elif any(not isinstance(f.get("finding_id"), str) or not f["finding_id"].strip()
                 for f in returned):
            # the resolution state skips an entry without an id, so it would be unread
            problems.append(f"{shown} holds a finding without a finding_id")
    return problems


def _checklist_verdicts(ps, register: dict, matrix: dict, unread: str) -> dict:
    """The first and third conditions, from the checklist this programme runs on."""
    phase = CHECKLIST_GATE_PHASE
    pc = ps.programme_checklist
    try:
        snapshot, evidence, _items = ps.reconciled(register, matrix)
    except ValueError as exc:
        why = _bound(f"the checklist is not sound, so no verdict is reported: {exc}")
        return {"items": (False, why), "fsck": (False, why)}
    gate = pc.phase_gate(snapshot, phase, evidence=evidence)
    if gate.closed:
        items = (True, f"every blocking {phase} item is VERIFIED or WAIVED")
    else:
        ids = [b.item_id for b in gate.blockers]
        more = f" and {len(ids) - 5} more" if len(ids) > 5 else ""
        items = (False, _bound(f"{len(ids)} {phase} blocker(s) neither VERIFIED nor "
                               f"WAIVED: {', '.join(ids[:5])}{more}"))
    if unread:
        return {"items": items, "fsck": (False, unread)}
    try:
        found = pc._preserved_release_blocking_findings()
    except pc.ReviewRootOutside as exc:
        return {"items": items, "fsck": (False, _bound(exc))}
    problems = pc.fsck(snapshot.items, snapshot=snapshot,
                       requirements=ps.declared_requirement_refs(register),
                       release_blocking_findings=found,
                       known_digests={ps.plan_revision(register)})
    if problems:
        return {"items": items, "fsck": (False, _bound(
            f"{len(problems)} problem(s), e.g. {problems[0]}"))}
    return {"items": items, "fsck": (True, "fsck finds no problem and no unresolved "
                                           "release-blocking finding")}


def _review_causes(record: dict, register: dict, matrix: dict) -> list[str]:
    """Why `record` is not a current, complete review of P2, in its own terms.

    A diagnostic and never a verdict -- the board's row decides, and its cell is cut
    at 400 characters, which can leave the cause unprinted."""
    try:
        ps = _programme_supervisor()
        snapshot, _evidence, _items = ps.reconciled(register, matrix)
        loaded = ps.load_record(record, snapshot=snapshot)
        causes = ([f"the record reviews {loaded.phase}, not {CHECKLIST_GATE_PHASE}"]
                  if loaded.phase != CHECKLIST_GATE_PHASE else [])
        if loaded.blocking_findings:
            causes.append(f"{len(loaded.blocking_findings)} blocking finding(s)")
        return causes + ps.stale_against(
            loaded, snapshot=snapshot, matrix_digest=ps.matrix_digest(matrix))
    except Exception as exc:  # noqa: BLE001 - a diagnostic that fails says so
        return [f"the record is refused ({type(exc).__name__})"]


def _review_verdict(register: dict, matrix: dict, unread: str) -> tuple[bool, str]:
    """The second condition: a record the gate may read, and the board's
    `programme_supervisor` row reads PASS over it."""
    if unread:
        return False, unread
    record, why = _gate_input(HOH / REVIEW_RECORD, "the review record")
    if record is None:
        return False, _bound(why)
    rd = _readiness()
    other = _other_tree(rd)
    if other:
        return False, _bound(f"the board's programme_supervisor row cannot be read: {other}")
    row = rd.row_programme_supervisor()
    detail = getattr(row, "value_", "")
    if row.state == rd.PASS:
        return True, (f"{_bound(detail, 160)}; independence is a property of the "
                      "dispatch and is not measured here")
    causes = _review_causes(record, register, matrix)
    return False, _bound(f"the board's programme_supervisor row is {row.state}: "
                         + ("; ".join(causes) if causes else detail))


def checklist_gate_measures() -> dict[str, tuple[bool, str]]:
    """P2's three checklist-gate questions put to the programme's own instruments.

    `{"items" | "review" | "fsck": (met, reason)}`, from the committed register
    and capability matrix and the preserved review under `dogfood/p2-gate`:

    * `items` -- `hoh.checklist.phase_gate` over the reconciled checklist
      (`programme_supervisor.reconciled`): every blocking P2 item VERIFIED or
      validly WAIVED, re-derived from evidence (a waiver is not handed in, so it
      does not meet the condition: the stricter reading);
    * `fsck` -- `hoh.checklist.fsck` over that checklist, with the release-blocking
      findings the preserved reviews leave open (the check no phase may close over),
      and none of those reviews unreadable;
    * `review` -- the board's `programme_supervisor` row: a record that loads, is
      about P2, is complete with no blocking finding, is not stale against the
      matrix digest or the snapshot seal, is bound to the findings and the brief, and
      rests on a vouched-for register. Whether the reviewer was independent is a
      property of the dispatch; no file shows it.

    Never raises: a register, matrix or record that is missing, unreadable,
    malformed, oversize or outside the repository, and any instrument that fails,
    is `(False, reason)`. Reasons are one line and bounded.
    """
    register, why_r = _gate_input(HOH / "program/v3_3/REQUIREMENTS.json",
                                  "the requirement register")
    matrix, why_m = _gate_input(
        HOH / "program/v3_3/VERIHARNESS_CAPABILITY_MATRIX.json",
        "the capability matrix")
    if register is None or matrix is None:
        why = _bound("; ".join(w for w in (why_r, why_m) if w))
        return {kind: (False, why) for kind in _GATE_LABELS}
    out: dict[str, tuple[bool, str]] = {}
    try:
        ps = _programme_supervisor()
        other = _other_tree(ps.programme_checklist)
        if other:
            raise RuntimeError(other)
        problems = _preserved_review_problems(ps.programme_checklist)
        unread = (_bound("a preserved review cannot be read: " + "; ".join(problems))
                  if problems else "")
        out.update(_checklist_verdicts(ps, register, matrix, unread))
    except Exception as exc:  # noqa: BLE001 - an instrument that fails is an unmet condition
        why = _bound(f"the checklist could not be derived ({type(exc).__name__}): {exc}")
        out.update(items=(False, why), fsck=(False, why))
        unread = why
    try:
        out["review"] = _review_verdict(register, matrix, unread)
    except Exception as exc:  # noqa: BLE001 - likewise
        out["review"] = (False, _bound(
            f"the review could not be evaluated ({type(exc).__name__}): {exc}"))
    return out


def _gate_measurements(provider) -> dict[str, tuple[bool, str]]:
    """The provider's answers, each a real `(bool, str)`; anything else is unmet.
    `met` must be the bool `True`: a truthy string or number does not open a gate."""
    try:
        got = (provider or checklist_gate_measures)()
    except Exception as exc:  # noqa: BLE001 - an instrument that fails is an unmet condition
        got = {}
        failure = f"the instrument failed ({type(exc).__name__}): {exc}"
    else:
        failure = "the instrument returned no usable answer"
    out = {}
    for kind in _GATE_LABELS:
        value = got.get(kind) if isinstance(got, dict) else None
        if (isinstance(value, tuple) and len(value) == 2
                and isinstance(value[0], bool) and isinstance(value[1], str)):
            out[kind] = (value[0], _bound(value[1]))
        else:
            out[kind] = (False, _bound(failure))
    return out


def phase_gates(capabilities: list[dict] | None = None,
                requirements: list[dict] | None = None, *,
                checklist_measures=None,
                ) -> tuple[list[str], dict[str, tuple[bool, str]]]:
    """The plan's phase order and, per phase, whether its non-requirement
    conditions are met. Three sources, each for one question:

    * the **inventory** says which conditions a phase has -- its gate-kind
      items (`_GATE_KINDS`), and P0's deliverables;
    * the **register** says who claims them (`gate_bindings`): a requirement
      of the same phase naming the item as `authoritative_item` or under
      `also_covers`. `requirements=None` reads the register's live ones;
    * the **matrix** -- `capabilities`, its measured rows -- says whether they
      are met.

    P0's condition is its deliverables existing. A bound item is met only when
    every requirement bound to it is measured exactly PROVEN: a waiver does
    not meet an exit condition, and a bound requirement with no row is not
    measured and unmet. An unbound item is an exit condition nothing here
    measures yet, and stays unmet -- P3's invariants, P7's whole product proof.
    That holds later phases shut, which is what "the authoritative
    implementation sequence" means, rather than letting a v2.0 backend show up
    as ready beside a v0.2 gap.

    The exception is the item named in `CHECKLIST_GATE_ITEMS`: P2's three
    checklist gates are measured by the programme's own instruments
    (`checklist_gate_measures`, or `checklist_measures` when given: a callable
    returning `{"items" | "review" | "fsck": (met, reason)}`). Such an
    item is met only when its instrument says `True` -- and, if a requirement
    also claims it, when that requirement is PROVEN as well: a claim never
    stands in for the instrument. An unmet one is reported with its reason, not
    as unmeasured. A phase with no gate-kind item is absent from the result.
    """
    scope = _scope()
    if scope is None:
        return [], {}
    if requirements is None:
        requirements = _register_requirements()
    status = {c["id"]: c.get("status") for c in capabilities or []}
    bindings = gate_bindings(scope["items"], requirements)
    order = [p["phase"] for p in scope["phases"]]
    gates: dict[str, tuple[bool, str]] = {}
    for ph in order:
        items = [i for i in scope["items"] if i["phase"] == ph]
        if ph == "P0":
            files = [i["text"] for i in items if i["kind"] == "baseline_deliverable"]
            missing = [f for f in files if not (HOH / "program/v3_3" / f).is_file()]
            gates[ph] = (not missing,
                         f"{len(files) - len(missing)} of {len(files)} P0 "
                         "deliverables present" + (f"; missing {missing[:3]}"
                                                   if missing else ""))
            continue
        conditions = [i for i in items if i["kind"] in _GATE_KINDS]
        if not conditions:
            continue
        instrument = {i["id"]: CHECKLIST_GATE_ITEMS[i["id"]] for i in conditions
                      if i["id"] in CHECKLIST_GATE_ITEMS}
        measured = _gate_measurements(checklist_measures) if instrument else {}
        unmeasured = [i for i in conditions
                      if not bindings[i["id"]] and i["id"] not in instrument]
        instrument_unmet = [(instrument[i["id"]], measured[instrument[i["id"]]][1])
                            for i in conditions if i["id"] in instrument
                            and not measured[instrument[i["id"]]][0]]
        unmet_bound = 0
        not_proven: list[str] = []
        for i in conditions:
            short = [rid for rid in bindings[i["id"]] if status.get(rid) != "PROVEN"]
            if short:
                unmet_bound += 1
                not_proven += [rid for rid in short if rid not in not_proven]
        if not unmeasured and not unmet_bound and not instrument_unmet:
            gates[ph] = (True, f"{len(conditions)} exit condition(s), each bound "
                               "to a PROVEN requirement" if not instrument else
                               f"{len(conditions)} exit condition(s) met: "
                               f"{len(conditions) - len(instrument)} bound to a "
                               f"PROVEN requirement, {len(instrument)} measured by "
                               "the checklist instruments")
            continue
        why = []
        if unmet_bound:
            named = ", ".join(f"{rid} {status.get(rid) or 'not measured'}"
                              for rid in not_proven[:5])
            if len(not_proven) > 5:
                named += f" and {len(not_proven) - 5} more"
            why.append(f"{unmet_bound} exit condition(s) bound to requirement(s) "
                       f"not PROVEN: {named}")
        if instrument_unmet:
            named = "; ".join(f"{_GATE_LABELS[kind]}: {reason}"
                              for kind, reason in instrument_unmet)
            why.append(f"{len(instrument_unmet)} checklist-gate exit condition(s) "
                       f"measured by the checklist instruments and unmet -- {named}")
        if unmeasured:
            why.append(f"{len(unmeasured)} exit condition(s) nothing "
                       "here measures yet, e.g. "
                       f"{unmeasured[0]['text'][:60]!r}")
        gates[ph] = (False, "; ".join(why))
    return order, gates


def start_label(rid: str | None, status: str, waits_for_gate: str | None,
                open_gates: set[str] | list[str], ready: set[str], held: set[str],
                pulled: set[str]) -> str:
    """What the dispositions table says about whether a requirement can start.

    A1: PROVEN is "done" only when the gate in front of the requirement's phase
    is open. Proven earlier -- pulled forward or not (O238 review, splan-a B2:
    P5-17 read "done" with only P0/P1 open) -- it is IMPLEMENTED EARLY, and the
    phase requirement is satisfied only once that gate opens."""
    if not rid:
        return "—"
    if status == "PROVEN" and waits_for_gate and waits_for_gate not in open_gates:
        return (f"IMPLEMENTED EARLY; phase requirement satisfied only "
                f"after gate {waits_for_gate}")
    if status == "PROVEN":
        return "done"
    if rid in pulled and rid in ready:
        return "ready now (pulled forward)"
    if rid in ready:
        return "ready now"
    if rid in held:
        return f"held by gate {waits_for_gate}"
    return "after its dependencies"


def render_dispositions(plan: dict, body: dict) -> str | None:
    """Every plan item and what became of it, with the measured status and
    the gate it waits for joined in. O196: silence is not a disposition."""
    ps = _program_scope()
    scope = _scope()
    if scope is None:
        return None
    register = json.loads((HOH / "program/v3_3/REQUIREMENTS.json").read_text())
    status = {r["id"]: r["status"] for r in body["capabilities"]}
    ready = set(plan.get("ready_now") or [])
    held = {i["id"] for i in plan.get("held_items") or []}
    rows = ps.dispositions(scope, register)
    pulled = {r["id"] for r in register.get("requirements") or []
              if _pulled_forward(r)}
    open_gates = set(plan.get("open_gates") or [])
    out = ["# Programme dispositions (V3.3)", "",
           "Generated by `python3 tools/build_plan.py`. One row per item of the",
           "pinned production line and of its pinned operator amendments (A1):",
           "what it is, which requirement implements it,",
           "that requirement's measured status, and whether it can start. Held",
           "is represented, not absent. Nothing here is a state somebody set.", "",
           f"Measured at {plan['measured_at_utc']}.", ""]
    for ph in [p["phase"] for p in scope["phases"]]:
        these = [r for r in rows if r["phase"] == ph]
        caps = [r for r in these if r["capability"]]
        out += [f"## {ph} — {len(these)} item(s), {len(caps)} capabilit"
                f"{'y' if len(caps) == 1 else 'ies'}", "",
                "| line | item | kind | requirement | status | can start |",
                "|---|---|---|---|---|---|"]
        for r in these:
            rid = r["requirement"]
            st = status.get(rid, "—") if rid else "—"
            if r["kind"] == "baseline_deliverable":
                present = (HOH / "program/v3_3" / r["text"]).is_file()
                start = "file present" if present else "FILE MISSING"
            elif r["kind"] == "measurement":
                start = measurement_status(r["item"])
            else:
                start = start_label(rid, st, r["waits_for_gate"], open_gates,
                                    ready, held, pulled)
            text = r["item"].split(".", 2)[-1]
            if r.get("amendment"):
                text = f"{r['amendment']}: " + r["item"].split(".", 3)[-1]
            out.append(f"| {r['line']} | `{text[:48]}` | {r['kind']} | "
                       f"{rid or '—'} | {st} | {start} |")
        out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dag", type=Path, default=None)
    ap.add_argument("--checklist", type=Path, default=None)
    ap.add_argument("--markdown", type=Path, default=None)
    ap.add_argument("--dispositions", type=Path, default=None)
    args = ap.parse_args(argv)

    cm = _matrix()
    try:
        body = cm.measure()
    except cm.RegisterMissing as exc:
        print(str(exc), file=sys.stderr)
        return 3
    # `depends_on` lives in the register and the matrix passes it through.
    register = json.loads((HOH / "program/v3_3/REQUIREMENTS.json").read_text())
    deps = {r["id"]: r.get("depends_on") or [] for r in register["requirements"]}
    for r in body["capabilities"]:
        r["depends_on"] = deps.get(r["id"], [])

    phase_order, gates = phase_gates(body["capabilities"], register["requirements"])
    plan = build(body, phase_order, gates)
    check = checklist(plan)
    for wave in plan["waves"]:
        blocking = sum(1 for i in wave["items"] if i["core_required_1_0"])
        print(f"  wave {wave['wave']}: {len(wave['items'])} item(s), "
              f"{blocking} phase-blocking")
    print(f"\n{plan['open_count']} open requirement(s) in "
          f"{len(plan['waves'])} wave(s); {len(plan['held_by_phase_gate'])} "
          "held behind a phase gate")
    print(f"ready now: {', '.join(plan['ready_now']) or 'nothing'}")
    for ph, g in plan["phase_gates"].items():
        if g["blocking_open"] or g["external_met"] is False:
            why = (f"{len(g['blocking_open'])} blocking open"
                   + (f"; {g['external']}" if g["external_met"] is False else ""))
            print(f"  gate {ph}: {why}")
    if plan["unplaceable"]:
        print(f"UNPLACEABLE: {', '.join(plan['unplaceable'])}")
    if plan["dangling_dependencies"]:
        print(f"DANGLING: {plan['dangling_dependencies']}")
    for target, payload in ((args.dag, plan), (args.checklist, check)):
        if target:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, indent=2) + "\n")
            print(f"wrote {target}")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render(plan, check) + "\n")
        print(f"wrote {args.markdown}")
    if args.dispositions:
        text = render_dispositions(plan, body)
        if text is not None:
            args.dispositions.parent.mkdir(parents=True, exist_ok=True)
            args.dispositions.write_text(text + "\n")
            print(f"wrote {args.dispositions}")
    return 1 if (plan["unplaceable"] or plan["dangling_dependencies"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
