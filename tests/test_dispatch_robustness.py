"""A dispatch survives a stalled prompt and a role that ends its turn early.

Three faults cost iterations, or whole runs, to the dispatcher rather than to
the work:

* **A stalled prompt is not a decision.** Herdr's `agent_prompt_stalled` (no
  state change within 5 s) was a non-transient `DispatchError`, so the run
  blocked (`20260925t1801z-pqagate2`). It is now transient, retried after a
  30 s back-off that waits only after the witness is re-baselined.
* **Idle is not done.** QA started its suite in the background and ended its
  turn; `herdr agent prompt --wait` returned on the idle pane and the run
  booked an outage (pqagate, ptimer, sdurw3, spark). The dispatcher now raises
  `AnswerMissing`, and the controller issues at most one charged, witnessed
  follow-up -- after a status read, and after a passive wait while the agent
  is `working`.
* **Carried-over work is not disclosed.** The QA prompt asks for it, and the
  planner and QA prompts ask the role not to end its turn on background work.

No agent is started and Herdr is never called: `subprocess.run` inside
`hoh.dispatchers` is a fake that answers `agent prompt`, `agent get` and
`agent read` (an empty input box unless a test says otherwise), the
dispatcher is built with `__new__` (never `require_herdr`), its clock is a
fake (no test waits a real second), and every file is under `tmp_path`.

Every test names, in its docstring, the failure it catches, and carries a
negative control that changes only the condition under test.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import types
from pathlib import Path

import pytest

from hoh import controller, dispatchers, roles
from hoh.capability import CapabilityViolation
from hoh.contracts import Budgets, DevelopmentPlan, Role
from hoh.controller import AnswerMissing, Controller, DispatchError, WaitingForApproval
from hoh.dispatchers import (
    ARGV_ELEMENT_LIMIT,
    STALL_RETRY_AFTER_S,
    HerdrDispatcher,
    _answer_instruction,
    _Base,
    _pointer_prompt,
)

#: Looked up, not imported, so this file still collects on a tree without it
#: (the pin's baseline): there, every test that expects it fails instead of
#: the whole file erroring. The stand-in is never raised by anything. The
#: other new names (`_pending_input`, `_first_line_guard`, ...) are read as
#: `dispatchers.<name>` inside the tests for the same reason.
PaneNotClean = getattr(controller, "PaneNotClean",
                       type("PaneNotCleanMissing", (Exception,), {}))
from hoh.evidence import EvidenceBundle
from hoh.store import RunStore

TARGET = "w0:p1"
PANE = "PANE-SENTINEL"
ANSWER = '{"ok": true}'
LONG_SEED = "LONG-PROMPT-SEED-7f3a"
#: Far above what the role timeout allows at the fake clock's pace (about 36).
MAX_STATUS_READS = 500


def _reply(status: str = "idle") -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
        {"result": {"agent": {"pane_id": TARGET, "workspace_id": "w0",
                              "tab_id": "w0:t1", "agent": "claude",
                              "agent_status": status, "cwd": "/x"}}}))


def _screen(view: str) -> types.SimpleNamespace:
    """`herdr agent read`: the screen as text on stdout, not JSON."""
    return types.SimpleNamespace(returncode=0, stderr="", stdout=view)


#: Claude Code's input box, framed by two rules (the pane view of run padopt at
#: 01:17:47Z, shortened).
RULE = "\u2500" * 88
EMPTY_BOX = (f"\u23fa Done.\n\u273b Baked for 2m 3s\n{RULE}\n\u276f \n{RULE}\n"
             "  \u23f5\u23f5 auto mode on (shift+tab to cycle)\n")
#: The unsent pointer of padopt i1, still in the box. Its tail lies far beyond
#: the first 120 characters, so a record cut to 120 characters loses it.
RESIDUE_TAIL = "RESIDUE-TAIL-91c2"
RESIDUE_LINES = [
    "[prompt-file]",
    "  Your instructions for this turn are too long for one command-line argument,",
    "  so they were written to a file. That file **is** your prompt; this message",
    f"  only points to it. {RESIDUE_TAIL}",
]
RESIDUE = "\n".join(RESIDUE_LINES)
RESIDUE_PANE = (f"\u273b Baked for 0s\n{RULE}\n\u276f {RESIDUE_LINES[0]}\n"
                + "\n".join(RESIDUE_LINES[1:])
                + f"\n{RULE}\n  \u23f5\u23f5 auto mode on (shift+tab to cycle)\n")
#: Another agent kind's screen: no Claude input box at all.
OTHER_SCREEN = "kimi> working on it\n  [esc] cancel   [tab] mode\n> \n"
#: Claude Code's placeholder in an empty box: indistinguishable from text.
PLACEHOLDER_PANE = (f"{RULE}\n\u276f Try \"refactor the dispatcher\"\n{RULE}\n"
                    "  ? for shortcuts\n")


def _error(code: str) -> types.SimpleNamespace:
    """A failed Herdr call, in the form Herdr writes it (run evidence of
    `20260925T1801Z`, history 18:03:33Z)."""
    return types.SimpleNamespace(returncode=1, stdout="", stderr=json.dumps(
        {"error": {"code": code, "message": "agent prompt produced no observed "
                   "state change within 5000 ms"}, "id": "cli:agent:prompt"}))


class FakeHerdr:
    """Stands in for `subprocess.run` inside `hoh.dispatchers`.

    `on_prompt(n)`, `on_get(n)` and `on_read(n)` are called with the 1-based
    number of the call of that kind; they may act (write a file, as a role
    would) and return a reply, or None for an idle success. `on_read` may
    return the screen as a string; its default is an empty input box.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.on_prompt = None
        self.on_get = None
        self.on_read = None

    def kind(self, kind: str) -> list[list[str]]:
        return [c for c in self.calls if c[1:3] == ["agent", kind]]

    @property
    def prompts(self) -> list[list[str]]:
        return self.kind("prompt")

    @property
    def gets(self) -> list[list[str]]:
        return self.kind("get")

    @property
    def reads(self) -> list[list[str]]:
        return [c for c in self.calls if c[1:3] in (["agent", "read"], ["pane", "read"])]

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if len(self.gets) > MAX_STATUS_READS:
            # An unbounded passive wait fails here instead of hanging.
            pytest.fail(f"more than {MAX_STATUS_READS} status reads: the passive "
                        "wait ignored the role timeout")
        reply = None
        if args[1:3] == ["agent", "prompt"] and self.on_prompt is not None:
            reply = self.on_prompt(len(self.prompts))
        elif args[1:3] == ["agent", "get"] and self.on_get is not None:
            reply = self.on_get(len(self.gets))
        elif args[1:3] in (["agent", "read"], ["pane", "read"]):
            if self.on_read is not None:
                reply = self.on_read(len(self.reads))
            if reply is None or isinstance(reply, str):
                reply = _screen(EMPTY_BOX if reply is None else reply)
        if isinstance(reply, BaseException):
            raise reply
        return reply or _reply("idle")


class Clock:
    """The dispatcher's `time`: every `monotonic()` advances by `step`
    seconds, `sleep` only records. `time()` is fixed ten seconds in the past,
    so a dispatch marker's `dispatched_at` is safely before any file the test
    writes (a file's mtime comes from the kernel's coarse clock)."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step
        self.slept: list[float] = []
        self.fixed = time.time() - 10.0

    def monotonic(self) -> float:
        self.now += self.step
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def time(self) -> float:
        return self.fixed


class Rig:
    """A run store, a controller and a Herdr-free `HerdrDispatcher`."""

    def __init__(self, root: Path, monkeypatch, *, max_dispatches: int = 5,
                 timeout_ms: int = 1000, step: float = 1000.0) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "repo").mkdir()
        (root / "spec.md").write_text("# s\n", encoding="utf-8")
        self.store = RunStore(root / "runs", "r1")
        self.state = Controller.new_state(
            run_id="r1", repo_path=root / "repo", project_name="p",
            spec_path=root / "spec.md", budgets=Budgets(max_dispatches=max_dispatches))
        self.store.write_state(self.state)
        d = HerdrDispatcher.__new__(HerdrDispatcher)
        _Base.__init__(d, answers_dir=self.store.dir / "answers")
        d.cwd = str(root / "repo")
        d.timeout_ms = timeout_ms
        self.pane_calls = 0

        def pane_view(target, lines=40):
            self.pane_calls += 1
            if self.on_pane is not None:
                self.on_pane()
            return PANE

        self.on_pane = None
        monkeypatch.setattr(d, "_ensure_agent", lambda role, state: TARGET)
        monkeypatch.setattr(d, "_pane_view", pane_view)
        self.herdr = FakeHerdr()
        monkeypatch.setattr(dispatchers, "subprocess", types.SimpleNamespace(
            run=self.herdr, TimeoutExpired=subprocess.TimeoutExpired))
        self.clock = Clock(step)
        monkeypatch.setattr(dispatchers, "time", self.clock)
        # The controller's back-off sleeps through the real `time` module.
        self.backoff: list[float] = []
        monkeypatch.setattr(time, "sleep", self.backoff.append)
        self.d = d
        self.answer = d._answer_path(Role.QA, self.state)
        self.controller = Controller(self.store, d, spec_path=root / "spec.md")

    def write_answer(self, text: str = ANSWER) -> None:
        self.answer.write_text(text, encoding="utf-8")

    def touch_state(self) -> None:
        """A role's write into the run's protected `state.json`."""
        path = self.store.state_path
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")

    def full(self, prompt: str = "p") -> str:
        return prompt + _answer_instruction(self.answer)

    def prompt_file(self) -> Path:
        return self.answer.with_name(self.answer.name[:-len(".json")] + ".prompt.md")

    def tamper(self) -> None:
        with self.prompt_file().open("a", encoding="utf-8") as fh:
            fh.write("tampered")

    def run(self, role: Role = Role.QA, prompt: str = "p"):
        """The controller's dispatch: (result, None) or (None, exception)."""
        try:
            return self.controller._dispatch(role, prompt, self.state), None
        except Exception as exc:          # noqa: BLE001 - the test inspects it
            return None, exc

    def dispatch(self, role: Role = Role.QA, prompt: str = "p"):
        """The dispatcher alone, without the controller."""
        try:
            return self.d.dispatch(role, prompt, state=self.state), None
        except Exception as exc:          # noqa: BLE001 - the test inspects it
            return None, exc

    def notes(self) -> str:
        return "\n".join(self.state.history)


def _cli_error(monkeypatch, reply) -> DispatchError:
    """What `_cli` raises for one failed `herdr agent prompt`."""
    monkeypatch.setattr(dispatchers, "subprocess", types.SimpleNamespace(
        run=lambda *a, **k: reply, TimeoutExpired=subprocess.TimeoutExpired))
    x = object.__new__(HerdrDispatcher)
    with pytest.raises(DispatchError) as info:
        x._cli(["herdr", "agent", "prompt", TARGET, "p", "--wait", "--timeout", "1000"],
               "qa dispatch")
    return info.value


# --------------------------------------------------------------------------- #
# K1 -- a stalled prompt is transient and carries the pane view
# --------------------------------------------------------------------------- #


def test_stalled_prompt_is_transient_in_cli(monkeypatch):
    """Catches: `agent_prompt_stalled` raised as a non-transient error, which
    blocked run pqagate2 for the operator to resume. Control: another Herdr
    code in the same JSON form stays non-transient, with no back-off."""
    stalled = _cli_error(monkeypatch, _error("agent_prompt_stalled"))
    assert stalled.transient is True
    assert stalled.retry_after >= 30

    other = _cli_error(monkeypatch, _error("agent_not_found"))
    assert other.transient is False
    assert other.retry_after == 0


def test_stalled_prompt_is_transient_through_dispatch_with_pane_view(tmp_path, monkeypatch):
    """Catches: the re-raise that attaches the pane view building a new error
    that drops `transient` or `retry_after`. Control: another code through
    the same path is non-transient and carries the pane view too."""
    rig = Rig(tmp_path / "stall", monkeypatch)
    rig.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
    _, exc = rig.dispatch()
    assert type(exc) is DispatchError
    assert exc.transient is True and exc.retry_after >= 30
    assert PANE in str(exc)

    control = Rig(tmp_path / "control", monkeypatch)
    control.herdr.on_prompt = lambda n: _error("agent_not_found")
    _, exc = control.dispatch()
    assert type(exc) is DispatchError
    assert exc.transient is False and exc.retry_after == 0
    assert PANE in str(exc)


def test_stalled_prompt_is_transient_unless_tampered(tmp_path, monkeypatch):
    """Catches: a stall winning over a tampered prompt file, so a prompt that
    is no longer what was asked is retried as transient (long-prompt L2).
    Control: the same stall with the prompt file intact is transient."""
    long_prompt = "x" * 70000

    rig = Rig(tmp_path / "tampered", monkeypatch)

    def tamper_and_stall(n):
        rig.tamper()
        return _error("agent_prompt_stalled")

    rig.herdr.on_prompt = tamper_and_stall
    _, exc = rig.dispatch(prompt=long_prompt)
    assert type(exc) is DispatchError
    assert exc.transient is False
    assert rig.prompt_file().name in str(exc)

    control = Rig(tmp_path / "control", monkeypatch)
    control.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
    _, exc = control.dispatch(prompt=long_prompt)
    assert exc.transient is True


def test_stalled_prompt_is_transient_retried_after_backoff(tmp_path, monkeypatch):
    """Catches: a stall retried at once (Revision 2: at least 30 s later), or
    the controller ignoring `retry_after`. Control: a transient error that is
    not a stall (a wrapper timeout) is retried without any wait."""
    rig = Rig(tmp_path / "stall", monkeypatch)

    def stall_then_answer(n):
        if n == 1:
            return _error("agent_prompt_stalled")
        rig.write_answer()
        return None

    rig.herdr.on_prompt = stall_then_answer
    result, exc = rig.run()
    assert exc is None and result == ANSWER
    assert len(rig.herdr.prompts) == 2
    assert rig.state.usage.transient_retries == 1
    assert rig.backoff and max(rig.backoff) >= STALL_RETRY_AFTER_S >= 30

    control = Rig(tmp_path / "control", monkeypatch)

    def timeout_then_answer(n):
        if n == 1:
            return subprocess.TimeoutExpired(["herdr"], 1)
        control.write_answer()
        return None

    control.herdr.on_prompt = timeout_then_answer
    result, exc = control.run()
    assert exc is None and result == ANSWER
    assert control.state.usage.transient_retries == 1
    assert control.backoff == []


def test_stalled_prompt_is_transient_backoff_waits_after_rebaseline(tmp_path, monkeypatch):
    """Catches: the back-off waiting before `witness_again`, which folds a
    role's write to `state.json` during those 30 s into the new baseline so
    it is never recorded (draft v1's order). Control: the same stall and
    back-off without the write succeeds."""
    rig = Rig(tmp_path / "write", monkeypatch)
    monkeypatch.setattr(time, "sleep", lambda s: rig.touch_state() if s >= 30 else None)

    def stall_then_answer(n):
        if n == 1:
            return _error("agent_prompt_stalled")
        rig.write_answer()
        return None

    rig.herdr.on_prompt = stall_then_answer
    _, exc = rig.run()
    assert isinstance(exc, CapabilityViolation)
    assert "state.json" in str(exc)
    assert len(rig.herdr.prompts) == 2

    control = Rig(tmp_path / "control", monkeypatch)

    def stall_then_answer_(n):
        if n == 1:
            return _error("agent_prompt_stalled")
        control.write_answer()
        return None

    control.herdr.on_prompt = stall_then_answer_
    result, exc = control.run()
    assert exc is None and result == ANSWER
    assert control.backoff and max(control.backoff) >= 30


# --------------------------------------------------------------------------- #
# K3 -- other failures keep their classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("reply, why", [
    (_error("agent_not_found"), "another Herdr code"),
    (types.SimpleNamespace(returncode=1, stdout="",
                           stderr="agent_prompt_stalled: no observed state change"),
     "the code in prose, not in the JSON error"),
    (types.SimpleNamespace(returncode=1, stdout="", stderr="{not json"), "unparseable stderr"),
    (types.SimpleNamespace(returncode=2, stdout="", stderr=""), "an empty stderr"),
])
def test_other_herdr_errors_unchanged(monkeypatch, reply, why):
    """Catches: the stall match widened -- to the prose of a message, to any
    error code, or to unparseable stderr -- so a real failure retries.
    Control: the observed stall form is the one that becomes transient."""
    exc = _cli_error(monkeypatch, reply)
    assert exc.transient is False, why
    assert exc.retry_after == 0, why
    assert _cli_error(monkeypatch, _error("agent_prompt_stalled")).transient is True


@pytest.mark.parametrize("raised, transient", [
    (OSError(5, "I/O error"), True),
    (subprocess.TimeoutExpired(["herdr"], 1), True),
    (OSError(7, "Argument list too long"), False),
])
def test_retry_classification_without_backoff_unchanged(monkeypatch, raised, transient):
    """Catches: a back-off given to transient errors other than the stall
    (the existing transient tests would start to wait), or E2BIG turned
    transient (long-prompt L6). Control: the stall carries the back-off."""
    def run(*a, **k):
        raise raised

    monkeypatch.setattr(dispatchers, "subprocess", types.SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired))
    x = object.__new__(HerdrDispatcher)
    with pytest.raises(DispatchError) as info:
        x._cli(["herdr", "agent", "prompt", TARGET, "p"], "qa dispatch")
    assert info.value.transient is transient
    assert info.value.retry_after == 0
    assert _cli_error(monkeypatch, _error("agent_prompt_stalled")).retry_after >= 30


def test_developer_dispatch_unchanged(tmp_path, monkeypatch):
    """Catches: an unstructured role (the developer answers through the
    artifact, not a file) raising `AnswerMissing` or getting a follow-up or
    a status read. Control: QA under the same fakes, without an answer, is
    followed up."""
    rig = Rig(tmp_path / "developer", monkeypatch)
    result, exc = rig.dispatch(role=Role.DEVELOPER)
    assert exc is None
    assert result.startswith("developer run finished")
    assert len(rig.herdr.prompts) == 1 and rig.herdr.gets == []

    control = Rig(tmp_path / "control", monkeypatch)
    _, exc = control.run(role=Role.QA)
    assert isinstance(exc, AnswerMissing) and exc.followed_up is True
    assert len(control.herdr.prompts) == 2


def test_blocked_pane_unchanged(tmp_path, monkeypatch):
    """Catches: a follow-up (or a status read) into an approval dialog: a
    first turn that ends `blocked` without an answer keeps today's block
    path. Control: the same turn ending `idle` is followed up."""
    rig = Rig(tmp_path / "blocked", monkeypatch)
    rig.herdr.on_prompt = lambda n: _reply("blocked")
    _, exc = rig.run()
    assert type(exc) is WaitingForApproval
    assert len(rig.herdr.prompts) == 1 and rig.herdr.gets == []
    assert rig.state.usage.dispatches == 1

    control = Rig(tmp_path / "control", monkeypatch)
    _, exc = control.run()
    assert isinstance(exc, AnswerMissing)
    assert len(control.herdr.prompts) == 2


