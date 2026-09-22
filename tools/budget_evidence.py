#!/usr/bin/env python3
"""Does the dispatch budget hold, and can this file tell if it stops holding?

Campaign v2's protocol said "nine role dispatches per task per arm". Three of
its five arm-C cells spent eighteen and the campaign did not notice, because
nothing enforced the number and the figure in the result files was a
**constant** the runner wrote next to the result (O140). A benchmark whose
premise is a matched budget cannot assert its own budget figure.

So before campaign v3 is run, the instrument that is supposed to carry that
premise gets falsified here. Each control is a property the budget must have
for `benchmark_v3` to mean anything, and each is measured on a fixture run
that really needs more dispatches than it may spend:

* **K1** dispatches 1..N reach the provider. A ceiling that refuses early is
  not a budget, it is an outage, and it would make every arm-C cell look
  cheap for the wrong reason.
* **K2** dispatch N+1 is refused structurally, and refused *as a budget*:
  `BUDGET_EXHAUSTED`, not `PROVIDER_UNAVAILABLE` and not an ambiguous halt.
  The two want opposite responses from a reader.
* **K3** the count reaches disk **before** the provider is called. A count
  written on the way out is a count a crash refunds.
* **K4** a second OS process over the same root sees the spend. This is the
  restart: the counter used to live in the launcher object, so killing the
  orchestrator after eight dispatches handed the next run a fresh nine.
* **K5** the next run is *started with* the remainder. K4 proves the number
  can be computed; this proves `prepare` passes it rather than a constant.
  This is the "repair node sees only the remaining shared budget" case.
* **K6** the measured consumption is not a capped constant. The fixture is
  run at two different ceilings; a constant cannot produce both. The
  independent telemetry count must agree with the run's own counter -- two
  records written by different code paths, compared.
* **K7** the falsifier. Enforcement is deliberately removed and the controls
  must go **red**. A control suite that passes on a build with the check
  taken out is measuring nothing, which is precisely what the v2 figure did.

What this does not establish: that a provider was really called (the fixture
dispatcher is local by construction), that the budget is the *right* number,
or that a run cannot be made to spend more by editing its store between runs.
The store is trusted here; `--repo`-style hardening is a different property
and `confinement_evidence.py` carries it.

Usage:
    python3 tools/budget_evidence.py [--json] [--out PATH] [--no-falsifier]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "src"))

from hoh.contracts import Budgets, Role, RunState   # noqa: E402
from hoh.controller import Controller, DispatchError        # noqa: E402
from hoh.store import RunStore                              # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _fixture_repo(where: Path) -> Path:
    r = where / "project"
    r.mkdir(parents=True)
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "budget@example.invalid")
    _git(r, "config", "user.name", "Budget Fixture")
    (r / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


class CountingDispatcher:
    """A provider that never satisfies the run, and counts what reached it.

    Never satisfying it is the point: the fixture must *want* more dispatches
    than it may spend, otherwise the run stopping proves only that the work
    ended. QA fails every iteration, so the loop would go on forever and the
    only thing that can stop it is the budget.

    It also reads `state.json` off disk on every call and records the number
    it finds there. That is control K3: if the persisted count already
    includes the call in flight, a crash mid-dispatch cannot refund it.
    """

    MARKER = "done.txt"

    def __init__(self, repo: Path, store: RunStore) -> None:
        self.repo = repo
        self.store = store
        self.calls_: list[str] = []
        self.on_disk: list[int] = []

    def _read(self) -> int:
        try:
            d = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):               # pragma: no cover - exotic
            return -1
        return int((d.get("usage") or {}).get("dispatches", 0) or 0)

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str:
        self.calls_.append(role.value)
        self.on_disk.append(self._read())
        if role is Role.PLANNER:
            import re
            m = re.search(r'"base_candidate_id":\s*"([^"]+)"', prompt)
            baseline = m.group(1) if m else "base"
            return json.dumps({
                "run_id": state.run_id, "iteration": state.iteration,
                "base_candidate_id": baseline, "spec_digest": state.spec_digest,
                "objective": "keep add() correct", "targets": ["app.py"],
                "preserve": [],
                "acceptance_checks": [{
                    "check_id": "K1", "description": "the marker is there",
                    "command": f"test -f {self.MARKER}", "expect_exit": 0,
                    "preserves": False,
                }],
                "out_of_scope": [], "evidence_refs": [],
                "repair_only": False, "repair_reason": None,
            })
        if role is Role.DEVELOPER:
            # Writes nothing, so the acceptance check below fails and the run
            # keeps wanting another iteration.
            return "done"
        return json.dumps({
            "verdicts": [{
                "check_id": "K1", "outcome": "FAIL",
                "receipt_id": f"{state.run_id}-i{state.iteration}-"
                              f"a{state.attempt}-K1",
                "reproduction": f"test -f {self.MARKER}",
                "note": "the marker is absent",
            }],
            "open_gaps": ["the developer wrote nothing"],
            "summary": "not there yet",
        })

    def endpoint_evidence(self, role: Role) -> str:
        return f"fixture:{role.value}"


def _run(where: Path, ceiling: int) -> dict:
    """One fixture run under a dispatch ceiling. Returns what it spent."""
    repo = _fixture_repo(where)
    spec = where / "spec.md"
    spec.write_text("# Spec\nadd(a,b) must add correctly.\n", encoding="utf-8")
    store = RunStore(where / "runs", "bfix")
    state = Controller.new_state(
        run_id="bfix", repo_path=repo, project_name="project", spec_path=spec,
        # Only the dispatch ceiling may bind. With the defaults, the loop
        # would stop on `max_loops_without_progress` after three fruitless
        # iterations and this file would be measuring that instead.
        budgets=Budgets(max_iterations=99, max_loops_without_progress=99,
                        max_dispatches=ceiling),
    )
    store.write_state(state)
    d = CountingDispatcher(repo, store)
    ctrl = Controller(store, d, spec_path=spec)

    refuses: str | None = None
    for _ in range(99):
        try:
            result = ctrl.run_iteration(state)
        except DispatchError as exc:
            refuses = str(exc)
            break
        if state.budget_exhausted():
            refuses = state.budget_exhausted()
            break
        if result.accepted:                      # pragma: no cover - cannot
            refuses = "the fixture accepted, which it must not"
            break

    end_state = json.loads(store.state_path.read_text(encoding="utf-8"))
    telemetry_ = store.dir / "telemetry.jsonl"
    lines: list[dict] = []
    if telemetry_.exists():
        for z in telemetry_.read_text().splitlines():
            if z.strip():
                lines.append(json.loads(z))
    without_field = [z for z in lines if z.get("provider_calls") is None]
    refused_ = [z for z in lines
                if (z.get("provider_calls") == 0 and z.get("outcome") != "ok")]
    return {
        "ceiling": ceiling,
        "provider_calls": len(d.calls_),
        "roles_called": d.calls_,
        "counter_seen_on_disk_per_call": d.on_disk,
        "dispatches_recorded": int(
            (end_state.get("usage") or {}).get("dispatches", 0) or 0),
        "telemetry_lines": len(lines),
        "telemetry_provider_calls": sum(
            int(z.get("provider_calls") or 0) for z in lines),
        "telemetry_lines_without_the_figure": len(without_field),
        "refusal_lines": len(refused_),
        "refusal_classes": sorted(
            {str(z.get("failure_class") or "") for z in refused_}),
        "refusal": refuses,
        "stage": end_state.get("stage"),
    }


def _foreign_process(root: Path, ceiling: int) -> dict:
    """Asks a *separate* interpreter what is left of the budget.

    A second object in this process shares this process's memory; a second
    process shares only the directory the runs wrote into. That is the
    restart the advisor's list asks about.
    """
    code = (
        "import json,sys;sys.path.insert(0,%r);"
        "from hoh.launcher import HohRunLauncher;"
        "l=HohRunLauncher(%r,%r,dispatch_budget=%d);"
        "d={'spent':l.spent_budget(),'left':l.remaining_budget()};"
        "\ntry:\n    d['args']=l.budget_argumente()"
        "\nexcept Exception as e:\n    d['refused']=type(e).__name__;"
        "d['said']=str(e)[:200]"
        "\nprint(json.dumps(d))"
        % (str(HERE / "src"), str(root), str(root.parent / "project"), ceiling)
    )
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=120,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if p.returncode != 0:                          # pragma: no cover - setup
        return {"error": (p.stderr or p.stdout)[-400:]}
    return json.loads(p.stdout)


#: The fixture spends three dispatches per iteration (planner, developer, QA),
#: and the controller checks the budget again at the *start* of an iteration.
#: At a ceiling that is a multiple of three the run therefore stops cleanly on
#: an iteration boundary and the per-dispatch refusal is never reached -- so a
#: build with that refusal deleted passes every control. Measured by an
#: adversarial review, which deleted it and watched seven of eight stay green.
#: At least one ceiling must force the refusal to land mid-iteration.
ROLES_PER_ITERATION = 3


def _control_per_run(runs: list[dict]) -> dict:
    """The controls that have to hold for **every** fixture run.

    They used to be evaluated on the first run only, so the second run could
    overspend its ceiling with nothing comparing the two.
    """
    return {
        "every_ceiling_was_reachable": all(
            l["provider_calls"] == l["ceiling"] for l in runs),
        "no_run_exceeded_its_ceiling": all(
            l["provider_calls"] <= l["ceiling"] for l in runs),
        "counter_persisted_before_each_call": all(
            l["counter_seen_on_disk_per_call"]
            == list(range(1, len(l["counter_seen_on_disk_per_call"]) + 1))
            for l in runs),
        # The fixture dispatcher counts the calls it received. That counter
        # lives in the test double, not in the controller, so it is the one
        # genuinely independent record here -- `state.usage.dispatches` and
        # the telemetry field are incremented by adjacent lines of the same
        # method and are blind to the same bypass. A mutant that let one role
        # reach the provider uncharged read 14 real calls against 9 recorded,
        # and only this comparison saw it.
        "the_counter_matches_what_the_provider_received": all(
            l["provider_calls"] == l["dispatches_recorded"] for l in runs),
        "telemetry_agrees_with_the_counter": all(
            l["telemetry_provider_calls"] == l["dispatches_recorded"]
            and not l["telemetry_lines_without_the_figure"]
            for l in runs),
    }


def measure_(*, ceilings: tuple[int, ...] = (9, 8, 4)) -> dict:
    if len(set(ceilings)) < 2:
        raise SystemExit(
            "the controls need at least two *different* ceilings: a capped "
            "constant cannot produce both, and that is what K6 is")
    if min(ceilings) < 2:
        raise SystemExit("a ceiling below 2 leaves nothing to refuse")
    enforcing = [c for c in ceilings if c % ROLES_PER_ITERATION]
    if not enforcing:
        raise SystemExit(
            f"every ceiling given is a multiple of {ROLES_PER_ITERATION}, so "
            f"the fixture stops on an iteration boundary and the per-dispatch "
            f"refusal is never reached. At least one ceiling must force it")

    m: dict = {"ceilings": list(ceilings),
               "ceilings_forcing_a_mid_iteration_refusal": enforcing}
    runs = []
    for c in ceilings:
        with tempfile.TemporaryDirectory(prefix="hoh-budget-") as tmp:
            runs.append(_run(Path(tmp) / f"c{c}", c))
    m["runs"] = runs
    m["ceiling"] = ceilings[0]
    m["dispatches_reaching_the_provider"] = runs[0]["provider_calls"]

    m.update(_control_per_run(runs))

    # K2: the refusal has to come from the **product**, and be a budget.
    #
    # It did not have to before, and at the ceiling this project actually uses
    # it did not: nine is three iterations of three roles, the run ended on an
    # iteration boundary, and the only thing that said "budget" was the
    # fixture's own loop guard. Five of eight controls were reading the
    # fixture.
    refused_ = [l for l in runs if l["refusal_lines"]]
    m["runs_whose_refusal_came_from_the_dispatch_path"] = [
        l["ceiling"] for l in refused_]
    m["the_product_refused_a_dispatch"] = bool(refused_)
    classes_ = sorted({k for l in runs for k in l["refusal_classes"]})
    m["refusal_classes"] = classes_
    m["the_refusal_is_recorded_as_a_budget"] = bool(classes_) and all(
        "BUDGET_EXHAUSTED" == k for k in classes_)
    m["refusals"] = [l["refusal"] for l in runs]

    # K6: a capped constant cannot follow two different ceilings.
    m["measured_follows_the_ceiling"] = all(
        l["dispatches_recorded"] == l["ceiling"] for l in runs)
    m["telemetry_lines"] = [l["telemetry_lines"] for l in runs]
    m["telemetry_provider_calls"] = [l["telemetry_provider_calls"]
                                     for l in runs]
    # Not a control, a reading: it is what the old line-counting got wrong.
    m["lines_differ_from_calls_somewhere"] = any(
        l["telemetry_lines"] != l["telemetry_provider_calls"] for l in runs)

    # K4/K5: the shared budget across runs, read by a foreign process, and
    # actually handed to the next run as an argument.
    ceiling = ceilings[0]
    already_spent = ceiling - 1
    distribution_ = (already_spent - already_spent // 2, already_spent // 2)
    with tempfile.TemporaryDirectory(prefix="hoh-budget-") as tmp:
        root = Path(tmp) / "root"
        for rid, n in zip(("n1", "n1r1"), distribution_):
            d = root / rid
            d.mkdir(parents=True)
            (d / "state.json").write_text(
                json.dumps({"run_id": rid, "usage": {"dispatches": n}}),
                encoding="utf-8")
        foreign = _foreign_process(root, ceiling)
        # And the case the shared budget exists for: a repair node arriving at
        # a spent ceiling. It used to be handed `--max-dispatches 0`, which
        # `Budgets` refuses, and the node was booked BLOCKED_DEPENDENCY.
        (root / "n1" / "state.json").write_text(
            json.dumps({"run_id": "n1", "usage": {"dispatches": ceiling}}),
            encoding="utf-8")
        empty = _foreign_process(root, ceiling)
    m["foreign_process"] = foreign
    m["already_spent_by_earlier_runs"] = already_spent
    m["a_restart_does_not_refund"] = foreign.get("spent") == already_spent
    m["the_next_run_is_started_with_the_remainder"] = (
        foreign.get("args") == ["--max-dispatches", "1"])
    m["spent_budget_refuses_the_next_run"] = empty.get("refused") == "BudgetExhausted"
    m["spent_budget_refusal"] = empty
    return m


EXPECTED: dict[str, type | tuple[type, ...]] = {
    "dispatches_reaching_the_provider": int,
    "every_ceiling_was_reachable": bool,
    "no_run_exceeded_its_ceiling": bool,
    "the_product_refused_a_dispatch": bool,
    "counter_persisted_before_each_call": bool,
    "the_counter_matches_what_the_provider_received": bool,
    "measured_follows_the_ceiling": bool,
    "telemetry_agrees_with_the_counter": bool,
    "a_restart_does_not_refund": bool,
    "the_next_run_is_started_with_the_remainder": bool,
    "spent_budget_refuses_the_next_run": bool,
    "the_refusal_is_recorded_as_a_budget": bool,
}

#: Control name -> the measured key that carries it.
CONTROLS = {
    "K1 every ceiling is reachable": "every_ceiling_was_reachable",
    "K1b no run exceeds its ceiling": "no_run_exceeded_its_ceiling",
    "K2 the product refuses a dispatch": "the_product_refused_a_dispatch",
    "K2b the refusal is logged as a budget": "the_refusal_is_recorded_as_a_budget",
    "K3 the count is persisted before the call": "counter_persisted_before_each_call",
    "K3b the counter matches what the provider got":
        "the_counter_matches_what_the_provider_received",
    "K4 a restart does not refund": "a_restart_does_not_refund",
    "K5 the next run gets the remainder": "the_next_run_is_started_with_the_remainder",
    "K5b a spent budget refuses the next run": "spent_budget_refuses_the_next_run",
    "K6 the spend follows the ceiling": "measured_follows_the_ceiling",
    "K6b telemetry agrees with the counter": "telemetry_agrees_with_the_counter",
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
    for label, key in CONTROLS.items():
        if not m[key]:
            open_.append(f"{label} failed ({key})")
    # The raw numbers get their own clause, for every run, so that a bug in
    # the derivation above cannot hide an overrun.
    for one_run in m.get("runs") or []:
        if one_run["provider_calls"] > one_run["ceiling"]:
            open_.append(
                f"{one_run['provider_calls']} calls reached the provider under a "
                f"ceiling of {one_run['ceiling']}")
    return not open_, open_


#: The two ways the enforcement can be lost, and the falsifier reproduces both.
#: The first is the one an adversarial review used to break the previous
#: version: delete only the per-dispatch check and leave everything else --
#: the counter, the persist, the iteration-level check -- in place.
FALSIFIERS = ("the per-dispatch check", "every budget check")


def falsifier(art: str, ceilings: tuple[int, ...]) -> dict:
    """Removes enforcement and requires `verdikt` to go red.

    The previous version asked only whether the fixture overran, which is a
    weaker question than the one the docstring claimed: "the controls must go
    red". A review deleted the per-dispatch check alone -- leaving the
    iteration-level check, which stops the fixture at a multiple of three
    anyway -- and seven of eight controls stayed green while the tool still
    reported the falsifier as detected. So the falsifier now runs the real
    `verdikt` over a real measurement of the mutated build, and `detected`
    means that verdict was False.
    """
    real_budget = RunState.budget_exhausted
    real_gate = Controller._budget_or_refusal

    def without_dispatch_ceiling(self) -> str | None:
        answer = real_budget(self)
        if answer and answer.startswith("dispatch budget exhausted"):
            return None
        return answer

    def without_check(self, state) -> None:
        # The shape of a lost check: the charge still happens, the state is
        # still written, nothing refuses.
        state.usage.dispatches += 1
        self._last_calls = getattr(self, "_last_calls", 0) + 1
        self.store.write_state(state)

    if art == "the per-dispatch check":
        Controller._budget_or_refusal = without_check   # type: ignore[method-assign]
    else:
        RunState.budget_exhausted = without_dispatch_ceiling  # type: ignore[method-assign]
    try:
        m = measure_(ceilings=ceilings)
        ok, open_ = verdict(m)
    finally:
        Controller._budget_or_refusal = real_gate       # type: ignore[method-assign]
        RunState.budget_exhausted = real_budget          # type: ignore[method-assign]

    return {
        "ran": True,
        "removed": art,
        "verdict_on_the_mutated_build": "VERIFIED" if ok else "NOT_VERIFIED",
        "controls_that_went_red": open_,
        "calls_per_run": [(l["ceiling"], l["provider_calls"])
                          for l in m.get("runs") or []],
        "detected": not ok,
        "missed": [] if not ok else [
            f"with {art} removed the controls still passed -- they are not "
            f"reading enforcement"],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ceilings", default="9,8,4",
                    help="the dispatch budgets the fixture is run at. The "
                         "default measures the protocol's own 9 and two "
                         "ceilings that force the per-dispatch refusal to "
                         "land mid-iteration.")
    ap.add_argument("--no-falsifier", action="store_true",
                    help="skip the falsifiers. The verdict then cannot be "
                         "VERIFIED, which is the point of them.")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        ceilings = tuple(int(x) for x in args.ceilings.split(",") if x.strip())
    except ValueError:
        raise SystemExit(f"--ceilings must be integers, got {args.ceilings!r}")

    m = measure_(ceilings=ceilings)
    ok, open_ = verdict(m)

    if args.no_falsifier:
        m["falsifiers"] = [{"ran": False, "reason": "--no-falsifier"}]
        open_ = [*open_, "the falsifiers were not run"]
        ok = False
    else:
        m["falsifiers"] = []
        for art in FALSIFIERS:
            f = falsifier(art, ceilings)
            m["falsifiers"].append(f)
            if f["missed"]:
                open_ = [*open_, "a falsifier was missed: "
                         + "; ".join(f["missed"])]
                ok = False

    m["budget_enforcement"] = "VERIFIED" if ok else "NOT_VERIFIED"
    m["open"] = open_

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    if args.json:
        print(json.dumps(m, indent=2))
    else:
        print(f"  ceilings measured: {m.get('ceilings', '?')} "
              f"(forcing a mid-iteration refusal: "
              f"{m.get('ceilings_forcing_a_mid_iteration_refusal', '?')})")
        for label, key in CONTROLS.items():
            print(f"  {label:<52s} {'ok' if m.get(key) else 'FAILED'}")
        for f in m["falsifiers"]:
            if not f.get("ran"):
                print(f"  {'falsifiers':<52s} NOT RUN")
                continue
            print(f"  falsifier: {f['removed']:<41s} "
                  f"{'detected' if f['detected'] else 'MISSED'}"
                  f"  ({len(f['controls_that_went_red'])} control(s) red)")
        print(f"  {'budget_enforcement':<52s} {m['budget_enforcement']}")
        for o in open_:
            print(f"  open: {o}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
