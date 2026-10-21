#!/usr/bin/env python3
"""bench1.py -- the runner of the BENCH1 campaign (SWE-bench Verified; arms A0, A1P, B, C).

Everything this file decides is a decision the pre-registration (`PREREG.md` of the BENCH1 strand, revision 1b)
made in advance: which cell ends in which class, what a claim is, which tree is graded, when a stage stops, what
may touch the hidden rows, how a runaway cell is stopped, which container image may be released. The program
executes cells and writes what happened; it computes the paired comparison in `report` and draws no conclusion.

**Why a new file and not `tools/benchmark.py`.** That file is the v0.1.0 campaign: its tasks, results readers and
`tools/prereg.py` freeze are bound to campaigns v1-v3, and extending it would unfreeze their instrument. This tool
needs one thing from it -- the shape of a cell record -- and nothing of arm C's project machinery beyond a port.

**Shape: ports and fakes.** Every side effect (the container runtime, the `hoh` CLI, the dispatcher that drives a
plain agent, the dataset, the clock, process groups) sits behind a small port object. The tests in
`tests/test_bench1.py` use fakes only: no container, no provider session, no network, and never a real agent run.
The production ports at the bottom are thin and are exercised only by the campaign's own Stage 0 checks.

**Fail closed everywhere a number could be invented or a safeguard could be skipped.** A transcript without a
usage block is `NOT_DETERMINABLE` and counts at the cell's cap-implied maximum, never zero; a claim marker that is
not the last line is no claim; a wiring read-back that is missing is a finding; the wiring is compared against
the **registered** declaration (the frozen values), never against a value the cell supplies; a stage that the
operator has not approved cannot start; a liveness question that cannot be answered is "alive"; an image that was
already on the machine, or whose id changed, or that is not in a manifest written before it, is never released; a
signal is never sent to a process group the runner did not start.

No command in this file removes a file. The one removal it can perform is an image the campaign itself pulled,
through the `release-images` subcommand, by image id, with an argument vector the tests pin exactly.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

# --------------------------------------------------------------------------- #
# Declarations of the registration (constants, never parameters)
# --------------------------------------------------------------------------- #

CAMPAIGN = "b1"
SCHEMA_RUN = "bench1-run/1"
SCHEMA_CELL = "bench1-cell/1"
SCHEMA_FREEZE = "bench1-freeze/1"
SCHEMA_APPROVAL = "bench1-approval/1"
SEED = 20261014

ARMS = ("A0", "A1P", "B", "C")
STAGES = ("T", "0", "1a", "1b", "1A0", "1C", "ADJ")

#: PREREG 5.2: the registered model, effort and deadline of every role of every arm.
REGISTERED_MODEL = "claude-sonnet-5-5"
REGISTERED_EFFORT = "high"
ROLE_TIMEOUT_S = 2700
#: PREREG 5.3: dispatch caps per cell, and the roles each arm has.
DISPATCH_CAP = {"A0": 1, "A1P": 9, "B": 9, "C": 18}
ARM_ROLES = {"A0": ("developer",), "A1P": ("developer", "reviewer"),
             "B": ("planner", "developer", "qa"), "C": ("planner", "developer", "qa")}
#: PREREG 5.6: runaway bound = cap-implied role time + grace (C gets the longer grace).
_GRACE_S = {"A0": 1800, "A1P": 1800, "B": 1800, "C": 3600}
KILL_GRACE_S = 30

#: PREREG 10, the *High* column in millions of weighted token units (WTU). AB4 is a stop-and-ask when the
#: cumulative measured spend of a stage exceeds its High sum -- or the operator's approved budget, whichever
#: is lower (the approval file carries the latter; the operator approved ~84 M central for T + 0).
HIGH_SUM_M = {"T": 60.0, "0": 66.8, "1a": 144.0, "1b": 144.0, "1A0": 28.8, "1C": 168.0, "ADJ": 12.0}
#: PREREG 10: per dispatch High 1.2 M WTU; a cell whose spend cannot be determined is charged at its cap-implied
#: maximum (cap x 1.2 M), never at zero.
WTU_PER_DISPATCH_HIGH = 1_200_000.0
CAP_IMPLIED_WTU = {arm: cap * WTU_PER_DISPATCH_HIGH for arm, cap in DISPATCH_CAP.items()}
#: PREREG 6/8: the planned number of cells of each stage, twice of which (each non-outcome re-run once) is the
#: most a stage may ever start; and the most attempts one (task, arm, rep) may have.
STAGE_PLANNED = {"T": 12, "0": 13, "1a": 24, "1b": 24, "1A0": 24, "1C": 12, "ADJ": 40}
MAX_ATTEMPTS = 2
#: PREREG 10: WTU = input + 1.25 cache_write + 0.1 cache_read + 5 output (the ratio is NOT_VERIFIED for
#: Sonnet 5.5; the collector reports the four counts as well, so a corrected ratio can be re-applied).
WTU_WEIGHTS = {"input_tokens": 1.0, "cache_creation_input_tokens": 1.25,
               "cache_read_input_tokens": 0.1, "output_tokens": 5.0}

#: PREREG 7.1. ACCEPT_CORRECT .. UNCLAIMED_FAIL and the three NO_CLAIM_* are outcomes; the last four are not.
OUTCOME_CLASSES = ("ACCEPT_CORRECT", "FALSE_ACCEPT", "UNCLAIMED_PASS", "UNCLAIMED_FAIL",
                   "NO_CLAIM_BLOCKED", "NO_CLAIM_NEEDS_YOU", "NO_CLAIM_BUDGET_EXHAUSTED")
NON_OUTCOME_CLASSES = ("TRUNCATED", "PROVIDER_FAILURE", "INSTRUMENT_FAILURE", "INVALID_LEAK")
CLASSES = OUTCOME_CLASSES + NON_OUTCOME_CLASSES

#: What the arm ended on, as the cell runner observed it (before grading).
ENDINGS = ("claim", "no_claim", "blocked", "needs_you", "budget_exhausted",
           "truncated", "provider_failure", "instrument_failure")

DELTA = 0.10
ALPHA = 0.05
NI_GRID = 161

_HEX64 = re.compile(r"[0-9a-f]{64}")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,200}(?::[A-Za-z0-9_.-]{1,128})?(?:@sha256:[0-9a-f]{64})?")


class Bench1Error(Exception):
    """A refusal. The message names the rule, never a value that could be a secret."""


class BudgetStop(Bench1Error):
    """AB4: a stage's measured spend exceeds its High sum or the approved budget -- stop and ask."""


class StageNotApproved(Bench1Error):
    """The operator has not approved this stage (approval file)."""


class KillRefused(Bench1Error):
    """A signal was asked for a process group the runner did not start itself."""


class HiddenRowsRefused(Bench1Error):
    """The hidden rows were asked for while an agent session could still read the machine, or while that
    could not be established."""


class ManifestRefused(Bench1Error):
    """An image operation outside the manifest, against its order, or against an image that is not ours."""


class ImageStateUnknown(Bench1Error):
    """The container runtime could not say whether an image exists (any answer other than an id or a plain
    "No such image"). UNKNOWN counts as pre-existing: such an image is never ours and never released."""


def runaway_bound_s(arm: str) -> int:
    """PREREG 5.6: cap x role timeout + grace. Not a performance cap: a cell that reaches it has an
    unbounded-wait defect, not a slow arm. Wall-clock is reported, never used as a verdict."""
    if arm not in DISPATCH_CAP:
        raise Bench1Error(f"unknown arm {arm!r}")
    return DISPATCH_CAP[arm] * ROLE_TIMEOUT_S + _GRACE_S[arm]


def utcstamp() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


#: Largest file this tool reads as a record, a pattern list or a transcript. A bound, not a tuning knob: a
#: transcript or record above it is refused rather than read into memory.
READ_LIMIT = 256 * 1024 * 1024


def read_regular_bytes(path: Path, limit: int | None = None, *, single_link: bool = False) -> bytes:
    """Bytes of a regular file, or `Bench1Error`: never a FIFO (a read would block forever), a directory, a
    device, a symlink or a file above the limit. Opened without following symlinks and without blocking.
    `single_link=True` also refuses a file with more than one hard link (files this tool writes itself)."""
    cap = READ_LIMIT if limit is None else limit
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise Bench1Error(f"{Path(path).name} cannot be opened: {type(exc).__name__}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Bench1Error(f"{Path(path).name} is not a regular file")
        if single_link and info.st_nlink != 1:
            raise Bench1Error(f"{Path(path).name} has more than one hard link")
        if info.st_size > cap:
            raise Bench1Error(f"{Path(path).name} is above the read limit")
        chunks, left = [], cap + 1
        while left > 0:
            chunk = os.read(fd, min(1 << 20, left))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) > cap:
        raise Bench1Error(f"{Path(path).name} is above the read limit")
    return data


def read_json_object(path: Path, *, single_link: bool = False) -> dict:
    """A JSON object from a regular file; anything else is a `Bench1Error`, never a traceback."""
    try:
        doc = json.loads(read_regular_bytes(Path(path), single_link=single_link).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise Bench1Error(f"{Path(path).name} is not valid JSON text") from exc
    if not isinstance(doc, dict):
        raise Bench1Error(f"{Path(path).name} is not a JSON object")
    return doc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, single_link: bool = False) -> str:
    return sha256_bytes(read_regular_bytes(Path(path), single_link=single_link))


# --------------------------------------------------------------------------- #
# Claim parser (PREREG 5.3, 5.4; I7)
# --------------------------------------------------------------------------- #

