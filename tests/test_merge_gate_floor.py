"""`hoh merge-gate check`: a carried review counts what happened since the reviewed base, not since a side
branch forked (MG-RB3, requirement row P5-36; follows MG-RB1 and MG-RB2).

A review bound to an older base is carried when no commit between that base and the current one touches a
path the candidate changes. "Between" is the range `<reviewed base>..<base>`, and it was listed with `git log
-m`, which also diffs a **merge commit against each parent**. A side branch that forked before the reviewed
base and was merged after it makes that diff list everything the other parent brought in since the fork:
work at or below the reviewed base, which the reviewers saw and a narrow re-bind had already judged. So the
same old overlap refused the carry again after every later, disjoint merge. Now, when the reviewed base is
the candidate's merge base or a descendant of it, a merge commit counts a path of its diff against a parent
only if its version of the path also differs from the reviewed base's: the floor is the reviewed base. A
merge that changes a path in its own right still counts; a reviewed base that is not such a descendant, or
git failing while asking, keeps the old rule.

Every test builds its own repository under `tmp_path` and its own reports; nothing starts Herdr or an agent.
The two gates (`hoh.mergegate` and `tools/merge_gate.py`, the cross-check) are compared on the same cases.
"""
from __future__ import annotations

import functools
import hashlib
import importlib.util
import os
import subprocess
import time
from collections.abc import Callable

import pytest

from hoh import mergegate
from test_merge_gate import _git, _load, _row
from test_merge_gate_rebind import (SPEC, TOOL, Case, _TOUCHED, _bound, _commit, _main, _run_product,
                                    _run_tool)
from test_merge_gate_rebind import world as _rebind_world

world = _rebind_world            # the fixture of test_merge_gate_rebind, by name (the tests below take it as `world`)

_COUPLED = "import coupling(s) between the candidate and the commits in between: "
_RECORD_KEYS = {"review_base", "base", "commits", "merge_base", "candidate_paths", "touched_paths",
                "overlap", "merge_tree", "merged_tree"}


# --- building histories ---------------------------------------------------------------------------


def _shared(*edits: tuple[int, str]) -> str:
    """`shared.txt`: twenty lines, some of them edited; two edits far apart merge without a conflict."""
    lines = [f"line {i}\n" for i in range(1, 21)]
    for number, text in edits:
        lines[number - 1] = text + "\n"
    return "".join(lines)


_CODE = {"src/hoh/__init__.py": "",
         "src/hoh/orch.py": "from . import wave\n\n\ndef run():\n    return wave.run()\n",
         "src/hoh/wave.py": "def run():\n    return 1\n",
         "src/hoh/other.py": "def noop():\n    return 0\n"}


def _start(w, *, code: bool = False, d: str = "") -> str:
    """The fork point: `<d>shared.txt` (and, for the coupling cases, a few modules) on `main`; `d` is a
    directory prefix (`"d/"`) for the nested-path cases."""
    return _commit(w, {f"{d}shared.txt": _shared(), **(_CODE if code else {})}, "fork point")


def _from(w, name: str, rev: str, files: dict[str, str], message: str = "work") -> str:
    """A branch `name` from `rev` with one commit that writes `files`; the work tree is back on `main`."""
    _git(w.repo, "checkout", "-q", "-b", name, rev)
    sha = _commit(w, files, message)
    _main(w)
    return sha


def _merge(w, *branches: str, message: str = "merge") -> None:
    _git(w.repo, "merge", "-q", "--no-ff", "-m", message, *branches)


def _case(w, candidate: str, bound: str, why: str | None = None) -> Case:
    """Two reviews of `candidate` bound to `bound`; eligible unless `why` (a text of the refusal) is given."""
    spec = _git(w.repo, "show", f"{candidate}:{SPEC}") + "\n"
    sha = hashlib.sha256(spec.encode()).hexdigest()
    return Case(candidate, "HEAD", _bound(w, candidate, sha, bound), why is None, why or "")


