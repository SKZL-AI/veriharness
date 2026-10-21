"""The autonomous double-down on BLOCKED (V3.3 P3-42, slice 1): `hoh.doubledown`.

A block is derived from what the product persists, classified into one tier (a table, fail
closed), kept in a create-only dossier and, for the tiers that take rounds, worked by two
analysts per round; it ends in a correction PROPOSAL or an ESCALATION that `hoh mission
needs-you` shows. Nothing here ever writes a run or project state.

Every test builds real runs and projects under `tmp_path` through the product's own stores and
drives the analysts with a scripted fake: no agent, no provider, no process. The tiers, the
round caps and every bound are written out as literals on purpose: they are the contract, and a
table imported from the module would agree with a mutated module.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import subprocess
import types
from contextlib import contextmanager, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hoh import cli
from hoh.contracts import Budgets, Condition, Role, RunState, Usage
from hoh.project import Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.store import RunStore

try:
    from hoh import doubledown
except ImportError:                      # not built yet: every test below fails on its own
    doubledown = None

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch):
    if doubledown is None:
        pytest.fail("hoh.doubledown does not exist")
    def forbidden(*args, **kwargs):
        pytest.fail("a test started a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


# --- builders ------------------------------------------------------------------------------ #

def _run(root, rid, condition=Condition.BLOCKED, **kw) -> RunStore:
    store = RunStore(root, rid)
    store.write_state(RunState(run_id=rid, repo_path="/x", project_name="p", spec_path="/x/s.md",
                               spec_digest="a" * 16, policy_digest="b" * 16,
                               profile_digest="c" * 16, condition=condition, **kw))
    return store


def _project(root, nodes, pid="p") -> ProjectStore:
    store = ProjectStore(root, pid)
    store.write_state(ProjectState(project_id=pid, repo_path="/x", nodes=list(nodes)))
    return store


def _node(nid, lifecycle=Lifecycle.BLOCKED, **kw) -> TaskNode:
    return TaskNode(id=nid, lifecycle=lifecycle, **kw)


def _loops_exhausted(rid="q", root=None):
    return dict(blocked_reason="3 loops without traceable progress (limit 3)",
                usage=Usage(iterations=3, loops_without_progress=3),
                budgets=Budgets(max_loops_without_progress=3))


def _hyp(path, outcome="open", evidence=("saw it",), tested=True, statement="s"):
    return {"path_id": path, "statement": statement, "tested": tested, "outcome": outcome,
            "evidence": list(evidence)}


def _report(perspective, hyps, proposal=None, unsolvable=None):
    return json.dumps({"perspective": perspective, "hypotheses": hyps, "proposal": proposal,
                       "unsolvable": unsolvable})


def _proposal(path, kind="repair_spec"):
    return {"path_id": path, "kind": kind, "summary": f"apply {path}", "steps": ["do it"]}


class Fake:
    """Scripted analysts: `script[(round, perspective)]` is the answer text, or an exception."""

    def __init__(self, script=None, default=None):
        self.script = script or {}
        self.default = default
        self.calls: list[tuple[int, str, str]] = []
        self.built: list[str] = []          # the halt ids a session was opened for
        self.closed = 0

    @contextmanager
    def factory(self, halt):
        self.built.append(halt.halt_id)
        try:
            yield self
        finally:
            self.closed += 1

    def analyse(self, perspective, prompt, *, round_no):
        self.calls.append((round_no, perspective, prompt))
        value = self.script.get((round_no, perspective), self.default)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(round_no, perspective)
        return value


def _open_hyps(round_no, perspective):
    return _report(perspective, [_hyp(f"open-{round_no}-{perspective}", "open", evidence=())])


def _tree_digest(root) -> dict:
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file() and p.name in ("state.json", "project.json"):
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _dossiers(owner: Path) -> list[Path]:
    dd = owner / "dd"
    return sorted(p for p in dd.iterdir() if p.is_dir() and len(p.name) == 16) if dd.is_dir() else []


def _only_dossier(owner: Path) -> Path:
    [d] = _dossiers(owner)
    return d


def _pass(root, fake=None, **kw):
    return doubledown.double_down_pass(root, analysts=fake.factory if fake else None,
                                       now=kw.pop("now", NOW), **kw)


# --- the bounds ---------------------------------------------------------------------------- #

def test_the_bounds_and_the_vocabulary_are_the_documented_numbers():
    assert doubledown.ROUND_CAPS == {"HARD_BLOCKER": 3, "RND_NEGATIVE": 8}
    assert doubledown.UNSOLVABLE_FROM == {"HARD_BLOCKER": 2, "RND_NEGATIVE": 5}
    assert doubledown.WAIT_GRACE == timedelta(hours=6)
    assert doubledown.PASS_ROUND_LIMIT == 4
    assert doubledown.DISPATCH_FAILURES == 3
    assert doubledown.PARK_MIN_SECONDS == 600
    assert doubledown.MAX_PARKS == 3
    assert doubledown.PARK_SURFACE_AFTER == timedelta(hours=24)
    assert (doubledown.OWNER_MAX_DOSSIERS, doubledown.OWNER_MAX_ROUNDS) == (64, 40)
    assert doubledown.OWNER_MAX_BYTES == 16 << 20
    assert (doubledown.MAX_FP_ENTRIES, doubledown.MAX_FP_HASHED_BYTES) == (50_000, 1 << 20)
    assert doubledown.ANALYST_SCRATCH_ESTIMATE == 64 << 20
    assert doubledown.CONFINABLE == {"claude"}
    assert doubledown.MAX_RECORD_BYTES == 1 << 20
    assert doubledown.PERSPECTIVES == ("root_cause", "alternative_design")
    assert {t.value for t in doubledown.Tier} == {"WAIT", "TRIVIAL", "HARD_BLOCKER", "RND_NEGATIVE",
                                       "HUMAN_ONLY"}
    assert doubledown.DECIDED == {"PROPOSAL", "ESCALATED", "HUMAN_ONLY"}


# --- halts and the classifier -------------------------------------------------------------- #

def _halt(scope="run", kind="", reason="r", retry_after=None, counters=None, facts=None):
    return doubledown.Halt(scope=scope, object_id="x", project_id=None, kind=kind, reason=reason,
                retry_after=retry_after, counters=counters or {}, facts=facts or {})


def _tier(halt, now=NOW):
    c = doubledown.classify(halt, now=now)
    return c.tier.value, c.rule


DUE = "2026-10-14T10:00:00Z"
CLASSIFIER_TABLE = [
    # the contract and the person
    (dict(kind="capability_violation"), ("HUMAN_ONLY", "capability_violation")),
    (dict(kind="human"), ("HUMAN_ONLY", "reserved_to_a_person")),
    (dict(kind="parked_by_contract"), ("HUMAN_ONLY", "reserved_to_a_person")),
    (dict(kind="collected_for_digest"), ("HUMAN_ONLY", "reserved_to_a_person")),
    (dict(kind="contract_stop"), ("HUMAN_ONLY", "reserved_to_a_person")),
    (dict(kind="contract_abort"), ("HUMAN_ONLY", "reserved_to_a_person")),
    (dict(kind="somebody_elses_kind"), ("HUMAN_ONLY", "unknown_kind")),
    (dict(kind="Human"), ("HUMAN_ONLY", "unknown_kind")),               # a spelling is not the kind
    (dict(kind="usage_limit "), ("HUMAN_ONLY", "unknown_kind")),
    (dict(kind="human\n"), ("HUMAN_ONLY", "unknown_kind")),
    # waits: an existing consumer owns them until the bound
    (dict(kind="usage_limit", retry_after=DUE), ("WAIT", "wait_within_bound")),
    (dict(kind="storage_pressure", retry_after=DUE), ("WAIT", "wait_within_bound")),
    (dict(kind="rate_limit", retry_after=DUE), ("WAIT", "wait_within_bound")),
    (dict(kind="usage_limit", retry_after="2026-10-14T06:00:00Z"), ("WAIT", "wait_within_bound")),
    (dict(kind="usage_limit", retry_after="2026-10-14T05:59:59Z"),
     ("WAIT", "wait_overdue")),
    (dict(kind="usage_limit", retry_after="2026-10-14T05:00:00+00:00"),
     ("WAIT", "wait_overdue")),
    (dict(kind="usage_limit", retry_after=None), ("WAIT", "wait_without_wakeup")),
    (dict(kind="usage_limit", retry_after="soon"), ("WAIT", "wait_without_wakeup")),
    (dict(kind="storage_pressure", retry_after=None), ("WAIT", "wait_without_wakeup")),
    (dict(kind="storage_pressure", retry_after="2026-10-14T05:59:59Z"), ("WAIT", "wait_overdue")),
    (dict(kind="role_timeout", retry_after="2026-10-14T05:59:59Z"), ("WAIT", "wait_overdue")),
    (dict(kind="usage_limit", retry_after="2026-10-14T05:00:00"),      # no zone: not a time
     ("WAIT", "wait_without_wakeup")),
    # a run with no kind
    (dict(counters={"loops_without_progress": 3},
          facts={"max_loops_without_progress": 3}), ("RND_NEGATIVE", "qa_loop_exhausted")),
    (dict(counters={"loops_without_progress": 2}, facts={"max_loops_without_progress": 3}),
     ("HARD_BLOCKER", "plain_block")),
    (dict(counters={"loops_without_progress": 0}, facts={
        "max_loops_without_progress": 3, "budget_exhausted": "iteration budget exhausted (3/3)"}),
     ("TRIVIAL", "budget_ceiling")),
    (dict(counters={"loops_without_progress": 3}, facts={
        "max_loops_without_progress": 3, "budget_exhausted": "iteration budget exhausted (3/3)"}),
     ("RND_NEGATIVE", "qa_loop_exhausted")),
    # nodes
    (dict(scope="node", counters={"lifecycle": "ABANDONED", "rejections": 4}),
     ("RND_NEGATIVE", "rejections_exhausted")),
    (dict(scope="node", counters={"lifecycle": "BLOCKED", "rejections": 0}),
     ("HARD_BLOCKER", "plain_block")),
    # the project
    (dict(scope="project", counters={"unknown_dependencies": {"a": ["b"]}}),
     ("TRIVIAL", "unknown_dependency")),
    (dict(scope="project", counters={}), ("HUMAN_ONLY", "unclassified")),
    (dict(scope="elsewhere"), ("HUMAN_ONLY", "unclassified")),
]


@pytest.mark.parametrize("spec,expected", CLASSIFIER_TABLE)
def test_the_classifier_maps_each_halt_to_one_tier(spec, expected):
    assert _tier(_halt(**spec)) == expected


def test_a_legacy_capability_hold_with_no_kind_is_still_a_capability_violation(tmp_path):
    state = RunState(run_id="old", repo_path="/x", project_name="p", spec_path="/x/s.md",
                     spec_digest="a" * 16, policy_digest="b" * 16, profile_digest="c" * 16,
                     condition=Condition.BLOCKED, blocked_reason="capability violation: wrote /etc")
    [halt] = doubledown.halts_of_run(state)
    assert halt.kind == "capability_violation"
    assert _tier(halt) == ("HUMAN_ONLY", "capability_violation")


def test_only_a_blocked_run_is_a_halt_and_a_node_with_a_blocked_run_is_that_runs_halt(tmp_path):
    for condition in (Condition.ACTIVE, Condition.PAUSED, Condition.FAILED, Condition.CANCELLED):
        state = _run(tmp_path, f"r-{condition.value}", condition=condition).read_state()
        assert doubledown.halts_of_run(state) == []
    blocked = _run(tmp_path, "rb", blocked_reason="why").read_state()
    nodes = [_node("n1", run_id="rb"), _node("n2", run_id="missing"), _node("n3")]
    state = ProjectState(project_id="p", repo_path="/x", nodes=nodes)
    halts = doubledown.halts_of_project(
        state, lambda rid: blocked if rid == "rb" else None)
    assert sorted(h.object_id for h in halts) == ["n2", "n3"]


def test_an_abandoned_node_is_a_halt_only_past_the_rejection_limit():
    from hoh.orchestrator import MAX_REJECTIONS

    assert MAX_REJECTIONS == 3
    at = _node("a", Lifecycle.ABANDONED, rejections=3)
    past = _node("b", Lifecycle.ABANDONED, rejections=4)
    state = ProjectState(project_id="p", repo_path="/x", nodes=[at, past])
    [halt] = doubledown.halts_of_project(state)
    assert (halt.object_id, halt.counters["rejections"]) == ("b", 4)


def test_the_halt_id_is_made_of_stable_fields_not_of_free_text():
    base = dict(kind="", counters={"stage": "PLANNING", "iteration": 1})
    a = _halt(reason="r", **base)
    assert a.halt_id == _halt(reason="r", **base).halt_id and len(a.halt_id) == 16
    # a rewritten reason, a new retry time, evidence that moved: the same block, the same dossier
    assert _halt(reason="another text entirely", **base).halt_id == a.halt_id
    assert _halt(reason="r", retry_after="2026-10-14T10:00:00Z", **base).halt_id == a.halt_id
    assert _halt(reason="r", facts={"outages": 9}, **base).halt_id == a.halt_id
    # the kind, the owner, the stage and the counters are the identity
    for other in (_halt(reason="r", kind="human", counters=base["counters"]),
                  _halt(reason="r", kind="", counters={"stage": "PLANNING", "iteration": 2}),
                  _halt(reason="r", kind="", counters={"stage": "VERIFYING", "iteration": 1}),
                  doubledown.Halt(scope="run", object_id="y", project_id=None, kind="",
                                  reason="r", retry_after=None, counters=base["counters"],
                                  facts={}),
                  doubledown.Halt(scope="node", object_id="x", project_id=None, kind="",
                                  reason="r", retry_after=None, counters=base["counters"],
                                  facts={})):
        assert other.halt_id != a.halt_id


def test_a_run_halt_names_its_stage_in_its_identity(tmp_path):
    state = _run(tmp_path, "r", blocked_reason="x").read_state()
    [halt] = doubledown.halts_of_run(state)
    assert halt.counters["stage"] == state.stage.value
    assert halt.facts["project_name"] == "p" and halt.facts["repo_path"] == "/x"


# --- tiers at work ------------------------------------------------------------------------- #

def test_a_wait_inside_its_bound_has_no_dossier_and_an_overdue_one_ends_in_one_decision(tmp_path):
    store = _run(tmp_path, "w", blocked_kind="usage_limit", blocked_reason="usage limit",
                 retry_after="2026-10-14T10:00:00+00:00")
    fake = Fake(default=_open_hyps)
    [row] = _pass(tmp_path, fake)
    assert (row["tier"], row["action"]) == ("WAIT", "waiting")
    assert _dossiers(store.dir) == [] and fake.calls == []
    [late] = _pass(tmp_path, fake, now=NOW + timedelta(hours=5))   # 17:00 > 10:00 + 6 h
    assert (late["tier"], late["rule"], late["action"], late["outcome"]) == (
        "WAIT", "wait_overdue", "escalated_wait_overdue", "ESCALATED")
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert d.rounds == [] and d.outcome["outcome"] == "ESCALATED"
    assert fake.calls == [] and fake.built == []                  # no provider use at all
    assert [r["action"] for r in _pass(tmp_path, fake, now=NOW + timedelta(hours=5))] == ["closed"]


@pytest.mark.parametrize("kind", ["storage_pressure", "usage_limit", "rate_limit",
                                  "provider_unavailable", "network_transient", "prompt_stalled",
                                  "role_timeout"])
@pytest.mark.parametrize("retry", ["2026-10-14T05:00:00+00:00", None])
def test_no_wait_ever_dispatches_an_analyst_overdue_or_without_a_wakeup(tmp_path, kind, retry):
    """Storage and quota (and every other timed kind) never take an analyst: they would add the
    very pressure the wait is for. Overdue, or with no readable retry time, each ends in ONE
    escalation decision, written with no session opened and nothing dispatched."""
    store = _run(tmp_path, "w", blocked_kind=kind, blocked_reason="waiting", retry_after=retry)
    fake = Fake(default=_open_hyps)
    for _ in range(3):
        _pass(tmp_path, fake)
    assert fake.calls == [] and fake.built == []
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert (d.block["tier"], d.outcome["outcome"], len(d.rounds)) == ("WAIT", "ESCALATED", 0)
    assert sorted(os.listdir(d.path)) == ["block.json", "outcome.json"]


def test_an_overdue_wait_is_one_decision_in_the_needs_you_document(tmp_path):
    """The decision is the dossier's ESCALATED outcome, listed under `double_down`: the ranked
    list of the frontier files a timed kind under `other_blocked` before it looks at a routing
    (`decisionfrontier.py`, not this slice's file), so it cannot carry it (limit L4)."""
    _project(tmp_path, [_node("s", blocked_kind="storage_pressure", note="disk",
                              retry_after="2026-10-14T05:00:00Z"),
                        _node("q", blocked_kind="usage_limit", note="quota",
                              retry_after="2026-10-14T05:00:00Z")])
    fake = Fake(default=_open_hyps)
    rows = _pass(tmp_path, fake)
    assert sorted(r["target"] for r in rows if r["action"] == "escalated_wait_overdue") == ["q", "s"]
    assert fake.calls == [] and fake.built == []
    doc = _needs_you(tmp_path)
    assert doc["decisions"] == [] and doc["totals"]["open"] == 0
    assert sorted((r["target"], r["outcome"], r["rounds"]) for r in doc["double_down"]) == [
        ("q", "ESCALATED", 0), ("s", "ESCALATED", 0)]


def test_a_trivial_block_gets_one_proposal_from_the_record_and_no_analyst(tmp_path):
    store = _run(tmp_path, "t", blocked_reason="iteration budget exhausted (3/3)",
                 usage=Usage(iterations=3), budgets=Budgets(max_iterations=3))
    fake = Fake()
    [row] = _pass(tmp_path, fake)
    assert (row["tier"], row["rule"], row["action"], row["outcome"]) == (
        "TRIVIAL", "budget_ceiling", "proposal", "PROPOSAL")
    assert fake.calls == [] and fake.built == []
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert d.rounds == [] and d.outcome["status"] == "UNVERIFIED"
    assert d.outcome["proposal"]["kind"] == "plan_note"
    assert "iteration budget exhausted (3/3)" in d.outcome["proposal"]["summary"]
    assert sorted(os.listdir(d.path)) == ["block.json", "outcome.json"]
    assert (_pass(tmp_path, fake)[0]["action"]) == "closed"          # idempotent


def test_unknown_dependencies_are_a_trivial_project_halt_with_a_derived_proposal(tmp_path):
    store = _project(tmp_path, [_node("a", Lifecycle.READY, dependencies=["ghost"])])
    [row] = _pass(tmp_path)
    assert (row["scope"], row["tier"], row["rule"]) == ("project", "TRIVIAL", "unknown_dependency")
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert d.outcome["proposal"]["path_id"] == "fix-dependency-ids"
    assert "a -> ghost" in d.outcome["proposal"]["summary"]


def test_a_reserved_block_is_a_dossier_and_nothing_is_unblocked(tmp_path):
    store = _run(tmp_path, "c", blocked_kind="capability_violation",
                 blocked_reason="capability violation: wrote outside the arena")
    before = _tree_digest(tmp_path)
    fake = Fake(default=_open_hyps)
    [row] = _pass(tmp_path, fake)
    assert (row["tier"], row["action"], row["outcome"]) == ("HUMAN_ONLY", "human_only",
                                                            "HUMAN_ONLY")
    assert fake.calls == [] and _tree_digest(tmp_path) == before
    assert store.read_state().condition is Condition.BLOCKED
    assert store.read_state().blocked_kind == "capability_violation"


def test_a_hard_blocker_is_two_analysts_per_round_and_ends_in_a_proposal(tmp_path):
    store = _run(tmp_path, "h", blocked_reason="the spec was changed under the run")
    script = {
        (1, "root_cause"): _report("root_cause", [_hyp("stale-digest", "refuted")]),
        (1, "alternative_design"): _report("alternative_design", [_hyp("pin-copy", "open",
                                                                       evidence=())]),
        (2, "root_cause"): _report("root_cause", [_hyp("edit-in-flight", "supported")],
                                   proposal=_proposal("edit-in-flight")),
        (2, "alternative_design"): _report("alternative_design", [_hyp("pin-copy", "open",
                                                                       evidence=())]),
    }
    fake = Fake(script)
    [r1] = _pass(tmp_path, fake)
    assert r1["action"] == "round_1_continue"
    assert [(n, p) for n, p, _ in fake.calls] == [(1, "root_cause"), (1, "alternative_design")]
    [r2] = _pass(tmp_path, fake)
    assert (r2["action"], r2["outcome"] if "outcome" in r2 else "proposal") == ("proposal",
                                                                                  "proposal")
    assert [(n, p) for n, p, _ in fake.calls] == [
        (1, "root_cause"), (1, "alternative_design"), (2, "root_cause"), (2, "alternative_design")]
    # context escalation: round 2 saw round 1's findings and the path it refuted
    second = fake.calls[2][2]
    assert "stale-digest" in second and "pin-copy" in second and "(this is round 1)" not in second
    assert '["stale-digest"]' in second
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert [r["verdict"] for r in d.rounds] == ["CONTINUE", "FRUITFUL"]
    assert d.rounds[0]["previous"] == d.block["digest"]
    assert d.rounds[1]["previous"] == d.rounds[0]["digest"]
    assert d.outcome["outcome"] == "PROPOSAL" and d.outcome["status"] == "UNVERIFIED"
    assert d.outcome["proposal"]["path_id"] == "edit-in-flight" and d.outcome["rounds"] == 2
    assert sorted(os.listdir(d.path)) == [
        "block.json", "outcome.json", "round-1.alternative_design.json", "round-1.guard.json",
        "round-1.json", "round-1.root_cause.json", "round-2.alternative_design.json",
        "round-2.guard.json", "round-2.json", "round-2.root_cause.json"]


def test_a_hard_blocker_is_escalated_at_the_third_round(tmp_path):
    store = _run(tmp_path, "h", blocked_reason="a block nobody can name")
    fake = Fake(default=_open_hyps)
    actions = [_pass(tmp_path, fake)[0]["action"] for _ in range(4)]
    assert actions == ["round_1_continue", "round_2_continue", "escalated_round_cap", "closed"]
    assert len(fake.calls) == 6
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert d.outcome["outcome"] == "ESCALATED" and d.outcome["rounds"] == 3
    assert "cap of 3 rounds" in d.outcome["reason"]


def test_an_rnd_negative_takes_five_rounds_before_unsolvable_counts_and_eight_at_most(tmp_path):
    claim = {"certain": True, "basis": "the criterion cannot hold on this base"}

    def certain(round_no, perspective):
        return _report(perspective, [_hyp(f"p-{round_no}-{perspective}", "refuted")],
                       unsolvable=claim)
    store = _run(tmp_path, "r", **_loops_exhausted())
    fake = Fake(default=certain)
    verdicts = []
    for _ in range(6):
        [row] = _pass(tmp_path, fake)
        verdicts.append(row["action"])
    assert verdicts == ["round_1_continue", "round_2_continue", "round_3_continue",
                        "round_4_continue", "escalated_unsolvable", "closed"]
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert d.block["tier"] == "RND_NEGATIVE" and d.outcome["rounds"] == 5
    assert d.rounds[3]["why"].startswith("certainty claimed in round 4, before the floor of "
                                         "round 5")
    # never conclusive: the cap, not a certainty, ends it at round 8
    other = tmp_path / "other"
    store2 = _run(other, "r", **_loops_exhausted())
    fake2 = Fake(default=_open_hyps)
    seen = [_pass(other, fake2)[0]["action"] for _ in range(9)]
    assert seen[:7] == [f"round_{n}_continue" for n in range(1, 8)]
    assert seen[7:] == ["escalated_round_cap", "closed"]
    assert doubledown.read_dossier(_only_dossier(store2.dir)).outcome["rounds"] == 8


def test_a_repeat_of_a_refuted_path_and_an_unsupported_proposal_are_not_fruitful(tmp_path):
    store = _run(tmp_path, "h", blocked_reason="stuck")
    script = {
        (1, "root_cause"): _report("root_cause", [_hyp("dead-end", "refuted")]),
        (1, "alternative_design"): _report("alternative_design", [_hyp("x", "open", evidence=())]),
        # round 2 proposes the path round 1 refuted
        (2, "root_cause"): _report("root_cause", [_hyp("dead-end", "supported")],
                                   proposal=_proposal("dead-end")),
        (2, "alternative_design"): _report("alternative_design", [_hyp("y", "open", evidence=())]),
        # round 3: a proposal on a hypothesis that is only 'open'
        (3, "root_cause"): _report("root_cause", [_hyp("z", "open")], proposal=_proposal("z")),
        (3, "alternative_design"): _report("alternative_design", [_hyp("w", "open", evidence=())]),
    }
    fake = Fake(script)
    for _ in range(3):
        _pass(tmp_path, fake)
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert [r["verdict"] for r in d.rounds] == ["CONTINUE", "INVALID", "CONTINUE"]
    assert "earlier round refuted" in d.rounds[1]["why"]
    assert d.outcome["outcome"] == "ESCALATED"


def test_a_proposal_with_no_evidence_is_not_fruitful_and_both_certain_blocks_a_proposal(tmp_path):
    reports = {"root_cause": {"perspective": "root_cause", "proposal": _proposal("a"),
                              "hypotheses": [_hyp("a", "supported", evidence=())],
                              "unsolvable": None},
               "alternative_design": {"perspective": "alternative_design", "proposal": None,
                                      "hypotheses": [_hyp("b")], "unsolvable": None}}
    assert doubledown.judge(1, "HARD_BLOCKER", reports, frozenset())[0] == "CONTINUE"
    reports["root_cause"]["hypotheses"] = [_hyp("a", "supported")]
    assert doubledown.judge(1, "HARD_BLOCKER", reports, frozenset())[0] == "FRUITFUL"
    reports["alternative_design"]["unsolvable"] = {"certain": True, "basis": "no way"}
    assert doubledown.judge(1, "HARD_BLOCKER", reports, frozenset())[0] == "CONTINUE"
    assert doubledown.judge(1, "HARD_BLOCKER", {"root_cause": None, "alternative_design": None},
                            frozenset())[0] == "INVALID"


@pytest.mark.parametrize("answer", [
    "no json at all",
    json.dumps([1, 2]),
    json.dumps({"perspective": "root_cause"}),
    json.dumps({"perspective": "alternative_design", "hypotheses": [_hyp("a")],
                "proposal": None, "unsolvable": None}),                   # the other perspective
    json.dumps({"perspective": "root_cause", "hypotheses": [], "proposal": None,
                "unsolvable": None}),
    json.dumps({"perspective": "root_cause", "hypotheses": [_hyp("Bad Slug")],
                "proposal": None, "unsolvable": None}),
    json.dumps({"perspective": "root_cause", "hypotheses": [_hyp("trailing\n")],
                "proposal": None, "unsolvable": None}),
    json.dumps({"perspective": "root_cause", "hypotheses": [_hyp("a")],
                "proposal": _proposal("not-mine"), "unsolvable": None}),
    json.dumps({"perspective": "root_cause", "hypotheses": [_hyp("a")],
                "proposal": _proposal("a", kind="apply_it_now"), "unsolvable": None}),
    json.dumps({"perspective": "root_cause", "hypotheses": [_hyp("a")], "proposal": None,
                "unsolvable": {"certain": "yes", "basis": "x"}}),
    json.dumps({"perspective": "root_cause", "hypotheses": [_hyp("a"), _hyp("a")],
                "proposal": None, "unsolvable": None}),
])
def test_an_answer_that_does_not_validate_is_kept_and_costs_a_round(tmp_path, answer):
    store = _run(tmp_path, "h", blocked_reason="stuck")
    fake = Fake({(1, "root_cause"): answer}, default=_open_hyps)
    [row] = _pass(tmp_path, fake)
    assert row["action"] == "round_1_invalid"
    d = doubledown.read_dossier(_only_dossier(store.dir))
    rec = json.loads((d.path / "round-1.root_cause.json").read_text())
    assert rec["valid"] is False and rec["error"] and rec["raw"] == answer[:65536]
    assert d.rounds[0]["verdict"] == "INVALID"


def test_three_failed_dispatches_park_the_dossier_and_end_nothing(tmp_path):
    store = _run(tmp_path, "h", **_loops_exhausted())          # RND_NEGATIVE, no round yet
    fake = Fake({(1, "root_cause"): RuntimeError("provider down")}, default=_open_hyps)
    acts = [_pass(tmp_path, fake)[0]["action"] for _ in range(3)]
    assert acts == ["dispatch_failed", "dispatch_failed", "parked"]
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert d.rounds == [] and d.outcome is None               # a provider failure is no verdict
    assert [p["kind"] for p in d.parks] == ["provider"] and d.parked is not None
    assert sorted(e for e in os.listdir(d.path) if e.startswith("fail-")) == [
        "fail-1-root_cause-1.json", "fail-1-root_cause-2.json", "fail-1-root_cause-3.json"]
    calls = len(fake.calls)
    [row] = _pass(tmp_path, fake)                              # too early, and nobody asked
    assert row["action"] == "parked" and len(fake.calls) == calls


def test_a_pass_starts_at_most_four_rounds_one_per_dossier(tmp_path):
    for i in range(6):
        _run(tmp_path, f"h{i}", blocked_reason=f"stuck {i}")
    fake = Fake(default=_open_hyps)
    rows = _pass(tmp_path, fake)
    started = [r for r in rows if r["action"] == "round_1_continue"]
    waiting = [r for r in rows if r["action"] == "waiting_for_analysts"]
    assert (len(started), len(waiting)) == (4, 2)
    assert len(fake.calls) == 8
    rows2 = _pass(tmp_path, fake, round_limit=1)
    assert sum(1 for r in rows2 if r["action"].startswith("round_")) == 1


def test_no_analyst_harness_opens_dossiers_and_dispatches_nothing(tmp_path):
    store = _run(tmp_path, "h", blocked_reason="stuck")
    [row] = _pass(tmp_path, None)
    assert row["action"] == "waiting_for_analysts"
    assert sorted(os.listdir(_only_dossier(store.dir))) == ["block.json"]


def test_the_analysts_are_built_only_for_a_real_round(tmp_path):
    _run(tmp_path, "t", blocked_reason="iteration budget exhausted (1/1)",
         usage=Usage(iterations=1), budgets=Budgets(max_iterations=1))
    _run(tmp_path, "c", blocked_kind="human", blocked_reason="a question")
    fake = Fake(default=_open_hyps)
    _pass(tmp_path, fake)
    assert fake.built == []


# --- the firewall -------------------------------------------------------------------------- #

def test_a_whole_pass_leaves_every_state_byte_identical(tmp_path):
    _run(tmp_path, "loops", **_loops_exhausted())
    _run(tmp_path, "cap", blocked_kind="capability_violation", blocked_reason="violation")
    _run(tmp_path, "wait", blocked_kind="usage_limit", blocked_reason="limit",
         retry_after="2026-10-14T11:00:00+00:00")
    _run(tmp_path, "budget", blocked_reason="iteration budget exhausted (1/1)",
         usage=Usage(iterations=1), budgets=Budgets(max_iterations=1))
    _run(tmp_path, "paused", condition=Condition.PAUSED)
    _project(tmp_path, [_node("n1"), _node("n2", Lifecycle.ABANDONED, rejections=5),
                        _node("n3", Lifecycle.READY, dependencies=["ghost"])])
    before = _tree_digest(tmp_path)
    assert len(before) == 6
    fake = Fake(default=_open_hyps)
    for _ in range(3):
        rows = _pass(tmp_path, fake)
        assert not any(r["action"] == "refused" for r in rows)
    assert _tree_digest(tmp_path) == before


def test_the_module_has_no_path_that_writes_a_state_or_lifts_a_block():
    tree = ast.parse(Path(doubledown.__file__).read_text(encoding="utf-8"))
    forbidden = {"write_state", "write_checks", "write_evidence", "unblock", "resume", "arm_new",
                 "disarm", "disarm_all", "acquire", "release", "begin_iteration",
                 "reject_candidate", "transition", "block_under_contract", "record_external_action"}
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    called |= {n.func.id for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not (forbidden & (called | imported)), sorted(forbidden & (called | imported))
    # the only module-level imports of the store layer are the readers
    assert {"RunStore", "ProjectStore", "list_runs", "list_projects"} & imported


# --- refusals: fail closed ----------------------------------------------------------------- #

def _opened(tmp_path):
    store = _run(tmp_path, "h", blocked_reason="stuck")
    fake = Fake(default=_open_hyps)
    _pass(tmp_path, fake)
    return store, _only_dossier(store.dir), fake


def test_a_dossier_that_exists_with_another_digest_is_refused_and_nothing_is_dispatched(tmp_path):
    store, d, fake = _opened(tmp_path)
    block = json.loads((d / "block.json").read_text())
    block["halt_digest"] = "0" * 64
    body = {k: v for k, v in block.items() if k != "digest"}
    block["digest"] = doubledown._seal(body)["digest"]       # a consistent, foreign block
    (d / "block.json").chmod(0o600)
    (d / "block.json").write_text(json.dumps(block))
    before = len(fake.calls)
    [row] = _pass(tmp_path, fake)
    assert row["action"] == "refused" and "another digest" in row["why"]
    assert len(fake.calls) == before


def _aside(path: Path) -> None:
    """Replace, never delete: the original is kept under a versioned name outside the patterns."""
    path.rename(path.with_name(path.name + ".v1"))


def _tamper(path, how):
    if how == "digest":
        rec = json.loads(path.read_text())
        rec["reason"] = "edited"
        path.write_text(json.dumps(rec))
    elif how == "corrupt":
        path.write_text("{not json")
    elif how == "array":
        path.write_text("[]")
    elif how == "latin1":
        path.write_bytes(b'{"reason": "\xe9"}')
    elif how == "oversize":
        path.write_bytes(b" " * ((1 << 20) + 1))
    elif how == "empty":
        path.write_bytes(b"")
    elif how == "nul":
        path.write_bytes(b'{"a": "\x00"}\x00')
    elif how == "fifo":
        _aside(path)
        os.mkfifo(path)
    elif how == "directory":
        _aside(path)
        path.mkdir()
    elif how == "dangling":
        _aside(path)
        path.symlink_to(path.parent / "nowhere")
    elif how == "hardlink":
        os.link(path, path.parent / "second-name")
    elif how == "mode000":
        path.chmod(0)


@pytest.mark.parametrize("target", ["block.json", "round-1.json", "round-1.root_cause.json",
                                    "outcome.json"])
@pytest.mark.parametrize("how", ["digest", "corrupt", "array", "latin1", "oversize", "empty",
                                 "nul", "fifo", "directory", "dangling", "hardlink", "mode000"])
def test_an_odd_dossier_file_is_a_refusal_never_a_dispatch_or_an_overwrite(tmp_path, target, how):
    if how == "mode000" and IS_ROOT:
        pytest.skip("root reads a mode-000 file")
    store = _run(tmp_path, "h", blocked_reason="stuck")
    fake = Fake({(1, "root_cause"): _report("root_cause", [_hyp("a", "supported")],
                                            proposal=_proposal("a")),
                 (1, "alternative_design"): _open_hyps(1, "alternative_design")})
    _pass(tmp_path, fake)                                      # closes the dossier in round 1
    d = _only_dossier(store.dir)
    assert (d / "outcome.json").exists()
    victim = d / target
    _tamper(victim, how)
    calls = len(fake.calls)
    snapshot = sorted(os.listdir(d))
    [row] = _pass(tmp_path, fake)
    assert row["action"] == "refused", row
    assert len(fake.calls) == calls
    assert sorted(os.listdir(d)) == snapshot


def test_a_round_whose_answer_is_missing_or_belongs_to_another_block_is_refused(tmp_path):
    store, d, _ = _opened(tmp_path)
    report = d / "round-1.alternative_design.json"
    keep = report.read_bytes()
    foreign = json.loads(keep)
    foreign["block_digest"] = "f" * 64
    foreign = doubledown._seal(foreign)
    report.write_text(json.dumps(foreign))
    [row] = _pass(tmp_path, Fake(default=_open_hyps))
    assert row["action"] == "refused" and "lacks the answer of alternative_design" in row["why"]
    report.write_bytes(keep)
    _aside(d / "round-1.root_cause.json")
    [row] = _pass(tmp_path, Fake(default=_open_hyps))
    assert row["action"] == "refused" and "lacks the answer of root_cause" in row["why"]


def test_a_round_gap_or_a_dossier_under_another_name_is_refused(tmp_path):
    store, d, _ = _opened(tmp_path)
    (d / "round-1.json").rename(d / "round-2.json")
    [row] = _pass(tmp_path, Fake(default=_open_hyps))
    assert row["action"] == "refused" and "not 1..n" in row["why"]
    (d / "round-2.json").rename(d / "round-1.json")
    moved = d.parent / ("f" * 16)
    d.rename(moved)
    rows = _pass(tmp_path, Fake(default=_open_hyps))
    refused = [r for r in rows if r["action"] == "refused"]
    assert [r["target"] for r in refused] == ["f" * 16] and "of its own name" in refused[0]["why"]
    # the moved dossier is left exactly as it is; the block has no dossier under its own name
    # now, so it is opened afresh (round 1), never merged into the stranger
    assert sorted(p.name for p in moved.parent.iterdir()) == sorted(["f" * 16, d.name])
    assert json.loads((moved / "round-1.json").read_text())["round"] == 1


def test_a_dd_that_is_a_link_or_a_file_is_refused(tmp_path):
    store = _run(tmp_path, "h", blocked_reason="stuck")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (store.dir / "dd").symlink_to(elsewhere)
    [row] = _pass(tmp_path, Fake(default=_open_hyps))
    assert row["action"] == "refused" and os.listdir(elsewhere) == []
    (store.dir / "dd").rename(store.dir / "dd.v1")
    (store.dir / "dd").write_text("not a directory")
    [row] = _pass(tmp_path, Fake(default=_open_hyps))
    assert row["action"] == "refused"


def test_an_unreadable_state_is_a_row_not_a_skip(tmp_path):
    store = _run(tmp_path, "h", blocked_reason="stuck")
    (store.dir / "state.json").write_text("{broken")
    [row] = _pass(tmp_path, Fake(default=_open_hyps))
    assert (row["scope"], row["target"], row["action"]) == ("run", "h", "refused")
    ok = _run(tmp_path, "ok", blocked_reason="stuck 2")
    rows = _pass(tmp_path, Fake(default=_open_hyps))
    assert {r["target"]: r["action"] for r in rows} == {"h": "refused", "ok": "round_1_continue"}
    assert len(_dossiers(ok.dir)) == 1


def test_a_second_pass_that_finds_the_dossier_locked_does_nothing(tmp_path):
    import fcntl

    store, d, fake = _opened(tmp_path)
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        calls = len(fake.calls)
        [row] = _pass(tmp_path, fake)
        assert row["action"] == "refused" and "another pass holds" in row["why"]
        assert len(fake.calls) == calls
    finally:
        os.close(fd)


def test_a_block_that_cleared_closes_its_dossier_as_obsolete(tmp_path):
    store, d, fake = _opened(tmp_path)
    state = store.read_state()
    state.condition = Condition.ACTIVE
    state.blocked_reason = None
    store.write_state(state)
    rows = _pass(tmp_path, fake)
    assert [(r["scope"], r["action"], r["outcome"]) for r in rows] == [
        ("dossier", "obsolete", "OBSOLETE")]
    assert doubledown.read_dossier(d).outcome["outcome"] == "OBSOLETE"
    assert _pass(tmp_path, fake) == []


def test_a_project_held_by_a_live_master_is_left_alone(tmp_path):
    from hoh.succession import MasterAuthority

    store = _project(tmp_path, [_node("n1")])
    authority = MasterAuthority(store.dir, "master-a", ttl_seconds=3600)
    authority.acquire(program_generation=1, note="test")
    fake = Fake(default=_open_hyps)
    now = datetime.now(UTC)                       # a lease is judged by the clock it ran on
    [row] = _pass(tmp_path, fake, now=now)
    assert row["action"] == "held_by_live_master" and fake.calls == []
    assert _dossiers(store.dir) == []
    authority.release(reason="done")
    [row] = _pass(tmp_path, fake, now=now)
    assert row["action"] == "round_1_continue"


STATE_ODDITIES = ["corrupt", "array", "latin1", "nul", "empty", "fifo", "directory", "dangling",
                  "oversize", "mode000"]


def _break(path: Path, how: str) -> None:
    if how == "oversize":
        path.write_bytes(b" " * ((8 << 20) + 1))
    else:
        _tamper(path, how)


@pytest.mark.parametrize("how", STATE_ODDITIES)
def test_an_odd_run_state_file_is_a_refusal_and_the_pass_goes_on(tmp_path, how):
    if how == "mode000" and IS_ROOT:
        pytest.skip("root reads a mode-000 file")
    bad = _run(tmp_path, "bad", blocked_reason="stuck")
    good = _run(tmp_path, "good", blocked_reason="stuck too")
    _break(bad.dir / "state.json", how)
    rows = _pass(tmp_path, Fake(default=_open_hyps))
    got = {r["target"]: r["action"] for r in rows}
    assert got.get("bad") in ("refused", None) and got["good"] == "round_1_continue"
    if got.get("bad") is None:                          # a dangling link / directory is no run
        assert how in ("dangling", "directory")
    assert not (bad.dir / "dd").exists() and len(_dossiers(good.dir)) == 1


@pytest.mark.parametrize("how", STATE_ODDITIES)
def test_an_odd_project_state_file_is_a_refusal_never_a_block_of_the_pass(tmp_path, how):
    if how == "mode000" and IS_ROOT:
        pytest.skip("root reads a mode-000 file")
    bad = _project(tmp_path, [_node("n1")], pid="bad")
    good = _project(tmp_path, [_node("n1")], pid="good")
    _break(bad.dir / "project.json", how)
    import signal

    def alarm(*_):
        raise AssertionError("the pass blocked on a project file")
    old = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(20)
    try:
        rows = _pass(tmp_path, Fake(default=_open_hyps))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    got = {(r["scope"], r["target"]): r["action"] for r in rows}
    assert got.get(("project", "bad")) in ("refused", None)
    assert got[("node", "n1")] == "round_1_continue" or any(
        r["target"] == "n1" and r["project"] == "good" for r in rows)
    assert not (bad.dir / "dd").exists() and len(_dossiers(good.dir)) == 1


def test_a_run_directory_that_is_a_link_is_not_written_through(tmp_path):
    real = tmp_path / "elsewhere"
    store = _run(real, "r", blocked_reason="stuck")
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "r").symlink_to(store.dir)
    rows = _pass(tmp_path / "runs", Fake(default=_open_hyps))
    assert [r["action"] for r in rows] == ["refused"] and "link" in rows[0]["why"]
    assert not (store.dir / "dd").exists()


def test_a_run_name_that_is_not_text_is_a_printable_row(tmp_path):
    name = b"bad-\xff-name"
    os.mkdir(os.path.join(os.fsencode(tmp_path), name))
    with open(os.path.join(os.fsencode(tmp_path), name, b"state.json"), "wb") as fh:
        fh.write(b"{}")
    rows = _pass(tmp_path, Fake(default=_open_hyps))
    assert len(rows) == 1 and rows[0]["action"] == "refused"
    json.dumps(rows, ensure_ascii=False).encode("utf-8")      # printable without an error


# --- the decision frontier ----------------------------------------------------------------- #

def _needs_you(root, pid="p"):
    from hoh import missioncontrol

    return missioncontrol.project_view(str(root), pid, "needs-you")


def test_a_proposal_and_an_escalation_are_decisions_in_needs_you(tmp_path):
    _project(tmp_path, [
        _node("n1", note="the merge was obstructed"),
        _node("n2", note="another", dependencies=[]),
        _node("n3", Lifecycle.READY, dependencies=["n1"])])
    fruitful = {(1, "root_cause"): _report("root_cause", [_hyp("fix", "supported")],
                                           proposal=_proposal("fix")),
                (1, "alternative_design"): _open_hyps(1, "alternative_design")}
    before = _needs_you(tmp_path)
    assert before["totals"]["open"] == 0 and "double_down" not in before
    fake = Fake(fruitful)
    for _ in range(3):
        _pass(tmp_path, fake)
    doc = _needs_you(tmp_path)
    by_node = {d["node_id"]: d for d in doc["decisions"]}
    assert set(by_node) == {"n1", "n2"}
    assert {d["handling"] for d in doc["decisions"]} == {"ASK_NOW"}
    assert {d["kind"] for d in doc["decisions"]} == {"double_down"}
    assert by_node["n1"]["holds"] == ["n3"] and by_node["n1"]["frees"] == ["n3"]
    assert all(d["decision"].startswith("correction proposal (UNVERIFIED)") for d in
               doc["decisions"])
    assert {r["outcome"] for r in doc["double_down"]} == {"PROPOSAL"}
    assert doc["totals"]["open"] == 2 and doc["unreadable"] == {}
    # an escalation reads as one too
    other = tmp_path / "esc"
    _project(other, [_node("m1", note="never solved")])
    fake2 = Fake(default=_open_hyps)
    for _ in range(3):
        _pass(other, fake2)
    [esc] = _needs_you(other)["decisions"]
    assert "double-down escalated after 3 round(s)" in esc["decision"]


def test_the_text_view_lists_the_double_down_and_the_json_view_the_same_rows(tmp_path):
    _project(tmp_path, [_node("n1", note="blocked"), _node("gone", Lifecycle.ABANDONED, rejections=4)])
    fake = Fake({(1, "root_cause"): _report("root_cause", [_hyp("fix", "supported")],
                                            proposal=_proposal("fix")),
                 (1, "alternative_design"): _open_hyps(1, "alternative_design")})
    _pass(tmp_path, fake)
    _pass(tmp_path, fake)
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["--root", str(tmp_path), "mission", "needs-you", "--project", "p"])
    text = out.getvalue()
    assert code == 0
    assert "DOUBLE-DOWN  n1  HARD_BLOCKER  PROPOSAL  correction proposal (UNVERIFIED)" in text
    assert "DOUBLE-DOWN  gone  RND_NEGATIVE  " in text
    doc = _needs_you(tmp_path)
    assert sorted(r["target"] for r in doc["double_down"]) == ["gone", "n1"]


