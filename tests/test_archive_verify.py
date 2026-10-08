"""P5-26: verifying archive bundles only by re-reading them (`hoh.archive_bundle`).

A bundle counts as ARCHIVED_VERIFIED only after it has been read again,
decompressed completely, compared member for member and in order with its
manifest, extracted into an empty root it cannot escape, and every extracted
byte re-hashed. These tests damage bundles in every way the spec names. The
`resealed` cases recompute SHA256SUMS, the manifest's payload_sha256 and its
content digest after tampering, so only extraction and re-hashing are left to
catch them; the hand-made hostile payloads check that nothing outside
`extract_root` changes (a sentinel's bytes and mode).

Every test names the failure it catches and the mutant that kills it, and
carries a negative control that changes only the condition under test. Tests
work only inside `tmp_path`; git runs with an explicit identity.
"""

from __future__ import annotations

import hashlib
import inspect
import io
import itertools
import json
import os
import shutil
import stat
import subprocess
import tarfile
import time
from pathlib import Path

import pytest
import zstandard

import hoh.archive_bundle as ab

VERIFIED = ab.ARCHIVED_VERIFIED
UNVERIFIED = ab.ARCHIVE_UNVERIFIED
_COUNTER = itertools.count()

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Archive Test",
    "GIT_AUTHOR_EMAIL": "archive-test@example.invalid",
    "GIT_COMMITTER_NAME": "Archive Test",
    "GIT_COMMITTER_EMAIL": "archive-test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _owned(root: Path) -> dict:
    return {"status": "OWNED", "root": str(root)}


def _build(source: Path, tmp_path: Path, *, label: str = "t", **kwargs) -> Path:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    kwargs.setdefault("ownership", _owned(tmp_path))
    return ab.ArchiveBundle.build_tree(source, out, label=label, meta={"run_id": "r"},
                                       **kwargs)


def _fresh(tmp_path: Path, stem: str = "x") -> Path:
    """A new, empty directory with a name never used before in this test."""
    path = tmp_path / ("%s-%d" % (stem, next(_COUNTER)))
    path.mkdir(parents=True)
    return path


def _verify(bundle: Path, tmp_path: Path) -> tuple[ab.VerificationReport, Path]:
    root = _fresh(tmp_path, "extract")
    return ab.verify_archive(bundle, extract_root=root), root


def _failed(report: ab.VerificationReport) -> set[str]:
    return {c["id"] for c in report.failed()}


def _check(report: ab.VerificationReport, check_id: str) -> dict:
    found = report.check(check_id)
    assert found is not None, (check_id, [c["id"] for c in report.checks])
    return found


def _manifest(bundle: Path) -> dict:
    return json.loads((bundle / ab.MANIFEST).read_text(encoding="ascii"))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _members(bundle: Path) -> list[tuple[tarfile.TarInfo, bytes | None]]:
    raw = zstandard.ZstdDecompressor().stream_reader(
        io.BytesIO((bundle / ab.TREE_PAYLOAD).read_bytes())).read()
    out = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:", encoding="utf-8",
                      errors="surrogateescape") as tar:
        for member in tar.getmembers():
            data = tar.extractfile(member).read() if member.isreg() else None
            out.append((member, data))
    return out


def _payload(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    plain = io.BytesIO()
    with tarfile.open(fileobj=plain, mode="w", format=tarfile.PAX_FORMAT,
                      encoding="utf-8", errors="surrogateescape") as tar:
        for info, data in members:
            if data is not None:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)
    return zstandard.ZstdCompressor(write_checksum=True).compress(plain.getvalue())


def _seal(target: Path, payload: bytes, manifest: dict) -> Path:
    """Write a complete new bundle directory: payload, manifest with a
    matching payload_sha256, and SHA256SUMS over both."""
    target.mkdir(parents=True)
    manifest = dict(manifest, payload_sha256=_sha(payload))
    body = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("ascii")
    (target / ab.TREE_PAYLOAD).write_bytes(payload)
    (target / ab.MANIFEST).write_bytes(body)
    (target / ab.SUMS).write_text(
        f"{_sha(payload)}  {ab.TREE_PAYLOAD}\n{_sha(body)}  {ab.MANIFEST}\n", encoding="ascii")
    return target


def _reseal(bundle: Path, tmp_path: Path, *, members=None, manifest=None,
            payload=None) -> Path:
    """A copy of `bundle` whose tar members and/or manifest were changed, with
    payload_sha256, SHA256SUMS and the content digest recomputed afterwards."""
    listed = _members(bundle)
    if members is not None:
        listed = members(listed)
    data = _payload(listed) if payload is None else payload(
        (bundle / ab.TREE_PAYLOAD).read_bytes())
    record = _manifest(bundle)
    if manifest is not None:
        manifest(record)
    record["content_digest"] = ab._content_digest(record["entries"])
    return _seal(_fresh(tmp_path, "resealed") / bundle.name, data, record)


HAND_ID = "vh-arch-20261001T000000Z-000000000000-hand"


def _hand_made(tmp_path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]],
               entries: list[dict] | None = None, externals: list[dict] | None = None,
               ) -> Path:
    """A bundle whose payload is exactly `members`, in the given order, with a
    manifest that describes those members truthfully (unless `entries`). Its
    directory is named after its bundle_id, so the `bundle_name` check passes
    and a VERIFIED control stays verified."""
    if entries is None:
        entries = []
        for info, data in members:
            kind = {tarfile.REGTYPE: "file", tarfile.DIRTYPE: "dir",
                    tarfile.SYMTYPE: "symlink", tarfile.LNKTYPE: "hardlink"}[info.type]
            entries.append({
                "path": info.name, "type": kind, "mode": info.mode, "mtime_ns": 0,
                "size": len(data) if data is not None else (
                    len(info.linkname.encode()) if info.linkname else 0),
                "sha256": _sha(data) if data is not None else None,
                "link_target": info.linkname or None, "hardlink_group": None})
    files = [e for e in entries if e["type"] == "file"]
    manifest = {
        "schema": ab.SCHEMA, "payload_kind": "tree", "payload": ab.TREE_PAYLOAD,
        "bundle_id": HAND_ID, "entries": entries,
        "externals": externals or [],
        "totals": {"files": len(files),
                   "dirs": sum(e["type"] == "dir" for e in entries),
                   "symlinks": sum(e["type"] == "symlink" for e in entries),
                   "external": len(externals or []),
                   "logical_bytes": sum(e["size"] for e in files),
                   "entry_count": len(entries)},
        "content_digest": ab._content_digest(entries),
    }
    return _seal(_fresh(tmp_path, "hand") / HAND_ID, _payload(members), manifest)


def _info(name: str, kind: bytes = tarfile.REGTYPE, *, mode: int = 0o644,
          link: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.mode, info.linkname = kind, mode, link
    return info


def _standard_source(tmp_path: Path) -> Path:
    source = tmp_path / "src"
    (source / "d").mkdir(parents=True)
    (source / "d" / "a.txt").write_bytes(b"alpha")
    (source / "b.bin").write_bytes(bytes(range(256)) * 4)
    (source / "run.sh").write_bytes(b"#!/bin/sh\necho ok\n")
    os.chmod(source / "run.sh", 0o755)
    os.symlink("d/a.txt", source / "ln")
    return source


def _sentinel(tmp_path: Path) -> tuple[Path, bytes, int]:
    path = tmp_path / "sentinel"
    path.write_bytes(b"SENTINEL")
    os.chmod(path, 0o600)
    return path, b"SENTINEL", 0o600


def _sentinel_intact(path: Path, data: bytes, mode: int) -> None:
    st = os.lstat(path)
    assert stat.S_ISREG(st.st_mode)
    assert path.read_bytes() == data
    assert stat.S_IMODE(st.st_mode) == mode
    assert st.st_nlink == 1


def _hostile_root(tmp_path: Path) -> Path:
    """extract_root two levels below tmp_path, so `../../sentinel` from it is
    the sentinel."""
    root = tmp_path / "victim" / "root"
    root.mkdir(parents=True)
    return root


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], env=GIT_ENV,
                          capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], env=GIT_ENV, check=True)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "second")
    _git(repo, "tag", "v1")
    return repo


def _git_build(repo: Path, tmp_path: Path, refs: list[str], label: str = "g") -> Path:
    out = tmp_path / "gout"
    out.mkdir(exist_ok=True)
    return ab.ArchiveBundle.build_git(repo, refs, out, label=label, meta={},
                                      ownership=_owned(tmp_path))


# --------------------------------------------------------------------------- #
# K1 / K2 -- round trips
# --------------------------------------------------------------------------- #

def test_symlink_only_parent_verified(tmp_path):
    """Catches the other half of 2026-09-28: archiving the cache's parent, which
    contains `blobs/`, must reach ARCHIVED_VERIFIED with logical_bytes equal to
    the blobs' size, and the links re-created as links with their exact text.
    Mutant M3 (links followed or materialised as content, or logical_bytes
    counting link sizes) fails here.

    Negative control: archiving `snapshots/` alone is refused (the link
    escapes it), so the result depends on the blobs being inside.
    """
    cache = tmp_path / "models--org--m"
    (cache / "blobs").mkdir(parents=True)
    (cache / "blobs" / "b1").write_bytes(b"w" * 7000)
    (cache / "blobs" / "b2").write_bytes(b"v" * 333)
    (cache / "snapshots" / "h1").mkdir(parents=True)
    os.symlink("../../blobs/b1", cache / "snapshots" / "h1" / "model.bin")
    os.symlink("../../blobs/b2", cache / "snapshots" / "h1" / "config.json")

    with pytest.raises(ab.ArchiveRefused):
        _build(cache / "snapshots", tmp_path, label="snap")

    bundle = _build(cache, tmp_path, label="cache")
    manifest = _manifest(bundle)
    assert manifest["totals"]["logical_bytes"] == 7000 + 333
    assert manifest["totals"]["symlinks"] == 2
    report, root = _verify(bundle, tmp_path)
    assert report.status == VERIFIED, report.failed()
    for name, text in (("model.bin", "../../blobs/b1"), ("config.json", "../../blobs/b2")):
        made = root / "snapshots" / "h1" / name
        assert made.is_symlink()
        assert os.readlink(made) == text
    assert (root / "blobs" / "b1").read_bytes() == b"w" * 7000


