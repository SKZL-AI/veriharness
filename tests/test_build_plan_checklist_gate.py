"""P2's three checklist-gate exit conditions are measured, not unclaimed.

`build_plan.phase_gates` used to count the three `P2.checklist-gate.*` items of the
plan as "nothing here measures yet", because no requirement claims them -- so the P2
gate stayed shut however many rows were PROVEN, and nothing said which instrument
would ever open it. They are measured by tools that already exist
(`programme_checklist`, `programme_supervisor`, the board's `programme_supervisor`
row), and `build_plan.checklist_gate_measures` now asks those:

    all phase-blocking checklist items VERIFIED or validly WAIVED   -> "items"
    current independent Checklist Supervisor review complete        -> "review"
    ChecklistFsck PASS                                              -> "fsck"

Three kinds of case, none of which reads a fact about today's tree that the closure
will change:

* wiring -- `phase_gates` with the instruments' answers handed in: an answer opens a
  condition only as the bool `True`, a requirement's claim never stands in for it,
  and an item that is not in the map stays unmeasured and unmet (the intent of
  `test_an_unbound_gate_item_still_counts_as_unmeasured` in `test_build_plan.py`);
* the instruments, over a small constructed tree -- the review is written by
  `programme_supervisor.record_review`, as the real one is; every degraded shape of
  the register, the matrix and the record is a reason, never an answer and never a
  traceback;
* parity, over the real tree -- the gate agrees with the original instruments
  whatever state they are in.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from conftest import PROGRAM_REGISTER, needs_evidence  # noqa: E402


def _load(name: str, as_name: str):
    spec = importlib.util.spec_from_file_location(as_name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[as_name] = module
    spec.loader.exec_module(module)
    return module


bp = _load("build_plan", "build_plan_gate_probe")
ps = _load("programme_supervisor", "programme_supervisor")
rd = _load("readiness", "readiness_gate_probe")
pc = ps.programme_checklist

ITEMS = "P2.checklist-gate.all-phase-blocking-checklist-items-verified-or-validly-waive"
REVIEW = "P2.checklist-gate.current-independent-checklist-supervisor-review-complete"
FSCK = "P2.checklist-gate.checklistfsck-pass"
BY_KIND = {"items": ITEMS, "review": REVIEW, "fsck": FSCK}
LABEL = {"items": "checklist items", "review": "supervisor review", "fsck": "ChecklistFsck"}


class _Blocked(BaseException):
    """Not an `OSError`: the tools under test swallow those and read on."""


@contextmanager
def _within(seconds: int):
    """A read that blocks (a FIFO) fails the test instead of hanging it."""
    def stop(_signum, _frame):
        raise _Blocked("the gate blocked")
    old = signal.signal(signal.SIGALRM, stop)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# --------------------------------------------------------------------------- #
# wiring: phase_gates with the instruments' answers handed in
# --------------------------------------------------------------------------- #

def _item(item_id, phase="P2", kind="gate"):
    return {"id": item_id, "phase": phase, "kind": kind, "text": item_id,
            "capability": False}


def _scope(*items):
    return {"phases": [{"phase": p} for p in ("P1", "P2", "P3")], "items": list(items)}


def _req(rid, phase, item=None, also=()):
    return {"id": rid, "name": rid, "target_phase": phase, "authoritative_item": item,
            "also_covers": list(also), "core_required_1_0": True}


def _answers(**changes):
    out = {kind: (True, f"{kind} is fine") for kind in BY_KIND}
    out.update(changes)
    return lambda: out


def _gate(monkeypatch, items, *, requirements=(), rows=(), answers=None):
    monkeypatch.setattr(bp, "_scope", lambda: _scope(*items))
    capabilities = [{"id": rid, "target_phase": "P2", "status": status,
                     "core_required_1_0": True} for rid, status in rows]
    gates = bp.phase_gates(capabilities, list(requirements),
                           checklist_measures=answers or _answers())[1]
    return gates["P2"]


THREE = [_item(ITEMS), _item(REVIEW), _item(FSCK)]


def test_the_instrument_map_names_exactly_the_three_checklist_gate_items():
    assert bp.CHECKLIST_GATE_ITEMS == {ITEMS: "items", REVIEW: "review", FSCK: "fsck"}


def test_three_met_instruments_open_a_gate_that_no_requirement_claims(monkeypatch):
    """The positive control: a gate that nothing could open is as useless as one that
    always opens. Red on the base, where the three read "nothing here measures yet"."""
    met, why = _gate(monkeypatch, THREE)
    assert met is True, why
    assert why == ("3 exit condition(s) met: 0 bound to a PROVEN requirement, "
                   "3 measured by the checklist instruments")


def test_the_instruments_count_beside_bound_items(monkeypatch):
    items = [_item("P2.exit.a"), *THREE]
    met, why = _gate(monkeypatch, items, requirements=[_req("P2-33", "P2", "P2.exit.a")],
                     rows=[("P2-33", "PROVEN")])
    assert met is True and why == ("4 exit condition(s) met: 1 bound to a PROVEN "
                                   "requirement, 3 measured by the checklist instruments"), why


@pytest.mark.parametrize("kind", sorted(BY_KIND))
def test_each_instrument_alone_holds_the_gate_with_its_reason(monkeypatch, kind):
    answers = _answers(**{kind: (False, f"REASON-{kind}")})
    met, why = _gate(monkeypatch, THREE, answers=answers)
    assert met is False
    assert "1 checklist-gate exit condition(s) measured by the checklist instruments " \
           "and unmet" in why, why
    assert f"{LABEL[kind]}: REASON-{kind}" in why, why
    assert why.count("REASON-") == 1, why
    assert "nothing here measures yet" not in why, why


def test_all_three_unmet_are_named_in_the_inventory_order(monkeypatch):
    answers = _answers(items=(False, "R1"), review=(False, "R2"), fsck=(False, "R3"))
    met, why = _gate(monkeypatch, THREE, answers=answers)
    assert met is False
    assert "3 checklist-gate exit condition(s)" in why, why
    assert why.index("checklist items: R1") < why.index("supervisor review: R2") \
        < why.index("ChecklistFsck: R3"), why


@pytest.mark.parametrize("kind", sorted(BY_KIND))
def test_a_requirement_that_claims_an_item_does_not_stand_in_for_its_instrument(
        monkeypatch, kind):
    """The shortcut that is forbidden: a PROVEN requirement named in `also_covers` must
    not read the condition met when the instrument says it is not."""
    answers = _answers(**{kind: (False, f"REASON-{kind}")})
    met, why = _gate(monkeypatch, THREE,
                     requirements=[_req("P2-90", "P2", "P2.exit.other", also=[BY_KIND[kind]])],
                     rows=[("P2-90", "PROVEN")], answers=answers)
    assert met is False and f"REASON-{kind}" in why, why
    met, why = _gate(monkeypatch, THREE,
                     requirements=[_req("P2-90", "P2", BY_KIND[kind])],
                     rows=[("P2-90", "PROVEN")], answers=answers)
    assert met is False and f"REASON-{kind}" in why, why


def test_a_claim_on_an_instrument_item_must_also_be_proven(monkeypatch):
    claim = [_req("P2-90", "P2", ITEMS)]
    met, why = _gate(monkeypatch, THREE, requirements=claim, rows=[("P2-90", "MISSING")])
    assert met is False and "P2-90 MISSING" in why, why
    met, why = _gate(monkeypatch, THREE, requirements=claim, rows=[("P2-90", "PROVEN")])
    assert met is True, why


def test_an_item_that_is_not_in_the_map_stays_unmeasured_and_unmet(monkeypatch):
    """The intent of `test_an_unbound_gate_item_still_counts_as_unmeasured`, with the
    three measured beside it: only the items the map names are measured. A fourth
    checklist gate the plan adds later, and any near spelling of a mapped id, is an
    exit condition nothing measures."""
    near = [ITEMS.upper(), ITEMS + " ", " " + REVIEW, FSCK[:-1], FSCK.replace("-", "_"),
            "x" + REVIEW]
    for spelling in ("P2.checklist-gate.a-fourth-condition", "P2.exit.free", *near):
        met, why = _gate(monkeypatch, [*THREE, _item(spelling)])
        assert met is False, spelling
        assert "1 exit condition(s) nothing here measures yet" in why, (spelling, why)
        assert "checklist-gate exit condition(s)" not in why, (spelling, why)
    met, why = _gate(monkeypatch, [_item(ITEMS.upper()), _item(REVIEW), _item(FSCK)])
    assert met is False and "1 exit condition(s) nothing here measures yet" in why, why


@pytest.mark.parametrize("answer", [
    (1, "ok"), ("True", "ok"), ("yes", "ok"), (None, "ok"), (True, 5), (True,),
    (True, "ok", "extra"), [True, "ok"], "ok", None, True,
], ids=repr)
def test_only_the_bool_true_with_a_text_reason_opens_a_condition(monkeypatch, answer):
    for kind in BY_KIND:
        met, why = _gate(monkeypatch, THREE, answers=_answers(**{kind: answer}))
        assert met is False, (kind, answer)
        assert f"{LABEL[kind]}: " in why and "no usable answer" in why, (kind, why)


def test_an_instrument_that_is_missing_a_kind_or_raises_holds_the_gate(monkeypatch):
    partial = lambda: {"items": (True, "ok"), "review": (True, "ok")}  # noqa: E731
    met, why = _gate(monkeypatch, THREE, answers=partial)
    assert met is False and f"{LABEL['fsck']}: " in why and "no usable answer" in why, why

    def boom():
        raise RuntimeError("the instrument fell over")
    met, why = _gate(monkeypatch, THREE, answers=boom)
    assert met is False and why.count("RuntimeError") == 3, why
    assert _gate(monkeypatch, THREE, answers=lambda: None)[0] is False


def test_a_reason_is_one_bounded_line(monkeypatch):
    assert bp._GATE_REASON_MAX == 240
    assert bp._bound("x" * 240) == "x" * 240
    assert bp._bound("x" * 241) == "x" * 237 + "..."
    assert bp._bound("a\n\nb\tc  d") == "a b c d"
    assert bp._bound("a\x1b[2Jb\x00c") == "a?[2Jb?c"
    answers = _answers(review=(False, "y" * 5000 + "\nsecond line"))
    met, why = _gate(monkeypatch, THREE, answers=answers)
    assert met is False
    assert ("supervisor review: " + "y" * 237 + "...") in why, why
    assert "y" * 238 not in why and "second line" not in why, why
    assert "\n" not in why


def test_a_phase_with_no_instrument_item_never_asks_the_instruments(monkeypatch):
    """The provider records that it was asked: an exception raised in it would be
    swallowed as "the instrument failed", which is what a phase without an instrument
    item must not even reach."""
    asked = []

    def provider():
        asked.append("asked")
        return {}
    met, why = _gate(monkeypatch, [_item("P2.exit.a")],
                     requirements=[_req("P2-33", "P2", "P2.exit.a")],
                     rows=[("P2-33", "PROVEN")], answers=provider)
    assert met is True
    assert why == "1 exit condition(s), each bound to a PROVEN requirement"
    assert asked == [], "the instruments were asked about a phase that has none"
    _gate(monkeypatch, THREE, answers=provider)
    assert asked == ["asked"], "a phase with an instrument item asks them exactly once"


def test_the_command_line_prints_and_records_the_reasons(tmp_path, monkeypatch, capsys):
    """`main` hands the measured rows and the register to `phase_gates`, and the
    instruments' reasons reach the console and the DAG file."""
    register = tmp_path / "program/v3_3/REQUIREMENTS.json"
    register.parent.mkdir(parents=True)
    register.write_text(json.dumps({"requirements": []}), encoding="utf-8")
    monkeypatch.setattr(bp, "HOH", tmp_path)
    monkeypatch.setattr(bp, "_scope", lambda: _scope(*THREE))

    class _Missing(Exception):
        pass

    class _Matrix:
        RegisterMissing = _Missing

        @staticmethod
        def measure():
            return {"capabilities": []}
    monkeypatch.setattr(bp, "_matrix", lambda: _Matrix)

    monkeypatch.setattr(bp, "checklist_gate_measures", _answers(review=(False, "REASON-R")))
    dag = tmp_path / "dag.json"
    assert bp.main(["--dag", str(dag)]) == 0
    out = capsys.readouterr().out
    assert "gate P2: " in out and "supervisor review: REASON-R" in out, out
    entry = json.loads(dag.read_text(encoding="utf-8"))["phase_gates"]["P2"]
    assert entry["external_met"] is False and "supervisor review: REASON-R" in entry["external"]

    monkeypatch.setattr(bp, "checklist_gate_measures", _answers())
    assert bp.main(["--dag", str(dag)]) == 0
    assert "gate P2" not in capsys.readouterr().out
    entry = json.loads(dag.read_text(encoding="utf-8"))["phase_gates"]["P2"]
    assert entry["external_met"] is True
    assert entry["external"].startswith("3 exit condition(s) met: 0 bound")


