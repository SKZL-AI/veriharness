"""The AutonomyContract: what may a programme decide when nobody is watching?

The cases are the plan's own rules. The one that matters most is negative: a
decision that is genuinely non-delegable is **never** approved by the
programme, whatever the mode. An unattended programme parks the affected branch
with its reason recorded and carries on with independent work -- "do not fake
approval" -- and there is a case here for every route that could have been a
quiet yes instead.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from hoh.autonomy import (  # noqa: F401 - OnLimit is asserted through Horizon
    PROFILES,
    AutonomyContract,
    AutonomyMode,
    Escalation,
    Handling,
    Horizon,
    KillSwitch,
    NeedsYou,
    OnLimit,
    ReleasePolicy,
    coherence,
    compile_profile,
    route,
)
from hoh.contracts import Budgets
from hoh.project import ActionClass


#: What an unattended contract needs before the coherence rules will accept it:
#: something that bounds the work, a wallclock, a dispatch ceiling and a review
#: interval. Written once here so every case below is about the rule it tests.
UNATTENDED_ENVELOPE = dict(
    scope=("src/hoh/",),
    horizon=Horizon(max_wallclock_seconds=3600, review_after_seconds=600),
    budgets=Budgets(max_dispatches=10, max_wallclock_seconds=3600),
)


def _contract(**kw) -> AutonomyContract:
    fields = dict(contract_id="c1", mode=AutonomyMode.POLICY_GOVERNED,
                  escalation={"decision": Escalation.AUTO_POLICY})
    if kw.get("mode") is AutonomyMode.UNATTENDED_LONG_HORIZON:
        fields.update(UNATTENDED_ENVELOPE, needs_you=NeedsYou.OFF)
    fields.update(kw)
    return AutonomyContract(**fields)


def _compiled(mode: AutonomyMode, **kw) -> AutonomyContract:
    """A compiled profile, with the scope an unattended one requires."""
    if mode is AutonomyMode.UNATTENDED_LONG_HORIZON:
        kw.setdefault("scope", ("src/hoh/",))
    return compile_profile(mode, contract_id=f"c-{mode.value.lower()}", **kw)


# --- P2-22: the schema ----------------------------------------------------------

def test_every_profile_compiles_into_a_coherent_contract():
    """The positive control: each of the four profiles the plan names must
    produce a contract that validation accepts, or the compiler is describing
    programmes that cannot run."""
    for mode in AutonomyMode:
        contract = _compiled(mode)
        assert contract.mode is mode
        assert coherence(contract) == []
        assert contract.digest() and len(contract.digest()) == 16


def test_the_digest_covers_the_policy_and_not_the_moment_it_was_written():
    a = _compiled(AutonomyMode.POLICY_GOVERNED)
    b = a.model_copy(update={"authored_at": "2020-01-01T00:00:00Z"})
    assert a.digest() == b.digest(), "when it was authored is provenance, not policy"
    assert a.digest() != a.model_copy(update={"needs_you": NeedsYou.OFF}).digest()


def test_pinning_to_an_epoch_changes_the_digest():
    """A contract edited mid-flight is a different contract, and the epoch is
    where that shows: a run records which digest it obeyed."""
    base = _compiled(AutonomyMode.PLAN_AUTONOMOUS)
    assert base.pin(4).program_epoch == 4
    assert base.pin(4).digest() != base.digest()
    assert base.pin(4).digest() == base.pin(4).digest()


def test_a_contract_that_contradicts_itself_is_refused_not_warned_about():
    with pytest.raises(ValidationError) as caught:
        _contract(mode=AutonomyMode.UNATTENDED_LONG_HORIZON, needs_you=NeedsYou.ON)
    assert "waits for someone who is not expected" in str(caught.value)


@pytest.mark.parametrize("change,needle", [
    ({"release_policy": ReleasePolicy.AUTO_PUBLISH_PREAUTHORIZED_TARGET},
     "without a named target"),
    ({"preauthorized_target": "github.com/someone/else"}, "is named while the release"),
    ({"allowed_action_classes": (ActionClass.INTERNAL, ActionClass.EXTERNAL)},
     "no escalation named for them"),
    ({"escalation": {"whatever": Escalation.AUTO_POLICY}}, "not an action class"),
])
def test_each_incoherent_combination_is_named(change, needle):
    """Refused at authoring time, and the message says which rule was broken --
    a contract that contradicts itself is worse than a strict one, because each
    party reading it believes a different half."""
    with pytest.raises(ValidationError) as caught:
        _contract(**change)
    assert needle in str(caught.value), str(caught.value)


def test_publishing_to_a_named_target_is_allowed_when_it_is_actually_named():
    contract = _contract(release_policy=ReleasePolicy.AUTO_PUBLISH_PREAUTHORIZED_TARGET,
                         preauthorized_target="github.com/SKZL-AI/veriharness")
    assert coherence(contract) == []


def test_a_kill_switch_nobody_can_reach_is_refused():
    with pytest.raises(ValidationError):
        KillSwitch(channels=())
    with pytest.raises(ValidationError):
        KillSwitch(channels=("cli",), independent_of_providers=False)


def test_external_actions_need_a_decided_route():
    """An irreversible action with no escalation is the shape that gets waved
    through, so it is refused at authoring time."""
    with pytest.raises(ValidationError):
        _contract(allowed_action_classes=(ActionClass.INTERNAL, ActionClass.EXTERNAL))
    ok = _contract(allowed_action_classes=(ActionClass.INTERNAL, ActionClass.EXTERNAL),
                   escalation={"decision": Escalation.AUTO_POLICY,
                               ActionClass.EXTERNAL.value: Escalation.HUMAN_IF_AVAILABLE})
    assert ok.may(ActionClass.EXTERNAL) and ok.may(ActionClass.INTERNAL)


def test_by_default_nothing_external_is_allowed():
    assert not _contract().may(ActionClass.EXTERNAL)
    for mode in AutonomyMode:
        assert not _compiled(mode).may(ActionClass.EXTERNAL), (
            "a profile is not where irreversibility is decided")


# --- P2-23: the compiler --------------------------------------------------------

def test_the_profiles_are_the_plans_own_descriptions():
    supervised = _compiled(AutonomyMode.SUPERVISED)
    assert supervised.needs_you is NeedsYou.ON
    assert supervised.release_policy is ReleasePolicy.HUMAN_APPROVAL

    unattended = _compiled(AutonomyMode.UNATTENDED_LONG_HORIZON)
    assert unattended.needs_you is NeedsYou.OFF
    assert unattended.release_policy is ReleasePolicy.NEVER_AUTO_PUBLISH
    assert unattended.escalation["scope"] is Escalation.PARK_BRANCH_AND_CONTINUE

    plan_led = _compiled(AutonomyMode.PLAN_AUTONOMOUS)
    assert plan_led.needs_you is NeedsYou.DIGEST_ONLY


def test_a_caller_may_narrow_a_profile_but_not_break_it():
    stricter = _compiled(AutonomyMode.UNATTENDED_LONG_HORIZON,
                         release_policy=ReleasePolicy.NEVER_AUTO_PUBLISH,
                         horizon=Horizon(max_wallclock_seconds=3600,
                                         review_after_seconds=600),
                         budgets=Budgets(max_iterations=3, max_dispatches=5,
                                         max_wallclock_seconds=3600))
    assert stricter.horizon.review_after_seconds == 600
    assert stricter.budgets.max_iterations == 3
    with pytest.raises(ValueError):
        _compiled(AutonomyMode.UNATTENDED_LONG_HORIZON, needs_you=NeedsYou.ON)


def test_the_compiler_covers_every_mode_the_enum_has():
    """A mode without a profile would be a mode nobody can compile, and the
    gap would only show when someone tried."""
    assert set(PROFILES) == set(AutonomyMode)


# --- P2-24: Needs-You handling --------------------------------------------------

def test_a_non_delegable_decision_is_never_approved_by_the_programme():
    """The plan's rule, and the one worth testing hardest: whatever the mode,
    the programme does not decide this itself."""
    for mode in AutonomyMode:
        contract = _compiled(mode)
        # Every kind, including the ones whose *delegable* route resolves by
        # policy -- a mutation check showed that testing only `release` passed
        # even with the non-delegable rule removed, because no profile resolves
        # a release by policy in the first place.
        for kind in ("release", "decision", "scope", ActionClass.INTERNAL.value):
            routing = route(contract, decision="sign it", kind=kind, delegable=False)
            assert routing.handling is not Handling.RESOLVED_BY_POLICY, (mode, kind, routing)
            assert routing.fabricated_approval is False
            assert routing.why


def test_unattended_parks_the_branch_and_says_why():
    contract = _compiled(AutonomyMode.UNATTENDED_LONG_HORIZON)
    routing = route(contract, decision="accept a licence change", kind="decision",
                    delegable=False)
    assert routing.handling is Handling.PARK_BRANCH
    assert "independent READY work continues" in routing.why


def test_supervised_asks_immediately():
    contract = _compiled(AutonomyMode.SUPERVISED)
    routing = route(contract, decision="accept a licence change", delegable=False)
    assert routing.handling is Handling.ASK_NOW


def test_digest_only_collects_instead_of_waiting():
    contract = _compiled(AutonomyMode.PLAN_AUTONOMOUS)
    routing = route(contract, decision="rename a public flag", delegable=False)
    assert routing.handling is Handling.COLLECT_FOR_DIGEST
    assert "independent work continues" in routing.why


def test_a_delegable_decision_resolves_by_policy_and_names_the_rule():
    contract = _compiled(AutonomyMode.POLICY_GOVERNED)
    routing = route(contract, decision="pick a retry interval", kind=ActionClass.INTERNAL.value)
    assert routing.handling is Handling.RESOLVED_BY_POLICY
    assert contract.decision_policy in routing.why


@pytest.mark.parametrize("strategy,expected", [
    (Escalation.STOP_PROGRAM, Handling.STOP_PROGRAM),
    (Escalation.ABORT_BRANCH, Handling.ABORT_BRANCH),
    (Escalation.PARK_BRANCH_AND_CONTINUE, Handling.PARK_BRANCH),
])
def test_the_contracts_own_strategy_decides_what_happens_instead(strategy, expected):
    contract = _contract(mode=AutonomyMode.UNATTENDED_LONG_HORIZON,
                         needs_you=NeedsYou.OFF,
                         escalation={"decision": strategy},
                         note="stop the whole programme is intended")
    assert route(contract, decision="d", delegable=False).handling is expected


def test_a_hard_gate_named_in_the_contract_is_treated_as_non_delegable():
    contract = _contract(hard_human_gates=("release",),
                         escalation={"decision": Escalation.AUTO_POLICY,
                                     "release": Escalation.HUMAN_IF_AVAILABLE})
    routing = route(contract, decision="publish", kind="release", delegable=True)
    assert routing.handling is Handling.ASK_NOW
    assert "not delegable" in routing.why


def test_routing_records_the_rule_it_came_from():
    """Weeks of unattended choices are only reviewable if each one says which
    rule produced it."""
    contract = _compiled(AutonomyMode.UNATTENDED_LONG_HORIZON)
    for kind in ("decision", "release", "scope", ActionClass.INTERNAL.value):
        routing = route(contract, decision="d", kind=kind)
        assert routing.why and routing.kind == kind
        assert json.loads(routing.model_dump_json())["handling"] == routing.handling.value


def test_needs_you_off_does_not_lower_a_single_acceptance_bar():
    """Needs-You is about interaction. Nothing in this module touches
    verification, and this pins that the contract carries no field that could:
    the runtime policy stays strict isolation in every profile."""
    for mode in AutonomyMode:
        contract = _compiled(mode)
        assert "strict" in contract.runtime_policy
        assert contract.network_policy == "denied inside checks"


# --- what two adversarial reviews walked through ---------------------------------

def test_a1_a_kind_the_contract_never_mentions_is_not_resolved_by_policy():
    """Absence of a rule used to mean "resolve it", which made a contract that
    said nothing about a kind the most permissive contract of all. Measured by a
    reviewer on `release`, on a typo, and on a forbidden action class."""
    attended = _contract(escalation={"decision": Escalation.AUTO_POLICY})
    unattended = _contract(mode=AutonomyMode.UNATTENDED_LONG_HORIZON,
                           escalation={"decision": Escalation.AUTO_POLICY})
    for kind in ("scope", ActionClass.INTERNAL.value):
        asked = route(attended, decision="d", kind=kind)
        assert asked.handling is Handling.ASK_NOW and asked.blocks_branch
        parked = route(unattended, decision="d", kind=kind)
        assert parked.handling is Handling.PARK_BRANCH and parked.blocks_branch


def test_a1_an_unknown_kind_parks_instead_of_deciding_itself():
    for kind in ("relase", "deploy", ""):
        routing = route(_contract(), decision="deploy to prod", kind=kind)
        assert routing.handling is Handling.PARK_BRANCH
        assert "not a decision kind this contract routes" in routing.why


def test_a1_a_release_is_never_policy_resolved_unless_the_target_is_named():
    """NEVER_AUTO_PUBLISH beside escalation['release'] = AUTO_POLICY answered
    "resolved by policy" for "publish 0.2.0 to pypi". Refused at authoring time
    now, and refused again here for a contract loaded from an older file."""
    with pytest.raises(ValidationError):
        _contract(escalation={"decision": Escalation.AUTO_POLICY,
                              "release": Escalation.AUTO_POLICY})
    for policy in (ReleasePolicy.NEVER_AUTO_PUBLISH, ReleasePolicy.HUMAN_APPROVAL,
                   ReleasePolicy.AUTO_PREPARE_ONLY):
        contract = _contract(release_policy=policy,
                             escalation={"decision": Escalation.AUTO_POLICY,
                                         "release": Escalation.HUMAN_IF_AVAILABLE})
        assert route(contract, decision="publish 0.2.0", kind="release").handling \
            is not Handling.RESOLVED_BY_POLICY
    named = _contract(release_policy=ReleasePolicy.AUTO_PUBLISH_PREAUTHORIZED_TARGET,
                      preauthorized_target="github.com/SKZL-AI/veriharness",
                      escalation={"decision": Escalation.AUTO_POLICY,
                                  "release": Escalation.AUTO_POLICY})
    assert route(named, decision="publish", kind="release").handling \
        is Handling.RESOLVED_BY_POLICY


def test_a1_an_action_class_the_contract_forbids_has_nothing_to_resolve():
    contract = _contract()
    assert not contract.may(ActionClass.EXTERNAL)
    routing = route(contract, decision="git push --force", kind=ActionClass.EXTERNAL.value)
    assert routing.handling is Handling.PARK_BRANCH
    assert "not allowed by this contract at all" in routing.why


@pytest.mark.parametrize("override,needle", [
    ({"needs_you": NeedsYou.OFF}, "is less attended"),
    ({"release_policy": ReleasePolicy.AUTO_PUBLISH_PREAUTHORIZED_TARGET}, "is looser"),
    ({"escalation": {"release": Escalation.AUTO_POLICY}}, "hands it back to the programme"),
    ({"allowed_action_classes": (ActionClass.INTERNAL, ActionClass.EXTERNAL)},
     "does not grant action classes beyond INTERNAL"),
])
def test_a2_a_profile_may_be_narrowed_but_not_widened(override, needle):
    """A reviewer compiled a contract labelled SUPERVISED that auto-published to
    a repository and had lost the profile's release and scope gates, because the
    escalation table was replaced wholesale and no ordering was checked."""
    with pytest.raises(ValueError) as caught:
        _compiled(AutonomyMode.SUPERVISED, **override)
    assert needle in str(caught.value), str(caught.value)


def test_a2_the_escalation_table_is_merged_so_a_gate_cannot_be_dropped():
    narrowed = _compiled(AutonomyMode.POLICY_GOVERNED,
                         escalation={"decision": Escalation.STOP_PROGRAM})
    assert narrowed.escalation["decision"] is Escalation.STOP_PROGRAM
    assert narrowed.escalation["release"] is Escalation.HUMAN_IF_AVAILABLE, (
        "omitting a kind must not drop the profile's rule for it")
    assert narrowed.escalation["scope"] is Escalation.HUMAN_IF_AVAILABLE


def test_a3_pinning_revalidates_so_a_copy_cannot_walk_around_the_rules():
    """`model_copy` does not run validators, and a reviewer used it to produce an
    unattended contract with Needs-You ON that digested happily."""
    contract = _compiled(AutonomyMode.UNATTENDED_LONG_HORIZON)
    smuggled = contract.model_copy(update={"needs_you": NeedsYou.ON})
    assert coherence(smuggled), "the copy is incoherent, which is the premise here"
    with pytest.raises(ValidationError):
        smuggled.pin(3)


def test_a3_an_incoherent_contract_decides_nothing():
    contract = _compiled(AutonomyMode.UNATTENDED_LONG_HORIZON)
    smuggled = contract.model_copy(update={"needs_you": NeedsYou.ON})
    routing = route(smuggled, decision="anything", kind="decision")
    assert routing.handling is Handling.PARK_BRANCH
    assert "does not hold together" in routing.why


def test_a4_the_routing_says_whether_the_branch_may_proceed():
    """COLLECT_FOR_DIGEST means "carry on, this can wait" for a delegable
    decision and "this branch waits" for one that is not; a controller reading
    only `handling` could not tell."""
    plan_led = _compiled(AutonomyMode.PLAN_AUTONOMOUS)
    waiting = route(plan_led, decision="d", kind="decision", delegable=False)
    assert waiting.handling is Handling.COLLECT_FOR_DIGEST and waiting.blocks_branch
    resolved = route(plan_led, decision="d", kind=ActionClass.INTERNAL.value)
    assert resolved.handling is Handling.RESOLVED_BY_POLICY and not resolved.blocks_branch


def test_a4_a_fabricated_approval_cannot_be_constructed():
    """The field used to be decoration -- never set, and asserted as a constant.
    It is an invariant now: this shape raises rather than returning."""
    from hoh.autonomy import Routing

    with pytest.raises(ValidationError):
        Routing(decision="publish", kind="release", delegable=False,
                handling=Handling.RESOLVED_BY_POLICY, why="because I said so")


@pytest.mark.parametrize("change,needle", [
    ({"hard_human_gates": ("publishing to the public remote",)}, "never fire"),
    ({"source_plan_id": "plan-1"}, "no digest"),
    ({"allowed_action_classes": (ActionClass.INTERNAL, ActionClass.EXTERNAL),
      "escalation": {"decision": Escalation.AUTO_POLICY,
                     ActionClass.EXTERNAL.value: Escalation.AUTO_POLICY}},
     "decided route to deciding it yourself"),
])
def test_a5_a7_gates_plans_and_irreversible_actions(change, needle):
    with pytest.raises(ValidationError) as caught:
        _contract(**change)
    assert needle in str(caught.value), str(caught.value)


def test_a7_a_kill_switch_must_name_channels_this_product_has():
    with pytest.raises(ValidationError):
        KillSwitch(channels=("carrier pigeon",))
    with pytest.raises(ValidationError):
        KillSwitch(channels=("ui",))          # the CLI is the one that always works
    assert KillSwitch(channels=("cli", "ui")).channels == ("cli", "ui")


@pytest.mark.parametrize("mode,needs_you", [
    (AutonomyMode.SUPERVISED, NeedsYou.DIGEST_ONLY),
    (AutonomyMode.SUPERVISED, NeedsYou.OFF),
    (AutonomyMode.POLICY_GOVERNED, NeedsYou.OFF),
])
def test_a7_a_mode_keeps_meaning_what_the_plan_says_it_means(mode, needs_you):
    with pytest.raises(ValidationError):
        _contract(mode=mode, needs_you=needs_you)


def test_an_unattended_contract_needs_an_envelope():
    """A months-long programme with no ceiling of any kind is not trusted, it is
    unbounded -- so the profile carries one and a contract without one is
    refused."""
    with pytest.raises(ValueError, match="needs a scope or the plan"):
        compile_profile(AutonomyMode.UNATTENDED_LONG_HORIZON, contract_id="c")
    compiled = _compiled(AutonomyMode.UNATTENDED_LONG_HORIZON)
    assert compiled.horizon.review_after_seconds and compiled.budgets.max_dispatches
    for missing in ({"budgets": Budgets(max_dispatches=10),            # no wallclock anywhere
                     "horizon": Horizon(review_after_seconds=600)},
                    {"budgets": Budgets(max_wallclock_seconds=60)},    # no dispatch ceiling
                    {"horizon": Horizon(max_wallclock_seconds=60)},    # no review interval
                    {"scope": ()}):                                    # nothing bounds the work
        with pytest.raises(ValidationError):
            _contract(mode=AutonomyMode.UNATTENDED_LONG_HORIZON,
                      **{**UNATTENDED_ENVELOPE, **missing})
    # A wallclock in the budgets is enough: the rule asks for one *anywhere*,
    # not for one in both places.
    assert _contract(mode=AutonomyMode.UNATTENDED_LONG_HORIZON,
                     **{**UNATTENDED_ENVELOPE,
                        "horizon": Horizon(review_after_seconds=60)})
