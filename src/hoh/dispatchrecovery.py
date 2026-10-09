"""What a failed dispatch leaves behind, and what a park is bound to (P2-43, A5).

`hoh.failures` decides *what a failure was*. This module is the record of it:
the provenance of every provider dispatch, the exact work a park is bound to,
and the two receipts of the P2 routing semantics. Nothing here routes. P2 has
one policy -- wait on the same route (`same-route-wait/1`) -- and the records
say so, so that P3's broker has something to extend rather than to replace.

* **`DispatchProvenance`**: the provider, the model identity (or the stated
  `NOT_AVAILABLE`), the operator-registered account handle (or
  `NOT_AVAILABLE`; never a secret, see `check_account_handle`), the role, the
  route policy and its version, the capability requirements, the data class
  and the failure class. Persisted with every dispatch line of the run's
  telemetry, failed or not.
* **`ParkBinding`**: programme, programme epoch, run, iteration, attempt,
  stage, the candidate the work stands on, master and session. `park_id` is a
  digest of exactly those fields plus the class and `retry_at`, so a reader
  can recompute it from the live state and a different park cannot pass for
  this one.
* **`RoutingDecision` / `RoutingEscalation`**: one digest-bound line each,
  appended to `<run>/recovery/routing.jsonl` **before** the timer is armed and
  the parked state is persisted. The crash invariant of `resumeloop` extends:
  a parked state is never durable without its record and its wake-up; a record
  may be durable without a parked state (an orphan, which slice c's reader
  ignores because no live state carries its `retry_at`).
* **The master of a run**: a plain `hoh run` has none. It records
  `master: NONE` -- explicitly, in `<run>/recovery/master.json`, never by
  absence -- and a governed caller (a project node under `MasterAuthority`)
  passes `--master-session` and `--master-epoch`. Inventing a pseudo-master
  for a bare run would be a fiction; this records only what was declared.

Slice b **wrote** the binding; slice c **enforces** it, in `hoh.resumeloop`:

* **`verify_binding`** recomputes a park's id from the live state, the pinned
  contract and the declared master, and compares it with the receipt that
  describes the park (`retry_at` is the join). A park that no receipt
  describes, or whose run, iteration, attempt, stage, candidate, programme or
  master has moved since, is **refused**, not resumed. A park a governed run
  made without a valid receipt is refused; a park of a bare run that never had
  one is a legacy park (slice a's) and resumes as before.
* **`master_standing`** asks the project that owns the run -- its epoch chain
  and its lease, the only authority there is -- whether the declared master is
  real. A master the chain never held is foreign (refused); a master that was
  superseded by a successor is inherited by that successor (the park is still
  its predecessor's, and durable recovery means the successor resumes it); a
  master holding a **live** lease keeps the work (a consumer defers to it, as
  the project pass does).
* **`declare_master`** asks the same question of a master that launches a run:
  a stale master -- a superseded epoch, a released or lapsed lease -- cannot
  resume (relaunch) work. The check is against staleness and confusion, not
  against a hostile process of the same user, as the lease itself is.
* **`claim_park`** is the receipt of a resume, written after the park was
  lifted: one file per park, never replaced, so the same park is lifted once
  even if a state is restored from a copy. Concurrency is the run lock's job
  (both consumers hold it); the claim is not a mutex and says so.
* **`escalate_exhaustion`** appends the `RETRIES_EXHAUSTED` escalation when the
  resume loop gives a park up.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, StrictInt, field_validator, model_validator

from .contracts import Condition, Role, RunState, Strict, utcnow
from .durable_io import durable_append, durable_create, durable_write
from .failures import AUTOMATIC_KINDS, NormalizedFailure, blocked_kind
from .telemetry import NOT_AVAILABLE

SCHEMA_VERSION = 1

#: The one P2 routing policy: wait for the same route, then resume on it.
ROUTE_POLICY_ID = "same-route-wait"
ROUTE_POLICY_VERSION = 1

#: The data class nobody has classified. A stated unknown, as `NOT_AVAILABLE`
#: is for a model; the operator's classes come with P3.
UNSPECIFIED = "UNSPECIFIED"

RECOVERY_DIR = "recovery"
ROUTING_FILE = "routing.jsonl"
MASTER_FILE = "master.json"
CLAIMS_DIR = "claims"

#: What each role needs of a route. P2 has one route, so this is a statement of
#: what the product's own role contracts already demand; P3 matches it against
#: a capability matrix. Fixed, not configured.
CAPABILITY_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    Role.PLANNER.value: ("read_repo", "structured_answer"),
    Role.DEVELOPER.value: ("read_repo", "write_worktree", "run_shell"),
    Role.QA.value: ("read_repo", "run_shell", "structured_answer"),
}


class RecoveryError(ValueError):
    """A recovery record or declaration that is not acceptable."""


# --------------------------------------------------------------------------- #
# The account handle: opaque, operator-registered, never a secret
# --------------------------------------------------------------------------- #

_HANDLE_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/\-]{0,63}")
#: Credential prefixes and words. A handle is a *name* for an account the
#: operator registered; anything that reads like the credential itself is
#: refused, which also refuses a few honest names -- the operator renames.
#: Searched, not matched at the start: `my-sk-ant-api03-...` is a key with a
#: word in front of it. Not inside a word (`task-2`, `disk-1`).
_SECRET_PREFIX = re.compile(
    r"(?<![A-Za-z0-9])(?:sk[-_]|pk[-_]|rk[-_]|hf[-_]|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|"
    r"glpat-|xox[abprs]-|AKIA|ASIA|AIza|ya29\.|eyJ|npm_|pypi-|bearer\b|basic\b)", re.I)
_SECRET_WORD = re.compile(
    r"secret|passw|passphrase|api[-_]?key|token|credential|private[-_]?key", re.I)
#: Twenty or more letters and digits in a row with both kinds present: the shape
#: of a key or a hash, not of a name.
_LONG_RUN = re.compile(r"[A-Za-z0-9]{20,}")
_UUID_SHAPE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def check_account_handle(value: str | None) -> str:
    """The handle to record: `NOT_AVAILABLE` for none, else the value if it can
    only be a name. Raises `RecoveryError` for anything that could be a secret."""
    if value is None or not str(value).strip():
        return NOT_AVAILABLE
    handle = str(value).strip()
    if handle == NOT_AVAILABLE:
        return handle
    if not _HANDLE_OK.fullmatch(handle):
        raise RecoveryError(
            "an account handle is a short name of letters, digits and ._:@/- "
            "(at most 64 characters, no spaces, no '=' or '?'); this one is refused "
            "and is not echoed")
    if "//" in handle or re.search(r":[^/@\s]*@", handle):
        raise RecoveryError("this account handle looks like an address with a login in "
                            "it; register a plain name for the account instead (not echoed)")
    if _SECRET_PREFIX.search(handle) or _SECRET_WORD.search(handle):
        raise RecoveryError("this account handle looks like a credential or names one; "
                            "register a plain name for the account instead (not echoed)")
    for run in _LONG_RUN.findall(handle):
        if re.search(r"\d", run) and re.search(r"[A-Za-z]", run):
            raise RecoveryError("this account handle contains a long letter-and-digit "
                                "run, which is the shape of a key; register a plain "
                                "name for the account instead (not echoed)")
    if (_UUID_SHAPE.search(handle) or re.search(r"\d{12,}", handle)
            or re.search(r"[A-Za-z]{30,}", handle)):
        raise RecoveryError("this account handle has the shape of an identifier or a "
                            "key (a UUID, a long number, a very long word); register a "
                            "plain name for the account instead (not echoed)")
    return handle


def parse_role_accounts(pairs: list[str]) -> dict[Role, str]:
    """`ROLE=HANDLE` pairs (`all=` sets every role, an explicit role after it
    wins) as a role -> handle mapping.

    Every message is fixed text: **nothing the operator typed is echoed**. A
    pasted key with no `ROLE=` (the likeliest mistake) and `<key>=qa` (a key in
    the role slot) are the cases `_per_role`, which echoes its input, would
    have printed.
    """
    roles = {r.value: r for r in Role}
    out: dict[Role, str] = {}
    for pair in pairs:
        key, sep, value = str(pair).partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key != "all" and key not in roles or not value:
            raise RecoveryError(
                "--role-account takes ROLE=HANDLE, where ROLE is all, planner, developer "
                "or qa (the value is not shown)")
        handle = check_account_handle(value)
        if key == "all":
            out = {r: handle for r in Role} | out
        else:
            out[roles[key]] = handle
    return out


# --------------------------------------------------------------------------- #
# Per-dispatch provenance
# --------------------------------------------------------------------------- #

class DispatchProvenance(Strict):
    """Who ran a role, under which account, by which policy, and how it ended."""

    #: `hide_input_in_errors`: a rejected handle is not quoted in the error.
    model_config = ConfigDict(extra="forbid", frozen=False, str_strip_whitespace=True,
                              hide_input_in_errors=True)

    schema_version: int = SCHEMA_VERSION
    #: The harness profile the role ran under (`claude`, `kimi`, ...).
    provider: str
    #: The model identity, or the stated `NOT_AVAILABLE`.
    model: str
    #: The operator-registered handle of the account, or `NOT_AVAILABLE`.
    account_handle: str = NOT_AVAILABLE
    role: str
    route_policy_id: str = ROUTE_POLICY_ID
    route_policy_version: int = ROUTE_POLICY_VERSION
    capability_requirements: list[str]
    data_class: str = UNSPECIFIED
    #: None on success.
    normalized_failure: NormalizedFailure | None = None

    @field_validator("provider", "model", "role", "data_class")
    @classmethod
    def _stated(cls, value: str) -> str:
        if not value:
            raise ValueError("an empty value is not a statement; use NOT_AVAILABLE")
        return value

    @field_validator("account_handle")
    @classmethod
    def _not_a_secret(cls, value: str) -> str:
        return check_account_handle(value)


@dataclass
class RecoveryContext:
    """What a run's caller declares about its accounts and data."""

    accounts: dict[Role, str] = field(default_factory=dict)
    data_class: str = UNSPECIFIED

    def __post_init__(self) -> None:
        self.accounts = {Role(r): check_account_handle(h) for r, h in self.accounts.items()}
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,31}", self.data_class or ""):
            raise RecoveryError("a data class is a short plain word")


