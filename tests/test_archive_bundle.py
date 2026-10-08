"""P5-25: building portable archive bundles (STORAGE-C1, `hoh.archive_bundle`).

On 2026-09-28 an archive made outside this repository held only a model
cache's symlinks, not the content they pointed to. These tests hold the
builder to the rules that make that impossible here: links that escape the
archived directory are refused, a source that changes while it is archived
fails loudly, the ownership gate stands in front of every build, and the
module has no code path that removes anything.

Every test names the failure it catches and the mutant that kills it, and
carries a negative control that changes only the condition under test. Tests
work only inside `tmp_path`.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import zstandard  # noqa: F401 -- the tests need the optional dependency directly

import hoh.archive_bundle as ab

MODULE = Path(ab.__file__)
_NAME = re.compile(r"vh-arch-\d{8}T\d{6}Z-([0-9a-f]{12})-([a-z0-9-]{1,40})")


def _owned(root: Path) -> dict:
    return {"status": "OWNED", "root": str(root)}


def _out(tmp_path: Path, name: str = "out") -> Path:
    out = tmp_path / name
    out.mkdir(exist_ok=True)
    return out


def _build(source: Path, tmp_path: Path, *, label: str = "t", out: str = "out",
           **kwargs) -> Path:
    kwargs.setdefault("ownership", _owned(tmp_path))
    return ab.ArchiveBundle.build_tree(source, _out(tmp_path, out), label=label,
                                       meta={"project_id": "p", "run_id": "r"}, **kwargs)


def _manifest(bundle: Path) -> dict:
    return json.loads((bundle / ab.MANIFEST).read_text(encoding="ascii"))


def _model_cache(base: Path, link_text: str = "../../blobs/abc123") -> Path:
    """`base/blobs/abc123` (the content) and `base/snapshots/h1/model.bin`, a
    symlink to it, laid out like a Hugging Face model cache."""
    (base / "blobs").mkdir(parents=True)
    (base / "blobs" / "abc123").write_bytes(b"weights" * 1000)
    (base / "snapshots" / "h1").mkdir(parents=True)
    os.symlink(link_text, base / "snapshots" / "h1" / "model.bin")
    return base


# --------------------------------------------------------------------------- #
# K1 -- the symlink-only archive is refused
# --------------------------------------------------------------------------- #

def test_symlink_only_cache_refused(tmp_path):
    """Catches the 2026-09-28 failure: archiving `snapshots/` of a model cache,
    whose files are links into `blobs/` outside it, would store only the links.
    It must raise ArchiveRefused; a dangling link inside the source is refused
    too. Mutant M1 (drop the escape/dangling refusal) builds and fails here.

    Negative control: the same layout with the blob inside the archived
    directory builds.
    """
    cache = _model_cache(tmp_path / "models--org--m")
    with pytest.raises(ab.ArchiveRefused, match="escapes the source"):
        _build(cache / "snapshots", tmp_path, label="snap")

    absolute = tmp_path / "absolute"
    absolute.mkdir()
    os.symlink(str(cache / "blobs" / "abc123"), absolute / "model.bin")
    with pytest.raises(ab.ArchiveRefused, match="is absolute"):
        _build(absolute, tmp_path, label="absolute")

    dangling = tmp_path / "dangling"
    dangling.mkdir()
    os.symlink("missing-file", dangling / "link")
    with pytest.raises(ab.ArchiveRefused, match="does not exist"):
        _build(dangling, tmp_path, label="dangling")

    control = tmp_path / "control"
    (control / "blobs").mkdir(parents=True)
    (control / "blobs" / "abc123").write_bytes(b"weights")
    (control / "h1").mkdir()
    os.symlink("../blobs/abc123", control / "h1" / "model.bin")
    bundle = _build(control, tmp_path, label="control")
    assert (bundle / ab.TREE_PAYLOAD).is_file()


def test_symlink_only_chain_refused(tmp_path):
    """Catches a chain that is inside lexically but escapes on real
    resolution: `s/up -> ..` and `s/x -> up/..` -- `s/x` normalises to `s`
    but really resolves to the source's parent. Mutant M2 (link_inside judged
    lexically only, without realpath(strict=True)) builds it and fails here.

    Negative control: `s/x -> up` (really the source itself) builds.
    """
    chain = tmp_path / "chain"
    (chain / "s").mkdir(parents=True)
    os.symlink("..", chain / "s" / "up")
    os.symlink("up/..", chain / "s" / "x")
    with pytest.raises(ab.ArchiveRefused, match="escapes the source on real resolution"):
        _build(chain, tmp_path, label="chain")

    control = tmp_path / "control"
    (control / "s").mkdir(parents=True)
    os.symlink("..", control / "s" / "up")
    os.symlink("up", control / "s" / "x")
    entries = {e["path"]: e for e in _manifest(_build(control, tmp_path, label="ok"))["entries"]}
    assert entries["s/x"]["link_inside"] is True
    assert entries["s/up"]["link_inside"] is True


# --------------------------------------------------------------------------- #
# K5 -- source stability and the ownership gate
# --------------------------------------------------------------------------- #

def test_source_changed_during_build(tmp_path, monkeypatch):
    """Catches a source that changes while it is archived: the hook appends to
    a file after the payload is written; the build must raise SourceChanged,
    leave the bundle directory standing and mark it INCOMPLETE, and never
    produce a finished bundle. Mutant M17 (no re-stat after writing) finishes
    the bundle and fails here.

    Negative control: a hook that changes nothing lets the build finish.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "data.txt").write_bytes(b"before")
    out = _out(tmp_path)

    def grow(*, source, bundle_dir):
        with open(source / "data.txt", "ab") as handle:
            handle.write(b" and after")

    monkeypatch.setattr(ab, "_after_payload_hook", grow)
    with pytest.raises(ab.SourceChanged) as caught:
        _build(source, tmp_path, label="changing")
    bundle_dir = caught.value.bundle_dir
    assert bundle_dir is not None and bundle_dir.is_dir()
    assert (bundle_dir / ab.INCOMPLETE).is_file()
    assert (bundle_dir / ab.TREE_PAYLOAD).is_file()        # left standing, not removed
    assert not (bundle_dir / ab.MANIFEST).exists()
    assert not [p for p in out.iterdir() if _NAME.fullmatch(p.name)]

    seen = []
    monkeypatch.setattr(ab, "_after_payload_hook",
                        lambda *, source, bundle_dir: seen.append(bundle_dir))
    bundle = _build(source, tmp_path, label="steady")
    assert seen and _NAME.fullmatch(bundle.name)
    assert not (bundle / ab.INCOMPLETE).exists()


def test_ownership_gate_refuses_unowned(tmp_path):
    """Catches archiving material nobody proved is ours: a status other than
    OWNED, and a source outside the ownership root, each alone, must raise
    ArchiveRefused. Mutant M18 (gate removed) builds both and fails here.

    Negative control: OWNED with a root that contains the source builds.
    """
    root = tmp_path / "owned-root"
    source = root / "src"
    source.mkdir(parents=True)
    (source / "f").write_bytes(b"x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    for status in ("UNKNOWN", "FOREIGN", None):
        with pytest.raises(ab.ArchiveRefused, match="ownership"):
            _build(source, tmp_path, label="s", ownership={"status": status, "root": str(root)})
    with pytest.raises(ab.ArchiveRefused, match="outside the ownership root"):
        _build(source, tmp_path, label="s", ownership=_owned(elsewhere))

    bundle = _build(source, tmp_path, label="owned", ownership=_owned(root))
    assert _manifest(bundle)["ownership"]["status"] == "OWNED"


# --------------------------------------------------------------------------- #
# coverage
# --------------------------------------------------------------------------- #

def test_fifo_refused(tmp_path):
    """Catches a FIFO (or socket/device) being archived as if it were a file
    -- reading one blocks or yields nothing meaningful. Mutant: the type
    refusal removed.

    Negative control: the same source without the FIFO builds.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "f").write_bytes(b"x")
    build_ok = _build(source, tmp_path, label="nofifo")
    assert _manifest(build_ok)["totals"]["files"] == 1

    os.mkfifo(source / "pipe")
    with pytest.raises(ab.ArchiveRefused, match="FIFO"):
        _build(source, tmp_path, label="fifo")


def test_label_exfat_safe_refused(tmp_path):
    """Catches bundle names exFAT rejects or that break the naming scheme:
    labels with `:`, upper case, spaces, slashes, empty or over 40 characters
    are refused. Mutant: the label check removed.

    Negative control: a valid label builds a bundle named
    `vh-arch-YYYYMMDDTHHMMSSZ-<12 hex>-<label>`, time part first, whose 12 hex
    are the start of the content digest.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "f").write_bytes(b"x")
    for bad in ("a:b", "Upper", "with space", "a/b", "", "x" * 41, "dot.dot"):
        with pytest.raises(ab.ArchiveRefused, match="label"):
            _build(source, tmp_path, label=bad)

    bundle = _build(source, tmp_path, label="ok-label-1")
    hit = _NAME.fullmatch(bundle.name)
    assert hit and hit.group(2) == "ok-label-1"
    manifest = _manifest(bundle)
    assert hit.group(1) == manifest["content_digest"][:12]
    assert manifest["bundle_id"] == bundle.name
    assert sorted(p.name for p in bundle.iterdir()) == sorted(
        [ab.MANIFEST, ab.SUMS, ab.TREE_PAYLOAD])
    assert ":" not in bundle.name


def test_include_external_directory_target_refused(tmp_path):
    """Catches include_external storing a directory (or anything that is not a
    regular file) as external content: it must be refused even with
    include_external=True. Mutant: the regular-file requirement removed.

    Negative control: the same link pointing at a regular file outside the
    source builds with include_external=True.
    """
    outside_dir = tmp_path / "store" / "adir"
    outside_dir.mkdir(parents=True)
    (tmp_path / "store" / "w.bin").write_bytes(b"w")
    source = tmp_path / "src"
    source.mkdir()
    os.symlink("../store/adir", source / "link")
    with pytest.raises(ab.ArchiveRefused, match="not a regular file"):
        _build(source, tmp_path, label="dir", include_external=True)

    control = tmp_path / "src2"
    control.mkdir()
    os.symlink("../store/w.bin", control / "link")
    bundle = _build(control, tmp_path, label="file", include_external=True)
    assert _manifest(bundle)["totals"]["external"] == 1


def test_module_has_no_removal_call(tmp_path):
    """Catches a removal path in the module (spec s6): no call named rmtree,
    unlink, remove, rmdir, removedirs, truncate or ftruncate anywhere in its
    AST. Mutant: any such call added.

    Negative control: the same detector flags a parsed (never executed)
    snippet that contains one.
    """
    banned = {"rmtree", "unlink", "remove", "rmdir", "removedirs", "truncate", "ftruncate"}

    def calls(tree: ast.AST) -> set[str]:
        return {(n.func.attr if isinstance(n.func, ast.Attribute)
                 else getattr(n.func, "id", ""))
                for n in ast.walk(tree) if isinstance(n, ast.Call)}

    assert not calls(ast.parse(MODULE.read_text(encoding="utf-8"))) & banned
    assert calls(ast.parse("def f(p):\n    p.unlink()\n")) & banned == {"unlink"}


