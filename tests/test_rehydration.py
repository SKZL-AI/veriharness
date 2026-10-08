"""P5-31: receipt-gated rehydration (`hoh.rehydration`, STORAGE-C2) and the
strengthened evidence boundary of `tools/capability_matrix.py` ``_inside()``.

Rehydration is tested against real two-commit git repositories built in
`tmp_path`: committed receipts are read from git at HEAD, never handed in by
the caller. Bundles are placed on `tmp_path` targets with injected probes
(see `test_archive_target`). ``_inside()`` is exercised with its module-level
``HOH`` pointed at such a repository. Every test names the failure it catches
and the mutant that kills it, and carries a negative control that changes
only the condition under test. Nothing here removes a file: tampering is done
by rewriting or adding files.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from test_archive_target import (  # noqa: F401  (isolated_env is an autouse fixture)
    PROJECT,
    SESSION,
    flip_byte,
    git,
    isolated_env,
    listing,
    make_bundle,
    make_target,
    new_registry,
)

import hoh.archive_bundle as ab
import hoh.archive_target as at
import hoh.rehydration as rh

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "capability_probe_c2", ROOT / "tools" / "capability_matrix.py")
cm = importlib.util.module_from_spec(_spec)
sys.modules["capability_probe_c2"] = cm
_spec.loader.exec_module(cm)


# --------------------------------------------------------------------------- #
# helpers (also imported by test_archive_target.py)
# --------------------------------------------------------------------------- #

def make_repo(path: Path) -> Path:
    """A repository with two commits; HEAD holds README and an empty
    receipts directory marker."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "README").write_text("one\n")
    git(path, "add", "README")
    git(path, "commit", "-q", "-m", "first")
    (path / "README").write_text("two\n")
    git(path, "commit", "-q", "-am", "second")
    return path


def receipts_dir(repo: Path) -> Path:
    path = repo / rh.REHYDRATION_RECEIPTS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def commit_receipt(repo: Path, receipt: Path, name: str | None = None) -> Path:
    """Copy a receipt into the repository's receipts directory and commit it."""
    dest = receipts_dir(repo) / (name or Path(receipt).parent.name + ".json")
    shutil.copyfile(receipt, dest)
    git(repo, "add", str(dest.relative_to(repo)))
    git(repo, "commit", "-q", "-m", "receipt " + dest.name)
    return dest


def repo_state(repo: Path) -> tuple:
    """Everything in the repository's working tree plus git's own view."""
    status = git(repo, "status", "--porcelain", "--ignored", "--untracked-files=all")
    return (sorted(p for p in listing(repo) if not p.startswith(".git" + os.sep)
                   and p != ".git"), status, git(repo, "rev-parse", "HEAD"))


@pytest.fixture
def world(tmp_path):
    """A registry with a live session, a healthy target, a repository and one
    placed bundle whose receipt is committed at HEAD."""
    registry = new_registry(tmp_path)
    target, probe = make_target(tmp_path, registry)
    repo = make_repo(tmp_path / "repo")
    placed = target.place(make_bundle(tmp_path), PROJECT, at.COLD_STORE)
    assert placed.status == ab.ARCHIVED_VERIFIED
    receipt = commit_receipt(repo, placed.receipt_path)
    return {"registry": registry, "target": target, "probe": probe, "repo": repo,
            "placed": placed, "receipt": receipt, "tmp": tmp_path}


def run(world, receipt=None, bundle=None, **kwargs):
    registry = world["registry"]
    kwargs.setdefault("registry", registry)
    return rh.rehydrate(receipt or world["receipt"], bundle or world["placed"].path,
                        world["repo"], state_root=registry.state_root, project_id=PROJECT,
                        session_id=SESSION, **kwargs)


def digest_of(world) -> str:
    return world["placed"].report.digests["content_digest"]


@pytest.fixture
def inside_repo(world, monkeypatch):
    """`_inside()` judging paths of the test repository."""
    monkeypatch.setattr(cm, "HOH", world["repo"])
    return world


# --------------------------------------------------------------------------- #
# K4 -- rehydration verifies before use (S19)
# --------------------------------------------------------------------------- #