def test_an_unreadable_dossier_is_an_unreadable_decision_not_a_hidden_one(tmp_path):
    store = _project(tmp_path, [_node("n1", note="blocked")])
    _pass(tmp_path, Fake(default=_open_hyps))
    d = _only_dossier(store.dir)
    (d / "block.json").write_text("{broken")
    doc = _needs_you(tmp_path)
    assert doc["unreadable"] and "n1" in doc["unreadable"]
    assert doc["decisions"][0]["handling"] == "UNREADABLE"


def test_a_contract_routing_still_reads_last(tmp_path):
    from hoh.autonomy import AutonomyMode, Horizon, compile_profile
    from hoh.governance import bind, block_under_contract

    run_dir = tmp_path / "r-a"
    run_dir.mkdir()
    governor = bind(run_dir, contract=compile_profile(
        AutonomyMode.UNATTENDED_LONG_HORIZON, contract_id="un", scope=("src/hoh/",),
        horizon=Horizon(max_wallclock_seconds=3600, review_after_seconds=900),
        budgets=Budgets(max_dispatches=10, max_wallclock_seconds=3600)))
    state = RunState(run_id="r-a", repo_path="/x", project_name="p", spec_path="/x/s.md",
                     spec_digest="a" * 16, policy_digest="b" * 16, profile_digest="c" * 16)
    block_under_contract(state, "the developer needs folder trust", governor)
    assert state.blocked_kind == "parked_by_contract"
    RunStore(tmp_path, "r-a").write_state(state)
    _project(tmp_path, [_node("A", run_id="r-a", note="run blocked"), _node("B", Lifecycle.READY,
                                                                          dependencies=["A"])])
    rows = _pass(tmp_path, Fake(default=_open_hyps))
    assert [(r["scope"], r["tier"], r["outcome"]) for r in rows] == [("run", "HUMAN_ONLY",
                                                                       "HUMAN_ONLY")]
    doc = _needs_you(tmp_path)
    [only] = doc["decisions"]
    assert (only["node_id"], only["handling"], only["kind"]) == ("A", "PARK_BRANCH", "decision")
    assert [r["outcome"] for r in doc["double_down"]] == ["HUMAN_ONLY"]


