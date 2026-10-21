"""What a programme is allowed to decide on its own, written down before it runs.

V3.3 P2-22, P2-23, P2-24. The plan's demand is a product requirement, not a
convenience:

    VeriHarness must support Programs that run for weeks or months without
    assuming continuous human attention. HITL is a capability, not a mandatory
    execution style.

The failure this module exists to prevent is not "the programme stopped". It is
the programme that *did not* stop -- one that met a decision nobody had
authorised it to take, took it anyway, and left no record that a boundary had
been crossed. So the contract is authored before the run, pinned to the
programme epoch by digest, and every routing decision it produces names the
rule it came from.

Three things live here:

* **`AutonomyContract` (P2-22).** The authoritative object: mode, scope,
  horizon, budgets, which action classes are allowed at all, the provider,
  runtime, network, browser, MCP, decision and release policies, how
  Needs-You is handled, the escalation strategy per class, and the kill
  switch. Its digest is what binds a run to the contract it ran under.
* **`compile_profile()` (P2-23).** One of the four operating profiles into a
  concrete contract. The compiler *proposes* and validation refuses: a profile
  is a starting point a human or a plan may narrow, never a way to acquire
  authority the profile does not carry.
* **`route()` (P2-24).** What happens when the programme meets a decision it
  cannot take: queue it, collect it for a digest, park that branch and carry
  on with independent work, or stop. It never fabricates an approval, and a
  decision that is genuinely non-delegable in an unattended programme parks
  with its reason recorded rather than being waved through.

What this module deliberately does not do: execute. It answers "may this, and
if not, what instead", and the controller acts. A policy object that could also
act would be a second controller with no lock discipline of its own -- the same
separation `hoh.sentinel` keeps for timers.
"""
from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import Field, model_validator

from .contracts import Budgets, Strict, utcnow
from .project import ActionClass

SCHEMA = "hoh-autonomy-contract/1"

#: The decision kinds `route()` understands. Escalation keys, hard gates and
#: routing calls are all checked against this one set: a kind nobody recognises
#: used to resolve itself by policy, which made a caller's typo an authority.
KNOWN_KINDS = frozenset({ActionClass.INTERNAL.value, ActionClass.EXTERNAL.value,
                         "decision", "release", "scope"})

#: Channels a kill switch may name. The plan requires the switch to be reachable
#: from the CLI, the UI and the service API and not to depend on a provider
#: being healthy; only the CLI exists today, so the others are listed but a
#: contract that names one is describing something this product does not have
#: yet -- which is why `cli` is required rather than merely allowed.
KILL_CHANNELS = frozenset({"cli", "ui", "service-api"})

class AutonomyMode(StrEnum):
    """The four profiles of the plan, in the order of increasing autonomy."""

    #: The captain steers actively: more human gates, conservative automatics.
    SUPERVISED = "SUPERVISED"
    #: The default: routine work autonomous, high-risk decisions asked.
    POLICY_GOVERNED = "POLICY_GOVERNED"
    #: Execution follows an accepted plan; delegable decisions resolve
    #: automatically, and amendments are governed by policy.
    PLAN_AUTONOMOUS = "PLAN_AUTONOMOUS"
    #: Days to months with no expected human presence.
    UNATTENDED_LONG_HORIZON = "UNATTENDED_LONG_HORIZON"


class NeedsYou(StrEnum):
    """How human interaction is handled. Not a statement about verification:
    turning it off does not lower a single acceptance bar."""

    ON = "ON"
    DIGEST_ONLY = "DIGEST_ONLY"
    OFF = "OFF"


class Escalation(StrEnum):
    """What to do with a decision the programme may not take by itself."""

    AUTO_POLICY = "AUTO_POLICY"
    SYSTEM_TWO = "SYSTEM_TWO"
    SYSTEM_ONE_THEN_SYSTEM_TWO = "SYSTEM_ONE_THEN_SYSTEM_TWO"
    PARK_BRANCH_AND_CONTINUE = "PARK_BRANCH_AND_CONTINUE"
    ABORT_BRANCH = "ABORT_BRANCH"
    STOP_PROGRAM = "STOP_PROGRAM"
    HUMAN_IF_AVAILABLE = "HUMAN_IF_AVAILABLE"


