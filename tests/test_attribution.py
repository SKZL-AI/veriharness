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
    alle = at.commits_since(WURZEL, ledger["anchor"])
    gesehen: dict[str, str] = {}
    for e in ledger["entries"]:
        for sha in at._shas(WURZEL, e, []):
            assert sha not in gesehen, f"{sha[:12]} claimed twice"
            gesehen[sha] = e["id"]
    # HEAD may be unattributed, and exactly it: a commit cannot name its own
    # sha, so its entry is written in the next commit. One is the bounded price
    # of closed ranges; two is the gap this check exists for.
    offen = [c for c in alle if c not in gesehen]
    assert offen in ([], [alle[-1]]), (
        "commits after the anchor with no entry: "
        + ", ".join(c[:12] for c in offen))


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

def test_a_trailing_ledger_only_commit_is_tolerated_and_anything_else_is_not():
    """O169: with only the tip tolerated, this check had no reachable green.

    The commit that writes an entry cannot name its own sha, so it is written
    in the next commit. That commit is the tip and is tolerated -- until a
    further commit displaces it, at which point it is a gap, and closing it
    needs another commit, which is then the tip. A release that needs both an
    attribution commit and a commit after it can therefore never be green.

    The widening is exactly one shape: a trailing commit that touched
    `dogfood/ATTRIBUTION.json` and nothing else. That is the ledger recording
    itself, checked against git rather than against a commit message, because
    a commit that changed no code, no document and no evidence cannot be work
    this ledger is failing to account for.

    Both halves are pinned here. The negative control is the one that matters:
    a trailing commit touching anything besides the ledger must still be a gap
    once it is no longer the tip, or the widening swallowed the check.
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

        ledger = {"anchor": anker, "entries": [
            {"id": "e", "category": "MAIN_ORCHESTRATOR_DIRECT",
             "is_development_node": False, "commits": [arbeit],
             "why_not_the_product": "fixture"},
        ]}
        bericht = at.pruefe(repo, ledger)
        # `more work` is the tip and tolerated; the ledger-only commit before
        # it is tolerated by the new rule; `work plus ledger` is named.
        assert not any("belong to no entry" in p for p in bericht["problems"]), (
            bericht["problems"])

        # Negative control: leave `work plus ledger` unnamed. It touched a.txt
        # as well, so the widening must not reach it and this must be a gap.
        bericht = at.pruefe(repo, {"anchor": anker, "entries": []})
        assert any("belong to no entry" in p for p in bericht["problems"]), (
            "a trailing commit that touched more than the ledger was excused, "
            "so the widening swallowed the check it was supposed to narrow"
        )
