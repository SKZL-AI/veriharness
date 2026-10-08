"""The P2 gate's third condition, as the board reports it (`row_programme_supervisor`).

Written because a third-round reviewer established that this row had **no test at
all**, and then walked through it: a hand-edited `"complete": true` published PASS on
a record the product's own loader refuses, and `--phase P1 --findings empty.json`
written to P2's evidence path closed P2's independent-review condition with a review
that had looked at no P2 item. Both were one step from a release claim.

Most cases here are a bypass a reviewer performed. Two are not, and saying so is
the point of this paragraph: `test_no_review_record_is_not_run_and_is_not_a_pass`
comes from O220, and `test_one_line_keeps_the_exception_type_and_bounds_the_message`
is an ordinary unit test of a helper. The rest are bypasses, plus the positive
control that shows the row can still say PASS when the review really is complete,
current and about P2 -- a gate that can never close is as useless as one that always
does.

The row reads a tree, so each test builds one: the real register and matrix copied
into `tmp_path` (so the reconciler runs over realistic data), and a review record
written by `programme_supervisor.record_review` rather than by hand, except where
hand-editing it *is* the case under test.
"""

from __future__ import annotations

import importlib.util as il
import json
import shutil
import sys
from pathlib import Path

import pytest

HOH = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = il.spec_from_file_location(name, HOH / "tools" / f"{name}.py")
    module = il.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rd = _load("readiness")
ps = _load("programme_supervisor")

REGISTER = "program/v3_3/REQUIREMENTS.json"
MATRIX = "program/v3_3/VERIHARNESS_CAPABILITY_MATRIX.json"
REVIEW = "dogfood/p2-gate/REVIEW.json"


def _tree(tmp_path: Path, monkeypatch) -> tuple[dict, dict]:
    """The real register and matrix in a throwaway tree, with no review in it yet.

    `row_program_scope` is pinned to PASS here. O228/J5: the gate now refuses to vouch
    for a register that `row_program_scope` has not vouched for, and on this machine that
    row is NOT_RUN because the pinned plan is unreachable -- so without this every
    positive control in this file would fail for an environment reason rather than for the
    thing it tests. `test_a_register_nothing_vouches_for_is_named` exercises the
    composition itself.
    """
    monkeypatch.setattr(rd, "row_program_scope",
                        lambda: rd.Row("program_scope", rd.PASS, "pinned for this test",
                                       "n/a", "n/a"))
    for rel in (REGISTER, MATRIX):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HOH / rel, tmp_path / rel)
    (tmp_path / REVIEW).parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rd, "HOH", tmp_path)
    register = json.loads((tmp_path / REGISTER).read_text(encoding="utf-8"))
    matrix = json.loads((tmp_path / MATRIX).read_text(encoding="utf-8"))
    return register, matrix


#: The stamped directory a helper-built review is preserved in. O227/G2: the gate
#: chooses the directory itself -- the newest one under `dogfood/p2-gate` holding both
#: `findings.json` and `capsule-p2.json` -- and never follows a path the record names,
#: because every binding added before that was another field of the document being
#: checked. So a test that wants the row to look at its review has to write the review
#: where the gate looks, exactly as the real launch path does.
STAMP = "20260924T120000Z"
RETURNED = f"dogfood/p2-gate/{STAMP}/findings.json"
CAPSULE = f"dogfood/p2-gate/{STAMP}/capsule-p2.json"


def _preserve(tmp_path, register, matrix, findings, *, phase="P2", limits=None):
    """Write the two artefacts a review leaves behind, and return the capsule.

    With no `limits` this builds the **canonical** brief -- the one the gate derives for
    this board -- because O228/J1 and J2 moved coverage and phase out of the document and
    into the derivation. A brief built any other way is a different brief, and the gate
    says so, which is the point.
    """
    snapshot, _ev, _items = ps.reconciled(register, matrix)
    capsule = (rd._canonical_brief(ps, register=register, matrix=matrix,
                                   snapshot=snapshot, previous=tmp_path / CAPSULE)
               if phase == "P2" and not limits
               else ps.capsule_for(phase, register=register, matrix=matrix,
                                   **(limits or {})))
    (tmp_path / CAPSULE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / CAPSULE).write_text(
        capsule.model_dump_json(indent=1, by_alias=True), encoding="utf-8")
    (tmp_path / RETURNED).write_text(json.dumps(list(findings)), encoding="utf-8")
    return capsule


def _record(tmp_path, register, matrix, *, phase="P2", findings=(), decisions=None,
            source=RETURNED, preserve=True, limits=None):
    findings = list(findings)
    capsule = (_preserve(tmp_path, register, matrix, findings,
                         phase=phase, limits=limits) if preserve
               else ps.capsule_for(phase, register=register, matrix=matrix,
                                   **(limits or {})))
    snapshot, _ev, _items = ps.reconciled(register, matrix)
    record = ps.record_review(phase, capsule=capsule, findings=findings,
                              snapshot=snapshot, decisions=dict(decisions or {}),
                              findings_source=source)
    body = json.loads(record.model_dump_json())
    (tmp_path / REVIEW).write_text(json.dumps(body, indent=1), encoding="utf-8")
    return body


def _applied_ref(evidence="tools/preserve.py:1"):
    """A decision that both agrees with a finding and says where the repair is."""
    return {"disposition": "apply", "evidence": evidence}


def _finding(fid="F-1", *, kind="PROPOSE_REOPEN", items=("P2-08",)):
    return {"finding_id": fid, "review_id": "R-test", "kind": kind,
            "severity": "critical", "item_ids": list(items),
            "reason": "the evidence for this item is older than the module it proves",
            "proposed_action": "re-measure the capability matrix",
            "supervisor_provider": "independent-supervisor"}


# --------------------------------------------------------------------------- #
# the positive control first, so the rest is not read as "the row always fails"
# --------------------------------------------------------------------------- #


def test_a_complete_current_p2_review_passes(tmp_path, monkeypatch):
    """The control every case below is measured against: a review of P2 that found
    nothing, taken against this register and matrix, is a satisfied condition."""
    register, matrix = _tree(tmp_path, monkeypatch)
    _record(tmp_path, register, matrix)
    row = rd.row_programme_supervisor()
    assert row.state == rd.PASS
    assert "about P2" in row.value_


def test_no_review_record_is_not_run_and_is_not_a_pass(tmp_path, monkeypatch):
    """Catches an absent review counting as a satisfied condition -- the first thing
    O220 established on its first run."""
    _tree(tmp_path, monkeypatch)
    row = rd.row_programme_supervisor()
    assert row.state == rd.NOT_RUN
    assert row.state != rd.PASS


# --------------------------------------------------------------------------- #
# C1 -- a hand-forged verdict
# --------------------------------------------------------------------------- #