def test_rehydrate_verifies_before_use(world, monkeypatch):
    """Catches extracted content reaching the repository before it was
    verified: verify_archive runs while `.rehydrated/` does not exist yet,
    into a registered scratch extract root, and only a verified tree is
    renamed to `.rehydrated/<content_digest>/`; a bundle with one flipped
    byte is refused and nothing appears. Mutant killed: rename before
    verification. Negative control: the honest bundle is rehydrated and its
    tree's digest recomputes."""
    seen = []
    real = ab.verify_archive

    def spy(bundle_dir, *, extract_root):
        seen.append((os.path.lexists(world["repo"] / ".rehydrated"), Path(extract_root)))
        return real(bundle_dir, extract_root=extract_root)

    monkeypatch.setattr(ab, "verify_archive", spy)
    out = run(world)
    digest = digest_of(world)
    assert out == world["repo"] / ".rehydrated" / digest
    assert seen[0][0] is False, ".rehydrated existed before verification"
    scratch = world["registry"].scratch_dir(PROJECT, SESSION, "scratch")
    assert seen[0][1].parent.parent == scratch and seen[0][1].name == "tree"
    assert seen[0][1].parent.name.startswith("rehydrate-")
    assert not seen[0][1].exists(), "tree/ is what was renamed"
    assert rh.tree_content_digest(out) == digest
    assert (out / "a.txt").read_bytes() == b"alpha\n"
    assert os.readlink(out / "link") == "a.txt"
    assert (world["repo"] / ".rehydrated" / rh.REHYDRATED_OWNER).is_file()

    # a second bundle, damaged after its receipt was committed
    target = world["target"]
    placed = target.place(make_bundle(world["tmp"], label="dmg",
                                      files={"d.txt": b"dmg\n"}), PROJECT, at.COLD_STORE)
    receipt = commit_receipt(world["repo"], placed.receipt_path)
    flip_byte(placed.path / ab.TREE_PAYLOAD)
    seen.clear()
    with pytest.raises(rh.RehydrationRefused) as info:
        run(world, receipt=receipt, bundle=placed.path)
    assert info.value.report.status == ab.ARCHIVE_UNVERIFIED
    assert seen and not os.path.lexists(
        world["repo"] / ".rehydrated" / placed.report.digests["content_digest"])


def test_rehydrate_refuses_uncommitted_receipt(world):
    """Catches a receipt trusted because it exists, or because the caller
    says so: a receipt only written, one staged but not committed, and the
    first commit's version of a receipt that HEAD changed are all refused;
    `rehydrate` has no parameter through which a caller could hand in a
    receipt set. Mutant killed: caller-supplied receipt set trusted.
    Negative control: the receipt committed at HEAD is accepted."""
    repo, target = world["repo"], world["target"]
    assert "committed_receipts" not in inspect.signature(rh.rehydrate).parameters
    placed = target.place(make_bundle(world["tmp"], label="two",
                                      files={"t.txt": b"two\n"}), PROJECT, at.COLD_STORE)
    written = receipts_dir(repo) / "uncommitted.json"
    shutil.copyfile(placed.receipt_path, written)
    with pytest.raises(rh.RehydrationRefused, match="not a receipt committed at HEAD"):
        run(world, receipt=written, bundle=placed.path)
    git(repo, "add", str(written.relative_to(repo)))
    with pytest.raises(rh.RehydrationRefused, match="not a receipt committed at HEAD"):
        run(world, receipt=written, bundle=placed.path)
    git(repo, "commit", "-q", "-m", "now committed")
    assert run(world, receipt=written, bundle=placed.path).is_dir()

    # HEAD, not history: the older version of a changed receipt is refused
    old_bytes = world["receipt"].read_bytes()
    world["receipt"].write_bytes(old_bytes.replace(b'"verified_utc"', b'"verified_utc_x"'))
    git(repo, "commit", "-q", "-am", "receipt changed")
    stash = repo / "notes" / "old-receipt.json"
    stash.parent.mkdir()
    stash.write_bytes(old_bytes)
    assert hashlib.sha256(old_bytes).hexdigest() not in rh.committed_receipt_file_digests(repo)
    with pytest.raises(rh.RehydrationRefused, match="not a receipt committed at HEAD"):
        run(world, receipt=stash)


def test_rehydrate_refuses_receipt_outside_repo(world):
    """Catches an external receipt path taken as input: a byte-identical
    copy of the committed receipt outside the repository, and an in-repo
    symlink resolving to it, are refused. Mutant killed: the inside-repo
    check removed. Negative control: the committed receipt itself."""
    outside = world["tmp"] / "outside-receipt.json"
    shutil.copyfile(world["receipt"], outside)
    with pytest.raises(rh.RehydrationRefused, match="outside the repository"):
        run(world, receipt=outside)
    link = world["repo"] / "receipt-link.json"
    link.symlink_to(outside)
    with pytest.raises(rh.RehydrationRefused, match="outside the repository"):
        run(world, receipt=link)
    assert run(world).is_dir()


def test_rehydrate_refuses_digest_mismatch(world):
    """Catches a bundle accepted under another bundle's receipt: the second
    bundle verifies on its own, but its content digest differs from the
    committed receipt's and it is refused. Mutant killed: the receipt/report
    digest comparison removed. Negative control: the matching bundle."""
    other = world["target"].place(make_bundle(world["tmp"], label="oth",
                                              files={"o.txt": b"o\n"}),
                                  PROJECT, at.COLD_STORE)
    with pytest.raises(rh.RehydrationRefused, match="content digest"):
        run(world, bundle=other.path)
    assert not os.path.lexists(world["repo"] / ".rehydrated")
    assert run(world).is_dir()


