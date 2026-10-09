"""Three tightenings of the durability record's derivation (tools/durability_e2e.py).

1. P2-33, the plan's umbrella line "unattended test:", holds only when its own
   scenarios pass *and* the four lines under it (P2-34..P2-37) are satisfied.
   Each dependency is shown on its own: P2-33 is false when only P2-34, only
   P2-35 or only P2-37 is unsatisfied (P2-36 shares P2-33's own scenarios). An
   umbrella's own scenarios are shown still required on a synthetic table in
   which the umbrella needs a scenario no dependency needs -- with the real
   table, P2-35 and P2-36 would mask a derivation that ignored them.
2. derive_orphaned_run checks that the process marker preserved in the
   `while-in-flight` moment names the orphaned child, running. The control
   `marker-names-other-pid` makes the product write a marker naming another
   pid; the marker it produces is shown to turn that check false while the
   scenario's other checks hold.
3. _harness_initiated, the record's list of everything the harness did itself
   (read for "no human attached"), also names every tick start, every SIGKILL
   step (cleanup and cleanup-child included), every injected crash and every
   signal recorded as a `signal-<name>` step, in step order, beside what it
   listed before. The quota-crash record concatenates its three cases' lists
   in case order, each entry prefixed with its case -- nothing sorted, nothing
   deduplicated.

The derived record states RECORD_VERSION 6 and lists the new control.

Plain unittest on purpose: the file runs under pytest and under
`python3 -m unittest`, needs neither pytest nor pydantic, and never starts the
harness (`run`) or any process -- evidence is synthetic and minimal.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("durability_e2e_probe",
                                               ROOT / "tools" / "durability_e2e.py")
dur = importlib.util.module_from_spec(_spec)
sys.modules["durability_e2e_probe"] = dur
_spec.loader.exec_module(dur)

CONTENT = ["P2-34", "P2-35", "P2-36", "P2-37"]
CLEAN_META = {"dirty": False, "dirty_at_end": False}


def _passing(name: str) -> dict:
    return {"status": "PASS", "passed": True, "checks": {f"{name}_ok": True},
            "problems": {"exactly_once": [], "continuity": [], "no_human": []}}


def _all_passing() -> dict:
    return {n: _passing(n) for n in dur.SCENARIOS}


def _evaluate(scenarios: dict) -> dict:
    return dur._evaluate_requirements(scenarios, dict(CLEAN_META), True, [])


class UmbrellaRequirementTest(unittest.TestCase):

    def test_umbrella_satisfied_when_all_five_hold(self):
        reqs = _evaluate(_all_passing())
        for rid in ["P2-33"] + CONTENT:
            self.assertIs(reqs[rid]["satisfied"], True, rid)

    def test_umbrella_not_satisfied_when_only_p2_35_fails(self):
        # rollover is needed by P2-35 alone, not by P2-33's own scenarios, and
        # a FAIL with empty problems leaves P2-34 and P2-37 untouched.
        self.assertNotIn("rollover", dur.REQUIREMENTS["P2-33"]["needs"])
        self.assertNotIn("rollover", dur.REQUIREMENTS["P2-36"]["needs"])
        scenarios = _all_passing()
        scenarios["rollover"] = {**scenarios["rollover"], "status": "FAIL", "passed": False}
        reqs = _evaluate(scenarios)
        self.assertIs(reqs["P2-35"]["satisfied"], False)
        self.assertIs(reqs["P2-33"]["satisfied"], False)
        for rid in ("P2-34", "P2-36", "P2-37"):
            self.assertIs(reqs[rid]["satisfied"], True, rid)

    def test_umbrella_not_satisfied_when_only_p2_34_fails(self):
        # A PASSing scenario with a human decision: P2-34 reads problems.no_human,
        # nothing else does, so every other line and P2-33's own scenarios hold.
        scenarios = _all_passing()
        scenarios["moved-branch"] = {**scenarios["moved-branch"],
                                     "problems": {"exactly_once": [], "continuity": [],
                                                  "no_human": ["decision by a human"]}}
        reqs = _evaluate(scenarios)
        self.assertIs(reqs["P2-34"]["satisfied"], False)
        for rid in ("P2-35", "P2-36", "P2-37"):
            self.assertIs(reqs[rid]["satisfied"], True, rid)
        self.assertIs(reqs["P2-33"]["satisfied"], False)
        # Control: the same rows without the human decision.
        self.assertIs(_evaluate(_all_passing())["P2-33"]["satisfied"], True)

    def test_umbrella_not_satisfied_when_only_p2_37_fails(self):
        # moved-branch is needed by P2-37 via and_pass, not by P2-33's own
        # scenarios, P2-35 or P2-36; a FAIL with empty problems leaves P2-34
        # (no_human) and P2-37's problem half untouched.
        self.assertIn("moved-branch", dur.REQUIREMENTS["P2-37"]["and_pass"])
        for rid in ("P2-33", "P2-35", "P2-36"):
            self.assertNotIn("moved-branch", dur.REQUIREMENTS[rid]["needs"], rid)
        scenarios = _all_passing()
        scenarios["moved-branch"] = {**scenarios["moved-branch"],
                                     "status": "FAIL", "passed": False}
        reqs = _evaluate(scenarios)
        self.assertIs(reqs["P2-37"]["satisfied"], False)
        for rid in ("P2-34", "P2-35", "P2-36"):
            self.assertIs(reqs[rid]["satisfied"], True, rid)
        self.assertIs(reqs["P2-33"]["satisfied"], False)
        # Control: the same rows with moved-branch passing.
        self.assertIs(_evaluate(_all_passing())["P2-33"]["satisfied"], True)

    def test_umbrella_fails_on_a_scenario_only_it_requires(self):
        # The dependency adds conditions, it never replaces the umbrella's own.
        # With the real table P2-33's own scenarios are also P2-35's and
        # P2-36's, so ignoring them would change nothing observable; here the
        # umbrella needs "lapse", which its dependency does not.
        table = {
            "DEP": {"item": "dep", "needs": ["controller-kill"]},
            "UMB": {"item": "umbrella", "needs": ["controller-kill", "lapse"],
                    "and_requirements": ["DEP"]},
        }
        scenarios = _all_passing()
        scenarios["lapse"] = {**scenarios["lapse"], "status": "FAIL", "passed": False}
        with mock.patch.object(dur, "REQUIREMENTS", table):
            reqs = _evaluate(scenarios)
            control = _evaluate(_all_passing())
        self.assertIs(reqs["DEP"]["satisfied"], True)
        self.assertIs(reqs["UMB"]["satisfied"], False)
        self.assertIs(control["DEP"]["satisfied"], True)
        self.assertIs(control["UMB"]["satisfied"], True)

    def test_umbrella_dependency_is_recorded(self):
        spec = dur.REQUIREMENTS["P2-33"]
        self.assertEqual(spec["needs"], ["controller-kill", "quota"])
        self.assertEqual(sorted(spec["and_requirements"]), CONTENT)
        for rid in CONTENT:
            self.assertNotIn("and_requirements", dur.REQUIREMENTS[rid], rid)
        reqs = _evaluate(_all_passing())
        self.assertEqual(sorted(reqs["P2-33"]["and_requirements"]), CONTENT)
        for rid in CONTENT:
            self.assertEqual(reqs[rid]["and_requirements"], [], rid)
        self.assertEqual(list(reqs), list(dur.REQUIREMENTS))

    def test_umbrella_unknown_dependency_fails_closed(self):
        patched = {**dur.REQUIREMENTS,
                   "P2-33": {**dur.REQUIREMENTS["P2-33"],
                             "and_requirements": CONTENT + ["P2-99"]}}
        with mock.patch.object(dur, "REQUIREMENTS", patched):
            reqs = _evaluate(_all_passing())
        self.assertIs(reqs["P2-33"]["satisfied"], False)
        self.assertIs(reqs["P2-34"]["satisfied"], True)


CHILD, MASTER = 4242, 4000


def _write_marker(d: Path, marker) -> None:
    f = (d / "moments" / "while-in-flight" / "projects" / dur.PROJECT / "fixture-runs"
         / "n1" / "process.json")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(marker if isinstance(marker, str) else json.dumps(marker) + "\n")


class OrphanMarkerHelperTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="dur-derive-")
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_orphan_marker_right_pid_running(self):
        _write_marker(self.d, {"pid": CHILD, "state": "running", "since": "x"})
        self.assertIs(dur._marker_names_child(self.d, CHILD), True)

    def test_orphan_marker_wrong_pid(self):
        _write_marker(self.d, {"pid": CHILD + 1, "state": "running", "since": "x"})
        self.assertIs(dur._marker_names_child(self.d, CHILD), False)

    def test_orphan_marker_wrong_state(self):
        _write_marker(self.d, {"pid": CHILD, "state": "ended", "since": "x"})
        self.assertIs(dur._marker_names_child(self.d, CHILD), False)

    def test_orphan_marker_missing_file(self):
        self.assertIs(dur._marker_names_child(self.d, CHILD), False)

    def test_orphan_marker_unparsable_or_no_child_pid(self):
        _write_marker(self.d, "{not json")
        self.assertIs(dur._marker_names_child(self.d, CHILD), False)
        _write_marker(self.d, {"pid": CHILD, "state": "running"})
        self.assertIs(dur._marker_names_child(self.d, None), False)


class OrphanMarkerWiringTest(unittest.TestCase):
    """derive_orphaned_run on synthetic evidence, with the fact readers that
    need a repo bundle or pydantic patched out."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="dur-derive-")
        self.d = Path(self._tmp.name)
        steps = [
            {"kind": "orphan", "child_pid": CHILD, "master_pid": MASTER},
            {"kind": "sigkill", "point": "between-iterations:n1", "pid": MASTER,
             "returncode": -9},
            {"kind": "process-exit", "label": "tick-01", "pid": 5000,
             "output": {"projects": [{"action": "waiting_on_run"}]}},
        ]
        (self.d / "steps.jsonl").write_text("".join(json.dumps(s) + "\n" for s in steps))

    def tearDown(self):
        self._tmp.cleanup()

    def _checks(self) -> dict:
        facts = {"epochs": [(1, "master-1", "INITIAL"), (2, "recover-1", "RECOVERY")]}
        during = {"epochs": [(1, "master-1", "INITIAL")]}
        with mock.patch.object(dur, "_project_facts", return_value=facts), \
                mock.patch.object(dur, "_repo_facts", return_value={}), \
                mock.patch.object(dur, "_moment", return_value=during), \
                mock.patch.object(dur, "_result",
                                  side_effect=lambda st, f, r, checks, *a, **k:
                                  {"checks": checks}):
            return dur.derive_orphaned_run(self.d, self.d / "work")["checks"]

    def test_orphan_marker_wired_right_pid(self):
        _write_marker(self.d, {"pid": CHILD, "state": "running"})
        checks = self._checks()
        self.assertIs(checks["marker_names_the_orphaned_child"], True)
        # The existing checks are still there, and hold on this evidence.
        for k in ("master_killed_while_child_ran", "first_tick_waits_on_the_run",
                  "no_epoch_spent_while_in_flight", "epochs_initial_then_one_recovery"):
            self.assertIs(checks[k], True, k)

    def test_orphan_marker_wired_wrong_pid(self):
        _write_marker(self.d, {"pid": CHILD + 1, "state": "running"})
        self.assertIs(self._checks()["marker_names_the_orphaned_child"], False)

    def test_marker_control_turns_the_check_false(self):
        # (a) The control exists, in the orphaned-run scenario, and targets
        # exactly this check.
        controls = [c for c in dur.CONTROLS
                    if "marker_names_the_orphaned_child" in c["targets"]]
        self.assertEqual(len(controls), 1)
        control = controls[0]
        self.assertEqual(control["scenario"], "orphaned-run")
        self.assertEqual(control["targets"], ["marker_names_the_orphaned_child"])
        self.assertFalse(control.get("allow_stall", False))
        # (b) The marker it makes the child write: running, but naming the
        # child's parent -- the killed master -- instead of the child.
        _write_marker(self.d, {"pid": MASTER, "state": "running", "since": "x"})
        checks = self._checks()
        self.assertIs(checks["marker_names_the_orphaned_child"], False)
        # The scenario still happened as set up: the precondition and the other
        # checks hold, so the control would count as caught.
        for k in ("master_killed_while_child_ran", "first_tick_waits_on_the_run",
                  "no_epoch_spent_while_in_flight", "epochs_initial_then_one_recovery"):
            self.assertIs(checks[k], True, k)
        broken = [k for k, v in checks.items() if not v and k in dur.PRECONDITIONS]
        self.assertEqual(broken, [])


