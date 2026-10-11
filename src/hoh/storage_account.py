"""Storage account of a project's registered namespace (I10b slice 1, plan row P5-40).

`take_account(registry, project_id, ...)` answers one question without touching
anything: how many **bytes and entries (inodes)** does the project hold inside
the paths the registry owns, per root, run, session, tier and "reclaimable
now"? The account is over **registry-owned paths only**. Every entry is judged
by `registry.owner_of` (a verdict per path, so the consumer asks for every
descendant); an entry is counted in exactly one bucket:

* `owned` -- `OWNED` and of this project;
* `bookkeeping` -- `PROTECTED` (the registry's own records);
* `unproven` -- anything else, including an `owner_of` that raised. Report
  only: it is never summed into the account.

The account proper is `owned + bookkeeping` (`total`). Only an `OWNED`
directory is entered; a directory that is `UNKNOWN` or `PROTECTED` is one entry
and is not listed, not read, not counted below it. A symlink is one entry of
size 0 and is never followed. Each `(st_dev, st_ino)` is counted once over the
whole account.

A path that cannot be measured reads the string `UNMEASURED`, **never 0**, with
the part that was seen as a lower bound; an aggregate that contains one reads
`UNMEASURED` too. A partial account is a report, never a verdict.

**Every read is bounded** by one time budget: the registry's index reads, the
listing of each root, the walk of each item (the clock is read per entry) and
the labels of each item run through `storage_tiers.bounded`; a call that does
not return is abandoned (its daemon thread only reads) and the account goes
on. Tiers come from `storage_tiers.classify_tier`; this module invents none.

What this is not (named here, not built; "Later slices"):

* No receipt, no file written, no `hoh status` change, no admission feed
  (refusal on entries). It writes nothing, removes nothing, opens no session,
  takes no lease and asks no admission.
* No strand and no role grouping beyond the `purpose` of a tmp workspace
  record; no surface grouping (no record names a strand).
* No `_arenas/*` split (an arena is part of the `_arenas` item) and no
  `target_clone` / `linked_worktree` roots: they are named in
  `not_accounted_roots` and make `scope_complete` false. So is a registered
  root the registry's identity check no longer accepts (a scratch directory,
  a runs base, an external root or a legacy root that is not the recorded
  inode any more), with reason `NOT_INTACT`.
* No fast path: `owner_of` per entry costs about 0.6 ms, so a tree of millions
  of entries reads `UNMEASURED` with a lower bound, honestly.
* A hard-linked regular file is `unproven`, even when every name lies in the
  project's namespace -- the registry's rule, not relaxed here.
* No foreign-consumer listing outside the registered roots. The `unproven`
  samples are the UNKNOWN things **inside** registered roots.
* The walk is path-based: between `owner_of(dir)` and the listing of `dir` a
  same-uid writer inside the namespace could swap a symlink (a time-of-check /
  time-of-use window; metadata only, nothing is read or written). Closing it
  needs a directory-descriptor walk (a later slice).
"""

from __future__ import annotations

import math
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hoh import storage_tiers
from hoh.storage_ephemeral import read_record
from hoh.storage_registry import CREATED, OWNED, PROTECTED, REGISTERED, SCRATCH_KINDS
from hoh.storage_tiers import TargetTimeout, bounded

SCHEMA = "veriharness.storage-account/1"
DEFAULT_BUDGET_S = 120.0
MAX_BUDGET_S = 3600.0
#: How long past the budget a blocked call is waited for before it is abandoned.
HUNG_GRACE_S = 2.0

UNMEASURED = "UNMEASURED"
NOT_RUN = "NOT_RUN"
UNTIERED = "UNTIERED"
NOT_APPLICABLE = "NOT_APPLICABLE"
NOT_INTACT = "NOT_INTACT"

#: Root kinds whose direct children the storage steward looks at
#: (`storage_steward._CANDIDATE_KINDS`, plus the runs bases and legacy roots it
#: also lists); the kinds `receipts` and `tmp` never are.
_CANDIDATE_KINDS = frozenset({"scratch", "reviews", "runs", "mutants", "runs_base",
                              "legacy_review"})
