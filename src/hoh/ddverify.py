"""The verification of a correction proposal (V3.3 P3-42, slice 2).

The double-down (`hoh.doubledown`) ends a block in a correction PROPOSAL that is only a claim.
Before anything may apply one, the claim is measured. For a `repair_spec` proposal that carries
its spec text, `verify` does what the product's own QA does with a plan's criteria, on a
scratch copy and never on the live worktree:

1. the spec's one fenced `criteria` block is read by `spec_criteria.declared_criteria`;
2. the run's last accepted candidate (its base when it has none) is materialised
   (`workspace.materialize`, a `git archive` of the bound tree: read-only for the repository)
   into a registered temporary workspace outside the run;
3. every criterion runs through `runner.run_check` under the requested isolation (STRICT by
   default: a read-only candidate, no network), each in a workspace of its own, and its receipt
   decides: PASS / FAIL / INCONCLUSIVE (`Receipt.outcome`);
4. a red criterion counts as a red only when it is not *artefactual*: the runner printed one of
   its "nothing ran / the file is not there" signatures (`runner.artefactual_reason`) **and** the
   command exited 4 or 5 (pytest's "usage error" and "no tests collected"). The signature alone is
   not enough: a real `ModuleNotFoundError` or `FileNotFoundError` raised by a test that ran
   (exit 1 or 2) is a red, and the record keeps `signature_seen` so the person can see it.

The verdict is recorded create-only as `verify.json` in the dossier, bound to the block, the
outcome and the proposal by digest, and says one of three things:

* RED_REPRODUCED -- every criterion was measured and at least one failed on the unrepaired
  candidate for a reason that is not its own absence. **That is all it says.** It is neither a
  proof that the repair works nor a proof that the red is the block's: the verdict never consults
  the block's kind, reason or facts, so an unrelated failing test of the same candidate gives the
  same status (SUSPECTED consequence for a later slice: an unrelated red can be fixed and turn
  green). The record says so in its `says` field and the decision a person reads names the
  criteria that failed;
* REFUTED   -- the measurement does not support the proposal: its spec is refused (no or more
  than one criteria block, a criterion the runner's guards refuse), no criterion is red, or
  every red is artefactual;
* INCONCLUSIVE -- the measurement did not come about (a runner, sandbox or workspace failure,
  a timeout), and the bound `MAX_VERIFY_ATTEMPTS` is spent. Never a verdict on the proposal.

**What RED_REPRODUCED does not say.** It does not say the repair works: nothing is repaired here.
Whether a repair turns the criteria green is for the product's own run of the repair spec, when a
later slice applies a proposal under the autonomy contract; that slice has to decide what binds
a red to the block (Q9), this one does not. (Slice 3, `hoh.ddapply`, does both: it proves a fix on a
scratch copy and ties the red to the run's own failed checks before anything is applied.)

**Termination.** An attempt is recorded (`verify-attempt-N.json`) before anything is measured,
so a crash inside it counts; at most `MAX_VERIFY_ATTEMPTS` attempts exist, and the last one
that measures nothing writes the terminal INCONCLUSIVE itself. A deterministic result (RED_REPRODUCED,
REFUTED) is terminal at once. A terminal record is quiet: a later call reads it and writes
nothing. A record that cannot be written leaves the attempt counted, and the next call either
measures again (an attempt left) or only tries to write the terminal record (none left).

**The firewall** is the double-down's: this module writes the dossier's own create-only
records and the temporary workspaces' contents, never a run or project state, a timer or a lease,
and lifts no block. A criterion is a command an analyst proposed: it is never run outside the
runner's guards (`assert_command_allowed`, `assert_stays_in_arena`) and, by default, never outside
the sandbox; a criterion the guards refuse refutes the spec.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import doubledown as dd
from .contracts import AcceptanceCheck, Candidate, Outcome
from .runner import ArenaEscape, HouseRuleViolation, PolicyUnavailable, artefactual_reason
from .sandbox import Isolation
from .spec_criteria import CriteriaBlockError, declared_criteria

#: Attempts a proposal's verification may take: one that measured nothing is tried again by the
#: next call, no more than this often; the last one ends INCONCLUSIVE, for good.
MAX_VERIFY_ATTEMPTS = 3
#: Criteria a spec may declare, and the seconds each may run: one attempt takes at most their
#: product (40 minutes) plus the materialisation.
MAX_CRITERIA = 8
CRITERION_TIMEOUT = 300
#: What the workspaces are admitted for (bytes): the scratch copy of the candidate, and the
#: scratch (HOME, TMPDIR) of one criterion.
ARENA_ESTIMATE = 512 << 20
CHECK_ESTIMATE = 64 << 20
#: How much of a criterion's output a record keeps (characters of its tail).
TAIL_CHARS = 2000

RUN_ID_PREFIX = "ddv-"
#: The pass does not start an attempt sooner than this after the previous one began, so a transient
#: cause (a sandbox that was down for a minute) cannot spend the attempts in consecutive ticks.
#: A person's `hoh double-down verify` is not held back.
VERIFY_RETRY_SECONDS = 600
#: The exit codes in which a signature of `runner.artefactual_reason` means "nothing ran": pytest's
#: usage error (4: file or node not found) and "no tests collected" (5).
ARTEFACTUAL_EXITS = frozenset({4, 5})
#: What every verification record says about itself.
SAYS = ("a criterion failed on the unrepaired candidate; this is neither a proof that the repair works "
        "nor a proof that the red is the block's")


class VerifyRefused(RuntimeError):
    """There is nowhere confined to measure in (no registered temporary root, no admission): the
    row says why, nothing is recorded, no attempt is spent."""


#: A session of workspaces: `session(block)` is a context manager yielding
#: `make(purpose, name, estimate_bytes)`, itself a context manager yielding the path of one
#: registered, admitted temporary workspace (released when it ends); `VerifyRefused` when there
#: is none.
Workspaces = Callable[[dict], AbstractContextManager]


def _digest(obj: Any) -> str:
    return hashlib.sha256(dd._canon(obj)).hexdigest()


def verifiable(dossier: dd.Dossier) -> str:
    """Why this dossier has nothing to verify, or "" when it has a repair spec to measure."""
    if dossier.outcome is None:
        return "the dossier has no outcome"
    if dossier.outcome["outcome"] != "PROPOSAL":
        return f"the dossier ended in {dossier.outcome['outcome']}, not in a proposal"
    proposal = dossier.outcome.get("proposal") or {}
    if proposal.get("kind") != "repair_spec":
        return (f"a {proposal.get('kind')} proposal is checked against the plan (a later slice): "
                f"only a repair spec's criteria are run")
    if not isinstance(proposal.get("spec"), str) or not proposal["spec"].strip():
        return "the proposal carries no spec text"
    return ""


def _candidate_of(root: Path, block: dict) -> tuple[Candidate, str]:
    """The state a repair would start from: the run's last accepted candidate, else its base."""
    from .store import RunStore

    if block["scope"] == "run":
        run_id = block["object_id"]
    elif block["scope"] == "node":
        run_id = (block.get("facts") or {}).get("run_id")
    else:
        run_id = None
    if not isinstance(run_id, str) or not run_id:
        raise dd.DossierError("this block has no run, so no candidate to measure on")
    try:
        state = dd.read_run_state(RunStore(root, run_id).dir)
    except dd.DossierError:
        raise
    except Exception as exc:                              # noqa: BLE001 - an unusable run id
        raise dd.DossierError(f"the run {run_id!r} cannot be read: {exc}") from exc
    if state is None:
        raise dd.DossierError(f"the run {run_id!r} has no state")
    for source, candidate in (("last_accepted", state.last_accepted_candidate),
                              ("base", state.base_candidate)):
        if candidate is not None:
            if not Path(candidate.repo_path).is_dir():
                raise dd.DossierError(f"the candidate's repository {candidate.repo_path!r} is "
                                      f"not a directory")
            return candidate, source
    raise dd.DossierError(f"the run {run_id!r} has no accepted or base candidate")


