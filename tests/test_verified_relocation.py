"""VERIFIED_RELOCATION, the one removing operation (STORAGE-D, P5-29).

The world is `test_storage_tiers.World`: a real registry with injected OS
readers (the setup session's process ends with `end_setup`, the relocation
runs as fake pid 300), a canonical git repository, owned scratch items, a
``tmp_path`` archive target with injected identity and health probes, and a
fake admission. Bundles are real (`archive_bundle`), placements are real
(`archive_target.place` verifies on the destination and writes the receipt),
the coordinator's driver lease is real (`storage_coordinator`).

Points inside `execute` are reached through the module's private seam
`verified_relocation._hooks` (``after_lock``, ``before_build``,
``after_place``, ``after_intent``, ``after_quarantine``, ``after_compare``,
``before_inode_check``, ``before_unlink``, ``after_rehash``, ``after_removal``,
``before_hold``, ``before_extract_gate``) and
`_unlink_entry`; each test resets them. An interrupted execute is simulated
by a `BaseException` raised at a seam (`Crash`), which nothing in `execute`
turns into a receipt.

Every test name contains the selector it is run by; every docstring names the
failure (the mutant) it catches and the negative control that changes only the
condition under test. Tests never remove anything: a "user deletion" from
reclaim-review is simulated by renaming the bundle out of the area.

Revision 11 item 6 (recorded): Revision 10 item 5 forced setup changes in
`test_extract_root_with_extra_file_kept` and
`test_extract_root_removal_set_is_compared_inventory` (and in
`test_storage_steward.py::test_candidate_plan_bytes_not_overclaimed`): each
now registers the target ``t9`` that holds the placement, because an extract
root under no registered target is kept. Their assertions are unchanged; the
setup changes are authorised retroactively by Revision 11 item 6.
"""

from __future__ import annotations

import ast
import errno
import fcntl
import inspect
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

import hoh.archive_bundle as ab
import hoh.archive_target as at
import hoh.verified_relocation as vr
from hoh import storage_coordinator as sc
from hoh import storage_registry as sr
from hoh import storage_tiers as st
from hoh.storage_coordinator import StorageCoordinator
from hoh.store import RunStore
from test_storage_tiers import (  # noqa: F401  (isolated_env and w are fixtures)
    BOOT,
    OTHER,
    OTHER_PID,
    PROJECT,
    RELOC_PID,
    SETUP_PID,
    FakeAdmission,
    GiB,
    World,
    git,
    isolated_env,
    w,
)

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def hook(name):
    """Decorator: install `fn` at the named seam of `execute`."""
    def install(fn):
        vr._hooks[name] = fn
        return fn
    return install


def plan_exec(world: World, *items, **kw):
    rel = kw.pop("rel", None) or world.relocation(**kw)
    plan = rel.plan(PROJECT, list(items))
    return rel, plan, rel.execute(plan.path)


def by_path(report, path) -> dict:
    return next(i for i in report.items if i["path"] == str(path))


def retain_refs(world: World) -> list[str]:
    out = git(world.canon, "for-each-ref", "--format=%(refname) %(objectname)",
              "refs/vh-retain/")
    return [line for line in out.splitlines() if line]


def runs_world(world: World, *names, stage="VERIFYING", condition="FAILED", text="same\n"):
    """Finished runs whose ``out/f.txt`` hold identical bytes, all listed in
    one committed retention manifest; returns the run directories."""
    runs = [world.run(name, condition=condition, stage=stage, files={"out/f.txt": text})
            for name in names]
    world.retain([r / "out" / "f.txt" for r in runs])
    return runs


def copy_of(world: World, source: Path, name: str) -> Path:
    import shutil
    target = world.scratch / name
    shutil.copytree(source, target)
    return target


# --------------------------------------------------------------------------- #
# K3 -- what may never be planned, and what relocates
# --------------------------------------------------------------------------- #


def test_current_evidence_never_planned(w):
    """S14. Catches the evidence collector removed: a duplicate named by a
    committed run-evidence file is rejected by `plan` (HOT) and stays.
    Negative control: the same duplicate unreferenced is planned."""
    item, free = w.copy("dup"), w.copy("free")
    w.end_setup()
    w.commit("dogfood/r/run-evidence/e.json", json.dumps({"temp_dirs": [str(item)]}))
    rel = w.relocation()
    plan = rel.plan(PROJECT, [item, free])
    assert [i["path"] for i in plan.items] == [str(free)]
    [rejected] = plan.rejected
    assert rejected["path"] == str(item) and "current_evidence" in rejected["reason"]
    rel.execute(plan.path)
    assert item.is_dir()


def test_open_finding_never_planned(w):
    """S15. Catches the findings collector removed: a duplicate named in an
    open DOGFOOD_LEDGER.md record is rejected and stays. Negative control:
    once the record moves to a closed section it is planned."""
    item = w.copy("dup")
    w.end_setup()
    w.ledger(f"## Offen\n\n- {item} still compared by a reviewer\n")
    plan = w.relocation().plan(PROJECT, [item])
    assert not plan.items and "open_finding" in plan.rejected[0]["reason"]
    w.ledger(f"## Erledigt\n\n- {item} compared\n")
    assert [i["path"] for i in w.relocation().plan(PROJECT, [item]).items] == [str(item)]


def test_duplicate_relocates_with_receipt(w):
    """S16. Catches a ReclaimReceipt missing a field (and a relocation that
    skips the destination receipt): a verified duplicate is placed into
    reclaim-review/<p>/ with ARCHIVE_RECEIPT.json, its local copy is gone,
    the canonical original is untouched, and the ReclaimReceipt holds the
    plan digest, path, proof, bundle receipt digest, time, post-delete
    absence and the retained originals. Negative control: before execute the
    item and no bundle exist."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    area = target.root / at.RECLAIM_REVIEW / PROJECT
    assert item.is_dir() and not area.exists()
    rel, plan, report = plan_exec(w, item, target=target)
    assert report.status == vr.DONE
    record = by_path(report, item)
    assert record["status"] == vr.RELOCATED, record["reason"]
    assert not item.exists() and record["post_delete_absence"] is True
    for key in ("plan_digest", "path", "proof", "proof_digest", "bundle_receipt_digest",
                "time_utc", "post_delete_absence", "retained_originals", "retain_ref",
                "bundle_id", "bundle_path", "classification", "receipt_id"):
        assert record.get(key) not in (None, "", []), key
    assert record["plan_digest"] == plan.digest
    bundle = Path(record["bundle_path"])
    assert bundle.parent == area and (bundle / ab.RECEIPT).is_file()
    assert st.sha256_file(bundle / ab.RECEIPT) == record["bundle_receipt_digest"]
    assert (w.canon / "data" / "a.txt").read_text() == "alpha\n"
    on_disk = json.loads(Path(record["receipt_path"]).read_text())
    assert on_disk["status"] == vr.RELOCATED and on_disk["plan_digest"] == plan.digest
    assert {o["path"] for o in on_disk["retained_originals"]} == {
        str(w.canon / "data" / "a.txt"), str(w.canon / "data" / "sub" / "b.txt")}


def test_noncanonical_original_rejected(w):
    """Catches the canonical check removed: a copy of another owned scratch
    item (not of the canonical repository) is not a VERIFIED_DUPLICATE and
    is never planned. Negative control: a copy of the canonical directory
    is planned."""
    source = w.scratch / "notes"
    source.mkdir()
    (source / "n.txt").write_text("only here\n")
    copy = copy_of(w, source, "notes-copy")
    good = w.copy("dup")
    w.end_setup()
    plan = w.relocation().plan(PROJECT, [copy, good])
    assert [i["path"] for i in plan.items] == [str(good)]
    assert "NOT_ELIGIBLE" in plan.rejected[0]["reason"]


def test_untracked_file_keeps_reproducible_item(w):
    """Catches untracked files ignored by the reproduction proof: a clone
    with one untracked file is not REPRODUCIBLE_SCRATCH (note
    ``untracked_files``) and is never planned. Negative control: the clone
    without it is planned."""
    clone, other = w.clone("clone"), w.clone("clean")
    (clone / "scratch.txt").write_text("not in git\n")
    w.end_setup()
    decision = w.classify(clone)
    assert decision.tier != st.REPRODUCIBLE_SCRATCH
    assert "untracked_files" in decision.notes
    plan = w.relocation().plan(PROJECT, [clone, other])
    assert [i["path"] for i in plan.items] == [str(other)]


def test_ignored_file_keeps_reproducible_item(w):
    """Catches ``--ignored`` dropped: a clone with a file its .gitignore
    ignores is not REPRODUCIBLE_SCRATCH (note ``ignored_files``) and is never
    planned. Negative control: the same clone with the ignored file moved
    out is reproducible."""
    w.commit(".gitignore", "*.log\n")
    clone = w.clone("clone")
    (clone / "build.log").write_text("ignored output\n")
    w.end_setup()
    decision = w.classify(clone)
    assert "ignored_files" in decision.notes and decision.tier != st.REPRODUCIBLE_SCRATCH
    assert not w.relocation().plan(PROJECT, [clone]).items
    (clone / "build.log").rename(w.tmp / "build.log")
    assert w.classify(clone).tier == st.REPRODUCIBLE_SCRATCH


def test_stash_keeps_reproducible_item(w):
    """Catches the stash check dropped: a clean clone with a stash entry is
    not REPRODUCIBLE_SCRATCH (note ``stash``) and is never planned. Negative
    control: a clean clone without a stash is."""
    clone = w.clone("clone")
    (clone / "README").write_text("work in progress\n")
    git(clone, "stash", "-q")
    w.end_setup()
    decision = w.classify(clone)
    assert "stash" in decision.notes and decision.tier != st.REPRODUCIBLE_SCRATCH
    assert not w.relocation().plan(PROJECT, [clone]).items
    assert w.classify(w.clone("fresh")).tier == st.REPRODUCIBLE_SCRATCH


def test_retain_ref_exists_before_removal(w):
    """Catches the retain ref created after removal or not at all: when the
    first entry of a reproducible clone is about to be unlinked,
    ``refs/vh-retain/<receipt-id>`` already names the clone's commit in the
    canonical repository, and it stays afterwards. Negative control: before
    execute no retain ref exists."""
    clone = w.clone("clone")
    w.end_setup()
    head = git(w.canon, "rev-parse", "HEAD")
    assert retain_refs(w) == []
    seen = []

    @hook("before_unlink")
    def check(item, quarantine, record):
        seen.append((record["receipt_id"], retain_refs(w)))

    _, _, report = plan_exec(w, clone)
    record = by_path(report, clone)
    assert record["status"] == vr.RELOCATED
    [(receipt_id, refs)] = seen
    assert refs == [f"refs/vh-retain/{receipt_id} {head}"]
    assert retain_refs(w) == refs and record["retain_ref"]["commit"] == head


def test_uncommitted_original_rejected(w):
    """Catches a working-tree file accepted as original: a copy of a
    directory whose file is modified but not committed in the canonical
    repository, one holding an untracked canonical file, and a copy of the
    committed state while the canonical file is modified, are not
    duplicates. Negative control: after the change is committed, the copy of
    the new state is planned."""
    committed = w.copy("committed")         # the committed state, copied first
    (w.canon / "data" / "a.txt").write_text("edited, not committed\n")
    (w.canon / "data" / "loose.txt").write_text("untracked\n")
    item = w.copy("dup")
    w.end_setup()
    assert w.classify(item).tier != st.VERIFIED_DUPLICATE
    assert not w.relocation().plan(PROJECT, [item]).items
    # Equal to the committed blob, but the canonical file is modified now:
    # a modified file is never an original (Revision 3 item 3).
    decision = w.classify(committed)
    assert decision.tier != st.VERIFIED_DUPLICATE
    assert any("is modified" in n for n in decision.notes)
    git(w.canon, "add", "-A")
    git(w.canon, "commit", "-qm", "commit the edit")
    assert [i["path"] for i in w.relocation().plan(PROJECT, [item]).items] == [str(item)]


def test_listed_original_never_planned(w):
    """Catches the pin ignored (Revision 4 item 2, "a listed original is
    pinned: it is never itself planned"): (a) a run directory holding a file
    a committed retention manifest lists is rejected PINNED_ORIGINAL by a
    plan of its own -- no earlier plan needed -- although its tier is
    VERIFIED_DUPLICATE; (b) an original a frozen plan relies on stays pinned
    after its listing is retired, so a later plan rejects it too. Negative
    control: the same run directory, unlisted while its twin is listed, is
    planned."""
    r1, r2 = runs_world(w, "20261001t0001z-ra", "20261001t0002z-rb")
    w.end_setup()
    rel = w.relocation()
    assert w.classify(r1 / "out").tier == st.VERIFIED_DUPLICATE   # it could be relied on
    listed = rel.plan(PROJECT, [r1 / "out"])
    assert not listed.items and vr.PINNED_ORIGINAL in listed.rejected[0]["reason"]
    # negative control: only the twin listed -> r1/out is an ordinary duplicate
    w2 = World(w.tmp.parent / "w2")
    q1, q2 = [w2.run(n, files={"out/f.txt": "same\n"})
              for n in ("20261001t0003z-ra", "20261001t0004z-rb")]
    w2.retain([q2 / "out" / "f.txt"])
    w2.end_setup()
    rel2 = w2.relocation()
    first = rel2.plan(PROJECT, [q1 / "out"])
    assert [i["path"] for i in first.items] == [str(q1 / "out")]
    assert [o["path"] for o in first.items[0]["originals"]] == [str(q2 / "out" / "f.txt")]
    # (b) retire that listing (renamed, never removed) and list q1's file
    # instead: q2/out is no longer listed, but the frozen plan still relies on it
    manifest = f"{st.RETENTION_DIR}/20261002T000000Z.json"
    git(w2.canon, "mv", manifest, manifest + ".v1.retired")
    git(w2.canon, "commit", "-qm", "retire the manifest")
    w2.retain([q1 / "out" / "f.txt"], name="20261002T010000Z.json")
    assert str(q2 / "out" / "f.txt") not in w2.sources().retention()
    assert w2.classify(q2 / "out").tier == st.VERIFIED_DUPLICATE
    second = rel2.plan(PROJECT, [q2 / "out"])
    assert not second.items and vr.PINNED_ORIGINAL in second.rejected[0]["reason"]


def test_original_bytes_differ_from_manifest_rejected(w):
    """Catches the manifest SHA-256 not compared (at plan and at execute): a
    listed original whose bytes differ from its manifest entry is no
    original; and an original changed after planning keeps the duplicate at
    execute (DUPLICATE_CHANGED, re-read before removal). Negative control:
    unchanged, the duplicate relocates."""
    r1, r2 = runs_world(w, "20261001t0011z-ra", "20261001t0012z-rb")
    w.end_setup()
    copy = copy_of(w, r1 / "out", "copy")
    (r2 / "out" / "f.txt").write_text("drifted\n")
    (r1 / "out" / "f.txt").write_text("drifted too\n")
    assert w.classify(copy).tier != st.VERIFIED_DUPLICATE
    # second world: the original changes only after planning
    w2 = World(w.tmp.parent / "w2")
    q1, q2 = runs_world(w2, "20261001t0013z-ra", "20261001t0014z-rb")
    w2.end_setup()
    copy2 = copy_of(w2, q1 / "out", "copy")

    @hook("after_place")
    def drift(item, record):
        for original in item["originals"]:
            Path(original["path"]).write_text("changed after planning\n")

    _, _, report = plan_exec(w2, copy2)
    record = by_path(report, copy2)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.DUPLICATE_CHANGED)
    assert copy2.is_dir()
    vr._hooks.clear()
    w3 = World(w.tmp.parent / "w3")
    p1, _ = runs_world(w3, "20261001t0015z-ra", "20261001t0016z-rb")
    w3.end_setup()
    copy3 = copy_of(w3, p1 / "out", "copy")
    _, _, report = plan_exec(w3, copy3)
    assert by_path(report, copy3)["status"] == vr.RELOCATED


def test_retention_manifest_changed_refuses_execute(w):
    """Catches the manifest entries not recorded in the plan: when the
    committed retention entry of an original the plan relies on changes
    before execute, the whole execute is refused
    (RETENTION_MANIFEST_CHANGED) and nothing is touched. Negative control:
    an unrelated evidence commit (a new manifest for another file) does not
    refuse it."""
    r1, r2 = runs_world(w, "20261001t0021z-ra", "20261001t0022z-rb")
    w.end_setup()
    copy = copy_of(w, r1 / "out", "copy")
    rel = w.relocation()
    plan = rel.plan(PROJECT, [copy])
    [original] = plan.items[0]["originals"]
    w.retain([Path(original["path"])], name="20261002T010000Z.json",
             digests={Path(original["path"]): "0" * 64})
    report = rel.execute(plan.path)
    assert report.status == vr.REFUSED
    assert report.detail.startswith(vr.RETENTION_MANIFEST_CHANGED) and copy.is_dir()
    w2 = World(w.tmp.parent / "w2")
    q1, _ = runs_world(w2, "20261001t0023z-ra", "20261001t0024z-rb")
    extra = w2.run("20261001t0025z-rc", files={"other.txt": "x\n"})
    w2.end_setup()
    copy2 = copy_of(w2, q1 / "out", "copy")
    rel2 = w2.relocation()
    plan2 = rel2.plan(PROJECT, [copy2])
    w2.retain([extra / "other.txt"], name="20261002T020000Z.json")
    assert rel2.execute(plan2.path).status == vr.DONE and not copy2.exists()


def test_nonterminal_run_original_rejected(w):
    """Catches the terminal check dropped: an original in a run that is
    ACTIVE/DEVELOPING is no original, so its copy is never planned.
    Negative control: the same run FAILED makes the copy plannable."""
    r1, r2 = runs_world(w, "20261001t0031z-ra", "20261001t0032z-rb",
                        condition="ACTIVE", stage="DEVELOPING")
    w.end_setup()
    copy = copy_of(w, r1 / "out", "copy")
    assert not w.relocation().plan(PROJECT, [copy]).items
    w.set_run(r2, condition="FAILED", stage="DEVELOPING")
    assert len(w.relocation().plan(PROJECT, [copy]).items) == 1


def test_ignored_but_retained_original_accepted(w):
    """Catches the retention manifest ignored: a runs base inside the
    canonical repository that git ignores holds a finished run whose file is
    listed in a committed retention manifest; its copy relocates, the
    original stays. Negative control: `test_unlisted_runs_original_rejected`
    (the same, unlisted)."""
    w.commit(".gitignore", "runs/\n")
    w.runs_base(parent=w.canon)
    run = w.run("20261001t0041z-ok", files={"out/f.txt": "kept by manifest\n"})
    w.retain([run / "out" / "f.txt"])
    w.end_setup()
    copy = copy_of(w, run / "out", "copy")
    _, _, report = plan_exec(w, copy)
    assert by_path(report, copy)["status"] == vr.RELOCATED
    assert (run / "out" / "f.txt").read_text() == "kept by manifest\n"


def test_unlisted_runs_original_rejected(w):
    """Catches the retention manifest not required: a finished run's file in
    a registered runs base that no committed manifest lists is no original.
    Negative control: once listed, the copy is planned."""
    run = w.run("20261001t0051z-ok", files={"out/f.txt": "not listed\n"})
    w.end_setup()
    copy = copy_of(w, run / "out", "copy")
    assert not w.relocation().plan(PROJECT, [copy]).items
    w.retain([run / "out" / "f.txt"])
    assert len(w.relocation().plan(PROJECT, [copy]).items) == 1


def test_checkpointed_run_without_process_original_accepted(w):
    """Catches the stage ignored (Revision 5a): an ACTIVE run at stage
    CHECKPOINTED with no live process counts as finished; its copy relocates.
    Negative control: the same run at stage DEVELOPING does not."""
    r1, r2 = runs_world(w, "20261001t0061z-ra", "20261001t0062z-rb",
                        condition="ACTIVE", stage="DEVELOPING")
    w.end_setup()
    copy = copy_of(w, r1 / "out", "copy")
    assert not w.relocation().plan(PROJECT, [copy]).items
    w.set_run(r2, condition="ACTIVE", stage="CHECKPOINTED")
    _, _, report = plan_exec(w, copy)
    assert by_path(report, copy)["status"] == vr.RELOCATED


def test_checkpointed_run_with_live_process_original_rejected(w):
    """Catches the process check dropped: a CHECKPOINTED run whose
    `.process.lock` is held by a live process is not finished; the copy of
    its file is never planned. Negative control: after the process ended
    the copy is planned."""
    r1, r2 = runs_world(w, "20261001t0071z-ra", "20261001t0072z-rb",
                        condition="ACTIVE", stage="CHECKPOINTED")
    w.set_run(r1, condition="ACTIVE", stage="DEVELOPING")
    w.end_setup()
    copy = copy_of(w, r1 / "out", "copy")
    with RunStore(r2.parent, r2.name).process_marker():
        assert not w.relocation().plan(PROJECT, [copy]).items
    assert len(w.relocation().plan(PROJECT, [copy]).items) == 1
    # A live holder of .process.lock whose process.json still says "ended"
    # (a new process before it rewrote the marker): only the lock tells.
    import fcntl
    fd = os.open(r2 / ".process.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert json.loads((r2 / "process.json").read_text())["state"] == "ended"
        assert not w.relocation().plan(PROJECT, [copy]).items
    finally:
        os.close(fd)


def test_other_project_item_rejected(w, tmp_path):
    """Catches the project filter dropped: a duplicate owned by another
    project is rejected by this project's plan (OTHER_PROJECT) and stays,
    even though it is eligible for its own project. Negative control: the
    other project's own plan accepts it."""
    canon_x = tmp_path / "canon-x"
    (canon_x / "data").mkdir(parents=True)
    (canon_x / "data" / "x.txt").write_text("x\n")
    git(canon_x, "init", "-q")
    git(canon_x, "add", "-A")
    git(canon_x, "commit", "-qm", "x")
    w.registry.register_project(OTHER, canon_x)
    w.registry.open_session(OTHER, "s1", pid=OTHER_PID, boot_id=BOOT)
    scratch_x = w.registry.scratch_dir(OTHER, "s1", "scratch")
    item = copy_of(w, canon_x / "data", "unused")
    item.rename(scratch_x / "dup-x")
    item = scratch_x / "dup-x"
    w.fake.procs.pop(OTHER_PID)
    w.end_setup()
    rel = w.relocation()
    plan = rel.plan(PROJECT, [item])
    assert not plan.items and plan.rejected[0]["reason"].startswith("OTHER_PROJECT")
    rel.execute(plan.path)
    assert item.is_dir()
    own = vr.VerifiedRelocation(w.registry, [], None, w.receipts,
                                sources=lambda p: w.sources(p))
    assert [i["path"] for i in own.plan(OTHER, [item]).items] == [str(item)]


def test_canonical_original_never_removed(w):
    """Catches an original accepted as a candidate: a plan holding a copy
    and the run directory whose listed file that copy relies on rejects the
    run directory (PINNED_ORIGINAL), and after execute the original is
    byte-for-byte where it was while the copy is gone. Negative control: the
    run directory's own tier is VERIFIED_DUPLICATE -- only being an original
    keeps it out of the plan."""
    r1, r2, r3 = runs_world(w, "20261001t0081z-ra", "20261001t0082z-rb",
                            "20261001t0083z-rc")
    w.end_setup()
    assert w.classify(r1 / "out").tier == st.VERIFIED_DUPLICATE
    copy = copy_of(w, r1 / "out", "copy")
    rel, plan, report = plan_exec(w, copy, r1 / "out")
    [item] = plan.items
    assert item["path"] == str(copy)
    relied = Path(item["originals"][0]["path"])
    assert relied == r1 / "out" / "f.txt"
    [rejected] = plan.rejected
    assert rejected["path"] == str(r1 / "out") and vr.PINNED_ORIGINAL in rejected["reason"]
    assert by_path(report, copy)["status"] == vr.RELOCATED and not copy.exists()
    for run in (r1, r2, r3):
        assert (run / "out" / "f.txt").read_text() == "same\n"


# --------------------------------------------------------------------------- #
# K4 -- execute re-checks every precondition and fails closed per item
# --------------------------------------------------------------------------- #


def test_precondition_duplicate_changed_kept(w):
    """Catches the re-check against the current original removed: when the
    canonical original changes after the bundle was placed, the duplicate is
    kept (DUPLICATE_CHANGED). Negative control: without the change it
    relocates (`test_duplicate_relocates_with_receipt`), and here the second
    item of the same plan does."""
    item = w.copy("dup")
    other = w.copy("other", rel="data/sub")
    w.end_setup()

    @hook("after_place")
    def change(item, record):
        if item["path"].endswith("/dup"):
            (w.canon / "data" / "a.txt").write_text("changed after planning\n")

    _, _, report = plan_exec(w, item, other)
    kept = by_path(report, item)
    assert kept["status"] == vr.KEPT and kept["reason"].startswith(vr.DUPLICATE_CHANGED)
    assert item.is_dir() and (item / "a.txt").read_text() == "alpha\n"
    assert by_path(report, other)["status"] == vr.RELOCATED    # its original is unchanged
    assert not other.exists()


def test_precondition_reproduction_ref_moved_kept(w):
    """Catches the reproduction re-check removed: when the retain ref is
    moved to another commit after placement, the clone is kept
    (REPRODUCTION_REF_MOVED). Negative control: an unmoved ref relocates the
    clone (`test_retain_ref_exists_before_removal`)."""
    clone = w.clone("clone")
    w.end_setup()
    other = w.commit("later.txt", "later\n")

    @hook("after_place")
    def move(item, record):
        git(w.canon, "update-ref", record["retain_ref"]["ref"], other)

    _, _, report = plan_exec(w, clone)
    record = by_path(report, clone)
    assert record["status"] == vr.KEPT
    assert record["reason"].startswith(vr.REPRODUCTION_REF_MOVED) and clone.is_dir()


def test_quarantine_mismatch_restores(w):
    """Catches the quarantine re-inventory removed: a file changed inside
    the quarantined item makes it differ from the bundle manifest; it gets
    its original name back, unremoved (QUARANTINE_MISMATCH). Negative
    control: unchanged it is removed (`test_duplicate_relocates_with_receipt`)."""
    item = w.copy("dup")
    w.end_setup()

    @hook("after_quarantine")
    def change(item, quarantine, record):
        (quarantine / "a.txt").write_text("written during quarantine\n")

    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.QUARANTINE_MISMATCH)
    assert (item / "a.txt").read_text() == "written during quarantine\n"
    assert (item / "sub" / "b.txt").is_file()
    assert not Path(record["quarantine_path"]).exists()


