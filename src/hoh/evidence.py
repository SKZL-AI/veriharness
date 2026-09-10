"""Evidence state E_t -- the second state that crosses loop boundaries.

The HoH paper (§3.3) insists that a later loop inherits more than the code: the
artifact state A_t carries the implementation, the evidence state E_t carries
the validated project knowledge that steers the next planning round.

    (A_{t-1}, E_{t-1})  --loop t under S-->  (A_t, E_t)

Without E_t a later loop has to reconstruct the development state from the
code -- and in doing so it overlooks open requirements, repeats paths that were
already refuted, forgets unresolved defects, or regresses validated behaviour.
Those are exactly the four failures the paper describes.

Normalizing a QA report into a bundle is **deterministic** and lives here in
the adapter, not in the model (paper A.2, final paragraph).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .contracts import Outcome, RoleResult, Strict, digest, utcnow


class EvidenceStatus(StrEnum):
    """Four categories. `VERIFIED` is the only one that permits anything --
    all the others are open items that have to enter the next planning round."""

    VERIFIED = "VERIFIED"
    """Evidenced by candidate-bound records. Becomes a preservation requirement."""

    GAP = "GAP"
    """A visible defect or an unmet requirement."""

    REGRESSION = "REGRESSION"
    """Was VERIFIED in an earlier loop and is not any more."""

    INSUFFICIENT = "INSUFFICIENT"
    """Not refuted, but not evidenced either. Explicitly not a success."""


class EvidenceItem(Strict):
    """One claim-evidence record."""

    item_id: str
    status: EvidenceStatus
    claim: str
    check_id: str | None = None
    receipt_ids: list[str] = Field(default_factory=list)
    first_seen_iteration: int
    last_seen_iteration: int
    impact: str | None = None
    recommendation: str | None = None
    was_verified: bool = Field(
        default=False,
        description="Was this item ever VERIFIED? Decides whether a later "
        "failure is a regression -- comparing against the immediately previous "
        "status was not enough: VERIFIED -> INSUFFICIENT -> GAP quietly "
        "downgraded the regression in the second round.",
    )

    def is_open(self) -> bool:
        return self.status is not EvidenceStatus.VERIFIED


class EvidenceBundle(Strict):
    """E_t. Crosses the loop boundary and is the planner's main input."""

    run_id: str
    iteration: int = 0
    items: list[EvidenceItem] = Field(default_factory=list)
    updated_at: str = Field(default_factory=utcnow)

    # -- The views the planner needs (paper A.2, /previous-iteration-evidence) --

    def to_preserve(self) -> list[EvidenceItem]:
        """Validated behaviour = a preservation requirement for the next loop."""
        return [i for i in self.items if i.status is EvidenceStatus.VERIFIED]

    def to_repair(self) -> list[EvidenceItem]:
        """Blockers and regressions first -- ahead of product extensions
        (paper A.2, /planning-policy)."""
        order = {EvidenceStatus.REGRESSION: 0, EvidenceStatus.GAP: 1}
        return sorted(
            (i for i in self.items if i.status in order),
            key=lambda i: (order[i.status], i.first_seen_iteration),
        )

    def insufficient(self) -> list[EvidenceItem]:
        return [i for i in self.items if i.status is EvidenceStatus.INSUFFICIENT]

    def has_open_work(self) -> bool:
        return any(i.is_open() for i in self.items)

    # -- Continuation across the loop boundary ---------------------------- #

    def carry_forward(self, new_items: list[EvidenceItem], iteration: int) -> EvidenceBundle:
        """Merges E_{t-1} and the fresh observations into E_t.

        No deletion (a house rule as well): an item never disappears, it
        changes its status. An item that was VERIFIED and is now FAIL gets
        explicitly promoted to REGRESSION -- that is the case A04 checks, and
        the one a plain overwrite would have swallowed.
        """
        by_id = {i.item_id: i.model_copy(deep=True) for i in self.items}

        for fresh in new_items:
            prior = by_id.get(fresh.item_id)
            if prior is None:
                item = fresh.model_copy(deep=True)
                item.first_seen_iteration = fresh.first_seen_iteration or iteration
                item.last_seen_iteration = iteration
                item.was_verified = fresh.status is EvidenceStatus.VERIFIED
                by_id[item.item_id] = item
                continue

            status = fresh.status
            # Regression hangs on "was ever verified", not on the previous
            # status. Otherwise a defect loses its priority in its second
            # failing round and slides to the back of to_repair().
            if (
                (prior.was_verified or prior.status is EvidenceStatus.VERIFIED)
                and fresh.status in (EvidenceStatus.GAP, EvidenceStatus.INSUFFICIENT)
            ):
                status = EvidenceStatus.REGRESSION

            prior.status = status
            prior.was_verified = prior.was_verified or status is EvidenceStatus.VERIFIED
            prior.claim = fresh.claim or prior.claim
            prior.check_id = fresh.check_id or prior.check_id
            # Do NOT inherit receipts: an item that is red now must not keep
            # carrying the receipt from the round in which it was green --
            # otherwise a defect looks evidenced.
            prior.receipt_ids = list(fresh.receipt_ids)
            prior.impact = fresh.impact or prior.impact
            prior.recommendation = fresh.recommendation or prior.recommendation
            prior.last_seen_iteration = iteration

        return EvidenceBundle(
            run_id=self.run_id,
            iteration=iteration,
            items=sorted(by_id.values(), key=lambda i: i.item_id),
            updated_at=utcnow(),
        )

    def packet(self, *, max_items: int = 40) -> str:
        """Progressive disclosure: a compact index for the role prompt.

        The paper deliberately does without a memory module and persists
        artifacts in the file system; the role gets an index and loads more on
        demand. Hence short lines here instead of whole reports.
        """
        if not self.items:
            return "(Iteration 1: no evidence from earlier rounds.)"

        lines: list[str] = []

        def section(title: str, items: list[EvidenceItem]) -> None:
            if not items:
                return
            lines.append(f"### {title}")
            for i in items[:max_items]:
                ref = f" [{i.check_id}]" if i.check_id else ""
                lines.append(f"- {i.item_id}{ref}: {i.claim}")
                if i.recommendation:
                    lines.append(f"  -> {i.recommendation}")
            if len(items) > max_items:
                lines.append(f"- (... {len(items) - max_items} more, see evidence.json)")

        section("To preserve (validated)", self.to_preserve())
        section("To repair (blockers and regressions first)", self.to_repair())
        section("Insufficiently evidenced -- not a success", self.insufficient())
        return "\n".join(lines)


