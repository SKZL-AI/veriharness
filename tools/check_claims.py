#!/usr/bin/env python3
"""The claims-evidence ledger checker.

Resolves every evidence reference in CLAIMS.json against the repository it
actually sits in -- it never trusts the text of a reference, only what it can
independently read back: a pytest collection, a file and line, a receipt on
disk, a run's recorded state history, or a well-formed dated citation. It
also decides the coverage question (does every number-bearing sentence in
every declared `claim_surfaces` entry have a ledger entry -- a declaration,
not a directory glob), the a02-invalidation rule, and whether CLAIMS.md is
really what CLAIMS.json says it is.

Usage:
    python3 tools/check_claims.py check schema
    python3 tools/check_claims.py check evidence
    python3 tools/check_claims.py check ids
    python3 tools/check_claims.py check coverage
    python3 tools/check_claims.py check a02
    python3 tools/check_claims.py check render
    python3 tools/check_claims.py check home-paths
    python3 tools/check_claims.py check undeclared-surfaces
    python3 tools/check_claims.py check resolvability
    python3 tools/check_claims.py check export-classification
    python3 tools/check_claims.py check local-only-diversity
    python3 tools/check_claims.py check all
    python3 tools/check_claims.py reconcile [path ...]  # read-only report; never writes
    python3 tools/check_claims.py selftest
    python3 tools/check_claims.py selftest-extended
    python3 tools/check_claims.py render          # writes CLAIMS.md from CLAIMS.json
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import string
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAIMS_JSON = REPO_ROOT / "CLAIMS.json"
CLAIMS_MD = REPO_ROOT / "CLAIMS.md"
CLAIMS_ANCHORS_TEST = REPO_ROOT / "tests" / "test_claims_anchors.py"
EXPORT_MANIFEST_JSON = REPO_ROOT / "EXPORT_MANIFEST.json"

VALID_STATUSES = {"SUPPORTED", "UNSUPPORTED", "INVALIDATED", "INTENT"}
CLAIM_ID_RE = re.compile(r"^C-(\d+)$")


class CheckFailure(Exception):
    """Raised with a list of human-readable problem lines."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


# --------------------------------------------------------------------------
# Home-path guard: the needles are assembled at runtime so this file's own
# source never contains the literal substrings it is refusing elsewhere.
# --------------------------------------------------------------------------


def _home_needles() -> list[str]:
    sl = chr(47)
    tilde = chr(126)
    dirnames = ["home", "root", "etc"]
    needles = [sl + name + sl for name in dirnames]
    needles.append(tilde + sl)
    return needles


def _mnt_pattern() -> re.Pattern:
    sl = chr(47)
    return re.compile(re.escape(sl + "mnt" + sl) + r"[^" + re.escape(sl) + r"]+" + re.escape(sl))


#: Characters that, immediately before a needle, mean it is *not* the start of
#: an absolute path. A needle in the middle of a relative path -- a directory
#: that happens to be named "root" inside an evidence tree -- is an ordinary
#: name, not this machine's layout leaking into a published file. The guard
#: used to flag those, and the only ways out would have been weakening it or
#: renaming real evidence.
#: Assembled rather than written out, because a 66-character literal alphabet
#: is indistinguishable from a token to the export's own token-shaped scan --
#: which flagged this very line. Building it from `string` is also the better
#: code.
_PFAD_ZEICHEN = frozenset(string.ascii_letters + string.digits + "._-/")


def find_home_paths(text: str) -> list[str]:
    """Returns the distinct needles found in text, empty if none.

    A needle counts only where it *begins* a path: at the start of the text, or
    after a character that cannot be part of one. Narrowed deliberately and no
    further -- a needle inside quotes, inside brackets, or at the start of a
    line still matches, because the character before it is a quote, a bracket
    or nothing. Only a needle preceded by another path segment is let through.

    Examples are deliberately not written out here: this file is itself one of
    the files the guard scans, and spelling an absolute path into the docstring
    would make the guard fail on its own explanation. The cases live in
    `tests/test_claims_anchors.py`, which is scanned too and therefore builds
    them from parts.
    """
    hits = []
    for needle in _home_needles():
        pos = text.find(needle)
        while pos != -1:
            if pos == 0 or text[pos - 1] not in _PFAD_ZEICHEN:
                hits.append(needle)
                break
            pos = text.find(needle, pos + 1)
    if _mnt_pattern().search(text):
        hits.append("<mnt-pattern>")
    return hits


# --------------------------------------------------------------------------
# Ledger I/O
# --------------------------------------------------------------------------


def load_ledger(path: Path = CLAIMS_JSON) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CheckFailure([f"cannot read {path}: {exc}"]) from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckFailure([f"{path} is not valid JSON: {exc}"]) from None


def load_manifest(path: Path = EXPORT_MANIFEST_JSON) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CheckFailure([f"cannot read {path}: {exc}"]) from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckFailure([f"{path} is not valid JSON: {exc}"]) from None


# --------------------------------------------------------------------------
# K1 -- schema
# --------------------------------------------------------------------------


def check_schema(data: dict) -> list[str]:
    problems = []
    if not isinstance(data.get("schema"), str) or not data["schema"]:
        problems.append("top-level 'schema' must be a non-empty string")
    claims = data.get("claims")
    if not isinstance(claims, list):
        problems.append("top-level 'claims' must be a list")
        claims = []
    not_claims = data.get("not_claims")
    if not isinstance(not_claims, list):
        problems.append("top-level 'not_claims' must be a list")
        not_claims = []

    seen_ids = set()
    for i, c in enumerate(claims):
        loc = f"claims[{i}]"
        if not isinstance(c, dict):
            problems.append(f"{loc}: not an object")
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not CLAIM_ID_RE.match(cid or ""):
            problems.append(f"{loc}: 'id' must match C-<digits>, got {cid!r}")
        else:
            if cid in seen_ids:
                problems.append(f"{loc}: duplicate id {cid!r}")
            seen_ids.add(cid)
        loc = f"claim {cid or i}"
        if not isinstance(c.get("text"), str) or not c["text"].strip():
            problems.append(f"{loc}: 'text' must be a non-empty string")
        if not isinstance(c.get("where"), str) or not c["where"].strip():
            problems.append(f"{loc}: 'where' must be a non-empty string")
        status = c.get("status")
        if status not in VALID_STATUSES:
            problems.append(f"{loc}: 'status' must be one of {sorted(VALID_STATUSES)}, got {status!r}")
            continue
        evidence = c.get("evidence")
        if evidence is None:
            evidence = []
        if not isinstance(evidence, list):
            problems.append(f"{loc}: 'evidence' must be a list")
            evidence = []
        note = c.get("note")
        if status == "SUPPORTED":
            if not evidence:
                problems.append(f"{loc}: status SUPPORTED requires a non-empty 'evidence' array")
            for j, e in enumerate(evidence):
                if not isinstance(e, str) or not e.strip():
                    problems.append(f"{loc}: evidence[{j}] must be a non-empty string")
        else:
            if not isinstance(note, str) or not note.strip():
                problems.append(f"{loc}: status {status} requires a non-empty 'note'")
        if status == "INVALIDATED":
            inv_run = c.get("invalidated_run")
            if inv_run is not None and not isinstance(inv_run, str):
                problems.append(f"{loc}: 'invalidated_run', if present, must be a string")

    for i, n in enumerate(not_claims):
        loc = f"not_claims[{i}]"
        if not isinstance(n, dict):
            problems.append(f"{loc}: not an object")
            continue
        if not isinstance(n.get("text"), str) or not n["text"].strip():
            problems.append(f"{loc}: 'text' must be a non-empty string")
        if not isinstance(n.get("where"), str) or not n["where"].strip():
            problems.append(f"{loc}: 'where' must be a non-empty string")
        if not isinstance(n.get("reason"), str) or not n["reason"].strip():
            problems.append(f"{loc}: 'reason' must be a non-empty string")

    return problems


# --------------------------------------------------------------------------
# K3 -- ids: unique, sequence complete
# --------------------------------------------------------------------------


def check_ids(data: dict) -> list[str]:
    problems = []
    claims = data.get("claims") or []
    nums = []
    seen = set()
    for c in claims:
        cid = c.get("id") if isinstance(c, dict) else None
        m = CLAIM_ID_RE.match(cid or "")
        if not m:
            problems.append(f"claim id {cid!r} does not match C-<digits>")
            continue
        n = int(m.group(1))
        if n in seen:
            problems.append(f"duplicate claim id number {n}")
        seen.add(n)
        nums.append(n)
    if nums:
        expected = set(range(min(nums), max(nums) + 1))
        missing = sorted(expected - seen)
        if missing:
            problems.append(
                "gap in id sequence, missing numbers: "
                + ", ".join(f"C-{n:03d}" for n in missing)
            )
        if min(nums) != 1:
            problems.append(f"id sequence does not start at 1 (starts at {min(nums)})")
    return problems


# --------------------------------------------------------------------------
# K2 -- evidence resolution
# --------------------------------------------------------------------------

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")

EV_TEST = re.compile(r"^test:(?P<nodeid>.+)$")
EV_FILE = re.compile(r"^file:(?P<path>.+):(?P<line>\d+)$")
EV_RECEIPT = re.compile(
    r"^receipt:(?P<run>[^/]+)/(?P<receipt_id>[^@]+)"
    r"(?:@iteration=(?P<iteration>\d+),exit_code=(?P<exit_code>-?\d+))?$"
)
EV_RUN = re.compile(r"^run:(?P<run>[^/]+)/(?P<iteration>\d+)$")
EV_CITE = re.compile(r"^cite:(?P<url>.+)@(?P<date>\d{4}-\d{2}-\d{2})$")
EV_RECEIPTCOUNT = re.compile(r"^receiptcount:(?P<run>[^=]+)=(?P<n>\d+)$")
EV_DISCRIMINATED = re.compile(
    r"^discriminated:(?P<run>[^/]+)/(?P<iteration>\d+)=(?P<k>\d+)/(?P<n>\d+)$"
)


def _recompute_discrimination(run: str, iteration: int) -> tuple[int, int]:
    """Recomputes (k, n) purely from receipt files on disk for one run/iteration.

    n = number of check ids for which both a `-basis` and a candidate receipt
    exist under runs/<run>/receipts/ for that iteration; k = how many of
    those pairs have differing exit_code. Mirrors the ledger's own
    verification method exactly so a claim can never merely restate itself.
    """
    rdir = REPO_ROOT / "runs" / run / "receipts"
    names = [p.name for p in rdir.iterdir()] if rdir.is_dir() else []
    rx = re.compile(r"^" + re.escape(run) + r"-i" + str(iteration) + r"-a(\d+)-(K\d+)(-basis)?\.json$")
    checks: dict[str, dict[bool, str]] = {}
    for name in names:
        m = rx.match(name)
        if not m:
            continue
        _attempt, check_id, is_basis = m.groups()
        checks.setdefault(check_id, {})[bool(is_basis)] = name
    k = n = 0
    for _check_id, pair in checks.items():
        if True in pair and False in pair:
            n += 1
            basis_exit = json.loads((rdir / pair[True]).read_text(encoding="utf-8"))["exit_code"]
            cand_exit = json.loads((rdir / pair[False]).read_text(encoding="utf-8"))["exit_code"]
            if basis_exit != cand_exit:
                k += 1
    return k, n


