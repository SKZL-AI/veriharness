"""P3-39 (C5b): the admission ledger keeps a measured history of the inodes a run creates, and predicts the next from it.

The directive says "predicted per-run storage from measured history", and on a host whose binding limit is inodes as much as bytes
(the inode axis is CRITICAL below 3 % free) both measures need a history. The ledger already records the bytes a closed run wrote
(`release`); this file pins the second record, `inodes.jsonl` (`StorageAdmission.record_inodes`, `estimate_inodes`), the two
measurements that feed it (`storage_park.measure_owned_inodes` for a role run's dispatch, the verified tree of a closed temporary
workspace) and the two places the product records it (`Controller._dispatch`, `EphemeralRoots.release`). Failing closed is the rule:
a count that cannot be measured records nothing, and the estimate stays what it was. The wave's gate that reads it is
`tests/test_wave_storage.py`.

What is real: `StorageAdmission` (its ledger files in `tmp_path`, free space a fake `statvfs`), the walks, `Controller._dispatch`,
`EphemeralRoots`. No agent, no Herdr, no network, nothing outside `tmp_path`.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

import pytest
from test_storage_ephemeral import roots
from test_storage_ephemeral import w as w  # noqa: F401 - fixture
from test_storage_park import admission
from test_storage_park import repo as repo, spec as spec  # the alias form is an explicit re-export: fixtures, no F811 on the parameters
from test_storage_pressure import make
from test_storage_tiers import FakeAdmission, isolated_env  # noqa: F401 - isolated_env is an autouse fixture

from hoh import storage_ephemeral as se
from hoh import storage_park as spk
from hoh.capability import CapabilityViolation
from hoh.contracts import Budgets, Role
from hoh.controller import Controller
from hoh.store import RunStore

TOP = 10 ** 12                         # the largest count one run may record (literal on purpose)


def real(tmp_path: Path, **kw):
    return make(tmp_path / "state", **kw)[0]


def reserved(adm, kind="role_run"):
    return adm.reserve("p", kind)


def lines(adm) -> list[dict]:
    return [json.loads(x) for x in adm.inodes_path.read_text(encoding="utf-8").splitlines()]


DEEP_LEVELS = 1200                     # more directory levels than the interpreter's default recursion limit (1000)


def build_deep(top: Path, levels: int = DEEP_LEVELS, *, top_bytes: int = 3, bottom_bytes: int = 5) -> None:
    """A tree `levels` directories deep below `top`, one file of `top_bytes` in `top` and one of `bottom_bytes` at the bottom. Built
    one level at a time through directory descriptors: `os.makedirs` is itself recursive and a long path is never resolved."""
    top.mkdir()
    (top / "top").write_bytes(b"t" * top_bytes)
    fd = os.open(top, os.O_RDONLY)
    try:
        for _ in range(levels):
            os.mkdir("d", dir_fd=fd)
            child = os.open("d", os.O_RDONLY, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open("bottom", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd)
        try:
            os.write(leaf, b"b" * bottom_bytes)
        finally:
            os.close(leaf)
    finally:
        os.close(fd)


def remove_deep(top: Path) -> None:
    """Takes a tree built by `build_deep` away, bottom up, **iteratively** (`os.walk(topdown=False)` keeps its own stack): a deep
    tree must not outlive its test inside a pytest basetemp -- the harness that sizes the arena a run's check wrote into walks it
    too. Test cleanup of the test's own synthetic tree, nothing else."""
    for dirpath, dirnames, filenames in os.walk(top, topdown=False):
        for name in filenames:
            os.unlink(os.path.join(dirpath, name))
        for name in dirnames:
            os.rmdir(os.path.join(dirpath, name))
    os.rmdir(top)


# --- R1 the ledger: record, window, estimate, fail closed -------------------------------------------------------------------

