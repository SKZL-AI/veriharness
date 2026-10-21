"""The bounded executor of pre-authorised external actions (V3.3 P3-32, fourth slice).

`governance.resolve_routed_decisions` answers an EXTERNAL decision "within the pre-authorisation" and
says `executes: false`. The operator decided (2026-10-20) that VeriHarness itself carries such an action
out when -- and only when -- the contract names where it may act, how many times, and that publishing to
that place is pre-authorised. This module is that, and nothing wider:

* the contract's release policy is `AUTO_PUBLISH_PREAUTHORIZED_TARGET` and names the target; any other
  release policy (`NEVER_AUTO_PUBLISH` first) is the standing answer "a person does this" and no decision
  is even looked at -- nothing is read, written or called;
* the release escalation resolves by policy (a contract that asks a human about releases is not
  pre-authorising one);
* the node declares its action in its note, in one fixed form (`external-action <verb> <target>`), and the
  target is the contract's `preauthorized_target`, character for character;
* the decision is answered (`governance.answered_external_decisions`: the resolver's own record, exactly
  as it would write it today), the programme is not stopped (`governance.programme_stoppers`: the one stop
  the running programme has -- the contract's `kill_switch` only declares channels), the contract lets the
  wave continue (the pass asks `continue_by_policy` first) and a verb is registered for it;
* the number of attempts recorded for the programme epoch is below `horizon.max_external_actions`.

Then, in this order and each durable before the next: an **attempt** record is created (create-only,
digest-bound: what, target, the contract digest, the decision it answers, its ordinal), the actor is
called once, an **outcome** record is created. A decision has at most one attempt, ever, under any
contract: an attempt with no outcome (a crash, a record that could not be written) is
`ATTEMPT_UNFINISHED` and stays with a person -- the executor never repeats an act whose result it does
not know. Every other case leaves the decision where it was (open, in front of a person); nothing here
approves, unblocks or settles a node.

What this module does not do: it registers no verb (`production_actors` is empty -- a verb is a reviewed
act of its own), it does not settle the node it acted for, and it is not a way around a person: a
registry entry only ever runs after every condition above holds.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import durable_io, governance
from .autonomy import AutonomyContract, Handling, ReleasePolicy, route

ACT_SCHEMA = "hoh-external-act/1"
#: Where a project keeps the acts, two create-only files per decision: `<decision>-<contract>.attempt.json`
#: (written before the actor is called) and `<decision>-<contract>.outcome.json`.
ACTS_DIR = "external-acts"
MAX_ACT_BYTES = 1 << 14
#: A directory with more act records than this is not read: it is not a project's own.
MAX_ACT_RECORDS = 4096
MAX_DETAIL = 200

#: A node declares its action in its note, in exactly this form (one line, ASCII, nothing around it).
_DECLARATION = re.compile(r"external-action ([a-z][a-z0-9-]{0,31}) ([A-Za-z0-9][A-Za-z0-9._:/@-]{0,255})",
                          re.ASCII)
_ACT_NAME = re.compile(r"([0-9a-f]{16})-([0-9a-f]{16})\.(attempt|outcome)\.json", re.ASCII)

EXECUTED, FAILED = "EXECUTED", "FAILED"
LEFT_WITH_PERSON, DONE_BEFORE = "LEFT_WITH_PERSON", "DONE_BEFORE"

#: Why a decision is left with a person: a fact about the decision or the contract, or a failure to read or to
#: record (both said by the pass); what a contract that does not name a target says is not a reason at all --
#: nothing is looked at (`execute_answered_decisions`, first line).
PROGRAMME_STOPPED = "PROGRAMME_STOPPED"
ROUTING_LOG_UNREADABLE = "ROUTING_LOG_UNREADABLE"
RELEASE_NOT_RESOLVED_BY_POLICY = "RELEASE_NOT_RESOLVED_BY_POLICY"
NO_ACTION_DECLARED = "NO_ACTION_DECLARED"
TARGET_NOT_PREAUTHORIZED = "TARGET_NOT_PREAUTHORIZED"
NO_ACTOR = "NO_ACTOR"
NO_PREAUTHORIZATION = "NO_PREAUTHORIZATION"
PREAUTHORIZATION_SPENT = "PREAUTHORIZATION_SPENT"
ALREADY_EXECUTED = "ALREADY_EXECUTED"
ALREADY_FAILED = "ALREADY_FAILED"
ATTEMPT_UNFINISHED = "ATTEMPT_UNFINISHED"
ACTS_UNREADABLE = "ACTS_UNREADABLE"
ATTEMPT_NOT_RECORDED = "ATTEMPT_NOT_RECORDED"
OUTCOME_NOT_RECORDED = "OUTCOME_NOT_RECORDED"

#: The reasons the pass says (a step each); the others say an act that was made before, and say nothing again.
VISIBLE_REASONS = frozenset({
    PROGRAMME_STOPPED, ROUTING_LOG_UNREADABLE, RELEASE_NOT_RESOLVED_BY_POLICY, NO_ACTION_DECLARED, TARGET_NOT_PREAUTHORIZED, NO_ACTOR,
    NO_PREAUTHORIZATION, PREAUTHORIZATION_SPENT, ATTEMPT_UNFINISHED, ACTS_UNREADABLE,
    ATTEMPT_NOT_RECORDED, OUTCOME_NOT_RECORDED})


@dataclass(frozen=True)
class ActionOrder:
    """What an actor is told, and all it is told: plain strings, nothing to write through."""

    project_id: str
    node_id: str
    verb: str
    target: str
    decision_digest: str
    contract_digest: str
    attempt_sha256: str


#: An actor carries out one verb against the target of its order and returns a short text; any exception
#: is a failed act. It is called at most once per decision.
Actor = Callable[[ActionOrder], object]


def production_actors() -> Mapping[str, Actor]:
    """The verbs the product executes by itself: none. A verb is registered here by a change of its own,
    reviewed as one; until then every answered action stays with a person (`NO_ACTOR`)."""
    return {}


@dataclass(frozen=True)
class ExecutedDecision:
    node_id: str
    decision_digest: str
    outcome: str              # EXECUTED | FAILED (this call acted), DONE_BEFORE, LEFT_WITH_PERSON
    reason: str = ""
    verb: str = ""
    target: str = ""
    detail: str = ""

    def describe(self) -> str:
        """One line for the drive's step: the decision, what became of it, what was asked, what the verb said."""
        said = f"{self.verb} {self.target}".strip()
        line = f"decision {self.decision_digest} {self.outcome}"
        if self.reason:
            line += f": {self.reason}"
        if said:
            line += f" ({said})"
        if self.detail:
            line += f" -- {self.detail}"
        return line[:300]


