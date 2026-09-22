"""The meta-evidence rules, and a negative control for each one.

Every test here that asserts a rule holds is paired with one that breaks the
rule deliberately, because a rule about falsifiers that has never been
falsified would be the joke this module is trying not to be.

The five historical false greens are reproduced at the bottom: each one is
built the way the broken tool actually read its number, and the closure has to
refuse it.

An adversarial review of the first version of this file found ten mutations
that survived it -- including a sixth `ResultState` that went green while
`authoritative()` said False, and a headline test for "SKIPPED != PASS" that
exercised none of the SKIPPED-!=-PASS machinery because `authoritative()`
short-circuited before reaching it. Those are the tests marked below.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from pydantic import ValidationError

from hoh.assurance import (
    HEAD_BOUND,
    NOT_A_PASS,
    TRUSTED_BASE,
    AssuranceClosure,
    AssuranceRecord,
    EvidenceSource,
    FalsifierState,
    Provenance,
    ResultState,
    SourceKind,
    from_ci_step,
    from_git,
    from_json_state,
    not_a_pass,
)

HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def git(metric_id="m", *, state=ResultState.VERIFIED, head=HEAD, **kw):
    """A record that is authoritative in every way, as the baseline to break."""
    fields_ = {
        "falsifier": FalsifierState.KILLED,
        "falsifier_detail": "a fabricated commit was injected; the count moved",
        "provenance": Provenance.MEASURED,
        "measured": 3,
    }
    fields_.update(kw)
    return AssuranceRecord(
        metric_id=metric_id,
        source=EvidenceSource(
            kind=SourceKind.REPOSITORY, identity="HEAD~1..HEAD", subject_head=head
        ),
        derivation="git log --no-merges --format=%H, counted",
        state=state,
        **fields_,
    )


# --------------------------------------------------------------------------- #
# The baseline is green -- otherwise every test below passes for the wrong
# reason
# --------------------------------------------------------------------------- #


def test_the_baseline_record_is_authoritative():
    r = git()
    assert r.weaknesses(subject_head=HEAD) == []
    assert r.authoritative(subject_head=HEAD)


def test_a_closure_of_one_good_record_is_green():
    c = AssuranceClosure(subject_head=HEAD, records=[git()])
    assert c.green()
    assert c.problems() == {}


# --------------------------------------------------------------------------- #
# absence != zero
# --------------------------------------------------------------------------- #


def test_an_absent_source_cannot_be_verified():
    with pytest.raises(ValidationError, match="absence is not a measured zero"):
        AssuranceRecord(
            metric_id="out_of_band",
            source=EvidenceSource(kind=SourceKind.ABSENT),
            derivation="len(external_actions)",
            measured=0,
            state=ResultState.VERIFIED,
            falsifier=FalsifierState.KILLED,
            falsifier_detail="d",
        )


def test_an_absent_source_is_reported_as_such_even_when_not_determinable():
    r = AssuranceRecord(
        metric_id="out_of_band",
        source=EvidenceSource(kind=SourceKind.ABSENT),
        derivation="len(external_actions) on a run predating the mechanism",
        measured=0,
        state=ResultState.NOT_DETERMINABLE,
        falsifier=FalsifierState.KILLED,
        falsifier_detail="d",
    )
    reasons = r.weaknesses()
    assert any("no source was read" in g for g in reasons)
    assert not r.authoritative()


def test_an_absent_source_cannot_name_something_to_open():
    with pytest.raises(ValidationError, match="cannot name an identity"):
        EvidenceSource(kind=SourceKind.ABSENT, identity="dogfood/state.json")


def test_a_present_source_must_name_something_to_open():
    with pytest.raises(ValidationError, match="must name what to open"):
        EvidenceSource(kind=SourceKind.REPOSITORY)


def test_verified_without_a_measured_value_is_a_claim_about_nothing():
    with pytest.raises(ValidationError, match="claim about nothing"):
        git(measured=None)


# --------------------------------------------------------------------------- #
# a weaker source than the one that was available
# --------------------------------------------------------------------------- #


def test_a_log_with_a_stronger_source_available_is_not_authoritative():
    r = AssuranceRecord(
        metric_id="repair_nodes",
        source=EvidenceSource(kind=SourceKind.TRANSIENT_LOG, identity="steps.txt"),
        derivation="counted CREATE_REPAIR lines in the step log",
        measured=0,
        state=ResultState.VERIFIED,
        falsifier=FalsifierState.KILLED,
        falsifier_detail="d",
        stronger_source_available=SourceKind.PROJECT_STATE,
    )
    assert any("transient_log while project_state" in g for g in r.weaknesses())
    assert not r.authoritative()


def test_the_weaker_source_rule_applies_to_every_kind_not_only_to_logs():
    """The first version confined this rule to `TRANSIENT_LOG`, so a record
    honestly declaring "I read run state where git was the answer" -- which is
    false green number two exactly -- passed clean."""
    r = AssuranceRecord(
        metric_id="merges",
        source=EvidenceSource(
            kind=SourceKind.RUN_STATE, identity="state.json", subject_head=HEAD
        ),
        derivation="counted merges in the run state",
        measured=1,
        state=ResultState.VERIFIED,
        falsifier=FalsifierState.KILLED,
        falsifier_detail="d",
        provenance=Provenance.MEASURED,
        stronger_source_available=SourceKind.REPOSITORY,
    )
    assert any("run_state while repository" in g for g in r.weaknesses())


def test_naming_the_source_that_was_used_is_rejected_as_a_mistake():
    """The field names a source that *should* have been used instead. Naming
    the one that was is a slip, not a weakness."""
    with pytest.raises(ValidationError, match="is the source that was used"):
        git(stronger_source_available=SourceKind.REPOSITORY)


def test_durability_is_an_ordering_and_not_a_ranking_of_worth():
    """It used to be `strength()` and decided which source may authorise a
    metric. That is wrong in a way that is easy to miss because it is usually
    right -- see the authority tests at the bottom of this file."""
    assert SourceKind.REPOSITORY.durability() > SourceKind.TRANSIENT_LOG.durability()
    assert SourceKind.RECEIPT.durability() > SourceKind.PROJECT_STATE.durability()
    assert SourceKind.ABSENT.durability() == 0
    for k in SourceKind:
        assert isinstance(k.durability(), int)


def test_a_critical_log_read_is_refused_even_with_no_stronger_source_named():
    r = AssuranceRecord(
        metric_id="something",
        source=EvidenceSource(kind=SourceKind.TRANSIENT_LOG, identity="console"),
        derivation="read off the console",
        measured=1,
        state=ResultState.VERIFIED,
        falsifier=FalsifierState.KILLED,
        falsifier_detail="d",
    )
    assert any("release-critical metric read from a transient log" in g
               for g in r.weaknesses())


def test_a_non_critical_log_read_may_stand():
    """The rule has to leave room for diagnostics, or people route around it.

    What stops `critical=False` from being a universal bypass is not this
    record: it is `AssuranceClosure.required`, which a record cannot lower.
    """
    r = AssuranceRecord(
        metric_id="wallclock_estimate",
        source=EvidenceSource(kind=SourceKind.TRANSIENT_LOG, identity="console"),
        derivation="the harness printed it",
        measured=41.0,
        state=ResultState.VERIFIED,
        falsifier=FalsifierState.NOT_RUN,
        critical=False,
    )
    assert r.weaknesses() == []
    assert r.authoritative()


# --------------------------------------------------------------------------- #
# SKIPPED != PASS -- tested through weaknesses(), not through the short circuit
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "condition",
    [s for s in ResultState if s is not ResultState.VERIFIED],
    ids=lambda s: s.value,
)
def test_every_non_verified_state_is_named_as_a_reason(condition):
    """The previous version of this test asserted via `authoritative()`, which
    short-circuited on `state is VERIFIED` before the rule under test ever
    ran: with the whole branch deleted it still passed. It also parametrised
    over `NOT_A_PASS` itself, so removing a state from the set deleted the
    test case instead of failing it."""
    reasons = git(state=condition).weaknesses(subject_head=HEAD)
    assert reasons, f"{condition.value} produced no reason at all"
    assert any(condition.value in g or "measured a failure" in g for g in reasons)
    assert not git(state=condition).authoritative(subject_head=HEAD)


def test_not_a_pass_is_derived_from_the_enum_not_maintained_by_hand():
    """A hand-written set is a set someone forgets. A reviewer added a sixth
    state, the closure went green on it, and `authoritative()` disagreed --
    two code paths for one rule."""
    assert NOT_A_PASS == not_a_pass()
    assert ResultState.VERIFIED not in NOT_A_PASS
    assert len(NOT_A_PASS) == len(ResultState) - 1


def test_unsupported_environment_is_not_a_failure_but_is_not_a_pass():
    r = git(state=ResultState.UNSUPPORTED_ENVIRONMENT)
    c = AssuranceClosure(subject_head=HEAD, records=[r])
    reasons = c.problems()["m"]
    assert any("UNSUPPORTED_ENVIRONMENT, which is not a pass" in g for g in reasons)
    assert not any("measured a failure" in g for g in reasons)


def test_a_failed_metric_is_reported_as_a_failure_by_the_record_itself():
    """`weaknesses()` used to exempt FAILED and leave the reason to be added by
    the closure, so the per-record API gave a clean bill to a metric that had
    measured a defect."""
    r = git(state=ResultState.FAILED)
    assert r.weaknesses(subject_head=HEAD) == ["the metric measured a failure"]
    assert not r.authoritative(subject_head=HEAD)


# --------------------------------------------------------------------------- #
# a critical metric needs a falsifier that actually killed
# --------------------------------------------------------------------------- #


def test_an_escaped_falsifier_disqualifies_the_metric():
    r = git(falsifier=FalsifierState.ESCAPED,
            falsifier_detail="15 of 16 injected defects went unnoticed")
    assert any("shown unable to fail" in g for g in r.weaknesses(subject_head=HEAD))


def test_no_falsifier_at_all_disqualifies_a_critical_metric():
    r = git(falsifier=FalsifierState.NOT_RUN, falsifier_detail="")
    assert any("no negative control was run" in g
               for g in r.weaknesses(subject_head=HEAD))


def test_killed_without_evidence_is_a_tick_box_and_is_rejected():
    with pytest.raises(ValidationError, match="tick-box"):
        git(falsifier=FalsifierState.KILLED, falsifier_detail="")


def test_not_applicable_is_confined_to_digests():
    """It used to admit REPOSITORY as well. Since most of this project's real
    metrics are git-derived with non-trivial derivations, that was not an
    escape hatch but the main road: the duplicate-merge check that caused
    false green number three passed as NOT_APPLICABLE."""
    with pytest.raises(ValidationError, match="NOT_APPLICABLE is for a direct read"):
        AssuranceRecord(
            metric_id="merges",
            source=EvidenceSource(kind=SourceKind.REPOSITORY, identity="git log"),
            derivation="parsed git log --merges and checked for repeated parents",
            measured=0,
            state=ResultState.VERIFIED,
            falsifier=FalsifierState.NOT_APPLICABLE,
        )


def test_not_applicable_is_allowed_for_a_digest():
    r = AssuranceRecord(
        metric_id="manifest_digest",
        source=EvidenceSource(kind=SourceKind.DIGEST, identity="sha256:beef"),
        derivation="read the digest",
        measured="beef",
        state=ResultState.VERIFIED,
        falsifier=FalsifierState.NOT_APPLICABLE,
        provenance=Provenance.MEASURED,
    )
    assert r.authoritative()


# --------------------------------------------------------------------------- #
# hand-assembled records say so
# --------------------------------------------------------------------------- #


def test_a_hand_assembled_critical_record_is_flagged():
    """The module cannot open what `identity` names, so a caller could write
    `kind=REPOSITORY` while having read a console log. What narrows it is that
    the honest path -- the `from_*` builders -- is the short one, and anything
    hand-built is marked."""
    r = git(provenance=Provenance.DECLARED)
    assert any("hand-assembled" in g for g in r.weaknesses(subject_head=HEAD))


def test_declared_is_the_default_so_the_marking_cannot_be_forgotten():
    r = AssuranceRecord(
        metric_id="x",
        source=EvidenceSource(kind=SourceKind.REPOSITORY, identity="HEAD"),
        derivation="d",
    )
    assert r.provenance is Provenance.DECLARED


def test_the_defaults_are_the_safe_ones():
    """Both survived the first version's whole suite when flipped."""
    r = AssuranceRecord(
        metric_id="x",
        source=EvidenceSource(kind=SourceKind.REPOSITORY, identity="HEAD"),
        derivation="d",
    )
    assert r.state is ResultState.NOT_RUN
    assert r.falsifier is FalsifierState.NOT_RUN
    assert r.critical is True


