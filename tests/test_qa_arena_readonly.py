"""QA's candidate arena is read-only, and any touch there is a capability violation.

Run `20261003t0141z-supdrift2`: QA created `.qa_probe` in the candidate arena
it was reviewing and moved it out again, and nothing in HoH noticed. The arena
was writable, it was left out of QA's witness, and the after-QA check looked
at the repository, not at the arena.

What this file pins (spec `dogfood/qa-arena-write/spec.md`, v7 + 7a to 7e, with the round-10 and
round-12 minors folded in):

* **Seal.** For QA's dispatch every write bit under the candidate arena is
  cleared; running the candidate still works.
* **Frozen witness.** Contents plus each entry's mode, size, mtime and ctime,
  read on every way out of the dispatch: an answer, a final `DispatchError`,
  a crash. A write QA undid is still a violation.
* **Kept seals.** A pane kept open (`WaitingForApproval`, `PaneNotClean`) or an
  interrupt keeps the arena sealed; every later verification compares it and
  never unseals it; only `release_open_seals()` -- after the tabs closed, with
  nothing unconfirmed and the QA agent gone -- lifts it, comparing once more.
* **Identity.** Every `chmod` is checked against the inode the plan recorded,
  so a swapped symlink, a hard link or a forged ledger line cannot make the
  machinery change a mode outside the arena.
* **Hard links.** A file with more than one name is never sealed and never
  changed: one name may lie outside the arena, and the count cannot tell.
* **Open seals gate.** The first `planned` line of an open seal is its plan,
  the first usable `sealed` line its digest, and a `touched` line is a record
  that never becomes the baseline; a touch is reported at every comparison.
  Every way a `hoh run` ends compares the seals it leaves, and while a seal is
  open nothing is merged or delivered.
* **The block holds.** A capability violation sets a sticky
  `blocked_kind = "capability_violation"`: `hoh run` exits 1, `hoh deliver`
  and `hoh resume` refuse, the launcher never merges or auto-unblocks, and the
  acceptance record refuses the checkpoint -- until a person runs `hoh
  unblock`.
* **The hold survives the contract and every way back (Revision 7e).** A
  contract's STOP or ABORT cancels a held run but keeps the kind; `hoh resume`
  refuses a held run in every condition but PAUSED (which returns to BLOCKED
  with no reconciliation); `hoh deliver` reads the hold first. The release
  compares the arena with its seal digest, so a touch no dispatch's witness
  covers is still found. K17 to K19 run every routing a block can get against
  every way back.

The fakes model agents. Nothing here starts Herdr or a provider.

Rule H (Revision 7d): no criterion command names a host path. A product run
puts `tmp_path` under `/home`, where the runner refuses such a command, so
whatever a criterion needs from the host is baked into a file the candidate
ships (`_hermetic` checks every plan).

Not to be run as root: root ignores the cleared write bits, and the tests that
expect a refused write fail there on purpose. Tests that end QA with a kept
pane or an interrupt leave a sealed arena in `tmp_path`; pytest's own cleanup
restores write bits, so nothing is removed by hand.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path

import pytest

from hoh import capability as cap
from hoh import cli, governance, stages
from hoh.contracts import Budgets, Condition, Role, RunState, Stage
from hoh.controller import (
    SEAL_LEDGER,
    Controller,
    DispatchError,
    PaneNotClean,
    WaitingForApproval,
)
from hoh.store import RunStore

ROOT = Path(__file__).resolve().parent.parent

#: A run root that does not exist, so it holds no seal ledger (nothing is written there).
NO_LEDGER_ROOT = Path("/nonexistent-hoh-run-root")


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "project"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.email", "t@example.invalid")
    git(r, "config", "user.name", "T")
    (r / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (r / "tool.sh").write_text("#!/bin/sh\necho tool\n", encoding="utf-8")
    (r / "tool.sh").chmod(0o755)
    (r / "pkg").mkdir()
    (r / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def spec(tmp_path: Path) -> Path:
    p = tmp_path / "spec.md"
    p.write_text("# Spec\nadd(a,b) must add correctly.\n", encoding="utf-8")
    return p


def _hermetic(command: str, *host_paths) -> str:
    """Rule H (Revision 7d): a criterion's command never names a host path.

    Runs the product's own `assert_stays_in_arena` on the command -- under a
    product run `tmp_path` is under `/home`, which the runner refuses with
    `ArenaEscape` -- and asserts that none of `host_paths` occurs in it, so the
    property holds under any basetemp and not only under `/home`. Whatever a
    criterion needs from the host is baked into a file the candidate ships.
    """
    from hoh.runner import assert_stays_in_arena

    assert_stays_in_arena(command)
    for path in host_paths:
        assert str(path) not in command, (
            f"the command names the host path {str(path)!r}: {command!r}")
    return command


def plan_for(state: RunState) -> dict:
    """A new criterion per iteration, so every iteration demonstrates one."""
    base = state.last_accepted_candidate or state.base_candidate
    n = state.iteration
    return {
        "run_id": state.run_id, "iteration": n,
        "base_candidate_id": base.candidate_id if base else f"{state.run_id}-base",
        "spec_digest": state.spec_digest, "objective": "keep add() correct",
        "targets": ["app.py"], "preserve": [],
        "acceptance_checks": [{
            "check_id": f"K{n}", "description": "the increment is there",
            "command": _hermetic(f"test -f step{n}.txt",
                                 Path(state.repo_path).parent),
            "expect_exit": 0, "preserves": False,
        }],
        "out_of_scope": [], "evidence_refs": [],
        "repair_only": False, "repair_reason": None,
    }


def qa_pass(state: RunState) -> dict:
    return {
        "verdicts": [{
            "check_id": f"K{i}", "outcome": "PASS",
            "receipt_id": f"{state.run_id}-i{state.iteration}-a{state.attempt}-K{i}",
            "reproduction": "test -f", "note": "green",
        } for i in range(1, state.iteration + 1)],
        "open_gaps": [], "summary": "all good",
    }


def develop(r: Path) -> None:
    n = 1 + len(list(r.glob("step*.txt")))
    (r / f"step{n}.txt").write_text("ok\n", encoding="utf-8")


class Agents:
    """Three fake agents. QA's behaviour is `qa(state, arena)` for its next
    dispatch, taken from `qa_script` in order (then `qa_default`); it may act
    on the arena and may raise, as a real pane's dispatch can end."""

    def __init__(self, repo: Path, *, qa_script=(), qa_default=None,
                 planner_effect=None):
        self.repo = repo
        self.qa_script = list(qa_script)
        self.qa_default = qa_default
        self.planner_effect = planner_effect
        self.ctrl: Controller | None = None
        self.calls: list[Role] = []
        self.prompts: dict[Role, str] = {}
        self.qa_arenas: list[Path] = []

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str:
        self.calls.append(role)
        self.prompts[role] = prompt
        if role is Role.PLANNER:
            if self.planner_effect:
                self.planner_effect(state)
            return json.dumps(plan_for(state))
        if role is Role.DEVELOPER:
            develop(self.repo)
            return "done"
        arena = self.ctrl._qa_arena
        self.qa_arenas.append(arena)
        effect = self.qa_script.pop(0) if self.qa_script else self.qa_default
        if effect is not None:
            effect(state, arena)
        return json.dumps(qa_pass(state))

    def endpoint_evidence(self, role: Role) -> str:
        return f"fake:{role.value}"


class ClosingAgents(Agents):
    """`_ClosedTabs`: agents whose tabs close cleanly at the end of `hoh run`
    (`close_own` returns `["t1"]`, nothing unconfirmed) and that cannot say
    whether a role is gone (no `role_gone`)."""

    unconfirmed = ()

    def close_own(self) -> list[str]:
        return ["t1"]