class _NoFollowUp:
    """A dispatcher with `dispatch` but no `follow_up`, like `FakeDispatcher`
    and `HarnessDispatcher`."""

    def __init__(self, inner: HerdrDispatcher) -> None:
        self.inner = inner

    def dispatch(self, role, prompt, *, state):
        return self.inner.dispatch(role, prompt, state=state)

    def endpoint_evidence(self, role):
        return self.inner.endpoint_evidence(role)


def test_no_follow_up_method_unchanged(tmp_path, monkeypatch):
    """Catches: the controller following up through a dispatcher that has no
    `follow_up` (it is looked up with getattr and optional). Control: the
    same dispatcher with the method gets one follow-up."""
    rig = Rig(tmp_path / "without", monkeypatch)
    rig.controller.dispatcher = _NoFollowUp(rig.d)
    _, exc = rig.run()
    assert isinstance(exc, AnswerMissing) and exc.followed_up is False
    assert len(rig.herdr.prompts) == 1 and rig.herdr.gets == []
    assert rig.state.usage.dispatches == 1

    control = Rig(tmp_path / "with", monkeypatch)
    _, exc = control.run()
    assert isinstance(exc, AnswerMissing) and exc.followed_up is True
    assert len(control.herdr.prompts) == 2


# --------------------------------------------------------------------------- #
# K2 -- an early-ended turn gets exactly one follow-up
# --------------------------------------------------------------------------- #


def test_follow_up_dispatcher_raises_answer_missing_after_one_prompt(tmp_path, monkeypatch):
    """Catches: the no-answer path raising a bare `DispatchError` without the
    pane view (it recorded none), or the dispatcher prompting again on its
    own, uncharged. Control: an answer present after the turn is returned."""
    rig = Rig(tmp_path / "missing", monkeypatch)
    _, exc = rig.dispatch()
    assert type(exc) is AnswerMissing
    assert exc.transient is False and exc.followed_up is False
    assert PANE in str(exc)
    assert len(rig.herdr.prompts) == 1
    assert exc.answer_path == rig.answer and exc.target == TARGET
    assert exc.prompt_sha256 == hashlib.sha256(rig.full().encode()).hexdigest()
    assert exc.prompt_bytes == len(rig.full().encode()) and exc.prompt_file is None

    control = Rig(tmp_path / "control", monkeypatch)
    control.herdr.on_prompt = lambda n: control.write_answer()
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER


def test_follow_up_answer_written_after_it_succeeds(tmp_path, monkeypatch):
    """Catches: no follow-up at all (the iteration lost as an outage), an
    uncharged follow-up, or one that rewrites the dispatch marker. Control:
    an answer present after the first turn needs exactly one prompt."""
    rig = Rig(tmp_path / "late", monkeypatch)
    rig.herdr.on_prompt = lambda n: rig.write_answer() if n == 2 else None
    result, exc = rig.run()
    assert exc is None and result == ANSWER
    assert len(rig.herdr.prompts) == 2
    assert rig.state.usage.dispatches == 2
    assert "follow-up" in rig.notes()
    marker = json.loads(rig.d._marker_for(rig.answer).read_text(encoding="utf-8"))
    assert marker["prompt_sha256"] == hashlib.sha256(rig.full().encode()).hexdigest()
    follow_up_text = rig.herdr.prompts[1][4]
    assert str(rig.answer) in follow_up_text

    control = Rig(tmp_path / "control", monkeypatch)
    control.herdr.on_prompt = lambda n: control.write_answer()
    result, exc = control.run()
    assert exc is None and result == ANSWER
    assert len(control.herdr.prompts) == 1
    assert control.state.usage.dispatches == 1


def test_follow_up_never_answered_fails_with_pane_view(tmp_path, monkeypatch):
    """Catches: a second follow-up, or a final error without the pane view.
    Control: the answer written during the follow-up turn is collected."""
    rig = Rig(tmp_path / "never", monkeypatch)
    _, exc = rig.run()
    assert type(exc) is AnswerMissing
    assert exc.followed_up is True and exc.transient is False
    assert PANE in str(exc)
    assert len(rig.herdr.prompts) == 2
    assert rig.state.usage.dispatches == 2

    control = Rig(tmp_path / "control", monkeypatch)
    control.herdr.on_prompt = lambda n: control.write_answer() if n == 2 else None
    result, exc = control.run()
    assert exc is None and result == ANSWER


def test_follow_up_refused_by_budget(tmp_path, monkeypatch):
    """Catches: a follow-up that is not charged -- an uncounted provider call
    past a budget that has refused it. Control: one more dispatch in the
    budget, and the follow-up is sent."""
    rig = Rig(tmp_path / "spent", monkeypatch, max_dispatches=1)
    _, exc = rig.run()
    assert isinstance(exc, DispatchError) and "refused" in str(exc)
    assert len(rig.herdr.prompts) == 1
    assert rig.state.usage.dispatches == 1
    assert "follow-up" in rig.notes()

    control = Rig(tmp_path / "control", monkeypatch, max_dispatches=2)
    control.herdr.on_prompt = lambda n: control.write_answer() if n == 2 else None
    result, exc = control.run()
    assert exc is None and result == ANSWER
    assert len(control.herdr.prompts) == 2


def test_follow_up_rechecks_the_answer_before_sending(tmp_path, monkeypatch):
    """Catches: no re-check before the follow-up -- an answer that arrived
    after the grace wait would be parked by a needless, charged prompt.
    Control: no late answer, and the follow-up is sent."""
    rig = Rig(tmp_path / "arrived", monkeypatch)
    rig.on_pane = lambda: rig.write_answer()
    result, exc = rig.run()
    assert exc is None and result == ANSWER
    assert len(rig.herdr.prompts) == 1 and rig.herdr.gets == []
    assert rig.state.usage.dispatches == 1

    control = Rig(tmp_path / "control", monkeypatch)
    _, exc = control.run()
    assert len(control.herdr.prompts) == 2


def test_follow_up_empty_answer(tmp_path, monkeypatch):
    """Catches: an empty answer file read as present, so the turn is not
    followed up and the run fails on "empty answer". Control: a non-empty
    file after the first turn is collected with one prompt."""
    rig = Rig(tmp_path / "empty", monkeypatch)
    rig.herdr.on_prompt = lambda n: rig.write_answer("  \n" if n == 1 else ANSWER)
    result, exc = rig.run()
    assert exc is None and result == ANSWER
    assert len(rig.herdr.prompts) == 2

    control = Rig(tmp_path / "control", monkeypatch)
    control.herdr.on_prompt = lambda n: control.write_answer(ANSWER)
    result, exc = control.run()
    assert exc is None and len(control.herdr.prompts) == 1


def test_follow_up_keeps_binding(tmp_path, monkeypatch):
    """Catches: the follow-up writing a dispatch marker of its own, so O199
    would no longer collect a late answer to the original prompt on a
    resume. Control: the follow-up's own text is not what the marker binds."""
    rig = Rig(tmp_path / "binding", monkeypatch)
    rig.herdr.on_prompt = lambda n: rig.write_answer() if n == 2 else None
    result, exc = rig.run()
    assert exc is None and result == ANSWER
    follow_up_text = rig.herdr.prompts[1][4]
    assert rig.d._resumable_answer(Role.QA, rig.answer, rig.full()) is True
    assert rig.d._resumable_answer(Role.QA, rig.answer, follow_up_text) is False
    # Nothing versioned in answers/: no second marker, no parked answer.
    assert not [p.name for p in rig.answer.parent.iterdir() if ".v" in p.name]


def test_follow_up_long_prompt(tmp_path, monkeypatch):
    """Catches: the follow-up inlining the prompt file's content into argv
    (an E2BIG at the kernel, long-prompt L5). Control: the content is in the
    prompt file, so the check would see it if it were inlined."""
    rig = Rig(tmp_path / "long", monkeypatch)
    long_prompt = (LONG_SEED + " " + "x" * 100) * 800
    rig.herdr.on_prompt = lambda n: rig.write_answer() if n == 2 else None
    result, exc = rig.run(prompt=long_prompt)
    assert exc is None and result == ANSWER
    follow_up = rig.herdr.prompts[1]
    assert max(len(a.encode("utf-8")) for a in follow_up) <= ARGV_ELEMENT_LIMIT
    assert LONG_SEED not in " ".join(follow_up)
    assert LONG_SEED in rig.prompt_file().read_text(encoding="utf-8")


def test_follow_up_tampered_prompt_file_refused(tmp_path, monkeypatch):
    """Catches: no re-hash after the follow-up turn -- an answer collected
    although the prompt file changed. Control: the same turn without the
    change is collected."""
    long_prompt = "x" * 70000
    rig = Rig(tmp_path / "tampered", monkeypatch)

    def tamper_and_answer(n):
        if n == 2:
            rig.tamper()
            rig.write_answer()

    rig.herdr.on_prompt = tamper_and_answer
    result, exc = rig.run(prompt=long_prompt)
    assert result is None and type(exc) is DispatchError
    assert exc.transient is False and rig.prompt_file().name in str(exc)
    assert len(rig.herdr.prompts) == 2

    control = Rig(tmp_path / "control", monkeypatch)
    control.herdr.on_prompt = lambda n: control.write_answer() if n == 2 else None
    result, exc = control.run(prompt=long_prompt)
    assert exc is None and result == ANSWER


def test_follow_up_failure_not_transient(tmp_path, monkeypatch):
    """Catches: a failed follow-up send retried as transient (a follow-up is
    never retried), or raised without the pane view. Control: the same
    failure on the first prompt is transient."""
    rig = Rig(tmp_path / "failed", monkeypatch)
    rig.herdr.on_prompt = lambda n: _error("agent_prompt_stalled") if n == 2 else None
    _, exc = rig.run()
    assert type(exc) is DispatchError
    assert exc.transient is False and PANE in str(exc)
    assert len(rig.herdr.prompts) == 2
    assert rig.state.usage.transient_retries == 0

    control = Rig(tmp_path / "control", monkeypatch)
    control.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
    _, exc = control.dispatch()
    assert exc.transient is True


def test_follow_up_blocked_pane_unchanged(tmp_path, monkeypatch):
    """Catches: the follow-up's own turn ending `blocked` without the answer
    read as a missing answer instead of a dialog (drspec-b F3). Control: the
    follow-up turn ending `idle` gives `AnswerMissing`."""
    rig = Rig(tmp_path / "blocked", monkeypatch)
    rig.herdr.on_prompt = lambda n: _reply("blocked") if n == 2 else None
    _, exc = rig.run()
    assert type(exc) is WaitingForApproval
    assert len(rig.herdr.prompts) == 2 and rig.state.usage.dispatches == 2

    control = Rig(tmp_path / "control", monkeypatch)
    _, exc = control.run()
    assert type(exc) is AnswerMissing


@pytest.mark.parametrize("status, expected", [
    ("blocked", WaitingForApproval),
    ("unknown", DispatchError),
    ("", DispatchError),
])
def test_follow_up_not_sent_unless_the_turn_ended(tmp_path, monkeypatch, status, expected):
    """Catches: a follow-up into a dialog (`blocked`), or into a turn whose
    status does not prove it ended (`unknown`, empty): only idle and done
    do. Control: `done` is followed up."""
    rig = Rig(tmp_path / "status", monkeypatch)
    rig.herdr.on_get = lambda n: _reply(status)
    _, exc = rig.run()
    assert type(exc) is expected
    assert exc.transient is False and PANE in str(exc)
    assert len(rig.herdr.prompts) == 1 and len(rig.herdr.gets) == 1
    assert rig.state.usage.dispatches == 1

    control = Rig(tmp_path / "control", monkeypatch)
    control.herdr.on_get = lambda n: _reply("done")
    _, exc = control.run()
    assert len(control.herdr.prompts) == 2


def test_follow_up_status_read_fails_closed(tmp_path, monkeypatch):
    """Catches: an unreadable status read as `idle`, so a turn whose state is
    unknown is prompted and charged. Control: a readable `idle` leads to the
    follow-up."""
    rig = Rig(tmp_path / "unreadable", monkeypatch)
    rig.herdr.on_get = lambda n: _error("agent_not_found")
    _, exc = rig.run()
    assert type(exc) is DispatchError
    assert exc.transient is False and PANE in str(exc)
    assert len(rig.herdr.prompts) == 1 and len(rig.herdr.gets) == 1
    assert rig.state.usage.dispatches == 1

    control = Rig(tmp_path / "control", monkeypatch)
    _, exc = control.run()
    assert len(control.herdr.prompts) == 2 and len(control.herdr.gets) == 1


def test_follow_up_after_working(tmp_path, monkeypatch):
    """Catches: a follow-up into an agent that resumed by itself, or a
    passive wait that is charged. `working`, then `idle` without the answer:
    exactly one follow-up, charged once. Control: the answer arrives with the
    end of that turn, and nothing is sent."""
    rig = Rig(tmp_path / "working", monkeypatch, timeout_ms=3_600_000, step=100.0)
    rig.herdr.on_get = lambda n: _reply("working" if n == 1 else "idle")
    rig.herdr.on_prompt = lambda n: rig.write_answer() if n == 2 else None
    result, exc = rig.run()
    assert exc is None and result == ANSWER
    assert len(rig.herdr.prompts) == 2 and rig.state.usage.dispatches == 2
    assert "resumed by itself" in rig.notes()

    control = Rig(tmp_path / "control", monkeypatch, timeout_ms=3_600_000, step=100.0)

    def working_then_done(n):
        if n == 1:
            return _reply("working")
        control.write_answer()
        return _reply("idle")

    control.herdr.on_get = working_then_done
    result, exc = control.run()
    assert exc is None and result == ANSWER
    assert len(control.herdr.prompts) == 1 and control.state.usage.dispatches == 1


def test_follow_up_answer_not_collected_mid_turn(tmp_path, monkeypatch):
    """Catches: an answer taken while the agent still reports `working`,
    which leaves the role working unwitnessed (drspec-a N2). Control: the
    turn that ends at once is collected at the first read."""
    rig = Rig(tmp_path / "midturn", monkeypatch, timeout_ms=3_600_000, step=100.0)

    def reads(n):
        if n == 2:
            rig.write_answer()
        return _reply("working" if n < 4 else "idle")

    rig.herdr.on_get = reads
    result, exc = rig.run()
    assert exc is None and result == ANSWER
    assert len(rig.herdr.gets) == 4
    assert len(rig.herdr.prompts) == 1 and rig.state.usage.dispatches == 1

    control = Rig(tmp_path / "control", monkeypatch, timeout_ms=3_600_000, step=100.0)

    def ended(n):
        control.write_answer()
        return _reply("working" if n < 2 else "idle")

    control.herdr.on_get = ended
    result, exc = control.run()
    assert exc is None and len(control.herdr.gets) == 2


def test_follow_up_passive_wait_is_bounded(tmp_path, monkeypatch):
    """Catches: a wait that ignores the role timeout, a tight loop, or a
    timeout reported as `AnswerMissing`. Always `working`, no answer: a
    non-transient `DispatchError` at the timeout, every wait at least 10 s,
    nothing sent and nothing charged. Control: the answer present at the
    deadline is collected there."""
    rig = Rig(tmp_path / "bounded", monkeypatch, timeout_ms=3_600_000, step=100.0)
    rig.herdr.on_get = lambda n: _reply("working")
    _, exc = rig.run()
    assert type(exc) is DispatchError
    assert exc.transient is False and PANE in str(exc)
    assert "still working" in str(exc)
    assert len(rig.herdr.gets) >= 2
    assert rig.clock.slept and min(rig.clock.slept) >= 10
    assert len(rig.herdr.prompts) == 1 and rig.state.usage.dispatches == 1

    control = Rig(tmp_path / "control", monkeypatch, timeout_ms=3_600_000, step=100.0)

    def working(n):
        if n == 2:
            control.write_answer()
        return _reply("working")

    control.herdr.on_get = working
    result, exc = control.run()
    assert exc is None and result == ANSWER
    assert "role timeout" in control.notes()
    assert len(control.herdr.prompts) == 1


def test_follow_up_failed_read_inside_wait_is_retried(tmp_path, monkeypatch):
    """Catches: one failed status read ending the passive wait (drspec-a N4).
    Control: `STATUS_READ_ATTEMPTS` consecutive failures do end it, with the
    pane view and nothing sent."""
    rig = Rig(tmp_path / "retried", monkeypatch, timeout_ms=3_600_000, step=100.0)

    def reads(n):
        if n == 1:
            return _reply("working")
        if n == 2:
            return _error("server_busy")
        rig.write_answer()
        return _reply("idle")

    rig.herdr.on_get = reads
    result, exc = rig.run()
    assert exc is None and result == ANSWER
    assert len(rig.herdr.gets) == 3
    assert len(rig.herdr.prompts) == 1 and rig.state.usage.dispatches == 1

    control = Rig(tmp_path / "control", monkeypatch, timeout_ms=3_600_000, step=100.0)
    control.herdr.on_get = lambda n: _reply("working") if n == 1 else _error("server_busy")
    _, exc = control.run()
    assert type(exc) is DispatchError and exc.transient is False and PANE in str(exc)
    assert len(control.herdr.gets) == 1 + dispatchers.STATUS_READ_ATTEMPTS
    assert len(control.herdr.prompts) == 1


def test_follow_up_working_tampered(tmp_path, monkeypatch):
    """Catches: an answer collected after a passive wait during which the
    prompt file changed. Control: the same wait without the change is
    collected."""
    long_prompt = "x" * 70000
    rig = Rig(tmp_path / "tampered", monkeypatch, timeout_ms=3_600_000, step=100.0)

    def reads(n):
        if n == 1:
            return _reply("working")
        rig.tamper()
        rig.write_answer()
        return _reply("idle")

    rig.herdr.on_get = reads
    result, exc = rig.run(prompt=long_prompt)
    assert result is None and type(exc) is DispatchError
    assert exc.transient is False and rig.prompt_file().name in str(exc)
    assert len(rig.herdr.prompts) == 1

    control = Rig(tmp_path / "control", monkeypatch, timeout_ms=3_600_000, step=100.0)

    def reads_(n):
        if n == 1:
            return _reply("working")
        control.write_answer()
        return _reply("idle")

    control.herdr.on_get = reads_
    result, exc = control.run(prompt=long_prompt)
    assert exc is None and result == ANSWER


def test_follow_up_after_violation_refused(tmp_path, monkeypatch):
    """Catches: the witness check before the follow-up skipped -- a role
    that wrote into a protected tree during its turn would still be read
    and prompted. Control: the same turn without the write is followed up."""
    rig = Rig(tmp_path / "violated", monkeypatch)
    rig.herdr.on_prompt = lambda n: rig.touch_state() if n == 1 else None
    _, exc = rig.run()
    assert isinstance(exc, CapabilityViolation) and "state.json" in str(exc)
    assert "on a turn that ended without the answer file" in rig.notes()
    assert rig.herdr.gets == [] and len(rig.herdr.prompts) == 1
    assert rig.state.usage.dispatches == 1

    control = Rig(tmp_path / "control", monkeypatch)
    _, exc = control.run()
    assert type(exc) is AnswerMissing and len(control.herdr.prompts) == 2


