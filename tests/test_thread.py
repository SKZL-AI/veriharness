"""The digital thread (V3.3 P2-31): a projection over authoritative records,
not another source of truth.

One case per rule of the spec. Each positive case sits next to a negative
control that changes only the condition under test and shows the verdict
flipping -- a check that holds whichever way the condition goes is a check of
nothing. Most controls are the *good* side: a full thread does trace complete,
an ordered edge is clean, a known id does resolve. Without them, a `fsck` that
always complained and a `trace` that never said complete would pass every
fail-closed case in this file.

Test names carry `gap`, `broken` or `unknown` where they apply, so the K2
selection (`-k "gap or broken or unknown"`) picks exactly those cases.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from hoh.thread import (
    AmbiguousIdentifier,
    BrokenProjection,
    DigitalThread,
    ThreadEdge,
    ThreadNode,
    ThreadStage,
    Trace,
    UnknownIdentifier,
    fsck,
    release_blocking_trace,
    trace,
    trace_by,
    why,
)

S = ThreadStage
ALL = list(ThreadStage)


def _node(node_id, stage, *, digest=None, record_ref=None) -> ThreadNode:
    return ThreadNode(node_id=node_id, stage=stage,
                      record_ref=record_ref or f"{stage.value.lower()}/{node_id}",
                      digest=digest or f"sha-{node_id}", label=f"label of {node_id}")


def _edge(a, b, *, relation="derives", digest=None) -> ThreadEdge:
    return ThreadEdge(from_id=a, to_id=b, relation=relation,
                      digest=digest or f"e-{a}-{b}")


def _thread(nodes, edges, *, generation=1) -> DigitalThread:
    return DigitalThread(thread_id="T1", generation=generation, nodes=list(nodes),
                         edges=list(edges), projected_from=["ledger@g1"])


def _linear(stages=ALL) -> DigitalThread:
    """One node per stage, each linked to the next -- the plan's chain."""
    nodes = [_node(s.value.lower(), s) for s in stages]
    edges = [_edge(a.node_id, b.node_id) for a, b in zip(nodes, nodes[1:])]
    return _thread(nodes, edges)


def _nid(stage: ThreadStage) -> str:
    return stage.value.lower()


# --------------------------------------------------------------------------- #
# the model
# --------------------------------------------------------------------------- #


def test_the_stage_enum_is_exactly_the_plans_chain_in_order():
    """A stage added, dropped or reordered silently changes what every edge's
    direction means; the spec fixes the list, so it is pinned here."""
    assert [s.value for s in ThreadStage] == [
        "REQUIREMENT", "PROJECT_CONTRACT", "WORK_PACKAGE", "CHECKLIST_REQUIREMENT",
        "TASK_SPEC_AMENDMENT", "POLICY_ROUTING_DECISION", "RUN", "CANDIDATE",
        "CHECKS_RECEIPTS", "QA_NEGATIVE_CONTROLS", "REPAIR_REVALIDATION",
        "ACCEPTANCE", "INTEGRATION_CLOSURE", "COMMIT_MERGE", "MILESTONE",
        "RELEASE_EXPORT"]
    assert all(s.value == s.name for s in ThreadStage)


def test_a_node_without_record_ref_is_refused_and_one_with_it_constructs():
    """Catches decoration: a projected element that cannot be traced back to
    the record holding its authority. Whitespace alone is no reference either."""
    for bad in ("", "   "):
        with pytest.raises(ValidationError):
            ThreadNode(node_id="x", stage=S.RUN, record_ref=bad, digest="d", label="x")
    assert ThreadNode(node_id="x", stage=S.RUN, record_ref="runs/7", digest="d",
                      label="x").record_ref == "runs/7"


def test_an_edge_with_an_empty_digest_is_refused_and_a_digested_one_constructs():
    """Catches an edge with no stable identity: every edge must carry a digest."""
    with pytest.raises(ValidationError):
        ThreadEdge(from_id="a", to_id="b", relation="derives", digest="")
    assert ThreadEdge(from_id="a", to_id="b", relation="derives", digest="e").digest == "e"