def test_roundtrip_every_file_rehashed(tmp_path, monkeypatch):
    """Catches a verifier that trusts the manifest instead of the bytes: the
    report must hold exactly one `file:` check per manifest file, and each
    SHA-256 must have been computed from the file under extract_root (spied on
    the module's one hashing helper). Mutant M4 (per-file check copies the
    manifest sha256) fails here.

    Negative control: the spy is checked against a path outside extract_root
    -- the bundle's own payload -- which must also have been hashed, so the
    spy demonstrably records real reads.
    """
    bundle = _build(_standard_source(tmp_path), tmp_path, label="rt")
    hashed: list[str] = []
    real_hash = ab._hash_path

    def spy(path):
        hashed.append(os.fsdecode(os.fspath(path)))
        return real_hash(path)

    monkeypatch.setattr(ab, "_hash_path", spy)
    report, root = _verify(bundle, tmp_path)
    assert report.status == VERIFIED, report.failed()
    assert report.extracted is True
    manifest = _manifest(bundle)
    file_checks = [c for c in report.checks if c["id"].startswith("file:")]
    assert len(file_checks) == manifest["totals"]["files"] == 3
    for entry in manifest["entries"]:
        if entry["type"] != "file":
            continue
        extracted = os.path.join(str(root), entry["path"])
        assert extracted in hashed, (extracted, hashed)
        check = _check(report, "file:" + entry["path"])
        assert check["ok"] and str(root) in check["detail"]
        assert entry["sha256"] in check["detail"]
    assert str(bundle / ab.TREE_PAYLOAD) in hashed
    assert "reduced" in report.page_cache_note


# --------------------------------------------------------------------------- #
# K3 -- damage
# --------------------------------------------------------------------------- #

def _identity_control(bundle: Path, tmp_path: Path) -> None:
    """The reseal helper itself yields a verifiable bundle when nothing is
    changed -- so a failure below is the tampering, not the helper."""
    report, _ = _verify(_reseal(bundle, tmp_path), tmp_path)
    assert report.status == VERIFIED, report.failed()


def _retouch(name: str, **changes):
    def apply(listed):
        out = []
        for info, data in listed:
            if info.name == name:
                info = info.replace(**{k: v for k, v in changes.items() if k != "data"})
                data = changes.get("data", data)
            out.append((info, data))
        return out
    return apply


def test_damaged_resealed_content(tmp_path):
    """Catches changed file content that keeps its size, with every sum and
    digest recomputed: only re-hashing the extracted file sees it. Mutant M5
    (no re-hash) fails here.

    Negative control: the identical reseal without the change verifies.
    """
    bundle = _build(_standard_source(tmp_path), tmp_path, label="content")
    _identity_control(bundle, tmp_path)
    damaged = _reseal(bundle, tmp_path, members=_retouch("d/a.txt", data=b"ALPHA"))
    report, _ = _verify(damaged, tmp_path)
    assert report.status == UNVERIFIED
    assert not _check(report, "file:d/a.txt")["ok"]
    assert _check(report, "member_list")["ok"]          # only the re-hash caught it
    assert _check(report, "sha256sums_payload")["ok"]


def test_damaged_resealed_extra_member(tmp_path):
    """Catches a tar member the manifest does not list. Mutant M6 (only checks
    that every manifest entry is among the members) fails here.

    Negative control: the identical reseal without the extra member verifies.
    """
    bundle = _build(_standard_source(tmp_path), tmp_path, label="extra")
    _identity_control(bundle, tmp_path)
    damaged = _reseal(bundle, tmp_path,
                      members=lambda listed: listed + [(_info("zz-extra"), b"planted")])
    report, _ = _verify(damaged, tmp_path)
    assert report.status == UNVERIFIED
    member_list = _check(report, "member_list")
    assert not member_list["ok"] and "extra member zz-extra" in member_list["detail"]


def test_damaged_resealed_missing_member(tmp_path):
    """Catches a manifest entry with no tar member. Mutant M7 (only checks that
    every member is in the manifest) fails here.

    Negative control: the identical reseal without dropping it verifies.
    """
    bundle = _build(_standard_source(tmp_path), tmp_path, label="missing")
    _identity_control(bundle, tmp_path)
    damaged = _reseal(bundle, tmp_path,
                      members=lambda listed: [m for m in listed if m[0].name != "run.sh"])
    report, _ = _verify(damaged, tmp_path)
    assert report.status == UNVERIFIED
    member_list = _check(report, "member_list")
    assert not member_list["ok"] and "missing member run.sh" in member_list["detail"]


def test_damaged_resealed_mode(tmp_path):
    """Catches a changed mode (the executable bit dropped from run.sh): both
    the member comparison and the extracted file's mode must fail. Mutant M8
    (mode not compared) fails here.

    Negative control: the identical reseal keeping the mode verifies.
    """
    bundle = _build(_standard_source(tmp_path), tmp_path, label="mode")
    _identity_control(bundle, tmp_path)
    damaged = _reseal(bundle, tmp_path, members=_retouch("run.sh", mode=0o644))
    report, _ = _verify(damaged, tmp_path)
    assert report.status == UNVERIFIED
    assert not _check(report, "member_list")["ok"]
    assert not _check(report, "file:run.sh")["ok"]


def test_damaged_resealed_link_target(tmp_path):
    """Catches a changed link text (still inside the root): both the member
    comparison and the re-read link must fail. Mutant M9 (link text not
    compared) fails here.

    Negative control: the identical reseal keeping the link verifies.
    """
    bundle = _build(_standard_source(tmp_path), tmp_path, label="link")
    _identity_control(bundle, tmp_path)
    damaged = _reseal(bundle, tmp_path, members=_retouch("ln", linkname="b.bin"))
    report, _ = _verify(damaged, tmp_path)
    assert report.status == UNVERIFIED
    assert not _check(report, "member_list")["ok"]
    assert not _check(report, "symlink:ln")["ok"]


