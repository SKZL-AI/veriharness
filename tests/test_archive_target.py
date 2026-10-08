"""P5-27: archive targets, destination-verified placement and redundancy
(`hoh.archive_target`, STORAGE-C2).

Targets are `tmp_path` directories with injected identity and health probes;
no test touches a real Windows volume or calls a Windows binary. Extract roots
come from a real `StorageRegistry` state root whose OS readers are injected
(one live fake pid on a fake boot). Every test names the failure it catches
and the mutant that kills it, and carries a negative control that changes
only the condition under test. Nothing here removes a file: damaged copies
are made by rewriting one byte in place.
"""

from __future__ import annotations

import ast
import fcntl
import itertools
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import hoh.archive_bundle as ab
import hoh.archive_target as at
from hoh.storage_registry import StorageRegistry

ROOT = Path(__file__).resolve().parent.parent
BOOT = "boot-c2"
PROJECT = "proj-c2"
SESSION = "s1"
_COUNTER = itertools.count()


# --------------------------------------------------------------------------- #
# fixtures and helpers (also imported by test_rehydration.py)
# --------------------------------------------------------------------------- #

class FakeOS:
    """Injected registry OS readers: pid 100 is alive on boot `BOOT`."""

    procs = {100: 5000}

    def boot_id(self) -> str:
        return BOOT

    def pid_start(self, pid: int) -> int | None:
        return self.procs.get(pid)

    def pid_exists(self, pid: int) -> bool:
        return pid in self.procs

    def is_mount(self, path) -> bool:
        return False

    def mount_id(self, path) -> int:
        return 1

    def st_dev(self, path) -> int:
        return os.stat(path).st_dev


