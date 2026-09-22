#!/usr/bin/env python3
"""readiness.py -- is this ready, and if not, exactly what is missing.

Every other document in this repository answers part of that question. The
ledger says what was found, the limitations say what is still true, the claims
ledger says what rests on what, and the benchmark documents say what was
measured. Nowhere is there one answer, and the one place a reader looks for it
is the place where a project is most tempted to write a number down by hand.

So this derives it. Each row names the command it ran, and the verdict at the
bottom is the conjunction of the rows -- not a judgement typed above them. A
row this tool cannot evaluate is `NOT_RUN`, which is never a pass: the same
rule the rest of this project applies to a criterion that did not execute.

What it deliberately does **not** do is decide anything about publishing. That
is the captain's, and the rows it produces are what such a decision would need
to look at.

Usage:
    python3 tools/readiness.py [--quick] [--json] [--write]

`--quick` skips the two slow rows (the suite and the clean install) and marks
them NOT_RUN rather than guessing them.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOH = HERE.parent
sys.path.insert(0, str(HOH / "src"))

PASS, FAIL, NOT_RUN = "PASS", "FAIL", "NOT_RUN"


@dataclass
class Row:
    """One condition, what it measured, and how."""

    name: str
    state: str
    value_: str
    command: str
    why_text: str = ""
    #: True when this row's failure does not block a technically stable
    #: release -- a tracked limitation rather than a broken gate. Named per
    #: row rather than decided at the bottom, so a reader can disagree with
    #: one row without discarding the verdict.
    advisory: bool = False
    notes_: list[str] = field(default_factory=list)


def _run(*args: str, cwd: Path = HOH, timeout: int = 1800) -> tuple[int, str]:
    # The caller's PATH is kept. A reduced one is right for a *check command*
    # -- that is `runner.py`'s job and its reasons -- and wrong here: this
    # tool runs the project's own development tools, and pinning them to
    # /usr/bin made `ruff` unfindable on a machine where it is installed
    # exactly where the CI finds it.
    import os

    p = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                       timeout=timeout, check=False,
                       env={**os.environ, "HERDR_ENV": "1",
                            "PYTHONPATH": str(HOH / "src")})
    return p.returncode, (p.stdout + p.stderr).strip()


def _py(*args: str, **kw) -> tuple[int, str]:
    return _run(sys.executable, *args, **kw)


# --------------------------------------------------------------------------- #
# The rows
# --------------------------------------------------------------------------- #


def row_tests(quick: bool) -> Row:
    command = "python3 -m pytest -q"
    if quick:
        return Row("tests", NOT_RUN, "not run in --quick", command,
                     "the suite is the slowest row and is skipped on request")
    rc, out = _py("-m", "pytest", "-q")
    m = re.search(r"(\d+) passed", out)
    bad = re.search(r"(\d+) failed", out)
    return Row(
        "tests", PASS if rc == 0 else FAIL,
        f"{m.group(1) if m else '?'} passed"
        + (f", {bad.group(1)} failed" if bad else ""),
        command)


def row_lint() -> Row:
    command = "ruff check --select F,E9 src tests tools"
    rc, out = _run("ruff", "check", "--select", "F,E9", "src", "tests", "tools")
    return Row("lint", PASS if rc == 0 else FAIL,
                 "clean" if rc == 0 else out.splitlines()[-1][:80], command)


def row_claims() -> Row:
    command = "python3 tools/check_claims.py check all"
    rc, out = _py("tools/check_claims.py", "check", "all")
    last_ = out.splitlines()[-1] if out else ""
    return Row("claims", PASS if rc == 0 else FAIL, last_[:80], command)


def row_union() -> Row:
    command = "python3 tools/union_gate.py"
    rc, out = _py("tools/union_gate.py")
    red = [z for z in out.splitlines() if "FAIL" in z]
    return Row("union_invariants", PASS if rc == 0 and not red else FAIL,
                 "U1-U5 pass" if not red else "; ".join(z[:60] for z in red),
                 command)


def row_meta() -> Row:
    command = "python3 tools/meta_evidence.py --falsify"
    rc, out = _py("tools/meta_evidence.py", "--falsify")
    n = len([z for z in out.splitlines() if z.strip().startswith("OK ")])
    return Row("meta_evidence", PASS if rc == 0 else FAIL,
                 f"{n} metric(s) VERIFIED, closure "
                 + ("GREEN" if "=> GREEN" in out else "NOT GREEN"), command)


def row_attribution() -> Row:
    command = "python3 tools/attribution.py"
    rc, out = _py("tools/attribution.py")
    if rc == 3 or "ENVIRONMENT_GAP" in out:
        # O171: the tool has a third state -- it ran somewhere that does not
        # carry the history the ledger describes, so it checked nothing. That
        # is not a failure of the ledger and must not be reported as one, and
        # it is not a pass either.
        return Row("attribution", NOT_RUN,
                     "this checkout does not carry the history the ledger "
                     "describes, so nothing was verified", command,
                     "an environment gap is its own state; reporting it as "
                     "FAIL would blame the ledger for the checkout")
    m = re.search(r"(\d+) of (\d+) post-anchor development nodes", out)
    return Row("attribution", PASS if rc == 0 else FAIL,
                 (f"{m.group(1)} of {m.group(2)} nodes through the product"
                  if m else out.splitlines()[-1][:70] if out else "?"),
                 command,
                 "the ratio is not a gate -- it is reported so that nobody has "
                 "to take the phase's own description of itself on trust")


def row_succession() -> Row:
    """Does the phase boundary still describe the tree it was written in?

    A long programme outlives the session that starts it, and the handover has
    been a document nobody could check. This row is the check: green means a
    successor can pick the capsule up and the ground under it has not moved.
    """
    command = "python3 tools/succession.py verify"
    rc, out = _py("tools/succession.py", "verify")
    if rc == 2:
        return Row("succession", NOT_RUN,
                     "no succession capsule in this tree", command,
                     "a phase boundary that was never written is not a "
                     "verified one; the row says so rather than reporting "
                     "the absence as safe")
    if rc == 3:
        gaps = [z for z in out.splitlines() if "could not be checked" in z]
        return Row("succession", NOT_RUN,
                     (gaps[0].strip()[:90] if gaps
                      else "some fields could not be checked here"), command,
                     "a field that cannot be checked from this machine is an "
                     "environment gap, which is neither a pass nor drift")
    drifted_ = [z for z in out.splitlines() if z.strip().startswith("DRIFTED:")]
    return Row(
        "succession", PASS if rc == 0 else FAIL,
        (drifted_[0].strip()[:90] if drifted_
         else "every field re-derives from this tree"),
        command,
        "the capsule is not wrong about the past when it drifts -- it is "
        "stale about the present, and a successor reading it would be too")


def row_identifiers() -> Row:
    """Is the published code still readable to the people it is published to?

    O182. The documents were English and the inside of the code was not: a
    reader of the public tree met `pruefe`, `zeile`, `wurzel`. The sweep is
    done; this row is what stops it coming back one file at a time, which is
    how it arrived.

    A gate over a *style* decision is unusual here, and it earns its place
    for one reason: the defect it prevents is invisible to every other gate.
    Nothing else in this board would go red if the next function were named
    `zeile_sonstwas`.
    """
    command = "python3 tools/identifiers.py check src tools tests"
    rc, out = _py("tools/identifiers.py", "check", "src", "tools", "tests")
    last = [z for z in out.strip().splitlines() if z.strip()]
    return Row(
        "identifiers", PASS if rc == 0 else FAIL,
        (last[-1].strip()[:90] if last else "no output"),
        command,
        "a published tool whose names are in another language is readable "
        "only to the people who wrote it")


def row_preflight() -> Row:
    """Can this machine do the work the next campaign will ask of it?

    V3.3 P0. Not a product gate and not advisory either: it answers a question
    about the *environment*, and the profile decides which answers block. The
    demo profile is used here because a source checkout is where somebody
    rehearses; `--profile unattended` is what a long-horizon run must pass,
    and it is stricter on purpose.

    Its INCONCLUSIVE rows are real: provider health cannot be measured without
    spending, and a worktree is not claimed unless one was created.
    """
    command = "python3 tools/preflight.py --profile demo"
    rc, out = _py("tools/preflight.py", "--profile", "demo")
    verdict_line = [z for z in out.splitlines() if z.startswith("profile ")]
    state = {0: PASS, 1: FAIL, 3: NOT_RUN}.get(rc, FAIL)
    return Row(
        "preflight", state,
        (verdict_line[-1].strip()[:110] if verdict_line else f"exit {rc}"),
        command,
        "an environment that cannot run the work is not a product failure, "
        "and discovering it after the quota is spent is not a measurement")


def row_parallelism() -> Row:
    """Does the parallelism baseline still describe this code?

    V3.3 P0. The document is generated, so the only way it can be wrong is by
    being old: the code moved and nobody re-derived it. This row re-derives it
    and compares, which makes a stale baseline a red row rather than a
    plausible paragraph.
    """
    command = "python3 tools/parallelism_baseline.py --out program/v3_3/VERIHARNESS_PARALLELISM_BASELINE.md"
    document = HOH / "program/v3_3/VERIHARNESS_PARALLELISM_BASELINE.md"
    if not document.is_file():
        return Row("parallelism_baseline", NOT_RUN,
                   "no baseline document in this tree", command,
                   "a baseline nobody wrote is not a baseline that holds")
    import importlib.util as _il
    spec = _il.spec_from_file_location("parallelism_baseline",
                                       HERE / "parallelism_baseline.py")
    pb = _il.module_from_spec(spec)
    # Registered before execution: a `@dataclass` resolves its annotations
    # through `sys.modules[cls.__module__]`, and a module loaded from a path
    # without being registered there raises an AttributeError that says
    # nothing about the cause.
    sys.modules["parallelism_baseline"] = pb
    spec.loader.exec_module(pb)
    body = pb.measure()
    fresh = pb.render(body).strip().splitlines()
    on_disk = document.read_text(encoding="utf-8").strip().splitlines()
    # The timestamp line is the one thing that is allowed to differ: it is
    # when the document was written, not what it says.
    fresh = [line for line in fresh if not line.startswith("Measured at ")]
    on_disk = [line for line in on_disk if not line.startswith("Measured at ")]
    native = [c for c in body["classifications"]
              if c["question"] == "project_native_parallelism"]
    detail = (native[0]["status"] if native else "?")
    if fresh != on_disk:
        moved = [line for line in fresh if line not in on_disk][:1]
        return Row("parallelism_baseline", FAIL,
                   "the document no longer matches the code: "
                   + (moved[0][:70] if moved else "it differs"), command,
                   "a generated document that was not regenerated is a "
                   "measurement of a tree that no longer exists")
    return Row("parallelism_baseline", PASS,
               f"re-derives identically; project_native_parallelism = {detail}",
               command,
               "the baseline is the premise V3.3 was planned against, so it "
               "is checked rather than remembered")


def row_capability_matrix() -> Row:
    """Does the capability matrix still describe this repository?

    V3.3 P0. Same shape as the parallelism row and for the same reason: the
    matrix is generated, so the only way it can be wrong is by being old. The
    row re-derives it and compares the statuses, which makes a stale matrix a
    red row rather than a table somebody trusts.

    It deliberately does not block on the *content* -- 29 MISSING capabilities
    are the plan's premise, not a regression. What it blocks on is the
    document disagreeing with the probes.
    """
    command = ("python3 tools/capability_matrix.py "
               "--out program/v3_3/VERIHARNESS_CAPABILITY_MATRIX.json")
    document = HOH / "program/v3_3/VERIHARNESS_CAPABILITY_MATRIX.json"
    if not document.is_file():
        return Row("capability_matrix", NOT_RUN,
                   "no capability matrix in this tree", command,
                   "a matrix nobody derived is not a matrix that holds")
    import importlib.util as _il
    spec = _il.spec_from_file_location("capability_matrix",
                                       HERE / "capability_matrix.py")
    cm = _il.module_from_spec(spec)
    sys.modules["capability_matrix"] = cm
    spec.loader.exec_module(cm)
    try:
        fresh = cm.measure()
    except cm.RegisterMissing as exc:
        return Row("capability_matrix", NOT_RUN, str(exc)[:100], command,
                   "the register is internal and this tool is published: a "
                   "clone can hold the instrument without holding anything "
                   "for it to measure")
    on_disk = json.loads(document.read_text(encoding="utf-8"))
    now = {r["id"]: r["status"] for r in fresh["capabilities"]}
    then = {r["id"]: r["status"] for r in on_disk.get("capabilities") or []}
    moved = sorted(k for k in set(now) | set(then) if now.get(k) != then.get(k))
    counts = fresh["counts"]
    if moved:
        return Row("capability_matrix", FAIL,
                   f"{len(moved)} capability status(es) moved since the "
                   f"document was written: " + ", ".join(moved[:4]), command,
                   "a generated document that was not regenerated describes a "
                   "tree that no longer exists")
    return Row(
        "capability_matrix", PASS,
        f"{counts.get('PROVEN', 0)} proven, {counts.get('MISSING', 0)} missing, "
        f"{counts.get('PARTIAL', 0)} partial -- re-derives identically",
        command,
        "the matrix is what the next phase is planned from, so it is checked "
        "rather than remembered")


def row_build_plan() -> Row:
    """Is the remaining work in an order somebody could actually execute?

    V3.3 P0. Not a measurement of the product: a measurement of the plan. It
    goes red on a dependency cycle or a dependency that is not a requirement,
    which are the two ways a build order can contain a step nobody can take.
    """
    command = "python3 tools/build_plan.py"
    rc, out = _py("tools/build_plan.py")
    if rc == 3:
        return Row("build_plan", NOT_RUN,
                   "no requirement register in this tree", command,
                   "the register is internal and this tool is published")
    if rc != 0:
        bad = [z for z in out.splitlines()
               if z.startswith(("UNPLACEABLE", "DANGLING"))]
        return Row("build_plan", FAIL,
                   (bad[0][:100] if bad else f"exit {rc}"), command,
                   "an order that drops an edge to stay acyclic is an order "
                   "with an impossible step in it")
    summary = [z for z in out.splitlines() if "open requirement" in z]
    return Row("build_plan", PASS,
               (summary[-1].strip()[:90] if summary else "derived"), command,
               "the build order is derived from the register, so it cannot "
               "drift from the statuses it is planned against")


def row_baseline_docs() -> Row:
    """Do the three rendered baselines still match what they render?

    V3.3 P0. Same rule as the other two generated documents: a rendering that
    was not re-rendered describes a tree that no longer exists. It compares
    the documents on disk with a fresh rendering, ignoring only the line that
    says when they were written.
    """
    command = "python3 tools/baseline_docs.py --out-dir program/v3_3"
    folder = HOH / "program/v3_3"
    import importlib.util as _il
    spec = _il.spec_from_file_location("baseline_docs", HERE / "baseline_docs.py")
    bd = _il.module_from_spec(spec)
    sys.modules["baseline_docs"] = bd
    spec.loader.exec_module(bd)
    try:
        body = bd.measure()
    except Exception as exc:                       # RegisterMissing and friends
        return Row("baseline_docs", NOT_RUN, str(exc)[:100], command,
                   "the register is internal and this tool is published")
    fresh = {
        "VERIHARNESS_CURRENT_BASELINE.md": bd.current_baseline(body),
        "VERIHARNESS_PRODUCT_BASELINE.md": bd.product_baseline(body),
        "VERIHARNESS_CONTRACT_TRACE_BASELINE.md": bd.contract_trace(body),
    }
    stale = []
    for name, text in fresh.items():
        target = folder / name
        if not target.is_file():
            stale.append(f"{name} is missing")
            continue
        a = [line for line in text.strip().splitlines()
             if not line.startswith("Measured at ")]
        b = [line for line in target.read_text(encoding="utf-8").strip().splitlines()
             if not line.startswith("Measured at ")]
        if a != b:
            stale.append(name)
    if stale:
        return Row("baseline_docs", FAIL,
                   f"{len(stale)} document(s) no longer match: "
                   + ", ".join(stale[:2]), command,
                   "a rendering that was not re-rendered describes a tree "
                   "that no longer exists")
    return Row("baseline_docs", PASS,
               f"{len(fresh)} document(s) re-render identically", command,
               "the baselines are summaries of measurements taken elsewhere, "
               "so the only thing that can go wrong is being old")


def row_program_scope() -> Row:
    """Does the register describe the whole programme the plan names?

    O196. The register used to certify its own completeness: the test asked
    whether it contained P1-P3, and the register supplied the answer. This row
    asks the pinned plan instead, parsed from its bytes, and fails on a missing
    capability, a missing phase, an invented requirement, a duplicate, or an
    inventory parsed from a different plan than the one pinned.
    """
    command = "python3 tools/program_scope.py --check"
    rc, out = _py("tools/program_scope.py", "--check")
    last = [z for z in out.strip().splitlines() if z.strip()]
    if rc == 3:
        return Row("program_scope", NOT_RUN,
                   (last[-1][:100] if last else "the pinned plan is not reachable"),
                   command,
                   "the inventory is derived from the plan; without the plan "
                   "nothing about completeness is claimed")
    return Row("program_scope", PASS if rc == 0 else FAIL,
               (last[-1].strip()[:100] if last else f"exit {rc}"), command,
               "a register that is internally consistent can still be a "
               "quarter of the programme; only the plan can say it is not")


def row_export_sync() -> Row:
    """Would exporting right now overwrite work that did not come from here?

    Separate from `export_manifest`, which asks whether the *set* of exported
    paths is right. This asks whether their *content* can be written without
    discarding somebody else's change -- the question nobody was asking when
    three merged pull requests landed in the public repository and the copier
    compared nothing.
    """
    command = "python3 tools/export_sync.py status"
    rc, out = _py("tools/export_sync.py", "status")
    if rc == 2:
        # No checkout configured, or no recorded base. Nothing was compared,
        # which is neither safe nor a failure of the export.
        return Row("export_sync", NOT_RUN,
                     (out.strip().splitlines() or ["no comparison was made"])[0][:90],
                     command,
                     "a comparison that did not run is not a green one; the "
                     "row says so rather than reporting the absence as safe")
    numbers = dict(re.findall(r"^(\w[\w ()]*?):\s+(\d+)$", out, re.M))
    conflicts = len([z for z in out.splitlines()
                     if z.strip().startswith(("conflict:", "only in the public"))])
    return Row(
        "export_sync", PASS if rc == 0 else FAIL,
        (f"{conflicts} unintegrated public change(s)" if conflicts
         else f"clean; {numbers.get('to write (ours)', '?')} ours to write"),
        command,
        "the export is one-directional and the public repository is not "
        "read-only: this row is what stops a copy from reverting work done "
        "there")


def row_export() -> Row:
    command = "python3 tools/export_manifest.py check"
    rc, out = _py("tools/export_manifest.py", "check")
    # O165: this counted every line mentioning "U2b", which includes the
    # check's own OK summary line ("passes U2b + the leak scan"). A green run
    # therefore reported "1 dangling reference(s)" when it had found none, and
    # said nothing about the acknowledged ones -- a number that meant
    # something other than what it was labelled. Count the FAIL lines, which
    # are the unacknowledged references and the only ones that are a finding.
    u2b = len([z for z in out.splitlines() if z.startswith("FAIL: U2b:")])
    acknowledged = len([z for z in out.splitlines() if z.startswith("ACKNOWLEDGED:")])
    stale_ = "disagrees with a fresh derivation" in out
    rest = len([z for z in out.splitlines()
                if z.startswith("FAIL") and "U2b" not in z])
    if stale_:
        # The reference checks do not run against a stale manifest, so
        # reporting "0 dangling references" here would be a zero that means
        # "not measured" -- the shape this project refuses everywhere else.
        return Row(
            "export_manifest", FAIL,
            "the manifest is out of date, so the reference checks did not run",
            command,
            "re-derive it with `python3 tools/export_manifest.py derive`; a "
            "count taken against a stale manifest would be a zero that means "
            "'not measured'")
    # A leak is the most serious thing this check can find and must not sit
    # inside "other problems": a home path, a private address or a
    # token-shaped string in an INCLUDE file is the one finding that would
    # make publishing actively harmful.
    lecks = [z for z in out.splitlines()
             if any(k in z for k in ("home-path", "private-address",
                                     "token-shaped"))]
    state = PASS if rc == 0 and not rest and not u2b else FAIL
    return Row(
        "export_manifest", state,
        (f"**{len(lecks)} leak(s)**, " if lecks else "no leaks, ")
        + f"{u2b} unacknowledged dangling reference(s), "
        + f"{acknowledged} acknowledged, {rest} other problem(s)", command,
        "the dangling references are limitation 12e: published documents "
        "citing internal ones. Advisory, because none of them is a false "
        "claim -- what a reader loses is the ability to follow a citation",
        # A leak is never advisory, whatever else the run found. The flag is
        # there to separate "a citation a reader cannot follow" from "a
        # finding", and a home path or a token-shaped string in a published
        # file is the second kind under any reading.
        advisory=not rest and not lecks)


def row_install(quick: bool) -> Row:
    command = "python3 tools/clean_install_check.py"
    if quick:
        return Row("clean_install", NOT_RUN, "not run in --quick", command)
    rc, out = _py("tools/clean_install_check.py")
    m = re.search(r"=== (\d+) red step", out)
    return Row("clean_install", PASS if rc == 0 else FAIL,
                 f"{m.group(1)} red step(s)" if m else out.splitlines()[-1][:60],
                 command)


def row_confinement() -> Row:
    command = "read dogfood/planner-confinement/SUMMARY.json"
    file_path = HOH / "dogfood/planner-confinement/SUMMARY.json"
    if not file_path.is_file():
        return Row("planner_capability_boundary", NOT_RUN,
                     "no confinement evidence installed", command)
    s = json.loads(file_path.read_text())
    verdict = s.get("planner_capability_boundary")
    return Row("planner_capability_boundary",
                 PASS if verdict == "VERIFIED" else FAIL,
                 f"{verdict} on run {s.get('run_id')}, witness armed for "
                 f"{s.get('planner_dispatches_with_an_armed_witness')} of "
                 f"{s.get('planner_dispatches')} planner dispatch(es)", command)


def row_budget() -> Row:
    """Is the dispatch budget enforced, and was that claim falsified?

    Release-critical because campaign v3's premise is a matched budget, and
    v2 showed what an unenforced one produces: three of five arm-C cells spent
    eighteen dispatches against a stated nine and the campaign reported nine,
    because the figure was a constant beside the result rather than a
    measurement of it (O140). The row reads the artifact rather than rerunning
    the controls: they build fixture runs, and a readiness pass that silently
    ran a benchmark-shaped workload would be the wrong kind of gate.
    """
    command = ("python3 tools/budget_evidence.py --out "
              "dogfood/budget-enforcement/BUDGET_EVIDENCE.json")
    file_path = HOH / "dogfood/budget-enforcement/BUDGET_EVIDENCE.json"
    if not file_path.is_file():
        return Row("budget_enforcement", NOT_RUN,
                     "no budget evidence installed", command)
    b = json.loads(file_path.read_text())
    v = b.get("budget_enforcement")
    # The control names come from the tool rather than being repeated here. A
    # second copy of the list drifts, and this row spent one regeneration
    # reporting FAILED against keys the instrument had renamed -- which is the
    # right failure (a missing key is not a pass) and the wrong reason.
    import importlib.util as _il

    spec = _il.spec_from_file_location("budget_evidence",
                                       HERE / "budget_evidence.py")
    be = _il.module_from_spec(spec)
    spec.loader.exec_module(be)
    missing = [k for k in be.CONTROLS.values() if not b.get(k)]

    falsifiers = b.get("falsifiers") or []
    ran = [f for f in falsifiers if f.get("ran")]
    recognised = bool(ran) and all(f.get("detected") for f in ran)
    return Row(
        "budget_enforcement",
        PASS if v == "VERIFIED" and not missing and recognised else FAIL,
        f"{v}; ceilings {b.get('ceilings')}, product refused a dispatch at "
        f"{b.get('runs_whose_refusal_came_from_the_dispatch_path')}; "
        f"{len(ran)} falsifier(s) "
        + ("all detected" if recognised else "NOT all detected")
        + (f"; failed: {', '.join(missing)}" if missing else ""),
        command,
        "the controls pass on a build with enforcement removed unless the "
        "falsifiers say otherwise, so they are part of the row and not a "
        "footnote under it. One of them deletes only the per-dispatch check, "
        "which is the mutant an earlier version of this instrument survived")


def row_closure() -> Row:
    """Does the control plane still reach a fixpoint when the budget suffices?

    Campaign v3 established the refusal: under nine dispatches per cell, arm C
    reached no closure at all, fifteen of fifteen. That is a correct refusal,
    and from outside it is indistinguishable from a regression -- a ceiling
    that refuses too early looks exactly like a ceiling that refuses rightly.
    So the positive path gets its own evidence, at a budget taken from what
    campaign v2 measured for this shape rather than from what a run turns out
    to want.

    Not a benchmark, and the artifact says so in its own fields. Nothing it
    produces may be reported next to arm A or arm B.
    """
    command = "python3 tools/closure_e2e.py"
    file_path = HOH / "dogfood/closure-e2e/CLOSURE_E2E.json"
    if not file_path.is_file():
        return Row("post_o143_closure", NOT_RUN,
                     "no closure evidence installed", command)
    c = json.loads(file_path.read_text())
    v = c.get("POST_O143_FULL_CONTROL_CLOSURE")
    open_ = c.get("open") or []
    # A run that closed by being given more budget than it declared is not
    # evidence of anything, so the two numbers travel together in the row.
    return Row(
        "post_o143_closure",
        PASS if v == "VERIFIED" and not open_ else FAIL,
        f"{v}; halt {c.get('halt')}; {c.get('provider_calls_total')} of "
        f"{c.get('declared_shared_budget')} declared dispatches "
        f"(primary {c.get('primary_spend')}, repair {c.get('repair_spend')}); "
        f"{len(c.get('repair_nodes') or [])} repair node(s); "
        f"{c.get('human_decisions')} human decision(s)"
        + ("; " + "; ".join(open_) if open_ else ""),
        command,
        "an operational regression test, not a benchmark: it asks only "
        "whether the closure path still works when the ceiling is not the "
        "binding constraint. Its budget is measured from campaign v2 and is "
        "not raised after a failed attempt -- previous attempts are parked "
        "beside the artifact so a sequence of them stays visible")


def row_telemetry() -> Row:
    """Does the product's dispatch record say what it claims, on real runs?

    Read across **every** installed audit, not one. The confinement run could
    not answer it: nothing failed and nothing retried in it, so three record
    shapes never occurred, and `NOT_OBSERVED` is not a pass. Campaign v3
    produced all three -- fifteen budget refusals, each classified, and four
    dispatches that really did retry -- so the question is answerable now.

    Two different things are therefore asked of the set:

    * a **field gap** anywhere blocks. A field nobody filled is a defect on
      the run that has it, and another run filling it does not repair that.
    * an **unobserved shape** blocks only while *no* run has observed it. A
      failure record cannot be conjured by a campaign in which nothing
      failed, and demanding one would be demanding a fabricated record.
    """
    command = ("python3 tools/telemetry_audit.py --run-root PATH --run-id ID "
              "--out dogfood/<tree>/TELEMETRY_AUDIT.json")
    # Parked predecessors are history, not evidence. A tree renamed
    # `<name>.v<UTC stamp>` beside a live one is this project's way of
    # replacing without deleting, and its audit was written against an
    # earlier version of the record -- so it reports gaps in fields that did
    # not exist yet. Judging the product by them would be judging it by what
    # it used to be.
    parked = re.compile(r"\.v\d{8}T\d{6}Z$")
    paths = sorted(f for f in (HOH / "dogfood").glob("*/TELEMETRY_AUDIT.json")
                   if not parked.search(f.parent.name))
    if not paths:
        return Row("telemetry_on_real_dispatches", NOT_RUN,
                     "no audit installed", command)
    reports = {}
    for file_path in paths:
        try:
            reports[file_path.parent.name] = json.loads(file_path.read_text())
        except ValueError:                         # pragma: no cover - exotic
            continue
    gaps = {name: (b.get("fields_with_gaps") or [])
                     + (b.get("coverage_gaps") or [])
               for name, b in reports.items()}
    with_gaps = {n: v for n, v in gaps.items() if v}
    # A shape is observed if any audit saw it. The names come from the audits
    # themselves rather than a second list here, which would drift.
    all_shapes = set()
    for b in reports.values():
        all_shapes |= set((b.get("coverage") or {}))
    never = sorted(f for f in all_shapes
                 if not any((b.get("coverage") or {}).get(f)
                            for b in reports.values()))
    green = not with_gaps and not never
    where = [n for n, b in reports.items()
          if b.get("telemetry_validated_on_real_dispatches") == "yes"]
    return Row(
        "telemetry_on_real_dispatches",
        PASS if green else FAIL,
        f"{len(reports)} audit(s): "
        + (f"fully validated on {', '.join(where)}" if where else "none fully validated")
        + ("; gaps: " + "; ".join(f"{n}: {', '.join(v)}"
                                  for n, v in with_gaps.items())
           if with_gaps else "; no field gaps")
        + ("; never observed anywhere: " + ", ".join(never) if never else ""),
        command,
        "a field gap is a defect on the run that has it and another run "
        "filling it repairs nothing, so any gap blocks; an unobserved shape "
        "blocks only while no run has observed it, because a campaign in "
        "which nothing failed cannot be asked to produce a failure record")


#: Every check `audit_refs.py` offers. Named here rather than discovered, so
#: that a check quietly disappearing from that tool shows up as a shorter list
#: instead of as a green row.
AUDIT_CHECKS = (
    "coverage", "verify-verdicts", "selftest", "a02-count",
    "numbers-recomputed", "summary-consistency", "claims-against-code",
    "no-overclaim-o31",
)


def _plan(campaign_: str) -> dict:
    import importlib.util as _il

    spec = _il.spec_from_file_location(
        "repetition_plan", HERE / "repetition_plan.py")
    rp = _il.module_from_spec(spec)
    spec.loader.exec_module(rp)
    return rp.plan(campaign_)


def campaign_verdict(p: dict) -> dict:
    """Three questions about a campaign, answered separately.

    They were one row and one verdict, and that conflated things a reader has
    to keep apart. A campaign can be **complete** and still not have had the
    matched budget it is named after -- v2 is exactly that -- and a release
    gate that reads "15 of 15 cells" as readiness is passing on the existence
    of result files. So:

    * `completeness`   -- did every cell run the repetitions it owed?
    * `matched_budget` -- did the runs stay inside the budget the protocol
                          defines, and were the ones that did not stopped?
    * `usable`         -- may a release rest on this campaign? Only when both
                          of the above hold. A complete campaign with a
                          violated budget is `NO`, and no count of files
                          changes that.
    """
    cells = p["cells"]
    ran = sum(1 for c in cells if c["completed_repetitions"])
    missing_ = [c for c in cells
               if isinstance(c["required_repetitions"], int)
               and c["completed_repetitions"] < c["required_repetitions"]]
    if not ran:
        complete_ = "NOT_RUN"
    elif p["cells_with_no_repetition"] or missing_:
        complete_ = "PARTIAL"
    elif p["protocol_repetition_requirement"] == "NOT_DETERMINABLE":
        # Every cell ran what it could be asked for, and what it *owed* is not
        # decidable from the frozen text. "Complete" would be a claim the
        # protocol cannot support; this says what is true.
        complete_ = "HISTORICAL_COMPLETE"
    else:
        complete_ = "COMPLETE"

    copies_ = p.get("cells_whose_repetitions_share_a_run") or []
    budget = {"ENFORCED": "YES", "VIOLATED": "NO"}.get(
        p["budget_rule"], "UNKNOWN")
    if complete_ == "NOT_RUN":
        # A campaign with no runs has no cell over budget, and the rule would
        # read `ENFORCED` off that emptiness -- a green derived from nothing
        # having happened. `NOT_RUN` is not a pass anywhere else in this file
        # and it is not one here either.
        budget = "UNKNOWN"
        reason = "no cell has run, so nothing about the budget was measured"
        return {
            "completeness": complete_, "matched_budget_valid": budget,
            "reason": reason, "cells_run": ran, "cells": len(cells),
            "usable_for_release": False,
        }
    reason = ""
    if copies_:
        # Counted labels are not counted runs. Three copies of one run,
        # relabelled, read as three repetitions -- and the campaign's own
        # completeness is the thing those labels decide.
        complete_ = "PARTIAL"
    if budget == "NO":
        reason = (f"budget_rule violated: "
                 f"{len(p['cells_over_budget_and_not_stopped'])} cell(s) ran "
                 f"past the dispatch budget without being stopped")
    elif budget == "UNKNOWN":
        reason = (f"budget_rule not determinable: "
                 f"{len(p['cells_whose_spend_is_unknown'])} cell(s) asserted "
                 f"a figure nothing counted")
    if copies_:
        reason = ((reason + "; ") if reason else "") + (
            f"{len(copies_)} cell(s) count repetitions that share a run "
            f"identity: " + ", ".join(copies_[:4]))
    return {
        "completeness": complete_,
        "matched_budget_valid": budget,
        "reason": reason,
        "cells_run": ran,
        "cells": len(cells),
        "cells_sharing_a_run": copies_,
        "usable_for_release": (
            budget == "YES" and complete_ == "COMPLETE" and not copies_),
    }


def row_benchmark_v2() -> Row:
    """Campaign v2, as history. Advisory, and it says why.

    It was release-critical, and that was wrong in a way worth recording: v2
    is an immutable dataset. Its budget was violated during the runs and no
    action available today can change that, so a blocking row over it would be
    a gate that can never be satisfied -- and the pressure a gate like that
    creates points at re-interpreting the dataset, which is the one thing the
    protocol's first paragraph forbids.

    So v2 keeps its findings and reports its own validity, and the
    release-critical question moved to `benchmark_v3`.
    """
    command = "python3 tools/repetition_plan.py --campaign v2"
    u = campaign_verdict(_plan("v2"))
    # A green `PASS` whose own text reads `matched_budget_valid = NO` is a row
    # that tells a reader scanning the column the opposite of what it says.
    # The row state follows the campaign's usability, and the advisory flag --
    # not the state -- is what keeps it from blocking.
    return Row(
        "benchmark_v2_historical",
        PASS if u["usable_for_release"] else FAIL,
        f"{u['completeness']}; matched_budget_valid = "
        f"{u['matched_budget_valid']}"
        + (f"; {u['reason']}" if u["reason"] else ""),
        command,
        "historical and advisory: v2 is an immutable dataset, its matched "
        "budget was not matched during the runs, and no step available today "
        "changes that. A blocking row over it could never be satisfied, and a "
        "gate that cannot be satisfied puts pressure on re-interpreting the "
        "dataset -- the one thing freezing a protocol forbids. `benchmark_v3` "
        "carries the release question",
        advisory=True)


def row_benchmark_v3() -> Row:
    """The release-critical campaign: one that was actually run under the rules.

    `current_valid_benchmark_campaign`. NOT_RUN until v3 exists, and NOT_RUN
    is not a pass -- the same rule this file applies everywhere else. The
    negative control is the part worth stating: a campaign with all 45 result
    files present and a violated budget is FAIL, not PASS. Completeness is
    not validity.
    """
    command = ("python3 tools/prereg.py check --campaign v3 && "
              "python3 tools/repetition_plan.py --campaign v3")
    p = _plan("v3")
    u = campaign_verdict(p)
    frost = _prereg("v3")

    open_ = []
    if frost["verdict"] == "NOT_REGISTERED":
        open_.append("the instrument freeze is NOT_REGISTERED")
    elif frost["verdict"] != "FROZEN":
        accounted, why_text = drift_accounted("v3", frost)
        if not accounted:
            open_.append(f"the instrument freeze is {frost['verdict']} and "
                         "the drift is not accounted for: " + "; ".join(why_text))
    if u["completeness"] not in ("COMPLETE",):
        open_.append(f"completeness {u['completeness']}")
    if u["matched_budget_valid"] != "YES":
        open_.append(u["reason"] or
                     f"matched_budget_valid = {u['matched_budget_valid']}")
    state = NOT_RUN if u["completeness"] == "NOT_RUN" else (
        PASS if not open_ else FAIL)
    accounted, _ = drift_accounted("v3", frost)
    return Row(
        "benchmark_v3", state,
        f"{u['cells_run']} of {u['cells']} cells; {u['completeness']}; "
        f"matched_budget_valid = {u['matched_budget_valid']}; freeze "
        f"{frost['verdict']}"
        + (" (post-campaign repair, accounted for)"
           if frost["verdict"] == "DRIFTED" and accounted else "")
        + ("" if not open_ else " -- " + "; ".join(open_)),
        command,
        "the campaign a release may rest on: pre-registered, run under an "
        "enforced budget, complete. A campaign that produced every result "
        "file while violating the protocol is FAIL here, because the files "
        "are not what is being asked about")


def _prereg(campaign_: str) -> dict:
    import importlib.util as _il

    spec = _il.spec_from_file_location("prereg", HERE / "prereg.py")
    pr = _il.module_from_spec(spec)
    spec.loader.exec_module(pr)
    return pr.comparison(campaign_)


def drift_accounted(campaign_: str, v: dict) -> tuple[bool, list[str]]:
    """Is every file that moved since the freeze accounted for, with evidence?

    The freeze describes the instrument **during** a campaign and is not
    re-taken afterwards: a re-taken freeze would describe a different
    instrument than the one that ran. So after a campaign ends, `DRIFTED` is
    the expected state of any file that was repaired since -- and the question
    stops being "did anything move" and becomes "is what moved accounted for".

    Accounted for is not a flag somebody sets. Every one of these has to hold,
    and each is recomputed here rather than read out of the artifact:

    * the campaign is complete -- a drift during a running campaign is not a
      post-campaign repair, it is the thing the freeze exists to forbid;
    * every drifted path is named in the artifact, with a reason;
    * every change to it was committed **after** the last cell finished;
    * the raw result files hash to exactly what they hashed to before, which
      is what makes the repair a repair of the report and not of the
      measurement.
    """
    import hashlib

    target = HOH / "docs" / "benchmarks" / campaign_ / "POST_CAMPAIGN_DRIFT.json"
    moved_ = sorted(set((v.get("changed") or []) + (v.get("added") or [])
                        + (v.get("removed") or [])))
    if not moved_:
        return True, []
    if not target.is_file():
        return False, [f"{len(moved_)} file(s) drifted and nothing accounts "
                       f"for them: " + ", ".join(moved_)]
    a = json.loads(target.read_text())
    open_ = []
    if not a.get("campaign_complete"):
        open_.append("the drift artifact does not say the campaign is complete")
    named_ = {c["path"] for c in (a.get("changes") or [])}
    for file_path in moved_:
        if file_path not in named_:
            open_.append(f"{file_path} drifted and is not named in the artifact")
        elif not (a.get("why_each_changed") or {}).get(file_path):
            open_.append(f"{file_path} is named without a reason")
    last_ = str(a.get("last_cell_finished_at_utc") or "")
    for c in (a.get("changes") or []):
        when_ = str(c.get("earliest_change_committed_at") or "")
        if not when_ or not last_:
            open_.append(f"{c['path']}: no date to compare against the campaign")
            continue
        from datetime import datetime

        if datetime.fromisoformat(when_).timestamp() <= datetime.fromisoformat(
                last_.replace("Z", "+00:00")).timestamp():
            open_.append(f"{c['path']} changed at {when_}, before the campaign "
                         f"finished at {last_}")
    # Recomputed, never read back: the whole point of the digest table is that
    # it is checked against the files, and an artifact that asserts its own
    # conclusion is the shape this project refuses everywhere else.
    bound_ = HOH / "docs" / "benchmarks" / campaign_ / "RAW_RESULT_DIGESTS.json"
    results = HOH / "dogfood" / "benchmark" / f"results-{campaign_}"
    if bound_.is_file() and results.is_dir():
        before = json.loads(bound_.read_text())["digests"]
        moment = {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                 for f in sorted(results.glob("*.json"))}
        if before != moment:
            open_.append("the raw result files do not hash to what they "
                         "hashed to before the repair")
    else:
        open_.append("no bound digest table to check the raw results against")
    return not open_, open_


def row_prereg() -> Row:
    """Is the frozen instrument still the instrument?

    Separate from `benchmark_v3` because it answers a different question and
    answers it before the campaign exists: the freeze can drift while no
    campaign is running, and a reader deciding whether to start one wants to
    know that first.
    """
    command = "python3 tools/prereg.py check --campaign v3"
    campaign_ = "v3"
    v = _prereg(campaign_)
    if v["verdict"] == "NOT_REGISTERED":
        return Row("benchmark_v3_preregistration", NOT_RUN,
                     "campaign v3 is not pre-registered", command)
    drift = (v.get("changed") or []) + (v.get("added") or []) + \
            (v.get("removed") or [])
    # Three cases, not two. Drift *before* a campaign is a re-freeze; drift
    # *during* one invalidates it; drift *after* it is a repair, and whether
    # that is acceptable is decided by `drift_abgerechnet` -- which recomputes
    # the campaign's completeness, the change dates and the raw digests rather
    # than taking an artifact's word for any of it.
    accounted, why_text = (True, [])
    if v["verdict"] == "DRIFTED":
        accounted, why_text = drift_accounted(campaign_, v)
    return Row(
        "benchmark_v3_preregistration",
        PASS if v["verdict"] == "FROZEN" or accounted else FAIL,
        f"{v['verdict']}, {v['files_frozen']} file(s) frozen at "
        f"{(v.get('protocol_commit') or '')[:12]}"
        + (f" -- moved after the campaign, accounted for: {', '.join(drift[:4])}"
           if drift and accounted else
           f" -- moved: {', '.join(drift[:4])}; " + "; ".join(why_text)
           if drift else ""),
        command,
        "drift before the campaign starts is a re-freeze; drift during it "
        "invalidates the campaign; drift after it is a repair, and it counts "
        "only while every moved file is named with a reason, changed after "
        "the last cell, and the raw results still hash to what they did")


def row_evidence_index() -> Row:
    """Does the evidence index still describe the trees it names?

    The index is the only published account of three evidence trees that are
    not in the export. If it drifts from them, a reader is being asked to
    trust a description of something that has since changed -- which is worse
    than no description, because it looks like one.
    """
    command = "python3 tools/evidence_index.py"
    rc, out = _py("tools/evidence_index.py")
    return Row("evidence_index", PASS if rc == 0 else FAIL,
                 (out.splitlines()[-1] if out else "?")[:70], command)


def row_audit() -> Row:
    """The paper's own citation and numbers audit, every check of it.

    It was not a row, and one of its checks had been red since before the
    benchmark it cites had run. A gate nobody looks at is a gate that teaches
    people not to look.
    """
    command = "python3 tools/audit_refs.py <each check>"
    red = []
    for name in AUDIT_CHECKS:
        rc, _ = _py("tools/audit_refs.py", name, timeout=600)
        if rc != 0:
            red.append(name)
    return Row(
        "paper_audit", PASS if not red else FAIL,
        f"{len(AUDIT_CHECKS) - len(red)} of {len(AUDIT_CHECKS)} checks"
        + (" -- red: " + ", ".join(red) if red else ""),
        command,
        "`coverage` is red on a maintenance item paper/AUDIT.md itself flags "
        "and explains: paper/NUMBERS.md catalogues the 2 of a '2 of 3' ratio "
        "and not the 3. It is a documented, deliberately deferred operator "
        "item, not an unexamined failure",
        advisory=red == ["coverage"])


def row_ci() -> Row:
    """Has an external CI run against the exact export this tree produces?

    It used to ask `git remote` here. The internal tree has none, deliberately:
    the rule that keeps measurements off the network is why the public
    repository is updated from a separate staging checkout carrying only
    INCLUDE paths. So the row could never be satisfied in the architecture it
    is part of -- which makes a release gate unreachable rather than strict,
    and an unreachable gate is one somebody eventually routes around.

    The evidence exists somewhere else, so it is recorded and bound to *what
    was tested*: `tools/exact_head_ci.py` writes the run, the export commit,
    and a digest over every INCLUDE path and its contents. This row recomputes
    that digest from the working tree. A CI result is evidence about a set of
    bytes; if the export has changed since, the run is no longer about the
    thing being released and the evidence is refused rather than reused.

    The sandbox is read per **step**. Its job is green on runners that cannot
    create the namespace it needs, because the step is skipped, and a green
    job whose relevant step did not run has measured nothing.
    """
    command = ("python3 tools/exact_head_ci.py --run-id ID --export-commit SHA")
    file_path = HOH / "dogfood/external-ci/EXACT_HEAD_CI.json"
    if not file_path.is_file():
        return Row("external_ci", NOT_RUN,
                     "no external CI evidence recorded", command,
                     "the external run is evidence a release needs, and a "
                     "checkout that cannot produce it is a checkout that "
                     "cannot declare itself ready")
    c = json.loads(file_path.read_text())

    import importlib.util as _il

    spec = _il.spec_from_file_location("exact_head_ci",
                                       HERE / "exact_head_ci.py")
    eh = _il.module_from_spec(spec)
    spec.loader.exec_module(eh)
    try:
        moment = eh.export_path_digests(HOH)
    except SystemExit as exc:
        return Row("external_ci", FAIL, f"the export cannot be digested: {exc}",
                     command)

    then = c.get("path_digests") or {}
    if not then:
        return Row("external_ci", FAIL,
                     "the recorded run carries no per-path digests, so what it "
                     "tested cannot be compared with this tree", command)
    differing_ = sorted(set(then) ^ set(moment)) + sorted(
        p for p in set(then) & set(moment) if then[p] != moment[p])
    # The gate writes its own report, and the report is published. So the
    # board and the ledger it renders are INCLUDE files that every run of this
    # gate rewrites -- which means an aggregate comparison is red forever, for
    # a reason that has nothing to do with the software. Those files are named
    # here and only they are tolerated; anything else differing is stale
    # evidence and fails.
    REPORTS = {"docs/READINESS.md", "CLAIMS.md", "CLAIMS.json"}
    real = [p for p in differing_ if p not in REPORTS]
    if real:
        return Row(
            "external_ci", FAIL,
            f"the recorded run tested a different export: "
            f"{len(real)} path(s) differ beyond this gate's own reports "
            f"({', '.join(real[:3])}). Re-export, re-run CI, record it again.",
            command,
            "a CI result is evidence about a set of bytes, not about a branch "
            "name; reusing it after the export changed would be citing a "
            "measurement of something else")

    red = [j["name"] for j in (c.get("jobs") or [])
           if j.get("conclusion") != "success"]
    return Row(
        "external_ci",
        PASS if c.get("run_conclusion") == "success" and not red else FAIL,
        f"{c.get('run_conclusion')} on {str(c.get('export_commit'))[:12]} "
        f"({len(c.get('jobs') or [])} job(s)"
        + (f", red: {', '.join(red)}" if red else "")
        + f"); sandbox_external_env = {c.get('sandbox_external_env')}"
        + (f"; differs only by this gate's own reports: "
           f"{', '.join(sorted(set(differing_) & REPORTS))}"
           if differing_ else ""),
        command,
        "the sandbox line is read from its step, not its job: a green job "
        "whose relevant step was skipped has measured nothing, and "
        "UNSUPPORTED_ENVIRONMENT is that state rather than a pass")


#: The dispositions `docs/ROUTING.md` may record. Anything else -- including
#: nothing at all -- is an open question, not a disposition.
ROUTING_DECIDED = ("DEFERRED_ON_EVIDENCE", "ADOPTED", "REJECTED")


def row_routing() -> Row:
    """Is the routing question disposed of, and does the file still say so?

    It returned the literal `PASS` regardless of what it read: a file saying
    `ABANDONED`, or carrying no decision line at all, produced a green row,
    and a missing file took the whole tool down with a traceback. A row that
    cannot fail is a permanent green sitting inside a conjunction, which is
    the shape of a gate that teaches people not to look.
    """
    command = "read docs/ROUTING.md"
    file_path = HOH / "docs/ROUTING.md"
    if not file_path.is_file():
        return Row("routing", NOT_RUN, "docs/ROUTING.md is missing", command)
    m = re.search(r"routing_decision\s*=\s*(\S+)", file_path.read_text())
    value_ = m.group(1) if m else "no routing_decision line"
    return Row("routing", PASS if value_ in ROUTING_DECIDED else FAIL,
                 value_, command,
                 "a disposition, not an open question: the condition for "
                 "revisiting it is named and was checked")


def row_list(quick: bool) -> list[Row]:
    return [
        row_tests(quick),
        row_lint(),
        row_claims(),
        row_union(),
        row_meta(),
        row_confinement(),
        row_budget(),
        row_closure(),
        row_telemetry(),
        row_prereg(),
        row_benchmark_v2(),
        row_benchmark_v3(),
        row_export(),
        row_export_sync(),
        row_succession(),
        row_identifiers(),
        row_preflight(),
        row_parallelism(),
        row_program_scope(),
        row_capability_matrix(),
        row_build_plan(),
        row_baseline_docs(),
        row_install(quick),
        row_attribution(),
        row_evidence_index(),
        row_audit(),
        row_ci(),
        row_routing(),
    ]


def verdict(rows: list[Row]) -> tuple[str, list[str]]:
    open_ = [r.name for r in rows if r.state != PASS and not r.advisory]
    return (PASS if not open_ else FAIL), open_


def markdown(rows: list[Row], head: str) -> str:
    stand, open_ = verdict(rows)
    z = [
        "# Readiness: what holds, what does not, and how each was measured",
        "",
        "Generated by `python3 tools/readiness.py --write`. Every row names the",
        "command that produced it, and the verdict is the conjunction of the",
        "rows rather than a judgement typed above them. A row this tool cannot",
        "evaluate is `NOT_RUN`, which is never a pass.",
        "",
        f"Measured at `{head}` on "
        + datetime.now(UTC).strftime("%Y-%m-%d") + ".",
        "",
        f"    TECHNICALLY_STABLE_READY = {'yes' if stand == PASS else 'no'}",
        "",
    ]
    if open_:
        z += ["Open, and each one blocking: " + ", ".join(open_) + ".", ""]
    z += ["| condition | state | measured | command |", "|---|---|---|---|"]
    for r in rows:
        marke = r.state + (" (advisory)" if r.advisory and r.state != PASS
                             else "")
        z.append(f"| `{r.name}` | {marke} | {r.value_} | `{r.command}` |")
    z += ["", "## Why some rows are advisory", ""]
    for r in rows:
        if r.why_text:
            z.append(f"* **`{r.name}`** — {r.why_text}.")
    z += distribution_section()
    z += [
        "",
        "## What this does not decide",
        "",
        "Publishing. Tagging a release, pushing to a public repository and",
        "uploading to a package index are the captain's, and nothing here",
        "lifts that. These rows are what such a decision would need to read.",
        "",
    ]
    return "\n".join(z)


def distribution_section() -> list[str]:
    """What the published distribution establishes -- derived, not asserted.

    O176 moved this section out of hand-written prose and into a generator,
    so that regenerating the board could not delete it. The first generator
    then read three fields -- version, date, the passing Python versions --
    and stated everything else as a constant. An independent review fed it a
    receipt saying `production_pypi_result: FAIL`, `attestations: NOT_VERIFIED`,
    `trusted_publishing: false`, `long_lived_pypi_token_used: true`, and the
    section still claimed byte-identical files, verified attestations and
    protected token-free publishing.

    That is O140's shape in the repair for O176: a sentence that reads like a
    measurement and is a constant written beside one. So every line below
    names the field it rests on, and a field that is missing, negative or
    contradictory produces a sentence saying **that**, not silence and not
    the positive claim. `_zusicherung` is the whole rule: it never emits the
    affirmative text unless the evidence it was handed says so.
    """
    receipt_ = HOH / ".github/releases/v0.1.0.json"
    if not receipt_.is_file():
        return ["", "## The published distribution", "",
                "No distribution receipt in this tree, so nothing is claimed "
                "about a published package here.", ""]
    try:
        d = json.loads(receipt_.read_text())
    except ValueError as exc:
        return ["", "## The published distribution", "",
                f"The distribution receipt is unreadable ({exc}), so nothing "
                "is claimed about a published package here.", ""]

    z = ["", "## The published distribution", "",
         "Derived from `.github/releases/v0.1.0.json`, the receipt the "
         "publishing workflow wrote -- every line below names the field it "
         "rests on, and a field that is missing or negative produces a line "
         "saying so rather than the positive claim. The table above remains "
         "the historical measurement at its stated commit; these lines are "
         "about the package, not about the campaigns.", ""]

    MISSING, KIND, NO, JA = "fehlt", "typ", "nein", "ja"

    def is_(value_, expected, kind_) -> str:
        """Does this field say what the affirmative sentence would need?

        Four answers, not two. An independent review found the third and
        fourth: `bool(wert)` accepted the string `"false"` as true, and a
        missing field was read as permission rather than as absence. So a
        value is compared by identity or equality against what is expected,
        and a value of the wrong type is its own answer -- never a pass, and
        never reported as though the receipt had said no.
        """
        if value_ is None:
            return MISSING
        if not isinstance(value_, kind_):
            return KIND
        return JA if value_ == expected else NO

    def all_(*states) -> str:
        """The weakest answer among several fields wins, worst first: a claim
        resting on two fields is only as good as the one that does not hold."""
        for bad in (KIND, MISSING, NO):
            if bad in states:
                return bad
        return JA

    def assurance_(state: str, ja: str, refuse: str, field_: str) -> str:
        """The affirmative sentence only when the evidence says so."""
        if state == MISSING:
            return f"Not confirmed -- the receipt carries no `{field_}`: {refuse}"
        if state == KIND:
            return (f"**Not confirmed** -- `{field_}` in the receipt is not of "
                    f"the type this reads: {refuse}")
        if state == NO:
            return f"**Not confirmed** -- `{field_}` in the receipt says otherwise: {refuse}"
        return ja

    version = d.get("version")
    tag = d.get("source_tag")
    when_ = str(d.get("publication_timestamp") or "")[:10]
    prod = d.get("production_pypi_result")
    test = d.get("testpypi_result")
    assets = d.get("github_assets_match")
    att = (d.get("attestations") or {}).get("status")
    tp = d.get("trusted_publishing")
    token = d.get("long_lived_pypi_token_used")
    environment_ = d.get("production_environment") or {}
    checker = environment_.get("required_reviewer")
    smoke = d.get("python_smoke") or {}
    passed_ = sorted(v for v, r in smoke.items()
                       if isinstance(r, dict) and r.get("result") == "PASS")
    durchgefallen = sorted(v for v, r in smoke.items()
                           if isinstance(r, dict) and r.get("result") != "PASS")

    z.append(assurance_(
        all_(is_(prod, "PASS", str), is_(version, version, str),
             is_(tag, tag, str)),
        f"The v{version} distribution was published"
        + (f" on {when_}" if when_ else "")
        + f" from the unchanged release tag `{tag}`.",
        "no successful publication to the production index is recorded.",
        "production_pypi_result"))

    z.append(assurance_(
        all_(is_(assets, True, bool), is_(test, "PASS", str)),
        "TestPyPI, PyPI, and the GitHub release carry byte-identical wheel "
        "and sdist files.",
        "the three copies are not recorded as byte-identical.",
        "github_assets_match / testpypi_result"))

    if not smoke:
        z.append("Not confirmed -- the receipt carries no `python_smoke`: no "
                 "fresh installation is recorded.")
    elif durchgefallen:
        z.append("**Not confirmed** -- fresh installations are recorded as "
                 "passing on Python " + (", ".join(passed_) or "none")
                 + " and as not passing on " + ", ".join(durchgefallen) + ".")
    else:
        z.append("Fresh installations passed on Python "
                 + ", ".join(passed_) + ".")

    z.append(assurance_(
        is_(att, "VERIFIED", str),
        "Both PEP-740 attestations were verified.",
        "the attestations are not recorded as verified.",
        "attestations.status"))

    z.append(assurance_(
        # Both fields, by identity. `bool(tp)` accepted the string "false",
        # and a missing token field was read as "no token was used" -- an
        # absent record is not a record of absence.
        all_(is_(tp, True, bool), is_(token, False, bool)),
        "Publication used OIDC Trusted Publishing"
        + (f" through the protected `{environment_.get('name', 'pypi')}` "
           f"environment, reviewer {checker}," if checker else ",")
        + " with no long-lived token.",
        "trusted publishing without a long-lived token is not recorded.",
        "trusted_publishing / long_lived_pypi_token_used"))

    z.append("The [machine-readable receipt](../.github/releases/v0.1.0.json) "
             "records hashes, job results, provenance and verification scope.")

    scope_ = d.get("verification_scope") or {}
    if scope_.get("historical_campaigns_rerun") or \
            scope_.get("external_sandbox_reverified"):
        z.append("The receipt's `verification_scope` claims more than "
                 "distribution; this board does not carry that claim.")
    else:
        z.append("These distribution checks do not remeasure the historical "
                 "agent campaigns and do not establish external sandbox "
                 "support.")
    z.append("")
    return z


def _anchor(row: str) -> str:
    import hashlib

    return hashlib.sha256(re.sub(r"\s+", " ", row.strip()).encode()).hexdigest()


def _catch_up_ledger(target: Path) -> int:
    """Re-anchor this document's own ledger entries after regenerating it.

    Its rows change whenever a gate's result changes, which is the point of
    it -- and this repository's policy is that a prose markdown file carrying
    numbers is a claim surface. Doing that by hand after every gate run is how
    O139 happened, so the tool that writes the rows writes their anchors.

    Entries are **updated in place and never removed**: the row set is fixed
    in code, so the count only changes when a row is added, and removing an id
    would leave the gap the ledger refuses.
    """
    file_path = HOH / "CLAIMS.json"
    if not file_path.is_file():
        return 0
    d = json.loads(file_path.read_text())
    doc_lines = target.read_text().splitlines()
    rel = target.relative_to(HOH).as_posix()

    claim_rows = [(i, z) for i, z in enumerate(doc_lines, 1) if z.startswith("| `")]
    non_claim_rows = [(i, z) for i, z in enumerate(doc_lines, 1)
                    if z.startswith("Open, and each one blocking")
                    or z.startswith("* **`") or z.startswith("| condition |")
                    or z.startswith("|---") or z.startswith("Measured at")]

    def belongs_to_us(text: str) -> bool:
        """Is this entry one this tool writes, or somebody else's?

        O176, second half. `vorhanden` used to be *every* entry anchored in
        this document, zipped positionally against the rows this tool
        generates. That was true while the board's rows were the only claims
        here. When four claims arrived whose anchors were prose in the
        distribution section, they were swept into the same list and silently
        rewritten: C-465's text became a `routing` table row. The ledger stayed
        internally consistent and became factually wrong, which is the one
        outcome a ledger must not have.

        Ownership is decided on the shape this tool emits, not on the file the
        entry happens to live in.
        """
        s = text.strip()
        return (s.startswith("| `") or s.startswith("* **`")
                or s.startswith("| condition |") or s.startswith("|---")
                or s.startswith("Measured at")
                or s.startswith("Open, and each one blocking"))

    def catch_up(key_name: str, prefix_: str, new_, note_: dict) -> int:
        present = [e for e in d[key_name]
                     if e.get("where", "").startswith(rel + ":")
                     and belongs_to_us(e.get("text", ""))]
        n = 0
        for e, (i, z) in zip(present, new_, strict=False):
            e["where"] = f"{rel}:{i}"
            e["text"] = z.strip()
            e["anchor_digest"] = _anchor(z)
            n += 1
        if len(new_) > len(present):
            highest = max((int(x["id"].split("-")[1]) for x in d[key_name]),
                           default=0)
            for k, (i, z) in enumerate(new_[len(present):], 1):
                d[key_name].append({
                    "id": f"{prefix_}-{highest + k:03d}",
                    "text": z.strip(), "where": f"{rel}:{i}",
                    "anchor_digest": _anchor(z), **note_})
                n += 1
        return n

    n = catch_up("claims", "C", claim_rows, {
        "status": "SUPPORTED", "evidence": ["file:tools/readiness.py:1"],
        "note": "Generated by tools/readiness.py; the row's own command column "
                "is how it is re-derived.",
        "local_only_resolution":
            "Resolved by the main orchestrator by running "
            "python3 tools/readiness.py --write, which re-runs every command "
            "the row names and rebuilds the table from their output.",
    })
    n += catch_up("not_claims", "N", non_claim_rows, {
        "reason": "a table frame, a provenance line, or the reason a row is "
                  "advisory; the measurement is in the row itself",
    })
    file_path.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    rc, head = _run("git", "rev-parse", "--short", "HEAD")
    head = head if rc == 0 else "(no git head)"
    rows = row_list(args.quick)
    stand, open_ = verdict(rows)

    if args.json:
        print(json.dumps({
            "head": head, "verdict": stand, "open": open_,
            "rows": [r.__dict__ for r in rows],
        }, indent=2))
    else:
        for r in rows:
            marke = "adv " if r.advisory and r.state != PASS else "    "
            print(f"  {marke}{r.state:<8s}{r.name:<32s}{r.value_}")
        print(f"\n  TECHNICALLY_STABLE_READY = "
              f"{'yes' if stand == PASS else 'no'}")
        if open_:
            print("  open: " + ", ".join(open_))
    if args.write:
        target = HOH / "docs/READINESS.md"
        target.write_text(markdown(rows, head), encoding="utf-8")
        print(f"wrote {target}")
        n = _catch_up_ledger(target)
        print(f"re-anchored {n} ledger entr{'y' if n == 1 else 'ies'} "
              "for it")
        if n:
            # CLAIMS.md is generated from CLAIMS.json, and re-anchoring
            # changes the text those entries carry. Leaving the rendering
            # behind put the repository in a state where
            # `check_claims.py check all` fails -- so running this gate made
            # the *next* run of this gate report `claims` and
            # `union_invariants` red, for a reason the previous run had
            # caused. A release gate whose own side effect fails the next
            # release gate is not a gate.
            rc, out = _py("tools/check_claims.py", "render")
            print(out.splitlines()[-1] if out else
                  f"re-render exited {rc}")
    return 0 if stand == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
