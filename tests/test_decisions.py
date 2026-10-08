"""The decision exploration plane (V3.3 P2-19): two frontiers, never one queue.

One case per rule of the spec, each next to a negative control that changes
only the condition under test and shows the verdict flipping -- a check that
holds whichever way the condition goes is a check of nothing. Most controls are
the *good* side: a resolved blocker does put a node on the frontier, a record
does make RESOLVED acceptable, a fully answered map does graduate. Without
them, a frontier that was always empty and a gate that never opened would pass
every fail-closed case in this file.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from hoh.contracts import Strict
from hoh.decisions import (
    DecisionError,
    DecisionMap,
    DecisionNode,
    DecisionStatus,
    FogPatch,
    ResolutionPath,
    ResolutionRecord,
    claim,
    decision_frontier,
    graduation_blockers,
    resolve,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
ONE_HOUR = timedelta(hours=1)


def _rec(decision_id="A", **kw) -> ResolutionRecord:
    fields = dict(decision_id=decision_id, path=ResolutionPath.RESEARCH,
                  answer="use the existing ledger", source_refs=["doc/ref-1"],
                  resolution_digest="sha256:abc", resolved_by="planner",
                  resolved_at=NOW)
    fields.update(kw)
    return ResolutionRecord(**fields)


def _node(decision_id="A", *, status=DecisionStatus.OPEN, blocked_by=(),
          scope=("S1",), resolution=None, claimed_by=None,
          claim_expires_at=None) -> DecisionNode:
    return DecisionNode(
        decision_id=decision_id, title=f"title of {decision_id}",
        question=f"what about {decision_id}?", status=status,
        blocked_by=list(blocked_by), scope_refs=list(scope),
        resolution=resolution, claimed_by=claimed_by,
        claim_expires_at=claim_expires_at,
    )


def _resolved(decision_id="A", **kw) -> DecisionNode:
    return _node(decision_id, status=DecisionStatus.RESOLVED,
                 resolution=_rec(decision_id), **kw)


def _fog(fog_id="F1", *, clears_with=("A",), out_of_scope=False) -> FogPatch:
    return FogPatch(fog_id=fog_id, title=f"fog {fog_id}", note="not yet a question",
                    clears_with=list(clears_with), out_of_scope=out_of_scope)


def _map(nodes, fog=(), generation=1) -> DecisionMap:
    return DecisionMap(map_id="M1", destination="what must be clear before build",
                       generation=generation, nodes=list(nodes), fog=list(fog),
                       program_ref="P-1")


def _ids(dmap, now=NOW):
    return [n.decision_id for n in decision_frontier(dmap, now=now)]


# --------------------------------------------------------------------------- #
# The vocabulary
# --------------------------------------------------------------------------- #


def test_enums_have_exactly_the_specified_members_in_order():
    """A reordered or extra status would change what every caller iterates
    over; a value that differs from its name would break round-trips."""
    assert [s.value for s in DecisionStatus] == [
        "OPEN", "CLAIMED", "RESOLVED", "UNDERMINED", "OUT_OF_SCOPE"]
    assert [p.value for p in ResolutionPath] == [
        "RESEARCH", "PROTOTYPE", "HUMAN_DECISION", "EXTERNAL_PREREQUISITE"]
    assert all(s.value == s.name for s in DecisionStatus)
    assert all(p.value == p.name for p in ResolutionPath)


def test_models_refuse_unknown_fields():
    """The models sit on the project's Strict base. Without it, a misspelt
    `resolved=True` on a node would be dropped silently and look accepted."""
    for model in (ResolutionRecord, DecisionNode, FogPatch, DecisionMap):
        assert issubclass(model, Strict)
    with pytest.raises(ValidationError):
        DecisionNode(**_node().model_dump(), resolved=True)
    # Control: the same fields without the extra one construct.
    DecisionNode(**_node().model_dump())


def test_fog_patch_has_no_status_or_claim_fields():
    """Fog is not a task. A FogPatch with a status or an owner could be
    scheduled like one, which is the category error the plane forbids."""
    for field in ("status", "claimed_by", "claim_expires_at"):
        assert field not in FogPatch.model_fields
    with pytest.raises(ValidationError):
        FogPatch(**_fog().model_dump(), status=DecisionStatus.OPEN)


# --------------------------------------------------------------------------- #
# ResolutionRecord -- an answer, not an assumption
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field", ["answer", "resolution_digest"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_record_with_an_empty_answer_or_digest_is_refused(field, blank):
    """A record with no answer or no digest records nothing that can be
    checked; accepting it would let an empty record launder an assertion."""
    with pytest.raises(ValidationError):
        _rec(**{field: blank})


def test_record_control_with_answer_and_digest_is_accepted():
    """Negative control: the same record with both fields filled constructs,
    so the refusal above is about the blank field and nothing else."""
    rec = _rec()
    assert rec.answer and rec.resolution_digest


# --------------------------------------------------------------------------- #
# DecisionNode -- structural rules at construction
# --------------------------------------------------------------------------- #


def test_resolved_asserted_without_a_record_is_refused_at_construction():
    """Resolved-by-assertion is the failure this plane exists to prevent: a
    node that merely says RESOLVED would unblock its dependents and pass the
    graduation gate with no answer anywhere."""
    with pytest.raises(ValidationError, match="asserted"):
        _node(status=DecisionStatus.RESOLVED)


def test_resolved_asserted_control_the_same_node_with_a_record_is_accepted():
    """Negative control: the same RESOLVED node carrying a record constructs.
    Without it, a model that refused RESOLVED outright would pass the case
    above."""
    node = _node(status=DecisionStatus.RESOLVED, resolution=_rec("A"))
    assert node.status is DecisionStatus.RESOLVED
    assert node.resolution.decision_id == "A"


def test_resolved_asserted_with_a_record_for_another_decision_is_refused():
    """A record answering B attached to A is still an assertion about A."""
    with pytest.raises(ValidationError):
        _node("A", status=DecisionStatus.RESOLVED, resolution=_rec("B"))


def test_node_blocked_by_itself_is_refused():
    """A self-block can never clear; the node would sit off the frontier
    forever and silently block graduation of its scope."""
    with pytest.raises(ValidationError, match="itself"):
        _node("A", blocked_by=["A"])
    # Control: blocked by a different id constructs.
    assert _node("A", blocked_by=["B"]).blocked_by == ["B"]


def test_claimed_node_without_owner_or_expiry_is_refused():
    """A CLAIMED node with no expiry could never lapse back to the frontier,
    and one with no owner is a lease nobody holds."""
    with pytest.raises(ValidationError):
        _node(status=DecisionStatus.CLAIMED)
    with pytest.raises(ValidationError):
        _node(status=DecisionStatus.CLAIMED, claimed_by="dev")
    # Control: owner and expiry together construct.
    _node(status=DecisionStatus.CLAIMED, claimed_by="dev", claim_expires_at=LATER)


# --------------------------------------------------------------------------- #
# DecisionMap -- structural rules at construction
# --------------------------------------------------------------------------- #


def test_map_with_duplicate_decision_ids_is_refused():
    """Two nodes with one id make every lookup ambiguous: resolving one would
    leave the other open under the same name."""
    with pytest.raises(ValidationError, match="duplicate decision"):
        _map([_node("A"), _node("A")])
    # Control: distinct ids construct.
    _map([_node("A"), _node("B")])


def test_map_with_duplicate_fog_ids_is_refused():
    """Two fog patches with one id: clearing one would hide the other."""
    with pytest.raises(ValidationError, match="duplicate fog"):
        _map([_node("A")], fog=[_fog("F1"), _fog("F1")])
    # Control: distinct fog ids construct.
    _map([_node("A")], fog=[_fog("F1"), _fog("F2")])


def test_map_with_a_dangling_blocked_by_is_refused():
    """A blocker the map does not contain can never be resolved in it; the
    node would be stuck off the frontier with no visible reason."""
    with pytest.raises(ValidationError, match="does not contain"):
        _map([_node("B", blocked_by=["A"])])
    # Control: the same edge with A present constructs.
    _map([_node("A"), _node("B", blocked_by=["A"])])


def test_map_with_an_id_used_for_both_decision_and_fog_is_refused():
    """If a fog id could also be a decision id, `claim` could not tell a
    legal claim from a claim on fog."""
    with pytest.raises(ValidationError):
        _map([_node("A")], fog=[_fog("A")])


# --------------------------------------------------------------------------- #
# decision_frontier -- the two frontiers are not one queue
# --------------------------------------------------------------------------- #


def test_frontier_never_carries_fog():
    """The Decision Frontier holds questions, never fog. A frontier that
    returned fog would hand unformulated scope to a worker as if it were a
    runnable item -- the two queues merged."""
    dmap = _map([_node("A")], fog=[_fog("F1", clears_with=[]), _fog("F2")])
    out = decision_frontier(dmap, now=NOW)
    assert all(isinstance(n, DecisionNode) for n in out)
    assert not any(isinstance(n, FogPatch) for n in out)
    assert _ids(dmap) == ["A"]


def test_frontier_excludes_a_node_blocked_by_an_unresolved_question():
    """B waits on A. Putting B on the frontier while A is open would invite an
    answer built on a question nobody has answered."""
    dmap = _map([_node("A"), _node("B", blocked_by=["A"])])
    assert "B" not in _ids(dmap)


def test_frontier_control_the_same_node_with_its_blocker_resolved_is_present():
    """Negative control: change only A to RESOLVED and B appears. Without it,
    a frontier that never listed blocked nodes at all would pass above."""
    dmap = _map([_resolved("A"), _node("B", blocked_by=["A"])])
    assert _ids(dmap) == ["B"]


def test_frontier_excludes_a_node_blocked_by_an_undermined_premise():
    """An undermined premise is not an answer. Treating it as one is how a
    graph drifts while every local edit looks valid."""
    dmap = _map([_node("A", status=DecisionStatus.UNDERMINED),
                 _node("B", blocked_by=["A"])])
    assert _ids(dmap) == []


def test_frontier_excludes_out_of_scope_and_resolved_nodes():
    """Only OPEN (or lapsed-claim) questions are resolvable now; an
    OUT_OF_SCOPE or already answered one on the frontier is wasted work."""
    dmap = _map([_node("A", status=DecisionStatus.OUT_OF_SCOPE), _resolved("B"),
                 _node("C")])
    assert _ids(dmap) == ["C"]


# --------------------------------------------------------------------------- #
# claim
# --------------------------------------------------------------------------- #


def test_claiming_fog_fails_and_says_fog_cannot_be_claimed():
    """Fog cannot be leased or run. A claim that accepted a fog id would turn
    unformulated scope into hidden execution work."""
    dmap = _map([_node("A")], fog=[_fog("F1")])
    with pytest.raises(DecisionError, match="fog cannot be claimed"):
        claim(dmap, "F1", owner="dev", expires_at=LATER, now=NOW)


def test_claiming_fog_control_claiming_a_frontier_node_succeeds():
    """Negative control: the same call on a frontier node claims it. Without
    it, a `claim` that refused everything would pass the fog case."""
    dmap = _map([_node("A")], fog=[_fog("F1")])
    out = claim(dmap, "A", owner="dev", expires_at=LATER, now=NOW)
    node = out.node("A")
    assert (node.status, node.claimed_by, node.claim_expires_at) == (
        DecisionStatus.CLAIMED, "dev", LATER)
    # The input is not mutated.
    assert dmap.node("A").status is DecisionStatus.OPEN


def test_claim_refuses_a_node_not_on_the_frontier():
    """Claiming a blocked or unknown node leases a question nobody can answer
    yet -- exactly the invented work the plane exists to avoid."""
    dmap = _map([_node("A"), _node("B", blocked_by=["A"])])
    with pytest.raises(DecisionError, match="frontier"):
        claim(dmap, "B", owner="dev", expires_at=LATER, now=NOW)
    with pytest.raises(DecisionError):
        claim(dmap, "NOPE", owner="dev", expires_at=LATER, now=NOW)


def test_claim_refuses_a_node_held_by_a_live_claim():
    """Two owners on one question would answer it twice, differently."""
    dmap = claim(_map([_node("A")]), "A", owner="dev", expires_at=LATER, now=NOW)
    with pytest.raises(DecisionError, match="live claim"):
        claim(dmap, "A", owner="other", expires_at=LATER, now=NOW)
    # Control: once the claim has lapsed, another owner may take it.
    later = LATER + timedelta(minutes=1)
    out = claim(dmap, "A", owner="other", expires_at=later + timedelta(hours=1), now=later)
    assert out.node("A").claimed_by == "other"


def test_a_live_claim_keeps_the_node_off_the_frontier():
    """While a claim is live the question is being worked; listing it again
    would hand it to a second worker."""
    dmap = claim(_map([_node("A")]), "A", owner="dev", expires_at=LATER, now=NOW)
    assert _ids(dmap, NOW) == []


def test_an_expired_claim_puts_the_node_back_on_the_frontier():
    """Negative control of the live-claim case: only the clock moves past the
    expiry, and the node returns. Otherwise an abandoned claim would strand
    the question forever."""
    dmap = claim(_map([_node("A")]), "A", owner="dev", expires_at=LATER, now=NOW)
    assert _ids(dmap, LATER) == ["A"]
    assert _ids(dmap, LATER + timedelta(hours=1)) == ["A"]


# --------------------------------------------------------------------------- #
# resolve
# --------------------------------------------------------------------------- #


def test_resolve_does_not_mutate_its_input_and_bumps_the_generation():
    """Correction is by generation, not by editing in place. A `resolve` that
    mutated its input would rewrite history under every holder of the old map."""
    dmap = _map([_node("A")], generation=3)
    before = dmap.model_dump()
    out = resolve(dmap, _rec("A"), now=NOW)
    assert dmap.model_dump() == before
    assert out.generation == 4 > dmap.generation
    node = out.node("A")
    assert node.status is DecisionStatus.RESOLVED
    assert node.resolution == _rec("A")


def test_resolve_clears_the_claim_of_the_holder_who_answers_it():
    """A resolved node still carrying its claim would look leased.

    The scenario changed under review, not the assertion: it used to claim as
    `dev` and answer as `planner`, which is now refused (see the case below).
    The holder answering their own claim is the ordinary path, and it is what
    this case is about.
    """
    dmap = claim(_map([_node("A")]), "A", owner="dev", expires_at=LATER, now=NOW)
    node = resolve(dmap, _rec("A", resolved_by="dev"), now=NOW).node("A")
    assert (node.claimed_by, node.claim_expires_at) == (None, None)


def test_resolve_refuses_someone_elses_live_claim():
    """Reviewer A, F2 sub-finding: `claim` guarded the lease and `resolve` did
    not, so a second worker could answer a question another one held and the
    lease vanished with it -- the "live claim that can be stolen" case, through
    the front door.
    """
    dmap = claim(_map([_node("A")]), "A", owner="dev", expires_at=LATER, now=NOW)
    with pytest.raises(DecisionError, match="may not answer it"):
        resolve(dmap, _rec("A", resolved_by="someone-else"), now=NOW)


def test_an_expired_claim_does_not_stop_anyone_answering():
    """The negative control for the case above: only the clock moves. An
    abandoned lease must not be able to park a question forever."""
    dmap = claim(_map([_node("A")]), "A", owner="dev", expires_at=LATER, now=NOW)
    after = resolve(dmap, _rec("A", resolved_by="someone-else"), now=LATER + ONE_HOUR)
    assert after.node("A").status is DecisionStatus.RESOLVED


def test_resolve_refuses_a_second_record():
    """A second record would overwrite the first in place and erase the
    answer that dependents were built on."""
    dmap = resolve(_map([_node("A")]), _rec("A"), now=NOW)
    with pytest.raises(DecisionError, match="already resolved"):
        resolve(dmap, _rec("A", answer="something else"), now=NOW)


def test_resolve_refuses_an_out_of_scope_node():
    """Answering an out-of-scope question spends effort on something the map
    has already excluded, and would make it look in scope again."""
    dmap = _map([_node("A", status=DecisionStatus.OUT_OF_SCOPE)])
    with pytest.raises(DecisionError, match="OUT_OF_SCOPE"):
        resolve(dmap, _rec("A"), now=NOW)


def test_resolve_refuses_an_unknown_decision_and_fog():
    """A record for a node the map lacks answers nothing here; one naming a
    fog patch would resolve fog as if it were a question."""
    dmap = _map([_node("A")], fog=[_fog("F1")])
    with pytest.raises(DecisionError, match="does not contain"):
        resolve(dmap, _rec("Z"), now=NOW)
    with pytest.raises(DecisionError, match="fog"):
        resolve(dmap, _rec("F1"), now=NOW)


# --------------------------------------------------------------------------- #
# graduation_blockers
# --------------------------------------------------------------------------- #


def test_graduation_refused_while_an_in_scope_question_is_open():
    """An open question on the intended scope means the route is not known;
    graduating would turn it into a work package nobody can do yet."""
    blockers = graduation_blockers(_map([_node("A")]), scope_refs=["S1"])
    assert blockers and "'A'" in blockers[0]


def test_graduation_allowed_when_the_in_scope_question_is_resolved():
    """Negative control: change only A to RESOLVED and the gate opens.
    Without it, a gate that never opened would pass every refusal here."""
    assert graduation_blockers(_map([_resolved("A")]), scope_refs=["S1"]) == []


@pytest.mark.parametrize("status", [DecisionStatus.CLAIMED, DecisionStatus.UNDERMINED])
def test_graduation_refused_for_claimed_or_undermined_in_scope_nodes(status):
    """Claimed is being worked, not answered; undermined is worse than open."""
    kw = dict(claimed_by="dev", claim_expires_at=LATER) if status is DecisionStatus.CLAIMED else {}
    assert graduation_blockers(_map([_node("A", status=status, **kw)]), scope_refs=["S1"])


def test_graduation_ignores_an_open_question_outside_the_scope():
    """Scope is the point of the gate: an open question on another scope must
    not hold this one back, or no partial graduation would ever happen."""
    dmap = _map([_resolved("A"), _node("B", scope=["OTHER"])])
    assert graduation_blockers(dmap, scope_refs=["S1"]) == []
    # Control: name B's scope and it blocks.
    assert graduation_blockers(dmap, scope_refs=["S1", "OTHER"])


def test_graduation_refused_again_when_in_scope_fog_points_at_an_unresolved_node():
    """Same map as the allowed case, plus fog that is not out of scope and
    waits on an unresolved question elsewhere. Ignoring it would graduate with
    known scope still unformulated."""
    dmap = _map([_resolved("A"), _node("C", scope=["OTHER"])],
                fog=[_fog("F2", clears_with=["C"])])
    blockers = graduation_blockers(dmap, scope_refs=["S1"])
    assert len(blockers) == 1 and "fog 'F2'" in blockers[0]


def test_graduation_control_out_of_scope_fog_or_cleared_fog_does_not_block():
    """Negative controls for the fog case: the same fog marked out_of_scope,
    or with its node resolved, lets the map graduate."""
    out = _map([_resolved("A"), _node("C", scope=["OTHER"])],
               fog=[_fog("F2", clears_with=["C"], out_of_scope=True)])
    assert graduation_blockers(out, scope_refs=["S1"]) == []
    cleared = _map([_resolved("A"), _resolved("C", scope=["OTHER"])],
                   fog=[_fog("F2", clears_with=["C"])])
    assert graduation_blockers(cleared, scope_refs=["S1"]) == []


@pytest.mark.parametrize("scope", [[], [""]])
def test_graduation_into_an_empty_scope_is_refused(scope):
    """"Graduate into what?" is not answerable. A fully resolved map that
    graduated into nothing would pass a gate that gated nothing."""
    blockers = graduation_blockers(_map([_resolved("A")]), scope_refs=scope)
    assert blockers and "graduate into what" in blockers[0]
    # Control: the same map with a named scope graduates.
    assert graduation_blockers(_map([_resolved("A")]), scope_refs=["S1"]) == []


# --------------------------------------------------------------------------- #
# Review repairs (reviewer A, 2026-09-23)
#
# The product wrote this module and its own tests, and an independent reviewer
# ran 53 mutations against them: 46 caught, 7 survived, and ten findings. Two
# were fail-opens on the exact rule the plane exists to enforce. Each case below
# names the finding it closes, and each has the control that shows it is about
# the condition under test.
# --------------------------------------------------------------------------- #


def test_a_structural_rule_cannot_be_undone_by_assignment():
    """Reviewer A F1, the most serious finding. Every rule in this module was a
    rule about *construction*: the models sat on a base with no
    `validate_assignment`, so a node refused at construction could be produced by
    one assignment afterwards -- and then it unblocked its dependents and the
    graduation gate returned []."""
    dmap = _map([_node("A"), _node("B", blocked_by=["A"])])
    with pytest.raises(ValidationError, match="frozen"):
        dmap.node("A").status = DecisionStatus.RESOLVED
    # The invariant still holds afterwards: nothing was half-applied. The first
    # repair used `validate_assignment` alone and failed exactly here -- pydantic
    # sets the attribute before the model validator runs, so the assignment
    # raised and the node was RESOLVED with no record anyway.
    assert dmap.node("A").status is DecisionStatus.OPEN
    assert _ids(dmap) == ["A"]


def test_a_record_cannot_be_overwritten_by_assignment_either():
    """Same finding, the other half: "a record is never overwritten in place --
    correct it in a new generation" was a statement about `resolve`, and the
    record was a writable attribute."""
    dmap = resolve(_map([_node("A")]), _rec("A"), now=NOW)
    with pytest.raises(ValidationError, match="frozen"):
        dmap.node("A").resolution = _rec("B")           # a record for another decision
    assert dmap.node("A").resolution.decision_id == "A"
    assert dmap.generation == 2


def test_a_blank_answer_cannot_be_assigned_into_a_record():
    """And the third: the record's non-blank validators were construction-only,
    so an answer could be emptied after the fact and the gate opened on it."""
    rec = _rec("A")
    with pytest.raises(ValidationError, match="frozen"):
        rec.answer = "   "
    with pytest.raises(ValidationError, match="frozen"):
        rec.resolution_digest = ""
    assert rec.answer and rec.resolution_digest


def test_resolve_refuses_a_question_standing_on_an_unresolved_premise():
    """Reviewer A F2. `decision_frontier` and `claim` both refuse a node whose
    premise is unresolved; `resolve` -- the only function that sets a node
    RESOLVED -- did not, and `graduation_blockers` never reads `blocked_by`, so
    the map graduated on an answer built on an unanswered question."""
    dmap = _map([_node("A", scope=("E2",)), _node("B", blocked_by=["A"])])
    with pytest.raises(DecisionError, match="nothing has resolved"):
        resolve(dmap, _rec("B"), now=NOW)


def test_resolve_refuses_a_question_whose_premise_is_undermined():
    """The same finding in the shape the reviewer reproduced it: A is UNDERMINED,
    B is blocked by A, and the gate returned [] after B was answered."""
    dmap = _map([_node("A", status=DecisionStatus.UNDERMINED, scope=("E2",)),
                 _node("B", blocked_by=["A"])])
    with pytest.raises(DecisionError, match="nothing has resolved"):
        resolve(dmap, _rec("B"), now=NOW)
    assert graduation_blockers(dmap, scope_refs=["S1"]), \
        "and the gate must still be blocked, since B was never answered"


def test_resolve_control_a_resolved_premise_lets_the_answer_through():
    """The control for both cases above: only the premise's status changes."""
    dmap = _map([_resolved("A", scope=("E2",)), _node("B", blocked_by=["A"])])
    after = resolve(dmap, _rec("B"), now=NOW)
    assert after.node("B").status is DecisionStatus.RESOLVED
    assert graduation_blockers(after, scope_refs=["S1"]) == []


