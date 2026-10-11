"""`hoh run RUN --detach` (I11 slice 1, plan row P5-42): the run starts in an owned run-host
tab of its own and the command returns once a process has started it (a new `process.json`
marker; the lock only says whether that process is still there).

Every test builds real runs under `tmp_path` with the product's own `RunStore`; the Herdr
runtime is `FakeAgentControl` plus one extra verb (`run_in_pane`) whose "process" is the test:
it takes the real process marker of the run, so the lock the command polls is a real lock.
No test starts Herdr, an agent or a process: a `herdr` stub first on `PATH` logs its argv and
fails, `subprocess.run` / `Popen` fail the test, and the stub's log is asserted empty at the
end (autouse fixture). Time is injected (`detach.time`); every fake sleep is guarded (a
positive duration and a call budget), so a poll that spins fails a test instead of the run.

The exit codes are written out as numbers on purpose: they are the contract of the command.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import math
import os
import shlex
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hoh import cli, detach, herdr, surfaces
from hoh.agentcontrol import HerdrAgentControl, NotOwned, OwnedAgentControl, ProgramAgentRole
from hoh.autonomy import AutonomyMode, Horizon, ReleasePolicy, compile_profile
from hoh.contracts import Budgets, Condition, RunState, Stage
from hoh.controller import DispatchError
from hoh.governance import bind, pinned_path
from hoh.store import RunStore

from test_agent_control import FakeAgentControl
from test_surfaces import FakeProcs

KEYS = ["schema", "run_id", "started", "exit_code", "why", "process", "pid", "process_since",
        "process_ended", "pane_id", "tab_id", "registry", "log", "launch", "command",
        "argv_digest", "waited_s", "polls"]
SECRET = "sk-test-not-a-real-key"
REAL_IMPORT_PROBE = detach.import_probe          # the World replaces it; D10 puts it back
ALLOW = ("HOH_RUNS", "HOH_STATE_ROOT", "HOH_PYTHON", "PYTHONPATH", "XDG_STATE_HOME")


# --------------------------------------------------------------------------- #
# The harness: no Herdr, no process, no sleep
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    log = tmp_path / "stub-herdr.log"
    stub = bin_dir / "herdr"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 1\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    for var in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID",
                "HOH_RUNS", "HOH_STATE_ROOT", "HOH_PYTHON", "PYTHONPATH", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    def refuse(*a, **k):
        pytest.fail(f"a test reached the outside world: {a!r} {k!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    for name in ("_cli", "snapshot", "agents", "find_agent", "liveness", "agent_status",
                 "read_agent", "worktree_create", "worktree_list"):
        monkeypatch.setattr(herdr, name, refuse)
    yield
    assert not log.exists() or log.read_text(encoding="utf-8") == "", "the stub herdr was called"


class HostControl(FakeAgentControl):
    """The fake runtime with the one verb beyond the protocol. `on_run(command)` is what the
    typed command does: the test starts (or does not start) the process there."""

    def __init__(self) -> None:
        super().__init__()
        self.on_run = None

    def run_in_pane(self, pane_id, command, *, purpose=""):
        self._log("run_in_pane", pane_id, command, purpose=purpose)
        if self.on_run is not None:
            self.on_run(pane_id, command)


class Clock:
    """An injected clock; `sleep` is guarded: a positive duration, at most `budget` calls.
    `on_sleep(n)` runs after the n-th sleep (the process starting while the command waits)."""

    def __init__(self, budget: int = 40) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.budget = budget
        self.on_sleep = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0, f"a poll slept {seconds!r} s: a wait that does not move the clock spins"
        assert len(self.sleeps) < self.budget, "the poll never ended"
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(len(self.sleeps))


class World:
    def __init__(self, tmp_path, monkeypatch, capsys) -> None:
        self.tmp_path, self.capsys, self.monkeypatch = tmp_path, capsys, monkeypatch
        self.root = tmp_path / "runs"
        self.root.mkdir()
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.fake = HostControl()
        self.procs = FakeProcs()
        self.clock = Clock()
        self.controls = 0
        self.control_error: Exception | None = None
        self.admission_calls: list[dict] = []
        self.storage_error: Exception | None = None
        self.stack = contextlib.ExitStack()
        monkeypatch.setattr(surfaces, "make_control", self._make_control)
        monkeypatch.setattr(surfaces, "make_proc_view", lambda: self.procs)
        monkeypatch.setattr(detach, "time", types.SimpleNamespace(
            monotonic=self.clock.monotonic, sleep=self.clock.sleep))
        monkeypatch.setattr(cli, "storage_admission_for", self._admission)
        self.probe_calls: list[tuple] = []
        self.probe_answer = ""
        monkeypatch.setattr(detach, "import_probe", self._probe)
        self.fake.on_run = self.start_process

    def close(self) -> None:
        self.stack.close()

    # -- seams ----------------------------------------------------------------- #
    def _make_control(self):
        self.controls += 1
        if self.control_error is not None:
            raise self.control_error
        return self.fake

    def _admission(self, option, *, claim=True):
        self.admission_calls.append({"option": option, "claim": claim})
        if self.storage_error is not None:
            raise self.storage_error
        return object(), self.tmp_path / "state"

    def _probe(self, interpreter, env, cwd):
        self.probe_calls.append((interpreter, dict(env), cwd))
        return self.probe_answer

    def start_process(self, pane_id, command):
        """The typed command starts `hoh run`: the process takes the real marker."""
        self.stack.enter_context(RunStore(self.root, self.current).process_marker())

    # -- runs ---------------------------------------------------------------- #
    def run(self, name="r1", stage=Stage.NEW, condition=Condition.ACTIVE) -> RunStore:
        store = RunStore(self.root, name)
        store.ensure()
        state = RunState(run_id=name, repo_path=str(self.work), project_name="p",
                         spec_path=str(self.work / "spec.md"), spec_digest="d",
                         policy_digest="d", profile_digest="d", stage=stage,
                         condition=condition)
        store.state_path.write_text(state.model_dump_json(), encoding="utf-8")
        self.current = name
        return store

    def hoh(self, *argv):
        rc = cli.main(["--root", str(self.root), *argv])
        cap = self.capsys.readouterr()
        return rc, cap.out, cap.err

    def detach(self, run="r1", *flags):
        self.current = run
        return self.hoh("run", run, "--detach", *flags)

    def closes(self) -> list[str]:
        return [v for v in self.fake.verbs() if v in ("close_tab", "close_pane")]

    def tree(self) -> dict:
        """mode, size, mtime (ns) and inode of every entry under the root."""
        out = {}
        for p in sorted(self.root.rglob("*")):
            st = p.lstat()
            out[str(p.relative_to(self.root))] = (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino)
        return out


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch, capsys)
    yield w
    w.close()


def answer_of(out: str) -> dict:
    assert out.endswith("\n") and out.count("\n") == 1, out
    out.encode("ascii")
    return json.loads(out)


def contract(**kw):
    return compile_profile(
        AutonomyMode.UNATTENDED_LONG_HORIZON, contract_id="un", scope=("src/hoh/",),
        horizon=Horizon(max_wallclock_seconds=3600, review_after_seconds=900),
        budgets=Budgets(max_dispatches=10, max_wallclock_seconds=3600), **kw)


def _value_of(argv, flag):
    """The value of `flag` in a typed argument list: the one word that starts with `flag=`
    (every option with a value is one word, `--flag=value`)."""
    words = [w for w in argv if w.startswith(flag + "=")]
    assert len(words) == 1, (flag, words)
    return words[0][len(flag) + 1:]


# --------------------------------------------------------------------------- #
# D1 the launch
# --------------------------------------------------------------------------- #


def test_a_launch_opens_an_owned_run_host_and_returns_once_the_process_has_started(
        world, monkeypatch):
    """Catches: a launch that returns before the process exists (a following `hoh wait` reads
    `idle`), a tab that is not recorded as the run's own, an environment that leaks or is
    missing a key, a command typed before the log and the launch record exist, a launch record
    or digest that does not say what was launched, a storage root left to the tab to resolve,
    a label over the cap, a default deadline that is not 60 s, and an answer whose fields are
    not the documented ones."""
    store = world.run("r1")
    given = {"HOH_RUNS": "/runs/root", "HOH_STATE_ROOT": "/state/root",
             "HOH_PYTHON": "/opt/py/bin/python3", "PYTHONPATH": "/clone/src",
             "XDG_STATE_HOME": "/xdg/state"}
    for key, value in given.items():
        monkeypatch.setenv(key, value)
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "HERDR_SOCKET_TOKEN"):
        monkeypatch.setenv(key, SECRET)
    seen = {}

    def on_run(pane_id, command):
        seen["files"] = sorted(p.name for p in (store.dir / "logs").iterdir())
        seen["lock_free"] = store.running_process() is None
        world.start_process(pane_id, command)

    world.fake.on_run = on_run
    rc, out, err = world.detach("r1", "--role-model", "planner=claude-opus-5-5",
                                "--isolation", "none", "--iterations", "3")
    assert rc == 0 and err == "", err
    a = answer_of(out)
    assert list(a) == KEYS and a["schema"] == "hoh-detach/1"
    assert a["started"] is True and a["exit_code"] == 0 and a["why"] is None
    assert a["process"] == "running" and a["process_ended"] is None
    assert a["pid"] == os.getpid() and a["process_since"]
    assert a["polls"] == 1 and a["waited_s"] == 0.0
    # the tab: opened in the run's directory, with the five allow-listed variables and one more
    (verb, cwd, label, env, _workspace), second = world.fake.calls[0], world.fake.calls[1]
    assert verb == "open_surface" and cwd == str(store.dir) and label == "run-host-r1"
    assert env == {**given, "PYTHONUNBUFFERED": "1"}
    assert [c[0] for c in world.fake.calls] == ["open_surface", "run_in_pane"]
    # recorded as the run's own, in the surfaces file, before the command was typed
    rows = surfaces.listing(store.dir)
    assert len(rows) == 1 and rows[0]["role"] == "program:run-host"
    assert rows[0]["pane_id"] == a["pane_id"] == second[1] and rows[0]["state"] == "owned"
    assert a["registry"] == str(store.dir / surfaces.SURFACES_FILE)
    assert not (store.dir / "owned-agents.jsonl").exists()
    # the log and the launch record exist before the command is typed, and the lock was free
    log, launch = Path(a["log"]), Path(a["launch"])
    assert seen["files"] == sorted([log.name, launch.name]) and seen["lock_free"] is True
    assert log.parent == launch.parent == store.dir / "logs"
    assert log.name.startswith("detached-") and log.suffix == ".log" and launch.suffix == ".json"
    stamp = log.name[len("detached-"):-len(".log")]
    record = json.loads(launch.read_text(encoding="utf-8"))
    assert list(record) == ["schema", "run_id", "launched_at", "argv", "env", "cwd", "command",
                            "log", "pane_id", "tab_id", "role", "start_timeout_s", "argv_digest",
                            "marker_before"]
    assert record["schema"] == "hoh-detach/1" and record["run_id"] == "r1"
    assert record["launched_at"] == stamp and record["cwd"] == str(store.dir)
    assert record["command"] == a["command"] == second[2] and record["log"] == str(log)
    assert record["env"] == env and record["role"] == "program:run-host"
    assert record["pane_id"] == a["pane_id"] and record["tab_id"] == a["tab_id"]
    assert record["start_timeout_s"] == 60.0 and record["marker_before"] is None
    assert detach.DEFAULT_START_TIMEOUT == 60.0
    assert record["argv"][:3] == ["/opt/py/bin/python3", "-m", "hoh.cli"]
    digest = hashlib.sha256(json.dumps({"argv": record["argv"], "env": record["env"]},
                                       sort_keys=True, ensure_ascii=True).encode("ascii")).hexdigest()
    assert record["argv_digest"] == a["argv_digest"] == digest
    other = hashlib.sha256(json.dumps({"argv": record["argv"], "env": {}}, sort_keys=True,
                                      ensure_ascii=True).encode("ascii")).hexdigest()
    assert other != digest, "the digest binds the environment"
    # the command: the same hoh run, no --detach, the storage root typed, output copied to the log
    assert "--detach" not in a["command"] and a["command"].endswith(f"2>&1 | tee -a {log}")
    assert "--iterations=3" in a["command"].split()
    assert "--role-model=planner=claude-opus-5-5" in a["command"].split()
    assert "--storage-root=/state/root" in a["command"].split()
    assert world.fake.purposes[-1] == ("run_in_pane", "start run r1")
    # nothing secret anywhere
    for text in (out, launch.read_text(encoding="utf-8"), log.read_text(encoding="utf-8"),
                 json.dumps(world.fake.calls)):
        assert SECRET not in text
    assert world.admission_calls == [{"option": None, "claim": False}]
    assert world.probe_calls == [("/opt/py/bin/python3",
                                  {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **env},
                                  str(store.dir))]
    # a long run id: the label is capped at 80 characters
    long_id = "r" + "x" * 90
    world.run(long_id)
    world.fake.on_run = world.start_process
    rc, out, err = world.detach(long_id)
    assert rc == 0, err
    assert len(world.fake.calls[-2][2]) == 80 and world.fake.calls[-2][2].startswith("run-host-rxx")


# --------------------------------------------------------------------------- #
# D2 the command is the same hoh run
# --------------------------------------------------------------------------- #


RUN_FLAGS = ["--iterations", "4", "--until-accepted", "--planner", "claude", "--developer",
             "codex", "--qa", "claude", "--no-herdr", "--isolation", "strict", "--role-model",
             "planner=claude-opus-5-5", "--role-model", "qa=claude-sonnet-5-5", "--role-effort",
             "qa=high", "--role-account", "planner=acct-a", "--master-session", "s-1",
             "--master-epoch", "7", "--role-timeout", "900", "--approval-policy", "policy.json",
             "--autonomy", "contract.json", "--program-epoch", "5", "--trust-worktree",
             "--storage-root", "state"]
DETACH_ONLY = {"--detach", "--detach-timeout", "--require-contract"}


def _run_parser():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return parser, sub.choices["run"]


def test_the_command_typed_into_the_tab_is_the_same_hoh_run_without_detach(tmp_path, monkeypatch):
    """Catches: an option of `hoh run` that the detached command drops, renames or changes
    (a role model, the isolation, a trust grant), a relative path that the tab's directory would
    read differently, a flag added to `hoh run` later that nobody maps, and a `--detach` that
    the tab would run again."""
    parser, run_parser = _run_parser()
    monkeypatch.chdir(tmp_path)
    args = parser.parse_args(["--root", "runs", "run", "r1", *RUN_FLAGS, "--detach",
                              "--detach-timeout", "30", "--require-contract"])
    argv = detach.run_argv(args, ["python3", "-m", "hoh.cli"])
    assert argv[:3] == ["python3", "-m", "hoh.cli"]
    # relative paths are made absolute against the caller's directory
    for flag in ("--root", "--approval-policy", "--autonomy", "--storage-root"):
        assert os.path.isabs(_value_of(argv, flag)), flag
    # parsing the typed command gives back the options that were asked for
    again = parser.parse_args(argv[3:])
    mine, theirs = vars(args).copy(), vars(again).copy()
    for key in ("detach", "detach_timeout", "require_contract"):
        assert mine.pop(key) is not None and theirs[key] in (False, None)
        theirs.pop(key)
    for key in ("root", "approval_policy", "autonomy", "storage_root"):
        mine[key] = os.path.abspath(mine[key])
    assert theirs == mine
    # drift guard: every option string of `hoh run` is exercised above or is detach-only
    options = {s for act in run_parser._actions for s in act.option_strings if s != "-h"
               and s != "--help"}
    assert options - DETACH_ONLY - set(RUN_FLAGS) == set(), "an option of hoh run is not covered"
    assert "--detach" not in argv and "--require-contract" not in argv
    # defaults round-trip too; the storage root is typed even when none was given, resolved here
    bare = parser.parse_args(["--root", "runs", "run", "r1", "--detach"])
    assert bare.storage_root is None
    argv = detach.run_argv(bare, ["python3", "-m", "hoh.cli"])
    typed = parser.parse_args(argv[3:])
    assert typed.iterations == bare.iterations
    assert typed.storage_root == os.path.abspath(cli.resolve_storage_root(None))
    monkeypatch.setenv("HOH_STATE_ROOT", "/from/env")
    argv = detach.run_argv(bare, ["python3", "-m", "hoh.cli"])
    assert _value_of(argv, "--storage-root") == "/from/env"
    # the environment given to the tab: five keys, only when set and non-empty, plus one fixed
    assert detach.ENV_ALLOW == ALLOW
    for key in ALLOW:
        monkeypatch.delenv(key, raising=False)
    assert detach.environment() == {"PYTHONUNBUFFERED": "1"}
    for key in ALLOW:
        monkeypatch.setenv(key, "")
    assert detach.environment() == {"PYTHONUNBUFFERED": "1"}, "an empty value is not passed"
    for key in ALLOW:
        monkeypatch.setenv(key, "/v/" + key)
    assert detach.environment() == {**{k: "/v/" + k for k in ALLOW}, "PYTHONUNBUFFERED": "1"}
    assert detach.start_timeout_arg(None) == 60.0
    assert bare.detach is True and bare.detach_timeout is None and bare.require_contract is False
    assert parser.parse_args(["run", "r1"]).detach is False


# --------------------------------------------------------------------------- #
# D3 started means a new process holds the lock
# --------------------------------------------------------------------------- #


def write_marker(store, pid, state, *, since="2026-01-01T00-00-00Z", ended=None):
    """A process.json as `process_marker` writes it, with a pid of the test's choosing."""
    record = {"pid": pid, "boot_id": "b", "since": since, "state": state}
    if ended is not None:
        record["ended"] = ended
    store.process_path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def test_the_start_is_decided_by_a_new_marker_and_the_lock_says_whether_it_still_runs(world):
    """Catches: a launch that returns on the lock alone (the instant before the marker is written,
    or while the previous process's `ended` marker is still the file), on a killed run's old
    `running` marker, or only while the lock is held (a run that ends within a poll interval was
    reported as never started in the probe); a killed new process reported as started; a poll
    that does not move the clock or never ends; a deadline that is not the one asked for."""
    store = world.run("r1")
    world.fake.on_run = lambda pane, command: None

    # (a) no process ever: exit 3 at the deadline, the tab stays owned and recorded
    rc, out, err = world.detach("r1", "--detach-timeout", "2")
    a = answer_of(out)
    assert rc == 3 and err == "" and a["started"] is False and a["exit_code"] == 3
    assert a["why"] and "2 s" in a["why"] and a["pid"] is None and a["process"] is None
    assert world.clock.sleeps == [0.25] * 8 and a["polls"] == 9 and a["waited_s"] == 2.0
    assert [v for v in world.fake.verbs() if v.startswith("close")] == []
    assert surfaces.listing(store.dir)[0]["state"] == "owned"
    assert Path(a["launch"]).is_file() and Path(a["log"]).is_file()

    def fresh():
        world.clock.sleeps.clear()
        world.clock.now = 0.0
        world.clock.on_sleep = None

    # (b) a process holds the lock but has not written its marker yet: the old marker says `ended`
    write_marker(store, 100, "ended", ended="2026-01-01T00-00-01Z")
    fresh()
    world.fake.on_run = lambda pane, command: world.stack.enter_context(
        RunStore(world.root, "r1").process_lock())
    world.clock.on_sleep = lambda n: write_marker(store, 777, "running") if n == 2 else None
    rc, out, err = world.detach("r1", "--detach-timeout", "10")
    a = answer_of(out)
    assert rc == 0 and a["started"] is True and a["process"] == "running", (out, err)
    assert a["pid"] == 777 and a["polls"] == 3 and a["process_ended"] is None
    assert world.clock.sleeps == [0.25, 0.25], "the held lock alone was not a start"
    world.stack.close()

    # (c) a killed run's `running` marker with a free lock is not a start
    write_marker(store, 4242, "running")
    fresh()
    world.fake.on_run = lambda pane, command: None
    rc, out, err = world.detach("r1", "--detach-timeout", "1")
    assert rc == 3 and answer_of(out)["started"] is False and answer_of(out)["pid"] is None
    rec = json.loads(Path(answer_of(out)["launch"]).read_text(encoding="utf-8"))
    assert rec["marker_before"]["pid"] == 4242 and rec["marker_before"]["state"] == "running"
    assert rec["start_timeout_s"] == 1.0

    # (d) the process starts while the command waits: seen at the next look, never earlier
    fresh()
    world.clock.on_sleep = lambda n: write_marker(store, 888, "running") if n == 3 else None
    world.fake.on_run = lambda pane, command: world.stack.enter_context(
        RunStore(world.root, "r1").process_lock())
    rc, out, err = world.detach("r1", "--detach-timeout", "10")
    a = answer_of(out)
    assert rc == 0 and a["started"] is True and a["polls"] == 4 and a["waited_s"] == 0.75
    assert world.clock.sleeps == [0.25, 0.25, 0.25] and a["pid"] == 888
    world.stack.close()

    # (e) a run that came and went before the first look (no lock now) did start
    fresh()
    world.fake.on_run = lambda pane, command: write_marker(
        store, 999, "ended", since="2026-01-02T00-00-00Z", ended="2026-01-02T00-00-01Z")
    rc, out, err = world.detach("r1", "--detach-timeout", "10")
    a = answer_of(out)
    assert rc == 0 and a["started"] is True and a["process"] == "ended" and a["polls"] == 1
    assert a["pid"] == 999 and a["process_ended"] == "2026-01-02T00-00-01Z"
    assert world.clock.sleeps == []

    # (f) a new `running` marker and no lock: the process was killed after it started
    fresh()
    world.fake.on_run = lambda pane, command: write_marker(store, 1001, "running",
                                                           since="2026-01-03T00-00-00Z")
    rc, out, err = world.detach("r1", "--detach-timeout", "10")
    a = answer_of(out)
    assert rc == 3 and a["started"] is False and a["process"] == "died" and a["pid"] == 1001
    assert "killed" in a["why"] and [v for v in world.fake.verbs() if v.startswith("close")] == []

    # (g) the run ends between the read of its `running` marker and the probe of the lock: it
    # ended in order, never `died`
    fresh()
    armed = []

    def typed(pane, command):
        write_marker(store, 1002, "running", since="2026-01-04T00-00-00Z")
        armed.append(True)

    world.fake.on_run = typed
    real = RunStore.running_process

    def ends_now(self):
        if not armed:
            return real(self)                         # the preflight's look
        write_marker(store, 1002, "ended", since="2026-01-04T00-00-00Z",
                     ended="2026-01-04T00-00-02Z")
        return None

    world.monkeypatch.setattr(RunStore, "running_process", ends_now)
    rc, out, err = world.detach("r1", "--detach-timeout", "10")
    world.monkeypatch.setattr(RunStore, "running_process", real)
    a = answer_of(out)
    assert rc == 0 and a["started"] is True and a["process"] == "ended", (out, err)
    assert a["pid"] == 1002 and a["process_ended"] == "2026-01-04T00-00-02Z"