def provenance_for(role, *, provider: str, model: str,
                   context: RecoveryContext | None = None,
                   failure: NormalizedFailure | None = None) -> DispatchProvenance | None:
    """The provenance of one dispatch of a dispatch role; None for the
    orchestration steps (`gate`, `merge`, `closure`), which reach no provider."""
    try:
        role_ = role if isinstance(role, Role) else Role(role)
    except ValueError:
        return None
    ctx = context or RecoveryContext()
    return DispatchProvenance(
        provider=provider or NOT_AVAILABLE, model=model or NOT_AVAILABLE,
        account_handle=ctx.accounts.get(role_, NOT_AVAILABLE), role=role_.value,
        capability_requirements=list(CAPABILITY_REQUIREMENTS[role_.value]),
        data_class=ctx.data_class, normalized_failure=failure)


# --------------------------------------------------------------------------- #
# The master of a run
# --------------------------------------------------------------------------- #

class MasterBinding(Strict):
    """Who governs a run: a session at an epoch, or explicitly nobody."""

    kind: Literal["NONE", "GOVERNED"]
    epoch: StrictInt | None = None
    session: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> MasterBinding:
        if self.kind == "NONE":
            if self.epoch is not None or self.session is not None:
                raise ValueError("a run with no master names neither epoch nor session")
        else:
            if (self.session is None or not str(self.session).strip()
                    or self.epoch is None or isinstance(self.epoch, bool) or self.epoch < 1):
                raise ValueError("a governed run names its master's session and a "
                                 "positive epoch, both")
        return self


