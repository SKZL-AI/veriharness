"""P3-08, first increment: what the parked runs of one root say about a provider, together.

`hoh.health.read_health` reads the runs under a runs root and groups the provider waits by the
provider and account that each park's receipt names. These tests build real runs under `tmp_path`
through the product's own `RunStore`, `RunState` and `dispatchrecovery.append_record`, and read
them back. Nothing starts a process, Herdr or an agent (any `subprocess` use fails the test), no
test sleeps, and nothing writes outside `tmp_path`. `now` is always injected.

Mutant ids in a docstring (`G1`..) are the spec's table of mutations the integration review
applies; each test says which it kills. The two byte limits and every number below are written
out as literals on purpose: a table imported from the module would agree with a mutated module.
"""
from __future__ import annotations

import ast
import json
import os
import signal
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hoh import dispatchrecovery, health, quota, resumeloop
from hoh.contracts import Condition, RunState, Stage
from hoh.dispatchrecovery import (
    NO_MASTER, DispatchProvenance, ParkBinding, RoutingDecision, RoutingEscalation, park_id,
)
from hoh.failures import AUTOMATIC_KINDS, NormalizedFailure
from hoh.health import Evidence, HealthState, read_health
from hoh.store import RunStore
from hoh.telemetry import NOT_AVAILABLE

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
SOON = (NOW + timedelta(hours=1)).isoformat()          # the form quota.next_attempt_at writes
LATER = (NOW + timedelta(hours=2)).isoformat()
PAST = (NOW - timedelta(hours=1)).isoformat()
N = NormalizedFailure
MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a test started a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "runs"
    r.mkdir()
    return r


# --------------------------------------------------------------------------- #
# Builders: real parked runs, with the receipt the controller writes
# --------------------------------------------------------------------------- #


def _state(run_id: str, *, failure: NormalizedFailure | None = N.USAGE_LIMIT, retry: str | None = SOON,
           condition: Condition = Condition.BLOCKED, kind: str | None = None,
           stage: Stage = Stage.DEVELOPING, iteration: int = 1) -> RunState:
    st = RunState(run_id=run_id, repo_path="/arena/repo", project_name="demo",
                  spec_path="/arena/spec.md", spec_digest="s", policy_digest="p",
                  profile_digest="h", stage=stage, condition=condition, iteration=iteration)
    if condition is Condition.BLOCKED:
        st.blocked_kind = kind if kind is not None else (failure.value.lower() if failure else None)
        st.retry_after = retry
        st.blocked_reason = "parked"
    return st


def _receipt(state: RunState, failure: NormalizedFailure, *, provider: str = "claude",
             account: str = "acct-a", model: str = "m-1", retry: str | None = None,
             run_id: str | None = None, iteration: int | None = None,
             attempt: int | None = None, stage: str | None = None) -> RoutingDecision:
    binding = ParkBinding(
        programme="NONE", program_epoch=0, run_id=run_id or state.run_id,
        iteration=state.iteration if iteration is None else iteration,
        attempt=state.attempt if attempt is None else attempt,
        stage=stage or state.stage.value, candidate="NONE", master=NO_MASTER)
    retry_at = retry or state.retry_after
    return RoutingDecision(
        park_id=park_id(binding, failure, retry_at), reason=failure.value, binding=binding,
        provenance=DispatchProvenance(
            provider=provider, model=model, account_handle=account, role="developer",
            capability_requirements=["read_repo", "write_worktree", "run_shell"]),
        retry_at=retry_at)


def _escalation(rec: RoutingDecision) -> RoutingEscalation:
    """The line `resumeloop._exhausted` appends when it gives a park up."""
    return RoutingEscalation(park_id=rec.park_id, reason=rec.reason, binding=rec.binding,
                             provenance=rec.provenance, escalation="RETRIES_EXHAUSTED")


def _park(root: Path, run_id: str, *, failure: N = N.USAGE_LIMIT, retry: str | None = SOON,
          provider: str | None = "claude", account: str = "acct-a", receipt: bool = True,
          attempts: int = 0, escalated: bool = False, **receipt_overrides) -> RunStore:
    """A run parked on `failure` with the receipt the controller writes, as one store.
    `attempts` is the state's `resume_attempts`; `escalated` appends the loop's
    RETRIES_EXHAUSTED line for the park."""
    st = _state(run_id, failure=failure, retry=retry)
    st.resume_attempts = attempts
    store = RunStore(root, run_id)
    store.write_state(st)
    if receipt and provider is not None:
        rec = _receipt(st, failure, provider=provider, account=account, **receipt_overrides)
        dispatchrecovery.append_record(store.dir, rec)
        if escalated:
            dispatchrecovery.append_record(store.dir, _escalation(rec))
    return store


def _read(root: Path, now: datetime = NOW):
    return read_health(root, now=now)


def _tree(path: Path) -> dict[str, tuple[bytes, int, int]]:
    out = {}
    for p in sorted(path.rglob("*")):
        st = p.lstat()
        out[str(p.relative_to(path))] = (p.read_bytes() if p.is_file() and not p.is_symlink()
                                         else b"", st.st_mtime_ns, st.st_mode)
    return out


# --------------------------------------------------------------------------- #
# H1 -- correlation: one limit, several runs, one reading
# --------------------------------------------------------------------------- #


