#!/usr/bin/env python3
"""Does today's build still reach a real closure when the budget suffices?

O143 gave the control plane a shared dispatch ceiling and O144 made it bind.
Campaign v3 then showed what that costs: under nine dispatches per cell, arm C
reached no fixpoint on any of the five benchmark tasks -- fifteen of fifteen
ended `BUDGET_EXHAUSTED`. That is a correct refusal, and it is also exactly the
shape a *regression* would have: a budget that refuses too early looks the same
from outside as a budget that refuses correctly.

So this establishes the other half, and only that half:

    purpose        = post-O143 operational closure sanity
    matched_budget = false

It is **not** a benchmark, not a repetition of any campaign, and not a
comparison with anything. Nothing it produces may be reported next to arm A or
arm B. The single question is whether the positive path still works when the
ceiling is not the binding constraint: primary node, acceptance, merge, a red
global gate, a repair node that draws on the *remaining shared* budget, merge,
closure, `CLOSED`.

## Where the budget comes from, and why it is not raised afterwards

**18**, and it is measured rather than chosen. Campaign v2 ran this exact
shape -- arm C, one repair node -- without a shared ceiling, so each run got
its own nine, and four of its five tasks reached `CLOSED` at a measured
eighteen dispatches. `to_roman` is one of them. Eighteen is therefore the known
magnitude for this fixture reaching closure, taken from a completed campaign
before this test was written.

If the run exhausts that budget anyway, the answer is a finding, not a larger
number. Raising the ceiling until a run closes and reporting that as a pass
would make this file an instrument that cannot fail, which is the thing this
project spent two adversarial reviews removing from its other gates. Previous
attempts are parked beside the artifact rather than overwritten, so a sequence
of them is visible.

Usage:
    python3 tools/closure_e2e.py [--task to_roman] [--out PATH] [--json]
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
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOH = HERE.parent
sys.path.insert(0, str(HOH / "src"))

TASKS = HOH / "dogfood/benchmark/tasks"

#: Measured, not chosen. See the module docstring: campaign v2's arm C reached
#: CLOSED at eighteen dispatches on this fixture, with one repair node, before
#: a shared ceiling existed. Changing this number is a source change with
#: provenance, which is the point.
DECLARED_BUDGET = 18
BUDGET_ORIGIN = (
    "campaign v2, arm C: four of five tasks reached CLOSED at a measured 18 "
    "dispatches with one repair node each (to_roman among them), before a "
    "shared ceiling existed. Read from "
    "dogfood/benchmark/results-v2/*.C.1.json."
)

TRUST_HELPER_ENV = "HOH_TRUST_HELPER"
WORKTREE_ROOT_ENV = "HOH_WORKTREE_ROOT"


def _configured(name: str) -> Path:
    value_ = os.environ.get(name, "").strip()
    if not value_:
        raise SystemExit(
            f"{name} is not set. This dispatches real agents into worktrees, "
            "which needs this deployment's trust helper and the directory its "
            "harness puts worktrees in.")
    return Path(value_).expanduser()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def _fixture(task: str) -> Path:
    """The task's base state in a fresh repository with its own nonce."""
    baseline = Path(tempfile.mkdtemp(prefix=f"closure-{task}-"))
    repo = baseline / f"ce-{task}-{baseline.name.rsplit('-', 1)[-1]}"
    repo.mkdir(parents=True)
    for f in sorted((TASKS / task / "base").iterdir()):
        shutil.copy2(f, repo / f.name)
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=closure@local",
                    "-c", "user.name=Closure E2E", "commit", "-qm", "Base"],
                   capture_output=True, text=True)
    return repo


def _per_run(root: Path) -> dict:
    """Provider calls per run under this root, from the runs' own records."""
    out_list = {}
    for state in sorted(root.glob("*/state.json")):
        try:
            d = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, ValueError):               # pragma: no cover - exotic
            continue
        telemetry_ = state.parent / "telemetry.jsonl"
        calls_ = 0
        if telemetry_.exists():
            for z in telemetry_.read_text().splitlines():
                if z.strip():
                    try:
                        calls_ += int(json.loads(z).get("provider_calls") or 0)
                    except ValueError:              # pragma: no cover - partial
                        pass
        out_list[state.parent.name] = {
            "state_usage_dispatches": int(
                (d.get("usage") or {}).get("dispatches", 0) or 0),
            "telemetry_provider_calls": calls_,
        }
    return out_list