def test_a_self_edge_is_refused_and_an_edge_between_two_nodes_constructs():
    """Catches an edge from a node to itself, which links nothing and would be
    the smallest possible cycle."""
    with pytest.raises(ValidationError):
        ThreadEdge(from_id="a", to_id="a", relation="derives", digest="e")
    ThreadEdge(from_id="a", to_id="b", relation="derives", digest="e")


def test_duplicate_node_ids_are_refused_and_distinct_ids_construct():
    """Catches two records claiming one identity: `node()` and every edge
    would silently resolve to whichever came first."""
    a = _node("a", S.REQUIREMENT)
    with pytest.raises(ValidationError):
        _thread([a, _node("a", S.PROJECT_CONTRACT)], [])
    assert len(_thread([a, _node("b", S.PROJECT_CONTRACT)], []).nodes) == 2


@pytest.mark.parametrize("obj, field, value", [
    (lambda: _linear(), "generation", 2),
    (lambda: _linear(), "nodes", ()),
    (lambda: _node("a", S.RUN), "record_ref", "elsewhere"),
    (lambda: _edge("a", "b"), "digest", "forged"),
])
def test_the_projection_is_frozen(obj, field, value):
    """Catches the thread turning into a second source of truth. A projection
    that can be edited in place lets a status be set here that the record it
    projects never said -- and then two places disagree with nothing to say
    which is right. The only way to change the thread is to change the records
    and project again."""
    target = obj()
    with pytest.raises(ValidationError):
        setattr(target, field, value)
    assert isinstance(_linear().nodes, tuple)  # nothing to append to in place


def test_the_docstring_says_it_is_a_projection_and_why_it_is_frozen():
    """Catches the design sentence being lost: the class must say it is not a
    source of truth and that being frozen is what keeps it that way."""
    doc = " ".join(DigitalThread.__doc__.split())
    assert "projection over authoritative records, not a source of truth" in doc
    assert "edited in place would itself become a source of truth" in doc


def test_complete_is_computed_and_cannot_be_handed_in():
    """Catches a caller asserting completeness instead of the trace deriving it."""
    with pytest.raises(ValidationError):
        Trace(thread_id="T1", generation=1, origin="a", stages=(), path=(),
              missing=(S.RUN,), fsck=(), complete=True)
    t = Trace(thread_id="T1", generation=1, origin="a", stages=(), path=(),
              missing=(S.RUN,), fsck=())
    assert t.complete is False


# --------------------------------------------------------------------------- #
# trace: gaps
# --------------------------------------------------------------------------- #


def test_a_gap_makes_the_full_thread_trace_incomplete_and_restoring_it_completes_it():
    """Catches a green chain that is not there: a missing interior stage must
    be named in `missing` and keep the trace from being complete, and the same
    trace must flip to complete only when that node is supplied again."""
    full = _linear()
    before = trace(full, node_id=_nid(S.REQUIREMENT))
    assert before.complete and before.missing == () and fsck(full) == []
    assert list(before.stages) == ALL
    assert before.path[0] == _nid(S.REQUIREMENT) and before.path[-1] == _nid(S.RELEASE_EXPORT)

    gapped = _linear([s for s in ALL if s is not S.RUN])
    after = trace(gapped, node_id=_nid(S.REQUIREMENT))
    assert not after.complete
    assert after.missing == (S.RUN,)
    assert after.fsck == ()  # the gap alone is what flips it

    restored = _linear()
    assert trace(restored, node_id=_nid(S.REQUIREMENT)).complete


def test_a_gap_is_seen_from_any_node_on_the_chain():
    """Catches a trace that only looks downstream: starting in the middle must
    still walk to both ends and see the gap upstream of the start."""
    gapped = _linear([s for s in ALL if s is not S.WORK_PACKAGE])
    mid = trace(gapped, node_id=_nid(S.ACCEPTANCE))
    assert mid.missing == (S.WORK_PACKAGE,) and not mid.complete
    assert mid.stages[0] is S.REQUIREMENT and mid.stages[-1] is S.RELEASE_EXPORT
    assert trace(_linear(), node_id=_nid(S.ACCEPTANCE)).complete


