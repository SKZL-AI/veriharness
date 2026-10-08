"""Receipt-gated rehydration of archived evidence trees (STORAGE-C2, P5-31).

Cold evidence may later be needed for verification. An external archive path
is never trusted input (directive section L, test S18): a bundle on an
archive target becomes evidence only after `rehydrate` has

1. accepted the receipt -- a file inside the repository whose SHA-256 is the
   digest of a receipt file committed at ``HEAD`` under
   `REHYDRATION_RECEIPTS_DIR` (read from git by
   `committed_receipt_file_digests`, never from the caller);
2. extracted the bundle with `archive_bundle.verify_archive` into a fresh
   extract root in the calling session's **registered scratch**
   (``<state_root>/projects/<p>/scratch/<s>/rehydrate-<uuid>/tree/``, beside
   its ``EXTRACT_ROOT.json`` marker, indexed ``PENDING`` -> ``VERIFIED`` /
   ``FAILED`` in the session's ``EXTRACT_ROOTS.jsonl``, see
   `hoh.archive_target`), required ``ARCHIVED_VERIFIED``, the receipt's
   content digest, and that the extracted tree's digest recomputes from the
   tree alone (`tree_content_digest`);
3. and only then moved ``tree/`` with one rename to
   ``repo_root/.rehydrated/<content_digest>/``.

Nothing unverified ever lands in the repository: there is no staging
directory inside it, and ``.rehydrated/`` (with its ownership marker
``OWNER.json``) is created only after verification **and** after a probe in
the call's own extract root has shown that ``renameat2(RENAME_NOREPLACE)``
works on this filesystem (`_probe_noreplace`). The state root must be on the
repository's filesystem so the final step is a rename, not a copy.

What a failed rehydration can leave, exactly:

* in the repository -- nothing, for every refusal up to and including the
  rename probe (receipt, git payload, filesystem, registry, verification,
  digests, renameat2 missing or unsupported). Only a failure *after* the
  probe -- creating ``.rehydrated/`` itself, or the final rename failing
  with another error (a ``<digest>/`` that appeared in between, I/O) -- can
  leave ``.rehydrated/`` with its ``OWNER.json`` (possibly incomplete) and no
  ``<digest>/`` in it; `_inside()` never trusts either;
* in registered scratch -- the extract root with its ``EXTRACT_ROOT.json``,
  the extracted ``tree/`` (``tree.noreplace-probe/`` if the probe could not
  move it back) and a ``FAILED`` line in the session's ``EXTRACT_ROOTS.jsonl``.

Rehydration is defined for tree payloads (``payload.tar.zst``) only; a git
payload is refused -- git bundles are restored with git, not rehydrated as
evidence trees (Revision 5), so no nested ``.git`` lands under
``.rehydrated/``. A tree whose digest cannot be recomputed from the extracted
files alone (a bundle with hard-link groups or stored external content) is
refused as well, because `is_trusted_evidence_path` could never trust it.

`is_trusted_evidence_path` is the rule `tools/capability_matrix.py`
``_inside()`` applies to every resolved path under ``.rehydrated/``: trusted
only inside a ``.rehydrated/<digest>/`` whose ``<digest>`` is recorded in a
committed receipt (`committed_content_digests`) **and** whose content digest
recomputes to it now.

Nothing in this module removes a file or a directory. The extract roots it
creates stay in registered scratch (REPRODUCIBLE_SCRATCH) until STORAGE-D's
VERIFIED_RELOCATION releases them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

from hoh import archive_bundle as ab
from hoh import archive_target as at

#: The one place committed receipts are read from (Revision 4 item 2).
REHYDRATION_RECEIPTS_DIR = "dogfood/storage-lifecycle/receipts"

REHYDRATED_DIR = ".rehydrated"
REHYDRATED_OWNER = "OWNER.json"
REHYDRATED_OWNER_SCHEMA = "veriharness.rehydrated-owner/1"
GIT_PAYLOAD_REFUSAL = "git bundles are restored with git, not rehydrated as evidence trees"

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_GIT_TIMEOUT = 120


class RehydrationRefused(ab.ArchiveRefused):
    """Rehydration was refused; nothing was written in the repository.
    `report` is the verification report when one was produced."""

    def __init__(self, message: str, *, report: ab.VerificationReport | None = None):
        super().__init__(message)
        self.report = report


# --------------------------------------------------------------------------- #
# committed receipts, read from git
# --------------------------------------------------------------------------- #

def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", os.fspath(repo_root), *args],
                          capture_output=True, env=ab._git_env(), timeout=_GIT_TIMEOUT,
                          check=False)


def _committed_receipt_blobs(repo_root: Path | str) -> list[tuple[str, bytes]]:
    """``(path, bytes at HEAD)`` for every file ``git ls-files`` lists under
    `REHYDRATION_RECEIPTS_DIR`; a listed file that ``HEAD`` does not hold
    (staged, not committed) is left out. Any git failure: no receipts."""
    repo = Path(repo_root)
    try:
        listed = _git(repo, "ls-files", "-z", "--", REHYDRATION_RECEIPTS_DIR)
    except (OSError, subprocess.SubprocessError):
        return []
    if listed.returncode != 0:
        return []
    out = []
    for raw in listed.stdout.split(b"\0"):
        if not raw:
            continue
        rel = os.fsdecode(raw)
        try:
            shown = _git(repo, "show", "HEAD:./" + rel)
        except (OSError, subprocess.SubprocessError):
            continue
        if shown.returncode == 0:
            out.append((rel, shown.stdout))
    return out


def committed_receipt_file_digests(repo_root: Path | str) -> frozenset[str]:
    """SHA-256 of every receipt **file** committed at ``HEAD`` under
    `REHYDRATION_RECEIPTS_DIR` (listed by ``git ls-files``, read with ``git
    show HEAD:<path>``). Used to accept a receipt as committed."""
    return frozenset(hashlib.sha256(data).hexdigest()
                     for _, data in _committed_receipt_blobs(repo_root))


def _parse_receipt(data: bytes) -> dict | None:
    try:
        record = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict) or record.get("schema") != ab.RECEIPT_SCHEMA:
        return None
    return record


def committed_content_digests(repo_root: Path | str) -> frozenset[str]:
    """The ``content_digest`` values **recorded in** the committed receipts of
    tree payloads that say ``ARCHIVED_VERIFIED`` (Revision 6). `_inside()`
    compares a ``.rehydrated/<digest>`` directory with these."""
    out = set()
    for _, data in _committed_receipt_blobs(repo_root):
        record = _parse_receipt(data)
        if (record is not None and record.get("payload_kind") == "tree"
                and record.get("status") == ab.ARCHIVED_VERIFIED
                and isinstance(record.get("content_digest"), str)
                and _DIGEST.fullmatch(record["content_digest"])):
            out.add(record["content_digest"])
    return frozenset(out)


# --------------------------------------------------------------------------- #
# the digest of a tree, recomputed from the tree alone
# --------------------------------------------------------------------------- #

def tree_content_digest(directory: Path | str) -> str | None:
    """The `archive_bundle` content digest of the tree below `directory`,
    recomputed from what is on disk now (paths, types, modes, sizes, SHA-256
    of every file, link texts), or None when it cannot be: `directory` is not
    a plain directory, or the tree holds anything but files, directories and
    symlinks, or a file with more than one link (a bundle's hard-link groups
    are not recoverable from an extraction, so such a tree is never trusted).
    """
    root = os.fsencode(os.fspath(directory))
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            return None
        entries = []
        stack = [b""]
        while stack:
            rel_dir = stack.pop()
            full_dir = os.path.join(root, rel_dir) if rel_dir else root
            with os.scandir(full_dir) as listing:
                names = [item.name for item in listing]
            for name in names:
                rel = os.path.join(rel_dir, name) if rel_dir else name
                full = os.path.join(root, rel)
                st = os.lstat(full)
                entry = dict(ab._name_fields(rel, "path"))
                entry.update(mode=stat.S_IMODE(st.st_mode), sha256=None,
                             link_target=None, hardlink_group=None)
                if stat.S_ISREG(st.st_mode):
                    if st.st_nlink != 1:
                        return None
                    digest, size = ab._hash_path(full)
                    entry.update(type="file", size=size, sha256=digest)
                elif stat.S_ISDIR(st.st_mode):
                    entry.update(type="dir", size=0)
                    stack.append(rel)
                elif stat.S_ISLNK(st.st_mode):
                    target = os.readlink(full)
                    entry.pop("link_target")
                    entry.update(type="symlink", size=len(target))
                    entry.update(ab._name_fields(target, "link_target"))
                else:
                    return None
                entries.append(entry)
    except OSError:
        return None
    return ab._content_digest(entries)


# --------------------------------------------------------------------------- #
# the trust rule for paths under .rehydrated/
# --------------------------------------------------------------------------- #

def is_trusted_evidence_path(path: Path | str, repo_root: Path | str) -> bool:
    """True only for a path that, **resolved**, lies inside `repo_root` and
    either outside ``.rehydrated/`` or inside a ``.rehydrated/<digest>/``
    whose ``<digest>`` is recorded in a receipt committed at ``HEAD``
    (`committed_content_digests`) and whose content digest recomputes to
    ``<digest>`` now. A relative `path` is taken relative to `repo_root`.
    The resolved location decides: an in-tree symlink into ``.rehydrated/``
    is judged where it leads."""
    try:
        repo = Path(repo_root).resolve(strict=True)
        given = Path(path)
        resolved = (given if given.is_absolute() else repo / given).resolve()
    except (OSError, RuntimeError):
        return False
    if not resolved.is_relative_to(repo):
        return False
    rehydrated = repo / REHYDRATED_DIR
    if not resolved.is_relative_to(rehydrated):
        return True
    parts = resolved.relative_to(rehydrated).parts
    if not parts or not _DIGEST.fullmatch(parts[0]):
        return False
    digest = parts[0]
    directory = rehydrated / digest
    try:
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            return False
    except OSError:
        return False
    if digest not in committed_content_digests(repo):
        return False
    return tree_content_digest(directory) == digest


# --------------------------------------------------------------------------- #
# rehydrate
# --------------------------------------------------------------------------- #

def _ensure_rehydrated_dir(repo: Path) -> Path:
    """``repo/.rehydrated/`` with its ownership marker; an existing directory
    is accepted only with a valid marker (never adopted)."""
    directory = repo / REHYDRATED_DIR
    if os.path.lexists(directory):
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            raise RehydrationRefused(f"{directory} is not a plain directory")
        try:
            marker = json.loads(ab._read_fresh(directory / REHYDRATED_OWNER))
        except (OSError, ValueError) as exc:
            raise RehydrationRefused(f"{directory} exists without a valid ownership "
                                     f"marker; refusing to adopt it ({exc})") from exc
        if not isinstance(marker, dict) or marker.get("schema") != REHYDRATED_OWNER_SCHEMA:
            raise RehydrationRefused(f"{directory} has a foreign ownership marker")
        return directory
    os.mkdir(directory)
    at._write_json_new(directory / REHYDRATED_OWNER, {
        "schema": REHYDRATED_OWNER_SCHEMA, "owner": "veriharness",
        "created_utc": at._utc_iso(), "repo_root": os.fspath(repo),
        "rule": "each <digest>/ is trusted only while a receipt committed at HEAD "
                "records <digest> and the tree's content digest recomputes to it",
    })
    ab._fsync_dir(directory)
    ab._fsync_dir(repo)
    return directory


def _rename_into_place(src: Path, dst: Path) -> None:
    """One ``renameat2(RENAME_NOREPLACE)`` of `src` to `dst`, which never
    replaces anything -- not even an empty directory, as ``os.rename`` would.

    There is no fallback: where libc does not offer renameat2, or the
    filesystem answers ENOSYS, EINVAL or ENOTSUP, the move fails closed with
    `RehydrationRefused` (a check followed by ``os.rename`` is not atomic and
    would move the tree onto a directory created in between). An existing
    `dst` is refused as well; any other error is raised as it is.
    """
    import ctypes
    import errno
    func = ab._renameat2()
    if func is None:
        raise RehydrationRefused("renameat2(RENAME_NOREPLACE) is not available here; "
                                 "the move into .rehydrated/ fails closed")
    rc = func(ab._AT_FDCWD, os.fsencode(src), ab._AT_FDCWD, os.fsencode(dst),
              ab._RENAME_NOREPLACE)
    if rc == 0:
        return
    err = ctypes.get_errno()
    if err in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
        raise RehydrationRefused(f"renameat2(RENAME_NOREPLACE) is not supported for "
                                 f"{dst} ({os.strerror(err)}); the move fails closed")
    if err == errno.EEXIST:
        raise RehydrationRefused(f"{dst} exists; a rehydrated tree is never replaced")
    raise OSError(err, os.strerror(err), os.fspath(dst))


def _probe_noreplace(tree: Path, report: ab.VerificationReport | None) -> None:
    """Prove that ``renameat2(RENAME_NOREPLACE)`` works on this filesystem
    before anything is created in the repository: rename `tree` to
    ``<tree>.noreplace-probe`` and back, both through `_rename_into_place`.

    The probe runs inside the call's own registered extract root, which lies
    on the repository's filesystem (`rehydrate` has compared ``st_dev``), so
    it answers for the final move as well. Success leaves no residue. Any
    failure -- renameat2 missing, ENOSYS/EINVAL/ENOTSUP, or another OSError
    -- raises `RehydrationRefused`; the extracted tree stays in registered
    scratch (under the probe name if the way back failed). Nothing is
    removed.
    """
    probe = tree.with_name(tree.name + ".noreplace-probe")
    try:
        _rename_into_place(tree, probe)
    except (RehydrationRefused, OSError) as exc:
        raise RehydrationRefused(f"this filesystem cannot rename without replacing "
                                 f"({exc}); refused before anything is created in the "
                                 f"repository", report=report) from exc
    try:
        _rename_into_place(probe, tree)
    except (RehydrationRefused, OSError) as exc:
        raise RehydrationRefused(f"the no-replace rename probe could not move the tree "
                                 f"back ({exc}); it stays in registered scratch at "
                                 f"{probe}", report=report) from exc


def rehydrate(receipt_path: Path | str, bundle_location: Path | str,
              repo_root: Path | str, *, state_root: Path | str, project_id: str,
              session_id: str, registry=None, target=None) -> Path:
    """Verify the bundle at `bundle_location` against a committed receipt and
    move its extracted tree to ``repo_root/.rehydrated/<content_digest>/``.

    Refused (`RehydrationRefused`) with nothing written in the repository: a
    receipt outside `repo_root` or whose file digest is not committed at
    ``HEAD``; a git payload; a state root on another filesystem than the
    repository; with `target` (an `ArchiveTarget` the bundle is read from), a
    target whose identity is not confirmed or a bundle outside its root; a
    bundle that does not verify, whose content digest differs from the
    receipt's, or whose extracted tree's digest does not recompute; a
    filesystem on which the no-replace rename probe fails. Refused after
    ``.rehydrated/`` exists (the module docstring says what can remain): an
    existing ``.rehydrated/<digest>/`` with different content, or a final
    rename that fails.

    `registry` defaults to a `StorageRegistry` on `state_root`; the session
    must hold a live lease there. Returns the rehydrated directory.
    """
    try:
        repo = Path(repo_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RehydrationRefused(f"repo_root {repo_root} does not exist") from exc
    given = Path(receipt_path)
    try:
        receipt_real = (given if given.is_absolute() else Path.cwd() / given).resolve(
            strict=True)
    except (OSError, RuntimeError) as exc:
        raise RehydrationRefused(f"receipt {receipt_path} does not exist") from exc
    if not receipt_real.is_relative_to(repo) or receipt_real == repo:
        raise RehydrationRefused(f"receipt {receipt_path} lies outside the repository "
                                 f"{repo}; an external path is never trusted input")
    if not stat.S_ISREG(os.lstat(receipt_real).st_mode):
        raise RehydrationRefused(f"receipt {receipt_real} is not a regular file")
    data = ab._read_fresh(receipt_real)
    receipt_digest = hashlib.sha256(data).hexdigest()
    if receipt_digest not in committed_receipt_file_digests(repo):
        raise RehydrationRefused(f"receipt {receipt_real} (sha256 {receipt_digest}) is not "
                                 f"a receipt committed at HEAD under "
                                 f"{REHYDRATION_RECEIPTS_DIR}")
    receipt = _parse_receipt(data)
    if receipt is None:
        raise RehydrationRefused(f"receipt {receipt_real} is not an archive receipt")

    bundle = Path(bundle_location)
    if receipt.get("payload_kind") == "git" or os.path.lexists(bundle / ab.GIT_PAYLOAD):
        raise RehydrationRefused(GIT_PAYLOAD_REFUSAL)
    content_digest = receipt.get("content_digest")
    if (receipt.get("payload_kind") != "tree" or receipt.get("status") != ab.ARCHIVED_VERIFIED
            or not isinstance(content_digest, str) or not _DIGEST.fullmatch(content_digest)):
        raise RehydrationRefused("the receipt does not record a verified tree payload "
                                 "with a content digest")
    try:
        if not stat.S_ISDIR(os.lstat(bundle).st_mode):
            raise RehydrationRefused(f"{bundle} is not a plain directory")
    except OSError as exc:
        raise RehydrationRefused(f"bundle {bundle} unreadable: {exc}") from exc
    if target is not None:
        check = target.check()
        if not check.identity_ok:
            raise RehydrationRefused(f"target {target.target_id}: {at.IDENTITY_MISMATCH} "
                                     f"({check.detail})")
        if not Path(os.path.realpath(bundle)).is_relative_to(
                Path(os.path.realpath(target.root))):
            raise RehydrationRefused(f"{bundle} does not lie on target {target.target_id}")

    state = Path(state_root).resolve()
    try:
        same_fs = os.stat(state).st_dev == os.stat(repo).st_dev
    except OSError as exc:
        raise RehydrationRefused(f"state root {state} unreadable: {exc}") from exc
    if not same_fs:
        raise RehydrationRefused(f"state root {state} is not on the repository's "
                                 f"filesystem; the final move must be one rename")
    if registry is None:
        from hoh.storage_registry import StorageRegistry
        registry = StorageRegistry(state)
    elif Path(registry.state_root).resolve() != state:
        raise RehydrationRefused("the registry is not rooted at state_root")
    try:
        at._require_scratch(registry, project_id, session_id)
    except at.ArchiveTargetRefused as exc:
        raise RehydrationRefused(str(exc)) from exc

    root = at.open_extract_root(registry, project_id, session_id, "rehydrate",
                                receipt.get("bundle_id"), receipt.get("payload_sha256"))
    report = None
    try:
        report = ab.verify_archive(bundle, extract_root=root.tree)
        problems = []
        if report.status != ab.ARCHIVED_VERIFIED:
            problems.append("the bundle did not verify: "
                            + ", ".join(i["id"] for i in report.failed()))
        if report.payload_kind != "tree":
            problems.append(GIT_PAYLOAD_REFUSAL)
        if report.digests.get("content_digest") != content_digest:
            problems.append(f"content digest {report.digests.get('content_digest')} != "
                            f"the receipt's {content_digest}")
        if report.digests.get("payload_sha256") != receipt.get("payload_sha256"):
            problems.append("the payload is not the one the receipt names")
        if not problems:
            recomputed = tree_content_digest(root.tree)
            if recomputed != content_digest:
                problems.append(f"the extracted tree's digest does not recompute "
                                f"({recomputed}); hard-link groups or external content "
                                f"cannot be rehydrated as evidence")
        if problems:
            raise RehydrationRefused("; ".join(problems), report=report)

        # Prove the no-replace rename works here before .rehydrated/ (and its
        # OWNER.json) can be created: a refusal leaves the repository as it was.
        _probe_noreplace(root.tree, report)
        directory = _ensure_rehydrated_dir(repo)
        dest = directory / content_digest
        if os.path.lexists(dest):
            if tree_content_digest(dest) != content_digest:
                raise RehydrationRefused(f"{dest} exists with different content",
                                         report=report)
            note = f"{dest} already held this content; the extraction stays in scratch"
        else:
            _rename_into_place(root.tree, dest)
            ab._fsync_dir(directory)
            ab._fsync_dir(root.path)
            note = f"tree/ renamed to {dest}"
    except BaseException as exc:
        if root.outcome == at.EXTRACT_PENDING:
            at.close_extract_root(root, verified=False, report=report,
                                  note=f"{type(exc).__name__}: {exc}")
        raise
    at.close_extract_root(root, verified=True, report=report,
                          receipt_digest=receipt_digest, receipt_path=receipt_real,
                          note=note)
    return dest