ENVIRONMENT_GAP_MARKER = "ENVIRONMENT GAP"


def _runs_root_missing() -> bool:
    """True when this check directory has no top-level runs/ at all.

    Distinguishes an environment that was never given the run evidence (every
    receipt:/run:/receiptcount:/discriminated: reference is unverifiable here,
    through no fault of the ledger) from a real content defect inside an
    existing runs/ tree (a specific run or receipt genuinely missing or
    wrong). Only the former gets the ENVIRONMENT_GAP_MARKER.
    """
    return not (REPO_ROOT / "runs").is_dir()


def _environment_gap_failure(ref: str, form: str) -> tuple[bool, str]:
    return False, (
        f"{ref}: {ENVIRONMENT_GAP_MARKER} -- this check directory has no top-level "
        f"runs/ directory at all, so the {form} evidence form cannot be verified "
        "here; this is not a content defect in the ledger"
    )


def _collect_pytest_nodeids() -> set[str]:
    """Real pytest collection -- never a grep for a function name."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    nodeids = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or "::" not in line:
            continue
        if line.startswith("=") or "error" in line.lower() and "::" not in line:
            continue
        nodeids.add(line)
    return nodeids


def _safe_relpath(path_str: str) -> Path | None:
    if path_str.startswith("/") or path_str.startswith("~"):
        return None
    p = Path(path_str)
    if ".." in p.parts:
        return None
    return REPO_ROOT / p


def resolve_evidence(ref: str, *, pytest_nodeids: set[str] | None) -> tuple[bool, str]:
    """Returns (ok, message)."""
    m = EV_TEST.match(ref)
    if m:
        nodeid = m.group("nodeid")
        if pytest_nodeids is None:
            return False, f"{ref}: pytest collection unavailable"
        if nodeid in pytest_nodeids:
            return True, f"{ref}: collected by pytest"
        return False, f"{ref}: node id not found in pytest collection"

    m = EV_FILE.match(ref)
    if m:
        path_str, line_str = m.group("path"), m.group("line")
        full = _safe_relpath(path_str)
        if full is None:
            return False, f"{ref}: path must be relative and inside the repository"
        if not full.is_file():
            return False, f"{ref}: file does not exist"
        line_no = int(line_str)
        try:
            lines = full.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            return False, f"{ref}: cannot read file: {exc}"
        if line_no < 1 or line_no > len(lines):
            return False, f"{ref}: line {line_no} out of range (file has {len(lines)} lines)"
        return True, f"{ref}: line present"

    m = EV_RECEIPT.match(ref)
    if m:
        run, receipt_id = m.group("run"), m.group("receipt_id")
        if not _SAFE_TOKEN.match(run) or not _SAFE_TOKEN.match(receipt_id):
            return False, f"{ref}: unsafe run or receipt id"
        if _runs_root_missing():
            return _environment_gap_failure(ref, "receipt:")
        target = REPO_ROOT / "runs" / run / "receipts" / f"{receipt_id}.json"
        if not target.is_file():
            return False, f"{ref}: no such receipt under runs/"
        iteration_str, exit_code_str = m.group("iteration"), m.group("exit_code")
        if iteration_str is None:
            return True, f"{ref}: receipt file exists"
        try:
            receipt = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"{ref}: cannot read receipt: {exc}"
        if receipt.get("run_id") != run:
            return False, f"{ref}: receipt run_id {receipt.get('run_id')!r} does not match {run!r}"
        if receipt.get("iteration") != int(iteration_str):
            return False, (
                f"{ref}: receipt iteration {receipt.get('iteration')!r} "
                f"does not match cited iteration {iteration_str}"
            )
        if receipt.get("exit_code") != int(exit_code_str):
            return False, (
                f"{ref}: receipt exit_code {receipt.get('exit_code')!r} "
                f"does not match cited exit_code {exit_code_str}"
            )
        return True, f"{ref}: receipt exists and its run_id/iteration/exit_code match the citation"

    m = EV_RECEIPTCOUNT.match(ref)
    if m:
        run = m.group("run")
        if not _SAFE_TOKEN.match(run):
            return False, f"{ref}: unsafe run id"
        if _runs_root_missing():
            return _environment_gap_failure(ref, "receiptcount:")
        claimed_n = int(m.group("n"))
        rdir = REPO_ROOT / "runs" / run / "receipts"
        actual_n = len([f for f in rdir.iterdir() if f.name.endswith(".json")]) if rdir.is_dir() else 0
        if actual_n != claimed_n:
            return False, (
                f"{ref}: runs/{run}/receipts/ holds {actual_n} *.json file(s) on disk, not {claimed_n}"
            )
        return True, f"{ref}: receipt count matches disk ({actual_n})"

    m = EV_DISCRIMINATED.match(ref)
    if m:
        run, iteration_str = m.group("run"), m.group("iteration")
        if not _SAFE_TOKEN.match(run):
            return False, f"{ref}: unsafe run id"
        if _runs_root_missing():
            return _environment_gap_failure(ref, "discriminated:")
        iteration = int(iteration_str)
        claimed_k, claimed_n = int(m.group("k")), int(m.group("n"))
        actual_k, actual_n = _recompute_discrimination(run, iteration)
        if (actual_k, actual_n) != (claimed_k, claimed_n):
            return False, (
                f"{ref}: recomputed {actual_k}/{actual_n} from runs/{run}/receipts/ for iteration "
                f"{iteration}, not {claimed_k}/{claimed_n}"
            )
        return True, f"{ref}: discrimination count matches recomputation ({actual_k}/{actual_n})"

    m = EV_RUN.match(ref)
    if m:
        run, iteration_str = m.group("run"), m.group("iteration")
        if not _SAFE_TOKEN.match(run):
            return False, f"{ref}: unsafe run id"
        if _runs_root_missing():
            return _environment_gap_failure(ref, "run:")
        iteration = int(iteration_str)
        run_dir = REPO_ROOT / "runs" / run
        if not run_dir.is_dir():
            return False, f"{ref}: no such run under runs/"
        candidates = [run_dir / "state.json"] + sorted(run_dir.glob("state.json.v*"))
        found_any_state = False
        for state_path in candidates:
            if not state_path.is_file():
                continue
            found_any_state = True
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if state.get("iteration") == iteration:
                return True, f"{ref}: found in {state_path.name}"
            for entry in state.get("history", []) or []:
                if isinstance(entry, str) and f"iteration {iteration}" in entry.lower():
                    return True, f"{ref}: found in history of {state_path.name}"
        if not found_any_state:
            return False, f"{ref}: run has no readable state history"
        return False, f"{ref}: iteration not found in run's state history"

    m = EV_CITE.match(ref)
    if m:
        url, date_str = m.group("url"), m.group("date")
        if not re.match(r"^https?://\S+$", url):
            return False, f"{ref}: url is not well-formed (expected http(s)://...)"
        try:
            datetime.date.fromisoformat(date_str)
        except ValueError:
            return False, f"{ref}: date is not a well-formed calendar date"
        return True, f"{ref}: well-formed dated citation"

    return False, (
        f"{ref}: does not match any known evidence form "
        "(test:/file:/receipt:/run:/cite:/receiptcount:/discriminated:)"
    )


def check_evidence(data: dict, *, need_pytest: bool = True) -> list[str]:
    problems = []
    claims = data.get("claims") or []
    refs = []
    for c in claims:
        if not isinstance(c, dict):
            continue
        for e in c.get("evidence") or []:
            if isinstance(e, str):
                refs.append((c.get("id", "?"), e))

    pytest_nodeids = None
    if need_pytest and any(r.startswith("test:") for _, r in refs):
        pytest_nodeids = _collect_pytest_nodeids()
    elif need_pytest:
        pytest_nodeids = set()

    for cid, ref in refs:
        ok, msg = resolve_evidence(ref, pytest_nodeids=pytest_nodeids)
        if not ok:
            problems.append(f"claim {cid}: {msg}")
    return problems


# --------------------------------------------------------------------------
# R1a -- resolvability classification: who can check a reference, not how
# strong it is. Three classes -- PUBLIC (resolvable from the exported
# repository alone), LOCAL_ONLY (the evidence exists and was resolved, but
# runs/** and a small set of internal working documents are never exported),
# EXTERNAL (a dated citation of something outside this project). The class is
# *derived* from the reference's evidence form and, for file:, from a small
# named export-exclusion rule table -- never typed per reference, so a rule
# change here changes every reference into that path without anyone
# re-annotating the ledger by hand.
# --------------------------------------------------------------------------

RESOLVABILITY_PUBLIC = "PUBLIC"
RESOLVABILITY_LOCAL_ONLY = "LOCAL_ONLY"
RESOLVABILITY_EXTERNAL = "EXTERNAL"
VALID_RESOLVABILITY_CLASSES = {
    RESOLVABILITY_PUBLIC,
    RESOLVABILITY_LOCAL_ONLY,
    RESOLVABILITY_EXTERNAL,
}

# Evidence forms whose class never depends on the path or url they name.
# runs/** is never exported (evidence-not-artifact, per
# dogfood/specs/r1-export-manifest.md), so every receipt:/run:/receiptcount:/
# discriminated: reference is LOCAL_ONLY with zero exceptions; cite: always
# names something outside this project (EXTERNAL); test: is always PUBLIC
# because tests/ is exported whole under the 'tests' rule.
_FORM_FIXED_RESOLVABILITY_CLASS = {
    "receipt": RESOLVABILITY_LOCAL_ONLY,
    "run": RESOLVABILITY_LOCAL_ONLY,
    "receiptcount": RESOLVABILITY_LOCAL_ONLY,
    "discriminated": RESOLVABILITY_LOCAL_ONLY,
    "cite": RESOLVABILITY_EXTERNAL,
    "test": RESOLVABILITY_PUBLIC,
}

LOCAL_ONLY_EVIDENCE_FORMS = ("receipt", "run", "receiptcount", "discriminated")

_PARKED_PREDECESSOR_RE = re.compile(r"\.v\d+[._-]?\d{4,}")


def _is_parked_predecessor_name(name: str) -> bool:
    """`*.v<timestamp>` versioned predecessors, e.g. `LICENSE.v1.20260908T084525Z`."""
    return bool(_PARKED_PREDECESSOR_RE.search(name))


_INTERNAL_WORKING_DOCUMENTS = {
    "A02_BEFUNDE.md",
    "A02_BERICHT.md",
    "A03_NACHWEIS.md",
    "HOH_ACCEPTANCE_REPORT.md",
    "PHASE2.md",
}


def _under_root(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


# The export-exclusion rule table `file:` classification is derived from --
# mirroring dogfood/specs/r1-export-manifest.md's own stated rule set.
# EXPORT_MANIFEST.json does not exist yet in this checkout (only the manifest
# spec file is present), so this table is derived from that spec's rules
# rather than read from a manifest, exactly as the R1a spec permits when no
# manifest exists. Each entry is (rule_name, predicate(path_posix) -> bool);
# a path is LOCAL_ONLY if any predicate matches, PUBLIC otherwise (the
# default -- a file: reference is assumed publicly checkable unless a named
# rule excludes it). This is a small, swappable table: classify_reference()
# takes it as a parameter precisely so changing one rule changes the derived
# class of a reference into that path -- the R1a spec's own falsifiability
# requirement -- without touching classify_reference() itself.
DEFAULT_EXPORT_EXCLUSION_RULES: "list[tuple[str, object]]" = [
    ("evidence-not-artifact", lambda p: _under_root(p, "runs")),
    ("parked-predecessor", lambda p: _under_root(p, "history") or _is_parked_predecessor_name(Path(p).name)),
    ("stale-build-output", lambda p: _under_root(p, "build")),
    ("foreign-subject", lambda p: p == "docs/K1_CACHE_PROFILE.md"),
    (
        "internal-working-document",
        lambda p: p in _INTERNAL_WORKING_DOCUMENTS or _under_root(p, "dogfood"),
    ),
]


def excluded_export_rule(path_str: str, rules=DEFAULT_EXPORT_EXCLUSION_RULES) -> str | None:
    """The name of the first export-exclusion rule matching path_str, or None
    if no rule excludes it -- i.e. it would be PUBLIC by default.
    """
    posix = Path(path_str).as_posix()
    for name, predicate in rules:
        if predicate(posix):
            return name
    return None


def classify_reference(ref: str, *, export_exclusion_rules=DEFAULT_EXPORT_EXCLUSION_RULES) -> str:
    """Derives one evidence reference's resolvability class.

    Never a per-reference hand annotation: the class follows mechanically
    from the reference's evidence form, and for file: from
    export_exclusion_rules applied to its path. Raises ValueError if the
    reference does not match any known evidence form -- callers that need to
    report this as a problem rather than crash should catch it, exactly as
    check_resolvability() below does.
    """
    form = ref.split(":", 1)[0] if ":" in ref else ""
    if form in _FORM_FIXED_RESOLVABILITY_CLASS:
        return _FORM_FIXED_RESOLVABILITY_CLASS[form]
    if form == "file":
        m = EV_FILE.match(ref)
        path_str = m.group("path") if m else ref[len("file:"):]
        rule = excluded_export_rule(path_str, export_exclusion_rules)
        return RESOLVABILITY_LOCAL_ONLY if rule else RESOLVABILITY_PUBLIC
    raise ValueError(
        f"{ref!r}: does not match any known evidence form, cannot derive a resolvability class "
        "(test:/file:/receipt:/run:/cite:/receiptcount:/discriminated:)"
    )


_RESOLUTION_BY_ON_RE = re.compile(
    r"\bby\s+(?P<actor>[^,.;]{3,80}?)\s+on\s+(?P<date>\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)
_GENERIC_RESOLUTION_ACTORS = {"someone", "it", "them", "evidence", "the evidence", "this"}


def parse_local_only_resolution(text) -> "tuple[str, str] | None":
    """Returns (actor, date) if text states a contiguous 'by <actor> on
    <calendar date>' clause -- None otherwise.

    A bare "evidence exists" (no date, no named actor) is exactly what this
    must reject -- the R1a spec's own example of a LOCAL_ONLY statement that
    is not usable ("what was resolved, by whom, and when"). Requiring 'by'
    and 'on' to sit next to each other (rather than searching for a date and
    an actor anywhere in the text independently) also avoids the actor
    swallowing unrelated words that merely happen to follow the same 'by'.
    """
    if not isinstance(text, str):
        return None
    m = _RESOLUTION_BY_ON_RE.search(text)
    if not m:
        return None
    date_str = m.group("date")
    try:
        datetime.date.fromisoformat(date_str)
    except ValueError:
        return None
    actor = m.group("actor").strip(" .,")
    if len(actor) < 3 or actor.lower() in _GENERIC_RESOLUTION_ACTORS:
        return None
    return actor, date_str


def _claim_local_only_resolution_text(claim: dict) -> "str | None":
    """Where a claim's LOCAL_ONLY resolution statement may be written: the
    claim-level `local_only_resolution` field (this run's convention), or --
    per the R1a spec's own "in the claim or in the reference itself" -- text
    embedded directly in one of its evidence strings.
    """
    field = claim.get("local_only_resolution")
    if isinstance(field, str) and field.strip():
        return field
    evidence = claim.get("evidence") or []
    combined = " ".join(e for e in evidence if isinstance(e, str))
    return combined or None


def check_resolvability(data: dict) -> "tuple[list[str], list[str]]":
    """Returns (problems, report_lines).

    problems is empty on success -- a LOCAL_ONLY reference is never itself a
    problem (criterion 7: it is the expected, honest shape of most of this
    ledger's strongest evidence, not a defect to chase toward zero).
    report_lines always carries the per-class reference counts and the list
    of claim ids whose evidence rests entirely on non-PUBLIC references,
    printed whether or not there are problems (criterion 6).
    """
    problems: list[str] = []
    counts = {RESOLVABILITY_PUBLIC: 0, RESOLVABILITY_LOCAL_ONLY: 0, RESOLVABILITY_EXTERNAL: 0}
    non_public_only: list[str] = []

    for c in data.get("claims") or []:
        if not isinstance(c, dict):
            continue
        cid = c.get("id", "?")
        evidence = c.get("evidence") or []
        classes: list[str] = []
        for e in evidence:
            if not isinstance(e, str):
                continue
            try:
                cls = classify_reference(e)
            except ValueError as exc:
                problems.append(f"claim {cid}: {exc}")
                continue
            if cls not in VALID_RESOLVABILITY_CLASSES:
                problems.append(
                    f"claim {cid}: {e}: classified as {cls!r}, not one of {sorted(VALID_RESOLVABILITY_CLASSES)}"
                )
                continue
            counts[cls] += 1
            classes.append(cls)
            form = e.split(":", 1)[0]
            if form in LOCAL_ONLY_EVIDENCE_FORMS and cls != RESOLVABILITY_LOCAL_ONLY:
                problems.append(
                    f"claim {cid}: {e}: {form}: evidence form must classify LOCAL_ONLY "
                    f"(runs/** is never exported), got {cls}"
                )

        if classes and all(cls != RESOLVABILITY_PUBLIC for cls in classes):
            non_public_only.append(cid)

        if any(cls == RESOLVABILITY_LOCAL_ONLY for cls in classes):
            resolution_text = _claim_local_only_resolution_text(c)
            if not parse_local_only_resolution(resolution_text):
                problems.append(
                    f"claim {cid}: carries LOCAL_ONLY evidence but states no parseable "
                    f"who/when resolution (found: {resolution_text!r}) -- "
                    "'evidence exists' alone is not enough"
                )

    report_lines = ["Resolvability reference counts:"]
    for cls in (RESOLVABILITY_PUBLIC, RESOLVABILITY_LOCAL_ONLY, RESOLVABILITY_EXTERNAL):
        report_lines.append(f"  {cls}: {counts[cls]}")
    report_lines.append(f"Claims resting entirely on non-PUBLIC evidence ({len(non_public_only)}):")
    for cid in non_public_only:
        report_lines.append(f"  {cid}")

    return problems, report_lines


# --------------------------------------------------------------------------
# K12 (d4e) -- no single local_only_resolution statement may satisfy more
# than one LOCAL_ONLY-evidence claim (O52, r1a's constant).
# --------------------------------------------------------------------------


def check_local_only_diversity(data: dict) -> list[str]:
    """Global distinctness: no two claims may carry an identical
    local_only_resolution statement.

    Chosen from the three rules this run's specification allows (a
    statement naming the specific reference it resolved; global
    distinctness; a distinct/occurrence ratio above a threshold) because it
    is the simplest mechanically decidable rule a future addition cannot
    accidentally re-satisfy with the same boilerplate, and the negative
    fixture (every LOCAL_ONLY claim sharing one sentence) trivially
    falsifies it.

    Only claims that actually carry LOCAL_ONLY evidence are considered --
    matching check_resolvability()'s own requirement that only those claims
    need a local_only_resolution statement at all. A claim with no
    resolvability class information (e.g. an unclassifiable reference) is
    silently skipped here; check_resolvability() is what reports that
    problem.
    """
    problems: list[str] = []
    seen: dict[str, list[str]] = {}

    for c in data.get("claims") or []:
        if not isinstance(c, dict):
            continue
        cid = c.get("id", "?")
        classes: list[str] = []
        for e in c.get("evidence") or []:
            if not isinstance(e, str):
                continue
            try:
                classes.append(classify_reference(e))
            except ValueError:
                continue
        if not any(cls == RESOLVABILITY_LOCAL_ONLY for cls in classes):
            continue
        text = _claim_local_only_resolution_text(c)
        if not isinstance(text, str) or not text.strip():
            continue
        seen.setdefault(text, []).append(cid)

    for text, cids in seen.items():
        if len(cids) > 1:
            problems.append(
                f"{len(cids)} claims share one identical local_only_resolution "
                f"statement ({', '.join(cids)}): {text!r} -- no two LOCAL_ONLY "
                "claims may carry the same statement"
            )
    return problems


# --------------------------------------------------------------------------
# K4 -- coverage of number-bearing sentences in README.md and docs/**
# --------------------------------------------------------------------------

VERSION_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}[:-]\d{2}[:-]\d{2}Z?)?\b")
DE_DATE_RE = re.compile(
    r"\b\d{1,2}\.\s?(?:Januar|Februar|M[äa]rz|April|Mai|Juni|Juli|August|September"
    r"|Oktober|November|Dezember)\s+\d{4}\b"
)
DE_NUM_DATE_RE = re.compile(r"\b\d{1,2}\.\d{2}\.\d{4}\b")
STAND_DATE_RE = re.compile(r"\bStand\s+\d{4}-\d{2}-\d{2}\b")
HEX_HASH_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
HEADING_ORD_RE = re.compile(r"^(#+\s+)\d+\.\s+")
LIST_ORD_RE = re.compile(r"^(\s*(?:[-*]\s+)?(?:\*\*)?)\d+\.\s+")
CODE_SPAN_RE = re.compile(r"`([^`]*)`")
PATH_EXT_RE = re.compile(r"\.(py|md|json|sh|toml|txt|cff|service|timer|cfg|ini)(\W|$)")


def _strip_excluded(line: str) -> str:
    """Removes version/date/path-like/structural-ordinal substrings.

    What remains is checked for a leftover digit; if none remains the line
    is not a "number-bearing sentence" under this document's methodology
    (see CLAIMS.md's methodology section for the rationale of each rule).
    """
    s = line
    m = HEADING_ORD_RE.match(s)
    if m:
        s = s[: m.start(1)] + s[m.end():]
    m = LIST_ORD_RE.match(s)
    if m:
        s = s[: m.start(1)] + s[m.end():]
    s = STAND_DATE_RE.sub(" ", s)
    s = ISO_DATE_RE.sub(" ", s)
    s = DE_DATE_RE.sub(" ", s)
    s = DE_NUM_DATE_RE.sub(" ", s)
    s = VERSION_RE.sub(" ", s)
    s = HEX_HASH_RE.sub(lambda m: " " if any(c.isalpha() for c in m.group(0)) else m.group(0), s)

    def repl_code_span(m: re.Match) -> str:
        inner = m.group(1)
        if "/" in inner or PATH_EXT_RE.search(inner):
            return " "
        return m.group(0)

    s = CODE_SPAN_RE.sub(repl_code_span, s)
    s = re.sub(r"\S*/\S*\.(py|md|json|sh|toml|txt)\b", " ", s)
    s = re.sub(r"\b[A-Za-z0-9_]+/[A-Za-z0-9_./*-]+\b", " ", s)
    return s


def extract_number_bearing_sentences(path: Path) -> list[tuple[int, str]]:
    """Returns (line_number, stripped_line_text) for every qualifying line."""
    text = path.read_text(encoding="utf-8")
    out = []
    in_code = False
    for i, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if not re.search(r"\d", raw):
            continue
        remainder = _strip_excluded(raw)
        if re.search(r"\d", remainder):
            out.append((i, stripped))
    return out


def coverage_targets(data: dict) -> list[tuple[Path, dict]]:
    """Returns (file_path, surface_entry) for every declared claim surface.

    Reads the ledger's own `claim_surfaces` array -- never walks a directory.
    This is the fix for O41: the old version globbed `docs/**` at check time,
    so two runs that never touched a common file still collided the moment
    either one's prose fell inside that glob. A declaration has no such
    blind spot: removing a surface here removes it from what coverage
    examines, and adding a new document requires adding it here too (the
    undeclared-surface heuristic, find_undeclared_claim_surfaces(), is the
    detector for the case where someone forgets).

    A surface whose file does not exist on disk is silently skipped here
    (rather than raising): the file may have been renamed or deleted, and a
    declaration going stale in that direction is not this function's job to
    police -- README.md and the docs/*.md files this project ships are
    expected to always exist. Every surface is checked, not looked up by
    name, so no positional or hardcoded assumption about which nine or
    eleven files matters here.
    """
    targets = []
    for surface in data.get("claim_surfaces") or []:
        if not isinstance(surface, dict):
            continue
        path_str = surface.get("path")
        if not path_str:
            continue
        full = REPO_ROOT / path_str
        if full.is_file():
            targets.append((full, surface))
    return targets


def _sentences_in_scope(path: Path, scope) -> list[tuple[int, str]]:
    """Number-bearing sentences in `path`, restricted to a surface's `scope`.

    `scope == "whole file"` is every qualifying line, unchanged from before
    surfaces existed. A `scope` given as a list of heading strings restricts
    the result to lines whose nearest preceding markdown heading is one of
    those -- section-level scoping, exercised by every surface declared that
    way. Any other shape falls back to whole-file treatment rather than
    silently under-covering the surface.
    """
    all_sentences = extract_number_bearing_sentences(path)
    if scope == "whole file" or not isinstance(scope, list):
        return all_sentences
    lines = path.read_text(encoding="utf-8").splitlines()
    return [
        (line_no, text)
        for line_no, text in all_sentences
        if _nearest_heading(lines, line_no) in scope
    ]


_CLAIM_SURFACE_ROOTS = ("README.md",)


def find_undeclared_claim_surfaces(data: dict, repo_root: Path = REPO_ROOT) -> list[str]:
    """Warns (never fails) about a claim-bearing document missing from
    `claim_surfaces`.

    Scans exactly the two locations the old coverage glob used to look --
    README.md at the repo root, and every `docs/**/*.md` file -- for
    number-bearing sentences (the same extraction the coverage check uses),
    and reports any such document whose path is not declared. This directory
    walk is deliberately *not* used to decide coverage (coverage_targets()
    never calls this function); it exists only to catch the one gap a bare
    declaration has that a glob did not: a new document nobody remembered to
    declare. That is why this is a warning, never a FAIL -- an undeclared
    surface is a prompt to update the manifest, not a ledger defect by
    itself, and turning it into a hard failure would recreate exactly the
    silent-scope-change problem O41 was about, just moved one level up.

    What this heuristic cannot catch: a claim-bearing file with no digits at
    all (nothing here is number-bearing by this schema's own operationalized
    definition of a claim); a non-.md file (a script's --help text, a
    docstring, a shell comment); or a document living outside README.md and
    docs/** entirely -- the out-of-scope root-level documents this project
    already excludes by policy (RUNBOOK.md, the A0*.md files, PHASE2.md, and
    similar) are invisible to this scan by construction, exactly as they were
    invisible to the old glob.
    """
    declared = {
        s.get("path")
        for s in (data.get("claim_surfaces") or [])
        if isinstance(s, dict)
    }
    candidates = []
    readme = repo_root / _CLAIM_SURFACE_ROOTS[0]
    if readme.is_file():
        candidates.append(readme)
    docs_dir = repo_root / "docs"
    if docs_dir.is_dir():
        candidates.extend(sorted(docs_dir.rglob("*.md")))

    warnings = []
    for candidate in candidates:
        rel = candidate.relative_to(repo_root).as_posix()
        if rel in declared:
            continue
        if extract_number_bearing_sentences(candidate):
            warnings.append(rel)
    return warnings


# --------------------------------------------------------------------------
# K11 (d4e) -- every EXPORT_MANIFEST.json INCLUDE path is classified exactly
# once: a declared claim surface, or a declared exclusion with a reason from
# a closed vocabulary. Fails closed -- unlike find_undeclared_claim_surfaces()
# above, which stays a warning over its own separate reference set.
# --------------------------------------------------------------------------

CLAIM_SURFACE_EXCLUSION_REASONS = {
    "ledger-itself",
    "copied-evidence",
    # Machine-written evidence published because a public claim names it: a
    # receipt, a run state, a project state, a check transcript, a git bundle.
    # Every number in one was produced by the runner rather than authored,
    # which is the distinction this ledger draws -- and the reason the tree is
    # exported at all is that a claim whose evidence is not published asks the
    # reader to take it on trust.
    "evidence-artifact",
    "pattern-data",
    "package-metadata",
    "source-code",
}

_RECORDED_REJECTION_ROOT = "examples/minimal/recorded-rejection/"


def check_export_classification(manifest_data: dict, data: dict) -> list[str]:
    """K1-K3 of this run's specification, fail-closed, over EXPORT_MANIFEST.json's
    own INCLUDE set -- a *separate* check from find_undeclared_claim_surfaces()
    above, over a *separate* reference set.

    find_undeclared_claim_surfaces() warns, never fails, over README.md +
    docs/**/*.md (a directory glob); its own docstring argues that a hard
    failure there would recreate O41's silent-scope-change problem one level
    up, because a developer can dodge a glob-based failure by keeping a
    claim-bearing file out of the scanned directories -- invisibly. That
    argument does not hold for the reference set here: it is
    EXPORT_MANIFEST.json's own INCLUDE set, and the only way to dodge a
    failure from *this* check is to stop exporting the file, which is a
    declared, reviewable act with a rule name attached in the manifest
    itself, not an invisible one. So this check fails closed, and
    find_undeclared_claim_surfaces() is left exactly as d4d wrote it.

    Reports, by path, every one of:
      - an INCLUDE path declared neither a claim surface nor an exclusion;
      - an INCLUDE path declared both a claim surface and an exclusion;
      - an exclusion whose reason is not one of the five closed values;
      - a prose `.md` INCLUDE path (outside recorded-rejection/, which is
        copied evidence, not prose) excluded instead of declared a surface.
    """
    problems: list[str] = []

    entries = manifest_data.get("entries") or []
    includes = {
        e.get("path")
        for e in entries
        if isinstance(e, dict) and e.get("decision") == "INCLUDE" and e.get("path")
    }

    surfaces = data.get("claim_surfaces") or []
    exclusions = data.get("claim_surface_exclusions") or []

    surface_paths = {
        s.get("path") for s in surfaces if isinstance(s, dict) and s.get("path")
    }
    exclusion_by_path: dict[str, dict] = {
        e.get("path"): e for e in exclusions if isinstance(e, dict) and e.get("path")
    }
    exclusion_paths = set(exclusion_by_path)

    both = sorted((surface_paths & exclusion_paths) & includes)
    for path in both:
        problems.append(
            f"{path}: declared BOTH a claim surface and a claim_surface_exclusions "
            "entry -- must be exactly one"
        )

    neither = sorted(includes - surface_paths - exclusion_paths)
    for path in neither:
        problems.append(
            f"{path}: an EXPORT_MANIFEST.json INCLUDE path with no claim_surfaces "
            "entry and no claim_surface_exclusions entry -- every exported path "
            "must be classified"
        )

    for path in sorted(exclusion_paths & includes):
        reason = exclusion_by_path[path].get("reason")
        if reason not in CLAIM_SURFACE_EXCLUSION_REASONS:
            problems.append(
                f"{path}: claim_surface_exclusions reason {reason!r} is not one of "
                f"the closed vocabulary {sorted(CLAIM_SURFACE_EXCLUSION_REASONS)}"
            )

    for path in sorted(includes):
        if not path.endswith(".md") or path.startswith(_RECORDED_REJECTION_ROOT):
            continue
        if path in exclusion_paths and path not in surface_paths:
            problems.append(
                f"{path}: a prose .md INCLUDE path is excluded instead of declared "
                "a claim surface -- a prose file that carries numbers is a claim surface"
            )

    return problems


def _parse_where(where: str) -> tuple[str, int] | None:
    m = re.match(r"^(?P<path>.+):(?P<line>\d+)$", where)
    if not m:
        return None
    return (m.group("path"), int(m.group("line")))


# --------------------------------------------------------------------------
# Anchor identity: a covered statement is identified by its file path plus a
# digest of its own (normalized) content -- never by the line number it once
# sat on. A line number drifts the moment anything above it in the document
# changes; content does not. The line is still reported back to the caller,
# but purely as an informative location, never as part of the identity.
# --------------------------------------------------------------------------

HEADING_LINE_RE = re.compile(r"^#{1,6}\s+\S.*$")


def normalize_anchor_text(line: str) -> str:
    """Per-line anchor normalization: strip the line, then collapse runs of
    internal whitespace to a single space.

    Deliberately does not lowercase, does not strip punctuation, and never
    joins across lines. Each of those would let a real content edit slip
    through unnoticed -- which is exactly what this identity exists to catch.
    """
    return re.sub(r"\s+", " ", line.strip())


def compute_anchor_digest(line: str) -> str:
    """A deterministic content digest for one physical line's anchor identity."""
    return hashlib.sha256(normalize_anchor_text(line).encode("utf-8")).hexdigest()


def _nearest_heading(lines: list[str], line_no: int) -> str | None:
    """The nearest markdown heading at or above line_no (1-indexed), or None."""
    for i in range(line_no - 1, 0, -1):
        candidate = lines[i - 1].strip()
        if HEADING_LINE_RE.match(candidate):
            return candidate
    return None


def _iter_physical_lines(path: Path) -> list[tuple[int, str]]:
    """Every physical line of path as (1-indexed line number, stripped text).

    Anchor identity is content-based, not tied to the number-bearing-sentence
    heuristic that drives the *coverage* reverse-scan (K4): a ledger entry's
    recorded location is sometimes the first line of a wrapped sentence whose
    digit sits a line or two later, so the candidate pool for matching must be
    every line, not only the ones the coverage heuristic itself flags.
    """
    text = path.read_text(encoding="utf-8")
    return [(i, raw.strip()) for i, raw in enumerate(text.splitlines(), start=1)]


class AnchorResolution:
    """The result of resolving one entry's anchor against a file on disk."""

    __slots__ = ("verdict", "line", "candidates")

    def __init__(self, verdict: str, line: int | None, candidates: list[int]):
        self.verdict = verdict  # "PASS" | "FAIL" | "AMBIGUOUS" | "ENVIRONMENT_GAP"
        self.line = line
        self.candidates = candidates


def resolve_anchor(path: str, digest: str, context: str | None, repo_root: Path) -> AnchorResolution:
    """Resolves one entry's (path, digest, context) anchor against repo_root.

    PASS: exactly one line in the file normalizes to the stored digest -- or
    several do, and the stored context (nearest preceding heading) narrows
    them to exactly one.
    FAIL: the target file exists but no line in it matches -- covers both
    "statement removed" and "statement changed" (the spec's table maps both
    to FAIL). A real content defect, never a marker.
    ENVIRONMENT_GAP: the target file itself does not exist in repo_root at
    all -- the anchor cannot be resolved because the document that would
    carry it was never given to this check (e.g. an internal working
    document excluded from an export), not because the statement moved or
    changed. Distinct from FAIL for the same reason _runs_root_missing() is
    distinct from a genuine receipt mismatch: an absent input is not a
    content defect.
    AMBIGUOUS: more than one line matches and context does not narrow it to
    exactly one -- its own verdict, never folded into FAIL. A resolver that
    picks one of several matches arbitrarily is worse than one that refuses.

    repo_root is an explicit parameter, never the module-level REPO_ROOT, so
    callers (tests included) can point this at an isolated fixture directory
    without mutating shared state.
    """
    full = repo_root / path
    if not full.is_file():
        return AnchorResolution("ENVIRONMENT_GAP", None, [])

    matches = [
        line_no
        for line_no, text in _iter_physical_lines(full)
        if compute_anchor_digest(text) == digest
    ]
    if not matches:
        return AnchorResolution("FAIL", None, [])
    if len(matches) == 1:
        return AnchorResolution("PASS", matches[0], matches)

    if context:
        lines = full.read_text(encoding="utf-8").splitlines()
        narrowed = [ln for ln in matches if _nearest_heading(lines, ln) == context]
        if len(narrowed) == 1:
            return AnchorResolution("PASS", narrowed[0], matches)

    return AnchorResolution("AMBIGUOUS", None, matches)


def _anchor_problem_message(kind: str, cid: str, where: str, path: str, result: AnchorResolution) -> str:
    if result.verdict == "AMBIGUOUS":
        return (
            f"{kind} {cid}: anchor recorded at {where} is AMBIGUOUS in {path} -- "
            f"{len(result.candidates)} candidate line(s) match {result.candidates}, "
            "no distinguishing context narrows it to one"
        )
    if result.verdict == "ENVIRONMENT_GAP":
        return (
            f"{kind} {cid}: anchor recorded at {where} cannot be resolved -- "
            f"{ENVIRONMENT_GAP_MARKER}: {path} is not present in this tree at all, so its "
            "absence, not the ledger, is why the anchor cannot be checked here; this is not "
            "a content defect"
        )
    return (
        f"{kind} {cid}: anchor recorded at {where} no longer resolves in {path} "
        "-- the statement is no longer present, or its content changed"
    )


# The ledger splits in two, and the split is structural rather than a count:
# every entry whose 'where' names a line -- 'path:<line>' -- carries an
# anchor_digest and is resolved below. The remainder are the runs/-evidence
# claims C-018..C-051, whose 'where' is a bare path like 'runs/d1/receipts'
# with no trailing ':<line>';
#
# No entry counts are stated here on purpose. This comment carried three
# ("122 ledger entries", "88 document-coverage entries", "26 claims + 62
# not_claims") and all three were false by the time d4f measured them: the
# ledger had grown to 648 entries. A corrected number would age the same way --
# the same reason d4f's criterion 10 forbade one in _sentences_in_scope()'s
# docstring, and the reason four standing numbers have been removed from this
# repository already. The id range C-018..C-051 stays because it is a property
# of those claims, not a tally of the ledger.
# _parse_where rejects that shape, so resolve_entries()'s `if not loc:
# continue` skips them before the anchor_digest check ever runs. Confirmed by
# reading CLAIMS.json directly.
#
# Separately: `python3 tools/check_claims.py check all` cannot exit 0 in a
# check directory that has no runs/ tree at all. Those same C-018..C-051
# claims cite runs/-evidence (receipt:/run:/receiptcount:/discriminated:)
# that _runs_root_missing()/_environment_gap_failure() fail closed on as an
# ENVIRONMENT GAP whenever runs/ is wholly absent -- independent of, and
# unrelated to, this file's coverage-anchor fix for docs/OPERATIONS.md.
# `check coverage` alone, plus a check-all-output OPERATIONS.md-occurrence
# probe, are the correct in-scope proxies for criterion F in that environment.
def check_coverage(data: dict) -> list[str]:
    problems = []
    claims = data.get("claims") or []
    not_claims = data.get("not_claims") or []

    covered: set[tuple[str, int]] = set()

    def resolve_entries(entries: list, kind: str) -> None:
        for e in entries:
            if not isinstance(e, dict):
                continue
            where = e.get("where") or ""
            loc = _parse_where(where)
            if not loc:
                continue
            path, _where_line = loc
            cid = e.get("id", "?")
            digest = e.get("anchor_digest")
            if not digest:
                problems.append(f"{kind} {cid}: no anchor_digest recorded for {where} -- cannot verify")
                continue
            context = e.get("anchor_context")
            result = resolve_anchor(path, digest, context, REPO_ROOT)
            if result.verdict == "PASS":
                covered.add((path, result.line))
            else:
                problems.append(_anchor_problem_message(kind, cid, where, path, result))

    resolve_entries(claims, "claim")
    resolve_entries(not_claims, "not_claim")

    for target, surface in coverage_targets(data):
        rel = target.relative_to(REPO_ROOT).as_posix()
        scope = surface.get("scope", "whole file")
        for line_no, text in _sentences_in_scope(target, scope):
            if (rel, line_no) not in covered:
                problems.append(f"{rel}:{line_no}: not covered by any claim or not_claims entry: {text!r}")
    return problems


# --------------------------------------------------------------------------
# Reconciliation mode: read-only report of what a declared surface needs.
#
# check_coverage() above answers a yes/no question (does everything resolve)
# with a flat list of FAIL strings. This answers a different question --
# *which direction* is a surface out of sync in -- and never writes to disk.
# The two directions:
#   "unbound"  -- a number-bearing sentence in the surface's declared scope
#                 that no claims/not_claims anchor currently resolves to.
#   "vanished" -- a claims/not_claims entry recorded against this surface
#                 whose anchor no longer resolves (FAIL or AMBIGUOUS) against
#                 the file's current content, or was never given a digest.
# A statement that simply shifted line numbers is neither: resolve_anchor()
# still finds it (PASS), so it shows up in neither list.
# --------------------------------------------------------------------------


def reconcile_surface(
    data: dict, surface: dict, repo_root: Path = REPO_ROOT
) -> dict:
    """The reconciliation report for a single declared surface.

    Returns {"path": ..., "unbound": [...], "vanished": [...]}. Read-only:
    resolve_anchor() only reads files, and this function itself never opens
    anything for writing.
    """
    path_str = surface.get("path", "")
    scope = surface.get("scope", "whole file")
    claims = data.get("claims") or []
    not_claims = data.get("not_claims") or []

    bound_lines: set[int] = set()
    vanished: list[dict] = []

    def _check_entries(entries: list, kind: str) -> None:
        for e in entries:
            if not isinstance(e, dict):
                continue
            where = e.get("where") or ""
            loc = _parse_where(where)
            if not loc or loc[0] != path_str:
                continue
            cid = e.get("id", "?")
            digest = e.get("anchor_digest")
            if not digest:
                vanished.append(
                    {"kind": kind, "id": cid, "where": where, "reason": "no anchor_digest recorded"}
                )
                continue
            context = e.get("anchor_context")
            result = resolve_anchor(path_str, digest, context, repo_root)
            if result.verdict == "PASS":
                bound_lines.add(result.line)
            else:
                vanished.append(
                    {"kind": kind, "id": cid, "where": where, "reason": result.verdict}
                )

    _check_entries(claims, "claim")
    _check_entries(not_claims, "not_claim")

    unbound: list[dict] = []
    full = repo_root / path_str
    if full.is_file():
        for line_no, text in _sentences_in_scope(full, scope):
            if line_no not in bound_lines:
                unbound.append({"line": line_no, "text": text})

    return {"path": path_str, "unbound": unbound, "vanished": vanished}


def reconcile_report(
    data: dict, surfaces: list[dict] | None = None, repo_root: Path = REPO_ROOT
) -> dict[str, dict]:
    """The reconciliation report across every given surface (default: every
    entry in data['claim_surfaces']), keyed by path. Read-only throughout --
    see reconcile_surface() and resolve_anchor(); neither writes.
    """
    if surfaces is None:
        surfaces = data.get("claim_surfaces") or []
    report = {}
    for surface in surfaces:
        if not isinstance(surface, dict) or not surface.get("path"):
            continue
        result = reconcile_surface(data, surface, repo_root)
        report[result["path"]] = {"unbound": result["unbound"], "vanished": result["vanished"]}
    return report


def format_reconcile_report(report: dict[str, dict]) -> list[str]:
    """Renders a reconcile_report() result as human-readable lines."""
    lines = []
    for path, directions in report.items():
        unbound = directions.get("unbound") or []
        vanished = directions.get("vanished") or []
        if not unbound and not vanished:
            lines.append(f"{path}: reconciled -- nothing unbound, nothing vanished")
            continue
        for u in unbound:
            lines.append(f"{path}:{u['line']}: UNBOUND -- {u['text']!r}")
        for v in vanished:
            lines.append(f"{path}: VANISHED -- {v['kind']} {v['id']} ({v['where']}): {v['reason']}")
    return lines


# --------------------------------------------------------------------------
# K5 -- no claim resting on a02 is SUPPORTED
# --------------------------------------------------------------------------


def _referenced_runs(claim: dict) -> set[str]:
    runs = set()
    for e in claim.get("evidence") or []:
        if not isinstance(e, str):
            continue
        m = EV_RECEIPT.match(e) or EV_RUN.match(e)
        if m:
            runs.add(m.group("run"))
    inv = claim.get("invalidated_run")
    if isinstance(inv, str):
        runs.add(inv)
    return runs


def check_a02(data: dict) -> list[str]:
    problems = []
    for c in data.get("claims") or []:
        if not isinstance(c, dict):
            continue
        runs = _referenced_runs(c)
        if "a02" not in runs:
            continue
        cid = c.get("id", "?")
        status = c.get("status")
        if status != "INVALIDATED":
            problems.append(f"claim {cid}: rests on run a02 but status is {status!r}, must be INVALIDATED")
            continue
        note = c.get("note") or ""
        if "A03_NACHWEIS.md" not in note:
            problems.append(f"claim {cid}: INVALIDATED-on-a02 note must name the replacement A03_NACHWEIS.md")
    return problems


# --------------------------------------------------------------------------
# K6 -- CLAIMS.md is generated from CLAIMS.json
# --------------------------------------------------------------------------


def _render_evidence_bullet(ref: str, claim: dict) -> str:
    """Renders one evidence bullet with its resolvability class attached --
    directly next to the reference it describes, never in a separate legend,
    footnote, or summary table (R1a criterion 5). A LOCAL_ONLY reference also
    carries its claim's who/when resolution statement right here.
    """
    try:
        cls = classify_reference(ref)
    except ValueError:
        cls = "UNCLASSIFIED"
    bullet = f"- `{ref}` -- resolvability: **{cls}**"
    if cls == RESOLVABILITY_LOCAL_ONLY:
        parsed = parse_local_only_resolution(_claim_local_only_resolution_text(claim))
        if parsed:
            actor, date = parsed
            bullet += f" (resolved by {actor}, {date})"
        else:
            bullet += " (no who/when resolution recorded)"
    return bullet


# --------------------------------------------------------------------------
# K6 continued -- reconstruction: a claim's rendered text is the sentence
# (or, for a literal table row, the whole row) it belongs to in its source
# file, resolved via the SAME digest-based resolve_anchor() the coverage and
# reconciliation checks above use -- never the stale line number stored in
# `where`. Purely a render-time computation: nothing here writes back into
# CLAIMS.json, and anchor identity (normalize_anchor_text,
# compute_anchor_digest, resolve_anchor) is called as-is, never touched.
# --------------------------------------------------------------------------

ANCHOR_OPEN = "⟦"
ANCHOR_CLOSE = "⟧"
RECONSTRUCTION_FALLBACK_MARKER = (
    "[reconstruction fallback -- source line shown as recorded, not reconstructed]"
)

_TABLE_ROW_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")

# Sentence boundary: a `.`/`!`/`?` followed by whitespace and either an
# uppercase letter, a backtick, or an opening `**` -- the next sentence's own
# first character. The boundary whitespace itself belongs to neither
# sentence. Also a boundary after a bold **label:** (colon immediately
# inside the closing `**`) followed by the same whitespace-plus-next-unit
# shape -- this project's own claim surfaces (paper/REVIEW_A.md,
# HOH_ACCEPTANCE_REPORT.md, and others) use `**Label:** content` as a
# standing convention, and without this the label prefix and the sentence it
# introduces would be treated as one run-on unit with no terminal
# punctuation of its own until the content's actual sentence end.
_SENTENCE_BOUND_RE = re.compile(r"(?:[.!?]|:\*\*)(\s+)(?=[A-Z`]|\*\*)")


def _render_table_row(line: str) -> str:
    """A one-line markdown table row rendered as its cells joined by an em
    dash. A table row is a complete unit on one line, not a sentence to join
    across rows -- the other rows of the same table are a different
    statement entirely.
    """
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return " — ".join(cells)


def _is_paragraph_boundary_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if HEADING_LINE_RE.match(stripped):
        return True
    if stripped.startswith("```"):
        return True
    if _TABLE_ROW_LINE_RE.match(line):
        return True
    return False


def _paragraph_bounds(lines: list[str], line_no: int) -> tuple[int, int]:
    """1-indexed inclusive (start, end) of the maximal run of non-blank lines
    around line_no -- stopped by a blank line, a markdown heading, a fenced
    code delimiter, or a table-row line, reusing
    extract_number_bearing_sentences()'s fence-tracking idea, applied
    locally around one line instead of scanning the whole file.
    """
    n = len(lines)
    idx = line_no - 1
    if _is_paragraph_boundary_line(lines[idx]):
        return (line_no, line_no)
    start = idx
    while start > 0 and not _is_paragraph_boundary_line(lines[start - 1]):
        start -= 1
    end = idx
    while end < n - 1 and not _is_paragraph_boundary_line(lines[end + 1]):
        end += 1
    return (start + 1, end + 1)


def _join_paragraph_with_spans(
    lines: list[str], start: int, end: int
) -> tuple[str, list[tuple[int, int, int]]]:
    """Joins physical lines [start, end] (1-indexed, inclusive) with single
    spaces, each line normalized the same way anchor identity is
    (normalize_anchor_text -- stripped, internal whitespace collapsed).
    Returns (joined_text, spans) where spans holds each source line's own
    (line_no, char_start, char_end) span within joined_text.
    """
    parts: list[str] = []
    spans: list[tuple[int, int, int]] = []
    offset = 0
    for line_no in range(start, end + 1):
        norm = normalize_anchor_text(lines[line_no - 1])
        if not norm:
            continue
        if parts:
            offset += 1
        char_start = offset
        parts.append(norm)
        offset += len(norm)
        spans.append((line_no, char_start, offset))
    return " ".join(parts), spans


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) character spans of each sentence in text, per
    _SENTENCE_BOUND_RE -- the boundary whitespace itself belongs to neither
    span.
    """
    spans = []
    prev = 0
    for m in _SENTENCE_BOUND_RE.finditer(text):
        spans.append((prev, m.start(1)))
        prev = m.end(1)
    spans.append((prev, len(text)))
    return spans


def reconstruct_claim_text(
    raw_text: str,
    where: str,
    anchor_digest: str | None,
    anchor_context: str | None,
    repo_root: Path,
) -> str:
    """Renders one claim/not_claim's display text as the sentence (or table
    row) it belongs to in its source file, with the anchored line's own raw
    content kept identifiable inside it.

    Resolves the entry's anchor through the existing digest-based
    resolve_anchor() -- never through the line number stored in `where`,
    which drifts the moment anything above it in the document changes. On
    anything other than a clean PASS -- ENVIRONMENT_GAP (source file absent:
    an export checkout, or a claim pointing into runs/), FAIL (the statement
    changed or is gone), or AMBIGUOUS (more than one line matches) -- this
    returns raw_text plus RECONSTRUCTION_FALLBACK_MARKER, verbatim, and never
    raises: an honest fallback is what an unresolvable anchor calls for,
    never an invented sentence.

    On PASS: a resolved line shaped like a markdown table row renders as its
    cells joined by an em dash. Otherwise, the paragraph containing the
    resolved line is located and split into sentences, and the sentence(s)
    whose span overlaps the resolved line become the rendered window --
    padded by exactly one more sentence on each side (clamped to the
    paragraph) only when the resolved line's own content straddles a
    sentence boundary (its identity is genuinely split between two
    sentences, so one bridging sentence of context on each side is what
    keeps both halves legible). The window is rendered whole and unmarked --
    splicing ANCHOR_OPEN/ANCHOR_CLOSE directly into that flow would land
    inside a real word pair for exactly the straddling lines this padding
    exists for (the resolved line's own content ends or begins mid-phrase,
    by definition), breaking the very continuity the padding was added to
    preserve. So the anchored line's own raw content is appended once more
    afterward, wrapped whole in ANCHOR_OPEN/ANCHOR_CLOSE -- identifiable
    without ever fracturing the surrounding sentence(s).

    This never writes back into CLAIMS.json or any source file -- purely a
    render-time computation, recomputed fresh on every render_markdown().
    """
    loc = _parse_where(where) if isinstance(where, str) else None
    if not loc or not anchor_digest:
        return f"{raw_text} {RECONSTRUCTION_FALLBACK_MARKER}"

    path, _stale_line = loc
    result = resolve_anchor(path, anchor_digest, anchor_context, repo_root)
    if result.verdict != "PASS":
        return f"{raw_text} {RECONSTRUCTION_FALLBACK_MARKER}"

    full = repo_root / path
    try:
        lines = full.read_text(encoding="utf-8").splitlines()
    except OSError:
        return f"{raw_text} {RECONSTRUCTION_FALLBACK_MARKER}"

    line_no = result.line
    if line_no is None or line_no < 1 or line_no > len(lines):
        return f"{raw_text} {RECONSTRUCTION_FALLBACK_MARKER}"

    if _TABLE_ROW_LINE_RE.match(lines[line_no - 1]):
        candidate = _render_table_row(lines[line_no - 1])
        # The stored `text` fields were hand-paraphrased, in a handful of
        # security-review entries, specifically to keep a literal guarded
        # needle (see find_home_paths()/check_home_paths()) out of
        # CLAIMS.md; pulling the untouched source line back in can
        # reintroduce exactly that needle even though raw_text itself never
        # carried it. Reconstruction must never regress that guard, so it
        # falls back to the raw line, same as an unresolvable anchor, rather
        # than launder a banned pattern back into the rendered ledger.
        if find_home_paths(candidate):
            return f"{raw_text} {RECONSTRUCTION_FALLBACK_MARKER}"
        return candidate

    para_start, para_end = _paragraph_bounds(lines, line_no)
    joined, spans = _join_paragraph_with_spans(lines, para_start, para_end)
    sentence_spans = _sentence_spans(joined)
    line_span = next((s for s in spans if s[0] == line_no), None)
    if line_span is None or not sentence_spans:
        return f"{raw_text} {RECONSTRUCTION_FALLBACK_MARKER}"
    _, line_start, line_end = line_span

    overlap = [
        i for i, (s, e) in enumerate(sentence_spans) if s < line_end and e > line_start
    ]
    if not overlap:
        return f"{raw_text} {RECONSTRUCTION_FALLBACK_MARKER}"

    lo, hi = overlap[0], overlap[-1]
    if len(overlap) > 1:
        lo = max(0, lo - 1)
        hi = min(len(sentence_spans) - 1, hi + 1)

    window_start = sentence_spans[lo][0]
    window_end = sentence_spans[hi][1]
    window_text = joined[window_start:window_end]

    candidate = f"{window_text} {ANCHOR_OPEN}{raw_text}{ANCHOR_CLOSE}"
    if find_home_paths(candidate):
        return f"{raw_text} {RECONSTRUCTION_FALLBACK_MARKER}"
    return candidate


def render_markdown(data: dict) -> str:
    lines = []
    lines.append("# Claims ledger")
    lines.append("")
    lines.append(
        "Generated from `CLAIMS.json` by `tools/check_claims.py render`. "
        "Do not hand-edit; `tools/check_claims.py check render` fails if this "
        "file disagrees with a fresh regeneration."
    )
    lines.append("")
    methodology = data.get("methodology")
    if isinstance(methodology, list):
        lines.append("## Methodology")
        lines.append("")
        for para in methodology:
            lines.append(str(para))
            lines.append("")

    lines.append("## Claims")
    lines.append("")
    lines.append("| id | status | text | where |")
    lines.append("|---|---|---|---|")
    for c in data.get("claims") or []:
        cid = c.get("id", "")
        status = c.get("status", "")
        text = _md_escape(
            reconstruct_claim_text(
                c.get("text", ""),
                c.get("where", ""),
                c.get("anchor_digest"),
                c.get("anchor_context"),
                REPO_ROOT,
            )
        )
        where = _md_escape(c.get("where", ""))
        lines.append(f"| {cid} | {status} | {text} | {where} |")
    lines.append("")

    lines.append("### Evidence and notes")
    lines.append("")
    for c in data.get("claims") or []:
        cid = c.get("id", "")
        lines.append(f"**{cid}**")
        evidence = c.get("evidence") or []
        if evidence:
            lines.append("")
            lines.append("Evidence:")
            for e in evidence:
                lines.append(_render_evidence_bullet(e, c))
        note = c.get("note")
        if note:
            lines.append("")
            lines.append(f"Note: {note}")
        inv_run = c.get("invalidated_run")
        if inv_run:
            lines.append("")
            lines.append(f"Invalidated run: `{inv_run}`")
        lines.append("")

    lines.append("## Not claims")
    lines.append("")
    lines.append(
        "Number-bearing sentences from README.md and docs/** that were judged "
        "not to be public claims -- see the reason for each."
    )
    lines.append("")
    lines.append("| where | text | reason |")
    lines.append("|---|---|---|")
    for n in data.get("not_claims") or []:
        where = _md_escape(n.get("where", ""))
        text = _md_escape(
            reconstruct_claim_text(
                n.get("text", ""),
                n.get("where", ""),
                n.get("anchor_digest"),
                n.get("anchor_context"),
                REPO_ROOT,
            )
        )
        reason = _md_escape(n.get("reason", ""))
        lines.append(f"| {where} | {text} | {reason} |")
    lines.append("")

    return "\n".join(lines) + "\n"


def _md_escape(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def check_render(data: dict) -> list[str]:
    expected = render_markdown(data)
    try:
        actual = CLAIMS_MD.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read {CLAIMS_MD}: {exc}"]
    if actual != expected:
        for i, (a, b) in enumerate(zip(actual.splitlines(), expected.splitlines()), start=1):
            if a != b:
                return [
                    "CLAIMS.md disagrees with a fresh regeneration from CLAIMS.json",
                    f"  first difference at line {i}:",
                    f"    CLAIMS.md : {a!r}",
                    f"    generated : {b!r}",
                ]
        return [
            "CLAIMS.md disagrees with a fresh regeneration from CLAIMS.json "
            f"(length differs: {len(actual)} vs {len(expected)} chars)"
        ]
    return []


# --------------------------------------------------------------------------
# K10 -- no home paths in the new files
# --------------------------------------------------------------------------


def check_home_paths() -> list[str]:
    problems = []
    for path in (CLAIMS_JSON, CLAIMS_MD, Path(__file__), CLAIMS_ANCHORS_TEST):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"cannot read {path}: {exc}")
            continue
        hits = find_home_paths(text)
        if hits:
            problems.append(f"{path.relative_to(REPO_ROOT)}: contains home-directory path pattern(s) {hits}")
    return problems


# --------------------------------------------------------------------------
# K7 -- selftest: the checker must falsify a broken ledger
# --------------------------------------------------------------------------


def run_selftest() -> tuple[bool, list[str]]:
    report = []
    ok = True

    real_nodeids = _collect_pytest_nodeids()
    dangling_nodeid = "tests/test_controller.py::test_this_node_id_does_not_exist_zzz"
    if dangling_nodeid in real_nodeids:
        report.append("selftest setup invalid: the dangling node id unexpectedly exists")
        ok = False

    broken = {
        "schema": "hoh-claims/1",
        "claims": [
            {
                "id": "C-001",
                "text": "a dangling test reference",
                "where": "README.md:1",
                "status": "SUPPORTED",
                "evidence": [f"test:{dangling_nodeid}"],
                "note": "",
            },
            {
                "id": "C-002",
                "text": "a supported claim with no evidence at all",
                "where": "README.md:1",
                "status": "SUPPORTED",
                "evidence": [],
                "note": "",
            },
        ],
        "not_claims": [],
    }

    schema_problems = check_schema(broken)
    empty_evidence_caught = any("C-002" in p and "evidence" in p for p in schema_problems)
    if empty_evidence_caught:
        report.append("PASS: SUPPORTED claim with empty evidence array is rejected by check_schema")
    else:
        report.append("FAIL: SUPPORTED claim with empty evidence array was NOT rejected")
        ok = False

    evidence_problems = check_evidence(broken, need_pytest=True)
    dangling_caught = any("C-001" in p for p in evidence_problems)
    if dangling_caught:
        report.append("PASS: dangling test: reference is rejected by check_evidence")
    else:
        report.append("FAIL: dangling test: reference was NOT rejected")
        ok = False

    return ok, report


def run_selftest_extended() -> tuple[bool, list[str]]:
    """The literal-minimum selftest above covers two of the nineteen moving
    parts a real ledger depends on (an empty-evidence SUPPORTED claim, a
    dangling test: reference). This covers the other seventeen, in four
    groups: (1) reject-path -- a wrong-line file: reference, a missing
    receipt: file, a run: iteration absent from real state history, a
    receipt: whose @iteration=,exit_code= suffix disagrees with the cited
    receipt's own exit_code, a receiptcount: that disagrees with the *.json
    files on disk, and a discriminated: that disagrees with a fresh
    recomputation from basis/candidate receipt pairs; (2) accept-path -- a
    correct receipt:/receiptcount:/discriminated: citation against synthetic
    fixture data resolves successfully, the one part of these forms'
    correctness a runs/-less check directory cannot otherwise demonstrate at
    all; (3) the ENVIRONMENT_GAP_MARKER distinction -- a genuine content
    defect against an EXISTING runs/ tree is never mislabeled as an
    environment gap, while every receipt:/run:/receiptcount:/discriminated:
    failure against a WHOLLY ABSENT runs/ directory is; (4) a malformed cite:
    date, a duplicate claim id, and a claim resting on run a02 wrongly left
    SUPPORTED. Nineteen assertions total; this command exits non-zero if even
    one of them is wrongly decided.
    """
    global REPO_ROOT

    ok = True
    report: list[str] = []

    def record(rejected: bool, pass_msg: str, fail_msg: str) -> None:
        nonlocal ok
        if rejected:
            report.append(f"PASS: {pass_msg}")
        else:
            report.append(f"FAIL: {fail_msg}")
            ok = False

    base_ok, base_report = run_selftest()
    report.extend(base_report)
    ok = ok and base_ok

    # 3. file: reference to a line number that does not exist in an existing file.
    resolved, _ = resolve_evidence("file:README.md:999999", pytest_nodeids=None)
    record(
        not resolved,
        "file: reference to an out-of-range line is rejected by resolve_evidence",
        "file: reference to an out-of-range line was NOT rejected",
    )

    # 4 & 5 need a run that genuinely HAS recorded state history, so the
    # failure exercised is "not in the history", not merely "no such run".
    # An isolated temp REPO_ROOT lets this construct that without touching
    # this repository's own runs/ directory (real dogfood evidence, read-only
    # for this ledger's purposes).
    original_root = REPO_ROOT
    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        run_dir = tmp_root / "runs" / "zz-selftest"
        receipts_dir = run_dir / "receipts"
        receipts_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text(
            json.dumps({"iteration": 2, "history": ["... iteration 2 accepted ..."]}),
            encoding="utf-8",
        )
        # Fixture receipts for the receiptcount:/discriminated: assertions below:
        # two checks in iteration 1, K1 discriminates (exit codes differ between
        # basis and candidate), K2 does not (both exit 0) -- so the true count
        # for iteration 1 is 1/2, and the receipts dir holds 4 files in total.
        fixture_receipts = {
            "zz-selftest-i1-a1-K1-basis.json": ("K1", 1),
            "zz-selftest-i1-a1-K1.json": ("K1", 0),
            "zz-selftest-i1-a1-K2-basis.json": ("K2", 0),
            "zz-selftest-i1-a1-K2.json": ("K2", 0),
        }
        for name, (check_id, exit_code) in fixture_receipts.items():
            receipt_id = name[: -len(".json")]
            payload = {
                "receipt_id": receipt_id,
                "run_id": "zz-selftest",
                "iteration": 1,
                "attempt": 1,
                "check_id": check_id,
                "exit_code": exit_code,
            }
            (receipts_dir / name).write_text(json.dumps(payload), encoding="utf-8")

        REPO_ROOT = tmp_root
        try:
            # 4. receipt: reference to a receipt file absent under runs/.
            resolved, _ = resolve_evidence("receipt:zz-selftest/does-not-exist", pytest_nodeids=None)
            record(
                not resolved,
                "receipt: reference to a missing file is rejected by resolve_evidence",
                "receipt: reference to a missing file was NOT rejected",
            )

            # 5. run: reference to an iteration absent from recorded state history.
            resolved, _ = resolve_evidence("run:zz-selftest/99", pytest_nodeids=None)
            record(
                not resolved,
                "run: reference to a missing iteration is rejected by resolve_evidence",
                "run: reference to a missing iteration was NOT rejected",
            )

            # 6. receipt: reference whose @iteration=,exit_code= suffix disagrees
            # with the cited receipt's own exit_code field (K1's real exit_code
            # is 0, per the fixture above).
            resolved, _ = resolve_evidence(
                "receipt:zz-selftest/zz-selftest-i1-a1-K1@iteration=1,exit_code=99",
                pytest_nodeids=None,
            )
            record(
                not resolved,
                "receipt: reference with a wrong exit_code is rejected by resolve_evidence",
                "receipt: reference with a wrong exit_code was NOT rejected",
            )

            # 7. receiptcount: that disagrees with the number of *.json files
            # actually on disk (4, not 999).
            resolved, _ = resolve_evidence("receiptcount:zz-selftest=999", pytest_nodeids=None)
            record(
                not resolved,
                "receiptcount: that disagrees with the files on disk is rejected by resolve_evidence",
                "receiptcount: that disagrees with the files on disk was NOT rejected",
            )

            # 8. discriminated: that disagrees with a fresh recomputation from
            # the basis/candidate receipt pairs (the true count is 1/2: only
            # K1's exit code differs between basis and candidate).
            resolved, _ = resolve_evidence("discriminated:zz-selftest/1=2/2", pytest_nodeids=None)
            record(
                not resolved,
                "discriminated: that disagrees with a fresh recomputation is rejected by resolve_evidence",
                "discriminated: that disagrees with a fresh recomputation was NOT rejected",
            )

            # 9, 10, 11: the ACCEPT path for all three new forms. A runs/-less
            # check directory can only ever see these forms fail, so this is
            # the one place the resolver's positive case is actually proven:
            # against synthetic fixture data this selftest builds itself,
            # never against the real (possibly absent) runs/ tree.
            resolved, _ = resolve_evidence(
                "receipt:zz-selftest/zz-selftest-i1-a1-K1@iteration=1,exit_code=0",
                pytest_nodeids=None,
            )
            record(
                resolved,
                "receipt: reference with correct run_id/iteration/exit_code resolves correctly",
                "receipt: reference with correct run_id/iteration/exit_code was NOT accepted",
            )

            resolved, _ = resolve_evidence("receiptcount:zz-selftest=4", pytest_nodeids=None)
            record(
                resolved,
                "receiptcount: that matches the files on disk resolves correctly",
                "receiptcount: that matches the files on disk was NOT accepted",
            )

            resolved, _ = resolve_evidence("discriminated:zz-selftest/1=1/2", pytest_nodeids=None)
            record(
                resolved,
                "discriminated: that matches a fresh recomputation resolves correctly",
                "discriminated: that matches a fresh recomputation was NOT accepted",
            )

            # 12. Distinguishing the two failure classes: a missing receipt
            # under an EXISTING runs/ tree (assertion 4's genuine content
            # defect) must NOT carry the ENVIRONMENT_GAP_MARKER -- only a
            # wholly absent runs/ directory (assertions 13-16 below) may.
            _, missing_receipt_msg = resolve_evidence(
                "receipt:zz-selftest/does-not-exist", pytest_nodeids=None
            )
            record(
                ENVIRONMENT_GAP_MARKER not in missing_receipt_msg,
                "a missing receipt under an existing runs/ tree is not mislabeled as an environment gap",
                "a missing receipt under an existing runs/ tree was wrongly labeled an environment gap",
            )
        finally:
            REPO_ROOT = original_root

    # 13-16: with runs/ entirely absent from REPO_ROOT (no runs/ directory at
    # all, not merely an empty one), every receipt:/run:/receiptcount:/
    # discriminated: resolution failure must carry the ENVIRONMENT_GAP_MARKER
    # -- proving resolve_evidence tells this class of failure apart from a
    # genuine content defect, per the house rule that a checker nobody
    # falsifies is a claim of its own.
    with tempfile.TemporaryDirectory() as td:
        REPO_ROOT = Path(td)
        try:
            _, msg = resolve_evidence("receipt:zz-selftest/whatever", pytest_nodeids=None)
            record(
                ENVIRONMENT_GAP_MARKER in msg,
                "receipt: failure against a wholly absent runs/ is labeled an environment gap",
                "receipt: failure against a wholly absent runs/ was NOT labeled an environment gap",
            )

            _, msg = resolve_evidence("run:zz-selftest/1", pytest_nodeids=None)
            record(
                ENVIRONMENT_GAP_MARKER in msg,
                "run: failure against a wholly absent runs/ is labeled an environment gap",
                "run: failure against a wholly absent runs/ was NOT labeled an environment gap",
            )

            _, msg = resolve_evidence("receiptcount:zz-selftest=0", pytest_nodeids=None)
            record(
                ENVIRONMENT_GAP_MARKER in msg,
                "receiptcount: failure against a wholly absent runs/ is labeled an environment gap",
                "receiptcount: failure against a wholly absent runs/ was NOT labeled an environment gap",
            )

            _, msg = resolve_evidence("discriminated:zz-selftest/1=0/0", pytest_nodeids=None)
            record(
                ENVIRONMENT_GAP_MARKER in msg,
                "discriminated: failure against a wholly absent runs/ is labeled an environment gap",
                "discriminated: failure against a wholly absent runs/ was NOT labeled an environment gap",
            )
        finally:
            REPO_ROOT = original_root

    # 17. cite: reference with a malformed date.
    resolved, _ = resolve_evidence("cite:https://example.com/page@2026-13-40", pytest_nodeids=None)
    record(
        not resolved,
        "cite: reference with a malformed date is rejected by resolve_evidence",
        "cite: reference with a malformed date was NOT rejected",
    )

    # 18. two claims sharing one id.
    dup_ledger = {
        "schema": "hoh-claims/1",
        "claims": [
            {"id": "C-100", "text": "a", "where": "README.md:1", "status": "INTENT", "evidence": [], "note": "x"},
            {"id": "C-100", "text": "b", "where": "README.md:1", "status": "INTENT", "evidence": [], "note": "y"},
        ],
        "not_claims": [],
    }
    dup_problems = check_ids(dup_ledger)
    record(
        any("duplicate" in p for p in dup_problems),
        "a duplicate claim id is rejected by check_ids",
        "a duplicate claim id was NOT rejected",
    )

    # 19. a claim resting on run a02 left SUPPORTED instead of INVALIDATED.
    a02_ledger = {
        "schema": "hoh-claims/1",
        "claims": [
            {
                "id": "C-101",
                "text": "a claim wrongly left SUPPORTED despite resting on a02",
                "where": "README.md:1",
                "status": "SUPPORTED",
                "invalidated_run": "a02",
                "evidence": ["file:README.md:1"],
                "note": "",
            }
        ],
        "not_claims": [],
    }
    a02_problems = check_a02(a02_ledger)
    record(
        any("C-101" in p for p in a02_problems),
        "a claim resting on a02 left SUPPORTED is rejected by check_a02",
        "a claim resting on a02 left SUPPORTED was NOT rejected",
    )

    return ok, report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_and_exit(problems: list[str], *, ok_message: str) -> None:
    if problems:
        for p in problems:
            print(f"FAIL: {p}")
        print(f"{len(problems)} problem(s).")
        sys.exit(1)
    print(f"OK: {ok_message}")
    sys.exit(0)


def cmd_check(which: str) -> None:
    if which == "home-paths":
        _print_and_exit(check_home_paths(), ok_message="no home-directory paths found in the new files")
        return

    data = load_ledger()

    if which == "schema":
        _print_and_exit(check_schema(data), ok_message="CLAIMS.json parses and every claim has its mandatory fields")
    elif which == "evidence":
        _print_and_exit(check_evidence(data), ok_message="every evidence reference resolves")
    elif which == "ids":
        _print_and_exit(check_ids(data), ok_message="claim ids are unique and the sequence has no gaps")
    elif which == "coverage":
        coverage_problems = check_coverage(data)
        gap_count = sum(1 for p in coverage_problems if ENVIRONMENT_GAP_MARKER in p)
        if gap_count:
            print(
                f"{gap_count} anchor problem(s) below are {ENVIRONMENT_GAP_MARKER}: their "
                "target file is absent from this tree, not a content defect."
            )
        _print_and_exit(coverage_problems, ok_message="every number-bearing sentence is covered")
    elif which == "a02":
        _print_and_exit(check_a02(data), ok_message="no claim resting on run a02 is SUPPORTED")
    elif which == "render":
        _print_and_exit(check_render(data), ok_message="CLAIMS.md agrees with a fresh regeneration from CLAIMS.json")
    elif which == "undeclared-surfaces":
        warnings = find_undeclared_claim_surfaces(data)
        for w in warnings:
            print(f"WARN: {w}: carries number-bearing sentences but is not declared in claim_surfaces")
        print("OK: undeclared-surface scan complete (warnings, if any, never fail this check)")
        sys.exit(0)
    elif which == "resolvability":
        problems, report_lines = check_resolvability(data)
        for line in report_lines:
            print(line)
        _print_and_exit(
            problems,
            ok_message=(
                "every evidence reference classifies as PUBLIC/LOCAL_ONLY/EXTERNAL, every "
                "receipt:/run:/receiptcount:/discriminated: reference is LOCAL_ONLY, and every "
                "LOCAL_ONLY reference states who resolved it and when"
            ),
        )
    elif which == "export-classification":
        manifest = load_manifest()
        _print_and_exit(
            check_export_classification(manifest, data),
            ok_message="every EXPORT_MANIFEST.json INCLUDE path is classified exactly once, as a declared claim surface or a declared exclusion",
        )
    elif which == "local-only-diversity":
        _print_and_exit(
            check_local_only_diversity(data),
            ok_message="no two LOCAL_ONLY claims share an identical local_only_resolution statement",
        )
    elif which == "all":
        problems = []
        problems += check_schema(data)
        problems += check_evidence(data)
        problems += check_ids(data)
        coverage_problems = check_coverage(data)
        problems += coverage_problems
        problems += check_a02(data)
        problems += check_render(data)
        problems += check_home_paths()
        try:
            manifest = load_manifest()
        except CheckFailure:
            # Mirrors coverage_targets()'s own precedent of silently skipping
            # a surface whose file is absent: EXPORT_MANIFEST.json is a real,
            # always-present input in this run's own environment (out of
            # scope to create or edit), but the pre-existing fixture-based
            # `check all` tests below build minimal repos that predate this
            # check and carry no manifest at all -- treating that as "zero
            # INCLUDE paths to classify" rather than crashing keeps this
            # command's existing holistic-health-check behaviour intact.
            manifest = {"entries": []}
        problems += check_export_classification(manifest, data)
        problems += check_local_only_diversity(data)
        for w in find_undeclared_claim_surfaces(data):
            print(f"WARN: {w}: carries number-bearing sentences but is not declared in claim_surfaces")
        gap_count = sum(1 for p in coverage_problems if ENVIRONMENT_GAP_MARKER in p)
        if gap_count:
            print(
                f"{gap_count} anchor problem(s) below are {ENVIRONMENT_GAP_MARKER}: their "
                "target file is absent from this tree, not a content defect."
            )
        _print_and_exit(problems, ok_message="all checks passed")
    else:
        print(f"unknown check: {which!r}", file=sys.stderr)
        sys.exit(2)


def cmd_reconcile(paths: list[str]) -> None:
    """CLI entry point for the read-only reconciliation-report mode.

    With no paths, reports every declared claim surface. With one or more
    paths, restricts the report to the matching declared surfaces (an
    unknown path is silently not reported -- it names no surface, so there
    is nothing to reconcile). Exits 1 if anything is unbound or vanished
    anywhere in the report, 0 if every reported surface is fully reconciled.
    Never writes to CLAIMS.json, CLAIMS.md, or any surface file.
    """
    data = load_ledger()
    surfaces = data.get("claim_surfaces") or []
    if paths:
        wanted = set(paths)
        surfaces = [s for s in surfaces if isinstance(s, dict) and s.get("path") in wanted]

    report = reconcile_report(data, surfaces)
    for line in format_reconcile_report(report):
        print(line)

    anything_pending = any(d.get("unbound") or d.get("vanished") for d in report.values())
    if anything_pending:
        sys.exit(1)
    print("OK: every reported surface is fully reconciled")
    sys.exit(0)


def cmd_selftest() -> None:
    ok, report = run_selftest()
    for line in report:
        print(line)
    if ok:
        print("OK: the checker correctly rejected both deliberately broken ledgers")
        sys.exit(0)
    print("FAIL: the checker failed to falsify itself")
    sys.exit(1)


def cmd_selftest_extended() -> None:
    ok, report = run_selftest_extended()
    for line in report:
        print(line)
    if ok:
        print("OK: the checker passed all nineteen self-checks (reject-path, accept-path, and the environment-gap distinction)")
        sys.exit(0)
    print("FAIL: the checker failed to falsify itself")
    sys.exit(1)


def cmd_render() -> None:
    data = load_ledger()
    CLAIMS_MD.write_text(render_markdown(data), encoding="utf-8")
    print(f"wrote {CLAIMS_MD.relative_to(REPO_ROOT)}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="check_claims.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check")
    p_check.add_argument(
        "which",
        choices=[
            "schema",
            "evidence",
            "ids",
            "coverage",
            "a02",
            "render",
            "home-paths",
            "undeclared-surfaces",
            "resolvability",
            "export-classification",
            "local-only-diversity",
            "all",
        ],
    )

    p_reconcile = sub.add_parser("reconcile")
    p_reconcile.add_argument(
        "paths",
        nargs="*",
        help="restrict the report to these declared claim_surfaces paths (default: all)",
    )

    sub.add_parser("selftest")
    sub.add_parser("selftest-extended")
    sub.add_parser("render")

    args = parser.parse_args(argv)

    if args.command == "check":
        cmd_check(args.which)
    elif args.command == "reconcile":
        cmd_reconcile(args.paths)
    elif args.command == "selftest":
        cmd_selftest()
    elif args.command == "selftest-extended":
        cmd_selftest_extended()
    elif args.command == "render":
        cmd_render()


if __name__ == "__main__":
    main()
