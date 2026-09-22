#!/usr/bin/env python3
"""evidence_index.py -- publish what can be published about evidence that cannot.

Three claims in this project's public documents rest on evidence trees that are
**not** in the public export: the unattended fixpoint, the STRICT acceptance
run, and the planner capability boundary. The reason is not coyness. A receipt records where a check ran
-- an arena, a worktree, a repository root -- which is exactly what makes it
evidence and exactly what must not be published. The export's own leak scan
found absolute machine paths in 36 of those files and blocked them, correctly.

Redaction was considered and refused. A receipt carries `stdout_digest`,
computed over the transcript *including* those paths; a redacted transcript no
longer matches its own digest. Published evidence whose integrity field is
knowingly wrong is worse than evidence that is honestly absent.

So this generates the index: for each claim, what the evidence consists of,
how many artifacts, the digest of the tree, and the semantic fields that carry
no paths at all -- exit codes, isolation records, namespace comparisons,
lifecycle states. A reader with the repository that holds the tree can check
every number here against it. A reader without it can at least see precisely
what they are being asked to take on trust, and how much of it that is.

Usage:
    python3 tools/evidence_index.py [--write]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOH = HERE.parent
TARGET = HOH / "docs/EVIDENCE_INDEX.md"


def tree_digest(root: Path) -> tuple[str, int, int]:
    """(digest, files, bytes) over a directory, content and relative names."""
    h = hashlib.sha256()
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )
    size_ = 0
    for p in files:
        h.update(str(p.relative_to(root)).encode())
        h.update(b"\x00")
        raw_ = p.read_bytes()
        h.update(hashlib.sha256(raw_).digest())
        size_ += len(raw_)
    return h.hexdigest()[:16], len(files), size_


def strict_summary(root: Path) -> dict:
    receipts_ = sorted((root / "receipts").glob("*.json"))
    lines = []
    for f in receipts_:
        d = json.loads(f.read_text())
        iso = d.get("isolation") or {}
        observed_calls = iso.get("observed_namespaces") or {}
        run = iso.get("runner_namespaces") or {}
        lines.append({
            "receipt_id": d.get("receipt_id"),
            "exit_code": d.get("exit_code"),
            "runner_ok": d.get("runner_ok"),
            "effective": iso.get("effective"),
            "verified_from_inside": iso.get("verified_from_inside"),
            "mount": iso.get("candidate_mount_mode"),
            "network": iso.get("network_policy"),
            "namespaces_differ": bool(observed_calls) and bool(run) and all(
                observed_calls.get(k) and observed_calls.get(k) != v for k, v in run.items()
            ),
        })
    return {"receipts": lines}


def unattended_summary(root: Path) -> dict:
    st = json.loads(
        (root / "root/projects/unattended/project.json").read_text()
    )
    human_ = [
        d for d in st.get("decisions", [])
        if d.get("actor") not in ("orchestrator", None)
    ]
    return {
        "nodes": {n["id"]: n.get("lifecycle") for n in st.get("nodes", [])},
        "repair_nodes": [n["id"] for n in st.get("nodes", []) if n.get("repair_of")],
        "gates": [
            {"name": g["name"], "outcome": g.get("outcome")}
            for g in st.get("gates", [])
        ],
        "closure_generation": st.get("closure_generation"),
        "decisions_total": len(st.get("decisions", [])),
        "decisions_by_a_person": len(human_),
        "external_actions": len(st.get("external_actions", [])),
    }


def confinement_summary(root: Path) -> dict:
    """The path-free half of a confinement measurement.

    The counters are the claim; the paths they were read from are the part
    that cannot be published, and they are also the part a reader does not
    need in order to see what is being asserted.
    """
    s = json.loads((root / "SUMMARY.json").read_text())
    control = s.get("instrument_control") or {}
    copies_ = [
        {"matches_tree": k.get("matches_tree"),
         "differences": len(k.get("differences") or [])}
        for k in s.get("planner_copies", [])
    ]
    return {
        "verdict": s.get("planner_capability_boundary"),
        "dispatches": s.get("planner_dispatches"),
        "armed": s.get("planner_dispatches_with_an_armed_witness"),
        "metrics": {
            k: s.get(k) for k in (
                "planner_capability_violations", "planner_repo_mutations",
                "planner_git_mutations", "planner_generated_implementation",
                "planner_output_valid", "developer_can_write",
                "qa_answered", "acceptance_functions",
            )
        },
        "planner_copies": copies_,
        "copy_separate_from_candidate_arenas":
            s.get("planner_copy_separate_from_candidate_arenas"),
        "developer_touched": s.get("developer_touched"),
        "receipts": s.get("receipts"),
        "control_planted": control.get("planted"),
        "control_detected": control.get("detected"),
        "control_missed": control.get("missed") or [],
        "open": s.get("open") or [],
    }


#: Internal working documents that published files used to cite by path.
#: Listed rather than published: they are German working material carrying
#: machine-local paths, and the export refuses those for the same reason it
#: refuses the receipt trees above. Naming them here is the third option the
#: advisory left open -- visibly marked as unpublished provenance, with no
#: path for a reader to try to follow.
INTERNAL_DOCUMENTS = (
    ("*Abschlussbericht*",
     "the closing report of this project's own dogfood phase: what was "
     "built, what was measured, what was left open",
     "`DOGFOOD_LEDGER.md` (the findings, entry by entry) and "
     "`docs/LIMITATIONS.md` (what is still true)"),
    ("*Quellencheck*",
     "a source-by-source check of the position paper's external citations",
     "`paper/AUDIT.md`, which reports the same checks as verdicts"),
    ("*d2b-licenses*",
     "the internal specification for the licence and third-party review",
     "`THIRD_PARTY_NOTICES.md` and `LICENSE`"),
    ("*d4-claims*",
     "the internal specification for the claims ledger",
     "`CLAIMS.md` and the ledger's own methodology section in `CLAIMS.json`"),
    ("*d5-paper* and *d5l-limits-from-the-file*",
     "the internal specifications for the position paper and for deriving "
     "its limitations from measured files",
     "`paper/POSITION_PAPER.md` and `docs/LIMITATIONS.md`"),
    ("*Export-Dateimenge*",
     "the working note that decided which paths the export carries",
     "`EXPORT_MANIFEST.json` and its rules, which are the decision itself "
     "rather than a description of it"),
    ("*D7-Review-Auftrag*",
     "the brief given to the independent reviewers of the release candidate",
     "`paper/REVIEW_A.md` and `paper/REVIEW_B.md`, which are their reports"),
    ("the run evidence trees",
     "receipts, run states and answers from the acceptance runs",
     "the three sections above, and `docs/LIMITATIONS.md` limit 12e"),
)


def render_() -> str:
    streng = HOH / "dogfood/strict-e2e"
    unbe = HOH / "dogfood/unattended-e2e"
    confined = HOH / "dogfood/planner-confinement"
    s_dig, s_n, s_b = tree_digest(streng)
    u_dig, u_n, u_b = tree_digest(unbe)
    c_dig, c_n, c_b = tree_digest(confined)
    s = strict_summary(streng)
    u = unattended_summary(unbe)
    c = confinement_summary(confined)

    honoured = sum(
        1 for r in s["receipts"]
        if r["verified_from_inside"] and r["namespaces_differ"]
        and r["mount"] == "read-only" and r["network"] == "denied"
    )

    lines = [
        "# Evidence index: what the claims rest on, and what is not published here",
        "",
        "Three claims in this repository rest on evidence trees that are **not**",
        "in the public export, and this file says exactly what they contain so",
        "that the gap is visible rather than quiet.",
        "",
        "## Why they are not published",
        "",
        "A receipt records where a check ran -- an arena, a worktree, a repository",
        "root. That is what makes it evidence, and it is also an absolute path on",
        "the machine that produced it. The export's own leak scan found such paths",
        "in 36 files of the first two trees and refused them, which is the",
        "behaviour anyone would want from it. A run state names the worktree it",
        "ran in for the same reason and with the same consequence.",
        "",
        "Redaction was considered and refused. Each receipt carries a",
        "`stdout_digest` computed over the transcript *including* those paths; a",
        "redacted transcript no longer matches its own digest. Evidence whose",
        "integrity field is knowingly wrong is worse than evidence that is",
        "honestly absent.",
        "",
        "What follows is therefore everything that can be stated without a path:",
        "the digest of each tree, its size, and the semantic fields. Anyone",
        "holding the repository that contains these trees can re-derive every",
        "number below with `python3 tools/evidence_index.py`, and a mismatch",
        "means the tree has changed since this file was written.",
        "",
        "## STRICT acceptance run",
        "",
        f"* tree digest: `{s_dig}` over {s_n} file(s), {s_b} bytes",
        f"* receipts carrying an isolation record: {len(s['receipts'])}",
        "* receipts where isolation was **shown** from inside -- namespaces",
        "  differing from the runner's, candidate read-only, network denied:",
        f"  **{honoured} of {len(s['receipts'])}**",
        "",
        "| receipt | exit | runner_ok | effective | verified inside | namespaces differ | mount | network |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in s["receipts"]:
        lines.append(
            f"| `{r['receipt_id']}` | {r['exit_code']} | {r['runner_ok']} | "
            f"{r['effective']} | {r['verified_from_inside']} | "
            f"{r['namespaces_differ']} | {r['mount']} | {r['network']} |"
        )
    lines += [
        "",
        "The namespace ids themselves are in the tree and are not reproduced",
        "here: they are kernel inode numbers for this machine's namespaces, and",
        "they are only meaningful in comparison with the runner's own, which is",
        "the comparison the `namespaces differ` column already reports.",
        "",
        "## Unattended fixpoint",
        "",
        f"* tree digest: `{u_dig}` over {u_n} file(s), {u_b} bytes",
        "* nodes and their final lifecycle: "
        + ", ".join(f"`{k}` = {v}" for k, v in sorted(u["nodes"].items())),
        "* repair nodes the run created by itself: "
        + (", ".join(f"`{n}`" for n in u["repair_nodes"]) or "none"),
        f"* closure generations: {u['closure_generation']}",
        f"* decisions recorded: {u['decisions_total']}, "
        f"of which by a person: **{u['decisions_by_a_person']}**",
        f"* repository mutations nothing in the state accounts for: "
        f"**{u['external_actions']}**",
        "",
        "| gate | outcome |",
        "|---|---|",
    ]
    for g in u["gates"]:
        lines.append(f"| `{g['name']}` | {g['outcome']} |")
    lines += [
        "",
        "Two gate results for the same gate name at different generations is the",
        "point, not a duplicate: the first closure found a red gate, a repair",
        "node was created and merged, and the second closure found it green.",
        "",
        "## Planner capability boundary",
        "",
        f"* tree digest: `{c_dig}` over {c_n} file(s), {c_b} bytes",
        f"* verdict recorded by the measurement: **{c['verdict']}**",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    for k, v in c["metrics"].items():
        lines.append(f"| `{k}` | {v} |")
    lines += [
        "",
        "The last four are positive controls. Without them a boundary that",
        "forbade everything would score perfectly on the first four, which is",
        "the failure mode a confinement measurement is most likely to have.",
        "",
        "* the planner's copies, each against the tree it was materialised from: "
        + ", ".join(
            f"`{k['matches_tree'] or 'no match'}`"
            + ("" if not k["differences"] else f" ({k['differences']} difference(s))")
            for k in c["planner_copies"]) + "",
        "* files the accepted candidate touched: "
        + (", ".join(f"`{f}`" for f in (c["developer_touched"] or [])) or "none")
        + f", with {c['receipts']} receipt(s)",
        ("* instrument control -- seven violations planted into throwaway "
         f"copies, each detected on its own: **{c['control_detected']} of "
         f"{c['control_planted']}**"
         + (f", missed {', '.join(c['control_missed'])}" if c["control_missed"]
            else "")),
        ("* the controller's witness was armed for "
         f"**{c['armed']} of {c['dispatches']}** planner dispatch(es), read "
         "from the dispatch records themselves rather than derived from what "
         "the controller does today"),
        "",
        "How much the witness covered is a property of each dispatch: the",
        "protected set is built from what exists when it starts. A run whose",
        "records do not carry that number is reported as not readable, never",
        "as zero -- an earlier run accepted in an iteration where the set was",
        "empty, and its violation count was a true statement about nothing.",
        "",
        "The tree digests and the copy digests are the parts an outside reader",
        "cannot check. The reasoning they support travels with the evidence",
        "tree, in a README beside these files, and is therefore not exported",
        "either -- naming its path here would be a reference nobody could",
        "follow. What *is* exported is limitation 12b, which carries the same",
        "argument in `docs/LIMITATIONS.md`.",
        "",
        "## Internal working documents that published files cite",
        "",
        "The documents below drove this project's own development and are",
        "**not** in the public export: they are working material in German,",
        "full of machine-local paths and of process detail that is provenance",
        "rather than product. Published documents used to cite them by path,",
        "which gave a reader twenty-one pointers that resolve to nothing in a",
        "clone. The citations now name the document without a path and point",
        "here, so that the source is still credited and nothing looks like a",
        "broken link.",
        "",
        "| internal document | what it is | where its substance is published |",
        "|---|---|---|",
        *(f"| {name} | {was} | {where} |" for name, was, where in INTERNAL_DOCUMENTS),
        "",
        "None of them is a source for a number. Every number a published",
        "document states is carried by `CLAIMS.json`, which names the file and",
        "line it was read from, and by the tools that recompute it.",
        "",
        "## What this index does not give you",
        "",
        "It does not let an outside reader verify the claims. It lets them see",
        "the shape and the size of what they are being asked to take on trust,",
        "and it lets anyone with the tree check that this file still describes",
        "it. Those are different things and the difference is the point.",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    text = render_()
    if args.write:
        TARGET.write_text(text, encoding="utf-8")
        print(f"wrote {TARGET}")
    else:
        old = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if old != text:
            print("EVIDENCE_INDEX.md is out of date; run with --write",
                  file=sys.stderr)
            return 1
        print("EVIDENCE_INDEX.md matches the trees it describes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