def test_rehydrate_checks_target_identity_and_filesystem(world, tmp_path, monkeypatch):
    """Catches a bundle read from an unconfirmed target or a state root on
    another filesystem: with `target` given, an identity mismatch refuses;
    a state root whose st_dev differs from the repository's refuses before
    any extract root is created. Mutant killed: the st_dev comparison
    removed. Negative control: the same call with the confirmed target."""
    world["probe"].identity = {"serial": "OTHER"}
    with pytest.raises(rh.RehydrationRefused, match="identity_mismatch"):
        run(world, target=world["target"])
    world["probe"].identity = dict(world["target"].expected_identity)

    real_stat = os.stat
    state = Path(world["registry"].state_root)

    def other_device(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if isinstance(path, (str, os.PathLike)) and Path(path) == state:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1) + tuple(st)[3:])
        return st

    monkeypatch.setattr(rh.os, "stat", other_device)
    with pytest.raises(rh.RehydrationRefused, match="filesystem"):
        run(world, target=world["target"])
    monkeypatch.setattr(rh.os, "stat", real_stat)
    assert run(world, target=world["target"]).is_dir()


def test_failed_rehydration_leaves_nothing_in_repo(world):
    """Catches a failed rehydration that leaves a trace in the repository (a
    staging directory, an empty `.rehydrated/`, a half tree): after refusals
    for an uncommitted receipt, a digest mismatch and a damaged bundle the
    working tree and git's view are unchanged (the git payload has its own
    test). Mutant killed:
    extraction staged inside the repository (`.rehydrated/.incoming/`).
    Negative control: a successful rehydration does change the repository."""
    repo, target, tmp = world["repo"], world["target"], world["tmp"]
    other = target.place(make_bundle(tmp, label="fail", files={"f.txt": b"f\n"}),
                         PROJECT, at.COLD_STORE)
    loose = receipts_dir(repo) / "loose.json"
    shutil.copyfile(other.receipt_path, loose)
    damaged = target.place(make_bundle(tmp, label="dmg2", files={"g.txt": b"g\n"}),
                           PROJECT, at.COLD_STORE)
    damaged_receipt = commit_receipt(repo, damaged.receipt_path)
    flip_byte(damaged.path / ab.TREE_PAYLOAD)
    before = repo_state(repo)
    attempts = [dict(receipt=loose, bundle=other.path),
                dict(bundle=other.path),
                dict(receipt=damaged_receipt, bundle=damaged.path)]
    for attempt in attempts:
        with pytest.raises(rh.RehydrationRefused):
            run(world, **attempt)
        assert repo_state(repo) == before, attempt
    assert not os.path.lexists(repo / ".rehydrated")
    run(world)
    assert repo_state(repo) != before


def test_rehydrate_refuses_git_payload(world):
    """Catches a git bundle rehydrated as an evidence tree (a nested .git
    under `.rehydrated/`): a committed receipt of a git payload is refused
    with the specified message before anything is extracted, and nothing
    lands in the repository. Mutant killed: the payload-kind check removed.
    Negative control: the tree payload's committed receipt rehydrates."""
    tmp, repo = world["tmp"], world["repo"]
    source = make_repo(tmp / "git-source")
    out = tmp / "git-bundles"
    out.mkdir()
    bundle = ab.ArchiveBundle.build_git(source, ["main"], out, label="gitev", meta={},
                                        ownership={"status": "OWNED", "root": str(tmp)})
    placed = world["target"].place(bundle, PROJECT, at.COLD_STORE)
    assert placed.status == ab.ARCHIVED_VERIFIED
    receipt = commit_receipt(repo, placed.receipt_path)
    lines_before = len((world["registry"].scratch_dir(PROJECT, SESSION, "scratch")
                        / "archive-target" / at.EXTRACT_INDEX).read_text().splitlines())
    with pytest.raises(rh.RehydrationRefused) as info:
        run(world, receipt=receipt, bundle=placed.path)
    assert str(info.value) == rh.GIT_PAYLOAD_REFUSAL == (
        "git bundles are restored with git, not rehydrated as evidence trees")
    assert not os.path.lexists(repo / ".rehydrated")
    lines_after = len((world["registry"].scratch_dir(PROJECT, SESSION, "scratch")
                       / "archive-target" / at.EXTRACT_INDEX).read_text().splitlines())
    assert lines_after == lines_before, "a git payload is refused before extraction"
    out_tree = run(world)
    assert not any(p.endswith(".git") or "/.git/" in p for p in listing(out_tree))


