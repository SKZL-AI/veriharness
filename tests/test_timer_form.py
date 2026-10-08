"""The timer form of a retry time is never due before the provider's reset.

O237: both `_timer_form` copies -- `hoh.launcher` and `hoh.resumeloop` (the one
the controller uses when it arms a PROVIDER_RETRY timer) -- formatted with
`strftime`, which cut off the fractional seconds. A reset at `...:25.897` gave a
timer due at `...:25Z`; the tick resumed the run, the provider refused again,
and the run was parked and resumed a second time, while `quota.is_due` (full
precision) said not due. These tests pin the rounding up, the agreement with
`quota.is_due`, the timer form `hoh.timers.check_time` accepts, and each
copy's handling of input it cannot read.

Pure function calls: nothing here writes to disk.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hoh import launcher, quota, resumeloop
from hoh.timers import check_time

COPIES = {"launcher": launcher._timer_form, "resumeloop": resumeloop._timer_form}

# A whole second, a millisecond past it, half a second, the reset from the O237
# evidence, and the last microsecond before the next second.
RESETS = [
    "2026-09-25T17:12:25+00:00",
    "2026-09-25T17:12:25.001+00:00",
    "2026-09-25T17:12:25.500+00:00",
    "2026-09-25T17:12:25.897+00:00",
    "2026-09-25T17:12:25.999999+00:00",
    "2026-09-25T17:12:25.897Z",               # the Z spelling
    "2026-09-25T19:12:25.897+02:00",          # a non-UTC offset, same instant
    "2026-09-25T23:59:59.500+00:00",          # rounding carries into the next day
]


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _floor_form(stamp: str) -> str:
    """The implementation before the fix -- the mutant the O237 review found."""
    return _parse(stamp).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _early_instants(form, reset: str) -> list[datetime]:
    """Instants in reset-0.9 s .. reset+0.9 s (0.1 s steps), plus the instant
    the timer itself comes due, at which the timer is due but `quota.is_due`
    says the reset has not passed. The due instant is added because the grid
    alone misses a floor that is early by less than 0.1 s (a reset at .001)."""
    due = _parse(form(reset))
    base = _parse(reset)
    instants = [base + timedelta(milliseconds=100 * step) for step in range(-9, 10)] + [due]
    early = []
    for instant in instants:
        if due <= instant and not quota.is_due(reset, now=instant):
            early.append(instant)
    return early


# -- K1 ---------------------------------------------------------------------

@pytest.mark.parametrize("copy", COPIES)
@pytest.mark.parametrize("reset", RESETS)
def test_timer_never_before_reset_and_less_than_one_second_after(copy, reset):
    """Catches a timer that is due before the reset (the floor), and one that
    rounds too far (a whole extra second, e.g. ceil applied to whole seconds)."""
    timer = COPIES[copy](reset)
    gap = _parse(timer) - _parse(reset)
    assert timedelta(0) <= gap < timedelta(seconds=1), (copy, reset, timer)


@pytest.mark.parametrize("copy", COPIES)
def test_never_before_reset_carries_across_the_day(copy):
    """Catches rounding done by editing the string: 23:59:59.5 must become the
    next day's midnight, not 23:59:60 or 23:59:59."""
    assert COPIES[copy]("2026-09-25T23:59:59.500+00:00") == "2026-09-26T00:00:00Z"


@pytest.mark.parametrize("copy", COPIES)
def test_never_before_reset_evidence_case_rounds_up(copy):
    """Catches the O237 defect on its own evidence: reset ...:25.897 gave a
    timer at ...:25Z; it must be ...:26Z."""
    assert COPIES[copy]("2026-09-25T17:12:25.897+00:00") == "2026-09-25T17:12:26Z"


def test_never_before_reset_negative_control_floor_is_early():
    """Negative control: the old floor form of a fractional reset IS earlier
    than the reset, so the K1 check above can tell the two apart."""
    reset = "2026-09-25T17:12:25.897+00:00"
    assert _parse(_floor_form(reset)) < _parse(reset)


# -- K2 ---------------------------------------------------------------------

