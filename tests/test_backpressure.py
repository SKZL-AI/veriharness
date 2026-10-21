"""P3-07, first increment: how wide a wave may be while a provider is under pressure.

`hoh.backpressure.decide` takes the width a planner wants and the `HealthReport` that
`hoh.health.read_health` built from the parked runs of a root, and gives back a width that is
never larger, with its reason; `BackpressureController.admit_width` writes the decision down
before it returns it. Reports are built two ways on purpose: from **real parked runs** under
`tmp_path` through the product's own `RunStore`, `RunState` and `dispatchrecovery.append_record`
and read back with `read_health` (so the decision is measured on what the reader really
produces), and by hand where a real root cannot make the case (an odd name, a stale moment).
Nothing starts a process, Herdr or an agent, no test sleeps, nothing writes outside `tmp_path`,
nothing is removed, and `now` and the clock are always injected. Every number below is a
literal on purpose: a table imported from the module would agree with a mutated module.
"""
from __future__ import annotations

import ast
import errno
import json
import os
import stat
import subprocess
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
STAMP = "2026-10-12T12:00:00Z"
SOON = (NOW + timedelta(hours=1)).isoformat()
PAST = (NOW - timedelta(hours=1)).isoformat()
HALF_OF = [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8]      # planned 1..16, halved, rounded up


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a test started a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def _bp():
    from hoh import backpressure
    return backpressure


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _group(provider="claude", account="acct-a", state=...):
    from hoh.health import HealthState, ProviderHealth
    return ProviderHealth(provider=provider, account_handle=account,
                          state=HealthState.LIMITED if state is ... else state, not_before=None,
                          runs=("r",), failures=(("USAGE_LIMIT", 1),))


def _wait(*, evidence=None, due=False, given_up=False):
    from hoh.failures import NormalizedFailure
    from hoh.health import Evidence, Wait
    from hoh.telemetry import NOT_AVAILABLE
    return Wait(run_id="u", failure=NormalizedFailure.USAGE_LIMIT,
                evidence=Evidence.LIMIT if evidence is None else evidence, retry_at=None, due=due,
                provider=NOT_AVAILABLE, account_handle=NOT_AVAILABLE, model=NOT_AVAILABLE,
                unattributed="no receipt describes this park", given_up=given_up)


def _report(groups=(), loose=(), problems=(), at=...):
    from hoh.health import HealthReport
    return HealthReport(at=NOW.isoformat() if at is ... else at, providers=tuple(groups),
                        unattributed=tuple(loose), problems=tuple(problems), runs_seen=0)


def _raw(**parts):
    """A `HealthReport` with exactly these parts, whatever their types (`_report` coerces)."""
    from hoh.health import HealthReport
    base = dict(at=NOW.isoformat(), providers=(), unattributed=(), problems=(), runs_seen=0)
    base.update(parts)
    return HealthReport(**base)


def _park(root: Path, run_id: str, *, failure=None, retry=SOON, provider="claude",
          account="acct-a", attempts=0, receipt=True):
    """A run parked on `failure` with the receipt the controller writes (the builders of
    `tests/test_provider_health.py`, reduced to what this file needs)."""
    from hoh import dispatchrecovery
    from hoh.contracts import Condition, RunState, Stage
    from hoh.dispatchrecovery import (
        NO_MASTER, DispatchProvenance, ParkBinding, RoutingDecision, park_id)
    from hoh.failures import NormalizedFailure
    from hoh.store import RunStore

    failure = NormalizedFailure.USAGE_LIMIT if failure is None else failure
    st = RunState(run_id=run_id, repo_path="/arena/repo", project_name="demo",
                  spec_path="/arena/spec.md", spec_digest="s", policy_digest="p",
                  profile_digest="h", stage=Stage.DEVELOPING, condition=Condition.BLOCKED,
                  iteration=1)
    st.blocked_kind = failure.value.lower()
    st.retry_after = retry
    st.blocked_reason = "parked"
    st.resume_attempts = attempts
    store = RunStore(root, run_id)
    store.write_state(st)
    binding = ParkBinding(programme="NONE", program_epoch=0, run_id=run_id, iteration=st.iteration,
                          attempt=st.attempt, stage=st.stage.value, candidate="NONE",
                          master=NO_MASTER)
    if not receipt:                       # the product writes none for a park with no stated time
        return store
    dispatchrecovery.append_record(store.dir, RoutingDecision(
        park_id=park_id(binding, failure, retry), reason=failure.value, binding=binding,
        provenance=DispatchProvenance(
            provider=provider, model="m-1", account_handle=account, role="developer",
            capability_requirements=["read_repo", "write_worktree", "run_shell"]),
        retry_at=retry))
    return store


def _real(root: Path):
    from hoh.health import read_health
    return read_health(root, now=NOW)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "runs"
    r.mkdir()
    return r


class _Unhashable:
    def __hash__(self):
        raise RuntimeError("no hash")

    def __eq__(self, other):
        return False


class _RaisingTuple(tuple):
    def __iter__(self):
        raise RuntimeError("no iteration")


def _decision(bp, planned=4, reason=None, provider="", account="", groups=0, loose=0, problems=0,
              report_at=STAMP):
    reason = bp.Reason.NONE if reason is None else reason
    return bp.WidthDecision(planned, bp.narrowed(reason, planned), reason, provider=provider,
                            account_handle=account, groups=groups, unattributed=loose,
                            problems=problems, report_at=report_at)


def _record_bytes(**over):
    """A valid LIMITED record as a dict of its fourteen fields, with `over` applied."""
    body = {"schema": "hoh-backpressure-decision/2", "decision_id": "w1",
            "decided_at": STAMP, "report_at": STAMP, "planned_width": 4, "width": 1,
            "reason": "LIMITED", "provider": "claude", "account_handle": "acct-a", "groups": 1,
            "unattributed": 0, "problems": 0, "scope": None, "ignored": 0}
    body.update(over)
    return body


