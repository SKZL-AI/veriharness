"""P2-17: the agent-control seam is a real interface, the product's dispatch
path goes through it, and a fake stands behind it as well as Herdr does.

Four things are shown, each by running the code and not by reading it:

* the contract (`AgentControlAdapter`) holds for the Herdr implementation and
  for the fake alike -- the same scenario, the same typed answers;
* `HerdrAgentControl` spells exactly the Herdr commands the dispatcher used to
  spell (golden argv), so nothing the dispatcher's other tests fake has moved;
* `HerdrDispatcher` run over the fake -- a real dispatch, answer file and all --
  starts no process and calls nothing of `hoh.herdr`: the verbs the fake logged
  are the only thing that happened;
* `dispatchers.py` spells no Herdr command and calls no `herdr.<function>` at
  all: the only door out of the dispatcher is `self.control`.

No test starts Herdr or an agent. Every one runs with `subprocess.run` inside
`hoh.dispatchers` and `hoh.herdr` replaced by something that fails the test.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from hoh import dispatchers, herdr, procs
from hoh.contracts import Budgets, Role, RunState
from hoh.controller import DispatchError
from hoh.dispatchers import HerdrDispatcher

WS = "w1"
BOX_RULE, INPUT_MARK = "\u2500", "\u276f"
SRC = Path(dispatchers.__file__).resolve().parent
ROOT = SRC.parent.parent


def ac():
    """`hoh.agentcontrol`, imported when a test needs it and not when the file
    is collected: the tests that do not need it (the dispatcher's own, and the
    structural ones) then fail on a tree without the seam for what they say,
    not for a missing module."""
    return importlib.import_module("hoh.agentcontrol")


# --------------------------------------------------------------------------- #
# The fake: an in-memory agent runtime that speaks the interface, not Herdr
# --------------------------------------------------------------------------- #


class FakeAgentControl:
    """Panes, tabs and agents in memory; every verb is logged in `calls`.

    `on_prompt(name, text)` is what the agent does in its turn (a role writes
    its answer file). `fail` maps a verb name to the `DispatchError` it raises.
    A standing pane `CALLER` is always listed, as a real snapshot lists the
    pane `hoh run` sits in.
    """

    backend = "fake"
    CALLER = f"{WS}:p0"

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.panes: dict[str, dict] = {
            self.CALLER: {"pane_id": self.CALLER, "tab_id": f"{WS}:t0",
                          "workspace_id": WS, "cwd": "/", "foreground_cwd": "/"}}
        self.agents: dict[str, dict] = {}
        self.on_prompt = None
        self.fail: dict[str, Exception] = {}
        #: (verb, purpose) of every call that carries one, in order.
        self.purposes: list[tuple[str, str]] = []
        self.turns = 0
        #: Claude Code's empty input box: what a fresh agent shows, and what a
        #: dispatcher that waits for the box before a first prompt reads.
        self.screen_text = f"{BOX_RULE * 40}\n{INPUT_MARK} \n{BOX_RULE * 40}\n"
        self._n = 0

    def _log(self, verb: str, *details, purpose=None) -> None:
        self.calls.append((verb, *details))
        if purpose is not None:
            self.purposes.append((verb, purpose))
        if verb in self.fail:
            raise self.fail[verb]

    def _info(self, row: dict, workspace: str = WS) -> AgentInfo:
        return ac().AgentInfo(backend=self.backend, name=row["name"],
                              status=row["agent_status"], pane_id=row["pane_id"],
                              tab_id=row["tab_id"], workspace_id=workspace,
                              cwd=row["cwd"])

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]

    def open_surface(self, *, cwd, label, env=None, workspace=None, purpose="create tab"):
        self._log("open_surface", cwd, label, dict(env or {}), workspace, purpose=purpose)
        self._n += 1
        tab, pane = f"{WS}:t{self._n}", f"{WS}:p{self._n}"
        self.panes[pane] = {"pane_id": pane, "tab_id": tab, "workspace_id": WS,
                            "cwd": cwd, "foreground_cwd": cwd}
        return ac().Surface(pane_id=pane, tab_id=tab)

    def start(self, name, *, kind, pane_id, args=(), timeout_ms=240000, purpose=""):
        self._log("start", name, kind, pane_id, tuple(args), timeout_ms, purpose=purpose)
        pane = self.panes[pane_id]
        row = {"name": name, "pane_id": pane_id, "tab_id": pane["tab_id"],
               "agent": kind, "agent_status": "idle", "cwd": pane["cwd"],
               "foreground_cwd": pane["cwd"], "workspace_id": WS}
        self.agents[name] = row
        return self._info(row)

    def prompt(self, name, text, *, timeout_ms, purpose=""):
        self._log("prompt", name, text, timeout_ms, purpose=purpose)
        self.turns += 1
        if self.on_prompt is not None:
            self.on_prompt(name, text)
        # Each turn reports its own workspace id, so the evidence string a
        # dispatcher keeps shows *which* answer it was built from.
        return self._info(self.agents[name], workspace=f"{WS}-turn{self.turns}")

    def press(self, pane_id, key, *, purpose=""):
        self._log("press", pane_id, key, purpose=purpose)

    def status(self, name, *, purpose=""):
        self._log("status", name, purpose=purpose)
        return self._info(self.agents[name])

    def screen(self, name, *, purpose=""):
        self._log("screen", name, purpose=purpose)
        return self.screen_text

    def pane_screen(self, pane_id, *, source="visible", purpose=""):
        self._log("pane_screen", pane_id, source, purpose=purpose)
        if pane_id not in self.panes:
            raise DispatchError("pane_not_found")
        return self.screen_text

    def transcript(self, name, *, lines=120):
        self._log("transcript", name, lines)
        return self.screen_text

    def snapshot(self):
        self._log("snapshot")
        tabs: dict[str, int] = {}
        for p in self.panes.values():
            tabs[p["tab_id"]] = tabs.get(p["tab_id"], 0) + 1
        return {"panes": [dict(p) for p in self.panes.values()],
                "tabs": [{"tab_id": t, "pane_count": n} for t, n in tabs.items()],
                "agents": [dict(a) for a in self.agents.values()]}

    def close_pane(self, pane_id, *, purpose=""):
        self._log("close_pane", pane_id, purpose=purpose)
        self.panes.pop(pane_id)
        for name in [n for n, a in self.agents.items() if a["pane_id"] == pane_id]:
            self.agents.pop(name)

    def close_tab(self, tab_id, *, purpose=""):
        self._log("close_tab", tab_id, purpose=purpose)
        for pane in [p for p, r in self.panes.items() if r["tab_id"] == tab_id]:
            self.close_pane(pane)


class HerdrWire:
    """A tiny Herdr behind `HerdrAgentControl`'s transport: it understands
    exactly the commands the adapter spells and fails the test on any other."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.panes = {"w1:p0": "w1:t0"}
        self.agents: dict[str, dict] = {}

    def __call__(self, argv, purpose, *, text=False):
        argv = list(argv)
        self.argvs.append(argv)
        assert argv[0] == "herdr", argv
        verb = argv[1:3]
        if verb == ["tab", "create"]:
            n = len(self.panes)
            tab, pane = f"{WS}:t{n}", f"{WS}:p{n}"
            self.panes[pane] = tab
            return {"result": {"tab": {"tab_id": tab},
                               "root_pane": {"pane_id": pane, "tab_id": tab}}}
        if verb == ["agent", "start"]:
            name, pane = argv[3], argv[argv.index("--pane") + 1]
            row = {"name": name, "pane_id": pane, "tab_id": self.panes[pane],
                   "workspace_id": WS, "agent": argv[argv.index("--kind") + 1],
                   "agent_status": "idle", "cwd": "/work"}
            self.agents[name] = row
            return {"result": {"agent": dict(row)}}
        if verb in (["agent", "prompt"], ["agent", "get"]):
            return {"result": {"agent": dict(self.agents[argv[3]])}}
        if verb in (["agent", "read"], ["pane", "read"]):
            assert text
            return "screen of " + argv[3]
        if verb == ["pane", "send-keys"]:
            assert text
            return ""
        if verb in (["pane", "close"], ["tab", "close"]):
            return {"result": {}}
        raise AssertionError(f"the wire does not understand {argv!r}")


