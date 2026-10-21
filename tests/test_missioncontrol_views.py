"""`hoh mission map`, `timeline` and `evidence` (P3-34, P3-37 and P3-38): the project's graph as a
structure, its dated records in order, and the health of its gates and of the evidence of its runs.

Every test builds a real project through the product's own `ProjectStore` (and, for the evidence
view, the product's own `EvidenceBundle` model), holds no process and starts none: `subprocess.run`
and `subprocess.Popen` fail the test. Every odd file a project directory or an evidence bundle can
be is made on disk (a FIFO, a link, a mode-000 file, a directory where a file belongs, bytes that
are not UTF-8) and looked at through the command, with a guard that turns a hang into a failure.

The exit codes, the vocabulary and the bounds are written out as literals on purpose: they are the
contract of the command, and a table imported from the module would agree with a mutated one.
"""
from __future__ import annotations

import io
import json
import os
import random
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from hoh import cli
from hoh.decisionfrontier import derive_frontier
from hoh.evidence import EvidenceBundle, EvidenceItem, EvidenceStatus
from hoh.programlogic import Event, EventKind
from hoh.project import (ActionClass, DecisionKind, DecisionRecord, ExternalActionRecord,
                         GateOutcome, GateResult, Lifecycle, ProjectState, TaskNode)
from hoh.projectstore import ProjectStore

READY, RUNNING, BLOCKED = Lifecycle.READY, Lifecycle.RUNNING, Lifecycle.BLOCKED
MERGED, CONTINGENT, ABANDONED = Lifecycle.MERGED, Lifecycle.CONTINGENT, Lifecycle.ABANDONED
GREEN, RED, NOT_RUN = GateOutcome.GREEN, GateOutcome.RED, GateOutcome.NOT_RUN
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
ONE_MIB = 1024 * 1024
STAMP = "2026-10-12T00:00:00Z"


def mc():
    """The module under test, looked up at the call: a missing module fails the test that needs
    it, not the collection of the file."""
    try:
        from hoh import missioncontrol
    except ImportError:
        pytest.fail("hoh.missioncontrol does not exist")
    return missioncontrol


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a test started a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


class Hang(BaseException):
    """Raised by the alarm of `Guard`. A `BaseException` on purpose: the module's own
    `except Exception` clauses must not be able to turn a hang into a row a test accepts."""


class Guard:
    """A look that does not return is a failure, not a hung suite."""

    def __init__(self, seconds: int = 10):
        self.seconds = seconds

    def __enter__(self):
        def alarm(signum, frame):
            raise Hang(f"the look did not return within {self.seconds} s: it blocked")

        self.old = signal.signal(signal.SIGALRM, alarm)
        signal.alarm(self.seconds)
        return self

    def __exit__(self, *exc):
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self.old)
        return False


# --------------------------------------------------------------------------- #
# Builders: real projects
# --------------------------------------------------------------------------- #

def _node(node_id, lifecycle=READY, deps=(), **kw) -> TaskNode:
    return TaskNode(id=node_id, lifecycle=lifecycle, dependencies=list(deps), **kw)


def _project(root: Path, nodes=(), project_id="p", **kw) -> ProjectStore:
    store = ProjectStore(root, project_id)
    store.write_state(ProjectState(project_id=project_id, repo_path="/x", nodes=list(nodes), **kw))
    return store


def _gate(name, outcome=GREEN, subject="h1", generation=1, at=STAMP, detail="") -> GateResult:
    return GateResult(name=name, outcome=outcome, subject=subject, generation=generation, at=at,
                      detail=detail)


def _item(item_id, status, claim="a claim") -> EvidenceItem:
    return EvidenceItem(item_id=item_id, status=status, claim=claim, first_seen_iteration=1,
                        last_seen_iteration=1)


def _bundle(root: Path, run_id: str, *statuses: EvidenceStatus, bundle_run=None) -> Path:
    run = root / run_id
    run.mkdir(parents=True, exist_ok=True)
    bundle = EvidenceBundle(run_id=bundle_run or run_id, items=[
        _item(f"i{k}", s) for k, s in enumerate(statuses)])
    (run / "evidence.json").write_text(bundle.model_dump_json(), encoding="utf-8")
    return run / "evidence.json"


def _ask(capsys, root: Path, view: str, *argv: str):
    """`hoh --root ROOT mission VIEW ARGV`: (exit code, stdout, stderr)."""
    try:
        code = cli.main(["--root", str(root), "mission", view, *argv])
    except SystemExit as exc:                       # argparse's own refusals
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _doc(capsys, root: Path, view: str, project: str = "p"):
    with Guard():
        code, out, err = _ask(capsys, root, view, "--project", project, "--json")
    assert err == "", err
    return code, json.loads(out)


def _text(capsys, root: Path, view: str, project: str = "p"):
    with Guard():
        code, out, err = _ask(capsys, root, view, "--project", project)
    assert err == "", err
    return code, out.splitlines()


def _tree(root: Path) -> dict[str, tuple]:
    """Every entry under `root`: kind, mode, size, links, mtime and, for a regular file, content."""
    seen = {}
    for base, dirs, files in os.walk(root):
        for name in ["", *dirs, *files]:
            path = os.path.join(base, name) if name else base
            info = os.lstat(path)
            body = None
            if os.path.isfile(path) and not os.path.islink(path):
                try:
                    body = Path(path).read_bytes()
                except OSError:
                    body = b"unreadable"
            seen[os.path.relpath(path, root)] = (info.st_mode, info.st_size, info.st_nlink,
                                                 info.st_mtime_ns, body)
    return seen


# --------------------------------------------------------------------------- #
# M: the map
# --------------------------------------------------------------------------- #

def _graph(root: Path) -> None:
    """One graph with a place of every kind, a cycle with a tail, an unknown and a repeated
    dependency, and a dependency on a settled node."""
    _project(root, [
        _node("M1", MERGED),
        _node("AB", ABANDONED),
        _node("CO", CONTINGENT),
        _node("RU", RUNNING, run_id="r-ru"),
        _node("RD", deps=["M1"]),
        _node("BR", deps=["RU", "RD", "RU"]),             # a repeated dependency
        _node("UL", BLOCKED, blocked_kind="usage_limit", retry_after="2026-10-12T20:00:00Z"),
        _node("UD", deps=["nope", "M1", "nope"]),         # an unknown one, twice
        _node("CY1", deps=["CY2"]),
        _node("CY2", deps=["CY1"]),
        _node("TL", deps=["CY1", "BR"]),                  # behind a cycle and over a ready chain
        _node("SELF", deps=["SELF"]),
    ])


