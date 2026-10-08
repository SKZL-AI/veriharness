"""The MCP tool gateway, safety half (V3.3 P2-18).

An agent that can reach arbitrary external tools has whatever authority those
tools have. The answer here is one gateway every tool call passes through,
classified by what the call would *do* (`ToolEffect`) rather than by what the
tool is named, with every decision and every call recorded.

There is no transport. The gateway is handed a description of a tool
(`ToolDescriptor`) and a call, and decides. The caller supplies the function
that actually executes the call; tests supply fakes. Nothing in this module
opens a socket, starts a process or imports a network library.

**The single-path property (MCP7).** `Gateway.call` is the only place in this
module where a caller's `execute` function is invoked, and no other function
here runs a tool. A UI, a runner or a helper that wanted to skip the policy
decision would need a second invocation path, and there is none to use.
`tests/test_gateway.py` defends exactly this property by walking this module's
AST and failing on any other place `execute` is invoked or accepted.

Failure classification is structural, never a string match: the fakes and
real adapters raise `ToolAuthError`, `ToolTransportError` or
`ToolContentError` (or the builtin `PermissionError` / `ConnectionError` /
`TimeoutError` they derive from), and the gateway maps exception *types* to
`Outcome`s. An auth failure is never retried; a transport failure is retried
up to the policy's limit; anything else that goes wrong while executing is a
content failure, never an `OK`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field, field_validator

from hoh.contracts import Strict, utcnow


# --------------------------------------------------------------------------- #
# Classifications
# --------------------------------------------------------------------------- #


class ToolEffect(StrEnum):
    """What a call would do -- the only thing a safety decision may rest on.

    `UNKNOWN` is a real classification, not a placeholder, and it is the one
    that matters: it is what a tool gets when nobody declared its effect. A
    tool of unknown effect is treated as at least as dangerous as
    `IRREVERSIBLE`, never as `READ_ONLY`. No policy may allow it outright
    (`ToolPolicy.coherence` reports that), and a policy that lists it nowhere
    denies it unless an explicit approval names the tool.
    """

    READ_ONLY = "READ_ONLY"
    REVERSIBLE_WRITE = "REVERSIBLE_WRITE"
    IRREVERSIBLE = "IRREVERSIBLE"
    EXTERNAL_PUBLISH = "EXTERNAL_PUBLISH"
    UNKNOWN = "UNKNOWN"


class Outcome(StrEnum):
    """Why a call ended.

    Auth, transport and content failures are distinct members so that MCP4 is
    structural: "you may not" (AUTH_FAILURE) is never retried, "ask again"
    (TRANSPORT_FAILURE) is retried within the policy's limit, and "the answer
    is bad" (CONTENT_FAILURE) is neither retried nor accepted.
    """

    OK = "OK"
    DENIED_BY_POLICY = "DENIED_BY_POLICY"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    AUTH_FAILURE = "AUTH_FAILURE"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
    CONTENT_FAILURE = "CONTENT_FAILURE"
    SCHEMA_STALE = "SCHEMA_STALE"


#: Effects that no default may allow; they reach OK only through an approval.
DANGEROUS_EFFECTS = frozenset({ToolEffect.IRREVERSIBLE, ToolEffect.UNKNOWN})


# --------------------------------------------------------------------------- #
# Structured failure signals raised by `execute`
# --------------------------------------------------------------------------- #


class ToolAuthError(PermissionError):
    """The tool refused the caller: "you may not". Never retried."""


class ToolTransportError(ConnectionError):
    """The call did not get through: "ask again". Retried within the policy."""


class ToolContentError(ValueError):
    """The call got through and the answer is unusable. Not retried."""


def _classify_exception(exc: Exception) -> Outcome:
    """Map an exception *type* to an outcome. No message is ever inspected."""
    if isinstance(exc, PermissionError):
        return Outcome.AUTH_FAILURE
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return Outcome.TRANSPORT_FAILURE
    return Outcome.CONTENT_FAILURE


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


def _require_text(value: str, name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


class ToolDescriptor(Strict):
    """One tool as a server describes it.

    `effect` defaults to `UNKNOWN`: a descriptor that does not declare what the
    tool does is not thereby read-only. An empty `schema_digest` is refused,
    because MCP3 is only checkable if the schema a decision was made against
    can be named.
    """

    model_config = ConfigDict(frozen=True)

    tool_id: str
    server_id: str
    name: str
    effect: ToolEffect = ToolEffect.UNKNOWN
    schema_digest: str
    description: str = ""

    @field_validator("tool_id", "server_id", "name", "schema_digest")
    @classmethod
    def _not_blank(cls, value: str, info) -> str:
        return _require_text(value, info.field_name)


class ToolPolicy(Strict):
    """What is allowed without asking, what needs an approval, what is denied.

    An effect that appears in none of the three sets is never allowed: it
    needs an approval, and `UNKNOWN` is denied unless an approval names the
    tool.
    """

    model_config = ConfigDict(frozen=True)

    allowed_effects: frozenset[ToolEffect] = frozenset()
    approval_required_effects: frozenset[ToolEffect] = frozenset()
    denied_effects: frozenset[ToolEffect] = frozenset()
    max_transport_retries: int = Field(default=0, ge=0)
    policy_version: str

    @field_validator("policy_version")
    @classmethod
    def _version_not_blank(cls, value: str) -> str:
        return _require_text(value, "policy_version")

    def coherence(self) -> list[str]:
        """The reasons this policy is incoherent, or `[]`."""
        sets = (
            ("allowed_effects", self.allowed_effects),
            ("approval_required_effects", self.approval_required_effects),
            ("denied_effects", self.denied_effects),
        )
        reasons: list[str] = []
        for i, (name_a, set_a) in enumerate(sets):
            for name_b, set_b in sets[i + 1:]:
                for effect in sorted(set_a & set_b):
                    reasons.append(
                        f"{effect.value} is listed in both {name_a} and {name_b}")
        for effect in sorted(DANGEROUS_EFFECTS & self.allowed_effects):
            reasons.append(
                f"allowed_effects contains {effect.value}; allowing it must be "
                f"written as an explicit approval, not as a default")
        return reasons


#: The default: read-only tools run, everything else waits for an approval.
PROVIDER_READ_ONLY = ToolPolicy(
    allowed_effects=frozenset({ToolEffect.READ_ONLY}),
    approval_required_effects=frozenset({
        ToolEffect.REVERSIBLE_WRITE, ToolEffect.IRREVERSIBLE,
        ToolEffect.EXTERNAL_PUBLISH, ToolEffect.UNKNOWN}),
    denied_effects=frozenset(),
    max_transport_retries=2,
    policy_version="provider-read-only-v1",
)


class Approval(Strict):
    """An explicit approval of one tool at one schema.

    An approval granted against another `schema_digest` does not carry over:
    the tool it approved is not the tool that is registered now.
    """

    model_config = ConfigDict(frozen=True)

    tool_id: str
    schema_digest: str
    approved_by: str = ""


class Decision(Strict):
    """A policy decision and what it was made against."""

    tool_id: str
    outcome: Outcome
    reason: str
    schema_digest: str | None
    policy_version: str
    cached: bool = False
    decided_at: str


class PolicyMapping(Strict):
    """The cached policy-derived decision for one tool under one policy.

    It holds the decision *before* approvals are applied, and it is only as
    good as the schema and policy it was made against: `is_valid_for` is False
    as soon as either changed (MCP3).
    """

    tool_id: str
    schema_digest: str
    policy_version: str
    decision: Outcome
    mapped_at: str

    def is_valid_for(self, descriptor: ToolDescriptor, policy: ToolPolicy) -> bool:
        return (self.tool_id == descriptor.tool_id
                and self.schema_digest == descriptor.schema_digest
                and self.policy_version == policy.policy_version)


class Provenance(Strict):
    """Where a call's content came from."""

    server_id: str | None
    tool_id: str