# --------------------------------------------------------------------------- #
# the instruments over a constructed tree
# --------------------------------------------------------------------------- #

#: A review is preserved under a UTC-stamped directory, and each later one under a later
#: stamp: the brief a review is performed on names the review before it.
STAMPS = tuple(f"20260924T{hour:02d}0000Z" for hour in range(10, 20))
OLDER = "20260923T120000Z"
REGISTER_JSON = "program/v3_3/REQUIREMENTS.json"
MATRIX_JSON = "program/v3_3/VERIHARNESS_CAPABILITY_MATRIX.json"
REVIEW_JSON = "dogfood/p2-gate/REVIEW.json"


def _requirement(rid, phase, **extra):
    return {"id": rid, "name": f"requirement {rid}", "target_phase": phase,
            "authoritative_item": f"{phase}.{rid}", "core_required_1_0": True, **extra}


def _capability(rid, status):
    return {"id": rid, "status": status, "target_phase": rid[:2],
            "evidence": [f"src/{rid.lower()}.py:1"]}


def _finding(fid="F-1", item="P2-01"):
    return {"finding_id": fid, "review_id": "R-test", "kind": "PROPOSE_REOPEN",
            "severity": "critical", "item_ids": [item],
            "reason": "the evidence for this item is older than the module it proves",
            "proposed_action": "re-measure the capability matrix",
            "supervisor_provider": "independent-supervisor"}