def test_an_empty_metric_id_or_derivation_is_rejected():
    with pytest.raises(ValidationError):
        git(metric_id="")
    with pytest.raises(ValidationError):
        AssuranceRecord(
            metric_id="x",
            source=EvidenceSource(kind=SourceKind.REPOSITORY, identity="HEAD"),
            derivation="",
        )


# --------------------------------------------------------------------------- #
# stale evidence
# --------------------------------------------------------------------------- #


def test_evidence_from_another_head_is_not_current_evidence():
    r = git(head=OTHER_HEAD)
    assert any("but the subject is" in g for g in r.weaknesses(subject_head=HEAD))


def test_a_head_bound_source_that_names_no_head_cannot_be_checked():
    """Omitting the head used to make a record stale-proof."""
    r = AssuranceRecord(
        metric_id="x",
        source=EvidenceSource(kind=SourceKind.PROJECT_STATE, identity="p.json"),
        derivation="d",
        measured=1,
        state=ResultState.VERIFIED,
        falsifier=FalsifierState.KILLED,
        falsifier_detail="d",
        provenance=Provenance.MEASURED,
    )
    assert any("cannot be checked for staleness" in g
               for g in r.weaknesses(subject_head=HEAD))
    # Without a subject there is nothing to be stale against.
    assert r.weaknesses() == []


