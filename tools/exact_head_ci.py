#!/usr/bin/env python3
"""Record that an external CI ran against the exact export this tree produces.

The internal working tree has no git remote, deliberately: the house rule that
keeps measurements off the network is the reason the public repository is
updated from a separate staging checkout carrying only INCLUDE-classified
paths. So `git remote` here is empty by design -- and a readiness row that asks
it can never be satisfied, which makes the release gate unreachable rather
than strict.

The evidence exists; it is just somewhere else. This records it, and binds it
to *what was tested* rather than to a branch name:

* `internal_commit` -- the commit of the tree the export was derived from;
* `export_commit` -- the commit CI actually ran on, in the public repository;
* `export_content_digest` -- a digest over every INCLUDE path and its
  contents. This is the load-bearing field. A CI result is evidence about a
  set of bytes, and if the INCLUDE set changes afterwards the recorded run is
  no longer about the thing being released. The readiness row recomputes this
  from the working tree and refuses the evidence when it differs.

The sandbox job is recorded per **step**, not per job. Its job is green on
runners that cannot create the namespace it needs, because the step is skipped
-- and a green job whose relevant step did not run has measured nothing. It is
reported `UNSUPPORTED_ENVIRONMENT`, never derived as a pass.

Usage:
    python3 tools/exact_head_ci.py --run-id <id> --export-commit <sha> \\
        [--repo owner/name] [--out PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

HIER = Path(__file__).resolve().parent
HOH = HIER.parent


def export_content_digest(root: Path = HOH) -> tuple[str, int]:
    """(digest over the INCLUDE set's paths and bytes, number of paths).

    Deliberately over content, not over a commit: the export is a function of
    the INCLUDE set, and two different internal commits that produce the same
    export are the same thing to a CI result.
    """
    manifest = root / "EXPORT_MANIFEST.json"
    daten = json.loads(manifest.read_text(encoding="utf-8"))
    eintraege = daten.get("entries") if isinstance(daten, dict) else daten
    pfade = sorted(e["path"] for e in eintraege or []
                   if e.get("decision") == "INCLUDE")
    h = hashlib.sha256()
    n = 0
    for rel in pfade:
        f = root / rel
        if not f.is_file():
            raise SystemExit(
                f"{rel} is INCLUDE but absent: the export cannot be digested, "
                f"and a CI result about it would be about nothing")
        h.update(rel.encode())
        h.update(b"\0")
        h.update(hashlib.sha256(f.read_bytes()).digest())
        n += 1
    return h.hexdigest(), n


def _gh(*args: str) -> str:
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"gh failed: {(p.stderr or p.stdout).strip()[:200]}")
    return p.stdout


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--export-commit", required=True)
    ap.add_argument("--repo", default="SKZL-AI/veriharness")
    ap.add_argument("--out", type=Path,
                    default=HOH / "dogfood/external-ci/EXACT_HEAD_CI.json")
    args = ap.parse_args(argv)

    lauf = json.loads(_gh("run", "view", args.run_id, "--repo", args.repo,
                          "--json", "headSha,conclusion,status,jobs"))
    if lauf["headSha"] != args.export_commit:
        raise SystemExit(
            f"run {args.run_id} ran on {lauf['headSha'][:12]}, not on the "
            f"export commit {args.export_commit[:12]}: that is a different "
            f"head and the evidence would not be about this export")

    jobs = []
    sandbox = "NOT_PRESENT"
    for j in lauf["jobs"]:
        schritte = [{"name": s["name"], "conclusion": s["conclusion"]}
                    for s in j.get("steps") or []]
        jobs.append({"name": j["name"], "conclusion": j["conclusion"],
                     "steps": schritte})
        if "sandbox" in j["name"].lower():
            echt = [s for s in schritte if "must actually run" in s["name"]]
            if echt and echt[0]["conclusion"] == "skipped":
                sandbox = "UNSUPPORTED_ENVIRONMENT"
            elif echt and echt[0]["conclusion"] == "success":
                sandbox = "VERIFIED"
            else:
                sandbox = "NOT_DETERMINABLE"

    digest, n = export_content_digest()
    intern = subprocess.run(["git", "-C", str(HOH), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    daten = {
        "repo": args.repo,
        "run_id": args.run_id,
        "run_conclusion": lauf["conclusion"],
        "run_status": lauf["status"],
        "export_commit": args.export_commit,
        "internal_commit": intern,
        "export_content_digest": digest,
        "export_paths": n,
        "jobs": jobs,
        "sandbox_external_env": sandbox,
        "measured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "what_this_is_not": (
            "not a statement that the sandbox was exercised: its job is green "
            "on runners that cannot create the namespace it needs, because the "
            "step is skipped, and a green job whose relevant step did not run "
            "has measured nothing."),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(daten, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out.relative_to(HOH)}")
    for j in jobs:
        print(f"  {j['conclusion']:<10s} {j['name']}")
    print(f"  sandbox_external_env = {sandbox}")
    print(f"  export digest {digest[:16]} over {n} path(s)")
    return 0 if lauf["conclusion"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
