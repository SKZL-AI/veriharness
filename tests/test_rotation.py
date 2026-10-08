"""Context rotation (V3.3 P2-14): rotation may reduce what is carried, never
what is reachable, and never silently.

One case per rule of `rotate`, plus the record, `reachable` and the cost
functions. Each positive case sits next to a negative control that changes only
the condition under test and shows the verdict flipping -- a pinned item that
"survives" a squeeze that would never have touched it proves nothing, and a
`reachable` that returns `[]` for every resolver is a check of nothing.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from hoh.contracts import Strict
from hoh.rotation import (
    ContextItem,
    RotationRecord,
    Tier,
    carried_cost,
    content_digest,
    reachable,
    rotate,
    rotation_pressure,
)

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def _content(item_id: str) -> str:
    return f"full source of {item_id}"


def _item(item_id, *, tier=Tier.CARRIED, age_days=1, cost=10, epoch=1) -> ContextItem:
    return ContextItem(
        item_id=item_id, tier=tier, title=f"title of {item_id}", gist=f"gist of {item_id}",
        source_ref=f"store/{item_id}", digest=content_digest(_content(item_id)),
        cost=cost, last_touched_at=NOW - timedelta(days=age_days), epoch=epoch)


def _store(items) -> dict[str, str]:
    return {i.source_ref: _content(i.item_id) for i in items}


def _rotate(items, budget, **kw):
    kw.setdefault("reason", "brief over budget")
    return rotate(items, budget=budget, now=NOW, **kw)


def _tiers(items) -> dict[str, Tier]:
    return {i.item_id: i.tier for i in items}


# --------------------------------------------------------------------------- #
# the model
# --------------------------------------------------------------------------- #


def test_the_tier_enum_has_exactly_the_specified_members_in_order():
    """A member added or reordered silently changes what a serialised item means."""
    assert [t.value for t in Tier] == ["CARRIED", "ROTATED", "PINNED"]


def test_an_item_with_an_empty_source_ref_or_digest_is_refused():
    """An item with no pointer back to Layer 2 is one rotation would destroy."""
    good = _item("a").model_dump()
    for field in ("source_ref", "digest"):
        for blank in ("", "   "):
            with pytest.raises(ValidationError):
                ContextItem(**{**good, field: blank})
    # Control: the same fields with the pointer intact construct.
    ContextItem(**good)


def test_an_unknown_field_on_an_item_is_refused():
    """On the Strict base, a misspelt `pinned=True` would otherwise be dropped silently."""
    assert issubclass(ContextItem, Strict) and issubclass(RotationRecord, Strict)
    with pytest.raises(ValidationError):
        ContextItem(**_item("a").model_dump(), pinned=True)
    ContextItem(**_item("a").model_dump())


def test_a_negative_cost_is_refused():
    """A negative cost would let an item *add* budget and hide an overrun."""
    with pytest.raises(ValidationError):
        ContextItem(**{**_item("a").model_dump(), "cost": -1})
    ContextItem(**{**_item("a").model_dump(), "cost": 0})


def test_the_models_are_frozen_because_a_mutable_rotation_record_would_be_worthless():
    """A record whose item_ids or reason can be edited after the fact certifies
    nothing: its completeness and its digest could be rewritten by whoever
    reads it next, and the audit trail of what left the brief would be
    whatever the last writer wanted. Items are frozen for the same reason --
    an item whose digest can be swapped is a pointer that can be forged."""
    items = [_item("a"), _item("b")]
    _, record = _rotate(items, 10)
    with pytest.raises(ValidationError):
        record.reason = "nothing happened"
    with pytest.raises(ValidationError):
        record.item_ids = ()
    with pytest.raises(ValidationError):
        items[0].tier = Tier.PINNED
    # Control: the unchanged record rebuilds from its own dump.
    assert RotationRecord(**record.model_dump()) == record


def test_a_record_whose_digest_does_not_match_its_content_is_refused():
    """Editing a dumped record and rebuilding it is the mutable record by another route."""
    _, record = _rotate([_item("a"), _item("b")], 10)
    dumped = record.model_dump()
    with pytest.raises(ValidationError):
        RotationRecord(**{**dumped, "reason": "edited afterwards"})
    RotationRecord(**dumped)


# --------------------------------------------------------------------------- #
# Rule 1 -- PINNED never rotates
# --------------------------------------------------------------------------- #


def test_a_pinned_item_stays_through_a_squeeze_that_rotates_everything_else():
    """Catches a rotation that drops an open blocker because it was the coldest."""
    items = [_item("a", age_days=5), _item("b", age_days=3),
             _item("p", tier=Tier.PINNED, age_days=99)]
    out, record = _rotate(items, 10)
    assert _tiers(out) == {"a": Tier.ROTATED, "b": Tier.ROTATED, "p": Tier.PINNED}
    assert "p" not in record.item_ids
    # Control: the same item CARRIED instead of PINNED is the coldest and goes first.
    items[2] = _item("p", tier=Tier.CARRIED, age_days=99)
    out, record = _rotate(items, 20)
    assert record.item_ids[0] == "p" and _tiers(out)["p"] is Tier.ROTATED


def test_pinned_items_alone_over_the_budget_raise():
    """Catches a budget that cannot hold what must be held being resolved by dropping it."""
    items = [_item("p", tier=Tier.PINNED, cost=15), _item("a")]
    with pytest.raises(ValueError, match="pinned"):
        _rotate(items, 10)
    # Control: a budget that holds the pinned cost rotates the rest instead.
    out, record = _rotate(items, 15)
    assert record.item_ids == ("a",) and _tiers(out)["p"] is Tier.PINNED


# --------------------------------------------------------------------------- #
# Rule 2 -- coldest first, deterministic
# --------------------------------------------------------------------------- #


def test_rotation_is_coldest_first():
    """Catches a rotation that moves the item somebody touched an hour ago."""
    items = [_item("warm", age_days=1), _item("cold", age_days=9), _item("mid", age_days=4)]
    _, record = _rotate(items, 20)
    assert record.item_ids == ("cold",)
    # Control: swapping only the ages of warm and cold swaps which one rotates.
    items = [_item("warm", age_days=9), _item("cold", age_days=1), _item("mid", age_days=4)]
    _, record = _rotate(items, 20)
    assert record.item_ids == ("warm",)


def test_the_same_items_in_any_input_order_rotate_the_same_set():
    """Catches an order that depends on input or dict iteration -- a different
    brief on every run from the same state."""
    items = [_item(f"i{n}", age_days=n % 3) for n in range(8)]  # many timestamp ties
    _, reference = _rotate(items, 40)
    for seed in range(20):
        shuffled = items[:]
        random.Random(seed).shuffle(shuffled)
        _, record = _rotate(shuffled, 40)
        assert record == reference
    # Tie-break by item_id: among equally cold items the lower id goes first.
    assert reference.item_ids == ("i2", "i5", "i1", "i4")
    # Control: a different budget does change the set, so equality above is not vacuous.
    _, other = _rotate(items, 60)
    assert other.item_ids != reference.item_ids


# --------------------------------------------------------------------------- #
# Rule 3 -- every rotated item is in the record
# --------------------------------------------------------------------------- #


def test_the_record_source_refs_recover_every_rotated_item():
    """Catches a rotation that moves an item to Layer 2 without leaving a pointer."""
    items = [_item("a", age_days=5), _item("b", age_days=3), _item("c", age_days=1)]
    out, record = _rotate(items, 10)
    rotated = [i for i in out if i.tier is Tier.ROTATED]
    assert {i.item_id for i in rotated} == set(record.item_ids) == {"a", "b"}
    assert len(record.item_ids) == len(record.source_refs) == len(record.digests)
    by_ref = {i.source_ref: i for i in items}
    for item_id, ref, digest in zip(record.item_ids, record.source_refs, record.digests):
        assert by_ref[ref].item_id == item_id and by_ref[ref].digest == digest
    store = _store(items)
    assert all(content_digest(store[r]) == d for r, d in zip(record.source_refs, record.digests))
    assert record.freed == 20
    # Control: the item left carried is not in the record.
    assert "c" not in record.item_ids and "store/c" not in record.source_refs


def test_a_record_with_mismatched_list_lengths_is_refused():
    """A record naming three items and two sources has lost one; refused at
    construction rather than discovered later by someone reading a brief."""
    _, record = _rotate([_item("a"), _item("b"), _item("c")], 10)
    dumped = record.model_dump()
    for field in ("item_ids", "source_refs", "digests"):
        with pytest.raises(ValidationError, match="same length"):
            RotationRecord(**{**dumped, field: dumped[field][:-1]})
    # Control: the equal-length record constructs.
    assert RotationRecord(**dumped) == record


def test_an_empty_record_or_blank_reason_is_refused():
    """Nothing rotated is None, not an empty record; and no rotation is silent."""
    _, record = _rotate([_item("a"), _item("b")], 10)
    dumped = record.model_dump()
    with pytest.raises(ValidationError):
        RotationRecord(**{**dumped, "item_ids": (), "source_refs": (), "digests": ()})
    with pytest.raises(ValueError, match="silent"):
        _rotate([_item("a"), _item("b")], 10, reason="  ")
    RotationRecord(**dumped)


def test_a_rotation_record_is_addressable_and_deterministic():
    """Catches a record id that changes between runs of the same rotation, so
    two briefs could not agree on which record they point to."""
    items = [_item("a", age_days=2), _item("b")]
    _, r1 = _rotate(items, 10)
    _, r2 = _rotate(list(reversed(items)), 10)
    assert r1.record_id == r2.record_id and r1.record_id.startswith("rot-")
    # Control: a different reason is a different record.
    _, r3 = _rotate(items, 10, reason="another reason")
    assert r3.record_id != r1.record_id


# --------------------------------------------------------------------------- #
# Rule 4 -- idempotent
# --------------------------------------------------------------------------- #


def test_rotating_the_result_again_returns_none_and_moves_nothing():
    """Catches a rotation that keeps eating the brief on every supervisor tick."""
    items = [_item("a", age_days=5), _item("b", age_days=3), _item("c", age_days=1)]
    out, record = _rotate(items, 10)
    assert record is not None
    again, second = _rotate(out, 10)
    assert second is None and again == out
    # Control: a tighter budget over the same result does move something.
    _, tighter = _rotate(out, 5)
    assert tighter is not None and tighter.item_ids == ("c",)


def test_rotate_does_not_mutate_its_input():
    """Catches a rotation that flips tiers on the caller's list in place."""
    items = [_item("a", age_days=5), _item("b")]
    before = [i.model_copy() for i in items]
    out, _ = _rotate(items, 10)
    assert items == before and out != items