def _dump(body) -> bytes:
    return (json.dumps(body, sort_keys=True, indent=1) + "\n").encode()


# --------------------------------------------------------------------------- #
# R1 -- the reading the product really builds, and what absence does
# --------------------------------------------------------------------------- #

def test_no_evidence_keeps_the_width_and_real_parked_runs_narrow_it_by_their_state(root):
    """An empty root is `NONE` at every planned width (absence is no evidence, never "healthy" and
    never a reason to change anything); runs really parked on a limit, an availability failure, a
    reset that has passed and a park the loop gave up on are read by `read_health` and narrow to
    1, half, half and 1; a limit park with no stated time (no receipt: unattributed) and one with
    a garbage time read RESET_DUE (half); a runs root that does not exist reads as a whole `NONE`
    (the caller owns the root). Kills FLOOR that keeps the width (M1), HALF rounded down (M2), a state with no
    narrowing (M3), unattributed waits not walked (M8)."""
    bp = _bp()
    for planned in range(1, 17):
        got = bp.decide(planned, _real(root), now=NOW)
        assert (got.width, got.reason, got.provider, got.groups, got.unattributed, got.problems) \
            == (planned, bp.Reason.NONE, "", 0, 0, 0), planned
        assert got.report_at == STAMP and not got.narrows

    from hoh import quota
    from hoh.failures import NormalizedFailure as N
    # A limit park with no stated retry time (no receipt can name one: unattributed), or one that is
    # no time, is "due" for `quota.is_due`: it reads RESET_DUE (half), not the floor (spec; review A, M2).
    cases = [("a-limit", dict(failure=N.USAGE_LIMIT), bp.Reason.LIMITED, 1),
             ("e-nostamp", dict(failure=N.USAGE_LIMIT, retry=None, receipt=False),
              bp.Reason.RESET_DUE, 3),
             ("f-garbage", dict(failure=N.USAGE_LIMIT, retry="soon"), bp.Reason.RESET_DUE, 3),
             ("b-degraded", dict(failure=N.PROVIDER_UNAVAILABLE), bp.Reason.DEGRADED, 3),
             ("c-due", dict(failure=N.RATE_LIMIT, retry=PAST), bp.Reason.RESET_DUE, 3),
             ("d-given-up", dict(failure=N.USAGE_LIMIT, attempts=quota.MAX_RESUME_ATTEMPTS),
              bp.Reason.GIVEN_UP, 1)]
    for run_id, kwargs, reason, width in cases:
        sub = root / run_id
        sub.mkdir()
        _park(sub, run_id, **kwargs)
        got = bp.decide(5, _real(sub), now=NOW)
        assert (got.reason, got.width, got.planned) == (reason, width, 5), run_id
        if run_id == "e-nostamp":
            assert (got.provider, got.groups, got.unattributed) == ("", 0, 1), run_id
        else:
            assert (got.provider, got.account_handle, got.groups) == ("claude", "acct-a", 1), run_id
        assert got.narrows

    # A runs root that does not exist reads as a whole, fresh "nothing parked": the caller owns
    # the root (spec L11, review A M3). Pinned so that a change of `health` is seen here.
    nowhere = _real(root / "no-such-root")
    assert nowhere.complete and nowhere.runs_seen == 0 and nowhere.providers == ()
    assert bp.decide(8, nowhere, now=NOW).reason is bp.Reason.NONE


# --------------------------------------------------------------------------- #
# R2 -- the table, at every planned width, and the one-way guarantee
# --------------------------------------------------------------------------- #

def test_the_width_table_is_pinned_at_every_planned_width_and_never_widens():
    """Planned 1..16 under every reason, with literal expected numbers (floor 1, half rounded up,
    keep); the tables are complete over `HealthState` and `Reason`; over a grid of every state
    and every mix of two, the result is from 1 to the planned width, equals it only without
    pressure (or at width 1), and is strictly smaller for any pressure at a planned width of 2
    or more. Kills FLOOR that keeps the width (M1), HALF rounded down (M2), a state with no
    narrowing (M3)."""
    from hoh.health import HealthState
    bp = _bp()
    assert set(bp.STATE_REASON) == set(HealthState)
    assert set(bp.WIDTH_RULE) == set(bp.Reason)
    assert {s: r.value for s, r in bp.STATE_REASON.items()} == {
        HealthState.LIMITED: "LIMITED", HealthState.DEGRADED: "DEGRADED",
        HealthState.RESET_DUE: "RESET_DUE", HealthState.GIVEN_UP: "GIVEN_UP"}
    assert {r.value: v for r, v in bp.WIDTH_RULE.items()} == {
        "NONE": "KEEP", "LIMITED": "FLOOR", "GIVEN_UP": "FLOOR", "INCOMPLETE": "FLOOR",
        "STALE": "FLOOR", "DEGRADED": "HALF", "RESET_DUE": "HALF"}
    for planned in range(1, 17):
        assert bp.narrowed(bp.Reason.NONE, planned) == planned
        for floor in ("LIMITED", "GIVEN_UP", "INCOMPLETE", "STALE"):
            assert bp.narrowed(bp.Reason(floor), planned) == 1, (floor, planned)
        for half in ("DEGRADED", "RESET_DUE"):
            assert bp.narrowed(bp.Reason(half), planned) == HALF_OF[planned - 1], (half, planned)

    states = list(HealthState)
    mixes = [(a,) for a in states] + [(a, b) for a in states for b in states]
    for planned in range(1, 17):
        for mix in mixes:
            groups = [_group(provider=f"p{i}", state=s) for i, s in enumerate(mix)]
            got = bp.decide(planned, _report(groups), now=NOW)
            assert 1 <= got.width <= planned, (planned, mix)
            assert got.planned == planned and got.reason is not bp.Reason.NONE
            if planned >= 2:
                assert got.width < planned, (planned, mix)
    for planned in range(1, 17):
        assert bp.decide(planned, _report(), now=NOW).width == planned


