"""P2-17: the agents a run owns are recorded durably, in a role, and the product
acts only on them.

`OwnedAgentRegistry` (which run owns which pane, tab and agent, in which
`ProgramAgentRole`), `OwnedAgentControl` (an `AgentControlAdapter` that refuses
to act on anything the registry does not hold) and the dispatcher's use of both
(it registers what it creates or adopts, consults the registry before it closes
a pane, releases what is gone) are run here, over the fake runtime of
`test_agent_control.py` and over nothing else.

No test starts Herdr or an agent: `_no_process` makes every route to a process or
to `hoh.herdr` fail the test, and the fake's call log is what is asserted.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

from hoh import agentcontrol, dispatchers, herdr
from hoh.agentcontrol import Surface
from hoh.contracts import Role
from hoh.controller import DispatchError
from hoh.durable_io import DiskFullError

from test_agent_control import FakeAgentControl, _dispatcher, _no_process

ROOT = Path(dispatchers.__file__).resolve().parent.parent.parent
RUN = "r1"
# The new names are read from the module when a test runs, never at import: a
# tree without them then fails each test for the missing name, not the file.
A = agentcontrol


def _planner():
    return A.ProgramAgentRole.pipeline(Role.PLANNER)


def _registry(tmp_path: Path, run_id: str = RUN) -> A.OwnedAgentRegistry:
    return A.OwnedAgentRegistry(tmp_path / "owned-agents.jsonl", run_id=run_id)


def _owned_control(tmp_path: Path, fake=None) -> tuple[A.OwnedAgentControl, FakeAgentControl]:
    fake = fake or FakeAgentControl()
    return A.OwnedAgentControl(fake, _registry(tmp_path)), fake


def _made(control: A.OwnedAgentControl, label: str = "hoh planner") -> Surface:
    with control.acting_as(_planner(), program="prog", run_id=RUN):
        return control.open_surface(cwd="/arena", label=label)


# --------------------------------------------------------------------------- #
# 1. ProgramAgentRole
# --------------------------------------------------------------------------- #


def test_a_program_agent_role_is_a_pipeline_role_or_a_program_level_one():
    for role in Role:
        assert A.ProgramAgentRole.pipeline(role).key == f"run:{role.value}"
        assert A.ProgramAgentRole.parse(f"run:{role.value}") == A.ProgramAgentRole.pipeline(role.value)
    reviewer = A.ProgramAgentRole.program("spec-reviewer")
    assert reviewer.key == "program:spec-reviewer"
    assert A.ProgramAgentRole.parse(reviewer.key) == reviewer and hash(reviewer)
    for bad in (lambda: A.ProgramAgentRole("run", "orchestrator"),
                lambda: A.ProgramAgentRole.program("planner"),
                lambda: A.ProgramAgentRole.program("Not A Slug"),
                lambda: A.ProgramAgentRole.program(""),
                lambda: A.ProgramAgentRole("session", "planner"),
                lambda: A.ProgramAgentRole.parse("planner")):
        with pytest.raises(ValueError):
            bad()


# --------------------------------------------------------------------------- #
# 2. OwnedAgentRegistry: durable, per run, fail closed
# --------------------------------------------------------------------------- #


def test_the_registry_is_what_a_new_process_reads_from_disk(tmp_path):
    reg = _registry(tmp_path)
    reg.record_surface(_planner(), Surface("w1:p1", "w1:t1"), program="prog",
                       backend="fake", cwd="/arena")
    reg.record_agent("w1:p1", "agent-1")
    reg.adopt(A.ProgramAgentRole.program("spec-reviewer"), program="prog", backend="fake",
              pane_id="w1:p2", tab_id="w1:t2", name="agent-2", cwd="/arena")

    again = _registry(tmp_path)                       # no state shared with `reg`
    by_pane = {a.pane_id: a for a in again.owned()}
    assert set(by_pane) == {"w1:p1", "w1:p2"}
    first, second = by_pane["w1:p1"], by_pane["w1:p2"]
    assert (first.role, first.name, first.tab_id, first.origin, first.program, first.run_id) == \
        (_planner(), "agent-1", "w1:t1", "created", "prog", RUN)
    assert (second.role.key, second.origin) == ("program:spec-reviewer", "adopted")
    assert again.owns_pane("w1:p1") and again.owns_tab("w1:t2") and again.owns_agent("agent-2")
    assert not again.owns_pane("w1:p0") and not again.owns_agent("") and not again.owns_tab("")
    assert [a.pane_id for a in again.for_role(_planner())] == ["w1:p1"]
    lines = (tmp_path / "owned-agents.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["event"] for x in lines] == ["surface", "agent", "adopt"]
    assert all(json.loads(x)["run_id"] == RUN and json.loads(x)["v"] == 1 for x in lines)


def test_a_release_ends_ownership_and_only_for_that_pane(tmp_path):
    reg = _registry(tmp_path)
    for n in (1, 2):
        reg.record_surface(_planner(), Surface(f"w1:p{n}", f"w1:t{n}"), program="prog",
                           backend="fake", cwd="/arena")
    reg.release("w1:p1", reason="gone")
    again = _registry(tmp_path)
    assert not again.owns_pane("w1:p1") and not again.owns_tab("w1:t1")
    assert again.owns_pane("w1:p2") and again.owns_tab("w1:t2")
    assert {a.pane_id: a.state for a in again.agents()} == {"w1:p1": "released", "w1:p2": "owned"}


def test_what_another_run_wrote_or_what_does_not_parse_owns_nothing(tmp_path):
    other = _registry(tmp_path, run_id="someone-else")
    other.record_surface(_planner(), Surface("w1:p9", "w1:t9"), program="prog",
                         backend="fake", cwd="/arena")
    mine = _registry(tmp_path)
    mine.record_surface(_planner(), Surface("w1:p1", "w1:t1"), program="prog",
                        backend="fake", cwd="/arena")
    with open(tmp_path / "owned-agents.jsonl", "a", encoding="utf-8") as fh:
        fh.write("not json\n")
        fh.write(json.dumps({"event": "teleport", "run_id": RUN, "pane_id": "w1:p5"}) + "\n")
        fh.write(json.dumps({"event": "surface", "run_id": RUN, "pane_id": "w1:p6",
                             "role": "run:wizard"}) + "\n")
        fh.write(json.dumps({"event": "release", "run_id": RUN, "pane_id": "w1:p7"}) + "\n")
        fh.write("[1, 2]\n")

    owned = mine.owned()
    assert [a.pane_id for a in owned] == ["w1:p1"]
    assert not mine.owns_pane("w1:p9") and not mine.owns_pane("w1:p5")
    assert not mine.owns_pane("w1:p6") and not mine.owns_pane("w1:p7")
    # the other run's line, not json, teleport, wizard, a release for nothing, [1, 2]
    assert mine.skipped == 6
    assert mine.problem == ""


def test_an_unreadable_registry_owns_nothing_and_says_why(tmp_path):
    real = tmp_path / "real.jsonl"
    reg = A.OwnedAgentRegistry(real, run_id=RUN)
    reg.record_surface(_planner(), Surface("w1:p1", "w1:t1"), program="prog",
                       backend="fake", cwd="/arena")
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    second_name = tmp_path / "second-name.jsonl"
    os.link(real, second_name)
    for path in (link, second_name):                  # a symbolic link / a second name
        bad = A.OwnedAgentRegistry(path, run_id=RUN)
        assert bad.owned() == [] and not bad.owns_pane("w1:p1")
        assert bad.problem, path
        with pytest.raises(OSError):
            bad.record_agent("w1:p1", "x")            # and nothing is written through it


def test_the_registry_writes_through_durable_io_and_a_failed_write_is_raised(tmp_path, monkeypatch):
    calls = []
    real = agentcontrol.durable_append

    def spy(path, line, **kw):
        calls.append((Path(path).name, kw))
        return real(path, line, **kw)

    monkeypatch.setattr(agentcontrol, "durable_append", spy)
    reg = _registry(tmp_path)
    reg.record_surface(_planner(), Surface("w1:p1", "w1:t1"), program="prog",
                       backend="fake", cwd="/arena")
    assert calls == [("owned-agents.jsonl", {"single_link": True})]

    def full(path, line, **kw):
        raise DiskFullError(path, 1, 0, "no room")

    monkeypatch.setattr(agentcontrol, "durable_append", full)
    with pytest.raises(DiskFullError):
        reg.release("w1:p1", reason="gone")
    assert _registry(tmp_path).owns_pane("w1:p1"), "the failed release changed nothing"


# --------------------------------------------------------------------------- #
# 3. OwnedAgentControl: below the seam, only owned ids are acted on
# --------------------------------------------------------------------------- #


def test_the_owned_control_is_an_adapter_that_registers_what_it_creates(tmp_path):
    control, fake = _owned_control(tmp_path)
    assert isinstance(control, agentcontrol.AgentControlAdapter) and control.backend == "fake"
    surface = _made(control)
    control.start("agent-1", kind="claude", pane_id=surface.pane_id)

    rec = _registry(tmp_path).owned()
    assert [(a.pane_id, a.tab_id, a.name, a.role, a.program, a.backend, a.cwd) for a in rec] == \
        [(surface.pane_id, surface.tab_id, "agent-1", _planner(), "prog", "fake", "/arena")]
    assert fake.verbs() == ["open_surface", "start"]
    control.prompt("agent-1", "go", timeout_ms=5)
    control.press(surface.pane_id, "Enter")
    control.close_tab(surface.tab_id)
    assert "close_tab" in fake.verbs() and surface.pane_id not in fake.panes


def test_without_a_role_or_for_another_run_nothing_is_created(tmp_path):
    control, fake = _owned_control(tmp_path)
    with pytest.raises(A.NotOwned):
        control.open_surface(cwd="/arena", label="hoh planner")
    _made(control)                                     # the first state binds the registry to run r1
    fake.calls.clear()
    with pytest.raises(A.NotOwned):
        with control.acting_as(_planner(), program="prog", run_id="another-run"):
            pass                                       # a bound registry never changes owner
    with pytest.raises(A.NotOwned):
        control.adopt(pane_id="w1:p3", tab_id="w1:t3", name="n", cwd="/arena")
    assert fake.calls == [], "nothing was sent for any of them"
    assert len(_registry(tmp_path).owned()) == 1, "and only the one surface of r1 is owned"


def test_an_acting_verb_on_a_foreign_id_is_refused_before_the_runtime_is_called(tmp_path):
    control, fake = _owned_control(tmp_path)
    mine = _made(control)
    control.start("mine", kind="claude", pane_id=mine.pane_id)
    # A foreign session in a tab of its own, listed by the runtime.
    fake.panes["w1:p77"] = {"pane_id": "w1:p77", "tab_id": "w1:t77", "workspace_id": "w1",
                            "cwd": "/elsewhere", "foreground_cwd": "/elsewhere"}
    fake.agents["captain"] = {"name": "captain", "pane_id": "w1:p77", "tab_id": "w1:t77",
                              "agent": "claude", "agent_status": "working",
                              "cwd": "/elsewhere", "foreground_cwd": "/elsewhere",
                              "workspace_id": "w1"}
    before = list(fake.calls)

    acts = [lambda: control.start("x", kind="claude", pane_id="w1:p77"),
            lambda: control.start("x", kind="claude", pane_id=fake.CALLER),
            lambda: control.prompt("captain", "hi", timeout_ms=5),
            lambda: control.press("w1:p77", "Enter"),
            lambda: control.close_pane("w1:p77"),
            lambda: control.close_pane(fake.CALLER),
            lambda: control.close_tab("w1:t77"),
            lambda: control.close_tab(""),
            lambda: control.close_pane("w1:p99")]
    for act in acts:
        with pytest.raises(A.NotOwned) as caught:
            act()
        assert caught.value.transient is False
    assert fake.calls == before, "the runtime was not called for any of them"

    # Observing is not acting.
    assert control.status("captain").status == "working"
    control.screen("captain"), control.pane_screen("w1:p77"), control.transcript("captain")
    assert {"w1:p77", fake.CALLER} <= {r["pane_id"] for r in control.snapshot()["panes"]}


def test_a_tab_that_holds_a_pane_nobody_registered_is_not_closed(tmp_path):
    control, fake = _owned_control(tmp_path)
    mine = _made(control)
    fake.panes["w1:p88"] = {"pane_id": "w1:p88", "tab_id": mine.tab_id, "workspace_id": "w1",
                            "cwd": "/elsewhere", "foreground_cwd": "/elsewhere"}
    with pytest.raises(A.NotOwned) as caught:
        control.close_tab(mine.tab_id)
    assert "w1:p88" in str(caught.value)
    assert "close_tab" not in fake.verbs() and "w1:p88" in fake.panes
    control.close_pane(mine.pane_id)                   # the owned pane alone may go
    assert "w1:p88" in fake.panes and mine.pane_id not in fake.panes


def test_a_surface_that_cannot_be_recorded_is_closed_again(tmp_path, monkeypatch):
    control, fake = _owned_control(tmp_path)

    def full(path, line, **kw):
        raise DiskFullError(path, 1, 0, "no room")

    monkeypatch.setattr(agentcontrol, "durable_append", full)
    with pytest.raises(DispatchError) as caught:
        _made(control)
    assert "could not be recorded" in str(caught.value)
    assert fake.verbs()[:2] == ["open_surface", "close_tab"]
    assert [p for p in fake.panes] == [fake.CALLER], "nothing the registry does not know is left"


def test_a_released_pane_and_an_unrecorded_agent_are_not_acted_on(tmp_path):
    control, fake = _owned_control(tmp_path)
    mine = _made(control)
    control.start("mine", kind="claude", pane_id=mine.pane_id)
    control.registry.release(mine.pane_id, reason="gone")
    for act in (lambda: control.prompt("mine", "hi", timeout_ms=5),
                lambda: control.close_pane(mine.pane_id),
                lambda: control.start("again", kind="claude", pane_id=mine.pane_id)):
        with pytest.raises(A.NotOwned):
            act()
    assert fake.verbs() == ["open_surface", "start"]


# --------------------------------------------------------------------------- #
# 4. The product's dispatch path registers and consults ownership
# --------------------------------------------------------------------------- #


def _answering(d, state, fake):
    fake.on_prompt = lambda name, text: d._answer_path(Role.PLANNER, state).write_text(
        '{"ok": true}\n', encoding="utf-8")


def test_a_dispatch_registers_the_agent_it_starts_under_its_role(tmp_path, monkeypatch):
    called = _no_process(monkeypatch)
    fake = FakeAgentControl()
    (tmp_path / "x").mkdir()
    d, state = _dispatcher(tmp_path, fake, registry=_registry(tmp_path / "x"))
    _answering(d, state, fake)

    d.dispatch(Role.PLANNER, "plan it", state=state)

    assert called == [] and isinstance(d.control, A.OwnedAgentControl)
    pane = d._role_panes()[Role.PLANNER]
    on_disk = _registry(tmp_path / "x").owned()          # read by a registry that never saw the write
    assert [(a.pane_id, a.name, a.role, a.program, a.origin) for a in on_disk] == \
        [(pane, d.agents[Role.PLANNER], _planner(), "p", "created")]
    assert on_disk[0].tab_id == fake.panes[pane]["tab_id"] and on_disk[0].cwd == str(d.cwd)


def test_the_run_end_closes_what_is_owned_and_releases_it(tmp_path, monkeypatch):
    called = _no_process(monkeypatch)
    fake = FakeAgentControl()
    (tmp_path / "x").mkdir()
    d, state = _dispatcher(tmp_path, fake, registry=_registry(tmp_path / "x"))
    _answering(d, state, fake)
    d.dispatch(Role.PLANNER, "plan it", state=state)
    pane = d._role_panes()[Role.PLANNER]

    d.close_own()

    assert called == [] and pane not in fake.panes and fake.CALLER in fake.panes
    reg = _registry(tmp_path / "x")
    assert reg.owned() == [] and [a.state for a in reg.agents()] == ["released"]
    assert not reg.owns_pane(pane)


def test_a_pane_the_registry_does_not_hold_is_never_closed_even_when_the_run_remembers_it(
        tmp_path, monkeypatch):
    called = _no_process(monkeypatch)
    fake = FakeAgentControl()
    (tmp_path / "x").mkdir()
    reg = _registry(tmp_path / "x")
    d, state = _dispatcher(tmp_path, fake, registry=reg)
    _answering(d, state, fake)
    d.dispatch(Role.PLANNER, "plan it", state=state)
    pane = d._role_panes()[Role.PLANNER]
    reg.release(pane, reason="gone")                     # the durable record says it is not ours
    assert pane in d.own_panes, "the in-memory table still lists it"
    fake.calls.clear()

    assert d._close_own_pane(pane, role=Role.PLANNER, reason="test") is False

    assert called == [] and pane in fake.panes
    assert not [c for c in fake.calls if c[0].startswith("close")]
    ledger = [json.loads(x) for x in
              (d.store_dir / "pane-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()]
    refused = [x for x in ledger if x["event"] == "refused" and x["pane"] == pane]
    assert refused and "owned-agent registry" in refused[-1]["why"]
    assert not [x for x in ledger if x["event"] == "closing"], "no close was attempted"


def test_a_new_process_of_the_run_closes_what_the_registry_holds_and_nothing_else(
        tmp_path, monkeypatch):
    """The registry is on disk for a reason: the dispatcher of a restarted `hoh
    run` (no in-memory table) closes the panes the earlier process created --
    through the same checks as ever -- and a registry of another run gives it
    nothing to close."""
    called = _no_process(monkeypatch)
    fake = FakeAgentControl()
    (tmp_path / "x").mkdir()
    first, state = _dispatcher(tmp_path, fake, registry=_registry(tmp_path / "x"))
    _answering(first, state, fake)
    first.dispatch(Role.PLANNER, "plan it", state=state)
    pane = first._role_panes()[Role.PLANNER]
    tab = fake.panes[pane]["tab_id"]
    fake.panes["w1:p66"] = {"pane_id": "w1:p66", "tab_id": "w1:t66", "workspace_id": "w1",
                            "cwd": str(first.cwd), "foreground_cwd": str(first.cwd)}

    def restarted(registry):
        return dispatchers.HerdrDispatcher(
            answers_dir=first.answers_dir, profiles={r: "claude" for r in Role},
            cwd=first.cwd, timeout_ms=1000, control=fake, registry=registry)

    stranger = restarted(A.OwnedAgentRegistry(tmp_path / "x" / "owned-agents.jsonl",
                                              run_id="another-run"))
    assert stranger.own_panes == []
    stranger.close_own()
    assert pane in fake.panes and not [c for c in fake.calls if c[0].startswith("close")]

    second = restarted(_registry(tmp_path / "x"))
    assert second.own_panes == [] and second.own_tabs == [], "a new process remembers nothing"
    second.close_own()

    assert called == []
    assert pane not in fake.panes
    assert {"w1:p66", fake.CALLER} <= set(fake.panes), "the pane nobody registered stays"
    closes = [c for c in fake.calls if c[0] in ("close_pane", "close_tab")]
    assert ("close_tab", tab) in closes, "the tab the earlier process created is recalled too"
    assert {c[1] for c in closes} <= {pane, tab}
    assert _registry(tmp_path / "x").owned() == []


def test_a_restored_agent_is_owned_only_once_the_dispatcher_proved_it_this_runs(tmp_path, monkeypatch):
    called = _no_process(monkeypatch)
    fake = FakeAgentControl()
    (tmp_path / "x").mkdir()
    reg = _registry(tmp_path / "x")
    d, state = _dispatcher(tmp_path, fake, registry=reg)
    name = d._agent_name(Role.PLANNER, state)
    arena = str(d.cwd)
    fake.panes["w1:p40"] = {"pane_id": "w1:p40", "tab_id": "w1:t40", "workspace_id": "w1",
                            "cwd": arena, "foreground_cwd": arena}
    fake.agents[name] = {"name": name, "pane_id": "w1:p40", "tab_id": "w1:t40",
                         "agent": "claude", "agent_status": "idle", "cwd": arena,
                         "foreground_cwd": arena, "workspace_id": "w1"}

    assert d._ensure_agent(Role.PLANNER, state) == name

    assert called == []
    assert [(a.pane_id, a.name, a.role, a.origin) for a in reg.owned()] == \
        [("w1:p40", name, _planner(), "adopted")]
    assert "start" not in fake.verbs() and "open_surface" not in fake.verbs()

    # The same name in a directory that is not this run's: refused, nothing owned.
    (tmp_path / "y").mkdir()
    other = _registry(tmp_path / "y")
    fake2 = FakeAgentControl()
    d2, state2 = _dispatcher(tmp_path / "y", fake2, registry=other)
    fake2.panes["w1:p41"] = {"pane_id": "w1:p41", "tab_id": "w1:t41", "workspace_id": "w1",
                             "cwd": "/elsewhere", "foreground_cwd": "/elsewhere"}
    fake2.agents[name] = {"name": name, "pane_id": "w1:p41", "tab_id": "w1:t41",
                          "agent": "claude", "agent_status": "idle", "cwd": "/elsewhere",
                          "foreground_cwd": "/elsewhere", "workspace_id": "w1"}
    with pytest.raises(DispatchError):
        d2._ensure_agent(Role.PLANNER, state2)
    assert other.owned() == [] and not [c for c in fake2.calls if c[0] in ("start", "prompt", "close_pane", "close_tab")]


def test_a_tab_is_not_closed_on_a_snapshot_that_does_not_show_it(tmp_path):
    """A snapshot with no pane list, an empty one, one without the tab, or with a row
    that is not an object proves nobody else is in the tab no better than it proves
    the tab is gone: the adapter refuses and sends nothing."""
    snapshots = ({}, {"panes": []}, {"panes": None}, {"panes": "x"},
                 {"panes": [{"pane_id": "w1:p0", "tab_id": "w1:t0"}]},
                 {"panes": ["not a row"]}, "not even an object")
    for n, bad in enumerate(snapshots):
        (tmp_path / f"n{n}").mkdir()
        control, fake = _owned_control(tmp_path / f"n{n}")
        mine = _made(control)
        fake.panes["w1:p88"] = {"pane_id": "w1:p88", "tab_id": mine.tab_id, "workspace_id": "w1",
                                "cwd": "/elsewhere", "foreground_cwd": "/elsewhere"}
        fake.snapshot = lambda bad=bad: bad
        with pytest.raises(A.NotOwned):
            control.close_tab(mine.tab_id)
        assert "close_tab" not in fake.verbs(), bad
        assert "w1:p88" in fake.panes and mine.pane_id in fake.panes


def test_a_registry_directory_named_otherwise_than_the_run_still_serves_the_run(tmp_path, monkeypatch):
    """`tools/benchmark.py` builds its answers directory as `<work dir>/answers`, so the
    directory name is not the run id there. The state's id is what the registry binds
    to; a mismatch must not refuse every dispatch."""
    called = _no_process(monkeypatch)
    fake = FakeAgentControl()
    (tmp_path / "x").mkdir()
    reg = A.OwnedAgentRegistry(tmp_path / "x" / "owned-agents.jsonl", run_id="bench-workdir")
    d, state = _dispatcher(tmp_path, fake, registry=reg)          # state.run_id == "r1"
    _answering(d, state, fake)

    d.dispatch(Role.PLANNER, "plan it", state=state)

    assert called == [] and reg.run_id == RUN
    pane = d._role_panes()[Role.PLANNER]
    assert [a.pane_id for a in _registry(tmp_path / "x").owned()] == [pane]
    lines = (tmp_path / "x" / "owned-agents.jsonl").read_text(encoding="utf-8").splitlines()
    assert {json.loads(x)["run_id"] for x in lines} == {RUN}
    d.close_own()
    assert pane not in fake.panes and _registry(tmp_path / "x").owned() == []


def test_the_built_dispatcher_binds_to_the_state_it_serves_not_to_its_directory(tmp_path, monkeypatch):
    called = _no_process(monkeypatch)
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "require_herdr", lambda: None)
    from test_agent_control import HerdrWire
    wire = HerdrWire()
    d = dispatchers.build_dispatcher(answers_dir=tmp_path / "bench-workdir" / "answers",
                                     profiles={r: "claude" for r in Role}, cwd=tmp_path)
    d.__dict__["_cli"] = wire
    assert d.registry.run_id == "bench-workdir"
    state = types.SimpleNamespace(run_id="bmaxyz1abcd", project_name="bench")

    with d._acting_as(Role.PLANNER, state):
        surface = d.control.open_surface(cwd=str(tmp_path), label="hoh planner")

    assert called == [] and d.registry.run_id == "bmaxyz1abcd"
    assert d.registry.owns_pane(surface.pane_id)
    again = A.OwnedAgentRegistry(d.registry.path, run_id="bmaxyz1abcd")
    assert [a.pane_id for a in again.owned()] == [surface.pane_id]
    with pytest.raises(A.NotOwned):                    # and it does not change owner again
        with d._acting_as(Role.QA, types.SimpleNamespace(run_id="another", project_name="b")):
            pass


def test_only_the_builder_makes_a_herdr_dispatcher_and_it_always_gives_it_a_registry():
    """No product path may construct a `HerdrDispatcher` without a registry: the only
    construction in `src/hoh` and `tools` is the one in `build_dispatcher`, with the
    keyword `registry`."""
    makers = []
    for path in sorted((ROOT / "src" / "hoh").glob("*.py")) + sorted((ROOT / "tools").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        inside: dict[int, str] = {}
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            for node in ast.walk(fn):
                inside.setdefault(id(node), fn.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) \
                    == "HerdrDispatcher":
                makers.append((path.name, inside.get(id(node), "<module>"),
                               tuple(sorted(k.arg for k in node.keywords))))
    assert [(f, n) for f, n, _k in makers] == [("dispatchers.py", "build_dispatcher")], makers
    assert "registry" in makers[0][2]


def _restore(fake, d, state, *, status="idle", pane="w1:p40", tab="w1:t40"):
    """A restored agent of the run's deterministic name, in a directory of the run."""
    name = d._agent_name(Role.PLANNER, state)
    arena = str(d.cwd)
    fake.panes[pane] = {"pane_id": pane, "tab_id": tab, "workspace_id": "w1",
                        "cwd": arena, "foreground_cwd": arena}
    fake.agents[name] = {"name": name, "pane_id": pane, "tab_id": tab, "agent": "claude",
                         "agent_status": status, "cwd": arena, "foreground_cwd": arena,
                         "workspace_id": "w1"}
    return name


