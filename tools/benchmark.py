#!/usr/bin/env python3
"""benchmark.py -- the matched-budget comparison `docs/BENCHMARK_PROTOCOL.md` fixes.

Three arms over the same task, the same dispatch budget and the same hidden
acceptance suite:

* **A** a plain agent in a Herdr session, given the task and the repository,
  with no harness around it;
* **B** one `hoh run` -- plan, develop, QA, acceptance checks, receipts;
* **C** the full control plane -- `ProjectState`, global gates over the merged
  result, automatic repair, closure to a fixpoint.

The protocol is committed before this file ran for the first time, and the
analysis it fixes is not re-decidable here. What this program does is execute
cells and write what happened; it computes no comparison and draws no
conclusion. `docs/BENCHMARK_RESULTS.md` is written separately, from the cells
that actually ran, and names the ones that did not.

**The hidden suite is the verdict.** An arm's own tests are evidence about the
arm and never the measurement -- an arm that writes a weak test and passes it
has demonstrated that it writes weak tests. The suite is copied in only after
the arm has stopped, into a throwaway directory, and the arm never sees it.

Usage:
    python3 tools/benchmark.py run --task to_roman --arm B
    python3 tools/benchmark.py report
"""

from __future__ import annotations

import argparse
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOH = HERE.parent
sys.path.insert(0, str(HOH / "src"))

TASKS = HOH / "dogfood/benchmark/tasks"
RESULTS = HOH / "dogfood/benchmark/results"

#: Campaign v1's cells live in `results/` and are never touched again: the
#: protocol marks that campaign `PRE-O125-CLOSURE` and a benchmark edited after
#: its result is known is not a benchmark. A replication writes beside it.
CAMPAIGNS = {
    "v1": HOH / "dogfood/benchmark/results",
    "v2": HOH / "dogfood/benchmark/results-v2",
    "v3": HOH / "dogfood/benchmark/results-v3",
    # Not a campaign, and never reported as one: a pre-flight cell for proving
    # that the pipeline runs end to end -- trust helper, worktree, real
    # dispatches, budget, hidden suite -- before a campaign's first cell is
    # spent on finding out. `tools/repetition_plan.py` does not know this name,
    # so nothing can aggregate it into a result.
    "smoke": HOH / "dogfood/benchmark/results-smoke",
}


def results_for(campaign_: str) -> Path:
    if campaign_ not in CAMPAIGNS:
        raise SystemExit(f"unknown campaign: {campaign_}. "
                         f"Known: {', '.join(sorted(CAMPAIGNS))}")
    return CAMPAIGNS[campaign_]

#: Where this deployment's trust helper lives, and where its harness puts
#: worktrees. Read from the environment with **no default**, for the reason
#: `approval.py` gives about its own script path: a hardcoded location makes
#: one machine's layout a product requirement. An earlier version of this file
#: spelled both out, and the export's leak scan found them -- correctly, since
#: publishing them would have published this machine's layout too.
TRUST_HELPER_ENV = "HOH_TRUST_HELPER"
WORKTREE_ROOT_ENV = "HOH_WORKTREE_ROOT"


def _configured(name: str) -> Path:
    value_ = os.environ.get(name, "").strip()
    if not value_:
        raise SystemExit(
            f"{name} is not set. The benchmark dispatches real agents into "
            "worktrees, which needs this deployment's trust helper and the "
            "directory its harness puts worktrees in. There is deliberately no "
            "default: a hardcoded path would make one machine's layout a "
            "requirement of the tool."
        )
    return Path(value_).expanduser()


#: Fixed by the protocol. Not a parameter: making it one is how a matched
#: budget stops being matched.
DISPATCH_BUDGET = 9


def dispatch_count(root: Path) -> dict:
    """Provider calls this arm spent, counted from the dispatch logs.

    It was `min(iterations * 3, DISPATCH_BUDGET)` -- a **constant** in arm C,
    and a ceiling-clipped estimate in arm B. Measured on the first cell of the
    replication: the arm ran a node and a repair node, spent 18 dispatches, and
    reported 9. A benchmark whose premise is a matched budget cannot assert its
    own budget figure.

    The replacement counted lines in `telemetry.jsonl`, on the stated
    assumption that it has "one line per dispatch by construction". It does
    not, in **both** directions, and `tools/budget_evidence.py` measured it:
    a role that retried twice writes one line for three provider calls, and a
    role refused at the budget writes one line for none. So the line now
    carries the number (`provider_calls`) and this sums it.

    Lines written before that field existed cannot answer. They are reported
    as `lines_without_the_figure` rather than scored as one call each --
    silently reading an old line as "1" is how the asserted figure got in.
    """
    calls_ = 0
    lines = 0
    silent_ = 0
    for f in sorted(root.rglob("telemetry.jsonl")):
        try:
            content_ = f.read_text()
        except OSError:                            # pragma: no cover
            continue
        for z in content_.splitlines():
            if not z.strip():
                continue
            lines += 1
            try:
                n = json.loads(z).get("provider_calls")
            except ValueError:                     # pragma: no cover - partial
                n = None
            if n is None:
                silent_ += 1
            else:
                calls_ += int(n)
    return {"provider_calls": calls_, "lines": lines,
            "lines_without_the_figure": silent_}


def counted_dispatches(root: Path) -> int:
    """`dispatch_zaehlung`'s call count, for callers that want one number.

    A root whose lines predate `provider_calls` returns the count of the ones
    that could answer -- which is why the cell records the full breakdown and
    the repetition plan reads *that*, not this.
    """
    return dispatch_count(root)["provider_calls"]


#: Where the harness records which directories an agent may work in. Read here
#: only to *verify* a registration, never to write one -- writing is the
#: helper's business and this file does not know its format.
TRUST_REGISTRY = Path.home() / ".claude.json"


def _is_trusted(file_path: Path) -> bool:
    try:
        data_ = json.loads(TRUST_REGISTRY.read_text())
    except (OSError, ValueError):
        return False
    return str(file_path) in (data_.get("projects") or {})


