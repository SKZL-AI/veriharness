"""The BENCH1 production ports: fakes only.

No container runtime, no `hoh`, no Herdr, no provider, no network and never a real agent run. Every collaborator of
`tools/bench1_ports.py` (runner, popen, clock, dispatcher, process table, git runner, `hoh` CLI) is replaced here;
git is used only on repositories built inside self-releasing temporary directories.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HOH = Path(__file__).resolve().parent.parent
TOOLS = HOH / "tools"


def _load(name):
    path = TOOLS / f"{name}.py"
    known = sys.modules.get(name)               # one module per process (test_bench1.py shares the tool)
    if known is not None and Path(getattr(known, "__file__", "")).resolve() == path.resolve():
        return known
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


b1 = _load("bench1")
bp = _load("bench1_ports")
IMG = "swebench/sweb.eval.x86_64.django_1776_django-12325:latest"
ID1 = "sha256:" + "1" * 64


@pytest.fixture
def work():
    with tempfile.TemporaryDirectory(prefix="bench1-ports-") as d:
        yield Path(d)


def vfs(free_inodes=5_000_000, used=0.5, blocks=1000):
    return lambda path: SimpleNamespace(f_favail=free_inodes, f_blocks=blocks, f_bavail=round(blocks * (1 - used)))


# --------------------------------------------------------------------------- storage guard


def test_storage_guard_passes_and_names_what_it_read():
    g = bp.StorageGuard(("/a", "/b"), statvfs=vfs())
    rows = g.check()
    assert [r["path"] for r in rows] == ["/a", "/b"] and rows[0]["free_inodes"] == 5_000_000


def test_storage_guard_stops_below_the_inode_floor_and_above_the_byte_ceiling():
    with pytest.raises(bp.StorageStop, match="free inodes"):
        bp.StorageGuard(("/a",), statvfs=vfs(free_inodes=2_999_999)).check()
    with pytest.raises(bp.StorageStop, match="bytes used"):
        bp.StorageGuard(("/a",), statvfs=vfs(used=0.93)).check()
    bp.StorageGuard(("/a",), statvfs=vfs(free_inodes=3_000_000, used=0.92)).check()      # exactly the limits


def test_storage_guard_counts_the_projected_cost_of_the_next_pull():
    g = bp.StorageGuard(("/a",), statvfs=vfs(free_inodes=3_100_000))
    g.check(extra_inodes=100_000)
    with pytest.raises(bp.StorageStop, match="projected"):
        g.check(extra_inodes=100_001)


def test_storage_guard_constants_are_the_registered_ones():
    assert bp.MIN_FREE_INODES == 3_000_000 and bp.MAX_USED_FRACTION == 0.92 and bp.PROJECTION_FACTOR == 1.5


# --------------------------------------------------------------------------- container runtime port


class Frames:
    """A scripted `runner`: records argv, answers by shape."""

    def __init__(self, tar_bytes=None, rc=0, stderr=""):
        self.calls, self.tar_bytes, self.rc, self.stderr = [], tar_bytes, rc, stderr

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        out = kw.get("stdout")
        if self.tar_bytes is not None and out is not None and hasattr(out, "write"):
            out.write(self.tar_bytes)
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(argv, self.rc, ID1 + "\n", self.stderr)
        return subprocess.CompletedProcess(argv, self.rc, "", self.stderr)


def make_tar(members: dict[str, bytes], extra=None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        if extra:
            for ti in extra:
                t.addfile(ti)
    return buf.getvalue()


def test_export_vector_is_exact_and_on_the_allow_list():
    argv = bp.export_argv(IMG, 1000, 1001)
    assert argv == ["docker", "run", b1._EPHEMERAL, "--network", "none", "--user", "1000:1001", IMG,
                    "tar", "-C", "/testbed", "--exclude=./.git", "-cf", "-", "."]
    assert b1.docker_argv_allowed(argv)
    with pytest.raises(b1.Bench1Error):
        bp.export_argv("--privileged", 1, 1)


def test_pull_is_guarded_measured_and_projected(work):
    inodes = iter([5_000_000, 4_900_000, 4_900_000, 4_800_000])   # before/after of pull 1, then pull 2

    def sv(path):
        return SimpleNamespace(f_favail=next(inodes), f_blocks=1000, f_bavail=500)
    runner = Frames()
    d = bp.GuardedDocker(bp.StorageGuard(("/",), min_free_inodes=4_700_000, statvfs=sv), runner=runner)
    d.pull("img/a:1")
    assert d.deltas == [100_000] and runner.calls == [["docker", "pull", "img/a:1"]]
    # second pull: free 4 900 000 - 1.5 x 100 000 = 4 750 000 >= 4 700 000: allowed
    d.pull("img/b:1")
    assert d.deltas == [100_000, 100_000]


def test_pull_is_refused_before_the_runtime_is_called_when_the_margin_would_not_survive_it():
    runner = Frames()
    d = bp.GuardedDocker(bp.StorageGuard(("/",), statvfs=vfs(free_inodes=3_050_000)), runner=runner)
    d.deltas = [100_000]                       # 1.5 x 100 000 = 150 000 > 50 000 of margin
    with pytest.raises(bp.StorageStop):
        d.pull("img/a:1")
    assert runner.calls == []


def test_export_testbed_extracts_into_a_new_directory(work):
    tar = make_tar({"manage.py": b"print(1)\n", "pkg/mod.py": b"x = 1\n"})
    d = bp.GuardedDocker(bp.StorageGuard(("/",), statvfs=vfs()), runner=Frames(tar_bytes=tar), uid=1, gid=2)
    n = d.export_testbed(IMG, work / "src", work)
    assert n == 2 and (work / "src" / "pkg" / "mod.py").read_text() == "x = 1\n"


@pytest.mark.parametrize("bad", ["/etc/passwd", "../escape", "a/../../b"])
def test_export_testbed_refuses_unsafe_members(work, bad):
    tar = make_tar({bad: b"x"})
    d = bp.GuardedDocker(bp.StorageGuard(("/",), statvfs=vfs()), runner=Frames(tar_bytes=tar))
    with pytest.raises(b1.Bench1Error, match="unsafe"):
        d.export_testbed(IMG, work / "src", work)


def test_export_testbed_refuses_a_device_member(work):
    ti = tarfile.TarInfo("dev")
    ti.type = tarfile.CHRTYPE
    tar = make_tar({}, extra=[ti])
    d = bp.GuardedDocker(bp.StorageGuard(("/",), statvfs=vfs()), runner=Frames(tar_bytes=tar))
    with pytest.raises(b1.Bench1Error, match="unsafe"):
        d.export_testbed(IMG, work / "src", work)


def test_export_failure_is_a_refusal(work):
    d = bp.GuardedDocker(bp.StorageGuard(("/",), statvfs=vfs()), runner=Frames(tar_bytes=b"", rc=125))
    with pytest.raises(b1.Bench1Error, match="export failed"):
        d.export_testbed(IMG, work / "src", work)


def test_prepare_snapshot_is_one_commit_with_the_build_artefacts(work):
    tar = make_tar({"manage.py": b"x\n", "_version.py": b"v='1'\n", "pkg/a.py": b"a=1\n"})
    d = bp.GuardedDocker(bp.StorageGuard(("/",), statvfs=vfs()), runner=Frames(tar_bytes=tar))
    tmp = work / "tmp"
    tmp.mkdir()
    info = bp.prepare_snapshot(d, IMG, work / "task", tmp)
    assert info["report"]["commits"] == 1 and info["report"]["unreachable_objects"] == 0 and info["members"] == 3
    assert (work / "task" / "snap" / "_version.py").exists()
    assert list(tmp.iterdir()) == []                      # the export directory released itself


def test_bench_test_script_is_python_and_carries_the_image(work):
    text = bp.bench_test_script(IMG, TOOLS / "bench1.py")
    compile(text, "bench-test", "exec")
    assert IMG in text and text.startswith("#!/usr/bin/env python3")
    p = bp.write_bench_test(work, IMG, TOOLS / "bench1.py")
    assert os.access(p, os.X_OK) and p.read_text() == text
    with pytest.raises(FileExistsError):
        bp.write_bench_test(work, IMG, TOOLS / "bench1.py")
    with pytest.raises(b1.Bench1Error):
        bp.bench_test_script("--entrypoint=sh", TOOLS / "bench1.py")


def test_image_ref_and_stage_tasks():
    assert bp.image_ref("django__django-12325") == "swebench/sweb.eval.x86_64.django_1776_django-12325:latest"
    assert bp.image_ref("pytest-dev__pytest-5631") == \
        "swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-5631:latest"
    with pytest.raises(b1.Bench1Error):
        bp.image_ref("no-double-underscore")
    sample = {"round1": [{"instance_id": "a"}], "round2": [{"instance_id": "b"}],
              "stage0": [{"instance_id": "c"}]}
    assert [r["instance_id"] for r in bp.stage_tasks(sample, "T")] == ["a", "b"]
    assert [r["instance_id"] for r in bp.stage_tasks(sample, "0")] == ["c"]
    with pytest.raises(b1.Bench1Error):
        bp.stage_tasks(sample, "1a")
    with pytest.raises(b1.Bench1Error):
        bp.stage_tasks({}, "T")


# --------------------------------------------------------------------------- parsers, arms' path, entry gate


PYTEST_OUT = """\
============ short test summary info ============
PASSED testing/test_a.py::test_one
FAILED testing/test_a.py::test_two - AssertionError
ERROR testing/test_b.py::test_three
XFAIL testing/test_b.py::test_four
"""
DJANGO_OUT = """\
test_alpha (app.tests.CaseA) ... ok
test_beta (app.tests.CaseA) ... FAIL
test_gamma (app.tests.CaseB) ... ERROR
test_delta (app.tests.CaseB) ... skipped 'needs db'
"""
SYMPY_OUT = """\
test_basic ok
test_edge F
test_other E
"""


def test_parse_test_results_for_the_three_repositories():
    p = bp.parse_test_results("pytest-dev/pytest", PYTEST_OUT)
    assert p == {"testing/test_a.py::test_one": "PASSED", "testing/test_a.py::test_two": "FAILED",
                 "testing/test_b.py::test_three": "FAILED", "testing/test_b.py::test_four": "PASSED"}
    d = bp.parse_test_results("django/django", DJANGO_OUT)
    assert d["test_alpha (app.tests.CaseA)"] == "PASSED" and d["test_beta (app.tests.CaseA)"] == "FAILED"
    assert d["test_gamma (app.tests.CaseB)"] == "FAILED" and d["test_delta (app.tests.CaseB)"] == "PASSED"
    s = bp.parse_test_results("sympy/sympy", SYMPY_OUT)
    assert s == {"test_basic": "PASSED", "test_edge": "FAILED", "test_other": "FAILED"}
    with pytest.raises(b1.Bench1Error):
        bp.parse_test_results("astropy/astropy", "x")


def test_arms_path_counts_treat_an_unmentioned_test_as_failed():
    c = bp.arms_path_counts("sympy/sympy", SYMPY_OUT, ["test_basic", "test_missing"], ["test_edge", "test_basic"])
    assert c == {"f2p_failed": 1, "p2p_failed": 1}
    assert bp.arms_path_counts("sympy/sympy", "", [], []) == {"f2p_failed": 0, "p2p_failed": 0}


def _runs(gold_arms=(0, 0), base_arms=(1, 0), gold_off=(0, 0), base_off=(1, 0)):
    def mk(g, b):
        return lambda patch: {"f2p_failed": (g if patch else b)[0], "p2p_failed": (g if patch else b)[1]}
    return mk(gold_arms, base_arms), mk(gold_off, base_off)


def test_entry_gate_for_task_runs_each_list_twice_and_holds():
    arms, off = _runs()
    g = bp.entry_gate_for_task(arms, off, "diff --git")
    assert g["holds"] is True and {k: len(v) for k, v in g["lists"].items()} == {
        "gold_arms": 2, "gold_official": 2, "base_arms": 2, "base_official": 2}


def test_entry_gate_for_task_refuses_a_result_that_is_not_two_counts():
    arms, off = _runs()
    g = bp.entry_gate_for_task(arms, lambda p: {"resolved": True}, "diff --git")
    assert g["holds"] is False and any("not two counts" in r for r in g["reasons"])
    g = bp.entry_gate_for_task(lambda p: {"f2p_failed": True, "p2p_failed": 0}, off, "d")
    assert g["holds"] is False


def test_entry_gate_for_task_fails_when_the_paths_disagree_or_the_base_is_green():
    arms, off = _runs(gold_off=(0, 3))
    assert bp.entry_gate_for_task(arms, off, "d")["holds"] is False
    arms, off = _runs(base_arms=(0, 0), base_off=(0, 0))
    assert bp.entry_gate_for_task(arms, off, "d")["holds"] is False


# --------------------------------------------------------------------------- the official grader


REPORT = {"django__django-12325": {"resolved": False,
                                   "tests_status": {"FAIL_TO_PASS": {"success": [], "failure": ["t1", "t2"]},
                                                    "PASS_TO_PASS": {"success": ["p1"], "failure": []}}}}


def test_official_grader_argv_is_pinned_and_never_cleans():
    g = bp.OfficialGrader(python="/usr/bin/python3")
    argv = g.argv(Path("/s/p.jsonl"), "django__django-12325", "bench1-abc")
    assert argv == ["/usr/bin/python3", "-m", "swebench.harness.run_evaluation", "--dataset_name",
                    "princeton-nlp/SWE-bench_Verified", "--predictions_path", "/s/p.jsonl", "--instance_ids",
                    "django__django-12325", "--max_workers", "1", "--run_id", "bench1-abc", "--cache_level",
                    "instance", "--clean", "False", "--namespace", "swebench"]
    with pytest.raises(b1.Bench1Error):
        g.argv(Path("/s/p"), "../x", "r")


def test_official_grader_reads_the_per_instance_report(work):
    calls = []

    def runner(argv, **kw):
        calls.append((argv, kw))
        run_id = argv[argv.index("--run_id") + 1]
        rep = Path(kw["cwd"]) / "logs" / "run_evaluation" / run_id / "bench1" / "django__django-12325"
        rep.mkdir(parents=True)
        (rep / "report.json").write_text(json.dumps(REPORT), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")
    patch = work / "p.diff"
    patch.write_text("diff --git a b\n", encoding="utf-8")
    out = bp.OfficialGrader(runner=runner).evaluate("django__django-12325", patch, {}, work)
    assert out == {"resolved": False, "f2p_failed": 2, "p2p_failed": 0}
    assert calls[0][1]["cwd"] == str(work)
    pred = json.loads((work / "predictions.jsonl").read_text())
    assert pred["instance_id"] == "django__django-12325" and pred["model_patch"].startswith("diff --git")


def test_official_grader_without_a_report_or_with_a_failed_run_raises(work):
    patch = work / "p.diff"
    patch.write_text("x", encoding="utf-8")
    with pytest.raises(b1.Bench1Error, match="no report"):
        bp.OfficialGrader(runner=lambda a, **k: subprocess.CompletedProcess(a, 0, "", "")).evaluate(
            "django__django-12325", patch, {}, work)
    with pytest.raises(b1.Bench1Error, match="no report"):
        bp.OfficialGrader(runner=lambda a, **k: subprocess.CompletedProcess(a, 1, "", "")).evaluate(
            "django__django-12325", patch, {}, work)


@pytest.mark.parametrize("report", [{}, {"t": {"resolved": "yes"}}, {"t": {"resolved": True}},
                                    {"t": {"resolved": True, "tests_status": {"FAIL_TO_PASS": {}}}}])
def test_parse_official_report_refuses_anything_it_cannot_count(report):
    with pytest.raises(b1.Bench1Error):
        bp.parse_official_report(report, "t")


# --------------------------------------------------------------------------- patches, transcripts, prompts


DIFF = """\
diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1 @@
-x
+y
diff --git a/src/a.py.v1.20261015T101010Z b/src/a.py.v1.20261015T101010Z
new file mode 100644
--- /dev/null
+++ b/src/a.py.v1.20261015T101010Z
@@ -0,0 +1 @@
+x
diff --git a/.archiv/notes.txt b/.archiv/notes.txt
new file mode 100644
--- /dev/null
+++ b/.archiv/notes.txt
@@ -0,0 +1 @@
+n
diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1 @@
+t
"""


def test_filter_patch_drops_versioned_copies_and_archiv_and_counts_them():
    kept, dropped = bp.filter_patch(DIFF)
    assert dropped == 2 and "src/a.py b/src/a.py\n" in kept and "tests/test_new.py" in kept
    assert ".v1." not in kept and ".archiv" not in kept
    assert bp.filter_patch("") == ("", 0)


def test_locate_transcripts_by_working_directory_and_time(work):
    proj = work / "projects"
    wt = work / "wt"
    wt.mkdir()
    mine = proj / bp.claude_slug(wt)
    other = proj / bp.claude_slug(work / "elsewhere")
    mine.mkdir(parents=True)
    other.mkdir(parents=True)
    fresh, old, foreign = mine / "a.jsonl", mine / "b.jsonl", other / "c.jsonl"
    for f in (fresh, old, foreign):
        f.write_text("{}\n")
    os.utime(old, (1, 1))
    link = mine / "link.jsonl"
    os.symlink(fresh, link)
    now = time.time()
    got = bp.locate_transcripts(proj, [wt], now - 3600)
    assert got == [fresh]
    assert bp.locate_transcripts(work / "none", [wt], 0) == []


def test_transcript_lines_reads_regular_files_only(work):
    f = work / "t.jsonl"
    f.write_text('{"a": 1}\n{"b": 2}\n', encoding="utf-8")
    assert bp.transcript_lines([f]) == ['{"a": 1}', '{"b": 2}']
    fifo = work / "q.jsonl"
    os.mkfifo(fifo)
    with pytest.raises(b1.Bench1Error):
        bp.transcript_lines([fifo])


def test_render_prompt_fills_placeholders_and_refuses_leftovers():
    assert bp.render_prompt("a {x} b {y}", x="1", y="2") == "a 1 b 2"
    with pytest.raises(b1.Bench1Error, match="unfilled"):
        bp.render_prompt("a {x} b {z}", x="1")


def test_without_fix_request_removes_only_that_paragraph():
    w = "You are working.\nFix the issue below so that it works.\nRules.\n\nRules of this environment:\n- one"
    out = bp.without_fix_request("Intro paragraph.\n\nFix the issue below so that it is right.\n\n- Rules")
    assert "Fix the issue below" not in out and "Intro paragraph." in out and "- Rules" in out
    assert w.count("Fix the issue below") == 1


# --------------------------------------------------------------------------- policy, routing, command line read-back


def _policy(work, **kw):
    doc = {"schema": "hoh-role-approval/1", "mode": "auto", "deny": ["WebFetch", "WebSearch", "Bash(curl:*)"],
           "house_rules": "# house rules\nno deletions\n"}
    doc.update(kw)
    p = work / "policy.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_policy_readback_digests_the_file_and_the_house_text(work):
    p = _policy(work)
    r = bp.policy_readback(p)
    assert r["policy_digest"] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert r["house_text_digest"] == hashlib.sha256(b"# house rules\nno deletions\n").hexdigest()
    assert r["denied_tools"] == ["WebFetch", "WebSearch", "Bash(curl:*)"]
    bad = work / "bad.json"
    bad.write_text(json.dumps({"deny": []}), encoding="utf-8")
    with pytest.raises(b1.Bench1Error):
        bp.policy_readback(bad)


def _routing(work, policy_doc=None, digest=None):
    pol = policy_doc or bp.routing_policy_doc()
    pol_path = work / "routing-policy.json"
    pol_path.write_text(json.dumps(pol), encoding="utf-8")
    canonical = json.dumps(pol, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    run_dir = work / "run"
    run_dir.mkdir()
    pin = {"schema": "hoh-routing-pin/1", "digest": digest or hashlib.sha256(canonical.encode()).hexdigest(),
           "source": str(pol_path)}
    (run_dir / "routing.json").write_text(json.dumps(pin), encoding="utf-8")
    return run_dir


def test_routing_readback_reads_the_pinned_policy_per_role(work):
    r = bp.routing_readback(_routing(work), ["planner", "developer", "qa"])
    assert r["models"] == {"planner": "claude-sonnet-5-5", "developer": "claude-sonnet-5-5",
                           "qa": "claude-sonnet-5-5"}
    assert set(r["efforts"].values()) == {"high"}


def test_routing_readback_honours_a_role_entry_and_refuses_a_digest_mismatch(work):
    pol = {"schema": "hoh-routing-policy/1", "default": {"model": "m", "effort": "high"},
           "roles": {"qa": {"model": "other"}}}
    r = bp.routing_readback(_routing(work, pol), ["developer", "qa"])
    assert r["models"] == {"developer": "m", "qa": "other"}
    with tempfile.TemporaryDirectory(dir=work) as d:
        bad = _routing(Path(d), digest="0" * 64)
        with pytest.raises(b1.Bench1Error, match="digest"):
            bp.routing_readback(bad, ["qa"])


def test_b_run_argv_is_the_registered_command_line(work):
    paths = bp.CampaignPaths.under(work, work / "policy.json", work / "arms", TOOLS / "bench1.py")
    argv = bp.b_run_argv("py", work / "root", "RID", paths, "none")
    assert argv == ["py", "-m", "hoh.cli", "--root", str(work / "root"), "run", "RID", "--iterations", "3",
                    "--until-accepted", "--planner", "claude", "--developer", "claude", "--qa", "claude",
                    "--role-model", "all=claude-sonnet-5-5", "--role-effort", "all=high", "--role-timeout", "2700",
                    "--approval-policy", str(work / "policy.json"), "--trust-worktree", "--storage-root",
                    str(work / "store"), "--isolation", "none"]
    assert "--until-accepted" in argv and bp.option_value(argv, "--role-timeout") == "2700"
    with pytest.raises(b1.Bench1Error):
        bp.b_run_argv("py", work, "R", paths, "off")


def test_option_value_and_process_argv(work):
    assert bp.option_value(["a", "--x", "1"], "--x") == "1"
    assert bp.option_value(["a", "--x=2"], "--x") == "2"
    assert bp.option_value(["a"], "--x") is None
    proc = work / "proc" / "123"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"py\x00-m\x00hoh.cli\x00--role-timeout\x002700\x00")
    assert bp.process_argv(123, work / "proc") == ["py", "-m", "hoh.cli", "--role-timeout", "2700"]


def test_run_id_keeps_the_task_after_a_random_part_within_herdr_names():
    r = bp.run_id_for("django__django-12325.B.1.a1", "django__django-12325")
    assert len(r) <= 24 and r.endswith("django12325") and r[:8] != r[-8:]


def test_trust_registered_reads_the_harness_registry(work):
    reg = work / "claude.json"
    reg.write_text(json.dumps({"projects": {"/wt/a": {"hasTrustDialogAccepted": True},
                                            "/wt/b": {"hasTrustDialogAccepted": False}}}), encoding="utf-8")
    assert bp.trust_registered("/wt/a", reg) is True
    assert bp.trust_registered("/wt/b", reg) is False
    assert bp.trust_registered("/wt/c", reg) is False
    assert bp.trust_registered("/wt/a", work / "missing.json") is False


# --------------------------------------------------------------------------- local git


def _git_repo(work, name="r", content="one\n"):
    repo = work / name
    repo.mkdir()
    (repo / "a.txt").write_text(content, encoding="utf-8")
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repo)}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=x", "-c", "user.email=x@x", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "c"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env)
    return repo


def test_run_git_allows_only_its_verbs():
    for verb in ("push", "reset", "clean", "checkout", "commit", "init", "rm", "stash"):
        with pytest.raises(b1.Bench1Error, match="allow-list"):
            bp.run_git(Path("."), verb)
    with pytest.raises(b1.Bench1Error):
        bp.run_git(Path("."))


def test_worktree_patch_includes_new_files_and_matches_snapshot_checks(work):
    repo = _git_repo(work)
    snap = _git_repo(work, "s", content="another\n")
    assert bp.worktree_matches_snapshot(repo, repo) is True
    assert bp.worktree_matches_snapshot(repo, snap) is False                 # another commit
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    assert bp.worktree_matches_snapshot(repo, repo) is False                 # dirty
    patch = bp.worktree_patch(repo)
    assert "+two" in patch and "new.py" in patch
    d1 = bp.worktree_digest(repo)
    assert d1 == bp.worktree_digest(repo)
    (repo / "new.py").write_text("x = 2\n", encoding="utf-8")
    assert bp.worktree_digest(repo) != d1


# --------------------------------------------------------------------------- arm B port


class FakeHohRoot:
    """`hoh worktree` / `hoh start` / `hoh status` answers; records argv."""

    def __init__(self, root, wt, run_dir_writer, state):
        self.root, self.wt, self.write, self.state = root, wt, run_dir_writer, state
        self.calls = []

    def run(self, *args, timeout=600):
        self.calls.append(args)
        if args[0] == "worktree":
            return SimpleNamespace(stdout=json.dumps({"worktree": str(self.wt), "branch": args[-1]}))
        if args[0] == "start":
            run_id = args[args.index("--run-id") + 1]
            self.write(run_id)
            return SimpleNamespace(stdout=json.dumps({"run_id": run_id,
                                                      "trust": "registered as trusted with the harness: x"}))
        raise AssertionError(args)


class FakePopen:
    def __init__(self, argv, **kw):
        self.argv, self.kw, self.pid = argv, kw, 7777
        self.timeout_with = None

    def wait(self, timeout=None):
        if self.timeout_with is not None:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return 0


class Env:
    """A campaign root with a snapshot, a policy file, arms files and a fake product record writer."""

    def __init__(self, work):
        self.work = work
        self.repo = _git_repo(work, "snapsrc")
        self.paths = bp.CampaignPaths.under(work / "camp", _policy(work), work / "camp" / "arms",
                                            TOOLS / "bench1.py", hoh_src=None)
        for d in (self.paths.store, self.paths.work, self.paths.roots, self.paths.tmp, self.paths.arms_dir):
            d.mkdir(parents=True)
        (self.paths.store / "OWNER.json").write_text("{}")
        (self.paths.arms_dir / "task_wrapper.md").write_text(
            "Wrapper.\n\nFix the issue below so it works.\n\n## Issue\n\n{problem_statement}\n", encoding="utf-8")
        for n in ("A0", "A1plus_dev", "A1plus_review"):
            (self.paths.arms_dir / f"{n}.md").write_text("{task_wrapper} {previous_review_findings}"
                                                         if n == "A1plus_dev" else
                                                         ("{task_wrapper}" if n == "A0"
                                                          else "{task_wrapper_without_fix_request}"), encoding="utf-8")
        self.task = "django__django-12325"
        self.snap = self.paths.task_dir(self.task) / "snap"
        self.snap.parent.mkdir(parents=True)
        os.rename(self.repo, self.snap)
        self.paths.routing_policy.write_text(json.dumps(bp.routing_policy_doc()), encoding="utf-8")
        self.issue = work / "issue.txt"
        self.issue.write_text("The bug.\n", encoding="utf-8")
        self.wt = self.snap          # the worktree is the snapshot itself in the fake (same commit, clean)
        self.state = {"condition": "ACTIVE", "budgets": {"max_dispatches": 9, "max_iterations": 3},
                      "usage": {"iterations": 0, "dispatches": 0}, "base_candidate": {"commit": "b" * 40}}

    def write_run(self, cell_root):
        def writer(run_id):
            rd = Path(cell_root) / run_id
            rd.mkdir(parents=True, exist_ok=True)
            (rd / "state.json").write_text(json.dumps(self.state), encoding="utf-8")
            pol = json.dumps(bp.routing_policy_doc())
            canonical = json.dumps(bp.routing_policy_doc(), sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=True)
            (rd / "routing.json").write_text(json.dumps({
                "schema": "hoh-routing-pin/1", "digest": hashlib.sha256(canonical.encode()).hexdigest(),
                "source": str(self.paths.routing_policy)}), encoding="utf-8")
            assert pol
        return writer

    def spec(self, arm="B", attempt=1):
        key = b1.cell_key(self.task, arm, 1, attempt)
        return b1.CellSpec(stage="0", task=self.task, arm=arm, rep=1, attempt=attempt, repo_slug="django/django",
                           issue_path=str(self.issue), snapshot_path=str(self.snap),
                           hoh_root=str(self.paths.cell_root(key)), base_commit="c" * 40)


@pytest.fixture
def env(work, monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")     # the registered environment contract (I15); never the ambient value
    return Env(work)


def parse_ns(argv):
    return SimpleNamespace(role_timeout=int(bp.option_value(argv, "--role-timeout")),
                           isolation=bp.option_value(argv, "--isolation"),
                           approval_policy=bp.option_value(argv, "--approval-policy"))


def make_b(env, **kw):
    guard = bp.StorageGuard((str(env.work),), statvfs=vfs())
    box = {}

    def factory(root):
        box["h"] = FakeHohRoot(root, env.wt, env.write_run(root), env.state)
        return box["h"]
    ports = bp.BArmPorts(env.paths, factory, SimpleNamespace(), isolation="none", task_meta=lambda t: {"repo": "x"},
                         guard=guard, popen=kw.pop("popen", FakePopen), parse_run_argv=kw.pop("parse", parse_ns),
                         proc_root=kw.pop("proc_root", env.work / "proc"), sleep=lambda s: None, **kw)
    return ports, box


def freeze_for(env, patterns):
    pol = bp.policy_readback(env.paths.policy_file)
    return b1.Freeze(pol["policy_digest"], pol["house_text_digest"], "none", str(env.paths.store),
                     patterns.digest)


def test_b_port_reads_back_the_registered_wiring_before_the_start(env):
    ports, box = make_b(env)
    obs = ports.build_and_readback(env.spec())
    patterns = b1.WiringPatterns.load(TOOLS / "bench1_wiring_patterns.txt")
    freeze = freeze_for(env, patterns)
    found = b1.compare_wiring(b1.registered_declaration("B", freeze), obs, "B")
    assert found == [], found
    assert obs["deferred_checks"] == ["project_resolved", "session_open", "scratch_under_storage_root",
                                      "trust_recorded"]
    assert "trust_recorded" not in obs                      # `hoh start`'s text is not worktree trust (post-start)
    assert obs["dispatch_cap"] == 9 and obs["isolation"] == "none"
    assert obs["role_timeout_s"] == {"planner": 2700, "developer": 2700, "qa": 2700}
    verbs = [c[0] for c in box["h"].calls]
    assert verbs == ["worktree", "start"]
    start = box["h"].calls[1]
    assert "--routing-policy" in start and "--max-dispatches" in start and "9" in start


def test_b_port_without_the_herdr_environment_is_an_i15_finding(env, monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    ports, _ = make_b(env)
    obs = ports.build_and_readback(env.spec())
    patterns = b1.WiringPatterns.load(TOOLS / "bench1_wiring_patterns.txt")
    found = b1.compare_wiring(b1.registered_declaration("B", freeze_for(env, patterns)), obs, "B")
    assert [f["what"] for f in found] == ["herdr_env"] and found[0]["check"] == "I15"


def test_b_port_start_is_a_session_leader_with_its_own_tmpdir(env):
    seen = {}

    def popen(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return FakePopen(argv, **kw)
    ports, _ = make_b(env, popen=popen)
    spec = env.spec()
    ports.build_and_readback(spec)
    h = ports.start(spec)
    assert h.pgid == h.leader_pid == 7777 and h.run_id == bp.run_id_for(spec.key, spec.task)
    assert seen["kw"]["start_new_session"] is True and seen["kw"]["env"]["TMPDIR"] == str(env.paths.tmp)
    assert seen["argv"][5] == "run" and "--until-accepted" in seen["argv"]
    assert (env.paths.cell_root(spec.key) / "hoh-run.log").exists()


PREFLIGHT_REPORT_ENV = "HOH_PREFLIGHT_REPORT"
PREFLIGHT_OPTIONS = ("--preflight-report", "/reports/ready.json", "--preflight-profile", "unattended",
                     "--preflight-max-age", "86400")


def test_b_run_argv_ends_in_the_preflight_options_the_product_parses_and_is_the_registered_vector_without_them(work):
    """P3-11 slice 4. Catches options dropped from the typed `hoh run`, put in the middle of the registered
    flags, or typed in a form the product's parser reads differently; and a registered vector that moved when
    no option was given. The registered vector is a prefix of the typed one and the product's own parser
    gets exactly the three values."""
    from hoh import cli
    paths = bp.CampaignPaths.under(work, work / "policy.json", work / "arms", TOOLS / "bench1.py")
    plain = bp.b_run_argv("py", work / "root", "RID", paths, "none")
    assert bp.b_run_argv("py", work / "root", "RID", paths, "none", ()) == plain
    typed = bp.b_run_argv("py", work / "root", "RID", paths, "none", PREFLIGHT_OPTIONS)
    assert typed == [*plain, *PREFLIGHT_OPTIONS]
    parsed = cli.build_parser().parse_args(typed[3:])
    assert (parsed.preflight_report, parsed.preflight_profile, parsed.preflight_max_age) == (
        "/reports/ready.json", "unattended", 86400)
    assert parsed.role_timeout == 2700 and parsed.isolation == "none" and parsed.until_accepted is True


def test_b_port_types_the_preflight_options_into_hoh_start_and_hoh_run_and_the_worktree_command_gets_none(env):
    """P3-11 slice 4. Catches an arm that spends quota on `hoh start` / `hoh run` without asking the
    environment: both typed commands carry the three options (after the registered flags, so the
    registered vector is a prefix), `hoh worktree` writes no run and is given none, and the command line the
    running process must carry is the typed one -- options included."""
    ports, box = make_b(env, preflight_options=PREFLIGHT_OPTIONS)
    spec = env.spec()
    ports.build_and_readback(spec)
    worktree, start = box["h"].calls
    assert worktree[0] == "worktree" and not [w for w in worktree if w.startswith("--preflight")]
    assert start[0] == "start" and tuple(start[-6:]) == PREFLIGHT_OPTIONS
    assert "--routing-policy" in start[:-6]
    state = ports._state[spec.key]
    assert tuple(state["argv"][-6:]) == PREFLIGHT_OPTIONS and state["argv"][5] == "run"
    seen = {}

    def popen(argv, **kw):
        seen["argv"] = argv
        return FakePopen(argv, **kw)
    ports.popen = popen
    ports.start(spec)
    assert seen["argv"] == state["argv"]
    bp.verify_process_argv(state["argv"], state["argv"])
    with pytest.raises(b1.Bench1Error, match="registered command line"):
        bp.verify_process_argv(state["argv"][:-6], state["argv"])      # a process without them is not the typed one


def test_b_port_without_options_types_exactly_the_registered_commands(env):
    """P3-11 slice 4 (compatibility). A port built without options -- every earlier caller -- types the
    registered `hoh start` and `hoh run` byte for byte as before."""
    ports, box = make_b(env)
    spec = env.spec()
    ports.build_and_readback(spec)
    assert not [w for call in box["h"].calls for w in call if str(w).startswith("--preflight")]
    assert ports._state[spec.key]["argv"] == bp.b_run_argv(sys.executable, env.paths.cell_root(spec.key),
                                                           bp.run_id_for(spec.key, spec.task), env.paths, "none")


def _preflight_report(path, *, verdict="READY", profile="unattended", age=10, environment=None):
    """A report in the producer's own shape for this interpreter (the place arm B's commands run under)."""
    from datetime import UTC, datetime, timedelta

    from hoh.preflightgate import environment_identity
    measured = (datetime.now(UTC) - timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps({
        "measured_at_utc": measured, "profile": profile, "verdict": verdict,
        "blocking": [] if verdict == "READY" else ["isolation"], "inconclusive": [],
        "environment": environment or environment_identity()}), encoding="utf-8")
    return path


def _clean_preflight_env(monkeypatch):
    for name in (PREFLIGHT_REPORT_ENV, "HOH_PREFLIGHT_PROFILE", "HOH_PREFLIGHT_MAX_AGE"):
        monkeypatch.delenv(name, raising=False)


def test_the_production_ports_of_arm_b_ask_the_environment_and_start_no_cell_without_a_report_named(
        env, monkeypatch, work):
    """P3-11 slice 4. Catches an arm B whose production ports build without the product's gate: with no
    `HOH_PREFLIGHT_REPORT` the ports are refused by name (no ports, no cell directory, no ledger entry, no
    spend); with a report this interpreter may start on, the options are the product's own `cli_options` of
    the requirement the environment names (profile and age defaults included). The A arms are plain agents
    built on `Controller` and `build_dispatcher`, not on the product's start paths: they are built without it."""
    from hoh.preflightgate import cli_options, requirement_from_environment
    _clean_preflight_env(monkeypatch)
    guard = bp.StorageGuard((str(work),), statvfs=vfs())
    with pytest.raises(b1.Bench1Error, match=rf"^refused: no preflight report is named: set {PREFLIGHT_REPORT_ENV}"):
        bp._production_ports("B", env.paths, SimpleNamespace(isolation="none"), guard, lambda t: {})
    assert list(env.paths.roots.iterdir()) == []
    assert bp._production_ports("A0", env.paths, SimpleNamespace(isolation="none"), guard,
                                lambda t: {}).__class__.__name__ == "AArmPorts"
    report = _preflight_report(work / "ready.json")
    monkeypatch.setenv(PREFLIGHT_REPORT_ENV, str(report))
    ports = bp._production_ports("B", env.paths, SimpleNamespace(isolation="none"), guard, lambda t: {})
    assert ports.preflight_options == tuple(cli_options(requirement_from_environment()))
    assert ports.preflight_options == ("--preflight-report", str(report), "--preflight-profile",
                                       "unattended", "--preflight-max-age", "86400")
    monkeypatch.setenv("HOH_PREFLIGHT_MAX_AGE", "0")
    with pytest.raises(b1.Bench1Error, match="^refused: "):
        bp._production_ports("B", env.paths, SimpleNamespace(isolation="none"), guard, lambda t: {})


