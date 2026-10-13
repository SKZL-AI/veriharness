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

The board field is a *projection*, not a copy: it leaves out the readiness row
that measures this capsule, and names what it left out, because a row whose
state writing the capsule changes cannot also be a fact the capsule records
(O209). So `verdict_excluding_self` answers "is anything else open", which is
the question a successor has, and never claims to be the board's own word.

What the capsule deliberately does **not** carry: narrative, plans for the
work, or advice. Those belong in the plan the capsule points at. This file
answers one question -- is the state described here the state that exists --
and answering it well is worth more than answering more questions vaguely.

A capsule describes the commit it was written at; the commit that carries it,
and the closure record, the board and the attribution entry that follow, are
records. HEAD behind by commits that touched only record paths
(`RECORD_PATHS`, never executed code) is therefore VERIFIED, with every
derived field still re-derived; a commit that moved anything else -- a source
file renamed into a record directory included -- is drift. The working tree
is judged the same way: dirt outside the record paths. A tree that is not a
git repository of its own, or a git command that fails, is an environment gap
(an archive copy under another repository would otherwise be judged by that
repository, and a failed command would otherwise read as "nothing changed").

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

#: Readiness rows that measure this capsule, and are therefore left out of the
#: board projection recorded here (O209). A row that reports whether the
#: capsule matches the tree cannot also be a fact the capsule records: writing
#: the capsule is what turns it green, which changes the record, which turns it
#: red again. `tests/test_succession.py` pins that this name is a row the board
#: actually has, so a rename cannot quietly restore the oscillation.
SELF_ROWS = frozenset({"succession"})

#: Verdicts a single field can carry. Three, not two: a field that cannot be
#: checked from here is not a failure of the capsule and not a pass either.
OK, DRIFT, GAP = "VERIFIED", "DRIFTED", "ENVIRONMENT_GAP"

#: Paths a records entry may touch *after* the commit a capsule describes without
#: the ground under the handover having moved: the ledgers, the board and the
#: other generated documents, the preserved reviews and pins under `dogfood/`
#: (the capsule itself among them), the public figures, and the artefacts the
#: programme regenerates from the tree. A closure is several commits -- the
#: sealing commit, the capsule, the closure record, the attribution entry that
#: follows every commit -- and the old rule (exactly one commit, touching only
#: the capsule) read DRIFTED at the second of them, so a successor learned to
#: read past red. What this list does **not** carry is anything a capsule field
#: or a matrix row is derived from as *ground*: `src/`, `tools/`, `tests/`,
#: `ops/`, `policy/`, `pyproject.toml`, the register, the plan inventory and
#: the amendments. A path not listed here is drift (fail closed), and so is a
#: path git had to quote. Directories end with `/`; `*` stands for one file
#: name in that directory (never a path below it); everything else is exact.
RECORD_PATHS = (
    "dogfood/",
    "docs/",
    "paper/",
    "DOGFOOD_*",
    "README.md",
    "CLAIMS.md",
    "CLAIMS.json",
    "EXPORT_MANIFEST.json",
    "CHANGELOG*",
    "RELEASE_NOTES*",
    "CITATION.cff",
    ".github/releases/*",
    "program/v3_3/VERIHARNESS_*",
    "program/v3_3/PROGRAMME_DISPOSITIONS.md",
)
#: What a file under a record directory (or a `*` rule) must look like to be a
#: record: a **data** file. Suffixes are case-folded; a dotfile's leading dot
#: is not a suffix; a versioned copy (`name.v1.<UTC>`, `state.json.v2026-...Z-001`,
#: a rotated `.1`) is judged by the chain it carries. Anything executable or
#: configuring anywhere in that chain is ground (`dogfood/` carries benchmark
#: references and instruments, and `pytest.ini` under it steers a test run),
#: and so is an extensionless file (a script needs no suffix) and any suffix
#: not listed. Fail closed: the closure's records entries write `.md`, `.json`,
#: `.jsonl`, `.log`, `.txt`, `.diff` and versioned copies, all listed; an
#: unlisted type is drift, which is noticed, not unsafe.
DATA_SUFFIXES = frozenset(
    "md txt json jsonl log out err diff patch csv tsv yaml yml lock index bundle body "
    "code provider-blocked html svg png jpg jpeg gif pdf tex bib cff receipt ack capsule "
    "stdout stderr console".split())