# --------------------------------------------------------------------------- #
# Rule 5 -- keep_touched_within
# --------------------------------------------------------------------------- #


def test_keep_touched_within_protects_a_recent_item_when_the_budget_allows():
    """Catches the parameter being silently ignored."""
    items = [_item("old", age_days=10), _item("recent", age_days=0, epoch=1),
             _item("mid", age_days=5)]
    # Same epoch; without protection the coldest two go: old, mid.
    _, plain = _rotate(items, 10)
    assert plain.item_ids == ("old", "mid")
    # Make "recent" the coldest by epoch so it would go first unprotected.
    items[1] = _item("recent", age_days=0, epoch=0)
    _, unprotected = _rotate(items, 20)
    assert unprotected.item_ids == ("recent",)
    # Positive: protected, the next coldest goes instead, and no override is claimed.
    _, protected = _rotate(items, 20, keep_touched_within=timedelta(days=2))
    assert protected.item_ids == ("old",)
    assert "overrid" not in protected.reason.lower()


def test_the_record_says_protection_was_overridden_when_the_budget_needs_it():
    """Catches a budget silently failed, or protection silently ignored."""
    items = [_item("old", age_days=10), _item("recent1", age_days=1),
             _item("recent2", age_days=0)]
    window = timedelta(days=2)
    out, record = _rotate(items, 10, keep_touched_within=window)
    assert record.item_ids == ("old", "recent1")
    assert "overridden" in record.reason.lower() and "recent1" in record.reason
    assert carried_cost(out) <= 10
    # Control: a budget reachable without touching protected items says nothing of an override.
    _, record = _rotate(items, 20, keep_touched_within=window)
    assert record.item_ids == ("old",) and "overrid" not in record.reason.lower()