class CallRecord(Strict):
    """What happened to one call. Appended to `Gateway.records`."""

    tool_id: str
    outcome: Outcome
    executed: bool
    attempts: int
    reason: str
    schema_digest: str | None
    policy_version: str
    content_digest: str | None
    provenance: Provenance
    recorded_at: str


# --------------------------------------------------------------------------- #
# Digest
# --------------------------------------------------------------------------- #


def content_digest(content: Any) -> str:
    """sha256 over a canonical serialisation of `content`.

    bytes are hashed as they are, str as UTF-8, everything else as JSON with
    sorted keys, no whitespace and no NaN/Infinity. Content that has no such
    serialisation raises `ToolContentError`.
    """
    if isinstance(content, (bytes, bytearray)):
        data = bytes(content)
    elif isinstance(content, str):
        data = content.encode("utf-8")
    else:
        try:
            data = json.dumps(content, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise ToolContentError(
                f"content of type {type(content).__name__} has no canonical "
                f"serialisation") from exc
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# The gateway
# --------------------------------------------------------------------------- #


class Gateway:
    """Every tool call passes through here; there is no other path (MCP7)."""

    def __init__(self, policy: ToolPolicy,
                 registry: Iterable[ToolDescriptor] | Mapping[str, ToolDescriptor]):
        self.policy = self._checked(policy)
        self.registry: dict[str, ToolDescriptor] = {}
        descriptors = registry.values() if isinstance(registry, Mapping) else registry
        for descriptor in descriptors:
            if descriptor.tool_id in self.registry:
                raise ValueError(f"tool_id {descriptor.tool_id!r} registered twice")
            self.registry[descriptor.tool_id] = descriptor
        self.mappings: dict[str, PolicyMapping] = {}
        self.decisions: list[Decision] = []
        self.records: list[CallRecord] = []

    @staticmethod
    def _checked(policy: ToolPolicy) -> ToolPolicy:
        reasons = policy.coherence()
        if reasons:
            raise ValueError("incoherent policy: " + "; ".join(reasons))
        return policy

    def register(self, descriptor: ToolDescriptor) -> None:
        """Add a tool, or replace it -- a new schema invalidates its mapping."""
        self.registry[descriptor.tool_id] = descriptor

    def set_policy(self, policy: ToolPolicy) -> None:
        """Replace the policy; mappings made under the old version go stale."""
        self.policy = self._checked(policy)

    # -- deciding ----------------------------------------------------------- #

    def _policy_outcome(self, effect: ToolEffect) -> tuple[Outcome, str]:
        p = self.policy
        if effect in p.denied_effects:
            return Outcome.DENIED_BY_POLICY, f"{effect.value} is denied by policy"
        if effect in p.approval_required_effects:
            return Outcome.NEEDS_APPROVAL, f"{effect.value} requires an approval"
        if effect in p.allowed_effects and effect not in DANGEROUS_EFFECTS:
            return Outcome.OK, f"{effect.value} is allowed by policy"
        if effect is ToolEffect.UNKNOWN:
            return (Outcome.DENIED_BY_POLICY,
                    "UNKNOWN effect is listed in no policy set and is never "
                    "allowed without an approval")
        return (Outcome.NEEDS_APPROVAL,
                f"{effect.value} is listed in no policy set; an unlisted effect "
                f"is never allowed without an approval")

    def _mapping(self, descriptor: ToolDescriptor, now: str) -> tuple[PolicyMapping, bool, str]:
        """The valid mapping for `descriptor`; a stale one is re-decided, never served."""
        cached = self.mappings.get(descriptor.tool_id)
        if cached is not None and cached.is_valid_for(descriptor, self.policy):
            return cached, True, ""
        note = ""
        if cached is not None:
            note = (f" (re-decided: cached mapping was made against schema "
                    f"{cached.schema_digest} under policy {cached.policy_version})")
        outcome, _ = self._policy_outcome(descriptor.effect)
        mapping = PolicyMapping(
            tool_id=descriptor.tool_id, schema_digest=descriptor.schema_digest,
            policy_version=self.policy.policy_version, decision=outcome,
            mapped_at=now)
        self.mappings[descriptor.tool_id] = mapping
        return mapping, False, note

    def decide(self, tool_id: str, *, approvals: Iterable[Approval | str] = (),
               now: str | None = None) -> Decision:
        """Decide one call and log the decision. Never OK for an unknown tool.

        An approval matches when it is an `Approval` naming this tool at its
        current `schema_digest`, or the bare `tool_id` string. An `Approval`
        for this tool made against another schema yields SCHEMA_STALE.
        """
        now = now or utcnow()
        descriptor = self.registry.get(tool_id)
        if descriptor is None:
            decision = Decision(
                tool_id=tool_id, outcome=Outcome.DENIED_BY_POLICY,
                reason=f"tool {tool_id!r} is not registered; an unknown tool is "
                       f"not an implicit allow",
                schema_digest=None, policy_version=self.policy.policy_version,
                decided_at=now)
            self.decisions.append(decision)
            return decision

        mapping, cached, note = self._mapping(descriptor, now)
        _, reason = self._policy_outcome(descriptor.effect)
        outcome = mapping.decision
        # An approval can lift NEEDS_APPROVAL, and the default denial of an
        # unlisted UNKNOWN. It never lifts an effect the policy denies outright.
        approvable = outcome is Outcome.NEEDS_APPROVAL or (
            outcome is Outcome.DENIED_BY_POLICY
            and descriptor.effect not in self.policy.denied_effects)
        if approvable:
            outcome, reason = self._apply_approvals(descriptor, outcome, reason, approvals)
        decision = Decision(
            tool_id=tool_id, outcome=outcome, reason=reason + note,
            schema_digest=descriptor.schema_digest,
            policy_version=self.policy.policy_version, cached=cached,
            decided_at=now)
        self.decisions.append(decision)
        return decision

    @staticmethod
    def _apply_approvals(descriptor: ToolDescriptor, outcome: Outcome, reason: str,
                         approvals: Iterable[Approval | str]) -> tuple[Outcome, str]:
        stale: Approval | None = None
        for approval in approvals:
            if isinstance(approval, str):
                if approval == descriptor.tool_id:
                    return Outcome.OK, f"{reason}; approved for {descriptor.tool_id}"
            elif isinstance(approval, Approval) and approval.tool_id == descriptor.tool_id:
                if approval.schema_digest == descriptor.schema_digest:
                    by = f" by {approval.approved_by}" if approval.approved_by else ""
                    return Outcome.OK, f"{reason}; approved{by} at schema {approval.schema_digest}"
                stale = approval
        if stale is not None:
            return (Outcome.SCHEMA_STALE,
                    f"{reason}; the approval was granted against schema "
                    f"{stale.schema_digest}, the tool is now at "
                    f"{descriptor.schema_digest}")
        return outcome, reason

    # -- the single execution path ----------------------------------------- #

    def call(self, tool_id: str, *, arguments: Any,
             execute: Callable[[Any], Any],
             approvals: Iterable[Approval | str] = (), now: str) -> CallRecord:
        """Decide, then -- only on OK -- run `execute(arguments)` and record it.

        This is the only place in the module that invokes `execute`; see the
        module docstring for the property `tests/test_gateway.py` defends.
        """
        decision = self.decide(tool_id, approvals=approvals, now=now)
        descriptor = self.registry.get(tool_id)
        provenance = Provenance(
            server_id=descriptor.server_id if descriptor else None, tool_id=tool_id)

        def record(outcome: Outcome, attempts: int, reason: str,
                   digest: str | None = None) -> CallRecord:
            rec = CallRecord(
                tool_id=tool_id, outcome=outcome, executed=attempts > 0,
                attempts=attempts, reason=reason,
                schema_digest=decision.schema_digest,
                policy_version=decision.policy_version, content_digest=digest,
                provenance=provenance, recorded_at=now)
            self.records.append(rec)
            return rec

        if decision.outcome is not Outcome.OK:
            return record(decision.outcome, 0,
                          f"not executed: {decision.reason}")

        limit = 1 + self.policy.max_transport_retries
        attempts = 0
        while True:
            attempts += 1
            try:
                content = execute(arguments)
            except Exception as exc:  # classified by type, never by message
                outcome = _classify_exception(exc)
                if outcome is Outcome.TRANSPORT_FAILURE and attempts < limit:
                    continue
                reason = f"{type(exc).__name__} on attempt {attempts}"
                if outcome is Outcome.TRANSPORT_FAILURE:
                    reason += f"; retry limit {self.policy.max_transport_retries} reached"
                return record(outcome, attempts, reason)
            try:
                digest = content_digest(content)
            except ToolContentError as exc:
                return record(Outcome.CONTENT_FAILURE, attempts, str(exc))
            return record(Outcome.OK, attempts, decision.reason, digest)