def git_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(name, None)
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="C2 Test",
               GIT_AUTHOR_EMAIL="c2-test@example.invalid", GIT_COMMITTER_NAME="C2 Test",
               GIT_COMMITTER_EMAIL="c2-test@example.invalid")
    return env


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], env=git_env(),
                          capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """HOME and git configuration confined to `tmp_path`; no test seam left set."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(at, "_after_copy_hook", None)


def new_registry(tmp_path: Path) -> StorageRegistry:
    """An initialised registry with project `PROJECT` and a live session."""
    fake = FakeOS()
    state = tmp_path / "state"
    registry = StorageRegistry(state, forbidden_roots=[tmp_path / "home"],
                               boot_id_reader=fake.boot_id,
                               pid_start_reader=fake.pid_start,
                               pid_exists=fake.pid_exists, mount_check=fake.is_mount,
                               mount_id_reader=fake.mount_id, st_dev_reader=fake.st_dev)
    registry.init()
    project_root = tmp_path / "project-root"
    project_root.mkdir(exist_ok=True)
    registry.register_project(PROJECT, project_root)
    registry.open_session(PROJECT, SESSION, pid=100, boot_id=BOOT)
    return registry


@pytest.fixture
def registry(tmp_path) -> StorageRegistry:
    return new_registry(tmp_path)


class Probe:
    """Injected identity and health probes; tests change them between uses."""

    def __init__(self, identity: dict | None, health: str = "healthy"):
        self.identity = identity
        self.health = health
        self.identity_calls = 0
        self.raise_identity = False

    def read_identity(self, root):
        self.identity_calls += 1
        if self.raise_identity:
            raise OSError("volume not readable")
        return None if self.identity is None else dict(self.identity)

    def read_health(self, root):
        return self.health


def make_target(tmp_path: Path, registry, *, name: str = "t1", domain: str = "d1",
                serial: str = "S1", health: str = "healthy", volume_label: str | None = None,
                probe_identity: dict | None | str = "same") -> tuple[at.ArchiveTarget, Probe]:
    root = tmp_path / ("target-" + name)
    root.mkdir(exist_ok=True)
    expected = {"serial": serial, "label": volume_label or "VH-" + name.upper()}
    identity = dict(expected) if probe_identity == "same" else probe_identity
    probe = Probe(identity, health)
    target = at.ArchiveTarget({
        "target_id": name, "root": root, "failure_domain": domain,
        "expected_identity": expected, "identity_probe": probe.read_identity,
        "health_probe": probe.read_health, "registry": registry, "session_id": SESSION,
    })
    return target, probe


def make_bundle(tmp_path: Path, *, unique: bool | None = True, label: str = "ev",
                files: dict[str, bytes] | None = None) -> Path:
    """A tree bundle of a small source directory; `unique` goes into meta
    (None: the flag is absent)."""
    n = next(_COUNTER)
    source = tmp_path / ("src-%d" % n)
    source.mkdir()
    files = files or {"a.txt": b"alpha\n", "sub/b.bin": b"\x00\x01beta"}
    # every bundle differs in content, so two bundles never share a name
    files = {**files, "id.txt": ("bundle %d of %s\n" % (n, tmp_path.name)).encode()}
    for rel, data in files.items():
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_bytes(data)
    if "a.txt" in files:
        (source / "link").symlink_to("a.txt")
    out = tmp_path / ("bundles-%d" % next(_COUNTER))
    out.mkdir()
    meta = {"run_id": "c2"}
    if unique is not None:
        meta["unique"] = unique
    return ab.ArchiveBundle.build_tree(source, out, label=label, meta=meta,
                                       ownership={"status": "OWNED", "root": str(tmp_path)})


def flip_byte(path: Path, offset: int = -5) -> None:
    """Rewrite one byte of `path` in place (nothing is removed)."""
    with open(path, "r+b") as fh:
        fh.seek(offset, os.SEEK_END if offset < 0 else os.SEEK_SET)
        byte = fh.read(1)
        fh.seek(-1, os.SEEK_CUR)
        fh.write(bytes([byte[0] ^ 0xFF]))


def listing(root: Path) -> list[str]:
    return sorted(os.path.relpath(os.path.join(d, n), root)
                  for d, dirs, files in os.walk(root) for n in dirs + files)


def index_lines(registry) -> list[dict]:
    scratch = registry.scratch_dir(PROJECT, SESSION, "scratch")
    path = at.extract_index_path(scratch)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


ELIGIBLE = {"classification": "VERIFIED_DUPLICATE", "proof_digest": "ab" * 32,
            "unique": False}


# --------------------------------------------------------------------------- #
# K1 -- identity (S13)
# --------------------------------------------------------------------------- #

def test_identity_mismatch_refused(tmp_path, registry):
    """Catches a wrong disk accepted as the target (S13): a probed identity
    that differs from the configured one must make `check()` refuse with
    identity_mismatch and `place()` refuse before anything is written.
    Mutant killed: identity comparison removed from check(). Negative
    control: the same target with the matching identity passes."""
    target, probe = make_target(tmp_path, registry)
    good = target.check()
    assert good.ok and good.reason is None and good.identity_ok

    probe.identity = {"serial": "OTHER", "label": "VH-T1"}
    bad = target.check()
    assert not bad.ok and not bad.identity_ok and bad.identity_readable
    assert bad.reason == at.IDENTITY_MISMATCH
    bundle = make_bundle(tmp_path)
    before = listing(target.root)
    with pytest.raises(at.ArchiveTargetRefused) as info:
        target.place(bundle, PROJECT, at.COLD_STORE)
    assert info.value.reason == at.IDENTITY_MISMATCH
    assert listing(target.root) == before == []


def test_identity_unreadable_refused(tmp_path, registry):
    """Catches an unreadable identity taken as confirmed: a probe answering
    None, or raising, must read as identity_mismatch (never as a match), and
    `place()` must refuse. Mutant killed: a None identity treated as
    matching. Negative control: a readable matching identity passes."""
    target, probe = make_target(tmp_path, registry)
    assert target.check().ok

    probe.identity = None
    none = target.check()
    assert not none.ok and none.reason == at.IDENTITY_MISMATCH
    assert not none.identity_readable and none.identity is None

    probe.identity = {"serial": "S1", "label": "VH-T1"}
    probe.raise_identity = True
    raised = target.check()
    assert not raised.ok and raised.reason == at.IDENTITY_MISMATCH
    assert "volume not readable" in raised.detail
    with pytest.raises(at.ArchiveTargetRefused) as info:
        target.place(make_bundle(tmp_path), PROJECT, at.COLD_STORE)
    assert info.value.reason == at.IDENTITY_MISMATCH
    assert listing(target.root) == []


def test_place_checks_identity_on_use(tmp_path, registry):
    """Catches an identity read once and trusted afterwards: after a first
    successful placement the disk is swapped (the probe now reads another
    serial); the next `place()` must refuse before copying anything. Mutant
    killed: place without check(). Negative control: the first placement,
    with the right disk, verifies."""
    target, probe = make_target(tmp_path, registry)
    first = target.place(make_bundle(tmp_path), PROJECT, at.COLD_STORE)
    assert first.status == ab.ARCHIVED_VERIFIED

    probe.identity = {"serial": "SWAPPED", "label": "VH-T1"}
    second_bundle = make_bundle(tmp_path, label="second")
    calls = probe.identity_calls
    before = listing(target.root)
    with pytest.raises(at.ArchiveTargetRefused) as info:
        target.place(second_bundle, PROJECT, at.COLD_STORE)
    assert info.value.reason == at.IDENTITY_MISMATCH
    assert probe.identity_calls == calls + 1, "check() must run first, on this use"
    assert listing(target.root) == before
    assert not any(second_bundle.name in p for p in listing(target.root))


def test_check_reports_health_and_not_writable(tmp_path, registry):
    """Catches health that is not reported (or not normalised) and a root
    that is not a writable directory accepted for writes. healthy, degraded
    and unknown are reported as such, any other answer reads unknown; a
    missing root is not_writable and check() does not create it. Mutant
    killed: writability check removed. Negative control: an existing root."""
    target, probe = make_target(tmp_path, registry)
    for health in ("healthy", "degraded", "unknown"):
        probe.health = health
        result = target.check()
        assert result.health == health and result.ok
    probe.health = "Warning"
    assert target.check().health == "unknown"

    missing, _ = make_target(tmp_path, registry, name="gone")
    gone_root = missing.root
    os.rename(gone_root, tmp_path / "gone-moved-away")
    result = missing.check()
    assert result.identity_ok and not result.writable and not result.ok
    assert result.reason == at.NOT_WRITABLE
    assert not os.path.lexists(gone_root), "check() created the root"
    with pytest.raises(at.ArchiveTargetRefused) as info:
        missing.place(make_bundle(tmp_path), PROJECT, at.COLD_STORE)
    assert info.value.reason == at.NOT_WRITABLE


def test_area_layout_and_project_id(tmp_path, registry):
    """Catches areas outside `<root>/<kind>/<project_id>/` and unchecked
    project ids or kinds. Mutant killed: the project-id pattern removed.
    Negative control: a valid id and both kinds."""
    target, _ = make_target(tmp_path, registry)
    for kind in (at.COLD_STORE, at.RECLAIM_REVIEW):
        assert target.area("p-1.x_y", kind) == target.root / kind / "p-1.x_y"
        assert (target.root / kind / "p-1.x_y").is_dir()
    for bad in ("", "UPPER", "../x", "a/b", "-lead", "x" * 65):
        with pytest.raises(at.ArchiveTargetRefused):
            target.area(bad, at.COLD_STORE)
    with pytest.raises(at.ArchiveTargetRefused):
        target.area("p", "hot-store")


# --------------------------------------------------------------------------- #
# K2 -- placing verifies on the destination
# --------------------------------------------------------------------------- #

def test_place_verifies_on_destination(tmp_path, registry, monkeypatch):
    """Catches a receipt written without a verification of the destination
    copy (e.g. the source verified instead): verify_archive must run on the
    destination, in a registered place-verify extract root, before the receipt
    is written, and the receipt must name the destination. Mutant killed:
    receipt without destination verification. Negative control: the source
    bundle is never the verified path."""
    target, _ = make_target(tmp_path, registry)
    bundle = make_bundle(tmp_path)
    calls = []
    real_verify, real_receipt = ab.verify_archive, ab.write_receipt

    def spy_verify(bundle_dir, *, extract_root):
        calls.append(("verify", Path(bundle_dir), Path(extract_root)))
        return real_verify(bundle_dir, extract_root=extract_root)

    def spy_receipt(bundle_dir, *, extract_root, destination):
        calls.append(("receipt", Path(bundle_dir), Path(extract_root)))
        return real_receipt(bundle_dir, extract_root=extract_root, destination=destination)

    monkeypatch.setattr(ab, "verify_archive", spy_verify)
    monkeypatch.setattr(ab, "write_receipt", spy_receipt)
    result = target.place(bundle, PROJECT, at.COLD_STORE)

    dest = target.root / at.COLD_STORE / PROJECT / bundle.name
    assert result.status == ab.ARCHIVED_VERIFIED and result.path == dest
    assert calls[0][0] == "verify" and calls[0][1] == dest
    assert calls[0][2].parent.name.startswith("place-verify-")
    assert calls[1][0] == "receipt" and calls[1][1] == dest
    assert all(c[1] != bundle for c in calls), "the source must not stand in for the copy"
    receipt = json.loads((dest / ab.RECEIPT).read_text())
    assert receipt["bundle_realpath"] == os.path.realpath(dest)
    assert receipt["status"] == ab.ARCHIVED_VERIFIED
    assert receipt["destination"]["target_id"] == "t1"
    assert sorted(os.listdir(dest)) == sorted(
        [ab.TREE_PAYLOAD, ab.MANIFEST, ab.SUMS, ab.RECEIPT])
    for name in (ab.TREE_PAYLOAD, ab.MANIFEST, ab.SUMS):
        assert (dest / name).read_bytes() == (bundle / name).read_bytes()


def test_place_damaged_destination_unverified_partial_kept(tmp_path, registry):
    """Catches a copy that becomes verified without a readback, and a failed
    copy that is removed: one byte flipped at the destination after the copy
    and before verification gives ARCHIVE_UNVERIFIED, no receipt, no
    COPY_VERIFIED, and the copied files kept under `<name>.partial`. Mutant
    killed: status taken from the copy step instead of the verification.
    Negative control: the same placement without the flip verifies."""
    target, _ = make_target(tmp_path, registry)
    ledger = at.RedundancyLedger(tmp_path / "ledger.jsonl")
    bundle = make_bundle(tmp_path)
    seen = []

    def damage(*, destination):
        seen.append(destination)
        flip_byte(destination / ab.TREE_PAYLOAD)

    at._after_copy_hook = damage
    result = target.place(bundle, PROJECT, at.COLD_STORE, ledger=ledger)
    area = target.root / at.COLD_STORE / PROJECT
    assert seen == [area / bundle.name]
    assert result.status == ab.ARCHIVE_UNVERIFIED
    assert result.path == area / (bundle.name + ".partial")
    assert not os.path.lexists(area / bundle.name)
    assert sorted(os.listdir(result.path)) == sorted([ab.TREE_PAYLOAD, ab.MANIFEST, ab.SUMS])
    assert (result.path / ab.MANIFEST).read_bytes() == (bundle / ab.MANIFEST).read_bytes()
    assert result.receipt_path is None and not (result.path / ab.RECEIPT).exists()
    assert [r["event"] for r in ledger.records()] == [at.COPY_WRITTEN]
    assert ledger.status(result.bundle_id).verified == []

    at._after_copy_hook = None
    control = target.place(make_bundle(tmp_path, label="ctl"), PROJECT, at.COLD_STORE,
                           ledger=ledger)
    assert control.status == ab.ARCHIVED_VERIFIED


def test_place_retry_versions_partial(tmp_path, registry):
    """Catches a leftover .partial that is overwritten or removed on retry:
    the retry renames it to `<name>.partial.v<N>.<UTC>` with its files intact
    and places afresh; a second failed retry gets v2. Mutant killed: retry
    without versioning (the rename onto the existing .partial fails or
    replaces it). Negative control: a verified destination refuses a retry."""
    target, _ = make_target(tmp_path, registry)
    bundle = make_bundle(tmp_path)
    area = target.root / at.COLD_STORE / PROJECT
    at._after_copy_hook = lambda *, destination: flip_byte(destination / ab.TREE_PAYLOAD)
    first = target.place(bundle, PROJECT, at.COLD_STORE)
    damaged = (first.path / ab.TREE_PAYLOAD).read_bytes()
    second = target.place(bundle, PROJECT, at.COLD_STORE)
    assert first.status == second.status == ab.ARCHIVE_UNVERIFIED
    v1 = second.versioned_partial
    assert re.fullmatch(re.escape(bundle.name) + r"\.partial\.v1\.\d{8}T\d{6}\d+Z", v1.name)
    assert (v1 / ab.TREE_PAYLOAD).read_bytes() == damaged

    at._after_copy_hook = None
    third = target.place(bundle, PROJECT, at.COLD_STORE)
    assert third.status == ab.ARCHIVED_VERIFIED and third.path == area / bundle.name
    assert third.versioned_partial.name.startswith(bundle.name + ".partial.v2.")
    assert sorted(n for n in os.listdir(area) if ".partial" in n) == sorted(
        [v1.name, third.versioned_partial.name])
    receipt = (third.path / ab.RECEIPT).read_bytes()
    with pytest.raises(at.ArchiveTargetRefused) as info:
        target.place(bundle, PROJECT, at.COLD_STORE)
    assert info.value.reason == "destination_exists"
    assert (third.path / ab.RECEIPT).read_bytes() == receipt


def test_place_copy_failure_partial_kept(tmp_path, registry, monkeypatch):
    """Catches a copy step that fails outside the failure handling: when the
    second fixed file cannot be written (OSError ENOSPC, after its temporary
    file was started) place() must not raise; it returns ARCHIVE_UNVERIFIED,
    keeps the half-copied destination as <name>.partial (the leftover
    temporary file inside it), writes no receipt and no COPY_VERIFIED, and a
    retry versions that .partial and verifies instead of being refused with
    destination_exists. Mutant killed: copy loop outside the try (as in the
    iteration-1 candidate). Negative control: the retry without the fault."""
    import errno
    target, _ = make_target(tmp_path, registry)
    ledger = at.RedundancyLedger(tmp_path / "ledger.jsonl")
    bundle = make_bundle(tmp_path)
    area = target.root / at.COLD_STORE / PROJECT
    real_copy = at._copy_file
    calls = []

    def failing_copy(src, dest_dir, name):
        calls.append(name)
        if len(calls) == 2:
            (dest_dir / f".{name}.0123456789ab.tmp").write_bytes(b"half")
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), str(dest_dir / name))
        return real_copy(src, dest_dir, name)

    monkeypatch.setattr(at, "_copy_file", failing_copy)
    result = target.place(bundle, PROJECT, at.COLD_STORE, ledger=ledger)
    assert len(calls) == 2
    assert result.status == ab.ARCHIVE_UNVERIFIED
    assert result.path == area / (bundle.name + ".partial")
    assert result.receipt_path is None and result.receipt_digest is None
    assert "copy failed" in result.detail and "No space" in result.detail
    assert result.extract_roots == []
    assert not os.path.lexists(area / bundle.name)
    kept = sorted(os.listdir(result.path))
    assert kept == sorted([calls[0], f".{calls[1]}.0123456789ab.tmp"])
    assert (result.path / calls[0]).read_bytes() == (bundle / calls[0]).read_bytes()
    assert not (result.path / ab.RECEIPT).exists()
    assert at.COPY_VERIFIED not in [r["event"] for r in ledger.records()]

    monkeypatch.setattr(at, "_copy_file", real_copy)
    retry = target.place(bundle, PROJECT, at.COLD_STORE, ledger=ledger)
    assert retry.status == ab.ARCHIVED_VERIFIED and retry.path == area / bundle.name
    assert re.fullmatch(re.escape(bundle.name) + r"\.partial\.v1\.\d{8}T\d{6}\d+Z",
                        retry.versioned_partial.name)
    assert sorted(os.listdir(retry.versioned_partial)) == kept
    assert ledger.status(bundle.name).verified[0]["target_id"] == "t1"


def test_extract_roots_registered_and_marked(tmp_path, registry):
    """Catches extract roots created outside the storage registry or left
    without their marker: every root of place/write_receipt/scrub lies in the
    session's registered scratch, the registry answers OWNED for it, and it
    holds EXTRACT_ROOT.json beside tree/. Mutant killed: an extract root
    outside the registry (e.g. a tempfile directory). Negative control: the
    registry answers UNKNOWN for a directory it did not create."""
    target, _ = make_target(tmp_path, registry)
    ledger = at.RedundancyLedger(tmp_path / "ledger.jsonl")
    bundle = make_bundle(tmp_path)
    placed = target.place(bundle, PROJECT, at.COLD_STORE, ledger=ledger)
    scrubbed = at.scrub(target, bundle.name, project_id=PROJECT, ledger=ledger)
    roots = placed.extract_roots + [scrubbed.extract_root]
    assert len(roots) == 3
    scratch = registry.scratch_dir(PROJECT, SESSION, "scratch")
    for root, op in zip(roots, ("place-verify", "place-receipt", "scrub")):
        assert root.parent == scratch and root.name.startswith(op + "-")
        owner = registry.owner_of(root)
        assert owner.status == "OWNED" and owner.session_id == SESSION, owner
        assert (root / "tree").is_dir()
        assert sorted(os.listdir(root)) == ["EXTRACT_ROOT.json", "tree"]
        marker = json.loads((root / "EXTRACT_ROOT.json").read_text())
        assert marker["bundle_id"] == bundle.name
        assert marker["payload_sha256"] == json.loads(
            (bundle / ab.MANIFEST).read_text())["payload_sha256"]
        assert marker["verification_status"] == ab.ARCHIVED_VERIFIED
        assert marker["receipt"]["digest"] == placed.receipt_digest
        assert marker["classification"] == "REPRODUCIBLE_SCRATCH"
    foreign = tmp_path / "foreign-extract"
    foreign.mkdir()
    assert registry.owner_of(foreign).status != "OWNED"


def _calls(tree: ast.AST) -> set[str]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            out.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    return out


REMOVALS = {"rmtree", "unlink", "remove", "rmdir", "removedirs", "truncate", "ftruncate"}


def test_no_removal_call_in_either_module():
    """Catches a removal path in STORAGE-C2 (Revision 3 item 2 withdrew the
    only one): neither archive_target.py nor rehydration.py may call any
    removal or truncation function, and `_remove_own_extract_root` must not
    exist. Mutant killed: any added unlink/rmtree/rmdir. Negative control:
    the same scan finds the removal in durable_io.py's `_abandon_own_temp`."""
    for name in ("archive_target.py", "rehydration.py"):
        source = (ROOT / "src" / "hoh" / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert not (_calls(tree) & REMOVALS), (name, _calls(tree) & REMOVALS)
        assert "_remove_own_extract_root" not in source
    control = ast.parse((ROOT / "src" / "hoh" / "durable_io.py").read_text(encoding="utf-8"))
    assert "unlink" in _calls(control)


# --------------------------------------------------------------------------- #
# K3 -- reclaim-review gate and redundancy
# --------------------------------------------------------------------------- #

def test_reclaim_review_requires_eligibility(tmp_path, registry):
    """Catches reclaim-review entered without a consistent eligibility
    record: missing, a wrong classification, an empty proof_digest, or
    `unique` not false are each refused before anything is copied. Mutant
    killed: no gate. Negative control: a complete record with a non-unique
    bundle is placed and verified."""
    target, _ = make_target(tmp_path, registry)
    bundle = make_bundle(tmp_path, unique=False)
    bad_records = [None, {},
                   {**ELIGIBLE, "classification": "UNIQUE_EVIDENCE"},
                   {**ELIGIBLE, "proof_digest": ""},
                   {**ELIGIBLE, "proof_digest": "   "},
                   {**ELIGIBLE, "unique": True},
                   {k: v for k, v in ELIGIBLE.items() if k != "unique"}]
    for record in bad_records:
        with pytest.raises(at.ArchiveTargetRefused) as info:
            target.place(bundle, PROJECT, at.RECLAIM_REVIEW, record)
        assert info.value.reason == "not_eligible", record
        assert not (target.root / at.RECLAIM_REVIEW).exists(), "copied before the gate"
    for classification in sorted(at.RECLAIM_CLASSIFICATIONS):
        ok = target.place(make_bundle(tmp_path, unique=False, label="ok"), PROJECT,
                          at.RECLAIM_REVIEW, {**ELIGIBLE, "classification": classification})
        assert ok.status == ab.ARCHIVED_VERIFIED


def test_reclaim_review_refuses_unique_bundle(tmp_path, registry):
    """Catches a unique bundle sent to reclaim-review on the strength of the
    caller's record alone: the bundle's manifest meta must say unique: false;
    an absent flag reads unique (fail closed) and is refused like true.
    Mutant killed: gate on the eligibility record only. Negative control: the
    same eligibility with a `unique: false` bundle passes, and a unique
    bundle may still go to cold-store."""
    target, _ = make_target(tmp_path, registry)
    for unique in (True, None):
        with pytest.raises(at.ArchiveTargetRefused) as info:
            target.place(make_bundle(tmp_path, unique=unique), PROJECT, at.RECLAIM_REVIEW,
                         ELIGIBLE)
        assert info.value.reason == "not_eligible"
        assert "manifest meta" in str(info.value)
    assert not (target.root / at.RECLAIM_REVIEW).exists()
    assert target.place(make_bundle(tmp_path, unique=False), PROJECT, at.RECLAIM_REVIEW,
                        ELIGIBLE).status == ab.ARCHIVED_VERIFIED
    assert target.place(make_bundle(tmp_path, unique=True), PROJECT,
                        at.COLD_STORE).status == ab.ARCHIVED_VERIFIED


def test_redundancy_needs_distinct_identities_and_healthy(tmp_path, registry):
    """Catches redundancy counted by labels alone or on unhealthy targets:
    two healthy copies with different failure-domain labels on the **same**
    probed volume identity do not satisfy; two copies with different
    identities but the same label do not; a degraded second copy does not;
    a copy on a different label and identity, healthy, does. Mutant killed:
    label-only domains. Negative control: the satisfying pair."""
    a, _ = make_target(tmp_path, registry, name="ta", domain="d1", serial="S1")
    # tb: another failure-domain label, but configured on the very same volume
    # as ta (two directories on one disk) -- the probe reads ta's identity.
    b, _ = make_target(tmp_path, registry, name="tb", domain="d2", serial="S1",
                       volume_label="VH-TA")
    # tc: another volume, but the same failure-domain label as ta
    c, _ = make_target(tmp_path, registry, name="tc", domain="d1", serial="S3")
    d, probe_d = make_target(tmp_path, registry, name="td", domain="d4", serial="S4",
                             health="degraded")
    assert a.check().identity == b.check().identity != c.check().identity

    one = at.RedundancyLedger(tmp_path / "ledger-same-volume.jsonl")
    bundle = make_bundle(tmp_path)
    for target in (a, b):
        assert target.place(bundle, PROJECT, at.COLD_STORE,
                            ledger=one).status == ab.ARCHIVED_VERIFIED
    # health and identity are read now through `targets` (Revision 8a item 1)
    status = one.status(bundle.name, targets=[a, b])
    assert status.status == at.REDUNDANCY_NOT_SATISFIED, status
    assert len(status.counting) == 2 and status.chosen == []

    two = at.RedundancyLedger(tmp_path / "ledger-same-label.jsonl")
    second = make_bundle(tmp_path, label="second")
    for target in (a, c, d):
        assert target.place(second, PROJECT, at.COLD_STORE,
                            ledger=two).status == ab.ARCHIVED_VERIFIED
    degraded = two.status(second.name, targets=[a, c, d])
    assert degraded.status == at.REDUNDANCY_NOT_SATISFIED, "ta, tc share d1; td degraded"
    assert {v["target_id"] for v in degraded.verified} == {"ta", "tc", "td"}
    assert {v["target_id"] for v in degraded.counting} == {"ta", "tc"}

    # the only change: td reports healthy now
    probe_d.health = "healthy"
    satisfied = two.status(second.name, targets=[a, c, d])
    assert satisfied.status == at.REDUNDANCY_SATISFIED
    chosen = {x["target_id"] for x in satisfied.chosen}
    assert "td" in chosen and len(chosen) == 2
    assert two.status(second.name, targets=[a, c, d],
                      required_copies=3).status == at.REDUNDANCY_NOT_SATISFIED


def test_only_copy_on_degraded_or_unknown_reported(tmp_path, registry):
    """Catches a unique bundle whose only verified copies sit on degraded or
    unknown targets passing unnoticed: it is reported
    ONLY_COPY_ON_DEGRADED_TARGET for degraded and for unknown health alike.
    Mutant killed: the report limited to `degraded`. Negative controls: the
    same placement on a healthy target, and a non-unique bundle on a degraded
    one, are not reported so."""
    for health in ("degraded", "unknown"):
        ledger = at.RedundancyLedger(tmp_path / f"ledger-{health}.jsonl")
        target, _ = make_target(tmp_path, registry, name="x" + health[:3], health=health)
        unique = make_bundle(tmp_path, unique=True)
        assert target.place(unique, PROJECT, at.COLD_STORE,
                            ledger=ledger).status == ab.ARCHIVED_VERIFIED
        status = ledger.status(unique.name, targets=[target])
        assert status.status == at.ONLY_COPY_ON_DEGRADED_TARGET, (health, status)
        assert status.unique and len(status.verified) == 1 and status.counting == []

        shared = make_bundle(tmp_path, unique=False)
        target.place(shared, PROJECT, at.COLD_STORE, ledger=ledger)
        assert ledger.status(shared.name,
                             targets=[target]).status == at.REDUNDANCY_NOT_SATISFIED

    ledger = at.RedundancyLedger(tmp_path / "ledger-healthy.jsonl")
    healthy, _ = make_target(tmp_path, registry, name="xhealthy")
    bundle = make_bundle(tmp_path, unique=True)
    healthy.place(bundle, PROJECT, at.COLD_STORE, ledger=ledger)
    assert ledger.status(bundle.name,
                         targets=[healthy]).status == at.REDUNDANCY_NOT_SATISFIED


def test_redundancy_ledger_states_stale_and_overdue(tmp_path, registry):
    """Catches a stale copy still counted: COPY_STALE after COPY_VERIFIED
    removes the copy from the verified set, a verified record older than
    max_age is stale (an overdue scrub), and a target whose identity is not
    confirmed now does not count; COPY_VERIFIED is refused for an
    unconfirmed identity. Mutant killed: the last-record rule replaced by
    any-verified. Negative control: two fresh verified copies satisfy."""
    import datetime
    ledger = at.RedundancyLedger(tmp_path / "ledger.jsonl")
    bundle = make_bundle(tmp_path)
    a, probe_a = make_target(tmp_path, registry, name="ta", domain="d1", serial="S1")
    b, _ = make_target(tmp_path, registry, name="tb", domain="d2", serial="S2")
    for target in (a, b):
        target.place(bundle, PROJECT, at.COLD_STORE, ledger=ledger)
    assert ledger.status(bundle.name, targets=[a, b]).status == at.REDUNDANCY_SATISFIED
    later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=40)
    assert ledger.status(bundle.name, targets=[a, b], max_age=datetime.timedelta(days=30),
                         now=later).status == at.REDUNDANCY_NOT_SATISFIED
    probe_a.identity = {"serial": "WRONG"}
    assert ledger.status(bundle.name, targets=[a, b]).status == at.REDUNDANCY_NOT_SATISFIED
    with pytest.raises(at.ArchiveTargetRefused):
        ledger.append(at.COPY_VERIFIED, bundle_id=bundle.name, target=a, check=a.check(),
                      receipt_digest="2" * 64)
    probe_a.identity = {"serial": "S1", "label": "VH-TA"}
    ledger.append(at.COPY_STALE, bundle_id=bundle.name, target=a, check=a.check(),
                  reason="scrub overdue")
    status = ledger.status(bundle.name, targets=[a, b])
    assert status.status == at.REDUNDANCY_NOT_SATISFIED
    assert [v["target_id"] for v in status.verified] == ["tb"]


