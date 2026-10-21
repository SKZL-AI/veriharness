"""P3-INV1 -- `spent + active_reserved <= authorized_budget`, on the path the project drive takes.

The ledger (`reservation.BudgetReservations`) grants a hold only out of what is neither spent nor held, under one
lock. That is half of the sentence: a run is only kept to its hold if the process that dispatches is told where
the hold ends. A run `hoh start` created carries its grant as its own ceiling (`--max-dispatches`); a run that
existed when the launcher met it -- the second launch of a node, a resume after a park or a crash -- kept the
ceiling it began with, whatever the other runs had spent meanwhile (the limit L2 of P3-04 slice 1). These tests
drive the whole sentence:

* `Controller(dispatch_ceiling=)` / `hoh run --dispatch-ceiling` stop a process where its hold ends and write
  nothing into the run's own budgets;
* the launcher holds, for a run that exists, what the OTHER runs leave (its own past dispatches included) and
  types that as the ceiling of its `hoh run`, on the sequential round and in a wave;
* with real run states under one root, concurrent starts are granted no more than the budget less what is
  spent;
* through `hoh project budget` + `hoh project drive`, the dispatches of every run under the root add up to at
  most the declared budget, and to all of it when the work can use it.

What is real: `cli.main` (`hoh start`, `hoh run`, `hoh project ...`), `Controller`, `HohRunLauncher`
(`prepare`, `take_hold`, `launch`), the ledger, the project loop and the wave executor, the run states on disk.
What is replaced: the provider (`tools/budget_evidence.CountingDispatcher`: it never satisfies a run and counts
what reached it) and `hoh worktree` (no Herdr). Everything is under `tmp_path`; nothing starts an agent.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import importlib.util
import io
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hoh import cli, dispatchers, rundrive
from hoh import controller as controller_module
from hoh.contracts import Budgets, Condition, RunState, Stage, Usage
from hoh.controller import Controller, DispatchError
from hoh.launcher import Dispatch
from hoh.orchestrator import BudgetExhausted, RunVerdict
from hoh.project import Lifecycle
from hoh.reservation import BudgetReservations, HoldStatus
from hoh.store import RunStore

from test_wave_controller import FakeCliLauncher, Gates, _nodes, _world, git

HOH = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
#: the driver factory of the drive tests reads its world from here (imported by name through `driver.json`)
CONFIG: dict = {}
BUDGET = 10


def _load_budget_evidence():
    spec = importlib.util.spec_from_file_location("budget_evidence", HOH / "tools" / "budget_evidence.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["budget_evidence"] = mod
    spec.loader.exec_module(mod)
    return mod


be = _load_budget_evidence()
#: roles per iteration of the fixture provider: planner, developer, QA
PER_ITERATION = be.ROLES_PER_ITERATION


# --------------------------------------------------------------------------- #
# the controller: stopped where it was told, nothing written into the run's own budgets
# --------------------------------------------------------------------------- #

def _fixture_run(where: Path, *, cap: int | None, ceiling: int | None, dispatched: int = 0):
    """A real run of `be.CountingDispatcher`'s kind, run by a real `Controller` until it stops."""
    repo = be._fixture_repo(where)
    spec = where / "spec.md"
    spec.write_text("# Spec\nadd(a,b) must add correctly.\n", encoding="utf-8")
    store = RunStore(where / "runs", "r")
    state = Controller.new_state(
        run_id="r", repo_path=repo, project_name="project", spec_path=spec,
        # only the dispatch bounds may bind (as in `tools/budget_evidence.py`), but a bound the run is
        # not kept to by the code under test must end the test, not run it for ever: 6 iterations are
        # 18 dispatches, more than any ceiling below
        budgets=Budgets(max_iterations=6, max_loops_without_progress=99, max_dispatches=cap))
    state.usage.dispatches = dispatched
    store.write_state(state)
    dispatcher = be.CountingDispatcher(repo, store)
    told = {} if ceiling is None else {"dispatch_ceiling": ceiling}      # the base has no such argument
    controller = Controller(store, dispatcher, spec_path=spec, **told)
    refused = None
    for _ in range(6):
        try:
            controller.run_iteration(state)
        except DispatchError as exc:
            refused = str(exc)
            break
        if state.condition is not Condition.ACTIVE:
            break
    persisted = RunStore(where / "runs", "r").read_state()
    # where the refusal surfaces depends on the role that met it: raised out of the iteration, or
    # booked inside it (a QA that cannot be dispatched is an outage, the block follows)
    said = " | ".join([refused or "", persisted.blocked_reason or "", persisted.stop_reason or "",
                       *persisted.history])
    return dispatcher, persisted, said, state


@pytest.mark.parametrize("ceiling", [4, 5, 7])
def test_a_controller_told_a_ceiling_stops_exactly_there_and_leaves_the_runs_own_budgets_alone(
        tmp_path, ceiling):
    """Catches a ceiling that is ignored (more calls reach the provider than the hold allows), off by one
    (`>` for `>=`), applied at the start of an iteration only (4 and 5 are not multiples of the 3 roles of an
    iteration, so the stop lands between two dispatches of one iteration), or written into the run's state
    (a ceiling of one launch would then cap every later one)."""
    assert ceiling % PER_ITERATION != 0
    dispatcher, persisted, said, _ = _fixture_run(tmp_path, cap=None, ceiling=ceiling)
    assert len(dispatcher.calls_) == ceiling
    assert persisted.usage.dispatches == ceiling
    assert persisted.budgets.max_dispatches is None                       # never written
    assert f"dispatch ceiling of this launch reached ({ceiling}/{ceiling})" in said


def test_a_ceiling_on_an_iteration_boundary_blocks_the_run_at_the_start_of_the_next_one(tmp_path):
    """Catches the start-of-iteration check missing (a run at its ceiling would be planned again and only
    its first dispatch refused): the run is BLOCKED, naming the ceiling, having dispatched nothing more."""
    dispatcher, persisted, said, state = _fixture_run(tmp_path, cap=None, ceiling=PER_ITERATION)
    assert len(dispatcher.calls_) == PER_ITERATION
    assert state.condition is Condition.BLOCKED
    assert "dispatch ceiling of this launch reached" in (persisted.blocked_reason or "")
    assert persisted.budgets.max_dispatches is None