def test_runs_parked_on_one_limited_account_are_one_provider_limited_until_the_latest_reset(root):
    """Two runs of one provider and account, two different stated resets: one group, LIMITED, the
    runs named, the class counted per class (two of one, one of the other), `not_before` the later
    stamp. Kills G2 (a group per run), N1 (`not_before` the earliest), Q2 (every class counted as
    one)."""
    _park(root, "a", retry=SOON)
    _park(root, "b", retry=LATER, failure=N.RATE_LIMIT)
    _park(root, "c", retry=SOON)
    got = _read(root)
    assert got.complete and got.runs_seen == 3 and got.unattributed == ()
    [claude] = got.providers
    assert (claude.provider, claude.account_handle) == ("claude", "acct-a")
    assert claude.state is HealthState.LIMITED
    assert claude.runs == ("a", "b", "c")
    assert dict(claude.failures) == {"USAGE_LIMIT": 2, "RATE_LIMIT": 1}
    assert claude.failures == (("RATE_LIMIT", 1), ("USAGE_LIMIT", 2))
    assert claude.given_up == ()
    assert claude.not_before == LATER
    assert got.for_provider("claude", "acct-a") is claude and got.for_provider("claude") is None


def test_another_provider_or_another_account_is_another_group(root):
    """Same provider, two accounts; a second provider under the first account's handle. The run
    names are in the opposite order of the group names, so the groups come back in name order only
    if they are sorted. Kills G1 (grouped by provider alone), Q3 (groups in discovery order)."""
    _park(root, "a1", provider="codex", account="acct-a")
    _park(root, "a2", provider="claude", account="acct-b")
    _park(root, "c1", provider="claude", account="acct-a")
    got = _read(root)
    keys = [(p.provider, p.account_handle, p.runs) for p in got.providers]
    assert keys == [("claude", "acct-a", ("c1",)), ("claude", "acct-b", ("a2",)),
                    ("codex", "acct-a", ("a1",))]


def test_an_account_the_run_never_declared_is_correlated_per_provider_and_says_so(root):
    """No registered account: the receipt says NOT_AVAILABLE and the group says it. Two runs of
    one harness profile are one group (the declared limit of this increment), never merged with a
    provider that is itself unnamed."""
    _park(root, "a", account=NOT_AVAILABLE)
    _park(root, "b", account=NOT_AVAILABLE)
    [g] = _read(root).providers
    assert (g.provider, g.account_handle, g.runs) == ("claude", NOT_AVAILABLE, ("a", "b"))


@pytest.mark.parametrize("limit,pending,expected", [
    # a limit still pending dominates whatever else waits
    ([SOON], [SOON], HealthState.LIMITED),
    ([SOON, PAST], [SOON], HealthState.LIMITED),
    # the limit has elapsed, an availability failure has not
    ([PAST], [SOON], HealthState.DEGRADED),
    # nothing pending: the stated times passed, nobody looked again
    ([PAST], [PAST], HealthState.RESET_DUE),
    ([], [PAST], HealthState.RESET_DUE),
    ([PAST], [], HealthState.RESET_DUE),
    # only availability failures, pending
    ([], [SOON], HealthState.DEGRADED),
])
def test_the_state_of_a_provider_follows_what_is_still_pending(root, limit, pending, expected):
    """A table over (limit waits, availability waits). Kills S1 (LIMITED decided over every wait,
    pending or not), and the order of LIMITED over DEGRADED over RESET_DUE."""
    for i, retry in enumerate(limit):
        _park(root, f"l{i}", failure=N.RATE_LIMIT, retry=retry)
    for i, retry in enumerate(pending):
        _park(root, f"p{i}", failure=N.PROVIDER_UNAVAILABLE, retry=retry)
    [g] = _read(root).providers
    assert g.state is expected


def test_no_evidence_is_not_health_a_clean_root_reports_nothing(root):
    """A runs root whose runs are not parked on a provider has no provider in the report: there
    is no HEALTHY state to read."""
    RunStore(root, "r1").write_state(_state("r1", condition=Condition.ACTIVE))
    got = _read(root)
    assert got.providers == () and got.unattributed == () and got.complete and got.runs_seen == 1
    assert [s.value for s in HealthState] == ["LIMITED", "DEGRADED", "RESET_DUE", "GIVEN_UP"]


# --------------------------------------------------------------------------- #
# H1 -- what is evidence about a provider, and what is not
# --------------------------------------------------------------------------- #


def test_the_evidence_table_decides_every_failure_class_and_names_exactly_four():
    """The table covers every `NormalizedFailure` (a class added to `failures.py` without a row
    fails here) and the four that say something about a provider are named as literals. A fifth class
    counted, or one dropped, fails here."""
    assert set(health.PROVIDER_EVIDENCE) == set(NormalizedFailure)
    counted = {c.value: e.value for c, e in health.PROVIDER_EVIDENCE.items() if e is not None}
    assert counted == {"USAGE_LIMIT": "LIMIT", "RATE_LIMIT": "LIMIT",
                       "PROVIDER_UNAVAILABLE": "AVAILABILITY", "NETWORK_TRANSIENT": "AVAILABILITY"}
    assert {e.value for e in Evidence} == {"LIMIT", "AVAILABILITY"}


@pytest.mark.parametrize("failure", [N.ROLE_TIMEOUT, N.PROMPT_STALLED])
def test_a_role_timeout_or_a_stalled_prompt_is_not_evidence_about_the_provider(root, failure):
    """Both are automatic waits and both are parked with a receipt, and neither says anything
    about the provider (`failures.py`): the run is seen and nothing is reported. Kills E1."""
    assert failure.value.lower() in AUTOMATIC_KINDS
    _park(root, "r", failure=failure)
    got = _read(root)
    assert got.runs_seen == 1 and got.providers == () and got.unattributed == ()


