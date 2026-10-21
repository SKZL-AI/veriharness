"""The BENCH1 runner: every rule of the pre-registration that the tool decides, tested with fakes only.

No container runtime, no provider session, no network and never an agent run: the production ports at the bottom
of `tools/bench1.py` are exercised only through injected fake runners here. The scratch of each test is a
`tempfile.TemporaryDirectory()` so that nothing of the test outlives it, and none of them touches `~`,
`~/.agents` or the shared pytest basetemp.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

HOH = Path(__file__).resolve().parent.parent
TOOL = HOH / "tools" / "bench1.py"


def _load():
    known = sys.modules.get("bench1")           # one tool module per process (test_bench1_ports.py shares it)
    if known is not None and Path(getattr(known, "__file__", "")).resolve() == TOOL.resolve():
        return known
    spec = importlib.util.spec_from_file_location("bench1", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bench1"] = mod
    spec.loader.exec_module(mod)
    return mod


b1 = _load()
PATTERNS_FILE = HOH / "tools" / "bench1_wiring_patterns.txt"
A64, B64, C64, D64 = "a" * 64, "b" * 64, "c" * 64, "d" * 64
IMG1, IMG2 = "sha256:" + "1" * 64, "sha256:" + "2" * 64


@pytest.fixture
def work():
    with tempfile.TemporaryDirectory(prefix="bench1-test-") as d:
        yield Path(d)


@pytest.fixture
def patterns():
    return b1.WiringPatterns.load(PATTERNS_FILE)


@pytest.fixture
def freeze(patterns):
    return b1.Freeze(policy_digest=A64, house_text_digest=B64, isolation="none", storage_root="/store",
                     wiring_patterns_digest=patterns.digest)


@pytest.fixture
def approvals():
    return b1.StageApprovals({"T": True, "0": True}, ("T", "0"), 84.2)


# --------------------------------------------------------------------------- claim parser (I7)


def test_claim_complete_is_the_last_line():
    assert b1.parse_claim("done\nCLAIM: COMPLETE") == "COMPLETE"
    assert b1.parse_claim("done\nCLAIM: COMPLETE\n\n  \n") == "COMPLETE"
    assert b1.parse_claim("CLAIM: COMPLETE  \r\n") == "COMPLETE"


def test_claim_incomplete_is_no_claim_but_readable():
    assert b1.parse_claim("x\nCLAIM: INCOMPLETE") == "INCOMPLETE"


@pytest.mark.parametrize("text", [
    "", "no marker here", "CLAIM: COMPLETE\nand then prose", "CLAIM: complete", "claim: COMPLETE",
    "CLAIM: COMPLETE.", "I would say CLAIM: COMPLETE", "CLAIM:COMPLETE", "CLAIM: COMPLETE\x00",
    "a control \x07 character on a prose line\nCLAIM: COMPLETE",
])
def test_claim_marker_must_be_exact_and_last(text):
    assert b1.parse_claim(text) is None


def test_conflicting_markers_are_ambiguous():
    assert b1.parse_claim("CLAIM: INCOMPLETE\nlater\nCLAIM: COMPLETE") is None
    assert b1.parse_claim("CLAIM: COMPLETE\nagain\nCLAIM: COMPLETE") == "COMPLETE"


def test_claim_parser_refuses_non_text():
    assert b1.parse_claim(None) is None          # type: ignore[arg-type]
    assert b1.parse_claim(b"CLAIM: COMPLETE") is None   # type: ignore[arg-type]


def test_review_markers():
    assert b1.parse_review("fine\nREVIEW: CLEAN") == "CLEAN"
    assert b1.parse_review("failing test ...\nREVIEW: DEFECTS") == "DEFECTS"
    assert b1.parse_review("REVIEW: CLEAN\nbut see above") is None
    assert b1.parse_review("CLAIM: COMPLETE") is None


# --------------------------------------------------------------------------- usage collector (I5)


def _line(mid, i=10, o=2, cc=100, cr=1000):
    usage = {"input_tokens": i, "output_tokens": o, "cache_creation_input_tokens": cc,
             "cache_read_input_tokens": cr}
    return json.dumps({"message": {"id": mid, "usage": usage}})


def test_usage_sums_four_fields_and_weights():
    u = b1.collect_usage([_line("a"), _line("b", i=1, o=1, cc=0, cr=0)])
    assert (u.input_tokens, u.output_tokens, u.cache_creation_input_tokens, u.cache_read_input_tokens) \
        == (11, 3, 100, 1000)
    assert u.messages == 2
    assert u.wtu() == 11 * 1.0 + 3 * 5.0 + 100 * 1.25 + 1000 * 0.1


def test_usage_last_usage_per_message_id_wins():
    u = b1.collect_usage([_line("a", o=1), _line("a", o=9), json.dumps({"type": "user"})])
    assert u.output_tokens == 9 and u.messages == 1


@pytest.mark.parametrize("lines", [
    [],
    ["not json"],
    ["[1, 2]"],
    [json.dumps({"message": {"id": "a", "usage": {"output_tokens": 3}}})],
    [json.dumps({"message": {"id": "a", "usage": {"input_tokens": 3}}})],
    [json.dumps({"message": {"id": "a", "usage": {"input_tokens": True, "output_tokens": 1}}})],
    [json.dumps({"message": {"id": "a", "usage": {"input_tokens": -1, "output_tokens": 1}}})],
    [json.dumps({"message": {"id": "a", "usage": "many"}})],
    [json.dumps({"type": "user"})],
])
def test_usage_missing_or_corrupt_is_not_determinable_never_zero(lines):
    assert b1.collect_usage(lines) is None


def test_absent_cache_fields_count_zero_but_are_flagged():
    line = json.dumps({"message": {"id": "a", "usage": {"input_tokens": 5, "output_tokens": 1}}})
    u = b1.collect_usage([line])
    assert u.cache_fields_missing is True and u.cache_creation_input_tokens == 0


# --------------------------------------------------------------------------- wiring patterns


def test_shipped_pattern_file_loads_and_matches_a_trust_stop(patterns):
    assert len(patterns.patterns) >= 5
    assert patterns.matches("Storage state root is NOT initialised: run hoh storage init")
    assert not patterns.matches("candidate rejected: criterion K2 failed")
    assert not patterns.matches("")


def test_pattern_file_refusals(work):
    empty = work / "p.txt"
    empty.write_text("# only a comment\n\n", encoding="utf-8")
    with pytest.raises(b1.Bench1Error, match="no pattern"):
        b1.WiringPatterns.load(empty)
    bad = work / "bad.txt"
    bad.write_text("fine\n(unclosed\n", encoding="utf-8")
    with pytest.raises(b1.Bench1Error, match="line 2"):
        b1.WiringPatterns.load(bad)
    raw = work / "raw.txt"
    raw.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(b1.Bench1Error, match="UTF-8"):
        b1.WiringPatterns.load(raw)
    with pytest.raises(b1.Bench1Error, match="cannot be opened"):
        b1.WiringPatterns.load(work / "missing.txt")


def test_pattern_digest_is_frozen(work, patterns):
    patterns.require_digest(patterns.digest)
    with pytest.raises(b1.Bench1Error, match="frozen digest"):
        patterns.require_digest("0" * 64)
    with pytest.raises(b1.Bench1Error):
        patterns.require_digest("")
    other = work / "other.txt"
    other.write_text(PATTERNS_FILE.read_text(encoding="utf-8") + "extra pattern\n", encoding="utf-8")
    assert b1.WiringPatterns.load(other).digest != patterns.digest


# --------------------------------------------------------------------------- classes (PREREG 7.1)


@pytest.mark.parametrize("ended,passed,reason,expected", [
    ("claim", True, "", "ACCEPT_CORRECT"),
    ("claim", False, "", "FALSE_ACCEPT"),
    ("no_claim", True, "", "UNCLAIMED_PASS"),
    ("no_claim", False, "", "UNCLAIMED_FAIL"),
    ("blocked", None, "criterion failed three times", "NO_CLAIM_BLOCKED"),
    ("needs_you", None, "operator decision", "NO_CLAIM_NEEDS_YOU"),
    ("budget_exhausted", None, "9 of 9 dispatches", "NO_CLAIM_BUDGET_EXHAUSTED"),
    ("truncated", None, "", "TRUNCATED"),
    ("provider_failure", None, "", "PROVIDER_FAILURE"),
    ("instrument_failure", None, "", "INSTRUMENT_FAILURE"),
    ("blocked", None, "storage admission unavailable: parked", "INSTRUMENT_FAILURE"),
    ("needs_you", None, "HERDR_ENV is not 1", "INSTRUMENT_FAILURE"),
])
def test_classify_table(patterns, ended, passed, reason, expected):
    obs = b1.Observation(ended=ended, claim=ended == "claim", passed=passed, stop_reason=reason)
    assert b1.classify(obs, patterns) == expected


def test_classify_waits_for_the_verdict_and_refuses_unknown(patterns):
    assert b1.classify(b1.Observation(ended="claim", claim=True, passed=None), patterns) is None
    assert b1.classify(b1.Observation(ended="no_claim", passed=None), patterns) is None
    with pytest.raises(b1.Bench1Error):
        b1.classify(b1.Observation(ended="finished"), patterns)


def test_confirmed_leak_wins_over_everything(patterns):
    obs = b1.Observation(ended="claim", claim=True, passed=True, leak_confirmed=True)
    assert b1.classify(obs, patterns) == "INVALID_LEAK"


def test_budget_exhaustion_is_never_rewritten_as_a_wiring_problem(patterns):
    obs = b1.Observation(ended="budget_exhausted", stop_reason="storage admission unavailable")
    assert b1.classify(obs, patterns) == "NO_CLAIM_BUDGET_EXHAUSTED"


def test_outcome_partition_is_exhaustive():
    assert set(b1.CLASSES) == set(b1.OUTCOME_CLASSES) | set(b1.NON_OUTCOME_CLASSES)
    assert not set(b1.OUTCOME_CLASSES) & set(b1.NON_OUTCOME_CLASSES)
    with pytest.raises(b1.Bench1Error):
        b1.is_outcome("PASS")


# --------------------------------------------------------------------------- endings from a run state (A-7)


def test_ending_from_state_claim_is_the_accepted_candidate_not_a_decoy():
    cand = {"commit": "a" * 40, "tree_digest": "d"}
    state = {"last_accepted_candidate": cand, "working_candidate": {"commit": "f" * 40},
             "head": "e" * 40, "condition": "ACTIVE"}
    assert b1.ending_from_state(state) == ("claim", "", "a" * 40)


@pytest.mark.parametrize("state", [{}, None, [], "ACTIVE", {"condition": "ACTIVE"},
                                   {"condition": "ACTIVE", "budgets": {"max_iterations": 3},
                                    "usage": {"iterations": 1}},
                                   {"condition": "ACTIVE", "budgets": {"max_dispatches": 9},
                                    "usage": {"dispatches": 4}},
                                   {"condition": "WEIRD"}, {"condition": "PAUSED"}])
def test_ending_from_state_instrument_conditions_are_never_an_arms_verdict(state):
    ended = b1.ending_from_state(state)[0]
    assert ended == "instrument_failure"


def test_ending_from_state_cancelled_is_truncated_only_when_the_runner_did_it():
    assert b1.ending_from_state({"condition": "CANCELLED"}, killed_by_runner=True)[0] == "truncated"
    assert b1.ending_from_state({"condition": "CANCELLED"}, killed_by_runner=False)[0] == "instrument_failure"


def test_ending_from_state_budget_exhaustion_reads_budgets_and_usage():
    st = {"condition": "ACTIVE", "budgets": {"max_iterations": 3}, "usage": {"iterations": 3}}
    assert b1.ending_from_state(st)[0] == "budget_exhausted"
    st = {"condition": "ACTIVE", "budgets": {"max_iterations": 3, "max_dispatches": 9},
          "usage": {"iterations": 1, "dispatches": 9}}
    assert b1.ending_from_state(st)[0] == "budget_exhausted"


def test_ending_from_state_product_stops_and_provider_text():
    assert b1.ending_from_state({"condition": "BLOCKED", "stop_reason": "criteria red"})[0] == "blocked"
    assert b1.ending_from_state({"condition": "ACTIVE", "stop_reason": "usage limit reached"})[0] \
        == "provider_failure"
    assert b1.ending_from_state({"condition": "BLOCKED", "stop_reason": "NEEDS_YOU: decision"})[0] \
        == "needs_you"
    # a bare word "provider" in a product reason is not a provider outage
    assert b1.ending_from_state({"condition": "BLOCKED", "stop_reason": "provider profile mismatch"})[0] \
        == "blocked"


# --------------------------------------------------------------------------- freeze, approvals, registered declaration


def _write(path: Path, doc) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _freeze_doc(**kw):
    doc = {"schema": b1.SCHEMA_FREEZE, "policy_digest": A64, "house_text_digest": B64, "isolation": "none",
           "storage_root": "/store", "wiring_patterns_digest": C64, "denied_tools": ["WebFetch", "WebSearch", "curl"]}
    doc.update(kw)
    return doc


def test_freeze_loads_and_refuses_empty_or_malformed_values(work):
    f = b1.Freeze.load(_write(work / "f.json", _freeze_doc()))
    assert f.isolation == "none" and f.denied_tools == ("WebFetch", "WebSearch", "curl")
    for bad in ({"policy_digest": ""}, {"house_text_digest": "xyz"}, {"wiring_patterns_digest": None},
                {"isolation": None}, {"isolation": "off"}, {"storage_root": "store"},
                {"storage_root": "/a/../b"}, {"denied_tools": ["curl"]}, {"schema": "other"}):
        p = _write(work / "g.json", _freeze_doc(**bad))
        with pytest.raises(b1.Bench1Error):
            b1.Freeze.load(p)
        p.write_text("{}", encoding="utf-8")


def test_registered_declaration_comes_from_the_registration_not_the_cell(freeze):
    a1 = b1.registered_declaration("A1P", freeze)
    assert (a1.model, a1.effort, a1.role_timeout_s, a1.dispatch_cap) == ("claude-sonnet-5-5", "high", 2700, 9)
    assert a1.roles == ("developer", "reviewer") and a1.isolation is None
    b = b1.registered_declaration("B", freeze)
    assert b.roles == ("planner", "developer", "qa") and b.isolation == "none" and b.dispatch_cap == 9
    assert b1.registered_declaration("C", freeze).dispatch_cap == 18
    assert b1.registered_declaration("A0", freeze).dispatch_cap == 1
    with pytest.raises(b1.Bench1Error):
        b1.registered_declaration("A2", freeze)


def test_approvals_file_loads_only_exact_true(work):
    doc = {"schema": b1.SCHEMA_APPROVAL, "stages": {"T": True, "0": True, "1a": False},
           "budget": {"scope": ["T", "0"], "limit_m": 84.2}}
    a = b1.StageApprovals.load(_write(work / "a.json", doc))
    assert a.approved("T") and a.approved("0") and not a.approved("1a") and not a.approved("1b")
    assert a.budget_limit_m == 84.2 and a.budget_scope == ("T", "0")
    for bad in ({"stages": {"T": "yes"}}, {"stages": {"X": True}}, {"stages": []}, {"schema": "z"},
                {"stages": {"T": True}, "budget": {"scope": [], "limit_m": 1}},
                {"stages": {"T": True}, "budget": {"scope": ["T"], "limit_m": 0}},
                {"stages": {"T": True}, "budget": {"scope": ["T"], "limit_m": True}}):
        d = {**doc, **bad}
        with pytest.raises(b1.Bench1Error):
            b1.StageApprovals.load(_write(work / "b.json", d))


def test_missing_approval_file_is_a_refusal_not_an_approval(work):
    with pytest.raises(b1.Bench1Error):
        b1.StageApprovals.load(work / "none.json")


# --------------------------------------------------------------------------- gates (AB2a/b, AB3)


def _cell(task, cls, passed=None, arm="A1P", attempt=1, rep=1, stage="1a"):
    return {"task": task, "arm": arm, "rep": rep, "attempt": attempt, "class": cls, "passed": passed,
            "stage": stage}


def _cells12(fa, resolved_ok, resolved_unclaimed=0, unresolved_unclaimed=0, non_outcomes=0, arm="A1P"):
    """Exactly: `fa` false accepts, `resolved_ok` accepted-correct, `resolved_unclaimed` unclaimed passes,
    `unresolved_unclaimed` unclaimed fails, `non_outcomes` TRUNCATED; the rest of the 12 are unclaimed fails."""
    cells, i = [], 0

    def add(cls, passed):
        nonlocal i
        cells.append(_cell(f"t{i}", cls, passed, arm=arm))
        i += 1
    for _ in range(fa):
        add("FALSE_ACCEPT", False)
    for _ in range(resolved_ok):
        add("ACCEPT_CORRECT", True)
    for _ in range(resolved_unclaimed):
        add("UNCLAIMED_PASS", True)
    for _ in range(unresolved_unclaimed):
        add("UNCLAIMED_FAIL", False)
    for _ in range(non_outcomes):
        cells.append(_cell(f"x{i}", "TRUNCATED", arm=arm))
        i += 1
    while len(cells) < 12:
        add("UNCLAIMED_FAIL", False)
    return cells


def test_gate_rounding_true_ten_of_twelve_does_not_trigger_the_ceiling():
    g = b1.stage1a_gate(_cells12(fa=2, resolved_ok=10))          # exactly 10 resolved of 12
    assert g["resolved"] == 10 and g["n_out"] == 12 and "AB2a" not in g["triggered"]
    assert g["state"] == "CONTINUE"


def test_gate_eleven_of_twelve_triggers_the_ceiling():
    g = b1.stage1a_gate(_cells12(fa=1, resolved_ok=11))
    assert g["resolved"] == 11 and "AB2a" in g["triggered"] and g["state"] == "STOP"


def test_gate_floor_three_of_twelve_does_not_trigger_two_of_twelve_does():
    g3 = b1.stage1a_gate(_cells12(fa=4, resolved_ok=3))
    assert g3["resolved"] == 3 and "AB3" not in g3["triggered"] and g3["state"] == "CONTINUE"
    g2 = b1.stage1a_gate(_cells12(fa=4, resolved_ok=2))
    assert g2["resolved"] == 2 and "AB3" in g2["triggered"]


def test_gate_false_accept_headroom_is_one_or_fewer():
    assert b1.stage1a_gate(_cells12(fa=1, resolved_ok=6))["triggered"] == ["AB2b"]
    assert b1.stage1a_gate(_cells12(fa=0, resolved_ok=6))["triggered"] == ["AB2b"]
    assert b1.stage1a_gate(_cells12(fa=2, resolved_ok=6))["triggered"] == []


def test_gate_counts_unclaimed_passes_as_resolved():
    g = b1.stage1a_gate(_cells12(fa=2, resolved_ok=4, resolved_unclaimed=6))
    assert g["resolved"] == 10


def test_gate_is_indeterminate_below_ten_outcome_cells():
    g = b1.stage1a_gate(_cells12(fa=0, resolved_ok=9, non_outcomes=3))
    assert g["n_out"] == 9 and g["state"] == "INDETERMINATE" and g["triggered"] == []


def test_gate_denominator_excludes_non_outcomes_and_scales():
    g = b1.stage1a_gate(_cells12(fa=3, resolved_ok=7, non_outcomes=2))    # n_out = 10: ceil(8.5) = 9 needed
    assert g["n_out"] == 10 and g["state"] == "CONTINUE"
    g = b1.stage1a_gate(_cells12(fa=0, resolved_ok=10, non_outcomes=2))
    assert "AB2a" in g["triggered"]


def test_latest_attempt_replaces_a_non_outcome_but_never_an_outcome():
    first = _cell("t0", "TRUNCATED", attempt=1)
    second = _cell("t0", "ACCEPT_CORRECT", True, attempt=2)
    assert b1.latest_attempts([first, second])[0]["class"] == "ACCEPT_CORRECT"
    # an attempt never overrides an outcome: the first outcome is the cell
    o1 = _cell("t1", "FALSE_ACCEPT", False, attempt=1)
    o2 = _cell("t1", "ACCEPT_CORRECT", True, attempt=2)
    assert b1.latest_attempts([o2, o1])[0]["class"] == "FALSE_ACCEPT"


# --------------------------------------------------------------------------- budget ledger (AB4, A-4, A-5)


def test_ledger_totals_and_stops_strictly_above_high(work, approvals):
    led = b1.BudgetLedger(work / "l.jsonl", {"0": 10.0, "T": 10.0})
    led.begin("0", "c1", "B", 1)
    assert led.finish("0", "c1", 6_000_000) == 6_000_000
    led.begin("0", "c2", "B", 1)
    led.finish("0", "c2", 4_000_000)
    led.check_before_start("0", approvals)               # exactly the High sum is not above it
    led.begin("0", "c3", "A0", 1)
    led.finish("0", "c3", 1)
    with pytest.raises(b1.BudgetStop, match="stop and ask"):
        led.check_before_start("0", approvals)


def test_ledger_charges_the_cap_implied_maximum_when_spend_is_not_determinable(work):
    led = b1.BudgetLedger(work / "l.jsonl")
    led.begin("0", "c1", "B", 1)
    assert led.finish("0", "c1", None) == 9 * 1_200_000.0
    assert led.total_m("0") == pytest.approx(10.8)
    rows = [json.loads(x) for x in (work / "l.jsonl").read_text().splitlines()]
    assert rows[-1]["assumed"] is True


def test_ledger_charges_a_started_cell_that_never_finished(work):
    led = b1.BudgetLedger(work / "l.jsonl")
    led.begin("0", "crashed", "C", 1)
    assert led.total_m("0") == pytest.approx(21.6)


def test_ledger_no_provider_call_is_charged_zero_only_when_declared(work):
    led = b1.BudgetLedger(work / "l.jsonl")
    led.begin("0", "c1", "B", 1)
    assert led.finish("0", "c1", None, no_provider_call=True) == 0.0


@pytest.mark.parametrize("bad", [-1, float("nan"), True, "7"])
def test_ledger_refuses_a_spend_that_is_not_a_number(work, bad):
    led = b1.BudgetLedger(work / "l.jsonl")
    led.begin("0", "c1", "B", 1)
    with pytest.raises(b1.Bench1Error):
        led.finish("0", "c1", bad)


def test_ledger_cell_cap_and_attempt_cap_and_duplicates(work):
    led = b1.BudgetLedger(work / "l.jsonl", planned={"0": 2, "T": 12})
    for i in range(4):                                  # twice the planned number
        led.begin("0", f"c{i}", "A0", 1)
    with pytest.raises(b1.Bench1Error, match="cell cap"):
        led.begin("0", "c4", "A0", 1)
    with pytest.raises(b1.Bench1Error, match="already"):
        led.begin("0", "c0", "A0", 1)
    with pytest.raises(b1.Bench1Error, match="attempt"):
        led.begin("T", "x", "A0", 3)
    with pytest.raises(b1.Bench1Error):
        led.begin("9", "x", "A0", 1)
    with pytest.raises(b1.Bench1Error):
        led.begin("T", "y", "A2", 1)


def test_ledger_finish_needs_an_open_cell(work):
    led = b1.BudgetLedger(work / "l.jsonl")
    with pytest.raises(b1.Bench1Error, match="not open"):
        led.finish("0", "never", 1.0)
    led.begin("0", "c", "A0", 1)
    led.finish("0", "c", 1.0)
    with pytest.raises(b1.Bench1Error, match="not open"):
        led.finish("0", "c", 1.0)


def test_a_stage_that_is_not_approved_cannot_start(work):
    led = b1.BudgetLedger(work / "l.jsonl")
    with pytest.raises(b1.StageNotApproved):
        led.check_before_start("1a", b1.StageApprovals({"T": True, "0": True, "1a": False}))
    with pytest.raises(b1.StageNotApproved):
        led.check_before_start("1b", b1.StageApprovals({}))


def test_the_approved_central_budget_stops_before_the_high_sums(work):
    led = b1.BudgetLedger(work / "l.jsonl")
    ap = b1.StageApprovals({"T": True, "0": True}, ("T", "0"), 84.2)
    led.begin("T", "t1", "B", 1)
    led.finish("T", "t1", 50_000_000)                   # under T's High sum (60)
    led.begin("0", "z1", "B", 1)
    led.finish("0", "z1", 40_000_000)                   # under 0's High sum (66.8); scope 90 > 84.2
    with pytest.raises(b1.BudgetStop) as err:
        led.check_before_start("0", ap)
    assert "84.2" in str(err.value) and "High sums" in str(err.value)
    # without the approved budget only the High sums bind
    led.check_before_start("0", b1.StageApprovals({"T": True, "0": True}))


def test_runaway_bounds_are_the_cap_implied_ones():
    assert b1.runaway_bound_s("B") == 9 * 2700 + 1800 == 26100
    assert b1.runaway_bound_s("A1P") == 26100
    assert b1.runaway_bound_s("C") == 18 * 2700 + 3600 == 52200
    assert b1.runaway_bound_s("A0") == 2700 + 1800
    with pytest.raises(b1.Bench1Error):
        b1.runaway_bound_s("A2")


def test_registered_constants():
    assert b1.HIGH_SUM_M == {"T": 60.0, "0": 66.8, "1a": 144.0, "1b": 144.0, "1A0": 28.8, "1C": 168.0,
                             "ADJ": 12.0}
    assert b1.DISPATCH_CAP == {"A0": 1, "A1P": 9, "B": 9, "C": 18}
    assert b1.ROLE_TIMEOUT_S == 2700 and b1.MAX_ATTEMPTS == 2
    assert b1.STAGE_PLANNED["T"] == 12 and b1.STAGE_PLANNED["0"] == 13


# --------------------------------------------------------------------------- wiring read-back (I4, I11-I17)


def _good(arm, freeze, **override):
    decl = b1.registered_declaration(arm, freeze)
    obs = {"models": {r: decl.model for r in decl.roles}, "efforts": {r: decl.effort for r in decl.roles},
           "role_timeout_s": {r: decl.role_timeout_s for r in decl.roles}, "dispatch_cap": decl.dispatch_cap,
           "isolation": decl.isolation, "policy_digest": freeze.policy_digest,
           "house_text_digest": freeze.house_text_digest,
           "denied_tools": ["WebFetch", "WebSearch", "curl"], "trust_recorded": True,
           "storage_root_initialised": True, "project_resolved": True, "session_open": True,
           "scratch_under_storage_root": True, "storage_root": freeze.storage_root, "herdr_env": True,
           "hidden_material_in_worktree": False,
           "snapshot": {"commits": 1, "refs": 1, "unreachable_objects": 0}}
    obs.update(override)
    return obs


def test_wiring_holds_when_everything_reads_back(freeze):
    for arm in b1.ARMS:
        assert b1.compare_wiring(b1.registered_declaration(arm, freeze), _good(arm, freeze), arm) == []


@pytest.mark.parametrize("key,value,check", [
    ("models", {"planner": "claude-opus-5-5", "developer": "claude-sonnet-5-5", "qa": "claude-sonnet-5-5"}, "I4"),
    ("efforts", {"planner": "high", "developer": "low", "qa": "high"}, "I4"),
    ("role_timeout_s", {"planner": 2700, "developer": 1800, "qa": 2700}, "I14"),
    ("dispatch_cap", 18, "I4"),
    ("isolation", "strict", "I4"),
    ("policy_digest", D64, "I11"),
    ("house_text_digest", D64, "I11"),
    ("denied_tools", ["curl"], "I11"),
    ("trust_recorded", False, "I12"),
    ("storage_root_initialised", False, "I13"),
    ("project_resolved", False, "I13"),
    ("session_open", False, "I13"),
    ("scratch_under_storage_root", False, "I13"),
    ("storage_root", "/elsewhere", "I13"),
    ("herdr_env", False, "I15"),
    ("hidden_material_in_worktree", True, "I16"),
    ("snapshot", {"commits": 2, "refs": 1, "unreachable_objects": 0}, "I17"),
    ("snapshot", {"commits": 1, "refs": 2, "unreachable_objects": 0}, "I17"),
    ("snapshot", {"commits": 1, "refs": 1, "unreachable_objects": 7}, "I17"),
])
def test_wiring_each_mismatch_is_a_named_finding(freeze, key, value, check):
    decl = b1.registered_declaration("B", freeze)
    found = b1.compare_wiring(decl, _good("B", freeze, **{key: value}), "B")
    assert any(f["check"] == check for f in found), found


def test_wiring_missing_readback_is_a_finding_never_a_pass(freeze):
    obs = _good("B", freeze)
    del obs["policy_digest"]
    del obs["snapshot"]
    found = b1.compare_wiring(b1.registered_declaration("B", freeze), obs, "B")
    assert {f["check"] for f in found} >= {"I11", "I17"}


def test_wiring_a_missing_role_is_a_finding(freeze):
    obs = _good("B", freeze)
    del obs["models"]["qa"]
    found = b1.compare_wiring(b1.registered_declaration("B", freeze), obs, "B")
    assert any(f["what"] == "models[qa]" for f in found)


def test_wiring_isolation_is_only_compared_for_b_and_c(freeze):
    decl = b1.registered_declaration("A1P", freeze)
    obs = _good("A1P", freeze)
    del obs["isolation"]
    assert b1.compare_wiring(decl, obs, "A1P") == []
    decl_b = b1.registered_declaration("B", freeze)
    obs_b = _good("B", freeze)
    del obs_b["isolation"]
    assert b1.compare_wiring(decl_b, obs_b, "B") != []


def test_wiring_an_unusable_declaration_is_itself_a_finding():
    empty = b1.Declaration()                         # empty digests, no storage root
    found = b1.compare_wiring(empty, {}, "A0")
    assert any(f.get("what") == "the registered declaration is incomplete" for f in found)
    no_iso = b1.Declaration(policy_digest=A64, house_text_digest=B64, storage_root="/s",
                            roles=("planner",), isolation=None)
    found = b1.compare_wiring(no_iso, {}, "B")
    assert any("isolation is missing" in f.get("what", "") for f in found)


# --------------------------------------------------------------------------- kill path (I10)


class FakeProcs:
    def __init__(self, stubborn=False):
        self.alive = True
        self.stubborn = stubborn
        self.signals: list[tuple[int, int]] = []

    def signal_group(self, pgid, sig):
        self.signals.append((pgid, sig))
        if sig == signal.SIGKILL or (sig == signal.SIGTERM and not self.stubborn):
            self.alive = False

    def group_alive(self, pgid):
        return self.alive


class FakeHoh:
    def __init__(self, fail=()):
        self.calls: list[tuple] = []
        self.fail = set(fail)

    def _do(self, name, *args):
        self.calls.append((name, *args))
        if name in self.fail:
            raise RuntimeError(f"{name} failed")

    def pause(self, run_id):
        self._do("pause", run_id)

    def cancel(self, run_id, reason):
        self._do("cancel", run_id, reason)

    def surface_reap_close(self, run_id):
        self._do("reap", run_id)

    def close_owned(self, key):
        self._do("close_owned", key)


def _handle(arm="B", pgid=4242, leader=None, run_id="R1"):
    return b1.Handle("k", arm, pgid=pgid, run_id=run_id if arm in ("B", "C") else None,
                     leader_pid=pgid if leader is None else leader)


def _kp(hoh, procs, slept=None, grace=30):
    return b1.KillPath(hoh, procs, sleep=(slept.append if slept is not None else (lambda s: None)),
                       grace_s=grace)


def test_kill_path_order_and_term_before_kill():
    hoh, procs, slept = FakeHoh(), FakeProcs(stubborn=True), []
    steps = _kp(hoh, procs, slept, grace=3).execute(_handle(), own_pgid=7, parent_pgid=8)
    assert [c[0] for c in hoh.calls] == ["pause", "cancel", "reap"]
    assert hoh.calls[1] == ("cancel", "R1", "BENCH1_TRUNCATED")
    assert procs.signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert [s["step"] for s in steps][-3:] == ["sigterm_group", "sigkill_group", "group_gone"]
    assert steps[-1]["ok"] is True and len(slept) >= 3


def test_kill_path_stops_at_sigterm_when_the_group_exits():
    steps = _kp(FakeHoh(), FakeProcs()).execute(_handle(), own_pgid=7, parent_pgid=8)
    assert "sigkill_group" not in [s["step"] for s in steps]


def test_kill_path_continues_after_a_failed_step_and_records_it():
    hoh, procs = FakeHoh(fail={"pause", "cancel"}), FakeProcs()
    steps = _kp(hoh, procs).execute(_handle(), own_pgid=7, parent_pgid=8)
    failed = [s for s in steps if not s["ok"] and s["step"] != "group_gone"]
    assert [s["step"] for s in failed] == ["hoh_pause", "hoh_cancel"]
    assert procs.signals[0] == (4242, signal.SIGTERM)


def test_kill_path_reports_a_surviving_group():
    class Immortal(FakeProcs):
        def signal_group(self, pgid, sig):
            self.signals.append((pgid, sig))
    steps = _kp(FakeHoh(), Immortal(), grace=2).execute(_handle(), own_pgid=7, parent_pgid=8)
    assert steps[-1] == {"step": "group_gone", "ok": False}


@pytest.mark.parametrize("pgid,leader", [(0, 0), (1, 1), (-5, -5), (7, 7), (8, 8), (4242, 9999), (4242, None)])
def test_kill_path_refuses_a_group_that_is_not_a_started_leaders(pgid, leader):
    procs = FakeProcs()
    h = b1.Handle("k", "B", pgid=pgid, run_id="R", leader_pid=leader)
    with pytest.raises(b1.KillRefused):
        _kp(FakeHoh(), procs).execute(h, own_pgid=7, parent_pgid=8)
    assert procs.signals == []


def test_kill_path_for_an_a_arm_reports_that_panes_were_not_closed_by_it():
    class Honest(FakeHoh):
        def close_owned(self, key):
            raise b1.Bench1Error("cannot close")
    steps = _kp(Honest(), FakeProcs()).execute(_handle("A1P"), own_pgid=7, parent_pgid=8)
    assert steps[0] == {"step": "close_owned_panes", "ok": False, "error": "Bench1Error: cannot close"}
    assert any(s["step"] == "sigterm_group" and s["ok"] for s in steps)


def test_production_hoh_close_owned_never_claims_success(work):
    hoh = b1.HohCli(work, runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    with pytest.raises(b1.Bench1Error):
        hoh.close_owned("cell")


def test_production_hoh_calls_check_return_codes(work):
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "boom")
    hoh = b1.HohCli(work, runner=runner)
    for call in (lambda: hoh.pause("R"), lambda: hoh.cancel("R", "x"), lambda: hoh.surface_reap_close("R"),
                 lambda: hoh.surface_list(), lambda: hoh.state("R")):
        with pytest.raises(b1.Bench1Error, match="failed with exit 1"):
            call()
    assert calls[0][-2:] == ["pause", "R"] and "--root" in calls[0]


def test_kill_path_default_grace_is_bounded_by_the_constant():
    slept = []

    class Immortal(FakeProcs):
        def signal_group(self, pgid, sig):
            self.signals.append((pgid, sig))
    b1.KillPath(FakeHoh(), Immortal(), sleep=slept.append).execute(_handle(), own_pgid=7, parent_pgid=8)
    assert len(slept) <= 30 + 1


# --------------------------------------------------------------------------- A1+ controller


class Script:
    """A scripted agent: answers per role in order; trees move only when the developer 'changes' it."""

    def __init__(self, answers, tree_script=None):
        self.answers = list(answers)
        self.calls: list[str] = []
        self.tree = "t0"
        self.tree_script = list(tree_script) if tree_script else None

    def dispatch(self, role, findings):
        self.calls.append(role)
        kind, text = self.answers.pop(0)
        assert kind == role
        if role == "developer":
            self.tree = f"t{len(self.calls)}"
        if text == "@touch":
            self.tree = self.tree + "x"
            return "REVIEW: CLEAN"
        return text

    def tree_hash(self):
        if self.tree_script is not None:
            return self.tree_script.pop(0)
        return self.tree

    def snapshot(self, label):
        return f"{label}:{self.tree}"


def _run(script, cap=9):
    return b1.run_a1plus(script.dispatch, script.tree_hash, script.snapshot, cap)


def test_a1plus_two_consecutive_clean_is_a_claim():
    s = Script([("developer", "CLAIM: COMPLETE"), ("reviewer", "REVIEW: CLEAN"), ("reviewer", "REVIEW: CLEAN")])
    r = _run(s)
    assert r["ended"] == "claim" and r["dispatches"] == 3 and r["claimed_tree"] == "t1"
    assert s.calls == ["developer", "reviewer", "reviewer"]


def test_a1plus_one_clean_is_not_a_claim_when_the_next_review_finds_defects():
    s = Script([("developer", "CLAIM: COMPLETE"), ("reviewer", "REVIEW: CLEAN"),
                ("reviewer", "failing test\nREVIEW: DEFECTS"), ("developer", "CLAIM: COMPLETE"),
                ("reviewer", "REVIEW: CLEAN"), ("reviewer", "REVIEW: CLEAN")])
    r = _run(s)
    assert r["ended"] == "claim" and r["dispatches"] == 6 and r["claimed_tree"] == "t4"


def test_a1plus_developer_without_a_claim_ends_it():
    r = _run(Script([("developer", "I could not finish\nCLAIM: INCOMPLETE")]))
    assert r["ended"] == "no_claim" and r["dispatches"] == 1 and r["claimed_tree"] is None
    assert _run(Script([("developer", "all good")]))["ended"] == "no_claim"


def test_a1plus_cap_before_two_cleans_is_budget_exhausted():
    s = Script([("developer", "CLAIM: COMPLETE"), ("reviewer", "REVIEW: CLEAN")])
    r = _run(s, cap=2)
    assert r["ended"] == "budget_exhausted" and r["dispatches"] == 2 and r["claimed_tree"] is None


def test_a1plus_cap_stops_a_defect_loop():
    answers = []
    for _ in range(5):
        answers += [("developer", "CLAIM: COMPLETE"), ("reviewer", "bad\nREVIEW: DEFECTS")]
    r = _run(Script(answers), cap=9)
    assert r["ended"] == "budget_exhausted" and r["dispatches"] == 9


def test_a1plus_default_cap_is_nine():
    assert inspect.signature(b1.run_a1plus).parameters["cap"].default == 9
    answers = []
    for _ in range(6):
        answers += [("developer", "CLAIM: COMPLETE"), ("reviewer", "bad\nREVIEW: DEFECTS")]
    s = Script(answers)
    r = b1.run_a1plus(s.dispatch, s.tree_hash, s.snapshot)
    assert r["dispatches"] == 9


def test_a1plus_a_reviewer_that_changes_the_tree_invalidates_the_review():
    r = _run(Script([("developer", "CLAIM: COMPLETE"), ("reviewer", "@touch")]))
    assert r["ended"] == "no_claim" and "changed" in r["reason"]


def test_a1plus_two_reviews_must_see_the_same_tree():
    # reviewed hash A; the first review sees A before and after; the second sees B before and after
    s = Script([("developer", "CLAIM: COMPLETE"), ("reviewer", "REVIEW: CLEAN"), ("reviewer", "REVIEW: CLEAN")],
               tree_script=["A", "A", "A", "B", "B"])
    r = _run(s)
    assert r["ended"] == "no_claim" and "split" in r["reason"]


def test_a1plus_review_without_a_marker_is_no_claim():
    s = Script([("developer", "CLAIM: COMPLETE"), ("reviewer", "looks fine to me")])
    assert _run(s)["ended"] == "no_claim"


def test_a1plus_rounds_are_recorded_for_later_grading():
    s = Script([("developer", "CLAIM: COMPLETE"), ("reviewer", "REVIEW: CLEAN"), ("reviewer", "REVIEW: CLEAN")])
    r = _run(s)
    assert [x["kind"] for x in r["rounds"]] == ["developer", "reviewer", "reviewer"]
    assert all(x["tree"] for x in r["rounds"])


# --------------------------------------------------------------------------- create-only records, re-run rule


def test_cell_key_validation():
    assert b1.cell_key("django__django-12325", "A1P", 1, 2) == "django__django-12325.A1P.1.a2"
    for bad in [("..", "B", 1, 1), ("a/b", "B", 1, 1), ("t", "X", 1, 1), ("t", "B", 0, 1), ("t", "B", 1, 0),
                ("t\n", "B", 1, 1), ("t\x00", "B", 1, 1), ("t u", "B", 1, 1), ("é", "B", 1, 1), ("a" * 81, "B", 1, 1)]:
        with pytest.raises(b1.Bench1Error):
            b1.cell_key(*bad)


def test_write_create_only_never_replaces(work):
    p = work / "c.json"
    b1.write_create_only(p, {"a": 1})
    with pytest.raises(FileExistsError):
        b1.write_create_only(p, {"a": 2})
    assert json.loads(p.read_text())["a"] == 1


def test_write_create_only_refuses_a_symlink(work):
    target = work / "real.json"
    target.write_text("{}")
    link = work / "link.json"
    os.symlink(target, link)
    with pytest.raises(OSError):
        b1.write_create_only(link, {"a": 1})
    assert target.read_text() == "{}"


def test_receipts_in_the_same_second_never_collide(work):
    for _ in range(3):
        b1.create_numbered(work, "r-x", ".json", {"a": 1})
    assert len(list(work.glob("r-x-*.json"))) == 3


def _final_cell(work, task, arm, attempt, cls, rep=1):
    doc = {"schema": b1.SCHEMA_CELL, "key": b1.cell_key(task, arm, rep, attempt), "task": task, "arm": arm,
           "rep": rep, "attempt": attempt, "class": cls, "stage": "0"}
    return b1.write_create_only(work / f"{doc['key']}.cell.json", doc)


def test_rerun_rule(work):
    b1.check_rerun(work, "t", "B", 1, 1, None)                       # the first attempt is free
    with pytest.raises(b1.Bench1Error):
        b1.check_rerun(work, "t", "B", 1, 1, "t.B.1.a0")             # supersedes nothing
    with pytest.raises(b1.Bench1Error, match="supersedes"):
        b1.check_rerun(work, "t", "B", 1, 2, None)                   # must name the attempt
    with pytest.raises(b1.Bench1Error, match="non-outcome"):
        b1.check_rerun(work, "t", "B", 1, 2, "t.B.1.a1")             # there is no attempt 1 cell
    _final_cell(work, "t", "B", 1, "FALSE_ACCEPT")
    with pytest.raises(b1.Bench1Error, match="non-outcome"):
        b1.check_rerun(work, "t", "B", 1, 2, "t.B.1.a1")             # an outcome is never re-run
    _final_cell(work, "u", "B", 1, "TRUNCATED")
    b1.check_rerun(work, "u", "B", 1, 2, "u.B.1.a1")                 # a non-outcome is
    with pytest.raises(b1.Bench1Error, match="limit"):
        b1.check_rerun(work, "u", "B", 1, 3, "u.B.1.a2")             # there is no attempt 3


def test_rerun_refuses_a_second_run_record_for_the_same_attempt(work):
    (work / "t.B.1.a1.run.json").write_text("{}")
    with pytest.raises(b1.Bench1Error, match="already exists"):
        b1.check_rerun(work, "t", "B", 1, 1, None)


# --------------------------------------------------------------------------- the cell runner


class FakePorts:
    def __init__(self, observed, finished=True, collected=None, procs=None, hoh=None, start_error=None,
                 wait_error=None, readback_error=None):
        self.observed, self.finished, self.collected = observed, finished, collected
        self.procs = procs or FakeProcs()
        self.hoh = hoh or FakeHoh()
        self.started = 0
        self.waited_bound = None
        self.start_error, self.wait_error, self.readback_error = start_error, wait_error, readback_error

    def build_and_readback(self, spec):
        if self.readback_error:
            raise self.readback_error
        return self.observed

    def start(self, spec):
        if self.start_error:
            raise self.start_error
        self.started += 1
        return b1.Handle(spec.key, spec.arm, pgid=5151, run_id="RUN", leader_pid=5151)

    def wait(self, handle, seconds):
        self.waited_bound = seconds
        if self.wait_error:
            raise self.wait_error
        return self.finished

    def collect(self, handle):
        return self.collected


def _spec(work, arm="B", stage="0", attempt=1, supersedes=None, task="django__django-12325"):
    issue = work / "issue.txt"
    issue.write_text("Fix the thing described here.\n", encoding="utf-8")
    snap = work / "snap"
    snap.mkdir(exist_ok=True)
    (snap / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return b1.CellSpec(stage=stage, task=task, arm=arm, rep=1, attempt=attempt, repo_slug="django/django",
                       issue_path=str(issue), snapshot_path=str(snap), hoh_root=str(work / "hoh"),
                       base_commit="c" * 40, supersedes=supersedes)


def _transcript(i=100, o=10):
    return [_line("m1", i=i, o=o, cc=0, cr=0)]


def _kill(ports):
    return b1.KillPath(ports.hoh, ports.procs, sleep=lambda s: None, grace_s=2)


def _run_cell(work, ports, freeze, approvals, patterns, led=None, spec=None, **kw):
    led = led or b1.BudgetLedger(work / "ledger.jsonl")
    res = work / "res"
    res.mkdir(exist_ok=True)
    spec = spec or _spec(work)
    return b1.run_cell(spec, ports, res, led, patterns, freeze, approvals, **kw), led


def test_run_cell_stops_before_any_provider_call_on_a_wiring_mismatch(work, freeze, approvals, patterns):
    ports = FakePorts(_good("B", freeze, policy_digest=D64))
    doc, led = _run_cell(work, ports, freeze, approvals, patterns)
    assert ports.started == 0
    assert doc["ended"] == "instrument_failure" and doc["wiring_findings"][0]["check"] == "I11"
    assert led.total_m("0") == 0.0 and led.started("0") == 1       # started, charged nothing (no provider call)


def test_run_cell_compares_with_the_registered_declaration(work, freeze, approvals, patterns):
    # a cell that "declares" nothing cannot loosen the contract: a timeout of 99999 is a finding
    ports = FakePorts(_good("B", freeze, role_timeout_s={"planner": 99999, "developer": 99999, "qa": 99999}))
    doc, _ = _run_cell(work, ports, freeze, approvals, patterns)
    assert doc["ended"] == "instrument_failure" and ports.started == 0
    assert {f["check"] for f in doc["wiring_findings"]} == {"I14"}


def test_run_cell_requires_the_frozen_pattern_digest(work, freeze, approvals, patterns):
    other = b1.Freeze(freeze.policy_digest, freeze.house_text_digest, "none", "/store", D64)
    ports = FakePorts(_good("B", other))
    with pytest.raises(b1.Bench1Error, match="frozen digest"):
        _run_cell(work, ports, other, approvals, patterns)
    assert ports.started == 0


def test_run_cell_requires_an_approved_stage(work, freeze, patterns):
    ports = FakePorts(_good("B", freeze))
    with pytest.raises(b1.StageNotApproved):
        _run_cell(work, ports, freeze, b1.StageApprovals({"T": True, "0": True}), patterns,
                  spec=_spec(work, stage="1a"))
    assert ports.started == 0


def test_run_cell_refuses_to_start_above_the_stage_budget(work, freeze, approvals, patterns):
    led = b1.BudgetLedger(work / "l.jsonl", {**b1.HIGH_SUM_M, "0": 1.0})
    led.begin("0", "earlier", "B", 1)
    led.finish("0", "earlier", 2_000_000)
    ports = FakePorts(_good("B", freeze))
    with pytest.raises(b1.BudgetStop):
        _run_cell(work, ports, freeze, approvals, patterns, led=led)
    assert ports.started == 0


def test_run_cell_success_writes_the_run_record_with_the_grade_inputs(work, freeze, approvals, patterns):
    tr = work / "tr.jsonl"
    tr.write_text("x\n", encoding="utf-8")
    ports = FakePorts(_good("B", freeze), collected={
        "ended": "claim", "claimed_tree": "abc", "patch_path": "p.patch", "transcript_lines": _transcript(),
        "dispatches": 3, "transcripts": [str(tr)], "artefactual": ["K2", "K1"]})
    doc, led = _run_cell(work, ports, freeze, approvals, patterns, monotonic=iter([0.0, 12.5]).__next__)
    assert doc["ended"] == "claim" and doc["claimed_tree"] == "abc" and doc["pgid"] == 5151
    assert doc["artefactual"] == ["K1", "K2"] and ports.waited_bound == 26100
    assert led.total_m("0") == pytest.approx((100 + 50) / 1e6)
    on_disk = json.loads(Path(doc["path"]).read_text())
    assert on_disk["schema"] == b1.SCHEMA_RUN and on_disk["wiring_patterns_digest"] == patterns.digest
    # what `grade` needs is in the record
    assert on_disk["repo"] == "django/django" and on_disk["issue_path"] and on_disk["snapshot_path"]
    assert on_disk["hoh_root"] and on_disk["transcripts"] == [str(tr)]


def test_run_cell_truncated_cell_is_charged_its_measured_spend(work, freeze, approvals, patterns):
    ports = FakePorts(_good("B", freeze), finished=False, procs=FakeProcs(stubborn=True), collected={
        "ended": "claim", "transcript_lines": [_line("m1", i=1_000_000, o=100_000, cc=0, cr=0)]})
    doc, led = _run_cell(work, ports, freeze, approvals, patterns, kill=_kill(ports))
    assert doc["ended"] == "truncated"
    assert [s["step"] for s in doc["kill_steps"]][-3:] == ["sigterm_group", "sigkill_group", "group_gone"]
    assert doc["spend_wtu"] == 1_500_000 and led.total_m("0") == pytest.approx(1.5)


def test_run_cell_truncated_without_usage_is_charged_the_cap_implied_maximum(work, freeze, approvals, patterns):
    ports = FakePorts(_good("B", freeze), finished=False, collected={"transcript_lines": ["garbage"]})
    doc, led = _run_cell(work, ports, freeze, approvals, patterns, kill=_kill(ports))
    assert doc["ended"] == "truncated" and doc["spend_assumed"] is True
    assert led.total_m("0") == pytest.approx(10.8)


def test_run_cell_undeterminable_usage_is_an_instrument_failure_charged_at_the_maximum(
        work, freeze, approvals, patterns):
    ports = FakePorts(_good("B", freeze), collected={"ended": "claim", "claimed_tree": "abc",
                                                     "transcript_lines": ["not json"]})
    doc, led = _run_cell(work, ports, freeze, approvals, patterns)
    assert doc["ended"] == "instrument_failure" and doc["usage"] is None
    assert led.total_m("0") == pytest.approx(10.8)


def test_run_cell_unreadable_ending_is_charged_what_it_cost(work, freeze, approvals, patterns):
    ports = FakePorts(_good("B", freeze), collected={"ended": "done-ish", "transcript_lines": _transcript()})
    doc, led = _run_cell(work, ports, freeze, approvals, patterns)
    assert doc["ended"] == "instrument_failure" and led.total_m("0") == pytest.approx(150 / 1e6)


def test_run_cell_a_failing_wait_still_kills_and_records(work, freeze, approvals, patterns):
    ports = FakePorts(_good("B", freeze), wait_error=OSError("lost the child"),
                      collected={"transcript_lines": _transcript()}, procs=FakeProcs(stubborn=True))
    doc, led = _run_cell(work, ports, freeze, approvals, patterns, kill=_kill(ports))
    assert doc["ended"] == "instrument_failure" and "waiting failed" in doc["stop_reason"]
    assert (5151, signal.SIGKILL) in ports.procs.signals
    assert led.total_m("0") == pytest.approx(150 / 1e6)
    assert Path(doc["path"]).exists()


def test_run_cell_a_failing_start_is_charged_the_maximum(work, freeze, approvals, patterns):
    ports = FakePorts(_good("B", freeze), start_error=RuntimeError("no pane"))
    doc, led = _run_cell(work, ports, freeze, approvals, patterns)
    assert doc["ended"] == "instrument_failure" and led.total_m("0") == pytest.approx(10.8)


def test_run_cell_a_failing_readback_is_an_instrument_failure_without_a_start(work, freeze, approvals, patterns):
    ports = FakePorts(None, readback_error=RuntimeError("no state"))
    doc, led = _run_cell(work, ports, freeze, approvals, patterns)
    assert doc["ended"] == "instrument_failure" and ports.started == 0 and led.total_m("0") == 0.0


def test_run_cell_needs_the_scan_inputs(work, freeze, approvals, patterns):
    ports = FakePorts(_good("B", freeze))
    spec = _spec(work)
    spec.repo_slug = ""
    with pytest.raises(b1.Bench1Error, match="repo slug"):
        _run_cell(work, ports, freeze, approvals, patterns, spec=spec)
    spec = _spec(work)
    spec.issue_path = ""
    with pytest.raises(b1.Bench1Error, match="issue_path"):
        _run_cell(work, ports, freeze, approvals, patterns, spec=spec)
    assert ports.started == 0


def test_run_cell_attempt_two_names_what_it_supersedes_and_attempt_three_does_not_exist(
        work, freeze, approvals, patterns):
    res = work / "res"
    res.mkdir()
    ports = FakePorts(_good("B", freeze), collected={"ended": "no_claim", "transcript_lines": _transcript(),
                                                     "patch_path": "p"})
    led = b1.BudgetLedger(work / "ledger.jsonl")
    first = b1.run_cell(_spec(work), ports, res, led, patterns, freeze, approvals)
    # attempt 1 is not a non-outcome cell file yet (no grade): a re-run is refused
    with pytest.raises(b1.Bench1Error, match="non-outcome"):
        b1.run_cell(_spec(work, attempt=2, supersedes=first["key"]), ports, res, led, patterns, freeze,
                    approvals)
    _final_cell(res, "django__django-12325", "B", 1, "TRUNCATED")
    second = b1.run_cell(_spec(work, attempt=2, supersedes=first["key"]), ports, res, led, patterns, freeze,
                         approvals)
    assert second["supersedes"] == first["key"] and Path(first["path"]).exists()
    with pytest.raises(b1.Bench1Error, match="limit"):
        b1.run_cell(_spec(work, attempt=3, supersedes=second["key"]), ports, res, led, patterns, freeze,
                    approvals)


def test_run_cell_stage_cell_cap_stops_a_runaway_of_cells(work, freeze, approvals, patterns):
    led = b1.BudgetLedger(work / "l.jsonl", planned={**b1.STAGE_PLANNED, "0": 1})
    res = work / "res"
    res.mkdir()
    ports = FakePorts(_good("A0", freeze), collected={"ended": "no_claim", "transcript_lines": ["garbage"]})
    for task in ("t1", "t2"):
        b1.run_cell(_spec(work, arm="A0", task=task), ports, res, led, patterns, freeze, approvals)
    with pytest.raises(b1.Bench1Error, match="cell cap"):
        b1.run_cell(_spec(work, arm="A0", task="t3"), ports, res, led, patterns, freeze, approvals)


# --------------------------------------------------------------------------- hidden rows, liveness, the scan


class FakeDataset:
    def __init__(self):
        self.calls = 0
        self.rows = {"T1": {"FAIL_TO_PASS": ["test_alpha_unique_name"], "PASS_TO_PASS": ["test_beta_unique_name"],
                            "test_patch": "diff", "patch": "diff"},
                     "django__django-12325": {"FAIL_TO_PASS": ["test_alpha_unique_name"],
                                              "PASS_TO_PASS": ["test_beta_unique_name"]}}

    def fetch(self, ids):
        self.calls += 1
        return {i: self.rows[i] for i in ids if i in self.rows}


def test_hidden_rows_are_refused_while_an_agent_is_alive(work):
    ds = FakeDataset()
    with pytest.raises(b1.HiddenRowsRefused), b1.HiddenRows(ds, work, lambda: ["pane-1"]).for_grading(["T1"]):
        pass
    assert ds.calls == 0


@pytest.mark.parametrize("answer", [RuntimeError("hoh surface list failed"), None, "none", [1]])
def test_hidden_rows_an_unanswerable_liveness_question_is_alive(work, answer):
    ds = FakeDataset()

    def alive():
        if isinstance(answer, Exception):
            raise answer
        return answer
    with pytest.raises(b1.HiddenRowsRefused, match="unknown|unreadable"), \
            b1.HiddenRows(ds, work, alive).for_grading(["T1"]):
        pass
    assert ds.calls == 0


def test_hidden_rows_live_in_a_self_releasing_directory(work):
    h = b1.HiddenRows(FakeDataset(), work, list)
    with h.for_grading(["T1"]) as (rows, scratch):
        assert rows["T1"]["FAIL_TO_PASS"] and scratch.is_dir() and scratch.parent == work
        (scratch / "x").write_text("row copy")
        seen = scratch
    assert not seen.exists()


def test_hidden_rows_window_violation_is_raised(work):
    state = {"n": 0}

    def alive():
        state["n"] += 1
        return [] if state["n"] == 1 else ["late-pane"]
    hidden = b1.HiddenRows(FakeDataset(), work, alive)
    with pytest.raises(b1.HiddenRowsRefused, match="while the rows were loaded"), hidden.for_grading(["T1"]):
        pass


def test_hidden_rows_missing_instance_is_an_error(work):
    hidden = b1.HiddenRows(FakeDataset(), work, list)
    with pytest.raises(b1.Bench1Error), hidden.for_grading(["T1", "T2"]):
        pass


def _surface_json(*rows):
    return json.dumps({"surfaces": list(rows)})


def test_parse_surface_list():
    assert b1.parse_surface_list(_surface_json()) == []
    owned = {"run_id": "r1", "pane_id": "p1", "state": "owned"}
    released = {"run_id": "r1", "pane_id": "p2", "state": "released"}
    assert b1.parse_surface_list(_surface_json(owned, released)) == ["r1:p1"]
    for bad in ("", "not json", "[]", json.dumps({"surfaces": "x"}), json.dumps({"other": []}),
                _surface_json({"run_id": "r", "problem": "unreadable"}),
                _surface_json({"run_id": "r", "pane_id": "p"}),
                _surface_json({"run_id": "r", "pane_id": "p", "state": "weird"}),
                _surface_json("a row")):
        with pytest.raises(b1.Bench1Error):
            b1.parse_surface_list(bad)


class FakeHohRoot:
    def __init__(self, out=None, error=None):
        self.out, self.error = out, error

    def surface_list(self):
        if self.error:
            raise self.error
        return self.out


def test_liveness_asks_every_root_and_refuses_when_one_cannot_answer(work):
    ok = FakeHohRoot(_surface_json())
    owned = FakeHohRoot(_surface_json({"run_id": "r", "pane_id": "p", "state": "owned"}))
    broken = FakeHohRoot(error=b1.Bench1Error("hoh surface failed with exit 1"))
    assert b1.surface_liveness([ok, ok])() == []
    assert b1.surface_liveness([ok, owned])() == ["r:p"]
    ds = FakeDataset()
    with pytest.raises(b1.HiddenRowsRefused, match="unknown"), \
            b1.HiddenRows(ds, work, b1.surface_liveness([ok, broken])).for_grading(["T1"]):
        pass
    assert ds.calls == 0
    with pytest.raises(b1.Bench1Error, match="nobody"):
        b1.surface_liveness([])


def _scan(work, text, **kw):
    f = work / "t.jsonl"
    f.write_text(text, encoding="utf-8")
    args = {"sealed_tokens": ["bench1-sealed", "rows_100.json"], "test_ids": ["test_alpha_unique_name"],
            "public_text": "the issue mentions test_other", "repo_slug": "django/django"}
    args.update(kw)
    return b1.scan_transcripts([f], **args)


def test_scan_clean_transcript_has_no_hits(work):
    assert _scan(work, "ran the suite\nall green\n") == []


@pytest.mark.parametrize("text,category", [
    ("cat /x/bench1-sealed/rows_0.json", "sealed_path"),
    ("grep rows_100.json", "sealed_path"),
    ("looking at FAIL_TO_PASS", "hidden_token"),
    ("looking at PASS_TO_PASS", "hidden_token"),
    ("the test_patch is", "hidden_token"),
    ("run test_alpha_unique_name now", "test_id"),
    ("run test_beta_unique_name now", "test_id"),
    ("see https://github.com/django/django/pull/123", "upstream_url"),
    ("see github.com/django/django/commit/abc", "upstream_url"),
    ("see github.com/django/django/issues/77", "upstream_url"),
])
def test_scan_categories(work, text, category):
    hits = _scan(work, "line one\n" + text + "\n", test_ids=["test_alpha_unique_name", "test_beta_unique_name"])
    assert [h.category for h in hits] == [category] and hits[0].line == 2


def test_scan_test_id_in_the_public_text_is_not_a_hit(work):
    assert _scan(work, "we use test_alpha_unique_name here", public_text="uses test_alpha_unique_name") == []


def test_scan_test_id_needs_a_word_boundary(work):
    assert _scan(work, "xtest_alpha_unique_name_longer") == []


def test_scan_other_repository_url_is_not_a_hit(work):
    assert _scan(work, "github.com/other/project/pull/1") == []


def test_scan_unreadable_transcript_raises_instead_of_passing(work):
    with pytest.raises(b1.Bench1Error, match="cannot be"):
        b1.scan_transcripts([work / "missing.jsonl"], sealed_tokens=["s"], test_ids=[], public_text="p",
                            repo_slug="a/b")
    bad = work / "bad.jsonl"
    bad.write_bytes(b"\xff\xfe\x00\x00 not utf-8")
    with pytest.raises(b1.Bench1Error):
        b1.scan_transcripts([bad], sealed_tokens=["s"], test_ids=[], public_text="p", repo_slug="a/b")


@pytest.mark.parametrize("kw", [{"repo_slug": ""}, {"repo_slug": "no-slash"}, {"public_text": ""},
                                {"public_text": "   \n"}, {"sealed_tokens": []}, {"sealed_tokens": [""]}])
def test_scan_without_its_inputs_is_a_refusal_never_zero_hits(work, kw):
    with pytest.raises(b1.ScanInputsMissing):
        _scan(work, "clean", **kw)


def test_build_public_text(work):
    issue = work / "i.txt"
    issue.write_text("ISSUE TEXT\n", encoding="utf-8")
    snap = work / "s"
    (snap / ".git").mkdir(parents=True)
    (snap / ".git" / "config").write_text("GITSECRET", encoding="utf-8")
    (snap / "a.py").write_text("def test_in_base_tree(): pass\n", encoding="utf-8")
    (snap / "bin.dat").write_bytes(b"\x00\x01binary")
    text = b1.build_public_text(issue, snap)
    assert "ISSUE TEXT" in text and "test_in_base_tree" in text and "GITSECRET" not in text
    empty = work / "e.txt"
    empty.write_text(" \n", encoding="utf-8")
    with pytest.raises(b1.ScanInputsMissing):
        b1.build_public_text(empty, snap)
    with pytest.raises(b1.ScanInputsMissing):
        b1.build_public_text(issue, work / "nope")


# --------------------------------------------------------------------------- grade, regrade, leak adjudication


class FakeGrader:
    def __init__(self, resolved=True):
        self.resolved, self.calls = resolved, 0

    def evaluate(self, task, patch_path, row, scratch):
        self.calls += 1
        assert Path(scratch).is_dir()
        return {"resolved": self.resolved}


def _run_doc(work, ended="claim", transcript_text="clean\n", patch=True, reason="", transcripts=True,
             dispatches=3, usage=True):
    t = work / "tr.jsonl"
    t.write_text(transcript_text, encoding="utf-8")
    p = work / "p.patch"
    p.write_text("diff --git a b\n", encoding="utf-8")
    spec = _spec(work)
    return {"key": "T1.B.1.a1", "task": "T1", "arm": "B", "rep": 1, "attempt": 1, "stage": "0",
            "ended": ended, "stop_reason": reason, "patch_path": str(p) if patch else None,
            "transcripts": [str(t)] if transcripts else [], "usage": {"wtu": 5.0} if usage else None,
            "dispatches": dispatches, "artefactual": ["K1"], "repo": "django/django",
            "issue_path": spec.issue_path, "snapshot_path": spec.snapshot_path, "hoh_root": spec.hoh_root,
            "wiring_patterns_digest": b1.WiringPatterns.load(PATTERNS_FILE).digest}


def _grade(work, run, patterns, resolved=True, alive=()):
    res = work / "res"
    res.mkdir(exist_ok=True)
    hidden = b1.HiddenRows(FakeDataset(), work, lambda: list(alive))
    grader = FakeGrader(resolved)
    cell = b1.grade_cell(run, hidden, grader, patterns, res, sealed_tokens=["bench1-sealed"])
    return cell, grader, res


def test_grade_cell_claim_and_pass(work, patterns):
    cell, grader, res = _grade(work, _run_doc(work), patterns, resolved=True)
    assert cell["class"] == "ACCEPT_CORRECT" and cell["passed"] is True and not cell["leak_suspect"]
    assert cell["scanned"] is True and cell["scan_files"] == 1
    assert (res / "T1.B.1.a1.cell.json").exists() and grader.calls == 1


def test_grade_cell_claim_and_fail_is_a_false_accept(work, patterns):
    cell, _, _ = _grade(work, _run_doc(work), patterns, resolved=False)
    assert cell["class"] == "FALSE_ACCEPT"


def test_grade_cell_refused_while_an_agent_is_alive_writes_nothing(work, patterns):
    res = work / "res"
    res.mkdir()
    with pytest.raises(b1.HiddenRowsRefused):
        _grade(work, _run_doc(work), patterns, alive=["pane"])
    assert list(res.glob("*.cell.json")) == []


def test_grade_cell_non_outcome_is_scanned_and_not_graded(work, patterns):
    cell, grader, _ = _grade(work, _run_doc(work, ended="truncated"), patterns)
    assert cell["class"] == "TRUNCATED" and grader.calls == 0 and cell["passed"] is None
    assert cell["scanned"] is True


def test_grade_cell_scan_flags_a_leak_in_a_provider_failure_cell(work, patterns):
    run = _run_doc(work, ended="provider_failure", transcript_text="cat bench1-sealed/rows_0.json\n")
    cell, grader, _ = _grade(work, run, patterns)
    assert cell["class"] == "PROVIDER_FAILURE" and cell["leak_suspect"] is True and grader.calls == 0


def test_grade_cell_product_stop_is_graded_descriptively(work, patterns):
    cell, grader, _ = _grade(work, _run_doc(work, ended="budget_exhausted"), patterns, resolved=True)
    assert cell["class"] == "NO_CLAIM_BUDGET_EXHAUSTED" and cell["passed"] is True and grader.calls == 1


def test_grade_cell_wiring_stop_is_an_instrument_failure_and_not_graded(work, patterns):
    run = _run_doc(work, ended="blocked", reason="storage admission unavailable")
    cell, grader, _ = _grade(work, run, patterns)
    assert cell["class"] == "INSTRUMENT_FAILURE" and grader.calls == 0


def test_grade_cell_claim_without_a_patch_is_an_instrument_failure(work, patterns):
    cell, grader, _ = _grade(work, _run_doc(work, patch=False), patterns)
    assert cell["class"] == "INSTRUMENT_FAILURE" and grader.calls == 0


def test_grade_cell_a_cell_with_sessions_but_no_transcript_is_a_named_non_outcome(work, patterns):
    cell, grader, _ = _grade(work, _run_doc(work, transcripts=False), patterns)
    assert cell["class"] == "INSTRUMENT_FAILURE" and cell["scanned"] is False
    assert cell["scan_gap"] and grader.calls == 0 and cell["passed"] is None
    # no sessions ran (wiring mismatch before the start): no transcript is legitimate
    run = _run_doc(work, ended="instrument_failure", transcripts=False, dispatches=0, usage=False, patch=False)
    run["key"] = "T1.B.1.a2"
    cell, grader, _ = _grade(work, run, patterns)
    assert cell["class"] == "INSTRUMENT_FAILURE" and cell["scanned"] is False and grader.calls == 0


@pytest.mark.parametrize("missing", ["repo", "issue_path", "snapshot_path", "hoh_root"])
def test_grade_cell_refuses_a_run_record_without_its_scan_inputs(work, patterns, missing):
    run = _run_doc(work)
    run[missing] = ""
    res = work / "res"
    res.mkdir()
    hidden = b1.HiddenRows(FakeDataset(), work, list)
    with pytest.raises(b1.ScanInputsMissing, match=missing):
        b1.grade_cell(run, hidden, FakeGrader(), patterns, res, sealed_tokens=["s"])
    assert list(res.glob("*.cell.json")) == []


def test_grade_cell_refuses_a_scan_without_sealed_tokens(work, patterns):
    res = work / "res"
    res.mkdir()
    hidden = b1.HiddenRows(FakeDataset(), work, list)
    with pytest.raises(b1.ScanInputsMissing):
        b1.grade_cell(_run_doc(work), hidden, FakeGrader(), patterns, res, sealed_tokens=[])


def test_grade_cell_records_scan_hits_without_changing_the_class(work, patterns):
    run = _run_doc(work, transcript_text="I read bench1-sealed/rows_0.json\n")
    cell, _, _ = _grade(work, run, patterns, resolved=True)
    assert cell["leak_suspect"] is True and cell["scan_hits"][0]["category"] == "sealed_path"
    assert cell["class"] == "ACCEPT_CORRECT"


def test_grade_cell_scans_the_p2p_ids_of_the_row(work, patterns):
    run = _run_doc(work, transcript_text="running test_beta_unique_name\n")
    run["task"] = "django__django-12325"
    run["key"] = "django__django-12325.B.1.a1"
    cell, _, _ = _grade(work, run, patterns)
    assert cell["leak_suspect"] is True and cell["scan_hits"][0]["token"] == "test_beta_unique_name"


def test_regrade_reports_agreement(work, patterns):
    cell, _, res = _grade(work, _run_doc(work), patterns, resolved=True)
    hidden = b1.HiddenRows(FakeDataset(), work, list)
    assert b1.regrade_cell(cell, work / "p.patch", hidden, FakeGrader(True), res)["agrees"] is True
    assert b1.regrade_cell(cell, work / "p.patch", hidden, FakeGrader(False), res)["agrees"] is False


def _suspect_cell(work, patterns):
    run = _run_doc(work, transcript_text="I read bench1-sealed/rows_0.json\n")
    return _grade(work, run, patterns)


def test_invalid_leak_has_a_writer_that_needs_two_confirming_readers(work, patterns):
    cell, _, res = _suspect_cell(work, patterns)
    key = cell["key"]
    with pytest.raises(b1.Bench1Error, match="two distinct readers"):
        b1.confirm_leak(res, key, [{"reader": "r1", "verdict": "CONFIRMED"}])
    with pytest.raises(b1.Bench1Error, match="two distinct readers"):
        b1.confirm_leak(res, key, [{"reader": "r1", "verdict": "CONFIRMED"},
                                   {"reader": "r1", "verdict": "CONFIRMED"}])
    with pytest.raises(b1.Bench1Error):
        b1.confirm_leak(res, key, [{"reader": "r1", "verdict": "MAYBE"}, {"reader": "r2", "verdict": "CONFIRMED"}])
    doc = b1.confirm_leak(res, key, [{"reader": "r1", "verdict": "CONFIRMED"},
                                     {"reader": "r2", "verdict": "CONFIRMED"}])
    assert doc["confirmed"] is True
    assert b1.load_cells(res)[0]["class"] == "INVALID_LEAK"
    assert json.loads((res / f"{key}.cell.json").read_text())["class"] == "ACCEPT_CORRECT"   # file untouched


def test_a_disputed_leak_does_not_change_the_class(work, patterns):
    cell, _, res = _suspect_cell(work, patterns)
    doc = b1.confirm_leak(res, cell["key"], [{"reader": "r1", "verdict": "CONFIRMED"},
                                             {"reader": "r2", "verdict": "NOT_CONFIRMED"}])
    assert doc["confirmed"] is False and b1.load_cells(res)[0]["class"] == "ACCEPT_CORRECT"


def test_only_a_suspect_cell_is_adjudicated(work, patterns):
    cell, _, res = _grade(work, _run_doc(work), patterns)
    with pytest.raises(b1.Bench1Error, match="leak_suspect"):
        b1.confirm_leak(res, cell["key"], [{"reader": "a", "verdict": "CONFIRMED"},
                                           {"reader": "b", "verdict": "CONFIRMED"}])


# --------------------------------------------------------------------------- snapshot (I17) -- local git only


def _make_source(root: Path) -> Path:
    src = root / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (src / "_version.py").write_text("v = '0'\n", encoding="utf-8")       # an image build artefact
    return src


def test_snapshot_is_one_commit_without_foreign_objects(work):
    commit = b1.make_snapshot(_make_source(work), work / "snap")
    rep = b1.snapshot_report(work / "snap")
    assert rep["commits"] == 1 and rep["refs"] == 1 and rep["unreachable_objects"] == 0
    assert len(commit) == 40
    tracked = subprocess.run(["git", "-C", str(work / "snap"), "ls-files"], capture_output=True,
                             text=True, check=False).stdout.split()
    assert "_version.py" in tracked and "pkg/a.py" in tracked


def test_snapshot_report_sees_history_and_stray_objects(work):
    b1.make_snapshot(_make_source(work), work / "snap")
    snap = work / "snap"
    (snap / "new.txt").write_text("n\n", encoding="utf-8")
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(snap)}
    subprocess.run(["git", "-C", str(snap), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(snap), "-c", "user.name=x", "-c", "user.email=x@x", "-c",
                    "commit.gpgsign=false", "commit", "-q", "-m", "two"], check=True, env=env)
    assert b1.snapshot_report(snap)["commits"] == 2
    stray = subprocess.run(["git", "-C", str(snap), "hash-object", "-w", "--stdin"], input="loose blob",
                           capture_output=True, text=True, env=env, check=True).stdout.strip()
    assert len(stray) == 40
    assert b1.snapshot_report(snap)["unreachable_objects"] >= 1


def test_snapshot_source_with_a_git_directory_is_refused(work):
    src = _make_source(work)
    (src / ".git").mkdir()
    with pytest.raises(b1.Bench1Error, match=r"\.git"):
        b1.make_snapshot(src, work / "snap")


def test_snapshot_skips_a_nested_git_directory_and_does_not_follow_a_top_level_symlink(work):
    src = _make_source(work)
    (src / "vendor" / ".git").mkdir(parents=True)
    (src / "vendor" / ".git" / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
    (src / "vendor" / "lib.py").write_text("y = 2\n", encoding="utf-8")
    outside = work / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET", encoding="utf-8")
    os.symlink(outside, src / "link")
    b1.make_snapshot(src, work / "snap")
    snap = work / "snap"
    assert not (snap / "vendor" / ".git").exists() and (snap / "vendor" / "lib.py").exists()
    assert (snap / "link").is_symlink() and os.readlink(snap / "link") == str(outside)
    tracked = subprocess.run(["git", "-C", str(snap), "ls-files"], capture_output=True, text=True,
                             check=False).stdout.split()
    assert "secret.txt" not in " ".join(tracked) and "link/secret.txt" not in tracked
    assert b1.snapshot_report(snap)["commits"] == 1


# --------------------------------------------------------------------------- images: manifest first, own images only


class FakeDocker:
    """A container runtime in memory: refs map to image ids."""

    def __init__(self, images=None, used=(), remove_error=None):
        self.images = dict(images or {})
        self.used = set(used)
        self.pulled, self.removed = [], []
        self.pull_ids = {}
        self.remove_error = remove_error

    def image_id(self, ref):
        return self.images.get(ref)

    def pull(self, ref):
        self.pulled.append(ref)
        self.images[ref] = self.pull_ids.get(ref, IMG1)

    def containers_using(self, image_id):
        return ["c1"] if image_id in self.used else []

    def remove_image(self, image_id):
        if self.remove_error:
            raise self.remove_error
        self.removed.append(image_id)
        for ref, i in list(self.images.items()):
            if i == image_id:
                del self.images[ref]


def _setup(work, refs=("img/a:1", "img/b:1")):
    chain = work / "CHAIN.jsonl"
    rec = work / "rec"
    rec.mkdir()
    m = work / "manifest.json"
    b1.write_manifest(m, "b1", [{"instance_id": f"T{i}", "ref": r} for i, r in enumerate(refs)], chain)
    return m, rec, chain


def test_manifest_is_create_only_sealed_and_checks_its_entries(work):
    m, _rec, chain = _setup(work)
    with pytest.raises(FileExistsError):
        b1.write_manifest(m, "b1", [{"instance_id": "T", "ref": "img/z:1"}], chain)
    with pytest.raises(b1.ManifestRefused):
        b1.write_manifest(work / "m2.json", "v3", [], chain)
    with pytest.raises(b1.ManifestRefused):
        b1.write_manifest(work / "m3.json", "b1", [{"instance_id": "T", "ref": "r:1"}] * 2, chain)
    with pytest.raises(b1.ManifestRefused):
        b1.write_manifest(work / "m4.json", "b1", [{"instance_id": "T"}], chain)
    with pytest.raises(b1.ManifestRefused):
        b1.write_manifest(work / "m5.json", "b1", [{"instance_id": "T", "ref": "--force"}], chain)
    b1.verify_sealed(chain, m)


def test_pull_only_what_the_manifest_lists_and_records_the_id(work):
    m, rec, chain = _setup(work)
    d = FakeDocker()
    r = b1.pull_one(d, m, "img/a:1", rec, chain)
    assert d.pulled == ["img/a:1"] and r["image_id"] == IMG1 and r["preexisting"] is False and r["ours"] is True
    with pytest.raises(b1.ManifestRefused):
        b1.pull_one(d, m, "other/img:9", rec, chain)
    assert d.pulled == ["img/a:1"]


def test_a_preexisting_image_is_not_ours_and_is_never_pulled_or_released(work):
    m, rec, chain = _setup(work)
    d = FakeDocker(images={"img/a:1": IMG2})
    r = b1.pull_one(d, m, "img/a:1", rec, chain)
    assert d.pulled == [] and r["preexisting"] is True and r["ours"] is False and r["image_id"] == IMG2
    with pytest.raises(b1.ManifestRefused, match="not ours"):
        b1.release_images(d, m, rec, chain, refs=["img/a:1"], confirm=True)
    # by default (no refs) a preexisting image is simply not in the plan
    assert b1.release_images(d, m, rec, chain, confirm=True) == []
    assert d.removed == [] and d.images == {"img/a:1": IMG2}


def test_pull_checks_the_expected_id(work):
    chain, rec = work / "CHAIN.jsonl", work / "rec"
    rec.mkdir()
    m = work / "m.json"
    b1.write_manifest(m, "b1", [{"instance_id": "T", "ref": "img/a:1", "expected_digest": IMG2}], chain)
    with pytest.raises(b1.ManifestRefused, match="expected"):
        b1.pull_one(FakeDocker(), m, "img/a:1", rec, chain)


def test_release_dry_run_removes_nothing_and_writes_receipts(work):
    m, rec, chain = _setup(work)
    d = FakeDocker()
    b1.pull_one(d, m, "img/a:1", rec, chain)
    rows = b1.release_images(d, m, rec, chain)
    assert rows[0]["dry_run"] is True and d.removed == [] and list(rec.glob("release-*-dry-*.json"))


def test_release_confirmed_removes_by_image_id_and_writes_intent_before_done(work):
    m, rec, chain = _setup(work)
    d = FakeDocker()
    b1.pull_one(d, m, "img/a:1", rec, chain)
    rows = b1.release_images(d, m, rec, chain, confirm=True)
    assert d.removed == [IMG1] and rows[0]["phase"] == "done"
    assert len(list(rec.glob("release-*-intent-*.json"))) == 1 and len(list(rec.glob("release-*-done-*.json"))) == 1
    kinds = [json.loads(x)["kind"] for x in chain.read_text().splitlines()]
    assert kinds.index("release-intent") < kinds.index("release-done")


def test_the_release_receipt_exists_even_if_the_removal_fails(work):
    m, rec, chain = _setup(work)
    d = FakeDocker(remove_error=RuntimeError("runtime said no"))
    b1.pull_one(d, m, "img/a:1", rec, chain)
    with pytest.raises(RuntimeError):
        b1.release_images(d, m, rec, chain, confirm=True)
    assert len(list(rec.glob("release-*-intent-*.json"))) == 1 and not list(rec.glob("release-*-done-*.json"))


def test_release_refuses_an_image_that_is_not_in_the_manifest(work):
    m, rec, chain = _setup(work)
    d = FakeDocker(images={"postgres:16": IMG2})
    b1.pull_one(d, m, "img/a:1", rec, chain)
    with pytest.raises(b1.ManifestRefused, match="not in the manifest"):
        b1.release_images(d, m, rec, chain, refs=["img/a:1", "postgres:16"], confirm=True)
    assert d.removed == []


def test_release_refuses_an_image_without_a_pull_receipt(work):
    m, rec, chain = _setup(work)
    with pytest.raises(b1.ManifestRefused, match="no pull receipt"):
        b1.release_images(FakeDocker(), m, rec, chain, refs=["img/b:1"], confirm=True)


def test_release_refuses_when_the_ref_now_names_another_image(work):
    m, rec, chain = _setup(work)
    d = FakeDocker()
    b1.pull_one(d, m, "img/a:1", rec, chain)
    d.images["img/a:1"] = IMG2                              # someone re-tagged it
    with pytest.raises(b1.ManifestRefused, match="another image"):
        b1.release_images(d, m, rec, chain, confirm=True)
    assert d.removed == []


def test_release_refuses_an_image_that_has_gone_and_one_that_is_used(work):
    m, rec, chain = _setup(work)
    d = FakeDocker()
    b1.pull_one(d, m, "img/a:1", rec, chain)
    d.used.add(IMG1)
    with pytest.raises(b1.ManifestRefused, match="container"):
        b1.release_images(d, m, rec, chain, confirm=True)
    d.used.clear()
    del d.images["img/a:1"]
    with pytest.raises(b1.ManifestRefused, match="no longer present"):
        b1.release_images(d, m, rec, chain, confirm=True)


def test_release_refuses_a_receipt_bound_to_a_different_manifest(work):
    m, rec, chain = _setup(work)
    d = FakeDocker()
    b1.pull_one(d, m, "img/a:1", rec, chain)
    other = work / "m-other.json"
    b1.write_manifest(other, "b1", [{"instance_id": "T0", "ref": "img/a:1"}], chain)
    with pytest.raises(b1.ManifestRefused, match="no pull receipt"):
        b1.release_images(d, other, rec, chain, refs=["img/a:1"], confirm=True)
    assert d.removed == []


def test_release_refuses_a_hand_edited_receipt_or_manifest(work):
    m, rec, chain = _setup(work)
    d = FakeDocker()
    b1.pull_one(d, m, "img/a:1", rec, chain)
    receipt = next(rec.glob("pull-*.json"))
    doc = json.loads(receipt.read_text())
    doc["image_id"] = IMG2                                 # a hand edit of the pinned id
    receipt.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(b1.ManifestRefused, match="not the file the tool wrote"):
        b1.release_images(d, m, rec, chain, confirm=True)
    assert d.removed == []
    m2, rec2, chain2 = _setup(work / "sub") if (work / "sub").mkdir() is None else (None, None, None)
    b1.pull_one(FakeDocker(), m2, "img/a:1", rec2, chain2)
    doc = json.loads(m2.read_text())
    doc["images"].append({"instance_id": "T9", "ref": "postgres:16"})
    m2.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(b1.ManifestRefused):
        b1.release_images(FakeDocker(), m2, rec2, chain2, refs=["postgres:16"], confirm=True)


def test_release_refuses_a_receipt_dated_before_the_manifest_or_in_the_future(work):
    m, rec, chain = _setup(work)
    d = FakeDocker()
    b1.pull_one(d, m, "img/a:1", rec, chain)
    receipt = next(rec.glob("pull-*.json"))
    for stamp in ("19990101T000000Z", "29990101T000000Z"):
        doc = json.loads(receipt.read_text())
        doc["pulled_at"] = stamp
        forged = rec / f"pull-zzzz-{stamp}.json"
        forged.write_text(json.dumps(doc), encoding="utf-8")
        b1.seal(chain, forged, "pull")                      # even a sealed forgery is refused by its dates
        with pytest.raises(b1.ManifestRefused, match="older than the manifest or dated in the future"):
            b1.release_images(d, m, rec, chain, refs=["img/a:1"], confirm=True)
        forged.rename(rec / f"forged-{stamp}.json.kept")
    assert d.removed == []


def test_release_refuses_a_missing_or_unreadable_chain(work):
    m, rec, chain = _setup(work)
    d = FakeDocker()
    b1.pull_one(d, m, "img/a:1", rec, chain)
    with pytest.raises(b1.ManifestRefused, match="chain"):
        b1.release_images(d, m, rec, work / "no-chain.jsonl", confirm=True)


# --------------------------------------------------------------------------- the container runtime's argument vectors


def test_every_docker_vector_is_pinned_exactly():
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        out = IMG1 + "\n" if argv[:3] == ["docker", "image", "inspect"] else ""
        return subprocess.CompletedProcess(argv, 0, out, "")
    d = b1.SubprocessDocker(runner=runner)
    d.pull("img/a:1")
    assert d.image_id("img/a:1") == IMG1
    assert d.containers_using(IMG1) == []
    d.remove_image(IMG1)
    assert calls == [
        ["docker", "pull", "img/a:1"],
        ["docker", "image", "inspect", "--format", "{{.Id}}", "img/a:1"],
        ["docker", "ps", "--all", "--filter", f"ancestor={IMG1}", "--format", "{{.ID}}"],
        ["docker", "image", "remove", IMG1],
    ]


@pytest.mark.parametrize("argv", [
    ["docker", "image", "remove", "-f", IMG1],
    ["docker", "image", "remove", IMG1, IMG2],
    ["docker", "image", "remove", "img/a:1"],                    # by name
    ["docker", "image", "remove"],
    ["docker", "image", "prune"],
    ["docker", "system", "prune", "--all"],
    ["docker", "rmi", IMG1],
    ["docker", "container", "prune"],
    ["docker", "pull", "--all-tags", "img/a"],
    ["docker", "pull", "-a"],
    ["docker", "run", "--privileged", "--network", "none", "img", "x"],
    ["sh", "-c", "docker image remove x"],
    [],
])
def test_a_container_runtime_vector_off_the_allow_list_is_refused_before_it_runs(argv):
    ran = []
    d = b1.SubprocessDocker(runner=lambda a, **k: ran.append(a))
    with pytest.raises(b1.Bench1Error, match="allow-list"):
        d._exec(argv)
    assert ran == [] and b1.docker_argv_allowed(argv) is False


@pytest.mark.parametrize("action,args", [
    ("image_remove", ("img/a:1",)), ("image_remove", ("-f",)), ("image_remove", (IMG1, "-f")),
    ("pull", ("--all-tags",)), ("pull", ("-a",)), ("image_id", ("--format=x",)), ("prune", ()),
    ("system_prune", ()), ("containers_using", ("img/a:1",)),
])
def test_docker_argv_builder_refuses_shapes_it_does_not_have(action, args):
    with pytest.raises(b1.Bench1Error):
        b1.docker_argv(action, *args)


def test_bench_test_argv_pins_user_network_and_mount(work):
    argv = b1.bench_test_argv("img:1", work, "python -m pytest tests/x.py", uid=1000, gid=1001)
    assert argv[:2] == ["docker", "run"]
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--user") + 1] == "1000:1001"
    assert f"{work.resolve()}:/testbed" in argv
    assert "--privileged" not in argv and argv[-1].endswith("&& python -m pytest tests/x.py")
    assert b1.docker_argv_allowed(argv) is True


def test_every_subprocess_call_site_and_module_call_is_on_the_allow_list():
    """A scan of the tool's own source: where processes are started, which functions of `os`, `shutil`,
    `subprocess` and the like may be called, and which git verbs exist. A new call site is a new review item."""
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    enclosing: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                enclosing[id(n)] = fn.name
    sub_sites, module_calls, git_calls = set(), set(), []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            v = n.func.value
            if isinstance(v, ast.Name) and v.id in {"os", "shutil", "subprocess", "signal", "stat", "tempfile"}:
                module_calls.add(f"{v.id}.{n.func.attr}")
                if v.id == "subprocess":
                    sub_sites.add(enclosing.get(id(n)))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_git":
            git_calls.append(n)
    assert sub_sites <= {"_git", "evaluate"}
    assert module_calls == {
        "os.close", "os.fstat", "os.fsync", "os.getgid", "os.getpgid", "os.getpgrp", "os.getppid", "os.getuid",
        "os.killpg", "os.open", "os.read", "os.readlink", "os.symlink", "os.walk", "os.write",
        "shutil.copy2", "shutil.copytree", "shutil.ignore_patterns", "stat.S_ISREG", "subprocess.run",
        "tempfile.TemporaryDirectory"}
    # git verbs: every `_git(repo, ...)` call site names an allowed verb among its constant arguments
    for call in git_calls:
        consts = [a.value for a in call.args[1:] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        assert any(c in b1.GIT_VERBS for c in consts), consts
    assert set(b1.GIT_VERBS) == {"init", "add", "commit", "rev-parse", "rev-list", "for-each-ref", "cat-file"}


def test_the_tool_calls_only_the_methods_it_is_known_to_call():
    """The attribute-call names of the whole file against a frozen list: a removal verb, a rename, a
    truncation or an in-place write would be a new name and fail here."""
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    names = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    allowed = {
        "ArgumentParser", "BytesIO", "Random", "S_ISREG", "TemporaryDirectory", "_alive", "_append", "_cells",
        "_exec", "_rows", "add", "add_argument", "add_parser", "add_subparsers", "agents_alive", "append",
        "approved", "argv", "asdict", "begin", "build_and_readback", "cancel", "ceil", "check_before_start",
        "close", "close_owned", "collect", "comb", "compile", "containers_using", "copy2", "copytree",
        "dataclass", "decode", "dumps", "encode", "escape", "evaluate", "execute", "exists", "exit", "exp",
        "extend", "fetch", "finditer", "finish", "floor", "for_grading", "fstat", "fsync", "fullmatch", "func",
        "get", "getgid", "getpgid", "getpgrp", "getppid", "getuid", "glob", "group", "group_alive",
        "hexdigest", "ignore_patterns", "image_id", "index", "is_dir", "is_symlink", "isnan", "items",
        "iterdir", "join", "killpg", "lgamma", "load", "loads", "log", "matches", "mkdir", "now", "open",
        "parse_args", "pause", "pop", "pull", "randrange", "read", "read_table", "read_text", "readlink",
        "remove_image", "require_digest", "resolve", "run", "runner", "scope_total_m", "search",
        "set_defaults", "setdefault", "sha256", "signal_group", "sleep", "sort", "split", "splitlines",
        "sqrt", "start", "startswith", "strftime", "strip", "surface_list", "surface_reap_close", "symlink",
        "to_json", "to_pylist", "total_m", "update", "urlopen", "values", "wait", "walk", "write",
        "write_text", "wtu", "started", "state", "run_tests", "keys",
        "exec_module", "module_from_spec", "replace", "spec_from_file_location", "with_name",
        "build_grader", "liveness_ports"}
    extra = names - allowed
    assert not extra, sorted(extra)


# --------------------------------------------------------------------------- odd inputs fail closed


def test_regular_reader_refuses_fifo_directory_symlink_oversize_and_extra_links(work, monkeypatch):
    fifo = work / "q.jsonl"
    os.mkfifo(fifo)
    for bad in (fifo, work):
        with pytest.raises(b1.Bench1Error):
            b1.read_regular_bytes(bad)
    real = work / "real.txt"
    real.write_text("data", encoding="utf-8")
    link = work / "link.txt"
    os.symlink(real, link)
    with pytest.raises(b1.Bench1Error):
        b1.read_regular_bytes(link)
    assert b1.read_regular_bytes(real) == b"data"
    hard = work / "hard.txt"
    os.link(real, hard)
    assert b1.read_regular_bytes(hard) == b"data"
    with pytest.raises(b1.Bench1Error, match="hard link"):
        b1.read_regular_bytes(hard, single_link=True)
    monkeypatch.setattr(b1, "READ_LIMIT", 3)
    with pytest.raises(b1.Bench1Error, match="read limit"):
        b1.read_regular_bytes(real)
    with pytest.raises(b1.Bench1Error):
        b1.read_regular_bytes(work / "absent.txt")


def test_records_the_tool_reads_refuse_fifos_and_symlinks(work, patterns):
    fifo = work / "t.jsonl"
    os.mkfifo(fifo)
    with pytest.raises(b1.Bench1Error):
        b1.scan_transcripts([fifo], sealed_tokens=["s"], test_ids=[], public_text="p", repo_slug="a/b")
    pf = work / "patterns.txt"
    os.mkfifo(pf)
    with pytest.raises(b1.Bench1Error):
        b1.WiringPatterns.load(pf)
    res = work / "res"
    res.mkdir()
    os.mkfifo(res / "x.cell.json")
    with pytest.raises(b1.Bench1Error):
        b1.load_cells(res)
    led = work / "ledger.jsonl"
    os.mkfifo(led)
    with pytest.raises(b1.Bench1Error):
        b1.BudgetLedger(led).total_m("0")
    real = work / "real-ledger.jsonl"
    real.write_text("", encoding="utf-8")
    link = work / "link-ledger.jsonl"
    os.symlink(real, link)
    with pytest.raises(b1.Bench1Error):
        b1.BudgetLedger(link).total_m("0")
    for loader, name in ((b1.Freeze.load, "f.json"), (b1.StageApprovals.load, "a.json")):
        p = work / name
        os.mkfifo(p)
        with pytest.raises(b1.Bench1Error):
            loader(p)


def test_default_ceilings_are_the_registered_ones():
    assert b1.KILL_GRACE_S == 30
    assert inspect.signature(b1.KillPath.__init__).parameters["grace_s"].default == 30
    assert inspect.signature(b1.bootstrap_interval).parameters["resamples"].default == 10000
    assert inspect.signature(b1.bootstrap_interval).parameters["seed"].default == 20261014
    assert inspect.signature(b1.bootstrap_interval).parameters["level"].default == 0.90
    assert b1.ALPHA == 0.05 and b1.DELTA == 0.10 and b1.NI_GRID == 161


# --------------------------------------------------------------------------- statistics


def test_mcnemar_exact_values():
    assert b1.mcnemar_one_sided_p(5, 0) == pytest.approx(1 / 32)
    assert b1.mcnemar_one_sided_p(0, 0) == 1.0
    assert b1.mcnemar_one_sided_p(3, 3) == pytest.approx(42 / 64)
    assert b1.mcnemar_one_sided_p(0, 4) == 1.0
    with pytest.raises(b1.Bench1Error):
        b1.mcnemar_one_sided_p(-1, 2)


def test_ni_cutoffs_equal_the_stored_power_script_values():
    # `power2_output.txt` of the BENCH1 strand (grid 161): n=12 (1.948, -2.48), n=24 (2.101, -2.478).
    t12 = b1.ni_cutoffs(12)
    t24 = b1.ni_cutoffs(24)
    assert t12[0] == pytest.approx(1.948, abs=2e-3) and t12[1] == pytest.approx(-2.48, abs=2e-3)
    assert t24[0] == pytest.approx(2.101, abs=2e-3) and t24[1] == pytest.approx(-2.478, abs=2e-3)


def test_ni_state_extremes():
    assert b1.ni_state(0, 0, 24) == "INDETERMINATE"
    assert b1.ni_state(0, 12, 24) == "NI_ESTABLISHED"
    assert b1.ni_state(10, 0, 24) == "INFERIOR"
    assert b1.ni_state(2, 2, 24) == "INDETERMINATE"


def test_score_z_is_zero_at_the_margin():
    assert b1.score_z(3, 0, 30) == pytest.approx(0.0, abs=1e-9)


def _pair(a_cls, b_cls, i):
    return _cell(f"t{i}", a_cls, arm="A1P"), _cell(f"t{i}", b_cls, arm="B")


def test_paired_report_counts_and_drops_non_outcomes_unless_itt():
    rows = ([("FALSE_ACCEPT", "ACCEPT_CORRECT")] * 4 + [("ACCEPT_CORRECT", "ACCEPT_CORRECT")] * 6
            + [("ACCEPT_CORRECT", "FALSE_ACCEPT")] * 1 + [("ACCEPT_CORRECT", "TRUNCATED")] * 2)
    a_cells, b_cells = zip(*[_pair(a, b, i) for i, (a, b) in enumerate(rows)], strict=True)
    r = b1.paired_report(list(a_cells), list(b_cells))
    assert r["n"] == 11 and len(r["dropped_tasks"]) == 2
    assert (r["h1_b_only"], r["h1_c_only"]) == (4, 1)
    assert r["h1_p"] == pytest.approx(b1.mcnemar_one_sided_p(4, 1))
    assert r["h1r_p"] == pytest.approx(b1.mcnemar_one_sided_p(1, 4))
    assert r["fa_reference"] == 4 and r["fa_other"] == 1
    itt = b1.paired_report(list(a_cells), list(b_cells), itt=True)
    assert itt["n"] == 13 and itt["itt"] is True
    assert itt["y_other"] == r["y_other"]                # a non-outcome is never a yield
    assert itt["h2_u"] == r["h2_u"] + 2                  # the two truncated B cells lose A1+'s yield


def test_itt_scores_a_non_outcome_as_no_claim_never_as_a_false_accept():
    a = [_cell("t0", "ACCEPT_CORRECT", True, arm="A1P")]
    b = [_cell("t0", "INSTRUMENT_FAILURE", arm="B")]
    r = b1.paired_report(a, b, itt=True)
    assert r["fa_other"] == 0 and r["y_other"] == 0 and r["h1_c_only"] == 0
    r_drop = b1.paired_report(a, b)
    assert r_drop["n"] == 0 and r_drop["dropped_tasks"] == ["t0"]


def test_bootstrap_interval_is_deterministic_empty_safe_and_a_90_percent_interval():
    d = [1, 0, 0, -1, 1, 0, 0, 0]
    assert b1.bootstrap_interval(d, resamples=500) == b1.bootstrap_interval(d, resamples=500)
    lo, hi = b1.bootstrap_interval(d, resamples=500)
    assert -1 <= lo <= hi <= 1
    assert b1.bootstrap_interval([]) is None
    assert b1.bootstrap_interval([1] * 6) == (1.0, 1.0) and b1.bootstrap_interval([0] * 6) == (0.0, 0.0)
    mixed = [1, 0] * 8
    lo90, hi90 = b1.bootstrap_interval(mixed, resamples=2000, level=0.90)
    lo50, hi50 = b1.bootstrap_interval(mixed, resamples=2000, level=0.50)
    assert lo90 < 0.5 < hi90 and (hi50 - lo50) < (hi90 - lo90) and lo90 <= lo50 and hi50 <= hi90


def test_attrition_flag_is_three_cells():
    a = [_cell(f"t{i}", "ACCEPT_CORRECT", True, arm="A1P") for i in range(6)]
    b = [_cell(f"t{i}", "TRUNCATED", arm="B") for i in range(3)] + \
        [_cell(f"t{i}", "ACCEPT_CORRECT", True, arm="B") for i in range(3, 6)]
    assert b1.attrition_flag({"A1P": a, "B": b}, "A1P", "B")["differential"] is True
    b2 = [_cell(f"t{i}", "TRUNCATED", arm="B") for i in range(2)] + \
        [_cell(f"t{i}", "ACCEPT_CORRECT", True, arm="B") for i in range(2, 6)]
    assert b1.attrition_flag({"A1P": a, "B": b2}, "A1P", "B")["differential"] is False


# --------------------------------------------------------------------------- entry gate


def _r(f2p, p2p=0):
    return {"f2p_failed": f2p, "p2p_failed": p2p}


def test_entry_gate_holds_only_with_twice_on_both_paths_agreeing():
    ok = b1.entry_gate([_r(0), _r(0)], [_r(0), _r(0)], [_r(2), _r(2)], [_r(1), _r(2)])
    assert ok["holds"] is True
    assert b1.entry_gate([_r(0)], [_r(0), _r(0)], [_r(2), _r(2)], [_r(2), _r(2)])["holds"] is False
    assert b1.entry_gate([_r(0), _r(1)], [_r(0), _r(0)], [_r(2), _r(2)], [_r(2), _r(2)])["holds"] is False
    assert b1.entry_gate([_r(0), _r(0)], [_r(0), _r(0)], [_r(0), _r(0)], [_r(0), _r(0)])["holds"] is False


def test_entry_gate_gold_must_pass_p2p_too_on_both_paths():
    g = b1.entry_gate([_r(0, 2), _r(0, 2)], [_r(0, 2), _r(0, 2)], [_r(1), _r(1)], [_r(1), _r(1)])
    assert g["holds"] is False and any("gold" in r for r in g["reasons"])
    g = b1.entry_gate([_r(0), _r(0)], [_r(0, 3), _r(0, 3)], [_r(1), _r(1)], [_r(1), _r(1)])
    assert g["holds"] is False


def test_entry_gate_refuses_paths_that_fail_differently_on_the_base():
    # both paths see an F2P failure, but only the official grader also sees P2P tests fail
    g = b1.entry_gate([_r(0), _r(0)], [_r(0), _r(0)], [_r(1, 0), _r(1, 0)], [_r(1, 3), _r(1, 3)])
    assert g["holds"] is False and any("disagree" in r for r in g["reasons"])


def test_entry_gate_refuses_disagreeing_paths():
    g = b1.entry_gate([_r(0), _r(0)], [_r(0), _r(0)], [_r(1), _r(1)], [_r(0, 0), _r(0, 0)])
    assert g["holds"] is False


# --------------------------------------------------------------------------- report and command line


def test_report_reads_final_cell_files_only(work):
    res = work / "res"
    res.mkdir()
    for i, (a, bcl) in enumerate([("FALSE_ACCEPT", "ACCEPT_CORRECT"), ("ACCEPT_CORRECT", "ACCEPT_CORRECT")]):
        for arm, cls in (("A1P", a), ("B", bcl)):
            doc = {"schema": b1.SCHEMA_CELL, "task": f"t{i}", "arm": arm, "rep": 1, "attempt": 1, "class": cls,
                   "passed": cls == "ACCEPT_CORRECT", "stage": "1a", "key": f"t{i}.{arm}.1.a1",
                   "usage": {"wtu": 1000.0}}
            b1.write_create_only(res / f"t{i}.{arm}.1.a1.cell.json", doc)
    b1.write_create_only(res / "t9.B.1.a1.run.json", {"schema": b1.SCHEMA_RUN})   # a run record is not a cell
    rep = b1.report(res)
    assert rep["arms"]["B"]["cells"] == 2 and rep["arms"]["A1P"]["classes"]["FALSE_ACCEPT"] == 1
    assert rep["paired_A1P_vs_B"]["h1_b_only"] == 1 and rep["attrition"]["differential"] is False
    assert "EXPLORATORY" in rep["label"]


def test_report_refuses_a_cell_file_that_is_not_final(work):
    res = work / "res"
    res.mkdir()
    (res / "x.cell.json").write_text(json.dumps({"schema": "other"}), encoding="utf-8")
    with pytest.raises(b1.Bench1Error):
        b1.report(res)


def test_main_refuses_with_exit_2_and_names_the_rule(work, capsys):
    res = work / "res"
    res.mkdir()
    (res / "x.cell.json").write_text("[]", encoding="utf-8")
    assert b1.main(["report", "--results", str(res)]) == 2
    assert "refused" in capsys.readouterr().err


@pytest.mark.parametrize("cmd", ["report", "gate"])
def test_main_corrupt_json_and_fifos_are_a_refusal_not_a_traceback_or_a_hang(work, capsys, cmd):
    res = work / "res"
    res.mkdir()
    (res / "x.cell.json").write_text("{not json", encoding="utf-8")
    assert b1.main([cmd, "--results", str(res)]) == 2
    fifo_dir = work / "res2"
    fifo_dir.mkdir()
    os.mkfifo(fifo_dir / "y.cell.json")
    assert b1.main([cmd, "--results", str(fifo_dir)]) == 2
    assert capsys.readouterr().err.count("refused:") == 2


def test_main_gate_check_and_check_patterns_fail_closed(work, capsys):
    f = work / "g.json"
    os.mkfifo(f)
    assert b1.main(["gate-check", "--results-file", str(f)]) == 2
    g = work / "g2.json"
    g.write_text("{broken", encoding="utf-8")
    assert b1.main(["gate-check", "--results-file", str(g)]) == 2
    assert b1.main(["check-patterns", "--file", str(PATTERNS_FILE)]) == 0
    assert b1.main(["check-patterns", "--file", str(PATTERNS_FILE), "--frozen", "0" * 64]) == 2
    capsys.readouterr()


def test_main_gate_check_exit_codes(work, capsys):
    results = work / "gate.json"
    results.write_text(json.dumps({"gold_arms": [_r(0), _r(0)], "gold_official": [_r(0), _r(0)],
                                   "base_arms": [_r(1), _r(1)], "base_official": [_r(1), _r(1)]}),
                       encoding="utf-8")
    assert b1.main(["gate-check", "--results-file", str(results)]) == 0
    assert b1.main(["validate", "--results-file", str(results)]) == 0
    results.write_text(json.dumps({"gold_arms": [], "gold_official": [], "base_arms": [],
                                   "base_official": []}), encoding="utf-8")
    assert b1.main(["gate-check", "--results-file", str(results)]) == 1
    capsys.readouterr()


@pytest.mark.parametrize("name", ["prepare", "run-cell", "regrade", "arm-worker"])
def test_bound_subcommands_demand_their_arguments(name, capsys):
    with pytest.raises(SystemExit) as err:
        b1.main([name])
    assert err.value.code == 2
    assert "required" in capsys.readouterr().err


def test_the_ports_module_is_loaded_by_path_and_shares_the_tools_classes():
    mod = b1._ports_module()
    assert mod.b1 is sys.modules["bench1"]
    for name in ("prepare", "run_cell", "regrade", "arm_worker"):
        assert callable(getattr(mod, "main_" + name))
    assert mod.b1.Bench1Error is b1.Bench1Error


def test_prepare_refuses_a_missing_freeze_with_exit_2_and_a_rule(work, capsys):
    rc = b1.main(["prepare", "--campaign-root", str(work), "--stage", "T", "--step", "manifest",
                  "--freeze", str(work / "no.json"), "--sample", str(work / "s.json")])
    assert rc == 2
    assert "refused:" in capsys.readouterr().err


def test_release_images_subcommand_demands_a_chain(capsys):
    with pytest.raises(SystemExit):
        b1.main(["release-images", "--manifest", "m", "--receipts", "r"])
    capsys.readouterr()


def test_grade_subcommand_demands_every_root_and_sealed_tokens(capsys):
    with pytest.raises(SystemExit):
        b1.main(["grade", "--results", "r", "--scratch-root", "s", "--patterns", "p"])
    capsys.readouterr()


# --------------------------------------------------------------------------- minor fold (review A, round 2)


def test_grade_cell_is_bound_to_the_pattern_list_the_cell_ran_under(work, patterns):
    res = work / "res"
    res.mkdir()
    hidden = b1.HiddenRows(FakeDataset(), work, list)
    for digest in ("0" * 64, None, ""):
        run = _run_doc(work)
        if digest is None:
            del run["wiring_patterns_digest"]
        else:
            run["wiring_patterns_digest"] = digest
        with pytest.raises(b1.Bench1Error, match="another wiring pattern list"):
            b1.grade_cell(run, hidden, FakeGrader(), patterns, res, sealed_tokens=["s"])
    assert list(res.glob("*.cell.json")) == []


@pytest.mark.parametrize("marker", [{"pgid": 4242}, {"spend_assumed": True}, {"kill_steps": [{"step": "x"}]}])
def test_a_started_cell_without_a_listed_transcript_is_a_named_non_outcome(work, patterns, marker):
    run = _run_doc(work, transcripts=False, dispatches=0, usage=False, patch=True)
    run.update(marker)
    cell, grader, _ = _grade(work, run, patterns)
    assert cell["class"] == "INSTRUMENT_FAILURE" and cell["scanned"] is False
    assert cell["scan_gap"] == "a started cell lists no transcript" and grader.calls == 0


def test_a_truncated_cell_without_a_transcript_keeps_its_class_and_names_the_gap(work, patterns):
    run = _run_doc(work, ended="truncated", transcripts=False, dispatches=0, usage=False, patch=False)
    run["pgid"] = 4242
    cell, grader, _ = _grade(work, run, patterns)
    assert cell["class"] == "TRUNCATED" and cell["scan_gap"] and cell["scanned"] is False
    assert grader.calls == 0


def test_a_cell_that_never_started_needs_no_transcript(work, patterns):
    run = _run_doc(work, ended="instrument_failure", transcripts=False, dispatches=0, usage=False, patch=False)
    cell, _, _ = _grade(work, run, patterns)
    assert cell["class"] == "INSTRUMENT_FAILURE" and cell["scan_gap"] is None


def _inspect_runner(returncode, stdout="", stderr=""):
    return lambda argv, **kw: subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def test_only_a_plain_no_such_image_means_absent():
    ok = b1.SubprocessDocker(runner=_inspect_runner(0, IMG1 + "\n"))
    assert ok.image_id("img/a:1") == IMG1
    absent = b1.SubprocessDocker(runner=_inspect_runner(1, "", "Error: No such image: img/a:1"))
    assert absent.image_id("img/a:1") is None
    for runner in (_inspect_runner(1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"),
                   _inspect_runner(1, "", "permission denied while trying to connect"),
                   _inspect_runner(125, "", ""),
                   _inspect_runner(0, "not-an-id\n", ""),
                   _inspect_runner(0, "", "")):
        with pytest.raises(b1.ImageStateUnknown):
            b1.SubprocessDocker(runner=runner).image_id("img/a:1")


class UnknownDocker(FakeDocker):
    def image_id(self, ref):
        raise b1.ImageStateUnknown("daemon unreachable")


def test_an_unknown_image_state_counts_as_preexisting_never_pulled_never_released(work):
    m, rec, chain = _setup(work)
    d = UnknownDocker()
    r = b1.pull_one(d, m, "img/a:1", rec, chain)
    assert d.pulled == [] and r["preexisting"] is True and r["ours"] is False
    assert r["state_unknown"] is True and r["image_id"] is None
    with pytest.raises(b1.ManifestRefused, match="not ours"):
        b1.release_images(d, m, rec, chain, refs=["img/a:1"], confirm=True)
    assert b1.release_images(d, m, rec, chain, confirm=True) == [] and d.removed == []


def test_release_refuses_when_the_runtime_cannot_say_whether_the_image_exists(work):
    m, rec, chain = _setup(work)
    d = FakeDocker()
    b1.pull_one(d, m, "img/a:1", rec, chain)
    d2 = UnknownDocker()
    with pytest.raises(b1.ManifestRefused, match="cannot say"):
        b1.release_images(d2, m, rec, chain, confirm=True)
    assert d2.removed == [] and d.images.get("img/a:1") == IMG1


def test_grade_subcommand_requires_the_frozen_pattern_digest(capsys):
    with pytest.raises(SystemExit):
        b1.main(["grade", "--results", "r", "--scratch-root", "s", "--patterns", "p",
                 "--sealed-token", "t", "--hoh-root", "h"])
    assert "--frozen-patterns" in capsys.readouterr().err


def test_a_product_stop_of_a_started_cell_without_a_transcript_is_not_kept_as_an_outcome(work, patterns):
    run = _run_doc(work, ended="blocked", reason="criteria red three times", transcripts=False,
                   dispatches=0, usage=False, patch=True)
    run["pgid"] = 4242
    cell, grader, _ = _grade(work, run, patterns)
    assert cell["class"] == "INSTRUMENT_FAILURE" and cell["scan_gap"] and grader.calls == 0


# --------------------------------------------------------------------------- deferred checks, post-start, release (ports slice)


def test_deferred_checks_are_skipped_before_the_start_and_enforced_after(freeze):
    decl = b1.registered_declaration("B", freeze)
    obs = _good("B", freeze)
    del obs["session_open"]
    obs["deferred_checks"] = ["session_open"]
    assert b1.compare_wiring(decl, obs, "B") == []
    # the post-start pass looks only at the deferred keys, and a missing one is a finding
    assert b1.compare_wiring(decl, obs, "B", only=frozenset({"session_open"}))[0]["check"] == "I13"
    obs["session_open"] = True
    assert b1.compare_wiring(decl, obs, "B", only=frozenset({"session_open"})) == []
    obs["session_open"] = False
    assert b1.compare_wiring(decl, obs, "B", only=frozenset({"session_open"}))[0]["what"] == "session_open"


def test_a_deferred_key_the_tool_does_not_know_is_a_finding(freeze):
    obs = _good("B", freeze)
    obs["deferred_checks"] = ["policy_digest", "colour_of_the_pane"]
    found = b1.compare_wiring(b1.registered_declaration("B", freeze), obs, "B")
    assert any("colour_of_the_pane" in f["what"] for f in found)


def test_not_applicable_is_allowed_for_a_arms_only_and_only_for_two_checks(freeze):
    decl = b1.registered_declaration("A1P", freeze)
    obs = _good("A1P", freeze)
    del obs["project_resolved"]
    del obs["session_open"]
    obs["not_applicable"] = ["project_resolved", "session_open"]
    assert b1.compare_wiring(decl, obs, "A1P") == []
    obs["not_applicable"] = ["project_resolved", "policy_digest"]
    assert any("not_applicable" in f["what"] for f in b1.compare_wiring(decl, obs, "A1P"))
    declb = b1.registered_declaration("B", freeze)
    obsb = _good("B", freeze)
    obsb["not_applicable"] = ["session_open"]
    assert any("not_applicable" in f["what"] for f in b1.compare_wiring(declb, obsb, "B"))


class PostStartPorts(FakePorts):
    def __init__(self, observed, post=None, post_error=None, release_error=None, **kw):
        super().__init__(observed, **kw)
        self.post, self.post_error, self.release_error = post or {}, post_error, release_error
        self.released = 0

    def post_start_readback(self, spec, handle):
        if self.post_error:
            raise self.post_error
        return self.post

    def release(self, spec):
        if self.release_error:
            raise self.release_error
        self.released += 1


def _deferred_obs(freeze, **kw):
    obs = _good("B", freeze)
    del obs["session_open"]
    obs["deferred_checks"] = ["session_open"]
    obs.update(kw)
    return obs


def test_run_cell_deferred_checks_without_a_post_start_reader_stop_before_the_start(
        work, freeze, approvals, patterns):
    ports = FakePorts(_deferred_obs(freeze))
    doc, led = _run_cell(work, ports, freeze, approvals, patterns)
    assert ports.started == 0 and doc["ended"] == "instrument_failure"
    assert any("no post-start read-back" in f["what"] for f in doc["wiring_findings"])
    assert led.total_m("0") == 0.0


def test_run_cell_post_start_pass_is_recorded_and_the_arm_is_released(work, freeze, approvals, patterns):
    ports = PostStartPorts(_deferred_obs(freeze), post={"session_open": True, "provider_calls_before_check": 0},
                           collected={"ended": "claim", "claimed_tree": "abc", "transcript_lines": _transcript(),
                                      "patch_path": "p"})
    doc, led = _run_cell(work, ports, freeze, approvals, patterns)
    assert doc["ended"] == "claim" and ports.released == 1
    assert doc["wiring_post_start"]["session_open"] is True and doc["wiring"]["deferred_checks"] == ["session_open"]
    assert led.total_m("0") == pytest.approx(150 / 1e6)


def test_run_cell_post_start_mismatch_kills_and_costs_nothing_when_no_call_was_made(
        work, freeze, approvals, patterns):
    ports = PostStartPorts(_deferred_obs(freeze), post={"session_open": False, "provider_calls_before_check": 0},
                           collected={"transcript_lines": [], "dispatches": 0}, procs=FakeProcs(stubborn=True))
    doc, led = _run_cell(work, ports, freeze, approvals, patterns, kill=_kill(ports))
    assert doc["ended"] == "instrument_failure" and "post-start" in doc["stop_reason"]
    assert doc["wiring_findings"][0]["what"] == "session_open"
    assert (5151, signal.SIGKILL) in ports.procs.signals and ports.released == 0
    assert doc["spend_wtu"] == 0.0 and led.total_m("0") == 0.0


def test_run_cell_post_start_mismatch_with_calls_made_is_charged_measured_or_maximum(
        work, freeze, approvals, patterns):
    ports = PostStartPorts(_deferred_obs(freeze), post={"session_open": False, "provider_calls_before_check": 2},
                           collected={"transcript_lines": ["garbage"]})
    doc, led = _run_cell(work, ports, freeze, approvals, patterns, kill=_kill(ports))
    assert doc["ended"] == "instrument_failure" and led.total_m("0") == pytest.approx(10.8)


@pytest.mark.parametrize("post_calls,collected", [
    (None, {"transcript_lines": [], "dispatches": 0}),        # the reader did not count: not "no call was made"
    (0, {"transcript_lines": []}),                            # the count after the kill is unknown
    (0, {"transcript_lines": [], "dispatches": 1}),           # a dispatch happened between the read and the kill
])
def test_run_cell_a_zero_charge_needs_both_counts_to_say_zero(work, freeze, approvals, patterns, post_calls,
                                                              collected):
    ports = PostStartPorts(_deferred_obs(freeze), post={"session_open": False,
                                                        "provider_calls_before_check": post_calls},
                           collected=collected)
    doc, led = _run_cell(work, ports, freeze, approvals, patterns, kill=_kill(ports))
    assert doc["ended"] == "instrument_failure" and doc["spend_assumed"] is True
    assert led.total_m("0") == pytest.approx(10.8)


def test_run_cell_a_failing_post_start_reader_fails_closed(work, freeze, approvals, patterns):
    ports = PostStartPorts(_deferred_obs(freeze), post_error=OSError("no record"),
                           collected={"transcript_lines": []})
    doc, _ = _run_cell(work, ports, freeze, approvals, patterns, kill=_kill(ports))
    assert doc["ended"] == "instrument_failure" and "read-back failed" in doc["wiring_findings"][0]["what"]


def test_run_cell_a_failed_release_kills_and_makes_the_worker_dispatch_nothing(
        work, freeze, approvals, patterns):
    ports = PostStartPorts(_deferred_obs(freeze), post={"session_open": True},
                           release_error=OSError("cannot create the go file"), procs=FakeProcs(stubborn=True))
    doc, led = _run_cell(work, ports, freeze, approvals, patterns, kill=_kill(ports))
    assert doc["ended"] == "instrument_failure" and "release failed" in doc["stop_reason"]
    assert (5151, signal.SIGKILL) in ports.procs.signals and led.total_m("0") == 0.0


class QuietStartError(Exception):
    no_provider_call = True


def test_run_cell_a_start_aborted_by_the_port_before_any_call_costs_nothing(work, freeze, approvals, patterns):
    ports = FakePorts(_good("B", freeze), start_error=QuietStartError("handshake refused"))
    doc, led = _run_cell(work, ports, freeze, approvals, patterns)
    assert doc["ended"] == "instrument_failure" and doc["spend_assumed"] is False and led.total_m("0") == 0.0


# --------------------------------------------------------------------------- ports seams, revision 1


class TruthyQuietStartError(Exception):
    no_provider_call = 1                       # truthy, but not `True`: nothing declared it a free abort


def test_run_cell_only_a_declared_true_no_provider_call_makes_a_failed_start_free(work, freeze, approvals,
                                                                                    patterns):
    ports = FakePorts(_good("B", freeze), start_error=TruthyQuietStartError("x"))
    doc, led = _run_cell(work, ports, freeze, approvals, patterns)
    assert doc["ended"] == "instrument_failure" and doc["spend_assumed"] is True
    assert led.total_m("0") == pytest.approx(10.8)


def test_not_applicable_is_refused_for_arm_c_too(freeze):
    decl = b1.registered_declaration("C", freeze)
    obs = _good("C", freeze)
    obs["not_applicable"] = ["session_open"]
    assert any("not_applicable" in f["what"] for f in b1.compare_wiring(decl, obs, "C"))


def test_the_not_applicable_set_and_the_read_back_keys_are_exactly_the_registered_ones():
    assert b1.NOT_APPLICABLE_FOR_A == frozenset({"project_resolved", "session_open"})
    assert b1.WIRING_KEYS == frozenset({
        "models", "efforts", "role_timeout_s", "dispatch_cap", "isolation", "policy_digest", "house_text_digest",
        "denied_tools", "trust_recorded", "storage_root_initialised", "project_resolved", "session_open",
        "scratch_under_storage_root", "storage_root", "herdr_env", "hidden_material_in_worktree", "snapshot"})


def test_each_bound_subcommand_reaches_its_own_main(monkeypatch, work):
    seen = []

    class FakePorts:
        pass
    for name in ("prepare", "run_cell", "regrade", "arm_worker"):
        setattr(FakePorts, "main_" + name, staticmethod(lambda args, tool, _n=name: seen.append(_n) or 0))
    monkeypatch.setattr(b1, "_ports_module", lambda: FakePorts)
    plans = {"prepare": ["--campaign-root", "c", "--stage", "T", "--step", "manifest", "--freeze", "f",
                         "--sample", "s"],
             "run-cell": ["--campaign-root", "c", "--stage", "0", "--task", "t", "--arm", "B", "--freeze", "f",
                          "--approval", "a", "--ledger", "l", "--patterns", "p", "--results", "r",
                          "--sample", "s"],
             "regrade": ["--results", "r", "--key", "k", "--scratch-root", "x", "--hoh-root", "h"],
             "arm-worker": ["--plan", "p"]}
    for sub, argv in plans.items():
        assert b1.main([sub, *argv]) == 0
    assert seen == ["prepare", "run_cell", "regrade", "arm_worker"]


def test_arm_c_is_not_a_choice_of_run_cell(capsys):
    with pytest.raises(SystemExit) as exc:
        b1.main(["run-cell", "--campaign-root", "c", "--stage", "0", "--task", "t", "--arm", "C",
                 "--freeze", "f", "--approval", "a", "--ledger", "l", "--patterns", "p", "--results", "r",
                 "--sample", "s"])
    assert exc.value.code == 2 and "invalid choice" in capsys.readouterr().err


def test_script_mode_shares_one_set_of_classes_with_the_ports(work):
    """Run as a script (`__main__`), a refusal raised by the ports must still be the tool's own `Bench1Error`:
    `refused:` and exit 2, not a traceback (the `setdefault("bench1", ...)` registration)."""
    done = subprocess.run(
        [sys.executable, "-I", "-B", str(TOOL), "prepare", "--campaign-root", str(work), "--stage", "T",
         "--step", "manifest", "--freeze", str(work / "no.json"), "--sample", str(work / "s.json")],
        capture_output=True, text=True, timeout=120, cwd=str(work), check=False)
    assert done.returncode == 2 and "refused:" in done.stderr and "Traceback" not in done.stderr


def test_grade_takes_its_instruments_from_the_ports_module(monkeypatch, work):
    """`grade` asks the liveness of the dispatchers' registry too and grades with the one pinned instrument."""
    hoh_root = work / "hoh"
    hoh_root.mkdir()
    results = work / "res"
    results.mkdir()
    (results / "k.run.json").write_text(json.dumps({"hoh_root": str(hoh_root)}), encoding="utf-8")
    got = {}

    class Mod:
        @staticmethod
        def liveness_ports(ports):
            got["ports"] = list(ports)

            class Registry:
                @staticmethod
                def surface_list():
                    return json.dumps({"surfaces": [{"state": "owned", "run_id": "r", "pane_id": "p"}]})
            return [Registry]

        @staticmethod
        def build_grader(agent_roots=()):
            got["roots"] = list(agent_roots)
            return "THE-GRADER"
    monkeypatch.setattr(b1, "_ports_module", lambda: Mod)
    monkeypatch.setattr(b1, "grade_cell", lambda run, hidden, grader, patterns, results, sealed_tokens:
                        got.update(grader=grader, hidden=hidden))
    args = SimpleNamespace(results=str(results), patterns=str(PATTERNS_FILE), frozen_patterns="",
                           hoh_root=[str(hoh_root)], scratch_root=str(work / "hid"), sealed_token=["t"])
    monkeypatch.setattr(b1.WiringPatterns, "require_digest", lambda self, d: None)
    assert b1._main_grade(args) == 0
    assert got["grader"] == "THE-GRADER" and got["roots"] == [hoh_root]
    assert isinstance(got["ports"][0], b1.HohCli)
    assert got["hidden"].agents_alive() == ["r:p"]                    # the ports module's ports are the ones asked
