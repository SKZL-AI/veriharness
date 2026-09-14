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

HIER = Path(__file__).resolve().parent
HOH = HIER.parent
sys.path.insert(0, str(HOH / "src"))

PASS, FAIL, NOT_RUN = "PASS", "FAIL", "NOT_RUN"


@dataclass
class Zeile:
    """One condition, what it measured, and how."""

    name: str
    zustand: str
    wert: str
    befehl: str
    warum: str = ""
    #: True when this row's failure does not block a technically stable
    #: release -- a tracked limitation rather than a broken gate. Named per
    #: row rather than decided at the bottom, so a reader can disagree with
    #: one row without discarding the verdict.
    beratend: bool = False
    notizen: list[str] = field(default_factory=list)


def _lauf(*args: str, cwd: Path = HOH, timeout: int = 1800) -> tuple[int, str]:
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
    return _lauf(sys.executable, *args, **kw)


# --------------------------------------------------------------------------- #
# The rows
# --------------------------------------------------------------------------- #


def zeile_tests(quick: bool) -> Zeile:
    befehl = "python3 -m pytest -q"
    if quick:
        return Zeile("tests", NOT_RUN, "not run in --quick", befehl,
                     "the suite is the slowest row and is skipped on request")
    rc, aus = _py("-m", "pytest", "-q")
    m = re.search(r"(\d+) passed", aus)
    schlecht = re.search(r"(\d+) failed", aus)
    return Zeile(
        "tests", PASS if rc == 0 else FAIL,
        f"{m.group(1) if m else '?'} passed"
        + (f", {schlecht.group(1)} failed" if schlecht else ""),
        befehl)


def zeile_lint() -> Zeile:
    befehl = "ruff check --select F,E9 src tests tools"
    rc, aus = _lauf("ruff", "check", "--select", "F,E9", "src", "tests", "tools")
    return Zeile("lint", PASS if rc == 0 else FAIL,
                 "clean" if rc == 0 else aus.splitlines()[-1][:80], befehl)


def zeile_claims() -> Zeile:
    befehl = "python3 tools/check_claims.py check all"
    rc, aus = _py("tools/check_claims.py", "check", "all")
    letzte = aus.splitlines()[-1] if aus else ""
    return Zeile("claims", PASS if rc == 0 else FAIL, letzte[:80], befehl)


def zeile_union() -> Zeile:
    befehl = "python3 tools/union_gate.py"
    rc, aus = _py("tools/union_gate.py")
    rot = [z for z in aus.splitlines() if "FAIL" in z]
    return Zeile("union_invariants", PASS if rc == 0 and not rot else FAIL,
                 "U1-U5 pass" if not rot else "; ".join(z[:60] for z in rot),
                 befehl)


def zeile_meta() -> Zeile:
    befehl = "python3 tools/meta_evidence.py --falsify"
    rc, aus = _py("tools/meta_evidence.py", "--falsify")
    n = len([z for z in aus.splitlines() if z.strip().startswith("OK ")])
    return Zeile("meta_evidence", PASS if rc == 0 else FAIL,
                 f"{n} metric(s) VERIFIED, closure "
                 + ("GREEN" if "=> GREEN" in aus else "NOT GREEN"), befehl)


def zeile_attribution() -> Zeile:
    befehl = "python3 tools/attribution.py"
    rc, aus = _py("tools/attribution.py")
    m = re.search(r"(\d+) of (\d+) post-anchor development nodes", aus)
    return Zeile("attribution", PASS if rc == 0 else FAIL,
                 (f"{m.group(1)} of {m.group(2)} nodes through the product"
                  if m else aus.splitlines()[-1][:70] if aus else "?"),
                 befehl,
                 "the ratio is not a gate -- it is reported so that nobody has "
                 "to take the phase's own description of itself on trust")