def test_the_map_gives_every_node_one_place_level_and_its_edges(tmp_path, capsys):
    _graph(tmp_path)
    code, doc = _doc(capsys, tmp_path, "map")
    assert code == 0
    assert list(doc) == ["schema", "view", "root", "project", "write_seq", "totals", "nodes",
                         "cycles", "unreadable"]
    assert doc["schema"] == "hoh-mission/1" and doc["view"] == "map" and doc["project"] == "p"
    assert doc["root"] == str(tmp_path.resolve()) and doc["write_seq"] == 1
    assert doc["totals"] == {"nodes": 12, "edges": 9, "unknown": 1, "cycles": 2, "unplaced": 5,
                             "unreadable": 0}
    rows = {r["node_id"]: r for r in doc["nodes"]}
    assert [r["node_id"] for r in doc["nodes"]] == [
        "M1", "AB", "CO", "RU", "RD", "BR", "UL", "UD", "CY1", "CY2", "TL", "SELF"]   # graph order
    assert list(rows["BR"]) == ["node_id", "place", "level", "shape", "depends_on", "unknown",
                                "dependents"]
    want = {   # node: (place, level, shape, depends_on, unknown, dependents)
        "M1": ("merged", 0, "ok", [], [], ["RD", "UD"]),
        "AB": ("abandoned", 0, "ok", [], [], []),
        "CO": ("contingent", 0, "ok", [], [], []),
        "RU": ("running", 0, "ok", [], [], ["BR"]),
        "RD": ("ready", 1, "ok", ["M1"], [], ["BR"]),
        "BR": ("queued", 2, "ok", ["RU", "RD"], [], ["TL"]),
        "UL": ("blocked", 0, "ok", [], [], []),
        "UD": ("queued", None, "unknown", ["M1"], ["nope"], []),
        "CY1": ("queued", None, "cycle", ["CY2"], [], ["CY2", "TL"]),
        "CY2": ("queued", None, "cycle", ["CY1"], [], ["CY1"]),
        "TL": ("queued", None, "behind", ["CY1", "BR"], [], []),
        "SELF": ("queued", None, "cycle", ["SELF"], [], ["SELF"]),
    }
    got = {k: (r["place"], r["level"], r["shape"], r["depends_on"], r["unknown"], r["dependents"])
           for k, r in rows.items()}
    assert got == want
    assert doc["cycles"] == [["CY1", "CY2"], ["SELF"]]
    assert doc["unreadable"] == {}


def test_the_map_text_is_exactly_these_lines(tmp_path, capsys):
    _graph(tmp_path)
    code, lines = _text(capsys, tmp_path, "map")
    assert code == 0
    assert lines[0] == f"mission map  project p  root {tmp_path.resolve()}"
    assert lines[1:] == [
        "totals: nodes=12  edges=9  unknown=1  cycles=2  unplaced=5  unreadable=0",
        "L0  M1  MERGED  needs=-",
        "L0  AB  ABANDONED  needs=-",
        "L0  CO  CONTINGENT  needs=-",
        "L0  RU  RUNNING  needs=-",
        "L0  UL  BLOCKED  needs=-",
        "L1  RD  READY  needs=M1",
        "L2  BR  QUEUED  needs=RU,RD",
        "UNPLACED  UD  QUEUED  shape=unknown  needs=M1  unknown=nope",
        "UNPLACED  CY1  QUEUED  shape=cycle  needs=CY2  unknown=-",
        "UNPLACED  CY2  QUEUED  shape=cycle  needs=CY1  unknown=-",
        "UNPLACED  TL  QUEUED  shape=behind  needs=CY1,BR  unknown=-",
        "UNPLACED  SELF  QUEUED  shape=cycle  needs=SELF  unknown=-",
        "CYCLE  CY1,CY2",
        "CYCLE  SELF",
    ]


def _random_graph(rng: random.Random, size: int) -> list[TaskNode]:
    ids = [f"n{k}" for k in range(size)]
    lifecycles = [READY, READY, READY, RUNNING, MERGED, ABANDONED, CONTINGENT, BLOCKED]
    nodes = []
    for i in ids:
        deps = [rng.choice(ids + ["ghost"]) for _ in range(rng.choice([0, 0, 1, 1, 2, 3]))]
        life = rng.choice(lifecycles)
        kw = {"blocked_kind": rng.choice(["usage_limit", "human", None])} if life is BLOCKED else {}
        nodes.append(_node(i, life, deps,
                           action_class=rng.choice([ActionClass.INTERNAL] * 4 + [ActionClass.EXTERNAL]),
                           **kw))
    return nodes


def test_the_map_agrees_with_the_board_and_the_frontier_on_random_graphs(tmp_path, capsys):
    """Place, level and shape are not the map's own opinion: the place is the board's bucket, and
    "no level" is exactly what the frontier calls unrunnable (for the queued nodes it lists)."""
    rng = random.Random(20261014)
    for round_ in range(40):
        root = tmp_path / f"r{round_}"
        nodes = _random_graph(rng, rng.choice([1, 2, 5, 9, 14]))
        _project(root, nodes)
        code, board = _doc(capsys, root, "board")
        _, doc = _doc(capsys, root, "map")
        assert code == 0
        bucket = {}
        for name in ("running", "ready", "queued", "blocked", "contingent"):
            for row in board[name]:
                bucket[row["node_id"]] = name
        state = ProjectStore(root, "p").read_state()
        frontier = derive_frontier(state)
        rows = {r["node_id"]: r for r in doc["nodes"]}
        assert len(rows) == len(nodes)
        for n in nodes:
            r = rows[n.id]
            if n.lifecycle in (MERGED, ABANDONED):
                assert r["place"] == n.lifecycle.value.lower()
            else:
                assert r["place"] == bucket[n.id], (n.id, r["place"], bucket[n.id])
            assert r["depends_on"] == list(dict.fromkeys(
                d for d in n.dependencies if d in rows)), n.id
            assert r["unknown"] == list(dict.fromkeys(
                d for d in n.dependencies if d not in rows)), n.id
            if n.id in frontier.unrunnable:
                assert r["level"] is None and r["shape"] != "ok", n.id
            if bucket.get(n.id) == "queued":
                assert (r["level"] is None) == (n.id in frontier.unrunnable), n.id
            if r["level"] is not None:
                assert r["shape"] == "ok" and not r["unknown"]
                want = 1 + max((rows[d]["level"] for d in r["depends_on"]), default=-1)
                assert r["level"] == want, (n.id, r["level"], want)
            else:
                assert r["shape"] in ("cycle", "unknown", "behind")
        for r in doc["nodes"]:                      # the two edge lists are one relation
            for d in r["depends_on"]:
                assert r["node_id"] in rows[d]["dependents"]
        assert doc["totals"]["edges"] == sum(len(r["depends_on"]) for r in doc["nodes"])
        assert doc["totals"]["unplaced"] == sum(r["level"] is None for r in doc["nodes"])


def test_cycles_are_strongly_connected_components_not_reachability(tmp_path, capsys):
    _project(tmp_path, [
        _node("P", deps=["P"]),                                                       # a self loop FIRST
        _node("A", deps=["B"]), _node("B", deps=["C"]), _node("C", deps=["A"]),      # a 3-cycle
        _node("T", deps=["A"]),                                                       # a tail
        _node("X", deps=["Y"]), _node("Y", deps=["X", "Z"]), _node("Z", deps=["Y"]),  # shares Y
        _node("Q", deps=["Q", "A"]),                                                  # a self loop
        _node("OK", deps=[]),
    ])
    _, doc = _doc(capsys, tmp_path, "map")
    # ordered by their first member in graph order, not by size
    assert doc["cycles"] == [["P"], ["A", "B", "C"], ["X", "Y", "Z"], ["Q"]]
    shape = {r["node_id"]: r["shape"] for r in doc["nodes"]}
    assert shape == {"P": "cycle", "A": "cycle", "B": "cycle", "C": "cycle", "T": "behind",
                     "X": "cycle", "Y": "cycle", "Z": "cycle", "Q": "cycle", "OK": "ok"}
    assert doc["totals"]["cycles"] == 4 and doc["totals"]["unplaced"] == 9


def test_a_chain_and_a_ring_of_two_thousand_nodes_are_mapped_without_recursion(tmp_path, capsys):
    n = 2000
    chain = [_node(f"c{k}", deps=[f"c{k - 1}"] if k else []) for k in range(n)]
    _project(tmp_path / "chain", chain)
    code, doc = _doc(capsys, tmp_path / "chain", "map")
    assert code == 0 and doc["cycles"] == [] and doc["nodes"][-1]["level"] == n - 1
    ring = [_node(f"c{k}", deps=[f"c{(k + 1) % n}"]) for k in range(n)]
    _project(tmp_path / "ring", ring)
    code, doc = _doc(capsys, tmp_path / "ring", "map")
    assert code == 0 and len(doc["cycles"]) == 1 and len(doc["cycles"][0]) == n
    assert doc["totals"]["unplaced"] == n