@pytest.mark.parametrize("label", ["missing", "stale", "not-ready", "other-profile", "other-interpreter",
                                   "not-json", "directory"])
def test_the_production_ports_of_arm_b_refuse_a_report_that_does_not_allow_a_start_before_any_cell(
        env, monkeypatch, work, label):
    """P3-11 slice 4 (minor fold, review A 3). Catches a variable that names a report nobody can start on
    passing the ports and failing only inside `hoh start`, after the cell directory and `hoh worktree` were
    made: a missing, stale, not READY, other-profile, other-interpreter, unreadable or directory report is
    refused by the product's own gate when the ports are built -- no cell directory exists afterwards."""
    from hoh.preflightgate import environment_identity
    _clean_preflight_env(monkeypatch)
    report = work / "report.json"
    if label == "stale":
        _preflight_report(report, age=86400 + 60)
    elif label == "not-ready":
        _preflight_report(report, verdict="NOT_READY")
    elif label == "other-profile":
        _preflight_report(report, profile="demo")
    elif label == "other-interpreter":
        _preflight_report(report, environment={**environment_identity(), "interpreter": "/elsewhere/python3"})
    elif label == "not-json":
        report.write_text("{", encoding="utf-8")
    elif label == "directory":
        report.mkdir()
    monkeypatch.setenv(PREFLIGHT_REPORT_ENV, str(report))
    guard = bp.StorageGuard((str(work),), statvfs=vfs())
    with pytest.raises(b1.Bench1Error, match="^refused: "):
        bp._production_ports("B", env.paths, SimpleNamespace(isolation="none"), guard, lambda t: {})
    assert list(env.paths.roots.iterdir()) == []


