#!/usr/bin/env python3
"""collect_strict_evidence.py -- install a STRICT campaign's evidence in the repo.

A run tree in a scratch directory is not evidence anybody can re-read. This
copies the parts that are, into `dogfood/strict-e2e/`:

* the run state and every receipt, including the `-basis` receipts, so the
  count of what was measured is complete;
* both sides of the namespace comparison behind `verified_from_inside`, taken
  off each receipt and **recomputed here independently**, so a reader can
  check the runner's verdict instead of taking it. The proof itself is not a
  file: it travels out of the sandbox over a pipe the runner holds, because a
  file in the scratch directory was a file the checked command could write
  (O114);
* an **instrument control**: the same measurement performed against a backend
  that runs the command unsandboxed while presenting itself as a sandbox. If
  the instrument cannot report a failure, its successes say nothing. A
  reviewer asked for exactly this and was right to.

Usage:
    python3 tools/collect_strict_evidence.py --run-root PATH --run-id ID \\
        [--into dogfood/strict-e2e]

Nothing is deleted: an existing destination is renamed with a UTC timestamp.
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER.parent / "src"))

from hoh.contracts import AcceptanceCheck, Candidate  # noqa: E402
from hoh.runner import run_check  # noqa: E402
from hoh.sandbox import MARKER_PROLOGUE, Isolation, LaunchPlan, own_namespaces  # noqa: E402


class UnsandboxedButClaiming:
    """Runs the command with no isolation at all, presenting a sandbox's plan.

    The control for `verified_from_inside`. It writes an honest marker -- the
    namespaces the command really ran in -- so the instrument has everything
    it needs and must still conclude that no isolation was applied. An
    instrument that has never been seen to say "no" is not an instrument.
    """

    name = "control-unsandboxed"

    def unavailable(self) -> str | None:
        return None

    def plan(self, argv, spec) -> LaunchPlan:
        # An honest proof of a dishonest claim: the prologue reports the
        # namespaces the command really ran in, which are the runner's own.
        return LaunchPlan(
            argv=[argv[0], "-c", MARKER_PROLOGUE + argv[2]],
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(spec.scratch),
                "TMPDIR": str(spec.scratch),
                "HOH_PROOF_FD": str(spec.proof_fd),
                "HOH_CANDIDATE": str(spec.candidate),
            },
            cwd=str(spec.candidate),
            proves_isolation=True,
        )

    def run(self, argv, spec):                   # pragma: no cover - unused
        raise NotImplementedError


def instrument_control(ziel: Path) -> dict:
    """Measures the same way against a deliberately unisolated run."""
    arena = ziel / "arena"
    arena.mkdir(parents=True, exist_ok=True)
    (arena / "hello.txt").write_text("hi\n")
    kandidat = Candidate(
        candidate_id="control", repo_path=str(arena), commit="0" * 40,
        tree_clean=True, tree_digest="0" * 16,
    )
    pruefung = AcceptanceCheck(
        check_id="CONTROL", command="cat hello.txt", expect_exit=0,
        description="the same shape of check, run without isolation",
    )
    receipt, log = run_check(
        pruefung, kandidat, run_id="control", iteration=1, attempt=1,
        cwd=arena, isolation=Isolation.STRICT, sandbox=UnsandboxedButClaiming(),
        timeout=60,
    )
    (ziel / "control-receipt.json").write_text(receipt.model_dump_json(indent=2))
    (ziel / "control-transcript.txt").write_text(log)
    iso = receipt.isolation
    return {
        "what": (
            "the same measurement against a backend that ran the command with "
            "no isolation while presenting a sandbox's launch plan"
        ),
        "requested": iso.requested,
        "effective": iso.effective,
        "fallback_to_none": iso.fallback_to_none,
        "runner_ok": receipt.runner_ok,
        "outcome": receipt.outcome(0).value,
        "honoured": iso.honoured(),
        "complaint": iso.complaint,
        "observed_namespaces": iso.observed_namespaces,
        "runner_namespaces": iso.runner_namespaces,
        "verdict": (
            "the instrument reported the absence of isolation"
            if not iso.honoured() and not receipt.runner_ok
            else "THE INSTRUMENT DID NOT NOTICE -- the positive results are worthless"
        ),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-root", required=True, type=Path)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--into", default="dogfood/strict-e2e", type=Path)
    ap.add_argument("--repo", default=".", type=Path)
    args = ap.parse_args(argv)

    quelle = args.run_root.expanduser().resolve() / args.run_id
    if not quelle.is_dir():
        print(f"no run tree at {quelle}", file=sys.stderr)
        return 2
    ziel = (args.repo.expanduser().resolve() / args.into)
    if ziel.exists():
        stempel = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        ziel.rename(ziel.with_name(f"{ziel.name}.v{stempel}"))
    ziel.mkdir(parents=True)

    # 1. state and receipts
    for name in ("state.json", "checks.json", "plan.json"):
        if (quelle / name).exists():
            shutil.copy2(quelle / name, ziel / name)
    for unter in ("receipts", "logs"):
        if (quelle / unter).is_dir():
            shutil.copytree(quelle / unter, ziel / unter)


    # 3. the control, run now against this machine
    kontrolle = instrument_control(ziel / "control")

    # 4. a README a third party can act on
    kopf = subprocess.run(
        ["git", "-C", str(args.repo.resolve()), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    quittungen = sorted((ziel / "receipts").glob("*.json"))
    geehrt = []
    for f in quittungen:
        d = json.loads(f.read_text())
        iso = d.get("isolation") or {}
        beobachtet = iso.get("observed_namespaces") or {}
        laeufer = iso.get("runner_namespaces") or {}
        # Recomputed here rather than read off `verified_from_inside`. The
        # runner's verdict and the numbers it rests on are separate things,
        # and a summary that only carried the verdict would ask a reader to
        # trust the code that has already been wrong about exactly this.
        unabhaengig = bool(beobachtet) and bool(laeufer) and all(
            beobachtet.get(k) and beobachtet.get(k) != v for k, v in laeufer.items()
        )
        geehrt.append({
            "receipt_id": d.get("receipt_id"),
            "exit_code": d.get("exit_code"),
            "runner_ok": d.get("runner_ok"),
            "requested": iso.get("requested"),
            "effective": iso.get("effective"),
            "candidate_mount_mode": iso.get("candidate_mount_mode"),
            "network_policy": iso.get("network_policy"),
            "complaint": iso.get("complaint"),
            "verified_from_inside": iso.get("verified_from_inside"),
            "observed": beobachtet,
            "runner": laeufer,
            "namespaces_differ_recomputed": unabhaengig,
        })
    (ziel / "SUMMARY.json").write_text(json.dumps({
        "run_id": args.run_id,
        "collected_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "subject_head": kopf,
        "runner_namespaces_at_collection": own_namespaces(),
        "namespace_comparison_recomputed": sum(
            1 for g in geehrt if g["namespaces_differ_recomputed"]
        ),
        "receipts": geehrt,
        "instrument_control": kontrolle,
    }, indent=2) + "\n")
    print(f"{len(quittungen)} receipts -> {ziel}")
    print(
        f"namespaces recomputed as differing on "
        f"{sum(1 for g in geehrt if g['namespaces_differ_recomputed'])}"
        f" of {len(geehrt)}"
    )
    print("instrument control:", kontrolle["verdict"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
