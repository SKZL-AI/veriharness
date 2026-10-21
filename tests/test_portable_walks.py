"""PFIX311: three places where the answer depended on the interpreter or on a concurrent writer.

The package supports Python 3.11, 3.12 and 3.13, and the public CI runs all three. Three defects of v0.4.0a1 showed only on
3.11 or only under a writer:

* the budget reads every run's `state.json` with `Path.rglob`, which on 3.11 omits a *dangling* link whose name is the
  pattern -- a run whose state is such a link counted as having spent nothing (the budget failed open);
* the digests of a frozen tree walk it with `Path.rglob` / `os.walk`, which on 3.11 recurse once per level -- a tree nested
  deeper than the recursion limit raised `RecursionError` and blocked the dispatch;
* `durable_io.read_single_link` refused a well-formed record that a writer of the same module was replacing or publishing
  at that moment (`has 0 names, not one`, `has 2 names, not one`).

The 3.11 behaviours are reproduced here by seams the production code goes through (`Path.rglob`, `os.walk`), written to do
what 3.11's pathlib and os.walk do -- the tests state what is simulated and run the same bodies with the interpreter's own
behaviour as well. Nothing here starts Herdr or an agent or reads outside `tmp_path`.
"""
from __future__ import annotations

import errno
import hashlib
import inspect
import json
import os
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from hoh import capability, durable_io
from hoh.governance import _spent_dispatches, limit_reached
from hoh.launcher import HohRunLauncher
from hoh.orchestrator import BudgetExhausted


# --- what Python 3.11 does, as seams ----------------------------------------------------------------------------------------

def _recursive_311(directory: Path, pattern: str):
    """3.11's `Path.rglob`: a generator per directory level (`yield from` down the tree, so the stack grows with the depth), a
    literal name found only if it `exists()` (a dangling link is not), a directory entered only if it is not a link."""
    with os.scandir(directory) as scan:
        entries = list(scan)
    for entry in entries:
        child = directory / entry.name
        if pattern == "*" or (entry.name == pattern and child.exists()):
            yield child
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            yield from _recursive_311(directory / entry.name, pattern)


def _rglob_as_311(self, pattern):
    return _recursive_311(self, pattern)


def _walk_as_311(top, topdown=True, onerror=None, followlinks=False):
    """3.11's `os.walk(topdown=True)`: recursive."""
    with os.scandir(top) as scan:
        entries = list(scan)
    dirs = [e.name for e in entries if e.is_dir()]
    yield top, dirs, [e.name for e in entries if not e.is_dir()]
    for name in dirs:
        new = os.path.join(top, name)
        if followlinks or not os.path.islink(new):
            yield from _walk_as_311(new)


@pytest.fixture(params=["interpreter", "as_3.11"])
def interpreter(request, monkeypatch):
    """Runs a test twice: with the running interpreter's own `rglob` / `os.walk`, and with those two replaced by what 3.11 does."""
    if request.param == "as_3.11":
        monkeypatch.setattr(Path, "rglob", _rglob_as_311)
        monkeypatch.setattr(os, "walk", _walk_as_311)
    return request.param


@contextmanager
def only_forty_frames_to_spare():
    """The recursion limit set 40 frames above the stack the test stands on: a walk that calls itself (or chains a generator)
    once per directory level fails on a tree 80 levels deep, one with an explicit stack needs a handful of frames."""
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack(0)) + 40)
    try:
        yield
    finally:
        sys.setrecursionlimit(old)


def _deep(top: Path, levels: int = 80) -> Path:
    here = top
    for _ in range(levels):
        here = here / "d"
        here.mkdir(parents=True)
    (here / "bottom").write_bytes(b"b")
    return here


def _write_run(root: Path, run_id: str, dispatches: int) -> Path:
    d = root / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({"run_id": run_id, "usage": {"dispatches": dispatches}}), encoding="utf-8")
    return d


def _launcher_spent(root: Path, tmp_path: Path) -> int:
    return HohRunLauncher(root, tmp_path / "repo", dispatch_budget=9).spent_budget()


def _governance_spent(root: Path, tmp_path: Path) -> int:
    return _spent_dispatches(root)


READERS = [pytest.param(_launcher_spent, BudgetExhausted, id="launcher"), pytest.param(_governance_spent, OSError, id="governance")]