# --- the production path: hoh resume-quota --double-down ----------------------------------- #

def _main(root, *argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["--root", str(root), "resume-quota", *argv])
    return code, out.getvalue()


class StorageWorld:
    """The storage state root, the registered project and its temporary root, as the command
    sees them: every call is recorded, and every workspace is made OUTSIDE the runs root."""

    def __init__(self, tmp_path, monkeypatch, *, registered=True, refuse_storage=False,
                 refuse_create=False):
        self.base = tmp_path.parent / f"{tmp_path.name}-state"
        self.base.mkdir()
        self.log: list = []
        self.registered, self.refuse_storage, self.refuse_create = (
            registered, refuse_storage, refuse_create)
        monkeypatch.setattr(cli, "storage_admission_for", self._admission)
        monkeypatch.setattr(cli, "_open_ephemeral", self._open)

    def _admission(self, option, **kw):
        self.log.append(("admission", option))
        if self.refuse_storage:
            raise cli.StorageRefused("storage admission unavailable: a test says so")
        return object(), self.base

    def _open(self, root, project_name, repo_path, run_id, admission):
        self.log.append(("open", str(root), project_name, repo_path, run_id))
        if not self.registered:
            return None, "no project is registered"
        world = self

        class Roots:
            def create(self_, **kw):
                world.log.append(("create", kw))
                if world.refuse_create:
                    raise RuntimeError("TempAdmissionRefused: a test says so")
                path = world.base / f"{kw['name']}-{len(world.log)}"
                path.mkdir()
                return types.SimpleNamespace(path=path)

            def release(self_, ws):
                world.log.append(("release", ws.path))

        class Session:
            session_id = run_id
            roots = Roots()

            def close(self_):
                world.log.append(("close",))
                return ""
        return Session(), "name"

    def kinds(self):
        return [entry[0] for entry in self.log]