# --------------------------------------------------------------------------- #
# D4 what is refused, and that nothing is created
# --------------------------------------------------------------------------- #


def _bad_contract(path: Path) -> None:
    path.write_text(contract().model_copy(update={
        "release_policy": ReleasePolicy.AUTO_PUBLISH_PREAUTHORIZED_TARGET,
        "preauthorized_target": None}).model_dump_json(), encoding="utf-8")


def test_a_launch_that_hoh_run_would_refuse_creates_nothing_and_starts_no_role(
        world, tmp_path, monkeypatch):
    """Catches: a refusal that happens after the tab, the log or the record exists (the roles
    never start, but a tab and a launch record are left for a run that was never started), a
    contract problem found by the tab instead of by the caller, a missing contract that is
    taken for none, a shell metacharacter or a leading `=` in a typed word, and a second start
    of a run that executes. Only the run directory and the fake runtime are compared: the state
    root's ledger may be created by the storage admission, as `hoh resume-storage` does."""
    store = world.run("r1")
    good_contract = tmp_path / "contract.json"
    good_contract.write_text(contract().model_dump_json(), encoding="utf-8")
    bad_contract = tmp_path / "incoherent.json"
    _bad_contract(bad_contract)
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{ not json", encoding="utf-8")
    other = tmp_path / "other.json"
    other.write_text(compile_profile(
        AutonomyMode.UNATTENDED_LONG_HORIZON, contract_id="other", scope=("docs/",),
        horizon=Horizon(max_wallclock_seconds=60, review_after_seconds=30),
        budgets=Budgets(max_dispatches=1, max_wallclock_seconds=60)).model_dump_json(),
        encoding="utf-8")
    spaced = tmp_path / "a b"
    spaced.mkdir()
    (spaced / "c.json").write_text(good_contract.read_text(encoding="utf-8"), encoding="utf-8")

    world.run("r-over", Stage.READY_FOR_DELIVERY)
    world.run("r-cancelled", Stage.DEVELOPING, Condition.CANCELLED)
    world.run("r-pinned")
    bind(RunStore(world.root, "r-pinned").dir, contract=good_contract_obj(good_contract))

    cases = [
        ("no such run", "nope", [], "there is no run nope"),
        ("run over", "r-over", [], "already over"),
        ("run cancelled", "r-cancelled", [], "already over"),
        ("autonomy file missing", "r1", ["--autonomy", str(tmp_path / "none.json")],
         "autonomy contract refused"),
        ("autonomy file garbage", "r1", ["--autonomy", str(garbage)], "autonomy contract refused"),
        ("autonomy incoherent", "r1", ["--autonomy", str(bad_contract)],
         "autonomy contract refused"),
        ("another contract than the pinned", "r-pinned", ["--autonomy", str(other)],
         "cannot be resumed under"),
        ("contract required, none", "r1", ["--require-contract"], "--require-contract"),
        ("approval policy refused", "r1", ["--approval-policy", str(garbage)],
         "approval policy refused"),
        ("bad role model", "r1", ["--role-model", "planner"], "expected role=value"),
        ("bad role effort", "r1", ["--role-effort", "nobody=high"], "nobody"),
        ("half a master binding", "r1", ["--master-session", "s-1"], "recovery declaration"),
        ("path with a space", "r1", ["--autonomy", str(spaced / "c.json")],
         "cannot be typed into a shell"),
        ("timeout 0", "r1", ["--detach-timeout", "0"], "--detach-timeout"),
        ("timeout 601", "r1", ["--detach-timeout", "601"], "--detach-timeout"),
        ("timeout nan", "r1", ["--detach-timeout", "nan"], "--detach-timeout"),
        ("an empty planner", "r1", ["--planner", ""], "cannot be typed into a shell"),
        ("an empty developer", "r1", ["--developer", ""], "cannot be typed into a shell"),
        ("an empty qa", "r1", ["--qa", ""], "cannot be typed into a shell"),
    ]
    for ch in [";", "|", "&", "$", "`", "(", ")", "<", ">", "\\", "'", '"', "*", "?", "#", "!",
               "~", "\n", "\t"]:
        cases.append((f"shell character {ch!r}", "r1", ["--role-model", f"planner=a{ch}b"],
                      "cannot be typed into a shell"))
    for name, run, flags, needle in cases:
        before = world.tree()
        rc, out, err = world.detach(run, *flags)
        assert rc == 2 and out == "", (name, rc, out, err)
        assert "run --detach refused" in err and needle in err, (name, err)
        assert world.tree() == before, name
        assert world.fake.calls == [] and world.controls == 0, name

    # a word that begins with `=` (zsh expands `=cmd`): no option can produce one any more
    # (`--planner==claude` begins with `-`), so the typed-word check is asserted directly
    with pytest.raises(detach.DetachRefused, match="cannot be typed into a shell"):
        detach.command_text(["python3", "=cmd"], str(store.dir / "logs" / "detached-1.log"))

    # the options that only mean something with --detach
    for flag in (["--require-contract"], ["--detach-timeout", "5"]):
        rc, out, err = world.hoh("run", "r1", *flag)
        assert rc == 2 and out == "" and "need --detach" in err and world.fake.calls == []

    # a refusal of the machine: storage, a sandbox that is not there, Herdr
    world.storage_error = cli.StorageRefused("storage admission unavailable: not initialised")
    before = world.tree()
    rc, out, err = world.detach("r1")
    assert rc == 2 and out == "" and "not initialised" in err and world.fake.calls == []
    assert world.tree() == before
    world.storage_error = None

    from hoh import sandbox
    def no_sandbox(isolation):
        raise sandbox.SandboxUnavailable("no bubblewrap here")

    real_select = sandbox.select
    monkeypatch.setattr(sandbox, "select", no_sandbox)
    rc, out, err = world.detach("r1", "--isolation", "strict")
    assert rc == 2 and out == "" and "cannot be provided here" in err
    assert world.fake.calls == [] and world.tree() == before
    monkeypatch.setattr(sandbox, "select", real_select)

    world.control_error = herdr.HerdrUnavailable("HERDR_ENV != 1")
    rc, out, err = world.detach("r1")
    assert rc == 2 and out == "" and "run host could not be opened" in err
    assert world.fake.calls == [] and world.tree() == before and world.controls == 1
    world.control_error = None

    # a process that executes the run is never started a second time
    marker = world.stack.enter_context(RunStore(world.root, "r1").process_marker())
    _ = marker
    rc, out, err = world.detach("r1")
    assert rc == 2 and out == "" and "already being executed" in err and world.fake.calls == []
    world.stack.close()

    # the same requests made valid start (a control: the table above refuses for its reasons)
    world.current = "r-pinned"
    rc, out, err = world.detach("r-pinned", "--autonomy", str(good_contract), "--require-contract")
    assert rc == 0 and answer_of(out)["started"] is True, err
    assert pinned_path(RunStore(world.root, "r-pinned").dir).is_file()
    assert store.dir.name == "r1"


def good_contract_obj(path: Path):
    from hoh.autonomy import AutonomyContract
    return AutonomyContract.model_validate_json(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# D5 the contract check
# --------------------------------------------------------------------------- #


def test_bind_without_write_decides_like_bind_and_pins_nothing(tmp_path):
    """Catches: a preflight that pins or witnesses a contract (a launch that is then refused
    for another reason has bound the run), a `write=False` that skips a refusal `bind` makes,
    and a default that changed."""
    from hoh.governance import ContractRefused
    mine = contract()
    run = tmp_path / "r"
    run.mkdir()
    assert bind(run, contract=mine, write=False).contract.contract_id == "un"
    assert sorted(p.name for p in run.rglob("*")) == [], "write=False wrote something"
    assert bind(run, write=False) is None
    assert bind(run, contract=mine, write=False, program_epoch=4).contract.program_epoch == 4
    assert sorted(p.name for p in run.rglob("*")) == []
    pinned = bind(run, contract=mine, program_epoch=2)                    # the default writes
    assert pinned_path(run).is_file() and (run / "approval" / "autonomy.json").is_file()
    before = sorted((p.name, p.stat().st_mtime_ns) for p in run.rglob("*") if p.is_file())
    other = compile_profile(AutonomyMode.UNATTENDED_LONG_HORIZON, contract_id="x",
                            scope=("docs/",),
                            horizon=Horizon(max_wallclock_seconds=60, review_after_seconds=30),
                            budgets=Budgets(max_dispatches=1, max_wallclock_seconds=60))
    for write in (True, False):
        with pytest.raises(ContractRefused, match="cannot be resumed"):
            bind(run, contract=other, write=write)
    bad = mine.model_copy(update={"release_policy": ReleasePolicy.AUTO_PUBLISH_PREAUTHORIZED_TARGET,
                                  "preauthorized_target": None})
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    for write in (True, False):
        with pytest.raises(ContractRefused):
            bind(fresh, contract=bad, write=write)
    assert sorted((p.name, p.stat().st_mtime_ns) for p in run.rglob("*") if p.is_file()) == before
    assert pinned.contract.program_epoch == 2
    assert bind(run, write=False).contract.digest() == pinned.contract.digest()


# --------------------------------------------------------------------------- #
# D6 the reaper
# --------------------------------------------------------------------------- #


def test_the_run_host_is_closed_by_the_reaper_when_the_run_is_over_and_not_before(world):
    """Catches: a run-host tab that the reaper does not know (it would stay open for ever), one it
    closes while the run executes, and one that is registered in the dispatcher's file (closed
    with the run's own panes, `hoh run`'s own tab among them)."""
    store = world.run("r1")
    rc, out, err = world.detach("r1")
    a = answer_of(out)
    assert rc == 0, err
    world.procs.add_shell(a["pane_id"])                       # the tab's shell, asleep
    # while the process executes, a reap keeps the tab
    rows = surfaces.reap(store.dir, close=True)
    assert len(rows) == 1 and rows[0]["outcome"] == "pending" and not world.closes()
    # the run ends: the marker is released, the state says ready for delivery
    world.stack.close()
    state = RunState(run_id="r1", repo_path=str(world.work), project_name="p",
                     spec_path=str(world.work / "spec.md"), spec_digest="d", policy_digest="d",
                     profile_digest="d", stage=Stage.READY_FOR_DELIVERY, condition=Condition.ACTIVE)
    store.state_path.write_text(state.model_dump_json(), encoding="utf-8")
    dry = surfaces.reap(store.dir, close=False)
    assert len(dry) == 1 and dry[0]["outcome"] == "would_close" and not world.closes()
    rows = surfaces.reap(store.dir, close=True)
    assert len(rows) == 1 and rows[0]["outcome"] == "closed"
    assert world.fake.verbs().count("close_tab") == 1
    assert surfaces.listing(store.dir)[0]["state"] == "released"


# --------------------------------------------------------------------------- #
# D7 the verb, over the wire
# --------------------------------------------------------------------------- #


def test_the_command_is_sent_only_into_a_pane_the_run_owns_and_spelled_as_herdr_run(tmp_path):
    """Catches: a command typed into a pane the registry does not own, a runtime without the verb
    that is called anyway, and a spelling of `herdr pane run` that is not the documented one."""
    wire_calls: list[tuple] = []

    def wire(argv, purpose, *, text=False):
        wire_calls.append((list(argv), purpose, text))
        if argv[1:3] == ["tab", "create"]:
            return {"result": {"tab": {"tab_id": "w1:t1"}, "root_pane": {"pane_id": "w1:p1"}}}
        return ""

    base = HerdrAgentControl(wire)
    registry = surfaces.registry_for(tmp_path)
    owned = OwnedAgentControl(base, registry)
    with owned.acting_as(ProgramAgentRole.program("run-host"), program="p", run_id=tmp_path.name):
        surface = owned.open_surface(cwd=str(tmp_path), label="l", env={"A": "1"})
    create = next(c for c in wire_calls if c[0][1:3] == ["tab", "create"])[0]
    assert create[create.index("--env") + 1] == "A=1"
    owned.run_in_pane(surface.pane_id, "echo hi", purpose="start")
    assert wire_calls[-1] == (["herdr", "pane", "run", "w1:p1", "echo hi"], "start", True)
    n = len(wire_calls)
    with pytest.raises(NotOwned):
        owned.run_in_pane("w1:p9", "echo no")
    assert len(wire_calls) == n, "nothing is sent to a pane the run does not own"
    plain = types.SimpleNamespace(backend="x", calls=[])
    plain.open_surface = lambda **kw: surface
    own2 = OwnedAgentControl(plain, registry)
    with pytest.raises(DispatchError, match="cannot run a command"):
        own2.run_in_pane(surface.pane_id, "echo hi")
    assert plain.calls == []


# --------------------------------------------------------------------------- #
# D8 the record is create-only
# --------------------------------------------------------------------------- #


def test_a_launch_record_is_create_only_and_a_failed_record_sends_nothing(world, monkeypatch):
    """Catches: a launch record or log that replaces an earlier one with the same name, a command
    typed after the record could not be written, and an exit code of 0 for a tab that holds
    nothing."""
    store = world.run("r1")
    monkeypatch.setattr(detach, "_utc_stamp", lambda: "20260101T000000000000Z")
    logs = store.dir / "logs"
    logs.mkdir(exist_ok=True)
    mine = logs / "detached-20260101T000000000000Z.json"
    mine.write_text("earlier launch\n", encoding="utf-8")
    rc, out, err = world.detach("r1")
    a = answer_of(out)
    assert rc == 3 and a["started"] is False and "was not sent" in a["why"]
    assert mine.read_text(encoding="utf-8") == "earlier launch\n"
    assert "run_in_pane" not in world.fake.verbs()
    assert surfaces.listing(store.dir)[0]["state"] == "owned", "the tab exists and is recorded"
    assert not [v for v in world.fake.verbs() if v.startswith("close")]


# --------------------------------------------------------------------------- #
# D9 the module
# --------------------------------------------------------------------------- #


def test_the_module_closes_nothing_removes_nothing_and_signals_nothing():
    """Catches: a launch that closes a tab on failure (the process may be starting in it), removes
    a file, signals a process, spawns one anywhere but in `import_probe` (a list, no shell), or
    writes a file by another way than the create-only writer."""
    source = Path(detach.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned_names = {"subprocess", "signal", "shutil", "Popen", "system", "kill", "killpg",
                    "unlink", "rmdir", "rmtree", "remove", "removedirs", "rename", "replace",
                    "truncate", "close_tab", "close_pane", "write_text", "write_bytes", "open"}
    # the one place a process is started: `import_probe`, `subprocess.run` of a list, no shell
    probe = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                 and n.name == "import_probe")
    inside = {id(n) for n in ast.walk(probe)}
    calls = [n for n in ast.walk(probe) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "run"]
    assert len(calls) == 1 and not [k for k in calls[0].keywords if k.arg == "shell"]
    assert isinstance(calls[0].args[0], ast.List)
    seen = set()
    for node in ast.walk(tree):
        if id(node) in inside:
            continue
        if isinstance(node, ast.Import):
            seen |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            seen.add((node.module or "").split(".")[0])
            seen |= {a.name for a in node.names}
        elif isinstance(node, ast.Attribute):
            seen.add(node.attr)
        elif isinstance(node, ast.Name):
            seen.add(node.id)
    assert seen & banned_names == set(), seen & banned_names
    assert "durable_create" in seen
    parser, run_parser = _run_parser()
    assert {"--detach", "--detach-timeout", "--require-contract"} <= {
        s for act in run_parser._actions for s in act.option_strings}
    assert math.isfinite(detach.DEFAULT_START_TIMEOUT) and sys.version_info >= (3, 11)


# --------------------------------------------------------------------------- #
# D10 the tab can import hoh
# --------------------------------------------------------------------------- #


def test_the_tab_must_be_able_to_import_hoh_from_the_run_directory(world, monkeypatch):
    """Catches: a launch whose tab starts in the run directory, cannot import `hoh.cli` (the
    caller ran it from a source tree through its own directory) and fails after the lock was
    taken; a probe that runs with the caller's whole environment or in the caller's directory;
    a probe that is a shell line; a probe that hangs the launch."""
    store = world.run("r1")
    monkeypatch.setenv("PYTHONPATH", "/clone/src")
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setattr(detach, "import_probe", REAL_IMPORT_PROBE)
    runs: list[tuple] = []
    outcome = {"rc": 0, "stderr": "", "raise": None}

    def fake_run(argv, **kw):
        runs.append((list(argv), kw))
        if outcome["raise"] is not None:
            raise outcome["raise"]
        return types.SimpleNamespace(returncode=outcome["rc"], stderr=outcome["stderr"], stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    rc, out, err = world.detach("r1")
    assert rc == 0, err
    (argv, kw), = runs
    assert argv == [sys.executable, "-c", "import hoh.cli"] and kw["cwd"] == str(store.dir)
    assert kw["env"] == {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
                         "PYTHONPATH": "/clone/src", "PYTHONUNBUFFERED": "1"}
    assert kw.get("shell") in (None, False) and kw["timeout"] <= 60
    assert SECRET not in json.dumps(kw["env"])
    world.stack.close()
    # each way the import can fail is a refusal before any tab
    store2 = world.run("r2")
    cases = [({"rc": 1, "stderr": "Traceback\nModuleNotFoundError: No module named 'hoh'"},
              "No module named 'hoh'"),
             ({"rc": 0, "raise": subprocess.TimeoutExpired("x", 60)}, "did not finish"),
             ({"rc": 0, "raise": FileNotFoundError("no interpreter")}, "no interpreter")]
    for change, needle in cases:
        outcome.update({"rc": 0, "stderr": "", "raise": None})
        outcome.update(change)
        world.fake.calls.clear()
        world.controls = 0
        before = world.tree()
        rc, out, err = world.detach("r2")
        assert rc == 2 and out == "" and "cannot import hoh.cli" in err and needle in err, err
        assert world.fake.calls == [] and world.controls == 0 and world.tree() == before
    assert store2.dir.name == "r2"


# --------------------------------------------------------------------------- #
# D11 the preflight order
# --------------------------------------------------------------------------- #


def test_the_preflight_refuses_in_the_documented_order(world, tmp_path):
    """Catches: a preflight that checks in another order than the specification states (a later,
    more expensive or less specific refusal hiding an earlier one), or that skips a check when
    another fault is present. Every fault is present at once; they are removed one by one."""
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{ not json", encoding="utf-8")
    world.run("r-over", Stage.READY_FOR_DELIVERY)
    faults = {"role": ["--role-model", "planner"], "policy": ["--approval-policy", str(garbage)],
              "contract": ["--autonomy", str(garbage)], "master": ["--master-session", "s-1"],
              "isolation": ["--isolation", "strict"]}

    from hoh import sandbox
    real_select = sandbox.select

    def no_sandbox(isolation):
        raise sandbox.SandboxUnavailable("no bubblewrap here")

    world.monkeypatch.setattr(sandbox, "select", no_sandbox)
    world.run("r-order")
    world.stack.enter_context(RunStore(world.root, "r-order").process_marker())
    world.storage_error = cli.StorageRefused("storage admission unavailable: not initialised")
    world.probe_answer = "No module named 'hoh'"

    def flags():
        return [x for f in faults.values() for x in f]

    rc, out, err = world.detach("nope", *flags())
    assert rc == 2 and "there is no run nope" in err
    rc, out, err = world.detach("r-over", *flags())
    assert rc == 2 and "already over" in err

    def release():
        world.stack.close()

    steps = [("already being executed", release),
             ("not initialised", lambda: setattr(world, "storage_error", None)),
             ("expected role=value", lambda: faults.pop("role")),
             ("approval policy refused", lambda: faults.pop("policy")),
             ("autonomy contract refused", lambda: faults.pop("contract")),
             ("recovery declaration refused", lambda: faults.pop("master")),
             ("cannot be provided here", lambda: faults.pop("isolation")),
             ("cannot import hoh.cli", lambda: setattr(world, "probe_answer", ""))]
    for needle, fix in steps:
        rc, out, err = world.detach("r-order", *flags())
        assert rc == 2 and out == "" and needle in err, (needle, err)
        assert world.fake.calls == [], needle
        fix()
    world.monkeypatch.setattr(sandbox, "select", real_select)
    rc, out, err = world.detach("r-order", *flags())
    assert rc == 0 and answer_of(out)["started"] is True, err


# --------------------------------------------------------------------------- #
# D12 a wait that fails once the tab exists
# --------------------------------------------------------------------------- #


def test_a_wait_that_fails_after_the_tab_exists_is_exit_3_not_a_refusal(world, monkeypatch):
    """Catches: an error while waiting for the process (an unreadable marker, a full disk) that
    is reported as exit 2 "nothing created" when a tab, a log and a record exist; or that closes
    the tab."""
    store = world.run("r1")
    world.fake.on_run = lambda pane, command: None

    def broken(*a, **k):
        raise OSError("the marker cannot be read")

    monkeypatch.setattr(detach, "await_start", broken)
    rc, out, err = world.detach("r1")
    a = answer_of(out)
    assert rc == 3 and a["started"] is False and a["exit_code"] == 3 and err == ""
    assert "the wait for the process failed" in a["why"] and "OSError" in a["why"]
    assert Path(a["launch"]).is_file() and Path(a["log"]).is_file()
    assert surfaces.listing(store.dir)[0]["state"] == "owned"
    assert [v for v in world.fake.verbs() if v.startswith("close")] == []


def test_a_marker_that_cannot_be_read_while_waiting_is_exit_3_with_the_tab_kept(world):
    """Catches: `await_start` reading an unreadable `process.json` as "no marker yet" and only
    answering at the deadline with a reason that does not name the read; the answer must be
    exit 3 at once, `the wait for the process failed`, the tab owned and nothing closed."""
    store = world.run("r1")

    def garbage(pane, command):
        store.process_path.write_text("{ this is not json", encoding="utf-8")

    world.fake.on_run = garbage
    rc, out, err = world.detach("r1")
    a = answer_of(out)
    assert rc == 3 and a["started"] is False and err == ""
    assert "the wait for the process failed" in a["why"]
    assert a["polls"] == 0 and a["process"] is None
    assert Path(a["launch"]).is_file() and Path(a["log"]).is_file()
    assert surfaces.listing(store.dir)[0]["state"] == "owned"
    assert [v for v in world.fake.verbs() if v.startswith("close")] == []


# --------------------------------------------------------------------------- #
# i11b: the run id, one word per option, and the gaps of the first slice
# --------------------------------------------------------------------------- #


def _entries(path: Path) -> dict:
    """mode, size, mtime (ns) and inode of every entry below `path`."""
    out = {}
    for p in sorted(path.rglob("*")):
        st = p.lstat()
        out[str(p.relative_to(path))] = (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino)
    return out


def _legacy_state(store) -> bytes:
    """The state of `store` with the dropped field `started_monotonic`: `read_state()` parks it
    and writes it again, `_parse_state` only reads it."""
    data = json.loads(store.state_path.read_text(encoding="utf-8"))
    data["started_monotonic"] = 123.5
    store.state_path.write_text(json.dumps(data), encoding="utf-8")
    return store.state_path.read_bytes()


def test_a_run_id_that_is_not_a_child_of_the_runs_root_reaches_no_command(world):
    """Catches: a run id of `.` or `..` whose run directory is the runs root or its parent (a tab
    opened there, `logs/` and `owned-surfaces.jsonl` written outside the root), a trailing newline
    taken for a name, the names the runs root keeps for itself taken for a run, a refusal that is
    a traceback instead of one line, and any file of the root or its parent touched on the way."""
    parent = world.tmp_path
    for where in (world.root, parent):
        (where / "state.json").write_text(RunState(
            run_id="x", repo_path=str(world.work), project_name="p",
            spec_path=str(world.work / "spec.md"), spec_digest="d", policy_digest="d",
            profile_digest="d").model_dump_json(), encoding="utf-8")
    ids = ["..", ".", "r1\n", "_arenas", "_restore", "backups", "projects", ".timers.lock"]
    commands = [("run", "--detach"), ("run", "--no-herdr", "--iterations", "0"), ("status",)]
    for run_id in ids:
        for command in commands:
            before = (_entries(world.root), _entries(parent))
            argv = [command[0], run_id, *command[1:]]
            rc, out, err = world.hoh(*argv)
            assert rc == 2 and out == "", (argv, rc, out, err)
            assert err.count("\n") == 1 and err.endswith("\n"), (argv, err)
            assert "unsafe file name" in err, (argv, err)
            prefix = "run --detach refused: " if "--detach" in command else "refused: "
            assert err.startswith(prefix), (argv, err)
            assert world.fake.calls == [] and world.controls == 0, argv
            assert world.admission_calls == [] and world.probe_calls == [], argv
            assert (_entries(world.root), _entries(parent)) == before, argv
    # the same root launches an ordinary id
    world.run("r1")
    rc, out, err = world.detach("r1")
    assert rc == 0 and answer_of(out)["started"] is True, err


def test_every_option_with_a_value_is_typed_as_flag_equals_value_even_when_it_begins_with_a_dash(
        world, tmp_path, monkeypatch):
    """Catches: an option typed as two words (`--planner -x`, which the parser of the tab reads
    as an option and exits 2 after the preflight passed), a negative number typed the same way, a
    value read back from the wrong word, a run id that begins with `-` typed as an option or
    refused only after the import probe and the sandbox probe have started."""
    parser, _run_parser_ = _run_parser()
    monkeypatch.chdir(tmp_path)
    asked = ["--iterations=-1", "--until-accepted", "--planner=-p", "--developer=-d", "--qa=-q",
             "--no-herdr", "--isolation=none", "--role-model=planner=-m", "--role-model=qa=-n",
             "--role-effort=planner=-high", "--role-account=planner=-acct", "--master-session=-s",
             "--master-epoch=7", "--role-timeout=-5", "--approval-policy=policy.json",
             "--autonomy=contract.json", "--program-epoch=-2", "--trust-worktree",
             "--storage-root=state"]
    args = parser.parse_args(["--root", "runs", "run", "r1", *asked, "--detach"])
    argv = detach.run_argv(args, ["python3", "-m", "hoh.cli"])
    switches = {"--until-accepted", "--no-herdr", "--trust-worktree"}
    for word in argv[3:]:
        assert not word.startswith("--") or word in switches or "=" in word, word
    assert _value_of(argv, "--iterations") == "-1"
    assert _value_of(argv, "--planner") == "-p"
    assert _value_of(argv, "--developer") == "-d"
    assert _value_of(argv, "--qa") == "-q"
    assert _value_of(argv, "--master-session") == "-s"
    assert _value_of(argv, "--role-timeout") == "-5"
    assert _value_of(argv, "--program-epoch") == "-2"
    assert [w for w in argv if w.startswith("--role-model=")] == [
        "--role-model=planner=-m", "--role-model=qa=-n"]
    assert _value_of(argv, "--role-effort") == "planner=-high"
    assert _value_of(argv, "--role-account") == "planner=-acct"
    assert argv.index("run") + 1 == argv.index("r1")
    # the parser of the tab gives back the namespace that was asked for
    again = vars(parser.parse_args(argv[3:]))
    mine = vars(args).copy()
    for key in ("detach", "detach_timeout", "require_contract"):
        mine.pop(key)
        again.pop(key)
    for key in ("root", "approval_policy", "autonomy", "storage_root"):
        mine[key] = os.path.abspath(mine[key])
    assert again == mine and again["planner"] == "-p" and again["iterations"] == -1

    # through the command: the line typed into the tab, split like a shell, parses back
    monkeypatch.setenv("HOH_PYTHON", "/opt/py/bin/python3")
    world.run("r1")
    rc, out, err = world.detach("r1", "--planner=-p", "--developer=-d", "--qa=-q",
                                "--role-model=planner=-m", "--role-effort=qa=-low",
                                "--role-account=planner=acct-a", "--master-session=s-1",
                                "--master-epoch=3", "--iterations=-1", "--role-timeout=-5",
                                "--program-epoch=-2", "--isolation=none")
    assert rc == 0, err
    a = answer_of(out)
    words = shlex.split(a["command"])
    assert words[:3] == ["/opt/py/bin/python3", "-m", "hoh.cli"]
    assert words[-5:] == ["2>&1", "|", "tee", "-a", a["log"]]
    typed = parser.parse_args(words[3:-5])
    assert typed.planner == "-p" and typed.developer == "-d" and typed.qa == "-q"
    assert typed.role_model == ["planner=-m"] and typed.role_effort == ["qa=-low"]
    assert typed.role_account == ["planner=acct-a"] and typed.master_session == "s-1"
    assert typed.master_epoch == 3 and typed.iterations == -1 and typed.role_timeout == -5
    assert typed.program_epoch == -2 and typed.run_id == "r1" and typed.detach is False
    world.stack.close()

    # a run id that begins with `-`: refused first, nothing started
    world.run("-x")
    from hoh import sandbox
    selects: list = []
    monkeypatch.setattr(sandbox, "select", lambda isolation: selects.append(isolation))
    world.fake.calls.clear()
    world.controls = 0
    world.probe_calls.clear()
    world.admission_calls.clear()
    before = world.tree()
    rc, out, err = world.hoh("run", "--detach", "--isolation", "strict", "--", "-x")
    assert rc == 2 and out == "" and "begins with '-'" in err, (rc, out, err)
    assert err.startswith("run --detach refused: ") and err.count("\n") == 1
    assert world.probe_calls == [] and selects == [] and world.admission_calls == []
    assert world.controls == 0 and world.fake.calls == [] and world.tree() == before


def test_a_word_the_shell_could_read_differently_is_refused_wherever_it_stands(
        world, tmp_path):
    """Catches: a word with a trailing newline (`$` in the pattern), a `;` or a space in the log
    path that is not one of the checked words, a word that begins with `=`, an empty word, and a
    root reached through a symlink whose real path holds a shell character."""
    for word in ("abc\n", "a;b", "a b", "=a", "", "a\r", "a\0b", "a|b", "$x", "a'b"):
        assert detach.SAFE_WORD.match(word) is None, repr(word)
    for word in ("abc", "--a=b", "/x/y.z", "-1", "planner=claude-opus-5-5", "a@b%c+d:e,f"):
        assert detach.SAFE_WORD.match(word) is not None, repr(word)
    log = "/runs/r1/logs/detached-1.log"
    assert detach.command_text(["python3", "-m", "hoh.cli"], log) == (
        f"python3 -m hoh.cli 2>&1 | tee -a {log}")
    for argv, where in ((["python3", "x\n"], log), (["python3", "a;b"], log),
                        (["python3", "=cmd"], log), (["python3", ""], log),
                        (["python3", "a b"], log), (["python3"], log + "\n"),
                        (["python3"], "/runs/a;b/logs/d.log"), (["python3"], "/runs/a b/d.log"),
                        (["python3"], "/runs/r1/logs/d.log;x"), (["python3"], "=/d.log")):
        with pytest.raises(detach.DetachRefused, match="cannot be typed into a shell"):
            detach.command_text(argv, where)

    # a root reached through a symlink: the store resolves it, the real path holds `;`
    real = tmp_path / "semi;colon"
    real.mkdir()
    world.root = real
    world.run("r1")
    link = tmp_path / "link"
    os.symlink(real, link)
    before = _entries(real)
    rc = cli.main(["--root", str(link), "run", "r1", "--detach"])
    cap = world.capsys.readouterr()
    assert rc == 2 and cap.out == "", (rc, cap.out, cap.err)
    assert "run --detach refused" in cap.err and "cannot be typed into a shell" in cap.err
    assert world.fake.calls == [] and world.controls == 0
    assert _entries(real) == before


def test_the_preflight_reads_a_legacy_state_without_migrating_it(world):
    """Catches: a preflight that reads the state with `read_state()`, which parks the legacy state
    (`state.json.v<n>.<UTC>`) and writes it again -- a launch that is refused for another reason
    has then changed the run, and a launch that succeeds has done it before the run started."""
    store = world.run("r1")
    raw = _legacy_state(store)
    from pydantic import ValidationError
    from hoh.contracts import RunState as State
    with pytest.raises(ValidationError):
        State.model_validate_json(raw.decode("utf-8"))
    mtime = store.state_path.stat().st_mtime_ns
    # a launch refused after the state was read
    before = world.tree()
    rc, out, err = world.detach("r1", "--role-model", "planner")
    assert rc == 2 and "expected role=value" in err and world.fake.calls == []
    assert store.state_path.read_bytes() == raw and store.state_path.stat().st_mtime_ns == mtime
    assert sorted(p.name for p in store.dir.glob("state.json.v*")) == []
    assert world.tree() == before
    # a launch that starts
    rc, out, err = world.detach("r1")
    assert rc == 0 and answer_of(out)["started"] is True, err
    assert store.state_path.read_bytes() == raw
    assert sorted(p.name for p in store.dir.glob("state.json.v*")) == []


def test_the_detach_timeout_accepts_exactly_one_to_six_hundred_seconds(world):
    """Catches: a range that is off by one at either end (`1 <= v < 600`, `1 < v`), a value that
    is not finite, a string taken for a number, a record that does not say the deadline that was
    used, and a refusal that leaves something behind."""
    for good in (1, 1.0, 1.5, 59, 600, 600.0):
        got = detach.start_timeout_arg(good)
        assert isinstance(got, float) and got == float(good), good
    for bad in (0, 0.999, -1, 600.001, 601, float("nan"), float("inf"), float("-inf"), "5"):
        with pytest.raises(detach.DetachRefused, match="takes 1 to 600"):
            detach.start_timeout_arg(bad)
    assert detach.start_timeout_arg(None) == 60.0
    for run, value, want in (("r1", "1", 1.0), ("r2", "600", 600.0)):
        world.run(run)
        rc, out, err = world.detach(run, "--detach-timeout", value)
        a = answer_of(out)
        assert rc == 0 and a["started"] is True, err
        record = json.loads(Path(a["launch"]).read_text(encoding="utf-8"))
        assert record["start_timeout_s"] == want and isinstance(record["start_timeout_s"], float)
    world.stack.close()
    world.run("r3")
    for value in ("0.999", "600.001"):
        before = world.tree()
        world.fake.calls.clear()
        rc, out, err = world.detach("r3", "--detach-timeout", value)
        assert rc == 2 and out == "" and "takes 1 to 600" in err, (value, rc, out, err)
        assert world.tree() == before and world.fake.calls == []


def test_a_storage_refusal_is_a_refusal_and_a_defect_is_a_failure(world):
    """Catches: a storage refusal (`StorageRefused`, and `SessionHeld` which is one) answered as
    `run --detach failed:` -- a defect of the command -- and a defect (a `RuntimeError` from the
    admission) taken for a refusal that hides it."""
    world.run("r1")
    for error in (cli.StorageRefused("storage admission unavailable: not initialised"),
                  cli.SessionHeld("Run r1 is already being executed by another process: pid 1")):
        world.storage_error = error
        before = world.tree()
        rc, out, err = world.detach("r1")
        assert rc == 2 and out == "", (type(error).__name__, rc, out, err)
        assert err == f"run --detach refused: {error}\n", err
        assert "failed" not in err
        assert world.tree() == before and world.fake.calls == [] and world.controls == 0
    world.storage_error = RuntimeError("the ledger is broken")
    before = world.tree()
    rc, out, err = world.detach("r1")
    assert rc == 2 and out == "", (rc, out, err)
    assert err.startswith("run --detach failed: RuntimeError") and "refused" not in err
    assert world.tree() == before and world.fake.calls == [] and world.controls == 0


def test_the_wait_never_sleeps_past_the_deadline(world):
    """Catches: a last sleep of a whole poll interval when less of the deadline is left (a
    deadline that is not a multiple of the poll interval is then overshot), a sleep of zero or less,
    and a poll count that does not follow from the sleeps."""
    store = world.run("r1")
    clock = world.clock
    for timeout, want in ((1.1, [0.25] * 4 + [0.1]), (0.1, [0.1]), (0.25, [0.25]), (0.6, [0.25, 0.25, 0.1])):
        clock.sleeps.clear()
        clock.now = 0.0
        marker, process, polls, waited = detach.await_start(store, None, timeout=timeout)
        assert marker is None and process is None
        assert clock.sleeps == pytest.approx(want), (timeout, clock.sleeps)
        assert sum(clock.sleeps) == pytest.approx(timeout) and sum(clock.sleeps) <= timeout + 1e-9
        assert polls == len(want) + 1 and waited <= timeout + 1e-9
    # through the command
    clock.sleeps.clear()
    clock.now = 0.0
    world.fake.on_run = lambda pane, command: None
    rc, out, err = world.detach("r1", "--detach-timeout", "1.1")
    a = answer_of(out)
    assert rc == 3 and a["started"] is False and a["waited_s"] == 1.1 and a["polls"] == 6, (out, err)
    assert clock.now == pytest.approx(1.1) and clock.now <= 1.1 + 1e-9
    assert clock.sleeps == pytest.approx([0.25] * 4 + [0.1])


def test_the_command_goes_through_the_owned_control_and_a_pane_the_run_does_not_own_gets_nothing(
        world, monkeypatch):
    """Catches: the command typed through the raw runtime (a pane the registry does not own would
    receive it), a command typed more than once, a pane or purpose that is not the run host's, and
    a refusal of the owned control that is ignored or closes the tab."""
    store = world.run("r1")
    real = OwnedAgentControl.run_in_pane
    calls: list[tuple] = []

    def recorded(self, pane_id, command, *, purpose=""):
        calls.append((pane_id, command, purpose))
        return real(self, pane_id, command, purpose=purpose)

    monkeypatch.setattr(OwnedAgentControl, "run_in_pane", recorded)
    rc, out, err = world.detach("r1")
    a = answer_of(out)
    assert rc == 0, err
    assert calls == [(a["pane_id"], a["command"], "start run r1")]
    assert world.fake.verbs().count("run_in_pane") == 1
    world.stack.close()

    # a control that says the pane is not the run's own: nothing is sent
    store2 = world.run("r2")
    calls.clear()
    world.fake.calls.clear()

    def not_owned(self, pane_id, command, *, purpose=""):
        calls.append((pane_id, command, purpose))
        raise NotOwned(f"pane {pane_id} is not owned by this run")

    monkeypatch.setattr(OwnedAgentControl, "run_in_pane", not_owned)
    rc, out, err = world.detach("r2")
    a = answer_of(out)
    assert rc == 3 and a["started"] is False and err == ""
    assert "the command was not sent" in a["why"] and "NotOwned" in a["why"]
    assert len(calls) == 1 and calls[0][2] == "start run r2"
    assert "run_in_pane" not in world.fake.verbs()
    assert surfaces.listing(store2.dir)[0]["state"] == "owned"
    assert world.closes() == []
    assert store.dir.name == "r1"
