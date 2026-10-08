#!/usr/bin/env python3
"""This programme's own supervisor review, bound to the product's supervisor.

V3.3 P2-27's binding, and the analogue of `tools/programme_checklist.py` one
level up. The P2 gate closes on three things:

    all phase-blocking checklist items VERIFIED or validly waived
    current independent Checklist Supervisor review complete
    ChecklistFsck PASS

`programme_checklist.py` answers the first and the third. This answers the
second: it assembles the bounded `ReviewCapsule` a supervisor is given from this
programme's actual register and measured matrix, validates the findings that
come back through `hoh.supervisor`, and writes a review record the gate can
read -- including whether that review is still *current*.

The rule it must not violate:

    Supervisor can FIND.  Reconciler can VERIFY.

So the division of labour is deliberate:

* **Nothing here dispatches a supervisor.** The caller hands the capsule to an
  independent supervisor and hands the findings back in. A tool that both asks
  the question and supplies the answer proves nothing.
* **Nothing here decides a status.** An "apply" decision goes through
  `apply_finding`, whose only possible effect is to reopen; its unsealed result
  is not kept anywhere. Nothing waives an item or accepts a candidate.
* **The capsule selects, and says so.** A phase larger than the budget is cut
  to its most informative items, and what was left out -- how many, which, by
  what rule -- travels with the capsule. A supervisor shown two thirds of a
  phase without knowing it would report the two thirds as the whole.
* **`complete` is computed, never handed in.** An undecided finding keeps the
  review open, and so does a finding the product refused: a gate that closed
  over either would be closing over what the supervisor found.

Usage:
    python3 tools/programme_supervisor.py --phase P2              # capsule JSON on stdout
    python3 tools/programme_supervisor.py --phase P2 \\
        --findings F.json --decisions D.json --out RECORD.json    # record the review
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import os
import posixpath
import re
import struct
import sys
from collections.abc import Iterable, Mapping
from typing import ClassVar, Literal
from datetime import UTC, datetime
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOH / "src"))

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)

from hoh import supervisor
from hoh.checklist import (
    BLOCKING,
    ChecklistItem,
    ItemEvidence,
    Snapshot,
    fsck,
    reconcile,
)
from hoh.contracts import Strict
from hoh.supervisor import (
    FindingKind,
    FindingStatus,
    ReviewCapsule,
    SupervisorFinding,
)


def _load_programme_checklist():
    """`tools/programme_checklist.py` is how this programme's checklist is
    derived; it is reused, not re-derived differently here."""
    name = "programme_checklist"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parent / "programme_checklist.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


programme_checklist = _load_programme_checklist()
build = programme_checklist.build
# O220/F9: the same external sets the checklist binding feeds `fsck`, reused rather
# than re-derived, so the two callers cannot drift apart again.
declared_requirement_refs = programme_checklist.declared_requirement_refs
plan_revision = programme_checklist.plan_revision
criticality_of = programme_checklist.criticality_of

REGISTER = HOH / "program/v3_3/REQUIREMENTS.json"
MATRIX = HOH / "program/v3_3/VERIHARNESS_CAPABILITY_MATRIX.json"

#: The decision value that routes a finding through `apply_finding`. Any other
#: (non-blank) string is a rejection reason.
APPLY = "apply"

#: Who a rejection is recorded against when the caller names nobody.
DEFAULT_DECIDER = "programme-orchestrator"

#: Statuses at which an item is settled: a supervisor learns least from them,
#: so they give way first when the budget binds. They are exactly the statuses
#: a supervisor may never set, and are taken from there rather than spelled out.
_SETTLED = frozenset(supervisor.FORBIDDEN_TO_SUPERVISOR)

SELECTION_RULE = (
    "items of the phase under review only; phase-blocking (P0/P1) before "
    "non-blocking (P2_NONBLOCKING); within each, open items (e.g. "
    "EVIDENCE_PENDING, REOPENED) before settled ones (the statuses in "
    "hoh.supervisor.FORBIDDEN_TO_SUPERVISOR); "
    "then by item_id; cut at max_items")


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


#: Keys that say *when* the matrix was measured rather than *what* it measured.
#: `capability_matrix.measure()` stamps the wall clock into every measurement, so a
#: digest over the whole mapping changes on each run of an unchanged tree. A reviewer
#: reproduced it: two measurements taken seconds apart, identical in every capability,
#: digested differently, which made "this review is current" unreachable for any
#: review ever taken -- the same defect as the file-bytes comparison this function
#: replaced, one layer in.
MEASUREMENT_TIME_KEYS: frozenset[str] = frozenset({"measured_at_utc"})


#: The shapes a repair-evidence reference may take. O226/E4: the field accepted any
#: non-blank string, so `{"disposition": "apply", "evidence": "."}` on each of the
#: thirteen non-phase findings closed all thirteen and the board printed
#: `PASS | 14 finding(s), 0 undecided, complete`. A single dot is not a reference to
#: anything. This layer still cannot look at a tree -- it does not check that the
#: target exists, and says so -- but it can insist the string is the *kind of thing*
#: that could be looked up, which a dot is not.
EVIDENCE_SHAPES: tuple[tuple[str, str], ...] = (
    (r"^[\w][\w./-]*\.[A-Za-z0-9]+(?::\d+)?(?:#\S+)?$",
     "a repo-relative path, optionally with :line or #anchor"),
    (r"^[\w][\w./-]*\.py::[\w:\[\]. -]+$", "a pytest node id"),
    (r"^O\d{1,4}(?:/[A-Za-z0-9-]+)?$", "a ledger entry id such as O225 or O225/C3"),
    # Eight characters and at least one digit, or English prose supplies the match:
    # "defaced" is seven hex letters and would otherwise have counted as a commit.
    (r"^(?=[0-9a-f]*\d)[0-9a-f]{8,40}$", "a commit sha (8+ chars, not a word)"),
)

#: Trailing punctuation to ignore when looking for a reference inside a sentence.
_EVIDENCE_TRIM = ".,;:)(][\"'`"


def evidence_reference_problems(reference) -> list[str]:
    """Why this string carries no usable repair-evidence reference, or `[]`.

    The field is allowed to be a sentence -- an evidence note that says *what* the
    reference shows is more use to a reader than a bare path -- so this asks whether
    the text **contains** at least one token of a documented shape, not whether the
    whole string is one. The first version required a full match and duly refused all
    five of this programme's own honest dispositions, which is the wrong failure: a
    check that rejects the real thing teaches people to route around it.

    Shape only, deliberately: whether the reference *resolves* is a question about a
    tree, and the layer that asks it is the gate (`readiness.py`), which has one.
    O226/E4 is the reason the check exists at all -- `{"evidence": "."}` on thirteen
    findings closed all thirteen and the board printed `complete`.
    """
    text = " ".join(str(reference or "").split())
    if not text:
        return ["no evidence reference: an apply records an intention, not a repair"]
    for token in text.split():
        bare = token.strip(_EVIDENCE_TRIM)
        if bare and any(re.match(pattern, bare) for pattern, _ in EVIDENCE_SHAPES):
            return []
    shapes = "; ".join(what for _, what in EVIDENCE_SHAPES)
    return [f"{text[:80]!r} names no reference anything could look up -- "
            f"expected the text to contain one of: {shapes}"]


def findings_digest(findings: Iterable) -> str:
    # O227/G1: every entry must be a mapping. `dict(f)` also accepts a list of
    # key/value pairs, so the same fourteen findings re-serialised as pair lists
    # digested identically while the gate's accounting -- which counted only `dict`
    # entries -- saw none of them. A record claiming zero findings then passed over a
    # preserved file holding all fourteen. Two readers of one file must not disagree
    # about what is in it.
    """sha256 over the finding set as the supervisor returned it -- the review's
    *output*, which nothing used to bind (O226/E2).

    Order-independent, because the order a supervisor happens to return findings in
    is not part of what it found: the digest is over the sorted canonical form. A
    finding removed, added or altered changes it; the same findings listed differently
    do not.
    """
    entries = list(findings)
    for index, f in enumerate(entries):
        if not (hasattr(f, "model_dump") or isinstance(f, Mapping)):
            raise TypeError(
                f"finding #{index} is a {type(f).__name__}, not a mapping: a findings "
                "file whose entries are not objects cannot be digested, because the "
                "gate that reads it counts objects (O227/G1)")
    canonical = sorted(
        _canonical(f.model_dump(mode="json") if hasattr(f, "model_dump") else dict(f))
        for f in entries)
    return _sha256(canonical)


#: The one brief this programme puts to a supervisor for a phase, and the only
#: derivation of it. O229/L7 and M2: the gate had a *private* `_canonical_brief` helper
#: while the CLI's `main()` built a different capsule, so the artefact the gate demanded
#: could not be produced by any command in the tree -- the fourth "gate that can never
#: close" in this sequence, and the one that made an honest review unpreservable. A
#: reviewer proved it by performing the review and being unable to bind it.
#:
#: So there is one function, it lives with the tool rather than inside the gate, and
#: `--canonical-brief` prints it. The gate and the operator cannot disagree about what
#: the brief is, because they call the same code.
def canonical_brief(*, register: Mapping, matrix: Mapping,
                    phase: str = "P2",
                    review_root: Path | None = None,
                    before: str | None = None,
                    repo: Path | None = None) -> ProgrammeCapsule:
    """The brief for `phase` on this board: every item, evidence for every item.

    Sized to the phase rather than to a flag (O228/S1), and by the *same* field
    `capsule_for` selects on. O229/L5: the gate counted items with
    `item_id.startswith("P2")` while `capsule_for` selects on `item.phase`, and those
    are two independent register fields -- so renaming two requirement ids made the
    derivation cover 30 of 32 items and the three-way digest identity then made the
    under-coverage *canonical*, with nothing left to compare it against.

    It carries **the accumulated open findings**, and getting that right took two wrong
    designs. The first put the newest earlier review's findings in the brief, which made
    the history one review deep: any review that honestly returned `[]` erased every
    earlier finding from every later brief (O229/L2). Removing them entirely fixed that
    and created O231/N2 -- the supervisor could no longer see what was open, so the
    twenty-five open ids reached it only as **prose in a dispatch written by the party
    under review**, which is the one input deciding this gate's outcome sitting outside
    both the record and the derivation.

    Accumulation is what makes carrying them safe: `resolution_state` is order-independent
    and cannot be shortened by adding a directory, so the set comes from every preserved
    review rather than from one. And because the set is in the brief, it is inside the
    digest the gate compares -- a brief showing a supervisor fewer open findings than the
    tree holds is not the canonical brief.

    Source drift is measured against the most recent preserved review of the phase
    **strictly before** `before` (`drift_baseline_for`); `None` means before now. The gate
    passes the stamp of the review it is checking, because it rebuilds the brief after
    that review was preserved, and a review must never be its own baseline (Revision 3).
    Without a usable baseline drift is NOT_RUN, and the reason travels in the brief.

    Files are digested in `repo`, which is never guessed (Revision 5 rule 5): an explicit
    `repo` wins; otherwise a `review_root` shaped `<dir>/dogfood/p2-gate` belongs to
    `<dir>`; any other `review_root` without `repo` is refused; with neither, `HOH`. The
    gate digests the tree it inspects, and a review root somewhere else does not quietly
    pair this checkout's files with another tree's reviews.
    """
    repo = _repo_for(review_root, repo)
    snapshot, _ev, _it = reconciled(register, matrix)
    of_phase = [i for i in snapshot.items if i.phase == phase]
    if not of_phase:
        raise ValueError(f"the snapshot holds no items of phase {phase}")
    # `review_root` is a parameter rather than a constant because the gate inspects a tree
    # it is *told* about: `readiness.HOH` is patched in tests and could point anywhere, and a
    # default that always read this checkout would make the brief describe one tree while
    # the gate checked another -- a mismatch of exactly the kind this function exists to
    # remove.
    carried = list(open_findings_for(phase, snapshot=snapshot,
                                     root=review_root, repo=repo).values())
    # `ReviewCapsule` bounds `open_findings` by `max_items`, so the size covers both --
    # O229/L6 was a brief that became underivable because an earlier review returned more
    # findings than the phase has items, with no escape that did not erase the history.
    size = max(len(of_phase), len(carried))
    # Evidence for every item, not for the first ten: O229/L11 -- the previous derivation
    # left `max_evidence` at the CLI default, so the canonical brief showed a supervisor
    # evidence for 9 of 32 items while arguing that a partial brief cannot answer for the
    # phase.
    baseline, baseline_id, reason = drift_baseline_for(phase, before=before,
                                                       root=review_root, repo=repo)
    return capsule_for(phase, register=register, matrix=matrix,
                       max_items=size, max_evidence=len(of_phase) + 1,
                       open_findings=carried,
                       previous_source_digests=baseline,
                       previous_review_id=baseline_id,
                       drift_reason=reason, repo=repo)


#: The last two parts of a review root whose tree can be derived from it.
_REVIEW_ROOT_PARTS = ("dogfood", "p2-gate")


def _repo_for(review_root: Path | None, repo: Path | None) -> Path:
    """The tree `canonical_brief` digests: explicit, derived exactly, or refused."""
    if repo is not None:
        return Path(repo)
    if review_root is None:
        return HOH
    root = Path(review_root)
    if root.parts[-2:] == _REVIEW_ROOT_PARTS:
        return root.parent.parent
    raise ValueError(
        f"review root {root} is not shaped <dir>/{'/'.join(_REVIEW_ROOT_PARTS)}, so the "
        "tree its files belong to cannot be derived: pass repo")


#: A phase name as it may appear in a file name: ASCII letters, digits, `_` and `-`,
#: starting with a letter or digit. No separator, no dot, nothing a path join could
#: turn into another directory (supdrift2-a M1).
_PHASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _phase_name(phase) -> str:
    """`phase` itself when it can name a file, else ValueError. Asked before any path
    is built from it, so a crafted `--phase` (`x/../../../outside/y`) never reaches the
    file system (supdrift2-a M1)."""
    if not isinstance(phase, str) or not _PHASE_NAME.fullmatch(phase):
        raise ValueError(f"phase {phase!r} is not a phase name (ASCII letters, digits, "
                         "'_' and '-', starting with a letter or digit)")
    return phase


#: Where preserved reviews live. The tool reads them so the brief and the gate agree about
#: what is open **by construction** rather than by agreement (O231/N2).
REVIEW_ROOT = Path(__file__).resolve().parent.parent / "dogfood/p2-gate"
_STAMP = re.compile(r"^\d{8}T\d{4}(?:\d{2})?Z$")


def _root_inside(root: Path, repo: Path | None) -> bool:
    """Does the review root resolve inside the repository? `repo` when given, else the
    tree a `<repo>/dogfood/p2-gate` root belongs to (Revision 6 item 1, 6a item 3).
    Unresolvable is outside. One check for every reader of a review root (T-PSD4)."""
    try:
        tree = Path(repo) if repo is not None else root.parent.parent
        return root.resolve().is_relative_to(tree.resolve())
    except (OSError, ValueError, RuntimeError):
        return False


def preserved_reviews(root: Path | None = None, *,
                      repo: Path | None = None) -> list[tuple[str, list]]:
    """Every preserved review under `root`, as `(stamp, findings)`.

    Stamped, in-tree and not a symlink: O229/L1 showed `Path.is_dir()` follows a symlink
    out of the repository, and O229/L3 that a rule with no stamp shape lets any lowercase
    name outrank every timestamp.

    T-PSD4: the root itself must lie inside the repository (`_root_inside`), as for
    `drift_baseline_for`. Checking each directory only against `root.resolve()` let a
    symlinked `dogfood/p2-gate` carry a foreign review's findings into the brief. An
    outside root holds no review of this repository: the answer is `[]`, and nothing
    under it is listed or opened.
    """
    root = root or REVIEW_ROOT
    if not _root_inside(root, repo):
        return []
    if not root.is_dir():
        return []
    out: list[tuple[str, list]] = []
    for d in sorted(root.iterdir(), key=lambda x: _stamp_key(x.name)):
        if d.is_symlink() or not d.is_dir() or not _STAMP.match(d.name):
            continue
        try:
            if not d.resolve().is_relative_to(root.resolve()):
                continue
            raw = json.loads((d / "findings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        returned = raw.get("findings", []) if isinstance(raw, dict) else raw
        out.append((d.name, [f for f in returned if isinstance(f, Mapping)]))
    return out


def drift_baseline_for(phase: str, *, before: str | None = None, root: Path | None = None,
                       repo: Path | None = None
                       ) -> tuple[dict[str, str] | None, str | None, str | None]:
    """The drift baseline for a brief of `phase`: `(digests, review_id, None)`, or
    `(None, None, reason)` when drift cannot be measured. Never an invented baseline.

    Candidates are the reviewed briefs strictly before `before` (`None`: before now):
    stamped, in-tree, non-symlink directories under `root` holding **both**
    `findings.json` and `capsule-<phase>.json` -- the predicate the gate's
    `_canonical_review` applies, so a directory never reviewed, or holding something that
    merely parses, is not one (Revision 4 item 2). They are ordered by parsed time, and
    **only the latest decides** (Revision 5 rule 3):

    * its capsule does not validate as a `ProgrammeCapsule` -> `baseline <id> is not a
      readable capsule`;
    * its `selection.source_digests` is empty -> `baseline <id> carries no source
      digests`;
    * another candidate has the same parsed time -> `ambiguous baseline: <id> and <id>`;
    * no candidate at all -> `no preserved baseline`.

    An older review is never consulted instead: the next brief would then be measured
    against something other than the review before it, and say MEASURED about it.

    The root itself must lie inside the repository (Revision 6 item 1, 6a item 3):
    `repo` when given, else the tree a `<repo>/dogfood/p2-gate` root belongs to. A
    symlinked `dogfood/p2-gate` (or `dogfood`) leading elsewhere gives `review root
    outside the repository`, and nothing under it is opened.

    `phase` is checked first (`_phase_name`), before the root is looked at: it is
    joined into a file name, and a crafted one is refused with ValueError.
    """
    _phase_name(phase)
    root = root or REVIEW_ROOT
    outside = (None, None, "review root outside the repository")
    if not _root_inside(root, repo):
        return outside
    findings, name = "findings.json", f"capsule-{phase.lower()}.json"
    limit = _stamp_key(before) if before is not None else None
    candidates: list[Path] = []
    try:
        entries = list(root.iterdir()) if root.is_dir() else []
    except OSError:
        entries = []
    for d in entries:
        try:
            if d.is_symlink() or not d.is_dir() or not _STAMP.match(d.name):
                continue
            if limit is not None and _stamp_key(d.name) >= limit:
                continue
            if not d.resolve().is_relative_to(root.resolve()):
                continue
            if not ((d / findings).is_file() and (d / name).is_file()):
                continue
        except (OSError, ValueError):
            continue
        candidates.append(d)
    if not candidates:
        return None, None, "no preserved baseline"
    newest = max(_stamp_key(d.name) for d in candidates)
    latest = sorted(d.name for d in candidates if _stamp_key(d.name) == newest)
    if len(latest) > 1:
        return None, None, f"ambiguous baseline: {' and '.join(latest)}"
    review_id = latest[0]
    capsule_file = root / review_id / name
    try:
        # A capsule file that is itself a symlink is a candidate (the gate counts it) but
        # is not read: it may point anywhere.
        if capsule_file.is_symlink():
            raise ValueError("a symlink is not read")
        capsule = ProgrammeCapsule.model_validate(
            json.loads(capsule_file.read_text(encoding="utf-8")))
    except Exception:                      # noqa: BLE001 - unreadable is the answer
        return None, None, f"baseline {review_id} is not a readable capsule"
    digests = dict(capsule.selection.source_digests)
    if not digests:
        return None, None, f"baseline {review_id} carries no source digests"
    return digests, review_id, None


def open_findings_for(phase: str, *, snapshot=None, root: Path | None = None,
                      repo: Path | None = None) -> dict:
    """The findings still open, keyed by id, fit to be carried in a brief.

    Only findings the product still accepts against `snapshot` are carried, when one is
    given: `ReviewCapsule` refuses a capsule holding a finding naming an item the snapshot
    does not have, and a brief that cannot be built is a gate that cannot close -- which is
    the shape this family of defects keeps taking (D1, E10, the seal check, O229/L6, M2).
    """
    still_open, _resolved = resolution_state(preserved_reviews(root, repo=repo))
    fit: dict = {}
    for fid, body in still_open.items():
        try:
            finding = SupervisorFinding.model_validate(dict(body))
        except Exception:                      # noqa: BLE001 - unusable here, not fatal
            continue
        if snapshot is not None and supervisor.validate(finding, snapshot=snapshot):
            continue
        fit[fid] = finding
    return fit


def resolution_state(reviews: Iterable[tuple[str, Iterable]]) -> tuple[dict, list[str]]:
    """What the preserved reviews, taken together, leave open.

    `reviews` is `(name, findings)` in any order; the name is the stamped directory, and
    stamp order is the only ordering that matters. Returns the findings still open, by
    id, and the ids a later review resolved.

    The rule, and it is the doctrine of O229/G made durable:

    * a review **raises** every finding it returns that is not a finding of nothing;
    * a later review **resolves** a raised id by returning a finding of nothing that
      names it -- a supervisor looking at the thing again and saying there is nothing
      there;
    * everything raised and not later resolved is open, and holds the phase.

    This accumulates rather than chains, which is the repair for O229/L2. The previous
    design asked only the *newest* earlier review what was open, so one honest empty
    review erased the history permanently, and a directory holding nothing but an empty
    `findings.json` was invisible as a review and decisive as history. Under
    accumulation an added directory can only ever raise or resolve; it cannot forget.
    """
    raised: dict[str, dict] = {}
    resolved: dict[str, str] = {}
    for name, findings in sorted(reviews, key=lambda pair: _stamp_key(pair[0])):
        for raw in findings:
            if not isinstance(raw, Mapping):
                continue
            fid = str(raw.get("finding_id") or "").strip()
            if not fid:
                continue
            quiet = (str(raw.get("kind") or "") == FindingKind.NO_ACTION.value
                     and str(raw.get("severity") or "").strip().casefold()
                     in ReviewRecord.NO_ACTION_SEVERITIES)
            if quiet:
                if fid in raised:
                    resolved[fid] = name
                continue
            if fid in resolved:          # raised again after a resolution: open again
                del resolved[fid]
            raised.setdefault(fid, dict(raw))
    still_open = {fid: body for fid, body in raised.items() if fid not in resolved}
    return still_open, sorted(resolved)


def _stamp_key(name: str) -> tuple:
    """Order a stamped directory name by time, not by ASCII. (O229/L8)

    `STAMP` accepts four or six digits after the `T`, and `'Z'` sorts after any digit,
    so `20260925T1015Z` compared greater than `20260925T101500Z` -- a four-digit
    directory permanently outranked a genuine review preserved seconds later, and both
    were valid stamps so the shape check did not notice.
    """
    head, _, tail = str(name).partition("T")
    digits = "".join(c for c in tail if c.isdigit())
    return (head, (digits + "00")[:6])


def matrix_digest(matrix: Mapping) -> str:
    """The digest a review is bound to: sha256 over the matrix's canonical JSON,
    excluding the keys that record when the measurement was taken.

    Currency must mean "the thing measured has not changed", not "the clock has not
    moved". A digest over the timestamp answers the second question and reports the
    first, so a review of an untouched matrix went stale by existing.
    """
    return _sha256({k: v for k, v in matrix.items()
                    if k not in MEASUREMENT_TIME_KEYS})


# --------------------------------------------------------------------------- #
# source drift: what the files an item's evidence names hash to now
# --------------------------------------------------------------------------- #


#: What a file the evidence names contributes when it cannot be read: absent, not a
#: regular file, unreadable, or outside the repository (never read at all).
MISSING_FILE = "<missing>"

#: What a symlink contributes, followed by its link text (`os.readlink`): a link is
#: never followed, and retargeting it is drift (Revision 5 rule 2, Revision 5a item 2).
SYMLINK = "<symlink>"

_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")

#: The evidence shapes that name files, each matched at the start of the string, as
#: the capability matrix writes them. A file these name is digested whether or not it
#: exists -- a file that vanished is drift, and says `<missing>`.
_EVIDENCE_FILE_SHAPES: tuple[re.Pattern, ...] = (
    re.compile(r"^(?P<paths>[^\s:]+\.[A-Za-z0-9]+(?::\d+(?:-\d+)?)?)$"),  # path:line
    re.compile(r"^test (?P<paths>[^\s:]+)(?::|\s|$)"),                   # test <path>: ...
    re.compile(r"^record (?P<paths>[^\s:]+)(?::|\s|$)"),                 # record <path> ...
    re.compile(r"^defined in [^:]*: (?P<paths>[^\s:]+(?::\d+)?)(?:\s|$)"),
    re.compile(r"^named in an import statement by (?P<paths>[^(]+?)\s*(?:\(|$)"),
)

#: Any other token that may be a repository-relative path. Revision 4 item 1: the first
#: version read only the shapes above, so the paths inside `STALE (code changed since:
#: ...)` and in prose (`... names src/hoh/x.py in an import statement`) were never
#: digested, and the channel read `MEASURED: 0 changed` while `src/hoh/runner.py` had
#: changed. Such a token counts only when the file exists under the repository -- a
#: guess that hits nothing digests nothing. The `:<line>` / `:<a>-<b>` suffix and list
#: commas fall outside the character class by construction.
_PATH_TOKEN = re.compile(r"[\w./-]+")

#: The names `durability_e2e.stale_paths` never sees under `RELEVANT_PATHS`, because
#: `git ls-files --others --exclude-standard` drops them: exactly the `.gitignore`
#: patterns that can match there (Revision 5 rule 2, Revision 5a item 1; T-PSD3 tracks
#: that this is a copy).
IGNORED_DIRS: frozenset[str] = frozenset({
    "__pycache__", ".venv", ".pytest_cache", ".ruff_cache", ".cache", "build",
    "runs", "demo"})
IGNORED_DIR_PATTERNS: tuple[str, ...] = ("*.egg-info", "c4-*")
IGNORED_FILE_PATTERNS: tuple[str, ...] = ("*.pyc", "*.bak", ".hoh-repair-*.md")

#: The deriver whose record's own sources an item inherits (Revision 5 rule 2, D2).
DURABILITY_DERIVER = "durability_e2e.py"


def _relevant_paths() -> tuple[str, ...]:
    """`durability_e2e.RELEVANT_PATHS`, imported from the tool directory beside this
    file the way `capability_matrix.py` imports `stale_paths` -- one list, not a copy.

    The module reads `ops/hoh-continue.service` when it loads, so in a tree without it
    the import fails; that is raised as ValueError -- the item's sources cannot be
    named, and digesting it without them would be the silent under-coverage Revision 5
    removed."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        from durability_e2e import RELEVANT_PATHS  # noqa: PLC0415
    except Exception as exc:               # noqa: BLE001 - any failure means no list
        raise ValueError(
            f"durability_e2e.RELEVANT_PATHS could not be imported ({type(exc).__name__}: "
            f"{exc}), so a durability-record item's sources cannot be named") from exc
    return tuple(RELEVANT_PATHS)


