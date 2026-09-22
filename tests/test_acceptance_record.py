"""The P1-16 acceptance record: derived from raw captures, refused when forged.

v3 (same day; v2 parked in .archiv/): a second round found nine more forged or
spliced evidence sets v2 accepted -- duplicated step rows, unchecked start and
run argv, a run not bound to the minimal spec, amendments not counted, silent
defaults, a dirty clone, a run spliced in from elsewhere, files read through
symlinks or absolute paths, and an interpreter named by path. Cases below.

v2 (2026-09-22; v1 parked in .archiv/). Reviewer A forged four copies of the
third run's evidence that the v1 derivation still called satisfied: a
hand-edited clone summary (M1), missing receipts (M3), weakened isolation
fields (M4), verdicts from the wrong QA file (M5). Reviewer B found the matrix
branch crashing on a malformed record, following paths out of the tree, and a
readiness row that passed an unsatisfied record. Each is a case below, against
a synthetic evidence directory whose unmodified form is the positive control
-- so every refusal here is the forgery being caught, not the fixture broken.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("acc_probe", ROOT / "tools" / "acceptance_record.py")
acc = importlib.util.module_from_spec(_spec)
sys.modules["acc_probe"] = acc
_spec.loader.exec_module(acc)

VENV = "/x/fresh/.venv"
HEAD = "a" * 40
RUN = "r1-demo"
BINDING = "c0ffee:t1:dirty"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


FRESH = "/x/fresh"
RECHECK = "/x/recheck/tree"
TARGET = "/x/target"
WT = "/x/wt"
DRIVER_BLOB = "d" * 40
TOOL_BLOB = "e" * 40


def _recheck_out(cid, log, exit_code=0, observed=None):
    return json.dumps({"receipt": {"check_id": cid, "exit_code": exit_code, "runner_ok": True,
                                   "isolation": {"effective": "strict",
                                                 "verified_from_inside": True,
                                                 "fallback_to_none": False,
                                                 "observed_python3": observed
                                                 or f"{VENV}/bin/python3"}},
                       "log": log})
README = """Run from the root of your clone.

```sh
pip install -e ".[test]"
export HOH_RUNS=<a-directory-for-your-runs>
python3 tools/preflight.py --profile demo
hoh worktree --repo <your-project-checkout> --branch hoh-minimal-example
hoh start --repo <worktree-path> --spec examples/minimal/spec.md --run-id minimal-demo
hoh run minimal-demo --iterations 2 --until-accepted --planner claude --developer claude --qa claude \\
    --isolation strict --approval-policy policy/role_approval.default.json --trust-worktree
hoh report minimal-demo
```
"""
SPEC = """# Spec

1. **K1 -- exists.**
   Command: `python3 -c "import greet"`

3. **K3 -- a test file exists and passes.**
   Command: `python3 -m pytest -q test_greet.py`
