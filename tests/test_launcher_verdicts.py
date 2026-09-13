"""How a real run's state becomes a verdict the orchestrator can act on.

This is the seam where an orchestrator can do the most damage, so it gets its
own module. A `RunState` carries no field saying "accepted": it carries a
stage, a condition, and a `last_accepted_candidate` that may be left over from
an earlier iteration entirely. Reading that wrongly means merging something
that was never accepted, or merging the same candidate twice.

Every case below is constructed rather than dispatched, so the mapping is
tested without agent quota. The real end-to-end run is a separate exercise;
what is pinned here is the translation.
"""

from __future__ import annotations

import pytest

from hoh.contracts import Budgets, Candidate, Condition, RunState, Stage, Usage
from hoh.launcher import HohRunLauncher
from hoh.orchestrator import RunOutcome, RunVerdict
from hoh.project import ActionClass, TaskNode


def kandidat(cid: str, commit: str = "abc1234") -> Candidate:
    return Candidate(
        candidate_id=cid, repo_path="/tmp/x", commit=commit,
        tree_clean=True, tree_digest="d" * 16,
    )


def zustand(tmp_path, stage=Stage.CHECKPOINTED, condition=Condition.ACTIVE, **kw) -> RunState:
    return RunState(
        run_id="r", repo_path=str(tmp_path), project_name="p",
        spec_path=str(tmp_path / "spec.md"), spec_digest="s" * 16,
        policy_digest="p" * 16, profile_digest="f" * 16,
        stage=stage, condition=condition, **kw,
    )


@pytest.fixture
def starter(tmp_path):
    return HohRunLauncher(tmp_path, tmp_path)


class Leer:
    exit_code = 0
    stdout = ""
    stderr = ""


# --------------------------------------------------------------------------- #
# Acceptance, and the thing that looks exactly like it
# --------------------------------------------------------------------------- #

def test_ein_neuer_angenommener_kandidat_ist_eine_annahme(starter, tmp_path):
    st = zustand(tmp_path, last_accepted_candidate=kandidat("r-i2"))
    ergebnis = starter._verdict(st, None, Leer())
    assert ergebnis.verdict is RunVerdict.ACCEPTED
    assert "r-i2" in ergebnis.detail


def test_derselbe_kandidat_wie_vorher_ist_KEINE_annahme(starter, tmp_path):
    """The case that would merge the same work twice.

    A run can finish CHECKPOINTED having accepted nothing new -- it ends where
    it started. The stage is identical to a genuine acceptance; only the
    candidate tells them apart.
    """
    vorher = kandidat("r-i1")
    st = zustand(tmp_path, last_accepted_candidate=kandidat("r-i1"))
    ergebnis = starter._verdict(st, vorher, Leer())
    assert ergebnis.verdict is RunVerdict.REJECTED
    assert "no new candidate" in ergebnis.detail


def test_checkpointed_ohne_kandidat_ist_unentschieden(starter, tmp_path):
    st = zustand(tmp_path, last_accepted_candidate=None)
    assert starter._verdict(st, None, Leer()).verdict is RunVerdict.UNDETERMINED


def test_ready_for_delivery_zaehlt_wie_checkpointed(starter, tmp_path):
    st = zustand(tmp_path, stage=Stage.READY_FOR_DELIVERY,
                 last_accepted_candidate=kandidat("r-i3"))
    assert starter._verdict(st, None, Leer()).verdict is RunVerdict.ACCEPTED


# --------------------------------------------------------------------------- #
# A provider outage is not a rejection
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("art", ["usage_quota", "rate_limit", "provider", "QUOTA"])
def test_provider_blockade_ist_keine_ablehnung(starter, tmp_path, art):
    """Recording an outage as a rejection burns the node's retry budget on
    somebody else's downtime."""
    st = zustand(tmp_path, stage=Stage.VERIFYING, condition=Condition.BLOCKED,
                 blocked_kind=art, blocked_reason="429 from the provider")
    ergebnis = starter._verdict(st, None, Leer())
    assert ergebnis.verdict is RunVerdict.PROVIDER_UNAVAILABLE
    assert "provider" in ergebnis.detail