class PresentAgents(Agents):
    """Agents whose QA agent may still be present in the multiplexer."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.qa_present = True

    def role_gone(self, role: Role, state: RunState) -> bool:
        return not (role is Role.QA and self.qa_present)


def build(tmp_path: Path, repo: Path, spec: Path, agents: Agents):
    store = RunStore(tmp_path / "runs", "r1")
    state = Controller.new_state(
        run_id="r1", repo_path=repo, project_name="project", spec_path=spec,
        budgets=Budgets(max_iterations=6),
    )
    store.write_state(state)
    ctrl = Controller(store, agents, spec_path=spec)
    agents.ctrl = ctrl
    return ctrl, state, store


def fresh_controller(store: RunStore, spec: Path, agents: Agents) -> Controller:
    """What a new `hoh run` process builds: nothing carried over in memory."""
    ctrl = Controller(store, agents, spec_path=spec)
    agents.ctrl = ctrl
    return ctrl


def modes(root: Path) -> dict[str, int]:
    out = {".": stat.S_IMODE(os.lstat(root).st_mode)}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            if not p.is_symlink():
                out[str(p.relative_to(root))] = stat.S_IMODE(os.lstat(p).st_mode)
    return out


def writable(root: Path) -> list[str]:
    """Entries under `root` that keep any write bit."""
    return [rel for rel, m in modes(root).items() if m & 0o222]


def ledger(store: RunStore) -> list[dict]:
    path = store.dir / SEAL_LEDGER
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def events_for(store: RunStore, arena: Path) -> list[dict]:
    return [e for e in ledger(store) if e.get("arena") == str(arena)]


def planned_modes(store: RunStore, arena: Path) -> dict[str, int]:
    [planned] = [e for e in events_for(store, arena) if e["event"] == "planned"]
    return {rel: stat.S_IMODE(entry[0]) for rel, entry in planned["entries"].items()}


@contextmanager
def opened(path: Path, mode: int = 0o755):
    """Give a sealed directory its write bit for one act, then take it back --
    what an agent with a shell can do to a tree it owns."""
    before = stat.S_IMODE(os.lstat(path).st_mode)
    os.chmod(path, before | 0o200)
    try:
        yield
    finally:
        os.chmod(path, before)


def undone_write(state: RunState, arena: Path) -> None:
    """The supdrift2 shape: create `.qa_probe`, move it out again."""
    outside = arena.parent.parent / "qa-probe-outside"
    with opened(arena):
        (arena / ".qa_probe").write_text("probe\n", encoding="utf-8")
        os.rename(arena / ".qa_probe", outside.with_name(f"{outside.name}-{os.getpid()}"))


def raising(exc: BaseException, before=None):
    def effect(state: RunState, arena: Path) -> None:
        if before is not None:
            before(state, arena)
        raise exc
    return effect


def state_on_disk(store: RunStore) -> RunState:
    return store.read_state()


def deliver(tmp_path: Path, *, approve: bool = False) -> int:
    return cli.cmd_deliver(Namespace(root=tmp_path / "runs", run_id="r1",
                                     into="main", approve=approve))


def _launcher_verdict(state: RunState, before=None, exit_code: int = 1):
    from hoh.launcher import Dispatch, HohRunLauncher

    launcher = HohRunLauncher.__new__(HohRunLauncher)
    # A root that holds no seal ledger: this helper probes the state gate only.
    launcher.root = NO_LEDGER_ROOT
    return launcher._verdict(state, before, Dispatch(exit_code, "", ""))


ROUTINGS = ["STOP_PROGRAM", "ABORT_BRANCH", "PARK_BRANCH", "COLLECT_FOR_DIGEST", "ASK_NOW"]
CANCELLING = ("STOP_PROGRAM", "ABORT_BRANCH")


def _contract(routing: str):
    """The real contract whose routing of a block is `routing` (pinned per case)."""
    from hoh.autonomy import (
        AutonomyContract,
        AutonomyMode,
        Escalation,
        Horizon,
        NeedsYou,
        compile_profile,
    )

    unattended = dict(
        scope=("src/hoh/",),
        horizon=Horizon(max_wallclock_seconds=3600, review_after_seconds=900),
        budgets=Budgets(max_dispatches=10, max_wallclock_seconds=3600))
    if routing in CANCELLING:
        return AutonomyContract(
            contract_id=f"qaw8-{routing.lower()}", mode=AutonomyMode.UNATTENDED_LONG_HORIZON,
            needs_you=NeedsYou.OFF, escalation={"decision": Escalation[routing]},
            note="stop the whole programme is intended here", **unattended)
    if routing == "PARK_BRANCH":
        return compile_profile(AutonomyMode.UNATTENDED_LONG_HORIZON,
                               contract_id="qaw8-park", **unattended)
    if routing == "COLLECT_FOR_DIGEST":
        return compile_profile(AutonomyMode.PLAN_AUTONOMOUS, contract_id="qaw8-collect",
                               horizon=Horizon(review_after_seconds=300))
    assert routing == "ASK_NOW", routing
    return compile_profile(AutonomyMode.SUPERVISED, contract_id="qaw8-ask")


def _k10_scenario(tmp_path, repo, spec, *, touch: bool = True, agents_cls=None,
                  routing: str | None = None):
    """Iteration 1's QA waits for an approval (its arena stays sealed),
    iteration 2 is accepted, then -- optionally -- the iteration-1 pane
    `chmod`s, creates and moves out `.qa_probe`, and `chmod`s back. With
    `routing`, the run is under that contract from the start."""
    agents = (agents_cls or ClosingAgents)(
        repo, qa_script=[raising(WaitingForApproval("dialog open"))])
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    if routing is not None:
        # The run is under a contract from the start, so the violation's own
        # block is routed by it (Revision 7e, K17).
        governance.bind(store.dir, contract=_contract(routing))
    first = ctrl.run_iteration(state)
    assert first.waiting_for_approval and not first.accepted
    second = ctrl.run_iteration(state)
    assert second.accepted, second.reason
    arena1 = agents.qa_arenas[0]
    assert not writable(arena1), "the kept arena is still sealed after iteration 2"
    if touch:
        undone_write(state, arena1)
    return agents, ctrl, state, store, arena1


# --------------------------------------------------------------------------- #
# K1 -- the seal
# --------------------------------------------------------------------------- #


def test_a_qa_probe_in_the_candidate_arena_is_refused(tmp_path, repo, spec):
    """The supdrift2 write, without the chmod: refused by the file system."""
    seen: dict = {}

    def probe(state: RunState, arena: Path) -> None:
        seen["writable"] = writable(arena)
        try:
            (arena / ".qa_probe").write_text("probe\n", encoding="utf-8")
        except PermissionError as exc:
            seen["refused"] = exc
        try:
            (arena / "app.py").write_text("tampered\n", encoding="utf-8")
        except PermissionError as exc:
            seen["refused_edit"] = exc

    agents = Agents(repo, qa_default=probe)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    out = ctrl.run_iteration(state)

    assert seen["writable"] == [], "every write bit is cleared for QA's dispatch"
    assert isinstance(seen.get("refused"), PermissionError)
    assert isinstance(seen.get("refused_edit"), PermissionError)
    arena = agents.qa_arenas[0]
    assert not (arena / ".qa_probe").exists()
    assert out.accepted, out.reason
    # Unsealed afterwards, to exactly the modes it had.
    assert modes(arena) == planned_modes(store, arena)
    assert modes(arena)["tool.sh"] == 0o755
    assert [e["event"] for e in events_for(store, arena)] == [
        "planned", "sealed", "releasing", "released"]


def test_running_the_candidate_from_a_sealed_arena_still_works(tmp_path, repo, spec):
    """Read and execute bits stay: the reviewer can still run the thing."""
    ran: list = []

    def run_it(state: RunState, arena: Path) -> None:
        assert writable(arena) == []
        out = subprocess.run(["python3", "-c", "import app, pkg.mod; print(app.add(2, 3))"],
                             cwd=arena, capture_output=True, text=True, check=True)
        ran.append(out.stdout.strip())
        tool = subprocess.run(["./tool.sh"], cwd=arena, capture_output=True, text=True,
                              check=True)
        ran.append(tool.stdout.strip())

    agents = Agents(repo, qa_default=run_it)
    ctrl, state, _ = build(tmp_path, repo, spec, agents)
    out = ctrl.run_iteration(state)
    assert ran == ["5", "tool"]
    assert out.accepted, out.reason


# --------------------------------------------------------------------------- #
# K2 -- the frozen witness
# --------------------------------------------------------------------------- #


def _assert_violation(out, state: RunState, arena: Path | None = None) -> None:
    assert not out.accepted
    assert "capability violation" in out.reason, out.reason
    assert state.condition is Condition.BLOCKED
    assert state.blocked_kind == stages.CAPABILITY_VIOLATION
    if arena is not None:
        assert str(arena) in out.reason, out.reason


def test_a_write_undone_inside_the_qa_dispatch_is_a_capability_violation(
        tmp_path, repo, spec):
    agents = Agents(repo, qa_default=undone_write)
    ctrl, state, _ = build(tmp_path, repo, spec, agents)
    out = ctrl.run_iteration(state)
    _assert_violation(out, state, agents.qa_arenas[0])
    assert "a frozen tree was touched during qa's dispatch" in out.reason


def test_a_content_write_with_the_timestamp_set_back_is_caught(tmp_path, repo, spec):
    """Contents restored, mtime set back, mode set back: only the change time
    is left to tell, and it is enough."""
    def rewrite(state: RunState, arena: Path) -> None:
        f = arena / "app.py"
        st = os.stat(f)
        original = f.read_bytes()
        os.chmod(f, 0o644)
        f.write_bytes(b"def add(a, b):\n    return 42\n")
        f.write_bytes(original)
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.chmod(f, stat.S_IMODE(st.st_mode))
        after = os.stat(f)
        assert (after.st_mtime_ns, after.st_size, after.st_mode) == (
            st.st_mtime_ns, st.st_size, st.st_mode)

    agents = Agents(repo, qa_default=rewrite)
    ctrl, state, _ = build(tmp_path, repo, spec, agents)
    out = ctrl.run_iteration(state)
    _assert_violation(out, state, agents.qa_arenas[0])


def test_a_chmod_and_back_right_after_the_seal_is_still_seen(tmp_path):
    """The seal settles before it returns, so even the first act after it
    lands in a later clock tick than the last entry it sealed."""
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "f.txt").write_text("x\n", encoding="utf-8")
    (root / "g.txt").write_text("y\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    plan = cap.seal_read_only(root)
    probe = elsewhere / "probe"
    probe.write_text("p\n", encoding="utf-8")
    last = list(plan)[-1]
    assert list(plan)[0] == ".", "the root is sealed first"
    assert os.lstat(probe).st_ctime_ns > os.lstat(root / last).st_ctime_ns, (
        "the seal returned inside the clock tick of its last chmod")

    witness = cap.CapabilityWitness.take(cap.qa_policy(
        arena=tmp_path, answer=tmp_path / "answer.json", frozen=(root,)))
    sealed_mode = stat.S_IMODE(os.lstat(root / last).st_mode)
    os.chmod(root / last, sealed_mode | 0o200)
    os.chmod(root / last, sealed_mode)
    assert witness.violations(), "a chmod and back right after the seal went unseen"
    assert cap.unseal(root, plan) == []


def test_reading_a_frozen_tree_is_not_a_touch(tmp_path, repo, spec):
    """The positive control: reading, listing and running is QA's work."""
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "f.py").write_text("print('hi')\n", encoding="utf-8")
    plan = cap.seal_read_only(root)
    witness = cap.CapabilityWitness.take(cap.qa_policy(
        arena=tmp_path, answer=tmp_path / "answer.json", frozen=(root,)))
    for p in root.rglob("*"):
        if p.is_file():
            p.read_bytes()
        os.listdir(p if p.is_dir() else p.parent)
    subprocess.run([sys.executable, "sub/f.py"], cwd=root, check=True,
                   capture_output=True)
    assert witness.violations() == []
    assert cap.unseal(root, plan) == []

    agents = Agents(repo, qa_default=lambda s, a: [p.read_bytes()
                                                   for p in a.rglob("*") if p.is_file()])
    ctrl, state, _ = build(tmp_path, repo, spec, agents)
    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason


# --------------------------------------------------------------------------- #
# K3 -- the policy, the prompt, the ledger
# --------------------------------------------------------------------------- #


def test_qas_policy_freezes_exactly_the_arena_it_reviews(tmp_path, repo, spec):
    seen: dict = {}

    def look(state: RunState, arena: Path) -> None:
        seen["qa"] = agents.ctrl._policy_for(Role.QA, state)
        seen["arena"] = arena
        seen["dev"] = agents.ctrl._policy_for(Role.DEVELOPER, state)
        seen["planner"] = agents.ctrl._policy_for(Role.PLANNER, state)

    agents = Agents(repo, qa_default=look)
    ctrl, state, _ = build(tmp_path, repo, spec, agents)
    assert ctrl.run_iteration(state).accepted
    assert seen["qa"].frozen == (seen["arena"],)
    assert seen["arena"] not in seen["qa"].protected
    assert seen["dev"].frozen == () and seen["planner"].frozen == ()
    assert "freezes 1 tree(s)" in seen["qa"].summary()


def test_the_qa_prompt_says_the_candidate_is_read_only_and_witnessed(tmp_path, repo, spec):
    agents = Agents(repo)
    ctrl, state, _ = build(tmp_path, repo, spec, agents)
    ctrl.run_iteration(state)
    prompt = agents.prompts[Role.QA]
    restrictions = prompt.split("/restrictions", 1)[1].split("/run", 1)[0]
    assert "read-only" in restrictions
    assert "witnessed" in restrictions
    assert "capability violation" in restrictions


def test_the_seal_ledger_is_protected_for_every_role(tmp_path, repo, spec):
    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    for role in Role:
        assert store.dir / SEAL_LEDGER in ctrl._policy_for(role, state).protected, role

    def forge(state: RunState) -> None:
        with open(agents2.ctrl.store.dir / SEAL_LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "released", "arena": "/elsewhere"}) + "\n")

    agents2 = Agents(repo, planner_effect=forge)
    ctrl2, state2, store2 = build(tmp_path / "second", repo, spec, agents2)
    out = ctrl2.run_iteration(state2)
    assert not out.accepted
    assert "capability violation" in out.reason
    assert SEAL_LEDGER in out.reason


# --------------------------------------------------------------------------- #
# K16 -- Revision 7d: the retry of a stalled send runs under the same seal
# --------------------------------------------------------------------------- #


def _stalled(seen: list[dict] | None = None, *, act=None):
    """QA's send that ends in a transient error with a back-off, as
    `agent_prompt_stalled` does; it records what the arena looks like to the
    pane at that moment, and may act on it first."""
    def effect(state: RunState, arena: Path) -> None:
        if seen is not None:
            seen.append(_view_of_a_sealed_arena(arena))
        if act is not None:
            act(state, arena)
        raise DispatchError("agent_prompt_stalled", transient=True, retry_after=0.01)
    return effect


def _view_of_a_sealed_arena(arena: Path) -> dict:
    refused = False
    try:
        (arena / ".probe-write").write_text("x\n", encoding="utf-8")
    except PermissionError:
        refused = True
    return {"writable": writable(arena), "refused": refused}


def test_a_stalled_send_is_sent_again_under_the_same_seal(tmp_path, repo, spec):
    seen: list[dict] = []

    def second(state: RunState, arena: Path) -> None:
        seen.append(_view_of_a_sealed_arena(arena))

    agents = Agents(repo, qa_script=[_stalled(seen), second])
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason
    assert agents.calls.count(Role.QA) == 2
    assert seen == [{"writable": [], "refused": True}] * 2, (
        "a send of QA's dispatch saw a write bit in the arena")
    arena = agents.qa_arenas[0]
    assert agents.qa_arenas == [arena, arena]
    events = [e["event"] for e in events_for(store, arena)]
    assert events.count("planned") == 1 and events.count("sealed") == 1
    assert events[-1] == "released"
    assert writable(arena) != [], "the arena is writable again once QA is done"
    assert not (arena / ".probe-write").exists()


