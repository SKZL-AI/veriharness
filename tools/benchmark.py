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
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HIER = Path(__file__).resolve().parent
HOH = HIER.parent
sys.path.insert(0, str(HOH / "src"))

AUFGABEN = HOH / "dogfood/benchmark/tasks"
ERGEBNISSE = HOH / "dogfood/benchmark/results"

#: Where this deployment's trust helper lives, and where its harness puts
#: worktrees. Read from the environment with **no default**, for the reason
#: `approval.py` gives about its own script path: a hardcoded location makes
#: one machine's layout a product requirement. An earlier version of this file
#: spelled both out, and the export's leak scan found them -- correctly, since
#: publishing them would have published this machine's layout too.
TRUST_HELPER_ENV = "HOH_TRUST_HELPER"
WORKTREE_ROOT_ENV = "HOH_WORKTREE_ROOT"


def _konfiguriert(name: str) -> Path:
    wert = os.environ.get(name, "").strip()
    if not wert:
        raise SystemExit(
            f"{name} is not set. The benchmark dispatches real agents into "
            "worktrees, which needs this deployment's trust helper and the "
            "directory its harness puts worktrees in. There is deliberately no "
            "default: a hardcoded path would make one machine's layout a "
            "requirement of the tool."
        )
    return Path(wert).expanduser()


#: Fixed by the protocol. Not a parameter: making it one is how a matched
#: budget stops being matched.
DISPATCH_BUDGET = 9


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def arbeitsbaum(task: str, arm: str, lauf: int) -> Path:
    """A fresh repository at the task's base state, named for its cell."""
    basis = Path(tempfile.mkdtemp(prefix=f"bench-{task}-{arm}{lauf}-"))
    repo = basis / f"bm-{task}-{arm}{lauf}"
    repo.mkdir(parents=True)
    for f in sorted((AUFGABEN / task / "base").iterdir()):
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


def hidden_verdict(repo: Path, task: str) -> dict:
    """Runs the hidden suite against the arm's final state, in a copy.

    A copy, because the suite must not become part of the tree an arm is
    measured on -- and because a second run of the same cell has to measure the
    same thing.
    """
    with tempfile.TemporaryDirectory(prefix="bench-verdict-") as d:
        ziel = Path(d)
        for f in repo.iterdir():
            if f.is_file() and f.suffix == ".py":
                shutil.copy2(f, ziel / f.name)
        shutil.copy2(AUFGABEN / task / "hidden_test.py", ziel / "hidden_test.py")
        p = subprocess.run(
            [sys.executable, "-m", "unittest", "hidden_test", "-v"],
            cwd=str(ziel), capture_output=True, text=True, timeout=300,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": str(ziel), "PYTHONDONTWRITEBYTECODE": "1"},
        )
    text = p.stdout + p.stderr
    return {
        "passed": p.returncode == 0,
        "exit_code": p.returncode,
        "failures": text.count("FAIL:"),
        "errors": text.count("ERROR:"),
        "tail": text.strip().splitlines()[-3:],
    }


# --------------------------------------------------------------------------- #
# Arm A: a plain agent, no harness
# --------------------------------------------------------------------------- #


def arm_a(task: str, repo: Path, lauf: int) -> dict:
    """One agent, the specification, the repository, and nothing else.

    Deliberately the baseline anybody already has: no acceptance checks, no
    receipts, no second opinion. Its budget is the same nine dispatches, spent
    as nine turns.
    """
    from hoh import herdr
    from hoh.contracts import Role
    from hoh.dispatchers import build_dispatcher

    spec = (AUFGABEN / task / "SPEC.md").read_text()
    antworten = repo.parent / "answers"
    antworten.mkdir(exist_ok=True)
    herdr.require_herdr()
    d = build_dispatcher(
        answers_dir=antworten,
        profiles={Role.DEVELOPER: "claude"},
        cwd=repo,
        prefer_herdr=True,
        timeout_ms=1800 * 1000,
    )

    class Zustand:
        run_id = f"bma-{task[:8]}-{lauf}"
        iteration = 1
        attempt = 1

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
    fehler = ""
    try:
        antwort = d.dispatch(Role.DEVELOPER, prompt, state=Zustand())
    except Exception as exc:
        antwort, fehler = "", f"{type(exc).__name__}: {exc}"
    dauer = time.monotonic() - t0
    try:
        d.close_own()
    except Exception:
        pass
    return {
        "dispatches": 1,
        "wallclock_seconds": round(dauer, 1),
        "answer_chars": len(antwort or ""),
        "error": fehler,
    }