def test_a_hand_forged_complete_is_refused_instead_of_published(tmp_path, monkeypatch):
    """Catches the bypass a reviewer performed with a text editor: the row used to
    read `complete` and `blocking_findings` out of the file body, so setting them by
    hand published PASS on bytes that `load_record` refuses. The forged record is
    otherwise byte-identical to an honest one, which is the whole point."""
    register, matrix = _tree(tmp_path, monkeypatch)
    body = _record(tmp_path, register, matrix, findings=[_finding()])
    honest = rd.row_programme_supervisor()
    assert honest.state == rd.FAIL and "1 blocking" in honest.value_

    body["complete"] = True
    body["blocking_findings"] = []
    body["applied_without_evidence"] = []
    body["thin_rejections"] = []
    (tmp_path / REVIEW).write_text(json.dumps(body, indent=1), encoding="utf-8")
    forged = rd.row_programme_supervisor()
    assert forged.state == rd.FAIL
    assert "refuses its own record" in forged.value_


def test_the_row_asks_the_loader_and_not_the_file(tmp_path, monkeypatch):
    """The mechanism behind the case above, asserted directly so a refactor that
    reintroduces `body.get("complete")` fails here: a file claiming *fewer* blocking
    findings than it has must be refused rather than believed.

    The row reports the refusal, not a corrected count -- an earlier version of this
    docstring said "is reported with the real number", which the assertions below
    never checked and the row never did (O226/F-8)."""
    register, matrix = _tree(tmp_path, monkeypatch)
    body = _record(tmp_path, register, matrix,
                   findings=[_finding("F-1"), _finding("F-2", items=("P2-12",))])
    body["blocking_findings"] = ["F-1"]          # the file understates by one
    (tmp_path / REVIEW).write_text(json.dumps(body, indent=1), encoding="utf-8")
    row = rd.row_programme_supervisor()
    assert row.state == rd.FAIL
    assert "refuses its own record" in row.value_


# --------------------------------------------------------------------------- #
# C2 -- a review of some other phase
# --------------------------------------------------------------------------- #


def test_a_zero_finding_review_of_another_phase_does_not_close_p2(tmp_path,
                                                                 monkeypatch):
    """Catches the bypass that needed no editing at all, only the tool's own CLI:
    `--phase P1` with empty findings and decisions, written to P2's evidence path,
    used to print PASS because the row checked currency and nothing else. The
    snapshot seal is global across all eleven phases, so currency could not catch it."""
    register, matrix = _tree(tmp_path, monkeypatch)
    _record(tmp_path, register, matrix, phase="P1")
    row = rd.row_programme_supervisor()
    assert row.state == rd.FAIL
    assert "phase P1, not P2" in row.value_


def test_a_capsule_from_a_different_state_does_not_satisfy_the_row(tmp_path,
                                                                  monkeypatch):
    """The second half of the same binding: the record names the capsule it was
    taken from, and the row rebuilds that capsule from the live register and matrix.
    A record whose capsule digest does not match one rebuilt for P2 is a review of a
    board that no longer exists, even when its seal still agrees."""
    register, matrix = _tree(tmp_path, monkeypatch)
    body = _record(tmp_path, register, matrix)
    body["capsule_digest"] = "0" * 64
    (tmp_path / REVIEW).write_text(json.dumps(body, indent=1), encoding="utf-8")
    row = rd.row_programme_supervisor()
    assert row.state == rd.FAIL
    assert "not bound to the brief" in row.value_


# --------------------------------------------------------------------------- #
# C6 -- what the row puts in a markdown cell
# --------------------------------------------------------------------------- #


def test_a_multi_line_exception_becomes_one_bounded_line(tmp_path, monkeypatch):
    """Catches the defect that was live in the published board: a pydantic
    `ValidationError` is multi-line and `docs/READINESS.md` interpolates a row's
    value straight into a table cell, which left a row with no closing pipe, three
    stray lines in the table body, and a red ledger test."""
    register, matrix = _tree(tmp_path, monkeypatch)
    body = _record(tmp_path, register, matrix)
    body["not_a_field_of_this_model"] = True     # forces a ValidationError
    (tmp_path / REVIEW).write_text(json.dumps(body, indent=1), encoding="utf-8")
    row = rd.row_programme_supervisor()
    assert row.state == rd.FAIL
    assert "\n" not in row.value_ and "|" not in row.value_
    assert len(row.value_) < 400


def test_one_line_keeps_the_exception_type_and_bounds_the_message():
    """The helper on its own: the type is the useful half and must survive the
    bounding, or a truncated message tells the reader nothing about what failed."""
    try:
        json.loads("{oh no")
    except ValueError as exc:
        line = rd._one_line(exc, limit=40)
    assert line.startswith("JSONDecodeError") and len(line) == 40 and "\n" not in line


def test_a_register_less_tree_is_not_run_rather_than_fail(tmp_path, monkeypatch):
    """Catches an environment gap reported as a bad review: with no register there is
    no question to ask, and this file's own header says an unevaluable row is NOT_RUN.
    `row_capability_matrix` already answers this condition that way."""
    register, matrix = _tree(tmp_path, monkeypatch)
    _record(tmp_path, register, matrix)
    (tmp_path / REGISTER).rename(tmp_path / "program/v3_3/REQUIREMENTS.moved.json")
    row = rd.row_programme_supervisor()
    assert row.state == rd.NOT_RUN


# --------------------------------------------------------------------------- #
# C3 -- what it takes to reject a phase-level finding
# --------------------------------------------------------------------------- #


def test_a_one_character_rejection_does_not_discharge_a_phase_finding(tmp_path,
                                                                     monkeypatch):
    """Catches B3's repair being moved rather than closed: making a rejection the
    only way to settle `PHASE_NOT_READY` left `{"F1": "x"}` -- one character, signed
    by default as `programme-orchestrator`, the party whose phase is being held.

    Both variables move against the control here (length *and* actor), so the reason
    is asserted, not just the verdict -- otherwise this would pass for any other
    reason the row fails (O226/F-8). The two cases below isolate each variable."""
    register, matrix = _tree(tmp_path, monkeypatch)
    finding = _finding("F-1", kind="PHASE_NOT_READY", items=("P2",))
    _record(tmp_path, register, matrix, findings=[finding], decisions={"F-1": "x"})
    row = rd.row_programme_supervisor()
    assert row.state == rd.FAIL
    assert "rejection below the bar" in row.value_


def test_a_reasoned_rejection_by_someone_else_still_does_not_settle_it(tmp_path,
                                                                      monkeypatch):
    """Was the control for the thin-rejection rule; now it records the doctrine that
    replaced it (O229/G). A substantial reason from a named independent actor leaves
    the finding blocking -- a decision cannot settle one at all.

    What settles a finding is a fresh review over the current state that does not
    raise it. `test_a_review_that_found_nothing_over_the_whole_phase_passes` is the
    control that keeps this from meaning "the gate can never close"."""
    register, matrix = _tree(tmp_path, monkeypatch)
    finding = _finding("F-1", kind="PHASE_NOT_READY", items=("P2",))
    capsule = ps.capsule_for("P2", register=register, matrix=matrix)
    snapshot, _ev, _items = ps.reconciled(register, matrix)
    reason = ("P2's exit conditions were re-measured after the resumption loop closed "
              "and every phase-blocking item is VERIFIED on the committed tree; the "
              "finding described the state before that measurement")
    _preserve(tmp_path, register, matrix, [finding])
    record = ps.record_review("P2", capsule=capsule, findings=[finding],
                              snapshot=snapshot, decisions={"F-1": reason},
                              findings_source=RETURNED,
                              rejected_by="independent-supervisor-2")
    (tmp_path / REVIEW).write_text(record.model_dump_json(indent=1), encoding="utf-8")
    # O229/G: the rejection is well-formed, so it is not *thin* -- and it still does
    # not settle the finding, because no decision does any more. Five rounds showed
    # that every "a good enough decision settles it" rule is a rule about how much the
    # gated party has to type; this one was defeated by `--rejected-by me`.
    assert record.thin_rejections == ()
    assert record.blocking_findings == ("F-1",)
    assert rd.row_programme_supervisor().state == rd.FAIL