# --------------------------------------------------------------------------- #
# R3 -- several groups: the most restrictive decides and is named
# --------------------------------------------------------------------------- #

def test_the_most_restrictive_group_decides_and_names_its_provider():
    """A DEGRADED and a RESET_DUE group halve; add a LIMITED one (named second) and the width is
    1 and the reason names that provider; two LIMITED groups are named in provider, account
    order, whatever the order they arrive in; GIVEN_UP outranks DEGRADED in the name and a floor
    outranks a half in the width. Kills `max` for `min` in the choice (M7), the naming order of LIMITED / GIVEN_UP
    (M21) and DEGRADED / RESET_DUE (M22), a name taken from
    the first group, a provider echoed for the wrong group."""
    from hoh.health import HealthState as S
    bp = _bp()
    half = bp.decide(6, _report([_group("b", "x", S.RESET_DUE), _group("a", "y", S.DEGRADED)]),
                     now=NOW)
    assert (half.width, half.reason, half.provider, half.account_handle) \
        == (3, bp.Reason.DEGRADED, "a", "y")
    limited = bp.decide(6, _report([_group("a", "y", S.DEGRADED), _group("z", "k", S.LIMITED),
                                    _group("b", "x", S.RESET_DUE)]), now=NOW)
    assert (limited.width, limited.reason, limited.provider, limited.account_handle,
            limited.groups) == (1, bp.Reason.LIMITED, "z", "k", 3)
    for order in ([_group("m", "2", S.LIMITED), _group("m", "1", S.LIMITED),
                   _group("c", "9", S.LIMITED)],
                  [_group("c", "9", S.LIMITED), _group("m", "1", S.LIMITED),
                   _group("m", "2", S.LIMITED)]):
        named = bp.decide(8, _report(order), now=NOW)
        assert (named.provider, named.account_handle, named.width) == ("c", "9", 1)
    gu = bp.decide(8, _report([_group("a", "1", S.DEGRADED), _group("b", "1", S.GIVEN_UP)]),
                   now=NOW)
    assert (gu.reason, gu.width, gu.provider) == (bp.Reason.GIVEN_UP, 1, "b")
    # The naming order itself, each pair with the provider that sorts first holding the lower rank.
    lg = bp.decide(8, _report([_group("a", "1", S.GIVEN_UP), _group("z", "1", S.LIMITED)]), now=NOW)
    assert (lg.reason, lg.provider, lg.width) == (bp.Reason.LIMITED, "z", 1)
    dr = bp.decide(8, _report([_group("a", "1", S.RESET_DUE), _group("z", "1", S.DEGRADED)]), now=NOW)
    assert (dr.reason, dr.provider, dr.width) == (bp.Reason.DEGRADED, "z", 4)
    gd = bp.decide(8, _report([_group("a", "1", S.DEGRADED), _group("z", "1", S.GIVEN_UP)]), now=NOW)
    assert (gd.reason, gd.provider) == (bp.Reason.GIVEN_UP, "z")


# --------------------------------------------------------------------------- #
# R4 -- waits nobody can name, and a reading with problems
# --------------------------------------------------------------------------- #

def test_unattributed_waits_and_a_reading_with_problems_narrow_without_naming_a_provider(root):
    """A pending limit with no named provider narrows to 1 and names none; a pending availability
    wait halves; a due one halves; a given-up one is the floor with its own reason. A reading
    with one problem (a real run directory whose state is damaged, read by `read_health`) is
    INCOMPLETE at the floor even with no evidence at all, and even beside a group that would only
    halve. Kills an ignored `unattributed` (M8), an ignored `problems` (M4)."""
    from hoh.health import Evidence
    bp = _bp()
    got = bp.decide(8, _report(loose=[_wait()]), now=NOW)
    assert (got.reason, got.width, got.provider, got.account_handle, got.unattributed) \
        == (bp.Reason.LIMITED, 1, "", "", 1)
    got = bp.decide(8, _report(loose=[_wait(evidence=Evidence.AVAILABILITY)]), now=NOW)
    assert (got.reason, got.width) == (bp.Reason.DEGRADED, 4)
    got = bp.decide(8, _report(loose=[_wait(due=True)]), now=NOW)
    assert (got.reason, got.width) == (bp.Reason.RESET_DUE, 4)
    got = bp.decide(8, _report(loose=[_wait(given_up=True, due=True)]), now=NOW)
    assert (got.reason, got.width) == (bp.Reason.GIVEN_UP, 1)

    sub = root / "broken"
    sub.mkdir()
    (sub / "state.json").write_text("{not json")
    real = _real(root)
    assert real.problems and real.providers == ()
    got = bp.decide(8, real, now=NOW)
    assert (got.reason, got.width, got.problems, got.provider) == (bp.Reason.INCOMPLETE, 1, 1, "")
    from hoh.health import HealthState as S
    mixed = bp.decide(8, _report([_group("a", "1", S.DEGRADED)], problems=[("r", "damaged")]),
                      now=NOW)
    assert (mixed.reason, mixed.width, mixed.groups, mixed.problems) \
        == (bp.Reason.INCOMPLETE, 1, 1, 1)
    over = bp.decide(8, _report([_group("a", "1", S.LIMITED)], problems=[("r", "damaged")]), now=NOW)
    assert (over.reason, over.provider, over.width) == (bp.Reason.INCOMPLETE, "", 1)


# --------------------------------------------------------------------------- #
# R5 -- the age of the reading
# --------------------------------------------------------------------------- #