def test_damaged_payload_byte(tmp_path):
    """Catches a flipped payload byte with SHA256SUMS and the manifest left as
    they were. Mutant M10 (SHA256SUMS not compared) fails here.

    Negative control: an unmodified copy of the payload verifies.
    """
    bundle = _build(_standard_source(tmp_path), tmp_path, label="byte")
    payload = (bundle / ab.TREE_PAYLOAD).read_bytes()

    def copy_with(data: bytes) -> Path:
        target = _fresh(tmp_path, "copy") / bundle.name
        target.mkdir()
        (target / ab.TREE_PAYLOAD).write_bytes(data)
        shutil.copy2(bundle / ab.MANIFEST, target / ab.MANIFEST)
        shutil.copy2(bundle / ab.SUMS, target / ab.SUMS)
        return target

    report, _ = _verify(copy_with(payload), tmp_path)
    assert report.status == VERIFIED, report.failed()

    flipped = bytearray(payload)
    flipped[len(flipped) // 2] ^= 0x01
    report, _ = _verify(copy_with(bytes(flipped)), tmp_path)
    assert report.status == UNVERIFIED
    assert not _check(report, "sha256sums_payload")["ok"]
    assert not _check(report, "manifest_payload_sha256")["ok"]


def test_damaged_truncated_payload(tmp_path):
    """Catches a truncated payload with every sum recomputed: the last three
    bytes (part of the frame checksum -- the frame never ends), and half of
    the payload. Only full decompression sees it. Mutant M11 (decompression
    errors or an unterminated frame tolerated) fails here.

    Negative control: the identical reseal without truncation verifies.
    """
    bundle = _build(_standard_source(tmp_path), tmp_path, label="trunc")
    _identity_control(bundle, tmp_path)
    for cut in (lambda data: data[:-3], lambda data: data[: len(data) // 2]):
        damaged = _reseal(bundle, tmp_path, payload=cut)
        report, _ = _verify(damaged, tmp_path)
        assert report.status == UNVERIFIED
        assert _check(report, "sha256sums_payload")["ok"]
        assert _check(report, "manifest_payload_sha256")["ok"]
        assert not _check(report, "zstd_stream")["ok"]


# --------------------------------------------------------------------------- #
# K4 -- hostile hand-made payloads
# --------------------------------------------------------------------------- #

def test_damaged_hardlink_outside(tmp_path):
    """Catches a LNKTYPE member pointing at a file outside extract_root: had it
    been extracted, its mode would be applied to the sentinel's inode. The
    member must be refused and the sentinel keep its bytes, mode and link
    count. Mutant M12 (LNKTYPE admitted) fails here.

    Negative control: the same payload with an ordinary file in place of the
    hardlink verifies.
    """
    sentinel, data, mode = _sentinel(tmp_path)
    good = _hand_made(tmp_path, [(_info("h", mode=0o777), b"plain")])
    report = ab.verify_archive(good, extract_root=_fresh(tmp_path, "ok"))
    assert report.status == VERIFIED, report.failed()

    hostile = _hand_made(tmp_path, [(_info("h", tarfile.LNKTYPE, mode=0o777,
                                           link="../../sentinel"), None)])
    report = ab.verify_archive(hostile, extract_root=_hostile_root(tmp_path))
    assert report.status == UNVERIFIED
    extract = _check(report, "extract")
    assert not extract["ok"] and "hardlink" in extract["detail"]
    _sentinel_intact(sentinel, data, mode)


def test_damaged_absolute_name(tmp_path):
    """Catches a member whose name is absolute -- here the sentinel's own path.
    It must be refused as absolute and the sentinel must keep its bytes and
    mode. Mutant M13 (absolute-name refusal removed) fails here.

    Negative control: the same member under a relative name verifies.
    """
    sentinel, data, mode = _sentinel(tmp_path)
    good = _hand_made(tmp_path, [(_info("sentinel", mode=0o777), b"EVIL")])
    report = ab.verify_archive(good, extract_root=_fresh(tmp_path, "ok"))
    assert report.status == VERIFIED, report.failed()

    hostile = _hand_made(tmp_path, [(_info(str(sentinel), mode=0o777), b"EVIL")])
    report = ab.verify_archive(hostile, extract_root=_hostile_root(tmp_path))
    assert report.status == UNVERIFIED
    extract = _check(report, "extract")
    assert not extract["ok"] and "absolute" in extract["detail"]
    _sentinel_intact(sentinel, data, mode)


def test_damaged_symlink_escape(tmp_path):
    """Catches writing through a symlink. Case A: `s -> ../..` (out of the
    root) followed by `s/sentinel` -- refused at the link for its text.
    Case B: `l -> d` (inside) followed by `l/f` -- refused because a path
    component is a symlink, so nothing is ever written through a link.
    Mutant M14 (linkname containment, or the symlink-component check,
    removed) fails here; the sentinel keeps its bytes and mode.

    Negative control: the same members with `s`/`l` as real directories
    verify.
    """
    sentinel, data, mode = _sentinel(tmp_path)
    good = _hand_made(tmp_path, [(_info("s", tarfile.DIRTYPE, mode=0o755), None),
                                 (_info("s/sentinel"), b"EVIL")])
    report = ab.verify_archive(good, extract_root=_fresh(tmp_path, "ok"))
    assert report.status == VERIFIED, report.failed()

    case_a = _hand_made(tmp_path, [(_info("s", tarfile.SYMTYPE, mode=0o777, link="../.."), None),
                                   (_info("s/sentinel", mode=0o777), b"EVIL")])
    report = ab.verify_archive(case_a, extract_root=_hostile_root(tmp_path))
    assert report.status == UNVERIFIED
    extract = _check(report, "extract")
    assert not extract["ok"]
    assert "'s'" in extract["detail"] and "leaves the archive root" in extract["detail"]
    _sentinel_intact(sentinel, data, mode)

    case_b = _hand_made(tmp_path, [(_info("d", tarfile.DIRTYPE, mode=0o755), None),
                                   (_info("l", tarfile.SYMTYPE, mode=0o777, link="d"), None),
                                   (_info("l/f"), b"through")])
    root_b = _fresh(tmp_path, "case-b")
    report = ab.verify_archive(case_b, extract_root=root_b)
    assert report.status == UNVERIFIED
    extract = _check(report, "extract")
    assert not extract["ok"] and "path component is a symlink" in extract["detail"]
    assert not (root_b / "d" / "f").exists()
    _sentinel_intact(sentinel, data, mode)


def test_damaged_resealed_link_chain_escape(tmp_path):
    """Catches a resealed chain `s/up -> ..`, `s/x -> up/..`: each text stays
    inside lexically, but `s/x` really resolves outside extract_root. The
    post-extraction realpath check must fail it. Mutant M15 (no
    post-extraction realpath check) fails here.

    Negative control: the same bundle before retouching (`s/x -> up`)
    verifies, and nothing outside extract_root changes.
    """
    sentinel, data, mode = _sentinel(tmp_path)
    source = tmp_path / "chain"
    (source / "s").mkdir(parents=True)
    os.symlink("..", source / "s" / "up")
    os.symlink("up", source / "s" / "x")
    bundle = _build(source, tmp_path, label="chain")
    _identity_control(bundle, tmp_path)

    def retext(record):
        for entry in record["entries"]:
            if entry["path"] == "s/x":
                entry["link_target"], entry["size"] = "up/..", 5

    damaged = _reseal(bundle, tmp_path, members=_retouch("s/x", linkname="up/.."),
                      manifest=retext)
    report = ab.verify_archive(damaged, extract_root=_hostile_root(tmp_path))
    assert report.status == UNVERIFIED
    assert _check(report, "member_list")["ok"] and _check(report, "extract")["ok"]
    assert not _check(report, "symlink_realpath:s/x")["ok"]
    assert _check(report, "symlink_realpath:s/up")["ok"]
    _sentinel_intact(sentinel, data, mode)


def test_damaged_resealed_dangling_link(tmp_path):
    """Catches a link-only archive whose links stay inside the root lexically:
    the model-cache parent bundle with `blobs/` dropped from the tar, the
    manifest entries and the totals (every digest resealed). The links
    `snapshots/h1/*.bin -> ../../blobs/*` are extracted but resolve to
    nothing, so `symlink_resolves:` must fail. Mutant: dangling links after
    extraction accepted (only containment checked) verifies it and fails here.

    Negative control: the identical reseal keeping `blobs/` verifies.
    """
    cache = tmp_path / "models--org--m"
    (cache / "blobs").mkdir(parents=True)
    (cache / "blobs" / "b1").write_bytes(b"w" * 700)
    (cache / "snapshots" / "h1").mkdir(parents=True)
    os.symlink("../../blobs/b1", cache / "snapshots" / "h1" / "model.bin")
    bundle = _build(cache, tmp_path, label="cache")
    _identity_control(bundle, tmp_path)

    def drop(record):
        gone = [e for e in record["entries"] if e["path"].startswith("blobs")]
        record["entries"] = [e for e in record["entries"] if e not in gone]
        totals = record["totals"]
        totals["dirs"] -= 1
        totals["files"] -= 1
        totals["logical_bytes"] = 0
        totals["entry_count"] -= 2

    damaged = _reseal(bundle, tmp_path, manifest=drop, members=lambda listed: [
        m for m in listed if not m[0].name.startswith("blobs")])
    report, _ = _verify(damaged, tmp_path)
    assert report.status == UNVERIFIED
    assert _failed(report) == {"symlink_resolves:snapshots/h1/model.bin"}


def test_damaged_duplicate_member(tmp_path):
    """Catches a name appearing twice in the tar -- the second copy would
    replace the first after it was checked. The member list must fail as a
    list, not a set. Mutant M16 (members compared as a set) fails here.

    Negative control: the same payload with the name once verifies.
    """
    sentinel, data, mode = _sentinel(tmp_path)
    good = _hand_made(tmp_path, [(_info("a"), b"one")])
    report = ab.verify_archive(good, extract_root=_fresh(tmp_path, "ok"))
    assert report.status == VERIFIED, report.failed()

    entries = json.loads((good / ab.MANIFEST).read_text(encoding="ascii"))["entries"]
    hostile = _hand_made(tmp_path, [(_info("a"), b"one"), (_info("a"), b"two")],
                         entries=entries)
    report = ab.verify_archive(hostile, extract_root=_hostile_root(tmp_path))
    assert report.status == UNVERIFIED
    member_list = _check(report, "member_list")
    assert not member_list["ok"] and "duplicate" in member_list["detail"]
    _sentinel_intact(sentinel, data, mode)


# --------------------------------------------------------------------------- #
# K6 -- git payloads, receipts, external content
# --------------------------------------------------------------------------- #

def test_git_bundle_tree_verified(tmp_path):
    """Catches a git archive judged by its exit status: verification runs
    `git bundle verify`, fetches from the bundle into a fresh `init --bare
    --template=` repository in extract_root and compares each recorded
    ref's commit and tree. A manifest whose recorded tree was edited and
    resealed must fail its ref check. Mutant M19 (tree not compared) fails
    here.

    Negative control: the unedited bundle verifies.
    """
    repo = _repo(tmp_path, "repo")
    bundle = _git_build(repo, tmp_path, ["main", "v1"])
    manifest = _manifest(bundle)
    assert manifest["payload_kind"] == "git"
    by_name = {r["name"]: r for r in manifest["refs"]}
    assert by_name["refs/heads/main"]["tree"] == _git(repo, "rev-parse", "main^{tree}")
    assert by_name["refs/tags/v1"]["commit"] == _git(repo, "rev-parse", "v1^{commit}")

    report, root = _verify(bundle, tmp_path)
    assert report.status == VERIFIED, report.failed()
    assert _check(report, "git_bundle_verify")["ok"]
    assert _check(report, "ref:refs/heads/main")["ok"]
    assert (root / "HEAD").exists() or (root / ".git").exists()

    edited = json.loads((bundle / ab.MANIFEST).read_text(encoding="ascii"))
    edited["refs"][0]["tree"] = _git(repo, "rev-parse", "main~1^{tree}")
    edited["content_digest"] = ab._git_digest(edited["refs"])
    payload = (bundle / ab.GIT_PAYLOAD).read_bytes()
    target = _fresh(tmp_path, "git-resealed") / bundle.name
    target.mkdir()
    body = (json.dumps(edited, indent=2, sort_keys=True) + "\n").encode("ascii")
    (target / ab.GIT_PAYLOAD).write_bytes(payload)
    (target / ab.MANIFEST).write_bytes(body)
    (target / ab.SUMS).write_text(
        f"{_sha(payload)}  {ab.GIT_PAYLOAD}\n{_sha(body)}  {ab.MANIFEST}\n", encoding="ascii")
    report, _ = _verify(target, tmp_path)
    assert report.status == UNVERIFIED
    assert _check(report, "sha256sums_manifest")["ok"]
    assert not _check(report, "ref:" + edited["refs"][0]["name"])["ok"]


def test_git_dirty_worktree_not_covered(tmp_path):
    """Catches a git bundle taken as a substitute for a worktree it does not
    cover: an untracked file, a stash, and a branch left out of the bundle --
    each alone -- give covers_worktree False (and the verifier reports it). A
    clean repo bundled with every branch and tag gives True. Mutant M20
    (covers_worktree always True) fails here.
    """
    clean = _repo(tmp_path, "clean")
    bundle = _git_build(clean, tmp_path, ["main", "v1"], label="clean")
    assert _manifest(bundle)["covers_worktree"] is True
    report, _ = _verify(bundle, tmp_path)
    assert report.status == VERIFIED and report.covers_worktree is True

    untracked = _repo(tmp_path, "untracked")
    (untracked / "new.txt").write_text("loose\n", encoding="utf-8")
    manifest = _manifest(_git_build(untracked, tmp_path, ["main", "v1"], label="untracked"))
    assert manifest["worktree_clean"] is False and manifest["covers_worktree"] is False

    stashed = _repo(tmp_path, "stashed")
    (stashed / "a.txt").write_text("three\n", encoding="utf-8")
    _git(stashed, "stash", "push", "-q", "-m", "archive-test")
    manifest = _manifest(_git_build(stashed, tmp_path, ["main", "v1"], label="stashed"))
    assert manifest["worktree_clean"] is True
    assert manifest["stash_empty"] is False and manifest["covers_worktree"] is False

    partial = _repo(tmp_path, "partial")
    _git(partial, "branch", "side")
    bundle = _git_build(partial, tmp_path, ["main", "v1"], label="partial")
    manifest = _manifest(bundle)
    assert manifest["all_refs_included"] is False and manifest["covers_worktree"] is False
    report, _ = _verify(bundle, tmp_path)
    assert report.status == VERIFIED          # a valid archive of its refs ...
    assert report.covers_worktree is False    # ... but not of the worktree
    assert any("not a substitute" in note for note in report.notes)


def test_receipt_forged_report_impossible(tmp_path):
    """Catches a receipt issued from a report the caller made, or without a
    verification of its own: write_receipt takes no report, a tampered bundle
    gets no receipt file, and a clean one gets a receipt bound to the bundle's
    realpath and st_dev. Mutant M21 (receipt written without its own
    verification) fails here.

    Negative control: the clean bundle gets its receipt; a second one is
    refused rather than overwritten.
    """
    params = inspect.signature(ab.write_receipt).parameters
    assert "report" not in params
    assert list(params) == ["bundle_dir", "extract_root", "destination"]

    bundle = _build(_standard_source(tmp_path), tmp_path, label="receipt")
    tampered = _reseal(bundle, tmp_path, members=_retouch("d/a.txt", data=b"ALPHA"))
    with pytest.raises(ab.ArchiveRefused, match="did not verify"):
        ab.write_receipt(tampered, extract_root=_fresh(tmp_path, "r"),
                         destination={"kind": "exfat"})
    assert not (tampered / ab.RECEIPT).exists()
    assert not [p for p in tampered.iterdir() if "RECEIPT" in p.name]

    path = ab.write_receipt(bundle, extract_root=_fresh(tmp_path, "r"),
                            destination={"kind": "exfat", "label": "E"})
    receipt = json.loads(path.read_text(encoding="ascii"))
    assert path == bundle / ab.RECEIPT
    assert receipt["bundle_realpath"] == os.path.realpath(bundle)
    assert receipt["bundle_st_dev"] == os.stat(bundle).st_dev
    assert receipt["status"] == VERIFIED
    assert receipt["payload_sha256"] == _sha((bundle / ab.TREE_PAYLOAD).read_bytes())
    assert receipt["manifest_sha256"] == _sha((bundle / ab.MANIFEST).read_bytes())
    assert receipt["content_digest"] == _manifest(bundle)["content_digest"]
    assert receipt["destination"] == {"kind": "exfat", "label": "E"}
    before = path.read_bytes()
    with pytest.raises(ab.ArchiveRefused, match="already exists"):
        ab.write_receipt(bundle, extract_root=_fresh(tmp_path, "r"), destination={})
    assert path.read_bytes() == before


def test_include_external_verifies(tmp_path, monkeypatch):
    """Catches include_external keeping only the link: the target's bytes must
    be stored as `.vh-external/0`, extracted and re-hashed from extract_root,
    and the link listed as verified-by-record, not created. Mutant M22
    (external content not stored) fails here.

    Negative control: with include_external=False the same source is refused.
    """
    (tmp_path / "store").mkdir()
    weights = b"external-weights" * 500
    (tmp_path / "store" / "w.bin").write_bytes(weights)
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"a")
    os.symlink("../store/w.bin", source / "model.bin")

    with pytest.raises(ab.ArchiveRefused):
        _build(source, tmp_path, label="noext")

    bundle = _build(source, tmp_path, label="ext", include_external=True)
    manifest = _manifest(bundle)
    assert manifest["externals"][0]["stored"] == ".vh-external/0"
    assert manifest["externals"][0]["link"] == "model.bin"
    assert manifest["externals"][0]["sha256"] == _sha(weights)
    assert manifest["totals"]["logical_bytes"] == len(weights) + 1

    hashed: list[str] = []
    real_hash = ab._hash_path
    monkeypatch.setattr(ab, "_hash_path",
                        lambda p: (hashed.append(os.fsdecode(os.fspath(p))), real_hash(p))[1])
    report, root = _verify(bundle, tmp_path)
    assert report.status == VERIFIED, report.failed()
    assert (root / ".vh-external" / "0").read_bytes() == weights
    assert str(root / ".vh-external" / "0") in hashed
    assert _check(report, "file:.vh-external/0")["ok"]
    external = _check(report, "external_link:model.bin")
    assert external["ok"] and "verified by record" in external["detail"]
    assert not os.path.lexists(root / "model.bin")


# --------------------------------------------------------------------------- #
# coverage
# --------------------------------------------------------------------------- #

def test_nonutf8_path_b64_roundtrip(tmp_path):
    """Catches a name that is not UTF-8 being mangled or dropped: it is
    recorded under path_b64, extracted under its exact bytes, and verifies.
    Mutant: names decoded with replacement characters.

    Negative control: a UTF-8 name is recorded under `path`, not path_b64.
    """
    source = tmp_path / "src"
    source.mkdir()
    raw = b"caf\xe9.bin"
    with open(os.path.join(os.fsencode(source), raw), "wb") as handle:
        handle.write(b"latin-1 name")
    (source / "café.txt").write_bytes(b"utf-8 name")
    bundle = _build(source, tmp_path, label="names")
    entries = _manifest(bundle)["entries"]
    b64 = [e for e in entries if "path_b64" in e]
    assert len(b64) == 1 and "path" not in b64[0]
    assert [e for e in entries if e.get("path") == "café.txt"]
    report, root = _verify(bundle, tmp_path)
    assert report.status == VERIFIED, report.failed()
    with open(os.path.join(os.fsencode(root), raw), "rb") as handle:
        assert handle.read() == b"latin-1 name"


def test_hardlinks_verify(tmp_path):
    """Catches hardlinked files losing their relation or their content: both
    are stored as ordinary members, share one hardlink group, and the group
    check needs equal re-hashed digests -- a resealed bundle whose group
    members differ (with matching per-file sums) fails only that check.
    Mutant: the hardlink-group check removed.

    Negative control: two separate copies carry no group.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "one").write_bytes(b"shared")
    os.link(source / "one", source / "two")
    (source / "copy-a").write_bytes(b"shared")
    (source / "copy-b").write_bytes(b"shared")
    bundle = _build(source, tmp_path, label="links")
    entries = {e["path"]: e for e in _manifest(bundle)["entries"]}
    assert entries["one"]["hardlink_group"] == entries["two"]["hardlink_group"] is not None
    assert entries["copy-a"]["hardlink_group"] is None
    assert entries["copy-b"]["hardlink_group"] is None
    assert all(m.type == tarfile.REGTYPE for m, _ in _members(bundle) if m.name in ("one", "two"))
    report, _ = _verify(bundle, tmp_path)
    assert report.status == VERIFIED, report.failed()
    group = entries["one"]["hardlink_group"]
    assert _check(report, "hardlink_group:" + str(group))["ok"]

    def split(record):
        for entry in record["entries"]:
            if entry["path"] == "two":
                entry["sha256"] = _sha(b"SHARED")
    damaged = _reseal(bundle, tmp_path, members=_retouch("two", data=b"SHARED"), manifest=split)
    report, _ = _verify(damaged, tmp_path)
    assert report.status == UNVERIFIED
    assert _failed(report) == {"hardlink_group:" + str(group)}


def test_exec_bit_roundtrip(tmp_path):
    """Catches modes lost in the round trip (exFAT keeps none, the bundle must):
    an executable file comes back 0o755 and a plain one 0o644. Mutant: modes
    not applied on extraction (or the stock `data` filter).

    Negative control: the plain file keeps 0o644, so the result is the bit.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "tool").write_bytes(b"#!/bin/sh\n")
    os.chmod(source / "tool", 0o755)
    (source / "plain").write_bytes(b"text")
    os.chmod(source / "plain", 0o644)
    (source / "shared").write_bytes(b"group-writable")
    os.chmod(source / "shared", 0o664)
    report, root = _verify(_build(source, tmp_path, label="exec"), tmp_path)
    assert report.status == VERIFIED, report.failed()
    assert stat.S_IMODE(os.lstat(root / "tool").st_mode) == 0o755
    assert stat.S_IMODE(os.lstat(root / "plain").st_mode) == 0o644
    assert stat.S_IMODE(os.lstat(root / "shared").st_mode) == 0o664


def test_extract_root_not_empty_refused(tmp_path):
    """Catches verification into a directory that already holds something --
    a leftover file could stand in for a missing member. It must fail
    extract_root_empty and never extract. Mutant: the emptiness check removed.

    Negative control: an empty extract_root verifies.
    """
    bundle = _build(_standard_source(tmp_path), tmp_path, label="nonempty")
    root = _fresh(tmp_path, "busy")
    (root / "leftover").write_bytes(b"x")
    report = ab.verify_archive(bundle, extract_root=root)
    assert report.status == UNVERIFIED
    assert report.extracted is False
    assert not _check(report, "extract_root_empty")["ok"]
    assert sorted(p.name for p in root.iterdir()) == ["leftover"]

    report, _ = _verify(bundle, tmp_path)
    assert report.status == VERIFIED, report.failed()


# --------------------------------------------------------------------------- #
# K8 -- never certify a link-only archive (Rev6 s1, Rev7 s1)
# --------------------------------------------------------------------------- #

_BLOB = b"model-weights" * 300


def _link_bundle(tmp_path: Path, *, link: str = "../../blobs/abc", stored: bool = True,
                 record: bool = True) -> Path:
    """A hand-made bundle holding the link `snapshots/h/model.bin -> <link>`
    whose target is outside the archived root, with (or without) an externals
    record and (or without) its stored content `.vh-external/0`."""
    members = []
    if stored:
        members += [(_info(".vh-external", tarfile.DIRTYPE, mode=0o755), None),
                    (_info(".vh-external/0"), _BLOB)]
    members += [(_info("snapshots", tarfile.DIRTYPE, mode=0o755), None),
                (_info("snapshots/h", tarfile.DIRTYPE, mode=0o755), None),
                (_info("snapshots/h/model.bin", tarfile.SYMTYPE, mode=0o777, link=link), None)]
    externals = [{"link": "snapshots/h/model.bin", "link_target": link,
                  "stored": ".vh-external/0", "sha256": _sha(_BLOB),
                  "size": len(_BLOB)}] if record else []
    return _hand_made(tmp_path, members, externals=externals)


def test_external_link_without_stored_content_unverified(tmp_path):
    """Catches a verifier that certifies a link-only archive (the 2026-09-28
    failure, seen from the verifier's side): a link out of the archived root
    with an externals record but no stored member must be ARCHIVE_UNVERIFIED
    with `external_content:<link>` failed -- whoever built the bundle. Also
    for an absolute link text, and for an escaping link with no record at all.
    Mutant M40 (externals accepted by their record only) verifies it and
    fails here.

    Negative control: the same bundle with the stored member present and
    correct verifies.
    """
    good = _link_bundle(tmp_path)
    report = ab.verify_archive(good, extract_root=_fresh(tmp_path, "ok"))
    assert report.status == VERIFIED, report.failed()
    assert _check(report, "external_content:snapshots/h/model.bin")["ok"]

    for link in ("../../blobs/abc", "/abs/x"):
        if link.startswith("/"):
            control = _link_bundle(tmp_path, link=link)
            assert ab.verify_archive(control, extract_root=_fresh(tmp_path, "ok")
                                     ).status == VERIFIED
        bare = _link_bundle(tmp_path, link=link, stored=False)
        report = ab.verify_archive(bare, extract_root=_fresh(tmp_path, "bare"))
        assert report.status == UNVERIFIED
        content = _check(report, "external_content:snapshots/h/model.bin")
        assert not content["ok"] and "not a regular-file member" in content["detail"]
        assert _check(report, "member_list")["ok"] and _check(report, "extract")["ok"]
        assert "nothing was re-hashed" in _check(
            report, "external_link:snapshots/h/model.bin")["detail"]

    unrecorded = _link_bundle(tmp_path, link="../../../blobs/abc", stored=False,
                              record=False)
    report = ab.verify_archive(unrecorded, extract_root=_fresh(tmp_path, "norecord"))
    assert report.status == UNVERIFIED
    content = _check(report, "external_content:snapshots/h/model.bin")
    assert not content["ok"] and "only the link" in content["detail"]


def _external_source(tmp_path: Path) -> Path:
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "w.bin").write_bytes(_BLOB)
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"a")
    os.symlink("../store/w.bin", source / "model.bin")
    return source


