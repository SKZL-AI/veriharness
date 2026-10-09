"""Portable archive bundles and their verification (STORAGE-C1, P5-25, P5-26).

An archive target such as an exFAT disk keeps no Linux modes, owners,
symlinks or hardlinks, so a raw directory copy is not an archive. On
2026-09-28 an archive made outside this repository held only a model cache's
symlinks, not the content they pointed to -- it was judged by names and a copy
command's exit status, and then trusted. This module builds a bundle whose
format carries all of that inside one payload, and a verifier that judges a
bundle **only** by re-reading it, decompressing the whole payload, comparing
it member for member with the manifest, extracting it into an empty root that
cannot be escaped and re-hashing every byte that came out.

A bundle is a directory ``vh-arch-YYYYMMDDTHHMMSSZ-<12 hex>-<label>`` holding
``payload.tar.zst`` or ``payload.git.bundle``, ``MANIFEST.json`` and
``SHA256SUMS``; ``ARCHIVE_RECEIPT.json`` is added only by `write_receipt`,
after a verification it ran itself. A build that fails half way leaves its
directory standing with a file ``INCOMPLETE`` in it.

Nothing in this module removes a file or a directory. Records are created
exclusively; temporary names are published with renameat2(RENAME_NOREPLACE)
or, where that is unavailable, with hard links (never a check followed by an
ordinary rename, and never replacing anything); a failed build is marked
rather than cleaned up.

``zstandard`` is optional (``pip install 'hoh[archive]'``) and imported only
inside the functions that need it; when it is missing they raise
`ArchiveDependencyMissing`. There is no fallback format.

Leftovers of the hard-link fallback (Rev8 s6). Where renameat2 is missing,
a receipt is published by linking ``.ARCHIVE_RECEIPT.json.<hex>.tmp`` to
``ARCHIVE_RECEIPT.json`` and a bundle by linking the files of its
``vh-arch-....building`` staging directory into the final directory. The
temporary name and the ``.building`` staging directory then remain as second
hard links to the same inodes (no extra space). They cannot be certified --
``bundle_name`` fails for them -- and STORAGE-D's inventory is to count each
inode once. This module still removes nothing.

NOT_RUN (Rev9 s5): the hard-link fallback has not been run on a real exFAT
target (E:), which has no hard links and must fail closed there; it is
exercised only through the ``_renameat2`` seam until a target run measures
it. The other-device case of the computed worktree walk is NOT_RUN where no
second device exists under the test's tmp_path (a seam case runs instead).

Git payloads: `ArchiveBundle.build_git` holds the complete definition of the
recorded facts. ``covers_worktree`` never covers ``.git`` metadata
(configuration, remotes, ``info/exclude``, hooks) -- STORAGE-D must not free a
repository on that fact alone -- nor permission bits other than the
executable bit; a repository using ``core.autocrlf``, eol attributes, clean
filters or LFS is never ``covers_worktree`` (it fails closed).

Git commands (Rev10a, Rev10b s3). The archiver and the verifier run exactly
these git commands and no others -- ``test_git_subcommands_closed_list``
enforces the list by parsing every argument tuple handed to the git runner:

- builder and verifier: ``rev-parse``, ``symbolic-ref``, ``for-each-ref``,
  ``rev-list``, ``reflog show``, ``ls-tree``, ``ls-files``, ``worktree
  list``, ``config --list``, ``bundle create``;
- verifier only, in its fresh extract root: ``init --bare`` (with
  ``--template=``), ``bundle verify``, ``fetch`` from the bundle, ``fsck``.

None of them applies a filter, and none of the fact commands runs a
repository-configured program -- no filter, hook, fsmonitor, pager or gpg:
``git status`` (which applies clean filters and runs fsmonitor) and ``git
stash list`` (which verifies signatures through ``gpg.program`` when
``log.showSignature`` is set) are not run at all, ``clone`` and ``bundle
list-heads`` are replaced by ``init --bare --template=`` plus ``fetch`` and
a bundle header parsed in Python. Every call also carries ``-c
core.fsmonitor=false -c core.untrackedCache=false -c core.fileMode=true -c
core.ignoreStat=false -c core.hooksPath=/dev/null -c log.showSignature=false
-c core.pager=cat`` (command-line ``-c`` wins over every configuration file,
included ones too), runs under `_git_env`, in its own session, and is ended
-- its whole process group killed -- after `_git_timeout` seconds. A partial
clone is refused before any object walk.

Mutant rulings (Rev8 s4-s5, Rev9 s2). M14c, M34 and M35 are not claimed
equivalent: each has a killing test (``test_mutant_m14c``,
``test_mutant_m34``, ``test_mutant_m35``). M34 is killed at the level of its
check line: the status stays ARCHIVE_UNVERIFIED under it, because
``member_list`` fails as well, but ``external_link:<link>`` would claim a
verification that did not happen.

Equivalent mutants -- each was run against the suite and survives, for the
reason given:

- M19c (the result of the post-fetch ``git bundle verify`` ignored): the
  verifier fetches from the bundle into its fresh repository first, and a
  bundle that ``git bundle verify`` rejects (missing prerequisites, a
  corrupt header) is also rejected by ``git fetch``, which runs the same
  prerequisite check and indexes the pack; that fetch-failed branch reports
  ``git_bundle_verify`` failed on its own, so the post-fetch result is
  shadowed. The fetch-failed branch is killed by
  ``test_mutant_git_bundle_verify``.
- M14d alone (the extraction filter's parent-realpath check removed while the
  ``..`` refusal stays): a member's parent can leave ``extract_root`` only
  through a ``..`` component or a symlink component, and both are refused
  before this check runs. The combination M13b+M14d is killed by
  ``test_extraction_refuses_dotdot_member``.
- The parent-realpath-only mutant of the extraction check (sarch2-b N1a): the
  same mutation seen from the symlink side -- a parent can only leave
  ``extract_root`` through a symlink component, which is refused first
  (``test_extraction_refuses_parent_escape_link`` asserts that refusal).

Shadowed guards of the git facts (Rev10 s4) -- each survives its removal,
for the reason given (QA runs each against the suite), except the one
marked otherwise:

- ``-c core.untrackedCache=false``: shadowed by the computed walk --
  ``computed_tree_equal`` reads the working tree itself and never consults
  the untracked cache, and no remaining fact command lists untracked files.
- ``-c core.ignoreStat=false``: shadowed by ``index_bits_clear`` and
  ``ignore_stat_unset`` -- a repository with ``core.ignoreStat`` set is
  never ``worktree_clean`` whatever git reports, and no fact is taken from
  git's stat comparison any more.
- ``-c core.hooksPath=/dev/null``: no fact command runs a hook (none of
  them commits, checks out, merges or writes the index), so there is no
  hook for it to suppress; it stays as a guard.
- ``GIT_NO_REPLACE_OBJECTS``: shadowed by ``refs_in_bundle`` together with
  the scrub of ``GIT_REPLACE_REF_BASE`` -- a ``refs/replace/*`` ref is a ref
  that ``for-each-ref`` lists, so a repository with a replacement is never
  ``all_refs_included`` unless the replace ref is bundled by name, and the
  verifier then fetches that same ref. It stays so that recording and
  verification always read the same objects.
- The empty-directory rule of the walk was listed as surviving (sarch3-a
  minor 2) but is **not** equivalent: an empty directory is invisible to
  both sides of the comparison with ``ls-tree -r``, so nothing else makes
  ``computed_tree_equal`` false for it. It now has a killing test,
  ``test_mutant_empty_directory``.
- The status-stderr rule (``git status`` output on stderr makes a fact
  false) was withdrawn together with the ``status`` command itself
  (Rev10a s1); ``worktree_clean`` is the computed comparison plus the
  index-bit, ``ignoreStat`` and ``no_conversion`` measures.
"""

from __future__ import annotations

import base64
import collections
import ctypes
import dataclasses
import datetime
import errno
import hashlib
import io
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import tarfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

SCHEMA = "veriharness.archive-manifest/1"
RECEIPT_SCHEMA = "veriharness.archive-receipt/1"

TREE_PAYLOAD = "payload.tar.zst"
GIT_PAYLOAD = "payload.git.bundle"
MANIFEST = "MANIFEST.json"
SUMS = "SHA256SUMS"
INCOMPLETE = "INCOMPLETE"
RECEIPT = "ARCHIVE_RECEIPT.json"
EXTERNAL_DIR = ".vh-external"

ARCHIVED_VERIFIED = "ARCHIVED_VERIFIED"
ARCHIVE_UNVERIFIED = "ARCHIVE_UNVERIFIED"

PAGE_CACHE_NOTE = (
    "every record was read through a fresh file descriptor, with "
    "posix_fadvise(POSIX_FADV_DONTNEED) where available; page-cache effects "
    "are reduced by that, not excluded"
)

_LABEL = re.compile(r"[a-z0-9-]{1,40}")
_BUNDLE_NAME = re.compile(r"vh-arch-\d{8}T\d{6}Z-[0-9a-f]{12}-[a-z0-9-]{1,40}")
_STORED = re.compile(r"\.vh-external/[0-9]+")
_CHUNK = 1 << 20
_SUMS_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)")

#: The fields `content_digest` covers (Rev2 s5): what survives a round trip.
#: `mtime_ns` and inode numbers stay in the manifest as information only.
_DIGEST_KEYS = ("path", "path_b64", "type", "mode", "size", "sha256",
                "link_target", "link_target_b64", "hardlink_group")

#: Private test seam. When set, it is called as
#: ``_after_payload_hook(source=..., bundle_dir=...)`` after the payload of a
#: tree bundle has been written and before the source is re-stat'ed, so a test
#: can change the source at exactly that point. Never set outside tests.
_after_payload_hook = None


class ArchiveError(Exception):
    """Base of every error this module raises on purpose."""


class ArchiveRefused(ArchiveError):
    """The input may not be archived, or a record may not be written."""


class SourceChanged(ArchiveError):
    """The source changed while it was being archived.

    ``bundle_dir`` is the bundle directory, left standing with ``INCOMPLETE``
    in it.
    """

    def __init__(self, message: str, bundle_dir: Path | None = None):
        super().__init__(message)
        self.bundle_dir = bundle_dir


class BuildCancelled(ArchiveError):
    """`build_tree`'s ``cancel`` returned true before an entry was written:
    the builder wrote nothing more and left the bundle directory standing
    with ``INCOMPLETE`` in it (``bundle_dir``). Nothing is removed."""

    def __init__(self, message: str, bundle_dir: Path | None = None):
        super().__init__(message)
        self.bundle_dir = bundle_dir


class ArchiveDependencyMissing(ArchiveError):
    """`zstandard` is not installed; there is no fallback format."""


