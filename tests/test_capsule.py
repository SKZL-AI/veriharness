"""The context capsule and its layered assembly (V3.3 P2-20, with P2-13).

A capsule is a projection: derived from records handed in, bound to their exact
digests, scoped to one role, frozen, and refused for dispatch once anything it
was built from has moved. Each case below is one rule of the spec, and each
sits next to a negative control that changes only the condition under test and
shows the verdict flipping -- a freshness check that refuses everything, or a
budget rule that holds because nothing was ever kept, would pass the positive
case alone.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from hoh.capsule import (
    CapsuleBudgetError,
    CapsuleItem,
    ContextCapsule,
    DropReason,
    Layer,
    Role,
    SourceRef,
    assemble,
    may_dispatch,
    stale_sources,
    view_for,
)

ALL = tuple(Role)
T0 = datetime(2026, 9, 24, tzinfo=timezone.utc)


def _ref(source_id="s0", *, digest="d0", generation=1, kind="decision") -> SourceRef:
    return SourceRef(source_id=source_id, kind=kind, digest=digest, generation=generation)


def _item(item_id, layer, *, source=None, cost=1, roles=ALL, gist="what it says",
          title=None) -> CapsuleItem:
    return CapsuleItem(item_id=item_id, layer=layer, title=title or f"title of {item_id}",
                       gist=gist, source=source or _ref(f"src-{item_id}"), cost=cost,
                       required_for_roles=list(roles))


def _chain():
    """One compact map, one relevant decision, one full source -- all
    required for every role, each on its own source."""
    return [
        _item("c", Layer.COMPACT, source=_ref("s0"), cost=2),
        _item("r", Layer.RELEVANT, source=_ref("s1"), cost=5),
        _item("o", Layer.ON_DEMAND, source=_ref("s2"), cost=5),
    ]


def _build(items=None, *, budget=100, role=Role.PLANNER, generation=3, policy="v1"):
    return assemble(_chain() if items is None else items, program_id="p",
                    generation=generation, role=role, token_budget=budget,
                    policy_version=policy, assembled_at=T0)


CURRENT = {"s0": ("d0", 1), "s1": ("d0", 1), "s2": ("d0", 1)}


def _ids(items):
    return [i.item_id for i in items]


# --------------------------------------------------------------------------- #
# the model
# --------------------------------------------------------------------------- #


def test_the_enums_have_exactly_the_specified_members_in_order():
    """Layer is the plan's 0/1/2 and Role its four views; a member added or
    reordered silently changes what a serialised capsule means."""
    assert [m.value for m in Layer] == ["COMPACT", "RELEVANT", "ON_DEMAND"]
    assert [m.value for m in Role] == ["PLANNER", "DEVELOPER", "QA", "ARCHIVIST"]


@pytest.mark.parametrize("field", ["digest", "source_id"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_a_source_ref_without_digest_or_source_id_is_refused(field, blank):
    """An item that cannot be traced to an exact source is the summary-
    without-provenance the capsule exists to replace."""
    fields = dict(source_id="s0", kind="decision", digest="d0", generation=1)
    fields[field] = blank
    with pytest.raises(ValidationError):
        SourceRef(**fields)


def test_control_a_source_ref_with_digest_and_source_id_is_accepted():
    """Control: the refusal above is about the blank field, not about
    SourceRef refusing everything."""
    ref = SourceRef(source_id="s0", kind="decision", digest="d0", generation=1)
    assert (ref.source_id, ref.digest) == ("s0", "d0")


def test_a_capsule_item_with_an_empty_gist_is_refused():
    """An item nobody can read is budget spent on nothing."""
    with pytest.raises(ValidationError):
        _item("x", Layer.RELEVANT, gist="")
    with pytest.raises(ValidationError):
        _item("x", Layer.RELEVANT, gist="  ")


def test_control_a_capsule_item_with_a_gist_is_accepted():
    """Control: only the gist changes, and the item is built."""
    assert _item("x", Layer.RELEVANT, gist="decision D7 holds").gist == "decision D7 holds"


def test_duplicate_item_ids_are_refused_by_assemble_and_by_the_capsule():
    """Two items under one id make `dropped` and `items` ambiguous: which of
    them was kept is no longer something a reader can tell."""
    items = _chain() + [_item("r", Layer.RELEVANT, source=_ref("s9"))]
    with pytest.raises(ValueError, match="duplicate"):
        _build(items)
    good = _build()
    with pytest.raises(ValidationError, match="duplicate"):
        ContextCapsule(**dict(good.model_dump(), items=good.items + (good.items[1],)))


def test_control_distinct_item_ids_assemble():
    """Control: the same list without the duplicate assembles."""
    items = _chain() + [_item("r2", Layer.RELEVANT, source=_ref("s9"))]
    assert _ids(_build(items).items) == ["c", "r", "o", "r2"]


def test_the_capsule_is_frozen():
    """A capsule is a projection of Goalbook, Decision Store, Program State and
    Evidence Store. One that could be edited after assembly would become a
    second source of truth, drifting from the sources its digest still names --
    so every attempt to set an attribute raises."""
    cap = _build()
    for field, value in [("role", Role.QA), ("spent", 0), ("capsule_digest", "x"),
                         ("items", ())]:
        with pytest.raises(ValidationError):
            setattr(cap, field, value)
    assert cap.role is Role.PLANNER


def test_a_capsule_whose_binding_disagrees_with_its_items_is_refused():
    """A hand-built capsule that names sources its items do not carry, or a
    digest that is not the one those sources give, would pass the freshness
    check against the wrong sources."""
    good = _build()
    with pytest.raises(ValidationError):
        ContextCapsule(**dict(good.model_dump(), source_digests=("x",) * 3))
    with pytest.raises(ValidationError):
        ContextCapsule(**dict(good.model_dump(), capsule_digest="sha256:0"))
    assert ContextCapsule(**good.model_dump()) == good          # control


# --------------------------------------------------------------------------- #
# freshness: stale sources and dispatch
# --------------------------------------------------------------------------- #


def test_stale_a_changed_source_digest_makes_the_capsule_undispatchable():
    """Reusing a brief after one of its sources changed hands an agent the
    old decision as if it were the current one."""
    cap = _build()
    current = dict(CURRENT, s1=("d-new", 1))
    assert stale_sources(cap, current=current) == ["s1"]
    reasons = may_dispatch(cap, current=current, program_generation=3)
    assert len(reasons) == 1 and "s1" in reasons[0]


def test_stale_control_unchanged_sources_are_dispatchable():
    """Control: same capsule, same generation, every source unchanged -- the
    refusal above is caused by the digest, not by may_dispatch refusing all."""
    cap = _build()
    assert stale_sources(cap, current=CURRENT) == []
    assert may_dispatch(cap, current=CURRENT, program_generation=3) == []


def test_stale_a_source_generation_bump_alone_makes_it_stale():
    """A source that moved generation with an equal digest has still moved;
    checking the digest only would miss it."""
    cap = _build()
    assert stale_sources(cap, current=dict(CURRENT, s2=("d0", 2))) == ["s2"]
    assert may_dispatch(cap, current=dict(CURRENT, s2=("d0", 2)), program_generation=3)


def test_missing_a_source_removed_from_current_is_stale():
    """Treating absence as agreement is the specific mistake this project has
    made before: a source that disappeared is not an unchanged source."""
    cap = _build()
    current = {k: v for k, v in CURRENT.items() if k != "s2"}
    assert stale_sources(cap, current=current) == ["s2"]
    reasons = may_dispatch(cap, current=current, program_generation=3)
    assert len(reasons) == 1 and "s2" in reasons[0] and "missing" in reasons[0]


def test_missing_control_the_same_source_present_is_not_stale():
    """Control: put s2 back unchanged and the capsule is dispatchable again."""
    cap = _build()
    current = {k: v for k, v in CURRENT.items() if k != "s2"}
    current["s2"] = ("d0", 1)
    assert stale_sources(cap, current=current) == []
    assert may_dispatch(cap, current=current, program_generation=3) == []


def test_stale_a_program_generation_bump_refuses_dispatch_even_when_every_digest_matches():
    """The programme moving on is a reason to rebuild even if no single
    bound source did; a digest-only check would dispatch a stale brief."""
    cap = _build(generation=3)
    assert stale_sources(cap, current=CURRENT) == []
    reasons = may_dispatch(cap, current=CURRENT, program_generation=4)
    assert len(reasons) == 1 and "generation" in reasons[0]


def test_stale_control_matching_program_generation_dispatches():
    """Control: only the programme generation differs from the case above."""
    assert may_dispatch(_build(generation=3), current=CURRENT, program_generation=3) == []


def test_stale_only_bound_sources_count_and_extra_current_sources_do_not():
    """Sources the capsule never included cannot make it stale; a check that
    flagged every unknown id would refuse every capsule in a live programme."""
    cap = _build(budget=7)                         # o (s2) dropped for budget
    assert cap.source_ids == ("s0", "s1")
    current = {"s0": ("d0", 1), "s1": ("d0", 1), "s2": ("moved", 9), "zz": ("x", 1)}
    assert stale_sources(cap, current=current) == []
    assert stale_sources(cap, current=dict(current, s1=("moved", 1))) == ["s1"]


# --------------------------------------------------------------------------- #
# assembly: budget and drops
# --------------------------------------------------------------------------- #


def test_the_compact_layer_survives_a_squeeze_that_drops_everything_else():
    """Layer 0 is the low-resolution map; a capsule without it is a capsule
    about nothing, so budget pressure must never take it."""
    cap = _build(budget=2)
    assert _ids(cap.items) == ["c"] and cap.spent == 2
    assert sorted(d.item_id for d in cap.dropped) == ["o", "r"]


def test_a_budget_too_small_for_the_compact_layer_raises_instead_of_returning():
    """Silently returning a capsule without layer 0 is the failure; raising is
    the only honest answer."""
    with pytest.raises(CapsuleBudgetError):
        _build(budget=1)


def test_control_a_budget_exactly_the_compact_cost_returns():
    """Control: one more unit of budget and the same items assemble."""
    assert _build(budget=2).spent == 2


def test_items_without_a_compact_layer_are_refused():
    """With no layer 0 at all there is nothing to squeeze down to."""
    with pytest.raises(ValueError, match="COMPACT"):
        _build(_chain()[1:])


def test_spent_stays_within_budget_and_every_omission_is_named_over_a_range_of_budgets():
    """A capsule that quietly lost context is "too little context" arrived at
    by accident: every item is either kept or named in `dropped` with a
    reason, and what is kept fits."""
    items = _chain() + [_item("r2", Layer.RELEVANT, source=_ref("s3"), cost=3,
                              roles=[Role.PLANNER, Role.QA])]
    total = sum(i.cost for i in items)
    for budget in range(2, total + 3):
        for role in Role:
            cap = _build(items, budget=budget, role=role)
            assert cap.spent == sum(i.cost for i in cap.items) <= budget
            assert sorted(_ids(cap.items) + [d.item_id for d in cap.dropped]) == \
                sorted(_ids(items))
            assert all(d.reason for d in cap.dropped)


def test_control_an_ample_budget_drops_nothing_for_the_planner():
    """Control: with room for everything, `dropped` is empty -- the drops
    above are caused by the budget, not by the assembler dropping by habit."""
    cap = _build(budget=12)
    assert _ids(cap.items) == ["c", "r", "o"] and cap.dropped == ()


def test_on_demand_is_dropped_before_relevant():
    """Full source is available on demand; the relevant decision is not
    reconstructible from the map alone, so it goes last."""
    cap = _build(budget=7)
    assert _ids(cap.items) == ["c", "r"]
    assert [(d.item_id, d.reason) for d in cap.dropped] == [
        ("o", DropReason.OVER_BUDGET_REQUIRED)]


def test_control_swapping_the_layers_swaps_which_one_is_dropped():
    """Control: the same costs with the layers exchanged -- now r is the
    on-demand item and it is the one dropped."""
    items = [_item("c", Layer.COMPACT, source=_ref("s0"), cost=2),
             _item("r", Layer.ON_DEMAND, source=_ref("s1"), cost=5),
             _item("o", Layer.RELEVANT, source=_ref("s2"), cost=5)]
    cap = _build(items, budget=7)
    assert _ids(cap.items) == ["c", "o"] and [d.item_id for d in cap.dropped] == ["r"]


def test_items_not_required_for_the_role_are_dropped_before_required_ones():
    """The planner sees optional context too; under pressure the context it
    needs must outlive the context it could do without -- even when the
    optional item is a RELEVANT and the required one ON_DEMAND."""
    items = [_item("c", Layer.COMPACT, source=_ref("s0"), cost=2),
             _item("opt", Layer.RELEVANT, source=_ref("s1"), cost=5, roles=[Role.QA]),
             _item("req", Layer.ON_DEMAND, source=_ref("s2"), cost=5, roles=[Role.PLANNER])]
    cap = _build(items, budget=7, role=Role.PLANNER)
    assert _ids(cap.items) == ["c", "req"]
    assert [(d.item_id, d.reason) for d in cap.dropped] == [
        ("opt", DropReason.OVER_BUDGET_OPTIONAL)]


def test_control_making_both_required_restores_the_layer_order():
    """Control: only the requirement of `opt` changes, and ON_DEMAND is
    dropped first again."""
    items = [_item("c", Layer.COMPACT, source=_ref("s0"), cost=2),
             _item("opt", Layer.RELEVANT, source=_ref("s1"), cost=5, roles=[Role.PLANNER]),
             _item("req", Layer.ON_DEMAND, source=_ref("s2"), cost=5, roles=[Role.PLANNER])]
    cap = _build(items, budget=7, role=Role.PLANNER)
    assert _ids(cap.items) == ["c", "opt"] and [d.item_id for d in cap.dropped] == ["req"]


def test_items_outside_the_role_view_are_dropped_with_a_reason_even_with_ample_budget():
    """Role scoping is not a budget effect: QA does not get the planner's
    optional context however much room there is, and says so in `dropped`."""
    items = _chain() + [_item("plan", Layer.RELEVANT, source=_ref("s3"), roles=[Role.PLANNER])]
    cap = _build(items, budget=1000, role=Role.QA)
    assert "plan" not in _ids(cap.items)
    assert [(d.item_id, d.reason) for d in cap.dropped] == [
        ("plan", DropReason.NOT_IN_ROLE_VIEW)]
    assert "s3" not in cap.source_ids
    assert "plan" in _ids(_build(items, budget=1000, role=Role.PLANNER).items)   # control


# --------------------------------------------------------------------------- #
# role views
# --------------------------------------------------------------------------- #


def _mixed():
    return [
        _item("map", Layer.COMPACT, source=_ref("g")),
        _item("crit", Layer.RELEVANT, source=_ref("k1", kind="criterion"),
              roles=[Role.QA, Role.PLANNER]),
        _item("why", Layer.RELEVANT, source=_ref("d1"), roles=[Role.PLANNER]),
        _item("code", Layer.ON_DEMAND, source=_ref("f1", kind="source_file"),
              roles=[Role.DEVELOPER]),
        _item("event", Layer.RELEVANT, source=_ref("e1", kind="event"),
              roles=[Role.ARCHIVIST]),
    ]


def test_the_qa_view_is_strictly_narrower_than_the_planner_view_over_the_same_items():
    """QA gets the criterion and the minimal decision context to judge; a QA
    view equal to the planner's would be the same list with a different label."""
    qa, planner = _ids(view_for(Role.QA, _mixed())), _ids(view_for(Role.PLANNER, _mixed()))
    assert qa == ["map", "crit"]
    assert set(qa) < set(planner)
    assert planner == ["map", "crit", "why", "code", "event"]