def test_the_map_lists_a_log_it_cannot_read_and_exits_1(tmp_path, capsys):
    """The map reads the logs the board reads, and says as the board does when one cannot be."""
    log = tmp_path / "r-a" / "autonomy" / "routings.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("{nope}\n", encoding="utf-8")
    _project(tmp_path, [_node("A", BLOCKED, run_id="r-a", blocked_kind="human"),
                        _node("B", deps=["A"])])
    code, doc = _doc(capsys, tmp_path, "map")
    assert code == 1 and doc["totals"]["unreadable"] == 1 and list(doc["unreadable"]) == ["A"]
    assert {r["node_id"]: r["place"] for r in doc["nodes"]} == {"A": "blocked", "B": "queued"}
    code, lines = _text(capsys, tmp_path, "map")
    assert code == 1 and lines[-1].startswith("UNREADABLE  A  ")


def test_the_map_reads_the_double_down_join_as_the_board_does(tmp_path, capsys, monkeypatch):
    """P3-42's read model (`doubledown.decisions_for_project`) is part of how the board is read:
    a dossier that cannot be read holds its node and is listed, in the map exactly as in the
    board; and the two project views that read no run (`timeline`, `evidence`) never ask it."""
    _project(tmp_path, [_node("A", BLOCKED, note="waits", blocked_kind="human"),
                        _node("B", deps=["A"])])
    calls = []

    def join(base, state, **kw):
        calls.append(state.project_id)
        return {}, {"A": "double-down dossier: torn"}, []

    monkeypatch.setattr(mc().doubledown, "decisions_for_project", join)
    code, board = _doc(capsys, tmp_path, "board")
    code_m, doc = _doc(capsys, tmp_path, "map")
    assert code == code_m == 1 and calls == ["p", "p"]
    assert doc["unreadable"] == board["unreadable"] == {"A": "double-down dossier: torn"}
    assert {r["node_id"]: r["place"] for r in doc["nodes"]} == {"A": "blocked", "B": "queued"}
    for view in ("timeline", "evidence"):
        code, out = _doc(capsys, tmp_path, view)
        assert code == 0 and out["unreadable"] == {}
    assert calls == ["p", "p"]                                # the two views above did not ask


# --------------------------------------------------------------------------- #
# T: the timeline
# --------------------------------------------------------------------------- #

def _event(seq, kind, node, at, **payload) -> Event:
    return Event(event_id=f"dispatch/{node}/{seq}", sequence=seq, kind=kind,
                 activity_id=f"dispatch/{node}", recorded_at=at,
                 payload={"name": "dispatch", "idempotency_key": f"dispatch:{node}",
                          "node": node, **payload})


def _records(root: Path) -> None:
    _project(
        root, [_node("A", MERGED), _node("B")],
        activity_events=[
            _event(1, EventKind.STARTED, "A", "2026-10-12T10:00:00Z"),
            _event(2, EventKind.FAILED, "A", "2026-10-12T10:05:00Z", failure="rejected twice"),
            _event(3, EventKind.STARTED, "A", "2026-10-12T10:06:00Z")],
        decisions=[
            DecisionRecord(id="d1", kind=DecisionKind.MERGE_RELEASE, actor="human",
                           reason="merge A", payload={"node": "A"}, at="2026-10-12T11:00:00Z"),
            DecisionRecord(id="d2", kind=DecisionKind.CLOSURE_VERDICT, actor="orchestrator",
                           reason="closed", at="2026-10-12T09:00:00Z")],
        external_actions=[
            ExternalActionRecord(action_id="x1", actor="human", reason="resolved a conflict",
                                 node="B", at="2026-10-12T10:00:00Z")],
        gates=[_gate("unit", GREEN, at="2026-10-12T10:00:00Z"),
               _gate("lint", RED, at="2026-10-12T12:00:00Z")])


def test_the_timeline_lists_every_dated_record_once_oldest_first(tmp_path, capsys):
    _records(tmp_path)
    code, doc = _doc(capsys, tmp_path, "timeline")
    assert code == 0
    assert list(doc) == ["schema", "view", "root", "project", "write_seq", "totals", "events",
                         "unreadable"]
    assert doc["view"] == "timeline" and doc["project"] == "p" and doc["unreadable"] == {}
    assert doc["totals"] == {"events": 8, "dispatch": 3, "decision": 2, "external": 1, "gate": 2,
                             "undated": 0, "shown": 8, "omitted": 0, "unreadable": 0}
    rows = [(e["at"], e["source"], e["index"], e["what"], e["node_id"], e["detail"], e["dated"])
            for e in doc["events"]]
    assert list(doc["events"][0]) == ["at", "source", "index", "what", "node_id", "detail", "dated"]
    assert rows == [
        ("2026-10-12T09:00:00Z", "decision", 1, "CLOSURE_VERDICT", None, "orchestrator: closed", True),
        ("2026-10-12T10:00:00Z", "dispatch", 0, "STARTED", "A", "sequence=1", True),
        ("2026-10-12T10:00:00Z", "external", 0, "repository-mutation", "B",
         "human: resolved a conflict", True),                        # same second: source order
        ("2026-10-12T10:00:00Z", "gate", 0, "GREEN", None, "unit  generation=1  subject=h1", True),
        ("2026-10-12T10:05:00Z", "dispatch", 1, "FAILED", "A",
         "sequence=2  failure=rejected twice", True),
        ("2026-10-12T10:06:00Z", "dispatch", 2, "STARTED", "A", "sequence=3", True),
        ("2026-10-12T11:00:00Z", "decision", 0, "MERGE_RELEASE", "A", "human: merge A", True),
        ("2026-10-12T12:00:00Z", "gate", 1, "RED", None, "lint  generation=1  subject=h1", True),
    ]


def test_the_timeline_text_is_exactly_these_lines(tmp_path, capsys):
    _records(tmp_path)
    code, lines = _text(capsys, tmp_path, "timeline")
    assert code == 0
    assert lines == [
        f"mission timeline  project p  root {tmp_path.resolve()}",
        "events: 8  shown: 8  omitted: 0  undated: 0",
        "2026-10-12T09:00:00Z  DECISION  CLOSURE_VERDICT  orchestrator: closed",
        "2026-10-12T10:00:00Z  DISPATCH  STARTED  node=A  sequence=1",
        "2026-10-12T10:00:00Z  EXTERNAL  repository-mutation  node=B  human: resolved a conflict",
        "2026-10-12T10:00:00Z  GATE  GREEN  unit  generation=1  subject=h1",
        "2026-10-12T10:05:00Z  DISPATCH  FAILED  node=A  sequence=2  failure=rejected twice",
        "2026-10-12T10:06:00Z  DISPATCH  STARTED  node=A  sequence=3",
        "2026-10-12T11:00:00Z  DECISION  MERGE_RELEASE  node=A  human: merge A",
        "2026-10-12T12:00:00Z  GATE  RED  lint  generation=1  subject=h1",
    ]


#: (The product's model strips surrounding white space on load, so a stamp with a trailing newline
#: arrives as a good one; it is not a case the view can see.)
BAD_STAMPS = ["", "yesterday", "2026-10-12", "2026-10-12T00:00:00+00:00", "2026-10-12T00:00:00Z x",
              "2026-10-12 00:00:00Z", "2026-10-12T00:00:00z", "2026-10-12T00:00:00ZZ",
              "\uff12\uff10\uff12\uff16-10-12T00:00:00Z", "2026-10-12T00:00:00.5Z",
              "2026-10-12T00:00:0\u0663Z"]


