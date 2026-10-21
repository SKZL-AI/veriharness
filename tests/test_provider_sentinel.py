"""P3-08, second increment: the runs of one account wake together (`ProviderHealthSentinel`).

`hoh.health.ProviderHealthSentinel` is the first consumer that acts on the cross-run reading, and
`hoh.resumeloop.resume_runs` -- the pass behind `hoh resume-quota` -- is the code that consults it.
These tests build real runs under `tmp_path` through the product's own `RunStore`, `RunState`,
`dispatchrecovery.append_record` and `TimerStore`, run the real pass, and read the records and the
state back. One test goes through `hoh.cli.main(["resume-quota"])`. Nothing starts a process,
Herdr or an agent (any `subprocess` use fails the test); no test sleeps (`now` is injected
everywhere except the command, whose stamps are written relative to the clock it reads).

Mutant ids in a docstring (`M1`..) are the spec's table of mutations the integration review
applies; each test says which it kills. Every number below is a literal on purpose: a value
imported from the module would agree with a mutated module.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hoh import cli, dispatchrecovery, health, quota, resumeloop
from hoh.contracts import Condition, RunState, Stage
from hoh.dispatchrecovery import (
    NO_MASTER, DispatchProvenance, MasterBinding, ParkBinding, ParkCheck, ParkVerdict,
    RoutingDecision, claim_path, park_id,
)
from hoh.failures import NormalizedFailure
from hoh.health import Action, Consultation, ProviderHealthSentinel
from hoh.store import RunStore
from hoh.telemetry import NOT_AVAILABLE
from hoh.timers import TimerKind, TimerStatus, TimerStore

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
PAST = (NOW - timedelta(hours=1)).isoformat()
SOON = (NOW + timedelta(hours=1)).isoformat()
LATER = (NOW + timedelta(hours=3)).isoformat()
N = NormalizedFailure


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


class _Blocked(BaseException):
    """Not an `OSError`: the code under test has `except OSError` clauses that would swallow it."""


@contextlib.contextmanager
def _within(seconds: int):
    """A pass that blocks (a FIFO opened for reading) fails the test instead of hanging it."""
    def alarm(*args):
        raise _Blocked(f"blocked for more than {seconds} s")

    previous = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


# --------------------------------------------------------------------------- #
# Builders: real parked runs with the receipt the controller writes
# --------------------------------------------------------------------------- #


def _timer_form(stamp: str) -> str:
    return datetime.fromisoformat(stamp).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _park(root: Path, run_id: str, *, failure: N = N.USAGE_LIMIT, retry: str = PAST,
          provider: str | None = "claude", account: str = "acct-a", model: str = "m-1",
          attempts: int = 0, candidate: str = "NONE", timer: bool = True,
          governed: bool | tuple[str, int] = False) -> RunStore:
    """A run parked on `failure` that resets at `retry`: its state, the receipt that describes
    the park (none with `provider=None`), and -- unless `timer=False` -- a PROVIDER_RETRY timer
    due at `retry`. `candidate` other than NONE makes a receipt that the reading attributes but
    `verify_binding` refuses (the candidate the park stands on is not the one it names).
    `governed=True` or `(session, epoch)` declares a governed master (`recovery/master.json`)
    that the receipt binds."""
    st = RunState(run_id=run_id, repo_path="/arena/repo", project_name="demo",
                  spec_path="/arena/spec.md", spec_digest="s", policy_digest="p",
                  profile_digest="h", stage=Stage.DEVELOPING, condition=Condition.BLOCKED,
                  iteration=1)
    st.blocked_kind = failure.value.lower()
    st.blocked_reason = "parked"
    st.retry_after = retry
    st.resume_attempts = attempts
    store = RunStore(root, run_id)
    store.write_state(st)
    session, epoch = governed if isinstance(governed, tuple) else ("sess-1", 1)
    master = MasterBinding(kind="GOVERNED", epoch=epoch, session=session) if governed else NO_MASTER
    if governed:
        (store.dir / "recovery").mkdir(parents=True, exist_ok=True)
        (store.dir / "recovery" / "master.json").write_text(
            json.dumps({"kind": "GOVERNED", "epoch": epoch, "session": session}))
    if provider is not None:
        binding = ParkBinding(programme="NONE", program_epoch=0, run_id=run_id,
                              iteration=st.iteration, attempt=st.attempt,
                              stage=st.stage.value, candidate=candidate, master=master)
        dispatchrecovery.append_record(store.dir, RoutingDecision(
            park_id=park_id(binding, failure, retry), reason=failure.value, binding=binding,
            provenance=DispatchProvenance(
                provider=provider, model=model, account_handle=account, role="developer",
                capability_requirements=["read_repo", "write_worktree", "run_shell"]),
            retry_at=retry))
    if timer:
        TimerStore(root).arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=_timer_form(retry),
                                 reason="usage limit", program_epoch=0, run_id=run_id)
    return store


def _by_target(records):
    return {r["target"]: r for r in records if r.get("scope") == "run"}


def _unchanged(store: RunStore) -> None:
    st = store.read_state()
    assert st.condition is Condition.BLOCKED and st.resume_attempts == 0
    assert not (store.dir / "recovery" / "claims").exists()


def _armed(root: Path, run_id: str) -> bool:
    return any(t.run_id == run_id and t.status is TimerStatus.ARMED for t in TimerStore(root).read())


def _tree(path: Path, *, locks: bool = True) -> dict[str, tuple[bytes, int]]:
    """Every path under `path`; a file with its bytes and modification time, a directory by
    its name alone (a lock file made in it moves its time). `locks=False`: without the `.lock`
    files, which the loop's own per-run lock creates."""
    out = {}
    for p in sorted(path.rglob("*")):
        if not locks and p.name.endswith(".lock"):
            continue
        out[str(p.relative_to(path))] = ((p.read_bytes(), p.lstat().st_mtime_ns) if p.is_file()
                                         else (b"", 0))
    return out


