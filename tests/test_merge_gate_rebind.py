"""`hoh merge-gate check`: a review bound to an older base is carried forward mechanically (MG-RB1,
requirement row P5-36, house rules addendum of 2026-10-14, audit recommendation R1).

The rule under test (`hoh.mergegate.measure_carry`, and the same rule in `tools/merge_gate.py`, the
orchestrator's cross-check): a footer BASE that is not the current base is still accepted when the gate
itself measures that the BASE is an ancestor of the current base, the range between them is short, no
commit in it touches a path the candidate changes, and merging the candidate into the current base is
clean. Nothing is taken from the report but the sha. Everything else stays `STALE_BASE`.

Every test builds its own git repositories under `tmp_path` and writes its own review reports; no real
review file, no Herdr, no agent. The two gates are compared on the same cases (`CASES`): their agreement
is a criterion of the slice.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from hoh import mergegate
from test_merge_gate import World, _gate, _git, _load, _report, _row, _snapshot, _verify

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "merge_gate.py"
SPEC = "docs/spec.md"
_names = iter(range(1, 10_000))
_TOUCHED = "touched by the commits in between: "


# --- building histories --------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path) -> World:
    """The world of tests/test_merge_gate.py: a base commit on `main` (HEAD) and a candidate branch `cand`
    that changes the specification and adds `a.txt`; the candidate's specification digest is `spec_sha`."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / SPEC).write_text("# spec\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "cand")
    (repo / SPEC).write_text("# spec\nrevision 2\n", encoding="utf-8")
    (repo / "a.txt").write_text("candidate\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "candidate")
    cand = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return World(repo=repo, tmp=tmp_path, base=base, cand=cand, spec=SPEC,
                 spec_sha=hashlib.sha256(b"# spec\nrevision 2\n").hexdigest())



def _main(w: World) -> None:
    _git(w.repo, "checkout", "-q", "-f", "main")


def _commit(w: World, files: dict[str, str] | None = None, message: str = "m", *,
            gone: tuple[str, ...] = ()) -> str:
    """Write `files` (and `git rm` the `gone` ones) on the current branch and commit; the sha."""
    for name, text in (files or {}).items():
        path = w.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for name in gone:
        _git(w.repo, "rm", "-q", "--", name)
    _git(w.repo, "add", "-A")
    _git(w.repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(w.repo, "rev-parse", "HEAD")


def _branch(w: World, name: str, files: dict[str, str]) -> tuple[str, str]:
    """A candidate branch from the world's base: `(sha, sha256 of the specification in it)`."""
    _git(w.repo, "checkout", "-q", "-b", name, w.base)
    sha = _commit(w, files, f"candidate {name}")
    spec = _git(w.repo, "show", f"{sha}:{SPEC}")
    _main(w)
    return sha, hashlib.sha256((spec + "\n").encode()).hexdigest()


def _bound(w: World, candidate: str, spec_sha: str, base: str, **over: str | None) -> list[str]:
    """Two complete, stable, clean reviews of `candidate` bound to `base`."""
    n = next(_names)
    footer = {"CANDIDATE": candidate, "SPEC_SHA256": spec_sha, "BASE": base, **over}
    return [f"{who}={_report(w, f'{who}{n}', **footer)}" for who in ("a", "b")]


@dataclass
class Case:
    candidate: str
    base: str            # the --base argument
    reviews: list[str]
    eligible: bool
    why: str = ""        # a text of the not-carried reason that must be in the problem line


def _plain(w: World, *, bound: str | None = None) -> Case:
    return Case(w.cand, "HEAD", _bound(w, w.cand, w.spec_sha, bound or w.base), True)


def _stale(w: World, why: str, *, bound: str | None = None, candidate: str | None = None,
           spec_sha: str | None = None, base: str = "HEAD") -> Case:
    return Case(candidate or w.cand, base,
                _bound(w, candidate or w.cand, spec_sha or w.spec_sha, bound or w.base), False, why)


# --- the cases: each moves `main` after the reviews were bound to `w.base` ---------------------


def _unrelated_commits(w):
    _commit(w, {"other.txt": "1\n"}, "unrelated 1")
    _commit(w, {"docs/other.md": "2\n"}, "unrelated 2")
    return _plain(w)


def _side_merge_elsewhere(w):
    _git(w.repo, "checkout", "-q", "-b", "side", w.base)
    _commit(w, {"side.txt": "s\n"}, "side")
    _main(w)
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    _git(w.repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    return _plain(w)


def _unrelated_symlink(w):
    sha, spec_sha = _branch(w, "cand3", {"b.txt": "b\n"})
    (w.repo / "ln2").symlink_to("elsewhere")
    _commit(w, None, "a symlink the candidate does not change")
    return Case(sha, "HEAD", _bound(w, sha, spec_sha, w.base), True)


def _modified(w):
    _commit(w, {SPEC: "# spec\nrevision 2\n"}, "main makes the candidate's own edit of the specification")
    return _stale(w, _TOUCHED + "'docs/spec.md'")


def _added_the_same_path(w):
    _commit(w, {"a.txt": "candidate\n"}, "main adds a.txt, with the candidate's content")
    return _stale(w, _TOUCHED + "'a.txt'")


def _deleted(w):
    _commit(w, None, "main deletes the specification", gone=(SPEC,))
    return _stale(w, _TOUCHED + "'docs/spec.md'")


def _renamed(w):
    _git(w.repo, "mv", SPEC, "docs/spec-renamed.md")
    _commit(w, None, "main renames the specification")
    return _stale(w, _TOUCHED + "'docs/spec.md'")


def _touched_and_undone(w):
    _commit(w, {"a.txt": "x\n"}, "main adds a.txt")
    _commit(w, None, "main removes it again", gone=("a.txt",))
    return _stale(w, _TOUCHED + "'a.txt'")


def _merged_and_discarded(w):
    _git(w.repo, "checkout", "-q", "-b", "side", w.base)
    _commit(w, {"a.txt": "side\n"}, "side adds a.txt")
    _main(w)
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    _git(w.repo, "merge", "-q", "--no-ff", "-s", "ours", "-m", "merge side, keep ours", "side")
    return _stale(w, _TOUCHED + "'a.txt'")


def _evil_merge(w):
    """Both parents leave the candidate's paths alone; the merge commit itself changes one."""
    _git(w.repo, "checkout", "-q", "-b", "side", w.base)
    _commit(w, {"side.txt": "s\n"}, "side")
    _main(w)
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    _git(w.repo, "merge", "-q", "--no-ff", "--no-commit", "side")
    (w.repo / SPEC).write_text("# spec\nrevision 2\n", encoding="utf-8")        # the candidate's own edit
    _git(w.repo, "add", "-A")
    _git(w.repo, "commit", "-q", "-m", "merge side, with a change of its own")
    return _stale(w, _TOUCHED + "'docs/spec.md'")


def _mode_change(w):
    (w.repo / SPEC).chmod(0o755)
    _git(w.repo, "add", "-A")
    _git(w.repo, "commit", "-q", "-m", "main makes the specification executable")
    return _stale(w, _TOUCHED + "'docs/spec.md'")


def _symlink_retargeted(w):
    _git(w.repo, "checkout", "-q", "-b", "cand4", w.base)
    (w.repo / "ln").symlink_to("target-1")
    link = _commit(w, None, "the candidate adds a symlink")
    _main(w)
    (w.repo / "ln").symlink_to("target-1")
    _commit(w, None, "main adds the same symlink")
    return _stale(w, _TOUCHED + "'ln'", candidate=link, spec_sha=hashlib.sha256(b"# spec\n").hexdigest())


def _gitlink_moved(w):
    _git(w.repo, "checkout", "-q", "-b", "cand4", w.base)
    _git(w.repo, "update-index", "--add", "--cacheinfo", f"160000,{w.base},sub")
    _git(w.repo, "commit", "-q", "-m", "the candidate adds a gitlink")
    sub = _git(w.repo, "rev-parse", "HEAD")
    _main(w)
    _git(w.repo, "update-index", "--add", "--cacheinfo", f"160000,{w.base},sub")
    _git(w.repo, "commit", "-q", "-m", "main adds the same gitlink")
    return _stale(w, _TOUCHED + "'sub'", candidate=sub, spec_sha=hashlib.sha256(b"# spec\n").hexdigest())


def _gitlink_hidden_by_config(w):
    """`diff.ignoreSubmodules=all` in the repository: `git log` would not list the gitlink at all."""
    _git(w.repo, "config", "diff.ignoreSubmodules", "all")
    return _gitlink_moved(w)


def _non_utf8_path(w):
    """A path that is not UTF-8 is a path like any other (compared as bytes) and is shown escaped."""
    name = os.fsdecode(b"caf\xe9.txt")
    sha, spec_sha = _branch(w, "cand3", {name: "x\n"})
    _commit(w, {name: "x\n"}, "main adds the same Latin-1 name")
    return _stale(w, _TOUCHED + "'caf\\ufffd.txt'", candidate=sha, spec_sha=spec_sha)


def _other_line(w):
    _git(w.repo, "checkout", "-q", "-b", "other", w.base)
    other = _commit(w, {"x.txt": "x\n"}, "another line")
    _main(w)
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    return _stale(w, "not an ancestor of the base", bound=other)


def _bound_after_the_base(w):
    first = _commit(w, {"other.txt": "1\n"}, "unrelated 1")
    second = _commit(w, {"other.txt": "2\n"}, "unrelated 2")
    return _stale(w, "not an ancestor of the base", bound=second, base=first)


def _branch_force_moved_back(w):
    _commit(w, {"other.txt": "1\n"}, "unrelated 1")
    second = _commit(w, {"other.txt": "2\n"}, "unrelated 2")
    _git(w.repo, "update-ref", "refs/heads/main", w.base)
    return _stale(w, "not an ancestor of the base", bound=second)


def _branch_rewritten(w):
    first = _commit(w, {"other.txt": "1\n"}, "unrelated")
    _git(w.repo, "commit", "-q", "--amend", "--allow-empty", "-m", "unrelated, rewritten")
    assert _git(w.repo, "rev-parse", "HEAD") != first
    return _stale(w, "not an ancestor of the base", bound=first)


def _directory_against_file(w):
    sha, spec_sha = _branch(w, "cand3", {"newdir/x.txt": "x\n"})
    _commit(w, {"newdir": "a file where the candidate has a directory\n"}, "main adds a file")
    return _stale(w, "conflicts", candidate=sha, spec_sha=spec_sha)


def _directory_renamed(w):
    """No candidate path is touched; only git's directory-rename detection would move the new file."""
    sha, spec_sha = _branch(w, "cand3", {"docs/new.md": "new\n"})
    _git(w.repo, "config", "merge.directoryRenames", "true")       # the repository's own setting
    _git(w.repo, "mv", "docs", "documents")
    _git(w.repo, "commit", "-q", "-m", "main renames the directory")
    return _stale(w, "conflicts", candidate=sha, spec_sha=spec_sha)


def _unrelated_candidate(w):
    other = w.tmp / "other-repo"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    (other / "docs").mkdir()
    (other / SPEC).write_text("# unrelated\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "unrelated root")
    _git(w.repo, "fetch", "-q", "--no-tags", str(other), "HEAD")
    sha = _git(w.repo, "rev-parse", "FETCH_HEAD")
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    return _stale(w, "no merge base", candidate=sha,
                  spec_sha=hashlib.sha256(b"# unrelated\n").hexdigest())


def _criss_cross(w):
    """Two merge bases: main and the candidate each merged the other's earlier commit."""
    m1 = _commit(w, {"other1.txt": "1\n"}, "main 1")
    _git(w.repo, "checkout", "-q", "-b", "cand2", w.cand)
    _git(w.repo, "merge", "-q", "--no-ff", "-m", "candidate takes main 1", m1)
    crossed = _git(w.repo, "rev-parse", "HEAD")
    _main(w)
    _git(w.repo, "merge", "-q", "--no-ff", "-m", "main takes the candidate", w.cand)
    _commit(w, {"other2.txt": "2\n"}, "main 2")
    assert len(_git(w.repo, "merge-base", "--all", "HEAD", crossed).split()) == 2
    return _stale(w, "more than one merge base", candidate=crossed)


def _candidate_already_in_the_base(w):
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    return _stale(w, "nothing to measure", candidate=w.base, spec_sha=hashlib.sha256(b"# spec\n").hexdigest())


CASES: dict[str, Callable[[World], Case]] = {
    "carried-over-unrelated-commits": _unrelated_commits,
    "carried-over-an-unrelated-side-merge": _side_merge_elsewhere,
    "carried-over-an-unrelated-symlink": _unrelated_symlink,
    "stale-path-modified": _modified,
    "stale-path-added-too": _added_the_same_path,
    "stale-path-deleted": _deleted,
    "stale-path-renamed": _renamed,
    "stale-path-touched-and-undone": _touched_and_undone,
    "stale-path-merged-and-discarded": _merged_and_discarded,
    "stale-path-changed-in-a-merge-commit": _evil_merge,
    "stale-path-mode-changed": _mode_change,
    "stale-path-not-utf8": _non_utf8_path,
    "stale-symlink-retargeted": _symlink_retargeted,
    "stale-gitlink-moved": _gitlink_moved,
    "stale-gitlink-hidden-by-config": _gitlink_hidden_by_config,
    "stale-other-line": _other_line,
    "stale-bound-after-the-base": _bound_after_the_base,
    "stale-branch-forced-back": _branch_force_moved_back,
    "stale-branch-rewritten": _branch_rewritten,
    "stale-directory-against-file": _directory_against_file,
    "stale-directory-renamed": _directory_renamed,
    "stale-candidate-without-merge-base": _unrelated_candidate,
    "stale-two-merge-bases": _criss_cross,
    "stale-candidate-already-in-the-base": _candidate_already_in_the_base,
}


def _tree(w: World, rev: str) -> str:
    return _git(w.repo, "rev-parse", f"{rev}^{{tree}}")


def _suite_body(w: World, case: Case, **over) -> dict:
    """A well-formed suite receipt for the merge of the case's candidate into its base *as resolved now*
    (the merged tree is what `git merge-tree` makes of them; the case's merge must be clean)."""
    base = _git(w.repo, "rev-parse", case.base)
    merged = _git(w.repo, "merge-tree", "--write-tree", base, case.candidate).splitlines()[0]
    body = {"schema": "hoh-suite-receipt/1", "base": base, "candidate": case.candidate,
            "base_tree": _tree(w, base), "merged_tree": merged,
            "counts": {"base_failed": 7, "candidate_failed": 7, "new": 0},
            "new_failures": [], "accepted": [], "measured_at": "2026-10-14T12:00:00Z"}
    body.update(over)
    return body


def _suite(w: World, case: Case, **over) -> str:
    """The path of a valid suite receipt (create-only: a name of its own each time)."""
    path = w.tmp / f"suite-{next(_names)}.json"
    path.write_text(json.dumps(_suite_body(w, case, **over)), encoding="utf-8")
    return str(path)


def _run_product(w: World, capsys, case: Case, suite: str | bool = True):
    """`suite`: True = a valid receipt for an eligible case (none for a stale one), a path = that file,
    False = none."""
    if suite is True:
        suite = _suite(w, case) if case.eligible else False
    extra = ["--suite-receipt", suite] if suite else []
    return _gate(w, capsys, case.reviews, *extra, candidate=case.candidate, base=case.base)


def _run_tool(w: World, case: Case, suite: str | bool = True) -> tuple[int, str, dict]:
    receipt = w.tmp / f"tool-receipt-{next(_names)}.json"
    if suite is True:
        suite = _suite(w, case) if case.eligible else False
    argv = [sys.executable, "-P", str(TOOL), "--repo", str(w.repo), "--candidate", case.candidate,
            "--base", case.base, "--spec", SPEC, "--receipt", str(receipt)]
    if suite:
        argv += ["--suite-receipt", suite]
    for item in case.reviews:
        argv += ["--review", item]
    done = subprocess.run(argv, capture_output=True, text=True, cwd=w.tmp, check=False)
    return done.returncode, done.stdout + done.stderr, _load(str(receipt))


# --- the product's decision --------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_carry_decision_is_the_gates_own_measurement(world, capsys, name):
    """Each case is decided from git alone, and says why when it is a no. Kills: dropping the overlap check
    (every `stale-path-*`, `stale-symlink-*`, `stale-gitlink-*`), `--no-renames` (`stale-path-renamed`),
    `-m` of the range log (`stale-path-changed-in-a-merge-commit`), the ancestor check (`stale-other-line`,
    `stale-bound-after-the-base`, `stale-branch-*`), the merge-tree check (`stale-directory-against-file`),
    the directory-rename setting (`stale-directory-renamed`), the single merge base (`stale-two-merge-bases`,
    `stale-candidate-without-merge-base`) and the empty candidate (`stale-candidate-already-in-the-base`)."""
    w = world
    case = CASES[name](w)
    rc, out, err, receipt = _run_product(w, capsys, case)
    assert err == ""
    body = _load(receipt)
    head = _git(w.repo, "rev-parse", "HEAD")
    if case.eligible:
        assert rc == 0, out
        assert body["aggregate"] == "MERGE_ELIGIBLE" and body["problems"] == []
        bound = _row(body, "a")["footer"]["BASE"]
        assert bound != body["base"] == head
        assert body["suite"]["problems"] == [] and body["suite"]["actual_merged_tree"] == _row(
            body, "a")["carried_forward"]["merged_tree"]
        assert body["suite"]["actual_base_tree"] == _tree(w, head)
        between = _git(w.repo, "rev-list", f"{bound}..{head}").split()
        for review in ("a", "b"):
            row = _row(body, review)
            assert (row["status"], row["problems"]) == ("RELEASE_YES", [])
            assert row["carried_forward"] == {
                "review_base": bound, "base": head, "commits": between,
                "merge_base": _git(w.repo, "merge-base", head, case.candidate),
                "candidate_paths": row["carried_forward"]["candidate_paths"],
                "touched_paths": row["carried_forward"]["touched_paths"],
                "overlap": [], "merge_tree": "clean", "merged_tree": row["carried_forward"]["merged_tree"]}
            assert row["carried_forward"]["candidate_paths"] >= 1
            assert row["carried_forward"]["touched_paths"] >= 1
        # the recorded tree is the one a real merge makes (the suite of the merge step runs on it)
        merged = row["carried_forward"]["merged_tree"]
        _git(w.repo, "checkout", "-q", "-f", "-b", "probe-merge", head)
        _git(w.repo, "merge", "-q", "--no-ff", "-m", "probe", case.candidate)
        assert _git(w.repo, "rev-parse", "HEAD^{tree}") == merged
        assert [ln for ln in out.splitlines() if ln.startswith("Carried forward mechanically: ")] == [
            f"Carried forward mechanically: {r} was reviewed at base {bound[:12]}, the base is "
            f"{head[:12]} ({len(between)} commit(s) between, no candidate path touched, merge clean)"
            for r in ("a", "b")]
    else:
        assert rc == 1, out
        assert body["aggregate"] == "NOT_MERGE_ELIGIBLE"
        for review in ("a", "b"):
            row = _row(body, review)
            assert (row["status"], row["problems"]) == ("NO_VERDICT", ["STALE_BASE"])
            assert "carried_forward" not in row
        details = [p["detail"] for p in body["problems"]]
        assert len(details) == 2
        for detail in details:
            assert "is not " in detail and "; not carried forward: " in detail and case.why in detail, detail
        assert "Carried forward mechanically" not in out


def test_a_review_bound_to_the_current_base_has_no_carry_and_the_receipt_is_unchanged(world, capsys):
    """The default path is byte for byte what it was: no `carried_forward` key, no git command beyond the
    three that resolve the inputs (`test_git_runs_with_the_hardening_the_spec_names` counts them)."""
    w = world
    rc, out, err, receipt = _gate(w, capsys, [f"a={_report(w, 'a-plain')}", f"b={_report(w, 'b-plain')}"])
    assert (rc, err) == (0, "")
    assert all("carried_forward" not in row for row in _load(receipt)["reviews"])
    assert "Carried forward" not in out


def test_the_bound_is_256_commits_in_both_gates(world, capsys):
    """Exactly `CARRY_MAX_COMMITS`: 256 commits between are carried, 257 are a stale base. Kills a bound of
    255 or 257 and a bound that is not read (the number is the rule: it is not an option)."""
    w = world
    assert mergegate.CARRY_MAX_COMMITS == 256
    tree = _git(w.repo, "rev-parse", "HEAD^{tree}")
    parent = w.base
    for i in range(256):
        parent = _git(w.repo, "commit-tree", tree, "-p", parent, "-m", f"c{i}")
    _git(w.repo, "update-ref", "refs/heads/main", parent)
    case = _plain(w)
    rc, out, err, receipt = _run_product(w, capsys, case)
    assert (rc, err) == (0, "")
    assert len(_row(_load(receipt), "a")["carried_forward"]["commits"]) == 256
    assert _run_tool(w, case)[0] == 0
    parent = _git(w.repo, "commit-tree", tree, "-p", parent, "-m", "c256")
    _git(w.repo, "update-ref", "refs/heads/main", parent)
    rc, out, err, receipt = _run_product(w, capsys, _plain(w), suite=False)
    assert (rc, err) == (1, "")
    body = _load(receipt)
    assert [p["code"] for p in body["problems"]] == ["STALE_BASE", "STALE_BASE"]
    assert all("more than 256 commits" in p["detail"] for p in body["problems"])
    assert _run_tool(w, _plain(w), suite=False)[0] == 1


def test_a_path_list_above_the_byte_bound_is_not_compared_in_either_gate(world, capsys, monkeypatch):
    """The lists git prints for the candidate and for the range are read whole and compared; above
    `CARRY_MAX_BYTES` (4 MiB) they are not, and the review stays stale with the reason. Kills a list that
    is compared whatever its size, in the product gate and in the tool."""
    w = world
    assert mergegate.CARRY_MAX_BYTES == 4 * 1024 * 1024
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    head = _git(w.repo, "rev-parse", "HEAD")
    with monkeypatch.context() as patched:
        patched.setattr(mergegate, "CARRY_MAX_BYTES", 8)       # the candidate's two paths are 19 bytes
        rc, out, err, receipt = _run_product(w, capsys, _plain(w))
    assert (rc, err) == (1, "")
    assert all("larger than 8 bytes" in p["detail"] for p in _load(receipt)["problems"])
    spec = importlib.util.spec_from_file_location("cross_check_merge_gate", TOOL)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool.REPO = str(w.repo)
    assert tool.CARRY_MAX_BYTES == 4 * 1024 * 1024 and tool.CARRY_MAX_COMMITS == 256
    assert tool.carried(w.cand, head, w.base)[0] is not None
    tool.CARRY_MAX_BYTES = 8
    record, why = tool.carried(w.cand, head, w.base)
    assert record is None and "too many" in why


def test_the_byte_bound_is_exact_a_list_of_that_size_is_compared_one_byte_more_is_not(world, monkeypatch):
    """The candidate's path list is `a.txt\\0docs/spec.md\\0`, 19 bytes; the range's is `other.txt\\0`, 10.
    At a bound of 19 both are compared (the review carries), at 18 the candidate's is refused. Pins the
    side of the bound -- `>`, not `>=` -- in the product and in the tool."""
    w = world
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    head = _git(w.repo, "rev-parse", "HEAD")
    spec = importlib.util.spec_from_file_location("cross_check_merge_gate_exact", TOOL)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool.REPO = str(w.repo)
    for size, expected in ((19, True), (18, False)):
        with monkeypatch.context() as patched:
            patched.setattr(mergegate, "CARRY_MAX_BYTES", size)
            product = mergegate.measure_carry(str(w.repo.resolve()), w.base, head, w.cand)
        assert product.carried is expected, (size, product.detail)
        tool.CARRY_MAX_BYTES = size
        assert (tool.carried(w.cand, head, w.base)[0] is not None) is expected


@pytest.mark.parametrize("form", ["abbreviated", "upper-case", "branch-name", "head", "tag-object", "blob",
                                  "not-hex", "missing-object", "nul", "fullwidth-digits", "padded-to-64"])
def test_only_a_full_lower_case_commit_id_can_be_carried(world, capsys, form):
    """The footer BASE is the one thing taken from the report, and it is taken as a sha or not at all:
    a ref name, a shortened or upper-case id, a tag object, a blob or an object the repository does not
    have is a stale base with the reason, never a lookup that could resolve to something else."""
    w = world
    _git(w.repo, "tag", "-a", "-m", "t", "reviewed", w.base)
    value = {
        "abbreviated": w.base[:12], "upper-case": w.base.upper(), "branch-name": "main", "head": "HEAD",
        "tag-object": _git(w.repo, "rev-parse", "reviewed"),
        "blob": _git(w.repo, "rev-parse", f"{w.base}:{SPEC}"),
        "not-hex": "z" * 40, "missing-object": "4" * 40, "nul": w.base + "\x00",
        "fullwidth-digits": "\uff10" * 40, "padded-to-64": w.base + "0" * 24}[form]
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    case = _stale(w, "", bound=value)
    rc, out, err, receipt = _run_product(w, capsys, case)
    assert (rc, err) == (1, "")
    body = _load(receipt)
    assert [_row(body, r)["problems"] for r in ("a", "b")] == [["STALE_BASE"], ["STALE_BASE"]]
    assert all("not a full object id" in p["detail"] or "not a commit" in p["detail"]
               for p in body["problems"]), body["problems"]
    assert all("carried_forward" not in row for row in body["reviews"])
    assert _run_tool(w, case)[0] == 1


@pytest.mark.parametrize("over", [{"VERDICT": "RELEASE_NO"}, {"BLOCKING_OPEN": "1"},
                                  {"COMPLETENESS": "PARTIAL"}, {"CANDIDATE": "1" * 40},
                                  {"SPEC_SHA256": "3" * 64}])
def test_a_carry_never_hides_another_problem_of_the_review(world, capsys, over):
    """(4) of the rule: the review must be otherwise valid exactly as today. One report is carryable and
    clean, the other has a problem of its own and a carryable base: the gate says no, names the problem,
    and does not measure or record a carry for that report. Kills a carry attempted whenever the base
    differs (it would drop the other problems too)."""
    w = world
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    good = _bound(w, w.cand, w.spec_sha, w.base)[0]
    bad = f"b={_report(w, f'b{next(_names)}', BASE=w.base, **over)}"
    rc, out, err, receipt = _gate(w, capsys, [good, bad], base="HEAD")
    assert (rc, err) == (1, "")
    body = _load(receipt)
    assert body["aggregate"] == "NOT_MERGE_ELIGIBLE"
    assert "carried_forward" in _row(body, "a")
    assert "carried_forward" not in _row(body, "b")
    assert "STALE_BASE" in _row(body, "b")["problems"] and len(_row(body, "b")["problems"]) >= 2
    assert _row(body, "b")["status"] in ("RELEASE_NO", "NO_VERDICT")
    assert "Carried forward mechanically: b" not in out


def test_an_unstable_or_colliding_review_is_not_made_eligible_by_a_carry(world, capsys):
    """The stable-time window and uniqueness are judged exactly as today, whatever the base."""
    w = world
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    young = f"b={_report(w, f'young{next(_names)}', age=5, BASE=w.base)}"
    rc, out, err, receipt = _gate(w, capsys, [_bound(w, w.cand, w.spec_sha, w.base)[0], young], base="HEAD")
    assert (rc, err) == (1, "")
    assert "REPORT_UNSTABLE" in _row(_load(receipt), "b")["problems"]
    assert "Carried forward mechanically: b" not in out
    one = _report(w, f"same{next(_names)}", BASE=w.base)
    rc, out, err, receipt = _gate(w, capsys, [f"a={one}", f"b={one}"], base="HEAD")
    assert (rc, err) == (1, "")
    assert "COLLISION" in _row(_load(receipt), "b")["problems"]


@pytest.mark.parametrize("failing", ["rev-parse", "merge-base", "rev-list", "diff-tree", "log", "merge-tree"])
def test_a_git_failure_in_the_measurement_is_a_stale_base_and_never_a_crash(world, capsys, monkeypatch,
                                                                            failing):
    """Unreadable git state during the measurement: the review cannot be carried and the answer is the one
    it was before the rule (exit 1, STALE_BASE, with the reason), not exit 2 and not eligible."""
    w = world
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    case = _plain(w)                 # no suite receipt is built here: the carry itself must fail
    real = subprocess.run
    seen: list[str] = []

    def broken(argv, *args, **kwargs):
        if isinstance(argv, list) and argv[:1] == ["git"] and "-C" in argv:
            hit = (f"{w.base}^{{commit}}" in argv) if failing == "rev-parse" else (failing in argv)
            if hit:
                seen.append(failing)
                if failing == "merge-tree":
                    raise OSError("git vanished")
                return subprocess.CompletedProcess(argv, 128, b"", b"fatal: simulated failure\n")
        return real(argv, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(subprocess, "run", broken)
        rc, out, err, receipt = _run_product(w, capsys, case, suite=False)
    assert seen, "the failing command was never run"
    assert (rc, err) == (1, "")
    body = _load(receipt)
    assert [_row(body, r)["problems"] for r in ("a", "b")] == [["STALE_BASE"], ["STALE_BASE"]]
    assert all("; not carried forward: " in p["detail"] for p in body["problems"])
    assert all("carried_forward" not in row for row in body["reviews"])


def _contents(snapshot: dict) -> dict:
    """A snapshot without the modification time of objects: git refreshes the time of an object it would
    write again and finds already there (it is in the repository's object store the private one reads);
    no object is added, removed or changed."""
    return {name: (stat[0], stat[2]) if name.startswith(".git/objects/") else stat
            for name, stat in snapshot.items()}


def test_a_carry_writes_nothing_into_the_repository_and_leaves_no_scratch(world, capsys, monkeypatch):
    """The merge-tree runs in a private object store: the repository (its objects included) and the work
    tree are as they were, and the store is gone when the measurement ends. Kills a merge-tree that writes
    into the repository's own object directory, and a store that is not removed."""
    w = world
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    scratch = w.tmp / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    case = _plain(w)
    suite = _suite(w, case)                 # the test's own merge-tree writes objects: before the snapshot
    before = _snapshot(w.repo)
    rc, out, err, receipt = _run_product(w, capsys, case, suite=suite)
    assert (rc, err) == (0, "")
    assert _contents(_snapshot(w.repo)) == _contents(before)
    assert list(scratch.iterdir()) == []


def test_every_git_command_of_a_carry_is_hardened_and_the_set_is_the_measurement(world, capsys, monkeypatch):
    """The commands of a carried check, in order: the three that resolve the inputs, then one carry
    measurement (shared by both reviews, which are bound to the same base): eight commands, then the
    base's tree for the suite receipt (the merged tree is the carry's). Each runs under the gate's
    hardening with its range spelled in full shas; the range log is not told what a submodule is; the
    merge-tree runs in its own object store and with directory renames reported as conflicts."""
    w = world
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    head = _git(w.repo, "rev-parse", "HEAD")
    calls = []
    real = subprocess.run

    def spy(argv, *args, **kwargs):
        calls.append((list(argv), kwargs))
        return real(argv, *args, **kwargs)

    case = _plain(w)
    suite = _suite(w, case)
    with monkeypatch.context() as patched:
        patched.setattr(subprocess, "run", spy)
        rc = _run_product(w, capsys, case, suite=suite)[0]
    assert rc == 0

    def verb(argv):
        rest = argv[argv.index("-C") + 2:]
        skip = 0
        for i, token in enumerate(rest):
            if skip:
                skip -= 1
            elif token == "-c":
                skip = 1
            else:
                return token
        raise AssertionError(argv)

    assert [verb(argv) for argv, _ in calls] == [
        "rev-parse", "rev-parse", "cat-file",                 # candidate, base, specification
        "rev-parse", "merge-base", "rev-list", "merge-base",  # the reviewed base, ancestry, range, merge base
        "diff-tree", "log", "rev-parse", "merge-tree",        # candidate paths, touched paths, objects, merge
        "rev-parse"]                                          # the base's tree, for the suite receipt
    for argv, kwargs in calls:
        joined = " ".join(argv)
        assert argv[0] == "git"
        for setting in ("core.fsmonitor=false", "core.hooksPath=/dev/null",
                        "advice.graftFileDeprecated=false"):
            assert f"-c {setting}" in joined, (setting, argv)
        assert argv[argv.index("-C") + 1] == str(w.repo.resolve())
        assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["capture_output"] is True
        assert kwargs["timeout"] > 0
        assert "GIT_DIR" not in kwargs["env"] and kwargs["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
    ranged = [a for argv, _ in calls for a in argv if ".." in a]
    assert ranged == [f"{w.base}..{head}", f"{w.base}..{head}"]
    log_argv = next(argv for argv, _ in calls if "log" in argv)
    assert "diff.ignoreSubmodules=none" in log_argv and "-m" in log_argv and "--no-renames" in log_argv
    merge_tree = calls[-2]
    assert "merge.directoryRenames=conflict" in merge_tree[0] and "--write-tree" in merge_tree[0]
    assert merge_tree[0][-2:] == [head, w.cand]
    env = merge_tree[1]["env"]
    assert env["GIT_OBJECT_DIRECTORY"] != env["GIT_ALTERNATE_OBJECT_DIRECTORIES"]
    assert env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] == f'"{(w.repo / ".git" / "objects").resolve()}"'
    assert not Path(env["GIT_OBJECT_DIRECTORY"]).exists()


def test_a_repository_under_a_path_with_a_colon_is_measured_in_both_gates(world, capsys):
    """`GIT_ALTERNATE_OBJECT_DIRECTORIES` is a list split at ':'; the repository's object directory is
    quoted for git, so such a repository is carried (not read as two directories and refused)."""
    w = world
    moved = w.tmp / 're:po "q"'
    os.rename(w.repo, moved)
    w.repo = moved
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    case = _plain(w)
    rc, out, err, receipt = _run_product(w, capsys, case)
    assert (rc, err) == (0, "")
    tool_rc, text, tool = _run_tool(w, case)
    assert tool_rc == 0, text
    assert _row(tool, "a")["carried_forward"] == _row(_load(receipt), "a")["carried_forward"]


def test_verify_decides_the_carry_again_now(world, capsys):
    """`verify` re-decides: a carried receipt verifies while the base can still carry the reviews, names the
    stale base once a later commit touches a candidate path, and is still tied to the tip with `--base`."""
    w = world
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    case = _plain(w)
    rc, out, err, receipt = _run_product(w, capsys, case)
    assert (rc, err) == (0, "")
    assert _verify(w, capsys, receipt)[0] == 0
    assert _verify(w, capsys, receipt, "--candidate", w.cand, "--base", "HEAD")[0] == 0
    _commit(w, {"other.txt": "2\n"}, "unrelated again")
    rc, out, err = _verify(w, capsys, receipt)       # still carryable at the new tip, but the suite was
    assert (rc, err) == (1, "")                      # not run on this merge: the receipt no longer fits
    assert "SUITE_STALE_BASE" in out and "SUITE_MERGED_TREE_MISMATCH" in out and "  a: STALE_BASE: " not in out
    rc, out, err = _verify(w, capsys, receipt, "--base", "HEAD")       # and it is for the old tip
    assert (rc, err) == (1, "") and "BASE_MISMATCH" in out
    _commit(w, {"a.txt": "main\n"}, "a candidate path moves")
    rc, out, err = _verify(w, capsys, receipt)
    assert (rc, err) == (1, "")
    assert "  a: STALE_BASE: " in out and "; not carried forward: " in out and "'a.txt'" in out


def test_verify_refuses_a_suite_receipt_that_changed_after_the_gate_ran(world, capsys):
    """The suite receipt is digested like a report: the same content in other bytes, or other content, is
    `SUITE_RECEIPT_CHANGED` at `verify`, whatever else is true. Kills a verify that does not compare it."""
    w = world
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    case = _plain(w)
    suite = _suite(w, case)
    rc, out, err, receipt = _run_product(w, capsys, case, suite=suite)
    assert (rc, err) == (0, "")
    body = _load(receipt)
    assert body["suite"]["arg"] == suite and len(body["suite"]["sha256"]) == 64
    assert _verify(w, capsys, receipt, "--candidate", w.cand, "--base", "HEAD")[0] == 0
    Path(suite).write_text(Path(suite).read_text(encoding="utf-8") + "\n", encoding="utf-8")
    rc, out, err = _verify(w, capsys, receipt)
    assert (rc, err) == (1, "") and "SUITE_RECEIPT_CHANGED" in out


# --- the cross-check tool agrees ---------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_cross_check_tool_agrees_with_the_product_gate(world, capsys, name):
    """`tools/merge_gate.py` (an independent implementation of the same rule) and `hoh merge-gate check` say
    the same on every case, and when the answer is yes they record the same carry. Kills a rule that was
    changed in one gate only."""
    w = world
    case = CASES[name](w)
    product_rc, _out, _err, product_receipt = _run_product(w, capsys, case)
    tool_rc, text, tool = _run_tool(w, case)
    assert product_rc == tool_rc == (0 if case.eligible else 1), text
    product = _load(product_receipt)
    assert tool["aggregate"] == product["aggregate"]
    assert (tool["candidate"], tool["base"]) == (product["candidate"], product["base"])
    for review in ("a", "b"):
        carried = _row(tool, review).get("carried_forward")
        assert carried == _row(product, review).get("carried_forward")
        assert (carried is not None) == case.eligible
    if not case.eligible:
        assert "not carried" in text and "(stale" in text and case.why in text, text


def test_the_cross_check_tool_still_judges_what_it_judged(world):
    """The tool's own rules are unchanged by the carry: a review bound to the current base is eligible
    without any carry record, and the other stale bindings (candidate, specification) still refuse."""
    w = world
    plain = Case(w.cand, w.base, _bound(w, w.cand, w.spec_sha, w.base), True)
    rc, text, body = _run_tool(w, plain)
    assert rc == 0 and body["aggregate"] == "MERGE_ELIGIBLE"
    assert all("carried_forward" not in row for row in body["reviews"])
    for over in ({"CANDIDATE": "1" * 40}, {"SPEC_SHA256": "3" * 64}, {"VERDICT": "RELEASE_NO"}):
        bad = Case(w.cand, w.base, _bound(w, w.cand, w.spec_sha, w.base, **over), False)
        assert _run_tool(w, bad)[0] == 1
    # a review with a problem of its own is not measured, even where the base would carry
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    mixed = Case(w.cand, "HEAD", [_bound(w, w.cand, w.spec_sha, w.base)[0],
                                  _bound(w, w.cand, w.spec_sha, w.base, VERDICT="RELEASE_NO")[1]], False)
    rc, text, body = _run_tool(w, mixed)
    assert rc == 1
    assert "carried_forward" in _row(body, "a") and "carried_forward" not in _row(body, "b")


# --- leg 3 of the rule: the suite receipt --------------------------------------------------------


_X = "tests/test_x.py::test_a"
_Y = "tests/test_x.py::test_b"


def _suite_forms(w: World, case: Case) -> dict[str, tuple[str | None, set[str]]]:
    """Every way a suite receipt can be right or wrong for a carried review, with the problem codes the
    gate must give: `(path or None for no flag, codes)`; empty codes = the review stands."""
    good = _suite_body(w, case)
    other_tree = _tree(w, w.base)                     # a tree of this repository that is not the merge's

    def write(content: str | bytes, name: str = "suite") -> str:
        path = w.tmp / f"{name}-{next(_names)}.json"
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return str(path)

    def one(**over) -> str:
        return write(json.dumps({**good, **over}))

    counts = good["counts"]
    unreadable = {"SUITE_RECEIPT_UNREADABLE"}
    big = w.tmp / f"suite-big-{next(_names)}.json"
    big.write_bytes(b" " * (8 * 1024 * 1024 + 1))
    adir = w.tmp / f"suite-dir-{next(_names)}"
    adir.mkdir()
    text = json.dumps(good)
    return {
        "bound": (one(), set()),
        "accepted-failure": (one(new_failures=[_X], accepted=[_X], counts={**counts, "new": 1}), set()),
        "accepted-but-not-new": (one(accepted=[_X]), set()),
        "missing-flag": (None, {"SUITE_RECEIPT_MISSING"}),
        "missing-file": (str(w.tmp / "no-such-suite.json"), {"SUITE_RECEIPT_MISSING"}),
        "stale-merged-tree": (one(merged_tree=other_tree), {"SUITE_MERGED_TREE_MISMATCH"}),
        "stale-base-tree": (one(base_tree=good["merged_tree"]), {"SUITE_BASE_TREE_MISMATCH"}),
        "stale-base": (one(base=w.base), {"SUITE_STALE_BASE"}),
        "stale-candidate": (one(candidate=w.base), {"SUITE_STALE_CANDIDATE"}),
        "new-failure": (one(new_failures=[_X], counts={**counts, "new": 1}), {"SUITE_NEW_FAILURES"}),
        "partly-accepted": (one(new_failures=[_X, _Y], accepted=[_X], counts={**counts, "new": 2}),
                            {"SUITE_NEW_FAILURES"}),
        "not-json": (write("{not json"), unreadable),
        "json-array": (write("[]"), unreadable),
        "non-utf8": (write(b"\xff\xfe{"), unreadable),
        "unknown-schema": (one(schema="hoh-suite-receipt/2"), unreadable),
        "missing-key": (write(json.dumps({k: v for k, v in good.items() if k != "accepted"})), unreadable),
        "extra-key": (one(extra=1), unreadable),
        "duplicate-key": (write(text[:-1] + ', "base": "' + w.base + '"}'), unreadable),
        "short-id": (one(merged_tree=good["merged_tree"][:12]), unreadable),
        "counts-inconsistent": (one(new_failures=[_X]), unreadable),
        "counts-negative": (one(counts={**counts, "base_failed": -1}), unreadable),
        "counts-contradict-the-list": (one(counts={"base_failed": 3, "candidate_failed": 500, "new": 0}),
                                       unreadable),
        "counts-new-failure-without-surplus": (one(new_failures=[_X], accepted=[_X],
                                                   counts={"base_failed": 3, "candidate_failed": 3,
                                                           "new": 1}), set()),
        "counts-bool": (one(counts={**counts, "new": False}), unreadable),
        "duplicate-test-id": (one(new_failures=[_X, _X], accepted=[_X], counts={**counts, "new": 2}),
                              unreadable),
        "empty-test-id": (one(new_failures=[""], accepted=[""], counts={**counts, "new": 1}), unreadable),
        "control-char-in-test-id": (one(accepted=["a\nb"]), unreadable),
        "bad-stamp": (one(measured_at="yesterday"), unreadable),
        "oversize": (str(big), unreadable),
        "directory": (str(adir), unreadable),
    }


_SUITE_FORMS = ["bound", "accepted-failure", "accepted-but-not-new", "missing-flag", "missing-file",
                "stale-merged-tree", "stale-base-tree", "stale-base", "stale-candidate", "new-failure",
                "partly-accepted", "not-json", "json-array", "non-utf8", "unknown-schema", "missing-key",
                "extra-key", "duplicate-key", "short-id", "counts-inconsistent", "counts-negative",
                "counts-bool", "counts-contradict-the-list", "counts-new-failure-without-surplus", "duplicate-test-id", "empty-test-id", "control-char-in-test-id", "bad-stamp",
                "oversize", "directory"]


@pytest.mark.parametrize("form", _SUITE_FORMS)
def test_a_carried_review_stands_only_with_a_suite_receipt_bound_to_the_merge(world, capsys, form):
    """Leg 3: a review carried forward from an older base is valid only together with a suite receipt for
    the merge being judged. Each form isolates one condition (the codes are exact): no receipt, a missing,
    unreadable or damaged one, another base, another candidate, another base tree, another merged tree, a new
    failure nobody accepted. Kills: the merged tree not compared, the base tree not compared, the base or the
    candidate not compared, the new failures ignored, the accepted list ignored, a missing receipt allowed."""
    w = world
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    case = _plain(w)
    arg, codes = _suite_forms(w, case)[form]
    extra = ["--suite-receipt", arg] if arg else []
    rc, out, err, receipt = _gate(w, capsys, case.reviews, *extra, candidate=case.candidate, base="HEAD")
    assert err == ""
    body = _load(receipt)
    assert all(_row(body, r)["status"] == "RELEASE_YES" and "carried_forward" in _row(body, r)
               for r in ("a", "b"))
    assert {p["code"] for p in body["problems"]} == codes, body["problems"]
    if not codes:
        assert rc == 0 and body["aggregate"] == "MERGE_ELIGIBLE", out
        suite = body["suite"]
        assert suite["problems"] == [] and suite["receipt"]["merged_tree"] == suite["actual_merged_tree"]
        assert suite["receipt"]["base_tree"] == suite["actual_base_tree"] == _tree(w, "HEAD")
        assert any(ln.startswith("Suite receipt: ") for ln in out.splitlines())
    else:
        assert rc == 1 and body["aggregate"] == "NOT_MERGE_ELIGIBLE"
        assert ("suite" in body) is (arg is not None)
        for code in codes:
            assert f"gate: {code}: " in out


@pytest.mark.parametrize("form", _SUITE_FORMS)
def test_the_cross_check_tool_judges_the_suite_receipt_like_the_product(world, form):
    """The tool's own suite check says the same (eligible or not, and the same code names)."""
    w = world
    _commit(w, {"other.txt": "1\n"}, "unrelated")
    case = _plain(w)
    arg, codes = _suite_forms(w, case)[form]
    rc, text, body = _run_tool(w, case, suite=arg or False)
    assert rc == (0 if not codes else 1), text
    for code in codes:
        assert code in text, text
    if not codes:
        assert body["suite"]["receipt"]["merged_tree"] == body["suite"]["actual_merged_tree"]


def test_a_suite_receipt_measured_at_an_older_head_does_not_carry_a_review(world, capsys):
    """The slip leg 3 closes: the suite file is real, and for an older HEAD. Three codes, nothing carries."""
    w = world
    _commit(w, {"other.txt": "1\n"}, "unrelated 1")
    suite = _suite(w, _plain(w))                         # measured here
    _commit(w, {"other2.txt": "2\n"}, "unrelated 2")    # HEAD moves again; the review still carries
    case = _plain(w)
    rc, out, err, receipt = _gate(w, capsys, case.reviews, "--suite-receipt", suite,
                                  candidate=case.candidate, base="HEAD")
    assert (rc, err) == (1, "")
    body = _load(receipt)
    assert {p["code"] for p in body["problems"]} == {"SUITE_STALE_BASE", "SUITE_BASE_TREE_MISMATCH",
                                                     "SUITE_MERGED_TREE_MISMATCH"}
    assert all("carried_forward" in _row(body, r) for r in ("a", "b"))
    tool_rc, text, _tool = _run_tool(w, case, suite=suite)
    assert tool_rc == 1 and "SUITE_STALE_BASE" in text and "SUITE_MERGED_TREE_MISMATCH" in text


def test_a_review_at_the_current_base_needs_no_suite_receipt_but_a_given_one_is_judged(world, capsys):
    """The suite receipt is required exactly when a review was carried. A review bound to the current base
    is decided as before (no key); a receipt that is given is judged all the same."""
    w = world
    plain = Case(w.cand, w.base, _bound(w, w.cand, w.spec_sha, w.base), True)
    rc, out, err, receipt = _run_product(w, capsys, plain, suite=False)
    assert (rc, err) == (0, "") and "suite" not in _load(receipt)
    good = _suite(w, plain)
    rc, out, err, receipt = _run_product(w, capsys, plain, suite=good)
    assert (rc, err) == (0, "") and _load(receipt)["suite"]["problems"] == []
    bad = _suite(w, plain, new_failures=[_X], counts={"base_failed": 0, "candidate_failed": 1, "new": 1})
    plain2 = Case(w.cand, w.base, _bound(w, w.cand, w.spec_sha, w.base), True)
    rc, out, err, receipt = _run_product(w, capsys, plain2, suite=bad)
    assert (rc, err) == (1, "")
    assert [p["code"] for p in _load(receipt)["problems"]] == ["SUITE_NEW_FAILURES"]
    assert "carried_forward" not in _row(_load(receipt), "a")