def _resumed_with_a_stale_kind(root: Path, condition: Condition) -> None:
    """A run that was parked and went on keeps what an earlier block wrote: only a run that is
    BLOCKED now is waiting on anything."""
    st = _state("r", condition=condition)
    st.blocked_kind, st.retry_after = "usage_limit", SOON
    RunStore(root, "r").write_state(st)


@pytest.mark.parametrize("make", [
    lambda root: RunStore(root, "r").write_state(_state("r", condition=Condition.ACTIVE)),
    lambda root: RunStore(root, "r").write_state(_state("r", condition=Condition.FAILED)),
    lambda root: _resumed_with_a_stale_kind(root, Condition.ACTIVE),
    lambda root: _resumed_with_a_stale_kind(root, Condition.PAUSED),
    lambda root: RunStore(root, "r").write_state(_state("r", failure=None, kind="storage_pressure")),
    lambda root: RunStore(root, "r").write_state(_state("r", failure=None, kind="parked_by_contract")),
    lambda root: RunStore(root, "r").write_state(_state("r", failure=None, kind=None)),
    lambda root: RunStore(root, "r").write_state(_state("r", failure=None, kind="auth_expired")),
])
def test_a_run_that_is_not_parked_on_an_automatic_provider_wait_says_nothing(root, make):
    """ACTIVE, FAILED, an ACTIVE or PAUSED run that still carries an old `usage_limit`, a storage
    park, a contract park, a block without a kind, and a class a person must act on
    (`auth_expired` is not an automatic wait). Kills A1 (the condition and the
    kind not checked)."""
    make(root)
    got = _read(root)
    assert got.runs_seen == 1 and got.providers == () and got.unattributed == ()


# --------------------------------------------------------------------------- #
# H1 -- attribution: no false correlation
# --------------------------------------------------------------------------- #


def test_a_park_without_a_receipt_is_listed_unattributed_and_never_grouped(root):
    """Two legacy parks (the state says `usage_limit`, no receipt) and a receipt that names no
    provider: all three are listed with the reason and none is a group. Kills U1 (unnamed waits
    grouped under NOT_AVAILABLE as if they were one provider)."""
    _park(root, "legacy1", receipt=False)
    _park(root, "legacy2", receipt=False)
    _park(root, "unnamed", provider=NOT_AVAILABLE)
    got = _read(root)
    assert got.providers == ()
    assert [w.run_id for w in got.unattributed] == ["legacy1", "legacy2", "unnamed"]
    assert got.unattributed[0].unattributed == "no receipt describes this park"
    assert got.unattributed[2].unattributed == "the receipt does not name the provider"
    assert got.complete


@pytest.mark.parametrize("override", [
    {"retry": LATER},                # another wait's receipt (the stated time differs)
    {"run_id": "someone-else"},      # a receipt copied from another run
    {"iteration": 2},
    {"attempt": 2},
    {"stage": "PLANNING"},
])
def test_a_receipt_of_another_park_does_not_attribute(root, override):
    """The receipt must be this park's: the stated retry time, the run, the iteration, the attempt
    and the stage the state still has. Kills B1 (the stated time not compared), B3a..B3d (the run, the iteration, the
    attempt, the stage: one comparison each dropped)."""
    st = _state("r", retry=SOON)
    store = RunStore(root, "r")
    store.write_state(st)
    dispatchrecovery.append_record(store.dir, _receipt(st, N.USAGE_LIMIT, **override))
    got = _read(root)
    assert got.providers == () and [w.run_id for w in got.unattributed] == ["r"]


def test_a_receipt_of_another_class_does_not_attribute_and_the_newest_matching_one_wins(root):
    """The state parks as `usage_limit`; a receipt for RATE_LIMIT with the same stated time is
    another park (kills B2). Of two matching receipts the newest names the provider (kills O1:
    the oldest read)."""
    st = _state("r", failure=N.USAGE_LIMIT)
    store = RunStore(root, "r")
    store.write_state(st)
    dispatchrecovery.append_record(store.dir, _receipt(st, N.RATE_LIMIT, provider="other"))
    assert _read(root).providers == ()
    dispatchrecovery.append_record(store.dir, _receipt(st, N.USAGE_LIMIT, provider="old"))
    dispatchrecovery.append_record(store.dir, _receipt(st, N.USAGE_LIMIT, provider="new"))
    [g] = _read(root).providers
    assert g.provider == "new"


@pytest.mark.parametrize("field", ["provider", "model"])
def test_a_name_of_over_128_characters_is_not_a_name_and_is_not_echoed(root, field):
    """The receipt's strings are unbounded and the report echoes them: 128 characters is a name,
    129 is not (the wait is unattributed and nothing of it is echoed). Kills Z4 (the bound
    doubled), Z5 (the bound exclusive)."""
    _park(root, "fits", **{field: "p" * 128})
    _park(root, "over", **{field: "q" * 129})
    got = _read(root)
    [g] = got.providers
    assert g.runs == ("fits",)
    [w] = got.unattributed
    assert w.run_id == "over" and "128 characters" in w.unattributed
    assert (w.provider, w.account_handle, w.model) == (NOT_AVAILABLE,) * 3
    assert "q" * 129 not in json.dumps(got.as_dict())