def zeile_export() -> Zeile:
    befehl = "python3 tools/export_manifest.py check"
    rc, aus = _py("tools/export_manifest.py", "check")
    # O165: this counted every line mentioning "U2b", which includes the
    # check's own OK summary line ("passes U2b + the leak scan"). A green run
    # therefore reported "1 dangling reference(s)" when it had found none, and
    # said nothing about the acknowledged ones -- a number that meant
    # something other than what it was labelled. Count the FAIL lines, which
    # are the unacknowledged references and the only ones that are a finding.
    u2b = len([z for z in aus.splitlines() if z.startswith("FAIL: U2b:")])
    anerkannt = len([z for z in aus.splitlines() if z.startswith("ACKNOWLEDGED:")])
    veraltet = "disagrees with a fresh derivation" in aus
    rest = len([z for z in aus.splitlines()
                if z.startswith("FAIL") and "U2b" not in z])
    if veraltet:
        # The reference checks do not run against a stale manifest, so
        # reporting "0 dangling references" here would be a zero that means
        # "not measured" -- the shape this project refuses everywhere else.
        return Zeile(
            "export_manifest", FAIL,
            "the manifest is out of date, so the reference checks did not run",
            befehl,
            "re-derive it with `python3 tools/export_manifest.py derive`; a "
            "count taken against a stale manifest would be a zero that means "
            "'not measured'")
    # A leak is the most serious thing this check can find and must not sit
    # inside "other problems": a home path, a private address or a
    # token-shaped string in an INCLUDE file is the one finding that would
    # make publishing actively harmful.
    lecks = [z for z in aus.splitlines()
             if any(k in z for k in ("home-path", "private-address",
                                     "token-shaped"))]
    zustand = PASS if rc == 0 and not rest and not u2b else FAIL
    return Zeile(
        "export_manifest", zustand,
        (f"**{len(lecks)} leak(s)**, " if lecks else "no leaks, ")
        + f"{u2b} unacknowledged dangling reference(s), "
        + f"{anerkannt} acknowledged, {rest} other problem(s)", befehl,
        "the dangling references are limitation 12e: published documents "
        "citing internal ones. Advisory, because none of them is a false "
        "claim -- what a reader loses is the ability to follow a citation",
        # A leak is never advisory, whatever else the run found. The flag is
        # there to separate "a citation a reader cannot follow" from "a
        # finding", and a home path or a token-shaped string in a published
        # file is the second kind under any reading.
        beratend=not rest and not lecks)


def zeile_install(quick: bool) -> Zeile:
    befehl = "python3 tools/clean_install_check.py"
    if quick:
        return Zeile("clean_install", NOT_RUN, "not run in --quick", befehl)
    rc, aus = _py("tools/clean_install_check.py")
    m = re.search(r"=== (\d+) red step", aus)
    return Zeile("clean_install", PASS if rc == 0 else FAIL,
                 f"{m.group(1)} red step(s)" if m else aus.splitlines()[-1][:60],
                 befehl)


def zeile_confinement() -> Zeile:
    befehl = "read dogfood/planner-confinement/SUMMARY.json"
    pfad = HOH / "dogfood/planner-confinement/SUMMARY.json"
    if not pfad.is_file():
        return Zeile("planner_capability_boundary", NOT_RUN,
                     "no confinement evidence installed", befehl)
    s = json.loads(pfad.read_text())
    verdikt = s.get("planner_capability_boundary")
    return Zeile("planner_capability_boundary",
                 PASS if verdikt == "VERIFIED" else FAIL,
                 f"{verdikt} on run {s.get('run_id')}, witness armed for "
                 f"{s.get('planner_dispatches_with_an_armed_witness')} of "
                 f"{s.get('planner_dispatches')} planner dispatch(es)", befehl)


def zeile_budget() -> Zeile:
    """Is the dispatch budget enforced, and was that claim falsified?

    Release-critical because campaign v3's premise is a matched budget, and
    v2 showed what an unenforced one produces: three of five arm-C cells spent
    eighteen dispatches against a stated nine and the campaign reported nine,
    because the figure was a constant beside the result rather than a
    measurement of it (O140). The row reads the artifact rather than rerunning
    the controls: they build fixture runs, and a readiness pass that silently
    ran a benchmark-shaped workload would be the wrong kind of gate.
    """
    befehl = ("python3 tools/budget_evidence.py --out "
              "dogfood/budget-enforcement/BUDGET_EVIDENCE.json")
    pfad = HOH / "dogfood/budget-enforcement/BUDGET_EVIDENCE.json"
    if not pfad.is_file():
        return Zeile("budget_enforcement", NOT_RUN,
                     "no budget evidence installed", befehl)
    b = json.loads(pfad.read_text())
    v = b.get("budget_enforcement")
    # The control names come from the tool rather than being repeated here. A
    # second copy of the list drifts, and this row spent one regeneration
    # reporting FAILED against keys the instrument had renamed -- which is the
    # right failure (a missing key is not a pass) and the wrong reason.
    import importlib.util as _il

    spec = _il.spec_from_file_location("budget_evidence",
                                       HIER / "budget_evidence.py")
    be = _il.module_from_spec(spec)
    spec.loader.exec_module(be)
    fehlt = [k for k in be.KONTROLLEN.values() if not b.get(k)]

    falsifikatoren = b.get("falsifiers") or []
    gelaufen = [f for f in falsifikatoren if f.get("ran")]
    erkannt = bool(gelaufen) and all(f.get("detected") for f in gelaufen)
    return Zeile(
        "budget_enforcement",
        PASS if v == "VERIFIED" and not fehlt and erkannt else FAIL,
        f"{v}; ceilings {b.get('ceilings')}, product refused a dispatch at "
        f"{b.get('runs_whose_refusal_came_from_the_dispatch_path')}; "
        f"{len(gelaufen)} falsifier(s) "
        + ("all detected" if erkannt else "NOT all detected")
        + (f"; failed: {', '.join(fehlt)}" if fehlt else ""),
        befehl,
        "the controls pass on a build with enforcement removed unless the "
        "falsifiers say otherwise, so they are part of the row and not a "
        "footnote under it. One of them deletes only the per-dispatch check, "
        "which is the mutant an earlier version of this instrument survived")