def test_open_fd_keeps_item(w):
    """Catches the fd check removed: a descriptor this (same-uid) process
    holds open on a file inside the item keeps it, restored to its name
    (OPEN_FD, read from the real /proc). Negative control: after closing the
    descriptor the next plan relocates it."""
    item = w.copy("dup")
    w.end_setup()
    held = []

    @hook("after_place")
    def open_one(item, record):
        held.append(os.open(Path(item["path"]) / "a.txt", os.O_RDONLY))

    try:
        _, _, report = plan_exec(w, item, proc_root="/proc")
    finally:
        for fd in held:
            os.close(fd)
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.OPEN_FD)
    assert item.is_dir()
    vr._hooks.clear()
    _, _, report = plan_exec(w, item, proc_root="/proc")
    assert by_path(report, item)["status"] == vr.RELOCATED


def test_changed_plan_refused(w):
    """Catches a changed plan executed or broadened: a plan file with an
    item added after freezing -- even re-sealed with a recomputed body
    digest -- is refused (PLAN_DIGEST), and a frozen plan
    whose item has since become HOT is refused as a whole (CHANGED_PLAN);
    nothing is touched either time. Negative control: the untouched plan
    executes."""
    item, extra = w.copy("dup"), w.copy("extra")
    w.end_setup()
    rel = w.relocation()
    plan = rel.plan(PROJECT, [item])
    document = json.loads(plan.path.read_text())
    added = dict(document["plan"]["items"][0], path=str(extra), given=str(extra))
    document["plan"]["items"].append(added)
    # Re-sealed: the forger recomputes the body digest inside the file too;
    # only the digest recorded in plans.jsonl when the plan was frozen tells.
    import hashlib
    document["body_sha256"] = hashlib.sha256(vr._canonical(document["plan"])).hexdigest()
    forged = plan.path.with_name("forged.json")
    forged.write_text(json.dumps(document))
    plan.path.rename(plan.path.with_name("original.json"))
    forged.rename(plan.path)
    report = rel.execute(plan.path)
    assert report.status == vr.REFUSED and report.detail.startswith(vr.PLAN_DIGEST)
    assert item.is_dir() and extra.is_dir()
    plan2 = rel.plan(PROJECT, [item])
    w.ledger(f"## Open\n\n- {item}\n")
    report = rel.execute(plan2.path)
    assert report.status == vr.REFUSED and report.detail.startswith(vr.CHANGED_PLAN)
    assert item.is_dir() and not report.items
    w.ledger("# nothing open\n")
    assert rel.execute(plan2.path).status == vr.DONE and not item.exists()


def test_concurrent_execute_refused(w):
    """Catches the relocation flock not taken for a second execute of the
    same process: an execute started while another holds relocation.lock
    reports BUSY, touches nothing and leaves the first execute's session
    alone. Negative control: the first execute itself completes and closes
    its session cleanly."""
    item = w.copy("dup")
    w.end_setup()
    rel = w.relocation()
    plan = rel.plan(PROJECT, [item])
    inner = []

    @hook("after_lock")
    def again(plan_path, project_id):
        inner.append(rel.execute(plan_path))

    report = rel.execute(plan.path)
    [second] = inner
    assert second.status == vr.BUSY and not second.items
    assert report.status == vr.DONE and by_path(report, item)["status"] == vr.RELOCATED
    assert "closing the relocation session failed" not in report.detail


def test_crossed_duplicates_one_kept(w):
    """Catches the original pinning removed: two run directories that are
    each other's listed originals, planned together, never both go -- each
    is rejected PINNED_ORIGINAL (a listed original is never a candidate) and
    both survive the execute with their bytes. Negative control: each is
    VERIFIED_DUPLICATE by tier, and a plain copy in the same plan is
    accepted and relocated."""
    r1, r2 = runs_world(w, "20261001t0091z-ra", "20261001t0092z-rb")
    w.end_setup()
    assert [w.classify(r / "out").tier for r in (r1, r2)] == [st.VERIFIED_DUPLICATE] * 2
    copy = copy_of(w, r1 / "out", "copy")
    _, plan, report = plan_exec(w, r1 / "out", r2 / "out", copy)
    assert [i["path"] for i in plan.items] == [str(copy)]
    assert sorted(r["path"] for r in plan.rejected) == [str(r1 / "out"), str(r2 / "out")]
    assert all(vr.PINNED_ORIGINAL in r["reason"] for r in plan.rejected)
    assert by_path(report, copy)["status"] == vr.RELOCATED
    for run in (r1, r2):
        assert (run / "out" / "f.txt").read_text() == "same\n"


def test_execute_without_driver_lease_does_nothing(w):
    """Catches the driver lease not taken (Revision 3 item 1): while another
    session holds the project's coordinator driver lease, execute reports
    BUSY and creates no retain ref, no bundle, no quarantine. Negative
    control: once that session releases the driver lease, the same plan
    executes."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    rel = w.relocation(target=target)
    plan = rel.plan(PROJECT, [item])
    other = StorageCoordinator(w.registry, None, [], getpid=lambda: OTHER_PID)
    lease = other.acquire_session(PROJECT, "driver")
    other.coordinate(PROJECT, "driver", lambda: None)
    report = rel.execute(plan.path)
    assert report.status == vr.BUSY and "driver lease" in report.detail
    assert item.is_dir() and retain_refs(w) == []
    assert not (target.root / at.RECLAIM_REVIEW).exists()
    other.release_driver(lease)
    assert rel.execute(plan.path).status == vr.DONE and not item.exists()


def test_execute_holds_flock_across_driver_loss(w):
    """Catches the flock not taken (scoord2-a F1): the first execute's
    process is judged dead mid-call, so a second process can supersede the
    relocation session and the driver lease -- but relocation.lock is still
    held, and the second execute reports BUSY. Negative control: after the
    first execute ends, the second process may execute."""
    item = w.copy("dup")
    w.end_setup()
    rel = w.relocation()
    rival = w.relocation(pid=OTHER_PID,
                         coordinator=StorageCoordinator(w.registry, None, [],
                                                        getpid=lambda: OTHER_PID))
    plan = rel.plan(PROJECT, [item])
    inner = []

    @hook("after_lock")
    def lose_driver(plan_path, project_id):
        if inner:
            return
        start = w.fake.procs.pop(RELOC_PID)
        try:
            inner.append(rival.execute(plan_path))
        finally:
            w.fake.procs[RELOC_PID] = start

    rel.execute(plan.path)
    [second] = inner
    assert second.status == vr.BUSY and vr.LOCK_NAME in second.detail
    vr._hooks.clear()
    later = rel.plan(PROJECT, [w.copy("again")]) if item.exists() else None
    if later is not None:
        assert rival.execute(later.path).status in (vr.DONE, vr.REFUSED)


def test_second_execute_busy_on_relocation_session(w):
    """Catches the relocation session not fixed (Revision 5 item 1): while
    one execute holds the project's fixed ``relocation`` session, an execute
    of another live process is refused by the registry -- BUSY, naming the
    relocation session, nothing done. Negative control: after the first
    ends, the other process can open the session."""
    item = w.copy("dup")
    w.end_setup()
    rel = w.relocation()
    rival = w.relocation(pid=OTHER_PID,
                         coordinator=StorageCoordinator(w.registry, None, [],
                                                        getpid=lambda: OTHER_PID))
    plan = rel.plan(PROJECT, [item])
    inner = []

    @hook("after_lock")
    def compete(plan_path, project_id):
        if not inner:
            inner.append(rival.execute(plan_path))

    rel.execute(plan.path)
    [second] = inner
    assert second.status == vr.BUSY and "relocation session" in second.detail
    vr._hooks.clear()
    fresh = w.copy("later")
    later = rival.plan(PROJECT, [fresh])
    assert rival.execute(later.path).status == vr.DONE and not fresh.exists()


def test_late_entry_in_quarantine_restores(w):
    """Catches a recursive removal instead of the manifest's entries: an
    entry that appears in the quarantine after the inventory survives --
    every manifest entry is unlinked, ``rmdir`` meets the late entry, and
    what is left gets the original name back (LATE_ENTRY_RESTORED,
    NEEDS_YOU). Negative control: without a late entry the item is gone."""
    item = w.copy("dup")
    w.end_setup()

    @hook("before_unlink")
    def late(item, quarantine, record):
        (quarantine / "sub" / "late.txt").write_text("arrived late\n")

    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.LATE_ENTRY_RESTORED and record["needs_you"] is True
    assert (item / "sub" / "late.txt").read_text() == "arrived late\n"
    assert not (item / "a.txt").exists() and not (item / "sub" / "b.txt").exists()
    vr._hooks.clear()
    again = w.copy("again")
    _, _, report = plan_exec(w, again)
    assert by_path(report, again)["status"] == vr.RELOCATED and not again.exists()


def _clone_gate(world: World, mutate, note: str):
    clone = world.clone("clone")
    control = world.clone("control")
    mutate(clone)
    world.end_setup()
    decision = world.classify(clone)
    assert decision.tier != st.REPRODUCIBLE_SCRATCH
    assert any(n.startswith(note) for n in decision.notes), decision.notes
    plan = world.relocation().plan(world_project(), [clone, control])
    assert [i["path"] for i in plan.items] == [str(control)]
    return clone


def world_project() -> str:
    return PROJECT


def test_clone_with_linked_worktree_kept(w):
    """Catches the linked-worktree gate dropped: a clone with a linked
    worktree (``.git/worktrees/`` not empty) is kept (note
    ``linked_worktree``). Negative control: a plain clone in the same plan
    is accepted."""
    _clone_gate(w, lambda c: git(c, "worktree", "add", "-q", str(w.scratch / "wt")),
                "linked_worktree")


def test_clone_with_submodule_kept(w):
    """Catches the submodule gate dropped: a clone whose ``.git/modules/``
    holds a submodule repository is kept (note ``submodule``). Negative
    control: a plain clone in the same plan is accepted."""
    def submodule(clone):
        modules = clone / ".git" / "modules" / "lib"
        modules.mkdir(parents=True)
        (modules / "HEAD").write_text("ref: refs/heads/main\n")
    _clone_gate(w, submodule, "submodule")


def test_clone_with_hook_kept(w):
    """Catches the hook check dropped: a clone with an active hook (a
    ``.git/hooks`` file that is not a ``*.sample``) is kept (note ``hook``).
    Negative control: a plain clone (samples only) is accepted."""
    def add_hook(clone):
        path = clone / ".git" / "hooks" / "post-checkout"
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    _clone_gate(w, add_hook, "hook")


def test_clone_with_unreachable_reflog_kept(w):
    """Catches the reflog check dropped: a clone whose reflog names a commit
    that HEAD no longer reaches (committed, then reset away) is kept (note
    ``unreachable_reflog``). Negative control: a plain clone is accepted."""
    def reset_away(clone):
        (clone / "README").write_text("lost work\n")
        git(clone, "commit", "-qam", "lost")
        git(clone, "reset", "-q", "--hard", "HEAD~1")
    _clone_gate(w, reset_away, "unreachable_reflog")


def test_clone_with_stash_kept(w):
    """Catches the stash check dropped at execute: a stash made in a clone
    after it was placed keeps it (REPRODUCTION_CHANGED, ``stash``).
    Negative control: without the stash the clone relocates
    (`test_retain_ref_exists_before_removal`)."""
    clone = w.clone("clone")
    w.end_setup()

    @hook("after_place")
    def stash(item, record):
        (clone / "README").write_text("stashed work\n")
        git(clone, "stash", "-q")

    _, _, report = plan_exec(w, clone)
    record = by_path(report, clone)
    assert record["status"] == vr.KEPT
    assert record["reason"].startswith(vr.REPRODUCTION_CHANGED + ": ")
    # The note itself, not a mention of refs/stash by the refs or reflog gates.
    notes = record["reason"][len(vr.REPRODUCTION_CHANGED) + 2:].split("; ")
    assert "stash" in notes, notes
    assert clone.is_dir()


def test_cwd_inside_keeps_item(w):
    """Catches the cwd re-check dropped: a process of the same uid whose cwd
    lies inside the item appears after planning; the item is kept and
    restored (CWD_INSIDE). Negative control: once that process has ended,
    the item relocates."""
    item = w.copy("dup")
    w.end_setup()
    procs = []

    @hook("after_place")
    def sit_inside(item, record):
        procs.append(subprocess.Popen(["sleep", "60"], cwd=Path(item["path"]) / "sub"))

    try:
        _, _, report = plan_exec(w, item, proc_root="/proc")
    finally:
        for proc in procs:
            proc.kill()
            proc.wait()
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.CWD_INSIDE)
    assert item.is_dir()
    vr._hooks.clear()
    _, _, report = plan_exec(w, item, proc_root="/proc")
    assert by_path(report, item)["status"] == vr.RELOCATED


def test_lease_after_plan_keeps_item(w):
    """Catches the lease re-check dropped: the item's session lease becomes
    live again after planning (its process is back); the item is kept
    (LEASE). Negative control: with the lease dead it relocates."""
    item = w.copy("dup")
    w.end_setup()

    @hook("after_place")
    def revive(item, record):
        w.revive_setup()

    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.LEASE)
    assert item.is_dir()
    vr._hooks.clear()
    w.end_setup()
    _, _, report = plan_exec(w, item)
    assert by_path(report, item)["status"] == vr.RELOCATED


def test_restore_never_replaces(w):
    """Catches a plain ``os.rename`` restore (Revision 4 item 8): when the
    old name was taken while the item sat in quarantine (an empty directory
    -- which a plain rename would silently replace), the item is restored to
    a fresh versioned name and the newcomer is untouched. Negative control:
    without a newcomer the item gets its own name back
    (`test_quarantine_mismatch_restores`)."""
    item = w.copy("dup")
    w.end_setup()
    newcomer = {}

    @hook("after_quarantine")
    def take_name(item, quarantine, record):
        (quarantine / "a.txt").write_text("mismatch\n")
        os.mkdir(item["path"])
        newcomer["ino"] = os.lstat(item["path"]).st_ino

    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.KEPT
    assert os.lstat(item).st_ino == newcomer["ino"] and os.listdir(item) == []
    restored = Path(record["restored_to"])
    assert restored != item and restored.name.startswith("dup.v")
    assert (restored / "a.txt").read_text() == "mismatch\n"


def test_failed_destination_verification_keeps_item(w):
    """Catches a removal after a placement that is not ARCHIVED_VERIFIED:
    a payload corrupted on the destination fails verification there, so the
    local copy stays (DESTINATION_UNVERIFIED). Negative control: an intact
    destination relocates (`test_duplicate_relocates_with_receipt`)."""
    item = w.copy("dup")
    w.end_setup()

    def corrupt(destination):
        payload = destination / ab.TREE_PAYLOAD
        data = bytearray(payload.read_bytes())
        data[len(data) // 2] ^= 0xFF
        with open(payload, "r+b") as handle:
            handle.write(bytes(data))

    at._after_copy_hook = corrupt
    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.KEPT
    assert record["reason"].startswith(vr.DESTINATION_UNVERIFIED)
    assert record["readback_status"] == ab.ARCHIVE_UNVERIFIED and item.is_dir()


def test_precondition_duplicate_item_changed_before_build_kept(w):
    """Catches the planned entries_digest not compared (E-K4 b): a writer
    changes the single VERIFIED_DUPLICATE item after execute's
    re-classification and before its bundle is built; the item is kept
    (ITEM_CHANGED) with its new bytes, and no bundle of those bytes reaches
    reclaim-review as a non-unique duplicate. Negative control: without the
    write the same kind of item relocates."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()

    @hook("before_build")
    def write(item, record):
        (Path(item["path"]) / "a.txt").write_text("changed before the build\n")

    _, _, report = plan_exec(w, item, target=target)
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.ITEM_CHANGED)
    assert (item / "a.txt").read_text() == "changed before the build\n"
    assert record["bundle_path"] is None and record["readback_status"] is None
    area = target.root / at.RECLAIM_REVIEW / PROJECT
    assert not area.exists() or list(area.iterdir()) == []
    vr._hooks.clear()
    again = w.copy("again")
    _, _, report = plan_exec(w, again, target=target)
    assert by_path(report, again)["status"] == vr.RELOCATED and not again.exists()


def test_precondition_duplicate_item_changed_after_place_kept(w):
    """Catches only the originals re-checked (E-K4 a): in a plan of two
    duplicates, the after_place of item 1 rewrites a file of item 2 -- item 2
    is kept (ITEM_CHANGED), its local bytes survive and no bundle of it is
    placed; and an item rewritten after its own bundle was placed is kept
    too (its bundle holds the planned bytes). Negative controls: item 1 of
    the same plan, and an untouched item, relocate."""
    first, second, third = w.copy("dup1"), w.copy("dup2"), w.copy("dup3")
    w.end_setup()
    target, _ = w.target()

    @hook("after_place")
    def rewrite(item, record):
        if item["path"] == str(first):
            (second / "a.txt").write_text("rewritten by another writer\n")
        if item["path"] == str(third):
            (third / "sub" / "b.txt").write_text("rewritten after its own place\n")

    _, _, report = plan_exec(w, first, second, third, target=target)
    assert by_path(report, first)["status"] == vr.RELOCATED and not first.exists()
    kept = by_path(report, second)
    assert kept["status"] == vr.KEPT and kept["reason"].startswith(vr.ITEM_CHANGED)
    assert (second / "a.txt").read_text() == "rewritten by another writer\n"
    assert kept["bundle_path"] is None
    late = by_path(report, third)
    assert late["status"] == vr.KEPT and late["reason"].startswith(vr.ITEM_CHANGED)
    assert (third / "sub" / "b.txt").read_text() == "rewritten after its own place\n"
    area = target.root / at.RECLAIM_REVIEW / PROJECT
    assert sorted(p.name for p in area.iterdir()) == sorted(
        Path(r["bundle_path"]).name for r in (by_path(report, first), late))
    vr._hooks.clear()
    fourth = w.copy("dup4")
    _, _, report = plan_exec(w, fourth, target=target)
    assert by_path(report, fourth)["status"] == vr.RELOCATED


def test_guest_staging_leftover_in_receipt(w):
    """Catches a guest leftover not recorded (E-gap-fae8d7db): when
    admission admits ``archive_staging`` the bundle is built in the guest,
    stays there after a successful place, and the ReclaimReceipt records it
    (where=guest, path, bytes, STAGING_LEFTOVER) with its real size.
    Negative control: refused, the leftover is on the target
    (where=target)."""
    item = w.copy("dup")
    w.end_setup()
    _, _, report = plan_exec(w, item, admission=FakeAdmission("PRESSURE", admit=True))
    record = by_path(report, item)
    assert record["status"] == vr.RELOCATED and record["staging"]["where"] == "guest"
    leftover = record["staging_leftover"]
    assert leftover["status"] == vr.STAGING_LEFTOVER and leftover["where"] == "guest"
    staged = Path(leftover["path"])
    assert staged.is_dir() and staged.is_relative_to(w.registry.state_root)
    assert leftover["bytes"] == sum(f.stat().st_size for f in staged.rglob("*") if f.is_file())
    on_disk = json.loads(Path(record["receipt_path"]).read_text())
    assert on_disk["staging_leftover"] == leftover
    other = w.copy("other")
    _, _, report = plan_exec(w, other, admission=FakeAdmission("PRESSURE", admit=False))
    assert by_path(report, other)["staging_leftover"]["where"] == "target"


# --------------------------------------------------------------------------- #
# K5 -- user-reclaimed, promotion, extract roots
# --------------------------------------------------------------------------- #


def _relocated(world: World):
    item = world.copy("dup")
    world.end_setup()
    target, probe = world.target()
    rel, _, report = plan_exec(world, item, target=target)
    record = by_path(report, item)
    assert record["status"] == vr.RELOCATED
    return rel, target, item, record


def test_user_reclaimed_recorded(w):
    """S20. Catches a disappearance read as corruption: a bundle the user
    took out of reclaim-review is recorded USER_RECLAIMED_ARCHIVE (once, in
    user-reclaimed.jsonl), never as stale or corrupt. Negative controls: a
    bundle still present records nothing; a target whose identity is not
    confirmed judges nothing (TARGET_UNCONFIRMED)."""
    rel, target, item, record = _relocated(w)
    assert rel.user_reclaimed(target, PROJECT) == []
    bundle = Path(record["bundle_path"])
    bundle.rename(w.tmp / "user-took-it")
    [entry] = rel.user_reclaimed(target, PROJECT)
    assert entry["status"] == vr.USER_RECLAIMED_ARCHIVE and entry["tier"] is None
    log = (w.receipts / vr.USER_RECLAIMED_LOG).read_text().splitlines()
    assert len(log) == 1 and json.loads(log[0])["status"] == vr.USER_RECLAIMED_ARCHIVE
    rel.user_reclaimed(target, PROJECT)
    assert len((w.receipts / vr.USER_RECLAIMED_LOG).read_text().splitlines()) == 1
    wrong, probe = w.target()
    probe.identity = {"serial": "another disk"}
    assert rel.user_reclaimed(wrong, PROJECT)[0]["status"] == vr.TARGET_UNCONFIRMED


def test_promotion_when_newly_referenced(w):
    """Catches promotion removed: when the user took the bundle out of
    reclaim-review and the item has meanwhile become referenced (an open
    finding names it), the judgement is PROMOTION_REQUIRED and the item is
    classified HOT. Negative control: unreferenced, the same disappearance is
    USER_RECLAIMED_ARCHIVE (`test_user_reclaimed_recorded`)."""
    rel, target, item, record = _relocated(w)
    Path(record["bundle_path"]).rename(w.tmp / "user-took-it")
    w.ledger(f"## Offen\n\n- re-check {item}/a.txt\n")
    [entry] = rel.user_reclaimed(target, PROJECT)
    assert entry["status"] == vr.PROMOTION_REQUIRED and entry["tier"] == st.HOT
    assert any("open_finding" in r for r in entry["reasons"])


def _extract_world(world: World, *, commit_receipt: bool = True):
    """Place a bundle on a target in session ``c2`` (C2 creates its extract
    roots in that session's scratch) and commit its receipt."""
    source = world.copy("payload")
    world.registry.open_session(PROJECT, "c2", pid=SETUP_PID, boot_id=BOOT)
    staging = world.tmp / "bundles"
    staging.mkdir()
    bundle = ab.ArchiveBundle.build_tree(source, staging, label="x",
                                         meta={"unique": False},
                                         ownership={"status": "OWNED",
                                                    "root": str(world.scratch)})
    target, _ = world.target("t9")
    placed = target.place(bundle, PROJECT, at.COLD_STORE, registry=world.registry,
                          session_id="c2")
    assert placed.status == ab.ARCHIVED_VERIFIED
    if commit_receipt:
        world.commit(f"dogfood/storage-lifecycle/receipts/{bundle.name}.json",
                     placed.receipt_path.read_text())
    roots = [Path(p) for p in placed.extract_roots]
    world.end_setup()
    return roots[0], placed


def test_extract_root_with_extra_file_kept(w):
    """Catches the extract-root content check removed: an extract root
    holding anything besides EXTRACT_ROOT.json and tree/ is kept. Negative
    control: the same extract root without the extra file is
    REPRODUCIBLE_SCRATCH and is released by execute without a new bundle."""
    root, _ = _extract_world(w)
    decision = w.classify(root)
    assert decision.tier == st.REPRODUCIBLE_SCRATCH, decision.notes
    (root / "stray.txt").write_text("extra\n")
    decision = w.classify(root)
    assert decision.tier != st.REPRODUCIBLE_SCRATCH
    assert any("not only EXTRACT_ROOT.json and tree/" in n for n in decision.notes)
    (root / "stray.txt").rename(w.tmp / "stray.txt")
    # The relocation registers the target holding the placement (``t9``):
    # an extract root under no registered target is kept (Revision 10 item 5).
    _, _, report = plan_exec(w, root, target=w.target("t9")[0])
    record = by_path(report, root)
    assert record["status"] == vr.RELOCATED and record["bundle_path"] is None
    assert not root.exists()


def test_extract_root_tree_digest_mismatch_kept(w):
    """Catches the tree-digest comparison removed: an extract root whose
    tree/ no longer matches the committed receipt's content digest is kept.
    Negative control: the untouched root is REPRODUCIBLE_SCRATCH."""
    root, _ = _extract_world(w)
    assert w.classify(root).tier == st.REPRODUCIBLE_SCRATCH
    (root / "tree" / "a.txt").write_text("edited extraction\n")
    decision = w.classify(root)
    assert decision.tier != st.REPRODUCIBLE_SCRATCH
    assert any("tree digest mismatch" in n for n in decision.notes)


