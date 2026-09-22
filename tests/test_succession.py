"""The succession capsule: does verifying it actually check anything?

A handover that cannot be checked is a document, and this project has one of
those already. Every case here drives `check` against a capsule that has been
made wrong in one specific way, and requires the verifier to name it. The
positive control runs first and matters just as much: a verifier that reported
drift for everything would pass every negative case below while telling a
successor nothing.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "succession_probe", ROOT / "tools" / "succession.py")
su = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("succession_probe", _spec.loader and "succession_probe")
_spec.loader.exec_module(su)

#: A value no derivation can produce, used wherever a case has to make one
#: field disagree with the tree. An earlier draft wrote a plausible value
#: instead -- `verdict: "no"` -- and the case went green on a tree whose board
#: already said `no`, i.e. the mutation mutated nothing. A negative control
#: must differ from the truth by construction, not by assumption about it.
MUTATED = "not-a-value-this-tree-can-derive"


def _capsule(tmp: Path) -> dict:
    """A capsule written from this tree, which must verify against it."""
    out = tmp / "SUCCESSION.json"
    assert su.write_capsule(None, out) == 0
    return json.loads(out.read_text())


def _verdict(capsule: dict, field: str) -> tuple[str, str]:
    for f, v, detail in su.check(capsule, None):
        if f == field:
            return v, detail
    raise AssertionError(f"no row for {field}")


def test_a_capsule_written_from_this_tree_verifies_against_it(tmp_path):
    """The positive control, and it is not a formality: every negative case
    below would also pass against a verifier that always reports drift."""
    capsule = _capsule(tmp_path)
    rows = su.check(capsule, None)
    bad = [(f, v, d) for f, v, d in rows if v == su.DRIFT]
    assert not bad, bad
    assert {f for f, _, _ in rows} >= {
        "internal_commit", "board", "sync", "release", "runs",
        "open_findings", "plan"}


def test_each_field_is_actually_re_derived(tmp_path):
    """One wrong field at a time, and the verifier must name that field.

    Written as a table because the failure that matters is a field nobody
    checks: if a row were dropped from `check`, its case here goes red.
    """
    capsule = _capsule(tmp_path)
    baseline = {f: v for f, v, _ in su.check(capsule, None)}

    cases = [
        ("board", lambda k: k["board"].update({"verdict": MUTATED})),
        ("sync", lambda k: k["sync"].update({"public_commit": "0" * 40})),
        ("release",
         lambda k: k["release_invariants"].update({"tag_object": "0" * 40})),
        ("runs", lambda k: k["runs"].update({f"{MUTATED}/ACTIVE": 99})),
    ]
    exercised = []
    for field, break_it in cases:
        if baseline[field] == su.GAP:
            # O185. This file ships in the export, and the export carries no
            # ledger, no run tree and no sync record. A field this tree cannot
            # derive is one whose wrong value nothing here could notice, so the
            # case does not run -- and says so by not being counted, rather
            # than passing as if it had.
            continue
        k = json.loads(json.dumps(capsule))
        before = json.dumps(k, sort_keys=True)
        break_it(k)
        # The mutation has to have changed something, or the case proves
        # nothing about the verifier.
        assert json.dumps(k, sort_keys=True) != before, field
        verdict, detail = _verdict(k, field)
        assert verdict == su.DRIFT, f"{field}: {verdict} -- {detail}"
        exercised.append(field)

    # And the skip must never swallow the whole table: `board` and `release`
    # are derivable from any tree this file can be in, published or not.
    assert set(exercised) >= {"board", "release"}, exercised


def test_a_closed_or_a_new_finding_is_named_in_both_directions(tmp_path):
    """A successor needs to know both that something was closed and that
    something appeared. Reporting only one direction would let a new open
    item arrive unannounced."""
    capsule = _capsule(tmp_path)
    if not capsule["open_findings"]:
        import pytest
        pytest.skip("this tree records no tracked items, so neither direction "
                    "can be exercised against it")

    fewer = json.loads(json.dumps(capsule))
    removed = fewer["open_findings"].pop(0)["finding"]
    verdict, detail = _verdict(fewer, "open_findings")
    assert verdict == su.DRIFT and f"new: {removed}" in detail

    more = json.loads(json.dumps(capsule))
    more["open_findings"].append(
        {"finding": "O999", "priority": "high", "heading": "invented"})
    verdict, detail = _verdict(more, "open_findings")
    assert verdict == su.DRIFT and "closed since: O999" in detail


def test_the_ledger_is_read_in_both_languages(tmp_path):
    """O182. The ledger up to O181 marks an open item in German and from O182
    in English. A parser that recognised only the new form would report 13
    items closed on the day the wording changed -- a rewrite of the record
    dressed as progress."""
    assert su._TRACKED.search("ein getracktes Todo bleibt offen")
    assert su._TRACKED.search("left as a tracked todo")
    assert su._PRIORITY.search("Priorität: hoch").group(1) == "hoch"
    assert su._PRIORITY.search("Priority: high").group(1) == "high"
    # And it does not match a section that declares nothing.
    assert not su._TRACKED.search("fixed in this commit, nothing outstanding")


def test_the_priority_is_read_in_both_forms_and_only_when_it_is_one():
    """O183. The ledger writes `Priorität: mittel` and also `Getracktes Todo,
    Prioritaet mittel` across a line break. Requiring the colon read five
    items as `unstated`; accepting any following word would read `Priorität
    dieser Sache` as a priority of `dieser`."""
    assert su._PRIORITY.search("Priorität: mittel").group(1) == "mittel"
    assert su._PRIORITY.search("Prioritaet\nmittel** —").group(1) == "mittel"
    assert su._PRIORITY.search("Priority: high").group(1) == "high"
    assert su._PRIORITY.search("Priorität dieser Sache ist offen") is None


def test_a_closed_section_leaves_the_open_list():
    """O183. Corrections in the ledger are additions, so a finished item still
    carries the line that opened it. Without a closing marker the open list
    could only grow, and a successor would inherit items closed months ago."""
    opened = "**Getracktes Todo, Priorität: hoch.** something to do later"
    assert su._TRACKED.search(opened) and not su._CLOSED.search(opened)
    closed = opened + "\n\n**Closed 2026-09-21:** merged as o175w-i2.\n"
    assert su._TRACKED.search(closed), "the opening line is never removed"
    assert su._CLOSED.search(closed), "and the closure is what ends it"
    # A closure is a line of its own, not a word in a sentence about closing.
    assert not su._CLOSED.search(opened + "\nwe could have closed this in June")


def test_a_capsule_from_another_history_is_a_gap_not_a_pass(tmp_path):
    """A commit this repository does not carry means the capsule describes a
    line this tree is not on. That is not the capsule being wrong, and it is
    certainly not a pass."""
    capsule = _capsule(tmp_path)
    capsule["internal_commit"] = "b" * 40
    verdict, detail = _verdict(capsule, "internal_commit")
    assert verdict == su.GAP
    assert "not in this repository" in detail


def test_a_moved_head_is_drift_and_says_how_far(tmp_path):
    """The capsule is not wrong about the past; it is stale about the present,
    and a successor deserves to know by how much.

    O187. This read `HEAD~1` and expected drift, which stopped being true the
    day O184 made a one-commit distance correct when that commit is the
    capsule's own -- and the last commit of a phase close is exactly that. Two
    commits back is drift under the narrow rule for any history, so the case
    no longer depends on what the tip happens to contain.
    """
    capsule = _capsule(tmp_path)
    grandparent = su._git("rev-parse", "HEAD~2")
    if not grandparent:
        import pytest
        pytest.skip("fewer than two parent commits in this checkout")
    capsule["internal_commit"] = grandparent
    verdict, detail = _verdict(capsule, "internal_commit")
    assert verdict == su.DRIFT and "moved 2 commit(s)" in detail


def test_the_commit_that_records_the_capsule_is_not_drift(tmp_path):
    """O184. A capsule cannot name the commit that carries it -- that commit
    does not exist until the capsule is written. So the successor checking out
    HEAD would read DRIFTED on a tree where nothing has drifted, and learn to
    read past it. Exercised in a scratch repository, because the case is about
    what a commit contains and this session's history is not a fixture."""
    repo = tmp_path / "repo"
    (repo / "dogfood/succession").mkdir(parents=True)
    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a],
                              capture_output=True, text=True, check=True)
    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (repo / "README.md").write_text("one\n")
    git("add", "-A"); git("commit", "-qm", "first")
    base = git("rev-parse", "HEAD").stdout.strip()

    capsule_rel = "dogfood/succession/SUCCESSION.json"
    (repo / capsule_rel).write_text("{}\n")
    git("add", "-A"); git("commit", "-qm", "capsule")
    head = git("rev-parse", "HEAD").stdout.strip()

    old_hoh, old_capsule = su.HOH, su.CAPSULE
    try:
        su.HOH, su.CAPSULE = repo, repo / capsule_rel
        assert su._only_the_capsule_moved(base, head) is True

        # The negative control, and it is the whole point: the same shape of
        # commit carrying one more file is drift.
        (repo / "other.md").write_text("two\n")
        (repo / capsule_rel).write_text("{ }\n")
        git("add", "-A"); git("commit", "-qm", "capsule and more")
        assert su._only_the_capsule_moved(head, git("rev-parse", "HEAD").stdout.strip()) is False
        # And two commits of distance is drift even if both are the capsule.
        assert su._only_the_capsule_moved(base, git("rev-parse", "HEAD").stdout.strip()) is False
    finally:
        su.HOH, su.CAPSULE = old_hoh, old_capsule