def test_follow_up_charge_checks_the_witness_first(tmp_path, monkeypatch):
    """Catches: the charge re-baselining `state.json` without checking the
    witness first -- a role's write to it during the passive wait is folded
    into the new baseline, a follow-up is charged and sent, and the write is
    never recorded (round-2 blocker, K24, mutant M24). With a budget that
    allows the follow-up, nothing else would catch it. Control: the same
    wait without the write is followed up."""
    rig = Rig(tmp_path / "write", monkeypatch, timeout_ms=3_600_000, step=100.0)

    def reads(n):
        if n == 1:
            return _reply("working")
        rig.touch_state()
        return _reply("idle")

    rig.herdr.on_get = reads
    _, exc = rig.run()
    assert isinstance(exc, CapabilityViolation) and "state.json" in str(exc)
    assert "before the follow-up charge" in rig.notes()
    assert len(rig.herdr.prompts) == 1 and rig.state.usage.dispatches == 1

    control = Rig(tmp_path / "control", monkeypatch, timeout_ms=3_600_000, step=100.0)
    control.herdr.on_get = lambda n: _reply("working" if n == 1 else "idle")
    _, exc = control.run()
    assert type(exc) is AnswerMissing and exc.followed_up is True
    assert len(control.herdr.prompts) == 2 and control.state.usage.dispatches == 2


# Each path out of `follow_up`: (rig options, the fake's behaviour, the error
# that leaves without a write). `write` is the role's write to `state.json`,
# placed after the controller's check before the follow-up.
def _path_timeout(rig, write):
    def reads(n):
        if n == 2 and write:
            rig.touch_state()
        return _reply("working")
    rig.herdr.on_get = reads


def _path_first_read_fails(rig, write):
    def reads(n):
        if write:
            rig.touch_state()
        return _error("agent_not_found")
    rig.herdr.on_get = reads


def _path_reads_fail_in_wait(rig, write):
    def reads(n):
        if n == 1:
            return _reply("working")
        if n == 2 and write:
            rig.touch_state()
        return _error("server_busy")
    rig.herdr.on_get = reads


def _path_unknown(rig, write):
    def reads(n):
        if write:
            rig.touch_state()
        return _reply("unknown")
    rig.herdr.on_get = reads


def _path_blocked_status(rig, write):
    def reads(n):
        if write:
            rig.touch_state()
        return _reply("blocked")
    rig.herdr.on_get = reads


def _path_charge_refused(rig, write):
    def reads(n):
        if write:
            rig.touch_state()
        return _reply("idle")
    rig.herdr.on_get = reads


def _path_blocked_turn(rig, write):
    def prompts(n):
        if n == 2:
            if write:
                rig.touch_state()
            return _reply("blocked")
    rig.herdr.on_prompt = prompts


def _path_send_fails(rig, write):
    def prompts(n):
        if n == 2:
            if write:
                rig.touch_state()
            return _error("agent_not_found")
    rig.herdr.on_prompt = prompts


def _path_answer_missing(rig, write):
    def prompts(n):
        if n == 2 and write:
            rig.touch_state()
    rig.herdr.on_prompt = prompts


def _path_tampered(rig, write):
    def prompts(n):
        if n == 2:
            rig.tamper()
            if write:
                rig.touch_state()
    rig.herdr.on_prompt = prompts


_WAIT = {"timeout_ms": 3_600_000, "step": 100.0}
_PATHS = [
    pytest.param(_path_timeout, _WAIT, "p", DispatchError, id="wait-timeout"),
    pytest.param(_path_first_read_fails, {}, "p", DispatchError, id="first-read-fails"),
    pytest.param(_path_reads_fail_in_wait, _WAIT, "p", DispatchError, id="reads-fail-in-wait"),
    pytest.param(_path_unknown, {}, "p", DispatchError, id="unknown-status"),
    pytest.param(_path_blocked_status, {}, "p", WaitingForApproval, id="blocked-status"),
    pytest.param(_path_blocked_turn, {}, "p", WaitingForApproval, id="blocked-follow-up-turn"),
    pytest.param(_path_charge_refused, {"max_dispatches": 1}, "p", DispatchError,
                 id="charge-refused"),
    pytest.param(_path_send_fails, {}, "p", DispatchError, id="send-fails"),
    pytest.param(_path_answer_missing, {}, "p", AnswerMissing, id="answer-missing"),
    pytest.param(_path_tampered, {}, "x" * 70000, DispatchError, id="tampered"),
]


@pytest.mark.parametrize("arrange, options, prompt, error", _PATHS)
def test_follow_up_error_paths_witnessed(tmp_path, monkeypatch, arrange, options,
                                         prompt, error):
    """Catches: a role's write to `state.json` during a passive wait or the
    follow-up turn hidden behind the error that ends the follow-up (round-2
    blocker; mutants M24, M25 and the tamper-only skip). Every path out of
    `follow_up` checks the witness first. Control: the same path without the
    write lets the original error through."""
    rig = Rig(tmp_path / "write", monkeypatch, **options)
    arrange(rig, True)
    _, exc = rig.run(prompt=prompt)
    assert isinstance(exc, CapabilityViolation), repr(exc)
    assert "state.json" in str(exc)
    assert "capability violation" in rig.notes()

    control = Rig(tmp_path / "control", monkeypatch, **options)
    arrange(control, False)
    _, exc = control.run(prompt=prompt)
    assert type(exc) is error, repr(exc)
    assert not isinstance(exc, CapabilityViolation)
    assert "capability violation" not in control.notes()


# --------------------------------------------------------------------------- #
# Disclosure and prevention in the role prompts
# --------------------------------------------------------------------------- #

DISCLOSURE = ("If a verdict rests on work you did in an earlier iteration or attempt of "
              "this run (this same session), say so in that verdict's `note`.")
PREVENTION = ("Do not end your turn while work you started in the background is still "
              "running; wait for it.")


def _plan() -> DevelopmentPlan:
    return DevelopmentPlan.model_validate({
        "run_id": "r", "iteration": 1, "base_candidate_id": "b", "spec_digest": "s",
        "objective": "o", "targets": ["t"],
        "acceptance_checks": [{"check_id": "K1", "description": "d", "command": "true",
                               "expect_exit": 0}]})


def test_follow_up_prevention_and_disclosure_in_role_prompts():
    """Catches: QA answering from an earlier iteration's reproductions
    without saying so (pqagate iteration 2), and a planner or QA prompt that
    does not ask the role to wait for its background work. Control: the
    developer prompt, which is not asked to disclose, carries neither."""
    qa = roles.qa_prompt(iteration=1, spec_text="spec", plan=_plan(), candidate_path="c",
                         candidate_binding="b", receipts_summary="r")
    planner = roles.planner_prompt(iteration=1, spec_text="spec",
                                   evidence=EvidenceBundle(run_id="r"), repo_path="/x",
                                   base_candidate_id="b", spec_digest="s", run_id="r")
    assert DISCLOSURE in qa
    assert PREVENTION in qa and PREVENTION in planner
    # The prevention sentence opens the policy section it belongs to.
    assert "/assessment-policy\n" + PREVENTION in qa
    assert "/planning-policy\n" + PREVENTION in planner

    developer = roles.developer_prompt(iteration=1, attempt=0, spec_text="spec",
                                       plan=_plan(), workspace="/w", warm_start=False,
                                       evidence=EvidenceBundle(run_id="r"))
    assert DISCLOSURE not in developer and PREVENTION not in developer


def test_follow_up_writes_nothing_in_answers(tmp_path, monkeypatch):
    """Catches: the follow-up writing a marker, a prompt file or an audit
    record, or renaming the answer (long-prompt L3/L4: the follow-up binds
    nothing). Control: the first dispatch of a long prompt does write them."""
    rig = Rig(tmp_path / "long", monkeypatch)
    long_prompt = "x" * 70000
    before: dict[str, list[str]] = {}

    def snapshot_then_answer(n):
        if n == 2:
            rig.write_answer()

    def first_turn(n):
        if n == 1:
            # The first dispatch has written its files; nothing may follow.
            before["answers"] = sorted(os.listdir(rig.answer.parent))
            before["run"] = sorted(os.listdir(rig.store.dir))
            before["audit"] = [(rig.store.dir / dispatchers.PROMPT_BINDINGS)
                               .read_text(encoding="utf-8")]
        snapshot_then_answer(n)

    rig.herdr.on_prompt = first_turn
    result, exc = rig.run(prompt=long_prompt)
    assert exc is None and result == ANSWER
    after = sorted(os.listdir(rig.answer.parent))
    assert sorted(set(after) - {rig.answer.name}) == sorted(
        set(before["answers"]) - {rig.answer.name})
    audit = (rig.store.dir / dispatchers.PROMPT_BINDINGS).read_text(encoding="utf-8")
    assert [audit] == before["audit"]
    # Control: what the first dispatch wrote is really there to compare with.
    assert rig.prompt_file().name in before["answers"]
    assert rig.d._marker_for(rig.answer).name in before["answers"]


# --------------------------------------------------------------------------- #
# Dispatch residue: never prompt into a pane whose input is not proven free,
# and never start a message with an input-mode character (run padopt, O252)
# --------------------------------------------------------------------------- #


def _stall_first(rig, then=None):
    """The first prompt stalls; later ones call `then(n)`."""
    def prompts(n):
        if n == 1:
            return _error("agent_prompt_stalled")
        return then(n) if then is not None else None
    rig.herdr.on_prompt = prompts


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_residue_stall_retry_into_unsent_input_refused(tmp_path, monkeypatch):
    """Catches: the merged stall retry re-sent without reading the pane
    (`controller.py` retry loop), so the next prompt is appended to the unsent
    one still in the input box -- padopt i2, `Unknown command: /prompt-file`.
    Control: the same stall with an empty box is retried and answered."""
    rig = Rig(tmp_path / "residue", monkeypatch)
    _stall_first(rig)
    rig.herdr.on_read = lambda n: RESIDUE_PANE
    _, exc = rig.run()
    assert type(exc) is PaneNotClean, repr(exc)
    assert exc.transient is False
    assert len(rig.herdr.prompts) == 1
    assert len(rig.herdr.gets) == 1 and len(rig.herdr.reads) == 1

    control = Rig(tmp_path / "control", monkeypatch)
    _stall_first(control, lambda n: control.write_answer())
    result, exc = control.run()
    assert exc is None and result == ANSWER
    assert len(control.herdr.prompts) == 2
    assert len(control.herdr.reads) == 1
    assert "input box empty" in control.notes()


@pytest.mark.parametrize("read, why", [
    pytest.param(lambda n: _error("agent_not_found"), "could not be read", id="read-fails"),
    pytest.param(lambda n: OTHER_SCREEN, "no input box", id="no-box"),
    pytest.param(lambda n: RESIDUE_PANE, "unsent text", id="text"),
])
def test_residue_input_unknown_refused(tmp_path, monkeypatch, read, why):
    """Catches: an input box that cannot be read, is not found, or holds text,
    taken as free after a prompt that did not return. The refusal names the
    length and the first 120 characters of the text. Control: an empty box
    is prompted."""
    rig = Rig(tmp_path / "unknown", monkeypatch)
    rig.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
    rig.herdr.on_read = read
    _, first = rig.dispatch()
    assert first.transient is True
    _, exc = rig.dispatch()
    assert type(exc) is PaneNotClean, repr(exc)
    assert why in str(exc)
    if why == "unsent text":
        assert str(len(RESIDUE)) in str(exc)
        assert RESIDUE[:120] in str(exc)
    assert len(rig.herdr.prompts) == 1

    control = Rig(tmp_path / "control", monkeypatch)
    control.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
    control.dispatch()
    _, exc = control.dispatch()
    assert type(exc) is DispatchError and exc.transient is True
    assert len(control.herdr.prompts) == 2


@pytest.mark.parametrize("get, expected", [
    pytest.param(lambda n: _error("agent_not_found"), PaneNotClean, id="unreadable"),
    pytest.param(lambda n: _reply("blocked"), WaitingForApproval, id="blocked"),
    pytest.param(lambda n: _reply("unknown"), PaneNotClean, id="unknown"),
])
def test_residue_status_must_prove_a_free_input(tmp_path, monkeypatch, get, expected):
    """Catches: a pane prompted after a prompt that did not return although
    its status is unreadable, a dialog, or proves nothing -- no box is read
    then either. Control: `idle` with an empty box is prompted."""
    rig = Rig(tmp_path / "status", monkeypatch)
    _stall_first(rig)
    rig.herdr.on_get = get
    _, exc = rig.run()
    assert type(exc) is expected, repr(exc)
    assert exc.transient is False
    assert len(rig.herdr.prompts) == 1 and rig.herdr.reads == []

    control = Rig(tmp_path / "control", monkeypatch)
    _stall_first(control, lambda n: control.write_answer())
    result, exc = control.run()
    assert exc is None and result == ANSWER
    assert len(control.herdr.prompts) == 2


_AWAIT = {"timeout_ms": 3_600_000, "step": 100.0}


def test_residue_working_agent_not_prompted_twice(tmp_path, monkeypatch):
    """Catches T-DR1: an agent that started the stalled prompt late is
    prompted a second time by the retry. It is awaited instead, and its
    answer is collected with one prompt. Control: an idle agent with an
    empty box gets the retry."""
    rig = Rig(tmp_path / "working", monkeypatch, **_AWAIT)
    _stall_first(rig)

    def reads(n):
        if n == 1:
            return _reply("working")
        rig.write_answer()
        return _reply("idle")

    rig.herdr.on_get = reads
    result, exc = rig.run()
    assert exc is None and result == ANSWER
    assert len(rig.herdr.prompts) == 1
    assert rig.herdr.reads == []
    assert "its turn is awaited" in rig.notes()
    assert TARGET not in rig.d._unsettled()

    control = Rig(tmp_path / "control", monkeypatch, **_AWAIT)
    _stall_first(control, lambda n: control.write_answer())
    result, exc = control.run()
    assert exc is None and result == ANSWER
    assert len(control.herdr.prompts) == 2


def test_residue_awaited_turn_keeps_the_original_send_time(tmp_path, monkeypatch):
    """Catches: the awaited turn judged as a fresh send -- re-sent (which
    stalls again here) or timed from the retry. Nothing is re-sent, and the
    `AnswerMissing` carries the stalled send's own monotonic time. Control:
    an idle pane with an empty box is sent again, at a later time."""
    rig = Rig(tmp_path / "awaited", monkeypatch, **_AWAIT)
    rig.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
    rig.dispatch()
    original = rig.d._unsettled()[TARGET][1]
    rig.herdr.on_get = lambda n: _reply("working" if n == 1 else "idle")
    _, exc = rig.dispatch()
    assert type(exc) is AnswerMissing, repr(exc)
    assert exc.sent_at == original
    assert len(rig.herdr.prompts) == 1

    control = Rig(tmp_path / "control", monkeypatch, **_AWAIT)
    control.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
    control.dispatch()
    first = control.d._unsettled()[TARGET][1]
    _, exc = control.dispatch()
    assert len(control.herdr.prompts) == 2
    assert control.d._unsettled()[TARGET][1] > first


def test_residue_awaited_turn_is_bounded(tmp_path, monkeypatch):
    """Catches: an unbounded wait for the stalled prompt's turn -- reads that
    keep failing, or an agent that never stops working. `STATUS_READ_ATTEMPTS`
    failed reads in a row raise `PaneNotClean`; the role timeout, counted from
    the stalled send, refuses; every wait is at least 10 s; nothing is sent.
    Control: one failed read inside the wait is read again, and the answer
    is collected."""
    rig = Rig(tmp_path / "reads", monkeypatch, **_AWAIT)
    rig.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
    rig.dispatch()
    rig.herdr.on_get = lambda n: _reply("working") if n == 1 else _error("server_busy")
    _, exc = rig.dispatch()
    assert type(exc) is PaneNotClean, repr(exc)
    assert len(rig.herdr.gets) == 1 + dispatchers.STATUS_READ_ATTEMPTS
    assert len(rig.herdr.prompts) == 1

    timeout = Rig(tmp_path / "timeout", monkeypatch, **_AWAIT)
    timeout.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
    timeout.dispatch()
    timeout.herdr.on_get = lambda n: _reply("working")
    _, exc = timeout.dispatch()
    assert type(exc) is DispatchError and exc.transient is False
    assert "still working at the role timeout" in str(exc)
    assert timeout.clock.slept and min(timeout.clock.slept) >= 10
    assert 2 <= len(timeout.herdr.gets) < MAX_STATUS_READS
    assert len(timeout.herdr.prompts) == 1

    control = Rig(tmp_path / "control", monkeypatch, **_AWAIT)
    control.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
    control.dispatch()

    def reads(n):
        if n == 1:
            return _reply("working")
        if n == 2:
            return _error("server_busy")
        control.write_answer()
        return _reply("idle")

    control.herdr.on_get = reads
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER
    assert len(control.herdr.prompts) == 1


def test_residue_working_on_another_prompt_refused(tmp_path, monkeypatch):
    """Catches: an agent working on a different prompt (its own digest does
    not match this one) awaited or prompted as if it were this prompt's turn.
    Control: working on exactly this prompt is awaited, not refused."""
    rig = Rig(tmp_path / "other", monkeypatch, **_AWAIT)
    rig.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
    rig.dispatch(prompt="p")
    rig.herdr.on_get = lambda n: _reply("working")
    _, exc = rig.dispatch(prompt="q")
    assert type(exc) is PaneNotClean, repr(exc)
    assert "not on this prompt" in str(exc)
    assert len(rig.herdr.prompts) == 1 and len(rig.herdr.gets) == 1

    control = Rig(tmp_path / "control", monkeypatch, **_AWAIT)
    control.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
    control.dispatch(prompt="p")

    def reads(n):
        if n > 1:
            control.write_answer()
        return _reply("working" if n == 1 else "idle")

    control.herdr.on_get = reads
    result, exc = control.dispatch(prompt="p")
    assert exc is None and result == ANSWER
    assert len(control.herdr.prompts) == 1


def test_residue_check_only_after_a_prompt_that_did_not_return(tmp_path, monkeypatch):
    """Catches: `_settle` reading every pane before every prompt -- a fresh
    agent's box may not be drawn yet, and a restored pane is
    `_check_restored`'s. An unmarked or restored target costs no Herdr call.
    Control: a target whose prompt did not return is read."""
    rig = Rig(tmp_path / "unmarked", monkeypatch)
    sha = _sha(rig.full())
    assert rig.d._settle(Role.QA, TARGET, sha, rig.state) is None
    rig.d._unsettled()[TARGET] = (dispatchers.RESTORED, 0.0, None)
    assert rig.d._settle(Role.QA, TARGET, sha, rig.state) is None
    assert rig.herdr.calls == []
    rig.d._unsettled().clear()
    rig.herdr.on_prompt = lambda n: rig.write_answer()
    result, exc = rig.dispatch()
    assert exc is None and result == ANSWER
    assert [c[1:3] for c in rig.herdr.calls] == [["agent", "prompt"]]

    control = Rig(tmp_path / "control", monkeypatch)
    control.d._unsettled()[TARGET] = (sha, 0.0, None)
    assert control.d._settle(Role.QA, TARGET, sha, control.state) is None
    assert [c[1:3] for c in control.herdr.calls] == [["agent", "get"], ["agent", "read"]]