def test_andere_blockade_wird_nicht_als_ablehnung_geraten(starter, tmp_path):
    """Blocked for an unclassified reason is not a rejection either.

    It is a state nobody has classified, and guessing is the thing this design
    exists to avoid.
    """
    st = zustand(tmp_path, stage=Stage.VERIFYING, condition=Condition.BLOCKED,
                 blocked_kind="awaiting_approval", blocked_reason="a human must decide")
    ergebnis = starter._verdict(st, None, Leer())
    assert ergebnis.verdict is RunVerdict.UNDETERMINED
    assert "awaiting_approval" in ergebnis.detail


def test_erschoepftes_budget_mitten_im_lauf_ist_kein_fehlschlag(starter, tmp_path):
    st = zustand(
        tmp_path, stage=Stage.DEVELOPING,
        budgets=Budgets(max_iterations=1),
        usage=Usage(iterations=1),
    )
    ergebnis = starter._verdict(st, None, Leer())
    assert ergebnis.verdict is RunVerdict.PROVIDER_UNAVAILABLE
    assert "budget" in ergebnis.detail.lower()


def test_unfertiger_lauf_ohne_budgetgrenze_ist_eine_ablehnung(starter, tmp_path):
    """Iterations spent without reaching a checkpoint: the loop did its work
    and produced nothing acceptable. That is a rejection, and a rejection is
    input to the next iteration."""
    st = zustand(tmp_path, stage=Stage.VERIFYING)
    ergebnis = starter._verdict(st, None, Leer())
    assert ergebnis.verdict is RunVerdict.REJECTED
    assert "without a checkpoint" in ergebnis.detail


@pytest.mark.parametrize("cond", [Condition.FAILED, Condition.CANCELLED])
def test_abgebrochener_lauf_ist_unentschieden(starter, tmp_path, cond):
    st = zustand(tmp_path, stage=Stage.DEVELOPING, condition=cond,
                 stop_reason="operator cancelled")
    assert starter._verdict(st, None, Leer()).verdict is RunVerdict.UNDETERMINED


# --------------------------------------------------------------------------- #
# Parallelism is measured, not assumed
# --------------------------------------------------------------------------- #

def test_ueberlappende_schreibmengen_sind_abhaengig(starter):
    a = TaskNode(id="a", writes=["README.md"])
    b = TaskNode(id="b", writes=["README.md", "docs/x.md"])
    assert starter.depends_on(a, b)


def test_schreiben_auf_das_ein_anderer_liest_ist_abhaengig(starter):
    """The case that actually bit this project: disjoint write sets, and still
    a conflict, because one run's correctness depends on what the other wrote.
    Both merges that produced the union gate had this shape.
    """
    a = TaskNode(id="a", writes=["EXPORT_MANIFEST.json"])
    b = TaskNode(id="b", writes=["CLAIMS.json"], semantic_reads=["EXPORT_MANIFEST.json"])
    assert not (set(a.writes) & set(b.writes)), "the write sets are disjoint"
    assert starter.depends_on(a, b)
    assert starter.depends_on(b, a), "the relation is symmetric"


def test_wirklich_unabhaengige_knoten_werden_nicht_serialisiert(starter):
    """A blanket rule would be safe and would also throw away every parallel
    round. Only a measured dependency serialises."""
    a = TaskNode(id="a", writes=["docs/a.md"], semantic_reads=["src/hoh/a.py"])
    b = TaskNode(id="b", writes=["docs/b.md"], semantic_reads=["src/hoh/b.py"])
    assert not starter.depends_on(a, b)