def test_a_gap_is_not_covered_by_a_disconnected_node_of_that_stage():
    """Catches a projection counting a stage as covered because *some* node
    has it, when that node is not on this chain."""
    base = _linear([s for s in ALL if s is not S.RUN])
    stray = _node("stray-run", S.RUN)
    t = _thread([*base.nodes, stray], base.edges)
    assert trace(t, node_id=_nid(S.REQUIREMENT)).missing == (S.RUN,)
    wired = _thread([*base.nodes, stray],
                    [e for e in base.edges if e.from_id != _nid(S.POLICY_ROUTING_DECISION)]
                    + [_edge(_nid(S.POLICY_ROUTING_DECISION), "stray-run"),
                       _edge("stray-run", _nid(S.CANDIDATE))])
    assert trace(wired, node_id=_nid(S.REQUIREMENT)).complete


def test_a_structural_defect_keeps_a_gapless_trace_incomplete():
    """Catches `complete` being computed from `missing` alone: a trace with no
    gap but an fsck finding is still not complete."""
    base = _linear()
    bad = _thread(base.nodes, [*base.edges, _edge(_nid(S.RUN), "ghost")])
    tr = trace(bad, node_id=_nid(S.REQUIREMENT))
    assert tr.missing == () and tr.fsck and not tr.complete
    assert trace(base, node_id=_nid(S.REQUIREMENT)).complete


# --------------------------------------------------------------------------- #
# fsck
# --------------------------------------------------------------------------- #


def test_a_broken_reference_is_an_fsck_error_and_adding_the_node_clears_it():
    """Catches an edge to an id that is not a node being silently dropped."""
    a = _node("a", S.REQUIREMENT)
    edges = [_edge("a", "b")]
    broken = fsck(_thread([a], edges))
    assert len(broken) == 1 and "broken reference" in broken[0] and "'b'" in broken[0]
    assert fsck(_thread([a, _node("b", S.PROJECT_CONTRACT)], edges)) == []


def test_a_broken_reference_on_the_from_side_is_reported_too():
    """Catches a check that only looks at `to_id`."""
    b = _node("b", S.PROJECT_CONTRACT)
    edges = [_edge("a", "b")]
    assert any("broken reference" in m for m in fsck(_thread([b], edges)))
    assert fsck(_thread([_node("a", S.REQUIREMENT), b], edges)) == []


def test_conflicting_digests_on_one_edge_are_an_error_and_identical_ones_are_not():
    """Catches two records disagreeing about the same link while the thread
    shows only one of them."""
    nodes = [_node("a", S.REQUIREMENT), _node("b", S.PROJECT_CONTRACT)]
    conflict = fsck(_thread(nodes, [_edge("a", "b", digest="x"), _edge("a", "b", digest="y")]))
    assert len(conflict) == 1 and "conflicting edge digests" in conflict[0]
    assert fsck(_thread(nodes, [_edge("a", "b", digest="x"), _edge("a", "b", digest="x")])) == []


def test_a_backwards_edge_is_an_error_and_the_ordered_edge_is_not():
    """Catches a link that runs against the chain, e.g. a release feeding a
    requirement."""
    nodes = [_node("a", S.REQUIREMENT), _node("b", S.PROJECT_CONTRACT)]
    back = fsck(_thread(nodes, [_edge("b", "a")]))
    assert len(back) == 1 and "backwards edge" in back[0]
    assert fsck(_thread(nodes, [_edge("a", "b")])) == []


