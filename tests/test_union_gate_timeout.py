"""A union gate that runs out of time says so, the same way every time.

Spec: `dogfood/union-gate-timeout/spec.md` (draft v3 + Revision 3a). O250: a
U5 timeout used to write `U5: FAIL: could not run pytest: <str(TimeoutExpired)>`
-- a text that repeats the command line, which starts with `sys.executable`,
a home path on most machines. The board copied it into the claims ledger, and
the next pass moved three more rows.

No test here waits for a real suite timeout or depends on machine load. The
timeout and start-failure paths are reached through the `_run_bounded` seam
(or `subprocess.run` for U2 and the wheel build). The few tests that start
real processes use tiny commands of their own, adapt their budget until they
have observed what they need, and in `finally` signal only processes they
started and can still prove are theirs.

Home-shaped paths are built from parts (`chr(47)`): the repository's
home-path guards scan this file.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_claims as cc  # noqa: E402
import union_gate as ug  # noqa: E402


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rd = _load("readiness_ugt_probe", "tools/readiness.py")
rg = _load("regenerate_ugt_probe", "tools/regenerate.py")

SL = chr(47)
#: An interpreter path shaped like the one on the machine where O250 happened.
HOME_PYTHON = SL.join(["", "home", "someone", "miniconda3", "bin", "python3"])


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _timeouts(argv0: str = HOME_PYTHON) -> list[subprocess.TimeoutExpired]:
    """Two timeouts that differ in everything the old reason interpolated:
    the command line, the timeout and the captured output."""
    return [
        subprocess.TimeoutExpired([argv0, "-m", "pytest", "-q"], 600, output="x"),
        subprocess.TimeoutExpired([argv0 + "3.12", "-m", "pytest"], 7.25, output=None),
    ]


def _start_failures() -> list[OSError]:
    return [
        FileNotFoundError(2, "No such file or directory", HOME_PYTHON),
        FileNotFoundError(2, "No such file or directory", SL.join(["", "usr", "bin", "x"])),
    ]


def _assert_machine_independent(text: str, budget: int | None = None) -> None:
    assert cc.find_home_paths(text) == [], text
    assert "someone" not in text and "miniconda" not in text, text
    rest = text.replace(f"budget {budget} s", "") if budget is not None else text
    # U<n> is the only other digit a line may carry.
    rest = rest.split(": ", 1)[1] if rest[:1] == "U" else rest
    assert not any(ch.isdigit() for ch in rest), text


def _raising(exc):
    def fake(*a, **k):
        raise exc
    return fake


def _u5_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    return repo


def _u4_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "runs").mkdir(parents=True)
    (repo / "tools").mkdir()
    (repo / "tools" / "check_claims.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (repo / "CLAIMS.json").write_text("{}\n", encoding="utf-8")
    return repo


def _wheel_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "fx").mkdir(parents=True)
    (repo / "src" / "fx" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "fx"\nversion = "0.0.1"\n', encoding="utf-8")
    return repo


def _ok(argv, *a, **k):
    return subprocess.CompletedProcess(argv, 0, "", "")


# --------------------------------------------------------------------------- #
# K1 -- one machine-independent reason
# --------------------------------------------------------------------------- #


def test_a_suite_timeout_is_not_run_with_one_machine_independent_reason(tmp_path, monkeypatch):
    repo = _u5_repo(tmp_path)
    lines = []
    for exc in _timeouts():
        monkeypatch.setattr(ug, "_run_bounded", _raising(exc))
        result = ug.check_u5(repo, tmp_path)
        assert result.status == ug.STATUS_NOT_RUN
        assert result.status != ug.STATUS_PASS
        assert result.detail == "pytest: TIMEOUT (budget 1800 s)"
        lines.append(ug._format_line(5, result))
    assert lines[0] == lines[1] == "U5: NOT_RUN: pytest: TIMEOUT (budget 1800 s)"
    _assert_machine_independent(lines[0], ug.SUITE_TIMEOUT_S)


def test_a_lint_timeout_is_not_run_too(tmp_path, monkeypatch):
    repo = _u5_repo(tmp_path)
    lines = []
    for exc in _timeouts("ruff"):
        def fake(argv, *a, _exc=exc, **k):
            if argv[0] == "ruff":
                raise _exc
            return _ok(argv)
        monkeypatch.setattr(ug, "_run_bounded", fake)
        result = ug.check_u5(repo, tmp_path)
        assert result.status == ug.STATUS_NOT_RUN
        assert result.detail == "ruff: TIMEOUT (budget 600 s)"
        lines.append(ug._format_line(5, result))
    assert lines[0] == lines[1]
    _assert_machine_independent(lines[0], ug.LINT_TIMEOUT_S)


def test_a_claims_check_timeout_is_not_run_too(tmp_path, monkeypatch):
    repo = _u4_repo(tmp_path)
    lines = []
    for exc in _timeouts():
        monkeypatch.setattr(ug, "_run_bounded", _raising(exc))
        result = ug.check_u4(repo)
        assert result.status == ug.STATUS_NOT_RUN
        assert result.detail == "check_claims.py check all: TIMEOUT (budget 600 s)"
        lines.append(ug._format_line(4, result))
    assert lines[0] == lines[1]
    _assert_machine_independent(lines[0], ug.CLAIMS_TIMEOUT_S)


def test_a_command_that_cannot_start_is_not_run_without_its_path(tmp_path, monkeypatch):
    repo = _u5_repo(tmp_path)
    lines = []
    for exc in _start_failures():
        monkeypatch.setattr(ug, "_run_bounded", _raising(exc))
        result = ug.check_u5(repo, tmp_path)
        assert result.status == ug.STATUS_NOT_RUN
        assert result.detail == "pytest: could not be started (FileNotFoundError)"
        lines.append(ug._format_line(5, result))
    assert lines[0] == lines[1]
    _assert_machine_independent(lines[0])


@pytest.mark.parametrize("where", ["u4", "ruff"])
def test_a_start_failure_at_u4_or_ruff_is_not_run_without_its_path(where, tmp_path, monkeypatch):
    lines = []
    for exc in [PermissionError(13, "Permission denied", HOME_PYTHON), *_start_failures()]:
        if where == "u4":
            repo = _u4_repo(tmp_path / f"r{len(lines)}")
            monkeypatch.setattr(ug, "_run_bounded", _raising(exc))
            result = ug.check_u4(repo)
            what, n = "check_claims.py check all", 4
        else:
            repo = _u5_repo(tmp_path / f"r{len(lines)}")

            def fake(argv, *a, _exc=exc, **k):
                if argv[0] == "ruff":
                    raise _exc
                return _ok(argv)
            monkeypatch.setattr(ug, "_run_bounded", fake)
            result = ug.check_u5(repo, tmp_path)
            what, n = "ruff", 5
        assert result.status == ug.STATUS_NOT_RUN
        assert result.detail == f"{what}: could not be started ({type(exc).__name__})"
        line = ug._format_line(n, result)
        _assert_machine_independent(line)
        lines.append(line)
    assert lines[1] == lines[2]


def test_a_git_timeout_skips_with_a_machine_independent_reason(tmp_path, monkeypatch):
    lines = []
    for exc in [subprocess.TimeoutExpired(["git", "-C", HOME_PYTHON, "ls-files"], 60),
                subprocess.TimeoutExpired(["git", "-C", str(tmp_path), "ls-files"], 7)]:
        monkeypatch.setattr(ug.subprocess, "run", _raising(exc))
        result = ug.check_u2(tmp_path)
        assert result.status == ug.STATUS_SKIPPED
        lines.append(ug._format_line(2, result))
    assert lines[0] == lines[1] == "U2: SKIPPED: git ls-files: TIMEOUT (budget 60 s)"
    _assert_machine_independent(lines[0], ug.GIT_TIMEOUT_S)

    monkeypatch.setattr(ug.subprocess, "run", _raising(_start_failures()[0]))
    line = ug._format_line(2, ug.check_u2(tmp_path))
    assert line == "U2: SKIPPED: git ls-files: could not be started (FileNotFoundError)"


def test_a_wheel_build_timeout_has_a_machine_independent_reason(tmp_path, monkeypatch):
    repo = _wheel_repo(tmp_path)
    lines = []
    for i, exc in enumerate(_timeouts()):
        monkeypatch.setattr(ug.subprocess, "run", _raising(exc))
        work = tmp_path / f"work{i}"
        work.mkdir()
        info = ug.build_wheel(repo, work)
        assert info.built is False
        assert info.reason == "pip wheel: TIMEOUT (budget 300 s)"
        result = ug.check_u3(repo, info)
        assert result.status == ug.STATUS_SKIPPED
        lines.append(ug._format_line(3, result))
    assert lines[0] == lines[1] == "U3: SKIPPED: could not build a wheel: pip wheel: TIMEOUT (budget 300 s)"
    _assert_machine_independent(lines[0], ug.WHEEL_TIMEOUT_S)


# --------------------------------------------------------------------------- #
# K2 -- NOT_RUN turns the exit red
# --------------------------------------------------------------------------- #


def test_not_run_is_never_a_pass_and_turns_the_exit_red():
    nr = ug.Result.not_run("pytest: TIMEOUT (budget 1800 s)")
    assert nr.status == "NOT_RUN" == ug.STATUS_NOT_RUN
    assert nr.status not in (ug.STATUS_PASS, ug.STATUS_SKIPPED, ug.STATUS_FAIL)
    assert ug._format_line(5, nr) == "U5: NOT_RUN: pytest: TIMEOUT (budget 1800 s)"
    assert len(ug.Result.not_run("x" * 5000).detail) <= ug.REASON_BUDGET

    ok = ug.Result.ok()
    skipped = ug.Result.skipped("no runs/")
    assert ug._exit_for([ok, ok, ok, ok, ok], None) == 0
    assert ug._exit_for([ok, skipped, skipped, skipped, ok], None) == 0
    assert ug._exit_for([ok, ok, ok, ok, nr], None) == 1
    assert ug._exit_for([nr, ok, ok, ok, ok], frozenset()) == 1
    assert ug._exit_for([ok, ok, ok, ug.Result.fail("x"), ok], None) == 1
    # --require is unchanged: a required SKIPPED still fails, an unrequired one does not.
    assert ug._exit_for([ok, ok, skipped, ok, ok], frozenset({3})) == 1
    assert ug._exit_for([ok, ok, skipped, ok, ok], frozenset({4})) == 0


def test_the_gate_prints_not_run_and_exits_red_on_a_suite_timeout(tmp_path, monkeypatch, capsys):
    repo = _u5_repo(tmp_path)
    monkeypatch.setattr(ug, "_run_bounded", _raising(_timeouts()[0]))
    rc = ug.main([str(repo)])
    out = capsys.readouterr().out.splitlines()
    assert "U5: NOT_RUN: pytest: TIMEOUT (budget 1800 s)" in out
    # Nothing else in this fixture fails: the red exit is the NOT_RUN's.
    assert not [z for z in out if z.startswith("U") and ": FAIL" in z], out
    assert rc == 1
    _assert_machine_independent("\n".join(out[-1:]), ug.SUITE_TIMEOUT_S)


# --------------------------------------------------------------------------- #
# K3 and Revision 3a -- the whole group dies, and only what is ours is signalled
# --------------------------------------------------------------------------- #


#: Taken once, so a test that records in a loop never subclasses its own recorder.
_REAL_POPEN = subprocess.Popen


class _Recording:
    """Wraps subprocess.Popen so a test holds the very object _run_bounded made."""

    def __init__(self, monkeypatch, after_start=None):
        self.made: list[subprocess.Popen] = []
        real = _REAL_POPEN
        rec = self

        class Popen(real):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                rec.made.append(self)
                if after_start is not None:
                    after_start()

        monkeypatch.setattr(ug.subprocess, "Popen", Popen)


def _leader_script(marker: str, pid_file: Path, *, ignore_term: bool = False) -> str:
    """A leader that starts one plain Popen grandchild, records who is who, and
    sleeps. `marker` is on the grandchild's command line, so cleanup can prove
    a pid is still the process this test started."""
    return "\n".join([
        "import os, signal, subprocess, sys, time",
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)" if ignore_term else "",
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)', "
        f"{marker!r}])",
        f"tmp = {str(pid_file) + '.part'!r}",
        "open(tmp, 'w').write(' '.join(map(str, [g.pid, os.getpid(), os.getpgid(0), "
        "os.getsid(0), os.getpgid(g.pid)])))",
        f"os.replace(tmp, {str(pid_file)!r})",
        "time.sleep(600)",
    ])


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return ""


def _alive(pid: int) -> bool:
    """True while `pid` exists and is not a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def _gone_soon(pid: int, marker: str, within_s: float = 10.0) -> bool:
    """SIGKILL is asynchronous; give the kernel a moment. A pid that no longer
    carries our marker has been reaped (and possibly reused): gone for us."""
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if marker not in _cmdline(pid) or not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _cleanup(pid: int | None, marker: str, procs: list[subprocess.Popen]) -> None:
    """Signals only what is provably ours: a grandchild whose command line still
    carries this test's marker, and a leader whose Popen has not reaped it.
    The marker is a fresh uuid, so a process carrying it can only be one this
    test started -- which also catches a grandchild whose pid was never
    written down because a mutant let the leader run on."""
    pids = {pid} if pid is not None else set()
    if marker:
        pids |= {int(d) for d in os.listdir("/proc") if d.isdigit() and marker in _cmdline(int(d))}
    for p_ in sorted(pids):
        if marker in _cmdline(p_) and _alive(p_):
            try:
                os.kill(p_, signal.SIGKILL)
            except ProcessLookupError:
                pass
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait()
        for s in (p.stdout, p.stderr):
            if s is not None and not s.closed:
                s.close()