@pytest.mark.parametrize("actor", ["programme-orchestrator", " "])
def test_the_party_under_review_cannot_sign_off_its_own_phase(tmp_path, monkeypatch,
                                                              actor):
    """The actor half of the rule, isolated: the reason is identical to the accepted
    control and only the signature changes, so a future relaxation of the floor
    cannot quietly take this with it."""
    register, matrix = _tree(tmp_path, monkeypatch)
    finding = _finding("F-1", kind="PHASE_NOT_READY", items=("P2",))
    capsule = ps.capsule_for("P2", register=register, matrix=matrix)
    snapshot, _ev, _items = ps.reconciled(register, matrix)
    reason = ("P2's exit conditions were re-measured after the resumption loop closed "
              "and every phase-blocking item is VERIFIED on the committed tree; the "
              "finding described the state before that measurement")
    if not actor.strip():
        # This half is `supervisor.reject_finding`'s pre-existing blank-actor bar, not
        # the new rule -- O226/F-8 noted that the docstring implied otherwise, and that
        # the `""` member of PHASE_REJECTION_FORBIDDEN_ACTORS is therefore unreachable
        # through `record_review`. Kept because the bar is worth pinning; labelled so
        # nobody reads it as evidence for the actor rule.
        with pytest.raises(ValueError):
            ps.record_review("P2", capsule=capsule, findings=[finding],
                             snapshot=snapshot, decisions={"F-1": reason},
                             rejected_by=actor)
        return
    record = ps.record_review("P2", capsule=capsule, findings=[finding],
                             snapshot=snapshot, decisions={"F-1": reason},
                             rejected_by=actor)
    assert record.thin_rejections == ("F-1",)
    assert record.complete is False
    (tmp_path / REVIEW).write_text(record.model_dump_json(indent=1), encoding="utf-8")
    assert rd.row_programme_supervisor().state == rd.FAIL


def test_a_named_reviewer_still_cannot_discharge_a_phase_with_one_character(
        tmp_path, monkeypatch):
    """The floor half of the rule, isolated -- and it exists because mutation testing
    caught its absence: with the actor requirement in place, every case above still
    passed when the length floor was deleted, so the floor was undefended. Here the
    actor is beyond reproach and only the substance is missing."""
    register, matrix = _tree(tmp_path, monkeypatch)
    finding = _finding("F-1", kind="PHASE_NOT_READY", items=("P2",))
    capsule = ps.capsule_for("P2", register=register, matrix=matrix)
    snapshot, _ev, _items = ps.reconciled(register, matrix)
    record = ps.record_review("P2", capsule=capsule, findings=[finding],
                              snapshot=snapshot, decisions={"F-1": "x"},
                              rejected_by="independent-supervisor-2")
    assert record.thin_rejections == ("F-1",)
    assert record.complete is False
    (tmp_path / REVIEW).write_text(record.model_dump_json(indent=1), encoding="utf-8")
    assert rd.row_programme_supervisor().state == rd.FAIL


# --------------------------------------------------------------------------- #
# O226/E1 -- `validation_failures` is an input to `complete`, so it is earned
# --------------------------------------------------------------------------- #


def test_an_emptied_validation_failures_array_does_not_make_a_record_complete(
        tmp_path, monkeypatch):
    """Catches the fourth bypass, and it is C1 one field over: `complete` has two
    inputs and the loader recomputed one of them. `validation_failures` could not be
    recomputed without a snapshot, so a record with that array emptied by hand loaded
    clean and the board printed PASS -- the row read the verdict out of the file
    again, just not the field C1 named."""
    register, matrix = _tree(tmp_path, monkeypatch)
    bogus = _finding("F-1", items=("P2-99-does-not-exist",))
    body = _record(tmp_path, register, matrix, findings=[bogus])
    assert body["validation_failures"], "the product must refuse this finding"
    honest = rd.row_programme_supervisor()
    assert honest.state == rd.FAIL

    body["validation_failures"] = []
    for field in ps.COMPUTED_FIELDS:
        body.pop(field, None)                      # omitted, so nothing disagrees
    (tmp_path / REVIEW).write_text(json.dumps(body, indent=1), encoding="utf-8")
    assert rd.row_programme_supervisor().state == rd.FAIL


def test_a_record_carrying_a_finding_about_an_unknown_item_is_refused(tmp_path,
                                                                     monkeypatch):
    """The other half: a hand-written record can put a refused finding straight into
    `findings` rather than into `validation_failures`. With the snapshot, the loader
    puts every stored finding back through `supervisor.validate`, which is what
    produced the field in the first place."""
    register, matrix = _tree(tmp_path, monkeypatch)
    snapshot, _ev, _items = ps.reconciled(register, matrix)
    body = _record(tmp_path, register, matrix)
    body["findings"] = [{**_finding("F-1", items=("P2-99-does-not-exist",)),
                         "status": "OPEN"}]
    body["applied"] = ["F-1"]
    body["repair_evidence"] = {"F-1": "tools/preserve.py:1"}
    for field in ps.COMPUTED_FIELDS:
        body.pop(field, None)
    with pytest.raises(ValueError, match="the product refuses"):
        ps.load_record(body, snapshot=snapshot)
    # Control: without a snapshot the loader cannot tell that the finding is about an
    # item no board holds, and says so in its docstring rather than pretending -- which
    # is why a gating caller must pass one. It loads, and the finding still blocks,
    # because under O229/G nothing but a fresh review settles a finding.
    loaded = ps.load_record(body)
    assert loaded.complete is False
    assert loaded.blocking_findings == ("F-1",)


# --------------------------------------------------------------------------- #
# O226/E2 -- the record is bound to what the supervisor returned
# --------------------------------------------------------------------------- #


def test_a_review_with_nothing_preserved_does_not_satisfy_the_row(tmp_path,
                                                                 monkeypatch):
    """The gate reads the preserved artefacts and nothing else, so a record with no
    preserved review behind it is not a satisfied condition -- however complete the
    record says it is. O227/G2: the previous version asked the *record* where to look,
    which let a record name a file in /tmp, a path outside the repo, or itself."""
    register, matrix = _tree(tmp_path, monkeypatch)
    _record(tmp_path, register, matrix, source=None, preserve=False)
    row = rd.row_programme_supervisor()
    assert row.state == rd.FAIL
    assert "no directory under" in row.value_