"""
RUN_ARGV = ["hoh", "run", RUN, "--iterations", "2", "--until-accepted", "--planner", "claude", "--developer",
            "claude", "--qa", "claude", "--isolation", "strict", "--approval-policy",
            "policy/role_approval.default.json", "--trust-worktree"]


def _evidence(root: Path) -> Path:
    import hashlib
    ev = root / "ev"
    steps = [
        ("driver_blob", ["git", "rev-parse", "HEAD:dogfood/p1-16/drive_acceptance.py"],
         DRIVER_BLOB + "\n"),
        ("tool_blob", ["git", "rev-parse", "HEAD:tools/acceptance_record.py"], TOOL_BLOB + "\n"),
        ("driver_status", ["git", "status", "--porcelain", "--",
                           "dogfood/p1-16/drive_acceptance.py", "tools/acceptance_record.py"], ""),
        ("clone", ["git", "clone", acc.PUBLISHED, FRESH], "Cloning...\n"),
        ("head", ["git", "rev-parse", "HEAD"], HEAD + "\n"),
        ("published_head", ["git", "ls-remote", "origin", "refs/heads/main"],
         f"{HEAD}\trefs/heads/main\n"),
        ("status_after_clone", ["git", "status", "--porcelain"], ""),
        ("readme_blob", ["git", "rev-parse", "HEAD:examples/minimal/README.md"],
         acc.git_blob(README) + "\n"),
        ("spec_blob", ["git", "rev-parse", "HEAD:examples/minimal/spec.md"],
         acc.git_blob(SPEC) + "\n"),
        ("venv", ["python3", "-m", "venv", ".venv"], ""),
        ("install", [f"{VENV}/bin/pip", "install", "-e", ".[test]"], "Successfully installed\n"),
        ("interpreter", ["python3", "-c", "..."], VENV + "\n"),
        ("doctor", ["python3", "tools/preflight.py", "--profile", "demo"],
         "PASS  x\n\nprofile demo: READY\n"),
        ("target_head", ["git", "rev-parse", "HEAD"], "c0ffee\n"),
        ("worktree", ["hoh", "worktree", "--repo", "/x/target", "--branch",
                      "hoh-minimal-example"], '{"worktree": "/x/wt"}\nNext step: ...\n'),
        ("start", ["hoh", "start", "--repo", "/x/wt", "--spec", "examples/minimal/spec.md",
                   "--run-id", RUN], "{}"),
        ("run", RUN_ARGV, "accepted"),
        ("report", ["hoh", "report", RUN], "# report"),
        ("status_after_run", ["git", "status", "--porcelain"], ""),
        ("accepted_head", ["git", "rev-parse", "HEAD"], "0e98\n"),
        ("recheck_clone", ["git", "clone", "-q", "--no-local", TARGET, RECHECK], ""),
        ("recheck_checkout", ["git", "checkout", "-q", "0e98"], ""),
        ("recheck_tree", ["git", "rev-parse", "HEAD^{tree}"], "t1\n"),
        ("recheck_K1", [f"{VENV}/bin/python3", "-I", "-c", acc.RECHECK_SCRIPT, "K1",
                        'python3 -c "import greet"', RECHECK, "0e98"], _recheck_out("K1", "")),
        ("recheck_K3", [f"{VENV}/bin/python3", "-I", "-c", acc.RECHECK_SCRIPT, "K3",
                        "python3 -m pytest -q test_greet.py", RECHECK, "0e98"],
         _recheck_out("K3", "....\n4 passed, 1 warning in 0.02s\n")),
    ]
    lines = []
    for n, (name, argv, out) in enumerate(steps, 1):
        rel = f"steps/{n:02d}-{name}.txt"
        _write(ev / rel, out)
        env_set = {"HOH_RUNS": "/x/runs"} if name in ("worktree", "start", "run", "report") else {}
        cwd = FRESH
        if name == "recheck_clone":
            cwd = "/x/recheck"
        elif name.startswith("recheck_"):
            cwd = RECHECK
            if name[8:9] == "K":
                env_set = {"PATH": f"{VENV}/bin:/usr/bin", "VIRTUAL_ENV": VENV}
        elif name == "target_head":
            cwd = TARGET
        elif name == "accepted_head":
            cwd = WT
        elif name in ("driver_blob", "tool_blob", "driver_status"):
            cwd = "/x/hoh"
        env = {"HOME": "/x", "PATH": "/usr/bin", "HERDR_ENV": "1", **env_set}
        lines.append(json.dumps({"n": n, "name": name, "argv": argv, "exit": 0, "output": rel,
                                 "env_set": env_set, "env": env, "cwd": cwd}))
    _write(ev / "steps.jsonl", "\n".join(lines) + "\n")
    _write(ev / "driver.json", {"venv": VENV, "run_id": RUN, "fresh": FRESH, "recheck": RECHECK,
                                "target": TARGET, "worktree": WT,
                                "driver_blob_running": DRIVER_BLOB, "tool_blob_running": TOOL_BLOB})
    _write(ev / "README.as-cloned.md", README)
    _write(ev / "spec.as-cloned.md", SPEC)
    run = ev / "run-evidence" / RUN
    _write(run / "state.json", {
        "run_id": RUN, "stage": "CHECKPOINTED", "resume_attempts": 0, "amendments_seen": 0,
        "spec_path": f"{FRESH}/examples/minimal/spec.md", "repo_path": "/x/wt",
        "base_candidate": {"candidate_id": f"{RUN}-base", "commit": "c0ffee"},
        "spec_digest": hashlib.sha256(SPEC.encode()).hexdigest()[:16],
        "last_accepted_candidate": {"candidate_id": f"{RUN}-i1", "commit": "c0ffee",
                                    "tree_digest": "t1", "tree_clean": False},
        "history": ["stage NEW -> PLANNING: new iteration",
                    f"stage VERIFYING -> CHECKPOINTED: candidate {RUN}-i1 accepted"],
        "usage": {"dispatches": 3}})
    _write(run / "results" / "qa-i1-a1.json", {
        "role": "qa", "candidate_id": f"{RUN}-i1", "iteration": 1, "attempt": 1,
        "verdicts": [{"check_id": "K1", "outcome": "PASS"},
                     {"check_id": "K3", "outcome": "PASS"}]})
    for cid, cmd, log in (("K1", 'python3 -c "import greet"', "ok\n"),
                          ("K3", "python3 -m pytest -q test_greet.py", "....\n4 passed in 0.01s\n")):
        _write(run / "logs" / f"{cid}.txt", log)
        _write(run / "receipts" / f"{RUN}-i1-a1-{cid}.json", {
            "check_id": cid, "exit_code": 0, "runner_ok": True, "command": cmd,
            "candidate_binding": BINDING, "stdout_path": f"logs/{cid}.txt",
            "isolation": {"effective": "strict", "verified_from_inside": True,
                          "fallback_to_none": False,
                          "observed_python3": f"{VENV}/bin/python3"}})
    _write(run / "approval" / "integrity.json", {"held": True, "violations": []})
    _write(run / "approval" / "policy.json", {"mode": "auto", "digest": "d", "deny": ["x"]})
    return ev


def _step_output(ev: Path, name: str) -> Path:
    """A step's output file, by name -- step numbers move when steps are added."""
    return next(p for p in (ev / "steps").iterdir() if p.name.endswith(f"-{name}.txt"))


def _problems(ev: Path) -> list[str]:
    return acc.satisfied(acc.derive(ev))


def _edit_json(path: Path, fn) -> None:
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data))


