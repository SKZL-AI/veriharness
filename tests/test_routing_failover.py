"""P3-41, slice 1: a declared route that the cross-run provider health calls unhealthy fails over
to the next route the policy declares, once, durably.

`routing.resolve_failover` reads `health.read_health` over the runs root a run lives in, chooses per
role the first declared route whose provider has no pending limit or availability wait, writes the
choice to `<run_dir>/routing-failover.json` and applies it. Every later resolution of the run replays
that record and reads no health. These tests build real runs under `tmp_path` through the product's
own `RunStore`, `RunState` and `dispatchrecovery.append_record` (the builders are copied from
`tests/test_provider_health.py`: test modules do not import each other). Nothing starts a process,
Herdr or an agent, no test sleeps, nothing is written outside `tmp_path`, and `now` is injected.

Mutant ids (`M1`..) are the spec's table of single changes to `src/hoh/routing.py` that the integration
review applies; each test says which it kills. Every limit and name below is written out as a literal on purpose: a table imported
from the module would agree with a mutated module.
"""
from __future__ import annotations

import ast
import errno
import hashlib
import json
import os
import signal
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hoh import dispatchrecovery, durable_io, health, routing
from hoh.contracts import Condition, Role, RunState, Stage
from hoh.dispatchrecovery import (
    NO_MASTER, DispatchProvenance, ParkBinding, RoutingDecision, park_id,
)
from hoh.failures import NormalizedFailure
from hoh.routing import RoutingRefused
from hoh.store import RunStore

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
SOON = (NOW + timedelta(hours=1)).isoformat()
PAST = (NOW - timedelta(hours=1)).isoformat()
N = NormalizedFailure
PROFILES = {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "claude"}
FILE = "routing-failover.json"


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


def _run(root: Path, name: str = "run-1") -> Path:
    """The run that resolves its routing: a directory with no state yet, as a run that has just been made."""
    d = root / name
    d.mkdir()
    return d


def _park(root: Path, run_id: str, *, provider: str = "claude", account: str = "acct-a",
          failure: NormalizedFailure = N.USAGE_LIMIT, retry: str = SOON,
          receipt: bool = True, attempts: int = 0) -> RunStore:
    """A sibling run parked on `failure` with the receipt the controller writes."""
    st = RunState(run_id=run_id, repo_path="/arena/repo", project_name="demo",
                  spec_path="/arena/spec.md", spec_digest="s", policy_digest="p",
                  profile_digest="h", stage=Stage.DEVELOPING, condition=Condition.BLOCKED,
                  iteration=1)
    st.blocked_kind = failure.value.lower()
    st.retry_after = retry
    st.blocked_reason = "parked"
    st.resume_attempts = attempts       # 99: the resume loop gave this park up
    store = RunStore(root, run_id)
    _put(store, st)
    if receipt:
        binding = ParkBinding(programme="NONE", program_epoch=0, run_id=run_id, iteration=1,
                              attempt=st.attempt, stage=st.stage.value, candidate="NONE",
                              master=NO_MASTER)
        dispatchrecovery.append_record(store.dir, RoutingDecision(
            park_id=park_id(binding, failure, retry), reason=failure.value, binding=binding,
            provenance=DispatchProvenance(
                provider=provider, model="m-1", account_handle=account, role="developer",
                capability_requirements=["read_repo", "write_worktree", "run_shell"]),
            retry_at=retry))
    return store


def _put(store: RunStore, st: RunState) -> None:
    """Write `st` as the run's state, advancing the sequence when the run already has one."""
    if store.state_path.exists():
        st.write_seq = RunStore._parse_state(store.state_path.read_text()).write_seq
    store.write_state(st)


def _unpark(store: RunStore) -> None:
    """The park is over: the same run, active again."""
    st = RunState(run_id=store.run_id, repo_path="/arena/repo", project_name="demo",
                  spec_path="/arena/spec.md", spec_digest="s", policy_digest="p",
                  profile_digest="h", stage=Stage.DEVELOPING, condition=Condition.ACTIVE,
                  iteration=1)
    _put(store, st)


def _doc(fallbacks=None, **qa) -> dict:
    entry = {"provider": "claude", "model": "opus",
             "fallbacks": ([{"provider": "codex", "model": "gpt-5"},
                            {"provider": "kimi", "model": "k2", "effort": "low"}]
                           if fallbacks is None else fallbacks)}
    entry.update(qa)
    return {"schema": routing.SCHEMA, "default": {"model": "sonnet", "effort": "high"},
            "roles": {"qa": entry}}


def _policy(**kw):
    return routing.parse(_doc(**kw))


