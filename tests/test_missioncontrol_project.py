"""`hoh mission board` and `hoh mission needs-you` (P3-35 and P3-36, slice 1): the project's graph,
read through the decision frontier (`hoh.decisionfrontier`, P3-27), as what runs, what can run,
what waits and what waits for a person.

Every test builds a real project through the product's own `ProjectStore` and real routing logs
(the product's writer where it matters, the line format where a defect has to be made), holds no
process and starts none: `subprocess.run` and `subprocess.Popen` fail the test. Every odd file a
project directory or a routing log can be is made on disk (a FIFO, a link, a mode-000 file, a
directory where a file belongs, bytes that are not UTF-8) and looked at through the command, with
a guard that turns a hang into a failure.

The exit codes, the vocabulary and the bounds are written out as literals on purpose: they are
the contract of the command, and a table imported from the module would agree with a mutated one.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import random
import signal
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hoh import cli
from hoh.autonomy import AutonomyMode, Escalation, Horizon, Routing, compile_profile, route
from hoh.contracts import Budgets, RunState
from hoh.governance import bind, block_under_contract
from hoh.project import ActionClass, Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore

READY, RUNNING, BLOCKED = Lifecycle.READY, Lifecycle.RUNNING, Lifecycle.BLOCKED
MERGED, CONTINGENT, ABANDONED = Lifecycle.MERGED, Lifecycle.CONTINGENT, Lifecycle.ABANDONED
EXTERNAL = ActionClass.EXTERNAL
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
EIGHT_MIB = 8 * 1024 * 1024


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
# Builders: real projects, real logs
# --------------------------------------------------------------------------- #

UNATTENDED = dict(scope=("src/hoh/",),
                  horizon=Horizon(max_wallclock_seconds=3600, review_after_seconds=900),
                  budgets=Budgets(max_dispatches=10, max_wallclock_seconds=3600))


def _routing(text="needs a licence answer", *, kind="decision", **contract_kw) -> Routing:
    contract = compile_profile(AutonomyMode.UNATTENDED_LONG_HORIZON, contract_id="un",
                               **{**UNATTENDED, **contract_kw})
    return route(contract, decision=text, kind=kind, delegable=False)


def _abort(text="a licence question") -> Routing:
    return _routing(text, escalation={"decision": Escalation.ABORT_BRANCH})


def _node(node_id, lifecycle=READY, deps=(), **kw) -> TaskNode:
    return TaskNode(id=node_id, lifecycle=lifecycle, dependencies=list(deps), **kw)


def _project(root: Path, nodes, project_id="p", **kw) -> ProjectStore:
    store = ProjectStore(root, project_id)
    store.write_state(ProjectState(project_id=project_id, repo_path="/x", nodes=list(nodes), **kw))
    return store


def _log_path(root: Path, run_id: str) -> Path:
    path = root / run_id / "autonomy" / "routings.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _line(routing: Routing, **extra) -> str:
    return json.dumps({"at": "2026-10-12T00:00:00Z", "contract_digest": "d" * 16,
                       "program_epoch": 1, **json.loads(routing.model_dump_json()), **extra})


def _log(root: Path, run_id: str, *routings: Routing) -> None:
    _log_path(root, run_id).write_text(
        "".join(_line(r) + "\n" for r in routings), encoding="utf-8")


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


def _ids(rows) -> list[str]:
    return [row["node_id"] for row in rows]


def _tree(root: Path) -> dict[str, tuple]:
    """Every entry under `root`: kind, mode, size, links, mtime and, for a regular file, content."""
    seen = {}
    for base, dirs, files in os.walk(root):
        for name in ["", *dirs, *files]:
            path = os.path.join(base, name) if name else base
            info = os.lstat(path)
            digest = None
            if os.path.isfile(path) and not os.path.islink(path):
                try:
                    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
                except OSError:
                    digest = "unreadable"
            seen[os.path.relpath(path, root)] = (info.st_mode, info.st_size, info.st_nlink,
                                                 info.st_mtime_ns, digest)
    return seen


# --------------------------------------------------------------------------- #
# B: the board
# --------------------------------------------------------------------------- #

def _rich(root: Path) -> None:
    """One graph with every place a node can be."""
    _log(root, "run-pk", _routing())
    _log(root, "run-en", _abort())
    _project(root, [
        _node("M1", MERGED),
        _node("AB", ABANDONED),
        _node("CO", CONTINGENT),
        _node("RU", RUNNING, run_id="run-ru"),
        _node("RD", deps=["M1"]),
        _node("RD2", deps=["AB"]),
        _node("PK", BLOCKED, run_id="run-pk", blocked_kind="parked_by_contract",
              retry_after="2026-10-12T21:00:00Z"),
        _node("HQ", deps=["PK", "M1", "PK"]),            # a settled and a repeated dependency
        _node("HQ2", deps=["HQ"]),
        _node("BR", deps=["RU"]),
        _node("UL", BLOCKED, blocked_kind="usage_limit", retry_after="2026-10-12T20:00:00Z"),
        _node("OT", BLOCKED, note="waiting for a merge"),
        _node("EN", BLOCKED, run_id="run-en", blocked_kind="contract_abort"),
        _node("EH", deps=["EN"]),
        _node("XT", action_class=EXTERNAL),
        _node("XQ", deps=["RU"], action_class=EXTERNAL),
        _node("UD", deps=["nope"]),
        _node("CY1", deps=["CY2"]),
        _node("CY2", deps=["CY1"]),
    ])


def test_the_board_places_every_node_once_where_the_frontier_puts_it(tmp_path, capsys):
    _rich(tmp_path)
    code, doc = _doc(capsys, tmp_path, "board")
    assert code == 0
    assert list(doc) == ["schema", "view", "root", "project", "write_seq", "totals", "running",
                         "ready", "queued", "blocked", "contingent", "unreadable"]
    assert doc["schema"] == "hoh-mission/1" and doc["view"] == "board"
    assert doc["project"] == "p" and doc["root"] == str(tmp_path.resolve())
    assert doc["write_seq"] == 1
    assert doc["totals"] == {"nodes": 19, "running": 1, "ready": 2, "queued": 8, "blocked": 5,
                             "contingent": 1, "merged": 1, "abandoned": 1, "unreadable": 0}
    assert doc["running"] == [{"node_id": "RU", "run_id": "run-ru"}]
    assert doc["ready"] == [{"node_id": "RD"}, {"node_id": "RD2"}]
    assert doc["contingent"] == [{"node_id": "CO"}]
    assert doc["unreadable"] == {}
    assert doc["queued"] == [
        {"node_id": "HQ", "waiting_on": ["PK"], "held_by": ["PK"], "unrunnable": False},
        {"node_id": "HQ2", "waiting_on": ["HQ"], "held_by": ["PK"], "unrunnable": False},
        {"node_id": "BR", "waiting_on": ["RU"], "held_by": [], "unrunnable": False},
        {"node_id": "EH", "waiting_on": ["EN"], "held_by": ["EN"], "unrunnable": False},
        {"node_id": "XQ", "waiting_on": ["RU"], "held_by": [], "unrunnable": False},
        {"node_id": "UD", "waiting_on": ["nope"], "held_by": [], "unrunnable": True},
        {"node_id": "CY1", "waiting_on": ["CY2"], "held_by": [], "unrunnable": True},
        {"node_id": "CY2", "waiting_on": ["CY1"], "held_by": [], "unrunnable": True},
    ]
    assert doc["blocked"] == [
        {"node_id": "PK", "waits_for": "decision", "kind": "decision", "handling": "PARK_BRANCH",
         "retry_after": "2026-10-12T21:00:00Z"},
        {"node_id": "UL", "waits_for": "time", "kind": "usage_limit", "handling": None,
         "retry_after": "2026-10-12T20:00:00Z"},
        {"node_id": "OT", "waits_for": "other", "kind": "", "handling": None, "retry_after": None},
        {"node_id": "EN", "waits_for": "nobody", "kind": "decision", "handling": "ABORT_BRANCH",
         "retry_after": None},
        {"node_id": "XT", "waits_for": "decision", "kind": "EXTERNAL",
         "handling": "EXTERNAL_ACTION", "retry_after": None},
    ]
    placed = [i for key in ("running", "ready", "queued", "blocked", "contingent")
              for i in _ids(doc[key])]
    assert len(placed) == len(set(placed)) == 17            # each node once; two are settled
    assert not set(placed) & {"M1", "AB"}


@pytest.mark.parametrize("kind, waits_for, handling", [
    ("parked_by_contract", "decision", "UNKNOWN"),
    ("collected_for_digest", "decision", "UNKNOWN"),
    ("human", "decision", "UNKNOWN"),
    ("contract_abort", "other", None),        # only a routing makes an ended branch a decision
    ("contract_stop", "other", None),
    ("usage_limit", "time", None),
    ("storage_pressure", "time", None),
    ("rate_limit", "time", None),
    ("role_timeout", "time", None),
    ("something_new", "other", None),
    (None, "other", None),
])
def test_a_blocked_node_with_no_log_is_read_from_its_kind_alone(tmp_path, capsys, kind, waits_for,
                                                                handling):
    _project(tmp_path, [_node("B", BLOCKED, blocked_kind=kind)])
    code, doc = _doc(capsys, tmp_path, "board")
    assert code == 0 and doc["unreadable"] == {}
    (row,) = doc["blocked"]
    assert row["waits_for"] == waits_for and row["handling"] == handling, row
    assert row["kind"] == (kind or "")


def test_the_board_agrees_with_the_frontier_on_random_graphs():
    """A reference that does not share the module's code: `ProjectState.ready()` and the node's own
    fields. The board is a partition of the nodes; `ready` is `ready()` less the external actions."""
    from hoh.decisionfrontier import derive_frontier

    rng = random.Random(3435)
    kinds = [None, "human", "parked_by_contract", "usage_limit", "contract_abort", "other"]
    for trial in range(60):
        n = rng.randint(1, 9)
        ids = [f"n{i}" for i in range(n)]
        nodes = []
        for i in ids:
            deps = [d for d in ids + ["ghost"] if rng.random() < 0.18 and d != i]
            life = rng.choice([READY, READY, READY, RUNNING, BLOCKED, MERGED, ABANDONED, CONTINGENT])
            nodes.append(_node(i, life, deps, blocked_kind=rng.choice(kinds) if life is BLOCKED else None,
                               action_class=EXTERNAL if rng.random() < 0.2 else ActionClass.INTERNAL))
        state = ProjectState(project_id="p", repo_path="/x", nodes=nodes)
        frontier = derive_frontier(state)
        doc = mc().board(state, frontier)
        buckets = {key: _ids(doc[key]) for key in
                   ("running", "ready", "queued", "blocked", "contingent")}
        placed = [i for ids_ in buckets.values() for i in ids_]
        assert len(placed) == len(set(placed)), (trial, buckets)
        settled = [x.id for x in nodes if x.lifecycle in (MERGED, ABANDONED)]
        assert sorted(placed + settled) == sorted(ids), (trial, buckets)
        t = doc["totals"]
        assert t["nodes"] == n and t["merged"] + t["abandoned"] == len(settled)
        assert sum(t[k] for k in ("running", "ready", "queued", "blocked", "contingent",
                                  "merged", "abandoned")) == n
        want_ready = [x.id for x in state.ready() if x.action_class is not EXTERNAL]
        assert buckets["ready"] == want_ready, (trial, buckets)
        assert buckets["running"] == [x.id for x in nodes if x.lifecycle is RUNNING]
        reachable = {x.id for x in state.ready()}
        assert sorted(buckets["blocked"]) == sorted(
            [x.id for x in nodes if x.lifecycle is BLOCKED]
            + [x.id for x in nodes if x.lifecycle is READY and x.action_class is EXTERNAL
               and x.id in reachable]), (trial, buckets)


def test_a_blocked_node_the_frontier_does_not_place_is_a_refusal_not_a_fallback(monkeypatch):
    from hoh import decisionfrontier

    state = ProjectState(project_id="p", repo_path="/x",
                         nodes=[_node("A", BLOCKED, blocked_kind="human")])
    with pytest.raises(mc().MissionRefused):
        mc().board(state, decisionfrontier.DecisionFrontier())


# --------------------------------------------------------------------------- #
# N: needs you
# --------------------------------------------------------------------------- #

def test_needs_you_lists_the_open_decisions_most_freeing_first_with_what_each_holds(tmp_path,
                                                                                     capsys):
    ask = _routing("a release question", kind="release")
    for run_id, routing in (("r-a", _routing("licence")), ("r-b", ask), ("r-c", _routing("tag"))):
        _log(tmp_path, run_id, routing)
    _project(tmp_path, [
        _node("A", BLOCKED, run_id="r-a", blocked_kind="parked_by_contract"),
        _node("a1", deps=["A"]),                                      # frees 1
        _node("B", BLOCKED, run_id="r-b", blocked_kind="human"),
        _node("b1", deps=["B"]), _node("b2", deps=["B"]),             # frees 2
        _node("C", BLOCKED, run_id="r-c", blocked_kind="parked_by_contract"),
        _node("c1", deps=["C"]), _node("r", RUNNING), _node("c2", deps=["C", "r"]),  # frees 1, holds 2
        _node("D", BLOCKED, blocked_kind="human"),                    # no log: UNKNOWN, frees 0
    ])
    code, doc = _doc(capsys, tmp_path, "needs-you")
    assert code == 0 and doc["view"] == "needs-you"
    assert list(doc) == ["schema", "view", "root", "project", "write_seq", "totals", "decisions",
                         "ended", "unreadable"]
    assert doc["totals"] == {"open": 4, "ended": 0, "unreadable": 0}
    assert [(d["rank"], d["node_id"]) for d in doc["decisions"]] == [
        (1, "B"), (2, "C"), (3, "A"), (4, "D")]
    by = {d["node_id"]: d for d in doc["decisions"]}
    assert by["B"] == {"rank": 1, "node_id": "B", "handling": "PARK_BRANCH", "kind": "release",
                       "decision": "a release question", "why": by["B"]["why"],
                       "retry_after": None, "holds": ["b1", "b2"], "frees": ["b1", "b2"]}
    assert by["B"]["why"] and isinstance(by["B"]["why"], str)
    assert by["C"]["holds"] == ["c1", "c2"] and by["C"]["frees"] == ["c1"]
    assert by["A"]["holds"] == ["a1"] and by["A"]["frees"] == ["a1"]
    assert by["D"]["handling"] == "UNKNOWN" and by["D"]["holds"] == [] and by["D"]["frees"] == []
    assert doc["ended"] == []


def test_a_branch_the_contract_ended_is_listed_apart_and_frees_nothing(tmp_path, capsys):
    _log(tmp_path, "r-x", _abort("a licence question"))
    _log(tmp_path, "r-p", _routing())
    _project(tmp_path, [
        _node("X", BLOCKED, run_id="r-x", blocked_kind="contract_abort"), _node("x1", deps=["X"]),
        _node("P", BLOCKED, run_id="r-p", blocked_kind="parked_by_contract"),
        _node("p1", deps=["P"]),
    ])
    code, doc = _doc(capsys, tmp_path, "needs-you")
    assert code == 0
    assert [d["node_id"] for d in doc["decisions"]] == ["P"]
    assert doc["totals"] == {"open": 1, "ended": 1, "unreadable": 0}
    (ended,) = doc["ended"]
    assert ended["node_id"] == "X" and ended["handling"] == "ABORT_BRANCH"
    assert ended["holds"] == ["x1"] and "frees" not in ended and "rank" not in ended
    code, out, err = _ask(capsys, tmp_path, "needs-you", "--project", "p")
    assert code == 0 and err == ""
    assert "ENDED  X" in out and out.index("ENDED  X") > out.index("P  PARK_BRANCH")


def test_a_graph_with_nothing_open_is_an_answer_not_an_error(tmp_path, capsys):
    _project(tmp_path, [_node("A"), _node("B", deps=["A"])])
    code, doc = _doc(capsys, tmp_path, "needs-you")
    assert code == 0 and doc["decisions"] == [] and doc["ended"] == []
    assert doc["totals"] == {"open": 0, "ended": 0, "unreadable": 0}
    code, out, err = _ask(capsys, tmp_path, "needs-you", "--project", "p")
    assert code == 0 and err == "" and "open: 0  ended: 0  unreadable: 0" in out


def test_needs_you_agrees_with_the_frontier_on_random_graphs():
    from hoh.decisionfrontier import derive_frontier

    rng = random.Random(3436)
    for trial in range(60):
        n = rng.randint(1, 9)
        ids = [f"n{i}" for i in range(n)]
        nodes = [_node(i, rng.choice([READY, READY, RUNNING, BLOCKED, MERGED]),
                       [d for d in ids if rng.random() < 0.2 and d != i],
                       blocked_kind="human", action_class=EXTERNAL if rng.random() < 0.25
                       else ActionClass.INTERNAL) for i in ids]
        stops = {x.id: [_routing(f"q{x.id}")] if rng.random() < 0.4 else []
                 for x in nodes if x.lifecycle is BLOCKED}
        stops = {k: v for k, v in stops.items() if v}
        state = ProjectState(project_id="p", repo_path="/x", nodes=nodes)
        frontier = derive_frontier(state, stops=stops)
        doc = mc().needs_you(frontier)
        want = [d for d in frontier.decisions if d.resolvable]
        assert [d["node_id"] for d in doc["decisions"]] == [d.node_id for d in want], trial
        assert [d["rank"] for d in doc["decisions"]] == list(range(1, len(want) + 1))
        assert [d["frees"] for d in doc["decisions"]] == [list(d.frees) for d in want]
        assert [d["holds"] for d in doc["decisions"]] == [list(d.holds) for d in want]
        assert [d["node_id"] for d in doc["ended"]] == [
            d.node_id for d in frontier.decisions if not d.resolvable]


def test_the_words_come_through_exactly_in_json_and_as_plain_ascii_in_text(tmp_path, capsys):
    words = "ship it?\n\x1b[31mred\x1b[0m ça va ‮  😀 and a very long " + "x" * 300
    _log(tmp_path, "r-a", _routing(words))
    node_id = "né\x1b[2J" + "y" * 100
    _project(tmp_path, [_node(node_id, BLOCKED, run_id="r-a", blocked_kind="human")])
    code, doc = _doc(capsys, tmp_path, "needs-you")
    assert code == 0
    assert doc["decisions"][0]["decision"] == words and doc["decisions"][0]["node_id"] == node_id
    for view in ("needs-you", "board"):
        code, out, err = _ask(capsys, tmp_path, view, "--project", "p")
        assert code == 0 and err == ""
        assert out.isascii() and "\x1b" not in out and out.endswith("\n")
        assert all(line.isprintable() for line in out.splitlines())
    code, out, err = _ask(capsys, tmp_path, "needs-you", "--project", "p")
    lines = out.splitlines()
    assert lines[2].split("  ")[1] == ("n\\xe9\\x1b[2J" + "y" * 100)[:77] + "..."   # NAME_WIDTH 80
    decision = next(line for line in lines if line.startswith("   decision: "))
    assert len(decision) == len("   decision: ") + 120 and decision.endswith("...")  # 120 characters


def test_a_kind_and_a_list_of_ids_are_cut_in_the_text_views(tmp_path, capsys):
    kind = "k" * 100
    deps = [f"dependency-number-{i:02d}" for i in range(12)]
    _project(tmp_path, [_node("B", BLOCKED, blocked_kind=kind), _node("Q", deps=["B", *deps]),
                        _node("R1", RUNNING, run_id="r" * 100), _node("R2", RUNNING)])
    code, out, err = _ask(capsys, tmp_path, "board", "--project", "p")
    assert code == 0 and err == ""
    assert "RUNNING  R1  run=" + "r" * 77 + "...\n" in out + "\n"             # NAME_WIDTH
    assert "RUNNING  R2  run=-\n" in out + "\n"
    blocked = next(line for line in out.splitlines() if line.startswith("BLOCKED  B"))
    assert blocked.endswith("  " + "k" * 37 + "...")                       # KIND_WIDTH 40
    queued = next(line for line in out.splitlines() if line.startswith("QUEUED  Q"))
    waiting = queued.split("waiting_on=")[1].split("  held_by=")[0]
    assert len(waiting) == 120 and waiting.endswith("...")                 # TEXT_REASON_WIDTH


# --------------------------------------------------------------------------- #
# R: what is read, and what is refused
# --------------------------------------------------------------------------- #

def _good_text(root: Path) -> str:
    _project(root, [_node("A")], project_id="seed")
    return (root / "projects" / "seed" / "project.json").read_text(encoding="utf-8")


def _plant(root: Path, how: str) -> None:
    """A `projects/p` that cannot be read whole, in the way `how` names."""
    good = _good_text(root)
    pdir = root / "projects" / "p"
    pdir.mkdir(parents=True)
    path = pdir / "project.json"
    outside = root.parent / (root.name + "-elsewhere")
    outside.mkdir(exist_ok=True)
    record = json.loads(good)
    record["project_id"] = "p"
    ok = json.dumps(record, indent=2)
    if how == "no project directory":
        pdir.rmdir()
    elif how == "no project.json":
        pass
    elif how == "project.json is a directory":
        path.mkdir()
    elif how == "project.json is a dangling link":
        os.symlink(outside / "gone.json", path)
    elif how == "project.json is a link to a good file":
        (outside / "good.json").write_text(ok, encoding="utf-8")
        os.symlink(outside / "good.json", path)
    elif how == "project.json is a FIFO":
        os.mkfifo(path)
    elif how == "project.json is mode 000":
        path.write_text(ok, encoding="utf-8")
        path.chmod(0)
    elif how == "the project directory is mode 000":
        path.write_text(ok, encoding="utf-8")
        pdir.chmod(0)
    elif how == "the project directory is a link":
        pdir.rmdir()
        (outside / "projects-p").mkdir()
        (outside / "projects-p" / "project.json").write_text(ok, encoding="utf-8")
        os.symlink(outside / "projects-p", pdir)
    elif how == "the projects directory is a link":
        pdir.rmdir()
        (root / "projects").rmdir() if not any((root / "projects").iterdir()) else None
        moved = outside / "projects-dir"
        (root / "projects").rename(moved)
        (moved / "p").mkdir()
        (moved / "p" / "project.json").write_text(ok, encoding="utf-8")
        os.symlink(moved, root / "projects")
    elif how == "larger than eight mebibytes":
        path.write_text(ok + " " * (EIGHT_MIB + 1 - len(ok)), encoding="utf-8")
    elif how == "Latin-1 bytes":
        path.write_bytes(ok.replace('"/x"', '"/ç"').encode("latin-1"))
    elif how == "UTF-16 with a BOM":
        path.write_bytes(ok.encode("utf-16"))
    elif how == "a UTF-8 BOM":
        path.write_bytes(b"\xef\xbb\xbf" + ok.encode())
    elif how == "a NUL byte":
        path.write_bytes(ok.encode() + b"\x00")
    elif how == "a torn file":
        path.write_text(ok[: len(ok) // 2], encoding="utf-8")
    elif how == "a JSON list":
        path.write_text("[1, 2]", encoding="utf-8")
    elif how == "JSON null":
        path.write_text("null", encoding="utf-8")
    elif how == "a deeply nested array":
        path.write_text("[" * 200000 + "]" * 200000, encoding="utf-8")
    elif how == "an unknown key":
        path.write_text(json.dumps({**record, "surprise": 1}), encoding="utf-8")
    elif how == "no schema version":
        path.write_text(json.dumps({k: v for k, v in record.items() if k != "schema_version"}),
                        encoding="utf-8")
    elif how == "an older schema version":
        path.write_text(json.dumps({**record, "schema_version": 1}), encoding="utf-8")
    elif how == "a newer schema version":
        path.write_text(json.dumps({**record, "schema_version": 99}), encoding="utf-8")
    elif how == "another project's state":
        path.write_text(json.dumps({**record, "project_id": "someone-else"}), encoding="utf-8")
    elif how == "two nodes with one id":
        path.write_text(json.dumps({**record, "nodes": [record["nodes"][0], record["nodes"][0]]}),
                        encoding="utf-8")
    else:
        raise AssertionError(how)


REFUSED_PROJECTS = [
    "no project directory", "no project.json", "project.json is a directory",
    "project.json is a dangling link", "project.json is a link to a good file",
    "project.json is a FIFO", "project.json is mode 000", "the project directory is mode 000",
    "the project directory is a link",
    "the projects directory is a link", "larger than eight mebibytes", "Latin-1 bytes",
    "UTF-16 with a BOM", "a UTF-8 BOM", "a NUL byte", "a torn file", "a JSON list", "JSON null",
    "a deeply nested array", "an unknown key", "no schema version", "an older schema version",
    "a newer schema version", "another project's state", "two nodes with one id",
]


@pytest.mark.parametrize("how", REFUSED_PROJECTS)
@pytest.mark.parametrize("view", ["board", "needs-you"])
def test_a_project_that_cannot_be_read_whole_is_refused_and_nothing_is_printed(tmp_path, capsys,
                                                                               how, view):
    if how in ("project.json is mode 000", "the project directory is mode 000") and IS_ROOT:
        pytest.skip("root reads a mode-000 file")
    root = tmp_path / "root"
    root.mkdir()
    _plant(root, how)
    try:
        with Guard():
            code, out, err = _ask(capsys, root, view, "--project", "p")
    finally:
        (root / "projects" / "p").chmod(0o755) if (root / "projects" / "p").is_dir() else None
    assert code == 2, (how, out, err)
    assert out == ""
    assert err.startswith("mission refused: project p: ") and err.count("\n") == 1
    assert err.isascii() and len(err) < 400, err


def test_the_project_file_ceiling_is_eight_mebibytes_exactly(tmp_path, capsys):
    good = _good_text(tmp_path)
    record = json.loads(good)
    record["project_id"] = "p"
    ok = json.dumps(record)
    pdir = tmp_path / "projects" / "p"
    pdir.mkdir()
    (pdir / "project.json").write_text(ok + " " * (EIGHT_MIB - len(ok)), encoding="utf-8")
    assert (pdir / "project.json").stat().st_size == EIGHT_MIB
    code, doc = _doc(capsys, tmp_path, "board")
    assert code == 0 and doc["totals"]["nodes"] == 1
    (pdir / "project.json").write_text(ok + " " * (EIGHT_MIB + 1 - len(ok)), encoding="utf-8")
    code, out, err = _ask(capsys, tmp_path, "board", "--project", "p")
    assert code == 2 and out == "" and "larger than 8388608 bytes" in err


def test_the_node_ceiling_is_two_thousand_exactly(tmp_path, capsys):
    chain = [_node(f"n{i}", deps=[f"n{i - 1}"] if i else []) for i in range(2000)]
    _project(tmp_path, chain)
    code, doc = _doc(capsys, tmp_path, "board")
    assert code == 0 and doc["totals"]["nodes"] == 2000 and doc["totals"]["queued"] == 1999
    _project(tmp_path, chain + [_node("n2000", deps=["n1999"])], project_id="q")
    code, out, err = _ask(capsys, tmp_path, "board", "--project", "q")
    assert code == 2 and out == "" and "has 2001 nodes, more than the 2000" in err


@pytest.mark.parametrize("project", ["../p", "a/b", ".", "..", "p\n", "-x", "x" * 65, "p q",
                                     "ç", ""])
def test_a_project_id_that_is_not_a_name_is_refused_and_nothing_outside_is_read(
        tmp_path, capsys, project):
    root = tmp_path / "root"
    root.mkdir()
    _project(tmp_path, [_node("A")], project_id="p")          # a project beside the root, not in it
    _project(root, [_node("A")], project_id="q")
    before = _tree(tmp_path)
    code, out, err = _ask(capsys, root, "board", f"--project={project}")
    assert code == 2 and out == "" and err.startswith("mission refused: project ")
    assert _tree(tmp_path) == before


def test_a_project_under_the_given_root_is_read_and_the_root_may_be_a_link(tmp_path, capsys):
    real = tmp_path / "real"
    real.mkdir()
    _project(real, [_node("A"), _node("B", deps=["A"])])
    link = tmp_path / "link"
    os.symlink(real, link)
    code, doc = _doc(capsys, link, "board")
    assert code == 0 and doc["root"] == str(real.resolve()) and _ids(doc["ready"]) == ["A"]
    code, out, err = _ask(capsys, tmp_path / "absent", "board", "--project", "p")
    assert code == 2 and out == "" and "mission refused" in err
    _project(real, [_node("A")], project_id="q")
    (real / "projects" / "q" / "project.json").rename(real / "projects" / "q" / "elsewhere")
    code, out, err = _ask(capsys, real, "board", "--project", "q")
    assert code == 2 and "no project.json" in err


# --------------------------------------------------------------------------- #
# L: the routing logs
# --------------------------------------------------------------------------- #

def _plant_log(root: Path, how: str) -> None:
    """The log of run `r-a` in the way `how` names; `A` is BLOCKED on it."""
    run = root / "r-a"
    run.mkdir()
    outside = root.parent / (root.name + "-elsewhere")
    outside.mkdir(exist_ok=True)
    good = _line(_routing()) + "\n"
    if how == "a torn fragment":
        _log_path(root, "r-a").write_text(good + good[:40] + "\n" + good, encoding="utf-8")
    elif how == "not JSON":
        _log_path(root, "r-a").write_text("{nope}\n", encoding="utf-8")
    elif how == "Latin-1 bytes":
        _log_path(root, "r-a").write_bytes(good.replace("licence", "licençe").encode("latin-1"))
    elif how == "an unknown handling":
        _log_path(root, "r-a").write_text(_line(_routing(), handling="WAVED_THROUGH") + "\n",
                                          encoding="utf-8")
    elif how == "a fabricated approval":
        _log_path(root, "r-a").write_text(
            _line(_routing(), handling="RESOLVED_BY_POLICY", delegable=False) + "\n",
            encoding="utf-8")
    elif how == "a very long value":
        _log_path(root, "r-a").write_text(_line(_routing(), handling="Z" * 1000) + "\n",
                                          encoding="utf-8")
    elif how == "larger than eight mebibytes":
        _log_path(root, "r-a").write_bytes(b" " * (EIGHT_MIB + 1))
    elif how == "a FIFO":
        os.mkfifo(_log_path(root, "r-a"))
    elif how == "a directory":
        _log_path(root, "r-a").mkdir()
    elif how == "a link to a good log":
        (outside / "good.jsonl").write_text(good, encoding="utf-8")
        os.symlink(outside / "good.jsonl", _log_path(root, "r-a"))
    elif how == "autonomy is a link":
        (outside / "autonomy").mkdir()
        (outside / "autonomy" / "routings.jsonl").write_text(good, encoding="utf-8")
        os.symlink(outside / "autonomy", run / "autonomy")
    elif how == "the run directory is a link":
        run.rmdir()
        (outside / "r-a" / "autonomy").mkdir(parents=True)
        (outside / "r-a" / "autonomy" / "routings.jsonl").write_text(good, encoding="utf-8")
        os.symlink(outside / "r-a", run)
    elif how == "mode 000":
        _log_path(root, "r-a").write_text(good, encoding="utf-8")
        _log_path(root, "r-a").chmod(0)
    else:
        raise AssertionError(how)


UNREADABLE_LOGS = ["a torn fragment", "not JSON", "Latin-1 bytes", "an unknown handling",
                   "a fabricated approval", "a very long value", "larger than eight mebibytes",
                   "a FIFO", "a directory", "a link to a good log", "autonomy is a link",
                   "the run directory is a link", "mode 000"]


@pytest.mark.parametrize("how", UNREADABLE_LOGS)
def test_a_routing_log_that_cannot_be_read_holds_its_node_and_is_listed(tmp_path, capsys, how):
    if how == "mode 000" and IS_ROOT:
        pytest.skip("root reads a mode-000 file")
    root = tmp_path / "root"
    root.mkdir()
    _plant_log(root, how)
    _project(root, [_node("A", BLOCKED, run_id="r-a", blocked_kind="parked_by_contract"),
                    _node("B", deps=["A"]), _node("C")])
    code, doc = _doc(capsys, root, "needs-you")
    assert code == 1, how
    assert list(doc["unreadable"]) == ["A"] and doc["totals"]["unreadable"] == 1
    (why,) = doc["unreadable"].values()
    assert why and why.isascii() and len(why) <= 240
    (row,) = doc["decisions"]
    assert row["node_id"] == "A" and row["handling"] == "UNREADABLE"
    assert row["holds"] == ["B"] and "cannot be read" in row["why"]
    code, board = _doc(capsys, root, "board")
    assert code == 1 and list(board["unreadable"]) == ["A"]
    assert _ids(board["blocked"]) == ["A"] and _ids(board["ready"]) == ["C"]
    assert board["queued"][0]["node_id"] == "B" and board["queued"][0]["held_by"] == ["A"]
    code, out, err = _ask(capsys, root, "needs-you", "--project", "p")
    assert code == 1 and err == "" and "UNREADABLE  A  " in out
    assert "unreadable: 1" in out.splitlines()[1]
    code, out, err = _ask(capsys, root, "board", "--project", "p")
    assert code == 1 and "  unreadable=1" in out.splitlines()[1]


@pytest.mark.parametrize("run_id", ["..", "../x", "/etc", "", "a/b", "_hidden", "projects",
                                    "backups", ".x"])
def test_a_run_id_that_is_not_a_run_directory_name_is_listed_as_unreadable(tmp_path, capsys,
                                                                           run_id):
    _project(tmp_path, [_node("A", BLOCKED, run_id=run_id, blocked_kind="human")])
    code, doc = _doc(capsys, tmp_path, "needs-you")
    assert code == 1 and list(doc["unreadable"]) == ["A"]
    assert doc["decisions"][0]["handling"] == "UNREADABLE"


def test_a_timed_wait_wins_over_an_unreadable_log_and_the_log_is_still_listed(tmp_path, capsys):
    _plant_log(tmp_path, "a torn fragment")
    _project(tmp_path, [_node("A", BLOCKED, run_id="r-a", blocked_kind="usage_limit",
                              retry_after="2026-10-12T20:00:00Z")])
    code, doc = _doc(capsys, tmp_path, "board")
    assert code == 1 and list(doc["unreadable"]) == ["A"]
    (row,) = doc["blocked"]
    assert row["waits_for"] == "time" and row["handling"] is None
    code, doc = _doc(capsys, tmp_path, "needs-you")
    assert code == 1 and doc["decisions"] == [] and list(doc["unreadable"]) == ["A"]


def test_a_node_that_names_no_run_or_a_run_with_no_log_is_read_from_its_kind(tmp_path, capsys):
    (tmp_path / "r-empty").mkdir()
    (tmp_path / "r-quiet" / "autonomy").mkdir(parents=True)
    _project(tmp_path, [_node("A", BLOCKED, blocked_kind="human"),
                        _node("B", BLOCKED, run_id="r-empty", blocked_kind="human"),
                        _node("C", BLOCKED, run_id="r-quiet", blocked_kind="human")])
    code, doc = _doc(capsys, tmp_path, "needs-you")
    assert code == 0 and doc["unreadable"] == {}
    assert [(d["node_id"], d["handling"]) for d in doc["decisions"]] == [
        ("A", "UNKNOWN"), ("B", "UNKNOWN"), ("C", "UNKNOWN")]


def test_a_run_directory_that_is_absent_is_a_log_that_cannot_be_read(tmp_path, capsys):
    """The node names a run; where that run's directory is not, its log is not "empty" but
    unknown (a run moved away, a run id mistyped): held as `UNREADABLE`, listed, exit 1 -- also
    when the node's kind says nothing (`blocked_kind` None)."""
    _project(tmp_path, [_node("A", BLOCKED, run_id="r-gone", blocked_kind="human"),
                        _node("N", BLOCKED, run_id="r-gone-too"), _node("B", deps=["N"])])
    code, doc = _doc(capsys, tmp_path, "needs-you")
    assert code == 1 and list(doc["unreadable"]) == ["A", "N"]
    assert "run directory does not exist" in doc["unreadable"]["A"]
    assert [(d["node_id"], d["handling"]) for d in doc["decisions"]] == [
        ("N", "UNREADABLE"), ("A", "UNREADABLE")]
    assert doc["decisions"][0]["holds"] == ["B"]
    assert not (tmp_path / "r-gone").exists()               # reading a log creates nothing