def _ledger(d) -> list[dict]:
    path = d.store_dir / "pane-lifecycle.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_a_restored_pane_the_registry_does_not_hold_is_not_replaced(tmp_path, monkeypatch):
    called = _no_process(monkeypatch)
    fake = FakeAgentControl()
    (tmp_path / "x").mkdir()
    reg = _registry(tmp_path / "x")
    d, state = _dispatcher(tmp_path, fake, registry=reg)
    name = _restore(fake, d, state)
    d._ensure_agent(Role.PLANNER, state)                 # adopted and registered
    reg.release("w1:p40", reason="gone")                 # the durable record no longer holds it
    fake.calls.clear()

    with pytest.raises(dispatchers.PaneNotClean):
        d._replace_restored(Role.PLANNER, state, name, "its input box holds text")

    assert called == [] and "w1:p40" in fake.panes
    assert not [c for c in fake.calls if c[0].startswith("close")]
    events = _ledger(d)
    assert [e for e in events if e["event"] == "refused" and "owned-agent registry" in e["why"]]
    assert not [e for e in events if e["event"] == "closing"], "no close was attempted"


def test_a_tab_is_closed_whole_only_if_the_registry_holds_every_pane_in_it(tmp_path, monkeypatch):
    called = _no_process(monkeypatch)
    fake = FakeAgentControl()
    (tmp_path / "x").mkdir()
    d, state = _dispatcher(tmp_path, fake, registry=_registry(tmp_path / "x"))
    _answering(d, state, fake)
    d.dispatch(Role.PLANNER, "plan it", state=state)
    pane = d._role_panes()[Role.PLANNER]
    tab = fake.panes[pane]["tab_id"]
    # A second pane in the run's tab that the run remembers but the registry never saw.
    fake.panes["w1:p55"] = {"pane_id": "w1:p55", "tab_id": tab, "workspace_id": "w1",
                            "cwd": str(d.cwd), "foreground_cwd": str(d.cwd)}
    d.own_panes.append("w1:p55")
    fake.calls.clear()

    assert d._close_own_pane(pane, role=Role.PLANNER, reason="test") is True

    assert called == []
    assert ("close_pane", pane) in fake.calls and not [c for c in fake.calls if c[0] == "close_tab"]
    assert pane not in fake.panes and "w1:p55" in fake.panes


