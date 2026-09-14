"""Can the telemetry audit tell an unknown from a blank?

`tools/telemetry_audit.py` exists because a record model that refuses to lie
still produces a log of well-typed blanks when nobody passes it anything. The
audit's whole value is the distinction between three states where the obvious
implementation has two, so this file attacks that distinction from both sides:
a legitimate `None` must not be reported as a gap, and a `0` standing in for a
measurement nobody took must be.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "src"))


def _laden():
    spec = importlib.util.spec_from_file_location(
        "telemetry_audit", WURZEL / "tools" / "telemetry_audit.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("telemetry_audit", mod)
    spec.loader.exec_module(mod)
    return mod


ta = _laden()


def satz(**ueber) -> dict:
    """A record with everything a real dispatch could know, filled in."""
    basis = {
        "schema_version": 1, "project_id": "", "node_id": "", "run_id": "r1",
        "iteration": 1, "attempt": 0, "role": "planner",
        "backend": "HerdrDispatcher", "provider": "claude",
        "model": "claude-opus-5", "effort": "NOT_AVAILABLE",
        "started_at": "2026-01-01T00:00:00Z", "ended_at": "2026-01-01T00:01:00Z",
        "wallclock_seconds": 60.0, "outcome": "ok", "failure_class": None,
        "witnessed_trees": 6, "witnessed_listings": 1,
        "detail": "", "retries": 0, "waited_seconds": 0,
        "tokens_in": 100, "tokens_out": 50, "quota_proxy": None,
        "quota_proxy_kind": "", "receipts": 0, "discriminating": 0,
        "artefactual": 0, "repair_of": "", "repair_depth": 0,
    }
    basis.update(ueber)
    return basis


def _felder(saetze: list[dict]) -> dict:
    return ta.pruefe(saetze)


# --------------------------------------------------------------------------- #
# A legitimate unknown is not a gap
# --------------------------------------------------------------------------- #


def test_tokens_the_harness_never_reported_are_not_available_not_missing():
    f = _felder([satz(tokens_in=None, tokens_out=None)])
    assert f["tokens_in"][ta.NOT_AVAILABLE] == 1
    assert f["tokens_in"][ta.MISSING] == 0


def test_a_dispatch_that_needed_no_retry_is_not_a_missing_measurement():
    f = _felder([satz(retries=0, waited_seconds=0)])
    assert f["retries"][ta.MISSING] == 0
    assert f["waited_seconds"][ta.MISSING] == 0


def test_attempt_zero_is_a_real_attempt_number():
    """The planner runs at attempt 0. Flagging that would make the audit noise."""
    f = _felder([satz(attempt=0)])
    assert f["attempt"][ta.MISSING] == 0


def test_a_planner_record_without_receipts_is_not_a_gap():
    f = _felder([satz(role="planner", receipts=0)])
    assert f["receipts"][ta.MISSING] == 0


def test_no_failure_class_on_a_dispatch_that_did_not_fail_is_not_a_gap():
    f = _felder([satz(outcome="ok", failure_class=None)])
    assert f["failure_class"][ta.MISSING] == 0


# --------------------------------------------------------------------------- #
# A blank standing in for a measurement is a gap
# --------------------------------------------------------------------------- #


def test_an_unnamed_provider_is_a_gap():
    f = _felder([satz(provider="")])
    assert f["provider"][ta.MISSING] == 1


def test_an_unnamed_model_is_a_gap_and_not_an_unknown():
    """`DispatchRecord.model` says an empty model is "genuine and unhelpful".

    It is knowable -- somebody chose a harness -- so nobody having asked is a
    gap in the wiring, not the harness declining to answer.
    """
    f = _felder([satz(model="")])
    assert f["model"][ta.MISSING] == 1
    assert f["model"][ta.NOT_AVAILABLE] == 0


def test_no_failure_class_on_a_dispatch_that_failed_is_a_gap():
    f = _felder([satz(outcome="failed", detail="broke", failure_class=None)])
    assert f["failure_class"][ta.MISSING] == 1


def test_zero_receipts_on_the_verifying_dispatch_is_a_gap():
    """The shape of this project's fourth false green, one field wide."""
    f = _felder([satz(role="qa", receipts=0)])
    assert f["receipts"][ta.MISSING] == 1


