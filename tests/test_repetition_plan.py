"""Can the repetition plan be talked into calling a campaign complete?

`docs/BENCHMARK_PROTOCOL.md` was frozen before any arm ran, and it says three
repetitions per cell "where the budget allows". A progress line that counts
fifteen task-arm cells answers a different question, and the difference is the
one a benchmark's author is most tempted to let slide.

So these tests attack the plan from the side it would be convenient to lose
on: an undecidable requirement silently becoming a decided one, a parked file
counting as a second repetition, a figure nobody measured certifying its own
conformance.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent


def _laden():
    spec = importlib.util.spec_from_file_location(
        "repetition_plan", WURZEL / "tools" / "repetition_plan.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("repetition_plan", mod)
    spec.loader.exec_module(mod)
    return mod


rp = _laden()


# --------------------------------------------------------------------------- #
# The requirement itself
# --------------------------------------------------------------------------- #


def test_a_protocol_with_no_repetition_allowance_is_not_determinable():
    """"Where the budget allows" needs a budget that governs repetitions.

    The frozen text defines one, and it governs a single run. A per-run
    ceiling cannot say whether a second run of the same cell is affordable.
    """
    text = (
        "* **Primary: dispatches.** Each arm gets at most **9 role dispatches "
        "per task**.\n"
        "* Repetitions: **3 per (task, arm)** where the budget allows, 1 where "
        "it does not.\n"
        "* The benchmark stops when every cell has been attempted once.\n")
    a = rp.repetition_requirement(text)
    assert a["verdict"] == "NOT_DETERMINABLE"
    assert "single run" in a["reason"]


def test_a_protocol_that_names_a_campaign_budget_is_determinable():
    """The positive control: if the rule were there, it would be read."""
    text = (
        "* **Primary: dispatches.** Each arm gets at most **9 role dispatches "
        "per task**.\n"
        "* The campaign budget is 200 dispatches in total.\n"
        "* Repetitions: **3 per (task, arm)** where the budget allows.\n")
    assert rp.repetition_requirement(text)["verdict"] == "DETERMINABLE"


def test_the_real_protocol_does_not_decide_the_requirement():
    """Measured against the frozen commit, not against the working tree."""
    commit = rp.frozen_protocol_commit()
    if not commit:
        import pytest

        pytest.skip("not a git checkout")
    a = rp.repetition_requirement(rp.frozen_text(commit))
    assert a["verdict"] == "NOT_DETERMINABLE"
    assert a["repetition_bullet"], "the repetition rule was not found at all"
    assert a["stopping_bullet"], "the stopping rule was not found at all"


# --------------------------------------------------------------------------- #
# Counting: the four negative controls
# --------------------------------------------------------------------------- #


def _zelle(tmp: Path, name: str, **felder):
    d = {"task": "t", "arm": "C", "repetition": 1,
         "arm_detail": {"dispatches": 9, "dispatch_budget": 9}}
    d.update(felder)
    (tmp / name).write_text(json.dumps(d))


def test_a_parked_earlier_version_is_not_a_second_repetition(tmp_path,
                                                             monkeypatch):
    quelle = tmp_path / "results-v2"
    quelle.mkdir()
    _zelle(quelle, "t.C.1.json")
    _zelle(quelle, "t.C.1.v20260913T164629Z.json")
    monkeypatch.setitem(rp.KAMPAGNEN_DIR, "v2", quelle)

    gezaehlt, verworfen = rp.zellen("v2")
    assert len(gezaehlt) == 1
    assert any("parked" in z for z in verworfen)


def test_an_attempt_that_did_not_deliver_is_not_a_repetition(tmp_path,
                                                             monkeypatch):
    quelle = tmp_path / "results-v2"
    quelle.mkdir()
    _zelle(quelle, "t.C.1.json")
    _zelle(quelle, "t.C.1.attempt2.v20260913T164629Z.json")
    monkeypatch.setitem(rp.KAMPAGNEN_DIR, "v2", quelle)

    gezaehlt, _ = rp.zellen("v2")
    assert len(gezaehlt) == 1


def test_a_result_file_whose_arm_never_ran_does_not_count(tmp_path,
                                                          monkeypatch):
    """A file exists and the arm recorded nothing: that is not a repetition."""
    quelle = tmp_path / "results-v2"
    quelle.mkdir()
    _zelle(quelle, "t.C.1.json", arm_detail={})
    monkeypatch.setitem(rp.KAMPAGNEN_DIR, "v2", quelle)

    gezaehlt, verworfen = rp.zellen("v2")
    assert gezaehlt == []
    assert any("recorded nothing" in z for z in verworfen)


def test_a_cell_from_another_campaign_is_not_counted(tmp_path, monkeypatch):
    """The directories are the campaigns; a v1 cell cannot fill a v2 slot."""
    v1 = tmp_path / "results"
    v2 = tmp_path / "results-v2"
    v1.mkdir()
    v2.mkdir()
    _zelle(v1, "t.C.2.json", repetition=2)
    monkeypatch.setitem(rp.KAMPAGNEN_DIR, "v1", v1)
    monkeypatch.setitem(rp.KAMPAGNEN_DIR, "v2", v2)

    gezaehlt, _ = rp.zellen("v2")
    assert gezaehlt == []


# --------------------------------------------------------------------------- #
# Budget conformance, which must not certify itself
# --------------------------------------------------------------------------- #


def test_an_asserted_dispatch_figure_cannot_certify_conformance(tmp_path,
                                                                monkeypatch):
    """A cell recorded before the figure was counted carries
    `min(iterations * 3, DISPATCH_BUDGET)`, which cannot exceed 9 by
    construction. Reading that as "within budget" cleared campaign v1 falsely.
    """
    quelle = tmp_path / "results"
    quelle.mkdir()
    (quelle / "t.C.1.json").write_text(json.dumps({
        "task": "t", "arm": "C", "repetition": 1,
        "arm_detail": {"dispatches": 9},          # no dispatch_budget: asserted
    }))
    monkeypatch.setitem(rp.KAMPAGNEN_DIR, "v1", quelle)

    p = rp.plan("v1")
    zelle = next(r for r in p["cells"] if r["task"] == "t" and r["arm"] == "C")
    assert zelle["budget_conformance"] == ["UNKNOWN_FIGURE_WAS_ASSERTED"]
    assert p["budget_rule"] == "NOT_DETERMINABLE"


def _gezaehlte_zelle(quelle, **detail):
    """A result file whose figure was actually counted."""
    d = {"dispatches": 18, "dispatch_budget": 9,
         "dispatch_count": {"provider_calls": 18, "lines": 18,
                            "lines_without_the_figure": 0}}
    d.update(detail)
    (quelle / "t.C.1.json").write_text(json.dumps({
        "task": "t", "arm": "C", "repetition": 1,
        "measured_tree": "/tmp/bm-t-C1-abc", "started_at": 1.0,
        "arm_detail": d,
    }))


def test_a_run_over_budget_that_was_not_stopped_is_a_violation(tmp_path,
                                                               monkeypatch):
    """The frozen protocol: a run that exceeds the budget is stopped and
    recorded as BUDGET_EXHAUSTED."""
    quelle = tmp_path / "results-v2"
    quelle.mkdir()
    _gezaehlte_zelle(quelle, halt="CLOSED")
    monkeypatch.setitem(rp.KAMPAGNEN_DIR, "v2", quelle)

    p = rp.plan("v2")
    assert p["budget_rule"] == "VIOLATED"
    assert "t/C" in p["cells_over_budget_and_not_stopped"]


def test_a_run_over_budget_is_a_violation_even_when_it_was_stopped(
        tmp_path, monkeypatch):
    """Correction provenance (2026-09-14, adversarial review).

    This test used to assert the opposite, and pinned it as intended: an
    overrun that recorded `BUDGET_EXHAUSTED` was excluded from the campaign's
    violation list. The consequence was measured -- a fixture of 45 runs each
    spending 18 against a hard 9, every one of them carrying the word
    BUDGET_EXHAUSTED, certified as `matched_budget_valid = YES` and
    `benchmark_v3 = PASS`. That is campaign v2's exact number with a string
    added to it.

    The reading was right while nothing enforced the budget: then stopping
    *was* the protocol's own consequence being applied. Under an enforced
    budget it is not. Exceeding a ceiling that refuses the call past it means
    the enforcement failed, and a campaign cannot certify its matched budget
    on the strength of a word in its own result file.
    """
    quelle = tmp_path / "results-v2"
    quelle.mkdir()
    _gezaehlte_zelle(quelle, halt="BUDGET_EXHAUSTED")
    monkeypatch.setitem(rp.KAMPAGNEN_DIR, "v2", quelle)

    p = rp.plan("v2")
    assert p["budget_rule"] == "VIOLATED"
    assert "t/C" in p["cells_over_budget_and_not_stopped"]
    zelle = next(r for r in p["cells"]
                 if r["task"] == "t" and r["arm"] == "C")
    assert zelle["budget_conformance"] == ["EXCEEDED_AND_STOPPED"]


def test_a_run_inside_the_budget_whose_figure_was_counted_is_conformant(
        tmp_path, monkeypatch):
    """The positive control. Without it every test above passes on a rule that
    calls everything a violation."""
    quelle = tmp_path / "results-v2"
    quelle.mkdir()
    _gezaehlte_zelle(quelle, halt="CLOSED", dispatches=7,
           dispatch_count={"provider_calls": 7, "lines": 8,
                           "lines_without_the_figure": 0})
    monkeypatch.setitem(rp.KAMPAGNEN_DIR, "v2", quelle)

    p = rp.plan("v2")
    assert p["budget_rule"] == "ENFORCED"
    assert p["cells_over_budget_and_not_stopped"] == []


def test_a_figure_nothing_counted_is_unknown_however_the_record_is_shaped(
        tmp_path, monkeypatch):
    """The guard keyed on the *presence of a key*, not on a measurement.

    Every cell a current benchmark writes carries `dispatch_budget`, so the
    check that caught campaign v1's asserted figures was inert for every
    future campaign by construction -- and arm A's constant 1 would have
    certified as measured.
    """
    quelle = tmp_path / "results-v2"
    quelle.mkdir()
    _gezaehlte_zelle(quelle, halt="CLOSED", dispatches=7, dispatch_count=None)
    monkeypatch.setitem(rp.KAMPAGNEN_DIR, "v2", quelle)

    p = rp.plan("v2")
    assert p["budget_rule"] == "NOT_DETERMINABLE"
    assert "t/C" in p["cells_whose_spend_is_unknown"]


def test_a_log_line_that_could_not_answer_makes_the_figure_unknown(
        tmp_path, monkeypatch):
    """A telemetry line written before `provider_calls` existed cannot say
    what it cost, and reading it as one call is how the asserted figure got
    in."""
    quelle = tmp_path / "results-v2"
    quelle.mkdir()
    _gezaehlte_zelle(quelle, halt="CLOSED", dispatches=7,
           dispatch_count={"provider_calls": 7, "lines": 9,
                           "lines_without_the_figure": 2})
    monkeypatch.setitem(rp.KAMPAGNEN_DIR, "v2", quelle)

    assert rp.plan("v2")["budget_rule"] == "NOT_DETERMINABLE"


def test_a_crashed_repetition_is_not_counted_as_one(tmp_path, monkeypatch):
    """A campaign in which every cell crashed reported itself complete.

    `zellen()` counted a file as a repetition on the strength of `arm_detail`
    being non-empty, and an arm whose single provider call raised still
    writes `{dispatches: 1, error: ...}`.
    """
    quelle = tmp_path / "results-v2"
    quelle.mkdir()
    # A declared task, so the cell has a row whether or not it was counted.
    (quelle / "to_roman.A.1.json").write_text(json.dumps({
        "task": "to_roman", "arm": "A", "repetition": 1,
        "harness_error": "TimeoutExpired: the arm never answered",
        "arm_detail": {"dispatches": 1, "dispatch_budget": 9},
    }))
    (quelle / "to_roman.B.1.json").write_text(json.dumps({
        "task": "to_roman", "arm": "B", "repetition": 1,
        "arm_detail": {"dispatches": 1, "dispatch_budget": 9,
                       "error": "provider unavailable"},
    }))
    monkeypatch.setitem(rp.KAMPAGNEN_DIR, "v2", quelle)

    p = rp.plan("v2")
    assert "to_roman/A" in p["cells_with_no_repetition"]
    assert "to_roman/B" in p["cells_with_no_repetition"]
    assert any("the instrument raised" in x for x in p["files_not_counted"])
    assert any("provider call raised" in x for x in p["files_not_counted"])


def test_copies_of_one_run_are_not_three_repetitions(tmp_path, monkeypatch):
    """`len(reps)` counted distinct labels, and a label is written into the
    file by whoever wrote the file."""
    quelle = tmp_path / "results-v2"
    quelle.mkdir()
    for rep in (1, 2, 3):
        (quelle / f"t.C.{rep}.json").write_text(json.dumps({
            "task": "t", "arm": "C", "repetition": rep,
            "measured_tree": "/tmp/bm-t-C1-abc", "started_at": 1.0,
            "arm_detail": {"dispatches": 7, "dispatch_budget": 9,
                           "dispatch_count": {"provider_calls": 7, "lines": 7,
                                              "lines_without_the_figure": 0}},
        }))
    monkeypatch.setitem(rp.KAMPAGNEN_DIR, "v2", quelle)

    p = rp.plan("v2")
    zelle = next(r for r in p["cells"]
                 if r["task"] == "t" and r["arm"] == "C")
    assert zelle["completed_repetitions"] == 3, "the labels are still counted"
    assert zelle["repetitions_sharing_a_run_identity"], (
        "three files, one run, and nothing said so")
    assert "t/C" in p["cells_whose_repetitions_share_a_run"]


def test_the_budget_is_read_from_the_protocol_and_not_typed_here(tmp_path,
                                                                 monkeypatch):
    """`9` was a literal in the plan while the protocol carried its own `9`,
    and nothing compared the two -- a constant beside a result, one level up
    from the one this project was already caught with."""
    assert rp.per_run_budget(
        "    dispatch_budget       = 20 per cell, hard, enforced\n") == 20
    assert rp.per_run_budget(
        "task**. B's three roles over three iterations exhausts it exactly;\n"
        "* **Primary: dispatches.** Each arm gets at most **9 role dispatches "
        "per task**.\n") == 9
    assert rp.per_run_budget("nothing about a budget here") is None


def test_a_pre_registered_campaign_states_its_own_repetition_count():
    """v3 must not be judged by v1's undecidable bullet.

    `frozen_protocol_commit` returns the commit that *added* the protocol for
    v1 and v2, which is right: they ran under the design frozen there. A
    campaign that pre-registers itself names its own commit instead. Without
    that, a v3 which ran all three repetitions of all fifteen cells would
    still report NOT_DETERMINABLE -- the wrong answer about a campaign whose
    own frozen text says `3 per (task, arm), without exception`.
    """
    text = (
        "    repetitions           = 3 per (task, arm), without exception\n"
        "    planned_runs          = 5 x 3 x 3 = 45\n"
    )
    r = rp.repetition_requirement(text)
    assert r["verdict"] == "DETERMINABLE"
    assert r["required_repetitions"] == 3


def test_a_qualified_repetition_count_is_still_not_determinable():
    """"3 where the budget allows" is the shape that was undecidable, and a
    pre-registration that reintroduces a condition reintroduces it."""
    text = "    repetitions           = 3 per (task, arm) where the budget allows\n"
    r = rp.repetition_requirement(text)
    assert r["verdict"] == "NOT_DETERMINABLE"
    assert r["required_repetitions"] is None


def test_the_frozen_commit_for_a_campaign_comes_from_its_registration(tmp_path,
                                                                      monkeypatch):
    monkeypatch.setattr(rp, "HOH", tmp_path)
    ziel = tmp_path / "docs" / "benchmarks" / "v3"
    ziel.mkdir(parents=True)
    (ziel / "PREREGISTRATION.json").write_text(
        json.dumps({"benchmark_v3_protocol_commit": "f" * 40}), encoding="utf-8")
    assert rp.frozen_protocol_commit("v3") == "f" * 40


def test_a_campaign_without_a_registration_falls_back_to_the_adding_commit():
    """v1 and v2 have no registration and must keep the commit they ran under.

    Silently reading them against a newer text would be judging a finished
    campaign by a rule written afterwards.

    The previous version of this test asserted
    `frozen_protocol_commit("v2") in ("", frozen_protocol_commit("v2"))`, a
    tautology that could not fail -- and it was the only test covering the
    fallback path. Caught by an adversarial review.
    """
    hinzugefuegt = rp.frozen_protocol_commit("v2")
    assert len(hinzugefuegt) == 40, "not a commit id"
    # The fallback really is the commit that *added* the protocol, and that
    # commit's text is the undecidable one.
    assert rp.repetition_requirement(
        rp.frozen_text(hinzugefuegt))["verdict"] == "NOT_DETERMINABLE"
    # And it is not simply HEAD: the working tree's text is decidable, so a
    # fallback that returned HEAD would have judged v2 by today's rule.
    import subprocess

    head = subprocess.run(["git", "-C", str(rp.HOH), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    assert hinzugefuegt != head


def test_the_required_count_is_not_a_constant_in_this_file():
    """A `3` typed into the plan would be the cost constant one level up:
    right today, unverifiable, and wrong the moment a campaign declares
    something else."""
    text = (
        "    repetitions           = 5 per (task, arm), without exception\n"
    )
    assert rp.repetition_requirement(text)["required_repetitions"] == 5