def test_an_undermined_node_must_be_reopened_before_it_can_be_answered():
    """Reviewer A F2 again, at the node itself rather than its dependents: an
    UNDERMINED question answered directly would bury the fact that its premise
    had collapsed."""
    dmap = _map([_node("A", status=DecisionStatus.UNDERMINED)])
    with pytest.raises(DecisionError, match="UNDERMINED"):
        resolve(dmap, _rec("A"), now=NOW)


def test_claim_fields_on_a_node_that_is_not_claimed_are_refused():
    """Reviewer A F3. `_live_claim` only looks at CLAIMED nodes, so a lease on an
    OPEN node was invisible: the node appeared on the frontier and a second
    worker's claim silently replaced the first one's."""
    with pytest.raises(ValidationError, match="claim fields"):
        _node("A", claimed_by="alice", claim_expires_at=LATER)
    # Control: the same fields with the matching status construct.
    _node("A", status=DecisionStatus.CLAIMED, claimed_by="alice",
          claim_expires_at=LATER)


def test_a_claim_expiry_without_a_timezone_is_refused():
    """Reviewer A F7: a naive datetime is legal JSON and crashed the frontier the
    moment it met an aware `now`. An instant without a zone is not an instant."""
    with pytest.raises(ValidationError, match="timezone"):
        _node("A", status=DecisionStatus.CLAIMED, claimed_by="alice",
              claim_expires_at=datetime(2026, 9, 24, 13, 0))