def _side_below(w, *, code: bool = False, d: str = "") -> tuple[str, str, str, str]:
    """The fork point `fork`, a side branch `side` from it, a candidate from it (editing line 18 of
    `<d>shared.txt`, or `orch.py`), and the reviewed base `x` on `main`, which edits line 2 of `<d>shared.txt`
    (or `wave.py`): the side branch forked below `x`."""
    fork = _start(w, code=code, d=d)
    side = _from(w, "side", fork, {"src/hoh/other.py": "def noop():\n    return 2\n"} if code
                 else {f"{d}side.txt": "s\n"}, "side")
    if code:
        cand = _from(w, "cand-x", fork, {"src/hoh/orch.py": _CODE["src/hoh/orch.py"] + "# candidate\n"},
                     "candidate")
        x = _commit(w, {"src/hoh/wave.py": "def run():\n    return 2\n"}, "main edits wave.py")
    else:
        cand = _from(w, "cand-x", fork, {f"{d}shared.txt": _shared((18, "candidate"))}, "candidate")
        x = _commit(w, {f"{d}shared.txt": _shared((2, "main"))}, "main edits shared.txt (the reviewed base)")
    return fork, side, cand, x


# --- the cases: each moves `main` after the reviews were bound to `x` --------------------------------


def _forked_below_the_reviewed_base(w, d=""):
    """The p330s2 shape: a narrow re-bind judged the overlap up to `x`; a disjoint side branch, forked below
    `x`, is merged after it. With `d`: the same, every path nested in a directory."""
    _fork, _side, cand, x = _side_below(w, d=d)
    _merge(w, "side", message="merge side (disjoint)")
    return _case(w, cand, x)


def _overlap_on_the_first_parent_after_the_merge(w):
    _fork, _side, cand, x = _side_below(w)
    _merge(w, "side")
    _commit(w, {"shared.txt": _shared((2, "main"), (10, "main again"))}, "main edits shared.txt after x")
    return _case(w, cand, x, _TOUCHED + "'shared.txt'")


def _overlap_by_a_side_branch_merged_after(w):
    fork, _side, cand, x = _side_below(w)
    _from(w, "side2", fork, {"shared.txt": _shared((10, "side two"))}, "side two edits shared.txt")
    _merge(w, "side", "side2", message="merge both")
    return _case(w, cand, x, _TOUCHED + "'shared.txt'")


def _merge_that_reverts_to_the_older_version(w, d=""):
    """Merge commit with the side branch's old `shared.txt`: it differs from the reviewed base's, from the
    first parent's and not from the second's. With `d`: the path is nested (a merge's own change of a nested
    path is listed whole, `diff-tree -r`)."""
    _fork, _side, cand, x = _side_below(w, d=d)
    _git(w.repo, "merge", "-q", "--no-ff", "--no-commit", "side")
    _commit(w, {f"{d}shared.txt": _shared()}, "merge side, keeping the old shared.txt")
    return _case(w, cand, x, _TOUCHED + f"'{d}shared.txt'")


def _reviewed_base_below_the_candidates_base(w):
    """The reviewed base is an ancestor of the candidate's merge base, not a descendant of it: the old rule."""
    fork = _start(w)
    _from(w, "side", fork, {"side.txt": "s\n"}, "side")
    x = _commit(w, {"shared.txt": _shared((2, "main"))}, "main edits shared.txt (the reviewed base)")
    tip = _commit(w, {"other.txt": "1\n"}, "the candidate's base")
    cand = _from(w, "cand-x", tip, {"shared.txt": _shared((2, "main"), (18, "candidate"))}, "candidate")
    _merge(w, "side")
    return _case(w, cand, x, _TOUCHED + "'shared.txt'")


def _reviewed_base_is_the_candidates_base(w):
    fork = _start(w)
    _from(w, "side", fork, {"side.txt": "s\n"}, "side")
    x = _commit(w, {"shared.txt": _shared((2, "main"))}, "main edits shared.txt (the reviewed base)")
    cand = _from(w, "cand-x", x, {"shared.txt": _shared((2, "main"), (18, "candidate"))}, "candidate")
    _merge(w, "side")
    return _case(w, cand, x)


def _octopus_of_branches_forked_below(w):
    fork, _side, cand, x = _side_below(w)
    _from(w, "side2", fork, {"side2.txt": "s2\n"}, "side two")
    _merge(w, "side", "side2", message="octopus")
    return _case(w, cand, x)


def _side_branch_that_took_the_base(w):
    """The merge commit's FIRST parent is the side branch (it merged the reviewed base to catch up, and
    `main` fast-forwards to it): its diff against that parent lists what `x` brought in."""
    _fork, _side, cand, x = _side_below(w)
    _git(w.repo, "checkout", "-q", "side")
    _git(w.repo, "merge", "-q", "--no-ff", "-m", "side takes the base", x)
    _git(w.repo, "checkout", "-q", "main")
    _git(w.repo, "merge", "-q", "--ff-only", "side")
    return _case(w, cand, x)


