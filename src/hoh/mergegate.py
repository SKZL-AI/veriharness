"""The merge gate as a product command: may this candidate be merged?

Amendment A6, requirement row P5-36. Until now the gate was an orchestrator script. This module is the
same decision with the slips that script let through closed, as pure functions plus one receipt:

* every required review report ends with a **footer** of six `KEY: value` lines (the five bound keys
  `VERDICT`, `CANDIDATE`, `BASE`, `SPEC_SHA256`, `BLOCKING_OPEN`, and `COMPLETENESS`);
* a merge is eligible only if every required review is present, unique, **complete**, **stable**,
  `RELEASE_YES` with 0 open blockers, and bound to the candidate, base and specification digest that
  are recomputed now. Missing, stale, ambiguous, partial, unstable or colliding means not eligible;
* a review bound to an older base than the current one is **carried forward mechanically** when the gate
  itself measures (`measure_carry`) that the older base is an ancestor of the current one, that no commit
  in between touches a path the candidate changes, and that merging the candidate is clean; any other
  outcome leaves it stale. The receipt records what was measured (MG-RB1, row P5-36). A carried review
  stands **only with a suite receipt** (`--suite-receipt`, `judge_suite`) that is bound to the merge
  being judged: its base, candidate, base tree and merged tree are the ones recomputed now and every
  new failure it reports is accepted by name -- leg 3 of the rule, the suite on the merge tree;
* a report that is partial, or that was modified a moment ago, or that changed while it was read, is
  **no verdict** -- never `RELEASE_YES` and never `RELEASE_NO`;
* the decision is written as a receipt that is created, never replaced; `verify` re-reads it, refuses a
  report that changed after the gate ran, and can tie the receipt to the pair about to be merged.

What it does not do: it merges nothing, writes nothing but the receipt (a carry runs `git merge-tree` in a
private, temporary object store), and decides nothing about the content of a review. It does not run the
suite: the suite receipt is written by the comparison tool that did, and the gate checks that it is for
exactly this merge. A footer is what a reviewer *declares*; the gate checks that the declaration is
complete, unambiguous, bound to the right objects and not in flux. The receipt is not signed, and no
merge step of the product consults it yet.
"""
from __future__ import annotations

import ast
import errno
import hashlib
import io
import json
import math
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive_bundle import _QUIET, _git_env
from .durable_io import DurableWriteNotSynced, durable_create

SCHEMA = "hoh-merge-gate/1"

#: The footer, in the order the reviewer rules print it. The grammar is closed: a seventh key is a
#: specification change, so an unknown key in the footer block is refused, not ignored.
FOOTER_KEYS = ("VERDICT", "CANDIDATE", "BASE", "SPEC_SHA256", "BLOCKING_OPEN", "COMPLETENESS")
VERDICTS = ("RELEASE_YES", "RELEASE_NO")
COMPLETENESS = ("COMPLETE", "PARTIAL")

#: A report must be at least this old (seconds since its last modification) when the gate reads it.
DEFAULT_MIN_AGE = 60.0
#: A report (or receipt) larger than this is not read (both are kilobytes); it is no verdict, not a memory risk.
MAX_REPORT_BYTES = 8 * 1024 * 1024
#: Dual review is mandatory; fewer required reviews is possible only by saying so (`--min-reviews`).
DEFAULT_MIN_REVIEWS = 2
#: A review bound to an older base is carried forward mechanically (see `measure_carry`) over at most this
#: many commits; a longer range is a stale base as before. Not an option: the number is the rule.
CARRY_MAX_COMMITS = 256
#: ... and only while the path lists git prints for the candidate and for the range stay below this size.
CARRY_MAX_BYTES = 4 * 1024 * 1024
#: ... and at most this many code files (`src/hoh/*.py`, `tools/*.py`) on either side, whose imports are read.
CARRY_MAX_CODE_FILES = 256
#: The register: its rows are compared by id, not as one file (MG-RB2).
REGISTER_PATH = b"program/v3_3/REQUIREMENTS.json"
LEDGER_PATH = b"DOGFOOD_LEDGER.md"
#: A candidate whose every changed path is one of these is records-only: the ledger and `dogfood/` records may
#: overlap what the range touched (git merges them or the merge-tree refuses), the register by rows.
RECORD_FILES = (REGISTER_PATH, LEDGER_PATH)
RECORD_PREFIXES = (b"dogfood/", b"program/v3_3/")
_MATRIX_TIMEOUT = 120.0
_MATRIX_OUT_MAX = 1024 * 1024
#: the merge tree's `src/hoh`, `tools`, `tests` and plan scope, by `git ls-tree -l`, before they are archived
_MATRIX_ARCHIVE_MAX = 64 * 1024 * 1024

#: The suite-comparison receipt a carried-forward review stands with (leg 3 of the operator's rule): what the
#: comparison tool that ran the suite on the merge writes, and what `judge_suite` reads. Closed shape.
SUITE_SCHEMA = "hoh-suite-receipt/1"
_SUITE_KEYS = frozenset({"schema", "base", "candidate", "base_tree", "merged_tree", "counts",
                         "new_failures", "accepted", "measured_at"})
_SUITE_COUNTS = ("base_failed", "candidate_failed", "new")
_SUITE_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_SUITE_ID_MAX = 1024

AGGREGATES = ("MERGE_ELIGIBLE", "NOT_MERGE_ELIGIBLE")
#: A review that speaks for this candidate and says no. Every other problem code makes it no verdict.
_NO_CODES = frozenset({"VERDICT_NO", "BLOCKING_OPEN"})

_GIT_TIMEOUT = 60.0
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_FOOTER_LINE = re.compile(r"([A-Z][A-Z0-9_]*):\s*(\S+)")
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DIGITS = re.compile(r"[0-9]+")
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class Refused(Exception):
    """The gate cannot decide (bad input, unreadable repository, receipt not creatable). Nothing is
    written, nothing is eligible; the command exits 2."""


@dataclass(frozen=True)
class Outcome:
    code: int
    out: str = ""
    err: str = ""


def _fstat(fd: int) -> os.stat_result:
    """The one place a report's identity is read from an open file (a seam for tests)."""
    return os.fstat(fd)


# --- the footer ---------------------------------------------------------------------------------


def parse_footer(text: str) -> tuple[dict[str, str] | None, tuple[str, str] | None]:
    """`(footer, None)` or `(None, (code, detail))`.

    The footer is the **trailing block** of the report: the maximal run of last non-blank lines that
    each read `KEY: value` (KEY upper case, value one token). Lines are split on CR LF, CR and LF
    only, so a Unicode line separator inside a sentence cannot make a footer line. Anything else
    after the footer ends the block and the footer is then incomplete (a report that kept growing
    after its footer is not final). Every key of `FOOTER_KEYS` must occur exactly once; an unknown
    key, a repeated key, or a value outside the closed vocabulary is refused, never guessed.
    """
    lines = [ln.strip() for ln in _LINE_BREAK.split(text)]
    block: list[tuple[str, str]] = []
    for line in reversed([ln for ln in lines if ln]):
        match = _FOOTER_LINE.fullmatch(line)
        if match is None:
            break
        block.append((match.group(1), match.group(2)))
    block.reverse()
    found: dict[str, str] = {}
    for key, value in block:
        if key not in FOOTER_KEYS:
            return None, ("FOOTER_UNKNOWN_KEY", f"unknown footer key {key!r}")
        if key in found:
            return None, ("FOOTER_DUPLICATE", f"footer key {key} occurs twice")
        found[key] = value
    missing = [key for key in FOOTER_KEYS if key not in found]
    if missing:
        return None, ("FOOTER_INCOMPLETE", f"the trailing block lacks {', '.join(missing)}")
    if found["VERDICT"] not in VERDICTS:
        return None, ("FOOTER_AMBIGUOUS", f"VERDICT {found['VERDICT']!r} is neither of {VERDICTS}")
    if _DIGITS.fullmatch(found["BLOCKING_OPEN"]) is None:
        return None, ("FOOTER_AMBIGUOUS",
                      f"BLOCKING_OPEN {found['BLOCKING_OPEN']!r} is not a non-negative integer")
    if found["COMPLETENESS"] not in COMPLETENESS:
        return None, ("FOOTER_AMBIGUOUS",
                      f"COMPLETENESS {found['COMPLETENESS']!r} is neither of {COMPLETENESS}")
    return found, None


def _count(value: str) -> str:
    """A BLOCKING_OPEN value as a short number: leading zeros dropped, long counts cut (no int(), so a
    count of any length is a count and not an overflow)."""
    number = value.lstrip("0") or "0"
    return number if len(number) <= 20 else number[:20] + "..."