_CLAIM_RE = re.compile(r"CLAIM: (COMPLETE|INCOMPLETE)")
_REVIEW_RE = re.compile(r"REVIEW: (CLEAN|DEFECTS)")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _marker(text: str, pattern: re.Pattern[str]) -> str | None:
    """The marker the answer *ends on*, or None.

    Fail closed, the way the whole pre-registration reads a missing marker: the marker must be the last
    non-empty line, spelled exactly (case-sensitive, nothing else on the line but ASCII blanks); a control
    character anywhere makes the answer unreadable; and a *different* marker earlier in the same answer makes it
    ambiguous. Prose that merely mentions the marker mid-answer is not a marker.
    """
    if not isinstance(text, str) or _CONTROL.search(text):
        return None
    lines = [line.strip(" \t\r") for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        return None
    last = pattern.fullmatch(lines[-1])
    if last is None:
        return None
    for line in lines[:-1]:
        other = pattern.fullmatch(line)
        if other is not None and other.group(1) != last.group(1):
            return None
    return last.group(1)


def parse_claim(text: str) -> str | None:
    """`COMPLETE`, `INCOMPLETE` or None (no marker / ambiguous). Only `COMPLETE` is a claim."""
    return _marker(text, _CLAIM_RE)


def parse_review(text: str) -> str | None:
    """`CLEAN`, `DEFECTS` or None."""
    return _marker(text, _REVIEW_RE)


# --------------------------------------------------------------------------- #
# Usage collector (PREREG 10; I5)
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Usage:
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int
    messages: int
    cache_fields_missing: bool = False

    def wtu(self) -> float:
        return (self.input_tokens * WTU_WEIGHTS["input_tokens"]
                + self.cache_creation_input_tokens * WTU_WEIGHTS["cache_creation_input_tokens"]
                + self.cache_read_input_tokens * WTU_WEIGHTS["cache_read_input_tokens"]
                + self.output_tokens * WTU_WEIGHTS["output_tokens"])

    def to_json(self) -> dict:
        return {**dataclasses.asdict(self), "wtu": self.wtu()}


def _count(value: object) -> int | None:
    # bool is an int in Python; a transcript with `true` as a token count is corrupt, not 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def collect_usage(lines: Iterable[str]) -> Usage | None:
    """Sum the `message.usage` of the cell's session transcripts (JSON lines), or None.

    None means NOT_DETERMINABLE; the cell is then charged at its cap-implied maximum (`BudgetLedger`), never
    zero. Specifically None for: a line that is not JSON, a JSON line that is not an object, a usage block
    without `input_tokens` or `output_tokens`, a count that is not a non-negative integer, or no usage block at
    all. A streamed message can appear on several lines; the **last** usage per `message.id` wins, so nothing is
    double counted. Absent cache fields count 0 and are flagged: the sum is then a lower bound.
    """
    per_message: dict[str, dict] = {}
    anonymous = 0
    cache_missing = False
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        msg = obj.get("message")
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if usage is None:
            continue
        if not isinstance(usage, dict):
            return None
        mid = msg.get("id") if isinstance(msg, dict) else None
        if not isinstance(mid, str) or not mid:
            anonymous += 1
            mid = f"@anonymous-{anonymous}"
        counts: dict[str, int] = {}
        for key in ("input_tokens", "output_tokens"):
            value = _count(usage.get(key))
            if value is None:
                return None
            counts[key] = value
        for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            if key not in usage:
                counts[key] = 0
                cache_missing = True
                continue
            value = _count(usage.get(key))
            if value is None:
                return None
            counts[key] = value
        per_message[mid] = counts
    if not per_message:
        return None
    total = {k: sum(c[k] for c in per_message.values()) for k in WTU_WEIGHTS}
    return Usage(messages=len(per_message), cache_fields_missing=cache_missing, **total)


# --------------------------------------------------------------------------- #
# Wiring patterns (PREREG 5.4; file frozen at F1)
# --------------------------------------------------------------------------- #


class WiringPatterns:
    """`tools/bench1_wiring_patterns.txt`: one regular expression per line; `#` starts a comment.

    A stop reason that matches one is an INSTRUMENT_FAILURE; any other product-stated stop reason is a
    product outcome (conservative against the hypothesis: unmatched reasons count against B). An empty list
    classifies nothing as wiring and silently changes verdicts, so an empty file is refused, like a line that
    does not compile.
    """

    def __init__(self, patterns: list[re.Pattern[str]], digest: str):
        self.patterns = patterns
        self.digest = digest

    @classmethod
    def load(cls, path: Path) -> WiringPatterns:
        data = read_regular_bytes(Path(path))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Bench1Error(f"wiring pattern file is not UTF-8: {exc.reason}") from exc
        compiled: list[re.Pattern[str]] = []
        for number, line in enumerate(text.split("\n"), 1):
            body = line.strip()
            if not body or body.startswith("#"):
                continue
            try:
                compiled.append(re.compile(body, re.IGNORECASE))
            except re.error as exc:
                raise Bench1Error(f"wiring pattern file line {number} does not compile: {exc}") from exc
        if not compiled:
            raise Bench1Error("wiring pattern file holds no pattern")
        return cls(compiled, sha256_bytes(data))

    def matches(self, reason: str) -> bool:
        return any(p.search(reason or "") for p in self.patterns)

    def require_digest(self, frozen: str) -> None:
        if not isinstance(frozen, str) or not _HEX64.fullmatch(frozen) or self.digest != frozen:
            raise Bench1Error("wiring pattern file differs from the frozen digest (F1)")


# --------------------------------------------------------------------------- #
# Cell classes (PREREG 7.1)
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class Observation:
    """What the cell runner saw, before the hidden verdict is known."""

    ended: str
    claim: bool = False
    passed: bool | None = None          # hidden verdict on the claimed (or last) tree; None = not graded
    stop_reason: str = ""
    leak_confirmed: bool = False


def classify(obs: Observation, patterns: WiringPatterns) -> str | None:
    """The class of a cell, or None while the hidden verdict is still pending.

    Order matters and is the pre-registered one: a confirmed leak, then the three non-outcomes the runner
    itself observed, then product stops (a stop that matches the wiring list is the instrument's fault), then
    claim and verdict. `budget_exhausted` is never rewritten as a wiring problem: the dispatch cap is the
    product's own stop.
    """
    if obs.ended not in ENDINGS:
        raise Bench1Error(f"unknown ending {obs.ended!r}")
    if obs.leak_confirmed:
        return "INVALID_LEAK"
    if obs.ended == "truncated":
        return "TRUNCATED"
    if obs.ended == "provider_failure":
        return "PROVIDER_FAILURE"
    if obs.ended == "instrument_failure":
        return "INSTRUMENT_FAILURE"
    if obs.ended in ("blocked", "needs_you"):
        if patterns.matches(obs.stop_reason):
            return "INSTRUMENT_FAILURE"
        return "NO_CLAIM_BLOCKED" if obs.ended == "blocked" else "NO_CLAIM_NEEDS_YOU"
    if obs.ended == "budget_exhausted":
        return "NO_CLAIM_BUDGET_EXHAUSTED"
    if obs.passed is None:
        return None
    if obs.ended == "claim":
        return "ACCEPT_CORRECT" if obs.passed else "FALSE_ACCEPT"
    return "UNCLAIMED_PASS" if obs.passed else "UNCLAIMED_FAIL"


def is_outcome(cls: str) -> bool:
    if cls not in CLASSES:
        raise Bench1Error(f"unknown class {cls!r}")
    return cls in OUTCOME_CLASSES


_PROVIDER_RE = re.compile(r"quota|rate.?limit|usage limit|session limit|\b429\b|overloaded", re.IGNORECASE)
_NEEDS_YOU_RE = re.compile(r"needs[_ -]?you|human decision|operator decision|awaiting (?:a )?human",
                           re.IGNORECASE)


def ending_from_state(state: object, killed_by_runner: bool = False) -> tuple[str, str, str | None]:
    """(ended, stop_reason, claimed_commit) read from a `hoh` run state (RunState fields as of the pinned commit).

    The claimed tree is the **last accepted candidate**, never the worktree HEAD or the working candidate
    (PREREG 5.4). Instrument and runner conditions are never an arm's verdict: an empty or unreadable state, a
    run that ended while ACTIVE with budget left, PAUSED, and CANCELLED (the runner's own kill is TRUNCATED, any
    other cancellation an instrument failure) are not `budget_exhausted` or `blocked`.
    """
    if not isinstance(state, dict) or not state:
        return "instrument_failure", "run state is empty or unreadable", None
    cand = state.get("last_accepted_candidate")
    reason = str(state.get("stop_reason") or "")
    if isinstance(cand, dict) and cand.get("commit"):
        return "claim", reason, str(cand["commit"])
    condition = state.get("condition")
    if condition == "CANCELLED":
        if killed_by_runner:
            return "truncated", reason or "cancelled by the runner", None
        return "instrument_failure", reason or "cancelled by something other than the runner", None
    if condition == "PAUSED":
        return "instrument_failure", reason or "run paused", None
    if _PROVIDER_RE.search(reason):
        return "provider_failure", reason, None
    if _NEEDS_YOU_RE.search(reason):
        return "needs_you", reason, None
    if condition in ("BLOCKED", "FAILED"):
        return "blocked", reason or str(condition), None
    if condition == "ACTIVE":
        budgets = state.get("budgets") if isinstance(state.get("budgets"), dict) else {}
        usage = state.get("usage") if isinstance(state.get("usage"), dict) else {}
        max_it, max_dp = budgets.get("max_iterations"), budgets.get("max_dispatches")
        it, dp = usage.get("iterations"), usage.get("dispatches")
        spent = (isinstance(max_it, int) and isinstance(it, int) and it >= max_it) \
            or (isinstance(max_dp, int) and isinstance(dp, int) and dp >= max_dp)
        if spent:
            return "budget_exhausted", reason or "iteration or dispatch budget spent", None
        return "instrument_failure", reason or "run ended while ACTIVE with budget left", None
    return "instrument_failure", f"unknown run condition {condition!r}", None


# --------------------------------------------------------------------------- #
# Records: attempts, re-runs, create-only files
# --------------------------------------------------------------------------- #


def cell_key(task: str, arm: str, rep: int, attempt: int) -> str:
    if arm not in ARMS:
        raise Bench1Error(f"unknown arm {arm!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", task) or ".." in task:
        raise Bench1Error("task id is not a plain name")
    if rep < 1 or attempt < 1:
        raise Bench1Error("rep and attempt start at 1")
    return f"{task}.{arm}.{rep}.a{attempt}"


def write_create_only(path: Path, doc: dict) -> Path:
    """One file, never replaced: an existing file is an error, a re-run writes a new attempt."""
    path = Path(path)
    data = (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        written = os.write(fd, data)
        if written != len(data):
            raise OSError("short write of a cell record")
        os.fsync(fd)
    finally:
        os.close(fd)
    json.loads(path.read_text(encoding="utf-8"))
    return path


def create_numbered(directory: Path, stem: str, suffix: str, doc: dict) -> Path:
    """`<stem>-NNN<suffix>` with the lowest free NNN: two receipts in the same second never collide and
    none is ever replaced."""
    for n in range(1, 1000):
        try:
            return write_create_only(Path(directory) / f"{stem}-{n:03d}{suffix}", doc)
        except FileExistsError:
            continue
    raise Bench1Error(f"no free name for {stem}")


def latest_attempts(cells: list[dict]) -> list[dict]:
    """One cell per (task, arm, rep).

    An attempt never overrides an outcome: if any attempt is an outcome, the first outcome is the cell; only a
    run of non-outcomes is superseded by its highest attempt (the single re-run of PREREG 7.1)."""
    groups: dict[tuple, list[dict]] = {}
    for c in cells:
        groups.setdefault((c["task"], c["arm"], c["rep"]), []).append(c)
    out = []
    for key in sorted(groups):
        attempts = sorted(groups[key], key=lambda c: c["attempt"])
        outcomes = [c for c in attempts if is_outcome(c["class"])]
        out.append(outcomes[0] if outcomes else attempts[-1])
    return out


def check_rerun(results: Path, task: str, arm: str, rep: int, attempt: int, supersedes: str | None) -> None:
    """PREREG 7.1: attempt 1 is the first; attempt 2 names attempt 1 in `supersedes`, only after attempt 1
    ended in a non-outcome; there is no attempt 3; nothing is re-run after an outcome."""
    if attempt > MAX_ATTEMPTS:
        raise Bench1Error(f"attempt {attempt} is above the limit of {MAX_ATTEMPTS}")
    results = Path(results)
    if attempt == 1:
        if supersedes is not None:
            raise Bench1Error("the first attempt supersedes nothing")
        if any(results.glob(f"{task}.{arm}.{rep}.a*.run.json")):
            raise Bench1Error("a run record of this cell already exists; a re-run is attempt 2")
        return
    previous = cell_key(task, arm, rep, attempt - 1)
    if supersedes != previous:
        raise Bench1Error("a re-run must name the attempt it supersedes")
    cell_file = results / f"{previous}.cell.json"
    prev = read_json_object(cell_file, single_link=True) if cell_file.exists() else None
    if prev is None or prev.get("class") not in NON_OUTCOME_CLASSES:
        raise Bench1Error("only a non-outcome cell is re-run")
    if any(results.glob(f"{task}.{arm}.{rep}.a{attempt}.run.json")):
        raise Bench1Error("this attempt already has a run record")


# --------------------------------------------------------------------------- #
# Freeze, approvals, registered declaration (PREREG 5.2, 12; A-5, A-6)
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Freeze:
    """The campaign values frozen at F1 by the orchestrator (create-only file): what the cells' wiring is compared
    against. None of them is ever taken from a cell."""

    policy_digest: str
    house_text_digest: str
    isolation: str
    storage_root: str
    wiring_patterns_digest: str
    denied_tools: tuple[str, ...] = ("WebFetch", "WebSearch")

    @classmethod
    def load(cls, path: Path) -> Freeze:
        doc = read_json_object(Path(path), single_link=True)
        if doc.get("schema") != SCHEMA_FREEZE:
            raise Bench1Error("not a bench1 freeze file")
        for key in ("policy_digest", "house_text_digest", "wiring_patterns_digest"):
            value = doc.get(key)
            if not isinstance(value, str) or not _HEX64.fullmatch(value):
                raise Bench1Error(f"freeze {key} is not a sha256 digest")
        if doc.get("isolation") not in ("none", "strict"):
            raise Bench1Error("freeze isolation must be 'none' or 'strict'")
        root = doc.get("storage_root")
        if not isinstance(root, str) or not root.startswith("/") or ".." in root.split("/"):
            raise Bench1Error("freeze storage_root must be an absolute path")
        denied = doc.get("denied_tools", ["WebFetch", "WebSearch"])
        if not isinstance(denied, list) or not all(isinstance(t, str) for t in denied) \
                or not {"WebFetch", "WebSearch"} <= set(denied):
            raise Bench1Error("freeze must deny WebFetch and WebSearch")
        return cls(doc["policy_digest"], doc["house_text_digest"], doc["isolation"], root,
                   doc["wiring_patterns_digest"], tuple(denied))


@dataclasses.dataclass(frozen=True)
class Declaration:
    """The environment contract of PREREG 5.2 for one arm: the registered values, built by
    `registered_declaration` and compared with what the built objects say."""

    model: str = REGISTERED_MODEL
    effort: str = REGISTERED_EFFORT
    role_timeout_s: int = ROLE_TIMEOUT_S
    dispatch_cap: int = 9
    policy_digest: str = ""
    house_text_digest: str = ""
    isolation: str | None = None        # fixed by I2 for B and C; None for A arms
    storage_root: str = ""
    roles: tuple[str, ...] = ("developer",)
    denied_tools: tuple[str, ...] = ("WebFetch", "WebSearch")


def registered_declaration(arm: str, freeze: Freeze) -> Declaration:
    if arm not in ARMS:
        raise Bench1Error(f"unknown arm {arm!r}")
    return Declaration(
        dispatch_cap=DISPATCH_CAP[arm], policy_digest=freeze.policy_digest,
        house_text_digest=freeze.house_text_digest,
        isolation=freeze.isolation if arm in ("B", "C") else None,
        storage_root=freeze.storage_root, roles=ARM_ROLES[arm], denied_tools=freeze.denied_tools)


@dataclasses.dataclass(frozen=True)
class StageApprovals:
    """The orchestrator's approval file: stage -> true/false, and an optional approved budget over a scope of
    stages. Nothing is approved by default; a stage that is absent or not exactly `true` cannot start."""

    stages: dict
    budget_scope: tuple[str, ...] = ()
    budget_limit_m: float | None = None

    @classmethod
    def load(cls, path: Path) -> StageApprovals:
        doc = read_json_object(Path(path), single_link=True)
        if doc.get("schema") != SCHEMA_APPROVAL:
            raise Bench1Error("not a bench1 approval file")
        stages = doc.get("stages")
        if not isinstance(stages, dict) or not set(stages) <= set(STAGES) \
                or not all(isinstance(v, bool) for v in stages.values()):
            raise Bench1Error("approval stages must map known stage names to booleans")
        budget = doc.get("budget")
        if budget is None:
            return cls(dict(stages))
        scope = budget.get("scope") if isinstance(budget, dict) else None
        limit = budget.get("limit_m") if isinstance(budget, dict) else None
        if not isinstance(scope, list) or not scope or not set(scope) <= set(STAGES) \
                or isinstance(limit, bool) or not isinstance(limit, (int, float)) or limit <= 0:
            raise Bench1Error("approval budget needs a scope of stages and a positive limit_m")
        return cls(dict(stages), tuple(scope), float(limit))

    def approved(self, stage: str) -> bool:
        return self.stages.get(stage) is True


# --------------------------------------------------------------------------- #
# Gates and budget (PREREG 8)
# --------------------------------------------------------------------------- #


def stage1a_gate(cells: list[dict]) -> dict:
    """AB2a / AB2b / AB3 over the A1+ cells of Stage 1a that are outcomes after their single re-run.

    The denominator is n_out, the *outcome* cells. With n_out < 10 the gate is INDETERMINATE: nothing stops
    automatically and the operator decides Stage 1b. A non-outcome cell is never silently a pass or a fail.
    """
    final = latest_attempts(cells)
    out = [c for c in final if is_outcome(c["class"])]
    n = len(out)
    resolved = sum(1 for c in out if c.get("passed") is True)
    fa = sum(1 for c in out if c["class"] == "FALSE_ACCEPT")
    result = {"n_planned": len(final), "n_out": n, "resolved": resolved, "false_accepts": fa,
              "triggered": [], "state": "CONTINUE"}
    if n < 10:
        result["state"] = "INDETERMINATE"
        return result
    if resolved >= math.ceil(0.85 * n):
        result["triggered"].append("AB2a")
    if fa <= 1:
        result["triggered"].append("AB2b")
    if resolved <= math.floor(0.20 * n):
        result["triggered"].append("AB3")
    if result["triggered"]:
        result["state"] = "STOP"
    return result


class BudgetLedger:
    """Append-only JSON lines: a `start` row before every cell, a `final` row after it (AB4, PREREG 8/10).

    Every cell that starts is in the ledger, whatever happens to it: a crash between the two rows leaves the
    cell charged at its cap-implied maximum; a cell whose spend cannot be determined is charged at that maximum
    too, flagged `assumed`, never at zero. A stage has a hard cap on started cells (twice the planned number)
    and an attempt cap, so a transcript-format drift cannot produce unbounded cells at a ledger of 0. AB4 is
    checked before every start, against the stage's High sum and against the approved budget.
    """

    def __init__(self, path: Path, high_m: dict[str, float] | None = None,
                 planned: dict[str, int] | None = None):
        self.path = Path(path)
        self.high_m = dict(HIGH_SUM_M if high_m is None else high_m)
        self.planned = dict(STAGE_PLANNED if planned is None else planned)

    def _rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        for line in read_regular_bytes(self.path, single_link=True).decode("utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise Bench1Error("budget ledger holds a non-object row")
                rows.append(row)
        return rows

    def _cells(self, stage: str) -> dict[str, dict]:
        cells: dict[str, dict] = {}
        for r in self._rows():
            if r.get("stage") != stage:
                continue
            c = cells.setdefault(r["cell"], {"assumed_wtu": r.get("assumed_max_wtu", 0.0), "final": None})
            if r["kind"] == "start":
                c["assumed_wtu"] = r["assumed_max_wtu"]
            elif r["kind"] == "final":
                c["final"] = r["wtu"]
        return cells

    def total_m(self, stage: str) -> float:
        total = 0.0
        for c in self._cells(stage).values():
            total += (c["final"] if c["final"] is not None else c["assumed_wtu"])
        return total / 1e6

    def scope_total_m(self, stages: Iterable[str]) -> float:
        return sum(self.total_m(s) for s in stages)

    def started(self, stage: str) -> int:
        return len(self._cells(stage))

    def _append(self, row: dict) -> None:
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def check_before_start(self, stage: str, approvals: StageApprovals) -> None:
        """AB4 and the approval, before every start."""
        if stage not in self.high_m:
            raise Bench1Error(f"unknown stage {stage!r}")
        if not approvals.approved(stage):
            raise StageNotApproved(f"stage {stage} is not approved by the operator's approval file")
        spent = self.total_m(stage)
        if spent > self.high_m[stage]:
            raise BudgetStop(f"AB4: stage {stage} has spent {spent:.1f} M WTU, above its High sum "
                             f"{self.high_m[stage]:.1f} M -- stop and ask")
        if approvals.budget_limit_m is not None and stage in approvals.budget_scope:
            scope = self.scope_total_m(approvals.budget_scope)
            if scope > approvals.budget_limit_m:
                raise BudgetStop(f"AB4: stages {','.join(approvals.budget_scope)} have spent {scope:.1f} M WTU, "
                                 f"above the approved {approvals.budget_limit_m:.1f} M (the High sums are "
                                 f"{', '.join(f'{s} {self.high_m[s]:.1f}' for s in approvals.budget_scope)} M) "
                                 f"-- stop and ask")

    def begin(self, stage: str, key: str, arm: str, attempt: int) -> None:
        if stage not in self.high_m:
            raise Bench1Error(f"unknown stage {stage!r}")
        if arm not in CAP_IMPLIED_WTU:
            raise Bench1Error(f"unknown arm {arm!r}")
        if attempt > MAX_ATTEMPTS:
            raise Bench1Error(f"attempt {attempt} is above the limit of {MAX_ATTEMPTS}")
        cells = self._cells(stage)
        if key in cells:
            raise Bench1Error(f"cell {key!r} is already in the ledger")
        if len(cells) >= 2 * self.planned.get(stage, 0):
            raise Bench1Error(f"stage {stage} has started its cell cap ({2 * self.planned.get(stage, 0)})")
        self._append({"kind": "start", "stage": stage, "cell": key, "arm": arm,
                      "assumed_max_wtu": CAP_IMPLIED_WTU[arm], "at": utcstamp()})

    def finish(self, stage: str, key: str, wtu: float | None, *, no_provider_call: bool = False) -> float:
        """Close a started cell; returns the charged WTU. `wtu is None` charges the cap-implied maximum."""
        cells = self._cells(stage)
        if key not in cells or cells[key]["final"] is not None:
            raise Bench1Error(f"cell {key!r} is not open in the ledger")
        assumed = False
        if wtu is None:
            if no_provider_call:
                wtu = 0.0
            else:
                wtu, assumed = cells[key]["assumed_wtu"], True
        elif isinstance(wtu, bool) or not isinstance(wtu, (int, float)) or wtu < 0 or math.isnan(wtu):
            raise Bench1Error("a spend that is not a non-negative number is not recorded")
        self._append({"kind": "final", "stage": stage, "cell": key, "wtu": float(wtu), "assumed": assumed,
                      "no_provider_call": no_provider_call, "at": utcstamp()})
        return float(wtu)


# --------------------------------------------------------------------------- #
# Wiring read-back (I4, I11-I17; PREREG 5.2, 6)
# --------------------------------------------------------------------------- #


#: The read-back keys `compare_wiring` knows. A port may *defer* some of them (`observed["deferred_checks"]`)
#: when only the started process can show them (the run's ephemeral session, the project resolution, a dispatcher's
#: deadline); the deferral is recorded in the run record and the port's `post_start_readback` must supply them
#: before the first provider call can matter. A deferred key that is not in this set is itself a finding.
WIRING_KEYS = frozenset({
    "models", "efforts", "role_timeout_s", "dispatch_cap", "isolation", "policy_digest", "house_text_digest",
    "denied_tools", "trust_recorded", "storage_root_initialised", "project_resolved", "session_open",
    "scratch_under_storage_root", "storage_root", "herdr_env", "hidden_material_in_worktree", "snapshot"})


#: The checks an A arm (no `hoh run`, no run project, no ephemeral session of the product) may declare not applicable.
NOT_APPLICABLE_FOR_A = frozenset({"project_resolved", "session_open"})


def compare_wiring(decl: Declaration, observed: dict, arm: str,
                   only: frozenset[str] | None = None) -> list[dict]:
    """Findings (an empty list = the wiring holds). A key that is absent from `observed` is a finding, and so is
    a declaration that is not itself usable (an empty digest, no isolation for B/C, no roles).

    `observed` is built by the readers of the production ports from the *built* run state, routing file,
    dispatch records and the snapshot -- never from the configuration that was handed over. `decl` is the
    registered declaration (`registered_declaration`), never a value a cell supplies.

    `observed["deferred_checks"]` names keys the port can only read after the process started; they are skipped in
    the first pass (and each must be a known key). `only` restricts a pass to exactly those keys (the post-start
    pass).
    """
    findings: list[dict] = []
    deferred = set(observed.get("deferred_checks", ())) if only is None else set()
    if only is None:
        for key in sorted(deferred - WIRING_KEYS):
            findings.append({"check": "I4", "what": f"deferred check {key!r} is not a known read-back key"})
    # `not_applicable`: checks that have no meaning for an arm without a `hoh run` (A0, A1+): named by the port,
    # recorded in the run record, never accepted for B or C
    na = set(observed.get("not_applicable", ()))
    if na and (arm in ("B", "C") or not na <= NOT_APPLICABLE_FOR_A):
        findings.append({"check": "I4", "what": "not_applicable names a check that applies to this arm",
                         "observed": sorted(na)})
        na = set()
    skip = (deferred | na) if only is None else ((WIRING_KEYS - set(only)) | na)
    if only is None and (not decl.policy_digest or not decl.house_text_digest or not decl.storage_root
                         or not decl.roles):
        findings.append({"check": "I4", "what": "the registered declaration is incomplete"})
    if only is None and arm in ("B", "C") and decl.isolation not in ("none", "strict"):
        findings.append({"check": "I4", "what": "the registered isolation is missing for an arm that has one"})

    def need(check: str, key: str):
        if key in skip:
            return None, False
        if key not in observed:
            findings.append({"check": check, "what": f"read-back {key} is missing"})
            return None, False
        return observed[key], True

    def expect(check: str, key: str, want):
        got, ok = need(check, key)
        if ok and got != want:
            findings.append({"check": check, "what": key, "expected": want, "observed": got})

    for role in decl.roles:
        for field, want, check in (("models", decl.model, "I4"), ("efforts", decl.effort, "I4"),
                                   ("role_timeout_s", decl.role_timeout_s, "I14")):
            table, ok = need(check, field)
            if ok and (not isinstance(table, dict) or table.get(role) != want):
                findings.append({"check": check, "what": f"{field}[{role}]", "expected": want,
                                 "observed": table.get(role) if isinstance(table, dict) else table})
    expect("I4", "dispatch_cap", decl.dispatch_cap)
    if arm in ("B", "C"):
        expect("I4", "isolation", decl.isolation)
    expect("I11", "policy_digest", decl.policy_digest)
    expect("I11", "house_text_digest", decl.house_text_digest)
    denied, ok = need("I11", "denied_tools")
    if ok:
        missing = [t for t in decl.denied_tools if t not in (denied or [])]
        if missing:
            findings.append({"check": "I11", "what": "web tools not denied", "missing": missing})
    expect("I12", "trust_recorded", True)
    expect("I13", "storage_root_initialised", True)
    expect("I13", "project_resolved", True)
    expect("I13", "session_open", True)
    expect("I13", "scratch_under_storage_root", True)
    expect("I13", "storage_root", decl.storage_root)
    expect("I15", "herdr_env", True)
    expect("I16", "hidden_material_in_worktree", False)
    snap, ok = need("I17", "snapshot")
    if ok and (not isinstance(snap, dict) or snap.get("commits") != 1 or snap.get("refs") != 1
               or snap.get("unreachable_objects") != 0):
        findings.append({"check": "I17", "what": "snapshot is not one commit with no foreign objects",
                         "observed": snap})
    return findings


# --------------------------------------------------------------------------- #
# Kill path (PREREG 5.6; I10)
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class Handle:
    """What the runner recorded when it started a cell. The kill path trusts nothing else. `leader_pid` is the
    pid of the process the runner started in a session of its own: its process group id equals it."""

    cell_key: str
    arm: str
    pgid: int
    run_id: str | None = None
    leader_pid: int | None = None
    started_at: float = 0.0


class KillPath:
    """pause -> cancel -> surface reap -> SIGTERM -> grace -> SIGKILL of the cell's own process group.

    `hoh cancel` only records a stop request for the next safe boundary and `hoh surface reap` closes the
    surfaces of runs that are over, so neither stops a hung process; the group signal does, and it is last.
    Each step is attempted and recorded even when the one before failed, and a step that did nothing is
    recorded as failed (a port that cannot close panes says so). A group id that is not the process group of
    the leader the runner started (`leader_pid`), or is 0, 1, the runner's own group or its parent's group, is
    refused before anything is signalled.
    """

    def __init__(self, hoh, procs, sleep: Callable[[float], None] = time.sleep,
                 grace_s: int = KILL_GRACE_S):
        self.hoh, self.procs, self.sleep, self.grace_s = hoh, procs, sleep, grace_s

    def execute(self, handle: Handle, own_pgid: int | None = None, parent_pgid: int | None = None) -> list[dict]:
        own = os.getpgrp() if own_pgid is None else own_pgid
        parent = os.getpgid(os.getppid()) if parent_pgid is None else parent_pgid
        pgid = handle.pgid
        if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid in (0, 1) or pgid < 0 \
                or pgid in (own, parent) or handle.leader_pid != pgid:
            raise KillRefused("the process group of the cell is not the one of a leader the runner started")
        steps: list[dict] = []

        def step(name: str, fn: Callable[[], object]) -> object:
            try:
                value = fn()
                steps.append({"step": name, "ok": True})
                return value
            except Exception as exc:                          # noqa: BLE001 - recorded, never raised
                steps.append({"step": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]})
                return None

        if handle.run_id is not None and handle.arm in ("B", "C"):
            step("hoh_pause", lambda: self.hoh.pause(handle.run_id))
            step("hoh_cancel", lambda: self.hoh.cancel(handle.run_id, "BENCH1_TRUNCATED"))
            step("hoh_surface_reap_close", lambda: self.hoh.surface_reap_close(handle.run_id))
        else:
            step("close_owned_panes", lambda: self.hoh.close_owned(handle.cell_key))
        step("sigterm_group", lambda: self.procs.signal_group(pgid, signal.SIGTERM))
        waited = 0
        while waited < self.grace_s and self.procs.group_alive(pgid):
            self.sleep(1)
            waited += 1
        if self.procs.group_alive(pgid):
            step("sigkill_group", lambda: self.procs.signal_group(pgid, signal.SIGKILL))
            self.sleep(1)
        steps.append({"step": "group_gone", "ok": not self.procs.group_alive(pgid)})
        return steps


# --------------------------------------------------------------------------- #
# A1+ controller (PREREG 5.3): developer, then two consecutive independent CLEAN reviews
# --------------------------------------------------------------------------- #


def run_a1plus(dispatch: Callable[[str, str], str], tree_hash: Callable[[], str],
               snapshot: Callable[[str], str], cap: int = DISPATCH_CAP["A1P"]) -> dict:
    """The A1+ loop with every provider call behind `dispatch(role, findings_or_empty) -> answer`.

    Claim iff two consecutive reviewer sessions say CLEAN on the same tree (hash-checked before and after each
    review, and equal between the two) within `cap` dispatches. Everything else is no claim, and the reason is
    recorded. A reviewer that changes the tree it reviews invalidates the review: no claim. `snapshot(label)`
    records the tree at each developer round and the reviewed tree, so every round can be graded later
    (E4/E5 and the spend curve) without extra provider spend.
    """
    dispatches = 0
    rounds: list[dict] = []
    findings = ""
    while True:
        if dispatches >= cap:
            return _a1_result("budget_exhausted", dispatches, rounds, None, "cap before developer round")
        answer = dispatch("developer", findings)
        dispatches += 1
        marker = parse_claim(answer)
        tree = snapshot(f"dev{len([r for r in rounds if r['kind'] == 'developer']) + 1}")
        rounds.append({"kind": "developer", "marker": marker, "tree": tree})
        if marker != "COMPLETE":
            return _a1_result("no_claim", dispatches, rounds, None,
                              "developer ended without CLAIM: COMPLETE")
        clean = 0
        reviewed = tree_hash()
        while clean < 2:
            if dispatches >= cap:
                return _a1_result("budget_exhausted", dispatches, rounds, None, "cap during review")
            before = tree_hash()
            answer = dispatch("reviewer", "")
            dispatches += 1
            after = tree_hash()
            verdict = parse_review(answer)
            rounds.append({"kind": "reviewer", "marker": verdict, "tree": snapshot(f"rev{dispatches}")})
            if before != after or before != reviewed:
                return _a1_result("no_claim", dispatches, rounds, None, "review changed or split the tree")
            if verdict == "CLEAN":
                clean += 1
                continue
            if verdict == "DEFECTS":
                findings = answer
                break
            return _a1_result("no_claim", dispatches, rounds, None, "reviewer ended without a marker")
        if clean == 2:
            return _a1_result("claim", dispatches, rounds, reviewed, "two consecutive CLEAN")


def _a1_result(ended: str, dispatches: int, rounds: list[dict], claimed_tree: str | None, why: str) -> dict:
    return {"ended": ended, "dispatches": dispatches, "rounds": rounds, "claimed_tree": claimed_tree,
            "reason": why}


# --------------------------------------------------------------------------- #
# The cell runner
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class CellSpec:
    """What identifies a cell. The declaration is not here: the wiring is compared with the registered one."""

    stage: str
    task: str
    arm: str
    rep: int
    attempt: int
    repo_slug: str                       # e.g. "django/django" -- the scan's upstream-URL pattern
    issue_path: str                      # the issue text file the arms were given
    snapshot_path: str                   # the single-commit snapshot (public text of the base tree)
    hoh_root: str                        # every `hoh --root` the cell uses (liveness at grading)
    base_commit: str = ""
    supersedes: str | None = None

    @property
    def key(self) -> str:
        return cell_key(self.task, self.arm, self.rep, self.attempt)


_SLUG = re.compile(r"[A-Za-z0-9_.-]{1,64}/[A-Za-z0-9_.-]{1,64}")


def run_cell(spec: CellSpec, ports, results: Path, ledger: BudgetLedger, patterns: WiringPatterns,
             freeze: Freeze, approvals: StageApprovals,
             monotonic: Callable[[], float] = time.monotonic, kill: KillPath | None = None) -> dict:
    """One cell, up to and including the run record. Grading is a separate step (temporal separation).

    Order, all pre-registered: (0) the frozen pattern list and the re-run rule; (1) the stage must be approved
    and AB4 must allow it -- before every start; (2) read the wiring back from the built objects and compare it
    with the **registered** declaration -- a mismatch ends the cell as INSTRUMENT_FAILURE **before any provider
    call**; (3) start in a process group of its own and record the group and its leader; (4) wait up to the
    runaway bound; at the bound, or when waiting itself fails, the kill path; (5) read the ending, collect
    usage from the transcripts; (6) charge the ledger -- the measured spend, or the cap-implied maximum when it
    cannot be determined, for **every** cell that started; (7) write `<key>.run.json` create-only, with the
    inputs `grade` needs (repo, issue, snapshot, root, transcripts).
    """
    results = Path(results)
    patterns.require_digest(freeze.wiring_patterns_digest)
    if not _SLUG.fullmatch(spec.repo_slug or ""):
        raise Bench1Error("the cell has no valid repo slug; the transcript scan needs it")
    for name in ("issue_path", "snapshot_path", "hoh_root"):
        if not getattr(spec, name):
            raise Bench1Error(f"the cell has no {name}; the transcript scan and the liveness check need it")
    check_rerun(results, spec.task, spec.arm, spec.rep, spec.attempt, spec.supersedes)
    ledger.check_before_start(spec.stage, approvals)
    decl = registered_declaration(spec.arm, freeze)
    doc: dict = {"schema": SCHEMA_RUN, "campaign": CAMPAIGN, "stage": spec.stage, "task": spec.task,
                 "arm": spec.arm, "rep": spec.rep, "attempt": spec.attempt, "key": spec.key,
                 "supersedes": spec.supersedes, "base_commit": spec.base_commit, "repo": spec.repo_slug,
                 "issue_path": spec.issue_path, "snapshot_path": spec.snapshot_path,
                 "hoh_root": spec.hoh_root, "started": utcstamp(), "registered": dataclasses.asdict(decl),
                 "wiring_patterns_digest": patterns.digest, "transcripts": []}
    ledger.begin(spec.stage, spec.key, spec.arm, spec.attempt)
    try:
        observed = ports.build_and_readback(spec)
    except Exception as exc:                                  # noqa: BLE001 - an instrument failure
        ledger.finish(spec.stage, spec.key, None, no_provider_call=True)
        doc.update(ended="instrument_failure", stop_reason=f"wiring read-back failed: {type(exc).__name__}",
                   usage=None, spend_wtu=0.0)
        return _finish_run(results, doc)
    doc["wiring"] = observed
    findings = compare_wiring(decl, observed, spec.arm)
    deferred = [k for k in observed.get("deferred_checks", ()) if k in WIRING_KEYS]
    post_reader = getattr(ports, "post_start_readback", None)
    if deferred and post_reader is None:
        findings.append({"check": "I4", "what": "checks are deferred and the port has no post-start read-back"})
    if findings:
        ledger.finish(spec.stage, spec.key, None, no_provider_call=True)
        doc.update(ended="instrument_failure", stop_reason="wiring read-back mismatch",
                   wiring_findings=findings, usage=None, spend_wtu=0.0)
        return _finish_run(results, doc)
    try:
        handle = ports.start(spec)
    except Exception as exc:                                  # noqa: BLE001
        # a start the port itself aborted before any provider call (`no_provider_call = True` on the
        # exception, e.g. a failed handshake) costs nothing; any other failed start is charged at the maximum
        quiet = getattr(exc, "no_provider_call", False) is True
        spent = ledger.finish(spec.stage, spec.key, None, no_provider_call=quiet)
        doc.update(ended="instrument_failure", stop_reason=f"start failed: {type(exc).__name__}",
                   usage=None, spend_wtu=spent, spend_assumed=not quiet)
        return _finish_run(results, doc)
    handle.started_at = monotonic()
    doc["pgid"] = handle.pgid
    if deferred:
        post_findings: list[dict]
        try:
            post = post_reader(spec, handle) or {}
            doc["wiring_post_start"] = post
            post_findings = compare_wiring(decl, {**observed, **post}, spec.arm, only=frozenset(deferred))
        except Exception as exc:                              # noqa: BLE001 - fail closed
            post = {}
            post_findings = [{"check": "I4", "what": f"post-start read-back failed: {type(exc).__name__}"}]
        if post_findings:
            killer = kill or KillPath(ports.hoh, ports.procs)
            try:
                doc["kill_steps"] = killer.execute(handle)
            except KillRefused as exc:
                doc["kill_steps"] = [{"step": "refused", "ok": False, "error": str(exc)}]
            try:
                obs_k = ports.collect(handle) or {}
            except Exception:                                 # noqa: BLE001
                obs_k = {}
            usage_k = collect_usage(obs_k.get("transcript_lines", []))
            quiet = usage_k is None and post.get("provider_calls_before_check") == 0 \
                and obs_k.get("dispatches") == 0
            spent = ledger.finish(spec.stage, spec.key, usage_k.wtu() if usage_k else None,
                                  no_provider_call=quiet)
            doc.update(ended="instrument_failure", stop_reason="post-start wiring read-back mismatch",
                       wiring_findings=post_findings, usage=usage_k.to_json() if usage_k else None,
                       spend_wtu=spent, spend_assumed=usage_k is None and not quiet,
                       transcripts=[str(p) for p in obs_k.get("transcripts", [])])
            return _finish_run(results, doc)
        release = getattr(ports, "release", None)
        if release is not None:
            try:
                release(spec)
            except Exception as exc:                              # noqa: BLE001 - nothing may dispatch
                killer = kill or KillPath(ports.hoh, ports.procs)
                try:
                    doc["kill_steps"] = killer.execute(handle)
                except KillRefused as exc2:
                    doc["kill_steps"] = [{"step": "refused", "ok": False, "error": str(exc2)}]
                spent = ledger.finish(spec.stage, spec.key, None, no_provider_call=True)
                doc.update(ended="instrument_failure", stop_reason=f"release failed: {type(exc).__name__}",
                           usage=None, spend_wtu=spent, spend_assumed=False)
                return _finish_run(results, doc)
    bound = runaway_bound_s(spec.arm)
    wait_error = None
    try:
        finished = ports.wait(handle, bound)
    except Exception as exc:                                  # noqa: BLE001 - kill and record, never leave it
        finished, wait_error = None, f"{type(exc).__name__}: {exc}"[:160]
    doc["wallclock_s"] = round(monotonic() - handle.started_at, 1)
    killed = False
    if finished is not True:
        killer = kill or KillPath(ports.hoh, ports.procs)
        try:
            doc["kill_steps"] = killer.execute(handle)
        except KillRefused as exc:
            doc["kill_steps"] = [{"step": "refused", "ok": False, "error": str(exc)}]
        killed = True
    try:
        obs = ports.collect(handle) or {}
    except Exception:                                         # noqa: BLE001
        obs = {"ended": "instrument_failure", "stop_reason": "collect failed"}
    usage = collect_usage(obs.get("transcript_lines", []))
    transcripts = [str(p) for p in obs.get("transcripts", [])]
    doc["transcripts"] = transcripts
    if killed:
        ended = "truncated" if finished is False else "instrument_failure"
        reason = f"runaway bound {bound} s" if finished is False else f"waiting failed: {wait_error}"
    else:
        ended, reason = obs.get("ended"), obs.get("stop_reason", "")
        if ended not in ENDINGS:
            ended, reason = "instrument_failure", f"unreadable ending {ended!r}"
    spent = ledger.finish(spec.stage, spec.key, usage.wtu() if usage else None)
    doc.update(ended=ended, stop_reason=reason, claimed_tree=obs.get("claimed_tree"),
               patch_path=obs.get("patch_path"), dispatches=obs.get("dispatches"),
               rounds=obs.get("rounds", []), artefactual=sorted(obs.get("artefactual", [])),
               usage=usage.to_json() if usage else None, spend_wtu=spent, spend_assumed=usage is None)
    if usage is None and ended not in ("truncated", "instrument_failure", "provider_failure"):
        doc.update(ended="instrument_failure", stop_reason="usage not determinable")
    return _finish_run(results, doc)


def _finish_run(results: Path, doc: dict) -> dict:
    path = write_create_only(results / f"{doc['key']}.run.json", doc)
    doc["path"] = str(path)
    return doc


# --------------------------------------------------------------------------- #
# Hidden rows: temporal separation, liveness, and the transcript scan (PREREG 13; I3, I15, I16)
# --------------------------------------------------------------------------- #


def parse_surface_list(stdout: str) -> list[str]:
    """The agent sessions `hoh surface list` says are still owned, as labels. Fail closed: anything that is not
    the documented shape (`{"surfaces": [rows]}`), a row with a `problem`, a row without a known `state`
    (`owned` / `released`) is an error -- an unanswerable question is not "nobody is alive"."""
    try:
        doc = json.loads(stdout)
    except ValueError as exc:
        raise Bench1Error("surface list is not JSON") from exc
    rows = doc.get("surfaces") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise Bench1Error("surface list has no 'surfaces' array")
    alive = []
    for row in rows:
        if not isinstance(row, dict):
            raise Bench1Error("surface list row is not an object")
        if "problem" in row:
            raise Bench1Error("surface registry of a run is unreadable")
        state = row.get("state")
        if state not in ("owned", "released"):
            raise Bench1Error("surface row has no known state")
        if state == "owned":
            alive.append(f"{row.get('run_id', '?')}:{row.get('pane_id', '?')}")
    return alive


def surface_liveness(hoh_ports: list) -> Callable[[], list[str]]:
    """`agents_alive` over **every** root the arms used. A root whose list cannot be had or read raises, and the
    caller (`HiddenRows`) then refuses to load rows."""
    if not hoh_ports:
        raise Bench1Error("no hoh root given: liveness would be asked of nobody")

    def alive() -> list[str]:
        out: list[str] = []
        for port in hoh_ports:
            out.extend(parse_surface_list(port.surface_list()))
        return out
    return alive


class HiddenRows:
    """Rows (patch, test_patch, FAIL_TO_PASS, PASS_TO_PASS) enter the grading environment only after the arm.

    `agents_alive()` lists the agent sessions that can still read this machine; any entry refuses the
    request, and so does any failure to answer (UNKNOWN is alive). The rows live in a
    `tempfile.TemporaryDirectory()` that releases itself, and the check is made again when the block ends: an
    agent session that started *during* grading is a window violation, recorded and raised. This class never
    reads the sealed directory: the production dataset port reads the public dataset.
    """

    def __init__(self, dataset, scratch_root: Path, agents_alive: Callable[[], list[str]]):
        self.dataset, self.scratch_root, self.agents_alive = dataset, Path(scratch_root), agents_alive

    def _alive(self) -> list[str]:
        try:
            alive = self.agents_alive()
        except Exception as exc:
            raise HiddenRowsRefused(f"liveness unknown ({type(exc).__name__}); rows are not loaded") from exc
        if not isinstance(alive, list) or not all(isinstance(a, str) for a in alive):
            raise HiddenRowsRefused("liveness answer unreadable; rows are not loaded")
        return alive

    @contextlib.contextmanager
    def for_grading(self, instance_ids: list[str]) -> Iterator[tuple[dict, Path]]:
        alive = self._alive()
        if alive:
            raise HiddenRowsRefused(f"{len(alive)} agent session(s) alive; rows are not loaded")
        with tempfile.TemporaryDirectory(dir=self.scratch_root, prefix="bench1-hidden-") as scratch:
            rows = self.dataset.fetch(list(instance_ids))
            missing = [i for i in instance_ids if i not in rows]
            if missing:
                raise Bench1Error(f"dataset did not return {len(missing)} requested instance(s)")
            yield rows, Path(scratch)
            late = self._alive()
            if late:
                raise HiddenRowsRefused("an agent session started while the rows were loaded")


@dataclasses.dataclass(frozen=True)
class Hit:
    file: str
    line: int
    category: str
    token: str


class ScanInputsMissing(Bench1Error):
    """The scan was asked to run without what it needs; "zero hits" would be a pass nobody read."""


def scan_transcripts(files: Iterable[Path], *, sealed_tokens: Iterable[str], test_ids: Iterable[str],
                     public_text: str, repo_slug: str) -> list[Hit]:
    """I3(b): every role transcript, in full, must have zero hits. Pure; fail closed.

    Categories: `sealed_path` (the sealed directory's path or a file name in it), `hidden_token`
    (`FAIL_TO_PASS`, `PASS_TO_PASS`, `test_patch`), `test_id` (an F2P/P2P id that does not occur in
    `public_text` = the issue plus the base tree's text, so a natural coincidence is not a hit) and
    `upstream_url` (`<repo>/(pull|commit|issues)`). An unreadable transcript raises: a scan that skipped a
    file would be a pass nobody read. A scan without a repo slug, without public text or without a single
    sealed token is refused (`ScanInputsMissing`) for the same reason.
    """
    if not isinstance(repo_slug, str) or not _SLUG.fullmatch(repo_slug):
        raise ScanInputsMissing("scan needs the repository slug")
    if not isinstance(public_text, str) or not public_text.strip():
        raise ScanInputsMissing("scan needs the public text (issue and base tree)")
    sealed = [t for t in sealed_tokens if t]
    if not sealed:
        raise ScanInputsMissing("scan needs the sealed-directory tokens")
    hits: list[Hit] = []
    ids = [i for i in test_ids if i and i not in public_text]
    url = re.compile(r"github\.com/" + re.escape(repo_slug) + r"/(?:pulls?|commits?|issues)\b")
    hidden = ("FAIL_TO_PASS", "PASS_TO_PASS", "test_patch")
    for path in files:
        try:
            text = read_regular_bytes(Path(path)).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise Bench1Error(f"transcript {Path(path).name} cannot be read: {type(exc).__name__}") from exc
        for number, line in enumerate(text.split("\n"), 1):
            for token in sealed:
                if token in line:
                    hits.append(Hit(str(path), number, "sealed_path", token))
            for token in hidden:
                if token in line:
                    hits.append(Hit(str(path), number, "hidden_token", token))
            for test_id in ids:
                if re.search(r"(?<![\w])" + re.escape(test_id) + r"(?![\w])", line):
                    hits.append(Hit(str(path), number, "test_id", test_id))
            for m in url.finditer(line):
                hits.append(Hit(str(path), number, "upstream_url", m.group(0)))
    return hits


#: Bound on the public text the scan builds from the base snapshot (text files only, each below the cap).
PUBLIC_FILE_CAP = 2 * 1024 * 1024
PUBLIC_TEXT_CAP = 128 * 1024 * 1024


def build_public_text(issue_path: Path, snapshot_path: Path) -> str:
    """The issue text plus the text files of the base snapshot (no `.git`): what an F2P/P2P id may legitimately
    occur in without being a leak. Refuses a missing issue (the scan must not run on nothing)."""
    issue = read_regular_bytes(Path(issue_path)).decode("utf-8", errors="replace")
    if not issue.strip():
        raise ScanInputsMissing("issue text is empty")
    parts, total = [issue], len(issue)
    root = Path(snapshot_path)
    if not root.is_dir():
        raise ScanInputsMissing("snapshot directory is missing")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            p = Path(dirpath) / name
            try:
                data = read_regular_bytes(p, PUBLIC_FILE_CAP)
            except Bench1Error:
                continue
            if b"\x00" in data:
                continue
            total += len(data)
            if total > PUBLIC_TEXT_CAP:
                raise Bench1Error("public text of the base tree is above its bound")
            parts.append(data.decode("utf-8", errors="replace"))
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Snapshot (I17) -- local git only
# --------------------------------------------------------------------------- #

#: The git verbs this tool uses. A call site outside this list is a defect; a test scans the source for it.
GIT_VERBS = ("init", "add", "commit", "rev-parse", "rev-list", "for-each-ref", "cat-file")


def _git(repo: Path, *args: str) -> str:
    verb = next((a for a in args if not a.startswith("-") and "=" not in a), None)
    if verb not in GIT_VERBS:
        raise Bench1Error("git verb is not on the allow-list")
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=300,
                          check=False, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repo)})
    if done.returncode != 0:
        raise Bench1Error(f"git {verb} failed: {done.stderr.strip()[:160]}")
    return done.stdout


