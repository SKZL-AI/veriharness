"""P3-25 slice 1: the parallelism profiles LINEAR to MAX_SAFE -- how many ready nodes a profile offers to `admit`.

Every test names what it catches. The numbers are literal (40, 70, 90 and 100 percent, 64 lanes, the widths of the
tables), not read from the module, so a changed constant fails a test instead of moving it. Nothing here reads a
file of the host, a clock, the home directory or a state root, starts a process, Herdr or an agent, or touches the
network. The function is not called by anything in the product yet (a later slice wires it): these tests pin its
contract, they are not evidence that a run uses it.
"""
from __future__ import annotations

import dataclasses
import importlib
import itertools

import pytest

NOW = "2026-10-12T09:00:00Z"
LADDER = ("LINEAR", "CONSERVATIVE", "MODERATE", "HIGH", "MAX_SAFE")


def _scheduler():
    """`hoh.scheduler`, imported inside each test: a tree without the new names fails each test with its own
    error (exit 1), not with one collection error (exit 4) that hides the others."""
    return importlib.import_module("hoh.scheduler")


def caps(slots, reserve, ceiling, active, ready):
    """`{profile: (cap, width)}` of the ladder for one set of numbers."""
    s = _scheduler()
    lanes = s.Lanes(slots, reserve, ceiling, active)
    return {p: (w.cap, w.width) for p in LADDER for w in [s.profile_width(p, lanes, ready)]}