def _fake_build(monkeypatch, fake, seen):
    def build(workspace, **kw):
        seen.update(kw, workspace=Path(workspace))
        return fake
    monkeypatch.setattr(doubledown, "build_analysts", build)


def test_resume_quota_double_down_classifies_opens_and_advances_through_the_cli(
        tmp_path, monkeypatch):
    store = _run(tmp_path, "h", blocked_reason="stuck")
    budget = _run(tmp_path, "t", blocked_reason="iteration budget exhausted (1/1)",
                  usage=Usage(iterations=1), budgets=Budgets(max_iterations=1))
    world = StorageWorld(tmp_path, monkeypatch)
    fake = Fake(default=_open_hyps)
    seen: dict = {}
    _fake_build(monkeypatch, fake, seen)
    before = _tree_digest(tmp_path)
    code, text = _main(tmp_path, "--double-down", "--analyst-harness", "claude",
                       "--analyst-model=m-1", "--analyst-effort", "high",
                       "--analyst-timeout", "123", "--storage-root", str(world.base))
    assert code == 0
    doc = json.loads(text)
    rows = {r["target"]: r for r in doc["double_down"]}
    assert rows["h"]["action"] == "round_1_continue" and rows["t"]["action"] == "proposal"
    assert doc["double_down_defects"] == 0
    workspace = seen.pop("workspace")
    assert seen == {"harness": "claude", "model": "m-1", "effort": "high", "timeout_s": 123}
    # the analysts live in a registered workspace OUTSIDE the run, admitted, and given back
    assert workspace.parent == world.base and tmp_path not in workspace.parents
    assert world.kinds() == ["admission", "open", "create", "release", "close"]
    assert world.log[0] == ("admission", str(world.base))
    assert world.log[1][2:] == ("p", "/x", f"dd-{rows['h']['halt_id']}")
    created = world.log[2][1]
    assert created == {"creator": "hoh.doubledown", "purpose": "reviewer",
                       "name": f"dd-{rows['h']['halt_id']}", "estimate_bytes": 64 << 20}
    assert world.log[3] == ("release", workspace)
    assert _only_dossier(budget.dir).parent.name == "dd" and _only_dossier(store.dir)
    assert _tree_digest(tmp_path) == before                      # no state written
    # a second tick continues the same dossier (the storage root is asked once per pass)
    code, text = _main(tmp_path, "--double-down", "--analyst-harness", "claude")
    assert {r["target"]: r["action"] for r in json.loads(text)["double_down"]} == {
        "h": "round_2_continue", "t": "closed"}
    seen.pop("workspace")
    assert seen == {"harness": "claude", "model": None, "effort": None, "timeout_s": 900}
    assert world.log[5] == ("admission", None)


