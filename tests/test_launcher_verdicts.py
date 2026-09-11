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
from hoh.orchestrator import RunVerdict
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
    assert starter.merge(TaskNode(id="a"), ergebnis) is False
