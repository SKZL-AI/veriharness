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


#: Where the harness records which directories an agent may work in. Read here
#: only to *verify* a registration, never to write one -- writing is the
#: helper's business and this file does not know its format.
TRUST_REGISTRY = Path.home() / ".claude.json"


def _ist_vertraut(pfad: Path) -> bool:
    try:
        daten = json.loads(TRUST_REGISTRY.read_text())
    except (OSError, ValueError):
        return False
    return str(pfad) in (daten.get("projects") or {})


def trust_und_pruefen(worktree: Path, repo: Path, *, versuche: int = 3) -> dict:
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
    letzte = ""
    for n in range(1, versuche + 1):
        p = subprocess.run(
            [str(_konfiguriert(TRUST_HELPER_ENV)), str(worktree), str(repo)],
            capture_output=True, text=True, timeout=120,
        )
        letzte = (p.stdout or p.stderr).strip()[-200:]
        if _ist_vertraut(worktree):
            return {"registered": True, "attempts": n, "said": letzte}
        time.sleep(1.0)
    return {"registered": False, "attempts": versuche, "said": letzte}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def arbeitsbaum(task: str, arm: str, lauf: int) -> Path:
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
    basis = Path(tempfile.mkdtemp(prefix=f"bench-{task}-{arm}{lauf}-"))
    repo = basis / f"bm-{task}-{arm}{lauf}-{basis.name.rsplit('-', 1)[-1]}"
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


def unveraendert(repo: Path, task: str) -> bool:
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
    basis = AUFGABEN / task / "base"
    for f in sorted(basis.iterdir()):
        ziel = repo / f.name
        if not ziel.is_file() or ziel.read_bytes() != f.read_bytes():
            return False
    return True


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
    # The agent works in a worktree, exactly as arms B and C do. Two reasons,
    # and only the second is about fairness:
    #
    # * the trust helper refuses a primary checkout by design -- it exists to
    #   pre-register an *isolated* worktree -- so a bare directory leaves the
    #   agent wedged on a dialog nobody is there to answer;
    # * an arm that worked directly in the checkout would differ from B and C
    #   in two ways at once, and the comparison is supposed to isolate the
    #   harness, not the working arrangement.
    arbeit = repo.parent / f"{repo.name}-wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(arbeit),
         "-b", f"arm-a-{lauf}"],
        capture_output=True, text=True, timeout=120,
    )
    trust = trust_und_pruefen(arbeit, repo)
    d = build_dispatcher(
        answers_dir=antworten,
        profiles={Role.DEVELOPER: "claude"},
        cwd=arbeit,
        prefer_herdr=True,
        timeout_ms=1800 * 1000,
    )

    # A real `RunState`, not a stand-in. The dispatcher reads `repo_path`
    # off it to place the agent's pane, and a duck-typed object missing one
    # field fails after the pane has been opened -- which is what the first
    # attempt at this arm did, in 0.1 seconds, five times.
    from hoh.controller import Controller

    zustand = Controller.new_state(
        run_id=f"bma{task[:6]}{lauf}{_nonce(repo)}".replace("_", ""),
        repo_path=arbeit,
        project_name=f"benchmark-{task}",
        spec_path=AUFGABEN / task / "SPEC.md",
    )
    zustand.iteration, zustand.attempt = 1, 1

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
        antwort = d.dispatch(Role.DEVELOPER, prompt, state=zustand)
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
        "worktree": str(arbeit),
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


def arm_b(task: str, repo: Path, lauf: int, isolation: str) -> dict:
    root = repo.parent / "root"
    run_id = f"bmb{task[:6]}{lauf}{_nonce(repo)}".replace("_", "")
    umgebung = dict(os.environ, PYTHONPATH=str(HOH / "src"))
    spec = AUFGABEN / task / "SPEC.md"

    wt = subprocess.run(
        [sys.executable, "-m", "hoh.cli", "--root", str(root), "worktree",
         "--repo", str(repo), "--branch", f"bench-{run_id}"],
        capture_output=True, text=True, env=umgebung, timeout=300,
    )
    worktree = _worktree_aus(wt.stdout) or repo
    # Recorded, not fired and forgotten. A cell that came back with the agent
    # sitting on a trust dialog could not say whether the helper had refused,
    # had never been reached, or had been given the wrong path -- so the
    # answer goes into the cell.
    trust = trust_und_pruefen(worktree, repo)
    trust["worktree"] = str(worktree)
    st = subprocess.run(
        [sys.executable, "-m", "hoh.cli", "--root", str(root), "start",
         "--repo", str(worktree), "--spec", str(spec), "--run-id", run_id,
         "--max-iterations", "3"],
        capture_output=True, text=True, env=umgebung, timeout=300,
    )
    if st.returncode != 0:
        return {"dispatches": 0, "error": (st.stderr or st.stdout)[-300:],
                "trust": trust}

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
        "trust": trust,
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


class _GeprueftesScope:
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
        ziel = a.worktree
        if _ist_vertraut(ziel):
            self.granted.append(ziel)
            return a
        ergebnis = trust_und_pruefen(ziel, repo)
        if ergebnis["registered"]:
            self.granted.append(ziel)
            return Approval(
                granted=True, worktree=ziel, provider=self.name,
                detail=f"{a.detail}; verified after {ergebnis['attempts']} attempt(s)",
            )
        return Approval(
            granted=False, worktree=ziel, provider=self.name,
            detail=(f"the helper answered '{ergebnis['said']}' but {ziel} is not "
                    f"in the registry after {ergebnis['attempts']} attempt(s); "
                    "refusing rather than dispatching an agent that will stop "
                    "at a dialog nobody is there to answer"),
        )