@pytest.mark.parametrize("stamp", BAD_STAMPS)
def test_a_record_without_a_stamp_of_the_product_form_is_listed_first_and_counted(
        tmp_path, capsys, stamp):
    _project(tmp_path, [], decisions=[
        DecisionRecord(id="d1", kind=DecisionKind.ABANDON_NODE, actor="human", reason="late",
                       at="2026-10-12T00:00:00Z"),
        DecisionRecord(id="d2", kind=DecisionKind.ABANDON_NODE, actor="human", reason="odd",
                       at=stamp)])
    code, doc = _doc(capsys, tmp_path, "timeline")
    assert code == 0 and doc["totals"]["undated"] == 1 and doc["totals"]["events"] == 2
    assert [(e["detail"], e["dated"]) for e in doc["events"]] == [("human: odd", False),
                                                                  ("human: late", True)]
    assert doc["events"][0]["at"] == stamp                    # as recorded, never repaired
    code, lines = _text(capsys, tmp_path, "timeline")
    assert lines[1] == "events: 2  shown: 2  omitted: 0  undated: 1"
    assert lines[2].endswith("  human: odd  UNDATED") and "UNDATED" not in lines[3]


def test_the_timeline_lists_at_most_500_records_the_newest_and_counts_the_rest(tmp_path, capsys):
    def gates(n):
        return [_gate(f"g{k:04d}", at=f"2026-10-12T{k // 3600:02d}:{k // 60 % 60:02d}:{k % 60:02d}Z")
                for k in range(n)]

    _project(tmp_path / "a", gates=gates(500))
    _, doc = _doc(capsys, tmp_path / "a", "timeline")
    assert doc["totals"]["shown"] == 500 and doc["totals"]["omitted"] == 0
    assert len(doc["events"]) == 500
    _project(tmp_path / "b", gates=gates(501))
    _, doc = _doc(capsys, tmp_path / "b", "timeline")
    assert doc["totals"] == {"events": 501, "dispatch": 0, "decision": 0, "external": 0,
                             "gate": 501, "undated": 0, "shown": 500, "omitted": 1, "unreadable": 0}
    assert doc["events"][0]["index"] == 1 and doc["events"][-1]["index"] == 500   # the oldest went
    _, lines = _text(capsys, tmp_path / "b", "timeline")
    assert lines[1] == "events: 501  shown: 500  omitted: 1  undated: 0" and len(lines) == 502


def test_a_timeline_with_nothing_recorded_is_an_answer_not_an_error(tmp_path, capsys):
    _project(tmp_path, [_node("A")])
    code, doc = _doc(capsys, tmp_path, "timeline")
    assert code == 0 and doc["events"] == [] and doc["totals"]["events"] == 0
    code, lines = _text(capsys, tmp_path, "timeline")
    assert code == 0 and lines[1] == "events: 0  shown: 0  omitted: 0  undated: 0"


def test_an_undated_record_is_never_cut_by_the_row_cap(tmp_path, capsys):
    """The cap is on the dated records (the newest 500); a record without a stamp of the
    product's form is listed first and counted, whatever the number of dated ones."""
    def stamp(k):
        return f"2026-10-12T{k // 3600:02d}:{k // 60 % 60:02d}:{k % 60:02d}Z"

    _project(
        tmp_path, [],
        gates=[_gate(f"g{k:04d}", at=stamp(k)) for k in range(600)],
        decisions=[DecisionRecord(id=f"u{k}", kind=DecisionKind.ABANDON_NODE, actor="human",
                                  reason=f"odd {k}", at="yesterday") for k in range(3)])
    code, doc = _doc(capsys, tmp_path, "timeline")
    assert code == 0
    assert doc["totals"] == {"events": 603, "dispatch": 0, "decision": 3, "external": 0,
                             "gate": 600, "undated": 3, "shown": 503, "omitted": 100,
                             "unreadable": 0}
    events = doc["events"]
    assert len(events) == 503
    assert [(e["dated"], e["detail"]) for e in events[:3]] == [
        (False, "human: odd 0"), (False, "human: odd 1"), (False, "human: odd 2")]
    assert [e["index"] for e in events[3:]] == list(range(100, 600))       # the newest 500
    code, lines = _text(capsys, tmp_path, "timeline")
    assert lines[1] == "events: 603  shown: 503  omitted: 100  undated: 3"
    assert len(lines) == 2 + 503 and [x.endswith("  UNDATED") for x in lines[2:5]] == [True] * 3
    assert not any(x.endswith("  UNDATED") for x in lines[5:])


def test_records_with_equal_stamps_keep_the_order_of_their_source_and_their_list(
        tmp_path, capsys):
    at = "2026-10-12T10:00:00Z"
    _project(
        tmp_path, [],
        activity_events=[_event(1, EventKind.STARTED, "A", at),
                         _event(2, EventKind.SUCCEEDED, "A", at)],
        decisions=[DecisionRecord(id=f"d{k}", kind=DecisionKind.ABANDON_NODE, actor="human",
                                  reason=f"r{k}", at=at) for k in range(3)],
        external_actions=[ExternalActionRecord(action_id="x", actor="human", reason="x", at=at)],
        gates=[_gate("g0", at=at), _gate("g1", at=at)])
    _, doc = _doc(capsys, tmp_path, "timeline")
    assert [(e["source"], e["index"]) for e in doc["events"]] == [
        ("dispatch", 0), ("dispatch", 1), ("decision", 0), ("decision", 1), ("decision", 2),
        ("external", 0), ("gate", 0), ("gate", 1)]


# --------------------------------------------------------------------------- #
# E: evidence health
# --------------------------------------------------------------------------- #

def _closure_project(root: Path, **kw) -> None:
    _project(root, [_node("A", MERGED, run_id="r-a"), _node("B", MERGED, run_id="r-b"),
                    _node("C", MERGED, run_id="r-a"), _node("D", MERGED),
                    _node("R", MERGED, repair_of="A")],
             gates=[_gate("old", GREEN, subject="h0", generation=1),
                    _gate("unit", GREEN, subject="h1", generation=2),
                    _gate("lint", RED, subject="h1", generation=2),
                    _gate("docs", GREEN, subject="h0", generation=2),
                    _gate("slow", NOT_RUN, subject="h1", generation=2),
                    _gate("audit", RED, subject="h0", generation=2)],   # RED and not current: not stale
             measurement_head="h1", closure_generation=2, **kw)


def test_the_evidence_view_reads_the_latest_pass_against_the_measured_head(tmp_path, capsys):
    _closure_project(tmp_path)
    _bundle(tmp_path, "r-a", EvidenceStatus.VERIFIED, EvidenceStatus.VERIFIED)
    _bundle(tmp_path, "r-b", EvidenceStatus.VERIFIED, EvidenceStatus.GAP, EvidenceStatus.GAP,
            EvidenceStatus.INSUFFICIENT)
    code, doc = _doc(capsys, tmp_path, "evidence")
    assert code == 0
    assert list(doc) == ["schema", "view", "root", "project", "write_seq", "totals", "closure",
                         "gates", "runs", "unreadable"]
    assert doc["closure"] == {"generation": 2, "measurement_head": "h1", "dag_terminal": True,
                              "gates_green": False, "open_repairs": [], "rc_closed": False,
                              "retired_gates": ["old"]}
    assert [(g["name"], g["outcome"], g["current"], g["stale"], g["generation"])
            for g in doc["gates"]] == [
        ("unit", "GREEN", True, False, 2), ("lint", "RED", True, False, 2),
        ("docs", "GREEN", False, True, 2), ("slow", "NOT_RUN", True, False, 2),
        ("audit", "RED", False, False, 2)]
    assert list(doc["gates"][0]) == ["name", "outcome", "subject", "current", "stale", "generation",
                                     "at", "detail"]
    assert doc["totals"] == {"gates": 5, "gates_green": 2, "gates_stale": 1, "runs": 2,
                             "no_bundle": 0, "verified": 3, "gap": 2, "regression": 0,
                             "insufficient": 1, "nodes_without_run": 2, "unreadable": 0}