# --------------------------------------------------------------------------- #
# The behaviour: a due run waits for the limit another run of its account states
# --------------------------------------------------------------------------- #


def test_a_due_run_is_held_while_another_run_of_its_account_states_a_pending_limit(root):
    """`a` is due; `b` of the same provider, account and model states a limit that resets in
    three hours. The pass lifts nothing: `a` is `held_for_provider` until `b`'s reset, its state,
    its armed timer and its receipts are untouched, and `b` is never touched. Kills M1 (no
    consultation), M19 (the clock read instead of the injected `now`)."""
    a = _park(root, "a", retry=PAST)
    b = _park(root, "b", retry=LATER)
    got = _by_target(resumeloop.resume_runs(root, now=NOW))
    assert got["a"]["action"] == "held_for_provider"
    assert got["a"]["until"] == LATER and got["a"]["evidence"] == ["b"]
    assert got["a"]["binding"] == "BOUND" and "wake together" in got["a"]["why"]
    _unchanged(a)
    assert _armed(root, "a")
    assert got["b"]["verdict"] == "NOT_DUE" and got["b"]["action"] == "refused"
    _unchanged(b)


def test_the_latest_pending_reset_decides_how_long_a_run_waits(root):
    """Two runs state a pending limit, one resets in one hour and one in three: `a` waits for
    the later. Kills M2 (`min` of the evidence)."""
    _park(root, "a", retry=PAST)
    _park(root, "b", retry=SOON)
    _park(root, "c", retry=LATER)
    got = _by_target(resumeloop.resume_runs(root, now=NOW))
    assert got["a"]["action"] == "held_for_provider"
    assert got["a"]["until"] == LATER and got["a"]["evidence"] == ["b", "c"]


def test_the_runs_of_one_account_wake_together_at_the_shared_reset(root):
    """Once `b`'s stated reset has passed nothing is pending: both runs are due and the same
    pass lifts both (attempt 1 each, their timers fired, their parks claimed) -- the account
    woke as one. Kills M3 (a hold that never lapses: the evidence not required to be pending)."""
    a = _park(root, "a", retry=PAST)
    b = _park(root, "b", retry=LATER)
    resumeloop.resume_runs(root, now=NOW)
    got = _by_target(resumeloop.resume_runs(root, now=NOW + timedelta(hours=3, seconds=1)))
    assert got["a"]["action"] == "resumed" and got["b"]["action"] == "resumed"
    assert got["a"]["attempt"] == 1 and got["a"]["claim"] == "written"
    for store in (a, b):
        st = store.read_state()
        assert st.condition is not Condition.BLOCKED and st.resume_attempts == 1
    assert not _armed(root, "a") and not _armed(root, "b")


def test_the_retry_after_fallback_holds_the_same_way(root):
    """A park with no timer (the legacy path, `_legacy_run`) is held by the same rule, and is
    lifted as `resumed_without_timer` once the evidence has elapsed. Kills M4 (the consultation
    on the timer path only)."""
    a = _park(root, "a", retry=PAST, timer=False)
    _park(root, "b", retry=LATER, timer=False)
    held = _by_target(resumeloop.resume_runs(root, now=NOW))["a"]
    assert held["action"] == "held_for_provider" and held["until"] == LATER
    assert held["verdict"] == "LEGACY_RETRY_AFTER"
    _unchanged(a)
    later = _by_target(resumeloop.resume_runs(root, now=NOW + timedelta(hours=3, seconds=1)))
    assert later["a"]["action"] == "resumed_without_timer"


@pytest.mark.parametrize("other", [
    dict(account="acct-b"),
    dict(provider="codex"),
    dict(model="m-2"),
    dict(failure=N.PROVIDER_UNAVAILABLE),
    dict(failure=N.NETWORK_TRANSIENT),
    dict(retry=PAST),
    dict(attempts=5),
    dict(provider=None),
    dict(provider="Claude"),
    dict(account="ACCT-A"),
], ids=["account", "provider", "model", "availability", "network", "elapsed", "given-up",
        "no-receipt", "provider-case", "account-case"])
def test_only_a_pending_limit_of_the_same_provider_account_and_model_is_evidence(root, other):
    """Another account, another provider, another model, an availability failure, a reset that
    has passed, a park the loop gave up on and a park with no receipt each say nothing about
    this run's account (the key is compared exactly: a case makes another key): `a` is lifted as it always was. Kills M20 (the key without the account),
    M21 (without the provider), M5 (without the model), M6 (every class counted as a limit), M3
    (an elapsed wait counted), M7 (a given-up wait counted)."""
    a = _park(root, "a", retry=PAST)
    spec = dict(retry=LATER)
    spec.update(other)
    _park(root, "b", **spec)
    got = _by_target(resumeloop.resume_runs(root, now=NOW))
    assert got["a"]["action"] == "resumed", got["a"]
    assert a.read_state().condition is not Condition.BLOCKED


def test_two_runs_with_no_registered_account_are_one_account(root):
    """The declared limit of the reading: with no account registered the receipt says
    NOT_AVAILABLE and the key is (provider, NOT_AVAILABLE, model). Two runs of one harness
    profile are held together. Pinned so that a change of the key is a decision."""
    _park(root, "a", retry=PAST, account=NOT_AVAILABLE, model=NOT_AVAILABLE)
    _park(root, "b", retry=LATER, account=NOT_AVAILABLE, model=NOT_AVAILABLE)
    got = _by_target(resumeloop.resume_runs(root, now=NOW))
    assert got["a"]["action"] == "held_for_provider"


