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
from datetime import UTC, datetime
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
        raise Abort(
            f"no export checkout. Set {STAGING_ENV} to the checkout that "
            "carries the public repository, or pass --staging. This is not "
            "defaulted because a default would be one machine's path in a "
            "file that ships to every reader")
    if not (STAGING / ".git").exists():
        raise Abort(f"{STAGING} is not a git checkout")
    return STAGING

#: No path is exempt from the comparison. The set is kept, empty, because
#: its history is the argument for why it is empty.
#:
#: It began as `{docs/READINESS.md, CLAIMS.md, CLAIMS.json}`, copied out of
#: the `external_ci` row's tolerance in `tools/readiness.py` -- a different
#: question -- and O174 removed the ledger from it after an attack showed six
#: public claims would have been reverted in silence. An independent review
#: then asked the obvious next question: why are the other two exempt at all?
#:
#: The answer was "they are generated, so a public edit to them does not
#: survive regeneration anyway". That is true of the *content* and irrelevant
#: to the *decision*: "generated" does not prove a public change is
#: dispensable, and a file being regenerable says nothing about whether
#: somebody added something to it that our regeneration will drop -- which is
#: precisely what O176 was.
#:
#: The exemption also turned out to be unnecessary. The case it was written
#: for -- this project rewrites these files on every gate run -- reads as
#: `mine != base, theirs == base`, which is already "ours" and already
#: written. The case it was silently covering was `theirs != base`, which is
#: somebody else's change and must stop. A stale base makes both sides look
#: moved; the answer to that is to record the base after a push, which the
#: tool now says in as many words, and the failure direction is a stop rather
#: than an overwrite.
#:
#: So a generated file that legitimately replaces public content does it the
#: same way every other file does: integrate, then record the new base. That
#: record is the explicit, checkable regeneration evidence.
REPORTS: frozenset[str] = frozenset()


class Abort(RuntimeError):
    """Raised before any write when the comparison is not conclusive."""


