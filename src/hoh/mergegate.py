"""The merge gate as a product command: may this candidate be merged?

Amendment A6, requirement row P5-36. Until now the gate was an orchestrator script. This module is the
same decision with the slips that script let through closed, as pure functions plus one receipt:

* every required review report ends with a **footer** of six `KEY: value` lines (the five bound keys
  `VERDICT`, `CANDIDATE`, `BASE`, `SPEC_SHA256`, `BLOCKING_OPEN`, and `COMPLETENESS`);
* a merge is eligible only if every required review is present, unique, **complete**, **stable**,
  `RELEASE_YES` with 0 open blockers, and bound to the candidate, base and specification digest that
  are recomputed now. Missing, stale, ambiguous, partial, unstable or colliding means not eligible;
* a report that is partial, or that was modified a moment ago, or that changed while it was read, is
  **no verdict** -- never `RELEASE_YES` and never `RELEASE_NO`;
* the decision is written as a receipt that is created, never replaced; `verify` re-reads it, refuses a
  report that changed after the gate ran, and can tie the receipt to the pair about to be merged.

What it does not do: it merges nothing, writes nothing but the receipt, and decides nothing about the
content of a review. A footer is what a reviewer *declares*; the gate checks that the declaration is
complete, unambiguous, bound to the right objects and not in flux. The receipt is not signed, and no
merge step of the product consults it yet.
"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
import subprocess
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

AGGREGATES = ("MERGE_ELIGIBLE", "NOT_MERGE_ELIGIBLE")
#: A review that speaks for this candidate and says no. Every other problem code makes it no verdict.
_NO_CODES = frozenset({"VERDICT_NO", "BLOCKING_OPEN"})

_GIT_TIMEOUT = 60.0
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_FOOTER_LINE = re.compile(r"([A-Z][A-Z0-9_]*):\s*(\S+)")
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
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


def _git(repo: str, *args: str) -> bytes:
    command = ["git", *_QUIET, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
               "-C", repo, *args]
    try:
        done = subprocess.run(command, capture_output=True, env=_git_env(),
                              stdin=subprocess.DEVNULL, timeout=_GIT_TIMEOUT, check=False)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise Refused(f"git cannot be run in {repo!r}: {exc}") from exc
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
             now: float | None = None) -> dict[str, Any]:
    """The receipt for these inputs (nothing is written). Raises `Refused` when it cannot decide."""
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
    for (rid, _), row in zip(reviews, reads, strict=True):
        footer = None
        if row["data"] is not None:
            footer, bad = parse_footer(row["data"].decode("utf-8", "replace"))
            if bad is not None:
                row["problems"].append(bad)
            elif footer is not None:
                row["problems"].extend(judge_footer(footer, candidate=cand, base=basesha,
                                                    spec_sha256=spec_sha))
        codes = [code for code, _ in row["problems"]]
        problems.extend({"review": rid, "code": code, "detail": detail}
                        for code, detail in row["problems"])
        rows.append({"review": rid, "report": row["report"], "report_sha256": row["sha256"],
                     "size": row["size"], "mtime_ns": row["mtime_ns"], "footer": footer,
                     "status": _status(codes), "problems": codes})
    if len(set(ids)) < min_reviews:
        problems.append({"review": None, "code": "TOO_FEW_REVIEWS",
                         "detail": f"{len(set(ids))} required review(s) named, at least "
                                   f"{min_reviews} needed (--min-reviews)"})
    return {"schema": SCHEMA, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "repo": repo_real, "candidate_arg": candidate, "base_arg": base, "candidate": cand,
            "base": basesha, "spec": spec, "spec_sha256": spec_sha, "min_reviews": min_reviews,
            "min_age_seconds": min_age, "required_reviews": ids, "reviews": rows,
            "aggregate": "NOT_MERGE_ELIGIBLE" if problems else "MERGE_ELIGIBLE",
            "problems": problems}


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
    if not ok:
        raise Refused(f"not a {SCHEMA} receipt")
    return receipt  # type: ignore[return-value]


def verify(receipt: object, *, repo: str | None = None, candidate: str | None = None,
           base: str | None = None, now: float | None = None) -> dict[str, Any]:
    """Decide the receipt's inputs again, now. `{"verified": bool, "problems": [...], "fresh": ...}`.

    Verified only if the receipt said MERGE_ELIGIBLE, the decision made now over the same arguments
    is MERGE_ELIGIBLE again (a base that moved since, a report modified since, a spec that differs
    all fail it) and every report still has the digest the receipt recorded. `candidate` and `base`,
    when given, are resolved now and must be the pair the receipt is for: the commit about to be
    merged and the tip about to receive it (a base the gate was given as a sha is thereby tied to the
    real tip as well).
    """
    rec = _require_receipt(receipt)
    fresh = evaluate(repo or rec["repo"], rec["candidate_arg"], rec["base_arg"], rec["spec"],
                     [(r["review"], r["report"]) for r in rec["reviews"]],
                     min_reviews=rec["min_reviews"], min_age=rec["min_age_seconds"], now=now)
    problems = list(fresh["problems"])
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


def render(receipt: Mapping[str, Any], receipt_arg: str, receipt_sha256: str) -> str:
    """The lines that go into a merge message (text, from the receipt that was written)."""
    parts = []
    for row in receipt["reviews"]:
        counted = row["status"] in ("RELEASE_YES", "RELEASE_NO") and row["footer"]
        parts.append(f"{row['review']} {row['status']}"
                     + (f" ({_count(row['footer']['BLOCKING_OPEN'])} blocking)" if counted else ""))
    lines = [f"Merge gate: {receipt['aggregate']}, receipt {receipt_arg} sha256 {receipt_sha256}",
             "Reviews: " + ", ".join(parts), *_lowered(receipt)]
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
              min_age: float = DEFAULT_MIN_AGE) -> Outcome:
    """Exit 0: eligible, receipt written. Exit 1: decided, not eligible, receipt written. Exit 2: no
    decision (nothing on stdout; the receipt does not exist, except when it was created and then could
    not be read back as written, which the message says) -- including a receipt that cannot be created
    and any failure of the gate itself, so that 1 never means "the gate crashed"."""
    try:
        result = evaluate(repo, candidate, base, spec, parse_review_args(reviews),
                          min_reviews=min_reviews, min_age=min_age)
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
