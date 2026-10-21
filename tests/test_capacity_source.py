"""P3-16 slice 3: the capacity source -- the registry, the provider state and the slots in one snapshot, each fact with
its time and its source, and a staleness policy that makes an old fact UNKNOWN and narrows the decision.

`hoh capacity show --source` prints the snapshot and `hoh capacity check` decides on it; the tests that matter drive
them through `hoh.cli.main` against a state root `StorageRegistry.init()` made under `tmp_path`, a runs root of the
test's own, and real parked runs (`RunStore`, `dispatchrecovery.append_record`) and real leases and reservations.
The broker's own judgement is pinned by `tests/test_broker.py`, the health reading by `tests/test_provider_health.py`,
the command's holds by `tests/test_capacity_check.py`; this file pins what the snapshot adds: which fact is read from
where, when it is UNKNOWN, and what it holds back from a decision.

Hermetic: `HOH_STATE_ROOT` is removed and `HOME` / `XDG_STATE_HOME` point below `tmp_path`; nothing reads
`~/.agents`, starts a process, Herdr or an agent, or opens the network. Every bound below is a literal (300, 60, 256,
220), not the module's constant: a table imported from the module would agree with a mutated module.
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hoh import cli, dispatchrecovery, quota
from hoh.contracts import Condition, RunState, Stage
from hoh.dispatchrecovery import NO_MASTER, DispatchProvenance, ParkBinding, RoutingDecision, park_id
from hoh.failures import NormalizedFailure
from hoh.store import RunStore

FILE = "capacity-registry.json"
TIME = "%Y-%m-%dT%H:%M:%SZ"
N = NormalizedFailure
NOW = "2026-10-12T12:00:00Z"


def _import(name: str):
    import importlib
    return importlib.import_module(name)


@pytest.fixture(autouse=True)
def hermetic_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("HOH_STATE_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))

    def forbidden(*args, **kwargs):
        pytest.fail("a test started a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def utc(delta_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).strftime(TIME)


def later(stamp: str, seconds: int) -> str:
    return (datetime.strptime(stamp, TIME) + timedelta(seconds=seconds)).strftime(TIME)


def retry_at(seconds: int, base: str | None = None) -> str:
    """A stated reset, in the form `quota.next_attempt_at` writes (aware ISO, whole seconds)."""
    start = datetime.strptime(base, TIME).replace(tzinfo=timezone.utc) if base else datetime.now(timezone.utc)
    return (start + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


class World:
    """A state root with a registry, a runs root, and helpers that make the records the product makes."""

    def __init__(self, tmp_path: Path) -> None:
        from hoh.storage_registry import StorageRegistry
        self.state = tmp_path / "state"
        StorageRegistry(self.state).init()
        self.state = self.state.resolve()
        self.runs = tmp_path / "runs"
        self.runs.mkdir()
        self.record = self.state / FILE

    def registry(self, *entries):
        capacity = _import("hoh.capacity")
        made = [capacity.Entry(kind=k, name=n, limit=l, unit=u, source=s, as_of=rest[0] if rest else utc())
                for (k, n, l, u, s, *rest) in entries]
        return capacity.write(self.record, made)

    def lease(self, project: str, run_id: str) -> None:
        from hoh.runlease import RunLeases
        RunLeases(self.runs / "projects" / project / "run-leases").acquire(run_id, "holder", ttl_seconds=3600)

    def reserve(self, run_id: str, amount: int) -> None:
        from hoh.reservation import BudgetReservations
        BudgetReservations(self.runs).reserve(run_id, ceiling=100, spent=lambda: {}, want=amount)

    def park(self, run_id: str, *, failure: N = N.USAGE_LIMIT, retry: str | None = None, provider: str | None = "claude",
             account: str = "acct-a", attempts: int = 0) -> None:
        """A run parked on `failure` with the receipt the controller writes (no receipt for `provider=None`)."""
        retry = retry or retry_at(3600)
        state = RunState(run_id=run_id, repo_path="/arena/repo", project_name="demo", spec_path="/arena/spec.md",
                         spec_digest="s", policy_digest="p", profile_digest="h", stage=Stage.DEVELOPING,
                         condition=Condition.BLOCKED, iteration=1)
        state.blocked_kind, state.retry_after, state.blocked_reason = failure.value.lower(), retry, "parked"
        state.resume_attempts = attempts
        store = RunStore(self.runs, run_id)
        store.write_state(state)
        if provider is not None:
            binding = ParkBinding(programme="NONE", program_epoch=0, run_id=run_id, iteration=1, attempt=0,
                                  stage=state.stage.value, candidate="NONE", master=NO_MASTER)
            dispatchrecovery.append_record(store.dir, RoutingDecision(
                park_id=park_id(binding, failure, retry), reason=failure.value, binding=binding,
                provenance=DispatchProvenance(provider=provider, model="m-1", account_handle=account,
                                              role="developer",
                                              capability_requirements=["read_repo", "write_worktree", "run_shell"]),
                retry_at=retry))

    def run(self, capsys, *argv):
        try:
            code = cli.main(["--root", str(self.runs), *argv, "--storage-root", str(self.state)])
        except SystemExit as stop:
            code = stop.code
        seen = capsys.readouterr()
        return code, seen.out, seen.err

    def show(self, capsys):
        code, out, err = self.run(capsys, "capacity", "show", "--source")
        assert code == 0 and err == "" and out.isascii(), (code, err)
        return json.loads(out)

    def check(self, capsys, kind, name, amount, unit):
        code, out, err = self.run(capsys, "capacity", "check", kind, name, amount, unit)
        assert err == "" and out.isascii(), err
        return {"code": code, **json.loads(out)}


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def pick(source: dict, field: str, name: str = "", account: str = "") -> dict:
    (found,) = [f for f in source["facts"] if (f["field"], f["name"], f["account"]) == (field, name, account)]
    return found


def fingerprint(root: Path) -> dict:
    seen = {}
    for path in sorted(root.rglob("*")):
        info = os.lstat(path)
        body = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        seen[str(path.relative_to(root))] = (info.st_mode, info.st_size, info.st_mtime_ns, body)
    return seen


def source_module():
    return _import("hoh.capacitysource")


def policy(declared: int = 604800, measured: int = 300):
    return _import("hoh.capacitycheck").policy(declared, measured)


# --- R1 the snapshot, through the real command ----------------------------------------------------------------------

def test_show_source_prints_every_fact_with_its_time_source_age_and_bound_through_the_real_command(world, capsys):
    """Catches a snapshot that drops a family (caps, provider state, slots), a fact without its time, source, age or
    bound, an `as_of` that is not the entry's own, a provider reset that is not the stated one, a slot count that is not
    what the leases and reservations hold, or a registry digest that is not the record's: three entries (one measured),
    a claude account limited for an hour, one live lease and a reservation of three dispatches -- eleven facts in the
    stated order, every one FRESH, the timestamps between the clocks before and after the command."""
    world.registry(("machine", "host.sessions", 4, "sessions", "declared"),
                   ("machine", "probe.sessions", 3, "sessions", "measured", utc(-10)),
                   ("provider", "claude.sessions", 2, "sessions", "declared"))
    reset = retry_at(3600)
    world.park("limited", retry=reset)
    world.lease("p1", "r1")
    world.reserve("r2", 3)
    before = datetime.now(timezone.utc).replace(microsecond=0)
    shown = world.show(capsys)
    after = datetime.now(timezone.utc)
    assert set(shown) == {"storage_root", "path", "digest", "entries", "source"}
    source = shown["source"]
    assert set(source) == {"taken_at", "judged_at", "runs_root", "registry", "complete", "facts"}
    assert source["registry"] == shown["digest"] == hashlib.sha256(world.record.read_bytes()).hexdigest()
    assert source["runs_root"] == str(world.runs) and source["complete"] is True
    assert source["taken_at"] == source["judged_at"]
    taken = datetime.strptime(source["taken_at"], TIME).replace(tzinfo=timezone.utc)
    assert before <= taken <= after
    assert [(f["family"], f["name"], f["account"], f["field"]) for f in source["facts"]] == [
        ("cap", "host.sessions", "", "declared_cap"), ("cap", "probe.sessions", "", "observed_cap"),
        ("cap", "claude.sessions", "", "declared_cap"), ("cap", "claude.sessions", "", "observed_safe_cap"),
        ("provider", "claude", "acct-a", "health_state"), ("provider", "claude", "acct-a", "reset_at"),
        ("provider", "claude", "acct-a", "failures"),
        ("provider", "", "", "problems"), ("provider", "", "", "unnamed"),
        ("slots", "host.sessions", "", "active"), ("slots", "dispatches.reserved", "", "reserved")]
    assert all(f["status"] == "FRESH" for f in source["facts"])
    assert [f["why"] for f in source["facts"] if f["why"]] == ["claude/acct-a is LIMITED"]
    assert all(f["age"] == 0 for f in source["facts"] if f["family"] != "cap")          # observed and judged at the command's `now`
    assert all(0 <= f["age"] <= 120 for f in source["facts"] if f["field"] != "observed_cap")
    assert 10 <= pick(source, "observed_cap", "probe.sessions")["age"] <= 120
    by = {(f["family"], f["field"], f["name"]): f for f in source["facts"]}
    declared = by[("cap", "declared_cap", "host.sessions")]
    assert (declared["kind"], declared["value"], declared["unit"], declared["source"], declared["bound"]) == (
        "machine", 4, "sessions", "registry:declared", 604800)
    assert declared["observed_at"] == json.loads(world.record.read_bytes())["entries"][0]["as_of"]
    measured = by[("cap", "observed_cap", "probe.sessions")]
    assert (measured["value"], measured["source"], measured["bound"], measured["observed_at"] <= source["taken_at"]) == (
        3, "registry:measured", 300, True)
    state = by[("provider", "health_state", "claude")]
    assert (state["kind"], state["value"], state["source"], state["observed_at"], state["bound"]) == (
        "provider", "LIMITED", "health.read_health", source["taken_at"], 300)
    assert by[("provider", "reset_at", "claude")]["value"] == reset
    assert by[("provider", "failures", "claude")]["value"] == "USAGE_LIMIT=1"
    safe = by[("cap", "observed_safe_cap", "claude.sessions")]
    assert (safe["kind"], safe["value"], safe["unit"], safe["source"], safe["observed_at"], safe["bound"]) == (
        "provider", 0, "sessions", "health.read_health", source["taken_at"], 300)
    assert by[("provider", "problems", "")]["value"] == 0 and by[("provider", "unnamed", "")]["value"] == 0
    active, reserved = by[("slots", "active", "host.sessions")], by[("slots", "reserved", "dispatches.reserved")]
    assert (active["kind"], active["value"], active["unit"], active["source"], active["bound"]) == (
        "machine", 1, "sessions", "capacitycheck.live_holds", 60)
    assert (reserved["kind"], reserved["value"], reserved["unit"], reserved["bound"]) == ("provider", 3, "requests", 60)


# --- R2 the flag is the only way in ---------------------------------------------------------------------------------

def test_without_the_flag_show_is_unchanged_and_never_reads_the_runs_root(world, capsys, monkeypatch):
    """Catches a `show` that always takes the snapshot: the plain output keeps its four keys and its bytes, the snapshot
    function is never called, and a runs root that does not exist changes nothing; with the flag the same missing root
    is not a refusal -- the slots are UNKNOWN with the reason and `complete` is false (a report prints what it can)."""
    world.registry(("machine", "host.sessions", 4, "sessions", "declared"))
    mod = source_module()

    def forbidden(*args, **kwargs):
        pytest.fail("show without --source took a snapshot")

    real_take = mod.take
    monkeypatch.setattr(mod, "take", forbidden)
    gone = world.runs / "no-such-root"
    code, out, err = world.run(capsys, "capacity", "show")
    assert code == 0 and err == ""
    assert set(json.loads(out)) == {"storage_root", "path", "digest", "entries"}
    monkeypatch.setattr(mod, "take", real_take)
    world.runs = gone
    shown = world.show(capsys)["source"]
    assert shown["complete"] is False
    for field, name in (("active", "host.sessions"), ("reserved", "dispatches.reserved")):
        fact = pick(shown, field, name)
        assert (fact["status"], fact["value"]) == ("UNKNOWN", None) and "does not exist" in fact["why"]
    assert pick(shown, "declared_cap", "host.sessions")["status"] == "FRESH"


# --- R3 caps: the broker's own boundary ------------------------------------------------------------------------------

def test_a_cap_is_unknown_exactly_when_the_broker_calls_it_stale_or_future(world):
    """Catches an off-by-one in the age bound, a future fact read as fresh, a measured figure given the declared bound, and
    a stale number that is still shown: declared 100 s / measured 50 s, five entries at the edges (100 and 50 s old are
    fresh, 101 and 51 are not, one second ahead is not) -- the fact's status is FRESH exactly when `broker.decide`'s line
    says `ok`, the reason is the broker's (`stale` / `future`), an UNKNOWN fact has no value, and `capacitysource.decide` on a kept
    snapshot judges the cap at the later `now` it is given (granted at T, `refuse`/`stale` at T+1 for a cap 100 s old at T)."""
    from hoh.broker import Amount, decide
    made = [("machine", "a.fresh", 5, "sessions", "declared", later(NOW, -100)),
            ("machine", "b.stale", 5, "sessions", "declared", later(NOW, -101)),
            ("machine", "c.fresh", 5, "sessions", "measured", later(NOW, -50)),
            ("machine", "d.stale", 5, "sessions", "measured", later(NOW, -51)),
            ("machine", "e.future", 5, "sessions", "declared", later(NOW, 1))]
    reg = world.registry(*made)
    mod = source_module()
    snap = mod.take(reg, runs_root=world.runs, now=NOW, policy=policy(100, 50))
    by = {f.name: f for f in mod.facts(snap, now=NOW) if f.family == "cap"}
    reasons = {line.name: line.reason for line in decide(
        reg, policy(100, 50), NOW, [Amount("machine", name, 1, "sessions") for name in by], ()).lines}
    assert reasons == {"a.fresh": "ok", "b.stale": "stale", "c.fresh": "ok", "d.stale": "stale", "e.future": "future"}
    assert {name: f.status for name, f in by.items()} == {
        "a.fresh": "FRESH", "b.stale": "UNKNOWN", "c.fresh": "FRESH", "d.stale": "UNKNOWN", "e.future": "UNKNOWN"}
    assert [(f.value, f.age, f.bound) for f in (by["a.fresh"], by["b.stale"], by["c.fresh"], by["d.stale"])] == [
        (5, 100, 100), (None, 101, 100), (5, 50, 50), (None, 51, 50)]
    assert by["e.future"].age == -1 and by["e.future"].why == "observed after now"
    assert by["b.stale"].why == "101 s old; at most 100 s"
    # a kept snapshot is decided at a later `now`: the broker judges the cap at that `now`, not at the time of the reading
    from hoh.broker import Amount as Want
    kept = mod.take(reg, runs_root=world.runs, now=NOW, policy=policy(100, 50), holds_age=10 ** 6, health_age=10 ** 6)
    ask = [Want("machine", "a.fresh", 1, "sessions")]
    now_line = mod.decide(kept, ask, now=NOW)[0]
    late_line = mod.decide(kept, ask, now=later(NOW, 1))[0]
    assert (now_line.verdict, now_line.reason) == ("grant", "ok")
    assert (late_line.verdict, late_line.reason, late_line.lines[0].age) == ("refuse", "stale", 101)
    assert {f.field for f in by.values()} == {"declared_cap", "observed_cap"} and by["c.fresh"].field == "observed_cap"


# --- R4 provider state and slots have bounds of their own ------------------------------------------------------------

def test_provider_state_is_good_for_five_minutes_and_slots_for_a_minute_and_a_future_fact_is_unknown(world):
    """Catches a bound that is off by one or swapped, an unbounded provider reading, a snapshot whose facts never age, a
    stale value that survives, and a fact from after `now` read as fresh: taken at T, the provider facts are FRESH at
    T+300 and UNKNOWN at T+301, the slot counts FRESH at T+60 and UNKNOWN at T+61, everything UNKNOWN one second
    before T, an UNKNOWN fact has no value, and a malformed `now` is a `ValueError`, not an UNKNOWN reading."""
    reg = world.registry(("machine", "host.sessions", 4, "sessions", "declared", later(NOW, -5)))
    world.park("limited", retry=retry_at(3600, NOW))
    world.lease("p1", "r1")
    mod = source_module()
    snap = mod.take(reg, runs_root=world.runs, now=NOW)

    def status(now: str) -> dict:
        return {(f.family, f.field): (f.status, f.value is None) for f in mod.facts(snap, now=now) if f.family != "cap"}

    for family, field, edge in (("provider", "health_state", 300), ("provider", "problems", 300),
                                ("slots", "active", 60), ("slots", "reserved", 60)):
        assert status(later(NOW, edge))[(family, field)] == ("FRESH", False), (family, field)
        assert status(later(NOW, edge + 1))[(family, field)] == ("UNKNOWN", True), (family, field)
        assert status(later(NOW, -1))[(family, field)] == ("UNKNOWN", True), (family, field)
    aged = {f.field: f for f in mod.facts(snap, now=later(NOW, 301)) if f.family == "provider"}
    assert aged["health_state"].why == "301 s old; at most 300 s" and aged["health_state"].age == 301
    assert [f.status for f in mod.facts(snap, now=NOW)] == ["FRESH"] * len(snap.observations)
    assert mod.view(snap, now=NOW) == mod.view(snap, now=NOW)
    for bad in ("2026-10-12", "2026-10-12T12:00:00", "", "2026-13-12T12:00:00Z"):
        with pytest.raises(ValueError):
            mod.take(reg, runs_root=world.runs, now=bad)


# --- R5 slots: no decision on a hold that cannot be known ---------------------------------------------------------

def test_slots_that_are_unknown_make_no_decision_and_never_a_hold_of_zero(world, capsys):
    """Catches a decision taken beside holds that could not be read (read as zero), a snapshot that is judged later and
    still believed, and a message that is lost: a runs root that does not exist is `CheckError` (`holds`) from `decide`
    with the reader's words, a snapshot judged 61 s after it was taken likewise, one judged at 60 s is decided; through
    the command the missing root is the same refusal as before, exit 2 and nothing on stdout."""
    from hoh.broker import Amount
    from hoh.capacitycheck import CheckError
    reg = world.registry(("machine", "host.sessions", 4, "sessions", "declared", later(NOW, -5)))
    mod = source_module()
    want = [Amount("machine", "host.sessions", 1, "sessions")]
    gone = mod.take(reg, runs_root=world.runs / "gone", now=NOW)
    assert gone.holds is None
    with pytest.raises(CheckError) as excinfo:
        mod.decide(gone, want, now=NOW)
    assert excinfo.value.reason == "holds" and "does not exist (pass --root)" in str(excinfo.value)
    kept = mod.take(reg, runs_root=world.runs, now=NOW)
    decision, narrowed = mod.decide(kept, want, now=later(NOW, 60))
    assert (decision.verdict, narrowed) == ("grant", ())
    with pytest.raises(CheckError) as excinfo:
        mod.decide(kept, want, now=later(NOW, 61))
    assert excinfo.value.reason == "holds" and "61 s old; at most 60 s" in str(excinfo.value)
    world.runs = world.runs / "gone"
    code, out, err = world.run(capsys, "capacity", "check", "machine", "host.sessions", "1", "sessions")
    assert code == 2 and out == "" and err.startswith("capacity check refused: ") and "does not exist" in err


# --- R6 a limited provider holds its capacity; the digest stays the record's ---------------------------------------------

def test_a_limited_provider_holds_its_capacity_so_the_decision_waits_and_still_quotes_the_record(world, capsys):
    """Catches a command that ignores the provider state, a hold that changes the digest the decision quotes, a name match
    on a bare prefix (`claudex`), on another provider, or on another kind, and a narrowing that appears with no evidence:
    `provider/claude.sessions` 4, the claude account limited -> exit 3 `wait`/`exhausted`, 4 held, `narrowed` names the
    capacity, the rule and the account, the digest is the file's; `claudex.sessions`, `kimi.sessions` and the machine
    capacity are granted with nothing narrowed; with no parked run the same ask is granted."""
    world.registry(("provider", "claude.sessions", 4, "sessions", "declared"),
                   ("provider", "claudex.sessions", 4, "sessions", "declared"),
                   ("provider", "kimi.sessions", 4, "sessions", "declared"),
                   ("machine", "claude", 4, "sessions", "declared"))
    first = world.check(capsys, "provider", "claude.sessions", "1", "sessions")
    assert (first["code"], first["narrowed"], first["decision"]["verdict"]) == (0, [], "grant")
    world.park("limited")
    got = world.check(capsys, "provider", "claude.sessions", "1", "sessions")
    assert got["code"] == 3 and (got["decision"]["verdict"], got["decision"]["reason"]) == ("wait", "exhausted")
    (line,) = got["decision"]["lines"]
    assert (line["limit"], line["in_use"], line["wanted"]) == (4, 4, 1)
    assert got["narrowed"] == [{"kind": "provider", "name": "claude.sessions", "unit": "sessions", "rule": "FULL",
                                "held": 4, "by": "claude/acct-a is LIMITED"}]
    assert got["decision"]["registry"] == hashlib.sha256(world.record.read_bytes()).hexdigest()
    for kind, name in (("provider", "claudex.sessions"), ("provider", "kimi.sessions"), ("machine", "claude")):
        other = world.check(capsys, kind, name, "1", "sessions")
        assert (other["code"], other["narrowed"], other["decision"]["verdict"]) == (0, [], "grant"), name
    assert world.check(capsys, "provider", "claude", "1", "sessions")["decision"]["reason"] == "unknown"


# --- R7 the table: what each health state holds back ------------------------------------------------------------------

@pytest.mark.parametrize("label, failure, retry, attempts, held", [
    ("LIMITED", N.USAGE_LIMIT, 3600, 0, 5),
    ("GIVEN_UP", N.USAGE_LIMIT, 3600, 5, 5),
    ("DEGRADED", N.PROVIDER_UNAVAILABLE, 3600, 0, 3),
    ("RESET_DUE", N.USAGE_LIMIT, -3600, 0, 3)])
def test_each_health_state_holds_what_its_row_says_and_a_half_is_rounded_up(world, label, failure, retry, attempts, held):
    """Catches a state with the wrong row, a half rounded down, a state that holds nothing, and a table with a row
    missing: against a capacity of 5, LIMITED and GIVEN_UP hold all 5, DEGRADED and RESET_DUE hold 3 (half of 5 rounded
    up); the rows are written out as literals and cover every `HealthState`."""
    from hoh.broker import Amount
    from hoh.health import HealthState
    assert quota.MAX_RESUME_ATTEMPTS == 5
    reg = world.registry(("provider", "claude.sessions", 5, "sessions", "declared", later(NOW, -5)))
    world.park("one", failure=failure, retry=retry_at(retry, NOW), attempts=attempts)
    mod = source_module()
    assert {state.value: rule for state, rule in mod.HOLD_RULE.items()} == {
        "LIMITED": "FULL", "GIVEN_UP": "FULL", "DEGRADED": "HALF", "RESET_DUE": "HALF"}
    assert set(mod.HOLD_RULE) == set(HealthState)
    snap = mod.take(reg, runs_root=world.runs, now=NOW)
    assert pick({"facts": [f.as_dict() for f in mod.facts(snap, now=NOW)]}, "health_state", "claude", "acct-a")[
        "value"] == label
    narrowed = mod.narrowing(snap, [Amount("provider", "claude.sessions", 1, "sessions")], now=NOW)
    assert [(n.held, n.by) for n in narrowed] == [(held, f"claude/acct-a is {label}")]
    safe = next(f for f in mod.facts(snap, now=NOW) if f.field == "observed_safe_cap")
    assert (safe.status, safe.value, safe.why) == ("FRESH", 5 - held, f"claude/acct-a is {label}")
    assert mod.decide(snap, [Amount("provider", "claude.sessions", 5 - held, "sessions")], now=NOW)[0].verdict == "grant"
    assert mod.decide(snap, [Amount("provider", "claude.sessions", 6 - held, "sessions")], now=NOW)[0].verdict == "wait"


# --- R8 an incomplete reading and an unnamed limit -----------------------------------------------------------------------

def test_an_incomplete_reading_holds_every_provider_capacity_and_a_machine_capacity_is_left_alone(world, capsys):
    """Catches a damaged run that hides a limit (fail-open) and a damaged run that narrows a machine capacity: a run whose
    state is not JSON makes the reading incomplete (`problems` 1) and holds all of `provider/kimi.sessions` (5 of 5),
    while `machine/host.sessions` is granted with nothing narrowed."""
    world.registry(("provider", "kimi.sessions", 5, "sessions", "declared"),
                   ("machine", "host.sessions", 4, "sessions", "declared"))
    bad = world.runs / "damaged"
    bad.mkdir()
    (bad / "state.json").write_bytes(b"{")
    got = world.check(capsys, "provider", "kimi.sessions", "1", "sessions")
    assert got["code"] == 3
    assert [(n["name"], n["rule"], n["held"], n["by"]) for n in got["narrowed"]] == [
        ("kimi.sessions", "FULL", 5, "the provider reading is incomplete or UNKNOWN")]
    shown = world.show(capsys)["source"]
    assert pick(shown, "problems")["value"] == 1
    safe = pick(shown, "observed_safe_cap", "kimi.sessions")
    assert (safe["status"], safe["value"], safe["why"]) == ("FRESH", 0, "the provider reading is incomplete or UNKNOWN")
    assert all(f["field"] != "observed_safe_cap" or f["kind"] == "provider" for f in shown["facts"])
    assert world.check(capsys, "machine", "host.sessions", "1", "sessions")["narrowed"] == []


def test_a_pending_limit_that_names_no_provider_holds_half_of_every_provider_capacity(world, capsys):
    """Catches an unnamed pending limit that narrows nothing and a half rounded down: one pending limit with no receipt holds
    3 of 5 (half of 5 rounded up) -- a want of 2 is granted, a want of 3 waits -- and the `unnamed` fact reads 1."""
    world.registry(("provider", "kimi.sessions", 5, "sessions", "declared"))
    world.park("nameless", provider=None)
    two = world.check(capsys, "provider", "kimi.sessions", "2", "sessions")
    assert (two["code"], two["decision"]["verdict"]) == (0, "grant")
    assert [(n["rule"], n["held"], n["by"]) for n in two["narrowed"]] == [("HALF", 3, "1 pending limit(s) name no provider")]
    assert world.check(capsys, "provider", "kimi.sessions", "3", "sessions")["code"] == 3
    shown = world.show(capsys)["source"]
    assert pick(shown, "unnamed")["value"] == 1 and pick(shown, "observed_safe_cap", "kimi.sessions")["value"] == 2


def test_an_unnamed_limit_that_is_due_or_given_up_is_not_pending(world, capsys):
    """Catches an unnamed park counted whether or not it is pending: one whose reset has passed and one the resume loop gave up
    on hold nothing, the `unnamed` fact reads 0."""
    world.registry(("provider", "kimi.sessions", 5, "sessions", "declared"))
    world.park("due", provider=None, retry=retry_at(-3600))
    world.park("gone", provider=None, attempts=5)
    got = world.check(capsys, "provider", "kimi.sessions", "5", "sessions")
    assert (got["code"], got["narrowed"]) == (0, [])
    assert pick(world.show(capsys)["source"], "unnamed")["value"] == 0


# --- R9 a provider reading that is too old ------------------------------------------------------------------------------

def test_a_provider_reading_older_than_five_minutes_holds_every_provider_capacity(world):
    """Catches a stale provider reading that narrows nothing (fail-open): with no parked run at all the reading says
    `problems` 0 and nothing is held, at T+300 still nothing, at T+301 (slots kept fresh) every provider capacity is held
    in full and a machine capacity is not."""
    from hoh.broker import Amount
    reg = world.registry(("provider", "kimi.sessions", 4, "sessions", "declared", later(NOW, -5)),
                         ("machine", "host.sessions", 4, "sessions", "declared", later(NOW, -5)))
    mod = source_module()
    snap = mod.take(reg, runs_root=world.runs, now=NOW, holds_age=10 ** 6)
    wants = [Amount("provider", "kimi.sessions", 1, "sessions"), Amount("machine", "host.sessions", 1, "sessions")]
    assert mod.narrowing(snap, wants, now=NOW) == mod.narrowing(snap, wants, now=later(NOW, 300)) == ()
    safe = {t: next(f for f in mod.facts(snap, now=t) if f.field == "observed_safe_cap") for t in (NOW, later(NOW, 301))}
    assert (safe[NOW].value, safe[NOW].why) == (4, "") and (safe[later(NOW, 301)].value, safe[later(NOW, 301)].why) == (
        0, "the provider reading is incomplete or UNKNOWN")
    (held,) = mod.narrowing(snap, wants, now=later(NOW, 301))
    assert (held.name, held.rule, held.held) == ("kimi.sessions", "FULL", 4)
    decision, narrowed = mod.decide(snap, wants, now=later(NOW, 301))
    assert [(line.name, line.verdict) for line in decision.lines] == [
        ("host.sessions", "grant"), ("kimi.sessions", "wait")] and narrowed == (held,)


# --- R10 / R11 the reading itself ----------------------------------------------------------------------------------------

def test_a_reading_that_raises_is_unknown_not_empty_and_does_not_escape(world, monkeypatch):
    """Catches a provider reading that fails open (no facts, nothing held) or lets its exception out: `read_health` raising
    leaves `problems` and `unnamed` UNKNOWN with the exception's type, take does not raise, and the provider capacity
    is held in full."""
    from hoh.broker import Amount
    reg = world.registry(("provider", "kimi.sessions", 4, "sessions", "declared", later(NOW, -5)))
    mod = source_module()

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "read_health", boom)
    snap = mod.take(reg, runs_root=world.runs, now=NOW)
    got = {f.field: f for f in mod.facts(snap, now=NOW) if f.field in ("problems", "unnamed")}
    assert [(f.status, f.value) for f in got.values()] == [("UNKNOWN", None)] * 2
    assert all(f.why.startswith("the provider waits cannot be read (RuntimeError): boom") for f in got.values())
    (held,) = mod.narrowing(snap, [Amount("provider", "kimi.sessions", 1, "sessions")], now=NOW)
    assert (held.rule, held.held) == ("FULL", 4)


@pytest.mark.parametrize("groups, listed, complete", [(256, 256, True), (257, 256, False)])
def test_at_most_256_provider_groups_are_listed_and_more_is_an_unknown_reading(world, monkeypatch, groups, listed, complete):
    """Catches an unbounded listing and a silent cut: 256 groups are listed and the reading is whole; 257 list 256 and the
    reading is UNKNOWN (`more than 256 provider groups`)."""
    from hoh.health import HealthReport, HealthState, ProviderHealth
    reg = world.registry(("machine", "host.sessions", 4, "sessions", "declared", later(NOW, -5)))
    mod = source_module()
    report = HealthReport(at=NOW, providers=tuple(
        ProviderHealth(provider=f"p{n:03d}", account_handle="a", state=HealthState.LIMITED, not_before=None,
                       runs=("r",), failures=()) for n in range(groups)), unattributed=(), problems=(), runs_seen=0)
    monkeypatch.setattr(mod, "read_health", lambda *args, **kwargs: report)
    snap = mod.take(reg, runs_root=world.runs, now=NOW)
    got = mod.facts(snap, now=NOW)
    assert sum(1 for f in got if f.field == "health_state") == listed
    problems = next(f for f in got if f.field == "problems")
    assert (problems.status == "FRESH") is complete
    if not complete:
        assert problems.why == "more than 256 provider groups: not all listed"


# --- R12 read-only, bounded, ASCII ----------------------------------------------------------------------------------------

def test_the_source_writes_nothing_and_what_it_prints_is_ascii_escaped_and_bounded(world, capsys):
    """Catches a snapshot or a decision that writes, locks or creates anything, an unescaped provider name, and a reason
    that is not bounded: the state root and the runs root keep every name, byte and mtime through `show --source` and
    `check`; a provider named with a diaeresis, an escape sequence and a newline is printed as ASCII JSON that round-trips
    to the same name; the reason of a runs root named with 200 characters is cut at 220."""
    world.registry(("provider", "kimi.sessions", 4, "sessions", "declared"))
    name = "cl\u00e4ude\x1b[31m\nx"
    world.park("odd", provider=name)
    before = (fingerprint(world.state), fingerprint(world.runs))
    shown = world.show(capsys)["source"]
    world.check(capsys, "provider", "kimi.sessions", "1", "sessions")
    assert (fingerprint(world.state), fingerprint(world.runs)) == before
    assert pick(shown, "health_state", name, "acct-a")["value"] == "LIMITED"
    assert all(f["why"] == "" for f in shown["facts"])
    world.runs = world.runs / ("r" * 200)
    long = [f for f in world.show(capsys)["source"]["facts"] if f["family"] == "slots"][0]["why"]
    assert len(long) == 220 and long.startswith("capacity check: the runs root 'rrr")


# --- R13 the pieces are exact ------------------------------------------------------------------------------------------------

def test_the_constants_the_wiring_and_the_purity_of_the_module_are_exact():
    """Catches a changed bound or table size, a module that reads a clock or writes, and a command that is not wired: the
    five numbers as literals, `show --source` in the parser and its help, and a scan of the module's own source for a clock,
    `open`, a write or a lock."""
    mod = source_module()
    assert (mod.HEALTH_AGE, mod.HOLDS_AGE, mod.MAX_GROUPS, mod.MAX_WHY) == (300, 60, 256, 220)
    assert (mod.FRESH, mod.UNKNOWN, mod.FULL, mod.HALF, mod.UNNAMED_RULE) == ("FRESH", "UNKNOWN", "FULL", "HALF", "HALF")
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0}
    assert imported <= {"__future__", "collections", "dataclasses", "datetime"}
    called = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
              for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not called & {"now", "utcnow", "today", "time", "open", "write", "write_text", "write_bytes", "flock", "mkdir"}
    parser = cli.build_parser()
    namespace = parser.parse_args(["capacity", "show", "--source"])
    assert namespace.source is True and namespace.func is cli.cmd_capacity_show
    assert parser.parse_args(["capacity", "show"]).source is False
    with contextlib.redirect_stdout(io.StringIO()) as out, pytest.raises(SystemExit):
        parser.parse_args(["capacity", "show", "--help"])
    assert "--source" in out.getvalue() and "UNKNOWN" in out.getvalue()


def test_the_safe_cap_of_a_provider_cap_that_is_stale_is_unknown_and_holds_nothing_back_by_itself(world):
    """Catches a safe cap derived from a number that is itself UNKNOWN: a declared provider cap older than its bound has an
    `observed_safe_cap` that is UNKNOWN with no value and the reason `the cap is UNKNOWN`; a fresh one beside it is not
    affected."""
    reg = world.registry(("provider", "kimi.sessions", 4, "sessions", "declared", later(NOW, -101)),
                         ("provider", "codex.sessions", 4, "sessions", "declared", later(NOW, -100)))
    mod = source_module()
    snap = mod.take(reg, runs_root=world.runs, now=NOW, policy=policy(100, 50))
    safe = {f.name: f for f in mod.facts(snap, now=NOW) if f.field == "observed_safe_cap"}
    assert [(f.status, f.value, f.why) for f in (safe["kimi.sessions"], safe["codex.sessions"])] == [
        ("UNKNOWN", None, "the cap is UNKNOWN"), ("FRESH", 4, "")]


@pytest.mark.parametrize("kind", ["a file", "a fifo", "a dangling link", "a directory nobody may read"])
def test_a_runs_root_that_is_not_a_readable_directory_is_unknown_never_empty(world, capsys, kind):
    """Catches an odd runs root read as "no runs, nothing held" (fail-open): a regular file, a FIFO (under an alarm), a
    dangling link and a directory with mode 000 (skipped as root) make both slot counts UNKNOWN with a reason, `complete`
    false and, through `check`, no decision at all -- exit 2, nothing on stdout."""
    import signal
    odd = world.runs.parent / "odd"
    if kind == "a file":
        odd.write_bytes(b"x")
    elif kind == "a fifo":
        os.mkfifo(odd)
    elif kind == "a dangling link":
        odd.symlink_to(world.runs.parent / "nowhere")
    else:
        if os.geteuid() == 0:
            pytest.skip("root reads every directory")
        odd.mkdir()
        odd.chmod(0)
    world.registry(("machine", "host.sessions", 4, "sessions", "declared"))
    world.runs = odd
    old_handler = signal.signal(signal.SIGALRM, lambda *args: pytest.fail("blocked on the runs root"))
    signal.alarm(20)
    try:
        shown = world.show(capsys)["source"]
        code, out, err = world.run(capsys, "capacity", "check", "machine", "host.sessions", "1", "sessions")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        if kind == "a directory nobody may read":
            odd.chmod(0o700)
    assert shown["complete"] is False
    for field, name in (("active", "host.sessions"), ("reserved", "dispatches.reserved")):
        fact = pick(shown, field, name)
        assert (fact["status"], fact["value"]) == ("UNKNOWN", None) and fact["why"].startswith("capacity check: ")
    assert code == 2 and out == "" and err.startswith("capacity check refused: ")


@pytest.mark.parametrize("label, parks, expect", [
    ("a pending nameless limit (HALF) before a limited claude account (FULL)",
     [("nameless", {"provider": None}), ("lim", {})], "claude/acct-a is LIMITED"),
    ("a degraded account (HALF) before a limited account (FULL) of one provider",
     [("deg", {"failure": N.PROVIDER_UNAVAILABLE, "account": "acct-a"}), ("lim", {"account": "acct-b"})],
     "claude/acct-b is LIMITED"),
    ("a limited account (FULL) before a degraded account (HALF) -- the control",
     [("lim", {"account": "acct-a"}), ("deg", {"failure": N.PROVIDER_UNAVAILABLE, "account": "acct-b"})],
     "claude/acct-a is LIMITED")])
def test_the_largest_rule_wins_when_a_half_and_a_full_cause_meet_and_narrowed_is_in_name_order(world, capsys, label, parks, expect):
    """Catches a narrowing that takes the first cause instead of the largest (fail-open: a limited provider's capacity is
    granted): a HALF cause is listed before the FULL one in two realistic cases, and the control has the FULL first. Through
    `narrowing` and `decide` and through `hoh capacity check`: the rule is FULL, all 5 of 5 are held, the cause named is the
    FULL one, the answer is `wait` (exit 3) even for a want of 1. `narrowed` is in (kind, name) order whatever order the wants
    are given in (two provider capacities, the wants reversed)."""
    from hoh.broker import Amount
    mod = source_module()
    for run_id, kwargs in parks:
        world.park(run_id, **kwargs)
    now = utc()
    reg = world.registry(("provider", "claude.sessions", 5, "sessions", "declared"),
                         ("provider", "claude.alpha", 4, "sessions", "declared"))
    snap = mod.take(reg, runs_root=world.runs, now=now)
    asks = [Amount("provider", "claude.sessions", 1, "sessions"), Amount("provider", "claude.alpha", 1, "sessions")]
    narrowed = mod.narrowing(snap, asks, now=now)
    assert [(n.name, n.rule, n.held, n.by) for n in narrowed] == [
        ("claude.alpha", "FULL", 4, expect), ("claude.sessions", "FULL", 5, expect)]
    decision, again = mod.decide(snap, asks[:1], now=now)
    assert (decision.verdict, decision.reason, decision.lines[0].in_use, again) == ("wait", "exhausted", 5, narrowed[1:])
    got = world.check(capsys, "provider", "claude.sessions", "1", "sessions")
    assert got["code"] == 3 and [(n["rule"], n["held"], n["by"]) for n in got["narrowed"]] == [("FULL", 5, expect)]
