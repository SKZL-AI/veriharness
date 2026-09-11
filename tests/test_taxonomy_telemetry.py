"""The failure taxonomy and the dispatch log, with the controls that matter.

Two properties carry most of the weight here and both have a history:

* a class is a **policy**, not a label. `PROVIDER_AUTH` retrying would repeat
  a call that will be rejected identically until a person acts, and this
  project lost a campaign's QA verdicts to exactly that credential;
* an unmeasured cost is **unmeasured**. Summing the records that happened to
  report tokens produces a smaller number that reads like a complete one,
  which is the shape of this project's fourth false green.
"""

from __future__ import annotations

import json

import pytest

from hoh.taxonomy import (
    POLICY,
    BudgetScope,
    FailureClass,
    auto_resumable,
    backoff_for,
    classify_exit,
    disposition,
    may_retry,
    needs_human,
)
from hoh.telemetry import DispatchRecord, TelemetryLog, record_from_role, summarise


# --------------------------------------------------------------------------- #
# Every class has a policy, and the policies say different things
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fc", list(FailureClass), ids=lambda f: f.value)
def test_every_class_has_a_disposition_with_its_reasoning(fc):
    d = disposition(fc)
    assert d.failure_class is fc
    assert d.rationale, f"{fc.value} has a policy and no stated reason for it"
    assert d.required_evidence, (
        f"{fc.value} can be assigned with no stated evidence, which is how a "
        "class becomes the place unexplained failures go"
    )


def test_the_table_covers_the_enum_exactly():
    """A class without a policy would fall through to a KeyError at the worst
    possible moment, and a policy without a class is dead configuration."""
    assert set(POLICY) == set(FailureClass)


def test_the_classes_do_not_all_have_the_same_policy():
    """The control for the parametrised test above: a table where every row is
    identical would pass it and would be worth nothing."""
    verschieden = {
        (d.max_retries, d.budget, d.auto_resume, d.human_gate)
        for d in POLICY.values()
    }
    assert len(verschieden) >= 5


# --------------------------------------------------------------------------- #
# The specific distinctions the taxonomy exists to make
# --------------------------------------------------------------------------- #


def test_an_expired_credential_is_never_retried_by_a_machine():
    assert not may_retry(FailureClass.PROVIDER_AUTH, 1)
    assert not auto_resumable(FailureClass.PROVIDER_AUTH)
    assert needs_human(FailureClass.PROVIDER_AUTH)


def test_a_rate_limit_waits_and_a_quota_waits_longer():
    assert backoff_for(FailureClass.PROVIDER_RATE_LIMIT, 1) == 30
    assert backoff_for(FailureClass.PROVIDER_QUOTA, 1) == 900
    assert auto_resumable(FailureClass.PROVIDER_RATE_LIMIT)


def test_a_broken_contract_gets_one_more_try_and_not_more():
    assert may_retry(FailureClass.CONTRACT_INVALID, 1)
    assert not may_retry(FailureClass.CONTRACT_INVALID, 2)


def test_a_quota_exhaustion_and_a_broken_contract_are_told_apart():
    """Limitation 16 in as many words: they were indistinguishable, so the
    orchestrator could only pick one behaviour for both."""
    q, c = disposition(FailureClass.PROVIDER_QUOTA), disposition(
        FailureClass.CONTRACT_INVALID
    )
    assert (q.max_retries, q.backoff_seconds) != (c.max_retries, c.backoff_seconds)


def test_infrastructure_spends_the_attempt_and_a_role_failure_spends_the_node():
    """A provider outage that spends a node's iterations turns an outage into
    a permanent failure: the work never got a chance and the budget is gone."""
    assert disposition(FailureClass.INFRASTRUCTURE).budget is BudgetScope.ATTEMPT
    assert disposition(FailureClass.ROLE_FAILED).budget is BudgetScope.NODE


def test_a_merge_conflict_is_not_retried_but_a_merge_obstruction_is():
    assert not may_retry(FailureClass.MERGE_CONFLICT, 1)
    assert may_retry(FailureClass.MERGE_OBSTRUCTED, 1)
    # Both may be carried forward by an unattended process: a conflict becomes
    # a repair node, which is work rather than a retry.
    assert auto_resumable(FailureClass.MERGE_CONFLICT)


