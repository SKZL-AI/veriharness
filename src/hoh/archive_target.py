"""Archive targets, destination-verified placement and redundancy (STORAGE-C2, P5-27).

STORAGE-C1 (`hoh.archive_bundle`) builds a portable bundle and verifies it by
extracting and re-hashing it. This module decides **where** a bundle may land
and when a landed copy counts:

* `ArchiveTarget` is a registered archive target: a root directory, a
  failure-domain label and the identity the operator configured for it
  (e.g. a volume serial and label). Identity and health are read through two
  injected probes on **every** use -- the wrong disk is the wrong target
  (directive test S13). `check()` reports; `place()` and `scrub()` refuse when
  it does not confirm the identity, and `place()` also when the root is not a
  writable directory.
* `ArchiveTarget.place` copies a bundle's fixed files into
  ``<root>/<kind>/<project_id>/<bundle name>/`` and then verifies the copy
  **on the destination** with `archive_bundle.verify_archive`; only a copy
  that verified there gets a receipt (`archive_bundle.write_receipt`, which
  verifies once more by itself). A copy that does not verify is renamed
  ``<name>.partial`` and kept; a later placement versions that leftover to
  ``<name>.partial.v<N>.<UTC>`` and places afresh.
* `RedundancyLedger` records, per (bundle, target), ``COPY_WRITTEN``,
  ``COPY_VERIFIED`` (with the receipt digest) and ``COPY_STALE``. A bundle is
  ``REDUNDANCY_SATISFIED`` only with `required_copies` verified, non-stale
  copies on ``healthy`` targets in distinct failure domains -- distinct means
  both the failure-domain label **and** the probed identity differ. Health and
  identity are read **now** (`ArchiveTarget.check()`), never taken from the
  record: a copy on a ``degraded`` or ``unknown`` target, on a target not
  checked now, or on one whose identity is not confirmed now is verified but
  never counts; a unique bundle whose verified copies include none that is
  healthy now is reported ``ONLY_COPY_ON_DEGRADED_TARGET`` (directive A1,
  sections G and I; Revision 8).
* `scrub` re-runs the full verification on a placed copy and appends
  ``COPY_VERIFIED`` or ``COPY_STALE``.

Extract roots (Revisions 3, 4 and 7). Every verification this module runs --
for `place`, for `write_receipt` and for `scrub`, and the one `rehydrate`
runs -- extracts into a fresh directory created inside the calling session's
registered scratch (`StorageRegistry.scratch_dir(project, session,
"scratch")`): ``<session scratch>/<op>-<uuid>/`` holding an empty ``tree/``
that `verify_archive` extracts into, and, once the outcome is known, the
marker ``EXTRACT_ROOT.json`` beside it (bundle id, payload SHA-256,
verification status, the receipt it verified if any). Each root is indexed in
``<session scratch>/archive-target/EXTRACT_ROOTS.jsonl`` under an ``flock`` on
``EXTRACT_ROOTS.jsonl.lock``: a ``PENDING`` line before the directory is
created and extraction starts, and an outcome line (``VERIFIED`` with the
receipt digest, or ``FAILED``) afterwards. A crash therefore leaves an indexed
``PENDING`` root, never an unindexed one. Such a directory is
REPRODUCIBLE_SCRATCH -- it is reproduced by extracting the kept bundle again --
and is released only by STORAGE-D's VERIFIED_RELOCATION. Until STORAGE-D is
merged these directories accumulate in registered scratch; that cost is
accepted (Revision 3 item 2).

Nothing in this module removes a file or a directory. Temporary names are
published with ``renameat2(RENAME_NOREPLACE)`` (or hard links where that is
missing, through `archive_bundle`); a failed copy is renamed, never cleaned
up; superseded leftovers are versioned. Tests use ``tmp_path`` directories as
targets with injected probes; nothing here calls a Windows binary.
"""

from __future__ import annotations

import dataclasses
import datetime
import fcntl
import itertools
import json
import os
import re
import secrets
import stat
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from hoh import archive_bundle as ab

COLD_STORE = "cold-store"
RECLAIM_REVIEW = "reclaim-review"
KINDS = frozenset({COLD_STORE, RECLAIM_REVIEW})

HEALTHY = "healthy"
DEGRADED = "degraded"
UNKNOWN = "unknown"
HEALTH_VALUES = frozenset({HEALTHY, DEGRADED, UNKNOWN})

IDENTITY_MISMATCH = "identity_mismatch"
NOT_WRITABLE = "not_writable"

COPY_WRITTEN = "COPY_WRITTEN"
COPY_VERIFIED = "COPY_VERIFIED"
COPY_STALE = "COPY_STALE"
LEDGER_EVENTS = frozenset({COPY_WRITTEN, COPY_VERIFIED, COPY_STALE})

REDUNDANCY_SATISFIED = "REDUNDANCY_SATISFIED"
REDUNDANCY_NOT_SATISFIED = "REDUNDANCY_NOT_SATISFIED"
ONLY_COPY_ON_DEGRADED_TARGET = "ONLY_COPY_ON_DEGRADED_TARGET"

#: Why a verified copy does or does not count, read now (`RedundancyLedger.status`).
#: Every standing but the first is one of the three cases
#: ``ONLY_COPY_ON_DEGRADED_TARGET`` covers (8c item 2).
STANDING_HEALTHY = "healthy now"
STANDING_DEGRADED = "degraded health"
STANDING_UNKNOWN = "unknown health"
STANDING_NO_READING = "unknown health (no fresh reading: target not passed)"
STANDING_UNCONFIRMED = "unconfirmed identity"

#: Eligibility classifications that may enter reclaim-review (Rev2 item 4).
#: Their semantics, and those of ``proof_digest``, are STORAGE-D's.
RECLAIM_CLASSIFICATIONS = frozenset({"VERIFIED_DUPLICATE", "REPRODUCIBLE_SCRATCH"})