def test_the_production_ports_of_arm_b_read_the_gate_from_the_product_under_test_and_never_run_without_it(
        env, monkeypatch, work):
    """P3-11 slice 4 (minor fold, review B N3/N4). Catches a product without the gate being run ungated
    (the import failure must refuse by name, not return no options) and a gate read from somewhere other
    than the product under test (`paths.hoh_src` is put first on the import path)."""
    import dataclasses
    _clean_preflight_env(monkeypatch)
    monkeypatch.setenv(PREFLIGHT_REPORT_ENV, str(_preflight_report(work / "ready.json")))
    guard = bp.StorageGuard((str(work),), statvfs=vfs())
    monkeypatch.setitem(sys.modules, "hoh.preflightgate", None)          # `import` of it raises ImportError
    with pytest.raises(b1.Bench1Error, match="^refused: the product under test has no environment preflight gate"):
        bp._production_ports("B", env.paths, SimpleNamespace(isolation="none"), guard, lambda t: {})
    monkeypatch.delitem(sys.modules, "hoh.preflightgate")
    product = work / "product-src"
    product.mkdir()
    monkeypatch.setattr(sys, "path", list(sys.path))
    paths = dataclasses.replace(env.paths, hoh_src=product)
    bp._production_ports("B", paths, SimpleNamespace(isolation="none"), guard, lambda t: {})
    assert sys.path[0] == str(product)