@pytest.mark.parametrize("harness", ["codex", "kimi", "pi"])
def test_a_harness_that_cannot_be_confined_is_refused_and_never_started(
        tmp_path, monkeypatch, harness):
    """Only `claude` takes a role approval policy (`dispatchers._agent_args` refuses any other
    harness once a policy is set): the pass says so, opens no storage session and no workspace,
    and dispatches nothing -- the dossier waits."""
    store = _run(tmp_path, "h", blocked_reason="stuck")
    world = StorageWorld(tmp_path, monkeypatch)
    fake = Fake(default=_open_hyps)
    real_build = doubledown.build_analysts
    _fake_build(monkeypatch, fake, {})
    code, text = _main(tmp_path, "--double-down", "--analyst-harness", harness)
    [row] = json.loads(text)["double_down"]
    assert code == 0 and row["action"] == "waiting_for_analysts"
    assert f"harness '{harness}' cannot be confined" in row["why"]
    assert world.log == [] and fake.calls == []
    assert os.listdir(_only_dossier(store.dir)) == ["block.json"]
    with pytest.raises(doubledown.AnalystRefused, match="cannot be confined"):
        real_build(tmp_path / "ws", harness=harness)


@pytest.mark.parametrize("how,why", [
    ({"refuse_storage": True}, "storage: storage admission unavailable"),
    ({"registered": False}, "no registered temporary root for this block"),
    ({"refuse_create": True}, "temporary workspace: RuntimeError: TempAdmissionRefused"),
])
def test_an_analyst_with_nowhere_confined_to_work_is_not_started(tmp_path, monkeypatch, how, why):
    store = _run(tmp_path, "h", blocked_reason="stuck")
    world = StorageWorld(tmp_path, monkeypatch, **how)
    fake = Fake(default=_open_hyps)
    _fake_build(monkeypatch, fake, {})
    code, text = _main(tmp_path, "--double-down", "--analyst-harness", "claude")
    [row] = json.loads(text)["double_down"]
    assert code == 0 and row["action"] == "waiting_for_analysts" and why in row["why"]
    assert fake.calls == []
    assert os.listdir(_only_dossier(store.dir)) == ["block.json"]
    if "create" in world.kinds():                        # an opened session is always closed
        assert world.kinds()[-1] == "close"


def test_the_workspace_is_given_back_and_the_session_closed_when_the_round_fails(
        tmp_path, monkeypatch):
    _run(tmp_path, "h", blocked_reason="stuck")
    world = StorageWorld(tmp_path, monkeypatch)

    def build(workspace, **kw):
        raise RuntimeError("the dispatcher could not be built")
    monkeypatch.setattr(doubledown, "build_analysts", build)
    code, text = _main(tmp_path, "--double-down", "--analyst-harness", "claude")
    [row] = json.loads(text)["double_down"]
    assert code == 0 and row["action"] == "refused" and "could not be built" in row["why"]
    assert world.kinds() == ["admission", "open", "create", "release", "close"]


def test_resume_quota_with_the_double_down_opted_out_adds_nothing_and_the_analyst_flags_need_it_on(
        tmp_path):
    """Slice 3 turned the double-down ON by default (`hoh resume-quota` passes it itself); the
    opt-out is `--no-double-down`, and it is the one that adds nothing."""
    store = _run(tmp_path, "h", blocked_reason="stuck")
    code, text = _main(tmp_path, "--no-double-down")
    doc = json.loads(text)
    assert code == 0 and [r["action"] for r in doc["double_down"]] == ["opted_out"]
    assert "double_down_defects" not in doc
    assert not (store.dir / "dd").exists()
    for flag in (("--analyst-harness", "claude"), ("--analyst-model", "m"),
                 ("--analyst-effort", "high")):
        code, _ = _main(tmp_path, "--no-double-down", *flag)
        assert code == 2
    assert not (store.dir / "dd").exists()


