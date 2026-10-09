"""The reboot half of the durability record (tools/durability_e2e.py): what DS2 may and may not claim.

DS2 is the only scenario an operator has to run by hand around a real restart
(`reboot-arm`, restart, `reboot-verify`). Four things about it are pinned here:

1. `derive_reboot` binds the restart to the evidence it is read with. The arm
   must have run from the commit the other scenarios ran from (`meta.json`'s
   head), on a clean tree, from a `git archive`; and `reboot-verify` must have
   run with that commit's own harness, and the archive that ran after the restart
   must hold exactly that commit's files under the relevant paths. One arm, one
   verify. A restart taken at another commit, or with an edited harness, says
   nothing about this one. (The arm's record and the steps are the harness's own
   statements, at the trust level of meta.json: a binding against mistakes and
   mixing, not against forgery.)
2. `reboot-verify` never signals a pid from a pause mark unless the boot is
   positively the same one (a missing, empty or unreadable boot id on either side
   signals nothing), and `capture()` signals a marked pid only when the process
   holding it now started when the mark says (field 22 of /proc/<pid>/stat,
   written into the mark by the fixture); a pid reused by another process, a mark
   without a start time or an unreadable one signals nothing.
3. P2-11, "crash and reboot recovery", is its own requirement of the record:
   it holds only when the crash scenarios (controller-kill, orphaned-run) AND the
   reboot scenario pass. It is standalone: a record without the restart still
   speaks for the five exit conditions P2-33..P2-37.
4. The record version names that change.

Plain unittest on purpose (runs under pytest and `python3 -m unittest`; the one
test that imports the fixture module needs `hoh` importable, like the fixture); it
never starts the harness (`run`), Herdr or an agent. The only process it starts
is a sleeping child that it kills itself.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("durability_e2e_reboot_probe",
                                               ROOT / "tools" / "durability_e2e.py")
dur = importlib.util.module_from_spec(_spec)
sys.modules["durability_e2e_reboot_probe"] = dur
_spec.loader.exec_module(dur)

HEAD = "a" * 40
OTHER = "b" * 40
EXIT_CONDITIONS = ["P2-33", "P2-34", "P2-35", "P2-36", "P2-37"]
CLEAN_META = {"dirty": False, "dirty_at_end": False}


def _steps(*, boot_before="boot-a", boot_after="boot-b", tool_ok=True, code_ok=True) -> list[dict]:
    verify = {"kind": "reboot-verify", "boot_id": boot_after}
    if tool_ok is not None:
        verify["tool_matches_armed_head"] = tool_ok
    if code_ok is not None:
        verify["code_matches_armed_head"] = code_ok
    return [
        {"kind": "process-start", "pid": 10, "argv": ["project", "drive"], "label": "drive-master-1"},
        {"kind": "reboot-arm", "boot_id": boot_before, "pid": 10},
        verify,
        {"kind": "process-exit", "label": "tick-01", "pid": 20,
         "output": {"projects": [{"action": "recovered", "halt": "CLOSED"}]}},
    ]


class DeriveRebootBindingTest(unittest.TestCase):
    """derive_reboot on synthetic evidence, with the fact readers that need a
    repo bundle or pydantic patched out."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="dur-reboot-")
        self.ev = Path(self._tmp.name)
        self.d = self.ev / "reboot"
        self.d.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, *, armed=None, meta=None, steps=None) -> None:
        good_armed = {"head": HEAD, "dirty": False, "code": f"git archive {HEAD}",
                      "boot_id": "boot-a", "base": "x"}
        (self.d / "ARMED.json").write_text(json.dumps(good_armed if armed is None else armed))
        if meta is not False:
            (self.ev / "meta.json").write_text(json.dumps({"head": HEAD} if meta is None else meta))
        rows = _steps() if steps is None else steps
        (self.d / "steps.jsonl").write_text("".join(json.dumps(s) + "\n" for s in rows))

    def _checks(self) -> dict:
        facts = {"epochs": [(1, "master-1", "INITIAL"), (2, "recover-1", "RECOVERY")],
                 "lease_log": [{"previous": "holder 'master-1' was on boot a, "
                                            "this is boot b: the machine restarted"}]}
        with mock.patch.object(dur, "_project_facts", return_value=facts), \
                mock.patch.object(dur, "_repo_facts", return_value={}), \
                mock.patch.object(dur, "_result",
                                  side_effect=lambda st, f, r, checks, *a, **k: {"checks": checks}):
            return dur.derive_reboot(self.d, self.d / "work")["checks"]

    def test_a_restart_taken_at_the_evidence_commit_holds(self):
        self._write()
        checks = self._checks()
        self.assertIs(checks["armed_at_the_evidence_commit"], True)
        self.assertIs(checks["verified_with_the_armed_harness"], True)
        self.assertIs(checks["armed_and_verified_once"], True)
        for k in ("boot_changed", "recovered_after_reboot", "restart_named_in_log",
                  "closed_by_the_canonical_tick"):
            self.assertIs(checks[k], True, k)

    def test_an_arm_at_another_commit_does_not_hold(self):
        self._write(armed={"head": OTHER, "dirty": False, "code": f"git archive {OTHER}",
                           "boot_id": "boot-a", "base": "x"})
        checks = self._checks()
        self.assertIs(checks["armed_at_the_evidence_commit"], False)
        self.assertIs(checks["boot_changed"], True)

    def test_an_arm_on_a_dirty_tree_does_not_hold(self):
        self._write(armed={"head": HEAD, "dirty": True, "code": f"git archive {HEAD}",
                           "boot_id": "boot-a", "base": "x"})
        self.assertIs(self._checks()["armed_at_the_evidence_commit"], False)

    def test_an_arm_from_the_working_tree_does_not_hold(self):
        self._write(armed={"head": HEAD, "dirty": False, "code": "live working tree",
                           "boot_id": "boot-a", "base": "x"})
        self.assertIs(self._checks()["armed_at_the_evidence_commit"], False)

    def test_no_evidence_commit_to_compare_with_fails_closed(self):
        # No meta.json at all, and an arm that names no commit: two missing
        # heads are not a match.
        self._write(armed={"dirty": False, "code": "git archive ", "boot_id": "boot-a",
                           "base": "x"}, meta=False)
        self.assertIs(self._checks()["armed_at_the_evidence_commit"], False)
        self._write(armed={"head": HEAD, "dirty": False, "code": f"git archive {HEAD}",
                           "boot_id": "boot-a", "base": "x"}, meta=False)
        self.assertIs(self._checks()["armed_at_the_evidence_commit"], False)
        self._write(armed={"dirty": False, "code": "git archive ", "boot_id": "boot-a",
                           "base": "x"}, meta={})
        self.assertIs(self._checks()["armed_at_the_evidence_commit"], False)

    def test_a_verify_with_another_harness_does_not_hold(self):
        self._write(steps=_steps(tool_ok=False))
        self.assertIs(self._checks()["verified_with_the_armed_harness"], False)
        self._write(steps=_steps(tool_ok=None))          # a verify that never said
        self.assertIs(self._checks()["verified_with_the_armed_harness"], False)

    def test_a_verify_whose_archive_is_not_the_armed_commits_does_not_hold(self):
        self._write(steps=_steps(code_ok=False))
        self.assertIs(self._checks()["verified_with_the_armed_harness"], False)
        self._write(steps=_steps(code_ok=None))
        self.assertIs(self._checks()["verified_with_the_armed_harness"], False)

    def test_a_second_verify_or_a_second_arm_does_not_hold(self):
        rows = _steps()
        verify = next(s for s in rows if s["kind"] == "reboot-verify")
        arm = next(s for s in rows if s["kind"] == "reboot-arm")
        self._write(steps=rows + [dict(verify)])
        self.assertIs(self._checks()["armed_and_verified_once"], False)
        self._write(steps=rows + [dict(arm)])
        self.assertIs(self._checks()["armed_and_verified_once"], False)
        self._write(steps=[s for s in rows if s["kind"] != "reboot-verify"])
        self.assertIs(self._checks()["armed_and_verified_once"], False)

    def test_the_restart_checks_are_still_there_and_still_fail_on_the_same_boot(self):
        self._write(steps=_steps(boot_before="boot-a", boot_after="boot-a"))
        checks = self._checks()
        self.assertIs(checks["boot_changed"], False)
        self.assertIs(checks["armed_at_the_evidence_commit"], True)


