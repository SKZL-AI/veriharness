"""STORAGE-L: VERIFIED_RELOCATION of a `git_tree` item of an
adopted legacy root (revision 6/6a, K6).

The world is `test_storage_tiers.World` (real registry with injected OS
readers, canonical repository ``canon``, a ``tmp_path`` archive target, a
fake admission). A legacy reviewer dir ``scratch/s0/reviews/r1`` holds a
`git archive` copy of the canonical repository's first commit and is adopted
through a committed legacy-roots anchor before the relocation runs.
"""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path

import hoh.verified_relocation as vr
from hoh import storage_tiers as st
from test_storage_registry_legacy import binding
from test_storage_tiers import PROJECT, World, git, isolated_env  # noqa: F401  (fixtures)
from test_storage_tiers import w as w  # a fixture, found by pytest by name; the alias form is an explicit re-export, so
# a test parameter named `w` is not a redefinition of an unused import (F811)

from test_verified_relocation import by_path, hook, plan_exec, retain_refs


def legacy_tree(world: World) -> tuple[Path, str]:
    """Adopt ``scratch/s0/reviews/r1`` (holding ``tree``, an archive of the
    first commit); returns the tree and that commit."""
    first = git(world.canon, "rev-list", "--max-parents=0", "HEAD")
    reviews = world.registry.state_root / "projects" / PROJECT / "scratch" / "s0" / "reviews"
    root = reviews / "r1"
    (root / "tree").mkdir(parents=True)
    data = subprocess.run(["git", "-C", str(world.canon), "archive", first],
                          capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(root / "tree", filter="tar")
    world.commit("data/a.txt", "alpha, later\n")   # HEAD differs: no VERIFIED_DUPLICATE
    cites = []
    for name, text in (("dogfood/x/r1.md", f"Scratch: {root}/\n"),
                       ("dogfood/x/r1-report.md", "release: yes\n")):
        world.commit(name, text)
        cites.append({"commit": git(world.canon, "rev-parse", "HEAD"), "file": name,
                      "blob": git(world.canon, "rev-parse", f"HEAD:{name}")})
    entry = {"path": str(root), "kind": "legacy_review", "project_id": PROJECT,
             "designations": [cites[0]], "reports": [cites[1]], "children": binding(root)}
    anchor_file = "dogfood/storage-anchors/legacy.json"
    world.commit(anchor_file, json.dumps({"schema": "veriharness.storage-legacy-roots/1",
                                          "roots": [entry]}))
    world.registry.register_legacy_root(
        PROJECT, root, "legacy_review",
        {"commit": git(world.canon, "rev-parse", "HEAD"), "file": anchor_file})
    return root / "tree", first


def test_git_tree_relocates_with_receipt(w):
    """Catches: a git_tree item not relocated, or its receipt without the tree
    and the retain ref. Mutant R1: `tree` dropped from the proof."""
    tree, first = legacy_tree(w)
    w.end_setup()
    assert w.classify(tree).proof["type"] == "git_tree"          # control: eligible
    _, plan, report = plan_exec(w, tree)
    record = by_path(report, tree)
    assert record["status"] == vr.RELOCATED, record.get("reason")
    assert record["proof"]["type"] == "git_tree" and record["proof"]["commit"] == first
    assert record["proof"]["tree"] == git(w.canon, "rev-parse", f"{first}^{{tree}}")
    assert record["retain_ref"]["commit"] == first
    assert retain_refs(w) == [f"{record['retain_ref']['ref']} {first}"]
    assert not tree.exists() and tree.parent.is_dir()


def test_git_tree_changed_after_plan_kept(w):
    """Catches: an item changed between plan and removal removed anyway.
    Mutant R2: the `git_tree` branch of `_recheck_proof` accepts unchecked."""
    tree, _ = legacy_tree(w)
    w.end_setup()

    @hook("after_place")
    def change(item, record):
        (tree / "README").write_text("changed after the plan\n", encoding="utf-8")

    _, _, report = plan_exec(w, tree)
    record = by_path(report, tree)
    assert record["status"] == vr.KEPT and tree.is_dir()
    assert record["reason"].startswith(vr.REPRODUCTION_CHANGED), record["reason"]


def test_git_tree_retain_ref_moved_kept(w):
    """Catches: removal while the retain ref no longer pins the commit.
    Mutant R3: the retain-ref comparison dropped."""
    tree, _ = legacy_tree(w)
    w.end_setup()
    other = git(w.canon, "rev-parse", "HEAD")

    @hook("after_place")
    def move(item, record):
        git(w.canon, "update-ref", record["retain_ref"]["ref"], other)

    _, _, report = plan_exec(w, tree)
    record = by_path(report, tree)
    assert record["status"] == vr.KEPT
    assert record["reason"].startswith(vr.REPRODUCTION_REF_MOVED) and tree.is_dir()


def test_legacy_root_never_planned(w):
    """Catches: the PROTECTED legacy root itself planned. Mutant R4: the root
    classified OWNED."""
    tree, _ = legacy_tree(w)
    w.end_setup()
    rel = w.relocation()
    plan = rel.plan(PROJECT, [tree.parent, tree])
    assert [i["path"] for i in plan.items] == [str(tree)]
    [rejected] = plan.rejected
    assert rejected["path"] == str(tree.parent) and rejected["refused"] == st.UNKNOWN_OWNERSHIP
    assert "PROTECTED" in rejected["reason"], rejected["reason"]


def test_binding_changed_after_plan_kept(w):
    """Catches: an item removed although its legacy binding no longer matches
    the planned content (the index entry replaced between plan and execute).
    Mutant R5: the LEGACY_UNBOUND re-check of `_recheck_proof` dropped."""
    tree, _ = legacy_tree(w)
    w.end_setup()
    index = w.registry.state_root / "projects" / PROJECT / "LEGACY_INDEX.json"

    @hook("after_place")
    def rebind(item, record):
        data = json.loads(index.read_text())
        data["entries"][-1]["children"]["tree"]["entries_digest"] = "0" * 64
        index.write_text(json.dumps(data))

    _, _, report = plan_exec(w, tree)
    record = by_path(report, tree)
    assert record["status"] == vr.KEPT and tree.is_dir()
    assert record["reason"].startswith(st.LEGACY_UNBOUND), record["reason"]


# --------------------------------------------------------------------------- #
# Revision 6 -- the legacy liveness is read again in quarantine
# --------------------------------------------------------------------------- #


def _kept_after(w, change):
    tree, _ = legacy_tree(w)
    w.end_setup()

    @hook("after_place")
    def act(item, record):
        change(tree.parent)

    _, _, report = plan_exec(w, tree)
    return tree, by_path(report, tree)


def test_drift_after_plan_kept(w):
    """lgspec-a r4 S1 / lgspec-b ra. Catches: a new round dir appearing in R
    after the plan, and the item removed anyway. Mutant G1: no legacy re-check
    in quarantine."""
    def new_round(root):
        (root / "r2").mkdir()
        (root / "r2" / "notes.txt").write_text("round 2\n", encoding="utf-8")
    tree, record = _kept_after(w, new_round)
    assert record["status"] == vr.KEPT and tree.is_dir()
    assert record["reason"].startswith(vr.LEGACY_HOT) and "legacy_drift" in record["reason"]


def test_uncited_committed_brief_after_plan_kept(w):
    """lgspec-a r4 S2. Catches: a committed, uncited brief naming R appearing
    after the plan. Mutant G1."""
    tree, record = _kept_after(w, lambda root: w.commit("dogfood/y/again.md",
                                                       f"Scratch: {root}/ again\n"))
    assert record["status"] == vr.KEPT and tree.is_dir()
    assert record["reason"].startswith(vr.LEGACY_HOT) and "legacy_reuse" in record["reason"]


def test_report_rewritten_after_plan_kept(w):
    """Catches: the anchor report rewritten after the plan. Mutant G1."""
    tree, record = _kept_after(w, lambda root: w.commit("dogfood/x/r1-report.md",
                                                       "release: no\n"))
    assert record["status"] == vr.KEPT and tree.is_dir()
    assert record["reason"].startswith(vr.LEGACY_HOT) and "legacy_report" in record["reason"]


def test_uncommitted_brief_after_plan_kept(w):
    """lgspec-a r4 S3 (control): an uncommitted brief naming R after the plan
    was already kept by the reference re-check of HEAD, before the quarantine."""
    tree, record = _kept_after(w, lambda root: (root.parent / "again.md").write_text(
        f"Scratch: {root}/tree\n", encoding="utf-8"))
    assert record["status"] == vr.KEPT and tree.is_dir()


def test_liveness_recheck_raises_item_kept(w, monkeypatch):
    """Revision 6a (lgspec-a r5 M1). Catches: the legacy liveness re-check in
    quarantine raising, and the item relocated anyway. Mutant Y1: the
    exception swallowed (no reasons), item relocated."""
    tree, _ = legacy_tree(w)
    w.end_setup()
    before = binding(tree.parent)
    calls = []

    def broken(root, sources):
        calls.append(Path(root))
        raise RuntimeError("liveness source exploded")

    monkeypatch.setattr(st, "legacy_liveness", broken)
    _, _, report = plan_exec(w, tree)
    record = by_path(report, tree)
    assert calls == [tree.parent]                  # control: the re-check ran in quarantine
    assert record["status"] == vr.KEPT, record.get("reason")
    assert record["reason"].startswith(vr.LEGACY_HOT), record["reason"]
    assert "liveness source exploded" in record["reason"]
    assert tree.is_dir() and binding(tree.parent) == before     # restored, unchanged
    assert sorted(p.name for p in tree.parent.iterdir()) == ["tree"]   # no quarantine left