def test_control_marking_every_item_required_for_qa_widens_the_qa_view():
    """Control: the narrowing is caused by `required_for_roles`, not by QA
    never seeing anything beyond the compact layer."""
    items = [i.model_copy(update={"required_for_roles": frozenset(Role)}) for i in _mixed()]
    assert _ids(view_for(Role.QA, items)) == _ids(view_for(Role.PLANNER, items))


def test_the_archivist_gets_events_and_source_refs_not_the_codebase():
    """The archivist records what happened; handing it the codebase spends its
    budget on what it must not act on -- even when an item names it."""
    items = _mixed() + [_item("code2", Layer.RELEVANT, source=_ref("f2", kind="code"),
                              roles=[Role.ARCHIVIST])]
    assert _ids(view_for(Role.ARCHIVIST, items)) == ["map", "event"]
    assert "code" in _ids(view_for(Role.DEVELOPER, items))                   # control


def test_every_role_sees_the_compact_layer():
    """Layer 0 is common ground; a role without it would be briefed on details
    with no map to place them on."""
    for role in Role:
        assert "map" in _ids(view_for(role, _mixed()))


# --------------------------------------------------------------------------- #
# the digest
# --------------------------------------------------------------------------- #


def test_two_capsules_from_identical_sources_and_policy_have_equal_digests():
    """The digest names what the capsule was built from; two builds from the
    same sources under the same policy must agree, whatever the prose or the
    clock says."""
    a = _build()
    reworded = [i.model_copy(update={"gist": "said differently", "title": "other"})
                for i in _chain()]
    b = assemble(reversed(reworded), program_id="p", generation=3, role=Role.PLANNER,
                 token_budget=100, policy_version="v1",
                 assembled_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert a.capsule_digest == b.capsule_digest


@pytest.mark.parametrize("change", ["source_digest", "role", "generation", "policy"])
def test_control_changing_one_input_changes_the_digest(change):
    """Control per input: a digest insensitive to any of these would let a
    moved source or a different role's brief pass as the same capsule."""
    base = _build()
    if change == "source_digest":
        items = _chain()
        items[1] = items[1].model_copy(update={"source": _ref("s1", digest="d-new")})
        other = _build(items)
    elif change == "role":
        other = _build(role=Role.DEVELOPER)
    elif change == "generation":
        other = _build(generation=4)
    else:
        other = _build(policy="v2")
    assert other.capsule_digest != base.capsule_digest
