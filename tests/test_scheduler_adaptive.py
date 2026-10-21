"""P3-26 slice 1: AUTO_ADAPTIVE as a pure adaptation along the P3-25 ladder.

`hoh.scheduler.adapt_profile(state, signal, bounds)` takes the profile in force and the count of stable readings
(the caller's state), one reading -- the `reason` of a `hoh.backpressure` decision -- and the operator's bounds, and
returns the profile for the next wave. Every test names what it catches. The numbers are literal (3 stable readings,
16 and 17, the sequences), not read from the module, so a changed constant fails a test instead of moving it. The
reports are built by hand the way `tests/test_backpressure.py` builds them and run through the real
`backpressure.decide`, so the signal is measured on what the decision really produces. Nothing here reads a file of
the host, a clock, the home directory or a state root, starts a process, Herdr or an agent, or touches the network.
The function is not called by anything in the product yet: these tests pin its contract, they are not evidence that
a run uses it.
"""
from __future__ import annotations

import dataclasses
import importlib
import itertools
from datetime import datetime, timezone

import pytest

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
LADDER = ("LINEAR", "CONSERVATIVE", "MODERATE", "HIGH", "MAX_SAFE")


def _s():
    """`hoh.scheduler`, imported inside each test: a tree without the new names fails each test with its own
    error (exit 1), not with one collection error (exit 4) that hides the others."""
    return importlib.import_module("hoh.scheduler")


def _bp():
    return importlib.import_module("hoh.backpressure")


def step(state, signal, top="HIGH", up_after=3):
    s = _s()
    return s.adapt_profile(state, signal, s.Bounds(top, up_after))


def run(signals, top="HIGH", up_after=3, start=None):
    """`[(level, stable, reason), ...]` of a sequence of readings from the start state (or `start`)."""
    s = _s()
    bounds = s.Bounds(top, up_after)
    state = s.adaptive_start(bounds) if start is None else start
    out = []
    for signal in signals:
        got = s.adapt_profile(state, signal, bounds)
        out.append((got.level, got.stable, got.reason))
        state = got.state
    return out


def _group(state):
    from hoh.health import ProviderHealth
    return ProviderHealth(provider="claude", account_handle="acct-a", state=state, not_before=None,
                          runs=("r",), failures=(("USAGE_LIMIT", 1),))


def _wait(*, due=False, given_up=False):
    from hoh.failures import NormalizedFailure
    from hoh.health import Evidence, Wait
    from hoh.telemetry import NOT_AVAILABLE
    return Wait(run_id="u", failure=NormalizedFailure.USAGE_LIMIT, evidence=Evidence.LIMIT, retry_at=None,
                due=due, provider=NOT_AVAILABLE, account_handle=NOT_AVAILABLE, model=NOT_AVAILABLE,
                unattributed="no receipt describes this park", given_up=given_up)


def _report(groups=(), loose=(), problems=(), at=None):
    from hoh.health import HealthReport
    return HealthReport(at=NOW.isoformat() if at is None else at, providers=tuple(groups),
                        unattributed=tuple(loose), problems=tuple(problems), runs_seen=0)


