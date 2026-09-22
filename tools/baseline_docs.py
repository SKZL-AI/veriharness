#!/usr/bin/env python3
"""The three P0 baselines that are summaries of measurements already taken.

V3.3 P0 asks for a current baseline, a product baseline and a contract trace.
None of them is a new measurement: the capability matrix, the readiness board,
the export manifest and the CLI's own parser already know the answers. So
these are renderings, and the only thing that can go wrong is a rendering that
says more than its source.

Two rules hold that line:

* **Every figure names where it came from.** A number without a source is not
  written; the line says `NOT_MEASURED` and why instead.
* **`NOT_MEASURED` is used, often.** "Time to first verified result" cannot be
  read out of this repository: it depends on a provider, a machine and a
  person. A product baseline that guessed it would be the most quoted number
  in the document and the only invented one.

Usage:
    python3 tools/baseline_docs.py --out-dir program/v3_3
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

NOT_MEASURED = "NOT_MEASURED"


def _matrix():
    spec = importlib.util.spec_from_file_location(
        "capability_matrix", HERE / "capability_matrix.py")
    cm = importlib.util.module_from_spec(spec)
    sys.modules["capability_matrix"] = cm
    spec.loader.exec_module(cm)
    return cm


def cli_surface() -> dict:
    """The CLI's commands, read from the parser rather than from the README.

    A command list in a document is a claim about software; a command list
    from `argparse` is the software.
    """
    p = subprocess.run([sys.executable, "-m", "hoh.cli", "--help"],
                       cwd=str(HOH), capture_output=True, text=True,
                       env={"PYTHONPATH": "src", "HERDR_ENV": "1",
                            "PATH": __import__("os").environ.get("PATH", "")})
    if p.returncode != 0:
        return {"commands": [], "source": NOT_MEASURED,
                "why": f"the CLI did not answer --help: {p.stderr.strip()[:90]}"}
    m = re.search(r"\{([a-z,\-]+)\}", p.stdout)
    commands = sorted(m.group(1).split(",")) if m else []
    return {"commands": commands, "source": "python3 -m hoh.cli --help"}


def package_shape() -> dict:
    modules, classes, functions = [], 0, 0
    for f in sorted((HOH / "src/hoh").glob("*.py")):
        modules.append(f.name)
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                classes += 1
            elif isinstance(n, ast.FunctionDef):
                functions += 1
    return {"modules": modules, "classes": classes, "functions": functions,
            "source": "ast over src/hoh/*.py"}


def board_rows() -> dict:
    doc = HOH / "docs/READINESS.md"
    if not doc.is_file():
        return {"rows": {}, "source": NOT_MEASURED, "why": "no readiness board"}
    rows = {}
    for line in doc.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| `([a-z_0-9]+)` \| ([A-Z_ ()a-z]+) \|", line)
        if m:
            rows[m.group(1)] = m.group(2).strip()
    verdict = re.search(r"TECHNICALLY_STABLE_READY = (\w+)",
                        doc.read_text(encoding="utf-8"))
    return {"rows": rows, "verdict": verdict.group(1) if verdict else None,
            "source": "docs/READINESS.md"}


def manifest_shape() -> dict:
    p = HOH / "EXPORT_MANIFEST.json"
    if not p.is_file():
        return {"source": NOT_MEASURED, "why": "no export manifest"}
    entries = json.loads(p.read_text())["entries"]
    include = [e for e in entries if e["decision"] == "INCLUDE"]
    return {"entries": len(entries), "included": len(include),
            "python_included": len([e for e in include
                                    if e["path"].endswith(".py")]),
            "source": "EXPORT_MANIFEST.json"}


def configuration_decisions() -> dict:
    """Environment variables the tools and the package read.

    The plan asks how many configuration decisions an operator faces. This is
    the answerable part of it: every variable something here reads is a
    decision somebody can be asked to make.
    """
    names: dict[str, list[str]] = {}
    for base in ("src/hoh", "tools"):
        for f in sorted((HOH / base).glob("*.py")):
            rel = str(f.relative_to(HOH))
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:                    # pragma: no cover
                continue
            # Module-level `NAME = "SOME_VAR"`, because a variable read
            # through a constant is still a variable. A regular expression
            # over the source saw only the literal spelling and missed
            # `VERIHARNESS_EXPORT_CHECKOUT`, which is exactly the shape of
            # under-counting this document exists to avoid.
            constants = {
                t.id: n.value.value
                for n in tree.body if isinstance(n, ast.Assign)
                for t in n.targets
                if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)
            }

            def _name(node) -> str | None:
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    return node.value
                if isinstance(node, ast.Name):
                    return constants.get(node.id)
                return None

            for n in ast.walk(tree):
                target = None
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr in ("get", "setdefault")
                        and isinstance(n.func.value, ast.Attribute)
                        and n.func.value.attr == "environ" and n.args):
                    target = _name(n.args[0])
                elif (isinstance(n, ast.Subscript)
                      and isinstance(n.value, ast.Attribute)
                      and n.value.attr == "environ"):
                    target = _name(n.slice)
                if target and re.fullmatch(r"[A-Z][A-Z_0-9]*", target):
                    names.setdefault(target, []).append(rel)
    return {"variables": {k: sorted(set(v)) for k, v in sorted(names.items())},
            "count": len(names),
            "source": "os.environ reads in src/ and tools/, resolved through "
                      "module-level constants"}


def current_baseline(body: dict) -> str:
    cli, pkg = body["cli"], body["package"]
    counts = body["matrix_counts"]
    out = ["# Current baseline (V3.3 P0)", "",
           "Generated by `python3 tools/baseline_docs.py`. Every figure names",
           "the command or file it was read from. Nothing here is a figure",
           "somebody remembered.", "",
           f"Measured at {body['measured_at_utc']}.", "",
           "## What the package is", "",
           f"* {len(pkg['modules'])} modules, {pkg['classes']} classes, "
           f"{pkg['functions']} module-level functions — `{pkg['source']}`",
           f"* CLI commands: {', '.join('`' + c + '`' for c in cli['commands']) or NOT_MEASURED}"
           f" — `{cli['source']}`",
           f"* export manifest: {body['manifest'].get('entries', NOT_MEASURED)} "
           f"entries, {body['manifest'].get('included', NOT_MEASURED)} published"
           f" — `{body['manifest']['source']}`", "",
           "## The board", "",
           "Not reproduced here. `docs/READINESS.md` is its own generated",
           "document, and embedding its verdict made this one depend on it",
           "while the board checks this one -- a cycle that never settled",
           "(O197).", "",
           "## What the capability matrix says", ""]
    for status in ("PROVEN", "IMPLEMENTED_NOT_PROVEN", "PARTIAL", "PREPARED",
                   "MISSING", "NOT_DETERMINABLE"):
        out.append(f"* {status}: {counts.get(status, 0)} — "
                   f"`{body['matrix_source']}`")
    out.append("")
    return "\n".join(out)


def product_baseline(body: dict) -> str:
    cfg = body["config"]
    out = ["# Product baseline (V3.3 P0)", "",
           "Generated by `python3 tools/baseline_docs.py`. The plan asks for",
           "installation steps, time to first run, time to first verified",
           "result, manual interventions, documentation touchpoints and the",
           "number of configuration decisions. Three of those are properties",
           "of a person and a machine rather than of this repository, and they",
           "are marked `NOT_MEASURED` with the reason rather than estimated.",
           "", f"Measured at {body['measured_at_utc']}.", "",
           "| question | answer | source |", "|---|---|---|"]
    # Pointers, not copied values (O197): the board checks this document, so
    # this document cannot carry the board's current state without the two
    # chasing each other.
    out += [
        "| does a fresh clone install and import | see board row "
        "`clean_install` | `tools/clean_install_check.py` |",
        "| does a fresh environment pass preflight | see board row "
        "`preflight` | `tools/preflight.py --profile demo` |",
        f"| configuration decisions an operator can face | {cfg['count']} "
        f"environment variables | `{cfg['source']}` |",
        f"| time to first run | {NOT_MEASURED} | depends on a provider, a "
        f"machine and a person; measuring it here would produce a number about "
        f"this machine and publish it as a number about the product |",
        f"| time to first verified result | {NOT_MEASURED} | same, and it also "
        f"depends on the task |",
        f"| manual interventions per run | {NOT_MEASURED} | the run ledger "
        f"records halts; a rate needs a campaign, and the campaigns this "
        f"repository has are benchmarks rather than product usage |",
        "", "## The configuration decisions, named", "",
        "| variable | read by |", "|---|---|"]
    for name, files in cfg["variables"].items():
        out.append(f"| `{name}` | {', '.join('`' + f + '`' for f in files[:3])} |")
    out += ["",
            "A variable with no default is a decision the operator must make;",
            "one with a default is a decision they may ignore. That",
            "distinction is not derivable from a regular expression over",
            "`os.environ`, so it is not claimed here.", ""]
    return "\n".join(out)


def contract_trace(body: dict) -> str:
    out = ["# Contract trace baseline (V3.3 P0)", "",
           "Generated by `python3 tools/baseline_docs.py`. One row per",
           "requirement in the register, with the symbol or board row that",
           "answers it. A requirement with no trace is not a failure of this",
           "document: it is the plan describing work that has not been done.",
           "", f"Measured at {body['measured_at_utc']}.", "",
           "| id | plan item | requirement | status | traces to |",
           "|---|---|---|---|---|"]
    for cap in body["capabilities"]:
        trace = "; ".join(cap.get("evidence") or []) or "—"
        item = cap.get("authoritative_item") or "—"
        out.append(f"| `{cap['id']}` | `{item}` | {cap['name'][:60]} | "
                   f"{cap['status']} | {trace[:90]} |")
    out.append("")
    return "\n".join(out)


def measure() -> dict:
    cm = _matrix()
    matrix = cm.measure()
    return {
        "measured_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cli": cli_surface(),
        "package": package_shape(),
        "board": board_rows(),
        "manifest": manifest_shape(),
        "config": configuration_decisions(),
        "matrix_counts": matrix["counts"],
        "matrix_source": matrix["source_register"],
        "capabilities": matrix["capabilities"],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", type=Path, default=None)
    args = ap.parse_args(argv)
    cm = _matrix()
    try:
        body = measure()
    except cm.RegisterMissing as exc:
        print(str(exc), file=sys.stderr)
        return 3
    documents = {
        "VERIHARNESS_CURRENT_BASELINE.md": current_baseline(body),
        "VERIHARNESS_PRODUCT_BASELINE.md": product_baseline(body),
        "VERIHARNESS_CONTRACT_TRACE_BASELINE.md": contract_trace(body),
    }
    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for name, text in documents.items():
            (args.out_dir / name).write_text(text + "\n")
            print(f"wrote {args.out_dir / name}")
    else:
        for name in documents:
            print(f"  would write {name}")
    print(f"{body['config']['count']} configuration variable(s); "
          f"{len(body['cli']['commands'])} CLI command(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
