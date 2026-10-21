"""P3-17 slice 2: `hoh capacity check` -- the capacity broker's first production caller.

The command reads the capacity registry of a state root (P3-16 slice 2), the live holds the product records under a
runs root (run leases of every project, open budget reservations), takes the clock and asks `broker.decide`. Every
test that drives it goes through `hoh.cli.main`, against a state root `StorageRegistry.init()` made under `tmp_path`
and a runs root of its own, and names what it catches. The broker's own judgement is pinned by `tests/test_broker.py`;
this file pins what the caller adds: which records count as holds and under which names, that an unreadable record
is a refusal and never a hold of zero, where the ages come from, the exit codes and the printed form.

Hermetic: `HOH_STATE_ROOT` is removed and `HOME` / `XDG_STATE_HOME` point below `tmp_path`; nothing reads `~/.agents`,
starts Herdr or an agent, or opens the network. A FIFO runs under an alarm. Leases are made with the real
`RunLeases` (the holder is this test process, so a fresh one is LIVE); a LOST lease is one whose recorded clock was in
2020, so it ran out; a RELEASED one is released.
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import hashlib
import json
import os
import signal
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hoh import cli

FILE = "capacity-registry.json"
TIME = "%Y-%m-%dT%H:%M:%SZ"


def _capacity():
    import importlib
    return importlib.import_module("hoh.capacity")


def _check():
    import importlib
    return importlib.import_module("hoh.capacitycheck")


@pytest.fixture(autouse=True)
def hermetic_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("HOH_STATE_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))


@contextlib.contextmanager
def alarm(seconds: int = 20):
    def fire(signum, frame):
        raise AssertionError(f"blocked for {seconds}s")
    old = signal.signal(signal.SIGALRM, fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def utc(delta_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).strftime(TIME)


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

    def registry(self, *entries) -> None:
        capacity = _capacity()
        made = [capacity.Entry(kind=k, name=n, limit=l, unit=u, source=s, as_of=a if a else utc())
                for (k, n, l, u, s, *rest) in entries for a in [rest[0] if rest else None]]
        capacity.write(self.record, made)

    def lease(self, project: str, run_id: str, how: str = "live"):
        from hoh.runlease import RunLeases
        directory = self.runs / "projects" / project / "run-leases"
        if how == "lost":                  # granted in 2020 for an hour: it ran out
            leases = RunLeases(directory, clock=lambda: "2020-01-01T00:00:00Z")
        else:
            leases = RunLeases(directory)
        taken = leases.acquire(run_id, "holder", ttl_seconds=3600)
        if how == "released":
            leases.release(taken, reason="done")
        return directory / f"{run_id}.json"

    def reserve(self, run_id: str, amount: int, ceiling: int = 100):
        from hoh.reservation import BudgetReservations
        books = BudgetReservations(self.runs)
        books.reserve(run_id, ceiling=ceiling, spent=lambda: {}, want=amount)
        return books

    def check(self, capsys, kind, name, amount, unit, *extra, runs=None):
        argv = ["--root", str(runs or self.runs), "capacity", "check", kind, name, amount, unit,
                "--storage-root", str(self.state), *extra]
        try:
            code = cli.main(argv)
        except SystemExit as stop:
            code = stop.code
        seen = capsys.readouterr()
        return code, seen.out, seen.err


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def verdict(result) -> dict:
    code, out, err = result
    assert err == "", err
    printed = json.loads(out)
    assert out.isascii()
    return {"code": code, **printed}


def refused(result) -> str:
    code, out, err = result
    assert code == 2 and out == "", (code, out, err)
    assert err.startswith("capacity check refused: ") and err.endswith("\n") and err.count("\n") == 1, err
    return err


def fingerprint(root: Path) -> dict:
    seen = {}
    for path in sorted(root.rglob("*")):
        info = os.lstat(path)
        body = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        seen[str(path.relative_to(root))] = (info.st_mode, info.st_size, info.st_mtime_ns, body)
    return seen


SESSIONS = ("machine", "host.sessions", "sessions")
DISPATCHES = ("provider", "dispatches.reserved", "requests")


# --- R1 one decision, through the real command ---------------------------------------------------------------------

def test_a_grant_is_a_decision_of_the_registry_quoting_its_digest_through_the_real_command(world, capsys):
    """Catches a command that does not call the broker, judges something other than the record on disk, drops the
    digest, the policy or the line, or exits non-zero for a grant: registry `machine/host.sessions` 4 declared, one
    wanted, nothing held -> exit 0, `grant`/`ok`, the line says limit 4, wanted 1, in_use 0, the decision quotes
    the sha256 of the file, `now` lies between the clock before and after, and the policy printed is the default."""
    world.registry(("machine", "host.sessions", 4, "sessions", "declared"))
    before = datetime.now(timezone.utc).replace(microsecond=0)
    got = verdict(world.check(capsys, "machine", "host.sessions", "1", "sessions"))
    after = datetime.now(timezone.utc)
    assert got["code"] == 0
    decision = got["decision"]
    assert (decision["verdict"], decision["reason"]) == ("grant", "ok")
    assert decision["registry"] == hashlib.sha256(world.record.read_bytes()).hexdigest()
    assert before <= datetime.strptime(decision["now"], TIME).replace(tzinfo=timezone.utc) <= after
    (line,) = decision["lines"]
    assert (line["kind"], line["name"], line["unit"], line["wanted"], line["limit"], line["in_use"]) == (
        "machine", "host.sessions", "sessions", 1, 4, 0)
    assert got["policy"] == {"declared": 604800, "measured": 300}
    assert got["storage_root"] == str(world.state) and got["path"] == str(world.record)
    assert got["holds"]["sessions"] == 0 and got["holds"]["dispatches"] == 0
    assert got["holds"]["runs_root"] == str(world.runs)


# --- R2 the holds -------------------------------------------------------------------------------------------------

def test_live_run_leases_of_every_project_are_sessions_held_and_nothing_else_is(world, capsys):
    """Catches a holds source that counts a lease that is not live, forgets a project, counts a parked copy or the
    lock file, or counts nothing: three LIVE leases in two projects, a LOST, a RELEASED, a parked copy and the lock
    file -> exactly three sessions are held; limit 4 grants one more, limit 3 waits (`exhausted`, exit 3), and a
    want above the limit is refused (`exceeds`, exit 4) whatever is held."""
    world.lease("alpha", "r1")
    world.lease("alpha", "r2")
    world.lease("beta", "r3")
    world.lease("alpha", "r4", "lost")
    world.lease("beta", "r5", "released")
    directory = world.runs / "projects" / "alpha" / "run-leases"
    (directory / "r1.json.g1").write_bytes((directory / "r1.json").read_bytes())
    assert (directory / ".lock").exists()
    world.registry(("machine", "host.sessions", 4, "sessions", "declared"))
    got = verdict(world.check(capsys, "machine", "host.sessions", "1", "sessions"))
    assert got["code"] == 0 and got["holds"]["sessions"] == 3 and got["holds"]["projects"] == 2
    assert got["holds"]["lease_files"] == 5 and got["decision"]["lines"][0]["in_use"] == 3
    world.registry(("machine", "host.sessions", 3, "sessions", "declared"))
    got = verdict(world.check(capsys, "machine", "host.sessions", "1", "sessions"))
    assert got["code"] == 3 and (got["decision"]["verdict"], got["decision"]["reason"]) == ("wait", "exhausted")
    got = verdict(world.check(capsys, "machine", "host.sessions", "4", "sessions"))
    assert got["code"] == 4 and (got["decision"]["verdict"], got["decision"]["reason"]) == ("refuse", "exceeds")
    got = verdict(world.check(capsys, "machine", "host.sessions", "0", "sessions"))
    assert got["code"] == 0                    # nothing still fits beside a full capacity: 3 + 0 is not above 3


def test_open_budget_reservations_are_dispatches_held_and_settled_ones_are_not(world, capsys):
    """Catches a holds source that counts a settled or superseded reservation, counts the ceiling or the spend
    instead of the open amount, or attributes the dispatches to the sessions: two runs hold 7 and 5, a third
    held 9 and was settled, the first was reserved again (4, superseding 7) -> 9 are held on
    `provider/dispatches.reserved`; limit 12 grants 3 and waits at 4; the sessions line stays at nothing held."""
    world.reserve("r1", 7)
    world.reserve("r2", 5)
    books = world.reserve("r3", 9)
    assert books.settle("r3", "done") == 1
    world.reserve("r1", 4)
    world.registry(("provider", "dispatches.reserved", 12, "requests", "declared"),
                   ("machine", "host.sessions", 2, "sessions", "declared"))
    got = verdict(world.check(capsys, "provider", "dispatches.reserved", "3", "requests"))
    assert got["code"] == 0 and got["holds"]["dispatches"] == 9 and got["decision"]["lines"][0]["in_use"] == 9
    assert got["holds"]["sessions"] == 0
    got = verdict(world.check(capsys, "provider", "dispatches.reserved", "4", "requests"))
    assert got["code"] == 3 and got["decision"]["reason"] == "exhausted"
    got = verdict(world.check(capsys, "machine", "host.sessions", "2", "sessions"))
    assert got["code"] == 0 and got["decision"]["lines"][0]["in_use"] == 0


def test_a_capacity_with_no_attributed_hold_is_judged_beside_nothing_and_a_wrong_unit_is_refused(world, capsys):
    """Catches holds smeared over every capacity (a provider limit eaten by sessions) and a hold in a unit the
    registry does not use: `provider/anthropic` sees no hold (the attribution names two capacities only); a registry
    that records `machine/host.sessions` in `items` makes the live lease a `unit` refusal, not a silent grant."""
    world.lease("alpha", "r1")
    world.registry(("provider", "anthropic", 1, "sessions", "declared"),
                   ("machine", "host.sessions", 9, "items", "declared"))
    got = verdict(world.check(capsys, "provider", "anthropic", "1", "sessions"))
    assert got["code"] == 0 and got["decision"]["lines"][0]["in_use"] == 0
    got = verdict(world.check(capsys, "machine", "host.sessions", "1", "items"))
    assert got["code"] == 4 and got["decision"]["reason"] == "unit"


# --- R3 an unknown hold is a refusal ----------------------------------------------------------------------------------

def _spoil(world: World, label: str) -> None:
    alpha = world.runs / "projects" / "alpha"
    leases = alpha / "run-leases"
    if label == "corrupt lease":
        world.lease("alpha", "r1")
        (leases / "r1.json").write_bytes(b"{not json")
    elif label == "oversize lease":
        world.lease("alpha", "r1")
        (leases / "r1.json").write_bytes(b" " * 5000)
    elif label == "lease for another run":
        world.lease("alpha", "r1")
        (leases / "r2.json").write_bytes((leases / "r1.json").read_bytes())
    elif label == "lease directory is a link":
        elsewhere = world.runs.parent / "elsewhere"
        elsewhere.mkdir()
        alpha.mkdir(parents=True)
        (alpha / "run-leases").symlink_to(elsewhere)
    elif label == "lease file is a FIFO":
        leases.mkdir(parents=True)
        os.mkfifo(leases / "r1.json")
    elif label == "lease file is a link":
        world.lease("alpha", "r1")
        (leases / "r9.json").symlink_to(leases / "r1.json")
    elif label == "a file that is not a run id":
        leases.mkdir(parents=True)
        (leases / "bad name.json").write_bytes(b"{}")
    elif label == "projects is a file":
        (world.runs / "projects").write_bytes(b"x")
    elif label == "corrupt ledger":
        (world.runs / "_budget-reservations.json").write_bytes(b"{not json")
    elif label == "ledger is a directory":
        (world.runs / "_budget-reservations.json").mkdir()
    elif label == "ledger is a link":
        (world.runs / "elsewhere.json").write_bytes(b"{}")
        (world.runs / "_budget-reservations.json").symlink_to(world.runs / "elsewhere.json")
    else:
        raise AssertionError(label)


SPOILED = ["corrupt lease", "oversize lease", "lease for another run", "lease directory is a link",
           "lease file is a FIFO", "lease file is a link", "a file that is not a run id", "projects is a file",
           "corrupt ledger", "ledger is a directory", "ledger is a link"]


@pytest.mark.parametrize("label", SPOILED)
def test_a_hold_that_cannot_be_read_is_a_refusal_never_a_hold_of_zero(world, capsys, label):
    """Catches a holds source that skips what it cannot read (the most dangerous reading: a limit that looks
    free): each spoiled record makes the command exit 2 with the refusal text and nothing on stdout, under an
    alarm (a FIFO must not block), and the records are untouched. The control, the same world without the spoiled
    record, grants."""
    world.registry(("machine", "host.sessions", 4, "sessions", "declared"))
    assert verdict(world.check(capsys, "machine", "host.sessions", "1", "sessions"))["code"] == 0
    _spoil(world, label)
    before = fingerprint(world.runs)
    with alarm():
        text = refused(world.check(capsys, "machine", "host.sessions", "1", "sessions"))
    assert "capacity check: " in text
    assert fingerprint(world.runs) == before


def test_the_runs_root_must_be_a_directory_and_an_absent_projects_directory_is_no_leases(world, capsys, tmp_path):
    """Catches a mistyped `--root` that reads as an empty, fully free host: a runs root that does not exist or is
    a file is refused; a real runs root with no `projects` directory holds nothing (explicit control)."""
    world.registry(("machine", "host.sessions", 1, "sessions", "declared"))
    text = refused(world.check(capsys, "machine", "host.sessions", "1", "sessions", runs=tmp_path / "typo"))
    assert "does not exist" in text
    plain = tmp_path / "a-file"
    plain.write_bytes(b"x")
    assert "not a directory" in refused(world.check(capsys, "machine", "host.sessions", "1", "sessions", runs=plain))
    assert verdict(world.check(capsys, "machine", "host.sessions", "1", "sessions"))["holds"]["projects"] == 0


def _leases_in(directory: Path, count: int, *, released: bool, first: int = 0) -> None:
    """`count` lease records in `directory`, made by the real module and copied under other run ids (cheap)."""
    from hoh.runlease import RunLeases, render
    seed = RunLeases(directory).acquire(f"r{first:05d}", "holder", ttl_seconds=3600)
    if released:
        seed = RunLeases(directory).release(seed, reason="done")
    for n in range(first, first + count):
        (directory / f"r{n:05d}.json").write_bytes(render(dataclasses.replace(seed, run_id=f"r{n:05d}")))


def test_what_is_live_is_capped_at_1024_and_projects_at_256(world, capsys):
    """Catches a moved cap: 256 project directories are read and 257 refused; 1024 LIVE leases in one project are read as
    1024 sessions and a 1025th live lease is refused ("live run leases: more than 1024"). The literals are the
    spec's, not the module's constants."""
    world.registry(("machine", "host.sessions", 5000, "sessions", "declared"))
    projects = world.runs / "projects"
    projects.mkdir()
    for n in range(256):
        (projects / f"p{n:03d}").mkdir()
    assert verdict(world.check(capsys, "machine", "host.sessions", "1", "sessions"))["code"] == 0
    (projects / "p256").mkdir()
    assert "project directories: more than 256" in refused(
        world.check(capsys, "machine", "host.sessions", "1", "sessions"))
    runs = world.runs.parent / "runs2"
    directory = runs / "projects" / "only" / "run-leases"
    _leases_in(directory, 1024, released=False)
    got = verdict(world.check(capsys, "machine", "host.sessions", "1", "sessions", runs=runs))
    assert got["holds"]["sessions"] == 1024 and got["holds"]["lease_files"] == 1024
    _leases_in(directory, 1, released=False, first=1024)
    assert "live run leases: more than 1024" in refused(
        world.check(capsys, "machine", "host.sessions", "1", "sessions", runs=runs))


def test_released_history_is_not_a_hold_and_the_scan_has_its_own_ceiling_of_16384_files(world, capsys):
    """Catches a bound that fails closed for ever on a busy host (lease files are never removed): 1100 RELEASED leases
    and two live ones in one project are read, not refused, and hold 2; 16384 released files are read; 16385 `.json`
    names are refused before any is read, with their own message that says why (the files are history)."""
    world.registry(("machine", "host.sessions", 10, "sessions", "declared"))
    directory = world.runs / "projects" / "busy" / "run-leases"
    _leases_in(directory, 1100, released=True)
    _leases_in(directory, 2, released=False, first=2000)
    got = verdict(world.check(capsys, "machine", "host.sessions", "1", "sessions"))
    assert got["code"] == 0 and got["holds"]["sessions"] == 2 and got["holds"]["lease_files"] == 1102
    big = world.runs.parent / "big"
    _leases_in(big / "projects" / "a" / "run-leases", 16384, released=True)
    got = verdict(world.check(capsys, "machine", "host.sessions", "1", "sessions", runs=big))
    assert got["holds"]["sessions"] == 0 and got["holds"]["lease_files"] == 16384
    (big / "projects" / "b" / "run-leases").mkdir(parents=True)
    (big / "projects" / "b" / "run-leases" / "x.json").write_bytes(b"")      # one more, and unreadable
    text = refused(world.check(capsys, "machine", "host.sessions", "1", "sessions", runs=big))
    assert "lease files: more than 16384" in text and "released leases are never removed" in text


# --- R4 where the ages come from ------------------------------------------------------------------------------------

def test_the_ages_are_the_operators_defaults_a_week_and_five_minutes_and_the_flags_reach_the_policy(world, capsys):
    """Catches ages that are ignored, swapped between sources or not forwarded: a declared limit 10 days old is
    `stale` by default (exit 4) and a grant with `--max-age-declared 1000000`; a measured figure 10 minutes old is
    `stale` by default and a grant with `--max-age-measured 3600`; a declared 10-minute-old limit is fresh by
    default (the declared age is not the measured one), and the printed policy is the one used."""
    world.registry(("provider", "old.declared", 5, "sessions", "declared", utc(-10 * 86400)),
                   ("machine", "old.measured", 5, "sessions", "measured", utc(-600)),
                   ("provider", "recent.declared", 5, "sessions", "declared", utc(-600)))
    got = verdict(world.check(capsys, "provider", "old.declared", "1", "sessions"))
    assert got["code"] == 4 and got["decision"]["reason"] == "stale" and got["policy"]["declared"] == 604800
    got = verdict(world.check(capsys, "provider", "old.declared", "1", "sessions", "--max-age-declared", "1000000"))
    assert got["code"] == 0 and got["policy"] == {"declared": 1000000, "measured": 300}
    got = verdict(world.check(capsys, "machine", "old.measured", "1", "sessions"))
    assert got["code"] == 4 and got["decision"]["reason"] == "stale"
    got = verdict(world.check(capsys, "machine", "old.measured", "1", "sessions", "--max-age-measured", "3600"))
    assert got["code"] == 0 and got["policy"] == {"declared": 604800, "measured": 3600}
    got = verdict(world.check(capsys, "provider", "recent.declared", "1", "sessions"))
    assert got["code"] == 0
    got = verdict(world.check(capsys, "provider", "recent.declared", "1", "sessions", "--max-age-declared", "0"))
    assert got["code"] == 4 and got["decision"]["reason"] == "stale"


def test_the_age_boundary_is_exact_and_a_bad_age_or_amount_is_a_refusal(world, capsys):
    """Catches an off-by-one in the policy and a coercing parser: through `capacitycheck.check` with an injected
    `now`, a fact exactly as old as its age is fresh and one second older is `stale`; the command refuses ages
    and amounts that are not plain whole numbers (`-1`, `1.5`, `x`, a leading zero, a space, 17 digits) and an age
    above 366 days, with nothing printed."""
    capacity, check = _capacity(), _check()
    entry = capacity.Entry(kind="provider", name="a", limit=2, unit="sessions", source="declared",
                           as_of="2026-10-01T00:00:00Z")
    registry = capacity.parse(capacity.render([entry]))
    from hoh.broker import Amount
    want = [Amount("provider", "a", 1, "sessions")]
    fresh = check.check(registry, want, runs_root=world.runs, now="2026-10-01T00:01:40Z",
                        policy_=check.policy(declared=100, measured=1))[0]
    late = check.check(registry, want, runs_root=world.runs, now="2026-10-01T00:01:41Z",
                       policy_=check.policy(declared=100, measured=1))[0]
    assert (fresh.verdict, fresh.reason) == ("grant", "ok") and (late.verdict, late.reason) == ("refuse", "stale")
    world.registry(("provider", "a", 2, "sessions", "declared"))
    for bad in ("-1", "1.5", "x", "01", " 1", "1 ", "9" * 17, "31622401"):
        refused(world.check(capsys, "provider", "a", "1", "sessions", "--max-age-declared", bad))
        refused(world.check(capsys, "provider", "a", "1", "sessions", "--max-age-measured", bad))
    for bad in ("0x1", "-1", "1e1", "01", " 1", "1.0", ""):
        refused(world.check(capsys, "provider", "a", bad, "sessions"))
    assert verdict(world.check(capsys, "provider", "a", "1", "sessions",
                               "--max-age-declared", "31622400"))["policy"]["declared"] == 31622400


# --- R5 the registry and the root -----------------------------------------------------------------------------------

def test_an_absent_or_unreadable_registry_and_a_foreign_root_are_refusals_and_an_unknown_capacity_is_a_refuse(
        world, capsys, tmp_path):
    """Catches a decision taken without a registry: no record, a corrupt record, an uninitialised root are exit 2
    (the P3-16 command's own refusals), the registry is not created; a capacity nobody recorded is a decision --
    `refuse`/`unknown`, exit 4 -- never a grant."""
    assert "does not exist" in refused(world.check(capsys, "machine", "host.sessions", "1", "sessions"))
    assert not world.record.exists()
    world.registry(("machine", "host.sessions", 1, "sessions", "declared"))
    got = verdict(world.check(capsys, "machine", "nothing.here", "1", "sessions"))
    assert got["code"] == 4 and got["decision"]["reason"] == "unknown" and got["decision"]["lines"][0]["limit"] is None
    world.record.write_bytes(b"{not json")
    refused(world.check(capsys, "machine", "host.sessions", "1", "sessions"))
    bare = tmp_path / "bare"
    bare.mkdir()
    code = cli.main(["--root", str(world.runs), "capacity", "check", "machine", "host.sessions", "1", "sessions",
                     "--storage-root", str(bare)])
    seen = capsys.readouterr()
    assert code == 2 and seen.out == "" and "storage state root is not initialised" in seen.err
    assert list(bare.iterdir()) == []


def test_the_command_writes_nothing_and_what_it_prints_is_ascii_and_escaped(world, capsys, tmp_path):
    """Catches a read-only command that creates a lock, a temp file or a record, and a path that reaches the
    terminal raw: every file and mtime of the state root and the runs root is as it was after a grant, a wait and
    a refusal; a runs root whose name holds an escape sequence and a newline is printed as ASCII JSON that
    round-trips and its refusal as one escaped line."""
    world.lease("alpha", "r1")
    world.reserve("r2", 3)
    world.registry(("machine", "host.sessions", 1, "sessions", "declared"))
    before = (fingerprint(world.state), fingerprint(world.runs))
    for amount in ("1", "0", "5"):
        assert world.check(capsys, "machine", "host.sessions", amount, "sessions")[0] in (0, 3, 4)
    refused(world.check(capsys, "machine", "host.sessions", "x", "sessions"))
    assert (fingerprint(world.state), fingerprint(world.runs)) == before
    odd = tmp_path / "ru\x1b[31m\nns\udcff"
    odd.mkdir()
    got = verdict(world.check(capsys, "machine", "host.sessions", "1", "sessions", runs=odd))
    assert got["holds"]["runs_root"] == str(odd) and got["code"] == 0
    text = refused(world.check(capsys, "machine", "host.sessions", "1", "sessions", runs=tmp_path / "gone\x1b\n"))
    assert text.isascii() and "\x1b" not in text and "\\x1b" in text


def test_a_link_a_stray_file_or_a_link_to_a_project_is_refused_and_a_link_to_the_runs_root_is_followed(
        world, capsys, tmp_path):
    """Catches a scan that follows or skips what is not a directory of its own: a stray regular file in `projects/`
    and a project directory that is a link are refused ("not a directory of its own"; a link would double count
    a project); a runs root that is a link to a real one is followed (as `ProjectStore` resolves its root) and
    counts the same holds; a dangling link is "does not exist"."""
    world.registry(("machine", "host.sessions", 5, "sessions", "declared"))
    world.lease("alpha", "r1")
    link = tmp_path / "runs-link"
    link.symlink_to(world.runs)
    got = verdict(world.check(capsys, "machine", "host.sessions", "1", "sessions", runs=link))
    assert got["holds"]["sessions"] == 1 and got["holds"]["runs_root"] == str(world.runs)
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "nowhere")
    assert "does not exist" in refused(world.check(capsys, "machine", "host.sessions", "1", "sessions", runs=dangling))
    (world.runs / "projects" / "stray.txt").write_bytes(b"x")
    text = refused(world.check(capsys, "machine", "host.sessions", "1", "sessions"))
    assert "project directory 'stray.txt' is not a directory of its own" in text
    (world.runs / "projects" / "stray.txt").rename(tmp_path / "stray-moved.txt")
    (world.runs / "projects" / "beta").symlink_to(world.runs / "projects" / "alpha")
    text = refused(world.check(capsys, "machine", "host.sessions", "1", "sessions"))
    assert "project directory 'beta' is not a directory of its own" in text


def test_nothing_is_created_on_an_empty_host_either(world, capsys):
    """Catches a read-only command that makes a directory or a marker where a record is absent (the production state
    today): a runs root with no `projects`, one with project directories that have no `run-leases`, and one with
    no ledger are each left byte-for-byte and mtime-for-mtime as they were, and so is the state root."""
    world.registry(("machine", "host.sessions", 2, "sessions", "declared"))
    for layout in ("empty", "projects without leases", "stale project"):
        runs = world.runs.parent / f"runs-{layout.replace(' ', '-')}"
        runs.mkdir()
        if layout != "empty":
            (runs / "projects" / "p1").mkdir(parents=True)
            (runs / "projects" / "p2").mkdir()
        if layout == "stale project":
            (runs / "projects" / "p3" / "run-leases").mkdir(parents=True)
        before = (fingerprint(runs), fingerprint(world.state))
        got = verdict(world.check(capsys, "machine", "host.sessions", "1", "sessions", runs=runs))
        assert got["code"] == 0 and got["holds"]["sessions"] == 0 and got["holds"]["dispatches"] == 0
        assert (fingerprint(runs), fingerprint(world.state)) == before, layout


def test_now_is_taken_after_the_registry_was_read(world, capsys, monkeypatch):
    """Catches a clock read before the registry: the registry is replaced by a concurrent `hoh capacity set` while it is
    being read (here: its read takes 1.1 s and the file it returns was written at the end of it), so a `now` taken
    before the read would be older than the fact and the decision `refuse`/`future` (broker.decide: a fact written
    after `now` is `future`). Taken after, it is a grant.

    Independent of the wall clock's position in the second and of any stall of the process: the fact is stamped with
    the whole second after the one the read began in, which is later than a `now` taken before the read began and, the
    read sleeping 1.1 s, never later than a `now` taken after it. The age is therefore not pinned to a value (it is
    the stall between the write and the clock read, in whole seconds); what is pinned is that it is not negative."""
    import time
    capacity = _capacity()
    world.registry(("machine", "host.sessions", 2, "sessions", "declared", utc(-3600)))
    real = capacity.read

    def slow(path):
        written = utc(1)
        time.sleep(1.1)
        world.registry(("machine", "host.sessions", 2, "sessions", "declared", written))
        return real(path)
    monkeypatch.setattr(capacity, "read", slow)
    got = verdict(world.check(capsys, "machine", "host.sessions", "1", "sessions"))
    assert got["code"] == 0 and got["decision"]["reason"] == "ok"
    assert got["decision"]["lines"][0]["age"] >= 0


# --- R6 wiring and the module ---------------------------------------------------------------------------------------

def test_the_command_is_wired_and_the_exit_codes_are_what_the_help_says(capsys):
    """Catches a command that is not reachable from `hoh`: the parser maps `capacity check` to the function
    `cli.main` calls, the group still has `set` and `show`, the help names the three exit codes and the two age
    options, and a missing argument is argparse's exit 2."""
    parser = cli.build_parser()
    parsed = parser.parse_args(["capacity", "check", "machine", "host.sessions", "1", "sessions"])
    assert parsed.func is cli.cmd_capacity_check
    assert parser.parse_args(["capacity", "show"]).func is cli.cmd_capacity_show
    with pytest.raises(SystemExit) as stop:
        parser.parse_args(["capacity", "check", "machine"])
    assert stop.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["capacity", "check", "--help"])
    helped = " ".join(capsys.readouterr().out.split())
    for word in ("--max-age-declared", "--max-age-measured", "604800", "300", "Sessions read zero until something takes a run lease"):
        assert word in helped, word
    with pytest.raises(SystemExit):
        cli.main(["capacity", "--help"])
    assert "check" in capsys.readouterr().out