def test_a_stale_future_or_momentless_reading_is_the_floor_with_its_own_reason(root):
    """60 whole seconds old is fresh and 61 is STALE; a reading from the future (one second) is
    STALE and the same second is fresh; the microsecond part is not counted on either side; a
    reading whose moment is empty, text, naive, an integer or None is STALE with no moment;
    a stale reading that carries a limit is named STALE. Kills the bound moved (M18) or
    inclusive (M5), a future reading accepted (M6), a stale reading that keeps the width (M15),
    an uncaught `OverflowError` (M23), a short year (M25)."""
    bp = _bp()

    def at(delta, micro=0):
        return (NOW + delta).replace(microsecond=micro).isoformat()

    fresh = bp.decide(4, _report(at=at(timedelta(seconds=-60))), now=NOW)
    assert (fresh.reason, fresh.width) == (bp.Reason.NONE, 4)
    old = bp.decide(4, _report(at=at(timedelta(seconds=-61))), now=NOW)
    assert (old.reason, old.width, old.report_at) == (bp.Reason.STALE, 1, "2026-10-12T11:58:59Z")
    future = bp.decide(4, _report(at=at(timedelta(seconds=1))), now=NOW)
    assert (future.reason, future.width) == (bp.Reason.STALE, 1)
    assert bp.decide(4, _report(at=at(timedelta(0))), now=NOW).reason is bp.Reason.NONE
    assert bp.decide(4, _report(at=at(timedelta(0), 999999)), now=NOW).reason is bp.Reason.NONE
    assert bp.decide(4, _report(at=at(timedelta(seconds=-60), 999999)), now=NOW).reason \
        is bp.Reason.NONE
    assert bp.decide(4, _report(at=at(timedelta(seconds=-61), 1)), now=NOW).reason \
        is bp.Reason.STALE
    now_micro = NOW.replace(microsecond=900000)
    assert bp.decide(4, _report(at=at(timedelta(0), 950000)), now=now_micro).reason \
        is bp.Reason.NONE
    for odd in ("", "yesterday", "2026-10-12T12:00:00", "2026-13-40T99:00:00Z", 1760270400, None):
        got = bp.decide(4, _report(at=odd), now=NOW)
        assert (got.reason, got.width, got.report_at) == (bp.Reason.STALE, 1, None), repr(odd)
    from hoh.health import HealthState as S
    named = bp.decide(4, _report([_group(state=S.LIMITED)], at=at(timedelta(hours=-1))), now=NOW)
    assert (named.reason, named.provider, named.width) == (bp.Reason.STALE, "", 1)
    other_zone = NOW.astimezone(timezone(timedelta(hours=5)))
    assert bp.decide(4, _report(at=other_zone.isoformat()), now=NOW).reason is bp.Reason.NONE

    # Moments with no UTC form (the year 1 east of UTC, the year 9999 west of it) are no moment:
    # STALE, never an OverflowError (review A B1 / B B1).
    for extreme in ("0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00",
                    "0001-01-01T00:00:00+14:00"):
        got = bp.decide(8, _report(at=extreme), now=NOW)
        assert (got.reason, got.width, got.report_at) == (bp.Reason.STALE, 1, None), extreme
    # ... also through the reader that really produces them
    far = datetime(9999, 12, 31, 23, 0, tzinfo=timezone(timedelta(hours=-5)))
    from hoh.health import read_health
    real = read_health(root, now=far)
    assert real.at == "9999-12-31T23:00:00-05:00"
    got = bp.decide(8, real, now=NOW)
    assert (got.reason, got.width, got.report_at) == (bp.Reason.STALE, 1, None)
    # A year below 1000 is a moment, four digits in the record (review B, M3): old, so STALE,
    # and the decision's own record reads back.
    ancient = bp.decide(8, _report(at="0005-01-01T00:00:00+00:00"), now=NOW)
    assert (ancient.reason, ancient.report_at) == (bp.Reason.STALE, "0005-01-01T00:00:00Z")


# --------------------------------------------------------------------------- #
# R6 -- odd arguments refuse; an odd reading narrows
# --------------------------------------------------------------------------- #