class _Hung(Exception):
    pass


def _watchdog(seconds: float):
    """Turns a mutant that hangs (a SIGTERM the group ignores, then wait())
    into a failure instead of a stalled suite."""
    def fire(signum, frame):
        raise _Hung(f"no result within {seconds} s")
    old = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    return old


def _disarm(old) -> None:
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, old)


def _timeout_with_grandchild(tmp_path, monkeypatch, *, ignore_term: bool = False):
    """Runs the leader under a doubling budget until a grandchild was observed
    before the budget expired. Returns (ids, leader Popen, marker)."""
    for budget in (2, 8, 32, 128):
        marker = f"ugt-grandchild-{uuid.uuid4().hex}"
        pid_file = tmp_path / f"ids-{budget}"
        rec = _Recording(monkeypatch)
        old = _watchdog(budget + 60)
        ids = None
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                ug._run_bounded(
                    [sys.executable, "-c", _leader_script(marker, pid_file, ignore_term=ignore_term)],
                    cwd=str(tmp_path), timeout=budget)
            if pid_file.is_file():
                ids = [int(x) for x in pid_file.read_text().split()]
                return ids, rec.made[0], marker
        finally:
            _disarm(old)
            if ids is None:
                gpid = None
                if pid_file.is_file():
                    gpid = int(pid_file.read_text().split()[0])
                _cleanup(gpid, marker, rec.made)
    pytest.fail("no grandchild was observed even with a 128-s budget")


