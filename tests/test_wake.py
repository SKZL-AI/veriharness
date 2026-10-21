"""P3-WAKE slice 1: the master consults its predecessor (`hoh.wake`).

Real successions are made here -- `freeze_capsule`, `Successor.acknowledge`,
`handoff`, `Successor.take` -- so the receipts, capsules and epoch chain the
module reads are the ones the product writes (A -> B -> C unless a test says
otherwise). The real clock is used (a few milliseconds per hand-over), a fake
observer stands for the world, and `FakeControl` stands for Herdr behind
`AgentControlAdapter`: it records every verb, and any verb it has no method for
fails the test, so a `close_*` call anywhere is a failure.

Nothing here starts Herdr, an agent or a subprocess, reads `~/.agents`, or writes
outside `tmp_path`. Every look at a file that could block runs under a 10 s
alarm that raises a `BaseException`, so a hang is a failed test.
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
from pathlib import Path

import pytest
from test_succession_capsule import FakeObserver

from hoh import cli
from hoh.agentcontrol import AgentInfo, Surface
from hoh.controller import DispatchError
from hoh.epoch import EpochStore
from hoh.herdr import HerdrUnavailable
from hoh.orchestrator import write_lock
from hoh.project import Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.rollover import Successor
from hoh.store import StaleWrite
from hoh.succession import MasterAuthority, StaleMaster
from hoh.succession_capsule import CapsuleInput, freeze_capsule, sha256_of

RID = "123e4567-e89b-42d3-a456-426614174000"
NONCE = "a1b2c3"
SRC = Path(__file__).resolve().parents[1] / "src" / "hoh"


def wk():
    """`hoh.wake`, imported when a test needs it and not when the file is
    collected: a tree without the module fails each test for what it says."""
    import importlib

    return importlib.import_module("hoh.wake")


class Hang(BaseException):
    """Raised by the alarm: not an `Exception`, so no `except Exception` or
    `except OSError` in the code under test can turn a hang into a refusal."""


@contextlib.contextmanager
def alarm(seconds: int = 10):
    def fire(signum, frame):
        raise Hang(f"blocked for {seconds} s")

    old = signal.signal(signal.SIGALRM, fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# --------------------------------------------------------------------------- #
# the world: a project and real successions
# --------------------------------------------------------------------------- #


class Program:
    """One project. `first` makes a session the master; `hand_over` runs a real
    rollover from one master to the next, with the records the product writes."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "hohroot"
        self.store = ProjectStore(self.root, "p")
        self.repo = str(tmp_path / "repo")
        self.store.create(ProjectState(project_id="p", repo_path=self.repo, nodes=[
            TaskNode(id="a", lifecycle=Lifecycle.BLOCKED, note="waiting")]))
        self.dir = self.store.dir
        plan = tmp_path / "PLAN.md"
        plan.write_text("plan\n")
        self.observer = FakeObserver(self.repo, str(tmp_path / "runs"),
                                     {str(plan): "sha256:plan-1"})
        self.input = CapsuleInput.model_validate({
            "successor_model": "claude-opus-5-5",
            "derive": [{"entry_id": "plan", "section": "REGISTER",
                        "deriver": "file_digest", "params": {"path": str(plan)}},
                       {"entry_id": "runs", "section": "RUNS", "deriver": "run_set",
                        "params": {"runs_root": str(tmp_path / "runs")}}]})
        self.masters: dict[str, MasterAuthority] = {}
        self.capsules: list[str] = []

    def authority(self, session: str) -> MasterAuthority:
        return MasterAuthority(self.dir, session, serialize=lambda: write_lock(self.store))

    def first(self, session: str) -> MasterAuthority:
        a = self.authority(session)
        a.acquire(program_generation=self.store.read_state().program_generation)
        self.masters[session] = a
        return a

    def hand_over(self, pred: str, successor: str) -> MasterAuthority:
        """A real rollover pred -> successor; returns the successor's handle."""
        tail = re.sub(r"[^A-Za-z0-9]", "", f"{pred[-6:]}{successor[-6:]}")
        capsule_id = f"rc-{len(self.capsules) + 1:02d}-{tail}"
        self.capsules.append(capsule_id)
        p = self.masters[pred]
        frozen = freeze_capsule(p, self.input, directory=self.dir, observer=self.observer,
                                repo=self.repo, project_id="p", successor_session=successor,
                                ack_window_s=600, capsule_id=capsule_id)
        launch = self.dir / "rollover" / f"{capsule_id}.cwd"
        launch.mkdir()
        auth = self.authority(successor)
        suc = Successor(auth, frozen.path, observer=self.observer,
                        pointer_digest=frozen.capsule.capsule_digest, poll_s=0.01, wait_s=30)
        ver, ack = suc.acknowledge()
        assert ver.ok, [str(d) for d in ver.differences]
        p.handoff(successor, expires_in=300, reason="test")
        suc.take(ack, token_out=str(launch / "master.token"))   # where the pointer says
        self.masters[successor] = auth
        return auth

    def chain(self) -> "Program":
        """A -> B -> C."""
        self.first("orch-a")
        self.hand_over("orch-a", "orch-b")
        self.hand_over("orch-b", "orch-c")
        return self

    def token_file(self, session: str, name: str | None = None) -> Path:
        path = self.tmp / (name or f"{session}.token")
        path.write_text(self.masters[session].token)
        return path

    def brief(self, name: str = "brief.md") -> str:
        path = self.tmp / name
        if not path.exists():
            path.write_text("what did you decide about X?\n")
        return str(path)

    def answer(self, name: str = "answer.md") -> str:
        """The file the predecessor will write: a directory of the caller's own."""
        folder = self.tmp / "answers"
        folder.mkdir(exist_ok=True)
        return str(folder / name)

    def receipt_file(self, capsule_id: str) -> Path:
        return self.dir / "rollover" / f"{capsule_id}.receipt.json"

    def plan(self, session: str = "orch-c", **kw):
        a = self.masters[session]
        mine = self.authority(session)
        mine.present(epoch=a.epoch, token=a.token)
        kw.setdefault("project_id", "p")
        kw.setdefault("resume_id", RID)
        kw.setdefault("brief", self.brief())
        kw.setdefault("answer", self.answer())
        kw.setdefault("nonce", NONCE)
        return wk().plan_wake(mine, self.dir, **kw)

    def snapshot(self) -> dict[str, tuple]:
        out = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and not p.is_symlink():
                st = p.stat()
                out[str(p.relative_to(self.root))] = (
                    st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            else:
                out[str(p.relative_to(self.root))] = ("kind", stat.S_IFMT(p.lstat().st_mode))
        return out


@pytest.fixture
def prog(tmp_path):
    return Program(tmp_path).chain()


def resign(data: dict) -> dict:
    body = {k: v for k, v in data.items() if k != "receipt_digest"}
    return {**body, "receipt_digest": sha256_of(body)}


class FakeControl:
    """`AgentControlAdapter` in memory, for the three verbs the wake may use.
    Any other attribute (every `close_*`, `press`, `snapshot`, ...) fails."""

    backend = "fake"

    def __init__(self, *, start_errors=(), prompt_error=None, open_error=None) -> None:
        self.calls: list[tuple] = []
        self.start_errors = list(start_errors)
        self.prompt_error = prompt_error
        self.open_error = open_error

    def open_surface(self, *, cwd, label, env=None, workspace=None, purpose="create tab"):
        self.calls.append(("open_surface", {"cwd": cwd, "label": label, "env": env,
                                            "workspace": workspace}))
        if self.open_error:
            raise self.open_error
        return Surface(pane_id="w1:p9", tab_id="w1:t9")

    def start(self, name, *, kind, pane_id, args=(), timeout_ms=None, purpose=""):
        self.calls.append(("start", {"name": name, "kind": kind, "pane_id": pane_id,
                                     "args": tuple(args), "timeout_ms": timeout_ms}))
        if self.start_errors:
            raise self.start_errors.pop(0)
        return AgentInfo("fake", name, "idle", pane_id, "w1:t9", "w1", "/")

    def prompt(self, name, text, *, timeout_ms, purpose=""):
        self.calls.append(("prompt", {"name": name, "text": text, "timeout_ms": timeout_ms}))
        if self.prompt_error:
            raise self.prompt_error
        return AgentInfo("fake", name, "idle", "w1:p9", "w1:t9", "w1", "/")

    def __getattr__(self, name):
        raise AssertionError(f"the wake used control.{name}")

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]