def zeile_closure() -> Zeile:
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
    befehl = "python3 tools/closure_e2e.py"
    pfad = HOH / "dogfood/closure-e2e/CLOSURE_E2E.json"
    if not pfad.is_file():
        return Zeile("post_o143_closure", NOT_RUN,
                     "no closure evidence installed", befehl)
    c = json.loads(pfad.read_text())
    v = c.get("POST_O143_FULL_CONTROL_CLOSURE")
    offen = c.get("open") or []
    # A run that closed by being given more budget than it declared is not
    # evidence of anything, so the two numbers travel together in the row.
    return Zeile(
        "post_o143_closure",
        PASS if v == "VERIFIED" and not offen else FAIL,
        f"{v}; halt {c.get('halt')}; {c.get('provider_calls_total')} of "
        f"{c.get('declared_shared_budget')} declared dispatches "
        f"(primary {c.get('primary_spend')}, repair {c.get('repair_spend')}); "
        f"{len(c.get('repair_nodes') or [])} repair node(s); "
        f"{c.get('human_decisions')} human decision(s)"
        + ("; " + "; ".join(offen) if offen else ""),
        befehl,
        "an operational regression test, not a benchmark: it asks only "
        "whether the closure path still works when the ceiling is not the "
        "binding constraint. Its budget is measured from campaign v2 and is "
        "not raised after a failed attempt -- previous attempts are parked "
        "beside the artifact so a sequence of them stays visible")


def zeile_telemetrie() -> Zeile:
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
    befehl = ("python3 tools/telemetry_audit.py --run-root PATH --run-id ID "
              "--out dogfood/<tree>/TELEMETRY_AUDIT.json")
    # Parked predecessors are history, not evidence. A tree renamed
    # `<name>.v<UTC stamp>` beside a live one is this project's way of
    # replacing without deleting, and its audit was written against an
    # earlier version of the record -- so it reports gaps in fields that did
    # not exist yet. Judging the product by them would be judging it by what
    # it used to be.
    geparkt = re.compile(r"\.v\d{8}T\d{6}Z$")
    pfade = sorted(f for f in (HOH / "dogfood").glob("*/TELEMETRY_AUDIT.json")
                   if not geparkt.search(f.parent.name))
    if not pfade:
        return Zeile("telemetry_on_real_dispatches", NOT_RUN,
                     "no audit installed", befehl)
    berichte = {}
    for pfad in pfade:
        try:
            berichte[pfad.parent.name] = json.loads(pfad.read_text())
        except ValueError:                         # pragma: no cover - exotic
            continue
    luecken = {name: (b.get("fields_with_gaps") or [])
                     + (b.get("coverage_gaps") or [])
               for name, b in berichte.items()}
    mit_luecken = {n: v for n, v in luecken.items() if v}
    # A shape is observed if any audit saw it. The names come from the audits
    # themselves rather than a second list here, which would drift.
    alle_formen = set()
    for b in berichte.values():
        alle_formen |= set((b.get("coverage") or {}))
    nie = sorted(f for f in alle_formen
                 if not any((b.get("coverage") or {}).get(f)
                            for b in berichte.values()))
    gruen = not mit_luecken and not nie
    wo = [n for n, b in berichte.items()
          if b.get("telemetry_validated_on_real_dispatches") == "yes"]
    return Zeile(
        "telemetry_on_real_dispatches",
        PASS if gruen else FAIL,
        f"{len(berichte)} audit(s): "
        + (f"fully validated on {', '.join(wo)}" if wo else "none fully validated")
        + ("; gaps: " + "; ".join(f"{n}: {', '.join(v)}"
                                  for n, v in mit_luecken.items())
           if mit_luecken else "; no field gaps")
        + ("; never observed anywhere: " + ", ".join(nie) if nie else ""),
        befehl,
        "a field gap is a defect on the run that has it and another run "
        "filling it repairs nothing, so any gap blocks; an unobserved shape "
        "blocks only while no run has observed it, because a campaign in "
        "which nothing failed cannot be asked to produce a failure record")