def _side_branch_that_discarded_the_base(w, d=""):
    """... and the side branch took the base with `-s ours`: the merge keeps the old `shared.txt`."""
    _fork, _side, cand, x = _side_below(w, d=d)
    _git(w.repo, "checkout", "-q", "side")
    _git(w.repo, "merge", "-q", "--no-ff", "-s", "ours", "-m", "side discards the base", x)
    _git(w.repo, "checkout", "-q", "main")
    _git(w.repo, "merge", "-q", "--ff-only", "side")
    return _case(w, cand, x, _TOUCHED + f"'{d}shared.txt'")


def _octopus_that_discarded_the_base(w):
    """An octopus merge `-s ours` whose THIRD parent is the reviewed base: its tree is the first parent's,
    so it differs from the base only in a diff against the third parent."""
    fork, _side, cand, x = _side_below(w)
    _from(w, "side2", fork, {"side2.txt": "s2\n"}, "side two")
    _git(w.repo, "checkout", "-q", "side")
    _git(w.repo, "merge", "-q", "--no-ff", "-s", "ours", "-m", "octopus, discarding the base", "side2", x)
    _git(w.repo, "checkout", "-q", "main")
    _git(w.repo, "merge", "-q", "--ff-only", "side")
    return _case(w, cand, x, _TOUCHED + "'shared.txt'")


def _two_side_branches_merged_before_the_base_is_merged(w):
    """Two branches forked below `x` are merged into each other (the merge holds the old `shared.txt`, which
    differs from `x`'s, though it moved nothing), and only then into `main` with `x`: the first merge's
    own change since `x` is nothing."""
    fork, _side, cand, x = _side_below(w)
    _from(w, "side2", fork, {"side2.txt": "s2\n"}, "side two")
    _git(w.repo, "checkout", "-q", "side")
    _git(w.repo, "merge", "-q", "--no-ff", "-m", "the side branches meet", "side2")
    _git(w.repo, "checkout", "-q", "main")
    _merge(w, "side", message="main takes both")
    return _case(w, cand, x)


def _import_of_a_module_the_reviewed_base_changed(w):
    """The candidate's `orch.py` imports `wave.py`, which `x` changed (judged then); the later merge is of a
    branch that forked below `x` and touches `other.py`."""
    _fork, _side, cand, x = _side_below(w, code=True)
    _merge(w, "side")
    return _case(w, cand, x)


def _import_of_a_module_changed_after_the_merge(w):
    _fork, _side, cand, x = _side_below(w, code=True)
    _merge(w, "side")
    _commit(w, {"src/hoh/wave.py": "def run():\n    return 3\n"}, "main edits wave.py after x")
    return _case(w, cand, x, _COUPLED + "src/hoh/orch.py (candidate) imports src/hoh/wave.py, touched in between")


def _non_utf8_path_changed_on_the_first_parent_after_the_merge(w):
    """A path that is not UTF-8 is a path like any other (bytes), in the floor's lists too."""
    name = os.fsdecode(b"caf\xe9.txt")
    fork = _commit(w, {"shared.txt": _shared(), name: _shared()}, "fork point")
    _from(w, "side", fork, {"side.txt": "s\n"}, "side")
    cand = _from(w, "cand-x", fork, {name: _shared((18, "candidate"))}, "candidate")
    x = _commit(w, {"shared.txt": _shared((2, "main"))}, "main edits shared.txt (the reviewed base)")
    _merge(w, "side")
    _commit(w, {name: _shared((10, "main"))}, "main edits the Latin-1 name after x")
    return _case(w, cand, x, _TOUCHED + "'caf\\ufffd.txt'")


def _path_renamed_on_the_first_parent_after_the_merge(w):
    """A plain commit after the merge renames a path the candidate edits: the old name must be listed
    (`--no-renames`), the new name alone would hide it."""
    _fork, _side, cand, x = _side_below(w)
    _merge(w, "side")
    _git(w.repo, "mv", "shared.txt", "renamed.txt")
    _commit(w, None, "main renames shared.txt after x")
    return _case(w, cand, x, _TOUCHED + "'shared.txt'")