def test_extract_root_without_committed_receipt_kept(w):
    """Catches the committed-receipt check dropped: until the bundle's
    receipt is committed, its extract roots are kept and reported
    AWAITING_COMMITTED_RECEIPT. Negative control: after the receipt is
    committed the root is REPRODUCIBLE_SCRATCH."""
    root, placed = _extract_world(w, commit_receipt=False)
    decision = w.classify(root)
    assert decision.tier != st.REPRODUCIBLE_SCRATCH
    assert any(n.startswith(st.AWAITING_COMMITTED_RECEIPT) for n in decision.notes)
    assert not w.relocation().plan(PROJECT, [root]).items
    w.commit("dogfood/storage-lifecycle/receipts/r.json", placed.receipt_path.read_text())
    assert w.classify(root).tier == st.REPRODUCIBLE_SCRATCH


def test_extract_root_payload_mismatch_kept(w):
    """Catches a comparison with the target's own SHA256SUMS instead of the
    committed receipt: the payload on the target is changed and its
    SHA256SUMS rewritten to match -- the receipt's payload SHA-256 still
    differs, so the root is kept. Negative control: the intact payload
    lets it be REPRODUCIBLE_SCRATCH."""
    root, placed = _extract_world(w)
    assert w.classify(root).tier == st.REPRODUCIBLE_SCRATCH
    payload = placed.path / ab.TREE_PAYLOAD
    data = bytearray(payload.read_bytes())
    data[-1] ^= 0xFF
    with open(payload, "r+b") as handle:
        handle.write(bytes(data))
    sums = (placed.path / ab.SUMS).read_text().splitlines()
    sums[0] = f"{st.sha256_file(payload)}  {ab.TREE_PAYLOAD}"
    with open(placed.path / ab.SUMS, "w") as handle:
        handle.write("\n".join(sums) + "\n")
    decision = w.classify(root)
    assert decision.tier != st.REPRODUCIBLE_SCRATCH
    assert any("payload mismatch" in n for n in decision.notes)


# --------------------------------------------------------------------------- #
# K8 -- the removal path
# --------------------------------------------------------------------------- #

_REMOVALS = {"unlink", "rmdir", "rmtree", "remove", "removedirs", "truncate", "ftruncate"}
SCANNED = ["storage_tiers.py", "verified_relocation.py", "storage_steward.py", "cli.py",
           "controller.py", "archive_bundle.py", "storage_park.py", "storage_coordinator.py"]


def _removal_functions(source: str) -> tuple[dict[str, set[str]], dict[str, set[str]], list]:
    """(functions with a removal call, names each function references,
    removal calls outside any function)."""
    tree = ast.parse(source)
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _REMOVALS:
                    aliases.add(alias.asname or alias.name)

    def removal(node) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr in _REMOVALS:
            return fn.attr
        if isinstance(fn, ast.Name) and (fn.id in _REMOVALS or fn.id in aliases):
            return fn.id
        if isinstance(fn, ast.Name) and fn.id == "getattr" and len(node.args) >= 2 \
                and isinstance(node.args[1], ast.Constant) and node.args[1].value in _REMOVALS:
            return "getattr:" + node.args[1].value
        return None

    removers: dict[str, set[str]] = {}
    references: dict[str, set[str]] = {}
    inside: set[int] = set()

    def visit(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                calls = {r for sub in ast.walk(child) if (r := removal(sub))}
                inside.update(id(sub) for sub in ast.walk(child))
                if calls:
                    removers[name] = calls
                references[name] = {n.id for n in ast.walk(child) if isinstance(n, ast.Name)} | {
                    n.attr for n in ast.walk(child) if isinstance(n, ast.Attribute)}
                visit(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)
    visit(tree)
    loose = [removal(n) for n in ast.walk(tree) if removal(n) and id(n) not in inside]
    return removers, references, loose


def _confined_to_execute(source: str) -> list[str]:
    """Problems: a removal outside any function, or a function with a
    removal (or calling one, transitively) that is public and not
    ``VerifiedRelocation.execute``, or that execute never reaches."""
    removers, references, loose = _removal_functions(source)
    problems = [f"module-level removal {r}" for r in loose]
    closure = set(removers)
    changed = True
    while changed:
        changed = False
        short = {name.split(".")[-1] for name in closure}
        for name, refs in references.items():
            if name not in closure and refs & short:
                closure.add(name)
                changed = True
    for name in sorted(closure):
        last = name.split(".")[-1]
        if name != "VerifiedRelocation.execute" and not last.startswith("_"):
            problems.append(f"{name} removes or reaches a removal but is not execute")
    if removers and "VerifiedRelocation.execute" not in closure:
        problems.append("execute does not reach the removal")
    return problems


_SEAM = "_hooks"
_NAMESPACE_CALLS = {"getattr", "setattr", "delattr", "hasattr", "vars", "globals"}


def _seam_uses(source: str) -> list[tuple[str, str, int]]:
    """Every occurrence of the name ``_hooks`` in `source` -- as a name, an
    attribute, or a string passed to ``getattr``/``setattr``/``vars``/
    ``globals`` (or used to index what they return) -- as ``(kind, the
    enclosing function or "<module>", line)``."""
    tree = ast.parse(source)
    found: list[tuple[str, str, int]] = []

    def visit(node, where: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = where
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = child.name if where == "<module>" else f"{where}.{child.name}"
            if isinstance(child, ast.Name) and child.id == _SEAM:
                found.append(("name", where, child.lineno))
            elif isinstance(child, ast.Attribute) and child.attr == _SEAM:
                found.append(("attribute", where, child.lineno))
            elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name) \
                    and child.func.id in _NAMESPACE_CALLS \
                    and any(isinstance(a, ast.Constant) and a.value == _SEAM
                            for a in child.args):
                found.append(("string", where, child.lineno))
            elif isinstance(child, ast.Subscript) and isinstance(child.slice, ast.Constant) \
                    and child.slice.value == _SEAM:
                found.append(("string", where, child.lineno))
            visit(child, inner)
    visit(tree, "<module>")
    return found


def test_removal_only_in_execute():
    """Catches a removal call outside VERIFIED_RELOCATION (e.g. an unlink in
    the steward) and the private seam used outside its module (e.g. a
    ``_hooks[...] =`` in the steward): over storage_tiers,
    verified_relocation, storage_steward, cli, controller, archive_bundle
    and storage_park, every removal call -- ``unlink``, ``rmdir``,
    ``rmtree``, ``remove``, ``truncate``, imported aliases and ``getattr``
    forms -- lies in `VerifiedRelocation.execute` or private helpers reached
    only from it; and over every module under ``src/``, the name ``_hooks``
    (name, attribute, or string given to ``getattr``/``setattr``/``vars``/
    ``globals``) occurs only in verified_relocation.py's module-level
    definition and in its reader `_hook`. Negative controls: the scanner
    flags an unlink in a public steward method, an aliased import and a
    ``getattr`` form, and the seam scanner flags an assignment through an
    attribute, a ``setattr`` string and a ``vars(...)`` index."""
    for name in SCANNED:
        source = (ROOT / "src" / "hoh" / name).read_text(encoding="utf-8")
        removers, _, loose = _removal_functions(source)
        if name == "verified_relocation.py":
            assert _confined_to_execute(source) == []
            assert set(removers) == {"_unlink_entry", "_rmdir_entry"}
        else:
            assert removers == {} and loose == [], (name, removers, loose)
    probe = ("import os\nclass StorageSteward:\n    def tidy(self, p):\n        os.unlink(p)\n")
    assert _removal_functions(probe)[0] == {"StorageSteward.tidy": {"unlink"}}
    alias = "from os import unlink as u\ndef _x(p):\n    u(p)\ndef public(p):\n    _x(p)\n"
    assert _confined_to_execute(alias) == [
        "public removes or reaches a removal but is not execute",
        "execute does not reach the removal"]
    dynamic = "import os\ndef f(p):\n    getattr(os, 'rmdir')(p)\n"
    assert _removal_functions(dynamic)[0] == {"f": {"getattr:rmdir"}}
    # The private seam: only its definition and its reader.
    modules = sorted((ROOT / "src").rglob("*.py"))
    assert len(modules) > 10
    for module in modules:
        uses = _seam_uses(module.read_text(encoding="utf-8"))
        if module.relative_to(ROOT / "src").as_posix() == "hoh/verified_relocation.py":
            assert {(kind, where) for kind, where, _ in uses} == {
                ("name", "<module>"), ("name", "_hook")}, uses
            assert len([u for u in uses if u[1] == "<module>"]) == 1, uses
        else:
            assert uses == [], (module, uses)
    leak = ("import hoh.verified_relocation as vr\nclass StorageSteward:\n"
            "    def run(self):\n        vr._hooks['after_place'] = print\n"
            "        setattr(vr, '_hooks', {})\n        vars(vr)['_hooks'].clear()\n")
    assert [(k, w) for k, w, _ in _seam_uses(leak)] == [
        ("attribute", "StorageSteward.run"), ("string", "StorageSteward.run"),
        ("string", "StorageSteward.run")]


def test_removal_rechecks_inode(w):
    """Catches the st_dev/st_ino re-check dropped: the quarantined item is
    swapped (renamed away, a look-alike directory with the same entries put
    in its place) after the last check; the removal sees another inode on
    its own descriptor and removes nothing (INODE_CHANGED). Negative
    control: unswapped, it is removed (`test_duplicate_relocates_with_receipt`)."""
    item = w.copy("dup")
    w.end_setup()
    swapped = {}

    @hook("before_inode_check")
    def swap(item, quarantine, record):
        aside = w.tmp / "swapped-away"
        quarantine.rename(aside)
        quarantine.mkdir()
        (quarantine / "sub").mkdir()
        (quarantine / "a.txt").write_text("alpha\n")
        (quarantine / "sub" / "b.txt").write_text("beta\n")
        swapped["path"] = quarantine

    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.INODE_CHANGED)
    look_alike = swapped["path"]
    assert (look_alike / "a.txt").read_text() == "alpha\n"
    assert (look_alike / "sub" / "b.txt").is_file()
    assert (w.tmp / "swapped-away" / "a.txt").is_file()


# --------------------------------------------------------------------------- #
# K9 -- no overrides; partial removal; staging; reservation
# --------------------------------------------------------------------------- #


def test_no_override_parameters():
    """Catches an override keyword: `plan`, `execute` and `classify_tier`
    accept no reference, lease, finding or proof set and no force/override
    switch -- they take only what they act on. Negative control: the
    introspection does see their real parameters."""
    expected = {
        vr.VerifiedRelocation.plan: ["self", "project_id", "candidates"],
        vr.VerifiedRelocation.execute: ["self", "plan_path"],
        st.classify_tier: ["path", "registry", "sources"],
    }
    for fn, names in expected.items():
        params = list(inspect.signature(fn).parameters)
        assert params == names
        for word in ("reference", "lease", "finding", "proof", "override", "force", "skip"):
            assert not [p for p in params if word in p]


def test_partial_removal_kept_as_partial(w, monkeypatch):
    """Catches a partial removal restored as whole (Revision 4 item 3): the
    second unlink fails, so the remainder keeps its quarantine name and is
    recorded PARTIAL with NEEDS_YOU; the original name stays free. Negative
    control: with every unlink succeeding the item is RELOCATED."""
    item = w.copy("dup")
    w.end_setup()
    calls = []
    real = vr._unlink_entry

    def flaky(name, dir_fd):
        calls.append(name)
        if len(calls) == 2:
            raise OSError(5, "simulated I/O error")
        real(name, dir_fd)

    monkeypatch.setattr(vr, "_unlink_entry", flaky)
    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.PARTIAL and record["needs_you"] is True
    quarantine = Path(record["quarantine_path"])
    assert quarantine.is_dir() and not item.exists()
    monkeypatch.setattr(vr, "_unlink_entry", real)
    again = w.copy("again")
    _, _, report = plan_exec(w, again)
    assert by_path(report, again)["status"] == vr.RELOCATED


def test_staging_dir_and_leftover_reported(w):
    """Catches staging inside reclaim-review (Revision 6b): with the
    reservation refused, the bundle is built under
    ``<target>/staging/<p>/`` -- outside both C2 areas -- and that staged
    copy is reported STAGING_LEFTOVER (path, bytes) for the user, never
    released. Negative control: reclaim-review holds only the placed bundle."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    _, _, report = plan_exec(w, item, target=target, admission=FakeAdmission(admit=False))
    record = by_path(report, item)
    assert record["status"] == vr.RELOCATED
    leftover = record["staging_leftover"]
    assert leftover["status"] == vr.STAGING_LEFTOVER and leftover["bytes"] > 0
    staged = Path(leftover["path"])
    assert staged.parent == target.root / "staging" / PROJECT and staged.is_dir()
    area = target.root / at.RECLAIM_REVIEW / PROJECT
    assert [p.name for p in area.iterdir()] == [Path(record["bundle_path"]).name]


def test_no_staging_on_failed_target_check(w):
    """Catches the target check ignored for staging: with the reservation
    refused and the target's identity no longer confirmed at staging time,
    nothing is staged on the target and the item waits (NO_STAGING).
    Negative control: a confirmed target stages and relocates
    (`test_staging_dir_and_leftover_reported`)."""
    item = w.copy("dup")
    w.end_setup()
    target, probe = w.target()
    probe.after = {"serial": "a different disk"}
    _, _, report = plan_exec(w, item, target=target, admission=FakeAdmission(admit=False))
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.NO_STAGING)
    assert not (target.root / "staging").exists() and item.is_dir()


def test_critical_allows_only_target_staging(w):
    """Catches guest staging in CRITICAL: even with an admission that would
    admit, a CRITICAL state stages only on the target and asks for no
    reservation. Negative control: in PRESSURE the same admission stages on
    the guest (`test_staging_reserves_archive_staging`)."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    admission = FakeAdmission("CRITICAL", admit=True)
    _, _, report = plan_exec(w, item, target=target, admission=admission)
    record = by_path(report, item)
    assert record["status"] == vr.RELOCATED and record["staging"]["where"] == "target"
    assert admission.reserved == []


def test_staging_reserves_archive_staging(w):
    """Catches staging without a reservation (Revision 6a item 2): an
    admitted ``archive_staging`` reservation for the item's bytes is taken
    before the guest build and released with the bundle's size. Negative
    control: refused, the build goes to the target
    (`test_refused_staging_builds_on_target`)."""
    item = w.copy("dup")
    w.end_setup()
    admission = FakeAdmission("PRESSURE", admit=True)
    _, plan, report = plan_exec(w, item, admission=admission)
    record = by_path(report, item)
    assert record["status"] == vr.RELOCATED and record["staging"]["where"] == "guest"
    assert admission.reserved == [(PROJECT, "archive_staging", plan.items[0]["bytes"])]
    [(granted, actual)] = admission.released
    assert granted.op_kind == "archive_staging" and actual > 0
    assert Path(record["staging"]["path"]).is_relative_to(w.registry.state_root)