#: Every check `audit_refs.py` offers. Named here rather than discovered, so
#: that a check quietly disappearing from that tool shows up as a shorter list
#: instead of as a green row.
AUDIT_PRUEFUNGEN = (
    "coverage", "verify-verdicts", "selftest", "a02-count",
    "numbers-recomputed", "summary-consistency", "claims-against-code",
    "no-overclaim-o31",
)


def _plan(kampagne: str) -> dict:
    import importlib.util as _il

    spec = _il.spec_from_file_location(
        "repetition_plan", HIER / "repetition_plan.py")
    rp = _il.module_from_spec(spec)
    spec.loader.exec_module(rp)
    return rp.plan(kampagne)


def kampagnen_urteil(p: dict) -> dict:
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
    zellen = p["cells"]
    gelaufen = sum(1 for c in zellen if c["completed_repetitions"])
    fehlend = [c for c in zellen
               if isinstance(c["required_repetitions"], int)
               and c["completed_repetitions"] < c["required_repetitions"]]
    if not gelaufen:
        vollstaendig = "NOT_RUN"
    elif p["cells_with_no_repetition"] or fehlend:
        vollstaendig = "PARTIAL"
    elif p["protocol_repetition_requirement"] == "NOT_DETERMINABLE":
        # Every cell ran what it could be asked for, and what it *owed* is not
        # decidable from the frozen text. "Complete" would be a claim the
        # protocol cannot support; this says what is true.
        vollstaendig = "HISTORICAL_COMPLETE"
    else:
        vollstaendig = "COMPLETE"

    kopien = p.get("cells_whose_repetitions_share_a_run") or []
    budget = {"ENFORCED": "YES", "VIOLATED": "NO"}.get(
        p["budget_rule"], "UNKNOWN")
    if vollstaendig == "NOT_RUN":
        # A campaign with no runs has no cell over budget, and the rule would
        # read `ENFORCED` off that emptiness -- a green derived from nothing
        # having happened. `NOT_RUN` is not a pass anywhere else in this file
        # and it is not one here either.
        budget = "UNKNOWN"
        grund = "no cell has run, so nothing about the budget was measured"
        return {
            "completeness": vollstaendig, "matched_budget_valid": budget,
            "reason": grund, "cells_run": gelaufen, "cells": len(zellen),
            "usable_for_release": False,
        }
    grund = ""
    if kopien:
        # Counted labels are not counted runs. Three copies of one run,
        # relabelled, read as three repetitions -- and the campaign's own
        # completeness is the thing those labels decide.
        vollstaendig = "PARTIAL"
    if budget == "NO":
        grund = (f"budget_rule violated: "
                 f"{len(p['cells_over_budget_and_not_stopped'])} cell(s) ran "
                 f"past the dispatch budget without being stopped")
    elif budget == "UNKNOWN":
        grund = (f"budget_rule not determinable: "
                 f"{len(p['cells_whose_spend_is_unknown'])} cell(s) asserted "
                 f"a figure nothing counted")
    if kopien:
        grund = ((grund + "; ") if grund else "") + (
            f"{len(kopien)} cell(s) count repetitions that share a run "
            f"identity: " + ", ".join(kopien[:4]))
    return {
        "completeness": vollstaendig,
        "matched_budget_valid": budget,
        "reason": grund,
        "cells_run": gelaufen,
        "cells": len(zellen),
        "cells_sharing_a_run": kopien,
        "usable_for_release": (
            budget == "YES" and vollstaendig == "COMPLETE" and not kopien),
    }


def zeile_benchmark_v2() -> Zeile:
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
    befehl = "python3 tools/repetition_plan.py --campaign v2"
    u = kampagnen_urteil(_plan("v2"))
    # A green `PASS` whose own text reads `matched_budget_valid = NO` is a row
    # that tells a reader scanning the column the opposite of what it says.
    # The row state follows the campaign's usability, and the advisory flag --
    # not the state -- is what keeps it from blocking.
    return Zeile(
        "benchmark_v2_historical",
        PASS if u["usable_for_release"] else FAIL,
        f"{u['completeness']}; matched_budget_valid = "
        f"{u['matched_budget_valid']}"
        + (f"; {u['reason']}" if u["reason"] else ""),
        befehl,
        "historical and advisory: v2 is an immutable dataset, its matched "
        "budget was not matched during the runs, and no step available today "
        "changes that. A blocking row over it could never be satisfied, and a "
        "gate that cannot be satisfied puts pressure on re-interpreting the "
        "dataset -- the one thing freezing a protocol forbids. `benchmark_v3` "
        "carries the release question",
        beratend=True)