def _relative(token: str) -> str | None:
    """`token` as a normalised repository-relative path, or None when it is not one
    (absolute, `..`, empty)."""
    token = token.strip().rstrip(".")
    if not token or token.startswith("/"):
        return None
    norm = posixpath.normpath(token)
    if norm in (".", "..") or norm.startswith("../"):
        return None
    return norm


def _parent_inside(link: Path, repo: Path) -> bool:
    """Does the directory holding `link` resolve inside `repo`? Asked **first**, before
    `link` itself is looked at (Revision 6 item 1): a path reached through a symlinked
    directory that leaves the repository is foreign, and neither its link text nor its
    content may reach a digest. Unresolvable is outside."""
    try:
        return link.parent.resolve().is_relative_to(repo.resolve())
    except (OSError, ValueError, RuntimeError):
        return False


def _exists_in(path: str, repo: Path) -> bool:
    """Is `path` a file -- or a symlink, which is listed rather than followed -- inside
    `repo`?"""
    target = repo / path
    if not _parent_inside(target, repo):
        return False
    try:
        if target.is_symlink():
            return True
        return target.is_file() and target.resolve().is_relative_to(repo.resolve())
    except (OSError, ValueError, RuntimeError):
        return False


def evidence_paths(evidence: Iterable, repo: Path | None = None) -> list[str]:
    """The repo-relative paths a matrix row's `evidence` strings name, `:line` stripped.

    Two sources, sorted and de-duplicated together:

    * the shapes in `_EVIDENCE_FILE_SHAPES` -- `path:line`, `test <path>: ...`,
      `record <path> ...`, `defined in ...: <path>:line`, `named in an import statement
      by <path>, ...` -- whose paths count whether or not the file exists;
    * with `repo`, every other token of every string that names a file existing under
      `repo` (Revision 4 item 1): the STALE clause, prose, extensionless names. Without
      `repo` nothing can be checked, so no such token is guessed.
    """
    out: set[str] = set()
    for raw in evidence or ():
        text = " ".join(str(raw).split())
        for shape in _EVIDENCE_FILE_SHAPES:
            match = shape.match(text)
            if not match:
                continue
            for part in match.group("paths").split(","):
                path = _LINE_SUFFIX.sub("", part.strip())
                if path:
                    out.add(path)
            break
        if repo is None:
            continue
        for token in _PATH_TOKEN.findall(text):
            path = _relative(token)
            if path is not None and path not in out and _exists_in(path, repo):
                out.add(path)
    return sorted(out)