def _measure(criteria, candidate: Candidate, arena: Path, halt_id: str, attempt: int, make,
             check_runner, isolation: Isolation, prefix: str = RUN_ID_PREFIX) -> list[dict]:
    """Run the criteria one by one, each in a workspace of its own; stop at the first one that
    decides the attempt (a refusal of the guards, a measurement that did not come about)."""
    probe = candidate.model_copy(update={"repo_path": str(arena)})
    rows: list[dict] = []
    for k, criterion in enumerate(criteria, 1):
        row: dict = {"id": criterion.criterion_id, "command": criterion.command}
        try:
            check = AcceptanceCheck(
                check_id=criterion.criterion_id,
                description=f"double-down verification of {halt_id}: {criterion.criterion_id}",
                command=criterion.command)
        except ValueError as exc:                         # a criterion the plan schema refuses
            rows.append({**row, "outcome": "REFUSED", "why": dd._safe(exc, 500)})
            break
        try:
            with make("check", f"ddc-{halt_id}-{k}", CHECK_ESTIMATE) as scratch:
                receipt, log = check_runner(
                    check, probe, run_id=prefix + halt_id, iteration=1, attempt=attempt,
                    cwd=arena, timeout=CRITERION_TIMEOUT, isolation=isolation, scratch=scratch)
        except (HouseRuleViolation, ArenaEscape) as exc:
            rows.append({**row, "outcome": "REFUSED", "why": dd._safe(exc, 500)})
            break
        except (PolicyUnavailable, VerifyRefused, ValueError, OSError) as exc:
            rows.append({**row, "outcome": "INCONCLUSIVE",
                         "why": dd._safe(f"{type(exc).__name__}: {exc}", 500)})
            break
        outcome = receipt.outcome(check.expect_exit)
        seen = bool(outcome is Outcome.FAIL and artefactual_reason(receipt.exit_code, log))
        row.update(outcome=outcome.value, exit_code=receipt.exit_code, receipt_id=receipt.receipt_id,
                   stdout_digest=receipt.stdout_digest, runner_ok=receipt.runner_ok,
                   signature_seen=seen, artefactual=seen and receipt.exit_code in ARTEFACTUAL_EXITS,
                   tail=dd._safe(log[-TAIL_CHARS:], TAIL_CHARS))
        rows.append(row)
        if outcome is Outcome.INCONCLUSIVE:
            break
    return rows