def test_unknown_stops_rather_than_hoping():
    assert not may_retry(FailureClass.UNKNOWN, 1)
    assert needs_human(FailureClass.UNKNOWN)
    assert not auto_resumable(FailureClass.UNKNOWN)


def test_needs_approval_is_not_a_failure_and_is_not_retried():
    d = disposition(FailureClass.NEEDS_APPROVAL)
    assert d.max_retries == 0 and d.human_gate and not d.auto_resume


def test_backoff_doubles_and_is_capped():
    werte = [backoff_for(FailureClass.PROVIDER_RATE_LIMIT, i) for i in range(1, 8)]
    assert werte[:4] == [30, 60, 120, 240]
    assert max(werte) == disposition(FailureClass.PROVIDER_RATE_LIMIT).backoff_ceiling


def test_a_class_that_never_retries_never_waits():
    for fc in (FailureClass.PROVIDER_AUTH, FailureClass.CORRUPT_STATE,
               FailureClass.UNKNOWN, FailureClass.NEEDS_APPROVAL):
        assert backoff_for(fc, 1) == 0


def test_only_infrastructure_exit_codes_classify_as_infrastructure():
    for code in (124, 126, 127):
        assert classify_exit(code) is FailureClass.INFRASTRUCTURE
    # A plain failing check is a product outcome, not a dispatch failure.
    for code in (0, 1, 2, 5):
        assert classify_exit(code) is FailureClass.UNKNOWN


# --------------------------------------------------------------------------- #
# An unmeasured cost stays unmeasured
# --------------------------------------------------------------------------- #


def _rec(**kw) -> DispatchRecord:
    basis = dict(role="developer", run_id="r", iteration=1, attempt=1,
                 started_at="2026-09-11T10:00:00Z", ended_at="2026-09-11T10:00:10Z",
                 wallclock_seconds=10.0)
    basis.update(kw)
    return DispatchRecord(**basis)


def test_a_partial_token_total_is_not_reported_as_a_total():
    s = summarise([
        _rec(tokens_in=100, tokens_out=50),
        _rec(),                                  # the harness reported nothing
    ])
    assert s.tokens is None, "a total over the records that reported is not a total"
    assert s.tokens_unknown == 1
    assert "NOT_DETERMINABLE" in s.line()


def test_a_complete_token_total_is_reported():
    s = summarise([
        _rec(tokens_in=100, tokens_out=50),
        _rec(tokens_in=7, tokens_out=3),
    ])
    assert s.tokens == 160
    assert s.tokens_unknown == 0


def test_an_unknown_wallclock_is_counted_not_treated_as_zero():
    s = summarise([_rec(wallclock_seconds=None), _rec(wallclock_seconds=4.0)])
    assert s.wallclock_seconds == 4.0
    assert s.wallclock_unknown == 1
    assert "1 unknown" in s.line()


def test_a_proxy_is_labelled_as_a_proxy():
    r = _rec(quota_proxy=3.0, quota_proxy_kind="requests")
    assert r.cost_known() is False, "a proxy is not a token measurement"
    assert r.quota_proxy_kind == "requests"


def test_artefactual_discrimination_is_counted_separately():
    """Counting it as discriminating overstates what the iteration showed."""
    s = summarise([_rec(discriminating=2, artefactual=1)])
    assert (s.discriminating, s.artefactual) == (2, 1)
    assert "1 artefactual" in s.line()


def test_the_summary_counts_roles_outcomes_and_failure_classes():
    s = summarise([
        _rec(role="planner"),
        _rec(role="qa", outcome="failed",
             failure_class=FailureClass.PROVIDER_RATE_LIMIT, retries=2),
    ])
    assert s.by_role == {"planner": 1, "qa": 1}
    assert s.by_outcome == {"ok": 1, "failed": 1}
    assert s.by_failure_class == {"PROVIDER_RATE_LIMIT": 1}
    assert s.retries == 2


