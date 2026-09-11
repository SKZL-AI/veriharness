"""Meta-evidence: what a measurement has to say about itself before it counts.

This module exists because of a pattern, not because of a plan. Five times in
this project a verification tool reported green while measuring nothing:

1. a halt class compared as ``str(HaltClass.X)`` against ``"HaltClass.X"`` --
   two strings that never match, so the check passed unconditionally;
2. repair nodes read from the step log instead of `ProjectState`, so a node
   created in one step and merged in another appeared in neither;
3. a duplicate-merge check read from the step log instead of git, and passed
   on a list of length one because the killed step had logged nothing;
4. an out-of-band-action count taken as ``len(external_actions)`` on a run
   that predated the record mechanism -- an empty list read as a measured zero;
5. a CI job reported green while the step that was supposed to measure the
   sandbox had been skipped.

Read individually these are five unrelated slips. Read together they are one
failure mode with one shape: **a convenient but weaker source was accepted as
proof.** A step log instead of the state that outlives it. An empty list
instead of an admission that nothing was measured. A job's conclusion instead
of a step's.

So the countermeasure is not five fixes. It is a rule about what a metric must
be able to say about itself:

* which source it came from, and whether that source is the strongest one
  available for this question;
* what was derived from it, and against which subject;
* what state the result is in -- where ``not measured`` is a state of its own
  and never collapses into ``pass``;
* whether a falsifier exists that would have caught the metric being wrong,
  and whether that falsifier was actually run and actually caught it.

**The trusted base is named, not assumed.** Verifying a verifier invites
infinite regress, so this module stops at a boundary and says where: git
objects, `ProjectState`, `RunState`, receipts, the conclusion of a named
external CI *step*, and cryptographic digests over those. Those are primitive
and deterministic enough to be believed without a meta-record of their own.
Anything else -- a console log, a report file, a remembered number -- is not
in the base and cannot be release-authoritative on its own.

## What this module cannot do, stated before anyone infers otherwise

An `AssuranceRecord` is a **declaration about** a measurement, not the
measurement. Nothing here opens the file that `identity` names or re-runs what
`derivation` describes. A caller who writes ``kind=REPOSITORY`` while having
read a console log produces a record this module will accept, and an
adversarial review demonstrated exactly that: the project's own false green
number two passes verbatim with one word changed.

Two things narrow it, and neither closes it:

* The `from_*` builders below **read the source themselves** and compute the
  digest from its bytes. A record they produce carries
  `Provenance.MEASURED`. A record assembled by hand carries
  `Provenance.DECLARED`, and for a release-critical metric that is a
  weakness on its own. So the honest path is the short one, and the dishonest
  path has to be written deliberately and shows up in the report.
* `derivation` is required, non-empty, and printed. A reviewer who reads the
  report sees what was claimed.

What remains is a gate against carelessness, not against a liar with commit
access. That is a real limitation and it is why this file says so here rather
than leaving a reader to discover it.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator

from .contracts import Strict


class SourceKind(StrEnum):
    """Where a number came from.

    `strength()` turns the intuition "some sources outlive others" into a
    number the rules can actually use. An earlier version of this file
    asserted the ordering in prose and never compared two kinds anywhere --
    which a reviewer pointed out is the same defect the module is about.
    """

    #: Git objects: commits, trees, blobs. Content-addressed and immutable.
    REPOSITORY = "repository"
    #: The persisted orchestration state.
    PROJECT_STATE = "project_state"
    #: The persisted state of a single run.
    RUN_STATE = "run_state"
    #: A receipt written by the runner, whose fields the model cannot fill.
    RECEIPT = "receipt"
    #: The conclusion of a **named step** in an external CI run. A job's own
    #: conclusion is not this: a job stays green while the step that mattered
    #: was skipped, which is exactly how false green number five happened.
    EXTERNAL_CI_STEP = "external_ci_step"
    #: A cryptographic digest over one of the above.
    DIGEST = "digest"
    #: Console output, a step log, a report file, an agent's own summary.
    #: Real evidence of something -- but it does not outlive the process, and
    #: a process that was killed logged nothing while still having acted.
    TRANSIENT_LOG = "transient_log"
    #: There is no source. Not "the source said zero": there was nothing to
    #: ask. Kept as a kind of its own so that `absence != zero` is
    #: representable rather than merely intended.
    ABSENT = "absent"

    def strength(self) -> int:
        """How much weight this kind carries. Higher wins.

        Git is above the rest because it is content-addressed and independent
        of this project's own code: a state file can be rewritten by the thing
        that writes state files, a commit cannot be rewritten without changing
        its name.
        """
        return {
            SourceKind.REPOSITORY: 5,
            SourceKind.DIGEST: 4,
            SourceKind.RECEIPT: 4,
            SourceKind.PROJECT_STATE: 3,
            SourceKind.RUN_STATE: 3,
            SourceKind.EXTERNAL_CI_STEP: 3,
            SourceKind.TRANSIENT_LOG: 1,
            SourceKind.ABSENT: 0,
        }[self]


#: The sources believed without a meta-record of their own. Everything here is
#: persistent, independently re-readable, and not produced by the thing being
#: measured. The boundary is explicit so that "who verifies the verifier" has
#: a written answer instead of an infinite regress.
TRUSTED_BASE: frozenset[SourceKind] = frozenset(
    {
        SourceKind.REPOSITORY,
        SourceKind.PROJECT_STATE,
        SourceKind.RUN_STATE,
        SourceKind.RECEIPT,
        SourceKind.EXTERNAL_CI_STEP,
        SourceKind.DIGEST,
    }
)

#: Kinds whose value is tied to a particular commit. A record from one of
#: these that does not say which commit it was measured at cannot be checked
#: for staleness, and staleness is the drift this project keeps finding.
HEAD_BOUND: frozenset[SourceKind] = frozenset(
    {SourceKind.REPOSITORY, SourceKind.PROJECT_STATE, SourceKind.RUN_STATE,
     SourceKind.RECEIPT}
)


class Provenance(StrEnum):
    """Did this module read the source, or was it told what the source said?"""

    #: Produced by one of the `from_*` builders, which opened the source and
    #: digested its bytes.
    MEASURED = "measured"
    #: Assembled by a caller. Every field is that caller's word.
    DECLARED = "declared"


class ResultState(StrEnum):
    """The state of a result -- where "did not happen" has several distinct
    shapes and none of them is a pass.

    Collapsing these into a boolean is what makes a skipped test look like a
    passing one. They are separate values so that a report can say *which*
    kind of not-measured it is, and so that a reader can tell an environment
    that cannot run something from a check that was never attempted.
    """

    #: Measured, and the measurement came out as required.
    VERIFIED = "VERIFIED"
    #: Measured, and it came out wrong. A real finding.
    FAILED = "FAILED"
    #: Not attempted. No claim in either direction.
    NOT_RUN = "NOT_RUN"
    #: Attempted, and the question could not be answered from the available
    #: source -- including the case where the source is empty because the
    #: mechanism that fills it was built afterwards.
    NOT_DETERMINABLE = "NOT_DETERMINABLE"
    #: The environment structurally cannot run this measurement. A fact about
    #: the environment, not about the product -- and not a defect either.
    UNSUPPORTED_ENVIRONMENT = "UNSUPPORTED_ENVIRONMENT"
    #: Was measured, and something has since invalidated it -- a new subject
    #: head, an amended spec, a superseded source.
    INVALIDATED = "INVALIDATED"


def not_a_pass() -> frozenset[ResultState]:
    """Every state that is not a pass, **derived** from the enum.

    The previous version kept this as a hand-written set with a docstring
    explaining that writing it out "forces a decision" when a state is added.
    It does not. A reviewer added a sixth state, forgot the set, and the
    closure went green on it while `authoritative()` said False -- a
    disagreement between two code paths that were supposed to encode one rule.
    Deriving it means a new state is not-a-pass by default, which is the only
    safe direction for this particular set.
    """
    return frozenset(s for s in ResultState if s is not ResultState.VERIFIED)


#: Kept as a module-level name because it reads well at call sites. It is a
#: derived value, not a maintained list.
NOT_A_PASS: frozenset[ResultState] = not_a_pass()


class FalsifierState(StrEnum):
    """Did a negative control run, and did it catch the thing it targets?

    A check that has never been seen to fail is not known to be a check. Two
    of the five false greens were tools that could not fail: one compared
    strings that never matched, one ran over a list that was always short
    enough. Both would have been caught in a minute by deliberately breaking
    the property and seeing whether the tool noticed.
    """

    #: The negative control was run and the metric caught it. The only state
    #: that demonstrates the metric can fail -- and it requires a
    #: `falsifier_detail` saying what was broken, because an unevidenced
    #: KILLED is a tick-box.
    KILLED = "KILLED"
    #: The negative control was run and the metric did **not** notice. The
    #: metric is broken, whatever it currently reports.
    ESCAPED = "ESCAPED"
    #: No negative control was run.
    NOT_RUN = "NOT_RUN"
    #: The metric is a direct read of a digest with no derivation that could
    #: be wrong. Confined to `DIGEST` sources: it used to admit `REPOSITORY`
    #: too, and since most of this project's real metrics are git-derived with
    #: non-trivial derivations, that was not an escape hatch but the main road.
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvidenceSource(Strict):
    """Where a metric read its value, named concretely enough to re-read."""

    kind: SourceKind
    #: What to open to check this: a path, a commit sha, a run id, a workflow
    #: run and step name. Empty only for `ABSENT`.
    identity: str = ""
    #: A digest over the source's content where one exists. Two runs quoting
    #: the same source with different digests are quoting different things,
    #: and `AssuranceRecord.weaknesses` says so when a cross-check disagrees.
    digest: str | None = None
    #: The commit this source was read at, where the source is repository-
    #: derived. A green whose subject is not the current head is stale, and
    #: `closure()` says so rather than counting it.
    subject_head: str | None = None

    @model_validator(mode="after")
    def _absent_has_no_identity(self) -> EvidenceSource:
        if self.kind is SourceKind.ABSENT and (self.identity or self.digest):
            raise ValueError(
                "an ABSENT source cannot name an identity or a digest -- if "
                "there is something to open, the kind is wrong"
            )
        if self.kind is not SourceKind.ABSENT and not self.identity:
            raise ValueError(
                f"a {self.kind.value} source must name what to open; an "
                "unnamed source cannot be re-read and therefore cannot be checked"
            )
        return self


class AssuranceRecord(Strict):
    """One metric, together with everything needed to disbelieve it."""

    metric_id: str = Field(min_length=1)
    #: Where the value came from.
    source: EvidenceSource
    #: How the value was obtained from the source, in a sentence. Not
    #: decoration: it is what a reviewer checks when the number looks wrong,
    #: and it is the only defence against a mislabelled `kind`.
    derivation: str = Field(min_length=1)
    #: The measured value, as measured. Kept raw so that a later reader can
    #: re-derive the state from it instead of trusting the state field. A
    #: VERIFIED record must carry one: "verified" without a value is a claim
    #: about nothing.
    measured: object = None
    state: ResultState = ResultState.NOT_RUN
    falsifier: FalsifierState = FalsifierState.NOT_RUN
    #: What the negative control did, concretely: what was broken and what the
    #: metric reported when it was. Required for KILLED.
    falsifier_detail: str = ""
    #: A second, independent source that was consulted and agreed. Optional --
    #: but for the metrics this project got wrong, the cheap cross-check
    #: (state *and* git) would have caught every one.
    cross_check: EvidenceSource | None = None
    #: Release-critical metrics are held to the falsifier rule. Ordinary
    #: diagnostics are not: demanding a negative control for every number
    #: would make the rule something people route around. A closure decides
    #: separately which metrics it *requires*, so lowering this flag cannot
    #: make a required metric disappear.
    critical: bool = True
    #: A stronger source that exists for this question but was not used.
    #: Naming it is what turns "I used the log" into a visible weakness --
    #: and the comparison is by `SourceKind.strength()`, so naming a *weaker*
    #: alternative is not a weakness and is rejected as a mistake.
    stronger_source_available: SourceKind | None = None
    #: Whether this module read the source or was told about it. The builders
    #: set MEASURED; anything hand-assembled is DECLARED.
    provenance: Provenance = Provenance.DECLARED
    note: str = ""

    @model_validator(mode="after")
    def _states_are_consistent(self) -> AssuranceRecord:
        if self.source.kind is SourceKind.ABSENT and self.state is ResultState.VERIFIED:
            raise ValueError(
                f"{self.metric_id}: nothing was read, so nothing was verified. "
                "absence is not a measured zero"
            )
        if (
            self.falsifier is FalsifierState.NOT_APPLICABLE
            and self.source.kind is not SourceKind.DIGEST
        ):
            raise ValueError(
                f"{self.metric_id}: NOT_APPLICABLE is for a direct read of a "
                f"digest, not for a {self.source.kind.value} value that was "
                "derived -- and most of this project's metrics are derived"
            )
        if self.falsifier is FalsifierState.KILLED and not self.falsifier_detail:
            raise ValueError(
                f"{self.metric_id}: KILLED without a falsifier_detail is a "
                "tick-box. Say what was broken and what the metric reported "
                "when it was"
            )
        if self.state is ResultState.VERIFIED and self.measured is None:
            raise ValueError(
                f"{self.metric_id}: VERIFIED without a measured value is a "
                "claim about nothing"
            )
        stronger = self.stronger_source_available
        if stronger is not None and stronger.strength() <= self.source.kind.strength():
            raise ValueError(
                f"{self.metric_id}: {stronger.value} is not stronger than "
                f"{self.source.kind.value}; this field names a source that "
                "should have been used instead"
            )
        return self

    # -- The rules ---------------------------------------------------------- #

    def weaknesses(self, *, subject_head: str | None = None) -> list[str]:
        """Every reason this record may not be believed, in plain words.

        Returns a list rather than a boolean because a record can be weak in
        more than one way at once, and fixing one of three is not progress
        anybody should be able to report as green.
        """
        offen: list[str] = []

        # Any state that is not VERIFIED. Derived from the enum, so a state
        # added later is caught by default rather than by remembering.
        if self.state is ResultState.FAILED:
            offen.append("the metric measured a failure")
        elif self.state in NOT_A_PASS:
            offen.append(f"state is {self.state.value}, which is not a pass")

        # absence != zero, and empty source != measured zero.
        if self.source.kind is SourceKind.ABSENT:
            offen.append(
                "no source was read; the result can only be NOT_DETERMINABLE"
            )

        # A stronger source existed and was not used -- whatever the kind. The
        # first version applied this only to logs, so a record honestly
        # declaring "I read run state where git was the answer" passed clean.
        if self.stronger_source_available is not None:
            offen.append(
                f"read from {self.source.kind.value} while "
                f"{self.stronger_source_available.value} was available for "
                "the same question"
            )

        # logs are not authoritative for anything release-critical.
        if self.source.kind is SourceKind.TRANSIENT_LOG and self.critical:
            offen.append(
                "a release-critical metric read from a transient log; a log "
                "does not outlive the process, and a process that was "
                "killed logged nothing while still having acted"
            )

        # a critical metric without a working falsifier cannot be authoritative.
        if self.critical:
            if self.falsifier is FalsifierState.ESCAPED:
                offen.append(
                    "the negative control was not caught: this metric has been "
                    f"shown unable to fail ({self.falsifier_detail or 'no detail'})"
                )
            elif self.falsifier is FalsifierState.NOT_RUN:
                offen.append(
                    "no negative control was run, so it is unknown whether this "
                    "metric can fail at all"
                )
            if self.provenance is Provenance.DECLARED:
                offen.append(
                    "hand-assembled: this module did not read the source it "
                    "names. Use one of the from_* builders, or accept that "
                    "every field here is the caller's word"
                )

        # stale evidence from another head is not current evidence.
        if subject_head:
            if self.source.subject_head and self.source.subject_head != subject_head:
                offen.append(
                    f"measured at {self.source.subject_head[:12]}, but the "
                    f"subject is {subject_head[:12]}"
                )
            elif not self.source.subject_head and self.source.kind in HEAD_BOUND:
                offen.append(
                    f"a {self.source.kind.value} value that does not say which "
                    "commit it was measured at cannot be checked for staleness"
                )

        # A source outside the trusted base needs a cross-check to stand.
        if self.critical and self.source.kind not in TRUSTED_BASE:
            if self.cross_check is None:
                offen.append(
                    f"{self.source.kind.value} is outside the trusted base and "
                    "no independent cross-check was recorded"
                )
            elif self.cross_check.kind not in TRUSTED_BASE:
                offen.append(
                    "the cross-check is itself outside the trusted base, which "
                    "moves the question rather than answering it"
                )

        # A cross-check quoting the same thing with a different digest is not
        # agreement, it is the drift this field was declared to detect.
        cc = self.cross_check
        if cc is not None and cc.identity == self.source.identity:
            if cc.digest and self.source.digest and cc.digest != self.source.digest:
                offen.append(
                    f"the cross-check quotes {cc.identity} with digest "
                    f"{cc.digest[:12]} while the source says "
                    f"{self.source.digest[:12]}: two different things with one name"
                )
        return offen

    def authoritative(self, *, subject_head: str | None = None) -> bool:
        """May this record be used to call a release green?

        Deliberately derived from `weaknesses()` alone. The state check used to
        live here *and* in a hand-maintained set, and the two disagreed.
        """
        return not self.weaknesses(subject_head=subject_head)


# --------------------------------------------------------------------------- #
# Builders that actually read the source
# --------------------------------------------------------------------------- #


def _digest_bytes(roh: bytes) -> str:
    return hashlib.sha256(roh).hexdigest()[:16]


def git_head(repo: Path | str) -> str:
    """The current commit of a working tree, or "" when there is none."""
    p = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    return p.stdout.strip() if p.returncode == 0 else ""


def from_git(
    metric_id: str,
    *,
    repo: Path | str,
    argv: list[str],
    derivation: str,
    interpret,
    falsifier_detail: str = "",
    falsifier: FalsifierState = FalsifierState.NOT_RUN,
    critical: bool = True,
    subject_head: str | None = None,
) -> AssuranceRecord:
    """Runs a git command and builds a record from what it actually printed.

    `interpret` receives the completed process and returns
    ``(measured, ResultState)``. Keeping interpretation in the caller and
    *reading* in here is the split that matters: the caller cannot claim to
    have read git without git having run.
    """
    p = subprocess.run(
        ["git", "-C", str(repo), *argv], capture_output=True, text=True
    )
    gemessen, zustand = interpret(p)
    return AssuranceRecord(
        metric_id=metric_id,
        source=EvidenceSource(
            kind=SourceKind.REPOSITORY,
            identity=f"git {' '.join(argv)} @ {repo}",
            digest=_digest_bytes(p.stdout.encode()),
            # `subject_head` is what the *closure* is about, which is not always
            # the repository being read: evidence pinned inside this project --
            # a bundle, a fixture -- has its own head, and comparing that with
            # the project's would report drift on every commit. The caller says
            # which subject the record belongs to; the read repository's own
            # head goes in the note, where it stays checkable.
            subject_head=subject_head if subject_head is not None else git_head(repo),
        ),
        note=f"read at {git_head(repo)[:12] or 'unknown'} in {repo}",
        derivation=derivation,
        measured=gemessen,
        state=zustand,
        falsifier=falsifier,
        falsifier_detail=falsifier_detail,
        critical=critical,
        provenance=Provenance.MEASURED,
    )


def from_json_state(
    metric_id: str,
    *,
    path: Path | str,
    kind: SourceKind,
    derivation: str,
    interpret,
    subject_head: str | None = None,
    falsifier_detail: str = "",
    falsifier: FalsifierState = FalsifierState.NOT_RUN,
    critical: bool = True,
) -> AssuranceRecord:
    """Opens a persisted JSON state and builds a record from its content.

    A missing file is `NOT_DETERMINABLE` with an `ABSENT` source -- never a
    measured zero. That single line is false green number four.
    """
    p = Path(path)
    if not p.exists():
        return AssuranceRecord(
            metric_id=metric_id,
            source=EvidenceSource(kind=SourceKind.ABSENT),
            derivation=f"{derivation} (the file does not exist: {p})",
            measured=None,
            state=ResultState.NOT_DETERMINABLE,
            falsifier=falsifier,
            falsifier_detail=falsifier_detail,
            critical=critical,
            provenance=Provenance.MEASURED,
        )
    roh = p.read_bytes()
    gemessen, zustand = interpret(json.loads(roh.decode("utf-8")))
    return AssuranceRecord(
        metric_id=metric_id,
        source=EvidenceSource(
            kind=kind, identity=str(p), digest=_digest_bytes(roh),
            subject_head=subject_head,
        ),
        derivation=derivation,
        measured=gemessen,
        state=zustand,
        falsifier=falsifier,
        falsifier_detail=falsifier_detail,
        critical=critical,
        provenance=Provenance.MEASURED,
    )


def from_ci_step(
    metric_id: str,
    *,
    run_id: str,
    step_name: str,
    conclusion: str,
    derivation: str,
    falsifier_detail: str = "",
    falsifier: FalsifierState = FalsifierState.NOT_RUN,
    head: str | None = None,
    critical: bool = True,
) -> AssuranceRecord:
    """A record for one **named step** of an external CI run.

    `skipped` becomes `UNSUPPORTED_ENVIRONMENT`, not a pass, and not a failure
    either. That is false green number five: the job was green, and the step
    that was supposed to measure the sandbox never ran.
    """
    zustand = {
        "success": ResultState.VERIFIED,
        "failure": ResultState.FAILED,
        "skipped": ResultState.UNSUPPORTED_ENVIRONMENT,
        "cancelled": ResultState.NOT_RUN,
    }.get(conclusion, ResultState.NOT_DETERMINABLE)
    return AssuranceRecord(
        metric_id=metric_id,
        source=EvidenceSource(
            kind=SourceKind.EXTERNAL_CI_STEP,
            identity=f"run/{run_id}#{step_name}",
            subject_head=head,
        ),
        derivation=derivation,
        measured=conclusion,
        state=zustand,
        falsifier=falsifier,
        falsifier_detail=falsifier_detail,
        critical=critical,
        provenance=Provenance.MEASURED,
    )


class AssuranceClosure(Strict):
    """The verdict over a set of records, with every reason it is not green."""

    subject_head: str | None = None
    records: list[AssuranceRecord] = Field(default_factory=list)
    #: Metric ids that must be present *and* authoritative. Separate from
    #: `AssuranceRecord.critical`, which a record sets about itself: a record
    #: cannot excuse itself from a requirement the closure imposes, and a
    #: requirement with no record at all is a missing measurement rather than
    #: a silent pass.
    required: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ids_are_unique(self) -> AssuranceClosure:
        ids = [r.metric_id for r in self.records]
        doppelt = sorted({i for i in ids if ids.count(i) > 1})
        if doppelt:
            raise ValueError(
                "duplicate metric_id(s) in one closure: "
                + ", ".join(doppelt)
                + " -- two records under one name means the report shows one "
                "record's reasons against the other's row"
            )
        return self

    def problems(self) -> dict[str, list[str]]:
        """Per metric, the reasons it is not authoritative.

        Includes the required metrics that have no record at all: a
        requirement nobody measured is the most complete way to be wrong about
        it.
        """
        raus: dict[str, list[str]] = {}
        for r in self.records:
            gruende = r.weaknesses(subject_head=self.subject_head)
            if gruende:
                raus[r.metric_id] = gruende
        vorhanden = {r.metric_id for r in self.records}
        for verlangt in self.required:
            if verlangt not in vorhanden:
                raus[verlangt] = ["required, and no record was produced for it"]
        return raus

    def green(self) -> bool:
        """True only when every record is authoritative and nothing required
        is missing.

        Note what this deliberately does not do: there is no threshold, no
        "mostly green", and an empty record set is **not** green. Zero gates
        passing is the shape of a check that was never wired up, and this
        project has shipped that mistake once already.
        """
        if not self.records:
            return False
        return not self.problems()

    def by_state(self) -> dict[str, int]:
        zaehler: dict[str, int] = {s.value: 0 for s in ResultState}
        for r in self.records:
            zaehler[r.state.value] += 1
        return {k: v for k, v in zaehler.items() if v}

    def report(self) -> str:
        zeilen = [
            f"meta-evidence closure over {len(self.records)} metric(s)"
            + (f" at {self.subject_head[:12]}" if self.subject_head else "")
        ]
        probleme = self.problems()
        for r in sorted(self.records, key=lambda x: x.metric_id):
            marke = "OK " if r.metric_id not in probleme else "NOT"
            zeilen.append(
                f"  {marke}  {r.metric_id:<38s} {r.state.value:<24s} "
                f"{r.source.kind.value}"
            )
            for g in probleme.get(r.metric_id, []):
                zeilen.append(f"          - {g}")
        for fehlt in sorted(set(probleme) - {r.metric_id for r in self.records}):
            zeilen.append(f"  NOT  {fehlt:<38s} {'MISSING':<24s} -")
            for g in probleme[fehlt]:
                zeilen.append(f"          - {g}")
        zeilen.append(f"  => {'GREEN' if self.green() else 'NOT GREEN'}")
        return "\n".join(zeilen)