def decide(rows: list[dict], declared: int) -> tuple[str, str, str]:
    """`(status, reason, why)` of one attempt's measured rows. A guard's refusal is the spec's
    fault and decides first; a measurement that did not come about is no verdict; the rest is
    arithmetic on the reds."""
    if any(r["outcome"] == "REFUSED" for r in rows):
        return "REFUTED", "criterion_refused", "the runner's guards refuse a criterion of the spec"
    if len(rows) < declared or any(r["outcome"] == "INCONCLUSIVE" for r in rows):
        return "INCONCLUSIVE", "not_measured", "a criterion could not be measured"
    red = [r for r in rows if r["outcome"] == "FAIL"]
    if any(not r["artefactual"] for r in red):
        return "RED_REPRODUCED", "red_found", ("a criterion fails on the unrepaired candidate for a "
                                               "reason that is not its own absence; " + SAYS)
    if red:
        return "REFUTED", "only_artefactual", ("every red criterion is red because its own file "
                                               "is not there yet: nothing reproduces the block")
    return "REFUTED", "nothing_red", "every criterion is already green on the unrepaired candidate"


def _terminal(dossier: dd.Dossier, status: str, reason: str, why: str, *, spec_sha: str,
              proposal_digest: str, candidate: dict | None, isolation: Isolation,
              criteria: list[dict], now: datetime) -> dict:
    return dd._create(dossier.path / "verify.json", {
        "schema": dd.SCHEMA_VERIFICATION, "status": status, "reason": reason, "why": why[:500],
        "halt_id": dossier.block["halt_id"], "block_digest": dossier.block["digest"],
        "outcome_digest": dossier.outcome["digest"], "proposal_digest": proposal_digest,
        "says": SAYS if status == "RED_REPRODUCED" else None,
        "block": {"kind": dossier.block["kind"], "reason": dossier.block["reason"][:300]},
        "spec_sha256": spec_sha, "candidate": candidate, "isolation": isolation.value,
        "criteria": criteria, "attempts": len(dossier.attempts), "verified_at": dd._stamp(now)})