def test_scrub_pass_and_fail(tmp_path, registry):
    """Catches a scrub that does not re-read the copy: a clean copy appends
    COPY_VERIFIED with the receipt digest; after one byte of the placed
    payload is flipped, the next scrub appends COPY_STALE; an identity that
    is not confirmed appends COPY_STALE without reading and the scrub is
    refused (identity_mismatch). Mutant killed: scrub trusting the earlier
    receipt. Negative control: the first scrub."""
    ledger = at.RedundancyLedger(tmp_path / "ledger.jsonl")
    target, probe = make_target(tmp_path, registry)
    bundle = make_bundle(tmp_path)
    placed = target.place(bundle, PROJECT, at.COLD_STORE, ledger=ledger)
    ok = at.scrub(target, bundle.name, project_id=PROJECT, ledger=ledger)
    assert ok.event == at.COPY_VERIFIED and ok.receipt_digest == placed.receipt_digest
    flip_byte(placed.path / ab.TREE_PAYLOAD)
    bad = at.scrub(target, bundle.name, project_id=PROJECT, ledger=ledger)
    assert bad.event == at.COPY_STALE and bad.report.status == ab.ARCHIVE_UNVERIFIED
    flip_byte(placed.path / ab.TREE_PAYLOAD)          # restore the byte
    probe.identity = {"serial": "OTHER"}
    with pytest.raises(at.ArchiveTargetRefused) as info:
        at.scrub(target, bundle.name, project_id=PROJECT, ledger=ledger)
    assert info.value.reason == at.IDENTITY_MISMATCH
    events = ledger.records(bundle.name)
    assert [r["event"] for r in events] == [
        at.COPY_WRITTEN, at.COPY_VERIFIED, at.COPY_VERIFIED, at.COPY_STALE, at.COPY_STALE]
    assert events[-1]["reason"].startswith(at.IDENTITY_MISMATCH)
    assert ledger.status(bundle.name).verified == []