# --------------------------------------------------------------------------- #
# the plan: what it says
# --------------------------------------------------------------------------- #


def test_the_master_gets_a_plan_that_names_the_predecessor_its_directory_and_two_files(prog):
    plan = prog.plan()
    rdir = os.path.realpath(prog.dir / "rollover")
    s = plan.succession
    assert (s.predecessor_session, s.predecessor_epoch) == ("orch-b", 2)
    assert (s.successor_session, s.successor_epoch) == ("orch-c", 3)
    assert s.capsule_id == prog.capsules[1]                    # the B -> C capsule
    # B was started by the A -> B rollover, in the directory that rollover made
    assert plan.cwd == f"{rdir}/{prog.capsules[0]}.cwd"
    assert plan.kind == "claude" and plan.agent_args == ("--resume", RID)
    assert plan.label == "VH \u25b8 predecessor orch-b (consult only) (keep)"
    assert re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", plan.agent_name)
    assert len(plan.agent_name) <= 24 and plan.agent_name.startswith(f"w{NONCE}-orch-b")
    assert plan.brief_file == prog.brief()
    assert plan.answer_file == prog.answer()
    assert plan.prompt.isascii() and len(plan.prompt) < 2000
    assert plan.brief_file in plan.prompt and plan.answer_file in plan.prompt
    assert plan.as_dict()["schema"] == "hoh-wake-plan/1"
    json.dumps(plan.as_dict())                                  # plain data


def test_planning_writes_nothing_and_a_plan_names_no_credential(prog):
    before = prog.snapshot()
    plan = prog.plan()
    assert prog.snapshot() == before                            # not a byte, not a timestamp
    text = json.dumps(plan.as_dict()) + plan.prompt
    for session in ("orch-a", "orch-b", "orch-c"):
        token = prog.masters[session].token
        assert token not in text
        assert hashlib.sha256(token.encode()).hexdigest() not in text


def test_a_receipt_selects_by_epoch_not_by_session_alone(tmp_path):
    """C holds epoch 3, hands over to D, and D hands back to C: C is master again at
    epoch 5. Two receipts name C as successor; the plan is the one of epoch 5."""
    p = Program(tmp_path)
    p.first("orch-a")
    p.hand_over("orch-a", "orch-b")
    p.hand_over("orch-b", "orch-c")
    p.hand_over("orch-c", "orch-d")
    c2 = p.hand_over("orch-d", "orch-c")                        # a fresh handle: epoch 5
    assert c2.epoch == 5
    plan = p.plan("orch-c")
    s = plan.succession
    assert (s.predecessor_session, s.predecessor_epoch, s.successor_epoch) == ("orch-d", 4, 5)
    assert s.capsule_id == p.capsules[3]
    assert plan.cwd.endswith(f"/{p.capsules[2]}.cwd")           # D was started by C's rollover


def test_the_first_master_has_no_launch_directory_in_the_records_and_it_must_be_given(tmp_path):
    p = Program(tmp_path)
    p.first("orch-a")
    p.hand_over("orch-a", "orch-b")
    with pytest.raises(wk().WakeUsage, match="--cwd"):
        p.plan("orch-b")
    start = tmp_path / "started-here"
    start.mkdir()
    plan = p.plan("orch-b", cwd=str(start))
    assert plan.cwd == os.path.realpath(start) and plan.succession.predecessor_cwd is None
    accent = tmp_path / "caf\u00e9"
    accent.mkdir()
    for bad in ("relative/dir", str(tmp_path / "nowhere"), str(tmp_path / "PLAN.md"),
                str(accent), ""):
        with pytest.raises(wk().WakeUsage):
            p.plan("orch-b", cwd=bad)


def test_an_explicit_directory_may_restate_the_records_and_may_not_contradict_them(prog, tmp_path):
    recorded = Path(prog.plan().cwd)
    assert prog.plan(cwd=str(recorded)).cwd == str(recorded)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(wk().WakeUsage, match="records say"):
        prog.plan(cwd=str(elsewhere))
    spelled = tmp_path / "spelled"                              # another spelling, same place
    spelled.symlink_to(recorded)
    assert prog.plan(cwd=str(spelled)).cwd == str(recorded)


# --------------------------------------------------------------------------- #
# the plan: who may have one
# --------------------------------------------------------------------------- #


def test_only_the_live_master_gets_a_plan(prog):
    # the predecessor itself, with its own old token
    b = prog.authority("orch-b")
    b.present(epoch=2, token=prog.masters["orch-b"].token)
    # the master's session and epoch with a token that is not its own
    forged = prog.authority("orch-c")
    forged.present(epoch=3, token="0" * 32)
    # the master's session id at an epoch ahead of the chain
    ahead = prog.authority("orch-c")
    ahead.present(epoch=4, token=prog.masters["orch-c"].token)
    # a handle that never acquired
    never = prog.authority("orch-c")
    for who in (b, forged, ahead, never):
        with pytest.raises(wk().WakeRefused, match="not the master"):
            wk().plan_wake(who, prog.dir, project_id="p", resume_id=RID,
                           brief=prog.brief(), answer=prog.answer(), nonce=NONCE)


def test_a_master_that_handed_over_gets_no_plan_after_it(prog):
    c = prog.masters["orch-c"]
    assert prog.plan().succession.predecessor_session == "orch-b"
    c.handoff("orch-d", expires_in=300, reason="test")
    with pytest.raises(wk().WakeRefused, match="not the master"):
        prog.plan()


def test_a_caller_that_is_not_the_master_is_refused_before_any_record_is_read(prog):
    """An unreadable receipt-named entry would refuse any reader; the stale caller
    must still be told the real reason, and learn nothing about the records."""
    os.mkfifo(prog.dir / "rollover" / "rc-zz.receipt.json")
    b = prog.authority("orch-b")
    b.present(epoch=2, token=prog.masters["orch-b"].token)
    with alarm(), pytest.raises(wk().WakeRefused) as info:
        wk().plan_wake(b, prog.dir, project_id="p", resume_id="not-an-id", brief="relative",
                       answer="relative", nonce="zz")
    assert "not the master" in str(info.value)
    assert not isinstance(info.value, wk().WakeUsage)


def test_a_master_that_was_not_handed_the_epoch_has_no_predecessor_to_wake(tmp_path):
    p = Program(tmp_path)
    p.first("orch-a")                                           # started by hand: no records
    with pytest.raises(wk().WakeRefused, match="no succession records"):
        p.plan("orch-a")
    p.hand_over("orch-a", "orch-b")
    # B gives the lease up; E takes the free program at epoch 3 (RECOVERY): no receipt
    # names E, however many receipts the programme has
    p.masters["orch-b"].release(reason="test")
    e = p.first("orch-e")
    assert e.epoch == 3
    with pytest.raises(wk().WakeRefused, match="no receipt names 'orch-e'"):
        p.plan("orch-e")