def test_rehydrate_rename_fails_closed_without_renameat2(world, monkeypatch):
    """Catches a non-atomic fallback for the move into `.rehydrated/`: where
    renameat2(RENAME_NOREPLACE) is missing (ab._renameat2() is None) or the
    filesystem answers ENOSYS, the move fails closed with RehydrationRefused,
    nothing lands under `.rehydrated/<digest>/`, the repository is unchanged
    and the extract root is indexed FAILED; `_rename_into_place` never moves
    a tree onto a pre-created empty directory. Mutant killed: the
    iteration-1 check-and-`os.rename` fallback. Negative control: with the
    real renameat2 the same rehydration succeeds."""
    import ctypes
    import errno
    repo = world["repo"]
    digest = digest_of(world)
    index = (world["registry"].scratch_dir(PROJECT, SESSION, "scratch")
             / "archive-target" / at.EXTRACT_INDEX)
    real = ab._renameat2
    before = repo_state(repo)
    monkeypatch.setattr(ab, "_renameat2", lambda: None)
    with pytest.raises(rh.RehydrationRefused, match="renameat2"):
        run(world)
    assert repo_state(repo) == before
    assert not os.path.lexists(repo / ".rehydrated")
    last = [line for line in index.read_text().splitlines() if line.strip()][-1]
    assert '"outcome": "FAILED"' in last and '"op": "rehydrate"' in last

    # the helper itself: no fallback onto a directory created in between
    src = world["tmp"] / "rename-src"
    src.mkdir()
    (src / "evidence.txt").write_text("e\n")
    dst = world["tmp"] / "rename-dst"
    dst.mkdir()
    with pytest.raises(rh.RehydrationRefused):
        rh._rename_into_place(src, dst)
    assert os.listdir(dst) == [] and os.listdir(src) == ["evidence.txt"]

    def enosys(*args):
        ctypes.set_errno(errno.ENOSYS)
        return -1

    monkeypatch.setattr(ab, "_renameat2", lambda: enosys)
    fresh = world["tmp"] / "rename-dst-absent"
    with pytest.raises(rh.RehydrationRefused, match="not supported"):
        rh._rename_into_place(src, fresh)
    assert not os.path.lexists(fresh) and os.listdir(src) == ["evidence.txt"]
    with pytest.raises(rh.RehydrationRefused):
        run(world)
    assert not os.path.lexists(repo / ".rehydrated" / digest)

    monkeypatch.setattr(ab, "_renameat2", real)
    out = run(world)
    assert out == repo / ".rehydrated" / digest and rh.tree_content_digest(out) == digest


def test_rehydrate_rename_unsupported_leaves_repo_unchanged(world, monkeypatch):
    """Catches a refusal that still writes into the repository: when
    renameat2 exists but the filesystem answers ENOSYS to RENAME_NOREPLACE,
    rehydrate must refuse before `.rehydrated/` (and its OWNER.json) is
    created -- repo_state unchanged, no `.rehydrated/` at all -- index the
    extract root FAILED and keep the verified tree in registered scratch.
    Mutant killed: no rename-capability probe before _ensure_rehydrated_dir
    (the e9b5ecae3d77 behaviour that leaves `.rehydrated/OWNER.json` behind).
    Negative control: with the real renameat2 the same call succeeds."""
    import ctypes
    import errno
    import json
    repo = world["repo"]
    digest = digest_of(world)
    index = (world["registry"].scratch_dir(PROJECT, SESSION, "scratch")
             / "archive-target" / at.EXTRACT_INDEX)

    def enosys(*args):
        ctypes.set_errno(errno.ENOSYS)
        return -1

    real = ab._renameat2
    before = repo_state(repo)
    monkeypatch.setattr(ab, "_renameat2", lambda: enosys)
    with pytest.raises(rh.RehydrationRefused) as info:
        run(world)
    assert not os.path.lexists(repo / ".rehydrated"), "OWNER.json left in the repository"
    assert repo_state(repo) == before
    assert "cannot rename without replacing" in str(info.value)
    lines = [json.loads(line) for line in index.read_text().splitlines() if line.strip()]
    last = lines[-1]
    assert last["op"] == "rehydrate" and last["outcome"] == "FAILED"
    root = Path(last["path"])
    assert [l["outcome"] for l in lines if l["path"] == last["path"]] == ["PENDING", "FAILED"]
    assert sorted(os.listdir(root)) == ["EXTRACT_ROOT.json", "tree"]
    assert rh.tree_content_digest(root / "tree") == digest
    assert (root / "tree" / "a.txt").read_bytes() == b"alpha\n"

    monkeypatch.setattr(ab, "_renameat2", real)
    out = run(world)
    assert out == repo / ".rehydrated" / digest and rh.tree_content_digest(out) == digest


def test_rehydrate_with_state_root_only(world, tmp_path):
    """Catches a rehydrate that works only with an injected registry: given
    just `state_root`, it opens the registry there and needs a live lease of
    the session -- the fake session's pid does not exist for the real OS
    readers, so it is refused before extraction. Mutant killed: the lease
    requirement bypassed. Negative control: the same call with the registry
    whose readers know the session."""
    with pytest.raises(rh.RehydrationRefused, match="no registered scratch"):
        rh.rehydrate(world["receipt"], world["placed"].path, world["repo"],
                     state_root=world["registry"].state_root, project_id=PROJECT,
                     session_id=SESSION)
    assert not os.path.lexists(world["repo"] / ".rehydrated")
    assert run(world).is_dir()


# --------------------------------------------------------------------------- #
# K5 -- _inside() and the evidence boundary (S18)
# --------------------------------------------------------------------------- #

def _original_inside(hoh: Path, rel: str):
    """`_inside()` as it was before STORAGE-C2, for comparison."""
    if not rel or Path(rel).is_absolute():
        return None
    path = (hoh / rel).resolve()
    return path if path.is_relative_to(hoh.resolve()) else None


def test_inside_accepts_honest_rehydrated_tree(inside_repo):
    """Catches a rule so strict that honest evidence is lost: a tree
    rehydrated through `rehydrate` from a receipt committed at HEAD passes
    `_inside()` and `is_trusted_evidence_path`. Mutant killed: every
    `.rehydrated/` path refused. Negative control: the boundary file
    `.rehydrated/OWNER.json` is not evidence."""
    out = run(inside_repo)
    rel = os.path.relpath(out / "a.txt", inside_repo["repo"])
    assert cm._inside(rel) == (out / "a.txt").resolve()
    assert cm._inside(os.path.relpath(out, inside_repo["repo"])) == out.resolve()
    assert rh.is_trusted_evidence_path(out / "sub" / "b.bin", inside_repo["repo"])
    assert cm._inside(".rehydrated/OWNER.json") is None
    assert cm._inside(".rehydrated") is None