def test_residue_after_a_failed_follow_up(tmp_path, monkeypatch):
    """Catches: a failed follow-up send leaving the pane unmarked, so the next
    dispatch prompts into whatever the follow-up left in the box. The mark is
    `(sha256(text), now, None)`, and the next dispatch reads the pane first.
    Control: a follow-up that returned leaves no mark, and the next dispatch
    prompts without a read."""
    rig = Rig(tmp_path / "failed", monkeypatch)
    rig.herdr.on_prompt = lambda n: _error("agent_not_found") if n == 2 else None
    _, exc = rig.run()
    assert type(exc) is DispatchError
    follow_up_text = rig.herdr.prompts[1][4]
    digest, _when, tree = rig.d._unsettled()[TARGET]
    assert digest == _sha(follow_up_text) and tree is None
    rig.herdr.on_read = lambda n: RESIDUE_PANE
    _, exc = rig.dispatch(prompt="next")
    assert type(exc) is PaneNotClean, repr(exc)
    assert len(rig.herdr.prompts) == 2

    control = Rig(tmp_path / "control", monkeypatch)
    _, exc = control.run()
    assert type(exc) is AnswerMissing
    assert TARGET not in control.d._unsettled()
    control.herdr.on_read = lambda n: RESIDUE_PANE
    control.herdr.on_prompt = lambda n: control.write_answer() if n == 3 else None
    result, exc = control.dispatch(prompt="next")
    assert exc is None and result == ANSWER
    assert len(control.herdr.prompts) == 3 and control.herdr.reads == []


def test_residue_after_a_transient_timeout(tmp_path, monkeypatch):
    """Catches: the mark set only for a stall, so a wrapper timeout (also
    transient) is retried straight into the box. Control: an empty box after
    the timeout is retried."""
    rig = Rig(tmp_path / "timeout", monkeypatch)
    rig.herdr.on_prompt = lambda n: subprocess.TimeoutExpired(["herdr"], 1)
    _, exc = rig.dispatch()
    assert exc.transient is True
    assert rig.d._unsettled()[TARGET][0] == _sha(rig.full())
    rig.herdr.on_read = lambda n: RESIDUE_PANE
    _, exc = rig.run()
    assert type(exc) is PaneNotClean, repr(exc)
    assert len(rig.herdr.prompts) == 1

    control = Rig(tmp_path / "control", monkeypatch)

    def timeout_then_answer(n):
        if n == 1:
            return subprocess.TimeoutExpired(["herdr"], 1)
        control.write_answer()

    control.herdr.on_prompt = timeout_then_answer
    result, exc = control.run()
    assert exc is None and result == ANSWER
    assert len(control.herdr.prompts) == 2 and len(control.herdr.reads) == 1


def test_residue_pending_input_parsed():
    """Catches: a box parser that reads a placeholder, an earlier box in the
    scrollback, or another kind's screen as empty. The last two rules frame
    the box and its first line opens with the input mark; anything else is
    None (unknown). Control: an empty box is `""`."""
    assert dispatchers._pending_input(EMPTY_BOX) == ""
    assert dispatchers._pending_input(RESIDUE_PANE) == RESIDUE
    assert dispatchers._pending_input(PLACEHOLDER_PANE) == 'Try "refactor the dispatcher"'
    assert dispatchers._pending_input(OTHER_SCREEN) is None
    assert dispatchers._pending_input("") is None
    assert dispatchers._pending_input(f"{RULE}\n❯ text\n") is None          # one rule only
    assert dispatchers._pending_input(f"{RULE}\nno mark here\n{RULE}\n") is None
    assert dispatchers._pending_input(f"{RULE}\n{RULE}\n") is None               # no body
    # The last box decides, not an earlier one in the scrollback.
    assert dispatchers._pending_input(RESIDUE_PANE + EMPTY_BOX) == ""
    assert dispatchers._pending_input(EMPTY_BOX + RESIDUE_PANE) == RESIDUE


def _all_role_prompts() -> dict[str, str]:
    plan = _plan()
    evidence = EvidenceBundle(run_id="r")
    return {
        "planner": roles.planner_prompt(iteration=1, spec_text="spec", evidence=evidence,
                                        repo_path="/x", base_candidate_id="b",
                                        spec_digest="s", run_id="r"),
        "developer-warm": roles.developer_prompt(iteration=1, attempt=0, spec_text="spec",
                                                 plan=plan, workspace="/w", warm_start=True,
                                                 evidence=evidence),
        "developer-cold": roles.developer_prompt(iteration=1, attempt=0, spec_text="spec",
                                                 plan=plan, workspace="/w", warm_start=False,
                                                 evidence=evidence),
        "qa": roles.qa_prompt(iteration=1, spec_text="spec", plan=plan, candidate_path="c",
                              candidate_binding="b", receipts_summary="r"),
        "repair": roles.repair_prompt(Role.QA, "bad", "{"),
    }


def test_no_dispatched_message_starts_with_a_slash(tmp_path, monkeypatch):
    """Catches: a dispatched message whose first character is `/` or `!` --
    the role prompts, the prompt-file pointer, the answer instruction and the
    follow-up all began with `/`, and Claude Code answered `/prompt-file` with
    `Unknown command`. Control: the old forms are what the guard refuses."""
    prompts = _all_role_prompts()
    for name, text in prompts.items():
        assert not text.lstrip().startswith(dispatchers.INPUT_MODE_PREFIXES), name
    assert prompts["planner"].splitlines()[0] == "[role/project-planner]"
    assert prompts["developer-warm"].splitlines()[0] == "[role/developer]"
    assert prompts["qa"].splitlines()[0] == "[role/qa-tester]"
    pointer = _pointer_prompt(Path("/x/p.md"), 1, "0" * 64)
    assert pointer.splitlines()[0] == "[prompt-file]"
    assert _answer_instruction(Path("/x/a.json")).lstrip().splitlines()[0] == "[answer-file]"

    # Every `agent prompt` a run sends: inline, long (the pointer), follow-up.
    rig = Rig(tmp_path / "sent", monkeypatch)
    rig.herdr.on_prompt = lambda n: rig.write_answer() if n == 2 else None
    result, exc = rig.run(prompt=prompts["qa"])
    assert exc is None and result == ANSWER
    long = Rig(tmp_path / "long", monkeypatch)
    long.herdr.on_prompt = lambda n: long.write_answer()
    result, exc = long.run(prompt=prompts["qa"] + "x" * 70000)
    assert exc is None and result == ANSWER
    sent = [c[4] for c in rig.herdr.prompts + long.herdr.prompts]
    assert len(sent) == 3
    assert sent[1].splitlines()[0] == "[answer-file-missing]"
    assert sent[2].splitlines()[0] == "[prompt-file]"
    for text in sent:
        assert not text.lstrip().startswith(dispatchers.INPUT_MODE_PREFIXES), text[:40]

    # Control: the forms before this change are refused by the guard.
    for old in ("/role/qa-tester\n", "/prompt-file\n", "/answer-file-missing\n"):
        with pytest.raises(DispatchError):
            dispatchers._first_line_guard(old, "control")


def test_first_line_guard_refuses_input_mode_prefixes(tmp_path, monkeypatch):
    """Catches: a message starting with an input-mode character sent anyway,
    or a refused follow-up still charged. `_first_line_guard` refuses `/` and
    `!` after leading whitespace; a refused dispatch sends and writes nothing;
    a refused follow-up is not charged. Control: a bracketed or mid-line
    slash passes, and the follow-up is charged and sent."""
    for text in ("/compact", "!rm x", "  /x", "\n\n!x", "\t/x"):
        with pytest.raises(DispatchError) as info:
            dispatchers._first_line_guard(text, "qa dispatch")
        assert info.value.transient is False
    for text in ("[role/qa-tester]", "a/b", "x !y", "  [answer-file]"):
        dispatchers._first_line_guard(text, "qa dispatch")

    rig = Rig(tmp_path / "dispatch", monkeypatch)
    _, exc = rig.dispatch(prompt="/compact now")
    assert type(exc) is DispatchError and "is not sent" in str(exc)
    assert rig.herdr.calls == []
    assert not rig.d._marker_for(rig.answer).exists()

    refused = Rig(tmp_path / "follow-up", monkeypatch)
    real = dispatchers._first_line_guard

    def guard(text, what):
        if "follow-up" in what:
            real("/" + text, what)
        real(text, what)

    monkeypatch.setattr(dispatchers, "_first_line_guard", guard)
    _, exc = refused.run()
    assert type(exc) is DispatchError and "is not sent" in str(exc)
    assert len(refused.herdr.prompts) == 1
    assert refused.state.usage.dispatches == 1

    monkeypatch.setattr(dispatchers, "_first_line_guard", real)
    control = Rig(tmp_path / "control", monkeypatch)
    _, exc = control.run()
    assert type(exc) is AnswerMissing
    assert len(control.herdr.prompts) == 2 and control.state.usage.dispatches == 2


def test_pane_not_clean_keeps_the_tabs_open():
    """Catches: `PaneNotClean` taking the normal error exit, where
    `close_own` closes the tabs -- and with them the pane the operator has to
    inspect. Recognised by type, not by a word. Control: a plain
    `DispatchError` with the same text does not keep them open."""
    from hoh import cli

    assert issubclass(PaneNotClean, DispatchError)
    assert not issubclass(PaneNotClean, WaitingForApproval)
    exc = PaneNotClean("qa dispatch not sent: the input box holds text")
    assert exc.transient is False
    assert cli._waiting_for_approval(exc) is True
    assert cli._waiting_for_approval(
        DispatchError("qa dispatch not sent: the input box holds text")) is False


def test_kept_open_notice_for_a_pane_that_is_not_clean():
    """Catches: the operator told to grant an approval that does not exist
    (MX7) when the tabs stay open for a pane that is not clean. Control: a
    `WaitingForApproval` keeps the approval sentence."""
    from hoh import cli

    notice = cli._kept_open_notice(PaneNotClean("x"))
    assert "inspected" in notice and "nothing in it was sent" in notice
    assert "hoh unblock <run-id> and hoh run" in notice
    assert "replaces a restored pane" in notice
    assert "approval" not in notice

    approval = cli._kept_open_notice(WaitingForApproval("x"))
    assert approval == ("The role tabs stay open so that the approval can be granted "
                        "in the pane. After that: hoh unblock <run-id> and hoh run.")


def test_residue_working_agent_answer_not_collected_mid_turn(tmp_path, monkeypatch):
    """Catches: the awaited stalled turn's answer taken while the agent still
    reports `working` -- the role would go on unwitnessed. Control: the turn
    that ends with the answer is collected at that read."""
    rig = Rig(tmp_path / "midturn", monkeypatch, **_AWAIT)
    _stall_first(rig)

    def reads(n):
        if n == 2:
            rig.write_answer()
        return _reply("working" if n < 4 else "idle")

    rig.herdr.on_get = reads
    result, exc = rig.run()
    assert exc is None and result == ANSWER
    assert len(rig.herdr.gets) == 4
    assert len(rig.herdr.prompts) == 1

    control = Rig(tmp_path / "control", monkeypatch, **_AWAIT)
    _stall_first(control)

    def ended(n):
        if n == 2:
            control.write_answer()
        return _reply("working" if n < 2 else "idle")

    control.herdr.on_get = ended
    result, exc = control.run()
    assert exc is None and result == ANSWER
    assert len(control.herdr.gets) == 2


def test_residue_mark_set_before_the_tamper_refusal(tmp_path, monkeypatch):
    """Catches: the mark set after lp-dirchain's tamper refusal (C3'), so a
    prompt that failed with a changed prompt file leaves the pane unmarked
    and the next dispatch prompts into it. Control: a prompt that returned
    leaves no mark, and the next one is sent without a read."""
    long_prompt = "x" * 70000
    rig = Rig(tmp_path / "tampered", monkeypatch)

    def tamper_and_stall(n):
        rig.tamper()
        return _error("agent_prompt_stalled")

    rig.herdr.on_prompt = tamper_and_stall
    _, exc = rig.dispatch(prompt=long_prompt)
    assert type(exc) is DispatchError and exc.transient is False
    assert rig.prompt_file().name in str(exc)
    assert TARGET in rig.d._unsettled()
    rig.herdr.on_read = lambda n: RESIDUE_PANE
    _, exc = rig.dispatch(prompt=long_prompt)
    assert type(exc) is PaneNotClean, repr(exc)
    assert len(rig.herdr.prompts) == 1

    control = Rig(tmp_path / "control", monkeypatch)
    control.herdr.on_read = lambda n: RESIDUE_PANE
    control.herdr.on_prompt = lambda n: control.write_answer() if n == 2 else None
    control.dispatch(prompt=long_prompt)
    assert TARGET not in control.d._unsettled()
    result, exc = control.dispatch(prompt="q")
    assert exc is None and result == ANSWER
    assert len(control.herdr.prompts) == 2 and control.herdr.reads == []


def test_residue_awaited_developer_judged_against_the_pre_send_tree(tmp_path, monkeypatch):
    """Catches (MR23): an awaited stalled developer judged against a tree
    digest taken after the wait -- its work during the wait is invisible, and
    a turn that ends `blocked` reads as a dialog instead of a delivery. The
    stalled send's own pre-send digest is used, nothing is re-sent. Control:
    a developer that changes nothing and ends `blocked` is a dialog."""
    def run(root: Path, change: bool):
        rig = Rig(root, monkeypatch, **_AWAIT)
        rig.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
        _, first = rig.dispatch(role=Role.DEVELOPER)
        assert first.transient is True

        def reads(n):
            if n == 1:
                return _reply("working")
            if change:
                (Path(rig.d.cwd) / "made-during-the-wait.txt").write_text("x\n",
                                                                           encoding="utf-8")
            return _reply("blocked")

        rig.herdr.on_get = reads
        return rig, *rig.dispatch(role=Role.DEVELOPER)

    rig, result, exc = run(tmp_path / "changed", True)
    assert exc is None, repr(exc)
    assert result.startswith("developer run finished")
    assert len(rig.herdr.prompts) == 1

    control, result, exc = run(tmp_path / "unchanged", False)
    assert type(exc) is WaitingForApproval, repr(exc)
    assert len(control.herdr.prompts) == 1


# --------------------------------------------------------------------------- #
# A pane Herdr restored: checked before its first prompt, replaced -- never
# prompted -- unless its input box is provably empty
# --------------------------------------------------------------------------- #

ADOPTED_TAB = "w0:t1"
ADOPTED_PANE = "w0:p1"
FRESH_TAB = "w0:t9"
FRESH_PANE = "w0:p9"
OPERATOR_PANE = "w0:p7"


class RestoredRig(Rig):
    """The `Rig`, but with the real `_ensure_agent`: Herdr has restored this
    run's QA agent in `ADOPTED_TAB`/`ADOPTED_PANE`.

    `dispatchers.herdr.snapshot` shows the agent and its tab until a
    `pane close` appears among the fake's calls; after that it shows `after`:
    "gone", "operator" (the operator's pane still holds the tab), "pane" (the
    pane still listed), "agent" (the agent still listed) or "reappears" (gone
    once, then back). `now(row)` changes the row after the adoption -- a race
    -- and may return a list of rows. `row` overrides fields of the agent's
    row from the start. `_new_tab_pane`, `_agent_args` and `_start_agent` are
    fakes that record a fresh start.
    """

    def __init__(self, root: Path, monkeypatch, *, after: str = "gone", now=None,
                 row: dict | None = None, restored: bool = True, **options) -> None:
        super().__init__(root, monkeypatch, **options)
        d = self.d
        monkeypatch.setattr(d, "_ensure_agent",
                            types.MethodType(HerdrDispatcher._ensure_agent, d))
        d.profiles = {Role.QA: "claude", Role.PLANNER: "claude", Role.DEVELOPER: "claude"}
        d.agents, d.own_tabs, d.own_panes = {}, [], []
        self.name = d._agent_name(Role.QA, self.state)
        self.row = {"name": self.name, "pane_id": ADOPTED_PANE, "workspace_id": "w0",
                    "tab_id": ADOPTED_TAB, "agent": "claude", "agent_status": "idle",
                    "cwd": str(self.store.dir), "foreground_cwd": str(self.store.dir),
                    **(row or {})}
        self.after = after
        self.now = now
        self.restored = restored
        self.snapshots = 0
        self.closed_snapshots = 0
        self.started: list[tuple[str, str]] = []
        monkeypatch.setattr(dispatchers.herdr, "snapshot", self.snapshot)

        def new_tab_pane(label, *, cwd=None):
            d.own_tabs.append(FRESH_TAB)
            return FRESH_PANE

        monkeypatch.setattr(d, "_new_tab_pane", new_tab_pane)
        monkeypatch.setattr(d, "_agent_args", lambda role: [])
        monkeypatch.setattr(d, "_start_agent",
                            lambda name, kind, pane, extra=None:
                            self.started.append((name, pane)))

    def closed(self) -> bool:
        return any(c[1:3] == ["pane", "close"] for c in self.herdr.calls)

    def snapshot(self) -> dict:
        self.snapshots += 1
        if not self.restored:
            return {"agents": [], "tabs": [], "panes": []}
        if not self.closed():
            rows = [dict(self.row)]
            if self.now is not None and self.snapshots > 1:
                changed = self.now(dict(self.row))
                rows = changed if isinstance(changed, list) else [changed]
            return {"agents": rows,
                    "tabs": [{"tab_id": ADOPTED_TAB}],
                    "panes": [{"pane_id": ADOPTED_PANE, "tab_id": ADOPTED_TAB}]}
        self.closed_snapshots += 1
        gone = {"agents": [], "tabs": [], "panes": []}
        if self.after == "operator":
            return {"agents": [], "tabs": [{"tab_id": ADOPTED_TAB}],
                    "panes": [{"pane_id": OPERATOR_PANE, "tab_id": ADOPTED_TAB}]}
        if self.after == "pane":
            return {"agents": [], "tabs": [{"tab_id": ADOPTED_TAB}],
                    "panes": [{"pane_id": ADOPTED_PANE, "tab_id": ADOPTED_TAB}]}
        if self.after == "agent" or (self.after == "reappears"
                                     and self.closed_snapshots > 1):
            return {"agents": [dict(self.row)], "tabs": [{"tab_id": ADOPTED_TAB}],
                    "panes": [{"pane_id": ADOPTED_PANE, "tab_id": ADOPTED_TAB}]}
        return gone

    def kinds(self) -> list[list[str]]:
        return [c[1:3] for c in self.herdr.calls]

    def answer_prompts(self) -> None:
        self.herdr.on_prompt = lambda n: self.write_answer()