# --------------------------------------------------------------------------- #
# Rule 6 -- older epoch first
# --------------------------------------------------------------------------- #


def test_an_older_epoch_item_rotates_before_a_colder_current_epoch_one():
    """Catches rotation by timestamp alone, which keeps a previous epoch's context
    in the brief because somebody glanced at it yesterday."""
    items = [_item("cold_current", age_days=30, epoch=2), _item("warm_old", age_days=1, epoch=1),
             _item("mid", age_days=5, epoch=2)]
    _, record = _rotate(items, 20)
    assert record.item_ids == ("warm_old",) and record.epoch == 2
    # Control: the same item in the current epoch loses its precedence; the cold one goes.
    items[1] = _item("warm_old", age_days=1, epoch=2)
    _, record = _rotate(items, 20)
    assert record.item_ids == ("cold_current",)


# --------------------------------------------------------------------------- #
# Rule 7 -- budget <= 0
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("budget", [0, -1])
def test_a_budget_of_zero_or_less_raises(budget):
    """Catches a zero budget read as 'rotate everything' instead of a caller error."""
    items = [_item("a")]
    with pytest.raises(ValueError, match="budget"):
        _rotate(items, budget)
    with pytest.raises(ValueError, match="budget"):
        rotation_pressure(items, budget=budget)
    # Control: budget 1 is accepted.
    _rotate(items, 1)
    assert rotation_pressure(items, budget=1) == 10.0