@pytest.fixture(params=["fake", "herdr-wire"])
def control(request, monkeypatch):
    """The same scenario over the fake and over `HerdrAgentControl`."""
    if request.param == "fake":
        return FakeAgentControl()
    wire = HerdrWire()
    adapter = ac().HerdrAgentControl(wire)
    monkeypatch.setattr(herdr, "snapshot", lambda: {
        "panes": [{"pane_id": p, "tab_id": t} for p, t in wire.panes.items()],
        "tabs": [], "agents": list(wire.agents.values())})
    monkeypatch.setattr(herdr, "read_agent", lambda target, lines=120: f"history of {target}")
    adapter.wire = wire
    return adapter


# --------------------------------------------------------------------------- #
# 1. The contract
# --------------------------------------------------------------------------- #


def test_both_implementations_are_agent_control_adapters(control):
    assert isinstance(control, ac().AgentControlAdapter)
    assert isinstance(control.backend, str) and control.backend


def test_the_scenario_a_dispatcher_lives_through_gives_the_same_typed_answers(control):
    """open -> start -> observe -> prompt -> press -> stop, on both backends."""
    surface = control.open_surface(cwd="/work", label="hoh planner",
                                   env={"A": "1"}, workspace=WS)
    assert isinstance(surface, ac().Surface) and surface.pane_id and surface.tab_id

    started = control.start("agent-a", kind="claude", pane_id=surface.pane_id,
                            args=("--model", "m"))
    assert isinstance(started, ac().AgentInfo)
    assert started.name == "agent-a" and started.pane_id == surface.pane_id
    assert started.tab_id == surface.tab_id and started.backend == control.backend
    assert started.evidence() == f"{control.backend}:{WS}/{surface.tab_id}/{surface.pane_id}"

    seen = control.status("agent-a")
    assert seen.status == "idle" and seen.pane_id == surface.pane_id

    snap = control.snapshot()
    assert surface.pane_id in {p["pane_id"] for p in snap["panes"]}
    assert "agent-a" in {a["name"] for a in snap["agents"]}

    after = control.prompt("agent-a", "do it", timeout_ms=1000)
    assert isinstance(after, ac().AgentInfo) and after.pane_id == surface.pane_id

    assert isinstance(control.screen("agent-a"), str)
    assert isinstance(control.pane_screen(surface.pane_id), str)
    assert isinstance(control.pane_screen(surface.pane_id, source=None), str)
    assert isinstance(control.transcript("agent-a", lines=5), str)
    assert control.press(surface.pane_id, "Enter") is None

    assert control.close_tab(surface.tab_id) is None
    if control.backend == "fake":
        assert surface.pane_id not in {p["pane_id"] for p in control.snapshot()["panes"]}