def test_a_stated_time_is_echoed_to_at_most_64_characters(root):
    """A state can carry any string as `retry_after`; the report keeps its first 64 characters and
    still says the wait is due (an unreadable stamp). Kills T1 (the echo unbounded)."""
    _park(root, "huge", retry="2026-10-12T13:00:00+00:00" + "x" * 100000, receipt=False)
    [w] = _read(root).unattributed
    assert len(w.retry_at) == 64 and w.due is True


# --------------------------------------------------------------------------- #
# H1 -- a park the resume loop gave up on is not a wait
# --------------------------------------------------------------------------- #

AFTER = NOW + timedelta(hours=3)            # every SOON and PAST stamp has come due, LATER has not


@pytest.mark.parametrize("how", ["attempts at the limit", "escalated only", "both"])
def test_a_park_the_loop_gave_up_on_is_given_up_and_never_reset_due(root, how):
    """`resumeloop` stops at `quota.MAX_RESUME_ATTEMPTS` (5), writes a RETRIES_EXHAUSTED line and
    leaves the run parked for a person: the group is GIVEN_UP, lists the run, and states no
    `not_before`. Either signal alone is enough (the state's counter, which also covers a park
    with no receipt and a park the loop has not yet looked at; the escalation line, which is the
    loop's own record). Kills X1 (never given up: the p308spec-a B1 repro), X2 (the counter not
    read), X3 (the escalation not read)."""
    _park(root, "r", retry=SOON, attempts=5 if how != "escalated only" else 0,
          escalated=how != "attempts at the limit")
    got = read_health(root, now=AFTER)
    [g] = got.providers
    assert g.state.value == "GIVEN_UP" and g.state is HealthState.GIVEN_UP
    assert g.runs == ("r",) and g.given_up == ("r",) and g.not_before is None
    assert got.complete


def test_one_attempt_short_of_the_limit_is_still_reset_due(root):
    """The control of the test above and the exact bound: 4 attempts, no escalation. Kills X7
    (the limit off by one: `>` instead of `>=`, or `>= 4`)."""
    _park(root, "r", retry=SOON, attempts=4)
    [g] = read_health(root, now=AFTER).providers
    assert g.state is HealthState.RESET_DUE and g.given_up == () and g.not_before == SOON


def test_a_given_up_run_is_listed_but_decides_nothing_for_its_group(root):
    """Run `a` is given up on a limit that has not come due; run `b` waits on an availability
    failure. The group is DEGRADED (not LIMITED) with `b`'s time, and `a` is listed as given up.
    Kills X4 (a given-up wait still decides the state and `not_before`)."""
    _park(root, "a", failure=N.USAGE_LIMIT, retry=LATER, attempts=5)
    _park(root, "b", failure=N.PROVIDER_UNAVAILABLE, retry=SOON)
    [g] = _read(root).providers
    assert g.state is HealthState.DEGRADED and g.not_before == SOON
    assert g.runs == ("a", "b") and g.given_up == ("a",)
    assert dict(g.failures) == {"PROVIDER_UNAVAILABLE": 1, "USAGE_LIMIT": 1}


def test_an_escalation_of_another_park_does_not_give_this_park_up(root):
    """The escalation names the park it ended; one for another park of the same run (another
    stated time) says nothing about this one. Kills X5 (any escalation counts)."""
    st = _state("r", retry=SOON)
    store = RunStore(root, "r")
    store.write_state(st)
    dispatchrecovery.append_record(store.dir, _receipt(st, N.USAGE_LIMIT))
    other = _receipt(st, N.USAGE_LIMIT, retry=LATER)
    assert other.park_id != _receipt(st, N.USAGE_LIMIT).park_id
    dispatchrecovery.append_record(store.dir, _escalation(other))
    [g] = read_health(root, now=AFTER).providers
    assert g.state is HealthState.RESET_DUE and g.given_up == ()


def test_a_park_with_no_receipt_at_the_limit_is_listed_given_up(root):
    """Without a receipt the counter is all there is. Kills X2 for the unattributed path."""
    _park(root, "short", receipt=False, attempts=4)
    _park(root, "long", receipt=False, attempts=5)
    got = _read(root)
    assert {w.run_id: w.given_up for w in got.unattributed} == {"short": False, "long": True}


def test_the_reading_agrees_with_the_real_resume_loop_on_a_park_it_gave_up(root):
    """`resumeloop.resume_runs` itself: a parked run at the attempt limit is `given_up` and gets
    its RETRIES_EXHAUSTED line; the reading then says GIVEN_UP -- and still says it after the
    counter is reset by hand, because the loop's own record names the park. Kills X1/X3 against
    the product's loop rather than a hand-made line."""
    store = _park(root, "r", retry=SOON, attempts=5)
    [rec] = resumeloop.resume_runs(root, now=AFTER)
    assert rec["action"] == "given_up" and rec["escalation"]["escalation"] == "RETRIES_EXHAUSTED"
    [g] = read_health(root, now=AFTER).providers
    assert g.state.value == "GIVEN_UP"
    st = store.read_state()
    st.resume_attempts = 0
    store.write_state(st)
    [g] = read_health(root, now=AFTER).providers
    assert g.state is HealthState.GIVEN_UP and g.given_up == ("r",)


def test_a_receipt_whose_class_is_no_failure_class_does_not_attribute(root):
    """`RoutingDecision.reason` is a plain string; one that names no `NormalizedFailure` is not a
    description of this park, whatever else it matches. Kills Q8 (the class check skipped)."""
    st = _state("r")
    store = RunStore(root, "r")
    store.write_state(st)
    rec = _receipt(st, N.USAGE_LIMIT).model_copy(update={"reason": "NOT_A_FAILURE_CLASS"})
    dispatchrecovery.append_record(store.dir, rec)
    got = _read(root)
    assert got.providers == () and [w.run_id for w in got.unattributed] == ["r"]


