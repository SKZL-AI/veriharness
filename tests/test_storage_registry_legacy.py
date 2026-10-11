"""STORAGE-L (revision 3): legacy review roots inside the state
root are adopted only through a committed `veriharness.storage-legacy-roots/1`
anchor whose entry cites committed designations and reports and binds every
direct child of the root by name, type and entries digest
(`StorageRegistry.register_legacy_root`).

Everything lives under pytest's `tmp_path`; git runs with a fixed identity
and no system configuration (`isolated_env`). No test reads or writes a real
state root. Each test names the failure it catches and the mutant it kills.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from hoh import storage_registry as sr
from hoh import storage_tiers as st
from hoh.storage_registry import OWNED, PROTECTED, UNKNOWN, StorageOwnershipError
from test_storage_registry import (  # noqa: F401  (isolated_env is an autouse fixture)
    BOOT,
    FakeOS,
    fake,
    git,
    isolated_env,
    make_repo,
    new_registry,
)

SCHEMA = "veriharness.storage-legacy-roots/1"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _entries(base: Path) -> list[st.Entry]:
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        for name in dirnames + filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(base).as_posix()
            mode = os.lstat(full).st_mode & 0o7777
            if full.is_symlink():
                target = os.readlink(full)
                out.append(st.Entry(rel, "symlink", mode, len(os.fsencode(target)),
                                    link_target=target))
            elif full.is_dir():
                out.append(st.Entry(rel, "dir", mode, 0))
            else:
                out.append(st.Entry(rel, "file", mode, os.lstat(full).st_size,
                                    st.sha256_file(full)))
    return sorted(out, key=lambda e: e.rel)


def binding(root: Path) -> dict:
    """The `children` binding of `root`, computed independently of the
    registry, in the form `classify_tier` measures each child."""
    out = {}
    for child in sorted(root.iterdir()):
        if child.is_symlink():
            out[child.name] = {"type": "symlink", "entries_digest": st.entries_digest([])}
        elif child.is_dir():
            out[child.name] = {"type": "dir", "entries_digest": st.entries_digest(_entries(child))}
        else:
            entry = st.Entry(".", "file", os.lstat(child).st_mode & 0o7777,
                             child.stat().st_size, st.sha256_file(child))
            out[child.name] = {"type": "file", "entries_digest": st.entries_digest([entry])}
    return out


def archive_into(repo: Path, commit: str, target: Path) -> None:
    """`git archive <commit> | tar -x` into `target` (a reviewer's tree copy)."""
    target.mkdir(parents=True)
    data = subprocess.run(["git", "-C", str(repo), "archive", commit], capture_output=True,
                          check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(target, filter="tar")


def commit_file(repo: Path, name: str, text: str) -> dict[str, str]:
    """Commit `name` and return its `{commit, file, blob}` citation."""
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"add {name}")
    return {"commit": git(repo, "rev-parse", "HEAD"), "file": name,
            "blob": git(repo, "rev-parse", f"HEAD:{name}")}


class World:
    """A registry with project `a` (trust root `proj-a`, two content commits)
    and one reviewer dir `scratch/s0/reviews/r1` holding a `git archive` copy
    of the first content commit."""

    def __init__(self, tmp_path: Path, fake: FakeOS) -> None:
        self.tmp = tmp_path
        self.fake = fake
        self.reg = new_registry(tmp_path, fake)
        self.reg.init()
        self.repo = make_repo(tmp_path / "proj-a")
        (self.repo / "tool.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        os.chmod(self.repo / "tool.sh", 0o755)
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
        os.symlink("mod.py", self.repo / "pkg" / "alias.py")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "content")
        self.old = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "pkg" / "mod.py").write_text("x = 2\n", encoding="utf-8")
        git(self.repo, "commit", "-q", "-am", "later")
        self.later = git(self.repo, "rev-parse", "HEAD")
        self.reg.register_project("a", self.repo)
        self.reviews = self.reg.state_root / "projects" / "a" / "scratch" / "s0" / "reviews"
        self.root = self.reviews / "r1"
        archive_into(self.repo, self.old, self.root / "tree")
        self._n = 0

    def brief(self, text: str | None = None, name: str = "dogfood/x/r1.md",
              root: Path | None = None) -> dict[str, str]:
        text = text if text is not None else f"Scratch: {root or self.root}/ (yours alone)\n"
        (self.reviews / Path(name).name).write_text(text, encoding="utf-8")
        return commit_file(self.repo, name, text)

    def report(self, name: str | None = None) -> dict[str, str]:
        self._n += 1
        return commit_file(self.repo, name or f"dogfood/x/r1-report-{self._n}.md",
                           f"release: yes ({self._n})\n")

    def anchor(self, *, designations, reports, children: dict | None = None,
               project: str = "a", path: Path | None = None, kind: str = "legacy_review",
               file: str = "dogfood/storage-anchors/legacy.json") -> dict[str, str]:
        target = path or self.root
        entry = {"path": str(target), "kind": kind, "project_id": project,
                 "designations": designations, "reports": reports,
                 "children": children if children is not None else binding(target)}
        self._n += 1
        cited = commit_file(self.repo, file, json.dumps(
            {"schema": SCHEMA, "roots": [entry], "n": self._n}, indent=2))
        return {"commit": cited["commit"], "file": cited["file"]}

    def register(self, **kw) -> dict:
        anchor = self.anchor(designations=[self.brief()], reports=[self.report()], **kw)
        return self.reg.register_legacy_root("a", self.root, "legacy_review", anchor)


