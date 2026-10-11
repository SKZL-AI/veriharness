"""`hoh wait --report PATH` and `hoh wait --pid N` (I12, slices 1b and 1c of the row P5-43):
the wait on a review report (C2, C8) and the wait on a process id (C3), read-only.

Every test builds its files under `tmp_path`: reports are plain files, and the process table is a
synthetic proc root (`<root>/<pid>/stat`, `<root>/sys/kernel/random/boot_id`) that `runwait.PROC_ROOT`
is pointed at. A process "ends" by its directory being renamed away; nothing is removed. The clocks
are injected: nothing sleeps, and every fake `sleep` is guarded (a positive duration and a call
budget), so a wait that loops without sleeping fails a test instead of spinning it. Nothing starts
Herdr, an agent or a process: a `herdr` stub first on `PATH` logs its argv and fails,
`subprocess.run` and `subprocess.Popen` fail the test, and the stub's log is asserted empty at the
end (autouse fixture). No test reads a real report, run or state root.

The exit codes are written out as numbers on purpose: they are the contract of the command, and a
table imported from the module would agree with a mutated module.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import signal
import subprocess
import threading
import types
from pathlib import Path

import pytest

from hoh import cli, mergegate, runwait
from hoh.contracts import Candidate, Condition, RunState, Stage
from hoh.store import RunStore

#: A fixed "wall clock now" for the report waits that inject their own clock.
NOW = 1_000_000.0
NOW_NS = 1_000_000 * 10 ** 9

SHA = "a" * 64

REPORT_KEYS = ["schema", "target", "report", "outcome", "matched", "exit_code", "status", "code",
               "verdict", "candidate", "base", "spec_sha256", "blocking_open", "completeness",
               "report_sha256", "size", "age_s", "min_age_s", "waited_s", "polls"]
PID_KEYS = ["schema", "target", "pid", "outcome", "matched", "exit_code", "how", "start_ticks",
            "boot_id", "waited_s", "polls"]

RUN_EXIT_MAP = {"accepted": 0, "reached": 0, "failed": 1, "cancelled": 1, "blocked": 3,
                "parked": 4, "paused": 5, "idle": 6, "died": 7, "held": 8, "timeout": 124}

BOOT_ID = "3f2b8a1c-9d4e-4b7a-8c1d-5e6f7a8b9c0d"   # a synthetic UUID on the export allowlist (records O265: the previous literal was not)
PID = 4242


# --------------------------------------------------------------------------- #
# Fixtures: no process, no Herdr, no sleep
# --------------------------------------------------------------------------- #


class Clock:
    """An injected clock. `sleep` is guarded: a positive duration, at most `budget` calls.
    `hooks[n]` runs after the n-th sleep (1-based): a writer that acts while the wait sleeps."""

    def __init__(self, budget: int = 8) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.budget = budget
        self.hooks: dict[int, object] = {}

    def sleep(self, seconds: float) -> None:
        assert seconds > 0, f"a sleep of {seconds!r} seconds: a wait that does not wait"
        assert len(self.sleeps) < self.budget, f"more than {self.budget} sleeps: a runaway loop"
        self.sleeps.append(seconds)
        self.now += seconds
        hook = self.hooks.get(len(self.sleeps))
        if hook is not None:
            hook()

    def monotonic(self) -> float:
        return self.now

    def wall(self) -> float:
        return NOW + self.now


def _use_clock(monkeypatch, clock: Clock) -> Clock:
    monkeypatch.setattr(runwait, "time", types.SimpleNamespace(
        sleep=clock.sleep, monotonic=clock.monotonic))
    return clock


@pytest.fixture(autouse=True)
def _nothing_runs(tmp_path, monkeypatch):
    log = tmp_path / "herdr-stub" / "calls.log"
    stub = tmp_path / "herdr-stub" / "herdr"
    stub.parent.mkdir()
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 1\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub.parent}{os.pathsep}{os.environ.get('PATH', '')}")

    def forbidden(*args, **kwargs):
        pytest.fail("a test started a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    # By default nothing may sleep; a test that waits installs its own guarded clock.
    _use_clock(monkeypatch, Clock(budget=0))
    yield
    assert not log.exists() or log.read_text(encoding="utf-8") == "", "a test called herdr"


# --------------------------------------------------------------------------- #
# Builders: reports
# --------------------------------------------------------------------------- #


def _footer(**over: str) -> str:
    fields = {"VERDICT": "RELEASE_YES", "CANDIDATE": "c" * 40, "BASE": "b" * 40,
              "SPEC_SHA256": SHA, "BLOCKING_OPEN": "0", "COMPLETENESS": "COMPLETE"}
    fields.update(over)
    return "".join(f"{key}: {value}\n" for key, value in fields.items())


def _report(path: Path, text: str | bytes | None = None, *, age: float | None = 3600.0,
            at: float = NOW) -> Path:
    """A report file whose modification time is `age` seconds before `at` (None: left alone)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is None:
        text = "The review.\n\nAll criteria were executed.\n\n" + _footer()
    if isinstance(text, str):
        path.write_text(text, encoding="utf-8")
    else:
        path.write_bytes(text)
    if age is not None:
        ns = round(at * 1e9) - round(age * 1e9)
        os.utime(path, ns=(ns, ns))
    return path


def _wait_report(path, clock: Clock | None = None, **kw):
    """`runwait.wait_report` with the injected clock (when there is one)."""
    if clock is not None:
        kw.setdefault("sleep", clock.sleep)
        kw.setdefault("monotonic", clock.monotonic)
        kw.setdefault("now", clock.wall)
    else:
        kw.setdefault("now", lambda: NOW)
    return runwait.wait_report(str(path), **kw)


def _doc(result) -> dict:
    line = runwait.render_report(result)
    assert "\n" not in line and line.isascii()
    return json.loads(line)


def _recording(monkeypatch) -> list[str]:
    """The statuses of the looks, in order (the real `look_report` runs)."""
    seen: list[str] = []
    real = runwait.look_report

    def look(*args, **kwargs):
        got = real(*args, **kwargs)
        seen.append(got.status)
        return got

    monkeypatch.setattr(runwait, "look_report", look)
    return seen


# --------------------------------------------------------------------------- #
# Builders: a synthetic proc root
# --------------------------------------------------------------------------- #