def test_deleting_the_inconvenient_findings_is_caught_by_the_digest(tmp_path,
                                                                   monkeypatch):
    """Catches the bypass that needed no editing of any computed field: hand
    `--findings` the real set with the blocking ones removed. The preserved source
    still holds them, so the digests disagree and the row names which file moved."""
    register, matrix = _tree(tmp_path, monkeypatch)
    everything = [_finding("F-1"), _finding("F-2", items=("P2-12",)),
                  _finding("F-3", items=("P2-13",))]
    capsule = _preserve(tmp_path, register, matrix, everything)
    snapshot, _ev, _items = ps.reconciled(register, matrix)
    # The record is built over one of the three; the preserved file keeps all three.
    record = ps.record_review("P2", capsule=capsule, findings=everything[:1],
                              snapshot=snapshot, decisions={},
                              findings_source=RETURNED)
    (tmp_path / REVIEW).write_text(record.model_dump_json(indent=1), encoding="utf-8")
    row = rd.row_programme_supervisor()
    assert row.state == rd.FAIL
    assert "no longer digest to" in row.value_


def test_a_review_that_found_nothing_over_the_whole_phase_passes(tmp_path,
                                                                monkeypatch):
    """The positive control for everything in this file. Under O229/G a finding is
    settled only by a fresh review that does not raise it, so the shape that closes
    this gate is: a brief cut from *this* board, covering the *whole* phase, and a
    supervisor that returned nothing. If this test fails the gate has become one that
    can never close -- which this programme has now shipped three times (D1, E10, and
    the seal comparison in `_capsule_binding_problems`)."""
    register, matrix = _tree(tmp_path, monkeypatch)
    # A review that found nothing over the whole current phase: the one shape that
    # satisfies this gate, and the reason the rules above are not "never closes".
    _record(tmp_path, register, matrix, findings=[])
    assert rd.row_programme_supervisor().state == rd.PASS


def test_the_findings_digest_ignores_order_but_not_content():
    """The digest has to survive a supervisor returning the same findings in another
    order -- otherwise the binding would report forgery on an honest re-serialisation
    -- and must not survive one finding being altered."""
    a, b = _finding("F-1"), _finding("F-2", items=("P2-12",))
    assert ps.findings_digest([a, b]) == ps.findings_digest([b, a])
    altered = {**b, "severity": "low"}
    assert ps.findings_digest([a, b]) != ps.findings_digest([a, altered])
    assert ps.findings_digest([a, b]) != ps.findings_digest([a])


# --------------------------------------------------------------------------- #
# O226/E3, E4 -- the two one-edit evasions of the round-three repairs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("actor", ["Programme-Orchestrator", "PROGRAMME-ORCHESTRATOR",
                                   "programme_orchestrator", "programme-orchestrator2",
                                   " programme orchestrator "])
def test_the_defendant_cannot_sign_off_its_own_phase_by_respelling_its_name(
        tmp_path, monkeypatch, actor):
    """Catches the actor rule being defeated by the shift key. The set was compared
    for exact equality, so every spelling here passed while `programme-orchestrator`
    was refused -- and the audit trail then read the defendant's own name beside a
    green gate. Comparison is now over a case-folded, punctuation-stripped form, and
    it is containment, so a digit stuck on the end is still the same party."""
    register, matrix = _tree(tmp_path, monkeypatch)
    finding = _finding("F-1", kind="PHASE_NOT_READY", items=("P2",))
    capsule = ps.capsule_for("P2", register=register, matrix=matrix)
    snapshot, _ev, _items = ps.reconciled(register, matrix)
    reason = ("P2's exit conditions were re-measured after the resumption loop closed "
              "and every phase-blocking item is VERIFIED on the committed tree; the "
              "finding described the state before that measurement")
    record = ps.record_review("P2", capsule=capsule, findings=[finding],
                              snapshot=snapshot, decisions={"F-1": reason},
                              rejected_by=actor)
    assert record.thin_rejections == ("F-1",)
    assert record.complete is False


@pytest.mark.parametrize("evidence", [".", "x", "the repair is done, trust me",
                                      "..............................", "defaced"])
def test_a_repair_evidence_that_names_nothing_settles_nothing(evidence):
    """Was the shape check's negative case. Kept because `applied_without_evidence`
    still reports on the quality of a decision, and dropped from load-bearing duty by
    O227/G3: `"0.0"` passed this check, two characters more than the `"."` it was
    written to refuse. Shape is evaded by anything with a dot in it."""
    assert ps.evidence_reference_problems(evidence) != []


@pytest.mark.parametrize("evidence", [
    "tools/preserve.py", "tools/preserve.py:41", "docs/READINESS.md#gate",
    "tests/test_preserve.py::test_the_policy_is_loaded", "O225", "O226/E4",
    "a6feda41",
    "tools/capability_matrix.py equivalent cap + tests/test_capability_caps.py::test_x",
])
def test_the_control_is_every_shape_a_real_reference_takes(evidence):
    """Control for the case above, and it is the half that matters: the first version
    of this check required the whole field to *be* one reference and duly refused all
    five of this programme's own honest dispositions. A check that rejects the real
    thing teaches people to route around it, so the field may be a sentence that
    contains a reference."""
    assert ps.evidence_reference_problems(evidence) == []


# --------------------------------------------------------------------------- #
# O226/E5 -- the five behaviours that no test defended
# --------------------------------------------------------------------------- #


def test_an_apply_never_settles_a_phase_level_finding_even_with_evidence(tmp_path,
                                                                        monkeypatch):
    """Repair #4 of round three, which mutation testing found defended by **nothing**
    on the full suite -- and it is load-bearing: with the branch reverted, an apply
    with any evidence string on a critical `PHASE_NOT_READY` gave a complete review.
    A phase is ready when a fresh review over the changed state says so, not when the
    previous review's author says the work is done."""
    register, matrix = _tree(tmp_path, monkeypatch)
    finding = _finding("F-1", kind="PHASE_NOT_READY", items=("P2",))
    body = _record(tmp_path, register, matrix, findings=[finding],
                   decisions={"F-1": _applied_ref()})
    assert body["blocking_findings"] == ["F-1"]
    assert rd.row_programme_supervisor().state == rd.FAIL


def test_the_row_reports_a_stale_review_as_stale(tmp_path, monkeypatch):
    """The row's currency check, which had no test although "a stale review" is one of
    the four failure modes its docstring advertises -- `stale_against` the function
    was tested, `stale_against` as used by the gate was not (O226/E5)."""
    register, matrix = _tree(tmp_path, monkeypatch)
    _record(tmp_path, register, matrix)
    assert rd.row_programme_supervisor().state == rd.PASS
    moved = {**matrix, "capabilities": [{**c, "status": "MISSING"}
                                        for c in matrix["capabilities"]]}
    (tmp_path / MATRIX).write_text(json.dumps(moved), encoding="utf-8")
    row = rd.row_programme_supervisor()
    assert row.state == rd.FAIL
    assert "changed" in row.value_