@pytest.fixture
def world(tmp_path, fake) -> World:
    return World(tmp_path, fake)


def refused(world: World, anchor: dict, path: Path | None = None, match: str = "") -> None:
    with pytest.raises(StorageOwnershipError, match=match):
        world.reg.register_legacy_root("a", path or world.root, "legacy_review", anchor)
    assert world.reg.owner_of(world.root / "tree" / "README").status == UNKNOWN


# --------------------------------------------------------------------------- #
# K1 -- identification is a committed designation, never a name
# --------------------------------------------------------------------------- #


def test_legacy_root_registered_with_committed_designation(world):
    """Catches: a committed, designated root not adopted. Mutant M1: no index entry."""
    inside = world.root / "tree" / "README"
    assert world.reg.owner_of(inside).status == UNKNOWN          # negative control
    entry = world.register()
    assert entry["state"] == "REGISTERED" and entry["kind"] == "legacy_review"
    assert set(entry["children"]) == {"tree"}
    owned = world.reg.owner_of(inside)
    assert owned.status == OWNED and owned.kind == "legacy_review"
    assert owned.session_id is None and owned.root == world.root
    assert world.reg.owner_of(world.root).status == PROTECTED
    assert world.reg.owner_of(world.reviews).status == UNKNOWN
    assert world.reg.owner_of(world.reviews.parent).status == UNKNOWN
    index = world.reg.state_root / "projects" / "a" / "LEGACY_INDEX.json"
    assert world.reg.owner_of(index).status == PROTECTED
    assert not any(p.name == ".vh-owner.json" for p in world.root.rglob("*"))


def test_uncommitted_brief_refused(world):
    """Catches: a designation that is not committed counts. Mutant M2: the
    blob check falls back to the working tree."""
    text = f"Scratch: {world.root}/\n"
    (world.reviews / "r1.md").write_text(text, encoding="utf-8")
    (world.repo / "dogfood" / "x").mkdir(parents=True)
    (world.repo / "dogfood" / "x" / "r1.md").write_text(text, encoding="utf-8")  # untracked
    blob = subprocess.run(["git", "-C", str(world.repo), "hash-object", "--stdin"],
                          input=text.encode(), capture_output=True, check=True).stdout
    cite = {"commit": git(world.repo, "rev-parse", "HEAD"), "file": "dogfood/x/r1.md",
            "blob": blob.decode().strip()}
    anchor = world.anchor(designations=[cite], reports=[world.report()])
    refused(world, anchor, match="designation")


def test_designation_from_unmerged_commit_refused(world):
    """Catches: a brief committed only on a side branch (never merged) counts.
    Mutant X1: the ancestor-of-HEAD check of `_committed_blob` dropped."""
    git(world.repo, "checkout", "-q", "-b", "side")
    side = world.brief()
    git(world.repo, "checkout", "-q", "-")
    anchor = world.anchor(designations=[side], reports=[world.report()])
    refused(world, anchor, match="not an ancestor of HEAD")