def _ignored(name: str, *, directory: bool) -> bool:
    """Is `name` dropped by the `.gitignore` copy? As in git, the unanchored no-slash
    file patterns (`*.bak`, `*.pyc`, `.hoh-repair-*.md`) ignore a directory of that name
    too (Revision 6 item 4), so `src/hoh/old.bak/a.py` is not digested."""
    if any(fnmatch.fnmatchcase(name, p) for p in IGNORED_FILE_PATTERNS):
        return True
    return directory and (name in IGNORED_DIRS or any(
        fnmatch.fnmatchcase(name, p) for p in IGNORED_DIR_PATTERNS))


def _walk(entry: str, repo: Path) -> list[str]:
    """`entry` itself if it is a file, a symlink or absent; otherwise every file under
    it, without git, skipping the ignored names and listing -- never following -- a
    symlink. Nothing outside `repo` is entered, and an entry whose directory lies outside
    it is returned as itself, which `_file_digest` then digests as `MISSING_FILE`."""
    top = repo / entry
    if not _parent_inside(top, repo):
        return [entry]
    try:
        if top.is_symlink() or not top.is_dir():
            return [entry]
        if not top.resolve().is_relative_to(repo.resolve()):
            return [entry]
    except (OSError, ValueError, RuntimeError):
        return [entry]
    out: list[str] = []
    for current, dirnames, filenames in os.walk(top, followlinks=False):
        rel = Path(current).relative_to(repo).as_posix()
        kept = []
        for name in sorted(dirnames):
            # `current` lies inside `repo`: the top resolved inside it and no link is
            # followed, so a symlinked dirname here is an in-repo link. Asked before the
            # directory patterns (supdrift2-a M2): git sees a symlink as a file, so only
            # the file patterns can drop it -- `build`, `runs` or `__pycache__` as a link
            # is listed by `ls-files --others` and must be listed here.
            if (Path(current) / name).is_symlink():
                if not _ignored(name, directory=False):
                    out.append(f"{rel}/{name}")     # listed, not walked
                continue
            if _ignored(name, directory=True):
                continue
            kept.append(name)
        dirnames[:] = kept
        out += [f"{rel}/{name}" for name in filenames
                if not _ignored(name, directory=False)]
    return out