def test_a_run_nobody_can_name_the_provider_of_is_lifted_as_before(root):
    """`a` has no receipt (a park of slice a): the sentinel has no opinion and the loop resumes it
    as before, say `LEGACY_UNBOUND`, even while `b` states a limit for `claude`; and two such
    runs are not one provider (two unnamed providers are not one)."""
    _park(root, "a", retry=PAST, provider=None)
    _park(root, "b", retry=LATER)
    got = _by_target(resumeloop.resume_runs(root, now=NOW))
    assert got["a"]["action"] == "resumed" and got["a"]["binding"] == "LEGACY_UNBOUND"
    _park(root, "c", retry=PAST, provider=None, timer=False)
    _park(root, "d", retry=LATER, provider=None, timer=False)
    later = _by_target(resumeloop.resume_runs(root, now=NOW))
    assert later["c"]["action"] == "resumed_without_timer"


# --------------------------------------------------------------------------- #
# Evidence is verified; refusals come first; ceilings
# --------------------------------------------------------------------------- #


def test_a_pending_limit_whose_park_no_longer_verifies_is_not_evidence(root):
    """`b`'s receipt names a candidate the park does not stand on. The reading (slice one)
    attributes it -- it is a limit of `claude`/`acct-a` -- but `verify_binding` refuses it, so
    it does not keep `a` waiting. Kills M8 (the evidence not verified)."""
    _park(root, "a", retry=PAST)
    _park(root, "b", retry=LATER, candidate="deadbeef")
    [group] = health.read_health(root, now=NOW).providers
    assert group.runs == ("a", "b") and group.state.value == "LIMITED"
    got = _by_target(resumeloop.resume_runs(root, now=NOW))
    assert got["a"]["action"] == "resumed"


def test_a_binding_refusal_of_the_run_itself_is_not_softened_into_a_hold(root):
    """`a` is attributed (it would be held) but its own park does not verify: the loop's
    `refused` stands, with the binding reason, and nothing is said about `b`. Kills M9 (the
    consultation before the binding check)."""
    a = _park(root, "a", retry=PAST, candidate="deadbeef")
    _park(root, "b", retry=LATER)
    got = _by_target(resumeloop.resume_runs(root, now=NOW))
    assert got["a"]["action"] == "refused" and got["a"]["binding"] == "REFUSED"
    _unchanged(a)


def test_a_run_the_loop_gives_up_on_is_given_up_not_held(root):
    """At the attempt limit the loop's own `given_up` (with its escalation) stands even while a
    limit of the account is pending, and the sentinel has no word about such a run. Kills M18
    (a given-up run held)."""
    _park(root, "a", retry=PAST, attempts=5)
    _park(root, "b", retry=LATER)
    assert ProviderHealthSentinel(root, now=NOW).consult("a").action is Action.PROCEED
    got = _by_target(resumeloop.resume_runs(root, now=NOW))
    assert got["a"]["action"] == "given_up"
    assert got["a"]["escalation"]["escalation"] == "RETRIES_EXHAUSTED"


def test_a_reset_beyond_seventy_two_hours_is_not_evidence(root):
    """The ceiling is exact: a stated reset 72 h away holds, one second more does not (a forged
    or mistyped stamp must not park an account's runs for good). Kills M10 (the ceiling
    exclusive), M11 (the ceiling dropped or doubled)."""
    assert health.MAX_HOLD == quota._MAX_STATED_WAIT == timedelta(hours=72)
    edge = (NOW + timedelta(hours=72)).isoformat()
    over = (NOW + timedelta(hours=72, seconds=1)).isoformat()
    _park(root, "a", retry=PAST)
    _park(root, "b", retry=edge)
    assert _by_target(resumeloop.resume_runs(root, now=NOW))["a"]["action"] == "held_for_provider"
    _park(root, "c", retry=PAST, account="acct-c")
    _park(root, "d", retry=over, account="acct-c")
    assert _by_target(resumeloop.resume_runs(root, now=NOW))["c"]["action"] == "resumed"


def test_a_fallback_run_that_is_not_due_is_waiting_not_held(root):
    """On the `retry_after` path the sentinel is asked after `quota.is_due` (which reads the wall
    clock): a run whose own reset is an hour away is `waiting` with the loop's own words, even
    while another run of the account states a later limit. Kills M22 (the consultation before
    the due check)."""
    clock = datetime.now(timezone.utc)
    a = _park(root, "a", retry=(clock + timedelta(hours=1)).isoformat(), timer=False)
    _park(root, "b", retry=(clock + timedelta(hours=2)).isoformat(), timer=False)
    got = _by_target(resumeloop.resume_runs(root, now=clock))
    assert got["a"]["action"] == "waiting" and got["a"]["verdict"] == "LEGACY_RETRY_AFTER"
    _unchanged(a)


# --------------------------------------------------------------------------- #
# What it never does
# --------------------------------------------------------------------------- #


def test_a_reading_that_fails_is_no_opinion_and_is_said(root, monkeypatch):
    """A sentinel that cannot read lets the run through, as before, and the record says the
    consultation did not happen -- a pass that consulted nothing is not one that found nothing.
    Kills M12 (the failure propagates and stops the pass), M13 (the failure is silent)."""
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(health, "_collect", broken)
    _park(root, "a", retry=PAST)
    _park(root, "b", retry=LATER)
    got = _by_target(resumeloop.resume_runs(root, now=NOW))
    assert got["a"]["action"] == "resumed"
    assert got["a"]["provider_health"].startswith("not consulted: provider health could not be read")
    assert "boom" not in got["a"]["provider_health"]


def test_a_pass_reads_the_root_once_and_only_when_something_is_due(root, monkeypatch):
    """One reading per pass, however many runs are consulted; none when nothing is due. Kills
    M14 (a reading per consultation), M15 (an eager reading at the start of every pass)."""
    calls = []
    real = health._collect

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(health, "_collect", counting)
    _park(root, "b", retry=LATER)
    resumeloop.resume_runs(root, now=NOW)
    assert calls == []                       # nothing was due: nothing was read
    _park(root, "a1", retry=PAST)
    _park(root, "a2", retry=PAST)
    resumeloop.resume_runs(root, now=NOW)
    assert calls == [1]                      # two consultations, one reading