def test_inside_refuses_tampered_rehydrated(inside_repo):
    """Catches tampered rehydrated evidence accepted because it once
    verified: after one file of an honest `.rehydrated/<digest>/` tree is
    rewritten, or a file is added to it, `_inside()` refuses every path in
    it. Mutant killed: `_inside` without the `.rehydrated` recompute.
    Negative control: before tampering the same path is accepted."""
    out = run(inside_repo)
    rel = os.path.relpath(out / "a.txt", inside_repo["repo"])
    assert cm._inside(rel) is not None
    (out / "a.txt").write_bytes(b"ALPHA\n")
    assert cm._inside(rel) is None
    assert cm._inside(os.path.relpath(out / "sub" / "b.bin", inside_repo["repo"])) is None
    (out / "a.txt").write_bytes(b"alpha\n")
    assert cm._inside(rel) is not None
    (out / "extra.txt").write_text("planted\n")
    assert cm._inside(rel) is None


def test_inside_refuses_uncommitted_rehydrated(inside_repo):
    """Catches a self-consistent tree trusted without a committed receipt:
    a directory placed at `.rehydrated/<d>/` where `<d>` is exactly the
    recomputed digest of its own content, but no receipt committed at HEAD
    records `<d>`, is refused; a receipt only written to the receipts
    directory does not change that. Mutant killed: recompute without the
    committed-receipt check. Negative control: the honest tree beside it."""
    repo = inside_repo["repo"]
    honest = run(inside_repo)
    forged_src = repo / ".rehydrated" / "forging"
    forged_src.mkdir()
    (forged_src / "a.txt").write_bytes(b"forged evidence\n")
    digest = rh.tree_content_digest(forged_src)
    forged = repo / ".rehydrated" / digest
    os.rename(forged_src, forged)
    assert rh.tree_content_digest(forged) == digest
    assert digest not in rh.committed_content_digests(repo)
    assert cm._inside(f".rehydrated/{digest}/a.txt") is None
    assert not rh.is_trusted_evidence_path(forged / "a.txt", repo)
    assert cm._inside(os.path.relpath(honest / "a.txt", repo)) is not None


def test_inside_judges_resolved_symlink(inside_repo):
    """Catches a path judged by its spelling: an in-tree symlink `evidence`
    that resolves into a forged `.rehydrated/<d>/` is refused although its
    lexical form does not mention `.rehydrated`. Mutant killed: a lexical
    prefix test. Negative control: a symlink to the honest tree passes, and
    one to an ordinary in-tree directory is unchanged."""
    repo = inside_repo["repo"]
    honest = run(inside_repo)
    forged_src = repo / ".rehydrated" / "tmpforge"
    forged_src.mkdir()
    (forged_src / "x.txt").write_text("x\n")
    digest = rh.tree_content_digest(forged_src)
    os.rename(forged_src, repo / ".rehydrated" / digest)
    (repo / "evidence").symlink_to(Path(".rehydrated") / digest)
    assert cm._inside("evidence/x.txt") is None
    assert cm._inside("evidence") is None
    (repo / "honest").symlink_to(os.path.relpath(honest, repo))
    assert cm._inside("honest/a.txt") == (honest / "a.txt").resolve()
    (repo / "plain").mkdir()
    (repo / "plain" / "p.txt").write_text("p\n")
    (repo / "plain-link").symlink_to("plain")
    assert cm._inside("plain-link/p.txt") == (repo / "plain" / "p.txt").resolve()


def test_inside_refuses_external_archive_path(inside_repo):
    """Catches an archive target path used as evidence (S18): the absolute
    path of the placed bundle, a relative path climbing out to it, and an
    in-tree symlink to it are all refused by `_inside()`; the rehydration
    rule does not trust it either. Mutant killed: `_inside()` accepting a
    resolved path outside the tree. Negative control: the same content
    rehydrated into the tree is accepted."""
    repo = inside_repo["repo"]
    placed = inside_repo["placed"].path
    assert cm._inside(str(placed / ab.MANIFEST)) is None
    assert cm._inside(os.path.relpath(placed / ab.MANIFEST, repo)) is None
    (repo / "archive-link").symlink_to(placed)
    assert cm._inside("archive-link/" + ab.MANIFEST) is None
    assert not rh.is_trusted_evidence_path(placed / ab.MANIFEST, repo)
    out = run(inside_repo)
    assert cm._inside(os.path.relpath(out / "a.txt", repo)) is not None