class ReleasePolicy(StrEnum):
    """Publishing is the one action whose mistakes cannot be taken back."""

    NEVER_AUTO_PUBLISH = "NEVER_AUTO_PUBLISH"
    AUTO_PREPARE_ONLY = "AUTO_PREPARE_ONLY"
    AUTO_PUBLISH_PREAUTHORIZED_TARGET = "AUTO_PUBLISH_PREAUTHORIZED_TARGET"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"


class OnLimit(StrEnum):
    """What a horizon or budget limit does when it is reached."""

    REPLAN = "REPLAN"
    DEGRADE = "DEGRADE"
    PARK = "PARK"
    STOP = "STOP"


class Horizon(Strict):
    """How long the programme may run, and when it must be looked at again.

    `review_after_seconds` is not a reminder to a human: it is what arms a
    `REVIEW_DUE` timer, so a review that is due exists as durable state rather
    than as someone's intention.
    """

    max_wallclock_seconds: int | None = Field(default=None, ge=1)
    max_external_actions: int | None = Field(default=None, ge=0)
    max_concurrency: int = Field(default=1, ge=1)
    review_after_seconds: int | None = Field(default=None, ge=1)
    on_limit: OnLimit = OnLimit.PARK


class KillSwitch(Strict):
    """How the programme can be stopped, and by what that does not have to be
    healthy for stopping to work.

    The plan requires the switch to be reachable from the CLI, the UI and the
    service API, and not to depend on agents or providers being alive. What
    this object carries is the *claim* about that, so a contract cannot quietly
    describe a programme nobody can stop: `channels` must name at least one
    that works without a provider.
    """

    #: Channels the plan names. Validated against a known set: "a kill switch
    #: reachable by carrier pigeon" passed the first version, which a reviewer
    #: pointed out is a claim and not a channel.
    channels: tuple[str, ...] = ("cli",)
    independent_of_providers: bool = True
    note: str = ""

    @model_validator(mode="after")
    def _at_least_one_channel(self) -> "KillSwitch":
        unknown = [c for c in self.channels if c not in KILL_CHANNELS]
        if unknown:
            raise ValueError(f"not a kill channel this product has: {unknown}; "
                             f"known: {sorted(KILL_CHANNELS)}")
        if "cli" not in self.channels:
            raise ValueError("the CLI channel is the one that works when nothing else "
                             "does, so a contract may not drop it")
        if not self.channels:
            raise ValueError("a programme nobody can stop is not a programme "
                             "that may run unattended: name a kill channel")
        if not self.independent_of_providers:
            raise ValueError("a kill switch that needs a provider to be healthy "
                             "is not a kill switch")
        return self


