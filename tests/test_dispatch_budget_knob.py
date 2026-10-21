"""P3-23 (and the caps of P3-22): the dispatch budget is an operator's declaration.

`hoh project budget` writes `<project>/dispatch-budget.json`; every drive binds it to the
launcher, so the QA reserve and the holds a recovery ends exist on the production path with
no code of the project's own (its driver factory below builds the launcher with **no**
budget). Hermetic: everything is under `tmp_path`; the driver factory is imported by name
through `driver.json`; only the launcher's process boundary is a fake (the harness of
`tests/test_wave_controller.py` and `tests/test_wave_settlement_wired.py`)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

import test_wave_recovery as wr
from test_wave_controller import Gates, _held_ids, _nodes, _world
from test_wave_settlement_wired import Launcher
from hoh import cli

try:                                  # the module is this slice's: on the base the tests still
    from hoh import budgetconfig      # run, and fail on the behaviour that is missing
except ImportError:                   # pragma: no cover - the base
    budgetconfig = None
from hoh.launcher import HohRunLauncher
from hoh.project import Lifecycle

CONFIG: dict = {}
HERE = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _the_module_exists():
    assert budgetconfig is not None, "hoh.budgetconfig does not exist on this tree"


def build_driver(store):
    """The project's driver: the real launcher, a fake process boundary, **no budget**."""
    cfg = CONFIG
    launcher = Launcher(cfg["root"], cfg["repo"], cfg["script"], wave_width=cfg.get("width", 1),
                        **cfg.get("own", {}))
    if cfg.get("no_bind"):
        launcher.bind_budget = None                    # a launcher that cannot take a declaration
    cfg["launcher"] = launcher
    return launcher, Gates()