def trust_and_check(worktree: Path, repo: Path, *, attempts_: int = 3) -> dict:
    """Registers trust for a worktree and checks that the registration stuck.

    Firing the helper and moving on is not enough, and the benchmark is where
    that showed. Four arm-C cells came back with their planner sitting on a
    trust dialog; the helper had been called and had answered `granted`, and
    the worktrees were nevertheless absent from the registry afterwards. The
    registry is one JSON file that every session read-modify-writes, and with
    several agents starting at once a registration is simply lost.

    So it is verified, retried, and the outcome goes into the cell. A cell that
    fails because an agent could not be given permission to work says nothing
    about the arm, and it must be possible to tell that from the record.
    """
    last_ = ""
    for n in range(1, attempts_ + 1):
        p = subprocess.run(
            [str(_configured(TRUST_HELPER_ENV)), str(worktree), str(repo)],
            capture_output=True, text=True, timeout=120,
        )
        last_ = (p.stdout or p.stderr).strip()[-200:]
        if _is_trusted(worktree):
            return {"registered": True, "attempts": n, "said": last_}
        time.sleep(1.0)
    return {"registered": False, "attempts": attempts_, "said": last_}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def working_tree(task: str, arm: str, one_run: int) -> Path:
    """A fresh repository at the task's base state, named uniquely per attempt.

    The name carries the temp directory's own nonce, and that is not cosmetic.
    The harness names a worktree after the repository's directory, so two
    attempts at the same cell -- a re-run after a provider outage, say --
    produced the same worktree path. The second attempt then found the first
    attempt's worktree already sitting there, registered against a repository
    that no longer existed, and the trust helper refused it: five cells failed
    in a tenth of a second each, for a reason that had nothing to do with the
    arms. This project has met the same collision once before, in a fixture.
    """
    baseline = Path(tempfile.mkdtemp(prefix=f"bench-{task}-{arm}{one_run}-"))
    repo = baseline / f"bm-{task}-{arm}{one_run}-{baseline.name.rsplit('-', 1)[-1]}"
    repo.mkdir(parents=True)
    for f in sorted((TASKS / task / "base").iterdir()):
        shutil.copy2(f, repo / f.name)
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=bench@local",
         "-c", "user.name=Benchmark", "commit", "-qm", "Base state"],
        capture_output=True, text=True,
    )
    return repo


def unchanged_(repo: Path, task: str) -> bool:
    """True when the arm left the tree byte-identical to the base state.

    The protocol's exclusion rule, made checkable and arm-agnostic: a cell is
    excluded from the correctness comparison **only** when the arm never
    produced a final state, and each exclusion is listed by name. "Never
    produced a final state" is not a judgement about why it stopped -- a rate
    limit, a dispatch that never landed, a pane that came back empty -- it is
    simply that nothing changed.

    Written after the first such cell appeared and before any number was read
    off it: the rule itself is in the protocol, committed before the first arm
    ran.
    """
    baseline = TASKS / task / "base"
    for f in sorted(baseline.iterdir()):
        target = repo / f.name
        if not target.is_file() or target.read_bytes() != f.read_bytes():
            return False
    return True


def hidden_verdict(repo: Path, task: str) -> dict:
    """Runs the hidden suite against the arm's final state, in a copy.

    A copy, because the suite must not become part of the tree an arm is
    measured on -- and because a second run of the same cell has to measure the
    same thing.
    """
    with tempfile.TemporaryDirectory(prefix="bench-verdict-") as d:
        above = Path(d)
        target = above / "kandidat"
        target.mkdir()
        shadowed = []
        for f in repo.iterdir():
            if f.is_file() and f.suffix == ".py":
                if f.stem in sys.stdlib_module_names:
                    shadowed.append(f.name)
                shutil.copy2(f, target / f.name)
        shutil.copy2(TASKS / task / "hidden_test.py", target / "hidden_test.py")
        # `python -m unittest` from inside the candidate puts the candidate at
        # the FRONT of `sys.path`, ahead of the standard library. A file the
        # arm happened to write named `unittest.py` -- or `types.py`, `copy.py`,
        # `string.py` -- then answers the import, and the verdict changes. A
        # stub `unittest.py` beside a failing hidden suite flipped
        # `hidden_suite.passed` to true and `false_accept` to false; no malice
        # is needed for a collision.
        #
        # So the suite is started from a directory the candidate does not own,
        # with `-P` (the candidate's directory is not prepended) and the
        # candidate **appended** to `sys.path` by the runner below. The
        # standard library then wins every name, and the candidate is still
        # importable.
        runner = above / "auffuehren.py"
        runner.write_text(
            "import sys, unittest\n"
            f"sys.path.append({str(target)!r})\n"
            "unittest.main(module=None, argv=['hidden', 'hidden_test', '-v'],\n"
            "              exit=True)\n",
            encoding="utf-8")
        p = subprocess.run(
            [sys.executable, "-P", str(runner)],
            cwd=str(above), capture_output=True, text=True, timeout=300,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": str(above), "PYTHONDONTWRITEBYTECODE": "1",
                 "PYTHONSAFEPATH": "1"},
        )
    text = p.stdout + p.stderr
    return {
        "passed": p.returncode == 0,
        "exit_code": p.returncode,
        "failures": text.count("FAIL:"),
        "errors": text.count("ERROR:"),
        # Recorded even though the import order now makes it harmless: a cell
        # whose candidate carries a file named after a standard-library module
        # is worth seeing, and a reader should not have to take the import
        # order on trust.
        "shadowed_stdlib_modules": shadowed,
        "tail": text.strip().splitlines()[-3:],
    }


# --------------------------------------------------------------------------- #
# Arm A: a plain agent, no harness
# --------------------------------------------------------------------------- #


def arm_a(task: str, repo: Path, one_run: int) -> dict:
    """One agent, the specification, the repository, and nothing else.

    Deliberately the baseline anybody already has: no acceptance checks, no
    receipts, no second opinion. Its budget is the same nine dispatches, spent
    as nine turns.
    """
    from hoh import herdr
    from hoh.contracts import Role
    from hoh.dispatchers import build_dispatcher

    spec = (TASKS / task / "SPEC.md").read_text()
    answers = repo.parent / "answers"
    answers.mkdir(exist_ok=True)
    herdr.require_herdr()
    # The agent works in a worktree, exactly as arms B and C do. Two reasons,
    # and only the second is about fairness:
    #
    # * the trust helper refuses a primary checkout by design -- it exists to
    #   pre-register an *isolated* worktree -- so a bare directory leaves the
    #   agent wedged on a dialog nobody is there to answer;
    # * an arm that worked directly in the checkout would differ from B and C
    #   in two ways at once, and the comparison is supposed to isolate the
    #   harness, not the working arrangement.
    work_ = repo.parent / f"{repo.name}-wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(work_),
         "-b", f"arm-a-{one_run}"],
        capture_output=True, text=True, timeout=120,
    )
    trust = trust_and_check(work_, repo)
    d = build_dispatcher(
        answers_dir=answers,
        profiles={Role.DEVELOPER: "claude"},
        cwd=work_,
        prefer_herdr=True,
        timeout_ms=1800 * 1000,
    )

    # A real `RunState`, not a stand-in. The dispatcher reads `repo_path`
    # off it to place the agent's pane, and a duck-typed object missing one
    # field fails after the pane has been opened -- which is what the first
    # attempt at this arm did, in 0.1 seconds, five times.
    from hoh.controller import Controller

    condition = Controller.new_state(
        run_id=f"bma{task[:6]}{one_run}{_nonce(repo)}".replace("_", ""),
        repo_path=work_,
        project_name=f"benchmark-{task}",
        spec_path=TASKS / task / "SPEC.md",
    )
    condition.iteration, condition.attempt = 1, 1

    prompt = (
        "You are working in a git repository. Implement what the specification "
        "below asks for, in the files that are already there.\n\n"
        "There is no acceptance harness here and no reviewer: what you leave in "
        "the working tree is the answer. Write whatever tests you think are "
        "warranted.\n\n"
        f"--- specification ---\n{spec}\n--- end ---\n\n"
        "When you are done, reply with a one-line summary."
    )
    t0 = time.monotonic()
    error = ""
    # Counted, not asserted. It is 1 by construction -- this arm is a single
    # agent turn and there is no loop above it -- but `"dispatches": 1` was a
    # constant beside a result, which is the exact shape the campaign's cost
    # figure was caught in (O140). A constant that happens to be right is
    # still not a measurement, and `repetition_plan` rightly reported every
    # arm-A cell as UNKNOWN_FIGURE_WAS_ASSERTED.
    calls_ = 0
    try:
        calls_ += 1
        answer = d.dispatch(Role.DEVELOPER, prompt, state=condition)
    except Exception as exc:
        answer, error = "", f"{type(exc).__name__}: {exc}"
    duration_ = time.monotonic() - t0
    try:
        d.close_own()
    except Exception:
        pass
    return {
        "dispatches": calls_,
        "dispatch_count": {"provider_calls": calls_, "lines": 0,
                           "lines_without_the_figure": 0,
                           "source": "counted in arm_a: this arm makes its "
                                     "provider calls directly, without a "
                                     "controller writing telemetry"},
        "dispatch_budget": DISPATCH_BUDGET,
        "single_shot": True,
        "wallclock_seconds": round(duration_, 1),
        "answer_chars": len(answer or ""),
        "error": error,
        "worktree": str(work_),
        "trust": trust,
    }