def test_zstandard_missing_no_fallback(tmp_path, monkeypatch):
    """Catches a silent fallback to another format when zstandard is missing:
    the build must raise ArchiveDependencyMissing naming the extra, and leave
    no bundle. Mutant: an uncompressed-tar fallback.

    Negative control: with zstandard importable the same build succeeds.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "f").write_bytes(b"x")
    monkeypatch.setitem(sys.modules, "zstandard", None)
    with pytest.raises(ab.ArchiveDependencyMissing, match=r"hoh\[archive\]"):
        _build(source, tmp_path, label="nozstd")
    assert not list(_out(tmp_path).iterdir())

    monkeypatch.undo()
    assert _NAME.fullmatch(_build(source, tmp_path, label="zstd").name)


# --------------------------------------------------------------------------- #
# K8 -- the source root itself is re-stat'ed (Rev6 s2)
# --------------------------------------------------------------------------- #

def test_source_root_toplevel_change_detected(tmp_path, monkeypatch):
    """Catches a file added to, or renamed out of, the source's top level while
    the build runs: only the source root's own re-stat (size, mtime, ctime,
    inode, listing) sees a new top-level name, since no scanned entry changed.
    The build must raise SourceChanged naming the source root and leave the
    bundle marked INCOMPLETE. Mutant M42 (no root re-stat) finishes the bundle
    for the added file, and reports the renamed one only as a vanished entry,
    not as a change of the source root -- both fail here.

    Negative controls: a nested change is still caught (by the entry re-stat),
    and an untouched build succeeds.
    """
    source = tmp_path / "src"
    (source / "sub").mkdir(parents=True)
    (source / "top.txt").write_bytes(b"top")
    (source / "sub" / "inner.txt").write_bytes(b"inner")

    def run(hook, label):
        monkeypatch.setattr(ab, "_after_payload_hook", hook)
        with pytest.raises(ab.SourceChanged) as caught:
            _build(source, tmp_path, label=label)
        assert (caught.value.bundle_dir / ab.INCOMPLETE).is_file()
        return str(caught.value)

    def add_top(*, source, bundle_dir):
        (source / "added.txt").write_bytes(b"new")

    assert "source root" in run(add_top, "added")
    os.rename(source / "added.txt", tmp_path / "added.txt.v1")     # restore by renaming

    def rename_away(*, source, bundle_dir):
        os.rename(source / "top.txt", tmp_path / "top.txt.moved")

    assert "source root" in run(rename_away, "renamed")
    os.rename(tmp_path / "top.txt.moved", source / "top.txt")

    def nested(*, source, bundle_dir):
        with open(source / "sub" / "inner.txt", "ab") as handle:
            handle.write(b"+")

    assert "inner.txt" in run(nested, "nested")

    monkeypatch.setattr(ab, "_after_payload_hook", None)
    bundle = _build(source, tmp_path, label="steady")
    assert _NAME.fullmatch(bundle.name) and not (bundle / ab.INCOMPLETE).exists()


# --------------------------------------------------------------------------- #
# K9 -- the ownership sub-rules (Rev2 s3, Rev6 s4)
# --------------------------------------------------------------------------- #

class _StatProxy:
    """A stat result with some fields replaced; every other field is real."""

    def __init__(self, real, **override):
        self._real = real
        self._override = override

    def __getattr__(self, name):
        if name in self._override:
            return self._override[name]
        return getattr(self._real, name)


def _patch_lstat(monkeypatch, path: Path, **change) -> None:
    """Make the module's lstat seam report `change` (e.g. a different st_dev)
    for exactly `path`; every other path is stat'ed for real."""
    wanted = os.fsencode(os.path.realpath(path))
    real_lstat = os.lstat

    def fake(p, *args, **kwargs):
        st = real_lstat(p, *args, **kwargs)
        if os.fsencode(p) == wanted:
            return _StatProxy(st, **{k: f(st) for k, f in change.items()})
        return st

    monkeypatch.setattr(ab, "_lstat", fake)


def _external_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """owned/src/model.bin -> ../store/w.bin (inside the ownership root
    `owned`) and owned/src/far.bin -> ../../outside/w.bin (outside it)."""
    owned = tmp_path / "owned"
    (owned / "store").mkdir(parents=True)
    (owned / "store" / "w.bin").write_bytes(b"inside-weights")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "w.bin").write_bytes(b"outside-weights")
    source = owned / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"a")
    return owned, source, owned / "store" / "w.bin"


def test_external_target_outside_root_refused(tmp_path):
    """Catches include_external storing content nobody proved is ours: a link
    target outside ownership['root'] must raise ArchiveRefused even though it
    is a regular file on the same device. Mutant M18d (the external target's
    ownership-root check removed) builds and fails here.

    Negative control: the same link to a target inside the ownership root
    builds and stores it.
    """
    owned, source, _ = _external_layout(tmp_path)
    os.symlink("../../outside/w.bin", source / "far.bin")
    with pytest.raises(ab.ArchiveRefused, match="outside the ownership root"):
        _build(source, tmp_path, label="far", include_external=True,
               ownership=_owned(owned))

    control = owned / "src2"
    control.mkdir()
    os.symlink("../store/w.bin", control / "near.bin")
    bundle = _build(control, tmp_path, label="near", include_external=True,
                    ownership=_owned(owned))
    assert _manifest(bundle)["externals"][0]["link"] == "near.bin"


def test_external_target_other_device_refused(tmp_path, monkeypatch):
    """Catches include_external reaching onto another filesystem: the target is
    reported (through the module's lstat seam, for that one path only -- no
    second filesystem is assumed) on a different st_dev, and must be refused.
    Mutant M18e (the external target's device check removed) builds and fails
    here.

    Negative control: without the patched device the same build succeeds.
    """
    owned, source, target = _external_layout(tmp_path)
    os.symlink("../store/w.bin", source / "model.bin")
    _patch_lstat(monkeypatch, target, st_dev=lambda st: st.st_dev + 1)
    with pytest.raises(ab.ArchiveRefused, match="another device"):
        _build(source, tmp_path, label="otherdev", include_external=True,
               ownership=_owned(owned))

    monkeypatch.setattr(ab, "_lstat", os.lstat)
    bundle = _build(source, tmp_path, label="samedev", include_external=True,
                    ownership=_owned(owned))
    assert _manifest(bundle)["totals"]["external"] == 1


def test_entry_other_device_refused(tmp_path, monkeypatch):
    """Catches an ordinary entry that lies on another device than the source
    (a mount point inside the tree): it must be refused. One file is reported
    on a different st_dev through the module's lstat seam. Mutant M18c (the
    per-entry device check removed) builds and fails here.

    Negative control: without the patched device the same source builds.
    """
    source = tmp_path / "src"
    (source / "mnt").mkdir(parents=True)
    (source / "mnt" / "f.bin").write_bytes(b"elsewhere")
    (source / "g.bin").write_bytes(b"here")
    _patch_lstat(monkeypatch, source / "mnt" / "f.bin", st_dev=lambda st: st.st_dev + 1)
    with pytest.raises(ab.ArchiveRefused, match="entry on another device"):
        _build(source, tmp_path, label="otherdev")

    monkeypatch.setattr(ab, "_lstat", os.lstat)
    assert _manifest(_build(source, tmp_path, label="samedev"))["totals"]["files"] == 2


def test_fstat_identity_mismatch_refused(tmp_path, monkeypatch):
    """Catches a file swapped between the scan and the read: the descriptor
    opened with O_NOFOLLOW reports (through the module's fstat seam, for that
    one file) a different st_ino, and separately a different size, than the
    scan saw. The build must raise SourceChanged and mark the bundle
    INCOMPLETE. Mutant M18f (fstat identity not compared) builds and fails
    here.

    Negative control: with the real fstat the same source builds.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "victim.bin").write_bytes(b"0123456789")
    (source / "other.bin").write_bytes(b"other")
    identity = os.stat(source / "victim.bin")
    real_fstat = os.fstat

    for field in ("st_ino", "st_size"):
        def fake(fd, field=field):
            st = real_fstat(fd)
            if (st.st_dev, st.st_ino) == (identity.st_dev, identity.st_ino):
                return _StatProxy(st, **{field: getattr(st, field) + 1})
            return st

        monkeypatch.setattr(ab, "_fstat", fake)
        with pytest.raises(ab.SourceChanged, match="replaced or resized") as caught:
            _build(source, tmp_path, label="swap-" + field.replace("_", "-"))
        assert (caught.value.bundle_dir / ab.INCOMPLETE).is_file()

    monkeypatch.setattr(ab, "_fstat", os.fstat)
    assert _manifest(_build(source, tmp_path, label="steady"))["totals"]["files"] == 2


# --------------------------------------------------------------------------- #
# K10 / K11 -- no replacing rename (Rev6 s7, Rev7 s3)
# --------------------------------------------------------------------------- #

def test_no_replacing_rename(tmp_path, monkeypatch):
    """Catches publishing by a check followed by an ordinary rename: with
    renameat2 unavailable, the target appears after any existence check,
    immediately before the publishing call (os.link, os.mkdir, os.rename or
    os.replace each first create it, then do their real work). The publish
    must refuse with FileExistsError and the target's bytes must be unchanged
    -- for a file and for a directory. Mutant M46 (lexists, then os.rename)
    passes its check, replaces the file target and fails here. The module
    source also has no os.rename/os.replace/.rename call at all.

    Negative controls: with renameat2 unavailable and no race, a file is
    published by a hard link (the temporary name stays as a second link to
    the same inode) and a directory by mkdir plus links; a complete build and
    a receipt both succeed that way.
    """
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    renames = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute)
               and (n.func.attr in ("rename", "renames")
                    or (n.func.attr == "replace" and isinstance(n.func.value, ast.Name)
                        and n.func.value.id == "os"))]
    assert not renames, renames

    monkeypatch.setattr(ab, "_renameat2", lambda: None)
    real = {name: getattr(os, name) for name in ("link", "mkdir", "rename", "replace")}

    def racing(final: Path, create):
        """Each publishing call first lets the target appear, then runs."""
        def wrap(name):
            def call(src, dst=None, *args, **kwargs):
                where = dst if name != "mkdir" else src
                if os.fspath(where) == os.fspath(final) and not os.path.lexists(final):
                    create()
                if name == "mkdir":
                    return real[name](src, *([dst] if dst is not None else []), *args, **kwargs)
                return real[name](src, dst, *args, **kwargs)
            return call
        for name in real:
            monkeypatch.setattr(os, name, wrap(name))

    def unrace():
        for name, func in real.items():
            monkeypatch.setattr(os, name, func)

    # a file
    temp, final = tmp_path / ".r.tmp", tmp_path / "RECEIPT"
    temp.write_bytes(b"ours")
    racing(final, lambda: final.write_bytes(b"THEIRS"))
    with pytest.raises(FileExistsError):
        ab._rename_noreplace(temp, final)
    unrace()
    assert final.read_bytes() == b"THEIRS" and temp.read_bytes() == b"ours"

    # a directory
    staging, target = tmp_path / "stage.building", tmp_path / "published"
    staging.mkdir()
    (staging / "payload").write_bytes(b"ours")

    def make_theirs():
        real["mkdir"](target)
        (target / "payload").write_bytes(b"THEIRS")

    racing(target, make_theirs)
    with pytest.raises(FileExistsError):
        ab._rename_noreplace(staging, target)
    unrace()
    assert (target / "payload").read_bytes() == b"THEIRS"
    assert sorted(p.name for p in target.iterdir()) == ["payload"]

    # controls: the fallback publishes without replacing anything
    fresh = tmp_path / "FRESH"
    ab._rename_noreplace(temp, fresh)
    assert fresh.read_bytes() == b"ours"
    assert os.stat(fresh).st_ino == os.stat(temp).st_ino      # second link, same inode
    source = tmp_path / "src"
    source.mkdir()
    (source / "f").write_bytes(b"x")
    bundle = _build(source, tmp_path, label="linked")
    assert _NAME.fullmatch(bundle.name)
    assert sorted(p.name for p in bundle.iterdir()) == sorted(
        [ab.MANIFEST, ab.SUMS, ab.TREE_PAYLOAD])
    root = tmp_path / "extract"
    root.mkdir()
    receipt = ab.write_receipt(bundle, extract_root=root, destination={"kind": "test"})
    assert json.loads(receipt.read_text(encoding="ascii"))["status"] == ab.ARCHIVED_VERIFIED


# --------------------------------------------------------------------------- #
# K12 -- surviving checks of the tree builder (Rev8 s3-s4, Rev9 s2)
# --------------------------------------------------------------------------- #

def test_mutant_root_listing(tmp_path, monkeypatch):
    """Catches a top-level file added during the build while the source root's
    own stat is held equal: through the `_lstat` seam the root reports its
    pre-build stat (mtime and ctime unchanged), so only the comparison of the
    root's entry listing can see the new name. The build must raise
    SourceChanged naming the listing change and leave INCOMPLETE. Mutant
    (the root-listing compare removed) finishes the bundle and fails here.

    Negative control: the same held stat without an added file builds.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"a")
    held = os.lstat(source)
    wanted = os.fsencode(os.path.abspath(source))
    real_lstat = os.lstat

    def fake(p, *args, **kwargs):
        if os.fsencode(p) == wanted:
            return held
        return real_lstat(p, *args, **kwargs)

    monkeypatch.setattr(ab, "_lstat", fake)

    def add_top(*, source, bundle_dir):
        (source / "added.txt").write_bytes(b"new")

    monkeypatch.setattr(ab, "_after_payload_hook", add_top)
    with pytest.raises(ab.SourceChanged, match="listing changed") as caught:
        _build(source, tmp_path, label="listing")
    assert "added.txt" in str(caught.value)
    assert (caught.value.bundle_dir / ab.INCOMPLETE).is_file()

    held = os.lstat(source)
    monkeypatch.setattr(ab, "_after_payload_hook", None)
    bundle = _build(source, tmp_path, label="steady")
    assert _manifest(bundle)["totals"]["files"] == 2