def test_resealed_external_dropped_unverified(tmp_path):
    """Catches a builder-made include_external bundle whose stored content was
    dropped afterwards -- from the tar, the manifest entries and the totals,
    with SHA256SUMS, payload_sha256 and the content digest recomputed: the
    member list, totals and digests all agree, and only the externals record
    still says what must be there. It must be ARCHIVE_UNVERIFIED with
    `external_content:model.bin` failed. Mutant M40 verifies it.

    Negative control: the identical reseal without dropping verifies.
    """
    bundle = _build(_external_source(tmp_path), tmp_path, label="ext",
                    include_external=True)
    _identity_control(bundle, tmp_path)

    def drop(record):
        gone = [e for e in record["entries"] if e["path"].startswith(".vh-external")]
        record["entries"] = [e for e in record["entries"] if e not in gone]
        totals = record["totals"]
        totals["dirs"] -= sum(e["type"] == "dir" for e in gone)
        totals["files"] -= sum(e["type"] == "file" for e in gone)
        totals["logical_bytes"] -= sum(e["size"] for e in gone if e["type"] == "file")
        totals["entry_count"] -= len(gone)

    damaged = _reseal(bundle, tmp_path, manifest=drop, members=lambda listed: [
        m for m in listed if not m[0].name.startswith(".vh-external")])
    report, _ = _verify(damaged, tmp_path)
    assert report.status == UNVERIFIED
    assert not _check(report, "external_content:model.bin")["ok"]
    for passing in ("member_list", "extract", "totals", "content_digest",
                    "sha256sums_payload", "sha256sums_manifest"):
        assert _check(report, passing)["ok"], passing