#: The largest index `_tracked_paths` reads; a bigger one is not read (fallback).
_INDEX_MAX_BYTES = 256 * 1024 * 1024
#: Index extensions whose presence means the entries are not the whole tracked set:
#: a split index keeps them in a shared file, a sparse index folds directories.
_INDEX_PARTIAL_EXTENSIONS = (b"link", b"sdir")


def _tracked_paths(repo: Path) -> frozenset[str] | None:
    """Every path git tracks in `repo`, read from `repo/.git/index` without starting
    git (T-PSD3), or None when the index cannot be read exactly -- then the walk alone
    decides, as before.

    Read only when `.git` is a real directory inside `repo` and `index` a regular file
    in it (a linked worktree's `.git` file points outside the repository and is not
    followed). Versions 2, 3 and 4, SHA-1 or SHA-256 (told by the trailing checksum,
    which must match); a split or sparse index, a sparse directory entry, a short read
    or any other surprise gives None. Stage entries of a conflict collapse to one path.
    """
    gitdir = repo / ".git"
    index = gitdir / "index"
    try:
        if not _parent_inside(index, repo) or gitdir.is_symlink() or not gitdir.is_dir():
            return None
        if index.is_symlink() or not index.is_file():
            return None
        if index.stat().st_size > _INDEX_MAX_BYTES:
            return None
        data = index.read_bytes()
    except (OSError, ValueError, RuntimeError):
        return None
    try:
        return _parse_index(data)
    except (ValueError, IndexError, UnicodeDecodeError, struct.error):
        return None