NO_MASTER = MasterBinding(kind="NONE")


def _recovery_dir(run_dir: Path) -> Path:
    d = Path(run_dir) / RECOVERY_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


_SESSION_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/\-]{0,127}")


def check_session_id(value: str) -> str:
    """A master's session id is a name (a UUID is fine): refused, not echoed, when
    it has other characters, starts like a credential, names one, or has the
    shape of a key (a long mixed letter-and-digit run, a very long word).

    The shape rules are those of `check_account_handle` that a session id can
    keep: a UUID (groups of at most twelve) and a timestamped id such as
    `resume-20261009T090000Z-4242` pass; a digit run alone is allowed here
    because session ids carry timestamps.
    """
    s = str(value).strip()
    refused = (not _SESSION_OK.fullmatch(s) or "//" in s or _SECRET_PREFIX.search(s)
               or _SECRET_WORD.search(s) or re.search(r"[A-Za-z]{30,}", s)
               or any(re.search(r"\d", run) and re.search(r"[A-Za-z]", run)
                      for run in _LONG_RUN.findall(s)))
    if refused:
        raise RecoveryError("--master-session takes a plain session id (letters, digits "
                            "and ._:@/-; the value is not shown)")
    return s


def check_master_args(session: str | None, epoch: int | None) -> MasterBinding | None:
    """The governed binding the arguments name, None for none; `RecoveryError`
    for half of one or a malformed one. Writes nothing."""
    if (session is None) != (epoch is None):
        raise RecoveryError("--master-session and --master-epoch are given together or "
                            "not at all")
    if session is None:
        return None
    check_session_id(session)
    try:
        return MasterBinding(kind="GOVERNED", session=str(session), epoch=epoch)
    except ValueError as exc:
        raise RecoveryError("--master-session needs a non-empty id and --master-epoch a "
                            "positive integer") from exc