def test_nodes_that_share_a_run_read_its_log_once(tmp_path, capsys, monkeypatch):
    _log(tmp_path, "r-a", _routing("licence"))
    _log(tmp_path, "r-b", _routing("tag"))
    _project(tmp_path, [_node(f"A{i}", BLOCKED, run_id="r-a", blocked_kind="human")
                        for i in range(5)] + [_node("B", BLOCKED, run_id="r-b",
                                                    blocked_kind="human")])
    real, reads = mc().read_branch_stops, []

    def counting(run_dir):
        reads.append(Path(run_dir).name)
        return real(run_dir)

    monkeypatch.setattr(mc(), "read_branch_stops", counting)
    code, doc = _doc(capsys, tmp_path, "needs-you")
    assert code == 0 and sorted(reads) == ["r-a", "r-b"], reads
    assert [d["decision"] for d in doc["decisions"]] == ["licence"] * 5 + ["tag"]


def test_only_the_logs_of_blocked_nodes_are_read(tmp_path, capsys):
    """A MERGED node's run may hold anything; the view must not open it (a FIFO would hang)."""
    for run_id in ("r-m", "r-r", "r-k"):
        (tmp_path / run_id / "autonomy").mkdir(parents=True)
        os.mkfifo(tmp_path / run_id / "autonomy" / "routings.jsonl")
    _project(tmp_path, [_node("M", MERGED, run_id="r-m"), _node("R", RUNNING, run_id="r-r"),
                        _node("K", run_id="r-k")])
    code, doc = _doc(capsys, tmp_path, "board")
    assert code == 0 and doc["unreadable"] == {}
    assert _ids(doc["running"]) == ["R"] and _ids(doc["ready"]) == ["K"]