def test_a_dead_restored_agents_pane_is_registered_before_it_is_closed(tmp_path, monkeypatch):
    called = _no_process(monkeypatch)
    fake = FakeAgentControl()
    (tmp_path / "x").mkdir()
    reg = _registry(tmp_path / "x")
    d, state = _dispatcher(tmp_path, fake, registry=reg)
    _restore(fake, d, state, status="unknown", pane="w1:p41", tab="w1:t41")

    d._ensure_agent(Role.PLANNER, state)                 # the dead pane goes, a fresh agent starts

    assert called == []
    assert "w1:p41" not in fake.panes, "the dead restored pane was closed"
    states = {a.pane_id: (a.state, a.origin) for a in reg.agents()}
    assert states["w1:p41"] == ("released", "adopted")
    fresh = d._role_panes()[Role.PLANNER]
    assert states[fresh] == ("owned", "created") and fresh != "w1:p41"


def test_the_product_builds_its_dispatcher_with_a_registry_in_the_run_directory(tmp_path, monkeypatch):
    called = _no_process(monkeypatch)
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "require_herdr", lambda: None)
    run_dir = tmp_path / "runs" / "20261008t0000z-demo"

    d = dispatchers.build_dispatcher(answers_dir=run_dir / "answers",
                                     profiles={r: "claude" for r in Role}, cwd=tmp_path)

    assert called == []
    assert isinstance(d.registry, A.OwnedAgentRegistry)
    assert d.registry.path == run_dir / "owned-agents.jsonl" and d.registry.run_id == run_dir.name
    assert isinstance(d.control, A.OwnedAgentControl)
    assert isinstance(d.control._base, agentcontrol.HerdrAgentControl)
    assert d.control.registry is d.registry