EXTRACT_PENDING = "PENDING"
EXTRACT_VERIFIED = "VERIFIED"
EXTRACT_FAILED = "FAILED"
EXTRACT_ROOT_MARKER = "EXTRACT_ROOT.json"
EXTRACT_ROOT_SCHEMA = "veriharness.extract-root/1"
EXTRACT_INDEX_DIR = "archive-target"
EXTRACT_INDEX = "EXTRACT_ROOTS.jsonl"
EXTRACT_INDEX_LOCK = "EXTRACT_ROOTS.jsonl.lock"
LEDGER_SCHEMA = "veriharness.redundancy-ledger/1"

PARTIAL_SUFFIX = ".partial"

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_OP = re.compile(r"^[a-z0-9-]{1,32}$")

#: Private test seam. When set, it is called as
#: ``_after_copy_hook(destination=...)`` after the bundle's files were copied
#: to the destination and before the destination is verified, so a test can
#: damage the copy at exactly that point. Never set outside tests.
_after_copy_hook = None


class ArchiveTargetRefused(ab.ArchiveRefused):
    """A target operation was refused before it changed anything it was asked
    to change. `reason` is a short machine-readable word; `check` is the
    `TargetCheck` when the refusal came from one."""

    def __init__(self, message: str, *, reason: str = "refused",
                 check: TargetCheck | None = None):
        super().__init__(message)
        self.reason = reason
        self.check = check


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _utc_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="microseconds")


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _canonical_identity(identity: Mapping | None) -> str | None:
    if identity is None:
        return None
    return json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_file(path: Path) -> str:
    return ab._hash_path(path)[0]


def _check_id(value: object, what: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ArchiveTargetRefused(f"{what} must match {_ID.pattern}: {value!r}",
                                   reason="invalid_" + what.replace(" ", "_"))
    return value


def _plain_dir(path: Path, what: str) -> None:
    """Create `path` if it is absent; refuse anything but a plain directory."""
    if os.path.lexists(path):
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            raise ArchiveTargetRefused(f"{what} {path} is not a plain directory "
                                       "(or is a symlink)", reason="not_a_directory")
        return
    os.mkdir(path)
    ab._fsync_dir(path.parent)


def _versioned(path: Path) -> Path:
    """``<name>.v<N>.<UTC>`` beside `path`, N one above every existing version."""
    pattern = re.compile(re.escape(path.name) + r"\.v(\d+)\.")
    versions = [int(m.group(1)) for name in os.listdir(path.parent)
                if (m := pattern.match(name))]
    return path.with_name(f"{path.name}.v{max(versions, default=0) + 1}.{_utc_stamp()}")


def _append_line(path: Path, record: Mapping) -> None:
    """One ``os.write`` of a complete JSON line on an ``O_APPEND`` descriptor,
    then ``fsync`` -- the durable append rule of the admission ledger. If the
    file does not end with a newline (a torn line), the newline goes in front
    of the record in the same write. Call under the file's lock."""
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                 | os.O_CLOEXEC, 0o644)
    try:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            line = b"\n" + line
        written = os.write(fd, line)
        if written != len(line):
            raise OSError(f"short write to {path}: {written} of {len(line)} bytes")
        os.fsync(fd)
    finally:
        os.close(fd)