def test_a_reason_is_cut_at_240_characters_in_json_and_120_in_text(tmp_path, capsys):
    _plant_log(tmp_path, "a very long value")
    _project(tmp_path, [_node("A", BLOCKED, run_id="r-a", blocked_kind="human")])
    code, doc = _doc(capsys, tmp_path, "board")
    why = doc["unreadable"]["A"]
    assert code == 1 and len(why) == 240 and why.endswith("...")
    code, out, err = _ask(capsys, tmp_path, "board", "--project", "p")
    line = next(x for x in out.splitlines() if x.startswith("UNREADABLE  A  "))
    assert len(line) == len("UNREADABLE  A  ") + 120 and line.endswith("...")


def test_the_product_writer_read_back_end_to_end(tmp_path, capsys):
    """A branch parked through `block_under_contract` (the product's path), read by the command."""
    run = tmp_path / "r-a"
    run.mkdir()
    governor = bind(run, contract=compile_profile(
        AutonomyMode.UNATTENDED_LONG_HORIZON, contract_id="un", **UNATTENDED))
    state = RunState(run_id="r-a", repo_path="/x", project_name="p", spec_path="/x/s.md",
                     spec_digest="a" * 16, policy_digest="b" * 16, profile_digest="c" * 16)
    block_under_contract(state, "the developer needs folder trust", governor)
    assert state.blocked_kind == "parked_by_contract"
    _project(tmp_path, [_node("A", BLOCKED, run_id="r-a", blocked_kind=None,
                              note="run blocked: the developer needs folder trust"),
                        _node("B", deps=["A"]), _node("C")])
    code, doc = _doc(capsys, tmp_path, "needs-you")
    assert code == 0
    (row,) = doc["decisions"]
    assert row["node_id"] == "A" and row["handling"] == "PARK_BRANCH"
    assert row["decision"] == "the developer needs folder trust" and row["holds"] == ["B"]
    code, board = _doc(capsys, tmp_path, "board")
    assert _ids(board["ready"]) == ["C"] and _ids(board["queued"]) == ["B"]
    assert board["blocked"][0]["waits_for"] == "decision"