def test_scrub_identity_mismatch_refused(tmp_path, registry, monkeypatch):
    """Catches a scrub that only records an unconfirmed identity and returns
    as if it had run (Rev2 item 5 says refuse): on a mismatching probe the
    scrub appends COPY_STALE with reason identity_mismatch, opens no extract
    root, never reads the copy (verify_archive is not called) and raises
    ArchiveTargetRefused(reason='identity_mismatch'). Mutant killed: scrub
    returning a result on a mismatch. Negative control: the same scrub with
    the confirmed identity returns COPY_VERIFIED."""
    ledger = at.RedundancyLedger(tmp_path / "ledger.jsonl")
    target, probe = make_target(tmp_path, registry)
    bundle = make_bundle(tmp_path)
    target.place(bundle, PROJECT, at.COLD_STORE, ledger=ledger)
    assert at.scrub(target, bundle.name, project_id=PROJECT,
                    ledger=ledger).event == at.COPY_VERIFIED

    reads = []
    real_verify = ab.verify_archive

    def spy(bundle_dir, *, extract_root):
        reads.append(Path(bundle_dir))
        return real_verify(bundle_dir, extract_root=extract_root)

    monkeypatch.setattr(ab, "verify_archive", spy)
    scratch = registry.scratch_dir(PROJECT, SESSION, "scratch")
    before_roots = sorted(os.listdir(scratch))
    before_index = index_lines(registry)
    for unconfirmed in ({"serial": "OTHER", "label": "VH-T1"}, None):
        probe.identity = unconfirmed
        with pytest.raises(at.ArchiveTargetRefused) as info:
            at.scrub(target, bundle.name, project_id=PROJECT, ledger=ledger)
        assert info.value.reason == at.IDENTITY_MISMATCH
        assert info.value.check is not None and not info.value.check.identity_ok
        last = ledger.records(bundle.name)[-1]
        assert last["event"] == at.COPY_STALE
        assert last["reason"].startswith(at.IDENTITY_MISMATCH)
    assert reads == [], "the copy must not be read on an unconfirmed identity"
    assert sorted(os.listdir(scratch)) == before_roots
    assert index_lines(registry) == before_index
    assert ledger.status(bundle.name).verified == []