@pytest.mark.parametrize("value,code", [("0", 2), ("-5", 2), ("3601", 2), ("1", 0), ("3600", 0)])
def test_the_analyst_timeout_is_one_to_3600_seconds(tmp_path, monkeypatch, value, code):
    _run(tmp_path, "h", blocked_reason="stuck")
    StorageWorld(tmp_path, monkeypatch)
    seen: dict = {}
    _fake_build(monkeypatch, Fake(default=_open_hyps), seen)
    got, _ = _main(tmp_path, "--double-down", "--analyst-harness", "claude", "--analyst-timeout",
                   value)
    assert got == code
    assert seen.get("timeout_s") == (int(value) if code == 0 else None)


def test_resume_quota_double_down_without_a_harness_only_opens_dossiers(tmp_path, monkeypatch):
    store = _run(tmp_path, "h", blocked_reason="stuck")
    world = StorageWorld(tmp_path, monkeypatch)
    code, text = _main(tmp_path, "--double-down")
    [row] = json.loads(text)["double_down"]
    assert code == 0 and row["action"] == "waiting_for_analysts"
    assert os.listdir(_only_dossier(store.dir)) == ["block.json"]
    assert world.log == []


def test_resume_quota_double_down_with_a_run_id_looks_at_that_run_only(tmp_path):
    _run(tmp_path, "a", blocked_reason="one")
    _run(tmp_path, "b", blocked_reason="two")
    _project(tmp_path, [_node("n1")])
    code, text = _main(tmp_path, "a", "--double-down")
    rows = json.loads(text)["double_down"]
    assert [r["target"] for r in rows] == ["a"]
    assert (tmp_path / "b" / "dd").exists() is False
    assert (tmp_path / "projects" / "p" / "dd").exists() is False


# --- the confinement of an analyst session (a configuration, pinned; the provider's behaviour
# --- under it is not measured here: no provider is called) ------------------------------------ #

def test_the_analyst_policy_is_dontask_with_the_whole_mandatory_deny_list():
    from hoh import approval_policy

    policy = doubledown.analyst_policy()
    assert policy.mode == "dontAsk"
    assert set(policy.deny) == set(approval_policy.MANDATORY_DENY)
    assert policy.allow == {"planner": ()}
    lowered = policy.house_rules.lower()
    assert all(w in lowered for w in ("never delete", "read-only", "nvidia-smi", "proposal"))


def test_an_analyst_session_is_built_under_the_policy_in_its_workspace(tmp_path):
    """The REAL dispatcher builder, no dispatch: the session's arguments carry the policy, its
    settings file allows exactly the answers directory of its own workspace, and its answers,
    working directory and role scratch are all inside that workspace."""
    ws = tmp_path / "ws"
    ws.mkdir()
    analyst = doubledown.build_analysts(ws, harness="claude", model="m", effort="e", timeout_s=77)
    d = analyst.dispatcher
    assert d.answers_dir == ws / "answers" and d.cwd == ws and d.timeout == 77
    args = d._agent_args(Role.PLANNER)
    assert args[:2] == ["--model", "m"] and "--effort" in args
    assert ["--permission-mode", "dontAsk"] == args[args.index("--permission-mode"):][:2]
    settings = Path(args[args.index("--settings") + 1])
    assert settings == ws / "approval" / "planner.settings.json"
    perms = json.loads(settings.read_text())["permissions"]
    answers = str((ws / "answers").resolve())
    assert perms["defaultMode"] == "dontAsk"
    assert perms["allow"] == [f"Edit(/{answers}/**)", f"Read(/{answers}/**)"]
    from hoh import approval_policy
    assert set(perms["deny"]) == set(approval_policy.MANDATORY_DENY) and "WebFetch" in perms["deny"]
    assert Path(args[args.index("--append-system-prompt-file") + 1]).parent == ws / "approval"
    scratch = d.role_scratch[Role.PLANNER]
    assert ws in scratch.parents
    assert d.profiles == {Role.PLANNER: "claude"}


def test_an_analyst_answers_in_the_planners_slot_with_its_round_and_perspective(tmp_path):
    seen = {}

    class Recorder:
        def dispatch(self, role, prompt, *, state):
            seen["dispatch"] = (role, prompt, state.iteration, state.attempt)
            return "answer"
    analyst = doubledown.RoleAnalyst(Recorder())
    assert analyst.analyse("alternative_design", "P", round_no=4) == "answer"
    assert seen["dispatch"] == (Role.PLANNER, "P", 4, 1)


def test_build_analysts_forwards_its_arguments_to_the_dispatcher_builder(tmp_path, monkeypatch):
    from hoh import dispatchers

    seen = {}

    class Dispatcher:
        def bind_role_scratch(self, workspace):
            seen["bound"] = workspace

    def fake_build(**kw):
        seen["build"] = kw
        return Dispatcher()
    monkeypatch.setattr(dispatchers, "build_dispatcher", fake_build)
    ws = tmp_path / "ws"
    doubledown.build_analysts(ws, harness="claude", model="m", effort="e", timeout_s=77)
    kw = seen["build"]
    assert kw["answers_dir"] == ws / "answers" and kw["cwd"] == ws
    assert kw["profiles"] == {Role.PLANNER: "claude"} and kw["prefer_herdr"] is False
    assert kw["timeout_ms"] == 77000 and kw["defer_role_scratch"] is True
    assert kw["models"] == {Role.PLANNER: "m"} and kw["efforts"] == {Role.PLANNER: "e"}
    assert kw["approval_policy"].mode == "dontAsk" and seen["bound"] == ws
    doubledown.build_analysts(ws, harness="claude")
    assert seen["build"]["models"] is None and seen["build"]["efforts"] is None


# --- the guard: what the analysts must not change ------------------------------------------- #

class Mutating(Fake):
    """Analysts that, as a side effect of answering, touch the filesystem."""

    def __init__(self, action, **kw):
        super().__init__(**kw)
        self.action = action

    def analyse(self, perspective, prompt, *, round_no):
        self.action(perspective)
        return super().analyse(perspective, prompt, round_no=round_no)


def _fruitful(path="fix"):
    return {(1, "root_cause"): _report("root_cause", [_hyp(path, "supported")],
                                       proposal=_proposal(path)),
            (1, "alternative_design"): _open_hyps(1, "alternative_design")}


@pytest.mark.parametrize("change", ["new-file", "edit-state", "grow", "new-dir"])
def test_a_change_to_the_run_while_the_analysts_run_discards_the_round_and_escalates(
        tmp_path, change):
    store = _run(tmp_path, "h", blocked_reason="stuck")

    def act(perspective):
        if perspective != "alternative_design":
            return
        if change == "new-file":
            (store.dir / "planted.txt").write_text("x")
        elif change == "edit-state":
            (store.dir / "state.json").write_bytes((store.dir / "state.json").read_bytes() + b" ")
        elif change == "grow":
            with open(store.dir / "state.json", "ab") as fh:
                fh.write(b"\n")
        else:
            (store.dir / "sub").mkdir()
    fake = Mutating(act, script=_fruitful())
    [row] = _pass(tmp_path, fake)
    assert row["action"] == "escalated_run_changed"
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert d.outcome["outcome"] == "ESCALATED" and d.outcome["proposal"] is None
    assert "changed while the analysts ran" in d.outcome["reason"]
    [r1] = d.rounds
    assert r1["verdict"] == "DISCARDED" and r1["reports"] == {}
    assert r1["guard"]["before"] != r1["guard"]["after"]
    assert set(r1["answers_sha256"]) == {"root_cause", "alternative_design"}
    assert "fix" not in json.dumps(d.outcome)              # the proposal did not survive


def test_a_node_s_run_directory_is_watched_with_the_project(tmp_path):
    run = _run(tmp_path, "r", condition=Condition.ACTIVE)
    store = _project(tmp_path, [_node("n1", run_id="r", note="merge failed")])

    def act(perspective):
        (run.dir / "planted.txt").write_text("x")
    [row] = _pass(tmp_path, Mutating(act, script=_fruitful()))
    assert (row["scope"], row["action"]) == ("node", "escalated_run_changed")
    assert doubledown.read_dossier(_only_dossier(store.dir)).rounds[0]["verdict"] == "DISCARDED"


def test_a_write_into_the_live_dossiers_own_directory_is_not_a_change(tmp_path):
    store = _run(tmp_path, "h", blocked_reason="stuck")

    def act(perspective):
        (_only_dossier(store.dir) / "scratch.txt").write_text("mine")
    [row] = _pass(tmp_path, Mutating(act, script=_fruitful()))
    assert row["action"] == "proposal"
    assert doubledown.read_dossier(_only_dossier(store.dir)).rounds[0]["verdict"] == "FRUITFUL"


@pytest.mark.parametrize("forge", ["other-dossier-outcome", "owner-cap", "dd-file"])
def test_a_record_forged_elsewhere_under_dd_is_a_change(tmp_path, forge):
    """Only the live dossier's own files are the double-down's while a round runs: a forged
    `outcome.json` of another dossier (or any other write under `dd/`) is a change of the run."""
    store = _run(tmp_path, "h", blocked_reason="stuck")
    other = store.dir / "dd" / ("a" * 16)
    other.mkdir(parents=True)

    def act(perspective):
        if forge == "other-dossier-outcome":
            (other / "outcome.json").write_text("{}")
        elif forge == "owner-cap":
            (store.dir / "dd" / "owner-cap.json").write_text("{}")
        else:
            (store.dir / "dd" / "note.txt").write_text("x")
    [row] = [r for r in _pass(tmp_path, Mutating(act, script=_fruitful())) if r["scope"] == "run"]
    assert row["action"] == "escalated_run_changed"


def test_a_discarded_dossier_whose_close_failed_is_never_dispatched_again(tmp_path, monkeypatch):
    """The window of review R2-B1: the round is written DISCARDED and the escalation cannot be
    (the disk). The next passes dispatch NOTHING on the run that has already changed; they only
    retry the close, until it succeeds."""
    store = _run(tmp_path, "h", blocked_reason="stuck")

    def act(perspective):
        (store.dir / "planted.txt").write_text("x")
    fake = Mutating(act, script=_fruitful(), default=_open_hyps)
    real_close = doubledown._close

    def no_space(*a, **kw):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(doubledown, "_close", no_space)
    [row] = _pass(tmp_path, fake)
    assert row["action"] == "refused" and "No space left" in row["why"]
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert [r["verdict"] for r in d.rounds] == ["DISCARDED"] and d.outcome is None
    calls = len(fake.calls)
    assert calls == 2
    for _ in range(3):                                   # the disk stays full
        [row] = _pass(tmp_path, fake)
        assert row["action"] == "refused"
    assert len(fake.calls) == calls
    assert len(doubledown.read_dossier(_only_dossier(store.dir)).rounds) == 1
    monkeypatch.setattr(doubledown, "_close", real_close)
    [row] = _pass(tmp_path, fake)
    assert (row["action"], row["outcome"]) == ("escalated_run_changed", "ESCALATED")
    assert len(fake.calls) == calls
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert d.outcome["outcome"] == "ESCALATED" and d.outcome["proposal"] is None
    assert "changed while the analysts ran" in d.outcome["reason"] and len(d.rounds) == 1
    assert _pass(tmp_path, fake)[0]["action"] == "closed"