def test_the_capsule_limits_are_recorded_so_a_rebuild_is_exact(tmp_path, monkeypatch):
    """There is exactly **one** brief this gate accepts for a board, and it derives it.

    O228/J1, J2, J8 and S1 were four spellings of one mistake: the row *read* the brief's
    coverage off `selection.left_out`, whose only validator checks its own arithmetic, and
    never read its phase at all. So a one-item capsule rewritten to claim `considered=1,
    included=1, left_out=0` passed as a whole-phase brief, and `--max-evidence 1` gave an
    honest `left_out == 0` brief with zero evidence summaries. Non-default limits are
    therefore a *different brief* now, whatever the brief says about itself."""
    register, matrix = _tree(tmp_path, monkeypatch)
    body = _record(tmp_path, register, matrix, findings=[])
    assert body["capsule_max_items"] and body["capsule_max_evidence"]
    assert rd.row_programme_supervisor().state == rd.PASS

    for limits in ({"max_items": 1, "max_evidence": 1},
                   {"max_evidence": 1},
                   {"max_summary_chars": 400}):
        _record(tmp_path, register, matrix, findings=[], limits=limits)
        row = rd.row_programme_supervisor()
        assert row.state == rd.FAIL, limits
        assert "is not the brief this gate would put to a supervisor" in row.value_


def test_every_computed_field_is_compared_not_just_the_first_three(tmp_path,
                                                                  monkeypatch):
    """Catches the checking list falling behind the model: `COMPUTED_FIELDS` had four
    members and `load_record` had three hand-rolled comparisons, so the field C3 added
    was stripped and discarded. Both round-four reviewers found this independently."""
    register, matrix = _tree(tmp_path, monkeypatch)
    finding = _finding("F-1", kind="PHASE_NOT_READY", items=("P2",))
    capsule = ps.capsule_for("P2", register=register, matrix=matrix)
    snapshot, _ev, _items = ps.reconciled(register, matrix)
    record = ps.record_review("P2", capsule=capsule, findings=[finding],
                              snapshot=snapshot, decisions={"F-1": "x"})
    assert record.thin_rejections == ("F-1",)
    body = json.loads(record.model_dump_json())
    for field in ps.COMPUTED_FIELDS:
        true_value = getattr(record, field)
        forged = dict(body)
        # A forgery has to *differ*: an earlier version of this loop wrote `[]` into
        # every tuple field, and for the two that are legitimately empty it forged the
        # right answer and then asserted the loader would object to it.
        if isinstance(true_value, tuple):
            forged[field] = [] if true_value else ["A-FINDING-THAT-IS-NOT-THERE"]
        else:
            forged[field] = not true_value
        with pytest.raises(ValueError, match=field):
            ps.load_record(forged, snapshot=snapshot)


def test_the_detail_line_names_the_cause_and_not_only_the_verdict(tmp_path,
                                                                 monkeypatch):
    """O226/E7. A record every one of whose findings the product refused printed
    "0 finding(s), 0 undecided, INCOMPLETE" -- the right verdict with its cause
    missing -- and a thin rejection read as though nobody had decided it. The board is
    the surface this programme publishes as its status, so the cell has to say why."""
    register, matrix = _tree(tmp_path, monkeypatch)
    finding = _finding("F-1", kind="PHASE_NOT_READY", items=("P2",))
    _record(tmp_path, register, matrix, findings=[finding], decisions={"F-1": "x"})
    assert "rejection below the bar" in rd.row_programme_supervisor().value_

    bogus = _finding("F-2", items=("P2-99-does-not-exist",))
    _record(tmp_path, register, matrix, findings=[bogus])
    assert "refused by validate" in rd.row_programme_supervisor().value_


def test_a_pipe_in_an_exception_message_cannot_break_the_table(tmp_path, monkeypatch):
    """O226/F-7. `_one_line` collapsed whitespace and left the pipe alone, and the
    test that claimed the cell was safe passed only because that particular error
    happened to contain none. A refused record with a pipe in a key is enough."""
    register, matrix = _tree(tmp_path, monkeypatch)
    body = _record(tmp_path, register, matrix)
    body["a | b"] = True                           # forces a ValidationError naming it
    (tmp_path / REVIEW).write_text(json.dumps(body, indent=1), encoding="utf-8")
    row = rd.row_programme_supervisor()
    assert row.state == rd.FAIL
    assert "|" not in row.value_ and "\n" not in row.value_


def test_the_row_re_validates_the_findings_and_not_only_their_accounting(tmp_path,
                                                                       monkeypatch):
    """Isolates the snapshot the row hands the loader, which mutation testing found
    undefended: with the accounting check in place, dropping `snapshot=` from the
    loader call changed no test, so re-validation itself was unprotected.

    Here the returned source *does* account for the finding, so the digest and the
    accounting both pass, and the only thing left that can notice the finding is about
    an item no snapshot contains is `supervisor.validate` -- which only runs if the row
    passes the snapshot."""
    register, matrix = _tree(tmp_path, monkeypatch)
    bogus = {**_finding("F-1", items=("P2-99-does-not-exist",)), "status": "OPEN"}
    (tmp_path / RETURNED).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / RETURNED).write_text(json.dumps([bogus]), encoding="utf-8")

    honest = _record(tmp_path, register, matrix, findings=[bogus])
    body = dict(honest)
    body["findings"] = [bogus]                     # moved out of validation_failures
    body["validation_failures"] = []
    body["applied"] = ["F-1"]
    body["repair_evidence"] = {"F-1": "tools/preserve.py:1"}
    for field in ps.COMPUTED_FIELDS:
        body.pop(field, None)
    (tmp_path / REVIEW).write_text(json.dumps(body, indent=1), encoding="utf-8")
    row = rd.row_programme_supervisor()
    assert row.state == rd.FAIL
    assert "refuses" in row.value_


def test_an_applied_finding_whose_evidence_names_nothing_stays_blocking(tmp_path,
                                                                      monkeypatch):
    """The shape check as the *record* applies it, not as a helper function: mutation
    testing showed that reverting `blocking_findings` to a bare `.strip()` broke no
    test, because every case for the shapes called `evidence_reference_problems`
    directly. This is the path O226/E4 actually walked -- thirteen findings applied
    with `"."` and a board printing complete."""
    register, matrix = _tree(tmp_path, monkeypatch)
    # O227/G3, the case that ended the shape-checking approach: `"0.0"` satisfied the
    # path pattern, and so did `e.g.` and any decimal in a sentence -- two characters
    # more than the `"."` the previous round repaired. The evidence is still recorded
    # and still reported as thin; it settles nothing, so there is nothing in a
    # decisions file left worth forging.
    for evidence in (".", "0.0", "e.g.", "tools/preserve.py:1"):
        body = _record(tmp_path, register, matrix, findings=[_finding("F-1")],
                       decisions={"F-1": {"disposition": "apply",
                                          "evidence": evidence}})
        assert body["blocking_findings"] == ["F-1"], evidence
        assert body["applied"] == ["F-1"], evidence
        assert rd.row_programme_supervisor().state == rd.FAIL, evidence