def test_designation_outside_dogfood_refused(world):
    """Catches: any committed file (e.g. a doc that mentions the path) used as a
    designation. Mutant X2: the `dogfood/` check dropped."""
    doc = world.brief(name="docs/r1.md")
    refused(world, world.anchor(designations=[doc], reports=[world.report()]),
            match="not a path under dogfood/")


def test_report_file_is_not_a_designation(world):
    """Catches: a report naming the path counts as its designation. Mutant M3:
    the `-report` filter dropped."""
    report = commit_file(world.repo, "dogfood/x/r1-report.md", f"I worked in {world.root}/\n")
    refused(world, world.anchor(designations=[report], reports=[report]),
            match="no committed designation")


def test_sibling_prefix_not_a_designation(world):
    """Catches: `<root>2/` designates `<root>`. Mutant M4: containment without
    the trailing separator."""
    brief = world.brief(text=f"Scratch: {world.root}2/\n")
    refused(world, world.anchor(designations=[brief], reports=[world.report()]),
            match="no committed designation")


def test_session_id_and_parent_marker_never_suffice(world):
    """Catches: ownership inherited from a bootstrap marker of the session dir
    or from a session id. Mutant M9b: the classifier trusts the parent marker.
    (Green on HEAD: a guard.)"""
    marker = world.reviews.parent / ".vh-owner.json"
    marker.write_text(json.dumps({"project_id": "a", "session_id": "s0", "kind": "scratch"}),
                      encoding="utf-8")
    assert world.reg.owner_of(world.root / "tree" / "README").status == UNKNOWN
    assert world.reg.owner_of(world.root).status == UNKNOWN


def test_anchor_for_other_project_refused(world):
    """Catches: an anchor entry naming another project. Mutant M9: the project
    check skipped."""
    anchor = world.anchor(designations=[world.brief()], reports=[world.report()], project="b")
    refused(world, anchor, match="another project")


def test_reports_required(world):
    """Catches: a root adopted while no report of its reviewer is committed.
    Mutant M10: the empty-reports refusal dropped."""
    refused(world, world.anchor(designations=[world.brief()], reports=[]), match="no reports")


# --------------------------------------------------------------------------- #
# K2 -- structure and content binding
# --------------------------------------------------------------------------- #


def test_path_outside_legacy_pattern_refused(world):
    """Catches: the session dir or `reviews/` itself adopted. Mutant M5: the
    pattern check dropped."""
    for path in (world.reviews, world.reviews.parent):
        anchor = world.anchor(designations=[world.brief(text=f"{path}/\n")],
                              reports=[world.report()], path=path)
        refused(world, anchor, path=path, match="is not <state_root>")


def test_invalid_reviewer_id_refused(world):
    """Catches: a reviewer dir whose name is not a registry id adopted. Mutant
    X9: the `_ID` match of the last component dropped."""
    bad = world.reviews / "Bad Name"
    archive_into(world.repo, world.old, bad / "tree")
    anchor = world.anchor(designations=[world.brief(root=bad)], reports=[world.report()],
                          path=bad)
    with pytest.raises(StorageOwnershipError, match="is not <state_root>"):
        world.reg.register_legacy_root("a", bad, "legacy_review", anchor)


def test_overlap_with_indexed_scratch_refused(world):
    """Catches: a dir inside a registry-created scratch dir adopted a second
    time as legacy. Mutant X3: the overlap check against indexed roots dropped."""
    world.reg.open_session("a", "s5", pid=100, boot_id=BOOT)
    session = world.reg.scratch_dir("a", "s5", "scratch")
    inner = session / "reviews" / "r5"
    archive_into(world.repo, world.old, inner / "tree")
    anchor = world.anchor(designations=[world.brief(root=inner)], reports=[world.report()],
                          path=inner)
    with pytest.raises(StorageOwnershipError, match="overlaps"):
        world.reg.register_legacy_root("a", inner, "legacy_review", anchor)


def test_root_registered_twice_refused(world):
    """Catches: one root registered twice (two index entries, two bindings).
    Mutant X4: the overlap check against other legacy roots dropped."""
    world.register()
    record = json.loads((world.reg.state_root / "projects" / "a" / "LEGACY_INDEX.json")
                        .read_text())
    anchor = record["entries"][0]["anchor"]
    with pytest.raises(StorageOwnershipError, match="already registered"):
        world.reg.register_legacy_root("a", world.root, "legacy_review", anchor)


