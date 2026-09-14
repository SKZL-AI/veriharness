"""Amending a specification mid-run, and the ways that could go wrong.

The danger is one sentence long: if the text can move, a candidate that does
not pass can be made to pass by editing what passing means. So the tests here
are mostly about what an amendment *costs* -- which evidence stops counting,
which criteria must be measured again, and whether the record makes the
direction of the change visible.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hoh.amendment import (
    AmendmentKind,
    AmendmentLedger,
    SpecAmendment,
    park_and_amend,
    text_digest,
)


def _a(aid="A-1", *, kind=AmendmentKind.CORRECT, von="aaaa", nach="bbbb",
       kriterien=("K1",), **kw) -> SpecAmendment:
    felder = dict(
        amendment_id=aid, run_id="r", from_digest=von, to_digest=nach,
        kind=kind, actor="captain", reason="the old text asked for something "
        "impossible", affected_criteria=list(kriterien),
    )
    felder.update(kw)
    return SpecAmendment(**felder)


# --------------------------------------------------------------------------- #
# An amendment has to be one
# --------------------------------------------------------------------------- #


def test_an_amendment_that_changes_nothing_is_rejected():
    with pytest.raises(ValidationError, match="did not change"):
        _a(von="aaaa", nach="aaaa")


def test_an_acceptance_affecting_amendment_must_name_its_criteria():
    """Without them nothing can be worked out about which evidence still
    holds, and "all of it" would make every amendment ruinous."""
    for kind in (AmendmentKind.NARROW, AmendmentKind.WIDEN, AmendmentKind.CORRECT):
        with pytest.raises(ValidationError, match="must name the criteria"):
            _a(kind=kind, kriterien=())


def test_a_clarification_that_touches_criteria_is_not_a_clarification():
    """The one kind that claims to change nothing is held to the claim."""
    with pytest.raises(ValidationError, match="is not a clarification"):
        _a(kind=AmendmentKind.CLARIFY, kriterien=("K1",))


def test_a_genuine_clarification_is_allowed_and_costs_nothing():
    a = _a(kind=AmendmentKind.CLARIFY, kriterien=())
    assert not a.acceptance_affecting()
    assert a.invalidates(["r-i1-a1-K1"]) == []
    assert a.requires_revalidation() == []


def test_an_amendment_must_say_why():
    with pytest.raises(ValidationError):
        SpecAmendment(
            amendment_id="A-1", run_id="r", from_digest="a", to_digest="b",
            kind=AmendmentKind.CLARIFY, actor="captain", reason="",
        )


def test_an_amendment_names_a_person_not_a_default():
    """An unattributed policy act is the kind this project spent a release
    correcting. `actor` is required, so there is no default to fall back to."""
    with pytest.raises(ValidationError):
        SpecAmendment(
            amendment_id="A-1", run_id="r", from_digest="a", to_digest="b",
            kind=AmendmentKind.CLARIFY, reason="because",
        )


# --------------------------------------------------------------------------- #
# What it costs
# --------------------------------------------------------------------------- #


def test_only_the_affected_criteria_lose_their_evidence():
    a = _a(kriterien=("K2",))
    quittungen = ["r-i1-a1-K1", "r-i1-a1-K2", "r-i1-a1-K3", "r-i1-a1-K2-basis"]
    assert sorted(a.invalidates(quittungen)) == ["r-i1-a1-K2", "r-i1-a1-K2-basis"]


def test_a_baseline_receipt_is_invalidated_with_its_criterion():
    """The baseline is what makes a criterion discriminating. Keeping it while
    dropping the candidate measurement would leave half a comparison."""
    a = _a(kriterien=("K1",))
    assert "r-i2-a1-K1-basis" in a.invalidates(["r-i2-a1-K1-basis"])


def test_a_clarification_invalidates_nothing():
    a = _a(kind=AmendmentKind.CLARIFY, kriterien=())
    assert a.invalidates(["r-i1-a1-K1", "r-i1-a1-K2"]) == []


def test_widening_requires_the_affected_criteria_to_be_measured_again():
    a = _a(kind=AmendmentKind.WIDEN, kriterien=("K1", "K2"))
    assert sorted(a.requires_revalidation()) == ["K1", "K2"]


def test_an_amendment_after_acceptance_says_so():
    a = _a(after_acceptance=True)
    assert "AFTER ACCEPTANCE" in a.summary()
    assert "AFTER ACCEPTANCE" not in _a().summary()


def test_the_summary_names_the_direction_and_the_actor():
    text = _a(kind=AmendmentKind.NARROW, kriterien=("K7",)).summary()
    assert "narrow" in text and "captain" in text and "K7" in text


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #


def test_a_ledger_with_a_gap_is_rejected():
    """A chain with a gap cannot say what the run promised at any point."""
    with pytest.raises(ValidationError, match="cannot say what the run promised"):
        AmendmentLedger(
            run_id="r", origin_digest="aaaa",
            amendments=[_a("A-1", von="aaaa", nach="bbbb"),
                        _a("A-2", von="cccc", nach="dddd")],
        )


def test_a_continuous_chain_reports_the_current_digest():
    led = AmendmentLedger(
        run_id="r", origin_digest="aaaa",
        amendments=[_a("A-1", von="aaaa", nach="bbbb"),
                    _a("A-2", von="bbbb", nach="cccc")],
    )
    assert led.current_digest() == "cccc"


def test_an_empty_ledger_is_the_origin():
    led = AmendmentLedger(run_id="r", origin_digest="aaaa")
    assert led.current_digest() == "aaaa"
    assert "no amendments" in led.report()


def test_two_amendments_cannot_share_an_id():
    with pytest.raises(ValidationError, match="share an id"):
        AmendmentLedger(
            run_id="r", origin_digest="aaaa",
            amendments=[_a("A-1", von="aaaa", nach="bbbb"),
                        _a("A-1", von="bbbb", nach="cccc")],
        )


def test_a_receipt_invalidated_twice_keeps_the_first_amendment():
    """That is when it stopped counting; a later amendment cannot un-supersede
    it."""
    led = AmendmentLedger(
        run_id="r", origin_digest="aaaa",
        amendments=[
            _a("A-1", von="aaaa", nach="bbbb", kriterien=("K1",)),
            _a("A-2", von="bbbb", nach="cccc", kriterien=("K1", "K2")),
        ],
    )
    ungueltig = led.invalidated_receipts(["r-i1-a1-K1", "r-i1-a1-K2"])
    assert ungueltig["r-i1-a1-K1"] == "A-1"
    assert ungueltig["r-i1-a1-K2"] == "A-2"


def test_the_ledger_collects_every_criterion_needing_revalidation():
    led = AmendmentLedger(
        run_id="r", origin_digest="aaaa",
        amendments=[
            _a("A-1", von="aaaa", nach="bbbb", kriterien=("K1",)),
            _a("A-2", von="bbbb", nach="cccc", kind=AmendmentKind.WIDEN,
               kriterien=("K3",)),
        ],
    )
    assert led.revalidation_needed() == {"K1", "K3"}
    assert "K1, K3" in led.report()


def test_a_ledger_of_clarifications_needs_no_revalidation():
    led = AmendmentLedger(
        run_id="r", origin_digest="aaaa",
        amendments=[_a("A-1", von="aaaa", nach="bbbb",
                       kind=AmendmentKind.CLARIFY, kriterien=())],
    )
    assert led.revalidation_needed() == set()
    assert "revalidation required for: nothing" in led.report()


# --------------------------------------------------------------------------- #
# Writing it, without losing the old text
# --------------------------------------------------------------------------- #


def test_the_superseded_text_is_kept_and_named(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("# Goal\n\nDo the thing.\n")
    alt_digest = text_digest(spec.read_text())

    a = park_and_amend(
        spec, "# Goal\n\nDo the other thing.\n",
        run_id="r", amendment_id="A-1", kind=AmendmentKind.CORRECT,
        actor="captain", reason="the thing turned out to be impossible",
        affected_criteria=["K1"],
    )
    assert a.from_digest == alt_digest
    assert spec.read_text() == "# Goal\n\nDo the other thing.\n"
    geparkt = tmp_path / a.from_path.split("/")[-1]
    assert geparkt.exists(), "the superseded text must remain readable"
    assert geparkt.read_text() == "# Goal\n\nDo the thing.\n"
    assert text_digest(geparkt.read_text()) == a.from_digest


def test_amending_to_the_identical_text_writes_nothing(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("same\n")
    with pytest.raises(ValueError, match="nothing to amend"):
        park_and_amend(
            spec, "same\n", run_id="r", amendment_id="A-1",
            kind=AmendmentKind.CLARIFY, actor="captain", reason="x",
        )
    assert spec.read_text() == "same\n"
    assert list(tmp_path.iterdir()) == [spec], "a refused amendment left a file behind"


def test_the_digests_describe_the_files_on_disk(tmp_path):
    """The ledger is only useful if it can be checked against the tree."""
    spec = tmp_path / "spec.md"
    spec.write_text("one\n")
    a = park_and_amend(
        spec, "two\n", run_id="r", amendment_id="A-1",
        kind=AmendmentKind.CLARIFY, actor="captain", reason="x",
    )
    from pathlib import Path

    assert text_digest(Path(a.to_path).read_text()) == a.to_digest
    assert text_digest(Path(a.from_path).read_text()) == a.from_digest