class _Flock:
    """An exclusive ``flock`` on `path` (created if absent) for a ``with`` block."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> _Flock:
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                          | os.O_CLOEXEC, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc) -> None:
        assert self.fd is not None
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


def _read_jsonl(path: Path) -> list[dict]:
    """Every well-formed JSON object line of `path`; torn lines are skipped."""
    if not os.path.lexists(path):
        return []
    out = []
    for raw in ab._read_fresh(path).splitlines():
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _write_json_new(path: Path, record: Mapping) -> None:
    ab._write_new(path, (json.dumps(record, indent=2, sort_keys=True, default=str)
                         + "\n").encode("utf-8"))


def _read_manifest(bundle: Path) -> dict:
    try:
        manifest = json.loads(ab._read_fresh(bundle / ab.MANIFEST).decode("ascii"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ArchiveTargetRefused(f"MANIFEST.json of {bundle} unreadable: {exc}",
                                   reason="manifest_unreadable") from exc
    if not isinstance(manifest, dict):
        raise ArchiveTargetRefused(f"MANIFEST.json of {bundle} is not an object",
                                   reason="manifest_unreadable")
    return manifest


def manifest_unique(manifest: Mapping) -> bool:
    """The bundle's ``meta.unique`` flag; anything but an explicit ``false``
    reads as unique (fail closed, Rev2 item 4)."""
    meta = manifest.get("meta")
    return not (isinstance(meta, Mapping) and meta.get("unique") is False)


# --------------------------------------------------------------------------- #
# extract roots (registered scratch, indexed under C2's lock)
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class ExtractRoot:
    """One registered extract root: ``path`` holds ``tree/`` (the extraction)
    and, once closed, ``EXTRACT_ROOT.json``."""

    path: Path
    tree: Path
    op: str
    bundle_id: str | None
    payload_sha256: str | None
    project_id: str
    session_id: str
    session_scratch: Path
    index: Path
    created_utc: str
    outcome: str = EXTRACT_PENDING


def extract_index_path(session_scratch: Path) -> Path:
    """``<session scratch>/archive-target/EXTRACT_ROOTS.jsonl``."""
    return Path(session_scratch) / EXTRACT_INDEX_DIR / EXTRACT_INDEX


def _index_append(session_scratch: Path, record: dict) -> Path:
    """Append one line to the session's extract-root index under C2's lock.
    The indexed path must lie directly under this session's own scratch, or
    the line is refused (Rev7 item 5)."""
    scratch = Path(session_scratch)
    path = Path(record["path"])
    if path.parent != scratch:
        raise ArchiveTargetRefused(f"extract root {path} does not lie in the indexing "
                                   f"session's own scratch {scratch}",
                                   reason="extract_root_outside_scratch")
    index_dir = scratch / EXTRACT_INDEX_DIR
    _plain_dir(index_dir, "extract-root index directory")
    index = index_dir / EXTRACT_INDEX
    with _Flock(index_dir / EXTRACT_INDEX_LOCK):
        _append_line(index, record)
    return index


def _require_scratch(registry, project_id: str, session_id: str) -> Path:
    """The session's registered scratch, or a refusal before anything else is
    done (no registry, no project, no live lease)."""
    try:
        return Path(registry.scratch_dir(project_id, session_id, "scratch"))
    except Exception as exc:
        raise ArchiveTargetRefused(f"no registered scratch for project {project_id!r}, "
                                   f"session {session_id!r}: {exc}",
                                   reason="no_registry") from exc


def open_extract_root(registry, project_id: str, session_id: str, op: str,
                      bundle_id: str | None, payload_sha256: str | None) -> ExtractRoot:
    """A fresh extract root ``<session scratch>/<op>-<uuid>/`` with an empty
    ``tree/``, created through the storage registry and indexed ``PENDING``
    **before** it is created.

    `registry` is a `hoh.storage_registry.StorageRegistry`; the session must
    hold a live lease (the registry refuses otherwise). The new root is
    accepted only when the registry answers ``OWNED`` for it.
    """
    if not _OP.fullmatch(op):
        raise ArchiveTargetRefused(f"extract-root op {op!r} is not a plain word")
    scratch = Path(registry.scratch_dir(project_id, session_id, "scratch"))
    root = scratch / f"{op}-{uuid.uuid4().hex}"
    created = _utc_iso()
    index = _index_append(scratch, {
        "outcome": EXTRACT_PENDING, "op": op, "bundle_id": bundle_id,
        "path": os.fspath(root), "created_utc": created,
        "project_id": project_id, "session_id": session_id,
    })
    os.mkdir(root)
    os.mkdir(root / "tree")
    ab._fsync_dir(root)
    ab._fsync_dir(scratch)
    owner = registry.owner_of(root)
    if owner.status != "OWNED" or owner.session_id != session_id:
        raise ArchiveTargetRefused(f"the registry does not answer OWNED for the new "
                                   f"extract root {root}: {owner.status} {owner.reason}",
                                   reason="extract_root_not_owned")
    return ExtractRoot(path=root, tree=root / "tree", op=op, bundle_id=bundle_id,
                       payload_sha256=payload_sha256, project_id=project_id,
                       session_id=session_id, session_scratch=scratch, index=index,
                       created_utc=created)


def close_extract_root(root: ExtractRoot, *, verified: bool,
                       report: ab.VerificationReport | None = None,
                       receipt_digest: str | None = None,
                       receipt_path: Path | str | None = None,
                       note: str = "") -> None:
    """Write ``EXTRACT_ROOT.json`` beside ``tree/`` and append the outcome
    line (``VERIFIED`` with the receipt digest, or ``FAILED``) to the index."""
    outcome = EXTRACT_VERIFIED if verified else EXTRACT_FAILED
    finished = _utc_iso()
    marker = {
        "schema": EXTRACT_ROOT_SCHEMA,
        "classification": "REPRODUCIBLE_SCRATCH",
        "op": root.op,
        "bundle_id": root.bundle_id,
        "payload_sha256": root.payload_sha256,
        # write_receipt keeps its report to itself; it returns only after
        # ARCHIVED_VERIFIED, so a verified root without a report is that.
        "verification_status": (report.status if report is not None
                                else ab.ARCHIVED_VERIFIED if verified
                                else ab.ARCHIVE_UNVERIFIED),
        "outcome": outcome,
        "receipt": ({"path": os.fspath(receipt_path) if receipt_path else None,
                     "digest": receipt_digest} if receipt_digest else None),
        "project_id": root.project_id,
        "session_id": root.session_id,
        "created_utc": root.created_utc,
        "finished_utc": finished,
        "note": note,
        "reproducible_by": "extracting the kept bundle again (verify_archive)",
    }
    marker_error = None
    try:
        _write_json_new(root.path / EXTRACT_ROOT_MARKER, marker)
    except OSError as exc:              # the index line must still be written
        marker_error = exc
    _index_append(root.session_scratch, {
        "outcome": outcome, "op": root.op, "bundle_id": root.bundle_id,
        "path": os.fspath(root.path), "receipt_digest": receipt_digest,
        "finished_utc": finished, "project_id": root.project_id,
        "session_id": root.session_id,
    })
    root.outcome = outcome
    if marker_error is not None:
        raise marker_error


# --------------------------------------------------------------------------- #
# the target
# --------------------------------------------------------------------------- #

@dataclasses.dataclass(frozen=True)
class TargetCheck:
    """What `ArchiveTarget.check` read. `ok` is true only when the identity
    was read and equals the expected one **and** the root is a writable
    directory; `identity_ok` alone is what a read-only use (scrub,
    rehydration) needs. `reason` is None, ``identity_mismatch`` (differs or
    unreadable) or ``not_writable``. `health` is always reported."""

    target_id: str
    ok: bool
    reason: str | None
    identity_ok: bool
    identity_readable: bool
    identity: dict | None
    health: str
    writable: bool
    detail: str


@dataclasses.dataclass
class PlaceResult:
    """The outcome of `ArchiveTarget.place`. `status` is ``ARCHIVED_VERIFIED``
    only when the copy verified on the destination and its receipt was
    written; otherwise ``ARCHIVE_UNVERIFIED`` and `path` is the kept
    ``*.partial`` directory."""

    status: str
    bundle_id: str
    path: Path
    receipt_path: Path | None
    receipt_digest: str | None
    report: ab.VerificationReport | None
    check: TargetCheck
    extract_roots: list[Path]
    versioned_partial: Path | None = None
    detail: str = ""


class ArchiveTarget:
    """A registered archive target.

    `config` is a mapping with ``target_id``, ``root`` (a directory),
    ``failure_domain`` (a label), ``expected_identity`` (a non-empty mapping,
    e.g. the volume serial and label as configured), ``identity_probe(root)
    -> dict | None`` and ``health_probe(root) -> "healthy" | "degraded" |
    "unknown"``; optionally ``registry`` (a `StorageRegistry`) and
    ``session_id``, the defaults for the extract roots of `place` and `scrub`.
    """

    def __init__(self, config: Mapping[str, Any]):
        if not isinstance(config, Mapping):
            raise ArchiveTargetRefused("config must be a mapping", reason="invalid_config")
        self.target_id = _check_id(config.get("target_id"), "target id")
        root = config.get("root")
        if not root:
            raise ArchiveTargetRefused("config names no root", reason="invalid_config")
        self.root = Path(os.path.abspath(os.fspath(root)))
        domain = config.get("failure_domain")
        if not isinstance(domain, str) or not domain.strip():
            raise ArchiveTargetRefused("failure_domain must be a non-empty label",
                                       reason="invalid_config")
        self.failure_domain = domain
        expected = config.get("expected_identity")
        if not isinstance(expected, Mapping) or not expected:
            raise ArchiveTargetRefused("expected_identity must be a non-empty mapping",
                                       reason="invalid_config")
        self.expected_identity = dict(expected)
        self.identity_probe: Callable[[Path], dict | None] = config.get("identity_probe")
        self.health_probe: Callable[[Path], str] = config.get("health_probe")
        if not callable(self.identity_probe) or not callable(self.health_probe):
            raise ArchiveTargetRefused("identity_probe and health_probe must be callables",
                                       reason="invalid_config")
        self.registry = config.get("registry")
        self.session_id = config.get("session_id")

    def __repr__(self) -> str:
        return (f"ArchiveTarget({self.target_id!r}, root={os.fspath(self.root)!r}, "
                f"failure_domain={self.failure_domain!r})")

    # -- check -------------------------------------------------------------- #

    def check(self) -> TargetCheck:
        """Read the identity and the health of the root, and whether it is a
        writable directory. Creates nothing anywhere."""
        notes = []
        try:
            identity = self.identity_probe(self.root)
        except Exception as exc:        # a probe that fails proves nothing
            identity = None
            notes.append(f"identity probe failed: {type(exc).__name__}: {exc}")
        readable = isinstance(identity, Mapping)
        identity = dict(identity) if readable else None
        identity_ok = readable and all(
            key in identity and identity[key] == value
            for key, value in self.expected_identity.items())
        if not readable:
            notes.append("identity unreadable")
        elif not identity_ok:
            notes.append(f"identity {identity!r} differs from the expected "
                         f"{self.expected_identity!r}")
        try:
            health = self.health_probe(self.root)
        except Exception as exc:
            health = UNKNOWN
            notes.append(f"health probe failed: {type(exc).__name__}: {exc}")
        if health not in HEALTH_VALUES:
            notes.append(f"health {health!r} is not a known value; read as unknown")
            health = UNKNOWN
        try:
            st = os.lstat(self.root)
            writable = (stat.S_ISDIR(st.st_mode)
                        and os.access(self.root, os.W_OK | os.X_OK))
        except OSError:
            writable = False
        if not writable:
            notes.append(f"root {self.root} is not a writable plain directory")
        reason = None
        if not identity_ok:
            reason = IDENTITY_MISMATCH
        elif not writable:
            reason = NOT_WRITABLE
        return TargetCheck(target_id=self.target_id, ok=reason is None, reason=reason,
                           identity_ok=identity_ok, identity_readable=readable,
                           identity=identity, health=health, writable=writable,
                           detail="; ".join(notes) or "identity confirmed, root writable")

    def _require(self, *, write: bool) -> TargetCheck:
        check = self.check()
        if not check.identity_ok:
            raise ArchiveTargetRefused(f"target {self.target_id}: {IDENTITY_MISMATCH} "
                                       f"({check.detail})", reason=IDENTITY_MISMATCH,
                                       check=check)
        if write and not check.writable:
            raise ArchiveTargetRefused(f"target {self.target_id}: {NOT_WRITABLE} "
                                       f"({check.detail})", reason=NOT_WRITABLE, check=check)
        return check

    # -- areas -------------------------------------------------------------- #

    def area_path(self, project_id: str, kind: str) -> Path:
        """``<root>/<kind>/<project_id>/`` without creating it."""
        if kind not in KINDS:
            raise ArchiveTargetRefused(f"kind must be one of {sorted(KINDS)}: {kind!r}",
                                       reason="invalid_kind")
        _check_id(project_id, "project id")
        return self.root / kind / project_id

    def area(self, project_id: str, kind: str) -> Path:
        """The directory ``<root>/<kind>/<project_id>/``, created on first use
        (only below `root`, never through a symlink)."""
        path = self.area_path(project_id, kind)
        st = os.lstat(self.root)
        if not stat.S_ISDIR(st.st_mode):
            raise ArchiveTargetRefused(f"target root {self.root} is not a plain directory",
                                       reason=NOT_WRITABLE)
        _plain_dir(path.parent, "area kind directory")
        _plain_dir(path, "area")
        return path

    # -- placing ------------------------------------------------------------ #

    def _session(self, registry, session_id):
        registry = registry if registry is not None else self.registry
        session_id = session_id if session_id is not None else self.session_id
        if registry is None or not session_id:
            raise ArchiveTargetRefused("extract roots must be registered scratch: pass a "
                                       "StorageRegistry and a session id",
                                       reason="no_registry")
        return registry, session_id

    def place(self, bundle_dir: Path, project_id: str, kind: str,
              eligibility: Mapping | None = None, *, registry=None,
              session_id: str | None = None,
              ledger: RedundancyLedger | None = None) -> PlaceResult:
        """Copy the bundle at `bundle_dir` into the area and verify it there.

        Refused (`ArchiveTargetRefused`) before anything is copied: a target
        whose identity is not confirmed or whose root is not writable (`check`
        runs first, on every call); an invalid project id or kind; a
        reclaim-review placement without a consistent `eligibility` record
        (classification ``VERIFIED_DUPLICATE`` or ``REPRODUCIBLE_SCRATCH``, a
        non-empty ``proof_digest``, ``unique`` false) or of a bundle whose
        manifest ``meta`` does not say ``unique: false``; a source that is not
        a complete bundle; an existing destination (a verified bundle is never
        overwritten).

        A leftover ``<name>.partial`` from an earlier failed placement is
        renamed ``<name>.partial.v<N>.<UTC>`` (kept) before the new copy.
        Each fixed file (payload, ``MANIFEST.json``, ``SHA256SUMS``) is written
        under a temporary name, fsynced and published without replacing; the
        destination directory is fsynced. Then `verify_archive` runs on the
        destination in a fresh registered extract root, the identity is
        checked again, and only on ``ARCHIVED_VERIFIED`` is the receipt
        written by `write_receipt` (in an extract root of its own). On any
        failure after the destination directory was created -- the copy
        itself included (e.g. ENOSPC on the second file) -- nothing is raised:
        the destination is renamed ``<name>.partial`` and kept (a leftover
        temporary file stays inside it), and the result says
        ``ARCHIVE_UNVERIFIED``.
        """
        check = self._require(write=True)
        _check_id(project_id, "project id")
        if kind not in KINDS:
            raise ArchiveTargetRefused(f"kind must be one of {sorted(KINDS)}: {kind!r}",
                                       reason="invalid_kind", check=check)
        registry, session_id = self._session(registry, session_id)
        _require_scratch(registry, project_id, session_id)
        source = Path(bundle_dir)
        name = source.name
        if not ab._BUNDLE_NAME.fullmatch(name):
            raise ArchiveTargetRefused(f"{source} is not named like a bundle",
                                       reason="not_a_bundle", check=check)
        if not stat.S_ISDIR(os.lstat(source).st_mode):
            raise ArchiveTargetRefused(f"{source} is not a plain directory",
                                       reason="not_a_bundle", check=check)
        if os.path.lexists(source / ab.INCOMPLETE):
            raise ArchiveTargetRefused(f"{source} is marked INCOMPLETE",
                                       reason="not_a_bundle", check=check)
        payloads = [p for p in (ab.TREE_PAYLOAD, ab.GIT_PAYLOAD)
                    if os.path.lexists(source / p)]
        if len(payloads) != 1:
            raise ArchiveTargetRefused(f"{source} must hold exactly one payload",
                                       reason="not_a_bundle", check=check)
        files = [payloads[0], ab.MANIFEST, ab.SUMS]
        for item in files:
            if not stat.S_ISREG(os.lstat(source / item).st_mode):
                raise ArchiveTargetRefused(f"{source / item} is not a regular file",
                                           reason="not_a_bundle", check=check)
        manifest = _read_manifest(source)
        unique = manifest_unique(manifest)
        if kind == RECLAIM_REVIEW:
            _reclaim_gate(eligibility, unique, check)

        area = self.area(project_id, kind)
        dest = area / name
        if os.path.lexists(dest):
            raise ArchiveTargetRefused(f"destination {dest} exists; a placed bundle is "
                                       "never overwritten", reason="destination_exists",
                                       check=check)
        partial = area / (name + PARTIAL_SUFFIX)
        versioned = None
        if os.path.lexists(partial):
            versioned = _versioned(partial)
            ab._rename_noreplace(partial, versioned)
            ab._fsync_dir(area)

        payload_sha = manifest.get("payload_sha256")
        bundle_id = manifest.get("bundle_id") or name
        os.mkdir(dest)
        # From here on every step is inside the failure handling: whatever
        # fails -- a copy (ENOSPC on the second file), an fsync, the ledger,
        # the verification or the receipt -- the destination is kept as
        # <name>.partial and the result says ARCHIVE_UNVERIFIED.
        roots: list[Path] = []
        report = None
        receipt = receipt_digest = None
        detail = ""
        pending: list[ExtractRoot] = []
        again = check
        stage = "copy"
        try:
            ab._fsync_dir(area)
            for item in files:
                _copy_file(source / item, dest, item)
            ab._fsync_dir(dest)
            if ledger is not None:
                ledger.append(COPY_WRITTEN, bundle_id=bundle_id, target=self, check=check,
                              path=dest, unique=unique, kind=kind, project_id=project_id)
            hook = _after_copy_hook
            if hook is not None:
                hook(destination=dest)
            stage = "verification"
            verify_root = open_extract_root(registry, project_id, session_id,
                                            "place-verify", bundle_id, payload_sha)
            roots.append(verify_root.path)
            pending.append(verify_root)
            report = ab.verify_archive(dest, extract_root=verify_root.tree)
            again = self.check()
            if report.status != ab.ARCHIVED_VERIFIED:
                detail = "the copy did not verify on the destination: " + ", ".join(
                    item["id"] for item in report.failed())
            elif not again.identity_ok:
                detail = ("target identity no longer confirmed after verification: "
                          + again.detail)
            else:
                receipt_root = open_extract_root(registry, project_id, session_id,
                                                 "place-receipt", bundle_id, payload_sha)
                roots.append(receipt_root.path)
                pending.append(receipt_root)
                try:
                    receipt = ab.write_receipt(
                        dest, extract_root=receipt_root.tree,
                        destination=self._destination(again, kind, project_id, area))
                    receipt_digest = _sha256_file(receipt)
                except ab.ArchiveRefused as exc:
                    receipt = receipt_digest = None
                    detail = "no receipt: " + str(exc)
                    close_extract_root(receipt_root, verified=False,
                                       report=getattr(exc, "report", None), note=detail)
                else:
                    close_extract_root(receipt_root, verified=True,
                                       receipt_digest=receipt_digest, receipt_path=receipt,
                                       note="write_receipt verified the destination again")
            close_extract_root(verify_root,
                               verified=report.status == ab.ARCHIVED_VERIFIED,
                               report=report, receipt_digest=receipt_digest,
                               receipt_path=receipt, note=detail)
        except Exception as exc:
            # Whatever stopped the copy or the verification, the destination
            # is kept as .partial and every root this call opened is closed
            # FAILED. A leftover temporary file stays inside the .partial.
            receipt = receipt_digest = None
            detail = (f"copy failed: {type(exc).__name__}: {exc}" if stage == "copy"
                      else f"verification stopped: {type(exc).__name__}: {exc}")
            for root in pending:
                if root.outcome == EXTRACT_PENDING:
                    try:
                        close_extract_root(root, verified=False, report=report,
                                           note=detail)
                    except Exception as inner:   # keep the first error's account
                        detail += f"; closing {root.path} failed: {inner}"

        if receipt is not None:
            if ledger is not None:
                ledger.append(COPY_VERIFIED, bundle_id=bundle_id, target=self, check=again,
                              path=dest, unique=unique, kind=kind, project_id=project_id,
                              receipt_digest=receipt_digest)
            return PlaceResult(status=ab.ARCHIVED_VERIFIED, bundle_id=bundle_id, path=dest,
                               receipt_path=receipt, receipt_digest=receipt_digest,
                               report=report, check=again, extract_roots=roots,
                               versioned_partial=versioned, detail="verified on the "
                               "destination; receipt written")
        kept = _keep_partial(dest)
        return PlaceResult(status=ab.ARCHIVE_UNVERIFIED, bundle_id=bundle_id, path=kept,
                           receipt_path=None, receipt_digest=None, report=report,
                           check=check, extract_roots=roots, versioned_partial=versioned,
                           detail=detail)

    def _destination(self, check: TargetCheck, kind: str, project_id: str,
                     area: Path) -> dict:
        return {"target_id": self.target_id, "failure_domain": self.failure_domain,
                "identity": check.identity, "health": check.health, "kind": kind,
                "project_id": project_id, "area": os.fspath(area)}


def _reclaim_gate(eligibility: Mapping | None, unique: bool, check: TargetCheck) -> None:
    """reclaim-review needs a consistent eligibility record and a bundle whose
    manifest says ``unique: false`` (Rev2 item 4). STORAGE-D owns the meaning
    of the eligibility; this checks its presence and consistency."""
    problems = []
    if not isinstance(eligibility, Mapping):
        problems.append("no eligibility record")
    else:
        if eligibility.get("classification") not in RECLAIM_CLASSIFICATIONS:
            problems.append(f"classification {eligibility.get('classification')!r} not in "
                            f"{sorted(RECLAIM_CLASSIFICATIONS)}")
        proof = eligibility.get("proof_digest")
        if not isinstance(proof, str) or not proof.strip():
            problems.append("proof_digest is empty")
        if eligibility.get("unique", True) is not False:
            problems.append("eligibility does not say unique: false")
    if unique:
        problems.append("the bundle's manifest meta does not say unique: false")
    if problems:
        raise ArchiveTargetRefused("reclaim-review refused before copying: "
                                   + "; ".join(problems), reason="not_eligible",
                                   check=check)


def _copy_file(src: Path, dest_dir: Path, name: str) -> None:
    """Copy `src` to ``dest_dir/name`` through a temporary name: written,
    fsynced, then published without replacing."""
    temp = dest_dir / f".{name}.{secrets.token_hex(6)}.tmp"
    sfd = ab._open_fresh(src)
    try:
        dfd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
                      | os.O_NOFOLLOW, 0o644)
        try:
            while True:
                chunk = os.read(sfd, ab._CHUNK)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(dfd, view)
                    if written <= 0:
                        raise OSError(f"zero-byte write to {temp}")
                    view = view[written:]
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)
    ab._rename_noreplace(temp, dest_dir / name)


def _keep_partial(dest: Path) -> Path:
    """Rename a copy that did not verify to ``<name>.partial`` (kept, never
    removed); an existing ``.partial`` is versioned first."""
    partial = dest.with_name(dest.name + PARTIAL_SUFFIX)
    if os.path.lexists(partial):
        ab._rename_noreplace(partial, _versioned(partial))
    ab._rename_noreplace(dest, partial)
    ab._fsync_dir(dest.parent)
    return partial


# --------------------------------------------------------------------------- #
# redundancy
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class RedundancyStatus:
    """What `RedundancyLedger.status` found for one bundle. `verified` lists
    every verified, non-stale copy (each with its ``standing`` read now);
    `counting` those on targets confirmed and healthy now;
    `chosen` the copies in distinct failure domains that satisfy the policy
    (empty unless satisfied)."""

    bundle_id: str
    status: str
    required_copies: int
    unique: bool
    verified: list[dict]
    counting: list[dict]
    chosen: list[dict]
    detail: str


class RedundancyLedger:
    """JSON lines, one record per event and (bundle, target): ``COPY_WRITTEN``,
    ``COPY_VERIFIED`` (with the receipt digest) or ``COPY_STALE`` (a scrub
    failed, the identity could not be confirmed, or the copy is overdue).

    Each record carries the target id, its failure-domain label, the probed
    identity and the health read at that time (kept for the record; a
    verdict never uses it, see `status`). Appends are one ``O_APPEND``
    write under an ``flock`` on ``<path>.lock``; nothing is rewritten."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def append(self, event: str, *, bundle_id: str, target: ArchiveTarget,
               check: TargetCheck | None = None, path: Path | None = None,
               unique: bool = True, receipt_digest: str | None = None,
               kind: str | None = None, project_id: str | None = None,
               reason: str = "") -> dict:
        if event not in LEDGER_EVENTS:
            raise ValueError(f"unknown ledger event {event!r}")
        if event == COPY_VERIFIED:
            if not receipt_digest:
                raise ValueError("COPY_VERIFIED needs the receipt digest")
            if check is None or not check.identity_ok:
                raise ArchiveTargetRefused("a copy on a target whose identity is not "
                                           "confirmed is never COPY_VERIFIED",
                                           reason=IDENTITY_MISMATCH, check=check)
        record = {
            "schema": LEDGER_SCHEMA, "event": event, "bundle_id": bundle_id,
            "target_id": target.target_id, "failure_domain": target.failure_domain,
            "identity": check.identity if check is not None else None,
            "identity_ok": bool(check.identity_ok) if check is not None else False,
            "health": check.health if check is not None else UNKNOWN,
            "path": os.fspath(path) if path is not None else None,
            "unique": bool(unique), "receipt_digest": receipt_digest,
            "kind": kind, "project_id": project_id, "reason": reason,
            "utc": _utc_iso(),
        }
        with _Flock(self.path.with_name(self.path.name + ".lock")):
            _append_line(self.path, record)
        return record

    def records(self, bundle_id: str | None = None) -> list[dict]:
        return [r for r in _read_jsonl(self.path)
                if r.get("event") in LEDGER_EVENTS
                and (bundle_id is None or r.get("bundle_id") == bundle_id)]

    def latest(self, bundle_id: str) -> dict[str, dict]:
        """The last record per target for `bundle_id`."""
        out: dict[str, dict] = {}
        for record in self.records(bundle_id):
            out[str(record.get("target_id"))] = record
        return out

    def status(self, bundle_id: str, *, required_copies: int = 2,
               unique: bool | None = None,
               targets: Mapping[str, ArchiveTarget] | Iterable[ArchiveTarget] | None = None,
               max_age: datetime.timedelta | None = None,
               now: datetime.datetime | None = None) -> RedundancyStatus:
        """Judge the bundle's redundancy from the ledger and from the targets
        as they are **now** (Revision 8 item 1, 8b item 3, 8c item 2).

        A copy is verified when its target's last record is ``COPY_VERIFIED``
        and, with `max_age`, not older than that (an overdue scrub is stale).
        The health recorded at copy time is never used for a verdict: every
        verified copy's target is checked now through the matching entry of
        `targets` (`ArchiveTarget.check()`). A copy stays in `verified` but
        counts as ``unknown`` when there is no fresh reading -- its target is
        not in `targets` (none given, or a partial list) -- and when its
        target's identity is not confirmed now (an unmounted or swapped
        volume). A verified copy counts only when its target is confirmed and
        ``healthy`` now. The policy is satisfied by `required_copies` counting
        copies whose failure-domain labels **and** identities differ pairwise.

        A unique bundle with verified copies of which none is healthy now is
        reported ``ONLY_COPY_ON_DEGRADED_TARGET`` -- the one constant for
        degraded health, unknown health (including no fresh reading) and an
        unconfirmed identity; `detail` names which of these applies to each
        copy, and each copy carries it as ``standing``. `unique` defaults to
        what the records say, and to true when any record does not say false
        (fail closed).
        """
        if not isinstance(required_copies, int) or required_copies < 1:
            raise ValueError("required_copies must be a positive int")
        if isinstance(targets, Mapping):
            current = dict(targets)
        else:
            current = {t.target_id: t for t in (targets or [])}
        now = now or datetime.datetime.now(datetime.UTC)
        latest = self.latest(bundle_id)
        if unique is None:
            unique = not latest or any(r.get("unique") is not False
                                       for r in self.records(bundle_id))
        verified: list[dict] = []
        notes: list[str] = []
        for target_id, record in sorted(latest.items()):
            if record.get("event") != COPY_VERIFIED or not record.get("identity_ok"):
                continue
            copy = dict(record)
            if max_age is not None:
                try:
                    when = datetime.datetime.fromisoformat(record["utc"])
                except (KeyError, TypeError, ValueError):
                    when = None
                if when is None or now - when > max_age:
                    notes.append(f"{target_id}: scrub overdue, stale")
                    continue
            # Never the recorded health: without a confirmed reading now the
            # copy counts as unknown (it stays verified, it does not count).
            copy["recorded_health"] = record.get("health")
            target = current.get(target_id)
            if target is None:
                copy.update(health=UNKNOWN, identity_confirmed=False,
                            standing=STANDING_NO_READING)
            else:
                check = target.check()
                if not check.identity_ok:
                    copy.update(health=UNKNOWN, identity_confirmed=False,
                                standing=STANDING_UNCONFIRMED)
                else:
                    copy.update(health=check.health, identity=check.identity,
                                failure_domain=target.failure_domain,
                                identity_confirmed=True,
                                standing={HEALTHY: STANDING_HEALTHY,
                                          DEGRADED: STANDING_DEGRADED}.get(
                                              check.health, STANDING_UNKNOWN))
            if copy["standing"] != STANDING_HEALTHY:
                notes.append(f"{target_id}: {copy['standing']}")
            verified.append(copy)
        counting = [c for c in verified
                    if c.get("identity_confirmed") and c.get("health") == HEALTHY]
        chosen = _distinct_domains(counting, required_copies)
        if chosen:
            status = REDUNDANCY_SATISFIED
        elif unique and verified and not counting:
            status = ONLY_COPY_ON_DEGRADED_TARGET
        else:
            status = REDUNDANCY_NOT_SATISFIED
        detail = (f"{len(verified)} verified, {len(counting)} on targets confirmed "
                  f"healthy now, {required_copies} required in distinct failure domains")
        if notes:
            detail += "; " + "; ".join(notes)
        return RedundancyStatus(bundle_id=bundle_id, status=status,
                                required_copies=required_copies, unique=bool(unique),
                                verified=verified, counting=counting, chosen=chosen,
                                detail=detail)


