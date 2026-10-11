"""STORAGE-L: the `git_tree` proof and the liveness of legacy roots.

A tree copy is REPRODUCIBLE_SCRATCH only when its in-memory git tree id equals
the tree of a commit reachable from the canonical repository's refs; a legacy
root is HOT while a brief beside it that names it is not committed, or while
one of its anchor reports is no longer committed. Everything under
`tmp_path`; `/proc` is an empty directory of the test's own.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hoh import storage_tiers as st
from hoh.storage_steward import StorageSteward
from test_storage_registry import BOOT, fake, git, isolated_env  # noqa: F401  (fixtures)
from test_storage_registry_legacy import World, archive_into, commit_file


@pytest.fixture
def world(tmp_path, fake) -> World:
    return World(tmp_path, fake)


def sources(world: World) -> st.EvidenceSources:
    proc = world.tmp / "proc"
    proc.mkdir(exist_ok=True)
    return st.EvidenceSources(world.reg, "a", proc_root=str(proc))


def classify(world: World, path: Path) -> st.TierDecision:
    return st.classify_tier(path, registry=world.reg, sources=sources(world))


def object_count(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "count-objects", "-v"], capture_output=True,
                          text=True, check=True).stdout


# --------------------------------------------------------------------------- #
# K4 -- the git_tree proof is exact
# --------------------------------------------------------------------------- #


def test_exact_archive_copy_is_reproducible(world):
    """Catches: an exact copy of an older commit left COLD (no proof). Mutant:
    `git_tree_proof` not called by `classify_tier`."""
    world.register()
    decision = classify(world, world.root / "tree")
    assert decision.tier == st.REPRODUCIBLE_SCRATCH, decision
    assert decision.proof["type"] == "git_tree"
    assert decision.proof["commit"] == world.old
    assert decision.proof["tree"] == git(world.repo, "rev-parse", f"{world.old}^{{tree}}")


def test_extra_probe_file_not_reproducible(world):
    """Catches: unique probe files hidden in a reproducible copy. Mutant: extra
    entries ignored."""
    (world.root / "tree" / "probe_test.py").write_text("def test(): pass\n", encoding="utf-8")
    world.register()
    decision = classify(world, world.root / "tree")
    assert decision.tier == st.COLD and decision.proof is None


def test_pycache_residue_not_reproducible(world):
    """Catches: `__pycache__` treated as reproducible. Mutant: `__pycache__` skipped."""
    (world.root / "tree" / "pkg" / "__pycache__").mkdir()
    (world.root / "tree" / "pkg" / "__pycache__" / "mod.cpython-313.pyc").write_bytes(b"\0pyc")
    world.register()
    assert classify(world, world.root / "tree").tier == st.COLD


def test_mode_difference_not_reproducible(world):
    """Catches: an executable bit changed after extraction. Mutant: modes ignored."""
    os.chmod(world.root / "tree" / "tool.sh", 0o644)
    world.register()
    assert classify(world, world.root / "tree").tier == st.COLD


def test_dangling_commit_not_reproducible(world):
    """Catches: a tree matching only a commit outside every ref (reflog only).
    Mutant: reachability dropped (`cat-file -e`) or `--reflog` added."""
    git(world.repo, "checkout", "-q", "-b", "side")
    (world.repo / "side.txt").write_text("side\n", encoding="utf-8")
    git(world.repo, "add", "side.txt")
    git(world.repo, "commit", "-q", "-m", "side")
    side = git(world.repo, "rev-parse", "HEAD")
    git(world.repo, "checkout", "-q", "-")
    git(world.repo, "branch", "-q", "-D", "side")
    archive_into(world.repo, side, world.root / "side")
    world.register()
    assert classify(world, world.root / "side").tier == st.COLD
    assert classify(world, world.root / "tree").tier == st.REPRODUCIBLE_SCRATCH  # control


def test_dot_git_entry_not_git_tree(world):
    """Catches: a nested `.git` hidden in a tree copy. Mutant: `.git` skipped."""
    (world.root / "tree" / "pkg" / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    world.register()
    decision = classify(world, world.root / "tree")
    assert decision.tier == st.COLD
    assert any(".git entry" in n for n in decision.notes)


def test_empty_directory_not_reproducible(world):
    """Catches: an empty directory (git cannot hold it) ignored. Mutant: empty
    dirs dropped from the tree."""
    (world.root / "tree" / "empty").mkdir()
    world.register()
    assert classify(world, world.root / "tree").tier == st.COLD


def test_git_tree_writes_no_objects(world):
    """Catches: the proof writing objects into the canonical repository.
    Mutant: `hash-object -w` / `write-tree`."""
    (world.root / "probe").mkdir()
    (world.root / "probe" / "new.txt").write_text("a blob git has never seen\n",
                                                  encoding="utf-8")
    world.register()
    before = object_count(world.repo)
    assert classify(world, world.root / "tree").tier == st.REPRODUCIBLE_SCRATCH
    assert classify(world, world.root / "probe").tier == st.COLD
    assert object_count(world.repo) == before


def test_age_irrelevant_git_tree(world, monkeypatch):
    """Catches: a decision that reads a time stamp. Mutant: an mtime read.

    Run alone, this node failed (LGS-F1): the first classification of a process
    imports `hoh.archive_target` and `hoh.rehydration` lazily
    (`extract_root_proof`), and importlib reads `st_mtime` of the package
    directories to do it. That is the import system, not the decision, and in
    the whole file an earlier test had already paid it. The warm-up call below
    pays it first, so the spy sees only the second, import-free decision."""
    world.register()
    for path in (world.root / "tree").rglob("*"):
        os.utime(path, (0, 0), follow_symlinks=False)
    warm = classify(world, world.root / "tree")
    assert warm.tier == st.REPRODUCIBLE_SCRATCH
    seen: list[str] = []
    real = os.stat_result.__getattribute__

    def spy(self, name):
        if name in ("st_mtime", "st_mtime_ns", "st_atime", "st_ctime"):
            seen.append(name)
        return real(self, name)

    monkeypatch.setattr(os.stat_result, "__getattribute__", spy, raising=False)
    decision = classify(world, world.root / "tree")
    monkeypatch.undo()
    assert decision.tier == st.REPRODUCIBLE_SCRATCH
    assert seen == []


# --------------------------------------------------------------------------- #
# K5 -- legacy liveness and terminality
# --------------------------------------------------------------------------- #


def test_legacy_hot_while_designation_uncommitted(world):
    """Catches: a reused root (a new, uncommitted brief names it) relocated.
    Mutant: `_legacy_findings` not part of the open findings."""
    world.register()
    assert classify(world, world.root / "tree").tier == st.REPRODUCIBLE_SCRATCH  # control
    (world.reviews / "r1-reuse.md").write_text(f"Scratch: {world.root}/ again\n",
                                               encoding="utf-8")
    decision = classify(world, world.root / "tree")
    assert decision.tier == st.HOT
    assert any(r.startswith("open_finding") for r in decision.reasons)


def test_legacy_hot_while_report_uncommitted(world):
    """Catches: a root whose report was removed from HEAD treated as finished.
    Mutant: the report check in `_hot_reasons` dropped."""
    entry = world.register()
    [report] = entry["reports"]
    commit_file(world.repo, report["file"], "release: no -- rewritten after the anchor\n")
    decision = classify(world, world.root / "tree")
    assert decision.tier == st.HOT
    assert any(r.startswith("legacy_report") for r in decision.reasons)


def test_legacy_unlistable_parent_hot(world):
    """Catches: an unreadable evidence source read as empty. Mutant:
    `_legacy_findings` swallows the listing error."""
    world.register()
    os.chmod(world.reviews, 0o311)
    try:
        decision = classify(world, world.root / "tree")
    finally:
        os.chmod(world.reviews, 0o755)
    assert decision.tier == st.HOT


def test_legacy_terminal_only_on_committed_reports(world):
    """Catches: unproven legacy content WARM forever, or COLD without its
    reports. Mutant: `_terminal` true (or false) by default for legacy kinds."""
    (world.root / "notes.txt").write_text("unique\n", encoding="utf-8")
    world.register()
    assert classify(world, world.root / "notes.txt").tier == st.COLD


def test_legacy_hot_while_uncited_committed_brief_names_root(world):
    """B1. Catches: a root reused by a reviewer whose brief was committed before
    launch (Q-v2-3's practice) treated as free. Mutant B1c: `_legacy_reuse`
    not consulted."""
    world.register()
    assert classify(world, world.root / "tree").tier == st.REPRODUCIBLE_SCRATCH  # control
    rel = world.root.relative_to(world.tmp).as_posix()
    commit_file(world.repo, "dogfood/y/r1-again.md", f"Scratch: ~/{rel} (reuse)\n")
    decision = classify(world, world.root / "tree")
    assert decision.tier == st.HOT
    assert any(r.startswith("legacy_reuse") for r in decision.reasons)


def test_reanchored_root_not_hot_from_its_own_anchors(world):
    """Catches: a re-anchored root HOT forever because its earlier anchor file
    names it. Mutant B1e: only the current entry's anchor blob excluded."""
    world.register()
    anchor = world.anchor(designations=[world.brief(name="dogfood/x/r1-second.md")],
                          reports=[world.report()],
                          file="dogfood/storage-anchors/legacy-second.json")
    world.reg.register_legacy_root("a", world.root, "legacy_review", anchor)
    decision = classify(world, world.root / "tree")
    assert decision.tier == st.REPRODUCIBLE_SCRATCH, decision.reasons


def test_legacy_hot_tilde_spelling_uncommitted(world):
    """B1. Catches: an uncommitted brief that spells the root as `~/...` missed.
    Mutant B1d: only the absolute spelling recognised."""
    world.register()
    rel = world.root.relative_to(world.tmp).as_posix()
    (world.reviews / "r1-reuse.md").write_text(f"Scratch: ~/{rel}\n", encoding="utf-8")
    assert classify(world, world.root / "tree").tier == st.HOT


def test_root_followed_by_full_stop_is_hot(world):
    """lgspec-a N1 (P1). Catches: a committed, uncited brief ending a sentence
    with the root ("... in <root>.") missed. Mutant N1: the lookahead
    `(?![\\w.-])`. Control: the sibling name `<root>.old` is not the root."""
    world.register()
    commit_file(world.repo, "dogfood/y/sibling.md", f"See {world.root}.old for the old one\n")
    assert classify(world, world.root / "tree").tier == st.REPRODUCIBLE_SCRATCH  # control
    commit_file(world.repo, "dogfood/y/round2.md", f"Round 2: work again in {world.root}.\n")
    decision = classify(world, world.root / "tree")
    assert decision.tier == st.HOT
    assert any(r.startswith("legacy_reuse") for r in decision.reasons)


def test_tilde_root_followed_by_full_stop_is_hot(world):
    """lgspec-a N1 (P2). Catches: an uncommitted brief "Your scratch is ~/<rel>."
    missed (`text_refs` cannot see `~/`). Mutant N1."""
    world.register()
    rel = world.root.relative_to(world.tmp).as_posix()
    (world.reviews / "r1-again.md").write_text(f"Your scratch is ~/{rel}.\n", encoding="utf-8")
    assert classify(world, world.root / "tree").tier == st.HOT


def test_owner_home_spelling_without_steward_home(world, monkeypatch):
    """lgspec-a N2 (P3). Catches: `~/` read only against the steward process's
    HOME. Mutant N2: `_owner_home` not consulted."""
    world.register()
    rel = world.root.relative_to(world.tmp).as_posix()
    (world.reviews / "r1-again.md").write_text(f"Scratch: ~/{rel}/\n", encoding="utf-8")
    elsewhere = world.tmp / "other-home"
    elsewhere.mkdir()
    monkeypatch.setenv("HOME", str(elsewhere))
    monkeypatch.setattr(st, "_owner_home", lambda uid: str(world.tmp))
    assert classify(world, world.root / "tree").tier == st.HOT


@pytest.mark.parametrize("where", ["sibling", "session"])
def test_nearby_uncommitted_files_are_findings(world, where):
    """lgspec-b N1 (p6). Catches: an uncommitted brief in another reviewer dir
    (`reviews/r9/`) or in the session dir (`scratch/s0/`) naming the root
    missed. Mutants F6a/F6b: those directories not read."""
    world.register()
    directory = world.reviews / "r9" if where == "sibling" else world.reviews.parent
    directory.mkdir(exist_ok=True)
    (directory / "brief.md").write_text(f"Scratch: {world.root}/tree\n", encoding="utf-8")
    assert classify(world, world.root / "tree").tier == st.HOT


def test_own_bound_notes_do_not_make_own_root_hot(world):
    """Catches: a root's own bound notes (written before its anchor) naming its
    own children making it HOT forever; the same text elsewhere still counts.
    Mutant F6c: the own-root filter dropped."""
    (world.root / "notes.md").write_text(f"tree copy in {world.root}/tree\n", encoding="utf-8")
    world.register()
    assert classify(world, world.root / "tree").tier == st.REPRODUCIBLE_SCRATCH
    (world.reviews / "r9").mkdir()
    (world.reviews / "r9" / "notes.md").write_text(f"tree copy in {world.root}/tree\n",
                                                   encoding="utf-8")
    assert classify(world, world.root / "tree").tier == st.HOT


def test_a_bound_note_does_not_exempt_the_naming_of_another_root(world):
    """LGS-F2 E5. Catches: the own-root exemption of a bound, unchanged note
    extended to every root, so a note kept in `r1` that names `r2` (in a spelling
    `text_refs` cannot see, `~/`) no longer holds `r2` while `r2` is still named
    there. Mutant E5: `if (root != own or not exempt)` becomes `if not exempt`
    in `_legacy_findings`. Control: the same note names `r1` too and does not
    hold `r1` (the exemption is real)."""
    r2 = world.reviews / "r2"
    rel1 = world.root.relative_to(world.tmp).as_posix()
    rel2 = r2.relative_to(world.tmp).as_posix()
    (world.root / "notes.md").write_text(f"Scratch: ~/{rel1}/ and later ~/{rel2}/\n",
                                         encoding="utf-8")
    world.register()
    archive_into(world.repo, world.old, r2 / "tree")
    anchor = world.anchor(designations=[world.brief(root=r2, name="dogfood/x/r2.md")],
                          reports=[world.report()], path=r2,
                          file="dogfood/storage-anchors/legacy-r2.json")
    world.reg.register_legacy_root("a", r2, "legacy_review", anchor)
    own = classify(world, world.root / "tree")
    assert own.tier == st.REPRODUCIBLE_SCRATCH, own.reasons
    other = classify(world, r2 / "tree")
    assert other.tier == st.HOT, other.reasons
    assert any(r.startswith("open_finding") for r in other.reasons)


@pytest.mark.parametrize("prefix", ["$HOME/", "${HOME}/"])
def test_dollar_home_spellings_of_the_root_are_hot(world, prefix):
    """LGS-F2 E2. Catches: a brief that spells the root `$HOME/...` or
    `${HOME}/...` missed (`text_refs` cannot see either). Mutant E2: those two
    forms dropped from `_root_spellings`. Control: before the brief exists the
    tree is REPRODUCIBLE_SCRATCH, and a sibling name (`<root>2`) is not the
    root."""
    world.register()
    assert classify(world, world.root / "tree").tier == st.REPRODUCIBLE_SCRATCH
    rel = world.root.relative_to(world.tmp).as_posix()
    (world.reviews / "r1-sibling.md").write_text(f"Scratch: {prefix}{rel}2/\n", encoding="utf-8")
    assert classify(world, world.root / "tree").tier == st.REPRODUCIBLE_SCRATCH
    (world.reviews / "r1-reuse.md").write_text(f"Scratch: {prefix}{rel}/\n", encoding="utf-8")
    decision = classify(world, world.root / "tree")
    assert decision.tier == st.HOT, decision.reasons
    assert any(r.startswith("open_finding") for r in decision.reasons)


def test_a_bound_sibling_replaced_by_the_same_bytes_is_drift(world):
    """LGS-F2 E1. Catches: drift that compares content only. A bound note that
    is deleted and written again with the same bytes and mode is another inode;
    the anchor bound the first one. Mutant E1: `recorded != [st.st_dev,
    st.st_ino]` ignores the inode in `_legacy_root_drift`. Control: the tree is
    REPRODUCIBLE_SCRATCH until the note is replaced."""
    (world.root / "notes.md").write_text("first round\n", encoding="utf-8")
    world.register()
    assert classify(world, world.root / "tree").tier == st.REPRODUCIBLE_SCRATCH
    new = world.root / "notes.md.new"
    new.write_text("first round\n", encoding="utf-8")
    os.chmod(new, os.stat(world.root / "notes.md").st_mode & 0o7777)
    os.replace(new, world.root / "notes.md")
    decision = classify(world, world.root / "tree")
    assert decision.tier == st.HOT, decision.reasons
    assert any(r.startswith("legacy_drift") and "notes.md" in r for r in decision.reasons)


def test_legacy_findings_read_txt_and_json(world):
    """Catches: briefs or notes in `.txt`/`.json` ignored. Mutant X11:
    `_legacy_findings` reads only `*.md`."""
    world.register()
    (world.reviews / "r1-note.txt").write_text(f"working in {world.root}/tree\n",
                                               encoding="utf-8")
    assert classify(world, world.root / "tree").tier == st.HOT


def test_child_content_changed_after_anchor_refused(world):
    """B1. Catches: a bound child whose content changed after the anchor (now an
    exact copy of another commit) planned. Mutant B1b: the binding check in
    `classify_tier` dropped."""
    world.register()
    for name in ("mod.py",):
        (world.root / "tree" / "pkg" / name).write_text("x = 2\n", encoding="utf-8")
    decision = classify(world, world.root / "tree")
    assert decision.tier is None and decision.refused == st.UNKNOWN_OWNERSHIP
    assert st.LEGACY_UNBOUND in decision.reasons[0]


def test_git_tree_only_for_legacy_kinds(world):
    """B2. Catches: the git_tree proof widening eligibility to ordinary
    registry scratch. Mutant B2: the `LEGACY_KINDS` gate dropped."""
    world.reg.open_session("a", "s7", pid=100, boot_id=BOOT)
    session = world.reg.scratch_dir("a", "s7", "scratch")
    archive_into(world.repo, world.old, session / "copy")
    world.fake.procs.pop(100)                       # the session's lease is no longer live
    decision = classify(world, session / "copy")
    assert decision.tier == st.COLD and decision.proof is None


# --------------------------------------------------------------------------- #
# K7 -- the steward stays inside registered legacy roots
# --------------------------------------------------------------------------- #


def test_candidates_exclude_swapped_root(world):
    """Catches: a root that is no longer its recorded inode still listed.
    Mutant X10: the steward reads non-intact legacy roots."""
    world.register()
    os.rename(world.root, world.reviews / "r1-old")
    archive_into(world.repo, world.old, world.root / "tree")
    steward = StorageSteward("a", world.reg, admission=None, relocation=None)
    assert steward.candidates() == []


def test_candidates_include_legacy_root_children(world):
    """Catches: adopted legacy roots never reach the steward, or the parent
    `reviews/` scanned. Mutant: legacy roots not enumerated in `candidates`."""
    (world.reviews / "r2" / "tree").mkdir(parents=True)          # not adopted
    steward = StorageSteward("a", world.reg, admission=None, relocation=None)
    assert steward.candidates() == []                             # negative control
    world.register()
    assert steward.candidates() == [world.root / "tree"]


# --------------------------------------------------------------------------- #
# Revision 5 -- own-root exemption only for bound, unchanged files; root drift
# --------------------------------------------------------------------------- #


def _names_inside(world: World, refs) -> bool:
    return any(p == str(world.root) or p.startswith(str(world.root) + "/") for p in refs.paths)


def test_unbound_note_in_root_is_finding(world):
    """lgspec-a N1 Q1 / lgspec-b qa. Catches: a new note inside R naming R/tree
    exempted as R's own (bound) note. Mutant V5a: every file directly in R
    exempt. Asserted on `open_findings` itself (the drift check would make
    the item HOT anyway)."""
    world.register()
    assert not _names_inside(world, sources(world).open_findings())       # control
    (world.root / "reuse.md").write_text(f"working in {world.root}/tree again\n",
                                         encoding="utf-8")
    assert _names_inside(world, sources(world).open_findings())
    assert classify(world, world.root / "tree").tier == st.HOT


def test_rewritten_bound_note_is_finding(world):
    """lgspec-a N1 Q2 / lgspec-b qb. Catches: a bound note rewritten to name R
    exempted. Mutants V5a and V5b (`_bound_unchanged_file` ignores the digest)."""
    (world.root / "notes.md").write_text("first round\n", encoding="utf-8")
    world.register()
    assert not _names_inside(world, sources(world).open_findings())       # control
    (world.root / "notes.md").write_text(f"round 2: {world.root}/tree again\n",
                                         encoding="utf-8")
    assert _names_inside(world, sources(world).open_findings())
    assert classify(world, world.root / "tree").tier == st.HOT


def test_new_child_dir_makes_siblings_hot(world):
    """lgspec-b qc. Catches: a new round's directory inside R (R is being
    reused) leaving R's bound children eligible. Mutant D1: the drift check
    not consulted."""
    world.register()
    (world.root / "r2").mkdir()
    (world.root / "r2" / "x.txt").write_text("round 2\n", encoding="utf-8")
    decision = classify(world, world.root / "tree")
    assert decision.tier == st.HOT
    assert any(r.startswith("legacy_drift") for r in decision.reasons)


def test_rewritten_bound_file_is_drift(world):
    """Catches: a bound file of R rewritten (without naming anything) leaving
    the siblings eligible. Mutant D2: the content check of the drift dropped."""
    (world.root / "notes.md").write_text("first round\n", encoding="utf-8")
    world.register()
    assert classify(world, world.root / "tree").tier == st.REPRODUCIBLE_SCRATCH  # control
    (world.root / "notes.md").write_text("second round, no paths\n", encoding="utf-8")
    decision = classify(world, world.root / "tree")
    assert decision.tier == st.HOT
    assert any("changed since the anchor" in r for r in decision.reasons)


def test_quarantine_of_bound_child_is_not_drift(world):
    """Catches: a sibling's in-flight relocation (its quarantine: same inode,
    child absent) making every other child HOT. Mutant D3: the quarantine
    exemption of the drift dropped."""
    archive_into(world.repo, world.old, world.root / "tree2")
    world.register()
    os.rename(world.root / "tree2", world.root / "tree2.vh-quarantine.20261004T000000Z")
    assert classify(world, world.root / "tree").tier == st.REPRODUCIBLE_SCRATCH


def test_root_hyphen_spellings_hot(world):
    """lgspec-b qf. Catches: "<root>-" and "<root>--see" missed. Mutant N1c: the
    revision-4 lookahead. Control: the sibling `<root>-b` is not the root."""
    world.register()
    (world.reviews / "sib.md").write_text(f"see {world.root}-b\n", encoding="utf-8")
    assert classify(world, world.root / "tree").tier == st.REPRODUCIBLE_SCRATCH  # control
    for text in (f"in {world.root}-\n", f"in {world.root}--see below\n"):
        (world.reviews / "again.md").write_text(text, encoding="utf-8")
        assert classify(world, world.root / "tree").tier == st.HOT, text


# --------------------------------------------------------------------------- #
# Revision 6 -- drift measures every bound sibling's content
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", ["write", "hardlink"])
def test_bound_sibling_dir_changed_is_drift(world, case):
    """lgspec-b r4 rb. Catches: a write into a bound sibling directory (R in use
    again), or a hard link appearing in it, leaving `tree` eligible. Mutants
    D4 (content compared for files only) and D5 (a refused measurement is not
    drift)."""
    archive_into(world.repo, world.old, world.root / "tree2")
    world.register()
    assert classify(world, world.root / "tree").tier == st.REPRODUCIBLE_SCRATCH  # control
    if case == "write":
        (world.root / "tree2" / "round2.txt").write_text("again\n", encoding="utf-8")
    else:
        os.link(world.root / "tree2" / "README", world.tmp / "elsewhere")
    decision = classify(world, world.root / "tree")
    assert decision.tier == st.HOT
    assert any(r.startswith("legacy_drift") for r in decision.reasons)


def test_absent_bound_sibling_is_not_drift(world):
    """Pins a decision (revision 6, lgspec-b r4 rc): a bound child that is gone
    -- relocated, or moved out by its owner -- is not drift; the remaining
    children stay bound by inode and content. Mutant D6: absence counted as
    drift (a relocation would then make every sibling HOT)."""
    archive_into(world.repo, world.old, world.root / "tree2")
    world.register()
    os.rename(world.root / "tree2", world.tmp / "tree2-moved")
    assert classify(world, world.root / "tree").tier == st.REPRODUCIBLE_SCRATCH