def _start(label: str, argv=None, crash=None) -> dict:
    return {"kind": "process-start", "pid": 1, "label": label,
            "argv": list(argv) if argv is not None else ["hoh", "drive", label],
            "pause": [], "crash": crash}


class HarnessInitiatedTest(unittest.TestCase):
    """_harness_initiated on hand-built steps; the expected lists are exact, so
    a dropped kind, a filtered cleanup kill or new kinds appended at the end
    all fail."""

    def test_harness_initiated_lists_ticks_and_kills(self):
        tick = list(dur.CONTINUE_ARGV)
        steps = [
            _start("drive-master-1"),
            _start("tick-1", argv=tick),
            {"kind": "sigkill", "pid": 1, "point": "kill-1", "returncode": -9},
            {"kind": "released", "point": "P"},
            _start("drive-A", crash="after-arm"),
            _start("", argv=tick),
            {"kind": "sigkill", "pid": 3, "point": "cleanup-child", "returncode": None},
            {"kind": "fixtures", "point": "F"},
            {"kind": "sigkill", "pid": 2, "point": "cleanup", "returncode": None},
        ]
        self.assertEqual(dur._harness_initiated(steps), [
            "process: drive-master-1", "tick: tick-1", "sigkill: kill-1",
            "released: P", "process: drive-A", "crash: after-arm", "tick: 2",
            "sigkill: cleanup-child", "fixtures: F", "sigkill: cleanup",
        ])

    def test_harness_initiated_keeps_existing(self):
        steps = [
            _start("drive-master-1"),
            {"kind": "paused", "point": "x"},
            {"kind": "released", "point": "P"},
            {"kind": "signal-sigstop", "point": "S"},
            {"kind": "process-exit", "label": "drive-master-1", "pid": 1},
            {"kind": "signal-sigcont", "point": "C"},
            {"kind": "signal-sigterm", "point": "T"},
            _start("", argv=["hoh", "drive", "--project", "dur"]),
            {"kind": "branch-moved", "point": "B"},
            {"kind": "signal-sighup", "point": "H"},
            {"kind": "fixtures", "point": "F"},
            {"kind": "fixtures"},
        ]
        # Every recorded signal is listed, not an allowlist of two.
        self.assertEqual(dur._harness_initiated(steps), [
            "process: drive-master-1", "released: P", "signal-sigstop: S",
            "signal-sigcont: C", "signal-sigterm: T", "process: hoh drive",
            "branch-moved: B", "signal-sighup: H", "fixtures: F", "fixtures",
        ])
        # Only what is not a harness act stays unlisted.
        ignored = [{"kind": "paused", "point": "x"},
                   {"kind": "process-exit", "label": "t", "pid": 1}]
        self.assertEqual(dur._harness_initiated(ignored), [])