# --------------------------------------------------------------------------- #
# K7 -- the extract-root index (Revision 7)
# --------------------------------------------------------------------------- #

def test_extract_roots_indexed_under_lock(tmp_path, registry, monkeypatch):
    """Catches extract roots that are not indexed, or indexed outside C2's
    lock, or only after extraction: every root of place/write_receipt (and of
    a failed place) gets a PENDING line written while the flock on
    EXTRACT_ROOTS.jsonl.lock is held and before the root exists, then a
    VERIFIED line with the receipt digest or a FAILED line. Mutant killed:
    index not written. Negative control: a directory the module did not
    create is in no index line."""
    target, _ = make_target(tmp_path, registry)
    held: dict[int, str] = {}
    events = []
    real_flock, real_append = fcntl.flock, at._append_line

    def spy_flock(fd, op):
        name = os.readlink(f"/proc/self/fd/{fd}")
        if op & fcntl.LOCK_UN:
            held.pop(fd, None)
        real_flock(fd, op)
        if op & fcntl.LOCK_EX:
            held[fd] = name

    def spy_append(path, record):
        if Path(path).name == at.EXTRACT_INDEX:
            lock = os.path.realpath(Path(path).with_name(at.EXTRACT_INDEX_LOCK))
            events.append((record["outcome"], record["path"],
                           lock in {os.path.realpath(v) for v in held.values()},
                           os.path.lexists(record["path"])))
        return real_append(path, record)

    monkeypatch.setattr(at.fcntl, "flock", spy_flock)
    monkeypatch.setattr(at, "_append_line", spy_append)
    ok = target.place(make_bundle(tmp_path), PROJECT, at.COLD_STORE)
    at._after_copy_hook = lambda *, destination: flip_byte(destination / ab.TREE_PAYLOAD)
    bad = target.place(make_bundle(tmp_path, label="bad"), PROJECT, at.COLD_STORE)
    assert ok.status == ab.ARCHIVED_VERIFIED and bad.status == ab.ARCHIVE_UNVERIFIED

    assert all(locked for _, _, locked, _ in events), events
    roots = [str(p) for p in ok.extract_roots + bad.extract_roots]
    assert len(roots) == 3
    for root in roots:
        mine = [e for e in events if e[1] == root]
        assert mine, f"{root} was never indexed"
        assert mine[0][0] == at.EXTRACT_PENDING
        assert mine[0][3] is False, "PENDING must be indexed before the root exists"
    lines = index_lines(registry)
    by_path = {}
    for line in lines:
        by_path.setdefault(line["path"], []).append(line)
    for root in [str(p) for p in ok.extract_roots]:
        assert [l["outcome"] for l in by_path[root]] == ["PENDING", "VERIFIED"]
        assert by_path[root][1]["receipt_digest"] == ok.receipt_digest
        assert "receipt_digest" not in by_path[root][0]
    assert [l["outcome"] for l in by_path[str(bad.extract_roots[0])]] == ["PENDING", "FAILED"]
    scratch = registry.scratch_dir(PROJECT, SESSION, "scratch")
    assert all(Path(l["path"]).parent == scratch for l in lines)
    assert (scratch / "archive-target" / at.EXTRACT_INDEX_LOCK).exists()
    assert str(tmp_path / "foreign") not in by_path