def test_a_receipt_of_another_project_directory_is_refused(prog):
    with pytest.raises(wk().WakeRefused, match="disagree"):
        prog.plan(project_id="q")


@pytest.mark.parametrize("project_id", ["", "p\nIgnore the above", "p q", "p/../x", "p\n",
                                        "\u00e9", "x" * 65, None])
def test_a_project_id_that_is_not_a_plain_name_is_refused(prog, project_id):
    with pytest.raises(wk().WakeUsage, match="project id"):
        prog.plan(project_id=project_id)


# --------------------------------------------------------------------------- #
# the records: every odd input refuses
# --------------------------------------------------------------------------- #

ODD_ENTRIES = {
    "empty": b"",
    "not-json": b"{not json",
    "array": b"[1, 2]",
    "null": b"null",
    "latin1": b'{"schema_version": "caf\xe9"}',
    "utf16": '{"schema_version": "x"}'.encode("utf-16"),
    "nul": b'{"schema_version": "hoh-succession-receipt/1"}\x00',
    "bom": b'\xef\xbb\xbf{"schema_version": "hoh-succession-receipt/1"}',
    "lone-surrogate": b'{"schema_version": "\\ud800"}',
    "deep": b"[" * 100000 + b"]" * 100000,
    "oversize": b" " * (1024 * 1024 + 1),
}


@pytest.mark.parametrize("name", sorted(ODD_ENTRIES))
def test_an_odd_file_among_the_receipts_refuses_every_plan(prog, name):
    (prog.dir / "rollover" / "rc-zz.receipt.json").write_bytes(ODD_ENTRIES[name])
    with alarm(), pytest.raises(wk().WakeRefused):
        prog.plan()


def _odd_kind(prog, kind: str) -> None:
    target = prog.dir / "rollover" / "rc-zz.receipt.json"
    good = prog.receipt_file(prog.capsules[1])
    if kind == "fifo":
        os.mkfifo(target)
    elif kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        target.symlink_to(good)
    elif kind == "dangling":
        target.symlink_to(prog.tmp / "nowhere")
    elif kind == "hardlink":
        os.link(good, target)
    elif kind == "unreadable":
        target.write_bytes(good.read_bytes())
        target.chmod(0)
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink", "dangling", "hardlink",
                                  "unreadable"])
def test_an_odd_kind_of_file_among_the_receipts_refuses_every_plan(prog, kind):
    if kind == "unreadable" and os.geteuid() == 0:
        pytest.skip("mode 000 does not stop root")
    _odd_kind(prog, kind)
    with alarm(), pytest.raises(wk().WakeRefused):
        prog.plan()


def test_a_rollover_directory_that_is_a_link_or_missing_refuses(prog, tmp_path):
    real = prog.dir / "rollover"
    moved = tmp_path / "moved"
    real.rename(moved)
    with pytest.raises(wk().WakeRefused):
        prog.plan()                                             # missing
    real.symlink_to(moved)
    with pytest.raises(wk().WakeRefused, match="not a directory of its own"):
        prog.plan()                                             # a link


def test_a_name_that_is_not_a_receipt_name_is_ignored(prog):
    (prog.dir / "rollover" / "not a receipt.receipt.json").write_text("garbage")
    (prog.dir / "rollover" / "rc-01.receipt.json.v1").write_text("garbage")
    assert prog.plan().succession.predecessor_session == "orch-b"


def _edit_receipt(prog, change, *, sign: bool) -> None:
    path = prog.receipt_file(prog.capsules[1])
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(resign(data) if sign else data, indent=2, sort_keys=True))


@pytest.mark.parametrize("label,change,sign,message", [
    ("changed-after-signing", lambda d: d.update(predecessor_session="orch-x"), False, "digest"),
    ("another-lease", lambda d: d.update(lease_token_sha256="0" * 64), True, "another lease"),
    ("epoch-chain-disagrees", lambda d: d.update(predecessor_epoch=1), True, "epoch chain"),
    ("other-capsule-id", lambda d: d.update(capsule_id="rc-elsewhere"), True, "file name"),
    ("wrong-schema", lambda d: d.update(schema_version="hoh-succession-receipt/2"), True,
     "not a"),
    ("session-with-newline", lambda d: d.update(predecessor_session="orch-b\nevil"), True,
     "plain id"),
    ("session-with-escape", lambda d: d.update(predecessor_session="orch-b\x1b[2J"), True,
     "plain id"),
    ("session-is-the-caller", lambda d: d.update(predecessor_session="orch-c"), True,
     "own predecessor"),
    ("epoch-not-an-int", lambda d: d.update(predecessor_epoch="2"), True, "positive integer"),
    ("epoch-is-a-bool", lambda d: d.update(predecessor_epoch=True), True, "positive integer"),
    ("digest-not-hex", lambda d: d.update(capsule_digest="zz"), True, "sha256"),
    ("capsule-digest-differs", lambda d: d.update(capsule_digest="f" * 64), True, "capsule"),
])
def test_a_receipt_that_does_not_bear_out_the_succession_refuses(prog, label, change, sign,
                                                                  message):
    _edit_receipt(prog, change, sign=sign)
    with pytest.raises(wk().WakeRefused, match=message):
        prog.plan()


def test_a_session_id_of_sixty_four_characters_is_plain_and_sixty_five_is_not(prog):
    long64, long65 = "o" * 64, "o" * 65
    _edit_receipt(prog, lambda d: d.update(predecessor_session=long64), sign=True)
    with pytest.raises(wk().WakeRefused, match="epoch chain"):        # plain; the chain disagrees
        prog.plan()
    _edit_receipt(prog, lambda d: d.update(predecessor_session=long65), sign=True)
    with pytest.raises(wk().WakeRefused, match="plain id"):
        prog.plan()


def test_run_style_session_ids_make_a_name_herdr_accepts(tmp_path):
    """Session ids carry upper-case letters, dots and are long (A7 rehearsal: the fakes never
    checked the agent name)."""
    p = Program(tmp_path)
    p.first("orchestrator-20261012T1432Z-ab12.Cd")
    p.hand_over("orchestrator-20261012T1432Z-ab12.Cd", "Orch.B_1-" + "x" * 50)
    plan = p.plan("Orch.B_1-" + "x" * 50, cwd=str(tmp_path))
    assert re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", plan.agent_name)
    assert plan.agent_name == f"w{NONCE}-orchestrator-202"        # random part first, cut at 24
    assert plan.label == ("VH \u25b8 predecessor orchestrator-20261012T1432Z-ab12.Cd "
                          "(consult only) (keep)")


def test_two_receipts_for_one_epoch_are_ambiguous_and_nothing_is_guessed(prog):
    data = json.loads(prog.receipt_file(prog.capsules[1]).read_text())
    data["capsule_id"] = "rc-dup"
    (prog.dir / "rollover" / "rc-dup.receipt.json").write_text(json.dumps(resign(data)))
    with pytest.raises(wk().WakeRefused, match="ambiguous"):
        prog.plan()


def test_two_receipts_for_the_predecessors_own_epoch_are_ambiguous_too(prog):
    """The directory B was started in comes from the receipt that made B master."""
    data = json.loads(prog.receipt_file(prog.capsules[0]).read_text())
    data["capsule_id"] = "rc-dup-earlier"
    (prog.dir / "rollover" / "rc-dup-earlier.receipt.json").write_text(json.dumps(resign(data)))
    with pytest.raises(wk().WakeRefused, match="ambiguous"):
        prog.plan()