def test_external_stored_content_mismatch_unverified(tmp_path):
    """Catches stored content that exists but is not the content the record
    names: `.vh-external/0` is replaced (same size) and its manifest entry
    resealed to match, so its own `file:` check passes; the externals
    record's sha256 no longer matches the re-hashed bytes. It must be
    ARCHIVE_UNVERIFIED with `external_content:model.bin` failed. Mutant M41
    (existence-only check of the stored entry) verifies it and fails here.

    Negative control: the identical reseal without the change verifies.
    """
    bundle = _build(_external_source(tmp_path), tmp_path, label="ext",
                    include_external=True)
    _identity_control(bundle, tmp_path)
    other = bytes(reversed(_BLOB))

    def match_entry(record):
        for entry in record["entries"]:
            if entry["path"] == ".vh-external/0":
                entry["sha256"] = _sha(other)

    damaged = _reseal(bundle, tmp_path, members=_retouch(".vh-external/0", data=other),
                      manifest=match_entry)
    report, root = _verify(damaged, tmp_path)
    assert report.status == UNVERIFIED
    assert _check(report, "file:.vh-external/0")["ok"]
    content = _check(report, "external_content:model.bin")
    assert not content["ok"] and _sha(other) in content["detail"]
    assert (root / ".vh-external" / "0").read_bytes() == other


def test_receipt_refused_for_link_only_bundle(tmp_path):
    """Catches a receipt for a link-only bundle: write_receipt runs the
    verifier itself, so a bundle whose external link has no stored content
    gets no ARCHIVE_RECEIPT.json -- not even a dangling name. Mutant M40
    verifies the bundle and writes the receipt.

    Negative control: the same bundle with its stored content gets a receipt.
    """
    bare = _link_bundle(tmp_path, stored=False)
    with pytest.raises(ab.ArchiveRefused, match="external_content") as caught:
        ab.write_receipt(bare, extract_root=_fresh(tmp_path, "r"), destination={})
    assert not caught.value.report.check("external_content:snapshots/h/model.bin")["ok"]
    assert not os.path.lexists(bare / ab.RECEIPT)

    good = _link_bundle(tmp_path)
    path = ab.write_receipt(good, extract_root=_fresh(tmp_path, "r"), destination={})
    assert json.loads(path.read_text(encoding="ascii"))["status"] == VERIFIED


# --------------------------------------------------------------------------- #
# K9 -- extraction containment (Rev6 s3)
# --------------------------------------------------------------------------- #

def test_extraction_refuses_dotdot_member(tmp_path):
    """Catches a member name with a `..` component that would be written
    beside extract_root: `../escaped` and `a/../../escaped`. The `extract`
    check must fail naming the `..` rule, nothing may appear at the escaped
    path, and the sentinel keeps its bytes and mode. Mutant M13b (the `..`
    refusal removed) fails here.

    Negative control: the member under the plain name `escaped` verifies.
    """
    sentinel, data, mode = _sentinel(tmp_path)
    good = _hand_made(tmp_path, [(_info("escaped"), b"EVIL")])
    assert ab.verify_archive(good, extract_root=_fresh(tmp_path, "ok")).status == VERIFIED

    for name in ("../escaped", "a/../../escaped"):
        hostile = _hand_made(tmp_path, [(_info(name), b"EVIL")])
        root = _hostile_root(tmp_path)
        report = ab.verify_archive(hostile, extract_root=root)
        assert report.status == UNVERIFIED
        extract = _check(report, "extract")
        assert not extract["ok"] and "'..' component" in extract["detail"]
        assert not os.path.lexists(root.parent / "escaped")
        assert not os.path.lexists(root.parent / "a")
        _sentinel_intact(sentinel, data, mode)
        os.rename(root, root.parent / ("root.v" + str(next(_COUNTER))))   # a fresh root next


def test_extraction_refuses_absolute_member(tmp_path):
    """Catches a member with an absolute name: `/tmp-abs-<uuid>/x`, a path
    that does not exist before and must not exist afterwards. The `extract`
    check must fail naming the absolute name, and the sentinel keeps its
    bytes and mode. Mutant M13 (absolute-name refusal removed) fails here.

    Negative control: the same member under the relative name verifies.
    """
    import uuid

    sentinel, data, mode = _sentinel(tmp_path)
    unique = "tmp-abs-" + uuid.uuid4().hex
    absolute = Path("/") / unique
    assert not os.path.lexists(absolute)
    good = _hand_made(tmp_path, [(_info(unique, tarfile.DIRTYPE, mode=0o755), None),
                                 (_info(unique + "/x"), b"EVIL")])
    assert ab.verify_archive(good, extract_root=_fresh(tmp_path, "ok")).status == VERIFIED

    hostile = _hand_made(tmp_path, [(_info(str(absolute / "x")), b"EVIL")])
    report = ab.verify_archive(hostile, extract_root=_hostile_root(tmp_path))
    assert report.status == UNVERIFIED
    extract = _check(report, "extract")
    assert not extract["ok"] and "absolute" in extract["detail"]
    assert not os.path.lexists(absolute)
    _sentinel_intact(sentinel, data, mode)


def test_extraction_refuses_parent_escape_link(tmp_path):
    """Catches writing through a link chain that leaves extract_root although
    every link text stays inside lexically: `s/up -> ..`, `s/x -> up/..`
    (really extract_root's parent), then a member `s/x/escaped` written
    through it. The `extract` check must fail at that member, nothing may
    appear beside extract_root, and the sentinel keeps its bytes and mode.
    Mutant M14d (the symlink-component and parent-realpath checks removed)
    writes `escaped` outside and fails here.

    Negative control: the same members with `s/x` a real directory verify.
    """
    sentinel, data, mode = _sentinel(tmp_path)
    good = _hand_made(tmp_path, [(_info("s", tarfile.DIRTYPE, mode=0o755), None),
                                 (_info("s/up", tarfile.SYMTYPE, mode=0o777, link=".."), None),
                                 (_info("s/x", tarfile.DIRTYPE, mode=0o755), None),
                                 (_info("s/x/escaped"), b"EVIL")])
    assert ab.verify_archive(good, extract_root=_fresh(tmp_path, "ok")).status == VERIFIED

    hostile = _hand_made(tmp_path, [
        (_info("s", tarfile.DIRTYPE, mode=0o755), None),
        (_info("s/up", tarfile.SYMTYPE, mode=0o777, link=".."), None),
        (_info("s/x", tarfile.SYMTYPE, mode=0o777, link="up/.."), None),
        (_info("s/x/escaped"), b"EVIL")])
    root = _hostile_root(tmp_path)
    report = ab.verify_archive(hostile, extract_root=root)
    assert report.status == UNVERIFIED
    extract = _check(report, "extract")
    assert not extract["ok"] and "s/x/escaped" in extract["detail"]
    assert not os.path.lexists(root.parent / "escaped")
    assert sorted(p.name for p in root.parent.iterdir()) == ["root"]
    _sentinel_intact(sentinel, data, mode)


# --------------------------------------------------------------------------- #
# K10 -- one killing test per directive check (Rev6 s5, Rev7 s4)
# --------------------------------------------------------------------------- #

def _copy_bundle(bundle: Path, tmp_path: Path, *, edit=None, reseal: bool = True) -> Path:
    """A copy of `bundle` (same name, same payload) whose manifest was edited;
    SHA256SUMS is recomputed for the new manifest only if `reseal`."""
    payload_name = ab.TREE_PAYLOAD if (bundle / ab.TREE_PAYLOAD).exists() else ab.GIT_PAYLOAD
    target = _fresh(tmp_path, "copy") / bundle.name
    target.mkdir()
    payload = (bundle / payload_name).read_bytes()
    record = _manifest(bundle)
    if edit is not None:
        edit(record)
    body = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("ascii")
    (target / payload_name).write_bytes(payload)
    (target / ab.MANIFEST).write_bytes(body)
    if reseal:
        (target / ab.SUMS).write_text(
            f"{_sha(payload)}  {payload_name}\n{_sha(body)}  {ab.MANIFEST}\n", encoding="ascii")
    else:
        shutil.copy2(bundle / ab.SUMS, target / ab.SUMS)
    return target


def _git_reseal(bundle: Path, tmp_path: Path, *, edit=None, payload: bytes | None = None,
                ) -> Path:
    """A copy of a git bundle with an edited manifest and/or another payload,
    payload_sha256, the content digest and SHA256SUMS recomputed."""
    target = _fresh(tmp_path, "git-resealed") / bundle.name
    target.mkdir()
    data = (bundle / ab.GIT_PAYLOAD).read_bytes() if payload is None else payload
    record = _manifest(bundle)
    if edit is not None:
        edit(record)
    record["content_digest"] = ab._git_digest(record["refs"])
    record["payload_sha256"] = _sha(data)
    body = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("ascii")
    (target / ab.GIT_PAYLOAD).write_bytes(data)
    (target / ab.MANIFEST).write_bytes(body)
    (target / ab.SUMS).write_text(
        f"{_sha(data)}  {ab.GIT_PAYLOAD}\n{_sha(body)}  {ab.MANIFEST}\n", encoding="ascii")
    return target


def _ref(record: dict, name: str) -> dict:
    return next(r for r in record["refs"] if r["name"] == name)