def test_rehydration_extract_roots_indexed(tmp_path, registry):
    """Catches rehydration roots missing from C2's index: a successful
    rehydration appends PENDING then VERIFIED (with the committed receipt's
    file digest), a failed one PENDING then FAILED, in the same per-session
    EXTRACT_ROOTS.jsonl as placement. Mutant killed: rehydrate does not
    append. Negative control: the lines placement wrote are still there."""
    from test_rehydration import commit_receipt, make_repo

    import hoh.rehydration as rh

    target, _ = make_target(tmp_path, registry)
    repo = make_repo(tmp_path / "repo")
    good = target.place(make_bundle(tmp_path), PROJECT, at.COLD_STORE)
    other = target.place(make_bundle(tmp_path, label="other",
                                     files={"z.txt": b"zzz\n"}), PROJECT, at.COLD_STORE)
    receipt = commit_receipt(repo, good.receipt_path)
    placed_lines = index_lines(registry)
    out = rh.rehydrate(receipt, good.path, repo, state_root=registry.state_root,
                       project_id=PROJECT, session_id=SESSION, registry=registry)
    assert out.is_dir()
    with pytest.raises(rh.RehydrationRefused):
        rh.rehydrate(receipt, other.path, repo, state_root=registry.state_root,
                     project_id=PROJECT, session_id=SESSION, registry=registry)
    lines = index_lines(registry)
    assert lines[:len(placed_lines)] == placed_lines
    new = [l for l in lines[len(placed_lines):]]
    assert [(l["op"], l["outcome"]) for l in new] == [
        ("rehydrate", "PENDING"), ("rehydrate", "VERIFIED"),
        ("rehydrate", "PENDING"), ("rehydrate", "FAILED")]
    assert new[0]["path"] == new[1]["path"] != new[2]["path"] == new[3]["path"]
    import hashlib
    assert new[1]["receipt_digest"] == hashlib.sha256(receipt.read_bytes()).hexdigest()
    for line in new:
        assert Path(line["path"]).parent == registry.scratch_dir(PROJECT, SESSION, "scratch")