def test_aktionsklasse_kommt_aus_dem_zustand_nicht_aus_der_spec(starter):
    """Whether a node is externally irreversible is policy, and policy lives in
    the state where a restart preserves it -- not re-inferred each round, where
    a wording change could quietly downgrade it."""
    assert starter.action_class(TaskNode(id="x")) is ActionClass.INTERNAL
    assert starter.action_class(
        TaskNode(id="push", action_class=ActionClass.EXTERNAL)
    ) is ActionClass.EXTERNAL


# --------------------------------------------------------------------------- #
# The dry run promises nothing
# --------------------------------------------------------------------------- #

def test_trockenlauf_meldet_NOT_RUN_und_mergt_nicht(tmp_path):
    starter = HohRunLauncher(tmp_path, tmp_path, dry_run=True)
    ergebnis = starter.launch(TaskNode(id="a"))
    assert ergebnis.verdict is RunVerdict.NOT_RUN
    assert starter.merge(TaskNode(id="a"), ergebnis).landed is False


# --------------------------------------------------------------------------- #
# Waiting for a person is not the same as not knowing
# --------------------------------------------------------------------------- #

def test_trust_dialog_ist_freigabebedarf_nicht_unklar(starter, tmp_path):
    """HoH will not answer a trust dialog, and that is deliberate: granting
    trust to a directory it was merely pointed at is the one capability it
    refuses to take. So a run can stop there.

    Reporting that as "unclassifiable" would send someone looking for a defect
    instead of answering the prompt. Found the first time a repair node was
    dispatched for real -- and note that `blocked_kind` was None, so the case
    is recognisable only from the reason text.
    """
    st = zustand(
        tmp_path, stage=Stage.DEVELOPING, condition=Condition.BLOCKED,
        stop_reason=("iteration aborted: developer waits for an approval in pane w17:p5Y. "
                     "A blocked dialog is not answered automatically.\n"
                     "--- Visible in pane ---\nYes, I trust this folder"),
    )
    ergebnis = starter._verdict(st, None, Leer())
    assert ergebnis.verdict is RunVerdict.NEEDS_APPROVAL
    assert "approval" in ergebnis.detail.lower()
    # One line, not the whole pane dump: a halt reason a person has to scroll
    # is a halt reason nobody reads.
    assert "\n" not in ergebnis.detail


def test_freigabebedarf_haelt_als_captain_gate_an(tmp_path):
    """It halts BLOCKED_EXTERNAL -- the class that legitimately needs a human --
    rather than AMBIGUOUS."""
    from hoh.orchestrator import HaltClass, ProjectController, RunOutcome
    from hoh.project import GateOutcome, GateResult, ProjectState
    from hoh.projectstore import ProjectStore

    class Wartet:
        def action_class(self, node): return ActionClass.INTERNAL
        def depends_on(self, a, b): return False
        def prepare(self, node): return None
        def accepted_baseline(self, node): return None
        def evaluate(self, node): return RunOutcome(RunVerdict.UNDETERMINED, "")
        def launch(self, node):
            return RunOutcome(RunVerdict.NEEDS_APPROVAL, "trust dialog in pane w17")
        def merge(self, node, outcome): raise AssertionError("must not merge")

    class Gruen:
        def subject(self): return "abc1234"
        def run(self, subject):
            return [GateResult(name="g", outcome=GateOutcome.GREEN, subject=subject)]

    s = ProjectStore(tmp_path, "p")
    st = ProjectState(project_id="p", repo_path=str(tmp_path))
    st.nodes = [TaskNode(id="a")]
    s.create(st)
    ergebnis = ProjectController(s, Wartet(), Gruen()).run()
    assert ergebnis.halt is HaltClass.BLOCKED_EXTERNAL, ergebnis.reason
    assert "trust dialog" in ergebnis.reason


# --------------------------------------------------------------------------- #
# A refused merge is a known state when git said why
# --------------------------------------------------------------------------- #

