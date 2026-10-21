"""P3-19 slice 1: the resource scheduler -- one pure, fail-closed admission of ready nodes.

Every test names what it catches. The limits are asserted with literal numbers (64 candidates, 768 holds, 10**9
for a share, 65 and 769 for the items that may be pulled), not with the module's constants, so a changed constant
fails a test instead of moving it. The scheduler reads no file and no clock; the tests that need a registry or a
ledger on disk write only under `tmp_path`. Nothing here reads the host's `/tmp`, the home directory or a state
root, starts Herdr or an agent, or touches the network. An endless iterable runs under an alarm so that a
scheduler that consumes it fails the test instead of hanging it.
"""
from __future__ import annotations

import ast
import collections
import contextlib
import dataclasses
import inspect
import itertools
import json
import random
import signal
from datetime import datetime, timedelta
from pathlib import Path

import pytest


def _scheduler():
    """`hoh.scheduler`, imported inside each test: a tree without the module fails each test with its own
    ModuleNotFoundError (exit 1), not with one collection error (exit 4) that hides the others."""
    import importlib
    return importlib.import_module("hoh.scheduler")


def _broker():
    import importlib
    return importlib.import_module("hoh.broker")


def _capacity():
    import importlib
    return importlib.import_module("hoh.capacity")


NOW = "2026-10-12T09:00:00Z"
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def ago(seconds: int, now: str = NOW) -> str:
    return (datetime.strptime(now, _FMT) - timedelta(seconds=seconds)).strftime(_FMT)


def entry(kind="machine", name="host.sessions", limit=8, unit="sessions", source="measured", as_of=NOW):
    return _capacity().Entry(kind=kind, name=name, limit=limit, unit=unit, source=source, as_of=as_of)


def registry(*entries):
    """A registry exactly as the reader produces it: parsed from the canonical bytes of the entries."""
    cap = _capacity()
    return cap.parse(cap.render(entries))


def amount(kind="machine", name="host.sessions", n=1, unit="sessions"):
    return _broker().Amount(kind=kind, name=name, amount=n, unit=unit)


def policy(default=3600):
    table = {(k, s): default for k in ("dependency", "machine", "provider") for s in ("declared", "measured")}
    return _broker().Policy(max_age=table)


def candidate(node, *wants, budget=1):
    return _scheduler().Candidate(node, tuple(wants), budget)


def plan(reg, candidates, holds=(), ceiling=100, left=100, pol=None, now=NOW):
    return _scheduler().admit(reg, pol or policy(), now, candidates, holds, ceiling, left)


def verdicts(result):
    return [(p.node, p.verdict, p.reason) for p in result.placements]


def refused(call, *args, **kw) -> str:
    """The `reason` of the `SchedulerError` the call raises; fails if it raises anything else or nothing."""
    with pytest.raises(_scheduler().SchedulerError) as caught:
        call(*args, **kw)
    assert len(str(caught.value)) < 200                    # a message never carries an unbounded value
    return caught.value.reason