# --------------------------------------------------------------------------- #
# 2. What HerdrAgentControl spells (the dispatcher used to)
# --------------------------------------------------------------------------- #


def test_herdr_agent_control_spells_the_commands_the_dispatcher_spelled(monkeypatch):
    wire = HerdrWire()
    adapter = ac().HerdrAgentControl(wire)

    surface = adapter.open_surface(cwd="/work", label="hoh planner",
                                   env={"K1": "v1", "K2": "v2"}, workspace="w7")
    adapter.start("n1", kind="claude", pane_id=surface.pane_id,
                  args=("--model", "m"), timeout_ms=240000)
    adapter.start("n2", kind="kimi", pane_id=surface.pane_id)
    adapter.prompt("n1", "the text", timeout_ms=900000)
    adapter.status("n1")
    adapter.screen("n1")
    adapter.pane_screen("w1:p1")
    adapter.pane_screen("w1:p1", source=None)
    adapter.press("w1:p1", "Enter")
    adapter.press("w1:p1", "Escape")
    adapter.close_pane("w1:p1")
    adapter.close_tab("w1:t1")
    seen_lines = []
    monkeypatch.setattr(herdr, "read_agent",
                        lambda target, lines=120: seen_lines.append((target, lines)) or "t")
    adapter.transcript("n1", lines=5)
    adapter.transcript("n1")

    assert wire.argvs == [
        ["herdr", "tab", "create", "--cwd", "/work", "--label", "hoh planner",
         "--no-focus", "--env", "K1=v1", "--env", "K2=v2", "--workspace", "w7"],
        ["herdr", "agent", "start", "n1", "--kind", "claude", "--pane", "w1:p1",
         "--timeout", "240000", "--", "--model", "m"],
        ["herdr", "agent", "start", "n2", "--kind", "kimi", "--pane", "w1:p1",
         "--timeout", "240000"],
        ["herdr", "agent", "prompt", "n1", "the text", "--wait", "--timeout", "900000"],
        ["herdr", "agent", "get", "n1"],
        ["herdr", "agent", "read", "n1", "--source", "visible"],
        ["herdr", "pane", "read", "w1:p1", "--source", "visible"],
        ["herdr", "pane", "read", "w1:p1"],
        ["herdr", "pane", "send-keys", "w1:p1", "Enter"],
        ["herdr", "pane", "send-keys", "w1:p1", "Escape"],
        ["herdr", "pane", "close", "w1:p1"],
        ["herdr", "tab", "close", "w1:t1"],
    ]
    assert seen_lines == [("n1", 5), ("n1", 120)], "the history length is the caller's"