def test_the_vocabulary_is_closed_and_the_values_are_frozen():
    """Catches a reason that changed its meaning, a seventh reason of the adaptation, the recommended three stable
    readings moving, the start profile moving, the limit 16, a mutable state or result, and fields or `as_dict`
    keys that changed."""
    s = _s()
    assert s.ADAPT_SIGNALS == {"NONE": "stable", "DEGRADED": "down", "RESET_DUE": "down", "LIMITED": "floor",
                               "GIVEN_UP": "floor", "INCOMPLETE": "floor", "STALE": "floor"}
    assert s.ADAPT_REASONS == ("stable", "up", "top", "down", "floor", "unreadable", "above_top")
    assert (s.ADAPT_UP_AFTER, s.ADAPT_START, s.MAX_UP_AFTER, s.MAX_WAVE) == (3, "MODERATE", 16, 16)
    assert [f.name for f in dataclasses.fields(s.Adaptive)] == ["level", "stable"]
    assert [f.name for f in dataclasses.fields(s.Bounds)] == ["top", "up_after"]
    assert [f.name for f in dataclasses.fields(s.Adaptation)] == ["level", "stable", "reason", "changed"]
    got = step(s.Adaptive("MODERATE", 2), "NONE")
    assert got.as_dict() == {"level": "HIGH", "stable": 0, "reason": "up", "changed": True}
    assert got.state == s.Adaptive("HIGH", 0)
    for frozen, field in ((s.Adaptive("LINEAR", 0), "level"), (s.Bounds("HIGH", 3), "top"), (got, "level")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(frozen, field, "MAX_SAFE")
    for item in s.ADAPT_SIGNALS.values():
        assert item in {"stable", "down", "floor"}


def test_each_reason_of_a_real_backpressure_decision_is_read_the_way_its_width_rule_says():
    """Catches an adaptation that drifts from `hoh.backpressure` (a reason added there without a row here, a rule
    changed there), a state that was never run through `decide`, and the enum member not being read by its text.
    Every case is a real `HealthReport` through the real `decide`."""
    from hoh.health import HealthState
    s, bp = _s(), _bp()
    assert set(s.ADAPT_SIGNALS) == {r.value for r in bp.Reason}
    rules = {bp.KEEP: "stable", bp.HALF: "down", bp.FLOOR: "floor"}
    for reason in bp.Reason:
        assert s.ADAPT_SIGNALS[reason.value] == rules[bp.WIDTH_RULE[reason]], reason
    stale = (NOW.replace(year=2026, hour=10)).isoformat()
    cases = [
        (_report(), "NONE", "stable"),
        (_report(groups=[_group(HealthState.LIMITED)]), "LIMITED", "floor"),
        (_report(groups=[_group(HealthState.DEGRADED)]), "DEGRADED", "down"),
        (_report(groups=[_group(HealthState.RESET_DUE)]), "RESET_DUE", "down"),
        (_report(groups=[_group(HealthState.GIVEN_UP)]), "GIVEN_UP", "floor"),
        (_report(loose=[_wait(due=True)]), "RESET_DUE", "down"),
        (_report(loose=[_wait(given_up=True)]), "GIVEN_UP", "floor"),
        (_report(problems=["a run directory could not be read"]), "INCOMPLETE", "floor"),
        (_report(at=stale), "STALE", "floor"),
    ]
    for report, reason, kind in cases:
        decision = bp.decide(6, report, now=NOW)
        assert decision.reason.value == reason, (reason, decision)
        for signal in (decision.reason, decision.reason.value):      # the StrEnum member and its string
            got = step(s.Adaptive("HIGH", 1), signal, top="MAX_SAFE")
            assert got.reason == {"stable": "stable", "down": "down", "floor": "floor"}[kind]
            assert got.level == {"stable": "HIGH", "down": "MODERATE", "floor": "LINEAR"}[kind]
        # for the floor reasons the wave of P3-07 is width 1 and so is the LINEAR profile on lanes with a free lane
        if kind == "floor":
            for lanes in (s.Lanes(12, 0, 10, 0), s.Lanes(1, 0, 1, 0), s.Lanes(12, 3, 10, 0), s.Lanes(3, 3, 10, 0)):
                assert s.profile_width("LINEAR", lanes, 20).width == decision.width == 1


def test_the_sequences_narrow_at_once_widen_slowly_and_never_flap():
    """Catches a step up on fewer than three stable readings or on more, a step down that is not immediate, a
    floor reading that does not go to LINEAR, a count that is not reset by pressure, a flapping signal that
    climbs, a recovery that is faster than three readings a step, and the bound not holding at the top."""
    N, D, L = "NONE", "DEGRADED", "LIMITED"
    assert run([N, N, N, N, N, N]) == [("MODERATE", 1, "stable"), ("MODERATE", 2, "stable"), ("HIGH", 0, "up"),
                                       ("HIGH", 1, "stable"), ("HIGH", 2, "stable"), ("HIGH", 3, "top")]
    assert run([N, N, D, N, N, N]) == [("MODERATE", 1, "stable"), ("MODERATE", 2, "stable"),
                                       ("CONSERVATIVE", 0, "down"), ("CONSERVATIVE", 1, "stable"),
                                       ("CONSERVATIVE", 2, "stable"), ("MODERATE", 0, "up")]
    assert run([N, N, N, D, N, N, N, L, N, N, N, N, N, N]) == [
        ("MODERATE", 1, "stable"), ("MODERATE", 2, "stable"), ("HIGH", 0, "up"), ("MODERATE", 0, "down"),
        ("MODERATE", 1, "stable"), ("MODERATE", 2, "stable"), ("HIGH", 0, "up"), ("LINEAR", 0, "floor"),
        ("LINEAR", 1, "stable"), ("LINEAR", 2, "stable"), ("CONSERVATIVE", 0, "up"),
        ("CONSERVATIVE", 1, "stable"), ("CONSERVATIVE", 2, "stable"), ("MODERATE", 0, "up")]
    assert run([L]) == [("LINEAR", 0, "floor")]
    # from LINEAR the way back to MODERATE is two steps of three readings: six in all, never faster
    back = run([L] + [N] * 6)
    assert [x[0] for x in back] == ["LINEAR"] * 3 + ["CONSERVATIVE"] * 3 + ["MODERATE"]
    assert back[3] == ("CONSERVATIVE", 0, "up") and back[6] == ("MODERATE", 0, "up")
    # a flapping signal never climbs, however long it flaps
    flap = run([N, D] * 20)
    assert {x[0] for x in flap} <= {"MODERATE", "CONSERVATIVE", "LINEAR"} and flap[-1] == ("LINEAR", 0, "down")
    assert [x[2] for x in flap].count("up") == 0
    # one pressure reading in a row of stable ones throws the count away
    assert run([N, N, D, N, N])[-1] == ("CONSERVATIVE", 2, "stable")
    # the bound: at the top nothing goes up and the count stays at up_after
    assert run([N] * 7, top="MODERATE")[-3:] == [("MODERATE", 3, "top")] * 3
    assert run([N] * 4, top="MODERATE", up_after=1) == [("MODERATE", 1, "top")] * 4
    assert run([N] * 2, top="MAX_SAFE", up_after=1) == [("HIGH", 0, "up"), ("MAX_SAFE", 0, "up")]
    # the bound lowered under a running project: brought to the bound whatever the reading says
    s = _s()
    assert run([N, L, D], top="CONSERVATIVE", start=s.Adaptive("MAX_SAFE", 2)) == [
        ("CONSERVATIVE", 0, "above_top"), ("LINEAR", 0, "floor"), ("LINEAR", 0, "down")]
    assert run([N], top="LINEAR", start=s.Adaptive("HIGH", 0)) == [("LINEAR", 0, "above_top")]
    # LINEAR is a floor: pressure at LINEAR is a reading with nothing to narrow, and it is not a change
    got = step(s.Adaptive("LINEAR", 1), D)
    assert (got.level, got.stable, got.reason, got.changed) == ("LINEAR", 0, "down", False)


def test_over_every_short_sequence_the_adaptation_keeps_its_invariants():
    """Catches, over every sequence of six readings from {NONE, DEGRADED, LIMITED, an unreadable one} under every
    top and three values of up_after (61,440 sequences, 368,640 readings): a level off the ladder or above the
    top, a step up on anything but NONE, a step that is not one, two steps up closer than `up_after` readings, a
    count that reaches `up_after` below the top, a `changed` that disagrees with the levels, and a floor that does
    not reach LINEAR."""
    s = _s()
    rank = LADDER.index
    total = 0
    for top, up_after in itertools.product(LADDER, (1, 2, 3)):
        bounds = s.Bounds(top, up_after)
        for seq in itertools.product(("NONE", "DEGRADED", "LIMITED", "bogus"), repeat=6):
            state, last_up = s.adaptive_start(bounds), None
            for index, signal in enumerate(seq):
                got = s.adapt_profile(state, signal, bounds)
                total += 1
                before, after = rank(state.level), rank(got.level)
                assert got.reason in s.ADAPT_REASONS and got.level in LADDER
                assert after <= rank(top) and 0 <= got.stable <= up_after
                assert got.changed == (after != before)
                if after > before:
                    assert signal == "NONE" and after == before + 1 and got.reason == "up"
                    assert last_up is None or index - last_up >= up_after, (top, up_after, seq)
                    last_up = index
                if signal == "LIMITED":
                    assert got.level == "LINEAR" and got.reason == "floor" and got.stable == 0
                if signal in ("DEGRADED", "bogus"):
                    assert after == max(before - 1, 0) and got.stable == 0
                if got.reason == "stable":
                    assert got.stable == state.stable + 1 < up_after
                if got.reason == "top":
                    assert got.level == top and got.stable == up_after
                state = got.state
    assert total == 368640


def test_a_reading_that_cannot_be_read_is_one_step_down_and_never_up_or_an_exception():
    """Catches an unreadable reading that raises, that is read as stable (it would count towards a step up), that
    steps up from the edge (two stable readings in hand), that is read by its spelling (case, padding, newline,
    empty), a `str` subclass read by its lying `__eq__` instead of its text, a non-string, an unbounded string, and
    an enum member that is not read by its value."""
    s = _s()

    class Liar(str):
        def __eq__(self, other):
            return True

        def __hash__(self):
            return hash("NONE")

    class Named(str):
        pass

    class Sneaky(str):
        def __str__(self):
            return "NONE"

    bad = [None, 0, 1, True, 1.5, b"NONE", ["NONE"], ("NONE",), {"NONE": 1}, object(), "", "none", "None",
           " NONE", "NONE ", "NONE\n", "\nNONE", "AUTO_ADAPTIVE", "limited", "MAX_SAFE", "BOGUS", "N" * 100000,
           "NONE\x00", "NONE\u00e9", Liar("BOGUS"), Named("BOGUS"), Sneaky("BOGUS")]
    for signal in bad:
        for level, stable in (("HIGH", 2), ("LINEAR", 2), ("MODERATE", 0)):
            got = step(s.Adaptive(level, stable), signal, top="MAX_SAFE")
            assert got.reason == "unreadable" and got.stable == 0, (repr(signal)[:40], level)
            assert got.level == LADDER[max(LADDER.index(level) - 1, 0)]
            assert got.changed == (level != "LINEAR")
    # a subclass is read by its text: the same text a plain string would be
    assert step(s.Adaptive("HIGH", 0), Named("NONE")).reason == "stable"
    assert step(s.Adaptive("HIGH", 0), Named("LIMITED")).level == "LINEAR"


def test_a_wrong_state_or_bounds_is_refused_and_start_is_moderate_below_the_top():
    """Catches a state or bounds that is read leniently (a bool, a float, a negative, 65, 17, 0, a case spelling,
    AUTO_ADAPTIVE, a `str` subclass, a forged or edited object, a subclass, a dict, a tuple, None), a message that
    is unbounded, and the start profile not being MODERATE or the top when that is narrower."""
    s = _s()
    good_bounds, good_state = s.Bounds("HIGH", 3), s.Adaptive("MODERATE", 0)

    class Str(str):
        pass

    def refused(call):
        with pytest.raises(ValueError) as caught:
            call()
        assert caught.value.reason == "invalid" and len(str(caught.value)) < 200

    for level in ("moderate", "MODERATE ", "AUTO_ADAPTIVE", "", None, 3, Str("MODERATE"), "X" * 100000):
        refused(lambda level=level: s.Adaptive(level, 0))
    for stable in (True, 1.0, "1", None, -1, 65):
        refused(lambda stable=stable: s.Adaptive("MODERATE", stable))
    for top in ("high", "AUTO_ADAPTIVE", "", None, Str("HIGH")):
        refused(lambda top=top: s.Bounds(top, 3))
    for up_after in (0, 17, True, 1.0, "3", None, -1):
        refused(lambda up_after=up_after: s.Bounds("HIGH", up_after))
    edited_state = s.Adaptive("MODERATE", 0)
    object.__setattr__(edited_state, "level", "AUTO_ADAPTIVE")
    edited_bounds = s.Bounds("HIGH", 3)
    object.__setattr__(edited_bounds, "up_after", 0)

    class SubState(s.Adaptive):
        pass

    class SubBounds(s.Bounds):
        pass

    for state in (object.__new__(s.Adaptive), edited_state, SubState("MODERATE", 0), {"level": "MODERATE"},
                  ("MODERATE", 0), None):
        refused(lambda state=state: s.adapt_profile(state, "NONE", good_bounds))
    for bounds in (object.__new__(s.Bounds), edited_bounds, SubBounds("HIGH", 3), {"top": "HIGH"}, ("HIGH", 3),
                   None):
        refused(lambda bounds=bounds: s.adapt_profile(good_state, "NONE", bounds))
        refused(lambda bounds=bounds: s.adaptive_start(bounds))
    # the edges are valid
    assert s.adapt_profile(s.Adaptive("MAX_SAFE", 16), "NONE", s.Bounds("MAX_SAFE", 16)).reason == "top"
    # a count above up_after is a wrong state (the function never produces one); up_after itself is valid at any
    # level: below the top it is "counted, the room came later" (the operator raised the top) and the next stable
    # reading climbs
    for level, stable, top, up_after in (("MODERATE", 4, "HIGH", 3), ("MODERATE", 64, "HIGH", 3), ("HIGH", 4, "HIGH", 3),
                                         ("MAX_SAFE", 17, "MAX_SAFE", 16), ("LINEAR", 2, "HIGH", 1)):
        refused(lambda args=(level, stable, top, up_after): s.adapt_profile(
            s.Adaptive(args[0], args[1]), "NONE", s.Bounds(args[2], args[3])))
    for level, stable, top, up_after in (("LINEAR", 2, "HIGH", 3), ("HIGH", 3, "HIGH", 3), ("MAX_SAFE", 3, "MODERATE", 3),
                                         ("LINEAR", 0, "HIGH", 1), ("LINEAR", 1, "LINEAR", 1), ("LINEAR", 3, "HIGH", 3),
                                         ("MODERATE", 3, "HIGH", 3)):
        assert s.adapt_profile(s.Adaptive(level, stable), "NONE", s.Bounds(top, up_after)).level in LADDER
    # the start: MODERATE, or the top when that is narrower; never above the top
    expected = {"LINEAR": "LINEAR", "CONSERVATIVE": "CONSERVATIVE", "MODERATE": "MODERATE", "HIGH": "MODERATE",
                "MAX_SAFE": "MODERATE"}
    for top, level in expected.items():
        assert s.adaptive_start(s.Bounds(top, 3)) == s.Adaptive(level, 0)


def test_the_adapted_profile_is_only_a_choice_the_width_the_ceiling_and_the_reserve_still_bound_it():
    """Catches an adaptation that widens past the operator's top once the width is taken, past the ceiling or the
    reserve (the width is `profile_width`'s), and a recovery whose widths are not the ladder's: 12 slots, 3 held
    back, ceiling 10, nothing active, 20 ready, MAX_SAFE as the top, from the floor after a LIMITED reading."""
    s = _s()
    lanes = s.Lanes(12, 3, 10, 0)
    state, widths = s.Adaptive("MODERATE", 0), []
    bounds = s.Bounds("MAX_SAFE", 3)
    for signal in ["NONE"] * 9 + ["LIMITED"] + ["NONE"] * 12:
        got = s.adapt_profile(state, signal, bounds)
        state = got.state
        widths.append(s.profile_width(state.level, lanes, 20).width)
    # MODERATE 8; three stable -> HIGH 9; three more -> MAX_SAFE 9 (reserve and ceiling bound it); LIMITED -> 1;
    # then 4 (CONSERVATIVE) after three readings, 8 (MODERATE) after three more, 9 (HIGH) after three more
    assert widths == [8, 8, 9, 9, 9, 9, 9, 9, 9, 1, 1, 1, 4, 4, 4, 8, 8, 8, 9, 9, 9, 9]
    ceiling = max(s.profile_width(p, lanes, 20).width for p in LADDER)
    assert ceiling == 9 and max(widths) == ceiling
    # a lower top keeps the width under that profile's width on the same lanes
    low = s.Bounds("MODERATE", 1)
    state = s.adaptive_start(low)
    for _ in range(10):
        state = s.adapt_profile(state, "NONE", low).state
        assert s.profile_width(state.level, lanes, 20).width <= s.profile_width("MODERATE", lanes, 20).width


def test_a_floor_reading_wins_over_every_other_rule_from_every_level_above_or_below_the_top():
    """Catches a bound that is applied before the reading (a floor reading swallowed by `above_top`: MAX_SAFE under
    a MODERATE top answering MODERATE while P3-07 decided width 1), a floor that is not LINEAR from some level, a
    pressure reading that leaves the level above the top, and a count not reset. Every floor reason, taken from a
    real `decide` over a real report, from every level under every top and both counts."""
    from hoh.health import HealthState
    s, bp = _s(), _bp()
    floor_reports = [
        ("LIMITED", _report(groups=[_group(HealthState.LIMITED)])),
        ("GIVEN_UP", _report(groups=[_group(HealthState.GIVEN_UP)])),
        ("INCOMPLETE", _report(problems=["a run directory could not be read"])),
        ("STALE", _report(at=NOW.replace(hour=10).isoformat())),
    ]
    lanes = s.Lanes(12, 0, 64, 0)
    for reason, report in floor_reports:
        decision = bp.decide(12, report, now=NOW)
        assert decision.reason.value == reason and decision.width == 1
        for level, top in itertools.product(LADDER, LADDER):
            for stable in (0, 2):
                got = step(s.Adaptive(level, stable), decision.reason, top=top)
                assert (got.level, got.stable, got.reason) == ("LINEAR", 0, "floor"), (reason, level, top)
                assert got.changed == (level != "LINEAR")
                assert s.profile_width(got.level, lanes, 20).width == decision.width == 1
    # a halving or an unreadable reading above the top: applied first, then brought under the top, reason kept
    got = step(s.Adaptive("MAX_SAFE", 0), "DEGRADED", top="MODERATE")
    assert (got.level, got.reason) == ("MODERATE", "down")
    assert step(s.Adaptive("MAX_SAFE", 0), "RESET_DUE", top="CONSERVATIVE").level == "CONSERVATIVE"
    assert step(s.Adaptive("MAX_SAFE", 0), "bogus", top="LINEAR").level == "LINEAR"
    assert step(s.Adaptive("HIGH", 0), "DEGRADED", top="HIGH").level == "MODERATE"
    # only a stable reading above the top is `above_top`
    got = step(s.Adaptive("HIGH", 1), "NONE", top="CONSERVATIVE")
    assert (got.level, got.stable, got.reason, got.changed) == ("CONSERVATIVE", 0, "above_top", True)


def test_the_wave_is_never_wider_than_the_decision_the_caller_just_obtained():
    """Catches a composition that launches the adapted profile's width and not P3-07's: on 12 slots, no reserve,
    20 ready, a DEGRADED reading halves a MAX_SAFE wave (12 -> 6) but is one rung for the adaptation (HIGH, width
    10), HIGH 10 -> 5 against MODERATE 8, CONSERVATIVE 4 -> 2 against LINEAR 1. Also: the planned width handed to
    `decide` is the width in force clamped to 1..16 (a profile has 0 to 64), the step up takes effect one wave
    later (the wave being decided was planned at the profile in force), a wave with no free lane is 0, and the
    helpers refuse what is not a width or a decision width."""
    from hoh.health import HealthState
    s, bp = _s(), _bp()
    lanes = s.Lanes(12, 0, 64, 0)
    degraded = _report(groups=[_group(HealthState.DEGRADED)])
    # (profile in force, planned, decision width, adapted level, its width, the wave)
    expected = [("MAX_SAFE", 12, 6, "HIGH", 10, 6), ("HIGH", 10, 5, "MODERATE", 8, 5),
                ("CONSERVATIVE", 4, 2, "LINEAR", 1, 1)]
    for level, planned, decided, adapted, adapted_width, wave in expected:
        in_force = s.profile_width(level, lanes, 20)
        assert (in_force.width, s.planned_for_decision(in_force)) == (planned, planned)
        decision = bp.decide(s.planned_for_decision(in_force), degraded, now=NOW)
        assert decision.width == decided
        got = step(s.Adaptive(level, 0), decision.reason, top="MAX_SAFE")
        assert got.level == adapted and got.reason == "down"
        next_width = s.profile_width(got.level, lanes, 20)
        assert next_width.width == adapted_width
        assert s.wave_width(next_width, decision.width) == wave <= decision.width
    # no pressure: the wave is the width in force; a step up is felt by the next wave, not this one
    moderate = s.profile_width("MODERATE", lanes, 20)
    calm = bp.decide(s.planned_for_decision(moderate), _report(), now=NOW)
    got = step(s.Adaptive("MODERATE", 2), calm.reason, top="MAX_SAFE")
    assert (got.level, got.reason) == ("HIGH", "up")
    assert (moderate.width, calm.width) == (8, 8)
    assert s.wave_width(s.profile_width(got.level, lanes, 20), calm.width) == 8
    # the planned width is clamped to what decide takes, in both directions
    wide = s.profile_width("MAX_SAFE", s.Lanes(64, 0, 64, 0), 64)
    full = s.profile_width("MAX_SAFE", s.Lanes(12, 0, 12, 12), 20)
    assert (wide.width, s.planned_for_decision(wide)) == (64, 16)
    assert (full.width, s.planned_for_decision(full)) == (0, 1)
    assert s.wave_width(full, 1) == 0 and s.wave_width(wide, 16) == 16 and s.wave_width(wide, 5) == 5
    # the limit is the executor's and the decision's
    from hoh import wave
    assert s.MAX_WAVE == wave.MAX_WAVE_WIDTH == 16
    bp.decide(16, _report(), now=NOW)
    with pytest.raises(bp.BackpressureError):
        bp.decide(17, _report(), now=NOW)
    # refusals
    good = s.profile_width("HIGH", lanes, 20)
    forged = object.__new__(s.Width)
    edited = s.profile_width("HIGH", lanes, 20)
    object.__setattr__(edited, "width", 99)
    wider_than_ready = s.profile_width("HIGH", lanes, 20)
    object.__setattr__(wider_than_ready, "ready", 3)
    for bad in (forged, edited, wider_than_ready, None, {"width": 3}, (1, 1, 1, 1, 1)):
        for call in (lambda bad=bad: s.planned_for_decision(bad), lambda bad=bad: s.wave_width(bad, 4)):
            with pytest.raises(ValueError) as caught:
                call()
            assert caught.value.reason == "invalid" and len(str(caught.value)) < 200
    for bad in (0, 17, -1, True, 1.0, "4", None):
        with pytest.raises(ValueError):
            s.wave_width(good, bad)


def test_a_signal_is_judged_by_its_real_type_never_by_a_class_it_claims():
    """Catches `isinstance` on the signal (a spoofed `__class__` passed the guard and `str.__str__` raised
    TypeError: `MagicMock(spec=str)`, an object whose `__class__` is `str`, a proxy), the same spoof in every
    other input of `adapt_profile` (state, bounds, their fields, a width, a decision width) answering with
    anything but a refusal, and a `str` subclass not being read by its text."""
    from unittest.mock import MagicMock
    s = _s()

    class Spoof:
        __class__ = str

    class SpoofState:
        __class__ = s.Adaptive

        level, stable = "HIGH", 0

    class SpoofBounds:
        __class__ = s.Bounds

        top, up_after = "HIGH", 3

    class Named(str):
        pass

    state, bounds = s.Adaptive("MODERATE", 0), s.Bounds("HIGH", 3)
    spoofs = [MagicMock(spec=str), MagicMock(spec=int), MagicMock(), Spoof(), SpoofState(), SpoofBounds()]
    for signal in spoofs:
        got = s.adapt_profile(state, signal, bounds)
        assert (got.level, got.stable, got.reason, got.changed) == ("CONSERVATIVE", 0, "unreadable", True), signal
        got = s.adapt_profile(s.Adaptive("LINEAR", 2), signal, bounds)
        assert (got.level, got.stable, got.reason, got.changed) == ("LINEAR", 0, "unreadable", False)
    # a real str subclass is read by its text
    assert s.adapt_profile(state, Named("NONE"), bounds).reason == "stable"
    assert s.adapt_profile(state, Named("LIMITED"), bounds).level == "LINEAR"

    def refused(call):
        with pytest.raises(ValueError) as caught:
            call()
        assert caught.value.reason == "invalid" and len(str(caught.value)) < 200

    # the other inputs: exact types, whatever class an object claims; no TypeError, no AttributeError
    for fake in (MagicMock(spec=s.Adaptive), SpoofState()):
        refused(lambda fake=fake: s.adapt_profile(fake, "NONE", bounds))
    for fake in (MagicMock(spec=s.Bounds), SpoofBounds()):
        refused(lambda fake=fake: s.adapt_profile(state, "NONE", fake))
        refused(lambda fake=fake: s.adaptive_start(fake))
    for fake in (MagicMock(spec=str), Spoof()):
        refused(lambda fake=fake: s.Adaptive(fake, 0))
        refused(lambda fake=fake: s.Bounds(fake, 3))
    class SpoofInt:
        __class__ = int

    for fake in (MagicMock(spec=int), SpoofInt()):
        refused(lambda fake=fake: s.Adaptive("LINEAR", fake))
        refused(lambda fake=fake: s.Bounds("HIGH", fake))
    good = s.profile_width("HIGH", s.Lanes(12, 0, 64, 0), 20)

    class SpoofWidth:                        # every field valid, the class only claimed
        __class__ = s.Width

        profile, cap, active, ready, width = "HIGH", 4, 0, 20, 4

    for fake in (MagicMock(spec=s.Width), type("W", (), {"__class__": s.Width})(), SpoofWidth()):
        refused(lambda fake=fake: s.wave_width(fake, 4))
        refused(lambda fake=fake: s.planned_for_decision(fake))
    for fake in (MagicMock(spec=int), SpoofInt()):
        refused(lambda fake=fake: s.wave_width(good, fake))


def test_a_state_the_function_produced_survives_the_operator_raising_the_top():
    """Catches the refusal of a state the function itself produced once the operator raises the top (a state at
    the top with the count at `up_after` was refused for every signal, a floor reading included, until the caller
    restarted the count), a floor reading that is not LINEAR from it, and the count above `up_after` being accepted
    (the guard that stays)."""
    s = _s()
    held = s.Adaptive("HIGH", 3)
    produced = step(s.Adaptive("HIGH", 2), "NONE", top="HIGH")
    assert produced.state == held and produced.reason == "top"
    for top in ("MAX_SAFE", "HIGH", "MODERATE"):
        assert step(held, "LIMITED", top=top).level == "LINEAR"
        assert step(held, "DEGRADED", top=top).level == "MODERATE"
        assert step(held, "bogus", top=top).reason == "unreadable"
    # the room came later: the next stable reading climbs, one rung, count 0
    got = step(held, "NONE", top="MAX_SAFE")
    assert (got.level, got.stable, got.reason, got.changed) == ("MAX_SAFE", 0, "up", True)
    # and the same state under a top it is above is brought down by a stable reading
    assert step(held, "NONE", top="MODERATE").reason == "above_top"
    # a count above up_after stays refused (up_after lowered under a running project: restart the count)
    with pytest.raises(ValueError) as caught:
        step(s.Adaptive("HIGH", 3), "NONE", top="HIGH", up_after=2)
    assert caught.value.reason == "invalid"
    assert step(s.Adaptive("HIGH", 0), "NONE", top="HIGH", up_after=2).stable == 1


def test_a_width_is_re_judged_to_the_value_profile_width_gives_for_its_own_fields():
    """Catches a hand-built or edited `Width` that no `profile_width` call could give being believed (width 16 at
    cap 1 and 20 ready: `wave_width` and `planned_for_decision` returned 16), a width above its free lanes, below
    them while ready allows more, a LINEAR cap of 2, and a real `profile_width` result being refused."""
    s = _s()

    def refused(width):
        for call in (lambda: s.wave_width(width, 16), lambda: s.planned_for_decision(width)):
            with pytest.raises(ValueError) as caught:
                call()
            assert caught.value.reason == "invalid" and len(str(caught.value)) < 200

    refused(s.Width("LINEAR", 1, 0, 20, 16))            # the reviewer's case: width above cap - active
    refused(s.Width("HIGH", 4, 1, 20, 4))               # above the free lanes (3)
    refused(s.Width("HIGH", 4, 1, 20, 2))               # below the free lanes with ready allowing 3
    refused(s.Width("HIGH", 4, 6, 20, 1))               # active above cap: the free lanes are 0
    refused(s.Width("HIGH", 4, 0, 2, 4))                # above ready
    refused(s.Width("LINEAR", 2, 0, 20, 2))             # LINEAR is at most one lane
    edited = s.profile_width("HIGH", s.Lanes(12, 0, 64, 0), 20)
    object.__setattr__(edited, "cap", 11)
    refused(edited)
    # every width profile_width gives is accepted, over a grid, and the wave is the bound
    count = 0
    for slots, reserve, ceiling, active, ready in itertools.product(
            (0, 1, 4, 12), (0, 3), (1, 5), (0, 2, 9), (0, 3, 20)):
        for profile in LADDER:
            width = s.profile_width(profile, s.Lanes(slots, reserve, ceiling, active), ready)
            assert s.planned_for_decision(width) == min(max(width.width, 1), 16)
            assert s.wave_width(width, 16) == min(width.width, 16)
            count += 1
    assert count == 4 * 2 * 2 * 3 * 3 * 5