def test_a_capsule_that_is_missing_changed_or_odd_refuses(prog):
    cap = prog.dir / "rollover" / f"{prog.capsules[1]}.capsule.json"
    text = cap.read_text()
    cap.write_text(text.replace('"claude-opus-5-5"', '"claude-sonnet-5-5"'))
    with pytest.raises(wk().WakeRefused, match="digest"):
        prog.plan()
    cap.write_text(text)
    assert prog.plan().succession.predecessor_session == "orch-b"
    moved = prog.tmp / "capsule.moved"
    cap.rename(moved)
    with pytest.raises(wk().WakeRefused, match="cannot be examined"):
        prog.plan()
    os.mkfifo(cap)                                              # a FIFO under the capsule's name
    with alarm(), pytest.raises(wk().WakeRefused):
        prog.plan()


def test_the_launch_directory_must_be_a_directory_of_its_own(prog, tmp_path):
    launch = prog.dir / "rollover" / f"{prog.capsules[0]}.cwd"
    launch.rename(tmp_path / "moved.cwd")
    with pytest.raises(wk().WakeRefused, match="cannot be examined"):
        prog.plan()
    launch.symlink_to(tmp_path / "moved.cwd")
    with pytest.raises(wk().WakeRefused, match="not a directory of its own"):
        prog.plan()


def test_the_record_ceiling_is_one_mebibyte_exactly(prog):
    path = prog.receipt_file(prog.capsules[1])
    body = path.read_bytes().rstrip()
    path.write_bytes(body + b" " * (1024 * 1024 - len(body)))
    assert path.stat().st_size == 1024 * 1024
    assert prog.plan().succession.predecessor_session == "orch-b"     # at the bound: read
    path.write_bytes(body + b" " * (1024 * 1024 + 1 - len(body)))
    with pytest.raises(wk().WakeRefused, match="larger than"):
        prog.plan()                                                    # one byte more: not


# --------------------------------------------------------------------------- #
# the plan: the values the caller supplies
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value,ok", [
    (RID, True),
    (RID.upper(), False),
    (RID + "\n", False),
    (" " + RID, False),
    ("--model", False),
    ("-" + RID[1:], False),
    ("", False),
    (RID[:-1], False),
    (RID + "0", False),
    (RID.replace("-", ""), False),
    (RID[:8] + "\x00" + RID[9:], False),
    ("123e4567-e89b-42d3-a456-42661417400g", False),
])
def test_the_resume_id_is_a_lower_case_uuid_and_nothing_else(prog, value, ok):
    if ok:
        assert prog.plan(resume_id=value).agent_args == ("--resume", value)
    else:
        with pytest.raises(wk().WakeUsage, match="resume-id"):
            prog.plan(resume_id=value)


def test_the_brief_is_a_normalized_absolute_regular_file_in_plain_ascii(prog, tmp_path):
    good = prog.brief()
    assert prog.plan(brief=good).brief_file == good
    sub = tmp_path / "sub"
    sub.mkdir()
    fifo = tmp_path / "fifo.md"
    os.mkfifo(fifo)
    link = tmp_path / "link.md"
    link.symlink_to(good)
    odd = tmp_path / "café.md"
    odd.write_text("x")
    nl = tmp_path / "new\nline.md"
    nl.write_text("x")
    for bad in ("brief.md", f"{sub}/../brief.md", f"{good}/", f"{tmp_path}//brief.md",
                str(sub), str(fifo), str(link), str(tmp_path / "missing.md"), str(odd),
                str(nl), ""):
        with alarm(), pytest.raises(wk().WakeUsage):
            prog.plan(brief=bad)


def test_the_answer_file_is_the_callers_new_file_in_a_directory_that_exists(prog, tmp_path):
    answer = prog.plan().answer_file
    assert answer == prog.answer()
    Path(answer).write_text("an earlier answer")
    with pytest.raises(wk().WakeUsage, match="already exists"):
        prog.plan()
    Path(answer).rename(Path(answer + ".moved"))
    Path(answer).symlink_to(prog.tmp / "nowhere")               # a dangling link is a name
    with pytest.raises(wk().WakeUsage, match="already exists"):
        prog.plan()
    folder = Path(answer).parent
    plain_file = tmp_path / "a-file"
    plain_file.write_text("x")
    for bad in ("answer.md", f"{folder}/../answers/new.md", f"{folder}//new.md", f"{folder}/",
                str(folder / "caf\u00e9.md"), str(folder / "new\nline.md"),
                str(tmp_path / "no-such-dir" / "new.md"), f"{plain_file}/new.md",
                "~/new.md", "", prog.brief()):
        with pytest.raises(wk().WakeUsage):
            prog.plan(answer=bad)
    assert prog.plan(answer=str(folder / "another.md")).answer_file == str(folder / "another.md")


def test_the_nonce_is_six_hex_digits_and_names_the_agent(prog):
    assert prog.plan(nonce="000000").agent_name == "w000000-orch-b"
    for bad in ("zzzzzz", "abcde", "abcdef0", "ABCDEF", "abcde\n", "", "a1b2c3\n"):
        with pytest.raises(wk().WakeUsage, match="nonce"):
            prog.plan(nonce=bad)


def test_the_prompt_is_below_two_thousand_characters_exactly(prog, tmp_path):
    fixed = len(prog.plan().prompt) - len(prog.brief())
    def brief_of(total: int) -> str:
        d = tmp_path
        while len(str(d)) + 1 + 200 < total - 8:
            d = d / ("d" * 200)
            d.mkdir(exist_ok=True)
        path = d / ("b" * (total - len(str(d)) - 1))
        path.write_text("q")
        assert len(str(path)) == total
        return str(path)
    ok = brief_of(2000 - 1 - fixed)
    assert len(prog.plan(brief=ok).prompt) == 1999
    too_long = brief_of(2000 - fixed)
    with pytest.raises(wk().WakeRefused, match="2000"):
        prog.plan(brief=too_long)


# --------------------------------------------------------------------------- #
# performing it, through the adapter
# --------------------------------------------------------------------------- #


def test_the_wake_opens_one_surface_starts_the_resumed_session_prompts_once_and_closes_nothing(
        prog):
    plan, control, slept = prog.plan(), FakeControl(), []
    done = wk().perform_wake(plan, control, predecessor_surface_closed=True, workspace="w1",
                         timeout_s=60, sleep=slept.append)
    assert control.verbs() == ["open_surface", "start", "prompt"]
    opened, started, prompted = (c[1] for c in control.calls)
    assert opened == {"cwd": plan.cwd, "label": plan.label, "env": None, "workspace": "w1"}
    assert started == {"name": plan.agent_name, "kind": "claude", "pane_id": "w1:p9",
                       "args": ("--resume", RID), "timeout_ms": wk().START_TIMEOUT_MS}
    assert wk().START_TIMEOUT_MS == 240000
    assert prompted == {"name": plan.agent_name, "text": plan.prompt, "timeout_ms": 60000}
    assert slept == [wk().SETTLE_S]
    assert done == {"outcome": "WOKEN", "agent": plan.agent_name, "pane_id": "w1:p9",
                    "tab_id": "w1:t9", "status": "idle", "answer_file": plan.answer_file}


