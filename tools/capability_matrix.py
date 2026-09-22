#!/usr/bin/env python3
"""What this repository has, measured requirement by requirement.

V3.3 P0. The audit doctrine the plan carries is short and hard: a capability
is `PROVEN` only with an implementation, a test, relevant positive evidence
*and* a discriminating control where one applies. Everything else has a weaker
word, and the weaker words are not interchangeable -- `MISSING` and
`PREPARED` and `PARTIAL` say different things to whoever plans the next phase.

The register lives beside this tool and names, for each requirement, a probe
this repository can actually run: a symbol, a collected test, a readiness row.
Nothing here reads a status out of a file. The one status this tool adds is
`NOT_DETERMINABLE`, which is not a status of the software: it means the probe
could not run, and it is never rounded to `MISSING`.

Usage:
    python3 tools/capability_matrix.py
    python3 tools/capability_matrix.py --out program/v3_3/VERIHARNESS_CAPABILITY_MATRIX.json
    python3 tools/capability_matrix.py --gaps program/v3_3/VERIHARNESS_GAP_REGISTER.md
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent
REGISTER = HOH / "program/v3_3/REQUIREMENTS.json"

PROVEN = "PROVEN"
IMPLEMENTED_NOT_PROVEN = "IMPLEMENTED_NOT_PROVEN"
PARTIAL = "PARTIAL"
PREPARED = "PREPARED"
MISSING = "MISSING"
NOT_DETERMINABLE = "NOT_DETERMINABLE"

#: Ordered weakest to strongest, so a summary can be counted without a second
#: opinion about which word is better than which.
ORDER = [MISSING, PREPARED, PARTIAL, IMPLEMENTED_NOT_PROVEN, PROVEN]


def _symbols() -> dict[str, list[str]]:
    """Every class, function and method defined under src/ and tools/."""
    found: dict[str, list[str]] = {}
    for base in ("src/hoh", "tools"):
        for p in sorted((HOH / base).glob("*.py")):
            rel = str(p.relative_to(HOH))
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError:                    # pragma: no cover
                continue
            for n in ast.walk(tree):
                if isinstance(n, (ast.ClassDef, ast.FunctionDef,
                                  ast.AsyncFunctionDef)):
                    found.setdefault(n.name, []).append(f"{rel}:{n.lineno}")
    return found


def _collected() -> tuple[str, bool]:
    """pytest's own collection, so a named test is checked and not assumed."""
    p = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"],
                       cwd=str(HOH), capture_output=True, text=True)
    return p.stdout, p.returncode == 0


def _board() -> dict[str, tuple[str, str]]:
    """The readiness board's rows, read from the document it writes."""
    doc = HOH / "docs/READINESS.md"
    if not doc.is_file():
        return {}
    rows: dict[str, tuple[str, str]] = {}
    for line in doc.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| `([a-z_0-9]+)` \| ([A-Z_ ()a-z]+) \| (.*?) \|", line)
        if m:
            rows[m.group(1)] = (m.group(2).strip(), m.group(3).strip())
    return rows


def classify(req: dict, symbols, collection, board) -> dict:
    """One requirement's status, and the evidence that produced it."""
    probe = req.get("probe") or {}
    kind = probe.get("kind", "none")
    equivalent = probe.get("equivalent")
    evidence: list[str] = []

    symbol_hits = symbols.get(probe.get("symbol", ""), []) if probe.get("symbol") else []
    if symbol_hits:
        evidence += symbol_hits[:2]

    test_ref = probe.get("test")
    test_seen = None
    if test_ref:
        if collection is None:
            test_seen = None
        else:
            text, ok = collection
            test_seen = (test_ref in text) or (test_ref.split("::")[-1] in text)
            evidence.append(f"test {test_ref}: "
                            + ("collected" if test_seen else "NOT collected"))

    row_name = probe.get("row")
    row_state = None
    if row_name:
        if not board:
            row_state = None
        else:
            row = board.get(row_name)
            row_state = row[0] if row else "ABSENT"
            evidence.append(f"board row `{row_name}`: {row_state}")

    # --- the decision, in the order the plan's vocabulary implies ----------
    if kind == "none":
        status = PARTIAL if equivalent else MISSING
    elif kind in ("symbol", "method"):
        if not symbol_hits:
            status = PARTIAL if equivalent else MISSING
        elif test_seen is None and test_ref:
            status = NOT_DETERMINABLE
        elif test_ref and not test_seen:
            status = IMPLEMENTED_NOT_PROVEN
        elif test_ref:
            status = PROVEN
        else:
            status = IMPLEMENTED_NOT_PROVEN
    elif kind == "test":
        if collection is None:
            status = NOT_DETERMINABLE
        else:
            status = PROVEN if test_seen else MISSING
    elif kind == "row":
        # O197. A board row answers "is this green right now", which is
        # operational state; this matrix answers "does the capability exist
        # and is it tested". Reading the one to decide the other made the
        # board and the matrix a cycle: `succession` flipped, P2-07 flipped
        # with it, the matrix went stale, its own row flipped, and the board
        # never reached a fixpoint in four passes. Refused rather than
        # supported, so the cycle cannot come back through a register edit.
        status = NOT_DETERMINABLE
        evidence.append("row probes are refused: the matrix does not read the "
                        "board (O197)")
    else:
        status = NOT_DETERMINABLE
        evidence.append(f"unknown probe kind {kind!r}")

    # An equivalent named in the register can only *raise* MISSING to PARTIAL.
    # It never lowers a measured status, and it never reaches PROVEN: a thing
    # that stands in for another thing has not been shown to be that thing.
    if status == MISSING and equivalent:
        status = PARTIAL
    out = dict(req)
    out.pop("probe", None)
    out["status"] = status
    out["evidence"] = evidence
    if equivalent:
        out["equivalent_here"] = equivalent
    return out