def test_mutant_sha256sums_manifest(tmp_path):
    """Catches an edited MANIFEST.json whose SHA256SUMS line was not updated:
    only meta changed, so the payload, its digest and every entry still
    agree; `sha256sums_manifest` must fail. Mutant M10b (manifest compared
    with SHA256SUMS no longer) verifies it and fails here.

    Negative control: the same edit with SHA256SUMS resealed verifies.
    """
    bundle = _build(_standard_source(tmp_path), tmp_path, label="sums")

    def edit(record):
        record["meta"] = {"run_id": "edited"}

    assert _verify(_copy_bundle(bundle, tmp_path, edit=edit), tmp_path)[0].status == VERIFIED
    report, _ = _verify(_copy_bundle(bundle, tmp_path, edit=edit, reseal=False), tmp_path)
    assert report.status == UNVERIFIED
    assert _failed(report) == {"sha256sums_manifest"}


def test_mutant_totals_logical_bytes(tmp_path):
    """Catches totals that do not describe the payload: logical_bytes and
    files edited, every digest resealed. `totals` (recomputed from the
    extraction) must fail. Mutant M26 (totals not recomputed) fails here.

    Negative control: the identical reseal without the edit verifies.
    """
    bundle = _build(_standard_source(tmp_path), tmp_path, label="totals")
    _identity_control(bundle, tmp_path)

    def edit(record):
        record["totals"]["logical_bytes"] += 1000
        record["totals"]["files"] += 1

    report, _ = _verify(_reseal(bundle, tmp_path, manifest=edit), tmp_path)
    assert report.status == UNVERIFIED
    assert _failed(report) == {"totals"}


def test_mutant_content_digest(tmp_path):
    """Catches a content_digest that does not belong to the entries: it is
    replaced and SHA256SUMS resealed. `content_digest` must fail. Mutant M27
    (content digest not recomputed) fails here.

    Negative control: a reseal keeping the digest verifies.
    """
    bundle = _build(_standard_source(tmp_path), tmp_path, label="digest")
    assert _verify(_copy_bundle(bundle, tmp_path), tmp_path)[0].status == VERIFIED

    def edit(record):
        record["content_digest"] = "0" * 64

    report, _ = _verify(_copy_bundle(bundle, tmp_path, edit=edit), tmp_path)
    assert report.status == UNVERIFIED
    assert _failed(report) == {"content_digest"}


def test_mutant_incomplete_marker(tmp_path):
    """Catches a bundle marked INCOMPLETE being certified: an otherwise intact
    bundle with the marker added must fail `not_incomplete`. Mutant M25
    (marker ignored) fails here.

    Negative control: the same copy without the marker verifies.
    """
    bundle = _build(_standard_source(tmp_path), tmp_path, label="marker")
    assert _verify(_copy_bundle(bundle, tmp_path), tmp_path)[0].status == VERIFIED
    marked = _copy_bundle(bundle, tmp_path)
    (marked / ab.INCOMPLETE).write_bytes(b"{}\n")
    report, _ = _verify(marked, tmp_path)
    assert report.status == UNVERIFIED
    assert _failed(report) == {"not_incomplete"}


def test_mutant_zstd_trailing_data(tmp_path):
    """Catches bytes after the end of the zstd frame (resealed): `zstd_stream`
    must fail with 'trailing data'. Mutant M11b (trailing data tolerated)
    fails here.

    Negative control: the identical reseal without the bytes verifies.
    """
    bundle = _build(_standard_source(tmp_path), tmp_path, label="trailing")
    _identity_control(bundle, tmp_path)
    report, _ = _verify(_reseal(bundle, tmp_path, payload=lambda d: d + b"\x00planted"),
                        tmp_path)
    assert report.status == UNVERIFIED
    stream = _check(report, "zstd_stream")
    assert not stream["ok"] and "trailing data" in stream["detail"]
    assert _check(report, "sha256sums_payload")["ok"]


def test_mutant_zstd_no_checksum(tmp_path):
    """Catches a frame without a content checksum being reported as verified:
    the payload is re-compressed with write_checksum=False and resealed.
    `zstd_stream` must fail with 'no checksum present' and never claim a
    verified checksum. Mutant M43 (frame header not read) fails here.

    Negative control: the same tar re-compressed with write_checksum=True
    verifies.
    """
    bundle = _build(_standard_source(tmp_path), tmp_path, label="nosum")

    def recompress(checksum: bool):
        def apply(data: bytes) -> bytes:
            plain = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)).read()
            return zstandard.ZstdCompressor(write_checksum=checksum).compress(plain)
        return apply

    report, _ = _verify(_reseal(bundle, tmp_path, payload=recompress(True)), tmp_path)
    assert report.status == VERIFIED, report.failed()
    report, _ = _verify(_reseal(bundle, tmp_path, payload=recompress(False)), tmp_path)
    assert report.status == UNVERIFIED
    stream = _check(report, "zstd_stream")
    assert not stream["ok"] and "no checksum present" in stream["detail"]
    assert "checksum present and verified" not in stream["detail"]


def test_mutant_lexical_containment(tmp_path):
    """Catches a link that is outside the source lexically but inside on real
    resolution: `src/x -> ../src/y`. Extracted elsewhere its text would leave
    the extraction root, so the builder must refuse it on the lexical rule
    (Rev2 s2). Mutant M2b (lexical rule removed, realpath only) builds it and
    fails here.

    Negative control: `src/x -> y` builds and verifies.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "y").write_bytes(b"y")
    os.symlink("../src/y", source / "x")
    with pytest.raises(ab.ArchiveRefused, match="target escapes the source: "):
        _build(source, tmp_path, label="lexical")

    control = tmp_path / "ctl" / "src"
    control.mkdir(parents=True)
    (control / "y").write_bytes(b"y")
    os.symlink("y", control / "x")
    report, _ = _verify(_build(control, tmp_path, label="plain"), tmp_path)
    assert report.status == VERIFIED, report.failed()


def _changing_build(tmp_path: Path, monkeypatch, hook, **kwargs):
    monkeypatch.setattr(ab, "_after_payload_hook", hook)
    try:
        with pytest.raises(ab.SourceChanged) as caught:
            _build(kwargs.pop("source"), tmp_path, **kwargs)
    finally:
        monkeypatch.setattr(ab, "_after_payload_hook", None)
    assert (caught.value.bundle_dir / ab.INCOMPLETE).is_file()
    return caught.value


def test_mutant_restat_ctime(tmp_path, monkeypatch):
    """Catches a change visible only in ctime: the hook chmods a file to the
    mode it already has, so size, mtime, inode and type stay equal. The build
    must raise SourceChanged. Mutant M17b (ctime not compared) fails here.

    Negative control: a hook that changes nothing lets the build finish.
    """
    source = _standard_source(tmp_path)
    before = {}

    def touch_ctime(*, source, bundle_dir):
        st = os.lstat(source / "b.bin")
        before.update(mtime=st.st_mtime_ns, size=st.st_size, ino=st.st_ino,
                      ctime=st.st_ctime_ns)
        # ctime comes from a coarse kernel clock: repeat the same-mode chmod
        # until the tick has advanced (bounded), so ctime really differs.
        deadline = time.monotonic() + 2.0
        while True:
            os.chmod(source / "b.bin", stat.S_IMODE(st.st_mode))
            after = os.lstat(source / "b.bin")
            if after.st_ctime_ns != before["ctime"] or time.monotonic() > deadline:
                break
            time.sleep(0.001)
        assert (after.st_mtime_ns, after.st_size, after.st_ino) == (
            before["mtime"], before["size"], before["ino"])
        assert after.st_ctime_ns != before["ctime"]

    error = _changing_build(tmp_path, monkeypatch, touch_ctime, source=source, label="ctime")
    assert "b.bin" in str(error)
    monkeypatch.setattr(ab, "_after_payload_hook", lambda **_: None)
    assert _build(source, tmp_path, label="steady").is_dir()


def test_mutant_restat_inode(tmp_path, monkeypatch):
    """Catches a file replaced by another inode with the same size, mtime and
    ctime: after the payload is written, the module's lstat seam reports a
    different st_ino for that one path. The build must raise SourceChanged.
    Mutant M17c (inode not compared) fails here.

    Negative control: the build without the patched inode finishes.
    """
    source = _standard_source(tmp_path)
    wanted = os.fsencode(source / "d" / "a.txt")
    real_lstat = os.lstat

    def new_inode(*, source, bundle_dir):
        def fake(p, *args, **kwargs):
            st = real_lstat(p, *args, **kwargs)
            if os.fsencode(p) == wanted:
                return _StatProxy(st, st_ino=st.st_ino + 1)
            return st
        monkeypatch.setattr(ab, "_lstat", fake)

    error = _changing_build(tmp_path, monkeypatch, new_inode, source=source, label="inode")
    assert "a.txt" in str(error)
    monkeypatch.setattr(ab, "_lstat", os.lstat)
    assert _build(source, tmp_path, label="steady").is_dir()


class _StatProxy:
    """A stat result with some fields replaced; every other field is real."""

    def __init__(self, real, **override):
        self._real = real
        self._override = override

    def __getattr__(self, name):
        if name in self._override:
            return self._override[name]
        return getattr(self._real, name)


def test_mutant_restat_external(tmp_path, monkeypatch):
    """Catches an include_external target that changes while it is archived:
    the hook appends to the target outside the source. Only the external
    entry's re-stat sees it. The build must raise SourceChanged. Mutant M17d
    (external targets not re-stat'ed) fails here.

    Negative control: the unchanged target builds.
    """
    source = _external_source(tmp_path)

    def grow(*, source, bundle_dir):
        with open(tmp_path / "store" / "w.bin", "ab") as handle:
            handle.write(b"+")

    error = _changing_build(tmp_path, monkeypatch, grow, source=source, label="ext",
                            include_external=True)
    assert "w.bin" in str(error)
    bundle = _build(source, tmp_path, label="steady", include_external=True)
    assert _manifest(bundle)["totals"]["external"] == 1


def test_mutant_git_heads(tmp_path):
    """Catches a bundle whose header does not match the recorded unpeeled
    ids: refs/heads/main's recorded unpeeled id is edited (resealed).
    `git_heads` -- the bundle header parsed by the verifier itself, no
    `bundle list-heads` (Rev10b s3) -- must fail. Mutant M19b (the header
    not compared) fails here.

    Negative control: the unedited reseal verifies.
    """
    repo = _repo(tmp_path, "repo")
    bundle = _git_build(repo, tmp_path, ["main", "v1"])
    assert _verify(_git_reseal(bundle, tmp_path), tmp_path)[0].status == VERIFIED
    older = _git(repo, "rev-parse", "main~1")

    def edit(record):
        _ref(record, "refs/heads/main")["unpeeled"] = older

    report, _ = _verify(_git_reseal(bundle, tmp_path, edit=edit), tmp_path)
    assert report.status == UNVERIFIED
    heads = _check(report, "git_heads")
    assert not heads["ok"] and older in heads["detail"]


def test_mutant_git_commit(tmp_path):
    """Catches a recorded peeled commit that is not the ref's: the commit of
    refs/heads/main is edited (unpeeled id and tree untouched, resealed).
    `ref:refs/heads/main` must fail. Mutant M19c (commit not compared) fails
    here.

    Negative control: the unedited reseal verifies.
    """
    repo = _repo(tmp_path, "repo")
    bundle = _git_build(repo, tmp_path, ["main", "v1"])
    assert _verify(_git_reseal(bundle, tmp_path), tmp_path)[0].status == VERIFIED
    older = _git(repo, "rev-parse", "main~1")

    def edit(record):
        _ref(record, "refs/heads/main")["commit"] = older

    report, _ = _verify(_git_reseal(bundle, tmp_path, edit=edit), tmp_path)
    assert report.status == UNVERIFIED
    assert _check(report, "git_heads")["ok"]
    ref = _check(report, "ref:refs/heads/main")
    assert not ref["ok"] and older in ref["detail"]


def test_mutant_git_bundle_verify(tmp_path):
    """Catches a git payload that cannot stand alone: it is replaced by a
    bundle `main~1..main` whose prerequisite commit is absent (resealed, the
    manifest recording main only). `git_bundle_verify` must fail. Mutant M19d
    (bundle verify dropped) fails here.

    Negative control: a complete bundle of main alone verifies.
    """
    repo = _repo(tmp_path, "repo")
    bundle = _git_build(repo, tmp_path, ["main"])
    report, _ = _verify(bundle, tmp_path)
    assert report.status == VERIFIED, report.failed()
    thin = tmp_path / "thin.bundle"
    _git(repo, "bundle", "create", "-q", str(thin), "main~1..main")
    report, _ = _verify(_git_reseal(bundle, tmp_path, payload=thin.read_bytes()), tmp_path)
    assert report.status == UNVERIFIED
    verify = _check(report, "git_bundle_verify")
    assert not verify["ok"] and "prerequisite" in verify["detail"]


def test_mutant_git_fsck(tmp_path, monkeypatch):
    """Catches a git payload whose objects are malformed although the fetch
    accepts them: a commit with a broken author line (written with
    `hash-object --literally`) is bundled; `git fsck --full --strict` on the
    fetched repository must fail `git_fsck`. This is real damage, no runner is faked.
    Mutant M36 (fsck dropped) fails here.

    Negative control: a well-formed repository verifies.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)   # no user fsck settings
    good = _repo(tmp_path, "good")
    report, _ = _verify(_git_build(good, tmp_path, ["main"], label="good"), tmp_path)
    assert report.status == VERIFIED, report.failed()

    bad = _repo(tmp_path, "bad")
    tree = _git(bad, "rev-parse", "main^{tree}")
    body = ("tree %s\nauthor nobody <broken 1 +0000\ncommitter nobody <broken 1 +0000\n"
            "\nmalformed\n" % tree)
    commit = subprocess.run(["git", "-C", str(bad), "hash-object", "-t", "commit",
                             "--literally", "-w", "--stdin"], input=body, env=GIT_ENV,
                            capture_output=True, text=True, check=True).stdout.strip()
    _git(bad, "update-ref", "refs/heads/main", commit)
    report, _ = _verify(_git_build(bad, tmp_path, ["main"], label="bad"), tmp_path)
    assert report.status == UNVERIFIED
    assert _check(report, "extract")["ok"] and _check(report, "git_bundle_verify")["ok"]
    fsck = _check(report, "git_fsck")
    assert not fsck["ok"] and commit in fsck["detail"]