def test_holding_a_run_and_consulting_write_nothing(root):
    """The sentinel alone leaves the whole tree as it was (every path, bytes and modification
    time); a pass that holds changes no file of any run and no timer either (the only paths a pass
    makes are the `.lock` files of its own locks). Kills M16 (a hold that writes a note or
    re-arms the timer)."""
    _park(root, "a", retry=PAST)
    _park(root, "b", retry=LATER)
    before = _tree(root)
    ProviderHealthSentinel(root, now=NOW).consult("a")
    assert _tree(root) == before
    resumeloop.resume_runs(root, now=NOW)
    assert _tree(root, locks=False) == {k: v for k, v in before.items()
                                        if not k.endswith(".lock")}


def test_the_consultation_has_a_fixed_shape_and_refuses_a_naive_now(root):
    """`consult` answers with `Consultation`: HOLD names the latest reset and the runs; a run
    nobody holds back says PROCEED with no reset (a run is never its own evidence: kills M17); a
    naive `now` is refused (comparing it with a stated reset is a guess)."""
    _park(root, "a", retry=PAST)
    _park(root, "b", retry=LATER)
    sentinel = ProviderHealthSentinel(root, now=NOW)
    held = sentinel.consult("a")
    assert held.action is Action.HOLD and held.not_before == LATER and held.evidence == ("b",)
    assert held.available and set(held.as_dict()) == {"action", "reason", "not_before",
                                                      "evidence", "available"}
    free = sentinel.consult("b")                 # b's own limit is pending; nothing else is
    assert free.action is Action.PROCEED and free.not_before is None and free.evidence == ()
    assert sentinel.consult("no-such-run").action is Action.PROCEED
    with pytest.raises(ValueError):
        ProviderHealthSentinel(root, now=datetime(2026, 10, 12, 12, 0, 0))


def test_an_unreadable_or_linked_neighbour_is_no_evidence_and_the_pass_goes_on(root, tmp_path):
    """A run whose `state.json` is not JSON and a run directory that is a symbolic link to a
    perfectly good pending limit are problems of the reading (slice one), never evidence: `a`
    is lifted as before and the pass reports it. The sentinel fails open, so what it cannot
    read can only mean fewer holds, never a hold on a guess."""
    a = _park(root, "a", retry=PAST)
    broken = _park(root, "b", retry=LATER)
    broken.state_path.write_text("{not json")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _park(elsewhere, "c", retry=LATER)
    (root / "c").symlink_to(elsewhere / "c", target_is_directory=True)
    assert sorted(what for what, why in health.read_health(root, now=NOW).problems) == ["b", "c"]
    got = _by_target(resumeloop.resume_runs(root, now=NOW))
    assert got["a"]["action"] == "resumed"
    assert a.read_state().condition is not Condition.BLOCKED


def test_a_name_with_control_characters_is_echoed_escaped(root):
    """A provider or model name is the receipt's string; the reason that echoes it carries it
    escaped (`repr`), never as a raw line break or NUL into the command's JSON or a log."""
    odd = "cla\nude\x00"
    _park(root, "a", retry=PAST, provider=odd)
    _park(root, "b", retry=LATER, provider=odd)
    held = ProviderHealthSentinel(root, now=NOW).consult("a")
    assert held.action is Action.HOLD
    assert "\n" not in held.reason and "\x00" not in held.reason and "\\n" in held.reason


def test_a_single_named_run_is_judged_against_the_whole_root(root):
    """`hoh resume-quota RUN` looks at one run, but the account's limit is stated by the others:
    the named run is held all the same (the argument `only` is forwarded, not the reading)."""
    _park(root, "a", retry=PAST)
    _park(root, "b", retry=LATER)
    [rec] = resumeloop.resume_runs(root, now=NOW, only="a")
    assert rec["action"] == "held_for_provider" and rec["target"] == "a"