def test_refused_staging_builds_on_target(w):
    """Catches guest staging under a refusal: when admission refuses
    ``archive_staging``, the bundle is built on the target, not in the
    guest. Negative control: admitted, it is built in the guest
    (`test_staging_reserves_archive_staging`)."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    admission = FakeAdmission("PRESSURE", admit=False)
    _, _, report = plan_exec(w, item, target=target, admission=admission)
    record = by_path(report, item)
    assert record["status"] == vr.RELOCATED and record["staging"]["where"] == "target"
    assert Path(record["staging"]["path"]) == target.root / "staging" / PROJECT
    assert admission.reserved and not admission.released


# --------------------------------------------------------------------------- #
# K7 -- the rest
# --------------------------------------------------------------------------- #


def test_plan_is_frozen_with_digest_and_pins(w):
    """Catches a plan that is not durable before execute: the plan file's
    SHA-256 and the originals it pins are in plans.jsonl when `plan`
    returns. Negative control: a plan of nothing pins nothing."""
    item = w.copy("dup")
    w.end_setup()
    rel = w.relocation()
    plan = rel.plan(PROJECT, [item])
    [line] = (w.receipts / vr.INDEX_NAME).read_text().splitlines()
    record = json.loads(line)
    assert record["sha256"] == plan.digest == st.sha256_file(plan.path)
    assert record["pins"] == sorted(o["path"] for o in plan.items[0]["originals"])
    empty = rel.plan(PROJECT, [])
    assert json.loads((w.receipts / vr.INDEX_NAME).read_text().splitlines()[1])["pins"] == []
    assert empty.items == ()


def test_target_timeout_keeps_item(w):
    """Catches a target call without a bound: a target whose identity probe
    hangs keeps the item (TARGET_TIMEOUT, NEEDS_YOU) within the timeout.
    Negative control: a responsive target relocates
    (`test_duplicate_relocates_with_receipt`)."""
    item = w.copy("dup")
    w.end_setup()
    target, probe = w.target()
    probe.block = threading.Event()
    try:
        _, _, report = plan_exec(w, item, target=target, timeout=0.5)
    finally:
        probe.block.set()
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.TARGET_TIMEOUT)
    assert item.is_dir()


def test_unreadable_proc_in_quarantine_restores(w):
    """Catches a failing probe that leaves the item under its quarantine
    name: when ``/proc`` cannot be listed after the quarantine compare, the
    item gets its original name back and is kept (fail closed). Negative
    control: with ``/proc`` readable the same item relocates."""
    item = w.copy("dup")
    w.end_setup()

    @hook("after_compare")
    def hide_proc(item, quarantine, record):
        w.proc.rename(w.tmp / "proc-hidden")

    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.OPEN_FD)
    assert record["restored_to"] == str(item) and (item / "a.txt").read_text() == "alpha\n"
    assert not Path(record["quarantine_path"]).exists()
    vr._hooks.clear()
    (w.tmp / "proc-hidden").rename(w.proc)
    _, _, report = plan_exec(w, item)
    assert by_path(report, item)["status"] == vr.RELOCATED


def test_unreadable_originals_plan_nothing(w):
    """Catches pins that fail open: when the committed retention manifests
    cannot be read (one is not JSON), no original can be told apart from a
    candidate, so `plan` accepts nothing -- although the item is still a
    VERIFIED_DUPLICATE of the canonical repository. Negative control: with
    the broken manifest renamed out of the retention directory the same
    item is planned."""
    item = w.copy("dup")
    w.end_setup()
    broken = f"{st.RETENTION_DIR}/20261002T000000Z.json"
    w.commit(broken, "{not json\n")
    assert w.classify(item).tier == st.VERIFIED_DUPLICATE
    plan = w.relocation().plan(PROJECT, [item])
    assert not plan.items and vr.PINNED_ORIGINAL in plan.rejected[0]["reason"]
    git(w.canon, "mv", broken, broken + ".v1.broken")
    git(w.canon, "commit", "-qm", "park the broken manifest")
    assert [i["path"] for i in w.relocation().plan(PROJECT, [item]).items] == [str(item)]


# --------------------------------------------------------------------------- #
# K11 -- the removal set is the compared inventory (Revision 8 item 1)
# --------------------------------------------------------------------------- #


def reseal(world: World, plan_path: Path, edit) -> None:
    """Edit a frozen plan the way a forger with write access would, and reseal
    it so `_load_plan` accepts it: the body digest recomputed, the new file
    put in place by rename (the original kept beside it), and a plans.jsonl
    row for the new file digest appended."""
    import hashlib
    document = json.loads(plan_path.read_text())
    edit(document["plan"])
    document["body_sha256"] = hashlib.sha256(vr._canonical(document["plan"])).hexdigest()
    raw = json.dumps(document, indent=2, sort_keys=True).encode("ascii")
    forged = plan_path.with_name(plan_path.name + ".forged")
    forged.write_bytes(raw)
    plan_path.rename(plan_path.with_name(plan_path.name + ".v1.sealed"))
    forged.rename(plan_path)
    with open(world.receipts / vr.INDEX_NAME, "a", encoding="ascii") as handle:
        handle.write(json.dumps({"plan": plan_path.name,
                                 "sha256": hashlib.sha256(raw).hexdigest(),
                                 "project_id": PROJECT,
                                 "pins": document["plan"]["pins"]}) + "\n")


def test_removal_ignores_manifest_tampered_after_place(w):
    """Catches the removal set read from the staged manifest (sstew-b B1): at
    seam ``after_compare`` a file ``a/late.txt`` appears inside the
    quarantine and a path-valid entry for it (and its directory) is appended
    to the staged bundle's MANIFEST.json; the removal acts on the compared
    walk only, so the file survives and the item comes back
    LATE_ENTRY_RESTORED. Mutant: the removal set read from the staged
    manifest (it removes the file). Negative control: every walked entry is
    removed (`a.txt`, `sub/`)."""
    item = w.copy("dup")
    w.end_setup()
    tampered = []

    @hook("after_compare")
    def tamper(item, quarantine, record):
        (quarantine / "a").mkdir()
        (quarantine / "a" / "late.txt").write_text("not walked\n")
        bundle = Path(record["staging"].get("bundle") or record["staging_leftover"]["path"])
        path = bundle / ab.MANIFEST
        manifest = json.loads(path.read_text())
        directory = dict(next(e for e in manifest["entries"] if e["type"] == "dir"), path="a")
        late = dict(next(e for e in manifest["entries"] if e["type"] == "file"),
                    path="a/late.txt")
        manifest["entries"] += [directory, late]
        path.rename(bundle / (ab.MANIFEST + ".v1.untampered"))
        path.write_text(json.dumps(manifest))
        tampered.append(path)

    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert tampered and "a/late.txt" in tampered[0].read_text()
    assert (item / "a" / "late.txt").read_text() == "not walked\n"
    assert record["status"] == vr.LATE_ENTRY_RESTORED and record["needs_you"] is True
    assert not (item / "a.txt").exists() and not (item / "sub").exists()


def test_removal_rejects_unsafe_entry_paths():
    """Catches the validator returning its input (Revision 8 item 1):
    `_safe_entry_path` refuses ``..``, ``.``, an empty path, an empty
    component, an absolute path and a NUL-bearing path with `UnsafeEntry`.
    Mutant: the validator returns whatever it is given. Negative control:
    ordinary relative paths come back unchanged."""
    for bad in ("..", ".", "", "a/../b", "a/./b", "a//b", "/etc/passwd", "a/\0b", "../x",
                "a/.."):
        with pytest.raises(vr.UnsafeEntry):
            vr._safe_entry_path(bad)
    for good in ("a.txt", "sub/b.txt", "..x/y", "x..", ".hidden/file"):
        assert vr._safe_entry_path(good) == good


def test_directory_swapped_during_removal_kept(w):
    """Catches the directory comparison dropped: at seam ``before_unlink``
    the item's directory ``sub`` is renamed away and a different directory
    takes its name, holding a file of the same name (the walked ``b.txt``
    itself, moved in, so only the directory differs); the removal opens
    ``sub``, its ``fstat`` differs from the walk's, and it stops --
    ``a.txt`` was already removed, so the item is PARTIAL (NEEDS_YOU) under
    its quarantine name and the substitute's file survives. Mutant: the
    directory comparison dropped (the substitute's file is unlinked).
    Negative control: the renamed-away original directory is untouched."""
    item = w.copy("dup")
    w.end_setup()
    aside = w.tmp / "sub-aside"

    @hook("before_unlink")
    def swap(item, quarantine, record):
        (quarantine / "sub").rename(aside)
        (quarantine / "sub").mkdir()
        (aside / "b.txt").rename(quarantine / "sub" / "b.txt")

    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.PARTIAL and record["needs_you"] is True
    assert vr.ENTRY_SWAPPED in record["reason"]
    quarantine = Path(record["quarantine_path"])
    assert (quarantine / "sub" / "b.txt").read_text() == "beta\n"
    assert not (quarantine / "a.txt").exists() and not item.exists()
    assert aside.is_dir() and os.listdir(aside) == []


def test_file_swapped_during_removal_kept(w):
    """Catches the file comparison dropped: at seam ``before_unlink`` the
    walked ``a.txt`` is moved away and another file takes its name; the
    ``fstatat`` right before its unlink differs from the walk's, nothing has
    been removed yet, so the item is KEPT (ENTRY_SWAPPED) and restored to
    its name with the replacement intact. Mutant: the file comparison
    dropped (the replacement is unlinked). Negative control: the moved-away
    original is untouched and ``sub/b.txt`` was never removed."""
    item = w.copy("dup")
    w.end_setup()

    @hook("before_unlink")
    def swap(item, quarantine, record):
        (quarantine / "a.txt").rename(w.tmp / "a-aside.txt")
        (w.tmp / "replacement").write_text("replacement\n")
        (w.tmp / "replacement").rename(quarantine / "a.txt")

    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.ENTRY_SWAPPED)
    assert record["restored_to"] == str(item) and record["needs_you"] is True
    assert (item / "a.txt").read_text() == "replacement\n"
    assert (item / "sub" / "b.txt").read_text() == "beta\n"
    assert (w.tmp / "a-aside.txt").read_text() == "alpha\n"


def test_extract_root_removal_set_is_compared_inventory(w):
    """Catches the removal set taken from the plan's ``entries``: the plan of
    an extract root is edited to list one more entry and resealed (body
    digest and plans.jsonl row, so it loads), and at seam ``after_compare``
    that file appears in the quarantine; it is not in the compared walk, so
    it survives and the record says LATE_ENTRY_RESTORED. Mutant: the
    removal set taken from the plan's entries (it removes the file).
    Negative control: the resealed plan loads (the execute is DONE) and the
    walked entries are removed."""
    root, _ = _extract_world(w)
    # The target holding the placement is registered (Revision 10 item 5).
    rel = w.relocation(target=w.target("t9")[0])
    plan = rel.plan(PROJECT, [root])
    assert [i["path"] for i in plan.items] == [str(root)]
    reseal(w, plan.path,
           lambda body: body["items"][0]["entries"].append(["tree/added.txt", "file"]))

    @hook("after_compare")
    def add(item, quarantine, record):
        (quarantine / "tree" / "added.txt").write_text("added after the compare\n")

    report = rel.execute(plan.path)
    assert report.status == vr.DONE, report.detail
    record = by_path(report, root)
    assert record["status"] == vr.LATE_ENTRY_RESTORED
    assert (root / "tree" / "added.txt").read_text() == "added after the compare\n"
    assert sorted(os.listdir(root)) == ["tree"] and os.listdir(root / "tree") == ["added.txt"]


def test_entries_digest_mismatch_kept(w):
    """Catches the entries-digest comparison dropped: a REPRODUCIBLE_SCRATCH
    clone whose plan is resealed with a different ``entries_digest``
    reaches the quarantine compare (its bundle equals the clone) and is KEPT
    (ENTRIES_MISMATCH), restored to its name. Mutant: the comparison dropped
    (the clone is removed). Negative control: an unforged plan of the same
    clone relocates it."""
    clone = w.clone("clone")
    w.end_setup()
    rel = w.relocation()
    plan = rel.plan(PROJECT, [clone])
    reseal(w, plan.path, lambda body: body["items"][0].update(entries_digest="0" * 64))
    report = rel.execute(plan.path)
    record = by_path(report, clone)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.ENTRIES_MISMATCH)
    assert record["restored_to"] == str(clone) and (clone / "README").read_text() == "canonical\n"
    _, _, report = plan_exec(w, clone, rel=rel)
    assert by_path(report, clone)["status"] == vr.RELOCATED and not clone.exists()


def test_entry_created_between_compare_and_removal_survives(w):
    """Catches a second walk feeding the removal: a VERIFIED_DUPLICATE; at
    seam ``after_compare`` a new file appears in the quarantine; the removal
    acts on the compared walk, so the file survives (LATE_ENTRY_RESTORED,
    back under the item's name). Mutant: the quarantine re-walked for the
    removal (it removes the new file). Negative control: without a new file
    the duplicate relocates (`test_duplicate_relocates_with_receipt`)."""
    item = w.copy("dup")
    w.end_setup()

    @hook("after_compare")
    def appear(item, quarantine, record):
        (quarantine / "sub" / "new.txt").write_text("appeared after the compare\n")

    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.LATE_ENTRY_RESTORED
    assert (item / "sub" / "new.txt").read_text() == "appeared after the compare\n"
    assert not (item / "a.txt").exists() and not (item / "sub" / "b.txt").exists()


# --------------------------------------------------------------------------- #
# K13 -- the removal-time re-checks (Revision 8 item 2)
# --------------------------------------------------------------------------- #


def test_reference_after_place_keeps_item(w):
    """Catches the reference re-check in execute dropped: at seam
    ``after_place`` an open DOGFOOD_LEDGER.md record names the item; the
    item is KEPT (REFERENCED) and never quarantined. Mutant: the reference
    re-check dropped (the item is removed). Negative control: once the
    record is closed, the next plan relocates it."""
    item = w.copy("dup")
    w.end_setup()

    @hook("after_place")
    def name_it(item, record):
        w.ledger(f"## Offen\n\n- {item['path']} is still compared by a reviewer\n")

    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.REFERENCED)
    assert record["quarantine_path"] is None and (item / "a.txt").read_text() == "alpha\n"
    vr._hooks.clear()
    w.ledger(f"## Erledigt\n\n- {item} compared\n")
    _, _, report = plan_exec(w, item)
    assert by_path(report, item)["status"] == vr.RELOCATED and not item.exists()


def test_bytes_changed_before_removal_keeps_item(w):
    """Catches the final re-read of a VERIFIED_DUPLICATE dropped or forced
    equal: at seam ``after_compare`` one byte of the quarantined item
    changes; the re-read against the current originals just before removal
    finds the difference, so the item is KEPT (ITEM_CHANGED) and restored
    with its changed bytes. Mutant: the final re-read dropped (the changed
    file is removed). Negative control: unchanged, it relocates
    (`test_duplicate_relocates_with_receipt`)."""
    item = w.copy("dup")
    w.end_setup()

    @hook("after_compare")
    def flip(item, quarantine, record):
        (quarantine / "a.txt").write_text("alphb\n")

    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.ITEM_CHANGED)
    assert record["restored_to"] == str(item) and (item / "a.txt").read_text() == "alphb\n"
    assert (item / "sub" / "b.txt").read_text() == "beta\n"


def _lock_ex_refused(lock: Path) -> bool:
    import fcntl
    fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def test_no_run_starts_during_removal(w):
    """Catches the run hold that does not hold (Revision 5a note 2): a
    duplicate of a retention-listed original; at seam
    ``before_inode_check`` an exclusive non-blocking lock on the original's
    run's ``.process.lock`` -- what a starting run takes -- is refused, and
    the duplicate relocates. Mutant: `_hold_run` returns without holding.
    Negative control: after the execute the same lock is granted."""
    r1, r2 = runs_world(w, "20261002t0101z-ra", "20261002t0102z-rb")
    for run in (r1, r2):                    # each run has had a process, now ended
        with RunStore(run.parent, run.name).process_marker():
            pass
    w.end_setup()
    copy = copy_of(w, r1 / "out", "copy")
    seen = []

    @hook("before_inode_check")
    def try_to_start(item, quarantine, record):
        for original in item["originals"]:
            lock = Path(original["path"]).parent.parent / ".process.lock"
            seen.append((lock, _lock_ex_refused(lock)))

    _, _, report = plan_exec(w, copy)
    assert by_path(report, copy)["status"] == vr.RELOCATED
    assert seen and all(refused for _, refused in seen), seen
    assert not any(_lock_ex_refused(lock) for lock, _ in seen)


def test_run_hold_without_lock_file(w):
    """Catches an absent ``.process.lock`` skipped by the run hold: a run
    whose lock file does not exist at the re-check is held too -- the hold
    creates it (``O_CREAT | O_NOFOLLOW``, never ``O_TRUNC``) relative to the
    run directory's descriptor and locks it, so at seam
    ``before_inode_check`` an exclusive lock is refused; the created file
    stays, empty. Mutant: the absent file skipped (nothing to lock, so a
    run could start). Negative control: before the execute no lock file
    exists."""
    r1, r2 = runs_world(w, "20261002t0111z-ra", "20261002t0112z-rb")
    w.end_setup()
    assert not any(os.path.lexists(r / ".process.lock") for r in (r1, r2))
    copy = copy_of(w, r1 / "out", "copy")
    seen = []

    @hook("before_inode_check")
    def try_to_start(item, quarantine, record):
        for original in item["originals"]:
            lock = Path(original["path"]).parent.parent / ".process.lock"
            seen.append((lock, os.path.lexists(lock) and _lock_ex_refused(lock)))

    _, _, report = plan_exec(w, copy)
    assert by_path(report, copy)["status"] == vr.RELOCATED
    assert seen and all(refused for _, refused in seen), seen
    for lock, _ in seen:
        assert lock.is_file() and not lock.is_symlink() and lock.stat().st_size == 0


# --------------------------------------------------------------------------- #
# K12 -- every staged copy is reported (Revision 8 item 3)
# --------------------------------------------------------------------------- #


def _failing_payload(monkeypatch):
    """Make every build fail after its payload is written (ENOSPC)."""
    def full(source, bundle_dir):
        raise OSError(28, "No space left on device (simulated)")
    monkeypatch.setattr(ab, "_after_payload_hook", full)


def test_failed_target_build_leftover_reported(w, monkeypatch):
    """Catches guest-only leftover recording (sstew-a B2): with guest staging
    refused, the build on ``<target>/staging/<p>/`` fails after writing its
    payload; the ``.building`` directory it left is recorded as
    STAGING_LEFTOVER (where=target, path, bytes) in the ReclaimReceipt and
    in the steward receipt, and stays where it is. Mutant: leftovers
    recorded for guest builds only. Negative control: the item is kept and
    untouched."""
    import hoh.storage_steward as ss
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    _failing_payload(monkeypatch)
    admission = FakeAdmission("PRESSURE", admit=False)
    rel = w.relocation(target=target, admission=admission)
    steward = ss.StorageSteward(PROJECT, w.registry, admission, rel,
                                ss.StewardPolicy(target_timeout_s=60.0))
    report = steward.run_once()
    [kept] = report["items_kept"]
    assert kept["path"] == str(item) and kept["why"].startswith(vr.BUILD_FAILED)
    [leftover] = report["staging_leftovers"]
    assert leftover["where"] == "target" and leftover["status"] == vr.STAGING_LEFTOVER
    staged = Path(leftover["path"])
    assert staged.parent == target.root / "staging" / PROJECT
    assert staged.name.endswith(".building") and (staged / ab.INCOMPLETE).is_file()
    assert leftover["bytes"] == sum(f.stat().st_size for f in staged.rglob("*") if f.is_file())
    assert leftover["bytes"] > 0
    [receipt] = [json.loads(p.read_text()) for p in w.receipts.glob("reclaim-*.json")]
    assert receipt["staging_leftover"] == leftover and receipt["status"] == vr.KEPT
    on_disk = json.loads(Path(report["receipt_path"]).read_text())
    assert on_disk["staging_leftovers"] == [leftover]
    assert (item / "a.txt").read_text() == "alpha\n"


def test_timed_out_target_build_cancelled_and_reported(w, monkeypatch):
    """Catches a timed-out target build neither cancelled nor reported: a
    small target timeout (1 s) and a slow `build_tree` (0.6 s before each
    entry) on target-side staging -- the relocation sets the cancel, the
    builder stops with `BuildCancelled` before writing all entries, and the
    receipt carries the leftover (STAGING_LEFTOVER, or STILL_WRITING had the
    builder not stopped in the second wait). The builder thread is joined
    before teardown. Mutants: the cancel not set (the build runs to the
    end); the leftover not recorded. Negative control: the item is kept
    (TARGET_TIMEOUT) and untouched."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    real = ab.ArchiveBundle.build_tree
    seen: dict = {"cancelled": False}

    def slow_build(source, out_parent, *, cancel=None, **kw):
        seen["thread"] = threading.current_thread()

        def slow_cancel():
            import time
            time.sleep(0.6)
            stop = bool(cancel and cancel())
            seen["cancelled"] = seen["cancelled"] or stop
            return stop
        try:
            bundle = real(source, out_parent, cancel=slow_cancel, **kw)
        except ab.BuildCancelled:
            seen["outcome"] = "cancelled"
            raise
        seen["outcome"] = "finished"
        return bundle

    monkeypatch.setattr(ab.ArchiveBundle, "build_tree", staticmethod(slow_build))
    try:
        _, _, report = plan_exec(w, item, target=target, timeout=1.0,
                                 admission=FakeAdmission("PRESSURE", admit=False))
    finally:
        if seen.get("thread") is not None:
            seen["thread"].join(30)
    assert not seen["thread"].is_alive()
    assert seen["cancelled"] is True and seen["outcome"] == "cancelled"
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.TARGET_TIMEOUT)
    leftover = record["staging_leftover"]
    assert leftover["where"] == "target"
    assert leftover["status"] in (vr.STAGING_LEFTOVER, vr.STILL_WRITING)
    assert Path(leftover["path"]).parent == target.root / "staging" / PROJECT
    on_disk = json.loads(Path(record["receipt_path"]).read_text())
    assert on_disk["staging_leftover"] == leftover
    assert (item / "a.txt").read_text() == "alpha\n"


def _staged_names(target: at.ArchiveTarget) -> list[str]:
    directory = target.root / "staging" / PROJECT
    return sorted(os.listdir(directory)) if directory.exists() else []


def test_leftover_blocks_rebuild_on_same_target(w, monkeypatch):
    """Catches no block: after a target-side build of an item failed and left
    a reported leftover, the next execute of that item does not build on the
    same target while the leftover is present -- KEPT (NO_STAGING:
    LEFTOVER_PRESENT), nothing new staged. Mutant: no block (a second staged
    copy appears). Negative control: once the user has taken the leftover
    away (simulated by a rename out of the target), the item is built and
    relocated."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    refused = FakeAdmission("PRESSURE", admit=False)
    _failing_payload(monkeypatch)
    _, _, report = plan_exec(w, item, target=target, admission=refused)
    first = by_path(report, item)
    assert first["reason"].startswith(vr.BUILD_FAILED)
    leftover = Path(first["staging_leftover"]["path"])
    staged = _staged_names(target)
    assert staged == [leftover.name]
    monkeypatch.setattr(ab, "_after_payload_hook", None)
    _, _, report = plan_exec(w, item, target=target, admission=refused)
    second = by_path(report, item)
    assert second["status"] == vr.KEPT and vr.LEFTOVER_PRESENT in second["reason"]
    assert _staged_names(target) == staged and item.is_dir()
    leftover.rename(w.tmp / "taken-by-the-user")
    _, _, report = plan_exec(w, item, target=target, admission=refused)
    assert by_path(report, item)["status"] == vr.RELOCATED and not item.exists()


def test_unmeasurable_leftover_counts_present(w, monkeypatch):
    """Catches an unmeasurable leftover read as absent: when the presence of
    a reported target-side leftover cannot be measured (its probe fails with
    a permission error), it counts as present -- the item is KEPT
    (LEFTOVER_PRESENT, "unmeasurable") and nothing new is staged. Mutant:
    unmeasurable read as absent (the item is built again). Negative
    control: with the probe working and the leftover gone, the item
    relocates."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    refused = FakeAdmission("PRESSURE", admit=False)
    _failing_payload(monkeypatch)
    _, _, report = plan_exec(w, item, target=target, admission=refused)
    leftover = Path(by_path(report, item)["staging_leftover"]["path"])
    monkeypatch.setattr(ab, "_after_payload_hook", None)
    staged = _staged_names(target)
    real_probe = vr._leftover_present

    def unreadable(path):
        raise PermissionError(13, "Permission denied (simulated)", path)

    monkeypatch.setattr(vr, "_leftover_present", unreadable)
    _, _, report = plan_exec(w, item, target=target, admission=refused)
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and vr.LEFTOVER_PRESENT in record["reason"]
    assert "unmeasurable" in record["reason"]
    assert _staged_names(target) == staged and item.is_dir()
    monkeypatch.setattr(vr, "_leftover_present", real_probe)
    leftover.rename(w.tmp / "taken-by-the-user")
    _, _, report = plan_exec(w, item, target=target, admission=refused)
    assert by_path(report, item)["status"] == vr.RELOCATED


# --------------------------------------------------------------------------- #
# K14 -- durable intent and post-delete absence (Revision 8 item 4)
# --------------------------------------------------------------------------- #


class Crash(BaseException):
    """The process dies here: not an `Exception`, so nothing in `execute`
    turns it into a receipt."""


def _intents(world: World) -> list[dict]:
    path = world.registry.state_root / "projects" / PROJECT / "scratch" / \
        vr.RELOCATION_SESSION / vr.INTENTS_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_interrupted_removal_resolved_from_intent(w, monkeypatch):
    """Catches an interrupted execute that leaves no durable trace or is
    resolved wrongly (Revision 8 item 4). An execute dies (a `BaseException`)
    at each point, and the next steward pass's recovery -- which renames and
    removes nothing -- resolves the open intent; each pass's own execute
    then runs, not refused BUSY:

    * after the intent, before the quarantine rename -> KEPT INTERRUPTED;
    * at the first unlink (quarantine present, removal_started) -> PARTIAL;
    * the same, with the quarantine back under the item's name (a restore
      after part of the removal) -> PARTIAL, told only by removal_started;
    * after the last unlink, with the bundle receipt renamed away ->
      UNRESOLVED (NEEDS_YOU, judged again next pass);
    * the same with the receipt back -> RELOCATED, ``recovered: true``.

    Mutants: no intent written; a recovery that removes or renames;
    removal_started ignored; the bundle receipt not checked. Negative
    control: a completed execute leaves no open intent, and the journal --
    every record completed -- stays beside ``relocation.lock`` (nothing
    named ``intents-*`` under the receipts)."""
    import hoh.storage_steward as ss
    w.end_setup()
    target, _ = w.target()
    admission = FakeAdmission("PRESSURE", admit=False)
    rel = w.relocation(target=target, admission=admission)
    steward = ss.StorageSteward(PROJECT, w.registry, admission, rel,
                                ss.StewardPolicy(target_timeout_s=60.0))

    def crash_at(name):
        def crash(**context):
            raise Crash(name)
        vr._hooks[name] = crash

    def crashed_execute(item):
        plan = rel.plan(PROJECT, [item])
        assert [i["path"] for i in plan.items] == [str(item)]
        with pytest.raises(Crash):
            rel.execute(plan.path)
        vr._hooks.clear()
        [intent] = [r for r in _intents(w) if r.get("item") == str(item)
                    and "classification" in r]
        assert intent["schema"] == vr.INTENT_SCHEMA
        for key in ("item", "root", "classification", "plan_digest", "receipt_digest",
                    "bundle_path", "target_id", "quarantine_name", "written_at"):
            assert key in intent, key
        return intent

    def steward_pass(watch):
        """One pass with a fresh eligible copy; `watch` maps paths to the
        existence they must still have when the pass's execute starts."""
        fresh = w.copy(f"fresh-{len(passes)}")
        at_execute = {}

        @hook("after_lock")
        def snapshot(plan_path, project_id):
            at_execute.update({p: os.path.lexists(p) for p in watch})

        report = steward.run_once()
        vr._hooks.clear()
        assert report["execute"]["status"] == vr.DONE, report["execute"]
        assert at_execute == watch, at_execute
        assert not fresh.exists()
        passes.append(report)
        return report

    passes: list = []

    # 1. after the intent, before the rename: KEPT INTERRUPTED
    item = w.copy("a-interrupted")
    crash_at("after_intent")
    intent = crashed_execute(item)
    quarantine = Path(intent["quarantine_name"])
    assert item.is_dir() and not quarantine.exists()
    report = steward_pass({str(item): True, str(quarantine): False})
    [outcome] = report["recovered"]
    assert outcome["status"] == vr.KEPT and outcome["detail"].startswith(vr.INTERRUPTED)
    recovered = json.loads(Path(outcome["receipt_path"]).read_text())
    assert recovered["recovered"] is True and recovered["status"] == vr.KEPT

    # 2. at the first unlink: the quarantine is there -> PARTIAL
    item = w.copy("b-partial")
    real_unlink = vr._unlink_entry

    def crash_unlinking(name, dir_fd):
        raise Crash("unlink")

    monkeypatch.setattr(vr, "_unlink_entry", crash_unlinking)
    intent = crashed_execute(item)
    monkeypatch.setattr(vr, "_unlink_entry", real_unlink)
    quarantine = Path(intent["quarantine_name"])
    assert quarantine.is_dir() and not item.exists()
    assert any(r.get("removal_started") and r.get("receipt_id") == intent["receipt_id"]
               for r in _intents(w))
    report = steward_pass({str(item): False, str(quarantine): True})
    [outcome] = report["recovered"]
    assert outcome["status"] == vr.PARTIAL and outcome["needs_you"] is True
    assert report["needs_you"] is True
    assert sorted(os.listdir(quarantine)) == ["a.txt", "sub"]

    # 3. the same, restored under its name after part of the removal:
    #    only removal_started tells
    item = w.copy("c-started")
    monkeypatch.setattr(vr, "_unlink_entry", crash_unlinking)
    intent = crashed_execute(item)
    monkeypatch.setattr(vr, "_unlink_entry", real_unlink)
    Path(intent["quarantine_name"]).rename(item)
    report = steward_pass({str(item): True, intent["quarantine_name"]: False})
    [outcome] = report["recovered"]
    assert outcome["status"] == vr.PARTIAL and outcome["item"] == str(item)

    # 4. after the last unlink, the bundle receipt renamed away: UNRESOLVED
    item = w.copy("d-removed")
    crash_at("after_removal")
    intent = crashed_execute(item)
    assert not item.exists() and not Path(intent["quarantine_name"]).exists()
    receipt = Path(intent["bundle_path"]) / ab.RECEIPT
    assert st.sha256_file(receipt) == intent["receipt_digest"]
    receipt.rename(w.tmp / "receipt-aside.json")
    report = steward_pass({str(item): False, intent["quarantine_name"]: False})
    [outcome] = report["recovered"]
    assert outcome["status"] == vr.UNRESOLVED and outcome["needs_you"] is True
    assert not (w.receipts / f"reclaim-{intent['receipt_id']}.json").exists()

    # 5. the receipt back: RELOCATED, recovered
    (w.tmp / "receipt-aside.json").rename(receipt)
    report = steward_pass({str(item): False, intent["quarantine_name"]: False})
    [outcome] = report["recovered"]
    assert outcome["status"] == vr.RELOCATED
    recovered = json.loads(Path(outcome["receipt_path"]).read_text())
    assert recovered["recovered"] is True and recovered["post_delete_absence"] is True
    assert recovered["bundle_receipt_digest"] == intent["receipt_digest"]

    # nothing open is left; the journal stays append-only beside the lock
    # (Revision 8 item 4): every intent of this test is still in it, now
    # completed, and nothing was moved to the receipts
    assert vr._open_intents(steward.relocation_scratch) == []
    journal = steward.relocation_scratch / vr.INTENTS_NAME
    assert journal.parent == (steward.relocation_scratch / vr.LOCK_NAME).parent
    assert journal.is_file() and _intents(w)
    assert intent["receipt_id"] in {r.get("receipt_id") for r in _intents(w)
                                    if "completed" in r}
    assert not list(w.receipts.glob("intents-*.jsonl"))
    assert steward.run_once()["recovered"] == []


def test_post_delete_absence_checks_item_path(w):
    """Catches ``post_delete_absence`` checking the quarantine name only (or
    forced true): through seam ``after_removal`` a file appears at the
    item's own path after the last unlink; the record says
    ``post_delete_absence: false`` and PARTIAL (NEEDS_YOU), and that file is
    untouched. Mutant: post_delete_absence forced true (RELOCATED).
    Negative control: without it the duplicate relocates with
    ``post_delete_absence: true`` (`test_duplicate_relocates_with_receipt`)."""
    item = w.copy("dup")
    w.end_setup()

    @hook("after_removal")
    def back(item, quarantine, record):
        Path(item["path"]).write_text("something new under the old name\n")

    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["post_delete_absence"] is False
    assert record["status"] == vr.PARTIAL and record["needs_you"] is True
    assert item.read_text() == "something new under the old name\n"
    assert not Path(record["quarantine_path"]).exists()


# --------------------------------------------------------------------------- #
# iteration 2 -- the journal stays beside the lock; extract-root intents
# --------------------------------------------------------------------------- #


def test_completed_intent_journal_stays_beside_lock(w):
    """Catches the completed journal moved away from ``relocation.lock``
    (Revision 8 item 4 places ``intents.jsonl`` beside the lock, append-only,
    and `recover` reads only that file): after a successful duplicate
    relocation the intent, its ``removal_started`` record and its completion
    record are all in the journal beside the lock, and nothing named
    ``intents-*`` appeared under the receipts. Mutant: the journal moved to
    ``<receipts_dir>/intents-<UTC>-<hex>.jsonl`` once every intent is
    completed. Negative control: the same relocation leaves the receipts
    directory holding the ReclaimReceipt (the receipts are written, only the
    journal is not moved there)."""
    item = w.copy("dup")
    w.end_setup()
    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.RELOCATED and not item.exists()
    scratch = w.registry.state_root / "projects" / PROJECT / "scratch" / vr.RELOCATION_SESSION
    journal = scratch / vr.INTENTS_NAME
    assert (scratch / vr.LOCK_NAME).is_file() and journal.is_file()
    mine = [r for r in _intents(w) if r.get("receipt_id") == record["receipt_id"]]
    # Revision 10 item 10: the build record first, then the intent,
    # removal_started and the completion.
    assert [("classification" in r, bool(r.get("removal_started")), "completed" in r)
            for r in mine] == [(False, False, False), (True, False, False),
                               (False, True, False), (False, False, True)], mine
    assert mine[1]["item"] == str(item) and mine[1]["quarantine_name"] == \
        record["quarantine_path"]
    assert vr._open_intents(scratch) == []
    assert not list(w.receipts.rglob("intents-*"))
    assert (w.receipts / f"reclaim-{record['receipt_id']}.json").is_file()
    assert not hasattr(report, "journal_archived")


def _interrupted_extract_root(world: World):
    """An eligible extract root (C2-indexed, only EXTRACT_ROOT.json and
    tree/, its receipt committed at HEAD -- built as `_extract_world` builds
    it) whose execute dies at seam ``after_removal``: the item and its
    quarantine name are both gone, the intent is open. Returns the steward
    (state NORMAL, so a pass recovers and executes nothing), the open intent
    and the placement."""
    import hoh.storage_steward as ss
    root, placed = _extract_world(world)
    target, _ = world.target("t9")      # the target the placement used
    rel = world.relocation(target=target)
    plan = rel.plan(PROJECT, [root])
    assert [i["path"] for i in plan.items] == [str(root)]
    assert plan.items[0]["proof"]["type"] == "extract_root"

    def crash(**context):
        raise Crash("after_removal")

    vr._hooks["after_removal"] = crash
    with pytest.raises(Crash):
        rel.execute(plan.path)
    vr._hooks.clear()
    [intent] = [r for r in _intents(world) if r.get("item") == str(root)
                and "classification" in r]
    assert not root.exists() and not os.path.lexists(intent["quarantine_name"])
    assert intent["target_id"] is None   # an extract root places nothing
    assert intent["bundle_path"] == os.path.realpath(placed.path)
    steward = ss.StorageSteward(PROJECT, world.registry, FakeAdmission("NORMAL"), rel,
                                ss.StewardPolicy(target_timeout_s=60.0))
    return steward, intent, placed


def _tree_state(*roots: Path) -> dict[str, tuple]:
    """Every path below `roots` with its inode, type and size -- what a
    rename or a removal would change."""
    out = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                path = os.path.join(dirpath, name)
                info = os.lstat(path)
                out[path] = (info.st_ino, info.st_mode, info.st_size)
    return out


def test_interrupted_extract_root_intent_resolved(w):
    """Catches a recovery that cannot resolve an interrupted extract-root
    removal: its intent has ``target_id`` None (an extract root is released
    on its committed receipt and places nothing), so the target is found by
    containment of the intent's ``bundle_path``; the target's
    ``ARCHIVE_RECEIPT.json`` equals the digest the intent pinned, and the
    next steward pass resolves the intent to RELOCATED with ``recovered:
    true`` and a completion record; the pass after it recovers nothing.
    Mutant 1: a recovery that requires ``target_id`` to match a configured
    target (UNRESOLVED on every pass, never resolved). Negative control:
    `test_interrupted_extract_root_intent_receipt_mismatch_unresolved`
    changes only the target receipt's bytes."""
    steward, intent, placed = _interrupted_extract_root(w)
    target_receipt = Path(intent["bundle_path"]) / ab.RECEIPT
    assert st.sha256_file(target_receipt) == intent["receipt_digest"]
    report = steward.run_once()
    [outcome] = report["recovered"]
    assert outcome["status"] == vr.RELOCATED, outcome
    assert outcome["needs_you"] is False
    recovered = json.loads(Path(outcome["receipt_path"]).read_text())
    assert recovered["recovered"] is True and recovered["status"] == vr.RELOCATED
    assert recovered["post_delete_absence"] is True and recovered["target_id"] is None
    assert recovered["bundle_receipt_digest"] == intent["receipt_digest"]
    assert recovered["classification"] == st.REPRODUCIBLE_SCRATCH
    assert [r for r in _intents(w) if r.get("receipt_id") == intent["receipt_id"]
            and "completed" in r]
    assert vr._open_intents(steward.relocation_scratch) == []
    assert Path(report["receipt_path"]).is_file()     # the pass's steward receipt
    assert target_receipt.is_file() and placed.path.is_dir()
    assert steward.run_once()["recovered"] == []


def test_interrupted_extract_root_intent_receipt_mismatch_unresolved(w):
    """Catches the target receipt's digest not compared when an extract-root
    intent is recovered: the same interrupted removal as
    `test_interrupted_extract_root_intent_resolved`, but the bytes of the
    target's ``<bundle>/ARCHIVE_RECEIPT.json`` now differ from the receipt
    file the intent pinned; the pass reports UNRESOLVED with NEEDS_YOU,
    renames and removes nothing in the project's scratch or on the target,
    writes no ReclaimReceipt and no completion record, and judges it again
    on the next pass. Mutant 2: the receipt digest not compared (RELOCATED).
    Negative control: the resolved test, with the receipt's bytes intact."""
    steward, intent, placed = _interrupted_extract_root(w)
    target_receipt = Path(intent["bundle_path"]) / ab.RECEIPT
    original = target_receipt.read_bytes()
    target_receipt.rename(w.tmp / "receipt-original.json")   # set aside, never removed
    target_receipt.write_bytes(original + b"\n")
    assert st.sha256_file(target_receipt) != intent["receipt_digest"]
    scratch = w.registry.state_root / "projects" / PROJECT / "scratch"
    before = _tree_state(scratch, placed.path.parent)
    for _ in range(2):                  # judged again, never resolved by itself
        report = steward.run_once()
        [outcome] = report["recovered"]
        assert outcome["status"] == vr.UNRESOLVED and outcome["needs_you"] is True
        assert report["needs_you"] is True
        assert "receipt" in outcome["detail"]
        assert _tree_state(scratch, placed.path.parent) == before
        assert not (w.receipts / f"reclaim-{intent['receipt_id']}.json").exists()
        assert [i["receipt_id"] for i in vr._open_intents(steward.relocation_scratch)] == \
            [intent["receipt_id"]]
    assert target_receipt.read_bytes() == original + b"\n"


# --------------------------------------------------------------------------- #
# K18 -- Revision 9 items 1, 3, 4, 5, 6
# --------------------------------------------------------------------------- #


def _join_builders(seen: dict) -> None:
    for thread in seen.get("threads", []):
        thread.join(60)
    assert not any(t.is_alive() for t in seen.get("threads", []))


def _still_writing_first_pass(world: World, monkeypatch):
    """A first pass whose target-side build outlives the timeout (1 s) and its
    second wait, then finishes on its own: the builder ignores the cancel and
    sleeps 3 s before its first entry. The relocation reports the build
    STILL_WRITING at its ``.building`` path; the builder later renames it to
    the finished ``vh-arch-...-<receipt-id>``. The builder thread is joined
    before returning; the real `build_tree` is back in place afterwards.
    Returns the item, the target, the first record and the finished bundle."""
    item = world.copy("dup")
    world.end_setup()
    target, _ = world.target()
    real = ab.ArchiveBundle.build_tree
    seen: dict = {"threads": [], "calls": 0}

    def outliving_build(source, out_parent, *, cancel=None, **kw):
        seen["threads"].append(threading.current_thread())

        def ignore_cancel():
            import time
            seen["calls"] += 1
            if seen["calls"] == 1:
                time.sleep(3.0)
            return False
        bundle = real(source, out_parent, cancel=ignore_cancel, **kw)
        seen["bundle"] = bundle
        return bundle

    monkeypatch.setattr(ab.ArchiveBundle, "build_tree", staticmethod(outliving_build))
    try:
        _, _, report = plan_exec(world, item, target=target, timeout=1.0,
                                 admission=FakeAdmission("PRESSURE", admit=False))
    finally:
        _join_builders(seen)
        monkeypatch.setattr(ab.ArchiveBundle, "build_tree", staticmethod(real))
    first = by_path(report, item)
    assert first["status"] == vr.KEPT and first["reason"].startswith(vr.TARGET_TIMEOUT)
    leftover = first["staging_leftover"]
    assert leftover["status"] == vr.STILL_WRITING and leftover["where"] == "target"
    stored = leftover["paths"]
    # what the first receipt stored is the .building directory, gone now
    assert stored and all(p.endswith(".building") for p in stored), stored
    assert not any(os.path.lexists(p) for p in stored)
    finished = Path(seen["bundle"])
    assert finished.is_dir() and finished.parent == target.root / "staging" / PROJECT
    assert finished.name.endswith("-" + first["receipt_id"])
    return item, target, first, finished


def test_still_writing_build_finished_blocks_rebuild(w, monkeypatch):
    """Catches a leftover followed only at the path its receipt stored
    (sstew2-b B1, Revision 9 item 1): a target-side build reported
    STILL_WRITING (timeout 1 s) finishes later under its final name
    ``vh-arch-...-<receipt-id>``; the next pass -- staging on the target
    again -- lists ``<target>/staging/<p>/`` for the earlier receipt's label,
    finds the finished bundle, does not build there (KEPT, NO_STAGING:
    LEFTOVER_PRESENT, nothing new staged) and reports it in
    ``target_leftovers`` (target id, STAGING_LEFTOVER, its current path,
    bytes). Mutant: the stored path alone checked (the ``.building`` path is
    gone, so the item is built a second time). Negative control: once the
    user has taken the finished bundle away (a rename out of the target), the
    item is built and relocated."""
    item, target, first, finished = _still_writing_first_pass(w, monkeypatch)
    refused = FakeAdmission("PRESSURE", admit=False)
    staged = _staged_names(target)
    assert staged == [finished.name]
    _, _, report = plan_exec(w, item, target=target, admission=refused)
    second = by_path(report, item)
    assert second["status"] == vr.KEPT and second["reason"].startswith(vr.NO_STAGING)
    assert vr.LEFTOVER_PRESENT in second["reason"] and str(finished) in second["reason"]
    assert _staged_names(target) == staged and (item / "a.txt").read_text() == "alpha\n"
    assert [(x["target_id"], x["status"], x["path"]) for x in second["target_leftovers"]] == [
        (target.target_id, vr.STAGING_LEFTOVER, str(finished))]
    assert second["target_leftovers"][0]["bytes"] == vr._tree_bytes(finished) > 0
    on_disk = json.loads(Path(second["receipt_path"]).read_text())
    assert on_disk["target_leftovers"] == second["target_leftovers"]
    finished.rename(w.tmp / "taken-by-the-user")
    _, _, report = plan_exec(w, item, target=target, admission=refused)
    third = by_path(report, item)
    assert third["status"] == vr.RELOCATED and not item.exists()
    assert third["target_leftovers"] == []


def test_leftover_reported_on_guest_staging_pass(w, monkeypatch):
    """Catches leftovers followed only where the item would be staged on the
    target (sstew3spec-a B1, Revision 9 item 1): after the same STILL_WRITING
    first pass, the second pass is admitted with guest staging -- it builds
    in the guest, places on the target and relocates the item, and its
    receipt still names the finished bundle on the target in
    ``target_leftovers`` (``staging_leftover`` is this pass's own guest
    build). Mutant: the listing only on the target-staging path (the guest
    pass reports nothing on the target). Negative control: the finished
    bundle is untouched on the target -- VeriHarness never removes a
    leftover there."""
    item, target, first, finished = _still_writing_first_pass(w, monkeypatch)
    _, _, report = plan_exec(w, item, target=target,
                             admission=FakeAdmission("PRESSURE", admit=True))
    second = by_path(report, item)
    assert second["status"] == vr.RELOCATED and second["staging"]["where"] == "guest"
    assert second["staging_leftover"]["where"] == "guest"
    assert [(x["target_id"], x["status"], x["path"]) for x in second["target_leftovers"]] == [
        (target.target_id, vr.STAGING_LEFTOVER, str(finished))]
    assert finished.is_dir() and (finished / ab.MANIFEST).is_file()


def test_in_place_write_after_compare_kept(w):
    """Catches the per-file re-hash dropped (sstew2-a M1, Revision 9 item 3):
    at seam ``before_inode_check`` -- after the compare and the final
    re-read, the inode unchanged -- one file of the quarantined duplicate is
    appended to in place. Right before its unlink the file is opened
    ``O_NOFOLLOW`` and its content hashed against the inventory, so it
    survives with its new bytes: ``a.txt`` (the first entry removed) -> KEPT
    and restored under the item's name, nothing removed; ``sub/b.txt``
    (after ``a.txt`` was removed) -> PARTIAL under the quarantine name.
    Mutant: no per-file re-hash (the appended file is unlinked and the item
    RELOCATED). Negative control: the untouched files of each item are
    exactly those the restore rule says."""
    outcomes = {}
    for victim in ("a.txt", "sub/b.txt"):
        item = w.copy("dup-" + victim.replace("/", "-"))
        w.end_setup()

        @hook("before_inode_check")
        def append(item, quarantine, record, victim=victim):
            with open(Path(quarantine) / victim, "a", encoding="utf-8") as handle:
                handle.write("written in place\n")

        _, _, report = plan_exec(w, item)
        vr._hooks.clear()
        outcomes[victim] = (item, by_path(report, item))
    item, record = outcomes["a.txt"]
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.ITEM_CHANGED)
    assert record["restored_to"] == str(item) and record["needs_you"] is True
    assert (item / "a.txt").read_text() == "alpha\nwritten in place\n"
    assert (item / "sub" / "b.txt").read_text() == "beta\n"
    item, record = outcomes["sub/b.txt"]
    assert record["status"] == vr.PARTIAL and record["needs_you"] is True
    assert vr.ITEM_CHANGED in record["reason"]
    quarantine = Path(record["quarantine_path"])
    assert (quarantine / "sub" / "b.txt").read_text() == "beta\nwritten in place\n"
    assert not (quarantine / "a.txt").exists() and not item.exists()


