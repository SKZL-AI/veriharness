"""The contract a run actually obeys, and the record of what it decided.

P2-24. `hoh.autonomy` answers "what may this programme decide alone"; this
module is what makes that answer happen to a real run. Two reviewers refused
to call the routing proven while nothing consulted it, and they were right: a
pure function with unit tests proves the artefact, not the capability.

Three things happen here:

* **The contract is pinned to the run, once.** `bind()` writes it into the run
  directory the first time and reads it back on every later call. A resumed run
  obeys the contract it started under, not whatever file the operator happens
  to pass on the resume command line -- and a *different* contract offered to a
  bound run is refused rather than silently adopted, because "which rules was
  this run under" must have one answer.
* **Every routing is recorded** in `<run>/autonomy/routings.jsonl`, with the
  rule that produced it. A programme that runs for weeks makes a long list of
  choices nobody watched; the list is only reviewable if each entry says which
  rule decided it.
* **The run acts on the answer.** A block becomes what the contract says it
  should be: a wait for a human, a parked branch with a durable retry time, a
  collected digest item, or a stopped programme. Without this, "Needs-You OFF"
  would be a field nobody read.

What this module does not do is lower a bar. The strictest thing a contract can
do here is refuse to proceed; nothing in it touches isolation, receipts or
acceptance.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .autonomy import (
    AutonomyContract,
    Handling,
    Routing,
    coherence,
    route,
)
from .contracts import Condition, RunState, utcnow
from .project import ActionClass


class ContractRefused(RuntimeError):
    """The contract cannot govern this run, and nothing was assumed instead."""


@dataclass
class Governor:
    """One run, under one pinned contract, with its decisions written down."""

    contract: AutonomyContract
    run_dir: Path

    @property
    def _log(self) -> Path:
        return self.run_dir / "autonomy" / "routings.jsonl"

    def decide(self, *, decision: str, kind: str = "decision",
               delegable: bool = True) -> Routing:
        """Route one decision and record it. The record is the point."""
        routing = route(self.contract, decision=decision, kind=kind, delegable=delegable)
        self._log.parent.mkdir(parents=True, exist_ok=True)
        with self._log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": utcnow(),
                "contract_digest": self.contract.digest(),
                "program_epoch": self.contract.program_epoch,
                **json.loads(routing.model_dump_json()),
            }) + "\n")
        return routing

    def routings(self) -> list[dict]:
        if not self._log.is_file():
            return []
        return [json.loads(line) for line in
                self._log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def evidence(self) -> dict:
        return {"contract_id": self.contract.contract_id,
                "digest": self.contract.digest(),
                "mode": self.contract.mode.value,
                "needs_you": self.contract.needs_you.value,
                "program_epoch": self.contract.program_epoch,
                "release_policy": self.contract.release_policy.value,
                "routings": len(self.routings())}


def _witnessed_digest(witness: Path) -> tuple[str | None, bool]:
    """(the digest recorded beside the pin, whether the record was readable).

    Two answers, not one: "no witness was ever written" and "the witness is
    damaged" are different situations, and treating the second as the first is
    what let a reviewer disable the tamper guard by overwriting one file.
    """
    if not witness.exists():
        return None, True
    try:
        return json.loads(witness.read_text(encoding="utf-8")).get("digest"), True
    except (OSError, ValueError, AttributeError):
        return None, False


def _first_logged_digest(run_dir: Path) -> str | None:
    """The contract digest of this run's first recorded decision, if any."""
    log = run_dir / "autonomy" / "routings.jsonl"
    try:
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                return json.loads(line).get("contract_digest")
    except (OSError, ValueError):
        return None
    return None


def pinned_path(run_dir: Path | str) -> Path:
    return Path(run_dir) / "autonomy" / "contract.json"