def test_zero_discriminating_criteria_is_a_real_answer_and_not_a_gap():
    """An iteration where no criterion flipped red to green is a real run.

    Insisting on a non-zero here would flag honest runs, which is why
    `receipts` carries the strictness instead: a verification that executed
    wrote receipts, so a QA dispatch reporting none is unambiguous.
    """
    f = _felder([satz(role="qa", discriminating=0, artefactual=0)])
    assert f["discriminating"][ta.MISSING] == 0
    assert f["artefactual"][ta.MISSING] == 0


# --------------------------------------------------------------------------- #
# Coverage: the shapes a campaign has to have shown at least once
# --------------------------------------------------------------------------- #


def test_a_record_that_predates_the_witness_fields_is_unknown_not_missing():
    """The field's own docstring says `None` is not zero, and the audit agrees.

    Two halves of the same statement in two files is how they drift; this is
    the one that would have contradicted the other on every older line.
    """
    f = _felder([satz(witnessed_trees=None, witnessed_listings=None)])
    assert f["witnessed_trees"][ta.NOT_AVAILABLE] == 1
    assert f["witnessed_trees"][ta.MISSING] == 0


def test_a_condition_may_not_exempt_a_field_by_asking_about_itself():
    """`lambda r: bool(r.get(<the same field>))` can never fire.

    Four entries had that shape: the field was exempt exactly when it was
    empty. An audit whose check cannot fail is a tick-box, which is the thing
    this project rejects one level up.
    """
    import inspect

    for feld, bedingung in ta.BEDINGT.items():
        quelle = inspect.getsource(bedingung)
        assert f'r.get("{feld}")' not in quelle, (
            f"{feld} exempts itself exactly when it is empty")


def test_an_unstated_effort_is_a_gap_now_that_it_is_configurable():
    """`--role-effort` reaches the agent CLI, so an empty field is a gap."""
    f = _felder([satz(effort="")])
    assert f["effort"][ta.MISSING] == 1


def test_a_campaign_without_a_failure_record_has_not_shown_it_can_fail():
    a = ta.abdeckung([satz(role=r) for r in ("planner", "developer", "qa")])
    assert a["failure_record"] is False
    assert a["retry_record"] is False


def test_a_campaign_with_every_shape_covers_them():
    saetze = [
        satz(role="planner"),
        satz(role="developer"),
        satz(role="qa", receipts=2, discriminating=1),
        satz(role="planner", outcome="failed", detail="x",
             failure_class="CONTRACT_INVALID", retries=1, waited_seconds=5),
    ]
    a = ta.abdeckung(saetze)
    assert all(a.values()), [k for k, v in a.items() if not v]


def test_an_unlabelled_proxy_is_reported():
    a = ta.abdeckung([satz(quota_proxy=3.0, quota_proxy_kind="")])
    assert a["quota_proxy_labelled"] is False


# --------------------------------------------------------------------------- #
# The aggregation control, which is the point of the whole module
# --------------------------------------------------------------------------- #


def test_an_unknown_token_count_does_not_become_a_zero_in_the_total():
    k = ta.aggregation_kontrolle([satz(tokens_in=None, tokens_out=None),
                                  satz(tokens_in=10, tokens_out=5)])
    assert k["records_with_unknown_tokens"] == 1
    assert k["careful_total"] is None
    assert k["naive_total"] == 15          # what the wrong answer looks like
    assert k["unknown_did_not_become_zero"] is True