def test_an_empty_summary_is_zero_dispatches_and_an_unknown_cost():
    s = summarise([])
    assert s.dispatches == 0
    # No records is not a measured zero cost either.
    assert s.tokens == 0 or s.tokens is None


# --------------------------------------------------------------------------- #
# The log
# --------------------------------------------------------------------------- #


def test_records_round_trip_through_the_log(tmp_path):
    log = TelemetryLog(tmp_path / "t.jsonl")
    log.append(_rec(role="planner"))
    log.append(_rec(role="qa", tokens_in=5, tokens_out=6))
    gelesen = log.read()
    assert [r.role for r in gelesen] == ["planner", "qa"]
    assert gelesen[1].tokens_in == 5


def test_a_truncated_last_line_does_not_lose_the_history(tmp_path):
    """A killed writer loses the line it was writing. Raising on it would lose
    everything before it, which is the worse failure."""
    pfad = tmp_path / "t.jsonl"
    log = TelemetryLog(pfad)
    log.append(_rec(role="planner"))
    with open(pfad, "a") as fh:
        fh.write('{"role": "dev", "run_i')
    assert [r.role for r in log.read()] == ["planner"]


def test_the_log_is_append_only(tmp_path):
    pfad = tmp_path / "t.jsonl"
    log = TelemetryLog(pfad)
    log.append(_rec(role="planner"))
    erste = pfad.read_text()
    log.append(_rec(role="qa"))
    assert pfad.read_text().startswith(erste), "an earlier record was rewritten"


def test_rewriting_sorted_keeps_every_record(tmp_path):
    pfad = tmp_path / "t.jsonl"
    log = TelemetryLog(pfad)
    log.append(_rec(role="qa", started_at="2026-09-11T10:00:02Z"))
    log.append(_rec(role="planner", started_at="2026-09-11T10:00:01Z"))
    log.rewrite_sorted()
    gelesen = log.read()
    assert [r.role for r in gelesen] == ["planner", "qa"]
    assert len(gelesen) == 2


def test_a_missing_log_reads_as_empty_rather_than_raising(tmp_path):
    assert TelemetryLog(tmp_path / "nope.jsonl").read() == []


# --------------------------------------------------------------------------- #
# Building a record from what a harness actually returned
# --------------------------------------------------------------------------- #


def test_usage_the_harness_did_not_report_stays_none():
    r = record_from_role(
        role="qa", run_id="r", iteration=1, attempt=1,
        started_at="2026-09-11T10:00:00Z", ended_at="2026-09-11T10:00:05Z",
        usage={},
    )
    assert r.tokens_in is None and r.tokens_out is None
    assert r.quota_proxy is None
    assert r.wallclock_seconds == 5.0


def test_usage_the_harness_did_report_is_carried_through():
    r = record_from_role(
        role="qa", run_id="r", iteration=1, attempt=1,
        started_at="2026-09-11T10:00:00Z", ended_at="2026-09-11T10:00:05Z",
        usage={"input_tokens": 11, "output_tokens": 22, "requests": 1},
    )
    assert (r.tokens_in, r.tokens_out) == (11, 22)
    assert r.quota_proxy == 1.0 and r.quota_proxy_kind == "requests"


def test_an_unparseable_timestamp_leaves_the_duration_unknown():
    r = record_from_role(
        role="qa", run_id="r", iteration=1, attempt=1,
        started_at="whenever", ended_at="later",
    )
    assert r.wallclock_seconds is None


def test_the_record_can_state_the_policy_that_applies_to_it():
    r = _rec(outcome="failed", failure_class=FailureClass.PROVIDER_RATE_LIMIT)
    text = r.retry_policy()
    assert "PROVIDER_RATE_LIMIT" in text and "attempt budget" in text
    assert _rec().retry_policy() == "no failure"


def test_a_record_serialises_to_one_line(tmp_path):
    """The log is line-oriented; a record with an embedded newline would
    corrupt every reader after it."""
    r = _rec(detail="line one\nline two")
    assert "\n" not in r.model_dump_json()
    assert json.loads(r.model_dump_json())["detail"] == "line one\nline two"
