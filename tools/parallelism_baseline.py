#!/usr/bin/env python3
"""What kind of parallelism this repository actually has, measured from it.

V3.3 P0. The live demo made one thing clear: three different things look the
same from outside. Several Herdr tabs are open; roles inside one run take
turns; two `hoh run` processes were started by hand. None of those is the
Project layer choosing a safe independent subset of its READY frontier and
launching it, which is what Core 1.0 requires -- and only the last one is
what "native parallelism" would mean.

So this tool answers four questions separately, and answers each from the
code rather than from a document:

    role_session_parallelism      inside one run, do the roles overlap?
    run_parallelism               can two runs execute at the same time?
    project_native_parallelism    does the Project layer launch more than one?
    integration_parallelism       is there a barrier where parallel work meets?

Each answer names the file and line that decided it. A question whose anchor
is not found is `NOT_DETERMINABLE` -- never `MISSING`, because "I could not
find it" and "it is not there" are different sentences and only one of them
is about the software.

Usage:
    python3 tools/parallelism_baseline.py
    python3 tools/parallelism_baseline.py --out program/v3_3/VERIHARNESS_PARALLELISM_BASELINE.md
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent
SRC = HOH / "src" / "hoh"

#: The statuses V3.3 allows. `NOT_DETERMINABLE` is this tool's own addition
#: and is not a status of the software: it says the probe failed.
PROVEN = "PROVEN"
IMPLEMENTED_NOT_PROVEN = "IMPLEMENTED_NOT_PROVEN"
PARTIAL = "PARTIAL"
PREPARED = "PREPARED"
MISSING = "MISSING"
SEQUENTIAL_BY_DESIGN = "SEQUENTIAL_BY_DESIGN"
NOT_DETERMINABLE = "NOT_DETERMINABLE"


@dataclass
class Finding:
    question: str
    status: str
    detail: str
    anchors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"question": self.question, "status": self.status,
                "detail": self.detail, "anchors": self.anchors}


def _source(rel: str) -> tuple[str, list[str]]:
    p = HOH / rel
    if not p.is_file():
        return "", []
    text = p.read_text(encoding="utf-8")
    return text, text.splitlines()


def _anchor(rel: str, lineno: int, lines: list[str]) -> str:
    body = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
    return f"{rel}:{lineno}  {body[:100]}"


# --------------------------------------------------------------------------- #
# The four questions
# --------------------------------------------------------------------------- #

def role_session_parallelism() -> Finding:
    """Do planner, developer and QA overlap inside one run?

    Answered from the controller's own body: if the three dispatches are
    statements in one sequence with no concurrency primitive between them,
    they are sequential. V3.3 says to keep it that way, so the honest status
    is not a gap but a design decision -- provided the code really is that.
    """
    rel = "src/hoh/controller.py"
    text, lines = _source(rel)
    if not text:
        return Finding("role_session_parallelism", NOT_DETERMINABLE,
                       f"{rel} is not in this tree", [])
    tree = ast.parse(text)
    concurrent = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.Call, ast.Attribute, ast.Name))
        and any(word in ast.dump(n)[:200]
                for word in ("ThreadPool", "ProcessPool", "concurrent",
                             "multiprocessing", "Thread("))
    ]
    anchors = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("dispatch", "run_role", "invoke")):
            anchors.append(_anchor(rel, n.lineno, lines))
    if concurrent:
        return Finding("role_session_parallelism", IMPLEMENTED_NOT_PROVEN,
                       "the controller references a concurrency primitive; "
                       "that would have to be measured, not read",
                       [_anchor(rel, n.lineno, lines) for n in concurrent[:3]])
    return Finding(
        "role_session_parallelism", SEQUENTIAL_BY_DESIGN,
        "no concurrency primitive anywhere in the controller: the roles are "
        "statements in one sequence. V3.3 asks for this to stay so, and it is "
        "not counted as a gap",
        anchors[:3] or [f"{rel}: no dispatch call found by name"])


def run_parallelism() -> Finding:
    """Can two runs execute at once, and is it the product that starts them?

    Two different questions that the demo ran together. A per-run lock means
    two *controllers* cannot fight over one run; it says nothing about whether
    two runs may proceed side by side, and nothing at all about whether
    anything in this package would start them.
    """
    rel = "src/hoh/projectstore.py"
    text, lines = _source(rel)
    lock_anchors = [
        _anchor(rel, i + 1, lines) for i, line in enumerate(lines)
        if "flock" in line or "LOCK_EX" in line
    ][:2]
    starters = []
    for name in ("orchestrator.py", "project.py", "launcher.py", "cli.py"):
        r = f"src/hoh/{name}"
        t, ls = _source(r)
        if not t:
            continue
        for i, line in enumerate(ls):
            if re.search(r"(ThreadPoolExecutor|ProcessPoolExecutor|"
                         r"multiprocessing|threading\.Thread)", line):
                starters.append(_anchor(r, i + 1, ls))
    if starters:
        return Finding("run_parallelism", IMPLEMENTED_NOT_PROVEN,
                       "something in the control plane can start work "
                       "concurrently; whether it does so safely is a "
                       "measurement, not a reading", starters[:3])
    return Finding(
        "run_parallelism", PARTIAL,
        "the lock makes one writer per run, so two runs in two processes do "
        "not corrupt each other -- an operator can and did start them by "
        "hand. Nothing in this package starts a second run, so the product "
        "does not have this capability; the operating system does",
        lock_anchors or [f"{rel}: no lock found"])


def project_native_parallelism() -> Finding:
    """Does the Project layer launch more than one READY node per round?

    The whole V3.3 question, and it has a precise answer in one line: the
    frontier is computed and then indexed. `ready()` returning a list is not
    parallelism; what is done with the list is.
    """
    rel = "src/hoh/orchestrator.py"
    text, lines = _source(rel)
    if not text:
        return Finding("project_native_parallelism", NOT_DETERMINABLE,
                       f"{rel} is not in this tree", [])
    tree = ast.parse(text)
    frontier_names: set[str] = set()
    anchors: list[str] = []
    for n in ast.walk(tree):
        # `x = state.ready()` -- remember what the frontier is called here.
        if (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and isinstance(n.value.func, ast.Attribute)
                and n.value.func.attr == "ready"):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    frontier_names.add(t.id)
            anchors.append(_anchor(rel, n.lineno, lines))
    if not frontier_names:
        return Finding("project_native_parallelism", NOT_DETERMINABLE,
                       "no call to ready() found; the probe cannot answer "
                       "from this file", anchors)
    indexed, iterated = [], []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                and n.value.id in frontier_names):
            indexed.append(_anchor(rel, n.lineno, lines))
        if (isinstance(n, ast.For) and isinstance(n.iter, ast.Name)
                and n.iter.id in frontier_names):
            iterated.append(_anchor(rel, n.lineno, lines))
    if iterated and not indexed:
        return Finding("project_native_parallelism", IMPLEMENTED_NOT_PROVEN,
                       "the frontier is iterated rather than indexed; whether "
                       "the nodes run concurrently is a measurement",
                       iterated[:3])
    return Finding(
        "project_native_parallelism", MISSING,
        "the READY frontier is computed and then one element is taken. "
        "Dependencies among the rest are measured and recorded as SERIALISED "
        "steps, which is the beginning of the selection V3.3 asks for -- but "
        "nothing selects a subset and nothing launches one",
        (anchors[:1] + indexed[:2]))


def integration_parallelism() -> Finding:
    """Is there a barrier where independently accepted work is composed?

    Global closure exists and runs when the frontier empties. That is a
    barrier in the sense that everything has finished before it; it is not an
    Integration Queue that accepted candidates from a wave pass through.
    """
    rel = "src/hoh/orchestrator.py"
    text, lines = _source(rel)
    if not text:
        return Finding("integration_parallelism", NOT_DETERMINABLE,
                       f"{rel} is not in this tree", [])
    closure = [_anchor(rel, i + 1, lines) for i, line in enumerate(lines)
               if re.search(r"def _closure|closure_generation", line)][:2]
    queue = [i + 1 for i, line in enumerate(lines)
             if re.search(r"integration_queue|IntegrationBarrier|"
                          r"integration_barrier", line)]
    if queue:
        return Finding("integration_parallelism", IMPLEMENTED_NOT_PROVEN,
                       "an integration queue or barrier is named in the "
                       "control plane; its behaviour under a wave is a "
                       "measurement", [_anchor(rel, q, lines) for q in queue[:2]])
    return Finding(
        "integration_parallelism", PREPARED,
        "global closure runs when the frontier is empty and repairs are "
        "created from its result, so the composition point exists. What does "
        "not exist is a queue that several accepted candidates from one wave "
        "pass through, because no wave produces several",
        closure or [f"{rel}: no closure found"])


# --------------------------------------------------------------------------- #
# The V3.3 contract surface
# --------------------------------------------------------------------------- #

#: Each contract V3.3 names, with the *semantic* question to ask about this
#: repository rather than the string to grep for. The plan is explicit that a
#: string search is not semantic proof, so where an equivalent exists under
#: another name it is named here and the status says so.
CONTRACTS: dict[str, tuple[str, str]] = {
    "ReadyFrontierPlanner": ("select a safe independent subset of READY",
                             "orchestrator takes ready()[0]"),
    "ParallelWaveExecutor": ("launch a selected subset concurrently", ""),
    "WaveRecord": ("persist the plan before dispatch", ""),
    "BudgetReservation": ("hold budget atomically across concurrent starts",
                          "launcher sums spent dispatches, which is durable "
                          "but not a reservation"),
    "CapacityReservation": ("hold a scarce slot across concurrent starts", ""),
    "WorkspaceLease": ("one writing node per worktree, persisted",
                       "worktrees are created per node; exclusivity is not a "
                       "first-class lease"),
    "BackpressureController": ("reduce wave width under pressure", ""),
    "ProviderHealthSentinel": ("correlate provider failures across runs",
                               "provider failure is classified per run"),
    "ValidationDomain": ("map checks to the scope they cover", ""),
    "EnvironmentCapabilityReport": ("report measured environment capability",
                                    "tools/preflight.py, V3.3 P0"),
    "ExecutionPreflight": ("refuse to start on an unsuitable environment",
                           "tools/preflight.py, V3.3 P0"),
    "OwnedAgentRegistry": ("track only sessions this project created", ""),
    "ProgramAgentRole": ("program-level specialist roles beside the run "
                         "pipeline", ""),
}


def contract_surface() -> list[dict]:
    """For each contract: is a symbol by that name here, and what stands in?"""
    names: dict[str, list[str]] = {}
    for p in sorted(SRC.glob("*.py")) + sorted((HOH / "tools").glob("*.py")):
        rel = str(p.relative_to(HOH))
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:                       # pragma: no cover
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.ClassDef, ast.FunctionDef)):
                names.setdefault(n.name, []).append(f"{rel}:{n.lineno}")
    out = []
    for contract, (question, equivalent) in sorted(CONTRACTS.items()):
        hit = names.get(contract) or []
        out.append({
            "contract": contract,
            "question": question,
            "status": (IMPLEMENTED_NOT_PROVEN if hit
                       else (PARTIAL if equivalent else MISSING)),
            "symbol": hit[:2],
            "equivalent_here": equivalent or None,
        })
    return out


def measure() -> dict:
    findings = [role_session_parallelism(), run_parallelism(),
                project_native_parallelism(), integration_parallelism()]
    return {
        "what": (
            "Parallelism baseline. Four kinds of parallelism, measured "
            "separately from the code, because from outside they look alike "
            "and only one of them is the Core 1.0 requirement."
        ),
        "measured_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "classifications": [f.as_dict() for f in findings],
        "contract_surface": contract_surface(),
    }


def render(body: dict) -> str:
    lines = ["# Parallelism baseline (V3.3 P0)", "",
             "Generated by `python3 tools/parallelism_baseline.py`. Every row",
             "names the file and line that decided it, so a reader can",
             "disagree with the verdict by reading the code rather than by",
             "trusting this document.", "",
             f"Measured at {body['measured_at_utc']}.", "",
             "## The four kinds", ""]
    for c in body["classifications"]:
        lines += [f"### `{c['question']}` — {c['status']}", "", c["detail"], ""]
        for a in c["anchors"]:
            lines.append(f"    {a}")
        lines.append("")
    lines += ["## V3.3 contract surface", "",
              "| contract | status | here |", "|---|---|---|"]
    for c in body["contract_surface"]:
        here = ", ".join(c["symbol"]) or (c["equivalent_here"] or "—")
        lines.append(f"| `{c['contract']}` | {c['status']} | {here} |")
    lines += ["",
              "`MISSING` means no symbol and no equivalent was found, not that",
              "the idea is rejected. `PARTIAL` means something here does part",
              "of the job under another name, and that name is in the last",
              "column.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    body = measure()
    if args.json:
        print(json.dumps(body, indent=2))
    else:
        for c in body["classifications"]:
            print(f"  {c['status']:<22} {c['question']}")
        missing = [c["contract"] for c in body["contract_surface"]
                   if c["status"] == MISSING]
        print(f"\n{len(missing)} of {len(body['contract_surface'])} V3.3 "
              f"contracts have neither a symbol nor an equivalent: "
              + ", ".join(missing))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(render(body) + "\n")
        args.out.with_suffix(".json").write_text(json.dumps(body, indent=2) + "\n")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