def _stat_line(pid: int, *, comm: str = "worker", state: str = "S", start: int = 123456,
               threads: int = 1) -> str:
    """A `/proc/<pid>/stat` line of 52 fields, as the kernel prints it (`proc(5)`)."""
    after = ["1", str(pid), str(pid), "0", "-1", "4194560", "100", "0", "0", "0", "5", "6", "0", "0",
             "20", "0", str(threads), "0", str(start)]            # fields 4 .. 22
    after += ["1000000", "100", "18446744073709551615"] + ["0"] * 26   # fields 23 .. 52 (30 values)
    return f"{pid} ({comm}) {state} " + " ".join(after) + "\n"


def _proc_root(tmp_path: Path, monkeypatch, *, boot: str | None = BOOT_ID) -> Path:
    root = tmp_path / "proc"
    if boot is not None:
        (root / "sys" / "kernel" / "random").mkdir(parents=True)
        (root / "sys" / "kernel" / "random" / "boot_id").write_text(boot + "\n", encoding="utf-8")
    else:
        root.mkdir()
    monkeypatch.setattr(runwait, "PROC_ROOT", str(root))
    return root


def _put(root: Path, pid: int, **kw) -> Path:
    """The process `pid` as the stat line says (a new one, or a change of the old one)."""
    directory = root / str(pid)
    directory.mkdir(exist_ok=True)
    stat_file = directory / "stat"
    stat_file.write_text(_stat_line(pid, **kw), encoding="utf-8")
    return stat_file


def _vanish(root: Path, pid: int) -> None:
    """The process ended: its directory is no longer there (moved aside, never removed)."""
    os.rename(root / str(pid), root / f"ended-{pid}-{len(list(root.iterdir()))}")


def _wait_pid(clock: Clock | None = None, pid: int = PID, **kw):
    if clock is not None:
        kw.setdefault("sleep", clock.sleep)
        kw.setdefault("monotonic", clock.monotonic)
    return runwait.wait_pid(pid, **kw)


def _pid_doc(result) -> dict:
    line = runwait.render_pid(result)
    assert "\n" not in line and line.isascii()
    return json.loads(line)


# --------------------------------------------------------------------------- #
# Running the command
# --------------------------------------------------------------------------- #


class Answer:
    def __init__(self, code, out, err):
        self.code, self.out, self.err = code, out, err
        self.doc = json.loads(out) if out.strip() else None


def _ask(capsys, *argv: str, root: Path | None = None) -> Answer:
    try:
        code = cli.main(["--root", str(root or Path("/nonexistent-root")), "wait", *argv])
    except SystemExit as exc:                       # argparse's own refusals
        code = exc.code
    captured = capsys.readouterr()
    if code == 2 and "invalid choice: 'wait'" in captured.err:
        pytest.fail("`hoh wait` does not exist: the parser has no such command")
    return Answer(code, captured.out, captured.err)


def _tree(root: Path) -> dict[str, tuple]:
    """Names, kinds, sizes, modification times and digests: everything a write would change."""
    seen = {}
    for base, dirs, files in os.walk(root):
        for name in ["", *dirs, *files]:
            path = os.path.join(base, name) if name else base
            info = os.lstat(path)
            digest = None
            if os.path.isfile(path) and not os.path.islink(path):
                digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            seen[os.path.relpath(path, root)] = (info.st_mode, info.st_size, info.st_mtime_ns,
                                                 info.st_ino, digest)
    return seen


# --------------------------------------------------------------------------- #
# W1: the stable age
# --------------------------------------------------------------------------- #


def test_a_report_that_does_not_exist_yet_is_waited_for_and_final_only_after_the_stable_age(
        tmp_path, monkeypatch):
    """The report is missing at the first two looks, written (complete footer) while the wait
    sleeps, and is final only 60 s after that write: not at the first look that sees it. Catches
    a wait that reads a report that was just written (the loop this command replaces)."""
    report = tmp_path / "reviews" / "r1.md"
    clock = Clock(budget=20)
    clock.hooks[2] = lambda: _report(report, age=0.0, at=clock.wall())     # written at t = 20
    seen = _recording(monkeypatch)
    result = _wait_report(report, clock, timeout=3600, poll=10)
    assert seen == ["missing", "missing"] + ["unstable"] * 6 + ["yes"]
    assert result.outcome == "report_yes" and result.matched is True and result.exit_code == 0
    assert clock.sleeps == [10] * 8 and result.polls == 9 and result.waited_s == 80.0
    assert result.look.age_s == 60.0 and result.look.code is None


def test_the_stable_age_boundary_is_the_gates(tmp_path):
    """Age 59.9 is unstable, 60.0 and 60.1 are stable, 0 and a report from the future (-5) are
    unstable: the gate's `age >= min_age`, one look each."""
    expected = {59.9: ("unstable", 124), 60.0: ("yes", 0), 60.1: ("yes", 0),
                0.0: ("unstable", 124), -5.0: ("unstable", 124)}
    for index, (age, (status, code)) in enumerate(expected.items()):
        report = _report(tmp_path / f"r{index}.md", age=age)
        result = _wait_report(report, timeout=0)
        assert (result.look.status, result.exit_code) == (status, code), age
        assert result.polls == 1
        assert result.look.code == (None if status == "yes" else "REPORT_UNSTABLE")
        assert result.min_age == 60.0
    # the same boundary through the gate's own reader: the wait agrees with `hoh merge-gate`
    young = _report(tmp_path / "gate.md", age=59.9)
    assert mergegate._read_report(str(young), now=NOW, min_age=mergegate.DEFAULT_MIN_AGE)["problems"]


def test_a_footer_that_arrives_late_starts_the_stable_age_again(tmp_path, monkeypatch):
    """A report that is old but has no footer is waited for; the footer is added at t = 20 and
    the file's time restarts: final at t = 80, not at t = 20. Catches an age that is kept from
    an earlier look."""
    report = _report(tmp_path / "r.md", "The review so far.\n\nWork in progress.\n", age=3600.0)
    clock = Clock(budget=20)
    clock.hooks[2] = lambda: _report(
        report, "The review.\n\n" + _footer(), age=0.0, at=clock.wall())
    seen = _recording(monkeypatch)
    result = _wait_report(report, clock, timeout=3600, poll=10)
    assert seen == ["no_footer", "no_footer"] + ["unstable"] * 6 + ["yes"]
    assert result.exit_code == 0 and result.waited_s == 80.0 and result.polls == 9


