"""VERIFIED_RELOCATION: the one operation that may remove a local copy (STORAGE-D, P5-29).

The operator's directive (A1, sections F, G, J, K) authorises automatic local
reclamation for exactly two classes -- `VERIFIED_DUPLICATE` and
`REPRODUCIBLE_SCRATCH` (`hoh.storage_tiers`) -- and only after the
destination bundle has passed full verification and its receipt is durable.
This module is that operation, and **nothing else in VeriHarness removes a
file or a directory** (``test_removal_only_in_execute`` scans for it).

`VerifiedRelocation.plan(project_id, candidates)` re-classifies every
candidate now and keeps only eligible items of that project: no item of
another project, none nested in another, none that is or holds a pinned
original -- one a plan or a receipt relies on, one a committed retention
manifest lists ("a listed original is pinned", Revision 4 item 2) or a tracked
file of the canonical repository -- and none whose original lies inside another
planned item. The plan is frozen to ``<receipts_dir>/plans/plan-<UTC>-<hex>.json``
(its body digested inside it) and its file digest is appended, together with
the originals it pins, to ``<receipts_dir>/plans.jsonl`` -- durably, before
anything is removed.

`VerifiedRelocation.execute(plan_path)`:

1. refuses (``REFUSED``) a plan whose file digest is not the one recorded in
   the index or whose body digest does not recompute;
2. opens the project's **fixed** registry session ``relocation``
   (`StorageCoordinator.acquire_session`; a live other holder is refused by
   the registry: ``BUSY``), takes an exclusive ``flock`` on
   ``registry.scratch_dir(p, "relocation", "scratch") / "relocation.lock"``
   (``EWOULDBLOCK``, held by someone else: ``BUSY``) and runs the rest as the
   project's driver through `StorageCoordinator.coordinate` (a driver lease
   live elsewhere: ``BUSY``, nothing done). ``BUSY`` means only a **proven
   live other holder** (Revision 11 item 1): a refusal whose explicit
   ``reason`` is ``LIVE_HOLDER`` (registry or coordinator) or the flock's
   ``EWOULDBLOCK``. Every other refusal before the work -- an ``OSError``
   such as ENOSPC writing ``LEASE.json``, ``DRIVER.json`` or on the
   coordinator's ``.lock``, a malformed driver record, a foreign coordinator
   marker, a holder of unknown liveness, an unreadable lease, any other
   flock error, an error of ``scratch_dir`` or opening ``relocation.lock`` --
   is ``FAILED`` with the real error text, nothing done. The flock is held
   for the whole call, so
   a second execute cannot start even when the driver lease is lost mid-call
   (the coordinator's lease is session ownership, not a per-call mutex; two
   executes inside the holding process are stopped by the same flock);
3. refuses the whole plan (``REFUSED``) when a retention-manifest entry an
   item relies on changed, or when any item's class, resolved path or proof
   digest differs from a fresh `classify_tier` -- a changed plan is never
   broadened;
4. per item, immediately before acting, failing closed (the item is kept):
   identity and ownership unchanged; not a pinned original; the retain ref
   ``refs/vh-retain/<receipt-id>`` created in the canonical repository
   **before** anything is removed; staging reserved through `StorageAdmission`
   (``archive_staging``) or, when refused or in CRITICAL, on the target under
   ``<target>/staging/<p>/``, none if the target check fails -- wherever it
   is built, the staged bundle stays and is reported ``STAGING_LEFTOVER``
   (``where`` guest or target, path, bytes); a bundle built by
   `archive_bundle` with the registry's ownership of the resolved item --
   for a duplicate, the item is first re-measured against the plan and the
   built bundle's content digest must equal the planned ``content_digest``
   (``ITEM_CHANGED`` otherwise: a bundle of changed bytes is never placed as
   ``unique: false``); placed into the target's
   ``reclaim-review/<p>/`` with an eligibility only `classify_tier` produced,
   accepted only as ``ARCHIVED_VERIFIED``; the proof re-checked (duplicates
   against the current original, reproductions against the current refs and
   the retain ref, retention entries re-read; a duplicate's own entries
   again equal to the plan and byte-equal to the current originals) while
   the original's run is held by a shared lock on its ``.process.lock``;
   the extract roots C2's placement left in the guest recorded (they await
   their committed receipt); no reference;
   a durable **intent** appended to ``intents.jsonl`` beside
   ``relocation.lock`` (fsync of the file and its directory) -- written
   before the rename, or nothing is renamed; renamed to
   ``<name>.vh-quarantine.<UTC>`` and walked **once** (`storage_tiers.inventory`,
   which records each entry's ``st_dev``/``st_ino`` beside the entries): its
   tree digest must equal the bundle's ``content_digest`` (archived items)
   and its ``entries_digest`` the plan item's (``ENTRIES_MISMATCH``
   otherwise), and every entry path passes `_safe_entry_path`
   (``UNSAFE_ENTRY``); then no process of the same uid with a cwd or
   descriptor inside and no live lease of its session; a duplicate's
   entries re-read against the current originals once more; the
   ``st_dev``/``st_ino`` of the registry's resolved path re-checked on an own
   descriptor; a ``removal_started`` intent record appended (or nothing is
   removed); **only the entries of that one walk** unlinked, each directory
   ``fstat``-ed after it is opened and compared with the walk's
   ``st_dev``/``st_ino`` (``ENTRY_SWAPPED``); each regular file, right before
   its unlink, opened ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK`` relative to its
   directory's descriptor, the descriptor ``fstat``-ed against the walk's
   ``st_dev``/``st_ino``, its content hashed against the inventory's SHA-256
   (``ITEM_CHANGED``: bytes written in place after the compare), then
   ``fstatat(..., AT_SYMLINK_NOFOLLOW)`` compared with the descriptor's
   inode, then ``unlinkat`` (Revision 9 item 3); each symlink
   ``fstatat``-ed against the walk; the directories removed with ``rmdir``;
   absence of the item path and of the quarantine name checked. No manifest,
   staged bundle or plan file is read at removal time, and no second walk
   feeds the removal. Accepted residual risk (Revision 10 item 8): between
   the end of a regular file's re-hash and its ``unlinkat``, a same-uid
   process can still replace the name (after the final ``fstatat`` compare)
   or write into the file in place (after its last read). Such a process
   opened the file after the quarantine's fd/cwd probe found none. The
   window is a few system calls per file, inside the owned namespace. Bytes
   written in it are removed with the file, and they are not in the bundle.
5. The restore rule: a removal stopped before anything was removed restores
   the original name with ``renameat2(RENAME_NOREPLACE)`` -- or a fresh
   versioned name when the old name was taken meanwhile; never a replacing
   rename -- and the item is ``KEPT`` with the reason. A removal that stops
   after anything was removed keeps the quarantine name and is recorded
   ``PARTIAL`` (NEEDS_YOU; recovery is re-extraction from the verified
   bundle). A late entry (``rmdir`` finds a directory not empty) is restored
   to the original name with what remains (``LATE_ENTRY_RESTORED``,
   NEEDS_YOU) only when every walked non-directory entry is gone and every
   walked directory still present holds, at any depth, only entries the
   inventory did not contain (and walked directories leading to them) -- the
   remainder then holds no archived file content (Revision 9 item 4); any
   other remainder keeps the quarantine name (``PARTIAL``).
   An item that lies inside a registered archive target's root, or holds
   one, is refused (``REFUSED`` ``INSIDE_ARCHIVE_TARGET``) by `plan` and
   again immediately before the quarantine rename, decided by
   ``(st_dev, st_ino)`` ancestry up to ``/``; a target root that cannot be
   measured in time refuses the item (``TARGET_UNMEASURABLE``), one that
   does not exist refuses nothing (Revision 9 item 5).
6. One ReclaimReceipt per item, ``<receipts_dir>/reclaim-<receipt-id>.json``,
   followed by a completion record in ``intents.jsonl``. The journal stays
   where Revision 8 item 4 puts it, append-only beside ``relocation.lock``:
   completed intents are neither moved nor removed, and `recover` reads only
   that file.

A build that fails, times out (on the target it is cancelled through
`archive_bundle`'s ``cancel`` and waited for once more; one still running is
``STILL_WRITING``) or is abandoned leaves its staged copy where it was built:
it is measured and reported ``STAGING_LEFTOVER`` (``where``, ``path``,
``bytes``), never removed. While a target-side leftover of an item is
present -- or its absence cannot be measured -- the item is not built on that
target again (``LEFTOVER_PRESENT``). Leftovers are followed by their label
(Revision 9 item 1): every candidate build is labelled with its receipt id,
and on every pass, before the item's staging is chosen and whatever it is,
``<target.root>/staging/<p>/`` of each registered target is listed (under the
timeout) for names ending in ``-<receipt-id>`` or ``-<receipt-id>.building``
of every earlier receipt of the item path, of any status. Each match is
reported in the receipt's ``target_leftovers`` (``target_id``,
``STAGING_LEFTOVER``, current path, bytes); a match or an unmeasurable
listing keeps the item from being built on that target in this pass (guest
staging and other targets stay possible).

Every record is durable and whole (Revision 10 items 1 and 2): before a
build starts, a **build record** (``build_started``, item, receipt id,
target, ``where``, ``out_parent``) is appended to ``intents.jsonl`` -- no
record, no build (``INTENT_FAILED``) -- and leftovers are followed by the
labels of every earlier receipt **and** every build record of the item path.
Plans, ReclaimReceipts and recovered receipts are published by `write_new`
(a dot-named temporary file written whole, fsynced, published with
``renameat2(RENAME_NOREPLACE)``; one that could not be published stays and is
listed ``STRANDED_TEMP`` by `unreadable_receipts`, beside every ``UNREADABLE``
``reclaim-*.json``). Journal appends are written whole and never glued to a
torn last line. A receipt that cannot be written is reported
(``receipt_not_written``, NEEDS_YOU) and its intent left open for `recover`.
`execute` is ``BUSY`` only when a proven live other holder refuses it before
any work (the relocation session or the coordinator with reason
``LIVE_HOLDER``, the flock with ``EWOULDBLOCK``); any other refusal before
the work is ``FAILED`` with the real error, and an `Exception` from the work
itself is ``FAILED`` with every item finished before it. `recover` is
classified the same way (``BUSY`` only for a live other holder, otherwise
``FAILED`` and NEEDS_YOU).

Below a target root everything is read by the **staging walk** (Revision 10,
10a item 2): the root opened once, every component below it examined with
``fstatat(AT_SYMLINK_NOFOLLOW)`` and only directories and regular files
opened, ``O_NOFOLLOW`` from the previous descriptor -- nothing below the
root is resolved, stat-ed or opened by path. `target_leftovers` is the
pass-level listing of ``<root>/staging/<p>/`` (found, unmeasured, unknown);
recovery reads a bundle receipt through it; an extract root's containing
target is settled, its identity probed through the walk's root descriptor
and its payload re-hashed through the walk before its intent is written
(``NO_CONTAINING_TARGET``, ``TARGET_UNMEASURABLE``, ``TARGET_UNCONFIRMED``,
``EXTRACT_PROOF_CHANGED``, ``TARGET_TIMEOUT``). While ``archive-targets.json``
has problems (`target_problems`) every item is refused and nothing is read
on any target (``TARGET_PROBLEMS``).

`VerifiedRelocation.recover(project_id)` resolves, under the same session
and lock and before a steward pass executes, every intent an interrupted
execute left without a completion -- renaming and removing nothing. An
intent whose ``receipt_id`` is not of the form this module generates is
``UNRESOLVED`` before any path is built from it; the bundle receipt is read
only inside a registered target (Revision 9 item 6): under the intent's
``target_id``, or -- ``target_id`` None, an extract root -- under its
``containing_target_id`` (intents written before that field: under exactly
one containing target), opened ``O_NOFOLLOW``. Then: item
path and quarantine name both absent and the bundle receipt still present
with its digest on a confirmed target -> ``RELOCATED`` (``recovered``);
both absent otherwise -> ``UNRESOLVED`` (NEEDS_YOU, judged again next pass);
the item path present, the quarantine name absent and no
``removal_started`` -> ``KEPT`` ``INTERRUPTED``; a ``removal_started`` record
or a present quarantine name -> ``PARTIAL`` (NEEDS_YOU). A write that fails
while one intent is resolved (the receipt, its directory, the completion)
marks that intent ``RECOVERY_FAILED`` (NEEDS_YOU, judged again next pass);
the other intents of the pass are still judged (Revision 11 item 4).

Unique, canonical, current or referenced material is never removed, whatever
a caller passes: there is no override parameter. Every call that touches an
archive target runs under a timeout (``timeout``, default 300 s); a target
that hangs keeps the item and is reported.

`VerifiedRelocation.user_reclaimed(target, project_id)` records a bundle that
has disappeared from ``reclaim-review`` as ``USER_RECLAIMED_ARCHIVE`` (never
as corruption) when its classification was eligible; if its item has
meanwhile become referenced it is ``PROMOTION_REQUIRED`` and the item is
classified `HOT`.
"""

from __future__ import annotations

import datetime
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from hoh import archive_bundle as ab
from hoh import archive_target as at
from hoh import storage_tiers as st
from hoh.storage_registry import (LEGACY_KINDS, LIVE_HOLDER, OWNED, SCRATCH_KINDS,
                                  StorageOwnershipError)

RELOCATION_SESSION = "relocation"
#: A legacy item kept because its root became HOT after the plan (STORAGE-L).
LEGACY_HOT = "LEGACY_HOT"
LOCK_NAME = "relocation.lock"
PLAN_SCHEMA = "veriharness.relocation-plan/1"
RECEIPT_SCHEMA = "veriharness.reclaim-receipt/1"
INDEX_NAME = "plans.jsonl"
USER_RECLAIMED_LOG = "user-reclaimed.jsonl"
QUARANTINE_MARK = ".vh-quarantine."

# Execute outcomes.
DONE = "DONE"
BUSY = "BUSY"
REFUSED = "REFUSED"
#: An `Exception` left the work itself (`_execute_locked`) after it began --
#: the report keeps every item finished before it (Revision 10 item 2) -- or
#: something other than a proven live other holder refused the execute (or
#: the recovery) before any work (Revision 11 item 1).
FAILED = "FAILED"
#: Resolving one open intent failed on a write; the other intents of the
#: pass are still judged (Revision 11 item 4).
RECOVERY_FAILED = "RECOVERY_FAILED"

# Item outcomes.
RELOCATED = "RELOCATED"
KEPT = "KEPT"
PARTIAL = "PARTIAL"
LATE_ENTRY_RESTORED = "LATE_ENTRY_RESTORED"

# Reclaim-review bookkeeping.
USER_RECLAIMED_ARCHIVE = "USER_RECLAIMED_ARCHIVE"
PROMOTION_REQUIRED = "PROMOTION_REQUIRED"
TARGET_UNCONFIRMED = "TARGET_UNCONFIRMED"

STAGING_LEFTOVER = "STAGING_LEFTOVER"
STILL_WRITING = "STILL_WRITING"
NEEDS_YOU = "NEEDS_YOU"

# Durable intents (Revision 8 item 4).
INTENT_SCHEMA = "veriharness.relocation-intent/1"
INTENTS_NAME = "intents.jsonl"
UNRESOLVED = "UNRESOLVED"
INTERRUPTED = "INTERRUPTED"