def _check_group_died(ids, leader, marker, *, sig=signal.SIGKILL):
    gpid, lpid, lpgid, lsid, gpgid = ids
    try:
        assert lpid == leader.pid
        # The command leads its own session and process group, and the
        # grandchild is in that group.
        assert lpgid == lpid and lsid == lpid, ids
        assert gpgid == lpid, ids
        # The leader was reaped, by the signal asked for.
        assert leader.returncode == -sig, leader.returncode
        assert _gone_soon(gpid, marker), "the grandchild outlived the timeout"
    finally:
        _cleanup(gpid, marker, [leader])


def test_a_timeout_kills_the_commands_children_too(tmp_path, monkeypatch):
    ids, leader, marker = _timeout_with_grandchild(tmp_path, monkeypatch)
    _check_group_died(ids, leader, marker)


def test_a_group_that_ignores_sigterm_is_killed_too(tmp_path, monkeypatch):
    """Pins SIGKILL: the leader and its grandchild both ignore SIGTERM."""
    ids, leader, marker = _timeout_with_grandchild(tmp_path, monkeypatch, ignore_term=True)
    _check_group_died(ids, leader, marker)


def test_a_timeout_reaps_the_leader_and_closes_its_pipes(tmp_path, monkeypatch):
    rec = _Recording(monkeypatch)
    old = _watchdog(60)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            ug._run_bounded([sys.executable, "-c", "import time; time.sleep(60)"],
                            cwd=str(tmp_path), timeout=0.5)
        assert len(rec.made) == 1
        p = rec.made[0]
        assert p.returncode is not None, "the leader was not reaped"
        assert p.returncode == -signal.SIGKILL
        assert p.stdout.closed and p.stderr.closed
    finally:
        _disarm(old)
        _cleanup(None, "", rec.made)