EXEC_SUFFIXES = frozenset(
    "py pyw pyc pyi pth sh bash zsh ksh fish pl rb js mjs cjs ts php lua ps1 bat cmd exe "
    "so dll service timer ini cfg toml mk nix".split())
#: Extensionless names that are data wherever they sit.
DATA_NAMES = frozenset({".lock", "README", "LICENSE", "MANIFEST", "NOTICE"})
_VERSION_TAIL = re.compile(
    r"v?\d{8}t\d{6}z|v?\d{4}-\d{2}-\d{2}t\d{2}[-:]\d{2}[-:]\d{2}z(-\d+)?|v\d+|\d+")
#: Never a record by name: the one hand-written document under the
#: generated-artefact prefix.
NOT_RECORD = ("program/v3_3/VERIHARNESS_STATE_OWNERSHIP_ADR.md",)

#: What `internal_commit` must be: one full, lower-case commit id. `write`
#: records `rev-parse HEAD`, which is exactly that. Anything git would also
#: resolve -- `HEAD`, `HEAD~0`, a branch, a tag, an abbreviated id -- is
#: refused before git is asked, because a symbolic name resolves to whatever
#: HEAD is *now*: a capsule naming `HEAD` would read VERIFIED after every
#: source commit for ever, and the capsule is a record file anyone may commit.
_COMMIT_ID = re.compile(r"[0-9a-f]{40}")

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


def _git_rc(*args: str, repo: Path | None = None) -> tuple[int, str]:
    """Exit code and raw stdout (bytes kept through `surrogateescape`, so a path
    git prints with `-z` survives and can be refused). Every call a verdict
    depends on reads the code: an empty answer from a failed command used to
    read as "nothing changed" and "clean", which is the opposite of what a
    failure means."""
    r = subprocess.run(["git", "-C", str(repo or HOH), *args], capture_output=True)
    return r.returncode, r.stdout.decode("utf-8", "surrogateescape")


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

def _source(rel: str, at_commit: str | None) -> str | None:
    """The text of a source file a capsule field is derived from: the working
    tree's, or -- when `at_commit` is given -- the blob committed there
    (`git show <commit>:<rel>`), so that a verdict about a commit is derived
    from that commit and not from whatever the working tree holds at the
    moment. `None` when the file is not there (or git could not answer: a
    failure is never an empty source)."""
    if at_commit is None:
        path = HOH / rel
        return path.read_text(encoding="utf-8") if path.is_file() else None
    rc, text = _git_rc("show", f"{at_commit}:{rel}")
    return text if rc == 0 else None


def _board(at_commit: str | None = None) -> dict:
    """The readiness board's own verdict and the commit it was measured at."""
    text = _source("docs/READINESS.md", at_commit)
    if text is None:
        return {}
    return _board_from(text)


