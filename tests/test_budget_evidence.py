"""Does `tools/budget_evidence.py` measure the budget, or describe it?

The tool exists because campaign v2's cost figure was a constant written
beside the result, so the first thing to establish about the tool is that it
is not the same shape of thing one level up. These tests therefore attack the
tool: they hand it a build with enforcement removed, a measurement with a
control missing, and a measurement whose keys are of the wrong type, and
require it to say NOT_VERIFIED in each case.

The expensive controls (two fixture runs and a subprocess) are exercised once,
in `test_the_controls_pass_on_this_build`. Everything else works on the
measured dict, which is the part a reader would otherwise have to trust.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HOH = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "budget_evidence", HOH / "tools" / "budget_evidence.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["budget_evidence"] = mod
    spec.loader.exec_module(mod)
    return mod


be = _load()


def _green() -> dict:
    """A measurement in which every control holds."""
    return {
        "ceilings": [9, 8, 4],
        "ceiling": 9,
        "dispatches_reaching_the_provider": 9,
        "runs": [{"ceiling": 9, "provider_calls": 9},
                 {"ceiling": 8, "provider_calls": 8},
                 {"ceiling": 4, "provider_calls": 4}],
        "every_ceiling_was_reachable": True,
        "no_run_exceeded_its_ceiling": True,
        "the_product_refused_a_dispatch": True,
        "counter_persisted_before_each_call": True,
        "the_counter_matches_what_the_provider_received": True,
        "measured_follows_the_ceiling": True,
        "telemetry_agrees_with_the_counter": True,
        "a_restart_does_not_refund": True,
        "the_next_run_is_started_with_the_remainder": True,
        "spent_budget_refuses_the_next_run": True,
        "the_refusal_is_recorded_as_a_budget": True,
    }


def test_the_green_measurement_is_the_only_one_that_passes():
    ok, open_ = be.verdict(_green())
    assert ok and not open_


@pytest.mark.parametrize("key", sorted(be.CONTROLS.values()))
def test_every_control_can_fail_the_verdict_on_its_own(key):
    """No control is decorative.

    A suite where one red control is outvoted by the others is a suite that
    reports an average, and the properties here are not averageable: a budget
    that cannot be reached and a budget that cannot be exceeded are different
    broken instruments, and either one invalidates the campaign.
    """
    m = _green()
    m[key] = False
    ok, open_ = be.verdict(m)
    assert not ok
    assert any(key in o for o in open_)


def test_a_missing_key_is_not_read_as_false_or_as_true():
    m = _green()
    del m["counter_persisted_before_each_call"]
    ok, open_ = be.verdict(m)
    assert not ok
    assert "counter_persisted_before_each_call was not measured" in open_


def test_a_key_of_the_wrong_type_is_refused():
    """`"yes"` is truthy, and that is exactly how an asserted figure passes."""
    m = _green()
    m["a_restart_does_not_refund"] = "yes"
    ok, open_ = be.verdict(m)
    assert not ok
    assert any("not a bool" in o for o in open_)


def test_an_overrun_in_any_run_fails_even_with_every_flag_green():
    """The flags are derived; the raw numbers get their own clause, for
    **every** run.

    `verdikt` compared only the first run's calls against the ceiling, so a
    mutant whose second run made six calls under a ceiling of four had
    nothing comparing them.
    """
    m = _green()
    m["runs"][1]["provider_calls"] = 18
    ok, open_ = be.verdict(m)
    assert not ok
    assert any("reached the provider under a ceiling of 8" in o for o in open_)


def test_a_ceiling_set_that_cannot_force_the_refusal_is_refused():
    """The finding that made the previous version worthless at its own ceiling.

    Nine is three iterations of three roles, so the fixture stops on an
    iteration boundary and the per-dispatch refusal is never reached. A build
    with that refusal deleted passed seven of eight controls.
    """
    with pytest.raises(SystemExit, match="multiple of"):
        be.measure_(ceilings=(9, 6))
    with pytest.raises(SystemExit, match="different"):
        be.measure_(ceilings=(8, 8))


def test_without_the_falsifier_the_verdict_cannot_be_verified(tmp_path,
                                                              monkeypatch):
    """The point of the switch, and the same rule `confinement_evidence.py`
    holds itself to: a control suite that was not falsified reports what it
    was built to report."""
    monkeypatch.setattr(be, "measure_", lambda **kw: _green())
    target = tmp_path / "b.json"
    rc = be.main(["--no-falsifier", "--out", str(target)])
    assert rc == 1
    import json

    d = json.loads(target.read_text())
    assert d["budget_enforcement"] == "NOT_VERIFIED"
    assert "the falsifiers were not run" in d["open"]


def test_the_controls_pass_on_this_build():
    """The measurement itself, at small ceilings to keep it quick.

    Two ceilings are required by K6 -- a capped constant cannot produce both
    -- and at least one must not be a multiple of three, or the per-dispatch
    refusal is never reached.
    """
    m = be.measure_(ceilings=(4, 2))
    ok, open_ = be.verdict(m)
    assert ok, open_
    assert m["the_product_refused_a_dispatch"]
    # The reading that motivated `provider_calls`: a telemetry line is not a
    # provider call. The refused dispatch writes a line and costs nothing.
    assert m["lines_differ_from_calls_somewhere"]
    assert m["refusal_classes"] == ["BUDGET_EXHAUSTED"]


@pytest.mark.parametrize("art", be.FALSIFIERS)
def test_each_falsifier_is_detected(art):
    """Both shapes of a lost enforcement, run for real.

    The narrow one is the one that broke the previous version: delete only the
    per-dispatch check and leave the counter, the persist and the
    iteration-level check in place. Seven of eight controls stayed green and
    the tool still called the falsifier detected, because it asked whether the
    fixture overran rather than whether the controls went red.
    """
    f = be.falsifier(art, (4, 2))
    assert f["ran"]
    assert f["detected"], f
    assert f["verdict_on_the_mutated_build"] == "NOT_VERIFIED"
    assert f["controls_that_went_red"]
    assert not f["missed"]