def test_mutant_m30(tmp_path):
    """Catches a bundle written into the source it archives (M30, out_parent
    inside the source admitted): out_parent = source/sub, and the same
    directory reached through a symlinked path, must each raise
    ArchiveRefused at build time, and no vh-arch-* entry may appear under the
    source. Mutant M30 creates its staging directory inside the source, the
    re-stat then raises SourceChanged instead, and it fails here.

    Negative control: an out_parent outside the source builds.
    """
    source = tmp_path / "src"
    (source / "sub").mkdir(parents=True)
    (source / "f.txt").write_bytes(b"f")
    alias = tmp_path / "alias"
    os.symlink(source, alias)
    for inside in (source / "sub", alias / "sub"):
        with pytest.raises(ab.ArchiveRefused, match="inside the source"):
            ab.ArchiveBundle.build_tree(source, inside, label="self", meta={},
                                        ownership=_owned(tmp_path))
    assert not [p for p in source.rglob("*") if p.name.startswith("vh-arch-")]

    bundle = _build(source, tmp_path, label="outside")
    assert _NAME.fullmatch(bundle.name)


def test_mutant_m37(tmp_path):
    """Catches external content stored under a name the source already uses
    (M37, a `.vh-external` collision admitted): a source with a top-level
    `.vh-external` and include_external=True must raise ArchiveRefused --
    with and without an external link in it. Mutant M37 builds the source
    without a link (and mixes both trees with one) and fails here.

    Negative controls: the same source builds with include_external=False,
    and a source without `.vh-external` builds with include_external=True.
    """
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "w.bin").write_bytes(b"weights")
    source = tmp_path / "src"
    (source / ".vh-external").mkdir(parents=True)
    (source / ".vh-external" / "0").write_bytes(b"ours, not external")
    (source / "a.txt").write_bytes(b"a")
    with pytest.raises(ab.ArchiveRefused, match="already has .vh-external"):
        _build(source, tmp_path, label="nolink", include_external=True)
    os.symlink("../store/w.bin", source / "model.bin")
    with pytest.raises(ab.ArchiveRefused, match="already has .vh-external"):
        _build(source, tmp_path, label="withlink", include_external=True)

    plain = tmp_path / "plain"
    (plain / ".vh-external").mkdir(parents=True)
    (plain / ".vh-external" / "0").write_bytes(b"ours")
    assert _manifest(_build(plain, tmp_path, label="plain"))["totals"]["files"] == 1
    clean = tmp_path / "clean"
    clean.mkdir()
    os.symlink("../store/w.bin", clean / "model.bin")
    bundle = _build(clean, tmp_path, label="clean", include_external=True)
    assert _manifest(bundle)["totals"]["external"] == 1


def test_mutant_m39(tmp_path, monkeypatch):
    """Catches a file that grows while it is read (M39): through the `_fstat`
    seam, bytes are appended to the file right after its identity was taken
    and before it is read to the end. The build must raise SourceChanged
    "grew while it was read" while writing the payload -- `_after_payload_hook`
    is never reached, which separates this check from the post-build re-stat
    that would otherwise mask the mutant. Mutant M39 (the read-to-EOF check
    removed) reaches the hook and fails here.

    Negative control: with the real fstat the same source builds and the hook
    runs.
    """
    source = tmp_path / "src"
    source.mkdir()
    victim = source / "grow.bin"
    victim.write_bytes(b"0123456789")
    (source / "other.bin").write_bytes(b"other")
    identity = os.stat(victim)
    real_fstat = os.fstat

    def fake(fd):
        st = real_fstat(fd)
        if (st.st_dev, st.st_ino) == (identity.st_dev, identity.st_ino):
            with open(victim, "ab") as handle:
                handle.write(b"MORE")
        return st

    hooked = []
    monkeypatch.setattr(ab, "_fstat", fake)
    monkeypatch.setattr(ab, "_after_payload_hook", lambda **kw: hooked.append(kw))
    with pytest.raises(ab.SourceChanged, match="grew while it was read") as caught:
        _build(source, tmp_path, label="grows")
    assert hooked == []
    assert (caught.value.bundle_dir / ab.INCOMPLETE).is_file()

    monkeypatch.setattr(ab, "_fstat", os.fstat)
    bundle = _build(source, tmp_path, label="steady")
    assert len(hooked) == 1 and _manifest(bundle)["totals"]["files"] == 2


# --------------------------------------------------------------------------- #
# K11 -- git: isolation, gates, facts and the post-create re-check
# (Rev8 s1-s2, Rev9, Rev9a-9d)
# --------------------------------------------------------------------------- #

_OLD_NS = 1_700_000_000 * 1_000_000_000          # a fixed mtime well in the past
_OUT = iter(range(1_000_000))


def _tenv(**extra) -> dict:
    """The tests' own git environment: nothing inherited from GIT_* (so a
    monkeypatched GIT_DIR cannot reach the test's git), no user or system
    configuration, an explicit identity."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="Archive Test", GIT_AUTHOR_EMAIL="archive-test@example.invalid",
               GIT_COMMITTER_NAME="Archive Test",
               GIT_COMMITTER_EMAIL="archive-test@example.invalid",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    env.update(extra)
    return env


def _g(repo: Path, *args: str, **extra) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], env=_tenv(**extra),
                          capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _write_old(path: Path, data: bytes) -> None:
    """Write `data` with an mtime well in the past -- a different one for
    every write -- so git's index never treats the entry as racily clean
    (its stat data alone then decides)."""
    path.write_bytes(data)
    when = _OLD_NS + next(_OUT) * 1_000_000_000
    os.utime(path, ns=(when, when))


def _new_repo(path: Path, content: bytes = b"two\n", ignore: bytes | None = None) -> Path:
    """A repository with two commits on main, tag v1, a.txt and (if given) a
    committed .gitignore; its working tree equals HEAD."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], env=_tenv(), check=True)
    _write_old(path / "a.txt", b"one\n")
    _g(path, "add", "a.txt")
    _g(path, "commit", "-q", "-m", "first")
    _write_old(path / "a.txt", content)
    if ignore is not None:
        _write_old(path / ".gitignore", ignore)
        _g(path, "add", ".gitignore")
    _g(path, "commit", "-q", "-am", "second")
    _g(path, "tag", "v1")
    return path


def _git_bundle(repo: Path, tmp_path: Path, refs=("main", "v1"), *, label: str = "g",
                root: Path | None = None, out: Path | None = None) -> Path:
    out = out if out is not None else _out(tmp_path, "gout-%d" % next(_OUT))
    return ab.ArchiveBundle.build_git(repo, list(refs), out, label=label, meta={},
                                      ownership=_owned(root if root is not None else tmp_path))


def _git_manifest(repo: Path, tmp_path: Path, refs=("main", "v1"), **kwargs) -> dict:
    return _manifest(_git_bundle(repo, tmp_path, refs, **kwargs))


def _covered(manifest: dict) -> bool:
    """covers_worktree, with every recorded fact and measure true."""
    facts = [manifest[f] for f in ab._FACT_MEASURES]
    return (manifest["covers_worktree"] is True and all(facts)
            and all(manifest["worktree_facts"].values()))


def _clean_control(tmp_path: Path, name: str, refs=("main", "v1"), setup=None) -> dict:
    """A fresh repository -- prepared by `setup` without the condition under
    test -- reaches covers_worktree with every fact and measure true."""
    repo = _new_repo(tmp_path / name)
    if setup is not None:
        setup(repo)
    manifest = _git_manifest(repo, tmp_path, refs, label="control")
    assert _covered(manifest), manifest
    return manifest


def _is_create(args) -> bool:
    args = list(args)
    return "bundle" in args and args[args.index("bundle") + 1:][:1] == ["create"]


