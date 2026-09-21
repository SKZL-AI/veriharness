#!/usr/bin/env python3
"""A phase boundary a successor session can **verify** instead of believe.

A long programme outlives the session that starts it. Today the handover is a
document: somebody writes down where things stand and the next session reads
it. That fails the way every unverified claim in this project has failed --
the prose is true when written and nobody notices when it stops being.

So a succession capsule is not a summary. Every field in it is something this
tool can re-derive from the tree, and `verify` says, field by field, whether
the capsule still describes what is here. A successor's first act is to run
it: green means the ground under the handover has not moved, red names what
moved, and an environment gap names what cannot be checked from where it is
standing -- which is its own answer and never a pass.

What the capsule deliberately does **not** carry: narrative, plans for the
work, or advice. Those belong in the plan the capsule points at. This file
answers one question -- is the state described here the state that exists --
and answering it well is worth more than answering more questions vaguely.

Usage:
    python3 tools/succession.py write [--plan-root DIR] [--out PATH]
    python3 tools/succession.py verify [--capsule PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent
CAPSULE = HOH / "dogfood/succession/SUCCESSION.json"

#: Where the programme plan lives. Not a constant in this file: the plan is on
#: the operator's machine, and an absolute path to it here would be one
#: machine's layout baked into a published tool (O175). Read from the
#: environment or given on the command line; absent, the plan fields become an
#: environment gap rather than an assumption.
PLAN_ENV = "VERIHARNESS_PLAN_ROOT"

#: Verdicts a single field can carry. Three, not two: a field that cannot be
#: checked from here is not a failure of the capsule and not a pass either.
OK, DRIFT, GAP = "VERIFIED", "DRIFTED", "ENVIRONMENT_GAP"

#: A ledger section declares an open item in one of these two ways. The German
#: form is what every entry up to O181 was written in; the English one is what
#: entries are written in from O182 onward. Both are read, because a rule that
#: stopped recognising the older form would silently close 13 open items --
#: correction by addition, not by rewriting the record.
_TRACKED = re.compile(r"getracktes Todo|tracked todo", re.I)

#: The priority a tracked item carries. Only a word from this set is read as
#: one: the ledger writes both `Priorität: mittel` and `Getracktes Todo,
#: Prioritaet mittel`, so the separator cannot be required -- and without the
#: set, `Priorität dieser Sache` would be read as a priority of "dieser".
_LEVELS = "hoch|mittel|niedrig|high|medium|low"
_PRIORITY = re.compile(
    rf"(?:Priorit(?:ä|ae)t|Priority)\b[:,]?\s*({_LEVELS})\b", re.I)

#: How a tracked item is closed. The ledger is never edited to remove a todo
#: -- corrections are additions here -- so closure is a line appended to the
#: section rather than the disappearance of the one that opened it. Without
#: this the open list could only ever grow, and a successor would inherit
#: items that were finished months earlier.
_CLOSED = re.compile(r"(?m)^\*\*(?:Closed|Geschlossen)\b[^*]*\*\*")


def _git(*args: str, repo: Path | None = None) -> str:
    r = subprocess.run(["git", "-C", str(repo or HOH), *args],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def digest(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _plan_root(given: Path | None) -> Path | None:
    if given is not None:
        return given
    from_env = os.environ.get(PLAN_ENV)
    return Path(from_env) if from_env else None


# --------------------------------------------------------------------------
# Deriving the state. Each of these is a *measurement*, and `check` runs the
# same function again rather than a second implementation of it -- two
# implementations of one rule is how a checker comes to disagree with itself.
# --------------------------------------------------------------------------

def _board() -> dict:
    """The readiness board's own verdict and the commit it was measured at."""
    path = HOH / "docs/READINESS.md"
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    m = re.search(r"Measured at `([0-9a-f]+)`", text)
    v = re.search(r"TECHNICALLY_STABLE_READY = (\w+)", text)
    open_rows = re.search(r"^Open, and each one blocking: (.+)$", text, re.M)
    return {
        "reference_commit": m.group(1) if m else None,
        "verdict": v.group(1) if v else None,
        "open_rows": ([s.strip().rstrip(".") for s in open_rows.group(1).split(",")]
                      if open_rows else []),
    }


def _sync() -> dict:
    path = HOH / "dogfood/export-sync/EXPORT_SYNC_STATE.json"
    if not path.is_file():
        return {}
    d = json.loads(path.read_text())
    return {"public_commit": d.get("synced_export_commit"),
            "paths": len(d.get("path_digests") or {})}


def _release() -> dict:
    """Invariants a successor must never move: the tag and the published bytes."""
    path = HOH / ".github/releases/v0.1.0.json"
    if not path.is_file():
        return {}
    d = json.loads(path.read_text())
    return {"source_tag": d.get("source_tag"),
            "tag_object": d.get("tag_object"),
            "source_commit": d.get("source_commit"),
            "sha256": d.get("sha256") or {}}