def test_herdr_agent_control_reads_what_herdr_answers_into_strings_never_none():
    def transport(argv, purpose, *, text=False):
        return {"result": {"agent": {"pane_id": "p", "agent_status": None}}}

    info = ac().HerdrAgentControl(transport).status("n")
    assert info == ac().AgentInfo(backend="herdr", name="n", status="", pane_id="p",
                             tab_id="", workspace_id="", cwd="")
    assert info.evidence() == "herdr://p"
    # An answer that is not even an object is an empty agent, not an exception.
    assert ac().HerdrAgentControl(lambda *a, **k: ["x"]).status("n").pane_id == ""


def test_a_tab_without_a_pane_is_refused_where_herdr_answers(monkeypatch):
    adapter = ac().HerdrAgentControl(lambda argv, purpose, **k: {"result": {"tab": {"tab_id": "t"}}})
    with pytest.raises(DispatchError, match="no pane"):
        adapter.open_surface(cwd="/w", label="l")


def test_a_failure_of_the_transport_reaches_the_caller_unchanged():
    boom = DispatchError("x failed", transient=True, retry_after=7.0)

    def transport(argv, purpose, **k):
        raise boom

    with pytest.raises(DispatchError) as caught:
        ac().HerdrAgentControl(transport).prompt("n", "t", timeout_ms=1)
    assert caught.value is boom


# --------------------------------------------------------------------------- #
# 3. A real dispatch over the fake
# --------------------------------------------------------------------------- #


def _no_process(monkeypatch) -> list[str]:
    """Anything that would start a process or reach `hoh.herdr` fails the test."""
    called: list[str] = []

    def fail(name):
        def refuse(*a, **k):
            called.append(name)
            raise AssertionError(f"{name} was called: the fake must be enough")
        return refuse

    monkeypatch.setattr(dispatchers, "subprocess", types.SimpleNamespace(
        run=fail("subprocess.run"), TimeoutExpired=Exception))
    for fn in ("snapshot", "read_agent", "agents", "agent_status", "liveness",
               "require_herdr", "worktree_create", "worktree_list"):
        monkeypatch.setattr(herdr, fn, fail(f"herdr.{fn}"))
    monkeypatch.setattr(herdr, "_cli", fail("herdr._cli"))
    monkeypatch.setattr(dispatchers.time, "sleep", lambda s: None)
    return called


def _dispatcher(tmp_path: Path, fake, **kwargs) -> tuple[HerdrDispatcher, RunState]:
    arena = tmp_path / "arena"
    arena.mkdir()
    answers = tmp_path / "run" / "answers"
    answers.mkdir(parents=True)
    d = HerdrDispatcher(answers_dir=answers, profiles={r: "claude" for r in Role},
                        cwd=arena, timeout_ms=1000, control=fake, **kwargs)
    state = RunState(run_id="r1", repo_path=str(arena), project_name="p",
                     spec_path=str(tmp_path / "spec.md"), spec_digest="d",
                     policy_digest="d", profile_digest="d", budgets=Budgets())
    return d, state