def _git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise Abort(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def digest(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def include_paths(root: Path) -> list[str]:
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
    out = _git(repo, "ls-tree", "-r", "-z", "--format=%(objectname) %(path)",
               commit)
    result = {}
    for entry in out.split("\0"):
        if not entry.strip():
            continue
        sha, _, path = entry.partition(" ")
        content_ = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", sha],
                                capture_output=True).stdout
        result[path] = digest(content_)
    return result


def staging_dirt(repo: Path) -> dict[str, str]:
    """Every path the staging checkout has touched and not committed.

    Staged, unstaged and untracked alike, because all three are somebody's
    work and this tool writes over the working tree. `--porcelain=v1 -z` so
    that a filename with a space, a quote or a newline in it is parsed and
    not guessed at; `--untracked-files=all` so that a new file inside an
    untracked directory is seen individually rather than as its parent.
    """
    out = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    fields_ = out.split("\0")
    dirt: dict[str, str] = {}
    i = 0
    while i < len(fields_):
        entry = fields_[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if xy[0] == "R":
            # A rename carries its source in the next field.
            if i < len(fields_):
                dirt[fields_[i]] = xy
                i += 1
        dirt[path] = xy
    return dirt


def staging_check(repo: Path, head: str, write_ops: list[str]) -> list[str]:
    """Reasons the staging checkout is not safe to write into right now.

    The comparison above is about `origin/main`. This is about the tree the
    files actually land in, and the two are not the same thing -- which is
    how the first version of this tool came to replace uncommitted work in
    the staging checkout with our version and report success. Two questions,
    both answered before the first write:

    * is the checkout at the commit the comparison was made against? If not,
      the result of writing into it is a mixture of two states that nobody
      reviewed;
    * does anything we would write collide with work that is here and not
      committed? Staged, unstaged and untracked all count.

    Never resolved automatically. No reset, no stash, no removal: the whole
    point is that this tool does not decide whose work survives.
    """
    reasons = []
    head_here = _git(repo, "rev-parse", "HEAD").strip()
    if head_here != head:
        reasons.append(
            f"the staging checkout is at {head_here[:12]}, not at the head "
            f"this comparison was made against ({head[:12]}). Writing here "
            "would mix two states; check it out first")
    dirt = staging_dirt(repo)
    clash = sorted(set(dirt) & set(write_ops))
    if clash:
        reasons.append(
            f"{len(clash)} path(s) we would write are modified and not "
            "committed in the staging checkout: "
            + ", ".join(f"{p} [{dirt[p].strip() or '??'}]"
                        for p in clash[:10]))
    otherwise = sorted(set(dirt) - set(write_ops))
    if otherwise:
        reasons.append(
            f"(not a collision, reported so it is not lost from view: "
            f"{len(otherwise)} other uncommitted path(s) here, e.g. "
            + ", ".join(otherwise[:5]) + ")")
    return reasons


def load_state() -> dict:
    if not STATE.is_file():
        # Not `relative_to(HOH)`: that raises ValueError for a path outside
        # the tree, so the message meant to explain the problem would replace
        # it with a different one -- the O171 shape, in an error path.
        try:
            where = STATE.relative_to(HOH)
        except ValueError:
            where = STATE
        raise Abort(
            f"no sync state at {where}. Without a recorded "
            "base there is no way to tell our change from theirs, and a copy "
            "made on that ignorance is exactly what this tool exists to "
            "prevent. Record one with `record --export-commit <sha>` naming "
            "the export commit the internal tree currently matches")
    return json.loads(STATE.read_text())


def plane(baseline: dict[str, str], meine: dict[str, str],
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
    for path in sorted(set(meine) | set(ihre) | set(baseline)):
        b, m, i = baseline.get(path), meine.get(path), ihre.get(path)
        if path in REPORTS:
            if m is not None and m != i:
                plan["berichte"].append(path)
            continue
        if m is not None and m == i:
            plan["unveraendert"].append(path)
        elif m is not None and i is None:
            (plan["neu"] if b is None else plan["konflikt"]).append(
                path if b is None else (path, "we have it, the base knew it, "
                                        "the public head no longer carries it"))
        elif m is None and i is not None:
            if b is None or i != b:
                plan["nur_dort"].append(path)
            else:
                plan["verschwunden"].append(path)
        elif m != b and i == b:
            plan["schreiben"].append(path)
        elif m == b and i != b:
            plan["konflikt"].append((path, "changed in the public repository "
                                           "since the last sync, not here"))
        else:
            plan["konflikt"].append((path, "changed on both sides since the "
                                           "last sync, and they disagree"))
    return plan


def comparisons(state: dict, remote_head: str) -> dict:
    """`plane`, over the three states as they are right now."""
    return plane(
        state["path_digests"],
        {p: digest((HOH / p).read_bytes())
         for p in include_paths(HOH) if (HOH / p).is_file()},
        blob_digests(staging(), remote_head),
    )


def name_obstacles(plan: dict, *, would_have_written: bool) -> bool:
    """Print what stands in the way, and say so. Returns True if any does.

    `status` used to exit 1 here and print nothing about why -- a refusal
    that withholds its reason makes the reader guess, which is the state this
    project treats as worse than a red result with a sentence next to it.
    """
    if not (plan["konflikt"] or plan["nur_dort"]):
        return False
    print()
    print("STOP -- nothing was written."
          if would_have_written else "STOP -- an export would not be safe now.")
    for entry in plan["konflikt"]:
        path, reason = entry if isinstance(entry, tuple) else (entry, "")
        print(f"  conflict: {path} -- {reason}")
    for path in plan["nur_dort"]:
        print(f"  only in the public repository: {path}")
    print()
    print("Integrate those changes here, then record the new sync state with "
          "`record --export-commit <sha>`. This tool will not decide whose "
          "version survives.")
    return True


def report_(plan: dict, remote_head: str) -> None:
    print(f"public head:        {remote_head[:12]}")
    print(f"unchanged:          {len(plan['unveraendert'])}")
    print(f"to write (ours):    {len(plan['schreiben'])}")
    print(f"new (ours):         {len(plan['neu'])}")
    print(f"reports (expected): {len(plan['berichte'])}")
    for key, word_ in (("nur_dort", "only in the public repository"),
                             ("verschwunden", "in the base, no longer here")):
        if plan[key]:
            print(f"{word_}: {len(plan[key])}")
            for p in plan[key][:10]:
                print(f"    {p}")


def export(push: bool) -> int:
    state = load_state()
    _git(staging(), "fetch", "--quiet", "origin")
    head_before = _git(staging(), "rev-parse", "origin/main").strip()

    plan = comparisons(state, head_before)
    report_(plan, head_before)

    if name_obstacles(plan, would_have_written=True):
        return 1

    to_write = plan["schreiben"] + plan["neu"] + plan["berichte"]
    obstacles = staging_check(staging(), head_before, to_write)
    serious = [g for g in obstacles if not g.startswith("(")]
    for g in obstacles:
        print(f"  staging: {g}")
    if serious:
        print()
        print("STOP -- nothing was written. The comparison was about the "
              "public head; this is about the tree the files land in, and it "
              "is not in a state this tool may write into. Resolve it there "
              "-- commit, move aside, or check out the right head. Nothing "
              "will be reset, stashed or removed from here.")
        return 1

    written = 0
    for path in to_write:
        target = staging() / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HOH / path, target)
        written += 1
    print(f"\nwrote {written} file(s) into the staging checkout")
    if plan["verschwunden"]:
        print(f"{len(plan['verschwunden'])} path(s) the base knew are gone "
              "here and were left in place, never removed:")
        for p in plan["verschwunden"][:10]:
            print(f"    {p}")

    if not push:
        print("not pushing (pass --push)")
        return 0

    _git(staging(), "fetch", "--quiet", "origin")
    head_after = _git(staging(), "rev-parse", "origin/main").strip()
    if head_after != head_before:
        print(f"\nSTOP -- the public head moved from {head_before[:12]} to "
              f"{head_after[:12]} while this ran. The comparison above "
              "describes a tree nobody is pushing to. Re-run.")
        return 1
    print("push it from the staging checkout with an ordinary `git push`; "
          "this tool does not push for you and has no force path")
    print("then record the new base: `python3 tools/export_sync.py record "
          "--export-commit origin/main`. Skipping that leaves the base behind "
          "our own push, and the next run reports our files as changed on both "
          "sides -- a true statement about the recorded base and a false alarm "
          "about the work.")
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
    full_ = _git(staging(), "rev-parse", export_commit).strip()
    ihre = blob_digests(staging(), full_)
    meine = {p: digest((HOH / p).read_bytes())
             for p in include_paths(HOH) if (HOH / p).is_file()}

    missing_ones = sorted(p for p in ihre
                    if p not in meine and p not in REPORTS)
    if missing_ones:
        print(f"refusing to record: {full_[:12]} carries {len(missing_ones)} path(s) "
              "this tree does not have. Recording it as integrated would tell "
              "the next export they are accounted for:")
        for p in missing_ones[:10]:
            print(f"    {p}")
        return 1

    ours_ = sorted(p for p in meine
                    if meine[p] != ihre.get(p) and p not in REPORTS)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({
        "what": "the public export commit whose content has been integrated "
                "into this tree. Base for the three-way comparison in "
                "tools/export_sync.py: digests are the public side at that "
                "commit, so anything differing here afterwards is ours.",
        "public_repo": "SKZL-AI/veriharness",
        "synced_export_commit": full_,
        # Not `subprocess.run(["date", ...])`: there is no `date -u` on
        # Windows, and shelling out for a timestamp the standard library
        # produces is a portability cost with nothing bought for it.
        "recorded_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ours_at_record_time": ours_,
        "path_digests": dict(sorted(ihre.items())),
    }, indent=2) + "\n")
    print(f"recorded {len(ihre)} path digest(s) from {full_[:12]}")
    if ours_:
        print(f"{len(ours_)} path(s) already differ here and will be treated "
              "as ours from now on:")
        for p in ours_[:10]:
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
            state = load_state()
            _git(staging(), "fetch", "--quiet", "origin")
            head = _git(staging(), "rev-parse", "origin/main").strip()
            plan = comparisons(state, head)
            report_(plan, head)
            return 1 if name_obstacles(plan, would_have_written=False) else 0
        return export(args.push)
    except Abort as exc:
        print(f"STOP -- {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
