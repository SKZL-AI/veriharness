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


def _run(*args: str, cwd: Path = HOH, timeout: int = 1800,
         stdin: str | None = None) -> tuple[int, str]:
    # The caller's PATH is kept. A reduced one is right for a *check command*
    # -- that is `runner.py`'s job and its reasons -- and wrong here: this
    # tool runs the project's own development tools, and pinning them to
    # /usr/bin made `ruff` unfindable on a machine where it is installed
    # exactly where the CI finds it.
    import os

    # `stdin` exists for the one row whose subject reads a payload rather than
    # arguments: a PreToolUse hook is handed JSON on standard input, and asking
    # it anything else would test a different code path than the one that runs.
    p = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                       timeout=timeout, check=False, input=stdin,
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


#: The board's budget for the whole union gate. It must exceed the sum of the
#: gate's own command budgets (300 + 60 + 600 + 1800 + 600 = 3360 s), or this
#: budget fires first and the gate never gets to say which command ran out.
UNION_GATE_TIMEOUT_S = 3600


def row_union() -> Row:
    command = "python3 tools/union_gate.py"
    try:
        rc, out = _py("tools/union_gate.py", timeout=UNION_GATE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # One fixed text, no `why_text`: a line that appears on the board only
        # while the gate times out is re-anchored by _catch_up_ledger and stays
        # behind as a stale entry after the gate recovers (ugspec-a, finding 3).
        return Row("union_invariants", NOT_RUN,
                   f"union_gate.py: TIMEOUT (budget {UNION_GATE_TIMEOUT_S} s)",
                   command)
    lines = out.splitlines()
    # Only an invariant's own status counts. A SKIPPED reason that quotes a
    # tool's `FAILED`, or a NOT_RUN mentioned mid-line, is not that status.
    red = [z for z in lines if re.match(r"U[1-5]: FAIL\b", z)]
    not_run = [z for z in lines if re.match(r"U[1-5]: NOT_RUN\b", z)]
    if red:
        state = FAIL
    elif not_run:
        state = NOT_RUN
    elif rc != 0:
        state = FAIL
    else:
        state = PASS
    if red or not_run:
        value_ = "; ".join(z[:60] for z in red + not_run)
    elif rc != 0:
        value_ = f"union_gate.py exited {rc} with no FAIL or NOT_RUN line"
    else:
        value_ = "U1-U5 pass"
    return Row("union_invariants", state, value_, command)


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


def row_preserve() -> Row:
    """Is this repository's no-delete policy present *and* deciding anything?

    O213. The rule "preserve, version, archive -- never delete" was enforced for
    every role in a run and for nobody outside one, which is where it broke: the
    orchestrator removed a parked duplicate of a run's evidence and the
    machine-global denylist allowed it, correctly, by its own design rule.

    Two questions, because either one alone is satisfiable without the other. Is
    the policy complete in this tree (`preserve.py digest` over all three files)?
    And does the wiring in front of a command actually refuse? The second is
    asked by handing `preserve_hook.py` the payload shape the harness really
    sends, once for a path that must be refused and once for cleanup that must
    stay free -- a row that only checked the file would stay green while the hook
    was unwired, which is the whole failure mode this replaces.
    """
    command = ("python3 tools/preserve.py digest && "
               "python3 tools/preserve_hook.py  # effective decision")
    rc, out = _py("tools/preserve.py", "digest")
    if rc != 0:
        last = [z for z in out.strip().splitlines() if z.strip()]
        return Row("preserve", FAIL,
                   (last[-1].strip()[:90] if last else "policy incomplete"),
                   command,
                   "a run cannot claim the no-delete invariant under a policy "
                   "that is not in its tree")

    protected = json.dumps({"tool_input": {"command":
                            "rm -rf dogfood/p2-19"}})
    ordinary = json.dumps({"tool_input": {"command": "rm -rf build"}})
    blocked, _ = _run(sys.executable, "tools/preserve_hook.py", stdin=protected)
    allowed, _ = _run(sys.executable, "tools/preserve_hook.py", stdin=ordinary)
    if blocked != 2:
        return Row("preserve", FAIL,
                   f"the hook let a protected path through (exit {blocked})",
                   command,
                   "a policy nothing consults is a document, not a guard")
    if allowed != 0:
        return Row("preserve", FAIL,
                   f"the hook blocked ordinary cleanup (exit {allowed})",
                   command,
                   "a guard that blocks everything is a guard that gets "
                   "switched off, taking the cases that matter with it")
    return Row("preserve", PASS,
               "policy complete; protected path refused, cleanup free",
               command,
               "project- and evidence-owned state must not be destroyable by "
               "an autonomous run, and the refusal must name the safe operation")


def row_self_hosted_topology() -> Row:
    """Does the O177 guard still refuse the arrangement it exists to refuse?

    A live self-test rather than a configuration read. The question is put to the
    instrument in the one form always available in a source checkout: this tree,
    checked against itself. A development worktree belonging to the same
    repository as the integration tree is exactly the falsified topology, so the
    only correct answer is a refusal -- and this row is PASS when the check says
    no.

    Stated that way round on purpose. A row that asserted "the tool runs" would
    stay green if the comparison inside it were inverted, which is the defect
    that would matter: it was proposed, in this programme, that runs move back
    into worktrees of the live repository.
    """
    here = str(HOH)
    command = ("python3 tools/self_hosted_topology.py check --repo . "
               "--pin $(git rev-parse HEAD) --integration .  # must refuse")
    _, head = _run("git", "rev-parse", "HEAD")
    rc, out = _py("tools/self_hosted_topology.py", "check", "--repo", here,
                  "--pin", head.strip(), "--integration", here)
    if rc == 0:
        return Row("self-hosted-topology", FAIL,
                   "the guard accepted a run in the integration repository",
                   command,
                   "O177: a commit in the integration tree moves history under "
                   "the run, and the witness cannot tell that from the agent")
    if "O177" not in out:
        return Row("self-hosted-topology", FAIL,
                   "refused, but not for the O177 reason: " + out.strip()[:70],
                   command,
                   "a refusal for the wrong reason would pass this row while "
                   "the topology check itself was broken")
    return Row("self-hosted-topology", PASS,
               "the same-repository topology is refused, naming O177",
               command,
               "isolation is not traded for an attribution category; a "
               "byte-identical import binds the claim either way")


def row_unit_drift() -> Row:
    """Does the automation installed on this machine match what the repo owns?

    #37. The installed `hoh-fortsetzen.service` called a verb renamed a week
    earlier and failed on every tick for 18 days; the journal said so and no
    board did. This row asks `tools/unit_drift.py`, which only reads unit files
    and `systemctl --user show` -- it never installs, enables or reloads, since
    switching on an automation is the operator's action, not a check's.

    Not installing any unit passes only in the supervised mode; when an open
    project registers a driver (unattended mode) the continuation unit must be
    installed, canonical and healthy (`unit_drift.operating_mode`). An installed `hoh-*` unit that is obsolete, drifted, calls a verb
    the CLI no longer has, or is failing is blocking: installed automation that
    silently does nothing is the failure this row exists to make loud.
    """
    command = "python3 tools/unit_drift.py --expect auto"
    rc, out = _py("tools/unit_drift.py", "--expect", "auto", "--json")
    try:
        rep = json.loads(out[out.index("{"):])
    except (ValueError, json.JSONDecodeError):
        return Row("unit-drift", NOT_RUN, _one_cell(out, 90) or "no output",
                   command, "a check that produced no report is not a pass")
    failing = rep.get("failing") or []
    if rc == 1:
        codes = sorted({f["code"] for f in failing})
        units = sorted({f["unit"] for f in failing})
        return Row("unit-drift", FAIL,
                   f"{', '.join(codes)} on {', '.join(units)}"[:120], command,
                   "an installed unit that fails on every tick is automation "
                   "the operator believes is running; reinstalling it from "
                   "ops/ is the operator's action, not this check's")
    if rc == 2:
        return Row("unit-drift", NOT_RUN,
                   "systemd user state unreadable for an installed unit",
                   command, "a unit whose state cannot be read is not healthy")
    installed = rep.get("installed") or []
    mode = rep.get("mode", "supervised")
    return Row("unit-drift", PASS,
               (f"{mode}: installed units match ops/ and are healthy" if installed
                else f"{mode}: no hoh-* unit installed, and none is required"),
               command,
               "the repository's unit is the canonical one; an installed "
               "copy is judged against it, never the other way round")


DURABILITY_DIR = "dogfood/p2-durability"
DURABILITY_RECORD = "P2_DURABILITY_ACCEPTANCE.json"


def row_durability() -> Row:
    """Do the P2 exit conditions hold under faults, re-derived from raw evidence?

    `tools/durability_e2e.py` runs governed masters as real processes, kills
    them from outside, rolls them over, lets them go stale, parks a node on a
    usage limit and lets a periodic tick continue it. This row does not run
    that (it takes minutes and real processes); it re-derives the committed
    acceptance record from the newest preserved evidence and reads it. A
    record that no longer equals its derivation, a negative control the
    instrument failed to catch, or an unsatisfied requirement is a FAIL.
    """
    base = HOH / DURABILITY_DIR
    record = base / DURABILITY_RECORD
    # The evidence the register's probe names, so this row and the capability
    # matrix judge the same directory (review B2); the newest one only when no
    # probe names any.
    named = None
    try:
        reg = json.loads((HOH / "program/v3_3/REQUIREMENTS.json").read_text())
        pairs = {(pr.get("evidence_dir"), pr.get("path"))
                 for q in reg.get("requirements", [])
                 for pr in [q.get("probe") or {}]
                 if pr.get("deriver") == "durability_e2e.py"}
    except (OSError, ValueError):
        pairs = set()
    if len(pairs) > 1:
        return Row("durability", FAIL,
                   f"the register's durability probes name {len(pairs)} different records",
                   "tools/readiness.py row_durability",
                   "one set of evidence per programme state, or the row and the matrix "
                   "can disagree")
    if pairs:
        evid, path = next(iter(pairs))
        e, r = (HOH / str(evid)).resolve(), (HOH / str(path)).resolve()
        if not (e.is_relative_to(HOH.resolve()) and r.is_relative_to(HOH.resolve())):
            return Row("durability", FAIL, "a durability probe points outside the tree",
                       "tools/readiness.py row_durability",
                       "evidence must travel with the tree it is about")
        named = (e, r)
    if named is not None:
        # The register names the evidence: judge exactly that, never a
        # fallback -- a missing named directory is a failure, not a reason to
        # look at some other one (review B3, 11).
        if not named[0].is_dir() or not named[1].is_file():
            return Row("durability", FAIL,
                       "the register names durability evidence that is not in the tree",
                       f"python3 tools/durability_e2e.py derive --evidence {named[0]} ...",
                       "a probe pointing at nothing proves nothing")
        record = named[1]
    runs = ([named[0]] if named is not None else
            sorted((d for d in base.glob("*") if d.is_dir() and STAMP.match(d.name)),
                   key=lambda d: _stamp_key(d)) if base.is_dir() else [])
    command = (f"python3 tools/durability_e2e.py derive --evidence {DURABILITY_DIR}/<newest> "
               f"--out {DURABILITY_DIR}/{DURABILITY_RECORD} --check")
    if not runs or not record.is_file():
        return Row("durability", NOT_RUN, "no preserved durability evidence", command,
                   "the P2 exit conditions are not shown by a test that did not run")
    rc, out = _py("tools/durability_e2e.py", "derive", "--evidence", str(runs[-1]),
                  "--out", str(record), "--check")
    if rc != 0:
        return Row("durability", FAIL, "record does not re-derive from its evidence",
                   command, "a hand-edited or stale record is not evidence")
    data = json.loads(record.read_text(encoding="utf-8"))
    stressors = data.get("stressors", {})
    summary = " ".join(f"{k}={v}" for k, v in sorted(stressors.items()))
    if data.get("evidence_dirty"):
        return Row("durability", FAIL, "evidence was taken on a dirty tree", command,
                   "evidence not bound to a commit binds to nothing")
    sys.path.insert(0, str(HERE))
    from durability_e2e import stale_paths  # noqa: PLC0415

    changed = stale_paths(data.get("evidence_head"))
    if changed is None or changed:
        return Row("durability", FAIL,
                   ("evidence commit unknown here" if changed is None else
                    "stale: changed since the evidence: " + ", ".join(changed[:4]))[:120],
                   command, "evidence about code that has since changed is about other code")
    failed = sorted(n for n, s in (data.get("scenarios") or {}).items()
                    if s.get("status") == "FAIL")
    if failed:
        return Row("durability", FAIL, "failed scenarios: " + ", ".join(failed), command,
                   "a scenario that failed is a finding even where no requirement "
                   "rests on it alone")
    if not data.get("instrument_falsifiable"):
        return Row("durability", FAIL, "a negative control was not caught", command,
                   "an instrument that passes a known-bad control proves nothing")
    if not data.get("requirement_satisfied"):
        return Row("durability", FAIL, summary[:120], command,
                   "at least one P2 exit condition is not satisfied by the evidence")
    return Row("durability", PASS, summary[:120], command,
               "every registered exit condition re-derives from preserved raw evidence; "
               "a stressor marked NOT_RUN is named, not counted")


def _one_line(exc: BaseException, limit: int = 160) -> str:
    """One bounded line from an exception, for a value that lands in a table cell.

    A pydantic `ValidationError` is multi-line, and `docs/READINESS.md` interpolates
    a row's value straight into markdown: the unbounded version broke the table,
    left a row with no closing pipe and three stray lines in the table body, and
    failed the ledger's own "no claim begins mid-sentence" test.
    """
    # O226/F-7: whitespace was collapsed and the pipe was not, and the test that
    # claimed the cell was safe passed only because that particular error happened to
    # contain none. A key named `a | b` in a refused record is enough.
    text = " ".join(str(exc).split()).replace("|", "/")
    return f"{type(exc).__name__}: {text}"[:limit]


def _one_cell(text: str, limit: int = 400) -> str:
    """One markdown-table-safe cell: no newline, no pipe, bounded.

    Applied to the whole detail and not only to an exception message, because
    O225/C6 was not really about pydantic -- it was about this row interpolating
    text it had not bounded into a table that a reader treats as a status board.
    """
    return " ".join(str(text).split()).replace("|", "/")[:limit]


#: Where a P2 review's preserved artefacts live. The gate finds them here, **by its
#: own rule**, and never by asking the record where to look. O227/G2 and F-H1: the
#: previous version resolved `HOH / rec.findings_source`, and the record supplied that
#: field -- so a record could name a file in `/tmp`, a path outside the repository via
#: `..` or an absolute path, or (the sharpest form) **itself**, since a record is a
#: JSON object with a `findings` key. Every binding added across five rounds had been
#: another field of the document being checked, which is the same defect as reading
#: `complete` out of it.
REVIEW_DIR = "dogfood/p2-gate"
RETURNED_FINDINGS = "findings.json"
PRESERVED_CAPSULE = "capsule-p2.json"

#: A review directory's name must be a UTC stamp. O228/J4: the rule was "newest by
#: name" with no shape required, and ASCII puts every lowercase name after every
#: timestamp -- so `zzzz`, `draft`, `notes` or `staging` won, permanently shadowing a
#: genuine review, and a *draft* of the next review won the moment it held both files.
#: The stamp is also what makes "an older directory is history" true rather than
#: assumed.
#: Four or six digits after the `T`: this tree uses both (`20260924T0030Z` for review
#: directories, `...T084449Z` for versioned files), and a pattern that accepted only one
#: of them rejected the programme's own preserved review -- which would have been a
#: fourth "gate that can never close", caught here by running it.
STAMP = re.compile(r"^\d{8}T\d{4}(?:\d{2})?Z$")


def _is_inside(candidate: Path, root: Path) -> bool:
    """A directory of `root` that really is one. (O229/L1)

    `Path.is_dir()` follows symlinks, so `ln -s /tmp/anything dogfood/p2-gate/<stamp>`
    gave the gate a review that lives outside the repository -- nothing preserved in the
    tree at all, one symlink in `git status`, and a cell naming a directory that looks
    like every other review. The whole point of locating the review by the gate's own
    rule was that *"a record could name a file in /tmp, a path outside the repository via
    `..`, or an absolute path"*; the record no longer can, and the filesystem could.
    """
    try:
        if candidate.is_symlink() or not candidate.is_dir():
            return False
        return candidate.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _review_root(hoh: Path) -> Path | None:
    """`hoh / REVIEW_DIR` when it resolves inside `hoh`, else None. (T-PSD4)

    `_is_inside` checks each review directory against the review root, so a symlinked
    `dogfood/p2-gate` (or `dogfood`) leading out of the repository made every directory
    under it look in-tree, and the gate read a foreign review and its findings. The
    root is checked against the repository first, as `programme_supervisor` does.
    Unresolvable is outside.
    """
    root = hoh / REVIEW_DIR
    try:
        return root if root.resolve().is_relative_to(hoh.resolve()) else None
    except (OSError, ValueError, RuntimeError):
        return None


def _stamp_key(directory: Path) -> tuple:
    """Order review directories by time rather than by ASCII. (O229/L8)"""
    return _stamp_key_of(directory.name)


def _stamp_key_of(name: str) -> tuple:
    head, _, tail = str(name).partition("T")
    digits = "".join(c for c in tail if c.isdigit())
    return (head, (digits + "00")[:6])


def _canonical_review(hoh: Path) -> tuple[Path | None, list[str]]:
    """The review the gate will read, chosen by the gate: the newest stamped
    directory under `REVIEW_DIR` that holds both preserved artefacts.

    Newest, because a review is retaken rather than amended -- so a later stamped
    directory is a later review, and an older one is history. Nothing the record says
    influences this choice.

    T-PSD1: two newest directories at the same parsed time (`T1015Z`, `T101500Z`) are
    ambiguous, and the gate says so instead of letting `iterdir` order pick one -- the
    rule `drift_baseline_for` applies to the same tie. T-PSD4: a review root that
    resolves outside the repository holds no review of it.
    """
    root = _review_root(hoh)
    if root is None:
        return None, [f"{REVIEW_DIR} resolves outside the repository, so no review "
                      "under it is this tree's"]
    if not root.is_dir():
        return None, [f"{REVIEW_DIR} does not exist, so no review has been preserved"]
    holds_both = [d for d in root.iterdir()
                  if _is_inside(d, root) and (d / RETURNED_FINDINGS).is_file()
                  and (d / PRESERVED_CAPSULE).is_file()]
    stamped = sorted((d for d in holds_both if STAMP.match(d.name)),
                     key=_stamp_key)
    if not stamped:
        unstamped = sorted(d.name for d in holds_both)
        extra = (f" (ignoring {', '.join(unstamped)}, whose name is not a UTC stamp)"
                 if unstamped else "")
        return None, [f"no directory under {REVIEW_DIR} holds both "
                      f"{RETURNED_FINDINGS} and {PRESERVED_CAPSULE} under a UTC-stamped "
                      f"name, so there is no preserved review for the gate to read"
                      + extra]
    tied = sorted(d.name for d in stamped if _stamp_key(d) == _stamp_key(stamped[-1]))
    if len(tied) > 1:
        return None, [f"ambiguous review: {' and '.join(tied)} are the newest under "
                      f"{REVIEW_DIR} at the same time, so the gate cannot tell which "
                      "one is current"]
    return stamped[-1], []


def _preserved_reviews(ps) -> list[tuple[str, list]]:
    """Every preserved review, as `(stamp, findings)`. (O229/L2)

    The previous design asked only the *newest earlier* review what was open, and a
    reviewer showed that made the finding history one review deep: any single review that
    honestly returned `[]` erased every earlier finding from every later brief,
    permanently -- no adversary required. It also used a *different* predicate here than
    `_canonical_review` used, requiring only `findings.json`, so a directory holding
    nothing but an empty findings file was invisible as a review and decisive as history.

    One predicate now, over every stamped in-tree review, and the accumulation in
    `ps.resolution_state` is order-independent: an added directory can raise or resolve,
    and cannot forget.
    """
    root = _review_root(HOH)
    if root is None or not root.is_dir():
        return []
    out: list[tuple[str, list]] = []
    for d in sorted((d for d in root.iterdir()
                     if _is_inside(d, root) and STAMP.match(d.name)
                     and (d / RETURNED_FINDINGS).is_file()),
                    key=_stamp_key):
        try:
            raw = json.loads((d / RETURNED_FINDINGS).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        returned = raw.get("findings", []) if isinstance(raw, dict) else raw
        out.append((d.name, [f for f in returned if isinstance(f, dict)]))
    return out


def _canonical_brief(ps, *, register, matrix, snapshot=None, previous=None, before=None):
    """The one brief this gate accepts, from the tool that the CLI also calls.

    O229/L7 and M2: this used to be a private helper here while `main()` built something
    else, so the artefact the gate demanded could not be produced by any command -- the
    fourth gate that could never close, and the one that made an honest review
    unpreservable. `programme_supervisor.canonical_brief` is now the single derivation and
    `--canonical-brief` prints it, so the gate and the operator call the same code.

    `snapshot` and `previous` are accepted and ignored; they were parameters of the
    carry-forward design that O229/L2 retired, and the signature is kept so a caller that
    still passes them is not broken silently.

    `before` is the stamp of the review under check: the brief's drift baseline is the
    most recent preserved review strictly before it, so a review is never its own
    baseline. `None` means before now, which is what the operator's CLI builds.

    `repo` is this gate's own `HOH` (patched in tests), passed rather than derived, so
    the brief digests the tree the gate inspects.
    """
    return ps.canonical_brief(register=register, matrix=matrix, phase="P2",
                              review_root=HOH / REVIEW_DIR, before=before, repo=HOH)


def _capsule_binding_problems(rec, ps, *, register, matrix, snapshot) -> list[str]:
    """Is the record bound to the brief a supervisor was actually given? (O227/G10)

    The previous version rebuilt the capsule from the *live* state and compared the
    record's `capsule_digest` against that. It always agreed, for a reason that took
    five rounds to see: the record had been re-recorded, and re-recording stamps the
    live digest. So the check proved that the record was recent, which is a fact about
    the recorder, and reported it as a fact about what was reviewed.

    Measured on this tree: the preserved `capsule-p2.json` digests to `dcb933de...`;
    every version of the record from v8 on claims `47b6671f...`, which is exactly what
    a rebuild produces now. **The record was bound to a brief no supervisor ever saw.**

    So the capsule digest is now compared against the *preserved file*, and staleness
    is a separate question answered by comparing that file against a rebuild. Two
    distinct facts that the single check had been conflating:

    * *is this record about the preserved brief?* -- provenance;
    * *is the preserved brief still about this board?* -- currency.
    """
    directory, problems = _canonical_review(HOH)
    if problems:
        return problems
    path = directory / PRESERVED_CAPSULE
    shown = f"{REVIEW_DIR}/{directory.name}/{PRESERVED_CAPSULE}"
    try:
        preserved = ps.ProgrammeCapsule.model_validate(
            json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001 - an unreadable brief is a failed binding
        return [f"{shown} cannot be read as a capsule: {_one_line(exc)}"]

    # O228/J1, J2, J8 and S1, which are four spellings of one mistake: the previous
    # version *read* the brief's coverage (`selection.left_out`), and never read its
    # phase at all. Both are fields the brief supplies, and `Selection`'s only validator
    # checks its own arithmetic -- so a one-item brief that says `considered=1,
    # included=1, left_out=0` was accepted as a whole-phase brief, and a brief for *P1*
    # closed P2. That is the defect O227 claims to have retired, one layer out: trust
    # moved from the decisions onto the brief, and the brief is written by the same party.
    #
    # So the gate does not ask the brief what it covers. **It derives the brief it
    # would accept** and compares digests. There is exactly one canonical brief for a
    # given board, and everything the previous version read off the document -- phase,
    # coverage, limits, the seal it was cut from -- is fixed by the derivation instead:
    #
    #   * a brief for another phase digests differently;
    #   * a narrow brief digests differently, whatever its selection claims;
    #   * a brief cut from an older board digests differently, which is currency;
    #   * `--max-evidence 1` (O228/J8, zero summaries under an honest `left_out == 0`)
    #     digests differently;
    #   * and a phase larger than the CLI's default `--max-items 50` cannot produce a
    #     brief the gate refuses for leaving items out, because the derivation sizes
    #     itself to the phase (O228/S1 -- eighteen items of headroom on P2 today, and a
    #     fourth "gate that can never close" waiting at fifty-one).
    try:
        canonical = _canonical_brief(ps, register=register, matrix=matrix,
                                     before=directory.name)
    except Exception as exc:  # noqa: BLE001 - an underivable brief is not a pass
        return [*problems, f"the canonical P2 brief could not be derived: "
                f"{_one_line(exc)}"]
    canonical_digest = ps.capsule_digest(canonical)
    preserved_digest = ps.capsule_digest(preserved)
    if preserved_digest != canonical_digest:
        problems.append(
            f"{shown} is not the brief this gate would put to a supervisor for P2 on "
            f"this board (preserved {preserved_digest[:12]}..., canonical "
            f"{canonical_digest[:12]}...): either it is about another phase or another "
            "board, or it does not cover the phase")
    if rec.capsule_digest != preserved_digest:
        problems.append(
            f"the record's capsule digest is not the digest of {shown}: the review is "
            "not bound to the brief that was preserved for it")
    return problems


def _findings_binding_problems(rec, ps) -> list[str]:
    """Is the record about the findings a supervisor actually returned? (O226/E2)

    The record used to bind itself to the supervisor's *input* -- the capsule -- and to
    nothing on the way out, so the findings were editable after the fact: handing
    `--findings` the real fourteen with the nine blocking ones deleted produced
    `complete (5 findings)`, under the genuine review's own id, and the board passed it.

    So the record now names the file it was given and a digest over the set as
    returned, and this re-reads that file and re-digests it. A forgery now has to alter
    two separately preserved artefacts consistently, and any mismatch is named here.

    What this does **not** establish, said plainly because the sentence matters more
    than the check: that the findings came from an independent supervisor at all. That
    is a property of the dispatch, not of any file, and no amount of digesting can
    recover it. A review of P2 that genuinely found nothing is indistinguishable at
    this layer from `echo '[]' > findings.json`, and the only thing standing between
    them is that the second one has to be written by someone willing to write it.
    """
    directory, problems = _canonical_review(HOH)
    if problems:
        return problems
    if not rec.findings_digest:
        return ["the record carries no findings digest, so nothing binds it to what "
                "the supervisor returned (O226/E2)"]
    # The record's own `findings_source` is still compared -- a record that claims to
    # be about some other file is worth reporting -- but it is never *followed*.
    claimed = str(rec.findings_source or "")
    canonical = f"{REVIEW_DIR}/{directory.name}/{RETURNED_FINDINGS}"
    if claimed and claimed != canonical:
        problems.append(f"the record names {claimed} as its findings source; the gate "
                        f"reads {canonical}, which is the newest preserved review")
    source = directory / RETURNED_FINDINGS
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [*problems, f"{canonical} cannot be read: {_one_line(exc)}"]
    returned = raw.get("findings", []) if isinstance(raw, dict) else raw
    if not isinstance(returned, list) or any(
            not isinstance(f, dict) for f in returned):
        return [*problems, f"{canonical} does not hold a list of finding objects, so "
                "the gate cannot tell what was returned (O227/G1)"]
    try:
        if ps.findings_digest(returned) != rec.findings_digest:
            return [*problems, f"the findings in {canonical} no longer digest to what "
                    "the record was written from: one of the two has been edited"]
    except TypeError as exc:
        return [*problems, f"{canonical} cannot be digested: {_one_line(exc)}"]
    # Every finding that came back has to be *accounted for* -- recorded as a finding
    # or named as a validation failure. This is the half that closes O226/E1 rather
    # than merely making it harder: emptying `validation_failures` by hand leaves the
    # record with no findings to re-validate, so re-validation alone catches nothing,
    # and the row read `complete: true` off a record that had silently dropped a
    # finding the product had refused. The digest proves the returned set is intact;
    # this proves the record says something about all of it.
    accounted = ({f.finding_id for f in rec.findings}
                 | {f.finding_id for f in rec.validation_failures})
    came_back = {str(f.get("finding_id") or "").strip()
                 for f in returned if isinstance(f, dict)}
    unaccounted = sorted(came_back - accounted)
    if unaccounted:
        return ["the record accounts for neither a finding nor a validation failure "
                f"named {', '.join(unaccounted)}, although the supervisor returned "
                "them: a dropped finding is not a decided one"]
    invented = sorted(accounted - came_back)
    if invented:
        return [*problems, f"the record names finding(s) the supervisor did not return: "
                f"{', '.join(invented)}"]
    # O228/J6: `problems` may already hold the source-mismatch report, and the previous
    # version returned a bare `[]` here -- so a record naming any file at all was
    # reported as "current, about P2" in exactly the case a reader would want to know.
    # "Reported but never followed" was half true: never followed, and never reported.
    return problems


def row_programme_supervisor() -> Row:
    """Is the P2 gate's independent review present, about P2, honest and *current*?

    The gate closes on three things and `row_programme_checklist` answers two. This
    answers the third, and there are four ways to fail it, three of which a reviewer
    demonstrated against the first version of this row:

    1. **No review.** NOT_RUN, which is honest and is not a pass: O220's review found
       fourteen findings and the first thing it established is that a missing review
       is not a satisfied condition.
    2. **A forged record.** The first version read `complete` and `blocking_findings`
       straight out of the file body. `programme_supervisor.load_record` exists to
       recompute exactly those fields and refuse a stored value that disagrees -- and
       this row imported the module containing that guard and then read the raw bytes
       anyway. A reviewer set `"complete": true` by hand: the loader refused the file
       and the board printed PASS on it. The row now asks the loader, and a record the
       product refuses is a FAIL naming the refusal.
    3. **A review of some other phase.** The row checked currency and nothing else, so
       `--phase P1 --findings empty.json` written to this path closed P2's condition
       with a review that had looked at no P2 item at all. The record carries its
       phase and its capsule digest; both are now checked, the capsule against one
       rebuilt from the live register and matrix, so the review must be about *this*
       phase and *this* state.
    4. **A stale review.** A review performed against an earlier snapshot seal or
       matrix digest is a review of a different programme; `stale_against` answers it.
    """
    record = HOH / "dogfood/p2-gate/REVIEW.json"
    command = ("python3 tools/programme_supervisor.py --phase P2 --findings PATH "
               "--decisions PATH --out dogfood/p2-gate/REVIEW.json")
    why = ("the P2 gate needs a *current, honest* independent supervisor review of "
           "*P2*: every finding other than a severity-consistent NO_ACTION holds the "
           "phase open and no decision settles one (O229/G), a record whose stored "
           "verdict disagrees with its own findings is not evidence, and the brief the "
           "review was performed on must be the one this gate derives for this board")
    if not record.is_file():
        return Row("programme_supervisor", NOT_RUN,
                   "no review record; the gate's third condition is unanswered",
                   command,
                   "a missing independent review is not a satisfied condition, "
                   "which is what O220 established on its first run")
    register_path = HOH / "program/v3_3/REQUIREMENTS.json"
    matrix_path = HOH / "program/v3_3/VERIHARNESS_CAPABILITY_MATRIX.json"
    if not register_path.is_file() or not matrix_path.is_file():
        # An unevaluable row is NOT_RUN, as this file's own header says. The first
        # version reported FAIL here, which reads as "the review is bad" when what
        # happened is that the question could not be asked.
        return Row("programme_supervisor", NOT_RUN,
                   "the register or the capability matrix is not in this tree, so "
                   "currency cannot be established", command, why)
    try:
        body = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Row("programme_supervisor", FAIL, f"unreadable: {_one_line(exc)}",
                   command, "a review nobody can read cannot be current")

    try:
        import importlib.util as _il
        # From this file's own directory, not from `HOH`: `programme_supervisor` is
        # a sibling gate in this checkout, while `HOH` is the tree being inspected.
        # Conflating them meant the row could not be exercised against a constructed
        # tree at all, which is why it had no test.
        _spec = _il.spec_from_file_location(
            "programme_supervisor",
            Path(__file__).resolve().parent / "programme_supervisor.py")
        _ps = _il.module_from_spec(_spec)
        sys.modules["programme_supervisor"] = _ps
        _spec.loader.exec_module(_ps)
    except Exception as exc:  # noqa: BLE001 - an unanswerable question is not a pass
        return Row("programme_supervisor", NOT_RUN,
                   f"the review tool could not be loaded: {_one_line(exc)}",
                   command, why)

    # The state the review claims to be about has to be built before the record can be
    # checked against it -- O226/E1: `validation_failures` is an input to `complete`
    # and is the one input a record can *carry* rather than earn, so a hand-emptied
    # array loaded clean and published PASS. `load_record` can only recompute it with a
    # snapshot, and this is where the snapshot comes from.
    try:
        register = json.loads(register_path.read_text(encoding="utf-8"))
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        snapshot, _ev, _items = _ps.reconciled(register, matrix)
    except Exception as exc:  # noqa: BLE001 - an unanswerable question is not a pass
        return Row("programme_supervisor", NOT_RUN,
                   f"the programme's state could not be derived: {_one_line(exc)}",
                   command, why)

    # The honesty half. `load_record` recomputes every member of `COMPUTED_FIELDS`,
    # refuses a stored value that disagrees, and -- with the snapshot -- puts the
    # findings back through `supervisor.validate`, so a record cannot carry a finding
    # about an item the snapshot it names does not contain.
    try:
        rec = _ps.load_record(body, snapshot=snapshot)
    except Exception as exc:  # noqa: BLE001 - a refused record is a failed condition
        return Row("programme_supervisor", FAIL,
                   f"the review tool refuses its own record: {_one_line(exc)}",
                   command, why)

    # The identity half: which phase, against which capsule, and over which findings.
    problems: list[str] = []
    causes: list[str] = []
    if rec.phase != "P2":
        problems.append(f"the record reviews phase {rec.phase}, not P2")
    problems += _findings_binding_problems(rec, _ps)

    # O229/L2 and L4. The phase is held by what the preserved reviews *accumulate*, not by
    # what the newest record says. A finding is raised by the review that returns it and
    # is resolved only by a later review that looks again and finds nothing there -- so
    # relabelling a finding in the review that raised it changes nothing, and adding a
    # directory can raise or resolve but never forget. Both were bypasses: fourteen
    # findings relabelled `NO_ACTION/low` published `0 blocking, complete` with their
    # critical reasons still on disk, and a shim holding an empty findings file erased the
    # history from every later brief.
    still_open, resolved = _ps.resolution_state(_preserved_reviews(_ps))
    if still_open:
        problems.append(
            f"{len(still_open)} finding(s) raised by a preserved review and resolved by "
            f"none: {', '.join(sorted(still_open)[:6])}"
            + (" ..." if len(still_open) > 6 else ""))
    # O229/L4, and this is a disclosure rather than a defence, said plainly because the
    # difference matters. A finding is settled by a later review that looks again and finds
    # nothing there -- and at this layer "a later review found nothing" is indistinguishable
    # from "someone wrote a file saying a later review found nothing". Every round of this
    # sequence has rediscovered that, and it is not closable by any check over files:
    # independence is a property of the dispatch.
    #
    # What *is* closable is doing it quietly. A mass resolution used to leave the board
    # reading `0 blocking, complete` with the original critical reasons still on disk in
    # the earlier review. The count is now on the face of the cell, on every outcome, so a
    # reader sees that findings were resolved rather than never raised.
    if resolved:
        causes.append(f"{len(resolved)} previously-raised finding(s) resolved by a later "
                      f"review: {', '.join(resolved[:6])}"
                      + (" ..." if len(resolved) > 6 else ""))

    # The currency half, and the second version of it. The first compared the matrix
    # *file bytes* against `REVIEW.json`'s `matrix_digest`, which
    # `programme_supervisor.matrix_digest()` computes over canonical JSON -- two
    # different functions of two different inputs, which a reviewer showed can never
    # agree for any matrix. It now asks the tool that already answers this question.
    try:
        problems += _ps.stale_against(rec, snapshot=snapshot,
                                      matrix_digest=_ps.matrix_digest(matrix))
        problems += _capsule_binding_problems(rec, _ps, register=register,
                                              matrix=matrix, snapshot=snapshot)
    except Exception as exc:  # noqa: BLE001 - an unanswerable question is not a pass
        problems.append(f"currency could not be established: {_one_line(exc)}")

    # O226/E7. The verdict was readable and the reason was not: a record every one of
    # whose findings the product refused printed "0 finding(s), 0 undecided,
    # INCOMPLETE" -- the right answer, with its cause missing -- and a finding held
    # open by a thin rejection printed as though nobody had decided it. Both now say
    # so, in the cell that C6 was about.
    if rec.validation_failures:
        causes.append(f"{len(rec.validation_failures)} finding(s) refused by validate: "
                      + ", ".join(f.finding_id for f in rec.validation_failures))
    if rec.thin_rejections:
        causes.append("rejection below the bar for a phase-level finding: "
                      + ", ".join(rec.thin_rejections))
    if rec.applied_without_evidence:
        causes.append("applied with no evidence reference: "
                      + ", ".join(rec.applied_without_evidence))
    # O228/K3: this said "undecided", which `blocking_findings` has not meant since
    # O229/G -- it counts every finding that is not a finding of nothing, decided or
    # not. A record with every finding applied and evidenced printed "1 undecided", and
    # the wrong word went into `docs/READINESS.md` and `CLAIMS.md`.
    #
    # O228/J4: the cell now names the review the gate read, on every outcome. It used to
    # name it only in failure messages, so four different directories -- including
    # `zzzz` and a draft -- produced byte-identical PASS cells.
    # O228/J5, and it is a composition gap rather than a forgery. Deleting 31 of the 32
    # P2 requirements from the register produces an *honest* whole-phase brief over one
    # item, an honest empty review of it, and -- before this -- a PASS. No hand-editing.
    # The row that would notice is `row_program_scope`, which checks the register against
    # the pinned plan and is NOT_RUN on this machine because the plan is not reachable.
    # So the gate was resting on a register nothing vouched for, and the honest thing is
    # to say so in the cell rather than to inherit the silence.
    scope = row_program_scope()
    if scope.state != PASS:
        problems.append(
            f"the register this brief was cut from is not vouched for: program_scope is "
            f"{scope.state}, so the phase could hold fewer items than the plan requires")

    reviewed = _canonical_review(HOH)[0]
    # O229/L9: this published `problems[0]` and computed four. The unvouched register and
    # the brief mismatch were dropped on the *failure* path -- the same shape as O228/J6's
    # finding about the success path, reintroduced one level up in the entry that repaired
    # J6. Every reason is published now; `_one_cell` bounds the result.
    detail = (f"review {reviewed.name if reviewed else 'none'}: {len(rec.findings)} "
              f"finding(s), {len(rec.blocking_findings)} blocking, "
              f"{'complete' if rec.complete else 'INCOMPLETE'}"
              + "".join(f"; {c}" for c in causes)
              + ("".join(f"; {p}" for p in problems) if problems
                 else "; current, about P2"))
    good = rec.complete and not rec.blocking_findings and not problems
    return Row("programme_supervisor", PASS if good else FAIL, _one_cell(detail),
               command, why)

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


def row_acceptance() -> Row:
    """Do the end-to-end acceptances in the register still hold?

    Every requirement whose probe is `evidence` (P1-16 and the P2 end-to-end
    items) is judged exactly as the capability matrix judges it: the record
    must exist, re-derive from its preserved run, *and* say the requirement is
    satisfied. Reviewer B: the first version passed on re-derivation alone, so
    a record that honestly said "not satisfied" made this row green while the
    matrix said IMPLEMENTED_NOT_PROVEN -- and it hard-coded the paths the
    register holds. Two evidence-probed entries with one id fail the row.
    """
    command = ("python3 tools/acceptance_record.py --evidence <dir> --out <record> --check, "
               "for each evidence probe in the register, via tools/capability_matrix.py")
    import importlib.util as _il
    spec = _il.spec_from_file_location("capability_matrix", HERE / "capability_matrix.py")
    cm = _il.module_from_spec(spec)
    sys.modules["capability_matrix"] = cm
    spec.loader.exec_module(cm)
    cm.HOH = HOH          # judge the same tree this board describes
    try:
        register = json.loads((HOH / "program/v3_3/REQUIREMENTS.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Row("acceptance", NOT_RUN, f"no readable register: {exc}", command,
                   "an acceptance is judged against the register")
    entries = [r for r in register.get("requirements") or []
               if (r.get("probe") or {}).get("kind") == "evidence"]
    probes = [(r["id"], r["probe"]) for r in entries]
    if not probes:
        return Row("acceptance", NOT_RUN, "no evidence-probed requirement in the register",
                   command, "nothing to judge")
    # Two entries with one id would collapse into one result below, and the one
    # that is not kept would never be judged (kbr review).
    ids = [rid for rid, _ in probes]
    repeated = sorted({rid for rid in ids if ids.count(rid) > 1})
    if repeated:
        return Row("acceptance", FAIL, "duplicate requirement id in the register: "
                   + ", ".join(repeated), command,
                   "an acceptance is judged per requirement; one id on two entries "
                   "leaves one of them unjudged")
    # Each probe is judged with the register entry that carries it, as the
    # matrix's `classify` does: without it a P2-33 line keyed `P2-35` read
    # PROVEN here while the matrix said NOT_DETERMINABLE (pkeybind review).
    results = {r["id"]: cm.evidence_status(r["probe"], requirement=r) for r in entries}
    # Blocking only for evidence that is broken: a record that no longer
    # re-derives from its run, or one that cannot be read. A requirement that
    # is honestly not proven yet -- no record, or a record that re-derives and
    # says it falls short -- is capability status, not a regression of this
    # tree, the same line the capability matrix row draws (reviewer B round 2:
    # otherwise the board flips to not-ready on a fact that has not changed).
    broken = {rid: note for rid, (st, note) in results.items()
              if st == cm.NOT_DETERMINABLE or "re-derivation FAILED" in note}
    unproven = {rid: st for rid, (st, _) in results.items() if st != cm.PROVEN}
    why = ("an end-to-end acceptance is evidence about one real run; a record that "
           "no longer matches that run is not evidence, and a requirement not yet "
           "proven is shown, not hidden")
    # A requirement the register records as proven (`proven_on`) that is no
    # longer PROVEN has regressed -- a stricter derivation or a lost record --
    # and that blocks: otherwise the drop would read as "not yet proven" and
    # show only as advisory (reviewer B, round 3).
    regressed = [rid for rid, probe in probes
                 if probe.get("proven_on") and results[rid][0] != cm.PROVEN]
    if regressed:
        return Row("acceptance", FAIL, "no longer proven: " + ", ".join(regressed),
                   command, why)
    if broken:
        return Row("acceptance", FAIL, "; ".join(f"{rid}: {n[:70]}" for rid, n in broken.items()),
                   command, why)
    if not unproven:
        return Row("acceptance", PASS, "all proven: " + ", ".join(results), command, why)
    # A missing record is only "not here" when its run is not here either --
    # the public export. Evidence present with its record gone is a broken
    # state, and blocks (reviewer B, round 3).
    orphaned = [rid for rid, probe in probes
                if results[rid][0] == cm.MISSING and probe.get("evidence_dir")
                and (HOH / probe["evidence_dir"]).is_dir()]
    if orphaned:
        return Row("acceptance", FAIL,
                   "evidence present but no record: " + ", ".join(orphaned), command, why)
    if all(st == cm.MISSING for st in unproven.values()):
        return Row("acceptance", NOT_RUN,
                   "no acceptance record in this tree for " + ", ".join(unproven), command,
                   why, advisory=True)
    return Row("acceptance", FAIL,
               "not yet proven: " + ", ".join(f"{rid} {st}" for rid, st in unproven.items()),
               command, why, advisory=True)


def row_programme_checklist() -> Row:
    """Do this programme's own phase gates come out of the product's reconciler?

    P2-28's binding, and the answer to a question this project kept asking
    itself: a component with tests is not a capability. `hoh.checklist` decides
    nothing unless something consults it, so this row runs the binding that
    makes the programme's own gates its verdict -- and an unsound checklist
    makes the row red rather than producing a verdict nobody should trust.
    """
    command = "python3 tools/programme_checklist.py"
    tool = HERE / "programme_checklist.py"
    if not tool.is_file():
        return Row("programme_checklist", NOT_RUN, "no binding in this tree", command,
                   "a reconciler nothing consults is a component, not a capability")
    rc, out = _py("tools/programme_checklist.py")
    last = (out.strip().splitlines() or [""])[0][:110]
    if rc == 3:
        return Row("programme_checklist", NOT_RUN,
                   "the register or the matrix is not in this tree", command,
                   "the programme's checklist is derived from them")
    return Row("programme_checklist", PASS if rc == 0 else FAIL, last or f"exit {rc}",
               command,
               "the gates this programme runs on are decided by hoh.checklist, so a "
               "structurally unsound checklist stops the verdict instead of colouring it")


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
                                     "token-shaped", "private-id found",
                                     "session-uuid found", "unreadable found"))]
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


def row_docs_consistency() -> Row:
    """Do the published figures still come from the tools that derive them,
    and is the changelog still generated from the release notes?

    `paper/NUMBERS_V2.md` is rendered by `tools/docs_numbers.py` from the
    repository's own tools and checked against the ledger entries its
    historical rows rely on; the changelog section of the newest release is
    generated from `RELEASE_NOTES.md` by `tools/changelog_from_release_notes.py`.
    A table that is typed rather than derived, and a second hand-kept copy of
    the release notes, are the two ways a published number drifts away from
    what produced it. The row that reads this board (V13) is left out of the
    check run here: a check the board runs must not depend on the board's own
    verdict, or the verdict could not settle.

    Without the git history or the ledger (an export) the first check cannot
    answer, and the row is `NOT_RUN`, never a pass.
    """
    command = ("python3 tools/docs_numbers.py check --skip V13 && "
               "python3 tools/changelog_from_release_notes.py check")
    rc1, out1 = _py("tools/docs_numbers.py", "check", "--skip", "V13",
                    timeout=900)
    rc2, out2 = _py("tools/changelog_from_release_notes.py", "check")
    if rc1 == 3:
        return Row("docs_consistency", NOT_RUN,
                   "needs the git history and the ledger", command,
                   "the figure table is derived from the history and checked "
                   "against ledger entries an export does not carry")
    if rc1 == 0 and rc2 == 0:
        return Row("docs_consistency", PASS,
                   "figures re-derived; changelog generated", command)
    worst = (out1 if rc1 != 0 else out2).splitlines() or ["?"]
    return Row("docs_consistency", FAIL, worst[0][:70], command)


#: Published files that every boundary closure rewrites, and that therefore
#: cannot be part of what a CI result is asked to bind.
#:
#: The gate writes its own report, and the report is published: the board and
#: the claims it renders are INCLUDE files that every run of this gate
#: rewrites, so counting them would make the comparison red forever for a
#: reason that has nothing to do with the software.
#:
#: One more was added on 2026-09-23, with the same argument the attribution
#: checker makes for ledger-only commits: `dogfood/ATTRIBUTION.json` is
#: published, and it is written in a *later* commit than the commit it
#: attributes, because a commit cannot name its own sha. Counted as product
#: bytes, a boundary therefore has **no reachable green state**: attribute the
#: export and the export is stale; export again and the new ledger entry is
#: stale. Measured, not feared -- four closure cycles of the P1 boundary, each
#: ending with this row red and `dogfood/ATTRIBUTION.json` as the only path
#: differing. A gate that cannot be satisfied does not make anyone careful; it
#: makes the verdict something to work around.
#:
#: Why the loop is provable rather than merely observed, in the three rules
#: that meet: `succession` is green only when HEAD is the capsule's commit or
#: the commits after it changed nothing but the capsule, so a closure's last
#: commit must be capsule-only; `attribution` tolerates an unattributed commit
#: only as the tip or when it touched exactly one bookkeeping file, so a
#: commit carrying content, the board, the sync state or the CI record needs an
#: entry in a *later* commit; and that entry writes this INCLUDE file. The
#: capsule-only commit spends the tip tolerance, so the ledger write always
#: lands after the export.
#:
#: What is given up, plainly: for these four files the released bytes are no
#: longer the bytes external CI executed against. That costs no coverage the
#: CI run provided -- in the exported repository the ledger's own anchor does
#: not exist, so `attribution` returns an environment gap there and its test
#: skips; what validates the ledger is the internal row, which runs after the
#: write. Each of the four has such a row: `claims` re-checks every anchor and
#: re-renders `CLAIMS.md`, `attribution` checks the ledger against git's own
#: history, and the board is regenerated by this tool. What is removed is a
#: coupling between bookkeeping and a CI result, not a check. Every source,
#: test, tool, document and example byte stays bound, which is what this row
#: exists for.
#:
#: The succession capsule is **not** listed: `EXPORT_MANIFEST.json` classifies
#: it EXCLUDE, so it never enters the comparison at all. A first version of
#: this set listed it, and a reviewer measured that the entry was inert and
#: the justification false -- the `succession` row goes red for its own
#: reason, and converges by the capsule being written after the final board
#: render, not by anything here.
#:
#: Not to be confused with `REPORTS` in `tools/export_sync.py`, which is
#: deliberately empty and answers a different question (whose change would be
#: overwritten).
EXPORT_REPORTS = frozenset({
    "docs/READINESS.md", "CLAIMS.md", "CLAIMS.json", "dogfood/ATTRIBUTION.json",
})


def stale_export_paths(then: dict[str, str], moment: dict[str, str],
                       reports: frozenset[str] = EXPORT_REPORTS
                       ) -> tuple[list[str], list[str]]:
    """(everything that differs, the part that makes the evidence stale).

    Separate from the row so the tolerance can be tested against data rather
    than read out of the row's source: an exclusion set is exactly the kind of
    thing that grows quietly.
    """
    differing = sorted(set(then) ^ set(moment)) + sorted(
        p for p in set(then) & set(moment) if then[p] != moment[p])
    # Tolerated by path, but only for a *changed* byte -- a tolerated file that
    # is in one set and not the other has left or entered the export, which is
    # a classification change and not this gate rewriting its own report. A
    # reviewer found that case tolerated: flipping the board or the ledger to
    # EXCLUDE would have dropped it from the comparison silently.
    both = set(then) & set(moment)
    return differing, [p for p in differing if p not in reports or p not in both]


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
    differing_, real = stale_export_paths(then, moment)
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
           f"{', '.join(sorted(set(differing_) & EXPORT_REPORTS))}"
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
        row_preserve(),
        row_self_hosted_topology(),
        row_unit_drift(),
        row_durability(),
        row_preflight(),
        row_parallelism(),
        row_program_scope(),
        row_capability_matrix(),
        row_build_plan(),
        row_acceptance(),
        row_programme_checklist(),
        row_programme_supervisor(),
        row_baseline_docs(),
        row_install(quick),
        row_attribution(),
        row_evidence_index(),
        row_audit(),
        row_docs_consistency(),
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
