"""P3-41 slice 4: route receipts, route kinds, the weaker-route verification, park failover as evidence.

Slices 1-3 decide a run's route (at its start and at a park) and record the decision. These tests
pin what slice 4 adds to that record and to the run: every route change carries a **route receipt**
(old and new route, reason, role, required and satisfied capabilities, trust class, policy,
whether additional verification is required) that a reader reproduces from the policy; a fallback
may be an operator-registered **account** or an approved **local** runtime; a run with a role on a
**weaker** route is accepted only when the policy's verification is met (the controller acts on the
receipt); and a park failover is health evidence for the routes it passed over.

The production path is driven through the entry points the operator uses -- `hoh run`, `hoh
resume-quota` and `hoh status` through `cli.main` (`--iterations 0 --no-herdr`: no role starts, no
agent runs, no real account or credential exists; the providers are faked by parks under a temporary
root). The controller's acceptance is driven with the fake dispatcher of `tests/test_controller.py`.

Mutant ids (`Q1`..) are the spec's table; each test says which it kills.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_controller import FakeDispatcher, build, plan_for, qa_pass
from test_controller import spec as _controller_spec
from test_park_failover import (
    AT, HOUR, PARK1, START, World, _doc, _objects, _reader,
)
from test_park_failover import _receipt as _park_receipt

from hoh import cli, controller, health, quota, routing
from hoh.contracts import Condition, Role, Stage
from hoh.controller import Controller
from hoh.dispatchrecovery import CAPABILITY_REQUIREMENTS
from hoh.routing import RoutingRefused
from hoh.store import RunStore

spec = _controller_spec          # the fixture of test_controller, by name (the tests below take it as `spec`)
ACCOUNT_B = {"provider": "claude", "model": "opus", "kind": "account", "account": "acct-b"}
ALL_CAPS = ["read_repo", "run_shell", "structured_answer", "write_worktree"]
LOCAL = {"provider": "pi", "model": "qwen3-coder:30b", "kind": "local", "capabilities": ALL_CAPS}
CODEX = {"provider": "codex", "model": "gpt-5"}
RECEIPT_KEYS = {"schema", "cause", "role", "old_route", "new_route", "reason", "failure_class",
                "required_capabilities", "satisfied_capabilities", "capabilities_declared",
                "trust_class", "policy", "additional_verification_required", "verification"}


def _policy_doc(*fallbacks, role: str = "qa", **top) -> dict:
    doc = {"schema": routing.SCHEMA, "default": {"model": "sonnet", "effort": "high"},
           "roles": {role: {"provider": "claude", "model": "opus",
                            "fallbacks": list(fallbacks)}}}
    doc.update(top)
    return doc


def _limited(provider: str, account: str, hours: int = 3) -> health.ProviderHealth:
    return health.ProviderHealth(
        provider=provider, account_handle=account, state=health.HealthState.LIMITED,
        not_before=(AT + hours * HOUR).isoformat(), runs=("x",), failures=(("USAGE_LIMIT", 1),))


def _plan(policy, run: Path, *groups, now: datetime = AT, begun: bool = False, **kw):
    profiles = {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "claude"}
    return routing.plan_failover(policy, run_dir=run, profiles=profiles, now=now, begun=begun,
                                 reader=_reader(*groups), **kw)


def _fresh_run(tmp_path: Path, name: str = "r1") -> Path:
    run = tmp_path / "runs" / name
    run.mkdir(parents=True)
    return run


def _resign(doc: dict) -> bytes:
    doc["digest"] = routing._body_digest({k: v for k, v in doc.items() if k != "digest"})
    return (json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode("ascii")


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    return World(tmp_path, monkeypatch, capsys)


def _status(world, run_id: str = "r1") -> dict:
    assert cli.main(["--root", str(world.root), "status", run_id]) == 0
    return _objects(world.capsys.readouterr().out)[0]


def _new_run(world, run_id: str) -> RunStore:
    store = RunStore(world.root, run_id)
    store.write_state(Controller.new_state(
        run_id=run_id, repo_path=world.repo, project_name="project", spec_path=world.spec))
    return store


# --------------------------------------------------------------------------- #
# S-A -- the production path: `hoh run`, `hoh resume-quota`, `hoh status`
# --------------------------------------------------------------------------- #

def test_hoh_run_starts_on_an_account_route_with_a_route_receipt_and_hoh_status_shows_it(world):
    """Kills Q1 (no receipt is written), Q2 (the account of the route is not the run's account:
    the receipts and the health reading would name the declared one), Q3 (the receipt omits the
    verification the policy asks for), Q13 (a fallback without a declared class is equivalent)."""
    world.policy(_doc(ACCOUNT_B, verification={"weaker_route": {"min_discriminating": 2}}))
    world.other("o", provider="claude")                    # claude's limit on account acct-a
    store = _new_run(world, "r1")
    rc, out, err = world.run("r1")
    assert rc == 0, err
    d = world.built[0]
    assert d.profiles[Role.QA] == "claude" and d.models[Role.QA] == "opus"
    record = json.loads((store.dir / START).read_bytes())
    decision = record["decisions"]["qa"]
    assert decision["outcome"] == "FALLBACK" and decision["index"] == 0
    assert decision["route"] == {"provider": "claude", "model": "opus", "kind": "account",
                                 "account": "acct-b"}
    assert decision["considered"][1] == {"provider": "claude", "model": "opus",
                                         "state": "NO_EVIDENCE", "not_before": None,
                                         "account": "acct-b"}
    digest = routing.load(world.policy_file).digest
    assert decision["receipt"] == {
        "schema": "hoh-route-receipt/1", "cause": "start", "role": "qa",
        "old_route": {"provider": "claude", "model": "opus", "effort": "high"},
        "new_route": decision["route"], "reason": "LIMITED", "failure_class": None,
        "required_capabilities": list(CAPABILITY_REQUIREMENTS["qa"]),
        "satisfied_capabilities": list(CAPABILITY_REQUIREMENTS["qa"]),
        "capabilities_declared": False, "trust_class": "weaker",
        "policy": {"schema": routing.SCHEMA, "digest": digest},
        "additional_verification_required": True,
        "verification": {"min_discriminating": 2}}
    # the run says it: the account it runs under, and the verification the controller will ask for
    (shown,) = [o["routing_policy"] for o in _objects(out) if "routing_policy" in o]
    assert shown["verification_required"] == {
        "required": True, "roles": ["qa"], "measures": {"min_discriminating": 2}}
    assert shown["failover"]["qa"]["receipt"]["trust_class"] == "weaker"
    (recovery,) = [o["recovery"] for o in _objects(out) if "recovery" in o]
    assert recovery["accounts"] == {"qa": "acct-b"}
    assert d.routing_evidence["verification"] == shown["verification_required"]
    assert d.routing_evidence["resolved"]["qa"]["provider"]["account"] == "acct-b"
    role = _status(world)["routing_failover"]["roles"]["qa"]
    assert role["trust_class"] == "weaker" and role["additional_verification_required"] is True


def test_the_receipt_of_a_park_failover_names_the_route_it_left_the_failure_and_the_policy(world):
    """Kills Q4 (the park receipt's old route is the declared one instead of what the parked
    dispatch ran on), Q5 (no failure class), Q6 (the spent record still demands the
    verification), Q42 (the default verification is empty), Q13 (a fallback is equivalent)."""
    store = world.started_run("r1")
    world.park("r1")                                       # claude, account acct-a, model m-1
    first = world.result()
    assert first["action"] == "resumed_failover", first
    record = json.loads((store.dir / PARK1).read_bytes())
    receipt = record["decisions"]["qa"]["receipt"]
    assert set(receipt) == RECEIPT_KEYS
    assert receipt["cause"] == "park" and receipt["role"] == "qa"
    assert receipt["old_route"] == {"provider": "claude", "model": "m-1", "account": "acct-a"}
    assert receipt["new_route"] == CODEX and receipt["reason"] == "LIMITED"
    assert receipt["failure_class"] == "USAGE_LIMIT"
    assert receipt["trust_class"] == "weaker" and receipt["additional_verification_required"]
    assert receipt["verification"] == {"no_open_gaps": True}      # the default: none declared
    assert receipt["policy"]["digest"] == routing.load(world.policy_file).digest
    # `hoh status`: the park record is visible (s3's Q4), with its trust class
    (park,) = _status(world)["routing_failover"]["park_failovers"]
    assert park == {"file": PARK1, "number": 1, "park_id": record["park_id"], "role": "qa",
                    "route": CODEX, "reason": "LIMITED", "failure_class": "USAGE_LIMIT",
                    "decided_at": record["decided_at"], "return_at": record["return_at"],
                    "trust_class": "weaker", "additional_verification_required": True}
    # the next `hoh run` builds on the fallback and says what the controller will ask
    rc, out, err = world.run()
    assert rc == 0, err
    asked = world.built[0].routing_evidence["verification"]
    assert asked == {"required": True, "roles": ["qa"], "measures": {"no_open_gaps": True}}
    # at the record's return the run is back on its declared route: nothing is asked any more
    policy = routing.load(world.policy_file)
    returns = datetime.fromisoformat(record["return_at"])
    profiles = {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "claude"}
    for at, wants in ((returns - timedelta(seconds=1), True), (returns, False)):
        plan = routing.plan_failover(policy, run_dir=store.dir, profiles=profiles, now=at,
                                     begun=True)
        try:
            assert ("verification" in plan.resolved.evidence) is wants, at
        finally:
            plan.close()


def test_a_run_parked_on_one_account_resumes_on_the_other_and_is_recorded_under_it(world):
    """Kills Q2 again at a park (the park failover's account route is not the run's account), Q43
    (the route back is matched without its account: an account route is taken for the declared
    one and the record cannot name its return)."""
    world.policy(_doc(ACCOUNT_B))
    store = world.started_run("r1")
    world.park("r1")                                       # claude on acct-a, three hours out
    first = world.result()
    assert first["action"] == "resumed_failover", first
    record = json.loads((store.dir / PARK1).read_bytes())
    decision = record["decisions"]["qa"]
    assert decision["route"] == {"provider": "claude", "model": "opus", "kind": "account",
                                 "account": "acct-b"}
    assert decision["considered"][1]["account"] == "acct-b" and "account" not in (
        decision["considered"][0])
    receipt = decision["receipt"]
    assert receipt["old_route"] == {"provider": "claude", "model": "m-1", "account": "acct-a"}
    assert receipt["new_route"] == decision["route"] and receipt["failure_class"] == "USAGE_LIMIT"
    rc, out, err = world.run()
    assert rc == 0, err
    (recovery,) = [o["recovery"] for o in _objects(out) if "recovery" in o]
    assert recovery["accounts"] == {"qa": "acct-b"}
    d = world.built[0]
    assert d.profiles[Role.QA] == "claude" and d.models[Role.QA] == "opus"
    assert d.routing_evidence["resolved"]["qa"]["provider"]["account"] == "acct-b"


def test_the_route_a_run_returns_to_is_matched_with_its_account(tmp_path):
    """Kills Q43: a run that began on the account route and parks on it returns to *that* route
    (its own reset bounds the record), not to the declared route of the same provider and model
    that resets later."""
    from types import SimpleNamespace
    policy = routing.parse(_policy_doc(ACCOUNT_B, CODEX))
    run = _fresh_run(tmp_path)
    start = _plan(policy, run, _limited("claude", "acct-a", 5))         # account A out for 5 h
    start.commit()
    start.close()
    assert json.loads((run / START).read_bytes())["decisions"]["qa"]["route"]["account"] == "acct-b"
    parked = SimpleNamespace(
        provenance=SimpleNamespace(role="qa", provider="claude", account_handle="acct-b",
                                   model="opus"),
        park_id="b" * 32, reason="USAGE_LIMIT", retry_at=(AT + 2 * HOUR).isoformat())
    made = routing.record_park_failover(policy, run_dir=run, receipt=parked, now=AT,
                                        reader=_reader(_limited("claude", "acct-a", 5)))
    assert made.route.provider == "codex"                  # both claude routes are out
    assert datetime.fromisoformat(made.return_at) == AT + 2 * HOUR + timedelta(
        seconds=routing.RETURN_DWELL_SECONDS)


def test_a_second_run_that_starts_inside_the_window_starts_on_the_fallback(world):
    """Kills Q7 (a park failover is no health evidence: the second run starts on the limited
    provider, meets the limit and parks; s3's Q3)."""
    world.started_run("r1")
    world.park("r1")
    first = world.result()
    assert first["action"] == "resumed_failover"
    record = json.loads((world.root / "r1" / PARK1).read_bytes())
    passed_over = record["decisions"]["qa"]["considered"][0]
    # nothing is parked any more (the lift ended the park) and a second run starts now
    store = _new_run(world, "r2")
    rc, out, err = world.run("r2")
    assert rc == 0, err
    d = world.built[0]
    assert d.profiles[Role.QA] == "codex"
    decision = json.loads((store.dir / START).read_bytes())["decisions"]["qa"]
    assert decision["outcome"] == "FALLBACK" and decision["reason"] == "LIMITED"
    assert decision["considered"][0]["not_before"] == passed_over["not_before"]
    assert decision["receipt"]["cause"] == "start"


def test_an_explicit_role_account_for_a_failed_over_account_role_is_replaced_by_the_routes_and_said(
        world):
    """Kills Q2 again (the route's account is not recorded) and Q8 (the typed account stays the
    role's account, or the run is refused: a provider stop turned into an exit 2 by the option the
    operator typed for the route the role left). The typed handle is never echoed."""
    world.policy(_doc(ACCOUNT_B))
    world.other("o", provider="claude")
    store = _new_run(world, "r1")
    rc, out, err = world.run("r1", "--role-account", "qa=acct-z", "--role-account", "planner=acct-p")
    assert rc == 0, err
    assert "--role-account for qa is not used: the role runs under the account route acct-b" in err
    assert "acct-z" not in err and "acct-z" not in out
    (recovery,) = [o["recovery"] for o in _objects(out) if "recovery" in o]
    assert recovery["accounts"] == {"planner": "acct-p", "qa": "acct-b"}
    assert json.loads((store.dir / START).read_bytes())["decisions"]["qa"]["outcome"] == "FALLBACK"
    # the same handle as the route's: nothing to say
    rc, out, err = world.run("r1", "--role-account", "qa=acct-b")
    assert rc == 0 and "is not used" not in err, err


def test_a_run_that_stays_on_its_declared_route_is_recorded_and_shown_exactly_as_before(tmp_path):
    """A compatibility row (green on the base): nothing the slice adds shows up for a decision that
    changes no route. Kills Q41 (a receipt, a verification or a new key on a DECLARED decision, or
    on a policy that declares none of the new words)."""
    doc = _policy_doc(CODEX)
    policy = routing.parse(doc)
    assert set(policy.evidence()) == {"schema", "source", "digest", "file_sha256", "default",
                                      "roles"}
    assert policy.evidence()["roles"]["qa"]["fallbacks"] == [CODEX]
    run = _fresh_run(tmp_path)
    plan = _plan(policy, run)                          # nothing limits claude: DECLARED
    plan.commit()
    plan.close()
    record = json.loads((run / START).read_bytes())
    assert set(record["decisions"]["qa"]) == {"outcome", "index", "route", "reason",
                                              "trust_assessed", "considered"}
    assert record["decisions"]["qa"]["considered"] == [
        {"provider": "claude", "model": "opus", "state": "NO_EVIDENCE", "not_before": None}]
    plan = _plan(policy, run, begun=True)
    try:
        assert plan.decided and "verification" not in plan.resolved.evidence
        assert getattr(plan.resolved, "accounts", {}) == {}
    finally:
        plan.close()
    assert routing.failover_summary(run) == {"decided_at": record["decided_at"], "roles": {
        "qa": {"outcome": "DECLARED", "route": {"provider": "claude", "model": "opus",
                                                "effort": "high"},
               "reason": "NO_EVIDENCE", "not_before": None}}}


# --------------------------------------------------------------------------- #
# S-B -- route kinds: declarable, selectable by `_decide`, refused when they cannot serve the role
# --------------------------------------------------------------------------- #

def test_an_account_route_is_selected_when_its_own_account_is_not_the_limited_one(tmp_path):
    """Kills Q9 (the health of the account route is read at provider level: an account that is
    fine is passed over for a limit on another), Q11 (a group whose account is unknown is taken
    as another account)."""
    policy = routing.parse(_policy_doc(ACCOUNT_B, CODEX))

    asked = []

    def chosen(*groups):
        asked.append(groups)
        run = _fresh_run(tmp_path, f"r{len(asked)}")     # a run decides once: one per question
        plan = _plan(policy, run, *groups)
        try:
            (decision,) = plan.resolved.evidence["failover"]["decisions"].values()
            return decision["index"], [c["state"] for c in decision["considered"]]
        finally:
            plan.close()

    # account A is out: the declared route (no account named: any account of the provider counts)
    assert chosen(_limited("claude", "acct-a")) == (0, ["LIMITED", "NO_EVIDENCE"])
    # account B is out too: the next declared route
    assert chosen(_limited("claude", "acct-a"), _limited("claude", "acct-b")) == (
        1, ["LIMITED", "LIMITED", "NO_EVIDENCE"])
    # only B is out: the declared route is out as well (any account counts against it)
    assert chosen(_limited("claude", "acct-b")) == (1, ["LIMITED", "LIMITED", "NO_EVIDENCE"])
    # a limit on an account nobody named could be B: the route is not taken
    assert chosen(_limited("claude", "NOT_AVAILABLE")) == (
        1, ["LIMITED", "LIMITED", "NO_EVIDENCE"])
    # another provider's limit says nothing about this one
    assert chosen(_limited("codex", "acct-a")) == (None, ["NO_EVIDENCE"])   # the declared route


def test_a_local_route_is_selected_when_the_cloud_is_out_and_its_receipt_says_what_it_can(tmp_path):
    """Kills Q13 (a local route is `equivalent` by default: trust inherited by silence); the
    receipt of a local route names its declared capabilities and its kind."""
    policy = routing.parse(_policy_doc(LOCAL))
    plan = _plan(policy, _fresh_run(tmp_path), _limited("claude", "acct-a"),
                 _limited("claude", "a"))
    try:
        (decision,) = plan.resolved.evidence["failover"]["decisions"].values()
        assert plan.resolved.profiles[Role.QA] == "pi" and plan.resolved.models[Role.QA] == (
            "qwen3-coder:30b")
        assert decision["route"] == {"provider": "pi", "model": "qwen3-coder:30b",
                                     "kind": "local", "capabilities": ALL_CAPS}
        receipt = decision["receipt"]
        assert receipt["new_route"] == decision["route"] and receipt["capabilities_declared"]
        assert receipt["satisfied_capabilities"] == list(CAPABILITY_REQUIREMENTS["qa"])
        assert receipt["trust_class"] == "weaker" and receipt["additional_verification_required"]
    finally:
        plan.close()
    # the operator declares it equivalent: no additional verification, nothing asked
    equal = routing.parse(_policy_doc({**LOCAL, "quality": "equivalent"}))
    plan = _plan(equal, _fresh_run(tmp_path, "r2"), _limited("claude", "acct-a"))
    try:
        (decision,) = plan.resolved.evidence["failover"]["decisions"].values()
        assert decision["receipt"]["trust_class"] == "equivalent"
        assert decision["receipt"]["additional_verification_required"] is False
        assert decision["receipt"]["verification"] is None
        assert "verification" not in plan.resolved.evidence
    finally:
        plan.close()


def test_a_route_that_cannot_serve_its_role_or_is_not_plainly_declared_is_refused(tmp_path):
    """Kills Q15 (a local route without a required capability is accepted), Q20 (the same
    provider and model under another account is taken for a repeat of the declared route); the
    other words of the table (a kind, an account handle that is a credential and is not echoed, a
    quality, a capability the policy does not know) are refused by name."""
    secret = "sk-live-0123456789abcdefghij"
    cases = {
        "local without capabilities": ({"provider": "pi", "model": "m", "kind": "local"},
                                       "a local route declares its capabilities"),
        "qa lacks run_shell": ({**LOCAL, "capabilities": ["read_repo", "structured_answer"]},
                               "lacks a capability the role requires (run_shell"),
        "capability word": ({**LOCAL, "capabilities": ["read_repo", "fly"]},
                            "capabilities is not a non-empty list"),
        "duplicate capability": ({**LOCAL, "capabilities": ["read_repo", "read_repo"]},
                                 "capabilities is not a non-empty list"),
        "capabilities not a list": ({**LOCAL, "capabilities": "read_repo"},
                                    "capabilities is not a non-empty list"),
        "kind unknown": ({**CODEX, "kind": "cloud"}, "kind is not one of"),
        "kind in another case": ({**ACCOUNT_B, "kind": "Account"}, "kind is not one of"),
        "account without kind": ({**CODEX, "account": "acct-b"}, "exactly the routes of kind"),
        "kind account without account": ({**CODEX, "kind": "account"},
                                         "exactly the routes of kind"),
        "unknown account": ({**ACCOUNT_B, "account": "NOT_AVAILABLE"}, "not a plain account"),
        "empty account": ({**ACCOUNT_B, "account": " "}, "not a plain account"),
        "account not a string": ({**ACCOUNT_B, "account": 5}, "not a plain account"),
        "account is a key": ({**ACCOUNT_B, "account": secret}, "not a plain account"),
        "account with a newline": ({**ACCOUNT_B, "account": "acct-b\n"}, "not a plain account"),
        "account with a blank": ({**ACCOUNT_B, "account": " acct-b"}, "not a plain account"),
        "quality unknown": ({**CODEX, "quality": "stronger"}, "quality is not one of"),
        "kind account, same account twice": (None, "repeats a route"),
    }
    for name, (route, why) in cases.items():
        fallbacks = [ACCOUNT_B, dict(ACCOUNT_B)] if route is None else [route]
        with pytest.raises(RoutingRefused) as exc:
            routing.parse(_policy_doc(*fallbacks))
        assert why in str(exc.value), (name, str(exc.value))
        assert secret not in str(exc.value), name
    # the developer needs write_worktree, not structured_answer
    with pytest.raises(RoutingRefused, match=r"lacks a capability the role requires \(write"):
        routing.parse(_policy_doc({**LOCAL, "capabilities": ["read_repo", "run_shell"]},
                                  role="developer"))
    # the same provider and model under another account is another route, not a repeat
    assert routing.parse(_policy_doc(ACCOUNT_B)).fallbacks[Role.QA][0].account == "acct-b"
    # kinds on the declared route are not known to this policy
    doc = _policy_doc(CODEX)
    doc["roles"]["qa"]["kind"] = "local"
    with pytest.raises(RoutingRefused, match="has a key this policy does not know"):
        routing.parse(doc)


def test_the_verification_clause_asks_only_for_what_the_controller_can_measure(tmp_path):
    """Kills Q21 (a requirement word this product does not act on is accepted and ignored), Q22
    (a bool is a number of criteria), Q22b (the ceiling is exclusive); the clause is also echoed
    in the policy's evidence (the run's pin)."""
    ok = routing.parse(_policy_doc(CODEX, verification={"weaker_route": {
        "min_discriminating": 2, "no_open_gaps": True}}))
    assert dict(ok.verification) == {"min_discriminating": 2, "no_open_gaps": True}
    assert ok.evidence()["verification"] == {"weaker_route": {
        "min_discriminating": 2, "no_open_gaps": True}}
    assert routing.parse(_policy_doc(CODEX)).verification == {}
    assert "verification" not in routing.parse(_policy_doc(CODEX)).evidence()
    assert routing.parse(_policy_doc(CODEX)).digest != ok.digest
    bad = {
        "not an object": ["weaker_route"],
        "no weaker_route": {"stronger_route": {"no_open_gaps": True}},
        "two keys": {"weaker_route": {"no_open_gaps": True}, "x": 1},
        "empty": {"weaker_route": {}},
        "independent_review": {"weaker_route": {"independent_review": True}},
        "no_open_gaps false": {"weaker_route": {"no_open_gaps": False}},
        "no_open_gaps one": {"weaker_route": {"no_open_gaps": 1}},
        "min zero": {"weaker_route": {"min_discriminating": 0}},
        "min over": {"weaker_route": {"min_discriminating": routing.MAX_DISCRIMINATING + 1}},
        "min bool": {"weaker_route": {"min_discriminating": True}},
        "min text": {"weaker_route": {"min_discriminating": "2"}},
        "min float": {"weaker_route": {"min_discriminating": 2.0}},
    }
    for name, clause in bad.items():
        with pytest.raises(RoutingRefused, match="verification"):
            routing.parse(_policy_doc(CODEX, verification=clause))
        assert name
    top = routing.MAX_DISCRIMINATING
    assert routing.parse(_policy_doc(CODEX, verification={
        "weaker_route": {"min_discriminating": top}})).verification["min_discriminating"] == top


# --------------------------------------------------------------------------- #
# S-C -- the receipt is reproduced from the policy; an older record is held to the same rule
# --------------------------------------------------------------------------- #

def _recorded_start(tmp_path, policy, *groups) -> tuple[Path, dict]:
    run = _fresh_run(tmp_path)
    plan = _plan(policy, run, *groups)
    plan.commit()
    plan.close()
    return run, json.loads((run / START).read_bytes())


def test_a_receipt_that_says_other_than_the_policy_gives_is_refused(tmp_path):
    """Kills Q24 (the receipt is read back without being reproduced: a record that claims an
    `equivalent` route or no verification is believed), Q25 (a receipt on a DECLARED decision)."""
    policy = routing.parse(_policy_doc(CODEX, verification={"weaker_route": {
        "min_discriminating": 2}}))
    run, good = _recorded_start(tmp_path, policy, _limited("claude", "acct-a"))
    assert good["decisions"]["qa"]["receipt"]["verification"] == {"min_discriminating": 2}

    def edit(fn):
        doc = json.loads(json.dumps(good))
        fn(doc["decisions"]["qa"]["receipt"], doc)
        return doc

    def key(name, value):
        return lambda r, d: r.__setitem__(name, value)

    mutations = {
        "trust class": key("trust_class", "equivalent"),
        "additional verification": key("additional_verification_required", False),
        "verification": key("verification", {"min_discriminating": 1}),
        "verification none": key("verification", None),
        "satisfied": key("satisfied_capabilities", ["read_repo"]),
        "required": key("required_capabilities", ["read_repo"]),
        "declared flag": key("capabilities_declared", True),
        "policy digest": lambda r, d: r["policy"].__setitem__("digest", "0" * 64),
        "policy schema": lambda r, d: r["policy"].__setitem__("schema", "x"),
        "cause": key("cause", "park"),
        "old route": lambda r, d: r["old_route"].__setitem__("model", "haiku"),
        "old route none": key("old_route", None),
        "failure class on a start": key("failure_class", "USAGE_LIMIT"),
        "new route": lambda r, d: r["new_route"].__setitem__("model", "gpt-9"),
        "reason": key("reason", "DEGRADED"),
        "role": key("role", "planner"),
        "schema": key("schema", "hoh-route-receipt/2"),
        "extra key": key("note", "x"),
        "missing key": lambda r, d: r.pop("verification"),
        "not an object": lambda r, d: d["decisions"]["qa"].__setitem__("receipt", ["x"]),
    }
    for name, fn in mutations.items():
        (run / START).write_bytes(_resign(edit(fn)))
        with pytest.raises(RoutingRefused):
            _plan(policy, run, begun=True).close()
    (run / START).write_bytes(_resign(edit(lambda r, d: None)))      # the control: accepted
    plan = _plan(policy, run, begun=True)
    assert plan.decided
    plan.close()
    # a DECLARED decision has no route change and no receipt
    healthy, doc = _recorded_start(tmp_path / "h", policy)
    assert doc["decisions"]["qa"]["outcome"] == "DECLARED" and "receipt" not in doc["decisions"]["qa"]
    doc["decisions"]["qa"]["receipt"] = good["decisions"]["qa"]["receipt"]
    (healthy / START).write_bytes(_resign(doc))
    with pytest.raises(RoutingRefused, match="no route change"):
        _plan(policy, healthy, begun=True).close()


def test_a_record_written_before_receipts_is_held_to_the_same_rule(tmp_path):
    """Kills Q26 (a fallback record without a receipt is a record without the requirement: the
    run began on a weaker route before this slice and is verified as if it had not); the derived
    receipt is not written back into the record (the bytes are compared)."""
    policy = routing.parse(_policy_doc(CODEX))
    run, doc = _recorded_start(tmp_path, policy, _limited("claude", "acct-a"))
    del doc["decisions"]["qa"]["receipt"]
    raw = _resign(doc)
    (run / START).write_bytes(raw)
    plan = _plan(policy, run, begun=True)
    try:
        seen = plan.resolved.evidence
        assert seen["verification"] == {"required": True, "roles": ["qa"],
                                        "measures": {"no_open_gaps": True}}
        receipt = seen["failover"]["decisions"]["qa"]["receipt"]
        assert receipt["source"] == "derived" and receipt["trust_class"] == "weaker"
        assert receipt["cause"] == "start" and receipt["failure_class"] is None
    finally:
        plan.close()
    assert (run / START).read_bytes() == raw                 # nothing was written back
    # an equivalent route of an older record asks for nothing
    equal = routing.parse(_policy_doc({**CODEX, "quality": "equivalent"}))
    run2, doc2 = _recorded_start(tmp_path / "e", equal, _limited("claude", "acct-a"))
    del doc2["decisions"]["qa"]["receipt"]
    (run2 / START).write_bytes(_resign(doc2))
    plan = _plan(equal, run2, begun=True)
    try:
        assert "verification" not in plan.resolved.evidence
    finally:
        plan.close()


# --------------------------------------------------------------------------- #
# S-D -- the controller acts on the receipt
# --------------------------------------------------------------------------- #

def _asked(**measures) -> dict:
    return {"verification": {"required": True, "roles": ["qa"], "measures": measures}}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _drive(tmp_path, spec, evidence, qa_json=qa_pass):
    """A controller over a repository of its own (the developer's marker file would make the
    check green on the next run's base, so no two drives share one)."""
    repo = tmp_path / "project"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    d = FakeDispatcher(repo=repo, plan_json=plan_for, qa_json=qa_json)
    if evidence is not None:
        d.routing_evidence = evidence
    ctrl, state, store = build(tmp_path, repo, spec, d)
    return ctrl, state, store, d


def test_a_candidate_on_a_weaker_route_is_accepted_only_when_the_policy_verification_is_met(
        tmp_path, spec):
    """Kills Q28 (the receipt is not acted on: an all-green candidate on a weaker route is
    accepted), Q29b (`min_discriminating` is exclusive), Q30 (`no_open_gaps` ignores QA's open
    gaps); a route that needs nothing is not held (the last cases)."""
    # a policy asking for two discriminating criteria: one is not enough
    ctrl, state, store, d = _drive(tmp_path / "a", spec, _asked(min_discriminating=2))
    out = ctrl.run_iteration(state)
    assert not out.accepted and out.reason.startswith(controller.WEAKER_ROUTE_HOLD), out.reason
    assert "at least 2 criteria that demonstrate the increment (1 here)" in out.reason
    assert state.stage is Stage.PLANNING and state.last_accepted_candidate is None
    assert state.working_candidate is not None and state.usage.loops_without_progress == 1
    assert any("candidate rejected: " + controller.WEAKER_ROUTE_HOLD in h and "(1 here)" in h
               for h in state.history)                    # the history says what was missing
    # one is enough when one is asked for
    ctrl, state, store, d = _drive(tmp_path / "b", spec, _asked(min_discriminating=1))
    out = ctrl.run_iteration(state)
    assert out.accepted and "all criteria demonstrated -- weaker-route verification met" in out.reason
    # QA reports an open gap: held under `no_open_gaps`, accepted without the measure
    gap = lambda s: {**qa_pass(s), "open_gaps": ["the edge case of an empty list"]}  # noqa: E731
    ctrl, state, store, d = _drive(tmp_path / "c", spec, _asked(no_open_gaps=True), gap)
    out = ctrl.run_iteration(state)
    assert not out.accepted and "asks QA for no open gap (1 reported)" in out.reason
    ctrl, state, store, d = _drive(tmp_path / "d", spec, _asked(min_discriminating=1), gap)
    assert ctrl.run_iteration(state).accepted
    # nothing asked (no routing, no failover, or every role on an equivalent route): unchanged
    for n, evidence in enumerate((None, {}, {"failover": {}})):
        ctrl, state, store, d = _drive(tmp_path / f"e{n}", spec, evidence)
        out = ctrl.run_iteration(state)
        assert out.accepted and "weaker-route" not in out.reason, out.reason


def test_min_discriminating_counts_the_criteria_that_demonstrate_the_increment_not_every_pass(
        tmp_path, spec):
    """Kills Q29: two criteria both PASS, but only one demonstrates the increment (the other is
    green on the predecessor state too): a policy that asks for two is not met, and one is."""
    def two(state):
        doc = plan_for(state)
        doc["acceptance_checks"].append({
            "check_id": "K2", "description": "the module is still there", "command": "test -f app.py",
            "expect_exit": 0, "preserves": False})
        return doc

    def both(state):
        rows = qa_pass(state)["verdicts"] + qa_pass(state, "K2")["verdicts"]
        return {"verdicts": rows, "open_gaps": [], "summary": "all good"}

    def run(path, **measures):
        ctrl, state, store, d = _drive(path, spec, _asked(**measures), both)
        d.plan_json = two
        return ctrl.run_iteration(state)

    out = run(tmp_path / "two", min_discriminating=2)
    assert not out.accepted and "(1 here)" in out.reason, out.reason
    assert [v.discriminates for v in out.verdicts if v.check_id == "K2"] == [False]
    assert run(tmp_path / "one", min_discriminating=1).accepted


def test_a_receipt_the_controller_cannot_read_holds_the_acceptance(tmp_path, spec):
    """Kills Q33 (a malformed requirement is read as no requirement: fail open), Q33b (an unknown
    measure is ignored)."""
    odd = {
        "measures not an object": {"verification": {"required": True, "roles": ["qa"],
                                                    "measures": "no_open_gaps"}},
        "roles not a list": {"verification": {"required": True, "roles": "qa", "measures": {}}},
        "required not true": {"verification": {"required": 1, "roles": ["qa"], "measures": {}}},
        "not an object": {"verification": ["qa"]},
        "unknown measure": _asked(independent_review=True),
        "bool as a number": _asked(min_discriminating=True),
        "text as a number": _asked(min_discriminating="1"),
    }
    for n, (name, evidence) in enumerate(odd.items()):
        ctrl, state, store, d = _drive(tmp_path / f"o{n}", spec, evidence)
        out = ctrl.run_iteration(state)
        assert not out.accepted and out.reason.startswith(controller.WEAKER_ROUTE_HOLD), (name, out.reason)


def test_a_hold_on_a_weaker_route_is_bounded_by_the_runs_own_budget_and_quiet_afterwards(
        tmp_path, spec):
    """Termination (spec section 4): each hold is one rejected iteration charged before the next
    starts (`loops_without_progress`, persisted by the rejection); at the budget the run is
    BLOCKED with the budget's own reason and a further call writes and dispatches nothing. Kills
    Q34 (a hold is not charged: the loop never ends)."""
    ctrl, state, store, d = _drive(tmp_path, spec, _asked(min_discriminating=2))
    for n in range(1, 4):
        out = ctrl.run_iteration(state)
        assert not out.accepted and out.reason.startswith(controller.WEAKER_ROUTE_HOLD), (n, out.reason)
        assert RunStore(tmp_path / "runs", "r1").read_state().usage.loops_without_progress == n
    assert state.budget_exhausted()
    calls = len(d.calls)
    out = ctrl.run_iteration(state)                       # the bound: a block, no dispatch
    assert not out.accepted and "loops without traceable progress" in out.reason
    assert state.condition is Condition.BLOCKED and len(d.calls) == calls
    reason = RunStore(tmp_path / "runs", "r1").read_state().blocked_reason
    again = ctrl.run_iteration(state)                     # terminal: the same block, no dispatch
    assert not again.accepted and len(d.calls) == calls
    kept = RunStore(tmp_path / "runs", "r1").read_state()
    assert kept.condition is Condition.BLOCKED and kept.blocked_reason == reason


# --------------------------------------------------------------------------- #
# S-E -- a park failover is evidence for the routes it passed over
# --------------------------------------------------------------------------- #

def _park_record(tmp_path, policy, name: str = "r1", *, hours: int = 3) -> Path:
    run = _fresh_run(tmp_path, name)
    routing.record_park_failover(policy, run_dir=run, receipt=_park_receipt(
        "qa", "claude", "opus", "a" * 32, hours=hours), now=AT, reader=_reader(
            _limited("claude", "a", hours)))
    return run


def test_a_park_failover_is_evidence_until_the_reset_it_passed_over_and_not_after(tmp_path):
    """Kills Q7 again at the boundary, Q35 (the run's own record is evidence against itself),
    Q36 (a passed-over route without a stated reset breaks the reading), Q36b (no settling
    window after the reset), Q36c (the settling window lasts one tick too long)."""
    policy = routing.parse(_policy_doc(CODEX))
    one = _park_record(tmp_path, policy)
    until = AT + 3 * HOUR
    dwell = timedelta(seconds=routing.RETURN_DWELL_SECONDS)
    moments = (("limited", until - timedelta(seconds=1)), ("reset", until),
               ("settling", until + dwell - timedelta(seconds=1)), ("over", until + dwell))
    outcomes = {}
    for label, now in moments:
        fresh = _fresh_run(tmp_path, f"second-{label}")   # a run decides once: one per moment
        plan = routing.plan_failover(policy, run_dir=fresh, profiles={
            Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "claude"},
            now=now, reader=_reader())
        try:
            decision = plan.resolved.evidence["failover"]["decisions"]["qa"]
            outcomes[label] = (decision["outcome"], decision["reason"])
        finally:
            plan.close()
    assert outcomes == {"limited": ("FALLBACK", "LIMITED"), "reset": ("FALLBACK", "SETTLING"),
                        "settling": ("FALLBACK", "SETTLING"), "over": ("DECLARED", "NO_EVIDENCE")}
    # the run that holds the record is not read as evidence about itself
    assert routing._park_evidence(one, AT, frozenset({"r1"})) == ()
    (group,) = routing._park_evidence(one, AT, frozenset())
    assert (group.provider, group.account_handle, group.state.value) == (
        "claude", "NOT_AVAILABLE", "LIMITED")
    assert group.not_before == until.isoformat() and group.runs == ("r1",)
    # the route's own clock, not the run's return: still out until the reset plus the dwell
    assert [g.state.value for g in routing._park_evidence(one, until, frozenset())] == ["RESET_DUE"]
    assert routing._park_evidence(one, until + dwell, frozenset()) == ()
    # a passed-over route with no stated reset is no evidence (nothing bounds it)
    good = (one / PARK1).read_bytes()
    doc = json.loads(good)
    doc["decisions"]["qa"]["considered"][0]["not_before"] = None
    (one / PARK1).write_bytes(_resign(doc))
    assert routing._park_evidence(one, AT, frozenset()) == ()
    # a reset further out than a provider's limit can be is read as that far (a forged year 2126)
    doc = json.loads(good)
    doc["decisions"]["qa"]["considered"][0]["not_before"] = datetime(2126, 1, 1, tzinfo=timezone.utc).isoformat()
    (one / PARK1).write_bytes(_resign(doc))
    ceiling = AT + quota.MAX_STATED_DATE_WAIT
    (group,) = routing._park_evidence(one, ceiling - timedelta(seconds=1), frozenset())
    assert group.not_before == ceiling.isoformat() and group.state.value == "LIMITED"
    assert [g.state.value for g in routing._park_evidence(one, ceiling, frozenset())] == ["RESET_DUE"]
    assert routing._park_evidence(one, ceiling + dwell, frozenset()) == ()
    (one / PARK1).write_bytes(good)


def test_a_park_failover_evidence_group_names_the_account_of_the_route_it_passed_over(tmp_path):
    """Kills Q46 (the evidence drops the account: an account-b park would be read as a limit of
    an account nobody named, or as one of every account)."""
    policy = routing.parse(_policy_doc(ACCOUNT_B, {**ACCOUNT_B, "account": "acct-c"}, CODEX))
    run = _fresh_run(tmp_path)
    routing.record_park_failover(
        policy, run_dir=run, receipt=_park_receipt("qa", "claude", "opus", "d" * 32), now=AT,
        reader=_reader(_limited("claude", "acct-a"), _limited("claude", "acct-b"),
                       _limited("claude", "acct-c")))
    handles = {(g.provider, g.account_handle)
               for g in routing._park_evidence(_fresh_run(tmp_path, "r2"), AT, frozenset())}
    assert handles == {("claude", "NOT_AVAILABLE"), ("claude", "acct-b"), ("claude", "acct-c")}


def test_a_park_failover_record_that_is_not_its_own_or_not_its_runs_policys_is_no_evidence_and_no_reading(
        tmp_path):
    """Kills Q37 (a damaged record of another run is skipped: the reading is called complete and
    a route is chosen on what is left), Q38 (a record made under another policy than the one its
    run is pinned to counts), Q44 (a run directory that cannot be opened is skipped)."""
    policy = routing.parse(_policy_doc(CODEX))
    one = _park_record(tmp_path, policy)
    two = _fresh_run(tmp_path, "r2")

    def refused(why: str):
        plan = _plan(policy, two)
        try:
            assert plan.stays and why in plan.stayed, plan.stayed
            assert not (two / START).exists()          # nothing was decided, nothing recorded
        finally:
            plan.close()
        with pytest.raises(RoutingRefused, match=why):
            _plan(policy, two, strict=True).close()

    good = (one / PARK1).read_bytes()
    doc = json.loads(good)
    (one / PARK1).write_bytes((json.dumps(dict(doc, number=2), sort_keys=True) + "\n").encode())
    refused("a park failover record is not what this module writes")
    (one / PARK1).write_bytes(good[:-3])                  # not JSON any more
    refused("the failover record is not one UTF-8 JSON document")
    (one / PARK1).write_bytes(good)
    pin = one / "routing.json"                            # a pin that names another policy
    pin.write_text(json.dumps({"schema": "hoh-routing-pin/1", "digest": "f" * 64}))
    refused("not made under the policy that run is pinned to")
    pin.write_text(json.dumps({"schema": "hoh-routing-pin/1", "digest": doc["policy_digest"]}))
    plan = _plan(policy, two)                             # the pin agrees: the evidence counts
    try:
        assert plan.decided and plan.resolved.profiles[Role.QA] == "codex"
    finally:
        plan.close()
    # a link, a FIFO, a file nobody may read, another encoding, a NUL, a lone surrogate, a JSON
    # that is no object, an oversize file in the place of a record: a refusal, never an absence
    for kind in ("link", "dangling link", "fifo", "directory", "mode 000", "latin-1", "nul",
                 "surrogate", "array", "oversize"):
        root = tmp_path / kind.replace(" ", "-")
        planted = _fresh_run(root, "r1")
        target = planted / "routing-failover-park-1.json"
        if kind == "link":
            target.symlink_to(one / PARK1)
        elif kind == "dangling link":
            target.symlink_to(root / "nowhere.json")
        elif kind == "fifo":
            os.mkfifo(target)
        elif kind == "directory":
            target.mkdir()
        else:
            target.write_bytes({
                "mode 000": good, "latin-1": b'{"schema": "\xe9"}', "nul": b'{"schema":\x00}',
                "surrogate": b'{"schema": "\\ud800"}', "array": b"[]",
                "oversize": b" " * (routing.MAX_FAILOVER_BYTES + 1)}[kind])
            if kind == "mode 000":
                target.chmod(0)
        with pytest.raises(RoutingRefused):
            routing._park_evidence(_fresh_run(root, "r2"), AT, frozenset())
    # a run directory nobody may open or list is an incomplete reading, not a run without records
    for mode in (0o000, 0o300):
        root = tmp_path / f"dir-{mode:o}"
        locked = _fresh_run(root, "r1")
        locked.chmod(mode)
        try:
            with pytest.raises(RoutingRefused, match="run directory under the runs root"):
                routing._park_evidence(_fresh_run(root, "r2"), AT, frozenset())
        finally:
            locked.chmod(0o700)


def test_failover_summary_shows_the_park_records_alone_and_refuses_a_record_that_is_not_its_own(
        tmp_path):
    """Kills Q19 (a run with park records and no start record shows nothing), Q40 (the summary
    reads a record that is not its own)."""
    policy = routing.parse(_policy_doc(CODEX))
    one = _park_record(tmp_path, policy)
    (shown,) = routing.failover_summary(one)["park_failovers"]
    assert shown["file"] == PARK1 and shown["role"] == "qa" and shown["route"] == CODEX
    assert shown["failure_class"] == "USAGE_LIMIT" and shown["trust_class"] == "weaker"
    assert "roles" not in routing.failover_summary(one)             # no start record
    assert routing.failover_summary(_fresh_run(tmp_path, "empty")) is None
    raw = (one / PARK1).read_bytes()
    (one / PARK1).write_bytes(raw.replace(b'"number": 1', b'"number": 2'))
    assert "unreadable" in routing.failover_summary(one)