def test_an_interrupt_kills_the_group_too(tmp_path, monkeypatch):
    """An interrupted gate must not leave the suite running in its own session.
    The interrupt is armed only once Popen has returned, so it always lands
    inside communicate; its delay doubles until a grandchild was observed."""
    fired = []

    def interrupt(signum, frame):
        # The first alarm is the interrupt; it re-arms the timer as a
        # watchdog, so a mutant whose cleanup hangs in wait() fails instead.
        fired.append(signum)
        if len(fired) == 1:
            signal.setitimer(signal.ITIMER_REAL, 60)
            raise KeyboardInterrupt
        raise _Hung("no result within 60 s of the interrupt")

    for delay in (1, 4, 16, 64):
        marker = f"ugt-grandchild-{uuid.uuid4().hex}"
        pid_file = tmp_path / f"ids-{delay}"
        fired.clear()
        old = signal.signal(signal.SIGALRM, interrupt)
        rec = _Recording(monkeypatch,
                         after_start=lambda d=delay: signal.setitimer(signal.ITIMER_REAL, d))
        ids = None
        try:
            with pytest.raises(KeyboardInterrupt):
                ug._run_bounded([sys.executable, "-c", _leader_script(marker, pid_file)],
                                cwd=str(tmp_path), timeout=600)
            signal.setitimer(signal.ITIMER_REAL, 0)
            if pid_file.is_file():
                ids = [int(x) for x in pid_file.read_text().split()]
                leader = rec.made[0]
                assert leader.stdout.closed and leader.stderr.closed
                _check_group_died(ids, leader, marker)
                return
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
            # Always, whichever assertion failed: _cleanup signals only what
            # is still provably ours, so a second call is harmless.
            gpid = ids[0] if ids else (
                int(pid_file.read_text().split()[0]) if pid_file.is_file() else None)
            _cleanup(gpid, marker, rec.made)
    pytest.fail("no grandchild was observed even with a 64-s interrupt delay")


