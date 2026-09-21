#!/usr/bin/env python3
"""The export, with a guard against overwriting work that did not come from here.

The export is one-directional: the internal tree is the source, the public
repository is a copy of its `INCLUDE`-classified paths. That held until the
distribution work was done **in the public repository** -- three merged PRs
touching fourteen files, nine of which the internal tree also carries. A plain
copy would have written the older internal version over six of them and said
nothing, because the copier compared nothing.

So this tool does not copy. It compares three states per path and refuses to
write when it cannot tell whose change it would be discarding:

* **base** -- the digest recorded the last time the two were deliberately
  synchronised, in `dogfood/export-sync/EXPORT_SYNC_STATE.json`;
* **mine** -- the internal file now;
* **theirs** -- the file at the public head now.

`mine != base, theirs == base` is our change and is written. `mine == base,
theirs != base` is their change and **stops the run before the first write**.
Both moved and disagree: also a stop. Equal is nothing to do.

Two things this deliberately does not do. It never uses a file's timestamp to
decide who is newer -- mtimes survive a copy, a checkout and a restore, and the
question here is provenance, not age. And it never deletes: a path that the
base knows and the internal tree no longer has is reported, not removed.

The remote head is read twice, before the comparison and again immediately
before the push, because a head that moves in between makes the comparison a
statement about a tree nobody is pushing to. The push is an ordinary
fast-forward push; there is no force path in this file, with or without a
lease.

Usage:
    python3 tools/export_sync.py status
    python3 tools/export_sync.py export [--push]
    python3 tools/export_sync.py record --export-commit <sha>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent

#: Where the public export is checked out. Deliberately **not** a path in this
#: file: this module ships in the export, and an absolute path to one
#: machine's home directory in a published tool is both a small leak and a
#: reason the tool cannot be run by anyone else without editing it. The
#: export's own home-path scan does not cover `tooling` files -- that scoping
#: is right, because the policy pattern files under `src/hoh/policy/` have to
#: contain home-path patterns -- so nothing would have caught it (O175).
STAGING_ENV = "VERIHARNESS_EXPORT_CHECKOUT"
STAGING = Path(os.environ[STAGING_ENV]) if os.environ.get(STAGING_ENV) else None
STATE = HOH / "dogfood/export-sync/EXPORT_SYNC_STATE.json"


def staging() -> Path:
    if STAGING is None:
        raise Abbruch(
            f"no export checkout. Set {STAGING_ENV} to the checkout that "
            "carries the public repository, or pass --staging. This is not "
            "defaulted because a default would be one machine's path in a "
            "file that ships to every reader")
    if not (STAGING / ".git").exists():
        raise Abbruch(f"{STAGING} is not a git checkout")
    return STAGING

#: Files this project *generates* from the tree on every gate run. They are
#: expected to differ between syncs for a reason that is not anyone's work, so
#: comparing them would stop every export; and a hand edit to a generated file
#: does not survive its next regeneration anyway, whoever made it.
#:
#: O174. The first draft of this set was copied from the `external_ci` row's
#: tolerance in `tools/readiness.py`, which answers a different question, and
#: it carried `CLAIMS.json`. That file is the ledger -- written, not generated
#: -- and exempting it meant a change made to it in the public repository
#: would be silently overwritten by ours. Not hypothetically: the distribution
#: work added six claims to it there, and an export under the first draft
#: would have reverted them without a word, which is the defect this whole
#: file exists to prevent, reintroduced by its own exemption list.
#:
#: The rule this set now follows: a path belongs here only if a tool in this
#: repository writes it wholesale from other inputs. `CLAIMS.md` is rendered
#: from `CLAIMS.json`; `docs/READINESS.md` is written by the board. The ledger
#: itself is neither, and is compared like everything else -- our re-anchoring
#: of it reads as ours, and a change of theirs stops the run.
BERICHTE = {"docs/READINESS.md", "CLAIMS.md"}


class Abbruch(RuntimeError):
    """Raised before any write when the comparison is not conclusive."""


def _git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise Abbruch(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def digest(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def include_pfade(root: Path) -> list[str]:
    man = json.loads((root / "EXPORT_MANIFEST.json").read_text())
    ent = man["entries"] if isinstance(man, dict) and "entries" in man else man
    return sorted(e["path"] for e in ent
                  if (e.get("classification") or e.get("decision")) == "INCLUDE")


def blob_digests(repo: Path, commit: str) -> dict[str, str]:
    """Every tracked path at `commit`, with the digest of its content.

    Read out of the object store rather than off the worktree: the worktree can
    carry an uncommitted edit, and what the public repository *has* is what its
    commit says, not what happens to be checked out beside it.
    """
    aus = _git(repo, "ls-tree", "-r", "-z", "--format=%(objectname) %(path)",
               commit)
    ergebnis = {}
    for eintrag in aus.split("\0"):
        if not eintrag.strip():
            continue
        sha, _, pfad = eintrag.partition(" ")
        inhalt = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", sha],
                                capture_output=True).stdout
        ergebnis[pfad] = digest(inhalt)
    return ergebnis


def lade_zustand() -> dict:
    if not STATE.is_file():
        # Not `relative_to(HOH)`: that raises ValueError for a path outside
        # the tree, so the message meant to explain the problem would replace
        # it with a different one -- the O171 shape, in an error path.
        try:
            wo = STATE.relative_to(HOH)
        except ValueError:
            wo = STATE
        raise Abbruch(
            f"no sync state at {wo}. Without a recorded "
            "base there is no way to tell our change from theirs, and a copy "
            "made on that ignorance is exactly what this tool exists to "
            "prevent. Record one with `record --export-commit <sha>` naming "
            "the export commit the internal tree currently matches")
    return json.loads(STATE.read_text())


def plane(basis: dict[str, str], meine: dict[str, str],
          ihre: dict[str, str]) -> dict:
    """The three-way comparison, over digests alone.

    Pure on purpose. Everything that can go wrong in this file is a question
    about which of three digests differ, and a test should be able to ask it
    without a filesystem, a git repository or a network -- so the reading of
    those three lives in the callers and this decides.
    """
    plan: dict[str, list] = {"schreiben": [], "unveraendert": [], "neu": [],
                             "konflikt": [], "nur_dort": [], "verschwunden": [],
                             "berichte": []}
    for pfad in sorted(set(meine) | set(ihre) | set(basis)):
        b, m, i = basis.get(pfad), meine.get(pfad), ihre.get(pfad)
        if pfad in BERICHTE:
            if m is not None and m != i:
                plan["berichte"].append(pfad)
            continue
        if m is not None and m == i:
            plan["unveraendert"].append(pfad)
        elif m is not None and i is None:
            (plan["neu"] if b is None else plan["konflikt"]).append(
                pfad if b is None else (pfad, "we have it, the base knew it, "
                                        "the public head no longer carries it"))
        elif m is None and i is not None:
            if b is None or i != b:
                plan["nur_dort"].append(pfad)
            else:
                plan["verschwunden"].append(pfad)
        elif m != b and i == b:
            plan["schreiben"].append(pfad)
        elif m == b and i != b:
            plan["konflikt"].append((pfad, "changed in the public repository "
                                           "since the last sync, not here"))
        else:
            plan["konflikt"].append((pfad, "changed on both sides since the "
                                           "last sync, and they disagree"))
    return plan


def vergleiche(zustand: dict, remote_head: str) -> dict:
    """`plane`, over the three states as they are right now."""
    return plane(
        zustand["path_digests"],
        {p: digest((HOH / p).read_bytes())
         for p in include_pfade(HOH) if (HOH / p).is_file()},
        blob_digests(staging(), remote_head),
    )


def nenne_hindernisse(plan: dict, *, geschrieben_haette: bool) -> bool:
    """Print what stands in the way, and say so. Returns True if any does.

    `status` used to exit 1 here and print nothing about why -- a refusal
    that withholds its reason makes the reader guess, which is the state this
    project treats as worse than a red result with a sentence next to it.
    """
    if not (plan["konflikt"] or plan["nur_dort"]):
        return False
    print()
    print("STOP -- nothing was written."
          if geschrieben_haette else "STOP -- an export would not be safe now.")
    for eintrag in plan["konflikt"]:
        pfad, grund = eintrag if isinstance(eintrag, tuple) else (eintrag, "")
        print(f"  conflict: {pfad} -- {grund}")
    for pfad in plan["nur_dort"]:
        print(f"  only in the public repository: {pfad}")
    print()
    print("Integrate those changes here, then record the new sync state with "
          "`record --export-commit <sha>`. This tool will not decide whose "
          "version survives.")
    return True


def melde(plan: dict, remote_head: str) -> None:
    print(f"public head:        {remote_head[:12]}")
    print(f"unchanged:          {len(plan['unveraendert'])}")
    print(f"to write (ours):    {len(plan['schreiben'])}")
    print(f"new (ours):         {len(plan['neu'])}")
    print(f"reports (expected): {len(plan['berichte'])}")
    for schluessel, wort in (("nur_dort", "only in the public repository"),
                             ("verschwunden", "in the base, no longer here")):
        if plan[schluessel]:
            print(f"{wort}: {len(plan[schluessel])}")
            for p in plan[schluessel][:10]:
                print(f"    {p}")


def export(push: bool) -> int:
    zustand = lade_zustand()
    _git(staging(), "fetch", "--quiet", "origin")
    kopf_vorher = _git(staging(), "rev-parse", "origin/main").strip()

    plan = vergleiche(zustand, kopf_vorher)
    melde(plan, kopf_vorher)

    if nenne_hindernisse(plan, geschrieben_haette=True):
        return 1

    geschrieben = 0
    for pfad in plan["schreiben"] + plan["neu"] + plan["berichte"]:
        ziel = staging() / pfad
        ziel.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HOH / pfad, ziel)
        geschrieben += 1
    print(f"\nwrote {geschrieben} file(s) into the staging checkout")
    if plan["verschwunden"]:
        print(f"{len(plan['verschwunden'])} path(s) the base knew are gone "
              "here and were left in place, never removed:")
        for p in plan["verschwunden"][:10]:
            print(f"    {p}")

    if not push:
        print("not pushing (pass --push)")
        return 0

    _git(staging(), "fetch", "--quiet", "origin")
    kopf_nachher = _git(staging(), "rev-parse", "origin/main").strip()
    if kopf_nachher != kopf_vorher:
        print(f"\nSTOP -- the public head moved from {kopf_vorher[:12]} to "
              f"{kopf_nachher[:12]} while this ran. The comparison above "
              "describes a tree nobody is pushing to. Re-run.")
        return 1
    print("push it from the staging checkout with an ordinary `git push`; "
          "this tool does not push for you and has no force path")
    return 0


def record(export_commit: str) -> int:
    """Record the public commit whose content has been integrated here.

    The base is a statement about **their** side, not about ours: "everything
    the public repository had at commit X is in this tree". So the digests
    stored are the ones at X. That is what lets the comparison distinguish our
    later work from theirs -- after recording X, a path we then change reads
    `mine != base, theirs == base`, which is ours and is written; a path they
    change after X reads `mine == base, theirs != base`, which stops the run.

    An earlier draft stored *our* digests and refused unless the two trees were
    identical. That made the normal case impossible to express: integrating
    their change and then continuing to work leaves the trees different on
    purpose, and there was no way to say so.

    One thing is still refused, because it is the failure this whole file is
    about: a path that X carries and this tree does not. Recording that as
    integrated would hand the next export a base in which their file is
    already accounted for while it is nowhere here -- the exact silence the
    guard exists to break.
    """
    _git(staging(), "fetch", "--quiet", "origin")
    voll = _git(staging(), "rev-parse", export_commit).strip()
    ihre = blob_digests(staging(), voll)
    meine = {p: digest((HOH / p).read_bytes())
             for p in include_pfade(HOH) if (HOH / p).is_file()}

    fehlen = sorted(p for p in ihre
                    if p not in meine and p not in BERICHTE)
    if fehlen:
        print(f"refusing to record: {voll[:12]} carries {len(fehlen)} path(s) "
              "this tree does not have. Recording it as integrated would tell "
              "the next export they are accounted for:")
        for p in fehlen[:10]:
            print(f"    {p}")
        return 1

    unsere = sorted(p for p in meine
                    if meine[p] != ihre.get(p) and p not in BERICHTE)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({
        "what": "the public export commit whose content has been integrated "
                "into this tree. Base for the three-way comparison in "
                "tools/export_sync.py: digests are the public side at that "
                "commit, so anything differing here afterwards is ours.",
        "public_repo": "SKZL-AI/veriharness",
        "synced_export_commit": voll,
        "recorded_at_utc": subprocess.run(
            ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
            capture_output=True, text=True).stdout.strip(),
        "ours_at_record_time": unsere,
        "path_digests": dict(sorted(ihre.items())),
    }, indent=2) + "\n")
    print(f"recorded {len(ihre)} path digest(s) from {voll[:12]}")
    if unsere:
        print(f"{len(unsere)} path(s) already differ here and will be treated "
              "as ours from now on:")
        for p in unsere[:10]:
            print(f"    {p}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--staging", type=Path, default=None,
                    help="the checkout carrying the public repository; "
                         f"overrides ${STAGING_ENV}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    e = sub.add_parser("export")
    e.add_argument("--push", action="store_true")
    r = sub.add_parser("record")
    r.add_argument("--export-commit", required=True)
    args = ap.parse_args(argv)
    if args.staging is not None:
        global STAGING
        STAGING = args.staging
    try:
        if args.cmd == "record":
            return record(args.export_commit)
        if args.cmd == "status":
            zustand = lade_zustand()
            _git(staging(), "fetch", "--quiet", "origin")
            kopf = _git(staging(), "rev-parse", "origin/main").strip()
            plan = vergleiche(zustand, kopf)
            melde(plan, kopf)
            return 1 if nenne_hindernisse(plan, geschrieben_haette=False) else 0
        return export(args.push)
    except Abbruch as exc:
        print(f"STOP -- {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