def zeile_benchmark_v3() -> Zeile:
    """The release-critical campaign: one that was actually run under the rules.

    `current_valid_benchmark_campaign`. NOT_RUN until v3 exists, and NOT_RUN
    is not a pass -- the same rule this file applies everywhere else. The
    negative control is the part worth stating: a campaign with all 45 result
    files present and a violated budget is FAIL, not PASS. Completeness is
    not validity.
    """
    befehl = ("python3 tools/prereg.py check --campaign v3 && "
              "python3 tools/repetition_plan.py --campaign v3")
    p = _plan("v3")
    u = kampagnen_urteil(p)
    frost = _prereg("v3")

    offen = []
    if frost["verdict"] == "NOT_REGISTERED":
        offen.append("the instrument freeze is NOT_REGISTERED")
    elif frost["verdict"] != "FROZEN":
        abgerechnet, warum = drift_abgerechnet("v3", frost)
        if not abgerechnet:
            offen.append(f"the instrument freeze is {frost['verdict']} and "
                         "the drift is not accounted for: " + "; ".join(warum))
    if u["completeness"] not in ("COMPLETE",):
        offen.append(f"completeness {u['completeness']}")
    if u["matched_budget_valid"] != "YES":
        offen.append(u["reason"] or
                     f"matched_budget_valid = {u['matched_budget_valid']}")
    zustand = NOT_RUN if u["completeness"] == "NOT_RUN" else (
        PASS if not offen else FAIL)
    abgerechnet, _ = drift_abgerechnet("v3", frost)
    return Zeile(
        "benchmark_v3", zustand,
        f"{u['cells_run']} of {u['cells']} cells; {u['completeness']}; "
        f"matched_budget_valid = {u['matched_budget_valid']}; freeze "
        f"{frost['verdict']}"
        + (" (post-campaign repair, accounted for)"
           if frost["verdict"] == "DRIFTED" and abgerechnet else "")
        + ("" if not offen else " -- " + "; ".join(offen)),
        befehl,
        "the campaign a release may rest on: pre-registered, run under an "
        "enforced budget, complete. A campaign that produced every result "
        "file while violating the protocol is FAIL here, because the files "
        "are not what is being asked about")


def _prereg(kampagne: str) -> dict:
    import importlib.util as _il

    spec = _il.spec_from_file_location("prereg", HIER / "prereg.py")
    pr = _il.module_from_spec(spec)
    spec.loader.exec_module(pr)
    return pr.vergleich(kampagne)


def drift_abgerechnet(kampagne: str, v: dict) -> tuple[bool, list[str]]:
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

    ziel = HOH / "docs" / "benchmarks" / kampagne / "POST_CAMPAIGN_DRIFT.json"
    bewegt = sorted(set((v.get("changed") or []) + (v.get("added") or [])
                        + (v.get("removed") or [])))
    if not bewegt:
        return True, []
    if not ziel.is_file():
        return False, [f"{len(bewegt)} file(s) drifted and nothing accounts "
                       f"for them: " + ", ".join(bewegt)]
    a = json.loads(ziel.read_text())
    offen = []
    if not a.get("campaign_complete"):
        offen.append("the drift artifact does not say the campaign is complete")
    genannt = {c["path"] for c in (a.get("changes") or [])}
    for pfad in bewegt:
        if pfad not in genannt:
            offen.append(f"{pfad} drifted and is not named in the artifact")
        elif not (a.get("why_each_changed") or {}).get(pfad):
            offen.append(f"{pfad} is named without a reason")
    letzte = str(a.get("last_cell_finished_at_utc") or "")
    for c in (a.get("changes") or []):
        wann = str(c.get("earliest_change_committed_at") or "")
        if not wann or not letzte:
            offen.append(f"{c['path']}: no date to compare against the campaign")
            continue
        from datetime import datetime

        if datetime.fromisoformat(wann).timestamp() <= datetime.fromisoformat(
                letzte.replace("Z", "+00:00")).timestamp():
            offen.append(f"{c['path']} changed at {wann}, before the campaign "
                         f"finished at {letzte}")
    # Recomputed, never read back: the whole point of the digest table is that
    # it is checked against the files, and an artifact that asserts its own
    # conclusion is the shape this project refuses everywhere else.
    gebunden = HOH / "docs" / "benchmarks" / kampagne / "RAW_RESULT_DIGESTS.json"
    ergebnisse = HOH / "dogfood" / "benchmark" / f"results-{kampagne}"
    if gebunden.is_file() and ergebnisse.is_dir():
        vorher = json.loads(gebunden.read_text())["digests"]
        jetzt = {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                 for f in sorted(ergebnisse.glob("*.json"))}
        if vorher != jetzt:
            offen.append("the raw result files do not hash to what they "
                         "hashed to before the repair")
    else:
        offen.append("no bound digest table to check the raw results against")
    return not offen, offen


