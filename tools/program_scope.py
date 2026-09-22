#!/usr/bin/env python3
"""The whole programme, read out of the plan rather than out of the register.

O196. The requirement register was tested for completeness by asserting that
it contained phases P1, P2 and P3 -- which the register itself supplied. A
register that silently dropped P4 through P12 would have passed that test, and
because the capability matrix, the gap register, the build order and the
checklist are all derived from the register, every one of them would have
stayed internally consistent and green while describing a quarter of the work.

So the expected side is computed from somewhere else: the pinned production
line itself, parsed from its bytes, bound to its digest. This tool does not
read `REQUIREMENTS.json` to build the inventory. It reads it only to compare.

What the parser does, and refuses to do:

* **Every line of the plan that says something becomes an item.** Bullets,
  numbered steps, lines inside fenced blocks, and prose paragraphs -- P11 is
  written entirely in prose, and a parser that read only bullets would make a
  whole capability phase disappear, which is the failure this exists to catch.
* **Every item gets a kind from the section it sits in**, through one table.
  A section the table does not know is `UNCLASSIFIED`, and the gate fails on
  it. Silence is not a classification.
* **Prose is a constraint unless the table says otherwise**, and the few
  places it says otherwise are listed by item id with the reason.

Usage:
    python3 tools/program_scope.py --out program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json
    python3 tools/program_scope.py --check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent
PLAN_ENV = "VERIHARNESS_PLAN_ROOT"
PLAN_FILE = ("VERIHARNESS_CLI_MASTER_ONBOARDING_V3_3_PARALLELISM_FINAL_2026-09-21/"
             "02_FINAL_PRODUCTION_LINE_V3_3.md")
#: The package's machine-readable summary. Its `new_in_v3_3` list names the
#: V3.3 contracts; several of them are not a line of the production line, so
#: they are checked as *contracts carried by some requirement* rather than as
#: items -- a second independent parse of the pinned plan.
MASTER_FILE = ("VERIHARNESS_CLI_MASTER_ONBOARDING_V3_3_PARALLELISM_FINAL_2026-09-21/"
               "MASTER_PLAN_V3_3.json")
SCOPE = HOH / "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json"
REGISTER = HOH / "program/v3_3/REQUIREMENTS.json"

#: Section label (lower case, markdown stripped, trailing colon removed) -> kind.
#: This is the one authored judgement in the inventory, and it classifies; it
#: never adds or removes an item.
LABEL_KINDS: dict[str, str] = {
    "deliver": "baseline_deliverable",
    "measure": "measurement",
    "build/verify": "capability",
    "core": "capability",
    "build": "capability",
    "formalize": "capability",
    "do": "capability",
    "product": "product_capability",
    "product requirement": "acceptance_path",
    "system-one": "optional_capability",
    "autonomy": "autonomy_capability",
    "checklist gate": "gate",
    "exit includes": "gate",
    "required invariant": "invariant",
    "build a thin read-only mission control, not the full ui": "ui_capability",
    "purpose": "rationale",
    "run endurance": "validation",
    "must include": "validation",
    "at least one real self-build must demonstrate": "validation",
    "at least one bounded reference program must demonstrate": "validation",
    "at least one real program": "validation",
    "core 1.0 must define stable interfaces for": "release_interface",
    "core 1.0 must be": "release_property",
    "must pass with": "release_gate",
    "product modes": "ui_capability",
    "needs you": "ui_capability",
    "veriharness owns semantics": "architecture",
    # Things to build, not a statement about ownership: each backend named
    # here is an implementation P12 asks for.
    "execution persistence backend should be pluggable, e.g.": "capability",
}

#: Kinds that name something to be built. Each item of one of these kinds must
#: map to exactly one requirement in the register; everything else must only
#: be classified.
CAPABILITY_KINDS = frozenset({
    "capability", "product_capability", "optional_capability",
    "autonomy_capability", "ui_capability", "release_interface",
})

#: Prose paragraphs that are capabilities rather than constraints. Keyed by the
#: first words of the paragraph so the override survives re-parsing, and each
#: says why -- a prose override is exactly the kind of quiet decision that
#: needs to be readable.
PROSE_KINDS: dict[str, tuple[str, str]] = {
    "promote only specific low-risk decision classes": (
        "optional_capability",
        "P11 is written entirely in prose; this sentence is its capability"),
    "core 1.0 must natively execute safe project-level parallel waves": (
        "release_property",
        "the native-parallelism claim Core 1.0 rests on"),
}


@dataclass
class Item:
    item_id: str
    phase: str
    version: str | None
    section: str
    kind: str
    text: str
    line: int

    def as_dict(self) -> dict:
        return {"id": self.item_id, "phase": self.phase, "version": self.version,
                "section": self.section, "kind": self.kind, "text": self.text,
                "line": self.line,
                "capability": self.kind in CAPABILITY_KINDS}


def plan_path(root: Path | None = None) -> Path | None:
    base = root or (Path(os.environ[PLAN_ENV]) if os.environ.get(PLAN_ENV) else None)
    if base is None:
        return None
    p = base / PLAN_FILE
    return p if p.is_file() else None


def _clean(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = text.replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def _label_key(text: str) -> str:
    return _clean(text).rstrip(":").strip().lower()


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", _clean(text).lower())
    return "-".join(words)[:60] or "item"


_PHASE = re.compile(r"^# (P\d+) — (.*)$")
_VERSION = re.compile(r"\bv?(\d+\.\d+)\b")


def parse(text: str) -> tuple[list[dict], list[Item]]:
    """(phases, items) from the plan's own text."""
    phases: list[dict] = []
    items: list[Item] = []
    phase = version = None
    section = "(none)"
    fence = False
    prose: list[tuple[int, str]] = []
    seen: dict[str, int] = {}

    def add(kind: str, raw: str, line: int) -> None:
        if phase is None:
            return
        base = f"{phase}.{_slug(section)[:24]}.{_slug(raw)}"
        seen[base] = seen.get(base, 0) + 1
        item_id = base if seen[base] == 1 else f"{base}-{seen[base]}"
        # The same line twice in one section is not two items; it is a copy
        # somebody did not notice. Kept with a suffix so nothing is lost, and
        # marked so the gate refuses it rather than counting it twice. The
        # same text in a *different* section has a different id and is not a
        # duplicate: "Product:" appears in P4 and P5 and means two things.
        kind_ = "DUPLICATE" if seen[base] > 1 else kind
        items.append(Item(item_id, phase, version, section, kind_, _clean(raw), line))

    def flush_prose() -> None:
        if not prose:
            return
        line, raw = prose[0][0], " ".join(t for _, t in prose)
        key = next((k for k in PROSE_KINDS if _clean(raw).lower().startswith(k)), None)
        kind = PROSE_KINDS[key][0] if key else (
            LABEL_KINDS.get(_label_key(section), "constraint")
            if section != "(none)" and _label_key(section) == "purpose" else "constraint")
        add(kind, raw, line)
        prose.clear()

    for n, raw_line in enumerate(text.splitlines(), 1):
        s = raw_line.strip()
        if s.startswith("```"):
            flush_prose()
            fence = not fence
            continue
        if not s or s == "---":
            flush_prose()
            continue
        m = _PHASE.match(s)
        if m:
            flush_prose()
            phase, title = m.group(1), m.group(2)
            vs = [v for v in _VERSION.findall(title)]
            version = ("/".join(vs) if vs else
                       ("1.0" if "Core 1.0" in title else None))
            section = "(none)"
            phases.append({"phase": phase, "title": _clean(title),
                           "version": version, "line": n})
            continue
        if phase is None:
            continue
        if s.startswith("## "):
            flush_prose()
            section = s[3:].strip()
            continue
        if fence:
            if re.fullmatch(r"[-> |]+", s):
                continue            # a bare arrow joins two steps; it says nothing
            kind = LABEL_KINDS.get(_label_key(section), "UNCLASSIFIED")
            add(kind, s, n)
            continue
        if re.match(r"^(- |\d+\. )", s):
            flush_prose()
            body = re.sub(r"^(- |\d+\. )", "", s)
            add(LABEL_KINDS.get(_label_key(section), "UNCLASSIFIED"), body, n)
            continue
        if s.endswith(":"):
            flush_prose()
            section = s
            continue
        prose.append((n, s))
    flush_prose()
    return phases, items