# --- D1 the budget sees every state, on every interpreter ---------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["dangling", "link-to-a-good-state"])
@pytest.mark.parametrize("reader, refusal", READERS)
def test_a_run_whose_state_is_a_link_is_refused_never_counted_as_zero_on_any_interpreter(tmp_path, interpreter, reader, refusal, kind):
    """Catches the budget failing open on 3.11: `rglob("state.json")` omits a dangling link there, so the run was never read and
    its spend counted as 0. Both readers of the run states (the launcher's budget and the governance limits) must refuse a
    state that is a link, dangling or not -- under the running interpreter and under 3.11's `rglob`."""
    root = tmp_path / "root"
    _write_run(root, "n1", 4)
    (root / "n2").mkdir()
    link = root / "n2" / "state.json"
    if kind == "dangling":
        link.symlink_to(root / "nowhere")
    else:
        link.symlink_to(root / "n1" / "state.json")
    with pytest.raises(refusal):
        reader(root, tmp_path)


@pytest.mark.parametrize("reader, refusal", READERS)
def test_a_good_tree_of_states_is_counted_at_any_depth_on_every_interpreter(tmp_path, interpreter, reader, refusal):
    """Catches a walk that loses states while it stops following links: three runs, one of them a directory deeper, a link to a
    directory and a dangling link elsewhere in the tree that are not states -- 4 + 3 + 2 = 9."""
    root = tmp_path / "root"
    _write_run(root, "n1", 4)
    _write_run(root, "n2", 3)
    _write_run(root / "group", "n3", 2)
    (root / "elsewhere").symlink_to(root / "n1", target_is_directory=True)
    (root / "loose").symlink_to(root / "nowhere")
    assert reader(root, tmp_path) == 9


def test_state_files_are_found_at_every_depth_in_path_order(tmp_path):
    """Catches the order and the reach of the walk. `sorted(Path.rglob(...))` compares paths component by component, so
    `a/b/state.json` and `a/state.json` come before `a.b/state.json` (a string sort puts `a.b` first: `.` < `/`). A state at
    the root of the walked directory, one four levels down, and a directory named `state.json` (an entry like any other) are
    found."""
    root = tmp_path / "root"
    for rel in ("a/b", "a.b", "b/c/d", "."):
        (root / rel).mkdir(parents=True, exist_ok=True)
    for rel in ("a/b/state.json", "a/state.json", "a.b/state.json", "b/c/d/state.json", "state.json"):
        (root / rel).write_text("{}")
    (root / "b" / "state.json").mkdir()
    found = [str(p.relative_to(root)) for p in HohRunLauncher._state_files(root)]
    assert found == ["a/b/state.json", "a/state.json", "a.b/state.json", "b/c/d/state.json", "b/state.json", "state.json"]


def test_only_the_exact_name_state_json_is_a_state(tmp_path):
    """Catches a name match that is looser than the base's literal `rglob("state.json")` (`endswith`, a prefix, a case fold): a
    run tree holding `xstate.json`, `state.json.bak`, `State.json`, `state.jsonx` and ` state.json` beside the one state finds the
    one state only."""
    root = tmp_path / "root"
    _write_run(root, "n1", 4)
    for n, name in enumerate(("xstate.json", "state.json.bak", "State.json", "state.jsonx", " state.json"), start=2):
        (root / f"n{n}").mkdir()
        (root / f"n{n}" / name).write_text("not a state")
    assert [str(p.relative_to(root)) for p in HohRunLauncher._state_files(root)] == ["n1/state.json"]


@pytest.mark.parametrize("reader, refusal", READERS)
def test_a_directory_that_cannot_be_listed_refuses_the_budget(tmp_path, monkeypatch, reader, refusal):
    """Catches a spend that is read from only the part of the tree that could be listed: a run directory that is denied may hold
    the state of the run that spent the budget (every pathlib skipped it silently). The listing is refused by a seam on
    `os.scandir` (a mode-000 directory is not refused to root)."""
    root = tmp_path / "root"
    _write_run(root, "n1", 4)
    _write_run(root, "n2", 3)
    real = os.scandir

    def denied(path):
        if str(path).endswith("n2"):
            raise PermissionError(errno.EACCES, "denied", str(path))
        return real(path)
    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(refusal) as caught:
        reader(root, tmp_path)
    assert "cannot be computed" in str(caught.value) or refusal is OSError