def zeile_prereg() -> Zeile:
    """Is the frozen instrument still the instrument?

    Separate from `benchmark_v3` because it answers a different question and
    answers it before the campaign exists: the freeze can drift while no
    campaign is running, and a reader deciding whether to start one wants to
    know that first.
    """
    befehl = "python3 tools/prereg.py check --campaign v3"
    kampagne = "v3"
    v = _prereg(kampagne)
    if v["verdict"] == "NOT_REGISTERED":
        return Zeile("benchmark_v3_preregistration", NOT_RUN,
                     "campaign v3 is not pre-registered", befehl)
    drift = (v.get("changed") or []) + (v.get("added") or []) + \
            (v.get("removed") or [])
    # Three cases, not two. Drift *before* a campaign is a re-freeze; drift
    # *during* one invalidates it; drift *after* it is a repair, and whether
    # that is acceptable is decided by `drift_abgerechnet` -- which recomputes
    # the campaign's completeness, the change dates and the raw digests rather
    # than taking an artifact's word for any of it.
    abgerechnet, warum = (True, [])
    if v["verdict"] == "DRIFTED":
        abgerechnet, warum = drift_abgerechnet(kampagne, v)
    return Zeile(
        "benchmark_v3_preregistration",
        PASS if v["verdict"] == "FROZEN" or abgerechnet else FAIL,
        f"{v['verdict']}, {v['files_frozen']} file(s) frozen at "
        f"{(v.get('protocol_commit') or '')[:12]}"
        + (f" -- moved after the campaign, accounted for: {', '.join(drift[:4])}"
           if drift and abgerechnet else
           f" -- moved: {', '.join(drift[:4])}; " + "; ".join(warum)
           if drift else ""),
        befehl,
        "drift before the campaign starts is a re-freeze; drift during it "
        "invalidates the campaign; drift after it is a repair, and it counts "
        "only while every moved file is named with a reason, changed after "
        "the last cell, and the raw results still hash to what they did")


def zeile_evidenzindex() -> Zeile:
    """Does the evidence index still describe the trees it names?

    The index is the only published account of three evidence trees that are
    not in the export. If it drifts from them, a reader is being asked to
    trust a description of something that has since changed -- which is worse
    than no description, because it looks like one.
    """
    befehl = "python3 tools/evidence_index.py"
    rc, aus = _py("tools/evidence_index.py")
    return Zeile("evidence_index", PASS if rc == 0 else FAIL,
                 (aus.splitlines()[-1] if aus else "?")[:70], befehl)


def zeile_audit() -> Zeile:
    """The paper's own citation and numbers audit, every check of it.

    It was not a row, and one of its checks had been red since before the
    benchmark it cites had run. A gate nobody looks at is a gate that teaches
    people not to look.
    """
    befehl = "python3 tools/audit_refs.py <each check>"
    rot = []
    for name in AUDIT_PRUEFUNGEN:
        rc, _ = _py("tools/audit_refs.py", name, timeout=600)
        if rc != 0:
            rot.append(name)
    return Zeile(
        "paper_audit", PASS if not rot else FAIL,
        f"{len(AUDIT_PRUEFUNGEN) - len(rot)} of {len(AUDIT_PRUEFUNGEN)} checks"
        + (" -- red: " + ", ".join(rot) if rot else ""),
        befehl,
        "`coverage` is red on a maintenance item paper/AUDIT.md itself flags "
        "and explains: paper/NUMBERS.md catalogues the 2 of a '2 of 3' ratio "
        "and not the 3. It is a documented, deliberately deferred operator "
        "item, not an unexamined failure",
        beratend=rot == ["coverage"])