class _TimerStatus(Enum):
    ARMED = "armed"
    FIRED = "fired"


def _exit(label: str, returncode: int = 0) -> dict:
    return {"kind": "process-exit", "label": label, "pid": 1, "returncode": returncode,
            "output": {"projects": []}}


class QuotaCrashRecordTest(unittest.TestCase):
    """derive_quota_crash on hand-built steps for its three cases, with every
    reader of the disk, the repo bundle or pydantic patched out. The record's
    harness_initiated is the cases' lists concatenated in case order, each
    entry prefixed with its case: a sorted set or an order-preserving
    deduplication both fail."""

    def test_quota_crash_record_keeps_every_case_in_order(self):
        tick = list(dur.CONTINUE_ARGV)
        steps = {
            "before-arm": [
                _start("drive-A", crash="before-arm"), _exit("drive-A", -9),
                _start("tick-01", argv=tick), _exit("tick-01"),
                _start("tick-02", argv=tick), _exit("tick-02"),
            ],
            "after-arm": [
                _start("drive-A", crash="after-arm"), _exit("drive-A", -9),
                _start("tick-01", argv=tick), _exit("tick-01"),
                {"kind": "released", "point": "P"},
                _start("tick-02", argv=tick), _exit("tick-02"),
                _start("tick-03", argv=tick), _exit("tick-03"),
            ],
            "after-park": [
                {"kind": "fixtures", "point": "F"},
                _start("drive-A", crash="after-park"), _exit("drive-A", -9),
                _start("tick-01", argv=tick), _exit("tick-01"),
                {"kind": "fixtures", "point": "F"},
                _start("tick-02", argv=tick), _exit("tick-02"),
            ],
        }
        self.assertEqual(list(steps), list(dur.CRASH_POINTS))
        facts = {"state": types.SimpleNamespace(rc_closed=lambda: True), "timers": []}

        def result(st, f, r, checks, stressors, landed=None, d=None):
            return {"stressors": stressors, "checks": checks,
                    "problems": {"exactly_once": [], "continuity": [], "no_human": []},
                    "harness_initiated": dur._harness_initiated(st), "passed": True}

        timers = types.ModuleType("hoh.timers")
        timers.TimerStatus = _TimerStatus
        with tempfile.TemporaryDirectory(prefix="dur-derive-") as tmp, \
                mock.patch.dict(sys.modules, {"hoh.timers": timers}), \
                mock.patch.object(dur, "_steps", side_effect=lambda c: steps[c.name]), \
                mock.patch.object(dur, "_project_facts", return_value=facts), \
                mock.patch.object(dur, "_repo_facts", return_value={}), \
                mock.patch.object(dur, "_moment", return_value=None), \
                mock.patch.object(dur, "_result", side_effect=result), \
                mock.patch.object(dur.subprocess, "run") as run:
            record = dur.derive_quota_crash(Path(tmp), Path(tmp) / "work")
        run.assert_not_called()
        self.assertEqual(record["harness_initiated"], [
            "before-arm: process: drive-A", "before-arm: crash: before-arm",
            "before-arm: tick: tick-01", "before-arm: tick: tick-02",
            "after-arm: process: drive-A", "after-arm: crash: after-arm",
            "after-arm: tick: tick-01", "after-arm: released: P",
            "after-arm: tick: tick-02", "after-arm: tick: tick-03",
            "after-park: fixtures: F", "after-park: process: drive-A",
            "after-park: crash: after-park", "after-park: tick: tick-01",
            "after-park: fixtures: F", "after-park: tick: tick-02",
        ])
        # Nothing else in the record moved: its stressors and the cases' checks.
        self.assertEqual(record["stressors"], ["DS3-CRASH"])
        self.assertIn("after-arm:orphan_timer_did_not_resume_a_running_node",
                      record["checks"])


class RecordVersionTest(unittest.TestCase):

    def test_record_version_stated_and_new_control_listed(self):
        self.assertEqual(dur.RECORD_VERSION, 6)
        # The only process derive would start is `git show` for the evidence
        # commit's unit; answered here as unreadable, so nothing is started.
        unreadable = mock.Mock(returncode=128, stdout="", stderr="")
        with tempfile.TemporaryDirectory(prefix="dur-derive-") as tmp, \
                mock.patch.object(dur.subprocess, "run", return_value=unreadable) as run:
            (Path(tmp) / "meta.json").write_text(json.dumps(CLEAN_META))
            record = dur.derive(Path(tmp))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(record["record_version"], 6)
        entry = record["negative_controls"]["marker-names-other-pid@orphaned-run"]
        self.assertEqual(entry["targets"], ["marker_names_the_orphaned_child"])
        self.assertEqual(entry["status"], "NOT_RUN")
        self.assertIs(entry["caught"], False)
        self.assertIs(record["instrument_falsifiable"], False)


if __name__ == "__main__":
    unittest.main()