@pytest.mark.parametrize("reader, refusal", READERS)
def test_a_deep_run_tree_is_walked_for_its_states_without_recursion(tmp_path, interpreter, reader, refusal):
    """Catches `RecursionError` out of the budget on 3.11: a run's arena holding a tree nested deeper than the recursion limit
    (a persisted pytest basetemp can be) made `rglob` raise, and nothing caught it. With 40 frames to spare the 80-level tree
    is walked and the spend, 4, is read."""
    root = tmp_path / "root"
    _deep(_write_run(root, "n1", 4) / "arena")
    with only_forty_frames_to_spare():
        assert reader(root, tmp_path) == 4


def test_the_dispatch_limit_of_a_contract_is_reached_when_a_state_cannot_be_read(tmp_path, interpreter):
    """Catches the fail-open at the place the governance limits ask: `limit_reached` reads the spend through the same states.
    With every state readable a limit of 9 is not reached by 4 + 3; with a run whose state is a dangling link it is reached (a
    limit that cannot be measured is reached), under the running interpreter and under 3.11's `rglob`."""
    state = SimpleNamespace(created_at="2026-10-01T00:00:00Z")
    contract = SimpleNamespace(budgets=SimpleNamespace(max_dispatches=9, max_wallclock_seconds=None),
                               horizon=SimpleNamespace(max_wallclock_seconds=None))
    good, bad = tmp_path / "good", tmp_path / "bad"
    for root in (good, bad):
        _write_run(root, "n1", 4)
    _write_run(good, "n2", 3)
    (bad / "n2").mkdir()
    (bad / "n2" / "state.json").symlink_to(bad / "nowhere")
    assert limit_reached(state, contract, good) is False
    assert limit_reached(state, contract, bad) is True


# --- D2 the digests walk a deep tree on every interpreter, with the bytes they always had --------------------------------------

def _ordinary_tree(top: Path) -> None:
    """A fixed tree of awkward names: `a` beside `a.b` (component order differs from string order), a dotfile, an executable,
    an empty directory, a link to a file, a dangling link, a link to a directory, a name with a space and a newline, a
    non-UTF-8 name, and a `.git` directory that is not digested."""
    for rel in ("a", "a.b", "empty", "sp ace", ".git/objects"):
        (top / rel).mkdir(parents=True)
    for rel, body in (("a/x", b"1"), ("a/y", b"22"), ("a.b/x", b"333"), (".hidden", b"h"), ("tool.sh", b"#!/bin/sh\n"),
                      ("sp ace/n\nl", b"nl"), (".git/HEAD", b"ref"), (".git/objects/o", b"o"), ("zed", b"z")):
        (top / rel).write_bytes(body)
    (top / "tool.sh").chmod(0o755)
    (top / "link").symlink_to("zed")
    (top / "dangling").symlink_to("nowhere")
    (top / "dirlink").symlink_to("a", target_is_directory=True)
    (top / os.fsdecode(b"bad\xff")).write_bytes(b"f")


# The digest of `_ordinary_tree` as the recursive walk computed it on the base commit (Python 3.13.5).
ORDINARY_TREE_DIGEST = "7b76ee151cacc0cd"


def _reference_tree_digest(root: Path) -> str:
    """The digest as it was computed before the walk was replaced: `sorted(rglob("*"))`, `.git` skipped, every entry folded."""
    h = hashlib.sha256()
    for entry in sorted(root.rglob("*")):
        if capability.VCS_METADATA in entry.parts:
            continue
        capability._entry(entry, str(entry.relative_to(root)), h)
    return h.hexdigest()[:16]


def _reference_inode_digest(root: Path) -> str:
    """`inode_digest` as it was before: `os.walk`, every relative name sorted as a string, one `lstat` each."""
    st = os.lstat(root)
    h = hashlib.sha256()

    def fold(rel, st_):
        h.update(os.fsencode(rel))
        h.update(b"\x00")
        h.update(f"{st_.st_mode}:{st_.st_size}:{st_.st_mtime_ns}:{st_.st_ctime_ns}".encode())
        h.update(b"\x00")
    fold(".", st)
    names = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            names.append(os.path.relpath(os.path.join(dirpath, name), root))
    for rel in sorted(names):
        fold(rel, os.lstat(root / rel))
    return h.hexdigest()[:16]