def _parse_index(data: bytes) -> frozenset[str] | None:
    """The paths of a git index file's entries; None when it is not exactly readable."""
    if len(data) < 12 + 20 or data[:4] != b"DIRC":
        return None
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3, 4):
        return None
    for algo, size in (("sha1", 20), ("sha256", 32)):
        if len(data) >= 12 + size and hashlib.new(algo, data[:-size]).digest() == data[-size:]:
            hash_len = size
            break
    else:
        return None
    end = len(data) - hash_len
    pos, previous, paths = 12, b"", set()
    for _ in range(count):
        start = pos
        fixed = 40 + hash_len + 2           # stat fields, object id, flags
        if pos + fixed > end:
            return None
        mode = struct.unpack(">I", data[pos + 24:pos + 28])[0]
        flags = struct.unpack(">H", data[pos + 40 + hash_len:pos + fixed])[0]
        pos += fixed
        if flags & 0x4000:                  # extended flags (version 3 and later)
            if version < 3:
                return None
            pos += 2
        if version == 4:
            byte = data[pos]                # git's offset varint (varint.c)
            pos += 1
            strip = byte & 0x7F
            while byte & 0x80:
                byte = data[pos]
                pos += 1
                strip = ((strip + 1) << 7) | (byte & 0x7F)
            nul = data.index(b"\0", pos, end)
            if strip > len(previous):
                return None
            name = previous[:len(previous) - strip] + data[pos:nul]
            pos = nul + 1
        else:
            nul = data.index(b"\0", pos, end)
            name = data[pos:nul]
            pos = start + ((nul - start) // 8 + 1) * 8     # 1-8 NULs of padding
        if (mode >> 12) == 0o04:            # a sparse directory entry
            return None
        previous = name
        paths.add(name.decode("utf-8"))
    while pos + 8 <= end:                   # extensions
        sig = data[pos:pos + 4]
        size = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        if sig in _INDEX_PARTIAL_EXTENSIONS:
            return None
        pos += 8 + size
    if pos != end:
        return None
    return frozenset(paths)


def _tracked_under(entry: str, tracked: frozenset[str] | None) -> set[str]:
    """The tracked paths that are `entry` or lie under it."""
    if not tracked:
        return set()
    return {p for p in tracked if p == entry or p.startswith(entry + "/")}


def item_source_paths(capability: Mapping | None, repo: Path, *,
                      probe: Mapping | None = None) -> list[str]:
    """Every file an item's source digest covers, sorted and repository-relative.

    The paths its matrix row's evidence names (`evidence_paths` under `repo`), and --
    when the item's register probe is a durability record (`deriver ==
    "durability_e2e.py"`) -- the record itself and the record's own sources:
    `durability_e2e.RELEVANT_PATHS`, a file entry as itself and a directory walked.
    Revision 5: the row's STALE clause names three of the changed paths, so an edit to
    `src/hoh/runner.py` used to move no P2 item although six rest on it. The record's
    evidence directory is not digested; the record's re-derivation reads it.
    """
    evidence = (capability or {}).get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    paths = set(evidence_paths(evidence, repo))
    if isinstance(probe, Mapping) and probe.get("deriver") == DURABILITY_DERIVER:
        record = probe.get("path")
        if isinstance(record, str) and _relative(record):
            paths.add(_relative(record))
        # T-PSD3: `stale_paths` sees tracked files through `git diff` even where
        # `.gitignore` matches them (added with `git add -f`); the walk, which drops
        # ignored names, does not. The index supplies them, read without git.
        tracked = _tracked_paths(repo)
        for entry in _relevant_paths():
            paths.update(_walk(entry, repo))
            paths.update(_tracked_under(entry, tracked))
    return sorted(paths)


def _file_digest(path: str, repo: Path) -> str:
    """sha256 of the file's bytes, `SYMLINK` plus the link text for a symlink, or
    `MISSING_FILE`. A path that resolves outside `repo` -- absolute, `..`, or through a
    symlinked directory -- is never read, and a symlink is never followed.

    Revision 6 item 1: the directory holding the path must resolve inside `repo` before
    the path itself is looked at. The candidate called `os.readlink` first, so `a/evil`
    with `a -> ../outside` recorded the text of a link outside the repository."""
    try:
        if Path(path).is_absolute() or _relative(path) is None:
            return MISSING_FILE
        link = repo / path
        if not _parent_inside(link, repo):
            return MISSING_FILE
        if link.is_symlink():
            return SYMLINK + os.readlink(link)
        target = link.resolve()
        if not target.is_relative_to(repo.resolve()) or not target.is_file():
            return MISSING_FILE
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except (OSError, ValueError, RuntimeError):
        return MISSING_FILE


def item_source_digest(capability: Mapping | None, repo: Path, *,
                       probe: Mapping | None = None) -> str:
    """sha256 over the sorted `(path, sha256-of-content)` pairs of `item_source_paths`,
    resolved against `repo`.

    A missing file contributes `(path, "<missing>")`, so a file disappearing is drift
    too; a symlink contributes `(path, "<symlink>" + its link text)`. A row naming no
    file -- or no row at all -- is the digest of the empty list: there is nothing to
    have drifted, and that is said by the digest being that one.
    """
    return _sha256([[path, _file_digest(path, repo)]
                    for path in item_source_paths(capability, repo, probe=probe)])


# --------------------------------------------------------------------------- #
# the programme's reconciled state
# --------------------------------------------------------------------------- #


def reconciled(register: Mapping, matrix: Mapping
               ) -> tuple[Snapshot, dict[str, ItemEvidence], list[ChecklistItem]]:
    """This programme's checklist, derived by `programme_checklist.build` and
    sealed by `reconcile` -- exactly as the checklist binding derives it.

    An unsound checklist raises: a review capsule about a checklist `fsck`
    refuses would send a supervisor to review a verdict nobody may report.
    """
    items, evidence = build(dict(register), dict(matrix))
    # O220/F9, and a reviewer caught that the repair reached only one of the two
    # callers. These two sets used to be derived from the very items being checked --
    # `{item.requirement_ref for item in items}` and `{item.source_digest ...}` --
    # which makes `fsck`'s "release-blocking requirement with no item" check
    # unanswerable here. This is the *more* authoritative caller: it builds the
    # capsule the P2 gate's review is performed against. It now reuses the checklist
    # binding's external sets, so both callers ask the same question.
    requirements = declared_requirement_refs(dict(register))
    digests = {plan_revision(dict(register))}
    broken = fsck(items, requirements=requirements, known_digests=digests)
    if broken:
        raise ValueError(f"the checklist is not sound ({len(broken)} problem(s)): "
                         + "; ".join(broken[:5]))
    snapshot = reconcile(items, evidence=evidence, generation=1)
    after = fsck(snapshot.items, snapshot=snapshot, requirements=requirements,
                 known_digests=digests)
    if after:
        raise ValueError("the reconciled checklist is not sound: " + "; ".join(after[:5]))
    return snapshot, evidence, items


# --------------------------------------------------------------------------- #
# the capsule
# --------------------------------------------------------------------------- #


class Selection(Strict):
    """What the capsule shows of its phase and what it leaves out, by which rule.

    Recorded whether or not the budget bound: `left_out == 0` is itself a
    statement a supervisor can rely on ("you are seeing the whole phase").
    """

    phase: str
    considered: int
    included: int
    left_out: int
    left_out_ids: list[str] = Field(default_factory=list)
    rule: str = SELECTION_RULE
    max_items: int
    evidence_considered: int = 0
    evidence_included: int = 0
    evidence_left_out_ids: list[str] = Field(default_factory=list)
    max_evidence: int
    source_seal: str
    source_generation: int
    matrix_digest: str
    #: Every chosen item -> `item_source_digest` of its matrix row, so the next review
    #: has a baseline to measure drift against. Defaulted, like `drift` and
    #: `drift_baseline`, because capsules preserved before these fields existed must
    #: still load.
    source_digests: dict[str, str] = Field(default_factory=dict)
    #: F10/M7: whether `changed_source_digests` was measured. `{}` under NOT_RUN means
    #: "nobody looked", never "nothing changed".
    drift: Literal["MEASURED", "NOT_RUN"] = "NOT_RUN"
    #: The review id the drift was measured against; None exactly when NOT_RUN.
    drift_baseline: str | None = None
    #: Why drift is NOT_RUN (`drift_baseline_for`'s reason); None exactly when MEASURED.
    #: A capsule preserved before these fields existed has none and still loads.
    drift_reason: str | None = None

    #: The four drift fields at their defaults -- the shape of a capsule preserved
    #: before they existed, which `capsule_digest` digests without them.
    DRIFT_DEFAULTS: ClassVar[dict] = {"source_digests": {}, "drift": "NOT_RUN",
                                      "drift_baseline": None, "drift_reason": None}

    @model_validator(mode="after")
    def _adds_up(self) -> Selection:
        if self.included + self.left_out != self.considered:
            raise ValueError("a selection must account for every item it considered")
        if len(self.left_out_ids) != self.left_out:
            raise ValueError("a selection must name every item it left out")
        if self.drift == "MEASURED" and self.drift_baseline is None:
            raise ValueError("drift MEASURED must name the review it was measured against")
        if self.drift == "NOT_RUN" and self.drift_baseline is not None:
            raise ValueError("drift NOT_RUN cannot name a baseline it did not measure against")
        if self.drift == "MEASURED" and self.drift_reason is not None:
            raise ValueError("drift MEASURED has no reason for not being measured")
        # Exempt only the legacy shape: a capsule with source digests was built by
        # `capsule_for`, which always states why drift was not measured.
        if self.drift == "NOT_RUN" and self.source_digests and not self.drift_reason:
            raise ValueError("drift NOT_RUN must say why it was not measured")
        return self

    def has_legacy_drift_fields(self) -> bool:
        """Do all four drift fields hold their defaults?"""
        return all(getattr(self, k) == v for k, v in self.DRIFT_DEFAULTS.items())

    def drift_clause(self, changed: int) -> str:
        if self.drift == "NOT_RUN":
            return f"drift NOT_RUN: {self.drift_reason or 'not measured'}"
        return (f"drift MEASURED: {changed} of {self.included} item(s) "
                f"changed against {self.drift_baseline}")

    def summary_line(self, max_chars: int, *, changed: int = 0) -> str:
        """The selection as the first evidence summary, so a supervisor handed
        only the base capsule still reads what it was not shown.

        The drift clause comes first after the label (Revision 5 rule 6), so no
        cut below can remove it, and a limit too short to hold it is refused
        rather than cut; `changed` is how many chosen items drifted."""
        drift = self.drift_clause(changed)
        label = f"SELECTION: {drift}"
        if max_chars < len(label):
            raise ValueError(
                f"max_summary_chars={max_chars} cannot hold the drift clause "
                f"({len(label)} chars: {label!r}); it is refused rather than cut")
        if self.left_out:
            head = (f"{label}; this capsule shows {self.included} of the "
                    f"{self.considered} item(s) of phase {self.phase}; "
                    f"{self.left_out} left out by rule [{self.rule}]; left out: ")
        else:
            head = (f"{label}; this capsule shows all {self.considered} item(s) of "
                    f"phase {self.phase}; nothing left out")
        tail = ""
        if self.evidence_left_out_ids:
            tail = (f"; {len(self.evidence_left_out_ids)} evidence summary(ies) left "
                    f"out of {self.evidence_considered}")
        if not self.left_out:
            return (head + tail)[:max_chars]
        ids = list(self.left_out_ids)
        for keep in range(len(ids), -1, -1):
            shown = ", ".join(ids[:keep])
            more = "" if keep == len(ids) else f" (+{len(ids) - keep} more, full list in selection)"
            line = head + shown + more + tail
            if len(line) <= max_chars:
                return line
        return line[:max_chars]


class ProgrammeCapsule(ReviewCapsule):
    """A `ReviewCapsule` that carries its own selection record.

    It is a `ReviewCapsule` in every respect -- including its bound check,
    which runs over the selected items -- plus `selection`, so what was left
    out is inspectable on the object and in its JSON, not only in prose.
    """

    selection: Selection


def _rank(item: ChecklistItem) -> tuple[int, int, str]:
    return (0 if item.criticality in BLOCKING else 1,
            0 if item.status not in _SETTLED else 1,
            item.item_id)


def _evidence_line(item: ChecklistItem, measured: Mapping | None, max_chars: int) -> str:
    if measured is None:
        seen = "never measured by the capability matrix"
    else:
        refs = "; ".join(str(e) for e in measured.get("evidence") or []) or "none"
        seen = f"matrix status {measured.get('status')}; evidence: {refs}"
    line = (f"{item.item_id} [{item.criticality.value}] {item.status.value} "
            f"-- {item.title}: {seen}")
    line = " ".join(line.split())
    return line if len(line) <= max_chars else line[:max_chars - 3] + "..."


def capsule_for(phase: str, *, register: Mapping, matrix: Mapping,
                max_items: int = 50, max_evidence: int = 10,
                max_summary_chars: int = 500,
                open_findings: Iterable = (),
                previous_source_digests: Mapping[str, str] | None = None,
                previous_review_id: str | None = None,
                drift_reason: str | None = None,
                repo: Path = HOH) -> ProgrammeCapsule:
    """The bounded brief for a review of `phase`, from the real register and matrix.

    Selection (see `SELECTION_RULE`): only items of `phase`; phase-blocking
    before non-blocking; open items before settled ones; cut at `max_items`.
    The cut is recorded in `selection` and in the first evidence summary --
    never silent. Evidence summaries go to the selected open items first, and
    those that do not fit `max_evidence` are named in the selection too.

    A phase the snapshot does not contain raises ValueError before any capsule
    is constructed: `ReviewCapsule` refuses such a capsule, and this does not
    build one to find out.

    Source drift (F10/M7): every chosen item's `item_source_digest` under `repo`, with
    its register probe, is recorded in `selection.source_digests`. With
    `previous_source_digests` -- the baseline a preserved earlier review recorded --
    drift is MEASURED, and `changed_source_digests` holds every chosen item whose digest
    differs from the baseline or is absent from it. With `None` it is NOT_RUN, for
    `drift_reason` (default `no preserved baseline`), and the first evidence summary says
    so before anything else: an empty mapping nobody measured must not read as "nothing
    changed". An empty baseline is refused -- it measures nothing.
    """
    snapshot, evidence, built = reconciled(register, matrix)
    of_phase = [item for item in snapshot.items if item.phase == phase]
    if not of_phase:
        phases = sorted({i.phase for i in snapshot.items}, key=lambda p: (len(p), p))
        raise ValueError(
            f"phase {phase!r} has no item in this programme's checklist "
            f"(phases: {', '.join(phases) or 'none'}); a capsule about it would "
            f"send a supervisor to review nothing")
    if max_items < 1 or max_evidence < 1 or max_summary_chars < 1:
        raise ValueError("max_items, max_evidence and max_summary_chars must be >= 1")

    ranked = sorted(of_phase, key=_rank)
    chosen, dropped = ranked[:max_items], ranked[max_items:]

    measured = {c["id"]: c for c in matrix.get("capabilities") or []}
    # One slot is the selection line; the rest go to the chosen items, open ones
    # first (the ranking already puts them there within each criticality).
    candidates = sorted(chosen, key=lambda i: (i.status in _SETTLED, _rank(i)))
    room = max_evidence - 1
    summarised, unsummarised = candidates[:room], candidates[room:]

    probes = {str(r.get("id")): r.get("probe")
              for r in register.get("requirements") or [] if isinstance(r, Mapping)}
    source_digests = {i.item_id: item_source_digest(measured.get(i.item_id), repo,
                                                    probe=probes.get(i.item_id))
                      for i in chosen}
    if previous_source_digests is None:
        changed: dict[str, str] = {}
        reason = drift_reason or "no preserved baseline"
    else:
        if previous_review_id is None:
            raise ValueError("a drift baseline must name the review it came from")
        if drift_reason is not None:
            raise ValueError("a measured drift has no reason for not being measured")
        if not previous_source_digests:
            raise ValueError("an empty drift baseline measures nothing; pass None, "
                             "which says NOT_RUN")
        changed = {item_id: digest for item_id, digest in source_digests.items()
                   if previous_source_digests.get(item_id) != digest}
        reason = None

    selection = Selection(
        phase=phase, considered=len(ranked), included=len(chosen),
        left_out=len(dropped), left_out_ids=[i.item_id for i in dropped],
        max_items=max_items,
        evidence_considered=len(candidates), evidence_included=len(summarised),
        evidence_left_out_ids=[i.item_id for i in unsummarised],
        max_evidence=max_evidence,
        source_seal=snapshot.seal, source_generation=snapshot.generation,
        matrix_digest=matrix_digest(matrix),
        source_digests=source_digests,
        drift="NOT_RUN" if previous_source_digests is None else "MEASURED",
        drift_baseline=None if previous_source_digests is None else previous_review_id,
        drift_reason=reason,
    )
    # Before anything is reconciled: a limit too short for the drift clause is refused.
    first_summary = selection.summary_line(max_summary_chars, changed=len(changed))

    # The capsule's snapshot is of the selected items only, so its own bound
    # check passes -- and it is produced by `reconcile`, the only producer of a
    # seal, from the same evidence, so every status in it is the status the
    # full reconciliation decided.
    chosen_ids = {i.item_id for i in chosen}
    subset = reconcile(
        [i for i in built if i.item_id in chosen_ids],
        evidence={k: v for k, v in evidence.items() if k in chosen_ids},
        generation=snapshot.generation)
    decided = {i.item_id: i.status for i in snapshot.items}
    drift = [i.item_id for i in subset.items if i.status is not decided[i.item_id]]
    if drift:
        raise ValueError(f"re-reconciling the selection changed statuses of "
                         f"{', '.join(drift)}; the capsule would misreport them")

    summaries = [first_summary]
    summaries += [_evidence_line(i, measured.get(i.item_id), max_summary_chars)
                  for i in summarised]
    # O228/K1, and it is the repair that makes the O229/G doctrine mean anything.
    # The doctrine says a finding stops blocking when a *fresh review over the current
    # state does not raise it* -- and nothing was carrying the findings forward, so the
    # next supervisor was handed the board and none of them. A review returning `[]`
    # then "did not raise" them by construction rather than by examination, which made
    # the gate cheaper to satisfy without a review than the rule it replaced.
    #
    # `ReviewCapsule` has always had the field and always refused a *decided* finding
    # in it ("a supervisor asked to weigh a closed finding is being asked to reopen a
    # decision it may not take"). Under the new doctrine findings are never decided, so
    # they stay OPEN and can be carried -- the doctrine change is what makes the
    # product's own designed route usable, and not wiring it was my omission.
    carried = [f if isinstance(f, SupervisorFinding)
               else SupervisorFinding.model_validate(dict(f))
               for f in open_findings]
    return ProgrammeCapsule(
        phase=phase, snapshot=subset, evidence_summaries=summaries,
        open_findings=carried, changed_source_digests=changed,
        max_items=max_items, max_evidence=max_evidence,
        max_summary_chars=max_summary_chars, selection=selection)


def capsule_digest(capsule: ReviewCapsule) -> str:
    """sha256 over the capsule's canonical JSON -- what the supervisor was given.

    The four drift fields of `selection` are left out exactly when all four hold their
    defaults (Revision 5 rule 4): that is the shape of a capsule preserved before they
    existed, whose digest a review record already binds and must keep. A capsule
    `capsule_for` builds always carries source digests, so it is always digested whole.
    """
    body = capsule.model_dump(mode="json", by_alias=True)
    selection = getattr(capsule, "selection", None)
    if isinstance(selection, Selection) and selection.has_legacy_drift_fields():
        for key in Selection.DRIFT_DEFAULTS:
            body["selection"].pop(key, None)
    return _sha256(body)


# --------------------------------------------------------------------------- #
# the review record
# --------------------------------------------------------------------------- #


class ValidationFailure(Strict):
    """A finding the product refused: named, with the product's reasons."""

    finding_id: str
    reasons: list[str]


class ReviewRecord(BaseModel):
    """What a completed (or still open) review leaves behind. Frozen.

    `blocking_findings` and `complete` are **computed** from the findings and
    the decisions recorded against them; neither can be handed in (unknown
    fields are refused) nor assigned (the record is frozen). The gate reads
    `complete`:

    * every finding is a *finding of nothing* -- `NO_ACTION` at a severity that agrees
      with it -- or has been resolved by a later review that looked again and found
      nothing there. **A decision does not settle a finding** (O229/G): five rounds of
      review each defeated the then-current definition of a sufficient decision with one
      keystroke, because a decision is written by the party whose phase is being gated.
      O228/K4 said this docstring had been corrected and it had not, which is the
      O226/F-4 failure -- a claimed correction that never landed -- repeated;
    * no finding the supervisor returned was refused by `supervisor.validate`
      -- a refused finding is not a finding, but it is not an answer either,
      and a gate that closed over it would close over something unread.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    review_id: str
    phase: str
    reviewed_at: str
    capsule_digest: str
    #: The limits the capsule was built under. Recorded because `capsule_digest`
    #: alone cannot be re-derived without them: a gate that wants to prove the review
    #: looked at *this* phase in *this* state rebuilds the capsule and compares, and a
    #: rebuild under different limits is a different capsule. A reviewer closed the P2
    #: gate with a zero-finding review of P1 written to P2's path, which the phase
    #: check catches; this is what makes the stronger check possible.
    capsule_max_items: int | None = None
    capsule_max_evidence: int | None = None
    #: O226/E10. The third limit was not recorded, so a review taken with a
    #: non-default `max_summary_chars` could never match a rebuilt capsule and could
    #: never be current -- the "gate that can never close" failure, one field wide.
    capsule_max_summary_chars: int | None = None
    #: O226/E2, critical. The record bound itself to the supervisor's *input* (the
    #: capsule) and to nothing on the way out, so the findings could be edited freely
    #: after the fact: handing `--findings` the real fourteen with the nine blocking
    #: ones deleted produced `complete (5 findings)` under the genuine review's own id.
    #: These two bind the output: the digest is over the finding set as returned, and
    #: the source names the preserved file a gate can re-read and re-digest. Neither
    #: makes forgery impossible at this layer -- nothing here can -- but a forgery now
    #: has to alter a second, separately preserved artefact and leaves the mismatch
    #: visible to anyone who compares them.
    findings_digest: str | None = None
    findings_source: str | None = None
    snapshot_seal: str
    matrix_digest: str | None
    findings: tuple[SupervisorFinding, ...] = ()
    applied: tuple[str, ...] = ()
    #: For each applied finding, a reference to the evidence that the repair exists:
    #: a commit, a ledger entry, a test node, a preserved artefact. O224/D2: a
    #: decision of "apply" records an *intention*, and this layer cannot see a tree,
    #: so an apply with no evidence reference leaves the finding blocking. Before this
    #: field, writing `{"F1": "apply"}` beside a critical finding settled it -- and the
    #: record produced that way marked all fourteen findings applied, at least three of whose repairs did not exist.
    repair_evidence: dict[str, str] = Field(default_factory=dict)
    rejected: tuple[SupervisorFinding, ...] = ()
    validation_failures: tuple[ValidationFailure, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> ReviewRecord:
        ids = [f.finding_id for f in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("a review record names a finding twice")
        known = set(ids)
        rejected_ids = [f.finding_id for f in self.rejected]
        stray = sorted((set(self.applied) | set(rejected_ids)) - known)
        if stray:
            raise ValueError(f"decisions about findings the review does not hold: "
                             f"{', '.join(stray)}")
        both = sorted(set(self.applied) & set(rejected_ids))
        if both:
            raise ValueError(f"findings both applied and rejected: {', '.join(both)}")
        for f in self.rejected:
            if f.status is not FindingStatus.REJECTED or not (f.rejection_reason or "").strip():
                raise ValueError(f"finding {f.finding_id} is listed as rejected without "
                                 f"a recorded rejection")
        return self

    #: Kinds a decision cannot settle by agreement. O222: applying a `PHASE_NOT_READY`
    #: reopens nothing -- `supervisor.apply_finding` says so in as many words for a
    #: finding whose `item_ids` name a phase -- and the snapshot it returns is
    #: discarded here. So writing `{"F1": "apply"}` next to a critical finding whose
    #: own proposed action is "hold phase P2 open" removed it from the blocking set
    #: and made this review `complete`. A reviewer demonstrated it against the real
    #: record. "Applied" has to mean the repair exists in the tree, and this layer
    #: cannot know that, so for these two kinds it must not pretend to.
    HELD_UNTIL_REJECTED: ClassVar[tuple[FindingKind, ...]] = (
        FindingKind.PHASE_NOT_READY, FindingKind.RELEASE_NOT_READY)

    #: What a rejection of one of those kinds has to carry. O225/C3: the previous
    #: rule made a rejection the *only* way to settle them, and `reject_finding`
    #: requires only a non-blank reason -- so `{"F1": "x"}` discharged a *critical*
    #: `PHASE_NOT_READY` whose own text is "P2's exit conditions are not proven",
    #: attributed by default to `programme-orchestrator`, the party under review.
    #: One character, signed by the defendant.
    #:
    #: The floor is a weak instrument and is documented as one: length cannot make a
    #: statement true. The **actor** requirement is the substantive half -- someone
    #: other than the party whose phase is being held has to be on record saying the
    #: phase is ready -- and the floor only stops a placeholder from wearing the
    #: shape of a reasoned disagreement.
    PHASE_REJECTION_MIN_CHARS: ClassVar[int] = 80
    #: O226/E3. The first version compared the actor string for equality against this
    #: set, so the defendant signed off its own phase with the shift key:
    #: `Programme-Orchestrator`, `programme_orchestrator` and
    #: `programme-orchestrator2` all passed while `programme-orchestrator` was
    #: refused. A rule a reviewer defeats by capitalisation is not a rule, and the
    #: audit trail it leaves reads as the defendant's own name beside a green gate.
    #: The comparison is now over `_normalised_actor` -- case-folded, stripped of
    #: everything but letters and digits -- and it is a *containment* test, so a
    #: suffixed variant of a forbidden name is still that name.
    PHASE_REJECTION_FORBIDDEN_ACTORS: ClassVar[frozenset[str]] = frozenset(
        {"programme-orchestrator", ""})

    @computed_field
    @property
    def blocking_findings(self) -> tuple[str, ...]:
        """Findings this review has not settled.

        O224/D2. The first guard was keyed on finding *kind* -- only
        `PHASE_NOT_READY` and `RELEASE_NOT_READY` resisted being settled by agreement
        -- and a reviewer flipped F1's kind, with the same critical severity and the
        same "hold phase P2 open" text, and the review completed again. Keying on kind
        made the rule evadable by relabelling.

        ## O229/G, and this is a doctrine change rather than another predicate

        Five rounds of review produced this sequence. Each entry hardened the
        condition under which a *decision* settles a finding, and each next round
        defeated the new predicate with one keystroke:

        | round | what settled a critical finding |
        |---|---|
        | 2 | `{"F1": "apply"}` |
        | 3 | `{"F1": "x"}` -- one character |
        | 4 | the same, respelled: `Programme-Orchestrator` for the actor |
        | 5 | `{"F2": "x"}` for any non-phase kind; `"0.0"` as repair evidence |

        The predicates were not too weak. **The premise was wrong.** A decision is
        written by the party whose phase is being gated, so every rule of the form
        "a sufficiently good decision settles the finding" is a rule about how much
        typing that party must do. A length floor, a blocklist of actor names and a
        regex over an evidence string are three spellings of the same mistake, and the
        blocklist was the clearest proof: it refused `programme-orchestrator` and
        accepted `me`.

        The plan's own rule is the way out, and it was there from the beginning:

            Supervisor can FIND. Reconciler can VERIFY.

        Deciding is neither. So, now:

        * `NO_ACTION` does not block -- a supervisor saying "nothing here" is a
          finding of nothing, which is an answer.
        * **Every other finding blocks, and no decision changes that.** A finding
          stops blocking when a *fresh review over the current state does not raise
          it*, which is the only event that is evidence about the thing the finding
          was about.

        `applied`, `rejected` and `repair_evidence` are still recorded, because what
        the orchestrator intended and pointed at is worth preserving -- and
        `applied_without_evidence` and `thin_rejections` still report on their quality.
        None of them settles anything, so none of them is worth forging. The attack
        surface that five rounds kept re-opening is simply gone: there is nothing in a
        decisions file that can close this gate.

        The cost is that closing P2 now requires retaking the review rather than
        answering it, which is more work and is the point. `PHASE_REJECTION_MIN_CHARS`,
        `PHASE_REJECTION_FORBIDDEN_ACTORS` and `evidence_reference_problems` are kept
        for the diagnostics they feed, and are deliberately no longer load-bearing.
        """
        return tuple(f.finding_id for f in self.findings
                     if not self._is_a_finding_of_nothing(f))

    #: What severity a genuine "nothing here" may carry. O228/J3: `NO_ACTION` was the
    #: single non-blocking kind, and `supervisor.validate` refuses it only for carrying
    #: a `proposed_action` -- so relabelling a finding cost `kind -> NO_ACTION` and
    #: `proposed_action -> null`, while `finding_id`, `severity` and the entire `reason`
    #: survived verbatim. A reviewer relabelled all fourteen and the board printed
    #: `14 finding(s), 0 undecided, complete` with the critical text still in the file.
    #:
    #: This is exactly the evasion-by-relabelling that O225/C3 named about the *previous*
    #: rule, reintroduced by the rule that replaced it. `severity` is free text in the
    #: product's schema, so the check is a stated vocabulary rather than an enum: a
    #: finding of nothing that calls itself critical is not a finding of nothing, and
    #: downgrading the severity too is a visible edit to the finding's own content.
    #: O229/L4, the availability half: the vocabulary is closed over a free-text field, so
    #: honest spellings were refused. A supervisor writing `kind: NO_ACTION, severity:
    #: minor` is saying nothing is wrong, and the gate held the phase open on it -- whose
    #: only remedy was for the gated party to *rewrite the supervisor's own severity word*,
    #: which is the last thing this gate should reward. Widened to the obvious synonyms,
    #: and it stays a stated list so the next reader can argue with it.
    NO_ACTION_SEVERITIES: ClassVar[frozenset[str]] = frozenset(
        {"low", "info", "informational", "none", "nil", "n/a", "na", "minor", "trivial",
         "note", "notice", "advisory", "negligible", "informative", "cosmetic", "nit"})

    @classmethod
    def _is_a_finding_of_nothing(cls, finding) -> bool:
        """Is this a supervisor saying "nothing here", which is a complete answer?

        `NO_ACTION` *and* a severity that agrees with it. A critical `NO_ACTION` is a
        contradiction, and treating it as an answer is what let fourteen findings be
        relabelled into silence with their reasons intact (O228/J3).
        """
        if finding.kind is not FindingKind.NO_ACTION:
            return False
        return str(finding.severity or "").strip().casefold() in cls.NO_ACTION_SEVERITIES

    @classmethod
    def _thin_rejection(cls, rejected) -> bool:
        """Is this rejection too thin to hold a phase-level finding open no longer?

        Returns True when the finding stays blocking. Two ways to be thin: a reason
        below the floor, and an actor who is the party under review (or none at all).
        """
        reason = " ".join(str(rejected.rejection_reason or "").split())
        return (len(reason) < cls.PHASE_REJECTION_MIN_CHARS
                or cls._actor_is_forbidden(rejected.rejected_by))

    @staticmethod
    def _normalised_actor(actor) -> str:
        """An actor name reduced to what identifies it: lowercase letters and digits.

        `Programme-Orchestrator`, `programme_orchestrator` and
        ` PROGRAMME ORCHESTRATOR ` are one party wearing three spellings, and an
        equality test over the raw string treated them as three parties (O226/E3).
        """
        return "".join(c for c in str(actor or "").casefold() if c.isalnum())

    @classmethod
    def _actor_is_forbidden(cls, actor) -> bool:
        """Is this actor one the phase-level rules refuse, however it is spelled?

        Containment, not equality: `programme-orchestrator2` is the orchestrator with
        a digit stuck on, and a rule that lets that through is decoration.
        """
        got = cls._normalised_actor(actor)
        if not got:
            return True
        return any(cls._normalised_actor(bad) in got
                   for bad in cls.PHASE_REJECTION_FORBIDDEN_ACTORS
                   if cls._normalised_actor(bad))

    @computed_field
    @property
    def thin_rejections(self) -> tuple[str, ...]:
        """Phase-level findings whose rejection is below the bar, named rather than
        silently ignored: the board has to be able to say *why* a finding it was told
        was settled is still blocking."""
        by_id = {f.finding_id: f for f in self.findings}
        return tuple(
            r.finding_id for r in self.rejected
            if r.finding_id in by_id
            and by_id[r.finding_id].kind in self.HELD_UNTIL_REJECTED
            and self._thin_rejection(r))

    @computed_field
    @property
    def applied_without_evidence(self) -> tuple[str, ...]:
        """Findings recorded as applied that name no evidence for the repair.

        Reported rather than hidden: the record that prompted this field marked
        fourteen findings applied, and at least three of those repairs demonstrably
        did not exist in the tree.
        """
        return tuple(f for f in self.applied
                     if not str(self.repair_evidence.get(f, "")).strip())

    @computed_field
    @property
    def complete(self) -> bool:
        return not self.blocking_findings and not self.validation_failures


#: The fields a record derives rather than carries. Named here because every caller
#: that reads a written record back has to strip them before validating, and the one
#: that hard-coded two of the three broke when the third arrived -- a list that lives
#: beside the model cannot fall behind it.
COMPUTED_FIELDS: tuple[str, ...] = (
    "complete", "blocking_findings", "applied_without_evidence", "thin_rejections")


def load_record(data: Mapping, *, snapshot: Snapshot | None = None) -> ReviewRecord:
    """Read a written record back, believing as little of it as possible.

    Every member of `COMPUTED_FIELDS` is recomputed and a stored value that disagrees
    is refused rather than believed. A field that is absent is not an agreement: it
    means nothing was stored, so there is nothing to disagree with, and the
    recomputation stands either way.

    With `snapshot`, the findings themselves go back through `supervisor.validate`.
    Without it, this function cannot tell whether the findings are about the snapshot
    the record names -- `validation_failures` is an input to `complete` and is the one
    input a record can carry rather than earn. **A caller that gates on the result
    must pass the snapshot**; `readiness.py` does, and the reason it must is O226/E1:
    a record with `validation_failures` emptied by hand, carrying a finding about an
    item no snapshot contains, loaded clean and published as PASS.
    """
    body = dict(data)
    stored = {name: body.pop(name, None) for name in COMPUTED_FIELDS}
    record = ReviewRecord.model_validate(body)

    # O226/E6, and both round-four reviewers found it independently: this used to be
    # three hand-rolled comparisons beside a four-member `COMPUTED_FIELDS`, and the
    # member C3 added was popped from the body and then never compared. The list did
    # not fall behind the model; the *checking* did. A loop cannot.
    for name in COMPUTED_FIELDS:
        if stored[name] is None:                    # not stored: nothing to disagree
            continue
        recomputed = getattr(record, name)
        got = tuple(stored[name]) if isinstance(recomputed, tuple) else stored[name]
        if got != recomputed:
            raise ValueError(
                f"the record says {name}={stored[name]!r}; its findings and decisions "
                f"say {recomputed!r}")

    # O226/E1, critical. `validation_failures` is an *input* to `complete` and was the
    # one input this function could not recompute, because it had no snapshot -- so a
    # record with one array emptied by hand loaded clean and the board printed PASS.
    # That is O225/C1 one field over: the row read the verdict out of the file, just
    # not the field C1 named. With a snapshot in hand the findings are put back
    # through `supervisor.validate`, which is what produced the field in the first
    # place, and a finding the product refuses is refused here too -- a record may not
    # carry a finding about an item that is not in the snapshot it claims to review.
    if snapshot is not None:
        recomputed_failures = [
            ValidationFailure(finding_id=f.finding_id, reasons=problems)
            for f in record.findings
            if (problems := supervisor.validate(f, snapshot=snapshot))]
        if recomputed_failures:
            named = ", ".join(f.finding_id for f in recomputed_failures)
            raise ValueError(
                f"the record carries finding(s) the product refuses against this "
                f"snapshot: {named} -- "
                f"{'; '.join(recomputed_failures[0].reasons)}")
    return record


def _finding_name(raw, index: int) -> str:
    if isinstance(raw, SupervisorFinding):
        return raw.finding_id
    if isinstance(raw, Mapping) and str(raw.get("finding_id") or "").strip():
        return str(raw["finding_id"]).strip()
    return f"#{index}"


def record_review(phase: str, *, capsule: ReviewCapsule, findings: Iterable,
                  snapshot: Snapshot,
                  decisions: Mapping[str, str | Mapping[str, str]],
                  matrix_digest: str | None = None, review_id: str | None = None,
                  reviewed_at: str | None = None,
                  findings_source: str | None = None,
                  rejected_by: str = DEFAULT_DECIDER) -> ReviewRecord:
    """Record a review of `phase` performed on `capsule` against `snapshot`.

    `decisions` maps finding_id to `"apply"`, to a rejection reason, or to a mapping
    of `disposition`/`evidence`/`reason` -- the last being the only shape that can
    settle an applied finding, because an apply with no evidence reference records an
    intention rather than a repair (O224/D2). The `Mapping[str, str]` annotation this
    parameter used to carry described only the first two and was wrong from the moment
    the third arrived (O228/F-9).

    1. Every finding is validated with `supervisor.validate` against
       `snapshot` first; a refused one is recorded as a named validation
       failure, not as a finding.
    2. A finding (other than NO_ACTION) with no decision is recorded, lands in
       `blocking_findings`, and leaves `complete` false.
    3. A rejection goes through `supervisor.reject_finding`, so a blank reason
       raises ValueError there -- this does not route around it.
    4. NO_ACTION needs no decision and never blocks.
    5. "apply" goes through `supervisor.apply_finding`; the reopened, unsealed
       snapshot it returns is not kept -- a later `reconcile` decides.

    Refused with ValueError: a phase that is not the capsule's, an unsealed
    snapshot (a review bound to it is bound to nothing), a snapshot that is not
    the one a `ProgrammeCapsule` was built from, and a decision about a finding
    nobody returned.
    """
    if phase != capsule.phase:
        raise ValueError(f"the review is of phase {phase!r} and the capsule of "
                         f"{capsule.phase!r}")
    if not snapshot.is_sealed():
        raise ValueError("the snapshot carries no intact seal; a review bound to it "
                         "would be bound to nothing")
    selection = getattr(capsule, "selection", None)
    if selection is not None:
        if snapshot.seal != selection.source_seal:
            raise ValueError(
                "the capsule was built from a different snapshot (seal "
                f"{selection.source_seal[:12]}..., this one {snapshot.seal[:12]}...): "
                "the findings are about a board that is not this one")
        if matrix_digest is None:
            matrix_digest = selection.matrix_digest
        elif matrix_digest != selection.matrix_digest:
            raise ValueError("the capsule was built from a different matrix")

    accepted: list[SupervisorFinding] = []
    failures: list[ValidationFailure] = []
    seen: set[str] = set()
    returned: set[str] = set()
    raw_findings = list(findings)
    for index, raw in enumerate(raw_findings):
        name = _finding_name(raw, index)
        returned.add(name)
        problems = supervisor.validate(raw, snapshot=snapshot)
        if not problems and name in seen:
            problems = [f"finding {name} was returned twice"]
        if not problems and review_id is not None:
            own = raw.review_id if isinstance(raw, SupervisorFinding) else raw.get("review_id")
            if str(own).strip() != review_id:
                problems = [f"finding {name} belongs to review {own!r}, not {review_id!r}"]
        if problems:
            failures.append(ValidationFailure(finding_id=name, reasons=problems))
            continue
        finding = (SupervisorFinding.model_validate(raw.model_dump())
                   if isinstance(raw, SupervisorFinding)
                   else SupervisorFinding.model_validate(dict(raw)))
        seen.add(name)
        accepted.append(finding)

    unknown = sorted(set(decisions) - returned)
    if unknown:
        raise ValueError(f"decisions about findings the supervisor did not return: "
                         f"{', '.join(unknown)}")

    applied: list[str] = []
    repair_evidence: dict[str, str] = {}
    rejected: list[SupervisorFinding] = []
    for finding in accepted:
        if finding.finding_id not in decisions:
            continue
        decision = decisions[finding.finding_id]
        # O224/D2. A decision may be the bare word "apply", a rejection reason, or a
        # mapping carrying the evidence that the repair exists. The bare word is still
        # accepted -- refusing it would break every existing record -- but it now
        # settles nothing: `blocking_findings` requires `repair_evidence`, because this
        # layer cannot look at a tree and the record that prompted the field marked
        # fourteen findings applied while at least three of the repairs did not exist.
        evidence_ref = ""
        if isinstance(decision, dict):
            disposition = str(decision.get("disposition") or "").strip()
            evidence_ref = str(decision.get("evidence") or "").strip()
            reason = str(decision.get("reason") or "").strip()
        elif isinstance(decision, str):
            disposition = APPLY if decision.strip() == APPLY else ""
            reason = decision
        else:
            raise TypeError(
                f"the decision for {finding.finding_id} is neither a string nor a "
                "mapping of disposition/evidence/reason")
        if disposition == APPLY:
            supervisor.apply_finding(finding, snapshot=snapshot)   # result not kept
            applied.append(finding.finding_id)
            if evidence_ref:
                repair_evidence[finding.finding_id] = evidence_ref
        else:
            rejected.append(supervisor.reject_finding(
                finding, reason=reason, rejected_by=rejected_by))

    if review_id is None:
        owners = {f.review_id for f in accepted}
        if len(owners) > 1:
            # O226/E9. This used to fall through to a synthesised id, so findings from
            # two different reviews were stitched into one record without a word. A
            # record is the outcome of *a* review; if the findings disagree about which
            # one, the caller has mixed two sets and the only honest answer is to say so.
            raise ValueError(
                "the findings belong to more than one review ("
                + ", ".join(sorted(str(o) for o in owners))
                + "); a record is the outcome of one review, so pass review_id "
                "explicitly if they really are to be recorded together")
        review_id = (owners.pop() if owners
                     else "review-" + _sha256([capsule_digest(capsule), snapshot.seal])[:16])
    return ReviewRecord(
        review_id=review_id,
        phase=phase,
        findings_digest=findings_digest(raw_findings),
        findings_source=findings_source,
        reviewed_at=reviewed_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        capsule_digest=capsule_digest(capsule),
        capsule_max_items=getattr(capsule, "max_items", None),
        capsule_max_evidence=getattr(capsule, "max_evidence", None),
        capsule_max_summary_chars=getattr(capsule, "max_summary_chars", None),
        snapshot_seal=snapshot.seal,
        matrix_digest=matrix_digest,
        findings=tuple(accepted),
        applied=tuple(applied),
        repair_evidence=repair_evidence,
        rejected=tuple(rejected),
        validation_failures=tuple(failures),
    )


def stale_against(record: ReviewRecord, *, snapshot: Snapshot,
                  matrix_digest: str) -> list[str]:
    """Why `record` may no longer be relied on as a *current* review; `[]` when
    it still may. One reason per binding that moved."""
    out: list[str] = []
    if not snapshot.is_sealed():
        out.append("the current snapshot carries no intact seal, so nothing can be "
                   "current against it")
    if record.snapshot_seal != snapshot.seal:
        out.append(f"snapshot seal changed: reviewed {record.snapshot_seal[:12]}..., "
                   f"now {(snapshot.seal or 'none')[:12]}...")
    if record.matrix_digest is None:
        out.append("the review was not bound to a matrix digest")
    elif record.matrix_digest != matrix_digest:
        out.append(f"matrix digest changed: reviewed {record.matrix_digest[:12]}..., "
                   f"now {matrix_digest[:12]}...")
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, help="the phase under review, e.g. P2")
    ap.add_argument("--max-items", type=int, default=50)
    ap.add_argument("--max-evidence", type=int, default=10)
    ap.add_argument("--findings", type=Path, default=None,
                    help="JSON list of findings the supervisor returned")
    ap.add_argument("--decisions", type=Path, default=None,
                    help='JSON object: finding_id -> "apply" | rejection reason')
    ap.add_argument("--capsule", type=Path, default=None,
                    help="the capsule JSON the supervisor was given (default: rebuilt now)")
    ap.add_argument("--out", type=Path, default=None, help="where to write the review record")
    ap.add_argument("--rejected-by", default=DEFAULT_DECIDER)
    ap.add_argument("--canonical-brief", action="store_true",
                    help="print the one brief this gate accepts for the phase, which is "
                         "what a supervisor must be given (O229/L7)")
    args = ap.parse_args(argv)

    if not REGISTER.is_file() or not MATRIX.is_file():
        print("the register or the capability matrix is not in this tree, so no "
              "review capsule can be derived and no review recorded", file=sys.stderr)
        return 3
    register = _read_json(REGISTER)
    matrix = _read_json(MATRIX)

    recording = [args.findings, args.decisions, args.out]
    if any(recording) and not all(recording):
        ap.error("--findings, --decisions and --out are given together")

    try:
        if args.canonical_brief:
            built = canonical_brief(register=register, matrix=matrix, phase=args.phase,
                                    before=None)
        else:
            # F10/M7: the baseline is the most recent reviewed brief of the phase, by the
            # rule `canonical_brief` uses; without a usable one drift is NOT_RUN with the
            # reason -- never invented.
            # Its own tree is `HOH`, looked up here rather than bound as a default, so the
            # review root is checked against -- and the files digested in -- one tree.
            baseline, baseline_id, reason = drift_baseline_for(args.phase, before=None,
                                                               repo=HOH)
            built = capsule_for(args.phase, register=register, matrix=matrix,
                                max_items=args.max_items,
                                max_evidence=args.max_evidence,
                                previous_source_digests=baseline,
                                previous_review_id=baseline_id,
                                drift_reason=reason, repo=HOH)
    except ValueError as exc:
        print(f"no capsule: {exc}", file=sys.stderr)
        return 1

    if not any(recording):
        print(built.model_dump_json(indent=2, by_alias=True))
        return 0

    if args.out.exists():
        print(f"{args.out} exists; a review record is not overwritten -- "
              "choose a new path", file=sys.stderr)
        return 2
    capsule = (ProgrammeCapsule.model_validate(_read_json(args.capsule))
               if args.capsule else built)
    raw = _read_json(args.findings)
    findings = raw.get("findings", []) if isinstance(raw, dict) else raw
    decisions = _read_json(args.decisions)
    snapshot, _, _ = reconciled(register, matrix)
    try:
        record = record_review(args.phase, capsule=capsule, findings=findings,
                               snapshot=snapshot, decisions=decisions,
                               # O226/E2: the record names the file it was given, so a
                               # gate can re-read and re-digest the supervisor's output
                               # instead of taking the record's word for what came back.
                               findings_source=str(args.findings),
                               rejected_by=args.rejected_by)
    except (ValueError, TypeError) as exc:
        print(f"review not recorded: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if record.complete:
        print(f"review {record.review_id} of {record.phase}: complete "
              f"({len(record.findings)} finding(s))")
        return 0
    print(f"review {record.review_id} of {record.phase}: NOT complete -- "
          # O228/K3: this said "undecided", which `blocking_findings` has not meant
          # since O229/G -- it counts every finding that is not a finding of nothing,
          # decided or not, and the board published the wrong word into two generated
          # documents.
          f"blocking: {', '.join(record.blocking_findings) or 'none'}; "
          f"refused by validate: "
          f"{', '.join(v.finding_id for v in record.validation_failures) or 'none'}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