def test_a_pane_that_acts_while_its_send_is_stalled_is_caught(tmp_path, repo, spec):
    agents = Agents(repo, qa_script=[_stalled(act=undone_write)])
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    out = ctrl.run_iteration(state)
    _assert_violation(out, state, agents.qa_arenas[0])
    assert agents.calls.count(Role.QA) == 1, "the prompt was sent again over a touched arena"
    assert "touched during qa's dispatch" in out.reason, out.reason


def test_a_touch_between_the_retry_check_and_the_rebaseline_is_caught(
        tmp_path, repo, spec, monkeypatch):
    """qaw7-b M2, qa7d-b: the pane acts inside the retry's budget charge, after
    the witness has checked the arena and before `state.json` is re-baselined.
    The re-baseline must leave the frozen arena's baseline alone, so the touch
    is found by the *dispatch's own witness* (its text), not only by the
    comparison at the release, which finds the same touch later."""
    agents = Agents(repo, qa_script=[_stalled()])
    ctrl, state, _ = build(tmp_path, repo, spec, agents)
    real = ctrl._budget_or_refusal
    acted: list[bool] = []

    def charge(st: RunState) -> None:
        if agents.calls.count(Role.QA) == 1 and not acted:
            acted.append(True)
            undone_write(st, ctrl._qa_arena)
        return real(st)

    monkeypatch.setattr(ctrl, "_budget_or_refusal", charge)
    out = ctrl.run_iteration(state)
    assert acted, "the retry's charge never ran"
    _assert_violation(out, state, agents.qa_arenas[0])
    assert "touched during qa's dispatch" in out.reason, out.reason
    assert "outside every dispatch's witness" not in out.reason


# --------------------------------------------------------------------------- #
# K6 -- every way out of QA's dispatch is witnessed
# --------------------------------------------------------------------------- #


def test_an_undone_write_then_a_dispatch_error_is_a_violation(tmp_path, repo, spec):
    agents = Agents(repo, qa_default=raising(DispatchError("pane gone"), undone_write))
    ctrl, state, _ = build(tmp_path, repo, spec, agents)
    out = ctrl.run_iteration(state)
    _assert_violation(out, state, agents.qa_arenas[0])
    assert "NO QA VERDICT" not in out.reason
    assert "touched during qa's dispatch" in out.reason, out.reason


def test_an_undone_write_then_an_approval_wait_is_a_violation(tmp_path, repo, spec):
    agents = Agents(repo, qa_default=raising(WaitingForApproval("dialog"), undone_write))
    ctrl, state, _ = build(tmp_path, repo, spec, agents)
    out = ctrl.run_iteration(state)
    _assert_violation(out, state, agents.qa_arenas[0])
    assert "touched during qa's dispatch" in out.reason, out.reason


def test_an_undone_write_then_a_crash_is_a_violation(tmp_path, repo, spec):
    class Boom(RuntimeError):
        pass

    agents = Agents(repo, qa_default=raising(Boom("dispatcher crashed"), undone_write))
    ctrl, state, _ = build(tmp_path, repo, spec, agents)
    out = ctrl.run_iteration(state)
    _assert_violation(out, state, agents.qa_arenas[0])
    assert "touched during qa's dispatch" in out.reason, out.reason


def test_an_undone_write_then_an_interrupt_keeps_the_arena_sealed_and_is_caught(
        tmp_path, repo, spec):
    agents = Agents(repo, qa_script=[raising(KeyboardInterrupt(), undone_write)])
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    with pytest.raises(KeyboardInterrupt):
        ctrl.run_iteration(state)
    arena = agents.qa_arenas[0]
    assert writable(arena) == [], "an interrupt keeps the seal"
    last = events_for(store, arena)[-1]
    assert last["event"] == "kept" and "KeyboardInterrupt" in last["why"]

    nxt = fresh_controller(store, spec, Agents(repo))
    on_disk = state_on_disk(store)
    out = nxt.run_iteration(on_disk)
    assert not out.accepted
    assert "capability violation" in out.reason and str(arena) in out.reason
    assert on_disk.blocked_kind == stages.CAPABILITY_VIOLATION
    assert "touched while it stayed sealed after QA's dispatch" in out.reason, out.reason


# --------------------------------------------------------------------------- #
# K7 -- kept seals, the ledger, a failing or interrupted seal
# --------------------------------------------------------------------------- #


def test_a_kept_open_pane_keeps_the_arena_sealed_until_the_panes_close(
        tmp_path, repo, spec):
    agents = Agents(repo, qa_script=[raising(WaitingForApproval("dialog open"))])
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    first = ctrl.run_iteration(state)
    assert first.waiting_for_approval
    arena1 = agents.qa_arenas[0]
    assert writable(arena1) == [], "sealed between the iterations"
    assert events_for(store, arena1)[-1]["event"] == "kept"

    second = ctrl.run_iteration(state)
    assert second.accepted, second.reason
    assert writable(arena1) == [], "still sealed after iteration 2"
    assert writable(agents.qa_arenas[1]), "iteration 2's own arena was unsealed"

    result = ctrl.release_open_seals()
    assert result["released"] == [str(arena1)] and result["touched"] == []
    assert modes(arena1) == planned_modes(store, arena1)
    assert modes(arena1)["tool.sh"] == 0o755
    last = events_for(store, arena1)[-1]
    assert last["event"] == "released" and last["on_close"] is True


def test_a_touch_while_kept_sealed_is_a_violation_at_the_next_check(tmp_path, repo, spec):
    agents = Agents(repo, qa_script=[raising(WaitingForApproval("dialog open"))])
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    ctrl.run_iteration(state)
    arena1 = agents.qa_arenas[0]
    undone_write(state, arena1)                       # the pane, between iterations

    second = ctrl.run_iteration(state)
    assert not second.accepted
    assert "capability violation" in second.reason and str(arena1) in second.reason
    assert state.condition is Condition.BLOCKED
    assert writable(arena1) == [], "the check compares, it never unseals"
    assert events_for(store, arena1)[-1]["event"] == "touched"

    # The recorded `touched` line is a record, never a baseline: the release
    # reports the touch again instead of treating it as already dealt with.
    result = ctrl.release_open_seals()
    assert result["touched"] == [str(arena1)], "the touch is reported again"
    assert result["released"] == [str(arena1)]


def test_a_touch_after_the_next_check_is_caught_when_the_panes_close(tmp_path, repo, spec):
    agents, ctrl, state, store, arena1 = _k10_scenario(tmp_path, repo, spec)
    result = ctrl.release_open_seals()
    assert result["touched"] == [str(arena1)]
    on_disk = state_on_disk(store)
    assert on_disk.condition is Condition.BLOCKED
    assert "capability violation" in (on_disk.blocked_reason or "")


class _RecordingController:
    def __init__(self, order: list):
        self.order = order

    def release_open_seals(self) -> dict:
        self.order.append("release")
        return {"released": [], "touched": []}


def test_seals_are_lifted_only_after_the_tabs_close():
    order: list = []

    class FailingClose:
        unconfirmed = ()

        def close_own(self):
            order.append("close")
            raise RuntimeError("herdr gone")

    assert cli._close_own_and_release(FailingClose(), _RecordingController(order)) == ([], [])
    assert order == ["close"], "a failed close releases nothing"

    order.clear()

    class Closing:
        unconfirmed = ()

        def close_own(self):
            order.append("close")
            return ["t1"]

    assert cli._close_own_and_release(Closing(), _RecordingController(order)) == (["t1"], [])
    assert order == ["close", "release"]

    order.clear()

    class HalfClosed:
        def __init__(self):
            self.unconfirmed = ()

        def close_own(self):
            order.append("close")
            self.unconfirmed = ("qa-tab",)
            return []

    assert cli._close_own_and_release(HalfClosed(), _RecordingController(order)) == ([], [])
    assert order == ["close"], "an unconfirmed tab keeps the seals"


def _fail_nth_seal_chmod(monkeypatch, n: int, exc: BaseException):
    """Make the n-th `chmod` through `/proc/self/fd` raise -- once. Only the
    seal's own calls are counted; the rollback goes through."""
    real = os.chmod
    count = {"n": 0}

    def chmod(path, mode, *a, **k):
        if str(path).startswith("/proc/self/fd/"):
            count["n"] += 1
            if count["n"] == n:
                raise exc
        return real(path, mode, *a, **k)

    monkeypatch.setattr(os, "chmod", chmod)
    return count


def test_a_process_ending_mid_seal_leaves_a_record_that_restores_it(
        tmp_path, repo, spec, monkeypatch):
    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    arenas_before = set(store.arenas_dir.iterdir())
    _fail_nth_seal_chmod(monkeypatch, 3, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        ctrl.run_iteration(state)
    monkeypatch.undo()
    assert Role.QA not in agents.calls
    [planned] = [e for e in ledger(store) if e["event"] == "planned"]
    arena = Path(planned["arena"])
    assert arena in set(store.arenas_dir.iterdir()) - arenas_before
    assert [e["event"] for e in events_for(store, arena)] == ["planned"]
    assert writable(arena) != [] and set(writable(arena)) != set(modes(arena)), (
        "the fixture should leave the arena half sealed")

    result = fresh_controller(store, spec, Agents(repo)).release_open_seals()
    assert result["released"] == [str(arena)] and result["touched"] == []
    assert modes(arena) == planned_modes(store, arena)


def test_a_failed_seal_is_rolled_back_and_blocks_before_qa_starts(
        tmp_path, repo, spec, monkeypatch):
    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    _fail_nth_seal_chmod(monkeypatch, 3, PermissionError(1, "Operation not permitted"))
    out = ctrl.run_iteration(state)
    monkeypatch.undo()
    assert Role.QA not in agents.calls, "QA never starts on an unsealed arena"
    assert not out.accepted
    assert state.condition is Condition.BLOCKED
    assert "could not be sealed" in (state.blocked_reason or "")
    assert state.blocked_kind != stages.CAPABILITY_VIOLATION
    [planned] = [e for e in ledger(store) if e["event"] == "planned"]
    arena = Path(planned["arena"])
    assert modes(arena) == planned_modes(store, arena), "rolled back"
    last = events_for(store, arena)[-1]
    assert last["event"] == "released" and last["failed"]


# --------------------------------------------------------------------------- #
# K8 -- the seal machinery never changes a mode outside the arena
# --------------------------------------------------------------------------- #


def test_a_symlinked_directory_does_not_lead_the_unseal_out_of_the_arena(
        tmp_path, repo, spec):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "mod.py").write_text("SECRET = 1\n", encoding="utf-8")
    os.chmod(foreign / "mod.py", 0o600)
    os.chmod(foreign, 0o700)

    def swap(state: RunState, arena: Path) -> None:
        with opened(arena):
            with opened(arena / "pkg"):
                pass
            os.rename(arena / "pkg", arena / "pkg-moved")
            os.symlink(foreign, arena / "pkg")

    agents = Agents(repo, qa_default=swap)
    ctrl, state, _ = build(tmp_path, repo, spec, agents)
    out = ctrl.run_iteration(state)
    _assert_violation(out, state)
    arena = agents.qa_arenas[0]
    assert stat.S_IMODE(os.lstat(foreign).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(foreign / "mod.py").st_mode) == 0o600
    assert (arena / "pkg").is_symlink()