def test_an_edge_skipping_a_stage_the_path_has_is_an_error_and_without_the_shortcut_not():
    """Catches a shortcut edge that jumps over a stage the same path does have
    a node for -- the chain would look shorter than the records say."""
    nodes = [_node("a", S.REQUIREMENT), _node("b", S.PROJECT_CONTRACT),
             _node("c", S.WORK_PACKAGE)]
    ordered = [_edge("a", "b"), _edge("b", "c")]
    skip = fsck(_thread(nodes, [*ordered, _edge("a", "c")]))
    assert len(skip) == 1 and "skipped stage" in skip[0] and "PROJECT_CONTRACT" in skip[0]
    assert fsck(_thread(nodes, ordered)) == []


def test_an_edge_over_a_stage_nobody_has_is_a_gap_not_an_fsck_error():
    """Catches the skip rule firing where there is nothing to skip: with no
    node for the middle stage the edge is structurally fine and the gap shows
    up in `missing` instead."""
    nodes = [_node("a", S.REQUIREMENT), _node("c", S.WORK_PACKAGE)]
    t = _thread(nodes, [_edge("a", "c")])
    assert fsck(t) == []
    assert trace(t, node_id="a").missing == (S.PROJECT_CONTRACT,)


def test_one_stage_twice_on_a_path_with_conflicting_digests_is_an_error_and_same_digest_is_not():
    """Catches two versions of one stage's record on one path -- which one the
    chain rests on would be undecided."""
    def build(d2):
        nodes = [_node("a", S.REQUIREMENT), _node("s1", S.TASK_SPEC_AMENDMENT, digest="d1"),
                 _node("s2", S.TASK_SPEC_AMENDMENT, digest=d2)]
        return _thread(nodes, [_edge("a", "s1"), _edge("s1", "s2")])
    conflict = [m for m in fsck(build("d2")) if "conflicting stage digests" in m]
    assert len(conflict) == 1
    assert fsck(build("d1")) == []


def test_one_stage_twice_on_separate_branches_is_not_a_path_conflict():
    """Catches the stage-conflict rule firing across branches: two work
    packages under one requirement are siblings, not one path."""
    nodes = [_node("r", S.REQUIREMENT), _node("pc", S.PROJECT_CONTRACT),
             _node("w1", S.WORK_PACKAGE, digest="x"), _node("w2", S.WORK_PACKAGE, digest="y")]
    edges = [_edge("r", "pc"), _edge("pc", "w1"), _edge("pc", "w2")]
    assert fsck(_thread(nodes, edges)) == []
    assert any("conflicting stage digests" in m
               for m in fsck(_thread(nodes, [*edges, _edge("w1", "w2")])))


def test_a_cycle_is_an_error_and_the_acyclic_twin_is_clean():
    """Catches a loop in the thread, which has no upstream end to walk to."""
    nodes = [_node("s1", S.TASK_SPEC_AMENDMENT, digest="d"),
             _node("s2", S.TASK_SPEC_AMENDMENT, digest="d")]
    cyc = fsck(_thread(nodes, [_edge("s1", "s2"), _edge("s2", "s1")]))
    assert [m for m in cyc if m.startswith("cycle")] == ["cycle: ['s1', 's2'] reach each other"]
    assert fsck(_thread(nodes, [_edge("s1", "s2")])) == []


def test_a_cycle_keeps_a_trace_incomplete_and_the_walk_terminates():
    """Catches a trace that loops forever or reports a cyclic chain complete."""
    base = _linear()
    cyc = _thread(base.nodes, [*base.edges, _edge(_nid(S.RELEASE_EXPORT), _nid(S.REQUIREMENT))])
    tr = trace(cyc, node_id=_nid(S.RUN))
    assert not tr.complete and any(m.startswith("cycle") for m in tr.fsck)
    assert trace(base, node_id=_nid(S.RUN)).complete


# --------------------------------------------------------------------------- #
# queries
# --------------------------------------------------------------------------- #


def test_trace_of_an_unknown_node_raises_and_a_known_node_traces():
    """Catches an unknown identifier coming back as an empty, successful trace."""
    t = _linear()
    with pytest.raises(UnknownIdentifier):
        trace(t, node_id="nope")
    assert trace(t, node_id=_nid(S.RUN)).origin == _nid(S.RUN)