def write_cmdline(env, argv, pid=7777):
    proc = env.work / "proc" / str(pid)
    proc.mkdir(parents=True, exist_ok=True)
    (proc / "cmdline").write_bytes(b"".join(a.encode() + b"\x00" for a in argv))


def started_b(env, **kw):
    ports, _ = make_b(env, **kw)
    spec = env.spec()
    ports.build_and_readback(spec)
    h = ports.start(spec)
    return ports, spec, h, ports._state[spec.key]


def write_registrations(env, st, trust=True):
    run_dir = st["cell_root"] / st["run_id"]
    (env.paths.store / f"session-{st['run_id']}.json").write_text("{}")
    (run_dir / "owned-agents.jsonl").write_text(json.dumps({"cwd": str(run_dir / "arena")}) + "\n")
    if trust:
        (run_dir / "approval").mkdir(exist_ok=True)
        (run_dir / "approval" / "worktree_trust.json").write_text(
            json.dumps({"worktree": str(Path(st["wt"]).resolve()), "registered": ["x"]}), encoding="utf-8")


def test_b_port_post_start_checks_the_running_command_line_and_the_registrations(env):
    ports, spec, h, st = started_b(env)
    write_cmdline(env, st["argv"])
    write_registrations(env, st)
    post = ports.post_start_readback(spec, h)
    assert post["session_open"] is True and post["project_resolved"] is True and post["trust_recorded"] is True
    assert post["scratch_under_storage_root"] is True and post["provider_calls_before_check"] == 0


def test_b_port_post_start_refuses_a_process_without_the_registered_timeout(env):
    ports, spec, h, st = started_b(env)
    write_cmdline(env, [a if a != "2700" else "900" for a in st["argv"]])
    with pytest.raises(b1.Bench1Error, match="role-timeout"):
        ports.post_start_readback(spec, h)


@pytest.mark.parametrize("mutate", [
    lambda a: a + ["--no-herdr"],                                          # an extra flag
    lambda a: [x if x != "--until-accepted" else "--iterations" for x in a],
    lambda a: [x if x != "none" else "strict" for x in a],                 # another isolation
])
def test_b_port_post_start_refuses_any_command_line_but_the_registered_one(env, mutate):
    ports, spec, h, st = started_b(env)
    write_cmdline(env, mutate(st["argv"]))
    with pytest.raises(b1.Bench1Error, match="registered command line"):
        ports.post_start_readback(spec, h)


def test_b_port_post_start_without_a_session_record_is_not_open(env):
    clock = iter(range(0, 1000, 5)).__next__
    ports, spec, h, st = started_b(env, clock=lambda: float(clock()))
    write_cmdline(env, st["argv"])
    post = ports.post_start_readback(spec, h)
    assert post["session_open"] is False and post["scratch_under_storage_root"] is False
    assert post["trust_recorded"] is False


@pytest.mark.parametrize("doc", [None, {"worktree": "/somewhere/else", "registered": ["x"]},
                                 {"registered": ["x"]}, {"worktree": "WT", "registered": []}, ["not", "an", "object"]])
def test_b_port_trust_is_the_run_record_of_this_worktree_not_the_start_text(env, doc):
    clock = iter(range(0, 1000, 5)).__next__
    ports, spec, h, st = started_b(env, clock=lambda: float(clock()))
    write_cmdline(env, st["argv"])
    write_registrations(env, st, trust=False)
    if doc is not None:
        d = st["cell_root"] / st["run_id"] / "approval"
        d.mkdir(exist_ok=True)
        text = json.dumps(doc).replace("WT", str(Path(st["wt"]).resolve()))
        (d / "worktree_trust.json").write_text(text, encoding="utf-8")
    assert ports.post_start_readback(spec, h)["trust_recorded"] is False


def test_b_port_wait_true_when_it_ends_false_on_timeout(env):
    ports, _ = make_b(env)
    spec = env.spec()
    ports.build_and_readback(spec)
    h = ports.start(spec)
    assert ports.wait(h, 5) is True
    ports._state[spec.key]["proc"].timeout_with = True
    assert ports.wait(h, 5) is False


def test_b_port_collect_extracts_the_accepted_candidate_and_filters_the_patch(env):
    cand = {"commit": "a" * 40, "tree_digest": "d"}
    env.state.update({"last_accepted_candidate": cand, "usage": {"iterations": 1, "dispatches": 3}})
    calls = []

    def git(argv, **kw):                       # real git for the read-back, a canned diff for the patch
        calls.append(argv)
        if "diff" in argv:
            return subprocess.CompletedProcess(argv, 0, DIFF, "")
        kw.pop("check", None)
        return subprocess.run(argv, check=False, **kw)
    ports, _ = make_b(env, git_runner=git)
    spec = env.spec()
    ports.build_and_readback(spec)
    ports.start(spec)
    st = ports._state[spec.key]
    proj = env.work / "cproj"
    env.paths = SimpleNamespace(**{**vars(env.paths), "claude_projects": proj})
    ports.paths = env.paths
    (proj / bp.claude_slug(st["wt"])).mkdir(parents=True)
    tr = proj / bp.claude_slug(st["wt"]) / "s.jsonl"
    tr.write_text(json.dumps({"message": {"id": "m", "usage": {"input_tokens": 1, "output_tokens": 1}}}) + "\n")
    st["t0"] = 0.0
    rd = st["cell_root"] / st["run_id"]
    (rd / "state.json").write_text(json.dumps(env.state), encoding="utf-8")
    (rd / "telemetry.jsonl").write_text(json.dumps({"artefactual": ["K2", "K1"]}) + "\nnot json\n")
    out = ports.collect(b1.Handle(spec.key, "B", 7777, st["run_id"], 7777))
    assert out["ended"] == "claim" and out["claimed_tree"] == "a" * 40 and out["dispatches"] == 3
    assert out["artefactual"] == ["K1", "K2"] and out["transcripts"] == [str(tr)]
    patch = Path(out["patch_path"]).read_text()
    assert "src/a.py" in patch and ".v1." not in patch and ".archiv" not in patch
    assert any(f"{'b' * 40}..{'a' * 40}" in a for c in calls for a in c)       # base..accepted, not HEAD


def test_b_port_refuses_to_start_when_the_storage_guard_stops(env):
    ports, _ = make_b(env)
    ports.guard = bp.StorageGuard((str(env.work),), statvfs=vfs(free_inodes=10))
    with pytest.raises(bp.StorageStop):
        ports.build_and_readback(env.spec())


# --------------------------------------------------------------------------- arms A0 / A1+


class FakeDispatcher:
    def __init__(self, answers, timeout_ms=2_700_000):
        self.answers = list(answers)
        self.models = {"developer": "claude-sonnet-5-5", "qa": "claude-sonnet-5-5"}
        self.efforts = {"developer": "high", "qa": "high"}
        self.timeout_ms = timeout_ms
        self.approval_policy_digest = "d" * 64
        self.trust_recorded = True
        self.calls, self.closed = [], 0

    def dispatch(self, role, prompt):
        self.calls.append((role, prompt))
        return self.answers.pop(0)

    def close_own(self):
        self.closed += 1


def plan_for(env, arm="A1P", wait=5):
    cell = env.paths.cell_root(b1.cell_key(env.task, arm, 1, 1))
    cell.mkdir(parents=True, exist_ok=True)
    return bp.WorkerPlan(key=f"{env.task}.{arm}.1.a1", arm=arm, cell_root=str(cell), worktree=str(env.wt),
                         spec_issue=str(env.issue), arms_dir=str(env.paths.arms_dir),
                         policy_file=str(env.paths.policy_file), hoh_src=None, model="claude-sonnet-5-5",
                         effort="high", timeout_s=2700, dispatch_cap=b1.DISPATCH_CAP[arm], handshake_wait_s=wait)


def git_fake_for_worker(patch="diff --git a/a b/a\n+x\n"):
    return lambda argv, **kw: subprocess.CompletedProcess(argv, 0, patch, "")


def test_worker_plan_roundtrip_and_refusal(work):
    plan = WorkerPlanFactory(work)
    p = work / "plan.json"
    plan.dump(p)
    assert bp.WorkerPlan.load(p) == plan
    q = work / "bad.json"
    q.write_text(json.dumps({"key": "k"}))
    with pytest.raises(b1.Bench1Error):
        bp.WorkerPlan.load(q)


def WorkerPlanFactory(work):
    return bp.WorkerPlan(key="k", arm="A0", cell_root=str(work), worktree=str(work), spec_issue=str(work),
                         arms_dir=str(work), policy_file=str(work), hoh_src=None, model="m", effort="e",
                         timeout_s=1, dispatch_cap=1)


def test_worker_writes_its_readback_before_it_dispatches_and_waits_for_go(env):
    plan = plan_for(env, "A0", wait=3)
    d = FakeDispatcher(["done\nCLAIM: COMPLETE"])
    ticks = {"n": 0}
    cell = Path(plan.cell_root)

    def sleep(s):
        ticks["n"] += 1
        assert (cell / "readback.json").exists() and d.calls == []        # read-back first, no dispatch yet
        if ticks["n"] == 2:
            (cell / "go").write_text("")
    rc = bp.arm_worker_main(plan, lambda p: d, sleep=sleep, runner=git_fake_for_worker())
    assert rc == 0 and len(d.calls) == 1 and d.closed == 1
    rb = json.loads((cell / "readback.json").read_text())
    assert rb["role_timeout_s"] == 2700 and rb["models"]["developer"] == "claude-sonnet-5-5"
    res = json.loads((cell / "result.json").read_text())
    assert res["ended"] == "claim" and set(res["prompt_digests"]) == {
        "task_wrapper.md", "A0.md", "A1plus_dev.md", "A1plus_review.md"}
    assert "The bug." in d.calls[0][1] and (cell / "patch.diff").exists()
    assert re.fullmatch(r"[0-9a-f]{64}", res["claimed_tree"])           # the claim names the tree it is about


def test_worker_a0_without_a_complete_claim_names_no_tree(env):
    plan = plan_for(env, "A0")
    (Path(plan.cell_root) / "go").write_text("")
    d = FakeDispatcher(["I could not finish.\nCLAIM: INCOMPLETE"])
    assert bp.arm_worker_main(plan, lambda p: d, sleep=lambda s: None, runner=git_fake_for_worker()) == 0
    res = json.loads((Path(plan.cell_root) / "result.json").read_text())
    assert res["ended"] == "no_claim" and "claimed_tree" not in res


def test_worker_readback_reports_the_built_dispatchers_deadline_not_the_plans(env):
    plan = plan_for(env, "A0")                      # the plan says 2700 s
    d = FakeDispatcher([], timeout_ms=1_800_000)    # the object that would reach the provider says 1800 s
    d.models["developer"] = "claude-opus-5-5"
    rc = bp.arm_worker_main(plan, lambda p: d, sleep=lambda s: None, clock=iter([0, 1000]).__next__)
    rb = json.loads((Path(plan.cell_root) / "readback.json").read_text())
    assert rc == 3 and d.calls == []                # no go: nothing dispatched
    assert rb["role_timeout_s"] == 1800 and rb["models"]["developer"] == "claude-opus-5-5"


def test_worker_without_go_exits_without_dispatching(env):
    plan = plan_for(env, "A1P", wait=1)
    d = FakeDispatcher([])
    now = iter([0.0, 0.5, 2.0, 3.0]).__next__
    rc = bp.arm_worker_main(plan, lambda p: d, sleep=lambda s: None, clock=now, runner=git_fake_for_worker())
    assert rc == 3 and d.calls == []
    assert not (Path(plan.cell_root) / "result.json").exists()