def zeile_ci() -> Zeile:
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
    befehl = ("python3 tools/exact_head_ci.py --run-id ID --export-commit SHA")
    pfad = HOH / "dogfood/external-ci/EXACT_HEAD_CI.json"
    if not pfad.is_file():
        return Zeile("external_ci", NOT_RUN,
                     "no external CI evidence recorded", befehl,
                     "the external run is evidence a release needs, and a "
                     "checkout that cannot produce it is a checkout that "
                     "cannot declare itself ready")
    c = json.loads(pfad.read_text())

    import importlib.util as _il

    spec = _il.spec_from_file_location("exact_head_ci",
                                       HIER / "exact_head_ci.py")
    eh = _il.module_from_spec(spec)
    spec.loader.exec_module(eh)
    try:
        jetzt = eh.export_path_digests(HOH)
    except SystemExit as exc:
        return Zeile("external_ci", FAIL, f"the export cannot be digested: {exc}",
                     befehl)

    damals = c.get("path_digests") or {}
    if not damals:
        return Zeile("external_ci", FAIL,
                     "the recorded run carries no per-path digests, so what it "
                     "tested cannot be compared with this tree", befehl)
    abweichend = sorted(set(damals) ^ set(jetzt)) + sorted(
        p for p in set(damals) & set(jetzt) if damals[p] != jetzt[p])
    # The gate writes its own report, and the report is published. So the
    # board and the ledger it renders are INCLUDE files that every run of this
    # gate rewrites -- which means an aggregate comparison is red forever, for
    # a reason that has nothing to do with the software. Those files are named
    # here and only they are tolerated; anything else differing is stale
    # evidence and fails.
    BERICHTE = {"docs/READINESS.md", "CLAIMS.md", "CLAIMS.json"}
    echt = [p for p in abweichend if p not in BERICHTE]
    if echt:
        return Zeile(
            "external_ci", FAIL,
            f"the recorded run tested a different export: "
            f"{len(echt)} path(s) differ beyond this gate's own reports "
            f"({', '.join(echt[:3])}). Re-export, re-run CI, record it again.",
            befehl,
            "a CI result is evidence about a set of bytes, not about a branch "
            "name; reusing it after the export changed would be citing a "
            "measurement of something else")

    rot = [j["name"] for j in (c.get("jobs") or [])
           if j.get("conclusion") != "success"]
    return Zeile(
        "external_ci",
        PASS if c.get("run_conclusion") == "success" and not rot else FAIL,
        f"{c.get('run_conclusion')} on {str(c.get('export_commit'))[:12]} "
        f"({len(c.get('jobs') or [])} job(s)"
        + (f", red: {', '.join(rot)}" if rot else "")
        + f"); sandbox_external_env = {c.get('sandbox_external_env')}"
        + (f"; differs only by this gate's own reports: "
           f"{', '.join(sorted(set(abweichend) & BERICHTE))}"
           if abweichend else ""),
        befehl,
        "the sandbox line is read from its step, not its job: a green job "
        "whose relevant step was skipped has measured nothing, and "
        "UNSUPPORTED_ENVIRONMENT is that state rather than a pass")


#: The dispositions `docs/ROUTING.md` may record. Anything else -- including
#: nothing at all -- is an open question, not a disposition.
ROUTING_ENTSCHIEDEN = ("DEFERRED_ON_EVIDENCE", "ADOPTED", "REJECTED")


def zeile_routing() -> Zeile:
    """Is the routing question disposed of, and does the file still say so?

    It returned the literal `PASS` regardless of what it read: a file saying
    `ABANDONED`, or carrying no decision line at all, produced a green row,
    and a missing file took the whole tool down with a traceback. A row that
    cannot fail is a permanent green sitting inside a conjunction, which is
    the shape of a gate that teaches people not to look.
    """
    befehl = "read docs/ROUTING.md"
    pfad = HOH / "docs/ROUTING.md"
    if not pfad.is_file():
        return Zeile("routing", NOT_RUN, "docs/ROUTING.md is missing", befehl)
    m = re.search(r"routing_decision\s*=\s*(\S+)", pfad.read_text())
    wert = m.group(1) if m else "no routing_decision line"
    return Zeile("routing", PASS if wert in ROUTING_ENTSCHIEDEN else FAIL,
                 wert, befehl,
                 "a disposition, not an open question: the condition for "
                 "revisiting it is named and was checked")


def zeilen(quick: bool) -> list[Zeile]:
    return [
        zeile_tests(quick),
        zeile_lint(),
        zeile_claims(),
        zeile_union(),
        zeile_meta(),
        zeile_confinement(),
        zeile_budget(),
        zeile_closure(),
        zeile_telemetrie(),
        zeile_prereg(),
        zeile_benchmark_v2(),
        zeile_benchmark_v3(),
        zeile_export(),
        zeile_install(quick),
        zeile_attribution(),
        zeile_evidenzindex(),
        zeile_audit(),
        zeile_ci(),
        zeile_routing(),
    ]


def verdikt(rows: list[Zeile]) -> tuple[str, list[str]]:
    offen = [r.name for r in rows if r.zustand != PASS and not r.beratend]
    return (PASS if not offen else FAIL), offen