def test_the_stable_age_is_read_from_the_gate_at_the_call(tmp_path, monkeypatch):
    """`mergegate.DEFAULT_MIN_AGE` is read at the call: a gate that says 5 s makes a 10 s old
    report final, one that says 100 s does not; an explicit `min_age` wins. Catches a copied
    constant."""
    report = _report(tmp_path / "r.md", age=10.0)
    monkeypatch.setattr(mergegate, "DEFAULT_MIN_AGE", 5.0)
    result = _wait_report(report, timeout=0)
    assert result.exit_code == 0 and result.min_age == 5.0
    assert _doc(result)["min_age_s"] == 5.0
    monkeypatch.setattr(mergegate, "DEFAULT_MIN_AGE", 100.0)
    result = _wait_report(report, timeout=0)
    assert result.exit_code == 124 and result.look.status == "unstable" and result.min_age == 100.0
    assert _wait_report(report, timeout=0, min_age=0).exit_code == 0
    assert _wait_report(report, timeout=0, min_age=10).exit_code == 0
    assert _wait_report(report, timeout=0, min_age=10.5).exit_code == 124


def test_the_report_is_resolved_at_every_look_and_the_answer_names_the_file_it_read(tmp_path):
    """A link that is re-pointed between two looks is followed: the first look reads a young
    file (unstable), the second a stable one, and the answer names the second file and its
    digest. Catches a path that is resolved once."""
    first = _report(tmp_path / "first.md", age=1.0)
    second = _report(tmp_path / "second.md", "Another review.\n\n" + _footer(VERDICT="RELEASE_NO"),
                     age=3600.0)
    link = tmp_path / "current.md"
    os.symlink(first, link)

    def repoint() -> None:
        scratch = tmp_path / "current.tmp"
        os.symlink(second, scratch)
        os.replace(scratch, link)

    clock = Clock(budget=4)
    clock.hooks[1] = repoint
    result = _wait_report(link, clock, timeout=3600, poll=10)
    assert result.polls == 2 and result.outcome == "report_no" and result.exit_code == 1
    doc = _doc(result)
    assert doc["report"] == os.path.realpath(second) != os.path.realpath(first)
    assert doc["report_sha256"] == hashlib.sha256(second.read_bytes()).hexdigest()
    assert doc["size"] == second.stat().st_size


# --------------------------------------------------------------------------- #
# W2: the footer
# --------------------------------------------------------------------------- #

PROSE = "The review.\n\nEvery criterion was executed by the reviewer.\n\n"


def _duplicate_key() -> str:
    return _footer() + "BASE: " + "d" * 40 + "\n"


FOOTER_TABLE = [
    # name, report text, status, outcome, exit code
    ("yes", PROSE + _footer(), "yes", "report_yes", 0),
    ("no", PROSE + _footer(VERDICT="RELEASE_NO"), "no", "report_no", 1),
    ("blocking", PROSE + _footer(BLOCKING_OPEN="2"), "no", "report_no", 1),
    ("zeros", PROSE + _footer(BLOCKING_OPEN="000"), "yes", "report_yes", 0),
    ("partial", PROSE + _footer(COMPLETENESS="PARTIAL"), "partial", "report_partial", 8),
    ("text after", PROSE + _footer() + "\nThanks for reading.\n", "no_footer", "timeout", 124),
    ("missing key", PROSE + _footer().replace("BASE: " + "b" * 40 + "\n", ""),
     "no_footer", "timeout", 124),
    ("no footer", "", "no_footer", "timeout", 124),
    ("prose only", PROSE, "no_footer", "timeout", 124),
    ("unknown key", PROSE + _footer() + "EXTRA: 1\n", "invalid", "report_invalid", 8),
    ("duplicate", PROSE + _duplicate_key(), "invalid", "report_invalid", 8),
    ("bad verdict", PROSE + _footer(VERDICT="MAYBE"), "invalid", "report_invalid", 8),
    ("bad count", PROSE + _footer(BLOCKING_OPEN="many"), "invalid", "report_invalid", 8),
]


def test_the_footer_decides_what_the_wait_says_and_a_writer_that_may_be_slow_is_not_judged(
        tmp_path):
    """13 reports, one look each: a complete footer is judged (yes / no / partial), a grammar
    violation is invalid, and a trailing block that is not (yet) a complete footer is waited for,
    never judged: a slow writer is not a verdict. Catches a wait that calls an unfinished report
    invalid, and a PARTIAL footer that is judged by its verdict."""
    assert len(FOOTER_TABLE) == 13
    for index, (name, text, status, outcome, code) in enumerate(FOOTER_TABLE):
        report = _report(tmp_path / f"r{index}.md", text)
        result = _wait_report(report, timeout=0)
        assert (result.look.status, result.outcome, result.exit_code) == (status, outcome, code), name
        assert result.matched is (outcome in ("report_yes", "report_no")), name
        footer_parsed = status in ("yes", "no", "partial")
        assert (result.look.verdict is not None) is footer_parsed, name
    # the gate's own footer code is named for a footer that is not complete or refused
    assert _wait_report(_report(tmp_path / "p.md", PROSE), timeout=0).look.code == "FOOTER_INCOMPLETE"
    assert _wait_report(_report(tmp_path / "u.md", PROSE + _footer() + "EXTRA: 1\n"),
                        timeout=0).look.code == "FOOTER_UNKNOWN_KEY"
    # waited for until the timeout, with the sleeps of the run wait
    clock = Clock(budget=8)
    slow = _report(tmp_path / "slow.md", PROSE + "VERDICT: RELEASE_YES\n", age=3600.0)
    result = _wait_report(slow, clock, timeout=25, poll=10)
    assert clock.sleeps == [10, 10, 5] and result.exit_code == 124 and result.polls == 4
    assert result.look.status == "no_footer"


def test_a_report_nobody_can_read_as_a_file_ends_the_wait_at_once_and_never_blocks(
        tmp_path, monkeypatch):
    """A FIFO nobody writes to, a directory and a file over the gate's size limit: `unreadable`,
    exit 8, at the first look, without a sleep and without blocking."""
    fifo = tmp_path / "fifo.md"
    os.mkfifo(fifo)
    directory = tmp_path / "dir.md"
    directory.mkdir()
    big = _report(tmp_path / "big.md", PROSE + _footer())
    monkeypatch.setattr(mergegate, "MAX_REPORT_BYTES", big.stat().st_size - 1)
    for target in (fifo, directory, big):
        answers: list = []
        worker = threading.Thread(
            target=lambda t=target: answers.append(_wait_report(t, timeout=3600)), daemon=True)
        worker.start()
        worker.join(20)
        assert not worker.is_alive(), f"the wait blocked on {target.name}"
        result = answers[0]
        assert (result.look.status, result.outcome, result.exit_code) == (
            "unreadable", "report_invalid", 8), target.name
        assert result.matched is False and result.polls == 1 and result.waited_s == 0.0
        assert result.look.code == "REPORT_UNREADABLE"