def normalize(result: RoleResult, *, prior: EvidenceBundle) -> EvidenceBundle:
    """Deterministic conversion of a QA result into E_t.

    The model supplies verdicts; the mapping onto evidence categories is done
    by the adapter. That way a role output cannot declare itself VERIFIED.
    """
    mapping = {
        Outcome.PASS: EvidenceStatus.VERIFIED,
        Outcome.FAIL: EvidenceStatus.GAP,
        Outcome.INCONCLUSIVE: EvidenceStatus.INSUFFICIENT,
    }

    fresh: list[EvidenceItem] = []
    for verdict in result.verdicts:
        fresh.append(
            EvidenceItem(
                item_id=f"E-{verdict.check_id}",
                status=mapping[verdict.outcome],
                claim=verdict.note or verdict.check_id,
                check_id=verdict.check_id,
                receipt_ids=[verdict.receipt_id] if verdict.receipt_id else [],
                first_seen_iteration=result.iteration,
                last_seen_iteration=result.iteration,
                recommendation=verdict.reproduction,
            )
        )

    # Open gaps that QA reported outside the criteria are not lost -- they are
    # insufficiently evidenced, not absent.
    for gap in result.open_gaps:
        # The id comes from the content, not from the iteration number:
        # otherwise the same open point shows up as a new item every round and
        # never gets closed.
        fresh.append(
            EvidenceItem(
                item_id=f"E-gap-{digest(gap)[:8]}",
                status=EvidenceStatus.INSUFFICIENT,
                claim=gap,
                first_seen_iteration=result.iteration,
                last_seen_iteration=result.iteration,
            )
        )

    return prior.carry_forward(fresh, result.iteration)