def markdown(rows: list[Zeile], kopf: str) -> str:
    stand, offen = verdikt(rows)
    z = [
        "# Readiness: what holds, what does not, and how each was measured",
        "",
        "Generated by `python3 tools/readiness.py --write`. Every row names the",
        "command that produced it, and the verdict is the conjunction of the",
        "rows rather than a judgement typed above them. A row this tool cannot",
        "evaluate is `NOT_RUN`, which is never a pass.",
        "",
        f"Measured at `{kopf}` on "
        + datetime.now(UTC).strftime("%Y-%m-%d") + ".",
        "",
        f"    TECHNICALLY_STABLE_READY = {'yes' if stand == PASS else 'no'}",
        "",
    ]
    if offen:
        z += ["Open, and each one blocking: " + ", ".join(offen) + ".", ""]
    z += ["| condition | state | measured | command |", "|---|---|---|---|"]
    for r in rows:
        marke = r.zustand + (" (advisory)" if r.beratend and r.zustand != PASS
                             else "")
        z.append(f"| `{r.name}` | {marke} | {r.wert} | `{r.befehl}` |")
    z += ["", "## Why some rows are advisory", ""]
    for r in rows:
        if r.warum:
            z.append(f"* **`{r.name}`** — {r.warum}.")
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


def _anker(zeile: str) -> str:
    import hashlib

    return hashlib.sha256(re.sub(r"\s+", " ", zeile.strip()).encode()).hexdigest()


def _ledger_nachziehen(ziel: Path) -> int:
    """Re-anchor this document's own ledger entries after regenerating it.

    Its rows change whenever a gate's result changes, which is the point of
    it -- and this repository's policy is that a prose markdown file carrying
    numbers is a claim surface. Doing that by hand after every gate run is how
    O139 happened, so the tool that writes the rows writes their anchors.

    Entries are **updated in place and never removed**: the row set is fixed
    in code, so the count only changes when a row is added, and removing an id
    would leave the gap the ledger refuses.
    """
    pfad = HOH / "CLAIMS.json"
    if not pfad.is_file():
        return 0
    d = json.loads(pfad.read_text())
    zeilen = ziel.read_text().splitlines()
    rel = ziel.relative_to(HOH).as_posix()

    zeilen_claim = [(i, z) for i, z in enumerate(zeilen, 1) if z.startswith("| `")]
    zeilen_nicht = [(i, z) for i, z in enumerate(zeilen, 1)
                    if z.startswith("Open, and each one blocking")
                    or z.startswith("* **`") or z.startswith("| condition |")
                    or z.startswith("|---") or z.startswith("Measured at")]

    def nachziehen(schlüssel: str, praefix: str, neue, notiz: dict) -> int:
        vorhanden = [e for e in d[schlüssel]
                     if e.get("where", "").startswith(rel + ":")]
        n = 0
        for e, (i, z) in zip(vorhanden, neue, strict=False):
            e["where"] = f"{rel}:{i}"
            e["text"] = z.strip()
            e["anchor_digest"] = _anker(z)
            n += 1
        if len(neue) > len(vorhanden):
            hoechste = max((int(x["id"].split("-")[1]) for x in d[schlüssel]),
                           default=0)
            for k, (i, z) in enumerate(neue[len(vorhanden):], 1):
                d[schlüssel].append({
                    "id": f"{praefix}-{hoechste + k:03d}",
                    "text": z.strip(), "where": f"{rel}:{i}",
                    "anchor_digest": _anker(z), **notiz})
                n += 1
        return n

    n = nachziehen("claims", "C", zeilen_claim, {
        "status": "SUPPORTED", "evidence": ["file:tools/readiness.py:1"],
        "note": "Generated by tools/readiness.py; the row's own command column "
                "is how it is re-derived.",
        "local_only_resolution":
            "Resolved by the main orchestrator by running "
            "python3 tools/readiness.py --write, which re-runs every command "
            "the row names and rebuilds the table from their output.",
    })
    n += nachziehen("not_claims", "N", zeilen_nicht, {
        "reason": "a table frame, a provenance line, or the reason a row is "
                  "advisory; the measurement is in the row itself",
    })
    pfad.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    rc, kopf = _lauf("git", "rev-parse", "--short", "HEAD")
    kopf = kopf if rc == 0 else "(no git head)"
    rows = zeilen(args.quick)
    stand, offen = verdikt(rows)

    if args.json:
        print(json.dumps({
            "head": kopf, "verdict": stand, "open": offen,
            "rows": [r.__dict__ for r in rows],
        }, indent=2))
    else:
        for r in rows:
            marke = "adv " if r.beratend and r.zustand != PASS else "    "
            print(f"  {marke}{r.zustand:<8s}{r.name:<32s}{r.wert}")
        print(f"\n  TECHNICALLY_STABLE_READY = "
              f"{'yes' if stand == PASS else 'no'}")
        if offen:
            print("  open: " + ", ".join(offen))
    if args.write:
        ziel = HOH / "docs/READINESS.md"
        ziel.write_text(markdown(rows, kopf), encoding="utf-8")
        print(f"wrote {ziel}")
        n = _ledger_nachziehen(ziel)
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
            rc, aus = _py("tools/check_claims.py", "render")
            print(aus.splitlines()[-1] if aus else
                  f"re-render exited {rc}")
    return 0 if stand == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