def bind(run_dir: Path | str, *, contract: AutonomyContract | None = None,
         program_epoch: int | None = None) -> Governor | None:
    """The contract this run is under.

    First call with a contract pins it; later calls read the pinned one back.
    Offering a *different* contract to a bound run is refused: a run that
    changed its rules halfway has no answer to "which rules was it under", and
    the plan's whole point is that the epoch is where such a change shows.

    Returns None when no contract was ever pinned and none is offered -- a run
    without a contract is the behaviour every run had before this existed, and
    it stays legal rather than becoming a silent default.
    """
    path = pinned_path(run_dir)
    witness = Path(run_dir) / "approval" / "autonomy.json"
    if path.is_symlink() or path.parent.is_symlink():
        raise ContractRefused(
            f"{path} is a link; the contract a run is judged against lives in the run "
            f"directory, not wherever a link points")
    if path.is_file():
        try:
            pinned = AutonomyContract.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:                      # noqa: BLE001 - one refusal type
            # A reviewer overwrote the pinned file with junk and got a
            # traceback out of `hoh deliver` and `hoh run` -- and, worse, out
            # of the abort handler whose whole job is to end bounded. One
            # exception type leaves this module, always.
            raise ContractRefused(
                f"the pinned contract in {path} cannot be read: {exc}") from exc
        # Bound to a digest recorded outside the file it protects: editing the
        # pinned contract into something wider is coherent, so `coherence()`
        # cannot catch it. A reviewer turned NEVER_AUTO_PUBLISH into
        # AUTO_PUBLISH_PREAUTHORIZED_TARGET this way and the delivery gate
        # opened.
        recorded, readable = _witnessed_digest(witness)
        if witness.exists() and not readable:
            raise ContractRefused(
                f"{witness} records which contract this run was bound to and cannot be "
                f"read. A guard that switches itself off when its own record is damaged "
                f"is not a guard; restore it or start a new run.")
        if recorded is None:
            # No witness: either a run pinned before `bind` wrote one itself, or
            # a witness that has been removed. The routing log is a second,
            # append-only record of the digest and answers the difference --
            # except on a freshly pinned run, which has no routing yet. That
            # gap is why the witness is written here rather than by the caller.
            logged = _first_logged_digest(Path(run_dir))
            if logged is None:
                raise ContractRefused(
                    f"{path} is pinned but neither {witness.name} nor a recorded "
                    f"decision says which contract this run was bound to. For a run "
                    f"pinned by this version both exist, so this is a tree somebody "
                    f"edited; restore it or start a new run.")
            if logged != pinned.digest():
                raise ContractRefused(
                    f"the pinned contract digests {pinned.digest()}, but this run's "
                    f"first recorded decision was made under {logged}. The contract "
                    f"file has been changed since the run started.")
        elif recorded != pinned.digest():
            raise ContractRefused(
                f"the pinned contract in {path} digests {pinned.digest()}, but this run "
                f"was bound to {recorded}. The contract file has been changed since the "
                f"run started; restore it or start a new run.")
        # Compared at the same epoch: the same contract offered again is the
        # same contract, whether or not the caller passed the epoch it was
        # pinned to. What is refused is a *different* set of rules.
        if contract is not None and \
                contract.pin(pinned.program_epoch).canonical() != pinned.canonical():
            raise ContractRefused(
                f"this run is pinned to contract {pinned.contract_id} "
                f"({pinned.digest()}, epoch {pinned.program_epoch}); it cannot be "
                f"resumed under {contract.contract_id} ({contract.digest()}). Start a "
                f"new run, or resume this one under the contract it began with.")
        return Governor(contract=pinned, run_dir=Path(run_dir))
    if contract is None:
        recorded, _ = _witnessed_digest(witness)
        if recorded:
            raise ContractRefused(
                f"this run was bound to contract {recorded}, and the pinned file "
                f"{path} is gone. A governed run does not quietly become an ungoverned "
                f"one; restore the contract or start a new run.")
        return None
    problems = coherence(contract)
    if problems:
        raise ContractRefused("; ".join(problems))
    pinned = contract if program_epoch is None else contract.pin(program_epoch)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pinned.model_dump_json(indent=2) + "\n", encoding="utf-8")
    governor = Governor(contract=pinned, run_dir=Path(run_dir))
    # The witness is written here, not by the caller: a run pinned a moment ago
    # has no routing yet, so a caller that wrote it afterwards left a window in
    # which a swapped pin had nothing to be compared against.
    witness.parent.mkdir(parents=True, exist_ok=True)
    witness.write_text(json.dumps(governor.evidence(), indent=2) + "\n",
                       encoding="utf-8")
    return governor