def _gitlink_added_on_the_first_parent_after_the_merge(w):
    """`diff.ignoreSubmodules=all` in the repository: only the gate's own `-c diff.ignoreSubmodules=none`
    lets the log of the range list a gitlink that a plain commit adds."""
    _git(w.repo, "config", "diff.ignoreSubmodules", "all")
    fork, _side, _plain, x = _side_below(w)
    _git(w.repo, "checkout", "-q", "-b", "cand-g", fork)
    _git(w.repo, "update-index", "--add", "--cacheinfo", f"160000,{fork},sub")
    _git(w.repo, "commit", "-q", "-m", "the candidate adds a gitlink")
    link = _git(w.repo, "rev-parse", "HEAD")
    _main(w)
    _merge(w, "side")
    _git(w.repo, "update-index", "--add", "--cacheinfo", f"160000,{fork},sub")
    _git(w.repo, "commit", "-q", "-m", "main adds the same gitlink after x")
    return _case(w, link, x, _TOUCHED + "'sub'")


CASES: dict[str, Callable] = {
    "carried-a-side-branch-forked-below-the-reviewed-base": _forked_below_the_reviewed_base,
    "carried-when-the-reviewed-base-is-the-candidates-base": _reviewed_base_is_the_candidates_base,
    "carried-an-octopus-of-branches-forked-below": _octopus_of_branches_forked_below,
    "carried-a-side-branch-that-took-the-base": _side_branch_that_took_the_base,
    "carried-an-import-of-a-module-the-reviewed-base-changed": _import_of_a_module_the_reviewed_base_changed,
    "stale-overlap-on-the-first-parent-after-the-merge": _overlap_on_the_first_parent_after_the_merge,
    "stale-overlap-by-a-side-branch-merged-after": _overlap_by_a_side_branch_merged_after,
    "stale-merge-reverts-to-the-older-version": _merge_that_reverts_to_the_older_version,
    "stale-side-branch-that-discarded-the-base": _side_branch_that_discarded_the_base,
    "stale-octopus-that-discarded-the-base-on-its-third-parent": _octopus_that_discarded_the_base,
    "carried-two-side-branches-merged-before-the-base-is-merged": _two_side_branches_merged_before_the_base_is_merged,
    "carried-a-nested-path-a-side-branch-forked-below-brought-in": functools.partial(
        _forked_below_the_reviewed_base, d="d/"),
    "stale-nested-path-a-merge-reverts-to-the-older-version": functools.partial(
        _merge_that_reverts_to_the_older_version, d="d/"),
    "stale-nested-path-a-side-branch-discarded-the-base": functools.partial(
        _side_branch_that_discarded_the_base, d="d/"),
    "stale-reviewed-base-below-the-candidates-base": _reviewed_base_below_the_candidates_base,
    "stale-import-of-a-module-changed-after-the-merge": _import_of_a_module_changed_after_the_merge,
    "stale-non-utf8-path-changed-on-the-first-parent-after-the-merge": _non_utf8_path_changed_on_the_first_parent_after_the_merge,
    "stale-path-renamed-on-the-first-parent-after-the-merge": _path_renamed_on_the_first_parent_after_the_merge,
    "stale-gitlink-added-on-the-first-parent-after-the-merge-with-submodules-ignored": _gitlink_added_on_the_first_parent_after_the_merge,
}
#: the number of paths the range touches, on the carried cases (`touched_paths` of the record)
_TOUCHED_PATHS = {"carried-a-side-branch-forked-below-the-reviewed-base": 1,
                  "carried-when-the-reviewed-base-is-the-candidates-base": 1,
                  "carried-an-octopus-of-branches-forked-below": 2,
                  "carried-a-side-branch-that-took-the-base": 1,
                  "carried-a-nested-path-a-side-branch-forked-below-brought-in": 1,
                  "carried-two-side-branches-merged-before-the-base-is-merged": 2,
                  "carried-an-import-of-a-module-the-reviewed-base-changed": 1}


def _tool(w):
    spec = importlib.util.spec_from_file_location("cross_check_merge_gate_floor", TOOL)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool.REPO = str(w.repo)
    return tool


