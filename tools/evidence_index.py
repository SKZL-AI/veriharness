#!/usr/bin/env python3
"""evidence_index.py -- publish what can be published about evidence that cannot.

Two claims in this project's public documents rest on evidence trees that are
**not** in the public export: the unattended fixpoint and the STRICT
acceptance run. The reason is not coyness. A receipt records where a check ran
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

HIER = Path(__file__).resolve().parent
HOH = HIER.parent
ZIEL = HOH / "docs/EVIDENCE_INDEX.md"


def tree_digest(wurzel: Path) -> tuple[str, int, int]:
    """(digest, files, bytes) over a directory, content and relative names."""
    h = hashlib.sha256()
    dateien = sorted(
        p for p in wurzel.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )
    groesse = 0
    for p in dateien:
        h.update(str(p.relative_to(wurzel)).encode())
        h.update(b"\x00")
        roh = p.read_bytes()
        h.update(hashlib.sha256(roh).digest())
        groesse += len(roh)
    return h.hexdigest()[:16], len(dateien), groesse


def strict_zusammenfassung(wurzel: Path) -> dict:
    quittungen = sorted((wurzel / "receipts").glob("*.json"))
    zeilen = []
    for f in quittungen:
        d = json.loads(f.read_text())
        iso = d.get("isolation") or {}
        beob = iso.get("observed_namespaces") or {}
        lauf = iso.get("runner_namespaces") or {}
        zeilen.append({
            "receipt_id": d.get("receipt_id"),
            "exit_code": d.get("exit_code"),
            "runner_ok": d.get("runner_ok"),
            "effective": iso.get("effective"),
            "verified_from_inside": iso.get("verified_from_inside"),
            "mount": iso.get("candidate_mount_mode"),
            "network": iso.get("network_policy"),
            "namespaces_differ": bool(beob) and bool(lauf) and all(
                beob.get(k) and beob.get(k) != v for k, v in lauf.items()
            ),
        })
    return {"receipts": zeilen}


def unattended_zusammenfassung(wurzel: Path) -> dict:
    st = json.loads(
        (wurzel / "root/projects/unattended/project.json").read_text()
    )
    menschlich = [
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
        "decisions_by_a_person": len(menschlich),
        "external_actions": len(st.get("external_actions", [])),
    }


def rendern() -> str:
    streng = HOH / "dogfood/strict-e2e"
    unbe = HOH / "dogfood/unattended-e2e"
    s_dig, s_n, s_b = tree_digest(streng)
    u_dig, u_n, u_b = tree_digest(unbe)
    s = strict_zusammenfassung(streng)
    u = unattended_zusammenfassung(unbe)

    geehrt = sum(
        1 for r in s["receipts"]
        if r["verified_from_inside"] and r["namespaces_differ"]
        and r["mount"] == "read-only" and r["network"] == "denied"
    )

    zeilen = [
        "# Evidence index: what the claims rest on, and what is not published here",
        "",
        "Two claims in this repository rest on evidence trees that are **not** in",
        "the public export, and this file says exactly what they contain so that",
        "the gap is visible rather than quiet.",
        "",
        "## Why they are not published",
        "",
        "A receipt records where a check ran -- an arena, a worktree, a repository",
        "root. That is what makes it evidence, and it is also an absolute path on",
        "the machine that produced it. The export's own leak scan found such paths",
        "in 36 files of these two trees and refused them, which is the behaviour",
        "anyone would want from it.",
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
        f"  **{geehrt} of {len(s['receipts'])}**",
        "",
        "| receipt | exit | runner_ok | effective | verified inside | namespaces differ | mount | network |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in s["receipts"]:
        zeilen.append(
            f"| `{r['receipt_id']}` | {r['exit_code']} | {r['runner_ok']} | "
            f"{r['effective']} | {r['verified_from_inside']} | "
            f"{r['namespaces_differ']} | {r['mount']} | {r['network']} |"
        )
    zeilen += [
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
        zeilen.append(f"| `{g['name']}` | {g['outcome']} |")
    zeilen += [
        "",
        "Two gate results for the same gate name at different generations is the",
        "point, not a duplicate: the first closure found a red gate, a repair",
        "node was created and merged, and the second closure found it green.",
        "",
        "## What this index does not give you",
        "",
        "It does not let an outside reader verify the claims. It lets them see",
        "the shape and the size of what they are being asked to take on trust,",
        "and it lets anyone with the tree check that this file still describes",
        "it. Those are different things and the difference is the point.",
        "",
    ]
    return "\n".join(zeilen)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    text = rendern()
    if args.write:
        ZIEL.write_text(text, encoding="utf-8")
        print(f"wrote {ZIEL}")
    else:
        alt = ZIEL.read_text(encoding="utf-8") if ZIEL.exists() else ""
        if alt != text:
            print("EVIDENCE_INDEX.md is out of date; run with --write",
                  file=sys.stderr)
            return 1
        print("EVIDENCE_INDEX.md matches the trees it describes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