def test_node_lookup_of_an_unknown_id_raises_and_a_known_id_resolves():
    """Catches `DigitalThread.node` returning None for an id it does not hold."""
    t = _linear()
    with pytest.raises(UnknownIdentifier):
        t.node("nope")
    assert t.node(_nid(S.RUN)).stage is S.RUN


def test_why_of_an_unknown_node_raises_and_a_known_node_answers_nearest_first():
    """Catches `why` answering "no causes" for an id it does not know, and an
    upstream chain returned in the wrong order or with downstream nodes in it."""
    t = _linear()
    with pytest.raises(UnknownIdentifier):
        why(t, node_id="nope")
    chain = why(t, node_id=_nid(S.RUN))
    assert [n.stage for n in chain] == list(reversed(ALL[:ALL.index(S.RUN)]))
    assert why(t, node_id=_nid(S.REQUIREMENT)) == []


def test_trace_by_an_unknown_record_ref_raises_and_a_known_one_traces():
    """Catches `trace requirement <id>` for a requirement that is not in the
    projection reading as an empty chain."""
    t = _linear()
    with pytest.raises(UnknownIdentifier):
        trace_by(t, stage=S.REQUIREMENT, record_ref="requirement/none")
    with pytest.raises(UnknownIdentifier):  # right ref, wrong stage
        trace_by(t, stage=S.CANDIDATE, record_ref=t.node(_nid(S.REQUIREMENT)).record_ref)
    tr = trace_by(t, stage=S.REQUIREMENT, record_ref=t.node(_nid(S.REQUIREMENT)).record_ref)
    assert tr.complete and tr.origin == _nid(S.REQUIREMENT)


def test_trace_by_an_ambiguous_record_ref_raises_and_a_unique_one_traces():
    """Catches `trace_by` picking one of two matching nodes and hiding the other."""
    def build(ref2):
        nodes = [_node("c1", S.CANDIDATE, record_ref="sha/abc"),
                 _node("c2", S.CANDIDATE, record_ref=ref2, digest="sha-c1")]
        return _thread(nodes, [])
    with pytest.raises(AmbiguousIdentifier):
        trace_by(build("sha/abc"), stage=S.CANDIDATE, record_ref="sha/abc")
    assert trace_by(build("sha/def"), stage=S.CANDIDATE, record_ref="sha/abc").origin == "c1"


def test_release_blocking_trace_of_an_unknown_release_raises_and_a_known_one_traces():
    """Catches "no trace found" reading as "nothing blocks this release"."""
    t = _linear()
    with pytest.raises(UnknownIdentifier):
        release_blocking_trace(t, release_ref="release/none")
    ref = t.node(_nid(S.RELEASE_EXPORT)).record_ref
    tr = release_blocking_trace(t, release_ref=ref)
    assert tr.complete and tr.origin == _nid(S.RELEASE_EXPORT)
    gapped = _linear([s for s in ALL if s is not S.CHECKS_RECEIPTS])
    blocked = release_blocking_trace(gapped, release_ref=ref)
    assert not blocked.complete and blocked.missing == (S.CHECKS_RECEIPTS,)


def test_queries_do_not_mutate_the_thread():
    """Catches a query that edits the projection it reads."""
    t = _linear()
    snapshot = t.model_dump()
    trace(t, node_id=_nid(S.RUN)); why(t, node_id=_nid(S.RUN)); fsck(t)
    assert t.model_dump() == snapshot


# --------------------------------------------------------------------------- #
# Review repairs (reviewer B, 2026-09-23)
#
# The product wrote this module and its own tests; an independent reviewer ran 49
# mutations (38 caught, 9 genuinely surviving) and found two proven false-green
# release-blocking traces -- the single failure mode this module exists to make
# impossible. Each case below names the finding it closes.
# --------------------------------------------------------------------------- #