def test_echter_konflikt_wird_als_konflikt_erkannt(starter):
    """Observed verbatim in the first real repair cycle."""
    from hoh.launcher import HohRunLauncher as L
    from hoh.orchestrator import MergeFailure

    text = (
        "CONFLICT (modify/delete): __pycache__/slug.cpython-313.pyc deleted in HEAD "
        "and modified in hoh-repair-2-1.  Version hoh-repair-2-1 of "
        "__pycache__/slug.cpython-313.pyc left in tree.\n"
        "Automatic merge failed; fix conflicts and then commit the result.\n"
    )
    assert L._classify(text) is MergeFailure.CONFLICT
    assert "__pycache__/slug.cpython-313.pyc" in L._conflicting_paths(text)


def test_verstellter_arbeitsbaum_ist_kein_konflikt(starter):
    """Also observed verbatim, and it needs a different remedy: clear the tree,
    not reconcile content. Git prints this *before* attempting any merge, so
    the two messages never appear together."""
    from hoh.launcher import HohRunLauncher as L
    from hoh.orchestrator import MergeFailure

    text = (
        "error: The following untracked working tree files would be overwritten by merge:\n"
        "\t__pycache__/slug.cpython-313.pyc\n"
        "Please move or remove them before you merge.\nAborting\n"
    )
    assert L._classify(text) is MergeFailure.OBSTRUCTED

    text2 = (
        "error: Your local changes to the following files would be overwritten by merge:\n"
        "\ttests/__pycache__/test_slug.cpython-313-pytest-9.0.3.pyc\n"
        "Please commit your changes or stash them before you merge.\nAborting\n"
    )
    assert L._classify(text2) is MergeFailure.OBSTRUCTED


@pytest.mark.parametrize("text", [
    "fatal: refusing to merge unrelated histories",
    "error: object file .git/objects/ab/cdef is empty",
    "fatal: Not possible to fast-forward, aborting.",
    "",
    "something nobody has seen before",
])
def test_unbekannter_mergefehler_wird_nicht_zum_konflikt_gemacht(text):
    """The negative control.

    A misclassified failure is worse than an unclassified one: it sends the
    reader somewhere specific and wrong. Anything git did not clearly describe
    stays UNKNOWN, and the controller halts AMBIGUOUS for it -- which is the
    honest verdict when the tool did not say.
    """
    from hoh.launcher import HohRunLauncher as L
    from hoh.orchestrator import MergeFailure

    assert L._classify(text) is MergeFailure.UNKNOWN


def test_der_halt_traegt_die_konfliktdetails(tmp_path):
    """The halt names the branch, the head, the merge base and the paths --
    the evidence git handed over, rather than a summary of it."""
    from hoh.orchestrator import (
        GateRunner, HaltClass, MergeFailure, MergeResult, ProjectController, RunLauncher,
    )
    from hoh.project import GateOutcome, GateResult, Lifecycle, ProjectState
    from hoh.projectstore import ProjectStore

    class Kollidiert(RunLauncher):
        def action_class(self, node): return ActionClass.INTERNAL
        def depends_on(self, a, b): return False
        def prepare(self, node): return None
        def accepted_baseline(self, node): return None
        def evaluate(self, node): return RunOutcome(RunVerdict.UNDETERMINED, "")
        def launch(self, node): return RunOutcome(RunVerdict.ACCEPTED, "ok", candidate="c1")
        def merge(self, node, outcome):
            return MergeResult(
                landed=False, failure=MergeFailure.CONFLICT,
                detail="Automatic merge failed; fix conflicts",
                conflicting_paths=("slug.py",), branch="hoh-a",
                target_head_before="abc1234", merge_base="def5678", candidate="c1",
            )

    class Gruen(GateRunner):
        def subject(self): return "abc1234"
        def run(self, subject):
            return [GateResult(name="g", outcome=GateOutcome.GREEN, subject=subject)]

    s = ProjectStore(tmp_path, "p")
    st = ProjectState(project_id="p", repo_path=str(tmp_path))
    st.nodes = [TaskNode(id="a")]
    s.create(st)
    ergebnis = ProjectController(s, Kollidiert(), Gruen()).run()

    assert ergebnis.halt is HaltClass.MERGE_CONFLICT, ergebnis.reason
    for erwartet in ("hoh-a", "abc1234", "def5678", "slug.py", "content conflict"):
        assert erwartet in ergebnis.reason, f"{erwartet!r} missing from: {ergebnis.reason}"
    assert s.read_state().node("a").lifecycle is Lifecycle.BLOCKED