def test_a_queued_node_names_its_holders_in_graph_order_not_in_rank_order(tmp_path, capsys):
    """A and B are open decisions; B frees more and so ranks first. C stands behind both: its
    `held_by` is `["A", "B"]` (the order of the graph), `needs-you` ranks `["B", "A"]`."""
    _project(tmp_path, [
        _node("A", BLOCKED, blocked_kind="human"), _node("B", BLOCKED, blocked_kind="human"),
        _node("b1", deps=["B"]), _node("b2", deps=["B"]), _node("C", deps=["A", "B"])])
    code, board = _doc(capsys, tmp_path, "board")
    assert code == 0
    assert [(q["node_id"], q["waiting_on"], q["held_by"]) for q in board["queued"]] == [
        ("b1", ["B"], ["B"]), ("b2", ["B"], ["B"]), ("C", ["A", "B"], ["A", "B"])]
    code, need = _doc(capsys, tmp_path, "needs-you")
    assert [d["node_id"] for d in need["decisions"]] == ["B", "A"]


def test_the_board_text_is_exactly_these_lines(tmp_path, capsys):
    _rich(tmp_path)
    code, out, err = _ask(capsys, tmp_path, "board", "--project", "p")
    assert code == 0 and err == ""
    assert out.splitlines() == [
        f"mission board  project p  root {tmp_path.resolve()}",
        "totals: nodes=19  running=1  ready=2  queued=8  blocked=5  contingent=1  merged=1  "
        "abandoned=1  unreadable=0",
        "RUNNING  RU  run=run-ru",
        "READY  RD",
        "READY  RD2",
        "QUEUED  HQ  waiting_on=PK  held_by=PK",
        "QUEUED  HQ2  waiting_on=HQ  held_by=PK",
        "QUEUED  BR  waiting_on=RU  held_by=-",
        "QUEUED  EH  waiting_on=EN  held_by=EN",
        "QUEUED  XQ  waiting_on=RU  held_by=-",
        "QUEUED  UD  waiting_on=nope  held_by=-  UNRUNNABLE",
        "QUEUED  CY1  waiting_on=CY2  held_by=-  UNRUNNABLE",
        "QUEUED  CY2  waiting_on=CY1  held_by=-  UNRUNNABLE",
        "BLOCKED  PK  waits_for=decision  decision  PARK_BRANCH  retry_after=2026-10-12T21:00:00Z",
        "BLOCKED  UL  waits_for=time  usage_limit  retry_after=2026-10-12T20:00:00Z",
        "BLOCKED  OT  waits_for=other",
        "BLOCKED  EN  waits_for=nobody  decision  ABORT_BRANCH",
        "BLOCKED  XT  waits_for=decision  EXTERNAL  EXTERNAL_ACTION",
        "CONTINGENT  CO",
    ]