class AutonomyContract(Strict):
    """What this programme may decide without being asked, and what it may not.

    Every field is a decision somebody made before the run. `digest()` is what
    a run records, so "it ran under this contract" is checkable rather than
    asserted, and `pin()` binds it to a programme epoch -- a contract edited
    mid-flight is a different contract, and the epoch is where that shows.
    """

    schema_version: str = SCHEMA
    contract_id: str
    version: int = Field(default=1, ge=1)
    #: The plan this was compiled from, so a contract cannot outlive its source
    #: without anyone noticing.
    source_plan_id: str | None = None
    source_plan_digest: str | None = None
    program_epoch: int = Field(default=0, ge=0)

    mode: AutonomyMode
    #: Paths, node ids or areas the programme may work in. Empty means "what the
    #: plan names", never "anything".
    scope: tuple[str, ...] = ()
    horizon: Horizon = Field(default_factory=Horizon)
    budgets: Budgets = Field(default_factory=Budgets)

    #: Action classes the programme may perform at all. `EXTERNAL` present is
    #: the difference between a programme that can push and one that cannot.
    allowed_action_classes: tuple[ActionClass, ...] = (ActionClass.INTERNAL,)

    provider_policy: str = "any configured provider, retried on quota"
    runtime_policy: str = "strict isolation for every acceptance check"
    network_policy: str = "denied inside checks"
    browser_policy: str = "no browser actions"
    mcp_policy: str = "read-only tools only"
    decision_policy: str = "delegable decisions resolved by policy; the rest escalate"
    release_policy: ReleasePolicy = ReleasePolicy.NEVER_AUTO_PUBLISH
    #: Only meaningful for AUTO_PUBLISH_PREAUTHORIZED_TARGET, and then required.
    preauthorized_target: str | None = None

    needs_you: NeedsYou = NeedsYou.ON
    #: Per action class, what happens when the programme may not decide.
    escalation: dict[str, Escalation] = Field(default_factory=dict)
    #: Decisions a human must take, whatever the mode. An unattended contract
    #: may carry them only if it also says what to do instead of waiting.
    hard_human_gates: tuple[str, ...] = ()
    kill_switch: KillSwitch = Field(default_factory=KillSwitch)
    review_cadence: str = ""
    authored_at: str = Field(default_factory=utcnow)
    note: str = ""

    @model_validator(mode="after")
    def _coherent(self) -> "AutonomyContract":
        problems = coherence(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def canonical(self) -> str:
        """The contract as bytes, with the fields that are not the contract
        left out: when it was authored is provenance, not policy."""
        raw = json.loads(self.model_dump_json())
        raw.pop("authored_at", None)
        return json.dumps(raw, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()[:16]

    def pin(self, program_epoch: int) -> "AutonomyContract":
        """The same contract, bound to an epoch. A new epoch is a new digest,
        which is the point: a run records which one it obeyed.

        Re-validated rather than copied: `model_copy` does not run validators,
        and a reviewer used exactly that to produce an unattended contract with
        Needs-You ON -- incoherent, digesting happily, and routed as if it were
        fine. Anything that goes through this method is a contract that passed
        the rules.
        """
        return AutonomyContract.model_validate(
            self.model_dump() | {"program_epoch": program_epoch})

    def may(self, action_class: ActionClass) -> bool:
        return action_class in self.allowed_action_classes


def coherence(contract: "AutonomyContract") -> list[str]:
    """Why this contract cannot be obeyed as written, or [].

    Refusals, not warnings. A contract that contradicts itself is worse than a
    strict one, because every party reading it believes a different half.
    """
    out: list[str] = []
    if contract.release_policy is ReleasePolicy.AUTO_PUBLISH_PREAUTHORIZED_TARGET \
            and not contract.preauthorized_target:
        out.append("AUTO_PUBLISH_PREAUTHORIZED_TARGET without a named target is "
                   "permission to publish anywhere")
    if contract.preauthorized_target and \
            contract.release_policy is not ReleasePolicy.AUTO_PUBLISH_PREAUTHORIZED_TARGET:
        out.append(f"a preauthorized target ({contract.preauthorized_target}) is named "
                   f"while the release policy is {contract.release_policy.value}")
    if contract.source_plan_id and not contract.source_plan_digest:
        out.append(f"a source plan ({contract.source_plan_id}) with no digest: the "
                   f"contract could outlive the plan it was compiled from with nobody "
                   f"noticing")
    if contract.mode is AutonomyMode.SUPERVISED and contract.needs_you is not NeedsYou.ON:
        out.append("SUPERVISED is defined by the captain steering actively; with "
                   f"Needs-You {contract.needs_you.value} the mode's name stops meaning "
                   "what the plan says it means")
    if contract.mode is AutonomyMode.POLICY_GOVERNED and contract.needs_you is NeedsYou.OFF:
        out.append("POLICY_GOVERNED asks for high-risk decisions; with Needs-You OFF it "
                   "is an unattended programme under another name")
    unattended = contract.mode is AutonomyMode.UNATTENDED_LONG_HORIZON
    if unattended and contract.needs_you is NeedsYou.ON:
        out.append("UNATTENDED_LONG_HORIZON with Needs-You ON describes a programme "
                   "that waits for someone who is not expected to be there")
    if unattended and contract.hard_human_gates and not any(
            contract.escalation.get(k) in (Escalation.PARK_BRANCH_AND_CONTINUE,
                                           Escalation.ABORT_BRANCH,
                                           Escalation.STOP_PROGRAM)
            for k in contract.escalation):
        out.append("unattended with hard human gates and no escalation that can "
                   "resolve one without a human: the programme would stall at the "
                   "first gate and call it progress")
    if ActionClass.EXTERNAL in contract.allowed_action_classes:
        strategy = contract.escalation.get(ActionClass.EXTERNAL.value)
        if strategy is None:
            out.append("EXTERNAL actions are allowed with no escalation named for them; "
                       "an irreversible action needs a decided route, not a default")
        elif strategy is Escalation.AUTO_POLICY:
            out.append("EXTERNAL actions are allowed and routed AUTO_POLICY, which is a "
                       "decided route to deciding it yourself: an irreversible action "
                       "needs an authority outside the programme")
    # A release policy and a release escalation that disagree is the pair a
    # reviewer walked straight through: NEVER_AUTO_PUBLISH beside
    # escalation["release"] = AUTO_POLICY, and `route` answering "resolved by
    # policy" for "publish 0.2.0". Two fields, one question, and nothing tied
    # them together.
    if contract.escalation.get("release") is Escalation.AUTO_POLICY \
            and contract.release_policy is not ReleasePolicy.AUTO_PUBLISH_PREAUTHORIZED_TARGET:
        out.append(f"escalation['release'] is AUTO_POLICY while the release policy is "
                   f"{contract.release_policy.value}: the programme would publish under a "
                   f"contract that forbids it")
    for gate in contract.hard_human_gates:
        if gate not in KNOWN_KINDS:
            out.append(f"hard_human_gates names {gate!r}, which is not a decision kind "
                       f"`route` is ever called with, so the gate would never fire")
    for key in contract.escalation:
        if key not in KNOWN_KINDS:
            out.append(f"escalation names {key!r}, which is not an action class or a "
                       f"known decision kind")
    if unattended:
        # A months-long programme with no ceiling of any kind is not "trusted",
        # it is unbounded. Either the plan names the scope or the contract does,
        # and time and spend need a limit whose breach has a defined effect.
        if not contract.scope and not contract.source_plan_id:
            out.append("unattended with an empty scope and no source plan: nothing "
                       "bounds what the programme may work on")
        if contract.horizon.max_wallclock_seconds is None \
                and contract.budgets.max_wallclock_seconds is None:
            out.append("unattended with no wallclock limit anywhere")
        if contract.budgets.max_dispatches is None:
            out.append("unattended with no dispatch ceiling: provider spend is unbounded")
        if contract.horizon.review_after_seconds is None:
            out.append("unattended with no review interval: the contract promises a "
                       "REVIEW_DUE timer and names no time to arm it for")
    if contract.horizon.max_concurrency > 1 and unattended and \
            contract.horizon.on_limit is OnLimit.STOP:
        # Not a contradiction, but worth stating: stopping a whole unattended
        # programme because one limit was reached throws away independent work.
        contract_note = ("unattended, parallel, and configured to stop the whole "
                         "programme at the first limit")
        if "stop the whole programme" not in (contract.note or ""):
            out.append(contract_note + " -- say so in `note` if that is intended")
    return out


#: How strict each value is, for checking that a caller narrows a profile
#: rather than widening it. Higher is stricter.
_NEEDS_YOU_ORDER = {NeedsYou.OFF: 0, NeedsYou.DIGEST_ONLY: 1, NeedsYou.ON: 2}
_RELEASE_ORDER = {ReleasePolicy.AUTO_PUBLISH_PREAUTHORIZED_TARGET: 0,
                  ReleasePolicy.AUTO_PREPARE_ONLY: 1,
                  ReleasePolicy.HUMAN_APPROVAL: 2,
                  ReleasePolicy.NEVER_AUTO_PUBLISH: 3}
#: An escalation that hands a decision to a human or parks it is stricter than
#: one the programme resolves itself.
_ESCALATION_ORDER = {
    Escalation.AUTO_POLICY: 0,
    Escalation.SYSTEM_TWO: 1,
    Escalation.SYSTEM_ONE_THEN_SYSTEM_TWO: 1,
    Escalation.HUMAN_IF_AVAILABLE: 2,
    Escalation.PARK_BRANCH_AND_CONTINUE: 3,
    Escalation.ABORT_BRANCH: 4,
    Escalation.STOP_PROGRAM: 5,
}


#: The four profiles, as the defaults a compiler proposes. Each is the plan's
#: own description turned into fields; nothing here grants EXTERNAL actions,
#: because the profile is not where irreversibility is decided.
PROFILES: dict[AutonomyMode, dict] = {
    AutonomyMode.SUPERVISED: {
        "needs_you": NeedsYou.ON,
        "release_policy": ReleasePolicy.HUMAN_APPROVAL,
        "escalation": {ActionClass.INTERNAL.value: Escalation.HUMAN_IF_AVAILABLE,
                       "decision": Escalation.HUMAN_IF_AVAILABLE,
                       "release": Escalation.HUMAN_IF_AVAILABLE,
                       "scope": Escalation.HUMAN_IF_AVAILABLE},
        "decision_policy": "the captain decides anything that is not routine",
    },
    AutonomyMode.POLICY_GOVERNED: {
        "needs_you": NeedsYou.ON,
        "release_policy": ReleasePolicy.AUTO_PREPARE_ONLY,
        "escalation": {ActionClass.INTERNAL.value: Escalation.AUTO_POLICY,
                       "decision": Escalation.SYSTEM_TWO,
                       "release": Escalation.HUMAN_IF_AVAILABLE,
                       "scope": Escalation.HUMAN_IF_AVAILABLE},
        "decision_policy": "routine work autonomous; high-risk or scope decisions asked",
    },
    AutonomyMode.PLAN_AUTONOMOUS: {
        "needs_you": NeedsYou.DIGEST_ONLY,
        "release_policy": ReleasePolicy.AUTO_PREPARE_ONLY,
        "escalation": {ActionClass.INTERNAL.value: Escalation.AUTO_POLICY,
                       "decision": Escalation.SYSTEM_TWO,
                       "release": Escalation.PARK_BRANCH_AND_CONTINUE,
                       "scope": Escalation.PARK_BRANCH_AND_CONTINUE},
        "decision_policy": "the accepted plan decides; delegable questions resolve "
                           "automatically; amendments follow policy",
    },
    AutonomyMode.UNATTENDED_LONG_HORIZON: {
        # An envelope, because the coherence rules refuse an unattended
        # contract without one -- a months-long programme with no ceiling is
        # not trusted, it is unbounded. Conservative on purpose: a week of
        # wallclock, two hundred dispatches, a review every six hours. A caller
        # who wants more says so, and says it in a contract someone can read.
        "horizon": Horizon(max_wallclock_seconds=7 * 24 * 3600,
                           review_after_seconds=6 * 3600,
                           on_limit=OnLimit.PARK),
        "budgets": Budgets(max_dispatches=200, max_wallclock_seconds=7 * 24 * 3600),
        "needs_you": NeedsYou.OFF,
        "release_policy": ReleasePolicy.NEVER_AUTO_PUBLISH,
        "escalation": {ActionClass.INTERNAL.value: Escalation.AUTO_POLICY,
                       "decision": Escalation.SYSTEM_ONE_THEN_SYSTEM_TWO,
                       "release": Escalation.PARK_BRANCH_AND_CONTINUE,
                       "scope": Escalation.PARK_BRANCH_AND_CONTINUE},
        "decision_policy": "every delegable decision routes automatically; "
                           "out-of-envelope work parks and independent work continues",
        "review_cadence": "a REVIEW_DUE timer, not a human's memory",
    },
}


def narrowing_problems(mode: AutonomyMode, proposed: dict) -> list[str]:
    """Which overrides would make a profile *wider* than it is.

    A reviewer compiled a contract labelled SUPERVISED that auto-published to a
    repository and had lost the profile's release and scope gates, because
    `update()` took any value and replaced the escalation table wholesale. A
    profile that can be widened is not a profile; it is a suggestion with an
    authoritative-sounding name.
    """
    base = PROFILES[mode]
    out: list[str] = []
    if "needs_you" in proposed and _NEEDS_YOU_ORDER[proposed["needs_you"]] < \
            _NEEDS_YOU_ORDER[base["needs_you"]]:
        out.append(f"{mode.value} attends with Needs-You {base['needs_you'].value}; "
                   f"{proposed['needs_you'].value} is less attended, not more")
    if "release_policy" in proposed and _RELEASE_ORDER[proposed["release_policy"]] < \
            _RELEASE_ORDER[base["release_policy"]]:
        out.append(f"{mode.value} releases under {base['release_policy'].value}; "
                   f"{proposed['release_policy'].value} is looser")
    for kind, strategy in (proposed.get("escalation") or {}).items():
        was = base["escalation"].get(kind)
        if was is not None and _ESCALATION_ORDER[strategy] < _ESCALATION_ORDER[was]:
            out.append(f"{mode.value} routes {kind} as {was.value}; {strategy.value} "
                       f"hands it back to the programme")
    allowed = proposed.get("allowed_action_classes")
    if allowed is not None and not set(allowed) <= {ActionClass.INTERNAL}:
        out.append("a profile does not grant action classes beyond INTERNAL; build the "
                   "contract directly and name an escalation for what it adds")
    return out


def compile_profile(mode: AutonomyMode, *, contract_id: str,
                    source_plan_id: str | None = None,
                    source_plan_digest: str | None = None,
                    program_epoch: int = 0,
                    scope: tuple[str, ...] = (),
                    horizon: Horizon | None = None,
                    budgets: Budgets | None = None,
                    **narrowing) -> AutonomyContract:
    """A profile into a contract (P2-23).

    The compiler *proposes* and validation refuses, which is the plan's rule
    ("the compiler proposes; Core validates"). An override may make a profile
    stricter and is refused when it would make it wider -- the escalation table
    is merged rather than replaced, so a caller cannot drop a gate by omitting
    it. Anything that leaves the contract incoherent is refused as well.
    """
    if mode not in PROFILES:                               # pragma: no cover - enum
        raise ValueError(f"no profile for {mode!r}")
    base = PROFILES[mode]
    proposed = {k: v for k, v in narrowing.items() if v is not None}
    wider = narrowing_problems(mode, proposed)
    if wider:
        raise ValueError("a profile may be narrowed, not widened: " + "; ".join(wider))
    fields = {k: v for k, v in base.items() if k not in ("horizon", "budgets")}
    fields.update({k: v for k, v in proposed.items() if k != "escalation"})
    fields["escalation"] = {**base["escalation"], **(proposed.get("escalation") or {})}
    if mode is AutonomyMode.UNATTENDED_LONG_HORIZON and not scope and not source_plan_id:
        raise ValueError(
            "an unattended contract needs a scope or the plan it was compiled from: "
            "with neither, nothing bounds what the programme may work on")
    return AutonomyContract(
        contract_id=contract_id, mode=mode, source_plan_id=source_plan_id,
        source_plan_digest=source_plan_digest, program_epoch=program_epoch,
        scope=scope,
        horizon=horizon or base.get("horizon") or Horizon(),
        budgets=budgets or base.get("budgets") or Budgets(), **fields)


# --- Needs-You handling (P2-24) -------------------------------------------------

class Handling(StrEnum):
    """What happens to a decision the programme may not take."""

    #: Resolved from the contract, with the rule recorded.
    RESOLVED_BY_POLICY = "RESOLVED_BY_POLICY"
    #: Put in front of a human now.
    ASK_NOW = "ASK_NOW"
    #: Collected for a periodic digest while safe work continues.
    COLLECT_FOR_DIGEST = "COLLECT_FOR_DIGEST"
    #: This branch waits; independent READY work continues.
    PARK_BRANCH = "PARK_BRANCH"
    #: This branch is abandoned, with the reason recorded.
    ABORT_BRANCH = "ABORT_BRANCH"
    #: The whole programme stops.
    STOP_PROGRAM = "STOP_PROGRAM"


#: The handlings after which work that is independent of the stopped branch may
#: carry on (V3.3 P3-28). Every handling is decided here on purpose: a
#: `Handling` added later is in neither this set nor `STOPS_EVERYTHING` until
#: somebody says which, and a handling in neither does not continue anything
#: (`continues_past` is a membership test).
CONTINUES_PAST = frozenset({Handling.RESOLVED_BY_POLICY, Handling.ASK_NOW,
                            Handling.COLLECT_FOR_DIGEST, Handling.PARK_BRANCH,
                            Handling.ABORT_BRANCH})
#: The one handling that is not about a branch: "the whole programme stops".
STOPS_EVERYTHING = frozenset({Handling.STOP_PROGRAM})


def continues_past(handling: Handling) -> bool:
    """May independent READY work carry on while a decision is handled this way?

    Yes for everything that is about *one branch* -- asked, collected, parked,
    abandoned -- and no for STOP_PROGRAM, which the plan words as "the whole
    programme stops". Anything this table does not know is a no, an
    unhashable value included: a handling nobody classified must not be read
    as permission.
    """
    try:
        return handling in CONTINUES_PAST
    except TypeError:
        return False


class Routing(Strict):
    """One decision, routed, with the rule that routed it.

    `why` is not decoration. A programme that runs for weeks produces a long
    list of choices nobody watched, and "which rule decided this" is the only
    thing that makes that list reviewable afterwards.
    """

    decision: str
    kind: str
    handling: Handling
    escalation: Escalation | None = None
    why: str
    #: True only on a routing this class itself refused to hand back: a
    #: decision the programme may not take that had been resolved by policy
    #: anyway. It used to be a field nothing ever set -- decoration, and a test
    #: asserting a constant (a reviewer's words, and they were right).
    fabricated_approval: bool = False
    delegable: bool = True
    #: May the branch this decision belongs to proceed? A controller reading
    #: only `handling` could not tell: COLLECT_FOR_DIGEST means "carry on, this
    #: can wait" for a delegable decision and "this branch waits" for one that
    #: is not. Named rather than inferred.
    blocks_branch: bool = True

    @model_validator(mode="after")
    def _never_approves_what_it_may_not(self) -> "Routing":
        if self.handling is Handling.RESOLVED_BY_POLICY and not self.delegable:
            raise ValueError(
                f"routing {self.decision!r} ({self.kind}) as RESOLVED_BY_POLICY while it "
                f"is not delegable would be a fabricated approval")
        return self


def _routing(**kw) -> Routing:
    """One routing, with `blocks_branch` derived rather than left to a caller.

    Only a decision the programme actually resolved lets its branch continue.
    Everything else -- asked, collected, parked, aborted, stopped -- means this
    branch does not proceed on its own, whatever else the programme does.
    """
    handling = kw["handling"]
    proceeds = handling is Handling.RESOLVED_BY_POLICY or (
        handling is Handling.COLLECT_FOR_DIGEST and kw.get("delegable", True))
    return Routing(blocks_branch=not proceeds, **kw)


def route(contract: AutonomyContract, *, decision: str, kind: str = "decision",
          delegable: bool = True) -> Routing:
    """How this programme handles a decision it has met (P2-24).

    The two rules that matter, both from the plan:

    * a decision that is **not** delegable is never approved by the programme.
      If no human is expected, the affected branch parks with the reason
      recorded and independent work continues -- "do not fake approval".
    * Needs-You is about *interaction*, never about verification. Nothing here
      lowers an acceptance bar; the strictest thing it can do is refuse to
      proceed.
    """
    if kind not in KNOWN_KINDS:
        # Fail-closed on a kind nobody declared. `escalation.get()` returned
        # None for it, None was grouped with AUTO_POLICY, and so a typo -- or
        # any decision kind added later -- resolved itself automatically.
        return _routing(decision=decision, kind=kind, handling=Handling.PARK_BRANCH,
                       delegable=False,
                       why=f"{kind!r} is not a decision kind this contract routes "
                           f"({', '.join(sorted(KNOWN_KINDS))}); the branch parks rather "
                           f"than deciding under a rule nobody wrote")
    # Publishing is decided by the release policy, not only by an escalation
    # entry: a contract saying NEVER_AUTO_PUBLISH beside
    # escalation['release']=AUTO_POLICY used to answer "resolved by policy" for
    # "publish 0.2.0". `coherence` refuses that pair now, and this refuses it
    # again here, because a contract loaded from an older file may still carry
    # it.
    if kind == "release" and contract.release_policy is not \
            ReleasePolicy.AUTO_PUBLISH_PREAUTHORIZED_TARGET:
        # Only a contract that names where it may publish lets a release be
        # resolved by policy. AUTO_PREPARE_ONLY prepares; preparing is not the
        # release decision.
        delegable = False
    if kind in {c.value for c in ActionClass} and not contract.may(ActionClass(kind)):
        # A class the contract does not allow at all cannot be "resolved": a
        # reviewer got RESOLVED_BY_POLICY for `git push --force` out of a
        # contract whose `may(EXTERNAL)` was False.
        return _routing(decision=decision, kind=kind, handling=Handling.PARK_BRANCH,
                        delegable=False,
                        why=f"{kind} actions are not allowed by this contract at all, so "
                            f"there is nothing to resolve: the branch parks")
    incoherent = coherence(contract)
    if incoherent:
        # An object that never passed validation can still be handed here --
        # `model_copy` does not validate, which is how a reviewer produced an
        # unattended contract with Needs-You ON and had it routed as if fine.
        return _routing(decision=decision, kind=kind, handling=Handling.PARK_BRANCH,
                        delegable=False,
                        why="this contract does not hold together, so nothing is decided "
                            f"under it: {incoherent[0]}")
    named = kind in contract.hard_human_gates or not delegable
    strategy = contract.escalation.get(kind)
    if named:
        if contract.needs_you is NeedsYou.ON:
            return _routing(decision=decision, kind=kind, handling=Handling.ASK_NOW,
                           delegable=delegable,
                           escalation=strategy,
                           why="not delegable, and a human is expected: asked directly")
        # No human expected. Whatever the strategy says, the programme does not
        # approve this itself.
        if strategy is Escalation.STOP_PROGRAM:
            return _routing(decision=decision, kind=kind, handling=Handling.STOP_PROGRAM,
                           delegable=delegable,
                           escalation=strategy,
                           why="not delegable, nobody to ask, and the contract stops "
                               "the programme rather than continuing around it")
        if strategy is Escalation.ABORT_BRANCH:
            return _routing(decision=decision, kind=kind, handling=Handling.ABORT_BRANCH,
                           delegable=delegable,
                           escalation=strategy,
                           why="not delegable, nobody to ask, and the contract abandons "
                               "this branch")
        if contract.needs_you is NeedsYou.DIGEST_ONLY:
            return _routing(decision=decision, kind=kind,
                           handling=Handling.COLLECT_FOR_DIGEST, escalation=strategy,
                           delegable=delegable,
                           why="not delegable: collected for the next digest while "
                               "independent work continues")
        return _routing(decision=decision, kind=kind, handling=Handling.PARK_BRANCH,
                       delegable=delegable,
                       escalation=strategy,
                       why="not delegable and no human is expected: this branch parks "
                           "with its reason recorded, and independent READY work "
                           "continues")
    if strategy is None:
        # Absence of a rule used to mean "resolve it", so a contract that did
        # not mention a kind was the most permissive contract of all. Inverted:
        # what nobody decided is asked while a human is expected, and parked
        # otherwise.
        if contract.needs_you is NeedsYou.ON:
            return _routing(decision=decision, kind=kind, handling=Handling.ASK_NOW,
                            delegable=False,
                            why=f"this contract names no escalation for {kind}, so it is "
                                f"asked rather than assumed")
        return _routing(decision=decision, kind=kind, handling=Handling.PARK_BRANCH,
                        delegable=False,
                        why=f"this contract names no escalation for {kind} and no human "
                            f"is expected: the branch parks rather than deciding under a "
                            f"rule nobody wrote")
    if strategy is Escalation.AUTO_POLICY:
        return _routing(decision=decision, kind=kind,
                       handling=Handling.RESOLVED_BY_POLICY, escalation=strategy,
                       delegable=delegable,
                       why=f"delegable, and the contract resolves {kind} by policy: "
                           f"{contract.decision_policy}")
    if strategy in (Escalation.SYSTEM_TWO, Escalation.SYSTEM_ONE_THEN_SYSTEM_TWO):
        return _routing(decision=decision, kind=kind,
                       handling=Handling.RESOLVED_BY_POLICY, escalation=strategy,
                       delegable=delegable,
                       why=f"delegable, routed to {strategy.value} by the contract")
    if strategy is Escalation.HUMAN_IF_AVAILABLE:
        if contract.needs_you is NeedsYou.ON:
            return _routing(decision=decision, kind=kind, handling=Handling.ASK_NOW,
                           delegable=delegable,
                           escalation=strategy, why="asked while a human is expected")
        if contract.needs_you is NeedsYou.DIGEST_ONLY:
            return _routing(decision=decision, kind=kind,
                           handling=Handling.COLLECT_FOR_DIGEST, escalation=strategy,
                           delegable=delegable,
                           why="collected for the digest: a human is not attending, "
                               "and this decision can wait for one")
        return _routing(decision=decision, kind=kind, handling=Handling.PARK_BRANCH,
                       delegable=delegable,
                       escalation=strategy,
                       why="the contract prefers a human and none is expected, so this "
                           "branch parks rather than deciding for them")
    if strategy is Escalation.PARK_BRANCH_AND_CONTINUE:
        return _routing(decision=decision, kind=kind, handling=Handling.PARK_BRANCH,
                       delegable=delegable,
                       escalation=strategy,
                       why="the contract parks this branch and continues elsewhere")
    if strategy is Escalation.ABORT_BRANCH:
        return _routing(decision=decision, kind=kind, handling=Handling.ABORT_BRANCH,
                           delegable=delegable,
                       escalation=strategy, why="the contract abandons this branch")
    return _routing(decision=decision, kind=kind, handling=Handling.STOP_PROGRAM,
                           delegable=delegable,
                   escalation=strategy,
                   why="the contract stops the programme for this kind of decision")