def test_a_ci_step_is_not_head_bound():
    assert SourceKind.EXTERNAL_CI_STEP not in HEAD_BOUND
    r = from_ci_step(
        "ci", run_id="34609909307", step_name="sandbox", conclusion="success",
        derivation="the conclusion of the named step",
        falsifier=FalsifierState.KILLED,
        falsifier_detail="a deliberately failing step turned the job red",
    )
    assert r.weaknesses(subject_head=HEAD) == []


def test_the_closure_passes_its_subject_down():
    """No test in the first version ever put a stale record inside a closure,
    so a mutation that stopped passing the head down survived."""
    c = AssuranceClosure(subject_head=HEAD, records=[git(head=OTHER_HEAD)])
    assert not c.green()
    assert any("but the subject is" in g for g in c.problems()["m"])


# --------------------------------------------------------------------------- #
# the trusted base
# --------------------------------------------------------------------------- #


def test_a_source_outside_the_base_needs_a_cross_check():
    r = AssuranceRecord(
        metric_id="x",
        source=EvidenceSource(kind=SourceKind.TRANSIENT_LOG, identity="l"),
        derivation="d",
        measured=1,
        state=ResultState.VERIFIED,
        falsifier=FalsifierState.KILLED,
        falsifier_detail="d",
        provenance=Provenance.MEASURED,
    )
    assert any("outside the trusted base" in g for g in r.weaknesses())