def test_the_closure_lines_are_the_projects_own_answers(tmp_path, capsys):
    """`gates_green`, `rc_closed`, `dag_terminal`, `retired_gates` come from the state itself: on
    random gate sets and node sets the view says what `ProjectState` says."""
    rng = random.Random(7)
    for round_ in range(30):
        root = tmp_path / f"r{round_}"
        gates = [_gate(rng.choice("abcd"), rng.choice(list(GateOutcome)),
                       subject=rng.choice(["h1", "h2"]), generation=rng.choice([0, 1, 2, 3]))
                 for _ in range(rng.choice([0, 1, 3, 6]))]
        nodes = [_node(f"n{k}", rng.choice([MERGED, MERGED, READY, ABANDONED]),
                       repair_of=rng.choice([None, None, "n0"])) for k in range(rng.choice([0, 3, 5]))]
        _project(root, nodes, gates=gates, measurement_head=rng.choice([None, "h1"]),
                 closure_generation=rng.choice([0, 3]))
        state = ProjectStore(root, "p").read_state()
        _, doc = _doc(capsys, root, "evidence")
        c = doc["closure"]
        assert (c["gates_green"], c["rc_closed"], c["dag_terminal"], c["retired_gates"],
                c["generation"], c["measurement_head"]) == (
            state.gates_green(), state.rc_closed(), state.dag_terminal(), state.retired_gates(),
            state.closure_generation, state.measurement_head)
        assert c["open_repairs"] == [n.id for n in state.nodes if n.repair_of and not n.settled]
        now = state.latest_generation()
        latest = [g for g in state.gates if g.generation == now] or list(state.gates)
        by_name = {}
        for g in latest:
            by_name[g.name] = g
        assert [(g["name"], g["outcome"], g["subject"]) for g in doc["gates"]] == [
            (g.name, g.outcome.value, g.subject) for g in by_name.values()]
        if state.measurement_head is None:
            assert all(g["current"] is None and not g["stale"] for g in doc["gates"])
        assert c["gates_green"] == (bool(doc["gates"])
                                    and all(g["outcome"] == "GREEN" for g in doc["gates"]))


def test_the_evidence_of_each_run_is_counted_by_status_and_the_worst_is_named(tmp_path, capsys):
    S = EvidenceStatus
    _project(tmp_path, [_node("A", MERGED, run_id="r-ok"), _node("B", run_id="r-gap"),
                        _node("C", RUNNING, run_id="r-reg"), _node("D", run_id="r-ins"),
                        _node("E", run_id="r-empty"), _node("F", run_id="r-none"),
                        _node("G", MERGED, run_id="r-ok"), _node("H")])
    _bundle(tmp_path, "r-ok", S.VERIFIED, S.VERIFIED)
    _bundle(tmp_path, "r-gap", S.VERIFIED, S.GAP, S.INSUFFICIENT)
    _bundle(tmp_path, "r-reg", S.GAP, S.REGRESSION, S.VERIFIED, S.INSUFFICIENT)
    _bundle(tmp_path, "r-ins", S.INSUFFICIENT, S.VERIFIED)
    _bundle(tmp_path, "r-empty")
    (tmp_path / "r-none").mkdir()
    code, doc = _doc(capsys, tmp_path, "evidence")
    assert code == 0 and doc["unreadable"] == {}
    runs = {r["run_id"]: r for r in doc["runs"]}
    assert [r["run_id"] for r in doc["runs"]] == ["r-ok", "r-gap", "r-reg", "r-ins", "r-empty",
                                                  "r-none"]                  # node order
    assert list(runs["r-ok"]) == ["run_id", "nodes", "bundle", "items", "verified", "gap",
                                  "regression", "insufficient", "worst"]
    got = {k: (r["nodes"], r["bundle"], r["items"], r["verified"], r["gap"], r["regression"],
               r["insufficient"], r["worst"]) for k, r in runs.items()}
    assert got == {
        "r-ok": (["A", "G"], True, 2, 2, 0, 0, 0, "VERIFIED"),
        "r-gap": (["B"], True, 3, 1, 1, 0, 1, "GAP"),
        "r-reg": (["C"], True, 4, 1, 1, 1, 1, "REGRESSION"),
        "r-ins": (["D"], True, 2, 1, 0, 0, 1, "INSUFFICIENT"),
        "r-empty": (["E"], True, 0, 0, 0, 0, 0, "EMPTY"),
        "r-none": (["F"], False, 0, 0, 0, 0, 0, "NONE"),
    }
    assert doc["totals"] == {"gates": 0, "gates_green": 0, "gates_stale": 0, "runs": 6,
                             "no_bundle": 1, "verified": 5, "gap": 2, "regression": 1,
                             "insufficient": 3, "nodes_without_run": 1, "unreadable": 0}


def test_the_evidence_text_is_exactly_these_lines(tmp_path, capsys):
    S = EvidenceStatus
    _closure_project(tmp_path)
    _bundle(tmp_path, "r-a", S.VERIFIED, S.REGRESSION)
    (tmp_path / "r-b").mkdir()
    code, lines = _text(capsys, tmp_path, "evidence")
    assert code == 0
    assert lines == [
        f"mission evidence  project p  root {tmp_path.resolve()}",
        "totals: gates=5  gates_green=2  gates_stale=1  runs=2  no_bundle=1  verified=1  gap=0  "
        "regression=1  insufficient=0  nodes_without_run=2  unreadable=0",
        "closure: generation=2  measured_at=h1  dag_terminal=true  gates_green=false  "
        "rc_closed=false  open_repairs=-  retired_gates=old",
        "GATE  unit  GREEN  current  generation=2  subject=h1",
        "GATE  lint  RED  current  generation=2  subject=h1",
        "GATE  docs  GREEN  NOT_CURRENT  STALE  generation=2  subject=h0",
        "GATE  slow  NOT_RUN  current  generation=2  subject=h1",
        "GATE  audit  RED  NOT_CURRENT  generation=2  subject=h0",
        "RUN  r-a  REGRESSION  items=2  verified=1  gap=0  regression=1  insufficient=0  nodes=A,C",
        "RUN  r-b  NONE  items=0  verified=0  gap=0  regression=0  insufficient=0  nodes=B",
    ]


def test_the_gates_of_a_project_that_never_closed_are_all_listed_last_result_per_name(
        tmp_path, capsys):
    """Every gate is generation 0 (results written before the field existed, or a pass that was
    not run): the latest generation is 0 and all of them are candidates, the last per name wins."""
    _project(tmp_path, gates=[_gate("a", RED, generation=0), _gate("b", GREEN, generation=0),
                              _gate("a", GREEN, generation=0)])
    code, doc = _doc(capsys, tmp_path, "evidence")
    assert code == 0
    assert [(g["name"], g["outcome"], g["generation"]) for g in doc["gates"]] == [
        ("a", "GREEN", 0), ("b", "GREEN", 0)]
    assert doc["closure"]["gates_green"] is True and doc["totals"]["gates"] == 2


