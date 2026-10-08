#!/usr/bin/env python3
"""Make "never delete, always version" something a machine enforces.

The house rule has always said it: if something has to go, it is renamed with a
version and a UTC stamp, or moved into `.archiv/`, never removed. On
2026-09-23 the orchestrator ran `rm -rf` on a parked duplicate of a run's
evidence, having judged the copy redundant. No unique bytes were lost -- the
original 497MB run directory was intact -- but that is luck reported as a
result, and the rule has no exception for "I checked first".

The denylist that guards every agent on this machine did not stop it, and was
right not to by its own design rule: *block only irreversible/catastrophic
commands; recoverable local-destructive commands stay allowed.* A duplicate of
evidence under a dated run directory looks exactly like `node_modules` to a
regular expression over the command line. So the gap is not that the guard
failed; it is that "project- and evidence-owned" was never expressed anywhere a
guard could read.

This file expresses it, and gives the refusal somewhere to send the caller:

* `classify PATH` -- is this path project- or evidence-owned, and why;
* `check -- ARGV...` -- would this command destroy such a path? Refuse, and name
  the operation that does the same job safely;
* `supersede PATH` -- the versioned rename, `name.v<N>.<UTC>`, beside itself;
* `archive PATH` -- the move into `<parent>/.archiv/name.v<N>.<UTC>`;
* `dedupe CANDIDATE ORIGINAL` -- when a copy really is redundant: prove the two
  are byte-identical, write the manifest record that says so, and archive the
  copy. The logical evidence record survives; the bytes are not deleted. Whether
  the archived copy is ever purged is the Captain's decision, not the
  orchestrator's, which is exactly the judgement that went wrong.

Nothing here removes a file. There is no code path that does, and
`tests/test_preserve.py` greps this module for one.

Usage:
    preserve.py classify PATH
    preserve.py check -- rm -rf /some/path
    preserve.py supersede PATH
    preserve.py archive PATH
    preserve.py dedupe CANDIDATE ORIGINAL
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

#: Directory names that mean "this holds the programme's own records". A path at
#: or under one of these is project-owned: the register, the ledger, the
#: benchmarks, the dogfood evidence, the plan's own history.
PROJECT_DIRS = frozenset({
    "dogfood", "program", "docs", "history", "paper", "ops", ".archiv",
})

#: File names that mean "this *is* evidence". Their presence makes the
#: containing directory evidence-owned even when its name says nothing -- which
#: is the case that got missed, because a run directory is called by its run id.
EVIDENCE_MARKERS = frozenset({
    "state.json", "evidence.json", "checks.json", "steps.jsonl",
    "EXPORT_MANIFEST.json", "SUCCESSION.json", "ATTRIBUTION.json",
})

#: Directory names that are themselves evidence containers.
EVIDENCE_DIRS = frozenset({"receipts", "run-evidence", "steps", "answers", "_arenas"})

#: Paths whose recursive removal the house rules forbid outright, independent of
#: what they contain. Kept here as a second line rather than trusted to the
#: command-line denylist, because this tool is also called from code.
FUNDAMENTAL = ("/", "/home", "/usr", "/etc", "/var", "/opt", "/boot", "/root")

#: Programs that destroy, and the flag shapes that make them recursive.
DESTRUCTIVE = frozenset({"rm", "shred", "dd", "rmdir", "unlink", "truncate"})
RECURSIVE_FLAG = re.compile(r"^-[a-zA-Z]*[rR]")

#: What a run directory looks like when it is named after its run: a dated
#: stamp. The incident's path had exactly this shape.
DATED = re.compile(r"\b(19|20)\d{6}[tT]?\d{0,6}[zZ]?\b")


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


#: The filesystem answers these three questions, and it is allowed to refuse.
#: `classify` is called about paths the caller may not be able to read -- `/`
#: holds `lost+found`, a run directory may be owned by another user, a mount may
#: be gone -- and a guard that raised there would fail *open*: the caller sees a
#: traceback, decides the tool is broken, and removes the path by hand. So an
#: unreadable path answers "not visibly evidence" and the reasons that do not
#: depend on the filesystem, above all the name and the fundamental-path list,
#: still stand on their own.
def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _children(path: Path, limit: int = 200) -> list[Path]:
    """At most `limit` entries of `path`, or none when it cannot be read.

    Bounded because a runs root can hold thousands of directories and this runs
    in front of every destructive command; unsorted iteration is taken from the
    directory as the filesystem offers it, then sorted, so the cap costs one
    pass rather than a full listing.
    """
    found: list[Path] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                found.append(Path(entry.path))
                if len(found) >= limit:
                    break
    except OSError:
        return []
    return sorted(found)


def classify(path: Path) -> list[str]:
    """Why this path is project- or evidence-owned, or `[]` when it is neither.

    Reasons rather than a boolean, because the refusal has to be able to say
    *which* rule it is applying -- a guard whose message is "no" teaches nobody
    and gets worked around.

    The path does not have to exist: a caller asking about a path it is about to
    destroy is the normal case, and a path that has already gone is the case
    where the answer matters most.
    """
    reasons: list[str] = []
    try:
        full_ = path.expanduser().resolve()
    except OSError:
        full_ = path
    parts = full_.parts

    if str(full_) in FUNDAMENTAL or full_ == Path(full_.anchor):
        reasons.append(f"{full_} is a fundamental path; the house rules forbid "
                       "recursive removal there outright")
    home = os.environ.get("HOME")
    if home and str(full_) == str(Path(home).resolve()):
        reasons.append(f"{full_} is the home directory itself")

    named = [p for p in parts if p in PROJECT_DIRS]
    if named:
        reasons.append(f"it lies under {named[0]}/, which holds the programme's "
                       "own records")
    evidence_named = [p for p in parts if p in EVIDENCE_DIRS]
    if evidence_named:
        reasons.append(f"it lies under {evidence_named[0]}/, which is an "
                       "evidence container")

    if _is_dir(full_):
        for marker in sorted(EVIDENCE_MARKERS):
            if _is_file(full_ / marker):
                reasons.append(f"it contains {marker}, so it is a record of a "
                               "run and not a working copy")
                break
        else:
            # One level down: a runs root holds run directories, and the markers
            # live inside those. Removing the root removes every record in it.
            for child in _children(full_):
                if _is_dir(child) and any(_is_file(child / m)
                                          for m in EVIDENCE_MARKERS):
                    reasons.append(f"it contains the run directory {child.name}/, "
                                   "whose records would go with it")
                    break
    elif full_.name in EVIDENCE_MARKERS:
        reasons.append(f"{full_.name} is itself an evidence record")

    if DATED.search(full_.name) and (full_.is_dir() or not full_.exists()):
        reasons.append(f"its name carries a date stamp ({full_.name}), the shape "
                       "a run directory has -- the exact shape of the path the "
                       "2026-09-23 rm -rf destroyed")

    return reasons


def command_problems(argv: list[str]) -> list[str]:
    """Why this command may not run as given, or `[]`.

    Deliberately about the *argument vector*, not a shell string: a guard that
    parses shell text loses to quoting, and that lesson was paid for once
    already in the P1-16 acceptance record. A caller that has only a string
    should refuse to guess.
    """
    if not argv:
        return []
    program = Path(argv[0]).name
    if program == "find" and "-delete" in argv:
        targets = [a for a in argv[1:] if not a.startswith("-")]
    elif program == "git" and len(argv) > 1 and argv[1] == "clean":
        targets = [a for a in argv[2:] if not a.startswith("-")] or ["."]
    elif program in DESTRUCTIVE:
        targets = [a for a in argv[1:] if not a.startswith("-")]
    else:
        return []

    recursive = any(RECURSIVE_FLAG.match(a) for a in argv[1:])
    problems: list[str] = []
    for target in targets:
        reasons = classify(Path(target))
        if not reasons:
            continue
        head = (f"{program} would destroy {target}, which is protected: "
                + "; ".join(reasons))
        problems.append(
            head + ". Do it by preserving instead: "
            f"`preserve.py supersede {target}` renames it beside itself, "
            f"`preserve.py archive {target}` moves it into .archiv/, and "
            "`preserve.py dedupe <copy> <original>` is for the case this "
            "command is usually reached for -- a copy believed redundant. That "
            "belief is what the rule exists to stop acting on")
    if recursive and not problems and program == "rm":
        # Not protected, so not refused; the note is still worth making because
        # "recoverable" is a judgement and this is where it gets made.
        return []
    return problems


def _next_version(target: Path) -> int:
    """The next free `v<N>` for `target`, counting what is already beside it."""
    highest = 0
    parent = target.parent
    if parent.is_dir():
        pattern = re.compile(rf"^{re.escape(target.name)}\.v(\d+)\.")
        for sibling in parent.iterdir():
            found = pattern.match(sibling.name)
            if found:
                highest = max(highest, int(found.group(1)))
    return highest + 1


def supersede(target: Path) -> Path:
    """Rename `target` to `name.v<N>.<UTC>` beside itself and return the new path.

    A rename, not a copy: the bytes are never read, so this costs nothing on a
    497MB run directory, which matters because cost is what makes an orchestrator
    reach for removal in the first place.
    """
    if not target.exists():
        raise FileNotFoundError(f"nothing to supersede at {target}")
    destination = target.with_name(f"{target.name}.v{_next_version(target)}.{_utc()}")
    if destination.exists():                      # pragma: no cover - same second
        raise FileExistsError(f"{destination} already exists")
    os.rename(target, destination)
    return destination


def archive(target: Path) -> Path:
    """Move `target` into `<parent>/.archiv/name.v<N>.<UTC>` and return it there.

    The archive convention from the house rules. `.archiv/` is itself classified
    as project-owned, so what lands here is protected from the next cleanup by
    the same rule that sent it here.
    """
    if not target.exists():
        raise FileNotFoundError(f"nothing to archive at {target}")
    box = target.parent / ".archiv"
    box.mkdir(exist_ok=True)
    destination = box / f"{target.name}.v{_next_version(box / target.name)}.{_utc()}"
    if destination.exists():                      # pragma: no cover - same second
        raise FileExistsError(f"{destination} already exists")
    shutil.move(str(target), str(destination))
    return destination


def content_digest(path: Path) -> str:
    """A digest over a file's bytes, or over a tree's names and bytes.

    Names are part of it: two trees holding the same bytes under different names
    are not the same evidence.
    """
    h = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    for item in sorted(path.rglob("*")):
        h.update(str(item.relative_to(path)).encode("utf-8"))
        h.update(b"\0")
        if item.is_file():
            with item.open("rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    h.update(block)
        h.update(b"\n")
    return h.hexdigest()


def dedupe(candidate: Path, original: Path) -> dict:
    """Archive `candidate` *only* if it is byte-identical to `original`.

    This is the operation the 2026-09-23 removal was reaching for, done so that
    the claim it rests on is proven rather than believed. If the two differ, the
    candidate is not a duplicate and nothing happens -- that difference is
    precisely the unique data a removal would have destroyed.

    The manifest record is written next to the archived copy so the logical
    evidence survives the physical move: what it was, what it was identical to,
    which digest, when, and where it went.
    """
    if not candidate.exists():
        raise FileNotFoundError(f"no candidate at {candidate}")
    if not original.exists():
        raise FileNotFoundError(f"no original at {original}")
    left, right = content_digest(candidate), content_digest(original)
    if left != right:
        return {"status": "REFUSED", "candidate": str(candidate),
                "original": str(original), "candidate_digest": left,
                "original_digest": right,
                "why": "the two are not byte-identical, so the candidate holds "
                       "something the original does not. That difference is the "
                       "unique data a removal would have destroyed"}
    moved = archive(candidate)
    record = {"status": "ARCHIVED", "candidate": str(candidate),
              "original": str(original), "digest": left,
              "archived_to": str(moved), "at": _utc(),
              "why": "byte-identical to the original, proven by digest; the copy "
                     "is archived, not removed, and this record keeps the "
                     "logical evidence"}
    (moved.parent / f"{moved.name}.identical.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


#: The files that together *are* this project's destructive-operation policy.
#: Both are in this repository and versioned with it, which is the point: the
#: programme's correctness claims may not rest on machine-global configuration
#: that other repositories and sessions share and can change underneath it.
POLICY_FILES = ("tools/preserve.py", "tools/preserve_hook.py",
                "policy/role_approval.default.json")


def policy_digest(root: Path) -> dict:
    """The digest of the policy actually present in `root`.

    Recorded in a self-hosted run's evidence so the claim "this run ran under the
    no-delete invariant" names a specific policy rather than a good intention. A
    run whose recorded digest does not match the tree it is being integrated into
    did not run under the policy being claimed, and `--expect` says so by exit
    code rather than in prose.
    """
    per_file: dict[str, str] = {}
    for relative in POLICY_FILES:
        spot = root / relative
        per_file[relative] = content_digest(spot) if spot.is_file() else "ABSENT"
    combined = hashlib.sha256()
    for relative in POLICY_FILES:
        combined.update(relative.encode("utf-8"))
        combined.update(b"\0")
        combined.update(per_file[relative].encode("utf-8"))
        combined.update(b"\n")
    return {"root": str(root), "files": per_file,
            "policy_digest": combined.hexdigest(),
            "complete": all(v != "ABSENT" for v in per_file.values())}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="preserve instead of delete")
    sub = ap.add_subparsers(dest="command", required=True)

    g = sub.add_parser("digest", help="the digest of the policy in a tree")
    g.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    g.add_argument("--expect", default="",
                   help="refuse unless the policy digest is exactly this")

    c = sub.add_parser("classify")
    c.add_argument("path", type=Path)

    k = sub.add_parser("check")
    k.add_argument("argv", nargs=argparse.REMAINDER,
                   help="the command, after --")

    s = sub.add_parser("supersede")
    s.add_argument("path", type=Path)

    a = sub.add_parser("archive")
    a.add_argument("path", type=Path)

    d = sub.add_parser("dedupe")
    d.add_argument("candidate", type=Path)
    d.add_argument("original", type=Path)

    args = ap.parse_args(argv)

    if args.command == "digest":
        found = policy_digest(args.root)
        print(json.dumps(found, indent=2))
        if not found["complete"]:
            print("the policy is incomplete in this tree, so a run here cannot "
                  "claim the no-delete invariant", file=sys.stderr)
            return 2
        if args.expect and args.expect != found["policy_digest"]:
            print(f"the policy in {args.root} is {found['policy_digest'][:12]}, "
                  f"not the expected {args.expect[:12]}. A run under a different "
                  "policy than the one being claimed proves nothing about the "
                  "policy being claimed", file=sys.stderr)
            return 2
        return 0

    if args.command == "classify":
        reasons = classify(args.path)
        if reasons:
            print(f"{args.path}: protected")
            for reason in reasons:
                print(f"  {reason}")
            return 0
        print(f"{args.path}: not project- or evidence-owned by these rules")
        return 0

    if args.command == "check":
        rest = [a for a in args.argv if a != "--"]
        problems = command_problems(rest)
        if problems:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 2
        print("no protected path in this command")
        return 0

    if args.command == "supersede":
        print(supersede(args.path))
        return 0

    if args.command == "archive":
        print(archive(args.path))
        return 0

    record = dedupe(args.candidate, args.original)
    print(json.dumps(record, indent=2))
    return 0 if record["status"] == "ARCHIVED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