_REPLACED = [["agent", "get"], ["agent", "read"], ["pane", "close"], ["agent", "prompt"]]


@pytest.mark.parametrize("read, recorded", [
    pytest.param(lambda n: RESIDUE_PANE, RESIDUE, id="residue"),
    pytest.param(lambda n: OTHER_SCREEN, "no input box", id="other-kind"),
    pytest.param(lambda n: PLACEHOLDER_PANE, 'Try "refactor the dispatcher"',
                 id="placeholder"),
    pytest.param(lambda n: _error("agent_not_found"), "could not be read", id="read-fails"),
])
def test_residue_restored_pane_replaced_not_prompted(tmp_path, monkeypatch, read, recorded):
    """Catches: a restored pane prompted into whatever its input box holds --
    residue of an earlier process, another kind's screen, a placeholder, or a
    box that cannot be read (MR27: refused instead, which blocks every
    resume). It is recorded with the whole input, its pane alone is closed
    (never the tab, MR30), the tab leaves `own_tabs` (MY2), and the prompt
    goes to a fresh agent. Control (MR28): an empty box is used, checked
    once, and the tab stays."""
    rig = RestoredRig(tmp_path / "replaced", monkeypatch)
    rig.answer_prompts()
    rig.herdr.on_read = read
    result, exc = rig.dispatch()
    assert exc is None and result == ANSWER, repr(exc)
    assert rig.kinds() == _REPLACED
    assert rig.herdr.calls[2] == ["herdr", "pane", "close", ADOPTED_PANE]
    assert rig.herdr.prompts[0][3] == rig.name
    assert rig.started == [(rig.name, FRESH_PANE)]
    assert ADOPTED_TAB not in rig.d.own_tabs and FRESH_TAB in rig.d.own_tabs
    assert f"restored pane {rig.name} not reused" in rig.notes()
    # The whole input, not its first 120 characters (MR32), and the pane view.
    assert recorded in rig.notes() and PANE in rig.notes()
    assert rig.d._unsettled() == {} and rig.d._adopted() == {}

    control = RestoredRig(tmp_path / "control", monkeypatch)
    control.answer_prompts()
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER
    assert control.kinds() == [["agent", "get"], ["agent", "read"], ["agent", "prompt"]]
    assert control.started == []
    assert ADOPTED_TAB in control.d.own_tabs
    assert "input box empty" in control.notes()
    # Checked once: the next prompt to it is not read again.
    control.state.attempt += 1
    control.answer = control.d._answer_path(Role.QA, control.state)
    result, exc = control.dispatch()
    assert exc is None and len(control.herdr.reads) == 1


@pytest.mark.parametrize("after", ["pane", "agent", "reappears"])
def test_residue_restored_pane_replacement_must_be_confirmed(tmp_path, monkeypatch, after):
    """Catches: a replacement that does not confirm the old pane is gone
    (MR25), or that adopts the old agent again and prompts it (MR26): the pane
    still listed, the agent still listed, or the agent back on the next
    snapshot. `PaneNotClean`, nothing prompted. Control: the confirmed
    replacement is prompted."""
    rig = RestoredRig(tmp_path / after, monkeypatch, after=after)
    rig.answer_prompts()
    rig.herdr.on_read = lambda n: RESIDUE_PANE
    _, exc = rig.dispatch()
    assert type(exc) is PaneNotClean, repr(exc)
    assert rig.herdr.prompts == []
    assert rig.started == []

    control = RestoredRig(tmp_path / "control", monkeypatch)
    control.answer_prompts()
    control.herdr.on_read = lambda n: RESIDUE_PANE
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER
    assert control.kinds() == _REPLACED


def test_residue_restored_pane_answer_collected_first(tmp_path, monkeypatch):
    """Catches (MR24): a restored pane checked -- and replaced -- before the
    O199 collection, so the answer the earlier process got to exactly this
    prompt is thrown away with its pane. It is collected with no Herdr call.
    Control: without that answer the pane is checked and replaced."""
    rig = RestoredRig(tmp_path / "collected", monkeypatch)
    rig.herdr.on_read = lambda n: RESIDUE_PANE
    rig.d._write_dispatch_marker(rig.answer, rig.full())
    rig.write_answer()
    result, exc = rig.dispatch()
    assert exc is None and result == ANSWER
    assert rig.herdr.calls == []
    assert rig.d.collected_after_dialog == [Role.QA.value]

    control = RestoredRig(tmp_path / "control", monkeypatch)
    control.answer_prompts()
    control.herdr.on_read = lambda n: RESIDUE_PANE
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER
    assert control.kinds() == _REPLACED


def test_fresh_agent_not_checked(tmp_path, monkeypatch):
    """Catches (MX6): a freshly started agent marked and read like a restored
    one -- its box may not be drawn yet, so the read proves nothing and a
    first prompt would be refused. Control: a restored pane is read exactly
    once."""
    rig = RestoredRig(tmp_path / "fresh", monkeypatch, restored=False)
    rig.answer_prompts()
    result, exc = rig.dispatch()
    assert exc is None and result == ANSWER
    assert rig.started == [(rig.name, FRESH_PANE)]
    assert rig.kinds() == [["agent", "prompt"]]
    assert rig.d._unsettled() == {}

    control = RestoredRig(tmp_path / "control", monkeypatch)
    control.answer_prompts()
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER
    assert len(control.herdr.reads) == 1


def _foreign_cwd(row):
    return {**row, "cwd": "/home/someone/other-project",
            "foreground_cwd": "/home/someone/other-project"}


def _other_tab(row):
    return {**row, "tab_id": "w0:t5", "pane_id": "w0:p5"}


def _two_of_that_name(row):
    return [row, {**row, "tab_id": "w0:t5", "pane_id": "w0:p5"}]


def _empty_cwd(row):
    return {**row, "cwd": "", "foreground_cwd": ""}


def _relative_cwd(row):
    return {**row, "cwd": "answers", "foreground_cwd": "answers"}


@pytest.mark.parametrize("case", [
    "foreign-cwd", "other-tab", "tab-not-own", "unknown-cwd-empty",
    "unknown-cwd-relative", "two-of-that-name", "empty-adopted-id",
])
def test_residue_replacement_closes_only_what_hoh_adopted(tmp_path, monkeypatch, case):
    """Catches (R1, MR29a-d, MR33, MZ1, MZ3): a replacement that closes a pane
    HoH cannot prove it adopted -- the agent now in a foreign directory, in
    another tab and pane, a tab that is not one of this run's, a directory
    that is empty or relative (it would resolve against HoH's own working
    directory, here the run directory), two agents of that name, or an
    adopted id that is empty. Each raises `PaneNotClean`, and no `pane` or
    `tab` command is issued at all. Control: the same rig without the change
    is replaced."""
    monkeypatch.chdir(tmp_path)
    options = {
        "foreign-cwd": {"now": _foreign_cwd},
        "other-tab": {"now": _other_tab},
        "tab-not-own": {},
        "unknown-cwd-empty": {"now": _empty_cwd},
        "unknown-cwd-relative": {"now": _relative_cwd},
        "two-of-that-name": {"now": _two_of_that_name},
        "empty-adopted-id": {"row": {"pane_id": ""}},
    }[case]
    rig = RestoredRig(tmp_path / "case", monkeypatch, **options)
    rig.answer_prompts()
    rig.herdr.on_read = lambda n: RESIDUE_PANE
    if case == "tab-not-own":
        def drop_tab(n):
            rig.d.own_tabs.remove(ADOPTED_TAB)
        rig.herdr.on_get = drop_tab
    if case.startswith("unknown-cwd"):
        # Inside the run directory, an empty or relative cwd resolves into
        # the run's roots: only the absolute-path check stands in the way.
        monkeypatch.chdir(rig.store.dir)
        assert HerdrDispatcher._is_under("answers", rig.d._own_roots(rig.state))
    _, exc = rig.dispatch()
    assert type(exc) is PaneNotClean, repr(exc)
    assert "Nothing was closed" in str(exc)
    assert not [c for c in rig.herdr.calls if c[1] in ("pane", "tab")]
    assert rig.herdr.prompts == [] and rig.started == []

    monkeypatch.chdir(tmp_path)
    control = RestoredRig(tmp_path / "control", monkeypatch)
    control.answer_prompts()
    control.herdr.on_read = lambda n: RESIDUE_PANE
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER
    assert control.kinds() == _REPLACED


def test_residue_replacement_leaves_an_operator_pane_alone(tmp_path, monkeypatch):
    """Catches (R2, R3, MR30, MR31, MY2): the whole tab closed with the
    restored agent's pane, so a pane the operator opened next to it dies, or
    the tab kept in `own_tabs` so `close_own` closes it later, or the
    survivor not recorded. No `tab` command, the tab leaves `own_tabs`, and
    the "left alone" note is written. Control: a tab that went with its
    only pane gets no such note."""
    rig = RestoredRig(tmp_path / "operator", monkeypatch, after="operator")
    rig.answer_prompts()
    rig.herdr.on_read = lambda n: RESIDUE_PANE
    result, exc = rig.dispatch()
    assert exc is None and result == ANSWER, repr(exc)
    assert not [c for c in rig.herdr.calls if c[1] == "tab"]
    assert ADOPTED_TAB not in rig.d.own_tabs
    assert f"tab {ADOPTED_TAB} still holds other panes" in rig.notes()
    assert "it is left alone" in rig.notes()

    control = RestoredRig(tmp_path / "control", monkeypatch)
    control.answer_prompts()
    control.herdr.on_read = lambda n: RESIDUE_PANE
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER
    assert ADOPTED_TAB not in control.d.own_tabs
    assert "left alone" not in control.notes()


@pytest.mark.parametrize("cwd", [pytest.param("", id="empty"),
                                 pytest.param("answers", id="relative")])
def test_restored_agent_with_unknown_cwd_not_adopted(tmp_path, monkeypatch, cwd):
    """Catches (MR34): a restored agent adopted although Herdr reports no
    absolute directory for it -- an empty or relative cwd resolves against
    HoH's own working directory (here the run directory), so ownership
    depended on where `hoh run` was started. Refused with `PaneNotClean`
    (Revision 5a: the run blocks once) naming "absolute directory"; nothing
    is read, closed, sent or started. Control: an absolute cwd in the run is
    adopted and prompted."""
    rig = RestoredRig(tmp_path / "unknown", monkeypatch,
                      row={"cwd": cwd, "foreground_cwd": cwd})
    rig.answer_prompts()
    monkeypatch.chdir(rig.store.dir)
    _, exc = rig.dispatch()
    assert type(exc) is PaneNotClean, repr(exc)
    assert exc.transient is False
    assert "absolute directory" in str(exc)
    assert rig.herdr.calls == [] and rig.started == []
    assert Role.QA not in rig.d.agents and rig.d._adopted() == {}

    control = RestoredRig(tmp_path / "control", monkeypatch)
    control.answer_prompts()
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER
    assert control.started == [] and len(control.herdr.prompts) == 1


# --------------------------------------------------------------------------- #
# dispatch-residue: drob-a's surviving mutants, each killed by a named test
# --------------------------------------------------------------------------- #


def _on_sleep(rig, monkeypatch, act):
    """Runs `act(seconds)` on every sleep of the dispatcher's fake clock."""
    slept = rig.clock.slept

    def sleep(seconds):
        slept.append(seconds)
        act(seconds)

    monkeypatch.setattr(rig.clock, "sleep", sleep)


def test_grace_tamper_check_in_dispatch(tmp_path, monkeypatch):
    """Catches X3: the re-hash after the grace wait in `_dispatch` removed,
    so an answer that arrives together with a changed prompt file during the
    grace wait is collected. Control: the same late answer without the
    change is collected."""
    long_prompt = "x" * 70000
    rig = Rig(tmp_path / "tampered", monkeypatch, step=1.0)

    def tamper_and_answer(seconds):
        if not rig.answer.exists():
            rig.tamper()
            rig.write_answer()

    _on_sleep(rig, monkeypatch, tamper_and_answer)
    _, exc = rig.dispatch(prompt=long_prompt)
    assert type(exc) is DispatchError and exc.transient is False
    assert rig.prompt_file().name in str(exc) and "after the turn" in str(exc)

    control = Rig(tmp_path / "control", monkeypatch, step=1.0)
    _on_sleep(control, monkeypatch,
              lambda s: control.answer.exists() or control.write_answer())
    result, exc = control.dispatch(prompt=long_prompt)
    assert exc is None and result == ANSWER


def test_follow_up_send_failure_checks_the_prompt_file(tmp_path, monkeypatch):
    """Catches X4: no re-hash when the follow-up send fails, so a prompt file
    changed meanwhile is reported as a plain send failure. Control: the same
    failure with the file intact is the plain refusal, naming no change."""
    long_prompt = "x" * 70000
    rig = Rig(tmp_path / "tampered", monkeypatch)

    def tamper_then_fail(n):
        if n == 2:
            rig.tamper()
            return _error("agent_not_found")

    rig.herdr.on_prompt = tamper_then_fail
    _, exc = rig.run(prompt=long_prompt)
    assert type(exc) is DispatchError and exc.transient is False
    assert "changed when the follow-up failed" in str(exc)
    assert rig.prompt_file().name in str(exc)

    control = Rig(tmp_path / "control", monkeypatch)
    control.herdr.on_prompt = lambda n: _error("agent_not_found") if n == 2 else None
    _, exc = control.run(prompt=long_prompt)
    assert type(exc) is DispatchError and "a follow-up is not retried" in str(exc)
    assert "changed" not in str(exc)


def test_follow_up_grace_after_the_follow_up_turn(tmp_path, monkeypatch):
    """Catches X6: no grace wait after the follow-up turn, so an answer
    written a few seconds after that turn ends becomes
    `AnswerMissing(followed_up)`. Control: an answer that never comes is
    still `AnswerMissing(followed_up)`."""
    rig = Rig(tmp_path / "late", monkeypatch, step=1.0)
    _on_sleep(rig, monkeypatch,
              lambda s: len(rig.herdr.prompts) == 2 and rig.write_answer())
    result, exc = rig.run()
    assert exc is None and result == ANSWER
    assert len(rig.herdr.prompts) == 2
    assert "collected after the follow-up" in rig.notes()

    control = Rig(tmp_path / "never", monkeypatch, step=1.0)
    _, exc = control.run()
    assert type(exc) is AnswerMissing and exc.followed_up is True


def test_follow_up_grace_after_the_passive_wait(tmp_path, monkeypatch):
    """Catches X7: no grace wait after the agent's own turn ended, so an
    answer written seconds later costs a charged follow-up prompt. Control:
    no answer at all, and the follow-up is sent."""
    options = {"timeout_ms": 3_600_000, "step": 1.0}
    rig = Rig(tmp_path / "late", monkeypatch, **options)
    rig.herdr.on_get = lambda n: _reply("working" if n == 1 else "idle")
    _on_sleep(rig, monkeypatch, lambda s: (s == dispatchers.POLL_SECONDS
                                           and len(rig.herdr.gets) >= 2
                                           and rig.write_answer()))
    result, exc = rig.run()
    assert exc is None and result == ANSWER
    assert len(rig.herdr.prompts) == 1 and rig.state.usage.dispatches == 1
    assert "collected after the agent's own turn ended" in rig.notes()

    control = Rig(tmp_path / "never", monkeypatch, **options)
    control.herdr.on_get = lambda n: _reply("working" if n == 1 else "idle")
    _, exc = control.run()
    assert len(control.herdr.prompts) == 2 and control.state.usage.dispatches == 2


def test_follow_up_wait_counts_from_the_original_send(tmp_path, monkeypatch):
    """Catches X8: `sent_at` taken when the turn ended instead of before the
    prompt, so a turn that used up the role timeout still gets a full new
    timeout of passive waiting. Control: a short turn leaves time to wait,
    and the wait runs."""
    options = {"timeout_ms": 3_600_000, "step": 100.0}
    rig = Rig(tmp_path / "long-turn", monkeypatch, **options)

    def long_turn(n):
        if n == 1:
            rig.clock.now += 3500.0

    rig.herdr.on_prompt = long_turn
    rig.herdr.on_get = lambda n: _reply("working" if n == 1 else "idle")
    _, exc = rig.run()
    assert type(exc) is DispatchError and exc.transient is False
    assert "still working at the role timeout" in str(exc)
    assert len(rig.herdr.gets) == 1 and len(rig.herdr.prompts) == 1

    control = Rig(tmp_path / "short-turn", monkeypatch, **options)
    control.herdr.on_get = lambda n: _reply("working" if n == 1 else "idle")
    _, exc = control.run()
    assert len(control.herdr.gets) >= 2 and len(control.herdr.prompts) == 2


def test_follow_up_counted_as_a_retry(tmp_path, monkeypatch):
    """Catches X10: the follow-up not counted in `_last_retries`, so the
    dispatch record under-counts the provider calls. Control: an answer
    after the first turn counts no retry."""
    rig = Rig(tmp_path / "followed", monkeypatch)
    rig.controller._last_retries = 0
    rig.herdr.on_prompt = lambda n: rig.write_answer() if n == 2 else None
    result, exc = rig.run()
    assert exc is None and result == ANSWER
    assert rig.controller._last_retries == 1

    control = Rig(tmp_path / "at-once", monkeypatch)
    control.controller._last_retries = 0
    control.herdr.on_prompt = lambda n: control.write_answer()
    result, exc = control.run()
    assert exc is None and control.controller._last_retries == 0


@pytest.mark.parametrize("second", ["answered", "send-fails"])
def test_follow_up_charge_note_recorded(tmp_path, monkeypatch, second):
    """Catches X11: the charge's history note dropped, so a follow-up -- and
    above all a failed one -- leaves no record line. Control: an answer
    after the first turn writes no such line."""
    rig = Rig(tmp_path / second, monkeypatch)

    def prompts(n):
        if n == 2:
            if second == "send-fails":
                return _error("agent_not_found")
            rig.write_answer()

    rig.herdr.on_prompt = prompts
    rig.run()
    line = (f"follow-up to qa: the turn ended without the answer file "
            f"{rig.answer.name}; one follow-up prompt is sent")
    assert line in rig.notes()

    control = Rig(tmp_path / f"{second}-control", monkeypatch)
    control.herdr.on_prompt = lambda n: control.write_answer()
    control.run()
    assert "one follow-up prompt is sent" not in control.notes()


# --------------------------------------------------------------------------- #
# dispatch-residue Revision 5b: the residue rules the dres2 integration review
# found unpinned (MR19, MO2, M2-M5), each killed by a named test
# --------------------------------------------------------------------------- #

PARTIAL = '{"ok": false, "partial": true}'
FINAL = '{"ok": true, "final": true}'
FOREIGN_DIR = "/home/someone/other-project"