def test_a_dispatch_runs_through_the_control_and_nothing_else(tmp_path, monkeypatch):
    """Catches: any path of `HerdrDispatcher.dispatch` that still reaches Herdr
    directly (a `herdr` command line, `herdr.snapshot()`, a `subprocess.run`):
    the guards above make that fail the test. The dispatch is real -- tab,
    agent, prompt, the role's answer file, the evidence string."""
    called = _no_process(monkeypatch)
    fake = FakeAgentControl()
    d, state = _dispatcher(tmp_path, fake)
    answer = d._answer_path(Role.PLANNER, state)

    def planner_turn(name, text):
        assert str(answer) in text, "the role is told where to write its answer"
        answer.write_text(json.dumps({"ok": True}) + "\n", encoding="utf-8")

    fake.on_prompt = planner_turn

    out = d.dispatch(Role.PLANNER, "plan it", state=state)

    assert json.loads(out) == {"ok": True}
    assert called == []
    verbs = fake.verbs()
    assert verbs.index("open_surface") < verbs.index("start") < verbs.index("prompt")
    start = next(c for c in fake.calls if c[0] == "start")
    prompt = next(c for c in fake.calls if c[0] == "prompt")
    assert start[1] == prompt[1] == d.agents[Role.PLANNER], "one agent, started then prompted"
    assert start[2] == "claude" and prompt[3] == 1000
    surface = next(c for c in fake.calls if c[0] == "open_surface")
    assert surface[1] == str(d.cwd) and surface[2] == "hoh planner"
    assert d.endpoint_evidence(Role.PLANNER).startswith("fake:")


def test_the_dispatcher_stops_through_the_control_too(tmp_path, monkeypatch):
    called = _no_process(monkeypatch)
    fake = FakeAgentControl()
    d, state = _dispatcher(tmp_path, fake)
    fake.on_prompt = lambda name, text: d._answer_path(Role.PLANNER, state).write_text(
        '{"ok": true}\n', encoding="utf-8")
    d.dispatch(Role.PLANNER, "plan it", state=state)
    pane = d._role_panes()[Role.PLANNER]
    tab = fake.panes[pane]["tab_id"]

    gone = d.close_own()

    assert called == []
    assert ("close_tab", tab) in fake.calls or ("close_pane", pane) in fake.calls
    assert pane not in fake.panes and fake.CALLER in fake.panes
    assert gone in ([tab], [])


def test_a_stalled_prompt_surfaces_the_transient_error_the_control_raised(tmp_path, monkeypatch):
    """The error classification stays where the transport is: an adapter that
    raises `DispatchError(transient, retry_after)` is retried as the Herdr one."""
    _no_process(monkeypatch)
    fake = FakeAgentControl()
    d, state = _dispatcher(tmp_path, fake)
    fake.fail["prompt"] = DispatchError("stalled", transient=True, retry_after=30.0)

    with pytest.raises(DispatchError) as caught:
        d.dispatch(Role.PLANNER, "plan it", state=state)

    assert caught.value.transient is True and caught.value.retry_after == 30.0


class _Policy:
    """What `HerdrDispatcher._agent_args` asks an approval policy for."""

    def write_role_files(self, store_dir, role, *, answers_dir=None):
        return f"settings-{role}.json", f"prompt-{role}.md"

    def args(self, role, settings, prompt):
        return ["--settings", settings, "--permission-mode", "plan"]


def _planner_writes_its_answer(d, state, fake):
    fake.on_prompt = lambda name, text: d._answer_path(Role.PLANNER, state).write_text(
        '{"ok": true}\n', encoding="utf-8")


