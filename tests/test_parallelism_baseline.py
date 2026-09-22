"""The parallelism baseline: would it notice if the answer changed?

The verdicts it produces today are all negative, which is the easy case for a
probe to get right by accident. Every test here makes the code say something
different and requires the verdict to move with it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "parallelism_probe", ROOT / "tools" / "parallelism_baseline.py")
pb = importlib.util.module_from_spec(_spec)
sys.modules["parallelism_probe"] = pb
_spec.loader.exec_module(pb)


def _fake_source(monkeypatch, mapping: dict[str, str]):
    """Answer `_source` from a dict of repository-relative path -> text."""
    def fake(rel: str):
        text = mapping.get(rel, "")
        return text, text.splitlines()
    monkeypatch.setattr(pb, "_source", fake)


def test_the_frontier_being_indexed_is_what_makes_it_missing(monkeypatch):
    """One element taken from a list of ready nodes is the whole finding, so
    the probe has to key on that and not on a word in a comment."""
    _fake_source(monkeypatch, {"src/hoh/orchestrator.py":
                               "def loop(state):\n"
                               "    ready_nodes = state.ready()\n"
                               "    one = ready_nodes[0]\n"
                               "    return one\n"})
    f = pb.project_native_parallelism()
    assert f.status == pb.MISSING
    assert any("ready_nodes[0]" in a or "ready_nodes = state.ready()" in a
               for a in f.anchors), f.anchors


def test_iterating_the_frontier_moves_the_verdict(monkeypatch):
    """The negative control that matters: when the day comes that the
    orchestrator launches the frontier, this row must stop saying MISSING
    without anybody remembering to edit it."""
    _fake_source(monkeypatch, {"src/hoh/orchestrator.py":
                               "def loop(state):\n"
                               "    ready_nodes = state.ready()\n"
                               "    for n in ready_nodes:\n"
                               "        launch(n)\n"})
    f = pb.project_native_parallelism()
    assert f.status == pb.IMPLEMENTED_NOT_PROVEN
    assert "measurement" in f.detail


def test_a_missing_file_is_not_determinable_and_never_missing(monkeypatch):
    """"I could not find it" and "it is not there" are different sentences,
    and only one of them is about the software."""
    _fake_source(monkeypatch, {})
    for probe in (pb.project_native_parallelism, pb.integration_parallelism,
                  pb.role_session_parallelism):
        f = probe()
        assert f.status == pb.NOT_DETERMINABLE, (probe.__name__, f.status)


def test_a_concurrency_primitive_in_the_controller_is_noticed(monkeypatch):
    """Sequential-by-design is a claim about the code, so it has to fail the
    moment the code stops being that."""
    _fake_source(monkeypatch, {"src/hoh/controller.py":
                               "import concurrent.futures\n"
                               "def drive():\n"
                               "    with concurrent.futures.ThreadPoolExecutor() as ex:\n"
                               "        ex.submit(plan)\n"})
    assert pb.role_session_parallelism().status == pb.IMPLEMENTED_NOT_PROVEN

    _fake_source(monkeypatch, {"src/hoh/controller.py":
                               "def drive(x):\n"
                               "    a = x.dispatch('planner')\n"
                               "    b = x.dispatch('developer')\n"
                               "    return a, b\n"})
    assert pb.role_session_parallelism().status == pb.SEQUENTIAL_BY_DESIGN


def test_a_lock_is_not_evidence_that_the_product_starts_two_runs(monkeypatch):
    """The demo ran these two together. A per-run lock means two controllers
    cannot fight over one run; it says nothing about who starts the second."""
    _fake_source(monkeypatch, {"src/hoh/projectstore.py":
                               "import fcntl\n"
                               "def hold(fh):\n"
                               "    fcntl.flock(fh, fcntl.LOCK_EX)\n"})
    f = pb.run_parallelism()
    assert f.status == pb.PARTIAL
    assert "operating system" in f.detail

    _fake_source(monkeypatch, {
        "src/hoh/projectstore.py": "import fcntl\n",
        "src/hoh/orchestrator.py":
            "from concurrent.futures import ThreadPoolExecutor\n"
            "def go():\n"
            "    ThreadPoolExecutor().submit(run)\n"})
    assert pb.run_parallelism().status == pb.IMPLEMENTED_NOT_PROVEN


def test_the_contract_surface_separates_absent_from_renamed():
    """`MISSING` has to mean no symbol *and* no equivalent, or the table reads
    as a to-do list with the finished items still on it."""
    rows = {r["contract"]: r for r in pb.contract_surface()}
    assert rows["ExecutionPreflight"]["status"] == pb.PARTIAL
    assert "preflight" in rows["ExecutionPreflight"]["equivalent_here"]
    assert rows["ParallelWaveExecutor"]["status"] == pb.MISSING
    assert rows["ParallelWaveExecutor"]["equivalent_here"] is None
    # And a contract whose symbol exists is never MISSING.
    for row in rows.values():
        if row["symbol"]:
            assert row["status"] != pb.MISSING, row


def test_this_tree_still_reads_as_the_baseline_v3_3_was_written_against():
    """The positive control for the whole tool: if this ever changes, the
    plan's premise has changed and the baseline document is stale."""
    body = pb.measure()
    by_q = {c["question"]: c["status"] for c in body["classifications"]}
    assert by_q["project_native_parallelism"] == pb.MISSING
    assert by_q["role_session_parallelism"] == pb.SEQUENTIAL_BY_DESIGN
    assert by_q["run_parallelism"] == pb.PARTIAL


def test_the_rendered_document_carries_the_anchors():
    """A verdict without the line that produced it is an opinion."""
    text = pb.render(pb.measure())
    assert "src/hoh/orchestrator.py:" in text
    assert "MISSING" in text and "| contract | status | here |" in text
