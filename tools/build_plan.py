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
        if phase_order and r["target_phase"] in phase_order:
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
            if i > 0:
                nodes[rid] = nodes[rid] + [f"gate:{phase_order[i - 1]}"]
        for ph, (met, _) in gates.items():
            if not met:
                nodes[f"unmet:{ph}"] = [f"unmet:{ph}"]     # a self-loop: never placed
    waves, unplaced = layers(nodes)
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
                        "depends_on": [d for d in nodes[rid]
                                       if not d.startswith(("gate:", "unmet:"))]}
                       for rid in wave]}
            for i, wave in enumerate(waves)
        ],
        "unplaceable": unplaced,
        "held_by_phase_gate": held,
        "phase_gates": gate_state,
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
            out.append(
                f"| `{item['id']}` | {item['name']} | {item['phase']} | "
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


def phase_gates() -> tuple[list[str], dict[str, tuple[bool, str]]]:
    """The plan's phase order and, per phase, whether its non-requirement
    conditions are met -- read from the inventory, never from the register.

    P0's condition is its deliverables existing. Any other phase whose exit
    includes items nothing here measures yet is an *unmet* gate, and the
    reason names how many: P2's unattended exit test, P3's invariants, P7's
    whole product proof. That holds later phases shut, which is what "the
    authoritative implementation sequence" means, rather than letting a v2.0
    backend show up as ready beside a v0.2 gap.
    """
    scope_file = HOH / "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json"
    if not scope_file.is_file():
        return [], {}
    scope = json.loads(scope_file.read_text(encoding="utf-8"))
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
        unmeasured = [i for i in items if i["kind"] in _GATE_KINDS]
        if unmeasured:
            gates[ph] = (False, f"{len(unmeasured)} exit condition(s) nothing "
                                "here measures yet, e.g. "
                                f"{unmeasured[0]['text'][:60]!r}")
    return order, gates


def render_dispositions(plan: dict, body: dict) -> str | None:
    """Every plan item and what became of it, with the measured status and
    the gate it waits for joined in. O196: silence is not a disposition."""
    scope_file = HOH / "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json"
    if not scope_file.is_file():
        return None
    spec = importlib.util.spec_from_file_location("program_scope",
                                                  HERE / "program_scope.py")
    ps = importlib.util.module_from_spec(spec)
    sys.modules["program_scope"] = ps
    spec.loader.exec_module(ps)
    scope = json.loads(scope_file.read_text(encoding="utf-8"))
    register = json.loads((HOH / "program/v3_3/REQUIREMENTS.json").read_text())
    status = {r["id"]: r["status"] for r in body["capabilities"]}
    ready = set(plan.get("ready_now") or [])
    held = {i["id"] for i in plan.get("held_items") or []}
    rows = ps.dispositions(scope, register)
    out = ["# Programme dispositions (V3.3)", "",
           "Generated by `python3 tools/build_plan.py`. One row per item of the",
           "pinned production line: what it is, which requirement implements it,",
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
            elif not rid:
                start = "—"
            elif st == "PROVEN":
                start = "done"
            elif rid in ready:
                start = "ready now"
            elif rid in held:
                start = f"held by gate {r['waits_for_gate']}"
            else:
                start = "after its dependencies"
            text = r["item"].split(".", 2)[-1]
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

    phase_order, gates = phase_gates()
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