def test_a_replaced_entry_is_left_alone_by_the_unseal(tmp_path, repo, spec):
    foreign = tmp_path / "foreign.txt"
    foreign.write_text("not the candidate's\n", encoding="utf-8")
    os.chmod(foreign, 0o600)

    def replace(state: RunState, arena: Path) -> None:
        with opened(arena):
            os.rename(arena / "app.py", arena / "app.py.moved")
            os.link(foreign, arena / "app.py")

    agents = Agents(repo, qa_default=replace)
    ctrl, state, _ = build(tmp_path, repo, spec, agents)
    out = ctrl.run_iteration(state)
    _assert_violation(out, state)
    assert stat.S_IMODE(os.lstat(foreign).st_mode) == 0o600


def test_a_forged_ledger_line_cannot_aim_the_unseal_outside_the_arenas(
        tmp_path, repo, spec):
    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    assert ctrl.run_iteration(state).accepted
    foreign = tmp_path / "foreign-dir"
    foreign.mkdir()
    os.chmod(foreign, 0o600)
    st = os.lstat(foreign)
    with open(store.dir / SEAL_LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "planned", "arena": str(foreign),
                             "entries": {".": [stat.S_IFDIR | 0o777, st.st_dev,
                                               st.st_ino]}}) + "\n")
    try:
        result = ctrl.release_open_seals()
        assert "not an arena of this run" in result.get("refused", "")
        assert result["released"] == []
        assert stat.S_IMODE(os.lstat(foreign).st_mode) == 0o600
        on_disk = state_on_disk(store)
        assert on_disk.condition is Condition.BLOCKED
        assert on_disk.blocked_kind == stages.CAPABILITY_VIOLATION
    finally:
        os.chmod(foreign, 0o700)      # the test's own directory, for pytest's cleanup


# --------------------------------------------------------------------------- #
# K9 -- the release gate
# --------------------------------------------------------------------------- #


def test_a_still_present_qa_agent_keeps_the_seal_on_release(tmp_path, repo, spec):
    agents = PresentAgents(repo, qa_script=[raising(WaitingForApproval("dialog open"))])
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    ctrl.run_iteration(state)
    arena = agents.qa_arenas[0]

    result = ctrl.release_open_seals()
    assert result["kept"] == [str(arena)] and result["released"] == []
    assert writable(arena) == []

    agents.qa_present = False
    result = ctrl.release_open_seals()
    assert result["released"] == [str(arena)]
    assert stat.S_IMODE(os.lstat(arena / "tool.sh").st_mode) == 0o755


def test_herdr_role_gone_looks_up_the_agent_by_its_name(monkeypatch):
    from hoh import dispatchers, herdr as herdr_mod

    d = object.__new__(dispatchers.HerdrDispatcher)
    st = RunState(run_id="r1", repo_path="/x/repo", project_name="p",
                  spec_path="/x/spec.md", spec_digest="d", policy_digest="d",
                  profile_digest="d")
    name = d._agent_name(Role.QA, st)

    def snap(value):
        monkeypatch.setattr(herdr_mod, "snapshot", lambda: value)

    snap({"agents": [{"name": "someone-else"}, {"name": name}]})
    assert d.role_gone(Role.QA, st) is False
    snap({"agents": [{"name": "someone-else"}]})
    assert d.role_gone(Role.QA, st) is True

    def broken():
        raise RuntimeError("socket gone")
    monkeypatch.setattr(herdr_mod, "snapshot", broken)
    assert d.role_gone(Role.QA, st) is False

    snap({"agents": [{"name": "someone-else"}, {"agent_name": name}]})
    assert d.role_gone(Role.QA, st) is False, "a nameless row could be the agent"
    snap({"agents": "not a list"})
    assert d.role_gone(Role.QA, st) is False


def test_an_unseal_cut_short_is_finished_not_reported(tmp_path, repo, spec, monkeypatch):
    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    real = ctrl._seal_released

    def cut_short(st, arena, plan, **kw):
        # The unseal gets as far as restoring the modes -- which moves every
        # change time -- and the process ends before `released` is written.
        monkeypatch.setattr(ctrl, "_seal_released", real)
        cap.unseal(arena, plan)
        raise KeyboardInterrupt

    monkeypatch.setattr(ctrl, "_seal_released", cut_short)
    with pytest.raises(KeyboardInterrupt):
        ctrl.run_iteration(state)
    arena = agents.qa_arenas[0]
    assert events_for(store, arena)[-1]["event"] == "releasing"

    out = fresh_controller(store, spec, Agents(repo)).run_iteration(state_on_disk(store))
    assert "capability violation" not in out.reason, out.reason
    assert events_for(store, arena)[-1]["event"] == "released"


def test_end_of_run_closes_and_releases_before_it_prunes():
    order: list = []

    class Store:
        @contextmanager
        def lock(self):
            yield

        def prune(self):
            order.append("prune")
            return {"arenas": []}

    class Tabs:
        unconfirmed = ()

        def close_own(self):
            order.append("close")
            return []

    cli._end_of_run(Store(), Tabs(), _RecordingController(order), False, "r1")
    assert order == ["close", "release", "prune"]
    order.clear()
    cli._end_of_run(Store(), Tabs(), _RecordingController(order), True, "r1")
    assert order == ["prune"]


# --------------------------------------------------------------------------- #
# K10 -- a run blocked by a capability violation fails, is not delivered,
# and is never merged
# --------------------------------------------------------------------------- #


def _finish(store, agents, ctrl, state):
    return cli._finish_run(store, agents, ctrl, state, waiting_for_approval=False,
                           run_id="r1", ran=2)


def test_a_touch_found_at_release_fails_the_run_and_blocks_delivery(
        tmp_path, repo, spec, capsys):
    agents, ctrl, state, store, arena1 = _k10_scenario(tmp_path, repo, spec)
    assert state.condition is Condition.ACTIVE      # the stale in-memory state
    payload, rc = _finish(store, agents, ctrl, state)
    assert rc != 0
    assert payload["condition"] == "BLOCKED"
    assert payload["capability_violation"] == [str(arena1)]
    assert state_on_disk(store).stage is Stage.CHECKPOINTED

    capsys.readouterr()
    assert deliver(tmp_path) == 2
    assert "capability violation" in capsys.readouterr().err


def test_an_untouched_release_finishes_the_run_normally(tmp_path, repo, spec):
    agents, ctrl, state, store, arena1 = _k10_scenario(tmp_path, repo, spec, touch=False)
    payload, rc = _finish(store, agents, ctrl, state)
    assert rc == 0
    assert payload["condition"] == "ACTIVE"
    assert "capability_violation" not in payload
    last = events_for(store, arena1)[-1]
    assert last["event"] == "released" and last["on_close"] is True
    assert last["touched"] is False
    assert payload["rotated"]["arenas"], (
        "the prune ran after the release, so it could move the unsealed arena")


def test_a_touch_found_at_release_is_never_merged(tmp_path, repo, spec):
    from hoh.orchestrator import RunVerdict

    agents, ctrl, state, store, _ = _k10_scenario(tmp_path, repo, spec)
    _, rc = _finish(store, agents, ctrl, state)
    assert rc == 1
    on_disk = state_on_disk(store)
    assert on_disk.blocked_kind == stages.CAPABILITY_VIOLATION
    for before in (None, "r1-i0-base"):
        for code in (1, 0):
            outcome = _launcher_verdict(on_disk, before, exit_code=code)
            assert outcome.verdict is not RunVerdict.ACCEPTED
            assert outcome.verdict is RunVerdict.UNDETERMINED
            assert "capability violation" in outcome.detail


def test_a_later_block_keeps_the_violation_until_a_person_unblocks(
        tmp_path, repo, spec, capsys):
    from hoh.orchestrator import RunVerdict

    agents, ctrl, state, store, _ = _k10_scenario(tmp_path, repo, spec)
    _finish(store, agents, ctrl, state)
    with store.lock():
        fresh = store.read_state()
        governance.block_under_contract(fresh, "iteration aborted: something else", None)
        store.write_state(fresh)
    on_disk = state_on_disk(store)
    assert on_disk.blocked_kind == stages.CAPABILITY_VIOLATION
    assert not on_disk.blocked_reason.startswith("capability violation")
    assert _launcher_verdict(on_disk).verdict is not RunVerdict.ACCEPTED
    capsys.readouterr()
    assert deliver(tmp_path) == 2
    assert "capability violation" in capsys.readouterr().err

    # Control: once a person has looked, the durable acceptance counts again.
    with store.lock():
        fresh = store.read_state()
        stages.unblock(fresh, "investigated: the pane was mine")
        store.write_state(fresh)
    on_disk = state_on_disk(store)
    assert on_disk.blocked_kind is None
    assert _launcher_verdict(on_disk).verdict is RunVerdict.ACCEPTED