def test_fog_that_waits_on_an_id_the_map_does_not_have_is_refused():
    """Reviewer A F6: `blocked_by` had referential integrity and `clears_with` did
    not, so one typo made a patch of fog wait forever on an id that does not
    exist -- and the gate reported it as a blocker nobody could clear."""
    with pytest.raises(ValidationError, match="clears_with"):
        _map([_node("A")], fog=[_fog("F1", clears_with=["TYPO"])])
    # Control: pointing at a node the map has constructs.
    _map([_node("A")], fog=[_fog("F1", clears_with=["A"])])


def test_a_cycle_in_the_blocking_graph_is_refused():
    """Reviewer A F8: no question in a cycle can ever reach the frontier, so the
    map could never graduate and nothing said why."""
    with pytest.raises(ValidationError, match="cycle"):
        _map([_node("A", blocked_by=["B"]), _node("B", blocked_by=["A"])])
    with pytest.raises(ValidationError, match="cycle"):
        _map([_node("A", blocked_by=["B"]), _node("B", blocked_by=["C"]),
              _node("C", blocked_by=["A"])])
    # Control: the same three nodes in a chain construct, and the frontier is
    # the end of it.
    chain = _map([_node("A", blocked_by=["B"]), _node("B", blocked_by=["C"]),
                  _node("C")])
    assert _ids(chain) == ["C"]