def test_a_recorded_count_is_a_line_of_its_own_file_and_the_estimate_is_one_and_a_half_times_the_p95(tmp_path):
    """Catches a history that is not kept or not read: with nothing recorded `estimate_inodes` is `None` (never a guess, not 0);
    one count of 100 gives 150 (the policy's safety factor 1.5); the record is a line of `inodes.jsonl` (kind, reservation id, count,
    time) and the bytes history `history.jsonl` is not touched by it; twenty counts 1..20 give 29 (ceil(1.5 x p95 = 19)); two
    kinds are two histories; an unknown kind is refused as `estimate` refuses it."""
    adm = real(tmp_path)
    assert adm.estimate_inodes("role_run") is None and adm.estimate_inodes("tmp_workspace") is None
    first = reserved(adm)
    assert adm.record_inodes(first, 100) is True
    assert adm.estimate_inodes("role_run") == 150 and adm.estimate_inodes("tmp_workspace") is None
    [line] = lines(adm)
    assert (line["op_kind"], line["id"], line["actual_inodes"]) == ("role_run", first.id, 100) and line["closed_utc"].endswith("Z")
    assert not adm.history_path.exists()
    adm2 = real(tmp_path / "twenty")
    for n in range(1, 21):
        assert adm2.record_inodes(reserved(adm2), n)
    assert adm2.estimate_inodes("role_run") == 29
    with pytest.raises(ValueError):
        adm.estimate_inodes("no_such_kind")


def test_the_estimate_is_windowed_so_an_old_outlier_does_not_ratchet(tmp_path):
    """Catches an estimate over the whole history: two counts of 1 000 000 followed by twenty of 100 (the policy's window is 20) --
    the outliers are out of the window and the estimate is 150, where over all twenty-two the p95 would be an outlier."""
    adm = real(tmp_path)
    adm.record_inodes(reserved(adm), 1_000_000)
    adm.record_inodes(reserved(adm), 1_000_000)
    assert adm.estimate_inodes("role_run") == 1_500_000
    for _ in range(20):
        adm.record_inodes(reserved(adm), 100)
    assert adm.estimate_inodes("role_run") == 150


@pytest.mark.parametrize("bad", [True, False, 0, 1.5, -1, TOP + 1, "5", None, float("nan"), [3]],
                         ids=["true", "false", "zero", "float", "negative", "above-ceiling", "text", "none", "nan", "list"])
def test_a_count_that_is_not_a_measurement_records_nothing(tmp_path, bad):
    """Catches a fail-open record: a bool, a float, a negative, a count above 10**12, text, nothing (an unmeasurable walk is `None`),
    NaN or a list, and **0** (a run that exists owns at least its own directory: a count of 0 measured nothing), is refused --
    `False`, no file, the estimate stays `None`. Negative control: 1 and 10**12 exactly are measurements (the ceiling is inclusive)."""
    adm = real(tmp_path)
    assert adm.record_inodes(reserved(adm), bad) is False
    assert not adm.inodes_path.exists() and adm.estimate_inodes("role_run") is None
    assert adm.record_inodes(reserved(adm), 1) is True and adm.estimate_inodes("role_run") == 2
    assert adm.record_inodes(reserved(adm), TOP) is True and adm.estimate_inodes("role_run") == TOP * 3 // 2


def test_a_torn_or_foreign_line_is_skipped_and_a_failed_write_records_nothing(tmp_path, monkeypatch):
    """Catches a reader that trusts the file: a torn last line, a line of another kind, a line whose count is a bool or a float or
    negative, and a line that is not JSON are skipped -- only the two good lines of the kind count (their p95, 1.5 x 200 = 300); and a
    write that raises (a full disk) is `False`, never an exception, with the old file as it was."""
    adm = real(tmp_path)
    adm.record_inodes(reserved(adm), 100)
    adm.record_inodes(reserved(adm), 200)
    adm.record_inodes(reserved(adm, "tmp_workspace"), 9_999)
    with open(adm.inodes_path, "a", encoding="utf-8") as fh:
        fh.write('{"op_kind": "role_run", "id": "x", "actual_inodes": true, "closed_utc": "t"}\n')
        fh.write('{"op_kind": "role_run", "id": "x", "actual_inodes": 1.5, "closed_utc": "t"}\n')
        fh.write('{"op_kind": "role_run", "id": "x", "actual_inodes": -4, "closed_utc": "t"}\n')
        fh.write("not json at all\n")
        fh.write('{"op_kind": "role_run", "id": "x", "actual_inodes": 7')      # torn: no newline
    assert adm.estimate_inodes("role_run") == 300
    before = adm.inodes_path.read_bytes()

    def full(*a, **k):
        raise OSError(28, "no space left on device")
    monkeypatch.setattr(adm, "_append_history", full)
    assert adm.record_inodes(reserved(adm), 5) is False
    assert adm.inodes_path.read_bytes() == before