def test_an_unreachable_plan_is_a_gap_and_claims_nothing(tmp_path):
    """On another machine the plan is simply not there. Silence about it would
    read as agreement; a gap says what it is."""
    capsule = _capsule(tmp_path)
    capsule["plan"] = {"root": str(tmp_path / "does-not-exist"), "files": {}}
    verdict, detail = _verdict(capsule, "plan")
    assert verdict == su.GAP
    assert su.PLAN_ENV in detail


def test_a_changed_plan_file_is_drift(tmp_path):
    """The plan is pinned by digest so a successor reads the same one. A file
    that moved must be named, not averaged into a count."""
    source = tmp_path / "plan"
    source.mkdir()
    (source / "01_goal.md").write_text("one\n")
    capsule = _capsule(tmp_path)
    capsule["plan"] = su._plan(source)
    assert _verdict(capsule, "plan")[0] == su.OK

    (source / "01_goal.md").write_text("two\n")
    verdict, detail = _verdict(capsule, "plan")
    assert verdict == su.DRIFT and "01_goal.md" in detail


def test_the_tool_carries_no_path_from_the_machine_that_wrote_it():
    """O175. This ships in the export, and the plan lives on the operator's
    machine; a default here would be one layout baked into every copy."""
    source = (ROOT / "tools" / "succession.py").read_text()
    for pattern in ("/home/", "/Users/", "/root/"):
        assert pattern not in source
    assert su.PLAN_ENV in source