def test_the_digests_of_an_ordinary_tree_are_the_bytes_they_always_were(tmp_path):
    """Catches any change of the digest bytes (they are in witnesses): the contents digest of a fixed tree equals the constant
    the recursive walk produced on the base commit and the old algorithm run on the same tree; the inode digest equals the old
    algorithm's; and `.git` is not digested (a file added under it moves nothing)."""
    top = tmp_path / "tree"
    _ordinary_tree(top)
    assert capability.tree_digest(top) == ORDINARY_TREE_DIGEST == _reference_tree_digest(top)
    assert capability.inode_digest(top) == _reference_inode_digest(top)
    assert capability.frozen_digest(top) == ORDINARY_TREE_DIGEST + ":" + _reference_inode_digest(top)
    (top / ".git" / "objects" / "new").write_bytes(b"x")
    assert capability.tree_digest(top) == ORDINARY_TREE_DIGEST


def test_a_tree_below_a_directory_named_dot_git_digests_as_nothing(tmp_path):
    """Catches the quirk the contents digest has always had and that must not move: an entry whose absolute path has a `.git`
    component is skipped, so a root that itself lies below one folds nothing in."""
    inner = tmp_path / ".git" / "inner"
    inner.mkdir(parents=True)
    (inner / "f").write_bytes(b"1")
    assert capability.tree_digest(inner) == hashlib.sha256().hexdigest()[:16] == _reference_tree_digest(inner)


@pytest.mark.parametrize("name", ["tree_digest", "inode_digest", "frozen_digest"])
def test_a_tree_deeper_than_the_recursion_limit_is_digested_on_every_interpreter(tmp_path, interpreter, name):
    """Catches `RecursionError` out of a dispatch on 3.11: the contents and the inode digest of an arena holding a tree nested
    deeper than the recursion limit. With 40 frames to spare, the 80-level tree gives the digest it gives with room to spare."""
    top = tmp_path / "tree"
    top.mkdir()
    (top / "file").write_bytes(b"f")
    _deep(top / "arena")
    digest = getattr(capability, name)
    expected = digest(top)
    with only_forty_frames_to_spare():
        assert digest(top) == expected
    assert expected and expected.count(":") == (1 if name == "frozen_digest" else 0)


@pytest.mark.parametrize("name", ["tree_digest", "inode_digest"])
def test_a_tree_past_the_entry_limit_is_refused_loudly_and_at_the_limit_is_not(tmp_path, monkeypatch, name):
    """Catches a digest of a part of a tree taken for the digest of the tree, and an off-by-one limit: a tree of exactly the limit
    is digested, one entry more is `TreeWalkRefused` (an `OSError`, errno EFBIG). The contents digest does not walk `.git` (5
    entries, limit 5); the inode digest does (`.git`, its 50 files and the 5 files: limit 56)."""
    top = tmp_path / "tree"
    (top / ".git").mkdir(parents=True)
    for n in range(50):
        (top / ".git" / f"g{n}").write_bytes(b"g")
    for n in range(5):
        (top / f"f{n}").write_bytes(b"f")
    # tree_digest prunes `.git` (5 entries); inode_digest walks it (1 + 50 + 5 = 56)
    monkeypatch.setattr(capability, "MAX_DIGEST_ENTRIES", 5 if name == "tree_digest" else 56)
    digest = getattr(capability, name)
    assert digest(top)
    (top / "f5").write_bytes(b"f")
    with pytest.raises(capability.TreeWalkRefused) as caught:
        digest(top)
    assert isinstance(caught.value, OSError) and caught.value.errno == errno.EFBIG


@pytest.mark.parametrize("name", ["tree_digest", "inode_digest"])
def test_a_tree_nested_past_the_depth_limit_is_refused_loudly_and_at_the_limit_is_not(tmp_path, monkeypatch, name):
    """Catches an unbounded descent and an off-by-one: `top/a/b/c/f` lists four directories (`top`, `a`, `b`, `c`); under a limit
    of four it is digested, under three it is `TreeWalkRefused` (errno ELOOP)."""
    top = tmp_path / "tree"
    (top / "a" / "b" / "c").mkdir(parents=True)
    (top / "a" / "b" / "c" / "f").write_bytes(b"f")
    digest = getattr(capability, name)
    monkeypatch.setattr(capability, "MAX_DIGEST_DEPTH", 4)
    assert digest(top)
    monkeypatch.setattr(capability, "MAX_DIGEST_DEPTH", 3)
    with pytest.raises(capability.TreeWalkRefused) as caught:
        digest(top)
    assert caught.value.errno == errno.ELOOP


