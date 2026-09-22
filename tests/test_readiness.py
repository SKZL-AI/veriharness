"""Can the readiness verdict be flattered?

`tools/readiness.py` answers one question -- is this ready, and if not, what is
missing -- and that is the question a project is most tempted to answer by
typing a number above a table. So the verdict is a conjunction of rows, each
row names the command that produced it, and a row that could not be evaluated
is `NOT_RUN`.

`NOT_RUN` never counting as a pass is the rule the rest of this project applies
to a criterion that did not execute. These tests make sure it holds here too,
where it would be most convenient to let it slide.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from conftest import (
    RUN_EVIDENCE_V2,
    RUN_EVIDENCE_V3,
    needs_evidence,
)

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "readiness", ROOT / "tools" / "readiness.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("readiness", mod)
    spec.loader.exec_module(mod)
    return mod


rd = _load()


def _row(name="x", state=rd.PASS, advisory=False):
    return rd.Row(name=name, state=state, value_="-", command="-",
                    advisory=advisory)


def test_all_rows_passing_is_the_only_way_to_pass():
    stand, open_ = rd.verdict([_row("a"), _row("b")])
    assert stand == rd.PASS
    assert open_ == []


def test_a_row_that_did_not_run_is_not_a_pass():
    """The rule this project applies to a criterion that did not execute."""
    stand, open_ = rd.verdict([_row("a"), _row("b", rd.NOT_RUN)])
    assert stand == rd.FAIL
    assert open_ == ["b"]


def test_a_failing_row_blocks_and_is_named():
    stand, open_ = rd.verdict([_row("a"), _row("b", rd.FAIL)])
    assert stand == rd.FAIL
    assert open_ == ["b"]


def test_an_advisory_row_does_not_block_but_is_still_shown():
    """A tracked limitation is not a broken gate.

    It is marked per row rather than decided at the bottom, so a reader can
    disagree with one row without discarding the verdict.
    """
    rows = [_row("a"), _row("b", rd.FAIL, advisory=True)]
    stand, open_ = rd.verdict(rows)
    assert stand == rd.PASS
    assert open_ == []
    text = rd.markdown(rows, "abc1234")
    assert "advisory" in text
    assert "`b`" in text


def test_the_document_states_the_verdict_it_derived():
    rows = [_row("a"), _row("b", rd.FAIL)]
    text = rd.markdown(rows, "abc1234")
    assert "TECHNICALLY_STABLE_READY = no" in text
    assert "Open, and each one blocking: b." in text

    text = rd.markdown([_row("a")], "abc1234")
    assert "TECHNICALLY_STABLE_READY = yes" in text


def test_every_row_names_the_command_that_produced_it():
    """A row without a command is a number somebody typed."""
    text = rd.markdown([_row("a")], "abc1234")
    assert "| command |" in text


def test_the_document_says_what_it_does_not_decide():
    """Publishing is the captain's, and the tool says so rather than implying
    a green table is an approval."""
    text = rd.markdown([_row("a")], "abc1234")
    assert "What this does not decide" in text
    assert "captain" in text


def test_a_stale_export_manifest_is_not_reported_as_zero_references():
    """A count taken against a stale manifest is a zero that means
    "not measured" -- the shape this project refuses everywhere else."""
    import inspect

    source = inspect.getsource(rd.row_export)
    assert "stale_" in source
    assert "not measured" in source


def test_the_dangling_reference_count_counts_findings_not_its_own_summary():
    """O165: the row counted every output line mentioning "U2b".

    `tools/export_manifest.py check` prints its own OK summary as "... passes
    U2b + the leak scan", so a run that found nothing reported
    "1 dangling reference(s)". The number was labelled as findings and was in
    fact one line of prose about there being none -- the same shape as a
    constant reported as a measurement (O140). A green export must report
    zero.

    Negative control: the pre-fix expression is applied to the same text and
    must produce the wrong answer, so this test would have caught it.
    """
    green = (
        "OK: EXPORT_MANIFEST.json matches a fresh derivation and passes U2b + "
        "the leak scan (804 entries, 10 acknowledged reference(s) in 3 document(s))"
    )
    red = (
        "FAIL: U2b: paper/X.md references dogfood/Y.md -- target is EXCLUDE\n"
        "ACKNOWLEDGED: paper/Z.md references dogfood/W.md -- target is EXCLUDE; reason\n"
        + green
    )

    def moment(out):
        return len([z for z in out.splitlines() if z.startswith("FAIL: U2b:")])

    def before(out):
        return len([z for z in out.splitlines() if "U2b" in z])

    assert moment(green) == 0, "a green export must report no dangling references"
    assert moment(red) == 1, "one unacknowledged reference must be counted once"
    assert before(green) == 1, (
        "the negative control no longer reproduces the defect, so this test "
        "would not have caught it"
    )


def test_the_acknowledged_references_are_reported_separately():
    """A reference that was decided is a different state from one nobody
    looked at, and the row must not collapse them into one number."""
    import inspect

    source = inspect.getsource(rd.row_export)
    assert "ACKNOWLEDGED:" in source
    assert "unacknowledged dangling reference(s)" in source
    assert "acknowledged, " in source


def test_the_written_document_carries_every_row_the_tool_produces():
    """A row that quietly stops being generated is a condition nobody checks.

    The document is not regenerated here -- that would run every gate, which
    is the point of it being an explicit command -- so what is checked is the
    cheap half: the set of row names in the file against the set the tool
    defines.
    """
    import inspect

    import pytest

    target = ROOT / "docs" / "READINESS.md"
    if not target.is_file():
        pytest.skip("no readiness document in this checkout")

    source = inspect.getsource(rd.row_list)
    functions = [n for n in dir(rd) if n.startswith("row_")]
    used = [n for n in functions if n + "(" in source]
    assert len(used) == len(functions), (
        "a row function exists and is not in the table: "
        + ", ".join(sorted(set(functions) - set(used))))

    text = target.read_text()
    for name in ("tests", "lint", "claims", "union_invariants", "meta_evidence",
                 "planner_capability_boundary", "telemetry_on_real_dispatches",
                 "benchmark_v2_historical", "benchmark_v3",
                 "benchmark_v3_preregistration", "budget_enforcement",
                 "export_manifest", "clean_install",
                 "attribution", "evidence_index", "paper_audit", "external_ci",
                 "routing"):
        assert f"| `{name}` |" in text, f"{name} is missing from the document"


def _plan(**kw):
    """A repetition plan with every cell complete and nothing wrong."""
    cells = [{"task": f"t{i}", "arm": a, "required_repetitions": 3,
               "completed_repetitions": 3}
              for i in range(5) for a in "ABC"]
    p = {"cells": cells, "cells_with_no_repetition": [],
         "cells_over_budget_and_not_stopped": [],
         "cells_whose_spend_is_unknown": [],
         "protocol_repetition_requirement": "DETERMINABLE",
         "budget_rule": "ENFORCED"}
    p.update(kw)
    return p


def test_a_complete_campaign_with_a_violated_budget_is_not_usable(tmp_path):
    """The negative control, and the reason the row exists at all.

    Every result file present, every repetition run, and the budget the
    benchmark is named after was not enforced during the runs. Passing on the
    count of files is how campaign v2 would have certified itself.
    """
    u = rd.campaign_verdict(_plan(
        budget_rule="VIOLATED",
        cells_over_budget_and_not_stopped=["a/C", "b/C", "c/C"]))
    assert u["completeness"] == "COMPLETE"
    assert u["matched_budget_valid"] == "NO"
    assert not u["usable_for_release"]
    assert "budget_rule violated" in u["reason"]
    assert "3 cell(s)" in u["reason"]


def test_a_campaign_whose_spend_nothing_counted_is_not_usable_either():
    """`UNKNOWN` is not `YES`. An asserted figure is what v2 reported."""
    u = rd.campaign_verdict(_plan(
        budget_rule="NOT_DETERMINABLE",
        cells_whose_spend_is_unknown=["a/A", "b/A"]))
    assert u["matched_budget_valid"] == "UNKNOWN"
    assert not u["usable_for_release"]
    assert "asserted a figure nothing counted" in u["reason"]


def test_a_complete_campaign_under_an_enforced_budget_is_usable():
    """The positive control: without it, a rule that failed everything would
    score perfectly on every test above."""
    u = rd.campaign_verdict(_plan())
    assert u["completeness"] == "COMPLETE"
    assert u["matched_budget_valid"] == "YES"
    assert u["usable_for_release"]


def test_a_campaign_missing_repetitions_is_partial_not_complete():
    p = _plan()
    p["cells"][0]["completed_repetitions"] = 1
    u = rd.campaign_verdict(p)
    assert u["completeness"] == "PARTIAL"
    assert not u["usable_for_release"]


def test_a_campaign_nothing_can_ask_a_number_of_is_historical_not_complete():
    """v2's shape: every cell ran what it could be asked for, and the frozen
    protocol does not decide what it owed. Calling that COMPLETE would be a
    claim the protocol cannot support."""
    u = rd.campaign_verdict(_plan(
        protocol_repetition_requirement="NOT_DETERMINABLE",
        cells=[{"task": "t", "arm": "A", "required_repetitions": "NOT_DETERMINABLE",
                "completed_repetitions": 1}]))
    assert u["completeness"] == "HISTORICAL_COMPLETE"
    assert not u["usable_for_release"]


def test_a_campaign_that_never_ran_is_not_run_rather_than_partial():
    u = rd.campaign_verdict(_plan(
        cells=[{"task": "t", "arm": "A", "required_repetitions": 3,
                "completed_repetitions": 0}],
        cells_with_no_repetition=["t/A"]))
    assert u["completeness"] == "NOT_RUN"
    assert not u["usable_for_release"]


def test_an_empty_campaign_does_not_report_its_budget_as_enforced():
    """A campaign with no runs has no cell over budget, and the rule would
    read ENFORCED off that emptiness. A green derived from nothing having
    happened is the shape this whole file refuses."""
    u = rd.campaign_verdict(_plan(
        cells=[{"task": "t", "arm": "A", "required_repetitions": 3,
                "completed_repetitions": 0}],
        cells_with_no_repetition=["t/A"], budget_rule="ENFORCED"))
    assert u["matched_budget_valid"] == "UNKNOWN"
    assert "nothing about the budget was measured" in u["reason"]


def test_the_release_critical_campaign_row_blocks_and_the_historical_one_does_not():
    """v2 is immutable, so a blocking row over it could never be satisfied --
    and a gate that cannot be satisfied puts pressure on re-interpreting the
    dataset, which the protocol forbids by name. v3 carries the block."""
    needs_evidence(RUN_EVIDENCE_V3)
    import inspect

    assert "advisory=True" in inspect.getsource(rd.row_benchmark_v2)
    assert "beratend" not in inspect.getsource(rd.row_benchmark_v3)
    assert not rd.row_benchmark_v3().advisory
    assert rd.row_benchmark_v2().advisory


def test_a_leak_is_named_separately_from_the_other_export_problems():
    """A home path, a private address or a token-shaped string in a published
    file is the one finding that would make publishing actively harmful.

    Folding it into "other problems" would put it next to a stale manifest.
    """
    import inspect

    source = inspect.getsource(rd.row_export)
    for art in ("home-path", "private-address", "token-shaped"):
        assert art in source, f"{art} is not named in the export row"
    assert "leak(s)" in source


def test_every_gate_shaped_tool_has_a_row():
    """The O138 class: a tool that decides something and no row that runs it.

    A gate nobody looks at is a gate that teaches people not to look, so the
    tools that answer a yes/no question about this repository are listed here
    and checked against the table. Collectors and generators are not gates and
    are named as the exception they are.
    """
    import inspect

    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parent.parent
    tools_ = {f.stem for f in (root / "tools").glob("*.py")}
    no_gates = {
        "collect_strict_evidence",   # a collector: it installs evidence
        "benchmark",                 # a runner; its *result* is the row
        "readiness",                 # this tool itself
        "confinement_evidence",      # a measurement; its SUMMARY is the row
        "telemetry_audit",           # a measurement; its report is the row
    }
    source = inspect.getsource(rd.row_list) + "".join(
        inspect.getsource(getattr(rd, n)) for n in dir(rd)
        if n.startswith("row_"))
    # Distribution checks operate on a canonical artifact and installed copies,
    # not on product readiness in a source checkout. Their gate surface is the
    # publishing workflow. Require executable invocations there rather than
    # pretending they are collectors or adding them to keine_gates.
    distribution_gates = {
        "distribution_release": "python control/tools/distribution_release.py archive",
        "distribution_smoke": "python -I distribution_smoke.py",
    }
    workflow = (root / ".github/workflows/publish-pypi.yml").read_text()
    for name, invocation in distribution_gates.items():
        assert name in tools_
        assert invocation in workflow, f"distribution gate {name} is not executed"
    assert "needs: testpypi-smoke" in workflow
    assert "name: pypi" in workflow
    for name in sorted(tools_ - no_gates - distribution_gates.keys()):
        assert name in source, (
            f"tools/{name}.py decides something and no readiness row runs it")


def test_the_external_ci_row_is_not_advisory_when_it_cannot_be_answered():
    """A row set that quietly leaves CI out lets a reader assume it is covered
    by the local suite, and it is not.

    Where there is no remote to ask, the row is `NOT_RUN` and blocks. A
    checkout that cannot produce that evidence is a checkout that cannot
    declare itself ready, and saying so is cheaper than discovering it after
    the tag.
    """
    import inspect

    source = inspect.getsource(rd.row_ci)
    assert "beratend" not in source, "the CI row must block"
    assert "NOT_RUN" in source


def test_writing_the_document_re_anchors_its_own_ledger_entries(tmp_path,
                                                                monkeypatch):
    """Doing this by hand after every gate run is how O139 happened.

    The rows change whenever a gate's result changes -- that is the point of
    them -- and this repository's policy makes the document a claim surface.
    So the tool that writes the rows writes their anchors, updating entries in
    place and never removing one, because a removed id leaves the gap the
    ledger refuses.
    """
    import json

    target = tmp_path / "docs" / "READINESS.md"
    target.parent.mkdir()
    target.write_text(
        "# x\n\nMeasured at `abc1234` on 2026-09-13.\n\n"
        "| condition | state | measured | command |\n"
        "|---|---|---|---|\n"
        "| `a` | PASS | one | `cmd a` |\n"
        "| `b` | FAIL | two | `cmd b` |\n")
    (tmp_path / "CLAIMS.json").write_text(json.dumps({
        "claims": [
            # Board-shaped: a stale version of the row it anchors. Ownership
            # is decided on the text (O176), so a placeholder here would
            # exercise the opposite of the real case.
            {"id": "C-001", "text": "| `a` | FAIL | zero | `cmd a` |",
             "where": "docs/READINESS.md:7",
             "status": "SUPPORTED", "anchor_digest": "stale"},
            # Not board-shaped, anchored in the same document: prose that
            # somebody else's claim points at. It must come back untouched.
            # Before O176 every entry in this file was zipped positionally
            # against the generated rows, so a claim about the published
            # distribution was rewritten into a `routing` row -- factually
            # wrong, and green, because the ledger had been made consistent
            # with itself.
            {"id": "C-002",
             "text": "The v0.1.0 distribution was published on 2026-09-15.",
             "where": "docs/READINESS.md:9",
             "status": "SUPPORTED", "anchor_digest": "prosa"},
        ],
        "not_claims": [],
    }))
    monkeypatch.setattr(rd, "HOH", tmp_path)

    n = rd._catch_up_ledger(target)

    d = json.loads((tmp_path / "CLAIMS.json").read_text())
    assert n >= 2
    ids = [e["id"] for e in d["claims"]]
    assert "C-001" in ids, "an existing entry was removed instead of updated"
    assert len(d["claims"]) == 3, "two rows, plus the prose entry left alone"
    after = {e["id"]: e for e in d["claims"]}
    assert after["C-001"]["anchor_digest"] != "stale"
    assert after["C-001"]["text"] == "| `a` | PASS | one | `cmd a` |"
    # The negative control, and the half that matters: prose this tool does
    # not write must come back exactly as it went in.
    assert after["C-002"]["text"] == (
        "The v0.1.0 distribution was published on 2026-09-15.")
    assert after["C-002"]["anchor_digest"] == "prosa"
    assert after["C-002"]["where"] == "docs/READINESS.md:9"
    assert d["not_claims"], "the frame and the provenance line are not_claims"


def test_the_campaign_rows_consult_the_frozen_contract_not_the_directory():
    """The row reads `tools/repetition_plan.py`, which reads the frozen
    commit. Counting result files would answer a question the protocol did
    not ask."""
    import inspect

    source = inspect.getsource(rd._plan) + inspect.getsource(rd.row_benchmark_v3)
    assert "repetition_plan" in source, "the row does not consult the contract"
    assert "prereg" in inspect.getsource(rd.row_benchmark_v3), (
        "the row does not check that the instrument was the frozen one")


def test_an_unregistered_or_unaccounted_freeze_fails_the_release_row(monkeypatch):
    """A campaign run on an instrument that moved under it is not the campaign
    that was pre-registered, however complete its results look.

    Drift that nothing accounts for fails. Drift that a post-campaign artifact
    accounts for -- named file, stated reason, changed after the last cell,
    raw results still hashing to what they hashed to -- does not: the freeze
    describes the instrument *during* the campaign and is deliberately not
    re-taken afterwards.
    """
    monkeypatch.setattr(rd, "_plan", lambda k: _plan())
    monkeypatch.setattr(rd, "_prereg", lambda k: {
        "verdict": "DRIFTED", "changed": ["tools/benchmark.py"],
        "files_frozen": 40, "protocol_commit": "a" * 40})
    monkeypatch.setattr(rd, "drift_accounted",
                        lambda k, v: (False, ["nothing accounts for them"]))
    z = rd.row_benchmark_v3()
    assert z.state == rd.FAIL
    assert "not accounted for" in z.value_

    monkeypatch.setattr(rd, "drift_accounted", lambda k, v: (True, []))
    z = rd.row_benchmark_v3()
    assert z.state == rd.PASS
    assert "accounted for" in z.value_

    monkeypatch.setattr(rd, "_prereg", lambda k: {
        "verdict": "NOT_REGISTERED", "changed": [], "files_frozen": 0,
        "protocol_commit": ""})
    assert rd.row_benchmark_v3().state == rd.FAIL, (
        "an unregistered campaign is never accounted for")

    monkeypatch.setattr(rd, "_prereg", lambda k: {
        "verdict": "FROZEN", "changed": [], "files_frozen": 40,
        "protocol_commit": "a" * 40})
    assert rd.row_benchmark_v3().state == rd.PASS


def test_copied_repetitions_make_a_campaign_partial_not_complete():
    """Three files, one run. `completed_repetitions` counts labels."""
    u = rd.campaign_verdict(_plan(cells_whose_repetitions_share_a_run=["t/C"]))
    assert u["completeness"] == "PARTIAL"
    assert not u["usable_for_release"]
    assert "share a run identity" in u["reason"]


def test_the_historical_row_does_not_render_green_while_saying_no():
    """A green PASS whose own text reads `matched_budget_valid = NO` tells a
    reader scanning the column the opposite of what it says. The advisory
    flag, not the state, is what keeps it from blocking."""
    needs_evidence(RUN_EVIDENCE_V2)
    z = rd.row_benchmark_v2()
    assert z.advisory
    assert z.state == rd.FAIL
    assert "matched_budget_valid = NO" in z.value_


def test_a_row_cannot_waive_itself_from_a_boolean_in_the_file_it_grades(
        tmp_path, monkeypatch):
    """`beratend = felder == "yes"` read one boolean out of an unsigned
    artifact while the verdict read another out of the same file, so a file
    saying `telemetry_validated: "no"` beside `fields_validated: "yes"`
    waived its own row.

    The row no longer has an advisory path at all: it blocks on a field gap
    anywhere and passes only when every shape has been observed somewhere.
    This checks that the artifact's own verdict field cannot decide it --
    the numbers decide it.
    """
    import inspect

    monkeypatch.setattr(rd, "HOH", tmp_path)
    _audit(tmp_path, "one", fields_with_gaps=["model"],
           telemetry_validated_on_real_dispatches="yes")
    z = rd.row_telemetry()
    assert z.state == rd.FAIL, (
        "an artifact claiming validation cannot override its own gap list")
    assert not z.advisory, "the row has no advisory escape"
    assert "beratend" not in inspect.getsource(rd.row_telemetry)


def test_the_routing_row_can_fail(tmp_path, monkeypatch):
    """It returned the literal PASS regardless of what it read, and a missing
    file took the whole tool down with a traceback. A row that cannot fail is
    a permanent green inside a conjunction."""
    monkeypatch.setattr(rd, "HOH", tmp_path)
    (tmp_path / "docs").mkdir()
    target = tmp_path / "docs" / "ROUTING.md"

    target.write_text("routing_decision = ABANDONED\n", encoding="utf-8")
    assert rd.row_routing().state == rd.FAIL

    target.write_text("nothing decided here\n", encoding="utf-8")
    z = rd.row_routing()
    assert z.state == rd.FAIL
    assert "no routing_decision line" in z.value_

    target.write_text("routing_decision = DEFERRED_ON_EVIDENCE\n", encoding="utf-8")
    assert rd.row_routing().state == rd.PASS

    target.rename(target.with_suffix(".md.parked"))
    assert rd.row_routing().state == rd.NOT_RUN


def test_no_row_is_both_not_run_and_advisory():
    """`verdikt()` drops any non-PASS row that is advisory, so a row that is
    both NOT_RUN and advisory vanishes silently -- a check that did not run,
    counted as no obstacle.

    There is no such row today. The mechanism permits one, which is why this
    is a test rather than a comment: `NOT_RUN` is never a pass anywhere else
    in this project, and a future advisory flag on an unrun row would be that
    rule quietly reversed.
    """
    import inspect

    source = "".join(inspect.getsource(getattr(rd, n)) for n in dir(rd)
                     if n.startswith("row_"))
    # Every NOT_RUN construction in the file, and none of them may carry the
    # advisory flag on the same call.
    for piece in source.split("NOT_RUN")[1:]:
        head = piece.split(")")[0]
        assert "beratend" not in head, (
            f"a NOT_RUN row sets beratend: ...NOT_RUN{head})")


def test_a_leak_is_never_advisory_whatever_else_the_export_found():
    """The flag separates "a citation a reader cannot follow" from "a
    finding". A home path or a token-shaped string in a published file is the
    second kind under any reading."""
    import inspect

    source = inspect.getsource(rd.row_export)
    assert "not lecks" in source, "a leak can still be waived"


def _drift(tmp_path, monkeypatch, artifact_=None, changed=("tools/benchmark.py",),
           digests_equal=True):
    """A tree with a campaign, a bound digest table, and optional accounting."""
    import hashlib
    import json

    monkeypatch.setattr(rd, "HOH", tmp_path)
    outcome_value = tmp_path / "dogfood" / "benchmark" / "results-v3"
    outcome_value.mkdir(parents=True)
    (outcome_value / "t.A.1.json").write_text('{"task": "t"}', encoding="utf-8")
    d = tmp_path / "docs" / "benchmarks" / "v3"
    d.mkdir(parents=True)
    real = hashlib.sha256((outcome_value / "t.A.1.json").read_bytes()).hexdigest()
    (d / "RAW_RESULT_DIGESTS.json").write_text(json.dumps(
        {"digests": {"t.A.1.json": real if digests_equal else "0" * 64}}),
        encoding="utf-8")
    if artifact_ is not None:
        (d / "POST_CAMPAIGN_DRIFT.json").write_text(
            json.dumps(artifact_), encoding="utf-8")
    return {"changed": list(changed), "added": [], "removed": []}


def _good_artifact(paths=("tools/benchmark.py",)):
    return {
        "campaign_complete": True,
        "last_cell_finished_at_utc": "2026-09-14T02:54:15Z",
        "changes": [{"path": p,
                     "earliest_change_committed_at": "2026-09-14T06:29:53+00:00"}
                    for p in paths],
        "why_each_changed": {p: "a stated reason long enough to be one" for p in paths},
    }


def test_drift_with_no_accounting_at_all_is_not_accepted(tmp_path, monkeypatch):
    v = _drift(tmp_path, monkeypatch)
    ok, why_text = rd.drift_accounted("v3", v)
    assert not ok
    assert "nothing accounts for them" in why_text[0]


def test_an_accounted_for_post_campaign_repair_is_accepted(tmp_path, monkeypatch):
    """The positive control. Without it a rule that refused everything would
    score perfectly on every test below."""
    v = _drift(tmp_path, monkeypatch, artifact_=_good_artifact())
    ok, why_text = rd.drift_accounted("v3", v)
    assert ok, why_text


def test_a_drifted_file_the_artifact_does_not_name_is_not_accepted(
        tmp_path, monkeypatch):
    """Naming one repair must not excuse a second file that moved with it."""
    v = _drift(tmp_path, monkeypatch, artifact_=_good_artifact(),
               changed=("tools/benchmark.py", "src/hoh/controller.py"))
    ok, why_text = rd.drift_accounted("v3", v)
    assert not ok
    assert any("src/hoh/controller.py" in w and "not named" in w for w in why_text)


def test_a_change_made_before_the_campaign_finished_is_not_a_post_campaign_repair(
        tmp_path, monkeypatch):
    """That is the thing the freeze exists to forbid, and calling it a repair
    afterwards would launder it."""
    a = _good_artifact()
    a["changes"][0]["earliest_change_committed_at"] = "2026-09-14T01:00:00+00:00"
    v = _drift(tmp_path, monkeypatch, artifact_=a)
    ok, why_text = rd.drift_accounted("v3", v)
    assert not ok
    assert any("before the campaign finished" in w for w in why_text)


def test_drift_during_an_incomplete_campaign_is_not_accepted(tmp_path, monkeypatch):
    a = _good_artifact()
    a["campaign_complete"] = False
    v = _drift(tmp_path, monkeypatch, artifact_=a)
    ok, why_text = rd.drift_accounted("v3", v)
    assert not ok
    assert any("complete" in w for w in why_text)


def test_a_reason_is_required_and_not_just_a_name(tmp_path, monkeypatch):
    a = _good_artifact()
    a["why_each_changed"] = {}
    v = _drift(tmp_path, monkeypatch, artifact_=a)
    ok, why_text = rd.drift_accounted("v3", v)
    assert not ok
    assert any("without a reason" in w for w in why_text)


def test_the_raw_digests_are_recomputed_and_not_read_back(tmp_path, monkeypatch):
    """An artifact that asserts its own conclusion is the shape this project
    refuses everywhere else. If the result files no longer hash to what the
    bound table says, no accounting makes the repair a repair of the report."""
    v = _drift(tmp_path, monkeypatch, artifact_=_good_artifact(),
               digests_equal=False)
    ok, why_text = rd.drift_accounted("v3", v)
    assert not ok
    assert any("do not hash to what they hashed" in w for w in why_text)


def _audit(d, name, **kw):
    import json

    baseline = {"fields_with_gaps": [], "coverage_gaps": [],
             "shapes_not_observed": [],
             "coverage": {"failure_record": True, "retry_record": True},
             "telemetry_validated_on_real_dispatches": "yes"}
    baseline.update(kw)
    target = d / "dogfood" / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "TELEMETRY_AUDIT.json").write_text(json.dumps(baseline),
                                               encoding="utf-8")


def test_a_field_gap_in_any_audit_blocks(tmp_path, monkeypatch):
    """A field nobody filled is a defect on the run that has it, and another
    run filling it repairs nothing."""
    monkeypatch.setattr(rd, "HOH", tmp_path)
    _audit(tmp_path, "good")
    _audit(tmp_path, "other", fields_with_gaps=["model"],
           telemetry_validated_on_real_dispatches="no")
    z = rd.row_telemetry()
    assert z.state == rd.FAIL
    assert "model" in z.value_


def test_a_shape_no_audit_ever_observed_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "HOH", tmp_path)
    _audit(tmp_path, "a", coverage={"failure_record": False,
                                    "retry_record": True},
           telemetry_validated_on_real_dispatches="no")
    _audit(tmp_path, "b", coverage={"failure_record": False,
                                    "retry_record": True},
           telemetry_validated_on_real_dispatches="no")
    z = rd.row_telemetry()
    assert z.state == rd.FAIL
    assert "failure_record" in z.value_


def test_a_shape_one_audit_observed_does_not_block(tmp_path, monkeypatch):
    """A campaign in which nothing failed cannot be asked to produce a failure
    record, and demanding one would be demanding a fabricated record."""
    monkeypatch.setattr(rd, "HOH", tmp_path)
    _audit(tmp_path, "quiet", coverage={"failure_record": False,
                                        "retry_record": False},
           shapes_not_observed=["failure_record", "retry_record"],
           telemetry_validated_on_real_dispatches="no")
    _audit(tmp_path, "busy")
    z = rd.row_telemetry()
    assert z.state == rd.PASS
    assert "busy" in z.value_


def test_a_parked_predecessor_audit_is_not_judged(tmp_path, monkeypatch):
    """Replacing without deleting is how this project versions a tree. The
    parked copy's audit was written against an earlier record and reports
    gaps in fields that did not exist yet."""
    monkeypatch.setattr(rd, "HOH", tmp_path)
    _audit(tmp_path, "live")
    _audit(tmp_path, "live.v20260913T143002Z",
           fields_with_gaps=["provider_calls"],
           telemetry_validated_on_real_dispatches="no")
    assert rd.row_telemetry().state == rd.PASS


def test_no_audit_at_all_is_not_run_rather_than_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "HOH", tmp_path)
    (tmp_path / "dogfood").mkdir()
    assert rd.row_telemetry().state == rd.NOT_RUN


def _section_with(receipt_changes, tmp_path):
    """Render the distribution section from a doctored copy of the receipt.

    The real receipt is never touched: it is the record of what was actually
    published, and editing it to make a test pass would be the one move this
    whole section is about.
    """
    import json

    real = json.loads(
        (ROOT / ".github/releases/v0.1.0.json").read_text())
    d = json.loads(json.dumps(real))
    for key, value_ in receipt_changes.items():
        if value_ is None:
            d.pop(key, None)   # absent, not present-and-null
        else:
            d[key] = value_
    target = tmp_path / ".github" / "releases"
    target.mkdir(parents=True, exist_ok=True)
    (target / "v0.1.0.json").write_text(json.dumps(d))
    old = rd.HOH
    try:
        rd.HOH = tmp_path
        return [z for z in rd.distribution_section() if z.strip()]
    finally:
        rd.HOH = old


def test_the_distribution_section_is_derived_and_not_asserted(tmp_path):
    """The counter-proof an independent review ran, kept as a test.

    O176 moved this section into a generator so a board run could not delete
    it. The first generator read three fields and stated the rest as
    constants -- so a receipt recording FAIL, NOT_VERIFIED and
    `long_lived_pypi_token_used: true` still produced text claiming
    byte-identical files, verified attestations and protected token-free
    publishing. O140's shape, inside the repair for O176.

    Each assurance is checked against its own field, and the positive case is
    asserted too: a guard that only ever says "not confirmed" would pass this
    while making the section useless.
    """
    real = _section_with({}, tmp_path / "echt")
    connected = " ".join(real)
    assert "Not confirmed" not in connected, (
        "the real receipt records a successful publication and the section "
        "must say so; a generator that never affirms anything is not a "
        "derivation either"
    )
    for must_ in ("byte-identical", "PEP-740", "Trusted Publishing"):
        assert must_ in connected

    negativ = _section_with({
        "production_pypi_result": "FAIL",
        "testpypi_result": "FAIL",
        "github_assets_match": False,
        "attestations": {"status": "NOT_VERIFIED"},
        "trusted_publishing": False,
        "long_lived_pypi_token_used": True,
        "production_environment": {"required_reviewer": None},
    }, tmp_path / "negativ")
    text = " ".join(negativ)
    for field_ in ("production_pypi_result", "github_assets_match",
                 "attestations.status", "trusted_publishing"):
        assert f"`{field_}" in text or field_ in text, (
            f"{field_} is not named in the section that rests on it")
    assert text.count("Not confirmed") >= 4, (
        "a receipt recording failure on every axis still produced affirmative "
        "text: " + text[:400]
    )
    assert "were verified." not in text
    assert "carry byte-identical wheel and sdist files." not in text


def test_a_missing_field_is_not_confirmed_rather_than_assumed(tmp_path):
    """Absent evidence and negative evidence are different, and neither is a
    pass. The wording distinguishes them; both refuse the claim."""
    missing_ = _section_with({
        "attestations": None, "trusted_publishing": None,
        "long_lived_pypi_token_used": None, "github_assets_match": None,
    }, tmp_path / "fehlend")
    text = " ".join(missing_)
    assert "the receipt carries no" in text
    assert "were verified." not in text


def test_one_failing_python_version_is_named_not_averaged(tmp_path):
    """The versions come from python_smoke, and a version recorded as failing
    must appear as failing rather than be dropped from the list -- a list of
    only the passing ones reads as though the others were never tried."""
    import json

    real = json.loads((ROOT / ".github/releases/v0.1.0.json").read_text())
    smoke = json.loads(json.dumps(real["python_smoke"]))
    bad = sorted(smoke)[-1]
    smoke[bad]["result"] = "FAIL"
    text = " ".join(_section_with({"python_smoke": smoke}, tmp_path / "py"))
    assert "Not confirmed" in text and bad in text
    assert "as not passing on " + bad in text


def test_no_receipt_says_so_instead_of_vanishing(tmp_path):
    """The section that replaced hand-written prose must not disappear when
    its input does -- that was the failure mode it was built to end."""
    empty = tmp_path / "leer"
    empty.mkdir()
    old = rd.HOH
    try:
        rd.HOH = empty
        row_list = [z for z in rd.distribution_section() if z.strip()]
    finally:
        rd.HOH = old
    assert any("## The published distribution" in z for z in row_list)
    assert any("No distribution receipt" in z for z in row_list)


def test_a_truthy_string_is_not_a_yes_and_a_missing_field_is_not_a_no(tmp_path):
    """An independent review's second pass, and both halves are the same slip.

    `bool(wert)` accepted the string "false" as true, so a receipt recording
    `trusted_publishing: "false"` still produced the success sentence. And a
    missing `long_lived_pypi_token_used` was read as "no token was used" --
    an absent record treated as a record of absence, which is the shape this
    project refuses everywhere else.

    Fields are compared by type and value now. Four answers, not two: says
    yes, says no, says nothing, says something of the wrong type -- and only
    the first produces the claim.
    """
    cases_ = [
        ("tp true, token field absent",
         {"long_lived_pypi_token_used": None}, "trusted_publishing"),
        ("tp as the string 'false'",
         {"trusted_publishing": "false"}, "trusted_publishing"),
        ("token as the int 1",
         {"long_lived_pypi_token_used": 1}, "trusted_publishing"),
        ("assets as the string 'false'",
         {"github_assets_match": "false"}, "github_assets_match"),
        ("assets field absent",
         {"github_assets_match": None}, "github_assets_match"),
    ]
    for name, change_, field_ in cases_:
        row_list = _section_with(change_, tmp_path / name.replace(" ", "_"))
        hits_ = [z for z in row_list if field_ in z]
        assert hits_, f"{name}: no line rests on {field_}"
        assert "Not confirmed" in hits_[0], (
            f"{name}: produced an affirmative claim -- {hits_[0][:120]}"
        )

    # The negative control: the real receipt must still affirm, or a check
    # that refuses everything would pass this test while saying nothing.
    real = " ".join(_section_with({}, tmp_path / "echt"))
    assert "Not confirmed" not in real
    assert "with no long-lived token." in real
    assert "carry byte-identical wheel and sdist files." in real