#: Reasons an item is kept (the receipt's ``reason`` starts with one of them).
CHANGED_PLAN = "CHANGED_PLAN"
PLAN_DIGEST = "PLAN_DIGEST"
RETENTION_MANIFEST_CHANGED = "RETENTION_MANIFEST_CHANGED"
OWNERSHIP_CHANGED = "OWNERSHIP_CHANGED"
PINNED_ORIGINAL = "PINNED_ORIGINAL"
RETAIN_REF_FAILED = "RETAIN_REF_FAILED"
NO_TARGET = "NO_TARGET"
NO_STAGING = "NO_STAGING"
TARGET_TIMEOUT = "TARGET_TIMEOUT"
BUILD_FAILED = "BUILD_FAILED"
DESTINATION_UNVERIFIED = "DESTINATION_UNVERIFIED"
DUPLICATE_CHANGED = "DUPLICATE_CHANGED"
REPRODUCTION_REF_MOVED = "REPRODUCTION_REF_MOVED"
REPRODUCTION_CHANGED = "REPRODUCTION_CHANGED"
EXTRACT_PROOF_CHANGED = "EXTRACT_PROOF_CHANGED"
RUN_PROCESS_LIVE = "RUN_PROCESS_LIVE"
REFERENCED = "REFERENCED"
LEASE = "LEASE"
QUARANTINE_FAILED = "QUARANTINE_FAILED"
QUARANTINE_MISMATCH = "QUARANTINE_MISMATCH"
OPEN_FD = "OPEN_FD"
CWD_INSIDE = "CWD_INSIDE"
INODE_CHANGED = "INODE_CHANGED"
ITEM_CHANGED = "ITEM_CHANGED"
ENTRIES_MISMATCH = "ENTRIES_MISMATCH"
UNSAFE_ENTRY = "UNSAFE_ENTRY"
ENTRY_SWAPPED = "ENTRY_SWAPPED"
INTENT_FAILED = "INTENT_FAILED"
LEFTOVER_PRESENT = "LEFTOVER_PRESENT"
INSIDE_ARCHIVE_TARGET = "INSIDE_ARCHIVE_TARGET"
TARGET_UNMEASURABLE = "TARGET_UNMEASURABLE"
NO_CONTAINING_TARGET = "NO_CONTAINING_TARGET"
#: ``archive-targets.json`` could not be read whole (Revision 10 item 6):
#: nothing is relocated, recovered or judged on any target.
TARGET_PROBLEMS = "TARGET_PROBLEMS"

# Pass-level listings (Revision 10 items 2 and 3).
UNKNOWN_STAGING_ENTRY = "UNKNOWN_STAGING_ENTRY"
UNREADABLE = "UNREADABLE"
STRANDED_TEMP = "STRANDED_TEMP"

#: The temporary name `write_new` publishes from: ``.<name>.tmp-<hex>``.
_TEMP_NAME = re.compile(r"\.(?P<name>.+)\.tmp-[0-9a-f]+")

#: The receipt ids this module generates (`_relocate`); recovery refuses any
#: other before it builds a path from one (Revision 9 item 6).
_RECEIPT_ID = re.compile(r"rr-\d{8}t\d{6}z-[0-9a-f]{8}")

_DEFAULT_TIMEOUT = 300.0

#: Module-private test seam, nothing else: ``_hooks[name](**context)`` is
#: called at the named point of `execute` -- ``after_lock``,
#: ``before_build``, ``after_place``, ``after_intent``, ``after_quarantine``,
#: ``after_compare``, ``before_inode_check``, ``before_unlink``,
#: ``after_rehash``, ``after_removal``, ``before_hold`` (in `_hold_run`),
#: ``before_receipt_open`` (in `_bundle_receipt_holds`, after containment is
#: decided, before the first ``openat`` below the target root) and
#: ``before_extract_gate`` (after `_recheck_proof`, before `_extract_gate`).
#: Only tests set it; no module of VeriHarness names it
#: outside this definition and `_hook` (``test_removal_only_in_execute``).
_hooks: dict[str, Callable[..., None]] = {}


def _hook(name: str, **context: Any) -> None:
    fn = _hooks.get(name)
    if fn is not None:
        fn(**context)


#: Re-exported: every call that touches an archive target is bounded.
TargetTimeout = st.TargetTimeout
bounded = st.bounded


def _live_holder(exc: BaseException) -> bool:
    """Whether a refusal proves a live other holder (Revision 11 item 1): its
    explicit ``reason`` is ``LIVE_HOLDER``. Never decided from a message."""
    return getattr(exc, "reason", None) == LIVE_HOLDER


def _flock_busy(exc: OSError) -> bool:
    """Whether a failed non-blocking ``flock`` means another holder
    (``EWOULDBLOCK``); any other errno (``ENOLCK``, ...) is a failure."""
    return exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN)


def _utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _stamp(moment: datetime.datetime | None = None) -> str:
    return (moment or _utc()).strftime("%Y%m%dT%H%M%SZ")


def _iso(moment: datetime.datetime | None = None) -> str:
    return (moment or _utc()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str).encode("ascii")