def _during_create(monkeypatch, action) -> list:
    """Wrap the module's git runner so that `action` runs while
    `git bundle create` runs -- after git wrote the bundle, before build_git
    sees it return."""
    real = ab._run_git
    seen = []

    def wrapped(*args):
        done = real(*args)
        if _is_create(args):
            seen.append(args)
            action()
        return done

    monkeypatch.setattr(ab, "_run_git", wrapped)
    return seen


def _refused_during_create(repo: Path, tmp_path: Path) -> ab.SourceChanged:
    out = _out(tmp_path, "race-out")
    with pytest.raises(ab.SourceChanged) as caught:
        ab.ArchiveBundle.build_git(repo, ["main", "v1"], out, label="race", meta={},
                                   ownership=_owned(tmp_path))
    staging = caught.value.bundle_dir
    assert staging is not None and staging.parent == out and staging.name.endswith(".building")
    assert (staging / ab.INCOMPLETE).is_file()
    assert not [p for p in out.iterdir() if _NAME.fullmatch(p.name)]
    assert not [p for p in out.rglob("*") if "RECEIPT" in p.name]
    return caught.value


def test_git_worktree_change_during_bundle_create_refused(tmp_path, monkeypatch):
    """Catches a git fact recorded before `git bundle create` that no longer
    holds after it (sarch2-a B1, repro p10): an untracked unique file is
    written while bundle create runs. The build must raise SourceChanged,
    leave the staging directory INCOMPLETE, publish no vh-arch-* bundle and
    write no receipt. Mutant M48 (the post-create re-check removed) publishes
    a bundle claiming covers_worktree and fails here.

    Negative control: the same wrapper doing nothing builds a covering bundle.
    """
    repo = _new_repo(tmp_path / "repo")
    seen = _during_create(monkeypatch, lambda: _write_old(repo / "unique.txt", b"only here"))
    error = _refused_during_create(repo, tmp_path)
    assert seen and "computed_tree_equal" in str(error) and "worktree_clean" in str(error)

    control = _new_repo(tmp_path / "control")
    seen = _during_create(monkeypatch, lambda: None)
    assert _covered(_git_manifest(control, tmp_path)) and seen


def test_git_commit_during_bundle_create_refused(tmp_path, monkeypatch):
    """Catches a ref that moves while `git bundle create` runs (repro p11): a
    commit is made on the bundled branch main. The build must raise
    SourceChanged (the re-parsed refs and the for-each-ref listing differ),
    INCOMPLETE left, nothing published or receipted. Mutant M48 fails here.

    Negative control: the same wrapper doing nothing builds.
    """
    repo = _new_repo(tmp_path / "repo")
    seen = _during_create(monkeypatch, lambda: _g(repo, "commit", "-q", "--allow-empty",
                                                  "-m", "during"))
    error = _refused_during_create(repo, tmp_path)
    assert seen and "refs" in str(error) and "for-each-ref" in str(error)

    control = _new_repo(tmp_path / "control")
    _during_create(monkeypatch, lambda: None)
    assert _covered(_git_manifest(control, tmp_path))


def test_git_linked_worktree_common_dir_outside_refused(tmp_path):
    """Catches a repository whose object store lies outside the ownership
    root behind a linked worktree inside it (Rev8 s2): the worktree passes
    the path gate, its git common dir does not. The build must be refused,
    naming the common dir, before anything is written. Mutant M49 (common-dir
    check removed) is refused only later for the object directory, with
    another reason, and fails here.

    Negative control: a linked worktree of a repository inside the root builds.
    """
    owned = tmp_path / "owned"
    owned.mkdir()
    foreign = _new_repo(tmp_path / "foreign")
    _g(foreign, "worktree", "add", "-q", "--detach", str(owned / "wt"))
    out = _out(tmp_path, "gout-wt")
    with pytest.raises(ab.ArchiveRefused, match="git common dir lies outside"):
        _git_bundle(owned / "wt", tmp_path, ["HEAD"], root=owned, out=out)
    assert not list(out.iterdir())

    inner = _new_repo(owned / "inner")
    _g(inner, "worktree", "add", "-q", "--detach", str(owned / "wt2"))
    manifest = _git_manifest(owned / "wt2", tmp_path, ["HEAD"], root=owned)
    assert manifest["single_worktree"] is False        # built, but not covering


def test_git_alternates_outside_refused(tmp_path):
    """Catches objects borrowed from a store outside the ownership root
    (Rev8 s2, Rev9 s1): a `git clone --shared` (absolute alternates entry)
    and a repository whose alternates entry is relative to its own objects
    directory must each be refused before anything is written. Mutant M50
    (alternates not followed) archives foreign objects and fails here.

    Negative control: a `--shared` clone of a repository inside the root builds.
    """
    owned = tmp_path / "owned"
    owned.mkdir()
    foreign = _new_repo(tmp_path / "foreign")
    subprocess.run(["git", "clone", "-q", "--shared", str(foreign), str(owned / "clone")],
                   env=_tenv(), check=True)
    out = _out(tmp_path, "gout-alt")
    with pytest.raises(ab.ArchiveRefused, match="alternates"):
        _git_bundle(owned / "clone", tmp_path, ["main"], root=owned, out=out)

    rel = owned / "rel"
    subprocess.run(["git", "init", "-q", "-b", "main", str(rel)], env=_tenv(), check=True)
    objects = rel / ".git" / "objects"
    (objects / "info").mkdir(exist_ok=True)
    with open(objects / "info" / "alternates", "x", encoding="utf-8") as handle:
        handle.write(os.path.relpath(foreign / ".git" / "objects", objects) + "\n")
    _g(rel, "update-ref", "refs/heads/main", _g(foreign, "rev-parse", "main"))
    with pytest.raises(ab.ArchiveRefused, match="alternates"):
        _git_bundle(rel, tmp_path, ["main"], root=owned, out=out)
    assert not list(out.iterdir())

    inner = _new_repo(owned / "inner")
    subprocess.run(["git", "clone", "-q", "--shared", str(inner), str(owned / "shared")],
                   env=_tenv(), check=True)
    assert _git_bundle(owned / "shared", tmp_path, ["main"], root=owned).is_dir()


def test_git_env_object_store_ignored(tmp_path, monkeypatch):
    """Catches an object store taken from the inherited environment (Rev9 s1):
    an owned repository without an alternates file whose main was set to a
    commit that exists only in a foreign store, and
    GIT_ALTERNATE_OBJECT_DIRECTORIES pointing at that store. The build must
    be refused (or its bundle never ARCHIVED_VERIFIED). Mutant M51 (the
    environment passed through unchanged) archives the foreign objects,
    verifies, and fails here.

    Negative control: with the same variable set, an ordinary owned
    repository builds and verifies with its own commits.
    """
    owned = tmp_path / "owned"
    owned.mkdir()
    foreign = _new_repo(tmp_path / "foreign", content=b"foreign\n")
    _g(foreign, "commit", "-q", "--allow-empty", "-m", "only in the foreign store")
    foreign_only = _g(foreign, "rev-parse", "main")
    store = str(foreign / ".git" / "objects")
    repo = _new_repo(owned / "repo")
    _g(repo, "update-ref", "refs/heads/main", foreign_only,
       GIT_ALTERNATE_OBJECT_DIRECTORIES=store)
    assert not os.path.exists(repo / ".git" / "objects" / "info" / "alternates")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", store)
    try:
        bundle = _git_bundle(repo, tmp_path, ["main"], root=owned)
    except ab.ArchiveRefused:
        pass
    else:
        extract = _out(tmp_path, "extract-env")
        assert ab.verify_archive(bundle, extract_root=extract).status != ab.ARCHIVED_VERIFIED

    control = _new_repo(owned / "control")
    bundle = _git_bundle(control, tmp_path, ["main"], root=owned)
    assert _manifest(bundle)["refs"][0]["commit"] == _g(control, "rev-parse", "main")
    report = ab.verify_archive(bundle, extract_root=_out(tmp_path, "extract-control"))
    assert report.status == ab.ARCHIVED_VERIFIED, report.failed()


def test_git_env_git_dir_ignored(tmp_path, monkeypatch):
    """Catches a repository chosen by an inherited GIT_DIR (Rev9 s1): with
    GIT_DIR naming another repository, the manifest's commits must still be
    the owned repository's, as the test rev-parses them under a clean
    environment. Mutant M51 (environment passed through) records the other
    repository's commits and fails here.

    Negative control: without GIT_DIR the same build records the same commits.
    """
    owned = _new_repo(tmp_path / "owned")
    other = _new_repo(tmp_path / "other", content=b"another history\n")
    _g(other, "commit", "-q", "--allow-empty", "-m", "other")
    assert _g(other, "rev-parse", "main") != _g(owned, "rev-parse", "main")
    want = {"refs/heads/main": _g(owned, "rev-parse", "main"),
            "refs/tags/v1": _g(owned, "rev-parse", "v1^{commit}")}

    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    got = {r["name"]: r["commit"] for r in _git_manifest(owned, tmp_path)["refs"]}
    assert got == want
    monkeypatch.delenv("GIT_DIR")
    got = {r["name"]: r["commit"] for r in _git_manifest(owned, tmp_path)["refs"]}
    assert got == want


def test_git_out_parent_inside_worktree_refused(tmp_path):
    """Catches a bundle staged inside the repository it archives (Rev9 s3):
    out_parent inside the worktree -- directly and through a symlinked path
    -- must be refused before anything is written. Mutant M52 (no such
    check) stages there, the post-create re-check then sees the staging
    directory as an untracked change and raises SourceChanged instead, and it
    fails here.

    Negative control: an out_parent outside the repository builds a covering
    bundle.
    """
    repo = _new_repo(tmp_path / "repo")
    inside = repo / "bundles"
    inside.mkdir()
    alias = tmp_path / "alias"
    os.symlink(repo, alias)
    for out in (inside, alias / "bundles"):
        with pytest.raises(ab.ArchiveRefused, match="inside the repository"):
            ab.ArchiveBundle.build_git(repo, ["main", "v1"], out, label="g", meta={},
                                       ownership=_owned(tmp_path))
    assert not list(inside.iterdir())
    _clean_control(tmp_path, "control")


@pytest.mark.parametrize("route", ["repo_config", "env"])
def test_untracked_hidden_by_config_not_covered(tmp_path, monkeypatch, route):
    """Catches an untracked file hidden from git's own view by configuration
    (Rev9a, Rev10a s1): status.showUntrackedFiles=no, set in .git/config or
    through GIT_CONFIG_COUNT/KEY_0/VALUE_0, with an untracked file present.
    `computed_tree_equal` (the archiver's own walk; the withdrawn
    `status_clean` measure no longer exists) must be False and
    covers_worktree False. Mutant M60a (the computed comparison replaced by
    git's view) fails here; the env case also fails the scrub mutant M51.

    Negative control: the same configuration without the untracked file
    covers the worktree.
    """
    def configure(repo):
        if route == "repo_config":
            _g(repo, "config", "status.showUntrackedFiles", "no")
        else:
            monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
            monkeypatch.setenv("GIT_CONFIG_KEY_0", "status.showUntrackedFiles")
            monkeypatch.setenv("GIT_CONFIG_VALUE_0", "no")

    _clean_control(tmp_path, "control", setup=configure)
    repo = _new_repo(tmp_path / "repo")
    configure(repo)
    _write_old(repo / "untracked.txt", b"loose")
    manifest = _git_manifest(repo, tmp_path)
    assert "status_clean" not in manifest["worktree_facts"]
    assert manifest["worktree_facts"]["computed_tree_equal"] is False
    assert manifest["worktree_clean"] is False and manifest["covers_worktree"] is False