def test_a_cross_check_outside_the_base_does_not_repair_it():
    r = AssuranceRecord(
        metric_id="x",
        source=EvidenceSource(kind=SourceKind.TRANSIENT_LOG, identity="l"),
        derivation="d",
        measured=1,
        state=ResultState.VERIFIED,
        falsifier=FalsifierState.KILLED,
        falsifier_detail="d",
        provenance=Provenance.MEASURED,
        cross_check=EvidenceSource(kind=SourceKind.TRANSIENT_LOG, identity="l2"),
    )
    assert any("itself outside the trusted base" in g for g in r.weaknesses())


def test_a_cross_check_disagreeing_on_the_digest_is_not_agreement():
    """`digest` was declared as the drift detector and read by no rule."""
    r = AssuranceRecord(
        metric_id="x",
        source=EvidenceSource(
            kind=SourceKind.REPOSITORY, identity="p.json", digest="aaaa",
            subject_head=HEAD,
        ),
        derivation="d",
        measured=1,
        state=ResultState.VERIFIED,
        falsifier=FalsifierState.KILLED,
        falsifier_detail="d",
        provenance=Provenance.MEASURED,
        cross_check=EvidenceSource(
            kind=SourceKind.REPOSITORY, identity="p.json", digest="bbbb"
        ),
    )
    assert any("two different things with one name" in g
               for g in r.weaknesses(subject_head=HEAD))