def test_inside_unchanged_for_ordinary_paths(inside_repo, monkeypatch):
    """Catches a regression of the old boundary: for empty, absolute,
    escaping, ordinary, missing and symlinked in-tree paths `_inside()`
    answers exactly what it answered before STORAGE-C2, in the test
    repository and in this repository. Mutant killed: any change to the
    pre-existing rules. Negative control: a `.rehydrated/` path, the one
    place where the answers differ."""
    repo = inside_repo["repo"]
    (repo / "dir").mkdir()
    (repo / "dir" / "f.txt").write_text("f\n")
    (repo / "dir-link").symlink_to("dir")
    (repo / "out-link").symlink_to(inside_repo["tmp"])
    cases = ["", "/etc/passwd", "../outside", "tests/../../x", "README", "dir/f.txt",
             "dir-link/f.txt", "out-link", "out-link/repo/README", "missing/file",
             "dir/../README", "./README"]
    for rel in cases:
        assert cm._inside(rel) == _original_inside(repo, rel), rel
    monkeypatch.setattr(cm, "HOH", ROOT)
    for rel in cases + ["pyproject.toml", "src/hoh", "tools/capability_matrix.py"]:
        assert cm._inside(rel) == _original_inside(ROOT, rel), rel
    monkeypatch.setattr(cm, "HOH", repo)
    run(inside_repo)
    (repo / ".rehydrated" / "loose.txt").write_text("not evidence\n")
    assert _original_inside(repo, ".rehydrated/loose.txt") is not None
    assert cm._inside(".rehydrated/loose.txt") is None


# --------------------------------------------------------------------------- #
# K12 -- Revision 8: the _inside() helper and the starget-a survivors
# --------------------------------------------------------------------------- #

def test_mutant_inside_import_fail_closed(inside_repo, monkeypatch):
    """Catches `_rehydrated_trusted` failing open when `hoh` cannot be
    imported (S18: every `.rehydrated/` path would be trusted): with `hoh`
    and `hoh.rehydration` absent from `sys.modules` and no package under
    `HOH / "src"` for the finder, `_inside()` of an honest rehydrated path
    returns None, `sys.path` is unchanged, no half-loaded `hoh` is left
    behind, and the evidence line from `evidence_status` says
    'rehydrated evidence not trusted: <reason>'. A check that raises fails
    closed the same way. Mutant killed: inside_import_fail_open. Negative
    control: with `hoh` imported the same path is accepted and the finder is
    never asked for a second copy."""
    import importlib.machinery
    repo = inside_repo["repo"]
    out = run(inside_repo)
    rel = os.path.relpath(out / "a.txt", repo)
    probe = {"path": rel, "evidence_dir": os.path.relpath(out, repo)}
    real_find = importlib.machinery.PathFinder.find_spec
    asked = []

    def spy_find(cls, name, path=None, target=None):
        if name == "hoh":
            asked.append(list(path or []))
        return real_find(name, path, target)

    monkeypatch.setattr(importlib.machinery.PathFinder, "find_spec", classmethod(spy_find))
    path_before = list(sys.path)
    assert cm._inside(rel) == (out / "a.txt").resolve()
    status, line = cm.evidence_status(probe)
    assert "rehydrated evidence not trusted" not in line
    assert asked == [], "an imported hoh must be reused, not found again"

    monkeypatch.delitem(sys.modules, "hoh")
    monkeypatch.delitem(sys.modules, "hoh.rehydration")
    assert real_find("hoh", [str(cm.HOH / "src")]) is None
    assert cm._inside(rel) is None
    assert asked == [[str(repo / "src")]]
    assert "hoh" not in sys.modules and "hoh.rehydration" not in sys.modules
    status, line = cm.evidence_status(probe)
    assert status == cm.NOT_DETERMINABLE
    assert "rehydrated evidence not trusted: hoh.rehydration unavailable" in line, line
    assert sys.path == path_before

    monkeypatch.undo()                   # hoh is back; undo also reset HOH
    monkeypatch.setattr(cm, "HOH", repo)
    assert cm._inside(rel) is not None

    def broken(*args, **kwargs):
        raise RuntimeError("git not runnable")

    monkeypatch.setattr(rh, "is_trusted_evidence_path", broken)
    assert cm._inside(rel) is None
    status, line = cm.evidence_status(probe)
    assert "rehydrated evidence not trusted: hoh.rehydration unavailable (RuntimeError" in line


def _function_node(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == name)


def _sys_path_uses(node: ast.AST) -> list[int]:
    """Lines in `node` that reach `sys.path`: the attribute itself (insert,
    append, assignment, slice), `from sys import path`, `getattr(sys, "path")`."""
    def reaches(item: ast.AST) -> bool:
        if isinstance(item, ast.Attribute):
            return (item.attr == "path" and isinstance(item.value, ast.Name)
                    and item.value.id == "sys")
        if isinstance(item, ast.ImportFrom):
            return item.module == "sys" and any(a.name == "path" for a in item.names)
        return (isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
                and item.func.id in ("getattr", "setattr") and len(item.args) >= 2
                and isinstance(item.args[1], ast.Constant) and item.args[1].value == "path")

    return [item.lineno for item in ast.walk(node) if reaches(item)]