@contextlib.contextmanager
def alarm(seconds: int = 10):
    def fire(signum, frame):
        raise AssertionError(f"blocked for {seconds}s")
    old = signal.signal(signal.SIGALRM, fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


class Str(str):
    """A str subclass: a spelling that compares equal to a good one and must still be refused."""


def test_the_vocabulary_is_closed_and_the_values_are_frozen():
    """Catches a fourth verdict or an undocumented reason, a reason of the broker the scheduler cannot pass
    through, a limit that moved (768 holds is the broker's 1024 less the 256 claims the scheduler keeps), an
    error that is not a `ValueError`, and a mutable `Candidate`, `Placement` or `Plan`."""
    s, b, cap = _scheduler(), _broker(), _capacity()
    assert s.VERDICTS == ("admit", "wait", "refuse")
    assert s.REASONS == ("ok", "exhausted", "exceeds", "stale", "future", "unknown", "unit", "budget_exhausted",
                         "budget_exceeds", "behind")
    assert set(b.REASONS) <= set(s.REASONS)
    assert (s.MAX_CANDIDATES, s.MAX_HOLDS, s.MAX_BUDGET) == (64, 768, 10 ** 9)
    assert (b.MAX_HOLDS, cap.MAX_ENTRIES, b.MAX_WANTS) == (1024, 256, 64)
    assert issubclass(s.SchedulerError, ValueError)
    assert [f.name for f in dataclasses.fields(s.Candidate)] == ["node", "wants", "budget"]
    assert [f.name for f in dataclasses.fields(s.Placement)] == ["node", "verdict", "reason", "budget", "decision"]
    assert [f.name for f in dataclasses.fields(s.Plan)] == [
        "registry", "now", "budget_ceiling", "budget_left", "budget_after", "placements"]
    result = plan(registry(entry()), [candidate("a", amount())])
    for frozen, field in ((candidate("a", amount()), "budget"), (result.placements[0], "verdict"),
                          (result, "budget_after")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(frozen, field, 7)
    for placement in result.placements:
        assert placement.verdict in s.VERDICTS and placement.reason in s.REASONS


def test_a_candidate_is_exactly_a_valid_node_a_tuple_of_amounts_and_a_share():
    """Catches a node id that is not the stated grammar (a trailing newline, a space, a slash, another script,
    a NUL, 129 characters, a `str` subclass, and the names the runs root keeps: `.`, `..`, `_arenas`,
    `.timers.lock`, `backups`, `projects`), wants that are a list, empty, 65 long, duplicated, not `Amount`
    or a subclass, and a share that is not a whole number from 1 to 10**9 (a bool, a float, a 5000-digit one)."""
    s = _scheduler()
    one = (amount(),)
    for node in ("a", "node-1", "A.b_c-d", "p3-19", "0", "x" * 128, "backups2", "my-projects"):
        assert s.Candidate(node, one, 1).node == node
    for node in ("", "a\n", "a b", "a/b", "../x", "a/../b", "\u00e9", "\u0661", "a\u0000", "x" * 129, " a", ".",
                 "..", ".a", "_a", "_arenas", ".timers.lock", "-a", "backups", "projects", Str("a"), None, 5,
                 b"a", ["a"]):
        assert refused(s.Candidate, node, one, 1) == "invalid", repr(node)[:40]
    import importlib
    store = importlib.import_module("hoh.store")            # the drift guard: what the runs root keeps stays refused
    for prefix in store.RESERVED_PREFIXES:
        assert refused(s.Candidate, prefix + "run", one, 1) == "invalid", prefix
    for kept in store.RESERVED_RUN_IDS:
        assert refused(s.Candidate, kept, one, 1) == "invalid", kept

    class SubAmount(_broker().Amount):
        pass
    wants64 = tuple(amount(name=f"host.s{i}") for i in range(64))
    assert len(s.Candidate("a", wants64, 1).wants) == 64
    for wants in ([amount()], (), tuple(amount(name=f"host.s{i}") for i in range(65)), (amount(), amount()),
                  (amount(), amount(n=2)), ("machine",), (entry(),), (None,), (SubAmount("machine", "host.sessions",
                                                                                         1, "sessions"),),
                  None, "x", amount(), {amount()}):
        assert refused(s.Candidate, "a", wants, 1) == "invalid", repr(wants)[:60]
    assert s.Candidate("a", one, 1).budget == 1 and s.Candidate("a", one, 10 ** 9).budget == 10 ** 9
    for share in (0, -1, True, False, 1.0, "1", None, 10 ** 9 + 1, 10 ** 5000, [1]):
        assert refused(s.Candidate, "a", one, share) == "invalid", repr(share)[:40]


def test_the_budget_ceiling_and_the_figure_left_are_whole_numbers_in_range():
    """Catches a ceiling or a figure left that is a bool, a float, a string, `None` or out of range, a figure
    above the ceiling (the ledger cannot say that), a ceiling of 0 that is an error instead of a refusal of
    every share (everything spent), and -10**9 that is read as "no budget" instead of "nothing is admitted"."""
    reg = registry(entry())
    cands = [candidate("a", amount(), budget=1)]

    def run(ceiling, left):
        return plan(reg, cands, ceiling=ceiling, left=left)
    for ceiling in (-1, True, False, 1.0, "5", None, 10 ** 9 + 1, [5]):
        assert refused(run, ceiling, 0) == "invalid", repr(ceiling)
    for left in (6, -10 ** 9 - 1, True, False, 1.0, "1", None, [1]):
        assert refused(run, 5, left) == "invalid", repr(left)
    assert refused(run, 0, 1) == "invalid"                      # one over a ceiling of 0
    assert refused(run, 10 ** 9, 10 ** 9 + 1) == "invalid"
    top = run(10 ** 9, 10 ** 9)
    assert verdicts(top) == [("a", "admit", "ok")] and top.budget_after == 10 ** 9 - 1
    equal = run(5, 5)
    assert verdicts(equal) == [("a", "admit", "ok")] and equal.budget_after == 4
    nothing = run(0, 0)                                          # everything spent: every share is refused
    assert verdicts(nothing) == [("a", "refuse", "budget_exceeds")]
    assert nothing.admitted == () and nothing.budget_after == 0
    nothing = run(0, -4)
    assert verdicts(nothing) == [("a", "refuse", "budget_exceeds")] and nothing.budget_after == -4
    bottom = run(5, -10 ** 9)                                    # not "no budget": admits nothing, says so
    assert verdicts(bottom) == [("a", "wait", "budget_exhausted")]
    assert bottom.admitted == () and bottom.budget_after == -10 ** 9 and bottom.budget_left == -10 ** 9
    assert (bottom.budget_ceiling, top.budget_ceiling) == (5, 10 ** 9)


def test_two_candidates_that_each_fit_alone_are_not_both_admitted_beyond_the_capacity():
    """Catches the double spend of capacity within one call: each candidate fits beside the holds alone, the
    broker says `grant` to both, and only the claims of the earlier admissions make the third wait. The claim
    shows in the decision's `in_use`, claims of one capacity are summed, and a provider claim is not a host
    session."""
    reg = registry(entry(limit=3), entry(kind="provider", name="anthropic", limit=5, source="declared"))
    result = plan(reg, [candidate(n, amount()) for n in ("c1", "c2", "c3")], holds=[amount(n=1)])
    assert verdicts(result) == [("c1", "admit", "ok"), ("c2", "admit", "ok"), ("c3", "wait", "exhausted")]
    assert [p.decision.lines[0].in_use for p in result.placements] == [1, 2, 3]
    assert result.admitted == ("c1", "c2")
    again = plan(reg, [candidate("a", amount(n=2)), candidate("b", amount(n=1)), candidate("c", amount(n=1))])
    assert verdicts(again) == [("a", "admit", "ok"), ("b", "admit", "ok"), ("c", "wait", "exhausted")]
    assert [p.decision.lines[0].in_use for p in again.placements] == [0, 2, 3]       # 2 + 1 fills 3 exactly
    apart = plan(reg, [candidate("p", amount(kind="provider", name="anthropic", n=3)),
                       candidate("h", amount(n=3)),
                       candidate("q", amount(kind="provider", name="anthropic", n=3))])
    assert verdicts(apart) == [("p", "admit", "ok"), ("h", "admit", "ok"), ("q", "wait", "exhausted")]
    assert [p.decision.lines[0].in_use for p in apart.placements] == [0, 0, 3]


def test_the_shares_are_taken_from_the_budget_left_and_the_edge_is_exact():
    """Catches shares that are not taken from the budget (everything fits for ever), an edge that is off by
    one (a share equal to what is left waits), a share above what could ever be granted that waits for ever
    instead of being refused (and stalls the list), and a wrong `budget_after`."""
    reg = registry(entry(limit=50))

    def run(shares, ceiling, left):
        return plan(reg, [candidate(f"n{i}", amount(), budget=share) for i, share in enumerate(shares)],
                    ceiling=ceiling, left=left)
    three = run([2, 2, 2], 5, 5)
    assert verdicts(three) == [("n0", "admit", "ok"), ("n1", "admit", "ok"), ("n2", "wait", "budget_exhausted")]
    assert three.budget_after == 1 and three.admitted == ("n0", "n1")
    edge = run([5], 5, 5)
    assert verdicts(edge) == [("n0", "admit", "ok")] and edge.budget_after == 0
    over = run([5, 1], 5, 5)
    assert verdicts(over) == [("n0", "admit", "ok"), ("n1", "wait", "budget_exhausted")] and over.budget_after == 0
    unreachable = run([5, 2], 4, 4)                            # 5 can never be granted: refused, the 2 still goes
    assert verdicts(unreachable) == [("n0", "refuse", "budget_exceeds"), ("n1", "admit", "ok")]
    assert unreachable.budget_after == 2
    net = run([2, 1], 1, 1)                                    # a ceiling of 10 less 9 spent, as the caller passes it
    assert verdicts(net) == [("n0", "refuse", "budget_exceeds"), ("n1", "admit", "ok")] and net.budget_after == 0
    tight = run([3, 2], 5, 3)                                  # the first share is the whole of what is left
    assert verdicts(tight) == [("n0", "admit", "ok"), ("n1", "wait", "budget_exhausted")]
    assert tight.budget_after == 0
    ceiling = run([6, 6], 5, 5)
    assert verdicts(ceiling) == [("n0", "refuse", "budget_exceeds"), ("n1", "refuse", "budget_exceeds")]
    assert ceiling.admitted == () and ceiling.budget_after == 5


def test_the_order_is_kept_a_waiting_candidate_holds_its_place_and_a_refused_one_does_not():
    """Catches overtaking (a candidate that fits is admitted past a waiter), a refused head that stops the
    list, a list that is sorted, a budget wait that does not hold its place, and a `behind` that is reported
    as the capacity being full when it is not."""
    reg = registry(entry(limit=2), entry(kind="provider", name="anthropic", limit=5, source="declared"))
    behind = plan(reg, [candidate("c1", amount(n=2)), candidate("c2", amount(n=1)),
                        candidate("c3", amount(kind="provider", name="anthropic", n=1))])
    assert verdicts(behind) == [("c1", "admit", "ok"), ("c2", "wait", "exhausted"), ("c3", "wait", "behind")]
    assert (behind.placements[2].decision.verdict, behind.placements[2].decision.reason) == ("grant", "ok")
    assert behind.admitted == ("c1",)
    head = plan(reg, [candidate("r1", amount(kind="provider", name="nobody")), candidate("r2", amount(n=1))])
    assert verdicts(head) == [("r1", "refuse", "unknown"), ("r2", "admit", "ok")]
    unsorted = plan(reg, [candidate("z", amount(n=0)), candidate("m", amount(n=0)), candidate("a", amount(n=0))])
    assert [p.node for p in unsorted.placements] == ["z", "m", "a"] and unsorted.admitted == ("z", "m", "a")
    budget = plan(reg, [candidate("b1", amount(n=0), budget=2), candidate("b2", amount(n=0), budget=2),
                        candidate("b3", amount(n=0), budget=1)], ceiling=10, left=3)
    assert verdicts(budget) == [("b1", "admit", "ok"), ("b2", "wait", "budget_exhausted"), ("b3", "wait", "behind")]
    assert budget.admitted == ("b1",) and budget.budget_after == 1
    after = plan(reg, [candidate("w", amount(n=3)), candidate("x", amount(kind="provider", name="nobody")),
                       candidate("y", amount(n=0))])
    assert verdicts(after) == [("w", "refuse", "exceeds"), ("x", "refuse", "unknown"), ("y", "admit", "ok")]


def test_each_reason_is_the_brokers_for_the_wants_or_the_budgets_and_the_worse_one_wins():
    """Catches a broker reason that is lost or renamed on the way through, a capacity refusal that a budget
    refusal overwrites (the first line's reason is lost), an over-ceiling share that is only a wait, and a short
    budget that hides that the capacity itself is full."""
    reg = registry(entry(limit=2),
                   entry(kind="provider", name="old", limit=5, source="declared", as_of=ago(7200)),
                   entry(kind="provider", name="later", limit=5, source="declared", as_of=ago(-60)))

    def one(wants, holds=(), budget=1, ceiling=10, left=10):
        result = plan(reg, [candidate("a", *wants, budget=budget)], holds=holds, ceiling=ceiling, left=left)
        return result.placements[0].verdict, result.placements[0].reason
    assert one([amount(n=1)]) == ("admit", "ok")
    assert one([amount(n=3)]) == ("refuse", "exceeds")
    assert one([amount(kind="provider", name="old")]) == ("refuse", "stale")
    assert one([amount(kind="provider", name="later")]) == ("refuse", "future")
    assert one([amount(kind="provider", name="nobody")]) == ("refuse", "unknown")
    assert one([amount(unit="tokens")]) == ("refuse", "unit")
    assert one([amount(n=1)], holds=[amount(n=2)]) == ("wait", "exhausted")
    assert one([amount(kind="provider", name="nobody")], budget=11) == ("refuse", "unknown")      # capacity first
    assert one([amount(n=3)], budget=11) == ("refuse", "exceeds")
    assert one([amount(n=1)], holds=[amount(n=2)], budget=11) == ("refuse", "budget_exceeds")     # wait + too big
    assert one([amount(n=1)], holds=[amount(n=2)], budget=4, left=3) == ("wait", "exhausted")     # names capacity
    assert one([amount(n=1)], budget=11) == ("refuse", "budget_exceeds")
    assert one([amount(n=1)], budget=4, left=3) == ("wait", "budget_exhausted")
    assert one([amount(n=1)], budget=3, left=3) == ("admit", "ok")


def test_missing_or_malformed_input_is_refused_and_never_read_as_nothing_held_or_no_budget():
    """Catches `holds` or the budget defaulting to nothing (an absent fact is never zero), a `now` that is not a
    canonical time, 65 candidates or 769 holds, an endless iterable consumed past 65 (769) items, a subclass or
    a forged object (made without its constructor, or with a list for a kind) that raises `AttributeError`, an
    unbounded class name in a message, the scheduler's own claims breaking the broker's bound of 1024 holds, and
    a `SchedulerError` where the caller's own iterable failed (that exception is theirs, unchanged)."""
    s, b = _scheduler(), _broker()
    reg = registry(entry())
    good = [candidate("a", amount())]
    params = inspect.signature(s.admit).parameters
    assert list(params) == ["registry", "policy", "now", "candidates", "holds", "budget_ceiling", "budget_left"]
    assert all(p.default is inspect.Parameter.empty for p in params.values())              # no default anywhere
    with pytest.raises(TypeError):
        s.admit(reg, policy(), NOW, good, [], 5)
    with pytest.raises(TypeError):
        s.admit(reg, policy(), NOW, good, 5, 5)

    ordinary = policy()

    def call(candidates=good, holds=(), registry_=reg, policy_=ordinary, now=NOW):
        return s.admit(registry_, policy_, now, candidates, holds, 100, 100)
    # candidates: 1 to 64, exactly Candidate, unique
    assert refused(call, []) == "invalid"
    wide = [candidate(f"n{i}", amount(n=0)) for i in range(65)]
    assert refused(call, wide) == "invalid"
    assert len(call(wide[:64]).admitted) == 64
    assert refused(call, [candidate("a", amount()), candidate("a", amount())]) == "invalid"
    for odd in ("abc", b"abc", {"a": good[0]}, [object()], [amount()], [None], [("a", (amount(),), 1)], good[0], 7,
                None, [entry()]):
        assert refused(call, odd) == "invalid", repr(odd)[:40]
    # endless iterables are refused after 65 (769) items, not consumed
    def endless(item, pulled):
        while True:
            pulled.append(1)
            yield item
    with alarm():
        assert refused(call, itertools.repeat(good[0])) == "invalid"
        assert refused(call, good, itertools.repeat(amount(n=0))) == "invalid"
    pulled = []
    assert refused(call, endless(candidate("a", amount()), pulled)) == "invalid"
    assert 0 < len(pulled) <= 65, len(pulled)
    pulled = []
    assert refused(call, good, endless(amount(n=0), pulled)) == "invalid"
    assert 0 < len(pulled) <= 769, len(pulled)
    # holds: 0 to 768, exactly Amount, never `None`
    assert call(good, []).admitted == ("a",)
    assert call(good, [amount(n=0)] * 768).admitted == ("a",)
    assert refused(call, good, [amount(n=0)] * 769) == "invalid"
    for odd in (None, 5, "abc", {"a": amount()}, [None], [entry()], [("machine", "host.sessions", 1, "sessions")],
                [good[0]]):
        assert refused(call, good, odd) == "invalid", repr(odd)[:40]

    class SubAmount(b.Amount):
        pass

    class SubCandidate(s.Candidate):
        pass
    sub = SubAmount("machine", "host.sessions", 1, "sessions")
    assert refused(call, good, [sub]) == "invalid"
    assert refused(call, [SubCandidate("a", (amount(),), 1)]) == "invalid"
    assert refused(s.Candidate, "a", (sub,), 1) == "invalid"
    # a frozen value edited in place is judged again
    edited = candidate("a", amount())
    object.__setattr__(edited, "budget", 0)
    assert refused(call, [edited]) == "invalid"
    edited = candidate("a", amount())
    object.__setattr__(edited, "wants", [amount()])
    assert refused(call, [edited]) == "invalid"
    edited = candidate("a", amount())
    object.__setattr__(edited, "node", "../a")
    assert refused(call, [edited]) == "invalid"
    for field, value in (("amount", -5), ("amount", 10 ** 15 + 1), ("amount", True), ("name", "Host\n"),
                         ("kind", "storage"), ("unit", "session")):
        want = amount()
        carrier = candidate("a", want)
        object.__setattr__(want, field, value)
        assert refused(call, [carrier]) == "invalid", (field, value)
        hold = amount()
        object.__setattr__(hold, field, value)
        assert refused(call, good, [hold]) == "invalid", (field, value)
    # forged objects: no constructor ran, or a field that is not even a string
    assert refused(call, [object.__new__(s.Candidate)]) == "invalid"
    forged = object.__new__(b.Amount)
    assert refused(call, good, [forged]) == "invalid"
    assert refused(s.Candidate, "a", (forged,), 1) == "invalid"
    carrier = candidate("a", amount())
    object.__setattr__(carrier, "wants", (forged,))
    assert refused(call, [carrier]) == "invalid"
    listed = amount()
    object.__setattr__(listed, "kind", ["machine"])
    assert refused(call, good, [listed]) == "invalid"
    carrier = candidate("a", amount())
    object.__setattr__(carrier, "wants", (listed,))
    assert refused(call, [carrier]) == "invalid"
    assert refused(call, [listed]) == "invalid"                                 # direct, not in a candidate
    # the registry, the policy and the time are the broker's to judge, with the broker's reason
    assert refused(call, registry_=dataclasses.replace(reg, digest="0" * 64)) == "registry"
    for odd in (None, {}, "registry", reg.entries, object.__new__(type(reg))):
        assert refused(call, registry_=odd) == "registry", repr(odd)[:40]
    ages = {(k, x): 60 for k in ("dependency", "machine", "provider") for x in ("declared", "measured")}

    class SubPolicy(b.Policy):
        pass
    for odd in (None, {"machine": 60}, SubPolicy(ages), object.__new__(b.Policy)):
        assert refused(call, policy_=odd) == "invalid", repr(odd)[:40]
    for now in ("2026-10-12T09:00:00", "2026-10-12 09:00:00Z", "2026-10-12T09:00:00z", "2026-10-12T09:00:00Z\n",
                "2026-13-01T00:00:00Z", "", None, 1760259600, Str(NOW)):
        assert refused(call, now=now) == "invalid", repr(now)
    # a hundred thousand characters of class name never reach a message
    Big = type("B" * 100000, (), {})
    assert refused(call, [Big()]) == "invalid"
    assert refused(call, good, [Big()]) == "invalid"
    assert refused(call, registry_=Big()) == "registry"
    assert refused(call, policy_=Big()) == "invalid"
    assert refused(call, Big()) == "invalid"
    assert refused(s.Candidate, "a", (Big(),), 1) == "invalid"
    # the claims are summed per capacity: 64 candidates x 5 wants beside 768 holds stay within the broker's 1024
    wide_reg = registry(*[entry(name=f"host.s{i}", limit=10 ** 6) for i in range(5)])
    many = [s.Candidate(f"n{i}", tuple(amount(name=f"host.s{k}") for k in range(5)), 1) for i in range(64)]
    full = s.admit(wide_reg, policy(), NOW, many, [amount(name="host.s0", n=1)] * 768, 100, 100)
    assert full.admitted == tuple(f"n{i}" for i in range(64))
    assert full.placements[-1].decision.lines[0].in_use == 768 + 63
    # what the caller's own iterable raises is theirs, unchanged
    def breaks(exc, item):
        yield item
        raise exc
    for exc in (ValueError("a"), KeyError("b"), OSError("c"), TypeError("d")):
        for position in (0, 1):
            with pytest.raises(type(exc)) as caught:
                if position == 0:
                    call(breaks(exc, candidate("a", amount())))
                else:
                    call(good, breaks(exc, amount(n=0)))
            assert caught.value is exc


def test_a_plan_is_a_plain_value_that_says_what_it_judged_and_judges_the_same_twice(tmp_path):
    """Catches an `as_dict` that changed a key or dropped a field (`now`, a budget figure, the digest, a line of
    a decision), a plan that quotes the wrong budget figure, a result that is not plain JSON, one that depends on
    the order of the holds, and one that changes what it was given."""
    cap = _capacity()
    path = tmp_path / "registry.json"
    cap.write(path, [entry(limit=4, as_of=ago(60)),
                     entry(kind="provider", name="anthropic", limit=2, source="declared", as_of=ago(30))])
    reg = cap.read(path)
    cands = [candidate("n1", amount(n=1), budget=2),
             candidate("n2", amount(kind="provider", name="nobody", n=1), budget=1),
             candidate("n3", amount(n=1), budget=3)]
    holds = [amount(n=1)]
    snapshot = (list(cands), list(holds), reg.digest)
    result = plan(reg, cands, holds, ceiling=9, left=4)
    assert result.as_dict() == {
        "registry": reg.digest, "now": NOW, "budget_ceiling": 9, "budget_left": 4, "budget_after": 2,
        "admitted": ["n1"],
        "placements": [
            {"node": "n1", "verdict": "admit", "reason": "ok", "budget": 2,
             "decision": {"verdict": "grant", "reason": "ok", "registry": reg.digest, "now": NOW, "lines": [
                 {"kind": "machine", "name": "host.sessions", "unit": "sessions", "wanted": 1, "limit": 4,
                  "in_use": 1, "age": 60, "verdict": "grant", "reason": "ok"}]}},
            {"node": "n2", "verdict": "refuse", "reason": "unknown", "budget": 1,
             "decision": {"verdict": "refuse", "reason": "unknown", "registry": reg.digest, "now": NOW, "lines": [
                 {"kind": "provider", "name": "nobody", "unit": "sessions", "wanted": 1, "limit": None,
                  "in_use": 0, "age": None, "verdict": "refuse", "reason": "unknown"}]}},
            {"node": "n3", "verdict": "wait", "reason": "budget_exhausted", "budget": 3,
             "decision": {"verdict": "grant", "reason": "ok", "registry": reg.digest, "now": NOW, "lines": [
                 {"kind": "machine", "name": "host.sessions", "unit": "sessions", "wanted": 1, "limit": 4,
                  "in_use": 2, "age": 60, "verdict": "grant", "reason": "ok"}]}},
        ]}
    assert result.now == NOW and result.registry == reg.digest
    assert (result.budget_ceiling, result.budget_left, result.budget_after) == (9, 4, 2)
    assert result.admitted == ("n1",)
    assert [(p.node, p.verdict, p.reason, p.budget) for p in result.placements] == [
        ("n1", "admit", "ok", 2), ("n2", "refuse", "unknown", 1), ("n3", "wait", "budget_exhausted", 3)]
    assert [p.decision.verdict for p in result.placements] == ["grant", "refuse", "grant"]
    assert [p.decision.registry for p in result.placements] == [reg.digest] * 3
    assert json.loads(json.dumps(result.as_dict(), sort_keys=True)) == result.as_dict()
    assert plan(reg, cands, holds, ceiling=9, left=4) == result
    assert (cands, holds, reg.digest) == snapshot
    assert plan(reg, cands, list(reversed(holds + [amount(n=0)])), ceiling=9, left=4) == plan(
        reg, cands, holds + [amount(n=0)], ceiling=9, left=4)
    roomy = [candidate("p", amount(n=0)), candidate("q", amount(n=0)), candidate("r", amount(n=0))]
    forward, backward = plan(reg, roomy), plan(reg, list(reversed(roomy)))
    assert [p.node for p in backward.placements] == [p.node for p in reversed(forward.placements)]
    assert backward.admitted == ("r", "q", "p") and forward.admitted == ("p", "q", "r")


def test_what_the_plan_admits_the_real_budget_ledger_grants_in_full_and_the_rest_it_does_not(tmp_path):
    """Catches a plan that disagrees with the ledger it advises: shares the plan admits that the ledger would
    not grant in full, a share above what could ever be granted that is not refused, the ledger's partial grant
    read as a refusal (only `left <= 0` raises), a negative figure that plans something, and `left` read as a
    figure of the ledger instead of one of a run id."""
    import importlib
    ledger_module = importlib.import_module("hoh.reservation")
    def spent():
        return {"r1": 1, "r2": 1}

    def nothing_spent():
        return {}

    def fresh(name):
        ledger = ledger_module.BudgetReservations(tmp_path / name)
        assert ledger.reserve("h1", ceiling=7, spent=spent, want=1).amount == 1      # one open hold of 1
        return ledger
    reg = registry(entry(limit=50))
    ledger = fresh("a")
    left = ledger.left("n1", ceiling=7, spent=spent)
    assert left == 4                                                              # 7 less 2 spent less 1 held
    net = 7 - 2                                                                   # what could ever be granted
    result = plan(reg, [candidate(f"n{i}", amount(), budget=2) for i in (1, 2, 3)], ceiling=net, left=left)
    assert verdicts(result) == [("n1", "admit", "ok"), ("n2", "admit", "ok"), ("n3", "wait", "budget_exhausted")]
    assert ledger.reserve("n1", ceiling=7, spent=spent, want=2).amount == 2
    assert ledger.reserve("n2", ceiling=7, spent=spent, want=2).amount == 2
    with pytest.raises(ledger_module.NothingToReserve):
        ledger.reserve("n3", ceiling=7, spent=spent, want=2)
    ledger = fresh("b")
    six = plan(reg, [candidate("big", amount(), budget=6)], ceiling=net, left=4)
    assert verdicts(six) == [("big", "refuse", "budget_exceeds")]
    five = plan(reg, [candidate("big", amount(), budget=5)], ceiling=net, left=4)
    assert verdicts(five) == [("big", "wait", "budget_exhausted")]
    assert ledger.reserve("big", ceiling=7, spent=spent, want=5).amount == 4        # the ledger grants min(want, left)
    holder = fresh("c")
    assert holder.left("h1", ceiling=7, spent=spent) == 5                         # the holder's own hold is left out
    assert holder.left("somebody-new", ceiling=7, spent=spent) == 4
    over = ledger_module.BudgetReservations(tmp_path / "d")
    assert over.reserve("x1", ceiling=20, spent=nothing_spent, want=6).amount == 6
    assert over.reserve("x2", ceiling=20, spent=nothing_spent, want=6).amount == 6
    negative = over.left("y1", ceiling=7, spent=nothing_spent)
    assert negative == -5
    nothing = plan(reg, [candidate("y1", amount(), budget=1)], ceiling=7, left=negative)
    assert verdicts(nothing) == [("y1", "wait", "budget_exhausted")] and nothing.budget_after == -5
    assert nothing.admitted == ()
    with pytest.raises(ledger_module.NothingToReserve):
        over.reserve("y1", ceiling=7, spent=nothing_spent, want=1)


def test_over_many_generated_requests_nothing_is_admitted_beyond_capacity_or_budget_or_past_a_waiter():
    """Catches an admission beyond a limit, beyond the budget, past a waiter or of what the broker refuses, over
    250 requests from one fixed seed. The expected answer is recomputed here, line by line, without the broker
    or the scheduler; the invariants are then checked on the plan itself."""
    s, b = _scheduler(), _broker()
    pool = (("machine", "host.sessions", "sessions"), ("machine", "host.bytes", "bytes"),
            ("provider", "anthropic", "sessions"), ("provider", "anthropic.tokens", "tokens"),
            ("dependency", "frontier", "items"))
    units = ("bytes", "items", "requests", "sessions", "tokens")
    rank = {"grant": 0, "wait": 1, "refuse": 2}
    rng = random.Random(20261013)
    seen: set = set()
    for case in range(250):
        entries, ages = [], {}
        for kind, name, unit in pool:
            if rng.random() < 0.12:
                continue                                                    # nobody recorded it: unknown
            age = rng.choice((3600, 3601, 7200, -45)) if rng.random() < 0.15 else rng.randint(0, 120)
            ages[(kind, name)] = age
            entries.append(entry(kind, name, limit=rng.randint(0, 6), unit=unit,
                                 source=rng.choice(("declared", "measured")), as_of=ago(age)))
        reg = registry(*entries)
        by_key = {(e.kind, e.name): e for e in entries}
        holds = []
        for _ in range(rng.randint(0, 4)):
            kind, name, unit = rng.choice(pool)
            holds.append(amount(kind, name, rng.randint(0, 3), unit if rng.random() > 0.05 else rng.choice(units)))
        cands = []
        for i in range(rng.randint(1, 6)):
            wants = tuple(amount(kind, name, rng.randint(0, 4), unit if rng.random() > 0.05 else rng.choice(units))
                          for kind, name, unit in rng.sample(pool, rng.randint(1, 3)))
            cands.append(s.Candidate(f"n{i}", wants, rng.randint(1, 4)))
        ceiling = rng.randint(0, 10)
        left = rng.randint(-3, ceiling)
        result = s.admit(reg, policy(), NOW, cands, holds, ceiling, left)

        # the expected answer, from the rules alone
        claims: dict = {}
        remaining, waiting = left, False
        for cand, placed in zip(cands, result.placements):
            lines = []
            for want in sorted(cand.wants, key=lambda w: (w.kind, w.name)):
                key = (want.kind, want.name)
                known = by_key.get(key)
                held_units = {h.unit for h in holds if (h.kind, h.name) == key}
                in_use = sum(h.amount for h in holds if (h.kind, h.name) == key) + claims.get(key, 0)
                if known is None:
                    lines.append(("refuse", "unknown"))
                elif want.unit != known.unit or held_units - {known.unit}:
                    lines.append(("refuse", "unit"))
                elif ages[key] < 0:
                    lines.append(("refuse", "future"))
                elif ages[key] > 3600:
                    lines.append(("refuse", "stale"))
                elif want.amount > known.limit:
                    lines.append(("refuse", "exceeds"))
                elif in_use + want.amount > known.limit:
                    lines.append(("wait", "exhausted"))
                else:
                    lines.append(("grant", "ok"))
            worst = lines[0]
            for line in lines[1:]:
                if rank[line[0]] > rank[worst[0]]:
                    worst = line
            assert (placed.decision.verdict, placed.decision.reason) == worst, (case, cand.node)
            if worst[0] == "refuse":
                expected = ("refuse", worst[1])
            elif cand.budget > ceiling:
                expected = ("refuse", "budget_exceeds")
            elif worst[0] == "wait":
                expected = ("wait", "exhausted")
            elif cand.budget > remaining:
                expected = ("wait", "budget_exhausted")
            elif waiting:
                expected = ("wait", "behind")
            else:
                expected = ("admit", "ok")
            assert (placed.verdict, placed.reason) == expected, (case, cand.node)
            seen.add(expected)
            if expected[0] == "wait":
                waiting = True
            if expected[0] == "admit":
                remaining -= cand.budget
                for want in cand.wants:
                    claims[(want.kind, want.name)] = claims.get((want.kind, want.name), 0) + want.amount

        # the invariants, on the plan itself
        assert [p.node for p in result.placements] == [c.node for c in cands]
        taken = [c for c, p in zip(cands, result.placements) if p.verdict == "admit"]
        assert result.admitted == tuple(c.node for c in taken)
        assert sum(c.budget for c in taken) <= max(left, 0)
        assert result.budget_after == left - sum(c.budget for c in taken)
        assert all(c.budget <= ceiling for c in taken)
        totals: dict = {}
        for hold in holds:
            totals[(hold.kind, hold.name)] = totals.get((hold.kind, hold.name), 0) + hold.amount
        for cand in taken:
            for want in cand.wants:
                key = (want.kind, want.name)
                known = by_key[key]
                assert 0 <= ages[key] <= 3600 and want.unit == known.unit
                totals[key] = totals.get(key, 0) + want.amount
        for key, total in totals.items():
            if key in by_key and any(key == (w.kind, w.name) for c in taken for w in c.wants):
                assert total <= by_key[key].limit, (case, key)
        flags = [p.verdict for p in result.placements]
        if "wait" in flags:
            assert "admit" not in flags[flags.index("wait"):], case
        for cand, placed in zip(cands, result.placements):
            if placed.verdict == "admit":
                assert b.decide(reg, policy(), NOW, cand.wants, []).verdict != "refuse", (case, cand.node)
    assert {("admit", "ok"), ("wait", "exhausted"), ("wait", "budget_exhausted"), ("wait", "behind"),
            ("refuse", "budget_exceeds"), ("refuse", "unknown"), ("refuse", "exceeds"), ("refuse", "stale"),
            ("refuse", "future"), ("refuse", "unit")} <= seen, sorted(seen)


def test_the_module_reads_no_clock_runs_nothing_writes_nothing_and_imports_only_the_broker_and_the_registry():
    """Catches a later edit that makes the scheduler impure: `datetime.now()`, a file, a process, the network,
    the environment, a private name of the registry or the broker. The modules it may import and the names it may
    take from `broker` and `capacity` are listed; the write-, read-, process-, environment- and clock-capable
    attribute names are forbidden anywhere."""
    tree = ast.parse(Path(_scheduler().__file__).read_text(encoding="utf-8"))
    imported, from_capacity, from_broker, from_package = set(), set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
            if node.level:
                assert node.module in (None, "broker", "capacity"), node.module
                names = {a.name for a in node.names}
                if node.module is None:
                    from_package |= names
                elif node.module == "broker":
                    from_broker |= names
                else:
                    from_capacity |= names
    assert imported <= {"__future__", "re", "collections.abc", "dataclasses", "itertools", ".", ".broker",
                        ".capacity"}, imported
    assert from_package <= {"broker"}, from_package
    assert from_broker <= {"Amount", "BrokerError", "Decision", "Policy", "decide"}, from_broker
    assert from_capacity <= {"MAX_ENTRIES", "Registry"}, from_capacity
    forbidden = {"write_bytes", "write_text", "read_bytes", "read_text", "open", "unlink", "rmdir", "remove",
                 "rename", "touch", "mkdir", "chmod", "system", "popen", "Popen", "environ", "getenv", "urlopen",
                 "connect", "fdopen", "makedirs", "truncate", "durable_write", "read_single_link", "read", "write",
                 "kill", "run", "call", "check_output", "spawn", "fork", "sleep", "shuffle", "random"}
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attributes & forbidden, attributes & forbidden
    clock = {"now", "utcnow", "today", "fromtimestamp", "monotonic", "time", "time_ns", "perf_counter"}
    for node in ast.walk(tree):                           # `self.now` is the field of a `Plan`, not a clock
        if isinstance(node, ast.Attribute) and node.attr in clock:
            assert isinstance(node.value, ast.Name) and node.value.id == "self", ast.dump(node)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "exec", "eval", "compile", "__import__", "input", "print"}


# ==== P3-19f: the repair slice (review findings of the integration pair on the imported candidate) ============

def test_an_iterable_that_fails_while_it_is_made_propagates_its_own_error_and_a_non_iterable_is_refused():
    """Catches an `__iter__` that raises `TypeError` (or anything) being turned into a `SchedulerError` with the
    wrong message ("not an iterable"): the exception of the caller's own object is theirs and propagates as the
    same object, for candidates and for holds. An object that is not iterable at all is still `invalid`, also
    one that sets `__iter__ = None`, and a class that is iterable only through `__getitem__` is taken."""
    s = _scheduler()
    reg, good = registry(entry()), [candidate("a", amount())]

    def call(candidates=good, holds=()):
        return s.admit(reg, policy(), NOW, candidates, holds, 100, 100)
    for exc in (TypeError("boom"), ValueError("v"), KeyError("k"), OSError("o")):
        class Breaks:
            def __iter__(self):
                raise exc
        for where in (0, 1):
            with pytest.raises(type(exc)) as caught:
                call(Breaks()) if where == 0 else call(good, Breaks())
            assert caught.value is exc, (type(exc), where)

    class NotIterable:
        __iter__ = None

    class Plain:
        pass
    for odd in (Plain(), NotIterable(), 7, None, 1.5):
        assert refused(call, odd) == "invalid", repr(odd)[:30]
        assert refused(call, good, odd) == "invalid", repr(odd)[:30]

    class ByIndex:                                         # the old sequence protocol is still an iterable
        def __getitem__(self, index):
            if index == 0:
                return good[0]
            raise IndexError(index)
    assert call(ByIndex()).admitted == ("a",)

    class NonIterator:                                     # `__iter__` returns something that is not an iterator
        def __iter__(self):
            return 5
    for where in (0, 1):
        with pytest.raises(TypeError) as caught:           # CPython's own error, the object's: not a SchedulerError
            call(NonIterator()) if where == 0 else call(good, NonIterator())
        assert not isinstance(caught.value, s.SchedulerError)
        assert "non-iterator" in str(caught.value)


def test_an_object_that_is_not_exactly_a_registry_or_a_policy_is_refused_before_any_attribute_is_read():
    """Catches a forged registry or policy whose attribute raises something that is not an `AttributeError` (a
    property that raises `RuntimeError`, a `__getattr__` that raises `KeyError`) escaping the scheduler raw: the
    type is judged first, as the broker does, so the refusal is a `SchedulerError` with the broker's reason."""
    s, b, cap = _scheduler(), _broker(), _capacity()
    reg = registry(entry())
    class Explodes:
        def __getattr__(self, name):
            raise KeyError(name)

    class SubRegistry(cap.Registry):
        @property
        def entries(self):
            raise RuntimeError("entries")

    class SubPolicy(b.Policy):
        @property
        def rows(self):
            raise RuntimeError("rows")
    forged_registry = object.__new__(SubRegistry)             # the property raises as soon as it is read
    forged_policy = object.__new__(SubPolicy)
    for odd in (Explodes(), forged_registry):
        assert refused(s.admit, odd, policy(), NOW, [candidate("a", amount())], (), 5, 5) == "registry"
    for odd in (Explodes(), forged_policy):
        assert refused(s.admit, reg, odd, NOW, [candidate("a", amount())], (), 5, 5) == "invalid"


def test_a_message_of_the_broker_is_cut_and_a_huge_class_name_of_the_time_never_reaches_it():
    """Catches the cut of the broker's message being dropped: `now` is the broker's to judge and its message names
    the class of a value that is not a time, which may be a hundred thousand characters long."""
    s = _scheduler()
    reg = registry(entry())
    Big = type("B" * 100000, (), {})
    with pytest.raises(s.SchedulerError) as caught:
        s.admit(reg, policy(), Big(), [candidate("a", amount())], (), 5, 5)
    assert caught.value.reason == "invalid"
    assert len(str(caught.value)) < 200, len(str(caught.value))


def test_an_empty_string_bytes_or_mapping_is_not_nothing_held_and_not_nothing_to_schedule():
    """Catches `holds=""`, `holds=b""`, `holds=bytearray()` or `holds={}` read as "no holds": an absent fact is
    never zero, and an empty tuple is the one way to say nothing is held. The same spellings are refused as
    candidates (they are not a list of nodes), also when they are not empty."""
    s = _scheduler()
    reg, good = registry(entry()), [candidate("a", amount())]
    assert s.admit(reg, policy(), NOW, good, (), 5, 5).admitted == ("a",)
    for empty in ("", b"", bytearray(), {}, frozenset(), set()):
        with pytest.raises(s.SchedulerError):
            s.admit(reg, policy(), NOW, good, empty, 5, 5)
        with pytest.raises(s.SchedulerError):
            s.admit(reg, policy(), NOW, empty, (), 5, 5)


def test_an_unordered_collection_is_not_an_order_of_candidates_and_not_a_list_of_holds():
    """Catches a `set` or `frozenset` of candidates admitted in hash order (the same set gave different plans
    under different hash seeds, against "the caller's order is the priority"), and a set of holds counting two
    equal holds once (two runs that each hold one session are a hold of two). Ordered containers are taken: a
    list, a tuple, a generator, the values of a dict."""
    s = _scheduler()
    reg = registry(entry(limit=2))
    a, b = candidate("a", amount()), candidate("b", amount())
    for unordered in (set([a, b]), frozenset([a, b]), {"a": a}.keys(), {"a": a}.items(),
                      collections.OrderedDict(a=a).keys()):      # views are refused by type, ordered or not
        with pytest.raises(s.SchedulerError):
            s.admit(reg, policy(), NOW, unordered, (), 5, 5)
    two = [amount(), amount()]                                  # two holds of one session each: full at limit 2
    assert s.admit(reg, policy(), NOW, [a], tuple(two), 5, 5).placements[0].verdict == "wait"
    for unordered in (set(two), frozenset(two), {"x": two[0]}.keys(), {"x": two[0]}.items()):
        with pytest.raises(s.SchedulerError):
            s.admit(reg, policy(), NOW, [a], unordered, 5, 5)
    for ordered in ([a, b], (a, b), iter([a, b]), (c for c in (a, b)), {"a": a, "b": b}.values()):
        assert s.admit(reg, policy(), NOW, ordered, (), 5, 5).admitted == ("a", "b")