# --- R2 the measurements ---------------------------------------------------------------------------------------------------

def test_measure_owned_inodes_counts_entries_follows_no_link_and_gives_up_loudly(tmp_path, monkeypatch):
    """Catches a walk that follows links, counts a link path as a tree, or reports a partial count as the whole: a directory with
    three files, a subdirectory with two files and a symlink to a directory elsewhere (holding 50 files) is 3 + 1 + 2 + 1 = 7 entries,
    the link counted as the one entry it is and never entered; a given path that is a link, a file or missing counts 0, a path that is not a path, and a directory that cannot be listed, are `None` (unmeasurable, never skipped); two given trees
    add. With the bound at 6 the same tree is `None` (unmeasurable, not 6), at exactly 7 it is 7. A tree 1200 directories deep (more than the recursion limit) is
    counted (the walk is iterative: a recursion would raise)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    for n in range(50):
        (outside / f"f{n}").write_text("x")
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    for name in ("a", "b", "c"):
        (tree / name).write_text("x")
    for name in ("d", "e"):
        (tree / "sub" / name).write_text("x")
    (tree / "link").symlink_to(outside, target_is_directory=True)
    assert spk.measure_owned_inodes([tree]) == 7
    other = tmp_path / "other"
    other.mkdir()
    (other / "z").write_text("x")
    link_path = tmp_path / "link-to-tree"
    link_path.symlink_to(tree, target_is_directory=True)
    afile = tmp_path / "afile"
    afile.write_text("x")
    assert spk.measure_owned_inodes([link_path, afile, tmp_path / "missing"]) == 0
    assert spk.measure_owned_inodes([tree, other]) == 8
    deep = tmp_path / "deep"
    build_deep(deep)
    try:
        assert spk.measure_owned_inodes([deep]) == DEEP_LEVELS + 2           # the levels, the two files
    finally:
        remove_deep(deep)                           # no deep tree outlives the test in a persisted basetemp
    assert not deep.exists()
    assert spk.measure_owned_inodes([None]) is None                # a walk that raises is unmeasurable, not 0
    real_scandir = os.scandir

    def unlistable(path):
        if str(path).endswith("sub"):
            raise PermissionError(13, "denied", str(path))
        return real_scandir(path)
    monkeypatch.setattr(spk.os, "scandir", unlistable)
    assert spk.measure_owned_inodes([tree]) is None                # a directory that cannot be listed is unmeasurable, not skipped
    monkeypatch.setattr(spk.os, "scandir", real_scandir)
    assert spk.MAX_INODE_WALK == 5_000_000
    monkeypatch.setattr(spk, "MAX_INODE_WALK", 6)
    assert spk.measure_owned_inodes([tree]) is None
    monkeypatch.setattr(spk, "MAX_INODE_WALK", 7)
    assert spk.measure_owned_inodes([tree]) == 7


def test_measure_owned_bytes_measures_a_tree_nested_deeper_than_the_recursion_limit(tmp_path):
    """Catches a recursive walk (the product run `20261019t1146z-p339c5` was blocked by one): a tree 1200 directories deep, a file
    of 3 bytes at its top and one of 5 at its bottom, beside a link to a file and a link to a directory elsewhere (never counted,
    never followed), is 8 bytes -- with the recursion limit held at the interpreter's default of 1000, where a walk that calls itself
    once per level raises `RecursionError` before it gets to the bottom. The synthetic tree is taken away again (iteratively)
    before the node returns."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big").write_bytes(b"z" * 500)
    deep = tmp_path / "deep"
    build_deep(deep)
    (deep / "link-file").symlink_to(outside / "big")
    (deep / "link-dir").symlink_to(outside, target_is_directory=True)
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(min(limit, 1000))
    try:
        assert spk.measure_owned_bytes([deep]) == 8
        assert spk.measure_owned_bytes([deep, deep]) == 16 and spk.measure_owned_bytes([tmp_path / "missing"]) == 0
    finally:
        sys.setrecursionlimit(limit)
        os.unlink(deep / "link-file")
        os.unlink(deep / "link-dir")
        remove_deep(deep)
    assert not deep.exists()


