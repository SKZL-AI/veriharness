"""P3-01t: the frontier planner's deferral, on the production path.

`tests/test_frontier.py` pins `ReadyFrontierPlanner` as a unit; `tests/test_wave_controller.py`
pins that independent nodes run together. Neither fails a planner that **never defers**, nor one
that compares a node with only the first or only the last selected node: the node below drives
`hoh project drive` (cli.main -> `drive_governed` -> `ProjectController` -> the planner -> the
wave round) with READY nodes among which one **depends on a selected node** -- the head, the
last selected, or one in the middle of three -- and asserts that the planner defers it: it is not
in the wave, the wave record says what it waits behind, and it runs only after the wave ended.

What is real: the CLI, the registered driver, `ProjectController`, `ReadyFrontierPlanner`,
`HohRunLauncher.depends_on`, the wave executor and its record. What is replaced is the process
boundary **and the merge** (`FakeCliLauncher` of `test_wave_controller` replaces `_run_cli` and
`merge`): no agent, no network, and the real merge path is not exercised -- "MERGED" here is the
controller's transition, not a git integration. A test reads nothing outside `tmp_path` except the
tests directory (the driver factory is imported from `test_wave_controller` by name, as
`driver.json` registers it) and never removes anything.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import test_wave_controller as twc
from hoh import cli
from hoh.frontier import SEMANTIC_DEPENDENCY
from hoh.orchestrator import HaltClass
from hoh.project import Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.wave import NodeState, ParallelismDegradation, ParallelismDegradationReason, read_record

#: READY order (the order the nodes are declared in), what each declares, the node that is deferred,
#: the selected node it waits behind, and the wave the independent nodes run in. `n2` always depends on
#: exactly one node: the head (`n1`) -- by a write scope both name, or by reading what it writes --, the
#: last selected node (`n3`) of a wave of two, or the middle one (`n3`) of a wave of three. A planner
#: that compares a node with no earlier selected node (never defers), only the first (`selected[:1]`)
#: or only the last (`selected[-1:]`) defers the wrong node or none in at least one case.
CASES = {
    "head_by_shared_write_scope": dict(
        declared={"n1": dict(writes=["a.txt"]), "n3": dict(writes=["c.txt"]), "n2": dict(writes=["a.txt"])},
        blocked_by="n1", wave=("n1", "n3")),
    "head_by_semantic_read": dict(
        declared={"n1": dict(writes=["a.txt"]), "n3": dict(writes=["c.txt"]),
                  "n2": dict(writes=["b.txt"], semantic_reads=["a.txt"])},
        blocked_by="n1", wave=("n1", "n3")),
    "last_selected_by_shared_write_scope": dict(
        declared={"n1": dict(writes=["a.txt"]), "n3": dict(writes=["c.txt"]), "n2": dict(writes=["c.txt"])},
        blocked_by="n3", wave=("n1", "n3")),
    "middle_selected_by_semantic_read": dict(
        declared={"n1": dict(writes=["a.txt"]), "n3": dict(writes=["c.txt"]), "n4": dict(writes=["d.txt"]),
                  "n2": dict(writes=["b.txt"], semantic_reads=["c.txt"])},
        blocked_by="n3", wave=("n1", "n3", "n4")),
}


def _world(tmp_path, declared):
    """The repository and the registered project of `twc._world`, with the declared write and
    read scopes of each node (that helper names every node's write scope after the node)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    twc.git(repo, "init", "-q", "-b", "master")
    (repo / "README").write_text("x\n", encoding="utf-8")
    twc.git(repo, "add", "-A")
    twc.git(repo, "commit", "-qm", "first")
    nodes = []
    for name, scopes in declared.items():
        spec = tmp_path / f"{name}.md"
        spec.write_text(f"# {name}\n", encoding="utf-8")
        nodes.append(TaskNode(id=name, spec_path=str(spec), **scopes))
    store = ProjectStore(tmp_path / "root", "p")
    store.create(ProjectState(project_id="p", repo_path=str(repo), nodes=nodes))
    return repo, tmp_path / "root", store


@pytest.mark.parametrize("case", sorted(CASES))
def test_hoh_project_drive_defers_a_ready_node_that_depends_on_the_head_and_launches_the_independent_one(
        tmp_path, monkeypatch, capsys, case):
    """The planner's deferral through `hoh project drive`. The READY nodes are declared in the order of
    `CASES[case]`; n2, last in that order, depends on one selected node and the others are independent.
    At a width one above the wave a planner that does not defer would launch n2 with them; one that
    selects only the head would launch n1 alone; one that compares with only the first or only the last
    selected node misses the dependency in the cases that put it elsewhere. Correct: one wave of the
    independent nodes (a barrier the size of the wave is passed only if they run together), n2 named in
    the wave record as deferred behind the node it depends on (SEMANTIC_DEPENDENCY), launched alone after
    the wave ended."""
    spec = CASES[case]
    wave, dependent, blocked_by = spec["wave"], "n2", spec["blocked_by"]
    repo, root, store = _world(tmp_path, spec["declared"])
    twc.CONFIG.clear()
    twc.CONFIG.update(root=root, repo=repo, barrier=threading.Barrier(len(wave)),
                      script={**{n: "overlap" for n in wave}, dependent: "ok"})
    (store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_wave_controller:build_driver", "path": str(Path(twc.__file__).resolve().parent)}))
    monkeypatch.setenv("HOH_WAVE_WIDTH", str(len(wave) + 1))
    rc = cli.main(["--root", str(root), "project", "drive", "p", "--session", "sA"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["halt"] == HaltClass.CLOSED.value, out
    assert [s for s in out["steps"] if s.startswith("WAVE:")], out["steps"]
    if blocked_by == "n1":
        # the loop writes the measured-dependency step for a deferral behind the head only
        # (orchestrator.py, "Serialise only where a dependency is actually measured")
        assert f"SERIALISED:n1<->{dependent}" in out["steps"], out["steps"]

    launcher = twc.CONFIG["launcher"]
    starts = {e[1]: e[2] for e in launcher.events if e[0] == "start"}
    ends = {e[1]: e[2] for e in launcher.events if e[0] == "end"}
    everyone = sorted([*wave, dependent])
    assert sorted(starts) == everyone
    assert max(starts[n] for n in wave) < min(ends[n] for n in wave)         # the independent nodes ran together
    assert starts[dependent] >= max(ends[n] for n in wave)                  # the dependent one only after the wave
    assert sorted(launcher.merged) == everyone
    assert {n.lifecycle for n in store.read_state().nodes} == {Lifecycle.MERGED}

    [path] = sorted((store.dir / "waves").iterdir())                        # exactly one wave: n2 ran alone
    record = read_record(path)
    assert [(n.node_id, n.state) for n in record.nodes] == [(n, NodeState.RETURNED) for n in wave]
    assert [(d.node_id, d.blocked_by, d.reason) for d in record.deferred] == [
        (dependent, blocked_by, SEMANTIC_DEPENDENCY)]
    assert record.degradation == ParallelismDegradation(
        ready=len(everyone), admitted=len(wave), reasons=(ParallelismDegradationReason.CONFLICT,))