def test_swap_during_rehash_kept(w):
    """Catches the final ``fstatat`` compare dropped (Revision 9 item 3):
    through seam ``after_rehash`` -- the walked ``a.txt`` opened, its inode
    and bytes confirmed -- the file is renamed away and a substitute takes
    its name; the ``fstatat`` against the descriptor's inode differs, nothing
    has been removed yet, so the item is KEPT (ENTRY_SWAPPED), restored to
    its name, and the substitute survives. Mutant: the final compare dropped
    (the substitute is unlinked and the item RELOCATED). Negative control:
    the renamed-away original keeps its bytes."""
    item = w.copy("dup")
    w.end_setup()
    aside = w.tmp / "a-aside.txt"

    @hook("after_rehash")
    def swap(rel, record):
        if rel != "a.txt":
            return
        quarantine = Path(record["quarantine_path"])
        (quarantine / "a.txt").rename(aside)
        (w.tmp / "substitute").write_text("substitute\n")
        (w.tmp / "substitute").rename(quarantine / "a.txt")

    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.ENTRY_SWAPPED)
    assert record["restored_to"] == str(item)
    assert (item / "a.txt").read_text() == "substitute\n"
    assert (item / "sub" / "b.txt").read_text() == "beta\n"
    assert aside.read_text() == "alpha\n"


def test_late_entry_restore_holds_no_walked_file(w, monkeypatch):
    """Catches the remainder renamed back while a walked non-directory is
    still present (sstew2-a M4, Revision 9 item 4): the unlink of the walked
    ``sub/b.txt`` succeeds, but a writer puts a file under the same name at
    once; ``rmdir`` of ``sub`` then finds it not empty. That remainder holds
    an entry the inventory contained, so it is not "only new": the item is
    PARTIAL (NEEDS_YOU) under its quarantine name, and the item path stays
    free. Mutant: the remainder renamed back (LATE_ENTRY_RESTORED with the
    walked name under the item's path). Negative control: a remainder that
    holds only a new name (``sub/new.txt``, never walked) is
    LATE_ENTRY_RESTORED under the item's name."""
    item = w.copy("dup")
    w.end_setup()
    real = vr._unlink_entry

    def recreated(name, dir_fd):
        real(name, dir_fd)
        if name == b"b.txt":
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=dir_fd)
            try:
                os.write(fd, b"written again under the walked name\n")
            finally:
                os.close(fd)

    monkeypatch.setattr(vr, "_unlink_entry", recreated)
    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.PARTIAL and record["needs_you"] is True, record["reason"]
    assert "sub/b.txt" in record["reason"]
    quarantine = Path(record["quarantine_path"])
    assert (quarantine / "sub" / "b.txt").read_text() == "written again under the walked name\n"
    assert not item.exists() and not (quarantine / "a.txt").exists()
    monkeypatch.setattr(vr, "_unlink_entry", real)
    control = w.copy("control")

    @hook("after_compare")
    def appear(item, quarantine, record):
        (Path(quarantine) / "sub" / "new.txt").write_text("never walked\n")

    _, _, report = plan_exec(w, control)
    record = by_path(report, control)
    assert record["status"] == vr.LATE_ENTRY_RESTORED
    assert sorted(os.listdir(control)) == ["sub"] and os.listdir(control / "sub") == ["new.txt"]


def _target_at(world: World, name: str, root: Path) -> at.ArchiveTarget:
    """A registered archive target `name` whose root is `root` (which need
    not exist yet), with confirmed identity and health."""
    from test_storage_tiers import Probe
    probe = Probe({"serial": "S-" + name})
    return at.ArchiveTarget({
        "target_id": name, "root": root, "failure_domain": "domain-" + name,
        "expected_identity": {"serial": "S-" + name},
        "identity_probe": probe.read_identity, "health_probe": probe.read_health})


def _relocation_with(world: World, *targets: at.ArchiveTarget) -> vr.VerifiedRelocation:
    return vr.VerifiedRelocation(
        world.registry, list(targets), None, world.receipts, admission=FakeAdmission(),
        sources=lambda project: world.sources(project), getpid=lambda: RELOC_PID,
        boot_id=world.fake.boot_id, timeout=120.0)


def test_item_inside_archive_target_refused(w):
    """Catches the archive-target overlap check dropped, or kept only in
    `plan` (sstew2-a M6, Revision 9 item 5): an item that lies inside a
    registered target's root is refused (INSIDE_ARCHIVE_TARGET) --

    * by `plan`, when the target's root is the scratch directory holding the
      item (decided by ``(st_dev, st_ino)`` ancestry);
    * by `execute`, immediately before the quarantine rename, when the
      target's root did not exist at planning (ENOENT refuses nothing) and,
      at seam ``after_place``, appears as a link to that scratch directory:
      the item is REFUSED, never quarantined, and intact;
    * by `plan` as TARGET_UNMEASURABLE when a target's root cannot be
      measured (its parent is not searchable).

    Mutants: the check dropped; the check only in `plan` (the item is
    removed in the execute case). Negative control: with only the ordinary
    target the same item is planned."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    assert [i["path"] for i in w.relocation(target=target).plan(PROJECT, [item]).items] == \
        [str(item)]
    holding = _target_at(w, "tin", w.scratch)
    plan = _relocation_with(w, target, holding).plan(PROJECT, [item])
    [rejected] = plan.rejected
    assert not plan.items and rejected["reason"].startswith(vr.INSIDE_ARCHIVE_TARGET)
    late_root = w.tmp / "late-root"
    rel = _relocation_with(w, target, _target_at(w, "tlate", late_root))
    plan = rel.plan(PROJECT, [item])
    assert [i["path"] for i in plan.items] == [str(item)]

    @hook("after_place")
    def appear(item, record):
        late_root.symlink_to(w.scratch)

    report = rel.execute(plan.path)
    record = by_path(report, item)
    assert record["status"] == vr.REFUSED, record["reason"]
    assert record["reason"].startswith(vr.INSIDE_ARCHIVE_TARGET)
    assert record["quarantine_path"] is None and (item / "a.txt").read_text() == "alpha\n"
    vr._hooks.clear()
    locked = w.tmp / "locked"
    (locked / "root").mkdir(parents=True)
    locked.chmod(0o000)
    try:
        plan = _relocation_with(w, target, _target_at(w, "tlocked", locked / "root")).plan(
            PROJECT, [item])
    finally:
        locked.chmod(0o755)
    assert not plan.items and plan.rejected[0]["reason"].startswith(vr.TARGET_UNMEASURABLE)


def test_target_root_inside_item_refused(w):
    """Catches the reverse overlap unchecked, or checked only in `plan`
    (Revision 9 item 5): a registered target's root that lies inside the item
    refuses it (INSIDE_ARCHIVE_TARGET) -- by `plan` when the root is the
    item's ``sub`` directory, and by `execute` immediately before the
    quarantine rename when the root, absent at planning, appears at seam
    ``after_place`` as a link to that directory: the item is REFUSED, never
    quarantined, and intact. Mutants: the check dropped; the check only in
    `plan` (the item, with the target's root, is removed). Negative control:
    the planning of the second case shows an absent root refuses nothing."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    plan = _relocation_with(w, target, _target_at(w, "tsub", item / "sub")).plan(
        PROJECT, [item])
    assert not plan.items and plan.rejected[0]["reason"].startswith(vr.INSIDE_ARCHIVE_TARGET)
    late_root = w.tmp / "late-root"
    rel = _relocation_with(w, target, _target_at(w, "tlate", late_root))
    plan = rel.plan(PROJECT, [item])
    assert [i["path"] for i in plan.items] == [str(item)]

    @hook("after_place")
    def appear(item, record):
        late_root.symlink_to(Path(item["path"]) / "sub")

    report = rel.execute(plan.path)
    record = by_path(report, item)
    assert record["status"] == vr.REFUSED, record["reason"]
    assert record["reason"].startswith(vr.INSIDE_ARCHIVE_TARGET)
    assert record["quarantine_path"] is None
    assert (item / "a.txt").read_text() == "alpha\n"
    assert (item / "sub" / "b.txt").read_text() == "beta\n"


def test_recovery_reads_only_inside_target(w, monkeypatch):
    """Catches a recovery that reads a bundle receipt outside the registered
    targets (sstew2-b M4, Revision 9 item 6). From the interrupted
    extract-root removal of `_interrupted_extract_root` (its intent carries
    ``target_id`` None and ``containing_target_id`` ``t9``), further open
    intents are journalled, each with the item and its quarantine gone:

    * named target ``t9``, the bundle inside it -> RELOCATED; the same with
      a byte-identical copy of the bundle outside every target -> UNRESOLVED;
    * no target id, ``containing_target_id`` ``t9``: inside -> RELOCATED,
      outside -> UNRESOLVED;
    * no target id and no ``containing_target_id`` (written before the
      field): inside exactly one target -> RELOCATED, outside -> UNRESOLVED;
    * a bundle inside ``t9`` whose ``ARCHIVE_RECEIPT.json`` is a symlink to
      the real receipt -> UNRESOLVED (``O_NOFOLLOW``);
    * a receipt id this module never generates -> UNRESOLVED, and no
      ReclaimReceipt is written under it.

    No receipt outside ``t9``'s root is ever read. Mutants: containment
    dropped for ``target_id`` None; ``O_NOFOLLOW`` dropped; the receipt id
    unchecked. Negative control: the original intent and every inside case
    resolve RELOCATED."""
    steward, intent, placed = _interrupted_extract_root(w)
    rel = steward.relocation
    [t9] = rel.targets
    assert intent["target_id"] is None and intent["containing_target_id"] == t9.target_id
    bundle = Path(intent["bundle_path"])
    outside = w.tmp / "outside-bundle"
    import shutil
    shutil.copytree(bundle, outside)
    assert st.sha256_file(outside / ab.RECEIPT) == intent["receipt_digest"]
    linked = t9.root / "linked-bundle"
    linked.mkdir()
    (linked / ab.RECEIPT).symlink_to(bundle / ab.RECEIPT)
    scratch = steward.relocation_scratch
    base = {k: v for k, v in intent.items()
            if k not in ("receipt_id", "item", "quarantine_name", "bundle_path", "target_id",
                         "containing_target_id")}
    ids: dict[str, str] = {}

    def journal(case: str, receipt_id: str | None = None, **fields) -> None:
        n = len(ids) + 1
        receipt_id = receipt_id or f"rr-20261002t1200{n:02d}z-{n:08x}"
        gone = w.scratch / f"gone-{case}"
        vr._append_intent(scratch, dict(base, receipt_id=receipt_id, item=str(gone),
                                        quarantine_name=str(gone) + vr.QUARANTINE_MARK + "x",
                                        **fields))
        ids[case] = receipt_id

    journal("named-in", target_id="t9", bundle_path=str(bundle))
    journal("named-out", target_id="t9", bundle_path=str(outside))
    journal("none-in", target_id=None, containing_target_id="t9", bundle_path=str(bundle))
    journal("none-out", target_id=None, containing_target_id="t9", bundle_path=str(outside))
    journal("legacy-in", target_id=None, bundle_path=str(bundle))
    journal("legacy-out", target_id=None, bundle_path=str(outside))
    journal("symlinked", target_id=None, containing_target_id="t9", bundle_path=str(linked))
    journal("malformed", receipt_id="RR-not-ours", target_id=None,
            containing_target_id="t9", bundle_path=str(bundle))
    reads: list[str] = []
    real = vr._hash_fd

    def recording(fd):                  # the descriptor hash of the staging walk
        reads.append(os.readlink(f"/proc/self/fd/{fd}"))
        return real(fd)

    monkeypatch.setattr(vr, "_hash_fd", recording)
    outcomes = {o["receipt_id"]: o for o in rel.recover(PROJECT)}
    status = {case: outcomes[rid]["status"] for case, rid in ids.items()}
    assert status == {
        "named-in": vr.RELOCATED, "named-out": vr.UNRESOLVED,
        "none-in": vr.RELOCATED, "none-out": vr.UNRESOLVED,
        "legacy-in": vr.RELOCATED, "legacy-out": vr.UNRESOLVED,
        "symlinked": vr.UNRESOLVED, "malformed": vr.UNRESOLVED}, status
    assert outcomes[intent["receipt_id"]]["status"] == vr.RELOCATED
    root = os.path.realpath(t9.root)
    # Revision 10a item 1: every receipt was hashed through the staging walk
    # -- from a descriptor whose file lies inside t9's root -- never by path.
    assert reads and all(p.startswith(root + "/") and p.endswith("/" + ab.RECEIPT)
                         for p in reads), reads
    assert not any(str(outside) in p for p in reads)
    assert not list(w.receipts.glob("reclaim-RR-*"))
    for case in ("named-out", "none-out", "legacy-out", "symlinked", "malformed"):
        assert outcomes[ids[case]]["needs_you"] is True
        assert not (w.receipts / f"reclaim-{ids[case]}.json").exists()


# --------------------------------------------------------------------------- #
# K19 -- killing tests for rules already right in the code (Revision 9 item 2)
# --------------------------------------------------------------------------- #