def build(plan: Path) -> dict:
    data = plan.read_bytes()
    phases, items = parse(data.decode("utf-8"))
    by_phase: dict[str, dict] = {p["phase"]: {**p, "items": 0, "capabilities": 0}
                                 for p in phases}
    for it in items:
        by_phase[it.phase]["items"] += 1
        if it.kind in CAPABILITY_KINDS:
            by_phase[it.phase]["capabilities"] += 1
    for p in by_phase.values():
        p["phase_kind"] = "capability" if p["capabilities"] else (
            "baseline" if p["phase"] == "P0" else "non-capability")
    return {
        "what": ("The authoritative programme inventory, parsed from the pinned "
                 "production line. The register is compared against this; it "
                 "is never the source of it."),
        "plan_file": PLAN_FILE,
        "plan_sha256": hashlib.sha256(data).hexdigest(),
        "phases": list(by_phase.values()),
        "items": [it.as_dict() for it in items],
        "counts": {
            "phases": len(phases), "items": len(items),
            "capability_items": sum(1 for it in items if it.kind in CAPABILITY_KINDS),
            "unclassified": sum(1 for it in items if it.kind == "UNCLASSIFIED"),
        },
    }


# --------------------------------------------------------------------------- #
# The completeness gate
# --------------------------------------------------------------------------- #