def _reference_seal_plan(root: Path) -> dict:
    """`seal_plan` as it was before: `os.walk`, one `lstat` per entry, links left out, shallowest first."""
    st = os.lstat(root)
    found = [(0, ".", capability.SealEntry(st.st_mode, st.st_dev, st.st_ino))]
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            st_ = os.lstat(root / rel)
            if stat.S_ISLNK(st_.st_mode):
                continue
            found.append((rel.count(os.sep) + 1, rel, capability.SealEntry(st_.st_mode, st_.st_dev, st_.st_ino)))
    found.sort(key=lambda t: (t[0], t[1]))
    return {rel: entry for _, rel, entry in found}


def test_the_seal_plan_of_an_ordinary_tree_is_the_plan_it_always_was(tmp_path):
    """Catches a changed plan (the seal is restored from it, entry by entry): the plan of the fixed awkward tree, links left out,
    `.git` included, equals the old algorithm's -- the same entries, the same identities, the same order."""
    top = tmp_path / "tree"
    _ordinary_tree(top)
    plan = capability.seal_plan(top)
    assert plan == _reference_seal_plan(top) and list(plan) == list(_reference_seal_plan(top))
    assert "link" not in plan and "dangling" not in plan and "dirlink" not in plan and ".git/objects/o" in plan


def test_the_seal_plan_of_a_tree_deeper_than_the_recursion_limit_is_made_on_every_interpreter(tmp_path, interpreter):
    """Catches `RecursionError` out of the seal on 3.11: the QA arena is sealed before it is digested, and a check's persisted
    pytest basetemp inside it is a tree nested deeper than the recursion limit. With 40 frames to spare the 80-level tree is
    planned: the plan has the same entries it has with room to spare, the deepest among them."""
    top = tmp_path / "arena"
    top.mkdir()
    (top / "file").write_bytes(b"f")
    bottom = _deep(top / "keep")
    expected = capability.seal_plan(top)
    with only_forty_frames_to_spare():
        plan = capability.seal_plan(top)
    assert plan == expected and str(bottom.relative_to(top) / "bottom") in plan and len(plan) == 1 + 1 + 1 + 80 + 1


def _scandir_seam(monkeypatch, name: str, exc: OSError) -> None:
    """`os.scandir` raising `exc` for the one directory whose path ends in `name` (a seam: a mode-000 directory is not refused to root)."""
    real = os.scandir

    def scandir(path):
        if str(path).endswith(name):
            raise exc
        return real(path)
    monkeypatch.setattr(os, "scandir", scandir)


@pytest.mark.parametrize("name", ["tree_digest", "inode_digest", "seal_plan"])
def test_a_listing_error_that_is_not_a_denial_is_raised_by_the_digests_and_the_seal_plan(tmp_path, monkeypatch, name):
    """Catches a silent partial walk (the base's `os.walk` swallowed every listing error, 3.13's `rglob` too): a directory that
    fails to list with ENAMETOOLONG is `TreeWalkRefused` out of the contents digest, the inode digest and the seal plan -- a digest or
    a plan of part of a tree is not one of the tree. (The QA seal's caller turns the `OSError` into `SealFailed`.)"""
    top = tmp_path / "arena"
    (top / "sub").mkdir(parents=True)
    (top / "sub" / "f").write_bytes(b"1")
    _scandir_seam(monkeypatch, "sub", OSError(errno.ENAMETOOLONG, "File name too long"))
    with pytest.raises(capability.TreeWalkRefused) as caught:
        getattr(capability, name)(top)
    assert caught.value.errno == errno.ENAMETOOLONG


@pytest.mark.parametrize("name", ["tree_digest", "inode_digest", "seal_plan"])
def test_the_digests_and_the_seal_plan_skip_a_denied_directory_as_they_always_did(tmp_path, monkeypatch, name):
    """Catches the budget's strictness leaking into the frozen-tree walks: a directory that cannot be listed (denied) is skipped by
    the digests and the seal plan -- the entry itself is folded in, its contents are not -- where the budget refuses. The contents
    digest of the tree with the denied directory equals that of the same tree with that directory empty; the plan lists the
    denied directory and not what is in it."""
    top = tmp_path / "arena"
    for rel, body in (("locked/f", b"1"), ("open/g", b"2")):
        (top / rel).parent.mkdir(parents=True, exist_ok=True)
        (top / rel).write_bytes(body)
    emptied = tmp_path / "emptied"
    (emptied / "locked").mkdir(parents=True)
    (emptied / "open").mkdir()
    (emptied / "open" / "g").write_bytes(b"2")
    _scandir_seam(monkeypatch, "locked", PermissionError(errno.EACCES, "denied"))
    result = getattr(capability, name)(top)
    if name == "seal_plan":
        assert set(result) == {".", "locked", "open", "open/g"}
    elif name == "tree_digest":
        assert result == capability.tree_digest(emptied) and result
    else:
        assert result