def _run_files(rig) -> dict:
    """Every file under the run directory but the answer and `state.json`,
    with its inode and size: a write of any kind -- a new version, a leaf
    replaced by `os.replace`, an appended audit line -- shows here."""
    skip = {rig.answer.name, "state.json"}
    out = {}
    for p in sorted(rig.store.dir.rglob("*")):
        if p.is_file() and p.name not in skip:
            st = p.stat()
            out[str(p.relative_to(rig.store.dir))] = (st.st_ino, st.st_size)
    return out


@pytest.mark.parametrize("prompt", [pytest.param("p", id="short"),
                                    pytest.param("x" * 70000, id="long")])
def test_residue_settled_before_an_earlier_answer_is_collected(tmp_path, monkeypatch, prompt):
    """Catches (MR19): `_settle` moved after the O199 collection, so the answer
    file of a stalled prompt the agent is still working on is collected
    mid-turn -- the partial answer, with no Herdr call. Also catches an
    awaited turn that runs the O199 collection anyway (MO1b: recorded as
    "collected after an approved dialog"), rewrites the dispatch marker
    (MO3), or rewrites the prompt file, its marker and the audit line (MO4):
    the awaited dispatch writes nothing and sends nothing, and the final
    answer is collected after the turn ended. Control: the same partial
    answer on disk with no mark is an O199 collection with no Herdr call --
    the answer is resumable, only the order keeps it from being taken."""
    rig = Rig(tmp_path / "awaited", monkeypatch, **_AWAIT)

    def stall_mid_turn(n):
        rig.write_answer(PARTIAL)
        return _error("agent_prompt_stalled")

    rig.herdr.on_prompt = stall_mid_turn
    _, first = rig.dispatch(prompt=prompt)
    assert first.transient is True and TARGET in rig.d._unsettled()
    before = _run_files(rig)
    bindings = rig.notes().count("prompt qa: sha256=")

    def turn(n):
        if n == 1:
            return _reply("working")
        rig.write_answer(FINAL)
        return _reply("idle")

    rig.herdr.on_get = turn
    result, exc = rig.dispatch(prompt=prompt)
    assert exc is None and result == FINAL, repr(exc)
    assert len(rig.herdr.gets) == 2 and len(rig.herdr.prompts) == 1
    assert getattr(rig.d, "collected_after_dialog", []) == []
    assert _run_files(rig) == before
    assert rig.notes().count("prompt qa: sha256=") == bindings

    control = Rig(tmp_path / "control", monkeypatch, **_AWAIT)
    control.herdr.on_prompt = lambda n: (control.write_answer(PARTIAL),
                                         _error("agent_prompt_stalled"))[1]
    control.dispatch(prompt=prompt)
    control.d._unsettled().clear()
    calls = len(control.herdr.calls)
    result, exc = control.dispatch(prompt=prompt)
    assert exc is None and result == PARTIAL
    assert len(control.herdr.calls) == calls
    assert control.d.collected_after_dialog == [Role.QA.value]


def test_residue_refusal_keeps_the_whole_unsent_input(tmp_path, monkeypatch):
    """Catches (MS10): `_settle`'s refusal for text left in the input box
    keeping only its first 120 characters, so the record of what sits unsent
    in the pane loses the rest (R3). Control: the refusal's first line names
    the length and only the first 120 characters, which stop before the
    tail -- the whole text is in the record only through its own part."""
    rig = Rig(tmp_path / "residue", monkeypatch)
    rig.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
    rig.dispatch()
    rig.herdr.on_read = lambda n: RESIDUE_PANE
    _, exc = rig.dispatch()
    assert type(exc) is PaneNotClean, repr(exc)
    assert f"whole input:\n{RESIDUE}\n" in str(exc)
    assert len(rig.herdr.prompts) == 1

    head = str(exc).split("whole input:")[0]
    assert RESIDUE_TAIL not in RESIDUE[:120]
    assert f"holds {len(RESIDUE)} characters" in head and RESIDUE_TAIL not in head


def test_residue_mark_cleared_by_the_check_itself(tmp_path, monkeypatch):
    """Catches (MS11): the mark kept after `_settle` found the pane idle with
    an empty box. Only a later send would clear it, so when the late answer
    to the stalled prompt is collected instead (O199) the pane is read again
    before every later prompt. Control: without the late answer the checked
    pane is sent to, and the next prompt is not read either."""
    def run(root: Path, late: bool):
        rig = Rig(root, monkeypatch)

        def prompts(n):
            if n == 1:
                return _error("agent_prompt_stalled")
            rig.write_answer()

        rig.herdr.on_prompt = prompts
        rig.dispatch()
        if late:
            rig.write_answer()       # the stalled prompt ran late and answered
        result, exc = rig.dispatch()
        assert exc is None and result == ANSWER, repr(exc)
        assert len(rig.herdr.reads) == 1
        assert TARGET not in rig.d._unsettled()
        rig.state.attempt += 1
        rig.answer = rig.d._answer_path(Role.QA, rig.state)
        result, exc = rig.dispatch(prompt="q")
        assert exc is None and result == ANSWER, repr(exc)
        assert len(rig.herdr.reads) == 1
        return rig

    rig = run(tmp_path / "late", True)
    assert len(rig.herdr.prompts) == 2
    assert rig.d.collected_after_dialog == [Role.QA.value]

    control = run(tmp_path / "control", False)
    assert len(control.herdr.prompts) == 3


@pytest.mark.parametrize("status", ["unknown", ""])
def test_residue_awaited_turn_must_end_in_a_known_status(tmp_path, monkeypatch, status):
    """Catches (MA5): the wait for a stalled prompt's turn accepting a status
    that does not prove the turn ended (`unknown`, or none) -- the mark is
    dropped and the dispatch goes on as if the turn were over, so the next
    prompt goes out without the pane ever being proven free. `PaneNotClean`,
    the mark stays, nothing is sent. Control: `idle` ends the wait, and the
    answer written in that turn is collected."""
    rig = Rig(tmp_path / "unknown", monkeypatch, **_AWAIT)
    rig.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
    rig.dispatch()
    rig.herdr.on_get = lambda n: _reply("working" if n == 1 else status)
    _, exc = rig.dispatch()
    assert type(exc) is PaneNotClean, repr(exc)
    assert TARGET in rig.d._unsettled()
    assert len(rig.herdr.prompts) == 1 and len(rig.herdr.gets) == 2

    control = Rig(tmp_path / "control", monkeypatch, **_AWAIT)
    control.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
    control.dispatch()

    def ended(n):
        if n == 1:
            return _reply("working")
        control.write_answer()
        return _reply("idle")

    control.herdr.on_get = ended
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER
    assert TARGET not in control.d._unsettled()


def test_residue_await_polls_at_least_ten_seconds_apart(tmp_path, monkeypatch):
    """Catches (MA4b): the 10 s floor of the await's poll interval dropped, so
    a small `STATUS_POLL_SECONDS` turns the wait for a stalled turn into a
    tight read loop against Herdr. Control: an interval above the floor is
    used as configured."""
    def slept(root: Path, interval: float) -> list[float]:
        monkeypatch.setattr(dispatchers, "STATUS_POLL_SECONDS", interval)
        rig = Rig(root, monkeypatch, **_AWAIT)
        rig.herdr.on_prompt = lambda n: _error("agent_prompt_stalled")
        rig.dispatch()
        rig.herdr.on_get = lambda n: _reply("working")
        _, exc = rig.dispatch()
        assert "still working at the role timeout" in str(exc)
        assert len(rig.herdr.prompts) == 1
        return rig.clock.slept

    waits = slept(tmp_path / "small", 1.0)
    assert waits and set(waits) == {10.0}

    control = slept(tmp_path / "large", 45.0)
    assert control and set(control) == {45.0}


@pytest.mark.parametrize("get, expected", [
    pytest.param(lambda n: _reply("working"), PaneNotClean, id="working"),
    pytest.param(lambda n: _reply("unknown"), PaneNotClean, id="unknown"),
    pytest.param(lambda n: _reply(""), PaneNotClean, id="no-status"),
    pytest.param(lambda n: _error("agent_not_found"), PaneNotClean, id="unreadable"),
    pytest.param(lambda n: _reply("blocked"), WaitingForApproval, id="blocked"),
])
def test_restored_pane_status_must_prove_a_free_input(tmp_path, monkeypatch, get, expected):
    """Catches (MC1, MC3, MC2b, MC2c of dres2-a): `_check_restored` taking a
    restored agent whose status, read right before its first prompt, does
    not prove a free input -- it turned `working` after the adoption
    snapshot, reports `unknown` or nothing, cannot be read, or sits at a
    dialog. Nothing is read, closed, started or sent; a dialog is
    `WaitingForApproval` (never answered automatically), the rest
    `PaneNotClean`. Control: `idle` is read once and prompted."""
    rig = RestoredRig(tmp_path / "status", monkeypatch)
    rig.answer_prompts()
    rig.herdr.on_get = get
    _, exc = rig.dispatch()
    assert type(exc) is expected, repr(exc)
    assert rig.kinds() == [["agent", "get"]]
    assert rig.started == [] and rig.herdr.prompts == []

    control = RestoredRig(tmp_path / "control", monkeypatch)
    control.answer_prompts()
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER
    assert control.kinds() == [["agent", "get"], ["agent", "read"], ["agent", "prompt"]]


class FailingRestoredRig(RestoredRig):
    """`RestoredRig` with one fault in `_replace_restored`'s path:

    * "not-adopted" -- the adoption record is gone by the time the pane is
      replaced (it is the only proof of which pane HoH adopted);
    * "snapshot-fails" -- the snapshot before the close cannot be read;
    * "close-fails" -- `herdr pane close` fails;
    * "confirm-fails" -- the snapshot after the close cannot be read;
    * "agent-moved" -- after the close the pane is gone, but an agent of that
      name is listed in another pane (later snapshots show nothing).
    """

    def __init__(self, root: Path, monkeypatch, *, case: str, **options) -> None:
        super().__init__(root, monkeypatch, **options)
        self.case = case
        inner = self.herdr

        def run(args, **kwargs):
            reply = inner(args, **kwargs)
            if case == "close-fails" and args[1:3] == ["pane", "close"]:
                return _error("pane_not_found")
            return reply

        monkeypatch.setattr(dispatchers, "subprocess", types.SimpleNamespace(
            run=run, TimeoutExpired=subprocess.TimeoutExpired))
        if case == "not-adopted":
            self.herdr.on_get = lambda n: (self.d._adopted().clear(), None)[1]

    def snapshot(self) -> dict:
        if self.case == "snapshot-fails" and self.snapshots >= 1 and not self.closed():
            self.snapshots += 1
            raise OSError("herdr api snapshot failed (simulated)")
        if self.closed() and self.closed_snapshots == 0:
            if self.case == "confirm-fails":
                self.closed_snapshots += 1
                raise OSError("herdr api snapshot failed (simulated)")
            if self.case == "agent-moved":
                self.closed_snapshots += 1
                return {"agents": [{**self.row, "tab_id": "w0:t8", "pane_id": "w0:p8"}],
                        "tabs": [{"tab_id": "w0:t8"}],
                        "panes": [{"pane_id": "w0:p8", "tab_id": "w0:t8"}]}
        return super().snapshot()


@pytest.mark.parametrize("case, why, closes", [
    ("not-adopted", "did not record adopting it", 0),
    ("snapshot-fails", "snapshot cannot be read", 0),
    ("close-fails", "failed", 1),
    ("confirm-fails", "cannot confirm it", 1),
    ("agent-moved", "an agent of that name", 1),
])
def test_residue_replacement_fails_closed(tmp_path, monkeypatch, case, why, closes):
    """Catches (MZ4, MZ5, MR25e, MR25d, MR25b of dres2-a): a replacement that
    goes on when its proof is missing -- the pane taken from the snapshot
    instead of the adoption record, a snapshot that cannot be read before or
    after the close, a close that failed, or an agent of that name still
    listed after the close (only the pane half of the confirmation checked).
    `PaneNotClean`, nothing started or sent, no `tab` command, and no `pane`
    command at all before the close was justified. Control: the same rig
    without the fault is replaced and prompted."""
    rig = FailingRestoredRig(tmp_path / case, monkeypatch, case=case)
    rig.answer_prompts()
    rig.herdr.on_read = lambda n: RESIDUE_PANE
    _, exc = rig.dispatch()
    assert type(exc) is PaneNotClean, repr(exc)
    assert why in str(exc)
    assert rig.herdr.prompts == [] and rig.started == []
    assert [c for c in rig.herdr.calls if c[1] in ("pane", "tab")] == (
        [["herdr", "pane", "close", ADOPTED_PANE]] * closes)

    control = RestoredRig(tmp_path / "control", monkeypatch)
    control.answer_prompts()
    control.herdr.on_read = lambda n: RESIDUE_PANE
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER
    assert control.kinds() == _REPLACED


@pytest.mark.parametrize("now", [
    pytest.param(lambda row: {**row, "cwd": FOREIGN_DIR}, id="foreign-cwd-own-foreground"),
    pytest.param(lambda row: {**row, "foreground_cwd": FOREIGN_DIR}, id="foreign-foreground-own-cwd"),
])
def test_residue_replacement_checks_both_directories(tmp_path, monkeypatch, now):
    """Catches (MO2 of dres2-b, MR33b of dres2-a): the replacement's ownership
    check reading only the first non-empty directory Herdr reports, so an
    agent whose `cwd` is foreign while its `foreground_cwd` is the run's (or
    the reverse) has its pane closed. `PaneNotClean`, no `pane` or `tab`
    command. Control: both directories in the run are replaced."""
    rig = RestoredRig(tmp_path / "mixed", monkeypatch, now=now)
    rig.answer_prompts()
    rig.herdr.on_read = lambda n: RESIDUE_PANE
    _, exc = rig.dispatch()
    assert type(exc) is PaneNotClean, repr(exc)
    assert FOREIGN_DIR in str(exc) and "Nothing was closed" in str(exc)
    assert not [c for c in rig.herdr.calls if c[1] in ("pane", "tab")]
    assert rig.herdr.prompts == [] and rig.started == []

    control = RestoredRig(tmp_path / "control", monkeypatch, now=lambda row: dict(row))
    control.answer_prompts()
    control.herdr.on_read = lambda n: RESIDUE_PANE
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER
    assert control.kinds() == _REPLACED


def _cmd_run_raising(tmp_path: Path, monkeypatch, exc: BaseException):
    """`hoh run` driven through `cli.main` to an iteration that raises `exc`.

    No Herdr, no agent, no storage root outside `tmp_path`: the storage
    admission, the steward hook, the integrity inventory, the dispatcher and
    the Controller are stand-ins. Returns (exit code, stderr lines, the
    dispatcher's `close_own` calls)."""
    import io
    import sys as sys_

    from hoh import cli, integrity

    runs = tmp_path / "runs"
    (tmp_path / "repo").mkdir(parents=True)
    (tmp_path / "spec.md").write_text("# s\n", encoding="utf-8")
    RunStore(runs, "r1").write_state(Controller.new_state(
        run_id="r1", repo_path=tmp_path / "repo", project_name="p",
        spec_path=tmp_path / "spec.md"))
    closed: list[int] = []

    class Raising:
        def __init__(self, *a, **kw):
            pass

        def run_iteration(self, state):
            raise exc

    ledger = tmp_path / "storage" / "ledger"
    monkeypatch.setattr(cli, "storage_admission_for", lambda option, **kw: (
        types.SimpleNamespace(ledger=ledger), tmp_path / "storage"))
    monkeypatch.setattr(cli, "_hook_steward", lambda *a, **kw: None)
    monkeypatch.setattr(integrity, "snapshot", lambda repo: (_ for _ in ()).throw(
        RuntimeError("not measured in this test")))
    monkeypatch.setattr(controller, "Controller", Raising)
    monkeypatch.setattr(dispatchers, "build_dispatcher", lambda **kw: types.SimpleNamespace(
        endpoint_evidence=lambda role: "fake", close_own=lambda: closed.append(1) or []))
    err = io.StringIO()
    monkeypatch.setattr(sys_, "stderr", err)
    rc = cli.main(["--root", str(runs), "run", "r1", "--no-herdr", "--iterations", "1"])
    return rc, err.getvalue(), closed


def test_cmd_run_tells_the_operator_why_the_tabs_stay_open(tmp_path, monkeypatch):
    """Catches (MX7b of dres2-a): `cmd_run`'s exception path printing the old
    approval sentence for a `PaneNotClean`, so the operator is sent to grant
    an approval that does not exist instead of inspecting the pane. The tabs
    stay open and the notice says nothing was sent. Control: a
    `WaitingForApproval` keeps the approval sentence and the tabs, and a
    plain `DispatchError` closes them (the stand-in dispatcher records it).
    Every one of them ends the command with exit code 1."""
    rc, err, closed = _cmd_run_raising(
        tmp_path / "pane", monkeypatch,
        PaneNotClean("qa dispatch not sent: its input box still holds text"))
    assert rc == 1
    assert "Iteration aborted and run blocked" in err
    assert "so the pane can be inspected; nothing in it was sent" in err
    assert "the approval can be granted" not in err
    assert closed == []

    rc, err, closed = _cmd_run_raising(
        tmp_path / "approval", monkeypatch, WaitingForApproval("qa waits in pane wX:pY"))
    assert rc == 1
    assert "so that the approval can be granted in the pane" in err
    assert "nothing in it was sent" not in err
    assert closed == []

    rc, err, closed = _cmd_run_raising(
        tmp_path / "outage", monkeypatch, DispatchError("qa dispatch failed"))
    assert rc == 1
    assert "Iteration aborted and run blocked" in err
    assert "The role tabs stay open" not in err
    assert closed == [1]


# --------------------------------------------------------------------------- #
# Stalled submit: Enter pressed once on this dispatch's own stalled prompt
# (runs qaw3 and qaw4, 2026-10-05)
# --------------------------------------------------------------------------- #

#: Run qaw3's planner residue, verbatim from its `blocked_reason` ("whole
#: input"), with the 40-character state-root prefix replaced by another of the
#: same length, so every soft wrap falls where it fell in the pane.
QAW3_RESIDUE = "\n".join([
    '[prompt-file]',
    '  Your instructions for this turn are too long for one command-line argument,',
    '  so they were written to a file. That file **is** your prompt; this message',
    '  only points to it.',
    '',
    '    path:    /srv/state/projects/example-project-0001/runs/20261005T1503Z-qaw3-runs/20',
    '  261005t1503z-qaw3/answers/i1-a0-planner.prompt.md',
    '    bytes:   87302 (UTF-8)',
    '    sha256:  3b42beda70bc95d48a0c9d2fa1e65b611ee4d00dbb82d4cb0eed66a9949a01eb',
    '',
    '  Read the **whole** file before you do anything else. A long file comes back',
    '  in parts: read it page by page, passing an offset each time, and keep going',
    '  until you have reached the end of the file -- not just its beginning. Then',
    '  follow it as your instructions. Do not change, move or delete the file: the',
    '  dispatcher checks its SHA-256 again after your turn, and a changed file fails',
    '  the dispatch.',
    '',
    '',
    '  [answer-file]',
    '  Write your JSON answer **as a file** to:',
    '',
    '    /srv/state/projects/example-project-0001/runs/20261005T1503Z-qaw3-runs/20261005t15',
    '  03z-qaw3/answers/i1-a0-planner.json',
    '',
    '  Only the JSON object, nothing else -- no code fence, no surrounding text. Reply',
    '  in the chat with a single sentence saying that the file is written. The file is',
    '  authoritative; your chat text is not evaluated.',
])