def test_the_lower_of_the_runs_own_ceiling_and_the_one_told_applies(tmp_path):
    """Catches a told ceiling that RAISES the run's own (a launch must never loosen what `hoh start` set):
    own 4, told 9 -> 4 calls and the run's own words; own 9, told 4 -> 4 calls and the ceiling's."""
    dispatcher, persisted, said, _ = _fixture_run(tmp_path / "a", cap=4, ceiling=9)
    assert len(dispatcher.calls_) == 4 and "dispatch budget exhausted (4/4)" in said
    dispatcher, persisted, said, _ = _fixture_run(tmp_path / "b", cap=9, ceiling=4)
    assert len(dispatcher.calls_) == 4 and persisted.budgets.max_dispatches == 9
    assert "dispatch ceiling of this launch reached (4/4)" in said


def test_a_ceiling_counts_the_runs_dispatches_of_earlier_launches(tmp_path):
    """Catches a ceiling that counts only this process's dispatches (a resumed run would get a fresh window
    each time): the run has dispatched 5 already, the ceiling is 7 -> two more, not seven."""
    dispatcher, persisted, said, _ = _fixture_run(tmp_path, cap=None, ceiling=7, dispatched=5)
    assert len(dispatcher.calls_) == 2
    assert persisted.usage.dispatches == 7


def test_without_a_told_ceiling_the_controller_is_what_it_was(tmp_path):
    """Compatibility (green on the base too, killed by a ceiling that defaults to something): no ceiling, the
    run's own cap alone decides."""
    dispatcher, persisted, said, _ = _fixture_run(tmp_path, cap=7, ceiling=None)
    assert len(dispatcher.calls_) == 7 and "dispatch budget exhausted (7/7)" in said


# --------------------------------------------------------------------------- #
# `hoh run --dispatch-ceiling`
# --------------------------------------------------------------------------- #

class _Counting(be.CountingDispatcher):
    """The fixture provider as `build_dispatcher` returns it to `hoh run`."""

    def __init__(self, root: Path, run_id: str, repo: Path, sink: list) -> None:
        super().__init__(repo, RunStore(root, run_id))
        sink.append(self)