def judge_footer(footer: Mapping[str, str], *, candidate: str, base: str, spec_sha256: str
                 ) -> list[tuple[str, str]]:
    """The problems of a well-formed footer against the values recomputed now (pure)."""
    problems: list[tuple[str, str]] = []
    if footer["COMPLETENESS"] != "COMPLETE":
        problems.append(("INCOMPLETE", "the reviewer declared the review PARTIAL: no verdict"))
    if footer["VERDICT"] != "RELEASE_YES":
        problems.append(("VERDICT_NO", f"VERDICT is {footer['VERDICT']}"))
    if footer["BLOCKING_OPEN"].lstrip("0"):
        problems.append(("BLOCKING_OPEN", f"{_count(footer['BLOCKING_OPEN'])} BLOCKING finding(s) open"))
    if footer["CANDIDATE"] != candidate:
        problems.append(("STALE_CANDIDATE",
                         f"reviewed candidate {footer['CANDIDATE'][:12]!r} is not {candidate[:12]}"))
    if footer["BASE"] != base:
        problems.append(("STALE_BASE", f"reviewed base {footer['BASE'][:12]!r} is not {base[:12]}"))
    if footer["SPEC_SHA256"] != spec_sha256:
        problems.append(("STALE_SPEC", "the reviewed specification digest differs"))
    return problems


def _status(codes: Sequence[str]) -> str:
    if not codes:
        return "RELEASE_YES"
    return "RELEASE_NO" if set(codes) <= _NO_CODES else "NO_VERDICT"


# --- reading one report ---------------------------------------------------------------------------


class _Unreadable(OSError):
    """A file the gate will not read (a report or a receipt), with the reason that is shown."""


def _is_link(name: str, dir_fd: int) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode)
    except OSError:
        return False