# --------------------------------------------------------------------------- #
# K10 -- git refs, covers_worktree, the bundle name
# --------------------------------------------------------------------------- #

def test_git_annotated_tag_verifies(tmp_path):
    """Catches refs compared without peeling: an annotated tag's unpeeled id is
    a tag object, not a commit. A bundle of main and the annotated tag v2
    must verify, recording the tag object as unpeeled and its commit as
    commit. Mutant M44 (unpeeled not compared, or the listed head compared
    with the commit) fails here: the intact bundle no longer verifies, or a
    reseal that records the commit as the tag's unpeeled id does.

    Negative control: that reseal fails `git_heads`.
    """
    repo = _repo(tmp_path, "repo")
    _git(repo, "tag", "-a", "v2", "-m", "annotated")
    bundle = _git_build(repo, tmp_path, ["main", "v2"])
    tag = _ref(_manifest(bundle), "refs/tags/v2")
    assert tag["unpeeled"] == _git(repo, "rev-parse", "v2")
    assert _git(repo, "cat-file", "-t", tag["unpeeled"]) == "tag"
    assert tag["commit"] == _git(repo, "rev-parse", "v2^{commit}") != tag["unpeeled"]
    report, _ = _verify(bundle, tmp_path)
    assert report.status == VERIFIED, report.failed()
    assert _check(report, "ref:refs/tags/v2")["ok"] and _check(report, "git_heads")["ok"]

    def as_commit(record):
        _ref(record, "refs/tags/v2")["unpeeled"] = tag["commit"]

    report, _ = _verify(_git_reseal(bundle, tmp_path, edit=as_commit), tmp_path)
    assert report.status == UNVERIFIED
    assert not _check(report, "git_heads")["ok"]
    assert not _check(report, "ref:refs/tags/v2")["ok"]


def test_git_head_ref_verifies(tmp_path):
    """Catches a symbolic HEAD that cannot be verified: a bundle of refs
    ['HEAD', 'main'] lists HEAD under its own name; it must verify with a
    `ref:HEAD` check. Mutant: HEAD recorded under the branch it points to
    (the bundle's HEAD then has no record) fails here.

    Negative control: HEAD's recorded unpeeled id edited (resealed) fails.
    """
    repo = _repo(tmp_path, "repo")
    bundle = _git_build(repo, tmp_path, ["HEAD", "main"])
    names = sorted(r["name"] for r in _manifest(bundle)["refs"])
    assert names == ["HEAD", "refs/heads/main"]
    report, _ = _verify(bundle, tmp_path)
    assert report.status == VERIFIED, report.failed()
    assert _check(report, "ref:HEAD")["ok"]

    def edit(record):
        _ref(record, "HEAD")["unpeeled"] = _git(repo, "rev-parse", "main~1")

    report, _ = _verify(_git_reseal(bundle, tmp_path, edit=edit), tmp_path)
    assert report.status == UNVERIFIED
    assert not _check(report, "ref:HEAD")["ok"]


def test_git_covers_worktree_rederived(tmp_path):
    """Catches a covers_worktree claim its recorded facts do not support:
    resealed covers_worktree=true with all_refs_included=false, and
    separately with stash_empty=false, must fail
    `covers_worktree_consistent` and report covers_worktree False. Mutant M45
    (the recorded flag trusted) fails here.

    Negative controls: the clean bundle (true, all facts true) and the
    partial one (false, all_refs_included false) are both consistent and
    verify.
    """
    clean = _repo(tmp_path, "clean")
    bundle = _git_build(clean, tmp_path, ["main", "v1"], label="clean")
    report, _ = _verify(bundle, tmp_path)
    assert report.status == VERIFIED and report.covers_worktree is True
    partial = _repo(tmp_path, "partial")
    _git(partial, "branch", "side")
    report, _ = _verify(_git_build(partial, tmp_path, ["main"], label="partial"), tmp_path)
    assert report.status == VERIFIED and report.covers_worktree is False
    assert _check(report, "covers_worktree_consistent")["ok"]

    for fact in ("all_refs_included", "stash_empty"):
        def edit(record, fact=fact):
            record[fact] = False
            record["covers_worktree"] = True
        report, _ = _verify(_git_reseal(bundle, tmp_path, edit=edit), tmp_path)
        assert report.status == UNVERIFIED
        assert _failed(report) == {"covers_worktree_consistent"}, fact
        assert report.covers_worktree is False


def test_git_ownership_gate_refused(tmp_path):
    """Catches a git archive of a repository nobody proved is ours: build_git
    takes the same ownership gate as build_tree (Rev6 s6). Mutant: the gate
    left out of build_git.

    Negative control: OWNED with a root containing the repository builds.
    """
    repo = _repo(tmp_path, "repo")
    out = _fresh(tmp_path, "gout")
    elsewhere = _fresh(tmp_path, "elsewhere")
    for ownership in ({"status": "UNKNOWN", "root": str(tmp_path)}, _owned(elsewhere)):
        with pytest.raises(ab.ArchiveRefused, match="ownership"):
            ab.ArchiveBundle.build_git(repo, ["main"], out, label="g", meta={},
                                       ownership=ownership)
    assert not list(out.iterdir())
    assert ab.ArchiveBundle.build_git(repo, ["main"], out, label="g", meta={},
                                      ownership=_owned(tmp_path)).is_dir()


def test_renamed_bundle_fails(tmp_path):
    """Catches a bundle directory that is not the bundle its manifest names:
    an intact bundle renamed to a valid-pattern name with another digest must
    fail `bundle_name`. Mutant M47 (the name not bound to bundle_id) fails
    here.

    Negative control: the bundle verifies before the rename.
    """
    bundle = _build(_standard_source(tmp_path), tmp_path, label="named")
    report, _ = _verify(bundle, tmp_path)
    assert report.status == VERIFIED and _check(report, "bundle_name")["ok"]
    renamed = bundle.parent / ("vh-arch-20260101T000000Z-" + "f" * 12 + "-named")
    assert renamed.name != bundle.name
    os.rename(bundle, renamed)
    report, _ = _verify(renamed, tmp_path)
    assert report.status == UNVERIFIED
    assert _failed(report) == {"bundle_name"}


# --------------------------------------------------------------------------- #
# K12 -- surviving checks of the verifier and the receipt (Rev8 s3-s4, Rev9 s2)
# --------------------------------------------------------------------------- #

def test_mutant_record_size(tmp_path):
    """Catches an externals record whose size is wrong although its SHA-256 is
    right: the record's size is raised by one and the bundle resealed, so
    everything else agrees. `external_content:model.bin` must fail, and only
    it. Mutant (the record-size comparison removed) verifies it and fails
    here.

    Negative control: the identical reseal without the edit verifies.
    """
    bundle = _build(_external_source(tmp_path), tmp_path, label="ext",
                    include_external=True)
    _identity_control(bundle, tmp_path)

    def grow(record):
        record["externals"][0]["size"] += 1

    report, _ = _verify(_reseal(bundle, tmp_path, manifest=grow), tmp_path)
    assert report.status == UNVERIFIED
    assert _failed(report) == {"external_content:model.bin"}
    assert "size" in _check(report, "external_content:model.bin")["detail"]