def test_worker_a1plus_runs_the_two_clean_loop_with_the_registered_prompts(env):
    plan = plan_for(env, "A1P")
    cell = Path(plan.cell_root)
    (cell / "go").write_text("")
    d = FakeDispatcher(["fixed\nCLAIM: COMPLETE", "REVIEW: CLEAN", "REVIEW: CLEAN"])
    rc = bp.arm_worker_main(plan, lambda p: d, sleep=lambda s: None, runner=git_fake_for_worker())
    assert rc == 0
    assert [c[0] for c in d.calls] == ["developer", "reviewer", "reviewer"]
    assert "Fix the issue below" in d.calls[0][1] and "Fix the issue below" not in d.calls[1][1]
    res = json.loads((cell / "result.json").read_text())
    assert res["ended"] == "claim" and res["dispatches"] == 3
    assert sorted(p.name for p in (cell / "rounds").iterdir()) == ["dev1.json", "rev2.json", "rev3.json"]


def test_worker_filters_versioned_copies_out_of_the_final_patch(env):
    plan = plan_for(env, "A0")
    (Path(plan.cell_root) / "go").write_text("")
    d = FakeDispatcher(["x\nCLAIM: COMPLETE"])
    bp.arm_worker_main(plan, lambda p: d, sleep=lambda s: None, runner=git_fake_for_worker(DIFF))
    res = json.loads((Path(plan.cell_root) / "result.json").read_text())
    patch = (Path(plan.cell_root) / "patch.diff").read_text()
    assert res["dropped_sections"] == 2 and ".archiv" not in patch


def test_worker_records_a_close_failure_without_failing_the_arm(env):
    plan = plan_for(env, "A0")
    (Path(plan.cell_root) / "go").write_text("")

    class Bad(FakeDispatcher):
        def close_own(self):
            raise RuntimeError("pane gone")
    rc = bp.arm_worker_main(plan, lambda p: Bad(["x\nCLAIM: COMPLETE"]), sleep=lambda s: None,
                            runner=git_fake_for_worker())
    assert rc == 0 and "pane gone" in (Path(plan.cell_root) / "close-own-error.txt").read_text()


def test_a_arm_port_hands_over_a_plan_deferrals_and_a_go_only_after_the_comparison(env):
    guard = bp.StorageGuard((str(env.work),), statvfs=vfs())
    factory = lambda root: SimpleNamespace(run=lambda *a, **k: SimpleNamespace(stdout=json.dumps(
        {"worktree": str(env.wt)})))
    ports = bp.AArmPorts(env.paths, SimpleNamespace(), guard=guard, task_meta=lambda t: {},
                         hoh_factory=factory, popen=FakePopen, sleep=lambda s: None)
    spec = env.spec("A1P")
    obs = ports.build_and_readback(spec)
    assert obs["not_applicable"] == ["project_resolved", "session_open"]
    assert set(obs["deferred_checks"]) == {"models", "efforts", "role_timeout_s", "trust_recorded",
                                           "scratch_under_storage_root", "policy_digest"}
    plan = bp.WorkerPlan.load(env.paths.cell_root(spec.key) / "plan.json")
    assert plan.arm == "A1P" and plan.dispatch_cap == 9 and plan.timeout_s == 2700
    h = ports.start(spec)
    assert h.leader_pid == h.pgid == 7777 and h.run_id is None
    cell = env.paths.cell_root(spec.key)
    (cell / "readback.json").write_text(json.dumps({
        "models": {"developer": "claude-sonnet-5-5", "qa": "claude-sonnet-5-5"},
        "efforts": {"developer": "high", "qa": "high"}, "role_timeout_s": 2700, "trust_recorded": True,
        "policy_digest": "d" * 64}))
    post = ports.post_start_readback(spec, h)
    assert post["models"] == {"developer": "claude-sonnet-5-5", "reviewer": "claude-sonnet-5-5"}
    assert post["trust_recorded"] is True and post["provider_calls_before_check"] == 0
    assert not (cell / "go").exists()
    ports.release(spec)
    assert (cell / "go").exists()
    with pytest.raises(FileExistsError):
        ports.release(spec)


def test_a_arm_post_start_times_out_without_a_readback(env):
    guard = bp.StorageGuard((str(env.work),), statvfs=vfs())
    clock = iter([0.0, 1.0, 500.0]).__next__
    ports = bp.AArmPorts(env.paths, SimpleNamespace(), guard=guard, task_meta=lambda t: {},
                         hoh_factory=lambda r: SimpleNamespace(run=lambda *a, **k: SimpleNamespace(
                             stdout=json.dumps({"worktree": str(env.wt)}))),
                         popen=FakePopen, sleep=lambda s: None, clock=lambda: clock())
    spec = env.spec("A0")
    ports.build_and_readback(spec)
    h = ports.start(spec)
    with pytest.raises(b1.Bench1Error, match="read-back"):
        ports.post_start_readback(spec, h)


def test_a_arm_collect_reads_the_workers_result(env):
    guard = bp.StorageGuard((str(env.work),), statvfs=vfs())
    ports = bp.AArmPorts(env.paths, SimpleNamespace(), guard=guard, task_meta=lambda t: {},
                         hoh_factory=lambda r: SimpleNamespace(run=lambda *a, **k: SimpleNamespace(
                             stdout=json.dumps({"worktree": str(env.wt)}))),
                         popen=FakePopen)
    ports.paths = SimpleNamespace(**{**vars(env.paths), "claude_projects": env.work / "no-projects"})
    ports.paths.cell_root = env.paths.cell_root
    ports.paths.task_dir = env.paths.task_dir
    spec = env.spec("A1P")
    ports.build_and_readback(spec)
    h = ports.start(spec)
    cell = env.paths.cell_root(spec.key)
    (cell / "result.json").write_text(json.dumps({"ended": "claim", "claimed_tree": "t", "dispatches": 3,
                                                  "reason": "two consecutive CLEAN", "rounds": [{"kind": "developer"}]}))
    (cell / "patch.diff").write_text("diff --git a b\n")
    out = ports.collect(h)
    assert out["ended"] == "claim" and out["dispatches"] == 3 and out["patch_path"].endswith("patch.diff")
    (cell / "result.json").rename(cell / "result.json.kept")
    assert ports.collect(h)["ended"] == "instrument_failure"


# --------------------------------------------------------------------------- the subcommand mains


def test_prepare_manifest_step_writes_a_sealed_manifest_of_the_stage(work):
    root = work / "camp"
    root.mkdir()
    sample = {"round1": [{"instance_id": "django__django-1"}, {"instance_id": "sympy__sympy-2"}],
              "round2": [{"instance_id": "pytest-dev__pytest-3"}]}
    s = work / "sample.json"
    s.write_text(json.dumps(sample))
    freeze = work / "freeze.json"
    freeze.write_text(json.dumps({"schema": b1.SCHEMA_FREEZE, "policy_digest": "a" * 64,
                                  "house_text_digest": "b" * 64, "isolation": "none", "storage_root": "/store",
                                  "wiring_patterns_digest": "c" * 64, "denied_tools": ["WebFetch", "WebSearch"]}))
    args = SimpleNamespace(campaign_root=str(root), stage="T", step="manifest", task="", freeze=str(freeze),
                           sample=str(s))
    assert bp.main_prepare(args, b1) == 0
    man = json.loads((root / "images" / "manifest.json").read_text())
    assert [e["ref"] for e in man["images"]] == [
        "swebench/sweb.eval.x86_64.django_1776_django-1:latest",
        "swebench/sweb.eval.x86_64.sympy_1776_sympy-2:latest",
        "swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-3:latest"]
    b1.verify_sealed(root / "images" / "CHAIN.jsonl", root / "images" / "manifest.json")
    with pytest.raises(FileExistsError):
        bp.main_prepare(args, b1)                           # a manifest is never replaced


def test_prepare_pull_and_snapshot_steps_demand_a_task_of_the_stage(work):
    root = work / "camp"
    root.mkdir()
    s = work / "sample.json"
    s.write_text(json.dumps({"round1": [{"instance_id": "django__django-1"}]}))
    freeze = work / "freeze.json"
    freeze.write_text(json.dumps({"schema": b1.SCHEMA_FREEZE, "policy_digest": "a" * 64,
                                  "house_text_digest": "b" * 64, "isolation": "none", "storage_root": "/store",
                                  "wiring_patterns_digest": "c" * 64}))
    base = {"campaign_root": str(root), "stage": "T", "freeze": str(freeze), "sample": str(s)}
    with pytest.raises(b1.Bench1Error, match="--task is required"):
        bp.main_prepare(SimpleNamespace(step="pull", task="", **base), b1)
    with pytest.raises(b1.Bench1Error, match="not in this stage"):
        bp.main_prepare(SimpleNamespace(step="pull", task="django__django-999", **base), b1)
    with pytest.raises(b1.Bench1Error, match="validate step"):
        bp.main_prepare(SimpleNamespace(step="validate", task="django__django-1", **base), b1)


# --------------------------------------------------------------------------- source allow-lists for the ports module


SRC = (TOOLS / "bench1_ports.py").read_text(encoding="utf-8")


def test_the_ports_module_starts_no_process_directly_and_calls_only_known_names():
    tree = ast.parse(SRC)
    module_calls, attr_names = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            attr_names.add(n.func.attr)
            v = n.func.value
            if isinstance(v, ast.Name) and v.id in {"os", "shutil", "subprocess", "tempfile", "tarfile", "signal"}:
                module_calls.add(f"{v.id}.{n.func.attr}")
    # no call of a `subprocess` function at all: every process goes through an injected runner or popen
    assert not {c for c in module_calls if c.startswith("subprocess.")}
    assert module_calls == {"os.close", "os.getgid", "os.getuid", "os.open", "os.walk", "os.write", "os.fsync",
                            "tarfile.open", "tempfile.TemporaryDirectory"}
    allowed = {
        'Bench1Error', 'BudgetLedger', 'CellSpec', 'DatasetHF', 'Handle', 'HiddenRows', 'HohCli',
        'ProcessGroups', 'TemporaryDirectory', '__init__', 'append', 'argv', 'asdict', 'cell_key',
        'cell_root', 'check', 'clock', 'close', 'close_own', 'compile', 'dataclass', 'decode', 'dispatch',
        'docker_argv_allowed', 'dump', 'dumps', 'encode', 'ending_from_state', 'entry_gate', 'exec_module',
        'exists', 'export_testbed', 'extend', 'extractall', 'findall', 'format', 'fsync', 'fullmatch',
        'get', 'getgid', 'getuid', 'glob', 'group', 'hexdigest', 'hoh_factory', 'home', 'insert', 'is_dir',
        'is_file', 'is_symlink', 'isdev', 'isfifo', 'items', 'iterdir', 'join', 'load', 'loads', 'lower',
        'lstrip', 'make_snapshot', 'match', 'mkdir', 'module_from_spec', 'new_state', 'open', 'parse_args',
        'parse_claim', 'parse_run_argv', 'popen', 'pull', 'pull_one', 'read_bytes', 'read_json_object',
        'read_regular_bytes', 'readings', 'regrade_cell', 'replace', 'require_herdr', 'resolve', 'rstrip',
        'run', 'run_a1plus', 'run_cell', 'runner', 'search', 'sha256', 'sleep', 'snapshot_report',
        'spec_from_file_location', 'split', 'splitlines', 'startswith', 'stat', 'statvfs', 'strip', 'sub',
        'surface_liveness', 'task_dir', '_fold', 'cache_env', 'evaluate', 'fetch', 'is_relative_to', 'lexists', 'task_meta', 'time_ns', 'under', 'update', 'utcstamp', 'wait',
        'walk', 'with_name', 'worktree_maker', 'write', 'write_create_only', 'write_manifest', 'write_text'}
    # a frozen list of every attribute-call name of the module: a new name is a new review item; the
    # removal-, move- and signal-class names must be absent from it
    forbidden = {"unlink", "rmdir", "rmtree", "remove", "removedirs", "truncate", "kill", "killpg", "system",
                 "chmod", "chown", "symlink", "link", "move", "copytree", "copy2", "copyfile", "write_bytes",
                 "touch", "popen_unsafe"}
    assert not (attr_names & forbidden), sorted(attr_names & forbidden)
    assert attr_names <= allowed, sorted(attr_names - allowed)


def test_the_ports_module_git_verbs_are_the_frozen_five():
    assert bp.PORTS_GIT_VERBS == ("rev-parse", "diff", "status", "ls-files", "add")
    calls = [n for n in ast.walk(ast.parse(SRC)) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "run_git"]
    verbs = {c.args[1].value for c in calls if len(c.args) > 1 and isinstance(c.args[1], ast.Constant)}
    assert verbs <= set(bp.PORTS_GIT_VERBS), verbs


def test_the_ports_module_builds_one_container_vector_and_only_through_the_tools_helpers():
    assert SRC.count('"docker"') == 1                       # export_argv; every other vector is the tool's
    assert "b1._EPHEMERAL" in SRC and "b1.docker_argv_allowed" in SRC
    assert "docker_argv(" not in SRC.replace("b1.docker_argv_allowed", "")  # nothing builds vectors by hand