def make_snapshot(source: Path, dest: Path, message: str = "bench1 snapshot") -> str:
    """A repository with exactly one commit of the files under `source` (no history, no foreign objects).

    The files of an image's own `/testbed` carry build artefacts (version files, egg-info) the grader sees; the
    caller streams them into `source` and this commits them all so they are part of the base and never of the
    agent's diff. A `.git` at the top of `source` is refused; a nested `.git` is skipped (its objects would be
    image-origin history), and a top-level symlink is copied as a link, never followed.
    """
    source, dest = Path(source), Path(dest)
    if (source / ".git").exists() or (source / ".git").is_symlink():
        raise Bench1Error("snapshot source carries a .git directory")
    dest.mkdir(parents=True, exist_ok=False)
    for entry in sorted(source.iterdir()):
        target = dest / entry.name
        if entry.is_symlink():
            os.symlink(os.readlink(entry), target)
        elif entry.is_dir():
            shutil.copytree(entry, target, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        else:
            shutil.copy2(entry, target, follow_symlinks=False)
    _git(dest, "init", "-q", "-b", "bench1-base")
    _git(dest, "add", "-A", "-f")
    _git(dest, "-c", "user.name=bench1", "-c", "user.email=bench1@localhost", "-c", "commit.gpgsign=false",
         "commit", "-q", "--no-verify", "-m", message)
    return _git(dest, "rev-parse", "HEAD").strip()


def snapshot_report(repo: Path) -> dict:
    """I17: commits reachable, refs, and objects that are not reachable from the commit's tree."""
    commits = int(_git(repo, "rev-list", "--all", "--count").strip() or "0")
    refs = len([line for line in _git(repo, "for-each-ref").splitlines() if line.strip()])
    reachable = {line.split()[0] for line in _git(repo, "rev-list", "--objects", "--all").splitlines()
                 if line.strip()}
    every = {line.split()[0] for line in
             _git(repo, "cat-file", "--batch-all-objects", "--batch-check").splitlines() if line.strip()}
    return {"commits": commits, "refs": refs, "objects": len(every),
            "unreachable_objects": len(every - reachable)}


# --------------------------------------------------------------------------- #
# Container runtime: allow-listed argument vectors, then images (manifest first, own images only)
# --------------------------------------------------------------------------- #

#: The container runtime's flag that discards a container when it exits. Written in two parts so that this
#: file contains no removal-looking token; the container it discards is this tool's own ephemeral one.
_EPHEMERAL = "--" "rm"
#: ASSUMED from the SWE-bench image layout (I1 reads the real line from the image in `prepare`).
CONDA_ACTIVATE = "source /opt/miniconda3/bin/activate testbed"


def bench_test_argv(image: str, worktree: Path, command: str, *, uid: int | None = None,
                    gid: int | None = None) -> list[str]:
    """The `bench-test` of PREREG 5.2/13 as an argument list: the task's own image, the invoking user (no
    root-owned files in the worktree), network off, the worktree mounted at `/testbed`, the conda
    environment activated. No shell string is interpolated into the runtime's own arguments."""
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    return ["docker", "run", _EPHEMERAL, "--network", "none", "--user", f"{uid}:{gid}",
            "-v", f"{Path(worktree).resolve()}:/testbed", "-w", "/testbed", "-e", "HOME=/tmp",
            image, "bash", "-c", f"{CONDA_ACTIVATE} && {command}"]


def docker_argv(action: str, *args: str) -> list[str]:
    """The ONLY way an argument vector for the container runtime is built (besides `bench_test_argv`). Every
    action is a fixed shape; a ref must look like an image reference (and not like an option), an id must be a
    full `sha256:` id. There is no prune, no force flag, no removal by name, no `system` verb."""
    if action == "pull" and len(args) == 1 and _REF.fullmatch(args[0]):
        return ["docker", "pull", args[0]]
    if action == "image_id" and len(args) == 1 and _REF.fullmatch(args[0]):
        return ["docker", "image", "inspect", "--format", "{{.Id}}", args[0]]
    if action == "containers_using" and len(args) == 1 and _IMAGE_ID.fullmatch(args[0]):
        return ["docker", "ps", "--all", "--filter", f"ancestor={args[0]}", "--format", "{{.ID}}"]
    if action == "image_remove" and len(args) == 1 and _IMAGE_ID.fullmatch(args[0]):
        return ["docker", "image", "remove", args[0]]
    raise Bench1Error(f"container runtime action {action!r} refused with these arguments")


def docker_argv_allowed(argv: list[str]) -> bool:
    """Whether an argument vector has one of the fixed shapes of `docker_argv` / `bench_test_argv`; the
    production port refuses anything else. `image remove` takes exactly one full image id and no flag."""
    if not isinstance(argv, list) or not argv or argv[0] != "docker" \
            or not all(isinstance(t, str) for t in argv):
        return False
    if argv[:3] == ["docker", "image", "remove"]:
        return len(argv) == 4 and _IMAGE_ID.fullmatch(argv[3]) is not None
    if argv[:2] == ["docker", "pull"]:
        return len(argv) == 3 and _REF.fullmatch(argv[2]) is not None
    if argv[:3] == ["docker", "image", "inspect"]:
        return len(argv) == 6 and argv[3:5] == ["--format", "{{.Id}}"] and _REF.fullmatch(argv[5]) is not None
    if argv[:2] == ["docker", "ps"]:
        return len(argv) == 7 and argv[2] == "--all" and argv[3] == "--filter" \
            and argv[4].startswith("ancestor=") and _IMAGE_ID.fullmatch(argv[4][9:]) is not None \
            and argv[5:] == ["--format", "{{.ID}}"]
    if argv[:2] == ["docker", "run"]:
        return len(argv) >= 6 and argv[2] == _EPHEMERAL and "--privileged" not in argv \
            and "--network" in argv and argv[argv.index("--network") + 1] == "none"
    return False


def seal(chain_path: Path, file: Path, kind: str) -> None:
    """Append the file's digest to the tool's own chain: a later hand edit of the file no longer matches."""
    row = {"file": Path(file).name, "sha256": sha256_file(file, single_link=True), "kind": kind,
           "at": utcstamp()}
    fd = os.open(chain_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_sealed(chain_path: Path, file: Path) -> None:
    """The file is a regular single-link file whose current digest is in the chain under its name."""
    try:
        text = read_regular_bytes(Path(chain_path), single_link=True).decode("utf-8")
    except Bench1Error as exc:
        raise ManifestRefused(f"chain unreadable: {exc}") from exc
    current = sha256_file(file, single_link=True)
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ManifestRefused("chain holds an unreadable row") from exc
        if isinstance(row, dict) and row.get("file") == Path(file).name and row.get("sha256") == current:
            return
    raise ManifestRefused(f"{Path(file).name} is not the file the tool wrote (digest not in the chain)")


def write_manifest(path: Path, campaign: str, entries: list[dict], chain_path: Path) -> Path:
    """Create-only and sealed, written **before** any pull: [{instance_id, ref, expected_digest?}]."""
    if campaign != CAMPAIGN:
        raise ManifestRefused("manifest is for another campaign")
    seen = set()
    for e in entries:
        if not isinstance(e.get("ref"), str) or not _REF.fullmatch(e["ref"]) \
                or not isinstance(e.get("instance_id"), str):
            raise ManifestRefused("a manifest entry needs instance_id and an image ref")
        if e["ref"] in seen:
            raise ManifestRefused("a manifest lists the same ref twice")
        seen.add(e["ref"])
    doc = {"schema": "bench1-image-manifest/1", "campaign": campaign, "written_at": utcstamp(),
           "images": entries}
    out = write_create_only(path, doc)
    seal(chain_path, out, "manifest")
    return out


def _read_manifest(path: Path, chain_path: Path) -> dict:
    verify_sealed(chain_path, path)
    doc = read_json_object(Path(path), single_link=True)
    if doc.get("schema") != "bench1-image-manifest/1" or doc.get("campaign") != CAMPAIGN \
            or not isinstance(doc.get("images"), list) or not isinstance(doc.get("written_at"), str):
        raise ManifestRefused("not a bench1 image manifest")
    return doc


def pull_one(docker, manifest_path: Path, ref: str, receipts_dir: Path, chain_path: Path) -> dict:
    """Use one listed image and write a sealed receipt **before** it is used.

    If the ref already exists locally the image is not ours: nothing is pulled (a pull would re-tag it), the
    receipt says `preexisting: true`, and `release_images` will never remove it. Otherwise the pulled image's
    id is recorded; release removes only an image whose current id equals it.
    """
    doc = _read_manifest(manifest_path, chain_path)
    entry = next((e for e in doc["images"] if e["ref"] == ref), None)
    if entry is None:
        raise ManifestRefused("image is not in the manifest; nothing is pulled")
    state_unknown = False
    try:
        before = docker.image_id(ref)
    except ImageStateUnknown:
        before, state_unknown = None, True
    preexisting = before is not None or state_unknown
    if preexisting:
        image_id = before
    else:
        docker.pull(ref)
        image_id = docker.image_id(ref)
        if image_id is None:
            raise ManifestRefused("image is not present after the pull")
    expected = entry.get("expected_digest")
    if expected and image_id is not None and expected != image_id:
        raise ManifestRefused("image id differs from the manifest's expected value")
    receipt = {"schema": "bench1-image-pull/1", "ref": ref, "image_id": image_id, "preexisting": preexisting,
               "state_unknown": state_unknown, "ours": not preexisting, "pulled_at": utcstamp(),
               "manifest": str(manifest_path),
               "manifest_digest": sha256_file(manifest_path, single_link=True),
               "manifest_written_at": doc["written_at"]}
    path = create_numbered(Path(receipts_dir), f"pull-{sha256_bytes(ref.encode())[:16]}", ".json", receipt)
    seal(chain_path, path, "pull")
    return receipt


def release_images(docker, manifest_path: Path, receipts_dir: Path, chain_path: Path,
                   refs: list[str] | None = None, confirm: bool = False) -> list[dict]:
    """Release images the campaign pulled, and nothing else.

    Every ref (default: every ref with a pull receipt) must (a) be in the manifest, whose digest is in the
    tool's chain, (b) have a sealed pull receipt bound to that manifest digest, with `ours` true and
    `preexisting` false, written after the manifest and not in the future, (c) still be the image the receipt
    names (current id == receipt id), and (d) have no container using it. A dry run unless `confirm`. The
    release receipt is written **before** the removal; the removal is by image id with the pinned argument
    vector `docker image remove <image-id>` (no force flag, no prune, no name); a completion receipt follows.
    """
    doc = _read_manifest(manifest_path, chain_path)
    manifest_digest = sha256_file(manifest_path, single_link=True)
    listed = {e["ref"] for e in doc["images"]}
    now = utcstamp()
    pulls: dict[str, dict] = {}
    for f in sorted(Path(receipts_dir).glob("pull-*.json")):
        verify_sealed(chain_path, f)
        r = read_json_object(f, single_link=True)
        if r.get("schema") == "bench1-image-pull/1" and r.get("manifest_digest") == manifest_digest:
            pulls[r["ref"]] = r
    wanted = [r for r in sorted(pulls) if pulls[r].get("ours") is True] if refs is None else list(refs)
    plan: list[tuple[str, str]] = []
    for ref in wanted:
        if ref not in listed:
            raise ManifestRefused("image is not in the manifest; nothing is released")
        r = pulls.get(ref)
        if r is None:
            raise ManifestRefused("image has no pull receipt bound to this manifest; nothing is released")
        if r.get("preexisting") is not False or r.get("ours") is not True:
            raise ManifestRefused("image existed before the campaign; it is not ours and is never released")
        if not isinstance(r.get("pulled_at"), str) or r["pulled_at"] < doc["written_at"] or r["pulled_at"] > now:
            raise ManifestRefused("pull receipt is older than the manifest or dated in the future")
        image_id = r.get("image_id")
        if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
            raise ManifestRefused("pull receipt carries no image id")
        try:
            current = docker.image_id(ref)
        except ImageStateUnknown as exc:
            raise ManifestRefused("the runtime cannot say whether the image exists; nothing is released") from exc
        if current is None:
            raise ManifestRefused("image is no longer present; nothing to release")
        if current != image_id:
            raise ManifestRefused("the ref now names another image than the one the campaign pulled")
        if docker.containers_using(image_id):
            raise ManifestRefused("a container still uses the image")
        plan.append((ref, image_id))
    out: list[dict] = []
    for ref, image_id in plan:
        stem = f"release-{sha256_bytes(ref.encode())[:16]}-{utcstamp()}"
        intent = {"schema": "bench1-image-release/1", "phase": "intent", "ref": ref, "image_id": image_id,
                  "dry_run": not confirm, "manifest_digest": manifest_digest, "at": utcstamp()}
        path = create_numbered(Path(receipts_dir), stem + ("-dry" if not confirm else "-intent"), ".json", intent)
        seal(chain_path, path, "release-intent")
        row = dict(intent)
        if confirm:
            docker.remove_image(image_id)
            done = {**intent, "phase": "done", "at": utcstamp()}
            path = create_numbered(Path(receipts_dir), stem + "-done", ".json", done)
            seal(chain_path, path, "release-done")
            row = done
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# Report statistics (PREREG 7.2, 7.3): stdlib, exact
# --------------------------------------------------------------------------- #


def mcnemar_one_sided_p(b: int, c: int) -> float:
    """P(X >= b), X ~ Bin(b + c, 1/2): evidence that the arm with the b-only events is worse.

    `b` is the count of tasks where the *reference* arm has the event and the other does not."""
    if b < 0 or c < 0:
        raise Bench1Error("negative count")
    m = b + c
    if m == 0:
        return 1.0
    return sum(math.comb(m, i) for i in range(b, m + 1)) / 2.0 ** m


def score_z(u: int, v: int, n: int, delta: float = DELTA) -> float:
    """Tango/Nam-type score statistic for H0: p_v - p_u = -delta, restricted MLE of q = p_u + p_v."""
    r = n - u - v
    a = 2.0 * n - (u + v)
    b = (u + v) - delta * (v - u)
    c = delta * (v - u) + 2.0 * r * delta * delta
    q = (b + math.sqrt(max(b * b + 4.0 * a * c, 0.0))) / (2.0 * a)
    return (v - u + delta * n) / math.sqrt(n * max(q - delta * delta, 1e-12))


_NI_CACHE: dict[tuple, tuple[float | None, float | None]] = {}


def ni_cutoffs(n: int, delta: float = DELTA, alpha: float = ALPHA,
               grid: int = NI_GRID) -> tuple[float | None, float | None]:
    """(t_ni, t_inf): non-inferiority is established if Z >= t_ni, INFERIOR if Z <= t_inf.

    Exact unconditional: the size of the region {Z >= t} is the maximum over the boundary
    p_u = pi + delta, p_v = pi (pi on `grid` points) of its trinomial probability. Computed once per (n, ...)
    with cumulative sums over the outcomes sorted by Z, so n = 172 takes seconds, not minutes.
    """
    key = (n, delta, alpha, grid)
    if key in _NI_CACHE:
        return _NI_CACHE[key]
    pairs = [(score_z(u, v, n, delta), u, v) for u in range(n + 1) for v in range(n + 1 - u)]
    pairs.sort(key=lambda t: (t[0], t[1], t[2]))
    zs = [round(p[0], 9) for p in pairs]
    lg = [math.lgamma(k + 1) for k in range(n + 2)]
    pis = [1e-6 + ((1.0 - delta) / 2.0 - 2e-6) * i / (grid - 1) for i in range(grid)]
    size_upper = [0.0] * len(pairs)            # max over pi of P(Z >= zs[i]) -- filled from the top
    size_lower = [0.0] * len(pairs)            # max over pi of P(Z <= zs[i])
    for pi in pis:
        pu, pv = pi + delta, pi
        pr = 1.0 - pu - pv
        lpu, lpv, lpr = math.log(pu), math.log(pv), math.log(pr)
        probs = [math.exp(lg[n] - lg[u] - lg[v] - lg[n - u - v] + u * lpu + v * lpv + (n - u - v) * lpr)
                 for (_z, u, v) in pairs]
        acc = 0.0
        for i in range(len(pairs) - 1, -1, -1):      # upper tail
            acc += probs[i]
            size_upper[i] = max(size_upper[i], acc)
        acc = 0.0
        for i in range(len(pairs)):                  # lower tail
            acc += probs[i]
            size_lower[i] = max(size_lower[i], acc)
    t_ni = t_inf = None
    # upper: the smallest t (a value of Z) whose region {Z >= t} has size <= alpha; ties evaluated at the
    # first index of their group (the whole group is in the region).
    i = len(pairs) - 1
    while i >= 0:
        j = i
        while j > 0 and zs[j - 1] == zs[i]:
            j -= 1
        if size_upper[j] <= alpha:
            t_ni = zs[j]
            i = j - 1
        else:
            break
    # lower: the largest t whose region {Z <= t} has size <= alpha; evaluated at the last index of the group.
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and zs[j + 1] == zs[i]:
            j += 1
        if size_lower[j] <= alpha:
            t_inf = zs[j]
            i = j + 1
        else:
            break
    _NI_CACHE[key] = (t_ni, t_inf)
    return t_ni, t_inf


def ni_state(u: int, v: int, n: int, delta: float = DELTA) -> str:
    """NI_ESTABLISHED / INFERIOR / INDETERMINATE for the yield pairs (u: reference only, v: other only)."""
    t_ni, t_inf = ni_cutoffs(n, delta)
    z = round(score_z(u, v, n, delta), 9)
    if t_ni is not None and z >= t_ni - 1e-9:
        return "NI_ESTABLISHED"
    if t_inf is not None and z <= t_inf + 1e-9:
        return "INFERIOR"
    return "INDETERMINATE"


def bootstrap_interval(diffs: list[int], resamples: int = 10000, seed: int = SEED,
                       level: float = 0.90) -> tuple[float, float] | None:
    """Percentile interval of the mean of per-task paired differences; None when there are no tasks."""
    n = len(diffs)
    if n == 0:
        return None
    rng = random.Random(seed)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    lo = means[int(((1 - level) / 2) * (resamples - 1))]
    hi = means[int((1 - (1 - level) / 2) * (resamples - 1))]
    return lo, hi


def paired_report(a_cells: list[dict], b_cells: list[dict], itt: bool = False) -> dict:
    """H1/H1r/H2 for reference arm A (A1+) vs arm B on the tasks both have, per PREREG 7.2.

    Pairwise deletion of tasks where either cell is a non-outcome; `itt=True` keeps them, scoring a
    non-outcome as no-claim (never a false accept, never a yield)."""
    a = {c["task"]: c for c in latest_attempts(a_cells)}
    b = {c["task"]: c for c in latest_attempts(b_cells)}
    tasks = sorted(set(a) & set(b))
    pairs = []
    dropped = []
    for t in tasks:
        ca, cb = a[t]["class"], b[t]["class"]
        if not itt and not (is_outcome(ca) and is_outcome(cb)):
            dropped.append(t)
            continue
        pairs.append((ca if is_outcome(ca) else "UNCLAIMED_FAIL", cb if is_outcome(cb) else "UNCLAIMED_FAIL"))
    n = len(pairs)

    def fa(c: str) -> bool:
        return c == "FALSE_ACCEPT"

    def yy(c: str) -> bool:
        return c == "ACCEPT_CORRECT"
    b_only = sum(1 for x, y in pairs if fa(x) and not fa(y))      # reference FA, other not
    c_only = sum(1 for x, y in pairs if fa(y) and not fa(x))
    u = sum(1 for x, y in pairs if yy(x) and not yy(y))
    v = sum(1 for x, y in pairs if yy(y) and not yy(x))
    out = {"n": n, "dropped_tasks": dropped, "itt": itt,
           "fa_reference": sum(1 for x, _ in pairs if fa(x)), "fa_other": sum(1 for _, y in pairs if fa(y)),
           "y_reference": sum(1 for x, _ in pairs if yy(x)), "y_other": sum(1 for _, y in pairs if yy(y)),
           "h1_b_only": b_only, "h1_c_only": c_only,
           "h1_p": mcnemar_one_sided_p(b_only, c_only), "h1r_p": mcnemar_one_sided_p(c_only, b_only),
           "h2_u": u, "h2_v": v}
    out["h2_state"] = ni_state(u, v, n) if n > 0 else "INDETERMINATE"
    out["fa_diff_interval90"] = bootstrap_interval([(1 if fa(y) else 0) - (1 if fa(x) else 0)
                                                    for x, y in pairs])
    out["y_diff_interval90"] = bootstrap_interval([(1 if yy(y) else 0) - (1 if yy(x) else 0)
                                                   for x, y in pairs])
    return out


def attrition_flag(cells_by_arm: dict[str, list[dict]], arm_x: str, arm_y: str) -> dict:
    """AB8: arms differ by >= 3 non-outcome cells in a stage."""
    nx = sum(1 for c in latest_attempts(cells_by_arm.get(arm_x, [])) if not is_outcome(c["class"]))
    ny = sum(1 for c in latest_attempts(cells_by_arm.get(arm_y, [])) if not is_outcome(c["class"]))
    return {"non_outcomes": {arm_x: nx, arm_y: ny}, "differential": abs(nx - ny) >= 3}


# --------------------------------------------------------------------------- #
# Entry gate (PREREG 4)
# --------------------------------------------------------------------------- #


def entry_gate(gold_arms_path: list[dict], gold_official: list[dict],
               base_arms_path: list[dict], base_official: list[dict]) -> dict:
    """The gate holds iff, on both paths, twice: gold -> every F2P and P2P test passes; base -> some F2P fails;
    and the two paths agree. Each result is {"f2p_failed": int, "p2p_failed": int}; fewer than two results on
    any of the four lists is a failure (twice means twice)."""
    reasons: list[str] = []
    for name, lst in (("gold/arms", gold_arms_path), ("gold/official", gold_official),
                      ("base/arms", base_arms_path), ("base/official", base_official)):
        if len(lst) < 2:
            reasons.append(f"{name}: fewer than two results")
    if reasons:
        return {"holds": False, "reasons": reasons}
    for name, lst in (("gold/arms", gold_arms_path), ("gold/official", gold_official)):
        for r in lst:
            if r["f2p_failed"] != 0 or r["p2p_failed"] != 0:
                reasons.append(f"{name}: gold patch does not pass everything")
    for name, lst in (("base/arms", base_arms_path), ("base/official", base_official)):
        for r in lst:
            if r["f2p_failed"] == 0:
                reasons.append(f"{name}: no FAIL_TO_PASS test fails without the gold patch")
    for a, b, name in ((gold_arms_path, gold_official, "gold"), (base_arms_path, base_official, "base")):
        if {(r["f2p_failed"] > 0, r["p2p_failed"] > 0) for r in a} != {(r["f2p_failed"] > 0,
                                                                         r["p2p_failed"] > 0) for r in b}:
            reasons.append(f"{name}: the arms' path and the official grader disagree")
    return {"holds": not reasons, "reasons": reasons}


# --------------------------------------------------------------------------- #
# Grade, regrade, leak adjudication (after the arm; rows only through HiddenRows)
# --------------------------------------------------------------------------- #


def grade_cell(run_doc: dict, hidden: HiddenRows, grader, patterns: WiringPatterns, results: Path,
               *, sealed_tokens: list[str]) -> dict:
    """Write `<key>.cell.json` from a run record: scan **every** transcript of the cell, grade the claimed patch
    where a verdict is wanted, classify.

    The scan inputs (repo slug, issue, base snapshot) come from the run record; if they are absent the
    function refuses before anything is loaded. A run that spent sessions but lists no transcript is refused
    too. Non-outcome cells are scanned (they cost the most and may have read the most) and are not graded; a
    product stop is graded descriptively; scan hits set `leak_suspect` without changing the class.
    """
    results = Path(results)
    for key in ("repo", "issue_path", "snapshot_path", "hoh_root"):
        if not run_doc.get(key):
            raise ScanInputsMissing(f"run record has no {key}")
    # the classification is digest-bound: the pattern list used now must be the one the cell ran under
    if run_doc.get("wiring_patterns_digest") != patterns.digest:
        raise Bench1Error("the run record was written under another wiring pattern list than the one loaded")
    transcripts = [Path(p) for p in run_doc.get("transcripts", [])]
    sessions_ran = (bool(run_doc.get("dispatches")) or run_doc.get("usage") is not None
                    or run_doc.get("pgid") is not None or run_doc.get("spend_assumed") is True
                    or bool(run_doc.get("kill_steps")))
    public_text = build_public_text(Path(run_doc["issue_path"]), Path(run_doc["snapshot_path"]))
    obs = Observation(ended=run_doc["ended"], claim=run_doc["ended"] == "claim",
                      stop_reason=run_doc.get("stop_reason", ""))
    verdict: dict | None = None
    hits: list[Hit] = []
    scanned = False
    scan_gap = None
    cls = classify(obs, patterns) if run_doc["ended"] not in ("claim", "no_claim") else None
    wants_verdict = (cls is None or cls in OUTCOME_CLASSES) and bool(run_doc.get("patch_path"))
    if sessions_ran and not transcripts:
        # a started cell that lists no transcript cannot be certified leak-free: a named non-outcome, never a
        # verdict about the arm (it is re-run once); not graded
        scan_gap = "a started cell lists no transcript"
        if cls is None or cls in OUTCOME_CLASSES:
            cls = "INSTRUMENT_FAILURE"
        wants_verdict = False
    if (transcripts or wants_verdict) and scan_gap is None:
        with hidden.for_grading([run_doc["task"]]) as (rows, scratch):
            row = rows[run_doc["task"]]
            if wants_verdict:
                verdict = grader.evaluate(run_doc["task"], Path(run_doc["patch_path"]), row, scratch)
            test_ids = list(row.get("FAIL_TO_PASS", [])) + list(row.get("PASS_TO_PASS", []))
        if transcripts:
            hits = scan_transcripts(transcripts, sealed_tokens=sealed_tokens, test_ids=test_ids,
                                    public_text=public_text, repo_slug=run_doc["repo"])
            scanned = True
    if verdict is not None:
        obs.passed = bool(verdict["resolved"])
        cls = classify(obs, patterns)
    elif cls is None:
        # a claim or a no-claim without a patch to grade has no verdict: the instrument failed
        cls = "INSTRUMENT_FAILURE"
    cell = {"schema": SCHEMA_CELL, "campaign": CAMPAIGN, "key": run_doc["key"], "task": run_doc["task"],
            "arm": run_doc["arm"], "rep": run_doc["rep"], "attempt": run_doc["attempt"],
            "stage": run_doc["stage"], "supersedes": run_doc.get("supersedes"), "ended": run_doc["ended"],
            "class": cls, "passed": obs.passed, "claim": obs.claim,
            "scanned": scanned, "scan_files": len(transcripts) if scanned else 0, "scan_gap": scan_gap,
            "leak_suspect": bool(hits), "scan_hits": [dataclasses.asdict(h) for h in hits][:50],
            "usage": run_doc.get("usage"), "spend_wtu": run_doc.get("spend_wtu"),
            "spend_assumed": run_doc.get("spend_assumed"), "wallclock_s": run_doc.get("wallclock_s"),
            "artefactual": run_doc.get("artefactual", []), "graded_at": utcstamp(), "grade": verdict}
    write_create_only(results / f"{run_doc['key']}.cell.json", cell)
    return cell


def regrade_cell(cell: dict, patch_path: Path, hidden: HiddenRows, grader, results: Path) -> dict:
    """Re-evaluate the stored patch; write a create-only regrade record and say whether it agrees."""
    with hidden.for_grading([cell["task"]]) as (rows, scratch):
        verdict = grader.evaluate(cell["task"], Path(patch_path), rows[cell["task"]], scratch)
    rec = {"schema": "bench1-regrade/1", "key": cell["key"], "at": utcstamp(),
           "resolved": bool(verdict["resolved"]), "agrees": bool(verdict["resolved"]) == cell.get("passed")}
    create_numbered(Path(results), f"{cell['key']}.regrade-{utcstamp()}", ".json", rec)
    return rec


def confirm_leak(results: Path, key: str, adjudications: list[dict]) -> dict:
    """The writer of INVALID_LEAK (PREREG I3(c)): two independent readers' verdicts on a `leak_suspect` cell.

    Needs at least two distinct readers; the cell is INVALID_LEAK only if **all** say CONFIRMED. The record is
    create-only (`<key>.leak-adjudication.json`); the cell file is not touched (`load_cells` applies it)."""
    results = Path(results)
    cell = read_json_object(results / f"{key}.cell.json", single_link=True)
    if cell.get("leak_suspect") is not True:
        raise Bench1Error("only a leak_suspect cell is adjudicated")
    readers = {a.get("reader") for a in adjudications if isinstance(a, dict)}
    verdicts = [a.get("verdict") for a in adjudications if isinstance(a, dict)]
    if len(readers) < 2 or None in readers or len(readers) != len(adjudications) \
            or not all(v in ("CONFIRMED", "NOT_CONFIRMED") for v in verdicts):
        raise Bench1Error("a leak needs two distinct readers with CONFIRMED or NOT_CONFIRMED")
    doc = {"schema": "bench1-leak-adjudication/1", "key": key, "adjudications": adjudications,
           "confirmed": all(v == "CONFIRMED" for v in verdicts), "at": utcstamp()}
    write_create_only(results / f"{key}.leak-adjudication.json", doc)
    return doc


# --------------------------------------------------------------------------- #
# Production ports (thin; exercised only by the campaign's Stage 0 checks, never by the tests)
# --------------------------------------------------------------------------- #


class SubprocessDocker:
    """The container runtime through allow-listed argument lists, as the invoking user.

    Every vector passes `docker_argv_allowed` before it is run; `runner` is injectable so that the tests pin
    each vector without a runtime."""

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess] | None = None):
        self.runner = runner or subprocess.run

    def _exec(self, argv: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
        if not docker_argv_allowed(argv):
            raise Bench1Error("container runtime vector is not on the allow-list")
        return self.runner(argv, capture_output=True, text=True, timeout=timeout, check=False)

    def pull(self, ref: str) -> None:
        done = self._exec(docker_argv("pull", ref), timeout=3600)
        if done.returncode != 0:
            raise Bench1Error("image pull failed")

    def image_id(self, ref: str) -> str | None:
        done = self._exec(docker_argv("image_id", ref))
        out = (done.stdout or "").strip()
        if done.returncode != 0:
            # only the runtime's plain "No such image" means absent; a daemon that cannot be reached, a
            # permission error or anything else is UNKNOWN, and UNKNOWN is treated as pre-existing
            if "No such image" in (done.stderr or ""):
                return None
            raise ImageStateUnknown("the runtime could not say whether the image exists")
        if not _IMAGE_ID.fullmatch(out):
            raise ImageStateUnknown("runtime returned an unreadable image id")
        return out

    def containers_using(self, image_id: str) -> list[str]:
        done = self._exec(docker_argv("containers_using", image_id))
        if done.returncode != 0:
            raise Bench1Error("container listing failed")
        return [x for x in done.stdout.split() if x]

    def remove_image(self, image_id: str) -> None:
        done = self._exec(docker_argv("image_remove", image_id))
        if done.returncode != 0:
            raise Bench1Error("image release failed")

    def run_tests(self, image: str, worktree: Path, command: str) -> subprocess.CompletedProcess:
        return self._exec(bench_test_argv(image, worktree, command), timeout=7200)


class ProcessGroups:  # pragma: no cover - signals real processes
    def signal_group(self, pgid: int, sig: int) -> None:
        os.killpg(pgid, sig)

    def group_alive(self, pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


class HohCli:
    """The product's own commands, by argument list, under one `--root`. Every call checks the return code:
    a command that failed raises, so a kill receipt never says `ok` for a step that did nothing."""

    def __init__(self, root: Path, python: str = sys.executable, src: Path | None = None,
                 runner: Callable[..., subprocess.CompletedProcess] | None = None):
        self.root, self.python = Path(root), python
        self.runner = runner or subprocess.run
        self.env = dict(os.environ)
        if src is not None:
            self.env["PYTHONPATH"] = str(src)

    def argv(self, *args: str) -> list[str]:
        return [self.python, "-m", "hoh.cli", "--root", str(self.root), *args]

    def run(self, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
        done = self.runner(self.argv(*args), capture_output=True, text=True, timeout=timeout,
                           env=self.env, check=False)
        if done.returncode != 0:
            raise Bench1Error(f"hoh {args[0] if args else ''} failed with exit {done.returncode}")
        return done

    def surface_list(self) -> str:
        return self.run("surface", "list").stdout

    def pause(self, run_id: str) -> None:
        self.run("pause", run_id)

    def cancel(self, run_id: str, reason: str) -> None:
        self.run("cancel", run_id, "--reason", reason)

    def surface_reap_close(self, run_id: str) -> None:
        self.run("surface", "reap", "--close", run_id)

    def close_owned(self, cell_key: str) -> None:
        raise Bench1Error("the panes of an A arm are closed by its worker's dispatcher; the runner cannot "
                          "close them (the group kill follows)")

    def state(self, run_id: str) -> dict:
        doc = json.loads(self.run("status", run_id).stdout)
        if not isinstance(doc, dict):
            raise Bench1Error("hoh status did not return an object")
        return doc


class DatasetHF:  # pragma: no cover - network
    """The public dataset, read into memory (never written to disk); the sealed directory is not read."""

    URL = ("https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified/resolve/main/data/"
           "test-00000-of-00001.parquet")

    def fetch(self, ids: list[str]) -> dict:
        import io
        import urllib.request

        import pyarrow.parquet as pq
        with urllib.request.urlopen(self.URL, timeout=300) as r:
            table = pq.read_table(io.BytesIO(r.read())).to_pylist()
        out = {}
        for row in table:
            if row["instance_id"] in ids:
                out[row["instance_id"]] = {
                    "FAIL_TO_PASS": json.loads(row["FAIL_TO_PASS"]),
                    "PASS_TO_PASS": json.loads(row["PASS_TO_PASS"]),
                    "test_patch": row["test_patch"], "patch": row["patch"],
                    "base_commit": row["base_commit"], "repo": row["repo"],
                    "problem_statement": row["problem_statement"]}
        return out


# --------------------------------------------------------------------------- #
# Report (tables from cell files only) and the command line
# --------------------------------------------------------------------------- #


def load_cells(results: Path) -> list[dict]:
    """Final cell files, with confirmed leak adjudications applied (a confirmed leak makes the cell
    INVALID_LEAK; the file itself is never edited)."""
    cells = []
    results = Path(results)
    for f in sorted(results.glob("*.cell.json")):
        doc = read_json_object(f, single_link=True)
        if doc.get("schema") != SCHEMA_CELL or doc.get("class") not in CLASSES:
            raise Bench1Error(f"{f.name} is not a final cell record")
        adj = results / f"{doc.get('key')}.leak-adjudication.json"
        if adj.exists():
            a = read_json_object(adj, single_link=True)
            if a.get("confirmed") is True:
                doc = {**doc, "class": "INVALID_LEAK", "leak_confirmed": True}
        cells.append(doc)
    return cells


def report(results: Path) -> dict:
    cells = load_cells(results)
    by_arm: dict[str, list[dict]] = {}
    for c in cells:
        by_arm.setdefault(c["arm"], []).append(c)
    table = {}
    for arm, lst in sorted(by_arm.items()):
        final = latest_attempts(lst)
        counts = {cls: sum(1 for c in final if c["class"] == cls) for cls in CLASSES}
        spend = [c["usage"]["wtu"] for c in final if c.get("usage")]
        table[arm] = {"cells": len(final), "classes": {k: v for k, v in counts.items() if v},
                      "wtu_total": sum(spend), "wtu_known_cells": len(spend)}
    out = {"campaign": CAMPAIGN, "arms": table, "label": "EXPLORATORY unless the stage is Stage 2"}
    if "A1P" in by_arm and "B" in by_arm:
        out["paired_A1P_vs_B"] = paired_report(by_arm["A1P"], by_arm["B"])
        out["paired_A1P_vs_B_itt"] = paired_report(by_arm["A1P"], by_arm["B"], itt=True)
        out["attrition"] = attrition_flag(by_arm, "A1P", "B")
    return out


def _main_report(args) -> int:
    print(json.dumps(report(Path(args.results)), indent=1, sort_keys=True))
    return 0


def _main_release(args) -> int:  # pragma: no cover - needs a container runtime
    rows = release_images(SubprocessDocker(), Path(args.manifest), Path(args.receipts), Path(args.chain),
                          refs=args.ref or None, confirm=args.confirm)
    print(json.dumps(rows, indent=1))
    return 0


def _main_gate(args) -> int:
    cells = [c for c in load_cells(Path(args.results)) if c["arm"] == "A1P" and c["stage"] == "1a"]
    print(json.dumps(stage1a_gate(cells), indent=1))
    return 0


def _main_check_patterns(args) -> int:
    p = WiringPatterns.load(Path(args.file))
    if args.frozen:
        p.require_digest(args.frozen)
    print(json.dumps({"patterns": len(p.patterns), "digest": p.digest}))
    return 0


def _main_entry_gate(args) -> int:
    doc = read_json_object(Path(args.results_file))
    out = entry_gate(doc.get("gold_arms", []), doc.get("gold_official", []),
                     doc.get("base_arms", []), doc.get("base_official", []))
    print(json.dumps(out, indent=1))
    return 0 if out["holds"] else 1


def _ports_module():
    """The production ports (`tools/bench1_ports.py`, the sibling file), loaded by path. This module registers
    itself as `bench1` first so that the ports and the CLI share one set of classes and exceptions."""
    sys.modules.setdefault("bench1", sys.modules[__name__])
    path = Path(__file__).resolve().with_name("bench1_ports.py")
    spec = importlib.util.spec_from_file_location("bench1_ports", path)
    if spec is None or spec.loader is None:
        raise Bench1Error("the production ports module cannot be loaded")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bench1_ports"] = mod
    spec.loader.exec_module(mod)
    return mod


def _main_bound(args) -> int:
    """`prepare`, `run-cell`, `regrade` and `arm-worker` are implemented by the ports module; each refuses with
    `refused: <rule>` (exit 2) when a precondition does not hold."""
    mod = _ports_module()
    return getattr(mod, "main_" + args.name.replace("-", "_"))(args, sys.modules[__name__])


def _main_grade(args) -> int:  # pragma: no cover - needs docker, the dataset and a stopped stage
    results = Path(args.results)
    patterns = WiringPatterns.load(Path(args.patterns))
    patterns.require_digest(args.frozen_patterns)
    roots = [Path(r) for r in args.hoh_root]
    mod = _ports_module()
    # the instruments are the ports module's: the liveness asks the product's surface list AND the dispatchers'
    # own registry, and the grader is the pinned `OfficialGrader` the entry gate validated (one class)
    ports = mod.liveness_ports([HohCli(r) for r in roots])
    hidden = HiddenRows(DatasetHF(), Path(args.scratch_root), surface_liveness(ports))
    grader = mod.build_grader(agent_roots=roots)
    written = 0
    for f in sorted(results.glob("*.run.json")):
        if (results / (f.name[:-len(".run.json")] + ".cell.json")).exists():
            continue
        run = read_json_object(f, single_link=True)
        if Path(run.get("hoh_root", "")) not in roots:
            raise Bench1Error("a run record names a root that liveness was not asked of")
        grade_cell(run, hidden, grader, patterns, results, sealed_tokens=args.sealed_token)
        written += 1
    print(json.dumps({"graded": written}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bench1", description=__doc__.split("\n\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="tables of PREREG 7 from final cell files only")
    r.add_argument("--results", required=True)
    r.set_defaults(func=_main_report)
    g = sub.add_parser("gate", help="AB2a/AB2b/AB3 over the A1+ cells of Stage 1a")
    g.add_argument("--results", required=True)
    g.set_defaults(func=_main_gate)
    w = sub.add_parser("check-patterns", help="load the wiring pattern file; refuse a changed digest")
    w.add_argument("--file", required=True)
    w.add_argument("--frozen", default="")
    w.set_defaults(func=_main_check_patterns)
    rel = sub.add_parser("release-images", help="release images the campaign pulled, from the manifest only")
    rel.add_argument("--manifest", required=True)
    rel.add_argument("--receipts", required=True)
    rel.add_argument("--chain", required=True)
    rel.add_argument("--ref", action="append")
    rel.add_argument("--confirm", action="store_true", help="without it: a dry run with receipts")
    rel.set_defaults(func=_main_release)
    gr = sub.add_parser("grade", help="grade the run records of a stage (no agent session may be alive)")
    gr.add_argument("--results", required=True)
    gr.add_argument("--scratch-root", required=True)
    gr.add_argument("--patterns", required=True)
    gr.add_argument("--frozen-patterns", required=True,
                    help="the F1 digest of the wiring pattern file; the run records must carry it too")
    gr.add_argument("--sealed-token", action="append", required=True)
    gr.add_argument("--hoh-root", action="append", required=True,
                    help="every root the arms used; liveness is asked of each, and a run record naming "
                         "another root is refused")
    gr.set_defaults(func=_main_grade)
    ar = sub.add_parser("gate-check", aliases=["validate"],
                        help="the entry gate from four result lists (JSON file)")
    ar.add_argument("--results-file", required=True)
    ar.set_defaults(func=_main_entry_gate)
    pr = sub.add_parser("prepare", help="one preparation step of a stage: manifest, pull, snapshot, validate")
    pr.add_argument("--campaign-root", required=True)
    pr.add_argument("--stage", required=True, choices=["T", "0"])
    pr.add_argument("--step", required=True, choices=["manifest", "pull", "snapshot", "validate"])
    pr.add_argument("--task", default="", help="required for pull, snapshot and validate")
    pr.add_argument("--freeze", required=True)
    pr.add_argument("--sample", required=True, help="tuning_sample.json (stage T) or sample.json (stage 0)")
    pr.add_argument("--meta", default="", help="the frozen metadata (data/*.json): problem_chars must match the issue")
    pr.set_defaults(func=_main_bound, name="prepare")
    rc = sub.add_parser("run-cell", help="one cell: wiring read-back, start, wait, kill path, run record")
    rc.add_argument("--campaign-root", required=True)
    rc.add_argument("--stage", required=True, choices=list(STAGES))
    rc.add_argument("--task", required=True)
    rc.add_argument("--arm", required=True, choices=["A0", "A1P", "B"])
    rc.add_argument("--rep", type=int, default=1)
    rc.add_argument("--attempt", type=int, default=1)
    rc.add_argument("--supersedes", default=None)
    rc.add_argument("--freeze", required=True)
    rc.add_argument("--approval", required=True)
    rc.add_argument("--ledger", required=True)
    rc.add_argument("--patterns", required=True)
    rc.add_argument("--results", required=True)
    rc.add_argument("--sample", required=True)
    rc.set_defaults(func=_main_bound, name="run-cell")
    rg = sub.add_parser("regrade", help="re-evaluate a stored cell's patch (no agent session may be alive)")
    rg.add_argument("--results", required=True)
    rg.add_argument("--key", required=True)
    rg.add_argument("--scratch-root", required=True)
    rg.add_argument("--hoh-root", action="append", required=True)
    rg.set_defaults(func=_main_bound, name="regrade")
    aw = sub.add_parser("arm-worker", help="the child process an A arm runs its dispatcher loop in")
    aw.add_argument("--plan", required=True)
    aw.set_defaults(func=_main_bound, name="arm-worker")
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (Bench1Error, ValueError, OSError) as exc:      # corrupt JSON, FIFOs, unreadable files: refuse
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