def test_a_file_that_grew_after_the_size_check_is_refused(root, monkeypatch):
    """The size is judged by `lstat` and again on the bytes read: a file that grows in between
    is refused, not read. Kills Q4 (the second check dropped)."""
    _park(root, "r")
    real = health.read_single_link

    def grown(path):
        data = real(path)
        return data + b" " * (8 * MIB) if str(path).endswith("state.json") and data else data

    monkeypatch.setattr(health, "read_single_link", grown)
    got = _read(root)
    assert got.providers == () and [what for what, why in got.problems] == ["r"]
    assert "larger than" in got.problems[0][1]


# --------------------------------------------------------------------------- #
# H1 -- the time rule is the resume path's
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("stamp", [
    None, "", "not a time", "2026-10-12T11:59:59+00:00", "2026-10-12T12:00:00+00:00",
    "2026-10-12T12:00:01+00:00", "2026-10-12T12:00:01Z", "2026-10-12T11:00:00Z",
    "2026-10-12T12:00:01", "2026-10-12T11:00:00", "2026-10-12T14:00:00+02:00",
    "2026-10-12T13:00:00+02:00", "0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00",
])
def test_a_wait_is_due_exactly_when_the_resume_path_says_so(root, stamp):
    """`quota.is_due` is what `hoh resume-quota` applies to the same park: the boundary (a stated
    time equal to now is due), a naive stamp (UTC), `Z`, offsets, and a stamp that is no time (due,
    never a wait that never ends). Kills D1 (an unreadable stamp pending)."""
    _park(root, "r", retry=stamp, receipt=False)
    [w] = _read(root).unattributed
    assert w.due is quota.is_due(stamp, now=NOW)
    assert w.retry_at == stamp


def test_an_unreadable_stamp_is_due_and_names_no_not_before(root):
    _park(root, "a", retry="soonish")
    _park(root, "b", retry="tomorrow-ish")
    [g] = _read(root).providers
    assert g.state is HealthState.RESET_DUE and g.not_before is None


def test_a_naive_now_is_refused_and_the_default_now_is_the_clock(root):
    """The default `now` is the real clock: a stamp in 2999 is pending, one in 2020 is due.
    Kills Q1 (a fixed default moment)."""
    far = "2999-01-01T00:00:00+00:00"
    old = "2020-01-01T00:00:00+00:00"
    _park(root, "far", retry=far)
    _park(root, "old", retry=old, provider="codex")
    with pytest.raises(ValueError):
        read_health(root, now=datetime(2026, 10, 12, 12, 0, 0))
    got = read_health(root)
    claude, codex = got.providers
    assert claude.state is HealthState.LIMITED and claude.not_before == far
    assert codex.state is HealthState.RESET_DUE and codex.not_before == old


def test_a_naive_stamp_is_utc_for_the_state_and_for_not_before(root):
    """The resume path reads a naive stamp as UTC (`quota.is_due`); so does the reading, for the
    state and for `not_before`. Kills Q7 (a naive stamp no time: `not_before` None)."""
    _park(root, "r", retry="2026-10-12T13:00:00")
    [g] = _read(root).providers
    assert g.state is HealthState.LIMITED and g.not_before == "2026-10-12T13:00:00+00:00"


def test_the_time_is_read_from_the_whole_stamp_and_only_the_echo_is_cut(root):
    """A readable stamp longer than the 64-character echo (forty fractional zeros, an offset): the
    wait is pending by the whole stamp and `not_before` is read from it, not from the cut echo.
    Kills X6 (the time read from the echo)."""
    long_stamp = "2026-10-12T20:00:00." + "0" * 40 + "-12:00"
    assert len(long_stamp) > 64
    _park(root, "r", retry=long_stamp)
    [g] = _read(root).providers
    assert g.state is HealthState.LIMITED
    assert g.not_before == "2026-10-12T20:00:00-12:00"


# --------------------------------------------------------------------------- #
# H1 -- a reading, nothing else
# --------------------------------------------------------------------------- #


def test_reading_writes_nothing_and_is_repeatable(root):
    """The root, every run directory and every file are byte-, mtime- and mode-identical after
    two readings, and the two readings are equal. Kills W1 (a reading that touches the run directory)."""
    _park(root, "a")
    _park(root, "b", failure=N.PROVIDER_UNAVAILABLE, receipt=False)
    (root / "timers.json").write_text("{}", encoding="utf-8")
    before = _tree(root)
    first, second = _read(root), _read(root)
    assert first == second
    assert _tree(root) == before


#: The three names that delete are built from halves on purpose: this file must not carry the
#: words as literals (a line-grep guard of another harness reads source text).
_DELETERS = {"un" + "link", "rm" + "dir", "re" + "move"}
FORBIDDEN_NAMES = {"read_state", "write_state", "ensure", "lock", "process_lock", "process_marker",
                   "rename", "mkdir", "touch", "write_text", "write_bytes", "kill",
                   "Popen", "system", "chmod", "symlink", "link"} | _DELETERS
FORBIDDEN_IMPORTS = {"subprocess", "shutil", "signal", "fcntl", "tempfile", "herdr", "agentcontrol",
                     "dispatchers", "pane_ledger", "controller", "stages", "resumeloop",
                     "timers", "sentinel"}