def _open_findings() -> list[dict]:
    """Findings the ledger itself marks as carrying an open, tracked item.

    Read out of the ledger rather than maintained beside it, so the capsule
    cannot drift from the record it summarises. A finding whose section says
    it is tracked and names a priority is open until the same section carries
    a closing line, which means closing a todo in the ledger closes it here
    too -- by appending the closure, never by removing what opened it.
    """
    path = HOH / "DOGFOOD_LEDGER.md"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^(#{2,4} (O\d+)[^\n]*)$", text)
    found: dict[str, dict] = {}
    for i in range(1, len(parts) - 2, 3):
        heading, number, body = parts[i], parts[i + 1], parts[i + 2]
        if not _TRACKED.search(body) or _CLOSED.search(body):
            continue
        prio = _PRIORITY.search(body)
        # A finding may carry several sections; the first that declares a
        # tracked item wins, and a later one does not overwrite it.
        found.setdefault(number, {
            "finding": number,
            "priority": prio.group(1).lower() if prio else "unstated",
            "heading": heading.lstrip("# ").strip()[:120],
        })
    return sorted(found.values(), key=lambda e: int(e["finding"][1:]))


def _runs() -> dict:
    """How many runs are in which state -- the successor inherits these."""
    root = HOH / "runs"
    if not root.is_dir():
        return {}
    counts: dict[str, int] = {}
    for f in sorted(root.glob("*/state.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            counts["unreadable"] = counts.get("unreadable", 0) + 1
            continue
        key = f"{d.get('stage')}/{d.get('condition')}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _plan(root: Path | None) -> dict:
    """The plan bundle, pinned by digest so a successor reads the same one."""
    if root is None:
        return {"root": None, "files": {}}
    if not root.is_dir():
        return {"root": str(root), "files": {}, "unreachable": True}
    files = {}
    for f in sorted(root.rglob("*")):
        if f.is_file() and f.suffix.lower() in {".md", ".json"}:
            try:
                files[str(f.relative_to(root))] = digest(f.read_bytes())
            except OSError:
                continue
    return {"root": str(root), "files": files}


def write_capsule(plan_root: Path | None, out: Path) -> int:
    capsule = {
        "what": (
            "A phase boundary a successor session verifies rather than reads. "
            "Every field here is re-derivable with `python3 tools/succession.py "
            "verify`, which reports per field whether the ground has moved."
        ),
        "written_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "internal_commit": _git("rev-parse", "HEAD"),
        "internal_tree_clean": _git("status", "--porcelain") == "",
        "board": _board(),
        "sync": _sync(),
        "release_invariants": _release(),
        "open_findings": _open_findings(),
        "runs": _runs(),
        "plan": _plan(plan_root),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(capsule, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out.relative_to(HOH) if out.is_relative_to(HOH) else out}")
    print(f"  internal commit    {capsule['internal_commit'][:12]}")
    print(f"  board verdict      {capsule['board'].get('verdict')} "
          f"at {str(capsule['board'].get('reference_commit'))[:12]}")
    print(f"  public commit      {str(capsule['sync'].get('public_commit'))[:12]}")
    print(f"  open findings      {len(capsule['open_findings'])}")
    print(f"  plan files pinned  {len(capsule['plan'].get('files') or {})}")
    return 0


def _only_the_capsule_moved(commit: str, head: str) -> bool:
    """True when `head` is the child of `commit` and touches only the capsule.

    Deliberately narrow: exactly one commit of distance, and its entire diff
    is the capsule file. Widening either half would turn "the capsule is
    current" into "the capsule is roughly current".
    """
    if _git("rev-list", "--count", f"{commit}..{head}") != "1":
        return False
    changed = _git("diff", "--name-only", f"{commit}..{head}").splitlines()
    try:
        capsule_rel = str(CAPSULE.relative_to(HOH))
    except ValueError:
        return False
    return changed == [capsule_rel]


def check(capsule: dict, plan_root: Path | None) -> list[tuple[str, str, str]]:
    """Re-derive every field. Returns (field, verdict, detail) per row."""
    rows: list[tuple[str, str, str]] = []

    # The commit the capsule was written at must still be in this history,
    # or the capsule describes a line this tree is not on.
    commit = capsule.get("internal_commit") or ""
    if not commit:
        rows.append(("internal_commit", DRIFT, "the capsule names no commit"))
    elif _git("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}") == "":
        # `rev-parse --verify` prints the sha on success and nothing on
        # failure, so its output distinguishes the two. `cat-file -e` prints
        # nothing either way, and an earlier draft of this line used it --
        # a check whose success and failure look identical decides nothing.
        rows.append(("internal_commit", GAP,
                     f"{commit[:12]} is not in this repository, so this is "
                     "not the history the capsule was written in"))
    else:
        head = _git("rev-parse", "HEAD")
        if head == commit:
            rows.append(("internal_commit", OK, f"HEAD is {commit[:12]}"))
        elif _only_the_capsule_moved(commit, head):
            # The unavoidable self-reference: a capsule cannot name the commit
            # that carries it, because that commit does not exist until the
            # capsule is written. One commit behind is therefore the correct
            # state *if* that commit changed nothing but the capsule -- and
            # only then. Anything else in the same commit and this is drift
            # like any other, because a successor checking out a tree needs
            # green to mean green rather than "green except for the usual".
            rows.append(("internal_commit", OK,
                         f"{commit[:12]}, recorded by {head[:12]}, which "
                         "changed nothing but this capsule"))
        else:
            n = _git("rev-list", "--count", f"{commit}..HEAD")
            rows.append(("internal_commit", DRIFT,
                         f"HEAD has moved {n or '?'} commit(s) past "
                         f"{commit[:12]}; the capsule is behind"))

    for field, now, name in (("board", _board(), "board"),
                             ("sync", _sync(), "sync"),
                             ("release_invariants", _release(), "release"),
                             ("runs", _runs(), "runs")):
        then = capsule.get(field)
        if not now:
            rows.append((name, GAP,
                         "cannot be re-derived here: the source this rests "
                         "on is not in this tree"))
        elif then == now:
            rows.append((name, OK, "re-derives identically"))
        else:
            differing = [k for k in set(then or {}) | set(now)
                         if (then or {}).get(k) != now.get(k)]
            rows.append((name, DRIFT,
                         f"{len(differing)} field(s) differ: "
                         + ", ".join(sorted(differing)[:4])))

    then_findings = {e["finding"] for e in capsule.get("open_findings") or []}
    now_findings = {e["finding"] for e in _open_findings()}
    if then_findings == now_findings:
        rows.append(("open_findings", OK,
                     f"{len(now_findings)} tracked item(s), unchanged"))
    else:
        rows.append(("open_findings", DRIFT,
                     f"closed since: {', '.join(sorted(then_findings - now_findings)) or 'none'}; "
                     f"new: {', '.join(sorted(now_findings - then_findings)) or 'none'}"))

    plan_then = capsule.get("plan") or {}
    root = plan_root or (Path(plan_then["root"])
                         if plan_then.get("root") else None)
    plan_now = _plan(root)
    if root is None or plan_now.get("unreachable"):
        rows.append(("plan", GAP,
                     f"the plan is not reachable from here; set ${PLAN_ENV} "
                     "or pass --plan-root. Nothing about it is claimed"))
    elif plan_then.get("files") == plan_now.get("files"):
        rows.append(("plan", OK,
                     f"{len(plan_now['files'])} file(s) unchanged"))
    else:
        a, b = plan_then.get("files") or {}, plan_now.get("files") or {}
        moved = sorted(set(a) ^ set(b)) + sorted(
            k for k in set(a) & set(b) if a[k] != b[k])
        rows.append(("plan", DRIFT,
                     f"{len(moved)} file(s) moved: " + ", ".join(moved[:3])))
    return rows


def verify_capsule(path: Path, plan_root: Path | None) -> int:
    if not path.is_file():
        print(f"no succession capsule at {path}", file=sys.stderr)
        return 2
    try:
        capsule = json.loads(path.read_text())
    except ValueError as exc:
        print(f"the capsule at {path} is unreadable: {exc}", file=sys.stderr)
        return 2

    rows = check(capsule, plan_root)
    width = max(len(f) for f, _, _ in rows)
    for field, verdict, detail in rows:
        print(f"  {verdict:<17} {field:<{width}}  {detail}")
    drifted = [f for f, u, _ in rows if u == DRIFT]
    gaps = [f for f, u, _ in rows if u == GAP]
    print()
    if drifted:
        print(f"DRIFTED: {len(drifted)} field(s) no longer describe this tree: "
              + ", ".join(drifted))
        print("Read those before continuing. A capsule that has drifted is not "
              "wrong about the past; it is stale about the present.")
        return 1
    if gaps:
        print(f"VERIFIED where checkable; {len(gaps)} field(s) could not be "
              "checked from here: " + ", ".join(gaps))
        print("Neither a pass nor a failure. Say which, rather than rounding.")
        return 3
    print("VERIFIED: every field re-derives from this tree.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan-root", type=Path, default=None,
                    help=f"the programme plan directory; overrides ${PLAN_ENV}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write")
    w.add_argument("--out", type=Path, default=CAPSULE)
    v = sub.add_parser("verify")
    v.add_argument("--capsule", type=Path, default=CAPSULE)
    args = ap.parse_args(argv)
    root = _plan_root(args.plan_root)
    if args.cmd == "write":
        return write_capsule(root, args.out)
    return verify_capsule(args.capsule, root)


if __name__ == "__main__":
    raise SystemExit(main())
