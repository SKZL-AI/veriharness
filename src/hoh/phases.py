"""Phase closure as a product primitive: `hoh phase close | status | reopen` (amendment A7).

The operator's ruling of 2026-10-10: a phase is closed at the commit of its closure record, and
later changes create regression obligations, not a reopened phase. Until this module that was a
behaviour of VeriHarness's own build tools (`tools/build_plan.py`, `tools/regression_watch.py`):
a customer's project did not get it, and a project whose gate reads open again at every merge
never converges -- its verification chokes in a chain that has no end. Three acts, for any
repository:

* `close` -- the operator hands in a **closure entry**: the phase, the closing commit X, the
  sealed checklist snapshot at X and the evidence files at X, each by repository path and
  sha256. The product reads those blobs at X and refuses unless every one matches; only then does
  it write a **receipt** (`<gate dir>/CLOSURE.json`, created, never replaced).
* `status` -- reads the receipt as committed at HEAD and re-derives it every time (the blobs at X,
  X an ancestor of HEAD, no committed reopen decision). A verified closure reads CLOSED at X; what
  the live snapshot would block on, the evidence that changed since X, the last re-run and the
  open reopen requests are the *watch* beside it, never a blocker. A record that does not verify
  is ignored and the reason is printed (fail closed: the phase is not closed by it).
* `reopen` -- records an explicit decision naming the closure and a reopen request. Nothing else
  reopens a phase: no function of this module is called by a loop, and a committed decision cannot
  be undone by deleting its file (the directory's history is read).

What it does not do. It commits nothing (a closure is read from HEAD, so committing the receipt
or the decision is the operator's act), runs no regression re-run (the watch records
`<gate dir>/watch/*.json` and the requests `REOPEN-REQUEST-*.json` are written by whatever runs the
obligation -- for VeriHarness `tools/regression_watch.py`, whose layout this module reads), and
ends a reopen only by a new closure at a new commit. The seal of a snapshot catches an edit, not a
forgery by someone who recomputes it (`checklist.Snapshot`), and the receipt records that
(`derived_from_evidence` false): the entry is an act of the records step, reviewed by a pair.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .archive_bundle import _QUIET, _git_env
from .checklist import BLOCKING, Closure, GateVerdict, Snapshot, phase_gate
from .durable_io import DurableWriteNotSynced, durable_create

ENTRY_SCHEMA = "hoh-phase-closure-entry/1"
RECEIPT_SCHEMA = "hoh-phase-closure/1"
DECISION_SCHEMA = "hoh-phase-reopen/1"
#: Where a phase's records live in the repository unless `--gate-dir` says otherwise.
GATE_ROOT = ".hoh/phases"
CLOSURE_FILE = "CLOSURE.json"
REOPEN_DIR = "reopen"
WATCH_DIR = "watch"
REQUEST_PREFIX = "REOPEN-REQUEST-"
#: Every list the product reads or accepts is bounded by one number: evidence files and watch
#: paths in an entry, reopen records ever committed, watch records and reopen requests read.
#: More is refused (never cut), because a directory that grows without bound must not turn a
#: gate into a long wait or hide a record behind a cut.
MAX_FILES = 64
#: A blob, an entry or a request is not read beyond this; one byte more is refused.
MAX_BYTES = 4 * 1024 * 1024
#: Every git call is bounded; a call that does not return is a refusal, not a wait.
GIT_TIMEOUT = 60
MAX_REASON = 2000
_PHASE_WORD = re.compile(r"[Pp]0*([0-9]{1,2})")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT_PREFIX = re.compile(r"[0-9a-fA-F]{7,40}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_PART = re.compile(r"[A-Za-z0-9._@+=,~-]{1,120}")
_REQUEST = re.compile(r"REOPEN-REQUEST-[A-Za-z0-9._-]{1,100}\.json")
_STAMP = re.compile(r"[0-9]{8}T[0-9]{6}Z")
_REGULAR = ("100644", "100755")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class Refused(Exception):
    """The input does not verify, or cannot be judged: nothing is written, the message is why."""


def _safe(text: str) -> str:
    """Text for a terminal. What a repository supplies (a file name, a reason, a failed
    requirement) is shown, not obeyed: control characters become `?` and bytes that are not UTF-8
    become `\\x..` escapes."""
    return _CONTROL.sub("?", text.encode("utf-8", "backslashreplace").decode("utf-8"))


@dataclass(frozen=True)
class Outcome:
    code: int
    out: str = ""
    err: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "out", _safe(self.out))
        object.__setattr__(self, "err", _safe(self.err))


@dataclass(frozen=True)
class PhaseStatus:
    phase: str
    state: str                      # CLOSED | REOPENED | OPEN
    reading: str                    # the one line
    note: str = ""                  # why it is not closed ("" when closed)
    verdict: GateVerdict | None = None
    facts: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {"phase": self.phase, "state": self.state, "reading": self.reading,
                "note": self.note, "closed_at": self.verdict.closed_at if self.verdict else None,
                "closure_ref": self.verdict.closure_ref if self.verdict else None,
                "watch": list(self.verdict.watch) if self.verdict else [], **self.facts}


# --------------------------------------------------------------------------- #
# small readers: names, objects, files, git
# --------------------------------------------------------------------------- #

def phase_of(value: object) -> str | None:
    """A phase as a record may spell it (`P2`, `p2`, ` P02 `) in its canonical spelling, or None."""
    if not isinstance(value, str):
        return None
    m = _PHASE_WORD.fullmatch(value.strip())
    return f"P{int(m.group(1))}" if m else None


def _phase(value: str) -> str:
    phase = phase_of(value)
    if phase is None:
        raise Refused(f"{str(value)[:40]!r} is not a phase name (P0 .. P99)")
    return phase


def _repo_path(value: object, what: str) -> str:
    """A repository-relative path in the one shape the product reads: `/`-separated parts of
    letters, digits and `._@+=,~-`; no empty, `.`, `..` or `.git` part, no leading `-`."""
    if not isinstance(value, str) or not value or len(value) > 240 or value.startswith("-"):
        raise Refused(f"{what}: not a repository path in the shape the product reads")
    parts = value.split("/")
    if any(not _PART.fullmatch(p) or p in (".", "..") or p.lower() == ".git" for p in parts):
        raise Refused(f"{what}: {value[:80]!r} is not a repository path in the shape the "
                      "product reads (letters, digits and ._@+=,~- in each /-separated part)")
    return value


def _gate_dir(phase: str, given: str | None) -> str:
    return _repo_path(given if given is not None else f"{GATE_ROOT}/{phase.lower()}",
                      "--gate-dir")


def _no_repeats(pairs: list[tuple[str, Any]]) -> dict:
    """An object whose key appears twice says two things; the reader would see only the last."""
    found = dict(pairs)
    if len(found) != len(pairs):
        raise ValueError("a key is repeated")
    return found


def _object(data: bytes, what: str) -> dict:
    try:
        body = json.loads(data.decode("utf-8"), object_pairs_hook=_no_repeats)
    except (ValueError, RecursionError):
        raise Refused(f"{what} is not UTF-8 JSON, or repeats a key") from None
    if not isinstance(body, dict):
        raise Refused(f"{what} is not a JSON object")
    return body


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular(path: str, what: str) -> bytes:
    """The bytes of a regular file the caller named, opened without waiting or following a
    link at the end, kind checked on the descriptor, bounded."""
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_NOCTTY | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError) as exc:
        raise Refused(f"{what} {str(path)[:120]!r} cannot be opened "
                      f"({getattr(exc, 'strerror', None) or type(exc).__name__})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f"{what} {str(path)[:120]!r} is not a regular file")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(1 << 16, MAX_BYTES + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_BYTES:
                raise Refused(f"{what} is larger than {MAX_BYTES} bytes: not read")
    finally:
        os.close(fd)


def _git(repo: Path, *args: str) -> tuple[int, bytes]:
    try:
        done = subprocess.run(["git", *_QUIET, "-C", str(repo), *args], capture_output=True,
                              env=_git_env(), stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused(f"git could not be run ({type(exc).__name__})") from None
    return done.returncode, done.stdout


def _commit(repo: Path, rev: str, what: str) -> str:
    rc, out = _git(repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}")
    full = out.decode("ascii", "replace").strip()
    if rc != 0 or not _FULL_SHA.fullmatch(full):
        raise Refused(f"{what} {rev[:12]} is not a commit of this repository")
    return full


def _ancestor(repo: Path, older: str, newer: str, what: str) -> None:
    """`older` is `newer` or one of its ancestors; exit 1 is "no", any other exit is "git could
    not say" -- never a pass."""
    rc, _ = _git(repo, "merge-base", "--is-ancestor", older, newer)
    if rc == 1:
        raise Refused(f"{what}: {older[:12]} is not an ancestor of {newer[:12]}")
    if rc != 0:
        raise Refused(f"{what}: git could not relate {older[:12]} to {newer[:12]} (exit {rc})")