def _spoil(path: Path, kind: str, tmp_path: Path) -> None:
    """Make `path` a FIFO, a symbolic link to a regular file, or a directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        target = tmp_path / "linked-target"
        target.write_text("{}")
        path.symlink_to(target)
    else:
        path.mkdir()


@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory"])
@pytest.mark.parametrize("which", ["claim", "master", "routing"])
def test_a_neighbour_with_an_odd_file_where_verification_looks_is_no_evidence_and_never_blocks(
        root, tmp_path, which, kind):
    """The sentinel verifies *other* runs' parks, which the loop never opened while they were not
    due: the park's claim, the master record, the routing receipts. A FIFO (or a link, or a
    directory) at one of them must not block the pass for ever under `.resume.lock`: the pass
    completes within the bound with the verdicts the base gives (`a` lifted, `b` refused), and the
    neighbour is not evidence. Kills M23 (the look-before-open dropped: the pass hangs)."""
    a = _park(root, "a", retry=PAST)
    b = _park(root, "b", retry=LATER)
    [rec] = dispatchrecovery.read_records(b.dir)[0]
    paths = {"claim": claim_path(b.dir, rec.park_id),
             "master": b.dir / "recovery" / "master.json",
             "routing": b.dir / "recovery" / "routing.jsonl"}
    if which == "routing":
        os.replace(paths[which], tmp_path / "moved-routing")
    _spoil(paths[which], kind, tmp_path)
    with _within(20):
        got = _by_target(resumeloop.resume_runs(root, now=NOW))
    assert got["a"]["action"] == "resumed", got["a"]
    assert got["b"]["action"] == "refused"       # the loop's own word for it, as on the base
    assert a.read_state().condition is not Condition.BLOCKED


@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory"])
@pytest.mark.parametrize("which", ["witness", "contract", "autonomy-log"])
def test_the_files_the_contract_check_opens_are_looked_at_before_they_are_opened(
        root, tmp_path, which, kind):
    """`verify_binding` binds the autonomy contract of the run it verifies: the pinned contract,
    its witness and the autonomy routing log. Asked about `a` with a neighbour `b` whose park is
    otherwise good evidence, the sentinel answers within the bound and answers PROCEED -- `b` is
    no evidence while one of those is not a regular single-name file. (The loop itself opens
    `b`'s contract through `run_epoch` for an armed neighbour; that is the base's, tracked, and
    this test asks the sentinel alone.) Kills M23."""
    _park(root, "a", retry=PAST)
    b = _park(root, "b", retry=LATER)
    assert ProviderHealthSentinel(root, now=NOW).consult("a").action is Action.HOLD   # the control
    paths = {"witness": b.dir / "approval" / "autonomy.json",
             "contract": b.dir / "autonomy" / "contract.json",
             "autonomy-log": b.dir / "autonomy" / "routings.jsonl"}
    _spoil(paths[which], kind, tmp_path)
    with _within(5):
        got = ProviderHealthSentinel(root, now=NOW).consult("a")
    assert got.action is Action.PROCEED and got.evidence == (), got


def test_a_claims_directory_with_a_hostile_entry_is_no_evidence(root, tmp_path):
    """The claims directory is looked through whole (the park id the verifier will pick is its
    business): a FIFO under another park's name blocks nothing and removes the neighbour from
    the evidence. Kills M23."""
    _park(root, "a", retry=PAST)
    b = _park(root, "b", retry=LATER)
    _spoil(b.dir / "recovery" / "claims" / "x.json", "fifo", tmp_path)
    with _within(20):
        got = _by_target(resumeloop.resume_runs(root, now=NOW))
    assert got["a"]["action"] == "resumed"


def test_the_bounds_of_the_look_are_exact(root):
    """Both bounds of the pre-check, on both sides: a witness of exactly 8 MiB is looked through
    and one byte more makes the neighbour no evidence; a claims directory of 1024 entries is
    looked through and 1025 is not. Kills M26 (the entry bound dropped), M27 (the size bound
    exclusive), M28 (the size bound doubled)."""
    _park(root, "a", retry=PAST)
    b = _park(root, "b", retry=LATER)
    witness = b.dir / "approval" / "autonomy.json"
    witness.parent.mkdir(parents=True, exist_ok=True)
    with open(witness, "wb") as fh:
        fh.truncate(8 * 1024 * 1024)
    assert ProviderHealthSentinel(root, now=NOW).consult("a").action is Action.HOLD
    with open(witness, "r+b") as fh:
        fh.truncate(8 * 1024 * 1024 + 1)
    assert ProviderHealthSentinel(root, now=NOW).consult("a").action is Action.PROCEED
    with open(witness, "r+b") as fh:
        fh.truncate(0)
    claims = b.dir / "recovery" / "claims"
    claims.mkdir(parents=True)
    for n in range(1024):
        (claims / f"p{n}.json").write_text("{}")
    assert ProviderHealthSentinel(root, now=NOW).consult("a").action is Action.HOLD
    (claims / "p1024.json").write_text("{}")
    assert ProviderHealthSentinel(root, now=NOW).consult("a").action is Action.PROCEED


def test_a_run_whose_master_record_does_not_say_is_outside_on_both_sides(root, monkeypatch):
    """`recovery/master.json` of a kind that is neither NONE nor GOVERNED "does not say" whether
    the run is governed: the run is not consulted and is not evidence. Verification is stubbed to
    BOUND so that the rule under test is the sentinel's own. Kills G1 (an unsaid record read as a
    plain run), G2 (an unsaid run consulted)."""
    monkeypatch.setattr(health, "verify_binding", lambda run_dir, state, now=None: ParkCheck(
        ParkVerdict.BOUND, "stub"))
    _park(root, "a", retry=PAST)
    b = _park(root, "b", retry=LATER)
    assert ProviderHealthSentinel(root, now=NOW).consult("a").action is Action.HOLD   # control
    (b.dir / "recovery").mkdir(exist_ok=True)
    (b.dir / "recovery" / "master.json").write_text(json.dumps({"kind": "SOMEONE"}))
    assert ProviderHealthSentinel(root, now=NOW).consult("a").action is Action.PROCEED
    c = _park(root, "c", retry=PAST, account="acct-c")
    _park(root, "d", retry=LATER, account="acct-c")
    (c.dir / "recovery").mkdir(exist_ok=True)
    (c.dir / "recovery" / "master.json").write_text(json.dumps({"kind": "SOMEONE"}))
    got = ProviderHealthSentinel(root, now=NOW).consult("c")
    assert got.action is Action.PROCEED and "does not say" in got.reason


# --------------------------------------------------------------------------- #
# The production path: `hoh resume-quota`
# --------------------------------------------------------------------------- #


def test_hoh_resume_quota_holds_the_run_and_says_so(root, capsys):
    """The command a systemd timer runs (`hoh resume-quota`, `cli.cmd_resume_quota` ->
    `resumeloop.resume_runs`): with a run of one account due and another stating a limit that
    resets in two hours (stamps written relative to the clock the command reads), the printed
    result says `held_for_provider` and the run is not resumed. Kills M1 through the product's own
    entry point."""
    clock = datetime.now(timezone.utc)
    a = _park(root, "a", retry=(clock - timedelta(hours=1)).isoformat())
    _park(root, "b", retry=(clock + timedelta(hours=2)).isoformat())
    assert cli.main(["--root", str(root), "resume-quota"]) == 0
    printed = json.loads(capsys.readouterr().out)
    held = [r for r in printed["results"] if r.get("target") == "a"]
    assert [r["action"] for r in held] == ["held_for_provider"]
    assert held[0]["evidence"] == ["b"]
    _unchanged(a)
    assert _armed(root, "a")


# --------------------------------------------------------------------------- #
# P3-08 slice 3: governed runs are consulted, and count under a live, current master
# --------------------------------------------------------------------------- #

from hoh.epoch import EpochStore                          # noqa: E402
from hoh.orchestrator import node_timer_key               # noqa: E402
from hoh.project import Lifecycle, ProjectState, TaskNode  # noqa: E402
from hoh.projectstore import ProjectStore                 # noqa: E402
from hoh.succession import MasterAuthority                # noqa: E402


def _project(root: Path, pid: str, run_id: str, *, retry: str | None = None,
             lifecycle: Lifecycle = Lifecycle.RUNNING, timer: bool = False) -> ProjectStore:
    """Project `pid` under the runs root with one node (`n-<run>`) that runs `run_id`; a BLOCKED
    node carries `blocked_kind=usage_limit` and `retry` and, with `timer`, its armed
    PROVIDER_RETRY timer in the project's own timer store."""
    project = ProjectStore(root, pid)
    blocked = lifecycle is Lifecycle.BLOCKED
    project.create(ProjectState(project_id=pid, repo_path=str(root.parent), nodes=[TaskNode(
        id=f"n-{run_id}", run_id=run_id, lifecycle=lifecycle,
        blocked_kind="usage_limit" if blocked else None,
        retry_after=_timer_form(retry) if blocked else None)]))
    if blocked and timer:
        TimerStore(project.dir).arm_new(
            kind=TimerKind.PROVIDER_RETRY, due_at=_timer_form(retry), reason="usage limit",
            program_epoch=1, run_id=node_timer_key(f"n-{run_id}"))
    return project