def test_the_matrix_never_says_a_module_is_unused_only_that_nothing_names_it():
    """Pins the wording O225/C4 corrected, because the sentence survived one
    "correction" that never happened: the scripted edit matched no anchor and reported
    success, so the ledger recorded a repair that was not in the tree (O226/F-4).

    In the style of `tests/test_capability_caps.py`, which greps for the old positive
    wording. A text scan can report that nothing *names* a module; it cannot report
    that nothing reaches it, because a dynamic load is invisible to it."""
    source = (HOH / "tools/capability_matrix.py").read_text(encoding="utf-8")
    offending = [line for line in source.splitlines()
                 if "used by the product" in line or "does not use it" in line]
    # The only permitted occurrence is the correction quoting the wording it replaced.
    assert all("an earlier form read" in line for line in offending), offending

    register = (HOH / "program/v3_3/REQUIREMENTS.json").read_text(encoding="utf-8")
    assert "the product does not use it" not in register


# --------------------------------------------------------------------------- #
# O228/J2, J3, J4, J5 -- what the brief itself may not be trusted about
# --------------------------------------------------------------------------- #


def test_a_brief_for_another_phase_does_not_close_p2(tmp_path, monkeypatch):
    """Catches bypass #3 resurrected one layer out (O228/J2): the row read the *record's*
    phase and never the brief's, so a brief covering eighteen P1 items and no P2 item
    closed P2 once the record's own `phase` field was edited -- and `load_record` believes
    that field. The brief is derived now, so its phase is not a claim."""
    register, matrix = _tree(tmp_path, monkeypatch)
    _preserve(tmp_path, register, matrix, [], phase="P1", limits={"max_items": 50})
    snapshot, _ev, _items = ps.reconciled(register, matrix)
    capsule = ps.capsule_for("P1", register=register, matrix=matrix)
    record = ps.record_review("P1", capsule=capsule, findings=[], snapshot=snapshot,
                              decisions={}, findings_source=RETURNED)
    body = json.loads(record.model_dump_json())
    body["phase"] = "P2"
    for field in ps.COMPUTED_FIELDS:
        body.pop(field, None)
    (tmp_path / REVIEW).write_text(json.dumps(body, indent=1), encoding="utf-8")
    row = rd.row_programme_supervisor()
    assert row.state == rd.FAIL
    assert "is not the brief this gate would put to a supervisor" in row.value_


def test_a_critical_no_action_is_not_a_finding_of_nothing(tmp_path, monkeypatch):
    """Catches O228/J3, which is round three's lesson reintroduced by the rule that
    replaced it: `NO_ACTION` was the single non-blocking kind and `supervisor.validate`
    refuses it only for proposing an action, so relabelling cost `kind -> NO_ACTION` and
    `proposed_action -> null` while `severity` and the entire `reason` survived verbatim.
    A reviewer relabelled all fourteen and the board printed `0 undecided, complete` with
    the critical text still in the file."""
    register, matrix = _tree(tmp_path, monkeypatch)
    relabelled = {**_finding("F-1"), "kind": "NO_ACTION", "proposed_action": None,
                  "severity": "critical"}
    body = _record(tmp_path, register, matrix, findings=[relabelled])
    assert body["blocking_findings"] == ["F-1"]
    assert rd.row_programme_supervisor().state == rd.FAIL


def test_the_control_is_a_no_action_whose_severity_agrees_with_it(tmp_path, monkeypatch):
    """Control for the case above: a supervisor saying "nothing here" at a severity that
    means it, which is a complete answer and closes. Without this the rule would be "a
    NO_ACTION can never be an answer", which is not the rule."""
    register, matrix = _tree(tmp_path, monkeypatch)
    honest = {**_finding("F-1"), "kind": "NO_ACTION", "proposed_action": None,
              "severity": "low"}
    body = _record(tmp_path, register, matrix, findings=[honest])
    assert body["blocking_findings"] == []
    assert rd.row_programme_supervisor().state == rd.PASS


def test_an_unstamped_directory_cannot_shadow_a_preserved_review(tmp_path, monkeypatch):
    """Catches O228/J4: the rule was "newest by name" with no shape required, and ASCII
    puts every lowercase name after every timestamp -- so `zzzz`, `draft` or `staging` won
    permanently, and a *draft* of the next review won the moment it held both files."""
    register, matrix = _tree(tmp_path, monkeypatch)
    _record(tmp_path, register, matrix, findings=[_finding("F-1")])
    for name in ("zzzz", "draft", "staging", "notes"):
        shadow = tmp_path / f"dogfood/p2-gate/{name}"
        shadow.mkdir(parents=True, exist_ok=True)
        (shadow / "capsule-p2.json").write_text(
            (tmp_path / CAPSULE).read_text(encoding="utf-8"), encoding="utf-8")
        (shadow / "findings.json").write_text("[]", encoding="utf-8")
    chosen, problems = rd._canonical_review(tmp_path)
    assert problems == [] and chosen.name == STAMP
    row = rd.row_programme_supervisor()
    assert row.state == rd.FAIL                     # the real review still holds
    assert f"review {STAMP}" in row.value_          # and the cell names which one


def test_the_cell_names_the_review_it_read_on_every_outcome(tmp_path, monkeypatch):
    """The other half of J4: four different directories produced byte-identical PASS
    cells, because the name was printed only inside failure messages. On the one outcome
    that matters the board said nothing about what it had read."""
    register, matrix = _tree(tmp_path, monkeypatch)
    _record(tmp_path, register, matrix, findings=[])
    row = rd.row_programme_supervisor()
    assert row.state == rd.PASS
    assert f"review {STAMP}" in row.value_


def test_a_register_nothing_vouches_for_is_named(tmp_path, monkeypatch):
    """Catches O228/J5, a composition gap rather than a forgery: deleting 31 of the 32 P2
    requirements yields an *honest* whole-phase brief over one item and an honest empty
    review of it. The row that would notice is `row_program_scope`, NOT_RUN on this
    machine because the pinned plan is unreachable -- so the gate rested on a register
    nothing vouched for, and inherited the silence."""
    register, matrix = _tree(tmp_path, monkeypatch)
    _record(tmp_path, register, matrix, findings=[])
    assert rd.row_programme_supervisor().state == rd.PASS
    monkeypatch.setattr(rd, "row_program_scope",
                        lambda: rd.Row("program_scope", rd.NOT_RUN, "plan unreachable",
                                       "n/a", "n/a"))
    row = rd.row_programme_supervisor()
    assert row.state == rd.FAIL
    assert "is not vouched for" in row.value_