def test_a_tree_that_is_itself_named_dot_git_digests_as_nothing(tmp_path):
    """Catches the `.git` quirk narrowed to the parents of the root: a root whose own name is `.git` has `.git` among the parts of
    every entry below it, so it folds nothing in -- as the base's `entry.parts` test always did."""
    top = tmp_path / ".git"
    top.mkdir()
    (top / "f").write_bytes(b"1")
    assert capability.tree_digest(top) == hashlib.sha256().hexdigest()[:16] == _reference_tree_digest(top)


def test_the_limits_of_the_digest_walk_are_the_stated_ones():
    """Catches a limit that drifted: the depth is far past what a path can carry (ENAMETOOLONG comes near 2000 levels), the
    entries are the figure `storage_park.MAX_INODE_WALK` already uses for a walk that must end."""
    assert capability.MAX_DIGEST_DEPTH == 4096
    assert capability.MAX_DIGEST_ENTRIES == 5_000_000


def test_walk_tree_lists_every_entry_follows_no_link_and_prunes_by_name(tmp_path):
    """Catches `walk_tree` losing an entry kind or following a link: a file, a directory, a link to a file, a dangling link and
    a link to a directory (listed, not entered) all appear in the order of `sorted(rglob("*"))`; `prune` drops a name and its
    contents."""
    top = tmp_path / "tree"
    (top / "a").mkdir(parents=True)
    (top / "a" / "x").write_bytes(b"1")
    (top / "a.b").write_bytes(b"2")
    (top / "skip").mkdir()
    (top / "skip" / "inside").write_bytes(b"3")
    (top / "file-link").symlink_to("a.b")
    (top / "dead").symlink_to("nowhere")
    (top / "dir-link").symlink_to("a", target_is_directory=True)
    seen = [rel for rel, _full in capability.walk_tree(top)]
    assert seen == [str(p.relative_to(top)) for p in sorted(top.rglob("*"))]
    assert "dead" in seen and "dir-link" in seen and "dir-link/x" not in seen
    assert [rel for rel, _ in capability.walk_tree(top, prune="skip")] == [r for r in seen if not r.startswith("skip")]
    assert [full for _rel, full in capability.walk_tree(top)][0] == os.path.join(top, "a")


def test_walk_tree_treats_a_directory_it_cannot_list_by_the_rule_of_its_caller(tmp_path, monkeypatch):
    """Catches an unlistable directory handled by guess: a denied directory is skipped when `skip_denied` (the digests keep what
    every pathlib did) and `TreeWalkRefused` when not (the budget); a directory that vanished is skipped either way; any other
    error (a path too long) is `TreeWalkRefused` -- never a silent partial walk."""
    top = tmp_path / "tree"
    for name in ("ok", "denied", "gone", "toolong"):
        (top / name).mkdir(parents=True)
        (top / name / "f").write_bytes(b"1")
    real = os.scandir
    errors = {"denied": PermissionError(errno.EACCES, "denied"), "gone": FileNotFoundError(errno.ENOENT, "gone"),
              "toolong": OSError(errno.ENAMETOOLONG, "File name too long")}

    def scandir(path):
        for name, exc in errors.items():
            if str(path).endswith(name):
                raise exc
        return real(path)
    monkeypatch.setattr(os, "scandir", scandir)
    errors.pop("toolong")
    assert [r for r, _ in capability.walk_tree(top)] == ["denied", "gone", "ok", "ok/f", "toolong", "toolong/f"]
    with pytest.raises(capability.TreeWalkRefused) as denied:
        list(capability.walk_tree(top, skip_denied=False))
    assert denied.value.errno == errno.EACCES
    errors["toolong"] = OSError(errno.ENAMETOOLONG, "File name too long")
    with pytest.raises(capability.TreeWalkRefused) as long:
        list(capability.walk_tree(top))
    assert long.value.errno == errno.ENAMETOOLONG