@pytest.mark.parametrize("field", ["map_id", "destination"])
def test_a_map_without_an_id_or_a_destination_is_refused(field):
    """Reviewer A F9. The Destination is the whole point of a Decision Map --
    "what must become clear before this effort can graduate into execution?" --
    and it could be blank."""
    fields = {"map_id": "M1", "destination": "d", "nodes": [], "fog": []}
    fields[field] = "   "
    with pytest.raises(ValidationError):
        DecisionMap(**fields)


@pytest.mark.parametrize("field", ["title", "question"])
def test_a_node_without_a_title_or_a_question_is_refused(field):
    """A node whose question is blank is not a question."""
    fields = {"decision_id": "A", "title": "t", "question": "q"}
    fields[field] = ""
    with pytest.raises(ValidationError):
        DecisionNode(**fields)


def test_an_unresolved_question_that_names_no_scope_blocks_every_scope():
    """Reviewer A F4(b): an OPEN node with no `scope_refs` blocked nothing, for
    any scope. A question whose scope nobody wrote down has not been shown to be
    outside this one, and OUT_OF_SCOPE is the status that says it is."""
    blockers = graduation_blockers(_map([_node("A", scope=())]), scope_refs=["S1"])
    assert any("names no scope" in b for b in blockers)
    # Control: the same node marked out of scope does not block.
    assert graduation_blockers(
        _map([_node("A", scope=(), status=DecisionStatus.OUT_OF_SCOPE)]),
        scope_refs=["S1"]) == []


