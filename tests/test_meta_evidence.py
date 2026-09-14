"""The release-critical metrics, and controls for the controls.

`tools/meta_evidence.py` measures four things and carries a negative control
for each. This file checks the measurements, and -- more to the point -- checks
that the negative controls can report ESCAPED. A falsifier that always says
KILLED is the same unfalsifiable tick-box as no falsifier at all, one level up.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from conftest import CONFINEMENT_EVIDENCE, braucht_evidenz


WURZEL = Path(__file__).resolve().parent.parent


def _laden():
    spec = importlib.util.spec_from_file_location(
        "meta_evidence", WURZEL / "tools" / "meta_evidence.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("meta_evidence", mod)
    spec.loader.exec_module(mod)
    return mod


me = _laden()


# --------------------------------------------------------------------------- #
# The measurements
# --------------------------------------------------------------------------- #


def test_only_decisions_by_someone_other_than_the_orchestrator_count():
    zustand = {
        "decisions": [
            {"kind": "MERGE_RELEASE", "actor": "orchestrator"},
            {"kind": "CLOSURE_VERDICT", "actor": "orchestrator"},
        ],
        "external_actions": [],
    }
    assert me._interventions(zustand) == 0
    zustand["decisions"].append({"kind": "POLICY_DISPOSITION", "actor": "a person"})
    assert me._interventions(zustand) == 1


def test_a_repository_mutation_nothing_accounts_for_also_counts():
    """Both halves, because the project has got each of them wrong once: the
    decision list alone missed two commits, and the record list alone was empty
    on a run that predated the mechanism that fills it."""
    zustand = {"decisions": [], "external_actions": [{"head_before": "a"}]}
    assert me._interventions(zustand) == 1


def test_a_project_with_an_unmerged_node_is_not_at_a_fixpoint():
    assert me._geschlossen({"nodes": [{"lifecycle": "MERGED"}]}) is True
    assert me._geschlossen(
        {"nodes": [{"lifecycle": "MERGED"}, {"lifecycle": "READY"}]}
    ) is False


def test_a_project_with_no_nodes_is_not_at_a_fixpoint():
    """Zero nodes all being merged is vacuously true, and is the shape of a
    state nothing ever wrote to."""
    assert me._geschlossen({"nodes": []}) is False


def _repo_mit(tmp_path: Path, subjects: list[str]) -> Path:
    p = tmp_path / "r"
    p.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=p, check=True)
    for s in subjects:
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-m", s],
            cwd=p, check=True,
        )
    return p


def test_a_candidate_taken_twice_is_caught_in_git(tmp_path):
    einmal = _repo_mit(tmp_path / "a", [
        "Initial",
        "Take accepted candidate slugify (accepted slugify-i1)",
        "Take accepted candidate repair-1-1 (accepted repair-1-1-i1)",
    ])
    assert me._kandidaten_einmal(einmal) is True

    zweimal = _repo_mit(tmp_path / "b", [
        "Initial",
        "Take accepted candidate slugify (accepted slugify-i1)",
        "Take accepted candidate slugify (accepted slugify-i1)",
    ])
    assert me._kandidaten_einmal(zweimal) is False


def test_a_directory_that_is_no_repository_does_not_read_as_clean(tmp_path):
    """`git log` failing must not be indistinguishable from "no duplicates"."""
    leer = tmp_path / "leer"
    leer.mkdir()
    assert me._kandidaten_einmal(leer) is False


# --------------------------------------------------------------------------- #
# The receipts
# --------------------------------------------------------------------------- #


def _quittung(pfad: Path, name: str, iso: dict | None) -> None:
    pfad.mkdir(parents=True, exist_ok=True)
    (pfad / f"{name}.json").write_text(json.dumps({
        "receipt_id": name, "run_id": "r", "iteration": 1, "attempt": 1,
        "check_id": "K1", "candidate_binding": "b", "command": "true",
        "exit_code": 0, "started_at": "t", "ended_at": "t", "stdout_digest": "d",
        "runner_identity": "t", "runner_ok": True, "isolation": iso,
    }))


STRENG = {
    "requested": "strict", "effective": "strict", "backend": "bubblewrap",
    "fallback_to_none": False, "verified_from_inside": True,
    "network_policy": "denied", "candidate_mount_mode": "read-only",
}


def test_receipts_that_prove_isolation_are_counted(tmp_path):
    q = tmp_path / "run" / "receipts"
    _quittung(q, "a", STRENG)
    _quittung(q, "b", STRENG)
    gut, alle, schlecht = me._strict_quittungen(tmp_path)
    assert (gut, alle, schlecht) == (2, 2, [])


def test_a_receipt_that_only_claims_isolation_is_an_offender(tmp_path):
    """The difference the marker introduced: `effective=strict` on its own was
    the backend agreeing with itself."""
    q = tmp_path / "run" / "receipts"
    _quittung(q, "a", STRENG)
    _quittung(q, "b", {**STRENG, "verified_from_inside": False})
    gut, alle, schlecht = me._strict_quittungen(tmp_path)
    assert gut == 1 and alle == 2
    assert schlecht == ["b"]


def test_a_receipt_that_fell_back_is_an_offender(tmp_path):
    q = tmp_path / "run" / "receipts"
    _quittung(q, "a", {**STRENG, "effective": "none", "fallback_to_none": True})
    assert me._strict_quittungen(tmp_path)[2] == ["a"]


def test_a_receipt_with_no_isolation_record_is_an_offender_not_an_absence(tmp_path):
    """It used to be skipped before the total was incremented, so a run where
    the guard refused every check -- zero executions, zero records -- returned
    (0, 0, []) and read as "no evidence exists". A run where nothing happened
    must not be able to look like a run that was never attempted."""
    q = tmp_path / "run" / "receipts"
    _quittung(q, "alt", None)
    gut, alle, schlecht = me._strict_quittungen(tmp_path)
    assert (gut, alle) == (0, 1)
    assert schlecht == ["alt (no isolation record)"]


def test_a_run_where_nothing_ran_is_not_the_same_as_no_run(tmp_path):
    leer = tmp_path / "nichts"
    leer.mkdir()
    assert me._strict_quittungen(leer) == (0, 0, [])

    refused = tmp_path / "refused" / "receipts"
    for name in ("K1", "K2", "K3"):
        _quittung(refused, name, None)
    gut, alle, schlecht = me._strict_quittungen(tmp_path / "refused")
    assert alle == 3 and len(schlecht) == 3


# --------------------------------------------------------------------------- #
# Controls for the controls
# --------------------------------------------------------------------------- #


def test_a_falsifier_reports_killed_when_the_metric_notices(tmp_path):
    pfad = tmp_path / "s.json"
    pfad.write_text(json.dumps({"decisions": [], "external_actions": []}))
    zustand, detail = me._falsify_json(
        pfad,
        lambda d: d["decisions"].append({"actor": "a person"}) or "a human decision",
        me._interventions,
    )
    assert zustand.value == "KILLED"
    assert "moved from 0 to 1" in detail


def test_a_falsifier_reports_escaped_when_the_metric_is_blind(tmp_path):
    """The control for every other control in this file. A falsifier that can
    only ever say KILLED proves nothing about the metric it is pointed at."""
    pfad = tmp_path / "s.json"
    pfad.write_text(json.dumps({"decisions": [], "external_actions": []}))
    zustand, detail = me._falsify_json(
        pfad,
        lambda d: d.update({"irrelevant": True}) or "something the metric ignores",
        me._interventions,
    )
    assert zustand.value == "ESCAPED"
    assert "noticed nothing" in detail


def test_a_falsifier_on_a_missing_source_is_not_run_rather_than_killed(tmp_path):
    zustand, detail = me._falsify_json(
        tmp_path / "nope.json", lambda d: "x", me._interventions
    )
    assert zustand.value == "NOT_RUN"


def test_the_falsifier_does_not_touch_the_original(tmp_path):
    pfad = tmp_path / "s.json"
    original = json.dumps({"decisions": [], "external_actions": []})
    pfad.write_text(original)
    me._falsify_json(
        pfad,
        lambda d: d["decisions"].append({"actor": "a person"}) or "a human decision",
        me._interventions,
    )
    assert pfad.read_text() == original


# --------------------------------------------------------------------------- #
# The closure as a gate
# --------------------------------------------------------------------------- #


def test_without_the_negative_controls_the_gate_cannot_go_green():
    """Skipping the falsifiers must not be a shortcut to a pass.

    Every critical metric stays `NOT_RUN`, and `NOT_RUN` is not a pass -- so
    the run that did the least work is the one that cannot claim anything.
    """
    schluss = me.sammeln(WURZEL, falsify=False)
    assert not schluss.green()
    for metrik, gruende in schluss.problems().items():
        assert any("negative control" in g or "not a pass" in g or "required" in g
                   for g in gruende), (metrik, gruende)


def test_the_closure_requires_all_four_metrics():
    schluss = me.sammeln(WURZEL, falsify=False)
    # Each metric carries its authority class as a prefix, so
    # `assurance.AUTHORITIES` decides which source may answer it. That is what
    # makes the policies load-bearing on the metrics a release rests on rather
    # than a table nothing consults.
    assert set(schluss.required) == {
        "node_lifecycle:unattended_interventions",
        "node_lifecycle:unattended_fixpoint",
        "landed_commit:unattended_no_duplicate_merge",
        "check_executed_under_isolation:strict_real_agent_e2e",
    }
    # A required metric that produced no record at all is reported as missing
    # rather than quietly absent from the report.
    vorhanden = {r.metric_id for r in schluss.records}
    for verlangt in schluss.required:
        assert verlangt in vorhanden or verlangt in schluss.problems()


# --------------------------------------------------------------------------- #
# The planner capability boundary as a release-critical metric
# --------------------------------------------------------------------------- #


def test_the_confinement_metric_reads_the_counters_not_only_the_verdict():
    """A summary is a verdict somebody's tool wrote.

    The record carries the counters and the open findings beside it, so a
    summary claiming VERIFIED with a non-zero counter shows the contradiction
    rather than passing on the word.
    """
    from pathlib import Path

    wurzel = WURZEL / "dogfood" / "planner-confinement"
    if not (wurzel / "SUMMARY.json").is_file():
        braucht_evidenz(CONFINEMENT_EVIDENCE)
    zustand, detail = me._falsifiziere_eingrenzung(Path(wurzel))
    assert zustand.value == "KILLED", detail
    assert "still said VERIFIED" in detail


def test_the_boundary_metric_is_in_the_closure_and_carries_its_source():
    schluss = me.sammeln(WURZEL, falsify=False)
    passend = [r for r in schluss.records
               if r.metric_id.startswith("planner_capability_boundary")]
    assert passend, [r.metric_id for r in schluss.records]
    (r,) = passend
    assert r.source.kind.value in ("run_state", "absent")
    if r.source.kind.value == "run_state":
        assert r.cross_check is not None
        assert r.cross_check.kind.value == "receipt"
        assert "planner_repo_mutations" in r.measured


def test_a_repository_may_not_answer_whether_a_planner_stayed_inside():
    """A clean history is exactly what a planner writing into the working tree
    leaves behind."""
    from hoh.assurance import authority_for

    politik = authority_for("planner_capability_boundary:real_agent_run")
    assert politik is not None
    erlaubt = {k.value for k in politik.allowed}
    assert "repository" not in erlaubt
    assert "digest" not in erlaubt
    assert erlaubt == {"run_state", "receipt"}


def test_the_boundary_metric_requires_a_second_source():
    """The run's own record is the thing most in reach of the roles it
    describes, so it does not stand alone."""
    from hoh.assurance import authority_for

    politik = authority_for("planner_capability_boundary:real_agent_run")
    assert politik.requires_cross_check is not None
    assert politik.requires_cross_check.value == "receipt"