def test_the_fingerprint_sees_content_size_and_new_entries_and_leaves_its_exclusions_out(tmp_path):
    d = tmp_path / "w"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("one")
    (d / "dd").mkdir()
    (d / "dd" / "own.json").write_text("{}")
    fp = lambda: doubledown.fingerprint([d], exclude=[d / "dd"])
    base = fp()
    assert fp() == base and base["entries"] == 2          # a.txt, sub (dd is left out)
    (d / "dd" / "own.json").write_text("changed")
    assert fp() == base                                    # an excluded path is not watched
    (d / "a.txt").write_text("two")                        # same size, other content
    assert fp()["digest"] != base["digest"]
    (d / "a.txt").write_text("one")
    os.utime(d / "a.txt", ns=(1, 1))                       # nothing but the mtime
    assert fp()["digest"] != base["digest"]
    base2 = fp()
    (d / "sub" / "deeper.txt").write_text("x")
    assert fp()["digest"] != base2["digest"]
    # a link is looked at, never followed; a FIFO is never opened
    os.mkfifo(d / "pipe")
    (d / "link").symlink_to(tmp_path)
    import signal

    def alarm(*_):
        raise AssertionError("the fingerprint blocked")
    old = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(10)
    try:
        assert fp()["entries"] == 5          # a.txt, sub, sub/deeper.txt, pipe, link
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def test_a_tree_too_big_to_guard_is_not_dispatched_and_the_bound_is_exact(tmp_path, monkeypatch):
    store = _run(tmp_path, "h", blocked_reason="stuck")
    # what the pass counts: the run directory's entries, `dd` itself, and nothing inside the
    # live dossier (the only thing in `dd` at that moment)
    n = doubledown.fingerprint([store.dir], exclude=[store.dir / "dd"])["entries"] + 1
    fake = Fake(default=_open_hyps)
    monkeypatch.setattr(doubledown, "MAX_FP_ENTRIES", n - 1)
    [row] = _pass(tmp_path, fake)
    assert row["action"] == "refused" and "cannot be guarded" in row["why"]
    assert fake.calls == []
    monkeypatch.setattr(doubledown, "MAX_FP_ENTRIES", n)           # exactly at the bound: guarded
    [row] = _pass(tmp_path, fake)
    assert row["action"] == "round_1_continue"


# --- the unsolvability judgement ----------------------------------------------------------- #

def _jr(perspective, *, proposal=None, claim=None, hyp=None):
    return {"perspective": perspective, "hypotheses": [hyp or _hyp(f"h-{perspective}", "open")],
            "proposal": proposal, "unsolvable": claim}


SURE = {"certain": True, "basis": "the criterion cannot hold on this base"}


@pytest.mark.parametrize("tier,floor", [("RND_NEGATIVE", 5), ("HARD_BLOCKER", 2)])
def test_unsolvable_needs_both_analysts_a_basis_and_no_proposal_from_the_floor(tier, floor):
    both = {"root_cause": _jr("root_cause", claim=SURE),
            "alternative_design": _jr("alternative_design", claim=SURE)}
    judged = lambda n, reports: doubledown.judge(n, tier, reports, frozenset())[0]
    assert judged(floor, both) == "UNSOLVABLE"
    assert judged(floor - 1, both) == "CONTINUE"           # the floor is exact
    # one analyst's certainty is not enough, at any round
    one = {"root_cause": _jr("root_cause", claim=SURE),
           "alternative_design": _jr("alternative_design")}
    assert judged(floor, one) == "CONTINUE" and judged(floor + 3, one) == "CONTINUE"
    # a certainty without a basis is no claim
    blank = {"certain": True, "basis": "   "}
    both_blank = {"root_cause": _jr("root_cause", claim=blank),
                  "alternative_design": _jr("alternative_design", claim=blank)}
    assert judged(floor, both_blank) == "CONTINUE"
    one_blank = {"root_cause": _jr("root_cause", claim=SURE),
                 "alternative_design": _jr("alternative_design", claim=blank)}
    assert judged(floor, one_blank) == "CONTINUE"
    # "certain: false" is no claim either
    unsure = {"certain": False, "basis": "probably"}
    assert judged(floor, {"root_cause": _jr("root_cause", claim=unsure),
                          "alternative_design": _jr("alternative_design", claim=unsure)}) == "CONTINUE"


@pytest.mark.parametrize("tier,floor", [("RND_NEGATIVE", 5), ("HARD_BLOCKER", 2)])
def test_a_proposal_and_a_claim_of_unsolvability_in_one_round_contradict_each_other(tier, floor):
    """Intended verdict: CONTINUE, whoever holds which. Neither a result nor a giving up is
    taken from a round that says both; the next round has to settle it."""
    prop = _jr("root_cause", proposal=_proposal("fix"), hyp=_hyp("fix", "supported"))
    for reports in (
            {"root_cause": prop, "alternative_design": _jr("alternative_design", claim=SURE)},
            {"root_cause": _jr("root_cause", claim=SURE, proposal=_proposal("fix"),
                               hyp=_hyp("fix", "supported")),
             "alternative_design": _jr("alternative_design", claim=SURE)}):
        verdict, _, why = doubledown.judge(floor, tier, reports, frozenset())
        assert verdict == "CONTINUE" and "contradict" in why
    # the same proposal with no claim in the round is FRUITFUL: it is the claim that blocks it
    assert doubledown.judge(floor, tier, {"root_cause": prop,
                                          "alternative_design": _jr("alternative_design")},
                            frozenset())[0] == "FRUITFUL"


# --- provider failures park; waits and bounds are never closed by a hiccup ------------------- #

class _Health:
    """A stand-in for `health.read_health`: what the sentinel says about the providers."""

    def __init__(self, state=None, *, complete=True, raises=False):
        self.state, self.complete, self.raises = state, complete, raises

    def __call__(self, root, *, now=None):
        if self.raises:
            raise RuntimeError("unreadable")
        providers = () if self.state is None else (
            types.SimpleNamespace(provider="claude", state=self.state),
            types.SimpleNamespace(provider="other", state=doubledown.HealthState.LIMITED))
        return types.SimpleNamespace(complete=self.complete, providers=providers)


def _fail_three_times(tmp_path, fake, now=NOW):
    return [_pass(tmp_path, fake, now=now)[0]["action"] for _ in range(3)]


def test_a_parked_dossier_resumes_only_when_the_sentinel_says_the_provider_is_back(
        tmp_path, monkeypatch):
    store = _run(tmp_path, "h", blocked_reason="stuck")
    fake = Fake({(1, "root_cause"): RuntimeError("provider down")}, default=_open_hyps)
    assert _fail_three_times(tmp_path, fake)[-1] == "parked"
    calls = len(fake.calls)
    fake.script.pop((1, "root_cause"))                    # the provider answers again
    at = lambda seconds: NOW + timedelta(seconds=seconds)
    monkeypatch.setattr(doubledown, "read_health", _Health())
    # the floor of the wait is exact: 599 s is too early, 600 s is not
    assert _pass(tmp_path, fake, now=at(599))[0]["action"] == "parked"
    # a pending limit or outage of THIS provider keeps it parked (another provider's does not)
    for state in (doubledown.HealthState.LIMITED, doubledown.HealthState.DEGRADED):
        monkeypatch.setattr(doubledown, "read_health", _Health(state))
        assert _pass(tmp_path, fake, now=at(7200))[0]["action"] == "parked"
    # an unreadable or incomplete reading is not "back"
    for sick in (_Health(complete=False), _Health(raises=True)):
        monkeypatch.setattr(doubledown, "read_health", sick)
        assert _pass(tmp_path, fake, now=at(7200))[0]["action"] == "parked"
    assert len(fake.calls) == calls
    monkeypatch.setattr(doubledown, "read_health",
                        _Health(doubledown.HealthState.RESET_DUE))     # elapsed: not pending
    [row] = _pass(tmp_path, fake, now=at(600))
    assert (row["action"], row.get("resumed")) == ("round_1_continue", True)
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert d.parked is None and len(d.parks) == 1 and len(d.resumes) == 1
    assert d.rounds[0]["verdict"] == "CONTINUE"
    # the failures before the resume are not counted again: one more failure is a failure, not a park
    fake.script[(2, "root_cause")] = RuntimeError("down again")
    assert _pass(tmp_path, fake, now=at(700))[0]["action"] == "dispatch_failed"


def _drive_failures(tmp_path, fake, monkeypatch, *, passes=40):
    monkeypatch.setattr(doubledown, "read_health", _Health())
    actions = []
    for k in range(passes):
        rows = _pass(tmp_path, fake, now=NOW + timedelta(seconds=700 * k))
        actions.extend(r["action"] for r in rows)
    return actions


def test_a_hard_blocker_escalates_when_the_fourth_provider_park_would_be_needed(
        tmp_path, monkeypatch):
    store = _run(tmp_path, "h", blocked_reason="stuck")
    fake = Fake({(1, "root_cause"): RuntimeError("down")}, default=_open_hyps)
    actions = _drive_failures(tmp_path, fake, monkeypatch)
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert [p["kind"] for p in d.parks] == ["provider"] * 3
    assert d.outcome["outcome"] == "ESCALATED" and "parks" in d.outcome["reason"]
    assert actions.count("parked") >= 3 and "escalated_bound" in actions
    assert len(fake.calls) == 12                          # 3 failures x (3 parks + the last one)


def test_an_rnd_negative_before_its_floor_is_parked_by_a_bound_and_never_ended(
        tmp_path, monkeypatch):
    run = _run(tmp_path, "r", **_loops_exhausted())
    _project(tmp_path, [_node("n1", run_id="r", note="x")])
    fake = Fake({(1, "root_cause"): RuntimeError("down")}, default=_open_hyps)
    actions = _drive_failures(tmp_path, fake, monkeypatch)
    d = doubledown.read_dossier(_only_dossier(run.dir))
    assert d.block["tier"] == "RND_NEGATIVE" and d.outcome is None and d.rounds == []
    assert [p["kind"] for p in d.parks] == ["provider", "provider", "provider", "bound"]
    assert actions[-1] == "parked_bound" and len(fake.calls) == 12
    calls = len(fake.calls)
    monkeypatch.setattr(doubledown, "read_health", _Health())
    assert _pass(tmp_path, fake, now=NOW + timedelta(days=30))[0]["action"] == "parked_bound"
    assert len(fake.calls) == calls                       # nothing resumes a bound by itself
    doc = _needs_you(tmp_path)
    assert [(r["outcome"], r["target"]) for r in doc["double_down"]] == [("PARKED_BOUND", "r")]
    [only] = doc["decisions"]
    assert only["node_id"] == "n1" and "parked" in only["decision"]


# --- the owner's lifetime allowance ---------------------------------------------------------- #

def test_a_rewritten_reason_is_the_same_block_and_keeps_its_dossier(tmp_path):
    store = _run(tmp_path, "h", blocked_reason="stuck on the spec")
    fake = Fake(default=_open_hyps)
    [first] = _pass(tmp_path, fake)
    state = store.read_state()
    state.blocked_reason = "stuck, said differently"
    store.write_state(state)
    [second] = _pass(tmp_path, fake)
    assert second["halt_id"] == first["halt_id"] and second["reason_changed"] is True
    assert second["action"] == "round_2_continue" and len(_dossiers(store.dir)) == 1
    assert doubledown.read_dossier(_only_dossier(store.dir)).block["reason"] == "stuck on the spec"