def test_the_herdr_control_the_product_builds_refuses_a_foreign_pane_too(tmp_path, monkeypatch):
    """The composition `hoh run` gets -- `HerdrAgentControl` through the dispatcher's
    own transport, wrapped by the registry -- not only the fake: the commands that
    reach the (fake) Herdr wire are the creation of the run's own tab and nothing
    aimed at a pane the run does not own."""
    called = _no_process(monkeypatch)
    monkeypatch.setattr(herdr, "require_herdr", lambda: None)
    from test_agent_control import HerdrWire
    wire = HerdrWire()
    (tmp_path / "x").mkdir()
    d = dispatchers.HerdrDispatcher(
        answers_dir=tmp_path / "run" / "answers", profiles={r: "claude" for r in Role},
        cwd=tmp_path, registry=_registry(tmp_path / "x"))
    d.__dict__["_cli"] = wire
    state = types.SimpleNamespace(run_id=RUN, project_name="p")

    with d._acting_as(Role.PLANNER, state):
        surface = d.control.open_surface(cwd=str(tmp_path), label="hoh planner")
    assert isinstance(d.control, A.OwnedAgentControl)
    assert isinstance(d.control._base, A.HerdrAgentControl)
    assert d.registry.owns_pane(surface.pane_id) and d.registry.owns_tab(surface.tab_id)
    for act in (lambda: d.control.close_pane("w1:p0"), lambda: d.control.close_tab("w1:t0"),
                lambda: d.control.press("w1:p0", "Enter"),
                lambda: d.control.prompt("captain", "hi", timeout_ms=5)):
        with pytest.raises(A.NotOwned):
            act()
    d.control.close_pane(surface.pane_id)

    assert called == []
    assert [a[1:3] for a in wire.argvs] == [["tab", "create"], ["pane", "close"]]
    assert wire.argvs[1][3] == surface.pane_id