def test_mutant_m21b(tmp_path, monkeypatch):
    """Catches a receipt for a bundle edited after it was verified (M21b, the
    receipt's post-verify re-hash removed): the module's verify_archive is
    wrapped so that it runs the real verification and then flips one payload
    byte. write_receipt must refuse, and ARCHIVE_RECEIPT.json (or a temporary
    receipt) must not exist. Mutant M21b writes the receipt and fails here.

    Negative control: the unwrapped call issues a receipt for an identical
    bundle.
    """
    source = _standard_source(tmp_path)
    bundle = _build(source, tmp_path, label="edited")
    control = _build(source, tmp_path, label="control")
    real = ab.verify_archive

    def verify_then_edit(bundle_dir, *, extract_root):
        report = real(bundle_dir, extract_root=extract_root)
        payload = Path(bundle_dir) / ab.TREE_PAYLOAD
        size = payload.stat().st_size
        with open(payload, "r+b") as handle:
            handle.seek(size // 2)
            byte = handle.read(1)
            handle.seek(size // 2)
            handle.write(bytes([byte[0] ^ 0xFF]))
        return report

    monkeypatch.setattr(ab, "verify_archive", verify_then_edit)
    with pytest.raises(ab.ArchiveRefused, match="changed after it was verified"):
        ab.write_receipt(bundle, extract_root=_fresh(tmp_path, "r"), destination={})
    assert not os.path.lexists(bundle / ab.RECEIPT)
    assert not [p for p in bundle.iterdir() if "RECEIPT" in p.name]

    monkeypatch.undo()
    path = ab.write_receipt(control, extract_root=_fresh(tmp_path, "r"), destination={})
    assert json.loads(path.read_text(encoding="ascii"))["status"] == VERIFIED


def test_mutant_m14c(tmp_path):
    """Catches extraction onto a destination that already exists (M14c): a
    hand-made payload creates `d/y -> ..` and `d/z -> y/../../sentinel` (both
    texts lexically inside the root), then a regular member named `d/z`
    again. Writing it would follow the existing link out of extract_root and
    overwrite the sentinel. The member must be refused ("already exists") and
    the sentinel keep its bytes and mode. Mutant M14c (an existing
    destination admitted) writes through the link and fails here.

    Negative control: the same members with the regular file under a new name
    `d/zz` extract (extract check ok) and leave the sentinel intact.
    """
    sentinel, data, mode = _sentinel(tmp_path)
    links = [(_info("d", tarfile.DIRTYPE, mode=0o755), None),
             (_info("d/y", tarfile.SYMTYPE, mode=0o777, link=".."), None),
             (_info("d/z", tarfile.SYMTYPE, mode=0o777, link="y/../../sentinel"), None)]
    control = _hand_made(tmp_path, links + [(_info("d/zz", mode=0o644), b"EVIL")])
    report = ab.verify_archive(control, extract_root=_hostile_root(tmp_path))
    assert _check(report, "extract")["ok"]
    _sentinel_intact(sentinel, data, mode)

    hostile = _hand_made(tmp_path, links + [(_info("d/z", mode=0o644), b"EVIL")])
    root = _fresh(tmp_path, "victim") / "root"
    root.mkdir()
    report = ab.verify_archive(hostile, extract_root=root)
    assert report.status == UNVERIFIED
    extract = _check(report, "extract")
    assert not extract["ok"] and "already exists" in extract["detail"]
    _sentinel_intact(sentinel, data, mode)


def test_mutant_m34(tmp_path):
    """Catches an external link reported as "verified by record" although it
    does not match its record (M34, the external record matched by the
    extraction filter only). Case A: the manifest entry's link text is
    edited (resealed) -- the filter still matches the tar member against the
    record and skips it. Case B: the tar member's link text is edited -- the
    filter refuses it, so it is never skipped. In both, `external_link:
    model.bin` must fail (member_list fails too, so the status is
    ARCHIVE_UNVERIFIED either way: the mutant falsifies the check line, which
    every damage test reads by id, not the status). Mutant M34 reports the
    link as verified and fails here.

    Negative control: the identical reseal without an edit verifies with the
    link verified by record.
    """
    bundle = _build(_external_source(tmp_path), tmp_path, label="ext",
                    include_external=True)
    report, _ = _verify(_reseal(bundle, tmp_path), tmp_path)
    assert report.status == VERIFIED, report.failed()
    assert "verified by record" in _check(report, "external_link:model.bin")["detail"]

    def retext(record):
        for entry in record["entries"]:
            if entry["path"] == "model.bin":
                entry["link_target"] = "../store/other.bin"
                entry["size"] = len("../store/other.bin")

    case_a = _reseal(bundle, tmp_path, manifest=retext)
    case_b = _reseal(bundle, tmp_path, members=_retouch("model.bin",
                                                        linkname="../store/xxxxx"))
    for damaged in (case_a, case_b):
        report, _ = _verify(damaged, tmp_path)
        assert report.status == UNVERIFIED
        assert not _check(report, "external_link:model.bin")["ok"]
        assert not _check(report, "member_list")["ok"]


def test_mutant_m35(tmp_path):
    """Catches an extracted tree that holds more than the manifest describes
    (M35, the extracted_tree exactness check removed): a hand-made payload
    with the member `d/a.txt` but no member or entry for its directory `d`.
    tarfile creates `d` implicitly, with a mode nobody recorded; only
    `extracted_tree` sees it, every count and digest agrees. Mutant M35
    verifies it and fails here.

    Negative control: the same payload with its `d` member verifies.
    """
    good = _hand_made(tmp_path, [(_info("d", tarfile.DIRTYPE, mode=0o755), None),
                                 (_info("d/a.txt"), b"alpha")])
    assert _verify(good, tmp_path)[0].status == VERIFIED

    implicit = _hand_made(tmp_path, [(_info("d/a.txt"), b"alpha")])
    report, root = _verify(implicit, tmp_path)
    assert report.status == UNVERIFIED
    assert _failed(report) == {"extracted_tree"}
    assert (root / "d").is_dir()


# --------------------------------------------------------------------------- #
# K14 -- a verifier git call that hangs (Rev10b s6)
# --------------------------------------------------------------------------- #

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


def test_verify_git_timeout_unverified(tmp_path, monkeypatch):
    """Catches a verifier git call that hangs verification, or a timeout that
    leaves helpers behind (Rev10b s6): the git on PATH is a wrapper that,
    for `fetch` only, waits on a helper (`sleep 30`) in its own process
    group before running git. With an injected timeout of 2 s the report
    must be ARCHIVE_UNVERIFIED with the `extract` check failed as timed out,
    reached well within a bounded wall time, and no process of the fetch's
    group may survive. Mutants: the timeout not passed (the fetch finishes
    after 30 s, the bundle verifies) and only the direct child killed (the
    helper survives) each fail here.

    Negative control: the same wrapper with the default timeout -- and no
    wait -- verifies the same bundle.
    """
    repo = _repo(tmp_path, "repo")
    bundle = _git_build(repo, tmp_path, ["main", "v1"])
    real = shutil.which("git")
    assert real
    bindir = _fresh(tmp_path, "wrapped-bin")
    pidfile = tmp_path / "fetch-pgids"
    hold = tmp_path / "hold-fetch"
    with open(bindir / "git", "x", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\ncase \" $* \" in *\" fetch \"*)\n"
                     "  if [ -e '%s' ]; then echo $$ >> '%s'; sleep 30 & wait; fi ;;\n"
                     "esac\nexec '%s' \"$@\"\n" % (hold, pidfile, real))
    os.chmod(bindir / "git", 0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))

    report, _ = _verify(bundle, tmp_path)
    assert report.status == VERIFIED, report.failed()

    with open(hold, "x", encoding="ascii"):
        pass
    monkeypatch.setattr(ab, "_git_timeout", 2.0)
    started = time.monotonic()
    report, _ = _verify(bundle, tmp_path)
    elapsed = time.monotonic() - started
    assert report.status == UNVERIFIED
    extract = _check(report, "extract")
    assert not extract["ok"] and "timed out" in extract["detail"], extract
    assert report.extracted is False
    assert elapsed < 20, elapsed
    pgids = [int(line) for line in pidfile.read_text(encoding="ascii").split()]
    assert pgids
    deadline = time.monotonic() + 3
    while any(_group_alive(p) for p in pgids) and time.monotonic() < deadline:
        time.sleep(0.1)
    left = {p: _group_alive(p) for p in pgids if _group_alive(p)}
    for pgid in left:
        try:
            os.killpg(pgid, 9)      # test hygiene for a mutant run
        except OSError:
            pass
    assert not left, "processes of the timed-out fetch's group survived: %r" % left


def test_git_bundle_header_read_without_list_heads(tmp_path):
    """Catches a verifier that trusts git's listing of the bundle's refs
    instead of reading the header itself (Rev10b s3): `_bundle_header`
    parses the v2 signature and every `<oid> <refname>` line of a real
    bundle exactly as `for-each-ref` names them; a thin bundle's
    prerequisite is reported in `git_heads`; a payload whose signature line
    is damaged (resealed) fails `extract` and `git_heads` with "header
    unreadable", and nothing is fetched. Mutant (a header line skipped, or
    prerequisites not reported) fails here.

    Negative control: the intact bundle verifies.
    """
    repo = _repo(tmp_path, "repo")
    bundle = _git_build(repo, tmp_path, ["main", "v1"])
    header = ab._bundle_header(os.fspath(bundle / ab.GIT_PAYLOAD))
    assert header["version"] == 2 and not header["prerequisites"]
    assert sorted(header["heads"]) == sorted(
        (_git(repo, "rev-parse", name), name) for name in ("refs/heads/main", "refs/tags/v1"))
    assert _verify(bundle, tmp_path)[0].status == VERIFIED

    thin = tmp_path / "thin.bundle"
    _git(repo, "bundle", "create", "-q", str(thin), "main~1..main")
    prerequisite = _git(repo, "rev-parse", "main~1")
    assert ab._bundle_header(os.fspath(thin))["prerequisites"] == [prerequisite]
    report, _ = _verify(_git_reseal(bundle, tmp_path, payload=thin.read_bytes()), tmp_path)
    heads = _check(report, "git_heads")
    assert not heads["ok"] and prerequisite in heads["detail"]

    data = (bundle / ab.GIT_PAYLOAD).read_bytes()
    assert data.startswith(b"# v2 git bundle\n")
    damaged = b"# v9 git bundle\n" + data[len(b"# v2 git bundle\n"):]
    report, root = _verify(_git_reseal(bundle, tmp_path, payload=damaged), tmp_path)
    assert report.status == UNVERIFIED
    assert not _check(report, "extract")["ok"]
    assert "header unreadable" in _check(report, "git_heads")["detail"]
    assert not any(p.is_file() for p in (root / "refs").rglob("*"))