def test_children_mismatch_refused(world):
    """Catches: content changed after the survey adopted. Mutant M6: the
    binding comparison dropped."""
    anchor = world.anchor(designations=[world.brief()], reports=[world.report()])
    (world.root / "tree" / "probe.txt").write_text("added later\n", encoding="utf-8")
    refused(world, anchor, match="children differ")


@pytest.mark.parametrize("case", ["hardlink", "symlink"])
def test_hardlink_or_escaping_symlink_refused(world, case):
    """Catches: a hard link (other names may be foreign) or a symlink leaving the
    root adopted. Mutants M7/M7b: the per-entry checks dropped."""
    if case == "hardlink":
        os.link(world.root / "tree" / "README", world.tmp / "elsewhere")
        match = "hard links"
    else:
        os.symlink(os.fspath(world.tmp / "proj-a" / "README"), world.root / "tree" / "out")
        match = "leaving the root"
    refused(world, world.anchor(designations=[world.brief()], reports=[world.report()],
                                children={}), match=match)


def test_foreign_uid_refused(world, monkeypatch):
    """Catches: an entry of another uid adopted. Mutant X5: the uid check dropped."""
    victim = world.root / "tree" / "README"
    real = os.lstat

    def lstat(path, *a, **kw):
        result = real(path, *a, **kw)
        if Path(path) == victim:
            fields = list(result)
            fields[4] = result.st_uid + 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(sr, "_legacy_lstat", lstat)
    refused(world, world.anchor(designations=[world.brief()], reports=[world.report()],
                                children={}), match="belongs to uid")


def test_other_device_refused(world):
    """Catches: an entry on another device adopted. Mutant X6: the device check dropped."""
    world.fake.st_devs[world.root / "tree" / "pkg"] = 4242
    refused(world, world.anchor(designations=[world.brief()], reports=[world.report()],
                                children={}), match="another device")


def test_other_mount_refused(world):
    """Catches: a bind mount below the root adopted. Mutant X7: the mount check dropped."""
    world.fake.mount_ids[world.root / "tree" / "pkg"] = 7
    refused(world, world.anchor(designations=[world.brief()], reports=[world.report()],
                                children={}), match="another mount")


def test_root_that_is_a_mount_refused(world):
    """LGS-F2 E3. Catches: a mount point adopted as a legacy root (what is
    below it need not belong to the state root). Mutant E3: `or
    self._mount_check(target)` dropped in `register_legacy_root`. Control: the
    same anchor registers once the path is no longer a mount."""
    anchor = world.anchor(designations=[world.brief()], reports=[world.report()])
    world.fake.mounts.add(world.root)
    refused(world, anchor, match="is not a plain directory or is a mount")
    world.fake.mounts.discard(world.root)
    entry = world.reg.register_legacy_root("a", world.root, "legacy_review", anchor)
    assert entry["state"] == sr.REGISTERED
    assert world.reg.owner_of(world.root / "tree" / "README").status == OWNED


def test_foreign_uid_on_a_child_directory_refused(world, monkeypatch):
    """LGS-F2 E4. Catches: a direct child directory of another uid adopted,
    because the survey checked the entries below it and not the directory
    itself. Mutant E4: `st.st_uid != uid or` dropped for a directory child in
    `legacy_survey_root`. The anchor binds the true content, so the mutant
    would register the root."""
    victim = world.root / "tree"
    real = os.lstat

    class Foreign:
        """The real `lstat` result of `victim` with another uid (every other
        field is the real one, `st_blocks` included, so nothing but the uid
        check can refuse it)."""

        def __init__(self, result):
            self._result = result
            self.st_uid = result.st_uid + 1

        def __getattr__(self, name):
            return getattr(self._result, name)

    def lstat(path, *a, **kw):
        result = real(path, *a, **kw)
        return Foreign(result) if Path(path) == victim else result

    monkeypatch.setattr(sr, "_legacy_lstat", lstat)
    refused(world, world.anchor(designations=[world.brief()], reports=[world.report()]),
            match="foreign or on another mount")


