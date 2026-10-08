"""What an external project manager owns, and what this product does not give up.

V3.3 P2-29, P2-30. The plan states the danger in one line:

    PM integration must not create two mutable sources of project truth.

So a `ProjectContract` is **imported**, immutable once imported, and amended
only by a new version that is evaluated rather than applied. The external
system owns objectives, milestones, budget authorisation and organisational
risk. This product owns the executable DAG, run and agent state, candidate
identity, evidence, acceptance, repair, integration and closure -- and the one
sentence that matters when the two disagree:

    External DONE cannot override VeriHarness NOT VERIFIED.

That is not a preference about tooling. A project manager marking a work
package done is a statement about a plan; a verdict here is a statement about
evidence, and the second does not become false because the first was written.

`OWNERSHIP` is the ADR's table as data, so "who decides this" is answerable by
a function instead of by reading a document -- and `authority_for()` refuses an
unknown state class rather than guessing, because a state nobody owns is how
two systems come to believe different things.
"""
from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from .contracts import Strict, utcnow

SCHEMA = "hoh-project-contract/1"


class Authority(StrEnum):
    """Who decides a class of state. Three, and no "shared": a state with two
    authorities is a state with none."""

    #: The external project manager, in PM_INTEGRATED mode.
    EXTERNAL_PM = "EXTERNAL_PM"
    #: This product.
    VERIHARNESS = "VERIHARNESS"
    #: A runtime adapter: process mechanics only.
    RUNTIME_ADAPTER = "RUNTIME_ADAPTER"
    #: A projection with no authority of its own -- a UI, a checklist
    #: rendering, a recommendation. Named rather than omitted, because "this
    #: has no authority" is the useful half of the table.
    NONE = "NONE"


#: The ADR's table, as data. Every row of
#: `specs/full_stack_contracts_r1/02_STATE_OWNERSHIP_ADR.md`.
OWNERSHIP: dict[str, Authority] = {
    "external_objective": Authority.EXTERNAL_PM,
    "portfolio_priority": Authority.EXTERNAL_PM,
    "milestone_deadline": Authority.EXTERNAL_PM,
    "budget_authorisation": Authority.EXTERNAL_PM,
    "organisational_risk": Authority.EXTERNAL_PM,
    "imported_project_contract": Authority.VERIHARNESS,
    "active_program_scope": Authority.VERIHARNESS,
    "program_dag": Authority.VERIHARNESS,
    "ready_frontier": Authority.VERIHARNESS,
    "run_acceptance": Authority.VERIHARNESS,
    "evidence_validity": Authority.VERIHARNESS,
    "integration_closure": Authority.VERIHARNESS,
    "model_routing": Authority.VERIHARNESS,
    "session_authority": Authority.VERIHARNESS,
    "leader_lease": Authority.VERIHARNESS,
    "process_mechanics": Authority.RUNTIME_ADAPTER,
    "session_mechanics": Authority.RUNTIME_ADAPTER,
    "ui_projection": Authority.NONE,
    "checklist_projection": Authority.NONE,
    "system_one_recommendation": Authority.NONE,
    "external_finding": Authority.NONE,
}


class UnknownState(KeyError):
    """Nobody owns this state class, and guessing an owner is how two systems
    come to believe different things."""


def authority_for(state_class: str) -> Authority:
    try:
        return OWNERSHIP[state_class]
    except KeyError as exc:
        raise UnknownState(
            f"no authority is declared for {state_class!r}; add it to the ownership "
            f"table with a reason rather than letting the caller assume one") from exc


class WorkPackage(Strict):
    """A unit of work as the external system describes it.

    Deliberately thin: this is what the PM says, not what this product will do
    about it. The mapping from a work package to a programme node is the
    programme's business, and conflating the two is how an external plan starts
    dictating an executable DAG.
    """

    work_package_id: str
    title: str
    depends_on: tuple[str, ...] = ()
    milestone: str | None = None
    #: What the external system currently believes. Recorded, never obeyed:
    #: `external_status` is an input to a decision, not the decision.
    external_status: str | None = None

    model_config = ConfigDict(frozen=True)