QAW3_PROMPT = Path("/srv/state/projects/example-project-0001/runs/"
                   "20261005T1503Z-qaw3-runs/20261005t1503z-qaw3/answers")
QAW3_SIZE = 87302
QAW3_SHA = "3b42beda70bc95d48a0c9d2fa1e65b611ee4d00dbb82d4cb0eed66a9949a01eb"


def _qaw3_sent() -> str:
    """What the dispatcher typed in run qaw3: the pointer and the answer
    instruction, built by the dispatcher's own functions."""
    return (_pointer_prompt(QAW3_PROMPT / "i1-a0-planner.prompt.md", QAW3_SIZE, QAW3_SHA)
            + _answer_instruction(QAW3_PROMPT / "i1-a0-planner.json"))


def _claude_box(text: str, width: int = 88) -> str:
    """A pane view of Claude Code's input box holding `text`, typed and not
    submitted, in a pane `width` columns wide: the rules span the pane, the
    first line follows the input mark, every further line is indented by two
    spaces, and a line breaks mid-word two columns short of the rule -- as
    in runs qaw3 and qaw4 (rule 88, broken lines 86)."""
    rule = "\u2500" * width
    out = []
    for n, line in enumerate(text.strip().split("\n")):
        if not line:
            out.append("")
            continue
        avail = width - 4
        chunks = [line[i:i + avail] for i in range(0, len(line), avail)]
        out.append(("\u276f " if n == 0 else "  ") + chunks[0])
        out.extend("  " + c for c in chunks[1:])
    return (f"\u273b Baked for 0s\n{rule}\n" + "\n".join(out)
            + f"\n{rule}\n  \u23f5\u23f5 auto mode on (shift+tab to cycle)\n")


def _send_keys(rig) -> list[list[str]]:
    """Every key sent to a pane or an agent: only Enter, once, is allowed."""
    return [c for c in rig.herdr.calls if c[2:3] == ["send-keys"]]


@pytest.mark.parametrize("box, width, expected", [
    pytest.param(lambda s: QAW3_RESIDUE, 86, True, id="qaw3-residue"),
    pytest.param(lambda s: QAW3_RESIDUE, 90, False, id="qaw3-residue-under-another-width"),
    pytest.param(lambda s: QAW3_RESIDUE, None, False, id="width-unknown"),
    pytest.param(lambda s: QAW3_RESIDUE.replace("/20\n  26", "/2\n  026", 1), 86, False,
                 id="break-short-of-the-pane-width"),
    pytest.param(lambda s: QAW3_RESIDUE.replace("\n  so they", "\nso they", 1), 86, False,
                 id="unindented-continuation"),
    pytest.param(lambda s: QAW3_RESIDUE.replace("\n  so they", "\n>>so they", 1), 86, False,
                 id="two-characters-instead-of-the-indentation"),
    pytest.param(lambda s: QAW3_RESIDUE.replace("sha256:  3b42", "sha256:  3b43", 1), 86, False,
                 id="one-character"),
    pytest.param(lambda s: QAW3_RESIDUE + " more", 86, False, id="extra-text"),
    pytest.param(lambda s: QAW3_RESIDUE[:600], 86, False, id="prefix-only"),
    pytest.param(lambda s: QAW3_RESIDUE + "\n" + QAW3_RESIDUE, 86, False, id="twice-padopt"),
    pytest.param(lambda s: QAW3_RESIDUE.replace("argument,\n  so they", "argument, so they", 1),
                 86, False, id="hard-break-joined"),
    pytest.param(lambda s: QAW3_RESIDUE.replace("only points to it.\n\n", "only points to it.\n", 1),
                 86, False, id="blank-line-dropped"),
    pytest.param(lambda s: QAW3_RESIDUE.replace("i1-a0-planner.json", "i1-a1-planner.json"),
                 86, False, id="other-answer-path"),
    pytest.param(lambda s: dispatchers._pending_input(_claude_box(s, width=70)), 68, True,
                 id="another-pane-width"),
])
def test_stalled_submit_box_compared_with_the_text_sent(box, width, expected):
    """Catches a comparison of the input box with the stalled prompt that is
    too loose (any of the negatives taken as the prompt -- Enter would submit
    text HoH did not send: one character, extra text, a prefix, the prompt
    twice, a joined or a missing line break, a break short of the box width,
    an unindented line, another answer path, the box read under a width it
    was not drawn at or an unknown one) or too strict (run qaw3's real
    residue -- two-space continuation lines, mid-word breaks at 86 columns
    under its 88-column rule -- not recognised, and the run blocks as before).
    Control: the same text in a pane of another width is the same text.
    The width is the pane's, read off its box rule (`_box_width`), never
    inferred from the text: a real line break in a short box is no wrap."""
    sent = _qaw3_sent()
    assert dispatchers._same_input(box(sent), sent, width) is expected
    assert dispatchers._box_width(_claude_box(sent)) == 86
    assert dispatchers._box_width(_claude_box(sent, width=70)) == 68
    # A line break typed into a box narrower than the pane is not a wrap
    # (ssspec r2: the width used to be the widest line of the box itself).
    assert dispatchers._same_input("abc\n  def", "abcdef", 86) is False
    assert dispatchers._same_input("abcdef", "abcdef", 86) is True


def _stalled_rig(root: Path, monkeypatch, *, prompt: str = "p", answer: bool = True,
                 after: str = "working", **options):
    """A dispatcher whose first prompt stalls and stays typed in the box.

    Status reads: 1 (`_settle`) idle, 2 (before Enter) idle in this run's own
    pane, 3 (after Enter) `after`, then idle -- writing the answer when
    `answer`. The box shows the text this dispatch sent until Enter is
    pressed, then it is empty."""
    rig = Rig(root, monkeypatch, **{**_AWAIT, **options})
    rig.d.own_panes = [TARGET]
    full = rig.full(prompt)
    rig.sent = full
    if len(full.encode("utf-8")) > dispatchers.PROMPT_FILE_THRESHOLD:
        rig.sent = (_pointer_prompt(rig.d.answers_dir.resolve() / rig.prompt_file().name,
                                    len(full.encode("utf-8")), _sha(full))
                    + _answer_instruction(rig.answer))
    rig.herdr.on_prompt = lambda n: _error("agent_prompt_stalled") if n == 1 else None
    rig.herdr.on_read = lambda n: None if _send_keys(rig) else _claude_box(rig.sent)

    def gets(n):
        if n <= 2:
            return _reply("idle")
        if n == 3:
            return _reply(after)
        if answer:
            rig.write_answer()
        return _reply("idle")

    rig.herdr.on_get = gets
    return rig


@pytest.mark.parametrize("prompt", [pytest.param("p", id="short"),
                                    pytest.param("x" * 70000, id="long")])
def test_stalled_submit_enter_pressed_once_on_its_own_prompt(tmp_path, monkeypatch, prompt):
    """Catches the residue guard blocking a run on its own stalled prompt
    (qaw3, qaw4: the box held exactly HoH's pointer, and the run blocked), and
    an Enter that is a second prompt in disguise. The controller's stall retry
    finds the box holding exactly this dispatch's text: Enter is pressed once
    (`herdr pane send-keys <pane> Enter`), the turn is awaited, and the
    answer is collected -- one `agent prompt` in all, the mark and the stall
    record spent. Control: the residue of another prompt in the box is refused
    exactly as before, and nothing is pressed."""
    rig = _stalled_rig(tmp_path / "own", monkeypatch, prompt=prompt)
    result, exc = rig.run(prompt=prompt)
    assert exc is None and result == ANSWER, repr(exc)
    assert len(rig.herdr.prompts) == 1
    assert _send_keys(rig) == [["herdr", "pane", "send-keys", TARGET, "Enter"]]
    # Right before Enter: the status of the agent, then the box of the very
    # pane Enter goes to (ssspec r2).
    enter = rig.herdr.calls.index(_send_keys(rig)[0])
    assert rig.herdr.calls[enter - 2][1:3] == ["agent", "get"]
    assert rig.herdr.calls[enter - 1] == ["herdr", "pane", "read", TARGET, "--source", "visible"]
    assert "Enter is pressed once" in rig.notes()
    assert rig.d._unsettled() == {} and rig.d._stalls() == {}
    # The turn gets Herdr's own five seconds to show it started.
    assert rig.clock.slept.count(dispatchers.SUBMIT_CONFIRM_SECONDS) == 1
    assert dispatchers.SUBMIT_CONFIRM_SECONDS == 5.0

    control = _stalled_rig(tmp_path / "residue", monkeypatch, prompt=prompt)
    control.herdr.on_read = lambda n: RESIDUE_PANE
    _, exc = control.run(prompt=prompt)
    assert type(exc) is PaneNotClean, repr(exc)
    assert "Enter alone is not pressed" in str(exc)
    assert _send_keys(control) == [] and len(control.herdr.prompts) == 1


def test_stalled_submit_keeps_the_stalled_send_time(tmp_path, monkeypatch):
    """Catches the Enter path treated as a fresh send: the role timeout and
    the `AnswerMissing` must count from the stalled send, as for an awaited
    turn (K5). Control: without Enter (an empty box) the prompt is sent again
    and the time is the new send's."""
    rig = _stalled_rig(tmp_path / "enter", monkeypatch, answer=False)
    rig.dispatch()
    original = rig.d._unsettled()[TARGET][1]
    _, exc = rig.dispatch()
    assert type(exc) is AnswerMissing, repr(exc)
    assert exc.sent_at == original
    assert len(rig.herdr.prompts) == 1 and len(_send_keys(rig)) == 1

    control = _stalled_rig(tmp_path / "empty", monkeypatch, answer=False)
    control.herdr.on_read = lambda n: None
    control.dispatch()
    first = control.d._unsettled()[TARGET][1]
    control.dispatch()
    assert len(control.herdr.prompts) == 2 and _send_keys(control) == []
    assert control.d._unsettled() == {} or control.d._unsettled()[TARGET][1] > first
    # A send that returned spends the stall record: no later Enter rides on it.
    assert control.d._stalls() == {}


def _tamper_marker(rig, field: str = "bytes"):
    path = rig.answer.with_name(rig.prompt_file().name[:-len(".md")] + ".json")
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta[field] = {"bytes": meta["bytes"] + 1, "prompt_sha256": "0" * 64,
                   "prompt_path": meta["prompt_path"] + ".elsewhere"}[field]
    path.write_text(json.dumps(meta) + "\n", encoding="utf-8")


#: Cases that reach `_submit_stalled` and must spend the stall record there,
#: whatever refuses afterwards.
_SPENT = {"foreign-pane", "busy-before-enter", "blocked-before-enter",
          "unknown-before-enter", "status-unreadable-before-enter",
          "prompt-marker-changed", "marker-sha-changed", "marker-path-changed",
          "box-changed-before-enter"}


@pytest.mark.parametrize("case", [
    "other-prompt", "not-a-stall", "stall-then-other-failure", "foreign-pane",
    "busy-before-enter", "blocked-before-enter", "unknown-before-enter",
    "status-unreadable-before-enter", "prompt-marker-changed", "marker-sha-changed",
    "marker-path-changed", "box-changed-before-enter",
])
def test_stalled_submit_refused_unless_exactly_this_send(tmp_path, monkeypatch, case):
    """Catches an Enter pressed without every proof: the box holds the text
    of an earlier, different prompt (padopt's case); the send failed for
    another reason than a stall, or a later send of the prompt failed for
    another reason (no stall record left); the agent now sits in a pane that
    is not this run's; it is busy, at a dialog, in an unknown state, or its
    status cannot be read; the prompt marker no longer names this prompt
    (bytes, digest or path); or the box changed between the check and Enter.
    A dialog is `WaitingForApproval` -- a blocked dialog is never answered
    automatically -- the rest is the residue refusal of before
    (`PaneNotClean`, "Enter alone is not pressed"). No Enter, no second
    prompt, and a stall record that reached the check is spent. Control: the
    same rig without the change presses Enter once."""
    long_prompt = "x" * 70000
    rig = _stalled_rig(tmp_path / case, monkeypatch, prompt=long_prompt)
    prompts = 1
    if case == "other-prompt":
        rig.dispatch(prompt="y" * 70000)          # a different prompt stalls ...
        rig.herdr.on_read = lambda n: _claude_box(rig.sent)   # ... the box shows ours
    elif case == "not-a-stall":
        rig.herdr.on_prompt = lambda n: _error("agent_not_found") if n == 1 else None
        rig.dispatch(prompt=long_prompt)
    elif case == "stall-then-other-failure":
        rig.herdr.on_prompt = lambda n: {1: _error("agent_prompt_stalled"),
                                         2: _error("agent_not_found")}.get(n)
        rig.herdr.on_read = lambda n: None if n == 1 else _claude_box(rig.sent)
        rig.herdr.on_get = lambda n: _reply("idle")
        rig.dispatch(prompt=long_prompt)          # stalls: a stall record
        rig.dispatch(prompt=long_prompt)          # empty box, sent again, fails otherwise
        prompts = 2
    else:
        rig.dispatch(prompt=long_prompt)
    if case == "foreign-pane":
        rig.d.own_panes = ["w0:p77"]
    elif case in ("busy-before-enter", "blocked-before-enter", "unknown-before-enter"):
        late = {"busy-before-enter": "working", "blocked-before-enter": "blocked",
                "unknown-before-enter": "unknown"}[case]
        rig.herdr.on_get = lambda n: _reply("idle" if n == 1 else late)
    elif case == "status-unreadable-before-enter":
        rig.herdr.on_get = lambda n: _reply("idle") if n == 1 else _error("server_busy")
    elif case == "prompt-marker-changed":
        _tamper_marker(rig)
    elif case == "marker-sha-changed":
        _tamper_marker(rig, "prompt_sha256")
    elif case == "marker-path-changed":
        _tamper_marker(rig, "prompt_path")
    elif case == "box-changed-before-enter":
        rig.herdr.on_read = lambda n: _claude_box(rig.sent if n == 1 else rig.sent + " more")
    _, exc = rig.dispatch(prompt=long_prompt)
    if case == "blocked-before-enter":
        assert type(exc) is WaitingForApproval, repr(exc)
        assert "not answered automatically" in str(exc)
    else:
        assert type(exc) is PaneNotClean, repr(exc)
        assert "Enter alone is not pressed" in str(exc)
    assert _send_keys(rig) == []
    assert len(rig.herdr.prompts) == prompts
    if case in _SPENT:
        assert rig.d._stalls() == {}

    control = _stalled_rig(tmp_path / "control", monkeypatch, prompt=long_prompt)
    control.dispatch(prompt=long_prompt)
    result, exc = control.dispatch(prompt=long_prompt)
    assert exc is None and result == ANSWER, repr(exc)
    assert len(_send_keys(control)) == 1 and len(control.herdr.prompts) == 1


def test_stalled_submit_in_an_adopted_pane(tmp_path, monkeypatch):
    """Catches the own-pane check knowing only the panes this process
    started: a pane Herdr restored and this process adopted is the run's own
    too (`_adopted()`), and its stalled prompt gets its Enter. Control: a
    pane that is neither started nor adopted gets none."""
    rig = _stalled_rig(tmp_path / "adopted", monkeypatch)
    rig.d.own_panes = []
    rig.d._adopted()["hoh-qa"] = ("w0:t1", TARGET)
    rig.dispatch()
    result, exc = rig.dispatch()
    assert exc is None and result == ANSWER, repr(exc)
    assert _send_keys(rig) == [["herdr", "pane", "send-keys", TARGET, "Enter"]]

    control = _stalled_rig(tmp_path / "neither", monkeypatch)
    control.d.own_panes = []
    control.dispatch()
    _, exc = control.dispatch()
    assert type(exc) is PaneNotClean and _send_keys(control) == []


def test_stalled_submit_prompt_file_changed_before_enter(tmp_path, monkeypatch):
    """Catches Enter pressed on a pointer whose prompt file changed after the
    stall: what the role would read is no longer what was asked. The
    tamper refusal wins (non-transient, names the prompt file), and nothing
    is pressed. Control: the intact file gets its Enter."""
    long_prompt = "x" * 70000
    rig = _stalled_rig(tmp_path / "tampered", monkeypatch, prompt=long_prompt)
    rig.dispatch(prompt=long_prompt)
    rig.tamper()
    _, exc = rig.dispatch(prompt=long_prompt)
    assert type(exc) is DispatchError and exc.transient is False, repr(exc)
    assert rig.prompt_file().name in str(exc) and "before Enter" in str(exc)
    assert _send_keys(rig) == []

    control = _stalled_rig(tmp_path / "intact", monkeypatch, prompt=long_prompt)
    control.dispatch(prompt=long_prompt)
    result, exc = control.dispatch(prompt=long_prompt)
    assert exc is None and result == ANSWER and len(_send_keys(control)) == 1


@pytest.mark.parametrize("case", ["still-typed", "status-unknown", "status-unreadable",
                                  "enter-fails"])
def test_stalled_submit_second_stall_after_enter_refused(tmp_path, monkeypatch, case):
    """Catches a loop of Enters, or a turn assumed after an Enter that
    started none: the prompt still typed in the box after Enter, a status
    that proves no turn, an unreadable status, or a failed `send-keys`.
    `PaneNotClean` naming Enter, exactly one Enter, the mark kept so a later
    dispatch checks the pane again -- and that later dispatch presses
    nothing (the stall record is spent). Control: a turn that starts is
    awaited and collected."""
    rig = _stalled_rig(tmp_path / case, monkeypatch, after={
        "still-typed": "idle", "status-unknown": "unknown",
        "status-unreadable": "idle", "enter-fails": "idle"}[case])
    if case == "still-typed":
        rig.herdr.on_read = lambda n: _claude_box(rig.sent)
    if case == "status-unreadable":
        rig.herdr.on_get = lambda n: _reply("idle") if n <= 2 else _error("server_busy")
    if case == "enter-fails":
        inner = rig.herdr

        def run(args, **kwargs):
            reply = inner(args, **kwargs)
            return _error("agent_not_found") if args[1:3] == ["pane", "send-keys"] else reply

        monkeypatch.setattr(dispatchers, "subprocess", types.SimpleNamespace(
            run=run, TimeoutExpired=subprocess.TimeoutExpired))
    rig.dispatch()
    _, exc = rig.dispatch()
    assert type(exc) is PaneNotClean, repr(exc)
    assert "Enter" in str(exc) and "Nothing more is pressed" in str(exc)
    assert len(_send_keys(rig)) == 1 and len(rig.herdr.prompts) == 1
    assert TARGET in rig.d._unsettled() and rig.d._stalls() == {}
    rig.herdr.on_read = lambda n: _claude_box(rig.sent)
    rig.herdr.on_get = lambda n: _reply("idle")
    _, exc = rig.dispatch()
    assert type(exc) is PaneNotClean and "Enter alone is not pressed" in str(exc)
    assert len(_send_keys(rig)) == 1 and len(rig.herdr.prompts) == 1

    control = _stalled_rig(tmp_path / "control", monkeypatch)
    control.dispatch()
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER and len(_send_keys(control)) == 1