def test_the_ladder_vocabulary_is_closed_and_the_values_are_frozen():
    """Catches a sixth profile, a profile moved in the order (the order is the ladder), a percentage that moved,
    AUTO_ADAPTIVE slipped into the fixed ladder, a limit that moved (64 lanes: one call of `admit` judges no
    more than 64 candidates, so `candidates[:width]` always fits it), a mutable `Lanes` or `Width`, and a `Width` whose fields or `as_dict` keys changed."""
    s = _scheduler()
    assert s.PROFILES == LADDER
    assert s.PERCENT == {"CONSERVATIVE": 40, "MODERATE": 70, "HIGH": 90, "MAX_SAFE": 100}
    assert "AUTO_ADAPTIVE" not in s.PROFILES
    assert s.MAX_LANES == 64 and s.MAX_LANES == s.MAX_CANDIDATES
    assert [f.name for f in dataclasses.fields(s.Lanes)] == ["slots", "reserve", "ceiling", "active"]
    assert [f.name for f in dataclasses.fields(s.Width)] == ["profile", "cap", "active", "ready", "width"]
    result = s.profile_width("MODERATE", s.Lanes(12, 3, 10, 1), 20)
    assert result.as_dict() == {"profile": "MODERATE", "cap": 8, "active": 1, "ready": 20, "width": 7}
    for frozen, field in ((s.Lanes(1, 0, 1, 0), "slots"), (result, "width")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(frozen, field, 7)


def test_the_ladder_table_is_pinned_at_two_scales():
    """Catches a percentage that is off by one in either direction (64 slots tells 40, 90 and 100 from their
    neighbours, 50 slots tells 70), a rounding that goes up instead of down (64 * 40 / 100 is 25.6), LINEAR that
    is not exactly one lane, and a profile that does not reach its full share of an unconstrained source."""
    assert caps(64, 0, 64, 0, 64) == {"LINEAR": (1, 1), "CONSERVATIVE": (25, 25), "MODERATE": (44, 44),
                                      "HIGH": (57, 57), "MAX_SAFE": (64, 64)}
    assert caps(50, 0, 64, 0, 64) == {"LINEAR": (1, 1), "CONSERVATIVE": (20, 20), "MODERATE": (35, 35),
                                      "HIGH": (45, 45), "MAX_SAFE": (50, 50)}
    # the plan's own example: 12 safe slots, 3 held back (2 QA, 1 integration), at most 10 active work packages
    assert caps(12, 3, 10, 0, 20) == {"LINEAR": (1, 1), "CONSERVATIVE": (4, 4), "MODERATE": (8, 8),
                                      "HIGH": (9, 9), "MAX_SAFE": (9, 9)}


def test_each_limit_binds_alone_and_the_edge_is_exact():
    """Catches a limit that is not applied (ready, ceiling, reserve, slots, active), one applied twice, an edge
    that is off by one, a width that goes negative when the profile is narrower than the nodes already active, and
    a lane granted where there is no slot."""
    # ready: unused slots are no permission to invent work
    assert caps(8, 0, 8, 0, 2)["MAX_SAFE"] == (8, 2)
    assert caps(8, 0, 8, 0, 0)["MAX_SAFE"] == (8, 0)
    # ceiling: MAX_SAFE is never unlimited
    assert caps(20, 0, 10, 0, 64)["MAX_SAFE"] == (10, 10)
    assert caps(20, 0, 11, 0, 64)["MAX_SAFE"] == (11, 11)
    # reserve: the slots it holds back are not used, to the edge
    assert caps(12, 3, 64, 0, 64)["MAX_SAFE"] == (9, 9)
    assert caps(12, 4, 64, 0, 64)["MAX_SAFE"] == (8, 8)
    assert caps(12, 0, 64, 0, 64)["MAX_SAFE"] == (12, 12)
    # the reserve is not subtracted from a profile that is already below it
    assert caps(12, 3, 64, 0, 64)["CONSERVATIVE"] == (4, 4)
    # the floor: a small source with a large reserve still runs one node
    assert caps(1, 3, 10, 0, 5)["MAX_SAFE"] == (1, 1)
    assert caps(3, 3, 10, 0, 5)["MAX_SAFE"] == (1, 1)
    assert caps(3, 2, 10, 0, 5)["MAX_SAFE"] == (1, 1)
    assert caps(3, 1, 10, 0, 5)["MAX_SAFE"] == (2, 2)
    assert caps(2, 0, 10, 0, 5)["CONSERVATIVE"] == (1, 1)      # 40 percent of 2 slots rounds to 0; a lane is kept
    # no slot, no lane (not even LINEAR, not even with nothing reserved)
    assert caps(0, 0, 10, 0, 5) == {p: (0, 0) for p in LADDER}
    assert caps(0, 5, 10, 0, 5) == {p: (0, 0) for p in LADDER}
    # active: a running node takes a lane; a profile that narrows later starts nothing and ends nothing
    assert caps(20, 0, 10, 3, 64)["MAX_SAFE"] == (10, 7)
    assert caps(20, 0, 10, 9, 64)["MAX_SAFE"] == (10, 1)
    assert caps(20, 0, 10, 10, 64)["MAX_SAFE"] == (10, 0)
    assert caps(20, 0, 10, 11, 64)["MAX_SAFE"] == (10, 0)
    assert caps(20, 0, 10, 64, 64)["LINEAR"] == (1, 0)
    # the node count that is active is reported as judged, and the width is cut by ready last
    result = _scheduler().profile_width("HIGH", _scheduler().Lanes(20, 2, 15, 4), 3)
    assert result.as_dict() == {"profile": "HIGH", "cap": 15, "active": 4, "ready": 3, "width": 3}


def test_the_named_capacity_scenarios_of_the_plan():
    """Catches the four acceptance scenarios of the plan that a width alone can show: C1 one small source with
    MAX_SAFE selected is one lane (no artificial overcommit), C2 eight independent ready runs on eight safe slots
    take the lanes the reserve allows, C3 eight slots with two ready tasks run two, C4 a QA backlog (the caller
    raises the reserve) throttles new development, and C9's premise that MAX_SAFE is never narrower than
    MODERATE on the same numbers."""
    assert caps(1, 2, 10, 0, 1)["MAX_SAFE"] == (1, 1)                                   # C1
    assert caps(8, 2, 10, 0, 8)["MAX_SAFE"] == (6, 6)                                   # C2
    assert caps(8, 0, 10, 0, 2)["MAX_SAFE"] == (8, 2)                                   # C3
    quiet, backlog = caps(10, 0, 10, 0, 64)["MAX_SAFE"], caps(10, 4, 10, 0, 64)["MAX_SAFE"]   # C4
    assert quiet == (10, 10) and backlog == (6, 6)
    assert caps(10, 0, 10, 0, 64)["MAX_SAFE"][1] >= caps(10, 0, 10, 0, 64)["MODERATE"][1]    # C9


def test_the_ladder_never_narrows_going_up_and_no_width_passes_a_bound():
    """Catches, over every combination of small numbers (141120 cases): a profile that is narrower than the one
    below it, a cap above the slots or the ceiling, a lane with no slot or no lane with a slot, a width above
    ready, above the free lanes or below zero, a reserve that is not honoured while a slot is left for it, and a
    start that would take the active count above the cap."""
    s = _scheduler()
    count = 0
    for slots, reserve, ceiling, active, ready in itertools.product(
            range(0, 7), range(0, 8), range(1, 8), range(0, 8), range(0, 9)):
        lanes = s.Lanes(slots, reserve, ceiling, active)
        previous = -1
        for profile in LADDER:
            got = s.profile_width(profile, lanes, ready)
            count += 1
            assert (got.profile, got.active, got.ready) == (profile, active, ready)
            assert got.cap >= previous, (profile, slots, reserve, ceiling)
            previous = got.cap
            assert got.cap <= slots and got.cap <= ceiling
            assert (got.cap == 0) == (slots == 0)
            assert 0 <= got.width <= ready
            assert got.width <= max(got.cap - active, 0)
            assert got.width == min(max(got.cap - active, 0), ready)
            assert active + got.width <= max(got.cap, active)
            if profile == "LINEAR":
                assert got.cap <= 1
            elif slots - reserve >= 1:
                assert got.cap <= slots - reserve
    assert count == 141120


def test_malformed_arguments_are_refused_and_never_read_as_a_default():
    """Catches a profile that is read leniently (lower case, padded, a trailing newline, a `str` subclass that
    compares equal, bytes, None, AUTO_ADAPTIVE which is not a ladder profile), `Lanes` that is forged (made
    without its constructor, edited in place, a subclass, a dict), a number that is a bool, a float, a string, a
    negative or one past the limit, an unbounded value in a message, and an error that is not a
    `ValueError` with a `reason`."""
    s = _scheduler()
    good = s.Lanes(4, 1, 4, 0)

    class Str(str):
        pass

    for bad in ("linear", "Max_Safe", " MAX_SAFE", "MAX_SAFE\n", "MAX_SAFE ", "AUTO_ADAPTIVE", "MODERATE ", "",
                None, 7, b"MAX_SAFE", ["MAX_SAFE"], Str("MAX_SAFE"), "MAX_SAFE" * 20000):
        with pytest.raises(ValueError) as caught:
            s.profile_width(bad, good, 1)
        assert caught.value.reason == "invalid" and len(str(caught.value)) < 200

    forged = object.__new__(s.Lanes)
    edited = s.Lanes(4, 1, 4, 0)
    object.__setattr__(edited, "ceiling", 0)
    huge = s.Lanes(4, 1, 4, 0)
    object.__setattr__(huge, "slots", 10 ** 6)
    wrong_kind = s.Lanes(4, 1, 4, 0)
    object.__setattr__(wrong_kind, "active", True)

    class Sub(s.Lanes):
        pass

    for bad in (forged, edited, huge, wrong_kind, Sub(4, 1, 4, 0), {"slots": 4, "reserve": 1, "ceiling": 4,
                                                                    "active": 0}, (4, 1, 4, 0), None):
        with pytest.raises(ValueError) as caught:
            s.profile_width("MAX_SAFE", bad, 1)
        assert caught.value.reason == "invalid" and len(str(caught.value)) < 200

    for args in ((True, 0, 1, 0), (1.0, 0, 1, 0), ("1", 0, 1, 0), (None, 0, 1, 0), (-1, 0, 1, 0), (65, 0, 1, 0),
                 (1, True, 1, 0), (1, -1, 1, 0), (1, 65, 1, 0), (1, 0, 0, 0), (1, 0, 65, 0), (1, 0, True, 0),
                 (1, 0, 1, -1), (1, 0, 1, 65), (1, 0, 1, False), (1, 0, 1, 1.5)):
        with pytest.raises(ValueError):
            s.Lanes(*args)
    for ready in (-1, 65, True, 1.0, "1", None, 10 ** 9):
        with pytest.raises(ValueError):
            s.profile_width("MAX_SAFE", good, ready)
    assert s.profile_width("MAX_SAFE", s.Lanes(64, 0, 64, 64), 64).width == 0          # the edges are valid
    assert s.profile_width("MAX_SAFE", s.Lanes(64, 64, 64, 0), 64).as_dict() == {
        "profile": "MAX_SAFE", "cap": 1, "active": 0, "ready": 64, "width": 1}


def _capacity_registry(limit):
    cap = importlib.import_module("hoh.capacity")
    entry = cap.Entry(kind="machine", name="host.sessions", limit=limit, unit="sessions", source="measured",
                      as_of=NOW)
    return cap.parse(cap.render([entry]))


def test_a_profile_only_narrows_what_admit_is_offered_and_never_what_it_grants():
    """Catches a profile that widens past the registry or the budget (the mode is not the safe core), a width
    that is not the number of candidates to offer (`candidates[:width]` of the caller's order), a profile that
    does not narrow below what the registry would carry, and the empty offer: with width 0 there is nothing to
    ask and `admit` itself refuses an empty list (with `ValueError`)."""
    s, b = _scheduler(), importlib.import_module("hoh.broker")
    policy = b.Policy(max_age={(k, src): 3600 for k in ("dependency", "machine", "provider")
                               for src in ("declared", "measured")})
    wants = (b.Amount(kind="machine", name="host.sessions", amount=1, unit="sessions"),)
    ready = [s.Candidate(f"n{i}", wants, 1) for i in range(1, 7)]
    registry = _capacity_registry(3)

    def admitted(profile, lanes, budget_left=100):
        width = s.profile_width(profile, lanes, len(ready)).width
        if width == 0:
            return width, ()
        return width, s.admit(registry, policy, NOW, ready[:width], (), 100, budget_left).admitted

    # MAX_SAFE offers all six; the registry carries three: admit, not the profile, decides
    assert admitted("MAX_SAFE", s.Lanes(6, 0, 10, 0)) == (6, ("n1", "n2", "n3"))
    # LINEAR offers one, in the caller's order
    assert admitted("LINEAR", s.Lanes(6, 0, 10, 0)) == (1, ("n1",))
    # MODERATE of four slots is two lanes: narrower than the three the registry would carry
    assert admitted("MODERATE", s.Lanes(4, 0, 10, 0)) == (2, ("n1", "n2"))
    # the budget still binds under the widest profile
    assert admitted("MAX_SAFE", s.Lanes(6, 0, 10, 0), budget_left=1) == (6, ("n1",))
    # nothing free: nothing offered, and `admit` does not take an empty list as "everything"
    assert admitted("MAX_SAFE", s.Lanes(6, 0, 4, 4)) == (0, ())
    with pytest.raises(ValueError):
        s.admit(registry, policy, NOW, ready[:0], (), 100, 100)


def test_the_reserve_is_withheld_from_the_call_and_the_reserved_class_is_a_second_call_with_its_own_pool():
    """Catches the reserve being read as "served first" by the same call (it is withheld from that call's candidates:
    a call with slots 3, reserve 3 and one active offers nothing), the floor exception being lost (a node takes a
    reserved slot when the slots do not exceed the reserve), the two pools (development and reserved class, each by
    its own call) adding up to more than the slots, and LINEAR's optional QA slot beside its one lane. The
    arithmetic is the caller's to apply; this pins that the function gives it."""
    # one call: the reserve is not usable by that call's own candidates, whoever they are
    assert caps(3, 3, 10, 1, 4) == {p: (1, 0) for p in LADDER}
    assert caps(12, 3, 10, 9, 20)["MAX_SAFE"] == (9, 0)
    # the floor exception: slots <= reserve leaves one lane, which is a reserved slot (deliberate; plan C1)
    assert caps(1, 3, 10, 0, 5)["MAX_SAFE"] == (1, 1)
    assert caps(3, 3, 10, 0, 5)["MAX_SAFE"] == (1, 1)
    assert caps(4, 3, 10, 0, 5)["MAX_SAFE"] == (1, 1)               # 4 - 3: not an exception, the same number
    # the reserved class is offered by a second call whose slots are the reserve: 12 slots, 3 reserved
    development = caps(12, 3, 10, 9, 20)["MAX_SAFE"]                 # saturated: nothing more for development
    reserved = caps(3, 0, 3, 0, 5)["MAX_SAFE"]                      # QA, integration, recovery
    assert development == (9, 0) and reserved == (3, 3)
    assert development[0] + reserved[0] == 12
    # LINEAR: one development lane and the plan's optional QA slot beside it
    assert caps(4, 1, 10, 1, 3)["LINEAR"] == (1, 0)
    assert caps(1, 0, 1, 0, 2)["LINEAR"] == (1, 1)
    # the two pools never add up to more than the slots while the reserve is smaller than the slots
    for slots in range(1, 13):
        for reserve in range(0, slots):
            dev = caps(slots, reserve, 64, 0, 64)["MAX_SAFE"][0]
            pool = caps(reserve, 0, max(reserve, 1), 0, 64)["MAX_SAFE"][0] if reserve else 0
            assert dev == slots - reserve and dev + pool == slots, (slots, reserve)