def _master(project: ProjectStore, session: str) -> MasterAuthority:
    authority = MasterAuthority(project.dir, session, serialize=project.lock)
    authority.acquire()
    return authority


def _live_limit(root: Path, *, run_id: str = "y", session: str = "S1", retry: str = LATER,
                **kw) -> MasterAuthority:
    """A governed run parked on a limit that resets in three hours, under a master that is live
    and current: project `p-<run>`, epoch 1 held by `session`, its lease live (this process)."""
    project = _project(root, f"p-{run_id}", run_id, retry=retry, lifecycle=Lifecycle.BLOCKED)
    master = _master(project, session)
    _park(root, run_id, retry=retry, governed=(session, master.epoch), timer=False, **kw)
    return master


def _due_governed(root: Path, *, run_id: str = "x", timer: bool = True, retry: str = PAST,
                  **kw) -> ProjectStore:
    """A governed node of its own project, due, whose master ended its drive (the lease is
    released: the park verifies BOUND and the resume loop may take it)."""
    project = _project(root, f"p-{run_id}", run_id, retry=retry, lifecycle=Lifecycle.BLOCKED,
                       timer=timer)
    master = _master(project, "S0")
    _park(root, run_id, retry=retry, governed=("S0", master.epoch), timer=False, **kw)
    master.release(reason="the drive ended")
    return project


def _node_record(records: list[dict], pid: str) -> dict:
    return next(r for r in records if r.get("target") == pid and r.get("node"))


def test_a_governed_node_is_held_behind_the_limit_of_a_run_under_a_live_master(root):
    """The orchestrated path. `y` is governed and parked on a limit that resets in three hours
    under a live, current master (its park is `DEFERRED`); `x`, of another project, is due and its
    own master has ended its drive. The project pass holds the node `held_for_provider` -- no
    master is acquired for it (the epoch chain is still one epoch), its node stays BLOCKED, its
    timer armed -- and the run pass holds the run the same way. Without `y` the same node is
    resumed (the control). Kills G3 (the project path not consulted), G4 (a deferred park not
    evidence)."""
    control = tmp = root.parent / "control"
    tmp.mkdir()
    cproj = _due_governed(control)
    [rec] = resumeloop.resume_projects(control, now=NOW)
    assert rec["action"] == "resumed" and rec["node"] == "n-x"
    assert cproj.read_state().nodes[0].lifecycle is Lifecycle.READY

    px = _due_governed(root)
    _live_limit(root)
    before = (px.state_path.read_bytes(), EpochStore(px.dir).chain()[-1].epoch)
    [run_x] = [r for r in resumeloop.resume_runs(root, now=NOW) if r.get("target") == "x"]
    assert run_x["action"] == "held_for_provider" and run_x["evidence"] == ["y"]
    out = resumeloop.resume_projects(root, now=NOW)
    held = _node_record(out, "p-x")
    assert held["action"] == "held_for_provider" and held["evidence"] == ["y"]
    assert held["until"] == LATER and held["node"] == "n-x"
    assert (px.state_path.read_bytes(), EpochStore(px.dir).chain()[-1].epoch) == before
    node = px.read_state().nodes[0]
    assert node.lifecycle is Lifecycle.BLOCKED and node.blocked_kind == "usage_limit"
    assert any(t.status is TimerStatus.ARMED for t in TimerStore(px.dir).read())


@pytest.mark.parametrize("how", ["released", "superseded", "never-held", "no-project",
                                 "live-and-current"])
def test_a_governed_park_is_evidence_only_under_a_live_current_master(root, how):
    """The brief's rule, case by case. `y`'s park verifies for its project in every case below,
    yet it does not keep `x` waiting unless its master is live **and** current: a lease the master
    released (the park is BOUND, resumable by the loop, not a live master's), a master that a
    later epoch superseded (DEFERRED to its successor: `stale_master_reason` says stale), a
    declared master the project never held, and a run no project has a node for. Kills G5 (BOUND
    accepted and the lease not judged), G6 (a superseded master counted), G7 (a governed park
    not verified at all: the never-held master, the run no project owns)."""
    _due_governed(root)
    if how == "released":
        _live_limit(root).release(reason="the drive ended")
    elif how == "superseded":
        old = _live_limit(root)
        old.release(reason="the master died")
        _master(ProjectStore(root, "p-y"), "S2")             # a live successor at epoch 2
    elif how == "never-held":
        project = _project(root, "p-y", "y", retry=LATER, lifecycle=Lifecycle.BLOCKED)
        _master(project, "S1")
        _park(root, "y", retry=LATER, governed=("S-other", 1), timer=False)
    elif how == "no-project":
        _park(root, "y", retry=LATER, governed=("S1", 1), timer=False)
    else:
        _live_limit(root)
    got = ProviderHealthSentinel(root, now=NOW).consult("x")
    if how == "live-and-current":
        assert got.action is Action.HOLD and got.evidence == ("y",)      # the control
    else:
        assert got.action is Action.PROCEED, got