def test_a_shell_that_is_not_ready_is_asked_again_up_to_five_times_in_all(prog):
    plan = prog.plan()
    busy = lambda: DispatchError("start failed: not an available shell")        # noqa: E731
    slept: list[float] = []
    control = FakeControl(start_errors=[busy() for _ in range(4)])
    wk().perform_wake(plan, control, predecessor_surface_closed=True, sleep=slept.append)
    assert control.verbs().count("start") == 5 and control.verbs()[-1] == "prompt"
    assert slept == [wk().SETTLE_S, 1.0, 1.5, 2.0, 2.5]
    control = FakeControl(start_errors=[busy() for _ in range(9)])
    with pytest.raises(wk().WakeFailed) as info:
        wk().perform_wake(plan, control, predecessor_surface_closed=True, sleep=lambda s: None)
    assert control.verbs() == ["open_surface"] + ["start"] * 5       # five, and no sixth
    assert info.value.surface == Surface("w1:p9", "w1:t9") and not info.value.agent_started
    control = FakeControl(start_errors=[DispatchError("start failed: permission denied")])
    with pytest.raises(wk().WakeFailed):
        wk().perform_wake(plan, control, predecessor_surface_closed=True, sleep=lambda s: None)
    assert control.verbs() == ["open_surface", "start"]              # not retried
    control = FakeControl(start_errors=[DispatchError("start failed: pane is busy")])
    wk().perform_wake(plan, control, predecessor_surface_closed=True, sleep=lambda s: None)
    assert control.verbs() == ["open_surface", "start", "start", "prompt"]   # the other literal


def test_a_failure_after_the_surface_opened_leaves_it_open_and_says_what_was_opened(prog):
    plan = prog.plan()
    control = FakeControl(prompt_error=DispatchError("prompt failed: stalled"))
    with pytest.raises(wk().WakeFailed) as info:
        wk().perform_wake(plan, control, predecessor_surface_closed=True, sleep=lambda s: None)
    assert info.value.surface == Surface("w1:p9", "w1:t9") and info.value.agent_started
    assert control.verbs() == ["open_surface", "start", "prompt"]    # no close of any kind
    control = FakeControl(open_error=DispatchError("tab create failed"))
    with pytest.raises(wk().WakeFailed) as info:
        wk().perform_wake(plan, control, predecessor_surface_closed=True, sleep=lambda s: None)
    assert info.value.surface is None and not info.value.agent_started
    assert control.verbs() == ["open_surface"]


@pytest.mark.parametrize("attest", [False, None, 1, "yes", "True"])
def test_a_wake_needs_the_statement_that_the_predecessors_own_surface_is_closed(prog, attest):
    control = FakeControl()
    with pytest.raises(wk().WakeUsage, match="surface-closed"):
        wk().perform_wake(prog.plan(), control, predecessor_surface_closed=attest,
                          sleep=lambda s: None)
    assert control.calls == []


@pytest.mark.parametrize("seconds,ms", [(60, 60000), (0.5, 500), (3600, 3600000), (600, 600000)])
def test_the_timeout_is_forwarded_to_the_prompt(prog, seconds, ms):
    control = FakeControl()
    wk().perform_wake(prog.plan(), control, predecessor_surface_closed=True,
                      timeout_s=seconds, sleep=lambda s: None)
    assert control.calls[-1][1]["timeout_ms"] == ms


@pytest.mark.parametrize("bad", [0, -1, 3600.001, 3601, float("nan"), float("inf"), True,
                                 "60", None])
def test_a_timeout_outside_zero_to_one_hour_is_refused_before_anything_is_opened(prog, bad):
    control = FakeControl()
    with pytest.raises(wk().WakeUsage, match="timeout"):
        wk().perform_wake(prog.plan(), control, predecessor_surface_closed=True,
                          timeout_s=bad, sleep=lambda s: None)
    assert control.calls == []


def test_the_module_spells_no_herdr_command_and_calls_nothing_that_writes_or_closes():
    """AST: the one process call is `subprocess.run` in `herdr_transport` (the Herdr
    command line itself is built by the adapter)."""
    tree = ast.parse((SRC / "wake.py").read_text(encoding="utf-8"))
    attrs = {"close_pane", "close_tab", "close_workspace", "write_text", "write_bytes",
             "mkdir", "makedirs", "unlink", "rename", "replace", "remove", "rmdir", "truncate",
             "symlink", "link", "chmod", "touch", "system", "Popen", "run", "durable_write",
             "durable_create", "durable_append", "fsync", "expanduser"}
    found, spelled, imported, runs = [], [], [], []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.Module))]:
        owner = fn.name if isinstance(fn, ast.FunctionDef) else "<module>"
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr == "run" \
                        and isinstance(f.value, ast.Name) and f.value.id == "subprocess":
                    runs.append(owner)
                elif isinstance(f, ast.Attribute) and f.attr in attrs:
                    found.append(f.attr)
                if isinstance(f, ast.Name) and f.id in {"open", "eval", "exec", "compile",
                                                        "__import__", "getattr", "setattr"}:
                    found.append(f.id)
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant) \
                and node.elts[0].value == "herdr":
            spelled.append(node.lineno)
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported += [a.name for a in node.names]
    assert sorted(set(found)) == [] and spelled == []
    # `herdr_transport` is the one place; nested walks list it from the module as well
    assert set(runs) == {"herdr_transport", "<module>"} and runs.count("herdr_transport") == 1
    for forbidden in ("shutil", "tempfile", "dispatchers"):
        assert forbidden not in imported, forbidden


# --------------------------------------------------------------------------- #
# stale by construction: what the wake does not hand out, and what it cannot stop
# --------------------------------------------------------------------------- #


def test_the_woken_predecessors_own_credentials_are_refused_and_the_wake_grants_it_nothing(
        prog, monkeypatch, capsys):
    """The wake is performed (fake control), then the predecessor -- which still holds
    the credentials of its own epoch in its memory -- tries every act it could through
    the product's handles and commands. Each is refused by code that exists without
    this module; the whole project root (bytes, sizes, mtimes, kinds) is unchanged by
    the wake itself, a renewal of the lease or a write of any file would show."""
    control = FakeControl()
    monkeypatch.setattr(wk(), "make_control", lambda: control)
    b_token = prog.token_file("orch-b")
    c_token = prog.token_file("orch-c")
    before = prog.snapshot()
    chain_before = [(e.epoch, e.master_id) for e in EpochStore(prog.dir).chain()]
    rc = cli.main(["--root", str(prog.root), "project", "wake", "p", "--session", "orch-c",
                   "--epoch", "3", "--token-file", str(c_token), "--resume-id", RID,
                   "--brief", prog.brief(), "--answer", prog.answer(), "--execute",
                   "--timeout", "30", "--predecessor-surface-closed"])
    assert rc == 0, capsys.readouterr()
    assert control.verbs() == ["open_surface", "start", "prompt"]
    assert prog.snapshot() == before
    assert [(e.epoch, e.master_id) for e in EpochStore(prog.dir).chain()] == chain_before
    # the predecessor, as itself, with its own old token
    b = prog.authority("orch-b")
    b.present(epoch=2, token=prog.masters["orch-b"].token)
    assert b.problems()
    state = prog.store.read_state()
    for act in (b.check, b.renew, lambda: b.handoff("orch-x", reason="late"),
                lambda: b.release(reason="late"), lambda: b.stamp(state)):
        with pytest.raises(StaleMaster):
            act()
    # and a write that carries its epoch, once the master has written at its own
    c_state = prog.store.read_state()
    prog.masters["orch-c"].stamp(c_state)                       # the master writes at epoch 3
    prog.store.write_state(c_state)
    state = prog.store.read_state()
    state.master_epoch = 2
    with pytest.raises(StaleWrite):
        prog.store.write_state(state)
    # the command line says the same
    rc = cli.main(["--root", str(prog.root), "project", "handoff", "p", "--session", "orch-b",
                   "--epoch", "2", "--token-file", str(b_token), "--to", "orch-x",
                   "--reason", "late"])
    assert rc == 5
    assert "stale master" in capsys.readouterr().err
    # the current master is untouched
    c = prog.authority("orch-c")
    c.present(epoch=3, token=prog.masters["orch-c"].token)
    assert c.problems() == []


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #


def wake_argv(prog, *extra, session="orch-c", epoch=3, token=None):
    token = token if token is not None else prog.token_file(session)
    return ["--root", str(prog.root), "project", "wake", "p", "--session", session,
            "--epoch", str(epoch), "--token-file", str(token), "--answer", prog.answer(), *extra]


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def no_herdr(monkeypatch):
    def boom():
        raise AssertionError("a plan must not reach for Herdr")
    monkeypatch.setattr(wk(), "make_control", boom)


def test_the_command_prints_the_plan_and_executes_nothing_by_default(prog, no_herdr):
    before = prog.snapshot()
    rc, out, err = run_cli(wake_argv(prog, "--resume-id", RID, "--brief", prog.brief()))
    assert rc == 0, err
    doc = json.loads(out)
    assert doc["executed"] is False and doc["schema"] == "hoh-wake-plan/1"
    assert doc["succession"]["predecessor_session"] == "orch-b"
    assert doc["agent_args"] == ["--resume", RID] and doc["label"].endswith("(keep)")
    assert doc["answer_file"] == prog.answer() and doc["brief_file"] == prog.brief()
    assert prog.snapshot() == before


def test_the_equals_spelling_of_every_option_is_the_same_command(prog, no_herdr):
    token = prog.token_file("orch-c")
    rc, out, err = run_cli(["--root", str(prog.root), "project", "wake", "p",
                            "--session=orch-c", "--epoch=3", f"--token-file={token}",
                            f"--resume-id={RID}", f"--brief={prog.brief()}",
                            f"--answer={prog.answer()}", "--timeout=30"])
    assert rc == 0, err
    assert json.loads(out)["agent_args"] == ["--resume", RID]


def test_the_command_refuses_anyone_but_the_live_master_with_exit_5(prog, no_herdr):
    base = ["--resume-id", RID, "--brief", prog.brief()]
    b_token = prog.token_file("orch-b")
    cases = [
        wake_argv(prog, *base, session="orch-b", epoch=2, token=b_token),   # the predecessor
        wake_argv(prog, *base, session="orch-b", epoch=3, token=b_token),   # claims C's epoch
        wake_argv(prog, *base, session="orch-c", epoch=3, token=b_token),   # a token not its own
        wake_argv(prog, *base, session="orch-c", epoch=2),                   # an epoch not its own
        ["--root", str(prog.root), "project", "wake", "p", "--session", "orch-c", "--epoch",
         "3", "--answer", prog.answer(), *base],                              # no token at all
    ]
    for argv in cases:
        rc, out, err = run_cli(argv)
        assert (rc, out) == (5, ""), (argv, err)
        assert err.startswith("refused:")


def test_execute_without_the_statement_about_the_predecessors_surface_is_exit_2(prog, no_herdr):
    rc, out, err = run_cli(wake_argv(prog, "--resume-id", RID, "--brief", prog.brief(),
                                     "--execute"))
    assert (rc, out) == (2, "") and "--predecessor-surface-closed" in err
    rc, out, err = run_cli(wake_argv(prog, "--resume-id", RID, "--brief", prog.brief()))
    assert rc == 0 and "--predecessor-surface-closed" in json.loads(out)["precondition"]


def test_the_command_says_two_for_values_and_five_for_records(prog, no_herdr):
    rc, out, err = run_cli(wake_argv(prog, "--resume-id", "nope", "--brief", prog.brief()))
    assert (rc, out) == (2, "") and "resume-id" in err
    rc, out, err = run_cli(wake_argv(prog, "--resume-id", RID, "--brief", "rel.md"))
    assert (rc, out) == (2, "") and "--brief" in err
    rc, out, err = run_cli(wake_argv(prog, "--resume-id", RID, "--brief", prog.brief(),
                                     "--timeout", "0"))
    assert (rc, out) == (2, "") and "timeout" in err                # also in a dry run
    os.mkfifo(prog.dir / "rollover" / "rc-zz.receipt.json")
    with alarm():
        rc, out, err = run_cli(wake_argv(prog, "--resume-id", RID, "--brief", prog.brief()))
    assert (rc, out) == (5, "") and "rc-zz.receipt.json" in err


def test_the_command_with_execute_goes_through_the_adapter_and_forwards_its_values(
        prog, monkeypatch):
    control = FakeControl()
    monkeypatch.setattr(wk(), "make_control", lambda: control)
    monkeypatch.setattr(wk().time, "sleep", lambda s: None)
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w-ops")
    rc, out, err = run_cli(wake_argv(prog, "--resume-id", RID, "--brief", prog.brief(),
                                     "--execute", "--predecessor-surface-closed",
                                     "--timeout", "60"))
    assert rc == 0, err
    doc = json.loads(out)
    assert doc["executed"] is True and doc["outcome"] == "WOKEN"
    assert (doc["pane_id"], doc["tab_id"]) == ("w1:p9", "w1:t9")
    assert control.verbs() == ["open_surface", "start", "prompt"]
    assert control.calls[0][1]["workspace"] == "w-ops"
    assert control.calls[2][1]["timeout_ms"] == 60000               # not the default 600 s
    assert control.calls[1][1]["args"] == ("--resume", RID)


def test_the_command_reports_a_failed_wake_with_the_surface_it_left_open(prog, monkeypatch):
    control = FakeControl(prompt_error=DispatchError("prompt failed: stalled"))
    monkeypatch.setattr(wk(), "make_control", lambda: control)
    monkeypatch.setattr(wk().time, "sleep", lambda s: None)
    rc, out, err = run_cli(wake_argv(prog, "--resume-id", RID, "--brief", prog.brief(),
                                     "--execute", "--predecessor-surface-closed"))
    assert rc == 5
    doc = json.loads(out)
    assert doc["outcome"] == "FAILED" and doc["agent_started"] is True
    assert (doc["pane_id"], doc["tab_id"]) == ("w1:p9", "w1:t9")
    assert "left open" in err
    assert control.verbs() == ["open_surface", "start", "prompt"]


def test_execute_outside_herdr_is_exit_2_and_a_plan_still_works(prog, monkeypatch):
    def unavailable():
        raise HerdrUnavailable("HERDR_ENV != 1")
    monkeypatch.setattr(wk(), "make_control", unavailable)
    rc, out, err = run_cli(wake_argv(prog, "--resume-id", RID, "--brief", prog.brief(),
                                     "--execute", "--predecessor-surface-closed"))
    assert (rc, out) == (2, "") and "HERDR_ENV" in err
    rc, out, err = run_cli(wake_argv(prog, "--resume-id", RID, "--brief", prog.brief()))
    assert rc == 0 and json.loads(out)["executed"] is False