def test_a_project_with_no_gates_and_no_head_says_so(tmp_path, capsys):
    _project(tmp_path, [_node("A")])
    code, doc = _doc(capsys, tmp_path, "evidence")
    assert code == 0 and doc["gates"] == [] and doc["runs"] == []
    assert doc["closure"]["measurement_head"] is None and doc["closure"]["gates_green"] is False
    code, lines = _text(capsys, tmp_path, "evidence")
    assert lines[2] == ("closure: generation=0  measured_at=-  dag_terminal=false  "
                        "gates_green=false  rc_closed=false  open_repairs=-  retired_gates=-")
    _project(tmp_path / "h", gates=[_gate("unit", GREEN)])          # a gate and no head
    _, doc = _doc(capsys, tmp_path / "h", "evidence")
    assert doc["gates"][0]["current"] is None and doc["gates"][0]["stale"] is False
    _, lines = _text(capsys, tmp_path / "h", "evidence")
    assert lines[3] == "GATE  unit  GREEN  no_head  generation=1  subject=h1"


def _plant_evidence(root: Path, how: str, run_id: str) -> None:
    """The evidence of run `run_id` in the way `how` names; node `A` names the run."""
    run = root / run_id
    outside = root.parent / (root.name + "-elsewhere")
    outside.mkdir(exist_ok=True)
    good = EvidenceBundle(run_id=run_id,
                          items=[_item("i", EvidenceStatus.VERIFIED)]).model_dump_json()
    if how == "the run directory is absent":
        return
    if how == "the run directory is a link":
        (outside / run_id).mkdir()
        (outside / run_id / "evidence.json").write_text(good, encoding="utf-8")
        os.symlink(outside / run_id, run)
        return
    if how == "the run directory is a file":
        run.write_text("not a directory", encoding="utf-8")
        return
    run.mkdir()
    path = run / "evidence.json"
    if how == "evidence.json is a FIFO":
        os.mkfifo(path)
    elif how == "evidence.json is a directory":
        path.mkdir()
    elif how == "evidence.json is a link to a good file":
        (outside / "good.json").write_text(good, encoding="utf-8")
        os.symlink(outside / "good.json", path)
    elif how == "evidence.json is a dangling link":
        os.symlink(outside / "gone.json", path)
    elif how == "evidence.json is mode 000":
        path.write_text(good, encoding="utf-8")
        path.chmod(0)
    elif how == "larger than one mebibyte":
        path.write_text(good + " " * (ONE_MIB + 1 - len(good)), encoding="utf-8")
    elif how == "Latin-1 bytes":
        path.write_bytes(good.replace('"a claim"', '"a cl\u00e4im"').encode("latin-1"))
    elif how == "UTF-16 with a BOM":
        path.write_bytes(good.encode("utf-16"))
    elif how == "a NUL byte":
        path.write_bytes(good.encode() + b"\x00")
    elif how == "a torn file":
        path.write_text(good[: len(good) // 2], encoding="utf-8")
    elif how == "a JSON list":
        path.write_text("[1, 2]", encoding="utf-8")
    elif how == "JSON null":
        path.write_text("null", encoding="utf-8")
    elif how == "a deeply nested array":
        path.write_text("[" * 200000 + "]" * 200000, encoding="utf-8")
    elif how == "an unknown key":
        path.write_text(json.dumps({**json.loads(good), "surprise": 1}), encoding="utf-8")
    elif how == "an unknown status":
        record = json.loads(good)
        record["items"][0]["status"] = "WAVED_THROUGH"
        path.write_text(json.dumps(record), encoding="utf-8")
    elif how == "another run's bundle":
        path.write_text(json.dumps({**json.loads(good), "run_id": "r-elsewhere"}), encoding="utf-8")
    else:
        raise AssertionError(how)


UNREADABLE_EVIDENCE = [
    "the run directory is absent", "the run directory is a link", "the run directory is a file",
    "evidence.json is a FIFO",
    "evidence.json is a directory", "evidence.json is a link to a good file",
    "evidence.json is a dangling link", "evidence.json is mode 000", "larger than one mebibyte",
    "Latin-1 bytes", "UTF-16 with a BOM", "a NUL byte", "a torn file", "a JSON list", "JSON null",
    "a deeply nested array", "an unknown key", "an unknown status", "another run's bundle",
]


#: What each reason says: a reader who is told "unreadable" is told why.
WHY = {
    "the run directory is absent": "the run directory does not exist",
    "the run directory is a link": "the run directory is a symbolic link (not followed)",
    "the run directory is a file": "the run directory is not a directory",
    "evidence.json is a FIFO": "evidence.json is not a regular file",
    "evidence.json is a directory": "evidence.json is not a regular file",
    "evidence.json is a link to a good file": "evidence.json is a symbolic link (not followed)",
    "evidence.json is a dangling link": "evidence.json is a symbolic link (not followed)",
    "evidence.json is mode 000": "evidence.json cannot be opened: Permission denied",
    "larger than one mebibyte": "evidence.json is larger than 1048576 bytes (not read)",
    "Latin-1 bytes": "evidence.json is not an evidence bundle: ",
    "UTF-16 with a BOM": "evidence.json is not an evidence bundle: ",
    "a NUL byte": "evidence.json is not an evidence bundle: ",
    "a torn file": "evidence.json is not an evidence bundle: ",
    "a JSON list": "evidence.json is not an evidence bundle: ",
    "JSON null": "evidence.json is not an evidence bundle: ",
    "a deeply nested array": "evidence.json is not an evidence bundle: ",
    "an unknown key": "evidence.json is not an evidence bundle: ",
    "an unknown status": "evidence.json is not an evidence bundle: ",
    "another run's bundle": "evidence.json says it is the bundle of run r-elsewhere: not read as this one",
}


@pytest.mark.parametrize("how", UNREADABLE_EVIDENCE)
def test_an_evidence_bundle_that_cannot_be_read_is_listed_not_counted_and_exits_1(
        tmp_path, capsys, how):
    if how == "evidence.json is mode 000" and IS_ROOT:
        pytest.skip("root reads mode 000 files")
    run_id = "r-a"
    _project(tmp_path, [_node("A", MERGED, run_id=run_id), _node("B", MERGED, run_id="r-ok")])
    _bundle(tmp_path, "r-ok", EvidenceStatus.VERIFIED)
    _plant_evidence(tmp_path, how, run_id)
    code, doc = _doc(capsys, tmp_path, "evidence")
    assert code == 1, (how, doc["unreadable"])
    assert list(doc["unreadable"]) == [run_id], doc["unreadable"]
    assert doc["unreadable"][run_id].startswith(WHY[how]), doc["unreadable"][run_id]
    assert [r["run_id"] for r in doc["runs"]] == ["r-ok"]            # never counted as empty
    assert doc["totals"]["unreadable"] == 1 and doc["totals"]["runs"] == 2
    assert doc["totals"]["verified"] == 1
    code, lines = _text(capsys, tmp_path, "evidence")
    assert code == 1 and any(x.startswith(f"UNREADABLE  {run_id}  ") for x in lines)


@pytest.mark.parametrize("run_id", ["..", "../x", "/etc", "", "a/b", "_hidden", "projects",
                                    "backups", "a\x00b", "a" * 300])
def test_a_run_id_that_is_not_a_run_directory_name_is_listed_as_unreadable(
        tmp_path, capsys, run_id):
    _project(tmp_path, [_node("A", MERGED, run_id=run_id)])
    code, doc = _doc(capsys, tmp_path, "evidence")
    assert code == 1 and list(doc["unreadable"]) == [run_id] and doc["runs"] == []


def test_the_evidence_ceiling_is_one_mebibyte_exactly(tmp_path, capsys, monkeypatch):
    _project(tmp_path, [_node("A", MERGED, run_id="r-a"), _node("B", MERGED, run_id="r-b")])
    good = EvidenceBundle(run_id="r-a", items=[_item("i", EvidenceStatus.GAP)]).model_dump_json()
    for run_id, size in (("r-a", ONE_MIB), ("r-b", ONE_MIB + 1)):
        run = tmp_path / run_id
        run.mkdir()
        body = good.replace("r-a", run_id)
        (run / "evidence.json").write_text(body + " " * (size - len(body)), encoding="utf-8")
    taken: dict[str, int] = {}
    names: dict[int, str] = {}
    real_open, real_read = os.open, os.read

    def opening(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        names[fd] = str(path)
        return fd

    def counting(fd, n):
        data = real_read(fd, n)
        taken[names.get(fd, "")] = taken.get(names.get(fd, ""), 0) + len(data)
        return data

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(os, "read", counting)
    code, doc = _doc(capsys, tmp_path, "evidence")
    assert code == 1 and [r["run_id"] for r in doc["runs"]] == ["r-a"]
    assert doc["runs"][0]["gap"] == 1
    assert doc["unreadable"]["r-b"].startswith("evidence.json is larger than 1048576 bytes")
    # The larger file was refused on its descriptor: not one byte of it was read.
    assert taken[str(tmp_path.resolve() / "r-a" / "evidence.json")] == ONE_MIB
    assert str(tmp_path.resolve() / "r-b" / "evidence.json") not in taken, taken


def test_runs_that_several_nodes_name_are_read_once_and_only_named_runs_are_opened(
        tmp_path, capsys, monkeypatch):
    _project(tmp_path, [_node(f"A{k}", MERGED, run_id="r-a") for k in range(4)] + [_node("B")])
    _bundle(tmp_path, "r-a", EvidenceStatus.VERIFIED)
    stranger = tmp_path / "r-stranger"
    stranger.mkdir()
    os.mkfifo(stranger / "evidence.json")                    # not named by a node: not opened
    seen = []
    real = EvidenceBundle.model_validate_json

    def counting(text, *a, **kw):
        seen.append(len(text))
        return real(text, *a, **kw)

    monkeypatch.setattr(EvidenceBundle, "model_validate_json", staticmethod(counting))
    code, doc = _doc(capsys, tmp_path, "evidence")
    assert code == 0 and len(seen) == 1, seen
    assert doc["runs"][0]["nodes"] == ["A0", "A1", "A2", "A3"]


def test_a_reason_is_cut_at_240_characters_in_json_and_120_in_text(tmp_path, capsys):
    _project(tmp_path, [_node("A", MERGED, run_id="r-a")])
    run = tmp_path / "r-a"
    run.mkdir()
    (run / "evidence.json").write_text('{"run_id": "r-a", "items": [], "zzzz": "' + "k" * 1000 + '"}',
                                       encoding="utf-8")
    code, doc = _doc(capsys, tmp_path, "evidence")
    why = doc["unreadable"]["r-a"]
    assert code == 1 and len(why) == 240 and why.endswith("...")
    _, lines = _text(capsys, tmp_path, "evidence")
    line = next(x for x in lines if x.startswith("UNREADABLE  r-a  "))
    assert len(line) == len("UNREADABLE  r-a  ") + 120 and line.endswith("...")


# --------------------------------------------------------------------------- #
# P: the project, shared by the three views
# --------------------------------------------------------------------------- #

NEW_VIEWS = ("map", "timeline", "evidence")


def _plant(root: Path, how: str) -> None:
    """A `projects/p` that cannot be read whole, in the way `how` names."""
    _project(root, [_node("A")], project_id="seed")
    ok = json.dumps({**json.loads((root / "projects" / "seed" / "project.json").read_text()),
                     "project_id": "p"}, indent=2)
    pdir = root / "projects" / "p"
    outside = root.parent / (root.name + "-elsewhere")
    outside.mkdir(exist_ok=True)
    if how == "no project directory":
        return
    pdir.mkdir()
    path = pdir / "project.json"
    if how == "no project.json":
        pass
    elif how == "project.json is a FIFO":
        os.mkfifo(path)
    elif how == "project.json is a link to a good file":
        (outside / "good.json").write_text(ok, encoding="utf-8")
        os.symlink(outside / "good.json", path)
    elif how == "project.json is a directory":
        path.mkdir()
    elif how == "larger than eight mebibytes":
        path.write_text(ok + " " * (8 * ONE_MIB + 1 - len(ok)), encoding="utf-8")
    elif how == "Latin-1 bytes":
        path.write_bytes(ok.replace('"/x"', '"/\u00e7"').encode("latin-1"))
    elif how == "a torn file":
        path.write_text(ok[: len(ok) // 2], encoding="utf-8")
    elif how == "an older schema version":
        path.write_text(json.dumps({**json.loads(ok), "schema_version": 1}), encoding="utf-8")
    elif how == "another project's state":
        path.write_text(json.dumps({**json.loads(ok), "project_id": "someone-else"}),
                        encoding="utf-8")
    elif how == "two nodes with one id":
        record = json.loads(ok)
        path.write_text(json.dumps({**record, "nodes": [record["nodes"][0]] * 2}), encoding="utf-8")
    else:
        raise AssertionError(how)


REFUSED = ["no project directory", "no project.json", "project.json is a FIFO",
           "project.json is a link to a good file", "project.json is a directory",
           "larger than eight mebibytes", "Latin-1 bytes", "a torn file",
           "an older schema version", "another project's state"]


@pytest.mark.parametrize("how", REFUSED)
@pytest.mark.parametrize("view", NEW_VIEWS)
def test_a_project_that_cannot_be_read_whole_is_refused_and_nothing_is_printed(
        tmp_path, capsys, view, how):
    _plant(tmp_path, how)
    with Guard():
        code, out, err = _ask(capsys, tmp_path, view, "--project", "p")
    assert code == 2 and out == "" and err.startswith("mission refused: project p: "), (out, err)
    code, out, err = _ask(capsys, tmp_path, view, "--project", "p", "--json")
    assert code == 2 and out == ""


def test_two_nodes_with_one_id_have_no_map(tmp_path, capsys):
    _plant(tmp_path, "two nodes with one id")
    code, out, err = _ask(capsys, tmp_path, "map", "--project", "p")
    assert code == 2 and out == "" and "two nodes with one id" in err


def test_the_new_views_need_a_project_and_are_reachable_through_the_parser(tmp_path, capsys):
    _project(tmp_path, [_node("A")])
    for view in NEW_VIEWS:
        code, out, err = _ask(capsys, tmp_path, view)
        assert code == 2 and out == "" and f"the {view} view needs a project (--project ID)" in err
        code, out, err = _ask(capsys, tmp_path, view, "--project", "p", "--json")
        assert code == 0 and json.loads(out)["view"] == view and err == ""
        code, out, err = _ask(capsys, tmp_path, view, "--project", "p")
        assert code == 0 and out.startswith(f"mission {view}  project p  root ") and err == ""
        for extra in (["extra"], ["-p", "x"]):
            with pytest.raises(SystemExit) as stop:
                cli.main(["--root", str(tmp_path), "mission", view, *extra])
            assert stop.value.code == 2, (view, extra)
            capsys.readouterr()
    for argv in (["mission", "frontier"], ["mission", "Map"], ["mission", "evidence-health"]):
        with pytest.raises(SystemExit) as stop:
            cli.main(["--root", str(tmp_path), *argv])
        assert stop.value.code == 2, argv
        capsys.readouterr()
    assert mc().VIEWS == ("overview", "board", "needs-you", "map", "timeline", "evidence")
    assert mc().PROJECT_VIEWS == ("board", "needs-you", "map", "timeline", "evidence")
    assert mc().MAX_EVIDENCE_BYTES == ONE_MIB and mc().TIMELINE_ROWS == 500


def test_the_command_reads_the_root_and_the_project_it_is_given(tmp_path, capsys, monkeypatch):
    given, default = tmp_path / "given", tmp_path / "default"
    _project(given, [_node("only-p")], project_id="p")
    _project(given, [_node("only-q"), _node("q2", deps=["only-q"])], project_id="q")
    _project(default, [_node("from-default")], project_id="p")
    for view in NEW_VIEWS:
        _, doc = _doc(capsys, given, view, "q")
        assert doc["project"] == "q" and doc["root"] == str(given.resolve())
    _, doc = _doc(capsys, given, "map", "q")
    assert [r["node_id"] for r in doc["nodes"]] == ["only-q", "q2"]
    monkeypatch.setattr(cli, "DEFAULT_ROOT", default)
    assert cli.main(["mission", "map", "--project", "p", "--json"]) == 0
    assert [r["node_id"] for r in json.loads(capsys.readouterr().out)["nodes"]] == ["from-default"]


@pytest.mark.parametrize("project", ["../p", "a/b", ".", "..", "p\n", "-x", "x" * 65, "p q", ""])
@pytest.mark.parametrize("view", NEW_VIEWS)
def test_a_project_id_that_is_not_a_name_is_refused_by_the_new_views(
        tmp_path, capsys, view, project):
    _project(tmp_path, [_node("A")], project_id="p")
    before = _tree(tmp_path)
    code, out, err = _ask(capsys, tmp_path, view, f"--project={project}")
    assert code == 2 and out == "" and err.startswith("mission refused: project "), (out, err)
    assert _tree(tmp_path) == before


def test_the_root_may_be_a_link_and_is_resolved_once_for_the_new_views(tmp_path, capsys):
    real = tmp_path / "real"
    _records(real)
    os.symlink(real, tmp_path / "alias")
    for view in NEW_VIEWS:
        code, doc = _doc(capsys, tmp_path / "alias", view)
        assert code == 0 and doc["root"] == str(real.resolve()), view


def test_the_new_views_write_nothing_not_even_for_what_they_refuse(tmp_path, capsys):
    _records(tmp_path)
    store = ProjectStore(tmp_path, "p")
    store.write_state(store.read_state())                       # a parked predecessor exists
    _bundle(tmp_path, "r-a", EvidenceStatus.GAP)
    os.mkfifo(tmp_path / "r-fifo")
    older = tmp_path / "projects" / "old"
    older.mkdir()
    older.joinpath("project.json").write_text(
        json.dumps({**json.loads((tmp_path / "projects" / "p" / "project.json").read_text()),
                    "project_id": "old", "schema_version": 1}), encoding="utf-8")
    before = _tree(tmp_path)
    for project, want in (("p", 0), ("old", 2), ("absent", 2)):
        for view in NEW_VIEWS:
            for extra in ((), ("--json",)):
                with Guard():
                    code, out, err = _ask(capsys, tmp_path, view, "--project", project, *extra)
                assert code == want, (project, view, extra, out, err)   # the command did answer
                assert bool(out) == (want == 0) and err.startswith("mission refused") == (want == 2)
    assert _tree(tmp_path) == before
    assert not (tmp_path / "projects" / "p" / ".lock").exists()


def test_every_row_is_one_line_of_printable_ascii_whatever_the_words(tmp_path, capsys):
    ugly = "a\x1b[31mb\r\nc\u202e\u00e9\U0001f600\u2028\x00z"
    _project(
        tmp_path, [_node(ugly, deps=[ugly + "x"], run_id="r-a"), _node("B", MERGED, run_id="r-a")],
        decisions=[DecisionRecord(id="d1", kind=DecisionKind.ABANDON_NODE, actor=ugly, reason=ugly,
                                  payload={"node": ugly}, at=ugly)],
        gates=[_gate(ugly, GREEN, subject=ugly, detail=ugly)], measurement_head="h")
    _bundle(tmp_path, "r-a", EvidenceStatus.GAP)
    for view in NEW_VIEWS:
        with Guard():
            code, out, err = _ask(capsys, tmp_path, view, "--project", "p")
        assert code == 0 and err == "", (view, err)
        for line in out.splitlines():
            assert line.isascii() and line.isprintable(), (view, line)
        assert out.count("\n") == len(out.splitlines()) and "\r" not in out
        with Guard():
            code, raw, err = _ask(capsys, tmp_path, view, "--project", "p", "--json")
        assert code == 0 and raw.isascii() and json.loads(raw)["view"] == view
    _, doc = _doc(capsys, tmp_path, "map")
    assert doc["nodes"][0]["node_id"] == ugly                 # the words are exact in JSON
    _, doc = _doc(capsys, tmp_path, "timeline")
    assert doc["events"][0]["detail"] == f"{ugly}: {ugly}" and doc["events"][0]["at"] == ugly


def test_an_answer_that_cannot_be_written_is_exit_2_for_the_new_views(tmp_path, monkeypatch):
    _records(tmp_path)

    class Broken:
        def write(self, text):
            raise OSError("No space left on device")

        def flush(self):
            raise OSError("No space left on device")

    for view in NEW_VIEWS:
        err = io.StringIO()
        assert mc().run(view, str(tmp_path), project="p", out=Broken(), err=err) == 2
        assert "could not be written" in err.getvalue()
        out = io.StringIO()
        assert mc().run(view, str(tmp_path), project="p", out=out, err=io.StringIO()) == 0
        assert out.getvalue().startswith(f"mission {view}  project p")
    monkeypatch.setattr(sys, "stdout", None)                   # `>&-`
    err = io.StringIO()
    assert mc().run("map", str(tmp_path), project="p", err=err) == 2
    assert "stdout is closed" in err.getvalue()


# --------------------------------------------------------------------------- #
# G: the register rows these views earn
# --------------------------------------------------------------------------- #

def test_the_register_names_the_three_probes_the_views_earn():
    """The probes of P3-34, P3-37 and P3-38 (R-1, with the slice that earns them): a symbol of
    this module, a test of this file that drives the command, no equivalent, and the earlier
    probe kept by addition."""
    root = Path(__file__).resolve().parent.parent
    register = json.loads((root / "program/v3_3/REQUIREMENTS.json").read_text(encoding="utf-8"))
    rows = {r["id"]: r for r in register["requirements"]}
    module = (root / "src/hoh/missioncontrol.py").read_text(encoding="utf-8")
    here = Path(__file__).read_text(encoding="utf-8")
    want = {"P3-34": ("mission_map", "test_the_map_gives_every_node_one_place_level_and_its_edges"),
            "P3-37": ("timeline", "test_the_timeline_lists_every_dated_record_once_oldest_first"),
            "P3-38": ("evidence_health",
                      "test_the_evidence_of_each_run_is_counted_by_status_and_the_worst_is_named")}
    for rid, (symbol, test) in want.items():
        row = rows[rid]
        probe = row["probe"]
        assert row["ui_only"] is True, rid
        assert set(probe) == {"kind", "symbol", "module", "test", "note"}, (rid, sorted(probe))
        assert (probe["kind"], probe["symbol"], probe["module"]) == (
            "symbol", symbol, "missioncontrol.py"), rid
        assert probe["test"] == f"tests/test_missioncontrol_views.py::{test}", rid
        assert f"\ndef {symbol}(" in module and f"\ndef {test}(" in here, rid
        previous = row["previous_probe"]
        assert previous["kind"] == "none" and previous["replaced_on"] == "2026-10-15", rid
        assert previous["by"] == "P3-UI1 (R-1)" and previous["why"], rid