#: External roots of the project that this slice names but does not account.
_NOT_ACCOUNTED_KINDS = ("target_clone", "linked_worktree")
_LABEL_KINDS = frozenset({"tmp", "runs_base", "runs"})
_LABEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MAX_SAMPLES = 10
_REASON_CHARS = 400


# --------------------------------------------------------------------------- #
# the seams a test replaces
# --------------------------------------------------------------------------- #


def _lstat(path: str) -> os.stat_result:
    return os.lstat(path)


def _scandir(path: str):
    return os.scandir(path)


# --------------------------------------------------------------------------- #
# values
# --------------------------------------------------------------------------- #


class _Budget(Exception):
    """The time budget is spent (or the call was abandoned)."""


class _Unreadable(Exception):
    """A directory or entry of an item could not be read."""


class _Tally:
    """items / bytes / allocated bytes / entries of a group of items. A group
    that contains an item that was not measured reads `UNMEASURED` and keeps
    the sum of what was seen as `*_lower_bound`."""

    def __init__(self) -> None:
        self.items = 0
        self.unmeasured_items = 0
        self.bytes = 0
        self.allocated_bytes = 0
        self.entries = 0

    def add(self, figures: tuple[int, int, int], measured: bool) -> None:
        self.items += 1
        if not measured:
            self.unmeasured_items += 1
        self.bytes += figures[0]
        self.allocated_bytes += figures[1]
        self.entries += figures[2]

    def out(self) -> dict[str, Any]:
        if self.unmeasured_items:
            return {"items": self.items, "unmeasured_items": self.unmeasured_items,
                    "bytes": UNMEASURED, "allocated_bytes": UNMEASURED,
                    "entries": UNMEASURED, "bytes_lower_bound": self.bytes,
                    "allocated_bytes_lower_bound": self.allocated_bytes,
                    "entries_lower_bound": self.entries}
        return {"items": self.items, "unmeasured_items": 0, "bytes": self.bytes,
                "allocated_bytes": self.allocated_bytes, "entries": self.entries}


class _Walk:
    """What the walk of one item has seen so far. Written by the walking
    thread only; read by the caller after the thread returned or was
    abandoned."""

    def __init__(self) -> None:
        self.owned = [0, 0, 0]              # bytes, allocated bytes, entries
        self.bookkeeping = [0, 0, 0]
        self.unproven_entries = 0
        self.unproven_bytes = 0
        self.not_descended = 0
        self.other_names = 0
        self.vanished = 0
        self.samples: list[dict[str, str]] = []
        self.local: set[tuple[int, int]] = set()
        self.abandoned = False
        self.outcome = "MEASURED"           # or UNMEASURED
        self.reason: str | None = None

    def account(self) -> tuple[int, int, int]:
        return (self.owned[0] + self.bookkeeping[0], self.owned[1] + self.bookkeeping[1],
                self.owned[2] + self.bookkeeping[2])


class _Item:
    def __init__(self, path: str, kind: str, session_id: str | None, is_dir: bool,
                 name: str | None) -> None:
        self.path = path
        self.kind = kind
        self.session_id = session_id
        self.is_dir = is_dir
        self.name = name                    # None for the pseudo-item of an unlistable root
        self.walk: _Walk | None = None
        self.status = "MEASURED"
        self.reason: str | None = None
        self.run_id: str | None = None
        self.run_id_basis: str | None = None
        self.purpose: str | None = None
        self.workspace_id: str | None = None
        self.label_status = NOT_APPLICABLE
        self.tier: str = NOT_RUN
        self.tier_reason: str | None = None
        self.eligible = False
        self.candidate = False

    @property
    def measured(self) -> bool:
        return self.status == "MEASURED"

    def owned_figures(self) -> tuple[int, int, int]:
        return tuple(self.walk.owned) if self.walk else (0, 0, 0)          # type: ignore[return-value]

    def bookkeeping_figures(self) -> tuple[int, int, int]:
        return tuple(self.walk.bookkeeping) if self.walk else (0, 0, 0)    # type: ignore[return-value]

    def figures(self) -> tuple[int, int, int]:
        return self.walk.account() if self.walk else (0, 0, 0)

    def out(self) -> dict[str, Any]:
        b, a, e = self.figures()
        item: dict[str, Any] = {
            "path": self.path, "root_kind": self.kind, "session_id": self.session_id,
            "run_id": self.run_id, "run_id_basis": self.run_id_basis,
            "purpose": self.purpose, "workspace_id": self.workspace_id,
            "label_status": self.label_status, "status": self.status,
            "reason": self.reason, "tier": self.tier, "tier_reason": self.tier_reason,
            "eligible": self.eligible, "candidate": self.candidate,
        }
        if self.measured:
            walk = self.walk
            item.update({"bytes": b, "allocated_bytes": a, "entries": e})
            if walk is not None:
                item["unproven"] = {
                    "entries": walk.unproven_entries, "bytes": walk.unproven_bytes,
                    "not_descended": walk.not_descended,
                    "other_names_of_counted_inodes": walk.other_names,
                    "samples": list(walk.samples)}
                item["vanished"] = walk.vanished
        else:
            item.update({"bytes": UNMEASURED, "allocated_bytes": UNMEASURED,
                         "entries": UNMEASURED,
                         "lower_bound": {"bytes": b, "allocated_bytes": a, "entries": e}})
        return item