def test_the_agent_is_started_where_and_with_what_the_role_needs(tmp_path, monkeypatch):
    """Catches: the dispatcher handing the control less than it handed `_cli`:
    the role's own arguments (model, effort, the approval policy's flags -- a
    trust boundary), the Herdr workspace, the start deadline, the pane
    environment and the purposes the error texts carry."""
    _no_process(monkeypatch)
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w9")
    fake = FakeAgentControl()
    d, state = _dispatcher(tmp_path, fake, models={Role.PLANNER: "opus-x"},
                           efforts={Role.PLANNER: "high"})
    d.approval_policy = _Policy()
    _planner_writes_its_answer(d, state, fake)

    d.dispatch(Role.PLANNER, "plan it", state=state)

    name = d.agents[Role.PLANNER]
    opened = next(c for c in fake.calls if c[0] == "open_surface")
    started = next(c for c in fake.calls if c[0] == "start")
    assert started[1:] == (name, "claude", d._role_panes()[Role.PLANNER],
                           ("--model", "opus-x", "--effort", "high", "--settings",
                            "settings-planner.json", "--permission-mode", "plan"), 240000)
    assert opened[4] == "w9", "the Herdr workspace of the session"
    assert opened[3]["PYTHONDONTWRITEBYTECODE"] == "1", "O215: the pane environment"
    assert opened[3][procs.MARK_ENV], "the pane mark that `procs` finds leftovers by"
    # (a dispatcher that waits for the input box before a first prompt also
    # reads `status`/`screen`, with "planner status read"/"planner input read")
    assert [p for p in fake.purposes if p[0] in ("open_surface", "start", "prompt")] == [
        ("open_surface", "create tab"), ("start", f"start agent {name}"),
        ("prompt", "planner dispatch")]
    assert {p for p in fake.purposes if p[0] in ("status", "screen")} <= {
        ("status", "planner status read"), ("screen", "planner input read")}


def test_a_follow_up_goes_through_the_control_and_keeps_its_own_evidence(tmp_path, monkeypatch):
    _no_process(monkeypatch)
    monkeypatch.setattr(dispatchers, "ANSWER_GRACE_SECONDS", 0.0)
    fake = FakeAgentControl()
    d, state = _dispatcher(tmp_path, fake)

    with pytest.raises(dispatchers.AnswerMissing) as missing:
        d.dispatch(Role.PLANNER, "plan it", state=state)
    first = d.endpoint_evidence(Role.PLANNER)
    assert "turn1" in first

    _planner_writes_its_answer(d, state, fake)
    charged = []
    out = d.follow_up(Role.PLANNER, state, missing.value, charge=lambda: charged.append(1))

    assert json.loads(out) == {"ok": True} and charged == [1]
    second = next(c for c in reversed(fake.calls) if c[0] == "prompt")
    assert "[answer-file-missing]" in second[2] and second[3] == 1000
    assert fake.purposes[-1] == ("prompt", "planner follow-up")
    assert "turn2" in d.endpoint_evidence(Role.PLANNER), "the follow-up's own answer is the evidence"


def test_the_dispatcher_hands_the_control_what_it_was_given(tmp_path, monkeypatch):
    """The two reads the dispatcher makes with an argument that matters: `lines`
    of the diagnosis view, and `source=None` of the question whether Herdr
    itself knows a pane."""
    _no_process(monkeypatch)
    fake = FakeAgentControl()
    d, state = _dispatcher(tmp_path, fake)

    d._pane_view("some-agent", lines=7)
    assert ("transcript", "some-agent", 7) in fake.calls

    assert d._herdr_says_gone("w1:p9") is True            # the fake raises pane_not_found
    assert ("pane_screen", "w1:p9", None) in fake.calls
    assert fake.purposes[-1] == ("pane_screen", "read pane w1:p9")
    assert d._herdr_says_gone(fake.CALLER) is False


# --------------------------------------------------------------------------- #
# 4. The only door out of the dispatcher is `self.control`
# --------------------------------------------------------------------------- #


def _tree(name: str) -> ast.AST:
    return ast.parse((SRC / name).read_text(encoding="utf-8"))


