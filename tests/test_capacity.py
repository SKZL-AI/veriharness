"""P3-16 slice 1: the capacity registry -- a closed vocabulary, one canonical record, a strict reader.

Every test names what it catches. The limits are asserted with literal numbers (256 entries, 10**15, 64
characters, 262144 bytes), not with the module's constants, so a changed constant fails a test instead of
moving it. Tests write only under `tmp_path`; nothing reads the host's `/tmp`, the home directory or a state
root, starts Herdr or an agent, or touches the network. A FIFO case runs under an alarm so that a reader that
blocks fails the test instead of hanging it.
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import errno
import hashlib
import json
import os
import signal
import stat
from pathlib import Path

import pytest

from hoh import durable_io


def _capacity():
    """`hoh.capacity`, imported inside each test: a tree without the module fails each test with its own
    ModuleNotFoundError (exit 1), not with one collection error (exit 4) that hides the others."""
    import importlib
    return importlib.import_module("hoh.capacity")


GOOD = {"kind": "provider", "name": "anthropic", "limit": 5, "unit": "sessions",
        "source": "declared", "as_of": "2026-10-12T09:00:00Z"}


def E(**over):
    return _capacity().Entry(**{**GOOD, **over})


def reason(call, *args, **kw) -> str:
    """The `reason` of the `CapacityError` the call raises; fails if it raises anything else or nothing."""
    with pytest.raises(_capacity().CapacityError) as caught:
        call(*args, **kw)
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


def three() -> list:
    return [E(kind="provider", name="anthropic", limit=5, unit="sessions"),
            E(kind="machine", name="host.sessions", limit=8, unit="sessions", source="measured"),
            E(kind="dependency", name="ready.frontier", limit=3, unit="items", source="measured")]


# --- R1 the vocabulary -------------------------------------------------------

def test_the_vocabulary_is_closed_and_pinned():
    """Catches a fourth kind, a renamed unit or source, or a loosened match: the scheduler keeps exactly three
    capacities apart and a spelling that nobody listed is refused, not a second unit."""
    capacity = _capacity()
    Entry = capacity.Entry
    assert capacity.SCHEMA == "veriharness.capacity-registry/1"
    assert capacity.KINDS == ("dependency", "machine", "provider")
    assert capacity.SOURCES == ("declared", "measured")
    assert capacity.UNITS == ("bytes", "items", "requests", "sessions", "tokens")
    for kind in ("dependency", "machine", "provider"):
        assert E(kind=kind).kind == kind
    for unit in ("bytes", "items", "requests", "sessions", "tokens"):
        assert E(unit=unit).unit == unit
    for source in ("declared", "measured"):
        assert E(source=source).source == source
    for bad in ("Provider", "provider ", "provider\n", "providers", "", "storage", None, 1, b"provider"):
        assert reason(Entry, **{**GOOD, "kind": bad}) == "invalid", bad
    for bad in ("session", "token", "Sessions", "sessions\n", "", "gib", None, 1):
        assert reason(Entry, **{**GOOD, "unit": bad}) == "invalid", bad
    for bad in ("estimated", "Declared", "measured\n", "", None, True):
        assert reason(Entry, **{**GOOD, "source": bad}) == "invalid", bad

    class Shouting(str):                                     # "a str, nothing else": a subclass is refused
        pass
    for field in ("kind", "name", "unit", "source", "as_of"):
        assert reason(Entry, **{**GOOD, field: Shouting(GOOD[field])}) == "invalid", field

    assert issubclass(capacity.CapacityError, ValueError)    # callers that catch ValueError keep working
    entry = E()
    with pytest.raises(dataclasses.FrozenInstanceError):     # an Entry and a Registry are values
        entry.limit = 6
    registry = capacity.parse(capacity.render([entry]))
    with pytest.raises(dataclasses.FrozenInstanceError):
        registry.digest = "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        registry.entries = ()


# --- R2 names, limits and times; R3 the six keys -----------------------------

def test_a_name_a_limit_and_a_time_are_exactly_what_the_grammar_says():
    """Catches `$` for a full match (a trailing newline), `\\d` for [0-9] (other scripts' digits), a bool or a
    float taken for an integer, an off-by-one at 64 characters, 0 or 10**15, and a time that only looks like
    one (month 13, 30 February, second 60, year 0)."""
    capacity = _capacity()
    Entry = capacity.Entry
    for ok in ("a", "a" * 64, "anthropic", "claude-sonnet-5-5", "host.cpu_slots", "a1-b_c.d"):
        assert E(name=ok).name == ok
    for bad in ("", "A", "1a", "-a", ".a", "a" * 65, "a b", "a\n", "a\x00", "\u00e9", "a/b", "a\uff41",
                "a\u0663", "..", "a\ud800", None, 1, b"a"):
        assert reason(Entry, **{**GOOD, "name": bad}) == "invalid", repr(bad)

    huge = 10 ** 5000                                         # repr() of this raises a plain ValueError
    for field in ("kind", "name", "limit", "unit", "source", "as_of"):
        assert reason(Entry, **{**GOOD, field: huge}) == "invalid", field
    with pytest.raises(_capacity().CapacityError) as long_name:              # a message never carries it whole
        Entry(**{**GOOD, "name": "a" * 100000})
    assert len(str(long_name.value)) < 400

    for ok in (0, 1, 10 ** 15):
        assert E(limit=ok).limit == ok
    for bad in (-1, 10 ** 15 + 1, 2 ** 70, True, False, 1.0, float("nan"), float("inf"), "1", None):
        assert reason(Entry, **{**GOOD, "limit": bad}) == "invalid", repr(bad)

    for ok in ("2026-10-12T09:00:00Z", "2026-02-28T23:59:59Z", "2028-02-29T00:00:00Z", "0001-01-01T00:00:00Z"):
        assert E(as_of=ok).as_of == ok
    for bad in ("2026-10-12T09:00:00Z\n", "2026-10-12T09:00:00", "2026-10-12 09:00:00Z", "2026-10-12t09:00:00z",
                "2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z", "2027-02-29T00:00:00Z", "2026-10-12T24:00:00Z",
                "2026-10-12T09:60:00Z", "2026-10-12T09:00:60Z", "2026-1-2T3:4:5Z", "2026-10-12T09:00:00+00:00",
                "0000-01-01T00:00:00Z", "\u0662026-10-12T09:00:00Z", "20261012T090000Z", "", None, 20261012):
        assert reason(Entry, **{**GOOD, "as_of": bad}) == "invalid", repr(bad)


def test_an_entry_from_a_json_object_has_exactly_its_six_keys():
    """Catches a reader that ignores an extra key or fills a missing one: an object with a seventh or a
    missing key, or not an object at all, is refused."""
    capacity = _capacity()
    Entry = capacity.Entry
    assert Entry.from_dict(dict(GOOD)) == E()
    assert Entry.from_dict(dict(GOOD)).as_dict() == GOOD
    for bad in ({**GOOD, "note": "x"}, {k: v for k, v in GOOD.items() if k != "unit"}, {}, [], None, "x", 1,
                list(GOOD.items())):
        assert reason(Entry.from_dict, bad) == "invalid", repr(bad)


# --- R4 canonical form and digest -------------------------------------------

def test_one_set_of_entries_is_one_file_and_one_digest():
    """Catches an order-dependent record, a non-ASCII or unsorted rendering, and a digest that is not the
    sha256 of the exact bytes or that does not move when any single field does."""
    capacity = _capacity()
    entries = three()
    data = capacity.render(entries)
    assert data == capacity.render(list(reversed(entries))) == capacity.render(iter(entries))
    assert data.isascii() and data.endswith(b"\n") and not data.endswith(b"\n\n")
    document = json.loads(data)
    assert document["schema"] == "veriharness.capacity-registry/1"
    assert [(e["kind"], e["name"]) for e in document["entries"]] == [
        ("dependency", "ready.frontier"), ("machine", "host.sessions"), ("provider", "anthropic")]
    parsed = capacity.parse(data)
    assert parsed.digest == hashlib.sha256(data).hexdigest()
    assert [e.name for e in parsed.entries] == ["ready.frontier", "host.sessions", "anthropic"]
    assert capacity.parse(capacity.render(reversed(entries))).digest == parsed.digest

    seen = {parsed.digest}
    for field, value in (("limit", 6), ("unit", "requests"), ("source", "measured"), ("as_of", "2026-10-12T09:00:01Z"),
                         ("name", "anthropic2"), ("kind", "machine")):
        changed = [E(**{**GOOD, field: value})] + three()[1:]
        digest = capacity.parse(capacity.render(changed)).digest
        assert digest not in seen, field
        seen.add(digest)
    assert capacity.parse(capacity.render([])).entries == ()
    assert capacity.render([]) != capacity.render(three())


# --- R5 write and read; R6 a failed write ------------------------------------

def test_write_then_read_round_trips_and_a_refused_write_leaves_the_old_record(tmp_path):
    """Catches a write that is not mode 0600 whatever the umask, leaves a temp file, writes through a link,
    or lets an invalid, duplicated or oversized set reach the disk over a good record; and the limit of 256."""
    capacity = _capacity()
    path = tmp_path / "capacity.json"
    old_umask = os.umask(0)
    try:
        written = capacity.write(path, three())
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["capacity.json"]
    again = capacity.read(path)
    assert again == written and again.digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert path.read_bytes() == capacity.render(three())

    before = path.read_bytes()
    over = [E(name=f"n{i:03d}") for i in range(257)]
    for bad in ([E(), E()], [E(), "x"], [GOOD], over, [E(kind="machine"), E(name="x"), E(name="x", limit=1)],
                None, 5):
        assert reason(capacity.write, path, bad) == "invalid", repr(bad)[:60]
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["capacity.json"]

    full = tmp_path / "full.json"
    capacity.write(full, over[:256])
    assert len(capacity.read(full).entries) == 256

    with pytest.raises(OSError) as io_error:                                        # I/O errors are not refusals
        capacity.write(tmp_path / "no-dir" / "capacity.json", three())
    assert not isinstance(io_error.value, capacity.CapacityError)
    assert not (tmp_path / "no-dir").exists()

    target = tmp_path / "target.json"
    target.write_bytes(b"precious")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    capacity.write(link, three())
    assert target.read_bytes() == b"precious"
    assert not link.is_symlink() and link.read_bytes() == capacity.render(three())


class _FaultyOS:
    """Stands in for `os` inside `durable_io` only (the seam of `tests/test_durable_write.py`); overrides win."""

    def __init__(self, **overrides):
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(os, name)


def test_a_write_that_fails_before_the_replace_leaves_the_old_record_and_one_that_fails_after_it_holds_the_new(
        tmp_path, monkeypatch):
    """Catches a writer that does not go through `durable_io.durable_write` (a raw write is not atomic, so the old
    record is damaged or the error is not raised), and a contract that is stated wrongly: a disk-full failure
    leaves the OLD record and no temp file, while `DurableWriteNotSynced` is raised AFTER the replace -- the NEW
    record is in place and `read` returns it. Faults are injected under `durable_io` at the descriptor level, so
    any module that writes through the primitive is judged on its behaviour."""
    capacity = _capacity()
    path = tmp_path / "capacity.json"
    capacity.write(path, three())
    before = path.read_bytes()
    new_entries = [E(limit=1)]
    new_bytes = capacity.render(new_entries)
    assert new_bytes != before

    real_write, real_fsync = os.write, os.fsync

    def enospc_on_temp(fd, data):
        try:
            name = os.path.basename(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            name = ""
        if name.startswith(".capacity.json.tmp"):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return real_write(fd, data)
    monkeypatch.setattr(durable_io, "os", _FaultyOS(write=enospc_on_temp))
    with pytest.raises(durable_io.DiskFullError):
        capacity.write(path, new_entries)
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["capacity.json"]
    monkeypatch.undo()

    def dir_fsync_fails(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_fsync(fd)
    monkeypatch.setattr(durable_io, "os", _FaultyOS(fsync=dir_fsync_fails))
    with pytest.raises(durable_io.DurableWriteNotSynced):
        capacity.write(path, new_entries)
    monkeypatch.undo()
    assert path.read_bytes() == new_bytes                                          # the new record IS in place
    assert capacity.read(path).digest == hashlib.sha256(new_bytes).hexdigest()
    assert [p.name for p in tmp_path.iterdir()] == ["capacity.json"]

    capacity.write(path, three())                                                 # the control: no fault, no error
    assert path.read_bytes() == before


# --- R7 the reader refuses; R8 odd bytes; R9 limits; R10 canonical ----------

def test_an_absent_or_odd_file_is_refused_never_read_as_empty(tmp_path):
    """Catches a reader that answers an empty registry (or zero, or unlimited) for a file it cannot trust:
    missing, a directory, a dangling and a good link, a FIFO, a second name, mode 000, an empty file, a file
    one byte over the size limit; and the control, a good record, reads."""
    capacity = _capacity()
    good = tmp_path / "good.json"
    capacity.write(good, three())
    assert len(capacity.read(good).entries) == 3                                   # the control

    assert reason(capacity.read, tmp_path / "absent.json") == "missing"
    assert reason(capacity.read, tmp_path / "no-dir" / "absent.json") == "missing"
    assert reason(capacity.read, tmp_path) == "unreadable"                          # a directory

    dangling = tmp_path / "dangling.json"
    dangling.symlink_to(tmp_path / "nowhere.json")
    assert reason(capacity.read, dangling) == "unreadable"                          # not "missing"
    link = tmp_path / "link.json"
    link.symlink_to(good)
    assert reason(capacity.read, link) == "unreadable"                              # a good file behind a link

    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    with alarm():
        assert reason(capacity.read, fifo) == "unreadable"

    other = tmp_path / "other-name.json"
    os.link(good, other)
    assert reason(capacity.read, good) == "unreadable" and reason(capacity.read, other) == "unreadable"
    huge_a, huge_b = tmp_path / "huge-a.json", tmp_path / "huge-b.json"
    huge_a.write_bytes(b" " * 262145)
    os.link(huge_a, huge_b)                                  # the type and the names are judged before the size
    assert reason(capacity.read, huge_a) == "unreadable" and reason(capacity.read, huge_b) == "unreadable"

    secret = tmp_path / "secret.json"
    capacity.write(secret, three())
    secret.chmod(0)
    try:
        if not os.access(secret, os.R_OK):                                         # root reads it: nothing to refuse
            assert reason(capacity.read, secret) == "unreadable"
    finally:
        secret.chmod(0o600)

    for odd in ("a\x00b.json", None, 5, b"capacity.json", ""):                      # not a usable path at all
        assert reason(capacity.read, odd) == "unreadable", repr(odd)
    with pytest.raises(capacity.CapacityError) as long_path:                         # a message stays short
        capacity.read(tmp_path / ("p" * 5000))
    assert long_path.value.reason == "unreadable" and len(str(long_path.value)) < 400

    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    assert reason(capacity.read, empty) == "malformed"
    exact = tmp_path / "exact.json"
    exact.write_bytes(b" " * 262144)
    assert reason(capacity.read, exact) == "malformed"                              # at the limit: looked at
    big = tmp_path / "big.json"
    big.write_bytes(b" " * 262145)
    assert reason(capacity.read, big) == "oversize"                                 # one over: not read


def test_odd_bytes_and_odd_json_are_refused_with_their_reason():
    """Catches a parser that is lenient: bytes that are not ASCII (Latin-1, UTF-16, a BOM), a NUL, a lone
    surrogate escape, a repeated key, NaN, truncated or deeply nested JSON, JSON that is not an object, a wrong
    or missing schema or key, an entry list that is not a list, and a size boundary tested by behaviour."""
    capacity = _capacity()
    ok = capacity.render(three())
    assert capacity.parse(ok).digest
    text = ok.decode("ascii")
    cases = {
        "latin1": ("malformed", text.replace('"anthropic"', '"anthr\u00f6pic"').encode("latin-1")),
        "utf16": ("malformed", text.encode("utf-16")),
        "bom": ("malformed", b"\xef\xbb\xbf" + ok),
        "nul": ("malformed", ok + b"\x00"),
        "surrogate": ("invalid", text.replace('"anthropic"', '"a\\ud800"').encode("ascii")),
        "dup-key": ("malformed", b'{"schema": "veriharness.capacity-registry/1", "entries": [], "entries": []}'),
        "dup-key-entry": ("malformed", text.replace('"limit": 5,', '"limit": 5, "limit": 6,').encode("ascii")),
        "nan": ("malformed", text.replace('"limit": 5', '"limit": NaN').encode("ascii")),
        "infinity": ("malformed", text.replace('"limit": 5', '"limit": Infinity').encode("ascii")),
        "truncated": ("malformed", ok[: len(ok) // 2]),
        "deep": ("malformed", b"[" * 100000),
        "not-object-list": ("malformed", b"[]"),
        "not-object-null": ("malformed", b"null"),
        "not-object-number": ("malformed", b"7"),
        "not-object-string": ("malformed", b'"x"'),
        "blank": ("malformed", b"   \n"),
        "no-schema": ("schema", b'{"entries": []}'),
        "no-entries": ("schema", b'{"schema": "veriharness.capacity-registry/1"}'),
        "other-schema": ("schema", b'{"schema": "veriharness.capacity-registry/2", "entries": []}'),
        "extra-key": ("schema", b'{"schema": "veriharness.capacity-registry/1", "entries": [], "digest": "x"}'),
        "entries-object": ("invalid", b'{"schema": "veriharness.capacity-registry/1", "entries": {}}'),
        "entries-null": ("invalid", b'{"schema": "veriharness.capacity-registry/1", "entries": null}'),
        "entry-float": ("invalid", text.replace('"limit": 5', '"limit": 5.0').encode("ascii")),
        "entry-bool": ("invalid", text.replace('"limit": 5', '"limit": true').encode("ascii")),
        "entry-negative": ("invalid", text.replace('"limit": 5', '"limit": -5').encode("ascii")),
        "entry-huge-int": ("invalid", text.replace('"limit": 5', '"limit": 1' + "0" * 30).encode("ascii")),
    }
    for label, (want, data) in cases.items():
        assert reason(capacity.parse, data) == want, label
    long_int = text.replace('"limit": 5', '"limit": 1' + "0" * 5000).encode("ascii")
    assert reason(capacity.parse, long_int) in ("malformed", "invalid")          # the int-digit limit decides which
    assert reason(capacity.parse, "x") == "malformed"
    assert reason(capacity.parse, None) == "malformed"
    assert reason(capacity.parse, bytearray(ok)) == "malformed"


def test_the_size_and_entry_limits_hold_by_behaviour_not_by_constant():
    """Catches a moved ceiling: 262144 bytes is looked at and 262145 is `oversize`, before any parse; 256
    entries in a hand-made record are read and 257 are `invalid`; 10**15 is read and 10**15+1 is `invalid`."""
    capacity = _capacity()
    assert reason(capacity.parse, b" " * 262144) == "malformed"
    assert reason(capacity.parse, b" " * 262145) == "oversize"
    assert reason(capacity.parse, b"x" * 1000000) == "oversize"
    assert reason(capacity.parse, "\u00e9".encode("utf-8") * 150000) == "oversize"     # size first, then ASCII
    assert reason(capacity.parse, "\u00e9".encode("utf-8") * 100) == "malformed"

    def record(n: int, limit: int = 1) -> bytes:
        entries = [{**GOOD, "name": f"n{i:03d}", "limit": limit} for i in range(n)]
        return (json.dumps({"schema": capacity.SCHEMA, "entries": entries}, sort_keys=True, indent=1) + "\n").encode()
    assert len(capacity.parse(record(256)).entries) == 256
    assert reason(capacity.parse, record(257)) == "invalid"
    assert capacity.parse(record(1, 10 ** 15)).entries[0].limit == 10 ** 15
    assert reason(capacity.parse, record(1, 10 ** 15 + 1)) == "invalid"


def test_a_record_that_is_not_exactly_what_render_writes_is_refused():
    """Catches a reader that accepts a second spelling of one set of entries (so one set would have two
    digests): other indentation, other key order, unsorted entries, escapes, CRLF, a missing or doubled final
    newline, a repeated entry; and a record the writer produced is accepted."""
    capacity = _capacity()
    entries = three()
    ok = capacity.render(entries)
    assert capacity.parse(ok).entries == tuple(sorted(entries, key=lambda e: (e.kind, e.name)))
    document = json.loads(ok)
    spelled = {
        "compact": json.dumps(document, sort_keys=True).encode(),
        "indent-4": (json.dumps(document, sort_keys=True, indent=4) + "\n").encode(),
        "key-order": (json.dumps({"schema": document["schema"], "entries": document["entries"]}, indent=1)
                      + "\n").encode(),
        "no-newline": ok[:-1],
        "two-newlines": ok + b"\n",
        "crlf": ok.replace(b"\n", b"\r\n"),
        "leading-space": b" " + ok,
        "escape": ok.replace(b'"anthropic"', b'"anthrop\\u0069c"'),
        "unsorted": (json.dumps({**document, "entries": list(reversed(document["entries"]))},
                                sort_keys=True, indent=1) + "\n").encode(),
    }
    for label, data in spelled.items():
        assert reason(capacity.parse, data) == "noncanonical", label
    repeated = {**document, "entries": document["entries"] + document["entries"][:1]}
    assert reason(capacity.parse, (json.dumps(repeated, sort_keys=True, indent=1) + "\n").encode()) == "invalid"


# --- R11 lookup ----------------------------------------------------------------

def test_an_unrecorded_capacity_is_an_error_never_zero_or_unlimited():
    """Catches a lookup that defaults: an absent capacity raises `unknown` (also for a wrong kind, a name of
    another kind, a near spelling and a non-string); an empty registry is a valid, different thing from a
    missing one; `of_kind` is in name order and empty for a kind with nothing."""
    capacity = _capacity()
    registry = capacity.parse(capacity.render(three() + [E(name="anthropic-b", limit=2)]))
    assert registry.get("provider", "anthropic").limit == 5
    assert registry.limit("machine", "host.sessions") == 8
    for kind, name in (("provider", "openai"), ("machine", "anthropic"), ("provider", "Anthropic"),
                       ("provider", "anthropic "), ("storage", "anthropic"), (None, None), ("provider", 1),
                       (10 ** 5000, "anthropic"), ("provider", 10 ** 5000), ("provider", "a" * 100000)):
        assert reason(registry.get, kind, name) == "unknown", (kind, name)
        assert reason(registry.limit, kind, name) == "unknown", (kind, name)
    assert [e.name for e in registry.of_kind("provider")] == ["anthropic", "anthropic-b"]
    assert registry.of_kind("storage") == () and registry.of_kind(None) == ()

    empty = capacity.parse(capacity.render([]))
    assert isinstance(empty, capacity.Registry) and empty.entries == ()
    assert reason(empty.get, "provider", "anthropic") == "unknown"


# --- R12 what the module may do ------------------------------------------------

def test_the_module_reads_no_clock_runs_nothing_and_writes_only_through_durable_write():
    """Catches a later edit that makes the registry impure: `datetime.now()`, a raw `open`, a process, the
    network, the environment, a second kind of write. The module is one pure schema, one reader and one durable
    write: the modules it may import and the members of `os` it may touch are listed, and the write-capable
    attribute names are forbidden anywhere. How `durable_write` is bound (`from .durable_io import ...` or
    `durable_io.durable_write`) and how `datetime` is imported are free."""
    tree = ast.parse(Path(_capacity().__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
            if node.level:                                   # a relative import names durable_io, nothing else
                assert (node.module or "") in ("durable_io", "") and all(
                    a.name in ("durable_io", "durable_write", "read_single_link") for a in node.names)
    assert imported <= {"__future__", "hashlib", "json", "os", "re", "stat", "collections.abc", "dataclasses",
                        "datetime", "pathlib", "typing", ".durable_io", "."}, imported
    forbidden = {"write_bytes", "write_text", "open", "unlink", "rmdir", "remove", "rename", "touch", "mkdir",
                 "chmod", "symlink_to", "hardlink_to", "system", "popen", "Popen", "now", "utcnow", "today",
                 "monotonic", "environ", "getenv", "urlopen", "connect", "fdopen", "makedirs", "truncate"}
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attributes & forbidden, attributes & forbidden
    os_members = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                  and isinstance(n.value, ast.Name) and n.value.id == "os"}
    assert os_members <= {"lstat", "PathLike", "fspath"}, os_members
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "exec", "eval", "compile", "__import__", "input", "print"}
    assert ".durable_io" in imported or "." in imported                     # and the write goes through it