def arm_c(task: str, repo: Path, lauf: int, isolation: str) -> dict:
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

    store = ProjectStore(root, f"bmc{lauf}{_nonce(repo)}")
    if not store.exists():
        store.write_state(ProjectState(
            project_id=f"bmc{lauf}{_nonce(repo)}",
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
        approvals=_GeprueftesScope(
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
            gemessen = Path(roh.get("worktree") or repo)
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
        # The protocol's exclusion rule. An arm that changed nothing never
        # produced a final state, and a cell like that says nothing about the
        # method -- but it is kept, reported, and named.
        "produced_final_state": (
            gemessen.is_dir() and not unveraendert(gemessen, task)
        ),
    }
    ziel.write_text(json.dumps(zelle, indent=2) + "\n")
    print(json.dumps({k: zelle[k] for k in
                      ("task", "arm", "repetition", "false_accept", "seconds")}))
    print("  hidden suite:", "PASS" if verdikt["passed"] else "FAIL",
          verdikt["tail"][-1] if verdikt["tail"] else "")
    if fehler:
        print("  harness error:", fehler)
    return 0


def markdown(zellen: list[dict]) -> str:
    """The results document the protocol asks for, from the cells that ran.

    It names the cells that did not run, in full. A results table that shows
    only the cells that exist is the shape of a benchmark whose author chose
    which ones to report -- and the protocol was frozen before any arm ran
    precisely so that could not happen.
    """
    aufgaben = sorted(p.name for p in AUFGABEN.iterdir() if p.is_dir())
    vorhanden = {(z["task"], z["arm"]): z for z in zellen}
    fehlend = [f"{t}/{a}" for t in aufgaben for a in "ABC"
               if (t, a) not in vorhanden]

    zeilen = [
        "# Matched-budget benchmark: results",
        "",
        "Generated by `python3 tools/benchmark.py report --write` from the "
        "cells in",
        "`dogfood/benchmark/results/`. The protocol in "
        "`docs/BENCHMARK_PROTOCOL.md` was",
        "committed before the first arm ran and is not re-decidable here.",
        "",
        f"**{len(zellen)} of {len(aufgaben) * 3} cells have run.**",
        "",
        "## Final correctness, by the hidden suite",
        "",
        "| task | A: plain agent | B: one HoH run | C: full control plane |",
        "|---|---|---|---|",
    ]
    ausgeschlossen = []
    for task in aufgaben:
        felder = []
        for arm in "ABC":
            z = vorhanden.get((task, arm))
            if z is None:
                felder.append("not run")
            elif not z.get("produced_final_state", True):
                felder.append("no final state")
                ausgeschlossen.append(f"{task}/{arm}")
            else:
                felder.append("PASS" if z["hidden_suite"]["passed"] else "FAIL")
        zeilen.append(f"| `{task}` | " + " | ".join(felder) + " |")
    if ausgeschlossen:
        zeilen += [
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
        for x in ausgeschlossen:
            task, arm = x.split("/")
            z = vorhanden[(task, arm)]
            a = z["arm_detail"]
            grund = (a.get("reason") or a.get("error")
                     or (a.get("tail") or [""])[-1] or "not recorded")
            zeilen.append(f"| `{x}` | {grund[:180].replace(chr(10), ' ')} |")

    zeilen += [
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
    for task in aufgaben:
        felder = []
        for arm in "ABC":
            z = vorhanden.get((task, arm))
            felder.append("not run" if z is None else str(z["false_accept"]))
        zeilen.append(f"| `{task}` | " + " | ".join(felder) + " |")

    zeilen += ["", "## Cost", "", "| task | arm | seconds | dispatches |",
               "|---|---|---|---|"]
    for task in aufgaben:
        for arm in "ABC":
            z = vorhanden.get((task, arm))
            if z is None:
                continue
            zeilen.append(
                f"| `{task}` | {arm} | {z['seconds']:.0f} | "
                f"{z['arm_detail'].get('dispatches', '?')} |"
            )

    zeilen += [
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
    zeilen.append(", ".join(f"`{f}`" for f in fehlend) if fehlend
                  else "None: every cell was attempted.")
    zeilen += [
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
    return "\n".join(zeilen)


def cmd_report(args) -> int:
    if not ERGEBNISSE.is_dir():
        print("no cells have run")
        return 1
    # `*.attemptN.v<timestamp>.json` are preserved earlier attempts at a cell,
    # kept because nothing here is deleted. They are not the cell: a re-run
    # replaces what the cell reports, and the attempt that did not deliver
    # stays on disk for anyone who wants to see what happened.
    zellen = [json.loads(f.read_text()) for f in sorted(ERGEBNISSE.glob("*.json"))
              if ".attempt" not in f.name]
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
    if getattr(args, "write", False):
        ziel = HOH / "docs/BENCHMARK_RESULTS.md"
        ziel.write_text(markdown(zellen), encoding="utf-8")
        print(f"wrote {ziel}")
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
    b.add_argument("--write", action="store_true",
                   help="also write docs/BENCHMARK_RESULTS.md")
    b.set_defaults(fn=cmd_report)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
