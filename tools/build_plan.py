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
import hashlib
import importlib.util
import json
import re
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
          phase_gates: dict[str, tuple[bool, str]] | None = None, *,
          closures: dict[str, dict] | None = None,
          watch: dict[str, dict] | None = None,
          closure_notes: dict[str, str] | None = None) -> dict:
    """The DAG and the checklist, from one measured matrix.

    With `phase_order`, the plan's sequence is part of the graph: every
    requirement in phase N waits on a gate node for phase N-1, and that gate
    waits on every blocking requirement of N-1 that is not yet satisfied.
    `phase_gates` supplies the answer for phases that have no requirements to
    wait on -- P0's deliverables exist or they do not; P7's product proof has
    been run or it has not. A phase absent from it with no open blocking work
    is open.

    `closures` (A7, from `verified_closures`) names the phases that are **closed**:
    such a phase has no blocking work and no unmet condition for the gates behind
    it, and its state carries `closed_at`, `closure_entry`, `release_tag` and the
    `watch` -- the phase's open blocking requirements as `blocking_open_live` beside
    what `phase_gate_report` measured -- so what a closure stopped holding shut is
    still in the plan. `closure_notes` is the reason a closure record that exists
    was not honoured; that phase is judged as it would be without one.
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
            closure = (closures or {}).get(ph)
            if isinstance(closure, dict):
                # Closed at a commit (A7): its open requirements are the watch, not
                # a hold on the gates behind it.
                gate_state[ph] = {
                    "blocking_open": [],
                    "external": (given[1] if given else None),
                    "external_met": True,
                    "closed_at": closure.get("closed_at"),
                    "closure_entry": closure.get("entry"),
                    "release_tag": closure.get("tag"),
                    "watch": {**((watch or {}).get(ph) or {}),
                              "blocking_open_live": blockers,
                              "regression_watch": closure.get("watch_state")}}
                nodes[f"gate:{ph}"] = ([f"gate:{phase_order[i - 1]}"] if i else [])
                continue
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
            if (closure_notes or {}).get(ph):
                gate_state[ph]["closure_ignored"] = closure_notes[ph]
        for rid, r in open_.items():
            i = phase_order.index(r["target_phase"]) if r["target_phase"] in phase_order else -1
            if i > 0 and not _pulled_forward(r):
                nodes[rid] = nodes[rid] + [f"gate:{phase_order[i - 1]}"]
        for ph, (met, _) in gates.items():
            if not met and not isinstance((closures or {}).get(ph), dict):
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
    # F13/M9 of the preserved P2 reviews: session succession is an ingredient of
    # the exit condition "controller/session replacement", and a capsule that has
    # drifted is precisely the state in which a successor is handed a wrong
    # description of the tree. The item stays claimed by its requirement (P2-35,
    # which must be PROVEN besides, as for every item in this map) and is in
    # addition measured by `tools/succession.py verify` over the committed capsule.
    "P2.exit-includes.controller-session-replacement": "succession",
}
_GATE_LABELS = {"items": "checklist items", "review": "supervisor review",
                "fsck": "ChecklistFsck", "succession": "succession capsule"}
#: The kinds `checklist_gate_measures` answers; `succession` has an instrument of
#: its own (`succession_measure`), asked only when an item maps to it.
_CHECKLIST_KINDS = ("items", "review", "fsck")
#: The succession capsule the gate verifies, as `tools/succession.py` writes and
#: reads it (the two spellings are pinned equal by the tests).
SUCCESSION_CAPSULE = "dogfood/succession/SUCCESSION.json"
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


def _succession():
    """`tools/succession.py`, loaded per call like the board: its `check` is the one
    place that knows what a capsule field means and how it is re-derived."""
    spec = importlib.util.spec_from_file_location(
        "succession_for_build_plan", HERE / "succession.py")
    su = importlib.util.module_from_spec(spec)
    sys.modules["succession_for_build_plan"] = su
    spec.loader.exec_module(su)
    return su


def _regression_watch():
    """`tools/regression_watch.py`, loaded per call like the other tools: what the
    watch records and requests say is read through it, from the blobs at HEAD."""
    spec = importlib.util.spec_from_file_location(
        "regression_watch_for_build_plan", HERE / "regression_watch.py")
    rw = importlib.util.module_from_spec(spec)
    sys.modules["regression_watch_for_build_plan"] = rw
    spec.loader.exec_module(rw)
    return rw


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
            loaded, snapshot=snapshot, matrix_digest=ps.matrix_digest(matrix),
            matrix=matrix)
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
        return {kind: (False, why) for kind in _CHECKLIST_KINDS}
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


# --------------------------------------------------------------------------- #
# a closed phase (amendment A7): the closure record and what the gate reads of it
# --------------------------------------------------------------------------- #

#: A7: "a phase is closed at the commit of its closure record; later changes create
#: regression obligations, not a reopened phase". The record is the machine-readable
#: twin of the records entry (for P2: ledger O268) and sits in the phase's gate
#: directory. The gate believes it only after `closure_record` has re-derived
#: every digest it names from the blobs at the commit it names.
CLOSURE_FILE = "CLOSURE.json"
CLOSURE_VERSION = 1
#: An explicit reopen decision (A7 rule 3) is a committed `<gate dir>/reopen/*.json`
#: naming the closure; nothing in this tool writes one.
REOPEN_DIR = "reopen"
#: More reopen records than this are not read: each is one git read, and a directory
#: that grows without bound must not turn the gate into a long wait. Refused, not cut.
REOPEN_MAX = 64
CLOSURE_KEYS = frozenset({
    "what", "record_version", "phase", "closure_entry", "closure_date", "amendment",
    "closed_at_commit", "sealing_commit", "capsule_commit", "release", "instruments",
    "rows"})
#: The instrument files a closure names, by their fixed repository path. The record
#: carries digests, never paths: a record cannot point the gate at another file.
MATRIX_JSON = "program/v3_3/VERIHARNESS_CAPABILITY_MATRIX.json"
REGISTER_JSON = "program/v3_3/REQUIREMENTS.json"
DURABILITY_RECORD = "dogfood/p2-durability/P2_DURABILITY_ACCEPTANCE.json"
LEDGER = "DOGFOOD_LEDGER.md"
AMENDMENTS_ROOT = "program/v3_3/amendments"
_PHASE = re.compile(r"P[0-9]{1,2}")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ENTRY = re.compile(r"O[0-9]{1,5}")
_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+-]{0,24}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_AMENDMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}")
_AMENDMENT_ID = re.compile(r"A[0-9]{1,3}")
#: A closing commit as a reopen record may spell it: the full id or an abbreviation of at
#: least seven hex digits (the gate itself prints twelve), in either case.
_COMMIT_PREFIX = re.compile(r"[0-9a-fA-F]{7,40}")
_PHASE_WORD = re.compile(r"[Pp]0*([0-9]{1,2})")


def _phase_of(value) -> str | None:
    """A phase as a record may spell it (`P2`, `p2`, ` P02 `) in its one canonical
    spelling (`P2`), or None when it is not a phase name at all."""
    if not isinstance(value, str):
        return None
    m = _PHASE_WORD.fullmatch(value.strip())
    return f"P{int(m.group(1))}" if m else None


class _Refused(Exception):
    """A closure record the gate does not honour; the message is the reason."""


def _is_object(value) -> bool:
    return isinstance(value, dict)


def _field(body: dict, key: str, what: str, pattern=None):
    """`body[key]`: a non-empty string, matching `pattern` entirely when one is given."""
    value = body.get(key)
    if not isinstance(value, str) or not value or (
            pattern is not None and not pattern.fullmatch(value)):
        raise _Refused(f"{what}: `{key}` is missing or not in its shape")
    return value


def _section(body: dict, key: str, keys: set, what: str) -> dict:
    value = body.get(key)
    if not _is_object(value) or set(value) != keys:
        raise _Refused(f"{what}: `{key}` is missing or does not hold exactly "
                       f"{sorted(keys)}")
    return value


def _read_object(blob: bytes, what: str) -> dict:
    try:
        body = json.loads(blob.decode("utf-8"))
    except (ValueError, RuntimeError) as exc:
        raise _Refused(f"{what} cannot be read ({type(exc).__name__})") from None
    if not _is_object(body):
        raise _Refused(f"{what} is not a JSON object")
    return body


def _git_blob(su, rev: str, path: str, what: str) -> bytes:
    """The bytes of `path` as committed at `rev`, or a refusal naming the exit."""
    rc, text = su._git_rc("cat-file", "blob", f"{rev}:{path}")
    if rc != 0:
        raise _Refused(f"{what} is not a file of {rev[:12]} (git exit {rc})")
    data = text.encode("utf-8", "surrogateescape")
    if len(data) > _GATE_INPUT_MAX_BYTES:
        raise _Refused(f"{what} at {rev[:12]} is larger than {_GATE_INPUT_MAX_BYTES} bytes")
    return data


def _git_commit(su, rev: str, what: str) -> str:
    rc, out = su._git_rc("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    full = out.strip()
    if rc != 0 or not _FULL_SHA.fullmatch(full):
        raise _Refused(f"{what} {rev[:12]} is not a commit of this repository")
    return full


def _git_ancestor(su, older: str, newer: str, what: str) -> None:
    """`older` is `newer` or one of its ancestors; otherwise refuse (exit 1 is "no",
    any other exit is "git could not say" -- never a pass)."""
    rc, _ = su._git_rc("merge-base", "--is-ancestor", older, newer)
    if rc == 1:
        raise _Refused(f"{what}: {older[:12]} is not an ancestor of {newer[:12]}")
    if rc != 0:
        raise _Refused(f"{what}: git could not relate {older[:12]} to {newer[:12]} "
                       f"(exit {rc})")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _same_digest(data: bytes, claimed: str, what: str, rev: str) -> None:
    if _digest(data) != claimed:
        raise _Refused(f"{what} at {rev[:12]} has sha256 {_digest(data)[:12]}..., "
                       f"the record says {claimed[:12]}...")


def _verify_closure(phase: str) -> dict:
    """The closure of `phase` as the committed record states it, every claim
    re-derived from the blobs of the commit it names; raises `_Refused` otherwise.

    Order matters: the tool must judge this tree (`_other_tree`), it must be a
    repository of its own (`_own_repository`, *before* any `git` read: on an archive
    copy `git` answers for whatever repository lies above it), and the record must
    be the one committed at HEAD and the one in the working tree."""
    if not isinstance(phase, str) or not _PHASE.fullmatch(phase):
        raise _Refused(f"{phase!r} is not a phase name")
    su = _succession()
    other = _other_tree(su)
    if other:
        raise _Refused(other)
    own, why = su._own_repository()
    if not own:
        raise _Refused("ENVIRONMENT_GAP: " + why)
    gate_dir = f"dogfood/{phase.lower()}-gate"
    path = f"{gate_dir}/{CLOSURE_FILE}"
    head = _git_commit(su, "HEAD", "HEAD")
    blob = _git_blob(su, head, path, "the closure record")
    working, why = _gate_input(HOH / path, "the closure record")
    if working is None:
        raise _Refused(why)
    if (HOH / path).resolve(strict=True).read_bytes() != blob:
        raise _Refused("the closure record in the working tree differs from the one "
                       "committed at HEAD; the gate reads the committed one")
    rec = _read_object(blob, "the closure record")
    what = "the closure record"
    if set(rec) != CLOSURE_KEYS:
        raise _Refused(f"{what} does not hold exactly its keys (extra {sorted(set(rec) - CLOSURE_KEYS)[:3]}, "
                       f"missing {sorted(CLOSURE_KEYS - set(rec))[:3]})")
    if rec["record_version"] != CLOSURE_VERSION or isinstance(rec["record_version"], bool):
        raise _Refused(f"{what} is version {rec['record_version']!r}, not {CLOSURE_VERSION}")
    if rec["phase"] != phase:
        raise _Refused(f"{what} is about {rec['phase']!r}, not {phase}")
    _field(rec, "what", what)
    entry = _field(rec, "closure_entry", what, _ENTRY)
    _field(rec, "closure_date", what, _DATE)
    x = _field(rec, "closed_at_commit", what, _FULL_SHA)
    sealing = _field(rec, "sealing_commit", what, _FULL_SHA)
    capsule_commit = _field(rec, "capsule_commit", what, _FULL_SHA)
    ins = rec["instruments"]
    if not _is_object(ins) or set(ins) != {"matrix", "snapshot", "review", "register",
                                           "capsule", "durability"}:
        raise _Refused(f"{what}: `instruments` does not name exactly the matrix, snapshot, "
                       "review, register, capsule and durability record")
    matrix_c = _section(ins, "matrix", {"file_sha256", "matrix_digest"}, what)
    snap_c = _section(ins, "snapshot", {"file_sha256", "seal"}, what)
    review_c = _section(ins, "review", {"file_sha256", "review_id", "snapshot_seal",
                                        "matrix_digest"}, what)
    register_c = _section(ins, "register", {"file_sha256"}, what)
    capsule_c = _section(ins, "capsule", {"file_sha256", "internal_commit"}, what)
    dur_c = _section(ins, "durability", {"file_sha256", "evidence_head"}, what)
    for group in (matrix_c, snap_c, review_c, register_c, capsule_c, dur_c):
        for key in ("file_sha256", "matrix_digest", "seal", "snapshot_seal"):
            if key in group:
                _field(group, key, what, _SHA256)
    for group, key in ((capsule_c, "internal_commit"), (dur_c, "evidence_head")):
        _field(group, key, what, _FULL_SHA)
    review_id = _field(review_c, "review_id", what)
    rows_c = _section(rec, "rows", {"phase_rows", "proven"}, what)
    release_c = _section(rec, "release", {"tag", "commit"}, what)
    tag = _field(release_c, "tag", what, _TAG)
    release_commit = _field(release_c, "commit", what, _FULL_SHA)
    amend = _section(rec, "amendment", {"id", "file", "file_sha256", "ruling_file",
                                        "ruling_sha256"}, what)
    for key in ("file_sha256", "ruling_sha256"):
        _field(amend, key, what, _SHA256)
    for key in ("file", "ruling_file"):
        _field(amend, key, what, _AMENDMENT_NAME)
    _field(amend, "id", what, _AMENDMENT_ID)
    if not (amend["file"].startswith(amend["id"] + "_")
            and amend["ruling_file"].startswith(amend["id"] + "_")):
        raise _Refused(f"{what}: amendment {amend['id']} is not the one its files name")

    # the commits: X is on this line, S and C lead to it, the release descends from it
    if _git_commit(su, x, "the closing commit") != x:
        raise _Refused(f"{what}: `closed_at_commit` is not a full commit id")
    _git_ancestor(su, x, head, "the closing commit is not in this tree's history")
    _git_ancestor(su, sealing, capsule_commit, "the sealing commit does not lead to the capsule")
    _git_ancestor(su, capsule_commit, x, "the capsule commit does not lead to the closing commit")
    _git_ancestor(su, x, release_commit, "the release does not descend from the closing commit")
    _git_ancestor(su, release_commit, head, "the release is not in this tree's history")
    rc, out = su._git_rc("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}")
    if rc == 0 and out.strip() != release_commit:
        raise _Refused(f"the tag {tag} here names {out.strip()[:12]}, the record says "
                       f"{release_commit[:12]}")
    #: Printed only when it resolves here and names the release commit: a name that
    #: nothing in this repository backs is a claim, and the gate does not repeat it.
    tag_here = tag if rc == 0 else None

    # the instruments at X: digests of the files, then the facts the record states
    def at_x(file: str, group: dict, name: str) -> bytes:
        data = _git_blob(su, x, file, name)
        _same_digest(data, group["file_sha256"], name, x)
        return data

    matrix_blob = at_x(MATRIX_JSON, matrix_c, "the capability matrix")
    snap_blob = at_x(f"{gate_dir}/CHECKLIST_SNAPSHOT.json", snap_c, "the checklist snapshot")
    review_blob = at_x(f"{gate_dir}/REVIEW.json", review_c, "the supervisor review")
    register_blob = at_x(REGISTER_JSON, register_c, "the requirement register")
    capsule_blob = at_x(SUCCESSION_CAPSULE, capsule_c, "the succession capsule")
    dur_blob = at_x(DURABILITY_RECORD, dur_c, "the durability record")
    for file, claimed, name in ((MATRIX_JSON, matrix_c, "the capability matrix"),
                                (f"{gate_dir}/CHECKLIST_SNAPSHOT.json", snap_c,
                                 "the checklist snapshot"),
                                (f"{gate_dir}/REVIEW.json", review_c, "the supervisor review")):
        _same_digest(_git_blob(su, sealing, file, name), claimed["file_sha256"], name, sealing)
    _same_digest(_git_blob(su, capsule_commit, SUCCESSION_CAPSULE, "the succession capsule"),
                 capsule_c["file_sha256"], "the succession capsule", capsule_commit)
    for key, name in (("file", "the amendment"), ("ruling_file", "the operator ruling")):
        digest_key = "file_sha256" if key == "file" else "ruling_sha256"
        _same_digest(_git_blob(su, x, f"{AMENDMENTS_ROOT}/{amend[key]}", name),
                     amend[digest_key], name, x)
    ledger = _git_blob(su, x, LEDGER, "the ledger").decode("utf-8", "replace")
    if not re.search(rf"(?m)^### {entry} -- {rec['closure_date']}\b", ledger):
        raise _Refused(f"the ledger at {x[:12]} has no entry {entry} of {rec['closure_date']}")

    matrix = _read_object(matrix_blob, "the capability matrix")
    ps = _programme_supervisor()
    if ps.matrix_digest(matrix) != matrix_c["matrix_digest"]:
        raise _Refused("the capability matrix at the closing commit does not re-digest "
                       f"to {matrix_c['matrix_digest'][:12]}...")
    rows = [c for c in matrix.get("capabilities") or []
            if _is_object(c) and c.get("target_phase") == phase]
    proven = sum(1 for c in rows if c.get("status") == "PROVEN")
    if (not rows or proven != len(rows) or rows_c["phase_rows"] != len(rows)
            or rows_c["proven"] != proven or isinstance(rows_c["phase_rows"], bool)):
        raise _Refused(f"the matrix at the closing commit has {proven} of {len(rows)} "
                       f"{phase} row(s) PROVEN; the record says {rows_c['proven']} of "
                       f"{rows_c['phase_rows']}, and closing lifts no row (A7 rule 2)")
    # the register and the matrix agree on which requirements the phase has
    register = _read_object(register_blob, "the requirement register")
    members = {r["id"] for r in register.get("requirements") or []
               if _is_object(r) and isinstance(r.get("id"), str)
               and _phase_of(r.get("target_phase")) == phase}
    if members != {c.get("id") for c in rows}:
        raise _Refused(f"the register and the matrix at {x[:12]} disagree about the "
                       f"requirements of {phase} ({len(members)} against {len(rows)})")
    snapshot = _read_object(snap_blob, "the checklist snapshot")
    verdict = (snapshot.get("gates") or {}).get(phase)
    if (snapshot.get("seal") != snap_c["seal"] or not _is_object(verdict)
            or verdict.get("closed") is not True or verdict.get("blockers") != []):
        raise _Refused(f"the checklist snapshot at the closing commit is not sealed "
                       f"{snap_c['seal'][:12]}... with {phase} closed and no blocker")
    review = _read_object(review_blob, "the supervisor review")
    if (review.get("review_id") != review_id or review.get("phase") != phase
            or review.get("complete") is not True or review.get("blocking_findings") != []
            or review.get("snapshot_seal") != snap_c["seal"]
            or review.get("matrix_digest") != matrix_c["matrix_digest"]
            or review_c["snapshot_seal"] != snap_c["seal"]
            or review_c["matrix_digest"] != matrix_c["matrix_digest"]):
        raise _Refused(f"the supervisor review at the closing commit is not complete, "
                       f"blocking-free, about {phase} and bound to the sealed snapshot "
                       f"and matrix ({review_id})")
    capsule = _read_object(capsule_blob, "the succession capsule")
    if (capsule.get("internal_commit") != capsule_c["internal_commit"]
            or capsule_c["internal_commit"] != sealing
            or capsule.get("internal_tree_clean") is not True):
        raise _Refused("the succession capsule at the closing commit does not describe "
                       "the sealing commit on a clean tree")
    durability = _read_object(dur_blob, "the durability record")
    if (durability.get("evidence_head") != dur_c["evidence_head"]
            or durability.get("requirement_satisfied") is not True):
        raise _Refused("the durability record at the closing commit does not name the "
                       "recorded evidence head or does not read satisfied")
    _git_ancestor(su, dur_c["evidence_head"], x, "the durability evidence head")

    # an explicit reopen decision naming this closure (A7 rule 3). A decision is read
    # as a person writes it: the phase in any case and with leading zeros, the closing
    # commit as the full id or as an abbreviation of at least seven hex digits (the gate
    # prints twelve). A record that names this phase and a closing commit the gate cannot
    # read is not "another closure": cannot tell -> not honoured. Deleting a reopen record
    # does not end it: the history of the directory is read, and a reopen is ended only by
    # a committed RESOLVED record that names it.
    reopen_dir = f"{gate_dir}/{REOPEN_DIR}"
    rc, listed = su._git_rc("ls-tree", "-r", "-z", "--name-only", head, "--", reopen_dir + "/")
    if rc != 0:
        raise _Refused(f"git could not list {reopen_dir} (exit {rc})")
    names = sorted(n for n in listed.split("\0") if n)
    rc, past = su._git_rc("log", "--name-only", "--format=", "--no-renames", "-z", head,
                          "--", reopen_dir + "/")
    if rc != 0:
        raise _Refused(f"git could not read the history of {reopen_dir} (exit {rc})")
    ever = {n for n in past.split("\0") if n.strip()}
    if len(names) > REOPEN_MAX or len(ever) > REOPEN_MAX:
        raise _Refused(f"{reopen_dir} holds or held {max(len(names), len(ever))} files, "
                       f"more than {REOPEN_MAX}; whether one of them reopens this closure "
                       "is not read")
    def reading(found: dict, name: str):
        """What one reopen file says about THIS closure: `(None, "")` when it is about
        another phase or another closure, else `("KEEP" | "RESOLVED" | "REOPEN", detail)`;
        a file that names this phase and cannot be read refuses (cannot tell)."""
        named_phase = _phase_of(found.get("phase"))
        if named_phase is None or not isinstance(found.get("closed_at_commit"), str):
            raise _Refused(f"{name} does not name a phase and a closing commit")
        if named_phase != phase:
            return None, ""
        prefix = found["closed_at_commit"].strip()
        if not _COMMIT_PREFIX.fullmatch(prefix):
            raise _Refused(f"{name} names {phase} and a closing commit that cannot be read "
                           f"({prefix[:12]!r}): whether it reopens this closure cannot be told")
        if not x.startswith(prefix.lower()):
            return None, ""             # another closure of the same phase
        decision = found.get("decision")
        if decision == "KEEP_CLOSED":
            return "KEEP", ""           # a records entry that answers a reopen request
        if decision == "RESOLVED":
            target = found.get("resolves")
            if not isinstance(target, str) or not target.strip():
                raise _Refused(f"{name} is RESOLVED and names no record it resolves")
            return "RESOLVED", target.strip().rsplit("/", 1)[-1]
        if decision != "REOPEN":
            raise _Refused(f"{name} names this closure and its decision is neither "
                           "REOPEN, KEEP_CLOSED nor RESOLVED")
        by = found.get("entry")
        return "REOPEN", (f" ({by})" if isinstance(by, str) and by else "")

    def commits_of(name: str, *args: str) -> list[str]:
        rc, out = su._git_rc("log", "--format=%H", "--no-renames", *args, head, "--", name)
        listed = out.split()
        if rc != 0 or any(not _FULL_SHA.fullmatch(c) for c in listed):
            raise _Refused(f"git could not read the history of {name} (exit {rc})")
        return listed

    def after(older: str, newer: str) -> bool:
        """`newer` was committed strictly after `older`: a descendant by ancestry."""
        if older == newer:
            return False
        rc, _ = su._git_rc("merge-base", "--is-ancestor", older, newer)
        if rc not in (0, 1):
            raise _Refused(f"git could not relate {older[:12]} to {newer[:12]} (exit {rc})")
        return rc == 0

    # A reopen is judged by the version that was COMMITTED as a reopen: every commit that
    # added the file is read (oldest first), and an in-place edit later -- another
    # decision, another commit id, another phase -- never cancels it. Only a RESOLVED
    # record does, and only one committed strictly AFTER the reopen it names (by
    # ancestry): a RESOLVED that names a reopen that does not exist yet is ignored.
    anchors: dict[str, list[str]] = {}       # file name -> commits that made it a reopen
    reopens: dict[str, str] = {}             # file name -> " (entry)" of its committed reopen
    resolvers: dict[str, list[str]] = {}     # target file name -> commits of RESOLVED records
    for name in sorted(ever | set(names)):
        if not name.endswith(".json"):
            if name in names:
                raise _Refused(f"{name} is in {reopen_dir} and is not a JSON record; whether "
                               "it reopens this closure cannot be told")
            continue
        base = name.rsplit("/", 1)[-1]
        added = list(reversed(commits_of(name, "--diff-filter=A")))
        anchors[name] = list(added)
        for commit in added:
            found = _read_object(_git_blob(su, commit, name, name), name)
            kind, detail = reading(found, name)
            if kind == "REOPEN":
                reopens[name] = detail
                if commit not in anchors[name]:
                    anchors[name].append(commit)
        if name in names:
            found = _read_object(_git_blob(su, head, name, name), name)
            kind, detail = reading(found, name)
            touch = commits_of(name, "-1")
            if kind == "REOPEN":
                reopens[name] = detail
                anchors[name] += [c for c in touch if c not in anchors[name]]
            elif kind == "RESOLVED":
                resolvers.setdefault(detail, []).extend(touch)
    for name in sorted(anchors):
        if name not in reopens and name in names:
            continue                         # present and never a reopen: nothing to end
        base = name.rsplit("/", 1)[-1]
        if any(all(after(a, r) for a in anchors[name]) for r in resolvers.get(base, [])):
            continue                         # ended by a RESOLVED committed after it
        if name in names:
            raise _Refused(f"reopened by {name}{reopens[name]}")
        raise _Refused(f"{name} was committed to {reopen_dir} and is no longer there; "
                       "a reopen is ended only by a committed RESOLVED record that "
                       "names it (committed after it), never by removing it")
    state = _watch_state(phase, head, x, dur_c["evidence_head"])
    return {"phase": phase, "closed_at": x, "entry": entry, "date": rec["closure_date"],
            "tag": tag_here, "tag_claimed": tag, "release_commit": release_commit, "sealing_commit": sealing,
            "capsule_commit": capsule_commit, "review_id": review_id,
            "snapshot_seal": snap_c["seal"], "matrix_digest": matrix_c["matrix_digest"],
            "evidence_head": dur_c["evidence_head"], "verified_at_head": head,
            "watch_state": state}


def _watch_state(phase: str, head: str, closed_at: str, evidence_head: str) -> dict:
    """What the regression watch says about a verified closure at `head`, from the
    committed records only: the newest watch record, the first commit that made the
    evidence stale, and the reopen requests nobody has answered. Never raises and never
    changes the closure: an unreadable piece is reported as unknown, not as a pass."""
    state: dict = {"stale_since": None, "stale": None, "last_watch": None,
                   "last_watch_current": None, "requests": []}
    try:
        rw = _regression_watch()
        if rw.PHASE != phase:
            return state
        changed = rw.changed_paths(HOH, evidence_head, head)
        state["stale"] = None if changed is None else bool(changed)
        if changed:
            state["stale_since"] = rw.stale_since(HOH, evidence_head, head)
        last = rw.last_watch(HOH, head, phase)
        if last is not None:
            state["last_watch"] = {k: last[k] for k in ("stamp", "verdict", "commit")}
            state["last_watch_current"] = last["relevant_tree"] == rw.relevant_tree(HOH, head)
        state["requests"] = [
            {"file": r["file"], "commit": r["commit"], "failed": r["failed"]}
            for r in rw.open_requests(HOH, head, closed_at, phase)][:8]
    except Exception as exc:  # noqa: BLE001 - the watch is information, not a verdict
        state["error"] = _bound(f"{type(exc).__name__}: {exc}", 120)
    return state


def closure_record(phase: str) -> tuple[dict | None, str]:
    """`(closure, "")` when the committed closure record of `phase` verifies, else
    `(None, reason)` and the gate behaves as it would without a record (fail closed).
    Never raises. See `_verify_closure` for what is re-derived."""
    try:
        return _verify_closure(phase), ""
    except _Refused as exc:
        return None, _bound(str(exc))
    except Exception as exc:  # noqa: BLE001 - a verifier that fails refuses
        return None, _bound(f"the closure record could not be verified "
                            f"({type(exc).__name__}): {exc}")


def verified_closures(order: list[str] | None = None
                      ) -> tuple[dict[str, dict], dict[str, str]]:
    """`(closed, ignored)` over the phases that have a closure record file: the
    verified closures by phase, and for each record that was not honoured the reason.
    A phase without a record file is in neither. P0 has deliverables, not a closure."""
    if order is None:
        scope = _scope()
        order = [p["phase"] for p in scope["phases"]] if scope else []
    closed: dict[str, dict] = {}
    ignored: dict[str, str] = {}
    for ph in order:
        if not isinstance(ph, str) or ph == "P0" or not _PHASE.fullmatch(ph):
            continue
        path = HOH / f"dogfood/{ph.lower()}-gate" / CLOSURE_FILE
        try:
            present = path.exists() or path.is_symlink()
        except OSError:
            present = True
        if not present:
            continue
        closure, why = closure_record(ph)
        if closure is None:
            ignored[ph] = why
        else:
            closed[ph] = closure
    return closed, ignored


def _closed_line(closure: dict, watch: dict) -> str:
    """What the gate prints for a closed phase: the closure, then the watch."""
    tag = closure.get("tag")
    head = (f"CLOSED at {closure['closed_at'][:12]} "
            f"({closure['entry']}{', ' + tag if tag else ''})")
    state = closure.get("watch_state") or {}
    for request in state.get("requests") or []:
        failed = ", ".join(str(x) for x in request.get("failed") or []) or "a watched requirement"
        head += (f"; REGRESSION at {str(request.get('commit'))[:12]}: {failed} FAILED, "
                 f"reopen decision pending (A7 rule 3; {request.get('file')})")
        break
    if state.get("error"):
        head += f"; regression watch unreadable ({state['error']})"
    last = state.get("last_watch")
    if last:
        head += (f"; last re-run {last['stamp']} {last['verdict']} at "
                 f"{str(last['commit'])[:12]}")
        if state.get("stale") and state.get("last_watch_current") is False:
            head += f", stale since {str(state.get('stale_since'))[:12]}: re-run pending"
    elif state.get("stale"):
        head += f"; evidence stale since {str(state.get('stale_since'))[:12]}, re-run pending"
    rows = list(watch["rows_not_proven"])
    if rows:
        more = f" and {len(rows) - 6} more" if len(rows) > 6 else ""
        seen = f"{len(rows)} row(s) not PROVEN on this tree ({', '.join(rows[:6])}{more})"
    else:
        seen = "every row PROVEN on this tree"
    live = ", ".join(f"{kind} {'met' if v['met'] else 'UNMET'}"
                     for kind, v in watch["instruments"].items() if v["met"] is not None)
    return _bound(f"{head}; watch: {seen}" + (f"; live instruments: {live}" if live else ""))


def succession_measure() -> tuple[bool, str]:
    """P2's succession exit question (F13/M9): does the committed succession capsule
    still describe this tree?

    The capsule the gate reads is the one **committed at HEAD** (`git show HEAD:<path>`):
    a gate is a statement about a commit. The working-tree file must be that blob byte
    for byte (else drift: an edited capsule is not the committed one), and the capsule
    must record a clean tree (`internal_tree_clean` true): one written on a dirty tree
    verifies only while the tree stays dirty the same way, which is not a state a
    phase closes in. `tools/succession.py check` then re-derives every field **from
    the blobs committed at HEAD** (`at_commit="HEAD"`: the board, the sync state, the
    release invariants, the ledger), so an uncommitted record file changes nothing
    here; this reads its rows and is met only when every one is VERIFIED. DRIFTED fields are
    named; a field that cannot be checked from here (the plan root not set, the run
    tree absent -- the tool's ENVIRONMENT_GAP, exit 3 at its command line) is unmet
    with that reason, never a pass; a capsule that is missing, unreadable, outside the
    repository, not a JSON object or over the ceiling is unmet (`_gate_input`); a tool
    that judges another tree, a verifier that raises or returns rows of another shape
    is unmet. Never raises.
    """
    try:
        su = _succession()
        other = _other_tree(su)
        if other:
            return False, _bound(other)
        # Before any `git show`: on an archive copy git answers for the repository
        # above it, or exits 128, and "not committed at HEAD" misnames the cause.
        own, gap = su._own_repository()
        if not own:
            return False, _bound("not checkable from here, which is not a pass: "
                                 f"{su.GAP}: {gap}")
        working, why = _gate_input(HOH / SUCCESSION_CAPSULE, "the succession capsule")
        if working is None:
            return False, _bound(why)
        rc, text = su._git_rc("show", f"HEAD:{SUCCESSION_CAPSULE}")
        if rc != 0:
            return False, _bound(f"the succession capsule is not committed at HEAD "
                                 f"(git show exit {rc})")
        blob = text.encode("utf-8", "surrogateescape")
        if len(blob) > _GATE_INPUT_MAX_BYTES:
            return False, (f"the committed succession capsule is larger than "
                           f"{_GATE_INPUT_MAX_BYTES} bytes")
        try:
            capsule = json.loads(blob.decode("utf-8"))
        except (ValueError, RuntimeError) as exc:
            return False, _bound("the committed succession capsule cannot be read "
                                 f"({type(exc).__name__})")
        if not isinstance(capsule, dict):
            return False, "the committed succession capsule is not a JSON object"
        if (HOH / SUCCESSION_CAPSULE).resolve(strict=True).read_bytes() != blob:
            return False, ("the succession capsule in the working tree differs from the "
                           "one committed at HEAD; the gate reads the committed one")
        if capsule.get("internal_tree_clean") is not True:
            return False, _bound("the committed succession capsule records a tree that was "
                                 f"not clean (internal_tree_clean={capsule.get('internal_tree_clean')!r}); "
                                 "a phase does not close on it")
        # Derived from HEAD's blobs: a gate is a statement about a commit, and an
        # old board restored in the working tree, uncommitted, must not turn a
        # drifted capsule green.
        rows = su.check(capsule, su._plan_root(None), at_commit="HEAD")
        ok, drift, gap = su.OK, su.DRIFT, su.GAP
    except Exception as exc:  # noqa: BLE001 - a verifier that fails is an unmet condition
        return False, _bound(
            f"the succession capsule could not be verified ({type(exc).__name__}): {exc}")
    shaped = [r for r in rows if isinstance(r, tuple) and len(r) == 3
              and all(isinstance(x, str) for x in r)] if isinstance(rows, list) else []
    if not rows or len(shaped) != len(rows) or any(v not in (ok, drift, gap)
                                                   for _, v, _ in shaped):
        return False, "the succession verifier returned no usable answer"
    drifted = [(f, d) for f, v, d in shaped if v == drift]
    gaps = [(f, d) for f, v, d in shaped if v == gap]
    if drifted:
        return False, _bound("DRIFTED: " + "; ".join(f"{f} ({d})" for f, d in drifted)
                             + ("; not checkable from here: " + ", ".join(f for f, _ in gaps)
                                if gaps else ""))
    if gaps:
        return False, _bound("not checkable from here, which is not a pass: "
                             + "; ".join(f"{f} ({d})" for f, d in gaps))
    commit = str(capsule.get("internal_commit") or "")[:12]
    return True, _bound(f"VERIFIED: every field re-derives from this tree "
                        f"({', '.join(f for f, _, _ in shaped)}); capsule for {commit}")


def _one_measurement(provider, kind: str) -> tuple[bool, str]:
    """One instrument's answer as a real `(bool, str)`; anything else is unmet."""
    try:
        value = provider()
    except Exception as exc:  # noqa: BLE001 - an instrument that fails is an unmet condition
        return False, _bound(f"the instrument failed ({type(exc).__name__}): {exc}")
    if (isinstance(value, tuple) and len(value) == 2
            and isinstance(value[0], bool) and isinstance(value[1], str)):
        return value[0], _bound(value[1])
    return False, "the instrument returned no usable answer"


def _gate_measurements(provider) -> dict[str, tuple[bool, str]]:
    """The checklist provider's answers, each a real `(bool, str)`; anything else is
    unmet. `met` must be the bool `True`: a truthy string or number does not open a
    gate."""
    try:
        got = (provider or checklist_gate_measures)()
    except Exception as exc:  # noqa: BLE001 - an instrument that fails is an unmet condition
        got = {}
        failure = f"the instrument failed ({type(exc).__name__}): {exc}"
    else:
        failure = "the instrument returned no usable answer"
    out = {}
    for kind in _CHECKLIST_KINDS:
        value = got.get(kind) if isinstance(got, dict) else None
        if (isinstance(value, tuple) and len(value) == 2
                and isinstance(value[0], bool) and isinstance(value[1], str)):
            out[kind] = (value[0], _bound(value[1]))
        else:
            out[kind] = (False, _bound(failure))
    return out


def phase_gates(capabilities: list[dict] | None = None,
                requirements: list[dict] | None = None, *,
                checklist_measures=None, succession_measures=None, closures=None,
                ) -> tuple[list[str], dict[str, tuple[bool, str]]]:
    """`phase_gate_report` without the watch: the phase order and, per phase, the
    gate's `(met, reason)`. See there."""
    order, gates, _watch = phase_gate_report(
        capabilities, requirements, checklist_measures=checklist_measures,
        succession_measures=succession_measures, closures=closures)
    return order, gates


def phase_gate_report(capabilities: list[dict] | None = None,
                      requirements: list[dict] | None = None, *,
                      checklist_measures=None, succession_measures=None, closures=None,
                      ) -> tuple[list[str], dict[str, tuple[bool, str]], dict[str, dict]]:
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
    returning `{"items" | "review" | "fsck": (met, reason)}`), and P2's
    "controller/session replacement" exit by the succession capsule
    (`succession_measure`, or `succession_measures` when given: a callable
    returning `(met, reason)`), asked only when an item maps to it. Such an
    item is met only when its instrument says `True` -- and, if a requirement
    also claims it, when that requirement is PROVEN as well: a claim never
    stands in for the instrument. An unmet one is reported with its reason, not
    as unmeasured. A phase with no gate-kind item is absent from the result.

    `closures` -- `{phase: closure}` as `verified_closures` returns it (a closure
    this tool has already re-derived from the blobs at its commit; nothing else
    is believed) -- makes the phase **closed** (amendment A7): its gate is
    `(True, "CLOSED at <commit> (<entry>, <tag>); watch: ...")` whatever the
    instruments answer, the succession capsule is not asked (it is the rotation
    instrument of the board, R4), and the third result, `watch`, keeps what the
    instruments and the matrix say on this tree -- `live_met`, `live_reason`, the
    phase's rows that are not PROVEN, each instrument's answer -- so a closure never
    hides a measurement. Without `closures` the result is what it was.
    """
    scope = _scope()
    if scope is None:
        return [], {}, {}
    closures = closures if isinstance(closures, dict) else {}
    watch: dict[str, dict] = {}
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
        checklist_kinds = {k for k in instrument.values() if k in _CHECKLIST_KINDS}
        closure = closures.get(ph)
        measured = _gate_measurements(checklist_measures) if checklist_kinds else {}
        if "succession" in instrument.values() and not isinstance(closure, dict):
            measured["succession"] = _one_measurement(
                succession_measures or succession_measure, "succession")
        unmeasured = [i for i in conditions
                      if not bindings[i["id"]] and i["id"] not in instrument]
        instrument_unmet = [(instrument[i["id"]], measured[instrument[i["id"]]][1])
                            for i in conditions if i["id"] in instrument
                            and instrument[i["id"]] in measured
                            and not measured[instrument[i["id"]]][0]]
        succession_unmet = [(k, r) for k, r in instrument_unmet if k == "succession"]
        instrument_unmet = [(k, r) for k, r in instrument_unmet if k != "succession"]
        succession_items = [i for i in conditions
                            if instrument.get(i["id"]) == "succession"]
        unmet_bound = 0
        not_proven: list[str] = []
        for i in conditions:
            short = [rid for rid in bindings[i["id"]] if status.get(rid) != "PROVEN"]
            if short:
                unmet_bound += 1
                not_proven += [rid for rid in short if rid not in not_proven]
        live = None
        if (not unmeasured and not unmet_bound and not instrument_unmet
                and not succession_unmet):
            live = (True, f"{len(conditions)} exit condition(s), each bound "
                          "to a PROVEN requirement" if not instrument else
                          f"{len(conditions)} exit condition(s) met: "
                          f"{len(conditions) - len(instrument)} bound to a "
                          f"PROVEN requirement, "
                          f"{len(instrument) - len(succession_items)} measured by "
                          "the checklist instruments"
                          + (f", {len(succession_items)} measured by the "
                             "succession instrument"
                             + (" (its claimant PROVEN besides)"
                                if any(bindings[i["id"]] for i in succession_items)
                                else "") if succession_items else ""))
        if live is None:
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
            if succession_unmet:
                named = "; ".join(f"{_GATE_LABELS[kind]}: {reason}"
                                  for kind, reason in succession_unmet)
                why.append(f"{len(succession_unmet)} exit-includes condition(s) measured "
                           f"by the succession instrument and unmet -- {named}")
            if unmeasured:
                why.append(f"{len(unmeasured)} exit condition(s) nothing "
                           "here measures yet, e.g. "
                           f"{unmeasured[0]['text'][:60]!r}")
            live = (False, "; ".join(why))
        if not isinstance(closure, dict):
            gates[ph] = live
            continue
        # A closed phase (A7): the gate is the closure; the instruments' answers and
        # the phase's rows stay, as the watch, exactly as measured on this tree.
        rows = {c["id"]: c.get("status") for c in capabilities or []
                if isinstance(c, dict) and c.get("target_phase") == ph
                and c.get("status") != "PROVEN"}
        shown = {kind: {"met": measured[kind][0], "reason": measured[kind][1]}
                 for kind in (*_CHECKLIST_KINDS, "succession")
                 if kind in set(instrument.values()) and kind in measured}
        if "succession" in instrument.values() and "succession" not in measured:
            shown["succession"] = {"met": None, "reason": (
                "not asked after closure: the capsule is the rotation instrument of "
                "the board's `succession` row and `tools/succession.py verify`")}
        # With the capsule not asked, "every asked instrument met" is not "met": it is
        # unknown, and says so. Unmet stays unmet whatever the capsule would say.
        unasked = "succession" in instrument.values() and "succession" not in measured
        live_met = (None if live[0] else False) if unasked else live[0]
        live_reason = live[1] + ("; the succession capsule was not asked after closure"
                                 if unasked else "")
        watch[ph] = {"live_met": live_met, "live_reason": live_reason,
                     "rows_not_proven": dict(sorted(rows.items())),
                     "instruments": shown}
        gates[ph] = (True, _closed_line(closure, watch[ph]))
    return order, gates, watch


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


# --------------------------------------------------------------------------- #
# the gate counter says why it moved (P3 analysis R-5)
# --------------------------------------------------------------------------- #

#: The statuses a matrix row can hold, lowest first, when the matrix module is not
#: the one that answers (a test's stub): `capability_matrix.ORDER` is preferred.
_COUNTER_ORDER = ("MISSING", "PREPARED", "PARTIAL", "IMPLEMENTED_NOT_PROVEN", "PROVEN")
#: At most this many ids of one kind are named in the line; the DAG carries them all.
_COUNTER_IDS_SHOWN = 5


def counter_baseline() -> tuple[dict | None, str]:
    """The committed capability matrix as of the newest commit that changed it -- the
    last records checkpoint that regenerated it -- as `{commit, rows: {id: row}}`, or
    `(None, reason)`. Read from git blobs of this repository only (never the working
    tree: the baseline is a commit), and only in a repository of its own."""
    try:
        su = _succession()
        other = _other_tree(su)
        if other:
            return None, _bound(other)
        own, why = su._own_repository()
        if not own:
            return None, _bound("ENVIRONMENT_GAP: " + why, 120)
        rc, out = su._git_rc("log", "-1", "--format=%H", "HEAD", "--", MATRIX_JSON)
        commit = out.strip()
        if rc != 0 or not _FULL_SHA.fullmatch(commit):
            return None, "the capability matrix has no commit in this history"
        body = _read_object(_git_blob(su, commit, MATRIX_JSON, "the committed matrix"),
                            "the committed capability matrix")
    except _Refused as exc:
        return None, _bound(str(exc), 120)
    except Exception as exc:  # noqa: BLE001 - provenance is information, never a verdict
        return None, _bound(f"{type(exc).__name__}: {exc}", 120)
    rows = {c["id"]: c for c in body.get("capabilities") or []
            if isinstance(c, dict) and isinstance(c.get("id"), str)}
    if not rows:
        return None, "the committed capability matrix holds no rows"
    return {"commit": commit, "rows": rows}, ""


def introduced_at(ids: list[str], since: str | None = None) -> dict[str, str | None]:
    """For each id, the short id of the first commit **after `since`** (the baseline;
    the whole history without it) that put it into the register -- `git log --reverse
    -S` over the register file, so a commit that only edits the row does not date it --
    or None where git cannot say. Read-only; one call per id."""
    out: dict[str, str | None] = {}
    try:
        su = _succession()
        for rid in ids:
            rc, text = su._git_rc("log", "--reverse", "--format=%h",
                                  f"-S\"id\": {json.dumps(rid)}",
                                  f"{since}..HEAD" if since else "HEAD", "--",
                                  REGISTER_JSON)
            first = text.split()[0] if rc == 0 and text.split() else None
            out[rid] = first
    except Exception:  # noqa: BLE001 - a missing provenance is a "?", not a failure
        out = {rid: None for rid in ids}
    return out


def blocking_counter(phase: str, live_rows: list[dict], baseline: dict,
                     order: tuple | list = _COUNTER_ORDER) -> dict:
    """Why the number of blocking requirements of `phase` is what it is, against the
    committed matrix. Pure, and **complete**: every row that is open now and was not open
    then (`entered`), and every row that was open then and is not now (`left`), is in
    exactly one named list.

    A row is *blocking open* when it holds its phase's gate shut and is not satisfied
    (`_blocking`, `_satisfied`: the build's own two tests). With `then` the baseline's
    rows of the phase and `now` the live ones, `entered = now_open - previous_open` is
    partitioned into

    * `added` -- not in the baseline at all (a requirement registered since);
    * `moved_in` -- in the baseline, but under another phase;
    * `regressed` -- in `then`, blocking there and **satisfied** there (PROVEN, or a
      waiver that has since gone), open now;
    * `newly_blocking` -- in `then`, **not blocking** there (optional provider / UI
      only), blocking and open now: the gate grew by a flag, not by a status;

    and `left = previous_open - now_open` into

    * `lifted` -- present now and satisfied (PROVEN or waived);
    * `no_longer_blocking` -- present now, not satisfied, no longer blocking;
    * `removed` -- gone from the matrix;
    * `moved_out` -- present now under another phase.

    `regressed` also lists the rows that stayed open at a **lower** status
    (`regressed_still_open`: they change no count); it never lists a row that left.
    The arithmetic of the line is therefore exact:
    `len(now_open) == len(previous_open) + added + moved_in + newly_blocking +
    regressed_entering - lifted - no_longer_blocking - removed - moved_out`, and
    `reconciled` says that the partitions cover the differences. Nothing here decides
    anything: it is the reading of the number, printed beside the number.
    """
    rank = {s: i for i, s in enumerate(order)}.get
    live_all = {r["id"]: r for r in live_rows
                if isinstance(r, dict) and isinstance(r.get("id"), str)}
    then_all = baseline["rows"]
    live = {i: r for i, r in live_all.items() if r.get("target_phase") == phase}
    then = {i: r for i, r in then_all.items() if r.get("target_phase") == phase}

    def satisfied(row: dict) -> bool:
        return _satisfied({"status": None, **row})

    def is_open(row: dict) -> bool:
        return _blocking(row) and not satisfied(row)
    previous = sorted(i for i, r in then.items() if is_open(r))
    now = sorted(i for i, r in live.items() if is_open(r))
    entered = [i for i in now if i not in previous]
    left = [i for i in previous if i not in now]
    added = [i for i in entered if i not in then_all]
    moved_in = [i for i in entered if i not in then and i in then_all]
    newly_blocking = [i for i in entered if i in then and not _blocking(then[i])]
    regressed_entering = [i for i in entered if i in then and _blocking(then[i])
                          and satisfied(then[i])]
    lowered = sorted(i for i in live if i in then and i in previous and i in now
                     and rank(live[i].get("status"), -1) < rank(then[i].get("status"), -1))
    lifted = [i for i in left if i in live and satisfied(live[i])]
    no_longer_blocking = [i for i in left if i in live and not satisfied(live[i])
                          and not _blocking(live[i])]
    removed = [i for i in left if i not in live and i not in live_all]
    moved_out = [i for i in left if i not in live and i in live_all]
    grouped_in = added + moved_in + newly_blocking + regressed_entering
    grouped_out = lifted + no_longer_blocking + removed + moved_out
    reconciled = (sorted(grouped_in) == sorted(entered) and sorted(grouped_out) == sorted(left)
                  and len(set(grouped_in)) == len(grouped_in)
                  and len(set(grouped_out)) == len(grouped_out)
                  and len(now) == len(previous) + len(entered) - len(left))
    return {
        "phase": phase, "baseline_commit": baseline["commit"],
        "previous_open": previous, "now_open": now, "entered": entered, "left": left,
        "added": added, "moved_in": moved_in, "newly_blocking": newly_blocking,
        "regressed": sorted(regressed_entering + lowered),
        "regressed_entering": sorted(regressed_entering), "regressed_still_open": lowered,
        "lifted": lifted, "no_longer_blocking": no_longer_blocking, "removed": removed,
        "moved_out": moved_out, "reconciled": reconciled}


def _named(ids: list[str], shown: int = _COUNTER_IDS_SHOWN) -> str:
    more = f" and {len(ids) - shown} more" if len(ids) > shown else ""
    return ", ".join(ids[:shown]) + more


def counter_text(counter: dict, introduced: dict[str, str | None] | None = None) -> str:
    """The one-line provenance printed after `N blocking open`:
    `(25 at f78d1cb9; +1 added: P3-42 at f03b5801; -1 lifted: P3-06; 0 regressed)`. The
    four fixed clauses are always there; the others only when they are not empty."""
    introduced = introduced or {}
    if counter.get("reconciled") is not True:
        return "(unreconciled: the named lists do not account for the difference)"
    added = counter["added"]
    parts = [f"{len(counter['previous_open'])} at {counter['baseline_commit'][:12]}"]
    if added:
        shown = [f"{i} at {introduced.get(i) or '?'}" for i in added[:_COUNTER_IDS_SHOWN]]
        more = f" and {len(added) - _COUNTER_IDS_SHOWN} more" if len(added) > _COUNTER_IDS_SHOWN else ""
        parts.append(f"+{len(added)} added: {', '.join(shown)}{more}")
    else:
        parts.append("+0 added")
    for key, label in (("moved_in", "moved in"), ("newly_blocking", "newly blocking")):
        if counter[key]:
            parts.append(f"+{len(counter[key])} {label}: {_named(counter[key])}")
    lifted = counter["lifted"]
    parts.append(f"-{len(lifted)} lifted" + (f": {_named(lifted)}" if lifted else ""))
    for key, label in (("no_longer_blocking", "no longer blocking"), ("removed", "removed"),
                       ("moved_out", "moved out")):
        if counter[key]:
            parts.append(f"-{len(counter[key])} {label}: {_named(counter[key])}")
    regressed = counter["regressed"]
    clause = f"{len(regressed)} regressed" + (f": {_named(regressed)}" if regressed else "")
    if counter["regressed_still_open"]:
        clause += f" ({len(counter['regressed_still_open'])} already open, at a lower status)"
    parts.append(clause)
    return "(" + "; ".join(parts) + ")"


def annotate_counters(plan: dict, rows: list[dict], order=_COUNTER_ORDER) -> dict[str, str]:
    """For every phase of `plan` with blocking requirements open, put `counter` into
    its gate state (JSON-able: the ids and the commit, or `{"unavailable": reason}`) and
    return the printed text per phase. Git is touched only when some phase is open.

    Never raises and never withholds the number: a baseline row of a shape the reading
    cannot take, or a counter whose `now_open` is not the gate's own `blocking_open`,
    prints as unavailable / unreconciled with its reason."""
    open_phases = [ph for ph, g in plan["phase_gates"].items() if g.get("blocking_open")]
    if not open_phases:
        return {}
    baseline, why = counter_baseline()
    texts: dict[str, str] = {}
    if baseline is None:
        for ph in open_phases:
            plan["phase_gates"][ph]["counter"] = {"unavailable": why}
            texts[ph] = f"(provenance unavailable: {why})"
        return texts
    for ph in open_phases:
        gate = plan["phase_gates"][ph]
        try:
            counter = blocking_counter(ph, rows, baseline, order)
            if sorted(counter["now_open"]) != sorted(gate.get("blocking_open") or []):
                counter["reconciled"] = False
                counter["unreconciled_with_the_gate"] = True
            introduced = introduced_at(counter["added"][:_COUNTER_IDS_SHOWN],
                                       baseline["commit"])
            gate["counter"] = {**counter, "introduced_at": introduced}
            texts[ph] = counter_text(counter, introduced)
        except Exception as exc:  # noqa: BLE001 - provenance is information, never a verdict
            reason = _bound(f"the baseline could not be read as rows "
                            f"({type(exc).__name__}: {exc})", 120)
            gate["counter"] = {"unavailable": reason}
            texts[ph] = f"(provenance unavailable: {reason})"
    return texts


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

    closed, ignored = verified_closures()
    phase_order, gates, watch = phase_gate_report(
        body["capabilities"], register["requirements"], closures=closed)
    plan = build(body, phase_order, gates, closures=closed, watch=watch,
                 closure_notes=ignored)
    check = checklist(plan)
    counters = annotate_counters(plan, body["capabilities"],
                                 tuple(getattr(cm, "ORDER", _COUNTER_ORDER)))
    for wave in plan["waves"]:
        blocking = sum(1 for i in wave["items"] if i["core_required_1_0"])
        print(f"  wave {wave['wave']}: {len(wave['items'])} item(s), "
              f"{blocking} phase-blocking")
    print(f"\n{plan['open_count']} open requirement(s) in "
          f"{len(plan['waves'])} wave(s); {len(plan['held_by_phase_gate'])} "
          "held behind a phase gate")
    print(f"ready now: {', '.join(plan['ready_now']) or 'nothing'}")
    for ph, g in plan["phase_gates"].items():
        if g.get("closure_ignored"):
            print(f"  gate {ph}: closure record ignored: {g['closure_ignored']}")
        if g.get("closed_at"):
            print(f"  gate {ph}: {g['external']}")
        elif g["blocking_open"] or g["external_met"] is False:
            why = (f"{len(g['blocking_open'])} blocking open"
                   + (f" {counters[ph]}" if ph in counters else "")
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