# --------------------------------------------------------------------------- #
# 5. What the matrix can and cannot say about it
# --------------------------------------------------------------------------- #


def test_the_matrix_finds_the_registry_defined_once_and_reachable_from_the_command_surface():
    """Text, not execution (O222): the execution is the tests above."""
    spec = importlib.util.spec_from_file_location(
        "capability_matrix_for_owned_agents", ROOT / "tools" / "capability_matrix.py")
    cm = importlib.util.module_from_spec(spec)
    sys.modules["capability_matrix_for_owned_agents"] = cm
    spec.loader.exec_module(cm)

    symbols = cm._symbols()
    for symbol in ("OwnedAgentRegistry", "ProgramAgentRole", "OwnedAgentControl"):
        assert [h.split(":")[0] for h in symbols.get(symbol, [])] == ["src/hoh/agentcontrol.py"], symbol
    graph = cm._import_graph()
    assert "src/hoh/cli.py" in cm._reachable_from_an_entry_point("agentcontrol", graph)

    probe = {"kind": "symbol", "symbol": "OwnedAgentRegistry", "module": "agentcontrol.py",
             "test": "tests/test_owned_agents.py"}
    requirement = {"id": "P2-17", "authoritative_item": "P2.build-verify.agentcontroladapter",
                   "probe": probe}
    got = cm.classify(requirement, symbols, ("tests/test_owned_agents.py", True), {}, graph)
    assert got["status"] == cm.PROVEN, (got["status"], got["evidence"])
    named = cm.classify({**requirement, "probe": {**probe, "equivalent": "x"}}, symbols,
                        ("tests/test_owned_agents.py", True), {}, graph)
    assert named["status"] == cm.PARTIAL, "an equivalent still caps the row"