def _write_all(fd: int, data: bytes) -> None:
    """Write all of `data` to `fd`, looping over short writes (a module-private
    seam: a test makes it fail part-way)."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "a write wrote nothing")
        view = view[written:]


def write_new(path: Path | str, data: bytes) -> None:
    """Publish `data` under the new name `path`, atomically (Revision 10
    item 2): the bytes go to a fresh dot-named temporary file in the same
    directory (``.<name>.tmp-<hex>``, ``O_EXCL | O_NOFOLLOW``), are written
    whole (`_write_all`) and fsynced, then published with
    `archive_bundle._rename_noreplace` (never replacing) and the directory
    fsynced. A temporary file that could not be completed or published stays
    where it is -- nothing removes it, and no reader takes a dot-name for a
    record; `VerifiedRelocation.unreadable_receipts` lists it."""
    path = Path(path)
    temp = path.with_name(f".{path.name}.tmp-{secrets.token_hex(4)}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o644)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    ab._rename_noreplace(temp, path)
    ab._fsync_dir(path.parent)


def _torn_tail(fd: int) -> bool:
    """Whether the file behind `fd` ends in a line without its newline."""
    size = os.fstat(fd).st_size
    return size > 0 and os.pread(fd, 1, size - 1) != b"\n"


def _append_line(path: Path, record: Mapping[str, Any]) -> None:
    """Append one JSON line under an exclusive ``flock``, written whole; a
    torn last line is terminated first, so this record is read back."""
    line = (json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if _torn_tail(fd):
            line = b"\n" + line
        _write_all(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_lines(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _overlaps(a: str, b: str) -> bool:
    return _inside(a, b) or _inside(b, a)


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RelocationPlan:
    """A frozen plan: the file, its SHA-256, and what it holds."""

    path: Path
    digest: str
    project_id: str
    items: tuple[dict, ...]
    rejected: tuple[dict, ...]

    @property
    def bytes(self) -> int:
        return sum(int(item.get("bytes", 0)) for item in self.items)


@dataclass
class ExecuteReport:
    """What `execute` did: ``DONE``, ``BUSY``, ``REFUSED`` or ``FAILED`` (an
    `Exception` after the work began; the items finished before it are
    kept), and per item the ReclaimReceipt it wrote."""

    status: str
    plan_path: str
    detail: str = ""
    items: list[dict] = field(default_factory=list)
    #: The guest scratch C2's placements of this execute filled (extract roots
    #: and their index), and its bytes -- what the execute added to the
    #: project's namespace besides guest staging.
    placement_scratch: str | None = None
    placement_scratch_bytes: int = 0

    @property
    def relocated(self) -> list[dict]:
        return [i for i in self.items if i.get("status") == RELOCATED]

    @property
    def kept(self) -> list[dict]:
        return [i for i in self.items if i.get("status") != RELOCATED]


# --------------------------------------------------------------------------- #
# The operation
# --------------------------------------------------------------------------- #


class VerifiedRelocation:
    """The only code that may remove a local copy (see the module docstring).

    `registry` is the `StorageRegistry`; `targets` the `ArchiveTarget`s a
    bundle may be placed on (the first whose check confirms identity and a
    writable root is used); `ledger` an optional C2 `RedundancyLedger`;
    `receipts_dir` the directory for plans, the plan index and
    ReclaimReceipts (normally the project's registered ``receipts/``
    scratch).

    Keywords (injection, not truth): `coordinator` (default: a
    `StorageCoordinator` over the registry and `admission`), `admission`
    (the `StorageAdmission` that reserves ``archive_staging``), `sources`
    (a callable ``project_id -> EvidenceSources``), `getpid`, `boot_id`
    (readers for the sessions this operation opens), `timeout` (seconds
    for every call that touches a target) and `target_problems` (what made
    ``archive-targets.json`` unreadable as a whole; default none).
    """

    def __init__(self, registry, targets: Iterable[at.ArchiveTarget], ledger,
                 receipts_dir: Path | str, *, coordinator=None, admission=None,
                 sources: Callable[[str], st.EvidenceSources] | None = None,
                 getpid: Callable[[], int] = os.getpid,
                 boot_id: Callable[[], str] | None = None,
                 timeout: float = _DEFAULT_TIMEOUT,
                 target_problems: Iterable[str] = ()) -> None:
        self.registry = registry
        self.targets = list(targets)
        #: What made ``archive-targets.json`` unreadable as a whole (Revision
        #: 10 item 6). While any is listed a registered target may be unseen:
        #: every item is refused, and `recover`, `user_reclaimed` and the
        #: staging listings read nothing on any target.
        self.target_problems = [str(p) for p in target_problems]
        self.ledger = ledger
        self.receipts_dir = Path(receipts_dir)
        self.admission = admission
        self._sources_factory = sources
        self._getpid = getpid
        self._boot_id = boot_id or getattr(registry, "_boot_id_reader")
        self.timeout = timeout
        if coordinator is None:
            from hoh.storage_coordinator import StorageCoordinator
            coordinator = StorageCoordinator(registry, admission, [], getpid=getpid,
                                             boot_id_reader=self._boot_id)
        self.coordinator = coordinator

    # -- sources and bookkeeping ------------------------------------------- #

    def _sources(self, project_id: str) -> st.EvidenceSources:
        if self._sources_factory is not None:
            return self._sources_factory(project_id).fresh()
        return st.EvidenceSources(self.registry, project_id)

    @property
    def _plans_dir(self) -> Path:
        return self.receipts_dir / "plans"

    @property
    def _index(self) -> Path:
        return self.receipts_dir / INDEX_NAME

    def pinned(self, sources: st.EvidenceSources | None = None) -> set[str]:
        """Every path that is never itself planned: each original a plan (from
        the index) or a ReclaimReceipt relies on -- pins never expire here, so
        an original whose duplicate's bundle sits only in reclaim-review is
        never planned (Revision 2 item 9) -- and, with `sources`, every
        original that *could* be relied on: each file a committed retention
        manifest lists (Revision 4 item 2: "a listed original is pinned") and
        each tracked file of the canonical repository. A source of originals
        that cannot be read pins ``/`` -- nothing is planned (fail closed)."""
        pins = set()
        for record in _read_lines(self._index):
            pins.update(p for p in record.get("pins") or [] if isinstance(p, str))
        for receipt in self._receipts():
            for original in receipt.get("retained_originals") or []:
                if isinstance(original, dict) and isinstance(original.get("path"), str):
                    pins.add(original["path"])
        if sources is not None:
            for reader in (sources.retention, sources.git_originals):
                try:
                    pins.update(reader())
                except st.SourceUnreadable:
                    pins.add("/")
        return pins

    def _receipts(self) -> list[dict]:
        out = []
        try:
            names = sorted(os.listdir(self.receipts_dir))
        except FileNotFoundError:
            return out
        for name in names:
            if name.startswith("reclaim-") and name.endswith(".json"):
                raw, problem = _read_receipt(self.receipts_dir / name)
                if not problem:                 # listed by `unreadable_receipts`
                    out.append({**json.loads(raw), "_file": os.fspath(self.receipts_dir / name)})
        return out

    def unreadable_receipts(self) -> list[dict]:
        """What `_receipts` cannot use, and what `write_new` left unpublished
        (Revision 10 item 2), read-only: every ``reclaim-*.json`` that cannot
        be read, parsed or is not a ReclaimReceipt (``UNREADABLE``, with the
        problem), and every temporary ``.<name>.tmp-<hex>`` in the receipts
        directory and its ``plans/`` (``STRANDED_TEMP``, path and size) --
        except one that is a second link of its published file (same
        ``st_dev``/``st_ino`` as the final name, `_rename_noreplace`'s link
        fallback)."""
        out: list[dict] = []
        for directory in (self.receipts_dir, self._plans_dir):
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                continue
            for name in names:
                path = directory / name
                if directory == self.receipts_dir and name.startswith("reclaim-") \
                        and name.endswith(".json"):
                    _, problem = _read_receipt(path)
                    if problem:
                        out.append({"status": UNREADABLE, "path": os.fspath(path),
                                    "detail": problem})
                    continue
                match = _TEMP_NAME.fullmatch(name)
                if match is None:
                    continue
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                try:
                    final = os.lstat(directory / match.group("name"))
                    if (final.st_dev, final.st_ino) == (info.st_dev, info.st_ino):
                        continue                # the link fallback's second name
                except OSError:
                    pass
                out.append({"status": STRANDED_TEMP, "path": os.fspath(path),
                            "bytes": info.st_size})
        return out

    # -- plan -------------------------------------------------------------- #

    def plan(self, project_id: str, candidates: Iterable[str | os.PathLike[str]]
             ) -> RelocationPlan:
        """Re-classify every candidate now and freeze the eligible items of
        `project_id` to a plan file. See the module docstring."""
        sources: dict[str, st.EvidenceSources] = {project_id: self._sources(project_id)}
        pins = self.pinned(sources[project_id])
        accepted: list[dict] = []
        rejected: list[dict] = []
        for candidate in candidates:
            shown = os.fspath(candidate)
            owner = self.registry.owner_of(candidate)
            owner_project = owner.project_id if owner.status == OWNED else None
            decision = st.classify_tier(
                candidate, registry=self.registry,
                sources=(sources.setdefault(owner_project, self._sources(owner_project))
                         if owner_project else None))
            reason = self._plan_refusal(project_id, decision, accepted, pins)
            if reason:
                rejected.append({"path": shown, "reason": reason, "tier": decision.tier,
                                 "refused": decision.refused})
                continue
            entry = self._plan_item(decision, shown)
            if entry["classification"] == st.VERIFIED_DUPLICATE and not entry["content_digest"]:
                rejected.append({"path": shown, "tier": decision.tier, "refused": None,
                                 "reason": f"{ITEM_CHANGED}: its content cannot be measured"})
                continue
            accepted.append(entry)
            pins.update(o["path"] for o in accepted[-1]["originals"])
        body = {
            "schema": PLAN_SCHEMA,
            "project_id": project_id,
            "created_utc": _iso(),
            "items": accepted,
            "rejected": rejected,
            "pins": sorted({o["path"] for item in accepted for o in item["originals"]}),
        }
        document = {"plan": body, "body_sha256": hashlib.sha256(_canonical(body)).hexdigest()}
        self._plans_dir.mkdir(parents=True, exist_ok=True)
        path = self._plans_dir / f"plan-{_stamp()}-{secrets.token_hex(4)}.json"
        raw = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True).encode("ascii")
        write_new(path, raw)
        digest = hashlib.sha256(raw).hexdigest()
        _append_line(self._index, {"plan": path.name, "sha256": digest,
                                   "project_id": project_id, "pins": body["pins"],
                                   "created_utc": body["created_utc"]})
        return RelocationPlan(path=path, digest=digest, project_id=project_id,
                              items=tuple(accepted), rejected=tuple(rejected))

    def _plan_refusal(self, project_id: str, decision: st.TierDecision,
                      accepted: list[dict], pins: set[str]) -> str:
        if decision.refused:
            return f"{decision.refused}: {'; '.join(decision.reasons)}"
        if decision.project_id != project_id:
            return f"OTHER_PROJECT: owned by {decision.project_id}, not {project_id}"
        if not decision.eligible:
            return f"NOT_ELIGIBLE: tier {decision.tier}: {'; '.join(decision.reasons)}"
        owner = decision.ownership
        if owner is None or owner.resolved is None or not owner.resolved.is_dir() \
                or owner.resolved.is_symlink():
            return "NOT_A_DIRECTORY: only directories are relocated"
        overlap = self._target_overlap(decision.path)
        if overlap:
            return overlap
        path = decision.path
        for item in accepted:
            if _overlaps(path, item["path"]):
                return f"NESTED: overlaps the planned item {item['path']}"
        # `pins` holds every original a plan, a receipt or an item accepted
        # above relies on, every listed retention original and every tracked
        # canonical file: an original is never itself a candidate.
        for pinned in sorted(pins):
            if _overlaps(pinned, path):
                return f"{PINNED_ORIGINAL}: {pinned} is a retained original (listed, " \
                       f"tracked, or relied on by a plan or a receipt)"
        originals = (decision.proof or {}).get("originals") or []
        for original in originals:
            for item in accepted:
                if _overlaps(original["path"], item["path"]):
                    return f"ORIGINAL_IN_PLAN: its original {original['path']} lies in the " \
                           f"planned item {item['path']}"
        return ""

    @staticmethod
    def _plan_item(decision: st.TierDecision, given: str) -> dict:
        owner = decision.ownership
        proof = dict(decision.proof or {})
        return {
            "path": decision.path,
            "given": given,
            "project_id": decision.project_id,
            "entries_digest": st.entries_digest(decision.entries),
            # The bytes that were proven duplicate, in the bundle's own digest
            # (paths, types, modes, sizes, SHA-256, link texts): what the
            # bundle's manifest and every later re-measure must equal.
            "content_digest": (_tree_digest(Path(decision.path))
                               if decision.tier == st.VERIFIED_DUPLICATE else None),
            "entries": [[e.rel, e.type] for e in decision.entries]
            if proof.get("type") == "extract_root" else [],
            "classification": decision.tier,
            "proof_digest": decision.proof_digest,
            "proof": proof,
            "bytes": decision.bytes,
            "st_dev": owner.st_dev,
            "st_ino": owner.st_ino,
            "root": os.fspath(owner.root),
            "kind": owner.kind,
            "session_id": owner.session_id,
            "originals": [{"path": o["path"], "digest": o["digest"], "source": o["source"]}
                          for o in proof.get("originals") or []],
        }

    # -- execute ----------------------------------------------------------- #

    def _load_plan(self, plan_path: Path) -> tuple[dict | None, str, str]:
        try:
            raw = Path(plan_path).read_bytes()
        except OSError as exc:
            return None, "", f"{PLAN_DIGEST}: plan unreadable: {exc}"
        digest = hashlib.sha256(raw).hexdigest()
        listed = [r for r in _read_lines(self._index)
                  if r.get("plan") == Path(plan_path).name]
        if not any(r.get("sha256") == digest for r in listed):
            return None, digest, f"{PLAN_DIGEST}: the plan's digest is not the one recorded " \
                                 f"when it was frozen"
        try:
            document = json.loads(raw)
            body = document["plan"]
            if hashlib.sha256(_canonical(body)).hexdigest() != document["body_sha256"]:
                raise ValueError("body digest does not recompute")
            if body.get("schema") != PLAN_SCHEMA:
                raise ValueError("not a relocation plan")
        except (ValueError, KeyError, TypeError) as exc:
            return None, digest, f"{PLAN_DIGEST}: {exc}"
        return body, digest, ""

    def execute(self, plan_path: Path | str) -> ExecuteReport:
        """Execute a frozen plan; see the module docstring for every step."""
        plan_path = Path(plan_path)
        body, digest, problem = self._load_plan(plan_path)
        if body is None:
            return ExecuteReport(REFUSED, os.fspath(plan_path), problem)
        project_id = body["project_id"]
        # An execute of this process already holding the session (a nested
        # call) must not close it on its way out; the flock refuses it anyway.
        opened_here = (project_id, RELOCATION_SESSION) not in self.coordinator._sessions
        try:
            lease = self.coordinator.acquire_session(project_id, RELOCATION_SESSION)
        except Exception as exc:                # noqa: BLE001 - BUSY or FAILED, said
            if _live_holder(exc):               # a proven live other holder
                return ExecuteReport(BUSY, os.fspath(plan_path),
                                     f"the relocation session is held by a live other "
                                     f"holder: {exc}")
            return ExecuteReport(FAILED, os.fspath(plan_path),
                                 f"the relocation session could not be opened: "
                                 f"{type(exc).__name__}: {exc}")
        # One report, created before `coordinate`: the work fills this same
        # object, so nothing it finished is lost when it fails (Rev 10 item 2).
        report = ExecuteReport(FAILED, os.fspath(plan_path))
        fd = None
        try:
            try:
                scratch = self.registry.scratch_dir(project_id, RELOCATION_SESSION, "scratch")
                fd = os.open(scratch / LOCK_NAME,
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
            except Exception as exc:            # noqa: BLE001 - FAILED, said
                report.detail = (f"{LOCK_NAME} could not be opened: "
                                 f"{type(exc).__name__}: {exc}")
                return report
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if _flock_busy(exc):            # EWOULDBLOCK: another execute holds it
                    report.status = BUSY
                    report.detail = f"{LOCK_NAME} is held by another execute: {exc}"
                else:
                    report.detail = (f"{LOCK_NAME} could not be locked: "
                                     f"{type(exc).__name__}: {exc}")
                return report
            started: list[bool] = []

            def _work() -> ExecuteReport:
                started.append(True)
                report.status = DONE
                try:
                    self._execute_locked(plan_path, body, digest, scratch, report)
                except Exception as exc:        # noqa: BLE001 - after work began: FAILED
                    report.status = FAILED
                    report.detail = (report.detail + "; " if report.detail else "") + \
                        f"{type(exc).__name__}: {exc}"
                return report
            try:
                self.coordinator.coordinate(project_id, RELOCATION_SESSION, _work)
            except Exception as exc:            # noqa: BLE001 - BUSY or FAILED, said
                if not started and _live_holder(exc):   # refused before any work by
                    report.status = BUSY                # a live other driver
                    report.detail = f"the project's driver lease is not ours: {exc}"
                elif not started:               # any other refusal: said, NEEDS_YOU
                    report.status = FAILED
                    report.detail = (f"the project's driver lease could not be taken: "
                                     f"{type(exc).__name__}: {exc}")
                else:
                    report.status = FAILED
                    report.detail = (report.detail + "; " if report.detail else "") + \
                        f"{type(exc).__name__}: {exc}"
            return report
        finally:
            if fd is not None:
                os.close(fd)                    # releases the flock
            if opened_here:
                try:
                    self.coordinator.close_session(lease)
                except Exception as exc:        # superseded meanwhile: said, not hidden
                    report.detail += f"; closing the relocation session failed: {exc}"

    def _execute_locked(self, plan_path: Path, body: dict, digest: str,
                        scratch: Path, report: ExecuteReport) -> ExecuteReport:
        project_id = body["project_id"]
        _hook("after_lock", plan_path=plan_path, project_id=project_id)
        sources = self._sources(project_id)
        try:
            retention = sources.retention()
        except st.SourceUnreadable as exc:
            retention = None
            unreadable = str(exc)
        for item in body["items"]:
            for original in item["originals"]:
                if original["source"] != "retention":
                    continue
                now = retention.get(original["path"]) if retention is not None else None
                if now is None or now.digest != original["digest"]:
                    report.status = REFUSED
                    report.detail = (f"{RETENTION_MANIFEST_CHANGED}: the entry of "
                                     f"{original['path']} is not the one the plan relied on"
                                     + (f" ({unreadable})" if retention is None else ""))
                    return report
        for item in body["items"]:
            decision = st.classify_tier(item["path"], registry=self.registry, sources=sources)
            if (decision.tier != item["classification"] or decision.path != item["path"]
                    or decision.proof_digest != item["proof_digest"]):
                report.status = REFUSED
                report.detail = (f"{CHANGED_PLAN}: {item['path']} is now "
                                 f"{decision.tier or decision.refused} "
                                 f"({'; '.join(decision.reasons + decision.notes)[:400]}); "
                                 f"the plan is never broadened or re-read")
                return report
        place_lease = None
        place_session = f"place-{_stamp().lower()}-{secrets.token_hex(3)}"
        try:
            place_lease = self.registry.open_session(project_id, place_session,
                                                     pid=self._getpid(), boot_id=self._boot_id())
        except StorageOwnershipError as exc:
            report.status = REFUSED
            report.detail = f"no session for the placements: {exc}"
            return report
        try:
            for item in body["items"]:
                record = self._relocate(project_id, item, digest, plan_path, scratch,
                                        place_session)
                report.items.append(record)
            roots = [r["path"] for i in report.items for r in i.get("extract_roots") or []]
            if roots:
                report.placement_scratch = os.fspath(Path(roots[0]).parent)
                report.placement_scratch_bytes = _tree_bytes(Path(roots[0]).parent)
        finally:
            try:
                self.registry.close_session(project_id, place_session, place_lease)
            except StorageOwnershipError as exc:
                report.detail += f"closing {place_session} failed: {exc}"
        return report

    # -- one item ---------------------------------------------------------- #

    def _relocate(self, project_id: str, item: dict, plan_digest: str, plan_path: Path,
                  scratch: Path, place_session: str) -> dict:
        receipt_id = f"rr-{_stamp().lower()}-{secrets.token_hex(4)}"
        path = Path(item["path"])
        record: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "receipt_id": receipt_id,
            "plan_digest": plan_digest,
            "plan_path": os.fspath(plan_path),
            "project_id": project_id,
            "path": item["path"],
            "root": item["root"],
            "classification": item["classification"],
            "proof_digest": item["proof_digest"],
            "proof": item["proof"],
            "retained_originals": item["originals"],
            "bytes": item["bytes"],
            "retain_ref": None,
            "bundle_id": None,
            "bundle_path": None,
            "bundle_receipt_digest": None,
            "target_id": None,
            "compressed_bytes": 0,
            "readback_status": None,
            "staging": None,
            "staging_leftover": None,
            "target_leftovers": [],
            "extract_roots": [],
            "quarantine_path": None,
            "post_delete_absence": False,
            "needs_you": False,
            "status": KEPT,
            "reason": "",
            "time_utc": _iso(),
        }
        locks: list[int] = []
        try:
            self._relocate_steps(project_id, item, path, record, scratch, place_session, locks)
        except Exception as exc:                # an unexpected failure keeps the item
            if record["status"] not in (RELOCATED, PARTIAL, LATE_ENTRY_RESTORED):
                reason = f"UNEXPECTED: {type(exc).__name__}: {exc}"
                quarantine = record.get("quarantine_path")
                if record.get("removed_entries"):
                    # Part of it is gone: never restored as whole (Rev 4 item 3).
                    record.update(status=PARTIAL, needs_you=True, reason=f"{PARTIAL}: {reason}")
                elif quarantine and not record.get("restored_to") \
                        and os.path.lexists(quarantine):
                    try:                        # never left under its quarantine name
                        self._restore(Path(quarantine), path, record, reason)
                    except Exception as again:  # noqa: BLE001 - said, NEEDS_YOU
                        reason += f"; restoring {quarantine} failed: {again}"
                if record["status"] != PARTIAL:
                    record.update(status=KEPT, needs_you=True, reason=reason)
        finally:
            for fd in locks:
                os.close(fd)
        record["finished_utc"] = _iso()
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = self.receipts_dir / f"reclaim-{receipt_id}.json"
        raw = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=True,
                         default=str).encode("ascii")
        try:
            write_new(receipt_path, raw)
        except (OSError, ab.ArchiveError) as exc:
            # Reported, not raised: the execute goes on. No completion is
            # appended, so the intent (if any) stays open and the next
            # `recover` judges it and writes the receipt (Revision 10 item 2).
            record.update(receipt_not_written=f"{type(exc).__name__}: {exc}", needs_you=True)
            return record
        record["receipt_path"] = os.fspath(receipt_path)
        if record.get("intent_written"):
            try:                                # an open intent is resolved by `recover`
                _append_intent(scratch, {"schema": INTENT_SCHEMA, "item": item["path"],
                                         "receipt_id": receipt_id,
                                         "completed": hashlib.sha256(raw).hexdigest(),
                                         "written_at": _iso()})
            except OSError as exc:
                record["completion_not_recorded"] = str(exc)
        return record

    def _relocate_steps(self, project_id: str, item: dict, path: Path, record: dict,
                        scratch: Path, place_session: str, locks: list[int]) -> None:
        def keep(reason: str, **extra: Any) -> None:
            record.update(status=KEPT, reason=reason, **extra)

        owner = self.registry.owner_of(path)
        if (owner.status != OWNED or owner.project_id != project_id or owner.resolved != path
                or (owner.st_dev, owner.st_ino) != (item["st_dev"], item["st_ino"])):
            return keep(f"{OWNERSHIP_CHANGED}: {owner.status} {owner.reason}")
        try:
            pin_sources = self._sources(project_id)
        except Exception as exc:                # no sources, no removal
            return keep(f"{PINNED_ORIGINAL}: the originals cannot be read ({exc})")
        others = {p for p in self.pinned(pin_sources)
                  if p not in {o["path"] for o in item["originals"]}}
        for pinned in others:
            if _overlaps(pinned, item["path"]):
                return keep(f"{PINNED_ORIGINAL}: {pinned}")
        proof = item["proof"]
        extract_root = proof.get("type") == "extract_root"

        if not extract_root:
            commit = proof.get("commit")
            if commit:
                ref = st.RETAIN_NAMESPACE + record["receipt_id"]
                done = st._git(proof["canonical"], "update-ref", "--no-deref", ref, commit, "")
                if done.returncode != 0:
                    return keep(f"{RETAIN_REF_FAILED}: "
                                + done.stderr.decode("utf-8", "replace").strip()[:200])
                record["retain_ref"] = {"repository": proof["canonical"], "ref": ref,
                                        "commit": commit}
            if not self._archive(project_id, item, path, owner, record, scratch,
                                 place_session):
                return None
        _hook("after_place", item=item, record=record)

        sources = self._sources(project_id)
        problem = self._recheck_proof(item, path, record, sources, locks)
        if problem:
            return keep(problem)
        references = st.reference_reasons(item["path"], root=Path(item["root"]),
                                          sources=sources)
        if references:
            return keep(f"{REFERENCED}: " + "; ".join(references)[:400])

        containing = None
        if extract_root:
            # Target problems and the overlap are judged before the gate reads
            # any target (Revision 11 item 5), and again before the rename.
            overlap = self._target_overlap(item["path"])
            if overlap:
                record.update(status=REFUSED, reason=overlap)
                return None
            _hook("before_extract_gate", item=item, proof=proof, record=record)
            # Its containing target, identity and payload settled before the
            # intent (Revision 10 item 5): otherwise no intent, no rename.
            problem, containing = self._extract_gate(proof)
            if problem:
                return keep(problem)
        quarantine = path.with_name(f"{path.name}{QUARANTINE_MARK}{_stamp()}")
        # The durable intent comes first: an execute that dies from here on
        # leaves a record `recover` resolves without renaming or removing.
        try:
            _append_intent(scratch, self._intent(item, record, quarantine, containing))
        except OSError as exc:
            return keep(f"{INTENT_FAILED}: the intent could not be written durably ({exc}); "
                        f"nothing renamed")
        record["intent_written"] = True
        _hook("after_intent", item=item, quarantine=quarantine, record=record)
        overlap = self._target_overlap(item["path"])     # immediately before the rename
        if overlap:
            record.update(status=REFUSED, reason=overlap)
            return None
        try:
            ab._rename_noreplace(path, quarantine)
            ab._fsync_dir(path.parent)
        except (OSError, ab.ArchiveError) as exc:
            return keep(f"{QUARANTINE_FAILED}: {exc}")
        record["quarantine_path"] = os.fspath(quarantine)
        _hook("after_quarantine", item=item, quarantine=quarantine, record=record)

        # One walk of the quarantine (Revision 8 item 1): it is compared, and
        # it -- with the st_dev/st_ino it recorded -- is the removal set.
        inodes: dict[str, tuple[int, int]] = {}
        try:
            walked = st.inventory(quarantine, self.registry, project_id, inodes=inodes)
        except st._NotOwned as exc:
            return self._restore(quarantine, path, record,
                                 f"{QUARANTINE_MISMATCH}: the quarantine cannot be walked "
                                 f"as owned ({exc})")
        if not extract_root:
            current = _tree_digest(quarantine)
            if current is None or current != record.get("content_digest"):
                return self._restore(quarantine, path, record,
                                     f"{QUARANTINE_MISMATCH}: the quarantined item no longer "
                                     f"equals the bundle it was archived into")
        if st.entries_digest(walked) != item["entries_digest"]:
            return self._restore(quarantine, path, record,
                                 f"{ENTRIES_MISMATCH}: the quarantined item's entries are not "
                                 f"the plan item's entries_digest")
        for entry in walked:
            try:
                _safe_entry_path(entry.rel)
            except UnsafeEntry as exc:
                return self._restore(quarantine, path, record, f"{UNSAFE_ENTRY}: {exc}")
        _hook("after_compare", item=item, quarantine=quarantine, record=record)

        # In quarantine, after the compare (Revision 3 item 5): no process of
        # the same uid with a cwd or descriptor inside, no live lease.
        now = sources.fresh()
        try:
            holders = now.processes_inside(os.fspath(quarantine))
        except st.SourceUnreadable as exc:
            return self._restore(quarantine, path, record,
                                 f"{OPEN_FD}: the process probe cannot read ({exc})")
        if holders:
            kind = CWD_INSIDE if any(" cwd " in h for h in holders) else OPEN_FD
            return self._restore(quarantine, path, record, f"{kind}: " + "; ".join(holders))
        if item.get("kind") in SCRATCH_KINDS and item.get("session_id"):
            try:
                live = now.live_sessions()
            except st.SourceUnreadable as exc:
                return self._restore(quarantine, path, record,
                                     f"{LEASE}: lease source unreadable ({exc})")
            if item["session_id"] in live:
                return self._restore(quarantine, path, record,
                                     f"{LEASE}: session {item['session_id']} holds a live "
                                     f"lease")

        if item.get("kind") in LEGACY_KINDS:
            # The legacy liveness, read anew in quarantine (STORAGE-L item 8):
            # a report rewritten, an uncited committed brief, or the root
            # drifting after the plan keeps the item. Its own quarantine is
            # not drift (same inode, child absent).
            try:
                legacy = st.legacy_liveness(Path(item["root"]), now)
            except Exception as exc:            # unreadable is HOT
                legacy = [f"legacy liveness unreadable ({exc})"]
            if legacy:
                return self._restore(quarantine, path, record,
                                     f"{LEGACY_HOT}: " + "; ".join(legacy)[:400])

        if item["classification"] == st.VERIFIED_DUPLICATE:
            problem = self._still_equal(quarantine, item)   # re-read just before removal
            if problem:
                return self._restore(quarantine, path, record, problem)
        _hook("before_inode_check", item=item, quarantine=quarantine, record=record)
        self._remove(project_id, item, path, quarantine, record, walked, inodes, scratch)
        return None

    def _intent(self, item: dict, record: dict, quarantine: Path,
                containing: str | None = None) -> dict:
        """The intent record of one item, written before its quarantine
        rename: what `recover` needs to judge an interrupted removal. An
        extract root places nothing, so its ``target_id`` stays None; the
        registered target holding its bundle is recorded apart, as
        ``containing_target_id`` (Revision 9 item 6), as `_extract_gate`
        decided it before this intent (Revision 10 item 5)."""
        proof = item["proof"]
        extract_root = proof.get("type") == "extract_root"
        bundle = proof.get("bundle_location") if extract_root else record.get("bundle_path")
        if not extract_root:
            containing = record.get("target_id")
        return {
            "schema": INTENT_SCHEMA,
            "item": item["path"],
            "root": item["root"],
            "classification": item["classification"],
            "plan_digest": record["plan_digest"],
            "receipt_digest": (proof.get("receipt_file_sha256") if extract_root
                               else record.get("bundle_receipt_digest")),
            "bundle_path": bundle,
            "target_id": record.get("target_id"),
            "containing_target_id": containing,
            "quarantine_name": os.fspath(quarantine),
            "written_at": _iso(),
            # Beside the named fields: what a recovered receipt carries on.
            "receipt_id": record["receipt_id"],
            "project_id": record["project_id"],
            "plan_path": record["plan_path"],
            "proof_digest": record["proof_digest"],
            "retained_originals": record["retained_originals"],
            "bytes": record["bytes"],
        }

    def _archive(self, project_id: str, item: dict, path: Path, owner, record: dict,
                 scratch: Path, place_session: str) -> bool:
        """Stage, build and place the item's bundle; True only on
        ARCHIVED_VERIFIED. On False the record says why the item is kept.

        Before the staging is chosen -- on every pass, whatever it will be --
        the leftovers earlier builds of this item path left on any target
        are followed by their label (`_follow_leftovers`); a target with one
        (or with an unmeasurable listing) is not built on in this pass, while
        guest staging and other targets stay possible."""
        blocked = self._follow_leftovers(project_id, item, record, scratch)
        target = None
        out_parent: Path | None = None
        reservation = None
        where = ""
        guest: tuple[Path, Any] | None = None
        guest_asked = False
        confirmed = False
        refusals: list[str] = []
        for candidate in self.targets:
            try:
                check = bounded(candidate.check, self.timeout, f"check {candidate.target_id}")
            except TargetTimeout as exc:
                record.setdefault("targets_timed_out", []).append(candidate.target_id)
                record.update(reason=f"{TARGET_TIMEOUT}: {exc}")
                continue
            if not check.ok:
                continue
            confirmed = True
            if not guest_asked:                 # one reservation, asked once
                guest_asked = True
                guest = self._guest_staging(project_id, item, scratch)
            if guest is not None:
                target, (out_parent, reservation), where = candidate, guest, "guest"
                break
            out, why = self._target_staging(project_id, item, candidate, blocked)
            if out is None:
                refusals.append(why)
                continue
            target, out_parent, where = candidate, out, "target"
            break
        if target is None or out_parent is None:
            if not confirmed:
                if not record["reason"]:
                    record.update(reason=f"{NO_TARGET}: no target confirms its identity and "
                                         f"a writable root")
                record["needs_you"] = True
                return False
            record.update(reason=f"{NO_STAGING}: " + "; ".join(refusals)[:800])
            if any(r.startswith(LEFTOVER_PRESENT) for r in refusals):
                record["needs_you"] = True
            return False
        record["target_id"] = target.target_id
        record["staging"] = {"where": where, "path": os.fspath(out_parent)}
        duplicate = item["classification"] == st.VERIFIED_DUPLICATE
        if duplicate:
            problem = self._item_matches_plan(path, item)
            if problem:
                record.update(reason=problem)
                self._release(reservation, None)
                return False
        ownership = {"status": owner.status, "root": os.fspath(owner.root)}
        meta = {"unique": False, "classification": item["classification"],
                "proof_digest": item["proof_digest"], "project_id": project_id,
                "receipt_id": record["receipt_id"]}

        label = record["receipt_id"]

        def build(cancel: Callable[[], bool]) -> Path:
            # The receipt id as label: two items with equal content built in
            # the same second still get distinct bundle names, and whatever a
            # build leaves is found by it.
            return ab.ArchiveBundle.build_tree(path, out_parent, label=label, meta=meta,
                                               ownership=ownership, cancel=cancel)
        # The build record (Revision 10 item 1): journaled durably before the
        # build starts, so whatever it leaves is followed by its label even
        # when this pass dies or its receipt is lost. No record, no build.
        try:
            _append_intent(scratch, {"schema": INTENT_SCHEMA, "build_started": True,
                                     "item": item["path"], "receipt_id": label,
                                     "target_id": target.target_id, "where": where,
                                     "out_parent": os.fspath(out_parent),
                                     "written_at": _iso()})
        except OSError as exc:
            self._release(reservation, None)
            record.update(reason=f"{INTENT_FAILED}: the build record could not be written "
                                 f"durably ({exc}); nothing built")
            return False
        bundle = None
        _hook("before_build", item=item, record=record)
        try:
            bundle = self._build(build, where)
        except _BuildTimedOut as exc:           # cancelled; waited for once more
            record.update(reason=f"{TARGET_TIMEOUT}: {exc}")
            record["staging_leftover"] = self._leftover(out_parent, label, where,
                                                        still_writing=exc.still_writing)
            return False
        except Exception as exc:                # SourceChanged, refused, ENOSPC
            record.update(reason=f"{BUILD_FAILED}: {type(exc).__name__}: {exc}")
            record["staging_leftover"] = self._leftover(out_parent, label, where)
            return False
        finally:
            self._release(reservation, bundle)
        # Every staged bundle stays where it was built -- on the target or in
        # the guest -- and is reported for the user (Revision 6b item 4); the
        # guest copy counts against what this relocation frees.
        try:
            staged = (bounded(lambda: _tree_bytes(bundle), self.timeout, "size the staged bundle")
                      if where == "target" else _tree_bytes(bundle))
        except TargetTimeout as exc:
            record.update(reason=f"{TARGET_TIMEOUT}: {exc}")
            record["staging_leftover"] = {"status": STAGING_LEFTOVER, "where": where,
                                          "path": os.fspath(bundle), "paths": [os.fspath(bundle)],
                                          "bytes": None, "measured": False,
                                          "detail": str(exc)}
            return False
        record["staging_leftover"] = {"status": STAGING_LEFTOVER, "where": where,
                                      "path": os.fspath(bundle), "bytes": staged}
        if where == "guest":
            record["staging"]["bundle"] = os.fspath(bundle)
        manifest = json.loads((bundle / ab.MANIFEST).read_text(encoding="ascii"))
        record["content_digest"] = manifest.get("content_digest")
        record["bundle_id"] = manifest.get("bundle_id")
        payload = bundle / ab.TREE_PAYLOAD
        record["compressed_bytes"] = payload.stat().st_size
        if duplicate:
            # The bundle must hold exactly the bytes that were proven duplicate:
            # a bundle of changed bytes is never placed as unique: false.
            problem = self._item_matches_plan(path, item,
                                              content_digest=manifest.get("content_digest"))
            if problem:
                record.update(reason=problem + " (the built bundle is not placed)")
                return False
        eligibility = {"classification": item["classification"],
                       "proof_digest": item["proof_digest"], "unique": False}
        try:
            placed = bounded(lambda: target.place(bundle, project_id, at.RECLAIM_REVIEW,
                                                  eligibility, registry=self.registry,
                                                  session_id=place_session, ledger=self.ledger),
                             self.timeout, f"place on {target.target_id}")
        except TargetTimeout as exc:
            record.update(reason=f"{TARGET_TIMEOUT}: {exc}")
            return False
        except Exception as exc:                # refused before copying
            record.update(reason=f"{DESTINATION_UNVERIFIED}: {type(exc).__name__}: {exc}")
            return False
        record["readback_status"] = placed.status
        record["bundle_path"] = os.fspath(placed.path)
        # C2 verifies the placement by extracting it into fresh registered
        # extract roots in the guest (place session scratch). They stay until
        # their receipt is committed (AWAITING_COMMITTED_RECEIPT) or, for a
        # failed verification, are UNVERIFIED_EXTRACT -- reported, and counted
        # against what this relocation frees.
        record["extract_roots"] = [
            {"path": os.fspath(root), "bytes": _tree_bytes(Path(root)),
             "status": (st.AWAITING_COMMITTED_RECEIPT if placed.status == ab.ARCHIVED_VERIFIED
                        else st.UNVERIFIED_EXTRACT)}
            for root in placed.extract_roots or []]
        if placed.status != ab.ARCHIVED_VERIFIED or not placed.receipt_digest:
            record.update(reason=f"{DESTINATION_UNVERIFIED}: {placed.detail}")
            return False
        record["bundle_receipt_digest"] = placed.receipt_digest
        record["bundle_receipt_path"] = os.fspath(placed.receipt_path)
        return True

    def _build(self, build: Callable[[Callable[[], bool]], Path], where: str) -> Path:
        """Run `build` with a cancel flag. In the guest it runs here; on the
        target in a thread bounded by the timeout: when it is not done in
        time the cancel is set (`archive_bundle` writes no further entry) and
        the thread is waited for once more, then `_BuildTimedOut` says
        whether it is still writing."""
        cancel = threading.Event()
        if where != "target":
            return build(cancel.is_set)
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["value"] = build(cancel.is_set)
            except BaseException as exc:        # handed to the caller
                box["error"] = exc

        thread = threading.Thread(target=run, name="vh-build-on-target", daemon=True)
        thread.start()
        thread.join(self.timeout)
        if thread.is_alive():
            cancel.set()
            thread.join(self.timeout)
            still = thread.is_alive()
            raise _BuildTimedOut(f"the build on the target ran longer than {self.timeout} s; "
                                 f"cancelled" + ("; still writing" if still else ""),
                                 still_writing=still)
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _leftover(self, out_parent: Path, label: str, where: str, *,
                  still_writing: bool = False) -> dict | None:
        """What a failed, timed-out or abandoned build left under
        `out_parent` (its directories carry the label), measured now -- on
        the target under the timeout. Reported, never removed; None when the
        build left nothing."""
        status = STILL_WRITING if still_writing else STAGING_LEFTOVER
        suffixes = ("-" + label, "-" + label + ".building")

        def measure() -> list[tuple[str, int]]:
            names = sorted(n for n in os.listdir(out_parent) if n.endswith(suffixes))
            return [(os.fspath(out_parent / n), _tree_bytes(out_parent / n)) for n in names]
        try:
            found = (bounded(measure, self.timeout, "measure the staged leftover")
                     if where == "target" else measure())
        except (TargetTimeout, OSError) as exc:
            return {"status": status, "where": where, "path": os.fspath(out_parent),
                    "paths": [], "bytes": None, "measured": False, "detail": str(exc)}
        if not found and not still_writing:
            return None
        return {"status": status, "where": where,
                "path": found[0][0] if found else os.fspath(out_parent),
                "paths": [p for p, _ in found], "bytes": sum(b for _, b in found),
                "measured": True}

    def _leftovers_present(self, item: dict, target: at.ArchiveTarget) -> list[str]:
        """The target-side leftovers an earlier attempt of this item reported
        on `target` that are still there -- or whose absence cannot be
        measured (counted as present)."""
        present = []
        for receipt in self._receipts():
            leftover = receipt.get("staging_leftover") or {}
            if receipt.get("path") != item["path"] or receipt.get("status") == RELOCATED \
                    or receipt.get("target_id") != target.target_id \
                    or leftover.get("where") != "target":
                continue
            for path in leftover.get("paths") or [leftover.get("path")]:
                if not isinstance(path, str):
                    present.append(f"{receipt.get('receipt_id')}: unmeasurable")
                    continue
                try:
                    there = bounded(lambda p=path: _leftover_present(p), self.timeout,
                                    "look for a staged leftover")
                except Exception as exc:        # unmeasurable counts as present
                    present.append(f"{path} (unmeasurable: {exc})")
                    continue
                if there:
                    present.append(path)
        return present

    def _release(self, reservation, bundle: Path | None) -> None:
        if reservation is None:
            return
        try:
            self.admission.release(reservation, _tree_bytes(bundle) if bundle is not None else 0)
        except Exception:                       # the reservation stays open, counted
            pass

    def _item_matches_plan(self, root: Path, item: dict, *,
                           content_digest: str | None = None) -> str:
        """Why the duplicate at `root` (or the bundle whose content digest is
        given) is no longer the bytes that were planned, or "". Re-measured:
        every entry asked of the registry, the inventory digest against the
        plan's ``entries_digest`` and the tree's content digest against the
        plan's ``content_digest``."""
        if content_digest is None:
            try:
                entries = st.inventory(root, self.registry, item["project_id"])
            except st._NotOwned as exc:
                return f"{ITEM_CHANGED}: {exc}"
            if st.entries_digest(entries) != item["entries_digest"]:
                return f"{ITEM_CHANGED}: {root} no longer holds the planned entries"
            content_digest = _tree_digest(root)
        if not content_digest or content_digest != item.get("content_digest"):
            return f"{ITEM_CHANGED}: the content is not the planned content"
        return ""

    @staticmethod
    def _still_equal(root: Path, item: dict) -> str:
        """Why an entry of the duplicate at `root` no longer byte-equals its
        current original, or "" -- both re-read now."""
        for original in item["proof"].get("originals") or []:
            mine = os.path.join(root, original["item_rel"])
            theirs = original["path"]
            try:
                a, b = os.lstat(mine), os.lstat(theirs)
                if stat.S_ISLNK(a.st_mode) and stat.S_ISLNK(b.st_mode):
                    same = os.readlink(mine) == os.readlink(theirs)
                elif stat.S_ISREG(a.st_mode) and stat.S_ISREG(b.st_mode):
                    same = a.st_size == b.st_size and \
                        st.sha256_file(mine) == st.sha256_file(theirs)
                else:
                    same = False
            except OSError as exc:
                return f"{ITEM_CHANGED}: {original['item_rel']} cannot be compared: {exc}"
            if not same:
                return f"{ITEM_CHANGED}: {original['item_rel']} no longer equals {theirs}"
        return ""

    def _guest_staging(self, project_id: str, item: dict, scratch: Path
                       ) -> tuple[Path, Any] | None:
        """Guest staging: an admitted ``archive_staging`` reservation, only in
        a known state short of CRITICAL -- the guest directory and the
        reservation, or None (refused, CRITICAL, unknown state, no
        admission: the bundle is built on the target)."""
        state = None
        reservation = None
        if self.admission is not None:
            try:
                state = self.admission.status().get("state")
            except Exception:                   # unobservable: no guest staging
                state = None
            # Guest staging only in a known state short of CRITICAL; an
            # unknown or unobservable state allows none.
            if state in ("NORMAL", "OPTIMIZE", "PRESSURE"):
                try:
                    decision = self.admission.reserve(project_id, "archive_staging",
                                                      estimate_bytes=int(item["bytes"]))
                except Exception:
                    decision = None
                if getattr(decision, "admitted", False):
                    reservation = decision
        if reservation is None:
            return None
        guest = scratch / "staging"
        guest.mkdir(exist_ok=True)
        return guest, reservation

    def _target_staging(self, project_id: str, item: dict, target: at.ArchiveTarget,
                        blocked: Mapping[str, list[str]]) -> tuple[Path | None, str]:
        """``<target>/staging/<p>/`` -- only when the target's check confirms
        it now and no leftover of an earlier build of this item is on that
        target (followed by label in `blocked`, or at the path an earlier
        receipt reported), else None and why."""
        try:
            check = bounded(target.check, self.timeout, f"check {target.target_id}")
        except TargetTimeout as exc:
            return None, f"{TARGET_TIMEOUT}: {exc}"
        if not check.ok:
            return None, f"the target check failed ({check.reason}); no target staging"
        left = list(blocked.get(target.target_id) or []) + [
            p for p in self._leftovers_present(item, target)
            if p not in (blocked.get(target.target_id) or [])]
        if left:
            return None, (f"{LEFTOVER_PRESENT}: an earlier build of this item left "
                          f"{'; '.join(left)[:400]} on {target.target_id}; it is not "
                          f"built there again while that is present (the user deletes "
                          f"it)")

        def make() -> Path:
            directory = target.root / "staging" / project_id
            for step in (target.root / "staging", directory):
                if not os.path.lexists(step):
                    os.mkdir(step)
                if step.is_symlink() or not step.is_dir():
                    raise OSError(f"{step} is not a plain directory")
            return directory
        try:
            return bounded(make, self.timeout, "staging on the target"), "target"
        except (TargetTimeout, OSError) as exc:
            return None, f"target staging unavailable: {exc}"

    def _follow_leftovers(self, project_id: str, item: dict, record: dict, scratch: Path
                          ) -> dict[str, list[str]]:
        """Revision 9 item 1, on the listing of Revision 10 item 3: every
        leftover an earlier build of this item path left on a registered
        target, found by its label.

        Every candidate build is labelled with its receipt id, so the bundle
        or ``.building`` directory it left -- finished later or not -- is
        named ``...-<receipt-id>`` or ``...-<receipt-id>.building``. The
        labels of `item`'s path are the receipt ids of every earlier receipt
        of that path (of any status, ``RELOCATED`` included) and of every
        build record of it in the journal (Revision 10 item 1), so a build is
        followed even when its pass died or its receipt is lost. The listing
        is `_scan_staging` -- the staging walk, restricted to these labels.
        Each match goes into ``record["target_leftovers"]``; a target whose
        listing cannot be taken goes into
        ``record["target_leftovers_unmeasured"]``. Both block building on that
        target; an item without any label is blocked by nothing. Returns
        ``{target_id: [what blocks building there]}``."""
        record["target_leftovers"] = []
        own = record["receipt_id"]
        labels = {r["receipt_id"] for r in self._receipts()
                  if r.get("path") == item["path"] and isinstance(r.get("receipt_id"), str)
                  and _RECEIPT_ID.fullmatch(r["receipt_id"])}
        labels |= {r["receipt_id"] for r in _build_records(scratch)
                   if r.get("item") == item["path"]}
        labels.discard(own)
        blocked: dict[str, list[str]] = {}
        if not labels:
            return blocked
        scan = self._scan_staging(project_id, lambda: labels)
        for found in scan["found"]:
            record["target_leftovers"].append(found)
            blocked.setdefault(found["target_id"], []).append(found["path"])
        for unmeasured in scan["unmeasured"]:
            record.setdefault("target_leftovers_unmeasured", []).append(unmeasured)
            blocked.setdefault(unmeasured["target_id"], []).append(
                f"{unmeasured['path']} (unmeasurable: {unmeasured['detail']})")
        return blocked

    def target_leftovers(self, project_id: str, journal_dir: Path | str
                         ) -> dict[str, list[dict]]:
        """The pass-level listing of Revision 10 item 3, read-only: for every
        registered target, under the timeout, ``<root>/staging/<project_id>/``
        opened by the staging walk and listed through its descriptor; then
        the labels read -- every receipt id of the project's receipts and of
        the build records in ``<journal_dir>/intents.jsonl`` (a build's
        record precedes its directory, so a name already listed finds its
        label). Returns ``{"found", "unmeasured", "unknown"}``: a labelled
        name is ``found`` (target id, ``STAGING_LEFTOVER``, path, the bytes of
        the regular files the walk reaches below it); any other name is
        ``unknown`` (``UNKNOWN_STAGING_ENTRY``, path only -- never opened,
        sized or followed); a target whose listing cannot be taken (root
        absent, a link or a non-directory on the walk's path, an error, a
        timeout) is ``unmeasured``. Creates, locks and removes nothing; with
        target problems (Revision 10 item 6) it reads no target."""
        journal = Path(journal_dir)

        def labels() -> set[str]:
            out = {r["receipt_id"] for r in self._receipts()
                   if isinstance(r.get("receipt_id"), str)
                   and _RECEIPT_ID.fullmatch(r["receipt_id"])
                   and r.get("project_id", project_id) == project_id}
            return out | {r["receipt_id"] for r in _build_records(journal)}
        return self._scan_staging(project_id, labels)

    def _scan_staging(self, project_id: str, labels: Callable[[], set[str]]
                      ) -> dict[str, list[dict]]:
        """List ``<root>/staging/<project_id>/`` of every registered target by
        the staging walk, then read `labels()` and sort each name into
        ``found`` (its label known), ``unknown`` or -- a listing or a sizing
        that cannot be taken -- ``unmeasured`` (see `target_leftovers`)."""
        out: dict[str, list[dict]] = {"found": [], "unmeasured": [], "unknown": []}
        if self.target_problems:
            for target in self.targets:
                out["unmeasured"].append(
                    {"target_id": target.target_id,
                     "path": os.fspath(Path(target.root) / "staging" / project_id),
                     "detail": f"{TARGET_PROBLEMS}: " + "; ".join(self.target_problems)})
            return out
        listed: list[tuple[at.ArchiveTarget, Path, list[str]]] = []
        for target in self.targets:
            directory = Path(target.root) / "staging" / project_id
            try:
                names = bounded(lambda t=target: _list_staging(t.root, project_id),
                                self.timeout, f"list the staging of {target.target_id}")
            except (TargetTimeout, OSError) as exc:
                out["unmeasured"].append({"target_id": target.target_id,
                                          "path": os.fspath(directory), "detail": str(exc)})
                continue
            listed.append((target, directory, names))
        known = labels() if any(names for _, _, names in listed) else set()
        for target, directory, names in listed:
            for name in names:
                if _label_of(name, known) is None:
                    out["unknown"].append({"target_id": target.target_id,
                                           "status": UNKNOWN_STAGING_ENTRY,
                                           "path": os.fspath(directory / name)})
                    continue
                try:
                    size = bounded(lambda t=target, n=name: _staged_bytes(t.root, project_id, n),
                                   self.timeout, f"size a leftover on {target.target_id}")
                except (TargetTimeout, OSError) as exc:
                    out["unmeasured"].append({"target_id": target.target_id,
                                              "path": os.fspath(directory / name),
                                              "detail": str(exc)})
                    continue
                out["found"].append({"target_id": target.target_id, "status": STAGING_LEFTOVER,
                                     "path": os.fspath(directory / name), "bytes": size})
        return out

    def _target_overlap(self, path: str) -> str:
        """Why the item at `path` overlaps a registered archive target, or ""
        (Revision 9 item 5): the item lies inside a target's root, or a
        target's root lies inside the item -- decided by ``(st_dev, st_ino)``
        ancestry up to ``/`` (across mount boundaries), never by string
        prefix. Each target root is measured under the timeout; one that does
        not exist contains nothing, one that cannot be measured refuses the
        item (``TARGET_UNMEASURABLE``), and so does any problem of
        ``archive-targets.json`` (Revision 10 item 6)."""
        if self.target_problems:
            return (f"{TARGET_UNMEASURABLE}: archive-targets.json: "
                    + "; ".join(self.target_problems))
        try:
            mine = _ancestry(path)
        except OSError as exc:
            return f"{TARGET_UNMEASURABLE}: the ancestry of {path} cannot be read ({exc})"
        for target in self.targets:
            try:
                theirs = bounded(lambda t=target: _ancestry(t.root, follow=True), self.timeout,
                                 f"measure the root of {target.target_id}")
            except FileNotFoundError:
                continue                        # not there (unplugged): contains nothing
            except (TargetTimeout, OSError) as exc:
                return (f"{TARGET_UNMEASURABLE}: the root of target {target.target_id} cannot "
                        f"be measured ({exc}); refused")
            if theirs[0] in mine:
                return (f"{INSIDE_ARCHIVE_TARGET}: {path} lies inside the root of archive "
                        f"target {target.target_id}")
            if mine[0] in theirs:
                return (f"{INSIDE_ARCHIVE_TARGET}: the root of archive target "
                        f"{target.target_id} lies inside {path}")
        return ""

    # -- re-checks --------------------------------------------------------- #

    def _recheck_proof(self, item: dict, path: Path, record: dict,
                       sources: st.EvidenceSources, locks: list[int]) -> str:
        proof = item["proof"]
        kind = item["classification"]
        if item.get("kind") in LEGACY_KINDS:
            bound = self.registry.legacy_child(path)
            if bound is None or bound.get("entries_digest") != item.get("entries_digest"):
                return f"{st.LEGACY_UNBOUND}: {path} is no longer bound by its legacy anchor"
        if kind == st.VERIFIED_DUPLICATE:
            retention = None
            for original in item["originals"]:
                if original["source"] == "retention":
                    if retention is None:
                        try:
                            retention = sources.retention()
                        except st.SourceUnreadable as exc:
                            return f"{RETENTION_MANIFEST_CHANGED}: unreadable ({exc})"
                    now = retention.get(original["path"])
                    if now is None or now.digest != original["digest"]:
                        return f"{RETENTION_MANIFEST_CHANGED}: {original['path']}"
                    problem = self._hold_run(original["path"], locks)
                    if problem:
                        return problem
                    obj = now
                else:
                    try:
                        obj = sources.git_originals().get(original["path"])
                    except st.SourceUnreadable as exc:
                        return f"{DUPLICATE_CHANGED}: originals unreadable ({exc})"
                    if obj is None or obj.digest != original["digest"]:
                        return f"{DUPLICATE_CHANGED}: {original['path']} is no longer the " \
                               f"committed original"
                    retain = record.get("retain_ref") or {}
                    rel = os.path.relpath(original["path"], proof["canonical"])
                    held = st._git(proof["canonical"], "rev-parse", "--verify", "-q",
                                   f"{retain.get('ref')}:{rel}")
                    if held.returncode != 0 or held.stdout.decode().strip() != obj.digest:
                        return f"{REPRODUCTION_REF_MOVED}: the retain ref no longer holds " \
                               f"{rel}"
                problem = st._original_ok(obj, sources, self.registry, item_project(item))
                if problem:
                    return f"{DUPLICATE_CHANGED}: {problem}"
            # The item itself: still the planned bytes, still equal to them.
            return self._item_matches_plan(path, item) or self._still_equal(path, item)
        if proof.get("type") == "clone":
            retain = record.get("retain_ref") or {}
            moved = st._git(proof["canonical"], "rev-parse", "--verify", "-q",
                            str(retain.get("ref")) + "^{commit}")
            if moved.returncode != 0 or moved.stdout.decode().strip() != proof["commit"]:
                return f"{REPRODUCTION_REF_MOVED}: {retain.get('ref')} no longer names " \
                       f"{proof['commit']}"
            fresh, notes = st.clone_proof(path, sources)
            if fresh is None or st.proof_digest(fresh) != item["proof_digest"]:
                return f"{REPRODUCTION_CHANGED}: " + "; ".join(notes)[:300]
            return ""
        if proof.get("type") == "git_tree":
            retain = record.get("retain_ref") or {}
            moved = st._git(proof["canonical"], "rev-parse", "--verify", "-q",
                            str(retain.get("ref")) + "^{commit}")
            if moved.returncode != 0 or moved.stdout.decode().strip() != proof["commit"]:
                return f"{REPRODUCTION_REF_MOVED}: {retain.get('ref')} no longer names " \
                       f"{proof['commit']}"
            try:
                entries = st.inventory(path, self.registry, item_project(item))
            except st._NotOwned as exc:
                return f"{REPRODUCTION_CHANGED}: {exc}"
            fresh, notes = st.git_tree_proof(path, entries, sources)
            if fresh is None or st.proof_digest(fresh) != item["proof_digest"]:
                return f"{REPRODUCTION_CHANGED}: " + "; ".join(notes)[:300]
            return ""
        if proof.get("type") == "extract_root":
            owner = self.registry.owner_of(path)
            try:
                entries = st.inventory(path, self.registry, item_project(item))
            except st._NotOwned as exc:
                return f"{EXTRACT_PROOF_CHANGED}: {exc}"
            fresh, notes = st.extract_root_proof(path, entries, owner, sources)
            if fresh is None or st.proof_digest(fresh) != item["proof_digest"]:
                return f"{EXTRACT_PROOF_CHANGED}: " + "; ".join(notes)[:300]
            return ""
        return f"{CHANGED_PLAN}: unknown proof"

    def _hold_run(self, original: str, locks: list[int]) -> str:
        """Hold the original's run by a shared lock on its ``.process.lock``
        (the probe `RunStore.running_process` takes) across the re-read and
        the removal; a live holder keeps the item, and a run cannot start
        (`RunStore.process_marker` needs the lock exclusively) while it is
        held. The lock file is opened relative to a descriptor of the owned
        run directory with ``O_CREAT | O_NOFOLLOW`` -- never ``O_TRUNC`` -- so
        a run without one is held too; the empty file it creates stays."""
        owner = self.registry.owner_of(original)
        if owner.status != OWNED or owner.root is None:
            return f"{DUPLICATE_CHANGED}: {original} is {owner.status}"
        name = Path(original).relative_to(owner.root).parts[0]
        run_dir = owner.root / name
        _hook("before_hold", original=original, run_dir=run_dir)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            root_fd = os.open(owner.root, flags)
        except OSError as exc:
            return f"{RUN_PROCESS_LIVE}: the runs base cannot be opened ({exc})"
        try:
            run_fd = os.open(name, flags, dir_fd=root_fd)
        except OSError as exc:
            os.close(root_fd)
            return f"{RUN_PROCESS_LIVE}: the run directory {name} cannot be opened ({exc})"
        try:
            fd = os.open(".process.lock", os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o644, dir_fd=run_fd)
        except OSError as exc:
            return f"{RUN_PROCESS_LIVE}: {name}/.process.lock cannot be opened ({exc})"
        finally:
            os.close(run_fd)
            os.close(root_fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return f"{RUN_PROCESS_LIVE}: a live process holds {run_dir.name}"
        locks.append(fd)
        finished, why = st.run_finished(run_dir)
        if not finished:
            return f"{RUN_PROCESS_LIVE}: {why}"
        return ""

    # -- quarantine, restore, removal --------------------------------------- #

    def _restore(self, quarantine: Path, original: Path, record: dict, reason: str) -> None:
        """Give the quarantined item back its name without replacing anything;
        if the name was taken meanwhile, a fresh versioned name instead."""
        target = original
        try:
            ab._rename_noreplace(quarantine, target)
        except FileExistsError:
            target = _versioned(original)
            ab._rename_noreplace(quarantine, target)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            target = _versioned(original)
            ab._rename_noreplace(quarantine, target)
        ab._fsync_dir(original.parent)
        record.update(status=KEPT, reason=reason, restored_to=os.fspath(target))

    def _remove(self, project_id: str, item: dict, original: Path, quarantine: Path,
                record: dict, walked: tuple[st.Entry, ...], inodes: dict[str, tuple[int, int]],
                scratch: Path) -> None:
        """Remove the entries of the one compared walk of the quarantine,
        nothing else (Revision 8 item 1).

        The registry's resolved path is opened without following links and
        its ``st_dev``/``st_ino`` compared with the item's on that descriptor;
        every unlink and rmdir then acts relative to descriptors opened from
        it, each directory ``fstat``-ed once opened and each file
        ``fstatat``-ed right before its unlink against what the walk
        recorded. A stop follows the restore rule (`_stopped`)."""
        owner = self.registry.owner_of(quarantine)
        if owner.status != OWNED or owner.project_id != project_id or owner.resolved is None:
            return self._restore(quarantine, original, record,
                                 f"{OWNERSHIP_CHANGED}: {owner.status} {owner.reason}")
        try:
            fd = os.open(owner.resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                         | os.O_CLOEXEC)
        except OSError as exc:
            record.update(status=KEPT, reason=f"{INODE_CHANGED}: {exc}", needs_you=True)
            return None
        late: list[str] = []
        stop = ""
        record["removed_entries"] = 0
        try:
            now = os.fstat(fd)
            planned = (item["st_dev"], item["st_ino"])
            if (now.st_dev, now.st_ino) != planned or (owner.st_dev, owner.st_ino) != planned \
                    or inodes.get("") != planned:
                record.update(status=KEPT, needs_you=True,
                              reason=f"{INODE_CHANGED}: {quarantine} is no longer the item "
                                     f"that was planned; nothing removed")
                return None
            tree = _removal_tree(walked, inodes)
            _hook("before_unlink", item=item, quarantine=quarantine, record=record)
            try:
                _append_intent(scratch, {"schema": INTENT_SCHEMA, "item": item["path"],
                                         "receipt_id": record["receipt_id"],
                                         "removal_started": True,
                                         "quarantine_name": os.fspath(quarantine),
                                         "written_at": _iso()})
            except OSError as exc:
                stop = f"{INTENT_FAILED}: removal_started could not be written durably ({exc})"
            else:
                record["removal_started"] = True
                try:
                    self._remove_tree(fd, tree, late, "", record)
                except _Stop as exc:
                    stop = str(exc)
                except OSError as exc:
                    stop = f"removal stopped ({exc})"
        finally:
            os.close(fd)
        if stop:
            return self._stopped(quarantine, original, record, stop)
        parent = os.open(quarantine.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            name = os.fsencode(quarantine.name)
            try:
                again = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (again.st_dev, again.st_ino) != inodes.get(""):
                    stop = f"{ENTRY_SWAPPED}: {quarantine} is no longer the walked item"
                else:
                    _rmdir_entry(name, parent)
                    record["removed_entries"] += 1
            except OSError as exc:
                if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    late.append(".")
                else:
                    stop = f"removal stopped at the item itself ({exc})"
        finally:
            os.close(parent)
        if stop:
            return self._stopped(quarantine, original, record, stop)
        if late:
            problem = _remainder_problem(quarantine, walked)
            if problem:
                return self._stopped(quarantine, original, record,
                                     f"late entries ({', '.join(late)}), but the remainder "
                                     f"is not only new: {problem}")
            self._restore(quarantine, original, record,
                          f"{LATE_ENTRY_RESTORED}: entries appeared after the inventory "
                          f"({', '.join(late)}); every walked non-directory was removed and "
                          f"the remainder holds only entries the inventory did not contain; "
                          f"it keeps the original name")
            record.update(status=LATE_ENTRY_RESTORED, needs_you=True)
            return None
        _hook("after_removal", item=item, quarantine=quarantine, record=record)
        absent = not os.path.lexists(quarantine) and not os.path.lexists(original)
        record["post_delete_absence"] = absent
        if absent:
            record.update(status=RELOCATED,
                          reason="relocated: verified bundle in reclaim-review, local copy "
                                 "removed")
        else:
            record.update(status=PARTIAL, needs_you=True,
                          reason=f"{PARTIAL}: after the removal the item path or the "
                                 f"quarantine name is present again")
        return None

    def _stopped(self, quarantine: Path, original: Path, record: dict, reason: str) -> None:
        """The restore rule of Revision 8: nothing removed yet -> `KEPT` with
        `reason`, restored from the quarantine name (never replacing);
        anything removed -> `PARTIAL`, the quarantine name kept, NEEDS_YOU."""
        if not record.get("removed_entries"):
            self._restore(quarantine, original, record, reason)
            record["needs_you"] = True
            return None
        record.update(status=PARTIAL, needs_you=True,
                      reason=f"{PARTIAL}: {reason}; {record['removed_entries']} entries were "
                             f"removed, the remainder keeps its quarantine name -- recover it "
                             f"by re-extracting the verified bundle")
        return None

    def _remove_tree(self, dir_fd: int, tree: dict, late: list[str], prefix: str,
                     record: dict) -> None:
        for name in sorted(tree):
            node = tree[name]
            rel = prefix + name
            raw = os.fsencode(name)
            if node["type"] == "file":
                self._recheck_file(dir_fd, raw, rel, node, record)
                _unlink_entry(raw, dir_fd)
                record["removed_entries"] += 1
                continue
            if node["type"] != "dir":
                try:
                    now = os.stat(raw, dir_fd=dir_fd, follow_symlinks=False)
                except OSError as exc:
                    raise _Stop(f"{ENTRY_SWAPPED}: {rel} cannot be checked ({exc})") from exc
                if (now.st_dev, now.st_ino) != node["inode"]:
                    raise _Stop(f"{ENTRY_SWAPPED}: {rel} is not the file the walk recorded")
                _unlink_entry(raw, dir_fd)
                record["removed_entries"] += 1
                continue
            try:
                child = os.open(raw, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                | os.O_CLOEXEC, dir_fd=dir_fd)
            except OSError as exc:
                raise _Stop(f"{ENTRY_SWAPPED}: {rel} cannot be opened as the walked "
                            f"directory ({exc})") from exc
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != node["inode"]:
                    raise _Stop(f"{ENTRY_SWAPPED}: {rel} is not the directory the walk "
                                f"recorded")
                self._remove_tree(child, node["children"], late, rel + "/", record)
            finally:
                os.close(child)
            try:
                again = os.stat(raw, dir_fd=dir_fd, follow_symlinks=False)
            except OSError as exc:
                raise _Stop(f"{ENTRY_SWAPPED}: {rel} vanished ({exc})") from exc
            if (again.st_dev, again.st_ino) != node["inode"]:
                raise _Stop(f"{ENTRY_SWAPPED}: {rel} was swapped while it was emptied")
            try:
                _rmdir_entry(raw, dir_fd)
                record["removed_entries"] += 1
            except OSError as exc:
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
                late.append(rel)

    @staticmethod
    def _recheck_file(dir_fd: int, raw: bytes, rel: str, node: dict, record: dict) -> None:
        """Immediately before a regular file is unlinked (Revision 9 item 3):
        open it ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK`` relative to its
        directory, compare the descriptor's ``st_dev``/``st_ino`` with the
        walk's, hash the descriptor's content against the inventory's
        SHA-256 (bytes written in place after the compare), then
        ``fstatat(..., AT_SYMLINK_NOFOLLOW)`` the name against the
        descriptor's inode. Any difference raises `_Stop`."""
        try:
            fd = os.open(raw, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=dir_fd)
        except OSError as exc:
            raise _Stop(f"{ENTRY_SWAPPED}: {rel} cannot be opened as the walked file "
                        f"({exc})") from exc
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != node["inode"] \
                    or not stat.S_ISREG(opened.st_mode):
                raise _Stop(f"{ENTRY_SWAPPED}: {rel} is not the file the walk recorded")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
            if digest.hexdigest() != node.get("sha256"):
                raise _Stop(f"{ITEM_CHANGED}: {rel} was written in place after the compare; "
                            f"its bytes are not the inventory's")
        finally:
            os.close(fd)
        _hook("after_rehash", rel=rel, record=record)
        try:
            now = os.stat(raw, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise _Stop(f"{ENTRY_SWAPPED}: {rel} cannot be checked ({exc})") from exc
        if (now.st_dev, now.st_ino) != (opened.st_dev, opened.st_ino):
            raise _Stop(f"{ENTRY_SWAPPED}: {rel} was replaced after it was re-hashed")

    # -- interrupted executes ---------------------------------------------- #

    def recover(self, project_id: str) -> list[dict]:
        """Resolve every intent an interrupted execute of `project_id` left
        without a completion (see the module docstring) -- renaming and
        removing nothing. Takes the project's relocation session and the
        flock on ``relocation.lock`` and releases both before returning, so
        the pass's own execute can take them again. Held elsewhere: one
        ``BUSY`` entry, nothing judged (an execute in flight is never judged
        interrupted). Refused by anything else -- an error writing the
        session's lease, opening or locking ``relocation.lock`` -- one
        ``FAILED`` entry with NEEDS_YOU (Revision 11 item 1). A write that
        fails while one intent is resolved marks that intent
        ``RECOVERY_FAILED`` (NEEDS_YOU); the others are still judged."""
        opened_here = (project_id, RELOCATION_SESSION) not in self.coordinator._sessions
        try:
            lease = self.coordinator.acquire_session(project_id, RELOCATION_SESSION)
        except Exception as exc:                # noqa: BLE001 - BUSY or FAILED, said
            if _live_holder(exc):
                return [{"status": BUSY, "needs_you": False,
                         "detail": f"the relocation session is held by a live other "
                                   f"holder: {exc}"}]
            return [{"status": FAILED, "needs_you": True,
                     "detail": f"the relocation session could not be opened: "
                               f"{type(exc).__name__}: {exc}"}]
        fd = None
        try:
            try:
                scratch = self.registry.scratch_dir(project_id, RELOCATION_SESSION, "scratch")
                fd = os.open(scratch / LOCK_NAME,
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
            except Exception as exc:            # noqa: BLE001 - FAILED, said
                return [{"status": FAILED, "needs_you": True,
                         "detail": f"{LOCK_NAME} could not be opened: "
                                   f"{type(exc).__name__}: {exc}"}]
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if _flock_busy(exc):
                    return [{"status": BUSY, "needs_you": False,
                             "detail": f"{LOCK_NAME} is held: {exc}"}]
                return [{"status": FAILED, "needs_you": True,
                         "detail": f"{LOCK_NAME} could not be locked: "
                                   f"{type(exc).__name__}: {exc}"}]
            if self.target_problems:            # a target may be unseen: judge nothing
                problems = "; ".join(self.target_problems)
                return [{"status": UNRESOLVED, "receipt_id": str(intent.get("receipt_id")),
                         "item": str(intent.get("item")), "needs_you": True,
                         "detail": f"{TARGET_PROBLEMS}: archive-targets.json: {problems}; "
                                   f"nothing read on any target, judged again next pass"}
                        for intent in _open_intents(scratch)]
            outcomes = []
            for intent in _open_intents(scratch):
                try:
                    outcomes.append(self._resolve_intent(scratch, intent))
                except Exception as exc:        # noqa: BLE001 - this intent only, said
                    outcomes.append({
                        "status": RECOVERY_FAILED, "receipt_id": str(intent.get("receipt_id")),
                        "item": str(intent.get("item")), "needs_you": True,
                        "detail": f"{RECOVERY_FAILED}: resolving this intent failed "
                                  f"({type(exc).__name__}: {exc}); its receipt or completion "
                                  f"is not written, judged again on the next pass"})
            return outcomes
        finally:
            if fd is not None:
                os.close(fd)
            if opened_here:
                try:
                    self.coordinator.close_session(lease)
                except Exception:               # superseded meanwhile; nothing held
                    pass

    def _resolve_intent(self, scratch: Path, intent: dict) -> dict:
        receipt_id = str(intent.get("receipt_id"))
        if not _RECEIPT_ID.fullmatch(receipt_id):
            # Never a path from an id this module did not generate.
            return {"status": UNRESOLVED, "receipt_id": receipt_id,
                    "item": str(intent.get("item")), "needs_you": True,
                    "detail": f"{UNRESOLVED}: the intent's receipt id {receipt_id[:80]!r} is not "
                              f"one this relocation generates; nothing read"}
        item = Path(str(intent.get("item")))
        quarantine = Path(str(intent.get("quarantine_name")))
        receipt_path = self.receipts_dir / f"reclaim-{receipt_id}.json"
        if os.path.lexists(receipt_path):       # written; only the completion was lost
            raw, problem = _read_receipt(receipt_path)
            if problem:                         # never parsed unguarded, never raised
                return {"status": UNRESOLVED, "receipt_id": receipt_id, "item": os.fspath(item),
                        "needs_you": True,
                        "detail": f"{UNRESOLVED}: the receipt {receipt_path} cannot be used "
                                  f"({problem}); nothing written, judged again next pass"}
            outcome = {"status": json.loads(raw).get("status"), "receipt_id": receipt_id,
                       "item": os.fspath(item), "detail": "receipt already written"}
            self._complete(scratch, intent, raw)
            return outcome
        item_there = os.path.lexists(item)
        quarantine_there = os.path.lexists(quarantine)
        record = {
            "schema": RECEIPT_SCHEMA, "receipt_id": receipt_id,
            "plan_digest": intent.get("plan_digest"), "plan_path": intent.get("plan_path"),
            "project_id": intent.get("project_id"), "path": os.fspath(item),
            "root": intent.get("root"), "classification": intent.get("classification"),
            "proof_digest": intent.get("proof_digest"),
            "retained_originals": intent.get("retained_originals") or [],
            "bytes": intent.get("bytes") or 0,
            "bundle_path": intent.get("bundle_path"),
            "bundle_receipt_digest": intent.get("receipt_digest"),
            "target_id": intent.get("target_id"),
            "quarantine_path": os.fspath(quarantine),
            "recovered": True, "intent_written_at": intent.get("written_at"),
            "post_delete_absence": False, "needs_you": False, "time_utc": _iso(),
        }
        if not item_there and not quarantine_there:
            verified, why = self._bundle_receipt_holds(intent)
            if not verified:
                return {"status": UNRESOLVED, "receipt_id": receipt_id, "item": os.fspath(item),
                        "needs_you": True,
                        "detail": f"{UNRESOLVED}: the item and its quarantine are gone but the "
                                  f"bundle receipt does not verify ({why}); judged again on "
                                  f"the next pass"}
            record.update(status=RELOCATED, post_delete_absence=True,
                          reason="relocated (recovered from the intent): the item and its "
                                 "quarantine are gone and the bundle receipt verifies")
        elif quarantine_there or intent.get("_removal_started"):
            record.update(status=PARTIAL, needs_you=True,
                          reason=f"{PARTIAL}: interrupted "
                                 + ("after the removal started" if intent.get("_removal_started")
                                    else "in quarantine")
                                 + "; nothing renamed or removed by the recovery -- recover by "
                                   "re-extracting the verified bundle")
        else:
            record.update(status=KEPT,
                          reason=f"{INTERRUPTED}: the execute stopped before the quarantine "
                                 f"rename; the item is where it was")
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=True,
                         default=str).encode("ascii")
        write_new(receipt_path, raw)
        self._complete(scratch, intent, raw)
        return {"status": record["status"], "receipt_id": receipt_id, "item": os.fspath(item),
                "needs_you": record["needs_you"], "detail": record["reason"],
                "receipt_path": os.fspath(receipt_path)}

    @staticmethod
    def _complete(scratch: Path, intent: dict, raw: bytes) -> None:
        _append_intent(scratch, {"schema": INTENT_SCHEMA, "item": intent.get("item"),
                                 "receipt_id": intent.get("receipt_id"),
                                 "completed": hashlib.sha256(raw).hexdigest(),
                                 "recovered": True, "written_at": _iso()})

    def _bundle_receipt_holds(self, intent: dict) -> tuple[bool, str]:
        """Whether ``<bundle_path>/ARCHIVE_RECEIPT.json`` is still there with
        the intent's digest, on a target whose identity is confirmed -- read
        under the timeout; no extraction, no full verification.

        The receipt is read only inside a registered target (Revision 9
        item 6): `bundle_path` must lie strictly below the root of the
        intent's ``target_id``; with ``target_id`` None (an extract root)
        below the root of its ``containing_target_id``, or -- an intent
        written before that field -- below exactly one registered target's
        root. Containment is decided by path components against the
        configured root or the root descriptor's own path (Revision 10 item
        4); then every remaining component is opened by the staging walk from
        the root's descriptor and the receipt is opened ``O_NOFOLLOW`` from
        the bundle's descriptor and hashed from it. No path below the root is
        resolved or reopened by name; a link or a non-directory on the way
        refuses the read."""
        bundle = intent.get("bundle_path")
        digest = intent.get("receipt_digest")
        if not isinstance(bundle, str) or not isinstance(digest, str):
            return False, "the intent names no bundle receipt"
        if intent.get("target_id") is not None:
            wanted = intent.get("target_id")
        elif "containing_target_id" in intent:
            wanted = intent.get("containing_target_id")
        else:
            wanted = None
        try:
            if wanted is not None:
                target = next((t for t in self.targets if t.target_id == wanted), None)
                if target is None:
                    return False, f"the intent's target {wanted!r} is not a registered target"
                rel = self._containment(bundle, target)
                if rel is None:
                    return False, (f"the bundle {bundle} does not lie under the root of "
                                   f"target {target.target_id}; nothing read")
            else:
                if "containing_target_id" in intent:
                    return False, "the intent names no containing target; nothing read"
                found = [(t, r) for t in self.targets
                         for r in [self._containment(bundle, t)] if r is not None]
                if len(found) != 1:
                    return False, (f"the bundle {bundle} lies under {len(found)} registered "
                                   f"targets, not exactly one; nothing read")
                target, rel = found[0]
            check = bounded(target.check, self.timeout, f"check {target.target_id}")
            if not check.identity_ok:
                return False, f"target {target.target_id} unconfirmed: {check.detail}"
            _hook("before_receipt_open", intent=intent, target=target, bundle=bundle)
            now = bounded(lambda: _receipt_digest_below(target.root, rel), self.timeout,
                          "read the bundle receipt")
        except (TargetTimeout, OSError, UnsafeEntry) as exc:
            code = errno.errorcode.get(getattr(exc, "errno", None) or -1, type(exc).__name__)
            return False, f"refused ({code}): {exc}"
        if now != digest:
            return False, "the bundle receipt's digest differs"
        return True, ""

    def _containment(self, bundle: str, target: at.ArchiveTarget) -> list[str] | None:
        """The components of `bundle` below `target`'s root when it lies
        strictly below the configured root or below the path of the root's
        own descriptor (``/proc/self/fd/<n>``), compared by path components;
        else None. The root is opened under the timeout (`TargetTimeout`,
        `OSError` -- ``FileNotFoundError`` when it does not exist -- raised);
        nothing below it is touched."""
        return bounded(lambda: _components_below(bundle, target.root), self.timeout,
                       f"open the root of {target.target_id}")

    def _containing_target(self, bundle: Any
                           ) -> tuple[at.ArchiveTarget | None, list[str] | None, str]:
        """The one registered target whose root holds `bundle` (Revision 10
        item 5), with the bundle's components below it -- or None and why:
        ``TARGET_UNMEASURABLE`` when a target root cannot be measured in time
        or fails with an error other than ``ENOENT`` (an absent root contains
        nothing), ``NO_CONTAINING_TARGET`` when none or several hold it."""
        if not isinstance(bundle, str):
            return None, None, f"{NO_CONTAINING_TARGET}: the proof names no bundle location"
        found = []
        for target in self.targets:
            try:
                rel = self._containment(bundle, target)
            except FileNotFoundError:
                continue                        # not there: contains nothing
            except (TargetTimeout, OSError) as exc:
                return None, None, (f"{TARGET_UNMEASURABLE}: the root of target "
                                    f"{target.target_id} cannot be measured ({exc}); kept")
            if rel is not None:
                found.append((target, rel))
        if len(found) != 1:
            return None, None, (f"{NO_CONTAINING_TARGET}: the bundle {bundle} lies under "
                                f"{len(found)} registered targets, not exactly one; kept")
        return found[0][0], found[0][1], ""

    def _extract_gate(self, proof: dict) -> tuple[str, str | None]:
        """Before an extract root's intent (Revision 10 item 5, 10a item 6):
        the one containing target, its identity read through the root
        descriptor the staging walk opened (the probe gets
        ``/proc/self/fd/<n>``) and compared with ``expected_identity`` as
        `ArchiveTarget.check` does, and the bundle's ``TREE_PAYLOAD``
        re-hashed through the staging walk against the proof's
        ``payload_sha256``. Returns ("", containing target id) or (why the
        item is kept, None)."""
        target, rel, why = self._containing_target(proof.get("bundle_location"))
        if target is None:
            return why, None
        try:
            identity, payload, problem = bounded(
                lambda: _identity_and_payload(target, rel), self.timeout,
                f"re-hash the payload on {target.target_id}")
        except TargetTimeout as exc:
            return f"{TARGET_TIMEOUT}: {exc}", None
        except OSError as exc:
            return (f"{TARGET_UNCONFIRMED}: the root of {target.target_id} cannot be "
                    f"opened ({exc})"), None
        if not isinstance(identity, Mapping) or not all(
                key in identity and identity[key] == value
                for key, value in target.expected_identity.items()):
            return (f"{TARGET_UNCONFIRMED}: target {target.target_id}'s identity read through "
                    f"its root descriptor is not the expected one{problem and ': ' + problem}"
                    ), None
        if payload is None or payload != proof.get("payload_sha256"):
            return (f"{EXTRACT_PROOF_CHANGED}: the payload on {target.target_id} "
                    + (f"cannot be re-hashed by the staging walk ({problem})" if payload is None
                       else "no longer has the proof's SHA-256")), None
        return "", target.target_id

    # -- reclaim-review bookkeeping ---------------------------------------- #

    def user_reclaimed(self, target: at.ArchiveTarget, project_id: str) -> list[dict]:
        """Judge every relocated bundle of `project_id` on `target` that is no
        longer in reclaim-review: ``USER_RECLAIMED_ARCHIVE`` -- the user's
        deletion, not corruption -- or, if its item has meanwhile become
        referenced, ``PROMOTION_REQUIRED`` with the item classified `HOT`.
        Nothing is judged while the target's identity is not confirmed (a
        wrong disk is not a deletion). Each judgement is appended once to
        ``<receipts_dir>/user-reclaimed.jsonl``. While ``archive-targets.json``
        has problems nothing is read on any target and nothing judged: one
        ``UNRESOLVED`` entry (``TARGET_PROBLEMS``) says so."""
        if self.target_problems:
            return [{"status": UNRESOLVED, "target_id": target.target_id, "needs_you": True,
                     "reason": f"{TARGET_PROBLEMS}: archive-targets.json: "
                               + "; ".join(self.target_problems)}]
        try:
            check = bounded(target.check, self.timeout, f"check {target.target_id}")
        except TargetTimeout as exc:
            return [{"status": TARGET_UNCONFIRMED, "target_id": target.target_id,
                     "detail": str(exc)}]
        if not check.identity_ok:
            return [{"status": TARGET_UNCONFIRMED, "target_id": target.target_id,
                     "detail": check.detail}]
        log = self.receipts_dir / USER_RECLAIMED_LOG
        seen = {(r.get("bundle_path"), r.get("status")) for r in _read_lines(log)}
        out = []
        sources = self._sources(project_id)
        for receipt in self._receipts():
            if receipt.get("project_id") != project_id or receipt.get("status") != RELOCATED \
                    or receipt.get("target_id") != target.target_id:
                continue
            bundle = receipt.get("bundle_path")
            if not isinstance(bundle, str):
                continue
            present = bounded(lambda b=bundle: os.path.lexists(b), self.timeout,
                              "look into reclaim-review")
            if present:
                continue
            if receipt.get("classification") not in st.ELIGIBLE:
                status, tier, reasons = NEEDS_YOU, None, ["not an eligible classification"]
            else:
                reasons = st.reference_reasons(receipt["path"], root=Path(receipt["root"]),
                                               sources=sources)
                status = PROMOTION_REQUIRED if reasons else USER_RECLAIMED_ARCHIVE
                tier = st.HOT if reasons else None
            entry = {"status": status, "bundle_path": bundle, "bundle_id": receipt.get("bundle_id"),
                     "item_path": receipt["path"], "classification": receipt.get("classification"),
                     "receipt": receipt["_file"], "tier": tier, "reasons": reasons,
                     "target_id": target.target_id, "time_utc": _iso()}
            if (bundle, status) not in seen:
                self.receipts_dir.mkdir(parents=True, exist_ok=True)
                _append_line(log, entry)
            out.append(entry)
        return out


def item_project(item: dict) -> str:
    """The project a plan item belongs to, as classified when planned."""
    return item["project_id"]


def _tree_digest(root: Path) -> str | None:
    from hoh import rehydration
    return rehydration.tree_content_digest(root)


def _tree_bytes(path: Path) -> int:
    """Bytes of the regular files below `path` (a staged bundle), links not
    followed."""
    total = 0
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            return os.lstat(path).st_size
    except OSError:
        return 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def _ancestry(path: str | os.PathLike[str], *, follow: bool = False
              ) -> list[tuple[int, int]]:
    """``(st_dev, st_ino)`` of the directory `path` and of every parent up to
    ``/``, walked by ``..`` from descriptors (so across mount boundaries and
    never by string). `follow`: a final symlink is followed (a target root
    is where its data lands); otherwise it is refused."""
    flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_CLOEXEC
    fd = os.open(path, flags if follow else flags | os.O_NOFOLLOW)
    out: list[tuple[int, int]] = []
    try:
        here = os.fstat(fd)
        out.append((here.st_dev, here.st_ino))
        while len(out) < 4096:
            parent = os.open("..", flags, dir_fd=fd)
            os.close(fd)
            fd = parent
            up = os.fstat(fd)
            if (up.st_dev, up.st_ino) == out[-1]:
                return out
            out.append((up.st_dev, up.st_ino))
        raise OSError(errno.ELOOP, "the ancestry does not end at /", os.fspath(path))
    finally:
        os.close(fd)


# -- the staging walk (Revision 10, 10a item 2) ------------------------------ #

_WALK_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_WALK_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _open_target_root(root: str | os.PathLike[str]) -> int:
    """The staging walk's first step: the configured target root opened
    ``O_RDONLY | O_DIRECTORY`` (links in the configured root path are
    followed, as the target's data lands there)."""
    return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def _walk_open(dir_fd: int, name: str, *, directory: bool) -> int:
    """One step below a target root: `name` is examined with
    ``fstatat(dir_fd, name, AT_SYMLINK_NOFOLLOW)``; only a directory
    (`directory`) or a regular file is opened -- ``openat`` with
    ``O_NOFOLLOW`` (plus ``O_DIRECTORY``, or ``O_NONBLOCK`` for a file) --
    and the descriptor's inode must be the one examined. A link, a device, a
    FIFO or any other type raises `OSError` naming the component; nothing is
    resolved, stat-ed or opened by path."""
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    want = stat.S_ISDIR if directory else stat.S_ISREG
    if not want(info.st_mode):
        if stat.S_ISLNK(info.st_mode):
            raise OSError(errno.ELOOP, "a symbolic link on the staging walk; not followed", name)
        raise OSError(errno.ENOTDIR if directory else errno.EINVAL,
                      f"not a {'directory' if directory else 'regular file'} on the staging "
                      f"walk; not opened", name)
    fd = os.open(name, _WALK_DIR if directory else _WALK_FILE, dir_fd=dir_fd)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) \
                or not want(opened.st_mode):
            raise OSError(errno.ESTALE, "changed while the staging walk opened it", name)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _walk_dirs(root_fd: int, parts: Iterable[str]) -> int:
    """The descriptor of the directory `parts` below `root_fd`, opened step by
    step by `_walk_open`; the caller closes it (never `root_fd` itself)."""
    fd = root_fd
    try:
        for part in parts:
            nxt = _walk_open(fd, part, directory=True)
            if fd != root_fd:
                os.close(fd)
            fd = nxt
    except BaseException:
        if fd != root_fd:
            os.close(fd)
        raise
    return os.dup(fd) if fd == root_fd else fd


def _walk_bytes(dir_fd: int) -> int:
    """Bytes of the regular files reached by the staging walk below `dir_fd`:
    each entry examined with ``fstatat(AT_SYMLINK_NOFOLLOW)``, directories
    descended through `_walk_open`; links and special files count 0 and are
    never followed or opened."""
    total = 0
    for name in os.listdir(dir_fd):
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
        elif stat.S_ISDIR(info.st_mode):
            child = _walk_open(dir_fd, name, directory=True)
            try:
                total += _walk_bytes(child)
            finally:
                os.close(child)
    return total


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _list_staging(root: str | os.PathLike[str], project_id: str) -> list[str]:
    """The names in ``<root>/staging/<project_id>/``, through the staging walk
    and listed by descriptor. The root absent raises `FileNotFoundError`;
    ``staging`` or ``<project_id>`` absent means no names; a link, a
    non-directory or any other error on the way raises."""
    root_fd = _open_target_root(root)
    try:
        fd = root_fd
        try:
            for part in ("staging", project_id):
                try:
                    nxt = _walk_open(fd, part, directory=True)
                except FileNotFoundError:
                    return []
                if fd != root_fd:
                    os.close(fd)
                fd = nxt
            return sorted(os.listdir(fd))
        finally:
            if fd != root_fd:
                os.close(fd)
    finally:
        os.close(root_fd)


def _staged_bytes(root: str | os.PathLike[str], project_id: str, name: str) -> int:
    """Bytes of the labelled entry ``<root>/staging/<project_id>/<name>``,
    reached by the staging walk; anything but a directory on that path
    raises (the target is then unmeasured)."""
    root_fd = _open_target_root(root)
    try:
        fd = _walk_dirs(root_fd, ("staging", project_id, name))
        try:
            return _walk_bytes(fd)
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)


_LABELLED = re.compile(r"-(?P<label>rr-\d{8}t\d{6}z-[0-9a-f]{8})(?:\.building)?\Z")


def _label_of(name: str, labels: set[str]) -> str | None:
    """The known receipt-id label a staging name ends with (``-<label>`` or
    ``-<label>.building``), or None."""
    match = _LABELLED.search(name)
    if match and match.group("label") in labels:
        return match.group("label")
    return None


def _build_records(journal_dir: Path | str) -> list[dict]:
    """The build records of ``<journal_dir>/intents.jsonl`` (Revision 10 item
    1) whose receipt id has the form this module generates."""
    return [r for r in _read_lines(Path(journal_dir) / INTENTS_NAME)
            if r.get("schema") == INTENT_SCHEMA and r.get("build_started") is True
            and isinstance(r.get("receipt_id"), str) and _RECEIPT_ID.fullmatch(r["receipt_id"])]


def _versioned(path: Path) -> Path:
    n = 1
    while True:
        candidate = path.with_name(f"{path.name}.v{n}.{_stamp()}")
        if not os.path.lexists(candidate):
            return candidate
        n += 1


class _BuildTimedOut(TargetTimeout):
    """A build on the target that did not finish in time; it was cancelled
    and waited for once more. `still_writing`: it had not stopped then."""

    def __init__(self, message: str, *, still_writing: bool) -> None:
        super().__init__(message)
        self.still_writing = still_writing


def _read_receipt(path: Path) -> tuple[bytes, str]:
    """The bytes of a ReclaimReceipt file and "" -- or why it cannot be used
    (unreadable, not JSON, not a ``RECEIPT_SCHEMA`` mapping)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            raw = b""
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                raw += chunk
        finally:
            os.close(fd)
    except OSError as exc:
        return b"", f"unreadable: {exc}"
    try:
        record = json.loads(raw)
    except ValueError as exc:
        return raw, f"not JSON: {exc}"
    if not isinstance(record, dict) or record.get("schema") != RECEIPT_SCHEMA:
        return raw, f"not a {RECEIPT_SCHEMA} mapping"
    return raw, ""


def _components_below(bundle: str, root: str | os.PathLike[str]) -> list[str] | None:
    """Open `root` (the staging walk's first step) and return the components
    of `bundle` (made absolute, never resolved) strictly below the configured
    root or below the root descriptor's own path, compared by path
    components; None when it lies below neither."""
    root_fd = _open_target_root(root)
    try:
        opened = os.readlink(f"/proc/self/fd/{root_fd}")
    finally:
        os.close(root_fd)
    parts = Path(os.path.abspath(bundle)).parts
    for base in (os.path.abspath(root), opened):
        prefix = Path(base).parts
        if len(parts) > len(prefix) and parts[:len(prefix)] == prefix:
            return list(parts[len(prefix):])
    return None


def _receipt_digest_below(root: str | os.PathLike[str], rel: Iterable[str]) -> str:
    """SHA-256 of ``<root>/<rel...>/ARCHIVE_RECEIPT.json`` read through the
    staging walk: every component checked by `_safe_entry_path` and opened
    from its parent's descriptor without following links, the receipt opened
    ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK`` from the bundle's descriptor and
    hashed from that descriptor (`_hash_fd`)."""
    parts = [_safe_entry_path(part) for part in rel]
    root_fd = _open_target_root(root)
    try:
        fd = _walk_dirs(root_fd, parts)
        try:
            receipt = _walk_open(fd, ab.RECEIPT, directory=False)
            try:
                return _hash_fd(receipt)
            finally:
                os.close(receipt)
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)


def _identity_and_payload(target: at.ArchiveTarget, rel: Iterable[str]
                          ) -> tuple[Any, str | None, str]:
    """One staging walk from `target`'s root: its identity probed through
    that root descriptor (``/proc/self/fd/<n>``) and the bundle's
    ``TREE_PAYLOAD`` re-hashed from it. Returns (identity or None, payload
    SHA-256 or None, the problem); the payload is not read when the identity
    cannot be read."""
    root_fd = _open_target_root(target.root)
    try:
        try:
            identity = target.identity_probe(Path(f"/proc/self/fd/{root_fd}"))
        except Exception as exc:                # a probe that fails proves nothing
            return None, None, f"identity probe failed: {type(exc).__name__}: {exc}"
        if not isinstance(identity, Mapping):
            return None, None, "identity unreadable"
        try:
            fd = _walk_dirs(root_fd, [_safe_entry_path(part) for part in rel])
            try:
                payload = _walk_open(fd, ab.TREE_PAYLOAD, directory=False)
                try:
                    return identity, _hash_fd(payload), ""
                finally:
                    os.close(payload)
            finally:
                os.close(fd)
        except (OSError, UnsafeEntry) as exc:
            return identity, None, str(exc)
    finally:
        os.close(root_fd)


def _leftover_present(path: str) -> bool:
    """Whether a reported staged leftover is still there: absent only when
    ``lstat`` says it does not exist; any other error is raised (the caller
    counts an unmeasurable leftover as present)."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


class UnsafeEntry(ValueError):
    """An entry path `_safe_entry_path` refuses."""


class _Stop(Exception):
    """A removal stops at an entry; the message is the reason."""


def _safe_entry_path(rel: str) -> str:
    """`rel` itself when it is a safe entry path of an item -- relative,
    non-empty, no empty, ``.`` or ``..`` component, no NUL -- else
    `UnsafeEntry`. Applied to every entry before anything is opened."""
    if not isinstance(rel, str) or not rel:
        raise UnsafeEntry(f"empty or not a text path: {rel!r}")
    if "\0" in rel:
        raise UnsafeEntry(f"NUL in {rel!r}")
    if rel.startswith("/"):
        raise UnsafeEntry(f"absolute: {rel!r}")
    for part in rel.split("/"):
        if part in ("", ".", ".."):
            raise UnsafeEntry(f"component {part!r} in {rel!r}")
    return rel


def _removal_tree(walked: Iterable[st.Entry], inodes: Mapping[str, tuple[int, int]]) -> dict:
    """The walked entries as a nested ``{name: {"type", "inode", "children"}}``
    -- each entry with the ``(st_dev, st_ino)`` the same walk recorded."""
    tree: dict = {}
    for entry in sorted(walked, key=lambda e: e.rel):
        rel = _safe_entry_path(entry.rel)
        parts = rel.split("/")
        node = tree
        for part in parts[:-1]:
            node = node[part]["children"]
        node[parts[-1]] = {"type": entry.type, "inode": inodes[rel], "children": {},
                           "sha256": entry.sha256}
    return tree


def _remainder_problem(quarantine: Path, walked: Iterable[st.Entry]) -> str:
    """Why what is left of a quarantine after its removal is not "only new"
    (Revision 9 item 4), or "": every walked non-directory entry must be gone,
    and every walked directory still present may hold -- at any depth --
    only entries the inventory did not contain (and walked directories
    leading to them). Read-only; it never feeds a removal. A directory that
    cannot be listed is a problem (fail closed: the quarantine name stays)."""
    kinds = {e.rel: e.type for e in walked}
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        full = os.path.join(quarantine, rel_dir) if rel_dir else os.fspath(quarantine)
        try:
            with os.scandir(full) as listing:
                children = sorted((c.name, c.is_dir(follow_symlinks=False)) for c in listing)
        except OSError as exc:
            return f"{rel_dir or '.'} cannot be listed ({exc})"
        for name, is_dir in children:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            kind = kinds.get(rel)
            if kind is None:
                continue                        # new: not in the inventory
            if kind != "dir":
                return f"the walked {kind} {rel} is still present"
            if not is_dir:
                return f"the walked directory {rel} is no longer a directory"
            stack.append(rel)
    return ""


def _append_intent(scratch: Path, record: Mapping[str, Any]) -> None:
    """Append one record to ``<relocation scratch>/intents.jsonl``: written
    whole, the file fsynced, and its directory fsynced (the first append
    creates the file). A torn last line is terminated first (the caller holds
    the relocation lock), so the record is never glued to it. Raises
    `OSError` when any step fails."""
    path = Path(scratch) / INTENTS_NAME
    line = (json.dumps(record, sort_keys=True, ensure_ascii=True, default=str) + "\n"
            ).encode("ascii")
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o644)
    try:
        if _torn_tail(fd):
            line = b"\n" + line
        _write_all(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    ab._fsync_dir(path.parent)


def _open_intents(scratch: Path) -> list[dict]:
    """The intents of ``intents.jsonl`` without a completion record, in
    order, each with ``_removal_started`` when such a record follows it."""
    open_: dict[str, dict] = {}
    for record in _read_lines(Path(scratch) / INTENTS_NAME):
        if record.get("schema") != INTENT_SCHEMA or not record.get("receipt_id"):
            continue
        key = str(record["receipt_id"])
        if "completed" in record:
            open_.pop(key, None)
        elif record.get("removal_started"):
            if key in open_:
                open_[key]["_removal_started"] = True
        elif "quarantine_name" in record and "classification" in record:
            open_[key] = dict(record)
    return list(open_.values())


def _unlink_entry(name: bytes, dir_fd: int) -> None:
    """Unlink one manifest entry relative to its directory's descriptor (a
    private seam: a test makes one fail to stop a removal part-way)."""
    os.unlink(name, dir_fd=dir_fd)


def _rmdir_entry(name: bytes, dir_fd: int) -> None:
    """Remove one emptied manifest directory relative to its parent's
    descriptor; a late entry makes it fail with ENOTEMPTY."""
    os.rmdir(name, dir_fd=dir_fd)


__all__ = [
    "BUSY", "DONE", "FAILED", "KEPT", "LATE_ENTRY_RESTORED", "NEEDS_YOU", "PARTIAL",
    "RECOVERY_FAILED",
    "PROMOTION_REQUIRED", "REFUSED", "RELOCATED", "RELOCATION_SESSION",
    "STAGING_LEFTOVER", "STILL_WRITING", "STRANDED_TEMP", "TARGET_PROBLEMS",
    "UNKNOWN_STAGING_ENTRY", "UNREADABLE", "UNRESOLVED", "USER_RECLAIMED_ARCHIVE",
    "ExecuteReport", "RelocationPlan", "TargetTimeout", "VerifiedRelocation", "bounded",
    "write_new",
]