def _foreign_process(root: Path, repo: Path, budget: int) -> dict:
    """What a **separate interpreter** says is left of the shared budget.

    The no-reset property is not readable from inside the process that spent
    it. This is the same control `tools/budget_evidence.py` runs, asked here
    of the real run's root rather than a fixture's.
    """
    code = (
        "import json,sys;sys.path.insert(0,%r);"
        "from hoh.launcher import HohRunLauncher;"
        "l=HohRunLauncher(%r,%r,dispatch_budget=%d);"
        "print(json.dumps({'spent':l.spent_budget(),"
        "'left':l.remaining_budget()}))"
        % (str(HOH / "src"), str(root), str(repo), budget))
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=120,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if p.returncode != 0:                           # pragma: no cover - setup
        return {"error": (p.stderr or p.stdout)[-300:]}
    return json.loads(p.stdout)


def one_run(task: str, budget: int) -> dict:
    from hoh.launcher import HohRunLauncher
    from hoh.orchestrator import GateOutcome, GateResult, GateRunner, HaltClass, ProjectController
    from hoh.project import ActionClass, Lifecycle, ProjectState, TaskNode
    from hoh.projectstore import ProjectStore

    os.environ["PYTHONPATH"] = str(HOH / "src")
    repo = _fixture(task)
    root = repo.parent / "root"
    spec = TASKS / task / "SPEC.md"
    nonce = repo.name.rsplit("-", 1)[-1][:8].replace("_", "")

    class Gates(GateRunner):
        def subject(self) -> str:
            return _git(repo, "rev-parse", "HEAD").stdout.strip()[:12] or "(none)"

        def run(self, subject: str) -> list[GateResult]:
            p = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", ".",
                 "-p", "test*.py"],
                cwd=str(repo), capture_output=True, text=True, timeout=900,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                     "HOME": str(repo)})
            return [GateResult(
                name="own-tests", subject=subject,
                outcome=GateOutcome.GREEN if p.returncode == 0 else GateOutcome.RED,
                exit_code=p.returncode,
                detail=((p.stdout + p.stderr).strip().splitlines() or ["-"])[-1][:160])]

    store = ProjectStore(root, f"ce{nonce}")
    store.write_state(ProjectState(
        project_id=f"ce{nonce}", repo_path=str(repo),
        nodes=[TaskNode(id=f"n{task[:6]}{nonce}".replace("_", ""),
                        spec_path=str(spec),
                        spec_digest=__import__("hashlib").sha256(
                            spec.read_bytes()).hexdigest()[:16],
                        lifecycle=Lifecycle.READY,
                        action_class=ActionClass.INTERNAL)]))

    from hoh.approval import PrefixScopedProvider

    # The approval authority. Without it the launcher refuses to start a run
    # at all -- correctly, and it says so exactly: "no approval authority is
    # configured; a person has to answer the trust prompt". The first attempt
    # at this test omitted it and halted BLOCKED_DEPENDENCY with zero provider
    # calls, which is a defect in this file and not in the product. Recorded
    # rather than quietly fixed: the parked artifact beside this one is that
    # attempt, and the declared budget did not move because of it.
    starter = HohRunLauncher(
        root, repo, mainline="main", iterations=3,
        planner="claude", developer="claude", qa="claude",
        dispatch_budget=budget,
        approvals=PrefixScopedProvider(
            script=_configured(TRUST_HELPER_ENV),
            prefix=_configured(WORKTREE_ROOT_ENV) / repo.name))
    t0 = time.monotonic()
    result = ProjectController(store, starter, Gates()).run()
    duration_ = time.monotonic() - t0
    after = store.read_state()

    runs = _per_run(root)
    node_list = [n.id for n in after.nodes if not n.repair_of]
    reparaturen = [n.id for n in after.nodes if n.repair_of]
    def total_(names_):
        return sum(v["telemetry_provider_calls"] for k, v in runs.items()
                   if any(k.startswith(n) or n in k for n in names_))
    total = sum(v["telemetry_provider_calls"] for v in runs.values())

    return {
        "purpose": "post-O143 operational closure sanity",
        "matched_budget": False,
        "is_a_benchmark": False,
        "task": task,
        "declared_shared_budget": budget,
        "budget_provenance": BUDGET_ORIGIN,
        "runs": runs,
        "primary_nodes": node_list,
        "repair_nodes": reparaturen,
        "provider_calls_total": total,
        "primary_spend": total_(node_list),
        "repair_spend": total_(reparaturen),
        "remaining_budget": max(budget - total, 0),
        "halt": str(result.halt).replace("HaltClass.", ""),
        "closed": result.halt is HaltClass.CLOSED,
        "rc_closed": after.rc_closed(),
        "closure_generation": after.closure_generation,
        "human_decisions": len([d for d in after.decisions
                                if d.actor not in ("orchestrator", None)]),
        "approvals": [{"granted": a.granted} for a in starter.approvals_given],
        "wallclock_seconds": round(duration_, 1),
        "steps": [f"{s.kind}:{s.node or ''}" for s in result.steps],
        "reason": result.reason[:300],
        "root": str(root),
        "repo": str(repo),
        "budget_seen_by_a_separate_process": _foreign_process(root, repo, budget),
    }