# =========================================================================== REVISION 1
# (reviews bench1portsspec-a / -b: liveness, the grading cache, one grading class, the real image layout, the
# issue source, the subcommand layer, one negative case per read-back key, the symlink-escape member)


# --------------------------------------------------------------------------- the image layout (A4) and the export (B3)


def synthetic_testbed(root: Path) -> Path:
    """The layout of a SWE-bench image's `/testbed`: a work tree with its own `.git`, a nested `.git` of a
    vendored checkout, build artefacts."""
    tb = root / "testbed"
    for rel, data in {".git/HEAD": "ref: refs/heads/main\n", ".git/objects/aa/bb": "obj", "manage.py": "print(1)\n",
                      "pkg/__init__.py": "", "pkg/_version.py": "v = '1'\n", "pkg.egg-info/PKG-INFO": "Name: pkg\n",
                      "vendor/lib/.git/config": "[core]\n", "vendor/lib/lib.py": "x = 1\n"}.items():
        f = tb / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(data, encoding="utf-8")
    return tb


class LocalTar:
    """A runner that really executes the export vector's `tar` part on a local directory standing for `/testbed`,
    so that what the vector excludes is what real `tar` excludes (the subprocess lives in the test, not the tool)."""

    def __init__(self, testbed: Path):
        self.testbed, self.calls = testbed, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        local = [str(self.testbed) if a == "/testbed" else a for a in argv[argv.index("tar"):]]
        done = subprocess.run(local, stdout=kw["stdout"], stderr=subprocess.PIPE, check=False)
        return subprocess.CompletedProcess(argv, done.returncode, "", done.stderr)


def test_prepare_snapshot_of_a_real_image_layout_excludes_the_images_own_dot_git(work):
    tb = synthetic_testbed(work)
    d = bp.GuardedDocker(bp.StorageGuard(("/",), statvfs=vfs()), runner=LocalTar(tb))
    tmp = work / "tmp"
    tmp.mkdir()
    info = bp.prepare_snapshot(d, IMG, work / "task", tmp)       # without --exclude: make_snapshot refuses `.git`
    snap = work / "task" / "snap"
    assert info["report"]["commits"] == 1 and info["report"]["unreachable_objects"] == 0
    assert (snap / "pkg" / "_version.py").exists() and (snap / "pkg.egg-info" / "PKG-INFO").exists()
    assert (snap / "vendor" / "lib" / "lib.py").exists()
    assert not (snap / "vendor" / "lib" / ".git").exists()          # a nested .git is skipped by make_snapshot
    assert not (snap / ".git" / "objects" / "aa").exists()          # the snapshot's own history, not the image's


def test_the_unfiltered_export_of_that_layout_would_be_refused_by_make_snapshot(work):
    tb = synthetic_testbed(work)
    unfiltered = subprocess.run(["tar", "-C", str(tb), "-cf", "-", "."], capture_output=True, check=True).stdout
    d = bp.GuardedDocker(bp.StorageGuard(("/",), statvfs=vfs()), runner=Frames(tar_bytes=unfiltered))
    tmp = work / "tmp"
    tmp.mkdir()
    with pytest.raises(b1.Bench1Error):
        bp.prepare_snapshot(d, IMG, work / "task", tmp)


def test_export_testbed_refuses_a_symlink_that_leaves_the_destination_and_writes_nothing(work):
    link = tarfile.TarInfo("lnk")
    link.type, link.linkname = tarfile.SYMTYPE, "../outside"
    payload = tarfile.TarInfo("lnk/pwned")
    payload.size = 1
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:               # the member pre-check sees nothing wrong here
        t.addfile(link)
        t.addfile(payload, io.BytesIO(b"x"))
    d = bp.GuardedDocker(bp.StorageGuard(("/",), statvfs=vfs()), runner=Frames(tar_bytes=buf.getvalue()))
    with pytest.raises(b1.Bench1Error, match="unsafe"):
        d.export_testbed(IMG, work / "src", work)
    assert not (work / "outside").exists()


def test_the_export_is_guarded_before_the_runtime_is_called(work):
    runner = Frames(tar_bytes=make_tar({"a.py": b"x"}))
    d = bp.GuardedDocker(bp.StorageGuard(("/",), statvfs=vfs(free_inodes=10)), runner=runner)
    with pytest.raises(bp.StorageStop):
        d.export_testbed(IMG, work / "src", work)
    assert runner.calls == []


def test_pull_costs_are_kept_between_processes_so_the_projection_is_not_dead(work):
    inodes = iter([5_000_000, 4_900_000])

    def sv(path):
        return SimpleNamespace(f_favail=next(inodes), f_blocks=1000, f_bavail=500)
    runner = Frames()
    first = bp.GuardedDocker(bp.StorageGuard(("/",), statvfs=sv), runner=runner, delta_dir=work / "deltas")
    first.pull("img/a:1")
    assert len(list((work / "deltas").glob("*.json"))) == 1
    second = bp.GuardedDocker(bp.StorageGuard(("/",), statvfs=vfs(free_inodes=3_050_000)), runner=runner,
                              delta_dir=work / "deltas")
    assert second.deltas == [100_000]                           # a fresh process knows what the last pull cost
    with pytest.raises(bp.StorageStop):
        second.pull("img/b:1")                                  # 1.5 x 100 000 > the 50 000 of margin


def test_a_pull_record_that_is_not_a_count_is_refused(work):
    (work / "deltas").mkdir()
    (work / "deltas" / "1.json").write_text(json.dumps({"inodes": True}))
    with pytest.raises(b1.Bench1Error, match="inode count"):
        bp.load_deltas(work / "deltas")


# --------------------------------------------------------------------------- the issue source (A5)


ROW = {"instance_id": "django__django-12325", "repo": "django/django", "base_commit": "c" * 40}
PUB = {"django__django-12325": {"repo": "django/django", "base_commit": "c" * 40,
                                "problem_statement": "Crash when X.\n\nSteps:\n1. do it\n"}}


def test_the_issue_is_the_dataset_problem_statement_verbatim_and_create_only(work):
    text = PUB[ROW["instance_id"]]["problem_statement"]
    n = bp.write_issue(work / "task", ROW["instance_id"], ROW, PUB,
                       {"django__django-12325": {"problem_chars": len(text)}})
    assert n == len(text)
    assert (work / "task" / "issue.txt").read_bytes() == text.encode("utf-8")
    assert bp.write_issue(work / "task", ROW["instance_id"], ROW, PUB) == n          # the same text: a no-op
    other = {ROW["instance_id"]: {**PUB[ROW["instance_id"]], "problem_statement": "Another text."}}
    with pytest.raises(b1.Bench1Error, match="another text"):
        bp.write_issue(work / "task", ROW["instance_id"], ROW, other)


@pytest.mark.parametrize("pub,meta,why", [
    ({}, None, "no problem statement"),
    ({"django__django-12325": {"repo": "django/django", "base_commit": "c" * 40, "problem_statement": "  \n"}},
     None, "no problem statement"),
    ({"django__django-12325": {"repo": "sympy/sympy", "base_commit": "c" * 40, "problem_statement": "x"}},
     None, "not the sample's instance"),
    ({"django__django-12325": {"repo": "django/django", "base_commit": "d" * 40, "problem_statement": "x"}},
     None, "not the sample's instance"),
    (PUB, {"django__django-12325": {"problem_chars": 5}}, "frozen metadata"),
    (PUB, {}, "frozen metadata"),
])
def test_the_issue_is_refused_unless_it_is_the_samples_instance_and_the_frozen_length(work, pub, meta, why):
    with pytest.raises(b1.Bench1Error, match=why):
        bp.write_issue(work / "task", ROW["instance_id"], ROW, pub, meta)
    assert not (work / "task" / "issue.txt").exists()


# --------------------------------------------------------------------------- the commands, behaviourally (B1)


def campaign_files(work, stage_rows=None):
    root = work / "camp"
    root.mkdir()
    rows = stage_rows or [dict(ROW, version="3.0")]
    sample = work / "sample.json"
    sample.write_text(json.dumps({"stage0": rows, "round1": rows, "round2": []}))
    freeze = work / "freeze.json"
    freeze.write_text(json.dumps({"schema": b1.SCHEMA_FREEZE, "policy_digest": "a" * 64,
                                  "house_text_digest": "b" * 64, "isolation": "none", "storage_root": "/store",
                                  "wiring_patterns_digest": b1.WiringPatterns.load(TOOLS / "bench1_wiring_patterns.txt").digest,
                                  "denied_tools": ["WebFetch", "WebSearch"]}))
    return root, sample, freeze


class FakePublic:
    def __init__(self, rows):
        self.rows, self.asked = rows, []

    def fetch(self, ids):
        self.asked.append(list(ids))
        return self.rows


def test_prepare_snapshot_step_writes_issue_snapshot_and_bench_test(work):
    root, sample, freeze = campaign_files(work)
    tb = synthetic_testbed(work)
    made = []

    def docker_factory(guard, delta_dir=None):
        made.append(delta_dir)
        return bp.GuardedDocker(guard, runner=LocalTar(tb), delta_dir=delta_dir)
    pub = FakePublic(PUB)
    args = SimpleNamespace(campaign_root=str(root), stage="0", step="snapshot", task=ROW["instance_id"],
                           freeze=str(freeze), sample=str(sample), meta="")
    assert bp.main_prepare(args, b1, docker_factory=docker_factory, public_source=pub, statvfs=vfs()) == 0
    task_dir = root / "work" / ROW["instance_id"]
    assert pub.asked == [[ROW["instance_id"]]] and made == [root / "images" / "inode-deltas"]
    assert (task_dir / "issue.txt").read_text() == PUB[ROW["instance_id"]]["problem_statement"]
    assert (task_dir / "snap" / "manage.py").exists() and not (task_dir / "snap" / ".git" / "aa").exists()
    script = task_dir / "bench-test"
    assert script.exists() and os.access(script, os.X_OK) and IMG in script.read_text()


def test_prepare_snapshot_step_stops_on_the_storage_guard_before_anything_is_written(work):
    root, sample, freeze = campaign_files(work)
    asked = []

    def low(path):
        asked.append(path)
        return SimpleNamespace(f_favail=10, f_blocks=1000, f_bavail=500)
    args = SimpleNamespace(campaign_root=str(root), stage="0", step="snapshot", task=ROW["instance_id"],
                           freeze=str(freeze), sample=str(sample), meta="")
    pub = FakePublic(PUB)
    with pytest.raises(bp.StorageStop):
        bp.main_prepare(args, b1, public_source=pub, statvfs=low,
                        docker_factory=lambda guard, delta_dir=None: pytest.fail("docker must not be reached"))
    assert pub.asked == [] and not (root / "work").exists() and set(asked) == {str(root), "/"}


def make_cell_args(root, work, sample, freeze, stage="0", task=ROW["instance_id"], arm="B"):
    approval, ledger, patterns = work / "approval.json", work / "ledger.jsonl", TOOLS / "bench1_wiring_patterns.txt"
    approval.write_text(json.dumps({"schema": b1.SCHEMA_APPROVAL, "stages": {"T": True, "0": True}}))
    return SimpleNamespace(campaign_root=str(root), stage=stage, task=task, arm=arm, rep=1, attempt=1,
                           supersedes=None, freeze=str(freeze), approval=str(approval), ledger=str(ledger),
                           patterns=str(patterns), results=str(work / "res"), sample=str(sample))


def test_run_cell_main_refuses_a_task_that_is_not_in_the_stage(work):
    root, sample, freeze = campaign_files(work)
    args = make_cell_args(root, work, sample, freeze, task="django__django-999")
    with pytest.raises(b1.Bench1Error, match="not in this stage"):
        bp.main_run_cell(args, b1, make_ports=lambda *a: pytest.fail("no ports for a task outside the stage"))


def test_run_cell_main_checks_the_guard_over_the_root_and_the_filesystem_before_any_port(work):
    root, sample, freeze = campaign_files(work)
    args = make_cell_args(root, work, sample, freeze)
    asked = []

    def low(path):
        asked.append(path)
        return SimpleNamespace(f_favail=10, f_blocks=1000, f_bavail=500)
    with pytest.raises(bp.StorageStop):
        bp.main_run_cell(args, b1, statvfs=low, make_ports=lambda *a: pytest.fail("ports must not be built"))
    assert set(asked) == {str(root), "/"}


class OutcomePorts:
    """Wiring that reads back cleanly, then a scripted ending."""

    hoh = SimpleNamespace()
    procs = SimpleNamespace()

    def __init__(self, observed, ended):
        self.observed, self.ended = observed, ended

    def build_and_readback(self, spec):
        return self.observed

    def start(self, spec):
        return b1.Handle(spec.key, spec.arm, pgid=5151, run_id="RUN", leader_pid=5151)

    def wait(self, handle, seconds):
        return True

    def collect(self, handle):
        line = json.dumps({"message": {"id": "m", "usage": {"input_tokens": 1, "output_tokens": 1}}})
        return {"ended": self.ended, "stop_reason": "scripted", "transcript_lines": [line], "dispatches": 1,
                "claimed_tree": "t" * 40 if self.ended == "claim" else None, "transcripts": ["/x/s.jsonl"]}