def test_the_parser_requires_the_values_the_wake_cannot_infer(prog, capsys):
    base = ["--root", str(prog.root), "project", "wake", "p", "--session", "orch-c",
            "--epoch", "3"]
    full = ["--resume-id", RID, "--brief", prog.brief(), "--answer", prog.answer()]
    for argv in (base, base + ["--resume-id", RID], base + ["--brief", prog.brief()],
                 base + ["--answer", prog.answer()],
                 base + full[:4], base + full[2:], base + [full[0], full[1], *full[4:]]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 2
    capsys.readouterr()
    assert wk().KIND == "claude"


# --------------------------------------------------------------------------- #
# the transport: a turn is waited for as long as Herdr is told to wait (review A, B1)
# --------------------------------------------------------------------------- #


class FakeHerdrProcess:
    """Stands in for `subprocess.run` of every Herdr command. Answers like Herdr, takes
    `durations[(verb, noun)]` simulated seconds, and raises `TimeoutExpired` when the
    caller's deadline is shorter -- the way the real call would. Records
    `(argv, timeout)`. Any command it does not know fails the test."""

    def __init__(self, durations):
        self.durations = durations
        self.calls = []

    def __call__(self, argv, capture_output=True, text=True, timeout=None, **kw):
        argv = list(argv)
        key = (argv[1], argv[2])
        self.calls.append((argv, timeout))
        took = self.durations.get(key, 0.1)
        if timeout is not None and took > timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        if key == ("tab", "create"):
            out = {"result": {"root_pane": {"pane_id": "w1:p5"}, "tab": {"tab_id": "w1:t5"}}}
        elif key in (("agent", "start"), ("agent", "prompt")):
            out = {"result": {"agent": {"name": argv[3], "agent_status": "idle",
                                        "pane_id": "w1:p5", "tab_id": "w1:t5",
                                        "workspace_id": "w1", "cwd": "/"}}}
        else:
            raise AssertionError(f"unexpected herdr command {argv[:3]}")
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out), stderr="")


def wake_through_the_real_control(prog, monkeypatch, fake, *extra):
    """`hoh project wake --execute` with the product's own control and transport; only
    the process call is the fake."""
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/herdr")
    monkeypatch.setattr(wk().time, "sleep", lambda s: None)
    return run_cli(wake_argv(prog, "--resume-id", RID, "--brief", prog.brief(), "--execute",
                             "--predecessor-surface-closed", *extra))


def test_a_slow_agent_start_is_not_a_failed_wake(prog, monkeypatch):
    """`agent start` takes 45 s (it waits for the agent; Herdr's own `--timeout 240000`)."""
    fake = FakeHerdrProcess({("agent", "start"): 45.0, ("agent", "prompt"): 5.0})
    rc, out, err = wake_through_the_real_control(prog, monkeypatch, fake)
    assert rc == 0, (out[-400:], err)
    assert json.loads(out)["outcome"] == "WOKEN"
    deadlines = {tuple(a[1:3]): t for a, t in fake.calls}
    assert deadlines[("agent", "start")] >= 240 and deadlines[("tab", "create")] == 30


def test_a_consultation_turn_of_two_minutes_is_not_a_failed_wake(prog, monkeypatch):
    """The predecessor reads the brief and writes the answer for 120 s; `--timeout 600`."""
    fake = FakeHerdrProcess({("agent", "start"): 5.0, ("agent", "prompt"): 120.0})
    rc, out, err = wake_through_the_real_control(prog, monkeypatch, fake, "--timeout", "600")
    assert rc == 0, (out[-400:], err)
    assert json.loads(out)["outcome"] == "WOKEN"
    deadlines = {tuple(a[1:3]): t for a, t in fake.calls}
    assert deadlines[("agent", "prompt")] == 630            # the command's own 600 s + 30 s
    # a turn that outlasts the caller's own --timeout (plus the overhead) is a failed wake
    fake = FakeHerdrProcess({("agent", "start"): 5.0, ("agent", "prompt"): 700.0})
    monkeypatch.setattr(subprocess, "run", fake)
    rc, out, err = run_cli(wake_argv(prog, "--resume-id", RID, "--brief", prog.brief(),
                                     "--execute", "--predecessor-surface-closed",
                                     "--timeout", "600", "--answer", prog.answer("second.md")))
    assert rc == 5 and json.loads(out)["outcome"] == "FAILED"
    assert "did not answer within 630 s" in json.loads(out)["detail"]


@pytest.mark.parametrize("argv,seconds", [
    (["herdr", "tab", "create", "--cwd", "/x", "--label", "l", "--no-focus"], 30),
    (["herdr", "agent", "start", "n", "--kind", "claude", "--pane", "p", "--timeout",
      "240000", "--", "--resume", RID], 270),
    (["herdr", "agent", "prompt", "n", "text", "--wait", "--timeout", "600000"], 630),
    (["herdr", "agent", "prompt", "n", "text", "--wait", "--timeout", "3600000"], 3630),
    (["herdr", "agent", "prompt", "n", "text", "--wait", "--timeout", "7200000"], 3630),
    (["herdr", "agent", "prompt", "n", "text", "--wait", "--timeout", "0"], 30),
    (["herdr", "agent", "prompt", "n", "text", "--wait", "--timeout", "1000"], 31),
    (["herdr", "agent", "prompt", "n", "text", "--wait", "--timeout", "abc"], 30),
    (["herdr", "agent", "prompt", "n", "text", "--wait", "--timeout", "-5"], 30),
    (["herdr", "agent", "prompt", "n", "text", "--wait", "--timeout", "６００"], 30),
    (["herdr", "agent", "prompt", "n", "text", "--wait", "--timeout"], 30),
    (["herdr", "agent", "prompt", "n", "--timeout", "--wait", "--timeout", "1000"], 31),
    (["herdr", "agent", "start", "n", "--", "--timeout", "999999"], 30),
])
def test_the_deadline_of_a_herdr_command_is_its_own_timeout_plus_thirty_seconds(argv, seconds):
    assert wk().transport_deadline_s(argv) == seconds


def test_the_transport_runs_the_command_under_that_deadline_and_fails_like_the_shared_one(
        monkeypatch):
    seen = []

    def make(result=None, error=None):
        def run(argv, **kw):
            seen.append((list(argv), kw.get("timeout"), kw.get("capture_output"), kw.get("text"),
                         kw.get("shell")))
            if error:
                raise error
            return result
        return run

    ok = subprocess.CompletedProcess([], 0, stdout='{"result": {}}', stderr="")
    monkeypatch.setattr(subprocess, "run", make(ok))
    argv = ["herdr", "agent", "prompt", "n", "t", "--wait", "--timeout", "600000"]
    assert wk().herdr_transport(argv, "p") == {"result": {}}
    assert wk().herdr_transport(argv, "p", text=True) == '{"result": {}}'
    assert seen[0] == (argv, 630, True, True, None)           # no shell, 630 s
    for error, transient, text in (
            (subprocess.TimeoutExpired(argv, 630), True, "did not answer within 630 s"),
            (FileNotFoundError("no herdr"), True, "could not be run")):
        monkeypatch.setattr(subprocess, "run", make(error=error))
        with pytest.raises(DispatchError, match=text) as info:
            wk().herdr_transport(argv, "send it")
        assert info.value.transient is transient and "send it" in str(info.value)
    bad = subprocess.CompletedProcess([], 1, stdout="", stderr="no such pane\n")
    monkeypatch.setattr(subprocess, "run", make(bad))
    with pytest.raises(DispatchError, match="exit 1: no such pane") as info:
        wk().herdr_transport(argv, "p")
    assert not info.value.transient
    junk = subprocess.CompletedProcess([], 0, stdout="not json", stderr="")
    monkeypatch.setattr(subprocess, "run", make(junk))
    with pytest.raises(DispatchError, match="returned no JSON"):
        wk().herdr_transport(argv, "p")