def test_a_reaped_leader_receives_no_signal(tmp_path, monkeypatch):
    p = subprocess.Popen([sys.executable, "-c", "pass"], cwd=str(tmp_path),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         start_new_session=True)
    p.communicate(timeout=120)
    assert p.returncode is not None
    sent = []
    monkeypatch.setattr(ug.os, "killpg", lambda *a: sent.append(("killpg", a)))
    monkeypatch.setattr(ug.os, "kill", lambda *a: sent.append(("kill", a)))
    monkeypatch.setattr(subprocess.Popen, "send_signal", lambda self, s: sent.append(("send", s)))
    ug._kill_group(p)
    assert sent == [], "a reaped leader's pid may already belong to someone else"
    assert p.stdout.closed and p.stderr.closed


def test_check_u4_u5_and_ruff_go_through_the_one_seam(tmp_path, monkeypatch):
    """U4, U5's pytest and U5's ruff all go through _run_bounded; U2 and the
    wheel build keep subprocess.run."""
    seen = []
    monkeypatch.setattr(ug, "_run_bounded", lambda argv, **k: seen.append(argv[0]) or _ok(argv))
    monkeypatch.setattr(ug.subprocess, "run", _raising(AssertionError("subprocess.run used")))
    assert ug.check_u4(_u4_repo(tmp_path / "a")).status == ug.STATUS_PASS
    assert ug.check_u5(_u5_repo(tmp_path / "b"), tmp_path).status == ug.STATUS_PASS
    assert seen == [sys.executable, sys.executable, "ruff"]


# --------------------------------------------------------------------------- #
# K4 -- the suite runs on its own fresh basetemp
# --------------------------------------------------------------------------- #


def _basetemp_of(argv) -> str:
    assert "--basetemp" in argv, argv
    return argv[argv.index("--basetemp") + 1]