def test_the_trusted_base_excludes_logs_and_absence():
    assert SourceKind.TRANSIENT_LOG not in TRUSTED_BASE
    assert SourceKind.ABSENT not in TRUSTED_BASE
    assert SourceKind.EXTERNAL_CI_STEP in TRUSTED_BASE


# --------------------------------------------------------------------------- #
# the closure
# --------------------------------------------------------------------------- #


def test_an_empty_closure_is_not_green():
    """Zero gates passing is the shape of a gate that was never wired up."""
    assert not AssuranceClosure(subject_head=HEAD).green()


def test_one_bad_record_makes_the_whole_closure_not_green():
    c = AssuranceClosure(
        subject_head=HEAD,
        records=[git("gut"), git("schlecht", state=ResultState.NOT_RUN)],
    )
    assert not c.green()
    assert set(c.problems()) == {"schlecht"}


def test_a_verified_but_weak_record_blocks_the_closure():
    """The closure's actual job. The first version's only coverage of it was a
    string in the report: a mutation replacing `green()` with "all states are
    VERIFIED" survived every closure test."""
    weak = git("schwach", provenance=Provenance.DECLARED)
    assert weak.state is ResultState.VERIFIED
    c = AssuranceClosure(subject_head=HEAD, records=[weak])
    assert not c.green()


def test_a_required_metric_with_no_record_is_a_missing_measurement():
    c = AssuranceClosure(subject_head=HEAD, records=[git("da")],
                         required=["da", "fehlt"])
    assert not c.green()
    assert c.problems()["fehlt"] == ["required, and no record was produced for it"]
    assert "fehlt" in c.report()


def test_a_record_cannot_lower_a_requirement_the_closure_imposes():
    """`critical=False` relaxes the record's own rules. It must not be able to
    remove the metric from the list of things that had to be measured."""
    soft = AssuranceRecord(
        metric_id="pflicht",
        source=EvidenceSource(kind=SourceKind.TRANSIENT_LOG, identity="l"),
        derivation="d",
        measured=1,
        state=ResultState.NOT_RUN,
        critical=False,
    )
    c = AssuranceClosure(records=[soft], required=["pflicht"])
    assert not c.green()
    assert any("NOT_RUN" in g for g in c.problems()["pflicht"])


def test_two_records_under_one_name_are_rejected():
    """One record's reasons used to be printed against the other's row."""
    with pytest.raises(ValidationError, match="duplicate metric_id"):
        AssuranceClosure(records=[git("dup"), git("dup", state=ResultState.NOT_RUN)])


def test_by_state_counts_every_record():
    c = AssuranceClosure(
        records=[git("a"), git("b"), git("c", state=ResultState.NOT_RUN)]
    )
    assert c.by_state() == {"VERIFIED": 2, "NOT_RUN": 1}


def test_the_report_names_every_reason():
    c = AssuranceClosure(
        subject_head=HEAD,
        records=[git("m", falsifier=FalsifierState.NOT_RUN, falsifier_detail="")],
    )
    text = c.report()
    assert "NOT  m" in text
    assert "no negative control was run" in text
    assert "NOT GREEN" in text