# --- D3 a read that races a writer of the module returns a whole record or refuses a real inconsistency -----------------------

class Replacer:
    """`os.fstat` as the reader meets it, with a writer acting at the instant after the reader's open: the first `fstat` of the
    run is preceded by `before(n)` for the n-th call, which may replace the record (`durable_write`) or not."""

    def __init__(self, monkeypatch, before, link_counts=None, limit=100):
        self.calls = 0
        self.before = before
        self.link_counts = link_counts
        self.limit = limit
        self.real = os.fstat
        monkeypatch.setattr(durable_io.os, "fstat", self)

    def __call__(self, fd):
        self.calls += 1
        assert self.calls <= self.limit, "the read did not stop"
        if self.before is not None:
            self.before(self.calls)
        st = self.real(fd)
        if self.link_counts is None:
            return st
        count = self.link_counts[min(self.calls, len(self.link_counts)) - 1]
        return SimpleNamespace(st_mode=st.st_mode, st_nlink=count)


class Clock:
    """A clock that only `sleep` moves, so a wait is measured in the time it asked for and takes none. (`raising=False`: the
    module did not import `time` before the change; the seam is then a name nothing reads, and a test fails on the behaviour.)"""

    def __init__(self, monkeypatch):
        self.now = 0.0
        self.sleeps: list[float] = []
        monkeypatch.setattr(durable_io, "time", SimpleNamespace(monotonic=lambda: self.now, sleep=self.sleep), raising=False)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        assert len(self.sleeps) < 10_000, "the wait did not stop"


def test_a_record_replaced_between_the_open_and_the_fstat_is_read_again_from_its_name(tmp_path, monkeypatch):
    """Catches the spurious refusal: `durable_write` replaces the record after the reader opened it, the open descriptor is on an
    inode with no name (`st_nlink == 0`), and the reader refused a well-formed record ("has 0 names, not one"). The name holds
    the newer record; the read returns it, after exactly one more open."""
    path = tmp_path / "lease.json"
    durable_io.durable_write(path, b'{"n": 1}', min_free_bytes=0)
    replaced = []

    def writer(call):
        if call == 1:
            replaced.append(call)
            durable_io.durable_write(path, b'{"n": 2}', min_free_bytes=0)
    fstat = Replacer(monkeypatch, writer)
    assert durable_io.read_single_link(path) == b'{"n": 2}'
    assert replaced == [1] and fstat.calls == 2


def test_a_name_replaced_again_and_again_is_refused_after_sixteen_opens_and_waits_between_them(tmp_path, monkeypatch):
    """Catches an unbounded retry and a tight one: a writer that replaces the record at every open is `UnsafeRecordFile` ("has 0
    names, not one") after 16 opens, with 15 pauses of 1 ms between them (the pause lets a writer thread finish its cycle: retries
    against a writer thread are correlated, and one run of 433,000 reads on 3.11 met eight replacements in a row with immediate reopens). The counter is a local of the call: a second read starts at 0."""
    path = tmp_path / "lease.json"
    durable_io.durable_write(path, b"{}", min_free_bytes=0)
    clock = Clock(monkeypatch)
    n = []

    def writer(call):
        n.append(call)
        durable_io.durable_write(path, b'{"n": %d}' % call, min_free_bytes=0)
    fstat = Replacer(monkeypatch, writer)
    assert (durable_io.RECORD_REOPEN_ATTEMPTS, durable_io._REOPEN_PAUSE_S) == (16, 0.001)
    with pytest.raises(durable_io.UnsafeRecordFile, match="has 0 names, not one"):
        durable_io.read_single_link(path)
    assert fstat.calls == 16 and clock.sleeps == [0.001] * 15
    fstat.calls = 0
    with pytest.raises(durable_io.UnsafeRecordFile, match="has 0 names, not one"):
        durable_io.read_single_link(path)
    assert fstat.calls == 16


def test_a_record_that_has_two_names_for_an_instant_is_read_once_the_second_name_is_gone(tmp_path, monkeypatch):
    """Catches the second spurious refusal: `durable_create` publishes by `os.link` and drops its temp name afterwards, so a
    reader that arrives between the two saw "has 2 names, not one" (measured: median 60 us, worst 30 ms on this disk). The
    descriptor is looked at again every millisecond; the third look finds one name and the record is returned."""
    path = tmp_path / "rec.json"
    durable_io.durable_write(path, b"whole", min_free_bytes=0)
    clock = Clock(monkeypatch)
    Replacer(monkeypatch, None, link_counts=[2, 2, 1])
    assert durable_io.read_single_link(path) == b"whole"
    assert clock.sleeps == [0.001, 0.001]