@pytest.mark.parametrize("ended,rc", [("claim", 0), ("blocked", 0)])
def test_run_cell_main_exit_code_is_zero_for_a_product_outcome(work, capsys, ended, rc):
    root, sample, freeze = campaign_files(work)
    args = make_cell_args(root, work, sample, freeze)
    fr = b1.Freeze.load(Path(args.freeze))
    ports_for = lambda arm, paths, f, guard, meta: OutcomePorts(_good_for(arm, f), ended)
    out = bp.main_run_cell(args, b1, make_ports=ports_for, statvfs=vfs())
    assert out == rc and json.loads(capsys.readouterr().out)["key"]
    assert fr.isolation == "none"


def test_run_cell_main_exit_code_is_one_for_an_instrument_failure(work, capsys):
    root, sample, freeze = campaign_files(work)
    args = make_cell_args(root, work, sample, freeze)
    bad = lambda arm, paths, f, guard, meta: OutcomePorts({**_good_for(arm, f), "herdr_env": False}, "claim")
    assert bp.main_run_cell(args, b1, make_ports=bad, statvfs=vfs()) == 1
    assert json.loads(capsys.readouterr().out)["ended"] == "instrument_failure"


def _good_for(arm, freeze):
    decl = b1.registered_declaration(arm, freeze)
    return {"models": {r: decl.model for r in decl.roles}, "efforts": {r: decl.effort for r in decl.roles},
            "role_timeout_s": {r: decl.role_timeout_s for r in decl.roles}, "dispatch_cap": decl.dispatch_cap,
            "isolation": decl.isolation, "policy_digest": freeze.policy_digest,
            "house_text_digest": freeze.house_text_digest, "denied_tools": ["WebFetch", "WebSearch"],
            "trust_recorded": True, "storage_root_initialised": True, "project_resolved": True,
            "session_open": True, "scratch_under_storage_root": True, "storage_root": freeze.storage_root,
            "herdr_env": True, "hidden_material_in_worktree": False,
            "snapshot": {"commits": 1, "refs": 1, "unreachable_objects": 0}}


def hoh_root_with(work, name, lines=None, link=False):
    root = work / name
    (root / "run1").mkdir(parents=True)
    if lines is not None:
        (root / "run1" / bp.OWNED_AGENTS_FILE).write_text("\n".join(json.dumps(r) for r in lines) + "\n")
    return root


def regrade_fixture(work, hoh_root, key="k1"):
    res = work / "res"
    res.mkdir(exist_ok=True)
    patch = hoh_root / "patch.diff"
    patch.write_text("diff --git a/a b/a\n")
    (res / f"{key}.cell.json").write_text(json.dumps({"key": key, "task": ROW["instance_id"], "passed": True}))
    (res / f"{key}.run.json").write_text(json.dumps({"hoh_root": str(hoh_root), "patch_path": str(patch)}))
    return res


class FakeDataset:
    def __init__(self):
        self.asked = 0

    def fetch(self, ids):
        self.asked += 1
        return {i: {"FAIL_TO_PASS": [], "PASS_TO_PASS": [], "test_patch": "", "patch": "", "base_commit": "c" * 40,
                    "repo": "django/django", "problem_statement": "x"} for i in ids}


class FakeGrader:
    def __init__(self):
        self.calls = []

    def evaluate(self, task, patch_path, row, scratch):
        self.calls.append((task, str(patch_path), str(scratch)))
        return {"resolved": True, "f2p_failed": 0, "p2p_failed": 0}


def regrade_args(res, roots, scratch, key="k1"):
    return SimpleNamespace(results=str(res), key=key, scratch_root=str(scratch), hoh_root=[str(r) for r in roots])


def test_regrade_main_refuses_while_a_dispatchers_pane_is_owned_and_loads_no_rows(work):
    root = hoh_root_with(work, "hoh", lines=[{"event": "surface", "run_id": "run1", "pane_id": "p1",
                                              "role": "developer"}])
    res = regrade_fixture(work, root)
    ds, gr = FakeDataset(), FakeGrader()
    scratch = work / "hid"
    scratch.mkdir()
    with pytest.raises(b1.HiddenRowsRefused):
        bp.main_regrade(regrade_args(res, [root], scratch), b1, hoh_factory=lambda r: FakeHohSurfaces(r),
                        dataset=ds, grader=gr)
    assert ds.asked == 0 and gr.calls == []


class FakeHohSurfaces:
    """`hoh surface list` as the product answers it: blind to the dispatchers' registry."""

    def __init__(self, root):
        self.root = Path(root)

    def surface_list(self):
        return json.dumps({"surfaces": []})


def test_regrade_main_grades_with_the_one_instrument_when_nobody_is_alive(work, capsys):
    root = hoh_root_with(work, "hoh", lines=[{"event": "surface", "run_id": "run1", "pane_id": "p1",
                                              "role": "developer"},
                                             {"event": "release", "run_id": "run1", "pane_id": "p1"}])
    res = regrade_fixture(work, root)
    scratch = work / "hid"
    scratch.mkdir()
    gr = FakeGrader()
    assert bp.main_regrade(regrade_args(res, [root], scratch), b1, hoh_factory=lambda r: FakeHohSurfaces(r),
                           dataset=FakeDataset(), grader=gr) == 0
    assert gr.calls and json.loads(capsys.readouterr().out)["resolved"] is True


def test_regrade_main_refuses_a_run_record_whose_root_liveness_was_not_asked_of(work):
    root = hoh_root_with(work, "hoh", lines=[])
    other = hoh_root_with(work, "other", lines=[])
    res = regrade_fixture(work, root)
    with pytest.raises(b1.Bench1Error, match="liveness was not asked"):
        bp.main_regrade(regrade_args(res, [other], work), b1, hoh_factory=lambda r: FakeHohSurfaces(r),
                        dataset=FakeDataset(), grader=FakeGrader())


def test_the_default_regrade_grader_is_the_pinned_official_grader_that_knows_the_cell_root(work, monkeypatch):
    root = hoh_root_with(work, "hoh", lines=[])
    res = regrade_fixture(work, root)
    scratch = work / "hid"
    scratch.mkdir()
    captured = {}

    def fake_regrade(cell, patch, hidden, grader, results):
        captured["grader"] = grader
        return {"resolved": True}
    monkeypatch.setattr(b1, "regrade_cell", fake_regrade)
    bp.main_regrade(regrade_args(res, [root], scratch), b1, hoh_factory=lambda r: FakeHohSurfaces(r),
                    dataset=FakeDataset())
    assert isinstance(captured["grader"], bp.OfficialGrader) and captured["grader"].agent_roots == (root,)
    run = json.loads((res / "k1.run.json").read_text())                  # a run record that names its task directory
    run["snapshot_path"] = str(work / "camp" / "work" / "t-1" / "snap")
    captured.clear()
    (res / "k2.cell.json").write_text((res / "k1.cell.json").read_text().replace("k1", "k2"))
    (res / "k2.run.json").write_text(json.dumps(run))
    bp.main_regrade(regrade_args(res, [root], scratch, key="k2"), b1, hoh_factory=lambda r: FakeHohSurfaces(r),
                    dataset=FakeDataset())
    assert captured["grader"].agent_roots == (root, work / "camp" / "work" / "t-1")


# --------------------------------------------------------------------------- liveness: the dispatchers' registry (A1)


def surfaces(root):
    return json.loads(bp.OwnedAgentsPort(root).surface_list())["surfaces"]


def test_the_registry_port_sees_the_pane_hoh_surface_list_cannot_see(work):
    root = hoh_root_with(work, "hoh", lines=[{"event": "surface", "run_id": "run1", "pane_id": "p1",
                                              "role": "developer", "tab_id": "t"}])
    assert b1.parse_surface_list(FakeHohSurfaces(root).surface_list()) == []          # the product's list: blind
    assert b1.surface_liveness(bp.liveness_ports([FakeHohSurfaces(root)]))() == ["run1:p1"]


def test_a_released_pane_is_not_alive_and_an_adopted_one_is(work):
    root = hoh_root_with(work, "hoh", lines=[
        {"event": "surface", "run_id": "r", "pane_id": "p1", "role": "developer"},
        {"event": "release", "run_id": "r", "pane_id": "p1"},
        {"event": "adopt", "run_id": "r", "pane_id": "p2", "role": "qa"},
        {"event": "agent", "run_id": "r", "pane_id": "p2", "name": "n"}])
    assert b1.parse_surface_list(json.dumps({"surfaces": surfaces(root)})) == ["r:p2"]


def test_the_registry_of_an_a_arms_worker_lives_in_the_cell_root_itself(work):
    root = work / "cell"
    root.mkdir()
    (root / bp.OWNED_AGENTS_FILE).write_text(json.dumps({"event": "surface", "run_id": "c", "pane_id": "p9",
                                                         "role": "developer"}) + "\n")
    assert b1.parse_surface_list(json.dumps({"surfaces": surfaces(root)})) == ["c:p9"]


@pytest.mark.parametrize("content", ["not json\n", json.dumps({"event": "boom", "run_id": "r", "pane_id": "p"}),
                                     json.dumps({"event": "surface", "run_id": "r"}), "[1]"])
def test_an_unreadable_registry_line_makes_liveness_unknown_which_is_alive(work, content):
    root = hoh_root_with(work, "hoh")
    (root / "run1" / bp.OWNED_AGENTS_FILE).write_text(content + "\n")
    assert "problem" in surfaces(root)[0]
    with pytest.raises(b1.Bench1Error):
        b1.parse_surface_list(json.dumps({"surfaces": surfaces(root)}))


def test_a_symlinked_registry_a_symlinked_run_dir_and_a_missing_root_are_unknown(work):
    root = hoh_root_with(work, "hoh")
    real = work / "elsewhere.jsonl"
    real.write_text("")
    os.symlink(real, root / "run1" / bp.OWNED_AGENTS_FILE)
    assert "problem" in surfaces(root)[0]
    root2 = work / "hoh2"
    root2.mkdir()
    os.symlink(root, root2 / "linked-run")
    assert "problem" in surfaces(root2)[0]
    assert "problem" in surfaces(work / "does-not-exist")[0]


def test_an_empty_root_has_nobody_alive(work):
    root = work / "empty"
    root.mkdir()
    assert surfaces(root) == []


# --------------------------------------------------------------------------- the grading instrument and its cache (A2, A3)


class RecordingRunner:
    def __init__(self, scratch_report=True):
        self.calls, self.report = [], scratch_report

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.report:
            run_id = argv[argv.index("--run_id") + 1]
            task = argv[argv.index("--instance_ids") + 1]
            d = Path(kw["cwd"]) / "logs" / "run_evaluation" / run_id / "bench1" / task
            d.mkdir(parents=True)
            (d / "report.json").write_text(json.dumps({task: {"resolved": True, "tests_status": {
                "FAIL_TO_PASS": {"success": ["a"], "failure": []}, "PASS_TO_PASS": {"success": [], "failure": ["b"]}}}}))
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_the_grader_pins_every_dataset_cache_inside_its_scratch(work):
    scratch = work / "hid"
    scratch.mkdir()
    runner = RecordingRunner()
    patch = work / "cell" / "patch.diff"
    patch.parent.mkdir()
    patch.write_text("diff --git a/a b/a\n")
    out = bp.OfficialGrader(runner=runner, agent_roots=[work / "roots"]).evaluate(
        "django__django-12325", patch, {}, scratch)
    assert out == {"resolved": True, "f2p_failed": 0, "p2p_failed": 1}
    env = runner.calls[0][1]["env"]
    for var in ("XDG_CACHE_HOME", "HF_HOME", "HF_DATASETS_CACHE", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE",
                "TRANSFORMERS_CACHE"):
        assert Path(env[var]).resolve().is_relative_to(scratch.resolve()), var
    assert runner.calls[0][1]["cwd"] == str(scratch)


@pytest.mark.parametrize("where", ["inside", "same", "around"])
def test_the_grader_refuses_a_scratch_that_overlaps_a_path_an_arm_can_read(work, where):
    agent = work / "roots"
    agent.mkdir()
    scratch = {"inside": agent / "hid", "same": agent, "around": work}[where]
    scratch.mkdir(exist_ok=True)
    runner = RecordingRunner()
    with pytest.raises(b1.Bench1Error, match="overlaps"):
        bp.OfficialGrader(runner=runner, agent_roots=[agent]).evaluate("t-1", None, {}, scratch)
    assert runner.calls == []


def test_the_grader_refuses_a_scratch_around_the_cell_that_holds_the_patch(work):
    cell = work / "hid" / "cell"
    cell.mkdir(parents=True)
    (cell / "patch.diff").write_text("")
    with pytest.raises(b1.Bench1Error, match="overlaps"):
        bp.OfficialGrader(runner=RecordingRunner()).evaluate("t-1", cell / "patch.diff", {}, work / "hid")


def test_agent_visible_roots_are_the_campaign_areas_an_arm_works_in(env):
    roots = bp.agent_visible_roots(env.paths)
    assert set(roots) == {env.paths.work, env.paths.roots, env.paths.store, env.paths.tmp,
                          env.paths.claude_projects}
    hidden = env.paths.root / "hidden-scratch"                  # the runbook's $HID: beside work/, roots/, store/
    hidden.mkdir()
    bp.assert_outside(hidden, roots)