def test_capability_matrix_helper_does_not_edit_sys_path():
    """Catches `_rehydrated_trusted` editing the interpreter's module search
    path to find `hoh` (starget-b minor, Revision 8c item 1): an AST scan of
    that function in tools/capability_matrix.py finds no `sys.path` access
    of any kind, and it locates the package with
    `importlib.machinery.PathFinder.find_spec`. Mutant killed: the sys.path
    edit restored. Negative control: the same scan finds the `sys.path`
    edit of `evidence_status` (around line 194), which is outside this
    run's scope and stays (8b item 1)."""
    tree = ast.parse((ROOT / "tools" / "capability_matrix.py").read_text(encoding="utf-8"))
    helper = _function_node(tree, "_rehydrated_trusted")
    assert _sys_path_uses(helper) == []
    source = ast.unparse(helper)
    assert "PathFinder.find_spec('hoh'" in source and "sys.modules.get('hoh')" in source
    assert _sys_path_uses(_function_node(tree, "evidence_status")) != []
    restored = ast.parse("def _rehydrated_trusted(path):\n"
                          "    sys.path.insert(0, 'src')\n")
    assert _sys_path_uses(restored) == [2]


def test_mutant_rh_unverified_ok(world):
    """Catches rehydration that skips S19's explicit status check: a placed
    bundle whose SHA256SUMS no longer matches its MANIFEST.json does not
    verify (ARCHIVE_UNVERIFIED), although its payload SHA-256, its content
    digest and the recompute of the extracted tree all still equal the
    receipt's -- only the status check refuses it, and nothing lands in the
    repository. Mutant killed: rh_unverified_ok. Negative control: with the
    SHA256SUMS line written back the same bundle is rehydrated."""
    placed = world["placed"].path
    sums = (placed / ab.SUMS).read_bytes()
    lines = sums.decode("ascii").splitlines(keepends=True)
    index = next(i for i, l in enumerate(lines) if l.rstrip().endswith(ab.MANIFEST))
    first = lines[index][0]
    lines[index] = ("0" if first != "0" else "1") + lines[index][1:]
    (placed / ab.SUMS).write_bytes("".join(lines).encode("ascii"))
    before = repo_state(world["repo"])
    with pytest.raises(rh.RehydrationRefused) as info:
        run(world)
    report = info.value.report
    assert report.status == ab.ARCHIVE_UNVERIFIED
    assert [i["id"] for i in report.failed()] == ["sha256sums_manifest"]
    assert report.digests["content_digest"] == digest_of(world)
    assert report.digests["payload_sha256"] == world["placed"].report.digests["payload_sha256"]
    assert "did not verify" in str(info.value)
    assert repo_state(world["repo"]) == before
    assert not os.path.lexists(world["repo"] / ".rehydrated")
    (placed / ab.SUMS).write_bytes(sums)
    assert run(world) == world["repo"] / ".rehydrated" / digest_of(world)


def _forged_receipt(world, name: str, **changes) -> Path:
    """A copy of the world's receipt with `changes`, committed at HEAD."""
    record = json.loads(world["placed"].receipt_path.read_text())
    record.update(changes)
    draft = world["tmp"] / ("draft-" + name)
    draft.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return commit_receipt(world["repo"], draft, name)


def test_mutant_rh_payload_sha_ignored(world):
    """Catches rehydration that does not bind the receipt to the payload: a
    committed receipt that records the right content digest and status but
    another payload SHA-256 is refused for the bundle, and nothing lands in
    the repository. Mutant killed: rh_payload_sha_ignored. Negative control:
    the honest committed receipt rehydrates the same bundle."""
    forged = _forged_receipt(world, "other-payload.json", payload_sha256="0" * 64)
    before = repo_state(world["repo"])
    with pytest.raises(rh.RehydrationRefused) as info:
        run(world, receipt=forged)
    assert "the payload is not the one the receipt names" in str(info.value)
    assert info.value.report.status == ab.ARCHIVED_VERIFIED
    assert repo_state(world["repo"]) == before
    assert run(world).is_dir()


def test_mutant_rh_no_recompute(world):
    """Catches a rehydrated tree whose digest cannot be recomputed from the
    tree alone, so `_inside()` could never trust it: a bundle whose source
    holds a hard-link pair verifies (its hard-link group checks pass) and
    its receipt is committed, but the extracted tree does not recompute
    (`tree_content_digest` is None) and rehydration refuses it before
    anything lands in the repository. Mutant killed: rh_no_recompute.
    Negative control: the same files as two independent copies rehydrate."""
    def bundle(linked: bool) -> Path:
        source = world["tmp"] / f"hl-src-{linked}"
        source.mkdir()
        (source / "one.txt").write_bytes(b"shared content\n")
        if linked:
            os.link(source / "one.txt", source / "two.txt")
        else:
            (source / "two.txt").write_bytes(b"shared content\n")
        out = world["tmp"] / f"hl-out-{linked}"
        out.mkdir()
        return ab.ArchiveBundle.build_tree(source, out, label="hl", meta={"unique": True},
                                           ownership={"status": "OWNED",
                                                      "root": str(world["tmp"])})

    target = world["target"]
    linked = target.place(bundle(True), PROJECT, at.COLD_STORE)
    assert linked.status == ab.ARCHIVED_VERIFIED, linked.detail
    receipt = commit_receipt(world["repo"], linked.receipt_path)
    before = repo_state(world["repo"])
    with pytest.raises(rh.RehydrationRefused) as info:
        run(world, receipt=receipt, bundle=linked.path)
    assert "does not recompute" in str(info.value)
    assert info.value.report.status == ab.ARCHIVED_VERIFIED
    assert repo_state(world["repo"]) == before

    plain = target.place(bundle(False), PROJECT, at.COLD_STORE)
    out = run(world, receipt=commit_receipt(world["repo"], plain.receipt_path),
              bundle=plain.path)
    assert rh.tree_content_digest(out) == plain.report.digests["content_digest"]