class _Sleeper:
    """A child process whose pid sits in a pause mark, killed by the test itself."""

    def __enter__(self):
        self.p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        return self.p

    def __exit__(self, *exc):
        if self.p.poll() is None:
            self.p.kill()
        self.p.wait()


def _alive_after(p: subprocess.Popen, seconds: float = 1.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if p.poll() is not None:
            return False
        time.sleep(0.02)
    return p.poll() is None


class CaptureAcrossABootTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="dur-reboot-")
        base = Path(self._tmp.name)
        self.sc = dur.Scenario.__new__(dur.Scenario)
        self.sc.name = "reboot"
        self.sc.dir = base / "reboot"
        self.sc.repo = self.sc.dir / "repo"
        self.sc.root = self.sc.dir / "root"
        self.sc.marks = self.sc.dir / "marks"
        self.sc.out = base / "ev" / "reboot"
        self.sc.procs = []
        self.sc.captured = False
        for d in (self.sc.repo, self.sc.root / "projects", self.sc.marks, self.sc.out):
            d.mkdir(parents=True)
        dur._git(self.sc.repo, "init", "-q", "-b", "main")
        (self.sc.repo / "README").write_text("fixture\n")
        dur._git(self.sc.repo, "add", "README")
        dur._git(self.sc.repo, "commit", "-q", "-m", "init")

    def tearDown(self):
        self._tmp.cleanup()

    def _mark(self, pid: int, start="MEASURE") -> None:
        data = {"point": "post-launch:n1", "pid": pid, "at": "x"}
        if start == "MEASURE":
            start = dur._pid_start(pid)
        if start is not None:
            data["start"] = start
        (self.sc.marks / "post-launch__n1.reached").write_text(json.dumps(data))

    def _steps(self) -> list[dict]:
        return [json.loads(ln) for ln in
                (self.sc.out / "steps.jsonl").read_text().splitlines() if ln.strip()]

    def _kinds(self, kind: str) -> list[dict]:
        f = self.sc.out / "steps.jsonl"
        return [s for s in self._steps() if s["kind"] == kind] if f.is_file() else []

    def test_a_pid_from_a_mark_of_the_previous_boot_is_never_signalled(self):
        with _Sleeper() as other:
            self._mark(other.pid)
            self.sc.capture(skip_marks=True)
            self.assertTrue(_alive_after(other))
        self.assertEqual(self._kinds("sigkill"), [])
        self.assertTrue((self.sc.out / "repo.bundle").is_file())

    def test_a_pid_from_a_mark_of_this_boot_is_still_cleaned_up(self):
        with _Sleeper() as child:
            self._mark(child.pid)
            self.sc.capture()
            self.assertIs(_alive_after(child, 3.0), False)
        kills = self._kinds("sigkill")
        self.assertEqual([(k["point"], k["pid"]) for k in kills],
                         [("cleanup-child", child.pid)])

    def test_a_pid_held_by_another_process_than_the_marks_is_not_signalled(self):
        with _Sleeper() as other:
            self._mark(other.pid, start="1")             # the mark's process started at tick 1
            self.sc.capture()
            self.assertTrue(_alive_after(other))
        self.assertEqual(self._kinds("sigkill"), [])
        skipped = self._kinds("mark-not-signalled")
        self.assertEqual([(s["pid"], s["point"]) for s in skipped],
                         [(other.pid, "post-launch:n1")])

    def test_a_mark_without_a_start_time_signals_nothing(self):
        with _Sleeper() as other:
            self._mark(other.pid, start=None)
            self.sc.capture()
            self.assertTrue(_alive_after(other))
        self.assertEqual(self._kinds("sigkill"), [])
        self.assertEqual(len(self._kinds("mark-not-signalled")), 1)

    def test_a_start_time_that_cannot_be_read_signals_nothing(self):
        with _Sleeper() as child:
            self._mark(child.pid)
            with mock.patch.object(dur, "_pid_start", return_value=None):
                self.sc.capture()
            self.assertTrue(_alive_after(child))
        self.assertEqual(self._kinds("sigkill"), [])

    def test_a_mark_of_a_process_that_is_gone_is_quietly_ignored(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        self._mark(gone.pid, start="123")
        self.sc.capture()
        self.assertEqual(self._kinds("sigkill"), [])
        self.assertEqual(self._kinds("mark-not-signalled"), [])

    def test_the_fixture_writes_the_start_time_the_harness_reads(self):
        sys.path.insert(0, str(ROOT / "src"))
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import durability_fixture as fx
        finally:
            sys.path.remove(str(ROOT / "tools"))
            sys.path.remove(str(ROOT / "src"))
        marks = Path(self._tmp.name) / "fxmarks"
        marks.mkdir()
        (marks / "p__q.release").write_text("go")          # released: pause() returns at once
        env = {fx.PAUSE_ENV: "p:q", fx.MARKS_ENV: str(marks)}
        with mock.patch.dict(os.environ, env):
            fx.pause("p:q")
        data = json.loads((marks / "p__q.reached").read_text())
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["start"], dur._pid_start(os.getpid()))
        self.assertTrue(data["start"])