def _open_regular(path: str, what: str = "report") -> int:
    """An open descriptor of the regular file at the absolute, already resolved `path`; `_Unreadable` or
    another `OSError` otherwise. Never blocks and never follows a link, at any component: the path is
    walked from `/` one component at a time, each directory opened with `O_DIRECTORY | O_NOFOLLOW` and
    the next name opened relative to it (a directory swapped for a link after `realpath` is refused, not
    entered); the last name is opened with `O_NONBLOCK` (a FIFO returns at once), `O_NOFOLLOW` and
    `O_NOCTTY` (a terminal does not become the process's); and the kind is read from the descriptor that
    was opened, not from the path a moment earlier, so that no swap between a check and the open can
    make the gate wait or read something that is not a regular file."""
    if not os.path.isabs(path):
        raise ValueError(f"{path!r} is not an absolute path")
    parts = [part for part in path.split("/") if part]
    if not parts:
        raise _Unreadable(f"the {what} is not a regular file")
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NOCTTY | os.O_CLOEXEC
    leaf_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_NOCTTY | os.O_CLOEXEC
    dir_fd = os.open("/", dir_flags)
    try:
        for part in parts[:-1]:
            try:
                below = os.open(part, dir_flags, dir_fd=dir_fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP or (exc.errno == errno.ENOTDIR and _is_link(part, dir_fd)):
                    raise _Unreadable(f"a directory on the path of the {what} is a symbolic link "
                                      f"(not followed)") from exc
                raise
            os.close(dir_fd)
            dir_fd = below
        try:
            fd = os.open(parts[-1], leaf_flags, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise _Unreadable(f"the {what} is a symbolic link (not followed)") from exc
            if exc.errno == errno.ENXIO:                  # a socket
                raise _Unreadable(f"the {what} is not a regular file") from exc
            raise
    finally:
        os.close(dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Unreadable(f"the {what} is not a regular file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_bounded(fd: int, limit: int, what: str = "report") -> bytes:
    """At most `limit` bytes of the descriptor; one byte more than that is `_Unreadable`."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(1 << 16, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise _Unreadable(f"the {what} is larger than {limit} bytes: not read")


def _read_file(path: str, what: str) -> bytes:
    """The bytes of a small regular file (a receipt), read the way a report is: resolved, opened without
    waiting or following, kind checked on the descriptor, bounded."""
    fd = _open_regular(os.path.realpath(path), what)
    try:
        return _read_bounded(fd, MAX_REPORT_BYTES, what)
    finally:
        os.close(fd)


def _read_report(path: str, *, now: float, min_age: float) -> dict[str, Any]:
    """What can be known about one report file; problems are codes, never exceptions."""
    row: dict[str, Any] = {"report": path, "data": None, "size": None, "mtime_ns": None,
                           "sha256": None, "identity": None, "problems": []}
    try:
        real = os.path.realpath(path)
        row["report"] = real
        fd = _open_regular(real)
        try:
            before = _fstat(fd)
            data = _read_bounded(fd, MAX_REPORT_BYTES)
            after = _fstat(fd)
        finally:
            os.close(fd)
    except (FileNotFoundError, NotADirectoryError):
        row["problems"].append(("REPORT_MISSING", f"no report at {path!r}"))
        return row
    except _Unreadable as exc:
        row["problems"].append(("REPORT_UNREADABLE", str(exc)))
        return row
    except (OSError, ValueError) as exc:
        row["problems"].append(("REPORT_UNREADABLE", f"{type(exc).__name__}: {exc}"))
        return row
    row.update(data=data, size=len(data), mtime_ns=after.st_mtime_ns,
               sha256=hashlib.sha256(data).hexdigest(), identity=(after.st_dev, after.st_ino))
    if ((before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
            or len(data) != after.st_size):
        row["problems"].append(("REPORT_UNSTABLE", "the report changed while it was being read"))
    else:
        age = now - after.st_mtime_ns / 1e9
        if age < min_age:
            why = (f"the report was modified {age:.0f} s ago (less than {min_age:g} s); "
                   f"read it again later, with a fresh receipt name")
            row["problems"].append(("REPORT_UNSTABLE", why))
    return row


# --- git ------------------------------------------------------------------------------------------


def _git_run(repo: str, *args: str, env: Mapping[str, str] | None = None
             ) -> subprocess.CompletedProcess:
    """One git command, hardened the same way for every caller; the exit code is the caller's to read."""
    command = ["git", *_QUIET, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
               "-C", repo, *args]
    try:
        return subprocess.run(command, capture_output=True, env={**_git_env(), **(env or {})},
                              stdin=subprocess.DEVNULL, timeout=_GIT_TIMEOUT, check=False)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise Refused(f"git cannot be run in {repo!r}: {exc}") from exc


def _git(repo: str, *args: str) -> bytes:
    done = _git_run(repo, *args)
    if done.returncode != 0:
        raise Refused(f"git {args[0]} failed in {repo!r}: "
                      + done.stderr.decode("utf-8", "replace").strip()[:200])
    return done.stdout


def _clean_ref(what: str, ref: str) -> str:
    if not ref or ref.startswith("-") or "\x00" in ref or "\n" in ref:
        raise Refused(f"{what} {ref!r} is not a usable revision")
    return ref


def _resolve(repo: str, what: str, ref: str) -> str:
    _clean_ref(what, ref)
    try:
        sha = _git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}").decode().strip()
    except Refused:
        raise Refused(f"{what} {ref!r} is not a commit of {repo!r}") from None
    if _OBJECT_ID.fullmatch(sha) is None:
        raise Refused(f"{what} {ref!r} did not resolve to a full object id")
    return sha


def _spec_sha256(repo: str, commit: str, spec: str) -> str:
    parts = spec.split("/")
    if (not spec or spec.startswith(("/", "-", "./")) or "\x00" in spec or ".." in parts
            or spec.endswith("/")):
        raise Refused(f"--spec {spec!r} is not a path inside the candidate")
    try:
        blob = _git(repo, "cat-file", "blob", f"{commit}:{spec}")
    except Refused:
        raise Refused(f"--spec {spec!r} is not a file of the candidate {commit[:12]}") from None
    return hashlib.sha256(blob).hexdigest()


# --- carrying a review forward ---------------------------------------------------------------------


@dataclass(frozen=True)
class Carry:
    """`measure_carry`'s answer: whether the review stands for the current base, the reason when it does
    not, and the record the receipt keeps when it does."""
    carried: bool
    detail: str
    record: dict[str, Any] | None = None


def _done(done: subprocess.CompletedProcess, what: str, *ok: int) -> subprocess.CompletedProcess:
    if done.returncode not in (0, *ok):
        raise Refused(f"git {what} failed (exit {done.returncode}): "
                      + done.stderr.decode("utf-8", "replace").strip()[:200])
    return done


def _paths(raw: bytes, what: str) -> set[bytes]:
    """The names in a NUL separated list printed by git; a list above `CARRY_MAX_BYTES` is not compared."""
    if len(raw) > CARRY_MAX_BYTES:
        raise Refused(f"the list of {what} is larger than {CARRY_MAX_BYTES} bytes")
    return {name for name in raw.split(b"\0") if name}


def _alternate(path: str) -> str:
    """A directory as one entry of `GIT_ALTERNATE_OBJECT_DIRECTORIES` (a list split at ':'): quoted the way
    git unquotes it, so a repository under a path with a colon is measured and not misread."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _shown(names: Sequence[bytes]) -> str:
    """Paths as one line of text: sorted, escaped (a path may hold any byte but NUL), at most five."""
    ordered = sorted(names)
    shown = ", ".join(ascii(name.decode("utf-8", "replace")) for name in ordered[:5])
    return shown + (f" and {len(ordered) - 5} more" if len(ordered) > 5 else "")


def _descends(repo: str, ancestor: str, rev: str) -> bool:
    """Is `ancestor` the commit `rev` or one of its ancestors? Any failure of git is "no" (fail closed)."""
    try:
        return _git_run(repo, "merge-base", "--is-ancestor", ancestor, rev).returncode == 0
    except Refused:
        return False


def _touched_in_between(repo: str, review_base: str, base: str, merge_base: str,
                        merges: Sequence[Sequence[str]]) -> set[bytes]:
    """The paths the commits of `review_base..base` touch, as whole paths, byte for byte (MG-RB3).

    A merge commit is also diffed against each of its parents (`log -m`), and the diff against a parent that
    forked before `review_base` lists the work the other parent brought in since the fork -- work at or below
    the reviewed base, which the reviewers saw. So when the reviewed base is the candidate's merge base or a
    descendant of it and the range holds merge commits, the lower bound is the reviewed base itself: a
    merge commit counts a path of its diff against a parent only if its version of the path also differs from
    the reviewed base's. Every other commit counts its own diff as before. A merge that changes a path in
    its own right (an edit, a conflict resolution, a revert to an older parent's version) still differs from
    the reviewed base and counts. A reviewed base that is not such a descendant, or a git failure while
    asking, keeps the rule as it was: `log -m` over the range. All lists together are held to
    `CARRY_MAX_BYTES`."""
    what = "the paths the commits in between touch"
    span = f"{review_base}..{base}"
    quiet = ("-c", "log.showSignature=false", "-c", "diff.ignoreSubmodules=none")
    if not merges or not _descends(repo, merge_base, review_base):
        return _paths(_done(_git_run(repo, *quiet, "log", "-m", "--no-renames", "--name-only", "-z",
                                     "--format=", span), "log").stdout, what)
    used = 0

    def take(*args: str) -> set[bytes]:
        nonlocal used
        raw = _done(_git_run(repo, *quiet, *args), args[0]).stdout
        used += len(raw)
        if used > CARRY_MAX_BYTES:
            raise Refused(f"the lists of {what} are larger than {CARRY_MAX_BYTES} bytes")
        return _paths(raw, what)

    seen = take("log", "--no-merges", "--no-renames", "--name-only", "-z", "--format=", span)
    for merge, *parents in merges:
        since = take("diff-tree", "-r", "--no-renames", "--name-only", "-z", review_base, merge)
        for parent in parents:
            seen |= take("diff-tree", "-r", "--no-renames", "--name-only", "-z", parent, merge) & since
    return seen


def measure_carry(repo: str, review_base: str, base: str, candidate: str) -> Carry:
    """May a review bound to `review_base` stand for `base`, the tip the candidate would be merged into?

    Only a measurement answers yes, and the gate makes every measurement itself (nothing is taken from
    a report except the sha the reviewer says it was bound to). All of these must hold; any other
    outcome, including a git command that fails, is "not carried" and the review stays `STALE_BASE`:

    1. `review_base` is a full object id of a commit that is an ancestor of `base` (not `base` itself:
       the caller compares first). A review of another line, of a branch that was moved back or
       rewritten, or bound to a commit after `base` is not an ancestor;
    2. the range `review_base..base` has at most `CARRY_MAX_COMMITS` commits;
    3. the candidate and `base` have exactly one merge base (none, or several, is not measured), and the
       candidate changes at least one path relative to it (renames count as the delete and the add);
    4. no commit of the range touches any of those paths, merge commits included, whatever the commit
       did to it and even if a later commit undid it (an exact, byte for byte path comparison). A merge
       commit counts a path it changes against a parent only if the path also differs from the reviewed
       base's (`_touched_in_between`, MG-RB3): work that was already at the reviewed base does not come
       back through a side branch that forked before it;
    5. `git merge-tree --write-tree` of `base` and the candidate is clean, with directory renames
       reported as conflicts, not silently followed. It runs in a private object store: the repository
       is read, never written. The tree it prints is recorded (`merged_tree`): the merge the candidate
       would make, which is what a suite run before the merge must have been run on.

    A carry alone says nothing about the suite: `evaluate` makes a carried review stand only together
    with a suite receipt bound to this merge (`judge_suite`).
    """
    try:
        return _measure_carry(repo, review_base, base, candidate)
    except Refused as exc:
        return Carry(False, f"not measurable: {exc}")


def _merge_tree(repo: str, base: str, candidate: str, want: Sequence[bytes] = (),
                blobs: dict[bytes, bytes | None] | None = None) -> tuple[int, list[str]]:
    """`(exit code, output lines)` of `git merge-tree --write-tree` of `base` and `candidate`: 0 and one
    line (the tree id) for a clean merge, 1 and the tree id plus the conflicted names otherwise; anything
    else is `Refused`. Directory renames are conflicts whatever the repository's configuration says. It
    runs in a private, temporary object store (`GIT_OBJECT_DIRECTORY`) that reads the repository's own
    (`GIT_ALTERNATE_OBJECT_DIRECTORIES`, quoted for git): the repository is never written. For a clean
    merge the content of each path of `want` in the merged tree (None when absent) is put into `blobs`
    while the store exists."""
    objects = _git(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects"
                   ).decode().strip()
    with tempfile.TemporaryDirectory(prefix="hoh-merge-gate-", ignore_cleanup_errors=True) as store:
        env = {"GIT_OBJECT_DIRECTORY": store, "GIT_ALTERNATE_OBJECT_DIRECTORIES": _alternate(objects)}
        merged = _done(_git_run(repo, "-c", "merge.directoryRenames=conflict", "merge-tree",
                                "--write-tree", "--name-only", "--no-messages", base, candidate, env=env),
                       "merge-tree", 1)
        lines = merged.stdout.decode("utf-8", "replace").splitlines()
        if (blobs is not None and merged.returncode == 0 and len(lines) == 1
                and _OBJECT_ID.fullmatch(lines[0]) is not None):
            for path in want:
                blobs[path] = _blob(repo, lines[0], path, env)
    return merged.returncode, lines



# --- what a carry compares: register rows, record files, imported modules, the matrix reading ---------------


def _is_record(path: bytes) -> bool:
    """A path of a records-only candidate: the register, the ledger, the dogfood records, the programme
    documents under `program/v3_3/`."""
    return path in RECORD_FILES or path.startswith(RECORD_PREFIXES)


def _blob(repo: str, rev: str, path: bytes, env: Mapping[str, str] | None = None) -> bytes | None:
    """The content of `path` at `rev` (a commit or a tree), or None when it does not exist there (a deleted
    or not yet added file). Any other git failure is `Refused`. At most `CARRY_MAX_BYTES`."""
    name = os.fsdecode(path)
    if not _done(_git_run(repo, "ls-tree", "-z", rev, "--", name, env=env), "ls-tree").stdout:
        return None
    raw = _done(_git_run(repo, "cat-file", "blob", f"{rev}:{name}", env=env), "cat-file").stdout
    if len(raw) > CARRY_MAX_BYTES:
        raise Refused(f"{name} at {rev[:12]} is larger than {CARRY_MAX_BYTES} bytes")
    return raw


def _register(repo: str, rev: str) -> dict[str, Any]:
    """The register at `rev` as `{"rows": {id: row}, "order": [id], "top": {key: value}}`; an absent file is an
    empty register. A file that is not a register of `requirements` rows with distinct string ids (not
    JSON, not UTF-8, a repeated key, not an object, a row without an id) is `Refused`: fail closed."""
    return _parse_register(_blob(repo, rev, REGISTER_PATH), rev[:12])


def _parse_register(raw: bytes | None, where: str) -> dict[str, Any]:
    """`_register` for bytes already read (`where` names the revision, or the merge tree)."""
    if raw is None:
        return {"rows": {}, "order": [], "top": {}}
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_distinct_keys)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise Refused(f"{REGISTER_PATH.decode()} at {where} is not readable JSON: {exc}"[:200]) from exc
    if not isinstance(body, dict) or not isinstance(body.get("requirements"), list):
        raise Refused(f"{REGISTER_PATH.decode()} at {where} is not a register of requirements")
    rows: dict[str, Any] = {}
    order: list[str] = []
    for row in body["requirements"]:
        rid = row.get("id") if isinstance(row, dict) else None
        if not isinstance(rid, str) or not rid or rid in rows:
            raise Refused(f"{REGISTER_PATH.decode()} at {where} has a row without a distinct string id")
        rows[rid] = row
        order.append(rid)
    return {"rows": rows, "order": order, "top": {k: v for k, v in body.items() if k != "requirements"}}


def _only_adds(old: bytes | None, new: bytes | None) -> bool:
    """Every line of `old` is in `new`, in order: `new` is `old` with lines inserted (an empty or absent
    `old` is always the case)."""
    if not old:
        return True
    wanted = iter(old.split(b"\n"))
    pending = next(wanted, None)
    for line in (new or b"").split(b"\n"):
        if pending is None:
            return True
        if line == pending:
            pending = next(wanted, None)
    return pending is None


def _append_only(m: bytes | None, c: bytes | None, b: bytes | None, h: bytes | None,
                 merged: bytes | None) -> bool:
    """Both sides only add lines (candidate M -> C, range B -> H) and the merged file holds each side's whole
    file verbatim, line by line in order: nothing was changed, removed or lost by the merge."""
    return (_only_adds(m, c) and _only_adds(b, h) and _only_adds(c, merged) and _only_adds(h, merged))


def _canon(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _changed_rows(old: Mapping[str, Any], new: Mapping[str, Any]) -> set[str]:
    """What differs between two registers: the ids of rows added, removed or changed, `<order>` when the
    common rows changed their order, `<key>` for a top-level key other than `requirements` that differs."""
    out = {rid for rid in set(old["rows"]) | set(new["rows"])
           if _canon(old["rows"].get(rid)) != _canon(new["rows"].get(rid))}
    if ([r for r in old["order"] if r in new["rows"]] != [r for r in new["order"] if r in old["rows"]]):
        out.add("<order>")
    for key in set(old["top"]) | set(new["top"]):
        if _canon(old["top"].get(key)) != _canon(new["top"].get(key)):
            out.add(f"<{key}>")
    return out


def _code_files(paths: set[bytes]) -> list[bytes]:
    """The python files of `paths` that the import graph of `tools/capability_matrix.py` covers:
    `src/hoh/<module>.py` and `tools/<module>.py`, directly under their folders."""
    return sorted(p for p in paths if p.endswith(b".py")
                  and ((p.startswith(b"src/hoh/") and p.count(b"/") == 2)
                       or (p.startswith(b"tools/") and p.count(b"/") == 1)))


def _imports(repo: str, rev: str, path: bytes, modules: set[str]) -> set[str]:
    """The `src/hoh` modules the python file imports, one level: the names of its import statements
    (anywhere in it) that are modules of the package, as `capability_matrix._import_graph` reads them. A file
    that is not python source is `Refused`."""
    raw = _blob(repo, rev, path)
    if raw is None:
        return set()
    try:
        tree = ast.parse(raw.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError, ValueError, RecursionError) as exc:
        raise Refused(f"{os.fsdecode(path)} at {rev[:12]} is not python source: {type(exc).__name__}") from exc
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[-1])
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name.split(".")[-1] for alias in node.names)
    return names & modules


def _coupling(repo: str, base: str, candidate: str, mine: set[bytes], seen: set[bytes]
              ) -> tuple[list[str], dict[str, Any]]:
    """`(reasons, record)`: where a file one side touches imports a module the other side touches (one
    level, both directions); `record` is empty when neither side touches a code file. More than
    `CARRY_MAX_CODE_FILES` code files on a side is `Refused`."""
    ours, theirs = _code_files(mine), _code_files(seen)
    if not ours and not theirs:
        return [], {}
    if max(len(ours), len(theirs)) > CARRY_MAX_CODE_FILES:
        raise Refused(f"more than {CARRY_MAX_CODE_FILES} code files are touched on one side: "
                      f"the imports are not followed")

    def modules(rev: str) -> set[str]:
        listed = _git(repo, "ls-tree", "-z", "--name-only", rev, "src/hoh/").decode("utf-8", "surrogateescape")
        return {Path(n).stem for n in listed.split("\0") if n.endswith(".py") and n.count("/") == 2}

    cand_modules, base_modules = modules(candidate), modules(base)
    ours_imports = {f: _imports(repo, candidate, f, cand_modules) for f in ours}
    theirs_imports = {f: _imports(repo, base, f, base_modules) for f in theirs}
    touched_theirs = {Path(os.fsdecode(f)).stem for f in theirs if f.startswith(b"src/hoh/")}
    touched_ours = {Path(os.fsdecode(f)).stem for f in ours if f.startswith(b"src/hoh/")}
    reasons = [f"{os.fsdecode(f)} (candidate) imports src/hoh/{m}.py, touched in between"
               for f, mods in sorted(ours_imports.items()) for m in sorted(mods & touched_theirs)]
    reasons += [f"{os.fsdecode(f)} (touched in between) imports src/hoh/{m}.py, which the candidate changes"
                for f, mods in sorted(theirs_imports.items()) for m in sorted(mods & touched_ours)]
    return reasons, {"candidate_code_files": len(ours), "range_code_files": len(theirs)}


_MATRIX_DRIVER = r"""
import ast, importlib.util, json, re, sys
from pathlib import Path
root, rows_file = Path(sys.argv[1]), Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("cm", root / "tools" / "capability_matrix.py")
cm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cm)
nodes = []
for path in sorted((root / "tests").rglob("*.py")):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        continue
    rel = path.relative_to(root).as_posix()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            nodes.append(f"{rel}::{node.name}")
        elif isinstance(node, ast.ClassDef):
            nodes.append(f"{rel}::{node.name}")
            nodes += [f"{rel}::{node.name}::{m.name}" for m in node.body
                      if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test")]
collection = ("\n".join(nodes) + "\n", True)
symbols, graph = cm._symbols(), cm._import_graph()
out = {}
for row in json.loads(rows_file.read_text(encoding="utf-8")):
    kind = (row.get("probe") or {}).get("kind", "none")
    if kind not in ("none", "symbol", "method", "test"):
        out[row["id"]] = {"status": "NOT_COMPARABLE", "evidence": [f"probe kind {kind!r}"]}
        continue
    got = cm.classify(row, symbols, collection, {}, graph)
    out[row["id"]] = {"status": got["status"],
                      "evidence": [re.sub(r"(?<=\.py):[0-9]+", "", e) for e in got["evidence"]]}
sys.stdout.write(json.dumps(out, sort_keys=True))
"""


def _matrix_reading(repo: str, rev: str, candidate: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """What `tools/capability_matrix.py` of the merge tree of `rev` and `candidate` says about `rows`
    (the candidate's own rows): `{id: {"status", "evidence"}}`, evidence with line numbers dropped. The
    tree is made in a private object store, its `src/hoh`, `tools`, `tests` and the plan scope written to a
    temporary directory (never the repository), and the matrix's own `classify` run on it in a separate
    interpreter (`-I`, no stdin, a time limit) with the tests' nodes read statically. The classifier is
    `tools/capability_matrix.py` of `rev` (the reviewers' base, or the current one), written over whatever
    the merge tree has: no file of the candidate is executed, only read. A conflict, a tree
    that cannot be written, a driver that fails or says anything but one JSON object of every row is
    `Refused`; so is a row whose probe is not a symbol, method, test or none (an `evidence` probe reads
    preserved runs, which this reading does not have)."""
    objects = _git(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects").decode().strip()
    with tempfile.TemporaryDirectory(prefix="hoh-merge-gate-", ignore_cleanup_errors=True) as scratch:
        work = Path(scratch)
        (work / "objects").mkdir()
        (work / "tree").mkdir()
        env = {"GIT_OBJECT_DIRECTORY": str(work / "objects"),
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": _alternate(objects)}
        merged = _done(_git_run(repo, "-c", "merge.directoryRenames=conflict", "merge-tree", "--write-tree",
                                "--no-messages", rev, candidate, env=env), "merge-tree", 1)
        lines = merged.stdout.decode("utf-8", "replace").splitlines()
        if merged.returncode == 1 or len(lines) != 1 or _OBJECT_ID.fullmatch(lines[0]) is None:
            raise Refused(f"the merge of the candidate into {rev[:12]} does not give one tree")
        shipped = ("src/hoh", "tools", "tests", "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json")
        listing = _done(_git_run(repo, "ls-tree", "-r", "-l", "-z", lines[0], "--", *shipped, env=env),
                        "ls-tree").stdout
        total = 0
        for entry in listing.split(b"\0"):
            fields = entry.partition(b"\t")[0].split()
            if len(fields) == 4 and fields[3].isdigit():
                total += int(fields[3])
        if total > _MATRIX_ARCHIVE_MAX:
            raise Refused(f"the merge tree's code and tests are larger than {_MATRIX_ARCHIVE_MAX} bytes")
        archive = _done(_git_run(repo, "archive", "--format=tar", lines[0], "--", *shipped, env=env),
                        "archive").stdout
        if not hasattr(tarfile, "data_filter"):
            raise Refused("this python cannot extract a tree safely (tarfile.data_filter)")
        try:
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(work / "tree", filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise Refused(f"the merge tree could not be written out: {type(exc).__name__}") from exc
        # the classifier is the base's, never the candidate's: nothing of the candidate is executed
        tool = _blob(repo, rev, b"tools/capability_matrix.py")
        if tool is None:
            raise Refused(f"tools/capability_matrix.py does not exist at {rev[:12]}")
        (work / "tool.new").write_bytes(tool)
        os.replace(work / "tool.new", work / "tree" / "tools" / "capability_matrix.py")
        (work / "driver.py").write_text(_MATRIX_DRIVER, encoding="utf-8")
        (work / "rows.json").write_text(json.dumps(rows), encoding="utf-8")
        try:
            done = subprocess.run([sys.executable, "-I", str(work / "driver.py"), str(work / "tree"),
                                   str(work / "rows.json")], cwd=work / "tree", capture_output=True,
                                  stdin=subprocess.DEVNULL, timeout=_MATRIX_TIMEOUT, check=False,
                                  env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"})
        except (OSError, subprocess.SubprocessError) as exc:
            raise Refused(f"the matrix reading could not be run: {type(exc).__name__}") from exc
    if done.returncode != 0 or len(done.stdout) > _MATRIX_OUT_MAX:
        raise Refused(f"the matrix reading failed at {rev[:12]} (exit {done.returncode})")
    try:
        got = json.loads(done.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refused("the matrix reading is not JSON") from exc
    if not isinstance(got, dict) or set(got) != {r["id"] for r in rows}:
        raise Refused("the matrix reading does not answer exactly the rows asked")
    return got


def _matrix_compare(repo: str, review_base: str, base: str, candidate: str,
                    rows: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """Reads `rows` on the reviewers' merge tree (review_base + candidate) and on the current one; `(record,
    "")` when every row reads the same, `(None, why)` naming the rows that do not."""
    then = _matrix_reading(repo, review_base, candidate, rows)
    now = _matrix_reading(repo, base, candidate, rows)
    differ = sorted(i for i in then if then[i] != now[i])
    unfit = sorted(i for i in now if now[i]["status"] == "NOT_COMPARABLE")
    if unfit:
        return None, "its probe cannot be read here: " + ", ".join(unfit[:5])
    if differ:
        return None, "; ".join(f"{i}: {then[i]['status']} -> {now[i]['status']}"
                               + ("" if then[i]["status"] != now[i]["status"] else " (evidence differs)")
                               for i in differ[:5])
    digest = hashlib.sha256(_canon(then).encode()).hexdigest()
    return {"rows": sorted(then), "digest": digest, "statuses": {i: then[i]["status"] for i in sorted(then)}}, ""


def _measure_carry(repo: str, review_base: str, base: str, candidate: str) -> Carry:
    if _OBJECT_ID.fullmatch(review_base) is None:
        return Carry(False, "the reviewed base is not a full object id")
    found = _git_run(repo, "rev-parse", "--verify", "--quiet", review_base + "^{commit}")
    if found.returncode != 0 or found.stdout.decode().strip() != review_base:
        return Carry(False, "the reviewed base is not a commit of the repository")
    ancestor = _done(_git_run(repo, "merge-base", "--is-ancestor", review_base, base),
                     "merge-base --is-ancestor", 1)
    if ancestor.returncode == 1:
        return Carry(False, "the reviewed base is not an ancestor of the base (another line, or the "
                            "base was moved back or rewritten)")
    listed = [line.split() for line in _git(repo, "rev-list", f"--max-count={CARRY_MAX_COMMITS + 1}", "--parents",
                                            f"{review_base}..{base}").decode().splitlines()]
    between = [line[0] for line in listed]
    merges = [line for line in listed if len(line) > 2]
    if len(between) > CARRY_MAX_COMMITS:
        return Carry(False, f"more than {CARRY_MAX_COMMITS} commits lie between the reviewed base "
                            f"and the base")
    roots = _done(_git_run(repo, "merge-base", "--all", base, candidate), "merge-base --all", 1)
    merge_bases = roots.stdout.decode().split()
    if not merge_bases:
        return Carry(False, "the candidate has no merge base with the base")
    if len(merge_bases) > 1:
        return Carry(False, "the candidate has more than one merge base with the base: not measured")
    mine = _paths(_git(repo, "diff-tree", "-r", "--no-renames", "--name-only", "-z", merge_bases[0],
                       candidate), "the paths the candidate changes")
    if not mine:
        return Carry(False, "the candidate changes no path relative to its merge base: "
                            "nothing to measure")
    seen = _touched_in_between(repo, review_base, base, merge_bases[0], merges)
    overlap = mine & seen
    notes: dict[str, Any] = {}
    edited: list[str] = []
    new_rows: dict[str, Any] = {}
    if REGISTER_PATH in mine:
        # the register is compared by rows, not as one file; an unparseable one is "not measurable"
        new = _register(repo, candidate)
        new_rows = new["rows"]
        theirs_rows: set[str] = set()
        ours_rows = _changed_rows(_register(repo, merge_bases[0]), new)
        edited = sorted(i for i in ours_rows if i in new["rows"])
        if REGISTER_PATH in overlap:
            theirs_rows = _changed_rows(_register(repo, review_base), _register(repo, base))
            shared = ours_rows & theirs_rows
            if shared:
                return Carry(False, f"{len(shared)} register row(s) the candidate changes are also changed by "
                                    f"the commits in between: {', '.join(sorted(shared)[:5])}"
                                    + (f" and {len(shared) - 5} more" if len(shared) > 5 else ""))
            overlap = overlap - {REGISTER_PATH}
        notes["register_rows"] = {"candidate": sorted(ours_rows), "range": sorted(theirs_rows),
                                  "overlap": []}
    ledger_pending = False
    if LEDGER_PATH in overlap and all(_is_record(p) for p in mine):
        # the one tolerated overlap: the ledger, when both sides only add lines (checked on the merged
        # file below); no other record is: a spec, a diff, a closure record is what a reviewer bound
        overlap = overlap - {LEDGER_PATH}
        ledger_pending = True
    if overlap:
        return Carry(False, f"{len(overlap)} path(s) the candidate changes are touched by the "
                            f"commits in between: {_shown(sorted(overlap))}")
    reasons, coupling = _coupling(repo, base, candidate, mine, seen)
    if reasons:
        return Carry(False, f"{len(reasons)} import coupling(s) between the candidate and the commits in "
                            f"between: " + "; ".join(reasons[:5])
                            + (f" and {len(reasons) - 5} more" if len(reasons) > 5 else ""))
    if coupling:
        notes["imports"] = coupling
    wanted = ([LEDGER_PATH] if ledger_pending else []) + ([REGISTER_PATH] if REGISTER_PATH in mine else [])
    merged_blobs: dict[bytes, bytes | None] = {}
    code, lines = _merge_tree(repo, base, candidate, wanted, merged_blobs)
    if code == 1:
        names = [line.encode() for line in lines[1:] if line]
        return Carry(False, "merging the candidate into the base conflicts"
                            + (f": {_shown(names)}" if names else ""))
    if len(lines) != 1 or _OBJECT_ID.fullmatch(lines[0]) is None:
        return Carry(False, "git merge-tree did not print one tree id for a clean merge")
    if REGISTER_PATH in mine:
        _parse_register(merged_blobs.get(REGISTER_PATH), "the merge tree")   # a merge that breaks it refuses
    if ledger_pending:
        if not _append_only(_blob(repo, merge_bases[0], LEDGER_PATH), _blob(repo, candidate, LEDGER_PATH),
                            _blob(repo, review_base, LEDGER_PATH), _blob(repo, base, LEDGER_PATH),
                            merged_blobs.get(LEDGER_PATH)):
            return Carry(False, f"1 path(s) the candidate changes are touched by the commits in between: "
                                f"{ascii(os.fsdecode(LEDGER_PATH))} (the ledger is carried over only when both "
                                f"sides only add lines and the merged file keeps every line of both)")
        notes["records_only_overlap_tolerated"] = [os.fsdecode(LEDGER_PATH)]
    if edited:
        # the rows the candidate edits must read the same on the reviewers' merge tree and on this one
        matrix, why = _matrix_compare(repo, review_base, base, candidate,
                                      [new_rows[i] for i in edited])
        if matrix is None:
            return Carry(False, f"the register rows the candidate edits do not read the same on the "
                                f"reviewers' merge tree and on the current one: {why}")
        notes["matrix"] = matrix
    return Carry(True, "carried forward", {
        "review_base": review_base, "base": base, "commits": between, "merge_base": merge_bases[0],
        "candidate_paths": len(mine), "touched_paths": len(seen), "overlap": [],
        "merge_tree": "clean", "merged_tree": lines[0], **notes})


# --- the suite receipt: leg 3 of a carried review ----------------------------------------------------


def _distinct_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"the key {key!r} occurs twice")
        out[key] = value
    return out


def parse_suite(raw: bytes) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    """`(receipt, None)` or `(None, (code, detail))`. A suite receipt is one JSON object with exactly the
    keys of `_SUITE_KEYS`: `schema` (`SUITE_SCHEMA`), `base`, `candidate`, `base_tree`, `merged_tree`
    (full object ids), `counts` (`base_failed`, `candidate_failed`, `new`: non-negative integers),
    `new_failures` and `accepted` (lists of distinct, non-empty test ids without control characters),
    `measured_at` (UTC, `YYYY-MM-DDTHH:MM:SSZ`). `counts.new` is the length of `new_failures` and is at
    least `candidate_failed - base_failed` (a candidate cannot fail more tests than the base without
    that many new ones). A key
    that occurs twice, an unknown key or a missing one is refused, never guessed."""
    def bad(why: str) -> tuple[None, tuple[str, str]]:
        return None, ("SUITE_RECEIPT_UNREADABLE", why)

    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_distinct_keys)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        return bad(f"not JSON: {exc}"[:200])
    if not isinstance(body, dict) or set(body) != _SUITE_KEYS:
        return bad("not an object with exactly the keys of a suite receipt")
    if body["schema"] != SUITE_SCHEMA:
        return bad(f"the schema is not {SUITE_SCHEMA}")
    for key in ("base", "candidate", "base_tree", "merged_tree"):
        if not isinstance(body[key], str) or _OBJECT_ID.fullmatch(body[key]) is None:
            return bad(f"{key} is not a full object id")
    counts = body["counts"]
    if (not isinstance(counts, dict) or set(counts) != set(_SUITE_COUNTS)
            or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in counts.values())):
        return bad("counts is not exactly base_failed, candidate_failed and new, non-negative integers")
    for key in ("new_failures", "accepted"):
        names = body[key]
        if (not isinstance(names, list) or len(set(map(str, names))) != len(names)
                or any(not isinstance(n, str) or not n or len(n) > _SUITE_ID_MAX
                       or _CONTROL.search(n) is not None for n in names)):
            return bad(f"{key} is not a list of distinct, non-empty test ids")
    if counts["new"] != len(body["new_failures"]):
        return bad("counts.new is not the number of new_failures")
    if counts["new"] < counts["candidate_failed"] - counts["base_failed"]:
        return bad("counts contradict the new failures: candidate_failed exceeds base_failed by more "
                   "than new")
    if not isinstance(body["measured_at"], str) or _SUITE_STAMP.fullmatch(body["measured_at"]) is None:
        return bad("measured_at is not a UTC time of the form YYYY-MM-DDTHH:MM:SSZ")
    return body, None


def judge_suite(receipt: Mapping[str, Any], *, base: str, candidate: str, base_tree: str | None,
                merged_tree: str | None) -> list[tuple[str, str]]:
    """The problems of a well-formed suite receipt against the values recomputed now (pure): it must be
    for this base and this candidate, its `base_tree` must be the base's tree, its `merged_tree` must be
    the tree `git merge-tree` makes of base and candidate now, and every new failure must be named in
    `accepted` (an empty `new_failures` needs nothing)."""
    problems: list[tuple[str, str]] = []
    if receipt["base"] != base:
        problems.append(("SUITE_STALE_BASE", f"the suite was run for base {receipt['base'][:12]}, "
                                              f"not {base[:12]}"))
    if receipt["candidate"] != candidate:
        problems.append(("SUITE_STALE_CANDIDATE", f"the suite was run for candidate "
                                                   f"{receipt['candidate'][:12]}, not {candidate[:12]}"))
    if base_tree is None or receipt["base_tree"] != base_tree:
        problems.append(("SUITE_BASE_TREE_MISMATCH", f"the suite's base tree {receipt['base_tree'][:12]} "
                         f"is not the base's tree {(base_tree or 'unreadable')[:12]}"))
    if merged_tree is None:
        problems.append(("SUITE_MERGED_TREE_MISMATCH", "there is no merge tree now (the merge conflicts "
                                                        "or could not be measured)"))
    elif receipt["merged_tree"] != merged_tree:
        problems.append(("SUITE_MERGED_TREE_MISMATCH", f"the suite was run on merged tree "
                         f"{receipt['merged_tree'][:12]}, the merge makes {merged_tree[:12]}"))
    unaccepted = [n for n in receipt["new_failures"] if n not in set(receipt["accepted"])]
    if unaccepted:
        shown = ", ".join(ascii(n) for n in unaccepted[:5])
        problems.append(("SUITE_NEW_FAILURES", f"{len(unaccepted)} new failure(s) are not accepted: "
                         + shown + (f" and {len(unaccepted) - 5} more" if len(unaccepted) > 5 else "")))
    return problems


def _suite_leg(repo: str, arg: str, base: str, candidate: str, carried_tree: str | None
               ) -> dict[str, Any]:
    """What the gate knows about the suite receipt `arg`: its digest, its content, the trees measured now,
    and the problems (`codes` and `details`). Read the way a report is: resolved, opened without waiting or
    following a link, kind checked on the descriptor, at most `MAX_REPORT_BYTES`."""
    rec: dict[str, Any] = {"arg": arg, "path": None, "sha256": None, "size": None, "receipt": None,
                           "actual_base_tree": None, "actual_merged_tree": None, "details": []}
    try:
        rec["path"] = os.path.realpath(arg)
        raw = _read_file(arg, "suite receipt")
    except (FileNotFoundError, NotADirectoryError):
        rec["details"].append(("SUITE_RECEIPT_MISSING", f"no suite receipt at {arg!r}"))
        return rec
    except (OSError, ValueError) as exc:
        rec["details"].append(("SUITE_RECEIPT_UNREADABLE", f"{arg!r}: {exc}"))
        return rec
    rec["sha256"], rec["size"] = hashlib.sha256(raw).hexdigest(), len(raw)
    body, bad = parse_suite(raw)
    if body is None:
        rec["details"].append(bad)
        return rec
    rec["receipt"] = body
    try:
        tree = _git(repo, "rev-parse", base + "^{tree}").decode().strip()
        rec["actual_base_tree"] = tree if _OBJECT_ID.fullmatch(tree) else None
    except Refused:
        pass
    merged = carried_tree
    if merged is None:
        try:
            code, lines = _merge_tree(repo, base, candidate)
            merged = lines[0] if code == 0 and len(lines) == 1 and _OBJECT_ID.fullmatch(lines[0]) else None
        except Refused:
            merged = None
    rec["actual_merged_tree"] = merged
    rec["details"].extend(judge_suite(body, base=base, candidate=candidate,
                                      base_tree=rec["actual_base_tree"], merged_tree=merged))
    return rec


# --- the decision ---------------------------------------------------------------------------------


def parse_review_args(items: Sequence[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in items:
        rid, sep, path = item.partition("=")
        if not sep or _ID.fullmatch(rid) is None or not path:
            raise Refused(f"--review {item!r} is not ID=FILE with an ID of letters, digits, "
                          f"'.', '_' and '-'")
        pairs.append((rid, path))
    if not pairs:
        raise Refused("at least one --review ID=FILE is required")
    return pairs


def evaluate(repo: str, candidate: str, base: str, spec: str, reviews: Sequence[tuple[str, str]],
             *, min_reviews: int = DEFAULT_MIN_REVIEWS, min_age: float = DEFAULT_MIN_AGE,
             now: float | None = None, suite_receipt: str | None = None) -> dict[str, Any]:
    """The receipt for these inputs (nothing is written). Raises `Refused` when it cannot decide.

    A review that was carried forward stands only with `suite_receipt`, a suite-comparison receipt bound
    to the merge being judged (`judge_suite`): without it, or with one that is stale, damaged or reports
    a new failure nobody accepted, the aggregate is NOT_MERGE_ELIGIBLE with the code of the reason. A
    receipt that is given is judged whether or not a review was carried."""
    if isinstance(min_reviews, bool) or not isinstance(min_reviews, int) or min_reviews < 1:
        raise Refused(f"--min-reviews {min_reviews!r} must be an integer of at least 1")
    if (isinstance(min_age, bool) or not isinstance(min_age, (int, float))
            or not math.isfinite(min_age) or min_age < 0):
        raise Refused(f"--min-age {min_age!r} must be a finite number of seconds, 0 or more")
    now = time.time() if now is None else now
    repo_real = os.path.realpath(repo)
    cand = _resolve(repo_real, "--candidate", candidate)
    basesha = _resolve(repo_real, "--base", base)
    spec_sha = _spec_sha256(repo_real, cand, spec)

    reads = [_read_report(path, now=now, min_age=min_age) for _, path in reviews]
    ids = [rid for rid, _ in reviews]
    for i, row in enumerate(reads):
        keys = {"id": ids[i], "path": row["report"], "inode": row["identity"], "sha": row["sha256"]}
        for j, other in enumerate(reads):
            if i == j:
                continue
            same = (keys["id"] == ids[j] or keys["path"] == other["report"]
                    or (keys["inode"] is not None and keys["inode"] == other["identity"])
                    or (keys["sha"] is not None and keys["sha"] == other["sha256"]))
            if same:
                row["problems"].append(
                    ("COLLISION", f"shares its id, file, inode or content with review {ids[j]!r}"))
                break
    rows, problems = [], []
    carries: dict[str, Carry] = {}
    for (rid, _), row in zip(reviews, reads, strict=True):
        footer = None
        if row["data"] is not None:
            footer, bad = parse_footer(row["data"].decode("utf-8", "replace"))
            if bad is not None:
                row["problems"].append(bad)
            elif footer is not None:
                found = judge_footer(footer, candidate=cand, base=basesha, spec_sha256=spec_sha)
                if [code for code, _ in found] == ["STALE_BASE"] and not row["problems"]:
                    # the review is otherwise valid: the only thing missing is the base, and a
                    # measurement (never the report) may make up for it
                    if footer["BASE"] not in carries:
                        carries[footer["BASE"]] = measure_carry(repo_real, footer["BASE"], basesha, cand)
                    carry = carries[footer["BASE"]]
                    if carry.carried:
                        found, row["carried_forward"] = [], carry.record
                    else:
                        found = [(found[0][0], f"{found[0][1]}; not carried forward: {carry.detail}")]
                row["problems"].extend(found)
        codes = [code for code, _ in row["problems"]]
        problems.extend({"review": rid, "code": code, "detail": detail}
                        for code, detail in row["problems"])
        rows.append({"review": rid, "report": row["report"], "report_sha256": row["sha256"],
                     "size": row["size"], "mtime_ns": row["mtime_ns"], "footer": footer,
                     "status": _status(codes), "problems": codes})
        if row.get("carried_forward") is not None:      # only a carried review has the key
            rows[-1]["carried_forward"] = row["carried_forward"]
    if len(set(ids)) < min_reviews:
        problems.append({"review": None, "code": "TOO_FEW_REVIEWS",
                         "detail": f"{len(set(ids))} required review(s) named, at least "
                                   f"{min_reviews} needed (--min-reviews)"})
    carried = [r["carried_forward"] for r in rows
               if r["status"] == "RELEASE_YES" and "carried_forward" in r]
    suite = None
    if suite_receipt is not None:
        suite = _suite_leg(repo_real, suite_receipt, basesha, cand,
                           carried[0]["merged_tree"] if carried else None)
        problems.extend({"review": None, "code": code, "detail": detail}
                        for code, detail in suite.pop("details"))
        suite["problems"] = [p["code"] for p in problems if p["code"].startswith("SUITE_")]
    elif carried:
        problems.append({"review": None, "code": "SUITE_RECEIPT_MISSING",
                         "detail": f"{len(carried)} review(s) were carried forward from an older base; "
                                   f"they stand only with a suite receipt bound to this merge "
                                   f"(--suite-receipt)"})
    result = {"schema": SCHEMA, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
              "repo": repo_real, "candidate_arg": candidate, "base_arg": base, "candidate": cand,
              "base": basesha, "spec": spec, "spec_sha256": spec_sha, "min_reviews": min_reviews,
              "min_age_seconds": min_age, "required_reviews": ids, "reviews": rows,
              "aggregate": "NOT_MERGE_ELIGIBLE" if problems else "MERGE_ELIGIBLE",
              "problems": problems}
    if suite is not None:                               # only a gate that was given one has the key
        result["suite"] = suite
    return result


def _require_receipt(receipt: object) -> Mapping[str, Any]:
    """The shape `verify` relies on; anything else is not a receipt of this gate."""
    ok = isinstance(receipt, dict) and receipt.get("schema") == SCHEMA
    if ok:
        ok = (receipt.get("aggregate") in AGGREGATES
              and all(isinstance(receipt.get(k), str)
                      for k in ("repo", "candidate_arg", "base_arg", "spec"))
              and isinstance(receipt.get("min_reviews"), int)
              and isinstance(receipt.get("min_age_seconds"), (int, float))
              and isinstance(receipt.get("reviews"), list) and bool(receipt["reviews"])
              and all(isinstance(r, dict) and isinstance(r.get("review"), str)
                      and isinstance(r.get("report"), str)
                      and (r.get("report_sha256") is None or isinstance(r["report_sha256"], str))
                      for r in receipt["reviews"]))
    if ok and "suite" in receipt:
        suite = receipt["suite"]
        ok = (isinstance(suite, dict) and isinstance(suite.get("arg"), str)
              and (suite.get("sha256") is None or isinstance(suite["sha256"], str)))
    if not ok:
        raise Refused(f"not a {SCHEMA} receipt")
    return receipt  # type: ignore[return-value]


def verify(receipt: object, *, repo: str | None = None, candidate: str | None = None,
           base: str | None = None, now: float | None = None) -> dict[str, Any]:
    """Decide the receipt's inputs again, now. `{"verified": bool, "problems": [...], "fresh": ...}`.

    Verified only if the receipt said MERGE_ELIGIBLE, the decision made now over the same arguments
    is MERGE_ELIGIBLE again (a base that moved since over a path the candidate changes, a report
    modified since, a spec that differs, a suite receipt that changed or no longer fits all fail it)
    and every report (and the suite receipt) still has the digest the receipt recorded. `candidate` and
    `base`, when given, are resolved now and must be the pair the receipt is for: the commit about to be
    merged and the tip about to receive it (a base the gate was given as a sha is thereby tied to the
    real tip as well).
    """
    rec = _require_receipt(receipt)
    fresh = evaluate(repo or rec["repo"], rec["candidate_arg"], rec["base_arg"], rec["spec"],
                     [(r["review"], r["report"]) for r in rec["reviews"]],
                     min_reviews=rec["min_reviews"], min_age=rec["min_age_seconds"], now=now,
                     suite_receipt=(rec["suite"]["arg"] if "suite" in rec else None))
    problems = list(fresh["problems"])
    if "suite" in rec and rec["suite"].get("sha256") != fresh["suite"]["sha256"]:
        problems.append({"review": None, "code": "SUITE_RECEIPT_CHANGED",
                         "detail": "the suite receipt differs from the one the gate digested"})
    if rec["aggregate"] != "MERGE_ELIGIBLE":
        problems.append({"review": None, "code": "RECEIPT_NOT_ELIGIBLE",
                         "detail": "the receipt itself says NOT_MERGE_ELIGIBLE"})
    for old, new in zip(rec["reviews"], fresh["reviews"], strict=True):
        if old.get("report_sha256") != new["report_sha256"]:
            problems.append({"review": old["review"], "code": "REPORT_CHANGED",
                             "detail": "the report differs from the one the gate digested"})
    for what, ref, code in (("candidate", candidate, "CANDIDATE_MISMATCH"), ("base", base, "BASE_MISMATCH")):
        if ref is not None:
            got = _resolve(fresh["repo"], f"--{what}", ref)
            if got != rec[what]:
                problems.append({"review": None, "code": code,
                                 "detail": f"the receipt is for {what} {str(rec[what])[:12]}, "
                                           f"not {got[:12]} ({ref!r})"})
    return {"verified": not problems, "problems": problems, "fresh": fresh}


# --- the command ----------------------------------------------------------------------------------


def _lowered(receipt: Mapping[str, Any]) -> list[str]:
    """The line that discloses rules the operator lowered (empty at the defaults)."""
    lowered = []
    if receipt["min_age_seconds"] < DEFAULT_MIN_AGE:
        lowered.append(f"--min-age {receipt['min_age_seconds']:g} (default {DEFAULT_MIN_AGE:g})")
    if receipt["min_reviews"] < DEFAULT_MIN_REVIEWS:
        lowered.append(f"--min-reviews {receipt['min_reviews']} (default {DEFAULT_MIN_REVIEWS})")
    return ["Rules lowered by the operator: " + ", ".join(lowered)] if lowered else []


def _carried_detail(carried: Mapping[str, Any]) -> str:
    """What a carry compared beyond whole files, as one line (empty for a plain carry)."""
    parts = []
    rows = carried.get("register_rows")
    if isinstance(rows, dict):
        parts.append("register rows by id (candidate " + (", ".join(rows["candidate"][:5]) or "none")
                     + "; range " + (", ".join(rows["range"][:5]) or "none") + "; shared none)")
    if carried.get("records_only_overlap_tolerated"):
        parts.append("ledger lines added on both sides, merged by git and all kept: "
                     + ", ".join(carried["records_only_overlap_tolerated"][:5]))
    matrix = carried.get("matrix")
    if isinstance(matrix, dict):
        parts.append(f"matrix of {', '.join(matrix['rows'][:5])} reads the same on both merge trees "
                     f"(digest {matrix['digest'][:12]})")
    imports = carried.get("imports")
    if isinstance(imports, dict):
        parts.append(f"imports followed one level in {imports['candidate_code_files']} candidate and "
                     f"{imports['range_code_files']} range code file(s), none coupled")
    return "; ".join(parts)


def render(receipt: Mapping[str, Any], receipt_arg: str, receipt_sha256: str) -> str:
    """The lines that go into a merge message (text, from the receipt that was written)."""
    parts = []
    for row in receipt["reviews"]:
        counted = row["status"] in ("RELEASE_YES", "RELEASE_NO") and row["footer"]
        parts.append(f"{row['review']} {row['status']}"
                     + (f" ({_count(row['footer']['BLOCKING_OPEN'])} blocking)" if counted else ""))
    lines = [f"Merge gate: {receipt['aggregate']}, receipt {receipt_arg} sha256 {receipt_sha256}",
             "Reviews: " + ", ".join(parts), *_lowered(receipt)]
    for row in receipt["reviews"]:
        carried = row.get("carried_forward")
        if row["status"] == "RELEASE_YES" and isinstance(carried, dict):
            detail = _carried_detail(carried)
            lines.append(f"Carried forward mechanically: {row['review']} was reviewed at base "
                         f"{carried['review_base'][:12]}, the base is {carried['base'][:12]} "
                         f"({len(carried['commits'])} commit(s) between, "
                         + ("overlap compared as listed, " if detail else "no candidate path touched, ")
                         + "merge clean)")
            if detail:
                lines.append(f"  carried over: {detail}")
    suite = receipt.get("suite")
    if isinstance(suite, dict):
        got = suite.get("receipt") or {}
        lines.append(f"Suite receipt: {suite.get('path')} sha256 {suite.get('sha256') or '-'}: "
                     + (", ".join(suite["problems"]) if suite.get("problems") else
                        f"bound to merged tree {got['merged_tree'][:12]}, "
                        f"{len(got['new_failures'])} new failure(s), {len(got['accepted'])} accepted"))
    lines += [f"  {p['review'] or 'gate'}: {p['code']}: {p['detail']}" for p in receipt["problems"]]
    return "\n".join(lines) + "\n"


def _publish(receipt_arg: str, text: str) -> tuple[str, str]:
    """Create the receipt (never replace one), read it back, and return `(digest of what is on disk,
    warning)`. The warning is non-empty only when the file is in place and whole but the directory
    entry may not be durable (`DurableWriteNotSynced`): the decision then stands, and `verify` -- which
    reads the file -- treats the receipt like any other. Every other failure is a `Refused`; one of them
    (the bytes read back differ, or cannot be read) happens after the file exists, and says so."""
    path = Path(receipt_arg)
    warning = ""
    try:
        try:
            durable_create(path, text)
        except DurableWriteNotSynced as exc:
            warning = ("merge-gate warning: the receipt is in place and was read back, but its directory "
                       f"entry may not be durable ({exc.strerror}); run `hoh merge-gate verify` before "
                       "relying on it\n")
    except (OSError, ValueError, RuntimeError) as exc:
        raise Refused(f"--receipt {receipt_arg}: {exc}") from exc
    try:
        on_disk = _read_file(receipt_arg, "receipt")
    except OSError as exc:
        raise Refused(f"--receipt {receipt_arg}: the receipt exists but cannot be read back ({exc}); "
                      f"do not use it, give the next gate a fresh name") from exc
    if on_disk != text.encode("utf-8"):
        raise Refused(f"--receipt {receipt_arg}: the receipt exists but what was read back is not what "
                      f"was written; do not use it, give the next gate a fresh name")
    return hashlib.sha256(on_disk).hexdigest(), warning


def run_check(*, repo: str, candidate: str, base: str, spec: str, reviews: Sequence[str],
              receipt: str, min_reviews: int = DEFAULT_MIN_REVIEWS,
              min_age: float = DEFAULT_MIN_AGE, suite_receipt: str | None = None) -> Outcome:
    """Exit 0: eligible, receipt written. Exit 1: decided, not eligible, receipt written. Exit 2: no
    decision (nothing on stdout; the receipt does not exist, except when it was created and then could
    not be read back as written, which the message says) -- including a receipt that cannot be created
    and any failure of the gate itself, so that 1 never means "the gate crashed"."""
    try:
        result = evaluate(repo, candidate, base, spec, parse_review_args(reviews),
                          min_reviews=min_reviews, min_age=min_age, suite_receipt=suite_receipt)
        digest, warning = _publish(receipt, json.dumps(result, indent=2, sort_keys=True) + "\n")
    except Refused as exc:
        return Outcome(2, "", f"merge-gate refused: {exc}\n")
    except Exception as exc:                      # noqa: BLE001 - a crash must not read as "not eligible"
        return Outcome(2, "", f"merge-gate refused: the gate itself failed: "
                              f"{type(exc).__name__}: {exc}\n")
    return Outcome(0 if result["aggregate"] == "MERGE_ELIGIBLE" else 1,
                   render(result, receipt, digest), warning)


def run_verify(receipt: str, *, repo: str | None = None, candidate: str | None = None,
               base: str | None = None) -> Outcome:
    """Exit 0: verified. Exit 1: not verified (reasons on stdout). Exit 2: not a readable receipt, or
    a `candidate` / `base` that is not a commit."""
    try:
        raw = _read_file(receipt, "receipt")
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise Refused(f"{receipt}: not JSON: {exc}") from exc
        result = verify(body, repo=repo, candidate=candidate, base=base)
    except (OSError, ValueError) as exc:
        return Outcome(2, "", f"merge-gate refused: {receipt}: {exc}\n")
    except Refused as exc:
        return Outcome(2, "", f"merge-gate refused: {exc}\n")
    except Exception as exc:                      # noqa: BLE001
        return Outcome(2, "", f"merge-gate refused: the gate itself failed: "
                              f"{type(exc).__name__}: {exc}\n")
    head = (f"Merge gate verify: {'VERIFIED' if result['verified'] else 'NOT_VERIFIED'}, "
            f"receipt {receipt} sha256 {hashlib.sha256(raw).hexdigest()}\n")
    lines = [*_lowered(body), *(f"  {p['review'] or 'gate'}: {p['code']}: {p['detail']}"
                                for p in result["problems"])]
    return Outcome(0 if result["verified"] else 1, head + "".join(ln + "\n" for ln in lines))