@pytest.mark.parametrize("after", ["done", "blocked"])
def test_stalled_submit_turn_that_ends_or_asks_at_once(tmp_path, monkeypatch, after):
    """Catches the two outcomes after Enter that are not `working`
    mishandled: a turn that already ended (`done`, the box empty, the answer
    written) must be collected, with the mark spent; a turn that stopped at a
    dialog (`blocked`, nothing delivered) is a `WaitingForApproval`, never
    answered and never a second Enter. Control: a still-typed prompt after
    Enter is refused (the turn never started)."""
    rig = _stalled_rig(tmp_path / after, monkeypatch, after=after, answer=False)
    if after == "done":
        base = rig.herdr.on_get

        def gets(n):
            if n == 3:
                rig.write_answer()
            return base(n)

        rig.herdr.on_get = gets
    rig.dispatch()
    result, exc = rig.dispatch()
    if after == "done":
        assert exc is None and result == ANSWER, repr(exc)
        assert rig.d._unsettled() == {}
    else:
        assert type(exc) is WaitingForApproval, repr(exc)
    assert len(_send_keys(rig)) == 1 and len(rig.herdr.prompts) == 1

    control = _stalled_rig(tmp_path / "typed", monkeypatch, after="idle")
    control.herdr.on_read = lambda n: _claude_box(control.sent)
    control.dispatch()
    _, exc = control.dispatch()
    assert type(exc) is PaneNotClean and len(_send_keys(control)) == 1


# --------------------------------------------------------------------------- #
# Stalled submit, Revision 2.2 (tests only): the Enter boundary
# (integration review sss-a: B1 and B2, and the MINOR survivors)
# --------------------------------------------------------------------------- #

#: The pane the stalled agent runs in. In production `target` is the agent's
#: name and `agent get` reports its pane id. `_reply` reports the target as the
#: pane id, so a call addressed to the wrong one of the two cannot be told apart
#: there; the tests below use `_pane_rig`, whose agent is in OWN_PANE.
OWN_PANE = "w0:p5"

#: A dialog where the input box should be: no box at all in the view.
DIALOG_VIEW = "Do you want to proceed?\n\u276f 1. Yes\n  2. No\n\nEsc to cancel\n"

#: A view cut off above the box (run qaw4, 17:46:25Z): only the bottom rule.
CUT_OFF_VIEW = ("  [answer-file]\n  Write your JSON answer **as a file** to:\n"
                f"{RULE}\n  \u23f5\u23f5 auto mode on (shift+tab to cycle)\n")


def _pane_rig(root: Path, monkeypatch, *, pane: str = OWN_PANE, **options):
    """`_stalled_rig`, with the agent in pane `pane`, which is not its target:
    every `agent get` reports that pane id, and it is this run's own pane."""
    rig = _stalled_rig(root, monkeypatch, **options)
    rig.d.own_panes = [pane]
    inner = rig.herdr

    def run(args, **kwargs):
        reply = inner(args, **kwargs)
        if args[1:3] == ["agent", "get"] and reply.returncode == 0:
            data = json.loads(reply.stdout)
            data["result"]["agent"]["pane_id"] = pane
            return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(data))
        return reply

    monkeypatch.setattr(dispatchers, "subprocess", types.SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired))
    return rig


@pytest.mark.parametrize("how", ["started", "adopted"])
def test_stalled_submit_enter_goes_to_the_pane_that_was_checked(tmp_path, monkeypatch, how):
    """Catches an Enter, a re-read of the box or an own-pane check addressed to
    the agent's target instead of the pane id `agent get` just reported. The
    rig of the S-tests reports the target as the pane, so each of the three
    could be swapped for the other unnoticed; in production the target is the
    agent's name. Here the agent is in OWN_PANE, never in its target: the box
    is read from OWN_PANE right after the status read, Enter goes to OWN_PANE,
    the own-pane test is made on OWN_PANE (a pane this process started, or one
    it adopted), the note names it, and no `pane` command names anything else.
    Control: a run whose own panes hold the agent's target but not its pane
    gets no Enter."""
    assert OWN_PANE != TARGET
    rig = _pane_rig(tmp_path / how, monkeypatch)
    if how == "adopted":
        rig.d.own_panes = []
        rig.d._adopted()["hoh-qa"] = ("w0:t1", OWN_PANE)
    result, exc = rig.run(prompt="p")
    assert exc is None and result == ANSWER, repr(exc)
    assert len(rig.herdr.prompts) == 1
    assert _send_keys(rig) == [["herdr", "pane", "send-keys", OWN_PANE, "Enter"]]
    enter = rig.herdr.calls.index(_send_keys(rig)[0])
    assert rig.herdr.calls[enter - 2] == ["herdr", "agent", "get", TARGET]
    assert rig.herdr.calls[enter - 1] == ["herdr", "pane", "read", OWN_PANE, "--source", "visible"]
    assert {c[3] for c in rig.herdr.calls if c[1] == "pane"} == {OWN_PANE}
    assert f"pane {OWN_PANE}: Enter is pressed once" in rig.notes()

    control = _pane_rig(tmp_path / "target-only", monkeypatch)
    control.d.own_panes = [TARGET]
    _, exc = control.run(prompt="p")
    assert type(exc) is PaneNotClean, repr(exc)
    assert "Enter alone is not pressed" in str(exc)
    assert _send_keys(control) == [] and len(control.herdr.prompts) == 1


@pytest.mark.parametrize("case", ["dialog", "cut-off-box", "other-agent-screen", "read-fails"])
def test_stalled_submit_nothing_pressed_when_the_reread_finds_no_box(tmp_path, monkeypatch, case):
    """Catches an Enter pressed after the box was read again right before it
    and found unreadable: a dialog where the box was (the agent asked
    something between the first check and now), a view cut off above the box,
    another agent's screen, or a read that failed. What Enter would submit is
    unknown then, so nothing is pressed. The refusal is the residue refusal of
    before (`PaneNotClean`, "Enter alone is not pressed"), the last call is
    the re-read of the very pane Enter would have gone to, the stall record is
    spent, and nothing is sent again. Control: the same rig with the box
    intact presses Enter once."""
    view = {"dialog": DIALOG_VIEW, "cut-off-box": CUT_OFF_VIEW,
            "other-agent-screen": OTHER_SCREEN, "read-fails": None}[case]
    rig = _pane_rig(tmp_path / case, monkeypatch)
    # Read 1 is `_settle`'s (the box holds exactly our text), read 2 is the
    # re-read right before Enter.
    rig.herdr.on_read = lambda n: (_claude_box(rig.sent) if n == 1 else
                                   _error("pane_not_found") if view is None else view)
    _, exc = rig.run(prompt="p")
    assert type(exc) is PaneNotClean, repr(exc)
    assert "Enter alone is not pressed" in str(exc)
    assert _send_keys(rig) == [] and len(rig.herdr.prompts) == 1
    assert rig.herdr.calls[-1] == ["herdr", "pane", "read", OWN_PANE, "--source", "visible"]
    assert rig.d._stalls() == {}

    control = _pane_rig(tmp_path / "control", monkeypatch)
    result, exc = control.run(prompt="p")
    assert exc is None and result == ANSWER, repr(exc)
    assert len(_send_keys(control)) == 1 and len(control.herdr.prompts) == 1


@pytest.mark.parametrize("case", ["idle-dialog", "done-dialog", "idle-read-fails"])
def test_stalled_submit_idle_after_enter_needs_an_empty_readable_box(tmp_path, monkeypatch, case):
    """Catches an idle (or done) agent accepted after Enter although its input
    box cannot be read: the view shows a dialog instead of a box, or the read
    fails. Only an empty box proves the prompt was submitted. `PaneNotClean`
    naming Enter and "not readable", exactly one Enter, the mark kept so a
    later dispatch checks the pane again, the stall record spent. Control: the
    same rig with the box empty after Enter and the turn done is collected."""
    after = case.split("-")[0]

    rig = _pane_rig(tmp_path / case, monkeypatch, after=after)
    rig.herdr.on_read = lambda n: (_claude_box(rig.sent) if not _send_keys(rig) else
                                   _error("pane_not_found") if case.endswith("read-fails")
                                   else DIALOG_VIEW)
    rig.dispatch()
    _, exc = rig.dispatch()
    assert type(exc) is PaneNotClean, repr(exc)
    assert "not readable" in str(exc) and "Nothing more is pressed" in str(exc)
    assert len(_send_keys(rig)) == 1 and len(rig.herdr.prompts) == 1
    assert TARGET in rig.d._unsettled() and rig.d._stalls() == {}

    control = _pane_rig(tmp_path / "control", monkeypatch, after="done", answer=False)
    base = control.herdr.on_get

    def gets(n):
        if n == 3:
            control.write_answer()
        return base(n)

    control.herdr.on_get = gets
    control.dispatch()
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER, repr(exc)
    assert len(_send_keys(control)) == 1 and len(control.herdr.prompts) == 1


@pytest.mark.parametrize("failure", ["timeout", "os-error"])
def test_stalled_submit_only_a_stall_licenses_enter(tmp_path, monkeypatch, failure):
    """Catches every transient failure of the send recorded as a stall. Only
    `agent_prompt_stalled` carries a back-off (`_cli`); a call that timed out,
    or could not be run at all, is transient too, but it is no stall: the
    prompt may or may not have been typed, and a box that happens to hold
    exactly the text is no licence for an Enter. `PaneNotClean`, "Enter alone
    is not pressed", no Enter, no second prompt. Control: the same rig whose
    send stalls gets its Enter."""
    import errno

    raised = {"timeout": subprocess.TimeoutExpired(cmd="herdr", timeout=1.0),
              "os-error": OSError(errno.EIO, "Input/output error")}[failure]
    rig = _pane_rig(tmp_path / failure, monkeypatch)
    rig.herdr.on_prompt = lambda n: raised if n == 1 else None
    _, exc = rig.dispatch()
    assert type(exc) is DispatchError, repr(exc)
    assert exc.transient is True and not exc.retry_after
    assert rig.d._stalls() == {}
    _, exc = rig.dispatch()
    assert type(exc) is PaneNotClean, repr(exc)
    assert "Enter alone is not pressed" in str(exc)
    assert _send_keys(rig) == [] and len(rig.herdr.prompts) == 1

    control = _pane_rig(tmp_path / "control", monkeypatch)
    control.dispatch()
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER, repr(exc)
    assert len(_send_keys(control)) == 1 and len(control.herdr.prompts) == 1


def test_stalled_submit_timeout_counts_from_the_stalled_send(tmp_path, monkeypatch):
    """Catches the role timeout after Enter counted from Enter instead of from
    the stalled send, which would give an agent that was prompted long ago a
    whole new timeout. The stalled send is 3300 s old at Enter (the role
    timeout is 3600 s), and the agent keeps working for ten polls (1000 s): the
    wait ends at the role timeout, `DispatchError` (not transient) "still
    working at the role timeout", one Enter, no second prompt, no answer
    taken. Control: the same turn, with the stalled send recent, is awaited
    and its answer collected, and so is a turn of 25 polls (2500 s, more than
    half the timeout: the wait is not cut short either)."""
    def attempt(root: Path, late: float, polls: int = 10):
        rig = _pane_rig(root, monkeypatch, answer=False)

        def gets(n):
            if n <= 2:
                return _reply("idle")
            if n <= 2 + polls:
                return _reply("working")
            rig.write_answer()
            return _reply("idle")

        rig.herdr.on_get = gets
        rig.dispatch()
        rig.clock.now += late          # the stalled send was `late` seconds ago
        return rig, rig.dispatch()

    rig, (result, exc) = attempt(tmp_path / "late", 3300.0)
    assert result is None and type(exc) is DispatchError, repr(exc)
    assert exc.transient is False and "still working at the role timeout" in str(exc)
    assert len(_send_keys(rig)) == 1 and len(rig.herdr.prompts) == 1
    assert len(rig.herdr.gets) < 12

    control, (result, exc) = attempt(tmp_path / "recent", 0.0)
    assert exc is None and result == ANSWER, repr(exc)
    assert len(_send_keys(control)) == 1 and len(control.herdr.prompts) == 1

    long_turn, (result, exc) = attempt(tmp_path / "most-of-it", 0.0, polls=25)
    assert exc is None and result == ANSWER, repr(exc)
    assert len(_send_keys(long_turn)) == 1 and len(long_turn.herdr.prompts) == 1


@pytest.mark.parametrize("sent, box, width, above, expected", [
    pytest.param("A" * 200, None, None, False, True, id="first-line-wraps"),
    pytest.param("A" * 84, None, None, False, True, id="first-line-exactly-the-width"),
    pytest.param("A" * 85, "A" * 85, 86, False, False, id="first-line-one-column-too-wide"),
    pytest.param("A\n" + "B" * 85, "A\n  " + "B" * 85, 86, False, False,
                 id="continuation-line-one-column-too-wide"),
    pytest.param("A" * 200, None, None, True, True, id="an-earlier-rule-above-the-box"),
])
def test_stalled_submit_box_lines_are_measured_against_the_pane_width(sent, box, width,
                                                                       above, expected):
    """Catches a box comparison that loses a width rule. A line of the box
    (counting the two-space indentation, and the input mark on the first line)
    may not be wider than the pane's width, and a first line that wraps wraps
    at exactly that width: the mark's two columns count on the first line (86
    columns of an 88-column pane), so a first line of 85 characters is no box
    the pane drew, a continuation line of 85 characters is none either, and a
    200-character first line is one box line of 84 characters plus the rest.
    The width is the last rule of the view, the bottom rule of the box, not an
    earlier rule of the transcript above it (`above`). Control: a first line of
    exactly 84 characters is the same text."""
    view = _claude_box(sent)
    if above:
        view = "\u2500" * 20 + "\n" + view
    if box is None:
        box = dispatchers._pending_input(view)
    if width is None:
        width = dispatchers._box_width(view)
        assert width == 86
    assert dispatchers._same_input(box, sent, width) is expected


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_stalled_submit_prompt_marker_must_be_the_regular_file_written(tmp_path, monkeypatch, kind):
    """Catches the prompt marker read through a link, or read from something
    that is not a regular file, before an Enter on a pointer. The marker is
    read link-free relative to the answers directory, like every leaf of a
    dispatch. Here it is replaced, after the stall, by a symlink to a file
    holding its exact record, or by a FIFO whose live writer holds that
    record: both would say what the dispatcher expects, and neither is the
    file it wrote. `PaneNotClean` "Enter alone is not pressed", no Enter, no
    second prompt. Control: the intact marker gets its Enter."""
    long_prompt = "x" * 70000
    rig = _pane_rig(tmp_path / kind, monkeypatch, prompt=long_prompt)
    rig.dispatch(prompt=long_prompt)
    marker = rig.answer.with_name(rig.prompt_file().name[:-len(".md")] + ".json")
    record = marker.read_bytes()
    stand_in = marker.with_name(marker.name + ".stand-in")
    writer = None
    if kind == "symlink":
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_bytes(record)
        stand_in.symlink_to(elsewhere)
    else:
        os.mkfifo(stand_in)
        writer = os.open(stand_in, os.O_RDWR | os.O_NONBLOCK)
        os.write(writer, record)
    os.replace(stand_in, marker)
    try:
        _, exc = rig.dispatch(prompt=long_prompt)
    finally:
        if writer is not None:
            os.close(writer)
    assert type(exc) is PaneNotClean, repr(exc)
    assert "Enter alone is not pressed" in str(exc)
    assert _send_keys(rig) == [] and len(rig.herdr.prompts) == 1

    control = _pane_rig(tmp_path / "control", monkeypatch, prompt=long_prompt)
    control.dispatch(prompt=long_prompt)
    result, exc = control.dispatch(prompt=long_prompt)
    assert exc is None and result == ANSWER, repr(exc)
    assert len(_send_keys(control)) == 1 and len(control.herdr.prompts) == 1


def test_stalled_submit_marker_read_does_not_block_on_a_writerless_fifo(tmp_path):
    """Catches the prompt marker opened without `O_NONBLOCK`: a FIFO planted at
    its name, with no writer, would then hang the dispatcher in `open` for
    ever (S15's FIFO has a live writer, which lets even a blocking open
    through). The read is bounded and gives None. The read runs in a thread
    here, so a regression fails after two real seconds instead of hanging the
    run; the stuck thread is released by opening the write end."""
    import threading

    os.mkfifo(tmp_path / "marker.json")
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    out: list = []
    reader = threading.Thread(
        target=lambda: out.append(dispatchers._read_json_leaf(dir_fd, "marker.json")), daemon=True)
    reader.start()
    reader.join(2.0)
    blocked = reader.is_alive()
    if blocked:
        os.close(os.open(tmp_path / "marker.json", os.O_WRONLY | os.O_NONBLOCK))
        reader.join(5.0)
    os.close(dir_fd)
    assert not blocked, "the marker read blocked on a FIFO nobody writes to"
    assert out == [None]


def test_stalled_submit_status_read_without_a_pane_id_presses_nothing(tmp_path, monkeypatch):
    """Catches a status read that names no pane taken as the agent's own pane
    by falling back to its target. The own-pane proof needs the pane id `agent
    get` reports; without one the pane is unknown, and nothing is pressed even
    when the target happens to be listed among the own panes (the rig of the
    S-tests lists it). `PaneNotClean`, "Enter alone is not pressed", no
    Enter, no second prompt. Control: the same rig whose status read names its
    pane gets its Enter."""
    rig = _stalled_rig(tmp_path / "unnamed", monkeypatch)
    nameless = types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
        {"result": {"agent": {"agent_status": "idle", "workspace_id": "w0",
                              "tab_id": "w0:t1", "agent": "claude", "cwd": "/x"}}}))
    rig.herdr.on_get = lambda n: _reply("idle") if n == 1 else nameless
    rig.dispatch()
    _, exc = rig.dispatch()
    assert type(exc) is PaneNotClean, repr(exc)
    assert "Enter alone is not pressed" in str(exc)
    assert _send_keys(rig) == [] and len(rig.herdr.prompts) == 1

    control = _stalled_rig(tmp_path / "control", monkeypatch)
    control.dispatch()
    result, exc = control.dispatch()
    assert exc is None and result == ANSWER, repr(exc)
    assert len(_send_keys(control)) == 1 and len(control.herdr.prompts) == 1