def test_a_complete_set_of_records_does_total():
    k = ta.aggregation_kontrolle([satz(tokens_in=10, tokens_out=5)])
    assert k["records_with_unknown_tokens"] == 0
    assert k["careful_total"] == 15
    assert k["unknown_did_not_become_zero"] is True


def test_the_control_reports_how_many_inputs_it_could_not_use():
    k = ta.aggregation_kontrolle([satz(tokens_in=None, tokens_out=None)] * 3)
    assert k["summary_counts_the_unknowns"] == 3


# --------------------------------------------------------------------------- #
# End to end over a written log
# --------------------------------------------------------------------------- #


@pytest.fixture
def protokoll(tmp_path: Path) -> Path:
    lauf = tmp_path / "r1"
    lauf.mkdir()
    return lauf.parent


def _schreiben(wurzel: Path, saetze: list[dict]) -> None:
    (wurzel / "r1" / "telemetry.jsonl").write_text(
        "".join(json.dumps(s) + "\n" for s in saetze))


def test_a_fully_wired_log_validates(protokoll):
    _schreiben(protokoll, [
        satz(role="planner"),
        satz(role="developer"),
        satz(role="qa", receipts=2, discriminating=1),
        satz(role="planner", outcome="failed", detail="x",
             failure_class="CONTRACT_INVALID", retries=1, waited_seconds=5),
    ])
    assert ta.main(["--run-root", str(protokoll), "--run-id", "r1"]) == 0


def test_a_log_of_well_typed_blanks_does_not_validate(protokoll, capsys):
    _schreiben(protokoll, [
        satz(role="planner", provider="", model=""),
        satz(role="developer", provider="", model=""),
        satz(role="qa", provider="", model="", receipts=0),
    ])
    assert ta.main(["--run-root", str(protokoll), "--run-id", "r1"]) == 1
    aus = capsys.readouterr().out
    assert "provider" in aus
    assert "= no" in aus


def test_receipts_summed_must_agree_with_the_receipt_files(tmp_path):
    """`Summary` sums receipts with no unknown accounting at all.

    Tokens were the only field the control looked at, and a run that wrote two
    receipts while recording none printed "0 receipt(s)" in the line a reader
    quotes -- the fourth false green, one column across. These fields have no
    `None` state to lose, so the check against them is external.
    """
    lauf = tmp_path / "r1"
    (lauf / "receipts").mkdir(parents=True)
    for name in ("K1", "K1-basis"):
        (lauf / "receipts" / f"r1-i1-a1-{name}.json").write_text("{}")

    k = ta.aggregation_kontrolle([satz(role="qa", receipts=0)], lauf)
    assert k["receipt_files_on_disk"] == 2
    assert k["receipts_summed"] == 0
    assert k["receipts_agree_with_the_files"] is False

    k = ta.aggregation_kontrolle([satz(role="qa", receipts=2)], lauf)
    assert k["receipts_agree_with_the_files"] is True


def test_without_a_run_directory_the_receipt_check_says_it_did_not_run(tmp_path):
    """Not `True`. A control that could not run has not passed."""
    k = ta.aggregation_kontrolle([satz(role="qa", receipts=2)], None)
    assert k["receipt_files_on_disk"] is None
    assert k["receipts_agree_with_the_files"] is None


def test_an_uneventful_campaign_is_not_the_same_as_a_defective_one():
    """A run in which nothing failed has not validated the failure record.

    It has not failed the audit either. Rolling the two together would either
    hide a real wiring gap or make an uneventful campaign look broken, so they
    are reported apart -- and "not observed" is still never "passed".
    """
    lauf = pathlib.Path(tempfile.mkdtemp()) / "r1"
    (lauf / "receipts").mkdir(parents=True)
    (lauf / "receipts" / "r1-i1-a1-K1.json").write_text("{}")
    (lauf / "telemetry.jsonl").write_text("".join(
        json.dumps(satz(role=r, receipts=1 if r == "qa" else 0)) + "\n"
        for r in ("planner", "developer", "qa")))

    assert ta.main(["--run-root", str(lauf.parent), "--run-id", "r1"]) == 1
    bericht = json.loads(_json_bericht(lauf.parent, "r1"))
    assert bericht["fields_validated_on_real_dispatches"] == "yes"
    assert bericht["telemetry_validated_on_real_dispatches"] == "no"
    assert bericht["coverage_gaps"] == []
    assert set(bericht["shapes_not_observed"]) == {
        "failure_record", "retry_record", "failure_taxonomy"}