# --------------------------------------------------------------------------- #
# Arm B: one HoH run
# --------------------------------------------------------------------------- #


def _nonce(repo: Path) -> str:
    """The attempt's own nonce, from the temp directory the cell runs in.

    Every name a re-run shares with its predecessor is a collision waiting to
    happen, and this benchmark found three of them in one sitting: the
    repository directory (so the harness reused the previous attempt's
    worktree), and -- the expensive one -- the **run id**, which is what the
    dispatcher derives an agent's name from. A second attempt at the same cell
    met its own first attempt's planner pane still open in a directory that no
    longer belonged to the run, and HoH refused to adopt it. Correctly: a
    foreign session is exactly what it should not take over.
    """
    return repo.name.rsplit("-", 1)[-1][:8].replace("_", "")


def arm_b(task: str, repo: Path, one_run: int, isolation: str) -> dict:
    root = repo.parent / "root"
    run_id = f"bmb{task[:6]}{one_run}{_nonce(repo)}".replace("_", "")
    environment_ = dict(os.environ, PYTHONPATH=str(HOH / "src"))
    spec = TASKS / task / "SPEC.md"

    wt = subprocess.run(
        [sys.executable, "-m", "hoh.cli", "--root", str(root), "worktree",
         "--repo", str(repo), "--branch", f"bench-{run_id}"],
        capture_output=True, text=True, env=environment_, timeout=300,
    )
    worktree = _worktree_from(wt.stdout) or repo
    # Recorded, not fired and forgotten. A cell that came back with the agent
    # sitting on a trust dialog could not say whether the helper had refused,
    # had never been reached, or had been given the wrong path -- so the
    # answer goes into the cell.
    trust = trust_and_check(worktree, repo)
    trust["worktree"] = str(worktree)
    st = subprocess.run(
        [sys.executable, "-m", "hoh.cli", "--root", str(root), "start",
         "--repo", str(worktree), "--spec", str(spec), "--run-id", run_id,
         "--max-iterations", "3",
         # Three roles over three iterations happens to be nine, so this arm
         # looked budgeted without being budgeted: a schema repair or a
         # transient retry is a further provider call and nothing stopped one.
         # The protocol says the ceiling is nine, so the ceiling is passed.
         "--max-dispatches", str(DISPATCH_BUDGET)],
        capture_output=True, text=True, env=environment_, timeout=300,
    )
    if st.returncode != 0:
        return {"dispatches": 0, "error": (st.stderr or st.stdout)[-300:],
                "trust": trust}

    t0 = time.monotonic()
    r = subprocess.run(
        [sys.executable, "-m", "hoh.cli", "--root", str(root), "run", run_id,
         "--iterations", "3", "--planner", "claude", "--developer", "claude",
         "--qa", "claude", "--isolation", isolation],
        capture_output=True, text=True, env=environment_, timeout=5400,
    )
    duration_ = time.monotonic() - t0
    condition = _run_state(root / run_id / "state.json")
    # Three roles per iteration is the budget's unit.
    iterations_ = condition.get("iteration", 0)
    count_ = dispatch_count(root)
    return {
        "dispatches": count_["provider_calls"],
        "dispatch_count": count_,
        "dispatch_budget": DISPATCH_BUDGET,
        "over_budget": count_["provider_calls"] > DISPATCH_BUDGET,
        "iterations": iterations_,
        "wallclock_seconds": round(duration_, 1),
        "accepted": bool(condition.get("last_accepted_candidate")),
        "receipts": len(list((root / run_id / "receipts").glob("*.json")))
        if (root / run_id / "receipts").is_dir() else 0,
        "worktree": str(worktree),
        "root": str(root),
        "run_id": run_id,
        "trust": trust,
        "cli_exit": r.returncode,
        "tail": (r.stdout or r.stderr).strip().splitlines()[-3:],
    }


def _worktree_from(stdout: str) -> Path | None:
    try:
        d = json.loads(stdout)
    except ValueError:
        return None
    stack_ = [d]
    while stack_:
        k = stack_.pop()
        if isinstance(k, dict):
            if "checkout_path" in k:
                return Path(k["checkout_path"])
            stack_.extend(k.values())
        elif isinstance(k, list):
            stack_.extend(k)
    return None


def _run_state(file_path: Path) -> dict:
    try:
        return json.loads(file_path.read_text())
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
# Arm C: the full control plane
# --------------------------------------------------------------------------- #


class _CheckedScope:
    """`PrefixScopedProvider` that checks its grant actually landed.

    Same scope rules, same refusals -- the only addition is that a grant is
    read back out of the registry and retried if it is not there. See
    `trust_und_pruefen`: the registry is one file that every session
    read-modify-writes, and a grant can be lost between being made and being
    needed. The provider was reporting `granted` truthfully and the agent was
    still meeting a dialog.
    """

    def __init__(self, *, script: Path, prefix: Path):
        from hoh.approval import PrefixScopedProvider

        self._inner = PrefixScopedProvider(script=script, prefix=prefix)
        self.name = "verified-prefix-scope"
        self.granted: list[Path] = []

    def may_approve(self, worktree: Path) -> bool:
        return self._inner.may_approve(worktree)

    def approve(self, worktree: Path, repo: Path):
        from hoh.approval import Approval

        a = self._inner.approve(worktree, repo)
        if not a.granted:
            return a
        target = a.worktree
        if _is_trusted(target):
            self.granted.append(target)
            return a
        result = trust_and_check(target, repo)
        if result["registered"]:
            self.granted.append(target)
            return Approval(
                granted=True, worktree=target, provider=self.name,
                detail=f"{a.detail}; verified after {result['attempts']} attempt(s)",
            )
        return Approval(
            granted=False, worktree=target, provider=self.name,
            detail=(f"the helper answered '{result['said']}' but {target} is not "
                    f"in the registry after {result['attempts']} attempt(s); "
                    "refusing rather than dispatching an agent that will stop "
                    "at a dialog nobody is there to answer"),
        )