def _parked_on_a_provider_wait(run_dir: Path) -> bool:
    """Is the run BLOCKED on a wait of the classes whose parks carry receipts?
    Read from the state file without migrating it; a state that cannot be read
    here is not a parked state as far as a declaration can tell (`hoh run` read
    it before it got this far)."""
    try:
        state = RunState.model_validate_json((run_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return False
    return (state.condition is Condition.BLOCKED
            and state.blocked_kind in AUTOMATIC_KINDS | {"usage_limit"})


def declare_master(run_dir: Path | str, *, session: str | None = None,
                   epoch: int | None = None) -> MasterBinding:
    """Record the master this invocation of `hoh run` declares, and return what
    the run now stands under. The caller holds the run's lock.

    Both `session` and `epoch`: the run is GOVERNED by that master (the record
    is replaced by a master of a higher epoch, or the same one again; a lower
    epoch, or the same epoch under another session, is refused). Neither: the run has no
    master -- recorded as `NONE` when nothing is recorded yet, and an existing
    declaration is **kept** (a bare resume of a governed run must not launder
    its master away). Exactly one of the two is refused.

    **A stale master cannot resume work (P2-43c).** When a project has a node
    for this run, a governed declaration is also asked of that project's epoch
    chain and lease: the declared epoch must be the current one, held by that
    session, under a lease that names it, is not released and has not lapsed
    (`stale_master_reason`). A superseded master, a foreign one and a master that
    gave its lease up are refused with nothing written. With no owning project
    there is nothing to ask and the declaration is recorded as before (it says
    only what was declared).

    **A declaration cannot move the master of a parked run.** While the run is
    BLOCKED on a provider wait, a declaration that would change the master
    recorded under that park is refused, whoever asks: the receipt names the
    master the park was made under, so rewriting the file would make
    `verify_binding` refuse the very park the current master is entitled to
    inherit. The park is lifted first (`hoh resume-quota`, or a person's
    `hoh unblock`); the declaration follows on the unblocked run. Declaring the
    master that is already recorded changes nothing and is accepted.
    """
    run_dir = Path(run_dir)
    governed = check_master_args(session, epoch)
    existing = read_master(run_dir, recorded_only=True)
    if governed is None:
        if existing is not None:
            return existing
        binding = NO_MASTER
    else:
        binding = governed
        if existing is not None and existing.kind == "GOVERNED":
            # Monotone: epochs only grow. A lower epoch is a master that has been
            # superseded; the same epoch under another session is two masters.
            if binding.epoch < existing.epoch:
                raise RecoveryError(
                    f"this run is declared under master epoch {existing.epoch}; "
                    f"epoch {binding.epoch} is older and is refused")
            if binding.epoch == existing.epoch and binding.session != existing.session:
                raise RecoveryError(
                    f"master epoch {existing.epoch} is already declared by another session")
        stale = stale_master_reason(run_dir, binding)
        if stale is not None:
            raise RecoveryError(stale)
        if binding != (existing if existing is not None else NO_MASTER) \
                and _parked_on_a_provider_wait(run_dir):
            raise RecoveryError(
                "this run is parked on a provider wait, and a declaration that changes its "
                "master would strand the park (the receipt names the master it was made "
                "under); lift the park first ('hoh resume-quota', or 'hoh unblock'), then "
                "declare")
    durable_write(_recovery_dir(run_dir) / MASTER_FILE,
                  json.dumps({"declared_at": utcnow(), **binding.model_dump()},
                             indent=2, sort_keys=True) + "\n")
    return binding


def read_master(run_dir: Path | str, *, recorded_only: bool = False) -> MasterBinding | None:
    """The recorded master. A run with no record reads as `NONE`
    (``recorded_only=True`` returns None instead); an unreadable record raises."""
    path = Path(run_dir) / RECOVERY_DIR / MASTER_FILE
    if not path.is_file():
        return None if recorded_only else NO_MASTER
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("declared_at", None)
        return MasterBinding.model_validate(raw)
    except (OSError, ValueError, RecursionError) as exc:
        raise RecoveryError(f"the master record {path} is unreadable: {exc}") from exc


# --------------------------------------------------------------------------- #
# The park binding and the two receipts
# --------------------------------------------------------------------------- #

class ParkBinding(Strict):
    """Exactly the work a park belongs to."""

    #: The pinned contract's id, or `NONE` for a run without one.
    programme: str
    program_epoch: int
    run_id: str
    iteration: int
    attempt: int
    stage: str
    #: The candidate the work stands on (`Candidate.binding()`: commit, tree
    #: digest, clean/dirty), or `NONE` where no candidate exists yet.
    candidate: str
    master: MasterBinding


def candidate_binding(state) -> str:
    """The candidate a state stands on: working, else last accepted, else base."""
    for cand in (state.working_candidate, state.last_accepted_candidate,
                 state.base_candidate):
        if cand is not None:
            return cand.binding()
    return "NONE"


def park_binding(state, *, governor, master: MasterBinding) -> ParkBinding:
    return ParkBinding(
        programme=(governor.contract.contract_id if governor else "NONE"),
        program_epoch=(int(governor.contract.program_epoch) if governor else 0),
        run_id=state.run_id, iteration=state.iteration, attempt=state.attempt,
        stage=state.stage.value, candidate=candidate_binding(state), master=master)


def park_id(binding: ParkBinding, failure: NormalizedFailure, retry_at: str | None) -> str:
    """A digest of the binding, the class and `retry_at`: recomputable from the
    live state, so a different park cannot pass for this one."""
    canon = json.dumps({"binding": binding.model_dump(), "failure": failure.value,
                        "retry_at": retry_at or ""}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]


class _Receipt(Strict):
    schema_version: int = SCHEMA_VERSION
    park_id: str
    at: str = Field(default_factory=utcnow)
    reason: str
    route_policy_id: str = ROUTE_POLICY_ID
    route_policy_version: int = ROUTE_POLICY_VERSION
    binding: ParkBinding
    provenance: DispatchProvenance


class RoutingDecision(_Receipt):
    """The run waits for the same route and resumes on it."""

    kind: Literal["ROUTING_DECISION"] = "ROUTING_DECISION"
    decision: Literal["WAIT_SAME_ROUTE"] = "WAIT_SAME_ROUTE"
    retry_at: str


class RoutingEscalation(_Receipt):
    """The run needs a person; no timer will wake it."""

    kind: Literal["ROUTING_ESCALATION"] = "ROUTING_ESCALATION"
    #: `RETRIES_EXHAUSTED` (slice c): the resume loop gave the park up after
    #: `MAX_RESUME_ATTEMPTS` unsuccessful resumes; only a person lifts it now.
    escalation: Literal["OPERATOR_REQUIRED", "HELD_BY_VIOLATION", "RETRIES_EXHAUSTED"]


def _digest_of(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def append_record(run_dir: Path | str, record: RoutingDecision | RoutingEscalation) -> None:
    """Append one digest-bound line, durably, or raise. The caller parks only
    after this returns."""
    body = record.model_dump(mode="json")
    line = json.dumps({"digest": _digest_of(body), **body}, sort_keys=True)
    durable_append(_recovery_dir(Path(run_dir)) / ROUTING_FILE, line, single_link=True)


def read_records(run_dir: Path | str
                 ) -> tuple[list[RoutingDecision | RoutingEscalation], list[str]]:
    """(valid records in order, problems). A line that does not parse, does not
    validate or whose digest does not match its content is a problem, not a record."""
    path = Path(run_dir) / RECOVERY_DIR / ROUTING_FILE
    records: list[RoutingDecision | RoutingEscalation] = []
    problems: list[str] = []
    if not path.is_file():
        return records, problems
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                problems.append(f"line {n}: not a JSON object")
                continue
            claimed = raw.pop("digest")
            if claimed != _digest_of(raw):
                problems.append(f"line {n}: digest mismatch")
                continue
            cls = {"ROUTING_DECISION": RoutingDecision,
                   "ROUTING_ESCALATION": RoutingEscalation}[raw["kind"]]
            records.append(cls.model_validate(raw))
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            # RecursionError: a deeply nested line (a hand-written file) is a
            # problem, not a crash of the resume pass.
            problems.append(f"line {n}: {type(exc).__name__}: {str(exc)[:120]}")
    return records, problems


# --------------------------------------------------------------------------- #
# Who holds the work (P2-43c): the owning project's epoch chain and lease
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MasterStanding:
    """What the epoch chain and the lease of the project that owns a run say
    about a declared master. Read-only: nothing is acquired, written or renewed."""

    #: The project with a node for this run, or None (no project, or several).
    owner: str | None
    #: Why the standing cannot be read ("" when it can).
    problem: str = ""
    #: The declared (session, epoch) is a row of the project's epoch chain.
    held: bool = False
    current_epoch: int | None = None
    #: `MasterAuthority.holder_state`: NONE, RELEASED, LOST or LIVE.
    lease: str = "NONE"
    lease_session: str | None = None
    lease_epoch: int | None = None


def master_standing(run_dir: Path | str, master: MasterBinding, *,
                    now: str | None = None) -> MasterStanding:
    """Ask the project that owns `run_dir` whether `master` is real.

    The owner is the project whose node runs this run (`node.run_id or
    node.id`, the launcher's own rule) among the projects under the run's
    root. Its epoch chain says who held which epoch and which epoch is
    current; its lease says whether anyone holds it now. A project state that
    cannot be read is not an owner (a damaged neighbour must not stop the
    masters of the others).
    """
    from .projectstore import ProjectStore, list_projects
    from .succession import MasterAuthority, utc_now

    run_dir = Path(run_dir)
    run_id, root = run_dir.name, run_dir.parent
    owners: list[str] = []
    for pid in list_projects(root):
        try:
            project = ProjectStore(root, pid).read_state()
        except Exception:                                # noqa: BLE001 - not an owner
            continue
        if any((n.run_id or n.id) == run_id for n in project.nodes):
            owners.append(pid)
    if len(owners) > 1:
        return MasterStanding(owner=None, problem="more than one project has a node "
                                                  "for this run, so which one governs it "
                                                  "is not known")
    if not owners:
        return MasterStanding(owner=None)
    project_dir = ProjectStore(root, owners[0]).dir
    authority = MasterAuthority(project_dir, "park-verifier")
    try:
        chain = authority.epochs.chain()
        lease = authority.read_lease()
        lease_state, _ = authority.holder_state(lease, now or utc_now())
    except Exception as exc:                             # noqa: BLE001 - said, fail closed
        return MasterStanding(owner=owners[0], problem="the project's epoch chain or "
                              f"lease cannot be read ({type(exc).__name__})")
    held = any(e.epoch == master.epoch and e.master_id == master.session for e in chain)
    return MasterStanding(
        owner=owners[0], held=held, current_epoch=chain[-1].epoch if chain else None,
        lease=lease_state, lease_session=lease.leader_session_id if lease else None,
        lease_epoch=lease.master_epoch if lease else None)


def stale_master_reason(run_dir: Path | str, master: MasterBinding, *,
                        now: str | None = None) -> str | None:
    """Why `master` may not launch work on this run, or None.

    None for a run with no master and for a governed master of a run that no
    project has a node for (nothing to ask). Otherwise the master must be the
    current epoch of its project, held by that session, under a lease that
    names it and is live. Fixed text; no session id is echoed.
    """
    if master.kind != "GOVERNED":
        return None
    s = master_standing(run_dir, master, now=now)
    if s.problem:
        return f"the declared master cannot be confirmed: {s.problem}"
    if s.owner is None:
        return None
    if not s.held:
        return (f"epoch {master.epoch} of project {s.owner} was never held by this "
                f"session; a master the epoch chain does not know cannot resume work")
    if s.current_epoch != master.epoch:
        return (f"stale master: epoch {master.epoch} was superseded, project {s.owner} "
                f"is at epoch {s.current_epoch}; only the current master resumes work")
    if s.lease != "LIVE":
        return (f"stale master: the lease of project {s.owner} is {s.lease}, not live; "
                f"a master that gave its lease up or lost it acquires again first")
    if s.lease_session != master.session or s.lease_epoch != master.epoch:
        return (f"the lease of project {s.owner} names another master at epoch "
                f"{s.lease_epoch}, not the declared one")
    return None


# --------------------------------------------------------------------------- #
# Verifying a park before it is resumed (P2-43c)
# --------------------------------------------------------------------------- #

class ParkVerdict(StrEnum):
    #: A receipt describes this park and the live state still matches it.
    BOUND = "BOUND"
    #: No receipt, no declared master: a park of slice a, resumed as it always was.
    LEGACY_UNBOUND = "LEGACY_UNBOUND"
    #: A live master holds the work; not now, not by a consumer.
    DEFERRED = "DEFERRED"
    #: Stale, foreign, altered, already resumed or unprovable: not resumed.
    REFUSED = "REFUSED"


@dataclass(frozen=True)
class ParkCheck:
    """The outcome of `verify_binding`."""

    verdict: ParkVerdict
    reason: str
    park_id: str | None = None
    record: RoutingDecision | None = None
    master: MasterBinding = field(default_factory=lambda: NO_MASTER)

    @property
    def resumes(self) -> bool:
        return self.verdict in (ParkVerdict.BOUND, ParkVerdict.LEGACY_UNBOUND)


def claim_path(run_dir: Path | str, park: str) -> Path:
    return Path(run_dir) / RECOVERY_DIR / CLAIMS_DIR / f"{park}.json"


def read_claim(run_dir: Path | str, park: str) -> dict | None:
    """The receipt of a resume of this park, or None."""
    path = claim_path(run_dir, park)
    if not os.path.lexists(path):
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {"unreadable": True}
    return raw if isinstance(raw, dict) else {"unreadable": True}


def claim_park(run_dir: Path | str, check: ParkCheck, *, resumer: str, attempt: int) -> Path:
    """Write the receipt that this park was lifted: one file per park, never
    replaced (`FileExistsError` when the park was lifted before). Written
    **after** the lift, under the run's lock: a claim taken before the lift
    would strand the park when the resumer dies between the two, which the
    crash invariant of `hoh.resumeloop` forbids. Concurrency is the run lock's
    job; this file is what survives a restored state."""
    if check.record is None:
        raise ValueError("only a park that a receipt describes can be claimed")
    body = {"schema_version": SCHEMA_VERSION, "kind": "PARK_RESUMED",
            "park_id": check.record.park_id, "resumed_at": utcnow(),
            "resumer": resumer, "pid": os.getpid(), "resume_attempt": int(attempt),
            "master": check.master.model_dump()}
    path = claim_path(run_dir, check.record.park_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    durable_create(path, json.dumps(body, indent=2, sort_keys=True) + "\n")
    return path


def escalate_exhaustion(run_dir: Path | str, check: ParkCheck) -> RoutingEscalation | None:
    """Append the `RETRIES_EXHAUSTED` escalation of a park the resume loop gave
    up, once per park. None for a park no receipt describes (nothing to extend).
    Raises what `append_record` raises."""
    rec = check.record
    if rec is None:
        return None
    records, _ = read_records(run_dir)
    for earlier in records:
        if (isinstance(earlier, RoutingEscalation) and earlier.park_id == rec.park_id
                and earlier.escalation == "RETRIES_EXHAUSTED"):
            return earlier
    esc = RoutingEscalation(park_id=rec.park_id, reason=rec.reason, binding=rec.binding,
                            provenance=rec.provenance, escalation="RETRIES_EXHAUSTED")
    append_record(run_dir, esc)
    return esc


def _refused(reason: str) -> ParkCheck:
    return ParkCheck(ParkVerdict.REFUSED, reason)


def _mismatch(rec: RoutingDecision, live: ParkBinding, state) -> str | None:
    """Why `rec` does not describe the live park, or None when it does."""
    try:
        failure = NormalizedFailure(rec.reason)
        kind = blocked_kind(failure)
    except ValueError:
        return "the receipt names a class that is not an automatic wait"
    if state.blocked_kind != kind:
        return (f"the state is parked as {state.blocked_kind!r}, the receipt is for "
                f"{failure.value} ({kind!r})")
    if rec.binding != live:
        moved = [f"{name} (receipt {getattr(rec.binding, name)!r}, state "
                 f"{getattr(live, name)!r})"
                 for name in ParkBinding.model_fields
                 if getattr(rec.binding, name) != getattr(live, name)]
        return "changed since the park: " + "; ".join(moved)[:300]
    if rec.park_id != park_id(live, failure, state.retry_after):
        return "the receipt's park id is not the one its own binding and class give"
    return None


def _without_receipt(master: MasterBinding, decisions: list, problems: list[str],
                     wanted: str | None) -> ParkCheck:
    if master.kind == "GOVERNED":
        return _refused("the run declares a master but no valid receipt describes this "
                        f"park (retry_after {wanted}); a governed park without its "
                        "receipt is not resumed on a guess")
    if problems:
        return _refused(f"the routing receipts hold {len(problems)} unreadable line(s) "
                        f"and none describes this park (retry_after {wanted})")
    if decisions:
        return _refused(f"the run's receipts describe {len(decisions)} other wait(s), "
                        f"none with retry_after {wanted}: the state is not a park this "
                        "run made")
    return ParkCheck(ParkVerdict.LEGACY_UNBOUND,
                     "no routing receipt and no declared master: a park of slice a, "
                     "resumed as before", master=master)


def verify_binding(run_dir: Path | str, state, *, now: str | None = None) -> ParkCheck:
    """May this park be resumed? Read-only; fail closed.

    The park is the blocked `state` of the run in `run_dir`. Its receipt is the
    newest valid `RoutingDecision` whose `retry_at` is the state's
    `retry_after`. The park's id is **recomputed** from the live state, the
    pinned contract and the declared master, never taken from the receipt: a
    receipt is a digest-bound line, which is integrity, not authenticity (a
    self-consistent forgery validates), so what makes a receipt worth anything
    is that the state still says the same thing. Then, for a governed park,
    the owning project's epoch chain and lease (`master_standing`).

    * the state names another run than the directory it is in (a restored or
      renamed copy keeps its `run_id`): REFUSED; a park is resumed in the run
      that made it;
    * no receipt describes the park: a governed run, an unreadable receipts
      file, or receipts for other waits only are REFUSED; a bare run that never
      had a receipt is LEGACY_UNBOUND;
    * the receipt differs in run, programme, epoch, iteration, attempt, stage,
      candidate or master, or the class differs from the state's: REFUSED;
    * the park was lifted before (`read_claim`): REFUSED;
    * the declared master was never held by its project (a foreign master), or
      the run is governed and no project has a node for it: REFUSED;
    * a live master holds the project: DEFERRED (also if it is a successor);
    * otherwise BOUND, with a note when the declared master was superseded (its
      successor inherits the park).
    """
    try:
        return _verify(Path(run_dir), state, now)
    except Exception as exc:                             # noqa: BLE001 - fail closed
        return _refused(f"the park could not be verified ({type(exc).__name__}); "
                        "nothing is resumed on a guess")


def _verify(run_dir: Path, state, now: str | None) -> ParkCheck:
    from .governance import ContractRefused, bind

    here = run_dir.resolve().name
    if str(state.run_id) != here:
        return _refused(f"the state says run {state.run_id} but this directory is {here}: "
                        "a copy of a run (a restored one, a renamed one) is not the run, "
                        "and a park is resumed in the run that made it")
    try:
        master = read_master(run_dir)
        assert master is not None
    except (RecoveryError, AttributeError, TypeError) as exc:
        return _refused(f"the master record cannot be read: {str(exc)[:120]}")
    try:
        governor = bind(run_dir)
    except ContractRefused as exc:
        return _refused(f"the pinned contract cannot be read: {str(exc)[:120]}")
    records, problems = read_records(run_dir)
    decisions = [r for r in records if isinstance(r, RoutingDecision)]
    wanted = state.retry_after
    matching = [r for r in decisions if wanted and r.retry_at == wanted]
    if not matching:
        return _without_receipt(master, decisions, problems, wanted)
    live = park_binding(state, governor=governor, master=master)
    chosen, newest_why = None, None
    for rec in reversed(matching):
        why = _mismatch(rec, live, state)
        if why is None:
            chosen = rec
            break
        newest_why = newest_why or why
    if chosen is None:
        return _refused(f"stale or foreign park: {newest_why}")
    earlier = read_claim(run_dir, chosen.park_id)
    if earlier is not None:
        return ParkCheck(ParkVerdict.REFUSED,
                         f"this park was already resumed ({earlier.get('resumed_at', '?')} "
                         f"by {earlier.get('resumer', '?')}); one park is lifted once, "
                         "and a person decides about a second time (hoh unblock)",
                         chosen.park_id, chosen, master)
    note = "the receipt matches the live state"
    if master.kind == "GOVERNED":
        s = master_standing(run_dir, master, now=now)
        if s.problem:
            return _refused(f"the declared master cannot be confirmed: {s.problem}")
        if s.owner is None:
            return _refused("the run declares a master but no project has a node for it, "
                            "so no epoch chain or lease can confirm that master; a person "
                            "decides (hoh unblock)")
        if not s.held:
            return _refused(f"foreign master: epoch {master.epoch} of project {s.owner} "
                            "was never held by the session the park names")
        if s.lease == "LIVE":
            return ParkCheck(ParkVerdict.DEFERRED,
                             f"a live master holds project {s.owner} (epoch {s.lease_epoch}); "
                             "the work is its, and a consumer does not resume it",
                             chosen.park_id, chosen, master)
        if s.current_epoch != master.epoch:
            note = (f"the park's master (epoch {master.epoch}) was superseded by epoch "
                    f"{s.current_epoch}; the current master inherits the park")
    return ParkCheck(ParkVerdict.BOUND, note, chosen.park_id, chosen, master)