def verify(root: Path | str, owner_dir: Path, halt_id: str, *, session: Workspaces,
           isolation: Isolation = Isolation.STRICT, now: datetime | None = None,
           materialize: Callable[[Candidate, Path], Path] | None = None,
           check_runner: Callable[..., tuple] | None = None) -> dict:
    """Measure the repair spec of the dossier `halt_id` of `owner_dir` and record the verdict.

    Never raises for a refusal: the row says what happened -- `action` is `red_reproduced`, `refuted`,
    `inconclusive` (an attempt that measured nothing, one left), `closed` (already terminal:
    nothing is written), `refused` (nothing to verify, or nowhere to measure: nothing is
    recorded and no attempt is spent) or `unrecorded` (measured, the record could not be written).
    `executed` is True when an attempt measured. The dossier's flock is held throughout."""
    from . import workspace
    from .runner import run_check

    now = now or datetime.now(UTC)
    materialize = materialize or workspace.materialize
    check_runner = check_runner or run_check
    if not isinstance(isolation, Isolation):
        isolation = Isolation(isolation)
    row: dict = {"scope": "dossier", "target": dd._safe(halt_id), "halt_id": dd._safe(halt_id)}
    try:
        path = dd.dossier_path(owner_dir, halt_id)
        with dd._locked(path):
            dossier = dd.read_dossier(path)
            if dossier.verification is not None:
                v = dossier.verification
                return {**row, "action": "closed", "status": v["status"], "reason": v["reason"]}
            why = verifiable(dossier)
            if why:
                raise dd.DossierError(why)
            proposal = dossier.outcome["proposal"]
            spec_sha = hashlib.sha256(proposal["spec"].encode("utf-8", "replace")).hexdigest()
            fixed = dict(spec_sha=spec_sha, proposal_digest=_digest(proposal), isolation=isolation,
                         now=now)
            attempt = len(dossier.attempts) + 1
            if attempt > MAX_VERIFY_ATTEMPTS:
                last = dossier.results[-1]["reasons"] if dossier.results else ["no result recorded"]
                return _write(row, dossier, "INCONCLUSIVE", "attempts_spent",
                              f"{MAX_VERIFY_ATTEMPTS} attempts measured nothing: {'; '.join(last)}",
                              candidate=None, criteria=[], **fixed)
            try:
                try:
                    proposal["spec"].encode("utf-8")
                except UnicodeEncodeError as exc:         # a lone surrogate is no text a shell gets
                    raise CriteriaBlockError(f"the spec is not valid UTF-8 text: {exc.reason}") from exc
                criteria = declared_criteria(proposal["spec"])
                if not criteria:
                    raise CriteriaBlockError("the spec declares no criteria block")
                if len(criteria) > MAX_CRITERIA:
                    raise CriteriaBlockError(f"the spec declares {len(criteria)} criteria; at "
                                             f"most {MAX_CRITERIA} are measured")
            except CriteriaBlockError as exc:
                return _write(row, dossier, "REFUTED", "spec_refused", str(exc),
                              candidate=None, criteria=[], **fixed)
            candidate, source = _candidate_of(Path(root).expanduser(), dossier.block)
            summary = {"id": candidate.candidate_id, "binding": candidate.binding(),
                       "commit": candidate.commit, "tree_digest": candidate.tree_digest,
                       "source": source}
            with session(dossier.block) as make:
                with make("verification", f"ddv-{halt_id}", ARENA_ESTIMATE) as workspace_path:
                    # the attempt exists before anything is measured: a crash inside it counts
                    dd._create(path / f"verify-attempt-{attempt}.json", {
                        "schema": dd.SCHEMA_ATTEMPT, "n": attempt,
                        "block_digest": dossier.block["digest"],
                        "outcome_digest": dossier.outcome["digest"], "spec_sha256": spec_sha,
                        "started_at": dd._stamp(now)})
                    dossier = dd.read_dossier(path)
                    row["executed"] = True
                    try:
                        arena = Path(workspace_path) / "arena"
                        materialize(candidate, arena)
                        rows = _measure(criteria, candidate, arena, halt_id, attempt, make,
                                        check_runner, isolation)
                    except Exception as exc:              # noqa: BLE001 - no measurement came about
                        rows = [{"id": "-", "command": "-", "outcome": "INCONCLUSIVE",
                                 "why": dd._safe(f"{type(exc).__name__}: {exc}", 500)}]
            status, reason, why = decide(rows, len(criteria))
            if status == "INCONCLUSIVE" and attempt < MAX_VERIFY_ATTEMPTS:
                reasons = [r.get("why") or r["outcome"] for r in rows if r["outcome"] != "FAIL"
                           and r["outcome"] != "PASS"]
                dd._create(path / f"verify-result-{attempt}.json", {
                    "schema": dd.SCHEMA_RESULT, "n": attempt, "status": status, "reason": reason,
                    "reasons": [dd._safe(x, 300) for x in reasons] or [why],
                    "criteria": rows, "block_digest": dossier.block["digest"],
                    "outcome_digest": dossier.outcome["digest"], "at": dd._stamp(now)})
                return {**row, "action": "inconclusive", "status": status, "reason": reason,
                        "attempt": attempt, "attempts_left": MAX_VERIFY_ATTEMPTS - attempt}
            if status == "INCONCLUSIVE":
                reason, why = "attempts_spent", (f"{MAX_VERIFY_ATTEMPTS} attempts measured "
                                                 f"nothing; the last: {why}")
            return _write(row, dd.read_dossier(path), status, reason, why, candidate=summary,
                          criteria=rows, **fixed)
    except VerifyRefused as exc:
        return {**row, "action": "refused", "why": dd._safe(exc)}
    except (dd.DossierError, OSError) as exc:
        return {**row, "action": "refused", "why": dd._safe(f"{type(exc).__name__}: {exc}")}