class ArchiveMatchesHeadTest(unittest.TestCase):
    """`_archive_matches_head` on a throwaway repository with the relevant
    paths, and a copy of its committed tree standing for the exported archive."""

    PATHS = ("src/hoh", "ops/unit.service", "tools/harness.py")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="dur-archive-")
        base = Path(self._tmp.name)
        self.repo, self.code = base / "repo", base / "code"
        for rel, text in (("src/hoh/a.py", "a = 1\n"), ("src/hoh/sub/b.py", "b = 2\n"),
                          ("ops/unit.service", "ExecStart=x\n"),
                          ("tools/harness.py", "h = 3\n"), ("README", "r\n")):
            for root in (self.repo, self.code):
                f = root / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text)
        dur._git(self.repo, "init", "-q", "-b", "main")
        dur._git(self.repo, "add", "-A")
        dur._git(self.repo, "commit", "-q", "-m", "c")
        self.head = dur._git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        self._tmp.cleanup()

    def _ok(self, head=None) -> bool:
        return dur._archive_matches_head(self.code, self.head if head is None else head,
                                         repo=self.repo, paths=self.PATHS)

    def test_the_committed_tree_matches(self):
        self.assertIs(self._ok(), True)
        (self.code / "src/hoh/__pycache__").mkdir()
        (self.code / "src/hoh/__pycache__/a.pyc").write_text("x")
        self.assertIs(self._ok(), True)                # bytecode is not a file of the commit

    def test_an_edited_missing_or_extra_file_does_not(self):
        (self.code / "tools/harness.py").write_text("h = 4\n")
        self.assertIs(self._ok(), False)
        (self.code / "tools/harness.py").write_text("h = 3\n")
        (self.code / "src/hoh/extra.py").write_text("x\n")
        self.assertIs(self._ok(), False)
        (self.code / "src/hoh/extra.py").unlink()
        (self.code / "src/hoh/sub/b.py").unlink()
        self.assertIs(self._ok(), False)

    def test_an_unknown_or_empty_head_never_matches(self):
        for head in ("", None, "0" * 40):
            self.assertIs(dur._archive_matches_head(self.code, head, repo=self.repo,
                                                    paths=self.PATHS), False)