def test_the_needs_you_text_and_rows_are_exactly_these(tmp_path, capsys):
    _log_path(tmp_path, "r-p").write_text(
        _line(_routing("licence"), why="a person must answer") + "\n", encoding="utf-8")
    _log_path(tmp_path, "r-x").write_text(
        _line(_abort("a tag question"), why="the contract ends it") + "\n", encoding="utf-8")
    _project(tmp_path, [
        _node("P", BLOCKED, run_id="r-p", blocked_kind="parked_by_contract",
              retry_after="2026-10-12T21:00:00Z"),
        _node("p1", deps=["P"]),
        _node("X", BLOCKED, run_id="r-x", blocked_kind="contract_abort"), _node("x1", deps=["X"]),
        _node("U", BLOCKED, blocked_kind="human"),
    ])
    code, out, err = _ask(capsys, tmp_path, "needs-you", "--project", "p")
    assert code == 0 and err == ""
    assert out.splitlines() == [
        f"mission needs-you  project p  root {tmp_path.resolve()}",
        "open: 2  ended: 1  unreadable: 0",
        "1  P  PARK_BRANCH  decision  frees=p1  holds=p1",
        "   decision: licence",
        "   why: a person must answer",
        "2  U  UNKNOWN  human  frees=-  holds=-",
        "   decision: -",
        "   why: blocked_kind='human' and no stopping routing was supplied",
        "ENDED  X  ABORT_BRANCH  decision  holds=x1",
        "   why: the contract ends it",
    ]
    code, doc = _doc(capsys, tmp_path, "needs-you")
    assert [d["retry_after"] for d in doc["decisions"]] == ["2026-10-12T21:00:00Z", None]
    assert doc["ended"][0]["retry_after"] is None and doc["ended"][0]["why"] == "the contract ends it"