def _zstd():
    try:
        import zstandard
    except ImportError as exc:
        raise ArchiveDependencyMissing(
            "payload.tar.zst needs the optional dependency zstandard: "
            "pip install 'hoh[archive]' (there is no fallback format)"
        ) from exc
    return zstandard


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _iso(moment: datetime.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("hoh")
        except PackageNotFoundError:
            return "unknown"
    except ImportError:
        return "unknown"


def _check_label(label: str) -> None:
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise ArchiveRefused(
            "label must match [a-z0-9-]{1,40} so the bundle name is exFAT-safe: "
            + repr(label)
        )


def _name_fields(raw: bytes, key: str) -> dict[str, str]:
    """``{key: text}`` when `raw` is UTF-8, else ``{key_b64: base64}``."""
    try:
        return {key: raw.decode("utf-8")}
    except UnicodeDecodeError:
        return {key + "_b64": base64.b64encode(raw).decode("ascii")}


def _raw_field(record: Mapping, key: str) -> bytes | None:
    if record.get(key + "_b64") is not None:
        return base64.b64decode(record[key + "_b64"], validate=True)
    value = record.get(key)
    if value is None:
        return None
    return value.encode("utf-8")


def _entry_raw(entry: Mapping) -> bytes:
    raw = _raw_field(entry, "path")
    if raw is None:
        raise ValueError("manifest entry without a path")
    return raw


def _show(raw: bytes) -> str:
    return raw.decode("utf-8", "backslashreplace")


def _inside(path: str | bytes, root: str | bytes) -> bool:
    """`path` is `root` or lies below it (both already absolute and normal)."""
    path, root = os.fsdecode(path), os.fsdecode(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _lexically_inside(link_dir: bytes, target: bytes) -> bool:
    """normpath(link_dir/target), relative to the archived root, stays in it."""
    if os.path.isabs(target):
        return False
    joined = os.path.normpath(os.path.join(link_dir, target) if link_dir else target)
    return not (joined == b".." or joined.startswith(b"../") or os.path.isabs(joined))


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _projection(entry: Mapping) -> dict:
    return {key: entry.get(key) for key in _DIGEST_KEYS}


def _content_digest(entries: list[Mapping]) -> str:
    """SHA-256 over the canonical JSON of the projected entries, sorted by
    raw path bytes (Rev2 s5, Rev5)."""
    ordered = sorted(entries, key=_entry_raw)
    return hashlib.sha256(_canonical([_projection(e) for e in ordered])).hexdigest()


def _fadvise_dontneed(fd: int) -> None:
    if hasattr(os, "posix_fadvise"):
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass


def _open_fresh(path: str | bytes | Path) -> int:
    """A fresh read-only descriptor that never follows a final symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    _fadvise_dontneed(fd)
    return fd


def _hash_path(path: str | bytes | Path) -> tuple[str, int]:
    """SHA-256 and byte count of the file at `path`, read freshly.

    The verifier re-hashes every extracted file through this one helper.
    """
    fd = _open_fresh(path)
    try:
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        return digest.hexdigest(), size
    finally:
        os.close(fd)


def _read_fresh(path: str | bytes | Path) -> bytes:
    fd = _open_fresh(path)
    try:
        parts = []
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            parts.append(chunk)
        return b"".join(parts)
    finally:
        os.close(fd)


def _write_new(path: Path, data: bytes) -> None:
    """Create `path` exclusively, write `data`, fsync. Never replaces."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(errno.ENOSPC, "write made no progress", str(path))
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


_RENAME_NOREPLACE = 1
_AT_FDCWD = -100

#: Private test seams. The builder reads every identity it compares through
#: these two names, so a test can report a different device, inode or size
#: for exactly one path or descriptor without a second filesystem.
_lstat = os.lstat
_fstat = os.fstat


def _renameat2():
    """libc's ``renameat2``, or None where libc does not offer it.

    A private seam: a test replaces it to exercise the hard-link fallback.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    return getattr(libc, "renameat2", None)


def _rename_noreplace(src: Path, dst: Path) -> None:
    """Publish `src` under the name `dst`, refusing if `dst` exists -- never
    replacing it, and never by a check followed by an ordinary rename.

    ``renameat2(RENAME_NOREPLACE)`` is used where libc and the filesystem
    offer it. Otherwise a file is published with ``os.link(src, dst)``, which
    itself fails if `dst` exists; the temporary name then stays as a second
    hard link to the same inode (no extra space -- this module removes
    nothing). A directory is published by creating `dst` with a fresh
    ``os.mkdir`` and hard-linking each of its files into it; the staging
    directory stays as it is. Where the filesystem offers no hard links
    either (exFAT, for example), publishing fails closed with
    `ArchiveRefused`; a `dst` that already exists raises `FileExistsError`.
    """
    func = _renameat2()
    if func is not None:
        rc = func(_AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst),
                  _RENAME_NOREPLACE)
        if rc == 0:
            return
        err = ctypes.get_errno()
        if err not in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
            raise OSError(err, os.strerror(err), os.fspath(dst))
    if stat.S_ISDIR(os.lstat(src).st_mode):
        _publish_dir_by_links(Path(src), Path(dst))
    else:
        _link_noreplace(src, dst)


def _link_noreplace(src: str | bytes | Path, dst: str | bytes | Path) -> None:
    try:
        os.link(src, dst, follow_symlinks=False)
    except FileExistsError:
        raise
    except OSError as exc:
        raise ArchiveRefused(
            "cannot publish " + os.fsdecode(dst) + " without replacing: neither "
            "renameat2(RENAME_NOREPLACE) nor hard links are available here ("
            + str(exc) + "); failing closed") from exc


def _publish_dir_by_links(src: Path, dst: Path) -> None:
    names = sorted(os.listdir(src))
    for name in names:
        if not stat.S_ISREG(os.lstat(src / name).st_mode):
            raise ArchiveRefused("cannot publish " + os.fspath(dst) + " by links: "
                                 + name + " is not a regular file")
    os.mkdir(dst)                       # raises FileExistsError if dst exists
    try:
        for name in names:
            _link_noreplace(src / name, dst / name)
    except BaseException as exc:
        _mark_incomplete(dst, "publishing by links stopped: " + str(exc))
        raise
    _fsync_dir(dst)


def _sums_text(payload_name: str, payload_sha: str, manifest_sha: str) -> bytes:
    return (f"{payload_sha}  {payload_name}\n"
            f"{manifest_sha}  {MANIFEST}\n").encode("ascii")


def _manifest_bytes(manifest: Mapping) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True)
            + "\n").encode("ascii")


def _staging_dir(out_parent: Path, stamp: str, label: str) -> Path:
    """A fresh build directory; renamed to the final name once the content
    digest is known. Created exclusively, so nothing existing is reused."""
    out_parent = Path(out_parent)
    if not out_parent.is_dir():
        raise ArchiveRefused("out_parent is not a directory: " + str(out_parent))
    staging = out_parent / ("vh-arch-" + stamp + "-" + secrets.token_hex(6)
                            + "-" + label + ".building")
    os.mkdir(staging)
    return staging


def _mark_incomplete(bundle_dir: Path, reason: str) -> None:
    try:
        _write_new(bundle_dir / INCOMPLETE,
                   _canonical({"incomplete": True, "reason": reason,
                               "marked_utc": _iso(_utc_now())}) + b"\n")
    except OSError:
        pass


def _finish(staging: Path, out_parent: Path, stamp: str, digest: str,
            label: str) -> Path:
    bundle_id = "vh-arch-" + stamp + "-" + digest[:12] + "-" + label
    final = Path(out_parent) / bundle_id
    _fsync_dir(staging)
    _rename_noreplace(staging, final)
    _fsync_dir(Path(out_parent))
    return final


# --------------------------------------------------------------------------- #
# building
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class _Scanned:
    raw: bytes                 # path relative to the archived root
    full: bytes                # absolute path of what is read or re-stat'ed
    st: os.stat_result
    entry: dict


class _HashingReader(io.RawIOBase):
    """Reads a descriptor once; every byte handed to the tar also goes into
    the SHA-256 that ends up in the manifest."""

    def __init__(self, fd: int):
        self._fd = fd
        self.sha = hashlib.sha256()
        self.count = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = _CHUNK
        data = os.read(self._fd, size)
        self.sha.update(data)
        self.count += len(data)
        return data


class _HashingWriter(io.RawIOBase):
    def __init__(self, raw):
        self._raw = raw
        self.sha = hashlib.sha256()

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        data = bytes(data)
        self.sha.update(data)
        self._raw.write(data)
        return len(data)


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_size == b.st_size and a.st_mtime_ns == b.st_mtime_ns
            and a.st_ctime_ns == b.st_ctime_ns and a.st_ino == b.st_ino
            and stat.S_IFMT(a.st_mode) == stat.S_IFMT(b.st_mode))


class ArchiveBundle:
    """Builds bundles. Verification is `verify_archive`; receipts are
    `write_receipt`."""

    @staticmethod
    def build_tree(source: Path, out_parent: Path, *, label: str, meta: dict,
                   ownership: Mapping, include_external: bool = False,
                   cancel: Callable[[], bool] | None = None) -> Path:
        """Archive the directory `source` into a new tree bundle under
        `out_parent` and return the bundle directory.

        `ownership` must say ``status == "OWNED"`` and name a ``root`` that
        contains `source`. Until the storage registry supplies an
        ``Ownership`` (STORAGE-D), this mapping is built by the caller and is
        **not yet a trust boundary**; no caller outside tests exists.

        The source is scanned without following symlinks. FIFOs, sockets and
        devices are refused, and so is every symlink whose target is absolute,
        escapes `source` (lexically or on real resolution) or does not exist
        -- unless `include_external`, in which case a regular-file target on
        the same device inside the ownership root is stored as
        ``.vh-external/<n>`` and the link is kept. If the source changes while
        it is archived, `SourceChanged` is raised and the bundle directory is
        left standing with ``INCOMPLETE`` in it.

        `cancel` (STORAGE-D Revision 8 item 3), when given, is called before
        each entry is written; once it returns true the builder writes no
        further entry and raises `BuildCancelled`, the bundle directory again
        left standing with ``INCOMPLETE``. It removes nothing. With
        ``cancel=None`` the build is exactly as before.
        """
        _check_label(label)
        zstd = _zstd()
        given = os.path.abspath(os.fspath(source))
        src_b = os.fsencode(given)
        src_real = os.path.realpath(src_b)
        owner_root = _ownership_root(ownership, src_real, given)
        root_st = _lstat(src_b)
        if not stat.S_ISDIR(root_st.st_mode):
            raise ArchiveRefused("source is not a directory (or is a symlink): " + given)
        out_real = os.path.realpath(os.fsencode(os.fspath(out_parent)))
        if _inside(out_real, src_real):
            raise ArchiveRefused("out_parent lies inside the source; the bundle "
                                 "would archive itself")

        root_names = sorted(os.listdir(src_b))
        scanned = ArchiveBundle._scan(src_b, src_real, root_st.st_dev,
                                      owner_root, include_external)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        created = _iso(_utc_now())
        staging = _staging_dir(Path(out_parent), stamp, label)
        try:
            payload_sha = ArchiveBundle._write_tar(zstd, scanned, staging, cancel)
            hook = _after_payload_hook
            if hook is not None:
                hook(source=Path(given), bundle_dir=staging)
            ArchiveBundle._restat_root(src_b, root_st, root_names, staging)
            for item in scanned:
                if item.full is None:
                    continue
                try:
                    now = _lstat(item.full)
                except OSError as exc:
                    raise SourceChanged("vanished while archiving: "
                                        + os.fsdecode(item.full) + " (" + str(exc) + ")",
                                        staging) from exc
                if not _same_file(item.st, now):
                    raise SourceChanged("changed while archiving: "
                                        + os.fsdecode(item.full), staging)
        except (SourceChanged, BuildCancelled) as exc:
            exc.bundle_dir = staging
            _mark_incomplete(staging, str(exc))
            raise
        except BaseException as exc:
            _mark_incomplete(staging, type(exc).__name__ + ": " + str(exc))
            raise

        entries = [item.entry for item in scanned]
        externals = [item.entry["_external"] for item in scanned
                     if "_external" in item.entry]
        for entry in entries:
            entry.pop("_external", None)
        digest = _content_digest(entries)
        bundle_id = "vh-arch-" + stamp + "-" + digest[:12] + "-" + label
        files = [e for e in entries if e["type"] == "file"]
        manifest = {
            "schema": SCHEMA,
            "payload_kind": "tree",
            "payload": TREE_PAYLOAD,
            "bundle_id": bundle_id,
            "created_utc": created,
            "veriharness_version": _version(),
            "source_path": given,
            "ownership": {"status": ownership.get("status"),
                          "root": os.fspath(ownership["root"])},
            "meta": meta,
            "entries": entries,
            "externals": externals,
            "totals": {
                "files": len(files),
                "dirs": sum(1 for e in entries if e["type"] == "dir"),
                "symlinks": sum(1 for e in entries if e["type"] == "symlink"),
                "external": len(externals),
                "logical_bytes": sum(e["size"] for e in files),
                "entry_count": len(entries),
            },
            "content_digest": digest,
            "payload_sha256": payload_sha,
        }
        try:
            body = _manifest_bytes(manifest)
            _write_new(staging / MANIFEST, body)
            _write_new(staging / SUMS, _sums_text(
                TREE_PAYLOAD, payload_sha, hashlib.sha256(body).hexdigest()))
            return _finish(staging, Path(out_parent), stamp, digest, label)
        except BaseException as exc:
            _mark_incomplete(staging, type(exc).__name__ + ": " + str(exc))
            raise

    @staticmethod
    def _restat_root(src_b: bytes, before: os.stat_result, names: list[bytes],
                     staging: Path) -> None:
        """The source root itself, not only what lies below it (Rev6 s2): its
        size, mtime_ns, ctime_ns, inode and type, and its entry listing."""
        shown = os.fsdecode(src_b)
        try:
            now = _lstat(src_b)
            now_names = sorted(os.listdir(src_b))
        except OSError as exc:
            raise SourceChanged("source root vanished while archiving: " + shown
                                + " (" + str(exc) + ")", staging) from exc
        if not _same_file(before, now):
            raise SourceChanged("source root changed while archiving (size, "
                                "mtime, ctime, inode or type): " + shown, staging)
        if now_names != names:
            added = sorted(set(now_names) - set(names))
            gone = sorted(set(names) - set(now_names))
            raise SourceChanged("source root listing changed while archiving: "
                                + shown + " added " + repr([_show(n) for n in added])
                                + ", gone " + repr([_show(n) for n in gone]), staging)

    # -- scanning ----------------------------------------------------------- #

    @staticmethod
    def _scan(src_b: bytes, src_real: bytes, dev: int, owner_root: bytes,
              include_external: bool) -> list[_Scanned]:
        found: list[tuple[bytes, os.stat_result]] = []
        stack = [b""]
        while stack:
            rel_dir = stack.pop()
            full_dir = os.path.join(src_b, rel_dir) if rel_dir else src_b
            with os.scandir(full_dir) as listing:
                names = [entry.name for entry in listing]
            for name in names:
                rel = os.path.join(rel_dir, name) if rel_dir else name
                st = _lstat(os.path.join(src_b, rel))
                found.append((rel, st))
                if stat.S_ISDIR(st.st_mode):
                    stack.append(rel)
        found.sort(key=lambda pair: pair[0])

        if include_external and any(rel == EXTERNAL_DIR.encode() for rel, _ in found):
            raise ArchiveRefused("the source already has " + EXTERNAL_DIR
                                 + "; external content could not be stored")

        out: list[_Scanned] = []
        externals: list[_Scanned] = []
        for rel, st in found:
            full = os.path.join(src_b, rel)
            shown = os.fsdecode(full)
            if st.st_dev != dev:
                raise ArchiveRefused("entry on another device: " + shown)
            mode = st.st_mode
            entry: dict[str, Any] = dict(_name_fields(rel, "path"))
            entry.update(mode=stat.S_IMODE(mode), mtime_ns=st.st_mtime_ns,
                         sha256=None, link_target=None, hardlink_group=None)
            if stat.S_ISREG(mode):
                entry.update(type="file", size=st.st_size)
            elif stat.S_ISDIR(mode):
                entry.update(type="dir", size=0)
            elif stat.S_ISLNK(mode):
                target = os.readlink(full)
                entry.update(type="symlink", size=len(target))
                entry.pop("link_target")
                entry.update(_name_fields(target, "link_target"))
                why = ArchiveBundle._link_outside(full, rel, target, src_real)
                entry["link_inside"] = why is None
                if why is not None:
                    if not include_external:
                        raise ArchiveRefused(
                            "symlink target " + why + ": " + shown + " -> "
                            + os.fsdecode(target)
                            + " (the archive would hold the link, not the content)")
                    externals.append(ArchiveBundle._external(
                        full, rel, target, entry, dev, owner_root, len(externals)))
            else:
                raise ArchiveRefused("not a file, directory or symlink "
                                     "(FIFO, socket or device): " + shown)
            out.append(_Scanned(rel, full, st, entry))

        if externals:
            synthetic = {"path": EXTERNAL_DIR, "type": "dir", "mode": 0o755,
                         "size": 0, "mtime_ns": 0, "sha256": None,
                         "link_target": None, "hardlink_group": None,
                         "synthetic": True}
            out.append(_Scanned(EXTERNAL_DIR.encode(), None, None, synthetic))
            out.extend(externals)
        out.sort(key=lambda item: item.raw)

        groups: dict[tuple[int, int], list[int]] = {}
        for index, item in enumerate(out):
            if item.entry["type"] == "file" and item.st is not None and item.st.st_nlink > 1:
                groups.setdefault((item.st.st_dev, item.st.st_ino), []).append(index)
        for members in groups.values():
            if len(members) > 1:
                for index in members:
                    out[index].entry["hardlink_group"] = members[0]
        return out

    @staticmethod
    def _link_outside(full: bytes, rel: bytes, target: bytes,
                      src_real: bytes) -> str | None:
        """None when the link is inside `source` both lexically and on real
        resolution (Rev2 s2) -- so a chain is judged by where it really
        leads; otherwise the reason it is not."""
        if os.path.isabs(target):
            return "is absolute"
        if not _lexically_inside(os.path.dirname(rel), target):
            return "escapes the source"
        try:
            real = os.path.realpath(full, strict=True)
        except OSError:
            return "does not exist (dangling or a loop)"
        if not _inside(real, src_real):
            return "escapes the source on real resolution"
        return None

    @staticmethod
    def _external(full: bytes, rel: bytes, target: bytes, link_entry: dict,
                  dev: int, owner_root: bytes, n: int) -> _Scanned:
        shown = os.fsdecode(full) + " -> " + os.fsdecode(target)
        try:
            real = os.path.realpath(full, strict=True)
            st = _lstat(real)
        except OSError as exc:
            raise ArchiveRefused("external symlink target does not exist: "
                                 + shown) from exc
        if not stat.S_ISREG(st.st_mode):
            raise ArchiveRefused("external symlink target is not a regular file "
                                 "(directory, FIFO, socket or device): " + shown)
        if st.st_dev != dev:
            raise ArchiveRefused("external symlink target is on another device: " + shown)
        if not _inside(real, owner_root):
            raise ArchiveRefused("external symlink target lies outside the "
                                 "ownership root: " + shown)
        stored = EXTERNAL_DIR + "/" + str(n)
        entry = {"path": stored, "type": "file", "mode": stat.S_IMODE(st.st_mode),
                 "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": None,
                 "link_target": None, "hardlink_group": None,
                 "external_of": link_entry.get("path") or link_entry.get("path_b64")}
        link_entry["external"] = stored
        record = dict(_name_fields(rel, "link"))
        record.update(_name_fields(target, "link_target"))
        record.update(stored=stored, target_realpath=os.fsdecode(real))
        link_entry["_external"] = record
        return _Scanned(stored.encode(), real, st, entry)

    # -- writing ------------------------------------------------------------ #

    @staticmethod
    def _write_tar(zstd, scanned: list[_Scanned], staging: Path,
                   cancel: Callable[[], bool] | None = None) -> str:
        fd = os.open(staging / TREE_PAYLOAD,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644)
        with os.fdopen(fd, "wb") as raw:
            hashed = _HashingWriter(raw)
            cctx = zstd.ZstdCompressor(write_checksum=True)
            with cctx.stream_writer(hashed, closefd=False) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|",
                                  format=tarfile.PAX_FORMAT, encoding="utf-8",
                                  errors="surrogateescape") as tar:
                    for item in scanned:
                        if cancel is not None and cancel():
                            raise BuildCancelled("cancelled before "
                                                 + os.fsdecode(item.raw) + " was written")
                        ArchiveBundle._add(tar, item)
            raw.flush()
            os.fsync(raw.fileno())
        externals = {item.raw: item.entry for item in scanned
                     if item.entry.get("external_of") is not None}
        for item in scanned:
            record = item.entry.get("_external")
            if record is not None:
                record["sha256"] = externals[record["stored"].encode()]["sha256"]
                record["size"] = externals[record["stored"].encode()]["size"]
        return hashed.sha.hexdigest()

    @staticmethod
    def _add(tar: tarfile.TarFile, item: _Scanned) -> None:
        entry = item.entry
        info = tarfile.TarInfo(os.fsdecode(item.raw))
        info.mode = entry["mode"]
        info.mtime = entry["mtime_ns"] // 1_000_000_000
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        kind = entry["type"]
        if kind == "dir":
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
        elif kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = os.fsdecode(_raw_field(entry, "link_target"))
            tar.addfile(info)
        else:
            info.type = tarfile.REGTYPE
            info.size = entry["size"]
            fd = os.open(item.full, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                now = _fstat(fd)
                if (now.st_dev, now.st_ino, now.st_size) != (
                        item.st.st_dev, item.st.st_ino, item.st.st_size):
                    raise SourceChanged("replaced or resized before it was read: "
                                        + os.fsdecode(item.full))
                reader = _HashingReader(fd)
                try:
                    tar.addfile(info, reader)
                except OSError as exc:
                    raise SourceChanged("shrank while it was read: "
                                        + os.fsdecode(item.full)) from exc
                if os.read(fd, 1):
                    raise SourceChanged("grew while it was read: "
                                        + os.fsdecode(item.full))
            finally:
                os.close(fd)
            entry["sha256"] = reader.sha.hexdigest()

    # -- git ---------------------------------------------------------------- #

    @staticmethod
    def build_git(repo: Path, refs: list[str], out_parent: Path, *, label: str,
                  meta: dict, ownership: Mapping) -> Path:
        """Archive `refs` of the git repository `repo` as ``git bundle create``.

        `ownership` is the same gate as for `build_tree` (status OWNED, the
        resolved repository inside the resolved root; not yet a trust
        boundary, see there). Before anything is written the build is refused
        (`ArchiveRefused`) when ``git rev-parse --show-toplevel`` is not the
        resolved `repo` or ``core.worktree`` is set; when the git common dir
        (``--path-format=absolute --git-common-dir``), its ``objects``
        directory or any object store reached through ``objects/info/
        alternates`` (recursively; relative entries against their own
        ``objects`` directory) lies outside the ownership root; and when
        `out_parent` lies inside the repository's worktree.

        A partial clone (``extensions.partialClone``, a
        ``remote.<name>.promisor``, or a ``*.promisor`` pack in any object
        store) is refused before any object-walking command runs.

        Every git call names the repository with ``-C``, runs under
        `_git_env` (no inherited repository, object store, template or
        configuration; no replace objects, no lazy fetch), carries
        `_FACT_FLAGS`, starts in its own session and is ended -- its process
        group killed -- after `_git_timeout` seconds, which surfaces as
        `ArchiveRefused`. Only the commands of the module docstring's closed
        list run; none of them applies a filter or runs a
        repository-configured program.

        The manifest records each ref's unpeeled object id, the commit and
        the tree it peels to, and the facts below, each measure as its own
        boolean under ``worktree_facts``. This is the facts' **complete
        definition** (Rev9b s5); a further way to make one lie is a finding
        against this list:

        - ``worktree_clean`` = ``computed_tree_equal`` (the archiver walks
          the repository directory itself, excluding only the top-level
          ``.git``, computes every entry's git mode and blob id in Python --
          symlinks by their link text -- and the result must equal ``git
          ls-tree -r -z --full-tree HEAD`` byte for byte; an empty
          directory, a FIFO, socket or device, an entry on another device, an
          unborn HEAD, or any walk, stat, read or hash error makes it false)
          AND ``index_bits_clear`` (no assume-unchanged or skip-worktree
          entry in ``git ls-files -v``) AND ``ignore_stat_unset``
          (``core.ignoreStat`` not true in the repository's configuration)
          AND ``no_conversion`` (`_no_conversion`: computed from the
          effective configuration -- ``config --list --show-scope
          --includes`` -- and attribute files; any include, filter, LFS,
          ``attr.tree``, attributes-file, eol or autocrlf setting, any
          converting attribute, or a ``.gitattributes`` staged in the index
          that differs from the working tree's makes it false). The
          ``status`` fact command of Rev9a is withdrawn (Rev10a s1).
        - ``all_refs_included`` = ``refs_in_bundle`` (every ref ``git
          for-each-ref`` lists, in every namespace -- notes, ``refs/stash``
          and custom ``refs/*`` included -- is in the bundle under its own
          name, with the same object id) AND ``detached_head_reachable``
          (a detached HEAD is reachable from a bundled ref) AND
          ``worktree_heads_reachable`` (the HEAD of every worktree in ``git
          worktree list --porcelain`` is reachable from a bundled ref).
        - ``stash_empty`` = ``stash_ref_absent`` (``git rev-parse --verify
          -q refs/stash`` exits 1) AND ``stash_reflog_empty`` (``git reflog
          show --format=%H refs/stash`` prints nothing; its non-zero exit for
          a missing ref counts as empty only together with
          ``stash_ref_absent``). ``git stash list`` is not run (Rev10b s1).
        - ``no_unbundled_objects``: ``git rev-list --objects --all --reflog
          --not <every bundled tip>`` prints nothing.
        - ``no_gitlinks``: ``git ls-files -s`` has no mode-160000 entry.
        - ``single_worktree``: ``git worktree list --porcelain`` lists
          exactly one worktree.

        ``covers_worktree`` is the AND of these six facts. All of them are
        taken before ``git bundle create`` and again after it returns,
        together with every recorded ref (unpeeled and peeled) and the whole
        ``for-each-ref`` listing; any difference raises `SourceChanged`, and
        so does a recorded ref that is not exactly as recorded in either
        pass (Rev10 s1: equal between the passes is not enough). Either
        leaves the staging directory marked ``INCOMPLETE`` -- never
        published, never receipted.

        Scope of the claim: ``covers_worktree`` never covers ``.git``
        metadata (configuration, remotes, ``info/exclude``, hooks), so
        STORAGE-D must not free a repository on this fact alone. A repository
        using ``core.autocrlf``, ``core.eol``, eol or text attributes, clean
        or smudge filters or LFS is never ``covers_worktree``
        (``no_conversion`` fails closed), and permission bits other than the
        executable bit are not covered. The computed comparison reads the
        whole working tree, twice per build; on a multi-GB repository this is
        slow, which is accepted for correctness (Rev9d s7).

        A bundle that does not cover the worktree is an archive of its refs,
        never a substitute for the worktree.
        """
        _check_label(label)
        if not refs or not all(isinstance(r, str) and r and not r.startswith("-")
                               for r in refs):
            raise ArchiveRefused("refs must be a non-empty list of ref names")
        repo_abs = os.path.abspath(os.fspath(repo))
        repo_real = os.path.realpath(os.fsencode(repo_abs))
        owner_root = _ownership_root(ownership, repo_real, repo_abs)
        _git_gates(repo_abs, repo_real, owner_root, Path(out_parent))

        recorded = []
        for ref in refs:
            # No --end-of-options here: git 2.43 echoes it in this mode. Refs
            # starting with "-" were refused above, so none can pose as an option.
            full_name = _git(repo_abs, "rev-parse", "--symbolic-full-name", ref).strip()
            if not full_name or "\n" in full_name:
                raise ArchiveRefused("not a ref name (a bundle needs refs): " + ref)
            # `git bundle create` lists HEAD under its own name, not the branch
            # it points to, so the record keeps that name.
            name = "HEAD" if ref == "HEAD" else full_name
            unpeeled = _git(repo_abs, "rev-parse", "--verify", "--end-of-options",
                            ref).strip()
            commit = _git(repo_abs, "rev-parse", "--verify", "--end-of-options",
                          ref + "^{commit}").strip()
            tree = _git(repo_abs, "rev-parse", "--verify", "--end-of-options",
                        ref + "^{tree}").strip()
            recorded.append({"ref": ref, "name": name, "full_name": full_name,
                             "unpeeled": unpeeled, "commit": commit, "tree": tree})
        if _after_record_hook is not None:
            _after_record_hook(repo=repo, recorded=recorded)
        digest = _git_digest(recorded)

        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        created = _iso(_utc_now())
        staging = _staging_dir(Path(out_parent), stamp, label)
        try:
            facts, measures, before = _git_facts(repo_abs, repo_real, recorded)
            _refs_still_recorded(before, recorded, "before git bundle create", staging)
            payload = staging / GIT_PAYLOAD
            _git(repo_abs, "bundle", "create", "--quiet", os.fspath(payload), *refs)
            # S17: the facts must still hold after the bundle is written.
            facts_after, measures_after, after = _git_facts(repo_abs, repo_real, recorded)
            changed = sorted(key for key in set(before) | set(after)
                             if before.get(key) != after.get(key))
            changed += ["fact " + key for key in facts if facts[key] != facts_after.get(key)]
            changed += ["measure " + key for key in measures
                        if measures[key] != measures_after.get(key)]
            if changed:
                raise SourceChanged("the repository changed while git bundle create "
                                    "ran: " + ", ".join(changed), staging)
            _refs_still_recorded(after, recorded, "after git bundle create", staging)
            payload_sha, _ = _hash_path(payload)
            bundle_id = "vh-arch-" + stamp + "-" + digest[:12] + "-" + label
            manifest = {
                "schema": SCHEMA,
                "payload_kind": "git",
                "payload": GIT_PAYLOAD,
                "bundle_id": bundle_id,
                "created_utc": created,
                "veriharness_version": _version(),
                "source_path": repo_abs,
                "meta": meta,
                "refs": recorded,
                "worktree_facts": measures,
                **facts,
                "covers_worktree": all(facts.values()),
                "content_digest": digest,
                "payload_sha256": payload_sha,
            }
            body = _manifest_bytes(manifest)
            _write_new(staging / MANIFEST, body)
            _write_new(staging / SUMS, _sums_text(
                GIT_PAYLOAD, payload_sha, hashlib.sha256(body).hexdigest()))
            return _finish(staging, Path(out_parent), stamp, digest, label)
        except SourceChanged as exc:
            exc.bundle_dir = staging
            _mark_incomplete(staging, str(exc))
            raise
        except BaseException as exc:
            _mark_incomplete(staging, type(exc).__name__ + ": " + str(exc))
            raise


def _refs_still_recorded(state: Mapping, recorded: list[Mapping], when: str,
                         staging: Path) -> None:
    """Every recorded ref must still be what was recorded -- unpeeled, commit
    and tree -- in this fact pass (Rev10 s1); equality between the two passes
    alone would accept a ref that moved before the first one."""
    moved = [str(r["ref"]) for r, ok in zip(recorded, state["refs as recorded"]) if not ok]
    if moved or len(state["refs as recorded"]) != len(recorded):
        raise SourceChanged("a recorded ref no longer holds " + when + " (refs as "
                            "recorded): " + ", ".join(moved), staging)


def _ownership_root(ownership: Mapping, real: bytes, shown: str) -> bytes:
    """The ownership gate in front of every build (Rev2 s3, Rev6 s6): status
    OWNED, and `real` (already resolved) inside the resolved ``root``.
    Returns that resolved root.

    Until the storage registry supplies an ``Ownership`` (STORAGE-D) the
    mapping is built by the caller and is not yet a trust boundary.
    """
    if not isinstance(ownership, Mapping) or ownership.get("status") != "OWNED":
        raise ArchiveRefused("ownership is not proven: status must be OWNED")
    if not ownership.get("root"):
        raise ArchiveRefused("ownership names no root")
    owner_root = os.path.realpath(os.fsencode(os.fspath(ownership["root"])))
    if not _inside(real, owner_root):
        raise ArchiveRefused("source lies outside the ownership root: " + shown)
    return owner_root


#: Variables that would let the caller's environment choose the repository,
#: its object store, its index or its configuration (Rev9 s1, Rev9a s2).
#: ``GIT_CONFIG_KEY_*`` and ``GIT_CONFIG_VALUE_*`` are removed by prefix.
_GIT_SCRUB = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
              "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
              "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES", "GIT_CONFIG",
              "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_SHALLOW_FILE",
              "GIT_ATTR_SOURCE", "GIT_REPLACE_REF_BASE", "GIT_QUARANTINE_PATH",
              "GIT_TEMPLATE_DIR")


def _git_env() -> dict:
    """The one environment every git subprocess of this module runs under.

    Nothing that locates a repository, an object store, an index or a
    configuration is inherited; global and system configuration are off, and
    replace objects and grafts are ignored, for recording and verification
    alike (Rev9, Rev9a s2, Rev9d). No template directory is inherited, and
    no missing object is fetched lazily (Rev10b s5-s6; ``GIT_NO_LAZY_FETCH``
    is honoured from git 2.44 on -- before that the partial-clone refusal of
    `_git_gates` is what holds).
    """
    env = {key: value for key, value in os.environ.items()
           if key not in _GIT_SCRUB
           and not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
               GIT_GRAFT_FILE="/dev/null", GIT_NO_REPLACE_OBJECTS="1",
               GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    return env


#: GIT_GRAFT_FILE=/dev/null makes git print a deprecation hint on stderr;
#: a fact whose command must leave stderr empty would read it as a problem.
_QUIET = ("-c", "advice.graftFileDeprecated=false")


#: Passed to every git command of this module, builder and verifier alike
#: (Rev9b s6, Rev10b s2, s6-s7): configuration cannot hide changes from a
#: fact, and no repository-configured program -- fsmonitor, hook, pager, gpg
#: -- runs inside the archiver or the verifier. Command-line ``-c`` takes
#: precedence over every configuration file, included ones too.
_FACT_FLAGS = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
               "-c", "core.fileMode=true", "-c", "core.ignoreStat=false",
               "-c", "core.hooksPath=/dev/null", "-c", "log.showSignature=false",
               "-c", "core.pager=cat")

#: Seconds after which a git subprocess is ended and its whole process group
#: killed (Rev10b s6). Private and injectable: a test sets it lower.
_git_timeout = 300.0


class _GitTimeout(ArchiveRefused):
    """A git subprocess ran longer than `_git_timeout`; its process group was
    killed and reaped. In `build_git` it surfaces as the `ArchiveRefused` it
    is (a staging directory, if one exists, marked ``INCOMPLETE``); the
    verifier turns it into a failed check."""


def _run_git(*args: str) -> subprocess.CompletedProcess:
    """Every git subprocess of this module -- builder and verifier -- goes
    through here (a private seam: a test wraps it to change the repository
    while ``git bundle create`` runs).

    The command always carries `_FACT_FLAGS` and runs under `_git_env`. It
    starts in its own session, so it and every helper it spawns form one
    process group; when it runs longer than `_git_timeout` (a FIFO named in
    ``include.path`` blocks git for ever) the whole group is killed with
    SIGKILL, the child reaped and `_GitTimeout` raised -- never a hang, and
    no helper outlives it.
    """
    command = ["git", *_QUIET, *_FACT_FLAGS, *args]
    timeout = _git_timeout
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=_git_env(), start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise _GitTimeout("git " + " ".join(_subcommand_words(args)) + " ran longer than "
                          + str(timeout) + " s; its process group was killed") from None
    except BaseException:
        _kill_group(proc)
        raise
    return subprocess.CompletedProcess(command, proc.returncode, out, err)


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the process group `proc` leads, reap `proc` and close its
    pipes (what a killed group left in them is not read)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def _subcommand_words(args: tuple) -> list[str]:
    """The subcommand and its first argument, for messages: ``-C <dir>`` and
    ``-c <key=value>`` pairs and other leading global options skipped."""
    rest = [str(arg) for arg in args]
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in ("-C", "-c") else rest[1:]
    return rest[:2]


#: Each fact of a git manifest and the measures it is the AND of; the three
#: facts without measures are measured directly.
_FACT_MEASURES = {
    "worktree_clean": ("computed_tree_equal", "index_bits_clear",
                       "ignore_stat_unset", "no_conversion"),
    "all_refs_included": ("refs_in_bundle", "detached_head_reachable",
                          "worktree_heads_reachable"),
    "stash_empty": ("stash_ref_absent", "stash_reflog_empty"),
    "no_unbundled_objects": (),
    "no_gitlinks": (),
    "single_worktree": (),
}

#: Attributes that make git convert content between the index and the
#: working tree (``no_conversion`` fails closed on any of them).
_CONVERTING_ATTRIBUTES = {b"text", b"eol", b"filter", b"crlf", b"ident",
                          b"working-tree-encoding"}

#: Private test seam: the directory walk behind ``computed_tree_equal``.
_walk = os.walk

#: Private test seam. When set, it is called as
#: ``_after_record_hook(repo=..., recorded=...)`` after `build_git` has
#: recorded the refs and before its first fact pass, so a test can move a ref
#: at exactly that point. Never set outside tests.
_after_record_hook = None


def _git_run(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return _run_git("-C", cwd, *args)


def _git(cwd: str, *args: str) -> str:
    done = _git_run(cwd, *args)
    if done.returncode != 0:
        raise ArchiveRefused("git " + " ".join(args[:2]) + " failed: "
                             + done.stderr.decode("utf-8", "replace").strip())
    return done.stdout.decode("utf-8", "replace")


def _git_config(cwd: str) -> list[tuple[str, str | None]] | None:
    """The repository's effective configuration (Rev10a s2): local, worktree
    and every included file (``--includes``; global and system configuration
    are off), keys lower-cased, a bare key as None. ``config --list`` reads
    files and runs nothing. `_FACT_FLAGS` appear in it under the scope
    ``command`` and are dropped; only the repository's own scopes are kept.
    None if unreadable."""
    done = _git_run(cwd, "config", "--list", "--show-scope", "--includes", "-z")
    if done.returncode != 0:
        return None
    fields = done.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        return None
    out = []
    for scope, item in zip(fields[0::2], fields[1::2]):
        if scope == b"command":
            continue
        key, sep, value = item.partition(b"\n")
        out.append((key.decode("utf-8", "replace").lower(),
                    value.decode("utf-8", "replace") if sep else None))
    return out


def _config_bool(value: str | None) -> bool | None:
    """git's reading of a boolean; None when it is not one."""
    if value is None:
        return True
    word = value.strip().lower()
    if word in ("true", "yes", "on", "1"):
        return True
    if word in ("false", "no", "off", "0", ""):
        return False
    return None


def _git_gates(repo_abs: str, repo_real: bytes, owner_root: bytes,
               out_parent: Path) -> None:
    """Refusals before anything is written (Rev8 s2, Rev9 s3, Rev9d s2)."""
    top = _git_run(repo_abs, "rev-parse", "--show-toplevel")
    top_real = os.path.realpath(top.stdout.rstrip(b"\n")) if top.returncode == 0 else None
    if top_real != repo_real:
        raise ArchiveRefused("not the top level of its own working tree: git rev-parse "
                             "--show-toplevel says " + repr(top.stdout.strip().decode(
                                 "utf-8", "replace")) + " for " + repo_abs)
    config = _git_config(repo_abs)
    if config is None:
        raise ArchiveRefused("the repository's configuration is unreadable: " + repo_abs)
    if any(key == "core.worktree" for key, _ in config):
        raise ArchiveRefused("core.worktree is set in the repository's configuration; "
                             "the repository is not where it says: " + repo_abs)
    common = _git_run(repo_abs, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode != 0:
        raise ArchiveRefused("git rev-parse --git-common-dir failed: "
                             + common.stderr.decode("utf-8", "replace").strip())
    common_real = os.path.realpath(common.stdout.rstrip(b"\n"))
    if not _inside(common_real, owner_root):
        raise ArchiveRefused("the git common dir lies outside the ownership root: "
                             + os.fsdecode(common_real))
    stores = _object_stores(os.path.join(common_real, b"objects"), owner_root)
    if _inside(os.path.realpath(os.fsencode(os.fspath(out_parent))), repo_real):
        raise ArchiveRefused("out_parent lies inside the repository's worktree; the "
                             "staging directory would change the facts it records")
    _refuse_partial_clone(config, stores, repo_abs)


def _refuse_partial_clone(config: list[tuple[str, str | None]], stores: list[bytes],
                          shown: str) -> None:
    """A partial clone is refused before any object-walking command runs
    (Rev10b s5): it cannot be bundled completely, and walking it would fetch
    missing objects lazily -- running a remote's configured upload-pack. Its
    signs: ``extensions.partialClone`` or any ``remote.<name>.promisor`` true
    in the effective configuration, or a ``*.promisor`` file in the pack
    directory of any object store (alternates included)."""
    for key, value in config:
        if key == "extensions.partialclone":
            raise ArchiveRefused("a partial clone (extensions.partialClone is set) cannot "
                                 "be archived completely: " + shown)
        if key.startswith("remote.") and key.endswith(".promisor") \
                and _config_bool(value) is not False:
            raise ArchiveRefused("a partial clone (" + key + " is set) cannot be archived "
                                 "completely: " + shown)
    for store in stores:
        pack = os.path.join(store, b"pack")
        try:
            names = os.listdir(pack)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ArchiveRefused("pack directory unreadable: " + str(exc)) from exc
        promisors = sorted(name for name in names if name.endswith(b".promisor"))
        if promisors:
            raise ArchiveRefused("a partial clone (promisor pack " + _show(promisors[0])
                                 + " in " + _show(pack) + ") cannot be archived "
                                 "completely: " + shown)


def _object_stores(objects: bytes, owner_root: bytes) -> list[bytes]:
    """The object directory and every store reached through
    ``objects/info/alternates``, recursively, must resolve inside the
    ownership root. Relative entries resolve against their own ``objects``
    directory; a quoted entry is not interpreted and fails closed. Returns
    every store, resolved."""
    seen: set[bytes] = set()
    stack = [(objects, "the object directory")]
    while stack:
        current, how = stack.pop()
        real = os.path.realpath(current)
        if real in seen:
            continue
        seen.add(real)
        if not _inside(real, owner_root):
            raise ArchiveRefused(how + " lies outside the ownership root: "
                                 + os.fsdecode(real))
        try:
            with open(os.path.join(current, b"info", b"alternates"), "rb") as handle:
                lines = handle.read().splitlines()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ArchiveRefused("objects/info/alternates unreadable: " + str(exc)) from exc
        for line in lines:
            if not line.strip() or line.startswith(b"#"):
                continue
            if line.startswith(b'"'):
                raise ArchiveRefused("a quoted objects/info/alternates entry is not "
                                     "followed; failing closed: " + _show(line))
            target = line if os.path.isabs(line) else os.path.join(current, line)
            stack.append((target, "an object store named in objects/info/alternates"))
    return sorted(seen)


def _reachable(cwd: str, oid: str | None, tips: list[str]) -> bool:
    """`oid` is reachable from a bundled tip."""
    if not oid or not tips or set(oid) == {"0"}:
        return False
    done = _git_run(cwd, "rev-list", "-n", "1", oid, "--not", *tips)
    return done.returncode == 0 and not done.stdout.strip() and not done.stderr.strip()


def _git_facts(repo_abs: str, repo_real: bytes,
               recorded: list[dict]) -> tuple[dict, dict, dict]:
    """Every fact and measure of `build_git`'s docstring, plus the state the
    post-create re-check compares: ``(facts, measures, state)``."""
    measures: dict[str, bool] = {}
    state: dict[str, Any] = {}
    config = _git_config(repo_abs)
    state["config"] = config

    tree_ok, tree_state, attributes, found = _computed_tree(repo_abs, repo_real)
    measures["computed_tree_equal"] = tree_ok
    state["worktree content"] = tree_state
    listed = _git_run(repo_abs, "ls-files", "-v", "-z")
    tags = [record[:1] for record in listed.stdout.split(b"\0") if record]
    measures["index_bits_clear"] = (listed.returncode == 0 and not any(
        tag.islower() or tag == b"S" for tag in tags))
    measures["ignore_stat_unset"] = config is not None and all(
        _config_bool(value) is False for key, value in config if key == "core.ignorestat")
    staged = _git_run(repo_abs, "ls-files", "-s", "-z")
    measures["no_conversion"] = (config is not None and attributes is not None
                                 and staged.returncode == 0
                                 and _no_conversion(repo_abs, config, attributes,
                                                    found, staged.stdout))

    tips = sorted({r["unpeeled"] for r in recorded})
    bundled = {r["full_name"]: r["unpeeled"] for r in recorded}
    refs = _git_run(repo_abs, "for-each-ref", "--format=%(objectname) %(refname)")
    rows = [line.split(" ", 1) for line in refs.stdout.decode("utf-8", "replace").splitlines()]
    measures["refs_in_bundle"] = refs.returncode == 0 and all(
        len(row) == 2 and bundled.get(row[1]) == row[0] for row in rows)
    state["for-each-ref"] = refs.stdout
    symbolic = _git_run(repo_abs, "symbolic-ref", "-q", "HEAD")
    if symbolic.returncode == 0:
        measures["detached_head_reachable"] = True
        state["HEAD"] = symbolic.stdout
    else:
        head = _git_run(repo_abs, "rev-parse", "--verify", "-q", "HEAD")
        oid = head.stdout.decode("ascii", "replace").strip() if head.returncode == 0 else None
        measures["detached_head_reachable"] = (symbolic.returncode == 1
                                               and _reachable(repo_abs, oid, tips))
        state["HEAD"] = (symbolic.returncode, oid)
    worktrees = _git_run(repo_abs, "worktree", "list", "--porcelain", "-z")
    records: list[dict[str, str]] = [{}]
    for field in worktrees.stdout.decode("utf-8", "replace").split("\0"):
        if not field:
            records.append({})
            continue
        key, _, value = field.partition(" ")
        records[-1][key] = value
    records = [r for r in records if "worktree" in r]
    measures["worktree_heads_reachable"] = (worktrees.returncode == 0 and bool(records)
                                            and all(_reachable(repo_abs, r.get("HEAD"), tips)
                                                    for r in records))
    state["worktree list"] = worktrees.stdout

    stash_ref = _git_run(repo_abs, "rev-parse", "--verify", "-q", "refs/stash")
    measures["stash_ref_absent"] = stash_ref.returncode == 1 and not stash_ref.stdout.strip()
    # `reflog show` with a fixed format, not `stash list`: no pretty format
    # from the configuration and no signature check (gpg) can run (Rev10b s1).
    # It exits non-zero when refs/stash does not exist; that counts as an
    # empty reflog only together with stash_ref_absent.
    reflog = _git_run(repo_abs, "reflog", "show", "--format=%H", "refs/stash")
    measures["stash_reflog_empty"] = not reflog.stdout.strip() and (
        reflog.returncode == 0 or measures["stash_ref_absent"])
    state["stash reflog"] = (reflog.returncode, reflog.stdout)

    facts = {fact: all(measures[name] for name in names)
             for fact, names in _FACT_MEASURES.items() if names}
    unbundled = _git_run(repo_abs, "rev-list", "--objects", "--all", "--reflog",
                         "--not", *tips)
    facts["no_unbundled_objects"] = (unbundled.returncode == 0 and bool(tips)
                                     and not unbundled.stdout.strip()
                                     and not unbundled.stderr.strip())
    state["unbundled"] = unbundled.stdout
    facts["no_gitlinks"] = staged.returncode == 0 and not any(
        record.startswith(b"160000 ") for record in staged.stdout.split(b"\0"))
    facts["single_worktree"] = worktrees.returncode == 0 and len(records) == 1

    reparsed = []
    for r in recorded:
        row = [r["ref"]]
        for spec in (r["ref"], r["ref"] + "^{commit}", r["ref"] + "^{tree}"):
            done = _git_run(repo_abs, "rev-parse", "--verify", "-q", "--end-of-options", spec)
            row.append(done.stdout.decode("ascii", "replace").strip()
                       if done.returncode == 0 else None)
        reparsed.append(row)
    state["refs"] = reparsed
    state["refs as recorded"] = [row[1:] == [r["unpeeled"], r["commit"], r["tree"]]
                                 for row, r in zip(reparsed, recorded)]
    return facts, measures, state


def git_facts(repo: Path, ownership: Mapping, recorded_refs: list[Mapping]) -> dict:
    """The facts `ArchiveBundle.build_git` records, measured now at `repo`
    (STORAGE-D, Revision 5 item 3): a public wrapper, no behaviour of its own.

    `ownership` is the same gate as for the builders (status OWNED, the
    resolved repository inside the resolved root), and the same refusals as
    `build_git` apply before any fact command runs -- not the top level of
    its own worktree, ``core.worktree`` set, the common dir or an object
    store outside the ownership root, a partial clone -- except the
    `out_parent` check, which only a build has. `recorded_refs` are the refs
    as a verified manifest records them (``ref``, ``full_name``,
    ``unpeeled``, ``commit``, ``tree``). Returns ``{"facts", "measures",
    "covers_worktree", "refs_as_recorded"}``; ``covers_worktree`` is the AND
    of the six facts, exactly as in a git manifest.
    """
    repo_abs = os.path.abspath(os.fspath(repo))
    repo_real = os.path.realpath(os.fsencode(repo_abs))
    owner_root = _ownership_root(ownership, repo_real, repo_abs)
    top = _git_run(repo_abs, "rev-parse", "--show-toplevel")
    top_real = os.path.realpath(top.stdout.rstrip(b"\n")) if top.returncode == 0 else None
    if top_real != repo_real:
        raise ArchiveRefused("not the top level of its own working tree: " + repo_abs)
    config = _git_config(repo_abs)
    if config is None:
        raise ArchiveRefused("the repository's configuration is unreadable: " + repo_abs)
    if any(key == "core.worktree" for key, _ in config):
        raise ArchiveRefused("core.worktree is set in the repository's configuration: "
                             + repo_abs)
    common = _git_run(repo_abs, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode != 0:
        raise ArchiveRefused("git rev-parse --git-common-dir failed: "
                             + common.stderr.decode("utf-8", "replace").strip())
    common_real = os.path.realpath(common.stdout.rstrip(b"\n"))
    if not _inside(common_real, owner_root):
        raise ArchiveRefused("the git common dir lies outside the ownership root: "
                             + os.fsdecode(common_real))
    stores = _object_stores(os.path.join(common_real, b"objects"), owner_root)
    _refuse_partial_clone(config, stores, repo_abs)
    recorded = [dict(r) for r in recorded_refs]
    facts, measures, state = _git_facts(repo_abs, repo_real, recorded)
    return {"facts": facts, "measures": measures,
            "covers_worktree": all(facts.values()),
            "refs_as_recorded": list(state["refs as recorded"])}


def _computed_tree(repo_abs: str, repo_real: bytes):
    """``computed_tree_equal`` (Rev9d s1, s6, s8): the working tree as the
    archiver itself reads it equals HEAD's tree. Returns the measure, a
    digest of what was read (for the re-check), the bytes of every
    ``.gitattributes`` read (None when the walk was not complete) and the
    computed ``{path: (mode, blob id)}``."""
    fmt = _git_run(repo_abs, "rev-parse", "--show-object-format")
    algo = fmt.stdout.decode("ascii", "replace").strip() if fmt.returncode == 0 else ""
    if algo not in ("sha1", "sha256"):
        return False, "object format " + repr(algo), None, {}
    listed = _git_run(repo_abs, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
    head_tree: dict[bytes, tuple[bytes, str]] | None = None
    if listed.returncode == 0:
        head_tree = {}
        for record in listed.stdout.split(b"\0"):
            if not record:
                continue
            meta, tab, path = record.partition(b"\t")
            parts = meta.split(b" ")
            if not tab or len(parts) != 3:
                head_tree = None
                break
            head_tree[path] = (parts[0], parts[2].decode("ascii", "replace"))
    found, problems, attributes = _walk_worktree(repo_real, algo)
    seen = hashlib.sha256(_canonical(
        [[base64.b64encode(p).decode("ascii"), m.decode("ascii"), o]
         for p, (m, o) in sorted(found.items())] + problems)).hexdigest()
    ok = head_tree is not None and not problems and found == head_tree
    return ok, seen, (None if problems else attributes), found


def _walk_worktree(root: bytes, algo: str):
    """Walk `root` (excluding only its top-level ``.git``) and compute every
    entry's git mode and blob id. Fails closed: every error, every special
    file (never opened), every entry on another device (never read) and every
    empty directory is a problem."""
    found: dict[bytes, tuple[bytes, str]] = {}
    problems: list[str] = []
    attributes: list[bytes] = []
    try:
        dev = _lstat(root).st_dev
    except OSError as exc:
        return found, ["repository root: " + str(exc)], attributes

    def onerror(exc: OSError) -> None:
        problems.append("cannot list " + os.fsdecode(exc.filename or b"?") + ": " + str(exc))

    for dirpath, dirnames, filenames in _walk(root, topdown=True, onerror=onerror,
                                              followlinks=False):
        names = sorted(dirnames + filenames)
        if dirpath == root:
            names = [name for name in names if name != b".git"]
        elif not names:
            problems.append("empty directory (git cannot hold it): " + os.fsdecode(dirpath))
        subdirs = []
        for name in names:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            try:
                st = _lstat(full)
            except OSError as exc:
                problems.append("vanished or unreadable: " + os.fsdecode(full) + " (" + str(exc) + ")")
                continue
            if st.st_dev != dev:
                problems.append("on another device (not read): " + os.fsdecode(full))
                continue
            if stat.S_ISDIR(st.st_mode):
                subdirs.append(name)
            elif stat.S_ISLNK(st.st_mode):
                try:
                    text = os.readlink(full)
                except OSError as exc:
                    problems.append("unreadable link " + os.fsdecode(full) + ": " + str(exc))
                    continue
                found[rel] = (b"120000", _blob_id(algo, [text]))
            elif stat.S_ISREG(st.st_mode):
                blob = _blob_of_file(full, st, algo, keep=(name == b".gitattributes"))
                if isinstance(blob, str):
                    problems.append(blob)
                    continue
                oid, data = blob
                if data is not None:
                    attributes.append(data)
                found[rel] = (b"100755" if st.st_mode & stat.S_IXUSR else b"100644", oid)
            else:
                problems.append("not a regular file, directory or symlink (FIFO, socket "
                                "or device; never opened): " + os.fsdecode(full))
        dirnames[:] = subdirs
    return found, problems, attributes


def _blob_id(algo: str, parts: list[bytes]) -> str:
    size = sum(len(part) for part in parts)
    digest = hashlib.new(algo, b"blob %d\0" % size)
    for part in parts:
        digest.update(part)
    return digest.hexdigest()


def _blob_of_file(full: bytes, st: os.stat_result, algo: str,
                  keep: bool) -> tuple[str, bytes | None] | str:
    """The blob id of the regular file `full` as scanned (`st`), or the
    problem that prevented computing it."""
    shown = os.fsdecode(full)
    try:
        fd = os.open(full, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        return "unreadable: " + shown + " (" + str(exc) + ")"
    try:
        now = _fstat(fd)
        if (not stat.S_ISREG(now.st_mode)
                or (now.st_dev, now.st_ino, now.st_size) != (st.st_dev, st.st_ino, st.st_size)):
            return "replaced or resized while it was read: " + shown
        digest = hashlib.new(algo, b"blob %d\0" % st.st_size)
        kept = [] if keep else None
        count = 0
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
            if kept is not None:
                kept.append(chunk)
        if count != st.st_size:
            return "changed size while it was read: " + shown
        return digest.hexdigest(), (b"".join(kept) if kept is not None else None)
    except OSError as exc:
        return "unreadable: " + shown + " (" + str(exc) + ")"
    finally:
        os.close(fd)


def _no_conversion(repo_abs: str, config: list[tuple[str, str | None]],
                   attributes: list[bytes], found: Mapping[bytes, tuple[bytes, str]],
                   staged: bytes) -> bool:
    """``no_conversion`` (Rev10a s2, s5): no configuration and no attribute
    by which git would convert content between the repository and the
    working tree. Computed from the effective configuration (includes
    resolved) and from attribute files; it gates no command, so an
    incomplete list here can only make the fact false more often. Any
    ``include.*``/``includeIf.*`` key, ``filter.*``, ``lfs.*``,
    ``core.attributesFile``, ``attr.tree``, ``core.eol``, ``core.autocrlf``
    other than false; a converting attribute in ``info/attributes`` or any
    working-tree ``.gitattributes``; or a ``.gitattributes`` in the index
    (``ls-files -s``) that is not byte-identical to the working-tree file the
    walk read, make it false."""
    for key, value in config:
        if key == "core.autocrlf" and _config_bool(value) is not False:
            return False
        if key in ("core.eol", "core.attributesfile", "attr.tree") or key.startswith(
                ("filter.", "lfs.", "include.", "includeif.")):
            return False
    for record in staged.split(b"\0"):
        if not record:
            continue
        meta, tab, path = record.partition(b"\t")
        parts = meta.split(b" ")
        if not tab or len(parts) != 3:
            return False
        if os.path.basename(path) == b".gitattributes":
            # an index-only or differing attributes file is not among those read
            on_disk = found.get(path)
            if on_disk is None or on_disk[1] != parts[1].decode("ascii", "replace"):
                return False
    info = _git_run(repo_abs, "rev-parse", "--path-format=absolute", "--git-path",
                    "info/attributes")
    if info.returncode != 0:
        return False
    try:
        with open(info.stdout.rstrip(b"\n"), "rb") as handle:
            attributes = attributes + [handle.read()]
    except FileNotFoundError:
        pass
    except OSError:
        return False
    for text in attributes:
        for line in text.splitlines():
            tokens = line.split()
            if not tokens or tokens[0].startswith(b"#"):
                continue
            for token in tokens[1:]:
                name = token.lstrip(b"-!").split(b"=", 1)[0].lower()
                if name in _CONVERTING_ATTRIBUTES or b"lfs" in token.lower():
                    return False
    return True


def _git_digest(recorded: list[Mapping]) -> str:
    rows = sorted(({"name": r.get("name"), "unpeeled": r.get("unpeeled"),
                    "commit": r.get("commit"), "tree": r.get("tree")}
                   for r in recorded), key=lambda r: str(r["name"]))
    return hashlib.sha256(_canonical(rows)).hexdigest()


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class VerificationReport:
    """What `verify_archive` found. `status` is ``ARCHIVED_VERIFIED`` only
    when every check passed and extraction was performed."""

    bundle_dir: str
    payload_kind: str | None = None
    status: str = ARCHIVE_UNVERIFIED
    checks: list[dict] = dataclasses.field(default_factory=list)
    digests: dict = dataclasses.field(default_factory=dict)
    extracted: bool = False
    covers_worktree: bool | None = None
    notes: list[str] = dataclasses.field(default_factory=list)
    page_cache_note: str = PAGE_CACHE_NOTE

    def add(self, check_id: str, ok: bool, detail: str) -> bool:
        self.checks.append({"id": check_id, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def check(self, check_id: str) -> dict | None:
        for item in self.checks:
            if item["id"] == check_id:
                return item
        return None

    def failed(self) -> list[dict]:
        return [item for item in self.checks if not item["ok"]]

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class _StreamError(Exception):
    """The zstd payload is truncated, unterminated, corrupt or has trailing data."""


class _ZstdReader(io.RawIOBase):
    """Decompresses exactly one zstd frame from a descriptor; the frame's
    checksum is verified by the decompressor, and running out of input before
    the frame ends -- or input after it -- is an error, not an end."""

    def __init__(self, fd: int, zstd):
        self._fd = fd
        self._zstd = zstd
        self._dobj = zstd.ZstdDecompressor().decompressobj()
        self._buf = bytearray()
        self.frame_done = False
        self._raw_eof = False

    def readable(self) -> bool:
        return True

    def _fill(self) -> None:
        chunk = os.read(self._fd, _CHUNK)
        if not chunk:
            self._raw_eof = True
            if not self.frame_done:
                raise _StreamError("payload ends before the zstd frame does "
                                   "(truncated or unterminated)")
            return
        if self.frame_done:
            raise _StreamError("trailing data after the end of the zstd frame")
        try:
            self._buf += self._dobj.decompress(chunk)
        except self._zstd.ZstdError as exc:
            raise _StreamError("zstd: " + str(exc)) from exc
        if self._dobj.eof:
            self.frame_done = True
            if self._dobj.unused_data:
                raise _StreamError("trailing data after the end of the zstd frame")

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            while not self._raw_eof:
                self._fill()
            size = len(self._buf)
        while len(self._buf) < size and not self._raw_eof:
            self._fill()
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def drain(self) -> None:
        while not self._raw_eof:
            self._fill()
            self._buf.clear()


#: tarfile.FilterError exists from Python 3.11.4 on; without it the import
#: must still work, and extraction then fails closed (no `filter` argument).
_FilterError = getattr(tarfile, "FilterError", tarfile.TarError)


class _MemberRefused(_FilterError):
    def __init__(self, member: tarfile.TarInfo, reason: str):
        super().__init__("refused member " + repr(member.name) + ": " + reason)
        self.tarinfo = member
        self.reason = reason


_MEMBER_TYPES = {tarfile.REGTYPE: "file", tarfile.AREGTYPE: "file",
                 tarfile.DIRTYPE: "dir", tarfile.SYMTYPE: "symlink",
                 tarfile.LNKTYPE: "hardlink"}
_ADMITTED = (tarfile.REGTYPE, tarfile.DIRTYPE, tarfile.SYMTYPE)


def _member_record(member: tarfile.TarInfo) -> dict:
    kind = _MEMBER_TYPES.get(member.type, "other:" + repr(member.type))
    return {"raw": os.fsencode(member.name), "type": kind,
            "mode": member.mode & 0o7777, "size": member.size,
            "link": os.fsencode(member.linkname) if member.linkname else None}


def _make_filter(root: str, externals: Mapping[bytes, bytes], skipped: set):
    """The extraction filter (Rev2 s1, Rev3 s1-2).

    Admits only REGTYPE, DIRTYPE and SYMTYPE. Refuses a name that is absolute
    or has a ``..`` component, a symlink whose text is absolute or leaves the
    archived root, any destination that already exists, any destination below
    an existing symlink, and any destination whose parent resolves outside
    `root`. Admitted members are returned unchanged, so modes stay exact.
    External links are matched against their record and not extracted.

    tarfile calls the filter a second time for every directory it admitted,
    to apply the directory's mode and mtime after its contents are written.
    That second call is recognised by identity -- the very TarInfo this filter
    returned -- and admitted only if the destination is still a real directory
    below no symlink and inside `root`; `chmod` follows symlinks, so this is
    the check that keeps the fix-up from reaching outside.
    """
    root_real = os.path.realpath(root)
    admitted_dirs: dict[int, tarfile.TarInfo] = {}

    def _filter(member: tarfile.TarInfo, dest_path: str) -> tarfile.TarInfo | None:
        raw = os.fsencode(member.name)
        if member.type not in _ADMITTED:
            what = "hardlink" if member.type == tarfile.LNKTYPE else "type " + repr(member.type)
            raise _MemberRefused(member, what + " members are not admitted")
        if not raw or os.path.isabs(raw) or raw.startswith(b"/"):
            raise _MemberRefused(member, "absolute or empty name")
        parts = raw.split(b"/")
        if b".." in parts:
            raise _MemberRefused(member, "name has a '..' component")
        if raw in externals:
            if member.type == tarfile.SYMTYPE and os.fsencode(member.linkname) == externals[raw]:
                skipped.add(raw)
                return None
            raise _MemberRefused(member, "does not match its external-link record")
        if member.type == tarfile.SYMTYPE:
            link = os.fsencode(member.linkname)
            if not _lexically_inside(os.path.dirname(raw), link):
                raise _MemberRefused(member, "symlink target leaves the archive root: "
                                     + repr(member.linkname))
        base = os.fsencode(root)
        clean = [p for p in parts if p not in (b"", b".")]
        if not clean:
            raise _MemberRefused(member, "name is the extraction root itself")
        current = base
        for part in clean[:-1]:
            current = os.path.join(current, part)
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                break
            if stat.S_ISLNK(st.st_mode):
                raise _MemberRefused(member, "path component is a symlink: "
                                     + repr(os.fsdecode(current)))
        dest = os.path.join(base, *clean)
        if admitted_dirs.get(id(member)) is member:
            st = os.lstat(dest)
            if not stat.S_ISDIR(st.st_mode) or not _inside(os.path.realpath(dest), root_real):
                raise _MemberRefused(member, "directory changed before its mode was set")
            return member
        if os.path.lexists(dest):
            raise _MemberRefused(member, "destination already exists (duplicate member)")
        if not _inside(os.path.realpath(os.path.dirname(dest)), root_real):
            raise _MemberRefused(member, "destination resolves outside the extraction root")
        if member.type == tarfile.DIRTYPE:
            admitted_dirs[id(member)] = member
        return member

    return _filter


def _walk_extracted(root: bytes) -> dict[bytes, os.stat_result]:
    out: dict[bytes, os.stat_result] = {}
    stack = [b""]
    while stack:
        rel_dir = stack.pop()
        with os.scandir(os.path.join(root, rel_dir) if rel_dir else root) as listing:
            names = [entry.name for entry in listing]
        for name in names:
            rel = os.path.join(rel_dir, name) if rel_dir else name
            st = os.lstat(os.path.join(root, rel))
            out[rel] = st
            if stat.S_ISDIR(st.st_mode):
                stack.append(rel)
    return out


def _parse_sums(text: bytes) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in text.decode("ascii").splitlines():
        hit = _SUMS_LINE.fullmatch(line)
        if not hit or hit.group(2) in sums:
            raise ValueError("malformed SHA256SUMS line: " + repr(line))
        sums[hit.group(2)] = hit.group(1)
    return sums


def verify_archive(bundle_dir: Path, *, extract_root: Path) -> VerificationReport:
    """Judge a bundle only by re-reading it.

    Every record is read again through fresh descriptors. Tree payloads are
    decompressed completely (zstd checksum verified), every tar member is
    compared one-to-one and in order with the manifest, the payload is
    extracted into `extract_root` -- which must be an empty directory --
    through a filter that cannot write outside it, and every extracted file is
    re-hashed from `extract_root`. Git payloads are fetched from the bundle
    into a fresh ``init --bare --template=`` repository in `extract_root`,
    checked with ``git bundle verify`` and ``git fsck``, their header parsed
    here, and every recorded ref's unpeeled id, commit and tree compared.
    There is no mode without extraction.
    """
    bundle = Path(bundle_dir)
    report = VerificationReport(bundle_dir=os.fspath(bundle))
    root = Path(extract_root)
    try:
        _verify(bundle, root, report)
    except ArchiveDependencyMissing:
        raise
    except Exception as exc:  # a malformed bundle is a failed check, not a crash
        report.add("verifier", False, "unexpected " + type(exc).__name__ + ": " + str(exc))
    report.status = (ARCHIVED_VERIFIED
                     if report.checks and report.extracted
                     and all(item["ok"] for item in report.checks)
                     else ARCHIVE_UNVERIFIED)
    return report


def _verify(bundle: Path, root: Path, report: VerificationReport) -> None:
    report.add("not_incomplete", not os.path.lexists(bundle / INCOMPLETE),
               "no INCOMPLETE marker" if not os.path.lexists(bundle / INCOMPLETE)
               else "the bundle is marked INCOMPLETE")

    tree_present = os.path.lexists(bundle / TREE_PAYLOAD)
    git_present = os.path.lexists(bundle / GIT_PAYLOAD)
    if tree_present == git_present:
        report.add("payload_present", False,
                   "exactly one of payload.tar.zst and payload.git.bundle must exist")
        return
    payload_name = TREE_PAYLOAD if tree_present else GIT_PAYLOAD
    kind = "tree" if tree_present else "git"
    report.payload_kind = kind
    if kind == "tree":
        zstd = _zstd()

    try:
        sums = _parse_sums(_read_fresh(bundle / SUMS))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        report.add("sha256sums_read", False, "SHA256SUMS unreadable: " + str(exc))
        sums = {}
    manifest_bytes = _read_fresh(bundle / MANIFEST)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    payload_sha, payload_size = _hash_path(bundle / payload_name)
    report.digests = {"manifest_sha256": manifest_sha, "payload_sha256": payload_sha,
                      "content_digest": None}

    report.add("sha256sums_payload", sums.get(payload_name) == payload_sha,
               "payload re-hashed " + payload_sha + " (" + str(payload_size)
               + " bytes); SHA256SUMS says " + str(sums.get(payload_name)))
    report.add("sha256sums_manifest", sums.get(MANIFEST) == manifest_sha,
               "MANIFEST.json re-hashed " + manifest_sha + "; SHA256SUMS says "
               + str(sums.get(MANIFEST)))
    if set(sums) - {payload_name, MANIFEST}:
        report.add("sha256sums_extra", False,
                   "SHA256SUMS lists unexpected names: " + repr(sorted(set(sums))))

    try:
        manifest = json.loads(manifest_bytes.decode("ascii"))
        if not isinstance(manifest, dict):
            raise ValueError("not an object")
    except (ValueError, UnicodeDecodeError) as exc:
        report.add("manifest_schema", False, "MANIFEST.json unreadable: " + str(exc))
        return
    schema_ok = (manifest.get("schema") == SCHEMA
                 and manifest.get("payload_kind") == kind)
    report.add("manifest_schema", schema_ok,
               "schema " + repr(manifest.get("schema")) + ", payload_kind "
               + repr(manifest.get("payload_kind")) + ", payload on disk is " + kind)
    report.add("manifest_payload_sha256", manifest.get("payload_sha256") == payload_sha,
               "manifest says " + str(manifest.get("payload_sha256")))
    report.digests["content_digest"] = manifest.get("content_digest")
    name = os.path.basename(os.path.realpath(bundle))
    bundle_id = manifest.get("bundle_id")
    report.add("bundle_name", name == bundle_id and bool(_BUNDLE_NAME.fullmatch(name)),
               "directory " + repr(name) + ", manifest bundle_id " + repr(bundle_id)
               + "; both must be equal and match " + _BUNDLE_NAME.pattern)

    if not _extract_root_ready(root, bundle, report):
        return
    if kind == "tree":
        _verify_tree(bundle, root, manifest, report, zstd)
    else:
        _verify_git(bundle, root, manifest, report)


def _extract_root_ready(root: Path, bundle: Path, report: VerificationReport) -> bool:
    try:
        st = os.lstat(root)
    except OSError as exc:
        return report.add("extract_root_empty", False,
                          "extract_root missing: " + str(exc))
    if not stat.S_ISDIR(st.st_mode):
        return report.add("extract_root_empty", False,
                          "extract_root is not a directory (or is a symlink)")
    with os.scandir(root) as listing:
        if any(True for _ in listing):
            return report.add("extract_root_empty", False,
                              "extract_root is not empty: " + os.fspath(root))
    if _inside(os.path.realpath(root), os.path.realpath(bundle)):
        return report.add("extract_root_empty", False,
                          "extract_root lies inside the bundle directory")
    return report.add("extract_root_empty", True, "empty directory " + os.fspath(root))


def _verify_tree(bundle: Path, root: Path, manifest: dict,
                 report: VerificationReport, zstd) -> None:
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        report.add("member_list", False, "manifest has no entry list")
        return
    raws = [_entry_raw(e) for e in entries]
    by_raw = dict(zip(raws, entries))
    externals_list = manifest.get("externals") or []
    externals = {_raw_field(x, "link"): _raw_field(x, "link_target")
                 for x in externals_list}

    # pass 0: the frame header must declare a content checksum (Rev6 s5)
    header_problem = None
    fd = _open_fresh(bundle / TREE_PAYLOAD)
    try:
        head = os.read(fd, 18)              # ZSTD_FRAMEHEADERSIZE_MAX
    finally:
        os.close(fd)
    try:
        if not zstd.get_frame_parameters(head).has_checksum:
            header_problem = "no checksum present: the zstd frame header declares none"
    except zstd.ZstdError as exc:
        header_problem = "zstd frame header unreadable: " + str(exc)

    # pass 1: decompress the whole stream and list every member
    members: list[dict] = []
    stream_error = tar_error = None
    fd = _open_fresh(bundle / TREE_PAYLOAD)
    try:
        reader = _ZstdReader(fd, zstd)
        try:
            with tarfile.open(fileobj=reader, mode="r|", encoding="utf-8",
                              errors="surrogateescape") as tar:
                for member in tar:
                    members.append(_member_record(member))
        except _StreamError as exc:
            stream_error = exc
        except (tarfile.TarError, OSError, EOFError) as exc:
            tar_error = exc
        if stream_error is None:
            try:
                reader.drain()
            except _StreamError as exc:
                stream_error = exc
    finally:
        os.close(fd)
    problems = [str(p) for p in (header_problem, stream_error) if p is not None]
    report.add("zstd_stream", not problems,
               "one zstd frame decompressed to its end, its content checksum "
               "present and verified, no trailing data"
               if not problems else "; ".join(problems))
    _compare_members(members, entries, raws, tar_error, report)
    if stream_error is not None:
        return

    # pass 2: extract through the filter
    skipped: set = set()
    extract_error = None
    fd = _open_fresh(bundle / TREE_PAYLOAD)
    try:
        reader = _ZstdReader(fd, zstd)
        try:
            with tarfile.open(fileobj=reader, mode="r|", encoding="utf-8",
                              errors="surrogateescape", errorlevel=2) as tar:
                tar.extractall(os.fspath(root),
                               filter=_make_filter(os.fspath(root), externals, skipped))
        except (tarfile.TarError, OSError, EOFError, _StreamError) as exc:
            extract_error = exc
    finally:
        os.close(fd)
    report.extracted = True
    report.add("extract", extract_error is None,
               "extracted into " + os.fspath(root) if extract_error is None
               else "extraction stopped: " + str(extract_error))

    _check_extracted(root, entries, raws, by_raw, externals_list, externals,
                     skipped, manifest, members, report)


def _compare_members(members: list[dict], entries: list[dict], raws: list[bytes],
                     tar_error, report: VerificationReport) -> None:
    problems: list[str] = []
    if tar_error is not None:
        problems.append("tar unreadable: " + str(tar_error))
    names = [m["raw"] for m in members]
    dupes = sorted(n for n, c in collections.Counter(names).items() if c > 1)
    if dupes:
        problems.append("duplicate member name(s): " + ", ".join(_show(d) for d in dupes))
    if raws != sorted(raws):
        problems.append("manifest entries are not sorted by raw path bytes")
    if names != sorted(names):
        problems.append("tar members are not sorted by raw path bytes")
    if len(members) != len(entries):
        problems.append("member count " + str(len(members)) + " != manifest entry count "
                        + str(len(entries)))
    for index, (member, entry, raw) in enumerate(zip(members, entries, raws)):
        where = "#" + str(index) + " " + _show(member["raw"])
        if member["raw"] != raw:
            problems.append(where + ": name differs from manifest " + _show(raw))
            continue
        if member["type"] != entry.get("type"):
            problems.append(where + ": type " + member["type"] + " != " + str(entry.get("type")))
            continue
        if member["mode"] != entry.get("mode"):
            problems.append(where + ": mode " + oct(member["mode"]) + " != "
                            + str(entry.get("mode")))
        if member["type"] == "file" and member["size"] != entry.get("size"):
            problems.append(where + ": size " + str(member["size"]) + " != "
                            + str(entry.get("size")))
        if member["type"] in ("symlink", "hardlink") and member["link"] != _raw_field(entry, "link_target"):
            problems.append(where + ": link text " + repr(member["link"]) + " != manifest")
    for extra in members[len(entries):]:
        problems.append("extra member " + _show(extra["raw"]))
    for raw in raws[len(members):]:
        problems.append("missing member " + _show(raw))
    report.add("member_list", not problems,
               str(len(members)) + " members equal the manifest one-to-one in order"
               if not problems else "; ".join(problems[:20]))


def _check_extracted(root: Path, entries: list[dict], raws: list[bytes],
                     by_raw: dict, externals_list: list, externals: dict,
                     skipped: set, manifest: dict, members: list[dict],
                     report: VerificationReport) -> None:
    root_b = os.fsencode(os.fspath(root))
    root_real = os.path.realpath(root_b)
    tree = _walk_extracted(root_b)

    expected = {raw for raw in raws if raw not in externals}
    unexpected = sorted(set(tree) - expected)
    missing = sorted(expected - set(tree))
    report.add("extracted_tree", not unexpected and not missing,
               "extracted tree holds exactly the manifest's entries"
               if not unexpected and not missing else
               "unexpected " + repr([_show(r) for r in unexpected[:10]])
               + ", missing " + repr([_show(r) for r in missing[:10]]))

    observed: list[dict] = []
    rehashed: dict[bytes, str] = {}
    sizes: dict[bytes, int] = {}
    external_links: list[tuple[bytes, dict]] = []
    counts = {"files": 0, "dirs": 0, "symlinks": 0, "external": 0,
              "logical_bytes": 0, "entry_count": 0}
    for raw, entry in zip(raws, entries):
        kind = entry.get("type")
        shown = _show(raw)
        if raw in externals:
            external_links.append((raw, entry))
            observed.append(_projection(entry))
            counts["symlinks"] += 1
            counts["external"] += 1
            counts["entry_count"] += 1
            continue
        st = tree.get(raw)
        target = os.path.join(root_b, raw)
        record = dict(_projection(entry))
        if st is None:
            report.add(_kind_id(kind) + shown, False, "not extracted: " + os.fsdecode(target))
            continue
        counts["entry_count"] += 1
        mode = stat.S_IMODE(st.st_mode)
        record["mode"] = mode
        if kind == "file":
            if not stat.S_ISREG(st.st_mode):
                report.add("file:" + shown, False, "not a regular file: " + os.fsdecode(target))
                continue
            try:
                digest, size = _hash_path(target)
            except OSError as exc:
                report.add("file:" + shown, False, "unreadable " + os.fsdecode(target)
                           + ": " + str(exc))
                continue
            rehashed[raw] = digest
            sizes[raw] = size
            record.update(sha256=digest, size=size)
            counts["files"] += 1
            counts["logical_bytes"] += size
            ok = (digest == entry.get("sha256") and size == entry.get("size")
                  and mode == entry.get("mode"))
            report.add("file:" + shown, ok,
                       "re-hashed " + os.fsdecode(target) + " -> " + digest + ", "
                       + str(size) + " bytes, mode " + oct(mode)
                       + ("" if ok else "; manifest: " + str(entry.get("sha256")) + ", "
                          + str(entry.get("size")) + " bytes, mode " + str(entry.get("mode"))))
        elif kind == "dir":
            counts["dirs"] += 1
            record["size"] = 0
            ok = stat.S_ISDIR(st.st_mode) and mode == entry.get("mode")
            report.add("dir:" + shown, ok,
                       os.fsdecode(target) + " mode " + oct(mode) + " (manifest "
                       + str(entry.get("mode")) + ")")
        elif kind == "symlink":
            counts["symlinks"] += 1
            if not stat.S_ISLNK(st.st_mode):
                report.add("symlink:" + shown, False, "not a symlink: " + os.fsdecode(target))
                continue
            text = os.readlink(target)
            record.update({"link_target": None, "link_target_b64": None})
            record.update(_name_fields(text, "link_target"))
            record["size"] = len(text)
            record["mode"] = entry.get("mode")
            ok = text == _raw_field(entry, "link_target")
            report.add("symlink:" + shown, ok,
                       "link text " + repr(os.fsdecode(text)) + " re-read from "
                       + os.fsdecode(target))
            real = os.path.realpath(target)
            report.add("symlink_realpath:" + shown, _inside(real, root_real),
                       "resolves to " + os.fsdecode(real))
            # The builder never writes a dangling link; a link whose content is
            # not in the extraction is a link-only archive (Rev6 s1).
            try:
                resolved = os.path.realpath(target, strict=True)
                resolves = True
            except OSError as exc:
                resolved, resolves = str(exc).encode(), False
            report.add("symlink_resolves:" + shown, resolves,
                       "resolves to the extracted " + os.fsdecode(resolved) if resolves
                       else "dangling after extraction, its content is not in the "
                       "archive: " + os.fsdecode(resolved))
        else:
            report.add("entry:" + shown, False, "unknown entry type " + repr(kind))
            continue
        observed.append(record)

    content = _check_external_content(root_b, externals_list, by_raw, members,
                                      tree, rehashed, sizes, report)
    for raw, entry in external_links:
        shown = _show(raw)
        ok = (entry.get("type") == "symlink" and raw in skipped
              and _raw_field(entry, "link_target") == externals[raw])
        report.add("external_link:" + shown, ok,
                   ("verified by record: link text " + repr(_show(externals[raw]))
                    + " matched its tar member and the link was not extracted; "
                    if ok else "external link not matched against its tar member; ")
                   + content.get(raw, "no stored content was re-hashed for it"))

    groups: dict[int, list[bytes]] = {}
    for raw, entry in zip(raws, entries):
        if entry.get("hardlink_group") is not None:
            groups.setdefault(entry["hardlink_group"], []).append(raw)
    for group, members in sorted(groups.items()):
        hashes = {rehashed.get(raw) for raw in members}
        ok = len(members) > 1 and len(hashes) == 1 and None not in hashes
        report.add("hardlink_group:" + str(group), ok,
                   str(len(members)) + " files, re-hashed digests " + repr(sorted(map(str, hashes))))

    totals = manifest.get("totals") or {}
    report.add("totals", all(totals.get(k) == v for k, v in counts.items()),
               "recomputed from the extraction " + json.dumps(counts, sort_keys=True)
               + "; manifest " + json.dumps(totals, sort_keys=True))

    recorded = manifest.get("content_digest")
    from_manifest = _content_digest(entries)
    from_extraction = hashlib.sha256(_canonical(observed)).hexdigest()
    ok = recorded == from_manifest == from_extraction
    report.add("content_digest", ok,
               "recorded " + str(recorded) + ", from manifest entries " + from_manifest
               + ", from the extracted tree " + from_extraction)


def _check_external_content(root_b: bytes, externals_list: list, by_raw: dict,
                            members: list[dict], tree: dict, rehashed: dict,
                            sizes: dict, report: VerificationReport) -> dict[bytes, str]:
    """The verifier never certifies a link-only archive (Rev6 s1, Rev7 s1).

    For every externals record, check ``external_content:<link>``: the record
    names a stored entry ``.vh-external/<n>``; that entry is a regular-file
    member of the payload and of the manifest; it was extracted under
    extract_root; and its SHA-256 and size, re-hashed from extract_root, equal
    the record's. Every symlink member whose text leaves the archived root
    and that has no such record fails the same check -- whoever built the
    bundle. Returns, per link, what was re-hashed (for the external_link
    detail).
    """
    member_types = {m["raw"]: m["type"] for m in members}
    outcome: dict[bytes, str] = {}
    recorded: set[bytes] = set()
    for record in externals_list:
        link = _raw_field(record, "link") if isinstance(record, Mapping) else None
        shown = _show(link) if link is not None else "?"
        if link is not None:
            recorded.add(link)
        stored = record.get("stored") if isinstance(record, Mapping) else None
        problems = []
        if link is None or (by_raw.get(link) or {}).get("type") != "symlink":
            problems.append("the record's link is not a symlink entry of the manifest")
        if not isinstance(stored, str) or not _STORED.fullmatch(stored):
            problems.append("the record names no stored entry .vh-external/<n>: "
                            + repr(stored))
            outcome[link] = "no stored content: the record names none"
            report.add("external_content:" + shown, False, "; ".join(problems))
            continue
        stored_raw = stored.encode("ascii")
        where = os.fsdecode(os.path.join(root_b, stored_raw))
        if member_types.get(stored_raw) != "file":
            problems.append("stored entry " + stored + " is not a regular-file member "
                            "of the payload (" + str(member_types.get(stored_raw)) + ")")
        if (by_raw.get(stored_raw) or {}).get("type") != "file":
            problems.append("stored entry " + stored + " is not a file entry of the manifest")
        st = tree.get(stored_raw)
        if st is None or not stat.S_ISREG(st.st_mode):
            problems.append("stored entry " + stored + " was not extracted as a regular "
                            "file under extract_root")
        digest, size = rehashed.get(stored_raw), sizes.get(stored_raw)
        if digest is None:
            problems.append("stored entry " + stored + " was not re-hashed")
            outcome[link] = "stored content " + stored + " missing: nothing was re-hashed"
        else:
            if digest != record.get("sha256"):
                problems.append("re-hashed sha256 " + digest + " != record "
                                + str(record.get("sha256")))
            if size != record.get("size"):
                problems.append("re-hashed size " + str(size) + " != record "
                                + str(record.get("size")))
            outcome[link] = ("stored content " + stored + " re-hashed from " + where
                             + ": sha256 " + digest + ", " + str(size) + " bytes (record: "
                             + str(record.get("sha256")) + ", " + str(record.get("size"))
                             + " bytes)")
        report.add("external_content:" + shown, not problems,
                   outcome[link] if not problems else "; ".join(problems))

    for member in members:
        if member["type"] != "symlink" or member["raw"] in recorded:
            continue
        link = member["link"] or b""
        if _lexically_inside(os.path.dirname(member["raw"]), link):
            continue
        report.add("external_content:" + _show(member["raw"]), False,
                   "symlink " + repr(_show(link)) + " leaves the archived root and no "
                   "externals record with stored content covers it: the archive would "
                   "hold only the link")
    return outcome


def _kind_id(kind: Any) -> str:
    return {"file": "file:", "dir": "dir:", "symlink": "symlink:"}.get(kind, "entry:")


def _git_output(done: subprocess.CompletedProcess) -> str:
    return (done.stdout + done.stderr).decode("utf-8", "replace").strip()


#: The two bundle signatures git writes; v3 may carry capability lines.
_BUNDLE_SIGNATURES = {b"# v2 git bundle": 2, b"# v3 git bundle": 3}
#: A bundle header larger than this is not read on (it fails closed).
_HEADER_LIMIT = 64 << 20
_OID = re.compile(rb"[0-9a-f]{40}|[0-9a-f]{64}")
#: The verifier fetches a bundled ``HEAD`` to this private ref; a bundle that
#: lists a ref below ``refs/vh-bundle/`` itself is refused.
_PRIVATE_HEAD = "refs/vh-bundle/HEAD"


def _bundle_header(path: str) -> dict:
    """The header of the git bundle at `path`, parsed here (Rev10b s3: no
    ``bundle list-heads``): the v2/v3 signature, v3 capability lines
    (``@key[=value]``), prerequisites (``-<oid>[ comment]``) and every
    ``<oid> <refname>`` line up to the blank line. Raises ValueError when the
    header is malformed or longer than `_HEADER_LIMIT`."""
    fd = _open_fresh(path)
    try:
        buf = bytearray()
        while True:
            end = buf.find(b"\n\n")
            if end >= 0:
                break
            if len(buf) > _HEADER_LIMIT:
                raise ValueError("bundle header longer than %d bytes" % _HEADER_LIMIT)
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                raise ValueError("bundle ends before its header does")
            buf += chunk
    finally:
        os.close(fd)
    lines = bytes(buf[:end]).split(b"\n")
    version = _BUNDLE_SIGNATURES.get(lines[0])
    if version is None:
        raise ValueError("not a v2 or v3 git bundle signature: " + repr(lines[0][:40]))
    header: dict = {"version": version, "capabilities": [], "prerequisites": [],
                    "heads": []}
    for line in lines[1:]:
        if line.startswith(b"@"):
            if version != 3 or header["prerequisites"] or header["heads"]:
                raise ValueError("capability line out of place: " + _show(line))
            header["capabilities"].append(_show(line[1:]))
        elif line.startswith(b"-"):
            oid = line[1:].split(b" ", 1)[0]
            if not _OID.fullmatch(oid):
                raise ValueError("malformed prerequisite line: " + _show(line))
            header["prerequisites"].append(oid.decode("ascii"))
        else:
            oid, sep, name = line.partition(b" ")
            if not sep or not _OID.fullmatch(oid) or not name:
                raise ValueError("malformed ref line: " + _show(line))
            header["heads"].append((oid.decode("ascii"), _show(name)))
    return header


def _plain_ref_name(name: str) -> bool:
    """``HEAD`` or a ref name below ``refs/`` that is safe to put into a
    refspec: no whitespace, control characters or refspec/revision syntax."""
    if name == "HEAD":
        return True
    if not name.startswith("refs/") or name.startswith(_PRIVATE_HEAD.rsplit("/", 1)[0] + "/"):
        return False
    if ".." in name or "//" in name or name.endswith(("/", ".", ".lock")) or "@{" in name:
        return False
    return not any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7f or ch in ":?*[\\^~"
                   for ch in name)


def _local_name(name: str) -> str:
    """Where the verifier keeps a bundled ref: under its own name, ``HEAD``
    under `_PRIVATE_HEAD` (the fresh repository's own HEAD is not touched)."""
    return _PRIVATE_HEAD if name == "HEAD" else name


def _verify_git(bundle: Path, root: Path, manifest: dict,
                report: VerificationReport) -> None:
    """Git payloads: the header parsed here, a fresh repository made with
    ``init --bare --template=`` in `root`, every bundled ref fetched from the
    bundle into it, ``bundle verify``, every recorded ref compared (unpeeled,
    commit, tree), ``fsck --full --strict``. A git call that times out fails
    the check it belongs to."""
    stage = ["extract"]
    try:
        _verify_git_steps(bundle, root, manifest, report, stage)
    except _GitTimeout as exc:
        report.add(stage[0], False, "timed out: " + str(exc))


def _verify_git_steps(bundle: Path, root: Path, manifest: dict,
                      report: VerificationReport, stage: list) -> None:
    payload = os.fspath((bundle / GIT_PAYLOAD).resolve())
    target = os.fspath(root)
    try:
        header = _bundle_header(payload)
        header_error = None
    except (OSError, ValueError) as exc:
        header, header_error = None, "bundle header unreadable: " + str(exc)

    init = _run_git("-C", target, "init", "--bare", "--quiet", "--template=", ".")
    fetch = None
    if init.returncode == 0 and header is not None:
        refspecs = [name + ":" + _local_name(name) for _, name in header["heads"]
                    if _plain_ref_name(name)]
        if refspecs:
            fetch = _run_git("-C", target, "fetch", "--quiet", "--no-tags",
                             "--no-write-fetch-head", "--no-auto-gc",
                             "--no-recurse-submodules", "--end-of-options", payload,
                             *refspecs)
    fetched = fetch is not None and fetch.returncode == 0
    report.extracted = fetched
    if fetched:
        detail = ("fetched every bundled ref from the bundle into a fresh repository "
                  "(init --bare --template=) at " + target)
    elif init.returncode != 0:
        detail = "git init --bare failed: " + _git_output(init)
    elif header is None:
        detail = "nothing fetched: " + str(header_error)
    elif fetch is None:
        detail = "nothing fetched: the bundle header names no fetchable ref"
    else:
        detail = "git fetch from the bundle failed: " + _git_output(fetch)
    report.add("extract", fetched, detail)

    stage[0] = "git_bundle_verify"
    if not fetched:
        # The fetch failed; `git bundle verify` in the fresh repository still
        # says why (missing prerequisites, ...).
        verify = (_run_git("-C", target, "bundle", "verify", payload)
                  if init.returncode == 0 else init)
        report.add("git_bundle_verify", False,
                   "git bundle verify (the fetch failed): exit " + str(verify.returncode)
                   + ": " + _git_output(verify))
        if header_error is not None:
            report.add("git_heads", False, header_error)
        elif header["prerequisites"]:
            report.add("git_heads", False,
                       "the bundle has prerequisites (it does not stand alone): "
                       + ", ".join(header["prerequisites"]))
        return
    verify = _run_git("-C", target, "bundle", "verify", payload)
    report.add("git_bundle_verify", verify.returncode == 0,
               "git bundle verify: exit " + str(verify.returncode) + ": " + _git_output(verify))

    recorded = manifest.get("refs")
    if (not isinstance(recorded, list) or not recorded
            or not all(isinstance(r, dict) for r in recorded)):
        report.add("refs", False, "manifest records no refs")
        return

    stage[0] = "git_heads"
    heads: dict[str, str] = {}
    problems = []
    for oid, name in header["heads"]:
        if name in heads:
            problems.append("the bundle header lists " + name + " twice")
        if not _plain_ref_name(name):
            problems.append("the bundle header lists a ref name that is not admitted: "
                            + repr(name))
        heads[name] = oid
    if header["prerequisites"]:
        problems.append("the bundle has prerequisites (it does not stand alone): "
                        + ", ".join(header["prerequisites"]))
    odd = [c for c in header["capabilities"]
           if c not in ("object-format=sha1", "object-format=sha256")]
    if odd:
        problems.append("the bundle header has capabilities this verifier does not "
                        "admit (a filtered bundle is not complete): " + ", ".join(odd))
    names = {r.get("name") for r in recorded}
    if set(heads) != names:
        problems.append("bundle header lists " + repr(sorted(heads)) + ", manifest records "
                        + repr(sorted(map(str, names))))
    for ref in recorded:
        if heads.get(ref.get("name")) != ref.get("unpeeled"):
            problems.append(str(ref.get("name")) + ": bundle header lists "
                            + str(heads.get(ref.get("name"))) + ", manifest unpeeled "
                            + str(ref.get("unpeeled")))
    report.add("git_heads", not problems,
               "the bundle header (v" + str(header["version"]) + ", read here) equals "
               "every recorded unpeeled id: "
               + ", ".join(str(r.get("name")) + "=" + str(r.get("unpeeled")) for r in recorded)
               if not problems else "; ".join(problems))

    seen = []
    for ref in recorded:
        name = str(ref.get("name"))
        stage[0] = "ref:" + name
        local = _local_name(name) if _plain_ref_name(name) else None
        got_unpeeled = _rev_parse(target, local) if local else None
        got_commit = _rev_parse(target, local + "^{commit}") if local else None
        got_tree = _rev_parse(target, local + "^{tree}") if local else None
        ok = (got_unpeeled == ref.get("unpeeled") and got_commit == ref.get("commit")
              and got_tree == ref.get("tree"))
        report.add("ref:" + name, ok,
                   "fetched " + name + " = " + str(got_unpeeled) + ", commit "
                   + str(got_commit) + ", tree " + str(got_tree) + "; manifest "
                   + str(ref.get("unpeeled")) + " / " + str(ref.get("commit")) + " / "
                   + str(ref.get("tree")))
        seen.append({"name": ref.get("name"), "unpeeled": got_unpeeled,
                     "commit": got_commit, "tree": got_tree})
    stage[0] = "git_fsck"
    fsck = _run_git("-C", target, "fsck", "--full", "--strict", "--no-progress")
    report.add("git_fsck", fsck.returncode == 0,
               "git fsck --full --strict on the fetched repository: exit "
               + str(fsck.returncode)
               + ("" if fsck.returncode == 0 else ": " + _git_output(fsck)[:500]))
    recomputed = _git_digest(seen)
    report.add("content_digest", recomputed == manifest.get("content_digest"),
               "recomputed from the fetched repository " + recomputed + ", recorded "
               + str(manifest.get("content_digest")))

    _check_covers_worktree(manifest, report)


def _check_covers_worktree(manifest: dict, report: VerificationReport) -> None:
    """Re-derive covers_worktree from the recorded facts and every recorded
    measure (Rev6 s6, Rev7 s2, Rev9b-9d): each fact must be a boolean equal
    to the AND of its measures, and covers_worktree the AND of the facts. A
    recorded value that does not follow from them is a failure."""
    measures = manifest.get("worktree_facts")
    problems = []
    if not isinstance(measures, dict):
        problems.append("no worktree_facts recorded")
        measures = {}
    facts = {}
    for fact, names in _FACT_MEASURES.items():
        value = manifest.get(fact)
        facts[fact] = value
        if not isinstance(value, bool):
            problems.append(fact + " is not recorded as a boolean")
            continue
        got = {name: measures.get(name) for name in names}
        if not all(isinstance(v, bool) for v in got.values()):
            problems.append(fact + ": a measure is not recorded as a boolean "
                            + json.dumps(got, sort_keys=True))
        elif names and value != all(got.values()):
            problems.append(fact + " recorded " + repr(value) + " but its measures "
                            + json.dumps(got, sort_keys=True) + " give "
                            + repr(all(got.values())))
    derived = not problems and all(value is True for value in facts.values())
    claimed = manifest.get("covers_worktree")
    consistent = not problems and isinstance(claimed, bool) and claimed == derived
    report.add("covers_worktree_consistent", consistent,
               "recorded covers_worktree " + repr(claimed) + "; re-derived from "
               + json.dumps(facts, sort_keys=True) + " and worktree_facts "
               + json.dumps(measures, sort_keys=True) + " as " + repr(derived)
               + ("" if not problems else "; " + "; ".join(problems)))
    report.covers_worktree = bool(consistent and derived)
    if not report.covers_worktree:
        report.notes.append(
            "covers_worktree is false: this bundle archives its refs, it is not a "
            "substitute for the worktree (a working tree that differs from HEAD, "
            "ignored or untracked files, index bits, content conversion, a stash, "
            "refs or objects left out of the bundle, gitlinks or linked worktrees)")


def _rev_parse(cwd: str, spec: str) -> str | None:
    done = _run_git("-C", cwd, "rev-parse", "--verify", "--quiet", "--end-of-options", spec)
    if done.returncode != 0:
        return None
    return done.stdout.decode("ascii", "replace").strip()


# --------------------------------------------------------------------------- #
# receipts
# --------------------------------------------------------------------------- #

def write_receipt(bundle_dir: Path, *, extract_root: Path, destination: dict) -> Path:
    """Verify the bundle here and now, and only then write ARCHIVE_RECEIPT.json.

    There is deliberately no parameter through which a caller could hand in a
    report (Rev2 s6): the receipt rests on a verification this function ran
    itself, bound to the bundle directory's realpath and device. An existing
    receipt is refused, never replaced.
    """
    bundle = Path(bundle_dir)
    if os.path.lexists(bundle / RECEIPT):
        raise ArchiveRefused("a receipt already exists: " + os.fspath(bundle / RECEIPT))
    if not isinstance(destination, Mapping):
        raise ArchiveRefused("destination must be a mapping")
    report = verify_archive(bundle, extract_root=extract_root)
    if report.status != ARCHIVED_VERIFIED:
        failed = ", ".join(item["id"] for item in report.failed()) or "no extraction"
        error = ArchiveRefused("no receipt: the bundle did not verify (" + failed + ")")
        error.report = report
        raise error
    payload_name = TREE_PAYLOAD if report.payload_kind == "tree" else GIT_PAYLOAD
    manifest_sha, _ = _hash_path(bundle / MANIFEST)
    payload_sha, _ = _hash_path(bundle / payload_name)
    if (manifest_sha != report.digests.get("manifest_sha256")
            or payload_sha != report.digests.get("payload_sha256")):
        raise ArchiveRefused("the bundle changed after it was verified; no receipt")
    manifest = json.loads(_read_fresh(bundle / MANIFEST).decode("ascii"))
    real = os.path.realpath(bundle)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "bundle_id": manifest.get("bundle_id"),
        "bundle_realpath": real,
        "bundle_st_dev": os.stat(real).st_dev,
        "manifest_sha256": manifest_sha,
        "payload_sha256": payload_sha,
        "content_digest": report.digests.get("content_digest"),
        "payload_kind": report.payload_kind,
        "covers_worktree": report.covers_worktree,
        "destination": dict(destination),
        "status": report.status,
        "checks": report.checks,
        "page_cache_note": report.page_cache_note,
        "verified_utc": _iso(_utc_now()),
    }
    temp = bundle / ("." + RECEIPT + "." + secrets.token_hex(6) + ".tmp")
    _write_new(temp, (json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=True)
                      + "\n").encode("ascii"))
    _rename_noreplace(temp, bundle / RECEIPT)
    _fsync_dir(bundle)
    return bundle / RECEIPT