def test_a_green_report_says_so():
    assert "=> GREEN" in AssuranceClosure(subject_head=HEAD, records=[git()]).report()


# --------------------------------------------------------------------------- #
# the builders read the source
# --------------------------------------------------------------------------- #


def test_from_git_runs_git_and_binds_the_head(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one"],
        cwd=tmp_path, check=True,
    )
    r = from_git(
        "commits",
        repo=tmp_path,
        argv=["log", "--format=%H"],
        derivation="counted the commits git printed",
        interpret=lambda p: (
            len(p.stdout.split()),
            ResultState.VERIFIED if p.returncode == 0 else ResultState.NOT_DETERMINABLE,
        ),
        falsifier=FalsifierState.KILLED,
        falsifier_detail="an extra commit was made; the count moved to 2",
    )
    assert r.measured == 1
    assert r.provenance is Provenance.MEASURED
    assert r.source.kind is SourceKind.REPOSITORY
    assert len(r.source.subject_head) == 40
    assert r.source.digest                      # digested from git's own output
    assert r.authoritative(subject_head=r.source.subject_head)


def test_from_json_state_treats_a_missing_file_as_not_determinable(tmp_path):
    """False green number four, in one line: an empty source is not a zero."""
    r = from_json_state(
        "out_of_band",
        path=tmp_path / "nope.json",
        kind=SourceKind.PROJECT_STATE,
        derivation="len(external_actions)",
        interpret=lambda d: (len(d["external_actions"]), ResultState.VERIFIED),
        falsifier=FalsifierState.KILLED,
        falsifier_detail="d",
    )
    assert r.state is ResultState.NOT_DETERMINABLE
    assert r.source.kind is SourceKind.ABSENT
    assert r.measured is None
    assert not r.authoritative()


def test_from_json_state_reads_and_digests_the_file(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"external_actions": []}))
    r = from_json_state(
        "out_of_band",
        path=p,
        kind=SourceKind.PROJECT_STATE,
        derivation="len(external_actions), on a state written after the "
                   "mechanism existed",
        interpret=lambda d: (len(d["external_actions"]), ResultState.VERIFIED),
        subject_head=HEAD,
        falsifier=FalsifierState.KILLED,
        falsifier_detail="an action was appended; the count moved to 1",
    )
    assert r.measured == 0
    assert r.source.digest
    assert r.authoritative(subject_head=HEAD)


def test_a_skipped_ci_step_is_an_unsupported_environment_not_a_pass():
    r = from_ci_step(
        "sandbox_external",
        run_id="34571331995", step_name="sandbox tests", conclusion="skipped",
        derivation="the conclusion of the sandbox test step",
        falsifier=FalsifierState.KILLED,
        falsifier_detail="a deliberately failing sandbox test turned the job red",
    )
    assert r.state is ResultState.UNSUPPORTED_ENVIRONMENT
    assert not r.authoritative()


@pytest.mark.parametrize(
    "conclusion,expected",
    [
        ("success", ResultState.VERIFIED),
        ("failure", ResultState.FAILED),
        ("skipped", ResultState.UNSUPPORTED_ENVIRONMENT),
        ("cancelled", ResultState.NOT_RUN),
        ("something-new", ResultState.NOT_DETERMINABLE),
    ],
)
def test_every_ci_conclusion_maps_to_a_state(conclusion, expected):
    r = from_ci_step(
        "x", run_id="1", step_name="s", conclusion=conclusion, derivation="d",
        falsifier=FalsifierState.KILLED, falsifier_detail="d",
    )
    assert r.state is expected


# --------------------------------------------------------------------------- #
# The five historical false greens, as records. Each must be refused.
# --------------------------------------------------------------------------- #


