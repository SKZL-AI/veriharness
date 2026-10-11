"""How the merge gate reads a report, and how the gate's own files are written (follow-up to I3).

`hoh merge-gate check` reads each review report once. This file pins three things the first version left
open: a report that is swapped for a FIFO (or a link, or anything that is not a file) between the moment
the gate looks at it and the moment it opens it must not make the gate wait; an absurdly large report is
not read into memory; and the gate's source and its tests are plain ASCII (a raw U+2028 or U+2029 in a
test file shows as a line break in some editors and is split by `str.splitlines()`).

Everything happens inside `tmp_path`. A FIFO is created with `os.mkfifo` and never given a writer; every
step that could wait is under a SIGALRM deadline that turns a hang into a failure.
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib
import json
import os
import signal
import socket
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _mg():
    return importlib.import_module("hoh.mergegate")


class Blocked(Exception):
    """A step that should return at once was still waiting at the deadline."""


@contextlib.contextmanager
def _deadline(seconds: int = 10):
    def fire(_signum, _frame):
        raise Blocked(f"still waiting after {seconds} s: the gate blocks on something that is not a file")

    old = signal.signal(signal.SIGALRM, fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _read(path: Path, **kw):
    """`_read_report` on one path: its row (problems are codes, never exceptions)."""
    return _mg()._read_report(str(path), now=kw.get("now", time.time()), min_age=kw.get("min_age", 0.0))


def _fifo(tmp_path: Path, name: str = "fifo.md") -> Path:
    path = tmp_path / name
    os.mkfifo(path)
    return path


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def _rchar() -> int:
    """Bytes this process has read so far, by any means (`/proc/self/io`); skip where it cannot be read."""
    try:
        fd = os.open("/proc/self/io", os.O_RDONLY)
    except OSError as exc:
        pytest.skip(f"/proc/self/io is not readable here: {exc}")
    try:
        text = os.read(fd, 4096).decode()
    finally:
        os.close(fd)
    return next(int(line.split(":")[1]) for line in text.splitlines() if line.startswith("rchar:"))


# --- the gate's files are plain ASCII ------------------------------------------------------------

_PLAIN = frozenset(range(0x20, 0x7F)) | {ord("\n"), ord("\t")}


@pytest.mark.parametrize("rel", ["src/hoh/mergegate.py", "tests/test_merge_gate.py",
                                 "tests/test_merge_gate_read.py"])
def test_the_gate_and_its_tests_are_written_in_plain_ascii(rel):
    text = (ROOT / rel).read_bytes().decode("utf-8")
    bad = [(n, f"U+{ord(ch):04X}") for n, line in enumerate(text.split("\n"), 1)
           for ch in line if ord(ch) not in _PLAIN]
    assert not bad, f"{rel}: characters outside printable ASCII (line, code point), write them as \\u escapes: {bad[:8]}"


def test_the_escapes_still_stand_for_the_characters_the_cases_use():
    # the raw characters were replaced by escapes; the cases must still contain exactly those characters
    tree = ast.parse((ROOT / "tests" / "test_merge_gate.py").read_text(encoding="utf-8"))
    strings = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
               and isinstance(node.value, str)}
    for char in (chr(0x2028), chr(0x2029), "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e"):
        assert char in strings, f"no string constant is U+{ord(char):04X} in tests/test_merge_gate.py"
    for tail in ("BLOCKING_OPEN: " + chr(0x00B2), "BLOCKING_OPEN: " + chr(0x0660)):
        assert tail in strings, repr(tail)


# --- a report that is swapped for something that is not a file ----------------------------------


def test_a_report_swapped_for_a_fifo_after_the_check_does_not_block_the_gate(tmp_path, monkeypatch):
    fifo = _fifo(tmp_path)
    regular = tmp_path / "decoy.md"
    regular.write_text("a file\n", encoding="utf-8")
    real_stat = os.stat
    path = os.path.realpath(fifo)

    def lying_stat(target, *args, **kw):
        # the race, made deterministic: whoever asks the path "what are you?" is told "a regular file"
        try:
            same = os.fspath(target) == path
        except TypeError:
            same = False
        return real_stat(regular if same else target, *args, **kw)

    monkeypatch.setattr(os, "stat", lying_stat)
    with _deadline():
        row = _read(Path(path))
    assert row["data"] is None and row["sha256"] is None and row["size"] is None
    assert row["problems"] == [("REPORT_UNREADABLE", "the report is not a regular file")]


@pytest.mark.parametrize("kind", ["fifo", "directory", "device", "socket"])
def test_only_a_regular_file_is_read_and_the_descriptor_is_always_closed(tmp_path, monkeypatch, kind):
    sock = None
    if kind == "socket":
        monkeypatch.chdir(tmp_path)                 # a socket's name is limited to about 100 bytes: bind a short one
        sock = socket.socket(socket.AF_UNIX)
        sock.bind("s.sock")
        target = tmp_path / "s.sock"
    elif kind == "fifo":
        target = _fifo(tmp_path)
    elif kind == "directory":
        target = tmp_path / "adir"
        target.mkdir()
    else:
        target = Path("/dev/null")
        if not target.is_char_device():
            pytest.skip("no /dev/null")
    before = _open_fds()
    try:
        with _deadline():
            rows = [_read(target) for _ in range(20)]
        after = _open_fds()
    finally:
        if sock is not None:
            sock.close()
    assert all(r["problems"] == [("REPORT_UNREADABLE", "the report is not a regular file")] for r in rows)
    assert after == before, "a refused report leaves a descriptor open"


def test_a_report_swapped_for_a_link_after_its_path_was_resolved_is_not_followed(tmp_path, monkeypatch):
    target = tmp_path / "report.md"
    target.write_text("VERDICT: RELEASE_YES\n", encoding="utf-8")
    link = tmp_path / "swapped-in.md"
    link.symlink_to(target)
    real_realpath = os.path.realpath

    def stale_realpath(p, *args, **kw):
        # the path was resolved to the link's own name; by the time it is opened, it is a link
        return str(link) if os.fspath(p) == str(link) else real_realpath(p, *args, **kw)

    monkeypatch.setattr(os.path, "realpath", stale_realpath)
    row = _read(link)
    assert row["data"] is None
    assert row["problems"] == [("REPORT_UNREADABLE", "the report is a symbolic link (not followed)")]


@pytest.mark.parametrize("depth", [1, 2])
def test_a_directory_swapped_for_a_link_after_the_path_was_resolved_is_not_entered(
        tmp_path, monkeypatch, depth):
    real_dir = tmp_path / "real"
    (real_dir / "sub").mkdir(parents=True)
    (real_dir / "sub" / "report.md").write_text("VERDICT: RELEASE_YES\n", encoding="utf-8")
    (real_dir / "report.md").write_text("VERDICT: RELEASE_YES\n", encoding="utf-8")
    link = tmp_path / "swapped-in"
    link.symlink_to(real_dir)
    named = link / "report.md" if depth == 1 else link / "sub" / "report.md"
    real_realpath = os.path.realpath

    def stale_realpath(p, *args, **kw):
        # the path was resolved to a name whose directory is, by the time it is opened, a link
        return str(named) if os.fspath(p) == str(named) else real_realpath(p, *args, **kw)

    monkeypatch.setattr(os.path, "realpath", stale_realpath)
    before = _open_fds()
    row = _read(named)
    assert row["data"] is None
    assert row["problems"] == [("REPORT_UNREADABLE",
                                "a directory on the path of the report is a symbolic link (not followed)")]
    assert _open_fds() == before


def test_an_ordinary_report_is_still_read_whole_and_closed(tmp_path, monkeypatch):
    report = tmp_path / "report.md"
    payload = ("VERDICT: RELEASE_YES\n" * 3).encode()
    report.write_bytes(payload)
    before = _open_fds()
    row = _read(report)
    assert row["problems"] == [] and row["data"] == payload and row["size"] == len(payload)
    assert row["sha256"] == hashlib.sha256(payload).hexdigest()
    assert _open_fds() == before
    # how it is opened: no waiting, no link, no controlling terminal, not inherited; directories on the way
    # are opened as directories without following links
    calls: list[tuple[str, int, object]] = []
    real_open = os.open

    def recording(path, flags, *args, **kw):
        calls.append((os.fspath(path), flags, kw.get("dir_fd")))
        return real_open(path, flags, *args, **kw)

    monkeypatch.setattr(os, "open", recording)
    fd = _mg()._open_regular(str(report))
    monkeypatch.undo()
    try:
        assert not os.get_inheritable(fd)
        assert stat.S_ISREG(os.fstat(fd).st_mode)
    finally:
        os.close(fd)
    *dirs, leaf = calls
    want_leaf = os.O_NONBLOCK | os.O_NOFOLLOW | os.O_NOCTTY | os.O_CLOEXEC
    assert leaf[1] & want_leaf == want_leaf and leaf[1] & os.O_DIRECTORY == 0, calls
    want_dir = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NOCTTY | os.O_CLOEXEC
    assert dirs and all(flags & want_dir == want_dir and dir_fd is not None or path == "/"
                        for path, flags, dir_fd in dirs), calls


# --- a report is read with a bound ---------------------------------------------------------------


def test_a_report_larger_than_the_bound_is_not_read_and_one_at_the_bound_is(tmp_path, monkeypatch):
    mg = _mg()
    assert (1 << 20) <= mg.MAX_REPORT_BYTES <= (64 << 20), "the bound must leave room for real reports"
    monkeypatch.setattr(mg, "MAX_REPORT_BYTES", 4096)
    exact = tmp_path / "exact.md"
    exact.write_bytes(b"x" * 4096)
    over = tmp_path / "over.md"
    over.write_bytes(b"x" * 4097)
    row = _read(exact)
    assert row["problems"] == [] and row["size"] == 4096
    before = _open_fds()
    for _ in range(5):
        row = _read(over)
    assert row["data"] is None and row["sha256"] is None and row["size"] is None
    assert row["problems"] == [("REPORT_UNREADABLE", "the report is larger than 4096 bytes: not read")]
    assert _open_fds() == before, "an oversize report leaves a descriptor open"


def test_the_bound_stops_the_read_it_does_not_check_afterwards(tmp_path, monkeypatch):
    mg = _mg()
    monkeypatch.setattr(mg, "MAX_REPORT_BYTES", 4096)
    big = tmp_path / "big.md"
    big.write_bytes(b"y" * (1 << 20))
    got = {"bytes": 0}
    real_read = os.read

    def counting(fd, n):
        chunk = real_read(fd, n)
        got["bytes"] += len(chunk)
        return chunk

    # whatever the shape of the read (os.read, a file object, readall), the process must not read the file:
    # five refusals of a 1 MiB report may cost the kernel at most 64 KiB of reads in all
    start = _rchar()
    monkeypatch.setattr(os, "read", counting)
    for _ in range(5):
        row = _read(big)
        assert [code for code, _ in row["problems"]] == ["REPORT_UNREADABLE"]
    monkeypatch.undo()
    spent = _rchar() - start
    assert got["bytes"] <= 5 * 4097, f"{got['bytes']} bytes were read through os.read for a bound of 4096"
    assert spent < 64 * 1024, f"the process read {spent} bytes for five refusals of a 1 MiB report"


# --- the whole command ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    done = subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=T",
                           "-c", "commit.gpgsign=false", *args],
                          check=True, capture_output=True, text=True, env=env)
    return done.stdout.strip()


def _world(tmp_path: Path) -> SimpleNamespace:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "docs" / "spec.md").write_text("# spec\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "docs" / "spec.md").write_text("# spec\nrevision 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "candidate")
    cand = _git(repo, "rev-parse", "HEAD")
    spec_sha = hashlib.sha256(b"# spec\nrevision 2\n").hexdigest()
    return SimpleNamespace(repo=repo, base=base, cand=cand, spec_sha=spec_sha)


def _good_report(w: SimpleNamespace, tmp_path: Path, name: str) -> Path:
    footer = {"VERDICT": "RELEASE_YES", "CANDIDATE": w.cand, "BASE": w.base, "SPEC_SHA256": w.spec_sha,
              "BLOCKING_OPEN": "0", "COMPLETENESS": "COMPLETE"}
    path = tmp_path / f"{name}.md"
    path.write_text(f"# report {name}\n\n" + "".join(f"{k}: {v}\n" for k, v in footer.items()),
                    encoding="utf-8")
    stamp = int(time.time()) - 3600
    os.utime(path, (stamp, stamp))
    return path


@pytest.mark.parametrize("kind", ["fifo", "oversize"])
def test_a_report_the_gate_cannot_read_is_no_verdict_and_the_other_review_still_counts(
        tmp_path, monkeypatch, kind):
    mg = _mg()
    w = _world(tmp_path)
    good = _good_report(w, tmp_path, "a")
    if kind == "fifo":
        bad = _fifo(tmp_path)
    else:
        monkeypatch.setattr(mg, "MAX_REPORT_BYTES", 4096)
        bad = tmp_path / "huge.md"
        bad.write_bytes(b"z" * 5000)
    receipt = tmp_path / "receipt.json"
    with _deadline():
        out = mg.run_check(repo=str(w.repo), candidate=w.cand, base=w.base, spec="docs/spec.md",
                           reviews=[f"a={good}", f"b={bad}"], receipt=str(receipt))
    assert (out.code, out.err) == (1, "")
    body = json.loads(receipt.read_text(encoding="utf-8"))
    rows = {r["review"]: r for r in body["reviews"]}
    assert (rows["b"]["status"], rows["b"]["problems"]) == ("NO_VERDICT", ["REPORT_UNREADABLE"])
    assert rows["a"]["status"] == "RELEASE_YES"
    assert body["aggregate"] == "NOT_MERGE_ELIGIBLE"


# --- the receipt is read the same way ----------------------------------------------------------------


def test_verify_and_the_read_back_of_a_receipt_do_not_wait_on_something_that_is_not_a_file(
        tmp_path, monkeypatch):
    mg = _mg()
    w = _world(tmp_path)
    reviews = [f"a={_good_report(w, tmp_path, 'a')}", f"b={_good_report(w, tmp_path, 'b')}"]
    gate = dict(repo=str(w.repo), candidate=w.cand, base=w.base, spec="docs/spec.md", reviews=reviews)
    # control: an ordinary receipt is written, read back and verified
    receipt = tmp_path / "receipt.json"
    assert mg.run_check(**gate, receipt=str(receipt)).code == 0
    assert mg.run_verify(str(receipt)).code == 0
    before = _open_fds()
    # `verify` on a FIFO, a directory and a receipt over the bound: no decision, exit 2, nothing on stdout
    fifo = _fifo(tmp_path, "receipt.fifo")
    with _deadline():
        out = mg.run_verify(str(fifo))
    assert (out.code, out.out) == (2, "") and out.err.endswith(": the receipt is not a regular file\n"), out
    out = mg.run_verify(str(tmp_path))
    assert (out.code, out.out) == (2, "") and "the receipt is not a regular file" in out.err, out
    monkeypatch.setattr(mg, "MAX_REPORT_BYTES", 100)
    out = mg.run_verify(str(receipt))
    assert (out.code, out.out) == (2, "") and "the receipt is larger than 100 bytes: not read" in out.err, out
    monkeypatch.undo()
    # the read-back after `check` created the receipt: a FIFO where the file should be does not stall the gate
    monkeypatch.setattr(mg, "durable_create", lambda path, text: os.mkfifo(path))
    with _deadline():
        out = mg.run_check(**gate, receipt=str(tmp_path / "second.json"))
    assert (out.code, out.out) == (2, ""), out
    assert "the receipt exists but cannot be read back (the receipt is not a regular file)" in out.err, out
    assert _open_fds() == before