def _write(row: dict, dossier: dd.Dossier, status: str, reason: str, why: str, *,
           candidate: dict | None, criteria: list[dict], **fixed) -> dict:
    """Record the terminal verdict; a record that cannot be written is said, not hidden."""
    try:
        record = _terminal(dossier, status, reason, why, candidate=candidate, criteria=criteria,
                           **fixed)
    except (dd.DossierError, OSError) as exc:
        return {**row, "action": "unrecorded", "status": status, "reason": reason,
                "why": dd._safe(f"{type(exc).__name__}: {exc}")}
    return {**row, "action": status.lower(), "status": record["status"], "reason": reason}


def verify_if_due(root: Path | str, owner_dir: Path, halt_id: str, *, session: Workspaces,
                  isolation: Isolation = Isolation.STRICT, now: datetime | None = None,
                  **kw) -> dict | None:
    """`verify`, but only for a dossier that has a repair spec and no verification yet; None
    (and nothing read twice for nothing) for every other: this is what the pass asks."""
    try:
        dossier = dd.read_dossier(dd.dossier_path(owner_dir, halt_id))
    except (dd.DossierError, OSError):
        return None                                       # the pass's own row says refused
    if dossier.verification is not None or verifiable(dossier):
        return None
    if dossier.attempts:                      # a transient cause must not spend the attempts in a row
        began = dd._when(dossier.attempts[-1].get("started_at"))
        if began is not None and (now or datetime.now(UTC)) < began + timedelta(seconds=VERIFY_RETRY_SECONDS):
            return None
    return verify(root, owner_dir, halt_id, session=session, isolation=isolation, now=now, **kw)
