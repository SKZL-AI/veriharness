"""Run leases: one durable, fenced claim per run (P3-18, slice 1).

Every limit is asserted with a literal number, not with the module's constant, so a moved constant fails a
test instead of moving it. The module is imported inside each test, so a missing module fails each test on
its own (a collection error would hide the nodes from one another). Environment facts -- the clock, the boot
id, which pids are alive, their start times -- are injected: those are exactly what the scenarios change.
Tests write only under `tmp_path`, start no Herdr and no agent, and use no network.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import signal
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

T0 = "2026-10-12T10:00:00Z"


def _later(moment: str, seconds: int) -> str:
    t = datetime.strptime(moment, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (t + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class World:
    """One machine: a clock, a boot id, the live pids and their start times."""

    def __init__(self):
        self.now = T0
        self.boot = "boot-1111-2222"
        self.host = "host-a"
        self.pidns = "pid:[4026531836]"
        self.starts: dict[int, str] = {}

    def advance(self, seconds):
        self.now = _later(self.now, seconds)

    def spawn(self, pid, start="1000"):
        self.starts[pid] = start

    def kill(self, pid):
        self.starts.pop(pid, None)

    def leases(self, directory, pid, **kw):
        from hoh.runlease import RunLeases

        self.spawn(pid) if pid not in self.starts else None
        env = dict(clock=lambda: self.now, boot=lambda: self.boot, host=self.host, pid=pid,
                   alive=lambda p: p in self.starts, pidns=lambda: self.pidns,
                   start=lambda p: self.starts.get(p, ""))
        env.update(kw)
        return RunLeases(directory, **env)


@pytest.fixture
def w():
    return World()


def _files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def _good_fields(**over):
    fields = dict(run_id="r1", holder="h1", generation=1, ttl_seconds=1800, acquired_at=T0,
                  renewed_at=T0, expires_at="2026-10-12T10:30:00Z", holder_pid=100,
                  holder_boot="boot-1", holder_host="host-a", holder_pidns="pid:[1]",
                  holder_start="1000")
    fields.update(over)
    return fields


class _Alarm:
    def __init__(self, seconds):
        self.seconds = seconds

    def __enter__(self):
        def fire(*_):
            raise AssertionError(f"still running after {self.seconds} s")
        self.old = signal.signal(signal.SIGALRM, fire)
        signal.alarm(self.seconds)

    def __exit__(self, *exc):
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self.old)


# --- R1: the vocabulary and the grammar ---------------------------------------------------------


def test_the_record_vocabulary_and_every_field_grammar_are_exactly_these():
    import hoh.runlease as runlease
    from hoh.runlease import RunLease, RunLeaseError

    assert runlease.SCHEMA == "veriharness.run-lease/1"
    assert runlease.DIRNAME == "run-leases"
    assert (runlease.LIVE, runlease.LOST, runlease.RELEASED, runlease.NONE) == (
        "LIVE", "LOST", "RELEASED", "NONE")
    assert RunLease(**_good_fields()).as_dict() == _good_fields() | {
        "released_at": None, "released_reason": ""}

    def refused(**over):
        with pytest.raises(RunLeaseError) as err:
            RunLease(**_good_fields(**over))
        assert err.value.reason == "invalid"

    # names: 64 characters ok, 65 refused; no trailing newline, dot or underscore first, space, other scripts
    RunLease(**_good_fields(run_id="a" * 64, holder="b" * 64))
    RunLease(**_good_fields(run_id="9a.b_c-d"))
    for bad in ("a" * 65, "", "r1\n", ".r1", "_r1", "-r1", "r 1", "r/1", "r\x00", "\u0663", "r\u00e9",
                "..", None, 1, True, b"r1", ["r1"]):
        refused(run_id=bad)
        refused(holder=bad)
    refused(run_id="backups")
    refused(run_id="projects")
    RunLease(**_good_fields(holder="backups"))
    # generation, ttl, pid: integers in a closed range, never a bool or a float
    RunLease(**_good_fields(generation=1000000000))
    for bad in (0, -1, 1000000001, True, 1.0, "1", None):
        refused(generation=bad)
    RunLease(**_good_fields(ttl_seconds=1, expires_at="2026-10-12T10:00:01Z"))
    RunLease(**_good_fields(ttl_seconds=86400, expires_at="2026-10-13T10:00:00Z"))
    for bad in (0, -1, 86401, True, 1800.0, "1800", None):
        refused(ttl_seconds=bad)
    RunLease(**_good_fields(holder_pid=1))
    RunLease(**_good_fields(holder_pid=2147483647))
    for bad in (0, -1, 2147483648, True, 1.0, "100", None):
        refused(holder_pid=bad)
    # times: ASCII, real calendar time, `...Z`
    RunLease(**_good_fields(acquired_at="2024-02-29T10:00:00Z", renewed_at="2024-02-29T10:00:00Z",
                            expires_at="2024-02-29T10:30:00Z"))
    for bad in ("2026-13-01T10:00:00Z", "2026-02-30T10:00:00Z", "2026-10-12T10:00:60Z",
                "0000-10-12T10:00:00Z", "2026-10-12T24:00:00Z", "2026-10-12T10:00:00",
                "2026-10-12 10:00:00Z", "2026-10-12T10:00:00Z\n", "\u0662026-10-12T10:00:00Z",
                "\u0662\u0660\u0662\u0666-10-12T10:00:00Z", "2026-10-12T10:00:00z", None, 7):
        refused(acquired_at=bad)
    # text: printable ASCII, at most 256 characters
    RunLease(**_good_fields(holder_host="h" * 256, holder_boot="", holder_pidns="", holder_start=""))
    for what in ("holder_host", "holder_boot", "holder_pidns", "holder_start"):
        for bad in ("h" * 257, "a\nb", "a\tb", "a\x7fb", "caf\u00e9", None, 1):
            refused(**{what: bad})
    # relations: expiry is renewal plus ttl; renewal is not before acquisition; release is both or neither
    refused(expires_at="2026-10-12T10:30:01Z")
    refused(renewed_at="2026-10-12T09:59:59Z", expires_at="2026-10-12T10:29:59Z")
    refused(released_reason="why")
    refused(released_at=T0)
    refused(released_at=T0, released_reason="   ")
    refused(released_at="2026-10-12T09:00:00Z", released_reason="why")
    RunLease(**_good_fields(released_at="2026-10-12T10:05:00Z", released_reason="done"))


# --- R2: odd bytes -------------------------------------------------------------------------------


def test_odd_bytes_and_odd_json_are_refused_with_their_reason_never_read_as_a_lease():
    from hoh.runlease import RunLease, RunLeaseError, parse, render

    good = render(RunLease(**_good_fields()))
    assert good.isascii() and good.endswith(b"}\n") and not good.endswith(b"\n\n")
    assert parse(good) == RunLease(**_good_fields())
    assert json.loads(good)["schema"] == "veriharness.run-lease/1"
    assert list(json.loads(good)) == sorted(json.loads(good))
    assert len(good) < 1024
    text = good.decode("ascii")

    def reason(data):
        with pytest.raises(RunLeaseError) as err:
            parse(data)
        return err.value.reason

    document = json.loads(good)
    assert reason(text) == "malformed" and reason(bytearray(good)) == "malformed" and reason(None) == "malformed"
    assert reason(text.encode("utf-16")) == "malformed"
    assert reason(b"\xef\xbb\xbf" + good) == "malformed"
    assert reason(text.replace("r1", "r\u00e9").encode("latin-1")) == "malformed"
    assert reason(good.replace(b'"r1"', b'"r1\\u0000"')) == "invalid"
    assert reason(good.replace(b'"h1"', b'"h\\ud800"')) == "invalid"
    assert reason(good[:-20]) == "malformed"
    assert reason(b"[" * 4000) == "malformed"
    for odd in (b"[]\n", b"null\n", b"7\n", b'"x"\n', b"", b"\n\n"):
        assert reason(odd) == "malformed"
    # exactly 4096 bytes is looked at (and is not JSON), 4097 is oversize
    assert reason(b" " * 4096) == "malformed" and reason(b" " * 4097) == "oversize"
    assert reason(b" " * 10 ** 6) == "oversize"
    # a repeated key, NaN, Infinity
    assert reason(good.replace(b'"holder": "h1",', b'"holder": "h1", "holder": "h2",')) == "malformed"
    assert reason(good.replace(b'"holder_pid": 100', b'"holder_pid": NaN')) == "malformed"
    assert reason(good.replace(b'"holder_pid": 100', b'"holder_pid": Infinity')) == "malformed"
    assert reason(good.replace(b'"holder_pid": 100', b'"holder_pid": ' + b"9" * 40)) == "invalid"
    assert reason(good.replace(b'"holder_pid": 100', b'"holder_pid": 1.5')) == "invalid"
    # schema and keys
    for mutate in (lambda d: d.pop("schema"), lambda d: d.update(schema="veriharness.run-lease/2"),
                   lambda d: d.update(extra=1), lambda d: d.pop("holder"),
                   lambda d: d.pop("released_at"), lambda d: d.pop("ttl_seconds")):
        broken = dict(document)
        mutate(broken)
        assert reason((json.dumps(broken, sort_keys=True, indent=1) + "\n").encode()) == "schema"
    # a value that breaks a field rule is invalid, whatever its JSON type
    for name, bad in (("generation", True), ("generation", 1.0), ("generation", "1"), ("holder_pid", None),
                      ("ttl_seconds", [1800]), ("run_id", {"a": 1}), ("released_at", 5),
                      ("released_reason", 5), ("holder", None), ("acquired_at", "yesterday")):
        broken = dict(document)
        broken[name] = bad
        assert reason((json.dumps(broken, sort_keys=True, indent=1) + "\n").encode()) == "invalid"
    # valid content in a spelling render() would not produce
    for data in (json.dumps(document, sort_keys=True).encode() + b"\n",
                 (json.dumps(document, sort_keys=True, indent=4) + "\n").encode(),
                 (json.dumps(dict(reversed(list(document.items()))), indent=1) + "\n").encode(),
                 good[:-1], good + b"\n", b"\n" + good, good.replace(b"\n", b"\r\n"),
                 good.replace(b'"r1"', b'"\\u0072\\u0031"')):
        assert reason(data) == "noncanonical"
    with pytest.raises(RunLeaseError):
        render({"run_id": "r1"})


# --- R3: acquire ---------------------------------------------------------------------------------


def test_acquire_takes_a_free_run_once_and_a_live_holder_is_never_displaced(tmp_path, w):
    from hoh.runlease import RunLeaseError, RunLeaseHeld, parse, render

    old_umask = os.umask(0)
    try:
        d = tmp_path / "project" / "run-leases"
        a = w.leases(d, 100)
        lease = a.acquire("r1", "orch-a", ttl_seconds=1800)
    finally:
        os.umask(old_umask)
    assert (lease.run_id, lease.holder, lease.generation, lease.ttl_seconds) == ("r1", "orch-a", 1, 1800)
    assert (lease.acquired_at, lease.renewed_at, lease.expires_at) == (T0, T0, "2026-10-12T10:30:00Z")
    assert (lease.holder_pid, lease.holder_boot, lease.holder_host) == (100, "boot-1111-2222", "host-a")
    assert (lease.holder_pidns, lease.holder_start) == ("pid:[4026531836]", "1000")
    assert lease.released_at is None and lease.released_reason == ""
    assert _files(d) == [".lock", "r1.json"]
    assert (d / "r1.json").read_bytes() == render(lease)
    assert parse((d / "r1.json").read_bytes()) == lease
    assert stat.S_IMODE(os.stat(d / "r1.json").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(d / ".lock").st_mode) == 0o600
    assert (d / ".lock").read_bytes() == b""
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert a.read("r1") == lease
    assert a.status("r1")[0] == "LIVE"
    # a live holder is never displaced: by another session, by the same session, from another handle
    before = (d / "r1.json").read_bytes()
    w.spawn(200)
    for who, handle in (("orch-b", w.leases(d, 200)), ("orch-a", a), ("orch-a", w.leases(d, 100))):
        with pytest.raises(RunLeaseHeld) as err:
            handle.acquire("r1", who, ttl_seconds=60)
        assert err.value.reason == "held" and isinstance(err.value, RunLeaseError)
    assert (d / "r1.json").read_bytes() == before and _files(d) == [".lock", "r1.json"]
    # another run in the same directory is independent
    other = w.leases(d, 200).acquire("r2", "orch-b", ttl_seconds=60)
    assert other.generation == 1 and _files(d) == [".lock", "r1.json", "r2.json"]
    # run ids that spell another run's file or park stay runs of their own
    for look_alike in ("r1.json", "r1.json.g1", "R1"):
        assert w.leases(d, 200).acquire(look_alike, "orch-b", ttl_seconds=60).generation == 1
    assert _files(d) == [".lock", "R1.json", "r1.json", "r1.json.g1.json", "r1.json.json", "r2.json"]
    assert a.read("r1").holder == "orch-a" and a.read("r1.json").holder == "orch-b"
    # a refused request creates nothing: not the directory, not the lock
    fresh = tmp_path / "never"
    for kwargs in (dict(run_id="../x", holder="h", ttl_seconds=60), dict(run_id="r", holder="", ttl_seconds=60),
                   dict(run_id="r", holder="h", ttl_seconds=0), dict(run_id="r", holder="h", ttl_seconds=86401),
                   dict(run_id="r", holder="h", ttl_seconds=True), dict(run_id="r", holder="h", ttl_seconds=60.0)):
        with pytest.raises(RunLeaseError) as err:
            w.leases(fresh, 100).acquire(**kwargs)
        assert err.value.reason == "invalid"
    assert not fresh.exists()


# --- R4: judging and takeover --------------------------------------------------------------------


def test_a_lost_or_released_run_is_taken_over_at_the_next_generation_and_the_old_record_is_kept(tmp_path, w):
    from hoh.runlease import RunLeaseHeld, parse, render

    d = tmp_path / "run-leases"
    a = w.leases(d, 100)
    first = a.acquire("r1", "orch-a", ttl_seconds=1800)
    assert a.judge(first)[0] == "LIVE"
    # LIVE: process alive and within the ttl, also seen from another process of the same machine
    w.advance(1799)
    assert a.status("r1")[0] == "LIVE"
    w.spawn(200)
    b = w.leases(d, 200)
    assert b.status("r1")[0] == "LIVE"
    # LOST: the ttl ran out although the process is alive
    w.advance(1)
    state, why = b.status("r1")
    assert state == "LOST" and "expired at 2026-10-12T10:30:00Z" in why
    # takeover: generation 2, the replaced record parked under its generation
    second = b.acquire("r1", "orch-b", ttl_seconds=600)
    assert (second.generation, second.holder, second.holder_pid) == (2, "orch-b", 200)
    assert second.acquired_at == w.now and second.expires_at == _later(w.now, 600)
    assert _files(d) == [".lock", "r1.json", "r1.json.g1"]
    assert (d / "r1.json.g1").read_bytes() == render(first)
    assert stat.S_IMODE(os.stat(d / "r1.json.g1").st_mode) == 0o600
    assert parse((d / "r1.json").read_bytes()) == second
    # LOST: the process is gone (no waiting for the ttl)
    w.kill(200)
    state, why = a.status("r1")
    assert state == "LOST" and "(pid 200) no longer exists" in why
    w.spawn(100)
    third = a.acquire("r1", "orch-a", ttl_seconds=600)
    assert third.generation == 3 and _files(d) == [".lock", "r1.json", "r1.json.g1", "r1.json.g2"]
    assert (d / "r1.json.g2").read_bytes() == render(second)
    # LOST: the pid was reused by another process (start time differs)
    w.kill(100)
    w.spawn(100, start="2000")
    state, why = b.status("r1")
    assert state == "LOST" and "now belongs to another process" in why
    # LOST: another boot, whatever the pid says
    w.kill(100)
    w.spawn(100, start="1000")
    assert a.status("r1")[0] == "LIVE"
    w.boot = "boot-9999-8888"
    state, why = a.status("r1")
    assert state == "LOST" and "the machine restarted" in why and "boot-111" in why
    fourth = a.acquire("r1", "orch-a", ttl_seconds=600)
    assert fourth.generation == 4 and fourth.holder_boot == "boot-9999-8888"
    # RELEASED beats everything, and its holder can be taken over from at once
    released = a.release(fourth, reason="done")
    assert a.status("r1")[0] == "RELEASED" and released.released_reason == "done"
    fifth = b.acquire("r1", "orch-b", ttl_seconds=600)
    assert fifth.generation == 5
    assert (d / "r1.json.g4").read_bytes() == render(released)
    # a holder in another pid namespace is judged by the ttl alone: a pid means nothing there
    other_ns = w.leases(d, 301, pidns=lambda: "pid:[999]")
    assert b.status("r1")[0] == "LOST" and other_ns.status("r1")[0] == "LIVE"
    # a holder judged from another host is judged by the ttl alone
    c = w.leases(d, 300, host="host-b")
    assert c.status("r1")[0] == "LIVE"
    w.kill(200)
    assert c.status("r1")[0] == "LIVE" and b.status("r1")[0] == "LOST"
    w.advance(601)
    assert c.status("r1")[0] == "LOST"
    # a live holder of the next generation is still not displaced
    w.spawn(300)
    c.acquire("r1", "orch-c", ttl_seconds=60)
    with pytest.raises(RunLeaseHeld):
        a.acquire("r1", "orch-a", ttl_seconds=60)


# --- R5: renew -----------------------------------------------------------------------------------


def test_renew_extends_only_the_current_grant_and_a_replaced_or_run_out_holder_is_refused(tmp_path, w):
    from hoh.runlease import RunLeaseError, StaleRunLease, render
    from dataclasses import replace

    d = tmp_path / "run-leases"
    a = w.leases(d, 100)
    first = a.acquire("r1", "orch-a", ttl_seconds=600)
    w.advance(400)
    renewed = a.renew(first)
    assert (renewed.generation, renewed.acquired_at, renewed.holder) == (1, T0, "orch-a")
    assert renewed.renewed_at == w.now == "2026-10-12T10:06:40Z"
    assert renewed.expires_at == "2026-10-12T10:16:40Z"
    assert (d / "r1.json").read_bytes() == render(renewed) and a.read("r1") == renewed
    assert _files(d) == [".lock", "r1.json"]
    # the old object is still the same grant: renewing it again is fine
    w.advance(100)
    assert a.renew(first).renewed_at == w.now
    # a clock that reads earlier than the last renewal refuses and writes nothing
    before = (d / "r1.json").read_bytes()
    w.now = "2026-10-12T10:00:30Z"
    with pytest.raises(RunLeaseError) as err:
        a.renew(first)
    assert err.value.reason == "clock" and (d / "r1.json").read_bytes() == before
    # a different holder, generation or process presented as the grant is refused, nothing written
    w.now = _later(T0, 600)
    for forged in (replace(first, holder="orch-b"), replace(first, generation=2),
                   replace(first, holder_pid=101), replace(first, holder_start="9"),
                   replace(first, holder_boot="boot-x"), replace(first, holder_host="host-z"),
                   replace(first, holder_pidns="pid:[2]"),
                   replace(first, ttl_seconds=601, expires_at=_later(T0, 601)),
                   replace(first, acquired_at=_later(T0, 1), renewed_at=_later(T0, 1),
                           expires_at=_later(T0, 601))):
        with pytest.raises(StaleRunLease) as err:
            a.renew(forged)
        assert err.value.reason == "stale"
    assert (d / "r1.json").read_bytes() == before
    for bad in (None, "r1", {"run_id": "r1"}):
        with pytest.raises(RunLeaseError) as err:
            a.renew(bad)
        assert err.value.reason == "invalid"
    # a renewal of a run nobody ever leased creates nothing, not even the directory
    never = tmp_path / "never-leased"
    with pytest.raises(StaleRunLease):
        w.leases(never, 100).renew(first)
    assert not never.exists()
    # a lease that ran out is not renewable even if nobody took it over: acquire again (next generation)
    w.now = "2026-10-12T10:20:00Z"
    assert a.status("r1")[0] == "LOST"
    with pytest.raises(StaleRunLease):
        a.renew(first)
    assert (d / "r1.json").read_bytes() == before
    # the boundary: one second before `expires_at` renews, at `expires_at` it is refused
    w.now = "2026-10-12T11:00:00Z"
    edge = a.acquire("edge", "orch-a", ttl_seconds=60)
    w.advance(59)
    edge = a.renew(edge)
    assert edge.expires_at == _later(w.now, 60)
    w.advance(60)
    assert a.judge(edge)[0] == "LOST"
    with pytest.raises(StaleRunLease):
        a.renew(edge)
    # a replaced holder cannot renew over its successor
    w.now = "2026-10-12T10:20:00Z"
    w.spawn(200)
    second = w.leases(d, 200).acquire("r1", "orch-b", ttl_seconds=600)
    taken = (d / "r1.json").read_bytes()
    with pytest.raises(StaleRunLease):
        a.renew(first)
    assert (d / "r1.json").read_bytes() == taken
    # a renewal of a lease that is not on disk at all
    with pytest.raises(StaleRunLease):
        a.renew(replace(second, run_id="r9"))
    assert not (d / "r9.json").exists()


# --- R6: release and status ----------------------------------------------------------------------


def test_release_ends_only_the_current_grant_with_a_reason_and_a_status_for_an_unknown_run_is_none(tmp_path, w):
    from hoh.runlease import RunLeaseError, StaleRunLease, render
    from dataclasses import replace

    d = tmp_path / "run-leases"
    a = w.leases(d, 100)
    assert a.read("r1") is None and a.status("r1") == ("NONE", "no lease has ever been written for this run")
    assert not d.exists()
    first = a.acquire("r1", "orch-a", ttl_seconds=600)
    never = tmp_path / "never-leased"
    with pytest.raises(StaleRunLease):
        w.leases(never, 100).release(first, reason="nothing was taken")
    assert not never.exists()
    for bad in ("", "   ", "a\nb", "x" * 257, None, 5):
        with pytest.raises(RunLeaseError) as err:
            a.release(first, reason=bad)
        assert err.value.reason == "invalid"
    assert a.read("r1") == first
    w.advance(30)
    released = a.release(first, reason="node settled")
    assert released.released_at == w.now and released.released_reason == "node settled"
    assert released.renewed_at == T0 and released.generation == 1
    assert (d / "r1.json").read_bytes() == render(released)
    assert a.status("r1")[0] == "RELEASED"
    # released twice, renewed after release: refused
    with pytest.raises(StaleRunLease):
        a.release(first, reason="again")
    with pytest.raises(StaleRunLease):
        a.renew(first)
    assert (d / "r1.json").read_bytes() == render(released)
    # a replaced holder cannot release its successor's lease
    w.spawn(200)
    second = w.leases(d, 200).acquire("r1", "orch-b", ttl_seconds=600)
    with pytest.raises(StaleRunLease):
        a.release(first, reason="late")
    with pytest.raises(StaleRunLease):
        a.release(replace(second, holder="orch-a"), reason="forged")
    assert a.read("r1") == second
    # after the ttl ran out and nobody took over, the holder may still say it is done
    third_holder = w.leases(d, 200)
    w.advance(601)
    assert third_holder.status("r1")[0] == "LOST"
    done = third_holder.release(second, reason="finished late")
    assert third_holder.status("r1")[0] == "RELEASED" and done.generation == 2
    # a release with a clock before the last renewal is refused
    third = third_holder.acquire("r1", "orch-b", ttl_seconds=600)
    w.now = "2026-10-12T09:00:00Z"
    with pytest.raises(RunLeaseError) as err:
        third_holder.release(third, reason="clock")
    assert err.value.reason == "clock"


# --- R7: odd files -------------------------------------------------------------------------------


def test_a_lease_file_that_cannot_be_read_exactly_is_never_a_free_run(tmp_path, w):
    from hoh.runlease import RunLeaseError, parse, render

    a = w.leases(tmp_path / "unused", 100)
    good = a.acquire("r1", "orch-a", ttl_seconds=600)
    good_bytes = render(good)

    def arrange(name, make):
        d = tmp_path / name
        d.mkdir()
        make(d)
        return d

    def second_name(d):
        (d / "other.json").write_bytes(good_bytes)
        os.link(d / "other.json", d / "r1.json")

    def fifo(d):
        os.mkfifo(d / "r1.json")

    def no_mode(d):
        (d / "r1.json").write_bytes(good_bytes)
        os.chmod(d / "r1.json", 0)

    cases = {
        "dangling": (lambda d: os.symlink(d / "nowhere", d / "r1.json"), "unreadable"),
        "link-to-good": (lambda d: (( d / "real.json").write_bytes(good_bytes),
                                    os.symlink(d / "real.json", d / "r1.json")), "unreadable"),
        "directory": (lambda d: (d / "r1.json").mkdir(), "unreadable"),
        "fifo": (fifo, "unreadable"),
        "second-name": (second_name, "unreadable"),
        "empty": (lambda d: (d / "r1.json").write_bytes(b""), "malformed"),
        "truncated": (lambda d: (d / "r1.json").write_bytes(good_bytes[:100]), "malformed"),
        "oversize": (lambda d: (d / "r1.json").write_bytes(good_bytes + b" " * 4096), "oversize"),
        "noncanonical": (lambda d: (d / "r1.json").write_bytes(good_bytes.replace(b'"r1"', b'"r1" ')), "noncanonical"),
        "other-run": (lambda d: (d / "r1.json").write_bytes(render(good.__class__(**{
            **good.as_dict(), "run_id": "r2"}))), "invalid"),
    }
    if os.geteuid() != 0:
        cases["mode-000"] = (no_mode, "unreadable")
    with _Alarm(20):
        for name, (make, reason) in cases.items():
            d = arrange(name, make)
            before = sorted((p.name, p.is_symlink()) for p in d.iterdir())
            probe = w.leases(d, 200)
            for call in (lambda: probe.read("r1"), lambda: probe.status("r1"),
                         lambda: probe.acquire("r1", "orch-b", ttl_seconds=600),
                         lambda: probe.renew(good), lambda: probe.release(good, reason="x")):
                with pytest.raises(RunLeaseError) as err:
                    call()
                assert err.value.reason == reason, name
            assert sorted(p.name for p in d.iterdir() if p.name != ".lock") == sorted(
                n for n, _ in before if n != ".lock"), name
    # the control: the same record in a plain file reads, and nothing but a missing name is "no lease"
    ok = arrange("control", lambda d: (d / "r1.json").write_bytes(good_bytes))
    assert w.leases(ok, 200).read("r1") == good
    assert w.leases(ok, 200).read("r2") is None
    assert parse(good_bytes) == good


# --- R8: the directory and the lock ---------------------------------------------------------------


def test_a_directory_or_lock_that_is_not_a_plain_directory_or_file_refuses_every_write(tmp_path, w):
    from hoh.runlease import RunLeaseError

    real = tmp_path / "real"
    real.mkdir()
    cases = {}
    link = tmp_path / "link"
    os.symlink(real, link)
    cases["dir-is-a-link"] = link
    plain = tmp_path / "file"
    plain.write_bytes(b"x")
    cases["dir-is-a-file"] = plain
    dangling = tmp_path / "dangling"
    os.symlink(tmp_path / "nowhere", dangling)
    cases["dir-is-a-dangling-link"] = dangling
    lock_link = tmp_path / "lock-link"
    lock_link.mkdir()
    os.symlink(tmp_path / "nowhere", lock_link / ".lock")
    cases["lock-is-a-link"] = lock_link
    lock_dir = tmp_path / "lock-dir"
    (lock_dir / ".lock").mkdir(parents=True)
    cases["lock-is-a-directory"] = lock_dir
    lock_fifo = tmp_path / "lock-fifo"
    lock_fifo.mkdir()
    os.mkfifo(lock_fifo / ".lock")
    cases["lock-is-a-fifo"] = lock_fifo
    lock_two = tmp_path / "lock-two-names"
    lock_two.mkdir()
    (lock_two / "elsewhere").write_bytes(b"")
    os.link(lock_two / "elsewhere", lock_two / ".lock")
    cases["lock-has-two-names"] = lock_two
    with _Alarm(20):
        for name, d in cases.items():
            with pytest.raises(RunLeaseError) as err:
                w.leases(d, 100).acquire("r1", "orch-a", ttl_seconds=600)
            assert err.value.reason == "unreadable", name
    for name in ("dir-is-a-link", "dir-is-a-file", "dir-is-a-dangling-link"):
        for call in (lambda d: w.leases(d, 100).read("r1"), lambda d: w.leases(d, 100).status("r1")):
            with pytest.raises(RunLeaseError) as err:
                call(cases[name])
            assert err.value.reason == "unreadable", name
    assert sorted(p.name for p in real.iterdir()) == []
    assert not (tmp_path / "nowhere").exists()


# --- R9: the park --------------------------------------------------------------------------------


def test_a_takeover_parks_the_replaced_record_first_and_refuses_when_it_cannot(tmp_path, w, monkeypatch):
    from dataclasses import replace

    import hoh.runlease as runlease
    from hoh.runlease import RunLeaseError, render

    d = tmp_path / "run-leases"
    a = w.leases(d, 100)
    first = a.acquire("r1", "orch-a", ttl_seconds=60)
    w.advance(61)
    b = w.leases(d, 200)

    # a copy that differs is a conflict: nothing is replaced
    (d / "r1.json.g1").write_bytes(b"not the record")
    with pytest.raises(RunLeaseError) as err:
        b.acquire("r1", "orch-b", ttl_seconds=60)
    assert err.value.reason == "conflict"
    assert a.read("r1") == first and (d / "r1.json.g1").read_bytes() == b"not the record"
    # the same bytes (a takeover that died after parking, retried) are fine and are not rewritten
    (d / "r1.json.g1").write_bytes(render(first))
    second = b.acquire("r1", "orch-b", ttl_seconds=60)
    assert second.generation == 2 and (d / "r1.json.g1").read_bytes() == render(first)

    # the last generation cannot be taken over, and nothing is parked for the refusal
    ceiling = tmp_path / "ceiling"
    ceiling.mkdir()
    last = replace(first, run_id="r9", generation=1000000000)
    (ceiling / "r9.json").write_bytes(render(last))
    with pytest.raises(RunLeaseError) as err:
        w.leases(ceiling, 200).acquire("r9", "orch-b", ttl_seconds=60)
    assert err.value.reason == "invalid"
    assert sorted(p.name for p in ceiling.iterdir() if p.name != ".lock") == ["r9.json"]
    assert (ceiling / "r9.json").read_bytes() == render(last)

    # a copy that cannot be made refuses the takeover and leaves the lease
    w.advance(61)
    w.spawn(300)
    boom = OSError(errno.ENOSPC, "no space")
    monkeypatch.setattr(runlease.durable_io, "durable_create", lambda *args, **kw: (_ for _ in ()).throw(boom))
    c = w.leases(d, 300)
    with pytest.raises(OSError) as err:
        c.acquire("r1", "orch-c", ttl_seconds=60)
    assert err.value is boom
    assert a.read("r1") == second and not (d / "r1.json.g2").exists()
    monkeypatch.undo()

    # the park comes before the replace
    order = []
    real_create, real_write = runlease.durable_io.durable_create, runlease.durable_io.durable_write
    monkeypatch.setattr(runlease.durable_io, "durable_create",
                        lambda path, *args, **kw: (order.append(("create", Path(path).name)),
                                                   real_create(path, *args, **kw))[1])
    monkeypatch.setattr(runlease.durable_io, "durable_write",
                        lambda path, *args, **kw: (order.append(("write", Path(path).name)),
                                                   real_write(path, *args, **kw))[1])
    third = c.acquire("r1", "orch-c", ttl_seconds=60)
    assert third.generation == 3
    assert order == [("create", "r1.json.g2"), ("write", "r1.json")]


# --- R10: the lock --------------------------------------------------------------------------------


def test_the_short_lock_is_held_across_decide_and_write_and_a_busy_lock_is_refused_in_time(tmp_path, w, monkeypatch):
    import hoh.runlease as runlease
    from hoh.runlease import RunLeaseBusy, RunLeaseError, RunLeaseHeld

    d = tmp_path / "run-leases"
    # bounds of the wait
    for bad in (-1, 61, True, "1", None, float("nan")):
        with pytest.raises(RunLeaseError) as err:
            w.leases(d, 100, lock_wait_seconds=bad)
        assert err.value.reason == "invalid"
    w.leases(d, 100, lock_wait_seconds=0)
    w.leases(d, 100, lock_wait_seconds=60)
    with pytest.raises(RunLeaseError) as err:
        w.leases(tmp_path / "a\x00b", 100)
    assert err.value.reason == "invalid"
    assert not d.exists()

    # while another process holds the lock the request is refused, and not later than its wait
    a = w.leases(d, 100, lock_wait_seconds=0.3)
    a.acquire("r0", "orch-a", ttl_seconds=60)
    fd = os.open(d / ".lock", os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with _Alarm(10):
            started = time.monotonic()
            with pytest.raises(RunLeaseBusy) as err:
                a.acquire("r1", "orch-a", ttl_seconds=60)
            waited = time.monotonic() - started
        assert err.value.reason == "busy" and 0.25 <= waited < 5
        assert not (d / "r1.json").exists()
        with _Alarm(10):
            for call in (lambda: a.renew(a.read("r0")), lambda: a.release(a.read("r0"), reason="x")):
                with pytest.raises(RunLeaseBusy):
                    call()
        # reads do not need the lock
        assert a.read("r0").holder == "orch-a"
    finally:
        os.close(fd)
    a.acquire("r1", "orch-a", ttl_seconds=60)

    # the lock is held while the record is written: a second lock attempt from here fails at that moment
    seen = []
    real_write = runlease.durable_io.durable_write

    def spying_write(path, *args, **kw):
        probe = os.open(d / ".lock", os.O_RDONLY)
        try:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("free")
                fcntl.flock(probe, fcntl.LOCK_UN)
            except BlockingIOError:
                seen.append("held")
        finally:
            os.close(probe)
        return real_write(path, *args, **kw)

    monkeypatch.setattr(runlease.durable_io, "durable_write", spying_write)
    lease = a.acquire("r2", "orch-a", ttl_seconds=60)
    w.advance(10)
    lease = a.renew(lease)
    a.release(lease, reason="done")
    assert seen == ["held", "held", "held"]
    monkeypatch.undo()

    # eight racing acquirers: exactly one wins, the others are told the run is held
    results = []
    barrier = threading.Barrier(8)

    def race(i):
        handle = w.leases(d, 1000 + i, lock_wait_seconds=30)
        barrier.wait()
        try:
            results.append(handle.acquire("race", f"orch-{i}", ttl_seconds=600))
        except RunLeaseHeld:
            results.append(None)

    for i in range(8):
        w.spawn(1000 + i)
    threads = [threading.Thread(target=race, args=(i,)) for i in range(8)]
    with _Alarm(60):
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    winners = [r for r in results if r is not None]
    assert len(results) == 8 and len(winners) == 1 and winners[0].generation == 1
    assert a.read("race") == winners[0]


# --- R11: the project store ----------------------------------------------------------------------


def test_a_project_store_hands_out_its_own_run_leases_and_creates_nothing_until_one_is_taken(tmp_path, w):
    from hoh.projectstore import ProjectStore, UnsafeProjectId, list_projects
    from hoh.runlease import RunLeases

    store = ProjectStore(tmp_path, "p1")
    leases = store.run_leases()
    assert isinstance(leases, RunLeases)
    assert leases.dir == tmp_path.resolve() / "projects" / "p1" / "run-leases"
    assert not (tmp_path / "projects").exists()
    assert list_projects(tmp_path) == []
    # the facts are the caller's; the directory is not
    w.spawn(100)
    env = dict(clock=lambda: w.now, boot=lambda: w.boot, host="h", pid=100, alive=lambda p: True,
               pidns=lambda: "ns", start=lambda p: "1")
    lease = store.run_leases(**env).acquire("r1", "orch-a", ttl_seconds=60)
    assert lease.acquired_at == T0 and lease.holder_pid == 100 and lease.holder_host == "h"
    assert sorted(p.name for p in (store.dir / "run-leases").iterdir()) == [".lock", "r1.json"]
    assert store.run_leases(**env).read("r1") == lease
    assert not store.exists() and list_projects(tmp_path) == []
    with pytest.raises(TypeError):
        store.run_leases(directory=tmp_path / "elsewhere")
    # a project id that is not a name never reaches the lease directory
    for bad in ("../p1", "p1\n", "", ".p"):
        with pytest.raises(UnsafeProjectId):
            ProjectStore(tmp_path, bad).run_leases()
    # the run directory's name is a leaf of its project: two projects keep separate leases
    other = ProjectStore(tmp_path, "p2").run_leases(**env)
    assert other.read("r1") is None
    assert other.acquire("r1", "orch-b", ttl_seconds=60).generation == 1


# --- R12: the clock and the environment ----------------------------------------------------------


def test_time_comes_only_from_the_injected_clock_and_environment_text_is_escaped_to_the_grammar(tmp_path, w):
    from hoh.runlease import RunLeaseError

    # no clock of its own: the injected one is the only time in the record, also at the calendar's edges
    d = tmp_path / "a"
    w.now = "0001-01-01T00:00:00Z"
    lease = w.leases(d, 100).acquire("r1", "orch-a", ttl_seconds=1800)
    assert (lease.acquired_at, lease.expires_at) == ("0001-01-01T00:00:00Z", "0001-01-01T00:30:00Z")
    assert json.loads((d / "r1.json").read_text())["expires_at"] == "0001-01-01T00:30:00Z"
    w.now = "9999-12-31T23:59:59Z"
    with pytest.raises(RunLeaseError) as err:
        w.leases(d, 100).acquire("r2", "orch-a", ttl_seconds=1)
    assert err.value.reason == "invalid" and not (d / "r2.json").exists()
    w.now = "2024-02-28T23:59:59Z"
    assert w.leases(d, 100).acquire("r3", "orch-a", ttl_seconds=1).expires_at == "2024-02-29T00:00:00Z"
    # a clock that answers anything but a time is refused and nothing is written
    for bad in (None, "now", "2026-10-12T10:00:00Z\n", 1760263200, "2026-10-12T10:00:00"):
        h = w.leases(tmp_path / "b", 100, clock=lambda bad=bad: bad)
        with pytest.raises(RunLeaseError) as err:
            h.acquire("r1", "orch-a", ttl_seconds=60)
        assert err.value.reason == "invalid"
    assert not (tmp_path / "b").exists() or not (tmp_path / "b" / "r1.json").exists()
    # host, boot and namespace text is escaped, bounded and judged the same way on both sides
    w.now = T0
    w.host = "h\u00e9\nst-" + "x" * 400
    w.boot = "b\u00f6\u00f6t"
    w.pidns = "ns\t1"
    w.starts[100] = "12\u00e9"
    c = tmp_path / "c"
    h = w.leases(c, 100)
    lease = h.acquire("r1", "orch-a", ttl_seconds=60)
    assert lease.holder_host.isascii() and "\n" not in lease.holder_host and len(lease.holder_host) == 256
    assert lease.holder_boot == "b\\xf6\\xf6t" and lease.holder_pidns == "ns\\t1"
    assert lease.holder_start == "12\\xe9"
    assert h.status("r1")[0] == "LIVE"
    w.kill(100)
    assert h.status("r1")[0] == "LOST"


# --- R13: what the module may do -----------------------------------------------------------------


def test_the_module_removes_nothing_signals_nothing_runs_nothing_and_writes_only_through_durable_io():
    import ast

    import hoh.runlease as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or ""))
    # a subset, not an equality: a module built differently may import less, never anything else
    assert imported <= {"__future__", "fcntl", "json", "os", "re", "stat", "time", "socket", "collections.abc",
                        "contextlib", "dataclasses", "datetime", "pathlib", ".", ".store", ".succession"}
    attrs = {(n.value.id, n.attr) for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    os_used = {attr for mod, attr in attrs if mod == "os"}
    assert os_used <= {"open", "close", "fstat", "lstat", "getpid", "PathLike", "O_RDONLY", "O_NOFOLLOW",
                       "O_NONBLOCK", "O_CLOEXEC"}
    assert {attr for mod, attr in attrs if mod == "time"} <= {"monotonic", "sleep"}
    assert {attr for mod, attr in attrs if mod == "datetime"} <= {"strptime"}
    assert {attr for mod, attr in attrs if mod == "fcntl"} <= {"flock", "LOCK_EX", "LOCK_NB"}
    assert {attr for mod, attr in attrs if mod == "durable_io"} <= {
        "durable_create", "durable_write", "read_single_link", "settled_stat", "DiskFullError"}
    assert {attr for mod, attr in attrs if mod == "socket"} <= {"gethostname"}
    all_attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for forbidden in ("unlink", "remove", "rmdir", "rename", "replace_file", "kill", "killpg", "system",
                      "popen", "Popen", "run", "check_output", "truncate", "write_text", "write_bytes",
                      "environ", "getenv", "now", "utcnow", "today", "time"):
        assert forbidden not in all_attrs, forbidden
    for forbidden in ("open", "exec", "eval", "print", "__import__", "subprocess", "shutil"):
        assert forbidden not in names, forbidden
    assert "subprocess" not in imported and "shutil" not in imported and "signal" not in imported


# --- R14: the defaults ---------------------------------------------------------------------------


def test_the_defaults_are_the_real_environment_and_not_a_value_the_tests_injected(tmp_path):
    import socket
    import subprocess
    import sys
    from dataclasses import replace

    from hoh.runlease import RunLeaseHeld, RunLeases, render

    def proc(path):
        try:
            return Path(path).read_text().strip()
        except OSError:
            return ""

    def start_ticks(pid):
        try:
            text = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return ""
        fields = text[text.rfind(")") + 2:].split()
        return fields[19] if len(fields) > 19 else ""

    d = tmp_path / "defaults"
    leases = RunLeases(d)
    assert leases.lock_wait_seconds == 5
    before = datetime.now(UTC)
    lease = leases.acquire("r1", "orch-default", ttl_seconds=600)
    after = datetime.now(UTC)
    # the real clock, to the second
    acquired = datetime.strptime(lease.acquired_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert before - timedelta(seconds=1) <= acquired <= after + timedelta(seconds=1)
    assert lease.expires_at == _later(lease.acquired_at, 600)
    # this process and this machine
    assert lease.holder_pid == os.getpid()
    assert lease.holder_host == socket.gethostname().encode("unicode_escape").decode("ascii")[:256]
    assert lease.holder_boot == proc("/proc/sys/kernel/random/boot_id")
    try:
        namespace = os.readlink("/proc/self/ns/pid")
    except OSError:
        namespace = ""
    assert lease.holder_pidns == namespace
    assert lease.holder_start == start_ticks(os.getpid())
    # live: judged by the real process, held against a second handle, renewed and released
    assert leases.status("r1")[0] == "LIVE"
    with pytest.raises(RunLeaseHeld):
        RunLeases(d).acquire("r1", "other", ttl_seconds=60)
    assert leases.renew(lease).generation == 1
    # a holder whose process is gone is LOST by the real liveness probe, and is taken over
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    (d / "r2.json").write_bytes(render(replace(lease, run_id="r2", holder_pid=child.pid)))
    state, why = leases.status("r2")
    assert state == "LOST" and f"(pid {child.pid}) no longer exists" in why
    assert leases.acquire("r2", "orch-default", ttl_seconds=60).generation == 2
    assert leases.release(leases.read("r1"), reason="done").released_reason == "done"


# === P3-18f: the repair slice (findings F1, F2, B-1, B-2, A-F3, A-F4, A-F5 of the post-tag reviews) ===
# Each node below is red on candidate 40a72197 for the reason its comment names and green on the repair.


def _nothing_made(directory: Path) -> bool:
    """The lease directory does not exist, or it is empty (not even the lock file)."""
    return (not directory.exists()) or sorted(directory.iterdir()) == []


# --- R15: a run id that is refused creates nothing, for every entry point (F1) --------------------


def test_a_reserved_or_odd_run_id_is_refused_before_anything_is_created_by_every_entry_point(tmp_path, w):
    from hoh.runlease import RunLeaseError

    ids = ("backups", "projects", "..", "r1\n", "", ".r1", "_r1", "a" * 65, "\u0663", None, 1, True, b"r1")
    for n, bad in enumerate(ids):
        for existing in (False, True):
            d = tmp_path / f"case-{n}-{existing}" / "run-leases"
            if existing:
                d.mkdir(parents=True)
            h = w.leases(d, 100)
            for name, call in (("acquire", lambda: h.acquire(bad, "orch-a", ttl_seconds=60)),
                               ("read", lambda: h.read(bad)), ("status", lambda: h.status(bad))):
                with pytest.raises(RunLeaseError) as err:
                    call()
                assert err.value.reason == "invalid", (bad, name)
            assert _nothing_made(d), (bad, existing, sorted(p.name for p in d.iterdir()) if d.exists() else None)
            if not existing:
                assert not d.exists() and not d.parent.joinpath("run-leases").exists()
    # the control: an ordinary id on the same kind of fresh directory is taken, and only then is anything made
    d = tmp_path / "control" / "run-leases"
    assert w.leases(d, 100).acquire("r1", "orch-a", ttl_seconds=60).generation == 1
    assert _files(d) == [".lock", "r1.json"]


# --- R16: an argument that is not a lease creates nothing, for renew and release (F2) -------------


def test_a_renew_or_release_of_something_that_is_not_a_lease_creates_nothing(tmp_path, w):
    from dataclasses import replace

    from hoh.runlease import RunLease, RunLeaseError, StaleRunLease

    donor = w.leases(tmp_path / "donor", 100).acquire("r1", "orch-a", ttl_seconds=60)
    stranger = replace(donor, run_id="r7")
    odd = (None, "x", "r1", 5, True, b"lease", {"run_id": "r1"}, ["r1"], object(), RunLease)
    for n, arg in enumerate(odd):
        for existing in (False, True):
            d = tmp_path / f"case-{n}-{existing}" / "run-leases"
            if existing:
                d.mkdir(parents=True)
            h = w.leases(d, 100)
            for name, call in (("renew", lambda: h.renew(arg)),
                               ("release", lambda: h.release(arg, reason="r"))):
                with pytest.raises(RunLeaseError) as err:
                    call()
                assert err.value.reason == "invalid", (arg, name)
            assert _nothing_made(d), (arg, existing)
    # a well-formed lease of a run nobody leased is `stale`, and makes nothing either
    for existing in (False, True):
        d = tmp_path / f"stranger-{existing}" / "run-leases"
        if existing:
            d.mkdir(parents=True)
        h = w.leases(d, 100)
        for call in (lambda: h.renew(stranger), lambda: h.release(stranger, reason="r")):
            with pytest.raises(StaleRunLease):
                call()
        assert _nothing_made(d)


# --- R17: a refusal after the decision leaves no parked copy behind (B-1) -------------------------


def test_a_takeover_that_is_refused_for_its_new_lease_parks_nothing(tmp_path, w):
    from hoh.runlease import RunLeaseError, render

    d = tmp_path / "run-leases"
    first = w.leases(d, 100).acquire("r1", "orch-a", ttl_seconds=60)
    w.advance(61)
    before = (d / "r1.json").read_bytes()
    assert before == render(first)
    # a pid that is not a pid, and a clock at the end of the calendar: refused, and nothing is parked
    for label, kwargs in (("pid 0", dict(pid=0)), ("negative pid", dict(pid=-5)), ("bool pid", dict(pid=True)),
                          ("end of the calendar", dict(clock=lambda: "9999-12-31T23:59:59Z"))):
        h = w.leases(d, 200, **kwargs) if "pid" not in kwargs else w.leases(d, 200)
        if "pid" in kwargs:
            h.pid = kwargs["pid"]
        with pytest.raises(RunLeaseError) as err:
            h.acquire("r1", "orch-b", ttl_seconds=60)
        assert err.value.reason == "invalid", label
        assert _files(d) == [".lock", "r1.json"], label
        assert (d / "r1.json").read_bytes() == before, label
    # a clock that is a time before the lock and the calendar's end under it: the refusal comes while the lease is
    # built, under the lock, and the replaced record is still not parked (the preflights above cannot see it)
    answers = iter([_later(T0, 61), "9999-12-31T23:59:59Z"])
    with pytest.raises(RunLeaseError) as err:
        w.leases(d, 200, clock=lambda: next(answers)).acquire("r1", "orch-b", ttl_seconds=60)
    assert err.value.reason == "invalid"
    assert _files(d) == [".lock", "r1.json"] and (d / "r1.json").read_bytes() == before
    # and the takeover that is allowed still parks, so the refusals above did not poison the name
    second = w.leases(d, 200).acquire("r1", "orch-b", ttl_seconds=60)
    assert second.generation == 2 and _files(d) == [".lock", "r1.json", "r1.json.g1"]
    assert (d / "r1.json.g1").read_bytes() == before


# --- R18: a clock that is not a time is refused before the directory or the lock exist (B-2) ------


def test_an_invalid_clock_is_refused_before_the_directory_and_the_lock_are_made(tmp_path, w):
    from dataclasses import replace

    from hoh.runlease import RunLeaseError, render

    bad_clocks = (None, "now", "2026-10-12T10:00:00Z\n", 1760263200, "2026-10-12T10:00:00",
                  "9999-12-31T23:59:59Z")          # the last is a time, but plus the ttl it is not one
    for n, bad in enumerate(bad_clocks):
        d = tmp_path / f"fresh-{n}" / "run-leases"
        with pytest.raises(RunLeaseError) as err:
            w.leases(d, 100, clock=lambda bad=bad: bad).acquire("r1", "orch-a", ttl_seconds=60)
        assert err.value.reason == "invalid", bad
        assert not d.exists(), bad
    # renew and release over a record that exists, in a directory that has no lock yet
    lease = w.leases(tmp_path / "donor", 100).acquire("r1", "orch-a", ttl_seconds=60)
    for n, bad in enumerate(bad_clocks):
        d = tmp_path / f"held-{n}" / "run-leases"
        d.mkdir(parents=True)
        (d / "r1.json").write_bytes(render(replace(lease, run_id="r1")))
        h = w.leases(d, 100, clock=lambda bad=bad: bad)
        # the last clock is a time: a renewal (which adds seconds) is refused, a release (which adds none) is allowed
        end_of_calendar = isinstance(bad, str) and bad.startswith("9999")
        calls = [lambda: h.renew(lease)]
        if not end_of_calendar:
            calls.append(lambda: h.release(lease, reason="r"))
        for call in calls:
            with pytest.raises(RunLeaseError) as err:
                call()
            assert err.value.reason == "invalid", bad
        assert sorted(p.name for p in d.iterdir()) == ["r1.json"], bad
        assert (d / "r1.json").read_bytes() == render(lease), bad
        if end_of_calendar:
            released = h.release(lease, reason="done at the end of the calendar")
            assert released.released_at == "9999-12-31T23:59:59Z" and released.generation == 1
            assert h.read("r1") == released
    # the stated limit: a clock that is a time the first time it is asked and not the second is refused
    # under the lock, after the directory exists (nothing is written but the empty lock file)
    answers = iter([T0, "not a time"])
    d = tmp_path / "flaky" / "run-leases"
    with pytest.raises(RunLeaseError) as err:
        w.leases(d, 100, clock=lambda: next(answers)).acquire("r1", "orch-a", ttl_seconds=60)
    assert err.value.reason == "invalid" and _files(d) == [".lock"]


# --- R19: an oversize file is refused without being read (A-F5) -----------------------------------


def test_an_oversize_file_is_refused_by_its_size_and_never_read_whole(tmp_path, w, monkeypatch):
    from hoh import runlease
    from hoh.runlease import RunLeaseError

    good = w.leases(tmp_path / "donor", 100).acquire("r1", "orch-a", ttl_seconds=60)
    calls = []
    real = runlease.durable_io.read_single_link
    monkeypatch.setattr(runlease.durable_io, "read_single_link",
                        lambda path: (calls.append(path), real(path))[1])
    # (the runner caps a file at 512 MiB, RLIMIT_FSIZE: the largest sparse file here is 256 MiB)
    for size in (4097, 1 << 20, 1 << 28):
        d = tmp_path / f"big-{size}"
        d.mkdir()
        (d / "r1.json").write_bytes(b"")
        os.truncate(d / "r1.json", size)           # a sparse file: no bytes on disk, a long read if it were read
        h = w.leases(d, 200)
        for name, call in (("read", lambda: h.read("r1")), ("status", lambda: h.status("r1")),
                           ("acquire", lambda: h.acquire("r1", "orch-b", ttl_seconds=60)),
                           ("renew", lambda: h.renew(good)), ("release", lambda: h.release(good, reason="r"))):
            with pytest.raises(RunLeaseError) as err:
                call()
            assert err.value.reason == "oversize", (size, name)
        assert os.stat(d / "r1.json").st_size == size
    assert calls == []
    # a parked copy is read under the same guard: a sparse foreign `<run_id>.json.g<N>` is refused by its size
    d = tmp_path / "park"
    held = w.leases(d, 100).acquire("r1", "orch-a", ttl_seconds=60)
    w.advance(61)
    (d / "r1.json.g1").write_bytes(b"")
    os.truncate(d / "r1.json.g1", 1 << 28)
    before = (d / "r1.json").read_bytes()
    calls.clear()
    with pytest.raises(RunLeaseError) as err:
        w.leases(d, 200).acquire("r1", "orch-b", ttl_seconds=60)
    assert err.value.reason == "oversize"
    assert [Path(c).name for c in calls] == ["r1.json"]       # the record was read, the park was not
    assert (d / "r1.json").read_bytes() == before and os.stat(d / "r1.json.g1").st_size == 1 << 28
    assert held.generation == 1
    calls.clear()
    # the boundary: exactly 4096 bytes is looked at (and is not a lease), 4097 is not
    d = tmp_path / "edge"
    d.mkdir()
    (d / "r1.json").write_bytes(b" " * 4096)
    with pytest.raises(RunLeaseError) as err:
        w.leases(d, 200).read("r1")
    assert err.value.reason == "malformed" and len(calls) == 1


# --- R20: the two limits the spec states are what the module does today (A-F3, A-F4) -------------


def test_the_stated_limits_a_takeover_that_died_and_a_caller_that_is_not_the_holder_are_pinned(tmp_path, w, monkeypatch):
    # These assertions pin TODAY's behaviour so the spec's prose cannot drift from the code. Slice 2
    # replaces the second block (Limits, Q11).
    from hoh import runlease
    from hoh.runlease import RunLeaseError

    # (a) a takeover dies after the park and before the write; the old holder then releases (allowed
    # after its ttl ran out): every later acquire is `conflict`, nothing is lost, and moving the park
    # aside (a versioned rename, nothing deleted) is the operator's way out.
    d = tmp_path / "died" / "run-leases"
    a = w.leases(d, 100)
    first = a.acquire("r1", "orch-a", ttl_seconds=60)
    w.advance(61)
    real = runlease.durable_io.durable_write
    state = {"armed": True}

    def dies_once(path, *args, **kw):
        if state["armed"] and Path(path).name == "r1.json":
            state["armed"] = False
            raise OSError(errno.EIO, "the process died here")
        return real(path, *args, **kw)

    monkeypatch.setattr(runlease.durable_io, "durable_write", dies_once)
    with pytest.raises(OSError):
        w.leases(d, 200).acquire("r1", "orch-b", ttl_seconds=60)
    assert _files(d) == [".lock", "r1.json", "r1.json.g1"] and a.read("r1") == first
    released = a.release(first, reason="done")
    with pytest.raises(RunLeaseError) as err:
        w.leases(d, 300).acquire("r1", "orch-c", ttl_seconds=60)
    assert err.value.reason == "conflict" and a.read("r1") == released
    assert (d / "r1.json.g1").read_bytes() != (d / "r1.json").read_bytes()
    os.rename(d / "r1.json.g1", d / "r1.json.g1.v1.20261013T000000Z")
    assert w.leases(d, 300).acquire("r1", "orch-c", ttl_seconds=60).generation == 2
    assert (d / "r1.json.g1").exists()
    monkeypatch.undo()

    # (b) a process that only read the record can renew and release a LIVE lease of another session
    d = tmp_path / "other" / "run-leases"
    holder = w.leases(d, 100)
    live = holder.acquire("r1", "orch-a", ttl_seconds=600)
    other = w.leases(d, 999)
    seen = other.read("r1")
    assert other.status("r1")[0] == "LIVE"
    assert other.renew(seen).generation == 1
    assert other.release(seen, reason="not mine to release").released_at is not None
    assert w.leases(d, 998).acquire("r1", "orch-d", ttl_seconds=60).generation == 2
    assert live.holder == "orch-a"


# === P3-18f, minors fold: the order of the early checks and the environment before the lock ==========


# --- R21: type, then record, then clock (A-N1, B-MINOR-1) ----------------------------------------


def test_renew_and_release_decide_the_type_then_the_record_then_the_clock_in_that_order(tmp_path, w):
    from dataclasses import replace

    from hoh.runlease import RunLeaseError, StaleRunLease, render

    donor = w.leases(tmp_path / "donor", 100).acquire("r1", "orch-a", ttl_seconds=60)
    d = tmp_path / "run-leases"
    d.mkdir()
    (d / "r1.json").write_bytes(render(donor))
    broken = w.leases(d, 100, clock=lambda: "now")
    # a well-formed lease of a run nobody leased: the record is decided before the clock is read
    stranger = replace(donor, run_id="r7")
    for call in (lambda: broken.renew(stranger), lambda: broken.release(stranger, reason="r")):
        with pytest.raises(StaleRunLease):
            call()
    # something that is not a lease: the type is decided before the record and before the clock
    for call in (lambda: broken.renew(None), lambda: broken.release("r1", reason="r")):
        with pytest.raises(RunLeaseError) as err:
            call()
        assert err.value.reason == "invalid" and "is not a RunLease" in str(err.value)
    # a lease that has a record: only now is the clock asked, and its answer is the refusal
    for call in (lambda: broken.renew(donor), lambda: broken.release(donor, reason="r")):
        with pytest.raises(RunLeaseError) as err:
            call()
        assert err.value.reason == "invalid" and "the clock's time" in str(err.value)
    # and an honest clock reads the same record and renews it
    assert w.leases(d, 100).renew(donor).generation == 1


# --- R22: an environment that is refused is refused before anything is made (A-N3) ----------------


def test_an_environment_that_cannot_describe_this_process_is_refused_before_anything_is_created(tmp_path, w):
    from hoh.runlease import RunLeaseError

    def raises(exc):
        def call(*args):
            raise exc
        return call

    cases = [
        ("pid 0", dict(pid=0), RunLeaseError),
        ("pid True", dict(pid=True), RunLeaseError),
        ("pid 2**31", dict(pid=2 ** 31), RunLeaseError),
        ("pid -1", dict(pid=-1), RunLeaseError),
        ("pid a string", dict(pid="100"), RunLeaseError),
        ("boot raises", dict(boot=raises(RuntimeError("no boot id"))), RuntimeError),
        ("pidns raises", dict(pidns=raises(RuntimeError("no namespace"))), RuntimeError),
        ("start raises", dict(start=raises(RuntimeError("no start time"))), RuntimeError),
    ]
    for n, (label, kwargs, expected) in enumerate(cases):
        d = tmp_path / f"case-{n}" / "run-leases"
        h = w.leases(d, 100)
        for name, value in kwargs.items():
            setattr(h, name, value)
        with pytest.raises(expected) as err:
            h.acquire("r1", "orch-a", ttl_seconds=60)
        if expected is RunLeaseError:
            assert err.value.reason == "invalid", label
        assert not d.exists(), label
    # the control: the same handle with an honest environment takes the run
    d = tmp_path / "control" / "run-leases"
    assert w.leases(d, 100).acquire("r1", "orch-a", ttl_seconds=60).holder_pid == 100