# --------------------------------------------------------------------------- #
# K12 -- Revision 8: fresh health, unconfirmed identity, mutant survivors
# --------------------------------------------------------------------------- #

def test_redundancy_uses_fresh_health(tmp_path, registry):
    """Catches redundancy judged on the health recorded at COPY_VERIFIED time
    (starget-a M1, fails open): a unique bundle is verified on two healthy
    targets in distinct failure domains, then target tb turns degraded.
    Called without `targets`, or with a partial list, status() has no fresh
    reading for the left-out target, counts it as unknown and is not
    REDUNDANCY_SATISFIED; without `targets` the unique bundle is reported
    ONLY_COPY_ON_DEGRADED_TARGET. With every target passed, tb's degraded
    health read now keeps it from counting. Mutant killed: recorded health
    used. Negative control: all targets passed and healthy now gives
    REDUNDANCY_SATISFIED."""
    ledger = at.RedundancyLedger(tmp_path / "ledger.jsonl")
    bundle = make_bundle(tmp_path, unique=True)
    a, _ = make_target(tmp_path, registry, name="ta", domain="d1", serial="S1")
    b, probe_b = make_target(tmp_path, registry, name="tb", domain="d2", serial="S2")
    for target in (a, b):
        assert target.place(bundle, PROJECT, at.COLD_STORE,
                            ledger=ledger).status == ab.ARCHIVED_VERIFIED
    recorded = [r["health"] for r in ledger.records(bundle.name)
                if r["event"] == at.COPY_VERIFIED]
    assert recorded == ["healthy", "healthy"]
    control = ledger.status(bundle.name, targets=[a, b])
    assert control.status == at.REDUNDANCY_SATISFIED, control

    probe_b.health = "degraded"          # the only change: tb degrades after the copy
    none = ledger.status(bundle.name)
    assert none.status == at.ONLY_COPY_ON_DEGRADED_TARGET, none
    assert none.counting == [] and none.chosen == []
    assert {v["target_id"] for v in none.verified} == {"ta", "tb"}
    assert all(v["health"] == at.UNKNOWN and v["recorded_health"] == "healthy"
               and v["standing"] == at.STANDING_NO_READING for v in none.verified)
    assert "tb: " + at.STANDING_NO_READING in none.detail
    assert ledger.status(bundle.name, required_copies=1).status \
        == at.ONLY_COPY_ON_DEGRADED_TARGET

    partial = ledger.status(bundle.name, targets=[a])
    assert partial.status == at.REDUNDANCY_NOT_SATISFIED, partial
    assert [c["target_id"] for c in partial.counting] == ["ta"]
    left_out = {v["target_id"]: v for v in partial.verified}["tb"]
    assert left_out["health"] == at.UNKNOWN and left_out["standing"] == at.STANDING_NO_READING

    only_b = ledger.status(bundle.name, targets=[b])
    assert only_b.status == at.ONLY_COPY_ON_DEGRADED_TARGET, only_b
    assert "tb: degraded health" in only_b.detail
    assert "ta: " + at.STANDING_NO_READING in only_b.detail

    full = ledger.status(bundle.name, targets=[a, b])
    assert full.status == at.REDUNDANCY_NOT_SATISFIED
    assert [c["target_id"] for c in full.counting] == ["ta"]

    probe_b.health = "healthy"
    assert ledger.status(bundle.name, targets=[a, b]).status == at.REDUNDANCY_SATISFIED


def test_unconfirmed_target_identity_is_unknown(tmp_path, registry):
    """Catches a copy on a target whose identity is not confirmed now (an
    unmounted or swapped volume) silently dropped, which hides that a unique
    bundle has no confirmed healthy copy: the copy stays in `verified`, does
    not count, the status is the one constant ONLY_COPY_ON_DEGRADED_TARGET
    (there is no ONLY_COPY_ON_UNKNOWN_TARGET, 8c item 2), and the detail
    names the unconfirmed identity -- for a probe that reads None, a
    mismatch and a probe that raises. Mutant killed: unconfirmed target
    dropped. Negative control: the same target with its identity confirmed
    and healthy counts (and satisfies required_copies=1)."""
    ledger = at.RedundancyLedger(tmp_path / "ledger.jsonl")
    target, probe = make_target(tmp_path, registry)
    expected = dict(probe.identity)
    bundle = make_bundle(tmp_path, unique=True)
    assert target.place(bundle, PROJECT, at.COLD_STORE,
                        ledger=ledger).status == ab.ARCHIVED_VERIFIED
    control = ledger.status(bundle.name, targets=[target], required_copies=1)
    assert control.status == at.REDUNDANCY_SATISFIED, control

    def unreadable():
        probe.identity, probe.raise_identity = dict(expected), True

    for change in (lambda: setattr(probe, "identity", None),
                   lambda: setattr(probe, "identity", {"serial": "OTHER", "label": "VH-T1"}),
                   unreadable):
        change()
        for required in (1, 2):
            status = ledger.status(bundle.name, targets=[target], required_copies=required)
            assert [v["target_id"] for v in status.verified] == ["t1"], status
            assert status.counting == [] and status.chosen == []
            assert status.status == at.ONLY_COPY_ON_DEGRADED_TARGET, status
            assert status.verified[0]["standing"] == at.STANDING_UNCONFIRMED
            assert status.verified[0]["health"] == at.UNKNOWN
            assert "t1: unconfirmed identity" in status.detail
        probe.identity, probe.raise_identity = dict(expected), False
    assert not hasattr(at, "ONLY_COPY_ON_UNKNOWN_TARGET")
    again = ledger.status(bundle.name, targets=[target], required_copies=1)
    assert again.status == at.REDUNDANCY_SATISFIED
    assert again.verified[0]["standing"] == at.STANDING_HEALTHY