def test_a_plain_run_is_held_by_the_limit_of_a_governed_run_under_a_live_master(root):
    """Evidence is about the account, not about who drives the run asking: a plain run `a` waits
    for `y`'s limit as it would for a plain neighbour's (the run pass, timer path)."""
    _park(root, "a", retry=PAST)
    _live_limit(root)
    got = _by_target(resumeloop.resume_runs(root, now=NOW))
    assert got["a"]["action"] == "held_for_provider" and got["a"]["evidence"] == ["y"]
    assert got["y"]["action"] in ("held_by_live_master", "refused")   # the loop's own word for it


def test_a_node_parked_without_a_timer_is_held_the_same_way(root):
    """The project path's fallback branch (`PARKED_WITHOUT_TIMER`, `retry_after` passed): held
    after its run's park verified, nothing acquired. Kills G8 (the fallback branch not
    consulted)."""
    px = _due_governed(root, timer=False)
    _live_limit(root)
    out = resumeloop.resume_projects(root, now=NOW)
    rec = _node_record(out, "p-x")
    assert rec["verdict"] == "PARKED_WITHOUT_TIMER" and rec["action"] == "held_for_provider"
    assert px.read_state().nodes[0].lifecycle is Lifecycle.BLOCKED
    assert len(EpochStore(px.dir).chain()) == 1


def test_the_project_gates_come_first_and_a_node_without_a_run_record_is_not_consulted(root):
    """A node whose run park does not verify stays `refused` with the loop's own words (the
    sentinel is asked after `_run_park_gate`); a node with no run record at all (a project-only
    park) is resumed as before. Kills G9 (the consultation before the park gate)."""
    px = _due_governed(root, candidate="deadbeef")           # attributed, but the park is moved
    _live_limit(root)
    out = resumeloop.resume_projects(root, now=NOW)
    assert _node_record(out, "p-x")["action"] == "refused"
    project = _project(root, "p-z", "z", retry=PAST, lifecycle=Lifecycle.BLOCKED, timer=True)
    out = resumeloop.resume_projects(root, now=NOW)
    assert _node_record(out, "p-z")["action"] == "resumed"
    assert px.read_state().nodes[0].lifecycle is Lifecycle.BLOCKED
    assert project.read_state().nodes[0].lifecycle is Lifecycle.READY


def test_the_project_path_fails_open_and_says_so(root, monkeypatch):
    """A reading that fails lets the node through, as before, and the record says the
    consultation did not happen. Kills G10 (the project path's failure propagates or is
    silent)."""
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(health, "_collect", broken)
    px = _due_governed(root)
    _live_limit(root)
    rec = _node_record(resumeloop.resume_projects(root, now=NOW), "p-x")
    assert rec["action"] == "resumed"
    assert rec["provider_health"].startswith("not consulted: provider health could not be read")
    assert px.read_state().nodes[0].lifecycle is Lifecycle.READY


def _hostile_pair(root: Path) -> ProjectStore:
    """The control of every project-file test: governed `x` (due, master ended) and governed `y`
    (limit in three hours, master live and current) -- `consult("x")` is a HOLD on `y`. Returns a
    further project, `p-other`, whose `project.json` the test then spoils."""
    _due_governed(root)
    _live_limit(root)
    assert ProviderHealthSentinel(root, now=NOW).consult("x").action is Action.HOLD
    return _project(root, "p-other", "other", lifecycle=Lifecycle.RUNNING)


def _no_evidence_within(root: Path, seconds: int = 5) -> Consultation:
    with _within(seconds):
        got = ProviderHealthSentinel(root, now=NOW).consult("x")
    assert got.action is Action.PROCEED and got.evidence == (), got
    return got


def test_a_fifo_project_file_makes_a_governed_neighbour_no_evidence_for_the_sentinel(
        root, tmp_path):
    """`master_standing` reads every project's `project.json` with `exists()` + `read_text`: a
    FIFO at another project's file would block a verification of a governed neighbour. It is
    looked at first; the neighbour is no evidence and the sentinel answers within the bound.
    (The sentinel alone: at pass level the base's own `_legacy_run` -> `_check_park` ->
    `master_standing` already reads that file for a run with no armed timer, which is not this
    slice's.) Kills G11 (the project files not looked at)."""
    other = _hostile_pair(root)
    os.replace(other.state_path, tmp_path / "moved")
    os.mkfifo(other.state_path)
    _no_evidence_within(root)


def test_a_symlinked_project_entry_is_no_evidence_and_is_not_read_through(root, tmp_path):
    """`list_projects` follows a link (`iterdir` + `exists()`), so a link in `projects/` whose
    target holds a FIFO `project.json` hangs `master_standing` although `lstat` of the entry says
    "link". A link entry is unsafe, whatever it points at. Kills G13 (the link entry skipped as
    "a loose file")."""
    _hostile_pair(root)
    ext = tmp_path / "ext"
    ext.mkdir()
    os.mkfifo(ext / "project.json")
    os.symlink(ext, root / "projects" / "zz-link", target_is_directory=True)
    _no_evidence_within(root)


def test_a_symlinked_projects_directory_is_no_evidence(root, tmp_path):
    """`projects/` itself a link: not looked through (`lstat`, not `stat`). The same tree behind
    the link is a perfectly good one, so the only reason for the refusal is the link. Kills G14
    (`lstat` of `projects/` replaced by `stat`)."""
    _hostile_pair(root)
    moved = tmp_path / "moved-projects"
    os.replace(root / "projects", moved)
    os.symlink(moved, root / "projects", target_is_directory=True)
    _no_evidence_within(root)