def _check_budget(budget_s: Any) -> float:
    finite = not isinstance(budget_s, bool) and isinstance(budget_s, (int, float))
    if finite:
        try:
            finite = math.isfinite(budget_s)
        except OverflowError:                          # an int too large for a float
            finite = False
    if not finite or not 0 < budget_s <= MAX_BUDGET_S:
        try:
            shown = _clip(repr(budget_s))
        except ValueError:                             # an int with too many digits to print
            shown = f"<a {type(budget_s).__name__} too large to show>"
        raise ValueError(f"budget_s must be a finite number of seconds in (0, "
                         f"{MAX_BUDGET_S:g}], not {shown}")
    return float(budget_s)


def _clip(text: str) -> str:
    return text if len(text) <= _REASON_CHARS else text[:_REASON_CHARS] + "..."


def _label(value: Any) -> str | None:
    return value if isinstance(value, str) and _LABEL.match(value) else None


# --------------------------------------------------------------------------- #
# the account
# --------------------------------------------------------------------------- #


class _Taker:
    def __init__(self, registry, project_id: str, budget: float, tiers: bool, sources,
                 tier_of, clock: Callable[[], float]) -> None:
        self.registry = registry
        self.project_id = project_id
        self.budget = budget
        self.tiers = tiers
        self.sources = sources
        self.tier_of = tier_of
        self.clock = clock
        self.started = clock()
        self.deadline = self.started + budget
        self.seen: set[tuple[int, int]] = set()
        self.runs_bases: list[str] = []
        self.run_cache: dict[str, bool] = {}

    def left(self) -> float:
        return self.deadline - self.clock()

    # -- the roots ---------------------------------------------------------- #

    def _enumerate(self):
        roots: list[tuple[str, str | None, str]] = []
        for kind, session, directory in self.sources.scratch_dirs():
            roots.append((kind, session, str(directory)))
        for directory in self.sources.runs_bases():
            roots.append(("runs_base", None, str(directory)))
        for directory in self.sources.legacy_roots():
            roots.append(("legacy_review", None, str(directory)))
        named = []
        for kind in _NOT_ACCOUNTED_KINDS:
            for entry in self.registry._accepted_roots(self.project_id, kind):
                named.append({"kind": kind, "path": str(entry.get("path")),
                              "reason": "LATER_SLICE"})
        named.extend(self._not_intact())
        return roots, named

    def _not_intact(self) -> list[dict[str, str]]:
        """The registered roots of the project that the registry's identity check does
        not accept (the directory is not the recorded inode, or is gone): they are not
        accounted, and are named, never skipped silently."""
        registry, project_id = self.registry, self.project_id
        out: list[dict[str, str]] = []
        for entry in registry._scratch_entries(project_id):
            kind, session, rel = entry.get("kind"), entry.get("session_id"), entry.get("path")
            if kind not in SCRATCH_KINDS or not isinstance(session, str) \
                    or not isinstance(rel, str):
                continue
            if registry._scratch_entry(project_id, kind, session) is None:
                out.append({"kind": kind, "path": str(registry.state_root / rel),
                            "reason": NOT_INTACT})
        for kind in ("runs_base",) + _NOT_ACCOUNTED_KINDS:
            accepted = {e.get("path") for e in registry._accepted_roots(project_id, kind)}
            for entry in registry._root_entries(project_id):
                if (entry.get("kind") == kind and entry.get("project_id") == project_id
                        and entry.get("state") in (CREATED, REGISTERED)
                        and entry.get("path") not in accepted):
                    out.append({"kind": kind, "path": str(entry.get("path")),
                                "reason": NOT_INTACT})
        intact = {e.get("path") for e in registry.legacy_roots(project_id)}
        for entry in registry.legacy_roots(project_id, intact_only=False):
            if entry.get("path") not in intact:
                out.append({"kind": str(entry.get("kind")), "path": str(entry.get("path")),
                            "reason": NOT_INTACT})
        return out

    def roots(self, problems: list[str]):
        if self.left() <= 0:
            problems.append("BUDGET: the budget was spent before the roots were listed")
            return [], []
        try:
            roots, named = bounded(self._enumerate, self.left() + HUNG_GRACE_S,
                                   "storage-account-roots")
        except Exception as exc:                       # noqa: BLE001 - stated, not hidden
            problems.append(f"the roots could not be listed: {type(exc).__name__}: {exc}")
            return [], []
        unique = {(kind, session or "", path): (kind, session, path)
                  for kind, session, path in roots}
        ordered = [unique[key] for key in sorted(unique)]
        named.sort(key=lambda e: (e["kind"], e["path"]))
        return ordered, named

    def items(self, roots) -> list[_Item]:
        """Lists each root in turn and measures and labels its items before the next root
        is listed, so a root that hangs or is spent costs only itself and what follows."""
        self.runs_bases = [path for kind, _, path in roots if kind == "runs_base"]
        items: list[_Item] = []
        for kind, session, path in roots:
            for item in self._list_root(kind, session, path):
                items.append(item)
                self.measure(item)
                self.label(item)
        return items

    def _list_root(self, kind: str, session: str | None, path: str) -> list[_Item]:
        if self.left() <= 0:
            return [self._pseudo(kind, session, path, "BUDGET")]

        def listing():
            found = []
            with _scandir(path) as entries:
                for entry in entries:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    found.append((entry.name, is_dir))
            return sorted(found)

        try:
            names = bounded(listing, self.left() + HUNG_GRACE_S, "storage-account-listing")
        except TargetTimeout:
            return [self._pseudo(kind, session, path, "BUDGET")]
        except Exception:                              # noqa: BLE001 - UNREADABLE, not empty
            return [self._pseudo(kind, session, path, "UNREADABLE")]
        return [_Item(os.path.join(path, name), kind, session, is_dir, name)
                for name, is_dir in names]

    @staticmethod
    def _pseudo(kind: str, session: str | None, path: str, reason: str) -> _Item:
        item = _Item(path, kind, session, True, None)
        item.status, item.reason = "UNMEASURED", reason
        return item

    # -- the walk ------------------------------------------------------------ #

    def measure(self, item: _Item) -> None:
        if item.name is None:
            return
        walk = _Walk()
        item.walk = walk
        if self.left() <= 0:
            item.status, item.reason = "UNMEASURED", "BUDGET"
            return
        try:
            bounded(lambda: self._walk(item, walk), self.left() + HUNG_GRACE_S,
                    "storage-account-walk")
        except TargetTimeout:
            walk.abandoned = True
            item.status, item.reason = "UNMEASURED", "BUDGET"
            return
        item.status, item.reason = walk.outcome, walk.reason
        self.seen |= walk.local

    def _walk(self, item: _Item, walk: _Walk) -> None:
        registry = self.registry
        project_id = self.project_id
        seen, clock, deadline = self.seen, self.clock, self.deadline
        base = item.path

        def tick() -> None:
            if walk.abandoned or clock() >= deadline:
                raise _Budget()

        def process(full: str, rel: str) -> bool:
            """Counts one entry; True when the entry is an OWNED directory to enter."""
            try:
                st = _lstat(full)
            except FileNotFoundError:
                walk.vanished += 1
                return False
            except OSError as exc:
                raise _Unreadable(f"{rel}: {exc}") from exc
            is_dir = stat.S_ISDIR(st.st_mode)
            try:
                owner = registry.owner_of(full)
                status, reason, owner_project = owner.status, owner.reason, owner.project_id
            except Exception as exc:                   # noqa: BLE001 - fails closed
                status, reason, owner_project = (
                    "UNKNOWN", f"owner_of raised {type(exc).__name__}: {exc}", None)
            if walk.abandoned:                         # the account gave up on this walker
                raise _Budget()
            if status == OWNED and owner_project == project_id:
                bucket = "owned"
            elif status == PROTECTED:
                bucket = "bookkeeping"
            else:
                bucket = "unproven"
            key = (st.st_dev, st.st_ino)
            if key in seen or key in walk.local:
                if bucket == "unproven":
                    walk.other_names += 1
                return False
            walk.local.add(key)
            size = st.st_size if stat.S_ISREG(st.st_mode) else 0
            allocated = st.st_blocks * 512
            if bucket == "unproven":
                walk.unproven_entries += 1
                walk.unproven_bytes += size
                if len(walk.samples) < _MAX_SAMPLES:
                    text = reason or "no reason given"
                    if not is_dir and st.st_nlink > 1:
                        text += "; HARDLINKED"
                    walk.samples.append({"rel": rel, "status": str(status),
                                         "reason": _clip(text)})
            else:
                figures = walk.owned if bucket == "owned" else walk.bookkeeping
                figures[0] += size
                figures[1] += allocated
                figures[2] += 1
            enter = is_dir and bucket == "owned"
            if is_dir and not enter:
                walk.not_descended += 1
            return enter

        try:
            tick()
            stack: list[tuple[str, str]] = []
            if process(base, "."):
                stack.append((base, ""))
            while stack:
                directory, rel = stack.pop()
                tick()
                try:
                    with _scandir(directory) as entries:
                        names = []
                        for entry in entries:
                            tick()
                            names.append(entry.name)
                except FileNotFoundError:
                    walk.vanished += 1
                    continue
                except OSError as exc:
                    raise _Unreadable(f"{rel or '.'}: {exc}") from exc
                enter: list[tuple[str, str]] = []
                for name in sorted(names):
                    tick()
                    child_rel = f"{rel}/{name}" if rel else name
                    child = os.path.join(directory, name)
                    if process(child, child_rel):
                        enter.append((child, child_rel))
                stack.extend(reversed(enter))
        except _Budget:
            walk.outcome, walk.reason = "UNMEASURED", "BUDGET"
        except _Unreadable:
            walk.outcome, walk.reason = "UNMEASURED", "UNREADABLE"
        except Exception:                              # noqa: BLE001 - an item error, not a crash
            walk.outcome, walk.reason = "UNMEASURED", "ERROR"

    # -- the labels ---------------------------------------------------------- #

    def _is_run(self, directory: Path) -> bool:
        """A run the registry's parser accepts **and** the registry owns for this
        project (a run on another mount, or of another project, is no run here).
        The clock is read before every check."""
        if self.clock() >= self.deadline:
            raise _Budget()
        if not self.registry._valid_run_dir(directory):
            return False
        try:
            owner = self.registry.owner_of(directory)
        except Exception:                              # noqa: BLE001 - fails closed
            return False
        return owner.status == OWNED and owner.project_id == self.project_id

    def _run_known(self, run_id: str) -> bool:
        if run_id not in self.run_cache:
            known = False
            for base in self.runs_bases:
                if self._is_run(Path(base) / run_id):
                    known = True
                    break
            self.run_cache[run_id] = known
        return self.run_cache[run_id]

    def _read_labels(self, item: _Item) -> dict[str, str | None]:
        out: dict[str, str | None] = {"run_id": None, "run_id_basis": None,
                                      "purpose": None, "workspace_id": None}
        if item.kind == "tmp":
            record = read_record(item.path)
            if isinstance(record, dict):
                out["purpose"] = _label(record.get("purpose"))
                out["workspace_id"] = _label(record.get("workspace_id"))
                run_id = _label(record.get("run_id"))
                if run_id is not None:
                    out["run_id"] = run_id
                    out["run_id_basis"] = "VALIDATED" if self._run_known(run_id) else "CLAIMED"
        elif item.is_dir and _label(item.name) is not None:
            if self._is_run(Path(item.path)):
                out["run_id"], out["run_id_basis"] = item.name, "VALIDATED"
        return out

    def label(self, item: _Item) -> None:
        if item.kind not in _LABEL_KINDS or item.name is None:
            return
        if self.left() <= 0:
            item.label_status = "NOT_REACHED"
            return
        try:
            labels = bounded(lambda: self._read_labels(item), self.left() + HUNG_GRACE_S,
                             "storage-account-labels")
        except _Budget:
            item.label_status = "NOT_REACHED"
            return
        except TargetTimeout:
            item.label_status = "TIMEOUT"
            return
        except Exception:                              # noqa: BLE001 - stated as ERROR
            item.label_status = "ERROR"
            return
        item.label_status = "OK"
        item.run_id, item.run_id_basis = labels["run_id"], labels["run_id_basis"]
        item.purpose, item.workspace_id = labels["purpose"], labels["workspace_id"]

    # -- the tiers ------------------------------------------------------------ #

    def classify(self, item: _Item) -> None:
        name = item.name
        item.candidate = bool(name is not None and item.is_dir and item.kind in _CANDIDATE_KINDS
                              and not name.startswith(".") and ".vh-quarantine." not in name)
        if not self.tiers:
            item.tier = NOT_RUN
            return
        if name is None:
            item.tier, item.tier_reason = UNMEASURED, f"the root was not listed: {item.reason}"
            return
        remaining = self.left()
        if remaining <= 0:
            item.tier, item.tier_reason = UNMEASURED, "BUDGET: the budget was spent"
            return
        try:
            decision = bounded(lambda: self.tier_of(item.path), remaining + HUNG_GRACE_S,
                               "storage-account-tier")
        except TargetTimeout as exc:
            item.tier, item.tier_reason = UNMEASURED, _clip(f"TIMEOUT: {exc}")
            return
        except Exception as exc:                       # noqa: BLE001 - stated as UNMEASURED
            item.tier, item.tier_reason = UNMEASURED, _clip(
                f"ERROR: {type(exc).__name__}: {exc}")
            return
        reasons = "; ".join(str(r) for r in (getattr(decision, "reasons", ()) or ()))
        if decision.tier is None:
            item.tier = UNTIERED
            item.tier_reason = _clip(f"{decision.refused}: {reasons}" if reasons
                                     else str(decision.refused))
        else:
            item.tier = str(decision.tier)
            item.tier_reason = _clip(reasons) if reasons else None
        item.eligible = bool(decision.eligible)