#: Noted when a contract's STOP or ABORT cancels a run that a capability
#: violation holds: the routing is recorded, the hold is not replaced.
_HELD_NOTE = ("autonomy: the run stays held by its capability violation; the "
              "contract's routing cancels it but does not replace the kind, and "
              "only a person's hoh unblock lifts the hold")


#: What a block means to the contract. A run blocks because it met something it
#: could not decide, so the decision is never delegable -- that is what being
#: blocked *is*.
def block_under_contract(state: RunState, reason: str, governor: Governor | None,
                         *, kind: str = "decision") -> Routing | None:
    """Block, then do what the contract says about blocking.

    Without a contract this is exactly the old behaviour: the run stays BLOCKED
    and waits for a person. With one, the four answers the plan names become
    four different states, and each of them is recorded:

    * **ask** -- BLOCKED, as before, because someone is expected;
    * **park** -- BLOCKED with `retry_after`, so the never-stuck invariant holds
      and a sentinel sweep can pick the branch up again;
    * **collect** -- BLOCKED as a digest item, with a retry time for the same
      reason;
    * **stop** -- the programme is over, and says so.

    The branch is what stops in every case. Whether *other* work continues is
    the scheduler's business, and a run that is one branch has nothing else to
    do -- which is why this records `blocks_branch` rather than deciding it.
    """
    from . import stages

    stages.block(state, reason)
    if governor is None:
        return None
    routing = governor.decide(decision=reason, kind=kind, delegable=False)
    state.note(f"autonomy: {routing.handling.value} -- {routing.why}")
    # `stages.block` has kept the kind of a capability violation (or adopted it
    # from a legacy reason). That hold has one carrier, `blocked_kind`, and this
    # function is its second writer: STOP and ABORT used to overwrite it, so the
    # next block (which rewrites the reason) lost the hold and `hoh deliver`
    # fast-forwarded a violation-held candidate (run qaw7, B1).
    held = state.blocked_kind == stages.CAPABILITY_VIOLATION
    if routing.handling is Handling.STOP_PROGRAM:
        state.condition = Condition.CANCELLED
        if held:
            state.note(_HELD_NOTE)
        else:
            state.blocked_kind = "contract_stop"
        state.stop_reason = f"the contract stops the programme: {reason}"
    elif routing.handling is Handling.ABORT_BRANCH:
        # Terminal, like STOP. A run *is* one branch, so an abandoned branch
        # with no retry time and no owner was a state `sentinel.stuck_run`
        # calls a control-plane defect -- which a reviewer found because the
        # profile sweep never routes to ABORT.
        state.condition = Condition.CANCELLED
        if held:
            state.note(_HELD_NOTE)
        else:
            state.blocked_kind = "contract_abort"
        state.stop_reason = f"the contract abandons this branch: {reason}"
    elif routing.handling in (Handling.PARK_BRANCH, Handling.COLLECT_FOR_DIGEST):
        parked = ("parked_by_contract" if routing.handling is Handling.PARK_BRANCH
                  else "collected_for_digest")
        if not state.retry_after:
            # A parked branch with no time to look again is the never-stuck
            # defect O209's neighbour module measures. The contract's review
            # interval is that time; without one, the branch keeps a human
            # owner instead, which is the third way out.
            seconds = (governor.contract.horizon.review_after_seconds
                       or governor.contract.horizon.max_wallclock_seconds)
            if seconds:
                from .timers import in_seconds

                state.retry_after = in_seconds(seconds)
                # And a durable timer, so the due branch surfaces in
                # `hoh timers` and `hoh sentinel`. Writing `retry_after` alone
                # silenced the never-stuck detector for a promise no component
                # kept -- green because a field was set, which is the shape
                # this project refuses everywhere else.
                _arm_review_timer(governor, state, state.retry_after, reason)
                # `_arm_review_timer` clears `retry_after` when it could not
                # arm, so the classification below sees the real state.
        # Three ways out, and the branch must keep one. A reviewer measured the
        # case this guards: a stock PLAN_AUTONOMOUS contract carries no review
        # interval, so a collected question had no retry time -- and
        # `sentinel.stuck_run` only reads a human owner from
        # `blocked_kind == "human"`. The result was a run this project's own
        # detector calls a control-plane defect, created inside a passing test.
        # The *cause* of the block stays in `blocked_kind`. A reviewer measured
        # what overwriting it did: a quota wait under an unattended contract
        # stopped being `usage_limit`, so `hoh resume-quota` would never pick it
        # up -- the one mode that must not depend on a person became the one
        # that needed `hoh unblock`, while a supervised run resumed by itself.
        # Exactly backwards.
        handling_kind = parked if state.retry_after else "human"
        cause = state.blocked_kind or "unclassified"
        if state.blocked_kind in (None, "", "human"):
            state.blocked_kind = handling_kind
        # Read before the assignment: a fresh park used to log "parked (cause:
        # parked)", which states nothing.
        state.note(f"autonomy: {handling_kind} (cause: {cause})")
        if not state.retry_after:
            state.note(f"autonomy: {parked} without a review interval, so this branch "
                       f"keeps a human owner rather than waiting for nobody")
    else:
        state.blocked_kind = state.blocked_kind or "human"
    return routing


