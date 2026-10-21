"""P3-17 slice 1: the capacity broker -- one pure decision over a capacity registry.

Every test names what it catches. The limits are asserted with literal numbers (64 wants, 1024 holds, 31622400
seconds of age, 10**15), not with the module's constants, so a changed constant fails a test instead of moving
it. The broker reads no file and no clock; tests that need a registry on disk write only under `tmp_path`.
Nothing here reads the host's `/tmp`, the home directory or a state root, starts Herdr or an agent, or touches
the network. An endless iterable runs under an alarm so that a broker that consumes it fails the test instead
of hanging it.
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import inspect
import itertools
import json
import signal
from datetime import datetime, timedelta
from pathlib import Path

import pytest


def _broker():
    """`hoh.broker`, imported inside each test: a tree without the module fails each test with its own
    ModuleNotFoundError (exit 1), not with one collection error (exit 4) that hides the others."""
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


def policy(default=3600, **ages):
    """A policy with `default` seconds everywhere; `machine_measured=60` style keywords override one pair."""
    table = {(k, s): default for k in ("dependency", "machine", "provider") for s in ("declared", "measured")}
    for key, seconds in ages.items():
        kind, source = key.split("_")
        table[(kind, source)] = seconds
    return _broker().Policy(max_age=table)


def decide(reg, wants, holds=(), pol=None, now=NOW):
    return _broker().decide(reg, pol or policy(), now, wants, holds)


def refused(call, *args, **kw) -> str:
    """The `reason` of the `BrokerError` the call raises; fails if it raises anything else or nothing."""
    with pytest.raises(_broker().BrokerError) as caught:
        call(*args, **kw)
    assert len(str(caught.value)) < 600                    # a message never carries an unbounded value
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
    """Catches a fourth verdict or an undocumented reason, an error that is not a `ValueError`, and a mutable
    `Amount`, `Policy`, `Line` or `Decision` (a decision someone edits after the fact is not a record)."""
    b = _broker()
    assert b.VERDICTS == ("grant", "wait", "refuse")
    assert b.REASONS == ("ok", "exhausted", "exceeds", "stale", "future", "unknown", "unit")
    assert issubclass(b.BrokerError, ValueError)
    assert [f.name for f in dataclasses.fields(b.Amount)] == ["kind", "name", "amount", "unit"]
    assert [f.name for f in dataclasses.fields(b.Line)] == [
        "kind", "name", "unit", "wanted", "limit", "in_use", "age", "verdict", "reason"]
    assert [f.name for f in dataclasses.fields(b.Decision)] == ["verdict", "reason", "registry", "now", "lines"]
    reg = registry(entry())
    decision = decide(reg, [amount()])
    for frozen, field in ((amount(), "amount"), (policy(), "rows"), (decision, "verdict"),
                          (decision.lines[0], "verdict")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(frozen, field, 7)
    assert decision.verdict in b.VERDICTS and decision.reason in b.REASONS


def test_an_amount_is_exactly_a_valid_kind_name_unit_and_count():
    """Catches an `Amount` that the registry's own grammar would refuse: a near spelling of a kind or unit, a
    `str` subclass, a name that is not `[a-z][a-z0-9_.-]{0,63}` in full, a count that is a bool, a float, below
    0 or above 10**15, an integer of 5000 digits (its `repr` raises a plain `ValueError` in Python)."""
    b = _broker()
    ok = {"kind": "provider", "name": "anthropic", "amount": 5, "unit": "sessions"}
    assert b.Amount(**ok).amount == 5
    assert b.Amount(**{**ok, "amount": 0}).amount == 0
    assert b.Amount(**{**ok, "amount": 10 ** 15}).amount == 10 ** 15
    assert b.Amount(**{**ok, "name": "a" * 64}).name == "a" * 64
    bad = [
        {"kind": "Provider"}, {"kind": "provider "}, {"kind": "provider\n"}, {"kind": "storage"}, {"kind": None},
        {"kind": Str("provider")}, {"name": "a" * 65}, {"name": ""}, {"name": "Anthropic"}, {"name": "a b"},
        {"name": "anthropic\n"}, {"name": ".."}, {"name": "a/b"}, {"name": "\u0430nthropic"}, {"name": "a\x00b"},
        {"name": Str("anthropic")}, {"name": "a" * 100000},
        {"unit": "session"}, {"unit": "Sessions"}, {"unit": "sessions\n"}, {"unit": None}, {"unit": Str("sessions")},
        {"amount": -1}, {"amount": 10 ** 15 + 1}, {"amount": True}, {"amount": 1.0}, {"amount": "1"},
        {"amount": None}, {"amount": 10 ** 5000}, {"amount": -10 ** 5000},
    ]
    for over in bad:
        assert refused(b.Amount, **{**ok, **over}) == "invalid", over


def test_a_policy_names_all_six_ages_and_each_is_whole_seconds_up_to_a_year():
    """Catches a policy with a default age (a missing pair must not be a silent number), a misspelt or extra
    pair, a non-integer or a bool age, and a ceiling that moved: 31622400 seconds (366 days) is allowed, one
    more is not."""
    b = _broker()
    pairs = [(k, s) for k in ("dependency", "machine", "provider") for s in ("declared", "measured")]
    good = {pair: 60 for pair in pairs}
    pol = b.Policy(max_age=good)
    assert pol.seconds("machine", "measured") == 60
    assert b.Policy(max_age={**good, ("machine", "measured"): 0}).seconds("machine", "measured") == 0
    assert b.Policy(max_age={**good, ("machine", "measured"): 31622400}).seconds("machine", "measured") == 31622400
    assert b.Policy(max_age=dict(reversed(list(good.items())))) == pol          # the order of the mapping is not state
    assert refused(b.Policy, max_age={**good, ("machine", "measured"): 31622401}) == "invalid"
    for seconds in (-1, True, 1.0, "60", None, 10 ** 5000):
        assert refused(b.Policy, max_age={**good, ("machine", "measured"): seconds}) == "invalid", seconds
    missing = dict(good)
    del missing[("provider", "declared")]
    assert refused(b.Policy, max_age=missing) == "invalid"
    assert refused(b.Policy, max_age={**good, ("storage", "declared"): 60}) == "invalid"
    assert refused(b.Policy, max_age={**good, ("machine", "estimated"): 60}) == "invalid"
    spelt = {p: 60 for p in pairs if p != ("machine", "measured")}
    spelt[("machine ", "measured")] = 60
    assert refused(b.Policy, max_age=spelt) == "invalid"
    assert refused(b.Policy, max_age={**good, "machine": 60}) == "invalid"
    for not_a_mapping in (None, [], list(good.items()), 60, "machine"):
        assert refused(b.Policy, max_age=not_a_mapping) == "invalid"


def test_the_judgement_runs_in_the_stated_order_and_names_one_reason():
    """Catches a line whose reason depends on the arithmetic that should not have run: no entry, then a unit
    that differs, then a fact from the future, then a stale one, then a request over the limit itself, then
    no room beside the holds. Each case below is wrong in every later way too."""
    old = ago(7200)                                        # 7200 s old against a 60 s policy: stale as well
    cases = [
        # (registry entries, wants, holds, expected verdict, reason)
        ([entry(name="x")], [amount(name="y", n=99)], [], "refuse", "unknown"),
        ([entry(as_of=old, limit=2)], [amount(n=5, unit="requests")], [amount(n=2)], "refuse", "unit"),
        ([entry(as_of=old, limit=2)], [amount(n=5)], [amount(n=2, unit="requests")], "refuse", "unit"),
        ([entry(as_of=ago(-1), limit=2)], [amount(n=5)], [amount(n=2)], "refuse", "future"),
        ([entry(as_of=old, limit=2)], [amount(n=5)], [amount(n=2)], "refuse", "stale"),
        ([entry(limit=2)], [amount(n=5)], [amount(n=2)], "refuse", "exceeds"),
        ([entry(limit=2)], [amount(n=1)], [amount(n=2)], "wait", "exhausted"),
        ([entry(limit=2)], [amount(n=1)], [amount(n=1)], "grant", "ok"),
    ]
    for entries, wants, holds, verdict, why in cases:
        decision = decide(registry(*entries), wants, holds, pol=policy(60))
        assert (decision.verdict, decision.reason) == (verdict, why), (entries, wants, holds, decision)
        assert (decision.lines[0].verdict, decision.lines[0].reason) == (verdict, why)
    # the unit of *any* hold counts, whichever holds come first or last (not the first, not the last one)
    for holds in ([amount(n=1, unit="requests"), amount(n=1)], [amount(n=1), amount(n=1, unit="requests")],
                  [amount(n=1), amount(n=1), amount(n=1, unit="tokens"), amount(n=1)]):
        decision = decide(registry(entry(limit=8)), [amount(n=1)], holds)
        assert (decision.verdict, decision.reason) == ("refuse", "unit"), holds
    # an unknown capacity is not zero (which would be `exceeds`) and not unlimited (which would be `ok`):
    unknown = decide(registry(entry(name="x")), [amount(name="y", n=0)])
    assert (unknown.verdict, unknown.reason) == ("refuse", "unknown")
    assert (unknown.lines[0].limit, unknown.lines[0].age) == (None, None)
    # and the empty registry knows nothing:
    assert decide(registry(), [amount(n=0)]).reason == "unknown"


def test_the_arithmetic_is_exact_at_the_edge_and_the_three_kinds_are_never_added():
    """Catches an off-by-one at the limit, a hold that is not summed, a hold of another kind or name counted
    against this capacity, and a request larger than the limit reported as one worth waiting for."""
    reg = registry(entry(limit=8))
    table = [  # (held, wanted, verdict, reason)
        (0, 8, "grant", "ok"), (0, 9, "refuse", "exceeds"), (5, 3, "grant", "ok"), (5, 4, "wait", "exhausted"),
        (8, 0, "grant", "ok"), (8, 1, "wait", "exhausted"), (9, 0, "wait", "exhausted"), (0, 0, "grant", "ok"),
        (7, 1, "grant", "ok"), (100, 9, "refuse", "exceeds"),
    ]
    for held, wanted, verdict, why in table:
        holds = [amount(n=part) for part in (held // 3, held // 3, held - 2 * (held // 3))]    # three holders
        decision = decide(reg, [amount(n=wanted)], holds)
        assert (decision.verdict, decision.reason) == (verdict, why), (held, wanted)
        assert decision.lines[0].in_use == held and decision.lines[0].limit == 8 and decision.lines[0].wanted == wanted
    big = registry(entry(limit=10 ** 15, unit="bytes", name="disk.free"))
    assert decide(big, [amount(name="disk.free", n=10 ** 15, unit="bytes")], []).verdict == "grant"
    assert decide(big, [amount(name="disk.free", n=10 ** 15, unit="bytes")],
                  [amount(name="disk.free", n=1, unit="bytes")]).reason == "exhausted"
    # three kinds, one name: holds of one are not holds of another; a want of a kind nobody recorded is unknown
    both = registry(entry(kind="machine", name="x", limit=4), entry(kind="provider", name="x", limit=4))
    holds = [amount(kind="provider", name="x", n=4)]
    assert decide(both, [amount(kind="machine", name="x", n=4)], holds).verdict == "grant"
    assert decide(both, [amount(kind="provider", name="x", n=1)], holds).reason == "exhausted"
    assert decide(both, [amount(kind="dependency", name="x", n=1)], holds).reason == "unknown"
    # ... nor is its unit: a hold of another kind in another unit is not a hold of this capacity
    units = registry(entry(kind="machine", name="x", limit=4), entry(kind="provider", name="x", limit=4, unit="tokens"))
    cross = [amount(kind="provider", name="x", n=2, unit="tokens")]
    assert decide(units, [amount(kind="machine", name="x", n=4)], cross).reason == "ok"
    assert decide(units, [amount(kind="provider", name="x", n=2, unit="tokens")], cross).reason == "ok"
    assert decide(units, [amount(kind="provider", name="x", n=3, unit="tokens")], cross).reason == "exhausted"
    # holds that are not about a wanted capacity are not looked at (nor are holds the registry does not know)
    others = [amount(name="host.other", n=99), amount(kind="provider", name="ghost", n=99, unit="tokens")]
    assert decide(registry(entry(limit=8), entry(name="host.other", limit=1)), [amount(n=8)], others).verdict == "grant"


def test_a_fact_is_fresh_for_exactly_its_policy_age_and_not_one_second_more():
    """Catches a boundary that moved (`>=` for `>`), an age that drops the days of a difference (`.seconds` for
    `.total_seconds()`), a fact from after `now` accepted, a policy that ignores `source` or `kind`, and a
    calendar that is not the calendar (leap day, new year)."""
    for age, max_age, verdict, why in [(60, 60, "grant", "ok"), (61, 60, "refuse", "stale"), (0, 0, "grant", "ok"),
                                       (1, 0, "refuse", "stale"), (86401, 86400, "refuse", "stale"),
                                       (86401, 86401, "grant", "ok"), (31622400, 31622400, "grant", "ok")]:
        decision = decide(registry(entry(as_of=ago(age))), [amount()], pol=policy(machine_measured=max_age))
        assert (decision.verdict, decision.reason) == (verdict, why), (age, max_age)
        assert decision.lines[0].age == age
    future = decide(registry(entry(as_of=ago(-1))), [amount()], pol=policy(31622400))
    assert (future.verdict, future.reason, future.lines[0].age) == ("refuse", "future", -1)
    assert decide(registry(entry(as_of=NOW)), [amount()], pol=policy(0)).verdict == "grant"      # age 0 is now
    # the policy is per (kind, source): the same 120 s old figure, four ways
    reg = registry(entry(kind="machine", name="a", source="measured", as_of=ago(120)),
                   entry(kind="machine", name="b", source="declared", as_of=ago(120)),
                   entry(kind="provider", name="a", source="measured", as_of=ago(120)),
                   entry(kind="provider", name="b", source="declared", as_of=ago(120)))
    pol = policy(machine_measured=60, machine_declared=3600, provider_measured=3600, provider_declared=60)
    got = {(w.kind, w.name): decide(reg, [w], pol=pol).reason for w in (
        amount(kind="machine", name="a"), amount(kind="machine", name="b"),
        amount(kind="provider", name="a"), amount(kind="provider", name="b"))}
    assert got == {("machine", "a"): "stale", ("machine", "b"): "ok", ("provider", "a"): "ok",
                   ("provider", "b"): "stale"}
    # calendar: a leap day is 86401 s from the 28th 23:59:59 to the 1st 00:00:00; a common year's is 1 s
    leap = registry(entry(as_of="2028-02-28T23:59:59Z"))
    assert decide(leap, [amount()], pol=policy(86400), now="2028-03-01T00:00:00Z").reason == "stale"
    assert decide(leap, [amount()], pol=policy(86401), now="2028-03-01T00:00:00Z").reason == "ok"
    common = registry(entry(as_of="2026-02-28T23:59:59Z"))
    assert decide(common, [amount()], pol=policy(1), now="2026-03-01T00:00:00Z").reason == "ok"
    assert decide(common, [amount()], pol=policy(0), now="2026-03-01T00:00:00Z").reason == "stale"
    year = registry(entry(as_of="2025-12-31T23:59:59Z"))
    assert decide(year, [amount()], pol=policy(1), now="2026-01-01T00:00:00Z").lines[0].age == 1


def test_one_request_is_all_or_nothing_and_the_first_worst_line_names_the_decision():
    """Catches a partial grant, a decision that takes the last (or the best) line, lines in the order the
    caller happened to list them, a capacity wanted twice summed silently, and an empty request granted."""
    reg = registry(entry(kind="machine", name="a", limit=4), entry(kind="provider", name="b", limit=4),
                   entry(kind="dependency", name="c", limit=4, unit="items"))
    ok = [amount(kind="provider", name="b"), amount(kind="machine", name="a"),
          amount(kind="dependency", name="c", unit="items")]
    decision = decide(reg, ok)
    assert (decision.verdict, decision.reason) == ("grant", "ok")
    assert [(l.kind, l.name) for l in decision.lines] == [("dependency", "c"), ("machine", "a"), ("provider", "b")]
    assert decision.lines == decide(reg, list(reversed(ok))).lines
    held = [amount(kind="machine", name="a", n=4)]
    waiting = decide(reg, ok, held)
    assert (waiting.verdict, waiting.reason) == ("wait", "exhausted")
    assert [l.verdict for l in waiting.lines] == ["grant", "wait", "grant"]              # one line waits: none granted
    mixed = ok + [amount(kind="provider", name="z")]                                       # unknown: refuse beats wait
    refusal = decide(reg, mixed, held)
    assert (refusal.verdict, refusal.reason) == ("refuse", "unknown")
    two = decide(reg, [amount(kind="provider", name="z"), amount(kind="dependency", name="c", unit="tokens")])
    assert (two.verdict, two.reason) == ("refuse", "unit")                               # dependency/c sorts first
    assert [l.reason for l in two.lines] == ["unit", "unknown"]
    assert refused(decide, reg, [amount(), amount(n=2)]) == "invalid"                      # one line per capacity
    assert refused(decide, reg, []) == "invalid"                                           # nothing is not a request


def test_the_registry_is_exactly_what_the_reader_produced_and_the_decision_quotes_its_digest(tmp_path):
    """Catches a decision over a hand-built `Registry` (a digest that is not that of its entries, entries that
    were edited, an entry corrupted in place, something that is not a `Registry`), and a decision that does
    not say which registry it judged. A registry read from a file judges like one parsed from bytes."""
    cap = _capacity()
    good = registry(entry(limit=8), entry(kind="provider", name="anthropic", limit=3))
    decision = decide(good, [amount()])
    assert decision.registry == good.digest and len(decision.registry) == 64
    path = tmp_path / "capacity.json"
    cap.write(path, good.entries)
    from_file = cap.read(path)
    assert decide(from_file, [amount()]) == decision
    forged = cap.Registry(entries=good.entries, digest="0" * 64)
    assert refused(decide, forged, [amount()]) == "registry"
    other = dataclasses.replace(good, entries=(entry(limit=9),))
    assert refused(decide, other, [amount()]) == "registry"                                # old digest, new entries
    as_list = cap.Registry(entries=list(good.entries), digest=good.digest)
    assert refused(decide, as_list, [amount()]) == "registry"
    twisted = registry(entry(limit=8))
    object.__setattr__(twisted.entries[0], "limit", 10 ** 15 + 1)                          # an entry edited in place
    assert refused(decide, twisted, [amount()]) == "registry"
    fake = cap.Registry(entries=(object(),), digest=good.digest)
    assert refused(decide, fake, [amount()]) == "registry"
    class Sub(cap.Registry):                                                               # exactly a Registry
        pass
    assert refused(decide, Sub(entries=good.entries, digest=good.digest), [amount()]) == "registry"
    for not_a_registry in (None, {"entries": []}, good.entries, good.digest, Str("x")):
        assert refused(decide, not_a_registry, [amount()]) == "registry"


def test_a_missing_or_malformed_input_is_refused_and_never_read_as_nothing_held():
    """Catches `holds` defaulting to nothing (an absent fact is never zero), a `now` that is not a canonical
    time, a request of more than 64 lines or 1024 holds, an endless iterable consumed past 65 (1025) items, a
    policy or an amount that is something else or a subclass of one, a plain `TypeError` where a `BrokerError` is
    the contract, and a `BrokerError` where the caller's own iterable failed (that exception is theirs, unchanged)."""
    b = _broker()
    reg = registry(entry())
    params = inspect.signature(b.decide).parameters
    assert list(params) == ["registry", "policy", "now", "wants", "holds"]
    assert all(p.default is inspect.Parameter.empty for p in params.values())              # no default anywhere
    with pytest.raises(TypeError):
        b.decide(reg, policy(), NOW, [amount()])
    for now in ("2026-10-12T09:00:00", "2026-10-12 09:00:00Z", "2026-10-12T09:00:00z", "2026-10-12T09:00:00Z\n",
                "2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z", "2026-10-12T09:00:60Z",
                "\u0662026-10-12T09:00:00Z", "", "now", None, 1760259600, datetime(2026, 10, 12), Str(NOW)):
        assert refused(b.decide, reg, policy(), now, [amount()], []) == "invalid", now
    assert refused(b.decide, reg, {"machine": 60}, NOW, [amount()], []) == "invalid"
    assert refused(b.decide, reg, None, NOW, [amount()], []) == "invalid"
    wants64 = [amount(name=f"host.s{i}", n=0) for i in range(64)]
    big = registry(*[entry(name=f"host.s{i}") for i in range(65)])
    assert b.decide(big, policy(), NOW, wants64, []).verdict == "grant"
    assert refused(b.decide, big, policy(), NOW, wants64 + [amount(name="host.s64", n=0)], []) == "invalid"
    assert b.decide(reg, policy(), NOW, [amount(n=0)], [amount(n=0)] * 1024).verdict == "grant"
    assert refused(b.decide, reg, policy(), NOW, [amount()], [amount(n=0)] * 1025) == "invalid"
    with alarm():
        assert refused(b.decide, reg, policy(), NOW, [amount()], itertools.repeat(amount(n=0))) == "invalid"
        assert refused(b.decide, reg, policy(), NOW, itertools.repeat(amount()), []) == "invalid"

    def endless(item, pulled):
        while True:
            pulled.append(1)
            yield item
    pulled = []                                              # at most limit + 1 items are taken, not "until it stops"
    assert refused(b.decide, reg, policy(), NOW, endless(amount(), pulled), []) == "invalid"
    assert 0 < len(pulled) <= 65, len(pulled)
    pulled = []
    assert refused(b.decide, reg, policy(), NOW, [amount()], endless(amount(n=0), pulled)) == "invalid"
    assert 0 < len(pulled) <= 1025, len(pulled)

    def breaks(exc):
        yield amount(n=0)
        raise exc
    for exc in (ValueError("a"), KeyError("b"), OSError("c"), TypeError("d")):         # the caller's failure is theirs
        for position in (0, 1):
            args = [breaks(exc), []] if position == 0 else [[amount()], breaks(exc)]
            with pytest.raises(type(exc)) as caught:
                b.decide(reg, policy(), NOW, *args)
            assert caught.value is exc
    # exactly an Amount, a Policy (a subclass is not one, though it carries valid fields)
    class SubAmount(b.Amount):
        pass

    class SubPolicy(b.Policy):
        pass
    sub = SubAmount("machine", "host.sessions", 1, "sessions")
    assert refused(b.decide, reg, policy(), NOW, [sub], []) == "invalid"
    assert refused(b.decide, reg, policy(), NOW, [amount()], [sub]) == "invalid"
    ages = {(k, s): 60 for k in ("dependency", "machine", "provider") for s in ("declared", "measured")}
    assert refused(b.decide, reg, SubPolicy(ages), NOW, [amount()], []) == "invalid"
    for odd in (None, 7, "machine", b"machine", {"kind": "machine"}, [{"kind": "machine"}], [None], [entry()],
                [("machine", "host.sessions", 1, "sessions")]):
        assert refused(b.decide, reg, policy(), NOW, odd, []) == "invalid", odd
        assert refused(b.decide, reg, policy(), NOW, [amount()], odd) == "invalid", odd
    generated = (a for a in [amount(n=3)])                                                 # any iterable will do
    assert b.decide(reg, policy(), NOW, generated, iter([])).verdict == "grant"
    # a frozen value edited in place is judged again, as the registry is: the checks are not only on the way in
    for field, value in (("amount", -5), ("amount", 10 ** 15 + 1), ("amount", True), ("name", "Host\n"),
                         ("kind", "storage"), ("unit", "session")):
        edited = amount(n=1)
        object.__setattr__(edited, field, value)
        assert refused(b.decide, reg, policy(), NOW, [edited], []) == "invalid", (field, value)
        assert refused(b.decide, reg, policy(), NOW, [amount()], [edited]) == "invalid", (field, value)
    six = [(k, s, 60) for k in ("dependency", "machine", "provider") for s in ("declared", "measured")]
    for rows in (7, (), tuple(six[:5]), tuple(six[:5]) + (("machine", "measured", -1),),
                 tuple(six[:5]) + (("machine", "measured", 31622401),), tuple(six[:5]) + (("machine", "measured"),),
                 tuple(six[:5]) + ((["machine"], "measured", 60),), tuple(six[:5]) + (("storage", "measured", 60),)):
        edited = policy()
        object.__setattr__(edited, "rows", rows)
        assert refused(b.decide, reg, edited, NOW, [amount()], []) == "invalid", rows


