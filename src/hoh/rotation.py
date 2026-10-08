"""Context rotation (V3.3 P2-14): moving Layer 1 material to Layer 2.

A programme that runs for weeks accumulates more context than any brief can
carry. Hierarchical loading answers *what to include*; rotation answers *what
to do with what no longer fits*:

    Layer 0  compact low-resolution context        (hoh.taskstate)
    Layer 1  relevant decisions / evidence         <- rotated out of here
    Layer 2  full source available on demand       <- ... to here

The rule this module exists for:

    Rotation may reduce what is *carried*. It may never reduce what is
    *reachable*, and it may never happen silently.

So every rotation leaves a `RotationRecord` that is addressable (`record_id`),
digest-bound (`record_digest`, and each item's content `digest`) and complete
about what it moved (`item_ids`, `source_refs` and `digests` are refused unless
they have the same length). `reachable` turns "Layer 2 still holds it" from an
assertion into a check.

No model or provider call is made here. A rotated item carries its *reference*
(`source_ref`, `digest`) and a caller-supplied one-line `gist`; summarising a
rotated group into new prose would be a provider call, and this module invents
no prose. It also reads nothing: items are handed in, nothing touches the run
store, the filesystem, git or the network, and there is no wall clock -- `now`
is a parameter. Inputs are never mutated; equal inputs give equal results.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import ConfigDict, Field, field_validator, model_validator

from hoh.contracts import Strict


class Tier(StrEnum):
    """Where a piece of context currently sits.

    PINNED is the escape hatch with teeth: the North Star digest, the work
    package under execution, an open blocker. `rotate` never moves a pinned
    item, and raises rather than drop one when the budget cannot hold them.
    """

    CARRIED = "CARRIED"
    ROTATED = "ROTATED"
    PINNED = "PINNED"


def content_digest(content: str | bytes) -> str:
    """sha256 hex of `content` -- the digest a caller stores in `ContextItem.digest`.

    `reachable` recomputes exactly this over what the resolver returns, so an
    item built with any other digest function will be reported as changed.
    """
    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    return hashlib.sha256(data).hexdigest()


def _require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware; a naive time cannot be ordered safely")
    return value


class ContextItem(Strict):
    """One piece of context and the pointer that makes it reachable again.

    An empty `source_ref` or `digest` is refused at construction: an item that
    cannot be reached again after rotation is an item rotation would destroy.
    """

    model_config = ConfigDict(frozen=True)

    item_id: str = Field(min_length=1)
    tier: Tier
    title: str
    #: Caller-supplied one-line gist. Never generated here.
    gist: str
    source_ref: str = Field(min_length=1)
    digest: str = Field(min_length=1)
    cost: int = Field(ge=0)
    last_touched_at: datetime
    epoch: int

    @field_validator("last_touched_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "last_touched_at")


def record_digest(*, record_id: str, rotated_at: datetime, epoch: int,
                  item_ids: Iterable[str], source_refs: Iterable[str],
                  digests: Iterable[str], reason: str, freed: int) -> str:
    """sha256 over canonical JSON of everything a `RotationRecord` claims.

    Same idiom as `taskstate.state_digest`: sorted keys, compact separators,
    utf-8. Every field is covered -- editing the reason after the fact is as
    much a falsification as editing which items moved.
    """
    body = {
        "record_id": record_id,
        "rotated_at": rotated_at.isoformat(),
        "epoch": epoch,
        "item_ids": list(item_ids),
        "source_refs": list(source_refs),
        "digests": list(digests),
        "reason": reason,
        "freed": freed,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RotationRecord(Strict):
    """What one rotation did. Frozen and digest-bound.

    Refused at construction: `item_ids`, `source_refs` and `digests` of unequal
    length (a rotation naming three items and two sources has lost one), an
    empty rotation (no rotation is `None`, not an empty record), a blank
    reason (rotation never happens silently) and a `record_digest` that is not
    the digest of what the record shows.
    """

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1)
    rotated_at: datetime
    epoch: int
    item_ids: tuple[str, ...]
    source_refs: tuple[str, ...]
    digests: tuple[str, ...]
    reason: str = Field(min_length=1)
    freed: int = Field(ge=0)
    record_digest: str

    @field_validator("rotated_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "rotated_at")

    @model_validator(mode="after")
    def _complete_and_bound(self) -> RotationRecord:
        lengths = (len(self.item_ids), len(self.source_refs), len(self.digests))
        if len(set(lengths)) != 1:
            raise ValueError(
                f"item_ids, source_refs and digests must have the same length, got {lengths}; "
                "a record that names fewer sources than items has lost one")
        if not self.item_ids:
            raise ValueError("a rotation record must name at least one item; "
                             "nothing rotated is None, not an empty record")
        if any(not s.strip() for s in self.source_refs + self.digests):
            raise ValueError("every rotated item needs a non-empty source_ref and digest")
        if self.record_digest != self.compute_digest():
            raise ValueError("record_digest does not match the record's content")
        return self

    def compute_digest(self) -> str:
        return record_digest(
            record_id=self.record_id, rotated_at=self.rotated_at, epoch=self.epoch,
            item_ids=self.item_ids, source_refs=self.source_refs, digests=self.digests,
            reason=self.reason, freed=self.freed)


# --------------------------------------------------------------------------- #
# cost
# --------------------------------------------------------------------------- #


def carried_cost(items: Iterable[ContextItem]) -> int:
    """Total cost of what a brief carries: CARRIED and PINNED, never ROTATED."""
    return sum(i.cost for i in items if i.tier is not Tier.ROTATED)


def rotation_pressure(items: Iterable[ContextItem], *, budget: int) -> float:
    """`carried_cost / budget`; above 1.0 the budget is already exceeded."""
    if budget <= 0:
        raise ValueError(f"budget must be at least 1, got {budget}")
    return carried_cost(items) / budget


# --------------------------------------------------------------------------- #
# rotate
# --------------------------------------------------------------------------- #


def _coldness(item: ContextItem) -> tuple[int, datetime, str]:
    # Rule 6 before rule 2: an older epoch goes first whatever its timestamp;
    # item_id makes ties deterministic regardless of input order.
    return (item.epoch, item.last_touched_at, item.item_id)


def rotate(
    items: Iterable[ContextItem],
    *,
    budget: int,
    now: datetime,
    keep_touched_within: timedelta | None = None,
    reason: str,
) -> tuple[list[ContextItem], RotationRecord | None]:
    """Rotate CARRIED items to ROTATED until the carried cost fits `budget`.

    Returns the new item list (input order, rotated items with tier ROTATED)
    and the record, or `(items, None)` when nothing needed to move.

    1. PINNED never rotates; pinned cost alone over `budget` raises.
    2. Coldest first: by `last_touched_at`, oldest first, then `item_id`.
    3. Every rotated item is in the record with its `source_ref` and `digest`.
    4. Idempotent: rotating the result again with the same budget returns None.
    5. `keep_touched_within` protects items touched at or after
       `now - keep_touched_within` -- unless the budget is otherwise
       unreachable; then the coldest protected items rotate and the record's
       reason says the protection was overridden.
    6. An older `epoch` rotates before any item of a newer one.
    7. `budget <= 0` raises.

    Duplicate item ids, a blank reason, a naive `now` and a negative
    `keep_touched_within` raise ValueError.
    """
    if budget <= 0:
        raise ValueError(f"budget must be at least 1, got {budget}")
    if not reason or not reason.strip():
        raise ValueError("reason must not be blank; rotation never happens silently")
    _require_aware(now, "now")
    if keep_touched_within is not None and keep_touched_within < timedelta(0):
        raise ValueError("keep_touched_within must not be negative")

    items = [i if isinstance(i, ContextItem) else ContextItem.model_validate(i) for i in items]
    duplicates = sorted(k for k, n in Counter(i.item_id for i in items).items() if n > 1)
    if duplicates:
        raise ValueError(f"duplicate item_id(s): {', '.join(duplicates)}")

    pinned = sum(i.cost for i in items if i.tier is Tier.PINNED)
    if pinned > budget:
        raise ValueError(
            f"pinned items alone cost {pinned}, over budget {budget}; "
            "a budget that cannot hold what must be held is a caller error")

    excess = carried_cost(items) - budget
    if excess <= 0:
        return items, None

    candidates = sorted((i for i in items if i.tier is Tier.CARRIED), key=_coldness)
    if keep_touched_within is None:
        free, protected = candidates, []
    else:
        since = now - keep_touched_within
        free = [i for i in candidates if i.last_touched_at < since]
        protected = [i for i in candidates if i.last_touched_at >= since]

    chosen: list[ContextItem] = []
    overridden: list[str] = []
    for group, is_protected in ((free, False), (protected, True)):
        for item in group:
            if excess <= 0:
                break
            chosen.append(item)
            excess -= item.cost
            if is_protected:
                overridden.append(item.item_id)

    text = reason.strip()
    if overridden:
        text += (f"; keep_touched_within={keep_touched_within} protection overridden for "
                 f"{', '.join(overridden)}: budget {budget} unreachable otherwise")

    moved = {i.item_id for i in chosen}
    out = [i.model_copy(update={"tier": Tier.ROTATED}) if i.item_id in moved else i
           for i in items]

    fields = dict(
        rotated_at=now,
        epoch=max(i.epoch for i in items),
        item_ids=tuple(i.item_id for i in chosen),
        source_refs=tuple(i.source_ref for i in chosen),
        digests=tuple(i.digest for i in chosen),
        reason=text,
        freed=sum(i.cost for i in chosen),
    )
    # Addressable by content: the same rotation of the same state has the same id.
    record_id = "rot-" + record_digest(record_id="", **fields)[:16]
    record = RotationRecord(record_id=record_id, **fields,
                            record_digest=record_digest(record_id=record_id, **fields))
    return out, record


# --------------------------------------------------------------------------- #
# reachable
# --------------------------------------------------------------------------- #


def reachable(record: RotationRecord, *,
              resolve: Callable[[str], str | bytes | None]) -> list[str]:
    """The refs of `record` that Layer 2 no longer holds; `[]` when it holds all.

    A ref is reported when `resolve` raises, returns None, or returns content
    whose `content_digest` differs from the recorded digest -- a stale pointer
    is not a working one. Order follows the record.
    """
    lost: list[str] = []
    for ref, want in zip(record.source_refs, record.digests):
        try:
            content = resolve(ref)
        except Exception:  # noqa: BLE001 -- any failure to resolve is unreachability
            lost.append(ref)
            continue
        if content is None or content_digest(content) != want:
            lost.append(ref)
    return lost