def _only_false(manifest: dict, measure: str) -> None:
    """`measure` is the one false measure; every other measure is true."""
    measures = manifest["worktree_facts"]
    assert measures[measure] is False, measures
    assert all(v for k, v in measures.items() if k != measure), measures


def test_assume_unchanged_not_clean(tmp_path):
    """Catches an assume-unchanged entry, with no content change: the bit
    alone makes `index_bits_clear` False (the computed walk sees a clean
    tree), so worktree_clean and covers_worktree are False.
    Mutant M54a (the lower-case tag check dropped) covers it and fails here.

    Negative control: the same repository without the bit covers.
    """
    _clean_control(tmp_path, "control")
    repo = _new_repo(tmp_path / "repo")
    _g(repo, "update-index", "--assume-unchanged", "a.txt")
    manifest = _git_manifest(repo, tmp_path)
    _only_false(manifest, "index_bits_clear")
    assert manifest["worktree_clean"] is False and manifest["covers_worktree"] is False


def test_skip_worktree_not_clean(tmp_path):
    """Catches a skip-worktree entry, with no content change: only
    `index_bits_clear` is False. Mutant M54b (the `S` tag check dropped)
    covers it and fails here.

    Negative control: the same repository without the bit covers.
    """
    _clean_control(tmp_path, "control")
    repo = _new_repo(tmp_path / "repo")
    _g(repo, "update-index", "--skip-worktree", "a.txt")
    manifest = _git_manifest(repo, tmp_path)
    _only_false(manifest, "index_bits_clear")
    assert manifest["worktree_clean"] is False and manifest["covers_worktree"] is False


def test_ignore_stat_not_clean(tmp_path):
    """Catches core.ignoreStat=true in the repository's configuration, with no
    content change: only `ignore_stat_unset` is False. Mutant M54c (the
    configuration check dropped) covers it and fails here.

    Negative control: the same repository without the setting covers.
    """
    _clean_control(tmp_path, "control")
    repo = _new_repo(tmp_path / "repo")
    _g(repo, "config", "core.ignoreStat", "true")
    manifest = _git_manifest(repo, tmp_path)
    _only_false(manifest, "ignore_stat_unset")
    assert manifest["worktree_clean"] is False and manifest["covers_worktree"] is False


def _detach_to(repo: Path, oid: str) -> None:
    _g(repo, "update-ref", "--no-deref", "HEAD", oid)


def _side_commit(repo: Path, message: str) -> str:
    """A new commit with HEAD's tree on top of HEAD; no ref points at it and
    the working tree is untouched."""
    return _g(repo, "commit-tree", "-p", "HEAD", "-m", message, "HEAD^{tree}")


def test_detached_head_commit_not_covered(tmp_path):
    """Catches a detached HEAD on a commit no bundled ref reaches (Rev9b s2):
    `detached_head_reachable` and `worktree_heads_reachable` are False,
    all_refs_included False. Mutant M55a (neither the detached HEAD nor the
    worktree HEADs added) covers it and fails here.

    Negative control: HEAD detached at main's own commit covers.
    """
    _clean_control(tmp_path, "control",
                   setup=lambda r: _detach_to(r, _g(r, "rev-parse", "main")))
    repo = _new_repo(tmp_path / "repo")
    _detach_to(repo, _side_commit(repo, "detached"))
    manifest = _git_manifest(repo, tmp_path)
    measures = manifest["worktree_facts"]
    assert measures["detached_head_reachable"] is False
    assert measures["worktree_heads_reachable"] is False
    assert measures["refs_in_bundle"] is True
    assert manifest["all_refs_included"] is False and manifest["covers_worktree"] is False


def test_notes_ref_not_covered(tmp_path):
    """Catches a ref outside refs/heads and refs/tags left out of the bundle
    (Rev9b s2): a notes ref makes `refs_in_bundle` and all_refs_included
    False. Mutant M55b (for-each-ref restricted to heads and tags) covers it
    and fails here.

    Negative control: the same notes ref bundled by name covers.
    """
    _clean_control(tmp_path, "control", refs=("main", "v1", "refs/notes/commits"),
                   setup=lambda r: _g(r, "notes", "add", "-m", "note", "HEAD"))
    repo = _new_repo(tmp_path / "repo")
    _g(repo, "notes", "add", "-m", "note", "HEAD")
    manifest = _git_manifest(repo, tmp_path)
    assert manifest["worktree_facts"]["refs_in_bundle"] is False
    assert manifest["all_refs_included"] is False and manifest["covers_worktree"] is False


def test_stash_ref_without_reflog_not_empty(tmp_path):
    """Catches a stash that a reflog cannot see (Rev9b s3, Rev10b s1):
    refs/stash written with core.logAllRefUpdates=false has no reflog, so
    `stash_reflog_empty` (`reflog show --format=%H refs/stash`, which
    replaced `stash list`) is True; `stash_ref_absent` and stash_empty must
    be False. Mutant M56 (the rev-parse refs/stash condition dropped)
    reports stash_empty True and fails here.

    Negative control: the same repository without refs/stash covers.
    """
    _clean_control(tmp_path, "control")
    repo = _new_repo(tmp_path / "repo")
    _g(repo, "-c", "core.logAllRefUpdates=false", "update-ref", "refs/stash",
       _side_commit(repo, "stash without reflog"))
    assert _g(repo, "stash", "list") == ""
    manifest = _git_manifest(repo, tmp_path)
    assert manifest["worktree_facts"]["stash_reflog_empty"] is True
    assert manifest["worktree_facts"]["stash_ref_absent"] is False
    assert manifest["stash_empty"] is False and manifest["covers_worktree"] is False