def test_unbekannter_mergefehler_haelt_weiterhin_ambiguous_an(tmp_path):
    from hoh.orchestrator import (
        GateRunner, HaltClass, MergeFailure, MergeResult, ProjectController, RunLauncher,
    )
    from hoh.project import GateOutcome, GateResult, ProjectState
    from hoh.projectstore import ProjectStore

    class Raetselhaft(RunLauncher):
        def action_class(self, node): return ActionClass.INTERNAL
        def depends_on(self, a, b): return False
        def prepare(self, node): return None
        def accepted_baseline(self, node): return None
        def evaluate(self, node): return RunOutcome(RunVerdict.UNDETERMINED, "")
        def launch(self, node): return RunOutcome(RunVerdict.ACCEPTED, "ok")
        def merge(self, node, outcome):
            return MergeResult(landed=False, failure=MergeFailure.UNKNOWN,
                               detail="fatal: refusing to merge unrelated histories")

    class Gruen(GateRunner):
        def subject(self): return "abc1234"
        def run(self, subject):
            return [GateResult(name="g", outcome=GateOutcome.GREEN, subject=subject)]

    s = ProjectStore(tmp_path, "p")
    st = ProjectState(project_id="p", repo_path=str(tmp_path))
    st.nodes = [TaskNode(id="a")]
    s.create(st)
    ergebnis = ProjectController(s, Raetselhaft(), Gruen()).run()
    assert ergebnis.halt is HaltClass.AMBIGUOUS
    assert "did not say why" in ergebnis.reason


def test_ein_nie_begonnener_lauf_ist_nicht_unklar(starter, tmp_path):
    """The deadlock the first unattended run hit.

    A process died between marking a node RUNNING and dispatching it. The run
    existed at stage NEW with no iteration and no candidate, `evaluate` called
    that UNDETERMINED, and the controller halted on it -- then re-read the same
    state and halted again, every round, forever.

    NEW with nothing behind it is the most knowable state a run can be in.
    Nothing was spent, so nothing can be repeated: it is simply work again.
    """
    st = zustand(tmp_path, stage=Stage.NEW, last_accepted_candidate=None)
    ergebnis = starter._verdict(st, None, Leer())
    assert ergebnis.verdict is RunVerdict.NOT_STARTED
    assert "has not begun" in ergebnis.detail


def test_ein_begonnener_lauf_ist_nicht_ungestartet(starter, tmp_path):
    """The distinction has to hold in the other direction, or the fix would
    re-dispatch runs that are already going."""
    laeuft = zustand(tmp_path, stage=Stage.DEVELOPING, iteration=1)
    assert starter._verdict(laeuft, None, Leer()).verdict is not RunVerdict.NOT_STARTED

    fertig = zustand(tmp_path, stage=Stage.CHECKPOINTED,
                     last_accepted_candidate=kandidat("r-i1"))
    assert starter._verdict(fertig, None, Leer()).verdict is RunVerdict.ACCEPTED


# --------------------------------------------------------------------------- #
# An acceptance is not undone by a later block
# --------------------------------------------------------------------------- #