def test_measure_owned_bytes_counts_a_directory_it_cannot_list_as_nothing_and_goes_on(tmp_path, monkeypatch):
    """Catches the iterative walk giving up (or counting a guess) at a directory it cannot list: the walk keeps the semantics of the
    recursive one -- an unlistable directory counts 0, an entry it cannot stat is skipped, and every other directory is still
    walked. `tree` holds 2 + 3 bytes in two listable directories and a third one (`locked`, 100 bytes) whose listing is denied: 5."""
    tree = tmp_path / "tree"
    for name, size in (("a", 2), ("b", 3), ("locked", 100)):
        (tree / name).mkdir(parents=True)
        (tree / name / "f").write_bytes(b"x" * size)
    real_scandir = os.scandir

    def denied(path):
        if str(path).endswith("locked"):
            raise PermissionError(13, "denied", str(path))
        return real_scandir(path)
    monkeypatch.setattr(spk.os, "scandir", denied)
    assert spk.measure_owned_bytes([tree]) == 5


def test_a_dispatch_over_an_arena_holding_a_tree_deeper_than_the_recursion_limit_is_sized_and_released(tmp_path, repo, spec):
    """Catches the blocked run itself: the harness sizes what a dispatch wrote with `measure_owned_bytes` over the run's owned
    trees, and a check's persisted pytest basetemp inside the arena (a deep tree) must not abort that sizing. The dispatch over
    an arena that also holds a tree 1200 directories deep releases its reservation with the bytes it wrote (one history line, the
    same figure as the same dispatch without the deep tree: its 8 bytes are in both samples), records its inode footprint (the deep levels counted) and raises only the frozen-tree
    violation the world expects."""
    plain = admission(tmp_path / "state-plain", 100)
    dispatch_world(tmp_path / "w-plain", repo, spec, plain, "add")
    [expected] = [json.loads(x) for x in plain.history_path.read_text(encoding="utf-8").splitlines()]
    adm = admission(tmp_path / "state", 100)
    state, before, after = dispatch_world(tmp_path / "w", repo, spec, adm, "add", deep=True)
    [entry] = [json.loads(x) for x in adm.history_path.read_text(encoding="utf-8").splitlines()]
    [counted] = lines(adm)
    assert entry["actual"] == expected["actual"] > 0 and before >= 200 + DEEP_LEVELS   # the deep tree's 8 bytes are in both samples
    assert counted["actual_inodes"] == max(before, after)
    assert not any("iteration aborted" in h for h in state.history)


# --- R3 where the product records it ---------------------------------------------------------------------------------------

class Footprint:
    """A role adapter for the dispatch nodes: before the dispatch the arena already holds `copies` files (the candidate's copy that
    `_verify` / `materialize` and the planner's read copy make before any dispatch starts); the dispatch then `add`s four entries or
    `remove`s `gone` of the copies."""

    def __init__(self, mode: str, gone: int = 0) -> None:
        self.arena: Path | None = None
        self.mode, self.gone = mode, gone

    def dispatch(self, role, prompt, *, state):
        if self.mode == "add":
            (self.arena / "one.bin").write_bytes(b"a" * 10)
            (self.arena / "dir").mkdir()
            (self.arena / "dir" / "two.bin").write_bytes(b"b")
            (self.arena / "dir" / "three.bin").write_bytes(b"c")
        else:
            for n in range(self.gone):
                (self.arena / f"copy{n}.py").unlink()
        return "done"

    def endpoint_evidence(self, role):
        return f"fake:{role.value}"