def arm_c(task: str, repo: Path, one_run: int, isolation: str) -> dict:
    """One node, the repository's own gates over the merged state, closure.

    On a single-node task C differs from B only by the global gates and the
    repair cycle they can trigger. That is the point: the protocol says the
    B-vs-C comparison is only meaningful on the tasks where composition exists,
    and reporting it on the others would be reporting noise.
    """
    from hoh.launcher import HohRunLauncher
    from hoh.orchestrator import GateRunner, HaltClass, ProjectController
    from hoh.project import (
        ActionClass,
        GateOutcome,
        GateResult,
        Lifecycle,
        ProjectState,
        TaskNode,
    )
    from hoh.projectstore import ProjectStore

    os.environ["PYTHONPATH"] = str(HOH / "src")
    root = repo.parent / "root"
    spec = TASKS / task / "SPEC.md"

    class Gates(GateRunner):
        def __init__(self, r: Path):
            self.repo = r

        def subject(self) -> str:
            return _git(self.repo, "rev-parse", "HEAD").stdout.strip()[:12] or "(none)"

        def run(self, subject: str) -> list[GateResult]:
            # The arm's *own* tests over the merged state. Not the hidden
            # suite: C may not see the measurement either.
            p = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p",
                 "test*.py"],
                cwd=str(self.repo), capture_output=True, text=True, timeout=900,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                     "HOME": str(self.repo)},
            )
            return [GateResult(
                name="own-tests", subject=subject,
                outcome=GateOutcome.GREEN if p.returncode == 0 else GateOutcome.RED,
                exit_code=p.returncode,
                detail=((p.stdout + p.stderr).strip().splitlines() or ["-"])[-1][:160],
            )]

    store = ProjectStore(root, f"bmc{one_run}{_nonce(repo)}")
    if not store.exists():
        store.write_state(ProjectState(
            project_id=f"bmc{one_run}{_nonce(repo)}",
            repo_path=str(repo),
            nodes=[TaskNode(
                id=f"n{task[:6]}{_nonce(repo)}".replace("_", ""),
                spec_path=str(spec),
                spec_digest=_digest(spec.read_text()),
                lifecycle=Lifecycle.READY,
                action_class=ActionClass.INTERNAL,
            )],
        ))
    starter = HohRunLauncher(
        root, repo, mainline="main", iterations=3,
        planner="claude", developer="claude", qa="claude",
        isolation=isolation,
        # The protocol's own words: nine role dispatches per task per arm, and
        # "C's dispatches include those its repair nodes make". Until this
        # existed there was no way to say it, and three of campaign v2's five
        # arm-C cells spent 18 against 9 without being stopped (O140).
        dispatch_budget=DISPATCH_BUDGET,
        approvals=_CheckedScope(
            script=_configured(TRUST_HELPER_ENV),
            prefix=_configured(WORKTREE_ROOT_ENV) / repo.name,
        ),
    )
    t0 = time.monotonic()
    result = ProjectController(store, starter, Gates(repo)).run()
    duration_ = time.monotonic() - t0
    after = store.read_state()
    count_ = dispatch_count(root)
    return {
        "dispatches": count_["provider_calls"],
        "dispatch_count": count_,
        "dispatch_budget": DISPATCH_BUDGET,
        "over_budget": count_["provider_calls"] > DISPATCH_BUDGET,
        "wallclock_seconds": round(duration_, 1),
        "halt": str(result.halt),
        "reason": result.reason[:200],
        "rc_closed": after.rc_closed(),
        "closure_generation": after.closure_generation,
        "repair_nodes": [n.id for n in after.nodes if n.repair_of],
        "human_decisions": len(
            [d for d in after.decisions if d.actor not in ("orchestrator", None)]
        ),
        "steps": [f"{s.kind}:{s.node or ''}" for s in result.steps],
        "closed": result.halt is HaltClass.CLOSED,
        "approvals": [
            {"granted": a.granted, "detail": a.detail[:200]}
            for a in starter.approvals_given
        ],
    }


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #


def cmd_run(args) -> int:
    task, arm, one_run = args.task, args.arm.upper(), args.rep
    if not (TASKS / task).is_dir():
        print(f"no such task: {task}", file=sys.stderr)
        return 2
    target_dir = results_for(getattr(args, "campaign", "v1"))
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{task}.{arm}.{one_run}.json"
    if target.exists() and not args.again:
        print(f"cell already run: {target.name}", file=sys.stderr)
        return 0

    repo = working_tree(task, arm, one_run)
    t0 = time.time()
    try:
        if arm == "A":
            raw_ = arm_a(task, repo, one_run)
            measured_ = Path(raw_.get("worktree") or repo)
        elif arm == "B":
            raw_ = arm_b(task, repo, one_run, args.isolation)
            measured_ = Path(raw_.get("worktree") or repo)
        elif arm == "C":
            raw_ = arm_c(task, repo, one_run, args.isolation)
            measured_ = repo
        else:
            print(f"unknown arm: {arm}", file=sys.stderr)
            return 2
        error = ""
    except Exception as exc:
        raw_, measured_, error = {}, repo, f"{type(exc).__name__}: {exc}"

    verdict = hidden_verdict(measured_, task) if measured_.is_dir() else {
        "passed": False, "exit_code": -1, "tail": ["no final state to measure"],
    }
    cell = {
        "task": task, "arm": arm, "repetition": one_run,
        "started_at": t0, "seconds": round(time.time() - t0, 1),
        "arm_detail": raw_,
        "harness_error": error,
        "hidden_suite": verdict,
        # The headline number: the arm said it was done and the hidden suite
        # disagrees. Only meaningful where the arm reported success at all.
        "false_accept": bool(raw_.get("accepted") or raw_.get("closed"))
                        and not verdict["passed"],
        "measured_tree": str(measured_),
        "budget": DISPATCH_BUDGET,
        # The protocol's exclusion rule. An arm that changed nothing never
        # produced a final state, and a cell like that says nothing about the
        # method -- but it is kept, reported, and named.
        "produced_final_state": (
            measured_.is_dir() and not unchanged_(measured_, task)
        ),
    }
    target.write_text(json.dumps(cell, indent=2) + "\n")
    print(json.dumps({k: cell[k] for k in
                      ("task", "arm", "repetition", "false_accept", "seconds")}))
    print("  hidden suite:", "PASS" if verdict["passed"] else "FAIL",
          verdict["tail"][-1] if verdict["tail"] else "")
    if error:
        print("  harness error:", error)
    return 0