def test_the_dispatcher_spells_no_herdr_command_and_calls_no_herdr_function():
    tree = _tree("dispatchers.py")
    spelled, called = [], []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts \
                and isinstance(node.elts[0], ast.Constant) and node.elts[0].value == "herdr":
            spelled.append(node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "herdr":
            called.append(node.func.attr)
    assert spelled == [], f"a Herdr command line is built in dispatchers.py at {spelled}"
    # `require_herdr` (construction without a control) and `available` (picking
    # the backend) decide *whether* Herdr is the backend; neither talks to it.
    assert sorted(called) == ["available", "require_herdr"]


def test_the_commands_are_spelled_in_the_adapter_and_in_no_other_dispatch_module():
    spellers = []
    for path in sorted(SRC.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.List) and node.elts \
                    and isinstance(node.elts[0], ast.Constant) \
                    and node.elts[0].value == "herdr":
                spellers.append(path.name)
                break
    # `agentcontrol` is the dispatch path's; `herdr.py` is the module-level
    # CLI helper and `rollover.py` the successor launcher (not yet behind the
    # seam: it keeps its own `run=` injection). A fourth module that spells a
    # Herdr command is a decision someone has to make on purpose.
    assert spellers == ["agentcontrol.py", "herdr.py", "rollover.py"]


def test_the_dispatcher_closes_only_through_two_functions():
    """The tripwire of `test_role_pane_lifecycle` for the new spelling: only
    `_close_own_pane` and `_replace_restored` call `close_pane`/`close_tab`."""
    sites = set()
    tree = _tree("dispatchers.py")
    for cls in (n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "HerdrDispatcher"):
        for fn in (f for f in cls.body if isinstance(f, ast.FunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr in ("close_pane", "close_tab", "close_workspace"):
                    sites.add(fn.name)
    assert sites == {"_close_own_pane", "_replace_restored"}


def test_the_adapter_module_is_named_by_the_command_surface_through_the_dispatcher():
    """P2-17's probe measures import *text*; this is the chain it follows."""
    def imports(name: str) -> set[str]:
        out: set[str] = set()
        for node in ast.walk(_tree(name)):
            if isinstance(node, ast.ImportFrom):
                out |= {a.name for a in node.names}
                if node.module:
                    out.add(node.module.split(".")[-1])
        return out

    assert "agentcontrol" in imports("dispatchers.py")
    assert "dispatchers" in imports("cli.py")


def test_the_matrix_measures_the_real_symbol_and_the_equivalent_still_caps_the_row():
    """What `tools/capability_matrix.py` measures for a probe naming
    `AgentControlAdapter`, tried on requirements made here (so it does not
    depend on the register): the symbol is defined once, in `agentcontrol.py`;
    the module is reachable from an entry point; the test file is collected.
    With the `equivalent` the register keeps for the unbuilt contracts the row
    is PARTIAL and the evidence names the real symbol and its consumer; the same
    probe without it would be PROVEN -- the cap is the only thing holding the
    row, which is what the register says. Text, not execution (O222): the
    execution is the other tests of this file."""
    spec = importlib.util.spec_from_file_location(
        "capability_matrix_for_agent_control", ROOT / "tools" / "capability_matrix.py")
    cm = importlib.util.module_from_spec(spec)
    sys.modules["capability_matrix_for_agent_control"] = cm
    spec.loader.exec_module(cm)

    symbols = cm._symbols()
    assert [h.split(":")[0] for h in symbols.get("AgentControlAdapter", [])] == ["src/hoh/agentcontrol.py"]
    graph = cm._import_graph()
    assert "src/hoh/cli.py" in cm._reachable_from_an_entry_point("agentcontrol", graph)

    probe = {"kind": "symbol", "symbol": "AgentControlAdapter",
             "module": "agentcontrol.py", "test": "tests/test_agent_control.py"}
    requirement = {"id": "P2-17", "authoritative_item": "P2.build-verify.agentcontroladapter"}
    collected = ("tests/test_agent_control.py", True)

    capped = cm.classify({**requirement, "probe": {**probe, "equivalent": "OwnedAgentRegistry missing"}},
                         symbols, collected, {}, graph)
    assert capped["status"] == cm.PARTIAL, (capped["status"], capped["evidence"])
    assert any(line.startswith("src/hoh/agentcontrol.py:") for line in capped["evidence"])
    assert any("named in an import statement by" in line for line in capped["evidence"])
    assert any("CAPPED at PARTIAL by the equivalent" in line for line in capped["evidence"])

    bare = cm.classify({**requirement, "probe": probe}, symbols, collected, {}, graph)
    assert bare["status"] == cm.PROVEN, (bare["status"], bare["evidence"])