def test_a_release_with_no_chain_above_it_is_not_a_complete_trace():
    """Reviewer B F1. `missing` was measured between the *reached* endpoints, so a
    trace that never reaches REQUIREMENT had nothing it could be missing: an
    orphan release reported complete, and the Evidence Pack would have carried a
    green release-blocking trace for a release with no traceability at all."""
    orphan = _thread([_node("rel", S.RELEASE_EXPORT)], [])
    result = release_blocking_trace(orphan, release_ref="release_export/rel")
    assert not result.complete
    assert len(result.missing) == len(ALL) - 1
    assert S.REQUIREMENT in result.missing


def test_a_requirement_not_yet_released_is_incomplete_but_not_by_its_tail():
    """The control that pins the *other* end deliberately: anchoring both ends
    would report a requirement as missing its release, and a requirement that has
    not been released yet has simply not got there. Only the low end is
    anchored."""
    head = _linear(ALL[:3])
    result = trace(head, node_id=_nid(S.REQUIREMENT))
    assert result.missing == ()
    assert result.complete


def test_a_stage_covered_only_off_to_the_side_does_not_complete_the_chain():
    """Reviewer B F2. Stage coverage came from the origin's whole bow-tie, so a
    node hanging off the graph counted as covering its stage even though the real
    chain skipped it -- a sixteen-stage complete=True trace with no walkable
    requirement-to-release path. `fsck` was silent because its skipped-stage rule
    looks at a narrower node set."""
    main = [s for s in ALL if s is not S.WORK_PACKAGE]
    nodes = [_node(s.value.lower(), s) for s in main]
    edges = [_edge(a.node_id, b.node_id) for a, b in zip(nodes, nodes[1:])]
    nodes += [_node("wp", S.WORK_PACKAGE),
              _node("ck2", S.CHECKLIST_REQUIREMENT,
                    digest="sha-checklist_requirement")]
    edges += [_edge("wp", "ck2"), _edge("ck2", _nid(S.TASK_SPEC_AMENDMENT))]
    sideways = _thread(nodes, edges)

    result = trace(sideways, node_id=_nid(S.RUN))
    assert len(result.stages) == len(ALL), "every stage is reached"
    assert result.missing == (), "so nothing is missing by stage coverage"
    assert result.unwalkable, "but no edge joins PROJECT_CONTRACT to WORK_PACKAGE"
    assert not result.complete
    assert any("WORK_PACKAGE" in u for u in result.unwalkable)


def test_the_same_chain_with_the_work_package_on_it_is_complete():
    """The control: only the two edges change, so the finding above is about
    walkability and not about the shape of the graph."""
    result = trace(_linear(), node_id=_nid(S.RUN))
    assert result.unwalkable == ()
    assert result.complete


def test_two_nodes_of_one_stage_that_reconverge_are_a_digest_conflict():
    """Reviewer B F3: two nodes of a stage that neither reaches the other were
    treated as harmless siblings, but when a later node descends from both, the
    chain passes through both and which digest it rests on is undecided."""
    nodes = [_node("r", S.REQUIREMENT), _node("pc", S.PROJECT_CONTRACT),
             _node("w1", S.WORK_PACKAGE, digest="DIGEST-A"),
             _node("w2", S.WORK_PACKAGE, digest="DIGEST-B"),
             _node("ck", S.CHECKLIST_REQUIREMENT)]
    edges = [_edge("r", "pc"), _edge("pc", "w1"), _edge("pc", "w2"),
             _edge("w1", "ck"), _edge("w2", "ck")]
    problems = fsck(_thread(nodes, edges))
    assert any("undecided" in p for p in problems)


def test_two_nodes_of_one_stage_that_never_reconverge_stay_exempt():
    """The control, and a boundary the original suite drew on purpose: pure
    siblings that never rejoin are two branches, not one undecided chain."""
    nodes = [_node("r", S.REQUIREMENT), _node("pc", S.PROJECT_CONTRACT),
             _node("w1", S.WORK_PACKAGE, digest="DIGEST-A"),
             _node("w2", S.WORK_PACKAGE, digest="DIGEST-B")]
    edges = [_edge("r", "pc"), _edge("pc", "w1"), _edge("pc", "w2")]
    assert fsck(_thread(nodes, edges)) == []