class Cli:
    """The real command line, in this process, with the provider replaced."""

    def __init__(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_register_trust", lambda *a, **kw: None)
        # storage admission is not what is measured here: the stub the other `hoh run` tests use
        # (`tests/test_dispatch_robustness.py`) and a controller without an admission
        ledger = tmp_path / "storage" / "ledger"
        monkeypatch.setattr(cli, "storage_admission_for", lambda option, **kw: (
            types.SimpleNamespace(ledger=ledger), tmp_path / "storage"))
        monkeypatch.setattr(cli, "_hook_steward", lambda *a, **kw: None)

        class NoAdmission(controller_module.Controller):
            def __init__(inner, *a, **kw):
                kw["storage_admission"] = None
                super().__init__(*a, **kw)

        monkeypatch.setattr(controller_module, "Controller", NoAdmission)
        self.tmp, self.capsys = tmp_path, capsys
        self.root = tmp_path / "runs"
        self.root.mkdir()
        self.built: list[_Counting] = []
        root, built = self.root, self.built

        def fake_build_dispatcher(*, answers_dir, cwd, **_kw):
            return _Counting(root, Path(answers_dir).parent.name, Path(cwd), built)

        monkeypatch.setattr(dispatchers, "build_dispatcher", fake_build_dispatcher)

    def new_run(self, run_id: str, *, dispatched: int = 0, cap: int | None = None) -> RunStore:
        repo = be._fixture_repo(self.tmp / f"repo-{run_id}")
        spec = self.tmp / f"{run_id}.md"
        spec.write_text("# Spec\nadd(a,b) must add correctly.\n", encoding="utf-8")
        store = RunStore(self.root, run_id)
        state = Controller.new_state(
            run_id=run_id, repo_path=repo, project_name="project", spec_path=spec,
            budgets=Budgets(max_iterations=99, max_loops_without_progress=99, max_dispatches=cap))
        state.usage.dispatches = dispatched
        store.write_state(state)
        return store

    def run(self, run_id: str, *extra: str):
        argv = ["--root", str(self.root), "run", run_id, "--iterations", "9", "--no-herdr",
                "--isolation", "none", *extra]
        rc = cli.main(argv)
        seen = self.capsys.readouterr()
        return rc, seen.out, seen.err


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    return Cli(tmp_path, monkeypatch, capsys)


@pytest.mark.parametrize("spelling", [["--dispatch-ceiling", "7"], ["--dispatch-ceiling=7"]])
def test_hoh_run_dispatch_ceiling_stops_the_real_command_where_it_is_told(world, spelling):
    """Catches `--dispatch-ceiling` parsed and dropped (the controller is never told), told but not enforced,
    or written into the run: the real `hoh run`, a run that has dispatched 2 before, ceiling 7 -> five more
    reach the provider, the run's `budgets` still say none, the command says what it was told."""
    store = world.new_run("r1", dispatched=2)
    before_budgets = store.read_state().budgets
    rc, out, err = world.run("r1", *spelling)
    (dispatcher,) = world.built
    assert len(dispatcher.calls_) == 5, (rc, err[-400:])
    after = store.read_state()
    assert after.usage.dispatches == 7 and after.budgets == before_budgets
    assert after.budgets.max_dispatches is None
    shown = [o["dispatch_ceiling"] for o in map(json.loads, _json_objects(out)) if "dispatch_ceiling" in o]
    assert shown == [{"ceiling": 7, "dispatched": 2, "run_max_dispatches": None}]


def test_hoh_run_without_the_option_is_unchanged(world):
    """Compatibility (green on the base): no option -> the run's own cap decides, nothing is said about a
    ceiling."""
    world.new_run("r1", cap=4)
    rc, out, err = world.run("r1")
    (dispatcher,) = world.built
    assert len(dispatcher.calls_) == 4
    assert "dispatch_ceiling" not in out


@pytest.mark.parametrize("bad", ["0", "-3"])
def test_hoh_run_refuses_a_ceiling_below_one_before_anything_is_written(world, bad):
    """Catches a ceiling of 0 or below accepted (every dispatch would be refused after the run was opened, its
    storage claimed and a note written) -- and a refusal that leaves a trace: exit 2, the reason on stderr, no
    provider, the run's state byte for byte as it was."""
    store = world.new_run("r1")
    before = store.state_path.read_bytes()
    rc, out, err = world.run("r1", "--dispatch-ceiling", bad)
    assert rc == 2 and "--dispatch-ceiling must be a whole number of at least 1" in err
    assert world.built == [] and store.state_path.read_bytes() == before


def test_hoh_run_refuses_a_ceiling_that_is_not_a_whole_number(world):
    """Catches `type=int` replaced by a float or a string (a ceiling of 2.5 or `many`): argparse refuses, exit 2."""
    store = world.new_run("r1")
    before = store.state_path.read_bytes()
    for bad in ("2.5", "many", ""):
        with pytest.raises(SystemExit) as exc:
            cli.main(["--root", str(world.root), "run", "r1", "--no-herdr", "--dispatch-ceiling", bad])
        assert exc.value.code == 2
    world.capsys.readouterr()
    assert world.built == [] and store.state_path.read_bytes() == before


def test_a_detached_run_types_the_ceiling_into_the_tab_and_a_bad_one_is_refused_before_a_tab_is_opened(
        world, monkeypatch):
    """Catches a ceiling dropped on the way into a tab (`detach.run_argv` does not carry it: the tab's `hoh run`
    would then dispatch without it) and a bad ceiling that is checked only after the tab is opened: the
    command typed into the tab carries `--dispatch-ceiling=7`; with 0 the command exits 2 and `detach` is
    never reached."""
    from hoh import detach

    parser = cli.build_parser()
    args = parser.parse_args(["--root", str(world.root), "run", "r1", "--detach", "--dispatch-ceiling", "7"])
    typed = detach.run_argv(args, ["python3", "-m", "hoh.cli"])
    assert "--dispatch-ceiling=7" in typed and "--detach" not in typed
    assert parser.parse_args(typed[3:]).dispatch_ceiling == 7
    assert not any(a.startswith("--dispatch-ceiling") for a in detach.run_argv(
        parser.parse_args(["--root", str(world.root), "run", "r1", "--detach"]), ["python3", "-m", "hoh.cli"]))

    def reached(args):
        raise AssertionError("a tab was opened for a ceiling that is refused")

    monkeypatch.setattr(detach, "cmd_detach", reached)
    store = world.new_run("r1")
    before = store.state_path.read_bytes()
    rc, out, err = world.run("r1", "--dispatch-ceiling", "0", "--detach")
    assert rc == 2 and "--dispatch-ceiling must be a whole number of at least 1" in err
    assert world.built == [] and store.state_path.read_bytes() == before


def _json_objects(text: str) -> list[str]:
    """The JSON objects `hoh` printed one after another (`_print` indents them), as text."""
    decoder, at, found = json.JSONDecoder(), 0, []
    while (at := text.find("{", at)) != -1:
        try:
            obj, end = decoder.raw_decode(text, at)
        except ValueError:
            at += 1
            continue
        found.append(json.dumps(obj))
        at = end
    return found


# --------------------------------------------------------------------------- #
# the launcher, on real run states: the hold of a run that exists, and what its `hoh run` is told
# --------------------------------------------------------------------------- #

class RealCliLauncher(FakeCliLauncher):
    """`FakeCliLauncher`'s `hoh worktree` (no Herdr); `hoh start` and `hoh run` are the real commands, run in
    this process with the fixture provider -- so the argv the launcher types is what runs."""

    def _run_cli(self, argv):
        if "worktree" in argv:
            return super()._run_cli(argv)
        with self._lock:
            self.calls.append(list(argv))
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(argv[argv.index("--root"):])
        return Dispatch(rc, out.getvalue(), err.getvalue())


def dispatched_in(root: Path) -> dict[str, int]:
    """What every run under the root has dispatched, read from the states (not from the launcher)."""
    return {p.parent.name: json.loads(p.read_text(encoding="utf-8")).get("usage", {}).get("dispatches", 0)
            for p in sorted(root.rglob("state.json"))}


def foreign_run(root: Path, run_id: str, dispatched: int) -> None:
    """Another run under the same root (another project's, a person's) that has spent `dispatched`."""
    RunStore(root, run_id).write_state(RunState(
        run_id=run_id, repo_path="/arena/repo", project_name="other", spec_path="/arena/spec.md",
        spec_digest="s" * 16, policy_digest="p" * 16, profile_digest="f" * 16,
        stage=Stage.CHECKPOINTED, condition=Condition.ACTIVE, usage=Usage(dispatches=dispatched)))


def ceilings_of(launcher, run_id: str) -> list[str]:
    """The `--dispatch-ceiling` values of the `hoh run` calls of `run_id`."""
    out = []
    for call in launcher.calls:
        if "run" in call and call[call.index("run") + 1] == run_id and "--dispatch-ceiling" in call:
            out.append(call[call.index("--dispatch-ceiling") + 1])
    return out


def interleaved_world(tmp_path, monkeypatch, capsys, *, width: int = 1, other: int = 5, iterations: int = 1):
    """The state the drive tests start from. A budget of 10 is declared. Node `n1`'s run was started under it
    (so it carries its grant, 10, as its own ceiling) and has dispatched 3; another run has dispatched `other`
    (5: 2 are left). Node `n2` (a wave of two) has no run."""
    names = ("n1", "n2") if width > 1 else ("n1",)
    monkeypatch.delenv("HOH_WAVE_WIDTH", raising=False)                  # the width is the launcher's, not the shell's
    Cli(tmp_path, monkeypatch, capsys)                                   # storage root, trust, provider
    repo, root, store = _world(tmp_path, names=names)
    first = RealCliLauncher(root, repo, {}, dispatch_budget=BUDGET, iterations=1)
    n1 = store.read_state().node("n1")
    assert first.prepare(n1) is None
    first.launch(n1)                                                     # one iteration: 3 dispatches
    assert dispatched_in(root)["n1"] == PER_ITERATION
    assert RunStore(root, "n1").read_state().budgets.max_dispatches == BUDGET     # the grant, as its own ceiling
    assert BudgetReservations(root).held() == []
    foreign_run(root, "other-1", other)
    CONFIG.clear()
    CONFIG.update(root=root, repo=repo, width=width, iterations=iterations)
    (store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_budget_invariant:build_driver", "path": str(HERE)}))
    capsys.readouterr()
    return repo, root, store


def build_driver(store):
    """The project's driver: the real launcher over the real CLI, **no budget** (the declaration binds it)."""
    launcher = RealCliLauncher(CONFIG["root"], CONFIG["repo"], {}, iterations=CONFIG.get("iterations", 1),
                               wave_width=CONFIG.get("width", 1))
    CONFIG["launcher"] = launcher
    return launcher, Gates()


def hoh(root, capsys, *argv):
    rc = cli.main(["--root", str(root), *argv])
    seen = capsys.readouterr()
    return rc, seen.out, seen.err


def declare_and_drive(root, capsys, *declared):
    rc, out, err = hoh(root, capsys, "project", "budget", "p", "--dispatch-budget", str(BUDGET),
                       "--reason", "the operator's ceiling", *declared)
    assert rc == 0, err
    rc, out, err = hoh(root, capsys, "project", "drive", "p", "--session", "sA")
    assert out.strip(), (rc, err[-500:])
    return json.loads(out)


@pytest.mark.parametrize("width", [1, 2])
def test_hoh_project_drive_keeps_the_dispatches_of_every_run_under_the_declared_budget(
        tmp_path, monkeypatch, capsys, width):
    """The invariant, on the production path (`hoh project budget` -> `hoh project drive` -> `ProjectController`
    -> `HohRunLauncher.prepare` / `take_hold` / `launch` -> the real `hoh run` -> `Controller`), for the
    sequential round (width 1) and a wave (width 2). Node n1's run exists with its own ceiling of 10 and has
    dispatched 3; another run under the root has dispatched 5; 2 are left. On the base n1's `hoh run` is
    typed no ceiling and its next iteration dispatches 3 more: 11 of 10. Now it is told 5 (what the other
    runs leave: 10 - 5), dispatches 2 and stops: all 10 are spent and none more. The hold it took ends with
    the launch; the ledger holds nothing open."""
    repo, root, store = interleaved_world(tmp_path, monkeypatch, capsys, width=width)
    out = declare_and_drive(root, capsys)
    spent = dispatched_in(root)
    assert sum(spent.values()) <= BUDGET, spent                  # the sentence
    assert spent == {"n1": BUDGET - 5, "other-1": 5}, spent     # and the work used all that was left
    launcher = CONFIG["launcher"]
    assert ceilings_of(launcher, "n1")[-1] == "5"
    assert out["halt"] == "BUDGET_EXHAUSTED", out                # the budget is what ended it
    assert not [x for x in out["steps"] if x.startswith("REJECTED")], out["steps"]   # a stop at the ceiling is no rejection
    assert BudgetReservations(root).held() == []
    if width > 1:                                                # the sibling had nothing left to be held
        assert not RunStore(root, "n2").state_exists()
        assert _nodes(store)["n2"].lifecycle is Lifecycle.BLOCKED
        assert "dispatch budget cannot be reserved for n2" in _nodes(store)["n2"].note


def test_hoh_project_drive_ends_at_the_budget_verdict_when_the_share_of_a_run_is_spent_and_stays_quiet(
        tmp_path, monkeypatch, capsys):
    """Termination, point by point. (1) bound: no counter is added -- a launch asks the ledger once and the answer
    is a verdict; (2) terminal state: the node is BLOCKED naming the numbers and the drive halts BUDGET_EXHAUSTED
    (a person decides whether the work is worth more budget); (3) a ledger that cannot be used is the same
    verdict (`..._fails_closed_when_the_budget_cannot_be_read`); (4) quiet: a second drive over the terminal
    state types no `hoh run`, writes no run state and no ledger row. The run's share is 3 and it has dispatched
    3: nothing is left for it although the budget as a whole is not spent (`prepare` lets it through)."""
    repo, root, store = interleaved_world(tmp_path, monkeypatch, capsys)
    out = declare_and_drive(root, capsys, "--dispatch-reservation", "3")
    assert out["halt"] == "BUDGET_EXHAUSTED", out
    node = _nodes(store)["n1"]
    assert node.lifecycle is Lifecycle.BLOCKED and "has dispatched 3" in node.note
    assert ceilings_of(CONFIG["launcher"], "n1") == [] and dispatched_in(root)["n1"] == PER_ITERATION
    after_first = _bytes_of(root)
    rc, text, err = hoh(root, capsys, "project", "drive", "p", "--session", "sB")
    assert not [c for c in CONFIG["launcher"].calls if "run" in c], CONFIG["launcher"].calls
    assert _bytes_of(root) == after_first


def _bytes_of(root: Path) -> dict:
    """Every run state and the ledger, byte for byte (not the locks and markers a drive takes and releases)."""
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*.json")) if p.is_file() and p.name in ("state.json", LEDGER)}


LEDGER = "_budget-reservations.json"


def test_the_hold_of_a_resumed_run_counts_what_it_dispatched_before_as_part_of_its_amount(
        tmp_path, monkeypatch, capsys):
    """Catches a hold granted out of `left` while the ledger compares it with the run's TOTAL (the base's
    `take_hold`: amount 2 for a run that has dispatched 3, headroom 0, so a sibling is granted what the run is
    about to spend). The ledger row of a resumed run is `ceiling - what the others spent - what they hold`,
    its own past dispatches inside it."""
    repo, root, store = interleaved_world(tmp_path, monkeypatch, capsys)
    launcher = RealCliLauncher(root, repo, {}, dispatch_budget=BUDGET, iterations=1)
    n1 = store.read_state().node("n1")
    assert launcher.prepare(n1) is None
    assert BudgetReservations(root).held() == []                  # a run that exists took no hold at `prepare`
    assert launcher.take_hold(n1) is True
    [row] = BudgetReservations(root).held()
    assert (row.run_id, row.amount) == ("n1", BUDGET - 5)         # 10 less the other run's 5
    # a sibling asks now: 10 - (3 + 5) spent - (5 - 3) still held for n1 = 0 left
    with pytest.raises(BudgetExhausted):
        launcher.prepare(_with_spec(store, tmp_path, "n3"))


def test_the_qa_reserve_is_kept_from_the_allowance_of_a_run_that_exists_unless_it_is_a_repair_node(
        tmp_path, monkeypatch, capsys):
    """Catches the reserve of P3-23 (`qa_reserve`) not applied to the hold of a run that exists: budget 10, reserve
    2, another run has dispatched 1, n1 has dispatched 3. A developer node is allowed 10 - 1 - 2 = 7 in all; a
    repair node, the one the reserve exists for, 10 - 1 = 9."""
    repo, root, store = interleaved_world(tmp_path, monkeypatch, capsys, other=1)
    n1 = store.read_state().node("n1")
    developer = RealCliLauncher(root, repo, {}, dispatch_budget=BUDGET, qa_reserve=2, iterations=1)
    assert developer.take_hold(n1) is True
    assert [r.amount for r in BudgetReservations(root).held()] == [BUDGET - 1 - 2]
    developer.release_prepared_hold(n1, "next")
    repair = RealCliLauncher(root, repo, {}, dispatch_budget=BUDGET, qa_reserve=2, iterations=1)
    assert repair.take_hold(n1.model_copy(update={"repair_of": "gate-x"})) is True
    assert [r.amount for r in BudgetReservations(root).held()] == [BUDGET - 1]


def _with_spec(store, tmp_path, name):
    from hoh.project import TaskNode

    spec = tmp_path / f"{name}.md"
    spec.write_text(f"# {name}\n", encoding="utf-8")
    return TaskNode(id=name, spec_path=str(spec), writes=[f"{name}.txt"])


def test_a_launch_of_a_run_that_exists_takes_its_hold_and_ends_it_with_the_launch(
        tmp_path, monkeypatch, capsys):
    """Catches the sequential round dispatching a resumed run under no hold (L2 of P3-04 slice 1): `launch` of
    a run that exists and holds nothing takes a hold, types its amount as `--dispatch-ceiling`, and ends the
    hold when the dispatch returns -- the row stays, SETTLED."""
    repo, root, store = interleaved_world(tmp_path, monkeypatch, capsys)
    launcher = RealCliLauncher(root, repo, {}, dispatch_budget=BUDGET, iterations=1)
    n1 = store.read_state().node("n1")
    assert launcher.prepare(n1) is None
    outcome = launcher.launch(n1)
    assert ceilings_of(launcher, "n1") == [str(BUDGET - 5)]
    assert dispatched_in(root)["n1"] == BUDGET - 5
    assert outcome.verdict is not RunVerdict.IN_FLIGHT
    rows = [(r.run_id, r.amount, r.status) for r in BudgetReservations(root).read() if r.run_id == "n1"]
    assert rows[-1] == ("n1", BUDGET - 5, HoldStatus.SETTLED)


def test_a_launch_with_nothing_left_for_a_run_that_exists_is_the_budget_verdict_and_starts_nothing(
        tmp_path, monkeypatch, capsys):
    """Catches a refusal that is an exception out of `launch` (the node is RUNNING and the drive crashes), a
    `hoh run` typed anyway, or a launch that ends a hold that is not its own. The other runs have spent 5 and
    hold 2 more: n1 is allowed 3 in all and has had 3, so nothing is left for it -- while the budget as a whole
    still has 2 unspent (which is why `prepare` lets it through). BUDGET_EXHAUSTED naming the numbers, no `hoh
    run`, the hold the refusal took ended, and the hold of the other run left alone."""
    repo, root, store = interleaved_world(tmp_path, monkeypatch, capsys)
    ledger = BudgetReservations(root)
    ledger.reserve("other-2", ceiling=BUDGET, spent=lambda: {"n1": 3, "other-1": 5}, want=2)
    launcher = RealCliLauncher(root, repo, {}, dispatch_budget=BUDGET, iterations=1)
    n1 = store.read_state().node("n1")
    assert launcher.prepare(n1) is None
    outcome = launcher.launch(n1)
    assert outcome.verdict is RunVerdict.BUDGET_EXHAUSTED
    assert "has dispatched 3" in outcome.detail and "3 dispatches in all" in outcome.detail
    assert ceilings_of(launcher, "n1") == [] and dispatched_in(root)["n1"] == PER_ITERATION
    assert [r.run_id for r in ledger.held()] == ["other-2"]          # n1's refused hold is ended, the other's is not


def _lease_record(root: Path, run_id: str, how: str):
    """The run lease of `run_id` as `hoh run` keeps it, in the state `how` says: LIVE (a holder that is this process),
    RELEASED, UNREADABLE (a record that is not one) or none at all."""
    if how == "none":
        return None
    leases = rundrive._leases(root)
    name = rundrive.lease_name(run_id)
    lease = leases.acquire(name, "hoh-run.test", ttl_seconds=300)
    if how == "released":
        leases.release(lease, reason="run ended")
    elif how == "unreadable":
        leases._path(name).write_text("{not a lease", encoding="utf-8")
    return lease


@pytest.mark.parametrize("how", ["live", "unreadable"])
def test_a_hold_of_a_run_that_is_driven_or_cannot_be_shown_free_is_not_taken_over(
        tmp_path, monkeypatch, capsys, how):
    """Catches `launch` adopting or ending the hold of a run another driver holds (never steal from a live run), and
    a lease record that cannot be read read as "free": n1 holds 2 under a hold this launcher did not take; its lease
    is LIVE, or its record is not one -> BUDGET_EXHAUSTED naming the lease, the hold stays open, nothing typed."""
    repo, root, store = interleaved_world(tmp_path, monkeypatch, capsys)
    ledger = BudgetReservations(root)
    ledger.reserve("n1", ceiling=BUDGET, spent=lambda: {"n1": 3, "other-1": 5}, want=2)
    _lease_record(root, "n1", how)
    launcher = RealCliLauncher(root, repo, {}, dispatch_budget=BUDGET, iterations=1)
    n1 = store.read_state().node("n1")
    outcome = launcher.launch(n1)
    assert outcome.verdict is RunVerdict.BUDGET_EXHAUSTED and "did not take" in outcome.detail
    assert how.upper() in outcome.detail
    assert [(r.run_id, r.amount) for r in ledger.held()] == [("n1", 2)]
    assert ceilings_of(launcher, "n1") == [] and dispatched_in(root)["n1"] == PER_ITERATION


@pytest.mark.parametrize("how", ["as-left", "released-again"])
def test_hoh_project_drive_goes_on_over_a_stale_hold_a_dead_launcher_left(tmp_path, monkeypatch, capsys, how):
    """The case of the review (B1): a launcher took n1's hold (`take_hold`) and its process died before the launch
    ended it. On the first design every later drive halted BUDGET_EXHAUSTED ("did not take") and then
    BLOCKED_DEPENDENCY, and no budget change cleared it. Nobody drives the run (its lease is the RELEASED one the
    earlier `hoh run` left, or a second one released again), so the
    launch ends the stale hold **by its id** (the row kept, SETTLED, the reason naming the lease), takes its own,
    n1 dispatches the 2 that are left, the ledger is empty at the end and a second drive finds the node BLOCKED by
    the budget, nothing more. (`take_hold` itself, the wave's admission, still refuses every hold it did not take:
    `tests/test_wave_controller.py::test_take_hold_never_adopts_or_supersedes_a_hold_it_did_not_take`.)"""
    repo, root, store = interleaved_world(tmp_path, monkeypatch, capsys)
    dead = RealCliLauncher(root, repo, {}, dispatch_budget=BUDGET, iterations=1)
    assert dead.take_hold(store.read_state().node("n1")) is True
    del dead
    _lease_record(root, "n1", "released" if how == "released-again" else "none")
    out = declare_and_drive(root, capsys)
    spent = dispatched_in(root)
    assert spent["n1"] == BUDGET - 5 and sum(spent.values()) <= BUDGET, spent
    assert out["halt"] == "BUDGET_EXHAUSTED" and "did not take" not in out["reason"], out
    rows = [r for r in BudgetReservations(root).read() if r.run_id == "n1"]
    assert [r.status for r in rows][-2:] == [HoldStatus.SETTLED, HoldStatus.SETTLED]
    assert "the launcher that took it is gone" in rows[-2].reason and "RELEASED" in rows[-2].reason
    assert BudgetReservations(root).held() == []
    again = _bytes_of(root)
    rc, text, err = hoh(root, capsys, "project", "drive", "p", "--session", "sB")
    assert _bytes_of(root) == again


@pytest.mark.parametrize("damage", ["ledger-not-json", "ledger-is-a-directory", "another-state-not-json",
                                    "another-state-negative-count"])
def test_a_launch_of_a_run_that_exists_fails_closed_when_the_budget_cannot_be_read(
        tmp_path, monkeypatch, capsys, damage):
    """Fail-open class (odd input): an unreadable ledger or the unreadable state of another run is not a spend
    of zero and not a hold of zero. `launch` of a run that exists returns the budget verdict naming the
    reason, types no `hoh run`, dispatches nothing and writes nothing into the ledger (the damaged file is
    left as it was)."""
    repo, root, store = interleaved_world(tmp_path, monkeypatch, capsys)
    ledger = BudgetReservations(root)
    if damage == "ledger-not-json":
        ledger.path.write_text("{not json", encoding="utf-8")
    elif damage == "ledger-is-a-directory":
        ledger.path.rename(ledger.path.with_name("aside.json"))            # kept, not removed
        ledger.path.mkdir()
    elif damage == "another-state-not-json":
        (root / "other-1" / "state.json").write_text("{not json", encoding="utf-8")
    else:
        (root / "other-1" / "state.json").write_text(json.dumps({"usage": {"dispatches": -1}}), encoding="utf-8")
    before = _shape(ledger.path)
    launcher = RealCliLauncher(root, repo, {}, dispatch_budget=BUDGET, iterations=1)
    n1 = store.read_state().node("n1")
    outcome = launcher.launch(n1)
    assert outcome.verdict is RunVerdict.BUDGET_EXHAUSTED and "cannot be" in outcome.detail
    assert launcher.calls == [] and dispatched_in_ok(root, "n1") == PER_ITERATION
    assert _shape(ledger.path) == before


def _shape(path: Path):
    return path.read_bytes() if path.is_file() else ("directory" if path.is_dir() else None)


def dispatched_in_ok(root: Path, run_id: str) -> int:
    return json.loads((root / run_id / "state.json").read_text(encoding="utf-8"))["usage"]["dispatches"]


def test_a_budgeted_launch_that_holds_nothing_starts_nothing(tmp_path, monkeypatch, capsys):
    """Fail closed (catches a `hoh run` typed with no ceiling when the launcher has a budget but no hold of
    its own behind the launch): a `take_hold` that took nothing (it says False: "already held") leaves a launch
    with nothing to type, and it refuses by name instead of starting an unbounded run."""
    repo, root, store = interleaved_world(tmp_path, monkeypatch, capsys)
    launcher = RealCliLauncher(root, repo, {}, dispatch_budget=BUDGET, iterations=1)
    monkeypatch.setattr(launcher, "take_hold", lambda node: False)
    n1 = store.read_state().node("n1")
    outcome = launcher.launch(n1)
    assert outcome.verdict is RunVerdict.UNDETERMINED and "holds no share" in outcome.detail
    assert launcher.calls == [] and dispatched_in(root)["n1"] == PER_ITERATION


def test_a_launcher_without_a_budget_types_no_ceiling(tmp_path, monkeypatch, capsys):
    """Compatibility (green on the base): no `dispatch_budget`, no hold, no ledger, no `--dispatch-ceiling`."""
    repo, root, store = interleaved_world(tmp_path, monkeypatch, capsys)
    launcher = RealCliLauncher(root, repo, {}, iterations=1)
    n1 = store.read_state().node("n1")
    assert launcher.prepare(n1) is None
    launcher.launch(n1)
    assert ceilings_of(launcher, "n1") == [] and BudgetReservations(root).held() == []
    assert dispatched_in(root)["n1"] == 2 * PER_ITERATION


def test_the_ceiling_of_a_hold_that_could_not_be_ended_is_typed_again_at_the_next_launch(
        tmp_path, monkeypatch, capsys):
    """Catches a ceiling dropped while its hold is still open (the ledger was busy at the end of a launch, the hold
    stays, and the next launch -- which adopts that hold -- must type the same ceiling, not none): n1's wave hold
    is 5; two launches with a ledger that cannot end holds both type `--dispatch-ceiling 5`; with the ledger back,
    the hold is ended by the next launch and the row says SETTLED."""
    from hoh.reservation import ReservationError

    repo, root, store = interleaved_world(tmp_path, monkeypatch, capsys)
    launcher = RealCliLauncher(root, repo, {}, dispatch_budget=BUDGET, iterations=1)
    n1 = store.read_state().node("n1")
    assert launcher.take_hold(n1) is True
    original = BudgetReservations.settle

    def busy(self, run_id, reason):
        raise ReservationError("ledger busy")

    monkeypatch.setattr(BudgetReservations, "settle", busy)
    launcher.launch(n1)
    launcher.launch(n1)
    assert ceilings_of(launcher, "n1") == [str(BUDGET - 5)] * 2
    assert launcher.reservation_notes and [r.run_id for r in BudgetReservations(root).held()] == ["n1"]
    monkeypatch.setattr(BudgetReservations, "settle", original)
    launcher.launch(n1)
    assert BudgetReservations(root).held() == []


def test_a_run_stopped_at_its_told_ceiling_is_the_budget_verdict_not_a_rejection(
        tmp_path, monkeypatch, capsys):
    """The case of the review (B2): the process stops at the ceiling of its hold in the middle of an iteration
    (planner and developer dispatched, QA refused) and, with iterations left, begins the next one and is BLOCKED
    naming the ceiling. Read as `UNDETERMINED`/`REJECTED` the node would be retried and finally abandoned as a run
    that "makes no progress". Here the drive says BUDGET_EXHAUSTED for n1 at once, the node is BLOCKED naming the
    ceiling, the run dispatched exactly the 2 it was held for, and a second drive is byte-quiet."""
    repo, root, store = interleaved_world(tmp_path, monkeypatch, capsys, iterations=3)
    out = declare_and_drive(root, capsys)
    assert out["halt"] == "BUDGET_EXHAUSTED", out
    assert [x for x in out["steps"] if x.startswith("BUDGET_EXHAUSTED")] == ["BUDGET_EXHAUSTED:n1"], out["steps"]
    assert not [x for x in out["steps"] if x.startswith(("REJECTED", "AMBIGUOUS"))], out["steps"]
    node = _nodes(store)["n1"]
    assert node.lifecycle is Lifecycle.BLOCKED and "dispatch ceiling of this launch reached" in node.note
    assert dispatched_in(root) == {"n1": BUDGET - 5, "other-1": 5}
    again = _bytes_of(root)
    hoh(root, capsys, "project", "drive", "p", "--session", "sB")
    assert _bytes_of(root) == again


@pytest.mark.parametrize("which", ["a run that exists", "a new run"])
def test_the_spend_is_read_under_the_ledgers_lock(tmp_path, monkeypatch, capsys, which):
    """Catches a spend read before the lock is taken (the review's M5: a run's spend can grow between the snapshot
    and the grant, and two holds are then granted what only one may have): while the launcher reads the spend for a
    hold, another descriptor cannot take the ledger's exclusive lock -- for a run that exists and for a new run."""
    repo, root, store = interleaved_world(tmp_path, monkeypatch, capsys)
    launcher = RealCliLauncher(root, repo, {}, dispatch_budget=BUDGET, iterations=1)
    held_while_reading: list[bool] = []
    real = launcher._spent_by_run

    def probe():
        fd = os.open(root / "_budget-reservations.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held_while_reading.append(True)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                held_while_reading.append(False)
        finally:
            os.close(fd)
        return real()

    monkeypatch.setattr(launcher, "_spent_by_run", probe)
    node = store.read_state().node("n1") if which == "a run that exists" else _with_spec(store, tmp_path, "n9")
    assert launcher.take_hold(node) is True
    assert held_while_reading == [True]


RESUMED_RACERS, RESUMED_BUDGET, RESUMED_SHARE = 4, 7, 2

_RESUMED_RACER = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
root, home, go, idx = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
from hoh.launcher import HohRunLauncher
from hoh.orchestrator import BudgetExhausted
from hoh.project import TaskNode

launcher = HohRunLauncher(root, home, dispatch_budget=%(budget)d, dispatch_reservation=%(share)d)
node = TaskNode(id="r" + idx, spec_path=os.path.join(home, "spec.md"), writes=["x"], run_id="r" + idx)
while not os.path.exists(go):
    time.sleep(0.005)
try:
    launcher.take_hold(node)
    print(json.dumps({"held": True}))
except BudgetExhausted as exc:
    print(json.dumps({"held": False, "why": str(exc)[:120]}))
"""


def test_concurrent_holds_for_runs_that_exist_through_take_hold_are_granted_no_more_than_the_budget(
        tmp_path):
    """The race of the resumed path (the first design pinned only new runs): four interpreters ask `take_hold` at
    once for four runs that exist and have dispatched 1 each, under a budget of 7 and a share of 2. 3 are left
    (7 - 4), a hold covers one more dispatch each, so three are granted and the fourth, which has nothing left, is
    refused. The holds the ledger holds add up to what is left and no more, in whatever order they ran."""
    root = tmp_path / "runs"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / "spec.md").write_text("# spec\n", encoding="utf-8")
    for i in range(RESUMED_RACERS):
        foreign_run(root, f"r{i}", 1)
    go = tmp_path / "go"
    code = _RESUMED_RACER % {"budget": RESUMED_BUDGET, "share": RESUMED_SHARE}
    procs = [subprocess.Popen([sys.executable, "-I", "-c", code, str(HOH / "src"), str(root), str(home), str(go),
                               str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for i in range(RESUMED_RACERS)]
    go.write_text("go")
    results = []
    for p in procs:
        out, err = p.communicate(timeout=240)
        assert p.returncode == 0, err[-600:]
        results.append(json.loads(out.strip().splitlines()[-1]))
    assert sorted(r["held"] for r in results) == [False, True, True, True], results
    held = BudgetReservations(root).held()
    assert len(held) == 3 and all(r.amount == RESUMED_SHARE for r in held)
    assert RESUMED_RACERS + sum(r.amount - 1 for r in held) == RESUMED_BUDGET     # spent + active_reserved = budget


# --------------------------------------------------------------------------- #
# concurrency, real run states, the launcher's own `prepare`
# --------------------------------------------------------------------------- #

RACE_BUDGET, RACE_SPENT, RACE_SHARE, RACE_PROCESSES = 9, 3, 2, 6

_RACER = r"""
import contextlib, io, json, os, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
root, home, go, idx = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
from hoh import cli
from hoh.launcher import Dispatch, HohRunLauncher
from hoh.orchestrator import BudgetExhausted
from hoh.project import TaskNode
from hoh.store import RunStore
cli._register_trust = lambda *a, **kw: None

class Grants:
    name = "test"
    def may_approve(self, worktree): return True
    def approve(self, worktree, repo):
        from hoh.approval import Approval
        return Approval(granted=True, worktree=worktree, provider="test")

class L(HohRunLauncher):
    def _run_cli(self, argv):
        if "worktree" in argv:
            return Dispatch(0, json.dumps({"path": home}), "")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv[argv.index("--root"):])
        return Dispatch(rc, out.getvalue(), err.getvalue())

spec = os.path.join(home, "spec.md")
open(spec, "w").write("# spec\n")
launcher = L(root, home, approvals=Grants(), dispatch_budget=%(budget)d, dispatch_reservation=%(share)d)
node = TaskNode(id="r" + idx, spec_path=spec, writes=["x"], repair_of="gate-x", run_id="r" + idx)
while not os.path.exists(go):
    time.sleep(0.005)
try:
    refused = launcher.prepare(node)
    cap = RunStore(root, "r" + idx).read_state().budgets.max_dispatches if refused is None else 0
    print(json.dumps({"granted": cap, "refused": refused}))
except BudgetExhausted as exc:
    print(json.dumps({"granted": 0, "refused": str(exc)[:120]}))
"""


def test_concurrent_starts_through_prepare_are_granted_no_more_than_the_budget_less_what_is_spent(
        tmp_path, monkeypatch):
    """The race test of the programme through `HohRunLauncher.prepare` with real run states (the P3-04
    `equivalent` lists it as not built; `tests/test_budget_reservation.py` races `reserve` with an empty
    spend): six interpreters start together under a budget of 9 with a share of 2; a run under the root has
    dispatched 3. 6 are left: three starts are granted 2, three are refused. Catches a spend not counted (the
    ledger would grant 9: four starts and a half), a hold not counted, and a lock that does not serialise."""
    root = tmp_path / "runs"
    root.mkdir()
    foreign_run(root, "pre", RACE_SPENT)
    go = tmp_path / "go"
    code = _RACER % {"budget": RACE_BUDGET, "share": RACE_SHARE}
    procs = []
    for i in range(RACE_PROCESSES):
        home = tmp_path / f"worktree{i}"
        home.mkdir()
        git(home, "init", "-q", "-b", "master")
        (home / "f").write_text("x\n", encoding="utf-8")
        git(home, "add", "-A")
        git(home, "commit", "-qm", "first")
        procs.append(subprocess.Popen(
            [sys.executable, "-I", "-c", code, str(HOH / "src"), str(root), str(home), str(go), str(i)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    go.write_text("go")
    results = []
    for p in procs:
        out, err = p.communicate(timeout=240)
        assert p.returncode == 0, err[-600:]
        results.append(json.loads(out.strip().splitlines()[-1]))
    grants = sorted(r["granted"] for r in results)
    assert grants == [0, 0, 0, 2, 2, 2], results
    held = BudgetReservations(root).held()
    assert sum(r.amount for r in held) == RACE_BUDGET - RACE_SPENT
    # and what each started run was handed as its own ceiling is the hold
    caps = sorted(RunStore(root, f"r{i}").read_state().budgets.max_dispatches
                  for i in range(RACE_PROCESSES) if RunStore(root, f"r{i}").state_exists())
    assert caps == [2, 2, 2]
    assert RACE_SPENT + sum(caps) == RACE_BUDGET


# --------------------------------------------------------------------------- #
# the register: the claim is on the row that owns the mechanism, and says what it does not claim
# --------------------------------------------------------------------------- #

ITEM = "P3.required-invariant.spent-active-reserved-authorized-budget"


def test_the_register_binds_the_invariant_to_p3_04_with_evidence_that_exists_in_this_tree():
    """Catches the register edit being left out (the exit condition then stays 'nothing here measures it'), a
    claim by a row that is not the ledger's, and a claim note whose named nodes do not exist: the item is
    bound to P3-04 alone (`build_plan.gate_bindings`), the note names the nodes that pin it (each one a test of
    this tree), says what is NOT claimed and that the row is PARTIAL."""
    import ast
    import re

    spec = importlib.util.spec_from_file_location("build_plan_inv1", HOH / "tools" / "build_plan.py")
    bp = importlib.util.module_from_spec(spec)
    sys.modules["build_plan_inv1"] = bp
    spec.loader.exec_module(bp)
    requirements = bp._register_requirements()
    assert bp.gate_bindings(bp._scope()["items"], requirements)[ITEM] == ["P3-04"]
    row = next(r for r in requirements if r["id"] == "P3-04")
    assert row["also_covers"] == [ITEM] and set(row["also_covers_why"]) == {ITEM}
    note = row["also_covers_why"][ITEM]
    assert "NOT claimed" in note and "stays PARTIAL" in note
    named, current = [], None
    for path, name in re.findall(r"(tests/test_\w+\.py)?::(test_\w+)", note):
        current = path or current
        named.append((current, name))
    assert len(named) >= 5 and {p for p, _ in named} == {
        "tests/test_budget_invariant.py", "tests/test_budget_reservation.py"}
    for path, name in named:
        tree = ast.parse((HOH / path).read_text(encoding="utf-8"))
        assert name in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}, (path, name)