class _FakeScenario:
    """Stands in for Scenario in cmd_reboot_verify: records what it is asked."""
    instances: list = []

    def __init__(self, base, evidence, name, nodes, *, fresh=True):
        self.steps, self.capture_calls, self.fresh = [], [], fresh
        _FakeScenario.instances.append(self)

    def step(self, kind, **fields):
        self.steps.append({"kind": kind, **fields})

    def tick(self, label, pause=()):
        return label

    def finish(self, proc, timeout=120):
        return {}

    def capture(self, **kw):
        self.capture_calls.append(kw)

    @property
    def store(self):
        closed = mock.Mock()
        closed.read_state.return_value.rc_closed.return_value = True
        return closed


class VerifyWiringTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="dur-reboot-")
        self.ev = Path(self._tmp.name) / "ev"
        (self.ev / "reboot").mkdir(parents=True)
        _FakeScenario.instances = []

    def tearDown(self):
        self._tmp.cleanup()

    def _verify(self, *, armed_boot="boot-a", now="boot-b", head=HEAD, harness_ok=True,
                code_ok=True) -> _FakeScenario:
        armed = {"base": str(Path(self._tmp.name) / "base"), "code_root": str(dur.HOH),
                 "head": head}
        if armed_boot is not None:                         # None: the key is absent
            armed["boot_id"] = armed_boot
        (self.ev / "reboot" / "ARMED.json").write_text(json.dumps(armed))
        with mock.patch.object(dur, "Scenario", _FakeScenario), \
                mock.patch.object(dur, "CODE", dur.CODE), \
                mock.patch.object(dur, "CONTINUE_ARGV", dur.CONTINUE_ARGV), \
                mock.patch.object(dur, "EXPORTED_HEAD", dur.EXPORTED_HEAD), \
                mock.patch.object(dur, "_current_boot_id", return_value=now), \
                mock.patch.object(dur, "_harness_matches_head", return_value=harness_ok), \
                mock.patch.object(dur, "_archive_matches_head", return_value=code_ok):
            self.assertEqual(dur.cmd_reboot_verify(argparse.Namespace(evidence=str(self.ev))), 0)
        self.assertEqual(len(_FakeScenario.instances), 1)
        return _FakeScenario.instances[0]

    def _step(self, sc: _FakeScenario) -> dict:
        return next(s for s in sc.steps if s["kind"] == "reboot-verify")

    def test_after_a_restart_the_marks_are_skipped_and_the_step_says_so(self):
        sc = self._verify(armed_boot="boot-a", now="boot-b")
        self.assertEqual(sc.capture_calls, [{"skip_marks": True}])
        self.assertIs(self._step(sc)["marks_skipped"], True)
        self.assertIs(self._step(sc)["tool_matches_armed_head"], True)
        self.assertIs(self._step(sc)["code_matches_armed_head"], True)

    def test_on_the_same_boot_the_marks_are_cleaned_up_as_before(self):
        sc = self._verify(armed_boot="boot-a", now="boot-a")
        self.assertEqual(sc.capture_calls, [{"skip_marks": False}])
        self.assertIs(self._step(sc)["marks_skipped"], False)

    def test_an_empty_or_missing_boot_id_never_signals(self):
        for armed_boot, now in (("", "boot-a"), (None, "boot-a"), ("boot-a", ""),
                                ("", ""), (None, ""), ("boot-a", None)):
            _FakeScenario.instances = []
            (self.ev / "reboot" / "steps.jsonl").unlink(missing_ok=True)
            sc = self._verify(armed_boot=armed_boot, now=now)
            self.assertEqual(sc.capture_calls, [{"skip_marks": True}], (armed_boot, now))
            self.assertIs(self._step(sc)["marks_skipped"], True, (armed_boot, now))

    def test_the_step_records_whether_the_harness_is_the_armed_commits(self):
        sc = self._verify(harness_ok=False)
        self.assertIs(self._step(sc)["tool_matches_armed_head"], False)
        _FakeScenario.instances = []
        sc = self._verify(head="")                          # an arm that named no commit
        self.assertIs(self._step(sc)["tool_matches_armed_head"], False)
        _FakeScenario.instances = []
        sc = self._verify(code_ok=False)
        self.assertIs(self._step(sc)["code_matches_armed_head"], False)

    def test_a_second_verify_is_refused_before_anything_runs(self):
        (self.ev / "reboot" / "steps.jsonl").write_text(
            json.dumps({"kind": "reboot-verify", "boot_id": "boot-b"}) + "\n")
        (self.ev / "reboot" / "ARMED.json").write_text(json.dumps(
            {"base": "x", "code_root": str(dur.HOH), "head": HEAD, "boot_id": "boot-a"}))
        with mock.patch.object(dur, "Scenario", _FakeScenario):
            rc = dur.cmd_reboot_verify(argparse.Namespace(evidence=str(self.ev)))
        self.assertEqual(rc, 2)
        self.assertEqual(_FakeScenario.instances, [])