def test_the_control_is_only_made_inside_herdr(monkeypatch):
    from hoh.herdr import HerdrUnavailable
    monkeypatch.delenv("HERDR_ENV", raising=False)
    with pytest.raises(HerdrUnavailable):
        wk().make_control()
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/herdr")
    control = wk().make_control()
    assert control.backend == "herdr" and control._run is wk().herdr_transport


# --------------------------------------------------------------------------- #
# what the plan hands out, and what it cannot stop (review A, B2)
# --------------------------------------------------------------------------- #


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def test_the_plan_names_no_path_in_or_beside_the_live_masters_token_directory(prog):
    live = prog.dir / "rollover" / f"{prog.capsules[1]}.cwd"
    token = live / "master.token"
    assert token.read_text() == prog.masters["orch-c"].token         # it is the live one
    plan = prog.plan()
    rollover = os.path.realpath(prog.dir / "rollover")
    # the predecessor's own launch directory: a neighbour of the live one, never it
    assert plan.cwd == f"{rollover}/{prog.capsules[0]}.cwd" and plan.cwd != str(live)
    for path in (plan.brief_file, plan.answer_file):
        real = os.path.realpath(path if os.path.isdir(path) else os.path.dirname(path))
        assert real != rollover and not real.startswith(rollover + os.sep)
    everything = "\n".join(_strings(plan.as_dict()))
    assert "master.token" not in everything and str(live) not in everything
    assert prog.masters["orch-c"].token not in everything


@pytest.mark.parametrize("where", ["brief", "answer", "brief-via-link", "answer-via-link"])
def test_a_brief_or_an_answer_in_the_rollover_directory_is_refused(prog, tmp_path, where):
    rollover = prog.dir / "rollover"
    link = tmp_path / "linked"
    link.symlink_to(rollover)
    brief_in = rollover / "brief.md"
    brief_in.write_text("question\n")
    kw = {
        "brief": {"brief": str(brief_in)},
        "answer": {"answer": str(rollover / "answer.md")},
        "brief-via-link": {"brief": str(link / "brief.md")},
        "answer-via-link": {"answer": str(link / "answer.md")},
    }[where]
    with pytest.raises(wk().WakeUsage, match="rollover directory"):
        prog.plan(**kw)


def test_an_explicit_directory_in_the_rollover_directory_is_refused(tmp_path):
    p = Program(tmp_path)
    p.first("orch-a")
    p.hand_over("orch-a", "orch-b")
    for bad in (p.dir / "rollover", p.dir / "rollover" / f"{p.capsules[0]}.cwd"):
        with pytest.raises(wk().WakeUsage, match="rollover directory"):
            p.plan("orch-b", cwd=str(bad))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert p.plan("orch-b", cwd=str(elsewhere)).cwd == os.path.realpath(elsewhere)


def test_the_label_and_the_prompt_claim_no_more_than_the_tests_show(prog):
    plan = prog.plan()
    low = (plan.label + "\n" + plan.prompt).lower()
    for word in ("fenced", "isolat", "sandbox", "safe", "secure", "cannot"):
        assert word not in low, word
    assert plan.label == "VH ▸ predecessor orch-b (consult only) (keep)"
    for phrase in ("You are not the master", "read-and-answer consultation only",
                   "do not merge, tag or release anything",
                   "do not read or use any token or credential file of the current master",
                   "Write your answer to exactly one file, which must not exist yet"):
        assert phrase in plan.prompt, phrase
    # it names the two files and the people, and nothing else of the programme
    assert plan.brief_file in plan.prompt and plan.answer_file in plan.prompt
    assert str(prog.dir) not in plan.prompt


def test_the_live_masters_token_file_is_readable_by_the_same_user_so_the_fence_is_not_isolation(
        prog):
    """A pin of a limit of the product, measured: the file the pointer's `--token-out`
    names is mode 0600 of the same OS user the woken predecessor runs as, and a read of
    it is the master (`present`). The wake does not change that and says so (L11)."""
    token = prog.dir / "rollover" / f"{prog.capsules[1]}.cwd" / "master.token"
    assert (token.stat().st_mode & 0o777) == 0o600 and token.stat().st_uid == os.getuid()
    thief = MasterAuthority(prog.dir, "orch-c")
    thief.present(epoch=3, token=token.read_text())
    assert thief.problems() == []
    assert os.path.dirname(prog.plan().cwd) == os.path.dirname(os.path.dirname(str(token)))


def test_the_stores_fence_binds_only_after_the_master_wrote_at_its_epoch(prog):
    """A pin of a limit of the product, measured (L12): a rollover writes no state, so
    until the master has written at its own epoch the disk carries epoch 0 and a raw
    write at the old epoch is accepted. The product's own paths are refused at once."""
    b = prog.authority("orch-b")
    b.present(epoch=2, token=prog.masters["orch-b"].token)
    with pytest.raises(StaleMaster):
        b.stamp(prog.store.read_state())
    old = prog.store.read_state()
    old.master_epoch = 2
    prog.store.write_state(old)                                  # accepted: disk is at 0
    fresh = prog.store.read_state()
    prog.masters["orch-c"].stamp(fresh)
    prog.store.write_state(fresh)                                # the master writes at 3
    behind = prog.store.read_state()
    behind.master_epoch = 2
    with pytest.raises(StaleWrite):
        prog.store.write_state(behind)                           # now it is refused


def test_a_tilde_is_not_expanded_in_any_path(prog, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "b.md").write_text("q")
    for kw in ({"brief": "~/b.md"}, {"answer": "~/new.md"}):
        with pytest.raises(wk().WakeUsage, match="no '~'"):
            prog.plan(**kw)


# --------------------------------------------------------------------------- #
# the records: an odd session id of an unrelated receipt, and the retry hazard
# --------------------------------------------------------------------------- #


def test_a_session_id_the_product_accepts_but_a_label_cannot_hold_blocks_only_its_own_plan(
        tmp_path):
    p = Program(tmp_path)
    p.first("orch-a")
    p.hand_over("orch-a", "orch b")                  # a space: the product takes any non-empty id
    p.hand_over("orch b", "orch-c")
    with pytest.raises(wk().WakeRefused, match="not a plain id"):
        p.plan("orch-c")                             # its own predecessor is "orch b"
    p.hand_over("orch-c", "orch-d")
    plan = p.plan("orch-d")                          # the odd receipt is no one's business now
    assert plan.succession.predecessor_session == "orch-c"
    assert plan.cwd.endswith(f"/{p.capsules[1]}.cwd")


def test_a_failed_wake_says_whether_running_it_again_is_safe(prog, monkeypatch):
    control = FakeControl(prompt_error=DispatchError("prompt failed: stalled"))
    monkeypatch.setattr(wk(), "make_control", lambda: control)
    monkeypatch.setattr(wk().time, "sleep", lambda s: None)
    rc, out, err = run_cli(wake_argv(prog, "--resume-id", RID, "--brief", prog.brief(),
                                     "--execute", "--predecessor-surface-closed"))
    doc = json.loads(out)
    assert rc == 5 and doc["agent_started"] is True
    assert "do NOT run this command again" in doc["hint"] and "message that surface" in doc["hint"]
    assert doc["answer_file"] == prog.answer()
    control = FakeControl(open_error=DispatchError("tab create failed"))
    monkeypatch.setattr(wk(), "make_control", lambda: control)
    rc, out, err = run_cli(wake_argv(prog, "--resume-id", RID, "--brief", prog.brief(),
                                     "--execute", "--predecessor-surface-closed"))
    doc = json.loads(out)
    assert rc == 5 and doc["agent_started"] is False and doc["pane_id"] is None
    assert "no agent was started" in doc["hint"] and "may be run again" in doc["hint"]