# --------------------------------------------------------------------------- #
# Arm B: one HoH run
# --------------------------------------------------------------------------- #


def arm_b(task: str, repo: Path, lauf: int, isolation: str) -> dict:
    root = repo.parent / "root"
    run_id = f"bmb{task[:8]}{lauf}".replace("_", "")
    umgebung = dict(os.environ, PYTHONPATH=str(HOH / "src"))
    spec = AUFGABEN / task / "SPEC.md"

    wt = subprocess.run(
        [sys.executable, "-m", "hoh.cli", "--root", str(root), "worktree",
         "--repo", str(repo), "--branch", f"bench-{run_id}"],
        capture_output=True, text=True, env=umgebung, timeout=300,
    )
    worktree = _worktree_aus(wt.stdout) or repo
    subprocess.run(
        [str(_konfiguriert(TRUST_HELPER_ENV)), str(worktree), str(repo)],
        capture_output=True, text=True, timeout=120,
    )
    st = subprocess.run(
        [sys.executable, "-m", "hoh.cli", "--root", str(root), "start",
         "--repo", str(worktree), "--spec", str(spec), "--run-id", run_id,
         "--max-iterations", "3"],
        capture_output=True, text=True, env=umgebung, timeout=300,
    )
    if st.returncode != 0:
        return {"dispatches": 0, "error": (st.stderr or st.stdout)[-300:]}

    t0 = time.monotonic()
    r = subprocess.run(
        [sys.executable, "-m", "hoh.cli", "--root", str(root), "run", run_id,
         "--iterations", "3", "--planner", "claude", "--developer", "claude",
         "--qa", "claude", "--isolation", isolation],
        capture_output=True, text=True, env=umgebung, timeout=5400,
    )
    dauer = time.monotonic() - t0
    zustand = _run_state(root / run_id / "state.json")
    # Three roles per iteration is the budget's unit.
    iterationen = zustand.get("iteration", 0)
    return {
        "dispatches": min(iterationen * 3, DISPATCH_BUDGET),
        "iterations": iterationen,
        "wallclock_seconds": round(dauer, 1),
        "accepted": bool(zustand.get("last_accepted_candidate")),
        "receipts": len(list((root / run_id / "receipts").glob("*.json")))
        if (root / run_id / "receipts").is_dir() else 0,
        "worktree": str(worktree),
        "root": str(root),
        "run_id": run_id,
        "cli_exit": r.returncode,
        "tail": (r.stdout or r.stderr).strip().splitlines()[-3:],
    }


def _worktree_aus(stdout: str) -> Path | None:
    try:
        d = json.loads(stdout)
    except ValueError:
        return None
    stapel = [d]
    while stapel:
        k = stapel.pop()
        if isinstance(k, dict):
            if "checkout_path" in k:
                return Path(k["checkout_path"])
            stapel.extend(k.values())
        elif isinstance(k, list):
            stapel.extend(k)
    return None


def _run_state(pfad: Path) -> dict:
    try:
        return json.loads(pfad.read_text())
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
# Arm C: the full control plane
# --------------------------------------------------------------------------- #


