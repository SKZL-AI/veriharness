"""P3-17 slice 3, P3-19 slice 3, P3-16 slice 4: the wave's admission is recorded, keeps a reserve for critical nodes and
reads the capacity source.

`capacitycheck.admit_wave` (the wave's admission, called by `ProjectController._wave_round`) now (1) takes the capacity
source's snapshot of what it judges and hands the scheduler the provider state's narrowing of every provider session
capacity the wave wants, (2) keeps `machine/host.sessions.critical` sessions from ordinary nodes, and (3) writes the
admission -- its inputs, the snapshot's digest, the plan, the rows held -- as a create-only record bound by its own
digest, read back, **before** it holds anything (`hoh.capacityrecord`).

What is real: the registry (`capacity.write` into a `StorageRegistry.init()` root), run leases, parked runs
(`RunStore` and `dispatchrecovery.append_record`), the capacity ledger, the project store, `ProjectController`,
`HohRunLauncher` (with only its process boundary replaced, as in `tests/test_wave_capacity.py`) and `hoh project drive`
through `hoh.cli.main`. Hermetic: no process, Herdr, agent or network; everything under `tmp_path`. Every numeric bound
is a literal, not the module's constant: a table imported from the module would agree with a mutated module.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hoh import capacityrecord, cli
from hoh.contracts import Candidate, Condition, RunState, Stage
from hoh.dispatchrecovery import NO_MASTER, DispatchProvenance, ParkBinding, RoutingDecision, park_id
from hoh.failures import NormalizedFailure
from hoh.launcher import Dispatch, HohRunLauncher
from hoh.orchestrator import GateRunner, HaltClass, MergeResult, ProjectController
from hoh.project import GateOutcome, GateResult, Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.store import RunStore

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
CONFIG: dict = {}
TIME = "%Y-%m-%dT%H:%M:%SZ"
FILE = "capacity-registry.json"
LEDGER = "_capacity-reservations.json"


def _import(name: str):
    import importlib
    return importlib.import_module(name)


def utc(delta_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).strftime(TIME)


def git(repo, *args):
    done = subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def hermetic_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("HOH_STATE_ROOT", raising=False)
    monkeypatch.delenv("HOH_WAVE_WIDTH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))


# --- a host: a state root with a registry, a runs root, a place for records ------------------------------------------------

class Host:
    def __init__(self, tmp_path: Path) -> None:
        from hoh.storage_registry import StorageRegistry
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp_path
        self.state = tmp_path / "state"
        StorageRegistry(self.state).init()
        self.state = self.state.resolve()
        self.runs = tmp_path / "runs"
        self.runs.mkdir()
        self.records = tmp_path / "records"
        self.record = self.state / FILE
        self.number = 0

    def registry(self, *entries) -> None:
        capacity = _import("hoh.capacity")
        capacity.write(self.record, [
            capacity.Entry(kind=k, name=n, limit=lim, unit=u, source=s, as_of=rest[0] if rest else utc())
            for (k, n, lim, u, s, *rest) in entries])

    def sessions(self, limit: int, *more, as_of: str | None = None) -> None:
        stamp = [as_of] if as_of else []
        self.registry(("machine", "host.sessions", limit, "sessions", "declared", *stamp), *more)

    def lease(self, project: str, run_id: str) -> None:
        from hoh.runlease import RunLeases
        RunLeases(self.runs / "projects" / project / "run-leases").acquire(run_id, "holder", ttl_seconds=3600)

    def park(self, run_id: str, *, failure: NormalizedFailure = NormalizedFailure.USAGE_LIMIT, provider: str = "claude",
             account: str = "acct-a") -> None:
        """A run parked on a usage limit with the receipt the controller writes: the provider is `LIMITED`."""
        retry = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0).isoformat()
        state = RunState(run_id=run_id, repo_path="/arena/repo", project_name="demo", spec_path="/arena/spec.md",
                         spec_digest="s", policy_digest="p", profile_digest="h", stage=Stage.DEVELOPING,
                         condition=Condition.BLOCKED, iteration=1)
        state.blocked_kind, state.retry_after, state.blocked_reason = failure.value.lower(), retry, "parked"
        store = RunStore(self.runs, run_id)
        store.write_state(state)
        binding = ParkBinding(programme="NONE", program_epoch=0, run_id=run_id, iteration=1, attempt=0,
                              stage=state.stage.value, candidate="NONE", master=NO_MASTER)
        _import("hoh.dispatchrecovery").append_record(store.dir, RoutingDecision(
            park_id=park_id(binding, failure, retry), reason=failure.value, binding=binding,
            provenance=DispatchProvenance(provider=provider, model="m-1", account_handle=account, role="developer",
                                          capability_requirements=["read_repo", "write_worktree", "run_shell"]),
            retry_at=retry))

    def ask(self, nodes, *, record: bool = True, **kw):
        """`admit_wave` as the controller calls it (a fresh decision id per ask when a record is wanted)."""
        if record:
            self.number += 1
            kw.setdefault("record_dir", self.records)
            kw.setdefault("decision_id", f"c1-{self.number}-aa")
        kw.setdefault("now", utc)
        return _import("hoh.capacitycheck").admit_wave(nodes, runs_root=self.runs, state_root=self.state, **kw)

    def held(self) -> list[tuple]:
        path = self.runs / LEDGER
        if not path.exists():
            return []
        return [(h["run_id"], h["kind"], h["name"], h["amount"], h["unit"])
                for h in json.loads(path.read_text())["holds"] if h["status"] == "HELD"]


@pytest.fixture
def host(tmp_path) -> Host:
    return Host(tmp_path)


def written(host: Host, gate) -> dict:
    return capacityrecord.read_record(host.records / gate.record)


# --- R1 the admission is recorded: create-only, digest-bound, read back, before it is held --------------------------------------

def test_an_admission_is_recorded_with_its_inputs_and_a_digest_and_what_was_held_is_what_it_says(host):
    """Catches a decision nobody can reconstruct: 3 nodes, 2 sessions. The gate names the record; it reads back through the
    strict reader; it quotes the sha256 of the registry file and the snapshot digest the gate returns, the policy, the
    width and ready it was told, each node offered with its class, the holds the decision was handed, the plan's
    verdicts and reasons and the rows held -- which are exactly the rows in the ledger -- and the file and its directory are
    owner-only."""
    host.sessions(2)
    gate = host.ask(["a", "b", "c"], width=3, ready=5)
    assert gate.admitted == ("a", "b") and gate.record == "capacity-c1-1-aa.json"
    rec = written(host, gate)
    assert rec["decision_id"] == "c1-1-aa" and (rec["width"], rec["ready"]) == (3, 5)
    assert rec["registry"] == sha(host.record) == gate.registry
    assert rec["snapshot"] == gate.snapshot and len(rec["snapshot"]) == 64
    assert rec["offered"] == [{"node": n, "critical": False} for n in "abc"]
    assert rec["outcome"] == {"admitted": ["a", "b"], "waiting": [["c", "wait", "exhausted"]]}
    assert rec["plan"]["admitted"] == ["a", "b"] and [p["reason"] for p in rec["plan"]["placements"]] == ["ok", "ok", "exhausted"]
    assert rec["held"] == [["a", "machine", "host.sessions", 1, "sessions"], ["b", "machine", "host.sessions", 1, "sessions"]]
    assert [tuple(row) for row in rec["held"]] == host.held()
    assert ["machine", "host.sessions", 0, "sessions"] in rec["holds"]
    assert [tuple(row) for row in rec["policy"]][:2] == [("dependency", "declared", 604800), ("dependency", "measured", 300)]
    assert rec["reserve"] is None and rec["narrowed"] == []
    assert stat.S_IMODE(os.stat(host.records / gate.record).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(host.records).st_mode) == 0o700


def test_the_record_is_written_before_the_rows_are_held_and_a_record_that_fails_holds_nothing(host, monkeypatch):
    """Catches an admission that stands without its record, or a record written after the holds: when the ledger cannot be
    written the record is on disk anyway (it was first), and when the record cannot be written nobody is admitted and the
    ledger has no row."""
    host.sessions(2)
    reservation = _import("hoh.reservation")

    def broken(self, rows):
        raise reservation.ReservationError("the capacity ledger could not be written: disk full")
    with monkeypatch.context() as scope:
        scope.setattr(reservation.CapacityReservations, "_write", broken)
        gate = host.ask(["a", "b"])
    assert gate.admitted == () and "disk full" in gate.why and host.held() == []
    assert [p.name for p in host.records.iterdir()] == ["capacity-c1-1-aa.json"]          # recorded first

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")
    with monkeypatch.context() as scope:
        scope.setattr(capacityrecord, "durable_create", full)
        gate = host.ask(["a", "b"])
    assert gate.admitted == () and "could not be written" in gate.why and gate.record is None
    assert host.held() == [] and [p.name for p in host.records.iterdir()] == ["capacity-c1-1-aa.json"]


def test_a_record_that_does_not_read_back_as_written_admits_nobody(host, monkeypatch):
    """Catches a record taken as written without looking: the read-back returns another digest than the one written, so the
    admission is refused, with nothing held."""
    host.sessions(2)
    monkeypatch.setattr(capacityrecord, "read_record", lambda path: {"digest": "0" * 64})
    gate = host.ask(["a"])
    assert gate.admitted == () and "reads back as another record" in gate.why and host.held() == []


@pytest.mark.parametrize("label", ["a file where the directory belongs", "a link where the directory belongs",
                                   "the same decision id again", "a directory that holds the most records",
                                   "a record directory without its id", "a width that is a bool"])
def test_what_cannot_be_recorded_admits_nobody_names_why_and_never_replaces_a_record(host, label, tmp_path, monkeypatch):
    """Catches an admission that stands on a record it could not write: each case is refused with its reason in the gate,
    nothing is held, and a record that exists is not touched (its bytes before and after are equal)."""
    host.sessions(5)
    kw: dict = {}
    if label == "a file where the directory belongs":
        host.records.write_text("not a directory")
    elif label == "a link where the directory belongs":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        host.records.symlink_to(elsewhere)
    elif label == "the same decision id again":
        first = host.ask(["a"], decision_id="c1-9-aa")
        assert first.admitted == ("a",)
        before = (host.records / first.record).read_bytes()
        kw["decision_id"] = "c1-9-aa"
    elif label == "a directory that holds the most records":
        monkeypatch.setattr(capacityrecord, "MAX_RECORDS", 2)
        host.records.mkdir(mode=0o700)
        for number in (1, 2):
            (host.records / f"capacity-old-{number}.json").write_text("{}")
    elif label == "a record directory without its id":
        kw.update(record=False, record_dir=host.records)
    else:
        kw["width"] = True
    held = host.held()
    gate = host.ask(["b"], **kw)
    assert gate.admitted == () and gate.configured and gate.record is None, gate
    assert gate.why.startswith("capacity cannot be judged"), gate.why
    assert host.held() == ([] if label == "the same decision id again" else held)      # asking again ends the owner's earlier wave
    if label == "the same decision id again":
        assert "never replaced" in gate.why and (host.records / "capacity-c1-9-aa.json").read_bytes() == before
    if label == "a directory that holds the most records":
        assert "holds 2 records" in gate.why and sorted(p.name for p in host.records.iterdir()) == [
            "capacity-old-1.json", "capacity-old-2.json"]


@pytest.mark.parametrize("spelled", ["../x", "c1-1-aa\n", "", "a" * 65, ".hidden", "a/b", "a b", "\u00e9"])
def test_a_decision_id_is_exactly_the_grammar_and_never_a_path(host, spelled):
    """Catches an id that reaches the file system as a path or a near miss of the grammar (a trailing newline, a leading dot,
    65 characters, a separator, a space, a non-ASCII letter): the admission is refused, nothing is written under or beside
    the records directory and nothing is held; the longest good id (64 characters) is accepted."""
    host.sessions(2)
    gate = host.ask(["a"], decision_id=spelled)
    assert gate.admitted == () and gate.record is None and gate.why.startswith("capacity cannot be judged"), gate
    assert host.held() == [] and not host.records.exists() and not (host.tmp / "x").exists()
    longest = "a" * 64
    assert host.ask(["a"], decision_id=longest).record == f"capacity-{longest}.json"


def test_a_record_larger_than_the_bound_is_not_written(host):
    """Catches a record that can grow without a bound: a body that renders to more than 2 MiB is refused by the writer, no
    file and no temporary name is left, and the reader refuses a file that size before it reads a byte."""
    host.sessions(2)
    good, doc = _good(host)
    body = {k: v for k, v in doc.items() if k != "digest"}
    body["decision_id"] = "big"
    body["facts"] = ["x" * 1000] * 2100
    with pytest.raises(capacityrecord.RecordError, match="larger than 2097152 bytes"):
        capacityrecord.write_record(host.records, "big", body)
    assert sorted(p.name for p in host.records.iterdir()) == [good.name]


def test_the_bounds_of_the_record_are_exact():
    """Catches a changed ceiling: the most records a directory holds, the most bytes a record is, the most provider
    capacities a wave asks for, and the names the record and its directory have."""
    check = _import("hoh.capacitycheck")
    assert (capacityrecord.MAX_RECORDS, capacityrecord.MAX_RECORD_BYTES) == (4096, 2 * 1024 * 1024)
    assert (capacityrecord.SCHEMA, capacityrecord.DIRNAME) == ("hoh-capacity-admission/1", "capacity")
    assert (check.MAX_PROVIDER_WANTS, check.RESERVE, check.PROVIDER_SESSIONS) == (
        16, ("machine", "host.sessions.critical", "sessions"), ".sessions")


# --- R2 the reader accepts exactly what the writer wrote --------------------------------------------------------------------

def _good(host: Host) -> tuple[Path, dict]:
    host.sessions(2)
    gate = host.ask(["a", "b"])
    path = host.records / gate.record
    return path, json.loads(path.read_text())


@pytest.mark.parametrize("label", ["missing", "a directory", "a link", "a second name", "a FIFO", "oversize",
                                   "not UTF-8", "not JSON", "a list", "a repeated key", "NaN", "a missing key",
                                   "an extra key", "another schema", "an edited value", "a wrong digest",
                                   "another rendering of the same body", "another decision's name", "deep nesting"])
def test_read_record_refuses_every_file_that_is_not_exactly_what_was_written(host, tmp_path, label):
    """Catches a reader that takes a damaged or forged record: each variant of a good record is a `RecordError`, and the
    good one reads. The variants are built under their own directory, never by editing the original."""
    good, doc = _good(host)
    assert capacityrecord.read_record(good)["decision_id"] == doc["decision_id"]
    where = tmp_path / "case"
    where.mkdir()
    target = where / good.name
    raw = good.read_bytes()
    body = {k: v for k, v in doc.items() if k != "digest"}

    def put(text: bytes | str) -> None:
        target.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))

    def reseal(changed: dict) -> None:                                    # a digest that matches the edited body
        digest, payload = capacityrecord._rendered(changed)
        put(payload)

    if label == "a directory":
        target.mkdir()
    elif label == "a link":
        target.symlink_to(good)
    elif label == "a second name":
        put(raw)
        os.link(target, where / "other-name")
    elif label == "a FIFO":
        os.mkfifo(target)
    elif label == "oversize":
        put(raw + b" " * (2 * 1024 * 1024))
    elif label == "not UTF-8":
        put(b"\xff\xfe" + raw)
    elif label == "not JSON":
        put(raw[:-20])
    elif label == "a list":
        put("[]")
    elif label == "a repeated key":
        put(raw.decode().replace("{", '{"schema": "x", ', 1))
    elif label == "NaN":
        put(raw.decode().replace('"width": null', '"width": NaN'))
    elif label == "a missing key":
        reseal({k: v for k, v in body.items() if k != "narrowed"})
    elif label == "an extra key":
        reseal({**body, "extra": 1})
    elif label == "another schema":
        reseal({**body, "schema": "hoh-capacity-admission/2"})
    elif label == "an edited value":
        put(raw.decode().replace('"ready": null', '"ready": 7'))
    elif label == "a wrong digest":
        put(raw.decode().replace(doc["digest"], "0" * 64))
    elif label == "another rendering of the same body":
        put(json.dumps(doc, sort_keys=True, indent=4))
    elif label == "another decision's name":
        target = where / "capacity-c9-9-zz.json"
        put(raw)
    elif label == "deep nesting":
        put("[" * 5000 + "]" * 5000)
    with pytest.raises(capacityrecord.RecordError):
        capacityrecord.read_record(target)


# --- R3 the reserve for critical nodes -------------------------------------------------------------------------------------

RESERVE = ("machine", "host.sessions.critical", 2, "sessions", "declared")


def test_an_ordinary_node_never_takes_the_reserve_and_a_critical_node_may(host):
    """Catches a reserve that is not kept or a critical node that is held to it: 4 sessions with 2 reserved admit two
    ordinary nodes of three, the third waits and says 2 are kept for critical nodes, the critical node is admitted over what
    is left; ordinary nodes alone never pass 2; critical nodes alone take all 4; the placements come back in the caller's
    order and no ledger row names the reserve."""
    host.sessions(4, RESERVE)
    gate = host.ask(["o1", "o2", "o3", "c1"], critical=["c1"])
    assert gate.admitted == ("o1", "o2", "c1") and [n for n, _ in gate.waits] == ["o3"]
    assert "2 of 4 sessions" in gate.waits[0][1] or "of 4 sessions of machine/host.sessions are held" in gate.waits[0][1]
    assert "2 of them are kept for critical nodes (machine/host.sessions.critical)" in gate.waits[0][1]
    assert [p["node"] for p in written(host, gate)["plan"]["placements"]] == ["o1", "o2", "o3", "c1"]
    assert written(host, gate)["offered"][3] == {"node": "c1", "critical": True}
    assert {name for _, _, name, _, _ in host.held()} == {"host.sessions"}
    assert host.ask(["o1", "o2", "o3", "o4"], critical=["not-offered"]).admitted == ("o1", "o2")
    assert host.ask([], record=False).admitted == ()                            # an empty wave is refused with or without a reserve
    all_critical = host.ask(["c1", "c2", "c3", "c4", "c5"], critical=["c1", "c2", "c3", "c4", "c5"])
    assert all_critical.admitted == ("c1", "c2", "c3", "c4") and [n for n, _ in all_critical.waits] == ["c5"]


def test_a_waiting_ordinary_node_does_not_hold_a_critical_node_back(host):
    """Catches head-of-line blocking across classes: 2 sessions with 1 reserved, offered o1, o2 and then c1: o1 is admitted
    (1 = 2 - 1), o2 waits `exhausted`, and c1 is admitted -- it is not `behind` o2, as it would be in one walk over the list;
    with a lease holding 1 session the ordinary nodes all wait and the critical one is still admitted."""
    host.sessions(2, ("machine", "host.sessions.critical", 1, "sessions", "declared"))
    gate = host.ask(["o1", "o2", "c1"], critical=["c1"])
    assert gate.admitted == ("o1", "c1") and dict(gate.waits)["o2"].startswith("wait/exhausted")
    plain = host.ask(["o1", "o2", "c1"])
    assert plain.admitted == ("o1",) and dict(plain.waits)["c1"].startswith("wait/exhausted")
    host.lease("other", "r1")
    held = host.ask(["o1", "o2", "c1"], critical=["c1"])
    assert held.admitted == ("c1",) and [n for n, _ in held.waits] == ["o1", "o2"]


@pytest.mark.parametrize("label", ["stale", "another unit", "absent", "limit zero", "more than the host has"])
def test_the_reserve_is_read_like_every_other_fact(host, label):
    """Catches a reserve that is trusted when it should not be: a stale record (older than the policy age) or one in another
    unit admits no ordinary node and says why with the broker's reason, a critical node is still admitted; no record is no
    reserve (the plain limit); a limit of zero reserves nothing; a limit above the host's sessions admits no ordinary node
    (waits, never an over-admission)."""
    if label == "stale":
        host.sessions(2, ("machine", "host.sessions.critical", 1, "sessions", "declared", utc(-10 * 86400)))
    elif label == "another unit":
        host.sessions(2, ("machine", "host.sessions.critical", 1, "requests", "declared"))
    elif label == "absent":
        host.sessions(2)
    elif label == "limit zero":
        host.sessions(2, ("machine", "host.sessions.critical", 0, "sessions", "declared"))
    else:
        host.sessions(2, ("machine", "host.sessions.critical", 5, "sessions", "declared"))
    gate = host.ask(["o1", "o2", "c1"], critical=["c1"])
    waits = dict(gate.waits)
    if label == "stale":
        assert gate.admitted == ("c1",) and waits["o1"].startswith("refuse/stale") and "hoh capacity set" in waits["o1"]
    elif label == "another unit":
        assert gate.admitted == ("c1",) and waits["o1"].startswith("refuse/unit")
    elif label == "absent":
        assert gate.admitted == ("o1", "o2") and waits["c1"].startswith("wait/exhausted")
    elif label == "limit zero":
        assert gate.admitted == ("o1", "o2") and waits["c1"].startswith("wait/exhausted")
    else:
        assert gate.admitted == ("c1",) and waits["o1"].startswith("wait/exhausted")


# --- R4 the provider's state is an input of the admission ------------------------------------------------------------------

PROVIDER = ("provider", "claude.sessions", 4, "sessions", "declared")


def test_a_limited_provider_admits_no_node_that_needs_its_session_and_a_healthy_one_does(host):
    """Catches a wave that ignores the provider's quota: with `provider/claude.sessions` declared and a run parked on a usage
    limit of provider claude, nodes of claude wait -- the line names the figures and the cause -- while nodes of another
    provider, or a registry that does not declare the capacity, are admitted; unscoped (`providers=None`) counts every declared
    one. Without the park the same wave is admitted and holds one `provider/claude.sessions` row per node."""
    host.sessions(4, PROVIDER)
    free = host.ask(["a", "b"], providers=["claude"])
    assert free.admitted == ("a", "b")
    assert sorted(set(host.held())) == [("a", "machine", "host.sessions", 1, "sessions"),
                                        ("a", "provider", "claude.sessions", 1, "sessions"),
                                        ("b", "machine", "host.sessions", 1, "sessions"),
                                        ("b", "provider", "claude.sessions", 1, "sessions")]
    host.park("parked", provider="claude")
    limited = host.ask(["a", "b"], providers=["claude"])
    assert limited.admitted == () and [n for n, _ in limited.waits] == ["a", "b"]
    assert limited.snapshot != free.snapshot and len(limited.snapshot) == 64             # another provider state, another digest
    text = limited.waits[0][1]
    assert text.startswith("wait/exhausted: 4 of 4 sessions of provider/claude.sessions are held; the provider state holds back 4")
    assert "(FULL)" in text and "claude/acct-a is LIMITED" in text
    rec = written(host, limited)
    assert rec["narrowed"][0]["name"] == "claude.sessions" and rec["narrowed"][0]["held"] == 4
    states = {(f["name"], f["field"]): f["value"] for f in rec["facts"] if f["family"] == "provider" and f["name"]}
    assert states[("claude", "health_state")] == "LIMITED"
    assert host.ask(["a", "b"], providers=["codex"]).admitted == ("a", "b")
    assert host.ask(["a"], providers=None).admitted == ()
    host.sessions(4)                                                       # nobody declared the provider's capacity
    assert host.ask(["a", "b"], providers=["claude"]).admitted == ("a", "b")


def test_a_provider_reading_that_cannot_be_read_holds_the_whole_capacity(host, monkeypatch):
    """Catches a provider state that is read as healthy when it could not be read: `health.read_health` raising makes the
    reading UNKNOWN, so every provider capacity the wave wants is held whole (nobody admitted, the reason says the reading is
    incomplete or UNKNOWN), while a wave that wants no provider capacity is not stopped by it."""
    host.sessions(4, PROVIDER)
    source = _import("hoh.capacitysource")

    def broken(*args, **kwargs):
        raise OSError("the runs root cannot be listed")
    monkeypatch.setattr(source, "read_health", broken)
    gate = host.ask(["a"], providers=["claude"])
    assert gate.admitted == () and "the provider reading is incomplete or UNKNOWN" in gate.waits[0][1]
    host.sessions(4)
    assert host.ask(["a"], providers=["claude"]).admitted == ("a",)


@pytest.mark.parametrize("gap, label", [(30, "inside both bounds"), (61, "past the slots' bound of 60 s")])
def test_the_snapshot_is_observed_at_one_reading_of_the_clock_and_judged_at_the_next(host, gap, label):
    """Catches a snapshot that is judged at the instant it was read (its bounds never fire): the clock gives one time for the
    observation and a later one for the decision. A gap inside the bounds admits; a gap past the slots' 60 s makes the holds
    UNKNOWN and admits nobody, with the reason. Two readings, in that order: a clock that gives the same time twice never
    reaches the bound."""
    host.sessions(2)
    base = datetime.now(timezone.utc).replace(microsecond=0)
    readings = iter([base, base + timedelta(seconds=gap)])
    calls: list[str] = []

    def clock() -> str:
        calls.append("now")
        return next(readings).strftime(TIME)
    gate = host.ask(["a"], now=clock, record=False)
    assert calls == ["now", "now"], calls
    if gap < 60:
        assert gate.admitted == ("a",)
    else:
        assert gate.admitted == () and "the holds are UNKNOWN" in gate.why and "61 s old; at most 60 s" in gate.why


def test_the_inputs_are_the_capacity_sources_own(host):
    """Catches a second rule for what a snapshot hands a decision: with a provider that is limited, the holds the admission
    builds make the broker answer exactly what `capacitysource.decide` answers for the same snapshot, wants and time."""
    host.sessions(4, PROVIDER)
    host.park("parked", provider="claude")
    source, check, broker = _import("hoh.capacitysource"), _import("hoh.capacitycheck"), _import("hoh.broker")
    capacity = _import("hoh.capacity")
    registry = capacity.read(host.record)
    now = utc()
    snapshot = source.take(registry, runs_root=host.runs, now=now)
    wants = (broker.Amount("machine", "host.sessions", 1, "sessions"), broker.Amount("provider", "claude.sessions", 1, "sessions"))
    holds, narrowed = check._inputs(snapshot, wants, now=now)
    mine = broker.decide(registry, check.policy(), now, wants, holds)
    theirs, their_narrowed = source.decide(snapshot, wants, now=now)
    assert mine == theirs and narrowed == their_narrowed and narrowed[0].held == 4


def test_the_provider_wants_are_bounded_and_only_declared_names_count(host):
    """Catches an unbounded want list and a convention that matches too much: 16 declared `<provider>.sessions` capacities are
    asked for one each (and admit); a 17th refuses the wave, naming the bound; `claude.requests` and a capacity of another
    kind are not provider session capacities."""
    host.sessions(4, *[("provider", f"p{n:02d}.sessions", 3, "sessions", "declared") for n in range(16)])
    ok = host.ask(["a"], providers=None)
    assert ok.admitted == ("a",) and len([h for h in host.held() if h[1] == "provider"]) == 16
    host.sessions(4, *[("provider", f"p{n:02d}.sessions", 3, "sessions", "declared") for n in range(17)])
    over = host.ask(["a"], providers=None)
    assert over.admitted == () and "more than 16 provider session capacities" in over.why
    host.sessions(4, ("provider", "claude.requests", 1, "requests", "declared"),
                  ("dependency", "claude.sessions", 1, "sessions", "declared"))
    assert host.ask(["a"], providers=["claude"]).admitted == ("a",)


@pytest.mark.parametrize("kw", [{"critical": "a"}, {"critical": [1]}, {"providers": "claude"}, {"providers": [3]}])
def test_a_critical_list_or_a_provider_list_that_is_not_a_list_of_names_admits_nobody(host, kw):
    """Catches a string taken as a list of one-letter names or a number taken as a name: each is refused (fail closed)."""
    host.sessions(4, PROVIDER)
    gate = host.ask(["a"], **kw)
    assert gate.admitted == () and gate.why.startswith("capacity cannot be judged"), gate


# --- R5 the production path ----------------------------------------------------------------------------------------------

class Grants:
    name = "test"

    def may_approve(self, worktree):
        return True

    def approve(self, worktree, repo):
        from hoh.approval import Approval
        return Approval(granted=True, worktree=worktree, provider=self.name)


class Gates(GateRunner):
    def subject(self) -> str:
        return "abc1234"

    def run(self, subject):
        return [GateResult(name="union", outcome=GateOutcome.GREEN, subject=subject, exit_code=0, detail="")]


class Launcher(HohRunLauncher):
    """The real launcher with only the process boundary replaced; every run is accepted at once."""

    def __init__(self, root, repo, **kw):
        super().__init__(root, repo, approvals=Grants(), **kw)
        self.starts: list[str] = []
        self.merged: list[str] = []
        self._lock = threading.Lock()

    def _run_cli(self, argv):
        if "worktree" in argv:
            branch = argv[argv.index("--branch") + 1]
            path = self.root.parent / "worktrees" / branch
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                git(self.repo_path, "worktree", "add", "-q", "-b", branch, str(path))
            return Dispatch(0, json.dumps({"path": str(path)}), "")
        if "start" in argv:
            run_id = argv[argv.index("--run-id") + 1]
            with self._lock:
                self.starts.append(run_id)
            RunStore(self.root, run_id).write_state(RunState(
                run_id=run_id, repo_path=argv[argv.index("--repo") + 1], project_name="p",
                spec_path=argv[argv.index("--spec") + 1], spec_digest="s" * 16, policy_digest="p" * 16,
                profile_digest="f" * 16, stage=Stage.PLANNING, condition=Condition.ACTIVE))
            return Dispatch(0, "", "")
        if "run" in argv:
            run_id = argv[argv.index("run") + 1]
            store = RunStore(self.root, run_id)
            state = store.read_state().model_copy(update={
                "stage": Stage.CHECKPOINTED,
                "last_accepted_candidate": Candidate(candidate_id=f"{run_id}-i1", repo_path=store.read_state().repo_path,
                                                     commit="a" * 40, tree_clean=True, tree_digest="d" * 16)})
            store.write_state(state)
            return Dispatch(0, "", "")
        raise AssertionError(f"unexpected cli call {argv}")

    def merge(self, node, outcome):
        self.merged.append(node.id)
        return MergeResult(landed=True, branch=node.branch or "", integrated="b" * 40, candidate=outcome.candidate or "")


class World(Host):
    """A host plus a git repository and a project `p` whose nodes are independent."""

    def __init__(self, tmp_path: Path, names=("n1", "n2", "n3"), node_kw: dict | None = None) -> None:
        node_kw = node_kw or {}
        super().__init__(tmp_path)
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "master")
        (self.repo / "README").write_text("x\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "first")
        self.root = self.runs
        nodes = []
        for name in names:
            spec = tmp_path / f"{name}.md"
            spec.write_text(f"# {name}\n", encoding="utf-8")
            nodes.append(TaskNode(id=name, spec_path=str(spec), writes=[f"{name}.txt"], **node_kw.get(name, {})))
        self.store = ProjectStore(self.root, "p")
        self.store.create(ProjectState(project_id="p", repo_path=str(self.repo), nodes=nodes))

    def launcher(self) -> Launcher:
        return Launcher(self.root, self.repo, storage_root=self.state)

    def records_dir(self) -> Path:
        return self.store.dir / "capacity"


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def build_driver(store):
    """The `driver.json` factory of `hoh project drive`: the real launcher with a fake process boundary."""
    cfg = CONFIG
    launcher = Launcher(cfg["root"], cfg["repo"], storage_root=cfg["state"])
    cfg["launcher"] = launcher
    return launcher, Gates()


def _drive(world, monkeypatch, capsys, width="3"):
    CONFIG.clear()
    CONFIG.update(root=world.root, repo=world.repo, state=world.state)
    (world.store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_capacity_admission:build_driver", "path": str(Path(__file__).resolve().parent)}))
    monkeypatch.setenv("HOH_WAVE_WIDTH", width)
    rc = cli.main(["--root", str(world.root), "project", "drive", "p", "--session", "sA"])
    return rc, json.loads(capsys.readouterr().out)


def test_hoh_project_drive_records_each_wave_admission_in_the_project_and_the_record_matches_what_was_held(world, monkeypatch,
                                                                                                    capsys):
    """The production path, end to end: `hoh project drive` (cli.main) -> `resumeloop.drive_governed` ->
    `ProjectController.run` -> `_wave_round` -> `_capacity_admission` -> `capacitycheck.admit_wave` ->
    `capacityrecord.write_record`. Catches a record only unit tests reach: with room for one session and three independent
    nodes, the project's `capacity/` directory holds one record per admission (round 1 admits n1, round 2 asks again), each
    reads back, names the controller's width (3) and the nodes ready, and the printed steps name the files."""
    world.sessions(1)
    rc, out = _drive(world, monkeypatch, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    files = sorted(p.name for p in world.records_dir().iterdir())
    assert len(files) >= 2 and all(name.startswith("capacity-c") and name.endswith(".json") for name in files)
    first = capacityrecord.read_record(world.records_dir() / files[0])
    assert first["outcome"]["admitted"] == ["n1"] and first["outcome"]["waiting"] == [["n2", "wait", "exhausted"], ["n3", "wait", "exhausted"]]
    assert (first["width"], first["ready"]) == (3, 3) and first["decision_id"].split("-")[1] == "1"
    assert first["registry"] == sha(world.record) and len(first["snapshot"]) == 64
    assert first["held"] == [["n1", "machine", "host.sessions", 1, "sessions"]]
    named = [s for s in out["steps"] if s.startswith("ADMISSION_RECORD")]
    assert len(named) == len(files)
    assert sorted(CONFIG["launcher"].merged) == ["n1", "n2", "n3"]
    before = {p.name: p.read_bytes() for p in world.records_dir().iterdir()}
    rc, again = _drive(world, monkeypatch, capsys)                               # the closed project: nothing to decide
    assert rc == 0 and again["halt"] == "CLOSED"
    assert {p.name: p.read_bytes() for p in world.records_dir().iterdir()} == before     # quiet: not one byte, not one file


def test_hoh_project_drive_halts_capacity_wait_when_the_admission_cannot_be_recorded(world, monkeypatch, capsys):
    """Catches a wave that runs on a decision nobody recorded: with a file where the project's `capacity/` directory belongs
    `hoh project drive` prints the halt `CAPACITY_WAIT`, says the directory is not a directory of its own, exits 1, launches
    nothing and leaves every node READY (and the ledger has no row)."""
    world.sessions(3)
    world.records_dir().write_text("not a directory")
    rc, out = _drive(world, monkeypatch, capsys)
    assert rc == 1 and out["halt"] == "CAPACITY_WAIT" and "not a directory of its own" in out["reason"], out
    assert CONFIG["launcher"].starts == [] and world.held() == []
    assert {n.lifecycle for n in world.store.read_state().nodes} == {Lifecycle.READY}


def test_hoh_project_drive_keeps_the_reserve_for_a_node_whose_run_started(tmp_path, monkeypatch, capsys):
    """Catches a reserve the production path never passes: 2 sessions with 1 reserved and three nodes where n3 carries a run
    (`run_id`, a started iteration): n1 takes the one ordinary session, n2 waits, n3 -- critical -- is admitted with it; the
    first record says so (`critical` true for n3 only, the reserve quoted) and the first wave is n1 and n3."""
    world = World(tmp_path, node_kw={"n3": {"run_id": "n3"}})
    world.sessions(2, ("machine", "host.sessions.critical", 1, "sessions", "declared"))
    rc, out = _drive(world, monkeypatch, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    first = capacityrecord.read_record(world.records_dir() / sorted(p.name for p in world.records_dir().iterdir())[0])
    assert first["offered"] == [{"node": "n1", "critical": False}, {"node": "n2", "critical": False},
                                {"node": "n3", "critical": True}]
    assert first["outcome"]["admitted"] == ["n1", "n3"] and first["reserve"]["limit"] == 1
    assert [s for s in out["steps"] if s.startswith("CAPACITY_WAIT")][0] == "CAPACITY_WAIT:n2"


def test_the_controller_names_the_critical_nodes_the_providers_the_directory_the_id_and_the_width(tmp_path, monkeypatch):
    """Catches a controller that stops handing the admission what it needs: a repair node (`repair_of`) and a node whose run
    started (`run_id`) are the critical ones and a plain node is not, the providers are the launcher's, the record goes to
    `<project>/capacity` under an id `c<write_seq>-<round>-<token>`, and the width and the ready count are the controller's.
    The admission is replaced by one that records its arguments and admits nobody, so nothing is started."""
    world = World(tmp_path, node_kw={"n2": {"run_id": "n2"}, "n3": {"repair_of": "n1"}})
    check = _import("hoh.capacitycheck")
    seen: dict = {}

    def spy(node_ids, **kw):
        seen.update(nodes=list(node_ids), **kw)
        return check.Gate(True, (), tuple((n, "spy") for n in node_ids), "spy")
    monkeypatch.setattr(check, "admit_wave", spy)
    result = ProjectController(world.store, world.launcher(), Gates(), width=3).run()
    assert result.halt is HaltClass.CAPACITY_WAIT
    assert seen["nodes"] == ["n1", "n2", "n3"] and seen["critical"] == ["n2", "n3"]
    assert seen["providers"] is not None and list(seen["providers"]) == sorted(set(seen["providers"]))
    assert Path(seen["record_dir"]) == world.records_dir()
    parts = seen["decision_id"].split("-")
    assert parts[0].startswith("c") and parts[1] == "1" and len(parts[2]) == 6
    assert (seen["width"], seen["ready"]) == (3, 3)


def test_hoh_project_drive_waits_while_the_provider_is_limited_and_runs_when_it_is_not(world, monkeypatch, capsys):
    """Catches a production wave that never consults the provider's state (the capacity source is read in the scheduling
    path): `provider/claude.sessions` is declared and a run is parked on claude's usage limit, so `hoh project drive` prints
    the halt `CAPACITY_WAIT` naming the provider and its state, launches nothing, leaves every node READY and records the
    refusal; on a host with the same registry and no park the project closes, each admitted node held a
    `provider/claude.sessions` session beside its machine session, and the record quotes the provider state as the fact it
    was made on."""
    world.sessions(4, PROVIDER)
    world.park("parked", provider="claude")
    rc, out = _drive(world, monkeypatch, capsys)
    assert rc == 1 and out["halt"] == "CAPACITY_WAIT" and "claude/acct-a is LIMITED" in out["reason"], out
    assert CONFIG["launcher"].starts == [] and {n.lifecycle for n in world.store.read_state().nodes} == {Lifecycle.READY}
    [name] = [p.name for p in world.records_dir().iterdir()]
    refused = capacityrecord.read_record(world.records_dir() / name)
    assert refused["outcome"]["admitted"] == [] and refused["narrowed"][0]["held"] == 4
    healthy = World(world.tmp / "healthy")
    healthy.sessions(4, PROVIDER)
    rc, out = _drive(healthy, monkeypatch, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    first = capacityrecord.read_record(healthy.records_dir() / sorted(p.name for p in healthy.records_dir().iterdir())[0])
    assert ["n1", "provider", "claude.sessions", 1, "sessions"] in first["held"]
    assert {(f["name"], f["field"]) for f in first["facts"] if f["family"] == "provider" and f["name"]} == set()


def test_the_register_reads_p3_17_proven_and_p3_16_and_p3_19_partial_with_what_is_left_on_this_tree():
    """Catches the register edit being left out or overclaiming: `capability_matrix.classify`, with this tree's own symbols and
    import graph and the node ids of the test files this tree holds, reads P3-17 PROVEN (a row that still has an `equivalent`,
    or whose probe names a test that does not exist, reads PARTIAL) and P3-16 and P3-19 PARTIAL, their equivalents naming exactly
    what is not built (the host-cap probe; per-node wants and backfill) and nothing that is."""
    import ast
    import importlib.util
    import sys

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("capability_matrix_rows_p317", root / "tools" / "capability_matrix.py")
    cm = importlib.util.module_from_spec(spec)
    sys.modules["capability_matrix_rows_p317"] = cm
    spec.loader.exec_module(cm)
    rows = {r["id"]: r for r in json.loads((root / "program" / "v3_3" / "REQUIREMENTS.json").read_text())["requirements"]}
    ids = []
    for path in sorted((root / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        ids += [f"tests/{path.name}::{n.name}" for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    collection = ("\n".join(ids), True)
    symbols, graph = cm._symbols(), cm._import_graph()
    seen = {rid: cm.classify(rows[rid], symbols, collection, {}, graph)["status"] for rid in ("P3-16", "P3-17", "P3-19")}
    assert seen == {"P3-16": cm.PARTIAL, "P3-17": cm.PROVEN, "P3-19": cm.PARTIAL}, seen
    assert "equivalent" not in rows["P3-17"]["probe"]
    assert rows["P3-17"]["probe"]["test"].startswith("tests/test_capacity_admission.py::")
    assert rows["P3-17"]["previous_probe"]["symbol"] == "decide" and "replaced_on" in rows["P3-17"]["previous_probe"]
    assert rows["P3-16"]["probe"]["equivalent"].endswith("(observed_cap is only a hand-written measured entry)")
    assert "reader in the scheduling path" not in rows["P3-16"]["probe"]["equivalent"].split("still missing:")[1]
    left = rows["P3-19"]["probe"]["equivalent"].split("not built:")[1]
    assert "per-node wants" in left and "backfill" in left and "reserve" not in left and "plan record" not in left