@dataclass(frozen=True)
class ExecutionReport:
    project_id: str
    contract_digest: str
    decisions: tuple[ExecutedDecision, ...]


def declared_action(note: object) -> tuple[str, str] | None:
    """`(verb, target)` of a node note that is exactly one declaration, else None."""
    if type(note) is not str:
        return None
    found = _DECLARATION.fullmatch(note)
    return (found.group(1), found.group(2)) if found else None


def _record(body: dict) -> bytes:
    return (json.dumps(body, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode("ascii")


def _tidy(text: object) -> str:
    """A short, printable, ASCII text for a record: whatever an actor said, made safe to store."""
    if type(text) is not str:
        return ""
    plain = text.encode("ascii", "backslashreplace").decode("ascii")
    return "".join(c if " " <= c <= "~" else " " for c in plain)[:MAX_DETAIL]


def _read_acts(directory: Path, project_id: str
               ) -> tuple[dict[str, bytes], dict[str, bytes], dict[int, int], str]:
    """`(attempts, outcomes, attempts by programme epoch, "")` of the act records beside the state, or
    `({}, {}, {}, why)`. A name of the shape `<16 hex>-<16 hex>.attempt|outcome.json` is a record and must
    be one: a link, a FIFO, a second name, a file over `MAX_ACT_BYTES`, bytes that are not one JSON object
    of this schema, project, phase and file name, or an outcome with no attempt it names, make the whole
    directory unreadable -- a count taken from part of a directory is a count that damage can lower. Other
    names are not records."""
    attempts: dict[str, bytes] = {}
    outcomes: dict[str, bytes] = {}
    epochs: dict[int, int] = {}
    try:
        kind = os.lstat(directory)
    except FileNotFoundError:
        return attempts, outcomes, epochs, ""
    except OSError as exc:
        return {}, {}, {}, type(exc).__name__
    if not stat.S_ISDIR(kind.st_mode):
        return {}, {}, {}, "not a directory of its own"
    try:
        entries = sorted(os.listdir(directory))
    except OSError as exc:
        return {}, {}, {}, type(exc).__name__
    bodies: dict[str, dict] = {}
    for entry in entries:
        named = _ACT_NAME.fullmatch(entry)
        if named is None:
            continue
        if len(bodies) >= MAX_ACT_RECORDS:
            return {}, {}, {}, f"more than {MAX_ACT_RECORDS} records"
        path = directory / entry
        try:
            if os.lstat(path).st_size > MAX_ACT_BYTES:
                return {}, {}, {}, f"{entry} is larger than {MAX_ACT_BYTES} bytes"
            data = durable_io.read_single_link(path)
        except OSError as exc:
            return {}, {}, {}, f"{entry}: {type(exc).__name__}"
        try:
            body = json.loads(data.decode("ascii")) if data is not None else None
        except (ValueError, UnicodeDecodeError):
            body = None
        phase = named.group(3).upper()
        if (type(body) is not dict or body.get("schema") != ACT_SCHEMA or body.get("phase") != phase
                or body.get("project_id") != project_id
                or entry != f"{body.get('decision_digest')}-{body.get('contract_digest')}.{named.group(3)}.json"
                or type(body.get("program_epoch")) is not int or data != _record(body)):
            return {}, {}, {}, f"{entry} is not an act record of this project"
        bodies[entry] = body
        (attempts if phase == "ATTEMPT" else outcomes)[entry] = data
    for entry, data in outcomes.items():
        mate = attempts.get(entry.replace(".outcome.", ".attempt."))
        if mate is None or bodies[entry].get("attempt_sha256") != hashlib.sha256(mate).hexdigest():
            return {}, {}, {}, f"{entry} names no attempt it follows"
    for entry in attempts:
        epoch = bodies[entry]["program_epoch"]
        epochs[epoch] = epochs.get(epoch, 0) + 1
    return attempts, outcomes, epochs, ""


def _fsync_directory(path: Path) -> None:
    """Makes a directory entry durable: opens the directory and fsyncs it (an `OSError` is the caller's refusal)."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _create(directory: Path, name: str, data: bytes) -> bool:
    """Creates `directory/name` with `data`, once: True when it is durably there as written. A directory
    that is not one, a record that exists and a write that fails are all False -- never a replacement. A directory
    made here is made durable in its parent before the record goes in (the first act of a project would otherwise
    be durable in a directory whose own entry is not)."""
    try:
        kind = os.lstat(directory)
    except FileNotFoundError:
        kind = None
    except OSError:
        return False
    if kind is not None and not stat.S_ISDIR(kind.st_mode):
        return False
    try:
        if kind is None:
            os.mkdir(directory, 0o700)
            _fsync_directory(directory.parent)
        durable_io.durable_create(directory / name, data, mode=0o600)
    except OSError:
        return False
    return True


def execute_answered_decisions(store, contract: AutonomyContract, runs_dir: Path | str, *,
                               actors: Mapping[str, Actor]) -> ExecutionReport:
    """Carries out the answered EXTERNAL decisions the contract pre-authorises, within the number it
    pre-authorises, and leaves every other one with a person.

    For each decision `governance.answered_external_decisions` returns, in its order, the first condition
    that fails leaves it with a person and records nothing:

    0. (before any decision is looked at) the release policy is `AUTO_PUBLISH_PREAUTHORIZED_TARGET`:
       `NEVER_AUTO_PUBLISH` and every other policy return an empty report, nothing read, written or called;
    0b. the programme is not stopped (`PROGRAMME_STOPPED`, or `ROUTING_LOG_UNREADABLE`: a log that could say so
       cannot be read) -- every answered decision stays with a person;
    1. `route(kind="release")` resolves by policy (`RELEASE_NOT_RESOLVED_BY_POLICY`);
    2. the node's note is one declaration (`NO_ACTION_DECLARED`) whose target is the contract's
       `preauthorized_target` exactly (`TARGET_NOT_PREAUTHORIZED`);
    3. a verb of `actors` is registered for it (`NO_ACTOR`);
    4. the acts beside the state can be read (`ACTS_UNREADABLE`);
    5. the decision has no attempt yet, under any contract: one with an outcome is `DONE_BEFORE`
       (`ALREADY_EXECUTED` / `ALREADY_FAILED`, never repeated), one without is `ATTEMPT_UNFINISHED` (never
       repeated either);
    6. the attempts of the programme epoch are fewer than `horizon.max_external_actions`
       (`NO_PREAUTHORIZATION`, `PREAUTHORIZATION_SPENT`).

    Then the attempt is created (`ATTEMPT_NOT_RECORDED`: the actor is not called), the actor is called once
    (an exception is a `FAILED` act), and the outcome is created (`OUTCOME_NOT_RECORDED`: the act was made
    and the attempt stands, so the decision is unfinished from then on).

    Raises `governance.RoutingRefused` (nothing done) for a contract, a runs directory or a registry that
    cannot be one, and for a graph without a frontier."""
    if not isinstance(contract, AutonomyContract):
        raise governance.RoutingRefused("the contract must be an AutonomyContract")
    if not isinstance(actors, Mapping):
        raise governance.RoutingRefused("the actors must be a mapping of verbs to callables")
    digest = contract.digest()
    if contract.release_policy is not ReleasePolicy.AUTO_PUBLISH_PREAUTHORIZED_TARGET:
        # NEVER_AUTO_PUBLISH first, and every policy that does not name a target: the standing answer is
        # "a person does this" -- no decision is looked at, nothing is read, written or called
        return ExecutionReport(store.project_id, digest, ())
    answered = governance.answered_external_decisions(store, contract, runs_dir)
    directory = store.dir / ACTS_DIR
    stoppers, broken = governance.programme_stoppers(store, runs_dir) if answered else ((), ())
    if stoppers or broken:
        # the programme is stopped (or a log that could say so cannot be read): nothing acts, every answered
        # decision stays with a person and the pass says why -- independent of what the pass decided
        why = PROGRAMME_STOPPED if stoppers else ROUTING_LOG_UNREADABLE
        return ExecutionReport(store.project_id, digest, tuple(
            ExecutedDecision(a.node_id, a.decision_digest, LEFT_WITH_PERSON, why) for a in answered))
    done: list[ExecutedDecision] = []
    acts: tuple[dict[str, bytes], dict[str, bytes], dict[int, int], str] | None = None
    for a in answered:
        def left(reason: str, verb: str = "", target: str = "", _a=a) -> ExecutedDecision:
            return ExecutedDecision(_a.node_id, _a.decision_digest, LEFT_WITH_PERSON, reason, verb, target)

        verdict = route(contract, decision=a.note or f"external action {a.node_id}", kind="release",
                        delegable=True)
        if verdict.handling is not Handling.RESOLVED_BY_POLICY:
            done.append(left(RELEASE_NOT_RESOLVED_BY_POLICY))
            continue
        declared = declared_action(a.note)
        if declared is None:
            done.append(left(NO_ACTION_DECLARED))
            continue
        verb, target = declared
        if not contract.preauthorized_target or target != contract.preauthorized_target:
            done.append(left(TARGET_NOT_PREAUTHORIZED, verb, target))
            continue
        actor = actors.get(verb)
        if not callable(actor):
            done.append(left(NO_ACTOR, verb, target))
            continue
        if acts is None:
            acts = _read_acts(directory, store.project_id)
        attempts, outcomes, epochs, why_unreadable = acts
        if why_unreadable:
            done.append(left(ACTS_UNREADABLE, verb, target))
            continue
        name = f"{a.decision_digest}-{digest}"
        # one attempt per decision, ever: under any contract (a new epoch or a new digest is a new authorisation,
        # not a new decision -- the node is not settled, and the same action must not be made twice)
        earlier = [n for n in sorted(attempts) if n.startswith(f"{a.decision_digest}-")]
        if earlier:
            ends = [outcomes.get(n.replace(".attempt.", ".outcome.")) for n in earlier]
            if None in ends:
                done.append(left(ATTEMPT_UNFINISHED, verb, target))
                continue
            made = any(json.loads(e).get("outcome") == EXECUTED for e in ends)
            done.append(ExecutedDecision(a.node_id, a.decision_digest, DONE_BEFORE,
                                         ALREADY_EXECUTED if made else ALREADY_FAILED, verb, target))
            continue
        cap = contract.horizon.max_external_actions
        if type(cap) is not int or cap < 1:
            done.append(left(NO_PREAUTHORIZATION, verb, target))
            continue
        used = epochs.get(contract.program_epoch, 0)
        if used >= cap:
            done.append(left(PREAUTHORIZATION_SPENT, verb, target))
            continue
        base = {"schema": ACT_SCHEMA, "project_id": store.project_id, "node_id": a.node_id,
                "decision_digest": a.decision_digest, "contract_id": contract.contract_id,
                "contract_digest": digest, "program_epoch": contract.program_epoch,
                "verb": verb, "target": target}
        attempt = _record({**base, "phase": "ATTEMPT", "answer": a.record_name,
                           "answer_sha256": a.record_sha256, "ordinal": used + 1, "pre_authorized": cap,
                           "release_policy": contract.release_policy.value})
        if not _create(directory, f"{name}.attempt.json", attempt):
            done.append(left(ATTEMPT_NOT_RECORDED, verb, target))
            acts = None                      # read again before any further decision
            continue
        attempts[f"{name}.attempt.json"] = attempt
        epochs[contract.program_epoch] = used + 1
        request = ActionOrder(store.project_id, a.node_id, verb, target, a.decision_digest, digest,
                              hashlib.sha256(attempt).hexdigest())
        try:
            detail, outcome = _tidy(actor(request)), EXECUTED
        except Exception as exc:             # an actor's failure is a result, never the loop's crash
            detail, outcome = _tidy(f"{type(exc).__name__}: {exc}"), FAILED
        written = _record({**base, "phase": "OUTCOME", "outcome": outcome, "detail": detail,
                           "attempt_sha256": request.attempt_sha256})
        if _create(directory, f"{name}.outcome.json", written):
            outcomes[f"{name}.outcome.json"] = written
            done.append(ExecutedDecision(a.node_id, a.decision_digest, outcome, "", verb, target, detail))
        else:
            done.append(ExecutedDecision(a.node_id, a.decision_digest, outcome, OUTCOME_NOT_RECORDED,
                                         verb, target, detail))
    return ExecutionReport(store.project_id, digest, tuple(done))