def world(tmp_path, names=("n1", "n2", "n3"), by_name=None, **cfg):
    repo, root, store = _world(tmp_path, names=names, by_name=by_name)
    CONFIG.clear()
    CONFIG.update(root=root, repo=repo, script={n: "ok" for n in names}, **cfg)
    (store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_dispatch_budget_knob:build_driver", "path": str(HERE)}))
    return repo, root, store


def hoh(root, capsys, *argv):
    rc = cli.main(["--root", str(root), *argv])
    seen = capsys.readouterr()
    return rc, seen.out, seen.err


def declare(root, capsys, budget, share=None, reserve=None, reason="the operator's ceiling"):
    argv = ["project", "budget", "p", "--dispatch-budget", str(budget), "--reason", reason]
    if share is not None:
        argv += ["--dispatch-reservation", str(share)]
    if reserve is not None:
        argv += ["--qa-reserve", str(reserve)]
    return hoh(root, capsys, *argv)


def drive(root, capsys, session="sA"):
    rc, out, err = hoh(root, capsys, "project", "drive", "p", "--session", session)
    assert out.strip(), (rc, err[-500:])
    return rc, json.loads(out)


# --------------------------------------------------------------------------- #
# the knob: written, recorded, read back
# --------------------------------------------------------------------------- #

def test_hoh_project_budget_declares_the_budget_and_reads_it_back(tmp_path, capsys):
    """Catches a knob that is not recorded: `hoh project budget` writes one strict file that
    names who set it and why, `--dispatch-budget` is required to write, a second declaration
    replaces the first (and says what it replaced), and with no value the command prints what
    is declared (null when nothing is)."""
    repo, root, store = world(tmp_path)
    rc, out, err = hoh(root, capsys, "project", "budget", "p")
    assert rc == 0 and json.loads(out)["declared"] is None, (out, err)
    rc, out, err = declare(root, capsys, 6, share=2, reserve=2)
    assert rc == 0, err
    first = json.loads(out)
    assert first["replaced"] is None
    d = first["declared"]
    assert (d["dispatch_budget"], d["dispatch_reservation"], d["qa_reserve"]) == (6, 2, 2)
    assert d["reason"] == "the operator's ceiling" and d["set_by"].startswith("operator-")
    on_disk = budgetconfig.read_config(store.dir)
    assert on_disk is not None and on_disk.describe().startswith("dispatch_budget=6, share 2, qa_reserve=2")
    assert (store.dir / budgetconfig.FILE).stat().st_mode & 0o777 == 0o644
    rc, out, err = hoh(root, capsys, "project", "budget", "p")
    assert rc == 0 and json.loads(out)["declared"] == d
    rc, out, err = declare(root, capsys, 9)
    assert rc == 0 and json.loads(out)["replaced"] == d
    assert budgetconfig.read_config(store.dir).dispatch_reservation is None


def test_a_declaration_that_does_not_read_back_as_written_is_refused(tmp_path, monkeypatch):
    """Catches a write that is reported as done when the file is not what was written (a full
    disk, a lying filesystem): the write is read back and compared; here the write does
    nothing, so the command is refused and no declaration exists."""
    from hoh import durable_io

    repo, root, store = world(tmp_path)
    monkeypatch.setattr(durable_io, "durable_write", lambda *a, **k: None)
    with pytest.raises(budgetconfig.BudgetConfigError, match="read back"):
        budgetconfig.write_config(store.dir, dispatch_budget=6, dispatch_reservation=None,
                                  qa_reserve=0, set_by="operator-1", epoch=None, reason="r")
    assert budgetconfig.read_config(store.dir) is None


@pytest.mark.parametrize("argv, rc", [
    (["--dispatch-budget", "0"], 4), (["--dispatch-budget", "-3"], 4),
    (["--dispatch-budget", "6", "--qa-reserve", "6"], 4),
    (["--dispatch-budget", "6", "--qa-reserve", "-1"], 4),
    (["--dispatch-budget", "6", "--dispatch-reservation", "0"], 4),
    (["--dispatch-budget", str(10**9 + 1)], 4),
    (["--qa-reserve", "1"], 2), (["--dispatch-reservation", "2"], 2),
])
def test_a_value_no_launcher_may_be_given_is_refused_and_the_declaration_is_untouched(
        tmp_path, capsys, argv, rc):
    """Catches a knob that accepts what the launcher refuses (or writes before it checks):
    every value out of the launcher's own rules is refused (exit 4; a reserve or a share with
    no budget is a usage error, exit 2) and the declaration on disk is the one before."""
    repo, root, store = world(tmp_path)
    assert declare(root, capsys, 6, share=2, reserve=2)[0] == 0
    before = (store.dir / budgetconfig.FILE).read_bytes()
    code, out, err = hoh(root, capsys, "project", "budget", "p", *argv, "--reason", "r")
    assert code == rc, (code, out, err)
    assert "refused" in err
    assert (store.dir / budgetconfig.FILE).read_bytes() == before


def test_a_declaration_needs_a_reason_and_the_masters_credential_when_one_is_alive(tmp_path, capsys):
    """Catches a budget set without a reason, or by someone who is not the master: no reason
    is a usage error; on a project a master holds, a wrong session/epoch/token is refused
    (exit 5, the declaration unchanged); with no live master an operator epoch is taken and
    released (the command of `unblock`)."""
    repo, root, store = world(tmp_path)
    rc, out, err = hoh(root, capsys, "project", "budget", "p", "--dispatch-budget", "6")
    assert rc == 2 and "--reason" in err
    rc, out = drive(root, capsys)                          # the project is governed from here on
    assert rc == 0 and out["halt"] == "CLOSED", out
    rc, out, err = hoh(root, capsys, "project", "budget", "p", "--dispatch-budget", "6", "--reason", "r",
                       "--session", "someone-else", "--epoch", "7", "--token", "not-the-token")
    assert rc == 5 and "refused" in err
    assert budgetconfig.read_config(store.dir) is None
    rc, out, err = declare(root, capsys, 6, share=2, reserve=1)    # no live master: an operator epoch
    assert rc == 0, err
    assert budgetconfig.read_config(store.dir).dispatch_budget == 6


# --------------------------------------------------------------------------- #
# the file is strict, and a bad one halts by name
# --------------------------------------------------------------------------- #

def test_a_declaration_that_is_not_one_is_refused_and_a_drive_over_it_halts_by_name(tmp_path, capsys):
    """Catches a declaration read as "no budget" when it is odd (fail open): corrupt JSON, a
    list, an extra / missing key, a repeated key, NaN, a bool for a number, a wrong schema,
    a reserve of the whole budget, non-ASCII, a NUL, oversize, and a link, a FIFO or a
    directory at the path are each `BudgetConfigError`; a missing file is None; and a drive
    over a bad one halts AMBIGUOUS with the reason and launches nothing."""
    repo, root, store = world(tmp_path)
    assert declare(root, capsys, 6, share=2, reserve=2)[0] == 0
    path = store.dir / budgetconfig.FILE
    good = json.loads(path.read_bytes())

    def variant(**changes):
        d = dict(good)
        d.update(changes)
        return json.dumps(d).encode()

    bad = {
        "not json": b"{", "list": b"[]", "empty": b"",
        "extra key": variant(extra=1), "missing key": json.dumps(
            {k: v for k, v in good.items() if k != "qa_reserve"}).encode(),
        "repeated key": b'{"schema": "hoh-dispatch-budget/1", "schema": "hoh-dispatch-budget/1"}',
        "nan": path.read_bytes().replace(b'"qa_reserve": 2', b'"qa_reserve": NaN'),
        "bool budget": variant(dispatch_budget=True), "string budget": variant(dispatch_budget="6"),
        "float budget": variant(dispatch_budget=6.0),
        "schema": variant(schema="hoh-dispatch-budget/2"),
        "reserve = budget": variant(qa_reserve=6), "reserve negative": variant(qa_reserve=-1),
        "share zero": variant(dispatch_reservation=0),
        "reason non-ascii": variant(reason="café"), "reason NUL": variant(reason="a\u0000b"),
        "set_by space": variant(set_by="a b"), "set_at month 13": variant(set_at="2026-13-01T00:00:00Z"),
        "oversize": json.dumps(good).encode() + b" " * (budgetconfig.MAX_BYTES + 1),
    }
    accepted = []
    for label, data in bad.items():
        try:
            budgetconfig.parse(data)
        except budgetconfig.BudgetConfigError:
            continue
        accepted.append(label)
    assert accepted == []
    assert budgetconfig.parse(path.read_bytes()).dispatch_budget == 6

    side = tmp_path / "elsewhere.json"
    side.write_bytes(path.read_bytes())
    # the four spellings at the path itself
    for number, label in enumerate(("corrupt", "link", "fifo", "directory")):
        if path.is_symlink() or path.exists():
            os.replace(path, tmp_path / f"was-{number}")
        if label == "corrupt":
            path.write_bytes(b"{")
        elif label == "link":
            path.symlink_to(side)
        elif label == "fifo":
            os.mkfifo(path)
        else:
            path.mkdir()
        with pytest.raises(budgetconfig.BudgetConfigError):
            budgetconfig.read_config(store.dir)
        rc, out = drive(root, capsys)
        assert out["halt"] == "AMBIGUOUS" and budgetconfig.FILE in out["reason"], (label, out)
        assert CONFIG["launcher"].calls == [], label       # nothing was started
        assert {n.lifecycle for n in _nodes(store).values()} == {Lifecycle.READY}
        rc, out, err = hoh(root, capsys, "project", "budget", "p")
        assert rc == 4 and "refused" in err                 # the read-back refuses too
    os.replace(path, tmp_path / "was-last")
    assert budgetconfig.read_config(store.dir) is None


# --------------------------------------------------------------------------- #
# P3-23: the reserve through the knob, on the production path
# --------------------------------------------------------------------------- #

def test_hoh_project_budget_sets_the_qa_reserve_the_production_launcher_keeps_from_developer_nodes(
        tmp_path, capsys):
    """The knob end to end: a driver whose launcher has **no budget**, `hoh project budget`
    (budget 6, share 2, reserve 2), `hoh project drive` -> `ProjectController` binds the
    declaration to the launcher -> `prepare` / `take_hold` keep the reserve. Three developer
    nodes at width 3: the third finds only the reserve left, is refused **before** its
    worktree exists and parks BLOCKED naming the reserve; the drive says what it bound
    (`DISPATCH_BUDGET`); with a repair node in the third place the reserve is granted and the
    project closes. The same world with **no declaration** is unbudgeted (no ceiling)."""
    repo, root, store = world(tmp_path, width=3)
    assert declare(root, capsys, 6, share=2, reserve=2)[0] == 0
    rc, out = drive(root, capsys)
    nodes = _nodes(store)
    assert nodes["n1"].lifecycle is Lifecycle.MERGED and nodes["n2"].lifecycle is Lifecycle.MERGED
    assert nodes["n3"].lifecycle is Lifecycle.BLOCKED
    assert "QA reserve of 2" in nodes["n3"].note and "repair" in nodes["n3"].note
    assert "BUDGET_EXHAUSTED:n3" in out["steps"], out
    assert out["steps"][0].startswith("DISPATCH_BUDGET:"), out["steps"]
    launcher = CONFIG["launcher"]
    assert (launcher.dispatch_budget, launcher.dispatch_reservation, launcher.qa_reserve) == (6, 2, 2)
    named = re.compile(r"(?<![A-Za-z0-9])n3(?![A-Za-z0-9])")
    assert not [c for c in launcher.calls if any(named.search(part) for part in c)]
    starts = [c for c in launcher.calls if "start" in c]
    assert [c[c.index("--max-dispatches") + 1] for c in starts] == ["2", "2"]

    other = tmp_path / "repair"
    other.mkdir()
    repo, root, store = world(other, names=("n1", "n2", "r1"), by_name={"r1": {"repair_of": "n0"}}, width=3)
    assert declare(root, capsys, 6, share=2, reserve=2)[0] == 0
    rc, out = drive(root, capsys)
    assert out["halt"] == "CLOSED", out
    assert {n.lifecycle for n in _nodes(store).values()} == {Lifecycle.MERGED}
    starts = [c for c in CONFIG["launcher"].calls if "start" in c]
    assert sorted(c[c.index("--max-dispatches") + 1] for c in starts) == ["2", "2", "2"]

    free = tmp_path / "unbudgeted"
    free.mkdir()
    repo, root, store = world(free, width=3)
    rc, out = drive(root, capsys)
    assert out["halt"] == "CLOSED", out
    assert not [s for s in out["steps"] if s.startswith("DISPATCH_BUDGET")]
    assert CONFIG["launcher"].dispatch_budget is None
    assert not [c for c in CONFIG["launcher"].calls if "--max-dispatches" in c and "start" in c
                and c[c.index("--max-dispatches") + 1] in ("2",)]


def test_a_launcher_with_a_budget_of_its_own_is_refused_a_second_source_and_the_same_values_are_a_no_op(
        tmp_path, capsys):
    """Catches two sources of one ceiling resolving silently: the driver's launcher sets
    budget 6 with a share of 3 and the project declares another share and a reserve -> the drive
    halts AMBIGUOUS naming both and starts nothing; the same values are accepted and bind nothing new."""
    repo, root, store = world(tmp_path, names=("n1", "n2"), width=2,
                              own={"dispatch_budget": 6, "dispatch_reservation": 3})
    assert declare(root, capsys, 6, share=2, reserve=1)[0] == 0
    rc, out = drive(root, capsys)
    assert out["halt"] == "AMBIGUOUS" and "one source of the ceiling" in out["reason"], out
    assert CONFIG["launcher"].calls == []
    assert declare(root, capsys, 6, share=3)[0] == 0
    rc, out = drive(root, capsys, session="sB")
    assert out["halt"] == "CLOSED", out
    assert CONFIG["launcher"].dispatch_budget == 6


@pytest.mark.parametrize("values", [(6, 0, 0), (6, True, 0), (6, 2, -1), (6, 2, True), (6, 2, 6),
                                    (6, None, 7)])
def test_bind_budget_refuses_what_the_launcher_refuses(tmp_path, values):
    """Catches the two statements of the rules drifting apart: every triple the launcher's
    constructor refuses is refused by `bind_budget` (and nothing is bound); the triples it
    accepts are bound. (`bind_budget` is also stricter on the budget itself -- a bool or 0 --
    which the constructor leaves to the run's own `--max-dispatches` check.)"""
    repo, root, store = _world(tmp_path)
    budget, share, reserve = values
    with pytest.raises(ValueError):
        HohRunLauncher(root, repo, dispatch_budget=budget, dispatch_reservation=share, qa_reserve=reserve)
    launcher = HohRunLauncher(root, repo)
    with pytest.raises(ValueError):
        launcher.bind_budget(budget, share, reserve)
    assert (launcher.dispatch_budget, launcher.dispatch_reservation, launcher.qa_reserve) == (None, None, 0)
    ok = HohRunLauncher(root, repo)
    ok.bind_budget(6, 2, 1)
    assert (ok.dispatch_budget, ok.dispatch_reservation, ok.qa_reserve) == (6, 2, 1)


# --------------------------------------------------------------------------- #
# P3-22: the recovery of a dead controller's holds, under the declared budget only
# --------------------------------------------------------------------------- #

def test_a_hold_a_dead_controller_left_is_recovered_when_the_budget_is_only_the_projects_declaration(
        tmp_path, capsys):
    """The recovery of slice 3 on the production path with no code of the project's own that
    sets a budget: the driver builds the launcher with `budget=None`; the budget is
    `hoh project budget`. A controller that dies between the wave's record and RUNNING leaves
    both holds HELD (they exist only because the declaration reached the launcher); the next
    drive ends exactly those holds, says `WAVE_RECOVERED`, and the project closes."""
    repo, root, store = wr.world(tmp_path, budget=None, share=None)
    assert declare(root, capsys, 6, share=3)[0] == 0
    wr.crash(root, capsys)
    assert _held_ids(root) == ["n1", "n2"]
    rc, out = wr.drive(root, capsys)
    assert out["halt"] == "CLOSED", out
    path, outcome = wr.the_outcome(store)
    assert f"WAVE_RECOVERED:{outcome.wave_id}" in out["steps"]
    assert outcome.phase.value == "RECOVERED" and _held_ids(root) == []
    assert {n.lifecycle for n in _nodes(store).values()} == {Lifecycle.MERGED}


@pytest.mark.parametrize("budget", [True, 0, -1, 10**9 + 1, 1.5, "6"])
def test_bind_budget_refuses_a_budget_that_is_not_a_whole_number_of_dispatches(tmp_path, budget):
    """Catches a budget of the wrong type, sign or size being bound (the constructor takes
    whatever it is given; the declaration never reaches it that way)."""
    repo, root, store = _world(tmp_path)
    launcher = HohRunLauncher(root, repo)
    with pytest.raises(ValueError):
        launcher.bind_budget(budget, None, 0)
    assert launcher.dispatch_budget is None


# --------------------------------------------------------------------------- #
# fail closed, each claim of the spec pinned by its own node
# --------------------------------------------------------------------------- #

def test_a_launcher_that_cannot_take_a_declaration_is_refused_by_name_and_not_run_unbudgeted(
        tmp_path, capsys):
    """Catches a project that declares a budget and runs without one because its launcher
    has no `bind_budget` (D3): `bind` raises `BudgetConfigError` naming the file and the
    launcher, and a drive over such a launcher halts AMBIGUOUS, nothing started."""
    repo, root, store = world(tmp_path, width=3, no_bind=True)
    assert declare(root, capsys, 6, share=2, reserve=2)[0] == 0
    with pytest.raises(budgetconfig.BudgetConfigError, match="cannot take one"):
        budgetconfig.bind(store.dir, object())
    rc, out = drive(root, capsys)
    assert out["halt"] == "AMBIGUOUS" and "cannot take one" in out["reason"], out
    assert budgetconfig.FILE in out["reason"]
    assert CONFIG["launcher"].calls == [] and CONFIG["launcher"].dispatch_budget is None


def test_a_declaration_with_a_second_name_is_refused(tmp_path, capsys):
    """Catches a declaration read through any path that follows a hard link (the ledgers
    of HoH are read with `read_single_link`): a second name for the file is refused by
    `read_config`, by a drive (AMBIGUOUS, nothing started) and by the read-back verb."""
    repo, root, store = world(tmp_path)
    assert declare(root, capsys, 6, share=2, reserve=2)[0] == 0
    os.link(store.dir / budgetconfig.FILE, tmp_path / "alias.json")
    with pytest.raises(budgetconfig.BudgetConfigError):
        budgetconfig.read_config(store.dir)
    rc, out = drive(root, capsys)
    assert out["halt"] == "AMBIGUOUS" and CONFIG["launcher"].calls == [], out
    rc, o, err = hoh(root, capsys, "project", "budget", "p")
    assert rc == 4 and "refused" in err


def test_a_declaration_with_a_repeated_key_is_refused_though_every_key_is_there(tmp_path, capsys):
    """Catches a duplicate key read as the last one (a mutant of the strict parser): the
    text has all eight keys, each valid, and one of them twice, so only the duplicate
    check can refuse it."""
    repo, root, store = world(tmp_path)
    assert declare(root, capsys, 6, share=2, reserve=2)[0] == 0
    good = (store.dir / budgetconfig.FILE).read_bytes()
    assert budgetconfig.parse(good).qa_reserve == 2
    repeated = good.replace(b"{\n", b'{\n  "qa_reserve": 2,\n', 1)
    assert repeated.count(b'"qa_reserve"') == 2
    with pytest.raises(budgetconfig.BudgetConfigError, match="repeated"):
        budgetconfig.parse(repeated)


def _live_master(store):
    from hoh.succession import MasterAuthority

    a = MasterAuthority(store.dir, "sM", serialize=store.lock)
    a.acquire()
    return a


def test_a_live_master_is_the_authority_of_a_declaration_and_the_epoch_is_recorded(tmp_path, capsys):
    """Catches the authority paths RB3 left: with a **live master** and no credential a
    declaration is refused (exit 5); a **bad value is still exit 4** there (the values are
    checked before the authority is asked); the master's own session, epoch and token write it
    and the file records them (`set_by` the session, `epoch` its epoch); a wrong epoch is
    refused; and an operator epoch taken when no master is alive is recorded too (not null)."""
    repo, root, store = world(tmp_path)
    master = _live_master(store)
    base = ["project", "budget", "p", "--reason", "r"]
    rc, out, err = hoh(root, capsys, *base, "--dispatch-budget", "6")
    assert rc == 5 and "live master" in err, (rc, err)
    assert budgetconfig.read_config(store.dir) is None
    rc, out, err = hoh(root, capsys, *base, "--dispatch-budget", "6", "--qa-reserve", "6")
    assert rc == 4, (rc, err)                           # the value is refused before the authority
    cred = ["--session", "sM", "--token", master.token]
    rc, out, err = hoh(root, capsys, *base, "--dispatch-budget", "6", *cred, "--epoch", str(master.epoch + 7))
    assert rc == 5, (rc, err)
    rc, out, err = hoh(root, capsys, *base, "--dispatch-budget", "6", "--dispatch-reservation", "2",
                       *cred, "--epoch", str(master.epoch))
    assert rc == 0, (rc, err)
    made = budgetconfig.read_config(store.dir)
    assert (made.set_by, made.epoch, made.dispatch_budget) == ("sM", master.epoch, 6)
    master.release(reason="orderly halt")
    rc, out, err = hoh(root, capsys, *base, "--dispatch-budget", "7")
    assert rc == 0, (rc, err)
    operator = budgetconfig.read_config(store.dir)
    assert operator.set_by.startswith("operator-") and isinstance(operator.epoch, int)
    assert operator.epoch > master.epoch                # an epoch of its own, taken and released


def test_a_master_superseded_between_the_claim_and_the_write_is_refused_not_a_traceback(
        tmp_path, capsys, monkeypatch):
    """Catches `StaleMaster` escaping `cli.main`: the authority section raises it (a master
    superseded or expired between the claim and the write): exit 5, `refused:`, nothing written."""
    from contextlib import contextmanager

    from hoh.succession import StaleMaster

    repo, root, store = world(tmp_path)

    @contextmanager
    def superseded(*a, **k):
        raise StaleMaster("this session was superseded")
        yield

    monkeypatch.setattr(cli, "_authority_section", superseded)
    rc, out, err = declare(root, capsys, 6)
    assert rc == 5 and "refused" in err and "superseded" in err, (rc, err)
    assert budgetconfig.read_config(store.dir) is None


def test_the_register_reads_p3_22_p3_23_and_p3_24_proven_on_this_tree():
    """Catches the register edit being left out (the headline of the slice rests on it):
    `capability_matrix.classify`, with this tree's own symbols and import graph and the node
    ids of the test files this tree holds, reads the three rows PROVEN -- a row that still has
    an `equivalent`, or whose probe names a test that does not exist, reads PARTIAL."""
    import ast
    import importlib.util
    import sys

    root = HERE.parent
    spec = importlib.util.spec_from_file_location("capability_matrix_rows", root / "tools" / "capability_matrix.py")
    cm = importlib.util.module_from_spec(spec)
    sys.modules["capability_matrix_rows"] = cm
    spec.loader.exec_module(cm)
    register = json.loads((root / "program" / "v3_3" / "REQUIREMENTS.json").read_text())
    rows = {r["id"]: r for r in register["requirements"]}
    ids = []
    for path in sorted((root / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        ids += [f"tests/{path.name}::{n.name}" for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name.startswith("test_")]
    collection = ("\n".join(ids), True)
    symbols, graph = cm._symbols(), cm._import_graph()
    seen = {}
    for rid in ("P3-22", "P3-23", "P3-24"):
        assert "equivalent" not in rows[rid]["probe"], rid
        seen[rid] = cm.classify(rows[rid], symbols, collection, {}, graph)["status"]
    assert seen == {"P3-22": cm.PROVEN, "P3-23": cm.PROVEN, "P3-24": cm.PROVEN}, seen