def test_entry_gate_and_the_cells_are_graded_by_the_same_instrument(work):
    scratch = work / "hid"
    scratch.mkdir()
    runner = RecordingRunner()
    grader = bp.build_grader(agent_roots=[work / "roots"], runner=runner)
    assert type(grader) is bp.OfficialGrader
    run = bp.official_run(grader, "django__django-12325", {}, scratch)
    assert run("diff --git a/a b/a\n") == {"f2p_failed": 0, "p2p_failed": 1}
    assert run(None) == {"f2p_failed": 0, "p2p_failed": 1}
    assert len(runner.calls) == 2 and all(c[0][1:3] == ["-m", "swebench.harness.run_evaluation"] for c in runner.calls)


# --------------------------------------------------------------------------- the worker ends cleanly on SIGTERM (A1, kill side)


def test_the_worker_closes_its_panes_when_the_kill_path_signals_it(env):
    plan = plan_for(env, "A0")
    (Path(plan.cell_root) / "go").write_text("")

    class Dying(FakeDispatcher):
        def dispatch(self, role, prompt):
            handler = installed[signal.SIGTERM]
            handler(signal.SIGTERM, None)                       # SIGTERM arrives in the middle of a dispatch
            raise AssertionError("not reached")
    d = Dying([])
    installed = {}
    with pytest.raises(SystemExit) as exc:
        bp.arm_worker_main(plan, lambda p: d, sleep=lambda s: None, runner=git_fake_for_worker(),
                           set_signal=lambda sig, fn: installed.update({sig: fn}))
    assert exc.value.code == 143 and d.closed == 1


def test_the_worker_closes_its_panes_on_a_handshake_timeout_and_on_any_exception(env):
    plan = plan_for(env, "A0", wait=3)
    d = FakeDispatcher([])
    ticks = iter([0, 1, 10]).__next__
    assert bp.arm_worker_main(plan, lambda p: d, sleep=lambda s: None, clock=lambda: ticks(),
                              set_signal=lambda *a: None) == 3 and d.closed == 1
    plan2 = plan_for(env, "A1P")
    (Path(plan2.cell_root) / "go").write_text("")
    d2 = FakeDispatcher([])                                     # no answers: the first dispatch raises IndexError
    with pytest.raises(IndexError):
        bp.arm_worker_main(plan2, lambda p: d2, sleep=lambda s: None, runner=git_fake_for_worker(),
                           set_signal=lambda *a: None)
    assert d2.closed == 1


def test_the_workers_readback_says_what_its_dispatcher_says_about_trust(env):
    for held, expect in ((False, False), (True, True), (None, False)):          # None: the attribute is absent
        plan = plan_for(env, "A0")
        cell = Path(plan.cell_root)
        for leftover in cell.glob("readback.json"):
            leftover.rename(cell / f"readback.{time.time_ns()}.old")
        d = FakeDispatcher([])
        if held is None:
            del d.trust_recorded
        else:
            d.trust_recorded = held
        ticks = iter([0, 10])
        bp.arm_worker_main(plan, lambda p, d=d: d, sleep=lambda s: None, clock=ticks.__next__,
                           set_signal=lambda *a: None)
        assert json.loads((cell / "readback.json").read_text())["trust_recorded"] is expect


def test_the_worker_installs_a_sigterm_handler_that_exits_143(env):
    plan = plan_for(env, "A0", wait=3)
    installed = {}
    clock = iter([0, 10]).__next__
    bp.arm_worker_main(plan, lambda p: FakeDispatcher([]), sleep=lambda s: None, clock=lambda: clock(),
                       set_signal=lambda sig, fn: installed.update({sig: fn}))
    assert set(installed) == {signal.SIGTERM}
    with pytest.raises(SystemExit) as exc:
        installed[signal.SIGTERM](signal.SIGTERM, None)
    assert exc.value.code == 143


# --------------------------------------------------------------------------- one negative case per read-back key (B2)


def a_ports(env, **kw):
    guard = bp.StorageGuard((str(env.work),), statvfs=vfs())
    kw.setdefault("popen", FakePopen)
    ports = bp.AArmPorts(env.paths, SimpleNamespace(), guard=guard, task_meta=lambda t: {},
                         hoh_factory=lambda r: SimpleNamespace(run=lambda *a, **k: SimpleNamespace(
                             stdout=json.dumps({"worktree": str(env.wt)}))),
                         sleep=lambda s: None, **kw)
    return ports


def a_findings(env, arm="A1P", paths=None, **overrides):
    ports = a_ports(env)
    if paths is not None:
        ports.paths = paths
    spec = env.spec(arm)
    obs = ports.build_and_readback(spec)
    h = ports.start(spec)
    pol = bp.policy_readback(env.paths.policy_file)
    rb = {"models": {"developer": "claude-sonnet-5-5", "qa": "claude-sonnet-5-5"},
          "efforts": {"developer": "high", "qa": "high"}, "role_timeout_s": 2700, "trust_recorded": True,
          "policy_digest": pol["policy_digest"]}
    rb.update(overrides)
    (env.paths.cell_root(spec.key) / "readback.json").write_text(json.dumps(rb))
    post = ports.post_start_readback(spec, h)
    patterns = b1.WiringPatterns.load(TOOLS / "bench1_wiring_patterns.txt")
    decl = b1.registered_declaration(arm, freeze_for(env, patterns))
    first = b1.compare_wiring(decl, obs, arm)
    later = b1.compare_wiring(decl, {**obs, **post}, arm, only=frozenset(obs["deferred_checks"]))
    return first, later


def test_a_arm_clean_readback_has_no_finding_in_either_pass(env):
    first, later = a_findings(env)
    assert first == [] and later == []


@pytest.mark.parametrize("arm,override,what", [
    ("A1P", {"models": {"developer": "claude-sonnet-5-5", "qa": "claude-opus-5-5"}}, "models[reviewer]"),
    ("A1P", {"models": {"developer": "claude-opus-5-5", "qa": "claude-sonnet-5-5"}}, "models[developer]"),
    ("A1P", {"efforts": {"developer": "high", "qa": "low"}}, "efforts[reviewer]"),
    ("A0", {"role_timeout_s": 1800}, "role_timeout_s[developer]"),
    ("A1P", {"trust_recorded": False}, "trust_recorded"),
    ("A1P", {"policy_digest": "f" * 64}, "policy_digest"),
])
def test_a_arm_every_worker_readback_key_is_compared(env, arm, override, what):
    _, later = a_findings(env, arm, **override)
    assert [f["what"] for f in later] == [what]


def test_a_arm_worktree_outside_the_campaign_root_is_a_scratch_finding(env):
    import dataclasses
    elsewhere = dataclasses.replace(env.paths, root=env.work / "elsewhere")
    _, later = a_findings(env, paths=elsewhere)
    assert [f["what"] for f in later] == ["scratch_under_storage_root"]


def test_a_arm_a_dirty_worktree_is_hidden_material_in_the_worktree(env):
    (env.wt / "stray.py").write_text("x = 1\n", encoding="utf-8")
    first, _ = a_findings(env)
    assert [f["what"] for f in first] == ["hidden_material_in_worktree"]


def test_a_arm_the_worker_is_a_session_leader_so_the_group_kill_reaches_only_it(env):
    seen = {}

    def popen(argv, **kw):
        seen.update(kw)
        return FakePopen(argv, **kw)
    ports = a_ports(env, popen=popen)
    spec = env.spec("A1P")
    ports.build_and_readback(spec)
    ports.start(spec)
    assert seen["start_new_session"] is True and seen["env"]["TMPDIR"] == str(env.paths.tmp)


def test_a_arm_that_was_never_released_has_made_no_dispatch(env):
    ports = a_ports(env)
    spec = env.spec("A1P")
    ports.build_and_readback(spec)
    h = ports.start(spec)
    ports.paths = SimpleNamespace(**{**vars(env.paths), "claude_projects": env.work / "none"})
    ports.paths.cell_root, ports.paths.task_dir = env.paths.cell_root, env.paths.task_dir
    assert ports.collect(h)["dispatches"] == 0                  # no `go`, no result: zero, not unknown
    ports.release(spec)
    assert ports.collect(h)["dispatches"] is None               # released and silent: unknown


def test_b_port_dispatch_cap_is_read_from_the_run_state(env):
    env.state["budgets"]["max_dispatches"] = 8
    ports, _ = make_b(env)
    obs = ports.build_and_readback(env.spec())
    patterns = b1.WiringPatterns.load(TOOLS / "bench1_wiring_patterns.txt")
    found = b1.compare_wiring(b1.registered_declaration("B", freeze_for(env, patterns)), obs, "B")
    assert [f["what"] for f in found] == ["dispatch_cap"] and found[0]["observed"] == 8


def parse_with(**over):
    def parse(argv):
        ns = parse_ns(argv)
        for k, v in over.items():
            setattr(ns, k, v)
        return ns
    return parse


def b_found(env, **kw):
    ports, _ = make_b(env, **kw)
    obs = ports.build_and_readback(env.spec())
    patterns = b1.WiringPatterns.load(TOOLS / "bench1_wiring_patterns.txt")
    return obs, b1.compare_wiring(b1.registered_declaration("B", freeze_for(env, patterns)), obs, "B")


def test_b_port_isolation_is_what_the_products_parser_returned_not_what_was_passed(env):
    _, found = b_found(env, parse=parse_with(isolation="strict"))
    assert [f["what"] for f in found] == ["isolation"] and found[0]["observed"] == "strict"


def test_b_port_role_timeout_is_what_the_products_parser_returned_not_a_constant(env):
    obs, found = b_found(env, parse=parse_with(role_timeout=1800))
    assert obs["role_timeout_s"] == {"planner": 1800, "developer": 1800, "qa": 1800}
    assert sorted(f["what"] for f in found) == ["role_timeout_s[developer]", "role_timeout_s[planner]",
                                               "role_timeout_s[qa]"]


def test_b_port_policy_is_the_file_the_product_will_load_not_the_campaigns_default(env):
    (env.work / "camp" / "other-policy.json").write_text(json.dumps(
        {"schema": "hoh-role-approval/1", "mode": "auto", "deny": ["WebFetch", "WebSearch"],
         "house_rules": "# another house text\n"}))
    _, found = b_found(env, parse=parse_with(approval_policy=str(env.work / "camp" / "other-policy.json")))
    assert {f["what"] for f in found} >= {"policy_digest", "house_text_digest"}


def test_b_port_a_dirty_worktree_is_hidden_material(env):
    (env.wt / "stray.py").write_text("x = 1\n", encoding="utf-8")
    obs, found = b_found(env)
    assert obs["hidden_material_in_worktree"] is True and [f["what"] for f in found] == ["hidden_material_in_worktree"]


def test_b_port_a_worktree_on_another_commit_is_hidden_material(env):
    other = _git_repo(env.work, "otherwt", content="different\n")
    env.wt = other
    _, found = b_found(env)
    assert [f["what"] for f in found] == ["hidden_material_in_worktree"]


# --------------------------------------------------------------------------- smaller pins


def test_image_refs_are_lower_case():
    assert bp.image_ref("Owner__Repo-1") == "swebench/sweb.eval.x86_64.owner_1776_repo-1:latest"


def test_only_passed_and_xfail_are_passes_in_pytest_output():
    out = bp.parse_test_results("pytest-dev/pytest", "XPASS a::t\nSKIPPED b::t\nPASSED c::t\nXFAIL d::t\nERROR e::t")
    assert out == {"a::t": "FAILED", "b::t": "FAILED", "c::t": "PASSED", "d::t": "PASSED", "e::t": "FAILED"}


def test_the_entry_gate_refuses_a_boolean_where_a_count_is_expected():
    ok = {"f2p_failed": 0, "p2p_failed": 0}
    bad = {"f2p_failed": True, "p2p_failed": 0}
    out = bp.entry_gate_for_task(lambda p: bad, lambda p: ok, "gold", repeats=1)
    assert out["holds"] is False and "not two counts" in out["reasons"][0]


def test_a_routing_pin_of_another_schema_is_refused(work):
    run = work / "run"
    run.mkdir()
    policy = work / "routing.json"
    policy.write_text(json.dumps(bp.routing_policy_doc()))
    canonical = json.dumps(bp.routing_policy_doc(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    pin = {"schema": "hoh-routing-pin/2", "digest": hashlib.sha256(canonical.encode()).hexdigest(),
           "source": str(policy)}
    (run / "routing.json").write_text(json.dumps(pin))
    with pytest.raises(b1.Bench1Error, match="unexpected shape"):
        bp.routing_readback(run, ["planner"])
    pin["schema"] = "hoh-routing-pin/1"
    (run / "routing.json").write_text(json.dumps(pin))
    assert bp.routing_readback(run, ["planner"])["models"] == {"planner": "claude-sonnet-5-5"}


def test_run_git_isolates_the_home_directory_like_the_tools_own_git(work):
    seen = {}

    def runner(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, "", "")
    bp.run_git(work, "status", runner=runner)
    assert seen["env"]["HOME"] == str(work) and seen["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