def _edit_steps(ev: Path, fn) -> None:
    rows = [json.loads(x) for x in (ev / "steps.jsonl").read_text().splitlines()]
    for r in rows:
        fn(r)
    (ev / "steps.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


@pytest.fixture
def ev(tmp_path):
    return _evidence(tmp_path)


def test_the_positive_control_a_complete_run_satisfies_the_requirement(ev):
    assert _problems(ev) == []


def test_m1_a_head_that_is_not_the_published_one_is_refused(ev):
    _step_output(ev, "head").write_text("0" * 40 + "\n")
    assert any("not the published head" in p for p in _problems(ev))


def test_m1_an_install_that_is_not_the_documented_one_is_refused(ev):
    _edit_steps(ev, lambda r: r.__setitem__("argv", ["true"]) if r["name"] == "install" else None)
    assert any("step install is not the command the README" in p for p in _problems(ev))


def test_m1_a_failed_install_is_refused(ev):
    _edit_steps(ev, lambda r: r.__setitem__("exit", 1) if r["name"] == "install" else None)
    assert any("step install exited 1" in p for p in _problems(ev))


def test_m1_a_readme_that_no_longer_documents_the_install_is_refused(ev):
    (ev / "README.as-cloned.md").write_text(README.replace('pip install -e ".[test]"', "pip install hoh"))
    assert any("step install is not the command the README" in p for p in _problems(ev))


def test_m1_a_clone_of_another_repository_is_refused(ev):
    _edit_steps(ev, lambda r: r["argv"].__setitem__(2, "https://example.invalid/fork.git")
                if r["name"] == "clone" else None)
    assert any("not a clone of" in p for p in _problems(ev))


def test_m2_missing_step_captures_are_refused(ev):
    (ev / "steps.jsonl").rename(ev / "steps.jsonl.v1.parked")
    problems = _problems(ev)
    assert any("step run exited None" in p for p in problems), problems


def test_m3_a_missing_receipt_is_refused(ev):
    rec = ev / "run-evidence" / RUN / "receipts" / f"{RUN}-i1-a1-K3.json"
    rec.rename(rec.with_suffix(".json.v1.parked"))
    assert any("do not match verdicts" in p for p in _problems(ev))


@pytest.mark.parametrize("field,value,needle", [
    ("verified_from_inside", False, "verified strict"),
    ("fallback_to_none", True, "verified strict"),
    ("effective", "none", "verified strict"),
    ("observed_python3", "/opt/elsewhere/miniconda3/bin/python3", "not the fresh install"),
    ("observed_python3", None, "not the fresh install"),
])
def test_m4_weakened_isolation_fields_are_refused(ev, field, value, needle):
    rec = ev / "run-evidence" / RUN / "receipts" / f"{RUN}-i1-a1-K3.json"
    _edit_json(rec, lambda d: d["isolation"].__setitem__(field, value))
    assert any(needle in p for p in _problems(ev))


def test_m4_a_pytest_criterion_without_passing_tests_is_refused(ev):
    (ev / "run-evidence" / RUN / "logs" / "K3.txt").write_text("no tests ran\n")
    assert any("shows no pytest run" in p for p in _problems(ev))


def test_m5_verdicts_come_from_the_accepted_candidates_qa_not_the_last_file(ev):
    run = ev / "run-evidence" / RUN
    _edit_json(run / "results" / "qa-i1-a1.json",
               lambda d: [v.__setitem__("outcome", "FAIL") for v in d["verdicts"]])
    _write(run / "results" / "qa-i9-a1.json", {
        "role": "qa", "candidate_id": f"{RUN}-i9", "iteration": 9, "attempt": 1,
        "verdicts": [{"check_id": "K1", "outcome": "PASS"}]})
    assert any("not every criterion passed" in p for p in _problems(ev))


def test_m5_a_receipt_bound_to_another_candidate_is_refused(ev):
    rec = ev / "run-evidence" / RUN / "receipts" / f"{RUN}-i1-a1-K1.json"
    _edit_json(rec, lambda d: d.__setitem__("candidate_binding", "other:tree:clean"))
    assert any("not the accepted candidate" in p for p in _problems(ev))


def test_a_run_someone_unblocked_is_refused(ev):
    _edit_json(ev / "run-evidence" / RUN / "state.json",
               lambda d: d["history"].append("unblock: operator granted the dialog"))
    assert any("intervened" in p for p in _problems(ev))


def test_a_run_not_under_the_fresh_venv_is_refused(ev):
    _step_output(ev, "interpreter").write_text("/usr\n")
    assert any("fresh venv" in p for p in _problems(ev))


def test_an_unaccepted_run_is_refused(ev):
    _edit_json(ev / "run-evidence" / RUN / "state.json",
               lambda d: d.__setitem__("stage", "PLANNING"))
    assert any("not accepted" in p for p in _problems(ev))


def test_broken_integrity_and_missing_policy_are_refused(ev):
    run = ev / "run-evidence" / RUN
    _write(run / "approval" / "integrity.json", {"held": False, "violations": ["deleted: x"]})
    (run / "approval" / "policy.json").rename(run / "approval" / "policy.json.v1.parked")
    problems = _problems(ev)
    assert any("integrity" in p for p in problems)
    assert any("approval policy" in p for p in problems)


def test_an_empty_evidence_directory_is_refused_not_crashed(tmp_path):
    (tmp_path / "empty").mkdir()
    assert _problems(tmp_path / "empty")


def test_check_detects_a_hand_edited_record(ev, tmp_path):
    out = tmp_path / "rec.json"
    assert acc.main(["--evidence", str(ev), "--out", str(out)]) == 0
    assert acc.main(["--evidence", str(ev), "--out", str(out), "--check"]) == 0
    _edit_json(out, lambda d: d.__setitem__("run_id", "never-happened"))
    assert acc.main(["--evidence", str(ev), "--out", str(out), "--check"]) == 1


def test_the_v1_evidence_of_the_third_run_no_longer_satisfies():
    """The third run's evidence has no raw captures; under v2 it is not a proof."""
    evidence = ROOT / "dogfood/p1-16/20260922T1508Z"
    if not evidence.is_dir():
        pytest.skip("the P1-16 evidence is internal and not in this tree")
    assert _problems(evidence)


# --- the matrix and the board (reviewer B) ------------------------------------

def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"tools/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tree(ev, tmp_path, monkeypatch):
    """A tree holding the evidence and a derived record, with the matrix
    pointed at it. Yields (matrix module, tree root, record path)."""
    cm = _load("capability_matrix")
    home = tmp_path / "tree"
    shutil.copytree(ev, home / "ev")
    rec = home / "rec.json"
    assert acc.main(["--evidence", str(home / "ev"), "--out", str(rec)]) == 0
    monkeypatch.setattr(cm, "HOH", home)
    return cm, home, rec


PROBE = {"kind": "evidence", "path": "rec.json", "evidence_dir": "ev"}


def test_the_matrix_positive_control_and_a_hand_edited_record(tree):
    cm, home, rec = tree
    assert cm.evidence_status(PROBE)[0] == cm.PROVEN
    _edit_json(rec, lambda d: d.__setitem__("run_id", "never-happened"))
    status, note = cm.evidence_status(PROBE)
    assert status == cm.IMPLEMENTED_NOT_PROVEN and "re-derivation FAILED" in note


def test_a_record_that_re_derives_but_falls_short_is_not_proven(tree):
    cm, home, rec = tree
    _write(home / "ev/run-evidence" / RUN / "approval/integrity.json", {"held": False})
    assert acc.main(["--evidence", str(home / "ev"), "--out", str(rec)]) == 1
    assert acc.main(["--evidence", str(home / "ev"), "--out", str(rec), "--check"]) == 0
    assert cm.evidence_status(PROBE)[0] == cm.IMPLEMENTED_NOT_PROVEN


def test_a_missing_record_is_missing(tree):
    cm, home, rec = tree
    rec.rename(rec.with_suffix(".json.v1.parked"))
    assert cm.evidence_status(PROBE)[0] == cm.MISSING


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_a_malformed_record_is_one_undeterminable_row_not_a_crash(tree, content):
    cm, home, rec = tree
    rec.write_text(content)
    assert cm.evidence_status(PROBE)[0] == cm.NOT_DETERMINABLE


@pytest.mark.parametrize("path,evidence_dir", [
    ("../rec.json", "ev"), ("rec.json", "../ev"), ("/etc/hostname", "ev"), ("", "ev"),
])
def test_probe_paths_outside_the_tree_are_refused(tree, path, evidence_dir):
    cm, home, rec = tree
    shutil.copy2(rec, home.parent / "rec.json")
    status, note = cm.evidence_status({"kind": "evidence", "path": path,
                                       "evidence_dir": evidence_dir})
    assert status == cm.NOT_DETERMINABLE, note


def test_the_readiness_row_agrees_with_the_matrix(tree, monkeypatch):
    """Reviewer B: the first row passed on re-derivation alone, green beside
    a matrix that said IMPLEMENTED_NOT_PROVEN."""
    cm, home, rec = tree
    rd = _load("readiness")
    _write(home / "program/v3_3/REQUIREMENTS.json",
           {"requirements": [{"id": "P1-16", "probe": PROBE}]})
    monkeypatch.setattr(rd, "HOH", home)
    assert rd.row_acceptance().state == rd.PASS
    _write(home / "ev/run-evidence" / RUN / "approval/integrity.json", {"held": False})
    assert acc.main(["--evidence", str(home / "ev"), "--out", str(rec)]) == 1
    row = rd.row_acceptance()
    assert row.state == rd.FAIL, row
    assert cm.evidence_status(PROBE)[0] == cm.IMPLEMENTED_NOT_PROVEN


# --- round 2 (reviewers A and B, v2 -> v3) --------------------------------------

def test_n1_a_step_recorded_twice_is_refused(ev):
    rows = (ev / "steps.jsonl").read_text()
    (ev / "steps.jsonl").write_text(rows + rows.splitlines()[5] + "\n")
    assert any("recorded twice" in p for p in _problems(ev))


@pytest.mark.parametrize("step,argv", [
    ("run", ["hoh", "run", RUN, "--iterations", "2", "--until-accepted", "--planner", "claude", "--developer",
             "claude", "--qa", "claude", "--isolation", "none", "--approval-policy",
             "policy/role_approval.default.json", "--trust-worktree"]),
    ("run", RUN_ARGV[:-1]),
    ("start", ["hoh", "start", "--repo", "/x/wt", "--spec", "/tmp/trivial.md", "--run-id", RUN]),
    ("doctor", ["python3", "tools/preflight.py", "--profile", "unattended"]),
    ("report", ["hoh", "report", "some-other-run"]),
])
def test_n2_a_command_that_is_not_the_documented_one_is_refused(ev, step, argv):
    _edit_steps(ev, lambda r: r.__setitem__("argv", argv) if r["name"] == step else None)
    assert any(f"step {step} is not the command the README" in p for p in _problems(ev))


def test_n3_a_run_that_skipped_the_specs_pytest_criterion_is_refused(ev):
    run = ev / "run-evidence" / RUN
    _edit_json(run / "results" / "qa-i1-a1.json",
               lambda d: d.__setitem__("verdicts", [{"check_id": "K1", "outcome": "PASS"}]))
    (run / "receipts" / f"{RUN}-i1-a1-K3.json").rename(run / "receipts" / "K3.json.v1.parked")
    problems = _problems(ev)
    assert any("the spec's K3 has no PASS verdict" in p for p in problems), problems


def test_n3_a_run_of_another_spec_is_refused(ev):
    (ev / "spec.as-cloned.md").write_text(SPEC + "\nsomething else\n")
    assert any("spec digest" in p for p in _problems(ev))


def test_n3_a_pytest_criterion_that_ran_unittest_is_refused(ev):
    run = ev / "run-evidence" / RUN
    (run / "logs" / "K3.txt").write_text("Ran 4 tests in 0.001s\n\nOK\n")
    assert any("shows no pytest run" in p for p in _problems(ev))


def test_n4_an_amendment_during_the_run_is_refused(ev):
    run = ev / "run-evidence" / RUN
    _edit_json(run / "state.json", lambda d: (
        d["history"].append("specification amended: K3 relaxed"),
        d.__setitem__("amendments_seen", 1)))
    problems = _problems(ev)
    assert any("amended" in p for p in problems), problems


@pytest.mark.parametrize("field", ["history", "amendments_seen"])
def test_n5_missing_state_fields_are_refused_not_defaulted(ev, field):
    _edit_json(ev / "run-evidence" / RUN / "state.json", lambda d: d.pop(field))
    assert _problems(ev)


@pytest.mark.parametrize("step", ["status_after_clone", "status_after_run"])
def test_n6_a_dirty_clone_is_refused(ev, step):
    _step_output(ev, step).write_text(" M src/hoh/runner.py\n")
    assert any("not clean" in p for p in _problems(ev))


def test_n7_a_run_spliced_in_from_another_drive_is_refused(ev):
    _edit_json(ev / "driver.json", lambda d: d.__setitem__("run_id", "another-drive"))
    assert any("not this drive" in p for p in _problems(ev))


def test_n7_a_spec_outside_the_fresh_clone_is_refused(ev):
    _edit_json(ev / "run-evidence" / RUN / "state.json",
               lambda d: d.__setitem__("spec_path", "/tmp/trivial.md"))
    assert any("not in the fresh clone" in p for p in _problems(ev))


@pytest.mark.parametrize("command", [
    "PATH=/usr/bin:$PATH; python3 -m pytest -q test_greet.py",
    "/usr/bin/python3 -m pytest -q test_greet.py",
    "/opt/conda/bin/python3.12 -m pytest -q test_greet.py",
])
def test_n9_a_criterion_that_picks_its_own_interpreter_is_refused(ev, command):
    rec = ev / "run-evidence" / RUN / "receipts" / f"{RUN}-i1-a1-K3.json"
    _edit_json(rec, lambda d: d.__setitem__("command", command))
    assert any("command cannot be counted" in p for p in _problems(ev))


def test_b1_a_symlink_anywhere_in_the_evidence_is_refused(ev, tmp_path):
    outside = tmp_path / "outside-run"
    shutil.copytree(ev / "run-evidence" / RUN, outside)
    (ev / "run-evidence" / RUN).rename(ev / "run-evidence.v1.parked")
    (ev / "run-evidence").mkdir(exist_ok=True)
    (ev / "run-evidence" / RUN).symlink_to(outside)
    assert any("symlinks" in p for p in _problems(ev))


def test_b1_a_step_output_outside_the_evidence_is_not_read(ev, tmp_path):
    outside = tmp_path / "head.txt"
    outside.write_text(HEAD + "\n")
    f = _step_output(ev, "head")
    f.rename(f.with_suffix(".txt.v1.parked"))
    _edit_steps(ev, lambda r: r.__setitem__("output", str(outside)) if r["name"] == "head" else None)
    assert any("not the published head" in p for p in _problems(ev))


def test_b1_a_receipt_log_outside_the_run_is_not_read(ev, tmp_path):
    outside = tmp_path / "k3.txt"
    outside.write_text("4 passed in 0.01s\n")
    run = ev / "run-evidence" / RUN
    (run / "logs" / "K3.txt").rename(run / "logs" / "K3.txt.v1.parked")
    _edit_json(run / "receipts" / f"{RUN}-i1-a1-K3.json",
               lambda d: d.__setitem__("stdout_path", str(outside)))
    assert any("shows no pytest run" in p for p in _problems(ev))


def test_the_board_blocks_on_tampered_evidence_but_only_shows_an_unproven_one(tree, monkeypatch):
    cm, home, rec = tree
    rd = _load("readiness")
    _write(home / "program/v3_3/REQUIREMENTS.json",
           {"requirements": [{"id": "P1-16", "probe": PROBE}]})
    monkeypatch.setattr(rd, "HOH", home)
    ok = rd.row_acceptance()
    assert (ok.state, ok.advisory) == (rd.PASS, False)
    # honestly unproven: re-derives, says it falls short -> shown, not blocking
    _write(home / "ev/run-evidence" / RUN / "approval/integrity.json", {"held": False})
    acc.main(["--evidence", str(home / "ev"), "--out", str(rec)])
    short = rd.row_acceptance()
    assert (short.state, short.advisory) == (rd.FAIL, True), short
    # tampered: the record no longer re-derives -> blocking
    _edit_json(rec, lambda d: d.__setitem__("run_id", "never-happened"))
    bad = rd.row_acceptance()
    assert (bad.state, bad.advisory) == (rd.FAIL, False), bad
    # the record gone while its run is still here -> blocking (reviewer B, round 3)
    rec.rename(rec.with_suffix(".json.v1.parked"))
    orphan = rd.row_acceptance()
    assert (orphan.state, orphan.advisory) == (rd.FAIL, False), orphan
    # neither record nor run, as in the public export -> NOT_RUN, advisory
    (home / "ev").rename(home / "ev.v1.parked")
    none = rd.row_acceptance()
    assert (none.state, none.advisory) == (rd.NOT_RUN, True), none


# --- round 3 (reviewer A, v3 -> v4) ---------------------------------------------

def _receipt(ev, cid):
    return ev / "run-evidence" / RUN / "receipts" / f"{RUN}-i1-a1-{cid}.json"


def test_r1_a_criterion_whose_command_is_not_the_specs_is_refused(ev):
    _edit_json(_receipt(ev, "K1"), lambda d: d.__setitem__("command", "true"))
    assert any("command cannot be counted" in p for p in _problems(ev))


def test_r1_a_planner_may_add_flags_without_losing_the_criterion(ev):
    """The positive side of r1: the third run's K3 added `-p no:cacheprovider`
    and an environment assignment; that is still the spec's command."""
    _edit_json(_receipt(ev, "K3"), lambda d: d.__setitem__(
        "command", "PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider test_greet.py"))
    assert _problems(ev) == []


@pytest.mark.parametrize("command", [
    "python3 -m pytest -q test_greet.py || true",
    "python3 -m pytest -q test_greet.py; exit 0",
    "set +e; python3 -m pytest -q test_greet.py",
    "python3 -m pytest -q test_greet.py || :",
])
def test_r2_a_masked_exit_code_is_refused(ev, command):
    _edit_json(_receipt(ev, "K3"), lambda d: d.__setitem__("command", command))
    assert any("command cannot be counted" in p for p in _problems(ev))


@pytest.mark.parametrize("summary", ["4 passed, 2 errors in 0.10s", "3 passed, 1 failed in 0.1s",
                                     "4 passed, 1 xpassed in 0.1s"])
def test_r2_a_pytest_summary_with_problems_is_refused(ev, summary):
    (ev / "run-evidence" / RUN / "logs" / "K3.txt").write_text(summary + "\n")
    assert any("summary reports problems" in p for p in _problems(ev))


@pytest.mark.parametrize("command", [
    "command -p python3 -m pytest -q test_greet.py",
    "$(which -a python3 | tail -1) -m pytest -q test_greet.py",
    "`which python3` -m pytest -q test_greet.py",
])
def test_r3_r4_other_ways_to_pick_an_interpreter_are_refused(ev, command):
    _edit_json(_receipt(ev, "K3"), lambda d: d.__setitem__("command", command))
    assert any("command cannot be counted" in p for p in _problems(ev))


def test_r5_a_spec_path_that_climbs_out_of_the_clone_is_refused(ev):
    _edit_json(ev / "run-evidence" / RUN / "state.json",
               lambda d: d.__setitem__("spec_path", f"{FRESH}/../../tmp/other/spec.md"))
    assert any("not in the fresh clone" in p for p in _problems(ev))


def test_r6_a_run_against_another_repository_is_refused(ev):
    _edit_json(ev / "run-evidence" / RUN / "state.json",
               lambda d: d.__setitem__("repo_path", "/x/prepared-elsewhere"))
    assert any("is not the worktree" in p for p in _problems(ev))


def test_r6_a_run_from_another_base_is_refused(ev):
    _edit_json(ev / "run-evidence" / RUN / "state.json",
               lambda d: d["base_candidate"].__setitem__("commit", "feedface"))
    assert any("did not start from the target" in p for p in _problems(ev))


@pytest.mark.parametrize("name", ["README.as-cloned.md", "spec.as-cloned.md"])
def test_r7_a_copy_that_is_not_the_published_file_is_refused(ev, name):
    path = ev / name
    path.write_text(path.read_text() + "\n")
    assert any("is not the published one" in p for p in _problems(ev))


def test_r8_a_readme_documenting_a_step_twice_is_refused(ev):
    readme = README.replace("hoh report minimal-demo", "hoh report minimal-demo\nhoh run minimal-demo")
    (ev / "README.as-cloned.md").write_text(readme)
    assert any("documents a step twice" in p for p in _problems(ev))


def test_r9_a_hoh_step_without_the_documented_hoh_runs_is_refused(ev):
    _edit_steps(ev, lambda r: r.__setitem__("env_set", {}) if r["name"] == "run" else None)
    assert any("without the documented HOH_RUNS" in p for p in _problems(ev))


@pytest.mark.parametrize("command", [
    "env -i python3 -m pytest -q test_greet.py",
    "python3.12 -m pytest -q test_greet.py",
    "exec python3 -m pytest -q test_greet.py",
    "python3 -m pytest -q test_greet.py; echo '4 passed in 0.1s'",
    "python3 -m pytest -q test_greet.py | tee out.txt",
    "sh -c 'python3 -m pytest -q test_greet.py'",
    "python3 -m pytest -q test_greet.py\necho '4 passed in 0.1s'",
])
def test_b_round3_the_allowlist_refuses_what_the_blocklist_missed(ev, command):
    _edit_json(_receipt(ev, "K3"), lambda d: d.__setitem__("command", command))
    assert any("command cannot be counted" in p for p in _problems(ev))


def test_b_round3_the_third_runs_real_commands_pass_the_allowlist():
    """The positive control against the real planner output: an allowlist that
    refused the third run's own K1-K5 would be refusing HoH, not forgeries."""
    evidence = ROOT / "dogfood/p1-16/20260922T1508Z/run-evidence"
    receipts = sorted(evidence.glob("*/receipts/*-K?.json"))
    if not receipts:
        pytest.skip("the P1-16 evidence is internal and not in this tree")
    for f in receipts:
        assert acc.command_problems(json.loads(f.read_text())["command"]) == [], f.name


def test_a_requirement_once_proven_that_drops_blocks(tree, monkeypatch):
    cm, home, rec = tree
    rd = _load("readiness")
    probe = dict(PROBE, proven_on="2026-09-22")
    _write(home / "program/v3_3/REQUIREMENTS.json",
           {"requirements": [{"id": "P1-16", "probe": probe}]})
    monkeypatch.setattr(rd, "HOH", home)
    assert rd.row_acceptance().state == rd.PASS
    _write(home / "ev/run-evidence" / RUN / "approval/integrity.json", {"held": False})
    acc.main(["--evidence", str(home / "ev"), "--out", str(rec)])
    row = rd.row_acceptance()
    assert (row.state, row.advisory) == (rd.FAIL, False), row
    assert "no longer proven" in row.value_


# --- round 4 (both reviewers, v4 -> v5): independent re-execution ------------------

@pytest.mark.parametrize("command,needle", [
    ('! python3 -c "import greet"', "inverts"),
    ("PYTHONPATH=/opt/host/site-packages python3 -m pytest -q test_greet.py", "sets PYTHONPATH"),
])
def test_r13_r15_inversion_and_foreign_packages_are_refused(ev, command, needle):
    _edit_json(_receipt(ev, "K3"), lambda d: d.__setitem__("command", command))
    assert any(needle in p for p in _problems(ev))


@pytest.mark.parametrize("summary", ["1 passed, 3 deselected in 0.1s", "2 passed, 2 skipped in 0.1s",
                                     "3 passed, 1 xfailed in 0.1s"])
def test_r16_a_partial_pytest_run_is_refused(ev, summary):
    (ev / "run-evidence" / RUN / "logs" / "K3.txt").write_text(summary + "\n")
    assert any("summary reports problems" in p for p in _problems(ev))


def _step_edit(ev, name, fn):
    _edit_steps(ev, lambda r: fn(r) if r["name"] == name else None)


def test_the_spec_rechecked_on_the_accepted_commit_must_pass(ev):
    """The binding that ends the grammar chase: whatever HoH's planner wrote
    (an if/else, `!`, a second -c), the spec's own K1 must pass on the
    accepted commit."""
    _step_output(ev, "recheck_K1").write_text(_recheck_out("K1", "AssertionError", exit_code=1))
    assert any("re-run on the accepted commit, did not pass" in p for p in _problems(ev))


def test_a_masked_receipt_is_caught_by_the_recheck_not_by_parsing(ev):
    """r12/r14's shapes pass the allowlist; the recheck is what refuses them
    when the spec's command fails."""
    masked = 'if python3 -c "import greet"; then python3 -c 0; else python3 -c 0; fi'
    assert acc.command_problems(masked) == []
    _edit_json(_receipt(ev, "K1"), lambda d: d.__setitem__("command", masked))
    _step_output(ev, "recheck_K1").write_text(_recheck_out("K1", "AssertionError", exit_code=1))
    assert any("did not pass" in p for p in _problems(ev))


@pytest.mark.parametrize("edit,needle", [
    (lambda r: r.__setitem__("argv", ["bash", "-c", "true"]), "was not re-run verbatim"),
    (lambda r: r.__setitem__("cwd", "/x/elsewhere"), "was not re-run verbatim"),
    (lambda r: r.__setitem__("env_set", {"PATH": "/usr/bin"}), "did not run under the fresh venv"),
])
def test_a_recheck_that_is_not_the_verbatim_spec_in_the_clone_is_refused(ev, edit, needle):
    _step_edit(ev, "recheck_K1", edit)
    assert any(needle in p for p in _problems(ev))


def test_a_recheck_of_another_tree_is_refused(ev):
    (ev / "steps").joinpath(next(p.name for p in (ev / "steps").iterdir()
                                 if p.name.endswith("recheck_tree.txt"))).write_text("t2\n")
    assert any("not the accepted candidate's tree" in p for p in _problems(ev))


def test_a_recheck_pytest_run_that_skipped_tests_is_refused(ev):
    _step_output(ev, "recheck_K3").write_text(_recheck_out("K3", "2 passed, 2 skipped in 0.1s\n"))
    assert any("no clean pytest summary" in p for p in _problems(ev))


def test_a_missing_recheck_step_is_refused(ev):
    rows = [json.loads(x) for x in (ev / "steps.jsonl").read_text().splitlines()]
    rows = [r for r in rows if r["name"] != "recheck_K3"]
    (ev / "steps.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert any("K3 was not re-run verbatim" in p for p in _problems(ev))


@pytest.mark.parametrize("name,argv", [
    ("readme_blob", ["git", "rev-parse", "HEAD:README.md"]),
    ("target_head", ["git", "rev-parse", "HEAD~1"]),
])
def test_an_anchor_step_with_another_command_is_refused(ev, name, argv):
    _step_edit(ev, name, lambda r: r.__setitem__("argv", argv))
    assert any(f"step {name} is not the anchoring command" in p for p in _problems(ev))


# --- round 5 (both reviewers, v5 -> v6): sandboxed, scrubbed, pinned, named -------

@pytest.mark.parametrize("change,needle", [
    (lambda i: i.__setitem__("effective", "none"), "verified strict isolation"),
    (lambda i: i.__setitem__("verified_from_inside", False), "verified strict isolation"),
    (lambda i: i.__setitem__("observed_python3", "/usr/bin/python3"), "ran under /usr/bin/python3"),
])
def test_r1_a_recheck_outside_the_strict_sandbox_is_refused(ev, change, needle):
    f = _step_output(ev, "recheck_K1")
    data = json.loads(f.read_text())
    change(data["receipt"]["isolation"])
    f.write_text(json.dumps(data))
    assert any(needle in p for p in _problems(ev))


@pytest.mark.parametrize("key", ["PYTHONPATH", "BASH_ENV", "PYTEST_ADDOPTS", "ANTHROPIC_API_KEY"])
def test_r2_a_step_with_environment_outside_the_allowlist_is_refused(ev, key):
    _step_edit(ev, "recheck_K3", lambda r: r["env"].__setitem__(key, "x"))
    assert any(f"outside the allowlist: ['{key}']" in p for p in _problems(ev))


def test_r2_a_step_that_did_not_record_its_environment_is_refused(ev):
    _step_edit(ev, "run", lambda r: r.pop("env"))
    assert any("step run ran with environment outside the allowlist: ['<not recorded>']" in p
               for p in _problems(ev))


@pytest.mark.parametrize("name,argv", [
    ("recheck_checkout", ["git", "checkout", "-q", "feedface"]),          # q1
    ("recheck_tree", ["echo", "t1"]),                                       # q2
    ("recheck_clone", ["cp", "-r", "/prepared", RECHECK]),                  # q4
    ("accepted_head", ["git", "rev-parse", "HEAD~1"]),
])
def test_r3_a_recheck_step_that_is_not_the_pinned_command_is_refused(ev, name, argv):
    _step_edit(ev, name, lambda r: r.__setitem__("argv", argv))
    assert any(f"step {name} is not the pinned command" in p for p in _problems(ev))


def test_r3_q5_a_checkout_of_another_sha_than_the_accepted_head_is_refused(ev):
    _step_output(ev, "accepted_head").write_text("feedface\n")
    assert any("step recheck_checkout is not the pinned command" in p for p in _problems(ev))


@pytest.mark.parametrize("which", ["driver", "tool"])
def test_r4_a_driver_or_tool_that_is_not_the_committed_one_is_refused(ev, which):
    _edit_json(ev / "driver.json", lambda d: d.__setitem__(f"{which}_blob_running", "0" * 40))
    assert any(f"the {which} that ran is not the committed one" in p for p in _problems(ev))


def test_r4_an_uncommitted_driver_is_refused(ev):
    _step_output(ev, "driver_status").write_text(" M tools/acceptance_record.py\n")
    assert any("uncommitted changes" in p for p in _problems(ev))


# --- round 6 (both reviewers): stdout only; stop after the first acceptance ------

@pytest.mark.parametrize("name", ["recheck_K3", "worktree", "interpreter", "doctor"])
def test_a_warning_on_stderr_does_not_fail_an_honest_run(ev, name):
    f = _step_output(ev, name)
    f.write_text(f.read_text() + "\n--- stderr ---\n/x/site.py:1: DeprecationWarning: {x}\n")
    assert _problems(ev) == []


def test_the_run_must_stop_at_the_first_acceptance_as_documented(ev):
    """Reviewer A, round 6: without --until-accepted a second iteration re-runs
    a satisfied spec and can leave the run in PLANNING."""
    _step_edit(ev, "run", lambda r: r["argv"].remove("--until-accepted"))
    assert any("step run is not the command the README" in p for p in _problems(ev))