def test_false_green_1_enum_string_comparison_had_no_falsifier():
    """`str(HaltClass.X) == "HaltClass.X"` never matched, so it never failed."""
    r = AssuranceRecord(
        metric_id="halt_class_correct",
        source=EvidenceSource(kind=SourceKind.TRANSIENT_LOG, identity="steps"),
        derivation='compared str(HaltClass.X) against "HaltClass.X"',
        measured=True,
        state=ResultState.VERIFIED,
        falsifier=FalsifierState.NOT_RUN,
        stronger_source_available=SourceKind.PROJECT_STATE,
    )
    assert not r.authoritative()
    assert len(r.weaknesses()) >= 2


def test_false_green_2_repair_nodes_from_the_step_log():
    r = AssuranceRecord(
        metric_id="repair_nodes",
        source=EvidenceSource(kind=SourceKind.TRANSIENT_LOG, identity="steps"),
        derivation="collected repair nodes from the step log",
        measured=[],
        state=ResultState.VERIFIED,
        falsifier=FalsifierState.KILLED,
        falsifier_detail="d",
        stronger_source_available=SourceKind.PROJECT_STATE,
    )
    assert not r.authoritative()


def test_false_green_3_duplicate_merges_from_the_step_log():
    r = AssuranceRecord(
        metric_id="no_duplicate_merge",
        source=EvidenceSource(kind=SourceKind.TRANSIENT_LOG, identity="steps"),
        derivation="checked the merge steps for repeats",
        measured=True,
        state=ResultState.VERIFIED,
        falsifier=FalsifierState.KILLED,
        falsifier_detail="d",
        stronger_source_available=SourceKind.REPOSITORY,
    )
    assert not r.authoritative()


def test_false_green_4_empty_record_list_as_a_measured_zero():
    with pytest.raises(ValidationError):
        AssuranceRecord(
            metric_id="out_of_band_mutations",
            source=EvidenceSource(kind=SourceKind.ABSENT),
            derivation="len(external_actions)",
            measured=0,
            state=ResultState.VERIFIED,
            falsifier=FalsifierState.KILLED,
            falsifier_detail="d",
        )


def test_false_green_5_a_green_job_whose_step_was_skipped():
    r = from_ci_step(
        "sandbox_external",
        run_id="34571331995", step_name="sandbox tests", conclusion="skipped",
        derivation="the conclusion of the sandbox test step",
        falsifier=FalsifierState.KILLED,
        falsifier_detail="a deliberately failing sandbox test turned the job red",
    )
    assert not r.authoritative()
    assert any("not a pass" in g for g in r.weaknesses())


# --------------------------------------------------------------------------- #
# Authority belongs to the question, not to the medium
# --------------------------------------------------------------------------- #


def _rec(metric_id, kind, *, head=HEAD, falsifier=FalsifierState.KILLED,
         cross=None, **kw):
    fields_ = dict(
        metric_id=metric_id,
        source=EvidenceSource(kind=kind, identity="x", subject_head=head),
        derivation="d", measured=1, state=ResultState.VERIFIED,
        falsifier=falsifier, falsifier_detail="d",
        provenance=Provenance.MEASURED, cross_check=cross,
    )
    fields_.update(kw)
    return AssuranceRecord(**fields_)


def test_git_is_the_authority_for_a_landed_commit():
    from hoh.assurance import authority_for

    assert authority_for("landed_commit").preferred is SourceKind.REPOSITORY
    assert _rec("landed_commit", SourceKind.REPOSITORY).authoritative(
        subject_head=HEAD)


def test_git_does_not_get_to_answer_what_stage_a_run_is_in():
    """The whole reason a single ordering of sources was wrong. Git is the
    most durable source this project has and it cannot see a run's stage."""
    reasons = _rec("node_lifecycle", SourceKind.REPOSITORY).weaknesses(
        subject_head=HEAD)
    assert any("cannot answer node_lifecycle" in g for g in reasons)
    assert _rec("node_lifecycle", SourceKind.PROJECT_STATE).authoritative(
        subject_head=HEAD)


