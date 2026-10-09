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

This slice **writes** the binding. Enforcing it on resume (a stale master, two
masters, a foreign park) is the next slice; nothing here refuses a resume.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, StrictInt, field_validator, model_validator

from .contracts import Role, Strict, utcnow
from .durable_io import durable_append, durable_write
from .failures import NormalizedFailure
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
    it has other characters or starts like a credential."""
    s = str(value).strip()
    if not _SESSION_OK.fullmatch(s) or "//" in s or _SECRET_PREFIX.search(s):
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
    except (OSError, ValueError) as exc:
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
    escalation: Literal["OPERATOR_REQUIRED", "HELD_BY_VIOLATION"]


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
        except (ValueError, KeyError, TypeError) as exc:
            problems.append(f"line {n}: {type(exc).__name__}: {str(exc)[:120]}")
    return records, problems