def test_the_suite_runs_on_its_own_fresh_basetemp(tmp_path, monkeypatch):
    repo = _u5_repo(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    seen = []

    def fake(argv, **k):
        if argv[0] != "ruff":
            bt = _basetemp_of(argv)
            seen.append((bt, os.path.exists(bt)))
        return _ok(argv)

    monkeypatch.setattr(ug, "_run_bounded", fake)
    assert ug.check_u5(repo, work).status == ug.STATUS_PASS
    assert seen == [(str(work / "pytest-basetemp"), False)]


def test_check_u5_without_a_work_dir_uses_its_own_temporary_basetemp(tmp_path, monkeypatch):
    repo = _u5_repo(tmp_path)
    seen = []

    def fake(argv, **k):
        if argv[0] != "ruff":
            bt = Path(_basetemp_of(argv))
            seen.append((bt, bt.exists(), bt.parent.is_dir()))
        return _ok(argv)

    monkeypatch.setattr(ug, "_run_bounded", fake)
    assert ug.check_u5(repo).status == ug.STATUS_PASS
    assert len(seen) == 1
    bt, existed, parent_was_dir = seen[0]
    assert bt.name == "pytest-basetemp"
    assert not existed and parent_was_dir
    real_bt = Path(os.path.realpath(bt.parent))
    assert not real_bt.is_relative_to(Path(os.path.realpath(repo))), bt
    assert real_bt.is_relative_to(Path(os.path.realpath(tempfile.gettempdir()))), bt
    assert not bt.parent.exists(), "the temporary directory outlived the call"


def test_run_gate_hands_its_work_dir_to_u5(tmp_path, monkeypatch):
    got = {}

    def fake_wheel(repo_path, work_dir):
        got["wheel"] = work_dir
        return ug.WheelInfo(attempted=False, built=False, reason="stub")

    def fake_u5(repo_path, work_dir=None):
        got["u5"] = work_dir
        return ug.Result.ok()

    monkeypatch.setattr(ug, "build_wheel", fake_wheel)
    for name in ("check_u1", "check_u3"):
        monkeypatch.setattr(ug, name, lambda *a, **k: ug.Result.ok())
    for name in ("check_u2", "check_u4"):
        monkeypatch.setattr(ug, name, lambda *a, **k: ug.Result.ok())
    monkeypatch.setattr(ug, "check_u5", fake_u5)
    results, rc = ug.run_gate(tmp_path)
    assert rc == 0
    assert got["u5"] is not None
    assert got["u5"] == got["wheel"]


# --------------------------------------------------------------------------- #
# K5 and Revision 3a -- the union row
# --------------------------------------------------------------------------- #


def _row(monkeypatch, rc, lines):
    calls = []

    def fake_py(*args, **kw):
        calls.append((args, kw))
        return rc, "\n".join(lines)

    monkeypatch.setattr(rd, "_py", fake_py)
    row = rd.row_union()
    assert calls and calls[0][0] == ("tools/union_gate.py",)
    return row


def test_row_union_reports_a_not_run_gate_as_not_run(monkeypatch):
    row = _row(monkeypatch, 1, ["U1: PASS", "U2: PASS", "U3: SKIPPED: no pyproject.toml",
                                "U4: PASS", "U5: NOT_RUN: pytest: TIMEOUT (budget 1800 s)"])
    assert row.state == rd.NOT_RUN == "NOT_RUN"
    assert row.value_ == "U5: NOT_RUN: pytest: TIMEOUT (budget 1800 s)"
    # A NOT_RUN line next to a clean exit is still NOT_RUN, never PASS.
    row = _row(monkeypatch, 0, ["U1: PASS", "U5: NOT_RUN: ruff: TIMEOUT (budget 600 s)"])
    assert row.state == rd.NOT_RUN


def test_row_union_keeps_a_real_failure_red_next_to_a_not_run(monkeypatch):
    fail = "U2: FAIL: 3 dangling reference(s): docs/a.md -> b.md; docs/c.md -> d.md; e -> f"
    nr = "U5: NOT_RUN: pytest: TIMEOUT (budget 1800 s)"
    row = _row(monkeypatch, 1, ["U1: PASS", fail, "U3: PASS", "U4: PASS", nr])
    assert row.state == rd.FAIL
    assert row.value_ == f"{fail[:60]}; {nr[:60]}"
    # FAIL lines come first, whatever order the gate printed them in.
    row = _row(monkeypatch, 1, [nr, fail])
    assert row.value_ == f"{fail[:60]}; {nr[:60]}"


def test_row_union_never_says_pass_for_a_red_exit_without_lines(monkeypatch):
    row = _row(monkeypatch, 1, ["Traceback (most recent call last):", "  boom"])
    assert row.state == rd.FAIL
    assert row.value_ == "union_gate.py exited 1 with no FAIL or NOT_RUN line"
    row = _row(monkeypatch, 2, [])
    assert row.value_ == "union_gate.py exited 2 with no FAIL or NOT_RUN line"
    row = _row(monkeypatch, 0, ["U1: PASS", "U2: PASS", "U3: PASS", "U4: PASS", "U5: PASS"])
    assert (row.state, row.value_) == (rd.PASS, "U1-U5 pass")


def test_row_union_reads_fail_only_from_an_invariants_status(monkeypatch):
    lines = ["U1: PASS", "U2: PASS",
             "U3: SKIPPED: could not build a wheel: pip wheel exited 1: 1 FAILED",
             "U4: SKIPPED: tool said : FAIL here",
             "U5: SKIPPED: quoted U4: FAIL: mid-line"]
    row = _row(monkeypatch, 0, lines)
    assert (row.state, row.value_) == (rd.PASS, "U1-U5 pass")
    row = _row(monkeypatch, 1, ["U4: FAIL: check_claims.py check all exited 1: 'x'"])
    assert row.state == rd.FAIL


def test_row_union_reads_not_run_only_from_an_invariants_status(monkeypatch):
    row = _row(monkeypatch, 0, ["U1: PASS", "U2: PASS", "U3: SKIPPED: tool said NOT_RUN here",
                                "U4: PASS", "U5: PASS"])
    assert (row.state, row.value_) == (rd.PASS, "U1-U5 pass")


def _timing_out(seen):
    def fake_py(*args, **kw):
        seen.append(kw.get("timeout"))
        raise subprocess.TimeoutExpired([HOME_PYTHON, *args], kw.get("timeout"))
    return fake_py


def test_row_union_turns_its_own_timeout_into_a_stable_not_run(monkeypatch):
    seen = []
    monkeypatch.setattr(rd, "_py", _timing_out(seen))
    first = rd.row_union()
    second = rd.row_union()
    assert seen == [rd.UNION_GATE_TIMEOUT_S, rd.UNION_GATE_TIMEOUT_S]
    assert first.state == rd.NOT_RUN
    assert first.value_ == "union_gate.py: TIMEOUT (budget 3600 s)"
    assert (first.name, first.state, first.value_, first.command) == \
        (second.name, second.state, second.value_, second.command)
    _assert_machine_independent(first.value_, rd.UNION_GATE_TIMEOUT_S)


def test_row_union_adds_no_line_of_its_own_to_the_board(monkeypatch):
    rows = []
    for rc, lines in [(0, ["U1: PASS", "U5: PASS"]),
                      (1, ["U5: FAIL: pytest: 1 failed, 2 passed"]),
                      (1, ["U5: NOT_RUN: pytest: TIMEOUT (budget 1800 s)"]),
                      (1, [])]:
        rows.append(_row(monkeypatch, rc, lines))
    monkeypatch.setattr(rd, "_py", _timing_out([]))
    rows.append(rd.row_union())
    for row in rows:
        assert row.name == "union_invariants"
        assert row.why_text == "", row
        assert row.advisory is False, row
        board = rd.markdown([row], "0000000")
        assert not [z for z in board.splitlines() if z.startswith("* **`union_invariants`")]


# --------------------------------------------------------------------------- #
# K6 -- named budgets
# --------------------------------------------------------------------------- #


def test_the_budgets_are_the_specified_values():
    assert ug.WHEEL_TIMEOUT_S == 300
    assert ug.GIT_TIMEOUT_S == 60
    assert ug.CLAIMS_TIMEOUT_S == 600
    assert ug.SUITE_TIMEOUT_S == 1800
    assert ug.LINT_TIMEOUT_S == 600
    assert rd.UNION_GATE_TIMEOUT_S == 3600


def test_the_gate_fits_inside_the_boards_budget_for_it(monkeypatch):
    inner = (ug.WHEEL_TIMEOUT_S + ug.GIT_TIMEOUT_S + ug.CLAIMS_TIMEOUT_S
             + ug.SUITE_TIMEOUT_S + ug.LINT_TIMEOUT_S)
    assert inner == 3360
    assert rd.UNION_GATE_TIMEOUT_S > inner
    seen = []
    monkeypatch.setattr(rd, "_py", lambda *a, **kw: seen.append(kw.get("timeout")) or (0, "U1: PASS"))
    rd.row_union()
    assert seen == [rd.UNION_GATE_TIMEOUT_S]


def test_the_union_suite_budget_is_the_tests_rows_budget():
    tests_row_budget = inspect.signature(rd._run).parameters["timeout"].default
    assert tests_row_budget == 1800
    assert ug.SUITE_TIMEOUT_S >= tests_row_budget


def test_each_command_runs_under_its_named_budget(tmp_path, monkeypatch):
    bounded = {}

    def fake_bounded(argv, **k):
        key = "ruff" if argv[0] == "ruff" else ("pytest" if "pytest" in argv else "claims")
        bounded[key] = k.get("timeout")
        return _ok(argv)

    monkeypatch.setattr(ug, "_run_bounded", fake_bounded)
    ug.check_u4(_u4_repo(tmp_path / "a"))
    ug.check_u5(_u5_repo(tmp_path / "b"), tmp_path)
    assert bounded == {"claims": ug.CLAIMS_TIMEOUT_S, "pytest": ug.SUITE_TIMEOUT_S,
                       "ruff": ug.LINT_TIMEOUT_S}

    plain = {}

    def fake_run(argv, *a, **k):
        plain["git" if argv[0] == "git" else "wheel"] = k.get("timeout")
        raise subprocess.TimeoutExpired(argv, k.get("timeout"))

    monkeypatch.setattr(ug.subprocess, "run", fake_run)
    ug.check_u2(tmp_path)
    work = tmp_path / "w"
    work.mkdir()
    ug.build_wheel(_wheel_repo(tmp_path / "c"), work)
    assert plain == {"git": ug.GIT_TIMEOUT_S, "wheel": ug.WHEEL_TIMEOUT_S}


# --------------------------------------------------------------------------- #
# K7 -- regenerate records which rows moved
# --------------------------------------------------------------------------- #


def _board(rows: dict[str, str], why: dict[str, str] | None = None,
           head: str = "# Readiness", measured: str = "aaa") -> str:
    z = [head, "", f"Measured at `{measured}` on 2026-10-04.", "",
         "| condition | state | measured | command |", "|---|---|---|---|"]
    z += [f"| `{n}` | {v} | x | `cmd` |" for n, v in rows.items()]
    z += ["", "## Why some rows are advisory", ""]
    z += [f"* **`{n}`** — {t}." for n, t in (why or {}).items()]
    return "\n".join(z) + "\n"


def _passes(monkeypatch, tmp_path, boards, fail_at=None):
    """Points the loop at a scratch board; pass i writes boards[i]."""
    board = tmp_path / "READINESS.md"
    board.write_text(boards[0], encoding="utf-8")
    monkeypatch.setattr(rg, "BOARD", board)
    state = {"i": 0}

    def once(env):
        state["i"] += 1
        if fail_at is not None and state["i"] == fail_at:
            return ["capability matrix: exit 2"]
        board.write_text(boards[min(state["i"], len(boards) - 1)], encoding="utf-8")
        return []

    monkeypatch.setattr(rg, "run_once", once)
    return state


CLEAN = {"tests": "PASS", "claims": "PASS", "union_invariants": "PASS"}
CASCADE = {"tests": "FAIL", "claims": "FAIL", "union_invariants": "FAIL"}


def test_a_cycle_names_the_rows_that_moved(tmp_path, monkeypatch, capsys):
    a = _board(CLEAN)
    b = _board({**CLEAN, "union_invariants": "NOT_RUN"},
               why={"union_invariants": "timed out"})
    state = _passes(monkeypatch, tmp_path, [a, b, a, b, a])
    result = rg.regenerate(max_passes=4)
    assert result["converged"] is False
    moved = ["union_invariants", "why:union_invariants"]
    assert result["changes"] == [moved, moved, moved, moved]

    state["i"] = 0
    (tmp_path / "READINESS.md").write_text(a, encoding="utf-8")
    assert rg.main(["--max-passes", "2"]) == 1
    out = capsys.readouterr().out
    assert "  pass 1: rows changed: union_invariants, why:union_invariants" in out
    assert "  pass 2: rows changed: union_invariants, why:union_invariants" in out

    # Several rows moving at once are named, sorted.
    _passes(monkeypatch, tmp_path, [_board(CLEAN), _board(CASCADE), _board(CASCADE)])
    result = rg.regenerate(max_passes=3)
    assert result["changes"] == [["claims", "tests", "union_invariants"], []]


def test_a_converged_pass_records_that_nothing_moved(tmp_path, monkeypatch, capsys):
    x = _board(CLEAN)
    y = _board({**CLEAN, "tests": "FAIL"})
    _passes(monkeypatch, tmp_path, [x, y, _board({**CLEAN, "tests": "FAIL"}, measured="bbb")])
    result = rg.regenerate(max_passes=5)
    assert result["converged"] is True
    assert result["changes"] == [["tests"], []]

    _passes(monkeypatch, tmp_path, [x, x])
    assert rg.main(["--max-passes", "3"]) == 0
    out = capsys.readouterr().out
    assert "  pass 1: rows changed: none" in out


def test_a_failed_step_still_reports_the_changes(tmp_path, monkeypatch):
    x = _board(CLEAN)
    y = _board({**CLEAN, "claims": "FAIL"})
    _passes(monkeypatch, tmp_path, [x, y, x], fail_at=2)
    result = rg.regenerate(max_passes=5)
    assert result["converged"] is False
    assert result["failed"] == ["capability matrix: exit 2"]
    assert result["changes"] == [["claims"]]

    _passes(monkeypatch, tmp_path, [x], fail_at=1)
    assert rg.regenerate(max_passes=5)["changes"] == []


def test_a_change_outside_the_rows_is_named_as_such(tmp_path, monkeypatch):
    x = _board(CLEAN)
    y = _board(CLEAN, head="# Readiness, reworded")
    _passes(monkeypatch, tmp_path, [x, y, y])
    result = rg.regenerate(max_passes=5)
    assert result["converged"] is True
    assert result["changes"] == [["(lines outside the rows)"], []]
