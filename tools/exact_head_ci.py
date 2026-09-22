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

HERE = Path(__file__).resolve().parent
HOH = HERE.parent


def export_path_digests(root: Path = HOH) -> dict[str, str]:
    """One digest per INCLUDE path, so a mismatch can be *named*.

    An aggregate digest can only say "something moved". That is not enough
    here: the readiness board and the claims ledger are themselves INCLUDE
    files that the readiness run rewrites, so every run of the gate changes
    the export it is reporting on. A row comparing aggregates would be
    permanently red for a reason that has nothing to do with the software --
    and a permanently red gate is one somebody routes around.

    With per-path digests the row can say which paths differ and decide.
    """
    manifest = root / "EXPORT_MANIFEST.json"
    data_ = json.loads(manifest.read_text(encoding="utf-8"))
    entries = data_.get("entries") if isinstance(data_, dict) else data_
    out_list = {}
    for e in entries or []:
        if e.get("decision") != "INCLUDE":
            continue
        f = root / e["path"]
        if not f.is_file():
            raise SystemExit(
                f"{e['path']} is INCLUDE but absent: the export cannot be "
                f"digested, and a CI result about it would be about nothing")
        out_list[e["path"]] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out_list


def export_content_digest(root: Path = HOH) -> tuple[str, int]:
    """(digest over the INCLUDE set's paths and bytes, number of paths).

    Deliberately over content, not over a commit: the export is a function of
    the INCLUDE set, and two different internal commits that produce the same
    export are the same thing to a CI result.
    """
    manifest = root / "EXPORT_MANIFEST.json"
    data_ = json.loads(manifest.read_text(encoding="utf-8"))
    entries = data_.get("entries") if isinstance(data_, dict) else data_
    paths = sorted(e["path"] for e in entries or []
                   if e.get("decision") == "INCLUDE")
    h = hashlib.sha256()
    n = 0
    for rel in paths:
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


def commit_path_digests(repo: Path, commit: str) -> dict[str, str]:
    """One digest per file in `commit`, read from the repository that has it.

    This is the only honest source. An earlier version digested the *working
    tree* at recording time, which is circular: if the tree had moved since
    CI ran, the recording said "this run tested this export" about bytes the
    run had never seen, and the readiness row then compared the tree with
    itself and passed trivially.

    What CI checked out is the commit, so the commit is what gets read.
    """
    p = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", commit],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(
            f"{repo} cannot read {commit[:12]}: "
            f"{(p.stderr or p.stdout).strip()[:160]}. The export commit has to "
            f"be readable somewhere, or the recording is about nothing.")
    out_list = {}
    for line in p.stdout.splitlines():
        try:
            head, path = line.split("\t", 1)
            _mode, art, blob = head.split()
        except ValueError:                          # pragma: no cover - exotic
            continue
        if art != "blob":
            continue
        content_ = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", blob],
                                capture_output=True)
        out_list[path] = hashlib.sha256(content_.stdout).hexdigest()
    return out_list


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
    ap.add_argument("--export-repo", type=Path, required=True,
                    help="a checkout that contains the export commit, so the "
                         "digests come from what CI tested rather than from "
                         "this working tree")
    ap.add_argument("--out", type=Path,
                    default=HOH / "dogfood/external-ci/EXACT_HEAD_CI.json")
    args = ap.parse_args(argv)

    one_run = json.loads(_gh("run", "view", args.run_id, "--repo", args.repo,
                          "--json", "headSha,conclusion,status,jobs"))
    if one_run["headSha"] != args.export_commit:
        raise SystemExit(
            f"run {args.run_id} ran on {one_run['headSha'][:12]}, not on the "
            f"export commit {args.export_commit[:12]}: that is a different "
            f"head and the evidence would not be about this export")

    jobs = []
    sandbox = "NOT_PRESENT"
    for j in one_run["jobs"]:
        steps_ = [{"name": s["name"], "conclusion": s["conclusion"]}
                    for s in j.get("steps") or []]
        jobs.append({"name": j["name"], "conclusion": j["conclusion"],
                     "steps": steps_})
        if "sandbox" in j["name"].lower():
            real = [s for s in steps_ if "must actually run" in s["name"]]
            if real and real[0]["conclusion"] == "skipped":
                sandbox = "UNSUPPORTED_ENVIRONMENT"
            elif real and real[0]["conclusion"] == "success":
                sandbox = "VERIFIED"
            else:
                sandbox = "NOT_DETERMINABLE"

    per_path = commit_path_digests(args.export_repo, args.export_commit)
    h = hashlib.sha256()
    for rel in sorted(per_path):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(bytes.fromhex(per_path[rel]))
    digest, n = h.hexdigest(), len(per_path)
    internal_ = subprocess.run(["git", "-C", str(HOH), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    data_ = {
        "repo": args.repo,
        "run_id": args.run_id,
        "run_conclusion": one_run["conclusion"],
        "run_status": one_run["status"],
        "export_commit": args.export_commit,
        "internal_commit": internal_,
        "export_content_digest": digest,
        "export_paths": n,
        "path_digests": per_path,
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
    args.out.write_text(json.dumps(data_, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out.relative_to(HOH)}")
    for j in jobs:
        print(f"  {j['conclusion']:<10s} {j['name']}")
    print(f"  sandbox_external_env = {sandbox}")
    print(f"  export digest {digest[:16]} over {n} path(s)")
    return 0 if one_run["conclusion"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
