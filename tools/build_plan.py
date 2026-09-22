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


def build(body: dict) -> dict:
    """The DAG and the checklist, from one measured matrix."""
    caps = {r["id"]: r for r in body["capabilities"]}
    open_ = {rid: r for rid, r in caps.items() if r["status"] != "PROVEN"}
    nodes: dict[str, list[str]] = {}
    dangling: dict[str, list[str]] = {}
    for rid, r in open_.items():
        deps = [d for d in (r.get("depends_on") or [])]
        # A dependency that is already PROVEN is satisfied, not a step.
        unmet = [d for d in deps if d in open_]
        unknown = [d for d in deps if d not in caps]
        if unknown:
            dangling[rid] = unknown
        nodes[rid] = unmet
    waves, unplaced = layers(nodes)
    return {
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
                        "depends_on": nodes[rid]}
                       for rid in wave]}
            for i, wave in enumerate(waves)
        ],
        "unplaceable": unplaced,
        "dangling_dependencies": dangling,
    }


def checklist(plan: dict) -> dict:
    """One item per open requirement, with what closes it.

    Phase-blocking is read from the register's own columns rather than
    decided here: a core-required capability blocks its phase, an optional
    provider does not.
    """
    items = []
    for wave in plan["waves"]:
        for item in wave["items"]:
            items.append({
                "item_id": item["id"],
                "title": item["name"],
                "phase": item["phase"],
                "wave": wave["wave"],
                "state": item["status"],
                "phase_blocking": bool(item["core_required_1_0"]),
                "blocked_by": item["depends_on"],
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
    if plan["unplaceable"]:
        out += ["## Unplaceable", "",
                "These could not be given a wave, which means a dependency "
                "cycle or a dependency that is not a requirement. A plan that "
                "dropped them to stay acyclic would be a plan with an "
                "impossible step in it.", ""]
        out += [f"* `{rid}`" for rid in plan["unplaceable"]] + [""]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dag", type=Path, default=None)
    ap.add_argument("--checklist", type=Path, default=None)
    ap.add_argument("--markdown", type=Path, default=None)
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

    plan = build(body)
    check = checklist(plan)
    for wave in plan["waves"]:
        blocking = sum(1 for i in wave["items"] if i["core_required_1_0"])
        print(f"  wave {wave['wave']}: {len(wave['items'])} item(s), "
              f"{blocking} phase-blocking")
    print(f"\n{plan['open_count']} open requirement(s) in "
          f"{len(plan['waves'])} wave(s)")
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
    return 1 if (plan["unplaceable"] or plan["dangling_dependencies"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