def _distinct_domains(copies: list[dict], required: int) -> list[dict]:
    """`required` copies whose failure-domain labels and probed identities
    differ pairwise, or [] when there are none (Rev2 item 6)."""
    for combo in itertools.combinations(copies, required):
        labels = {c.get("failure_domain") for c in combo}
        identities = {_canonical_identity(c.get("identity")) for c in combo}
        if len(labels) == required and len(identities) == required \
                and None not in identities:
            return list(combo)
    return []


# --------------------------------------------------------------------------- #
# scrub
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class ScrubResult:
    event: str
    bundle_id: str
    path: Path
    report: ab.VerificationReport | None
    check: TargetCheck
    receipt_digest: str | None
    extract_root: Path | None
    detail: str


def scrub(target: ArchiveTarget, bundle_name: str, *, project_id: str,
          ledger: RedundancyLedger, kind: str = COLD_STORE, registry=None,
          session_id: str | None = None) -> ScrubResult:
    """Re-run the full verification of a placed copy on the destination, in a
    fresh registered extract root, and append ``COPY_VERIFIED`` (with the
    digest of the copy's receipt) or ``COPY_STALE``.

    `check` runs first: on a target whose identity is not confirmed the copy
    is never read, ``COPY_STALE`` (reason ``identity_mismatch``) is recorded
    and the scrub is refused with `ArchiveTargetRefused` (Rev2 item 5). A
    copy without its receipt,
    whose receipt names another payload, or that does not verify, is stale.
    """
    if not ab._BUNDLE_NAME.fullmatch(str(bundle_name)):
        raise ArchiveTargetRefused(f"{bundle_name!r} is not a bundle name",
                                   reason="not_a_bundle")
    path = target.area_path(project_id, kind) / bundle_name
    check = target.check()
    if not check.identity_ok:
        detail = f"{IDENTITY_MISMATCH}: {check.detail}"
        ledger.append(COPY_STALE, bundle_id=bundle_name, target=target, check=check,
                      path=path, kind=kind, project_id=project_id, reason=detail)
        raise ArchiveTargetRefused(f"scrub of {bundle_name} on target {target.target_id} "
                                   f"refused: {detail}", reason=IDENTITY_MISMATCH,
                                   check=check)
    registry, session_id = target._session(registry, session_id)
    _require_scratch(registry, project_id, session_id)
    unique = True
    payload_sha = None
    try:
        manifest = _read_manifest(path)
        unique = manifest_unique(manifest)
        payload_sha = manifest.get("payload_sha256")
    except ArchiveTargetRefused:
        pass
    root = open_extract_root(registry, project_id, session_id, "scrub", bundle_name,
                             payload_sha)
    report = None
    receipt_digest = None
    detail = ""
    try:
        report = ab.verify_archive(path, extract_root=root.tree)
        again = target.check()
        receipt = path / ab.RECEIPT
        if report.status != ab.ARCHIVED_VERIFIED:
            detail = "did not verify: " + ", ".join(i["id"] for i in report.failed())
        elif not again.identity_ok:
            detail = f"{IDENTITY_MISMATCH} after verification: {again.detail}"
        elif not os.path.lexists(receipt) or not stat.S_ISREG(os.lstat(receipt).st_mode):
            detail = "the copy has no receipt"
        else:
            try:
                record = json.loads(ab._read_fresh(receipt).decode("ascii"))
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                record = None
                detail = f"receipt unreadable: {exc}"
            if record is not None:
                if (record.get("payload_sha256") != report.digests.get("payload_sha256")
                        or record.get("content_digest")
                        != report.digests.get("content_digest")):
                    detail = "the receipt names another payload or content"
                else:
                    receipt_digest = _sha256_file(receipt)
            check = again
    finally:
        close_extract_root(root, verified=receipt_digest is not None, report=report,
                           receipt_digest=receipt_digest,
                           receipt_path=(path / ab.RECEIPT) if receipt_digest else None,
                           note=detail or "scrub verified the copy")
    if receipt_digest is not None:
        ledger.append(COPY_VERIFIED, bundle_id=bundle_name, target=target, check=check,
                      path=path, unique=unique, kind=kind, project_id=project_id,
                      receipt_digest=receipt_digest)
        return ScrubResult(event=COPY_VERIFIED, bundle_id=bundle_name, path=path,
                           report=report, check=check, receipt_digest=receipt_digest,
                           extract_root=root.path, detail="verified on the destination")
    ledger.append(COPY_STALE, bundle_id=bundle_name, target=target, check=check, path=path,
                  unique=unique, kind=kind, project_id=project_id, reason=detail)
    return ScrubResult(event=COPY_STALE, bundle_id=bundle_name, path=path, report=report,
                       check=check, receipt_digest=None, extract_root=root.path,
                       detail=detail)