def test_fog_that_names_nothing_that_would_clear_it_blocks_graduation():
    """Reviewer A F4(a). The vaguest fog there is -- in scope, with nothing
    identified that would make it formulable -- graduated the map, because an
    empty `clears_with` produced no blocker at all. The source spec requires the
    remaining fog to be affirmatively justified, not merely quiet."""
    blockers = graduation_blockers(
        _map([_resolved("A")], fog=[_fog("F1", clears_with=())]), scope_refs=["S1"])
    assert any("names nothing that would clear it" in b for b in blockers)


@pytest.mark.parametrize("justified", [
    {"out_of_scope": True},
    {"deferred_reason": "deferred by policy to the next epoch"},
])
def test_fog_with_an_affirmative_justification_does_not_block(justified):
    """The control, and the reason the field exists: the plan allows fog to be
    out of scope, deferred by policy, or not required for this epoch. Each of
    those is something someone wrote down."""
    patch = FogPatch(fog_id="F1", title="f", note="n", clears_with=[], **justified)
    assert graduation_blockers(_map([_resolved("A")], fog=[patch]),
                              scope_refs=["S1"]) == []


def test_a_claim_needs_an_owner_and_an_expiry_in_the_future():
    """Reviewer A F5: both guards worked and neither was tested, so either could
    have been deleted without turning the suite red."""
    dmap = _map([_node("A")])
    with pytest.raises(DecisionError, match="needs an owner"):
        claim(dmap, "A", owner="   ", expires_at=LATER, now=NOW)
    with pytest.raises(DecisionError, match="must expire after now"):
        claim(dmap, "A", owner="dev", expires_at=NOW, now=NOW)
    # Control: an owner and a future expiry are accepted.
    assert claim(dmap, "A", owner="dev", expires_at=LATER,
                 now=NOW).node("A").status is DecisionStatus.CLAIMED


def test_fog_pointing_at_an_out_of_scope_decision_still_blocks():
    """Reviewer A F5, the fail-closed direction that was untested: the gate asks
    whether the named decision is RESOLVED, not whether it is unresolved, so fog
    waiting on an OUT_OF_SCOPE node blocks. Rewriting that test to the fail-open
    variant kept all 42 original tests green."""
    dmap = _map([_node("A", status=DecisionStatus.OUT_OF_SCOPE, scope=())],
                fog=[_fog("F1", clears_with=["A"])])
    blockers = graduation_blockers(dmap, scope_refs=["S1"])
    assert any("F1" in b and "waits on unresolved" in b for b in blockers)


def test_the_frontier_is_in_map_order():
    """Reviewer A F5: the docstring promises map order and a mutation reversing
    it survived. Order is what makes the frontier a queue anyone can predict."""
    dmap = _map([_node("C"), _node("A"), _node("B")])
    assert _ids(dmap) == ["C", "A", "B"]