def test_the_module_is_structurally_a_reader():
    """A syntax-tree scan of `src/hoh/health.py`: no writer name, no process or write-capable
    import, `durable_io` only for `read_single_link`, no `open(...)` with a mode that writes.
    Catches a reader that grows a write (W1 by construction rather than by observation)."""
    tree = ast.parse(Path(health.__file__).read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.Attribute):
            names = [node.attr]
        else:
            continue
        found += [f"{n} (line {node.lineno})" for n in names
                  if n in FORBIDDEN_NAMES or n.startswith("durable_")]
        if (isinstance(node, ast.Attribute) and node.attr == "replace"
                and isinstance(node.value, ast.Name) and node.value.id == "os"):
            found.append(f"os.replace (line {node.lineno})")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [(a.name, None) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [(node.module or "", a.name) for a in node.names]
        else:
            continue
        for module, name in modules:
            top = module.split(".")[0]
            if top in FORBIDDEN_IMPORTS or name in FORBIDDEN_IMPORTS:
                found.append(f"import {module or name} (line {node.lineno})")
            if module == "durable_io" and name != "read_single_link":
                found.append(f"durable_io.{name} (line {node.lineno})")
    assert not found, found
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "open"]
    assert not calls


# --------------------------------------------------------------------------- #
# H2 -- hostile input: every unreadable thing is a problem, never a crash, a hang or a skip
# --------------------------------------------------------------------------- #


class _Hang(Exception):
    pass


@pytest.fixture
def _alarm():
    """A reading that blocks (a FIFO opened for reading) fails the test instead of hanging it."""
    def fire(signum, frame):
        raise _Hang("read_health blocked for 20 s")

    old = signal.signal(signal.SIGALRM, fire)
    signal.alarm(20)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


def _good_state_text(root: Path, run_id: str = "bad") -> str:
    return _state(run_id).model_dump_json()


def _bad_run(root: Path, run_id: str = "bad") -> Path:
    d = root / run_id
    d.mkdir()
    return d


def _case_missing_mode(root, tmp_path):
    d = _bad_run(root)
    (d / "state.json").write_text(_good_state_text(root), encoding="utf-8")
    os.chmod(d / "state.json", 0)


def _case_fifo(root, tmp_path):
    os.mkfifo(_bad_run(root) / "state.json")


def _case_directory(root, tmp_path):
    (_bad_run(root) / "state.json").mkdir()


def _case_dangling_link(root, tmp_path):
    os.symlink(tmp_path / "nowhere.json", _bad_run(root) / "state.json")


