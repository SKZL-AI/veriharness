"""How the successor is launched and told where to start: the permission mode and
effort it runs under, the idle before its pointer, the command that starts the
product with no `hoh` on PATH, and the lease bound to the successor's own
conversation process.

Herdr is a fake that records argv; nothing here starts Herdr or an agent, and no
test can, even under a mutant of the code it checks: every test that runs the
`rollover` command patches `ro.HerdrLauncher` first (`RecordingLauncher` refuses
to start, so a rotation that gets that far ends at once, exit 5, and records
what it was given), and passes `--ack-timeout 1` so nothing waits. Three tests
start `bash` or the interpreter with a PATH of their own making, in a temporary
directory, and read nothing outside it. The real clock is not used: every wait is
an injected `sleep`.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_rollover import (
    WATCH_TAIL, FakeHerdr, Rotation, accept_argv, assert_still_master, events,
    hoh, make_project, master_args, pointer_command,
)
from test_succession_capsule import World

from hoh import cli
from hoh import rollover as ro
from hoh.rollover import (
    HerdrLauncher, LauncherError, Outcome, RolloverError, RolloverLog, pointer_prompt,
    run_rollover,
)
from hoh.succession import StaleMaster
from hoh.succession_capsule import aborted_path


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def make_launcher(herdr=None, **kw):
    """A real `HerdrLauncher` over a fake Herdr; every sleep is recorded, in the
    same list as every Herdr call, so the order of the two can be read."""
    herdr = herdr or FakeHerdr()
    log: list[tuple] = []

    def run(argv, **k):
        log.append(("herdr", argv[1], argv[2]))
        return herdr(argv, **k)

    launcher = HerdrLauncher(run=run, settle_s=0, sleep=lambda s: log.append(("sleep", s)), **kw)
    return launcher, herdr, log


class Watched:
    """What `Rotation` needs of a launcher (`started`, `stopped`) around the real one."""

    def __init__(self, inner: HerdrLauncher) -> None:
        self.inner, self.started, self.stopped = inner, [], []

    def start(self, **kw):
        handle = self.inner.start(**kw)
        self.started.append({**kw, "cwd": Path(kw["cwd"])})
        return handle

    def stop(self, handle) -> None:
        self.stopped.append(handle)
        self.inner.stop(handle)


NASTY = {
    "root": "/r", "project_id": "p", "capsule_file": "/r/c.capsule.json",
    "capsule_digest": "ab" * 32, "session": "succ", "token_out": "/r/t",
}


# --------------------------------------------------------------------------- #
# L -- the successor's own Claude arguments and the idle before the pointer
# --------------------------------------------------------------------------- #


def test_the_launcher_starts_the_successor_with_the_arguments_it_was_given(tmp_path):
    launcher, herdr, _ = make_launcher(
        agent_args=("--permission-mode", "auto", "--effort", "high"))
    launcher.start(session_id="s", model="claude-opus-5-5", cwd=tmp_path, prompt="P", label="l")
    [start] = herdr.of("agent", "start")
    assert start[start.index("--") + 1:] == [
        "--model", "claude-opus-5-5", "--permission-mode", "auto", "--effort", "high"]
    # by default nothing is added: the call is what it was
    plain, herdr2, _ = make_launcher()
    plain.start(session_id="s", model="claude-opus-5-5", cwd=tmp_path, prompt="P", label="l")
    [start2] = herdr2.of("agent", "start")
    assert start2[start2.index("--") + 1:] == ["--model", "claude-opus-5-5"]
    assert plain.agent_args == () and plain.prompt_delay_s == 0.0


def test_the_prompt_delay_idles_between_the_start_and_the_pointer_and_only_then(tmp_path):
    launcher, _, log = make_launcher(prompt_delay_s=7.5)
    launcher.start(session_id="s", model="m", cwd=tmp_path, prompt="P", label="l")
    assert log == [("herdr", "tab", "create"), ("sleep", 0), ("herdr", "agent", "start"),
                   ("sleep", 7.5), ("herdr", "agent", "prompt")]
    # no delay, no extra sleep: the pointer follows the start at once
    none, _, log0 = make_launcher()
    none.start(session_id="s", model="m", cwd=tmp_path, prompt="P", label="l")
    assert log0 == [("herdr", "tab", "create"), ("sleep", 0), ("herdr", "agent", "start"),
                    ("herdr", "agent", "prompt")]


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), float("-inf")])
def test_a_prompt_delay_that_is_negative_or_not_finite_is_refused_by_the_launcher(bad):
    with pytest.raises(ValueError, match="prompt_delay_s"):
        HerdrLauncher(prompt_delay_s=bad)


# --------------------------------------------------------------------------- #
# L -- the command line
# --------------------------------------------------------------------------- #


class _Refusing:
    """The launcher a rotation gets in these tests: it never starts anything."""

    def start(self, **kw):
        raise LauncherError("the recording launcher starts nothing")

    def stop(self, handle) -> None:
        pass


class RecordingLauncher:
    """Stands in for `ro.HerdrLauncher`: keeps what it was constructed with and
    hands back a launcher that refuses to start (so `rollover` ends LAUNCH_FAILED,
    exit 5, at once -- and a real `herdr` on PATH is never reached)."""

    kwargs: list[dict] = []

    def __new__(cls, **kw):
        cls.kwargs.append(dict(kw))
        return _Refusing()


def test_the_command_line_hands_the_successor_options_to_the_launcher(
        tmp_path, monkeypatch, capsys):
    project = make_project(tmp_path)
    RecordingLauncher.kwargs = []
    monkeypatch.setattr(ro, "HerdrLauncher", RecordingLauncher)
    common = ["rollover", "p", *master_args(project), "--model", "m",
              "--ack-timeout", "1", "--poll", "0.05"]
    assert hoh(project, *common) == 5                              # the recording launcher refuses
    assert hoh(project, *common, "--successor-permission-mode", "auto",
               "--successor-effort", "high", "--prompt-delay", "0.5") == 5
    assert '"LAUNCH_FAILED"' in capsys.readouterr().out
    assert RecordingLauncher.kwargs == [
        {"agent_args": (), "prompt_delay_s": 0.0},
        {"agent_args": ("--permission-mode", "auto", "--effort", "high"), "prompt_delay_s": 0.5},
    ]


def test_the_command_line_passes_the_mode_and_effort_through_the_real_launcher_to_herdr(
        tmp_path, monkeypatch, capsys):
    project = make_project(tmp_path)
    herdr = FakeHerdr()
    monkeypatch.setattr(ro, "HerdrLauncher", lambda **kw: HerdrLauncher(
        run=herdr, settle_s=0, sleep=lambda s: None, **kw))
    rc = hoh(project, "rollover", "p", *master_args(project), "--model", "claude-opus-5-5",
             "--ack-timeout", "1", "--poll", "0.05", "--successor-permission-mode", "auto")
    assert rc == 5 and '"NO_ACK"' in capsys.readouterr().out
    [start] = herdr.of("agent", "start")
    assert start[start.index("--") + 1:] == [
        "--model", "claude-opus-5-5", "--permission-mode", "auto"]


def test_only_auto_is_offered_as_the_successors_permission_mode(tmp_path, monkeypatch, capsys):
    project = make_project(tmp_path)
    RecordingLauncher.kwargs = []
    monkeypatch.setattr(ro, "HerdrLauncher", RecordingLauncher)   # whatever a mutant lets through
    parser = cli.build_parser()                                   # stays off any real Herdr
    base = ["--root", str(project.root), "project", "rollover", "p", *master_args(project),
            "--model", "m", "--ack-timeout", "1", "--poll", "0.05"]
    assert parser.parse_args([*base, "--successor-permission-mode", "auto"]
                             ).successor_permission_mode == "auto"
    assert parser.parse_args(base).successor_permission_mode is None
    # not offered: the role deny-list and house-rules prompt do not travel with the
    # successor (bypassPermissions); it could not run the pointer's command (dontAsk);
    # and the modes that stop at the first approval (default, manual, acceptEdits, plan)
    for refused in ("bypassPermissions", "dontAsk", "default", "manual", "acceptEdits",
                    "plan", "AUTO", ""):
        with pytest.raises(SystemExit) as exc:
            hoh(project, "rollover", "p", *master_args(project), "--model", "m",
                "--ack-timeout", "1", "--poll", "0.05", "--successor-permission-mode", refused)
        assert exc.value.code == 2, refused
    for effort in ("low", "medium", "high", "xhigh", "max"):
        assert parser.parse_args([*base, "--successor-effort", effort]).successor_effort == effort
    with pytest.raises(SystemExit) as exc:
        hoh(project, "rollover", "p", *master_args(project), "--model", "m",
            "--ack-timeout", "1", "--poll", "0.05", "--successor-effort", "turbo")
    assert exc.value.code == 2
    capsys.readouterr()
    assert RecordingLauncher.kwargs == []                         # no launcher was ever built
    assert not (project.store.dir / "rollover").exists()          # nothing was frozen


@pytest.mark.parametrize("delay", ["-1", "nan", "inf", "-inf", "300.5", "1e9"])
def test_a_prompt_delay_outside_zero_to_300_is_refused_with_exit_2_before_anything_happens(
        tmp_path, monkeypatch, capsys, delay):
    project = make_project(tmp_path)
    RecordingLauncher.kwargs = []
    monkeypatch.setattr(ro, "HerdrLauncher", RecordingLauncher)
    rc = hoh(project, "rollover", "p", *master_args(project), "--model", "m",
             f"--prompt-delay={delay}", "--ack-timeout", "1000", "--poll", "0.05")
    err = capsys.readouterr().err
    assert rc == 2 and "refused" in err and "--prompt-delay" in err
    assert RecordingLauncher.kwargs == [] and not (project.store.dir / "rollover").exists()
    assert project.master.problems() == []                         # still the master


def test_a_prompt_delay_that_is_not_shorter_than_the_ack_window_is_refused_with_exit_2(
        tmp_path, monkeypatch, capsys):
    project = make_project(tmp_path)
    RecordingLauncher.kwargs = []
    monkeypatch.setattr(ro, "HerdrLauncher", RecordingLauncher)
    common = ["rollover", "p", *master_args(project), "--model", "m", "--poll", "0.05"]
    for delay, window, refused in (("1", "1", True), ("2", "1", True), ("0.5", "1", False),
                                   ("299", "300", False), ("300", "301", False)):  # 300 is in range
        rc = hoh(project, *common, f"--prompt-delay={delay}", "--ack-timeout", window)
        err = capsys.readouterr().err
        if refused:
            assert rc == 2 and "--ack-timeout" in err and "--prompt-delay" in err, (delay, window)
        else:
            assert rc == 5, (delay, window)                        # it goes on; the recording
    assert len(RecordingLauncher.kwargs) == 3                      # launcher refuses to start
    # a delay of 0 is never refused, whatever the window
    assert hoh(project, *common, "--prompt-delay=0", "--ack-timeout", "1") == 5
    capsys.readouterr()


# --------------------------------------------------------------------------- #
# L -- a first prompt that does not go through fails closed
# --------------------------------------------------------------------------- #


def test_a_pointer_that_is_typed_but_never_answered_ends_in_no_ack_and_the_tab_is_closed(w):
    launcher, herdr, _ = make_launcher(prompt_delay_s=2.0)         # Herdr says the prompt went in
    rotation = Rotation(w, launcher=Watched(launcher), acks=False)  # ...and the successor never acts
    result = rotation.go()
    assert result.outcome is Outcome.NO_ACK and result.predecessor_is_master
    assert [(c[1], c[2]) for c in herdr.calls] == [
        ("tab", "create"), ("agent", "start"), ("agent", "prompt"), ("tab", "close")]
    assert herdr.of("tab", "close") == [["herdr", "tab", "close", "tab-9"]]
    assert events(w) == ["draining", "frozen", "launched", "abort"]
    assert aborted_path(w.dir, result.capsule_id).exists()
    assert_still_master(w, rotation)


def test_a_pointer_herdr_refuses_to_send_ends_launch_failed_and_the_tab_is_closed(w):
    launcher, herdr, _ = make_launcher(FakeHerdr(fail_on=("agent", "prompt")))
    result = Rotation(w, launcher=Watched(launcher)).go()
    assert result.outcome is Outcome.LAUNCH_FAILED and result.predecessor_is_master
    assert herdr.of("tab", "close") == [["herdr", "tab", "close", "tab-9"]]
    assert events(w) == ["draining", "frozen", "abort"]
    assert w.pred.problems() == []


# --------------------------------------------------------------------------- #
# L -- the command the successor runs
# --------------------------------------------------------------------------- #


def test_the_product_invocation_names_the_running_interpreter_and_import_root():
    root = Path(ro.__file__).resolve().parent.parent
    assert (root / "hoh" / "cli.py").is_file()
    assert ro.product_invocation() == (
        "env", f"PYTHONPATH={root}", sys.executable, "-P", "-m", "hoh.cli")
    assert ro.product_invocation(python="/opt/py/bin/python3", package_root=root) == (
        "env", f"PYTHONPATH={root}", "/opt/py/bin/python3", "-P", "-m", "hoh.cli")


def test_a_product_that_cannot_name_itself_is_refused_rather_than_pointed_at(tmp_path):
    with pytest.raises(RolloverError, match="interpreter"):
        ro.product_invocation(python="")
    with pytest.raises(RolloverError, match="interpreter"):
        ro.product_invocation(python="python3")                    # relative: not a command anywhere
    with pytest.raises(RolloverError, match="import root"):
        ro.product_invocation(package_root=tmp_path)               # no hoh/cli.py in it
    # a newline in a path would split the pointer's one command line in two
    odd = tmp_path / "a\nb"
    (odd / "hoh").mkdir(parents=True)
    (odd / "hoh" / "cli.py").write_text("", encoding="utf-8")
    with pytest.raises(RolloverError, match="printable"):
        ro.product_invocation(package_root=odd)
    with pytest.raises(RolloverError, match="printable"):
        ro.product_invocation(python="/opt/py\n/python3")


def test_a_product_that_cannot_name_itself_is_refused_before_the_authority_is_checked(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "executable", "")

    class Stale:                                       # an authority whose check would refuse
        clock = staticmethod(lambda: "2026-10-08T00:00:00Z")

        def check(self):
            raise StaleMaster("this session is no longer the master")

    with pytest.raises(RolloverError, match="interpreter"):    # not a REFUSED result of the check
        run_rollover(Stale(), tmp_path / "d", inp=None, observer=None, launcher=None,
                     root=tmp_path, repo=str(tmp_path), project_id="p", successor_session="s")
    assert not (tmp_path / "d").exists()
    # and through the command line it is `refused:` exit 2, with the session still the master
    (tmp_path / "proj").mkdir()
    project = make_project(tmp_path / "proj")
    RecordingLauncher.kwargs = []
    monkeypatch.setattr(ro, "HerdrLauncher", RecordingLauncher)
    rc = hoh(project, "rollover", "p", *master_args(project), "--model", "m",
             "--ack-timeout", "1", "--poll", "0.05")
    err = capsys.readouterr().err
    assert rc == 2 and "refused" in err and "interpreter" in err
    assert not (project.store.dir / "rollover").exists() and project.master.problems() == []


def test_a_rotation_that_cannot_name_the_product_refuses_before_anything_is_frozen(w, monkeypatch):
    monkeypatch.setattr(sys, "executable", "")
    rotation = Rotation(w)
    with pytest.raises(RolloverError, match="interpreter"):
        rotation.go()
    assert not rotation.launcher.started and RolloverLog(w.dir).events() == []
    assert not (w.dir / "rollover").exists() and w.pred.problems() == []


def test_a_rotation_points_the_successor_at_this_products_own_invocation_unless_told_otherwise(w):
    rotation = Rotation(w)
    assert rotation.go().outcome is Outcome.COMPLETE
    words = shlex.split(pointer_command(rotation.launcher.started[0]["prompt"])
                        .removesuffix(WATCH_TAIL))
    n = len(ro.product_invocation())
    assert words[:n] == list(ro.product_invocation()) and words[n] == "--root"
    other = Rotation(World(w.root.parent / "other"))
    assert other.go(invocation=("/usr/bin/env", "hoh")).outcome is Outcome.COMPLETE
    words = shlex.split(pointer_command(other.launcher.started[0]["prompt"])
                        .removesuffix(WATCH_TAIL))
    assert words[:3] == ["/usr/bin/env", "hoh", "--root"]


def test_every_word_of_the_invocation_is_shell_quoted_in_the_pointer():
    invocation = ("env", "PYTHONPATH=/a b/it's/src", "/py thon/bin/python3", "-m", "hoh.cli")
    command = pointer_command(pointer_prompt(**NASTY, invocation=invocation))
    assert command.endswith(WATCH_TAIL)
    words = shlex.split(command.removesuffix(WATCH_TAIL))
    assert words[:5] == list(invocation)
    assert shlex.quote("PYTHONPATH=/a b/it's/src") in command      # one word, not an assignment to a shell


def bare_path(tmp_path: Path) -> dict:
    """An environment whose PATH is one directory of our own, holding `bash` and
    `env` and nothing else: a `hoh` installed anywhere on the machine cannot be found."""
    bindir = tmp_path / "bare-bin"
    bindir.mkdir()
    for tool in ("bash", "env"):
        (bindir / tool).symlink_to(shutil.which(tool))
    work = tmp_path / "bare-work"
    work.mkdir()
    return {"PATH": str(bindir), "HOME": str(work), "TMPDIR": str(work),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}


def pointer_start(rotation) -> str:
    """The pointer's command up to the product's own arguments (the words that
    start the product), as the successor's shell would read them."""
    words = shlex.split(pointer_command(rotation.launcher.started[0]["prompt"])
                        .removesuffix(WATCH_TAIL))
    return shlex.join(words[:words.index("--root")])