def test_the_verifier_exits_three_states_not_two(tmp_path):
    """0 verified, 1 drifted, 3 nothing checkable -- as real CLI invocations,
    because O171 was a defect in a return path that no in-process test saw."""
    # Written *with* a plan, so every field is checkable and 0 is reachable.
    # Without one the plan row is an environment gap and the run exits 3 --
    # which is correct, and was this test's first wrong expectation.
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "01_goal.md").write_text("one\n")
    out = tmp_path / "SUCCESSION.json"
    assert su.write_capsule(plan, out) == 0
    tool = str(ROOT / "tools" / "succession.py")

    def run(capsule_path):
        return subprocess.run(
            [sys.executable, tool, "verify", "--capsule", str(capsule_path)],
            capture_output=True, text=True, cwd=str(ROOT),
            env={"PATH": __import__("os").environ.get("PATH", "")}).returncode

    # What 0 means here depends on the tree: in this repository every field is
    # derivable, in the published export four of them are not. So the expected
    # code is read from the per-field rows -- a different level from the exit
    # mapping this test is about, which is where O171 actually was.
    rows = su.check(json.loads(out.read_text()), plan)
    assert not [f for f, v, _ in rows if v == su.DRIFT], rows
    assert run(out) == (3 if any(v == su.GAP for _, v, _ in rows) else 0)

    # And the gap is its own code, not folded into either of the others.
    without_plan = tmp_path / "without_plan.json"
    assert su.write_capsule(None, without_plan) == 0
    assert run(without_plan) == 3

    capsule = json.loads(out.read_text())
    drifted = tmp_path / "drift.json"
    capsule["board"]["verdict"] = MUTATED
    drifted.write_text(json.dumps(capsule))
    assert run(drifted) == 1

    missing = tmp_path / "does-not-exist.json"
    assert run(missing) == 2