def _comparison(cells: list[dict]) -> list[str]:
    """Each replicated cell against its v1 counterpart, and nothing else.

    The protocol allows exactly this comparison: the cells present on **both**
    sides. A replication that reported its own cells next to v1's totals would
    be comparing a subset with a whole, which is the thing the protocol was
    frozen to prevent.
    """
    old_dir = CAMPAIGNS["v1"]
    if not old_dir.is_dir():
        return ["Campaign v1's cells are not in this checkout, so no",
                "comparison is possible here."]
    old = {}
    for f in sorted(old_dir.glob("*.json")):
        if ".attempt" in f.name or _parked(f):
            continue
        z = json.loads(f.read_text())
        old[(z["task"], z["arm"])] = z
    lines = [
        "| task | arm | v1 final state | v2 final state | v1 hidden | v2 hidden |",
        "|---|---|---|---|---|---|",
    ]
    pairs = 0
    for z in sorted(cells, key=lambda x: (x["task"], x["arm"])):
        a = old.get((z["task"], z["arm"]))
        if a is None:
            continue
        pairs += 1
        def f(cell, field_):
            if field_ == "final":
                return "yes" if cell.get("produced_final_state") else "**no**"
            # The protocol excludes a cell with no final state from the
            # correctness comparison, so its suite result is not shown as one.
            # It ran -- arm C's tree exists whatever the orchestrator did --
            # and it measured a state the arm never finished, which is a
            # different thing from a failed candidate.
            if not cell.get("produced_final_state"):
                return "(excluded)"
            return "PASS" if cell["hidden_suite"]["passed"] else "FAIL"
        lines.append(
            f"| `{z['task']}` | {z['arm']} | {f(a, 'final')} | {f(z, 'final')} "
            f"| {f(a, 'hidden')} | {f(z, 'hidden')} |")
    if not pairs:
        return ["No cell of this campaign has a v1 counterpart yet."]
    lines += [
        "",
        f"{pairs} cell(s) appear on both sides. Cells that ran in only one",
        "campaign are not in this table and are not counted anywhere in it.",
        "",
        "`(excluded)` is the protocol's own rule, not a missing number: an arm",
        "that produced no final state says nothing about the method, so its",
        "suite result is not reported as a correctness outcome. The suite did",
        "run -- arm C's tree exists whatever the orchestrator did -- and what",
        "it measured was a state the arm never finished.",
    ]
    return lines


def _kostenzeilen(cells: list[dict]) -> list[str]:
    out_list = []
    # Sorted and labelled by repetition. Without the repetition column three
    # cells of the same (task, arm) that spent the same number printed three
    # byte-identical rows: a reader could not tell which cell a row was about,
    # and neither could the claims ledger, which refused to anchor them
    # (AMBIGUOUS). Three indistinguishable rows are not three measurements.
    for z in sorted(cells,
                    key=lambda x: (x["task"], x["arm"], x.get("repetition", 1))):
        d = z.get("arm_detail") or {}
        counted = d.get("dispatches_recounted")
        recorded_ = d.get("dispatches_as_recorded", d.get("dispatches"))
        # A cell that carries no `dispatch_budget` predates the counting: its
        # figure came from `min(iterations * 3, DISPATCH_BUDGET)` and is an
        # assertion, not a measurement. Saying so in the cell that shows it is
        # cheaper than a footnote nobody reads.
        claimed_ = not counted and "dispatch_budget" not in d
        if counted:
            number_ = f"{counted} (recorded as {recorded_})"
        elif recorded_ is None:
            number_ = "-"
        else:
            number_ = f"{recorded_}" + (" (asserted, not counted)" if claimed_ else "")
        budget = d.get("dispatch_budget", DISPATCH_BUDGET)
        genuinely = counted if counted else recorded_
        above_it = ("unknown" if claimed_
                   else "yes" if isinstance(genuinely, int) and genuinely > budget
                   else "no" if isinstance(genuinely, int) else "-")
        out_list.append(f"| `{z['task']}` | {z['arm']} | {z.get('repetition', 1)} "
                    f"| {number_} | {budget} | {above_it} |")
    return out_list


def _campaign_head(campaign_: str) -> list[str]:
    """What a reader has to know about this campaign before the tables."""
    if campaign_ == "v1":
        return [
            "## Campaign `v1`, status `PRE-O125-CLOSURE`",
            "",
            "This campaign ran **before** the planner capability boundary (O125,",
            "O126) and before the blocked-pane misreading (O127) were fixed. It is",
            "kept exactly as it was measured: no cell was re-run, no number was",
            "repaired, and no cell was reinterpreted in the light of what was",
            "learned afterwards. A benchmark edited after its result is known is",
            "not a benchmark.",
            "",
            "**Nothing here says VeriHarness performed better.** Arm A -- a plain",
            "agent with no control plane -- produced the most passing cells. The",
            "reason the C cells stopped is named per cell below, and in four of",
            "five it is the same reason: a planner pane that had finished was read",
            "as waiting for an approval. That is O127, a defect in this harness.",
            "",
            "That attribution is **measured, not inferred**. The four cells' run",
            "trees still hold a valid plan in `answers/i1-a0-planner.json` -- 5,",
            "6, 4 and 3 acceptance criteria respectively -- written while the run",
            "state says `PLANNING / BLOCKED / planner waits for an approval`. The",
            "planner had delivered and the harness reported it as waiting.",
            "",
            "A replication under a new campaign id is the only thing that may",
            "change these numbers, and `docs/BENCHMARK_PROTOCOL.md` fixes its",
            "shape before it runs.",
            "",
        ]
    if campaign_ == "v3":
        return [
            "## Campaign `v3`, pre-registered in full before the first dispatch",
            "",
            "The design was frozen in `docs/BENCHMARK_PROTOCOL.md` and bound to",
            "a commit before any cell ran: 5 tasks x 3 arms x 3 repetitions,",
            "unconditionally, with nine role dispatches per **cell** -- shared",
            "by a node and its repair nodes, and enforced rather than",
            "described. `docs/benchmarks/v3/PREREGISTRATION.json` carries the",
            "digests and the commit; `PREREGISTRATION_PROVENANCE.json` carries",
            "the check that those exact bytes were committed before the first",
            "dispatch, derived from git rather than from the order of a log.",
            "",
            "**Arm C reached no fixpoint in this campaign.** All fifteen arm-C",
            "cells end `BUDGET_EXHAUSTED` and none closes: the primary node",
            "spends the nine, the gate is not green, the repair node it spawns",
            "is refused for want of budget. Campaign v2's arm C closed five of",
            "five -- with a separate nine per *run*, so eighteen per cell. That",
            "closure was bought with twice the declared resource, and this is",
            "what the same control plane does under the rule the protocol",
            "actually writes down. It is a finding about this harness, and it",
            "belongs next to every arm-C number below.",
            "",
            "**Arm C's zero false accepts is therefore not a success.** A false",
            "accept requires the arm to say it is finished. Arm C never says",
            "it. `slug_pair/C/3` shows the difference cleanly: the hidden suite",
            "is red and the arm made no claim -- a miss, not a false pass.",
            "Comparing B's false-accept count with C's without that sentence",
            "would compare \"answered wrongly once\" against \"never answered\".",
            "",
            "**The one false accept reproduced v2's.** `slug_pair`, arm B,",
            "repetition 1: its own criteria green, the hidden suite red -- the",
            "same task, the same arm, the same shape as the single false accept",
            "campaign v2 found. One of three repetitions. n=3 carries no rate;",
            "what it carries is that v2's finding was not a one-off.",
            "",
            "**And the arms do not spend the budget symmetrically.** Arm A is",
            "single-shot by construction -- one provider call per cell, not",
            "nine. That is declared in the protocol and in `docs/LIMITATIONS.md`",
            "limit 17, and it points the inconvenient way: more turns could",
            "only help arm A, so a campaign in which A matches the harnessed",
            "arms is robust to it, and one in which A lost would not be",
            "evidence for the harness.",
            "",
        ]
    return [
        f"## Campaign `{campaign_}`, the declared replication",
        "",
        "**Read arm C's closures with O137 beside them.** The global gate runs",
        "`unittest discover` over the merged state, and every task's base is a",
        "single source file with no test -- so the gate's first result is RED",
        "with `NO TESTS RAN`, which is the signature this project treats as",
        "\"nothing executed\". The repair node it triggers does real work and",
        "the closure that follows is real. What arm C demonstrates in these",
        "cells is that it reacts to a red gate, repairs, and reaches a fixpoint",
        "without a human -- not that it repaired a regression anybody found.",
        "",
        "The protocol fixed this campaign's shape before it ran: the same five",
        "tasks, the same hidden suites, the same budget rule, the same outcome",
        "metrics, the same exclusion rule, and one named intervention -- the",
        "planner capability boundary and the two defects found while closing",
        "it. Campaign v1 is untouched and is reported separately.",
        "",
        "**A partial campaign is reported as partial.** The cells that ran are",
        "named, the ones that did not are named, and only the cells present on",
        "both sides may be compared with v1. Reporting a subset as if it were",
        "the campaign is the thing the protocol was frozen to prevent.",
        "",
        "**What a green replication would and would not license** is in the",
        "protocol and is not renegotiable here: it would license a sentence",
        "about how many final states each arm produced in this campaign. It",
        "would not license a claim that this harness performs better.",
        "",
    ]