def test_a_finding_is_open_until_a_later_review_resolves_it(tmp_path, monkeypatch):
    """O229/L2, and it replaces a test that depended on the hole it is about.

    The previous design put the open findings *in the brief*, taken from the newest earlier
    review -- so any single review that honestly returned `[]` erased every earlier finding
    from every later brief, permanently, with no adversary involved. Worse, the only test of
    that repair built its earlier review with `findings.json` alone, which is exactly the
    shape that made a directory invisible as a review and decisive as history: the test did
    not merely fail to catch the hole, it *depended* on it.

    Findings accumulate across every preserved review now. An added directory can raise or
    resolve; it cannot forget.
    """
    raised = [("20260924T0030Z", [_finding("F-1"), _finding("F-2", items=("P2-12",))])]
    still_open, resolved = ps.resolution_state(raised)
    assert sorted(still_open) == ["F-1", "F-2"] and resolved == []

    # An honest empty review afterwards settles nothing -- the chain-of-one defect.
    with_empty = [*raised, ("20260925T0000Z", [])]
    still_open, resolved = ps.resolution_state(with_empty)
    assert sorted(still_open) == ["F-1", "F-2"] and resolved == []

    # Only a later review that looks again and finds nothing *there* resolves it.
    quiet = {**_finding("F-1"), "kind": "NO_ACTION", "proposed_action": None,
             "severity": "low"}
    with_resolution = [*raised, ("20260926T0000Z", [quiet])]
    still_open, resolved = ps.resolution_state(with_resolution)
    assert sorted(still_open) == ["F-2"] and resolved == ["F-1"]

    # And a finding raised again after a resolution is open again.
    reraised = [*with_resolution, ("20260927T0000Z", [_finding("F-1")])]
    still_open, resolved = ps.resolution_state(reraised)
    assert sorted(still_open) == ["F-1", "F-2"] and resolved == []


def test_the_accumulation_does_not_depend_on_the_order_it_is_given(tmp_path):
    """The property that makes accumulation safe: the stamps decide, not the argument
    order, so a caller that lists directories differently cannot change the verdict."""
    quiet = {**_finding("F-1"), "kind": "NO_ACTION", "proposed_action": None,
             "severity": "low"}
    forward = [("20260924T0030Z", [_finding("F-1")]), ("20260926T0000Z", [quiet])]
    assert ps.resolution_state(forward) == ps.resolution_state(list(reversed(forward)))


def test_a_four_digit_stamp_does_not_outrank_a_later_six_digit_one(tmp_path):
    """O229/L8: `'Z'` sorts after every digit, so `20260925T1015Z` compared *greater* than
    `20260925T101500Z` -- a four-digit directory permanently outranked a genuine review
    preserved seconds later, and both are valid stamps so the shape check did not notice."""
    # `T1015Z` *is* 10:15:00, so the two are the same instant and compare equal -- which
    # is the right semantics and is what the raw string comparison got wrong.
    assert ps._stamp_key("20260925T1015Z") == ps._stamp_key("20260925T101500Z")
    assert ps._stamp_key("20260925T1015Z") < ps._stamp_key("20260925T101501Z")
    assert ps._stamp_key("20260925T1015Z") < ps._stamp_key("20260925T101559Z")
    assert ps._stamp_key("20260924T2359Z") < ps._stamp_key("20260925T0000Z")
    # The defect, for the record: raw ASCII put the four-digit name *after* the six.
    assert not ("20260925T1015Z" < "20260925T101559Z")


def test_the_canonical_brief_is_the_one_the_cli_prints(tmp_path, monkeypatch):
    """O229/L7 and M2, the fourth gate that could never close: the gate had a private
    derivation while the CLI built something else, so the brief the gate demanded could not
    be produced by any command in the tree. A reviewer proved it by performing the review
    and being unable to preserve it. One derivation now, and `--canonical-brief` prints it."""
    register, matrix = _tree(tmp_path, monkeypatch)
    from_gate = rd._canonical_brief(ps, register=register, matrix=matrix)
    # The same review root, because the brief describes a tree and the gate inspects the
    # tree it is *told* about. Passing none made the tool read this checkout while the gate
    # read `tmp_path`, so the two disagreed -- which this test caught.
    from_tool = ps.canonical_brief(register=register, matrix=matrix, phase="P2",
                                   review_root=tmp_path / rd.REVIEW_DIR)
    assert ps.capsule_digest(from_gate) == ps.capsule_digest(from_tool)
    # It covers the phase and carries evidence for every item of it (O229/L11).
    assert from_tool.selection.left_out == 0
    assert from_tool.selection.included == from_tool.selection.considered
    assert len(from_tool.evidence_summaries) > from_tool.selection.included


def test_the_brief_counts_the_phase_by_the_field_the_selection_uses(tmp_path,
                                                                   monkeypatch):
    """O229/L5: the gate counted items with `item_id.startswith("P2")` while `capsule_for`
    selects on `item.phase`, and those are two independent register fields -- so renaming
    two requirement ids made the derivation cover 30 of 32 items, and the three-way digest
    identity then made the under-coverage *canonical*, with nothing left to compare against.
    """
    register, matrix = _tree(tmp_path, monkeypatch)
    renamed = dict(register)
    rows = []
    for row in register["requirements"]:
        row = dict(row)
        if row["id"] in ("P2-31", "P2-32"):
            row["id"] = row["id"].replace("P2", "Q2")      # target_phase untouched
        rows.append(row)
    renamed["requirements"] = rows
    brief = ps.canonical_brief(register=renamed, matrix=matrix, phase="P2")
    assert brief.selection.left_out == 0, "the phase is counted by target_phase, not by id"


def test_the_brief_shows_the_supervisor_every_open_finding(tmp_path, monkeypatch):
    """O231/N2, and it is the finding that closed the loop this gate was built to run.

    Removing carry-forward to fix O229/L2 left the supervisor unable to see what was open,
    so the open ids reached it only as **prose in a dispatch written by the party under
    review** -- the one input deciding the gate's outcome sitting outside both the record
    and the derivation. Accumulation makes carrying them safe, and because they are in the
    brief they are inside the digest the gate compares: a brief showing fewer open findings
    than the tree holds is not the canonical brief.
    """
    register, matrix = _tree(tmp_path, monkeypatch)
    root = tmp_path / rd.REVIEW_DIR
    (root / "20260924T0030Z").mkdir(parents=True, exist_ok=True)
    (root / "20260924T0030Z/findings.json").write_text(
        json.dumps([_finding("F-1"), _finding("F-2", items=("P2-12",))]), encoding="utf-8")

    brief = ps.canonical_brief(register=register, matrix=matrix, phase="P2",
                               review_root=root)
    assert sorted(f.finding_id for f in brief.open_findings) == ["F-1", "F-2"]

    # A brief that hides one of them is not canonical, so the gate refuses it.
    narrow = ps.capsule_for("P2", register=register, matrix=matrix,
                            max_items=32, max_evidence=33)
    assert narrow.open_findings == []
    assert ps.capsule_digest(narrow) != ps.capsule_digest(brief)

    # And a later review that resolves one shrinks the next brief by exactly that one.
    quiet = {**_finding("F-1"), "kind": "NO_ACTION", "proposed_action": None,
             "severity": "low"}
    (root / "20260925T0000Z").mkdir(parents=True, exist_ok=True)
    (root / "20260925T0000Z/findings.json").write_text(json.dumps([quiet]), encoding="utf-8")
    after = ps.canonical_brief(register=register, matrix=matrix, phase="P2",
                               review_root=root)
    assert [f.finding_id for f in after.open_findings] == ["F-2"]