def _board_from(text: str) -> dict:
    """The same, from the board's text, so the parsing can be tested."""
    m = re.search(r"Measured at `([0-9a-f]+)`", text)
    v = re.search(r"TECHNICALLY_STABLE_READY = (\w+)", text)
    open_rows = re.search(r"^Open, and each one blocking: (.+)$", text, re.M)
    rows = ([s.strip().rstrip(".").strip() for s in open_rows.group(1).split(",")]
            if open_rows else [])
    rows_other_than_this = [r for r in rows if r not in SELF_ROWS]
    # Fail-closed on a board this cannot read. Deriving "yes" from an empty
    # list meant a board whose open-rows line was missing, truncated or
    # reworded read as green -- strictly weaker than the old behaviour, which
    # copied the word `no` and stayed red. A reviewer demonstrated it. So
    # "yes" is derived only when the board already says yes, or when its word
    # was readable, rows were actually parsed, and the only open one measures
    # this capsule. A board that lost the verdict line while keeping the one
    # below it -- a hand edit -- would otherwise still read as green.
    said = v.group(1) if v else None
    if said == "yes" or (said and rows and not rows_other_than_this):
        verdict = "yes"
    else:
        verdict = said
    return {
        "reference_commit": m.group(1) if m else None,
        #: Which rows this projection leaves out, in the capsule itself, so a
        #: reader does not have to find this code to know that the verdict is
        #: not `TECHNICALLY_STABLE_READY`.
        "excludes": sorted(SELF_ROWS),
        # Derived from the rows that are not about this capsule, for the same
        # reason (O209): the board's own verdict word flips with the
        # `succession` row, so recording it verbatim made the capsule and the
        # board unable to agree -- `no` while the row was red, `yes` once
        # writing the capsule turned it green, and red again at the next
        # render. This says what a successor needs: is anything *else* open.
        # Not the board's word verbatim: that would drift again the moment it
        # flips, which is the defect this whole field exists to stop.
        "verdict_excluding_self": verdict,
        # Every row except the one that measures this capsule. O209: with
        # `succession` included, the pair had no fixpoint. Writing the capsule
        # is what turns that row green, which removes it from the board's open
        # list, which changes the very field the capsule just recorded -- so
        # the next render turned the row red again, and the one after that
        # green. Four closure cycles oscillated between those two states, and
        # `regenerate` cannot reach a fixpoint across them either.
        #
        # Dropping it removes a self-reference, not a check. That a capsule
        # describes this tree is what `verify` answers field by field, and the
        # board's own row reports that answer; the capsule restating it proved
        # nothing and made the two unable to agree. Every other row stays, so a
        # capsule written while `tests` or `export_manifest` was red still says
        # so.
        "open_rows_excluding_self": rows_other_than_this,
    }


def _sync(at_commit: str | None = None) -> dict:
    text = _source("dogfood/export-sync/EXPORT_SYNC_STATE.json", at_commit)
    if text is None:
        return {}
    d = json.loads(text)
    return {"public_commit": d.get("synced_export_commit"),
            "paths": len(d.get("path_digests") or {})}


def _release(at_commit: str | None = None) -> dict:
    """Invariants a successor must never move: the tag and the published bytes."""
    text = _source(".github/releases/v0.1.0.json", at_commit)
    if text is None:
        return {}
    d = json.loads(text)
    return {"source_tag": d.get("source_tag"),
            "tag_object": d.get("tag_object"),
            "source_commit": d.get("source_commit"),
            "sha256": d.get("sha256") or {}}