def test_the_owner_has_a_lifetime_allowance_of_dossiers(tmp_path, monkeypatch):
    monkeypatch.setattr(doubledown, "OWNER_MAX_DOSSIERS", 2)
    store = _project(tmp_path, [_node("a", note="1"), _node("b", note="2"), _node("c", note="3")])
    fake = Fake(default=_open_hyps)
    rows = _pass(tmp_path, fake)
    assert sorted(r["action"] for r in rows) == ["owner_cap", "round_1_continue",
                                                 "round_1_continue"]
    assert len(_dossiers(store.dir)) == 2
    cap = json.loads((store.dir / "dd" / "owner-cap.json").read_text())
    assert "allowance" in cap["reason"]
    again = _pass(tmp_path, fake)
    assert sorted(r["action"] for r in again).count("owner_cap") == 1
    assert len(_dossiers(store.dir)) == 2


def test_the_owner_has_a_lifetime_allowance_of_bytes(tmp_path, monkeypatch):
    store = _project(tmp_path, [_node("a", note="1"), _node("b", note="2")])
    fake = Fake(default=_open_hyps)
    monkeypatch.setattr(doubledown, "OWNER_MAX_BYTES", 1)
    rows = _pass(tmp_path, fake)
    # the first dossier opens (the allowance was untouched), its own block record spends it, and
    # a spent allowance ends a hard blocker at the start of its round; the second block gets none
    assert sorted(r["action"] for r in rows) == ["escalated_bound", "owner_cap"]
    assert len(_dossiers(store.dir)) == 1 and fake.calls == []


def test_the_owner_has_a_lifetime_allowance_of_rounds(tmp_path, monkeypatch):
    monkeypatch.setattr(doubledown, "OWNER_MAX_ROUNDS", 2)
    hard = _run(tmp_path, "hard", blocked_reason="stuck")
    rnd = _run(tmp_path, "rnd", **_loops_exhausted())
    fake = Fake(default=_open_hyps)
    seen = {r["target"]: [] for r in _pass(tmp_path, fake)}      # round 1 of both
    for _ in range(2):
        for r in _pass(tmp_path, fake):
            seen.setdefault(r["target"], []).append(r["action"])
    assert seen["hard"] == ["round_2_continue", "escalated_bound"]
    assert seen["rnd"] == ["round_2_continue", "parked_bound"]    # an R&D negative is parked
    assert doubledown.read_dossier(_only_dossier(hard.dir)).outcome["outcome"] == "ESCALATED"
    d = doubledown.read_dossier(_only_dossier(rnd.dir))
    assert d.outcome is None and d.parked["kind"] == "bound"


# --- unclassifiable blocks are counted ----------------------------------------------------- #

def test_an_unclassifiable_block_is_a_counted_classifier_defect_and_never_released(tmp_path):
    store = _run(tmp_path, "u", blocked_kind="a_kind_from_the_future", blocked_reason="?")
    _run(tmp_path, "ok", blocked_kind="capability_violation", blocked_reason="violation")
    fake = Fake(default=_open_hyps)
    rows = {r["target"]: r for r in _pass(tmp_path, fake)}
    assert rows["u"]["defect"] == "UNCLASSIFIED" and rows["u"]["outcome"] == "HUMAN_ONLY"
    assert "defect" not in rows["ok"] and fake.calls == []
    assert store.read_state().condition is Condition.BLOCKED
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert d.block["defect"] is True
    code, text = _main(tmp_path, "--double-down")
    assert json.loads(text)["double_down_defects"] == 1
    for spec in (dict(kind="x"), dict(scope="project", counters={})):
        assert doubledown.classify(_halt(**spec), now=NOW).defect is True
    for spec in (dict(kind="human"), dict(kind="usage_limit", retry_after=DUE), dict()):
        assert doubledown.classify(_halt(**spec), now=NOW).defect is False


# --- the chain, the cap at equality, the tier recorded at opening ---------------------------- #

def test_a_round_that_does_not_chain_to_the_round_before_is_refused(tmp_path):
    store = _run(tmp_path, "h", blocked_reason="stuck")
    fake = Fake(default=_open_hyps)
    _pass(tmp_path, fake)
    _pass(tmp_path, fake)
    d = _only_dossier(store.dir)
    second = json.loads((d / "round-2.json").read_text())
    second["previous"] = "f" * 64                       # a consistent record, the wrong parent
    (d / "round-2.json").write_text(json.dumps(doubledown._seal(second)))
    [row] = _pass(tmp_path, fake)
    assert row["action"] == "refused" and "does not chain" in row["why"]
    assert len(fake.calls) == 4


def test_the_round_cap_is_reached_at_equality_even_when_the_outcome_was_not_written(
        tmp_path, monkeypatch):
    """A crash between the last round file and its outcome: the next pass closes it, and never
    dispatches the round after the cap."""
    store = _run(tmp_path, "h", blocked_reason="stuck")
    fake = Fake(default=_open_hyps)
    _pass(tmp_path, fake)
    _pass(tmp_path, fake)
    real_close = doubledown._close

    def crash(*a, **kw):
        raise OSError("the disk went away")
    monkeypatch.setattr(doubledown, "_close", crash)
    [row] = _pass(tmp_path, fake)                       # round 3 is written, its outcome is not
    assert row["action"] == "refused"
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert len(d.rounds) == 3 and d.outcome is None
    monkeypatch.setattr(doubledown, "_close", real_close)
    calls = len(fake.calls)
    [row] = _pass(tmp_path, fake)
    assert row["action"] == "escalated" and len(fake.calls) == calls
    assert doubledown.read_dossier(_only_dossier(store.dir)).outcome["outcome"] == "ESCALATED"


def test_the_tier_recorded_at_opening_governs_the_dossier(tmp_path, monkeypatch):
    store = _run(tmp_path, "h", blocked_reason="stuck")
    fake = Fake(default=_open_hyps)
    _pass(tmp_path, fake)
    real = doubledown.classify

    def other(halt, *, now):
        real(halt, now=now)
        return doubledown.Classification(doubledown.Tier.HUMAN_ONLY, "changed", "x")
    monkeypatch.setattr(doubledown, "classify", other)
    [row] = _pass(tmp_path, fake)
    assert row["action"] == "round_2_continue"           # still the HARD_BLOCKER it was opened as
    assert doubledown.read_dossier(_only_dossier(store.dir)).block["tier"] == "HARD_BLOCKER"


def test_the_analyst_workspace_is_a_registered_admitted_workspace_outside_the_run(
        tmp_path, monkeypatch):
    """The command's own session code with the product's real `EphemeralRoots` (a registry that
    only answers what the roots ask of it): while an analyst answers, its workspace is an
    ACTIVE, recorded, admitted workspace of the project's registered temporary root, and nowhere
    near the run; afterwards it is RELEASED and gone."""
    import hoh.storage_ephemeral as se

    state_root = tmp_path.parent / f"{tmp_path.name}-registry"
    state_root.mkdir()
    reserved, released = [], []

    class Admission:
        def reserve(self, project_id, op_kind, *, estimate_bytes=None):
            reserved.append((project_id, op_kind, estimate_bytes))
            return types.SimpleNamespace(admitted=True, id="res-1")

        def release(self, reservation, actual_bytes):
            released.append(actual_bytes)

    class Registry:
        def _read_lease(self, project_id, session_id):
            return types.SimpleNamespace(pid=os.getpid(), pid_start=1, boot_id="b")

        def lease_is_live(self, lease):
            return True

        def scratch_dir(self, project_id, session_id, kind):
            path = state_root / "projects" / project_id / kind / session_id
            path.mkdir(parents=True, exist_ok=True)
            return path

    admission = Admission()
    roots = se.EphemeralRoots(Registry(), "pd", "dd-session", admission=admission)

    class Session:
        session_id = "dd-session"
        project_id = "pd"

        def __init__(self):
            self.roots = roots

        def close(self):
            return ""
    monkeypatch.setattr(cli, "storage_admission_for", lambda option, **kw: (admission, state_root))
    monkeypatch.setattr(cli, "_open_ephemeral", lambda *a: (Session(), "name"))
    store = _run(tmp_path, "h", blocked_reason="stuck")
    entries_before = set(os.listdir(store.dir))
    watched: dict = {}

    def during(perspective):
        ws = watched["workspace"]
        record = se.read_record(ws)
        watched.setdefault("seen", []).append(
            (ws.exists(), record["state"], record["purpose"], record["creator"],
             ws.parent == state_root / "projects" / "pd" / "tmp" / "dd-session",
             tmp_path not in ws.parents and store.dir not in ws.parents))
    analysts = Mutating(during, default=_open_hyps)
    monkeypatch.setattr(doubledown, "build_analysts",
                        lambda ws, **kw: watched.update(workspace=Path(ws)) or analysts)
    args = types.SimpleNamespace(analyst_harness="claude", analyst_model=None,
                                 analyst_effort=None, analyst_timeout=900, storage_root=None)
    rows = doubledown.double_down_pass(tmp_path, analysts=cli._analyst_sessions(args), now=NOW)
    assert [r["action"] for r in rows] == ["round_1_continue"]
    assert watched["seen"] == [(True, "ACTIVE", "reviewer", "hoh.doubledown", True, True)] * 2
    assert reserved == [("pd", "tmp_workspace", 64 << 20)] and len(released) == 1
    ws = watched["workspace"]
    assert not ws.exists() and se.read_record(ws)["state"] == "RELEASED"
    assert set(os.listdir(store.dir)) - entries_before == {"dd"}       # nothing but the dossier


def test_an_rnd_negative_that_reached_its_floor_is_ended_by_a_bound_not_parked(
        tmp_path, monkeypatch):
    monkeypatch.setattr(doubledown, "OWNER_MAX_ROUNDS", 5)
    store = _run(tmp_path, "rnd", **_loops_exhausted())
    fake = Fake(default=_open_hyps)
    actions = [_pass(tmp_path, fake)[0]["action"] for _ in range(6)]
    assert actions == [f"round_{n}_continue" for n in range(1, 6)] + ["escalated_bound"]
    d = doubledown.read_dossier(_only_dossier(store.dir))
    assert d.outcome["outcome"] == "ESCALATED" and d.parks == [] and len(d.rounds) == 5


def test_a_provider_park_older_than_a_day_is_one_decision_in_needs_you(tmp_path, monkeypatch):
    run = _run(tmp_path, "r", blocked_reason="stuck")
    store = _project(tmp_path, [_node("n1", run_id="r", note="x")])
    fake = Fake({(1, "root_cause"): RuntimeError("down")}, default=_open_hyps)
    assert _fail_three_times(tmp_path, fake)[-1] == "parked"            # parked_at == NOW
    monkeypatch.setattr(doubledown, "read_health", _Health(doubledown.HealthState.LIMITED))
    state = store.read_state()

    def listing(seconds):
        stops, unreadable, rows = doubledown.decisions_for_project(
            tmp_path, state, now=NOW + timedelta(seconds=seconds))
        return stops, rows
    stops, rows = listing(24 * 3600 - 1)
    assert rows == [] and stops == {}
    stops, rows = listing(24 * 3600)                                     # the bound is exact
    assert [(r["outcome"], r["target"]) for r in rows] == [("PARKED_LONG", "r")]
    assert "24 h" in rows[0]["summary"] and list(stops) == ["n1"]
    # the pass row says so too (the same exact bound), and it is still a park
    just = [r for r in _pass(tmp_path, fake, now=NOW + timedelta(seconds=24 * 3600 - 1))
            if r["scope"] == "run"]
    assert [(r["action"], r.get("surfaced")) for r in just] == [("parked", None)]
    [row] = [r for r in _pass(tmp_path, fake, now=NOW + timedelta(hours=24)) if r["scope"] == "run"]
    assert (row["action"], row.get("surfaced")) == ("parked", True)
    d = doubledown.read_dossier(_only_dossier(run.dir))
    assert d.outcome is None and d.parked is not None