def test_intent_write_failure_keeps_item(w, monkeypatch):
    """Catches a failed intent write ignored (Revision 8 item 4): when the
    durable intent cannot be appended (ENOSPC), nothing is renamed or
    removed -- the item is KEPT (INTENT_FAILED), never quarantined, intact.
    Mutant: the failure ignored (the item is quarantined and removed).
    Negative control: with the journal writable the same item relocates."""
    item = w.copy("dup")
    w.end_setup()
    real = vr._append_intent

    def full(scratch, record):
        if "classification" in record and "completed" not in record:
            raise OSError(28, "No space left on device (simulated)")
        real(scratch, record)

    monkeypatch.setattr(vr, "_append_intent", full)
    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.INTENT_FAILED)
    assert record["quarantine_path"] is None and (item / "a.txt").read_text() == "alpha\n"
    assert not list(w.scratch.glob("dup" + vr.QUARANTINE_MARK + "*"))
    monkeypatch.setattr(vr, "_append_intent", real)
    _, _, report = plan_exec(w, item)
    assert by_path(report, item)["status"] == vr.RELOCATED and not item.exists()


def test_removal_started_write_failure_keeps_item(w, monkeypatch):
    """Catches a failed ``removal_started`` write ignored (Revision 8 item 4):
    when the record that must precede the first unlink cannot be appended,
    nothing is removed -- the item is KEPT (INTENT_FAILED, NEEDS_YOU) and
    restored from its quarantine name with every entry. Mutant: the failure
    ignored (the item is removed without the record). Negative control: the
    intent itself was written (the quarantine was reached)."""
    item = w.copy("dup")
    w.end_setup()
    real = vr._append_intent

    def full(scratch, record):
        if record.get("removal_started"):
            raise OSError(28, "No space left on device (simulated)")
        real(scratch, record)

    monkeypatch.setattr(vr, "_append_intent", full)
    _, _, report = plan_exec(w, item)
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.INTENT_FAILED)
    assert record["needs_you"] is True and record["restored_to"] == str(item)
    assert record["quarantine_path"] and not Path(record["quarantine_path"]).exists()
    assert (item / "a.txt").read_text() == "alpha\n"
    assert (item / "sub" / "b.txt").read_text() == "beta\n"
    assert not any(r.get("removal_started") for r in _intents(w))


def test_timeout_waits_once_more_for_builder(w, monkeypatch):
    """Catches the second wait for a cancelled builder dropped (Revision 8
    item 3): a target-side build that blocks until it is cancelled (it polls
    the cancel every 50 ms) outlives the timeout (1 s); the relocation sets
    the cancel and waits once more, in which the builder stops -- so the
    leftover is STAGING_LEFTOVER (measured after it stopped), not
    STILL_WRITING. Mutant: no second wait (the builder is still running when
    the leftover is measured: STILL_WRITING). Negative control: the builder
    did stop by the cancel (BuildCancelled) and the item is kept intact."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    real = ab.ArchiveBundle.build_tree
    seen: dict = {"threads": [], "outcome": None}

    def blocking_build(source, out_parent, *, cancel=None, **kw):
        seen["threads"].append(threading.current_thread())

        def wait_for_cancel():
            import time
            for _ in range(200):
                if cancel is not None and cancel():
                    return True
                time.sleep(0.05)
            return False
        try:
            return real(source, out_parent, cancel=wait_for_cancel, **kw)
        except ab.BuildCancelled:
            seen["outcome"] = "cancelled"
            raise

    monkeypatch.setattr(ab.ArchiveBundle, "build_tree", staticmethod(blocking_build))
    try:
        _, _, report = plan_exec(w, item, target=target, timeout=1.0,
                                 admission=FakeAdmission("PRESSURE", admit=False))
    finally:
        _join_builders(seen)
    record = by_path(report, item)
    assert seen["outcome"] == "cancelled"
    assert record["status"] == vr.KEPT and record["reason"].startswith(vr.TARGET_TIMEOUT)
    assert "still writing" not in record["reason"]
    leftover = record["staging_leftover"]
    assert leftover["status"] == vr.STAGING_LEFTOVER and leftover["where"] == "target"
    assert leftover["paths"] and all(Path(p).parent == target.root / "staging" / PROJECT
                                     for p in leftover["paths"])
    assert (item / "a.txt").read_text() == "alpha\n"


def test_run_hold_never_follows_symlinks(w):
    """Catches the run hold following links (Revision 5a note 2, Revision 9
    item 2). A duplicate of a retention-listed original; through seam
    ``before_hold`` (after ``owner_of`` answered, before anything is opened):

    * the run's ``.process.lock`` becomes a dangling symlink -- the hold's
      ``O_CREAT | O_NOFOLLOW`` open fails (ELOOP), the item is KEPT and the
      link's target is never created (mutant: ``O_NOFOLLOW`` dropped);
    * the run directory itself is replaced by a symlink to a directory
      outside the namespace -- the hold refuses to open it, the item is KEPT
      and nothing is created in the outside directory (mutant: the run
      directory opened following links).

    Negative control: `test_run_hold_without_lock_file` -- the same hold on
    a plain run directory creates and locks the file, and the duplicate
    relocates."""
    r1, r2 = runs_world(w, "20261002t0121z-ra", "20261002t0122z-rb")
    w.end_setup()
    copy = copy_of(w, r1 / "out", "copy")
    dangling = w.tmp / "lock-target"
    links: list[Path] = []

    @hook("before_hold")
    def link_lock(original, run_dir):
        if not os.path.lexists(run_dir / ".process.lock"):
            (run_dir / ".process.lock").symlink_to(dangling)
            links.append(run_dir / ".process.lock")

    _, _, report = plan_exec(w, copy)
    record = by_path(report, copy)
    assert links and record["status"] == vr.KEPT, record["reason"]
    assert record["reason"].startswith(vr.RUN_PROCESS_LIVE)
    assert not os.path.lexists(dangling) and (copy / "f.txt").read_text() == "same\n"
    for link in links:                          # set aside, never removed
        link.rename(w.tmp / f"lock-link-aside-{link.parent.name}")
    vr._hooks.clear()
    outside = w.tmp / "outside"
    outside.mkdir()
    swapped: list[Path] = []

    @hook("before_hold")
    def swap_run_dir(original, run_dir):
        if run_dir.is_symlink():
            return
        run_dir.rename(w.tmp / f"run-aside-{run_dir.name}")
        run_dir.symlink_to(outside)
        swapped.append(run_dir)

    _, _, report = plan_exec(w, copy)
    record = by_path(report, copy)
    assert swapped and record["status"] == vr.KEPT, record["reason"]
    assert record["reason"].startswith(vr.RUN_PROCESS_LIVE)
    assert os.listdir(outside) == [] and (copy / "f.txt").read_text() == "same\n"


def test_unknown_state_allows_no_guest_staging(w):
    """Catches guest staging allowed in an unknown admission state (Revision
    8 item 4: an unknown string allows none): with an admission that would
    admit but reports a state outside NORMAL, OPTIMIZE, PRESSURE, CRITICAL,
    no ``archive_staging`` reservation is asked and the bundle is built on
    the target. Mutant: ``state != "CRITICAL"`` (the unknown state stages in
    the guest). Negative control: the same admission in PRESSURE stages in
    the guest with a reservation."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    admission = FakeAdmission("NOT_A_STATE", admit=True)
    _, _, report = plan_exec(w, item, target=target, admission=admission)
    record = by_path(report, item)
    assert record["status"] == vr.RELOCATED, record["reason"]
    assert record["staging"]["where"] == "target" and admission.reserved == []
    control = w.copy("control")
    admission = FakeAdmission("PRESSURE", admit=True)
    _, _, report = plan_exec(w, control, target=target, admission=admission)
    record = by_path(report, control)
    assert record["staging"]["where"] == "guest" and len(admission.reserved) == 1


# --------------------------------------------------------------------------- #
# K20 -- Revision 10 items 1-5: durable records, the staging walk
# --------------------------------------------------------------------------- #


def _relocation_scratch(world: World) -> Path:
    return world.registry.state_root / "projects" / PROJECT / "scratch" / vr.RELOCATION_SESSION


def _kept_without_label_leftover(world: World, item: Path, target: at.ArchiveTarget, probe,
                                 refused: FakeAdmission):
    """A first execute of `item` kept before anything is built (the target's
    identity is not confirmed: NO_TARGET) -- so the item path has a label
    (that receipt's id) and no leftover anywhere. Returns the relocation."""
    probe.identity = {"serial": "another disk"}
    rel = world.relocation(target=target, admission=refused)
    _, _, report = plan_exec(world, item, rel=rel)
    first = by_path(report, item)
    assert first["status"] == vr.KEPT and first["reason"].startswith(vr.NO_TARGET)
    assert _staged_names(target) == []
    probe.identity = {"serial": "S-" + target.target_id}
    return rel


def test_abandoned_target_build_followed_from_journal(w, monkeypatch):
    """Catches a build followed by its receipt's label only (sstew3-a M1,
    Revision 10 item 1): guest staging is refused; a `build_tree` wrapper
    finishes the bundle on ``<target>/staging/<p>/`` and then the process
    dies (a `BaseException`) -- no receipt, no intent. The build record
    journaled before the build names the label, so the next pass does not
    build on that target: ``KEPT`` (LEFTOVER_PRESENT), the staging directory
    holds the same names as before, the item's ``target_leftovers`` names
    the finished bundle, and `recover` returns no outcome for the build
    record. Mutant: labels taken from receipts only (the item is built a
    second time). Negative control: the dead pass left exactly one staged
    bundle, labelled with the build record's receipt id."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    refused = FakeAdmission("PRESSURE", admit=False)
    real = ab.ArchiveBundle.build_tree

    def dying(source, out_parent, **kw):
        real(source, out_parent, **kw)
        raise Crash("the process dies right after the build")

    monkeypatch.setattr(ab.ArchiveBundle, "build_tree", staticmethod(dying))
    rel = w.relocation(target=target, admission=refused)
    plan = rel.plan(PROJECT, [item])
    with pytest.raises(Crash):
        rel.execute(plan.path)
    monkeypatch.setattr(ab.ArchiveBundle, "build_tree", staticmethod(real))
    staged = _staged_names(target)
    [build] = [r for r in _intents(w) if r.get("build_started")]
    assert build["item"] == str(item) and build["target_id"] == target.target_id
    assert build["where"] == "target" and "quarantine_name" not in build
    assert "classification" not in build
    assert len(staged) == 1 and staged[0].endswith("-" + build["receipt_id"])
    assert not list(w.receipts.glob("reclaim-*.json"))
    _, _, report = plan_exec(w, item, rel=rel)
    second = by_path(report, item)
    assert second["status"] == vr.KEPT and vr.LEFTOVER_PRESENT in second["reason"]
    assert _staged_names(target) == staged and (item / "a.txt").read_text() == "alpha\n"
    assert [x["path"] for x in second["target_leftovers"]] == [
        str(target.root / "staging" / PROJECT / staged[0])]
    assert rel.recover(PROJECT) == []


def test_build_record_write_failure_builds_nothing(w, monkeypatch):
    """Catches a failed build record ignored (Revision 10 item 1): when the
    build record cannot be appended (ENOSPC), nothing is built -- neither
    under ``<target>/staging/<p>/`` (guest staging refused) nor in the guest
    staging (admitted, and its reservation released) -- and the item is
    ``KEPT`` (INTENT_FAILED), intact. Mutant: the failure ignored (a bundle
    is built and the item relocated). Negative control: with the journal
    writable the same item relocates."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    real = vr._append_intent

    def full(scratch, record):
        if record.get("build_started"):
            raise OSError(28, "No space left on device (simulated)")
        real(scratch, record)

    monkeypatch.setattr(vr, "_append_intent", full)
    for admission in (FakeAdmission("PRESSURE", admit=False),
                      FakeAdmission("PRESSURE", admit=True)):
        _, _, report = plan_exec(w, item, target=target, admission=admission)
        record = by_path(report, item)
        assert record["status"] == vr.KEPT, record["reason"]
        assert record["reason"].startswith(vr.INTENT_FAILED)
        assert record["staging"]["where"] == ("guest" if admission.admit else "target")
        assert _staged_names(target) == []
        guest = _relocation_scratch(w) / "staging"
        assert not guest.exists() or os.listdir(guest) == []
        assert (item / "a.txt").read_text() == "alpha\n"
        assert len(admission.released) == (1 if admission.admit else 0)
    assert not [r for r in _intents(w) if r.get("build_started")]
    monkeypatch.setattr(vr, "_append_intent", real)
    _, _, report = plan_exec(w, item, target=target)
    assert by_path(report, item)["status"] == vr.RELOCATED and not item.exists()