# --- the product's decision -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_merge_counts_only_what_changed_since_the_reviewed_base_and_a_refusal_names_it(world, capsys, name):
    """Each case is decided from git alone. The carried ones are refused on the old rule (`log -m` lists the
    side branch's second-parent diff); the stale ones must stay refused with the path or import named: a
    path touched after the reviewed base on the first parent, by a side branch merged after it, by a merge
    commit that reverts to an older version (also with the side branch as FIRST parent), a reviewed base
    below the candidate's base. Kills: the old rule (every `carried-*`), a floor at the candidate's merge base
    (`carried-a-side-branch-forked-below-the-reviewed-base`), a floor that is not applied to the first parent
    (`carried-a-side-branch-that-took-the-base`), one that drops a parent (`stale-merge-reverts-to-the-older-
    version`, `stale-side-branch-that-discarded-the-base`), a floor applied although the reviewed base is
    not a descendant (`stale-reviewed-base-below-the-candidates-base`), one that needs a strict descendant
    (`carried-when-the-reviewed-base-is-the-candidates-base`), and the loss of the plain commits of the range
    (`stale-overlap-*`, `stale-import-*`)."""
    w = world
    case = CASES[name](w)
    rc, out, err, receipt = _run_product(w, capsys, case)
    assert err == ""
    body = _load(receipt)
    if case.eligible:
        assert rc == 0, out
        assert body["aggregate"] == "MERGE_ELIGIBLE" and body["problems"] == []
        for review in ("a", "b"):
            record = _row(body, review)["carried_forward"]
            assert record["touched_paths"] == _TOUCHED_PATHS[name], record
            assert record["overlap"] == [] and record["merge_tree"] == "clean"
            assert set(record) == _RECORD_KEYS | ({"imports"} if "import" in name else set()), record
    else:
        assert rc == 1, out
        assert body["aggregate"] == "NOT_MERGE_ELIGIBLE"
        details = [p["detail"] for p in body["problems"]]
        assert len(details) == 2
        for detail in details:
            assert "; not carried forward: " in detail and case.why in detail, detail


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_cross_check_tool_agrees_on_every_floor_case(world, capsys, name):
    """`tools/merge_gate.py` decides, names and records the same on the same cases. Kills a floor written
    in one gate only."""
    w = world
    case = CASES[name](w)
    product_rc, _out, _err, product_receipt = _run_product(w, capsys, case)
    tool_rc, text, tool = _run_tool(w, case)
    assert product_rc == tool_rc == (0 if case.eligible else 1), text
    product = _load(product_receipt)
    assert tool["aggregate"] == product["aggregate"]
    for review in ("a", "b"):
        carried = _row(tool, review).get("carried_forward")
        assert carried == _row(product, review).get("carried_forward")
        assert (carried is not None) == case.eligible
    if not case.eligible:
        assert "not carried" in text and case.why in text, text


def test_a_failure_while_asking_whether_the_reviewed_base_descends_keeps_the_old_rule_in_both_gates(
        world, monkeypatch):
    """Whether the reviewed base is the candidate's merge base or a descendant of it is asked with one
    `git merge-base --is-ancestor <merge base> <reviewed base>`; when that command fails (exit 128, or git
    cannot be run) the answer is no and the old rule applies: the p330s2 shape is then refused again, naming
    the old overlap. Without the failure it is carried. Kills a failure read as yes (fail open)."""
    w = world
    fork, _side, cand, x = _side_below(w)
    _merge(w, "side")
    head = _git(w.repo, "rev-parse", "HEAD")
    repo = str(w.repo.resolve())
    tool = _tool(w)
    assert mergegate.measure_carry(repo, x, head, cand).carried
    assert tool.carried(cand, head, x)[0] is not None
    real = subprocess.run

    def asked(argv) -> bool:
        return isinstance(argv, list) and "--is-ancestor" in argv and argv[-2:] == [fork, x]

    for failure in ("exit", "cannot-run"):
        def broken(argv, *args, **kwargs):
            if asked(argv):
                if failure == "cannot-run":
                    raise OSError("git vanished")
                return subprocess.CompletedProcess(argv, 128, b"", b"fatal: simulated failure\n")
            return real(argv, *args, **kwargs)

        with monkeypatch.context() as patched:
            patched.setattr(subprocess, "run", broken)
            product = mergegate.measure_carry(repo, x, head, cand)
            record, why = tool.carried(cand, head, x)
        assert not product.carried and _TOUCHED + "'shared.txt'" in product.detail, product.detail
        assert record is None and "'shared.txt'" in why, why