def _fsmonitor_hook(tmp_path: Path) -> tuple[Path, Path]:
    """A silent fsmonitor hook outside the repository: it prints nothing (as
    if nothing changed) and leaves a marker file when it is run."""
    hook, marker = tmp_path / "fsmonitor-hook.sh", tmp_path / "fsmonitor-ran"
    with open(hook, "x", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\necho ran >> '%s'\nexit 0\n" % marker)
    os.chmod(hook, 0o755)
    return hook, marker


_HIDDEN = ["assume_unchanged", "skip_worktree", "fsmonitor", "filemode", "detached_head"]


@pytest.mark.parametrize("case", _HIDDEN)
def test_git_facts_not_hidden_by_index_or_config(tmp_path, case):
    """Catches a change hidden from git by the index or the repository's
    configuration (Rev9b s1, s6, Rev10a s1): assume-unchanged or
    skip-worktree on a changed file (`index_bits_clear` False), a silent
    fsmonitor hook with a changed file (`computed_tree_equal` False, and the
    hook never runs inside the archiver), core.fileMode=false with a mode
    change (`computed_tree_equal` False: the walk computes the executable
    bit itself), a detached-HEAD commit (`detached_head_reachable` False).
    Mutants, one per measure: the index-bit check dropped, the computed
    comparison replaced by git's view (M60a), the detached HEAD not added --
    each fails here.

    Negative control: the same preparation without the change covers (the
    fsmonitor hook configured but never run).
    """
    hook = marker = None
    if case == "fsmonitor":
        hook, marker = _fsmonitor_hook(tmp_path)

    def prepare(repo):
        if case == "fsmonitor":
            _g(repo, "config", "core.fsmonitor", str(hook))
        elif case == "filemode":
            _g(repo, "config", "core.fileMode", "false")

    _clean_control(tmp_path, "control", setup=prepare)
    repo = _new_repo(tmp_path / "repo")
    if case == "assume_unchanged":
        _g(repo, "update-index", "--assume-unchanged", "a.txt")
        _write_old(repo / "a.txt", b"TWO\n")
        measure = "index_bits_clear"
    elif case == "skip_worktree":
        _g(repo, "update-index", "--skip-worktree", "a.txt")
        _write_old(repo / "a.txt", b"TWO\n")
        measure = "index_bits_clear"
    elif case == "fsmonitor":
        prepare(repo)
        _write_old(repo / "a.txt", b"TWO\n")
        measure = "computed_tree_equal"
    elif case == "filemode":
        prepare(repo)
        os.chmod(repo / "a.txt", 0o755)
        measure = "computed_tree_equal"
    else:
        _detach_to(repo, _side_commit(repo, "detached"))
        measure = "detached_head_reachable"
    manifest = _git_manifest(repo, tmp_path)
    assert manifest["worktree_facts"][measure] is False, manifest["worktree_facts"]
    assert manifest["covers_worktree"] is False
    if marker is not None:
        assert not marker.exists(), "a repository-configured program ran in the archiver"


def test_reflog_only_commit_not_covered(tmp_path):
    """Catches objects the bundle leaves behind (Rev9c s1): main is moved to a
    new commit and back with update-ref, so that commit is reachable only
    from a reflog. `no_unbundled_objects` must be False while the worktree,
    refs and stash facts stay True. Mutant M57 (the fact dropped) covers it
    and fails here.

    Negative control: the same repository without the reflog-only commit
    covers.
    """
    _clean_control(tmp_path, "control")
    repo = _new_repo(tmp_path / "repo")
    main = _g(repo, "rev-parse", "main")
    _g(repo, "update-ref", "-m", "forward", "refs/heads/main", _side_commit(repo, "reflog only"))
    _g(repo, "update-ref", "-m", "back", "refs/heads/main", main)
    manifest = _git_manifest(repo, tmp_path)
    assert manifest["no_unbundled_objects"] is False
    assert manifest["worktree_clean"] is True and manifest["all_refs_included"] is True
    assert manifest["stash_empty"] is True and manifest["covers_worktree"] is False


def test_submodule_not_covered(tmp_path):
    """Catches a gitlink, whose content lives in another repository (Rev9c
    s2): one is added with `update-index --cacheinfo 160000,...` and
    committed (no network, no submodule add). `no_gitlinks` must be False.
    Mutant M58 (the fact dropped) leaves only the computed walk to object,
    and the test asserts the fact itself, so it fails here.

    Negative control: the same repository without the gitlink covers.
    """
    _clean_control(tmp_path, "control")
    repo = _new_repo(tmp_path / "repo")
    _g(repo, "update-index", "--add", "--cacheinfo",
       "160000,%s,sub" % _g(repo, "rev-parse", "main"))
    _g(repo, "commit", "-q", "-m", "gitlink")
    manifest = _git_manifest(repo, tmp_path)
    assert manifest["no_gitlinks"] is False and manifest["covers_worktree"] is False


def test_linked_worktree_not_covered(tmp_path):
    """Catches a second worktree, whose files the bundle does not hold (Rev9c
    s3): `git worktree add --detach` to a path under tmp_path makes
    `single_worktree` False (its HEAD is reachable, so no other fact moves).
    Mutant M59 (the fact dropped) covers it and fails here.

    Negative control: the same repository without the linked worktree covers.
    """
    _clean_control(tmp_path, "control")
    repo = _new_repo(tmp_path / "repo")
    _g(repo, "worktree", "add", "-q", "--detach", str(tmp_path / "wt2"))
    manifest = _git_manifest(repo, tmp_path)
    assert manifest["single_worktree"] is False
    assert manifest["worktree_facts"]["worktree_heads_reachable"] is True
    assert manifest["covers_worktree"] is False


def _second_device_under(path: Path) -> Path | None:
    """A mount point below `path`, if one exists (never for a fresh tmp_path
    without privileges)."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            mounts = [line.split()[4] for line in handle if len(line.split()) > 4]
    except OSError:
        return None
    base = os.path.realpath(path)
    for mount in mounts:
        if mount != base and mount.startswith(base + os.sep):
            return Path(mount)
    return None


_COMPUTED = ["checkstat", "ignorecase", "core_worktree", "assume_unchanged",
             "skip_worktree", "fsmonitor", "unreadable_dir", "fifo", "other_device",
             "other_device_seam"]


@pytest.mark.parametrize("case", _COMPUTED)
def test_worktree_fact_computed_not_configured(tmp_path, monkeypatch, case):
    """Catches a worktree fact that git's own view can be configured to miss
    (Rev9d): checkStat=minimal with trustctime=false and a same-size edit
    whose mtime is restored; ignorecase=true with an untracked case variant;
    assume-unchanged and skip-worktree with a content change; a silent
    fsmonitor hook with a content change; an unreadable directory holding a
    unique file (if euid is 0, the walk's error is injected through the
    module's `_walk` seam instead); a FIFO (never opened, the build returns);
    an entry on another device (a real second device under tmp_path, else
    NOT_RUN; and through the `_lstat` seam). Each must give
    `computed_tree_equal` False. A repository-local core.worktree must be
    refused. Mutants: M60a (the computed comparison replaced by git status
    alone) fails the checkstat, ignorecase, index-bit and fsmonitor cases;
    M60b (the top-level/core.worktree check removed) fails core_worktree;
    M60c (walk errors ignored) fails unreadable_dir.

    Negative control: the same preparation without the condition reaches
    `computed_tree_equal` True and covers the worktree.
    """
    hook = marker = None
    if case == "fsmonitor":
        hook, marker = _fsmonitor_hook(tmp_path)
    if case == "other_device":
        mount = _second_device_under(tmp_path)
        if mount is None:
            pytest.skip("NOT_RUN: no second device under tmp_path")

    def prepare(repo):
        if case == "checkstat":
            _g(repo, "config", "core.checkStat", "minimal")
            _g(repo, "config", "core.trustctime", "false")
        elif case == "ignorecase":
            _g(repo, "config", "core.ignorecase", "true")
        elif case == "fsmonitor":
            _g(repo, "config", "core.fsmonitor", str(hook))

    _clean_control(tmp_path, "control", setup=prepare)
    repo = _new_repo(tmp_path / "repo")
    prepare(repo)
    if case == "core_worktree":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        _write_old(elsewhere / "a.txt", b"two\n")
        _g(repo, "config", "core.worktree", str(elsewhere))
        out = _out(tmp_path, "gout-cw")
        with pytest.raises(ab.ArchiveRefused, match="core.worktree|top level"):
            _git_bundle(repo, tmp_path, out=out)
        assert not list(out.iterdir())
        return
    if case == "checkstat":
        st = os.stat(repo / "a.txt")
        (repo / "a.txt").write_bytes(b"TWO\n")                  # same size
        os.utime(repo / "a.txt", ns=(st.st_atime_ns, st.st_mtime_ns))
    elif case == "ignorecase":
        _write_old(repo / "A.TXT", b"case variant")
    elif case in ("assume_unchanged", "skip_worktree"):
        _g(repo, "update-index", "--" + case.replace("_", "-"), "a.txt")
        _write_old(repo / "a.txt", b"TWO\n")
    elif case == "fsmonitor":
        _write_old(repo / "a.txt", b"TWO\n")
    elif case == "unreadable_dir":
        locked = repo / "locked"
        locked.mkdir()
        _write_old(locked / "unique.bin", b"only here")
        if os.geteuid() == 0:
            real_walk = ab._walk

            def walk(top, onerror=None, **kwargs):
                for item in real_walk(top, onerror=onerror, **kwargs):
                    if os.fsencode(os.path.realpath(locked)) in item[1] or (
                            os.path.join(item[0], b"locked") == os.fsencode(
                                os.path.realpath(locked))):
                        item[1][:] = [d for d in item[1] if d != b"locked"]
                        onerror(PermissionError(13, "injected", os.fsencode(locked)))
                    yield item

            monkeypatch.setattr(ab, "_walk", walk)
        else:
            os.chmod(locked, 0)
    elif case == "fifo":
        os.mkfifo(repo / "pipe")
    elif case == "other_device":
        pytest.skip("NOT_RUN: a second device was found but is outside the repository")
    elif case == "other_device_seam":
        _write_old(repo / "mounted.bin", b"on another filesystem")
        wanted = os.fsencode(os.path.realpath(repo / "mounted.bin"))
        real_lstat = os.lstat

        def fake(p, *args, **kwargs):
            st = real_lstat(p, *args, **kwargs)
            if os.fsencode(p) == wanted:
                return _StatProxy(st, st_dev=st.st_dev + 1)
            return st

        monkeypatch.setattr(ab, "_lstat", fake)
    try:
        manifest = _git_manifest(repo, tmp_path)
    finally:
        if case == "unreadable_dir" and os.geteuid() != 0:
            os.chmod(repo / "locked", 0o755)
    assert manifest["worktree_facts"]["computed_tree_equal"] is False, manifest["worktree_facts"]
    assert manifest["worktree_clean"] is False and manifest["covers_worktree"] is False
    if marker is not None:
        assert not marker.exists(), "a repository-configured program ran in the archiver"


def test_mutant_m38(tmp_path):
    """Catches an ignored file taken as covered (M38; since the status fact
    was withdrawn (Rev10a s1), the mutant is the walk skipping what
    .gitignore lists): a repository whose committed .gitignore lists a
    unique file that is present. `worktree_facts.computed_tree_equal` must
    be False -- the walk reads ignored files like any other -- and
    covers_worktree False.

    Negative control: the same repository without the ignored file covers.
    """
    _clean_control(tmp_path, "control")
    repo = _new_repo(tmp_path / "repo", ignore=b"secret.txt\n")
    assert _covered(_git_manifest(repo, tmp_path))
    _write_old(repo / "secret.txt", b"unique, ignored")
    manifest = _git_manifest(repo, tmp_path)
    assert manifest["covers_worktree"] is False
    assert manifest["worktree_facts"]["computed_tree_equal"] is False


# --------------------------------------------------------------------------- #
# K13 / K14 -- recorded refs in both passes, no converting or program-running
# git command, the closed command list, partial clones, timeouts
# (Rev10, Rev10a, Rev10b)
# --------------------------------------------------------------------------- #

def test_commit_between_recording_and_first_facts_refused(tmp_path, monkeypatch):
    """Catches a ref that moved before the first fact pass (Rev10 s1,
    sarch3-a B2): through the `_after_record_hook` seam a commit lands on the
    recorded branch main after its id was recorded and before the facts are
    first taken. Both fact passes then agree with each other, so only the
    requirement that every 'refs as recorded' entry is True in each pass can
    see it. The build must raise SourceChanged, leave the staging directory
    INCOMPLETE and publish nothing. Mutant (the requirement removed, only the
    before/after equality kept) publishes a bundle whose manifest records the
    old commit for a payload holding the new one, and fails here.

    Negative control: the same hook doing nothing builds a covering bundle.
    """
    repo = _new_repo(tmp_path / "repo")
    calls = []

    def commit(**kwargs):
        calls.append(kwargs)
        _g(repo, "commit", "-q", "--allow-empty", "-m", "between recording and facts")

    monkeypatch.setattr(ab, "_after_record_hook", commit)
    out = _out(tmp_path, "between-out")
    with pytest.raises(ab.SourceChanged, match="refs as recorded") as caught:
        ab.ArchiveBundle.build_git(repo, ["main", "v1"], out, label="between", meta={},
                                   ownership=_owned(tmp_path))
    assert len(calls) == 1 and [r["ref"] for r in calls[0]["recorded"]] == ["main", "v1"]
    staging = caught.value.bundle_dir
    assert staging is not None and staging.parent == out and staging.name.endswith(".building")
    assert (staging / ab.INCOMPLETE).is_file()
    assert "main" in (staging / ab.INCOMPLETE).read_text(encoding="utf-8")
    assert not [p for p in out.iterdir() if _NAME.fullmatch(p.name)]

    control = _new_repo(tmp_path / "control")
    calls.clear()
    monkeypatch.setattr(ab, "_after_record_hook", lambda **kwargs: calls.append(kwargs))
    assert _covered(_git_manifest(control, tmp_path)) and len(calls) == 1


def _marker_program(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """An executable outside every repository that appends a line to a marker
    file each time it runs and copies stdin to stdout (a filter that changes
    nothing). The marker file does not exist until it has run."""
    script, marker = tmp_path / (name + ".sh"), tmp_path / (name + "-ran")
    with open(script, "x", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\necho \"ran $*\" >> '%s'\ncat\n" % marker)
    os.chmod(script, 0o755)
    return script, marker


def _filter_config(script: Path) -> str:
    return "[filter \"x\"]\n\tprocess = %s\n\tclean = %s\n" % (script, script)


def _stat_dirty(path: Path) -> None:
    """Give a tracked file a new mtime (content unchanged), so a stat-based
    comparison has to re-read it -- through any clean filter."""
    when = _OLD_NS + next(_OUT) * 1_000_000_000
    os.utime(path, ns=(when, when))


def _write_new_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as handle:
        handle.write(text)


_FILTER_ROUTES = ["git_config", "include", "include_if", "submodule",
                  "include_index_attributes", "include_attr_tree"]


@pytest.mark.parametrize("route", _FILTER_ROUTES)
def test_filter_process_never_executed(tmp_path, route):
    """Catches a git command in the archiver that applies a
    repository-configured filter (Rev10 s2, Rev10a, sarch3-a B1): a
    `filter.x.process` and `filter.x.clean` writing to a marker file,
    configured in .git/config, in an [include] file, in an [includeIf
    "gitdir:..."] file, or in a submodule's own config with its
    info/attributes; and through an include with the attributes staged only
    in the index, or named by `attr.tree`. Each has `* filter=x` attributes
    and a stat-dirty tracked file, so `git status` would run the filter. The
    build must leave the marker absent and covers_worktree False. Mutant
    (the `status` fact command restored) runs the filter and fails here.

    Positive control of the trap itself: after the build, the test runs
    `git status --ignore-submodules=none` on the same repository, and the
    marker appears -- so the build ran no such command.
    """
    script, marker = _marker_program(tmp_path, "filter")
    repo = _new_repo(tmp_path / "repo")
    git_dir = repo / ".git"
    inc = tmp_path / "included.cfg"
    dirty = repo / "a.txt"
    if route != "git_config" and route != "submodule":
        _write_new_text(inc, _filter_config(script))
    if route == "git_config":
        _g(repo, "config", "filter.x.process", str(script))
        _g(repo, "config", "filter.x.clean", str(script))
        _write_new_text(git_dir / "info" / "attributes", "* filter=x\n")
    elif route == "include":
        _g(repo, "config", "include.path", str(inc))
        _write_new_text(git_dir / "info" / "attributes", "* filter=x\n")
    elif route == "include_if":
        _g(repo, "config", "includeIf.gitdir:%s.path" % os.path.realpath(git_dir), str(inc))
        _write_new_text(git_dir / "info" / "attributes", "* filter=x\n")
    elif route == "submodule":
        sub = _new_repo(repo / "sub")
        _write_new_text(repo / ".gitmodules", "[submodule \"sub\"]\n\tpath = sub\n"
                        "\turl = ./sub\n")
        _g(repo, "add", ".gitmodules")
        _g(repo, "update-index", "--add", "--cacheinfo",
           "160000,%s,sub" % _g(sub, "rev-parse", "HEAD"))
        _g(repo, "commit", "-q", "-m", "submodule")
        _g(sub, "config", "filter.x.process", str(script))
        _g(sub, "config", "filter.x.clean", str(script))
        _write_new_text(sub / ".git" / "info" / "attributes", "* filter=x\n")
        dirty = sub / "a.txt"
    elif route == "include_index_attributes":
        blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                              input="* filter=x\n", env=_tenv(), capture_output=True,
                              text=True, check=True).stdout.strip()
        _g(repo, "update-index", "--add", "--cacheinfo", "100644,%s,.gitattributes" % blob)
        _g(repo, "config", "include.path", str(inc))
        assert not (repo / ".gitattributes").exists()
    else:
        blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                              input="* filter=x\n", env=_tenv(), capture_output=True,
                              text=True, check=True).stdout.strip()
        tree = subprocess.run(["git", "-C", str(repo), "mktree"],
                              input="100644 blob %s\t.gitattributes\n" % blob, env=_tenv(),
                              capture_output=True, text=True, check=True).stdout.strip()
        _g(repo, "config", "include.path", str(inc))
        _g(repo, "config", "attr.tree", tree)
    _stat_dirty(dirty)
    assert not marker.exists()

    manifest = _git_manifest(repo, tmp_path, ["main", "v1"] if route != "submodule"
                             else ["main"])
    assert not marker.exists(), "a repository-configured filter ran inside the archiver"
    assert manifest["covers_worktree"] is False
    if route != "submodule":
        assert manifest["worktree_facts"]["no_conversion"] is False
    else:
        assert manifest["no_gitlinks"] is False

    subprocess.run(["git", "-C", str(repo), "status", "--porcelain",
                    "--ignore-submodules=none"], env=_tenv(), capture_output=True,
                   check=False, timeout=60)
    assert marker.exists(), "the trap is not armed: git status did not run the filter"


def test_repo_given_as_subdirectory_refused(tmp_path):
    """Catches a repository named by a subdirectory of its worktree (Rev9d
    s2, Rev10 s3): the archiver would walk only that subdirectory and compare
    it with the whole of HEAD. The build must be refused (`ArchiveRefused`,
    'top level') with nothing written to out_parent. Mutant M60b1 (the
    `--show-toplevel` check removed) builds a bundle and fails here.

    Negative control: the same repository given by its top level builds a
    covering bundle.
    """
    repo = _new_repo(tmp_path / "repo")
    (repo / "sub").mkdir()
    _write_old(repo / "sub" / "b.txt", b"below\n")
    _g(repo, "add", "sub/b.txt")
    _g(repo, "commit", "-q", "-m", "sub")
    _g(repo, "tag", "-f", "v1")
    out = _out(tmp_path, "gout-sub")
    with pytest.raises(ab.ArchiveRefused, match="top level"):
        ab.ArchiveBundle.build_git(repo / "sub", ["main", "v1"], out, label="g", meta={},
                                   ownership=_owned(tmp_path))
    assert not list(out.iterdir())
    assert _covered(_git_manifest(repo, tmp_path))


def test_mutant_empty_directory(tmp_path):
    """Catches an empty directory taken as covered (Rev9d s8; the rule was
    claimed shadowed in sarch3-a minor 2, which is wrong): git cannot hold an
    empty directory, and it is invisible to both sides of the comparison
    with `ls-tree -r`, so only the walk's empty-directory rule makes
    `computed_tree_equal` False. Mutant (the rule removed) covers it and
    fails here.

    Negative control: the same repository without the empty directory
    covers.
    """
    _clean_control(tmp_path, "control")
    repo = _new_repo(tmp_path / "repo")
    (repo / "empty").mkdir()
    manifest = _git_manifest(repo, tmp_path)
    # no_conversion fails closed with it: an incomplete walk yields no
    # attribute list to judge.
    assert manifest["worktree_facts"]["computed_tree_equal"] is False
    assert manifest["worktree_clean"] is False and manifest["covers_worktree"] is False


#: The closed list of git commands (Rev10b s3): the subcommand, and for the
#: commands in `_TWO_WORD` its first argument as well.
_CLOSED_LIST = {("rev-parse",), ("symbolic-ref",), ("for-each-ref",), ("rev-list",),
                ("reflog", "show"), ("ls-tree",), ("ls-files",), ("worktree", "list"),
                ("config", "--list"), ("bundle", "create"),
                ("init", "--bare"), ("bundle", "verify"), ("fetch",), ("fsck",)}
_TWO_WORD = {"reflog", "worktree", "config", "bundle", "init"}
#: The module's git runners and how many leading positional arguments are
#: not part of git's argument list (the working directory).
_RUNNERS = {"_run_git": 0, "_git_run": 1, "_git": 1}
#: Runners that may hand their own ``*args`` through to the next runner.
_PASS_THROUGH = {"_git_run", "_git"}
#: Names whose ``*name`` expansion stands for global ``-c`` options.
_FLAG_TUPLES = {"_FACT_FLAGS", "_QUIET"}


def _call_name(func: ast.AST) -> str:
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _scan_git_calls(source: str) -> tuple[set, list]:
    """Every argument tuple handed to a git runner in `source`, parsed:
    ``-C <x>``, ``-c <x>`` pairs, other leading global options and `*` of
    the flag tuples skipped; then the subcommand (plus its second word for
    `_TWO_WORD`) must be a literal on `_CLOSED_LIST`. Returns the commands
    seen and the problems (a non-literal or unlisted subcommand, or a
    subprocess call outside `_run_git`)."""
    seen: set = set()
    problems: list = []
    calls: list = []

    class Visitor(ast.NodeVisitor):
        """Collects every call with the name of the innermost function
        around it."""

        def __init__(self):
            self.where = ["<module>"]

        def visit_FunctionDef(self, node):
            self.where.append(node.name)
            self.generic_visit(node)
            self.where.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            calls.append((node, self.where[-1]))
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    for node, where in calls:
        name = _call_name(node.func)
        if isinstance(node.func, ast.Attribute) and getattr(node.func.value, "id", "") \
                == "subprocess" and where != "_run_git":
            problems.append((node.lineno, "subprocess call outside _run_git"))
            continue
        if name not in _RUNNERS:
            continue
        args = list(node.args)[_RUNNERS[name]:]
        while args:
            first = args[0]
            if isinstance(first, ast.Starred) and getattr(first.value, "id", "") in _FLAG_TUPLES:
                args = args[1:]
            elif isinstance(first, ast.Constant) and isinstance(first.value, str) \
                    and first.value.startswith("-"):
                args = args[2:] if first.value in ("-C", "-c") else args[1:]
            else:
                break
        if (where in _PASS_THROUGH and len(args) == 1 and isinstance(args[0], ast.Starred)
                and getattr(args[0].value, "id", "") == "args"):
            continue
        words = []
        for arg in args[:2]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                words.append(arg.value)
            else:
                words.append(None)
        if not words or words[0] is None:
            problems.append((node.lineno, "non-literal git subcommand"))
            continue
        command = tuple(words[:2]) if words[0] in _TWO_WORD else (words[0],)
        if None in command or command not in _CLOSED_LIST:
            problems.append((node.lineno, "git command not on the closed list: "
                             + repr(command)))
            continue
        seen.add(command)
    return seen, problems


def test_git_subcommands_closed_list(tmp_path):
    """Catches a git command outside the closed list (Rev10b s3, s7): every
    argument tuple handed to the module's git runners (`_run_git`,
    `_git_run`, `_git`) is parsed from the AST -- not searched as text -- so
    a subcommand built from a variable fails, and so does any subprocess
    call that bypasses `_run_git`. The commands seen must be exactly the
    closed list. Mutant (a `stash list` call added) fails here.

    Negative controls: the scanner itself rejects an injected
    `_run_git("-C", x, "stash", "list")`, a `-c`-prefixed `status`, a
    non-literal subcommand and a bare `subprocess.run`, and accepts a listed
    command.
    """
    seen, problems = _scan_git_calls(MODULE.read_text(encoding="utf-8"))
    assert not problems, problems
    assert seen == _CLOSED_LIST, sorted(seen ^ _CLOSED_LIST)

    def scan(snippet: str):
        return _scan_git_calls("def probe(x, sub):\n    " + snippet + "\n")[1]

    assert scan('_run_git("-C", x, "stash", "list")')
    assert scan('_run_git("-C", x, "-c", "a.b=c", "status", "--porcelain")')
    assert scan('_git_run(x, sub)')
    assert scan('_git(x, "bundle", sub)')
    assert scan('_git(x, "config", "--get", "a.b")')
    assert scan('subprocess.run(["git", "status"])')
    assert not scan('_git(x, "reflog", "show", "--format=%H", "refs/stash")')
    assert not scan('_run_git("-C", x, *_FACT_FLAGS, "fetch", "--quiet", x)')


def _signed_stash(repo: Path) -> str:
    """refs/stash, with a reflog, pointing at a commit that carries a
    `gpgsig` header (written with hash-object and update-ref; no gpg)."""
    tree = _g(repo, "rev-parse", "HEAD^{tree}")
    head = _g(repo, "rev-parse", "HEAD")
    body = ("tree %s\nparent %s\nauthor Archive Test <a@example.invalid> 1700000000 +0000\n"
            "committer Archive Test <a@example.invalid> 1700000000 +0000\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEEAAAA\n"
            " -----END PGP SIGNATURE-----\n\nWIP on main: signed\n" % (tree, head))
    oid = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit", "-w",
                          "--stdin"], input=body, env=_tenv(), capture_output=True,
                         text=True, check=True).stdout.strip()
    _g(repo, "update-ref", "--create-reflog", "-m", "WIP on main: signed", "refs/stash", oid)
    return oid


@pytest.mark.parametrize("route", ["git_config", "include"])
def test_gpg_program_never_executed(tmp_path, route):
    """Catches a fact command that verifies signatures through a
    repository-configured `gpg.program` (Rev10b s1, s4): a signed stash
    entry, `log.showSignature=true` and a marker `gpg.program`, in
    .git/config or in an included file. The build must leave the marker
    absent (and stash_empty False). Mutant (plain `stash list` restored --
    without `-c log.showSignature=false`, as on the third candidate) runs the
    marker program and fails here; so does `-c log.showSignature=false`
    dropped while `reflog show` stays.

    Positive control of the trap: after the build, `git stash list` run by
    the test on the same repository makes the marker appear.
    """
    script, marker = _marker_program(tmp_path, "gpg")
    repo = _new_repo(tmp_path / "repo")
    _signed_stash(repo)
    settings = "[log]\n\tshowSignature = true\n[gpg]\n\tprogram = %s\n" % script
    if route == "git_config":
        _g(repo, "config", "log.showSignature", "true")
        _g(repo, "config", "gpg.program", str(script))
    else:
        inc = tmp_path / "included.cfg"
        _write_new_text(inc, settings)
        _g(repo, "config", "include.path", str(inc))
    manifest = _git_manifest(repo, tmp_path)
    assert not marker.exists(), "gpg.program ran inside the archiver"
    assert manifest["stash_empty"] is False and manifest["covers_worktree"] is False
    assert manifest["worktree_facts"]["stash_reflog_empty"] is False

    subprocess.run(["git", "-C", str(repo), "stash", "list"], env=_tenv(),
                   capture_output=True, check=False, timeout=60)
    assert marker.exists(), "the trap is not armed: git stash list did not run gpg"


def test_partial_clone_refused_before_walk(tmp_path):
    """Catches a partial clone walked by the archiver (Rev10b s5): a local
    `--filter=blob:none` clone (file:// URL, uploadpack.allowFilter on the
    source, no network) whose remote.origin.uploadpack is a marker program.
    Walking it would fetch the missing blobs lazily through that program,
    and a bundle of it could not be complete. The build must raise
    ArchiveRefused naming the partial clone before any object walk: the
    marker stays absent and out_parent empty. Mutant (the refusal removed)
    fails here.

    Positive control of the trap: an ordinary local clone of the same source
    builds; and a test-side `git rev-list --objects --all` in the partial
    clone runs the marker program.
    """
    source = _new_repo(tmp_path / "source")
    _g(source, "config", "uploadpack.allowFilter", "true")
    clone = tmp_path / "partial"
    subprocess.run(["git", "clone", "-q", "--filter=blob:none", "file://" + str(source),
                    str(clone)], env=_tenv(), check=True, capture_output=True)
    script = tmp_path / "uploadpack.sh"
    marker = tmp_path / "uploadpack-ran"
    with open(script, "x", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\necho ran >> '%s'\nexec git-upload-pack \"$@\"\n" % marker)
    os.chmod(script, 0o755)
    _g(clone, "config", "remote.origin.uploadpack", str(script))
    assert _g(clone, "config", "remote.origin.promisor") == "true"
    out = _out(tmp_path, "gout-partial")
    with pytest.raises(ab.ArchiveRefused, match="partial clone"):
        ab.ArchiveBundle.build_git(clone, ["main"], out, label="g", meta={},
                                   ownership=_owned(tmp_path))
    assert not marker.exists(), "the archiver fetched from the promisor remote"
    assert not list(out.iterdir())

    full = tmp_path / "full"
    subprocess.run(["git", "clone", "-q", "file://" + str(source), str(full)], env=_tenv(),
                   check=True, capture_output=True)
    assert _git_bundle(full, tmp_path, ["main"]).is_dir()
    subprocess.run(["git", "-C", str(clone), "rev-list", "--objects", "--all"],
                   env=_tenv(), capture_output=True, check=False, timeout=60)
    assert marker.exists(), "the trap is not armed: no lazy fetch in the partial clone"


def _group_alive(pgid: int) -> list[int]:
    """Live (not zombie) processes whose process group is `pgid`, from /proc."""
    alive = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % entry, encoding="ascii", errors="replace") as handle:
                data = handle.read()
        except OSError:
            continue
        fields = data[data.rindex(")") + 2:].split()
        if len(fields) > 2 and int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
            alive.append(int(entry))
    return alive


def _survivors(pidfile: Path, settle: float = 3.0) -> dict[int, list[int]]:
    """For every process group a wrapped git started (its pid, written by the
    wrapper, is the group id), the members still alive after `settle` s."""
    pgids = [int(line) for line in pidfile.read_text(encoding="ascii").split()]
    deadline = time.monotonic() + settle
    while True:
        left = {pgid: _group_alive(pgid) for pgid in pgids}
        left = {pgid: pids for pgid, pids in left.items() if pids}
        if not left or time.monotonic() > deadline:
            return left
        time.sleep(0.1)


def _end_group(pgid: int) -> None:
    """Test hygiene for a mutant run: SIGKILL a surviving group the test made."""
    try:
        os.killpg(pgid, 9)
    except OSError:
        pass


def _git_wrapper(tmp_path: Path, monkeypatch, body: str) -> Path:
    """Put a `git` first on PATH that records its pid (its process group id:
    the module starts git in a new session), runs `body`, then execs the
    real git. Returns the pid file."""
    real = shutil.which("git")
    assert real
    bindir = tmp_path / "wrapped-bin"
    bindir.mkdir()
    pidfile = tmp_path / "git-pgids"
    with open(bindir / "git", "x", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\necho $$ >> '%s'\n%s\nexec '%s' \"$@\"\n"
                     % (pidfile, body, real))
    os.chmod(bindir / "git", 0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return pidfile


def test_git_timeout_kills_process_group(tmp_path, monkeypatch):
    """Catches a git call that hangs the archiver, or a timeout that leaves
    helpers behind (Rev10b s6 and its confirmation note): `include.path`
    names a FIFO, which blocks every git that reads the configuration, and
    the git on PATH is a wrapper that first starts a helper (`sleep`) in its
    own process group. With an injected timeout of 3 s the build must end in
    ArchiveRefused well within a bounded wall time, with nothing in
    out_parent, and no process of any group the module started may survive.
    Mutants: the timeout not passed (git blocks until the test's own
    unblocker opens the FIFO after 25 s -- too late -- and the build goes on)
    and only the direct child killed (the helper survives) each fail here.

    Negative control: the same wrapper without the FIFO builds within the
    same timeout (the helpers of calls that end normally are not the
    module's to kill; the test ends them).
    """
    repo = _new_repo(tmp_path / "repo")
    control = _new_repo(tmp_path / "control")
    fifo = tmp_path / "config-fifo"
    os.mkfifo(fifo)
    _g(repo, "config", "include.path", str(fifo))     # git config set reads no include
    # The test's own git calls end here: the wrapper below is for the module's.
    pidfile = _git_wrapper(tmp_path, monkeypatch,
                           "sleep 120 </dev/null >/dev/null 2>&1 &")
    monkeypatch.setattr(ab, "_git_timeout", 3.0)

    assert _git_bundle(control, tmp_path).is_dir()
    left = _survivors(pidfile, settle=0.0)
    for pgid in left:
        _end_group(pgid)          # helpers of calls that ended normally are not killed
    pidfile.rename(tmp_path / "git-pgids.control")
    done = threading.Event()

    def unblock():
        """From 25 s on, keep opening the FIFO for writing (non-blocking) --
        every git that reads the configuration blocks on it anew -- so a
        mutant without a timeout ends instead of hanging the suite."""
        if done.wait(25):
            return
        end = time.monotonic() + 240
        while time.monotonic() < end and not done.is_set():
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
            time.sleep(0.05)

    thread = threading.Thread(target=unblock, daemon=True)
    thread.start()
    out = _out(tmp_path, "gout-fifo")
    started = time.monotonic()
    try:
        with pytest.raises(ab.ArchiveRefused, match="ran longer than"):
            ab.ArchiveBundle.build_git(repo, ["main", "v1"], out, label="g", meta={},
                                       ownership=_owned(tmp_path))
        elapsed = time.monotonic() - started
    finally:
        done.set()
    assert elapsed < 20, elapsed
    assert not list(out.iterdir())
    left = _survivors(pidfile)
    for pgid in left:
        _end_group(pgid)
    assert not left, "processes of a timed-out git group survived: %r" % left


# --------------------------------------------------------------------------- #
# STORAGE-D Revision 8 item 3 -- a build can be cancelled
# --------------------------------------------------------------------------- #

def test_build_tree_cancel_stops_writing(tmp_path, monkeypatch):
    """Catches `cancel` not consulted (STORAGE-D Revision 8 item 3): a
    ``cancel`` that turns true after three entries stops the builder -- the
    first three entries are written, no entry after the third is, and
    `BuildCancelled` is raised with the bundle directory left standing as
    ``INCOMPLETE`` (nothing removed). Mutant: `cancel` ignored -- all six
    entries are written and the bundle completes. Negative control: a
    ``cancel`` that stays false builds the same source completely, and
    ``cancel=None`` builds as before."""
    source = tmp_path / "src"
    (source / "d").mkdir(parents=True)
    for name in ("a.txt", "b.txt", "c.txt", "d/e.txt", "d/f.txt"):
        (source / name).write_text(name + "\n")
    written: list[str] = []
    real_add = ab.ArchiveBundle._add

    def recording_add(tar, item):
        written.append(os.fsdecode(item.raw))
        return real_add(tar, item)

    monkeypatch.setattr(ab.ArchiveBundle, "_add", staticmethod(recording_add))
    asked = []

    def cancel():
        asked.append(len(written))
        return len(written) >= 3

    out = _out(tmp_path, "cancelled")
    with pytest.raises(ab.BuildCancelled) as caught:
        ab.ArchiveBundle.build_tree(source, out, label="c", meta={},
                                    ownership=_owned(tmp_path), cancel=cancel)
    assert written == ["a.txt", "b.txt", "c.txt"]
    assert asked == [0, 1, 2, 3]
    [left] = list(out.iterdir())
    assert left.name.endswith(".building") and (left / ab.INCOMPLETE).is_file()
    assert caught.value.bundle_dir == left
    written.clear()
    bundle = ab.ArchiveBundle.build_tree(source, _out(tmp_path, "complete"), label="c",
                                         meta={}, ownership=_owned(tmp_path),
                                         cancel=lambda: False)
    assert written == ["a.txt", "b.txt", "c.txt", "d", "d/e.txt", "d/f.txt"]
    assert _manifest(bundle)["totals"]["entry_count"] == 6
    written.clear()
    plain = _build(source, tmp_path, label="plain", out="plain")
    assert len(written) == 6 and (plain / ab.TREE_PAYLOAD).is_file()