def completeness(scope: dict, register: dict, plan_sha256: str | None,
                 contracts: list[str] | None = None) -> list[str]:
    """Every way the register can fail to describe the programme. Empty = pass.

    Pure over its three inputs, so every negative control is a fixture edit
    rather than a change to this repository.
    """
    problems: list[str] = []
    if plan_sha256 is not None and scope.get("plan_sha256") != plan_sha256:
        problems.append(
            f"the inventory was parsed from plan {str(scope.get('plan_sha256'))[:12]} "
            f"but the pinned plan is {plan_sha256[:12]}: re-derive it")

    phases = {p["phase"] for p in scope.get("phases") or []}
    items = {i["id"]: i for i in scope.get("items") or []}
    order = [p["phase"] for p in scope.get("phases") or []]
    numbers = [int(x[1:]) for x in order if re.fullmatch(r"P\d+", x)]
    if len(numbers) != len(order) or numbers != list(range(len(numbers))):
        problems.append(f"the plan's phases are not P0..P{len(order) - 1} in "
                        f"order: {order} -- a phase was skipped, repeated or "
                        "renamed, and a later one could be missing unnoticed")
    for i in items.values():
        if i["kind"] == "DUPLICATE":
            problems.append(f"{i['id']}: the same line appears twice in one "
                            "section of the plan")
    for i in items.values():
        if i["kind"] == "UNCLASSIFIED":
            problems.append(f"{i['id']}: section {i['section']!r} has no kind; "
                            "every item must be classified")
    for p in scope.get("phases") or []:
        if not p.get("items"):
            problems.append(f"{p['phase']}: a phase with no items at all")

    reqs = register.get("requirements") or []
    ids = [r.get("id") for r in reqs]
    for rid in sorted({x for x in ids if ids.count(x) > 1}):
        problems.append(f"requirement id {rid} is used {ids.count(rid)} times")

    mapped: dict[str, list[str]] = {}
    for r in reqs:
        phase = r.get("target_phase")
        if phase not in phases:
            problems.append(f"{r.get('id')}: target_phase {phase!r} is not a "
                            "phase of the plan")
        target = r.get("authoritative_item")
        if not target:
            problems.append(f"{r.get('id')}: no authoritative_item -- a "
                            "requirement the plan does not name is invented work")
            continue
        if target not in items:
            problems.append(f"{r.get('id')}: authoritative_item {target!r} is "
                            "not an item of the plan")
            continue
        if items[target]["phase"] != phase:
            problems.append(f"{r.get('id')}: says {phase} but its item is in "
                            f"{items[target]['phase']}")
        mapped.setdefault(target, []).append(r.get("id"))

    for target, rids in sorted(mapped.items()):
        if len(rids) > 1:
            problems.append(f"{target} is claimed by {len(rids)} requirements: "
                            + ", ".join(rids))
    for i in items.values():
        if i["kind"] in CAPABILITY_KINDS and i["id"] not in mapped:
            problems.append(f"{i['id']} ({i['phase']}, {i['kind']}): an "
                            "authoritative capability with no requirement")
    # Retired entries are kept, not deleted, and must say what replaced them.
    live = set(ids)
    for rt in register.get("retired") or []:
        if rt.get("id") in live:
            problems.append(f"{rt.get('id')} is both retired and live")
        sup = rt.get("superseded_by")
        if not sup or (sup not in live and sup not in items):
            problems.append(f"retired {rt.get('id')}: superseded_by {sup!r} is "
                            "neither a live requirement nor a plan item")
        if not rt.get("reason"):
            problems.append(f"retired {rt.get('id')}: no reason")

    # Every V3.3 contract the package names must be carried by a requirement.
    if contracts is not None:
        carried = {c for r in reqs for c in (r.get("contracts") or [])}
        for c in contracts:
            if c not in carried:
                problems.append(f"V3.3 contract {c!r} is carried by no "
                                "requirement")

    covered = {items[t]["phase"] for t in mapped if t in items}
    for p in scope.get("phases") or []:
        if p.get("capabilities") and p["phase"] not in covered:
            problems.append(f"{p['phase']}: a capability phase with no "
                            "requirement mapped to it at all")
    return problems