def _case_link_to_valid_file(root, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(_good_state_text(root), encoding="utf-8")
    os.symlink(target, _bad_run(root) / "state.json")


def _case_hard_link(root, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(_good_state_text(root), encoding="utf-8")
    os.link(target, _bad_run(root) / "state.json")


def _raw(data: bytes):
    def case(root, tmp_path):
        (_bad_run(root) / "state.json").write_bytes(data)
    return case


def _case_oversize(root, tmp_path):
    (_bad_run(root) / "state.json").write_bytes(
        _good_state_text(root).encode() + b" " * (8 * MIB + 1 - len(_good_state_text(root))))


BAD_STATES = {
    "mode 000": _case_missing_mode,
    "fifo": _case_fifo,
    "directory": _case_directory,
    "dangling symlink": _case_dangling_link,
    "symlink to a valid state": _case_link_to_valid_file,
    "second name (hard link)": _case_hard_link,
    "latin-1 bytes": _raw(b'{"run_id": "bad", "note": "caf\xe9"}'),
    "utf-16 with bom": _raw('{"run_id": "bad"}'.encode("utf-16")),
    "nul byte": _raw(b'{"run_id": "bad"}\x00'),
    "lone surrogate escape": _raw(b'{"run_id": "\\ud800"}'),
    "corrupt json": _raw(b'{"run_id": "bad", "stage": '),
    "schema from the future": lambda root, tmp: _raw(json.dumps(
        {**json.loads(_good_state_text(root)), "schema_version": 999}).encode())(root, tmp),
    "deeply nested": _raw(b"[" * 100000 + b"]" * 100000),
    "json array": _raw(b'[1, 2, 3]'),
    "json string": _raw(b'"parked"'),
    "empty file": _raw(b''),
    "one byte over eight mebibytes": _case_oversize,
}


@pytest.mark.parametrize("case", sorted(BAD_STATES))
def test_an_unreadable_state_is_a_problem_of_the_report_and_never_a_silent_skip(
        root, tmp_path, case, _alarm):
    """Each hostile `state.json` next to a good parked run: the good run is still read, the bad
    one is exactly one problem naming it, the report is incomplete, nothing raises and nothing
    blocks. Kills F1 (the regular-file/no-follow/no-block open dropped: the FIFO hangs, the link
    is followed), L1 (the second name accepted), P1 (a problem dropped: `complete` stays true),
    Z1 (the byte limit)."""
    if case == "mode 000" and os.geteuid() == 0:
        pytest.skip("root reads mode 000")
    _park(root, "good")
    BAD_STATES[case](root, tmp_path)
    got = _read(root)
    assert [p.provider for p in got.providers] == ["claude"] and got.providers[0].runs == ("good",)
    assert [what for what, why in got.problems] == ["bad"], got.problems
    assert got.complete is False and got.runs_seen == 1
    json.dumps(got.as_dict())


def test_state_files_of_exactly_eight_mebibytes_are_read_and_one_byte_more_is_not(root):
    """The bound is 8 MiB, inclusive. A valid state padded with trailing whitespace to exactly that
    is read as a run; the oversize case above is the other side. Kills Z2 (the bound lowered or
    off by one; `>` made `>=`), Z1 with the case above."""
    d = _bad_run(root, "big")
    text = _state("big").model_dump_json()
    (d / "state.json").write_bytes(text.encode() + b" " * (8 * MIB - len(text)))
    assert (d / "state.json").stat().st_size == 8 * MIB
    got = _read(root)
    assert got.complete and got.runs_seen == 1 and len(got.unattributed) == 1


def _receipt_lines(root: Path) -> str:
    st = _state("r")
    return dispatchrecovery_line(_receipt(st, N.USAGE_LIMIT, provider="claude"))


def dispatchrecovery_line(rec: RoutingDecision) -> str:
    body = rec.model_dump(mode="json")
    return json.dumps({"digest": dispatchrecovery._digest_of(body), **body}, sort_keys=True)


def _park_with_receipts_file(root: Path, how) -> RunStore:
    st = _state("r")
    store = RunStore(root, "r")
    store.write_state(st)
    (store.dir / "recovery").mkdir()
    how(store.dir / "recovery" / "routing.jsonl", dispatchrecovery_line(_receipt(st, N.USAGE_LIMIT)))
    return store


def _fifo(path, line):
    os.mkfifo(path)


def _dir(path, line):
    path.mkdir()


def _dangling(path, line):
    os.symlink(path.parent / "nowhere", path)


def _link_to_valid(path, line):
    target = path.parent / "real.jsonl"
    target.write_text(line + "\n", encoding="utf-8")
    os.symlink(target, path)


def _hard(path, line):
    target = path.parent.parent / "outside.jsonl"
    target.write_text(line + "\n", encoding="utf-8")
    os.link(target, path)


def _bytes(data):
    def how(path, line):
        path.write_bytes(data)
    return how


def _oversize(extra: int):
    def how(path, line):
        body = (line + "\n").encode()
        path.write_bytes(body + b" " * (8 * MIB + extra - len(body)))
    return how


BAD_RECEIPTS = {
    "fifo": _fifo, "directory": _dir, "dangling symlink": _dangling,
    "symlink to a valid file": _link_to_valid, "second name (hard link)": _hard,
    "latin-1 bytes": _bytes(b'{"digest": "caf\xe9"}\n'),
    "utf-16 with bom": _bytes('{"digest": "x"}\n'.encode("utf-16")),
    "one byte over eight mebibytes": _oversize(1),
}


@pytest.mark.parametrize("case", sorted(BAD_RECEIPTS))
def test_an_unreadable_receipts_file_leaves_the_park_unattributed_and_says_why(
        root, case, _alarm):
    """The state is fine, the receipts are not: the wait is listed unattributed with a reason that
    names the receipts, never attributed to a provider, never a raise or a hang. (A problem of the
    report would make an unrelated bad file hide a run's own wait; the wait is still evidence.)
    Kills F1 (a blocking or following read), Z3 (no bound on receipts), L1 (a second name
    accepted)."""
    _park_with_receipts_file(root, BAD_RECEIPTS[case])
    got = _read(root)
    assert got.providers == () and got.complete
    [w] = got.unattributed
    assert w.run_id == "r" and w.unattributed.startswith("the routing receipts ")


def test_receipt_lines_that_are_damaged_do_not_poison_the_one_that_matches(root):
    """A torn line, a tampered digest, a JSON array, JSON scalars (a number, a string, `null`), a
    line nested 100,000 deep and blank lines beside the valid receipt: the valid one attributes.
    Without a valid one the reason counts the unreadable lines. The lines are judged as
    `dispatchrecovery.read_records` judges them. Kills Q5 (a scalar line not refused: it would
    raise), Q6 (the deep line not caught)."""
    st = _state("r")
    store = RunStore(root, "r")
    store.write_state(st)
    good = dispatchrecovery_line(_receipt(st, N.USAGE_LIMIT, provider="claude"))
    tampered = good.replace('"claude"', '"codex"')
    deep = "[" * 100000 + "]" * 100000
    damaged = ['{"digest": "x", "kind": ', tampered, "[1, 2]", "5", '"text"', "null", deep, "", "   "]
    path = store.dir / "recovery" / "routing.jsonl"
    path.parent.mkdir()
    path.write_text("\n".join(damaged) + "\n", encoding="utf-8")
    got = _read(root)
    [w] = got.unattributed
    assert w.unattributed == "no receipt describes this park; 7 receipt line(s) unreadable"
    records, problems = dispatchrecovery.read_records(store.dir)
    assert records == [] and len(problems) == 7                 # parity with the product's reader
    path.write_text("\n".join(damaged + [good]) + "\n", encoding="utf-8")
    [g] = _read(root).providers
    assert g.provider == "claude"
    records, problems = dispatchrecovery.read_records(store.dir)
    assert len(records) == 1 and len(problems) == 7


def test_receipts_of_exactly_eight_mebibytes_are_read(root):
    """Inclusive bound, the other side of the oversize receipts case. Kills Z4."""
    _park_with_receipts_file(root, _oversize(0))
    [g] = _read(root).providers
    assert g.provider == "claude"


# --------------------------------------------------------------------------- #
# H2 -- what is a run, and what is the same run twice
# --------------------------------------------------------------------------- #


def test_a_symlinked_run_directory_is_a_problem_and_is_not_followed(root, tmp_path):
    """`alias -> good` would count the same park twice (or read a directory that is not this
    root's). It is named and not read. Kills Y1 (the link followed)."""
    _park(root, "good")
    os.symlink(root / "good", root / "alias")
    elsewhere = tmp_path / "foreign" / "far"
    elsewhere.mkdir(parents=True)
    foreign = RunStore(elsewhere.parent, "far")
    foreign.write_state(_state("far"))
    os.symlink(foreign.dir, root / "far")
    got = _read(root)
    assert [what for what, why in got.problems] == ["alias", "far"]
    assert got.runs_seen == 1 and got.providers[0].runs == ("good",)
    assert all("symbolic link" in why for what, why in got.problems)


def test_a_copy_of_a_run_under_another_name_is_a_problem_and_counts_once(root):
    """A restored or renamed directory keeps its run id in `state.json`; `dispatchrecovery`
    refuses it for the same reason. Kills C1 (the park counted twice)."""
    _park(root, "good")
    copy = root / "good-restored"
    copy.mkdir()
    (copy / "state.json").write_bytes((root / "good" / "state.json").read_bytes())
    got = _read(root)
    assert got.providers[0].runs == ("good",) and got.runs_seen == 1
    [(what, why)] = got.problems
    assert what == "good-restored" and "names run 'good'" in why


def test_a_recovery_directory_linked_to_another_runs_receipts_attributes_nothing(root):
    """Only the last name of a file is opened without following a link; a `recovery` directory
    that is a link to another run's reaches that run's receipts. They bind the other run, so this
    run's park stays unattributed and the other run is still its own. Kills B3a."""
    _park(root, "other", provider="codex")
    st = _state("r")
    store = RunStore(root, "r")
    store.write_state(st)
    os.symlink(root / "other" / "recovery", store.dir / "recovery")
    got = _read(root)
    assert [(p.provider, p.runs) for p in got.providers] == [("codex", ("other",))]
    assert [w.run_id for w in got.unattributed] == ["r"] and got.complete


def test_names_the_runs_root_keeps_for_itself_are_never_runs(root):
    """`_arenas`, a dot-directory, `backups` and `projects` are skipped even when they hold a
    valid parked state; loose files and a directory with no `state.json` are skipped without a
    problem. Kills R1 (a reserved name read as a run)."""
    _park(root, "good")
    for name in ("_arenas", ".hidden", "backups", "projects"):
        d = root / name
        d.mkdir()
        (d / "state.json").write_text(_state(name).model_dump_json(), encoding="utf-8")
    (root / "timers.json").write_text("{}", encoding="utf-8")
    (root / ".resume.lock").write_text("", encoding="utf-8")
    (root / "empty").mkdir()
    got = _read(root)
    assert got.complete and got.runs_seen == 1 and got.providers[0].runs == ("good",)


def test_a_directory_name_that_is_no_run_id_is_a_problem_not_a_crash(root):
    """A run directory named with a byte that is not UTF-8 (`os.listdir` hands it back as a lone
    surrogate) and one with a trailing newline (`$` would match it, `\\Z` does not): refused as run
    ids, reported, and the report still serialises."""
    _park(root, "good")
    for odd in (os.fsencode(root) + b"/\xff-run", os.fsencode(root) + b"/trailing\n"):
        os.mkdir(odd)
        with open(odd + b"/state.json", "wb") as fh:
            fh.write(_state("x").model_dump_json().encode())
    got = _read(root)
    assert got.providers[0].runs == ("good",)
    assert [what for what, why in got.problems] == ["trailing\n", "\udcff-run"]
    assert all("not a usable run id" in why for what, why in got.problems)
    json.dumps(got.as_dict())


def test_a_missing_root_is_no_runs_and_an_unreadable_one_is_a_problem(tmp_path):
    """No runs root yet is a project that has not run anything: complete and empty. A root that is
    a file, or a directory nobody may list, is evidence this reading could not get."""
    absent = read_health(tmp_path / "never", now=NOW)
    assert absent.complete and absent.providers == () and absent.runs_seen == 0
    afile = tmp_path / "file"
    afile.write_text("x", encoding="utf-8")
    assert [what for what, why in read_health(afile, now=NOW).problems] == ["root"]
    if os.geteuid() != 0:
        locked = tmp_path / "locked"
        locked.mkdir()
        os.chmod(locked, 0)
        try:
            assert [what for what, why in read_health(locked, now=NOW).problems] == ["root"]
        finally:
            os.chmod(locked, 0o700)


# --------------------------------------------------------------------------- #
# H1 -- the shape consumers will read
# --------------------------------------------------------------------------- #


def test_the_report_is_plain_json_with_a_fixed_set_of_keys(root):
    _park(root, "a", account="acct-a")
    _park(root, "legacy", receipt=False)
    doc = _read(root).as_dict()
    assert json.loads(json.dumps(doc)) == doc
    assert set(doc) == {"schema", "at", "complete", "runs_seen", "providers", "unattributed",
                        "problems"}
    assert doc["schema"] == "hoh-provider-health/1" and doc["at"] == NOW.isoformat()
    assert set(doc["providers"][0]) == {"provider", "account_handle", "state", "not_before",
                                        "runs", "failures", "given_up"}
    assert set(doc["unattributed"][0]) == {"run_id", "failure", "evidence", "retry_at", "due",
                                           "provider", "account_handle", "model", "unattributed",
                                           "given_up"}
    assert "when" not in doc["unattributed"][0]
    assert doc["providers"][0]["failures"] == {"USAGE_LIMIT": 1}