@pytest.mark.parametrize("when", ["at_registration", "after_registration"])
def test_a_hard_link_on_a_direct_file_of_the_root_refused(world, when):
    """LGS-F2 E9. Catches: a regular file directly in the root with a second
    name (which may be foreign) accepted; the entries below a child directory
    are covered by `test_hardlink_or_escaping_symlink_refused`. Mutant E9: the
    `st_nlink > 1` refusal dropped in `_legacy_single`. Registration refuses it;
    after registration the same refusal is drift (HOT)."""
    note = world.root / "notes.md"
    note.write_text("first round\n", encoding="utf-8")
    if when == "at_registration":
        os.link(note, world.tmp / "elsewhere")
        refused(world, world.anchor(designations=[world.brief()], reports=[world.report()]),
                match="hard links")
        return
    world.register()
    assert world.reg.legacy_entry_digest(world.root, note)          # control: measurable
    os.link(note, world.tmp / "elsewhere")
    with pytest.raises(StorageOwnershipError, match="hard links"):
        world.reg.legacy_entry_digest(world.root, note)


def test_swapped_root_inode_unknown(world):
    """Catches: a replaced root keeps its adoption. Mutant M8: `_root_intact`
    skipped in `_legacy_verdict`."""
    world.register()
    os.rename(world.root, world.reviews / "r1-old")
    world.root.mkdir()
    os.rename(world.reviews / "r1-old" / "tree", world.root / "tree")   # same child inode
    assert world.reg.owner_of(world.root / "tree" / "README").status == UNKNOWN


def test_child_added_after_anchor_unknown(world):
    """B1. Catches: a child created after the anchor's survey (a reused dir)
    becomes OWNED. Mutant B1a: the unbound-child check of `_legacy_verdict`
    dropped."""
    world.register()
    archive_into(world.repo, world.old, world.root / "tree-r2")
    assert world.reg.owner_of(world.root / "tree" / "README").status == OWNED   # control
    assert world.reg.owner_of(world.root / "tree-r2" / "README").status == UNKNOWN
    assert world.reg.owner_of(world.root / "tree-r2").status == UNKNOWN


def test_reanchor_descendant_supersedes(world):
    """Catches: a new child can never be bound, or the old entry still
    consulted after re-anchoring. Mutant RA1: the old entry not marked
    SUPERSEDED."""
    world.register()
    archive_into(world.repo, world.old, world.root / "tree-r2")
    anchor = world.anchor(designations=[world.brief(name="dogfood/x/r1-again.md")],
                          reports=[world.report()])
    entry = world.reg.register_legacy_root("a", world.root, "legacy_review", anchor)
    assert set(entry["children"]) == {"tree", "tree-r2"}
    states = [e["state"] for e in json.loads(
        (world.reg.state_root / "projects" / "a" / "LEGACY_INDEX.json").read_text())["entries"]]
    assert states == ["SUPERSEDED", "REGISTERED"]
    assert world.reg.owner_of(world.root / "tree-r2" / "README").status == OWNED


def test_reanchor_with_older_anchor_refused(world):
    """Catches: a root re-anchored by an anchor that does not descend from the
    recorded one (an older or parallel binding). Mutant RA2: the descendant
    check dropped."""
    older = world.anchor(designations=[world.brief(name="dogfood/x/r1-first.md")],
                         reports=[world.report()],
                         file="dogfood/storage-anchors/older.json")
    world.register()
    with pytest.raises(StorageOwnershipError, match="already registered"):
        world.reg.register_legacy_root("a", world.root, "legacy_review", older)


def test_quarantine_name_beside_present_child_unknown(world):
    """Catches: a dir named like a quarantine of a bound child (while that
    child is still present) taken as the child mid-relocation. Mutant Q1: the
    presence check of `_legacy_verdict` dropped."""
    world.register()
    fake_q = world.root / "tree.vh-quarantine.20261004T000000Z"
    archive_into(world.repo, world.later, fake_q)
    assert world.reg.owner_of(fake_q / "README").status == UNKNOWN
    assert world.reg.owner_of(world.root / "tree" / "README").status == OWNED   # control


def test_recreated_child_unknown(world):
    """Catches: a child moved away (or relocated) and re-created with identical
    content inheriting the binding (lgspec-b p5). Mutants C1/C2: the child
    inode check of `_legacy_verdict` / `legacy_child` dropped."""
    world.register()
    assert world.reg.legacy_child(world.root / "tree") is not None      # control
    os.rename(world.root / "tree", world.tmp / "tree-moved-away")
    archive_into(world.repo, world.old, world.root / "tree")
    assert world.reg.owner_of(world.root / "tree" / "README").status == UNKNOWN
    assert world.reg.legacy_child(world.root / "tree") is None