def _repository(repo: str) -> tuple[Path, str]:
    """`(top, HEAD)` of a repository of its own: `git` answers for whatever repository lies above
    a directory, so a copy of a tree without its own `.git` would be judged by another history."""
    try:
        root = Path(repo).resolve(strict=True)
    except (OSError, ValueError):
        raise Refused(f"--repo {str(repo)[:120]!r} is not a directory") from None
    rc, top = _git(root, "rev-parse", "--show-toplevel")
    try:
        own = rc == 0 and Path(os.fsdecode(top).strip()).resolve() == root
    except OSError:
        own = False
    if not own:
        raise Refused(f"--repo {str(repo)[:120]!r} is not the top of a git repository of its own, "
                      "so nothing about its history is claimed")
    return root, _commit(root, "HEAD", "HEAD")


def _blob(repo: Path, rev: str, path: str, what: str) -> bytes:
    """The bytes of the regular file `path` as committed at `rev` (a link, a submodule or a
    directory at that name is refused), bounded by `MAX_BYTES`."""
    rc, out = _git(repo, "ls-tree", "-z", rev, "--", path)
    rows = [r for r in out.split(b"\0") if r]
    if rc != 0 or len(rows) != 1:
        raise Refused(f"{what} {path} is not a file of {rev[:12]}")
    meta, _, name = rows[0].partition(b"\t")
    fields = meta.decode("ascii", "replace").split(" ")
    if len(fields) != 3 or name != os.fsencode(path):
        raise Refused(f"{what} {path} is not a file of {rev[:12]}")
    mode, kind, oid = fields
    if kind != "blob" or mode not in _REGULAR:
        raise Refused(f"{what} {path} at {rev[:12]} is not a regular file (mode {mode})")
    rc, size = _git(repo, "cat-file", "-s", oid)
    if rc != 0 or not size.strip().isdigit():
        raise Refused(f"{what} {path} at {rev[:12]} cannot be sized")
    if int(size) > MAX_BYTES:
        raise Refused(f"{what} {path} at {rev[:12]} is larger than {MAX_BYTES} bytes: not read")
    rc, data = _git(repo, "cat-file", "blob", oid)
    if rc != 0:
        raise Refused(f"{what} {path} at {rev[:12]} cannot be read (git exit {rc})")
    return data