@pytest.mark.parametrize("copy", COPIES)
@pytest.mark.parametrize("reset", RESETS)
def test_timer_never_due_before_is_due(copy, reset):
    """Catches a timer that comes due while `quota.is_due` still says not due.

    This FAILS against the floor implementation (strftime dropping the
    fractions) -- the mutant the O237 review found: for every fractional reset
    the floored timer is due at its own due instant, before the reset.
    """
    assert _early_instants(COPIES[copy], reset) == [], (copy, reset)


@pytest.mark.parametrize("reset", [r for r in RESETS if _parse(r).microsecond])
def test_never_due_before_is_due_control_detects_the_floor_mutant(reset):
    """Negative control: the same predicate applied to the floor mutant finds
    early instants for every fractional reset -- the test above is not vacuous."""
    assert _early_instants(_floor_form, reset)


# -- K3 ---------------------------------------------------------------------

@pytest.mark.parametrize("stamp", ["soon", "2026-09-25T25:00:00Z"])
def test_unreadable_launcher_gives_none(stamp):
    """Catches a launcher that guesses a time: an unreadable stamp must leave
    the node with a human owner (None), not parked on an uncheckable time."""
    assert launcher._timer_form(stamp) is None


@pytest.mark.parametrize("stamp", ["soon", "2026-09-25T25:00:00Z"])
def test_unreadable_resumeloop_returns_input_unchanged(stamp):
    """Catches a resume-loop copy that drops or repairs an unreadable stamp:
    it must come back unchanged so resumption refuses it."""
    assert resumeloop._timer_form(stamp) == stamp


def test_naive_launcher_gives_none():
    """Catches a launcher that reads a zone-less stamp as some zone."""
    assert launcher._timer_form("2026-09-25T10:00:00") is None


def test_naive_resumeloop_returns_input_unchanged():
    """Catches a resume-loop copy that converts a zone-less stamp instead of
    handing it on for resumption to refuse."""
    assert resumeloop._timer_form("2026-09-25T10:00:00") == "2026-09-25T10:00:00"


def test_naive_fractional_stamp_is_not_rounded_into_a_timer():
    """Catches rounding that happens before the zone check: a naive fractional
    stamp keeps the naive handling of each copy."""
    stamp = "2026-09-25T10:00:00.500"
    assert launcher._timer_form(stamp) is None
    assert resumeloop._timer_form(stamp) == stamp


@pytest.mark.parametrize("copy", COPIES)
@pytest.mark.parametrize("empty", [None, ""])
def test_unreadable_nothing_recorded_gives_none(copy, empty):
    """Catches a copy that turns "no retry time" into a string."""
    assert COPIES[copy](empty) is None


@pytest.mark.parametrize("copy", COPIES)
@pytest.mark.parametrize("stamp", ["2026-09-25T17:12:25Z", "2026-09-25T17:12:25.000+00:00",
                                   "2026-09-25T19:12:25+02:00"])
def test_whole_second_reset_is_unchanged(copy, stamp):
    """Catches rounding that also moves a whole second (an unconditional +1 s):
    stored timers and exact-string comparisons must stay byte-identical."""
    assert COPIES[copy](stamp) == "2026-09-25T17:12:25Z"
    assert COPIES[copy](stamp) == _floor_form(stamp)


# -- K5 ---------------------------------------------------------------------

@pytest.mark.parametrize("copy", COPIES)
@pytest.mark.parametrize("reset", RESETS)
def test_output_is_in_check_time_form(copy, reset):
    """Catches an output that keeps fractions or an offset, e.g.
    `isoformat().replace("+00:00", "Z")`: `hoh.timers.check_time` rejects it."""
    timer = COPIES[copy](reset)
    assert check_time(timer) == timer


def test_check_time_form_negative_control_rejects_isoformat_mutant():
    """Negative control: the isoformat mutant keeps the fractions, which K1-K3
    alone would accept, and `check_time` refuses it."""
    mutant = _parse("2026-09-25T17:12:25.897+00:00").isoformat().replace("+00:00", "Z")
    with pytest.raises(ValueError):
        check_time(mutant)