def test_receipt_write_is_atomic(w, monkeypatch):
    """Catches a receipt written in place under its final name (sstew3-b M1,
    Revision 10 item 2): `_write_all` writes half of the ReclaimReceipt and
    raises ENOSPC; no ``reclaim-<id>.json`` exists, the temporary
    ``.reclaim-<id>.json.tmp-<hex>`` stays with the half written, and
    `unreadable_receipts` lists it ``STRANDED_TEMP``. Mutant: written in
    place under the final name (a torn ``reclaim-<id>.json``). Negative
    control: the plan, written by the same writer, was published whole."""
    item = w.copy("dup")
    w.end_setup()
    real = vr._write_all
    halves: list[int] = []

    def half(fd, data):
        if b'"schema": "' + vr.RECEIPT_SCHEMA.encode() + b'"' in bytes(data):
            halves.append(len(data) // 2)
            os.write(fd, bytes(data)[: len(data) // 2])
            raise OSError(28, "No space left on device (simulated)")
        real(fd, data)

    monkeypatch.setattr(vr, "_write_all", half)
    rel, plan, report = plan_exec(w, item)
    record = by_path(report, item)
    assert halves and record["receipt_not_written"].startswith("OSError")
    assert not list(w.receipts.glob("reclaim-*.json"))
    [temp] = [n for n in os.listdir(w.receipts) if n.startswith(".reclaim-")]
    assert temp.startswith(f".reclaim-{record['receipt_id']}.json.tmp-")
    assert os.path.getsize(w.receipts / temp) == halves[0]
    assert [(e["status"], e["path"]) for e in rel.unreadable_receipts()] == [
        (vr.STRANDED_TEMP, str(w.receipts / temp))]
    assert json.loads(plan.path.read_text())["plan"]["items"][0]["path"] == str(item)


def test_receipt_write_failure_reported_and_recovered(w, monkeypatch):
    """Catches a failed ReclaimReceipt write that ends the execute or claims
    a completion (sstew3-b M1, Revision 10 item 2): after the removal the
    receipt write fails; `execute` is still ``DONE``, the item record is
    ``RELOCATED`` with ``receipt_not_written`` and NEEDS_YOU, the intent has
    no completion, and the next `recover` writes the receipt as
    ``RELOCATED``, ``recovered: true``. Mutants: the exception propagated;
    a completion appended without a receipt. Negative control: once
    recovered, no intent is open."""
    item = w.copy("dup")
    w.end_setup()
    real = vr.write_new

    def failing(path, data):
        if Path(path).name.startswith("reclaim-"):
            raise OSError(28, "No space left on device (simulated)")
        return real(path, data)

    monkeypatch.setattr(vr, "write_new", failing)
    rel, _, report = plan_exec(w, item)
    assert report.status == vr.DONE, report.detail
    record = by_path(report, item)
    assert record["status"] == vr.RELOCATED and not item.exists()
    assert "No space left" in record["receipt_not_written"] and record["needs_you"] is True
    assert "receipt_path" not in record
    [open_] = vr._open_intents(_relocation_scratch(w))
    assert open_["receipt_id"] == record["receipt_id"]
    monkeypatch.setattr(vr, "write_new", real)
    [outcome] = rel.recover(PROJECT)
    assert outcome["status"] == vr.RELOCATED and outcome["receipt_id"] == record["receipt_id"]
    written = json.loads(Path(outcome["receipt_path"]).read_text())
    assert written["status"] == vr.RELOCATED and written["recovered"] is True
    assert vr._open_intents(_relocation_scratch(w)) == []


def test_execute_failure_is_failed_not_busy(w):
    """Catches every exception that leaves `coordinate` read as BUSY (sstew3-b
    M1, Revision 10 item 2): an ``after_lock`` hook raises `RuntimeError`
    inside the work; the outcome is ``FAILED`` with the exception's type and
    message, never ``BUSY``. (A `BaseException` still propagates:
    `test_interrupted_removal_resolved_from_intent`.) Mutant: every exception
    from `coordinate` read as BUSY. Negative control: the flock and the
    session were released -- the same plan then executes ``DONE``."""
    item = w.copy("dup")
    w.end_setup()
    rel = w.relocation()
    plan = rel.plan(PROJECT, [item])

    @hook("after_lock")
    def explode(plan_path, project_id):
        raise RuntimeError("exploded inside the work (simulated)")

    report = rel.execute(plan.path)
    assert report.status == vr.FAILED and report.status != vr.BUSY
    assert "RuntimeError" in report.detail and "exploded inside the work" in report.detail
    assert item.is_dir()
    vr._hooks.clear()
    again = rel.execute(plan.path)
    assert again.status == vr.DONE and by_path(again, item)["status"] == vr.RELOCATED


def _failing_place_close(world: World, monkeypatch) -> None:
    """Closing a ``place-*`` session raises `RuntimeError` (the registry's
    `close_session` patched for those sessions only; `_execute_locked`'s
    ``finally`` catches only `StorageOwnershipError`)."""
    real = world.registry.close_session

    def closing(project_id, session_id, *args, **kwargs):
        if str(session_id).startswith("place-"):
            raise RuntimeError("closing the place session failed (simulated)")
        return real(project_id, session_id, *args, **kwargs)

    monkeypatch.setattr(world.registry, "close_session", closing)


def test_failed_execute_carries_finished_items(w, monkeypatch):
    """Catches a FAILED report built afresh, without the items finished
    before the exception (Revision 10 item 2): a plan of two duplicates;
    after both are relocated, closing the place session raises
    `RuntimeError`. The outcome is ``FAILED`` with both item records
    ``RELOCATED`` in ``report.items`` and their receipts written. Mutant:
    the FAILED report built afresh, without items. Negative control: both
    items are really gone."""
    one, two = w.copy("dup-1"), w.copy("dup-2")
    w.end_setup()
    _failing_place_close(w, monkeypatch)
    _, _, report = plan_exec(w, one, two)
    assert report.status == vr.FAILED and "closing the place session failed" in report.detail
    assert [by_path(report, p)["status"] for p in (one, two)] == [vr.RELOCATED] * 2
    for path in (one, two):
        receipt = Path(by_path(report, path)["receipt_path"])
        assert json.loads(receipt.read_text())["status"] == vr.RELOCATED
        assert not path.exists()


def test_unparsable_receipt_unresolved(w):
    """Catches a receipt under the intent's name parsed without a guard
    (sstew3-b M1, Revision 10 item 2): beside an open intent (the execute
    died after it) lies a torn ``reclaim-<id>.json``; two `recover` calls
    each return ``UNRESOLVED``, NEEDS_YOU, naming the file, without raising,
    writing or appending a completion. Mutant: the receipt parsed without a
    guard (`JSONDecodeError` on every pass). Negative control: with the torn
    file set aside (renamed, never removed) the intent resolves ``KEPT``
    ``INTERRUPTED``."""
    item = w.copy("dup")
    w.end_setup()
    rel = w.relocation()
    plan = rel.plan(PROJECT, [item])
    vr._hooks["after_intent"] = lambda **context: (_ for _ in ()).throw(Crash("dies"))
    with pytest.raises(Crash):
        rel.execute(plan.path)
    vr._hooks.clear()
    [intent] = vr._open_intents(_relocation_scratch(w))
    torn = w.receipts / f"reclaim-{intent['receipt_id']}.json"
    torn.write_bytes(b'{"schema": "veriharness.reclaim-receipt/1", "status": "REL')
    for _ in range(2):
        [outcome] = rel.recover(PROJECT)
        assert outcome["status"] == vr.UNRESOLVED and outcome["needs_you"] is True
        assert str(torn) in outcome["detail"]
        assert [i["receipt_id"] for i in vr._open_intents(_relocation_scratch(w))] == \
            [intent["receipt_id"]]
    assert torn.read_bytes().endswith(b'"REL')
    torn.rename(w.tmp / "torn-receipt-aside.json")
    [outcome] = rel.recover(PROJECT)
    assert outcome["status"] == vr.KEPT and outcome["detail"].startswith(vr.INTERRUPTED)


def test_torn_journal_line_terminated_before_append(w):
    """Catches a record glued to a torn last line (sstew3-b M1, Revision 10
    item 2): ``plans.jsonl`` ends in a partial line before `plan`, and
    ``intents.jsonl`` ends in one (written at seam ``after_place``) before
    the intent is appended; the plan still executes (not ``REFUSED
    PLAN_DIGEST``) and the intent of the execute that dies right after it is
    returned by `_open_intents`. Mutant: no termination (the plan line is
    unreadable and the plan refused; the intent line is lost). Negative
    control: the torn bytes stay in both files, each on its own line."""
    first = w.copy("first")
    w.end_setup()
    rel, _, report = plan_exec(w, first)
    assert by_path(report, first)["status"] == vr.RELOCATED
    index = w.receipts / vr.INDEX_NAME
    journal = _relocation_scratch(w) / vr.INTENTS_NAME
    with open(index, "ab") as handle:
        handle.write(b'{"plan": "plan-torn')
    item = w.copy("second")
    plan = rel.plan(PROJECT, [item])

    @hook("after_place")
    def tear(item, record):
        with open(journal, "ab") as handle:
            handle.write(b'{"schema": "veriharness.relocation-int')

    vr._hooks["after_intent"] = lambda **context: (_ for _ in ()).throw(Crash("dies"))
    with pytest.raises(Crash):
        rel.execute(plan.path)
    vr._hooks.clear()
    [intent] = vr._open_intents(_relocation_scratch(w))
    assert intent["item"] == str(item)
    assert b'{"plan": "plan-torn\n' in index.read_bytes()
    assert b'"veriharness.relocation-int\n' in journal.read_bytes()


def test_append_line_writes_whole_line(w, monkeypatch):
    """Catches a short `os.write` ignored by `_append_line` (sstew4spec-a M5,
    Revision 10 item 2): while `os.write` writes at most 7 bytes per call,
    one `_append_line` to ``plans.jsonl`` is read back whole. Mutant: one
    `os.write`, its return value ignored (7 bytes land). Negative control:
    the same record appended without the limit reads back the same."""
    index = w.receipts / vr.INDEX_NAME
    record = {"plan": "plan-x.json", "sha256": "0" * 64, "project_id": PROJECT,
              "pins": ["/a/b"], "created_utc": "2026-10-02T00:00:00Z"}
    real = os.write
    calls: list[int] = []

    def short(fd, data):
        calls.append(len(data))
        return real(fd, bytes(data)[:7])

    monkeypatch.setattr(os, "write", short)
    try:
        vr._append_line(index, record)
    finally:
        monkeypatch.setattr(os, "write", real)
    assert len(calls) > 1 and vr._read_lines(index) == [record]
    vr._append_line(index, record)
    assert vr._read_lines(index) == [record, record]


def _staged_failure(world: World, item: Path, target: at.ArchiveTarget, monkeypatch):
    """A first execute whose build on the target fails after its payload
    (guest staging refused): the ``.building`` directory it leaves carries
    the receipt's label. Returns the relocation and that directory."""
    refused = FakeAdmission("PRESSURE", admit=False)
    rel = world.relocation(target=target, admission=refused)
    _failing_payload(monkeypatch)
    _, _, report = plan_exec(world, item, rel=rel)
    monkeypatch.setattr(ab, "_after_payload_hook", None)
    first = by_path(report, item)
    assert first["reason"].startswith(vr.BUILD_FAILED)
    return rel, Path(first["staging_leftover"]["path"])


def test_symlinked_staging_directory_unmeasured(w, monkeypatch):
    """Catches a staging listing taken by path, following links (sstew4spec-a
    M2, Revision 10 item 3, 10a items 2 and 5). Guest staging is refused;
    the item has an earlier labelled ``.building`` leftover, moved outside
    every target:

    * case 1: ``<root>/staging`` is a symlink to an outside directory holding
      ``<p>/<the labelled name>``;
    * case 2: ``<root>/staging/<p>`` is a symlink to an outside directory
      holding the labelled name.

    For each, `target_leftovers` finds nothing (the outside names are never
    listed), names the target ``unmeasured``, sizes nothing, and the item is
    not built on that target (``KEPT`` LEFTOVER_PRESENT, nothing new outside
    or on the target). Mutant: the listing by path following links
    (``os.listdir(<root>/staging/<p>)``). Negative control: with the plain
    directory back, the leftover is found with its bytes."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    rel, leftover = _staged_failure(w, item, target, monkeypatch)
    sized: list = []
    real_bytes = vr._walk_bytes
    monkeypatch.setattr(vr, "_walk_bytes", lambda fd: sized.append(fd) or real_bytes(fd))
    staging = target.root / "staging"
    outside = w.tmp / "outside-staging"
    staging.rename(outside)
    staging.symlink_to(outside)
    outside_p = w.tmp / "outside-p"
    links = [staging]
    for case in (1, 2):
        if case == 2:
            staging.rename(w.tmp / "staging-link-aside")
            staging.mkdir()
            (outside / PROJECT).rename(outside_p)
            (staging / PROJECT).symlink_to(outside_p)
            links.append(staging / PROJECT)
        before = sorted(os.listdir(outside_p if case == 2 else outside / PROJECT))
        listing = rel.target_leftovers(PROJECT, _relocation_scratch(w))
        assert listing["found"] == [] and listing["unknown"] == [], (case, listing)
        assert [u["target_id"] for u in listing["unmeasured"]] == [target.target_id]
        # the outside names were never listed: none appears anywhere
        assert leftover.name not in json.dumps(listing), (case, listing)
        assert sized == []
        _, _, report = plan_exec(w, item, rel=rel)
        record = by_path(report, item)
        assert record["status"] == vr.KEPT and vr.LEFTOVER_PRESENT in record["reason"], case
        assert record["target_leftovers"] == []
        assert [u["target_id"] for u in record["target_leftovers_unmeasured"]] == [
            target.target_id]
        assert leftover.name not in json.dumps(record["target_leftovers_unmeasured"])
        assert sorted(os.listdir(outside_p if case == 2 else outside / PROJECT)) == before
        assert (item / "a.txt").read_text() == "alpha\n"
    (staging / PROJECT).rename(w.tmp / "p-link-aside")
    outside_p.rename(staging / PROJECT)
    listing = rel.target_leftovers(PROJECT, _relocation_scratch(w))
    assert [f["path"] for f in listing["found"]] == [str(staging / PROJECT / leftover.name)]
    assert listing["found"][0]["bytes"] == vr._tree_bytes(staging / PROJECT / leftover.name) > 0


def test_unlabelled_staging_name_reported(w, monkeypatch):
    """Catches unlabelled staging names not reported, or sized by following
    a link (9a, Revision 10 item 3): an unlabelled directory and an
    unlabelled symlink to an outside directory in ``<target>/staging/<p>/``
    are both reported ``UNKNOWN_STAGING_ENTRY`` with path only (no
    ``bytes``), nothing is sized, and neither blocks building: an item with
    a label (an earlier receipt kept before any build) is built on that
    target and relocated. Mutants: not reported; sized by following the
    link. Negative control: nothing is ``found`` or ``unmeasured``."""
    item = w.copy("dup")
    w.end_setup()
    target, probe = w.target()
    refused = FakeAdmission("PRESSURE", admit=False)
    rel = _kept_without_label_leftover(w, item, target, probe, refused)
    directory = target.root / "staging" / PROJECT
    directory.mkdir(parents=True)
    (directory / "notes").mkdir()
    (directory / "notes" / "n.txt").write_text("someone's notes\n")
    elsewhere = w.tmp / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "big.bin").write_bytes(b"\0" * 4096)
    (directory / "elsewhere-link").symlink_to(elsewhere)
    sized: list = []
    real_bytes = vr._walk_bytes
    monkeypatch.setattr(vr, "_walk_bytes", lambda fd: sized.append(fd) or real_bytes(fd))
    listing = rel.target_leftovers(PROJECT, _relocation_scratch(w))
    assert listing["found"] == [] and listing["unmeasured"] == []
    assert sorted(listing["unknown"], key=lambda e: e["path"]) == [
        {"target_id": target.target_id, "status": vr.UNKNOWN_STAGING_ENTRY,
         "path": str(directory / "elsewhere-link")},
        {"target_id": target.target_id, "status": vr.UNKNOWN_STAGING_ENTRY,
         "path": str(directory / "notes")}]
    assert sized == []
    _, _, report = plan_exec(w, item, rel=rel)
    record = by_path(report, item)
    assert record["status"] == vr.RELOCATED and record["staging"]["where"] == "target"
    assert record["target_leftovers"] == []


def test_recovery_receipt_read_never_follows_a_component(w):
    """Catches a recovery that resolves the bundle path and then opens the
    receipt by that path (sstew3-b M3, Revision 10 item 4): at seam
    ``before_receipt_open`` -- after containment is decided, before the first
    ``openat`` below the root -- an intermediate directory of the bundle path
    is renamed outside every target and a symlink to it takes its old name;
    the only receipt with the intent's digest now lies outside. The intent
    is ``UNRESOLVED`` and its detail names the refused component (ELOOP or
    ENOTDIR): it was not read. Mutant: the path resolved with ``realpath``
    before the seam and the receipt opened by it after (RELOCATED). Negative
    control: with the directory back in place the intent resolves
    RELOCATED."""
    steward, intent, _ = _interrupted_extract_root(w)
    rel = steward.relocation
    bundle = Path(intent["bundle_path"])
    middle = bundle.parent                      # <t9>/cold-store/<p>
    outside = w.tmp / "outside-middle"
    swapped: list = []

    @hook("before_receipt_open")
    def swap(intent, target, bundle):
        if swapped:
            return
        middle.rename(outside)
        middle.symlink_to(outside)
        swapped.append(True)

    [outcome] = rel.recover(PROJECT)
    vr._hooks.clear()
    assert swapped and outcome["status"] == vr.UNRESOLVED, outcome
    assert ("ELOOP" in outcome["detail"] or "ENOTDIR" in outcome["detail"]), outcome
    assert repr(middle.name) in outcome["detail"]
    assert st.sha256_file(outside / bundle.name / ab.RECEIPT) == intent["receipt_digest"]
    assert not (w.receipts / f"reclaim-{intent['receipt_id']}.json").exists()
    middle.rename(w.tmp / "middle-link-aside")
    outside.rename(middle)
    [outcome] = rel.recover(PROJECT)
    assert outcome["status"] == vr.RELOCATED


def _extract_intents(world: World, root: Path) -> list[dict]:
    return [r for r in _intents(world) if r.get("item") == str(root) and "classification" in r]


def test_extract_root_kept_without_one_containing_target(w, monkeypatch):
    """Catches an extract-root intent written without one containing target
    (sstew3-b M4, Revision 10 item 5). Each case is ``KEPT`` with its reason,
    the item still at its path, and no intent for it in ``intents.jsonl``:

    * the containing target's root cannot be measured in time (its opening
      blocks past the 1 s timeout at that point): TARGET_UNMEASURABLE;
    * the bundle lies under no registered target: NO_CONTAINING_TARGET;
    * it lies under two (one root inside the other): NO_CONTAINING_TARGET.

    Mutant: the intent written with ``containing_target_id: None``.
    Negative control: with ``t9`` alone registered the root relocates."""
    root, _ = _extract_world(w)
    t9, _ = w.target("t9")
    t1, _ = w.target("t1")
    gate = {"armed": False, "event": threading.Event()}
    real_open = vr._open_target_root

    def blocking(path):
        if gate["armed"] and Path(path) == t9.root:
            gate["event"].wait(30)
        return real_open(path)

    monkeypatch.setattr(vr, "_open_target_root", blocking)
    nested = _target_at(w, "tin", t9.root / "cold-store")
    cases = [("timeout", [t9], vr.TARGET_UNMEASURABLE),
             ("none", [t1], vr.NO_CONTAINING_TARGET),
             ("two", [t9, nested], vr.NO_CONTAINING_TARGET)]
    try:
        for case, targets, reason in cases:
            rel = _relocation_with(w, *targets)
            rel.timeout = 1.0
            plan = rel.plan(PROJECT, [root])
            assert [i["path"] for i in plan.items] == [str(root)], case

            @hook("after_place")
            def arm(item, record, case=case):
                gate["armed"] = case == "timeout"

            report = rel.execute(plan.path)
            vr._hooks.clear()
            gate["armed"] = False
            record = by_path(report, root)
            assert record["status"] == vr.KEPT and record["reason"].startswith(reason), \
                (case, record["reason"])
            assert (root / "tree").is_dir() and _extract_intents(w, root) == [], case
    finally:
        gate["event"].set()
    _, _, report = plan_exec(w, root, rel=_relocation_with(w, t9))
    assert by_path(report, root)["status"] == vr.RELOCATED and not root.exists()


def test_containing_target_is_the_one_holding_the_bundle(w):
    """Catches a containing target that is just the first registered one
    (sstew3-a M3, Revision 10 item 5): with two targets registered, ``t1``
    first and ``t9`` second, and the bundle under ``t9``, the extract root's
    intent names ``t9`` as ``containing_target_id`` (its ``target_id`` stays
    None) and the root relocates. Mutant: the first registered target
    returned. Negative control: ``t1`` is a valid target that holds
    nothing of it."""
    root, placed = _extract_world(w)
    t1, _ = w.target("t1")
    t9, _ = w.target("t9")
    _, _, report = plan_exec(w, root, rel=_relocation_with(w, t1, t9))
    assert by_path(report, root)["status"] == vr.RELOCATED
    [intent] = _extract_intents(w, root)
    assert intent["containing_target_id"] == "t9" and intent["target_id"] is None
    assert not str(placed.path).startswith(str(t1.root) + "/")


def test_extract_root_payload_rehashed_on_target(w, monkeypatch):
    """Catches an extract root released on a payload read through links, an
    unconfirmed target, or a timed-out re-hash (sstew4spec-a M1, Revision 10
    item 5, 10a item 6). At seam ``after_place`` (extract roots reach it
    before their intent):

    * case 1: the bundle directory on the target is renamed outside every
      target and a symlink to that byte-identical copy takes its name:
      ``KEPT`` EXTRACT_PROOF_CHANGED;
    * case 2: the target's identity probe answers another identity:
      ``KEPT`` TARGET_UNCONFIRMED -- and the probe was given
      ``/proc/self/fd/<n>``, the root descriptor of the walk, never the root
      path;
    * case 3: the re-hash blocks past the 1 s timeout: ``KEPT``
      TARGET_TIMEOUT.

    Each: no intent, the item still at its path. Mutants: the payload
    re-hashed by path following links; the identity check dropped; the
    identity probed by the root's path; a timeout read as a match. Negative
    control: untouched, the root relocates."""
    root, placed = _extract_world(w)
    t9, probe = w.target("t9")
    seen: list = []
    real_probe = t9.identity_probe

    def recording(path):
        seen.append(os.fspath(path))
        return real_probe(path)

    t9.identity_probe = recording
    rel = _relocation_with(w, t9)
    rel.timeout = 1.0
    bundle = Path(placed.path)
    block = {"armed": False, "event": threading.Event()}
    real_hash = vr._hash_fd

    def slow_hash(fd):
        if block["armed"]:
            block["event"].wait(30)
        return real_hash(fd)

    monkeypatch.setattr(vr, "_hash_fd", slow_hash)

    def run(case, change):
        plan = rel.plan(PROJECT, [root])
        assert [i["path"] for i in plan.items] == [str(root)], case
        vr._hooks["after_place"] = lambda item, record: change()
        seen.clear()
        report = rel.execute(plan.path)
        vr._hooks.clear()
        return by_path(report, root)

    def linked():
        bundle.rename(w.tmp / "outside-bundle")
        bundle.symlink_to(w.tmp / "outside-bundle")

    def other_disk():
        probe.identity = {"serial": "another disk"}

    def slow():
        block["armed"] = True

    try:
        for case, change, reason in ((1, linked, vr.EXTRACT_PROOF_CHANGED),
                                     (2, other_disk, vr.TARGET_UNCONFIRMED),
                                     (3, slow, vr.TARGET_TIMEOUT)):
            record = run(case, change)
            assert record["status"] == vr.KEPT and record["reason"].startswith(reason), \
                (case, record["reason"])
            assert (root / "tree").is_dir() and _extract_intents(w, root) == [], case
            if case == 1:
                bundle.rename(w.tmp / "bundle-link-aside")
                (w.tmp / "outside-bundle").rename(bundle)
            if case == 2:
                assert seen and all(p.startswith("/proc/self/fd/") for p in seen), seen
                probe.identity = {"serial": "S-t9"}
            if case == 3:
                block["event"].set()
                block["armed"] = False
    finally:
        block["event"].set()
    record = run("control", lambda: None)
    assert record["status"] == vr.RELOCATED and not root.exists()
    assert all(p.startswith("/proc/self/fd/") for p in seen)


# --------------------------------------------------------------------------- #
# K21 -- Revision 10 item 7: rules that had no killing test
# --------------------------------------------------------------------------- #


def test_relocated_receipt_label_still_followed(w):
    """Catches ``RELOCATED`` receipts skipped when the labels are collected
    (sstew3-a M3, Revision 10 item 7): what a pass of ``8c8c25e4`` left -- a
    ``RELOCATED`` ReclaimReceipt of the item path (no build record carries
    its label) and its bundle ``vh-arch-...-<that id>`` under
    ``<target>/staging/<p>/`` -- keeps the item, an eligible duplicate back
    at its path, from being built on that target (guest staging refused):
    ``KEPT`` LEFTOVER_PRESENT, its ``target_leftovers`` names the directory,
    the staging directory unchanged. Mutant: RELOCATED receipts skipped.
    Negative control: no build record names the label."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    rid = "rr-20261002t100000z-0badc0de"
    vr.write_new(w.receipts / f"reclaim-{rid}.json", json.dumps({
        "schema": vr.RECEIPT_SCHEMA, "receipt_id": rid, "project_id": PROJECT,
        "path": str(item), "status": vr.RELOCATED, "target_id": target.target_id,
        "retained_originals": []}).encode())
    staged = target.root / "staging" / PROJECT / f"vh-arch-20261002T100000Z-abc-{rid}"
    staged.mkdir(parents=True)
    (staged / ab.MANIFEST).write_text("{}\n")
    before = _staged_names(target)
    _, _, report = plan_exec(w, item, target=target,
                             admission=FakeAdmission("PRESSURE", admit=False))
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and vr.LEFTOVER_PRESENT in record["reason"]
    assert [x["path"] for x in record["target_leftovers"]] == [str(staged)]
    assert _staged_names(target) == before and item.is_dir()
    assert not [r for r in _intents(w) if r.get("build_started")
                and r.get("receipt_id") == rid]


def test_unmeasurable_staging_listing_blocks_target(w):
    """Catches an unmeasurable listing that does not block (sstew3-a M3,
    Revision 10 item 7): guest staging refused, the item has an earlier
    receipt (kept before any build, so no leftover exists), and the
    ``openat`` of ``<target>/staging/<p>`` fails with EACCES (that directory
    alone is mode 000). The item is not built on that target -- ``KEPT``
    LEFTOVER_PRESENT, no new name there -- and its
    ``target_leftovers_unmeasured`` names the target. Mutant: an
    unmeasurable listing that does not block. Negative control: with the
    mode back the same item is built there and relocated."""
    item = w.copy("dup")
    w.end_setup()
    target, probe = w.target()
    refused = FakeAdmission("PRESSURE", admit=False)
    rel = _kept_without_label_leftover(w, item, target, probe, refused)
    directory = target.root / "staging" / PROJECT
    directory.mkdir(parents=True)
    directory.chmod(0o000)
    try:
        _, _, report = plan_exec(w, item, rel=rel)
    finally:
        directory.chmod(0o755)
    record = by_path(report, item)
    assert record["status"] == vr.KEPT and vr.LEFTOVER_PRESENT in record["reason"]
    assert [u["target_id"] for u in record["target_leftovers_unmeasured"]] == [
        target.target_id]
    assert "Permission denied" in record["target_leftovers_unmeasured"][0]["detail"]
    assert os.listdir(directory) == [] and item.is_dir()
    _, _, report = plan_exec(w, item, rel=rel)
    assert by_path(report, item)["status"] == vr.RELOCATED


def test_recovery_uses_containing_target_field(w):
    """Catches ``containing_target_id`` ignored in favour of the "exactly one
    target" rule (sstew3-a M3, Revision 10 item 7). Beside the interrupted
    extract-root removal (whose own intent resolves RELOCATED):

    * case 1: an intent whose ``containing_target_id`` names ``t1``, a
      registered target the bundle is not under -- the bundle lies under
      exactly one other target (``t9``) with the matching receipt;
    * case 2: an intent whose field is present and None, as ``8c8c25e4``
      wrote it.

    Both are ``UNRESOLVED``. Mutant: the field ignored and the "exactly one"
    rule applied (both RELOCATED). Negative control: the original intent,
    naming ``t9``, resolves RELOCATED."""
    steward, intent, _ = _interrupted_extract_root(w)
    t1, _ = w.target("t1")
    [t9] = steward.relocation.targets
    rel = _relocation_with(w, t1, t9)
    base = {k: v for k, v in intent.items()
            if k not in ("receipt_id", "item", "quarantine_name", "containing_target_id")}
    ids = {}
    for n, (case, value) in enumerate((("wrong-target", "t1"), ("none", None)), start=1):
        rid = f"rr-20261002t1300{n:02d}z-{n:08x}"
        gone = w.scratch / f"gone-{case}"
        vr._append_intent(steward.relocation_scratch, dict(
            base, receipt_id=rid, item=str(gone), containing_target_id=value,
            quarantine_name=str(gone) + vr.QUARANTINE_MARK + "x"))
        ids[case] = rid
    outcomes = {o["receipt_id"]: o for o in rel.recover(PROJECT)}
    assert outcomes[ids["wrong-target"]]["status"] == vr.UNRESOLVED
    assert outcomes[ids["none"]]["status"] == vr.UNRESOLVED
    assert outcomes[intent["receipt_id"]]["status"] == vr.RELOCATED


def test_unlistable_remainder_keeps_quarantine(w, monkeypatch):
    """Catches an unlistable directory passed over by `_remainder_problem`
    (sstew3-a M3, Revision 10 item 7): over a quarantine whose one remaining
    walked directory ``d`` cannot be listed (`os.scandir` raises for it
    alone), it returns a problem -- so the quarantine name stays. Mutant: the
    unlistable directory passed over (""). Negative control: listable, the
    same remainder (only a new file in ``d``) is no problem."""
    quarantine = w.tmp / ("item" + vr.QUARANTINE_MARK + "20261002T000000Z")
    (quarantine / "d").mkdir(parents=True)
    (quarantine / "d" / "new.txt").write_text("appeared after the inventory\n")
    walked = (st.Entry(rel="d", type="dir", mode=0o755, size=0),
              st.Entry(rel="d/old.txt", type="file", mode=0o644, size=4, sha256="0" * 64))
    assert vr._remainder_problem(quarantine, walked) == ""
    real = os.scandir

    def failing(path="."):
        if os.fspath(path) == os.path.join(quarantine, "d"):
            raise PermissionError(13, "Permission denied (simulated)", os.fspath(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", failing)
    problem = vr._remainder_problem(quarantine, walked)
    assert problem and "cannot be listed" in problem


def test_item_ancestry_unreadable_refused(w, monkeypatch):
    """Catches an unreadable item ancestry read as no overlap (sstew3-a M3,
    Revision 10 item 7): when `_ancestry` raises for the item's own path,
    `plan` rejects the item ``TARGET_UNMEASURABLE``. Mutant: an unreadable
    item ancestry read as no overlap (the item is planned). Negative
    control: with `_ancestry` working the same item is planned."""
    item = w.copy("dup")
    w.end_setup()
    rel = w.relocation()
    assert [i["path"] for i in rel.plan(PROJECT, [item]).items] == [str(item)]
    real = vr._ancestry

    def unreadable(path, **kw):
        if os.fspath(path) == str(item):
            raise PermissionError(13, "Permission denied (simulated)", os.fspath(path))
        return real(path, **kw)

    monkeypatch.setattr(vr, "_ancestry", unreadable)
    plan = rel.plan(PROJECT, [item])
    assert not plan.items
    assert plan.rejected[0]["reason"].startswith(vr.TARGET_UNMEASURABLE)


# --------------------------------------------------------------------------- #
# Revision 11 item 1 -- BUSY only for a proven live other holder
# --------------------------------------------------------------------------- #


def _relocation_scratch(world: World) -> Path:
    return world.registry.state_root / "projects" / PROJECT / "scratch" / vr.RELOCATION_SESSION


def _coordinator_dir(world: World) -> Path:
    return world.registry.state_root / "coordinator" / PROJECT


def _normal_forecast_pass(world: World, rel: vr.VerifiedRelocation, *,
                          forecast: bool = True) -> dict:
    """One steward pass in NORMAL whose free space is ample (no shortfall,
    NORMAL after the pass): with `forecast` the forecast crosses the reserve,
    so the pass plans and executes; without it the pass only observes. Only
    a failure can make such a pass NEEDS_YOU."""
    import hoh.storage_steward as ss
    admission = FakeAdmission("NORMAL", free=100 * GiB,
                              seconds_to_reserve=60.0 if forecast else None)
    steward = ss.StorageSteward(PROJECT, world.registry, admission, rel,
                                ss.StewardPolicy(target_timeout_s=60.0))
    return steward.run_once()


def _enospc(*args, **kwargs):
    raise OSError(errno.ENOSPC, "No space left on device (simulated)")


def test_flock_other_errno_failed(w, monkeypatch):
    """Catches every flock error read as BUSY (Revision 11 item 1): the
    non-blocking ``flock`` on ``relocation.lock`` failing with ``ENOLCK``
    makes `execute` ``FAILED`` (with the error text) and `recover` one
    ``FAILED`` entry with NEEDS_YOU; the item is untouched. Mutant: any
    flock ``OSError`` is BUSY. Negative controls, changing only the errno:
    ``EWOULDBLOCK`` (another holder) is ``BUSY`` in both doors; without the
    failure the same plan executes ``DONE``."""
    item = w.copy("dup")
    w.end_setup()
    rel = w.relocation()
    plan = rel.plan(PROJECT, [item])
    real_flock = fcntl.flock
    answer = {"errno": errno.ENOLCK}

    def flock(fd, operation):
        if operation & fcntl.LOCK_NB and \
                os.readlink(f"/proc/self/fd/{fd}").endswith("/" + vr.LOCK_NAME):
            raise OSError(answer["errno"], os.strerror(answer["errno"]) + " (simulated)")
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", flock)
    report = rel.execute(plan.path)
    assert report.status == vr.FAILED, report.detail
    assert os.strerror(errno.ENOLCK) in report.detail and not report.items
    [entry] = rel.recover(PROJECT)
    assert entry["status"] == vr.FAILED and entry["needs_you"] is True
    assert item.is_dir() and retain_refs(w) == []
    answer["errno"] = errno.EWOULDBLOCK
    assert rel.execute(plan.path).status == vr.BUSY
    assert [e["status"] for e in rel.recover(PROJECT)] == [vr.BUSY]
    monkeypatch.setattr(fcntl, "flock", real_flock)
    done = rel.execute(plan.path)
    assert done.status == vr.DONE and by_path(done, item)["status"] == vr.RELOCATED


def test_lock_path_error_failed(w, monkeypatch):
    """Catches an error of `registry.scratch_dir` or of opening
    ``relocation.lock`` propagated out of `execute` or `recover` (Revision 11
    item 1): (1) `scratch_dir` raises for the relocation session; (2)
    ``relocation.lock`` is a dangling symlink (``O_NOFOLLOW`` refuses it,
    ELOOP). Each makes `execute` ``FAILED`` with the error and `recover` one
    ``FAILED`` entry with NEEDS_YOU, never raised, never BUSY; the item is
    untouched and the session is released. Mutant: the error propagated.
    Negative control: with the lock file back, the same plan executes
    ``DONE``."""
    item = w.copy("dup")
    w.end_setup()
    rel = w.relocation()
    plan = rel.plan(PROJECT, [item])
    real_scratch = w.registry.scratch_dir

    def failing(project_id, session_id, *args, **kwargs):
        if session_id == vr.RELOCATION_SESSION:
            raise OSError(errno.EACCES, "Permission denied (simulated)")
        return real_scratch(project_id, session_id, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(w.registry, "scratch_dir", failing)
        report = rel.execute(plan.path)
        assert report.status == vr.FAILED and "Permission denied" in report.detail
        [entry] = rel.recover(PROJECT)
        assert entry["status"] == vr.FAILED and entry["needs_you"] is True
    assert rel.recover(PROJECT) == []            # creates the scratch and the lock file
    lock = _relocation_scratch(w) / vr.LOCK_NAME
    aside = w.tmp / "aside"
    aside.mkdir()
    lock.rename(aside / "relocation.lock")
    lock.symlink_to(aside / "elsewhere.lock")
    report = rel.execute(plan.path)
    assert report.status == vr.FAILED and vr.LOCK_NAME in report.detail, report.detail
    [entry] = rel.recover(PROJECT)
    assert entry["status"] == vr.FAILED and entry["needs_you"] is True
    assert not (aside / "elsewhere.lock").exists() and item.is_dir()
    lock.rename(aside / "the-symlink")
    (aside / "relocation.lock").rename(lock)
    done = rel.execute(plan.path)
    assert done.status == vr.DONE and by_path(done, item)["status"] == vr.RELOCATED


def test_enospc_before_work_is_failed_not_busy(w, monkeypatch):
    """Catches an ENOSPC before the work read as BUSY (sstew4b-a B1,
    Revision 11 item 1): ENOSPC (a) writing ``DRIVER.json`` (the coordinator
    wraps it, reason ``OS_ERROR``), (b) writing the relocation session's
    ``LEASE.json`` (the registry's raw ``OSError``) and (c) opening the
    coordinator's ``.lock`` (wrapped) each make `execute` ``FAILED`` with the
    real error text, never ``BUSY``; nothing is done. Mutants: any pre-work
    exception is BUSY; the session door still BUSY (b); a wrapped
    ``OSError`` still BUSY (a, c). Negative control: without the ENOSPC the
    same plan executes ``DONE``."""
    item = w.copy("dup")
    w.end_setup()
    rel = w.relocation()
    plan = rel.plan(PROJECT, [item])
    real_atomic = sr._atomic_write_json
    real_open = os.open

    def lease_full(path, *args, **kwargs):
        if Path(path).name == "LEASE.json" and Path(path).parent.name == vr.RELOCATION_SESSION:
            _enospc()
        return real_atomic(path, *args, **kwargs)

    def lock_full(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and \
                os.fspath(path).endswith(f"/coordinator/{PROJECT}/.lock"):
            _enospc()
        return real_open(path, *args, **kwargs)

    cases = (("DRIVER.json", sc, "_write_whole", lambda fd, data: _enospc()),
             ("LEASE.json", sr, "_atomic_write_json", lease_full),
             ("coordinator .lock", os, "open", lock_full))
    for case, owner, name, replacement in cases:
        with monkeypatch.context() as patch:
            patch.setattr(owner, name, replacement)
            report = rel.execute(plan.path)
        assert report.status == vr.FAILED and report.status != vr.BUSY, (case, report.detail)
        assert "No space left on device" in report.detail, (case, report.detail)
        assert not report.items and item.is_dir() and retain_refs(w) == [], case
    assert not os.path.lexists(_coordinator_dir(w) / "DRIVER.json")
    done = rel.execute(plan.path)
    assert done.status == vr.DONE and by_path(done, item)["status"] == vr.RELOCATED


def test_busy_only_for_live_holder(w):
    """Catches BUSY lost for a real live other holder (Revision 11 item 1):
    (1) another live session holds the project's driver lease (the
    coordinator's refusal carries ``LIVE_HOLDER``); (2) another holder has
    ``relocation.lock`` flocked (``EWOULDBLOCK``); (3) another live process
    holds the fixed ``relocation`` session (the registry's refusal carries
    ``LIVE_HOLDER``). Each is ``BUSY`` and nothing is done. Mutant: none of
    them BUSY. Negative control: once every holder has gone, the same plan
    executes ``DONE``."""
    item = w.copy("dup")
    w.end_setup()
    rel = w.relocation()
    plan = rel.plan(PROJECT, [item])
    assert rel.recover(PROJECT) == []            # the relocation scratch and its lock

    driver = StorageCoordinator(w.registry, None, [], getpid=lambda: OTHER_PID)
    lease = driver.acquire_session(PROJECT, "driver")
    driver.coordinate(PROJECT, "driver", lambda: None)
    report = rel.execute(plan.path)
    assert report.status == vr.BUSY and "driver lease" in report.detail, report.detail
    driver.close_session(lease)

    fd = os.open(_relocation_scratch(w) / vr.LOCK_NAME, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        report = rel.execute(plan.path)
    finally:
        os.close(fd)
    assert report.status == vr.BUSY and vr.LOCK_NAME in report.detail, report.detail

    session = StorageCoordinator(w.registry, None, [], getpid=lambda: OTHER_PID)
    held = session.acquire_session(PROJECT, vr.RELOCATION_SESSION)
    report = rel.execute(plan.path)
    assert report.status == vr.BUSY and "relocation session" in report.detail, report.detail
    session.close_session(held)

    assert item.is_dir() and retain_refs(w) == []
    done = rel.execute(plan.path)
    assert done.status == vr.DONE and by_path(done, item)["status"] == vr.RELOCATED


def test_malformed_driver_record_failed(w):
    """Catches a malformed ``DRIVER.json`` read as BUSY (Revision 11 item 1,
    s4cspec q3): after one execute, the project's ``DRIVER.json`` is
    replaced (the kept record renamed aside) by unparsable bytes; `execute`
    is ``FAILED`` naming the malformed record, and nothing is done. Mutant:
    malformed is BUSY. Negative control: with the kept record back, the same
    plan executes ``DONE``."""
    first, item = w.copy("first"), w.copy("dup")
    w.end_setup()
    rel = w.relocation()
    assert plan_exec(w, first, rel=rel)[2].status == vr.DONE
    plan = rel.plan(PROJECT, [item])
    driver = _coordinator_dir(w) / "DRIVER.json"
    aside = w.tmp / "aside"
    aside.mkdir()
    driver.rename(aside / "DRIVER.json")
    driver.write_text("{ not json", encoding="utf-8")
    report = rel.execute(plan.path)
    assert report.status == vr.FAILED and "malformed" in report.detail, report.detail
    assert not report.items and item.is_dir()
    driver.rename(aside / "malformed")
    (aside / "DRIVER.json").rename(driver)
    done = rel.execute(plan.path)
    assert done.status == vr.DONE and by_path(done, item)["status"] == vr.RELOCATED


def test_non_live_refusal_is_failed(w):
    """Catches every refusal that is not a live other holder read as BUSY
    (Revision 11 item 1). In a NORMAL pass whose forecast crosses the
    reserve (so only a failure can make it NEEDS_YOU):

    * a foreign coordinator marker (it names another project);
    * a driver holder of unknown liveness (its pid exists, its start time is
      unreadable);
    * an unreadable ``LEASE.json`` of the relocation session (it cannot be
      proven dead) -- also the recovery's door;

    each gives an execute ``FAILED`` and a report with NEEDS_YOU, the item
    untouched. Mutant: every unwrapped refusal is BUSY. Negative control:
    with each cause undone, the same pass executes ``DONE``."""
    first, item = w.copy("first"), w.copy("dup")
    w.end_setup()
    rel = w.relocation()
    assert plan_exec(w, first, rel=rel)[2].status == vr.DONE
    aside = w.tmp / "aside"
    aside.mkdir()

    def foreign_marker():
        marker = _coordinator_dir(w) / ".vh-owner.json"
        record = json.loads(marker.read_text())
        marker.rename(aside / "marker")
        marker.write_text(json.dumps({**record, "project_id": OTHER}), encoding="utf-8")

        def undo():
            marker.rename(aside / "foreign-marker")
            (aside / "marker").rename(marker)
        return undo

    def unknown_liveness():
        other = StorageCoordinator(w.registry, None, [], getpid=lambda: OTHER_PID)
        lease = other.acquire_session(PROJECT, "driver")
        other.coordinate(PROJECT, "driver", lambda: None)
        start = w.fake.procs[OTHER_PID]
        w.fake.procs[OTHER_PID] = None          # the pid exists, its start time is unreadable

        def undo():
            w.fake.procs[OTHER_PID] = start
            other.close_session(lease)
        return undo

    def unreadable_lease():
        path = w.registry._lease_path(PROJECT, vr.RELOCATION_SESSION)
        assert not os.path.lexists(path)
        path.write_text("{ not json", encoding="utf-8")
        return lambda: path.rename(aside / "unreadable-lease")

    for cause in (foreign_marker, unknown_liveness, unreadable_lease):
        undo = cause()
        report = _normal_forecast_pass(w, rel)
        undo()
        assert report["state"] == "NORMAL" and report["forecast_crossing"] is True
        assert report["execute"]["status"] == vr.FAILED, (cause.__name__, report["execute"])
        assert report["needs_you"] is True and item.is_dir(), cause.__name__
        if cause is unreadable_lease:
            assert [r["status"] for r in report["recovered"]] == [vr.FAILED]
    report = _normal_forecast_pass(w, rel)
    assert report["execute"]["status"] == vr.DONE and not item.exists()


def test_recover_door_classified(w, monkeypatch):
    """Catches the recovery's door still BUSY for a refusal that is not a
    live other holder (s4cspec-a q4, Revision 11 item 1): ENOSPC writing the
    relocation session's ``LEASE.json`` during `recover`, in a NORMAL pass
    that only observes, gives one ``FAILED`` entry with NEEDS_YOU, and the
    report is NEEDS_YOU. Mutant: the recover door still BUSY (BUSY is not
    NEEDS_YOU). Negative control, changing only the ENOSPC: the same NORMAL
    pass recovers nothing and is not NEEDS_YOU."""
    w.end_setup()
    rel = w.relocation()
    real_atomic = sr._atomic_write_json

    def lease_full(path, *args, **kwargs):
        if Path(path).name == "LEASE.json" and Path(path).parent.name == vr.RELOCATION_SESSION:
            _enospc()
        return real_atomic(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sr, "_atomic_write_json", lease_full)
        [entry] = rel.recover(PROJECT)
        assert entry["status"] == vr.FAILED and entry["needs_you"] is True
        assert "No space left on device" in entry["detail"]
        report = _normal_forecast_pass(w, rel, forecast=False)
    assert report["state"] == "NORMAL" and report["action"] == "OBSERVED"
    assert [r["status"] for r in report["recovered"]] == [vr.FAILED]
    assert report["needs_you"] is True
    calm = _normal_forecast_pass(w, rel, forecast=False)
    assert calm["recovered"] == [] and calm["needs_you"] is False


# --------------------------------------------------------------------------- #
# Revision 11 items 4 and 5 -- recovery past one failure; the extract gate's order
# --------------------------------------------------------------------------- #


def test_recover_continues_after_write_failure(w, monkeypatch):
    """Catches the first failed write ending the recovery (sstew4b-a M4,
    Revision 11 item 4): two executes die after their intents (two open
    intents); while the first intent's receipt is written, `write_new` fails
    (ENOSPC). That intent is ``RECOVERY_FAILED`` with NEEDS_YOU and stays
    open; the second is still judged (``KEPT`` INTERRUPTED, its receipt and
    completion written); `recover` does not raise. Mutant: the first failure
    ends the recovery. Negative control: the next recovery, with the writer
    back, resolves the first intent."""
    one, two = w.copy("one"), w.copy("two")
    w.end_setup()
    rel = w.relocation()
    for item in (one, two):
        plan = rel.plan(PROJECT, [item])
        vr._hooks["after_intent"] = lambda **context: (_ for _ in ()).throw(Crash("dies"))
        with pytest.raises(Crash):
            rel.execute(plan.path)
        vr._hooks.clear()
    scratch = _relocation_scratch(w)
    first, second = vr._open_intents(scratch)
    assert (first["item"], second["item"]) == (str(one), str(two))
    real_write = vr.write_new
    calls: list = []

    def failing_once(path, data):
        calls.append(path)
        if len(calls) == 1:
            _enospc()
        return real_write(path, data)

    monkeypatch.setattr(vr, "write_new", failing_once)
    failed, judged = rel.recover(PROJECT)
    assert failed["status"] == vr.RECOVERY_FAILED and failed["needs_you"] is True
    assert failed["receipt_id"] == first["receipt_id"]
    assert "No space left on device" in failed["detail"]
    assert judged["status"] == vr.KEPT and judged["detail"].startswith(vr.INTERRUPTED)
    assert judged["receipt_id"] == second["receipt_id"]
    assert [i["receipt_id"] for i in vr._open_intents(scratch)] == [first["receipt_id"]]
    assert not (w.receipts / f"reclaim-{first['receipt_id']}.json").exists()
    monkeypatch.setattr(vr, "write_new", real_write)
    [again] = rel.recover(PROJECT)
    assert again["status"] == vr.KEPT and again["receipt_id"] == first["receipt_id"]
    assert vr._open_intents(scratch) == [] and one.is_dir() and two.is_dir()


def test_target_problems_refuse_before_extract_gate(w, monkeypatch):
    """Catches the early check dropped (sstew4b-b M3, Revision 11 item 5):
    an extract root is planned; then ``archive-targets.json`` has a problem
    (`target_problems`) when `execute` runs. The item is ``REFUSED``
    (TARGET_UNMEASURABLE: archive-targets.json) before `_extract_gate` runs
    -- the gate is never called, no target root is opened by the staging
    walk and no identity probe is read -- and no intent is written. Mutant:
    the early check dropped (the gate reads the target before the late check
    refuses). Negative control: without the problem the gate runs and the
    root relocates."""
    root, _ = _extract_world(w)
    t9, probe = w.target("t9")
    rel = _relocation_with(w, t9)
    plan = rel.plan(PROJECT, [root])
    assert [i["path"] for i in plan.items] == [str(root)]
    gate: list = []
    opened: list = []
    real_gate = rel._extract_gate
    real_open = vr._open_target_root
    monkeypatch.setattr(rel, "_extract_gate",
                        lambda proof: (gate.append(proof), real_gate(proof))[1])
    monkeypatch.setattr(vr, "_open_target_root",
                        lambda r: (opened.append(os.fspath(r)), real_open(r))[1])
    probed = probe.calls
    rel.target_problems = ["entry 1 is not a mapping but list (simulated)"]
    record = by_path(rel.execute(plan.path), root)
    assert record["status"] == vr.REFUSED, record["reason"]
    assert record["reason"].startswith(vr.TARGET_UNMEASURABLE + ": archive-targets.json")
    assert gate == [] and opened == [] and probe.calls == probed
    assert (root / "tree").is_dir() and _extract_intents(w, root) == []
    rel.target_problems = []
    record = by_path(rel.execute(plan.path), root)
    assert record["status"] == vr.RELOCATED and gate and opened


def test_overlap_appearing_after_intent_refused(w):
    """Catches the late overlap check dropped (Revision 11 item 5): an
    extract root passes the early check (the second target's root does not
    exist yet), its intent is written, and at seam ``after_intent`` that
    root appears as a link to the scratch directory holding the item. The
    item is ``REFUSED`` (INSIDE_ARCHIVE_TARGET) before the quarantine
    rename: never quarantined, still whole. Mutant: the late check dropped
    (the item is quarantined and removed). Negative control: the intent was
    written, so the early check had let the item through."""
    root, _ = _extract_world(w)
    t9, _ = w.target("t9")
    late_root = w.tmp / "late-root"
    rel = _relocation_with(w, t9, _target_at(w, "tlate", late_root))
    plan = rel.plan(PROJECT, [root])
    assert [i["path"] for i in plan.items] == [str(root)]

    @hook("after_intent")
    def appear(item, quarantine, record):
        late_root.symlink_to(root.parent)

    record = by_path(rel.execute(plan.path), root)
    assert record["status"] == vr.REFUSED, record["reason"]
    assert record["reason"].startswith(vr.INSIDE_ARCHIVE_TARGET)
    assert record.get("quarantine_path") is None and (root / "tree").is_dir()
    assert record["intent_written"] is True and len(_extract_intents(w, root)) == 1


# --------------------------------------------------------------------------- #
# Revision 11 item 2 -- killing tests of rules the code already follows
# --------------------------------------------------------------------------- #

_LABEL_A = "rr-20261002t120000z-0a0a0a0a"
_LABEL_B = "rr-20261002t120001z-0b0b0b0b"


def _journal_with(world: World, *labels: str) -> Path:
    """A journal directory whose ``intents.jsonl`` holds one build record per
    label (what `_archive` appends before a build)."""
    journal = world.tmp / "journal"
    journal.mkdir(exist_ok=True)
    lines = [json.dumps({"schema": vr.INTENT_SCHEMA, "build_started": True,
                         "item": str(world.scratch / "x"), "receipt_id": label,
                         "target_id": "t1", "where": "target", "out_parent": "-",
                         "written_at": "2026-10-02T12:00:00Z"}) for label in labels]
    (journal / vr.INTENTS_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return journal


def test_pass_listing_uses_build_records(w, monkeypatch):
    """Catches the pass-level listing labelled by receipts alone (sstew4b-a
    M1, Revision 11 item 2): a build on the target is abandoned (the process
    dies after it; no receipt, only its build record). The pass-level
    `target_leftovers` reports the bundle as ``found`` with its bytes, and
    not as ``UNKNOWN_STAGING_ENTRY``. Mutant: pass-level labels without the
    build records. Negative control: no ReclaimReceipt exists, so only the
    build record can name the label."""
    item = w.copy("dup")
    w.end_setup()
    target, _ = w.target()
    real = ab.ArchiveBundle.build_tree

    def dying(source, out_parent, **kw):
        real(source, out_parent, **kw)
        raise Crash("the process dies right after the build")

    monkeypatch.setattr(ab.ArchiveBundle, "build_tree", staticmethod(dying))
    rel = w.relocation(target=target, admission=FakeAdmission("PRESSURE", admit=False))
    plan = rel.plan(PROJECT, [item])
    with pytest.raises(Crash):
        rel.execute(plan.path)
    monkeypatch.setattr(ab.ArchiveBundle, "build_tree", staticmethod(real))
    assert not list(w.receipts.glob("reclaim-*.json"))
    [name] = _staged_names(target)
    staged = target.root / "staging" / PROJECT / name
    size = sum((Path(d) / f).lstat().st_size for d, _, files in os.walk(staged) for f in files
               if (Path(d) / f).is_file() and not (Path(d) / f).is_symlink())
    listing = rel.target_leftovers(PROJECT, _relocation_scratch(w))
    assert listing["unknown"] == [] and listing["unmeasured"] == []
    assert listing["found"] == [{"target_id": target.target_id, "status": vr.STAGING_LEFTOVER,
                                 "path": str(staged), "bytes": size}]
    assert size > 0


def test_link_inside_found_entry_counts_zero(w):
    """Catches a found entry sized by following links (Revision 11 item 2,
    10a item 2): a labelled staging entry holds 3 bytes of its own, a symlink
    to a 5000-byte file outside every target and a symlink to an outside
    directory. It is ``found`` with 3 bytes; neither link is followed or
    opened. Mutant: `_walk_bytes` follows links. Negative control: the
    outside file really holds 5000 bytes, so 3 is not vacuous."""
    target, _ = w.target()
    entry = target.root / "staging" / PROJECT / f"vh-arch-x-{_LABEL_A}"
    (entry / "sub").mkdir(parents=True)
    (entry / "sub" / "own.bin").write_bytes(b"abc")
    outside = w.tmp / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"x" * 5000)
    (entry / "link-file").symlink_to(outside / "big.bin")
    (entry / "sub" / "link-dir").symlink_to(outside)
    rel = w.relocation(target=target)
    listing = rel.target_leftovers(PROJECT, _journal_with(w, _LABEL_A))
    assert listing["found"] == [{"target_id": target.target_id, "status": vr.STAGING_LEFTOVER,
                                 "path": str(entry), "bytes": 3}]
    assert listing["unmeasured"] == [] and listing["unknown"] == []
    assert (outside / "big.bin").stat().st_size == 5000


def test_labelled_symlink_on_walk_path_unmeasured(w):
    """Catches a labelled symlink on the walk's own path counted as found
    (Revision 11 item 2, 10a item 2): ``<target>/staging/<p>/vh-arch-y-<label>``
    is a symlink to an outside directory holding a file. It is not
    ``found`` and not sized; the target is ``unmeasured`` for that path.
    Mutant: counted as found (the link followed or sized). Negative
    control: a labelled real directory beside it is found with its bytes."""
    target, _ = w.target()
    staging = target.root / "staging" / PROJECT
    staging.mkdir(parents=True)
    outside = w.tmp / "outside"
    outside.mkdir()
    (outside / "f.bin").write_bytes(b"outside!")
    link = staging / f"vh-arch-y-{_LABEL_A}"
    link.symlink_to(outside)
    real_dir = staging / f"vh-arch-z-{_LABEL_B}"
    real_dir.mkdir()
    (real_dir / "f.bin").write_bytes(b"12345")
    rel = w.relocation(target=target)
    listing = rel.target_leftovers(PROJECT, _journal_with(w, _LABEL_A, _LABEL_B))
    assert listing["found"] == [{"target_id": target.target_id, "status": vr.STAGING_LEFTOVER,
                                 "path": str(real_dir), "bytes": 5}]
    [unmeasured] = listing["unmeasured"]
    assert unmeasured["target_id"] == target.target_id and unmeasured["path"] == str(link)
    assert "bytes" not in unmeasured and listing["unknown"] == []


def test_sizing_error_counts_unmeasured(w, monkeypatch):
    """Catches a labelled entry whose sizing fails dropped from the listing
    (Revision 11 item 2): sizing one of two labelled entries raises EIO; it
    is reported ``unmeasured`` (target, path, the error), the other is
    ``found``. Mutant: the failed entry dropped. Negative control: without
    the error both are found."""
    target, _ = w.target()
    staging = target.root / "staging" / PROJECT
    bad, good = staging / f"vh-arch-a-{_LABEL_A}", staging / f"vh-arch-b-{_LABEL_B}"
    for directory in (bad, good):
        directory.mkdir(parents=True)
        (directory / "f.bin").write_bytes(b"1234")
    rel = w.relocation(target=target)
    journal = _journal_with(w, _LABEL_A, _LABEL_B)
    real = vr._staged_bytes

    def failing(root, project_id, name):
        if name == bad.name:
            raise OSError(errno.EIO, "Input/output error (simulated)")
        return real(root, project_id, name)

    monkeypatch.setattr(vr, "_staged_bytes", failing)
    listing = rel.target_leftovers(PROJECT, journal)
    assert [f["path"] for f in listing["found"]] == [str(good)]
    [unmeasured] = listing["unmeasured"]
    assert unmeasured["path"] == str(bad) and "Input/output error" in unmeasured["detail"]
    monkeypatch.setattr(vr, "_staged_bytes", real)
    assert sorted(f["path"] for f in rel.target_leftovers(PROJECT, journal)["found"]) == [
        str(bad), str(good)]


def test_extract_payload_mismatch_kept(w):
    """Catches the extract gate's payload comparison removed (sstew4b-b M1,
    Revision 11 item 2): at the new seam ``before_extract_gate`` (after
    `_recheck_proof`, before `_extract_gate`) one byte of the bundle's
    payload on the target changes in place -- no link involved. The item is
    ``KEPT`` (EXTRACT_PROOF_CHANGED: the payload no longer has the proof's
    SHA-256), still at its path, with no intent. Mutant: the comparison
    removed. Negative control: with the byte restored the root relocates."""
    root, placed = _extract_world(w)
    t9, _ = w.target("t9")
    rel = _relocation_with(w, t9)
    payload = Path(placed.path) / ab.TREE_PAYLOAD
    payload.chmod(payload.stat().st_mode | 0o200)
    original = payload.read_bytes()[:1]

    def put(byte: bytes) -> None:
        with open(payload, "r+b") as handle:
            handle.write(byte)

    plan = rel.plan(PROJECT, [root])
    assert [i["path"] for i in plan.items] == [str(root)]
    vr._hooks["before_extract_gate"] = lambda item, proof, record: put(bytes([original[0] ^ 0xFF]))
    record = by_path(rel.execute(plan.path), root)
    vr._hooks.clear()
    assert record["status"] == vr.KEPT, record["reason"]
    assert record["reason"].startswith(vr.EXTRACT_PROOF_CHANGED)
    assert "no longer has the proof's SHA-256" in record["reason"]
    assert (root / "tree").is_dir() and _extract_intents(w, root) == []
    put(original)
    record = by_path(rel.execute(plan.path), root)
    assert record["status"] == vr.RELOCATED and not root.exists()


def test_containing_target_root_error_unmeasurable(w, monkeypatch):
    """Catches a target root that cannot be opened skipped when the
    containing target is decided (Revision 11 item 2): two targets, the
    bundle under the second (``t9``); opening the first's root by the
    staging walk fails with EACCES (`_open_target_root`, which the overlap
    check does not use, so it does not refuse first). The extract root is
    ``KEPT`` (TARGET_UNMEASURABLE naming ``t1``), still at its path, with no
    intent. Mutant: the failing root skipped (the root relocates under
    ``t9``). Negative control: without the EACCES it relocates."""
    root, _ = _extract_world(w)
    t1, _ = w.target("t1")
    t9, _ = w.target("t9")
    rel = _relocation_with(w, t1, t9)
    plan = rel.plan(PROJECT, [root])
    assert [i["path"] for i in plan.items] == [str(root)]
    real = vr._open_target_root
    armed = {"on": True}

    def denied(path):
        if armed["on"] and os.path.basename(os.fspath(path)) == "target-t1":
            raise PermissionError(errno.EACCES, "Permission denied (simulated)",
                                  os.fspath(path))
        return real(path)

    monkeypatch.setattr(vr, "_open_target_root", denied)
    record = by_path(rel.execute(plan.path), root)
    assert record["status"] == vr.KEPT, record["reason"]
    assert record["reason"].startswith(vr.TARGET_UNMEASURABLE) and "t1" in record["reason"]
    assert (root / "tree").is_dir() and _extract_intents(w, root) == []
    armed["on"] = False
    assert by_path(rel.execute(plan.path), root)["status"] == vr.RELOCATED


def test_walk_open_inode_compare(w, monkeypatch):
    """Catches the post-open inode comparison of the staging walk dropped
    (Revision 11 item 2): between `_walk_open`'s ``fstatat`` of ``staging``
    and its ``openat``, the directory is renamed away and a substitute
    holding a labelled entry takes its name. `_walk_open` raises ESTALE, and
    the pass-level listing reports the target ``unmeasured`` and finds
    nothing in the substitute. Mutant: the comparison dropped (the
    substitute is opened and its labelled entry found). Negative control:
    without the swap `_walk_open` returns a descriptor of the examined
    directory."""
    target, _ = w.target()
    (target.root / "staging" / PROJECT).mkdir(parents=True)
    real_stat = os.stat
    swaps = {"left": 0, "n": 0}

    def swapping(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if swaps["left"] and path == "staging" and kwargs.get("dir_fd") is not None:
            swaps["left"] -= 1
            swaps["n"] += 1
            (target.root / "staging").rename(target.root / f"staging.orig{swaps['n']}")
            substitute = target.root / "staging" / PROJECT / f"vh-arch-s-{_LABEL_A}"
            substitute.mkdir(parents=True)
            (substitute / "f.bin").write_bytes(b"substitute")
        return result

    monkeypatch.setattr(os, "stat", swapping)
    root_fd = vr._open_target_root(target.root)
    try:
        swaps["left"] = 1
        with pytest.raises(OSError) as caught:
            vr._walk_open(root_fd, "staging", directory=True)
        assert caught.value.errno == errno.ESTALE
        fd = vr._walk_open(root_fd, "staging", directory=True)    # no swap
        try:
            assert (os.fstat(fd).st_ino, os.fstat(fd).st_dev) == (
                real_stat(target.root / "staging").st_ino,
                real_stat(target.root / "staging").st_dev)
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)
    rel = w.relocation(target=target)
    swaps["left"] = 1
    listing = rel.target_leftovers(PROJECT, _journal_with(w, _LABEL_A))
    assert listing["found"] == []
    assert [u["target_id"] for u in listing["unmeasured"]] == [target.target_id]
