"""Can the attribution ledger claim work the product did not do?

`dogfood/ATTRIBUTION.json` answers an uncomfortable question on purpose: how
much of this phase did the product execute, and how much did the session
writing about the product execute by hand. The number is only worth having if
it cannot be flattered, so this file tries to flatter it.

The finding that prompted these tests: an entry claiming `VERIHARNESS_RUN`
carried a range ending at `HEAD`. That was written for a real reason -- an
entry describing a merge cannot name its own sha -- and then the history moved,
and four later commits the product had nothing to do with were inside it. The
tiling check could not see it, because the range still tiled.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent


def _laden():
    spec = importlib.util.spec_from_file_location(
        "attribution", WURZEL / "tools" / "attribution.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("attribution", mod)
    spec.loader.exec_module(mod)
    return mod


at = _laden()


def _ledger() -> dict:
    return json.loads((WURZEL / "dogfood" / "ATTRIBUTION.json").read_text())


def test_no_entry_claims_a_range_that_ends_at_head():
    """An open range grows with the history and claims what it cannot know."""
    for e in _ledger()["entries"]:
        ziel = str((e.get("range") or {}).get("to", ""))
        assert ziel.upper() not in ("HEAD", "@"), e["id"]


def test_an_open_range_is_reported_as_a_problem(tmp_path):
    bericht = at.pruefe(WURZEL, {
        "anchor": "HEAD",
        "entries": [{
            "id": "offen", "category": "MAIN_ORCHESTRATOR_DIRECT",
            "range": {"from": "HEAD", "to": "HEAD"},
        }],
    })
    assert any("ends at HEAD" in p for p in bericht["problems"])


def _fremde_historie() -> bool:
    """Is this a clone that does not contain the history the ledger describes?

    A published export is a different repository with different commits, so
    the ledger travels with it as a *record* and cannot be verified there.
    Found by the exact-head CI the first time the ledger was published: sixty
    entries reported as naming commits "not after the anchor", when the truth
    was that the anchor is not in that clone either.
    """
    anker = _ledger()["anchor"]
    return not at._anker_vorhanden(WURZEL, anker)


def test_the_real_ledger_still_tiles_the_history():
    if not (WURZEL / ".git").exists():
        pytest.skip("not a git checkout")
    bericht = at.pruefe(WURZEL, _ledger())
    if bericht.get("environment_gap"):
        assert bericht["problems"] == [], (
            "a clone without this history reports the gap, not defects")
        pytest.skip(bericht["environment_gap"])
    assert bericht["problems"] == [], bericht["problems"]


def test_a_clone_without_the_history_reports_a_gap_rather_than_defects():
    """The control for the skip above: the gap has to be *stated*, not
    inferred from an empty problem list."""
    bericht = at.pruefe(WURZEL, {"anchor": "0" * 40, "entries": []})
    assert bericht["problems"] == []
    assert "does not contain the anchor commit" in bericht["environment_gap"]


def test_every_commit_after_the_anchor_belongs_to_exactly_one_entry():
    """Stated as its own test, because it is the denominator."""
    if not (WURZEL / ".git").exists():
        pytest.skip("not a git checkout")
    if _fremde_historie():
        pytest.skip("this clone does not contain the history the ledger describes")
    ledger = _ledger()
    gesehen: dict[str, str] = {}
    for e in ledger["entries"]:
        for sha in at._shas(WURZEL, e, []):
            assert sha not in gesehen, f"{sha[:12]} claimed twice"
            gesehen[sha] = e["id"]
    # The gap question is asked of the tool, not re-implemented here. This
    # test used to carry its own copy of the tolerance rule -- "HEAD and
    # exactly HEAD" -- and when O169 widened that rule by one shape, the tool
    # went green and this test stayed red against the same history. A rule
    # written down twice is a rule that will disagree with itself; the part
    # that belongs here is the one above, which the tool does not check: that
    # no commit is claimed by two entries.
    bericht = at.pruefe(WURZEL, ledger)
    luecken = [p for p in bericht["problems"] if "belong to no entry" in p]
    assert not luecken, "; ".join(luecken)


def test_a_veriharness_claim_must_point_at_a_state_that_exists():
    """Claiming the product did it is free; pointing at what it left is not.

    The **naming** is required everywhere, including in an export: an entry
    with no `project_state` is an unsupported claim wherever it is read. The
    file existing is required only where the evidence lives -- the run trees
    are classified EXCLUDE and a published clone does not carry them, which is
    the export working rather than a missing state.
    """
    fremd = _fremde_historie()
    for e in _ledger()["entries"]:
        if e["category"] != "VERIHARNESS_RUN":
            continue
        zustand = e.get("project_state", "")
        assert zustand, e["id"]
        if fremd:
            continue
        assert (WURZEL / zustand).is_file(), f"{e['id']}: {zustand}"


def test_the_ratio_is_derived_and_not_written_down():
    """A hand-maintained ratio in a document is a ratio that drifts."""
    ledger = _ledger()
    for e in ledger["entries"]:
        assert "commit_count" not in e, e["id"]
    assert "commits_by_category" not in ledger


def test_todays_work_is_not_attributed_to_the_product():
    """The specific claim that was false, pinned so it cannot come back.

    The capability boundary, the confinement measurement and the amendment
    path were written by the main session. An entry saying otherwise would be
    the fake attribution this phase was explicitly told not to produce.
    """
    if not (WURZEL / ".git").exists():
        pytest.skip("not a git checkout")
    ledger = _ledger()
    nach_sha = {}
    for e in ledger["entries"]:
        for sha in at._shas(WURZEL, e, []):
            nach_sha[sha] = e["category"]
    log = subprocess.run(
        ["git", "-C", str(WURZEL), "log", "--format=%H %s", "-20"],
        capture_output=True, text=True).stdout.splitlines()
    for zeile in log:
        sha, betreff = zeile.split(" ", 1)
        if sha not in nach_sha:
            continue
        if any(k in betreff for k in ("O125", "O129", "O132", "O126/O127")):
            assert nach_sha[sha] == "MAIN_ORCHESTRATOR_DIRECT", (
                f"{sha[:12]} ({betreff[:50]}) is attributed to "
                f"{nach_sha[sha]}, and the session wrote it by hand")


def test_two_unattributed_commits_are_still_a_gap():
    """One is the price of a closed range. Two is the thing being prevented."""
    if _fremde_historie():
        pytest.skip("this clone does not contain the history the ledger describes")
    bericht = at.pruefe(WURZEL, {"anchor": "HEAD~3", "entries": []})
    assert any("belong to no entry" in p for p in bericht["problems"])

def test_a_ledger_only_commit_is_tolerated_and_anything_else_is_not():
    """O169: with only the tip tolerated, this check had no reachable green.

    The commit that writes an entry cannot name its own sha, so it is written
    in the next commit. That commit is the tip and is tolerated -- until a
    further commit displaces it, at which point it is a gap, and closing it
    needs another commit, which is then the tip. A release that needs both an
    attribution commit and a commit after it can therefore never be green.

    The widening is exactly one shape: a commit that touched
    `dogfood/ATTRIBUTION.json` and nothing else, wherever it sits. That is the
    ledger recording itself, checked against git rather than against a commit
    message, because a commit that changed no code, no document and no
    evidence cannot be work this ledger is failing to account for.

    Position was tried first -- only a trailing *run* of such commits -- and it
    was wrong within the hour: one attributed commit landing after the
    ledger-only one ends the run, and a content-free commit becomes a gap
    again. What makes it safe is what it touched, not where it is, so this
    pins the ledger-only commit with ordinary work committed after it.

    Both halves are pinned here. The negative control is the one that matters:
    a commit touching anything besides the ledger must still be a gap once it
    is no longer the tip, or the widening swallowed the check.
    """
    import subprocess as _sp
    import tempfile as _tf
    from pathlib import Path as _P

    with _tf.TemporaryDirectory() as tmp:
        repo = _P(tmp) / "r"
        repo.mkdir()

        def g(*a):
            return _sp.run(["git", "-C", str(repo), *a], capture_output=True,
                           text=True, check=True).stdout.strip()

        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@example.invalid")
        g("config", "user.name", "t")
        (repo / "a.txt").write_text("1\n")
        g("add", "-A"); g("commit", "-q", "-m", "anchor")
        anker = g("rev-parse", "HEAD")

        led = repo / "dogfood" / "ATTRIBUTION.json"
        led.parent.mkdir(parents=True)
        led.write_text("{}\n")
        (repo / "a.txt").write_text("2\n")
        g("add", "-A"); g("commit", "-q", "-m", "work plus ledger")
        arbeit = g("rev-parse", "HEAD")

        led.write_text('{"x": 1}\n')
        g("add", "-A"); g("commit", "-q", "-m", "ledger only")

        (repo / "a.txt").write_text("3\n")
        g("add", "-A"); g("commit", "-q", "-m", "more work")
        spaeter = g("rev-parse", "HEAD")

        (repo / "a.txt").write_text("4\n")
        g("add", "-A"); g("commit", "-q", "-m", "tip")

        # `spaeter` is named, so the ledger-only commit is neither the tip nor
        # part of a trailing run of unattributed commits. Under the first
        # version of this rule -- a trailing run -- this fixture went red.
        ledger = {"anchor": anker, "entries": [
            {"id": "e", "category": "MAIN_ORCHESTRATOR_DIRECT",
             "is_development_node": False, "commits": [arbeit, spaeter],
             "why_not_the_product": "fixture"},
        ]}
        bericht = at.pruefe(repo, ledger)
        # `tip` is tolerated as the tip; `more work` and `work plus ledger` are
        # named; the ledger-only commit is buried between named commits and is
        # tolerated on what it touched rather than on where it is.
        assert not any("belong to no entry" in p for p in bericht["problems"]), (
            bericht["problems"])

        # Negative control: leave `work plus ledger` unnamed. It touched a.txt
        # as well, so the widening must not reach it and this must be a gap.
        bericht = at.pruefe(repo, {"anchor": anker, "entries": []})
        assert any("belong to no entry" in p for p in bericht["problems"]), (
            "a trailing commit that touched more than the ledger was excused, "
            "so the widening swallowed the check it was supposed to narrow"
        )


def _fixture_repo(tmp):
    """A throwaway repository with an anchor and two commits after it."""
    import subprocess as _sp
    from pathlib import Path as _P
    repo = _P(tmp) / "r"
    repo.mkdir()

    def g(*a):
        return _sp.run(["git", "-C", str(repo), *a], capture_output=True,
                       text=True, check=True).stdout.strip()

    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@example.invalid")
    g("config", "user.name", "t")
    (repo / "a.txt").write_text("1\n")
    g("add", "-A"); g("commit", "-q", "-m", "anchor")
    anker = g("rev-parse", "HEAD")
    (repo / "a.txt").write_text("2\n")
    g("add", "-A"); g("commit", "-q", "-m", "work")
    arbeit = g("rev-parse", "HEAD")
    (repo / "a.txt").write_text("3\n")
    g("add", "-A"); g("commit", "-q", "-m", "tip")
    return repo, anker, arbeit


FREMD_SHA = "8edb1a2a1af6fe07f2db5bdaedfa6d2e041c289d"


def test_an_entry_for_another_history_is_recorded_and_not_resolved():
    """O172: the distribution work happened in the export repository.

    Its commits are real, reviewed and merged, and not one of their shas is in
    the development history this ledger tiles. Resolving them here would
    report "names a commit that is not after the anchor" -- true, and useless,
    because the sha was never supposed to be in this history. So an entry may
    declare the history it describes, and is then recorded rather than
    resolved.

    The negative control is the half that matters: the same entry *without*
    the marker must still be reported, or the marker is not a declaration but
    a way to smuggle any sha past the check.
    """
    import tempfile as _tf

    with _tf.TemporaryDirectory() as tmp:
        repo, anker, arbeit = _fixture_repo(tmp)
        basis = {"id": "e", "category": "MAIN_ORCHESTRATOR_DIRECT",
                 "is_development_node": False, "commits": [FREMD_SHA],
                 "why_not_the_product": "fixture"}

        mit = at.pruefe(repo, {"anchor": anker, "entries": [
            {**basis, "history": "public-export"},
            {"id": "i", "category": "MAIN_ORCHESTRATOR_DIRECT",
             "is_development_node": False, "commits": [arbeit],
             "why_not_the_product": "fixture"}]})
        assert not mit["problems"], mit["problems"]
        assert mit["other_histories"]["public-export"][
            "commits_by_category"]["MAIN_ORCHESTRATOR_DIRECT"] == 1

        ohne = at.pruefe(repo, {"anchor": anker, "entries": [basis]})
        assert any("not a commit after" in p for p in ohne["problems"]), (
            "an unmarked foreign sha was accepted, so the marker is not doing "
            "the work -- the check would pass for any sha at all"
        )


def test_another_history_never_enters_the_internal_denominator():
    """The ratio is about the history the anchor names.

    Adding commits from another repository to `commits_by_category` would move
    a number without moving anything it measures -- the O140 shape. This pins
    that the internal count is identical with and without the foreign entry,
    and that the foreign commits are reported under their own history instead.
    """
    import tempfile as _tf

    with _tf.TemporaryDirectory() as tmp:
        repo, anker, arbeit = _fixture_repo(tmp)
        intern_nur = {"id": "i", "category": "MAIN_ORCHESTRATOR_DIRECT",
                      "is_development_node": True, "commits": [arbeit],
                      "why_not_the_product": "fixture"}
        fremd = {"id": "e", "category": "MAIN_ORCHESTRATOR_DIRECT",
                 "history": "public-export", "is_development_node": True,
                 "commits": [FREMD_SHA, FREMD_SHA[:-1] + "0"],
                 "why_not_the_product": "fixture"}

        a = at.pruefe(repo, {"anchor": anker, "entries": [intern_nur]})
        b = at.pruefe(repo, {"anchor": anker, "entries": [intern_nur, fremd]})
        assert a["commits_by_category"] == b["commits_by_category"]
        assert a["development_nodes"] == b["development_nodes"], (
            "a foreign-history entry moved the node denominator"
        )
        assert b["other_histories"]["public-export"][
            "commits_by_category"]["MAIN_ORCHESTRATOR_DIRECT"] == 2


def test_a_foreign_history_entry_is_still_checked_on_what_is_checkable():
    """Recorded is not unexamined. Four things are still refused."""
    import tempfile as _tf

    with _tf.TemporaryDirectory() as tmp:
        repo, anker, arbeit = _fixture_repo(tmp)
        # The internal commit needs its own entry, or every case below also
        # reports the gap it leaves -- which would hide what is being tested.
        intern = {"id": "i", "category": "MAIN_ORCHESTRATOR_DIRECT",
                  "is_development_node": False, "commits": [arbeit],
                  "why_not_the_product": "fixture"}

        def probleme(**ueberschreiben):
            e = {"id": "e", "category": "MAIN_ORCHESTRATOR_DIRECT",
                 "history": "public-export", "is_development_node": False,
                 "commits": [FREMD_SHA], "why_not_the_product": "fixture"}
            e.update(ueberschreiben)
            return at.pruefe(
                repo, {"anchor": anker, "entries": [intern, e]})["problems"]

        assert any("unknown history" in p for p in probleme(history="erfunden"))
        assert any("unknown category" in p for p in probleme(category="ERFUNDEN"))
        assert any("names a range" in p
                   for p in probleme(range={"from": "a", "to": "b"}))
        assert any("names no commits" in p for p in probleme(commits=[]))
        assert any("cannot be checked where it is made" in p
                   for p in probleme(category="VERIHARNESS_RUN")), (
            "a foreign-history entry was allowed to claim the product executed "
            "it, in a repository that does not carry the run state to check"
        )
        assert not probleme()


def test_a_missing_anchor_reports_an_environment_gap_instead_of_crashing():
    """O171: the missing-anchor branch returned half a report.

    It carried no `commits_by_category`, no `sentence` and no `ok`, and the CLI
    read all three unconditionally -- so running this in an export clone ended
    in `KeyError: commits_by_category` rather than the environment gap the
    branch exists to state. A gate that crashes where it means to say "I
    cannot check this here" has reported nothing at all.

    The negative control is the key set: if a future edit drops one of them
    again, this fails before the CLI does.
    """
    import tempfile as _tf

    with _tf.TemporaryDirectory() as tmp:
        repo, _, _ = _fixture_repo(tmp)
        bericht = at.pruefe(repo, {"anchor": "0" * 40, "entries": []})
        assert bericht.get("environment_gap")
        for schluessel in ("commits_by_category", "sentence", "ok",
                           "commits_after_anchor", "problems",
                           "other_histories"):
            assert schluessel in bericht, (
                f"{schluessel} missing from the environment-gap report; the "
                "CLI reads it unconditionally and would crash again"
            )
        assert bericht["ok"] is False, (
            "an environment gap reported ok=True would be a pass for a run "
            "that verified nothing"
        )


def test_both_output_modes_carry_the_same_exit_semantics():
    """O171, finished. The repair gave the text mode a third exit code for an
    environment gap and left `--json` returning 0 for the same state -- so the
    machine-readable mode reported as success exactly what the human-readable
    one had been changed to refuse. Found by an independent review.

    Run as real CLI invocations, because the defect was in `main`'s return
    path and a test that called `pruefe` directly would have missed it
    entirely. Three states, both modes, and the environment-gap case uses a
    repository that genuinely lacks the anchor rather than a mocked one.
    """
    import json as _json
    import subprocess as _sp
    import sys as _sys
    import tempfile as _tf
    from pathlib import Path as _P

    werkzeug = str(WURZEL / "tools" / "attribution.py")

    def lauf(*extra):
        return _sp.run([_sys.executable, werkzeug, *extra],
                       capture_output=True, text=True)

    with _tf.TemporaryDirectory() as tmp:
        repo, anker, arbeit = _fixture_repo(tmp)
        ledger = _P(tmp) / "ledger.json"

        # 1. An environment gap: this repository does not carry the anchor.
        ledger.write_text(_json.dumps({"anchor": "0" * 40, "entries": []}))
        for modus in ([], ["--json"]):
            r = lauf("--repo", str(repo), "--ledger", str(ledger), *modus)
            assert r.returncode == 3, (
                f"{modus or ['text']}: an environment gap exited "
                f"{r.returncode}, not 3 -- 0 would report a run that verified "
                "nothing as a pass"
            )

        # 2. A finding: a commit no entry claims.
        ledger.write_text(_json.dumps({"anchor": anker, "entries": []}))
        rc_text = lauf("--repo", str(repo), "--ledger", str(ledger)).returncode
        rc_json = lauf("--repo", str(repo), "--ledger", str(ledger),
                       "--json").returncode
        assert rc_text == rc_json == 1, (f"text={rc_text} json={rc_json}")

        # 3. A verified pass.
        ledger.write_text(_json.dumps({"anchor": anker, "entries": [
            {"id": "i", "category": "MAIN_ORCHESTRATOR_DIRECT",
             "is_development_node": False, "commits": [arbeit],
             "why_not_the_product": "fixture"}]}))
        rc_text = lauf("--repo", str(repo), "--ledger", str(ledger)).returncode
        rc_json = lauf("--repo", str(repo), "--ledger", str(ledger),
                       "--json").returncode
        assert rc_text == rc_json == 0, (f"text={rc_text} json={rc_json}")


def _lauf_zustand(pfad, candidate_id, tree_digest):
    import json as _json
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(_json.dumps({"last_accepted_candidate": {
        "candidate_id": candidate_id, "tree_digest": tree_digest}}))


def test_a_plain_run_entry_must_point_at_the_contents_that_were_accepted():
    """O179: the category exists because neither neighbour was true.

    `VERIHARNESS_RUN` says "a merge the product decided" and wants a
    ProjectState node; `MAIN_ORCHESTRATOR_DIRECT` says the main session wrote
    it. A plain run that the orchestrator merged after review is neither, and
    forcing one of them would have put a false entry in the one file whose
    purpose is not to carry one.

    The bar is raised rather than lowered by the split: this binds the claim
    to **bytes**. The accepted candidate's recorded tree digest has to equal
    the git tree of a commit the entry claims. A lifecycle string says a node
    was merged; this says these contents were the ones accepted.
    """
    import tempfile as _tf
    from pathlib import Path as _P

    with _tf.TemporaryDirectory() as tmp:
        repo, anker, arbeit = _fixture_repo(tmp)
        baum = at._git(repo, "rev-parse", f"{arbeit}^{{tree}}").strip()
        zustand = _P(tmp) / "runs" / "r1" / "state.json"
        _lauf_zustand(repo / "runs/r1/state.json", "r1-i2", baum)

        def probleme(**ueberschreiben):
            e = {"id": "e", "category": "VERIHARNESS_RUN_ORCHESTRATOR_MERGED",
                 "is_development_node": True, "commits": [arbeit],
                 "run_state": "runs/r1/state.json", "candidate_id": "r1-i2",
                 "why_not_the_product": "fixture"}
            e.update(ueberschreiben)
            return at.pruefe(repo, {"anchor": anker, "entries": [e]})["problems"]

        assert not probleme(), probleme()

        # Each way of claiming without pointing.
        assert any("names no run state" in p for p in probleme(run_state=""))
        assert any("not there" in p
                   for p in probleme(run_state="runs/nope/state.json"))
        assert any("no candidate_id" in p for p in probleme(candidate_id=""))
        assert any("records" in p and "as the accepted one" in p
                   for p in probleme(candidate_id="r1-i9")), (
            "an entry could name one run and mean another iteration of it")

        # The negative control that matters: the right run, the right
        # candidate, and contents that are not the ones this entry claims.
        anderer = at._git(repo, "rev-parse", f"{anker}^{{tree}}").strip()
        assert anderer != baum
        _lauf_zustand(repo / "runs/r1/state.json", "r1-i2", anderer)
        assert any("is not the tree of any commit this entry claims" in p
                   for p in probleme()), (
            "the entry was accepted while the run's accepted contents were "
            "somewhere else -- the binding to bytes is not doing its work"
        )
        assert zustand or True  # kept: the fixture path is inside the repo


def test_the_new_category_moves_neither_number_it_sits_between():
    """The ratio is about the full control plane, merge decision included.

    Counting a plain run in the numerator would claim the product decided a
    merge it did not; leaving it only in the denominator would deny that the
    product developed the node. Both are false, so neither moves and a third
    number says what happened.
    """
    import tempfile as _tf

    with _tf.TemporaryDirectory() as tmp:
        repo, anker, arbeit = _fixture_repo(tmp)
        baum = at._git(repo, "rev-parse", f"{arbeit}^{{tree}}").strip()
        _lauf_zustand(repo / "runs/r1/state.json", "r1-i2", baum)

        ohne = at.pruefe(repo, {"anchor": anker, "entries": [
            {"id": "i", "category": "MAIN_ORCHESTRATOR_DIRECT",
             "is_development_node": True, "commits": [arbeit],
             "why_not_the_product": "fixture"}]})
        mit = at.pruefe(repo, {"anchor": anker, "entries": [
            {"id": "i", "category": "VERIHARNESS_RUN_ORCHESTRATOR_MERGED",
             "is_development_node": True, "commits": [arbeit],
             "run_state": "runs/r1/state.json", "candidate_id": "r1-i2",
             "why_not_the_product": "fixture"}]})
        assert mit["nodes_through_the_product"] == \
            ohne["nodes_through_the_product"] == 0, (
            "the new category inflated the ratio's numerator")
        assert mit["development_nodes"] == ohne["development_nodes"] == 1
        assert mit["nodes_developed_by_the_product_merged_by_the_orchestrator"] == 1
        assert ohne[
            "nodes_developed_by_the_product_merged_by_the_orchestrator"] == 0