def test_the_invocation_in_the_pointer_starts_the_product_with_no_hoh_on_path(w, tmp_path):
    rotation = Rotation(w)
    assert rotation.go().outcome is Outcome.COMPLETE
    start = pointer_start(rotation)
    env = bare_path(tmp_path)
    work = Path(env["HOME"])
    assert subprocess.run(["bash", "-c", "command -v hoh"], env=env, cwd=work,
                          capture_output=True).returncode == 1     # nothing named hoh is reachable
    done = subprocess.run(["bash", "-c", start + " --help"], env=env, cwd=work,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, (start, done.stderr)
    assert done.stdout.startswith("usage: hoh")


def test_a_hoh_package_in_the_successors_directory_does_not_shadow_the_product(w, tmp_path):
    rotation = Rotation(w)
    assert rotation.go().outcome is Outcome.COMPLETE
    start = pointer_start(rotation)
    env = bare_path(tmp_path)
    work = Path(env["HOME"])
    (work / "hoh").mkdir()
    (work / "hoh" / "__init__.py").write_text("", encoding="utf-8")
    (work / "hoh" / "cli.py").write_text("print('SHADOWED-CLI')\n", encoding="utf-8")
    done = subprocess.run(["bash", "-c", start + " --help"], env=env, cwd=work,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and "SHADOWED-CLI" not in done.stdout, (done.stdout, done.stderr)
    assert done.stdout.startswith("usage: hoh")


def test_the_pointer_binds_the_lease_to_the_conversation_process_and_nothing_else_is_added():
    text = pointer_prompt(**NASTY)
    command = pointer_command(text)
    assert command.endswith(' --watch-pid "${CLAUDE_PID:-$PPID}"') and command.count("--watch-pid") == 1
    assert ro.SESSION_PID_EXPR == '"${CLAUDE_PID:-$PPID}"'
    # what the successor's shell makes of it: Claude Code's variable, else its parent
    expr = ro.SESSION_PID_EXPR
    base = {"PATH": "/usr/bin:/bin"}
    named = subprocess.run(["bash", "-c", f"echo {expr}"], env={**base, "CLAUDE_PID": "4242"},
                           capture_output=True, text=True, timeout=30)
    assert named.stdout.strip() == "4242"
    unnamed = subprocess.run(["bash", "-c", f"echo {expr}"], env=base,
                             capture_output=True, text=True, timeout=30)
    assert unnamed.stdout.strip() == str(os.getpid())              # bash's parent: this process
    # and the real parser takes what the shell makes of the line
    parsed = cli.build_parser().parse_args(accept_argv(text, watch_pid=4242))
    assert parsed.project_cmd == "rollover-accept" and parsed.watch_pid == 4242
    assert text.isascii() and len(text) < ro.POINTER_MAX_CHARS
