"""P3-16 slice 2: `hoh capacity set` / `hoh capacity show` -- the capacity registry's location and the operator's command.

Every test drives the command through `hoh.cli.main` (the entry the product executes), against a state root that
`StorageRegistry.init()` made under `tmp_path`, and names what it catches. The slice-1 module (`tests/test_capacity.py`)
pins the record itself; this file pins what the command adds: where the record lives, which roots it will touch,
what it refuses before it writes, the lock, the clock and the printed view. The refusal cases always assert the
`capacity <verb> refused:` text, so a tree without the command (argparse's own exit 2) cannot pass them.

Hermetic: `HOH_STATE_ROOT` is removed and `HOME` / `XDG_STATE_HOME` point below `tmp_path` for every test, so the
default root is never the host's; nothing reads `~/.agents`, starts Herdr or an agent, fills a disk or opens the
network. A FIFO and a held lock run under an alarm, so a command that blocks fails the test instead of hanging it.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import signal
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hoh import cli


def _capacity():
    """`hoh.capacity`, imported inside each test (see tests/test_capacity.py): a tree without a name this file
    needs fails that test with its own AttributeError, not the whole file with one collection error."""
    import importlib
    return importlib.import_module("hoh.capacity")


FILE = "capacity-registry.json"


@pytest.fixture(autouse=True)
def hermetic_environment(tmp_path, monkeypatch):
    """No test may reach the host's state root: the environment resolution is pointed below `tmp_path`."""
    monkeypatch.delenv("HOH_STATE_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))


def initialised(where: Path) -> Path:
    """A real, initialised state root: what `hoh storage init --root` makes."""
    from hoh.storage_registry import StorageRegistry
    StorageRegistry(where).init()
    return where.resolve()


@pytest.fixture
def root(tmp_path) -> Path:
    return initialised(tmp_path / "state")


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


def capacity_cli(capsys, *argv: str) -> tuple[int, str, str]:
    """`hoh capacity <argv>` through `cli.main`; argparse's own exit is returned as its code."""
    try:
        code = cli.main(["capacity", *argv])
    except SystemExit as stop:
        code = stop.code
    seen = capsys.readouterr()
    return code, seen.out, seen.err


def set_(capsys, root, kind, name, limit, unit, *extra):
    return capacity_cli(capsys, "set", kind, name, limit, unit, "--storage-root", str(root), *extra)


def show(capsys, root, *extra):
    return capacity_cli(capsys, "show", "--storage-root", str(root), *extra)


def good(capsys, root, kind="provider", name="anthropic", limit="5", unit="sessions") -> dict:
    code, out, err = set_(capsys, root, kind, name, limit, unit)
    assert (code, err) == (0, ""), (code, err)
    return json.loads(out)


def refused(result, verb: str) -> str:
    code, out, err = result
    assert code == 2 and out == "", (code, out, err)
    assert err.startswith(f"capacity {verb} refused: ") and err.endswith("\n") and err.count("\n") == 1, err
    return err


def fingerprint(path: Path):
    """What a refusal must leave as it was: type, size, mtime and (for a regular file) its bytes -- never opened
    when it is a FIFO or a link."""
    info = os.lstat(path)
    body = None
    if stat.S_ISREG(info.st_mode):
        try:
            body = path.read_bytes()
        except OSError:                                     # a mode 000 file: its bytes are not readable either
            body = b"<unreadable>"
    return info.st_mode, info.st_size, info.st_mtime_ns, info.st_nlink, body


def listing(directory: Path) -> dict:
    return {entry.name: fingerprint(entry) for entry in sorted(directory.iterdir())}


# --- R1 the round trip, through the command ----------------------------------------------------------------------

def test_set_then_show_round_trips_through_the_real_command(root, capsys):
    """Catches a command that writes something other than the canonical slice-1 record, at another place or mode,
    stamps a constant or measured entry, prints a digest that is not the file's, or shows what it did not write:
    the record is `<state root>/capacity-registry.json`, exactly the bytes `capacity.render` makes, mode 0600,
    one `declared` entry whose `as_of` lies between the clock before and after the command, and `show` prints the
    same entry and the same digest."""
    capacity = _capacity()
    before = datetime.now(timezone.utc).replace(microsecond=0)
    code, out, err = set_(capsys, root, "provider", "anthropic", "5", "sessions")
    after = datetime.now(timezone.utc)
    assert (code, err) == (0, "")
    printed = json.loads(out)
    assert set(printed) == {"storage_root", "path", "durable", "entry", "replaced", "entries", "digest"}
    assert printed["storage_root"] == str(root) and printed["path"] == str(root / FILE)
    assert printed["durable"] is True and printed["replaced"] is None and printed["entries"] == 1
    entry = printed["entry"]
    stamped = datetime.strptime(entry["as_of"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert before <= stamped <= after
    assert entry == {"kind": "provider", "name": "anthropic", "limit": 5, "unit": "sessions",
                     "source": "declared", "as_of": entry["as_of"]}
    record = root / FILE
    data = record.read_bytes()
    assert data == capacity.render([capacity.Entry(**entry)])
    assert hashlib.sha256(data).hexdigest() == printed["digest"]
    assert stat.S_IMODE(os.lstat(record).st_mode) == 0o600
    assert capacity.read(record).entries == (capacity.Entry(**entry),)
    code, out, err = show(capsys, root)
    assert (code, err) == (0, "")
    shown = json.loads(out)
    assert set(shown) == {"storage_root", "path", "digest", "entries"}
    assert shown["storage_root"] == str(root) and shown["path"] == str(record)
    assert shown["digest"] == printed["digest"] and shown["entries"] == [entry]
    assert out.isascii()


def test_a_second_set_replaces_by_kind_and_name_and_the_record_stays_sorted(root, capsys):
    """Catches a command that appends a duplicate, loses the entries it did not touch, replaces by name alone,
    forgets to say what it replaced or leaves the entries unsorted: three kinds are kept apart, the same
    kind/name is replaced (limit and unit), the other entries survive, and `replaced` is the previous entry."""
    good(capsys, root, "provider", "zeta", "1", "requests")
    good(capsys, root, "machine", "zeta", "8", "sessions")
    good(capsys, root, "dependency", "frontier", "4", "items")
    third = good(capsys, root, "provider", "zeta", "9", "tokens")
    assert third["replaced"]["limit"] == 1 and third["replaced"]["unit"] == "requests"
    assert third["replaced"]["kind"] == "provider" and third["entries"] == 3
    shown = json.loads(show(capsys, root)[1])
    keys = [(e["kind"], e["name"], e["limit"], e["unit"]) for e in shown["entries"]]
    assert keys == [("dependency", "frontier", 4, "items"), ("machine", "zeta", 8, "sessions"),
                    ("provider", "zeta", 9, "tokens")]
    assert shown["digest"] == third["digest"]


def test_the_limit_boundaries_are_zero_and_ten_to_the_fifteenth(root, capsys):
    """Catches a bound moved by one in the command's own parsing: 0 is a limit (declared, not absent),
    1000000000000000 is the largest, 1000000000000001 is refused; the literals are not the module's constant."""
    assert good(capsys, root, "machine", "host.bytes", "1000000000000000", "bytes")["entry"]["limit"] == 10 ** 15
    assert good(capsys, root, "machine", "host.zero", "0", "sessions")["entry"]["limit"] == 0
    kept = fingerprint(root / FILE)
    refused(set_(capsys, root, "machine", "host.over", "1000000000000001", "bytes"), "set")
    assert fingerprint(root / FILE) == kept


# --- R2 where the record lives, and which roots the command touches ------------------------------------------------

def test_the_record_is_the_only_file_set_adds_and_show_writes_nothing(root, capsys):
    """Catches a command that leaves a lock file, a temp file or a second record beside the registry, or a `show`
    that creates, touches or rewrites anything: the state root gains exactly `capacity-registry.json`, and a
    later `show` (and a refused `set`) leave every name, byte and mtime of the root as they were."""
    names = set(os.listdir(root))
    good(capsys, root)
    assert set(os.listdir(root)) - names == {FILE}
    assert set(os.listdir(root)) == names | {FILE}
    before = listing(root)
    directory_mtime = os.stat(root).st_mtime_ns
    assert show(capsys, root)[0] == 0
    refused(set_(capsys, root, "provider", "anthropic", "x", "sessions"), "set")
    assert listing(root) == before and os.stat(root).st_mtime_ns == directory_mtime


def _foreign_roots(tmp_path: Path) -> dict[str, Path]:
    """Directories the command must not treat as a registered state root (each a fresh path)."""
    cases: dict[str, Path] = {}
    cases["absent"] = tmp_path / "nowhere"
    cases["empty"] = tmp_path / "empty"
    cases["empty"].mkdir()
    cases["a file only"] = tmp_path / "plain"
    cases["a file only"].mkdir()
    (cases["a file only"] / "notes.txt").write_text("hello", encoding="utf-8")
    cases["foreign owner"] = tmp_path / "foreign"
    cases["foreign owner"].mkdir()
    (cases["foreign owner"] / "OWNER.json").write_text(
        json.dumps({"schema": "veriharness.storage-owner/1", "owner": "someone-else"}), encoding="utf-8")
    cases["legacy owner record"] = tmp_path / "legacy"
    cases["legacy owner record"].mkdir()
    (cases["legacy owner record"] / "OWNER.json").write_text(
        json.dumps({"schema": "veriharness.storage-owner/v0-bootstrap", "owner": "veriharness"}), encoding="utf-8")
    real = initialised(tmp_path / "real-elsewhere")
    cases["owner record is a link"] = tmp_path / "linked-owner"
    cases["owner record is a link"].mkdir()
    (cases["owner record is a link"] / "OWNER.json").symlink_to(real / "OWNER.json")
    for name in ("projects", "ledger"):
        (cases["owner record is a link"] / name).mkdir()
    cases["ledger is a link"] = initialised(tmp_path / "bad-ledger")
    (cases["ledger is a link"] / "ledger").rename(cases["ledger is a link"] / "ledger.moved")
    (cases["ledger is a link"] / "ledger").symlink_to(cases["ledger is a link"] / "ledger.moved")
    return cases


def test_a_root_that_is_not_an_initialised_state_root_is_refused_and_nothing_is_created(tmp_path, capsys):
    """Catches a command that writes into any directory it is pointed at: an absent path, an empty directory, a
    directory of other files, a foreign or legacy `OWNER.json`, an owner record that is a link and a `ledger/`
    that is a link are each refused by `set` and `show` with the storage-root text, and the tree is byte-for-byte
    what it was (an absent path is still absent)."""
    for label, where in _foreign_roots(tmp_path).items():
        before = listing(where) if where.exists() else None
        for verb, result in (("set", set_(capsys, where, "provider", "anthropic", "5", "sessions")),
                             ("show", show(capsys, where))):
            text = refused(result, verb)
            assert "storage state root is not initialised" in text, (label, text)
        assert (listing(where) if where.exists() else None) == before, label
        assert not (where / FILE).exists() and not (where / FILE).is_symlink(), label


def test_the_root_is_the_option_then_the_environment_then_the_xdg_default_and_never_an_empty_option(
        tmp_path, monkeypatch, capsys):
    """Catches a command that resolves the root its own way: `--storage-root` beats `$HOH_STATE_ROOT`, the
    environment beats `$XDG_STATE_HOME/veriharness`, nothing is created at an unresolved default, and an EMPTY
    `--storage-root` is refused rather than silently falling back to another root."""
    env_root = initialised(tmp_path / "from-env")
    option_root = initialised(tmp_path / "from-option")
    monkeypatch.setenv("HOH_STATE_ROOT", str(env_root))
    code, out, err = capacity_cli(capsys, "set", "provider", "a", "1", "sessions")
    assert (code, err) == (0, "") and json.loads(out)["path"] == str(env_root / FILE)
    code, out, err = capacity_cli(capsys, "set", "provider", "a", "2", "sessions", "--storage-root", str(option_root))
    assert (code, err) == (0, "") and json.loads(out)["path"] == str(option_root / FILE)
    assert json.loads((env_root / FILE).read_text(encoding="ascii"))["entries"][0]["limit"] == 1
    kept = fingerprint(env_root / FILE)
    text = refused(capacity_cli(capsys, "set", "provider", "a", "3", "sessions", "--storage-root", ""), "set")
    assert "--storage-root is empty" in text and fingerprint(env_root / FILE) == kept
    refused(capacity_cli(capsys, "show", "--storage-root", ""), "show")
    monkeypatch.delenv("HOH_STATE_ROOT")
    refused(capacity_cli(capsys, "set", "provider", "a", "1", "sessions"), "set")
    assert not (tmp_path / "xdg").exists() and not (tmp_path / "home").exists()
    (tmp_path / "xdg").mkdir()
    default = initialised(tmp_path / "xdg" / "veriharness")
    assert json.loads(capacity_cli(capsys, "set", "provider", "a", "1", "sessions")[1])["path"] == str(default / FILE)


def test_a_root_spelled_as_a_link_a_relative_path_or_with_an_equals_sign_is_the_same_root(
        tmp_path, monkeypatch, capsys):
    """Catches a command that writes through a spelling it did not resolve or reads a spelling differently: a
    symbolic link to the root, a path with `..`, a relative path and `--storage-root=PATH` all name the one
    registered root (the registry resolves them as every storage command does), the record is made inside the
    resolved root and never at a link, and a path with a NUL is a refusal, not a traceback."""
    real = initialised(tmp_path / "real")
    (tmp_path / "link").symlink_to(real)
    spelled = [str(tmp_path / "link"), str(tmp_path / "real" / ".." / "real"), str(tmp_path / "real") + "/"]
    for n, where in enumerate(spelled, start=1):
        code, out, err = set_(capsys, where, "machine", "host.sessions", str(n), "sessions")
        assert (code, err) == (0, "") and json.loads(out)["path"] == str(real / FILE)
    assert not (tmp_path / "link" / FILE).is_symlink() and os.listdir(real).count(FILE) == 1
    monkeypatch.chdir(tmp_path)
    code, out, err = capacity_cli(capsys, "set", "machine", "host.sessions", "9", "sessions", "--storage-root", "real")
    assert (code, err) == (0, "") and json.loads(out)["path"] == str(real / FILE)
    code, out, err = capacity_cli(capsys, "show", f"--storage-root={real}")
    assert code == 0 and json.loads(out)["entries"][0]["limit"] == 9
    refused(capacity_cli(capsys, "show", "--storage-root="), "show")
    refused(capacity_cli(capsys, "show", "--storage-root", "a\x00b"), "show")
    refused(capacity_cli(capsys, "set", "machine", "x", "1", "sessions", "--storage-root", str(real) + "\x00"), "set")


# --- R3 what is refused before anything is written ---------------------------------------------------------------

def test_every_malformed_argument_is_refused_with_the_old_record_in_place(root, capsys):
    """Catches a command that coerces or truncates what it is given (`int()` accepts a sign, spaces, `1_0`, an
    exponent-free float spelling and the digits of other scripts), accepts a word outside the closed vocabulary or
    a name outside the grammar, or writes before it has validated: every case below is exit 2 with the refusal
    text on stderr, nothing on stdout, and the record's type, bytes and mtime unchanged."""
    good(capsys, root)
    kept = fingerprint(root / FILE)
    cases = [
        ("kind", ("disk", "x", "1", "sessions")), ("kind", ("Provider", "x", "1", "sessions")),
        ("kind", ("provider\n", "x", "1", "sessions")), ("kind", ("", "x", "1", "sessions")),
        ("name", ("provider", "", "1", "sessions")), ("name", ("provider", "Upper", "1", "sessions")),
        ("name", ("provider", "9a", "1", "sessions")), ("name", ("provider", "a" * 65, "1", "sessions")),
        ("name", ("provider", "a b", "1", "sessions")), ("name", ("provider", "a\n", "1", "sessions")),
        ("name", ("provider", "ä", "1", "sessions")), ("name", ("provider", "a\udcff", "1", "sessions")),
        ("limit", ("provider", "x", "", "sessions")), ("limit", ("provider", "x", "-1", "sessions")),
        ("limit", ("provider", "x", "+1", "sessions")), ("limit", ("provider", "x", " 1", "sessions")),
        ("limit", ("provider", "x", "1 ", "sessions")), ("limit", ("provider", "x", "1\n", "sessions")),
        ("limit", ("provider", "x", "01", "sessions")), ("limit", ("provider", "x", "1_0", "sessions")),
        ("limit", ("provider", "x", "1.0", "sessions")), ("limit", ("provider", "x", "1e3", "sessions")),
        ("limit", ("provider", "x", "0x10", "sessions")), ("limit", ("provider", "x", "٥", "sessions")),
        ("limit", ("provider", "x", "１", "sessions")), ("limit", ("provider", "x", "9" * 17, "sessions")),
        ("limit", ("provider", "x", "9" * 5000, "sessions")),
        ("limit", ("provider", "x", "1000000000000001", "sessions")),
        ("unit", ("provider", "x", "1", "session")), ("unit", ("provider", "x", "1", "Sessions")),
        ("unit", ("provider", "x", "1", "bytes\n")), ("unit", ("provider", "x", "1", "")),
    ]
    for which, (kind, name, limit, unit) in cases:
        text = refused(set_(capsys, root, kind, name, limit, unit), "set")
        assert text.isascii() and len(text) < 700, (which, limit[:20], text[:200])
        assert fingerprint(root / FILE) == kept, (which, kind, name, limit[:20], unit)
    for arguments in (("set", "provider", "x", "1"), ("set", "provider", "x", "1", "sessions", "extra"),
                      ("set",), ("show", "extra"), ("frobnicate",), ()):
        code, out, err = capacity_cli(capsys, *arguments)
        assert code == 2 and out == "" and "capacity set refused" not in err, arguments
    assert fingerprint(root / FILE) == kept


def _write_spoiled(root: Path, label: str) -> Path:
    capacity = _capacity()
    record = root / FILE
    entry = capacity.Entry(kind="provider", name="anthropic", limit=5, unit="sessions", source="declared",
                           as_of="2026-10-12T09:00:00Z")
    valid = capacity.render([entry])
    if label == "not JSON":
        record.write_bytes(b"{not json")
    elif label == "empty":
        record.write_bytes(b"")
    elif label == "valid but not canonical":
        record.write_bytes(json.dumps(json.loads(valid), indent=4).encode("ascii") + b"\n")
    elif label == "wrong schema":
        record.write_bytes(valid.replace(b"capacity-registry/1", b"capacity-registry/2"))
    elif label == "not ASCII":
        record.write_bytes(valid.replace(b"anthropic", "anthröpic".encode("utf-8")))
    elif label == "oversize":
        record.write_bytes(b" " * (262144 + 1))
    elif label == "a directory":
        record.mkdir()
    elif label == "a dangling link":
        record.symlink_to(root / "no-such-target")
    elif label == "a link to a valid record":
        target = root.parent / "valid-elsewhere.json"
        target.write_bytes(valid)
        record.symlink_to(target)
    elif label == "a second name":
        record.write_bytes(valid)
        os.link(record, root.parent / f"second-name-of-{root.name}")
    elif label == "a FIFO":
        os.mkfifo(record)
    elif label == "not readable":
        record.write_bytes(valid)
        os.chmod(record, 0)
    else:
        raise AssertionError(label)
    return record


SPOILED = ["not JSON", "empty", "valid but not canonical", "wrong schema", "not ASCII", "oversize", "a directory",
           "a dangling link", "a link to a valid record", "a second name", "a FIFO", "not readable"]


@pytest.mark.parametrize("label", SPOILED)
def test_a_record_the_reader_refuses_is_never_replaced_by_set_and_is_refused_by_show(tmp_path, capsys, label):
    """Catches a command that treats an unreadable record as an empty registry (and so writes over it, forgetting
    every limit in it), follows a link, opens a FIFO, or shows a record the reader refuses: every spoiled
    spelling is refused by `set` and by `show` under an alarm, with the file exactly as it was; the control (a
    good record) is accepted by both."""
    if label == "not readable" and os.geteuid() == 0:
        pytest.skip("root reads a mode 000 file")
    state = initialised(tmp_path / "state")
    record = _write_spoiled(state, label)
    before = fingerprint(record)
    target_before = os.readlink(record) if record.is_symlink() else None
    with alarm():
        set_text = refused(set_(capsys, state, "provider", "other", "1", "sessions"), "set")
        show_text = refused(show(capsys, state), "show")
    assert "capacity registry: " in set_text and "capacity registry: " in show_text
    assert fingerprint(record) == before
    assert (os.readlink(record) if record.is_symlink() else None) == target_before
    if label == "a link to a valid record":
        assert (tmp_path / "valid-elsewhere.json").read_bytes() == _capacity().render(
            [_capacity().Entry(kind="provider", name="anthropic", limit=5, unit="sessions", source="declared",
                               as_of="2026-10-12T09:00:00Z")])
    leftovers = [name for name in os.listdir(state) if name.startswith(".")]
    assert leftovers == [], leftovers


def test_show_on_an_initialised_root_without_a_record_is_a_refusal_not_an_empty_registry(root, capsys):
    """Catches a `show` that reads an absent record as an empty registry (`entries: []`, exit 0): a root that
    `hoh storage init` made and no `set` has touched is refused with the reader's `missing` text, stdout empty,
    nothing created; the control, after one `set`, shows the entry. `set` on the same root starts the record."""
    names = set(os.listdir(root))
    text = refused(show(capsys, root), "show")
    assert "capacity registry: " in text and "does not exist" in text
    assert set(os.listdir(root)) == names and not (root / FILE).exists()
    good(capsys, root)
    assert len(json.loads(show(capsys, root)[1])["entries"]) == 1


def test_the_registry_holds_at_most_256_entries_through_the_command(root, capsys):
    """Catches a command that grows a registry past the reader's own limit (a record `show` would then refuse):
    with 256 entries one more is refused and the record unchanged, while replacing one of the 256 is accepted."""
    capacity = _capacity()
    entries = [capacity.Entry(kind="provider", name=f"p{n:03d}", limit=n, unit="requests", source="declared",
                              as_of="2026-10-12T09:00:00Z") for n in range(256)]
    capacity.write(root / FILE, entries)
    kept = fingerprint(root / FILE)
    text = refused(set_(capsys, root, "provider", "p256", "1", "requests"), "set")
    assert "257 entries; at most 256" in text and fingerprint(root / FILE) == kept
    done = good(capsys, root, "provider", "p000", "7", "requests")
    assert done["entries"] == 256 and done["replaced"]["limit"] == 0
    assert len(json.loads(show(capsys, root)[1])["entries"]) == 256


# --- R4 the lock -------------------------------------------------------------------------------------------------------

def test_a_held_lock_is_refused_not_waited_for_and_is_released_on_every_exit(root, capsys):
    """Catches a command that waits on a held lock (a stuck holder would hang every operator), takes no lock (two
    writers lose an update), leaves it held after a refusal, or makes `show` take it: while another descriptor
    holds the state root's lock `set` is refused at once and writes nothing, `show` still reads, and after a
    successful `set` and after a refused one the lock is free again."""
    import fcntl
    good(capsys, root)
    kept = fingerprint(root / FILE)
    holder = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with alarm():
            text = refused(set_(capsys, root, "provider", "other", "1", "sessions"), "set")
            assert "the state root is locked by another process" in text
            assert show(capsys, root)[0] == 0
        assert fingerprint(root / FILE) == kept
    finally:
        os.close(holder)
    good(capsys, root, "provider", "other", "1", "sessions")
    refused(set_(capsys, root, "provider", "other", "nope", "sessions"), "set")
    (root / FILE).write_bytes(b"{not json")
    refused(set_(capsys, root, "provider", "other", "1", "sessions"), "set")
    probe = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(probe)


def test_a_write_that_may_not_be_durable_is_reported_and_one_that_failed_leaves_the_old_record(
        root, capsys, monkeypatch):
    """Catches a command that hides `DurableWriteNotSynced` (the record IS in place) or prints a traceback for it,
    reports success for a write that failed, or lets an OSError of the write escape: the first is exit 0 with
    `durable: false` and a warning, the digest being the file's; the second is exit 2 with the old record."""
    from hoh import durable_io
    capacity = _capacity()
    real_write = capacity.write
    good(capsys, root)
    kept = fingerprint(root / FILE)

    def unsynced(path, entries):
        real_write(path, entries)
        raise durable_io.DurableWriteNotSynced(path, OSError(errno.EIO, "directory fsync failed"))
    monkeypatch.setattr(capacity, "write", unsynced)
    code, out, err = set_(capsys, root, "provider", "other", "2", "sessions")
    assert code == 0 and err.startswith("warning: the capacity registry is written, but")
    printed = json.loads(out)
    assert printed["durable"] is False and printed["entries"] == 2
    assert hashlib.sha256((root / FILE).read_bytes()).hexdigest() == printed["digest"]
    kept = fingerprint(root / FILE)

    def full(path, entries):
        raise durable_io.DiskFullError(path, 10, 0, "refused before writing")
    monkeypatch.setattr(capacity, "write", full)
    text = refused(set_(capsys, root, "provider", "third", "3", "sessions"), "set")
    assert "the old record stands; DiskFullError" in text
    assert fingerprint(root / FILE) == kept


# --- R5 what is printed ------------------------------------------------------------------------------------------------

def test_what_is_printed_is_ascii_escaped_and_bounded(tmp_path, capsys):
    """Catches terminal injection and a crash on a path a person typed: a state root whose name holds an escape
    sequence, a newline and a lone surrogate is shown as ASCII JSON that round-trips, and its refusal is one
    escaped line; a 5000-character path is refused in a bounded line."""
    odd = initialised(tmp_path / "st\x1b[31m\nate\udcff")
    printed = json.loads(set_(capsys, odd, "provider", "anthropic", "5", "sessions")[1])
    assert printed["storage_root"] == str(odd)
    code, out, err = show(capsys, odd)
    assert code == 0 and out.isascii() and "\x1b" not in out and json.loads(out)["path"] == str(odd / FILE)
    spoiled = initialised(tmp_path / "st\x1b[31m\nate\udcfe")
    (spoiled / FILE).mkdir()
    text = refused(show(capsys, spoiled), "show")
    assert text.isascii() and "\x1b" not in text and "\\x1b" in text and "\\udcfe" in text
    code, out, err = show(capsys, tmp_path / ("x\x1b\n" + "y" * 5000))
    assert code == 2 and out == "" and err.startswith("capacity show refused: ")
    assert err.isascii() and err.count("\n") == 1 and len(err) < 400 and "characters)" in err


# --- R6 the command is wired where the product executes it, and the helpers are what they say --------------------

def test_both_commands_are_wired_into_the_parser_and_the_help_names_the_vocabulary(capsys):
    """Catches a command that is not reachable from `hoh`: the parser maps `capacity set` and `capacity show` to
    the command functions `cli.main` calls, the group needs a subcommand, and the help of `set` names every kind
    and unit the module accepts (so a vocabulary change cannot leave the help behind)."""
    capacity = _capacity()
    parser = cli.build_parser()
    assert parser.parse_args(["capacity", "set", "provider", "a", "1", "sessions"]).func is cli.cmd_capacity_set
    assert parser.parse_args(["capacity", "show"]).func is cli.cmd_capacity_show
    with pytest.raises(SystemExit) as stop:
        parser.parse_args(["capacity"])
    assert stop.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["capacity", "set", "--help"])
    helped = capsys.readouterr().out
    for word in (*capacity.KINDS, *capacity.UNITS):
        assert word in helped, word


def test_the_helpers_are_exact_and_pure():
    """Catches a location that moves, a limit parser that is looser than the grammar, a `set_entry` that
    mutates or does not replace by key, and a `printable` that lets a control character through or does not
    bound: literal expectations, no module constant."""
    capacity = _capacity()
    assert capacity.FILE_NAME == "capacity-registry.json"
    assert capacity.location("/x/state") == Path("/x/state/capacity-registry.json")
    assert [capacity.parse_limit(t) for t in ("0", "7", "10", "1000000000000000", "9999999999999999")] == [
        0, 7, 10, 10 ** 15, 9999999999999999]
    for text in ("", "00", "01", "-0", "+0", "0 ", " 0", "0\n", "1_0", "1e1", "١", "9" * 17, 5, None, b"1"):
        with pytest.raises(capacity.CapacityError) as caught:
            capacity.parse_limit(text)
        assert caught.value.reason == "invalid"
    first = capacity.Entry(kind="provider", name="a", limit=1, unit="sessions", source="declared",
                           as_of="2026-10-12T09:00:00Z")
    entries, previous = capacity.set_entry(None, first)
    assert entries == (first,) and previous is None
    registry = capacity.parse(capacity.render([first]))
    second = capacity.Entry(kind="provider", name="a", limit=2, unit="tokens", source="measured",
                            as_of="2026-10-13T09:00:00Z")
    entries, previous = capacity.set_entry(registry, second)
    assert entries == (second,) and previous == first and registry.entries == (first,)
    assert capacity.printable("\x1b[2J\n\udcff") == "\\x1b[2J\\n\\udcff"
    assert capacity.printable("a" * 240) == "a" * 240
    assert capacity.printable("a" * 241) == "a" * 240 + "... (241 characters)"
    assert capacity.printable(12) == "12" and capacity.printable("x", 0) == "... (1 characters)"
    assert capacity.view(registry) == {"digest": registry.digest, "entries": [first.as_dict()]}