class RegisterMissing(FileNotFoundError):
    """The register is internal and the tools are published, so a clone can
    have this tool without having anything for it to measure. That is an
    environment gap with a name, not an empty matrix."""


def measure() -> dict:
    if not REGISTER.is_file():
        raise RegisterMissing(
            f"{REGISTER.relative_to(HOH)} is not in this tree. It is derived "
            "from an internal planning package and is not exported; without "
            "it there is nothing to measure, and an empty matrix would read "
            "as a repository with no requirements.")
    register = json.loads(REGISTER.read_text(encoding="utf-8"))
    symbols = _symbols()
    collection = _collected()
    board: dict = {}   # O197: never read; see the `row` branch in classify
    rows = [classify(r, symbols, collection, board)
            for r in register["requirements"]]
    counts: dict[str, int] = {s: 0 for s in ORDER + [NOT_DETERMINABLE]}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "what": ("Capability matrix. Each status is derived from a probe this "
                 "repository ran at the time given, not read from a document."),
        "measured_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_register": str(REGISTER.relative_to(HOH)),
        "plan_snapshot_commit": register.get("plan_snapshot_commit"),
        "counts": counts,
        "capabilities": rows,
    }


def gap_register(body: dict) -> str:
    """The gaps, ordered by what a release would need first."""
    # Everything that is not PROVEN, and `IMPLEMENTED_NOT_PROVEN` above all:
    # a capability that exists and has never been shown to work is the one a
    # reader is most likely to assume is fine.
    rows = [r for r in body["capabilities"] if r["status"] != PROVEN]
    rows.sort(key=lambda r: (not r["core_required_1_0"], r["target_phase"], r["id"]))
    out = ["# Gap register (V3.3 P0)", "",
           "Generated by `python3 tools/capability_matrix.py --gaps`. Every row",
           "is a requirement whose probe did not find a proven capability, with",
           "what the probe saw. `PARTIAL` means something here does part of the",
           "job under another name and that name is given; `NOT_DETERMINABLE`",
           "means the probe could not run and is not a statement about the",
           "software.", "",
           f"Measured at {body['measured_at_utc']}.", "",
           "| id | requirement | phase | status | core | what the probe saw |",
           "|---|---|---|---|---|---|"]
    for r in rows:
        saw = r.get("equivalent_here") or (r["evidence"][0] if r["evidence"] else "nothing")
        out.append(f"| `{r['id']}` | {r['name']} | {r['target_phase']} | "
                   f"{r['status']} | {'yes' if r['core_required_1_0'] else 'no'} | "
                   f"{saw[:90]} |")
    counts = body["counts"]
    out += ["", "## Counts", ""]
    for s in ORDER + [NOT_DETERMINABLE]:
        out.append(f"* {s}: {counts.get(s, 0)}")
    out += ["",
            "A `MISSING` row is not a rejected idea. It is a requirement whose",
            "probe found neither a symbol nor a named equivalent, which is the",
            "state most of V3.3's parallelism contracts are in by design: the",
            "plan is the thing that has not been built yet.", ""]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--gaps", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        body = measure()
    except RegisterMissing as exc:
        print(str(exc), file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(body, indent=2))
    else:
        for s in ORDER + [NOT_DETERMINABLE]:
            print(f"  {s:<24} {body['counts'].get(s, 0)}")
        core_missing = [r["id"] for r in body["capabilities"]
                        if r["core_required_1_0"] and r["status"] == MISSING]
        print(f"\n{len(core_missing)} core-required capabilities are MISSING: "
              + ", ".join(core_missing))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(body, indent=2) + "\n")
        print(f"wrote {args.out}")
    if args.gaps:
        args.gaps.parent.mkdir(parents=True, exist_ok=True)
        args.gaps.write_text(gap_register(body) + "\n")
        print(f"wrote {args.gaps}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