def test_a_carried_finding_the_snapshot_cannot_hold_is_left_out(tmp_path, monkeypatch):
    """The availability half, which is the shape this family of defects keeps taking (D1,
    E10, the seal check, O229/L6, O229/M2 -- five never-closing gates). `ReviewCapsule`
    refuses a capsule holding a finding that names an item the snapshot does not have, so
    carrying one unfiltered would make the brief underivable and the gate unclosable."""
    register, matrix = _tree(tmp_path, monkeypatch)
    root = tmp_path / rd.REVIEW_DIR
    (root / "20260924T0030Z").mkdir(parents=True, exist_ok=True)
    (root / "20260924T0030Z/findings.json").write_text(
        json.dumps([_finding("F-1"), _finding("F-9", items=("NO-SUCH-ITEM",))]),
        encoding="utf-8")
    brief = ps.canonical_brief(register=register, matrix=matrix, phase="P2",
                               review_root=root)
    assert [f.finding_id for f in brief.open_findings] == ["F-1"]


# --------------------------------------------------------------------------- #
# supervisor-followups: T-PSD1 and T-PSD4 in the gate
# --------------------------------------------------------------------------- #


def _two_artefacts(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "findings.json").write_text("[]", encoding="utf-8")
    (directory / "capsule-p2.json").write_text("{}", encoding="utf-8")
    return directory


@pytest.mark.parametrize("reverse", [False, True])
def test_tied_newest_review_stamps_are_ambiguous_whatever_the_listing_order(
        tmp_path, monkeypatch, reverse):
    """Catches T-PSD1: `_canonical_review` sorted by parsed time and took the last, so
    two newest directories at the same instant (`T1015Z`, `T101500Z`) were told apart by
    `iterdir` order -- the filesystem chose the review the gate read. Under either
    listing order the gate now names both as ambiguous and reads neither. Controls: one
    second apart, the later one is chosen in either order; and an *older* tied pair
    under a unique newest review does not matter."""
    listing = Path.iterdir
    monkeypatch.setattr(Path, "iterdir",
                        lambda self: iter(sorted(listing(self), reverse=reverse)))
    root = tmp_path / rd.REVIEW_DIR
    _two_artefacts(root / "20260925T1015Z")
    tied = _two_artefacts(root / "20260925T101500Z")
    chosen, problems = rd._canonical_review(tmp_path)
    assert chosen is None
    assert problems == [
        "ambiguous review: 20260925T101500Z and 20260925T1015Z are the newest under "
        "dogfood/p2-gate at the same time, so the gate cannot tell which one is current"]

    tied.rename(root / "20260925T101501Z")
    assert rd._canonical_review(tmp_path) == (root / "20260925T101501Z", [])

    _two_artefacts(root / "20260924T0030Z")
    _two_artefacts(root / "20260924T003000Z")
    assert rd._canonical_review(tmp_path) == (root / "20260925T101501Z", [])


def test_a_review_root_outside_the_repository_is_not_the_gates(tmp_path, monkeypatch):
    """Catches T-PSD4 in the gate: `_is_inside` checks each review directory only
    against `root.resolve()`, so a symlinked `dogfood/p2-gate` leading out of the
    repository gave the gate a foreign review to bind to and foreign findings to
    accumulate. The root is checked against the repository first: `_canonical_review`
    names the problem and chooses nothing, and `_preserved_reviews` holds nothing.
    Control: the same review under a real `dogfood/p2-gate` is chosen and read."""
    foreign = {**_finding("F-FOREIGN")}
    outside = tmp_path / "outside/gate"
    held = _two_artefacts(outside / STAMP)
    (held / "findings.json").write_text(json.dumps([foreign]), encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / "dogfood").mkdir(parents=True)
    (repo / "dogfood/p2-gate").symlink_to("../../outside/gate")
    monkeypatch.setattr(rd, "HOH", repo)
    assert rd._canonical_review(repo) == (None, [
        "dogfood/p2-gate resolves outside the repository, so no review under it is "
        "this tree's"])
    assert rd._preserved_reviews(ps) == []

    # Revision 2a item 1: a symlinked `dogfood` leads the root out just the same, though
    # `dogfood/p2-gate` itself is a real directory behind it. A root check that refused
    # only a symlinked root would read this complete foreign review.
    whole = _two_artefacts(tmp_path / "outside/dogfood/p2-gate" / STAMP)
    (whole / "findings.json").write_text(json.dumps([foreign]), encoding="utf-8")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "dogfood").symlink_to("../outside/dogfood")
    monkeypatch.setattr(rd, "HOH", linked)
    assert rd._canonical_review(linked) == (None, [
        "dogfood/p2-gate resolves outside the repository, so no review under it is "
        "this tree's"])
    assert rd._preserved_reviews(ps) == []

    control = tmp_path / "control"
    real = _two_artefacts(control / rd.REVIEW_DIR / STAMP)
    (real / "findings.json").write_text(json.dumps([foreign]), encoding="utf-8")
    monkeypatch.setattr(rd, "HOH", control)
    assert rd._canonical_review(control) == (real, [])
    assert [(n, [f["finding_id"] for f in fs]) for n, fs in rd._preserved_reviews(ps)] == [
        (STAMP, ["F-FOREIGN"])]


def test_tied_newest_reviews_fail_the_row_and_name_the_ambiguity(tmp_path, monkeypatch):
    """T-PSD1 at the row: a complete, current review and a byte-identical twin under the
    same instant (`T120000Z`, `T1200Z`) passed, bound to whichever directory `iterdir`
    listed last. The row fails and says why. Control: the positive control of this file
    (`test_a_complete_current_p2_review_passes`) passes with the single review."""
    register, matrix = _tree(tmp_path, monkeypatch)
    _record(tmp_path, register, matrix, findings=[])
    assert rd.row_programme_supervisor().state == rd.PASS
    twin = tmp_path / "dogfood/p2-gate" / (STAMP[:-3] + "Z")
    shutil.copytree(tmp_path / "dogfood/p2-gate" / STAMP, twin)
    row = rd.row_programme_supervisor()
    assert row.state == rd.FAIL
    assert "ambiguous review: 20260924T120000Z and 20260924T1200Z" in row.value_


def test_a_review_root_outside_the_repository_fails_the_row(tmp_path, monkeypatch):
    """T-PSD4 at the row (sfspec-a M1): the row reads `dogfood/p2-gate/REVIEW.json` and
    the review directory through the same root. With the whole review gate moved out of
    the repository and `dogfood/p2-gate` a symlink to it, the pin read the foreign
    review, named it in the cell, and failed only by accident -- a digest mismatch,
    because the rebuilt brief's drift reason became `review root outside the
    repository`. The row now fails for the stated reason and names no foreign review.
    Control: the same review in place passes (the first assertion)."""
    repo = tmp_path / "repo"
    register, matrix = _tree(repo, monkeypatch)
    _record(repo, register, matrix, findings=[])
    assert rd.row_programme_supervisor().state == rd.PASS
    (tmp_path / "outside").mkdir()
    (repo / "dogfood/p2-gate").rename(tmp_path / "outside/gate")      # moved, not removed
    (repo / "dogfood/p2-gate").symlink_to("../../outside/gate")
    row = rd.row_programme_supervisor()
    assert row.state == rd.FAIL
    assert "dogfood/p2-gate resolves outside the repository" in row.value_
    assert row.value_.startswith("review none:")