class ProjectContract(Strict):
    """One imported version of an external project's intent.

    Immutable after import. A changed external plan does not edit this; it
    produces a `ProjectContractAmendment` that is *evaluated* -- against scope,
    evidence, active work, the autonomy contract and the release state -- and
    only then becomes a new version.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA
    project_contract_id: str
    version: int = Field(default=1, ge=1)
    source_system: str
    source_revision: str
    objective: str
    deliverables: tuple[str, ...] = ()
    work_packages: tuple[WorkPackage, ...] = ()
    dependencies: tuple[str, ...] = ()
    milestones: dict[str, str] = Field(default_factory=dict)
    budget: str | None = None
    resource_constraints: str | None = None
    risk_classification: str | None = None
    data_classification: str | None = None
    model_policy: str | None = None
    tool_policy: str | None = None
    network_policy: str | None = None
    acceptance_policy: str | None = None
    completion_policy: str | None = None
    release_policy: str | None = None
    change_control: str | None = None
    effective_at: str = Field(default_factory=utcnow)
    imported_at: str = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _dependencies_resolve(self) -> "ProjectContract":
        known = {w.work_package_id for w in self.work_packages}
        if len(known) != len(self.work_packages):
            raise ValueError("two work packages share an id, so a dependency on it "
                             "names two different things")
        for package in self.work_packages:
            unknown = [d for d in package.depends_on if d not in known]
            if unknown:
                raise ValueError(
                    f"work package {package.work_package_id} depends on {unknown}, which "
                    f"this contract does not contain: an imported plan with dangling "
                    f"dependencies would produce a DAG nobody can execute")
            if package.milestone and package.milestone not in self.milestones:
                raise ValueError(
                    f"work package {package.work_package_id} names milestone "
                    f"{package.milestone!r}, which this contract does not define")
        return self

    def canonical(self) -> str:
        raw = json.loads(self.model_dump_json())
        for field in ("imported_at",):
            raw.pop(field, None)          # when it arrived is provenance, not content
        return json.dumps(raw, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()[:16]


class AmendmentImpact(Strict):
    """What a proposed external change would disturb here.

    The plan lists five questions an amendment is evaluated for, and this is
    those five as fields. An amendment is never applied by this object: it is
    described, and the programme decides.
    """

    scope_changes: tuple[str, ...] = ()
    dependency_changes: tuple[str, ...] = ()
    #: Work packages whose evidence would no longer describe what they name.
    evidence_invalidated: tuple[str, ...] = ()
    #: Work packages with work in flight against the old version.
    active_work_affected: tuple[str, ...] = ()
    #: True when the change needs authority the autonomy contract does not give.
    beyond_autonomy: bool = False
    release_affected: bool = False

    @property
    def disturbs_anything(self) -> bool:
        return bool(self.scope_changes or self.dependency_changes
                    or self.evidence_invalidated or self.active_work_affected
                    or self.beyond_autonomy or self.release_affected)


class ProjectContractAmendment(Strict):
    """A proposed next version of an imported contract, and what it would cost."""

    amendment_id: str
    project_contract_id: str
    from_version: int = Field(ge=1)
    to_version: int = Field(ge=2)
    source_revision: str
    reason: str
    proposed_at: str = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _forward_only(self) -> "ProjectContractAmendment":
        if self.to_version <= self.from_version:
            raise ValueError(
                f"an amendment goes forward: {self.from_version} -> {self.to_version} "
                f"would rewrite a version that has already been imported")
        return self


def impact_of(previous: ProjectContract, proposed: ProjectContract, *,
              evidence_for: dict[str, str] | None = None,
              active_work: set[str] | None = None,
              autonomy_allows_scope_change: bool = True,
              released: set[str] | None = None) -> AmendmentImpact:
    """What changing from one imported version to the next would disturb.

    `evidence_for` maps a work package to the contract digest its evidence was
    produced under: evidence gathered against a description that has since
    changed does not describe the new one, which is the invalidation the plan
    asks to be evaluated rather than assumed.
    """
    if previous.project_contract_id != proposed.project_contract_id:
        raise ValueError("these are two different contracts, not two versions of one")
    was = {w.work_package_id: w for w in previous.work_packages}
    now = {w.work_package_id: w for w in proposed.work_packages}
    added = sorted(set(now) - set(was))
    removed = sorted(set(was) - set(now))
    changed = sorted(w for w in set(was) & set(now)
                     if was[w].model_dump() != now[w].model_dump())
    dependency_changes = sorted(
        w for w in set(was) & set(now) if was[w].depends_on != now[w].depends_on)
    evidence_for = evidence_for or {}
    invalidated = sorted(
        w for w in set(was) & set(now)
        if w in evidence_for and evidence_for[w] == previous.digest()
        and (was[w].model_dump() != now[w].model_dump()))
    scope = tuple(f"+{w}" for w in added) + tuple(f"-{w}" for w in removed) + \
        tuple(f"~{w}" for w in changed)
    return AmendmentImpact(
        scope_changes=scope,
        dependency_changes=tuple(dependency_changes),
        evidence_invalidated=tuple(invalidated),
        active_work_affected=tuple(sorted((active_work or set()) & (set(removed) | set(changed)))),
        beyond_autonomy=bool(scope) and not autonomy_allows_scope_change,
        release_affected=bool((released or set()) & (set(removed) | set(changed))),
    )


class VerdictConflict(Strict):
    """An external system says done; this product says not verified."""

    work_package_id: str
    external_status: str
    internal_status: str
    #: Always the internal one. The field exists so the answer is in the record
    #: rather than in whoever reads it.
    authoritative: str = "VERIHARNESS"

    def __str__(self) -> str:
        return (f"{self.work_package_id}: the external system reports "
                f"{self.external_status!r} and this product has "
                f"{self.internal_status!r}; the verdict here stands")


def conflicts(contract: ProjectContract, *, internal: dict[str, str],
              done_words: frozenset[str] = frozenset({"DONE", "COMPLETE", "CLOSED"}),
              verified_words: frozenset[str] = frozenset({"VERIFIED", "ACCEPTED"}),
              ) -> list[VerdictConflict]:
    """Where an imported plan claims completion this product has not verified.

    The plan's rule, as a measurement: *external `DONE` cannot override
    VeriHarness `NOT VERIFIED`*. Reported rather than resolved -- a conflict is
    something a person or a policy decides about, and silently preferring
    either side is how one of the two stops being believed at all.
    """
    out = []
    for package in contract.work_packages:
        external = (package.external_status or "").upper()
        if external not in done_words:
            continue
        here = (internal.get(package.work_package_id) or "NOT_VERIFIED").upper()
        if here not in verified_words:
            out.append(VerdictConflict(work_package_id=package.work_package_id,
                                       external_status=external, internal_status=here))
    return out