def dispatch_world(tmp_path, repo, spec, adm, mode, *, scratch=None, copies=200, gone=0, deep=False):
    """A QA dispatch (the frozen arena: it ends in a capability violation after the release) over an arena that already holds
    `copies` files; returns `(state, before, after)`, the entries of the owned trees before and after the dispatch. With `deep` the
    arena also holds a tree nested deeper than the recursion limit (a persisted pytest basetemp of a check can be), taken away again
    before the world returns."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = RunStore(tmp_path / "runs", "r1")
    state = Controller.new_state(run_id="r1", repo_path=repo, project_name="project", spec_path=spec, budgets=Budgets(max_iterations=5))
    state.arena_secret = "k11"
    store.write_state(state)
    writer = Footprint(mode, gone)
    ctl = Controller(store, writer, spec_path=spec, scratch_dir=scratch, storage_admission=adm)
    writer.arena = ctl._arena(state, suffix="k11")
    for n in range(copies):
        (writer.arena / f"copy{n}.py").write_text("x")
    ctl._qa_arena = writer.arena
    if deep:
        build_deep(writer.arena / "deep")
    try:
        before = spk.measure_owned_inodes(ctl._owned_for_sizing())
        with pytest.raises(CapabilityViolation, match="frozen tree was touched"):
            ctl._dispatch(Role.QA, "verify", state)
        return state, before, spk.measure_owned_inodes(ctl._owned_for_sizing())
    finally:
        if deep:
            remove_deep(writer.arena / "deep")


@pytest.mark.parametrize("scratch_dir", [True, False], ids=["scratch", "store"])
def test_a_role_dispatch_records_the_footprint_of_its_owned_trees_arena_copies_included(tmp_path, repo, spec, scratch_dir):
    """Catches the blocking finding of the spec review (A-B1): a count of the net a dispatch added. The arena copies are made before
    the dispatch starts, so a dispatch that writes four entries into an arena already holding 200 files must record what the
    owned trees hold (the 200 and the four, `max(before, after)`), not 4 -- the figure the gate predicts a run's inodes from. The
    release still records the bytes, and the estimate follows (1.5 x). Negative control: the same with `scratch_dir=None`."""
    adm = admission(tmp_path / "state", 100)
    scratch = tmp_path / "scratch" if scratch_dir else None
    state, before, after = dispatch_world(tmp_path / "w", repo, spec, adm, "add", scratch=scratch)
    [entry] = [json.loads(x) for x in adm.history_path.read_text(encoding="utf-8").splitlines()]
    [counted] = lines(adm)
    assert after >= before + 4 and before >= 200
    assert counted["op_kind"] == "role_run" and counted["id"] == entry["id"] and counted["actual_inodes"] == max(before, after)
    assert adm.estimate_inodes("role_run") == -(-counted["actual_inodes"] * 3 // 2)


def test_a_dispatch_that_removes_entries_records_the_peak_not_the_net_or_the_end(tmp_path, repo, spec):
    """Catches a record of the end (or of the net) of the dispatch: a dispatch that removes 100 of the 200 files its arena held
    ends with fewer entries than it began with; the record is the start (the larger of the two samples), and the net of a dispatch
    that only removed would be 0."""
    adm = admission(tmp_path / "state", 100)
    state, before, after = dispatch_world(tmp_path / "w", repo, spec, adm, "remove", copies=200, gone=100)
    [counted] = lines(adm)
    assert after < before and counted["actual_inodes"] == before


def test_an_admission_with_no_inode_record_still_releases_the_bytes(tmp_path, repo, spec):
    """Negative control of the node above: the admission of the other tests has `reserve` and `release` and no `record_inodes`; the
    bytes are released, nothing else is recorded and nothing raises."""
    class BytesOnly:
        def __init__(self, inner) -> None:
            self.inner = inner

        def __getattr__(self, name):
            if name == "record_inodes":
                raise AttributeError(name)
            return getattr(self.inner, name)

    inner = admission(tmp_path / "state", 100)
    dispatch_world(tmp_path / "w", repo, spec, BytesOnly(inner), "add")
    assert len(inner.history_path.read_text(encoding="utf-8").splitlines()) == 1 and not inner.inodes_path.exists()


@pytest.mark.parametrize("how", ["gave-up", "zero"])
def test_a_dispatch_whose_count_is_not_a_measurement_records_nothing_and_says_why(tmp_path, repo, spec, monkeypatch, how):
    """Catches a count recorded from a walk that gave up or that found nothing: with `MAX_INODE_WALK` at 0 every walk is
    unmeasurable, and with a walk that answers 0 (the owned trees of a run that has arenas hold entries; 0 measured nothing) the
    count is refused -- either way the dispatch releases its reservation with its bytes, the inode record is **absent**, the
    estimate stays `None` (the gate's floor stands) and the run's state says why."""
    if how == "gave-up":
        monkeypatch.setattr(spk, "MAX_INODE_WALK", 0)
    else:
        monkeypatch.setattr(spk, "measure_owned_inodes", lambda paths: 0)
    adm = admission(tmp_path / "state", 100)
    state, _, _ = dispatch_world(tmp_path / "w", repo, spec, adm, "add")
    assert len(adm.history_path.read_text(encoding="utf-8").splitlines()) == 1
    assert not adm.inodes_path.exists() and adm.estimate_inodes("role_run") is None
    said = [h for h in state.history if "no inode count recorded" in h]
    assert len(said) == 1 and ("unmeasurable" if how == "gave-up" else "a count of 0 is not a measurement") in said[0]


class Counting(FakeAdmission):
    """A fake admission that also keeps the inode records it is given."""

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.counted: list[tuple[object, object]] = []

    def record_inodes(self, reservation, actual_inodes):
        self.counted.append((reservation, actual_inodes))
        return True


def test_a_closed_temporary_workspace_records_the_entries_of_the_tree_it_verified(w, tmp_path):  # noqa: F811
    """Catches a workspace that records no count, or one for a tree nobody verified: a workspace with two files, a directory and one
    file in it (five entries with the workspace's own inode) is cleaned and its reservation released with its bytes and then a count of
    5; a workspace that is kept (not removed) is counted all the same; and one whose root is a link -- the walk refuses, nothing is
    verified, the bytes are 0 -- records **no** count. An admission with no `record_inodes` is released as before."""
    admission_ = Counting("NORMAL")
    r = roots(w, admission=admission_)
    ws = r.create(creator="t", purpose="test")
    (ws.path / "a").write_text("1")
    (ws.path / "b").write_text("22")
    (ws.path / "d").mkdir()
    (ws.path / "d" / "c").write_text("333")
    result = r.release(ws)
    assert result["removed"] is True
    assert [n for _, n in admission_.counted] == [5] and admission_.released[-1][1] == 6
    kept = r.create(creator="t", purpose="test")
    (kept.path / "k").write_text("1")
    r.release(kept, keep=True)
    assert [n for _, n in admission_.counted] == [5, 2]
    third = r.create(creator="t", purpose="test")
    (third.path / "x").write_text("1")
    link = tmp_path / "root-link"
    link.symlink_to(third.path.parent)
    out = r.release(dataclasses.replace(third, path=link / third.path.name))
    assert out["removed"] is False and [n for _, n in admission_.counted] == [5, 2]
    plain = FakeAdmission("NORMAL")
    p = roots(w, admission=plain)
    q = p.create(creator="t", purpose="test")
    assert p.release(q)["removed"] is True and len(plain.released) == 1


def test_the_entries_of_a_verified_tree_count_every_file_and_directory_once():
    """Catches a miscount of the snapshot: `_tree_entries` of a snapshot with two files and a directory holding a file and an empty
    directory is 5, and of nothing is 0."""
    snapshot = {"f": ("f", 1, 1), "g": ("f", 1, 2), "d": ("d", 3, {"h": ("f", 1, 3), "e": ("d", 4, {})})}
    assert se._tree_entries(snapshot) == 5 and se._tree_entries({}) == 0