def _open_findings(at_commit: str | None = None) -> list[dict]:
    """Findings the ledger itself marks as carrying an open, tracked item.

    Read out of the ledger rather than maintained beside it, so the capsule
    cannot drift from the record it summarises. A finding whose section says
    it is tracked and names a priority is open until the same section carries
    a closing line, which means closing a todo in the ledger closes it here
    too -- by appending the closure, never by removing what opened it.
    """
    text = _source("DOGFOOD_LEDGER.md", at_commit)
    if text is None:
        return []
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
    own, why = _own_repository()
    rc_head, head = _git_rc("rev-parse", "HEAD")
    dirt = _dirt_outside_records() if own else None
    if rc_head == 0 and not _COMMIT_ID.fullmatch(head.strip()):
        rc_head, why = 1, (why or f"git named HEAD as {head.strip()[:60]!r}, not a commit id")
    if not own or rc_head != 0 or dirt is None:
        # A capsule that could record a commit or a cleanness it did not
        # measure is the document this tool exists to replace.
        print("refusing to write a capsule: " + (why or
              f"git could not answer (rev-parse exit {rc_head}, status "
              f"{'failed' if dirt is None else 'ok'})"), file=sys.stderr)
        return 2
    capsule = {
        "what": (
            "A phase boundary a successor session verifies rather than reads. "
            "Every field here is re-derivable with `python3 tools/succession.py "
            "verify`, which reports per field whether the ground has moved."
        ),
        "written_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "internal_commit": head.strip(),
        # True when nothing outside the record paths (`RECORD_PATHS`) is
        # modified, staged, deleted or untracked; the capsule's own file and
        # the documents a records entry regenerates do not count.
        "internal_tree_clean": not dirt,
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
    board = capsule["board"]
    print(f"  board              {board.get('verdict_excluding_self')} at "
          f"{str(board.get('reference_commit'))[:12]}, excluding "
          f"{', '.join(board.get('excludes') or []) or 'nothing'}")
    print(f"  public commit      {str(capsule['sync'].get('public_commit'))[:12]}")
    print(f"  open findings      {len(capsule['open_findings'])}")
    print(f"  plan files pinned  {len(capsule['plan'].get('files') or {})}")
    return 0


def _is_record(path: str) -> bool:
    """Is `path` (as `git diff --name-only` prints it) one of `RECORD_PATHS`?

    Exact, case-sensitive, on the repository-relative spelling git prints. A
    quoted name (git quotes anything it cannot print plainly), an absolute
    path, an empty one or one with `..` in it is never a record.
    """
    if (not path or path.startswith('"') or path.startswith("/")
            or ".." in path.split("/") or not path.isprintable()
            or any(0xD800 <= ord(c) <= 0xDFFF for c in path)):
        return False
    if path in NOT_RECORD:
        return False
    for rule in RECORD_PATHS:
        if rule.endswith("/"):
            if path.startswith(rule):
                return _data_file(path)
        elif rule.endswith("*"):
            stem = rule[:-1]
            if path.startswith(stem) and len(path) > len(stem) \
                    and "/" not in path[len(stem):]:
                return _data_file(path)
        elif path == rule:
            return True
    return False


def _data_file(path: str) -> bool:
    """Is the file name a data file (`DATA_SUFFIXES`, `DATA_NAMES`), with nothing
    executable or configuring in its suffix chain (`EXEC_SUFFIXES`)?"""
    name = path.rsplit("/", 1)[-1]
    if name in DATA_NAMES:
        return True
    parts = name.lower().lstrip(".").split(".")
    if len(parts) < 2 or any(part in EXEC_SUFFIXES for part in parts[1:]):
        return False
    tail = parts[-1]
    return tail in DATA_SUFFIXES or _VERSION_TAIL.fullmatch(tail) is not None


def _records_only_since(commit: str, head: str) -> tuple[bool | None, str, list[str]]:
    """Does `head` differ from `commit` only in record paths?

    `(True, detail, window)` when `commit` is an ancestor of `head` and every
    path of the tree diff `commit..head` is a record path (an identical tree
    counts); `(False, why, [])` otherwise, naming the paths that are not
    records, or that `commit` is not on this line at all; `(None, why, [])`
    when git could not answer (a non-zero exit is never "nothing changed").
    `window` is the list of commits between the two, newest first, for the
    board's re-measurement. The diff is taken with `--no-renames`: a source
    file moved into a record directory is a deletion of the source path and
    must be named as one.

    The old rule -- exactly one commit of distance, touching only the capsule
    -- is the special case of this one in which the window is the capsule's
    own commit. Widened to records because a closure is several commits and a
    rule that reads DRIFTED at the second of them teaches a successor to read
    past red; not widened further, because `src/` moving is exactly what a
    successor must be told.
    """
    rc_base, base = _git_rc("merge-base", commit, head)
    rc_full, full = _git_rc("rev-parse", f"{commit}^{{commit}}")
    if rc_base != 0 or rc_full != 0:
        return None, f"git could not relate {commit[:12]} to HEAD (merge-base exit {rc_base})", []
    if base.strip() != full.strip():
        return False, f"{commit[:12]} is not an ancestor of HEAD, so this tree is on another line", []
    rc_list, listed = _git_rc("rev-list", f"{commit}..{head}")
    rc_diff, out = _git_rc("diff", "--name-only", "--no-renames", "-z", f"{commit}..{head}")
    if rc_list != 0 or rc_diff != 0:
        return None, (f"git could not list what changed since {commit[:12]} "
                      f"(rev-list exit {rc_list}, diff exit {rc_diff})"), []
    window = listed.split()
    changed = [p for p in out.split("\0") if p]
    offenders = [p for p in changed if not _is_record(p)]
    if len(offenders) == 1:
        return False, f"{offenders[0]} is not a record path", []
    if offenders:
        more = f" and {len(offenders) - 3} more" if len(offenders) > 3 else ""
        return False, (f"{len(offenders)} paths are not record paths: "
                       + ", ".join(offenders[:3]) + more), []
    return True, (f"{len(window)} records-only commit(s) touched "
                  f"{len(changed)} record path(s) and nothing else"), window


def _own_repository() -> tuple[bool, str]:
    """Is `HOH` the top of a git repository of its own? `git -C` walks up to the
    first `.git` above a tree, so an archive copy under somebody else's
    repository would be judged by that repository's HEAD and status."""
    rc, top = _git_rc("rev-parse", "--show-toplevel")
    top = top.strip()
    try:
        own = rc == 0 and bool(top) and Path(top).resolve() == HOH.resolve()
    except OSError:
        own = False
    if own:
        return True, ""
    return False, ("not a git repository of its own, so nothing about its history or "
                   f"its working tree is claimed: {HOH}"
                   + (f" (git answers for {top})" if rc == 0 and top else
                      f" (git exit {rc})"))


def _dirt_outside_records() -> list[str] | None:
    """Working-tree changes on paths that are not records: modified, staged,
    deleted or untracked, each file on its own (`--untracked-files=all`, so a
    module in a new directory under `dogfood/` is seen), renames not folded.
    `None` when git could not answer -- never an empty list for a failure.

    Judged on non-record paths only (O-regenerate): the records entry that
    follows a capsule regenerates the DAG, the board and the other generated
    documents between two passes, and a tree that is dirty only in those is
    not a tree whose ground has moved. A modified source file, a new module
    under `dogfood/`, a changed register still count.
    """
    rc, out = _git_rc("status", "--porcelain", "--no-renames", "--untracked-files=all", "-z")
    if rc != 0:
        return None
    paths = [entry[3:] for entry in out.split("\0") if len(entry) > 3]
    return [p for p in paths if not _is_record(p)]


def _measured_inside(reference: object, window: list[str]) -> bool:
    """Does the board's reference commit name one of the records-only commits
    between the capsule's commit and HEAD? A short sha is resolved by git; a
    value that is not a commit of this repository, or is one outside the
    window, is not."""
    if not window or not isinstance(reference, str) or not reference.strip():
        return False
    rc, full = _git_rc("rev-parse", "--verify", "--quiet", f"{reference.strip()}^{{commit}}")
    return rc == 0 and bool(full.strip()) and full.strip() in window


def check(capsule: dict, plan_root: Path | None, *,
          at_commit: str | None = None) -> list[tuple[str, str, str]]:
    """Re-derive every field. Returns (field, verdict, detail) per row.

    With `at_commit` (the gate passes `"HEAD"`), the board, the sync state, the
    release invariants and the ledger are read from the blobs committed there,
    not from the working tree: a gate's verdict is a statement about a commit,
    and a working tree in which an old board has been restored, uncommitted,
    must not turn a drifted capsule green. `runs/` (untracked) and the plan
    (outside the repository) are read where they are; the tree row still
    reports dirt outside the record paths.
    """
    rows: list[tuple[str, str, str]] = []
    #: Commits between the capsule's commit and HEAD when every one of them is
    #: records-only; the board may have been re-measured at one of them.
    window: list[str] = []

    # The tree must be a repository of its own. `git -C` walks up to the first
    # `.git` above it, so an archive copy under somebody else's repository
    # would be judged by that repository's HEAD and status -- and read green.
    # That is an environment gap, named, never a pass; and the tree row is not
    # derived either, for the same reason.
    own_repo, why = _own_repository()
    if not own_repo:
        rows.append(("internal_commit", GAP, why))
        rows.append(("tree", GAP, why))
    # The commit the capsule was written at must still be in this history,
    # or the capsule describes a line this tree is not on.
    commit = capsule.get("internal_commit")
    rc_head, head = _git_rc("rev-parse", "HEAD")
    head = head.strip()
    resolved = ""
    if own_repo and isinstance(commit, str) and _COMMIT_ID.fullmatch(commit):
        # `rev-parse --verify` prints the sha on success and nothing on
        # failure, so its output distinguishes the two. `cat-file -e` prints
        # nothing either way, and an earlier draft of this line used it --
        # a check whose success and failure look identical decides nothing.
        resolved = _git("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    if not own_repo:
        pass
    elif not commit:
        rows.append(("internal_commit", DRIFT, "the capsule names no commit"))
    elif not isinstance(commit, str) or not _COMMIT_ID.fullmatch(commit):
        # Fail closed before git is asked: `HEAD`, a branch, a tag or an
        # abbreviated id would resolve, and a name that resolves to the
        # current tip every time can never drift (integration reviews of the
        # first candidate, B1/F1).
        shown = repr(commit)[:60]
        rows.append(("internal_commit", DRIFT,
                     f"the capsule names {shown} where a full 40-hex commit id is "
                     "required; a symbolic or abbreviated name would follow HEAD "
                     "and never drift"))
    elif rc_head != 0 or not head:
        rows.append(("internal_commit", GAP,
                     f"git could not name HEAD (exit {rc_head}); nothing is claimed"))
    elif resolved == "":
        rows.append(("internal_commit", GAP,
                     f"{commit[:12]} is not in this repository, so this is "
                     "not the history the capsule was written in"))
    elif resolved != commit:
        # A 40-hex id that peels to another commit is an annotated tag's
        # object id, not a commit id: the tag can be moved.
        rows.append(("internal_commit", DRIFT,
                     f"{commit[:12]} is not a commit id: it peels to "
                     f"{resolved[:12]}; a tag object can be re-pointed"))
    else:
        if head == commit:
            rows.append(("internal_commit", OK, f"HEAD is {commit[:12]}"))
        else:
            # The unavoidable self-reference: a capsule cannot name the commit
            # that carries it, because that commit does not exist until the
            # capsule is written -- and a closure does not stop there: the
            # closure record, the board, the attribution entry each follow in
            # a commit of their own. Behind by commits that touched nothing
            # but record paths (`RECORD_PATHS`) is therefore the correct
            # state; every derived field is still re-derived below, so a
            # record that changed what the capsule says is caught by its own
            # row. Anything outside those paths is drift like any other,
            # because a successor checking out a tree needs green to mean
            # green rather than "green except for the usual".
            records_only, detail, window = _records_only_since(commit, head)
            if records_only is None:
                rows.append(("internal_commit", GAP, detail))
            elif records_only:
                rows.append(("internal_commit", OK,
                             f"{commit[:12]}, followed to {head[:12]} by {detail}"))
            else:
                n = _git("rev-list", "--count", f"{commit}..HEAD")
                rows.append(("internal_commit", DRIFT,
                             f"HEAD has moved {n or '?'} commit(s) past "
                             f"{commit[:12]}; the capsule is behind: {detail}"))

    # O210. Recorded since the first capsule and never re-derived, which a
    # reviewer demonstrated the expensive way: `verify` reported all seven
    # fields VERIFIED in a tree with a modified source file and three untracked
    # new modules. No other field covers working-tree content -- `internal_commit`
    # compares commits, not bytes -- so a successor could trust a green capsule
    # while reading a tree that differs from every commit in it.
    #
    # Compared, not required: a capsule written during a closure records a dirty
    # tree (its own file is uncommitted), and that is honest. What must not
    # happen is a capsule recording `true` verifying where the tree has since
    # moved.
    recorded_clean = capsule.get("internal_tree_clean")
    dirt = _dirt_outside_records() if own_repo else None
    if not own_repo:
        pass
    elif dirt is None:
        rows.append(("tree", GAP, "git status failed; nothing about the working tree is claimed"))
    elif recorded_clean is None:
        rows.append(("tree", GAP, "the capsule does not say whether the tree was clean"))
    elif bool(recorded_clean) == (not dirt):
        rows.append(("tree", OK, "clean outside the record paths" if not dirt else
                     "dirty outside the record paths, as it was when the capsule was written"))
    else:
        rows.append(("tree", DRIFT,
                     f"the capsule recorded a {'clean' if recorded_clean else 'dirty'} "
                     f"tree and this one is {'clean' if not dirt else 'dirty'} outside "
                     "the record paths"
                     + (f": {', '.join(dirt[:4])}" if dirt else "")))

    for field, now, name in (("board", _board(at_commit), "board"),
                             ("sync", _sync(at_commit), "sync"),
                             ("release_invariants", _release(at_commit), "release"),
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
            # The board is regenerated by the records entry that follows the
            # capsule, and names the commit it was measured at. A board
            # re-measured at one of the records-only commits behind HEAD
            # says the same thing about a tree whose ground has not moved;
            # what it says -- the verdict and the open rows -- is still
            # compared. A reference commit from anywhere else is drift.
            if (field == "board" and differing == ["reference_commit"]
                    and _measured_inside(now.get("reference_commit"), window)):
                rows.append((name, OK,
                             "re-derives identically; the board was re-measured at "
                             f"{now['reference_commit']}, a records-only commit "
                             "behind HEAD"))
                continue
            rows.append((name, DRIFT,
                         f"{len(differing)} field(s) differ: "
                         + ", ".join(sorted(differing)[:4])))

    then_findings = {e["finding"] for e in capsule.get("open_findings") or []}
    now_findings = {e["finding"] for e in _open_findings(at_commit)}
    if then_findings == now_findings:
        rows.append(("open_findings", OK,
                     f"{len(now_findings)} tracked item(s), unchanged"))
    else:
        rows.append(("open_findings", DRIFT,
                     f"closed since: {', '.join(sorted(then_findings - now_findings)) or 'none'}; "
                     f"new: {', '.join(sorted(now_findings - then_findings)) or 'none'}"))

    plan_then = capsule.get("plan") if isinstance(capsule.get("plan"), dict) else {}
    # The root the capsule recorded is a convenience for a successor at the
    # command line. The gate (`at_commit` given) does not take it: the gate
    # reads a committed capsule, and a capsule may name any directory whose
    # files it has digested itself. There the plan comes from the environment
    # or from `--plan-root`, or the row is a gap -- which is not a pass. The
    # recorded root is read only when it is used, and only as a non-empty
    # string: a capsule may carry anything there, and `Path(5)` is a traceback.
    recorded_raw = plan_then.get("root")
    recorded = (Path(recorded_raw) if isinstance(recorded_raw, str) and recorded_raw.strip()
                else None)
    root = plan_root if at_commit is not None else (plan_root or recorded)
    plan_now = _plan(root)
    if root is None or plan_now.get("unreachable"):
        rows.append(("plan", GAP,
                     f"the plan is not reachable from here; set ${PLAN_ENV} "
                     "or pass --plan-root. Nothing about it is claimed"
                     + ("; the root recorded in the capsule is not used when the "
                        "verdict is about a commit" if at_commit is not None and recorded_raw
                        else "")))
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