def _arm_review_timer(governor: Governor, state: RunState, due_at: str,
                      reason: str) -> None:
    """A timer for a parked branch, in the run's own timer store."""
    from .timers import TimerKind, TimerStore

    try:
        TimerStore(governor.run_dir.parent).arm_new(
            kind=TimerKind.EXTERNAL_WAIT, due_at=due_at,
            reason=f"parked by the autonomy contract: {reason}",
            program_id=governor.contract.contract_id,
            program_epoch=governor.contract.program_epoch,
            run_id=state.run_id)
    except Exception as exc:                          # noqa: BLE001 - reported, not fatal
        # And the retry time goes with it: `retry_after` is what silences the
        # never-stuck detector, so leaving it standing after the timer failed
        # would restore exactly the shape arming the timer removed.
        state.retry_after = None
        state.note(f"autonomy: the review timer could not be armed ({exc}), so this "
                   f"branch keeps a human owner instead")


def record_override(governor: Governor | None, *, routing: Routing, by: str,
                    what: str) -> None:
    """A human went past a blocking routing. That is allowed; leaving no record
    of it is not.

    Without this the log said "parked" while the release happened -- a reviewer
    found the pair and it is the worst kind of record, one that is wrong in the
    direction of looking safer.
    """
    if governor is None:
        return
    governor._log.parent.mkdir(parents=True, exist_ok=True)
    with governor._log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "at": utcnow(), "contract_digest": governor.contract.digest(),
            "program_epoch": governor.contract.program_epoch,
            "override_of": routing.handling.value, "kind": routing.kind,
            "decision": what, "by": by,
            "why": f"a human authority overrode a {routing.handling.value} routing",
            # The routing's own words, kept: what was overridden matters as
            # much as that it was.
            "routing_why": routing.why,
        }) + "\n")


def forbidden_outright(routing: Routing | None) -> bool:
    """Is this a refusal a human at the keyboard may not wave through?

    A contract that does not allow external actions **at all** is not asking
    for approval; it is saying this programme does not do that. `--approve` is
    the captain's authority over a gate the contract *has*, not permission to
    leave the envelope it was given.
    """
    return routing is not None and routing.kind == ActionClass.EXTERNAL.value \
        and routing.blocks_branch


#: Delivery modes that stay inside this machine. `local-only` is a guarded
#: fast-forward of one local branch into another, which `project.ActionClass`
#: calls INTERNAL in as many words: "a `git merge` into the mainline is local
#: and reversible; a `git push` is not… treating anything else as external
#: turns the gate into noise that gets routinely waved through." Routing it as
#: EXTERNAL made every governed run unable to deliver locally, in every
#: profile, un-overridably -- a gate that refuses correct work, which is how a
#: gate teaches people to switch it off.
LOCAL_MODES = frozenset({"local-only"})


def may_release(governor: Governor | None, *, what: str,
                mode: str = "local-only") -> Routing | None:
    """Does the contract allow this delivery to proceed without a human?

    Returns the routing; the caller refuses unless it is resolved by policy.
    `None` means no contract governs this run, and the delivery rules that
    existed before decide alone.
    """
    if governor is None:
        return None
    if mode not in LOCAL_MODES and not governor.contract.may(ActionClass.EXTERNAL):
        # Leaving this machine, under a contract that does not allow it.
        return governor.decide(decision=what, kind=ActionClass.EXTERNAL.value)
    return governor.decide(decision=what, kind="release")