def test_mutant_rh_nlink_ignored(inside_repo):
    """Catches a rehydrated file that is a hard link to a file outside the
    tree: with one file of an honest `.rehydrated/<digest>/` tree replaced
    by a hard link to an outside file of identical content and mode, every
    hash still matches, but the outside name can change the evidence at any
    time -- `tree_content_digest` is None and the path is not trusted by
    `is_trusted_evidence_path` or `_inside()`. Mutant killed:
    rh_nlink_ignored. Negative control: the same content as a plain copy
    is trusted again."""
    repo, tmp = inside_repo["repo"], inside_repo["tmp"]
    out = run(inside_repo)
    digest = out.name
    rel = os.path.relpath(out / "a.txt", repo)
    assert cm._inside(rel) is not None
    mode = os.lstat(out / "a.txt").st_mode
    os.rename(out / "a.txt", tmp / "a.txt.rehydrated-original")
    outside = tmp / "outside-a.txt"
    outside.write_bytes(b"alpha\n")
    os.chmod(outside, mode & 0o7777)
    os.link(outside, out / "a.txt")
    assert os.lstat(out / "a.txt").st_nlink == 2
    assert rh.tree_content_digest(out) is None
    assert not rh.is_trusted_evidence_path(out / "a.txt", repo)
    assert cm._inside(rel) is None
    os.rename(out / "a.txt", tmp / "a.txt.hardlinked")
    os.rename(tmp / "a.txt.rehydrated-original", out / "a.txt")
    assert rh.tree_content_digest(out) == digest
    assert cm._inside(rel) is not None


def test_mutant_trusted_lexical(inside_repo):
    """Catches `is_trusted_evidence_path` judging a path by its spelling
    (STORAGE-D calls it directly, without `_inside()` resolving first): an
    in-tree symlink that resolves into a forged `.rehydrated/<d>/`, and one
    that resolves out of the repository, are refused although neither
    spelling mentions `.rehydrated` or leaves the tree. Mutant killed:
    trusted_lexical. Negative control: a symlink to the honest tree and an
    ordinary in-tree file are trusted."""
    repo, tmp = inside_repo["repo"], inside_repo["tmp"]
    honest = run(inside_repo)
    forging = repo / ".rehydrated" / "forging"
    forging.mkdir()
    (forging / "x.txt").write_text("forged\n")
    digest = rh.tree_content_digest(forging)
    os.rename(forging, repo / ".rehydrated" / digest)
    (repo / "into-forged").symlink_to(Path(".rehydrated") / digest)
    (tmp / "outside-dir").mkdir()
    (tmp / "outside-dir" / "y.txt").write_text("outside\n")
    (repo / "out-of-repo").symlink_to(tmp / "outside-dir")
    assert not rh.is_trusted_evidence_path(repo / "into-forged" / "x.txt", repo)
    assert not rh.is_trusted_evidence_path("into-forged/x.txt", repo)
    assert not rh.is_trusted_evidence_path(repo / "out-of-repo" / "y.txt", repo)
    (repo / "to-honest").symlink_to(os.path.relpath(honest, repo))
    assert rh.is_trusted_evidence_path(repo / "to-honest" / "a.txt", repo)
    assert rh.is_trusted_evidence_path(repo / "README", repo)


def test_mutant_committed_status_unfiltered(inside_repo):
    """Catches a committed receipt that records a failed verification
    vouching for a `.rehydrated/<d>/` tree: a self-consistent tree whose
    digest `<d>` is recorded only in a committed receipt with status
    ARCHIVE_UNVERIFIED is not in `committed_content_digests` and `_inside()`
    refuses it. Mutant killed: committed_status_unfiltered. Negative
    control: the same receipt committed with status ARCHIVED_VERIFIED makes
    the same tree trusted."""
    repo = inside_repo["repo"]
    rehydrated = repo / ".rehydrated"
    run(inside_repo)
    src = rehydrated / "forging"
    src.mkdir()
    (src / "a.txt").write_text("from an unverified bundle\n")
    digest = rh.tree_content_digest(src)
    os.rename(src, rehydrated / digest)
    _forged_receipt(inside_repo, "unverified.json", status=ab.ARCHIVE_UNVERIFIED,
                    content_digest=digest)
    assert digest not in rh.committed_content_digests(repo)
    assert cm._inside(f".rehydrated/{digest}/a.txt") is None
    _forged_receipt(inside_repo, "verified.json", status=ab.ARCHIVED_VERIFIED,
                    content_digest=digest)
    assert digest in rh.committed_content_digests(repo)
    assert cm._inside(f".rehydrated/{digest}/a.txt") == (rehydrated / digest / "a.txt").resolve()