def _go(policy, run: Path, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("profiles", dict(PROFILES))
    return routing.resolve_failover(policy, run_dir=run, **kw)


def _patch_health(monkeypatch, fn) -> None:
    """Replace the cross-run reader for the module under test, however it binds the name: the
    attribute of `hoh.health` and, if the module imported the name itself, its own."""
    monkeypatch.setattr(health, "read_health", fn)
    if hasattr(routing, "read_health"):
        monkeypatch.setattr(routing, "read_health", fn)


def _patch_create(monkeypatch, fn) -> None:
    """The same for the one durable create (`from .durable_io import durable_create` or
    `durable_io.durable_create(...)`)."""
    monkeypatch.setattr(durable_io, "durable_create", fn)
    if hasattr(routing, "durable_create"):
        monkeypatch.setattr(routing, "durable_create", fn)


def _no_health(monkeypatch) -> None:
    def boom(*a, **k):
        raise AssertionError("the provider health was read")

    _patch_health(monkeypatch, boom)


def _sign(doc: dict) -> dict:
    """The record's own digest rule, written out: sha256 of the compact, sorted, ASCII body."""
    body = {k: v for k, v in doc.items() if k != "digest"}
    doc["digest"] = hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
    return doc


def _tree(path: Path) -> dict:
    out = {}
    for p in sorted(path.rglob("*")):
        st = p.lstat()
        try:
            data = p.read_bytes() if p.is_file() and not p.is_symlink() else b""
        except PermissionError:
            data = b"<unreadable>"
        out[str(p.relative_to(path))] = (data, st.st_mode)
    return out


# --------------------------------------------------------------------------- #
# R1 -- what a policy may declare
# --------------------------------------------------------------------------- #

def test_a_policy_declares_fallbacks_as_whole_ordered_routes_and_nothing_else():
    """Kills M1 (a fallback may omit its model), M2 (a role entry carries fallbacks without a
    provider), M3 (a fallback repeats the declared route or another fallback), M4 (the ceiling of
    three is four)."""
    policy = _policy()
    assert [(r.provider, r.model, r.effort) for r in policy.fallbacks[Role.QA]] == [
        ("codex", "gpt-5", None), ("kimi", "k2", "low")]
    assert set(policy.fallbacks) == {Role.QA}
    with pytest.raises(TypeError):
        policy.fallbacks[Role.PLANNER] = (routing.Route(provider="codex", model="x"),)
    assert policy.evidence()["roles"]["qa"]["fallbacks"] == [
        {"provider": "codex", "model": "gpt-5"},
        {"provider": "kimi", "model": "k2", "effort": "low"}]
    assert routing.MAX_FALLBACKS == 3
    # no fallback declared: the shape of every earlier policy is unchanged
    plain = routing.parse({"schema": routing.SCHEMA, "roles": {"qa": {"model": "opus"}}})
    assert plain.fallbacks == {} and "fallbacks" not in plain.evidence()["roles"]["qa"]
    # the digest binds the order and the content of the fallbacks
    reordered = _policy(fallbacks=[{"provider": "kimi", "model": "k2", "effort": "low"},
                                   {"provider": "codex", "model": "gpt-5"}])
    assert len({policy.digest, reordered.digest}) == 2

    def refused(doc) -> bool:
        try:
            routing.parse(doc)
        except RoutingRefused:
            return True
        return False

    one = {"provider": "codex", "model": "gpt-5"}
    three = [one, {"provider": "kimi", "model": "k2"}, {"provider": "pi", "model": "p1"}]
    assert not refused(_doc(fallbacks=three))                              # exactly the ceiling
    for label, doc in {
        "four routes": _doc(fallbacks=[*three, {"provider": "codex", "model": "gpt-6"}]),
        "empty list": _doc(fallbacks=[]),
        "not a list": _doc(fallbacks={"provider": "codex", "model": "gpt-5"}),
        "a string": _doc(fallbacks="codex"),
        "item not an object": _doc(fallbacks=["codex"]),
        "no model": _doc(fallbacks=[{"provider": "codex"}]),
        "no provider": _doc(fallbacks=[{"model": "gpt-5"}]),
        "effort only": _doc(fallbacks=[{"effort": "low"}]),
        "unknown provider": _doc(fallbacks=[{"provider": "gpt", "model": "x"}]),
        "provider in another case": _doc(fallbacks=[{"provider": "Codex", "model": "x"}]),
        "provider with a trailing blank": _doc(fallbacks=[{"provider": "codex ", "model": "x"}]),
        "model with a trailing newline": _doc(fallbacks=[{"provider": "codex",
                                                          "model": "gpt-5\n"}]),
        "unknown key": _doc(fallbacks=[{**one, "account": "acct-b"}]),
        "nested fallbacks": _doc(fallbacks=[{**one, "fallbacks": [one]}]),
        "repeats the declared route": _doc(fallbacks=[{"provider": "claude", "model": "opus"}]),
        "repeats it at another effort": _doc(fallbacks=[{"provider": "claude", "model": "opus",
                                                         "effort": "low"}]),
        "repeats another fallback": _doc(fallbacks=[one, {**one, "effort": "max"}]),
        "no provider on the role entry": {"schema": routing.SCHEMA, "roles": {"qa": {
            "model": "opus", "fallbacks": [one]}}},
        "fallbacks alone": {"schema": routing.SCHEMA, "roles": {"qa": {"fallbacks": [one]}}},
        "on the default": {"schema": routing.SCHEMA, "default": {"model": "x", "fallbacks": [one]},
                           "roles": {"qa": {"model": "opus"}}},
        "credential-shaped model": _doc(fallbacks=[{"provider": "codex",
                                                    "model": "sk-ant-api03-AbCdEf0123456789xyz"}]),
    }.items():
        assert refused(doc), label
    # the declared route's model may come from the default: repeating it is still a repeat
    inherited = {"schema": routing.SCHEMA, "default": {"model": "sonnet"},
                 "roles": {"qa": {"provider": "claude", "fallbacks": [
                     {"provider": "claude", "model": "sonnet"}]}}}
    assert refused(inherited)
    inherited["roles"]["qa"]["fallbacks"][0]["model"] = "haiku"
    assert not refused(inherited)


# --------------------------------------------------------------------------- #
# R2 -- nothing declared, nothing read, nothing written
# --------------------------------------------------------------------------- #

def test_a_policy_without_fallbacks_reads_no_health_and_writes_nothing(root, monkeypatch):
    """Kills M22 (health is read, or a record written, for a policy that declares no fallback)."""
    run = _run(root)
    _park(root, "other")
    _no_health(monkeypatch)
    policy = routing.parse({"schema": routing.SCHEMA, "default": {"model": "sonnet"},
                            "roles": {"qa": {"provider": "claude", "model": "opus"}}})
    before = _tree(root)
    got = _go(policy, run)
    plain = routing.resolve(policy, profiles=dict(PROFILES))
    assert (got.models, got.efforts, got.evidence) == (plain.models, plain.efforts, plain.evidence)
    assert "failover" not in got.evidence and got.profiles == PROFILES
    assert _tree(root) == before and not (run / FILE).exists()
    # `resolve` itself does not read `fallbacks`: a policy that declares them, resolved plainly,
    # is its declared route and has no failover key (only `resolve_failover` acts on them)
    declared = routing.resolve(_policy(), profiles=dict(PROFILES))
    assert declared.models[Role.QA] == "opus" and declared.profiles[Role.QA] == "claude"
    assert "failover" not in declared.evidence


# --------------------------------------------------------------------------- #
# R3.. -- the failover itself, and what it records
# --------------------------------------------------------------------------- #

def test_an_unhealthy_declared_route_fails_over_to_the_next_declared_route_and_records_it(root):
    """Kills M20 (the fallback reaches the models but not the provider table), M21 (a decision is
    recorded only when the role moved)."""
    run = _run(root)
    _park(root, "other", provider="claude")
    got = _go(_policy(), run)
    assert got.models == {Role.PLANNER: "sonnet", Role.DEVELOPER: "sonnet", Role.QA: "gpt-5"}
    assert got.efforts == {Role.PLANNER: "high", Role.DEVELOPER: "high", Role.QA: "high"}
    assert got.profiles == {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "codex"}
    seen = got.evidence["resolved"]["qa"]
    assert seen["provider"] == {"value": "codex", "source": "policy:failover",
                                "declared_provider": "claude"}
    assert seen["model"] == {"value": "gpt-5", "source": "policy:failover"}
    assert seen["effort"] == {"value": "high", "source": "policy:default"}
    # a role the policy gives no fallbacks is left as `resolve` made it
    assert got.evidence["resolved"]["developer"]["provider"] == {"value": "claude",
                                                                 "source": "run-profile"}
    [decision] = got.evidence["failover"]["decisions"].values()
    # P3-41 slice 4: a FALLBACK carries its route receipt (tests/test_route_receipts.py pins it)
    assert decision["receipt"]["cause"] == "start" and decision["receipt"]["trust_class"] == "weaker"
    assert {k: v for k, v in decision.items() if k != "receipt"} == {
        "outcome": "FALLBACK", "index": 0, "route": {"provider": "codex", "model": "gpt-5"},
        "reason": "LIMITED", "trust_assessed": False,
        "considered": [{"provider": "claude", "model": "opus", "state": "LIMITED",
                        "not_before": SOON},
                       {"provider": "codex", "model": "gpt-5", "state": "NO_EVIDENCE",
                        "not_before": None}]}
    assert set(got.evidence["failover"]["decisions"]) == {"qa"}
    assert got.evidence["failover"]["decided_at"] == NOW.isoformat()
    assert got.evidence["failover"]["policy_digest"] == _policy().digest
    # the record is on disk, whole, private, bound to the policy and to its own digest
    path = run / FILE
    raw = path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_nlink == 1
    assert raw.isascii() and raw.endswith(b"}\n") and not raw.endswith(b"\n\n")
    doc = json.loads(raw)
    assert set(doc) == {"schema", "policy_digest", "decided_at", "decisions", "digest"}
    assert doc["schema"] == "hoh-routing-failover/2" and doc["policy_digest"] == _policy().digest
    assert doc["decisions"]["qa"] == decision and doc == _sign(dict(doc))
    # the fallback's own effort, when it declares one, is the role's effort on that route
    other = _run(root, "run-2")
    _park(root, "third", provider="codex")
    again = _go(_policy(), other)
    assert again.evidence["failover"]["decisions"]["qa"]["route"] == {
        "provider": "kimi", "model": "k2", "effort": "low"}
    assert again.efforts[Role.QA] == "low"
    assert again.evidence["resolved"]["qa"]["effort"] == {"value": "low",
                                                          "source": "policy:failover"}


@pytest.mark.parametrize("states,chosen,index", [
    # (provider, failure, retry) of the parked siblings -> the route that is taken
    ([], "claude", None),                                                  # no evidence at all
    ([("claude", N.USAGE_LIMIT, SOON)], "codex", 0),                       # LIMITED
    ([("claude", N.PROVIDER_UNAVAILABLE, SOON)], "codex", 0),              # DEGRADED
    ([("claude", N.USAGE_LIMIT, PAST)], "claude", None),                   # RESET_DUE: the original route after its time
    ([("claude", N.USAGE_LIMIT, SOON), ("codex", N.RATE_LIMIT, SOON)], "kimi", 1),
    ([("codex", N.USAGE_LIMIT, SOON)], "claude", None),                    # a fallback's trouble moves nothing
    ([("pi", N.USAGE_LIMIT, SOON)], "claude", None),                       # an undeclared provider's neither
])
def test_which_route_is_taken_follows_the_pending_waits_of_each_declared_provider(
        root, states, chosen, index):
    """Kills M5 (`RESET_DUE` counts as unhealthy), M33 (the chain is not tried in declaration order)."""
    run = _run(root)
    for i, (provider, failure, retry) in enumerate(states):
        _park(root, f"s{i}", provider=provider, failure=failure, retry=retry)
    got = _go(_policy(), run)
    route = got.evidence["failover"]["decisions"]["qa"]
    assert route["route"]["provider"] == chosen and route["index"] == index
    assert route["outcome"] == ("DECLARED" if index is None else "FALLBACK")
    assert got.profiles[Role.QA] == chosen


@pytest.mark.parametrize("failure,retry,attempts,state", [
    (N.USAGE_LIMIT, PAST, 0, "RESET_DUE"),
    (N.USAGE_LIMIT, SOON, 99, "GIVEN_UP"),
])
def test_a_fallback_that_is_reset_due_or_given_up_is_taken_and_the_record_says_which(
        root, failure, retry, attempts, state):
    """Pins the one rule of C7 on the fallback side: only a pending limit or availability wait
    makes a route unhealthy, so a fallback whose provider is `RESET_DUE` or `GIVEN_UP` is taken,
    and the record names the state it was taken in. Kills M43 (`GIVEN_UP` is unhealthy), M5."""
    policy = _policy()
    run = _run(root)
    _park(root, "a", provider="claude")
    _park(root, "b", provider="codex", failure=failure, retry=retry, attempts=attempts)
    got = _go(policy, run)
    decision = got.evidence["failover"]["decisions"]["qa"]
    assert (decision["outcome"], decision["index"]) == ("FALLBACK", 0)
    assert [c["state"] for c in decision["considered"]] == ["LIMITED", state]
    assert got.profiles[Role.QA] == "codex"


@pytest.mark.parametrize("accounts,reason", [
    # the worst state of the provider's accounts is the provider's: LIMITED over DEGRADED over
    # RESET_DUE over GIVEN_UP, whatever the order the accounts are read in
    (("GIVEN_UP", "LIMITED"), "LIMITED"),
    (("LIMITED", "GIVEN_UP"), "LIMITED"),
    (("RESET_DUE", "DEGRADED"), "DEGRADED"),
    (("DEGRADED", "RESET_DUE"), "DEGRADED"),
    (("GIVEN_UP", "DEGRADED"), "DEGRADED"),
    (("DEGRADED", "LIMITED"), "LIMITED"),
    (("LIMITED", "DEGRADED"), "LIMITED"),
    (("GIVEN_UP", "RESET_DUE"), "RESET_DUE"),
    (("RESET_DUE", "GIVEN_UP"), "RESET_DUE"),
])
def test_the_worst_state_of_a_providers_accounts_decides_the_route(root, accounts, reason):
    """Kills M44 (the precedence of the states is another: a limited provider is read healthy
    because a given-up or elapsed account of it comes first), M6."""
    how = {"LIMITED": (N.USAGE_LIMIT, SOON, 0), "DEGRADED": (N.PROVIDER_UNAVAILABLE, SOON, 0),
           "RESET_DUE": (N.USAGE_LIMIT, PAST, 0), "GIVEN_UP": (N.USAGE_LIMIT, SOON, 99)}
    policy = _policy()
    run = _run(root)
    for i, state in enumerate(accounts):
        failure, retry, attempts = how[state]
        _park(root, f"s{i}", account=f"acct-{i}", failure=failure, retry=retry, attempts=attempts)
    got = _go(policy, run)
    decision = got.evidence["failover"]["decisions"]["qa"]
    assert decision["reason"] == reason == decision["considered"][0]["state"]
    assert got.profiles[Role.QA] == ("codex" if reason in {"LIMITED", "DEGRADED"} else "claude")


def test_every_role_that_declares_fallbacks_is_decided_and_recorded_together(root):
    """Kills M45 (only the first role with fallbacks is decided). The second half pins that a
    refusal for one role records no role."""
    doc = {"schema": routing.SCHEMA, "default": {"model": "sonnet", "effort": "high"},
           "roles": {"planner": {"provider": "claude", "model": "opus",
                                 "fallbacks": [{"provider": "kimi", "model": "k2"}]},
                     "qa": {"provider": "claude", "model": "opus",
                            "fallbacks": [{"provider": "codex", "model": "gpt-5"}]}}}
    policy = routing.parse(doc)
    run = _run(root)
    _park(root, "a", provider="claude")
    got = _go(policy, run)
    assert got.profiles == {Role.PLANNER: "kimi", Role.DEVELOPER: "claude", Role.QA: "codex"}
    assert got.models[Role.PLANNER] == "k2" and got.models[Role.QA] == "gpt-5"
    assert set(got.evidence["failover"]["decisions"]) == {"planner", "qa"}
    assert set(json.loads((run / FILE).read_bytes())["decisions"]) == {"planner", "qa"}
    # all or nothing: when one role has no healthy route, no role is recorded
    run2 = _run(root, "run-2")
    _park(root, "b", provider="kimi", failure=N.RATE_LIMIT)
    with pytest.raises(RoutingRefused):
        _go(policy, run2)
    assert not (run2 / FILE).exists()


def test_the_record_lands_in_the_directory_that_was_checked_and_not_in_one_swapped_in_after(
        root, tmp_path, monkeypatch):
    """Kills M47 (the record is created by path: a run directory swapped for a link between the
    check and the create receives the record in the link's target)."""
    policy = _policy()
    run = _run(root)
    _park(root, "other")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    moved = root / "moved"
    real = health.read_health

    def swapping(path, **kw):
        report = real(path, **kw)
        os.rename(run, moved)                                  # after the directory was checked,
        run.symlink_to(elsewhere, target_is_directory=True)    # before the record is created
        return report

    _patch_health(monkeypatch, swapping)
    got = _go(policy, run)
    assert got.profiles[Role.QA] == "codex"
    assert (moved / FILE).is_file()
    assert list(elsewhere.iterdir()) == []


def test_a_pending_wait_of_any_account_of_the_provider_counts_and_a_given_up_park_does_not(root):
    """Kills M6 (health matched on the first account of the provider only), M5."""
    run = _run(root)
    _park(root, "a", account="acct-a", failure=N.USAGE_LIMIT, retry=PAST)       # RESET_DUE
    _park(root, "b", account="acct-b", failure=N.RATE_LIMIT, retry=SOON)        # LIMITED
    assert _go(_policy(), run).profiles[Role.QA] == "codex"
    run2 = _run(root, "run-2")
    _park(root, "c", provider="claude", account="acct-c", failure=N.USAGE_LIMIT)
    # the limit of "b" is over; "c" is parked at the attempt limit, which the resume loop gave up on
    _unpark(RunStore(root, "b"))
    state = RunStore(root, "c").state_path
    data = json.loads(state.read_text())
    data["resume_attempts"] = 99
    state.write_text(json.dumps(data))
    got = _go(_policy(), run2)
    assert got.profiles[Role.QA] == "claude"


def test_a_fallback_at_the_declared_provider_is_unhealthy_with_it_and_no_healthy_route_refuses(root):
    """Kills M7 (every declared route unhealthy returns the declared route instead of refusing)."""
    policy = _policy()      # built outside `pytest.raises`: a refusal of the policy is not the one under test
    run = _run(root)
    _park(root, "a", provider="claude")
    _park(root, "b", provider="codex", failure=N.NETWORK_TRANSIENT)
    _park(root, "c", provider="kimi", failure=N.RATE_LIMIT)
    # "pi" is healthy and the operator approved other things for it elsewhere: not a route here
    with pytest.raises(RoutingRefused) as err:
        _go(policy, run)
    assert "no route" in str(err.value)
    assert not (run / FILE).exists()
    # another model at the same provider is the same provider's health
    same = _policy(fallbacks=[{"provider": "claude", "model": "haiku"}])
    run2 = _run(root, "run-2")
    with pytest.raises(RoutingRefused):
        _go(same, run2)
    assert not (run2 / FILE).exists()


def test_an_explicit_model_for_a_role_that_fails_over_is_refused_and_an_explicit_effort_wins(root):
    """Kills M19 (the caller's model, bound to the declared provider, is sent to the fallback's
    provider), M27 (an explicit effort is overwritten by the fallback's)."""
    policy = _policy()      # built outside `pytest.raises`: a refusal of the policy is not the one under test
    run = _run(root)
    _park(root, "other")
    with pytest.raises(RoutingRefused):
        _go(policy, run, models={Role.QA: "haiku"})
    assert not (run / FILE).exists()
    got = _go(policy, run, efforts={Role.QA: "max"}, models={Role.PLANNER: "haiku"})
    assert got.efforts[Role.QA] == "max" and got.models[Role.PLANNER] == "haiku"
    assert got.evidence["resolved"]["qa"]["effort"]["source"] == "explicit"
    assert got.models[Role.QA] == "gpt-5" and got.profiles[Role.QA] == "codex"


# --------------------------------------------------------------------------- #
# R8-R9 -- the decision is durable: a resume neither flips back nor reads health again
# --------------------------------------------------------------------------- #

def test_a_resume_replays_the_recorded_decision_and_reads_no_health(root, monkeypatch):
    """Kills M10 (the record is written and never read: a resume decides again, from health), M21 (a
    role that stayed is not recorded, so a limit that comes later moves it)."""
    run = _run(root)
    policy = _policy()
    sibling = _park(root, "other")
    first = _go(policy, run)
    assert first.profiles[Role.QA] == "codex"
    raw = (run / FILE).read_bytes()
    _unpark(sibling)                                   # claude is fine again
    _no_health(monkeypatch)                            # and nothing may be asked
    later = _go(policy, run, now=NOW + timedelta(days=3))
    assert (later.models, later.efforts, later.profiles) == (first.models, first.efforts,
                                                              first.profiles)
    assert later.evidence == first.evidence
    assert (run / FILE).read_bytes() == raw
    # a resolution always takes the DECLARED profiles: the previous result's provider table names
    # a provider the policy does not declare for the role, and `resolve` refuses it
    with pytest.raises(RoutingRefused):
        _go(policy, run, profiles=first.profiles)
    assert (run / FILE).read_bytes() == raw
    # the other direction: a role that stayed is not moved by a limit that comes after
    monkeypatch.undo()
    run2 = _run(root, "run-2")
    stay = _go(_policy(), run2)
    assert stay.profiles[Role.QA] == "claude"
    _park(root, "other", provider="claude")             # now claude is LIMITED
    _no_health(monkeypatch)
    assert _go(_policy(), run2).profiles[Role.QA] == "claude"


def test_two_resolutions_of_one_run_agree_even_when_the_second_loses_the_create_race(
        root, tmp_path, monkeypatch):
    """Kills M16 (the record is replaced, not created), M17 (a lost create-only race returns the
    loser's own decision)."""
    # the world a competing resolver saw: nothing parked, so it recorded "stay"
    rival = _run(root, "rival")
    _go(_policy(), rival)
    winner = (rival / FILE).read_bytes()
    assert json.loads(winner)["decisions"]["qa"]["outcome"] == "DECLARED"
    run = _run(root, "run-1")
    _park(root, "other")                                  # our resolver sees claude LIMITED
    real = durable_io.durable_create
    elsewhere = tmp_path / "cwd"                          # a create that ignored its directory
    elsewhere.mkdir()                                     # descriptor would land here, not in `run`
    monkeypatch.chdir(elsewhere)

    def racing(path, payload, **kw):
        # the other resolver's record lands first, in the same directory (the descriptor given)
        real(path, winner, mode=0o600, **{k: v for k, v in kw.items() if k == "dir_fd"})
        return real(path, payload, **kw)                  # ours then finds the name taken

    _patch_create(monkeypatch, racing)
    got = _go(_policy(), run)
    assert list(elsewhere.iterdir()) == []
    assert got.profiles[Role.QA] == "claude"
    assert (run / FILE).read_bytes() == winner
    assert got.evidence["failover"]["decisions"]["qa"]["outcome"] == "DECLARED"


# --------------------------------------------------------------------------- #
# R10-R12 -- fail closed
# --------------------------------------------------------------------------- #

def test_health_that_cannot_be_read_completely_is_refused_and_nothing_is_recorded(root, monkeypatch):
    """Kills M8 (an incomplete report is accepted: an unreadable run is skipped and the role stays),
    M51 (accepted when some run was readable), M9 (an exception of the reader is read as 'no
    evidence'), M38 (`now` is not forwarded), M52 (the clock is checked only on the path that reads
    health)."""
    policy = _policy()      # built outside `pytest.raises`: a refusal of the policy is not the one under test
    run = _run(root)
    _park(root, "good")                  # a readable, limited claude run beside the damaged one:
    broken = root / "broken"             # incomplete is incomplete, whatever else could be read
    broken.mkdir()
    (broken / "state.json").write_text("{")
    with pytest.raises(RoutingRefused) as err:
        _go(policy, run)
    assert "incomplete" in str(err.value)
    assert not (run / FILE).exists()
    # the same root with that run made readable-or-absent decides normally (the control)
    (broken / "state.json").write_text(json.dumps(
        json.loads(RunState(run_id="broken", repo_path="/r", project_name="d", spec_path="/s",
                            spec_digest="s", policy_digest="p", profile_digest="h",
                            stage=Stage.DEVELOPING, condition=Condition.ACTIVE,
                            iteration=1).model_dump_json())))
    assert _go(policy, run).profiles[Role.QA] == "codex"
    # the clock is checked before a record is: a resolution that already has one still refuses a
    # naive `now`
    assert (run / FILE).is_file()
    with pytest.raises(RoutingRefused):
        _go(policy, run, now=datetime(2026, 10, 12, 12, 0, 0))
    # a reader that raises is a refusal, whatever it raises
    for exc in (RuntimeError("x"), OSError(errno.EIO, "io"), ValueError("v"), KeyError("k")):
        run_n = _run(root, f"r-{type(exc).__name__}")

        def boom(*a, _exc=exc, **k):
            raise _exc

        _patch_health(monkeypatch, boom)
        with pytest.raises(RoutingRefused):
            _go(policy, run_n)
        assert not (run_n / FILE).exists()
    monkeypatch.undo()
    run_m = _run(root, "run-m")
    for bad in (datetime(2026, 10, 12, 12, 0, 0), "2026-10-12T12:00:00+00:00", 0):
        with pytest.raises(RoutingRefused):
            _go(policy, run_m, now=bad)
    assert not (run_m / FILE).exists()


def test_a_run_directory_that_is_not_a_real_directory_is_refused(root, tmp_path, monkeypatch):
    """Kills M25 (the directory is opened without `O_NOFOLLOW`: a link to a run directory is
    replayed), M39 (a relative spelling has the wrong parent), M40 (the filesystem root is no run)."""
    policy = _policy()      # built outside `pytest.raises`: a refusal of the policy is not the one under test
    real = _run(root)
    link = root / "link"
    link.symlink_to(real, target_is_directory=True)
    plain = root / "file"
    plain.write_text("x")
    for bad in (link, plain, root / "missing", None, 7, b"x", "", "run\x00x", "\udc80"):
        with pytest.raises(RoutingRefused):
            _go(policy, bad)
    assert not (real / FILE).exists()
    # the filesystem root is no run, and its "parent" is itself. Pinned independently of the
    # host: health is made to answer "complete, nothing parked" and a create anywhere fails the
    # test, so only the refusal itself can satisfy it.
    nothing = health.HealthReport(at=NOW.isoformat(), providers=(), unattributed=(), problems=(),
                                  runs_seen=0)
    _patch_health(monkeypatch, lambda *a, **k: nothing)

    def forbidden_create(*a, **k):
        pytest.fail("a failover record was created for the filesystem root")

    _patch_create(monkeypatch, forbidden_create)
    with pytest.raises(RoutingRefused):
        _go(policy, "/")
    monkeypatch.undo()
    # a relative spelling has the parent it really has: the runs root is the directory above
    # the working directory, and the record lands in the working directory
    rel = tmp_path / "rel-runs"
    rel.mkdir()
    here = _run(rel, "here")
    _park(rel, "other")
    monkeypatch.chdir(here)
    for spelling in (".", "./", "../here", "sub/.."):
        (here / "sub").mkdir(exist_ok=True)
        got = _go(policy, spelling)
        assert got.profiles[Role.QA] == "codex" and (here / FILE).is_file()
        (here / FILE).rename(here / "sub" / f"moved-{len(list((here / 'sub').iterdir()))}")
    monkeypatch.undo()
    # a link to a run directory that already holds a good record would replay it: the directory
    # is judged before the record is read, and it is the link that is refused
    clean = tmp_path / "clean-runs"
    clean.mkdir()
    done = _run(clean, "done")
    _park(clean, "other")
    _go(policy, done)
    holder = tmp_path / "holder"
    holder.mkdir()
    (holder / "run").symlink_to(done, target_is_directory=True)
    with pytest.raises(RoutingRefused):
        _go(policy, holder / "run")


def test_a_failover_record_that_cannot_be_made_durable_chooses_nothing(root, monkeypatch):
    """Kills M18 (a failed write is swallowed and the result returned as decided), M42 (a failure
    after the link is told as 'nothing was chosen', or the record is not replayed)."""
    policy = _policy()      # built outside `pytest.raises`: a refusal of the policy is not the one under test
    run = _run(root)
    _park(root, "other")

    class _Faulty:
        def __init__(self, **over):
            self._over = over

        def __getattr__(self, name):
            return self._over.get(name, getattr(os, name))

    def eio(fd):
        raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(durable_io, "os", _Faulty(fsync=eio))
    with pytest.raises(RoutingRefused):
        _go(policy, run)
    names = sorted(p.name for p in run.iterdir())
    assert FILE not in names
    monkeypatch.undo()
    # the control: without the fault the same resolution writes and returns
    assert _go(policy, run).profiles[Role.QA] == "codex" and (run / FILE).is_file()
    # a directory fsync that fails AFTER the record is linked in: the resolution still refuses,
    # but the record is on disk -- "refused" is not "nothing was chosen" -- and the next
    # resolution replays it, reading no health
    run2 = _run(root, "run-2")
    real_fsync = os.fsync

    def dir_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected")
        return real_fsync(fd)

    monkeypatch.setattr(durable_io, "os", _Faulty(fsync=dir_fsync))
    with pytest.raises(RoutingRefused) as err:
        _go(policy, run2)
    assert "durable" in str(err.value) and "replays" in str(err.value)
    monkeypatch.undo()
    assert (run2 / FILE).is_file()
    _no_health(monkeypatch)
    assert _go(policy, run2).profiles[Role.QA] == "codex"


def test_a_park_whose_provider_is_not_named_is_no_evidence_and_the_record_says_so(
        root, tmp_path):
    """Pins the stated limit of the reading (`health.py`: an unnamed provider is never grouped): a
    park with no receipt moves no role. Kills M37 (an unattributed wait is read as a wait of the
    declared provider)."""
    run = _run(root)
    policy = _policy()
    _park(root, "orphan", receipt=False)
    got = _go(policy, run)
    assert got.profiles[Role.QA] == "claude"
    [first, *_] = got.evidence["failover"]["decisions"]["qa"]["considered"]
    assert first == {"provider": "claude", "model": "opus", "state": "NO_EVIDENCE",
                     "not_before": None}
    # the same for a park whose receipts file is there but cannot be read (a link): the park is
    # listed unattributed, the report is still complete (`tests/test_provider_health.py`), and
    # the role stays -- this is the reading's rule, stated here, not a refusal
    clean = tmp_path / "damaged-runs"
    clean.mkdir()
    store = _park(clean, "x")
    ledger = store.dir / "recovery" / "routing.jsonl"
    ledger.rename(ledger.with_name("routing.jsonl.moved"))
    ledger.symlink_to(ledger.with_name("routing.jsonl.moved"))
    again = _go(policy, _run(clean, "run-1"))
    assert again.profiles[Role.QA] == "claude"
    assert again.evidence["failover"]["decisions"]["qa"]["considered"][0]["state"] == "NO_EVIDENCE"


# --------------------------------------------------------------------------- #
# R13-R15 -- a record that is odd, or not ours, is refused and never overwritten
# --------------------------------------------------------------------------- #

def _written(root: Path, name: str = "run-1") -> tuple[Path, bytes]:
    run = _run(root, name)
    _park(root, "other") if not (root / "other").exists() else None
    _go(_policy(), run)
    return run, (run / FILE).read_bytes()


class _Alarm(Exception):
    pass


def _alarmed(fn):
    def handler(signum, frame):
        raise _Alarm("the call did not return")

    old = signal.signal(signal.SIGALRM, handler)
    signal.alarm(10)
    try:
        return fn()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def test_a_record_that_is_not_one_plain_regular_file_is_refused_and_left_as_it_was(
        root, tmp_path, monkeypatch):
    """Kills M13 (a link is followed), M14 (a second name is read), M15 and M15b (the size ceiling is
    one byte too wide or too narrow). Every case also asserts that the run directory is byte-for-byte
    as it was and that health was not read."""
    policy = _policy()      # built outside `pytest.raises`: a refusal of the policy is not the one under test
    run, good = _written(root)
    outside = tmp_path / "elsewhere.json"
    outside.write_bytes(good)

    def fresh(make):
        """A new run directory whose record is `make(path)`; the resolution must refuse it."""
        r = _run(root, f"v{len(list(root.iterdir()))}")
        make(r / FILE)
        before = _tree(r)
        _no_health(monkeypatch)
        with pytest.raises(RoutingRefused):
            _alarmed(lambda: _go(policy, r))
        monkeypatch.undo()
        assert _tree(r) == before

    fresh(lambda p: p.symlink_to(outside))                       # a link to a good record
    fresh(lambda p: p.symlink_to(tmp_path / "dangling.json"))    # a dangling link
    fresh(lambda p: os.mkfifo(p))                                # a FIFO
    fresh(lambda p: p.mkdir())                                   # a directory
    fresh(lambda p: os.link(outside, p))                         # a second name
    fresh(lambda p: p.write_bytes(b""))                          # empty
    fresh(lambda p: p.write_bytes(good + b" " * (65536 + 1 - len(good))))        # one byte over
    fresh(lambda p: p.write_bytes(b" " * 65536 * 2))             # far over
    fresh(lambda p: p.write_bytes(good[:-5]))                    # truncated
    fresh(lambda p: p.write_bytes(good.replace(b"{", b"\xff", 1)))   # not UTF-8
    fresh(lambda p: p.write_bytes(good.decode().encode("utf-16")))   # UTF-16
    fresh(lambda p: p.write_bytes(b"\xef\xbb\xbf" + good))       # a BOM
    fresh(lambda p: p.write_bytes(good.replace(b'"digest"', b'"dig\\u0000est"')))  # NUL in a key
    fresh(lambda p: p.write_bytes(b"[" * 100000 + b"]" * 100000))  # nesting the parser cannot take
    fresh(lambda p: p.write_bytes(b"[]"))                        # not an object
    fresh(lambda p: p.write_bytes(b"null"))
    fresh(lambda p: p.write_bytes(good.replace(b'"schema"', b'"digest": "x", "schema"', 1)))  # repeated key
    if os.geteuid() != 0:
        def unreadable(p):
            p.write_bytes(good)
            p.chmod(0)

        fresh(unreadable)
    # exactly 65536 bytes of an otherwise good record (JSON whitespace is not in the digest) is
    # read and replayed: the ceiling is the boundary, not 65535
    pad = good[:-1] + b" " * (65536 - len(good)) + b"\n"
    assert len(pad) == 65536
    r = _run(root, "exact")
    (r / FILE).write_bytes(pad)
    _no_health(monkeypatch)
    assert _go(policy, r).profiles[Role.QA] == "codex"


@pytest.mark.parametrize("label,edit,resign", [
    ("another policy", lambda d: d.update(policy_digest="0" * 64), True),
    ("another schema", lambda d: d.update(schema="hoh-routing-failover/3"), True),
    ("an extra key", lambda d: d.update(extra=1), True),
    ("a missing key", lambda d: d.pop("decided_at"), True),
    ("a naive time", lambda d: d.update(decided_at="2026-10-12T12:00:00"), True),
    ("a time that is no time", lambda d: d.update(decided_at="yesterday"), True),
    ("a role the policy gives no fallbacks", lambda d: d["decisions"].update(
        planner=d["decisions"]["qa"]), True),
    ("no decision for the role", lambda d: d["decisions"].clear(), True),
    ("an index beyond the chain", lambda d: d["decisions"]["qa"].update(index=2), True),
    ("a negative index", lambda d: d["decisions"]["qa"].update(index=-1), True),
    ("a boolean index", lambda d: d["decisions"]["qa"].update(index=True), True),
    ("a fallback index for the declared route", lambda d: d["decisions"]["qa"].update(
        outcome="DECLARED"), True),
    ("an undeclared route", lambda d: d["decisions"]["qa"].update(
        route={"provider": "pi", "model": "p9"}), True),
    ("another model of a declared provider", lambda d: d["decisions"]["qa"].update(
        route={"provider": "codex", "model": "gpt-9"}), True),
    ("a route looked at that is not declared", lambda d: d["decisions"]["qa"]["considered"][1]
     .update(provider="pi"), True),
    ("a chosen route its own reading calls unhealthy", lambda d: d["decisions"]["qa"][
        "considered"][1].update(state="LIMITED"), True),
    ("a declared route that was healthy", lambda d: d["decisions"]["qa"]["considered"][0].update(
        state="NO_EVIDENCE"), True),
    ("a state that is no state", lambda d: d["decisions"]["qa"]["considered"][0].update(
        state="HEALTHY"), True),
    ("a reason that is not what was read", lambda d: d["decisions"]["qa"].update(
        reason="DEGRADED"), True),
    ("a trust assessment nobody made", lambda d: d["decisions"]["qa"].update(
        trust_assessed=True), True),
    ("a trust assessment that is zero", lambda d: d["decisions"]["qa"].update(
        trust_assessed=0), True),
    ("a trust assessment that is a string", lambda d: d["decisions"]["qa"].update(
        trust_assessed="false"), True),
    ("an index that is False for the first fallback", lambda d: d["decisions"]["qa"].update(
        index=False), True),
    ("another model of the declared provider looked at", lambda d: d["decisions"]["qa"][
        "considered"][0].update(model="sonnet"), True),
    ("another model of the chosen provider looked at", lambda d: d["decisions"]["qa"][
        "considered"][1].update(model="gpt-9"), True),
    ("a lone surrogate in the reason", lambda d: d["decisions"]["qa"].update(
        reason="\ud800"), True),
    ("a route that is not an object", lambda d: d["decisions"]["qa"].update(route="codex"), True),
    ("a considered list that is not a list", lambda d: d["decisions"]["qa"].update(
        considered={"provider": "claude"}), True),
    ("decisions that are not an object", lambda d: d.update(decisions=[]), True),
    ("a state that is a list", lambda d: d["decisions"]["qa"]["considered"][0].update(
        state=["LIMITED"]), True),
    ("an outcome that is a list", lambda d: d["decisions"]["qa"].update(outcome=["FALLBACK"]), True),
    ("an index that is a float", lambda d: d["decisions"]["qa"].update(index=0.0), True),
    ("a considered entry that is a list", lambda d: d["decisions"]["qa"]["considered"].__setitem__(
        1, ["codex", "gpt-5", "NO_EVIDENCE"]), True),
    ("a decision that is a list", lambda d: d["decisions"].update(qa=[]), True),
    ("a route skipped over", lambda d: d["decisions"]["qa"].update(
        index=1, route={"provider": "kimi", "model": "k2", "effort": "low"}), True),
    ("an edit that is not re-signed", lambda d: d["decisions"]["qa"].update(index=1), False),
    ("a decision time edited without re-signing", lambda d: d.update(
        decided_at="2030-01-01T00:00:00+00:00"), False),
    ("a digest that is not the body's", lambda d: d.update(digest="0" * 64), False),
    ("a digest that is not a string", lambda d: d.update(digest=None), False),
])
def test_a_signed_record_that_names_what_the_policy_does_not_declare_is_refused(
        root, monkeypatch, label, edit, resign):
    """Kills M11 (the route is taken from the record, so a record can name an undeclared one), M12
    (the record's digest is not checked), M26 (the chosen route is not checked against what the
    record says was read), M32 (a trust assessment is accepted), M34 (a record of another policy is
    applied), M35 (the decision time is not checked)."""
    policy = _policy()      # built outside `pytest.raises`: a refusal of the policy is not the one under test
    run, good = _written(root)
    doc = json.loads(good)
    edit(doc)
    if resign:
        _sign(doc)
    (run / FILE).write_text(json.dumps(doc, sort_keys=True, indent=1))
    before = (run / FILE).read_bytes()
    _no_health(monkeypatch)
    with pytest.raises(RoutingRefused):
        _go(policy, run)
    assert (run / FILE).read_bytes() == before


def test_a_record_of_a_policy_that_declared_fallbacks_is_refused_by_one_that_does_not(root, monkeypatch):
    """Kills M36 (a policy without fallbacks ignores an existing record and is routed as if nothing
    had been decided)."""
    run, good = _written(root)
    bare = routing.parse({"schema": routing.SCHEMA, "default": {"model": "sonnet"},
                          "roles": {"qa": {"provider": "claude", "model": "opus"}}})
    _no_health(monkeypatch)
    with pytest.raises(RoutingRefused):
        _go(bare, run)
    assert (run / FILE).read_bytes() == good


# --------------------------------------------------------------------------- #
# R16 -- structure: a decision module, not a dispatcher
# --------------------------------------------------------------------------- #

def test_the_failover_code_is_structurally_a_decision_and_one_durable_create():
    """Kills M16 and M23 (a raw write instead of the one durable create), and any process, sleep, clock
    or removal call added to the module."""
    src = Path(routing.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
    assert imported <= {"__future__", "hashlib", "json", "os", "re", "stat", "collections.abc",
                        "dataclasses", "datetime", "pathlib", "types", "typing", ".", ".contracts",
                        ".dispatchrecovery", ".durable_io", ".health", ".telemetry"}, imported
    forbidden = {"write_text", "write_bytes", "open", "unlink", "rename", "remove", "rmdir",
                 "touch", "mkdir", "makedirs", "chmod", "system", "Popen", "sleep", "kill",
                 "fsync", "ftruncate", "truncate", "link", "symlink", "write"}
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            seen.add(node.attr)
        elif isinstance(node, ast.Name) and node.id in forbidden | {"exec", "eval", "print"}:
            seen.add(node.id)
    # `os.open` is the one read open this module has (the O_RDONLY | O_NOFOLLOW reads of the
    # policy file and of the record); a flag that writes is not among its arguments
    assert seen <= {"open"}, seen
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "open"):
            flags = ast.unparse(node.args[1]) if len(node.args) > 1 else ""
            assert "O_RDONLY" in flags and not any(
                w in flags for w in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC")), flags
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and ast.unparse(n.func) in {"durable_create", "durable_io.durable_create"}]
    assert len(calls) == 1    # the one write; a lost race reads the winner's record, never writes again
    assert "os.replace" not in src and "shutil" not in src