def test_a_record_that_keeps_two_names_is_refused_after_a_quarter_of_a_second(tmp_path, monkeypatch):
    """Catches an unbounded wait and a wait that is no wait: two names that stay (a hard link made on purpose) are refused as
    before, after at most 0.25 s of 1 ms looks -- and a third name is not the instant `durable_create` leaves, so it is refused
    at once, without a look."""
    path = tmp_path / "rec.json"
    durable_io.durable_write(path, b"whole", min_free_bytes=0)
    clock = Clock(monkeypatch)
    Replacer(monkeypatch, None, link_counts=[2], limit=1000)
    assert durable_io.SECOND_NAME_GRACE_S == 0.25
    before = (path.read_bytes(), sorted(os.listdir(tmp_path)))
    with pytest.raises(durable_io.UnsafeRecordFile, match="has 2 names, not one"):
        durable_io.read_single_link(path)
    assert 249 <= len(clock.sleeps) <= 251 and abs(sum(clock.sleeps) - 0.25) < 0.005
    with pytest.raises(durable_io.UnsafeRecordFile, match="has 2 names, not one"):   # a further read starts again, and wrote nothing
        durable_io.read_single_link(path)
    assert (path.read_bytes(), sorted(os.listdir(tmp_path))) == before
    clock.sleeps.clear()
    Replacer(monkeypatch, None, link_counts=[3])
    with pytest.raises(durable_io.UnsafeRecordFile, match="has 3 names, not one"):
        durable_io.read_single_link(path)
    assert clock.sleeps == []


def test_a_real_second_name_is_still_refused_and_a_record_with_one_name_is_read(tmp_path):
    """Catches the guard being weakened for the real case: a record with a second name made by `os.link` (nothing is replacing
    anything) is refused, a symbolic link is refused, an absent name is `None`, and an ordinary record is returned. Real files
    and the real clock: the refusal of the second name takes a quarter of a second at most (and nothing near three)."""
    path = tmp_path / "rec.json"
    durable_io.durable_write(path, b"whole", min_free_bytes=0)
    assert durable_io.read_single_link(path) == b"whole"
    assert durable_io.read_single_link(tmp_path / "absent.json") is None
    os.link(path, tmp_path / "second-name")
    assert stat.S_ISREG(os.stat(path).st_mode) and os.stat(path).st_nlink == 2
    started = time.monotonic()
    with pytest.raises(durable_io.UnsafeRecordFile, match="has 2 names, not one"):
        durable_io.read_single_link(path)
    assert time.monotonic() - started < 3
    (tmp_path / "sym.json").symlink_to(path)
    with pytest.raises(durable_io.UnsafeRecordFile, match="symbolic link"):
        durable_io.read_single_link(tmp_path / "sym.json")


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="counts descriptors through /proc")
def test_the_record_read_closes_its_descriptor_on_the_retry_path_and_on_the_refusal_path(tmp_path, monkeypatch):
    """Catches a descriptor leaked by the loop: 20 reads that each meet one replacement (the retry path closes the old descriptor,
    the second open is read and closed) and 20 reads that are refused (the refusal path closes it) leave the process with the
    descriptors it had. The count is `/proc/self/fd` before and after."""
    path = tmp_path / "rec.json"
    durable_io.durable_write(path, b"x", min_free_bytes=0)
    (tmp_path / "adir").mkdir()
    Clock(monkeypatch)
    before = len(os.listdir("/proc/self/fd"))
    with monkeypatch.context() as seam:
        def writer(call):
            if call % 2 == 1:                     # the first look of every read finds the record replaced
                durable_io.durable_write(path, b"y", min_free_bytes=0)
        Replacer(seam, writer, limit=10_000)
        for _ in range(20):
            assert durable_io.read_single_link(path) == b"y"
    for _ in range(20):
        with pytest.raises(durable_io.UnsafeRecordFile, match="not a regular file"):
            durable_io.read_single_link(tmp_path / "adir")
    assert len(os.listdir("/proc/self/fd")) == before