def test_a_violation_after_a_checkpoint_is_never_merged(tmp_path, repo, spec):
    from hoh.orchestrator import RunVerdict

    calls = {"n": 0}

    def write_checks(state: RunState, arena: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            (agents.ctrl.store.dir / "checks.json").write_text("{}\n", encoding="utf-8")

    agents = Agents(repo, qa_default=write_checks)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    assert ctrl.run_iteration(state).accepted
    second = ctrl.run_iteration(state)
    assert "capability violation" in second.reason
    on_disk = state_on_disk(store)
    assert on_disk.last_accepted_candidate is not None
    assert on_disk.blocked_kind == stages.CAPABILITY_VIOLATION
    assert _launcher_verdict(on_disk).verdict is not RunVerdict.ACCEPTED


# --------------------------------------------------------------------------- #
# K11 -- the kind holds in every condition and on every path out of the block
# --------------------------------------------------------------------------- #


def test_a_paused_violation_is_still_never_merged_or_delivered(
        tmp_path, repo, spec, capsys):
    from hoh.orchestrator import RunVerdict

    agents, ctrl, state, store, _ = _k10_scenario(tmp_path, repo, spec)
    _finish(store, agents, ctrl, state)
    runs = tmp_path / "runs"
    assert cli.cmd_pause(Namespace(root=runs, run_id="r1", reason="look later")) == 0
    on_disk = state_on_disk(store)
    assert on_disk.condition is Condition.PAUSED
    assert _launcher_verdict(on_disk).verdict is not RunVerdict.ACCEPTED
    capsys.readouterr()
    assert deliver(tmp_path, approve=True) == 2
    assert "capability violation" in capsys.readouterr().err

    # Iteration 1's QA task was never retired, so the resume may also find
    # the state unclear and block it again; either way the hold survives.
    cli.cmd_resume(Namespace(root=runs, run_id="r1", force=False, reason=None))
    on_disk = state_on_disk(store)
    assert (on_disk.condition, on_disk.blocked_kind) == (
        Condition.BLOCKED, stages.CAPABILITY_VIOLATION)
    assert _launcher_verdict(on_disk).verdict is not RunVerdict.ACCEPTED


def _bare_state() -> RunState:
    return RunState(run_id="r1", repo_path="/x/repo", project_name="p",
                    spec_path="/x/spec.md", spec_digest="d", policy_digest="d",
                    profile_digest="d")


def test_pause_and_resume_never_leave_a_stale_violation_kind():
    st = _bare_state()
    st.blocked_kind = stages.CAPABILITY_VIOLATION
    stages.block(st, "capability violation: qa touched the arena")
    stages.pause(st, "later")
    stages.resume(st)
    assert (st.condition, st.blocked_kind) == (Condition.BLOCKED, stages.CAPABILITY_VIOLATION)

    stages.unblock(st, "investigated")
    assert (st.condition, st.blocked_kind) == (Condition.ACTIVE, None)

    stages.block(st, "usage limit reached, resets in 2h")
    assert st.blocked_kind == "usage_limit" and st.retry_after


def test_the_launcher_never_auto_unblocks_a_violation(tmp_path, monkeypatch):
    from hoh.launcher import HohRunLauncher

    store = RunStore(tmp_path / "runs", "r1")
    st = _bare_state()
    st.blocked_kind = stages.CAPABILITY_VIOLATION
    stages.block(st, "qa waits for an approval in its pane")
    store.write_state(st)

    calls: list = []
    launcher = HohRunLauncher.__new__(HohRunLauncher)
    monkeypatch.setattr(launcher, "_approve", lambda *a, **k: calls.append("approve"),
                        raising=False)
    monkeypatch.setattr(launcher, "_run_cli", lambda *a, **k: calls.append("cli"),
                        raising=False)
    monkeypatch.setattr(launcher, "_worktree_for", lambda *a, **k: calls.append("wt"),
                        raising=False)
    said = launcher._ensure_runnable("r1", "branch", store)
    assert said and "capability violation" in said and "hoh unblock" in said
    assert calls == []
    assert store.read_state().blocked_kind == stages.CAPABILITY_VIOLATION


def _rewrite(store: RunStore, **fields) -> RunState:
    with store.lock():
        st = store.read_state()
        for k, v in fields.items():
            setattr(st, k, v)
        store.write_state(st)
    return store.read_state()


def test_a_legacy_violation_block_without_the_kind_is_recognised(
        tmp_path, repo, spec, capsys):
    from hoh.orchestrator import RunVerdict

    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    assert ctrl.run_iteration(state).accepted
    legacy = _rewrite(store, condition=Condition.BLOCKED, blocked_kind="collected_for_digest",
                      blocked_reason="capability violation: qa: a protected tree changed",
                      stop_reason="capability violation: qa: a protected tree changed")
    assert stages.violation_unresolved(legacy)
    assert _launcher_verdict(legacy).verdict is not RunVerdict.ACCEPTED
    capsys.readouterr()
    assert deliver(tmp_path) == 2
    assert "capability violation" in capsys.readouterr().err

    parked = _rewrite(store, blocked_reason="autonomy: parked for the digest",
                      stop_reason="autonomy: parked for the digest")
    assert not stages.violation_unresolved(parked)
    assert _launcher_verdict(parked).verdict is RunVerdict.ACCEPTED


def _acceptance_tool():
    spec_ = importlib.util.spec_from_file_location(
        "acc_probe_qaw", ROOT / "tools" / "acceptance_record.py")
    mod = importlib.util.module_from_spec(spec_)
    sys.modules["acc_probe_qaw"] = mod
    spec_.loader.exec_module(mod)
    return mod


def test_the_acceptance_record_refuses_a_checkpoint_held_by_a_violation():
    acc = _acceptance_tool()

    def held_problems(state: dict) -> list[str]:
        record = {"stage": "CHECKPOINTED", "accepted_candidate": "r1-i1"}
        if acc.held_by_violation(state):
            record["held_by_capability_violation"] = {
                k: state.get(k) for k in ("condition", "blocked_kind", "blocked_reason")}
        return [p for p in acc.satisfied(record) if "capability violation" in p]

    assert held_problems({"condition": "ACTIVE", "blocked_kind": None}) == []
    for condition in ("BLOCKED", "PAUSED"):
        assert held_problems({"condition": condition,
                              "blocked_kind": "capability_violation",
                              "blocked_reason": "iteration aborted: x"})
    assert held_problems({"condition": "BLOCKED", "blocked_kind": None,
                          "blocked_reason": "capability violation: qa touched it"})


def test_the_kind_gates_in_every_condition_whatever_the_reason_says(
        tmp_path, repo, spec):
    from hoh.orchestrator import RunVerdict

    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    assert ctrl.run_iteration(state).accepted
    for verb in (stages.pause, stages.cancel):
        st = store.read_state()
        st.blocked_kind = stages.CAPABILITY_VIOLATION
        stages.block(st, "capability violation: qa touched the arena")
        governance.block_under_contract(st, "iteration aborted: something else", None)
        verb(st, "an operator's verb")
        assert not (st.blocked_reason or "").startswith("capability violation")
        assert stages.violation_unresolved(st), verb.__name__
        assert _launcher_verdict(st).verdict is not RunVerdict.ACCEPTED, verb.__name__


# --------------------------------------------------------------------------- #
# K12 -- `hoh resume` never releases a hold; legacy holds are adopted
# --------------------------------------------------------------------------- #


def test_hoh_resume_refuses_a_violation_held_run_even_with_force(
        tmp_path, repo, spec, capsys):
    from hoh.orchestrator import RunVerdict

    agents, ctrl, state, store, _ = _k10_scenario(tmp_path, repo, spec)
    _finish(store, agents, ctrl, state)
    seq = state_on_disk(store).write_seq
    capsys.readouterr()
    rc = cli.cmd_resume(Namespace(root=tmp_path / "runs", run_id="r1", force=True,
                                  reason="go on"))
    assert rc == 2
    assert "hoh unblock" in capsys.readouterr().err
    on_disk = state_on_disk(store)
    assert on_disk.condition is Condition.BLOCKED
    assert on_disk.blocked_kind == stages.CAPABILITY_VIOLATION
    assert on_disk.write_seq == seq, "a refusal writes nothing"
    assert _launcher_verdict(on_disk).verdict is not RunVerdict.ACCEPTED


def test_a_legacy_violation_is_not_resumed_past(tmp_path, repo, spec, capsys):
    from hoh.orchestrator import RunVerdict

    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    assert ctrl.run_iteration(state).accepted
    reason = "capability violation: planner: a protected tree changed"
    _rewrite(store, condition=Condition.BLOCKED, blocked_kind="collected_for_digest",
             blocked_reason=reason, stop_reason=reason)
    runs = tmp_path / "runs"

    assert cli.cmd_pause(Namespace(root=runs, run_id="r1", reason="later")) == 0
    with store.lock():
        st = store.read_state()
        stages.resume(st)
        store.write_state(st)
    on_disk = state_on_disk(store)
    assert (on_disk.condition, on_disk.blocked_kind) == (
        Condition.BLOCKED, stages.CAPABILITY_VIOLATION)
    assert _launcher_verdict(on_disk).verdict is not RunVerdict.ACCEPTED

    capsys.readouterr()
    assert cli.cmd_resume(Namespace(root=runs, run_id="r1", force=False, reason=None)) == 2
    assert state_on_disk(store).condition is Condition.BLOCKED

    st = _bare_state()
    st.condition, st.blocked_kind, st.blocked_reason = (
        Condition.BLOCKED, None, reason)
    stages.block(st, "resume unclear: t1 no endpoint")
    assert st.blocked_kind == stages.CAPABILITY_VIOLATION
    assert st.blocked_reason == "resume unclear: t1 no endpoint"


# --------------------------------------------------------------------------- #
# K13 -- a pane that is not clean keeps the arena sealed (Revision 7b)
# --------------------------------------------------------------------------- #


def test_a_pane_that_is_not_clean_keeps_the_arena_sealed(tmp_path, repo, spec):
    agents = Agents(repo, qa_default=raising(PaneNotClean("input box not empty")))
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    with pytest.raises(PaneNotClean):
        ctrl.run_iteration(state)
    arena = agents.qa_arenas[0]
    assert writable(arena) == []
    last = events_for(store, arena)[-1]
    assert last["event"] == "kept" and "kept open" in last["why"]
    assert re.search(r"QA pane is kept open", last["why"])


# --------------------------------------------------------------------------- #
# Revision 7c helpers
# --------------------------------------------------------------------------- #


class UnconfirmedAgents(ClosingAgents):
    """`close_own` returns, but a tab is not confirmed closed."""

    unconfirmed = ("qa-tab",)


class FailingCloseAgents(ClosingAgents):
    """`close_own` raises: the multiplexer is gone."""

    def close_own(self) -> list[str]:
        raise RuntimeError("herdr is gone")


def forge(store: RunStore, **record) -> None:
    """A line a pane appended to the seal ledger between two dispatches."""
    with open(store.dir / SEAL_LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def foreign_file(tmp_path: Path, name: str = "foreign.bin", mode: int = 0o600) -> Path:
    path = tmp_path / name
    path.write_text("not the candidate's\n", encoding="utf-8")
    os.chmod(path, mode)
    return path


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _real_verdict(tmp_path: Path, repo: Path, before: str | None = None):
    """The launcher's verdict through its real entry point, `evaluate`."""
    from hoh.launcher import HohRunLauncher
    from hoh.project import TaskNode

    launcher = HohRunLauncher(tmp_path / "runs", repo)
    return launcher.evaluate(TaskNode(id="r1", run_id="r1", accepted_before=before))


def _finish_open(store, agents, ctrl, state):
    """`_finish_run` with the tabs kept open (`waiting_for_approval`)."""
    return cli._finish_run(store, agents, ctrl, state, waiting_for_approval=True,
                           run_id="r1", ran=2)


# --------------------------------------------------------------------------- #
# K14 -- Revision 7c: hard links, forged plans, open seals, CANCELLED
# --------------------------------------------------------------------------- #


def test_a_touch_while_the_tabs_stay_open_fails_the_run_at_its_end(
        tmp_path, repo, spec, capsys):
    from hoh.orchestrator import RunVerdict

    agents, ctrl, state, store, arena1 = _k10_scenario(tmp_path, repo, spec)
    payload, rc = _finish_open(store, agents, ctrl, state)
    assert rc == 1
    assert payload["condition"] == "BLOCKED"
    assert payload["capability_violation"] == [str(arena1)]
    assert writable(arena1) == [], "the seal stays: the tabs are still open"
    on_disk = state_on_disk(store)
    assert on_disk.blocked_kind == stages.CAPABILITY_VIOLATION
    assert _real_verdict(tmp_path, repo).verdict is not RunVerdict.ACCEPTED
    capsys.readouterr()
    assert deliver(tmp_path) == 2
    assert "capability violation" in capsys.readouterr().err


def test_an_open_seal_holds_merge_and_delivery_until_it_is_released(
        tmp_path, repo, spec, capsys):
    from hoh.orchestrator import RunVerdict

    agents, ctrl, state, store, arena1 = _k10_scenario(tmp_path, repo, spec, touch=False)
    payload, rc = _finish_open(store, agents, ctrl, state)
    assert rc == 0 and payload["condition"] == "ACTIVE"
    assert writable(arena1) == []

    held = _real_verdict(tmp_path, repo)
    assert held.verdict is RunVerdict.UNDETERMINED
    assert "still open" in held.detail
    capsys.readouterr()
    assert deliver(tmp_path) == 2
    assert "still open" in capsys.readouterr().err

    result = ctrl.release_open_seals()
    assert result["released"] == [str(arena1)] and result["touched"] == []
    assert _real_verdict(tmp_path, repo).verdict is RunVerdict.ACCEPTED


def test_a_present_qa_agent_does_not_hide_a_touch(tmp_path, repo, spec):
    from hoh.orchestrator import RunVerdict

    agents, ctrl, state, store, arena1 = _k10_scenario(
        tmp_path, repo, spec, agents_cls=PresentAgents)
    result = ctrl.release_open_seals()
    assert result["kept"] == [str(arena1)]
    assert result["touched"] == [str(arena1)]
    assert result["released"] == []
    assert writable(arena1) == [], "still sealed: the agent may still act"
    on_disk = state_on_disk(store)
    assert on_disk.condition is Condition.BLOCKED
    assert on_disk.blocked_kind == stages.CAPABILITY_VIOLATION
    assert _launcher_verdict(on_disk).verdict is not RunVerdict.ACCEPTED


def test_a_forged_plan_for_a_hard_linked_foreign_file_changes_nothing(
        tmp_path, repo, spec):
    agents, ctrl, state, store, arena1 = _k10_scenario(tmp_path, repo, spec, touch=False)
    foreign = foreign_file(tmp_path)
    real_modes = planned_modes(store, arena1)
    with opened(arena1):
        os.link(foreign, arena1 / "evil")                 # the pane's hard link
    st_f, st_root = os.lstat(foreign), os.lstat(arena1)
    forge(store, event="planned", arena=str(arena1), entries={
        ".": [stat.S_IFDIR | 0o777, st_root.st_dev, st_root.st_ino],
        "evil": [stat.S_IFREG | 0o666, st_f.st_dev, st_f.st_ino]})

    result = ctrl.release_open_seals()
    assert mode_of(foreign) == 0o600, "the foreign file was not chmodded"
    assert result["touched"] == [str(arena1)], "the touch is reported"
    assert mode_of(arena1) == real_modes["."] != 0o777, (
        "only the first `planned` line is the plan: the forged one restored nothing")


def test_a_forged_seal_of_a_released_arena_cannot_chmod_a_linked_foreign_file(
        tmp_path, repo, spec):
    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    assert ctrl.run_iteration(state).accepted
    arena1 = agents.qa_arenas[0]
    assert events_for(store, arena1)[-1]["event"] == "released"
    foreign = foreign_file(tmp_path)
    os.link(foreign, arena1 / "evil")                     # the arena is writable again
    st_f = os.lstat(foreign)
    own = events_for(store, arena1)[0]            # the pane copies the arena's own iteration and attempt
    forge(store, event="planned", arena=str(arena1), iteration=own["iteration"],
          attempt=own["attempt"], entries={
              "evil": [stat.S_IFREG | 0o666, st_f.st_dev, st_f.st_ino]})
    forge(store, event="sealed", arena=str(arena1), frozen="forged")

    result = ctrl.release_open_seals()
    assert result["released"] == [str(arena1)]
    assert mode_of(foreign) == 0o600, "a linked inode is left alone"


def test_a_hard_linked_file_is_never_sealed(tmp_path):
    arena = tmp_path / "arena"
    arena.mkdir()
    (arena / "a.py").write_text("A = 1\n", encoding="utf-8")
    foreign = foreign_file(tmp_path, mode=0o644)
    os.link(foreign, arena / "linked.py")
    before, root_ctime = modes(arena), os.lstat(arena).st_ctime_ns

    with pytest.raises(cap.SealFailed) as err:
        cap.seal_read_only(arena)
    assert "nothing changed" in str(err.value) and "hard link" in str(err.value)
    assert mode_of(foreign) == 0o644
    assert modes(arena) == before
    assert os.lstat(arena).st_ctime_ns == root_ctime


def test_a_check_that_plants_a_hard_link_blocks_the_run_before_qa(tmp_path, repo, spec):
    foreign_dir = tmp_path / "foreign"
    foreign_dir.mkdir()
    victim = foreign_dir / "state.json"
    victim.write_text("{}\n", encoding="utf-8")
    os.chmod(victim, 0o644)

    class PlantingAgents(Agents):
        """The candidate ships `plant.py`; the criterion that runs in the
        arena links a foreign file into it. The victim is named inside the
        candidate's own file, as a real candidate ships its own code, and
        never in the command (Revision 7d, rule H)."""

        def dispatch(self, role, prompt, *, state):
            out = super().dispatch(role, prompt, state=state)
            if role is Role.PLANNER:
                plan = json.loads(out)
                plan["acceptance_checks"][0]["command"] = _hermetic(
                    "test -f step1.txt && python3 plant.py", tmp_path)
                return json.dumps(plan)
            if role is Role.DEVELOPER:
                (self.repo / "plant.py").write_text(
                    f"import os\nos.link({str(victim)!r}, 'planted.json')\n",
                    encoding="utf-8")
            return out

    agents = PlantingAgents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    out = ctrl.run_iteration(state)
    assert not out.accepted
    assert "could not be sealed" in out.reason and "hard link" in out.reason
    assert Role.QA not in agents.calls, "QA was not started on an arena it could write"
    assert mode_of(victim) == 0o644


@pytest.mark.parametrize("kind", ["contract_stop", "collected_for_digest"])
def test_a_cancelled_or_contract_stopped_violation_is_still_held(
        tmp_path, repo, spec, capsys, kind):
    from hoh.orchestrator import RunVerdict

    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    assert ctrl.run_iteration(state).accepted
    reason = "capability violation: qa: a protected tree changed"
    held = _rewrite(store, condition=Condition.CANCELLED, blocked_kind=kind,
                    blocked_reason=reason, stop_reason=reason)
    assert stages.violation_unresolved(held)
    assert _launcher_verdict(held).verdict is not RunVerdict.ACCEPTED
    capsys.readouterr()
    assert deliver(tmp_path) == 2
    assert "capability violation" in capsys.readouterr().err

    acc = _acceptance_tool()
    for condition in ("CANCELLED", "FAILED"):
        assert acc.held_by_violation({"condition": condition, "blocked_kind": kind,
                                      "blocked_reason": reason})
    assert not acc.held_by_violation({"condition": "CANCELLED", "blocked_kind": kind,
                                      "blocked_reason": "autonomy: parked for the digest"})


def test_a_replaced_entry_with_one_link_is_left_alone(tmp_path, repo, spec):
    """The identity check alone: the replacement has one name, so the link
    count does not refuse it."""
    foreign = foreign_file(tmp_path)

    def replace(state: RunState, arena: Path) -> None:
        with opened(arena):
            os.rename(arena / "app.py", arena / "app.py.moved")
            os.rename(foreign, arena / "app.py")          # moved in: one name

    agents = Agents(repo, qa_default=replace)
    ctrl, state, _ = build(tmp_path, repo, spec, agents)
    out = ctrl.run_iteration(state)
    _assert_violation(out, state)
    assert mode_of(agents.qa_arenas[0] / "app.py") == 0o600


# --------------------------------------------------------------------------- #
# K15 -- Revision 7c, round-10 minors
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tabs_open", [True, False], ids=["tabs-open", "tabs-closed"])
@pytest.mark.parametrize("line", ["sealed", "touched"])
def test_a_forged_sealed_or_touched_line_cannot_hide_a_touch(
        tmp_path, repo, spec, line, tabs_open):
    from hoh.orchestrator import RunVerdict

    agents, ctrl, state, store, arena1 = _k10_scenario(tmp_path, repo, spec)
    # The pane, after its touch, appends a line carrying the touched tree's digest.
    forge(store, event=line, arena=str(arena1), frozen=cap.frozen_digest(arena1))
    payload, rc = cli._finish_run(store, agents, ctrl, state,
                                  waiting_for_approval=tabs_open, run_id="r1", ran=2)
    assert rc == 1
    assert payload["condition"] == "BLOCKED"
    assert payload["capability_violation"] == [str(arena1)]
    assert _real_verdict(tmp_path, repo).verdict is not RunVerdict.ACCEPTED


def test_a_forged_sealed_line_with_no_usable_digest_is_ignored(tmp_path, repo, spec):
    # The parse alone: the first usable `sealed` digest is the seal's.
    from hoh.controller import arena_name

    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    _rewrite(store, arena_secret="s3cret")        # what `run_iteration` sets at the start of a run
    arena = store.arenas_dir / arena_name("s3cret", 1, 1)
    entries = {".": [stat.S_IFDIR | 0o755, 1, 2]}
    forge(store, event="planned", arena=str(arena), iteration=1, attempt=1, entries=entries)
    forge(store, event="sealed", arena=str(arena), frozen="")
    forge(store, event="sealed", arena=str(arena), frozen=None)
    forge(store, event="sealed", arena=str(arena))
    forge(store, event="sealed", arena=str(arena), frozen="FIRST")
    forge(store, event="sealed", arena=str(arena), frozen="LATER")
    forge(store, event="touched", arena=str(arena), frozen="TOUCHED")
    seal = ctrl._open_seals()[str(arena)]
    assert seal["frozen"] == "FIRST", "an empty, missing or later digest cannot replace it"
    assert seal["touched"] == "TOUCHED", "a touched line is a record"

    # And on a real seal: a later line with the touched tree's digest changes nothing.
    (tmp_path / "second").mkdir()
    agents2 = Agents(repo, qa_script=[raising(WaitingForApproval("dialog open"))])
    ctrl2, state2, store2 = build(tmp_path / "second", repo, spec, agents2)
    ctrl2.run_iteration(state2)
    arena1 = agents2.qa_arenas[0]
    real = ctrl2._open_seals()[str(arena1)]["frozen"]
    undone_write(state2, arena1)
    forge(store2, event="sealed", arena=str(arena1), frozen=cap.frozen_digest(arena1))
    assert ctrl2._open_seals()[str(arena1)]["frozen"] == real
    assert ctrl2.compare_open_seals()["touched"] == [str(arena1)]


def test_a_touch_stays_reported_and_the_ledger_records_each_state_once(
        tmp_path, repo, spec):
    agents, ctrl, state, store, arena1 = _k10_scenario(tmp_path, repo, spec)
    for _ in range(3):
        assert ctrl.compare_open_seals()["touched"] == [str(arena1)]
    touched = [e for e in events_for(store, arena1) if e["event"] == "touched"]
    assert len(touched) == 1, "one line per distinct state, not one per comparison"
    assert ctrl.release_open_seals()["touched"] == [str(arena1)]


def test_a_check_reports_a_touch_every_time_and_records_it_once(tmp_path, repo, spec):
    from hoh.capability import CapabilityViolation

    agents = Agents(repo, qa_script=[raising(WaitingForApproval("dialog open"))])
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    ctrl.run_iteration(state)
    arena1 = agents.qa_arenas[0]
    undone_write(state, arena1)
    for _ in range(3):
        with pytest.raises(CapabilityViolation) as err:
            ctrl._check_open_seals(state)
        assert str(arena1) in str(err.value)
    touched = [e for e in events_for(store, arena1) if e["event"] == "touched"]
    assert len(touched) == 1
    assert writable(arena1) == [], "the check never unseals"


def test_the_launcher_verdict_reads_the_ledger_when_the_flag_is_left_out(
        tmp_path, repo, spec):
    from hoh.launcher import Dispatch, HohRunLauncher
    from hoh.orchestrator import RunVerdict

    agents, ctrl, state, store, arena1 = _k10_scenario(tmp_path, repo, spec, touch=False)
    launcher = HohRunLauncher(tmp_path / "runs", repo)
    on_disk = state_on_disk(store)

    for out in (launcher._verdict(on_disk, None, Dispatch(0, "", "")),
                launcher._verdict_from_baseline(on_disk, None)):
        assert out.verdict is RunVerdict.UNDETERMINED and "still open" in out.detail

    assert ctrl.release_open_seals()["released"] == [str(arena1)]
    on_disk = state_on_disk(store)
    for out in (launcher._verdict(on_disk, None, Dispatch(0, "", "")),
                launcher._verdict_from_baseline(on_disk, None)):
        assert out.verdict is RunVerdict.ACCEPTED

    # A run id that names no directory of the root cannot be looked up: it holds.
    stray = on_disk.model_copy(update={"run_id": "../elsewhere"})
    assert launcher._verdict(stray, None, Dispatch(0, "", "")).verdict \
        is RunVerdict.UNDETERMINED
    # An explicit answer is honoured.
    assert launcher._verdict(on_disk, None, Dispatch(0, "", ""),
                             open_seals=True).verdict is RunVerdict.UNDETERMINED


def test_an_unreadable_seal_ledger_counts_as_an_open_seal(tmp_path, repo, spec, capsys):
    from hoh.controller import open_seal_arenas
    from hoh.orchestrator import RunVerdict

    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    assert ctrl.run_iteration(state).accepted
    assert open_seal_arenas(store.dir) == []
    with open(store.dir / SEAL_LEDGER, "ab") as fh:
        fh.write(b"\xff\xfe not utf-8\n")
    assert open_seal_arenas(store.dir) == [str(store.dir / SEAL_LEDGER)]
    out = _real_verdict(tmp_path, repo)
    assert out.verdict is RunVerdict.UNDETERMINED and "still open" in out.detail
    capsys.readouterr()
    assert deliver(tmp_path) == 2
    assert "still open" in capsys.readouterr().err


def test_the_launchers_entry_points_gate_on_an_open_seal(tmp_path, repo, spec):
    from hoh.launcher import Dispatch, HohRunLauncher
    from hoh.orchestrator import RunVerdict
    from hoh.project import TaskNode

    agents, ctrl, state, store, arena1 = _k10_scenario(tmp_path, repo, spec, touch=False)
    launcher = HohRunLauncher(tmp_path / "runs", repo)
    launcher._run_cli = lambda argv: Dispatch(0, "", "")    # no `hoh run` is started
    node = TaskNode(id="r1", run_id="r1", accepted_before="r1-i0-base")

    for entry in (launcher.evaluate, launcher.launch):
        out = entry(node)
        assert out.verdict is RunVerdict.UNDETERMINED and "still open" in out.detail

    assert ctrl.release_open_seals()["released"] == [str(arena1)]
    for entry in (launcher.evaluate, launcher.launch):
        assert entry(node).verdict is RunVerdict.ACCEPTED


@pytest.mark.parametrize("way", ["unconfirmed", "close-raises", "release-raises"])
def test_a_touch_is_compared_even_when_the_tabs_cannot_be_closed(
        tmp_path, repo, spec, way):
    from hoh.orchestrator import RunVerdict

    cls = {"unconfirmed": UnconfirmedAgents, "close-raises": FailingCloseAgents,
           "release-raises": ClosingAgents}[way]
    agents, ctrl, state, store, arena1 = _k10_scenario(
        tmp_path, repo, spec, agents_cls=cls)
    if way == "release-raises":
        def boom():
            raise RuntimeError("the release failed")
        ctrl.release_open_seals = boom
    payload, rc = _finish(store, agents, ctrl, state)
    assert rc == 1
    assert payload["condition"] == "BLOCKED"
    assert payload["capability_violation"] == [str(arena1)]
    assert writable(arena1) == [], "the seal stays in place"
    assert state_on_disk(store).blocked_kind == stages.CAPABILITY_VIOLATION
    assert _real_verdict(tmp_path, repo).verdict is not RunVerdict.ACCEPTED


def test_unconfirmed_tabs_without_a_touch_end_the_run_cleanly_but_open(
        tmp_path, repo, spec):
    from hoh.orchestrator import RunVerdict

    agents, ctrl, state, store, arena1 = _k10_scenario(
        tmp_path, repo, spec, touch=False, agents_cls=UnconfirmedAgents)
    payload, rc = _finish(store, agents, ctrl, state)
    assert rc == 0
    assert payload["condition"] == "ACTIVE"
    assert "capability_violation" not in payload
    assert writable(arena1) == []
    out = _real_verdict(tmp_path, repo)
    assert out.verdict is RunVerdict.UNDETERMINED and "still open" in out.detail


def test_two_names_inside_the_arena_are_refused_and_the_message_does_not_say_otherwise(
        tmp_path):
    arena = tmp_path / "arena"
    arena.mkdir()
    (arena / "one.py").write_text("X = 1\n", encoding="utf-8")
    os.link(arena / "one.py", arena / "two.py")           # both names are inside
    before = modes(arena)
    with pytest.raises(cap.SealFailed) as err:
        cap.seal_read_only(arena)
    text = str(err.value)
    assert "more than one name" in text and "cannot tell" in text
    assert "nothing changed" in text
    assert "name outside the arena is not sealed" not in text
    assert modes(arena) == before


def test_a_hard_linked_fifo_is_never_sealed(tmp_path):
    arena = tmp_path / "arena"
    arena.mkdir()
    (arena / "a.py").write_text("A = 1\n", encoding="utf-8")
    foreign = tmp_path / "foreign.fifo"
    os.mkfifo(foreign)
    os.chmod(foreign, 0o666)
    os.link(foreign, arena / "pipe")
    before = modes(arena)
    with pytest.raises(cap.SealFailed):
        cap.seal_read_only(arena)
    assert mode_of(foreign) == 0o666
    assert modes(arena) == before


# --------------------------------------------------------------------------- #
# Revision 7e -- the violation hold survives the contract and every way back
# (run qaw7: a contract's STOP or ABORT replaced the kind, the next block
# rewrote the reason, and `hoh deliver` fast-forwarded a violation-held run)
# --------------------------------------------------------------------------- #

HOLD_REASON = "capability violation: qa touched the arena"


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _real_delivery(tmp_path: Path, store: RunStore) -> tuple[Path, str]:
    """A real git repository with a real, fast-forwardable `work` branch (the
    one checked out) and the run's accepted candidate bound to its tree, so
    that `hoh deliver --approve` really would move `main`. Returns the
    repository and the commit `work` is at."""
    from hoh.contracts import Candidate

    r = tmp_path / "deliv"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.invalid")
    git(r, "config", "user.name", "T")
    (r / "a.txt").write_text("a\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    git(r, "checkout", "-q", "-b", "work")
    (r / "b.txt").write_text("b\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "the accepted candidate")
    head = _git_out(r, "rev-parse", "HEAD")
    cand = Candidate(candidate_id="r1-i2-a1", repo_path=str(r), commit=head,
                     tree_clean=True, tree_digest=_git_out(r, "rev-parse", "HEAD^{tree}"))
    _rewrite(store, repo_path=str(r), last_accepted_candidate=cand)
    return r, head


def _reblock(store: RunStore, governor, reason: str = "iteration aborted: boom"):
    """What `cmd_run`'s abort handler does: block again, through the contract."""
    with store.lock():
        fresh = store.read_state()
        routing = governance.block_under_contract(fresh, reason, governor)
        store.write_state(fresh)
    return routing


def _held_run(tmp_path, repo, spec, *, routing: str | None = None):
    agents, ctrl, state, store, arena1 = _k10_scenario(tmp_path, repo, spec, routing=routing)
    _finish(store, agents, ctrl, state)
    on_disk = state_on_disk(store)
    assert on_disk.blocked_kind == stages.CAPABILITY_VIOLATION
    return agents, ctrl, store, arena1


def _unblock(tmp_path) -> int:
    return cli.cmd_unblock(Namespace(root=tmp_path / "runs", run_id="r1",
                                     reason="investigated: the pane was mine"))


# --- K17: the contract keeps the hold -------------------------------------- #


@pytest.mark.parametrize("routing", ROUTINGS)
def test_a_second_block_under_a_contract_keeps_the_hold(
        tmp_path, repo, spec, capsys, routing):
    from hoh.autonomy import Handling
    from hoh.orchestrator import RunVerdict

    _, _, store, _ = _held_run(tmp_path, repo, spec)
    governor = governance.bind(store.dir, contract=_contract(routing))
    got = _reblock(store, governor)
    assert got.handling is Handling[routing]
    assert governor.routings()[-1]["handling"] == Handling[routing].value, (
        "the contract's own log says which routing it was")

    on_disk = state_on_disk(store)
    cancelled = routing in CANCELLING
    assert on_disk.blocked_kind == stages.CAPABILITY_VIOLATION
    assert on_disk.blocked_reason == "iteration aborted: boom"
    assert "capability violation" not in on_disk.blocked_reason, (
        "the reason no longer names the violation: only the kind holds the run")
    assert on_disk.condition is (Condition.CANCELLED if cancelled else Condition.BLOCKED)
    assert any(governance._HELD_NOTE in line for line in on_disk.history) is cancelled, (
        "a STOP or ABORT says that the hold stays; the other routings say nothing of it")
    outcome = _launcher_verdict(on_disk)
    assert outcome.verdict is RunVerdict.UNDETERMINED
    assert "capability violation" in outcome.detail
    capsys.readouterr()
    assert deliver(tmp_path, approve=True) == 2
    assert "capability violation" in capsys.readouterr().err

    # Control: a person's `hoh unblock` lifts a BLOCKED hold and refuses a
    # CANCELLED one (it takes a BLOCKED run only).
    rc = _unblock(tmp_path)
    after = state_on_disk(store)
    if cancelled:
        assert rc == 2
        assert (after.condition, after.blocked_kind) == (
            Condition.CANCELLED, stages.CAPABILITY_VIOLATION)
        assert _launcher_verdict(after).verdict is not RunVerdict.ACCEPTED
    else:
        assert rc == 0
        assert after.blocked_kind is None
        assert _launcher_verdict(after).verdict is RunVerdict.ACCEPTED


def test_pause_then_a_run_under_a_stop_contract_keeps_the_hold_and_delivers_nothing(
        tmp_path, repo, spec, capsys):
    """qaw7-b `test_a5`: no contract at the first block, `hoh pause`, a stop
    contract pinned, `run_iteration` meets "run is PAUSED" and the handler
    blocks again through the contract. The base branch must not move."""
    agents, ctrl, store, _ = _held_run(tmp_path, repo, spec)
    deliv, head = _real_delivery(tmp_path, store)
    main_before = _git_out(deliv, "rev-parse", "main")
    assert main_before != head
    runs = tmp_path / "runs"
    assert cli.cmd_pause(Namespace(root=runs, run_id="r1", reason="look later")) == 0
    governor = governance.bind(store.dir, contract=_contract("STOP_PROGRAM"))

    with pytest.raises(Exception) as met:
        fresh_controller(store, spec, Agents(repo)).run_iteration(state_on_disk(store))
    assert "PAUSED" in str(met.value)
    _reblock(store, governor, f"iteration aborted: {met.value}")

    on_disk = state_on_disk(store)
    assert on_disk.condition is Condition.CANCELLED
    assert on_disk.blocked_kind == stages.CAPABILITY_VIOLATION
    capsys.readouterr()
    assert deliver(tmp_path, approve=True) == 2
    assert "capability violation" in capsys.readouterr().err
    assert _git_out(deliv, "rev-parse", "main") == main_before, (
        "the base branch moved to the candidate of a violation-held run")


@pytest.mark.parametrize("routing", ROUTINGS)
def test_pause_and_an_unclear_resume_under_a_contract_keep_the_hold(
        tmp_path, repo, spec, capsys, routing):
    """qaw7-a `test_c3`: the violation under each routing, `hoh pause`, a start
    intent a crashed dispatch left, `hoh resume`. The reconciliation never
    runs for a held run, so the reason is never rewritten ("resume unclear")."""
    from hoh.contracts import TaskRef
    from hoh.orchestrator import RunVerdict

    _, _, store, _ = _held_run(tmp_path, repo, spec, routing=routing)
    cancelled = routing in CANCELLING
    held = state_on_disk(store)
    assert held.condition is (Condition.CANCELLED if cancelled else Condition.BLOCKED)
    reason = held.blocked_reason
    runs = tmp_path / "runs"

    capsys.readouterr()
    paused = cli.cmd_pause(Namespace(root=runs, run_id="r1", reason="look later"))
    assert paused == (2 if cancelled else 0), "pause is refused for a CANCELLED run"
    _rewrite(store, active_tasks=[TaskRef(task_id="t1", role=Role.QA, harness="fake",
                                          attempt_key="k1")])
    seq = state_on_disk(store).write_seq
    rc = cli.cmd_resume(Namespace(root=runs, run_id="r1", force=False, reason=None))
    after = state_on_disk(store)

    if cancelled:
        assert rc == 2
        assert after.write_seq == seq, "a refused resume writes nothing"
        assert after.condition is Condition.CANCELLED
    else:
        assert rc == 0, "a PAUSED hold resumes into BLOCKED, with no reconciliation"
        assert after.condition is Condition.BLOCKED
    assert after.blocked_kind == stages.CAPABILITY_VIOLATION
    assert after.blocked_reason == reason, "the block reason is never rewritten"
    assert "resume unclear" not in "".join(after.history)
    assert _launcher_verdict(after).verdict is not RunVerdict.ACCEPTED
    capsys.readouterr()
    assert deliver(tmp_path, approve=True) == 2
    assert "capability violation" in capsys.readouterr().err


def test_an_unheld_checkpoint_is_delivered_by_the_same_harness(tmp_path, repo, spec):
    """The control: with no violation, `hoh deliver` really fast-forwards the
    base branch here, so a refusal elsewhere is the hold and not the harness."""
    agents, ctrl, state, store, _ = _k10_scenario(tmp_path, repo, spec, touch=False)
    _, rc = _finish(store, agents, ctrl, state)
    assert rc == 0
    deliv, head = _real_delivery(tmp_path, store)
    assert _git_out(deliv, "rev-parse", "main") != head
    assert deliver(tmp_path, approve=True) == 0
    assert _git_out(deliv, "rev-parse", "main") == head


def test_a_cancelled_hold_re_blocked_with_no_contract_stays_held_until_a_person_lifts_it(
        tmp_path, repo, spec, capsys):
    """Round 12 (qa7e-a 3, qa7e-b 4): "terminal" meant "no automatic path". A
    later block with no contract turns CANCELLED into BLOCKED; the kind, the
    hold, is lost only by a person's `hoh unblock`."""
    from hoh.orchestrator import RunVerdict

    _, _, store, _ = _held_run(tmp_path, repo, spec, routing="STOP_PROGRAM")
    cancelled = state_on_disk(store)
    assert cancelled.condition is Condition.CANCELLED
    assert _unblock(tmp_path) == 2, "hoh unblock takes a BLOCKED run only"
    assert state_on_disk(store).blocked_kind == stages.CAPABILITY_VIOLATION

    _reblock(store, None, "iteration aborted: the contract could not be read")
    again = state_on_disk(store)
    assert again.condition is Condition.BLOCKED
    assert again.blocked_kind == stages.CAPABILITY_VIOLATION
    assert _launcher_verdict(again).verdict is not RunVerdict.ACCEPTED
    capsys.readouterr()
    assert deliver(tmp_path, approve=True) == 2
    assert "capability violation" in capsys.readouterr().err

    assert _unblock(tmp_path) == 0
    lifted = state_on_disk(store)
    assert lifted.blocked_kind is None
    assert _launcher_verdict(lifted).verdict is RunVerdict.ACCEPTED


# --- K18: every condition --------------------------------------------------- #


def _held_state(tmp_path, repo, spec, condition: Condition):
    """An accepted run (stage CHECKPOINTED) that carries the hold in `condition`."""
    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    assert ctrl.run_iteration(state).accepted
    held = _rewrite(store, condition=condition, blocked_kind=stages.CAPABILITY_VIOLATION,
                    blocked_reason=HOLD_REASON, stop_reason=HOLD_REASON)
    assert stages.violation_unresolved(held)
    return store


@pytest.mark.parametrize("force", [False, True], ids=["plain", "force"])
@pytest.mark.parametrize("condition", [Condition.BLOCKED, Condition.CANCELLED,
                                       Condition.FAILED, Condition.ACTIVE],
                         ids=lambda c: c.value)
def test_hoh_resume_refuses_a_held_run_in_every_condition_but_paused(
        tmp_path, repo, spec, capsys, condition, force):
    store = _held_state(tmp_path, repo, spec, condition)
    before = state_on_disk(store)
    capsys.readouterr()
    rc = cli.cmd_resume(Namespace(root=tmp_path / "runs", run_id="r1", force=force,
                                  reason="go on"))
    err = capsys.readouterr().err
    assert rc == 2
    assert "capability violation" in err and condition.value in err
    if condition is Condition.BLOCKED:
        assert "hoh unblock" in err
    else:
        assert "BLOCKED run only" in err
    after = state_on_disk(store)
    assert after.write_seq == before.write_seq, "a refusal writes nothing"
    assert (after.condition, after.blocked_kind, after.blocked_reason) == (
        before.condition, before.blocked_kind, before.blocked_reason)


@pytest.mark.parametrize("stage", [Stage.CHECKPOINTED, Stage.PLANNING],
                         ids=lambda x: x.value)
@pytest.mark.parametrize("condition", [Condition.BLOCKED, Condition.PAUSED,
                                       Condition.CANCELLED, Condition.FAILED,
                                       Condition.ACTIVE], ids=lambda c: c.value)
def test_deliver_checks_the_hold_before_the_stage_and_before_any_git_operation(
        tmp_path, repo, spec, capsys, monkeypatch, condition, stage):
    import hoh.delivery

    store = _held_state(tmp_path, repo, spec, condition)
    deliv, _ = _real_delivery(tmp_path, store)
    _rewrite(store, stage=stage)
    main_before = _git_out(deliv, "rev-parse", "main")

    def tripwire(**kwargs):
        raise AssertionError("delivery.deliver was reached for a violation-held run")

    monkeypatch.setattr(hoh.delivery, "deliver", tripwire)
    capsys.readouterr()
    assert deliver(tmp_path, approve=True) == 2
    err = capsys.readouterr().err
    assert "held by a capability violation" in err and condition.value in err
    assert "Stage is" not in err, "the hold is judged before the stage"
    assert _git_out(deliv, "rev-parse", "main") == main_before


@pytest.mark.parametrize("routing", ROUTINGS)
def test_the_acceptance_record_refuses_a_held_run_in_every_condition(
        tmp_path, repo, spec, routing):
    """The tool reads the real `state.json` of each routing (CANCELLED ones
    included), and the kind alone holds in every condition."""
    acc = _acceptance_tool()
    _, _, store, _ = _held_run(tmp_path, repo, spec, routing=routing)
    state = json.loads((store.dir / "state.json").read_text(encoding="utf-8"))
    assert state["blocked_kind"] == "capability_violation"
    assert acc.held_by_violation(state)
    record = {"stage": "CHECKPOINTED", "accepted_candidate": "r1-i1",
              "held_by_capability_violation": {
                  k: state.get(k) for k in ("condition", "blocked_kind", "blocked_reason")}}
    assert [p for p in acc.satisfied(record) if "capability violation" in p]

    for condition in ("ACTIVE", "BLOCKED", "PAUSED", "CANCELLED", "FAILED"):
        assert acc.held_by_violation({"condition": condition,
                                      "blocked_kind": "capability_violation",
                                      "blocked_reason": "iteration aborted: x"}), condition


def test_deliver_reads_the_hold_before_an_open_seal(tmp_path, repo, spec, capsys):
    """A held run whose arena is still sealed is refused for the violation, not
    for the open seal (qa7e-b 2): the order of the two checks is pinned."""
    from hoh.controller import open_seal_arenas

    _, _, state, store, arena1 = _k10_scenario(tmp_path, repo, spec, touch=False)
    _rewrite(store, condition=Condition.BLOCKED, blocked_kind=stages.CAPABILITY_VIOLATION,
             blocked_reason=HOLD_REASON, stop_reason=HOLD_REASON)
    assert open_seal_arenas(store.dir) == [str(arena1)], "the seal is still open"
    capsys.readouterr()
    assert deliver(tmp_path, approve=True) == 2
    err = capsys.readouterr().err
    assert "held by a capability violation" in err
    assert "still open" not in err


# --- K19: the window before the release ------------------------------------ #


class GarbledFirstAnswer(Agents):
    """QA's first answer is not JSON, so the schema repair is a second dispatch."""

    garbled = False

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str:
        if role is Role.QA and not self.garbled:
            self.garbled = True
            self.calls.append(role)
            self.prompts[role] = prompt
            self.qa_arenas.append(self.ctrl._qa_arena)
            return "I looked at it and it all seems fine."
        return super().dispatch(role, prompt, state=state)


def _act_in_the_charge(ctrl, monkeypatch, agents, *, qa_calls_before: int):
    """The pane acts inside a dispatch's budget charge: `_dispatch_admitted`
    charges before it takes its witness, so the touch lies in no witness."""
    real = ctrl._budget_or_refusal
    acted: list[bool] = []

    def charge(st: RunState) -> None:
        arena = getattr(ctrl, "_qa_arena", None)
        if (not acted and arena is not None and arena.exists() and not writable(arena)
                and agents.calls.count(Role.QA) == qa_calls_before):
            acted.append(True)
            undone_write(st, arena)
        return real(st)

    monkeypatch.setattr(ctrl, "_budget_or_refusal", charge)
    return acted


def _assert_caught_at_the_release(out, state, store, arena) -> None:
    _assert_violation(out, state, arena)
    assert "after it was sealed, outside every dispatch's witness" in out.reason, out.reason
    events = events_for(store, arena)
    assert any(e["event"] == "touched" for e in events)
    assert events[-1]["event"] == "released" and events[-1]["touched"] is True
    assert writable(arena) != [], "the arena is restored before the violation is raised"


@pytest.mark.parametrize("window", ["before-the-first-send", "between-two-sends"])
def test_a_touch_before_the_release_that_no_witness_covers_is_caught_at_the_release(
        tmp_path, repo, spec, monkeypatch, window):
    """qaw7-b `test_w1`: an undone write where no dispatch's witness is. Every
    witness is taken when its dispatch starts, so the write is inside a later
    baseline or outside every baseline, and the iteration used to be ACCEPTED."""
    first = window == "before-the-first-send"
    agents = (Agents if first else GarbledFirstAnswer)(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    acted = _act_in_the_charge(ctrl, monkeypatch, agents, qa_calls_before=0 if first else 1)
    out = ctrl.run_iteration(state)
    assert acted, "the pane never got its window"
    _assert_caught_at_the_release(out, state, store, agents.qa_arenas[0])


@pytest.mark.parametrize("how", ["dispatch-error", "crash"])
def test_a_touch_in_the_window_is_caught_when_the_send_then_fails(
        tmp_path, repo, spec, monkeypatch, how):
    """The pane acts before the dispatch's witness, then the send fails (a
    timeout is the common case): the release still finds the touch."""
    class Boom(RuntimeError):
        pass

    failure = DispatchError("role timeout") if how == "dispatch-error" else Boom("crash")
    agents = Agents(repo, qa_script=[raising(failure)])
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    acted = _act_in_the_charge(ctrl, monkeypatch, agents, qa_calls_before=0)
    out = ctrl.run_iteration(state)
    assert acted, "the pane never got its window"
    _assert_caught_at_the_release(out, state, store, agents.qa_arenas[0])
    assert "NO QA VERDICT" not in out.reason


def test_a_release_that_cannot_compare_the_arena_fails_closed_with_the_kind(
        tmp_path, repo, spec):
    """Round 12 (qa7e-a 4): if the digest at the release raises, the arena
    cannot be shown untouched, so it is taken as touched -- recorded, restored
    and blocked with the kind, not an error that escapes ahead of the unseal."""
    agents = Agents(repo)
    ctrl, state, store = build(tmp_path, repo, spec, agents)
    real_release, real_digest = ctrl._release_seal, cap.frozen_digest

    def unreadable(root):
        raise OSError(13, "Permission denied")

    def release(*a, **k):
        cap.frozen_digest = unreadable
        try:
            return real_release(*a, **k)
        finally:
            cap.frozen_digest = real_digest

    ctrl._release_seal = release
    try:
        out = ctrl.run_iteration(state)
    finally:
        cap.frozen_digest = real_digest
    arena = agents.qa_arenas[0]
    _assert_violation(out, state, arena)
    assert "could not be compared with its seal digest" in out.reason, out.reason
    events = events_for(store, arena)
    touched = [e for e in events if e["event"] == "touched"]
    assert touched and "unreadable" in touched[0]
    assert [e["event"] for e in events][-2:] == ["releasing", "released"]
    assert events[-1]["touched"] is True
    assert writable(arena) != [], "the modes are restored although the digest failed"