def test_an_odd_argument_is_refused_and_an_odd_reading_narrows_to_the_floor_never_raises():
    """A planned width that is 0, -1, 17, `True`, 2.0, `"3"` or None, a reading that is not a
    `HealthReport` (None, a dict, a namespace with the right fields) and a `now` that is naive,
    text or None are `BackpressureError`; 1 and 16 are accepted. Inside a reading, a provider
    with a newline, of 129 characters (128 is a name), empty or not text, a state that is not a
    `HealthState`, parts that are lists, a wait that is a dict or has no evidence class are
    INCOMPLETE at the floor and raise nothing. Kills `bool` as a width (M13), `$` for a full
    match on names (M12), a reading whose type is not checked (M16), `problems` that need not
    be a tuple (M19), an uncaught `OverflowError` of `now` (M24), a walk that catches only three
    exception types (M26)."""
    from hoh.health import HealthReport, HealthState as S
    bp = _bp()
    for bad in (0, -1, 17, True, 2.0, "3", None, 16.0, 2**31):
        with pytest.raises(bp.BackpressureError):
            bp.decide(bad, _report(), now=NOW)
    assert bp.decide(1, _report(), now=NOW).width == 1
    assert bp.decide(16, _report(), now=NOW).width == 16
    for bad in (None, {"at": NOW.isoformat()}, [], "report",
                types.SimpleNamespace(at=NOW.isoformat(), providers=(), unattributed=(),
                                      problems=(), runs_seen=0)):
        with pytest.raises(bp.BackpressureError):
            bp.decide(4, bad, now=NOW)
    for bad in (None, STAMP, NOW.replace(tzinfo=None), 0,
                datetime(9999, 12, 31, 23, 0, tzinfo=timezone(timedelta(hours=-5))),
                datetime(1, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=5)))):
        with pytest.raises(bp.BackpressureError):
            bp.decide(4, _report(), now=bad)

    odd = [
        _report([_group(provider="claude\n")]), _report([_group(provider="x" * 129)]),
        _report([_group(provider="")]), _report([_group(provider=7)]),
        _report([_group(account="a\x00b")]), _report([_group(account="é")]),
        _report([_group(account="")]), _report([_group(provider="a\ud800b")]),
        _report([_group(provider="\x00")]), _report([_group(state="BOGUS")]),
        _report([_group(state=None)]), _report(["claude"]),
        _report(loose=[{"given_up": True}]), _report(loose=[_wait(evidence="LIMIT?")]),
        HealthReport(at=NOW.isoformat(), providers=[_group()], unattributed=(), problems=(),
                     runs_seen=0),
        HealthReport(at=NOW.isoformat(), providers=(), unattributed=None, problems=(),
                     runs_seen=0),
        # `problems` is not a tuple: falsy or not, the part is not what `health` builds
        _raw(problems=None), _raw(problems=0), _raw(problems=""),
        _raw(problems=[("r", "damaged")]), _raw(problems="damaged"),
        # anything the walk meets, not only TypeError / KeyError / AttributeError
        _report([_group(state=_Unhashable())]),
        _raw(providers=_RaisingTuple()), _raw(unattributed=_RaisingTuple()),
    ]
    for report in odd:
        got = bp.decide(8, report, now=NOW)
        assert (got.reason, got.width, got.provider) == (bp.Reason.INCOMPLETE, 1, ""), report
    assert bp.decide(8, _report([_group(provider="x" * 128, account="y" * 128)]),
                     now=NOW).provider == "x" * 128
    assert bp.decide(8, _report([_group(state=S.LIMITED)]), now=NOW).width == 1


# --------------------------------------------------------------------------- #
# R7 -- written before it is returned
# --------------------------------------------------------------------------- #