def _required_repetitions(campaign_: str) -> int:
    """How many repetitions this campaign's own frozen text requires.

    Read, not typed: a campaign that declares a different number must not be
    reported against a constant in this file. Falls back to 1 where the
    frozen text does not decide, which is the historical case (v1, v2).
    """
    try:
        import importlib.util as _il

        spec = _il.spec_from_file_location(
            "repetition_plan", HERE / "repetition_plan.py")
        rp = _il.module_from_spec(spec)
        spec.loader.exec_module(rp)
        text = rp.frozen_text(rp.frozen_protocol_commit(campaign_))
        n = rp.repetition_requirement(text).get("required_repetitions")
        return int(n) if n else 1
    except Exception:                              # pragma: no cover - exotic
        return 1


def _cells_grouped(cells: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """(task, arm) -> its repetitions, in order.

    It was `{(task, arm): cell}`, a dict comprehension over the same list --
    which silently keeps the **last** repetition and drops the rest. That was
    invisible while every cell had exactly one repetition and would have
    reported one third of campaign v3.
    """
    out_list: dict[tuple[str, str], list[dict]] = {}
    for z in cells:
        out_list.setdefault((z.get("task"), z.get("arm")), []).append(z)
    for lookup_key in out_list:
        out_list[lookup_key].sort(key=lambda z: z.get("repetition", 1))
    return out_list


#: What a repetition records, in the order the protocol's §"What is measured"
#: lists it. The value is either a reader for the cell or `None`, meaning the
#: field does not exist for that arm -- and an absent field is printed as `-`,
#: never as 0. A zero that means "not applicable" cannot be told apart from a
#: zero that was measured.
def _end_state(z: dict) -> str:
    d = z.get("arm_detail") or {}
    if z.get("harness_error"):
        return "harness error"
    if "halt" in d:                                  # arm C
        return str(d["halt"]).replace("HaltClass.", "")
    if "accepted" in d:                              # arm B
        return "accepted" if d["accepted"] else "not accepted"
    if d.get("error"):                               # arm A, provider failed
        return "provider error"
    return "answered" if d.get("answer_chars") else "no answer"


def _budget_exhausted(z: dict) -> bool:
    d = z.get("arm_detail") or {}
    text = " ".join(str(x) for x in
                    (d.get("halt", ""), d.get("reason", ""),
                     " ".join(d.get("tail") or [])))
    return "BUDGET_EXHAUSTED" in text


def _mehrdeutig(z: dict) -> bool:
    d = z.get("arm_detail") or {}
    return "AMBIGUOUS" in str(d.get("halt", ""))


def _providerfehler(z: dict) -> bool:
    d = z.get("arm_detail") or {}
    return bool(z.get("harness_error") or d.get("error"))


def _wiederholungszeilen(cells: list[dict]) -> list[str]:
    """One row per repetition, with everything the protocol says to record."""
    out_list = []
    for (task, arm), group_ in sorted(_cells_grouped(cells).items()):
        for z in group_:
            d = z.get("arm_detail") or {}
            reparaturen = d.get("repair_nodes")
            humans = d.get("human_decisions")
            out_list.append(
                f"| `{task}` | {arm} | {z.get('repetition', 1)} "
                f"| {_end_state(z)} "
                f"| {'PASS' if (z.get('hidden_suite') or {}).get('passed') else 'FAIL'} "
                f"| {'yes' if z.get('false_accept') else 'no'} "
                f"| {d.get('dispatches', '-')} "
                f"| {'yes' if _budget_exhausted(z) else 'no'} "
                f"| {len(reparaturen) if reparaturen is not None else '-'} "
                f"| {humans if humans is not None else '-'} "
                f"| {d.get('wallclock_seconds', z.get('seconds', '-'))} "
                f"| {'yes' if _providerfehler(z) else 'no'} "
                f"| {'yes' if _mehrdeutig(z) else 'no'} |")
    return out_list


def _aggregate_rows(cells: list[dict]) -> list[str]:
    """Per (task, arm) over its repetitions. Counts, never means.

    n=3 does not support a mean and the protocol says so: what is aggregated
    is how many repetitions passed, how many were false accepts, and the range
    of what they spent. A single averaged number over three runs would read as
    a measurement of the method.
    """
    out_list = []
    for (task, arm), group_ in sorted(_cells_grouped(cells).items()):
        measured_ = [z for z in group_ if z.get("produced_final_state", True)]
        passed_ = sum(1 for z in measured_
                        if (z.get("hidden_suite") or {}).get("passed"))
        wrong = sum(1 for z in group_ if z.get("false_accept"))
        outputs_ = [(z.get("arm_detail") or {}).get("dispatches")
                    for z in group_]
        outputs_ = [x for x in outputs_ if isinstance(x, int)]
        span = (f"{min(outputs_)}-{max(outputs_)}" if len(set(outputs_)) > 1
                  else str(outputs_[0]) if outputs_ else "-")
        exhausted_ = sum(1 for z in group_ if _budget_exhausted(z))
        excluded_ = len(group_) - len(measured_)
        out_list.append(
            f"| `{task}` | {arm} | {len(group_)} | {passed_}/{len(measured_)} "
            f"| {wrong} | {span} | {exhausted_} | {excluded_} |")
    return out_list


def markdown(cells: list[dict], campaign_: str = "v1") -> str:
    """The results document the protocol asks for, from the cells that ran.

    It names the cells that did not run, in full. A results table that shows
    only the cells that exist is the shape of a benchmark whose author chose
    which ones to report -- and the protocol was frozen before any arm ran
    precisely so that could not happen.
    """
    tasks_ = sorted(p.name for p in TASKS.iterdir() if p.is_dir())
    # `{(task, arm): cell}` kept the **last** repetition of three and dropped
    # the rest, silently, and would have reported a third of campaign v3
    # (O154). The grouping is what the tables below read; `vorhanden` survives
    # only as "did this cell run at all".
    groups_ = _cells_grouped(cells)
    present = {k: v[-1] for k, v in groups_.items()}
    missing_ = [f"{t}/{a}" for t in tasks_ for a in "ABC"
               if (t, a) not in present]

    lines = [
        "# Matched-budget benchmark: results",
        "",
        f"Generated by `python3 tools/benchmark.py report --campaign {campaign_}"
        " --write` from",
        f"the cells in `{CAMPAIGNS[campaign_].relative_to(HOH)}/`. The protocol "
        "in `docs/BENCHMARK_PROTOCOL.md`",
        "was committed before the first arm ran and is not re-decidable here.",
        "",
        *(_campaign_head(campaign_)),
        f"**{len(cells)} of {len(tasks_) * 3} cells have run.**",
        "",
        *(["## Against campaign v1, cell by cell", "",
           *_comparison(cells), ""] if campaign_ != "v1" else []),
        "## What each cell cost, counted",
        "",
        "Provider calls, summed from the `provider_calls` field each dispatch",
        "record carries -- not a count of log lines. A line is not a call in",
        "either direction: a role that retried twice writes one line for three",
        "calls, and a role refused at the budget writes one line for none",
        "(O144). Where a cell was recorded before the figure was counted",
        "rather than asserted, both numbers are shown -- the recorded one is",
        "kept, and the file as it stood is parked beside the cell.",
        "",
        "| task | arm | rep | dispatches | budget | over budget |",
        "|---|---|---|---|---|---|",
        *(_kostenzeilen(cells)),
        "",
        "## Every repetition, as it ran",
        "",
        "One row per repetition, carrying what the protocol says to record. A",
        "field that does not exist for an arm is `-`, never 0: a zero that",
        "means \"not applicable\" cannot be told apart from one that was",
        "measured.",
        "",
        "| task | arm | rep | final state | hidden | false accept | dispatches "
        "| budget exhausted | repairs | human decisions | wallclock | provider "
        "failure | ambiguous |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        *(_wiederholungszeilen(cells)),
        "",
        "## Per (task, arm), over its repetitions",
        "",
        "Counts, never means. Three repetitions do not support an average, and",
        "a single averaged number would read as a measurement of the method.",
        "",
        "| task | arm | reps | hidden PASS | false accepts | dispatches "
        "| budget exhausted | excluded |",
        "|---|---|---|---|---|---|---|---|",
        *(_aggregate_rows(cells)),
        "",
        "## Final correctness, by the hidden suite",
        "",
        "Read over a cell's repetitions: `n/m PASS` is n of the m repetitions",
        "that produced a final state.",
        "",
        "| task | A: plain agent | B: one HoH run | C: full control plane |",
        "|---|---|---|---|",
    ]
    excluded_ = []
    for task in tasks_:
        fields_ = []
        for arm in "ABC":
            z = present.get((task, arm))
            if z is None:
                fields_.append("not run")
            elif not z.get("produced_final_state", True):
                fields_.append("no final state")
                excluded_.append(f"{task}/{arm}")
            else:
                fields_.append("PASS" if z["hidden_suite"]["passed"] else "FAIL")
        lines.append(f"| `{task}` | " + " | ".join(fields_) + " |")
    if excluded_:
        lines += [
            "",
            "**Excluded from the correctness comparison**, by the rule the "
            "protocol fixes",
            "in advance -- the arm never produced a final state, so the cell "
            "says nothing",
            "about the method. Kept, reported and named, with the reason each "
            "one stopped:",
            "",
            "| cell | why it produced no final state |",
            "|---|---|",
        ]
        for x in excluded_:
            task, arm = x.split("/")
            z = present[(task, arm)]
            a = z["arm_detail"]
            why = (a.get("reason") or a.get("error")
                     or (a.get("tail") or [""])[-1] or "not recorded")
            lines.append(f"| `{x}` | {why[:180].replace(chr(10), ' ')} |")

    lines += [
        "",
        "## False accepts",
        "",
        "The headline number: the arm reported success and the hidden suite",
        "disagrees. Only meaningful where the arm reported success at all --",
        "arm A has no acceptance step, so it never claims one.",
        "",
        "| task | A | B | C |",
        "|---|---|---|---|",
    ]
    for task in tasks_:
        fields_ = []
        for arm in "ABC":
            z = present.get((task, arm))
            fields_.append("not run" if z is None else str(z["false_accept"]))
        lines.append(f"| `{task}` | " + " | ".join(fields_) + " |")

    lines += ["", "## Cost", "", "| task | arm | seconds | dispatches |",
               "|---|---|---|---|"]
    for task in tasks_:
        for arm in "ABC":
            z = present.get((task, arm))
            if z is None:
                continue
            lines.append(
                f"| `{task}` | {arm} | {z['seconds']:.0f} | "
                f"{z['arm_detail'].get('dispatches', '?')} |"
            )

    lines += [
        "",
        "Tokens are not reported. The harness in use does not return usage for "
        "these",
        "dispatches, so the honest value is `NOT_DETERMINABLE` rather than a "
        "total over",
        "the runs that happened to report one.",
        "",
        "## Cells that did not run",
        "",
    ]
    lines.append(", ".join(f"`{f}`" for f in missing_) if missing_
                  else "None: every cell was attempted.")
    lines += [
        "",
        "## The one thing this run could not measure",
        "",
        "Four of the five arm-C cells, and one arm-B cell, stopped at an "
        "interactive",
        "permission prompt: the **planner** role ran shell commands and the "
        "harness gated",
        "them. Each was attempted five times, with the repository name, the run "
        "id and",
        "the trust registration made unique and verified in between; the cause "
        "did not",
        "change. It is a property of this machine's harness, not of the arms.",
        "",
        "Two things follow, and neither is comfortable:",
        "",
        "* **The B-vs-C comparison is not supported by this run.** One arm-C "
        "cell produced",
        "  a final state. A comparison of composition behaviour on one data "
        "point is not",
        "  a comparison, and it is not offered as one.",
        "* **The planner exceeded its own contract.** Its prompt says, in "
        "those words:",
        "  *implement nothing, edit nothing, test nothing*. The transcripts "
        "show it",
        "  running `mv` on the file under test and executing the acceptance "
        "criteria it",
        "  was drafting. The gate that stopped it is the harness's, not HoH's.",
        "",
        "## What these numbers do not support",
        "",
        "Everything `docs/BENCHMARK_PROTOCOL.md` lists under *What this cannot "
        "establish*",
        "still holds. In particular: five small tasks, one language, one "
        "machine, one",
        "provider, and repetition counts as shown. Where every arm passes a "
        "task, the",
        "honest reading is that the task does not discriminate between them --",
        "which is a finding about the benchmark, not a verdict on an arm.",
        "",
    ]
    return "\n".join(lines)


#: `<task>.<arm>.<rep>.v<UTC timestamp>.json` -- a cell as it stood before a
#: recount. Kept, never read as a cell: the file without the stamp is the cell.
_PARKED = re.compile(r"\.v\d{8}T\d{6}Z\.json$")


def _parked(f: Path) -> bool:
    return bool(_PARKED.search(f.name))


def cmd_recount(args) -> int:
    """Write the counted dispatch figure into cells recorded with the constant.

    The run trees live in a temp directory, so the number is re-derivable only
    until the machine clears it -- and it is the figure the campaign's own
    budget claim rests on. Carried into the cell **by addition**: the recorded
    value stays in `dispatches_as_recorded`, the counted one goes in
    `dispatches_recounted`, and the previous file is parked with a timestamp.
    Nothing is overwritten and nothing is invented; a cell whose tree is gone
    is left alone and reported as such.
    """
    source = results_for(args.campaign)
    if not source.is_dir():
        print("no cells have run")
        return 1
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    touched, skipped_ = [], []
    for f in sorted(source.glob("*.json")):
        if ".attempt" in f.name:
            continue
        z = json.loads(f.read_text())
        detail = z.get("arm_detail") or {}
        if "dispatches_recounted" in detail:
            continue
        measured_ = Path(z.get("measured_tree") or "")
        tree_ = measured_.parent / "root"
        # The tree has to be *this* cell's. A temp path is reusable, and a
        # figure taken from the wrong directory is worse than an old one.
        # Arm C records the repository directory (`bm-<task>-<arm><rep>-…`);
        # arms A and B record the *worktree* the harness made, whose name the
        # harness chooses. So the identity test accepts either the directory
        # itself or its parent temp directory (`bench-<task>-<arm><rep>-…`),
        # and still refuses a tree belonging to some other cell.
        expected = (f"bm-{z['task']}-{z['arm']}{z.get('repetition', 1)}-",
                    f"bench-{z['task']}-{z['arm']}{z.get('repetition', 1)}-")
        if not (measured_.name.startswith(expected)
                or measured_.parent.name.startswith(expected)):
            skipped_.append(
                f"{f.name} (its recorded tree {measured_.name!r} is not this cell's)")
            continue
        if not tree_.is_dir():
            skipped_.append(f"{f.name} (its run tree is gone)")
            continue
        real = counted_dispatches(tree_)
        if not real or real == detail.get("dispatches"):
            continue
        f.rename(f.with_name(f"{f.stem}.v{stamp}{f.suffix}"))
        detail["dispatches_as_recorded"] = detail.get("dispatches")
        detail["dispatches_recounted"] = real
        detail["recounted_at"] = stamp
        z["arm_detail"] = detail
        f.write_text(json.dumps(z, indent=2) + "\n")
        touched.append(f"{f.name}: {detail['dispatches_as_recorded']} -> {real}")
    for z in touched:
        print("recounted", z)
    for z in skipped_:
        print("skipped  ", z)
    if not touched and not skipped_:
        print("nothing to recount")
    return 0


def cmd_report(args) -> int:
    source = results_for(getattr(args, "campaign", "v1"))
    if not source.is_dir():
        print("no cells have run")
        return 1
    # `*.attemptN.v<timestamp>.json` are preserved earlier attempts at a cell,
    # kept because nothing here is deleted. They are not the cell: a re-run
    # replaces what the cell reports, and the attempt that did not deliver
    # stays on disk for anyone who wants to see what happened.
    cells = [json.loads(f.read_text()) for f in sorted(source.glob("*.json"))
              if ".attempt" not in f.name and not _parked(f)]
    # No opportunistic recount here. Reading whatever still sits at a cell's
    # recorded temp path attached a figure of 1 to a v1 arm-C cell whose tree
    # had long since been reused -- a number from the wrong directory is worse
    # than an old one. The recount is an explicit command that checks the
    # tree's identity and writes its result into the cell, by addition.
    tasks_ = sorted({z["task"] for z in cells})
    cells_total = len([p for p in TASKS.iterdir() if p.is_dir()]) * 3
    needed = _required_repetitions(getattr(args, "campaign", "v1"))
    print(f"{len(cells)} run(s) of {cells_total * needed} planned "
          f"({len(tasks_) or len(list(TASKS.iterdir()))} tasks x 3 arms x "
          f"{needed} repetition(s))")
    print()
    print(f"{'task':<18s} {'arm':<4s} {'rep':<4s} {'hidden':<8s} "
          f"{'false accept':<13s} {'dispatches':>11s} {'seconds':>8s}")
    for t in tasks_:
        for z in sorted([x for x in cells if x["task"] == t],
                        key=lambda x: (x["arm"], x.get("repetition", 1))):
            d = z.get("arm_detail") or {}
            print(f"{z['task']:<18s} {z['arm']:<4s} "
                  f"{z.get('repetition', 1):<4d} "
                  f"{'PASS' if z['hidden_suite']['passed'] else 'FAIL':<8s} "
                  f"{str(z['false_accept']):<13s} "
                  f"{str(d.get('dispatches', '-')):>11s} {z['seconds']:>8.0f}")
    missing_ = []
    for t in sorted(p.name for p in TASKS.iterdir() if p.is_dir()):
        for a in ("A", "B", "C"):
            if not any(z["task"] == t and z["arm"] == a for z in cells):
                missing_.append(f"{t}/{a}")
    print()
    print(f"cells not run: {len(missing_)}"
          + (" -- " + ", ".join(missing_) if missing_ else ""))
    if getattr(args, "write", False):
        # Each campaign writes its own document. v1's stays where every
        # reference to it points; a replication cannot overwrite the campaign
        # it replicates by forgetting a flag.
        campaign_ = getattr(args, "campaign", "v1")
        target = (HOH / "docs/BENCHMARK_RESULTS.md" if campaign_ == "v1"
                else HOH / f"docs/BENCHMARK_RESULTS_{campaign_}.md")
        target.write_text(markdown(cells, campaign_), encoding="utf-8")
        print(f"wrote {target}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--task", required=True)
    r.add_argument("--arm", required=True, choices=list("ABCabc"))
    r.add_argument("--rep", type=int, default=1)
    r.add_argument("--isolation", default="none")
    r.add_argument("--again", action="store_true")
    r.add_argument("--campaign", default="v1", choices=sorted(CAMPAIGNS),
                   help="which campaign's cells this belongs to. v1 is the "
                        "PRE-O125-CLOSURE campaign and is never re-run; v2 is "
                        "the replication the protocol declares in advance")
    r.set_defaults(fn=cmd_run)
    b = sub.add_parser("report")
    b.add_argument("--write", action="store_true",
                   help="also write docs/BENCHMARK_RESULTS.md")
    b.add_argument("--campaign", default="v1", choices=sorted(CAMPAIGNS))
    b.set_defaults(fn=cmd_report)
    rc = sub.add_parser(
        "recount",
        help="carry the counted dispatch figure into cells recorded with the "
             "old constant, by addition")
    rc.add_argument("--campaign", default="v2", choices=sorted(CAMPAIGNS))
    rc.set_defaults(fn=cmd_recount)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
