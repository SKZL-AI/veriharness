"""The gate counter says why it moved (P3 analysis R-5).

`gate P3: 26 blocking open` stood for 33 merges, read 25 once, and went back to 26 because
a requirement (P3-42) was registered -- legitimately, and invisibly: the number was the
same word for "extended" and for "fell back". `build_plan` now prints its provenance beside
the number, against the committed capability matrix (the last records checkpoint that
regenerated it): `(25 at f78d1cb9edab; +1 added: P3-42 at f03b5801; -1 lifted: P3-06;
0 regressed)`.

Everything runs over a synthetic git history in a temporary directory: a register at one
commit, a matrix regenerated at another, a requirement added at a third.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from test_succession_records_window import _git, _write  # noqa: E402


def _load(name: str, as_name: str):
    spec = importlib.util.spec_from_file_location(as_name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[as_name] = module
    spec.loader.exec_module(module)
    return module


bp = _load("build_plan", "build_plan_counter_probe")
REGISTER = bp.REGISTER_JSON
MATRIX = bp.MATRIX_JSON


def _req(rid: str, phase: str, **extra) -> dict:
    return {"id": rid, "name": rid, "target_phase": phase, "core_required_1_0": True,
            "optional_provider": False, "ui_only": False, "depends_on": [], **extra}


def _row(rid: str, phase: str, status: str, **extra) -> dict:
    return {"id": rid, "name": rid, "target_phase": phase, "status": status,
            "core_required_1_0": True, "optional_provider": False, "ui_only": False,
            "depends_on": [], **extra}


def _json(obj) -> str:
    return json.dumps(obj, indent=2) + "\n"


class History:
    """R1: the register (P3-01..P3-03, an optional P3-09, P4-01); M: the matrix, committed
    (the baseline); R2: the register gains P3-04."""

    def __init__(self, root: Path, monkeypatch) -> None:
        self.root = root
        root.mkdir(parents=True)
        _git(root, "init", "-q")
        self.su = _load("succession", "succession_for_counter_tests")
        monkeypatch.setattr(self.su, "HOH", root)
        monkeypatch.setattr(bp, "HOH", root)
        monkeypatch.setattr(bp, "_succession", lambda: self.su)
        self.register = [_req("P3-01", "P3"), _req("P3-02", "P3"), _req("P3-03", "P3"),
                         _req("P3-09", "P3", optional_provider=True), _req("P4-01", "P4")]
        _write(root, REGISTER, _json({"requirements": self.register}))
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "R1: register")
        self.r1 = _git(root, "rev-parse", "--short", "HEAD")
        self.baseline_rows = [
            _row("P3-01", "P3", "MISSING"), _row("P3-02", "P3", "PARTIAL"),
            _row("P3-03", "P3", "PROVEN"), _row("P3-09", "P3", "MISSING", optional_provider=True),
            _row("P4-01", "P4", "MISSING")]
        _write(root, MATRIX, _json({"capabilities": self.baseline_rows}))
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "M: matrix regenerated")
        self.m_full = _git(root, "rev-parse", "HEAD")
        self.register.append(_req("P3-04", "P3"))
        _write(root, REGISTER, _json({"requirements": self.register}))
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "R2: P3-04 registered")
        self.r2 = _git(root, "rev-parse", "--short", "HEAD")
        # a later, unrelated commit: the register edit must not be dated by it
        _write(root, "README.md", "x\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "records")
        self.live = [
            _row("P3-01", "P3", "PROVEN"), _row("P3-02", "P3", "MISSING"),
            _row("P3-03", "P3", "MISSING"), _row("P3-04", "P3", "MISSING"),
            _row("P3-09", "P3", "MISSING", optional_provider=True),
            _row("P4-01", "P4", "MISSING")]


@pytest.fixture
def history(tmp_path, monkeypatch):
    return History(tmp_path / "repo", monkeypatch)


# --------------------------------------------------------------------------- #
# the reading, pure
# --------------------------------------------------------------------------- #

def _baseline(rows, commit="f" * 40):
    return {"commit": commit, "rows": {r["id"]: r for r in rows}}


def test_added_lifted_removed_and_regressed_are_told_apart():
    then = [_row("A-1", "PX", "MISSING"), _row("A-2", "PX", "PARTIAL"),
            _row("A-3", "PX", "PROVEN"), _row("A-4", "PX", "MISSING")]
    now = [_row("A-1", "PX", "PROVEN"), _row("A-2", "PX", "MISSING"),
           _row("A-3", "PX", "IMPLEMENTED_NOT_PROVEN"), _row("A-5", "PX", "MISSING")]
    c = bp.blocking_counter("PX", now, _baseline(then))
    assert c["previous_open"] == ["A-1", "A-2", "A-4"]
    assert c["now_open"] == ["A-2", "A-3", "A-5"]
    assert c["added"] == ["A-5"] and c["lifted"] == ["A-1"]
    assert c["removed"] == ["A-4"] and c["moved_out"] == []
    assert c["regressed"] == ["A-2", "A-3"] and c["regressed_entering"] == ["A-3"], \
        "A-3 went PROVEN -> open (entered); A-2 stayed open at a lower status"
    assert c["regressed_still_open"] == ["A-2"]
    assert c["reconciled"] is True
    assert c["entered"] == ["A-3", "A-5"] and c["left"] == ["A-1", "A-4"]


# --- completeness: every difference of N is in a named list (the A/B reviewers' F1) ----

def _identity(c: dict) -> bool:
    """now == previous + (added, moved_in, newly_blocking, regressed entering) - (lifted,
    no longer blocking, removed, moved out)."""
    gained = (len(c["added"]) + len(c["moved_in"]) + len(c["newly_blocking"])
              + len(c["regressed_entering"]))
    lost = (len(c["lifted"]) + len(c["no_longer_blocking"]) + len(c["removed"])
            + len(c["moved_out"]))
    return len(c["now_open"]) == len(c["previous_open"]) + gained - lost


def test_a_row_that_became_blocking_at_an_unchanged_status_is_named_not_invisible():
    """The reviewers' repro: A-1 was an optional provider (not blocking) and is blocking
    now, MISSING in both. N moved 0 -> 1 and the first version printed
    `+0 added; -0 lifted; 0 regressed`."""
    then = [_row("A-1", "PX", "MISSING", optional_provider=True)]
    now = [_row("A-1", "PX", "MISSING")]
    c = bp.blocking_counter("PX", now, _baseline(then))
    assert c["previous_open"] == [] and c["now_open"] == ["A-1"]
    assert c["newly_blocking"] == ["A-1"] and c["added"] == [] and c["regressed"] == []
    assert c["reconciled"] is True and _identity(c)
    assert "+1 newly blocking: A-1" in bp.counter_text(c)


def test_a_ui_only_row_that_became_blocking_is_newly_blocking_too():
    c = bp.blocking_counter("PX", [_row("A-1", "PX", "PARTIAL")],
                            _baseline([_row("A-1", "PX", "PARTIAL", ui_only=True)]))
    assert c["newly_blocking"] == ["A-1"] and _identity(c)


def test_a_row_that_stopped_being_blocking_at_an_unchanged_status_is_named():
    c = bp.blocking_counter("PX", [_row("A-1", "PX", "MISSING", optional_provider=True)],
                            _baseline([_row("A-1", "PX", "MISSING")]))
    assert c["no_longer_blocking"] == ["A-1"] and c["lifted"] == []
    assert c["now_open"] == [] and c["reconciled"] is True and _identity(c)
    assert "-1 no longer blocking: A-1" in bp.counter_text(c)


def test_a_waiver_that_went_away_at_an_unchanged_status_is_a_regression():
    waiver = {"document": "docs/x.md", "reason": "later"}
    c = bp.blocking_counter("PX", [_row("A-1", "PX", "MISSING")],
                            _baseline([_row("A-1", "PX", "MISSING", waiver=waiver)]))
    assert c["regressed"] == ["A-1"] and c["regressed_entering"] == ["A-1"]
    assert c["newly_blocking"] == [] and c["reconciled"] is True and _identity(c)


def test_a_row_that_moved_out_of_the_phase_is_named_and_one_that_moved_in_too():
    then = [_row("A-1", "PX", "MISSING"), _row("B-1", "PY", "MISSING")]
    now = [_row("A-1", "PY", "MISSING"), _row("B-1", "PX", "MISSING")]
    c = bp.blocking_counter("PX", now, _baseline(then))
    assert c["moved_out"] == ["A-1"] and c["removed"] == []
    assert c["moved_in"] == ["B-1"] and c["added"] == []
    assert c["reconciled"] is True and _identity(c)
    text = bp.counter_text(c)
    assert "+1 moved in: B-1" in text and "-1 moved out: A-1" in text, text


def test_a_row_lifted_by_a_waiver_while_its_status_fell_is_lifted_only():
    """The row is satisfied now, so it left; it is not also listed as a regression."""
    waiver = {"document": "docs/x.md", "reason": "later"}
    c = bp.blocking_counter("PX", [_row("A-1", "PX", "MISSING", waiver=waiver)],
                            _baseline([_row("A-1", "PX", "PARTIAL")]))
    assert c["lifted"] == ["A-1"] and c["regressed"] == [] and c["now_open"] == []


def test_a_row_with_a_status_the_reading_cannot_take_never_breaks_the_identity():
    for status in ("PROVEN", "MISSING", "NOT_DETERMINABLE", "SOMETHING_NEW"):
        for flag in ({}, {"optional_provider": True}):
            c = bp.blocking_counter("PX", [_row("A-1", "PX", status, **flag)],
                                    _baseline([_row("A-1", "PX", "PARTIAL")]))
            assert c["reconciled"] is True and _identity(c), (status, flag)


def test_every_difference_in_a_mixed_history_is_named():
    """Eight rows, one of each kind of change at once."""
    then = [_row("R-1", "PX", "PROVEN"), _row("R-2", "PX", "MISSING", optional_provider=True),
            _row("R-3", "PX", "MISSING"), _row("R-4", "PX", "MISSING"),
            _row("R-5", "PX", "MISSING"), _row("R-6", "PX", "MISSING"),
            _row("R-7", "PY", "MISSING"), _row("R-8", "PX", "PARTIAL")]
    now = [_row("R-1", "PX", "MISSING"), _row("R-2", "PX", "MISSING"),
           _row("R-3", "PX", "PROVEN"), _row("R-4", "PX", "MISSING", ui_only=True),
           _row("R-5", "PY", "MISSING"), _row("R-7", "PX", "MISSING"),
           _row("R-8", "PX", "MISSING"), _row("R-9", "PX", "MISSING")]
    c = bp.blocking_counter("PX", now, _baseline(then))
    assert c["regressed_entering"] == ["R-1"] and c["newly_blocking"] == ["R-2"]
    assert c["lifted"] == ["R-3"] and c["no_longer_blocking"] == ["R-4"]
    assert c["moved_out"] == ["R-5"] and c["removed"] == ["R-6"]
    assert c["moved_in"] == ["R-7"] and c["added"] == ["R-9"]
    assert c["regressed_still_open"] == ["R-8"]
    assert c["reconciled"] is True and _identity(c)


def test_a_status_outside_the_order_ranks_below_every_status_inside_it():
    c = bp.blocking_counter("PX", [_row("A-1", "PX", "NOT_DETERMINABLE")],
                            _baseline([_row("A-1", "PX", "MISSING")]))
    assert c["regressed"] == ["A-1"]
    same = bp.blocking_counter("PX", [_row("A-1", "PX", "MISSING")],
                               _baseline([_row("A-1", "PX", "MISSING")]))
    assert same["regressed"] == []


def test_rows_that_do_not_hold_the_gate_shut_are_not_counted():
    then = [_row("A-1", "PX", "MISSING", optional_provider=True),
            _row("A-2", "PX", "MISSING", ui_only=True),
            _row("A-3", "PX", "MISSING", waiver={"document": "docs/x.md", "reason": "later"})]
    now = [dict(r) for r in then]
    c = bp.blocking_counter("PX", now, _baseline(then))
    assert c["previous_open"] == [] and c["now_open"] == [] and c["added"] == []


def test_a_row_added_already_satisfied_is_not_blocking_open():
    c = bp.blocking_counter("PX", [_row("A-9", "PX", "PROVEN")], _baseline([]))
    assert c["added"] == [] and c["now_open"] == []


def test_another_phase_does_not_leak_in_and_a_row_that_moved_in_is_moved_in():
    then = [_row("B-1", "PY", "MISSING"), _row("A-1", "PX", "MISSING")]
    now = [_row("B-1", "PX", "MISSING"), _row("A-1", "PX", "MISSING"),
           _row("C-1", "PZ", "MISSING")]
    c = bp.blocking_counter("PX", now, _baseline(then))
    assert c["previous_open"] == ["A-1"] and c["now_open"] == ["A-1", "B-1"]
    assert c["moved_in"] == ["B-1"] and c["added"] == []


# --------------------------------------------------------------------------- #
# the text
# --------------------------------------------------------------------------- #

def _counter(**kw) -> dict:
    base = {"phase": "P3", "baseline_commit": "f78d1cb9edab95efeba5f8ae3f10edaecb58ebb0",
            "previous_open": [], "now_open": [], "entered": [], "left": [], "added": [],
            "moved_in": [], "newly_blocking": [], "regressed": [], "regressed_entering": [],
            "regressed_still_open": [], "lifted": [], "no_longer_blocking": [],
            "removed": [], "moved_out": [], "reconciled": True}
    base.update(kw)
    return base


def test_the_text_is_the_line_of_the_analysis():
    c = _counter(previous_open=["x"] * 25, added=["P3-42"], lifted=["P3-06"])
    assert bp.counter_text(c, {"P3-42": "f03b5801"}) == (
        "(25 at f78d1cb9edab; +1 added: P3-42 at f03b5801; -1 lifted: P3-06; 0 regressed)")
    quiet = {**c, "added": [], "lifted": []}
    assert bp.counter_text(quiet) == "(25 at f78d1cb9edab; +0 added; -0 lifted; 0 regressed)"
    assert "P3-42 at ?" in bp.counter_text(c, {})


def test_at_most_five_ids_of_a_kind_are_named_and_the_rest_counted():
    ids = [f"P3-{n:02d}" for n in range(10, 18)]
    c = _counter(baseline_commit="a" * 40, now_open=ids, added=ids, lifted=ids, removed=ids,
                 regressed=ids, moved_in=ids, newly_blocking=ids, no_longer_blocking=ids,
                 moved_out=ids)
    text = bp.counter_text(c, {i: "abc12345" for i in ids})
    assert text.count(" at abc12345") == 5 and "and 3 more" in text, text
    assert "\n" not in text
    assert text.count("and 3 more") == 8, text


def test_a_counter_that_does_not_account_for_the_difference_says_so_not_a_line():
    text = bp.counter_text(_counter(reconciled=False))
    assert text.startswith("(unreconciled:"), text


# --------------------------------------------------------------------------- #
# over a history
# --------------------------------------------------------------------------- #

def test_the_baseline_is_the_matrix_as_of_its_newest_commit(history):
    baseline, why = bp.counter_baseline()
    assert why == "" and baseline["commit"] == history.m_full
    assert set(baseline["rows"]) == {"P3-01", "P3-02", "P3-03", "P3-09", "P4-01"}


def test_the_counter_over_the_history(history):
    plan = {"phase_gates": {"P3": {"blocking_open": ["P3-02", "P3-03", "P3-04"]},
                            "P4": {"blocking_open": ["P4-01"]},
                            "P2": {"blocking_open": []}}}
    texts = bp.annotate_counters(plan, history.live)
    short = history.m_full[:12]
    assert texts["P3"] == (f"(2 at {short}; +1 added: P3-04 at {history.r2}; "
                           "-1 lifted: P3-01; 2 regressed: P3-02, P3-03 "
                           "(1 already open, at a lower status))"), texts
    assert texts["P4"] == f"(1 at {short}; +0 added; -0 lifted; 0 regressed)"
    assert "P2" not in texts and "counter" not in plan["phase_gates"]["P2"]
    counter = plan["phase_gates"]["P3"]["counter"]
    assert counter["added"] == ["P3-04"] and counter["introduced_at"] == {"P3-04": history.r2}
    assert counter["baseline_commit"] == history.m_full
    json.dumps(plan)


def _register_with(history, *extra_ids, message):
    reqs = list(history.register) + [_req(i, "P3") for i in extra_ids]
    _write(history.root, REGISTER, _json({"requirements": reqs}))
    _git(history.root, "add", "-A")
    _git(history.root, "commit", "-qm", message)
    return _git(history.root, "rev-parse", "--short", "HEAD")


def test_the_commit_of_an_added_requirement_is_the_first_after_the_baseline(history):
    # P3-05: in, out, in again after the baseline; P3-06: in, out, in again -- the
    # first commit that puts it in after the baseline dates it, not the last
    first = _register_with(history, "P3-05", message="P3-05 in")
    _register_with(history, message="P3-05 out")
    _register_with(history, "P3-05", message="P3-05 in again")
    renamed = [dict(r) for r in history.register]
    renamed[0]["name"] = "renamed"
    history.register[:] = renamed
    _register_with(history, "P3-05", message="an edit of another row")
    got = bp.introduced_at(["P3-05", "P3-01", "NOPE-1"], since=history.m_full)
    assert got["P3-05"] == first
    assert got["P3-01"] is None, "P3-01 was already in the register before the baseline"
    assert got["NOPE-1"] is None
    assert bp.introduced_at(["P3-01"])["P3-01"] == history.r1, "without a baseline: from the start"


def test_a_row_removed_before_the_baseline_and_registered_again_is_dated_after_it(
        tmp_path, monkeypatch):
    h = History(tmp_path / "repo", monkeypatch)
    out = [dict(r) for r in h.register if r["id"] != "P3-02"]
    _write(h.root, REGISTER, _json({"requirements": out}))
    _git(h.root, "add", "-A")
    _git(h.root, "commit", "-qm", "P3-02 out")
    _write(h.root, MATRIX, _json({"capabilities": [r for r in h.baseline_rows
                                                   if r["id"] != "P3-02"]}))
    _git(h.root, "add", "-A")
    _git(h.root, "commit", "-qm", "matrix regenerated without P3-02")
    baseline, _ = bp.counter_baseline()
    back = _register_with(h, message="P3-02 back")
    assert bp.introduced_at(["P3-02"], since=baseline["commit"])["P3-02"] == back


def test_an_unchanged_programme_reads_zero_zero_zero(history):
    plan = {"phase_gates": {"P4": {"blocking_open": ["P4-01"]}}}
    rows = [dict(r) for r in history.baseline_rows]
    texts = bp.annotate_counters(plan, rows)
    assert texts["P4"].endswith("+0 added; -0 lifted; 0 regressed)"), texts


def test_a_closed_phase_and_a_programme_with_nothing_open_touch_git_not_at_all(
        history, monkeypatch):
    def boom():
        raise AssertionError("git was read")
    monkeypatch.setattr(bp, "counter_baseline", boom)
    assert bp.annotate_counters({"phase_gates": {"P2": {"blocking_open": []}}}, []) == {}


# --------------------------------------------------------------------------- #
# what cannot be said is said, with its reason
# --------------------------------------------------------------------------- #

def test_a_tree_without_its_own_history_says_so(tmp_path, history, monkeypatch):
    inner = history.root / "inner"
    inner.mkdir()
    monkeypatch.setattr(bp, "HOH", inner)
    monkeypatch.setattr(history.su, "HOH", inner)
    baseline, why = bp.counter_baseline()
    assert baseline is None and why.startswith("ENVIRONMENT_GAP: not a git repository"), why
    plan = {"phase_gates": {"P3": {"blocking_open": ["P3-02"]}}}
    texts = bp.annotate_counters(plan, history.live)
    assert texts["P3"].startswith("(provenance unavailable: ENVIRONMENT_GAP"), texts
    assert plan["phase_gates"]["P3"]["counter"] == {"unavailable": why}


def test_a_matrix_with_no_commit_or_no_rows_or_unreadable_says_so(tmp_path, monkeypatch):
    root = tmp_path / "bare"
    root.mkdir()
    _git(root, "init", "-q")
    su = _load("succession", "succession_for_counter_bare")
    monkeypatch.setattr(su, "HOH", root)
    monkeypatch.setattr(bp, "HOH", root)
    monkeypatch.setattr(bp, "_succession", lambda: su)
    _write(root, "README.md", "x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    assert bp.counter_baseline() == (None, "the capability matrix has no commit in this history")
    for text, fragment in (("{not json", "cannot be read"), ("[1]", "is not a JSON object"),
                           ('{"capabilities": []}', "holds no rows")):
        _write(root, MATRIX, text)
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "matrix")
        baseline, why = bp.counter_baseline()
        assert baseline is None and fragment in why, (text, why)


def test_the_tool_that_judges_another_tree_gives_no_provenance(history, monkeypatch):
    monkeypatch.setattr(history.su, "HOH", history.root / "elsewhere")
    baseline, why = bp.counter_baseline()
    assert baseline is None and "judges another tree" in why, why


# --------------------------------------------------------------------------- #
# the command line
# --------------------------------------------------------------------------- #

class _Matrix:
    class RegisterMissing(Exception):
        pass

    ORDER = ["MISSING", "PREPARED", "PARTIAL", "IMPLEMENTED_NOT_PROVEN", "PROVEN"]
    rows: list = []

    @classmethod
    def measure(cls):
        return {"capabilities": [dict(r) for r in cls.rows]}


def test_the_command_line_prints_the_provenance_beside_the_number_and_records_it(
        history, monkeypatch, capsys):
    _Matrix.rows = history.live
    monkeypatch.setattr(bp, "_scope", lambda: {
        "phases": [{"phase": p} for p in ("P2", "P3", "P4")], "items": []})
    monkeypatch.setattr(bp, "_matrix", lambda: _Matrix)
    dag = history.root.parent / "dag.json"
    assert bp.main(["--dag", str(dag)]) == 0
    out = capsys.readouterr().out
    short = history.m_full[:12]
    assert (f"gate P3: 3 blocking open (2 at {short}; +1 added: P3-04 at {history.r2}; "
            "-1 lifted: P3-01; 2 regressed: P3-02, P3-03 (1 already open, at a lower status))"
            ) in out, out
    assert f"gate P4: 1 blocking open (1 at {short}; +0 added; -0 lifted; 0 regressed)" in out
    state = json.loads(dag.read_text(encoding="utf-8"))["phase_gates"]["P3"]
    assert state["counter"]["added"] == ["P3-04"] and state["blocking_open"] == [
        "P3-02", "P3-03", "P3-04"]


def test_the_tool_reads_only(history, monkeypatch, capsys):
    _Matrix.rows = history.live
    monkeypatch.setattr(bp, "_scope", lambda: {"phases": [{"phase": "P3"}], "items": []})
    monkeypatch.setattr(bp, "_matrix", lambda: _Matrix)
    before = _git(history.root, "status", "--porcelain")
    files = sorted(p.name for p in history.root.rglob("*") if ".git" not in p.parts)
    assert bp.main([]) == 0
    capsys.readouterr()
    assert _git(history.root, "status", "--porcelain") == before
    assert sorted(p.name for p in history.root.rglob("*") if ".git" not in p.parts) == files


# --- the reading never breaks the number, and is tied to the gate's own ------------

def _plan(blocking_open, phase="P3"):
    return {"phase_gates": {phase: {"blocking_open": list(blocking_open)}}}


def test_a_baseline_row_the_reading_cannot_take_degrades_to_unavailable(history, monkeypatch):
    """`waiver: ["x"]` and `status: ["x"]` used to raise out of `main`; the number is kept."""
    baseline, _ = bp.counter_baseline()
    for bad in ({"waiver": ["x"]}, {"status": ["x"]}):
        rows = {**baseline["rows"], "P3-02": {**baseline["rows"]["P3-02"], **bad}}
        monkeypatch.setattr(bp, "counter_baseline",
                            lambda r=rows: ({"commit": baseline["commit"], "rows": r}, ""))
        plan = _plan(["P3-02", "P3-03", "P3-04"])
        texts = bp.annotate_counters(plan, history.live)
        assert texts["P3"].startswith("(provenance unavailable: the baseline could not be read"), (
            bad, texts)
        assert "unavailable" in plan["phase_gates"]["P3"]["counter"]


def test_a_counter_that_is_not_the_gates_own_number_is_unreconciled(history):
    plan = _plan(["P3-02", "P3-03"])          # the gate says two; the reading finds three
    texts = bp.annotate_counters(plan, history.live)
    assert texts["P3"].startswith("(unreconciled:"), texts
    assert plan["phase_gates"]["P3"]["counter"]["unreconciled_with_the_gate"] is True


def test_a_row_that_was_not_blocking_and_was_satisfied_is_newly_blocking_not_regressed():
    then = [_row("A-1", "PX", "PROVEN", optional_provider=True)]
    c = bp.blocking_counter("PX", [_row("A-1", "PX", "MISSING")], _baseline(then))
    assert c["newly_blocking"] == ["A-1"] and c["regressed"] == [] and _identity(c)


def test_a_row_that_was_not_open_then_is_not_removed_when_it_goes():
    c = bp.blocking_counter("PX", [], _baseline([_row("A-1", "PX", "PROVEN")]))
    assert c["removed"] == [] and c["left"] == [] and c["previous_open"] == []


def test_a_row_that_is_not_blocking_now_is_never_regressed():
    """A lowered status of a row that no longer holds the gate shut changes nothing."""
    c = bp.blocking_counter("PX", [_row("A-1", "PX", "MISSING", optional_provider=True)],
                            _baseline([_row("A-1", "PX", "PARTIAL", optional_provider=True)]))
    assert c["regressed"] == [] and c["regressed_still_open"] == []


def test_at_most_five_added_ids_are_dated(history, monkeypatch):
    calls = []
    real = history.su._git_rc

    def counting(*args, **kw):
        if args and args[0] == "log" and any(str(a).startswith("-S") for a in args):
            calls.append(args)
        return real(*args, **kw)
    monkeypatch.setattr(history.su, "_git_rc", counting)
    ids = [f"P3-{n}" for n in range(50, 58)]
    rows = history.live + [_row(i, "P3", "MISSING") for i in ids]
    plan = _plan(["P3-02", "P3-03", "P3-04", *ids])
    bp.annotate_counters(plan, rows)
    assert len(calls) == 5
    assert len(plan["phase_gates"]["P3"]["counter"]["introduced_at"]) == 5
    assert len(plan["phase_gates"]["P3"]["counter"]["added"]) == 9, "the DAG lists every id"


def test_a_reading_whose_lists_do_not_cover_the_difference_is_not_reconciled(monkeypatch):
    """Defence in depth: if the test the lists are built from ever disagrees with itself
    mid-reading (here: `_satisfied` answers True for the six rows of `then`, and False
    from then on), the counter says it is not reconciled instead of printing lists that
    do not add up."""
    calls = {"n": 0}

    def unstable(row):
        calls["n"] += 1
        return calls["n"] <= 6
    monkeypatch.setattr(bp, "_satisfied", unstable)
    then = [_row(f"A-{n}", "PX", "MISSING") for n in range(1, 7)]
    now = [_row(f"A-{n}", "PX", "MISSING") for n in range(1, 7)]
    c = bp.blocking_counter("PX", now, _baseline(then))
    assert c["entered"] == [f"A-{n}" for n in range(1, 7)]
    assert c["reconciled"] is False
    assert bp.counter_text(c).startswith("(unreconciled:")