class Tree:
    """A P2 whose four requirements are PROVEN, under a root of its own that the
    gate, the board's row and the checklist binding are all pointed at."""

    def __init__(self, root: Path, monkeypatch) -> None:
        self.root = root
        self.reviews = 0
        root.mkdir()
        (root / "program/v3_3").mkdir(parents=True)
        (root / "dogfood/p2-gate").mkdir(parents=True)
        for module in (bp, rd, pc):
            monkeypatch.setattr(module, "HOH", root)
        monkeypatch.setattr(rd, "row_program_scope", lambda: rd.Row(
            "program_scope", rd.PASS, "pinned for this test", "n/a", "n/a"))
        monkeypatch.setattr(bp, "_programme_supervisor", lambda: ps, raising=False)
        monkeypatch.setattr(bp, "_readiness", lambda: rd, raising=False)
        self.register = {"scope_plan_sha256": "plan-digest-1", "requirements": [
            _requirement("P1-01", "P1"), _requirement("P2-01", "P2"),
            _requirement("P2-02", "P2"), _requirement("P2-03", "P2"),
            _requirement("P3-01", "P3")]}
        self.matrix = {"capabilities": [
            _capability("P1-01", "PROVEN"), _capability("P2-01", "PROVEN"),
            _capability("P2-02", "PROVEN"), _capability("P2-03", "PROVEN"),
            _capability("P3-01", "MISSING")]}
        self.write()

    def write(self) -> None:
        (self.root / REGISTER_JSON).write_text(json.dumps(self.register), encoding="utf-8")
        (self.root / MATRIX_JSON).write_text(json.dumps(self.matrix), encoding="utf-8")

    def status(self, rid: str, status: str) -> None:
        for row in self.matrix["capabilities"]:
            if row["id"] == rid:
                row["status"] = status
        self.write()

    def review(self, findings=(), *, phase="P2", decisions=None) -> None:
        """Preserve a review the way the real launch path does, and record it."""
        findings = list(findings)
        stamp = STAMPS[self.reviews]
        self.reviews += 1
        snapshot, _evidence, _items = ps.reconciled(self.register, self.matrix)
        capsule = (rd._canonical_brief(ps, register=self.register, matrix=self.matrix)
                   if phase == "P2"
                   else ps.capsule_for(phase, register=self.register, matrix=self.matrix))
        directory = self.root / "dogfood/p2-gate" / stamp
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "capsule-p2.json").write_text(
            capsule.model_dump_json(indent=1, by_alias=True), encoding="utf-8")
        (directory / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
        record = ps.record_review(
            phase, capsule=capsule, findings=findings, snapshot=snapshot,
            decisions=dict(decisions or {}),
            findings_source=f"dogfood/p2-gate/{stamp}/findings.json")
        (self.root / REVIEW_JSON).write_text(record.model_dump_json(indent=1),
                                             encoding="utf-8")

    def measures(self) -> dict:
        return bp.checklist_gate_measures()


@pytest.fixture
def tree(tmp_path, monkeypatch):
    return Tree(tmp_path / "tree", monkeypatch)


def _met(measures: dict) -> dict[str, bool]:
    for kind, value in measures.items():
        assert isinstance(value, tuple) and isinstance(value[0], bool) \
            and isinstance(value[1], str) and "\n" not in value[1], (kind, value)
    return {kind: value[0] for kind, value in measures.items()}


def test_a_complete_current_review_over_a_proven_p2_meets_all_three(tree):
    """The control every degraded case below is measured against."""
    tree.review()
    measures = tree.measures()
    assert _met(measures) == {"items": True, "review": True, "fsck": True}, measures
    assert "independence is a property of the dispatch" in measures["review"][1]


def _real_scope():
    program_scope = _load("program_scope", "program_scope_gate_probe")
    return program_scope.load_merged_scope(
        ROOT / "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json",
        ROOT / "program/v3_3/PROGRAMME_AMENDMENTS.json")


@pytest.fixture
def real_gate(tree, monkeypatch):
    """`phase_gates` over the real plan inventory, with the real instruments reading
    the constructed tree and the five exit-includes bound to PROVEN rows."""
    needs_evidence(PROGRAM_REGISTER)
    scope = _real_scope()
    monkeypatch.setattr(bp, "_scope", lambda: scope)
    exits = [i["id"] for i in scope["items"]
             if i["phase"] == "P2" and i["section"] == "Exit includes:"]
    assert len(exits) == 5
    requirements = [_req(f"P2-{33 + n}", "P2", item) for n, item in enumerate(exits)]
    rows = [{"id": r["id"], "target_phase": "P2", "status": "PROVEN",
             "core_required_1_0": True} for r in requirements]

    def gate():
        return bp.phase_gates(rows, requirements)[1]["P2"]
    return tree, gate


def test_the_real_inventory_has_exactly_the_three_checklist_gate_items():
    needs_evidence(PROGRAM_REGISTER)
    in_plan = {i["id"] for i in _real_scope()["items"]
               if i["phase"] == "P2" and i["section"] == "Checklist gate:"}
    assert in_plan == set(bp.CHECKLIST_GATE_ITEMS)


def test_p2_opens_when_all_three_instruments_agree_and_not_before(real_gate):
    """Red on the base: there the three items read "nothing here measures yet" in the
    very state in which every instrument is satisfied."""
    tree, gate = real_gate
    met, why = gate()
    assert met is False and "supervisor review: the review record is missing" in why, why
    tree.review()
    met, why = gate()
    assert met is True, why
    assert why == ("8 exit condition(s) met: 5 bound to a PROVEN requirement, "
                   "3 measured by the checklist instruments")


def _stale(tree):
    tree.matrix["capabilities"][0]["evidence"] = ["src/changed-after-the-review.py:1"]
    tree.write()


def _missing(tree):
    (tree.root / REVIEW_JSON).rename(tree.root / "dogfood/p2-gate/REVIEW.moved.json")


def _wrong_phase(tree):
    tree.review(phase="P1")


def _blocking(tree):
    tree.review([_finding()])


def _open_older(tree):
    older = tree.root / "dogfood/p2-gate" / OLDER
    older.mkdir()
    (older / "findings.json").write_text(json.dumps([_finding("F-OLD")]), encoding="utf-8")


def _unproven(tree):
    tree.status("P2-02", "PARTIAL")
    tree.review()


def _waived(tree):
    row = next(r for r in tree.register["requirements"] if r["id"] == "P2-02")
    row["waiver"] = {"document": "docs/ROUTING.md", "reason": "later"}
    tree.status("P2-02", "PARTIAL")
    tree.review()


DEGRADED = {
    # name: (what to do after a complete review, kinds that must read unmet, reason fragment)
    "stale": (_stale, {"review"}, "matrix digest changed"),
    "missing": (_missing, {"review"}, "the review record is missing"),
    "wrong-phase": (_wrong_phase, {"review"}, "P1"),
    "blocking-finding": (_blocking, {"fsck", "review"}, "1 blocking finding(s)"),
    "open-older-finding": (_open_older, {"fsck", "review"},
                           "release-blocking finding F-OLD is unresolved"),
    "unproven-row": (_unproven, {"items"}, "P2-02"),
    "waived-is-not-verified": (_waived, {"items"}, "P2-02"),
}


@pytest.mark.parametrize("name", sorted(DEGRADED))
def test_each_degraded_state_is_unmet_for_its_own_reason(tree, name):
    tree.review()
    assert all(_met(tree.measures()).values())
    degrade, unmet, fragment = DEGRADED[name]
    degrade(tree)
    measures = tree.measures()
    assert _met(measures) == {k: k not in unmet for k in BY_KIND}, measures
    assert any(fragment in measures[k][1] for k in unmet), (fragment, measures)


class _Row:
    def __init__(self, state, value_="BOARD-DETAIL"):
        self.state, self.value_ = state, value_


class _Board:
    PASS = "PASS"

    def __init__(self, outcome):
        self.outcome = outcome
        self.HOH = bp.HOH            # the tree the board judges: the gate's own

    def row_programme_supervisor(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.parametrize("outcome", [
    _Row("FAIL"), _Row("NOT_RUN"), _Row("pass"), _Row("PASS "), _Row(True), _Row(None),
    object(), None, RuntimeError("the row fell over")], ids=lambda o: type(o).__name__
    + str(getattr(o, "state", "")))
def test_the_review_is_met_only_when_the_boards_row_says_exactly_pass(
        tree, monkeypatch, outcome):
    """The record on file is a good one here; the verdict is the row's. Anything but
    the state `PASS` -- a row that is NOT_RUN is not a pass either -- holds the gate."""
    tree.review()
    monkeypatch.setattr(bp, "_readiness", lambda: _Board(_Row("PASS")))
    assert tree.measures()["review"][0] is True
    monkeypatch.setattr(bp, "_readiness", lambda: _Board(outcome))
    met, why = tree.measures()["review"]
    assert met is False, why
    if isinstance(outcome, _Row):
        assert f"programme_supervisor row is {outcome.state}: BOARD-DETAIL" in why, why
    else:
        assert "could not be evaluated" in why, why


def test_a_phase_not_ready_finding_holds_the_review_even_when_applied_with_evidence(tree):
    """O229/G, through the gate: no decision settles a finding."""
    finding = {**_finding(), "kind": "PHASE_NOT_READY", "item_ids": ["P2"],
               "proposed_action": "hold phase P2 open"}
    tree.review([finding], decisions={"F-1": {"disposition": "apply",
                                              "evidence": "tools/preserve.py:1"}})
    measures = tree.measures()
    assert measures["review"][0] is False and "blocking finding" in measures["review"][1]


def test_a_non_blocking_requirement_that_is_not_proven_does_not_hold_the_items(tree):
    tree.register["requirements"].append(
        _requirement("P2-04", "P2", optional_provider=True, core_required_1_0=False))
    tree.matrix["capabilities"].append(_capability("P2-04", "MISSING"))
    tree.write()
    tree.review()
    assert all(_met(tree.measures()).values())


def test_without_a_p2_item_the_phase_is_not_closed_by_having_nothing_to_check(tree):
    tree.register["requirements"] = [r for r in tree.register["requirements"]
                                     if r["target_phase"] != "P2"]
    tree.matrix["capabilities"] = [c for c in tree.matrix["capabilities"]
                                   if not c["id"].startswith("P2")]
    tree.write()
    measures = tree.measures()
    assert _met(measures)["items"] is False and "(phase)" in measures["items"][1], measures


def test_a_register_with_no_plan_revision_is_unsound_and_gives_no_verdict(tree):
    del tree.register["scope_plan_sha256"]
    tree.write()
    measures = tree.measures()
    assert _met(measures)["items"] is False and _met(measures)["fsck"] is False
    assert "not sound" in measures["items"][1] and "no plan revision" in measures["items"][1]


def test_the_items_reason_names_five_blockers_and_counts_the_rest(tree):
    tree.review()
    for n in range(4, 8):
        tree.register["requirements"].append(_requirement(f"P2-0{n}", "P2"))
        tree.matrix["capabilities"].append(_capability(f"P2-0{n}", "MISSING"))
    tree.status("P2-01", "PARTIAL")
    tree.status("P2-02", "PARTIAL")
    tree.status("P2-03", "PARTIAL")
    assert tree.measures()["items"] == (False, (
        "7 P2 blocker(s) neither VERIFIED nor WAIVED: P2-01, P2-02, P2-03, P2-04, "
        "P2-05 and 2 more"))
    for n in (6, 7):
        tree.status(f"P2-0{n}", "PROVEN")
    assert tree.measures()["items"] == (False, (
        "5 P2 blocker(s) neither VERIFIED nor WAIVED: P2-01, P2-02, P2-03, P2-04, P2-05"))


def test_the_met_reason_carries_the_row_detail_cut_at_160_and_the_disclosure(
        tree, monkeypatch):
    tree.review()
    monkeypatch.setattr(bp, "_readiness", lambda: _Board(_Row("PASS", "x" * 5000)))
    met, why = tree.measures()["review"]
    assert met is True
    assert why == ("x" * 157 + "...; independence is a property of the dispatch and "
                   "is not measured here"), why


@pytest.mark.parametrize("which", ["checklist", "board", "board-without-HOH"])
def test_a_loaded_tool_that_judges_another_tree_holds_the_gate(
        tree, monkeypatch, tmp_path, which):
    """Items and fsck are read through the checklist binding the supervisor module
    carries, the review through the board's row; each derives its own `HOH`. The gate
    reads its register, matrix and record from `bp.HOH`, so a tool pointed at another
    tree (or one with no `HOH`) must not be asked: two trees' answers would be mixed."""
    tree.review()
    assert all(_met(tree.measures()).values())
    elsewhere = tmp_path / "another-tree"
    elsewhere.mkdir()
    if which == "checklist":
        monkeypatch.setattr(pc, "HOH", elsewhere)
    elif which == "board":
        monkeypatch.setattr(rd, "HOH", elsewhere)
    else:
        monkeypatch.delattr(rd, "HOH")
    measures = tree.measures()
    assert "judges another tree" in measures["review"][1], measures
    if which == "checklist":
        assert not any(_met(measures).values()), measures
        assert all("judges another tree" in measures[k][1] for k in BY_KIND), measures
    else:
        assert _met(measures) == {"items": True, "fsck": True, "review": False}, measures


def test_a_tool_that_reaches_the_same_tree_by_a_link_is_not_another_tree(
        tree, monkeypatch, tmp_path):
    """The control: the comparison is of resolved paths, not of spellings."""
    alias = tmp_path / "alias-of-the-tree"
    alias.symlink_to(tree.root)
    tree.review()
    monkeypatch.setattr(pc, "HOH", alias)
    monkeypatch.setattr(rd, "HOH", alias)
    assert all(_met(tree.measures()).values())


def test_a_requirement_without_an_id_is_a_reason_not_a_traceback(tree):
    tree.register["requirements"].append({"name": "no id"})
    tree.write()
    measures = tree.measures()
    assert _met(measures)["items"] is False and _met(measures)["fsck"] is False
    assert "could not be derived" in measures["items"][1], measures


# --- unreadable and odd input (fail closed, a reason, no traceback) -------------

def _shape_missing(path: Path, outside: Path) -> None:
    pass


def _shape_dangling(path: Path, outside: Path) -> None:
    path.symlink_to(path.parent / "nowhere.json")


def _shape_directory(path: Path, outside: Path) -> None:
    path.mkdir()


def _shape_fifo(path: Path, outside: Path) -> None:
    os.mkfifo(path)


def _shape_unreadable(path: Path, outside: Path) -> None:
    path.write_text("{}", encoding="utf-8")
    path.chmod(0)
    if os.access(path, os.R_OK):
        pytest.skip("permission bits are not enforced for this user here")


def _shape_corrupt(path: Path, outside: Path) -> None:
    path.write_text('{"requirements": [', encoding="utf-8")


def _shape_list(path: Path, outside: Path) -> None:
    path.write_text("[]", encoding="utf-8")


def _shape_scalar(path: Path, outside: Path) -> None:
    path.write_text("42", encoding="utf-8")


def _shape_empty(path: Path, outside: Path) -> None:
    path.write_bytes(b"")


def _shape_utf16(path: Path, outside: Path) -> None:
    path.write_bytes("{}".encode("utf-16"))


def _shape_latin1(path: Path, outside: Path) -> None:
    path.write_bytes(b'{"name": "caf\xe9"}')


def _shape_nul(path: Path, outside: Path) -> None:
    path.write_bytes(b'{"a":\x00}')


def _shape_deep(path: Path, outside: Path) -> None:
    path.write_text("[" * 200000, encoding="utf-8")


def _shape_outside(path: Path, outside: Path) -> None:
    outside.mkdir(exist_ok=True)
    target = outside / path.name
    target.write_bytes(path.read_bytes())
    path.rename(path.with_name(path.name + ".moved"))
    path.symlink_to(target)


SHAPES = {f.__name__[len("_shape_"):]: f for f in (
    _shape_missing, _shape_dangling, _shape_directory, _shape_fifo, _shape_unreadable,
    _shape_corrupt, _shape_list, _shape_scalar, _shape_empty, _shape_utf16,
    _shape_latin1, _shape_nul, _shape_deep, _shape_outside)}
TARGETS = {"register": (REGISTER_JSON, "the requirement register"),
           "matrix": (MATRIX_JSON, "the capability matrix"),
           "record": (REVIEW_JSON, "the review record")}


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("target", sorted(TARGETS))
def test_an_input_the_gate_cannot_read_is_a_reason_and_never_an_answer(
        tree, target, shape):
    tree.review()
    relative, label = TARGETS[target]
    path = tree.root / relative
    if shape != "outside":
        path.rename(path.with_name(path.name + ".good"))
    SHAPES[shape](path, tree.root.parent / "outside")
    with _within(10):
        measures = tree.measures()
    met = _met(measures)
    if target == "record":
        assert met["review"] is False and label in measures["review"][1], measures
        assert met["items"] is True and met["fsck"] is True, measures
    else:
        assert not any(met.values()), measures
        assert all(label in measures[k][1] for k in BY_KIND), measures


def _shape_strings(path: Path, outside: Path) -> None:
    path.write_text('["F9: critical, written as a string"]', encoding="utf-8")


def _shape_wrong_key(path: Path, outside: Path) -> None:
    path.write_text('{"findings": "none"}', encoding="utf-8")


def _shape_object_entry_of_the_wrong_type(path: Path, outside: Path) -> None:
    path.write_text('{"findings": [1]}', encoding="utf-8")


def _shape_finding_without_an_id(path: Path, outside: Path) -> None:
    path.write_text('[{"kind": "FINDING", "severity": "critical", "title": "open"}]',
                    encoding="utf-8")


def _shape_finding_with_a_blank_id(path: Path, outside: Path) -> None:
    path.write_text('[{"finding_id": "  ", "kind": "FINDING", "severity": "critical"}]',
                    encoding="utf-8")


def _shape_object_without_the_findings_key(path: Path, outside: Path) -> None:
    path.write_text('{"results": [{"finding_id": "F9", "kind": "FINDING", '
                    '"severity": "critical"}]}', encoding="utf-8")


#: Not "missing" (no file is no review) and not "list" (`[]` is a good findings file).
FINDING_SHAPES = {k: v for k, v in SHAPES.items() if k not in ("missing", "list")}
FINDING_SHAPES.update(strings=_shape_strings, wrong_key=_shape_wrong_key,
                      wrong_entry=_shape_object_entry_of_the_wrong_type,
                      no_id=_shape_finding_without_an_id, blank_id=_shape_finding_with_a_blank_id,
                      no_findings_key=_shape_object_without_the_findings_key)


@pytest.mark.parametrize("shape", sorted(FINDING_SHAPES))
def test_a_preserved_review_whose_findings_cannot_be_read_holds_fsck(tree, shape):
    """The binding skips a review it cannot parse, and an entry that is not an object, so
    an open finding could be hidden by spoiling the file that raised it."""
    tree.review()
    older = tree.root / "dogfood/p2-gate" / OLDER
    older.mkdir()
    path = older / "findings.json"
    if shape != "outside":
        path.write_text("[]", encoding="utf-8")
        path.rename(path.with_name("findings.good"))
    else:
        path.write_text("[]", encoding="utf-8")
    FINDING_SHAPES[shape](path, tree.root.parent / "outside")
    with _within(10):
        measures = tree.measures()
    assert _met(measures) == {"items": True, "fsck": False, "review": False}, measures
    assert f"{OLDER}/findings.json" in measures["fsck"][1], measures
    assert f"{OLDER}/findings.json" in measures["review"][1], measures


@pytest.mark.parametrize("content", ["[]", '{"findings": []}', json.dumps([
    {"finding_id": "F-0", "kind": "NO_ACTION", "severity": "low"}])], ids=["list", "object", "quiet"])
def test_a_readable_preserved_review_with_nothing_open_is_no_problem(tree, content):
    tree.review()
    older = tree.root / "dogfood/p2-gate" / OLDER
    older.mkdir()
    (older / "findings.json").write_text(content, encoding="utf-8")
    assert _met(tree.measures())["fsck"] is True


def test_what_the_binding_does_not_count_as_a_review_is_not_read_either(tree):
    """A stamped directory with no `findings.json`, a directory that is not named with a
    UTC stamp, and a stamped name that is a link to a directory are not reviews: the
    same rule as the binding's (`programme_checklist.py:349`, `readiness.py:514`)."""
    tree.review()
    (tree.root / "dogfood/p2-gate" / OLDER).mkdir()
    spoiled = tree.root / "dogfood/p2-gate" / "draft"
    spoiled.mkdir()
    (spoiled / "findings.json").write_text("{not json", encoding="utf-8")
    assert _met(tree.measures())["fsck"] is True
    (tree.root / "dogfood/p2-gate" / "20260922T120000Z").symlink_to("draft")
    assert _met(tree.measures())["fsck"] is True


@pytest.mark.parametrize("directory", ["program", "dogfood/p2-gate"])
def test_a_directory_that_is_a_link_out_of_the_tree_is_not_this_trees(tree, directory):
    """A symlinked directory is the same escape as a symlinked file."""
    tree.review()
    here = tree.root / directory
    outside = tree.root.parent / "outside" / directory.replace("/", "_")
    outside.parent.mkdir(exist_ok=True)
    here.rename(outside)
    here.symlink_to(outside)
    with _within(10):
        measures = tree.measures()
    met = _met(measures)
    if directory == "program":
        assert not any(met.values()), measures
    else:
        assert met == {"items": True, "fsck": False, "review": False}, measures
        assert "resolves outside the repository" in measures["fsck"][1], measures
        assert "resolves outside the repository" in measures["review"][1], measures


WRONG = {
    "requirements-a-string": ("register", {"requirements": "P2-01"}),
    "requirements-not-objects": ("register", {"requirements": [1, "x", None]}),
    "requirements-an-object": ("register", {"requirements": {"P2-01": {}}}),
    "requirements-twice": ("register", "duplicate"),
    "capabilities-a-string": ("matrix", {"capabilities": "P2-01"}),
    "capabilities-not-objects": ("matrix", {"capabilities": [1, None]}),
    "capabilities-an-object": ("matrix", {"capabilities": {"P2-01": "PROVEN"}}),
    "capabilities-empty": ("matrix", {"capabilities": []}),
    "status-not-text": ("matrix", "status"),
}


@pytest.mark.parametrize("name", sorted(WRONG))
def test_a_wrongly_typed_field_is_a_reason_and_never_a_traceback_or_a_pass(tree, name):
    which, change = WRONG[name]
    tree.review()
    if change == "duplicate":
        tree.register["requirements"].append(dict(tree.register["requirements"][1]))
    elif change == "status":
        tree.matrix["capabilities"][1]["status"] = ["PROVEN"]
    elif which == "register":
        tree.register["requirements"] = change["requirements"]
    else:
        tree.matrix["capabilities"] = change["capabilities"]
    tree.write()
    met = _met(tree.measures())
    assert met["items"] is False, name


def test_an_input_in_the_tree_behind_a_link_is_read_like_the_file(tree):
    """The control for the case above: a link that stays inside the repository is not
    a refusal."""
    tree.review()
    for relative in (REGISTER_JSON, MATRIX_JSON, REVIEW_JSON):
        path = tree.root / relative
        real = path.with_name(path.name + ".real")
        path.rename(real)
        path.symlink_to(real.name)
    assert all(_met(tree.measures()).values())


def test_the_ceiling_on_what_is_read_is_exact_and_large_enough_for_the_real_inputs(
        tree, monkeypatch):
    assert bp._GATE_INPUT_MAX_BYTES == 32 * 1024 * 1024
    path = tree.root / MATRIX_JSON
    body = path.read_bytes()
    monkeypatch.setattr(bp, "_GATE_INPUT_MAX_BYTES", len(body))
    assert bp._gate_input(path, "the matrix")[0] is not None
    monkeypatch.setattr(bp, "_GATE_INPUT_MAX_BYTES", len(body) - 1)
    got, why = bp._gate_input(path, "the matrix")
    assert got is None and why == f"the matrix is larger than {len(body) - 1} bytes", why
    real = [ROOT / REGISTER_JSON, ROOT / MATRIX_JSON, ROOT / REVIEW_JSON]
    for file in (f for f in real if f.is_file()):
        assert file.stat().st_size * 8 < 32 * 1024 * 1024, file


def test_odd_text_in_a_name_is_never_a_traceback(tree):
    tree.register["requirements"][1]["name"] = "nul \x00 and surrogate \ud800"
    tree.write()
    assert set(_met(tree.measures())) == set(BY_KIND)


# --------------------------------------------------------------------------- #
# parity with the original instruments, over the real tree
# --------------------------------------------------------------------------- #

def _checklist_tool(*args):
    done = subprocess.run([sys.executable, "-B", str(ROOT / "tools/programme_checklist.py"),
                           *args], capture_output=True, text=True, cwd=ROOT, timeout=300)
    return done.returncode, done.stdout


def test_items_and_fsck_agree_with_the_checklist_tool_whatever_its_state():
    """Time-invariant: red tomorrow only if the gate and the tool disagree."""
    needs_evidence(PROGRAM_REGISTER)
    measures = bp.checklist_gate_measures()
    code, out = _checklist_tool("--json")
    report = json.loads(out[out.index("\n{") + 1:] if "\n{" in out else out)
    assert measures["items"][0] is report["P2"]["closed"], (measures, report["P2"])
    assert measures["fsck"][0] is (code == 0), (measures, code)


def test_the_review_condition_is_the_boards_programme_supervisor_row():
    needs_evidence(PROGRAM_REGISTER)
    row = rd.row_programme_supervisor()
    review = bp.checklist_gate_measures()["review"]
    assert review[0] is (row.state == rd.PASS), (review, row.state, row.value_)