def test_a_real_field_gap_is_not_excused_by_an_uneventful_campaign():
    """The other direction: an empty `provider` is still a gap."""
    lauf = pathlib.Path(tempfile.mkdtemp()) / "r1"
    (lauf / "receipts").mkdir(parents=True)
    (lauf / "receipts" / "r1-i1-a1-K1.json").write_text("{}")
    (lauf / "telemetry.jsonl").write_text("".join(
        json.dumps(satz(role=r, provider="", receipts=1 if r == "qa" else 0)) + "\n"
        for r in ("planner", "developer", "qa")))

    bericht = json.loads(_json_bericht(lauf.parent, "r1"))
    assert bericht["fields_validated_on_real_dispatches"] == "no"
    assert "provider" in bericht["fields_with_gaps"]


def _json_bericht(wurzel, run_id) -> str:
    """Run the audit and capture its JSON, without going through the CLI's
    exit code."""
    import io
    from contextlib import redirect_stdout

    puffer = io.StringIO()
    with redirect_stdout(puffer):
        ta.main(["--run-root", str(wurzel), "--run-id", run_id, "--json"])
    return puffer.getvalue()


def _satz(**kw):
    """A complete-enough record; overrides say what is being tested."""
    basis = {
        "role": "planner", "run_id": "r", "iteration": 1, "attempt": 1,
        "outcome": "ok", "provider": "claude", "model": "NOT_AVAILABLE",
        "effort": "NOT_AVAILABLE", "wallclock_seconds": 1.0, "retries": 0,
        "provider_calls": 1, "witnessed_trees": 8, "witnessed_listings": 1,
    }
    basis.update(kw)
    return basis


def test_a_refused_dispatch_may_report_zero_provider_calls():
    """It reached no provider, so zero is what it cost.

    Judged on a different field than itself: `outcome`. A dispatch that
    answered and claims zero calls is still a gap.
    """
    assert ta._zustand("provider_calls", 0,
                       _satz(outcome="failed", provider_calls=0)) == ta.MEASURED


def test_a_successful_dispatch_reporting_zero_provider_calls_is_still_a_gap():
    """The control. Without it the exemption above would excuse every record."""
    assert ta._zustand("provider_calls", 0,
                       _satz(outcome="ok", provider_calls=0)) == ta.MISSING


def test_a_refused_dispatch_is_not_required_to_carry_a_witness():
    """It never took one, and requiring a number would push the controller
    towards writing coverage for a witness it did not take -- the fabricated
    measurement the record's own docstring forbids."""
    a = ta.abdeckung([_satz(), _satz(outcome="failed", provider_calls=0,
                                     witnessed_trees=None,
                                     witnessed_listings=None,
                                     failure_class="BUDGET_EXHAUSTED")])
    assert a["witness_coverage_recorded"] is True


def test_a_dispatch_that_ran_without_a_witness_is_still_a_coverage_gap():
    """The control for the exemption above."""
    a = ta.abdeckung([_satz(), _satz(provider_calls=1, witnessed_trees=None)])
    assert a["witness_coverage_recorded"] is False


def test_a_record_predating_provider_calls_is_still_asked_for_its_witness():
    """`None` is not `0`: an old record carries no figure at all, and
    exempting it would quietly drop the check for every historical run."""
    a = ta.abdeckung([_satz(), _satz(provider_calls=None, witnessed_trees=None)])
    assert a["witness_coverage_recorded"] is False