def dispositions(scope: dict, register: dict) -> list[dict]:
    """One row per plan item, saying what became of it. Silence is not a
    disposition: a capability names its requirement, anything else names its
    non-capability kind, and nothing is left blank.

    Pure over scope and register. The measured status of each requirement is
    the capability matrix's business and is joined in by the build plan, so
    this function never reads a generated document.
    """
    by_item: dict[str, list[str]] = {}
    for r in register.get("requirements") or []:
        if r.get("authoritative_item"):
            by_item.setdefault(r["authoritative_item"], []).append(r["id"])
    order = [p["phase"] for p in scope.get("phases") or []]
    rows = []
    for i in scope.get("items") or []:
        reqs = by_item.get(i["id"], [])
        idx = order.index(i["phase"]) if i["phase"] in order else -1
        gate = order[idx - 1] if idx > 0 else None
        if i["capability"]:
            disp = (f"mapped to {reqs[0]}" if len(reqs) == 1 else
                    ("UNMAPPED" if not reqs else f"MAPPED {len(reqs)} TIMES"))
        else:
            disp = f"non-capability: {i['kind']}" + (
                f" (tracked by {reqs[0]})" if reqs else "")
        rows.append({"item": i["id"], "phase": i["phase"],
                     "version": i.get("version"), "section": i["section"],
                     "line": i.get("line"), "kind": i["kind"],
                     "text": i["text"],
                     "capability": i["capability"],
                     "requirement": reqs[0] if len(reqs) == 1 else None,
                     "disposition": disp, "waits_for_gate": gate})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan-root", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--check", action="store_true",
                    help="compare the register against the inventory")
    args = ap.parse_args(argv)

    plan = plan_path(args.plan_root)
    if plan is None:
        print(f"the pinned plan is not reachable; set ${PLAN_ENV}. The "
              "inventory is derived from it and nothing is claimed without it",
              file=sys.stderr)
        return 3
    scope = build(plan)
    c = scope["counts"]
    print(f"{c['phases']} phases, {c['items']} items, "
          f"{c['capability_items']} capability items, "
          f"{c['unclassified']} unclassified")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(scope, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {args.out}")
    if args.check:
        if not REGISTER.is_file():
            print("no register to check", file=sys.stderr)
            return 3
        register = json.loads(REGISTER.read_text(encoding="utf-8"))
        master = plan.parent.parent / MASTER_FILE
        contracts = (json.loads(master.read_text(encoding="utf-8")).get("new_in_v3_3")
                     if master.is_file() else None)
        if contracts is None:
            print("MASTER_PLAN_V3_3.json is not reachable; the contract check "
                  "did not run", file=sys.stderr)
            return 3
        # The inventory on disk must be the one this plan produces: a stale
        # file would let the register be compared against yesterday's scope.
        if SCOPE.is_file():
            on_disk = json.loads(SCOPE.read_text(encoding="utf-8"))
            if on_disk.get("items") != scope["items"]:
                print("  PROBLEM: AUTHORITATIVE_PROGRAM_SCOPE.json does not match "
                      "a fresh parse of the pinned plan; re-derive it")
                return 1
        problems = completeness(scope, register, scope["plan_sha256"], contracts)
        for p in problems[:40]:
            print(f"  PROBLEM: {p}")
        if len(problems) > 40:
            print(f"  ... and {len(problems) - 40} more")
        print(f"{'COMPLETE' if not problems else 'INCOMPLETE'}: "
              f"{len(problems)} problem(s)")
        return 0 if not problems else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