def _row(status: str) -> dict:
    return {"status": status, "passed": status == "PASS", "checks": {"c": status == "PASS"},
            "problems": {"exactly_once": [], "continuity": [], "no_human": []}}


def _scenarios(**over) -> dict:
    out = {n: _row("PASS") for n in dur.SCENARIOS + ("reboot",)}
    out.update({k.replace("_", "-"): _row(v) for k, v in over.items()})
    return out


def _evaluate(scenarios: dict) -> dict:
    return dur._evaluate_requirements(scenarios, dict(CLEAN_META), True, [])


class CrashAndRebootRequirementTest(unittest.TestCase):

    def test_p2_11_is_a_requirement_of_the_record_bound_to_its_plan_item(self):
        entry = dur.REQUIREMENTS["P2-11"]
        self.assertEqual(entry["item"], "P2.build-verify.crash-reboot-recovery")
        self.assertEqual(sorted(entry["needs"]), ["controller-kill", "orphaned-run", "reboot"])
        register = json.loads((ROOT / "program" / "v3_3" / "REQUIREMENTS.json").read_text())
        row = next(r for r in register["requirements"] if r["id"] == "P2-11")
        self.assertEqual(row["authoritative_item"], entry["item"])

    def test_p2_11_holds_when_both_halves_pass(self):
        reqs = _evaluate(_scenarios())
        self.assertIs(reqs["P2-11"]["satisfied"], True)

    def test_without_the_restart_p2_11_is_open_and_the_exit_conditions_are_not(self):
        scenarios = _scenarios()
        scenarios["reboot"] = {"status": "NOT_RUN"}
        reqs = _evaluate(scenarios)
        self.assertIs(reqs["P2-11"]["satisfied"], False)
        for rid in EXIT_CONDITIONS:
            self.assertIs(reqs[rid]["satisfied"], True, rid)
        self.assertIs(dur._exit_conditions_satisfied(reqs), True)

    def test_a_failed_reboot_scenario_keeps_p2_11_open(self):
        reqs = _evaluate(_scenarios(reboot="FAIL"))
        self.assertIs(reqs["P2-11"]["satisfied"], False)

    def test_each_crash_scenario_is_needed(self):
        for name in ("controller_kill", "orphaned_run"):
            reqs = _evaluate(_scenarios(**{name: "FAIL"}))
            self.assertIs(reqs["P2-11"]["satisfied"], False, name)

    def test_the_top_level_flag_is_the_exit_conditions_not_p2_11(self):
        reqs = _evaluate(_scenarios(controller_kill="FAIL"))
        self.assertIs(dur._exit_conditions_satisfied(reqs), False)
        reqs = _evaluate(_scenarios(reboot="FAIL"))
        self.assertIs(dur._exit_conditions_satisfied(reqs), True)

    def test_the_record_version_names_this_shape(self):
        self.assertEqual(dur.RECORD_VERSION, 6)


if __name__ == "__main__":
    unittest.main()