def test_the_decision_is_written_before_it_is_returned_and_an_unwritten_one_is_not_returned(
        tmp_path, monkeypatch):
    """`admit_width` leaves `backpressure-<id>.json` (mode 0600, the record reading back equal to
    the returned decision, `decided_at` the clock's moment) and nothing else in the directory;
    a used id is refused with the first file byte-identical; ids of 65 characters, `a b`, `a/b`,
    `../x`, `.a`, an id with a newline, `""`, None, 1 are refused and 64 is accepted; a missing
    directory, a file, a symbolic link to a directory are refused; a write that raises
    (`ENOSPC`, `EIO`) is `BackpressureError` and no file is left; an odd planned width, reading
    or clock writes nothing. Kills a swallowed write error (M9), `durable_write` for
    `durable_create` with no exists check (M10), `bool` as a width (M13), a short year (M25),
    a record written some other way (M27)."""
    import hoh.backpressure as bp
    from hoh.health import HealthState as S
    d = tmp_path / "records"
    d.mkdir()
    ctl = bp.BackpressureController(d, clock=lambda: STAMP)
    got = ctl.admit_width("w1", 6, _report([_group(state=S.DEGRADED)]))
    assert (got.width, got.reason, got.provider) == (3, bp.Reason.DEGRADED, "claude")
    path = d / "backpressure-w1.json"
    assert [p.name for p in d.iterdir()] == ["backpressure-w1.json"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    rec = bp.read_decision(path)
    assert rec == bp.BackpressureRecord("w1", STAMP, got)
    assert rec.to_bytes() == path.read_bytes()
    before = path.read_bytes()
    with pytest.raises(bp.BackpressureError):
        ctl.admit_width("w1", 2, _report())
    assert path.read_bytes() == before

    assert ctl.admit_width("A" + "b" * 63, 3, _report()).width == 3
    for bad in ("b" * 65, "a b", "a/b", "../x", ".a", "a\n", "a\x00", "\ud800", "", None, 1):
        with pytest.raises(bp.BackpressureError):
            ctl.admit_width(bad, 3, _report())
    for kwargs in (dict(planned_width=0, report=_report()), dict(planned_width=3, report=None),
                   dict(planned_width=True, report=_report())):
        with pytest.raises(bp.BackpressureError):
            ctl.admit_width("w2", **kwargs)
    with pytest.raises(bp.BackpressureError):
        bp.BackpressureController(d, clock=lambda: "now").admit_width("w2", 3, _report())
    with pytest.raises(bp.BackpressureError):
        bp.BackpressureController(d, clock=lambda: 5).admit_width("w2", 3, _report())

    def broken_clock():
        raise RuntimeError("no clock")
    with pytest.raises(bp.BackpressureError):
        bp.BackpressureController(d, clock=broken_clock).admit_width("w2", 3, _report())
    with pytest.raises(bp.BackpressureError):
        bp.BackpressureController(d, clock="x")
    for bad in (None, 5, b"records"):
        with pytest.raises(bp.BackpressureError):
            bp.BackpressureController(bad)
    assert sorted(p.name for p in d.iterdir()) == sorted(
        ["backpressure-w1.json", "backpressure-" + "A" + "b" * 63 + ".json"])

    for number in (errno.ENOSPC, errno.EIO):
        def boom(*a, _n=number, **k):
            raise OSError(_n, os.strerror(_n))
        with monkeypatch.context() as m:
            m.setattr(bp.durable_io, "durable_create", boom)
            with pytest.raises(bp.BackpressureError):
                ctl.admit_width("w3", 4, _report([_group()]))
        assert not (d / "backpressure-w3.json").exists()
    assert ctl.admit_width("w3", 4, _report([_group()])).width == 1

    # A year below 1000 (a reading, and a clock) is written with four digits and reads back.
    old_clock = bp.BackpressureController(d, clock=lambda: "0005-01-01T00:00:00Z")
    got = old_clock.admit_width("w4", 6, _report(at="0005-01-01T00:00:00+00:00"))
    assert (got.reason, got.report_at) == (bp.Reason.NONE, "0005-01-01T00:00:00Z")
    rec4 = bp.read_decision(d / "backpressure-w4.json")
    assert rec4 == bp.BackpressureRecord("w4", "0005-01-01T00:00:00Z", got)

    # A failure *after* the link (the directory sync) is an error and returns nothing, but the
    # file is there and whole: the id is used (review A, M4).
    real_create = bp.durable_io.durable_create

    def create_then_fail(*a, **k):
        real_create(*a, **k)
        raise OSError(errno.EIO, "directory sync failed")
    with monkeypatch.context() as m:
        m.setattr(bp.durable_io, "durable_create", create_then_fail)
        with pytest.raises(bp.BackpressureError):
            ctl.admit_width("w5", 4, _report())
    rec5 = bp.read_decision(d / "backpressure-w5.json")
    assert rec5 is not None and rec5.decision.width == 4
    with pytest.raises(bp.BackpressureError):
        ctl.admit_width("w5", 4, _report())

    other = tmp_path / "elsewhere"
    other.mkdir()
    (tmp_path / "ln").symlink_to(other)
    (tmp_path / "file").write_text("x")
    for target in (tmp_path / "missing", tmp_path / "file", tmp_path / "ln", str(d) + "\x00x"):
        with pytest.raises(bp.BackpressureError):
            bp.BackpressureController(target, clock=lambda: STAMP).admit_width("w1", 3, _report())
    assert list(other.iterdir()) == []


# --------------------------------------------------------------------------- #
# R8 -- the record is a strict document
# --------------------------------------------------------------------------- #

def test_the_record_round_trips_and_parse_refuses_everything_that_is_not_one():
    """Every decision of the module reads back equal and re-renders to the same bytes; odd
    documents refuse (a list, `null`, a repeated key, `NaN`, UTF-16, a missing or extra field, a
    wrong schema, a lower-case or padded reason, `bool` or float as a width, text as a count, a
    non-ASCII name, a trailing newline in an id, a width above the plan, 0, and a width that is
    not what the reason gives: LIMITED with 2, DEGRADED with 3 of 4, NONE below the plan);
    consistency rules (a provider without an account, a provider on STALE, evidence with no group
    or wait, NONE with a count, a missing moment on anything but STALE); the bounds: planned 16
    and 17, counts 2**31 and 2**31 + 1, names 128 and 129, exactly 4096 bytes and one more.
    Kills the width/reason check dropped (M11), a bound moved (M17), the repeated-key
    guard dropped (M20)."""
    bp = _bp()
    dec = [
        _decision(bp, 4), _decision(bp, 16, bp.Reason.LIMITED, "claude", "acct-a", groups=2),
        _decision(bp, 9, bp.Reason.DEGRADED, "p", "a", groups=1, loose=2),
        _decision(bp, 5, bp.Reason.RESET_DUE, loose=1), _decision(bp, 1, bp.Reason.GIVEN_UP, groups=1,
                                                                   provider="p", account="a"),
        _decision(bp, 7, bp.Reason.INCOMPLETE, groups=1, problems=3),
        _decision(bp, 3, bp.Reason.STALE, report_at=None),
    ]
    for d in dec:
        rec = bp.BackpressureRecord("w-1.x_y", STAMP, d)
        assert bp.parse(rec.to_bytes()) == rec
        assert bp.parse(rec.to_bytes()).to_bytes() == rec.to_bytes()
    assert bp.parse(_dump(_record_bytes())).decision.width == 1

    good = _record_bytes()
    bad_docs = [
        b"[]", b"null", b"{}", b"", b"\xff\xfe{", json.dumps(good).encode("utf-16"),
        b'{"schema": "a", "schema": "b"}', _dump(good).replace(b'"groups": 1', b'"groups": NaN'),
        _dump(good)[:-3],
        _dump({k: v for k, v in good.items() if k != "problems"}),
        _dump({**good, "extra": 1}),
        _dump(_record_bytes(schema="hoh-backpressure-decision/1")),
        _dump(_record_bytes(schema="hoh-backpressure-decision/3")),
        _dump(_record_bytes(reason="limited")), _dump(_record_bytes(reason="LIMITED ")),
        _dump(_record_bytes(reason="HALF")), _dump(_record_bytes(reason=1)),
        _dump(_record_bytes(planned_width=True)), _dump(_record_bytes(planned_width=4.0)),
        _dump(_record_bytes(width=True)), _dump(_record_bytes(width="1")),
        _dump(_record_bytes(planned_width=17, width=1)), _dump(_record_bytes(planned_width=0, width=0)),
        _dump(_record_bytes(width=0)), _dump(_record_bytes(width=5)),
        _dump(_record_bytes(width=2)),
        _dump(_record_bytes(reason="DEGRADED", width=3)),
        _dump(_record_bytes(reason="NONE", provider="", account_handle="", groups=0, width=3)),
        _dump(_record_bytes(decision_id="w1\n")), _dump(_record_bytes(decision_id="é")),
        _dump(_record_bytes(decision_id="")), _dump(_record_bytes(decision_id="a\x00")),
        _dump(_record_bytes(decision_id=7)), _dump(_record_bytes(decision_id=None)),
        _dump(_record_bytes(decided_at="2026-10-12 12:00:00")),
        _dump(_record_bytes(decided_at="2026-13-12T12:00:00Z")),
        _dump(_record_bytes(report_at="2026-10-12T12:00:00+00:00")),
        _dump(_record_bytes(report_at=None)),
        _dump(_record_bytes(provider="é")), _dump(_record_bytes(account_handle="a\nb")),
        _dump(_record_bytes(provider="", account_handle="acct-a")),
        _dump(_record_bytes(provider="claude", account_handle="")),
        _dump(_record_bytes(provider="x" * 129)), _dump(_record_bytes(account_handle="y" * 129)),
        _dump(_record_bytes(reason="STALE", width=1, report_at=STAMP)),
        _dump(_record_bytes(reason="INCOMPLETE", width=1, provider="claude")),
        _dump(_record_bytes(groups=0, unattributed=0)),
        _dump(_record_bytes(groups=0, unattributed=1)),
        _dump(_record_bytes(groups=-1)), _dump(_record_bytes(groups="1")),
        _dump(_record_bytes(groups=2**31 + 1)), _dump(_record_bytes(unattributed=2**31 + 1)),
        _dump(_record_bytes(problems=2**31 + 1)),
        _dump(_record_bytes(reason="NONE", width=4, provider="", account_handle="", groups=1)),
        _dump(_record_bytes(reason="NONE", width=4, provider="", account_handle="", groups=0,
                            problems=1)),
    ]
    valid = _dump(_record_bytes())
    assert bp.parse(valid).decision.width == 1
    # every other field valid, one key written twice (same value, and another value)
    bad_docs.append(valid.replace(b'"width": 1\n}', b'"width": 1,\n "width": 1\n}'))
    bad_docs.append(valid.replace(b'"provider": "claude",', b'"provider": "claude",\n "provider": "other",'))
    bad_docs.append(valid.replace(b'"groups": 1,', b'"groups": 1,\n "groups": 2,'))
    assert all(d != valid for d in bad_docs[-3:])
    for data in bad_docs:
        with pytest.raises(bp.BackpressureError):
            bp.parse(data)
    for data in ("text", None, 1, bytearray(b"{}")):
        with pytest.raises(bp.BackpressureError):
            bp.parse(data)

    assert bp.parse(_dump(_record_bytes(decision_id="A" + "b" * 63))).decision_id == "A" + "b" * 63
    with pytest.raises(bp.BackpressureError):
        bp.parse(_dump(_record_bytes(decision_id="A" + "b" * 64)))
    assert bp.parse(_dump(_record_bytes(planned_width=16, width=1))).decision.planned == 16
    assert bp.parse(_dump(_record_bytes(groups=2**31, unattributed=2**31, problems=2**31))
                    ).decision.problems == 2**31
    assert bp.parse(_dump(_record_bytes(provider="x" * 128, account_handle="y" * 128))
                    ).decision.provider == "x" * 128
    stale = _record_bytes(reason="STALE", width=1, provider="", account_handle="", groups=0,
                          report_at=None)
    assert bp.parse(_dump(stale)).decision.report_at is None
    filler = _dump(_record_bytes())
    edge = filler[:-2] + b" " * (4096 - len(filler)) + b"}\n"
    assert len(edge) == 4096 and bp.parse(edge).decision.width == 1
    with pytest.raises(bp.BackpressureError):
        bp.parse(edge + b" ")


# --------------------------------------------------------------------------- #
# R9 -- a file that is not a record is never "no decision"
# --------------------------------------------------------------------------- #

def test_a_record_file_that_is_not_one_is_never_read_as_no_decision(tmp_path):
    """A symbolic link (to a good record and dangling), a directory, a FIFO, a second name, an
    oversize file (4097 bytes), `{}` and mode 000 are `BackpressureError`; a missing file is
    None; a good file reads. Kills `read_decision` following a link (M14) or returning None for
    anything but a missing file."""
    bp = _bp()
    good = tmp_path / "good.json"
    good.write_bytes(_dump(_record_bytes()))
    assert bp.read_decision(good).decision.reason is bp.Reason.LIMITED
    assert bp.read_decision(tmp_path / "missing.json") is None
    with pytest.raises(bp.BackpressureError):
        bp.read_decision(str(good) + "\x00")

    (tmp_path / "link.json").symlink_to(good)
    (tmp_path / "dangling.json").symlink_to(tmp_path / "nowhere.json")
    (tmp_path / "dir.json").mkdir()
    os.mkfifo(tmp_path / "fifo.json")
    os.link(good, tmp_path / "second.json")
    (tmp_path / "big.json").write_bytes(b" " * 4097)
    (tmp_path / "empty.json").write_bytes(b"{}")
    for name in ("link.json", "dangling.json", "dir.json", "fifo.json", "second.json", "big.json",
                 "empty.json"):
        with pytest.raises(bp.BackpressureError):
            bp.read_decision(tmp_path / name)
    if os.geteuid() != 0:
        locked = tmp_path / "locked.json"
        locked.write_bytes(_dump(_record_bytes()))
        locked.chmod(0)
        try:
            with pytest.raises(bp.BackpressureError):
                bp.read_decision(locked)
        finally:
            locked.chmod(0o600)


# --------------------------------------------------------------------------- #
# R10 -- pure, and not wired in
# --------------------------------------------------------------------------- #

def test_the_decision_reads_and_writes_nothing_and_nothing_calls_the_module_yet(monkeypatch):
    """`decide` runs with `open`, `os.open`, `os.lstat`, `os.stat`, `os.listdir`, `os.scandir`,
    `os.mkdir`, `durable_io.durable_create` and `durable_io.durable_write` made to fail: it
    touches none of them. The module may write only through `durable_io.durable_create`: its
    imports and the `os.` / `durable_io.` attributes it uses stay inside **allow-lists** written
    below (a module that uses fewer, or an equivalent read-only name, extends the list in the same
    hunk); it never names `read_health`, `open`, `eval`, `exec`, `print` or `__import__`, never
    calls a path-writing method, and no other module of `src/hoh` imports it (the module has no
    caller outside the project loop (`orchestrator.py`, slice 2); a second caller moves this hunk). Kills a record written by `Path.write_bytes`
    (M27)."""
    import builtins
    from hoh import durable_io
    bp = _bp()

    def forbidden(*args, **kwargs):
        raise AssertionError("decide touched the filesystem")

    report = _report([_group()])
    with monkeypatch.context() as m:
        for owner, name in ((builtins, "open"), (os, "open"), (os, "lstat"), (os, "stat"),
                            (os, "listdir"), (os, "scandir"), (os, "mkdir"),
                            (durable_io, "durable_create"), (durable_io, "durable_write")):
            m.setattr(owner, name, forbidden)
        got = bp.decide(8, report, now=NOW)
    assert (got.width, got.reason) == (1, bp.Reason.LIMITED)

    src = ROOT / "src" / "hoh"
    tree = ast.parse((src / "backpressure.py").read_text(encoding="utf-8"))
    plain = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    assert plain <= {"json", "os", "re", "stat"}
    relative = {(n.module or a.name) for n in tree.body
                if isinstance(n, ast.ImportFrom) and n.level == 1 for a in n.names}
    assert relative <= {"durable_io", "contracts", "health", "routing", "wave"}
    outside = {n.module for n in tree.body
               if isinstance(n, ast.ImportFrom) and n.level == 0}
    assert outside <= {"__future__", "collections.abc", "dataclasses", "datetime", "enum",
                       "pathlib"}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert names.isdisjoint({"open", "eval", "exec", "print", "__import__"})
    # the decision itself (not the admission of slice 2, which reads the health) never names a reader
    pure = [n for n in tree.body if isinstance(n, ast.FunctionDef)
            and n.name in ("decide", "narrowed", "_scope", "_group_reason", "_wait_reason",
                           "_read_moment", "_moment", "_stamp", "_planned")]
    assert len(pure) == 9
    assert {n.id for f in pure for n in ast.walk(f) if isinstance(n, ast.Name)}.isdisjoint(
        {"read_health", "open", "os", "durable_io", "load_routing_policy"})
    attrs = {(n.value.id, n.attr) for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    assert {a for o, a in attrs if o == "os"} <= {"PathLike", "fspath", "lstat", "mkdir", "path"}
    assert {a for o, a in attrs if o == "durable_io"} <= {"durable_create", "read_single_link"}
    # methods of any receiver except the module `os` (whose names the allow-list above pins): `os.mkdir` is
    # allowed by attribute, `Path.mkdir` and every other path-writing method is not
    methods = {n.func.attr for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and not (isinstance(n.func.value, ast.Name) and n.func.value.id == "os")}
    assert methods.isdisjoint({"write_bytes", "write_text", "open", "mkdir", "touch", "rename",
                               "symlink_to", "hardlink_to", "chmod", "fdopen", "system", "popen"})
    callers = []
    for path in sorted(src.rglob("*.py")):
        if path.name == "backpressure.py":
            continue
        callers += [path.name] * _runtime_imports_of_backpressure(ast.parse(path.read_text(encoding="utf-8")))
    assert callers == ["orchestrator.py"]


def _is_type_checking(test: ast.expr) -> bool:
    """`TYPE_CHECKING` or `typing.TYPE_CHECKING` and nothing else (`not TYPE_CHECKING`, `TYPE_CHECKING or x` run)."""
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name) and test.value.id == "typing")


