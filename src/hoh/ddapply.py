"""The application of a verified correction (V3.3 P3-42, slice 3).

The double-down (`hoh.doubledown`) ends a block in a correction PROPOSAL and `hoh.ddverify`
measures that a criterion of its repair spec is red on the candidate (RED_REPRODUCED). Neither says
that the repair works, or that the red is the block's. This module closes both gaps and, only then,
lets the blocked run continue:

1. **The repair is proven, on a scratch copy.** A `repair_spec` proposal may carry a `fix`, a unified
   diff. It is applied to a scratch copy of the run's candidate (`workspace.materialize`; a diff that
   deletes, renames, links, is binary or leaves the copy is refused) and the proposal's criteria are
   measured again through `runner.run_check`, in the regime of the first measurement. PROVEN needs:
   the block's own failing check red before and green after; every keep-green criterion (id starting
   with K) green before and after; every other criterion green after.
2. **The red is the block's.** A criterion is the block's own failing check only when its id AND its
   command are those of a failed check in the latest iteration of the run's own receipts. The
   verdict reads the run's receipts, never the analyst's word. No such criterion: NOT_APPLIED.
3. **A person applies it; the loop never does.** In this slice NO path applies a proven fix without a
   person: every verified fix is WITHHELD (`apply-withheld.json`, once) and is applied only by
   `hoh double-down apply HALT --run RUN --approved-by NAME --reason TEXT`, which proves it and
   records the approval (the run's routing log, `apply.json`). The run's contract is not asked a
   `scope` decision: that kind keeps its meaning (the requirements changed), and a later slice (DD4)
   lets only an UNATTENDED_LONG_HORIZON contract apply a proven correction itself, through a new decision kind
   `proven_correction`.
4. **It is applied as a new iteration, through the amendment path.** The run's specification is
   amended (`amendment.park_and_amend`, kind WIDEN, naming the block's failing checks as the
   criteria to measure again): the repair is *appended*, inside a fenced block that no criteria
   parser reads, so the specification's own criteria, every requirement above and the measured
   verdict stay as written; the superseded text is parked beside it. Then the block is lifted
   (`stages.unblock`) and the run continues at its stage through the ordinary `hoh run`, which plans
   the next iteration against the amended text. **The candidate, its worktree and the run's
   receipts and results are never written.** A fix is measured, not applied: the developer of the
   new iteration realises it and QA measures that, as for any iteration.
5. **It is reversible.** `revert` puts the parked text back through a second amendment (kind
   NARROW, by a named person) and records it; the block's unblock is not undone.

**Termination.** An attempt to prove a fix is recorded (`apply-attempt-N.json`) before anything is
measured, at most `MAX_APPLY_ATTEMPTS`; a deterministic answer is terminal at once; the third
attempt does not exist (`attempts_spent`). The terminal record, `apply.json`, is APPLIED or
NOT_APPLIED with a reason, create-only and quiet: a later call reads it and writes nothing, and a
dossier is applied once, whatever the run does afterwards. The double-down applies at most
`MAX_APPLIES_PER_RUN` corrections to a run in its life (counted from the run's own amendment chain).

**The firewall** is the double-down's: nothing here changes a measured verdict, a receipt, a gate, a
criterion that was declared, or the candidate. The run's spec text gains a section and its block
is lifted; that is the feature. A capability violation is never lifted; neither is a block whose
budget is spent (an exhausted bound is a person's decision) or a run another driver holds.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from . import ddverify
from . import doubledown as dd
from .contracts import Candidate, Condition, Outcome, Receipt
from .sandbox import Isolation
from .spec_criteria import CriteriaBlockError, PinsBlockError, declared_criteria, declared_pins
from .store import StoreError

#: A criterion whose id starts with this is a keep-green row: green before the fix and after it.
KEEP_PREFIX = "K"
#: Files a fix may touch, and the seconds `git apply` may take on the scratch copy.
MAX_FIX_FILES = 16
FIX_TIMEOUT = 60
#: The receipts of one run that are read to find the failed checks (a run with more is not bound).
MAX_RECEIPT_FILES = 2000
MAX_RECEIPT_BYTES = 1 << 20
#: The run id of the proof's receipts, and the amendment and actor of an application.
RUN_ID_PREFIX = "dda-"
AMENDMENT_PREFIX = "dd-"
ACTOR = "hoh double-down"

_PATH_LINE = re.compile(r"^(?P<mark>---|\+\+\+) (?P<path>[^\t]+?)(?:\t.*)?$")
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_REFUSED_LINES = ("GIT binary patch", "Binary files ", "deleted file mode", "rename ", "copy ",
                  "similarity index", "dissimilarity index", "old mode", "new mode")
_FILE_MODES = ("100644", "100755")


class FixRefused(ValueError):
    """The fix is not a diff the product applies on a scratch copy: the reason is the message."""


# --- the fix --------------------------------------------------------------------------------- #

def _fix_path(raw: str, side: str, prefix: str) -> str | None:
    """The relative path a `---`/`+++`/`diff --git` name stands for (None for /dev/null). `git apply`
    strips exactly one leading component of ANY name (`-p1`), so a name is read the way git reads
    it and only `a/` (old side) or `b/` (new side) is accepted: `x/tests/t.py` would write
    `tests/t.py` while looking like another file. FixRefused for a name without the prefix and for any
    path that is not a plain relative one: absolute, with `..`, `.git` or an empty step."""
    raw = raw.strip()
    if raw == "/dev/null":
        return None
    if not raw.startswith(prefix):
        raise FixRefused(f"the {side} name {raw[:80]!r} does not start with {prefix!r}: git strips one "
                         f"leading component of any name, so only that form is read")
    raw = raw[2:]
    parts = raw.split("/")
    if not raw or raw.startswith("/") or any(p in ("", ".", "..") or p.lower() == ".git" for p in parts) \
            or len(raw) > 255 or not raw.isprintable() or "\\" in raw:
        raise FixRefused(f"the {side} path {raw[:80]!r} is not a plain relative path")
    return raw


def fix_paths(fix: str) -> list[str]:
    """The files a fix writes, after refusing everything a scratch copy should not be asked to
    do: a deletion, rename, copy, mode change, link, submodule or binary patch, a NUL, a path
    outside the copy, a hunk that does not add up, more than `MAX_FIX_FILES` files, or no file at
    all. Hunks are read by their line counts, so a removed line that reads `-- x` is not a header."""
    if not isinstance(fix, str) or not fix.strip() or "\0" in fix:
        raise FixRefused("the fix is empty or holds a NUL")
    try:
        fix.encode("utf-8")
    except UnicodeEncodeError as exc:                # a lone surrogate is no text a command gets
        raise FixRefused(f"the fix is not valid UTF-8 text: {exc.reason}") from exc
    targets: list[str] = []
    old_left = new_left = 0
    old_name: str | None = None
    block = ""                                       # the file of the `diff --git` header still waiting for its ---/+++
    for line in fix.split("\n"):
        if old_left or new_left:
            tag = line[:1]
            if tag in (" ", ""):
                old_left, new_left = old_left - 1, new_left - 1
            elif tag == "-":
                old_left -= 1
            elif tag == "+":
                new_left -= 1
            elif tag != "\\":
                raise FixRefused("a hunk line must start with a space, +, - or a backslash")
            if old_left < 0 or new_left < 0:
                raise FixRefused("a hunk has more lines than its header says")
            continue
        hunk = _HUNK.match(line)
        if hunk:
            old_left = int(hunk.group(1) if hunk.group(1) is not None else 1)
            new_left = int(hunk.group(2) if hunk.group(2) is not None else 1)
            continue
        if line.startswith(_REFUSED_LINES):
            raise FixRefused(f"the fix holds {line[:20]!r}: only modified and new text files are applied")
        if line.startswith("new file mode") and line.split()[-1] not in _FILE_MODES:
            raise FixRefused("the fix creates something that is not a plain file (a link or a submodule)")
        if line.startswith("diff --git "):
            if block:
                raise FixRefused(f"a `diff --git` block for {block} has no ---/+++ lines: git would write a file "
                                 f"this reading never lists")
            names = line[len("diff --git "):].split(" ")
            if len(names) != 2:
                raise FixRefused("a diff --git line must name one path twice")
            old, new = _fix_path(names[0], "diff --git", "a/"), _fix_path(names[1], "diff --git", "b/")
            if old != new or new is None:
                raise FixRefused("a diff --git line must name one path twice")
            block = new
            continue
        m = _PATH_LINE.match(line)
        if m is None:
            continue
        mark = m.group("mark")
        path = _fix_path(m.group("path"), mark, "a/" if mark == "---" else "b/")
        if mark == "---":
            old_name = path
            continue
        if path is None:
            raise FixRefused("the fix deletes a file (+++ /dev/null)")
        if old_name is not None and old_name != path:
            raise FixRefused("a file is renamed (--- and +++ name different paths)")
        if block and block != path:
            raise FixRefused("the `diff --git` header and the ---/+++ lines name different files")
        block = ""
        targets.append(path)
    if block:
        raise FixRefused(f"a `diff --git` block for {block} has no ---/+++ lines: git would write a file "
                         f"this reading never lists")
    if old_left or new_left:
        raise FixRefused("a hunk has fewer lines than its header says")
    if not targets:
        raise FixRefused("the fix names no file")
    unique = sorted(set(targets))
    if len(unique) > MAX_FIX_FILES:
        raise FixRefused(f"the fix touches {len(unique)} files; at most {MAX_FIX_FILES} are applied")
    return unique


def git_written(numstat_z: bytes) -> list[str]:
    """The files `git apply --numstat -z` says a fix writes (`added TAB deleted TAB path NUL`),
    sorted; an unreadable record is a path no reading can match."""
    out: set[str] = set()
    for rec in numstat_z.decode("utf-8", "replace").split("\0"):
        if not rec:
            continue
        parts = rec.split("\t", 2)
        out.add(parts[2] if len(parts) == 3 else "?" + rec)
    return sorted(out)


def apply_fix(arena: Path, fix: str, home: Path) -> None:
    """Apply `fix` to the scratch copy `arena` with `git apply` (it needs no repository). FixRefused
    when the diff is refused, writes through a link of the copy, writes files other than the ones
    `fix_paths` listed (git's own `--numstat -z` is asked first), or does not apply; an OSError or a
    timeout of git itself propagates."""
    paths = fix_paths(fix)
    for rel in paths:
        here = Path(arena)
        for step in rel.split("/"):
            here = here / step
            if here.is_symlink():
                raise FixRefused(f"{rel} runs through a link of the copy")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LC_ALL": "C",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    # git that cannot run (missing, a timeout) is no verdict on the fix: the error propagates and the
    # attempt measures nothing; only git's own "no" refuses the fix
    listed = subprocess.run(["git", "apply", "--numstat", "-z", "-"], cwd=arena,
                            input=fix.encode("utf-8"), capture_output=True, timeout=FIX_TIMEOUT,
                            env=env, check=False)
    if listed.returncode != 0 or git_written(listed.stdout) != paths:
        # the set the pins were compared against must be the set git writes; when git cannot say, or
        # says something else, nothing is written
        raise FixRefused("the files git would write are not the files this reading found: "
                         + dd._safe(repr(git_written(listed.stdout)) if listed.returncode == 0
                                    else listed.stderr.decode("utf-8", "replace").strip(), 200))
    done = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=arena,
                          input=fix.encode("utf-8"), capture_output=True, timeout=FIX_TIMEOUT,
                          env=env, check=False)
    if done.returncode != 0:
        raise FixRefused("the fix does not apply to the candidate: "
                         + dd._safe(done.stderr.decode("utf-8", "replace").strip(), 200))


def _read_spec(path: str) -> str:
    """The run's specification text: a link or a FIFO is refused, and text that is not UTF-8 raises."""
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise FixRefused("the run's specification is not a file of its own (a link, a FIFO)")
    return Path(path).read_text(encoding="utf-8")


# --- the block's own failing checks ------------------------------------------------------------ #

def failing_checks(run_dir: Path, run_id: str) -> dict[str, str]:
    """`{check id: command}` of the checks that failed in the latest iteration the run's own
    receipts hold (`receipts/<run>-i<n>-a<m>-<check>.json`; a `-basis` receipt measures the
    predecessor, not the candidate). The newest attempt of an iteration speaks for its check; an
    infrastructure error is no failure; a receipt that cannot be read or is another run's is
    ignored. A criterion expects exit 0 (`spec_criteria.fidelity_findings`)."""
    directory = Path(run_dir) / "receipts"
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return {}
    found: dict[int, dict[str, tuple[int, Receipt]]] = {}
    for name in names[:MAX_RECEIPT_FILES]:
        if not name.endswith(".json") or name.endswith("-basis.json"):
            continue
        try:
            full = directory / name
            st = os.lstat(full)                       # a link, a FIFO or a directory is no receipt
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_RECEIPT_BYTES:
                continue
            receipt = Receipt.model_validate_json(full.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if receipt.run_id != run_id:
            continue
        best = found.setdefault(receipt.iteration, {})
        held = best.get(receipt.check_id)
        if held is None or receipt.attempt > held[0]:
            best[receipt.check_id] = (receipt.attempt, receipt)
    if not found:
        return {}
    latest = found[max(found)]
    return {cid: rec.command for cid, (_, rec) in latest.items() if rec.outcome(0) is Outcome.FAIL}


def bound_ids(criteria, failing: dict[str, str]) -> list[str]:
    """The criteria that are the block's own failing check: same id, same command, not keep-green."""
    return [c.criterion_id for c in criteria
            if not c.criterion_id.startswith(KEEP_PREFIX) and failing.get(c.criterion_id) == c.command]


# --- the proof ------------------------------------------------------------------------------- #

def decide_proof(before: list[dict], after: list[dict], bound: list[str],
                 declared: int) -> tuple[str, str, str]:
    """`(status, reason, why)` of one proof: `before` is the verification's own measurement of the
    unrepaired candidate, `after` the measurement with the fix applied. A measurement that did not
    come about decides first (no verdict); then the table, first match wins."""
    if any(r["outcome"] == "REFUSED" for r in after):
        return "NOT_PROVEN", "criterion_refused", "the runner's guards refuse a criterion"
    if len(after) < declared or any(r["outcome"] == "INCONCLUSIVE" for r in after):
        return "INCONCLUSIVE", "not_measured", "a criterion could not be measured with the fix"
    was = {r["id"]: r for r in before}
    now = {r["id"]: r for r in after}
    for cid in bound:
        row = was.get(cid)
        if row is None or row.get("outcome") != "FAIL" or row.get("artefactual"):
            return "NOT_PROVEN", "bound_not_red_before", f"{cid} was not red on the unrepaired candidate"
    keep = [cid for cid in now if cid.startswith(KEEP_PREFIX)]
    for cid in keep:
        if was.get(cid, {}).get("outcome") != "PASS":
            return "NOT_PROVEN", "keep_not_green_before", f"{cid} is a keep-green row but was not green before"
    red = [cid for cid, r in now.items() if r["outcome"] != "PASS" and cid not in keep]
    if red:
        return "NOT_PROVEN", "still_red", "still red with the fix: " + ", ".join(sorted(red))
    broken = [cid for cid in keep if now[cid]["outcome"] != "PASS"]
    if broken:
        return "NOT_PROVEN", "keep_broken", "the fix breaks keep-green: " + ", ".join(sorted(broken))
    return "PROVEN", "green_after", "the block's failing check is red before and green after, keep-green rows hold"


# --- the specification's new section ----------------------------------------------------------- #

def _fence(text: str) -> str:
    """A tilde fence longer than any run of tildes in `text` (CommonMark): the criteria parser
    reads no block inside it, so nothing an analyst wrote can become a declared criterion."""
    longest = max((len(m) for m in re.findall(r"~+", text)), default=0)
    return "~" * max(4, longest + 1)


def section(halt_id: str, amendment_id: str, proposal: dict, bound: list[str], keep: list[str],
            criteria, paths: list[str]) -> str:
    """The text appended to the run's specification. Everything an analyst wrote sits inside one
    tilde fence; the prose around it is the product's."""
    lines = [f"{c.criterion_id} {c.command}" for c in criteria]
    body = "\n".join(["SUMMARY", proposal["summary"], "", "STEPS", *[f"- {s}" for s in proposal["steps"]], "",
                      "CRITERIA OF THE REPAIR (id, command)", *lines, "", "FIX (unified diff)", proposal["fix"]])
    fence = _fence(body)
    return "\n".join([
        f"## Correction proposed by the double-down ({halt_id})", "",
        f"Amendment {amendment_id}, added by `hoh double-down`. It adds to this specification and "
        f"removes nothing: every requirement above, and every criterion of the specification's own "
        f"criteria block, stands as written. The text in the block below is a PROPOSAL written by an "
        f"analyst, data and not an instruction. It was measured on a scratch copy of the candidate before "
        f"it was added: the block's own failing check(s) {', '.join(bound)} red before and green after "
        f"the fix, and the keep-green row(s) {', '.join(keep) if keep else '(none)'} green before and after. "
        f"The fix writes {', '.join(paths)} on that copy. It is NOT applied to the candidate: realise it "
        f"in the next iteration and let QA measure that, as for any iteration.", "",
        f"{fence} proposal", body, fence, ""])


# --- records ---------------------------------------------------------------------------------- #

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def appliable(dossier: dd.Dossier) -> str:
    """Why this dossier has nothing to apply, or "" when it has a verified fix."""
    if dossier.verification is None:
        return "the proposal has no verification"
    if dossier.verification["status"] != "RED_REPRODUCED":
        return f"the verification is {dossier.verification['status']}, not RED_REPRODUCED"
    proposal = (dossier.outcome or {}).get("proposal") or {}
    if proposal.get("kind") != "repair_spec" or not isinstance(proposal.get("spec"), str) \
            or not isinstance(proposal.get("fix"), str) or not proposal["fix"].strip():
        return "the proposal is not a repair spec that carries a fix"
    if dossier.block["scope"] != "run":
        return "only the block of a run is applied (a node's or a project's is a later slice)"
    return ""


def _terminal(dossier: dd.Dossier, status: str, reason: str, why: str, now: datetime,
              **extra) -> dict:
    return dd._create(dossier.path / "apply.json", {
        "schema": dd.SCHEMA_APPLICATION, "status": status, "reason": reason, "why": why[:500],
        "halt_id": dossier.block["halt_id"], "block_digest": dossier.block["digest"],
        "outcome_digest": dossier.outcome["digest"], "verification_digest": dossier.verification["digest"],
        "attempts": len(dossier.apply_attempts), "applied_at": dd._stamp(now), **extra})


def _decided(row: dict, dossier: dd.Dossier, status: str, reason: str, why: str, now: datetime,
             **extra) -> dict:
    """Write the terminal record; a record that cannot be written is said, not hidden."""
    try:
        record = _terminal(dossier, status, reason, why, now, **extra)
    except (dd.DossierError, OSError) as exc:
        return {**row, "action": "unrecorded", "status": status, "reason": reason,
                "why": dd._safe(f"{type(exc).__name__}: {exc}")}
    return {**row, "action": status.lower(), "status": record["status"], "reason": reason,
            **{k: extra[k] for k in ("amendment_id", "next_step", "approved_by") if extra.get(k)}}


# --- apply ------------------------------------------------------------------------------------ #

def apply(root: Path | str, owner_dir: Path, halt_id: str, *, session: ddverify.Workspaces,
          now: datetime | None = None,
          materialize: Callable[[Candidate, Path], Path] | None = None,
          check_runner: Callable[..., tuple] | None = None,
          fix_applier: Callable[[Path, str, Path], None] | None = None,
          approved_by: str | None = None, approval_reason: str | None = None) -> dict:
    """Prove the fix of the dossier `halt_id` of `owner_dir` on a scratch copy and, if it is
    proven and a person approved it, apply it as a new iteration of the blocked run. Without an
    approval nothing is measured and nothing is applied: the fix is WITHHELD.

    Never raises for a refusal: the row says what happened -- `applied`, `not_applied` (terminal,
    with a reason), `inconclusive` (a proof that measured nothing, one attempt left), `closed`
    (already decided: nothing is written), `refused` (nothing to apply, a transient obstacle, or
    nowhere to measure: nothing is decided) or `unrecorded` (decided, the record could not be
    written). `executed` is True when an attempt was recorded. The dossier's flock is held
    throughout, and the run's own lock while the specification and the state are written.

    `approved_by` / `approval_reason` are the person's approval, one printable line each: the proof is
    still done, and the approval is recorded in the run's routing log (`governance.record_override`,
    as `hoh unblock` records an override) and in `apply.json`. Without them every proven fix is
    WITHHELD (`apply-withheld.json`, once), whatever the run's contract says."""
    from . import workspace
    from .runner import run_check

    now = now or datetime.now(UTC)
    materialize = materialize or workspace.materialize
    check_runner = check_runner or run_check
    fix_applier = fix_applier or apply_fix
    row: dict = {"scope": "dossier", "target": dd._safe(halt_id), "halt_id": dd._safe(halt_id)}
    try:
        path = dd.dossier_path(owner_dir, halt_id)
        with dd._locked(path):
            dossier = dd.read_dossier(path)
            if dossier.application is not None:
                a = dossier.application
                return {**row, "action": "closed", "status": a["status"], "reason": a["reason"]}
            why = appliable(dossier)
            if why:
                raise dd.DossierError(why)
            if approved_by is not None or approval_reason is not None:
                approved_by = dd._person_text(approved_by, "approved_by")
                approval_reason = dd._person_text(approval_reason, "reason")
            return _apply_locked(Path(root).expanduser(), Path(owner_dir), dossier, row, now,
                                 session, materialize, check_runner, fix_applier,
                                 approved_by, approval_reason)
    except ddverify.VerifyRefused as exc:
        return {**row, "action": "refused", "why": dd._safe(exc)}
    except (dd.DossierError, OSError, ValueError, StoreError) as exc:
        return {**row, "action": "refused", "why": dd._safe(f"{type(exc).__name__}: {exc}")}


def _decision_text(halt_id: str, proposal: dict) -> str:
    return f"apply the verified correction {halt_id} to the blocked run: {proposal['summary'][:200]}"


def _approval_routing(halt_id: str, proposal: dict):
    """The routing a person's approval goes past, built by hand: `autonomy.route` is NOT asked (a
    scope decision keeps its meaning, and no contract lets the loop apply a proven fix in this slice)."""
    from .autonomy import Handling, Routing

    return Routing(decision=_decision_text(halt_id, proposal), kind="decision", handling=Handling.ASK_NOW,
                   delegable=False, why="a proven correction is applied by a person only (P3-DD3)")


WITHHELD_WHY = ("a proven correction is applied by a person only, never by the loop: hoh double-down apply "
                "HALT --run RUN --approved-by NAME --reason TEXT (a later slice lets an "
                "UNATTENDED_LONG_HORIZON contract apply it itself, through the decision kind proven_correction)")


def _withhold(row: dict, dossier: dd.Dossier, now: datetime) -> dict:
    """Say once, in `apply-withheld.json`, that a verified fix waits for a person; later calls
    find it and write nothing. The run's contract is not consulted."""
    why = WITHHELD_WHY
    out = {**row, "action": "withheld", "reason": "awaits_a_person", "why": why[:300],
           "next_step": f"hoh double-down apply {dossier.block['halt_id']} --run {dossier.block['object_id']} "
                        f"--approved-by NAME --reason TEXT"}
    if dossier.withheld is None:
        try:
            dd._create(dossier.path / "apply-withheld.json", {
                "schema": dd.SCHEMA_WITHHELD, "block_digest": dossier.block["digest"],
                "verification_digest": dossier.verification["digest"], "reason": why[:500],
                "withheld_at": dd._stamp(now)})
        except (dd.DossierError, OSError) as exc:
            out["record"] = "NOT written: " + dd._safe(f"{type(exc).__name__}: {exc}")
    return out


def _apply_locked(root: Path, owner_dir: Path, dossier: dd.Dossier, row: dict, now: datetime,
                  session, materialize, check_runner, fix_applier, approved_by: str | None = None,
                  approval_reason: str | None = None) -> dict:
    from . import governance, rundrive
    from .store import RunStore

    block, proposal = dossier.block, dossier.outcome["proposal"]
    halt_id, run_id = block["halt_id"], block["object_id"]
    store = RunStore(root, run_id)
    if store.dir != owner_dir:
        raise dd.DossierError("the dossier does not belong to the run it names")
    state = dd.read_run_state(store.dir)
    if state is None or state.condition is not Condition.BLOCKED \
            or halt_id not in {h.halt_id for h in dd.halts_of_run(state)}:
        # (a run held by a capability violation is another halt: its kind is part of the identity,
        # so a hold is never lifted here -- only a person's `hoh unblock` lifts it)
        raise dd.DossierError("the run is not in the block this dossier answers")

    def no(reason: str, why: str, **extra) -> dict:
        return _decided(row, dossier, "NOT_APPLIED", reason, why, now, **extra)

    # 1. what no measurement can change: a decision at once, no attempt spent
    if state.budget_exhausted():
        return no("bound_exhausted", f"the run cannot start another iteration: {state.budget_exhausted()}")
    chain = store.read_amendments(origin_digest=state.spec_digest)
    if sum(1 for a in chain.amendments if a.actor == ACTOR) >= dd.MAX_APPLIES_PER_RUN:
        return no("run_allowance_spent", f"the double-down has applied {dd.MAX_APPLIES_PER_RUN} corrections to this run")
    try:
        for text in (proposal["summary"], *proposal["steps"], proposal["spec"]):
            text.encode("utf-8")                      # what is written into the specification must encode
        paths = fix_paths(proposal["fix"])
        criteria = declared_criteria(proposal["spec"])
        if not criteria or len(criteria) > ddverify.MAX_CRITERIA:
            raise CriteriaBlockError("the spec declares no criteria or too many")
        pinned = {pin.path.casefold() for pin in declared_pins(_read_spec(state.spec_path))}
    except (FixRefused, CriteriaBlockError, PinsBlockError, UnicodeEncodeError) as exc:
        return no("fix_refused", dd._safe(exc, 300))
    touched = sorted(p for p in paths if p.casefold() in pinned)
    if touched:                                   # a fix may not rewrite the tests the run pinned (FID1)
        return no("fix_touches_pins", "the fix writes " + ", ".join(touched[:5]) + ", which the run's "
                  "specification pins: a repair that edits the tests that judge it proves nothing")
    bound = bound_ids(criteria, failing_checks(store.dir, run_id))
    if not bound:
        return no("not_bound", "no criterion of the repair spec is a check that failed in the run's latest iteration "
                               "(same id, same command): nothing ties the red to the block")
    if approved_by is None:                       # no path applies a proven fix without a person
        return _withhold(row, dossier, now)
    try:                                          # the contract is read only to record the approval on its log
        governor = governance.bind(store.dir, write=False)
    except governance.ContractRefused as exc:
        return no("contract_unreadable", dd._safe(exc, 300))
    try:
        candidate, source = ddverify._candidate_of(root, block)
    except dd.DossierError as exc:
        return no("no_candidate", dd._safe(exc, 300))
    if candidate.binding() != (dossier.verification.get("candidate") or {}).get("binding"):
        return no("candidate_moved", "the candidate is not the one the proposal was verified on")
    # 2. transient obstacles: said, nothing decided
    held = rundrive.refusal_for_resume(root, run_id)
    if held is not None:
        raise dd.DossierError(held)
    attempt = len(dossier.apply_attempts) + 1
    if attempt > dd.MAX_APPLY_ATTEMPTS:
        return no("attempts_spent", f"{dd.MAX_APPLY_ATTEMPTS} attempts to prove the fix measured nothing")
    isolation = Isolation(dossier.verification["isolation"])
    spec_sha, fix_sha = _sha(proposal["spec"]), _sha(proposal["fix"])
    # 3. the attempt exists before anything is measured: a crash inside it counts
    with session(block) as make:
        with make("verification", f"dda-{halt_id}", ddverify.ARENA_ESTIMATE) as workspace_path:
            dd._create(dossier.path / f"apply-attempt-{attempt}.json", {
                "schema": dd.SCHEMA_APPLY_ATTEMPT, "n": attempt, "block_digest": block["digest"],
                "verification_digest": dossier.verification["digest"], "spec_sha256": spec_sha,
                "fix_sha256": fix_sha, "started_at": dd._stamp(now)})
            dossier = dd.read_dossier(dossier.path)
            row["executed"] = True
            try:
                arena = Path(workspace_path) / "arena"
                materialize(candidate, arena)
                fix_applier(arena, proposal["fix"], Path(workspace_path))
                after = ddverify._measure(criteria, candidate, arena, halt_id, attempt, make,
                                          check_runner, isolation, prefix=RUN_ID_PREFIX)
            except FixRefused as exc:
                return no("fix_does_not_apply", dd._safe(exc, 300), **{"fix_sha256": fix_sha})
            except Exception as exc:                      # noqa: BLE001 - no measurement came about
                after = [{"id": "-", "command": "-", "outcome": "INCONCLUSIVE",
                          "why": dd._safe(f"{type(exc).__name__}: {exc}", 500)}]
    status, reason, why = decide_proof(dossier.verification["criteria"], after, bound, len(criteria))
    if status == "INCONCLUSIVE":
        said = [str(r.get("why") or r["outcome"]) for r in after if r["outcome"] == "INCONCLUSIVE"]
        why = "; ".join(said) or why
        if attempt < dd.MAX_APPLY_ATTEMPTS:
            return {**row, "action": "inconclusive", "status": status, "reason": reason,
                    "attempt": attempt, "attempts_left": dd.MAX_APPLY_ATTEMPTS - attempt}
        return no("attempts_spent", f"{dd.MAX_APPLY_ATTEMPTS} attempts to prove the fix measured nothing; the last: {why}")
    proof = {"criteria_after": after, "bound": bound, "fix_sha256": fix_sha, "spec_sha256": spec_sha,
             "isolation": isolation.value, "paths": paths}
    if status != "PROVEN":
        return no(reason, why, **proof)
    # 4. the person's approval is on the record, on the run's own routing log (as `hoh unblock` records one)
    try:
        if governor is not None:
            governance.record_override(governor, routing=_approval_routing(halt_id, proposal),
                                       by=approved_by, what=approval_reason)
    except (OSError, ValueError) as exc:
        raise dd.DossierError(f"the approval could not be recorded: {type(exc).__name__}: {exc}") from exc
    # 5. the change: the specification, its record, the block
    keep = [c.criterion_id for c in criteria if c.criterion_id.startswith(KEEP_PREFIX)]
    amendment_id = AMENDMENT_PREFIX + halt_id
    moved = _amend_and_unblock(store, halt_id, amendment_id, proposal, criteria, bound, keep, paths,
                               dossier, now)
    if moved is not None:
        return no(moved[0], moved[1], **proof)
    return _decided(row, dossier, "APPLIED", reason, why, now, amendment_id=amendment_id,
                    approved_by=approved_by, approval_reason=approval_reason, **proof,
                    next_step=f"hoh --root {root} run {run_id}")


def _amend_and_unblock(store, halt_id: str, amendment_id: str, proposal: dict, criteria,
                       bound: list[str], keep: list[str], paths: list[str], dossier: dd.Dossier,
                       now: datetime) -> tuple[str, str] | None:
    """Amend the run's specification and lift its block under the run's own lock, or say why not:
    `(reason, why)` when the run moved since the proof (nothing is written then)."""
    from . import rundrive, stages
    from .amendment import AmendmentKind, AmendmentLedger, park_and_amend, text_digest
    from .store import LockBusy, StoreError

    try:
        with store.lock():
            # the lease was asked before a proof that may take most of an hour: asked again, under the lock
            held = rundrive.refusal_for_resume(store.root, store.run_id)
            if held is not None:
                raise dd.DossierError(held)
            state = store.read_state()
            if state.condition is not Condition.BLOCKED \
                    or halt_id not in {h.halt_id for h in dd.halts_of_run(state)}:
                return "run_moved", "the run is no longer in the block this dossier answers"
            if stages.violation_unresolved(state) or state.budget_exhausted():
                return "run_moved", "the run was held or its budget was spent while the fix was proven"
            try:
                old_text = _read_spec(state.spec_path)
            except FixRefused as exc:
                return "spec_changed", str(exc)
            chain = store.read_amendments(origin_digest=state.spec_digest)
            if text_digest(old_text) != state.spec_digest or chain.current_digest() != state.spec_digest:
                return "spec_changed", "the run's specification is not the text its state and amendment chain name"
            if not chain.amendments and chain.origin_digest != state.spec_digest:
                chain = AmendmentLedger(run_id=state.run_id, origin_digest=state.spec_digest)
            new_text = old_text.rstrip("\n") + "\n\n" + section(halt_id, amendment_id, proposal, bound, keep, criteria, paths)
            amendment = park_and_amend(
                state.spec_path, new_text, run_id=state.run_id, amendment_id=amendment_id,
                kind=AmendmentKind.WIDEN, actor=ACTOR,
                reason=f"the double-down proved a correction for the block {halt_id}: {proposal['summary'][:300]}",
                affected_criteria=list(bound),
                evidence=[f"dossier:{halt_id}", f"verification:{dossier.verification['digest'][:16]}",
                          f"fix:{_sha(proposal['fix'])[:16]}"],
                after_acceptance=state.last_accepted_candidate is not None, write_seq=state.write_seq)
            chain = AmendmentLedger(run_id=chain.run_id, origin_digest=chain.origin_digest,
                                    amendments=[*chain.amendments, amendment])
            store.write_amendments(chain)
            state.note(f"specification amended: {amendment.summary()}")
            state.spec_digest = text_digest(new_text)
            stages.unblock(state, f"double-down correction {halt_id} applied as amendment "
                                  f"{amendment_id}: the run continues in a new iteration")
            store.write_state(state)
    except (LockBusy, StoreError) as exc:
        raise dd.DossierError(f"the run is busy or unreadable: {exc}") from exc
    return None


def apply_if_due(root: Path | str, owner_dir: Path, halt_id: str, *, session: ddverify.Workspaces,
                 now: datetime | None = None, **kw) -> dict | None:
    """`apply` without an approval, but only for a dossier that has a verified fix and no decision
    yet; None (and nothing read twice for nothing) for every other: this is what the pass asks. It
    measures nothing: it records what is decided without a person (a refusal that no measurement can
    change) or that the fix is WITHHELD for one."""
    try:
        dossier = dd.read_dossier(dd.dossier_path(owner_dir, halt_id))
    except (dd.DossierError, OSError):
        return None                                       # the pass's own row says refused
    if dossier.application is not None or dossier.withheld is not None or appliable(dossier):
        return None
    return apply(root, owner_dir, halt_id, session=session, now=now, **kw)


# --- reversal --------------------------------------------------------------------------------- #

def revert(root: Path | str, owner_dir: Path, halt_id: str, *, by: str, reason: str,
           now: datetime | None = None) -> dict:
    """A person puts the superseded specification back (`hoh double-down revert`): a second
    amendment (NARROW, by `by`) restores the parked text, and `revert.json` records it. Only an
    APPLIED dossier whose amendment is still the last of the run's chain, whose text is still the
    one it wrote and which no driver holds can be reverted; once. The unblock is not undone and
    nothing else is touched. Never raises for a refusal: the row says what happened."""
    from . import rundrive
    from .amendment import AmendmentKind, AmendmentLedger, park_and_amend, text_digest
    from .store import LockBusy, RunStore, StoreError

    now = now or datetime.now(UTC)
    root = Path(root).expanduser()
    row: dict = {"scope": "dossier", "target": dd._safe(halt_id), "halt_id": dd._safe(halt_id)}
    try:
        by = dd._person_text(by, "by")
        reason = dd._person_text(reason, "reason")
        path = dd.dossier_path(owner_dir, halt_id)
        with dd._locked(path):
            dossier = dd.read_dossier(path)
            if dossier.reversal is not None:
                return {**row, "action": "closed", "by": dd._safe(dossier.reversal["by"])}
            application = dossier.application
            if application is None or application["status"] != "APPLIED":
                raise dd.DossierError("nothing was applied: nothing to revert")
            run_id = dossier.block["object_id"]
            store = RunStore(root, run_id)
            if store.dir != Path(owner_dir):
                raise dd.DossierError("the dossier does not belong to the run it names")
            held = rundrive.refusal_for_resume(root, run_id)
            if held is not None:
                raise dd.DossierError(held)
            try:
                with store.lock():
                    state = store.read_state()
                    chain = store.read_amendments(origin_digest=state.spec_digest)
                    last = chain.amendments[-1] if chain.amendments else None
                    if last is None or last.amendment_id != application["amendment_id"]:
                        raise dd.DossierError("a later amendment exists (or this one is gone): nothing is "
                                              "reverted over it")
                    text = Path(state.spec_path).read_text(encoding="utf-8")
                    if text_digest(text) != last.to_digest or state.spec_digest != last.to_digest:
                        raise dd.DossierError("the specification is no longer the text this application wrote")
                    old = Path(last.from_path).read_text(encoding="utf-8")
                    if text_digest(old) != last.from_digest:
                        raise dd.DossierError("the parked text does not hash to what the amendment claims")
                    amendment = park_and_amend(
                        state.spec_path, old, run_id=state.run_id, amendment_id=last.amendment_id + "-revert",
                        kind=AmendmentKind.NARROW, actor=by, reason=reason,
                        affected_criteria=list(last.affected_criteria),
                        evidence=[f"reverts:{last.amendment_id}"],
                        after_acceptance=state.last_accepted_candidate is not None,
                        write_seq=state.write_seq)
                    store.write_amendments(AmendmentLedger(
                        run_id=chain.run_id, origin_digest=chain.origin_digest,
                        amendments=[*chain.amendments, amendment]))
                    state.note(f"specification amended: {amendment.summary()}")
                    state.spec_digest = text_digest(old)
                    store.write_state(state)
            except (LockBusy, StoreError) as exc:
                raise dd.DossierError(f"the run is busy or unreadable: {exc}") from exc
            dd._create(path / "revert.json", {
                "schema": dd.SCHEMA_REVERSAL, "block_digest": dossier.block["digest"],
                "application_digest": application["digest"], "amendment_id": amendment.amendment_id,
                "by": by, "reason": reason, "reverted_at": dd._stamp(now)})
            return {**row, "action": "reverted", "amendment_id": amendment.amendment_id, "by": dd._safe(by)}
    except (dd.DossierError, OSError, ValueError) as exc:
        return {**row, "action": "refused", "why": dd._safe(f"{type(exc).__name__}: {exc}")}
