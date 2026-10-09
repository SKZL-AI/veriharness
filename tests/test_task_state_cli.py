"""Compact task state (V3.3 P2-13), executed from the product's own command surface.

`src/hoh/taskstate.py` existed and was tested (44 tests), and nothing in the product used it:
no entry point named it, so the capability matrix capped the item at PARTIAL (text, not
execution, O222). `hoh project resume` is the command a fresh session runs to learn where a
project stands from persisted state alone, and `hoh project status` is its human form; both
now project the state through `taskstate.from_project`. Every test but the last runs those
commands through `hoh.cli.main` and checks what they printed; the last asks what the matrix sees.

Read-only by specification: neither command writes. Nothing here starts Herdr or an agent.

An autouse fixture wraps `taskstate.from_project`, and the two command helpers assert that each
command called it exactly once: a command that built the same output some other way (a local call
of `compact`, a hard-coded dict) with `from_project` dead is red in every test that runs a command.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hoh import cli, taskstate
from hoh.project import GateOutcome, GateResult, Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore

ROOT = Path(__file__).resolve().parent.parent

from conftest import PROGRAM_REGISTER, needs_evidence  # noqa: E402


_CALLS: list[int] = []


@pytest.fixture(autouse=True)
def _spy_on_from_project(monkeypatch):
    _CALLS.clear()
    real = getattr(taskstate, "from_project", None)
    if real is None:                    # the module has no such function yet: nothing to wrap, and
        return                          # the command helpers then fail with their own message

    def spy(*args, **kwargs):
        _CALLS.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(taskstate, "from_project", spy)


def _project(tmp_path: Path, nodes: list[TaskNode], **over) -> ProjectStore:
    store = ProjectStore(tmp_path, "p")
    store.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=nodes, **over))
    return store


def _node(node_id: str, life: Lifecycle = Lifecycle.READY, **over) -> TaskNode:
    return TaskNode(id=node_id, lifecycle=life, **over)


def _resume(tmp_path: Path, capsys) -> dict:
    capsys.readouterr()
    before = len(_CALLS)
    assert cli.main(["--root", str(tmp_path), "project", "resume", "p"]) == 0
    assert len(_CALLS) == before + 1, "resume did not go through taskstate.from_project exactly once"
    return json.loads(capsys.readouterr().out)


def _status(tmp_path: Path, capsys) -> str:
    capsys.readouterr()
    before = len(_CALLS)
    assert cli.main(["--root", str(tmp_path), "project", "status", "p"]) == 0
    assert len(_CALLS) == before + 1, "status did not go through taskstate.from_project exactly once"
    return capsys.readouterr().out


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_resume_prints_the_compact_task_state_of_the_persisted_project(tmp_path, capsys):
    _project(tmp_path, [
        _node("m1", Lifecycle.MERGED), _node("m2", Lifecycle.MERGED), _node("r1"),
        _node("run1", Lifecycle.RUNNING),
        _node("lim", Lifecycle.BLOCKED, blocked_kind="usage_limit",
              retry_after="2026-10-09T00:00:00Z", rejections=2),
        _node("ask", Lifecycle.BLOCKED, blocked_kind="human", note="which licence?"),
    ], program_generation=3)
    data = _resume(tmp_path, capsys)
    # what resume has always answered is still there
    assert {"project_id", "verdict", "reason", "measurement_head", "closure_generation",
            "rc_closed"} <= set(data)
    assert data["gates_green"] is False                           # no gate has run
    assert "nodes only" in data["task_state_scope"] and "FAILED is 0" in data["task_state_scope"]
    state = data["task_state"]
    assert data["task_state_error"] is None
    assert (state["program_id"], state["program_generation"]) == ("p", 3)
    assert state["counts"] == {"FAILED": 0, "BLOCKED": 1, "WAITING_FOR_DECISION": 1,
                               "RUNNING": 1, "READY": 1, "DONE": 2}
    assert state["truncated"] is False and state["omissions"] == []
    # attention order: the blocked node first, then the question nobody answered
    assert [(l["node_id"], l["health"]) for l in state["lines"]] == [
        ("lim", "BLOCKED"), ("ask", "WAITING_FOR_DECISION"), ("run1", "RUNNING"),
        ("r1", "READY"), ("m1", "DONE"), ("m2", "DONE")]
    lim = state["lines"][0]
    assert lim["attempts"] == 2
    assert "usage_limit" in lim["blocked_reason"] and "2026-10-09T00:00:00Z" in lim["blocked_reason"]
    assert state["lines"][1]["waiting_on"] == "which licence?"
    # the printed document is a state the module itself accepts: its digest is checked on load
    rebuilt = taskstate.CompactState.model_validate(state)
    assert rebuilt.state_digest == state["state_digest"] == rebuilt.compute_digest()


def test_the_counts_are_about_every_node_and_the_budget_never_drops_an_attention_node(
        tmp_path, capsys):
    nodes = [_node(f"d{i:02d}", Lifecycle.MERGED) for i in range(60)]
    nodes += [_node("blocked-late", Lifecycle.BLOCKED, blocked_kind="capability_violation")]
    _project(tmp_path, nodes)
    state = _resume(tmp_path, capsys)["task_state"]
    assert state["truncated"] is True
    assert state["counts"]["DONE"] == 60 and state["counts"]["BLOCKED"] == 1   # not the excerpt's
    assert len(state["lines"]) == taskstate.DEFAULT_MAX_LINES
    assert state["lines"][0]["node_id"] == "blocked-late"                    # first, though listed last
    dropped = [o for o in state["omissions"] if o["kind"] == taskstate.DROPPED]
    assert [o["count"] for o in dropped] == [60 - (taskstate.DEFAULT_MAX_LINES - 1)]
    assert len(state["lines"]) + dropped[0]["count"] == 61                    # nothing vanished unrecorded

    # more attention nodes than the budget: the overrun is recorded, not hidden
    many = [_node(f"b{i:02d}", Lifecycle.BLOCKED, blocked_kind="usage_limit")
            for i in range(taskstate.DEFAULT_MAX_LINES + 5)]
    other = tmp_path / "other"
    other.mkdir()
    _project(other, many)
    state = _resume(other, capsys)["task_state"]
    assert len(state["lines"]) == taskstate.DEFAULT_MAX_LINES + 5
    assert [o["kind"] for o in state["omissions"]] == [taskstate.BUDGET_EXCEEDED]
    assert state["omissions"][0]["count"] == 5 and state["truncated"] is False


def test_project_nodes_without_a_health_of_their_own_keep_their_lifecycle_word_on_the_line(tmp_path, capsys):
    _project(tmp_path, [
        _node("gone", Lifecycle.ABANDONED, note="superseded"),
        _node("maybe", Lifecycle.CONTINGENT),
        _node("odd", Lifecycle.BLOCKED),                                       # no blocked_kind
        _node("done", Lifecycle.MERGED),
    ])
    lines = {l["node_id"]: l for l in _resume(tmp_path, capsys)["task_state"]["lines"]}
    assert lines["gone"]["health"] == "DONE" and lines["gone"]["title"] == "ABANDONED: superseded"
    assert lines["done"]["health"] == "DONE" and lines["done"]["title"] == "MERGED: done"
    # a contingent node is not READY (nobody should dispatch it) and is never dropped
    assert lines["maybe"]["health"] == "WAITING_FOR_DECISION"
    assert lines["maybe"]["title"] == "CONTINGENT: maybe" and lines["maybe"]["waiting_on"]
    # (the counts say DONE for both; only a kept line says which it was)
    assert _resume(tmp_path, capsys)["task_state"]["counts"]["DONE"] == 2
    # an unclassified block stays BLOCKED and says so
    assert lines["odd"]["health"] == "BLOCKED" and "unclassified" in lines["odd"]["blocked_reason"]


def test_the_projection_names_the_state_it_was_made_from(tmp_path, capsys):
    store = _project(tmp_path, [_node("a"), _node("b", Lifecycle.MERGED)], program_generation=4)
    first = _resume(tmp_path, capsys)["task_state"]
    assert first["program_generation"] == 4
    assert first["as_of"] == store.read_state().updated_at
    assert list(first["source_digests"]) == ["project-nodes"]
    assert _resume(tmp_path, capsys)["task_state"] == first                    # equal input, equal output
    st = store.read_state()
    st.nodes[0].lifecycle = Lifecycle.MERGED
    store.write_state(st)
    second = _resume(tmp_path, capsys)["task_state"]
    assert second["source_digests"] != first["source_digests"]
    assert second["state_digest"] != first["state_digest"]
    assert second["counts"]["DONE"] == 2 and second["counts"]["READY"] == 0
    # and the module's own staleness rule reads that difference
    reasons = taskstate.stale_against(taskstate.CompactState.model_validate(first),
                                      current=second["source_digests"], current_generation=4)
    assert any("project-nodes" in r and "changed" in r for r in reasons)


def test_status_prints_the_rendered_layer_zero_with_every_omission(tmp_path, capsys):
    nodes = [_node(f"d{i:02d}", Lifecycle.MERGED) for i in range(50)]
    nodes.append(_node("stuck", Lifecycle.BLOCKED, blocked_kind="usage_limit"))
    _project(tmp_path, nodes)
    out = _status(tmp_path, capsys)
    assert "compact task state (Layer 0):" in out
    assert "TRUNCATED" in out
    assert "counts (all nodes, before truncation): FAILED=0, BLOCKED=1" in out
    assert "DONE=50" in out
    assert "[BLOCKED] stuck" in out
    assert "dropped (" in out and "to fit max_lines=" in out                    # the renderer's omission line
    assert out.startswith("project p  (")                                       # the old report comes first ...
    assert out.index("  verdict ") < out.index("  nodes:") < out.index("compact task state (Layer 0):")
    assert "    scope: nodes only" in out                                          # ... and the block says what it covers


def test_a_state_the_projection_refuses_is_reported_beside_the_verdict_not_instead_of_it(
        tmp_path, capsys):
    _project(tmp_path, [_node("twin"), _node("twin", Lifecycle.MERGED)])      # duplicate ids
    data = _resume(tmp_path, capsys)
    assert data["task_state"] is None and "duplicate" in data["task_state_error"]
    assert data["verdict"] and data["reason"]                                  # still answered
    out = _status(tmp_path, capsys)
    assert "compact task state (Layer 0):" in out and "unavailable:" in out and "duplicate" in out
    # a refusal that pydantic words over several lines is still one short line
    other = tmp_path / "other"
    other.mkdir()
    _project(other, [_node("neg", rejections=-1)])
    data = _resume(other, capsys)
    error = data["task_state_error"]
    assert data["task_state"] is None and data["verdict"]
    assert "\n" not in error and 0 < len(error) <= 300 and "attempts" in error


def test_neither_command_writes_anything(tmp_path, capsys):
    _project(tmp_path, [_node("a"), _node("b", Lifecycle.BLOCKED, blocked_kind="human")])
    before = _snapshot(tmp_path)
    _resume(tmp_path, capsys)
    _status(tmp_path, capsys)
    assert _snapshot(tmp_path) == before


def test_a_ready_node_with_an_open_dependency_says_so(tmp_path, capsys):
    _project(tmp_path, [
        _node("blocker", Lifecycle.BLOCKED, blocked_kind="usage_limit"),
        _node("done", Lifecycle.MERGED),
        _node("waits", dependencies=["blocker"]),
        _node("ghostly", dependencies=["ghost"]),
        _node("free", dependencies=["done"]),
    ])
    lines = {l["node_id"]: l for l in _resume(tmp_path, capsys)["task_state"]["lines"]}
    assert lines["waits"]["health"] == "READY" and lines["waits"]["title"].endswith("(dependency open)")
    assert lines["waits"]["waiting_on"] == "blocker"
    assert lines["ghostly"]["title"].endswith("(dependency open)")
    assert lines["ghostly"]["waiting_on"] == "ghost (missing)"
    assert lines["free"]["title"] == "free" and lines["free"]["waiting_on"] is None   # settled: runnable
    assert "(dependency open)" in _status(tmp_path, capsys)


def test_free_text_is_clipped_and_an_unmapped_lifecycle_is_refused(tmp_path, capsys):
    long_note = "word\n" * 2000
    nodes = [_node(f"n{i:02d}", Lifecycle.BLOCKED, blocked_kind="usage_limit", note=long_note)
             for i in range(taskstate.DEFAULT_MAX_LINES)]
    nodes.append(_node("ready-long", note=long_note, dependencies=["n00"]))
    _project(tmp_path, nodes)
    text = json.dumps(_resume(tmp_path, capsys))
    state = json.loads(text)["task_state"]
    for line in state["lines"]:
        assert "\n" not in line["title"] and len(line["title"]) <= taskstate.TITLE_MAX + len(" (dependency open)")
        assert len(line["blocked_reason"] or "") <= taskstate.DETAIL_MAX
    assert len(text) < 60_000                                   # was 208 KB for 40 uncapped nodes
    # a lifecycle the mapping does not know is refused, never read as BLOCKED
    future = TaskNode.model_construct(id="x", lifecycle="FUTURE", note="", blocked_kind=None,
                                      retry_after=None, rejections=0, dependencies=[])
    with pytest.raises(ValueError, match="no compact mapping"):
        taskstate.line_for_node(future)


def test_a_block_that_covers_nodes_only_is_never_an_all_clear_while_a_gate_is_not_green(
        tmp_path, capsys):
    red = GateResult(name="union", outcome=GateOutcome.RED, subject="abc1234", exit_code=1)
    green = GateResult(name="union", outcome=GateOutcome.GREEN, subject="abc1234", exit_code=0)
    _project(tmp_path, [_node("a", Lifecycle.MERGED), _node("b", Lifecycle.MERGED)], gates=[red])
    data = _resume(tmp_path, capsys)
    assert data["task_state"]["counts"]["DONE"] == 2 and data["task_state"]["counts"]["FAILED"] == 0
    assert data["gates_green"] is False and data["verdict"] != "closed"
    out = _status(tmp_path, capsys)
    assert "NOT ALL CLEAR" in out and out.index("gates green      False") < out.index("NOT ALL CLEAR")
    # with every gate green the warning is gone
    other = tmp_path / "other"
    other.mkdir()
    _project(other, [_node("a", Lifecycle.MERGED)], gates=[green], measurement_head="abc1234")
    assert _resume(other, capsys)["gates_green"] is True
    assert "NOT ALL CLEAR" not in _status(other, capsys)


def test_the_command_surface_names_the_module_the_matrix_measures():
    spec = importlib.util.spec_from_file_location(
        "capability_matrix_for_task_state", ROOT / "tools" / "capability_matrix.py")
    cm = importlib.util.module_from_spec(spec)
    sys.modules["capability_matrix_for_task_state"] = cm
    spec.loader.exec_module(cm)

    graph = cm._import_graph()
    assert "src/hoh/cli.py" in cm._reachable_from_an_entry_point("taskstate", graph)

    needs_evidence(PROGRAM_REGISTER)
    register = json.loads((ROOT / PROGRAM_REGISTER.path).read_text(encoding="utf-8"))
    rows = {r["id"]: r for r in register["requirements"]}
    # Deliberately tied to the matrix, as test_projectcontract_cli and test_decision_cli are:
    # the point of this wiring is the PROVEN lift, so a change of the matrix's rule that moves
    # P2-13 must be seen here and decided, not absorbed. The reachability half above is the
    # part that stays true whatever the matrix does with statuses.
    row = cm.classify(rows["P2-13"], cm._symbols(), ("tests/test_taskstate.py", True), {}, graph)
    assert row["status"] == cm.PROVEN, (row["status"], row["evidence"])