def _runtime_imports_of_backpressure(tree: ast.AST) -> int:
    """How many imports of `backpressure` the module runs: every import, in a function or a branch too, except
    those in the body of an `if TYPE_CHECKING:` (a name for the type checker, never executed; its `else` runs)."""
    count, todo = 0, [tree]
    while todo:
        n = todo.pop()
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[-1] == "backpressure":
            count += 1
        if isinstance(n, ast.ImportFrom) and any(a.name == "backpressure" for a in n.names):
            count += 1
        if isinstance(n, ast.Import) and any(a.name.endswith("backpressure") for a in n.names):
            count += 1
        if isinstance(n, ast.If) and _is_type_checking(n.test):
            todo.extend(n.orelse)
        else:
            todo.extend(ast.iter_child_nodes(n))
    return count


def test_a_type_checking_only_import_is_not_a_caller_of_the_module_and_a_runtime_import_is():
    """The caller scan above means "nothing CALLS the module": no import that runs. `parallelmode` names `WaveGate`
    for the type checker alone (`if TYPE_CHECKING:`), which is not a caller; every other spelling still counts.
    Kills a scan that skips every `if`, every guarded import whatever the test, or the `else` branch (M-C1..M-C3)."""
    def n(source: str) -> int:
        return _runtime_imports_of_backpressure(ast.parse(source))
    assert n("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .backpressure import WaveGate\n") == 0
    assert n("import typing\nif typing.TYPE_CHECKING:\n    from . import backpressure\n") == 0
    assert n("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import hoh.backpressure\n") == 0
    assert n("from .backpressure import WaveGate\n") == 1
    assert n("from . import backpressure\n") == 1
    assert n("import hoh.backpressure\n") == 1
    assert n("def f():\n    from . import backpressure\n") == 1                                  # a lazy import runs
    assert n("if TYPE_CHECKING:\n    pass\nelse:\n    from .backpressure import WaveGate\n") == 1
    assert n("if not TYPE_CHECKING:\n    from .backpressure import WaveGate\n") == 1
    assert n("if TYPE_CHECKING or flag:\n    from .backpressure import WaveGate\n") == 1
    assert n("if flag:\n    from .backpressure import WaveGate\n") == 1
    assert n("if TYPE_CHECKING:\n    from .backpressure import WaveGate\nfrom .backpressure import decide\n") == 1