def test_duplicate_item_ids_and_naive_times_are_refused():
    """Duplicate ids make the record ambiguous about which item moved; a naive
    `now` cannot be ordered against aware timestamps."""
    with pytest.raises(ValueError, match="duplicate"):
        _rotate([_item("a"), _item("a", age_days=3)], 5)
    with pytest.raises(ValueError, match="timezone"):
        rotate([_item("a"), _item("b")], budget=10, now=NOW.replace(tzinfo=None), reason="r")
    _rotate([_item("a"), _item("b", age_days=3)], 5)


# --------------------------------------------------------------------------- #
# reachable
# --------------------------------------------------------------------------- #


def _rotated_three():
    items = [_item("a", age_days=5), _item("b", age_days=3), _item("c", age_days=1),
             _item("p", tier=Tier.PINNED)]
    _, record = _rotate(items, 10)
    return items, record


def test_reachable_is_empty_for_an_honest_resolver():
    """Catches a check that always complains -- it would be ignored."""
    items, record = _rotated_three()
    assert reachable(record, resolve=_store(items).get) == []


def test_reachable_names_a_ref_the_resolver_cannot_find():
    """Catches Layer 2 losing an item after rotation without anyone noticing."""
    items, record = _rotated_three()
    store = _store(items)
    del store["store/b"]
    assert reachable(record, resolve=store.get) == ["store/b"]

    def raising(ref):
        if ref == "store/c":
            raise KeyError(ref)
        return _store(items)[ref]
    assert reachable(record, resolve=raising) == ["store/c"]
    # Control: the full store resolves everything.
    assert reachable(record, resolve=_store(items).get) == []


def test_reachable_names_a_ref_whose_content_changed_underneath():
    """A stale pointer is not a working one: the ref resolves, but not to what was rotated."""
    items, record = _rotated_three()
    store = _store(items)
    store["store/a"] = "rewritten after rotation"
    assert reachable(record, resolve=store.get) == ["store/a"]
    # Control: the same content as bytes digests the same and passes.
    store["store/a"] = _content("a").encode("utf-8")
    assert reachable(record, resolve=store.get) == []


# --------------------------------------------------------------------------- #
# carried_cost / rotation_pressure
# --------------------------------------------------------------------------- #


def test_carried_cost_ignores_rotated_items_and_counts_pinned_ones():
    """Catches a supervisor rotating forever because rotated items still count,
    or overfilling the brief because pinned ones do not."""
    items = [_item("c", cost=3), _item("p", tier=Tier.PINNED, cost=5),
             _item("r", tier=Tier.ROTATED, cost=100)]
    assert carried_cost(items) == 8
    # Control: flipping only the tiers flips what is counted.
    flipped = [items[0], items[1].model_copy(update={"tier": Tier.ROTATED}),
               items[2].model_copy(update={"tier": Tier.PINNED})]
    assert carried_cost(flipped) == 103


def test_rotation_pressure_above_one_means_over_budget():
    """Catches a pressure that reads the rotated backlog as load, or the wrong way round."""
    items = [_item("a", cost=6), _item("p", tier=Tier.PINNED, cost=6),
             _item("r", tier=Tier.ROTATED, cost=50)]
    assert rotation_pressure(items, budget=10) == pytest.approx(1.2)
    out, _ = _rotate(items, 10)
    assert rotation_pressure(out, budget=10) <= 1.0
    assert rotation_pressure(items, budget=12) == pytest.approx(1.0)