EXPECTED = {
    "closed": bool,
    "provider_calls_total": int,
    "human_decisions": int,
}


def verdict(m: dict) -> tuple[bool, list[str]]:
    open_ = []
    for name, art in EXPECTED.items():
        if name not in m:
            open_.append(f"{name} was not measured")
        elif not isinstance(m[name], art):
            open_.append(f"{name} is {m[name]!r}, not a {art.__name__}")
    if open_:
        return False, open_

    if not m["closed"]:
        open_.append(f"the run halted {m['halt']}, not CLOSED")
    if not m["rc_closed"]:
        open_.append("the project state does not report closure")
    if not m["repair_nodes"]:
        open_.append("no repair node ran, so the repair path was not exercised")
    if m["human_decisions"]:
        open_.append(f"{m['human_decisions']} human decision(s): this has to "
                     f"close without intervention or it says nothing about "
                     f"the unattended path")
    if m["provider_calls_total"] > m["declared_shared_budget"]:
        open_.append(f"{m['provider_calls_total']} provider calls against a "
                     f"declared ceiling of {m['declared_shared_budget']}")
    if not m["repair_spend"]:
        open_.append("the repair node spent nothing, so it did not draw on "
                     "the shared budget")
    foreign = m.get("budget_seen_by_a_separate_process") or {}
    if foreign.get("spent") != m["provider_calls_total"]:
        open_.append(
            f"a separate process reads {foreign.get('spent')} spent where the "
            f"run's own records say {m['provider_calls_total']}: the budget "
            f"does not survive the process that spent it")
    return not open_, open_


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task", default="to_roman")
    ap.add_argument("--out", type=Path,
                    default=HOH / "dogfood/closure-e2e/CLOSURE_E2E.json")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    _configured(TRUST_HELPER_ENV)
    _configured(WORKTREE_ROOT_ENV)

    m = one_run(args.task, DECLARED_BUDGET)
    ok, open_ = verdict(m)
    m["POST_O143_FULL_CONTROL_CLOSURE"] = "VERIFIED" if ok else "NOT_VERIFIED"
    m["open"] = open_
    m["measured_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        if args.out.exists():
            # Never overwritten: a sequence of attempts at this is exactly
            # what a reader needs to see, and a file that only ever holds the
            # last one hides a budget that was raised until something closed.
            stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
            args.out.rename(args.out.with_name(f"{args.out.name}.v{stamp}"))
        args.out.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    if args.json:
        print(json.dumps(m, indent=2))
    else:
        for k in ("task", "declared_shared_budget", "provider_calls_total",
                  "primary_spend", "repair_spend", "remaining_budget",
                  "halt", "closed", "human_decisions"):
            print(f"  {k:<34s} {m.get(k)}")
        print(f"  {'budget from a separate process':<34s} "
              f"{m.get('budget_seen_by_a_separate_process')}")
        print(f"  {'POST_O143_FULL_CONTROL_CLOSURE':<34s} "
              f"{m['POST_O143_FULL_CONTROL_CLOSURE']}")
        for o in open_:
            print(f"  open: {o}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
