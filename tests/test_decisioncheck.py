"""The decision graph fsck (V3.3 P2-21): structure, and a receipt for it.

One case per check, each next to a negative control that changes only the
condition under test and shows the verdict flipping. Every error starts from
the same clean graph -- which passes with no findings at all -- and turns it
FAIL with exactly one code; without the controls, a checker that failed
everything would pass every error case in this file.

Warnings get the same treatment from the other side: a graph carrying every
warning code still reports PASS. The plan is explicit that warnings do not
silently become failures.

Finally the plan's whole check list is accounted for: each name is either
emitted by `check` (and a graph here triggers it) or listed in
`ENFORCED_AT_CONSTRUCTION` (and constructing the offending graph here raises).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from hoh.decisioncheck import (
    CHECK_CODES,
    ENFORCED_AT_CONSTRUCTION,
    LARGE_MAP_THRESHOLD,
    CheckResult,
    Finding,
    ProgramRef,
    Severity,
    check,
    graph_digest,
    resolution_digest,
)
from hoh.decisions import (
    DecisionMap,
    DecisionNode,
    DecisionStatus,
    FogPatch,
    ResolutionPath,
    ResolutionRecord,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
EARLIER = NOW - timedelta(hours=1)

#: The plan's check list, written out here independently of the module: a name
#: dropped from the module's own registries must fail this file, not vanish
#: from both sides at once.
PLAN_CHECKS = (
    "duplicate-map-id",
    "duplicate-decision-id",
    "self-block",
    "dangling-blocked-by",
    "dangling-clears-with",
    "dependency-cycle",
    "resolved-without-record",
    "cross-map-blocker-unresolved",
    "resolution-id-mismatch",
    "resolution-digest-mismatch",
    "resolution-without-source",
    "out-of-scope-blocks-active",
    "out-of-scope-as-provenance",
    "lease-expired-treated-active",
    "two-valid-leases",
    "fog-shadows-decision",
    "fog-clears-with-missing",
    "undermined-shown-as-settled",
    "graduated-with-blockers",
    "unresolved-as-settled",
    "unusually-large-map",
    "resolution-lacks-primary-source",
)

WARNING_CODES = ("lease-expired-treated-active", "fog-shadows-decision",
                 "unusually-large-map", "resolution-lacks-primary-source")


def _rec(decision_id="R", *, answer="use the existing ledger",
         source_refs=("doc/primary-1",), digest=None) -> ResolutionRecord:
    refs = list(source_refs)
    return ResolutionRecord(
        decision_id=decision_id, path=ResolutionPath.RESEARCH, answer=answer,
        source_refs=refs,
        resolution_digest=digest or resolution_digest(answer, refs),
        resolved_by="planner", resolved_at=NOW)


def _node(decision_id, *, status=DecisionStatus.OPEN, blocked_by=(),
          scope=("S1",), resolution=None, claimed_by=None,
          claim_expires_at=None, title=None) -> DecisionNode:
    return DecisionNode(
        decision_id=decision_id, title=title or f"title of {decision_id}",
        question=f"what about {decision_id}?", status=status,
        blocked_by=list(blocked_by), scope_refs=list(scope),
        resolution=resolution, claimed_by=claimed_by,
        claim_expires_at=claim_expires_at)


def _resolved(decision_id, **kw) -> DecisionNode:
    rec = kw.pop("rec", None) or _rec(decision_id)
    return _node(decision_id, status=DecisionStatus.RESOLVED, resolution=rec, **kw)


def _claimed(decision_id, *, owner="worker-1", until=LATER, **kw) -> DecisionNode:
    return _node(decision_id, status=DecisionStatus.CLAIMED, claimed_by=owner,
                 claim_expires_at=until, **kw)


def _fog(fog_id="F1", *, clears_with=("R",), title=None) -> FogPatch:
    return FogPatch(fog_id=fog_id, title=title or f"fog {fog_id}",
                    note="not yet a question", clears_with=list(clears_with))


def _map(map_id, nodes, fog=(), generation=1) -> DecisionMap:
    return DecisionMap(map_id=map_id, destination=f"what must be clear for {map_id}",
                       generation=generation, nodes=list(nodes), fog=list(fog),
                       program_ref="P-1")


def _m1(*, r=None, q=None, extra=(), fog=None) -> DecisionMap:
    """M1: R resolved, Q open and blocked by R, fog F1 clearing with R."""
    return _map("M1", [r or _resolved("R"), q or _node("Q", blocked_by=["R"]), *extra],
                fog=[_fog()] if fog is None else fog, generation=2)


def _m2(*, map_id="M2", extra=()) -> DecisionMap:
    """M2: one open question T in another scope."""
    return _map(map_id, [_node("T", scope=["S2"]), *extra], generation=5)


def _clean() -> list[DecisionMap]:
    return [_m1(), _m2()]


def _check(maps, **kw):
    kw.setdefault("now", NOW)
    return check(maps, validator_version="test-1", **kw)


def _codes(result: CheckResult) -> list[str]:
    return [f.code for f in (*result.errors, *result.warnings)]


def _unvalidated_node(valid: DecisionNode, **overrides) -> DecisionNode:
    """A node that skipped validation, as `model_construct` or a bad loader produces."""
    fields = {k: getattr(valid, k) for k in DecisionNode.model_fields}
    fields.update(overrides)
    return DecisionNode.model_construct(**fields)


def _unvalidated_map(valid: DecisionMap, **overrides) -> DecisionMap:
    """A map that skipped validation. Needed around an unvalidated node too:
    building a validated map re-runs the validators of the nodes inside it."""
    fields = {k: getattr(valid, k) for k in DecisionMap.model_fields}
    fields.update(overrides)
    return DecisionMap.model_construct(**fields)


def _m1_with_record_for(record_id: str) -> DecisionMap:
    """M1 with R carrying a record for `record_id`, validation bypassed."""
    valid = _m1()
    r = _unvalidated_node(valid.node("R"), resolution=_rec(record_id))
    return _unvalidated_map(valid, nodes=[r, valid.node("Q")])


def _assert_fails_with_only(result: CheckResult, code: str) -> None:
    assert result.status == "FAIL"
    assert {f.code for f in result.errors} == {code}
    assert result.warnings == []


def _assert_clean_pass(result: CheckResult) -> None:
    assert result.status == "PASS"
    assert result.errors == [] and result.warnings == []


# --------------------------------------------------------------------------- #
# The result model
# --------------------------------------------------------------------------- #


def test_severity_has_exactly_error_then_warning():
    """An extra or reordered severity would change what callers iterate over
    and could introduce a level between failing and not failing."""
    assert [s.name for s in Severity] == ["ERROR", "WARNING"]
    assert all(s.value == s.name for s in Severity)


def test_finding_with_empty_message_is_refused():
    """A finding that does not say what is wrong cannot be acted on; a blank
    or whitespace-only message must not be accepted as one."""
    for message in ("", "   "):
        with pytest.raises(ValidationError):
            Finding(code="duplicate-map-id", severity=Severity.ERROR,
                    message=message, decision_ids=[], fog_ids=[])
    # Control: the same finding with a message constructs.
    Finding(code="duplicate-map-id", severity=Severity.ERROR,
            message="map id 'M1' appears twice", decision_ids=[], fog_ids=[])


def test_finding_code_must_be_a_stable_slug():
    """Callers act on a class of finding by its code; prose or a blank code
    would force them to match messages instead."""
    for code in ("", "Duplicate map id", "dup_map"):
        with pytest.raises(ValidationError):
            Finding(code=code, severity=Severity.ERROR, message="m")
    Finding(code="duplicate-map-id", severity=Severity.ERROR, message="m")


def test_status_cannot_be_handed_in():
    """A receipt whose verdict is an input rather than a consequence could
    say PASS over a list of errors."""
    err = Finding(code="duplicate-map-id", severity=Severity.ERROR, message="m")
    with pytest.raises(ValidationError):
        CheckResult(status="PASS", errors=[err], warnings=[], checked_generation=1,
                    decision_graph_digest="sha256:x", validator_version="v")
    # Control: status derives from errors, and is still part of the dump.
    failing = CheckResult(errors=[err], warnings=[], checked_generation=1,
                          decision_graph_digest="sha256:x", validator_version="v")
    passing = CheckResult(errors=[], warnings=[], checked_generation=1,
                          decision_graph_digest="sha256:x", validator_version="v")
    assert failing.status == "FAIL" and passing.status == "PASS"
    assert failing.model_dump()["status"] == "FAIL"


def test_a_warning_cannot_be_placed_among_errors():
    """If a WARNING finding could sit in `errors`, a warning could fail the
    check after all -- and an ERROR in `warnings` could hide a failure."""
    warn = Finding(code="unusually-large-map", severity=Severity.WARNING, message="m")
    err = Finding(code="duplicate-map-id", severity=Severity.ERROR, message="m")
    base = {"checked_generation": 1, "decision_graph_digest": "sha256:x",
            "validator_version": "v"}
    with pytest.raises(ValidationError):
        CheckResult(errors=[warn], warnings=[], **base)
    with pytest.raises(ValidationError):
        CheckResult(errors=[], warnings=[err], **base)
    # Control: each in its own list is accepted.
    assert CheckResult(errors=[err], warnings=[warn], **base).status == "FAIL"


def test_result_binds_generation_digest_and_validator_version():
    """Section 5: the receipt must bind the generation checked, the graph's
    digest and the validator's version -- a verdict without them cannot be
    matched to the graph it was about."""
    maps = _clean()
    result = _check(maps)
    assert result.checked_generation == 5          # max over M1 (2) and M2 (5)
    assert result.decision_graph_digest == graph_digest(maps)
    assert result.validator_version == "test-1"
    assert check([], validator_version="v").checked_generation == 0
    with pytest.raises(ValidationError):
        check(maps, validator_version="  ")


# --------------------------------------------------------------------------- #
# The clean graph
# --------------------------------------------------------------------------- #


def test_clean_graph_passes_with_no_findings():
    """The baseline every error case flips. A checker that found something in
    a sound graph would make every FAIL below meaningless."""
    _assert_clean_pass(_check(_clean()))
    _assert_clean_pass(_check(_clean(), now=None))


# --------------------------------------------------------------------------- #
# Identity and dependencies
# --------------------------------------------------------------------------- #


def test_duplicate_map_id_fails():
    """Two maps under one id make every reference to that id ambiguous."""
    _assert_fails_with_only(_check([_m1(), _m2(map_id="M1")]), "duplicate-map-id")
    # Control: the same maps under distinct ids.
    _assert_clean_pass(_check([_m1(), _m2(map_id="M2")]))


def test_cross_map_blocker_unresolved_fails():
    """Construction only sees one map: a blocker resolved here but still open
    in another map of the graph is invisible to it."""
    result = _check([_m1(), _m2(extra=[_node("R", scope=["S2"])])])
    _assert_fails_with_only(result, "cross-map-blocker-unresolved")
    assert result.errors[0].decision_ids == ["Q", "R"]
    # Control: the same id, resolved in the other map too.
    _assert_clean_pass(_check([_m1(), _m2(extra=[_resolved("R", scope=["S2"])])]))


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def test_resolution_id_mismatch_fails_even_when_validation_was_bypassed():
    """DecisionNode refuses this at construction; the checker re-checks it on
    purpose, because `model_construct` (or a careless loader) skips validation
    and the fsck must not trust what it was handed."""
    _assert_fails_with_only(_check([_m1_with_record_for("Z"), _m2()]),
                            "resolution-id-mismatch")
    # Control: the same bypass with the matching record.
    _assert_clean_pass(_check([_m1_with_record_for("R"), _m2()]))


def test_resolution_digest_mismatch_fails():
    """A record whose digest does not match its answer and sources has been
    edited after the fact, or was never computed."""
    bad = _resolved("R", rec=_rec("R", digest="sha256:abc"))
    _assert_fails_with_only(_check([_m1(r=bad), _m2()]), "resolution-digest-mismatch")
    # Control: the recomputed digest.
    _assert_clean_pass(_check([_m1(r=_resolved("R", rec=_rec("R"))), _m2()]))


def test_resolution_digest_is_order_independent_over_sources():
    """Source refs are a set; reordering them must not turn a valid record
    into a digest mismatch."""
    assert resolution_digest("a", ["x", "y"]) == resolution_digest("a", ["y", "x"])
    assert resolution_digest("a", ["x"]) != resolution_digest("b", ["x"])


def test_resolution_without_source_fails():
    """An answer with no source is an assertion, which is exactly what a
    ResolutionRecord exists to rule out."""
    bare = _resolved("R", rec=_rec("R", source_refs=()))
    _assert_fails_with_only(_check([_m1(r=bare), _m2()]), "resolution-without-source")
    # Control: one primary source.
    _assert_clean_pass(_check([_m1(r=_resolved("R", rec=_rec("R"))), _m2()]))


# --------------------------------------------------------------------------- #
# Scope and undermining
# --------------------------------------------------------------------------- #


def test_out_of_scope_blocks_active_fails():
    """Out-of-scope work cannot be a live blocker: a node waiting on it waits
    on something nobody will do."""
    q = _node("Q", blocked_by=["R", "O"])
    oos = _node("O", status=DecisionStatus.OUT_OF_SCOPE)
    _assert_fails_with_only(_check([_m1(q=q, extra=[oos]), _m2()]),
                            "out-of-scope-blocks-active")
    # Control: the same blocker, in scope.
    _assert_clean_pass(_check([_m1(q=q, extra=[_node("O")]), _m2()]))


def test_out_of_scope_as_provenance_fails():
    """An OUT_OF_SCOPE question was never answered; offering it as execution
    provenance presents an unanswered question as a settled one."""
    oos = _node("O", status=DecisionStatus.OUT_OF_SCOPE)
    _assert_fails_with_only(_check([_m1(extra=[oos]), _m2()], settled_refs=["O"]),
                            "out-of-scope-as-provenance")
    # Control: the same ref, once O is actually resolved.
    _assert_clean_pass(_check([_m1(extra=[_resolved("O")]), _m2()], settled_refs=["O"]))


def test_undermined_shown_as_settled_fails():
    """The plan's "UNDERMINED node shown as an active accepted premise": an
    undermined premise is not an answer."""
    undermined = _node("U", status=DecisionStatus.UNDERMINED)
    _assert_fails_with_only(_check([_m1(extra=[undermined]), _m2()], settled_refs=["U"]),
                            "undermined-shown-as-settled")
    # Control: the same graph, U not offered as settled.
    _assert_clean_pass(_check([_m1(extra=[undermined]), _m2()], settled_refs=[]))


# --------------------------------------------------------------------------- #
# Claims
# --------------------------------------------------------------------------- #


def test_two_valid_leases_fails():
    """One question held by two live leases means two workers answering it,
    and the second answer silently overwriting the first."""
    maps = [_m1(q=_claimed("Q", owner="w1", blocked_by=["R"])),
            _m2(extra=[_claimed("Q", owner="w2", scope=["S2"])])]
    result = _check(maps)
    _assert_fails_with_only(result, "two-valid-leases")
    assert result.errors[0].map_ids == ["M1", "M2"]
    # Without a clock every CLAIMED node counts as holding its lease.
    _assert_fails_with_only(_check(maps, now=None), "two-valid-leases")
    # Control: the second copy is not claimed.
    _assert_clean_pass(_check([maps[0], _m2(extra=[_node("Q", scope=["S2"])])]))


def test_two_leases_of_which_one_expired_is_only_a_warning():
    """An expired lease is not valid, so it must not count toward
    two-valid-leases -- it is the recoverable warning instead."""
    maps = [_m1(q=_claimed("Q", owner="w1", blocked_by=["R"])),
            _m2(extra=[_claimed("Q", owner="w2", until=EARLIER, scope=["S2"])])]
    result = _check(maps)
    assert result.status == "PASS"
    assert _codes(result) == ["lease-expired-treated-active"]


# --------------------------------------------------------------------------- #
# Fog
# --------------------------------------------------------------------------- #


def test_fog_clears_with_missing_fails_even_when_validation_was_bypassed():
    """Construction refuses a dangling clears_with within one map; a map that
    skipped validation could still carry one, and the fog would then wait
    forever on a decision the graph does not contain."""
    def bypassed(clears_with):
        return _unvalidated_map(_m1(), fog=[_fog(clears_with=clears_with)])
    _assert_fails_with_only(_check([bypassed(["ghost"]), _m2()]),
                            "fog-clears-with-missing")
    # Control: the same bypass, clearing with a decision that exists.
    _assert_clean_pass(_check([bypassed(["R"]), _m2()]))


# --------------------------------------------------------------------------- #
# Graduation
# --------------------------------------------------------------------------- #


def test_graduated_with_blockers_fails():
    """A programme that graduated a map whose own gate still reports blockers
    for that scope has executed on unanswered questions."""
    ref = ProgramRef(program_id="P-1", map_id="M1", graduated=True, scope_refs=["S1"])
    _assert_fails_with_only(_check(_clean(), program_refs=[ref]), "graduated-with-blockers")
    # Control: the same graduation for a scope Q does not block.
    ok = ProgramRef(program_id="P-1", map_id="M1", graduated=True, scope_refs=["S9"])
    _assert_clean_pass(_check(_clean(), program_refs=[ok]))
    # Control: the same scope, not graduated.
    not_yet = ProgramRef(program_id="P-1", map_id="M1", scope_refs=["S1"])
    _assert_clean_pass(_check(_clean(), program_refs=[not_yet]))


def test_program_ref_to_unknown_map_fails():
    """A programme pointing at a map the graph does not contain has a gate
    that nobody can check."""
    ref = ProgramRef(program_id="P-1", map_id="M9")
    _assert_fails_with_only(_check(_clean(), program_refs=[ref]), "program-ref-unknown-map")
    _assert_clean_pass(_check(_clean(), program_refs=[ProgramRef(program_id="P-1",
                                                                  map_id="M1")]))


def test_unresolved_as_settled_fails():
    """A programme that treats an open question -- or one that exists
    nowhere -- as a settled premise is building on nothing."""
    for premise in ("Q", "ghost"):
        ref = ProgramRef(program_id="P-1", map_id="M1", settled_premises=[premise])
        _assert_fails_with_only(_check(_clean(), program_refs=[ref]),
                                "unresolved-as-settled")
    # Control: a premise that is resolved.
    ref = ProgramRef(program_id="P-1", map_id="M1", settled_premises=["R"])
    _assert_clean_pass(_check(_clean(), program_refs=[ref]))


# --------------------------------------------------------------------------- #
# Warnings
# --------------------------------------------------------------------------- #


def test_warning_lease_expired_treated_active_is_reported_and_passes():
    """An expired lease left CLAIMED is recoverable -- the plane returns it
    to the frontier -- so it must be reported without failing the check."""
    result = _check([_m1(q=_claimed("Q", until=EARLIER, blocked_by=["R"])), _m2()])
    assert result.status == "PASS"
    assert _codes(result) == ["lease-expired-treated-active"]
    # Control: a live lease.
    _assert_clean_pass(_check([_m1(q=_claimed("Q", until=LATER, blocked_by=["R"])), _m2()]))
    # Control: without a clock the expiry is not evaluated.
    _assert_clean_pass(_check([_m1(q=_claimed("Q", until=EARLIER, blocked_by=["R"])),
                               _m2()], now=None))


def test_warning_fog_shadows_decision_is_reported_and_passes():
    """Fog titled like a live question is probably that question, stated
    twice; the duplicate should be noticed, not fail the graph."""
    shadow = _fog(title="  Title OF   t ")          # T lives in M2: across maps
    result = _check([_m1(fog=[shadow]), _m2()])
    assert result.status == "PASS"
    assert _codes(result) == ["fog-shadows-decision"]
    assert result.warnings[0].decision_ids == ["T"]
    # Control: the same title matching a *resolved* decision shadows nothing.
    _assert_clean_pass(_check([_m1(fog=[_fog(title="title of R")]), _m2()]))


def test_warning_unusually_large_map_is_reported_and_passes():
    """A very large unresolved map is a smell, not a defect."""
    assert LARGE_MAP_THRESHOLD == 50
    result = _check(_clean(), large_map_threshold=1)          # M1 has 2 nodes
    assert result.status == "PASS"
    assert _codes(result) == ["unusually-large-map"]
    # Control: the same graph at a threshold it does not exceed.
    _assert_clean_pass(_check(_clean(), large_map_threshold=2))
    # Control: above the threshold but fully resolved.
    done = _map("M3", [_resolved("A"), _resolved("B"), _resolved("C")])
    _assert_clean_pass(_check([done], large_map_threshold=1))


def test_warning_resolution_lacks_primary_source_is_reported_and_passes():
    """An answer resting only on summaries or model output is weaker than one
    with a primary source; worth flagging, not worth failing."""
    weak = _resolved("R", rec=_rec("R", source_refs=["summary:notes", "llm:chat-1"]))
    result = _check([_m1(r=weak), _m2()])
    assert result.status == "PASS"
    assert _codes(result) == ["resolution-lacks-primary-source"]
    # Control: the same sources plus one primary.
    mixed = _resolved("R", rec=_rec("R", source_refs=["summary:notes", "doc/primary-1"]))
    _assert_clean_pass(_check([_m1(r=mixed), _m2()]))


def _warnings_only_graph() -> list[DecisionMap]:
    weak = _resolved("R", rec=_rec("R", source_refs=["derived:report"]))
    q = _claimed("Q", until=EARLIER, blocked_by=["R"])
    return [_m1(r=weak, q=q, fog=[_fog(), _fog("F2", title="title of T")]), _m2()]


def test_warning_only_graph_still_passes_and_reports_warnings():
    """The plan's rule: warnings do not silently become failures. A graph
    carrying every warning code at once must still report PASS -- and must
    still list the warnings, or PASS would hide them."""
    result = _check(_warnings_only_graph(), large_map_threshold=1)
    assert result.status == "PASS"
    assert result.errors == []
    assert sorted({f.code for f in result.warnings}) == sorted(WARNING_CODES)
    assert all(f.severity is Severity.WARNING for f in result.warnings)


def test_warning_graph_plus_one_error_fails_and_keeps_its_warnings():
    """Control for the warnings-only case: the same graph with one error
    added fails, so the PASS above is not a checker that never fails."""
    maps = [*_warnings_only_graph(), _m2(map_id="M1")]
    result = _check(maps, large_map_threshold=1)
    assert result.status == "FAIL"
    assert [f.code for f in result.errors] == ["duplicate-map-id"]
    assert set(WARNING_CODES) <= {f.code for f in result.warnings}


# --------------------------------------------------------------------------- #
# The graph digest
# --------------------------------------------------------------------------- #


def test_graph_digest_is_independent_of_map_order():
    """Map order carries no meaning; a digest that depended on it would call
    the same graph two different graphs."""
    m1, m2 = _clean()
    assert graph_digest([m1, m2]) == graph_digest([m2, m1])
    assert graph_digest([m1, m2]).startswith("sha256:")
    # Node and fog order within a map carry no meaning either.
    swapped = _map("M1", list(reversed(m1.nodes)), fog=m1.fog, generation=2)
    assert graph_digest([swapped, m2]) == graph_digest([m1, m2])


def test_graph_digest_changes_when_a_status_changes():
    """A digest that did not move with a status would bind a receipt to a
    graph that has since changed underneath it."""
    base = graph_digest(_clean())
    changed = [_m1(q=_node("Q", status=DecisionStatus.UNDERMINED, blocked_by=["R"])), _m2()]
    assert graph_digest(changed) != base
    # Control: rebuilding the identical graph gives the identical digest.
    assert graph_digest(_clean()) == base


def test_check_result_is_deterministic_regardless_of_map_order():
    """Equal graphs must give equal receipts, or two runs over one graph
    could not be compared."""
    maps = [*_warnings_only_graph(), _m2(map_id="M1")]
    a = _check(maps, large_map_threshold=1).model_dump()
    b = _check(list(reversed(maps)), large_map_threshold=1).model_dump()
    assert a == b


# --------------------------------------------------------------------------- #
# The plan's check list is accounted for
# --------------------------------------------------------------------------- #


def test_plan_check_list_is_accounted_for():
    """A plan check that is neither implemented nor proven impossible is a
    hole; this is where it would show."""
    unaccounted = [name for name in PLAN_CHECKS
                   if name not in CHECK_CODES and name not in ENFORCED_AT_CONSTRUCTION]
    assert unaccounted == []
    # A name in both would claim to be implemented and impossible at once.
    assert not set(CHECK_CODES) & set(ENFORCED_AT_CONSTRUCTION)
    assert all(note.strip() for note in ENFORCED_AT_CONSTRUCTION.values())


def _cycle_map():
    return _map("M", [_node("A", blocked_by=["B"]), _node("B", blocked_by=["A"])])


#: For each construction claim, the construction it says is refused.
CONSTRUCTION_BUILDERS = {
    "duplicate-decision-id": lambda: _map("M", [_node("A"), _node("A")]),
    "duplicate-fog-id": lambda: _map("M", [_node("A")],
                                     fog=[_fog("F", clears_with=["A"]),
                                          _fog("F", clears_with=["A"])]),
    "decision-fog-id-clash": lambda: _map("M", [_node("A")],
                                          fog=[_fog("A", clears_with=[])]),
    "self-block": lambda: _node("A", blocked_by=["A"]),
    "dangling-blocked-by": lambda: _map("M", [_node("A", blocked_by=["ghost"])]),
    "dangling-clears-with": lambda: _map("M", [_node("A")],
                                         fog=[_fog(clears_with=["ghost"])]),
    "dependency-cycle": _cycle_map,
    "resolved-without-record": lambda: _node("A", status=DecisionStatus.RESOLVED),
    "claimed-without-lease": lambda: _node("A", status=DecisionStatus.CLAIMED),
    "claim-fields-without-claim": lambda: _node("A", claimed_by="w1",
                                                claim_expires_at=LATER),
    # Naive on purpose: this is the construction the claim says is refused.
    "naive-claim-expiry": lambda: _claimed("A", until=datetime(2026, 9, 24, 13, 0)),  # noqa: DTZ001
}


def test_construction_builders_are_accounted_for_every_key():
    """A construction claim without a builder here would be an unproven
    claim; a builder without a claim would test nothing."""
    assert set(CONSTRUCTION_BUILDERS) == set(ENFORCED_AT_CONSTRUCTION)


@pytest.mark.parametrize("name", sorted(ENFORCED_AT_CONSTRUCTION))
def test_enforced_at_construction_claims_are_accounted_true(name):
    """`ENFORCED_AT_CONSTRUCTION` says a check is unreachable because
    construction refuses the graph. If construction stopped refusing it, the
    check would silently become a hole; this is where it would show."""
    with pytest.raises(ValidationError):
        CONSTRUCTION_BUILDERS[name]()


def test_construction_builders_control_constructs_when_the_defect_is_removed():
    """Control for the raising builders: the same shapes without the defect
    construct, so the raises above are about the defect, not the builders."""
    _map("M", [_node("A"), _node("B")])
    _map("M", [_node("A")], fog=[_fog("F", clears_with=["A"]), _fog("G", clears_with=["A"])])
    _map("M", [_node("A", blocked_by=["B"]), _node("B")])
    _node("A", status=DecisionStatus.RESOLVED, resolution=_rec("A"))
    _claimed("A", until=LATER)


def _triggering_graphs():
    """For each code `check` can emit, a call whose result contains it."""
    oos = _node("O", status=DecisionStatus.OUT_OF_SCOPE)
    undermined = _node("U", status=DecisionStatus.UNDERMINED)
    fog_bad = _unvalidated_map(_m1(), fog=[_fog(clears_with=["ghost"])])
    return {
        "duplicate-map-id": lambda: _check([_m1(), _m2(map_id="M1")]),
        "cross-map-blocker-unresolved":
            lambda: _check([_m1(), _m2(extra=[_node("R", scope=["S2"])])]),
        "resolution-id-mismatch": lambda: _check([_m1_with_record_for("Z"), _m2()]),
        "resolution-digest-mismatch": lambda: _check(
            [_m1(r=_resolved("R", rec=_rec("R", digest="sha256:abc"))), _m2()]),
        "resolution-without-source": lambda: _check(
            [_m1(r=_resolved("R", rec=_rec("R", source_refs=()))), _m2()]),
        "out-of-scope-blocks-active": lambda: _check(
            [_m1(q=_node("Q", blocked_by=["R", "O"]), extra=[oos]), _m2()]),
        "out-of-scope-as-provenance": lambda: _check(
            [_m1(extra=[oos]), _m2()], settled_refs=["O"]),
        "lease-expired-treated-active": lambda: _check(
            [_m1(q=_claimed("Q", until=EARLIER, blocked_by=["R"])), _m2()]),
        "two-valid-leases": lambda: _check(
            [_m1(q=_claimed("Q", blocked_by=["R"])),
             _m2(extra=[_claimed("Q", owner="w2", scope=["S2"])])]),
        "fog-shadows-decision": lambda: _check(
            [_m1(fog=[_fog(title="title of Q")]), _m2()]),
        "fog-clears-with-missing": lambda: _check([fog_bad, _m2()]),
        "undermined-shown-as-settled": lambda: _check(
            [_m1(extra=[undermined]), _m2()], settled_refs=["U"]),
        "graduated-with-blockers": lambda: _check(_clean(), program_refs=[
            ProgramRef(program_id="P", map_id="M1", graduated=True, scope_refs=["S1"])]),
        "program-ref-unknown-map": lambda: _check(_clean(), program_refs=[
            ProgramRef(program_id="P", map_id="M9")]),
        "unresolved-as-settled": lambda: _check(_clean(), program_refs=[
            ProgramRef(program_id="P", map_id="M1", settled_premises=["Q"])]),
        "unusually-large-map": lambda: _check(_clean(), large_map_threshold=1),
        "resolution-lacks-primary-source": lambda: _check(
            [_m1(r=_resolved("R", rec=_rec("R", source_refs=["chat:thread-9"]))), _m2()]),
    }


def test_every_implemented_code_is_accounted_by_a_triggering_graph():
    """A code in CHECK_CODES that no graph can trigger is a claimed check that
    does not run; one emitted outside CHECK_CODES is an unregistered one."""
    graphs = _triggering_graphs()
    assert set(graphs) == set(CHECK_CODES)
    for code, run in graphs.items():
        result = run()
        assert code in _codes(result), code
        assert set(_codes(result)) <= CHECK_CODES
        expected = Severity.WARNING if code in WARNING_CODES else Severity.ERROR
        assert all(f.severity is expected for f in (*result.errors, *result.warnings)
                   if f.code == code)