def test_a_project_file_with_two_names_is_no_evidence(root, tmp_path):
    """A second name (a hard link) at `project.json` of another project. Kills G15 (the link
    count not judged)."""
    other = _hostile_pair(root)
    os.link(other.state_path, tmp_path / "second-name")
    _no_evidence_within(root)


def test_a_project_file_is_looked_through_up_to_eight_mebibytes_exactly(root):
    """The size bound of the look, on both sides: a `project.json` of exactly 8 MiB is looked
    through (the verifier then finds it unreadable as a project and goes on: still a HOLD), one
    byte more makes the neighbour no evidence. Kills G16 (the bound exclusive), G17 (doubled)."""
    other = _hostile_pair(root)
    with open(other.state_path, "r+b") as fh:
        fh.truncate(8 * 1024 * 1024)
    assert ProviderHealthSentinel(root, now=NOW).consult("x").action is Action.HOLD
    with open(other.state_path, "r+b") as fh:
        fh.truncate(8 * 1024 * 1024 + 1)
    _no_evidence_within(root)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through a directory without x")
def test_an_unreadable_project_entry_is_no_evidence_and_never_blocks(root):
    """`projects/` readable but not searchable (mode 0444): the entries are listed and their
    metadata is not. Fail closed -- the neighbour is no evidence, nothing is opened. (The
    verifier's own fail-closed `except` refuses it as well, so the look is one of two guards here:
    the mutant G18 that skips an unreadable entry is equivalent in isolation, and measured so.)"""
    _hostile_pair(root)
    projects = root / "projects"
    os.chmod(projects, 0o444)
    try:
        _no_evidence_within(root)
    finally:
        os.chmod(projects, 0o755)


def test_a_failing_verification_of_one_neighbour_does_not_hide_another(root, monkeypatch):
    """"Any exception in the verification of a neighbour is no evidence for *that* neighbour":
    `stale_master_reason` raises for the governed `y` while the plain `z` of the same account is
    pending -- the answer is HOLD on `z` alone, not "could not be read" for the whole question.
    Kills G19 (the try/except of `_verify` dropped)."""
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    _due_governed(root)
    _live_limit(root)
    _park(root, "z", retry=LATER)
    monkeypatch.setattr(health, "stale_master_reason", broken)
    got = ProviderHealthSentinel(root, now=NOW).consult("x")
    assert got.action is Action.HOLD and got.evidence == ("z",) and got.available, got


@pytest.mark.parametrize("name", ["lease.json", "epochs.json"])
def test_a_fifo_at_the_owners_lease_or_epoch_chain_does_not_block_and_is_no_evidence(
        root, name):
    """The owner project's lease and epoch chain are read behind `is_file()`, which a FIFO fails:
    nothing blocks and the neighbour is no evidence (its master cannot be confirmed). This pins
    that base guard (`succession.read_lease`, `epoch.EpochStore._records`); no mutant of this
    slice kills it."""
    _due_governed(root)
    _live_limit(root)
    owner = ProjectStore(root, "p-y").dir
    os.replace(owner / name, owner / (name + ".moved"))
    os.mkfifo(owner / name)
    _no_evidence_within(root)


def test_the_projects_bound_is_exact(root):
    """1024 projects are looked through, 1025 are not (the neighbour is then no evidence).
    Kills G12 (the bound dropped)."""
    _due_governed(root)
    _live_limit(root)
    projects = root / "projects"
    for n in range(1024 - 2):                  # p-x and p-y already exist
        (projects / f"q{n:04d}").mkdir()
    assert ProviderHealthSentinel(root, now=NOW).consult("x").action is Action.HOLD
    (projects / "q-over").mkdir()
    _no_evidence_within(root)


def test_an_unsaid_master_record_is_said_for_the_asked_run(root, monkeypatch):
    """Not consulting a run because its master record does not say whether it is governed is
    *said*: `available=False`, and the loop's record carries `provider_health`. Kills G20 (said as
    an ordinary PROCEED)."""
    _park(root, "c", retry=PAST, account="acct-c")
    _park(root, "d", retry=LATER, account="acct-c")
    (root / "c" / "recovery").mkdir(exist_ok=True)
    (root / "c" / "recovery" / "master.json").write_text(json.dumps({"kind": "SOMEONE"}))
    got = ProviderHealthSentinel(root, now=NOW).consult("c")
    assert got.action is Action.PROCEED and not got.available and "does not say" in got.reason


def test_hoh_resume_quota_holds_a_governed_node_behind_a_live_masters_limit(root, capsys):
    """The production path of the orchestrated product: `hoh.cli.main(["resume-quota"])` ->
    `cmd_resume_quota` -> `resume_runs` and `resume_projects` -> the sentinel (stamps written
    relative to the clock the command reads). `x`, governed and due, is held both as a run and as a
    project node behind `y`'s limit under a live, current master; nothing of its project is
    written. Kills G3, G4 through the product's own entry point."""
    clock = datetime.now(timezone.utc)
    px = _due_governed(root, retry=(clock - timedelta(hours=1)).isoformat())
    _live_limit(root, retry=(clock + timedelta(hours=2)).isoformat())
    before = px.state_path.read_bytes()
    assert cli.main(["--root", str(root), "resume-quota"]) == 0
    printed = json.loads(capsys.readouterr().out)
    ran = [r for r in printed["results"] if r.get("target") == "x"]
    node = [r for r in printed["projects"] if r.get("target") == "p-x"]
    assert [r["action"] for r in ran] == ["held_for_provider"]
    assert [r["action"] for r in node] == ["held_for_provider"] and node[0]["evidence"] == ["y"]
    assert px.state_path.read_bytes() == before
    assert len(EpochStore(px.dir).chain()) == 1