def arm_c(task: str, repo: Path, lauf: int, isolation: str) -> dict:
    """One node, the repository's own gates over the merged state, closure.

    On a single-node task C differs from B only by the global gates and the
    repair cycle they can trigger. That is the point: the protocol says the
    B-vs-C comparison is only meaningful on the tasks where composition exists,
    and reporting it on the others would be reporting noise.
    """
    from hoh.approval import PrefixScopedProvider
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
    spec = AUFGABEN / task / "SPEC.md"

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

    store = ProjectStore(root, f"bmc{lauf}")
    if not store.exists():
        store.write_state(ProjectState(
            project_id=f"bmc{lauf}",
            repo_path=str(repo),
            nodes=[TaskNode(
                id=f"n{task[:10]}".replace("_", ""),
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
        approvals=PrefixScopedProvider(
            script=_konfiguriert(TRUST_HELPER_ENV),
            prefix=_konfiguriert(WORKTREE_ROOT_ENV) / repo.name,
        ),
    )
    t0 = time.monotonic()
    ergebnis = ProjectController(store, starter, Gates(repo)).run()
    dauer = time.monotonic() - t0
    nach = store.read_state()
    return {
        "dispatches": min(3 * 3, DISPATCH_BUDGET),
        "wallclock_seconds": round(dauer, 1),
        "halt": str(ergebnis.halt),
        "reason": ergebnis.reason[:200],
        "rc_closed": nach.rc_closed(),
        "closure_generation": nach.closure_generation,
        "repair_nodes": [n.id for n in nach.nodes if n.repair_of],
        "human_decisions": len(
            [d for d in nach.decisions if d.actor not in ("orchestrator", None)]
        ),
        "steps": [f"{s.kind}:{s.node or ''}" for s in ergebnis.steps],
        "closed": ergebnis.halt is HaltClass.CLOSED,
    }


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #


def cmd_run(args) -> int:
    task, arm, lauf = args.task, args.arm.upper(), args.rep
    if not (AUFGABEN / task).is_dir():
        print(f"no such task: {task}", file=sys.stderr)
        return 2
    ERGEBNISSE.mkdir(parents=True, exist_ok=True)
    ziel = ERGEBNISSE / f"{task}.{arm}.{lauf}.json"
    if ziel.exists() and not args.again:
        print(f"cell already run: {ziel.name}", file=sys.stderr)
        return 0

    repo = arbeitsbaum(task, arm, lauf)
    t0 = time.time()
    try:
        if arm == "A":
            roh = arm_a(task, repo, lauf)
            gemessen = repo
        elif arm == "B":
            roh = arm_b(task, repo, lauf, args.isolation)
            gemessen = Path(roh.get("worktree") or repo)
        elif arm == "C":
            roh = arm_c(task, repo, lauf, args.isolation)
            gemessen = repo
        else:
            print(f"unknown arm: {arm}", file=sys.stderr)
            return 2
        fehler = ""
    except Exception as exc:
        roh, gemessen, fehler = {}, repo, f"{type(exc).__name__}: {exc}"

    verdikt = hidden_verdict(gemessen, task) if gemessen.is_dir() else {
        "passed": False, "exit_code": -1, "tail": ["no final state to measure"],
    }
    zelle = {
        "task": task, "arm": arm, "repetition": lauf,
        "started_at": t0, "seconds": round(time.time() - t0, 1),
        "arm_detail": roh,
        "harness_error": fehler,
        "hidden_suite": verdikt,
        # The headline number: the arm said it was done and the hidden suite
        # disagrees. Only meaningful where the arm reported success at all.
        "false_accept": bool(roh.get("accepted") or roh.get("closed"))
                        and not verdikt["passed"],
        "measured_tree": str(gemessen),
        "budget": DISPATCH_BUDGET,
    }
    ziel.write_text(json.dumps(zelle, indent=2) + "\n")
    print(json.dumps({k: zelle[k] for k in
                      ("task", "arm", "repetition", "false_accept", "seconds")}))
    print("  hidden suite:", "PASS" if verdikt["passed"] else "FAIL",
          verdikt["tail"][-1] if verdikt["tail"] else "")
    if fehler:
        print("  harness error:", fehler)
    return 0


def cmd_report(args) -> int:
    if not ERGEBNISSE.is_dir():
        print("no cells have run")
        return 1
    zellen = [json.loads(f.read_text()) for f in sorted(ERGEBNISSE.glob("*.json"))]
    aufgaben = sorted({z["task"] for z in zellen})
    print(f"{len(zellen)} cell(s) run of {len(list(AUFGABEN.iterdir())) * 3} "
          "(5 tasks x 3 arms, one repetition each)")
    print()
    print(f"{'task':<18s} {'arm':<4s} {'hidden':<8s} {'false accept':<13s} {'seconds':>8s}")
    for t in aufgaben:
        for z in [x for x in zellen if x["task"] == t]:
            print(f"{z['task']:<18s} {z['arm']:<4s} "
                  f"{'PASS' if z['hidden_suite']['passed'] else 'FAIL':<8s} "
                  f"{str(z['false_accept']):<13s} {z['seconds']:>8.0f}")
    fehlend = []
    for t in sorted(p.name for p in AUFGABEN.iterdir() if p.is_dir()):
        for a in ("A", "B", "C"):
            if not any(z["task"] == t and z["arm"] == a for z in zellen):
                fehlend.append(f"{t}/{a}")
    print()
    print(f"cells not run: {len(fehlend)}"
          + (" -- " + ", ".join(fehlend) if fehlend else ""))
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
    r.set_defaults(fn=cmd_run)
    b = sub.add_parser("report")
    b.set_defaults(fn=cmd_report)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