# --------------------------------------------------------------------------- #
# the entry point
# --------------------------------------------------------------------------- #


def take_account(registry, project_id: str, *, budget_s: float = DEFAULT_BUDGET_S,
                 tiers: bool = True, sources=None, tier_of: Callable[[str], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """The storage account of `project_id` as a JSON-ready dict (schema
    `veriharness.storage-account/1`). Read only: nothing is written or
    removed, no session is opened, no lease taken, no admission asked.
    `ValueError` for a budget that is not a finite number in (0, 3600]
    (a bool, a string or an int too large for a float is refused) and for a
    project the registry does not know. The read of the project's record runs
    inside the budget: one that does not return gives an account with no
    items and a `BUDGET` problem."""
    budget = _check_budget(budget_s)
    if sources is None:
        sources = storage_tiers.EvidenceSources(registry, project_id)
    if tier_of is None:
        def tier_of(path: str, _sources=sources):
            return storage_tiers.classify_tier(path, registry=registry, sources=_sources)
    taker = _Taker(registry, project_id, budget, tiers, sources, tier_of, clock)

    problems: list[str] = []
    roots: list = []
    named: list[dict[str, str]] = []
    items: list[_Item] = []
    try:
        record = bounded(lambda: registry._project_record(project_id),
                         budget + HUNG_GRACE_S, "storage-account-project")
    except TargetTimeout as exc:
        problems.append(_clip(f"BUDGET: the project record was not read: {exc}"))
    else:
        if record is None:
            raise ValueError(f"project {project_id!r} is not registered")
        roots, named = taker.roots(problems)
        items = taker.items(roots)
        for item in items:
            taker.classify(item)

    total, owned, bookkeeping = _Tally(), _Tally(), _Tally()
    by_tier: dict[str, _Tally] = {}
    by_kind: dict[str, _Tally] = {}
    by_session: dict[str, _Tally] = {}
    by_run: dict[str, _Tally] = {}
    by_purpose: dict[str, _Tally] = {}
    reclaimable = _Tally()
    seen_unproven = {"entries": 0, "bytes": 0, "not_descended": 0,
                     "other_names_of_counted_inodes": 0, "items_with_unproven": 0}
    tier_unmeasured = 0
    labels_missing = 0
    for item in items:
        measured = item.measured
        total.add(item.figures(), measured)
        owned.add(item.owned_figures(), measured)
        bookkeeping.add(item.bookkeeping_figures(), measured)
        if item.run_id is None:
            run_key = "-"
        elif item.run_id_basis == "VALIDATED":
            run_key = item.run_id
        else:
            run_key = f"claimed:{item.run_id}"
        for groups, key in ((by_tier, item.tier), (by_kind, item.kind),
                            (by_session, item.session_id or "-"), (by_run, run_key),
                            (by_purpose, item.purpose or "-")):
            groups.setdefault(key, _Tally()).add(item.figures(), measured)
        if item.eligible and item.candidate:
            reclaimable.add(item.figures(), measured)
        if tiers and item.tier == UNMEASURED:
            tier_unmeasured += 1
        if item.label_status in ("NOT_REACHED", "TIMEOUT", "ERROR"):
            labels_missing += 1
        if item.walk is not None:
            if item.walk.unproven_entries + item.walk.other_names:
                seen_unproven["items_with_unproven"] += 1
            seen_unproven["entries"] += item.walk.unproven_entries
            seen_unproven["bytes"] += item.walk.unproven_bytes
            seen_unproven["not_descended"] += item.walk.not_descended
            seen_unproven["other_names_of_counted_inodes"] += item.walk.other_names
    unproven: dict[str, Any] = {}
    unproven_unmeasured = total.unmeasured_items
    if unproven_unmeasured:                  # the headline rule: UNMEASURED, never 0
        for figure, value in seen_unproven.items():
            unproven[figure] = UNMEASURED
            unproven[f"{figure}_lower_bound"] = value
    else:
        unproven.update(seen_unproven)
    unproven["unmeasured_items"] = unproven_unmeasured
    unproven["lower_bound"] = unproven_unmeasured > 0

    if tiers:
        reclaimable_out = reclaimable.out()
        reclaimable_out["basis"] = "GROSS"
        reclaimable_out["tier_unmeasured_items"] = tier_unmeasured
        reclaimable_out["lower_bound"] = bool(reclaimable.unmeasured_items or tier_unmeasured)
    else:
        reclaimable_out = {"basis": "GROSS", "items": NOT_RUN, "unmeasured_items": NOT_RUN,
                           "bytes": NOT_RUN, "allocated_bytes": NOT_RUN, "entries": NOT_RUN}

    complete = (not problems and not total.unmeasured_items and not tier_unmeasured
                and not labels_missing)
    scope_complete = not problems and not named
    return {
        "schema": SCHEMA,
        "project_id": project_id,
        "state_root": str(registry.state_root),
        "taken_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "budget_s": budget,
        "elapsed_s": round(clock() - taker.started, 3),
        "complete": complete,
        "scope_complete": scope_complete,
        "problems": problems,
        "tiers": {"run": bool(tiers),
                  "classified": sum(1 for i in items if i.tier not in (NOT_RUN, UNMEASURED)),
                  "unmeasured": tier_unmeasured},
        "total": total.out(),
        "owned": owned.out(),
        "bookkeeping": bookkeeping.out(),
        "unproven": unproven,
        "by_tier": {k: v.out() for k, v in sorted(by_tier.items())},
        "reclaimable_now": reclaimable_out,
        "by_kind": {k: v.out() for k, v in sorted(by_kind.items())},
        "by_session": {k: v.out() for k, v in sorted(by_session.items())},
        "by_run": {k: v.out() for k, v in sorted(by_run.items())},
        "by_purpose": {k: v.out() for k, v in sorted(by_purpose.items())},
        "not_accounted_roots": named,
        "items": [item.out() for item in items],
    }