# --------------------------------------------------------------------------- #
# W3: the bounds, the answer
# --------------------------------------------------------------------------- #


def test_the_report_wait_is_bounded_like_the_run_wait(tmp_path, monkeypatch):
    """A report that never appears: the default 3600 s at 10 s is 361 looks, `timeout=0` is one
    look and no sleep, 25 s at 10 s sleeps 10, 10, 5. Also with the module's own clock (looked
    up at the call). Catches a loop that does not end."""
    missing = tmp_path / "never.md"
    clock = Clock(budget=400)
    result = _wait_report(missing, clock)
    assert clock.sleeps == [10.0] * 360 and result.polls == 361
    assert (result.outcome, result.exit_code, result.matched) == ("timeout", 124, False)
    assert result.waited_s == 3600.0 and result.look.status == "missing"

    clock = Clock(budget=0)
    result = _wait_report(missing, clock, timeout=0)
    assert clock.sleeps == [] and result.polls == 1 and result.exit_code == 124

    clock = Clock(budget=8)
    result = _wait_report(missing, clock, timeout=25, poll=10)
    assert clock.sleeps == [10, 10, 5] and result.polls == 4 and result.waited_s == 25.0

    clock = _use_clock(monkeypatch, Clock(budget=8))
    result = runwait.wait_report(str(missing), timeout=25, poll=10, now=lambda: NOW)
    assert clock.sleeps == [10, 10, 5] and result.exit_code == 124


def test_the_report_wait_refuses_a_tight_or_unbounded_loop_before_it_reads_anything(
        tmp_path, monkeypatch):
    """A poll under 1 s, a negative or non-finite bound, a boolean or a string, a NaN or negative
    `min_age`, an empty path: refused before the gate's reader is called."""
    def reader(*args, **kwargs):
        pytest.fail("the report was read although the request was refused")

    monkeypatch.setattr(mergegate, "_read_report", reader)
    path = str(tmp_path / "r.md")
    for poll in (0, 0.5, -1, math.nan, math.inf, True, "10"):
        with pytest.raises(ValueError):
            runwait.wait_report(path, timeout=5, poll=poll)
    for timeout in (-1, -0.1, math.nan, math.inf, True, "5"):
        with pytest.raises(ValueError):
            runwait.wait_report(path, timeout=timeout, poll=10)
    for min_age in (math.nan, -1, -0.001, math.inf, True, "60"):
        with pytest.raises(ValueError):
            runwait.wait_report(path, timeout=0, poll=10, min_age=min_age)
    with pytest.raises(runwait.WaitRefused):
        runwait.wait_report("", timeout=0)


def test_a_report_that_is_final_at_the_start_is_answered_at_once(tmp_path):
    """A complete, stable report with the default bounds: one look, no sleep (the clock of this
    module fails on any sleep), `waited_s` 0 -- also a stale one (L17) and a `no`."""
    for index, text in enumerate((PROSE + _footer(), PROSE + _footer(VERDICT="RELEASE_NO"))):
        clock = Clock(budget=0)
        result = _wait_report(_report(tmp_path / f"r{index}.md", text), clock)
        assert result.polls == 1 and result.waited_s == 0.0 and clock.sleeps == []
        assert result.exit_code == index
    stale = _report(tmp_path / "stale.md", PROSE + _footer(CANDIDATE="1" * 40, BASE="2" * 40))
    result = _wait_report(stale, Clock(budget=0))
    assert result.exit_code == 0 and result.polls == 1


def test_the_report_wait_writes_nothing(tmp_path, capsys):
    """The tree around the report (the report itself, its directory, the current directory) is
    identical after waits that end as every kind of look; no marker, no copy, no lock."""
    base = tmp_path / "reviews"
    cases = [_report(base / "yes.md", PROSE + _footer()),
             _report(base / "young.md", age=5.0),
             _report(base / "nofooter.md", PROSE),
             _report(base / "bad.md", PROSE + _footer(VERDICT="MAYBE"))]
    before = _tree(tmp_path)
    for report in cases:
        clock = Clock(budget=4)
        _wait_report(report, clock, timeout=25, poll=10)
    _wait_report(base / "missing.md", Clock(budget=4), timeout=25, poll=10)
    for argv in (["--report", str(cases[0])], ["--report", str(base / "missing.md"),
                                               "--timeout", "0"]):
        _ask(capsys, *argv)
    assert _tree(tmp_path) == before
    assert not (base / "missing.md").exists()