def test_mutant_index_path_rule(tmp_path, registry):
    """Catches an extract-root index line for a path that does not lie
    directly under the indexing session's own scratch (Revision 7 item 5:
    such a line is invalid, so STORAGE-D could be pointed at a foreign
    directory): `_index_append` refuses it with reason
    extract_root_outside_scratch and appends nothing to EXTRACT_ROOTS.jsonl
    -- for a path elsewhere, one nested a level deeper, the scratch itself,
    a `..` spelling and another session's scratch. Mutant killed:
    index_path_check_removed. Negative control: a root directly under the
    session's scratch is appended."""
    scratch = registry.scratch_dir(PROJECT, SESSION, "scratch")
    index = at.extract_index_path(scratch)
    good = scratch / "place-verify-0123456789abcdef"
    line = {"outcome": at.EXTRACT_PENDING, "op": "place-verify", "bundle_id": None,
            "created_utc": "2026-10-02T00:00:00+00:00", "project_id": PROJECT,
            "session_id": SESSION}
    assert at._index_append(scratch, {**line, "path": str(good)}) == index
    before = index.read_bytes()
    assert json.loads(before.decode().splitlines()[-1])["path"] == str(good)

    registry.open_session(PROJECT, "s2", pid=100, boot_id=BOOT)
    other = registry.scratch_dir(PROJECT, "s2", "scratch")
    assert other != scratch
    for bad in (tmp_path / "elsewhere" / "place-verify-1",
                scratch / "nested" / "place-verify-2",
                scratch,
                scratch / ".." / "place-verify-3",
                other / "place-verify-4"):
        with pytest.raises(at.ArchiveTargetRefused) as info:
            at._index_append(scratch, {**line, "path": str(bad)})
        assert info.value.reason == "extract_root_outside_scratch", bad
    assert index.read_bytes() == before
    assert not at.extract_index_path(other).exists()


def test_mutant_identity_recheck(tmp_path, registry, monkeypatch):
    """Catches a receipt written for a copy whose target identity changed
    while it was being verified (S13, identity on every use): the identity
    probe flips to a mismatch after `verify_archive` passed on the
    destination and before the receipt would be written. place() must
    return ARCHIVE_UNVERIFIED, write no receipt (write_receipt never runs),
    append no COPY_VERIFIED and keep the copy as `.partial`. Mutant killed:
    no_recheck_after_verify. Negative control: the same placement without
    the flip verifies and gets its receipt and COPY_VERIFIED."""
    target, probe = make_target(tmp_path, registry)
    expected = dict(probe.identity)
    ledger = at.RedundancyLedger(tmp_path / "ledger.jsonl")
    real_verify = ab.verify_archive
    calls = []
    flip = {"on": True}

    def verify_then_flip(bundle_dir, *, extract_root):
        report = real_verify(bundle_dir, extract_root=extract_root)
        calls.append(Path(bundle_dir))
        if flip["on"]:
            probe.identity = {"serial": "OTHER", "label": "VH-T1"}
        return report

    monkeypatch.setattr(ab, "verify_archive", verify_then_flip)
    bundle = make_bundle(tmp_path)
    area = target.root / at.COLD_STORE / PROJECT
    result = target.place(bundle, PROJECT, at.COLD_STORE, ledger=ledger)
    assert result.status == ab.ARCHIVE_UNVERIFIED, result.detail
    assert result.report.status == ab.ARCHIVED_VERIFIED, "the copy itself verified"
    assert "identity no longer confirmed" in result.detail
    assert calls == [area / bundle.name], "write_receipt must not run"
    assert result.receipt_path is None and result.receipt_digest is None
    assert result.path == area / (bundle.name + at.PARTIAL_SUFFIX)
    assert not (result.path / ab.RECEIPT).exists()
    assert not os.path.lexists(area / bundle.name)
    assert [r["event"] for r in ledger.records(bundle.name)] == [at.COPY_WRITTEN]

    flip["on"] = False
    probe.identity = expected
    calls.clear()
    control = target.place(make_bundle(tmp_path, label="ctl"), PROJECT, at.COLD_STORE,
                           ledger=ledger)
    assert control.status == ab.ARCHIVED_VERIFIED, control.detail
    assert (control.path / ab.RECEIPT).is_file() and len(calls) == 2
    assert [r["event"] for r in ledger.records(control.bundle_id)] == [
        at.COPY_WRITTEN, at.COPY_VERIFIED]


def test_mutant_scrub_receipt_must_name_verified_payload(tmp_path, registry):
    """Catches a scrub that accepts any receipt beside a copy that verifies:
    when the placed copy's ARCHIVE_RECEIPT.json is rewritten with the
    receipt of another bundle (which names another payload and content),
    the copy itself still verifies but the scrub appends COPY_STALE, never
    COPY_VERIFIED with that receipt's digest. Mutant killed:
    scrub_receipt_match_removed. Negative control: the same scrub with the
    copy's own receipt (before, and after it is written back) appends
    COPY_VERIFIED."""
    ledger = at.RedundancyLedger(tmp_path / "ledger.jsonl")
    target, _ = make_target(tmp_path, registry)
    first = target.place(make_bundle(tmp_path), PROJECT, at.COLD_STORE, ledger=ledger)
    second = target.place(make_bundle(tmp_path, label="two"), PROJECT, at.COLD_STORE)
    assert first.status == second.status == ab.ARCHIVED_VERIFIED
    name = first.path.name
    assert at.scrub(target, name, project_id=PROJECT,
                    ledger=ledger).event == at.COPY_VERIFIED
    own = (first.path / ab.RECEIPT).read_bytes()
    (first.path / ab.RECEIPT).write_bytes((second.path / ab.RECEIPT).read_bytes())
    bad = at.scrub(target, name, project_id=PROJECT, ledger=ledger)
    assert bad.report.status == ab.ARCHIVED_VERIFIED, "the copy itself verifies"
    assert bad.event == at.COPY_STALE and bad.receipt_digest is None
    assert "another payload" in bad.detail
    last = ledger.records(name)[-1]
    assert last["event"] == at.COPY_STALE and last["receipt_digest"] is None
    (first.path / ab.RECEIPT).write_bytes(own)
    again = at.scrub(target, name, project_id=PROJECT, ledger=ledger)
    assert again.event == at.COPY_VERIFIED and again.receipt_digest == first.receipt_digest