def test_the_dependency_ceiling_is_twenty_thousand_entries_exactly(tmp_path, capsys):
    ids = [f"n{i}" for i in range(2000)]
    nodes = [_node(i, deps=ids[:10]) if k >= 10 else _node(i) for k, i in enumerate(ids)]
    assert sum(len(n.dependencies) for n in nodes) == 19900
    nodes[10].dependencies = ids[:10] + ["n0"] * 100                 # 20 000 in all, repeats count
    assert sum(len(n.dependencies) for n in nodes) == 20000
    _project(tmp_path, nodes)
    code, doc = _doc(capsys, tmp_path, "board")
    assert code == 0 and doc["totals"]["nodes"] == 2000
    nodes[10].dependencies = nodes[10].dependencies + ["n1"]
    _project(tmp_path, nodes, project_id="q")
    code, out, err = _ask(capsys, tmp_path, "board", "--project", "q")
    assert code == 2 and out == "" and "has 20001 dependency entries, more than the 20000" in err


def test_the_ceilings_are_counted_before_the_nodes_are_validated(tmp_path, capsys):
    """2001 nodes, one of them not a node at all: the refusal is the ceiling's -- the cost of
    building the model for a graph of any size is never paid."""
    pdir = tmp_path / "projects" / "p"
    pdir.mkdir(parents=True)
    record = json.loads(ProjectState(project_id="p", repo_path="/x").model_dump_json())
    record["nodes"] = [{"id": f"n{i}"} for i in range(2000)] + [{"surprise": 1}]
    (pdir / "project.json").write_text(json.dumps(record), encoding="utf-8")
    code, out, err = _ask(capsys, tmp_path, "board", "--project", "p")
    assert code == 2 and out == "" and "has 2001 nodes, more than the 2000" in err
    record["nodes"] = [{"id": "a", "dependencies": ["x"] * 20001}]
    (pdir / "project.json").write_text(json.dumps(record), encoding="utf-8")
    code, out, err = _ask(capsys, tmp_path, "board", "--project", "p")
    assert code == 2 and "has 20001 dependency entries" in err


