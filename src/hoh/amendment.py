"""Changing a specification after a run has begun, without losing what it meant.

`docs/LIMITATIONS.md` limit 15 states the situation this replaces: a
specification is immutable for the life of a run, and there is no supported
path to amend one. That is a safe default and a real obstruction. Requirements
are discovered by working on them, and a run that cannot absorb a correction
has to be thrown away and restarted -- which loses the evidence, the budget
and the history of why the correction was needed.

The danger is obvious and is the reason the default was immutability: if the
text can move, a candidate that does not pass can be made to pass by editing
what passing means. Every rule here exists to make that visible rather than to
make it impossible, because a person with commit access can always edit a file.

**An amendment is a record, not an edit.** The old text keeps its digest and
stays readable; the new text is a new version beside it. Nothing is
overwritten, so "what did this run actually promise when iteration 3 was
accepted" has an answer.

**An acceptance-affecting amendment invalidates the evidence that rested on
the old text.** Not all evidence -- naming which criteria are affected is the
author's job, and the ledger then derives which receipts stop counting and
which criteria must be measured again. A receipt is not deleted: it is marked
as belonging to a superseded specification, which is the difference between
losing evidence and re-scoping it.

**An amendment after acceptance is a different act from one before it.** The
first re-opens a decision that has already been made, and the record says so.

What this module does not do is judge whether an amendment is honest. It
makes the shape of the change, its author, its reason and its cost visible in
one place, so that a reader can.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator

from .contracts import Strict, utcnow

AMENDMENT_SCHEMA_VERSION = 1


def text_digest(text: str) -> str:
    """The same digest discipline the rest of the project uses for specs."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class AmendmentKind(StrEnum):
    """What kind of change this is, from the point of view of acceptance."""

    #: Wording only. The set of things that would pass is unchanged -- and the
    #: ledger checks that claim against `affected_criteria` rather than taking
    #: it on faith.
    CLARIFY = "clarify"
    #: Removes a requirement or loosens a criterion. The suspicious direction:
    #: something that would not have passed now might.
    NARROW = "narrow"
    #: Adds a requirement. Existing evidence may be insufficient rather than
    #: wrong, so what it invalidates is different.
    WIDEN = "widen"
    #: The old text said something untrue or impossible. Not narrowing and not
    #: widening: a different requirement, and everything measured against the
    #: old one is measured against a thing that no longer exists.
    CORRECT = "correct"


#: Kinds that can change what would be accepted. `CLARIFY` is the only one
#: that claims not to, which is why the validator holds it to that claim.
ACCEPTANCE_AFFECTING: frozenset[AmendmentKind] = frozenset(
    {AmendmentKind.NARROW, AmendmentKind.WIDEN, AmendmentKind.CORRECT}
)


class SpecAmendment(Strict):
    """One change to a specification, with everything it costs."""

    schema_version: int = AMENDMENT_SCHEMA_VERSION
    amendment_id: str
    run_id: str
    #: The digest of the text this amends, and of the text that replaces it.
    #: Both, so a reader can tell whether the ledger describes the files on
    #: disk or has drifted from them.
    from_digest: str
    to_digest: str
    #: Where the superseded text now lives. Never overwritten in place.
    from_path: str = ""
    to_path: str = ""
    kind: AmendmentKind
    #: Who decided. Not "the orchestrator" by default: an amendment is a
    #: policy act, and an unattributed one is the kind this project has spent
    #: a release correcting.
    actor: str
    reason: str = Field(min_length=1)
    #: What the reason rests on -- a receipt id, a finding, a commit. Empty is
    #: allowed and is itself informative.
    evidence: list[str] = Field(default_factory=list)
    #: The criteria whose meaning changes. The author names them; the ledger
    #: derives the consequences.
    affected_criteria: list[str] = Field(default_factory=list)
    #: True when the run had already accepted a candidate. Re-opening a
    #: decision that has been made is a different act from changing one that
    #: has not, and the record says which.
    after_acceptance: bool = False
    created_at: str = Field(default_factory=utcnow)
    #: Fencing, as everywhere else in this project: two processes amending the
    #: same run must not silently produce one surviving amendment.
    write_seq: int = 0

    @model_validator(mode="after")
    def _kind_matches_its_claims(self) -> SpecAmendment:
        if self.from_digest == self.to_digest:
            raise ValueError(
                f"{self.amendment_id}: the text did not change "
                f"({self.from_digest}); an amendment that amends nothing is a "
                "record of an intention, not of a change"
            )
        if self.kind is AmendmentKind.CLARIFY and self.affected_criteria:
            raise ValueError(
                f"{self.amendment_id}: a CLARIFY that names affected criteria "
                f"({', '.join(self.affected_criteria)}) is not a clarification. "
                "Say which it is -- narrow, widen or correct -- so the evidence "
                "it costs is visible"
            )
        if self.acceptance_affecting() and not self.affected_criteria:
            raise ValueError(
                f"{self.amendment_id}: a {self.kind.value} amendment must name "
                "the criteria it affects, or nothing can be worked out about "
                "which evidence still holds"
            )
        return self

    def acceptance_affecting(self) -> bool:
        return self.kind in ACCEPTANCE_AFFECTING

    def invalidates(self, receipt_ids: list[str]) -> list[str]:
        """Which of these receipts no longer supports an acceptance decision.

        Matched on the check id embedded in the receipt id, which is the
        project's own naming: `<run>-i<n>-a<m>-<check_id>[-basis]`. A receipt
        for an untouched criterion keeps counting -- invalidating everything
        would be simpler and would make every amendment ruinous, which is how
        a feature becomes one people route around.
        """
        if not self.acceptance_affecting():
            return []
        betroffen = set(self.affected_criteria)
        raus = []
        for rid in receipt_ids:
            teile = rid.split("-")
            if not teile:
                continue
            kandidat = teile[-2] if rid.endswith("-basis") else teile[-1]
            if kandidat in betroffen:
                raus.append(rid)
        return raus

    def requires_revalidation(self) -> list[str]:
        """Criteria that must be measured again before acceptance can stand.

        For `WIDEN` this is the affected criteria: the old measurements are not
        wrong, they are insufficient. For `NARROW` and `CORRECT` it is the same
        list for a different reason -- the criterion now means something else,
        so what was measured was a different question.
        """
        return list(self.affected_criteria) if self.acceptance_affecting() else []

    def summary(self) -> str:
        teile = [
            f"{self.amendment_id} {self.kind.value} by {self.actor}",
            f"{self.from_digest} -> {self.to_digest}",
        ]
        if self.affected_criteria:
            teile.append("affects " + ", ".join(sorted(self.affected_criteria)))
        if self.after_acceptance:
            teile.append("AFTER ACCEPTANCE")
        return " · ".join(teile)


