#!/usr/bin/env python3
"""Regenerate the derived documents and the board, in order, to a fixpoint.

O194. Four documents are generated from measurements, and one of those
measurements is the readiness board -- which is itself generated, and which
runs the tools that write the other four. So a change anywhere moves the
board, which moves the documents, which moves the board's rows about those
documents. Each pass is correct; no single pass is final.

The answer is not to break the dependency. The board is the right source for
a row's state, and re-implementing every row's question inside the matrix
would be two implementations of one question -- the defect O192 recorded the
day before this tool was written. The answer is to run the cycle until it
stops moving, and to say so when it does not.

What it will not do: stop early and report success. If the board is still
changing after `--max-passes`, that is a cycle rather than slow convergence,
and a fixpoint nobody reached is not a fixpoint.

Usage:
    python3 tools/regenerate.py
    python3 tools/regenerate.py --max-passes 8
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent
PROGRAM = HOH / "program/v3_3"
BOARD = HOH / "docs/READINESS.md"

#: In order. The board is last in every pass because it reads what the others
#: write; the others are in dependency order among themselves.
STEPS: tuple[tuple[str, list[str]], ...] = (
    ("parallelism baseline", [
        "tools/parallelism_baseline.py",
        "--out", str(PROGRAM / "VERIHARNESS_PARALLELISM_BASELINE.md")]),
    ("capability matrix", [
        "tools/capability_matrix.py",
        "--out", str(PROGRAM / "VERIHARNESS_CAPABILITY_MATRIX.json"),
        "--gaps", str(PROGRAM / "VERIHARNESS_GAP_REGISTER.md")]),
    ("build plan", [
        "tools/build_plan.py",
        "--dag", str(PROGRAM / "VERIHARNESS_IMPLEMENTATION_DAG.json"),
        "--checklist", str(PROGRAM / "VERIHARNESS_BUILD_CHECKLIST.json"),
        "--markdown", str(PROGRAM / "VERIHARNESS_BUILD_CHECKLIST.md")]),
    ("baseline documents", [
        "tools/baseline_docs.py", "--out-dir", str(PROGRAM)]),
    ("readiness board", ["tools/readiness.py", "--write"]),
)


def _digest(path: Path) -> str:
    """The board's content, ignoring the line that says when it was written."""
    if not path.is_file():
        return ""
    lines = [z for z in path.read_text(encoding="utf-8").splitlines()
             if not z.startswith("Measured at ")]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def run_once(env: dict) -> list[str]:
    """One pass. Returns the steps that failed, in order."""
    failed = []
    for name, argv in STEPS:
        p = subprocess.run([sys.executable, *argv], cwd=str(HOH),
                           capture_output=True, text=True, env=env)
        # Two exit codes are answers rather than failures. 3 is "nothing to
        # measure here": a published clone has the tools and not the
        # register, and regenerating there should say so rather than stop. 1
        # from the board is its *verdict* -- rows are open -- which is the
        # thing the board exists to report, and treating it as a broken step
        # would make a red board indistinguishable from a crashed tool.
        allowed = (0, 3, 1) if name == "readiness board" else (0, 3)
        if p.returncode not in allowed:
            failed.append(f"{name}: exit {p.returncode} "
                          f"{(p.stderr or p.stdout).strip()[-120:]}")
    return failed


def regenerate(max_passes: int = 5, env: dict | None = None) -> dict:
    env = {**os.environ, **(env or {})}
    seen: list[str] = [_digest(BOARD)]
    for i in range(1, max_passes + 1):
        failed = run_once(env)
        if failed:
            return {"converged": False, "passes": i, "failed": failed,
                    "why": "a step failed, so the cycle was not completed"}
        now = _digest(BOARD)
        if now == seen[-1]:
            return {"converged": True, "passes": i, "failed": [],
                    "why": "the board stopped changing"}
        seen.append(now)
    return {"converged": False, "passes": max_passes, "failed": [],
            "why": (f"the board was still changing after {max_passes} passes, "
                    "which is a cycle rather than slow convergence")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-passes", type=int, default=5)
    args = ap.parse_args(argv)
    result = regenerate(args.max_passes)
    for line in result["failed"]:
        print(f"  FAILED {line}")
    print(f"{'converged' if result['converged'] else 'DID NOT CONVERGE'} "
          f"after {result['passes']} pass(es): {result['why']}")
    return 0 if result["converged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