def _blockiert(tmp_path, *, accepted=None, kind="plan-binding", reason="x"):
    z = zustand(tmp_path, stage=Stage.PLANNING, condition=Condition.BLOCKED)
    z.blocked_kind = kind
    z.blocked_reason = reason
    if accepted:
        z.last_accepted_candidate = kandidat(accepted, "c" * 40)
    return z


def test_a_run_that_accepted_and_then_blocked_is_still_accepted(tmp_path):
    """Measured in the benchmark's arm C.

    `to_roman` accepted `ntoroman-i2`, kept iterating because the budget
    allowed it, and blocked at iteration 4 on a plan whose `base_candidate_id`
    named a *rejected* candidate -- which the product refused, correctly. The
    node then came back UNDETERMINED and the arm produced no final state on a
    task whose work had been finished two iterations earlier.

    The acceptance happened, it is durable, and the candidate is in the state.
    The block is still reported -- in the detail, where a reader sees it.
    """
    l = HohRunLauncher(tmp_path, tmp_path)
    z = _blockiert(
        tmp_path, accepted="ntoroman-i2",
        reason="The plan is not bound to this run: base_candidate_id "
               "'ntoroman-i3' instead of 'ntoroman-i2'",
    )
    out = l._verdict(z, None, None)
    assert out.verdict is RunVerdict.ACCEPTED
    assert "ntoroman-i2" in out.detail
    assert "BLOCKED" in out.detail, "the block has to stay visible"
    assert "base_candidate_id" in out.detail, "and so does its reason"
    assert out.candidate == "c" * 40


def test_a_block_with_nothing_accepted_stays_undetermined(tmp_path):
    """The control. Reporting a block as an acceptance in general would be the
    guess this whole design exists to avoid."""
    l = HohRunLauncher(tmp_path, tmp_path)
    z = _blockiert(tmp_path, kind="disk", reason="no space left on device")
    assert l._verdict(z, None, None).verdict is RunVerdict.UNDETERMINED


def test_a_block_after_re_accepting_the_same_candidate_is_a_rejection(tmp_path):
    """If the accepted candidate is the one the node already merged, the run
    ended where it started. That is a rejection -- the same answer this
    function gives for the same situation in CHECKPOINTED -- and a block does
    not turn it into progress. Merging on it would apply one candidate twice.
    """
    l = HohRunLauncher(tmp_path, tmp_path)
    z = _blockiert(tmp_path, accepted="ntoroman-i2")
    out = l._verdict(z, "ntoroman-i2", None)
    assert out.verdict is RunVerdict.REJECTED
    assert "no new candidate" in out.detail


def test_an_acceptance_survives_a_spent_budget(tmp_path):
    """The second branch the same defect lived on.

    A run that accepted a candidate and then kept iterating -- because the
    budget allowed it -- ended in PLANNING when the iterations ran out, and was
    reported by the stage it stopped in: REJECTED, "iterations spent without a
    checkpoint". The candidate was in the state the whole time.
    """
    l = HohRunLauncher(tmp_path, tmp_path)
    z = zustand(tmp_path, stage=Stage.PLANNING, condition=Condition.ACTIVE)
    z.iteration = 4
    z.last_accepted_candidate = kandidat("repair-2-1-i2", "d" * 40)
    out = l._verdict(z, None, None)
    assert out.verdict is RunVerdict.ACCEPTED
    assert "repair-2-1-i2" in out.detail
    assert "PLANNING" in out.detail, "where it ended up is still reported"


def test_a_run_still_planning_with_nothing_accepted_is_rejected(tmp_path):
    """The control: without an acceptance, spending the iterations is exactly
    what a rejection is."""
    l = HohRunLauncher(tmp_path, tmp_path)
    z = zustand(tmp_path, stage=Stage.PLANNING, condition=Condition.ACTIVE)
    z.iteration = 4
    out = l._verdict(z, None, None)
    assert out.verdict is RunVerdict.REJECTED
    assert "without a checkpoint" in out.detail