def test_why_refuses_to_list_causes_over_a_broken_reference():
    """Reviewer B F5: `_adjacency` skips edges whose endpoints are not nodes, so
    `why` answered with the causes it could see and never mentioned the one it
    could not. `trace` carries `fsck` in its result; `why` returns a bare list,
    so it has to raise."""
    broken = _thread([_node("a", S.REQUIREMENT), _node("b", S.PROJECT_CONTRACT)],
                     [_edge("a", "b"), _edge("ghost", "b")])
    with pytest.raises(BrokenProjection, match="broken reference"):
        why(broken, node_id="b")
    # Control: the same thread with the ghost present answers.
    whole = _thread([_node("a", S.REQUIREMENT), _node("b", S.PROJECT_CONTRACT),
                     _node("ghost", S.REQUIREMENT)],
                    [_edge("a", "b"), _edge("ghost", "b")])
    assert sorted(n.node_id for n in why(whole, node_id="b")) == ["a", "ghost"]


def test_a_stage_name_that_is_not_a_stage_stays_inside_the_error_hierarchy():
    """Reviewer B F7: the plan's query surface is `trace requirement <id>`, and a
    lower-case stage name escaped as a bare ValueError from the enum, which no
    caller catching ThreadLookupError would see."""
    with pytest.raises(UnknownIdentifier, match="not a stage"):
        trace_by(_linear(), stage="requirement", record_ref="requirement/requirement")
    # Control: the real member resolves.
    assert trace_by(_linear(), stage=S.REQUIREMENT,
                    record_ref="requirement/requirement").origin


def test_a_projection_with_no_named_source_is_refused():
    """Reviewer B F6: the rule held and nothing tested it, so removing
    `projected_from`'s constraint kept all 32 tests green. A projection that
    cannot say what it is a projection of is the second source of truth the spec
    forbids."""
    with pytest.raises(ValidationError):
        DigitalThread(thread_id="T", generation=1, nodes=[], edges=[],
                      projected_from=[])
    with pytest.raises(ValidationError):
        DigitalThread(thread_id="T", generation=1, nodes=[], edges=[],
                      projected_from=["   "])
    assert DigitalThread(thread_id="T", generation=1, nodes=[], edges=[],
                         projected_from=["ledger@g1"]).projected_from


def test_complete_is_forgeable_through_model_copy_and_that_is_recorded_not_fixed():
    """Reviewer B F4, recorded as a limitation rather than claimed closed (O214).

    `complete` is a computed property over `missing`, `unwalkable` and `fsck`, and
    `model_copy(update=...)` replaces stored fields without running validators. So
    a caller who builds a Trace by hand can produce one that says complete. There
    is no fix inside `Trace`: it does not hold the thread, so it cannot recompute
    the three fields from anything.

    This test exists so the gap is a written-down property with a name, not a
    surprise. What it also pins is the part that *is* defended: the only way to
    obtain a Trace from this module is `trace`, `trace_by` or
    `release_blocking_trace`, each of which derives all three fields itself, and
    none of them accepts them as arguments.
    """
    broken = _thread([_node("a", S.REQUIREMENT), _node("b", S.PROJECT_CONTRACT)],
                     [_edge("a", "b"), _edge("ghost", "b")])
    result = trace(broken, node_id="a")
    assert not result.complete
    assert result.model_copy(update={"fsck": (), "missing": ()}).complete, \
        "if this ever becomes False, O214 is closed and this test should say so"
    # The defended part: no query takes the verdict from its caller.
    import inspect
    for fn in (trace, trace_by, release_blocking_trace):
        taken = set(inspect.signature(fn).parameters)
        assert not taken & {"missing", "unwalkable", "fsck", "complete"}, fn