def _names(repo: Path, rev: str, directory: str) -> list[str]:
    """Every path below `directory` at `rev` (recursive), committed state only."""
    rc, out = _git(repo, "ls-tree", "-r", "-z", "--name-only", rev, "--", directory + "/")
    if rc != 0:
        raise Refused(f"git could not list {directory} at {rev[:12]} (exit {rc})")
    return sorted(n for n in os.fsdecode(out).split("\0") if n)


# --------------------------------------------------------------------------- #
# the claims: what an entry and a receipt say
# --------------------------------------------------------------------------- #

def _exact(body: object, keys: set[str], what: str) -> dict:
    if not isinstance(body, dict) or set(body) != keys:
        raise Refused(f"{what} does not hold exactly {sorted(keys)}")
    return body


def _hex(value: object, pattern: re.Pattern, what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise Refused(f"{what} is missing or not in its shape")
    return value


def _claims(body: dict, schema: str, phase: str, extra: set[str] = frozenset()) -> dict:
    """The claims of an entry or a receipt, in their one shape; anything else is refused."""
    keys = {"schema", "phase", "ref", "closed_at_commit", "snapshot", "evidence", "watch"} | extra
    if body.get("schema") != schema:
        raise Refused(f"the record carries schema {str(body.get('schema'))[:40]!r}, not {schema}")
    body = _exact(body, keys, "the record")
    if phase_of(body["phase"]) != phase:
        raise Refused(f"the record is about {str(body['phase'])[:20]!r}, not {phase}")
    snap = _exact(body["snapshot"], {"path", "file_sha256", "seal"}, "`snapshot`")
    evidence = body["evidence"]
    if not isinstance(evidence, list) or len(evidence) > MAX_FILES:
        raise Refused(f"`evidence` is not a list of at most {MAX_FILES} files")
    rows = [_exact(e, {"path", "file_sha256"}, "an `evidence` row") for e in evidence]
    paths = [_repo_path(snap["path"], "`snapshot.path`")]
    paths += [_repo_path(r["path"], "an `evidence` path") for r in rows]
    if len(set(paths)) != len(paths):
        raise Refused("the snapshot and the evidence files are not all different files")
    _hex(snap["file_sha256"], _SHA256, "`snapshot.file_sha256`")
    _hex(snap["seal"], _SHA256, "`snapshot.seal`")
    for r in rows:
        _hex(r["file_sha256"], _SHA256, "an `evidence` digest")
    watch = body["watch"]
    if watch is not None:
        watch = _exact(watch, {"evidence_commit", "paths"}, "`watch`")
        _hex(watch["evidence_commit"], _FULL_SHA, "`watch.evidence_commit`")
        wp = watch["paths"]
        if not isinstance(wp, list) or not 1 <= len(wp) <= MAX_FILES:
            raise Refused(f"`watch.paths` is not a list of 1 to {MAX_FILES} paths")
        for p in wp:
            _repo_path(p, "a `watch` path")
        if len(set(wp)) != len(wp):
            raise Refused("`watch.paths` names a path twice")
    _hex(body["ref"], _REF, "`ref`")
    _hex(body["closed_at_commit"], _FULL_SHA, "`closed_at_commit`")
    return body


def _verify_at_x(repo: Path, head: str, phase: str, claims: dict) -> tuple[Snapshot, int]:
    """Re-derive what the claims say from the blobs at X. Returns the snapshot at X and the
    number of blocking items of the phase it closes. Raises `Refused` on any mismatch."""
    x = _commit(repo, claims["closed_at_commit"], "the closing commit")
    if x != claims["closed_at_commit"]:
        raise Refused("`closed_at_commit` is not one full commit id")
    _ancestor(repo, x, head, "the closing commit is not in this tree's history")
    snap_c = claims["snapshot"]
    blob = _blob(repo, x, snap_c["path"], "the checklist snapshot")
    if _digest(blob) != snap_c["file_sha256"]:
        raise Refused(f"the checklist snapshot at {x[:12]} has sha256 {_digest(blob)[:12]}..., "
                      f"the record says {snap_c['file_sha256'][:12]}...")
    snapshot = _snapshot(_object(blob, "the checklist snapshot"))
    if snapshot.seal != snap_c["seal"] or not snapshot.is_sealed():
        raise Refused(f"the checklist snapshot at {x[:12]} is not sealed "
                      f"{snap_c['seal'][:12]}... by its own content")
    verdict = phase_gate(snapshot, phase)
    if not verdict.closed:
        shown = "; ".join(f"{b.item_id}: {b.why}" for b in verdict.blockers[:3])
        raise Refused(f"phase {phase} is not closed by the snapshot at {x[:12]}: {shown[:300]}"
                      + (f" and {len(verdict.blockers) - 3} more" if len(verdict.blockers) > 3
                         else ""))
    for row in claims["evidence"]:
        data = _blob(repo, x, row["path"], "an evidence file")
        if _digest(data) != row["file_sha256"]:
            raise Refused(f"the evidence file {row['path']} at {x[:12]} has sha256 "
                          f"{_digest(data)[:12]}..., the record says {row['file_sha256'][:12]}...")
    watch = claims["watch"]
    if watch is not None:
        ev = _commit(repo, watch["evidence_commit"], "the watch's evidence commit")
        if ev != watch["evidence_commit"]:
            raise Refused("`watch.evidence_commit` is not one full commit id")
        _ancestor(repo, ev, x, "the watch's evidence commit does not lead to the closing commit")
    blocking = sum(1 for i in snapshot.items if i.phase == phase and i.criticality in BLOCKING)
    return snapshot, blocking


def _snapshot(body: dict) -> Snapshot:
    """A checklist snapshot out of a JSON object that holds at least `items`, `generation` and
    `seal` (a file that carries more, like the programme's own, is read for those)."""
    keys = {k: body[k] for k in ("items", "generation", "seal", "transitions") if k in body}
    try:
        return Snapshot.model_validate(keys)
    except ValidationError as exc:
        raise Refused(f"the checklist snapshot is not a snapshot "
                      f"({exc.error_count()} error(s): {str(exc.errors()[0]['msg'])[:80]})") from None


# --------------------------------------------------------------------------- #
# reopen decisions (read from history) and the watch
# --------------------------------------------------------------------------- #

def names_closure(body: dict, phase: str, closed_at: str) -> bool:
    """Does a record name this closure -- the phase as a person writes it (`p2`, `P02`) and the
    closing commit as the full id or an abbreviation of at least seven hex digits, in either
    case?"""
    commit = body.get("closed_at_commit")
    if phase_of(body.get("phase")) != phase or not isinstance(commit, str):
        return False
    prefix = commit.strip()
    return bool(_COMMIT_PREFIX.fullmatch(prefix)) and closed_at.startswith(prefix.lower())


def _reading(found: dict, name: str, phase: str, x: str) -> str | None:
    """What one decision file says about THIS closure: None when it is about another phase or
    another closure, else `REOPEN` or `KEEP_CLOSED`. A file that names this phase and cannot be
    read refuses (cannot tell)."""
    named = phase_of(found.get("phase"))
    if named is None or not isinstance(found.get("closed_at_commit"), str):
        raise Refused(f"{name} does not name a phase and a closing commit")
    if named != phase:
        return None
    prefix = found["closed_at_commit"].strip()
    if not _COMMIT_PREFIX.fullmatch(prefix):
        raise Refused(f"{name} names {phase} and a closing commit that cannot be read "
                      f"({prefix[:12]!r}): whether it reopens this closure cannot be told")
    if not x.startswith(prefix.lower()):
        return None
    if found.get("decision") not in ("REOPEN", "KEEP_CLOSED"):
        raise Refused(f"{name} names this closure and its decision is neither REOPEN nor "
                      "KEEP_CLOSED")
    return found["decision"]


def _history_names(repo: Path, head: str, directory: str) -> list[str]:
    rc, out = _git(repo, "log", "--full-history", "-m", "--name-only", "--format=", "--no-renames",
                   "-z", head, "--", directory + "/")
    if rc != 0:
        raise Refused(f"git could not read the history of {directory} (exit {rc})")
    return sorted({n for n in os.fsdecode(out).split("\0") if n.strip()})


def reopened_by(repo: Path, head: str, phase: str, gate: str, x: str) -> tuple[str, str] | None:
    """`(file, reason)` of a committed REOPEN decision naming the closure at X, or None. A
    decision is judged by the version that was COMMITTED as one -- every commit that added the
    file is read -- so editing it in place or removing it later does not undo it. Anything that
    cannot be read, or too many records, refuses: whether the closure was reopened cannot be
    told."""
    directory = f"{gate}/{REOPEN_DIR}"
    present = _names(repo, head, directory)
    ever = _history_names(repo, head, directory)
    if len(present) > MAX_FILES or len(ever) > MAX_FILES:
        raise Refused(f"{directory} holds or held {max(len(present), len(ever))} files, more "
                      f"than {MAX_FILES}; whether one of them reopens this closure is not read")
    found: tuple[str, str] | None = None
    for name in sorted(set(present) | set(ever)):
        if not name.endswith(".json"):
            if name in present:
                raise Refused(f"{name} is in {directory} and is not a JSON record; whether it "
                              "reopens this closure cannot be told")
            continue
        rc, out = _git(repo, "log", "--full-history", "-m", "--diff-filter=A", "--format=%H",
                       "--no-renames", head, "--", f":(literal){name}")
        added = list(dict.fromkeys(out.decode("ascii", "replace").split()))
        if rc != 0 or any(not _FULL_SHA.fullmatch(c) for c in added):
            raise Refused(f"git could not read the history of {name} (exit {rc})")
        revs = list(reversed(added)) + ([head] if name in present else [])
        for rev in revs:
            body = _object(_blob(repo, rev, name, name), name)
            if _reading(body, name, phase, x) == "REOPEN" and found is None:
                reason = body.get("reason")
                found = (name, reason[:200] if isinstance(reason, str) else "")
    return found


def _relevant_tree(repo: Path, commit: str, paths: Sequence[str]) -> str | None:
    """One digest of the git object ids of `paths` at `commit` (a path the commit does not have
    contributes `<absent>`): two commits with the same value have the same relevant content."""
    lines = []
    for path in paths:
        rc, out = _git(repo, "rev-parse", "--verify", "--quiet", f"{commit}:{path}")
        oid = out.decode("ascii", "replace").strip() if rc == 0 else None
        if oid is None:
            rc2, _ = _git(repo, "cat-file", "-e", commit)
            if rc2 != 0:
                return None
            oid = "<absent>"
        lines.append(f"{path}\0{oid}\n")
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def changed_paths(repo: Path, since: str, commit: str, paths: Sequence[str]) -> list[str] | None:
    """The watched files that differ between `since` and `commit` (committed state), or None
    when git cannot say."""
    rc, out = _git(repo, "diff", "--name-only", "--no-renames", "-z", since, commit,
                   "--", *paths)
    return None if rc != 0 else sorted(n for n in os.fsdecode(out).split("\0") if n)


def stale_since(repo: Path, since: str, commit: str, paths: Sequence[str]) -> str | None:
    """The first commit after `since` on the way to `commit` that touched a watched path."""
    rc, out = _git(repo, "log", "--reverse", "--format=%H", f"{since}..{commit}", "--", *paths)
    listed = out.decode("ascii", "replace").split()
    return listed[0] if rc == 0 and listed and _FULL_SHA.fullmatch(listed[0]) else None


def _watch_records(repo: Path, head: str, gate: str) -> list[dict]:
    """The newest `MAX_FILES` watch records committed at HEAD, oldest first; a file that is not
    a readable record is left out (it can only make a re-run look necessary, never a pass)."""
    names = [n for n in _names(repo, head, f"{gate}/{WATCH_DIR}")
             if n.endswith(".json") and n.count("/") == gate.count("/") + 2]
    out = []
    for name in names[-MAX_FILES:]:
        try:
            body = _object(_blob(repo, head, name, name), name)
        except Refused:
            continue
        if (body.get("record_version") == 1 and body.get("verdict") in
                ("PASS", "FAIL", "ENVIRONMENT_GAP") and isinstance(body.get("relevant_tree"), str)
                and isinstance(body.get("commit"), str) and _FULL_SHA.fullmatch(body["commit"])):
            out.append({**body, "file": name})
    return out


def open_requests(repo: Path, head: str, phase: str, gate: str, x: str) -> list[dict]:
    """The reopen requests of the closure at X that no committed decision answers. A request is
    read as it was ADDED (history), so removing the file does not answer it; a committed
    `reopen/*.json` with decision REOPEN or KEEP_CLOSED that names the closure and carries
    `"request": <file>` does. Raises when git cannot say."""
    answered = set()
    for name in _names(repo, head, f"{gate}/{REOPEN_DIR}"):
        if name.endswith(".json"):
            body = _object(_blob(repo, head, name, name), name)
            if (body.get("decision") in ("REOPEN", "KEEP_CLOSED")
                    and isinstance(body.get("request"), str) and names_closure(body, phase, x)):
                answered.add(body["request"].strip().rsplit("/", 1)[-1])
    spec = f":(glob){gate}/{REQUEST_PREFIX}*.json"
    rc, out = _git(repo, "log", "--reverse", "--full-history", "-m", "--diff-filter=A",
                   "--no-renames", "--name-only", "--format=%x01%H", head, "--", spec)
    if rc != 0:
        raise Refused("git cannot list the reopen requests")
    added: dict[str, str] = {}
    current = None
    for line in os.fsdecode(out).splitlines():
        if line.startswith("\x01"):
            current = line[1:].strip()
        elif line.strip() and current and _FULL_SHA.fullmatch(current):
            added.setdefault(line.strip(), current)
    if len(added) > MAX_FILES:
        raise Refused(f"{len(added)} reopen requests were added, more than {MAX_FILES}: not listed")
    found = []
    for name, commit in sorted(added.items()):
        base = name.rsplit("/", 1)[-1]
        if name != f"{gate}/{base}" or not _REQUEST.fullmatch(base) or base in answered:
            continue
        try:
            body = _object(_blob(repo, commit, name, name), name)
        except Refused:
            continue
        if body.get("closed_at_commit") != x or body.get("phase") != phase:
            continue
        failed = body.get("failed_requirements")
        found.append({"file": base, "commit": body.get("commit"),
                      "failed": [v[:40] for v in failed if isinstance(v, str)][:20]
                      if isinstance(failed, list) else []})
    return found


def _uncommitted_decisions(repo: Path, head: str, gate: str) -> list[str]:
    """Decision files in the working tree's reopen directory that HEAD does not have: written,
    and not yet counting. Only names are read."""
    directory = f"{gate}/{REOPEN_DIR}"
    try:
        with os.scandir(repo / directory) as entries:
            found = [e.name for e in itertools.islice(entries, MAX_FILES + 1)]
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise Refused(f"{directory} cannot be listed ({exc.strerror})") from None
    if len(found) > MAX_FILES:
        raise Refused(f"{directory} holds more than {MAX_FILES} entries: not listed")
    committed = {n.rsplit("/", 1)[-1] for n in _names(repo, head, directory)}
    return sorted(n for n in found if n.endswith(".json") and n not in committed)


def _watch_lines(repo: Path, head: str, phase: str, gate: str, x: str, watch: dict | None
                 ) -> tuple[list[str], dict[str, Any]]:
    """What the regression watch says about a verified closure at HEAD, as lines and as facts,
    from committed records only. Never raises and never changes the closure: a piece that
    cannot be read is reported as unreadable, never as a pass."""
    lines: list[str] = []
    facts: dict[str, Any] = {"stale": None, "stale_since": None, "last_watch": None,
                             "requests": []}
    try:
        for name in _uncommitted_decisions(repo, head, gate)[:2]:
            lines.append(f"reopen decision {name} is written but not committed: it counts once "
                         "committed")
        requests = open_requests(repo, head, phase, gate, x)
        facts["requests"] = requests[:8]
        for request in requests[:3]:
            failed = ", ".join(request["failed"]) or "a watched requirement"
            lines.append(f"REGRESSION at {str(request['commit'])[:12]}: {failed} FAILED, reopen "
                         f"decision pending ({request['file']})")
        if len(requests) > 3:
            lines.append(f"and {len(requests) - 3} more reopen request(s) pending")
        if watch is None:
            lines.append("no regression watch declared")
            return lines, facts
        paths = watch["paths"]
        changed = changed_paths(repo, watch["evidence_commit"], head, paths)
        facts["stale"] = None if changed is None else bool(changed)
        if changed:
            facts["stale_since"] = stale_since(repo, watch["evidence_commit"], head, paths)
        records = _watch_records(repo, head, gate)
        last = records[-1] if records else None
        current = None
        if last is not None:
            stamp = last["file"].rsplit("/", 1)[-1].split("-")[0]
            current = last["relevant_tree"] == _relevant_tree(repo, head, paths)
            facts["last_watch"] = {"stamp": stamp, "verdict": last["verdict"],
                                   "commit": last["commit"], "current": current}
            lines.append(f"last re-run {stamp} {last['verdict']} at {last['commit'][:12]}")
        if changed is None:
            lines.append("evidence staleness cannot be told (git could not say)")
        elif changed and current is not True:
            since = str(facts["stale_since"] or "?")[:12]
            lines.append(f"evidence stale since {since}, re-run pending")
        elif not changed:
            lines.append(f"evidence unchanged since {watch['evidence_commit'][:12]}")
    except Refused as exc:
        lines.append(f"regression watch unreadable ({str(exc)[:100]})")
        facts["error"] = str(exc)[:120]
    return lines, facts


# --------------------------------------------------------------------------- #
# the receipt, as committed
# --------------------------------------------------------------------------- #

def _committed_receipt(root: Path, head: str, gate: str) -> bytes | None:
    """The receipt committed at HEAD, or None when HEAD has none (a file only in the working
    tree is not a closure: it is returned as a refusal naming that). The working-tree file must
    be that blob byte for byte -- the product reads the committed one."""
    path = f"{gate}/{CLOSURE_FILE}"
    rc, out = _git(root, "ls-tree", "-z", head, "--", path)
    if rc != 0:
        raise Refused(f"git could not look for {path} at {head[:12]} (exit {rc})")
    if not [r for r in out.split(b"\0") if r]:
        if os.path.lexists(root / path):
            raise Refused(f"{path} exists in the working tree but is not committed at "
                          f"{head[:12]}: commit it, a closure is read from HEAD")
        return None
    blob = _blob(root, head, path, "the closure receipt")
    if _read_regular(str(root / path), "the closure receipt in the working tree") != blob:
        raise Refused(f"{path} in the working tree differs from the one committed at "
                      f"{head[:12]}; the product reads the committed one")
    return blob


class _Reopened(Refused):
    """The closure verifies and a committed decision reopened it."""


@dataclass(frozen=True)
class Standing:
    claims: dict
    snapshot: Snapshot
    blocking: int
    x: str


def _standing(root: Path, head: str, phase: str, gate: str) -> Standing | None:
    """The closure of `phase` as its committed receipt states it, every claim re-derived from
    the blobs at X; None when no receipt is committed. Raises `Refused` (the reason) when one is
    and does not verify, or was reopened."""
    blob = _committed_receipt(root, head, gate)
    if blob is None:
        return None
    body = _object(blob, "the closure receipt")
    claims = _claims(body, RECEIPT_SCHEMA, phase, {"closed_by"})
    by = _exact(claims["closed_by"], {"entry_sha256", "head", "at_utc"}, "`closed_by`")
    _hex(by["entry_sha256"], _SHA256, "`closed_by.entry_sha256`")
    _hex(by["head"], _FULL_SHA, "`closed_by.head`")
    if not isinstance(by["at_utc"], str) or not _STAMP.fullmatch(by["at_utc"]):
        raise Refused("`closed_by.at_utc` is missing or not in its shape")
    snapshot, blocking = _verify_at_x(root, head, phase, claims)
    x = claims["closed_at_commit"]
    _receipt_unedited(root, head, f"{gate}/{CLOSURE_FILE}")
    hit = reopened_by(root, head, phase, gate, x)
    if hit is not None:
        raise _Reopened(f"reopened by {hit[0]}" + (f": {hit[1]}" if hit[1] else ""))
    return Standing(claims, snapshot, blocking, x)


def _receipt_unedited(root: Path, head: str, path: str) -> None:
    """The receipt was only ever added or moved away: every commit that touched its path did one of
    those. A receipt edited in place (a new closing commit written into the old file) is a closure
    nobody closed; refused with the commit that changed it."""
    rc, out = _git(root, "log", "--full-history", "-m", "--no-renames", "--name-status",
                   "--format=%x01%H", head, "--", f":(literal){path}")
    if rc != 0:
        raise Refused(f"git could not read the history of {path} (exit {rc})")
    commit = ""
    for line in os.fsdecode(out).splitlines():
        if line.startswith("\x01"):
            commit = line[1:].strip()
        elif line.strip() and line.split("\t", 1)[0] not in ("A", "D"):
            raise Refused(f"{path} was changed in place (commit {commit[:12]}): a receipt is never "
                          "edited -- move it aside under a versioned name and close again")


def _live_snapshot(repo: Path, head: str, standing: Standing) -> tuple[Snapshot, list[str]]:
    """The snapshot at HEAD at the receipt's path, or the one at X with a line saying why not."""
    path = standing.claims["snapshot"]["path"]
    try:
        return _snapshot(_object(_blob(repo, head, path, "the live snapshot"),
                                 "the live snapshot")), []
    except Refused as exc:
        return standing.snapshot, [(f"live snapshot {path} at HEAD cannot be read "
                                    f"({str(exc)[:100]}); the one at the closing commit is shown")]


def phase_status(repo: str, phase: str, gate_dir: str | None = None) -> PhaseStatus:
    """The state of one phase. Raises `Refused` only when the repository itself cannot be
    judged; a record that does not verify is a state (OPEN, with its reason), never a raise."""
    phase = _phase(phase)
    root, head = _repository(repo)
    gate = _gate_dir(phase, gate_dir)
    try:
        standing = _standing(root, head, phase, gate)
    except _Reopened as exc:
        return PhaseStatus(phase, "REOPENED", f"{phase} OPEN: closure {exc}", str(exc))
    except Refused as exc:
        why = str(exc)[:300]
        return PhaseStatus(phase, "OPEN", f"{phase} OPEN: closure record ignored: {why}", why)
    if standing is None:
        return PhaseStatus(phase, "OPEN", f"{phase} OPEN: no closure record")
    lines, facts = _watch_lines(root, head, phase, gate, standing.x, standing.claims["watch"])
    live, extra = _live_snapshot(root, head, standing)
    closure = Closure(phase=phase, closed_at=standing.x, ref=standing.claims["ref"],
                      watch=[*lines, *extra][:8])
    verdict = phase_gate(live, phase, closure=closure)
    return PhaseStatus(phase, "CLOSED", f"{phase} {verdict.reading()}", "", verdict, facts)


# --------------------------------------------------------------------------- #
# writing: the receipt and the decision
# --------------------------------------------------------------------------- #

def _stamp_now(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _plain_dir(root: Path, rel: str) -> Path:
    """`root/rel` as a real directory below `root`, made where it is missing: a component that is
    a link, or a file, refuses (a record must not land outside the repository through a link)."""
    here = root
    for part in rel.split("/"):
        here = here / part
        try:
            mode = os.lstat(here).st_mode
        except FileNotFoundError:
            try:
                os.mkdir(here, 0o755)
            except FileExistsError:
                pass
            except OSError as exc:
                raise Refused(f"{rel} cannot be created ({exc.strerror})") from None
            mode = os.lstat(here).st_mode
        except OSError as exc:
            raise Refused(f"{rel} cannot be looked at ({exc.strerror})") from None
        if not stat.S_ISDIR(mode):
            raise Refused(f"{part} on the path {rel} is a link or not a directory (not followed)")
    return here


def _create(root: Path, rel_dir: str, name: str, text: str) -> tuple[Path, str]:
    """Create `rel_dir/name` (never replace), read it back. Returns `(path, warning)`."""
    directory = _plain_dir(root, rel_dir)
    path = directory / name
    warning = ""
    try:
        try:
            durable_create(path, text)
        except DurableWriteNotSynced as exc:
            warning = (f"phase warning: {rel_dir}/{name} is in place, but its directory entry may "
                       f"not be durable ({exc.strerror}); look at it before relying on it\n")
    except FileExistsError:
        raise Refused(f"{rel_dir}/{name} already exists and is never replaced") from None
    except (OSError, ValueError, RuntimeError) as exc:
        raise Refused(f"{rel_dir}/{name} cannot be created ({type(exc).__name__}: {exc})") from None
    if _read_regular(str(path), "what was written") != text.encode("utf-8"):
        raise Refused(f"{rel_dir}/{name} exists but what was read back is not what was written; "
                      "do not use it")
    return path, warning


def _text(body: dict) -> str:
    return json.dumps(body, indent=2, sort_keys=True) + "\n"


def close_phase(repo: str, phase: str, record: str, gate_dir: str | None = None, *,
                now: datetime | None = None) -> tuple[str, str]:
    """Verify the closure entry at `record` against the blobs at its closing commit and write
    the receipt. Returns `(message, warning)`; raises `Refused` and writes nothing otherwise."""
    phase = _phase(phase)
    root, head = _repository(repo)
    gate = _gate_dir(phase, gate_dir)
    raw = _read_regular(record, "the closure entry")
    claims = _claims(_object(raw, "the closure entry"), ENTRY_SCHEMA, phase)
    path = f"{gate}/{CLOSURE_FILE}"
    rc, out = _git(root, "ls-tree", "-z", head, "--", path)
    if rc != 0:
        raise Refused(f"git could not look for {path} at {head[:12]} (exit {rc})")
    if [r for r in out.split(b"\0") if r] or os.path.lexists(root / path):
        raise Refused(f"{path} already exists: a receipt is never replaced (if a reopen ended "
                      "it, move it aside under a versioned name and commit that first)")
    _, blocking = _verify_at_x(root, head, phase, claims)
    hit = reopened_by(root, head, phase, gate, claims["closed_at_commit"])
    if hit is not None:
        raise Refused(f"{claims['closed_at_commit'][:12]} was reopened by {hit[0]}: close "
                      "at a later commit")
    receipt = {**claims, "schema": RECEIPT_SCHEMA, "phase": phase,
               "closed_by": {"entry_sha256": _digest(raw), "head": head,
                             "at_utc": _stamp_now(now)}}
    written, warning = _create(root, gate, CLOSURE_FILE, _text(receipt))
    x = claims["closed_at_commit"]
    watch = claims["watch"]
    msg = (f"phase close: {phase} closed at {x[:12]} ({claims['ref']}): receipt {path} written, "
           f"sha256 {_digest(written.read_bytes())}\n"
           f"  snapshot {claims['snapshot']['path']} sealed {claims['snapshot']['seal'][:12]}..., "
           f"{blocking} blocking item(s) of {phase} VERIFIED or WAIVED (seal checked, not "
           f"re-derived from evidence)\n"
           f"  evidence: {len(claims['evidence'])} file(s) digest-bound at {x[:12]}\n"
           f"  watch: " + (f"{len(watch['paths'])} path(s) since {watch['evidence_commit'][:12]}"
                           if watch else "none declared") + "\n"
           "  commit the receipt: a closure is read from HEAD, never from the working tree\n")
    return msg, warning


def _reopen_files(root: Path, gate: str) -> list[tuple[str, dict]]:
    """The decision files in the working tree's reopen directory (committed or not), read; a
    file that cannot be read refuses (an unread file cannot be told from a decision)."""
    directory = root / gate / REOPEN_DIR
    try:
        names = sorted(e.name for e in os.scandir(directory))
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise Refused(f"{gate}/{REOPEN_DIR} cannot be listed ({exc.strerror})") from None
    if len(names) > MAX_FILES:
        raise Refused(f"{gate}/{REOPEN_DIR} holds more than {MAX_FILES} files: not read")
    out = []
    for name in names:
        if name.endswith(".json"):
            out.append((name, _object(_read_regular(str(directory / name), name), name)))
    return out


def reopen_phase(repo: str, phase: str, request: str, reason: str,
                 gate_dir: str | None = None, *, now: datetime | None = None
                 ) -> tuple[str, str]:
    """Record the operator's decision to reopen the standing closure of `phase`, answering one
    reopen request. Returns `(message, warning)`; raises `Refused` and writes nothing otherwise."""
    phase = _phase(phase)
    root, head = _repository(repo)
    gate = _gate_dir(phase, gate_dir)
    text = reason.strip() if isinstance(reason, str) else ""
    if (not text or len(text) > MAX_REASON or any(ord(c) < 32 and c != "\t" for c in text)
            or "\x7f" in text):
        raise Refused(f"--reason must be 1 to {MAX_REASON} characters of text, without control "
                      "characters")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise Refused("--reason is not valid UTF-8 text") from None
    base = os.path.basename(str(request))
    if not _REQUEST.fullmatch(base):
        raise Refused(f"--request {str(request)[:80]!r} is not a reopen request file "
                      f"({REQUEST_PREFIX}*.json)")
    try:
        standing = _standing(root, head, phase, gate)
    except Refused as exc:
        raise Refused(f"there is no standing closure of {phase} to reopen: {exc}") from None
    if standing is None:
        raise Refused(f"there is no standing closure of {phase} to reopen: no closure record")
    x = standing.x
    on_disk = _read_regular(str(root / gate / base), "the reopen request")
    if str(request) != base and os.path.realpath(str(request)) != str(root / gate / base):
        raise Refused(f"--request {str(request)[:120]!r} is not the file {gate}/{base}")
    if _blob(root, head, f"{gate}/{base}", "the reopen request") != on_disk:
        raise Refused(f"the reopen request {gate}/{base} is not committed at {head[:12]} as it "
                      "is on disk: commit it first")
    body = _object(on_disk, "the reopen request")
    if phase_of(body.get("phase")) != phase or body.get("closed_at_commit") != x:
        raise Refused(f"the reopen request is not about the closure of {phase} at {x[:12]}")
    for name, found in _reopen_files(root, gate):
        if names_closure(found, phase, x) and found.get("decision") == "REOPEN":
            raise Refused(f"the closure of {phase} at {x[:12]} already has a reopen decision "
                          f"({name}); commit it")
    decision = {"schema": DECISION_SCHEMA, "decision": "REOPEN", "phase": phase,
                "closed_at_commit": x, "request": base, "reason": text,
                "decided_at_utc": _stamp_now(now)}
    name = f"{decision['decided_at_utc']}-{x[:12]}.json"
    _create(root, f"{gate}/{REOPEN_DIR}", name, _text(decision))
    return (f"phase reopen: {phase} closure at {x[:12]} reopened by {gate}/{REOPEN_DIR}/{name} "
            f"(request {base})\n  commit the decision: it counts once it is committed\n"), ""


# --------------------------------------------------------------------------- #
# the commands
# --------------------------------------------------------------------------- #

def _phases_present(repo: str) -> list[str]:
    root, head = _repository(repo)
    rc, out = _git(root, "ls-tree", "-z", "--name-only", head, "--", GATE_ROOT + "/")
    found = []
    for entry in os.fsdecode(out).split("\0") if rc == 0 else []:
        phase = phase_of(entry.rsplit("/", 1)[-1])
        if phase is not None and entry.rsplit("/", 1)[-1] == phase.lower():
            found.append(phase)
    return sorted(set(found), key=lambda p: int(p[1:]))


def run_status(*, repo: str, phase: str | None, gate_dir: str | None = None,
               as_json: bool = False) -> Outcome:
    """Exit 0: every phase asked about is CLOSED. Exit 1: one is not (OPEN or REOPENED, the
    reason printed). Exit 2: the repository cannot be judged. Writes nothing."""
    try:
        if phase is not None:
            phases = [_phase(phase)]
        elif gate_dir is not None:
            raise Refused("--gate-dir needs a phase")
        else:
            phases = _phases_present(repo)
        results = [phase_status(repo, p, gate_dir) for p in phases]
    except Refused as exc:
        return Outcome(2, "", f"phase status refused: {exc}\n")
    except Exception as exc:                      # noqa: BLE001 - a crash must not read as "open"
        return Outcome(2, "", f"phase status refused: it failed itself: "
                              f"{type(exc).__name__}: {exc}\n")
    code = 0 if results and all(r.state == "CLOSED" for r in results) else 1
    if as_json:
        return Outcome(code, json.dumps({"phases": [r.as_json() for r in results]}, indent=2,
                                        sort_keys=True) + "\n")
    out = "".join(r.reading + "\n" for r in results) or "no phase has a closure record\n"
    return Outcome(code, out)


def run_close(*, repo: str, phase: str, record: str, gate_dir: str | None = None,
              now: datetime | None = None) -> Outcome:
    """Exit 0: the receipt is written. Exit 1: refused, nothing written (stderr says why)."""
    try:
        msg, warning = close_phase(repo, phase, record, gate_dir, now=now)
    except Refused as exc:
        return Outcome(1, "", f"phase close refused: {exc}\n")
    except Exception as exc:                      # noqa: BLE001
        return Outcome(1, "", f"phase close refused: it failed itself: "
                              f"{type(exc).__name__}: {exc}\n")
    return Outcome(0, msg, warning)


def run_reopen(*, repo: str, phase: str, request: str, reason: str,
               gate_dir: str | None = None, now: datetime | None = None) -> Outcome:
    """Exit 0: the decision is written. Exit 1: refused, nothing written."""
    try:
        msg, warning = reopen_phase(repo, phase, request, reason, gate_dir, now=now)
    except Refused as exc:
        return Outcome(1, "", f"phase reopen refused: {exc}\n")
    except Exception as exc:                      # noqa: BLE001
        return Outcome(1, "", f"phase reopen refused: it failed itself: "
                              f"{type(exc).__name__}: {exc}\n")
    return Outcome(0, msg, warning)