def test_the_report_answer_is_one_ascii_line_with_the_documented_fields(tmp_path):
    """The fixed key order of the answer for a final report and for a missing one, ASCII only,
    footer values cut to 64, a non-ASCII path escaped."""
    report = _report(tmp_path / "reäport.md",
                     PROSE + _footer(CANDIDATE="f" * 100, BLOCKING_OPEN="0"), age=120.0)
    result = _wait_report(report, timeout=0)
    line = runwait.render_report(result)
    assert line.isascii() and "\n" not in line
    doc = json.loads(line)
    assert list(doc) == REPORT_KEYS
    assert doc["schema"] == "hoh-wait/1" and doc["target"] == "report"
    assert doc["report"].endswith("reäport.md")
    assert (doc["outcome"], doc["matched"], doc["exit_code"], doc["status"], doc["code"]) == (
        "report_yes", True, 0, "yes", None)
    assert doc["verdict"] == "RELEASE_YES" and doc["candidate"] == "f" * 64
    assert doc["base"] == "b" * 40 and doc["spec_sha256"] == SHA
    assert doc["blocking_open"] == "0" and doc["completeness"] == "COMPLETE"
    assert doc["report_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
    assert doc["size"] == report.stat().st_size
    assert doc["age_s"] == 120.0 and doc["min_age_s"] == 60.0
    assert doc["waited_s"] == 0.0 and doc["polls"] == 1

    gone = _doc(_wait_report(tmp_path / "gone.md", timeout=0))
    assert list(gone) == REPORT_KEYS
    assert (gone["outcome"], gone["matched"], gone["exit_code"], gone["status"], gone["code"]) == (
        "timeout", False, 124, "missing", "REPORT_MISSING")
    for key in ("verdict", "candidate", "base", "spec_sha256", "blocking_open", "completeness",
                "report_sha256", "size", "age_s"):
        assert gone[key] is None, key
    assert gone["min_age_s"] == 60.0


def test_a_long_path_is_cut_in_the_answer_and_a_report_with_a_bad_byte_is_still_read(tmp_path):
    """A 700-character path is cut to 400 in the answer; a report with an invalid UTF-8 byte in
    its prose and a complete footer is `yes` (the decode is lenient, as the gate's is)."""
    deep = tmp_path
    for part in ("a" * 200, "b" * 200, "c" * 200):
        deep = deep / part
    report = _report(deep / "r.md", PROSE + _footer())
    assert len(str(report)) > 600
    doc = _doc(_wait_report(report, timeout=0))
    assert doc["status"] == "yes" and len(doc["report"]) == 400
    assert doc["report"] == os.path.realpath(report)[:400]

    dirty = _report(tmp_path / "dirty.md", b"caf\xff the review\n\n" + _footer().encode())
    result = _wait_report(dirty, timeout=0)
    assert result.look.status == "yes" and result.exit_code == 0


# --------------------------------------------------------------------------- #
# W4: the process wait
# --------------------------------------------------------------------------- #


def test_a_process_that_ends_while_it_is_waited_for_is_ended(tmp_path, monkeypatch):
    """The process is there at the first two looks and gone at the third: `ended`, how `exited`,
    exit 0, matched, with the start time that was waited for."""
    root = _proc_root(tmp_path, monkeypatch)
    _put(root, PID, start=5000)
    clock = Clock(budget=8)
    clock.hooks[2] = lambda: _vanish(root, PID)
    result = _wait_pid(clock, timeout=3600, poll=10)
    assert (result.outcome, result.matched, result.exit_code, result.how) == (
        "ended", True, 0, "exited")
    assert result.start_ticks == 5000 and result.polls == 3 and result.waited_s == 20.0
    assert clock.sleeps == [10, 10]


def test_a_pid_that_is_not_there_at_the_start_is_gone_and_says_so(tmp_path, monkeypatch):
    """A pid that is not there at the first look: `gone`, exit 9, not matched: the wait did not
    see it end. One look, no sleep, no start time."""
    root = _proc_root(tmp_path, monkeypatch)
    (root / "1").mkdir()
    (root / "1" / "stat").write_text(_stat_line(1), encoding="utf-8")
    clock = Clock(budget=0)
    result = _wait_pid(clock, timeout=3600, poll=10)
    assert (result.outcome, result.matched, result.exit_code, result.how) == ("gone", False, 9, None)
    assert result.start_ticks is None and result.polls == 1 and result.waited_s == 0.0
    assert clock.sleeps == []


def test_a_pid_given_to_a_new_process_is_a_process_that_ended(tmp_path, monkeypatch):
    """The number is given to a new process (another start time) while the wait sleeps: the one
    waited for ended. Catches a process identified by its number alone."""
    root = _proc_root(tmp_path, monkeypatch)
    _put(root, PID, start=5000)
    clock = Clock(budget=8)
    clock.hooks[1] = lambda: _put(root, PID, start=9000, comm="newcomer")
    result = _wait_pid(clock, timeout=3600, poll=10)
    assert (result.outcome, result.matched, result.exit_code, result.how) == (
        "ended", True, 0, "replaced")
    assert result.start_ticks == 5000 and result.polls == 2


def test_a_process_that_has_ended_but_is_not_collected_is_ended(tmp_path, monkeypatch):
    """`Z` (one thread), `X` and `x` run nothing: ended with how `zombie`, also at the first
    look, when the process is positively identified (its start time is known)."""
    root = _proc_root(tmp_path, monkeypatch)
    for state in ("Z", "X", "x"):
        _put(root, PID, state="S", start=7000)
        clock = Clock(budget=4)
        clock.hooks[1] = lambda state=state: _put(root, PID, state=state, start=7000)
        result = _wait_pid(clock, timeout=3600, poll=10)
        assert (result.outcome, result.how, result.exit_code) == ("ended", "zombie", 0), state
        assert result.start_ticks == 7000 and result.polls == 2

        _put(root, PID, state=state, start=7000)
        clock = Clock(budget=0)
        result = _wait_pid(clock, timeout=3600, poll=10)
        assert (result.outcome, result.how, result.matched, result.exit_code) == (
            "ended", "zombie", True, 0), state
        assert result.polls == 1 and result.start_ticks == 7000 and clock.sleeps == []


def test_a_zombie_group_leader_whose_threads_still_run_is_alive(tmp_path, monkeypatch):
    """`Z` with a thread count above 1 is alive (the leader of a group that still runs): the
    wait goes on to its timeout, and ends as soon as the count is 1. Catches a zombie that is
    read from the state letter alone."""
    root = _proc_root(tmp_path, monkeypatch)
    _put(root, PID, state="Z", start=7000, threads=3)
    clock = Clock(budget=8)
    result = _wait_pid(clock, timeout=25, poll=10)
    assert (result.outcome, result.exit_code, result.matched, result.how) == (
        "timeout", 124, False, None)
    assert clock.sleeps == [10, 10, 5] and result.polls == 4 and result.start_ticks == 7000

    clock = Clock(budget=8)
    clock.hooks[2] = lambda: _put(root, PID, state="Z", start=7000, threads=1)
    result = _wait_pid(clock, timeout=3600, poll=10)
    assert (result.outcome, result.how, result.polls) == ("ended", "zombie", 3)


# --------------------------------------------------------------------------- #
# W5: what is read
# --------------------------------------------------------------------------- #


def test_the_start_time_is_read_after_the_last_bracket_of_the_name(tmp_path, monkeypatch):
    """The name (`comm`) holds spaces, brackets and a fake rest of a line: field 22 is read from
    after the last `)`. Catches a line split on spaces."""
    root = _proc_root(tmp_path, monkeypatch)
    names = ["a b", "x) S 1 (y", "(", ")", "w (1) z", "((()))", "S 1 2 3 4 5 6 7 8 9 10 11 12"]
    for name in names:
        _put(root, PID, comm=name, state="R", start=424242, threads=4)
        looked = runwait.read_pid(PID)
        assert looked == runwait.ProcLook(PID, 424242, "R", 4), name
        result = _wait_pid(Clock(budget=0), timeout=0)
        assert (result.outcome, result.start_ticks) == ("timeout", 424242), name


def test_the_process_wait_reads_the_stat_line_and_the_boot_id_and_nothing_else(
        tmp_path, monkeypatch):
    """A spy on `open`, `io.open`, `os.open`, `os.scandir` and `os.listdir` for the whole wait:
    only `<root>/<pid>/stat` and `<root>/sys/kernel/random/boot_id` are opened -- no command
    line, no name, no listing. (The purity scan of `tests/test_wait.py` is the second line.)"""
    import builtins

    root = _proc_root(tmp_path, monkeypatch)
    _put(root, PID, start=5000)
    for sibling in ("cmdline", "comm", "environ", "status"):
        (root / str(PID) / sibling).write_text("x", encoding="utf-8")
    from hoh import procs  # noqa: F401 - imported before the spy, so that the import is not seen

    touched: list[str] = []

    def spy(real):
        def wrapper(path, *args, **kwargs):
            try:
                touched.append(os.fspath(path))
            except TypeError:
                touched.append(repr(path))
            return real(path, *args, **kwargs)
        return wrapper

    clock = Clock(budget=4)
    clock.hooks[1] = lambda: _put(root, PID, state="Z", start=5000)
    with monkeypatch.context() as spied:
        spied.setattr(builtins, "open", spy(builtins.open))
        spied.setattr(io, "open", spy(io.open))
        spied.setattr(os, "open", spy(os.open))
        spied.setattr(os, "scandir", spy(os.scandir))
        spied.setattr(os, "listdir", spy(os.listdir))
        result = _wait_pid(clock, timeout=3600, poll=10)
    assert result.outcome == "ended" and result.how == "zombie"
    allowed = {str(root / str(PID) / "stat"), str(root / "sys" / "kernel" / "random" / "boot_id")}
    ours = [path for path in touched if str(root) in path or "/proc" in path]
    assert set(ours) == allowed, ours
    assert str(root / "sys" / "kernel" / "random" / "boot_id") in ours


def test_the_real_proc_shows_this_process_with_the_start_time_of_its_stat_line():
    """The real `/proc`: this process is read with the start time of its own stat line, and the
    parent is waited for once (timeout 0) with that parent's start time."""
    text = Path("/proc/self/stat").read_text(encoding="utf-8")
    fields = text[text.rindex(")") + 1:].split()
    looked = runwait.read_pid(os.getpid(), "/proc")
    assert looked is not None and looked.pid == os.getpid()
    assert looked.start == int(fields[19]) and looked.threads >= 1 and len(looked.state) == 1
    assert runwait.read_boot_id("/proc") == Path(
        "/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    parent = runwait.read_pid(os.getppid(), "/proc")
    assert parent is not None
    result = runwait.wait_pid(os.getppid(), timeout=0)
    assert result.outcome == "timeout" and result.start_ticks == parent.start
    assert result.boot_id == runwait.read_boot_id("/proc")
    assert runwait.read_pid(2 ** 31 - 2, "/proc") is None


# --------------------------------------------------------------------------- #
# W6: bounds, refusals, the answer of the process wait
# --------------------------------------------------------------------------- #


def test_the_process_wait_is_bounded_and_sends_no_signal(tmp_path, monkeypatch):
    """A live process: the default 3600 s at 10 s is 361 looks, `timeout=0` is one, 25 s at 10 s
    sleeps 10, 10, 5; a bad bound is a `ValueError` before any look; no signal is sent (not even
    signal 0)."""
    def forbidden(*args, **kwargs):
        pytest.fail("a signal was sent")

    for name in ("kill", "killpg", "pidfd_open"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, forbidden)
    monkeypatch.setattr(signal, "raise_signal", forbidden)
    root = _proc_root(tmp_path, monkeypatch)
    _put(root, PID, start=5000)

    clock = Clock(budget=400)
    result = _wait_pid(clock)
    assert clock.sleeps == [10.0] * 360 and result.polls == 361 and result.waited_s == 3600.0
    assert (result.outcome, result.exit_code, result.matched, result.how) == (
        "timeout", 124, False, None)
    assert result.start_ticks == 5000

    clock = Clock(budget=0)
    result = _wait_pid(clock, timeout=0)
    assert clock.sleeps == [] and result.polls == 1 and result.exit_code == 124

    clock = Clock(budget=8)
    result = _wait_pid(clock, timeout=25, poll=10)
    assert clock.sleeps == [10, 10, 5] and result.polls == 4

    clock = _use_clock(monkeypatch, Clock(budget=8))
    result = runwait.wait_pid(PID, timeout=25, poll=10)
    assert clock.sleeps == [10, 10, 5] and result.exit_code == 124

    before = _tree(root)
    for poll in (0, 0.5, -1, math.nan, True):
        with pytest.raises(ValueError):
            runwait.wait_pid(PID, timeout=5, poll=poll)
    for timeout in (-1, math.nan, math.inf, True):
        with pytest.raises(ValueError):
            runwait.wait_pid(PID, timeout=timeout, poll=10)
    assert _tree(root) == before


def test_what_cannot_be_identified_is_refused_and_the_waiter_never_waits_for_itself(
        tmp_path, monkeypatch):
    """Refused (`WaitRefused`, exit 2 on the command line): the waiter's own pid, 0, -1, True,
    a string, 2**31, a proc root without a boot id, a garbage stat line, a stat line with too
    few fields, and a permission error -- at the first look and at a later one: an unreadable
    process is never `ended`."""
    root = _proc_root(tmp_path, monkeypatch)
    _put(root, PID, start=5000)
    for bad in (os.getpid(), 0, -1, True, "4242", 2 ** 31, 4242.0, None):
        with pytest.raises(runwait.WaitRefused):
            runwait.wait_pid(bad, timeout=0)

    bare = tmp_path / "bare"
    (bare / str(PID)).mkdir(parents=True)
    (bare / str(PID) / "stat").write_text(_stat_line(PID), encoding="utf-8")
    monkeypatch.setattr(runwait, "PROC_ROOT", str(bare))
    with pytest.raises(runwait.WaitRefused):
        runwait.wait_pid(PID, timeout=0)
    (bare / "sys" / "kernel" / "random").mkdir(parents=True)
    (bare / "sys" / "kernel" / "random" / "boot_id").write_text("\n", encoding="utf-8")
    with pytest.raises(runwait.WaitRefused):                     # an empty boot id is none
        runwait.wait_pid(PID, timeout=0)
    monkeypatch.setattr(runwait, "PROC_ROOT", str(root))

    for text in ("garbage\n", "", "4242 (x) S 1 2\n", "4242 x S 1 2 3\n",
                 _stat_line(PID).replace(" S ", " SS ", 1),
                 _stat_line(PID).replace("123456", "soon", 1)):
        (root / str(PID) / "stat").write_text(text, encoding="utf-8")
        with pytest.raises(runwait.WaitRefused):
            runwait.wait_pid(PID, timeout=0)

    _put(root, PID, start=5000)
    real = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self.name == "stat" and self.parent.name == str(PID):
            raise PermissionError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(runwait.WaitRefused):
        runwait.wait_pid(PID, timeout=0)
    monkeypatch.setattr(Path, "read_text", real)

    clock = Clock(budget=4)
    clock.hooks[1] = lambda: monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(runwait.WaitRefused):
        _wait_pid(clock, timeout=3600, poll=10)


def test_the_pid_argument_is_a_plain_positive_decimal(capsys):
    """`pid_arg` accepts canonical decimals below 2**31 and nothing else: no sign, no leading
    zero, no space, no underscore, no hex, no float, no non-ASCII digit."""
    for good, value in (("1", 1), ("4242", 4242), ("10", 10), ("2147483647", 2 ** 31 - 1)):
        assert runwait.pid_arg(good) == value and isinstance(runwait.pid_arg(good), int)
    for bad in ("", "0", "-1", "+5", "007", " 5", "5 ", "1_0", "0x10", "1e3", "4.0", "５",
                "٣", "2147483648", "99999999999999999999", "five", "5\n"):
        with pytest.raises(argparse.ArgumentTypeError):
            runwait.pid_arg(bad)
    for bad in ("0", "-1", "007", "2147483648", "x"):
        got = _ask(capsys, "--pid", bad)
        assert got.code == 2 and got.out == "", bad


def test_the_process_answer_is_one_ascii_line_with_the_documented_fields(tmp_path, monkeypatch):
    """The fixed key order for `ended`, `gone` and `timeout`; the boot id is cut to 64."""
    long_boot = "x" * 100
    root = _proc_root(tmp_path, monkeypatch, boot=long_boot)
    _put(root, PID, start=5000)
    clock = Clock(budget=4)
    clock.hooks[1] = lambda: _vanish(root, PID)
    doc = _pid_doc(_wait_pid(clock, timeout=3600, poll=10))
    assert list(doc) == PID_KEYS
    assert doc == {"schema": "hoh-wait/1", "target": "pid", "pid": PID, "outcome": "ended",
                   "matched": True, "exit_code": 0, "how": "exited", "start_ticks": 5000,
                   "boot_id": "x" * 64, "waited_s": 10.0, "polls": 2}

    gone = _pid_doc(_wait_pid(Clock(budget=0), pid=PID, timeout=0))
    assert list(gone) == PID_KEYS
    assert (gone["outcome"], gone["matched"], gone["exit_code"], gone["how"],
            gone["start_ticks"], gone["polls"]) == ("gone", False, 9, None, None, 1)

    _put(root, 4343, start=77)
    alive = _pid_doc(_wait_pid(Clock(budget=0), pid=4343, timeout=0))
    assert (alive["outcome"], alive["exit_code"], alive["start_ticks"], alive["how"]) == (
        "timeout", 124, 77, None)


# --------------------------------------------------------------------------- #
# W7: the command
# --------------------------------------------------------------------------- #


def _a_run(root: Path, run_id: str) -> None:
    candidate = Candidate(candidate_id="cand-1", repo_path=str(root / "repo"), commit="c0ffee",
                          tree_clean=True, tree_digest="t")
    state = RunState(run_id=run_id, repo_path=str(root / "repo"), project_name="demo",
                     spec_path=str(root / "spec.md"), spec_digest="s", policy_digest="p",
                     profile_digest="h", stage=Stage.CHECKPOINTED, condition=Condition.ACTIVE,
                     iteration=1)
    state.last_accepted_candidate = candidate
    RunStore(root, run_id).write_state(state)


def test_the_exit_map_of_the_run_wait_is_unchanged_and_the_new_outcomes_have_their_codes(
        tmp_path, monkeypatch, capsys):
    """The first eleven entries of `EXIT` are the run wait's, in their order; the six new ones
    have their codes; and the command exits with them."""
    items = list(runwait.EXIT.items())
    assert dict(items[:11]) == RUN_EXIT_MAP and [k for k, _ in items[:11]] == list(RUN_EXIT_MAP)
    assert dict(items[11:]) == {"report_yes": 0, "report_no": 1, "report_partial": 8,
                                "report_invalid": 8, "ended": 0, "gone": 9}
    assert runwait.REFUSED == 2 and "refused" not in runwait.EXIT

    root = tmp_path / "runs"
    _a_run(root, "r1")
    got = _ask(capsys, "r1", root=root)
    assert got.code == 0 and got.doc["outcome"] == "accepted" and "target" not in got.doc

    reports = tmp_path / "reports"
    expected = [("yes", PROSE + _footer(), 0, "report_yes"),
                ("no", PROSE + _footer(VERDICT="RELEASE_NO"), 1, "report_no"),
                ("partial", PROSE + _footer(COMPLETENESS="PARTIAL"), 8, "report_partial"),
                ("invalid", PROSE + _footer() + "EXTRA: 1\n", 8, "report_invalid"),
                ("prose", PROSE, 124, "timeout")]
    for name, text, code, outcome in expected:
        report = _report(reports / f"{name}.md", text, age=None)
        old = NOW - 7200
        os.utime(report, (old, old))
        got = _ask(capsys, "--report", str(report), "--timeout", "0")
        assert got.code == code and got.doc["outcome"] == outcome, name
        assert got.doc["target"] == "report" and got.doc["exit_code"] == code

    proc = _proc_root(tmp_path, monkeypatch)
    _put(proc, PID, start=5000)
    got = _ask(capsys, "--pid", str(PID), "--timeout", "0")
    assert got.code == 124 and got.doc["outcome"] == "timeout" and got.doc["target"] == "pid"
    _put(proc, PID, state="Z", start=5000)
    got = _ask(capsys, "--pid", str(PID))
    assert got.code == 0 and got.doc["outcome"] == "ended" and got.doc["how"] == "zombie"
    got = _ask(capsys, "--pid", "4343")
    assert got.code == 9 and got.doc["outcome"] == "gone" and got.doc["matched"] is False


def test_the_command_takes_exactly_one_target_and_answers_for_each(tmp_path, monkeypatch, capsys):
    """None or two targets, `--until` with a report or a pid, an empty path and the waiter's own
    pid are exit 2 with one line on stderr and nothing on stdout; each of the three targets is
    answered by its own wait."""
    root = tmp_path / "runs"
    _a_run(root, "r1")
    report = _report(tmp_path / "r.md", PROSE + _footer())
    proc = _proc_root(tmp_path, monkeypatch)
    _put(proc, PID, state="Z", start=5000)

    refusals = [
        ([], "none"),
        (["r1", "--report", str(report)], "run and report"),
        (["r1", "--pid", str(PID)], "run and pid"),
        (["--report", str(report), "--pid", str(PID)], "report and pid"),
        (["r1", "--report", str(report), "--pid", str(PID)], "all three"),
        (["--report", str(report), "--until", "terminal"], "until with report"),
        (["--report", str(report), "--until", "settled"], "explicit settled with report"),
        (["--pid", str(PID), "--until", "terminal"], "until with pid"),
        (["--report", ""], "empty path"),
        (["--pid", str(os.getpid())], "own pid"),
    ]
    for argv, why in refusals:
        got = _ask(capsys, *argv, root=root)
        assert got.code == 2 and got.out == "", why
        assert got.err.strip() != "" and "Traceback" not in got.err, why
    # a refusal of the command is one line (argparse's own usage excepted)
    got = _ask(capsys, "--report", str(report), "--until", "terminal", root=root)
    assert len(got.err.strip().splitlines()) == 1 and got.err.startswith("wait refused:")

    run = _ask(capsys, "r1", root=root)
    assert run.code == 0 and run.doc["run_id"] == "r1" and "target" not in run.doc
    answered = _ask(capsys, "--report", str(report), root=root)
    assert answered.code == 0 and answered.doc["target"] == "report"
    assert answered.doc["report"] == os.path.realpath(report)
    process = _ask(capsys, "--pid", str(PID), root=root)
    assert process.code == 0 and process.doc["target"] == "pid" and process.doc["pid"] == PID


def test_a_relative_report_path_is_resolved_where_the_command_runs(tmp_path, monkeypatch, capsys):
    """`--report r.md` is the file of the current directory, resolved at the look: the same
    relative name in another directory is another report."""
    one, two = tmp_path / "one", tmp_path / "two"
    _report(one / "r.md", PROSE + _footer())
    _report(two / "r.md", PROSE + _footer(VERDICT="RELEASE_NO"))
    monkeypatch.chdir(one)
    got = _ask(capsys, "--report", "r.md", "--timeout", "0")
    assert got.code == 0 and got.doc["report"] == os.path.realpath(one / "r.md")
    monkeypatch.chdir(two)
    got = _ask(capsys, "--report", "r.md", "--timeout", "0")
    assert got.code == 1 and got.doc["report"] == os.path.realpath(two / "r.md")
    got = _ask(capsys, "--report", "./missing.md", "--timeout", "0")
    assert got.code == 124 and got.doc["status"] == "missing"


def test_the_parser_registers_the_targets_without_a_second_command():
    """One `wait` command, with an optional run, `--report` and `--pid` (typed by `pid_arg`);
    the defaults of the bounds are the run wait's; there is no `wait-report` command."""
    parser = cli.build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert [name for name in subparsers.choices if name.startswith("wait")] == ["wait"]
    wait = subparsers.choices["wait"]
    assert wait.get_default("func") is cli.cmd_wait
    options = {flag: a for a in wait._actions for flag in a.option_strings}
    assert options["--pid"].type is runwait.pid_arg
    assert options["--timeout"].type is runwait.timeout_arg
    assert options["--poll"].type is runwait.poll_arg
    assert options["--report"].default is None and options["--pid"].default is None
    positional = next(a for a in wait._actions if a.dest == "run_id")
    assert positional.nargs == "?" and not positional.option_strings

    args = parser.parse_args(["wait", "--report", "x.md"])
    assert (args.run_id, args.report, args.pid, args.until) == (None, "x.md", None, None)
    assert (args.timeout, args.poll) == (3600.0, 10.0)
    args = parser.parse_args(["wait", "--pid", "5", "--timeout", "7"])
    assert (args.run_id, args.report, args.pid, args.timeout) == (None, None, 5, 7.0)
    args = parser.parse_args(["wait", "r1"])
    assert (args.run_id, args.report, args.pid) == ("r1", None, None)
    for argv in (["wait", "--pid", "0"], ["wait", "--pid", "abc"], ["wait", "--pid"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_the_command_forwards_timeout_and_poll_to_the_report_and_pid_waits(
        tmp_path, monkeypatch, capsys):
    """Through `cli.main` with `runwait.time` replaced: `--timeout 25 --poll 10` ends a report
    that never appears and a live process as `timeout` / 124 after sleeps 10, 10, 5 and four
    looks; `--timeout 7 --poll 3` sleeps 3, 3, 1 (a swapped pair would not); a poll under 1 s
    and a negative timeout are exit 2 on both targets; the defaults are 361 looks.
    Catches a flag that is parsed and dropped."""
    proc = _proc_root(tmp_path, monkeypatch)
    _put(proc, PID, start=5000)
    never = str(tmp_path / "never.md")
    targets = {"report": ["--report", never], "pid": ["--pid", str(PID)]}
    for name, target in targets.items():
        clock = _use_clock(monkeypatch, Clock(budget=400))
        got = _ask(capsys, *target, "--timeout", "25", "--poll", "10")
        assert clock.sleeps == [10, 10, 5] and got.code == 124, name
        assert got.doc["polls"] == 4 and got.doc["waited_s"] == 25.0 and got.doc["target"] == name

        clock = _use_clock(monkeypatch, Clock(budget=400))
        got = _ask(capsys, *target, "--timeout", "7", "--poll", "3")
        assert clock.sleeps == [3, 3, 1] and got.code == 124, name

        clock = _use_clock(monkeypatch, Clock(budget=0))
        got = _ask(capsys, *target, "--timeout", "0")
        assert clock.sleeps == [] and got.code == 124 and got.doc["polls"] == 1, name

        clock = _use_clock(monkeypatch, Clock(budget=400))
        got = _ask(capsys, *target)
        assert clock.sleeps == [10.0] * 360 and got.code == 124 and got.doc["polls"] == 361, name

        for extra in (["--poll", "0"], ["--poll", "0.5"], ["--timeout", "-1"],
                      ["--poll", "nan"], ["--timeout", "inf"]):
            clock = _use_clock(monkeypatch, Clock(budget=0))
            got = _ask(capsys, *target, *extra)
            assert got.code == 2 and got.out == "" and clock.sleeps == [], (name, extra)