def test_a_decision_is_a_plain_value_that_says_what_it_judged_and_judges_the_same_twice():
    """Catches an `as_dict` that changed its keys or its order of lines, an unrecorded `now` or registry, a
    decision that depends on the order of wants or holds, one that mutates what it was given, and one that is
    not plain JSON (the shape a later slice writes durably)."""
    reg = registry(entry(limit=8, as_of=ago(60)))
    wants = [amount(kind="provider", name="anthropic", n=1), amount(n=3)]
    holds = [amount(n=2), amount(n=1)]
    snapshot = (list(wants), list(holds), reg.digest)
    first = decide(reg, wants, holds)
    assert first.as_dict() == {
        "verdict": "refuse", "reason": "unknown", "registry": reg.digest, "now": NOW,
        "lines": [
            {"kind": "machine", "name": "host.sessions", "unit": "sessions", "wanted": 3, "limit": 8, "in_use": 3,
             "age": 60, "verdict": "grant", "reason": "ok"},
            {"kind": "provider", "name": "anthropic", "unit": "sessions", "wanted": 1, "limit": None, "in_use": 0,
             "age": None, "verdict": "refuse", "reason": "unknown"},
        ]}
    assert json.loads(json.dumps(first.as_dict(), sort_keys=True)) == first.as_dict()
    assert decide(reg, wants, holds) == first
    assert decide(reg, list(reversed(wants)), list(reversed(holds))) == first
    assert (wants, holds, reg.digest) == snapshot
    assert first.registry == reg.digest and first.now == NOW