def test_run_state_does_not_get_to_answer_where_a_commit_landed():
    """The converse, and it has to be tested separately: a state file saying
    a node is MERGED records an intention to merge."""
    reasons = _rec("landed_commit", SourceKind.RUN_STATE).weaknesses(
        subject_head=HEAD)
    assert any("cannot answer landed_commit" in g for g in reasons)


def test_a_digest_alone_authorises_no_derived_metric():
    """A digest proves two byte sequences are the same. It says nothing about
    what they mean, so it is in no policy's allowed set."""
    for metric in ("landed_commit", "node_lifecycle", "run_accepted_candidate",
                   "check_executed_under_isolation"):
        reasons = _rec(metric, SourceKind.DIGEST).weaknesses(subject_head=HEAD)
        assert any("cannot answer" in g for g in reasons), metric


def test_a_receipt_does_not_replace_the_project_states_lifecycle():
    reasons = _rec("node_lifecycle", SourceKind.RECEIPT).weaknesses(
        subject_head=HEAD)
    assert any("cannot answer node_lifecycle" in g for g in reasons)


def test_only_the_receipt_may_say_a_check_ran_isolated():
    """O113: configuration says what was asked for. The receipt carries what
    the runner measured from inside."""
    assert _rec("check_executed_under_isolation", SourceKind.RECEIPT).authoritative(
        subject_head=HEAD)
    for k in (SourceKind.PROJECT_STATE, SourceKind.RUN_STATE,
              SourceKind.REPOSITORY, SourceKind.TRANSIENT_LOG):
        reasons = _rec("check_executed_under_isolation", k).weaknesses(
            subject_head=HEAD)
        assert any("cannot answer" in g for g in reasons), k.value


def test_a_ci_job_does_not_stand_in_for_the_named_step():
    from hoh.assurance import authority_for

    p = authority_for("external_ci_step")
    assert p.allowed == frozenset({SourceKind.EXTERNAL_CI_STEP})
    assert not p.requires_binding, (
        "a CI step's conclusion is not a property of a commit in this tree"
    )


def test_a_metric_class_prefix_shares_one_policy():
    from hoh.assurance import authority_for

    assert authority_for("check_executed_under_isolation:strictroman4") is (
        authority_for("check_executed_under_isolation"))
    assert authority_for("something_nobody_declared") is None


def test_a_metric_with_no_policy_is_judged_by_the_general_rules_only():
    """Absence of a policy must not become a licence. The general weaknesses
    still apply; what is absent is only the question-specific ownership."""
    r = _rec("something_nobody_declared", SourceKind.PROJECT_STATE)
    assert r.authoritative(subject_head=HEAD)
    weak = _rec("something_nobody_declared", SourceKind.PROJECT_STATE,
                   falsifier=FalsifierState.NOT_RUN, falsifier_detail="")
    assert not weak.authoritative(subject_head=HEAD)


def test_an_authoritative_source_still_needs_its_binding_and_falsifier():
    without_head = _rec("landed_commit", SourceKind.REPOSITORY, head=None)
    assert any("requires the commit it was measured at" in g
               for g in without_head.weaknesses(subject_head=HEAD))
    without_control = _rec("landed_commit", SourceKind.REPOSITORY,
                          falsifier=FalsifierState.NOT_RUN, falsifier_detail="")
    assert any("negative control" in g
               for g in without_control.weaknesses(subject_head=HEAD))


def test_durability_says_only_how_long_a_source_lasts():
    """It used to be called strength and decided who may answer. Now it is a
    technical property with no say in authority."""
    assert SourceKind.REPOSITORY.durability() > SourceKind.TRANSIENT_LOG.durability()
    assert not hasattr(SourceKind.REPOSITORY, "strength")
    # And durability does not rescue a source the policy excludes.
    reasons = _rec("node_lifecycle", SourceKind.REPOSITORY).weaknesses(
        subject_head=HEAD)
    assert reasons, "the most durable source answered a question it does not own"


def test_every_declared_policy_states_why():
    from hoh.assurance import AUTHORITIES

    for name, p in AUTHORITIES.items():
        assert p.rationale, f"{name} restricts sources and does not say why"
        assert p.allowed, name