def test_the_read_stops_one_byte_past_the_file_ceiling(tmp_path, capsys, monkeypatch):
    """The size is checked on the descriptor before the read; the read itself is bounded too,
    for a file that grows in between (here: an opener that does not check the size)."""
    pdir = tmp_path / "projects" / "p"
    pdir.mkdir(parents=True)
    (pdir / "project.json").write_bytes(b" " * (EIGHT_MIB + 3 * 1024 * 1024))
    monkeypatch.setattr(
        mc(), "_open_regular", lambda path, limit: os.open(path, os.O_RDONLY | os.O_NONBLOCK))
    real_read, taken = os.read, []

    def counting(fd, size):
        data = real_read(fd, size)
        taken.append(len(data))
        return data

    monkeypatch.setattr(os, "read", counting)
    code, out, err = _ask(capsys, tmp_path, "board", "--project", "p")
    assert code == 2 and "larger than 8388608 bytes" in err
    assert sum(taken) == EIGHT_MIB + 1, sum(taken)


# --------------------------------------------------------------------------- #
# W: nothing is written; the module's own source
# --------------------------------------------------------------------------- #

def test_the_project_views_write_nothing_not_even_for_what_they_refuse(tmp_path, capsys):
    _rich(tmp_path)
    store = ProjectStore(tmp_path, "p")
    store.write_state(store.read_state())                       # a parked predecessor exists
    older = tmp_path / "projects" / "old"
    older.mkdir()
    (older / "project.json").write_text(
        json.dumps({**json.loads((tmp_path / "projects" / "p" / "project.json").read_text()),
                    "project_id": "old", "schema_version": 1}), encoding="utf-8")
    before = _tree(tmp_path)
    for project, want in (("p", 0), ("old", 2), ("absent", 2)):
        for view in ("board", "needs-you"):
            for extra in ((), ("--json",)):
                with Guard():
                    code, out, err = _ask(capsys, tmp_path, view, "--project", project, *extra)
                assert code == want, (project, view, extra, out, err)   # the command did answer
                assert bool(out) == (want == 0) and err.startswith("mission refused") == (want == 2)
    assert _tree(tmp_path) == before
    assert not (tmp_path / "projects" / "p" / ".lock").exists()


EXPECTED_IMPORTS = [
    "from . import doubledown, migrations, runwait",
    "from .decisionfrontier import DecisionFrontier, FrontierError, TIMED_KINDS, derive_frontier",
    "from .evidence import EvidenceBundle",
    "from .governance import read_branch_stops",
    "from .project import Lifecycle, ProjectState",
    "from .projectstore import ProjectStore",
    "from .store import RESERVED_PREFIXES, RESERVED_RUN_IDS, RunStore, StoreError",
    "from __future__ import annotations",
    "from pathlib import Path",
    "from typing import Any, TextIO",
    "import dataclasses", "import errno", "import json", "import os", "import stat", "import sys",
]
#: What the module may touch on the two modules it imports as a whole.
ATTRIBUTES_OF = {"migrations": {"require_readable", "STATE_REGISTRIES", "UNPARSEABLE",
                                "MigrationError"},
                 "runwait": {"observe_run", "phase_of"},
                 # P3-42: the double-down's read model, and nothing else of that module
                 "doubledown": {"decisions_for_project"}}
WRITERS = {"unblock", "abandon", "record_external_action", "record_override",
           "block_under_contract", "bind", "decide", "create", "write_state", "read_state", "_park",
           "parked_states", "lock", "ensure", "exists", "list_projects", "Governor",
           "durable_create", "durable_write"}