def test_the_module_reads_no_clock_runs_nothing_writes_nothing_and_imports_only_the_registry():
    """Catches a later edit that makes the broker impure: `datetime.now()`, a file, a process, the network, the
    environment, a private name of the registry module. The modules it may import and the names it may take
    from `capacity` are listed; the write- and clock-capable attribute names are forbidden anywhere."""
    tree = ast.parse(Path(_broker().__file__).read_text(encoding="utf-8"))
    imported, from_capacity = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
            if node.level:
                assert node.module == "capacity", node.module
                from_capacity |= {a.name for a in node.names}
    assert imported <= {"__future__", "collections.abc", "dataclasses", "datetime", "itertools", ".capacity"}, imported
    assert from_capacity <= {"KINDS", "SOURCES", "CapacityError", "Entry", "Registry", "parse", "render"}, from_capacity
    forbidden = {"write_bytes", "write_text", "read_bytes", "read_text", "open", "unlink", "rmdir", "remove",
                 "rename", "touch", "mkdir", "chmod", "system", "popen", "Popen", "environ", "getenv", "urlopen",
                 "connect", "fdopen", "makedirs", "truncate", "durable_write", "read_single_link", "read", "write"}
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attributes & forbidden, attributes & forbidden
    clock = {"now", "utcnow", "today", "fromtimestamp", "monotonic", "time", "time_ns", "perf_counter"}
    for node in ast.walk(tree):                           # `self.now` is the field of a `Decision`, not a clock
        if isinstance(node, ast.Attribute) and node.attr in clock:
            assert isinstance(node.value, ast.Name) and node.value.id == "self", ast.dump(node)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "exec", "eval", "compile", "__import__", "input", "print"}