@pytest.mark.parametrize("failing", ["log", "tree-diff"])
def test_a_git_failure_in_the_lists_of_a_range_with_a_merge_is_a_stale_base_in_both_gates(world, monkeypatch,
                                                                                         failing):
    """Unreadable git state while the plain commits or a merge's tree diff are listed: the review cannot be
    carried and the answer is "not measurable" (stale, with the reason), never an empty list. Kills a failed
    command whose empty output is read as "nothing touched" (the floor would drop every path)."""
    w = world
    _fork, _side, cand, x = _side_below(w)
    _merge(w, "side")
    head = _git(w.repo, "rev-parse", "HEAD")
    repo = str(w.repo.resolve())
    tool = _tool(w)
    real = subprocess.run

    def hit(argv) -> bool:
        if not (isinstance(argv, list) and argv[:1] == ["git"] and "-C" in argv):
            return False
        return ("--no-merges" in argv) if failing == "log" else ("diff-tree" in argv and argv[-2:] == [x, head])

    def broken(argv, *args, **kwargs):
        if hit(argv):
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: simulated failure\n")
        return real(argv, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(subprocess, "run", broken)
        product = mergegate.measure_carry(repo, x, head, cand)
        record, why = tool.carried(cand, head, x)
    assert not product.carried and product.detail.startswith("not measurable: "), product.detail
    assert record is None and "unavailable or too many" in why, why


@pytest.mark.parametrize("merged", [True, False])
def test_the_tools_git_commands_floor_only_a_range_with_a_merge(world, merged):
    """The cross-check tool's commands, in order. A range with a merge asks once whether the reviewed base
    descends from the merge base, lists the plain commits (`log --no-merges`, not `-m`) and diffs the merge
    against the reviewed base and against each parent; a range WITHOUT a merge is the old sequence, one
    `log -m`, and no further question. Kills a tool that floors every range (its decisions are the same,
    so only the commands show it) and one that keeps `-m` in the floor's log."""
    w = world
    _fork, _side, cand, x = _side_below(w)
    if merged:
        _merge(w, "side")
    else:
        _commit(w, {"other.txt": "1\n"}, "plain")
    head = _git(w.repo, "rev-parse", "HEAD")
    side_tip = _git(w.repo, "rev-parse", "side")
    tool = _tool(w)
    calls = []
    real = tool.git_rc

    def spy(*args, **kwargs):
        calls.append(list(args))
        return real(*args, **kwargs)

    tool.git_rc = spy
    assert tool.carried(cand, head, x)[0] is not None

    def verb(args):
        i = 0
        while args[i] == "-c":
            i += 2
        return args[i]

    log = next(args for args in calls if verb(args) == "log")
    if merged:
        assert [verb(args) for args in calls] == [
            "rev-parse", "merge-base", "rev-list", "merge-base", "diff-tree", "merge-base", "log",
            "diff-tree", "diff-tree", "diff-tree", "rev-parse", "merge-tree"]
        assert "--no-merges" in log and "-m" not in log and f"{x}..{head}" in log
        assert [args[-2:] for args in calls if verb(args) == "diff-tree"][1:] == [
            [x, head], [x, head], [side_tip, head]]
        assert "--parents" in next(args for args in calls if verb(args) == "rev-list")
    else:
        assert [verb(args) for args in calls] == [
            "rev-parse", "merge-base", "rev-list", "merge-base", "diff-tree", "log", "rev-parse",
            "merge-tree"]
        assert "-m" in log and "--no-merges" not in log


def test_the_lists_of_a_range_with_merges_are_bounded_together_in_both_gates(world, monkeypatch):
    """The path lists of the log and of the tree diffs of a merge are held together to `CARRY_MAX_BYTES`
    (`>`): the p330s2 shape reads 9 + 9 + 9 + 11 = 38 bytes of them. At a bound of 38 it carries, at 37 the
    review stays stale with the reason. Kills a bound per list only, `>=`, and an unbounded read."""
    w = world
    _fork, _side, cand, x = _side_below(w)
    _merge(w, "side")
    head = _git(w.repo, "rev-parse", "HEAD")
    repo = str(w.repo.resolve())
    tool = _tool(w)
    for size, expected in ((38, True), (37, False)):
        with monkeypatch.context() as patched:
            patched.setattr(mergegate, "CARRY_MAX_BYTES", size)
            product = mergegate.measure_carry(repo, x, head, cand)
        assert product.carried is expected, (size, product.detail)
        assert expected or "larger than 37 bytes" in product.detail, product.detail
        tool.CARRY_MAX_BYTES = size
        record, why = tool.carried(cand, head, x)
        assert (record is not None) is expected, (size, why)
        assert expected or "too many" in why, why


def test_the_commands_of_a_range_with_a_merge_are_the_floor_and_nothing_else(world, monkeypatch):
    """After the ancestry, the range, the merge base and the candidate's paths, a range with a merge asks
    once whether the reviewed base descends from the merge base, lists the plain commits (`log --no-merges`,
    not `-m`), and diffs the merge against the reviewed base and against each parent; the range is listed with
    `--parents`. Each runs under the gate's hardening with full shas. Kills `-m` kept, a diff against the base
    branch instead of the reviewed base, and a diff taken without the parent."""
    w = world
    fork, _side, cand, x = _side_below(w)
    _merge(w, "side")
    head = _git(w.repo, "rev-parse", "HEAD")
    side_tip = _git(w.repo, "rev-parse", "side")
    calls = []
    real = subprocess.run

    def spy(argv, *args, **kwargs):
        calls.append(list(argv))
        return real(argv, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(subprocess, "run", spy)
        assert mergegate.measure_carry(str(w.repo.resolve()), x, head, cand).carried

    def verb(argv):
        rest = argv[argv.index("-C") + 2:]
        skip = 0
        for token in rest:
            if skip:
                skip -= 1
            elif token == "-c":
                skip = 1
            else:
                return token
        raise AssertionError(argv)

    assert [verb(argv) for argv in calls] == [
        "rev-parse", "merge-base", "rev-list", "merge-base",   # reviewed base, ancestry, range, merge base
        "diff-tree",                                           # the candidate's paths
        "merge-base",                                          # does the reviewed base descend from the merge base
        "log", "diff-tree", "diff-tree", "diff-tree",          # plain commits; the merge against x, x, the side
        "rev-parse", "merge-tree"]                             # objects, the merge
    assert all(argv[0] == "git" and "core.hooksPath=/dev/null" in argv for argv in calls)
    assert "--parents" in calls[2] and f"{x}..{head}" in calls[2]
    assert calls[5][-3:] == ["--is-ancestor", fork, x]
    merge_log = calls[6]
    assert "--no-merges" in merge_log and "-m" not in merge_log and f"{x}..{head}" in merge_log
    assert "diff.ignoreSubmodules=none" in merge_log
    diffs = [argv[-2:] for argv in calls[7:10]]
    assert diffs == [[x, head], [x, head], [side_tip, head]], diffs


def test_a_range_of_256_merge_commits_is_measured_in_both_gates(world, capsys):
    """Termination: there is no loop or retry; the work is bounded by the 256 commits of the range
    (`CARRY_MAX_COMMITS`): 255 merge commits and the side branch's commit here, 3 tree diffs per merge and one
    log, then the decision. All 256 carry (and 257 are a stale base, as in MG-RB1), in the product and in the
    tool."""
    w = world
    fork = _start(w)
    old_tree = _git(w.repo, "rev-parse", "HEAD^{tree}")
    cand = _from(w, "cand-x", fork, {"shared.txt": _shared((18, "candidate"))}, "candidate")
    x = _commit(w, {"shared.txt": _shared((2, "main"))}, "main edits shared.txt (the reviewed base)")
    tree = _git(w.repo, "rev-parse", "HEAD^{tree}")
    side = _git(w.repo, "commit-tree", old_tree, "-p", fork, "-m", "side, forked below the reviewed base")
    parent = x
    for i in range(255):
        parent = _git(w.repo, "commit-tree", tree, "-p", parent, "-p", side, "-m", f"merge {i}")
    _git(w.repo, "update-ref", "refs/heads/main", parent)
    case = _case(w, cand, x)
    started = time.monotonic()
    rc, out, err, receipt = _run_product(w, capsys, case)
    assert (rc, err) == (0, ""), out
    assert len(_row(_load(receipt), "a")["carried_forward"]["commits"]) == 256
    assert _run_tool(w, case)[0] == 0
    assert time.monotonic() - started < 240
    parent = _git(w.repo, "commit-tree", tree, "-p", parent, "-p", side, "-m", "merge 255")
    _git(w.repo, "update-ref", "refs/heads/main", parent)
    rc, out, err, receipt = _run_product(w, capsys, _case(w, cand, x), suite=False)
    assert (rc, err) == (1, "")
    assert all("more than 256 commits" in p["detail"] for p in _load(receipt)["problems"])
    assert _run_tool(w, _case(w, cand, x), suite=False)[0] == 1