class AmendmentLedger(Strict):
    """Every amendment to one run's specification, in order."""

    run_id: str
    #: The digest the run started with. An amendment chain that does not begin
    #: here describes a different run.
    origin_digest: str
    amendments: list[SpecAmendment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _chain_is_continuous(self) -> AmendmentLedger:
        erwartet = self.origin_digest
        for a in self.amendments:
            if a.from_digest != erwartet:
                raise ValueError(
                    f"{a.amendment_id} amends {a.from_digest}, but the "
                    f"specification was at {erwartet}. A chain with a gap "
                    "cannot say what the run promised at any point in it"
                )
            erwartet = a.to_digest
        ids = [a.amendment_id for a in self.amendments]
        if len(ids) != len(set(ids)):
            raise ValueError("two amendments share an id")
        return self

    def current_digest(self) -> str:
        return self.amendments[-1].to_digest if self.amendments else self.origin_digest

    def acceptance_affecting(self) -> list[SpecAmendment]:
        return [a for a in self.amendments if a.acceptance_affecting()]

    def invalidated_receipts(self, receipt_ids: list[str]) -> dict[str, str]:
        """Receipt id -> the amendment id that superseded it.

        A receipt invalidated twice keeps the **first** amendment that did it:
        that is when it stopped counting, and a later one cannot un-supersede
        it.
        """
        raus: dict[str, str] = {}
        for a in self.amendments:
            for rid in a.invalidates(receipt_ids):
                raus.setdefault(rid, a.amendment_id)
        return raus

    def revalidation_needed(self) -> set[str]:
        noetig: set[str] = set()
        for a in self.amendments:
            noetig.update(a.requires_revalidation())
        return noetig

    def report(self) -> str:
        if not self.amendments:
            return f"{self.run_id}: no amendments; specification at {self.origin_digest}"
        zeilen = [f"{self.run_id}: {len(self.amendments)} amendment(s)"]
        for a in self.amendments:
            zeilen.append("  " + a.summary())
            zeilen.append(f"      reason: {a.reason}")
            if a.evidence:
                zeilen.append("      evidence: " + ", ".join(a.evidence))
        noetig = self.revalidation_needed()
        zeilen.append(
            "  revalidation required for: "
            + (", ".join(sorted(noetig)) if noetig else "nothing")
        )
        return "\n".join(zeilen)


def park_and_amend(
    spec_path: Path | str,
    new_text: str,
    *,
    run_id: str,
    amendment_id: str,
    kind: AmendmentKind,
    actor: str,
    reason: str,
    affected_criteria: list[str] | None = None,
    evidence: list[str] | None = None,
    after_acceptance: bool = False,
    write_seq: int = 0,
) -> SpecAmendment:
    """Writes the new specification beside the old one and returns the record.

    The old file is **kept**, renamed with a version suffix, in line with this
    machine's rule that nothing is deleted. The returned amendment names both
    paths, so the superseded text can be read rather than reconstructed.

    Raises before writing anything if the text is unchanged: a no-op amendment
    would leave a record of an intention and no change to go with it.
    """
    pfad = Path(spec_path).expanduser()
    alt = pfad.read_text(encoding="utf-8")
    von, nach = text_digest(alt), text_digest(new_text)
    if von == nach:
        raise ValueError(
            f"{amendment_id}: the new text is identical to the old one "
            f"({von}); there is nothing to amend"
        )
    geparkt = pfad.with_name(
        f"{pfad.name}.v{utcnow().replace(':', '-')}.{von}"
    )
    pfad.rename(geparkt)
    pfad.write_text(new_text, encoding="utf-8")
    return SpecAmendment(
        amendment_id=amendment_id,
        run_id=run_id,
        from_digest=von,
        to_digest=nach,
        from_path=str(geparkt),
        to_path=str(pfad),
        kind=kind,
        actor=actor,
        reason=reason,
        affected_criteria=list(affected_criteria or []),
        evidence=list(evidence or []),
        after_acceptance=after_acceptance,
        write_seq=write_seq,
    )