def test_the_adapter_is_read_only_reads_no_clock_and_attributes_two_capacities(world):
    """Catches a later edit that makes the adapter impure or changes the attribution: the module imports no
    `datetime`/`time`, calls none of `now`, `utcnow`, `acquire`, `renew`, `release`, `reserve`, `settle`, `write`,
    `durable_write`, `mkdir`, `open`, and the two attributions are the literal names the spec states; `policy()`
    has all six pairs with the stated defaults."""
    check = _check()
    assert check.SESSIONS == ("machine", "host.sessions", "sessions")
    assert check.DISPATCHES == ("provider", "dispatches.reserved", "requests")
    assert (check.DEFAULT_DECLARED_AGE, check.DEFAULT_MEASURED_AGE) == (604800, 300)
    assert (check.MAX_PROJECTS, check.MAX_LEASES, check.MAX_SCANNED) == (256, 1024, 16384)
    rows = check.policy().rows
    assert rows == (("dependency", "declared", 604800), ("dependency", "measured", 300),
                    ("machine", "declared", 604800), ("machine", "measured", 300),
                    ("provider", "declared", 604800), ("provider", "measured", 300))
    tree = ast.parse(Path(check.__file__).read_text(encoding="utf-8"))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"datetime", "time", "subprocess", "socket", "json"}, imported
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    forbidden = {"now", "utcnow", "today", "acquire", "renew", "release", "reserve", "settle", "write",
                 "write_bytes", "write_text", "durable_write", "mkdir", "makedirs", "touch", "symlink_to", "chmod",
                 "open", "unlink", "rename", "replace", "flock", "system", "Popen"}
    assert not called & forbidden, called & forbidden