def _surface_problems(source: str) -> list[str]:
    """What a module source imports and names beyond what the project views need: its import
    list is exactly `EXPECTED_IMPORTS` (by spelling: level, module, names, aliases), the
    attributes of the two modules it imports whole are the listed ones, and no writer of the
    project or governance store is named anywhere (a name, an attribute or an alias)."""
    tree = ast.parse(source)
    problems, imports, used = [], [], {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = ", ".join(sorted(a.name + (f" as {a.asname}" if a.asname else "")
                                     for a in node.names))
            imports.append(f"from {'.' * node.level}{node.module or ''} import {names}")
        elif isinstance(node, ast.Import):
            imports.extend(f"import {a.name}" + (f" as {a.asname}" if a.asname else "")
                           for a in node.names)
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                used.setdefault(node.value.id, set()).add(node.attr)
            if node.attr in WRITERS:
                problems.append(f"attribute {node.attr}")
        elif isinstance(node, ast.Name) and node.id in WRITERS:
            problems.append(f"name {node.id}")
        elif isinstance(node, ast.alias) and node.name in WRITERS:
            problems.append(f"imported {node.name}")
    want = sorted(EXPECTED_IMPORTS)
    if sorted(imports) != want:
        problems.append(f"imports differ: +{sorted(set(imports) - set(want))} "
                        f"-{sorted(set(want) - set(imports))}")
    for module, allowed in ATTRIBUTES_OF.items():
        if used.get(module, set()) - allowed:
            problems.append(f"{module}.{sorted(used[module] - allowed)}")
    return sorted(set(problems))


def test_the_module_reads_projects_and_logs_through_nothing_that_writes():
    """The module's import list is exact, and no writer of the stores is named in it."""
    assert _surface_problems(Path(mc().__file__).read_text(encoding="utf-8")) == []


GUARD_CASES = {
    "from . import projectstore; unblock": (
        "from . import projectstore\n\n\ndef _m(b, p, n):\n"
        "    projectstore.unblock(ProjectStore(b, p), n, 'x')\n"),
    "from . import governance; record_override": (
        "from . import governance\n\n\ndef _m(g, r):\n"
        "    governance.record_override(g, routing=r, by='x', what='y')\n"),
    "from .projectstore import unblock": (
        "from .projectstore import unblock\n\n\ndef _m(b, p, n):\n"
        "    unblock(ProjectStore(b, p), n, 'x')\n"),
    "from .projectstore import abandon": "from .projectstore import abandon\n",
    "from .projectstore import record_external_action":
        "from .projectstore import record_external_action\n",
    "from .governance import block_under_contract":
        "from .governance import block_under_contract\n",
    "from .governance import bind": "from .governance import bind\n",
    "import hoh.projectstore as ps": (
        "import hoh.projectstore as ps\n\n\ndef _m(b, p, n):\n"
        "    ps.unblock(ps.ProjectStore(b, p), n, 'x')\n"),
    "import hoh.governance": "import hoh.governance\n",
    "from .projectstore import ProjectStore as P": "from .projectstore import ProjectStore as P\n",
    "from ..projectstore import ProjectStore": "from ..projectstore import ProjectStore\n",
    "ProjectStore(...).create": "def _m(b, p, s):\n    ProjectStore(b, p).create(s)\n",
    "ProjectStore(...).write_state": "def _m(b, p, s):\n    ProjectStore(b, p).write_state(s)\n",
    "ProjectStore(...).lock": "def _m(b, p):\n    ProjectStore(b, p).lock()\n",
    "a second import of ProjectStore": "from .projectstore import ProjectStore as PS\n",
}


def _p33_guard():
    """The module-source guard of `tests/test_missioncontrol.py`, loaded from that file."""
    path = Path(__file__).with_name("test_missioncontrol.py")
    spec = importlib.util.spec_from_file_location("p33_missioncontrol_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("case", sorted(GUARD_CASES))
def test_the_module_source_guards_refuse_every_spelling_that_reaches_a_writer(
        case, tmp_path, monkeypatch):
    """The two guards are run, unchanged, against the real module plus ONE appended spelling
    (parsed, never imported or executed). The amendment of `tests/test_missioncontrol.py` may
    narrow what the module imports, never widen it: each spelling must be refused by that
    file's own guard *and* by `_surface_problems`."""
    mutated = tmp_path / "missioncontrol_mutated.py"
    source = Path(mc().__file__).read_text(encoding="utf-8") + "\n\n" + GUARD_CASES[case]
    mutated.write_text(source, encoding="utf-8")
    assert _surface_problems(source), "the new file's guard accepted it"
    p33 = _p33_guard()
    monkeypatch.setattr(p33, "mc", lambda: types.SimpleNamespace(__file__=str(mutated)))
    with pytest.raises(AssertionError):
        p33.test_the_module_calls_nothing_that_writes()


@pytest.mark.parametrize("spelling", [
    "def _m(r):\n    doubledown.double_down_pass(r)\n",
    "def _m(r, a, h, c, n):\n    doubledown.advance(r, a, h, c, n)\n",
    "def _m(d):\n    doubledown._create(d, {})\n",
])
def test_the_module_may_name_only_the_read_model_of_the_double_down(spelling):
    """P3-42: `doubledown` is imported whole, and `decisions_for_project` is the one name of it
    the module may use -- the pass and the dossier writers are refused by name."""
    real = Path(mc().__file__).read_text(encoding="utf-8")
    assert _surface_problems(real) == []
    problems = _surface_problems(real + "\n\n" + spelling)
    assert any(p.startswith("doubledown.") for p in problems), problems


def test_the_real_module_passes_the_guard_of_the_other_file():
    p33 = _p33_guard()
    p33.test_the_module_calls_nothing_that_writes()          # unmutated: accepted


# --------------------------------------------------------------------------- #
# C: the command
# --------------------------------------------------------------------------- #

def test_the_project_views_need_a_project_and_the_overview_takes_none(tmp_path, capsys):
    _project(tmp_path, [_node("A")])
    for argv in (("board",), ("needs-you", "--json")):
        code, out, err = _ask(capsys, tmp_path, *argv)
        assert code == 2 and out == "" and "needs a project (--project ID)" in err, argv
    code, out, err = _ask(capsys, tmp_path, "overview", "--project", "p")
    assert code == 2 and out == "" and "takes no project" in err
    code, out, err = _ask(capsys, tmp_path, "overview", "--json")
    assert code == 0 and json.loads(out)["view"] == "overview"       # unchanged
    for argv in (["mission", "frontier"], ["mission", "board", "extra"], ["mission", "board", "-p", "x"]):
        with pytest.raises(SystemExit) as stop:
            cli.main(["--root", str(tmp_path), *argv])
        assert stop.value.code == 2, argv
        capsys.readouterr()
    assert mc().VIEWS == ("overview", "board", "needs-you", "map", "timeline", "evidence")


def test_the_command_reads_the_root_and_the_project_it_is_given(tmp_path, capsys, monkeypatch):
    given, default = tmp_path / "given", tmp_path / "default"
    _project(given, [_node("only-p")], project_id="p")
    _project(given, [_node("only-q"), _node("q2", deps=["only-q"])], project_id="q")
    _project(default, [_node("from-default")], project_id="p")
    code, doc = _doc(capsys, given, "board", "q")
    assert code == 0 and doc["project"] == "q" and _ids(doc["ready"]) == ["only-q"]
    assert doc["totals"]["nodes"] == 2 and doc["root"] == str(given.resolve())
    code, doc = _doc(capsys, given, "board", "p")
    assert _ids(doc["ready"]) == ["only-p"]
    monkeypatch.setattr(cli, "DEFAULT_ROOT", default)
    assert cli.main(["mission", "board", "--project", "p", "--json"]) == 0
    assert _ids(json.loads(capsys.readouterr().out)["ready"]) == ["from-default"]


def test_an_answer_that_cannot_be_written_is_exit_2_for_the_project_views(tmp_path, monkeypatch):
    _project(tmp_path, [_node("A")])

    class Broken:
        def write(self, text):
            raise OSError("No space left on device")

        def flush(self):
            raise OSError("No space left on device")

    for view in ("board", "needs-you"):
        err = io.StringIO()
        assert mc().run(view, str(tmp_path), project="p", out=Broken(), err=err) == 2
        assert "could not be written" in err.getvalue()
        out = io.StringIO()
        assert mc().run(view, str(tmp_path), project="p", out=out, err=io.StringIO()) == 0
        assert out.getvalue().startswith(f"mission {view}  project p")
    monkeypatch.setattr(sys, "stdout", None)                   # `>&-`
    err = io.StringIO()
    assert mc().run("board", str(tmp_path), project="p", err=err) == 2
    assert "stdout is closed" in err.getvalue()
