"""P3-11 slice 3: the execution preflight closes its gaps.

Slice 1 built the verdict, slice 2 handed it to every start path; three things were still open
and are pinned here, each by what it catches:

* **A default.** A project that declares nothing used to be gated by nothing. It is gated by the
  product default now (`hoh.preflightgate.default_requirement`: the `unattended` profile, a report
  of at most a day at `<project>/preflight-report.json`); the only way out is a recorded waiver
  (`preflight.json` with schema `hoh-preflight-waiver/1`: who, when, why).
* **A report that says where it was measured.** `tools/preflight.py` writes an `environment`
  object (interpreter, Python environment, repository, runs directory); the gate refuses, by
  name, a report measured for another interpreter, and -- for a launcher -- another repository or
  runs directory.
* **The tools that start runs on their own** (`tools/benchmark.py` arm C, `tools/closure_e2e.py`)
  pass a requirement and start nothing without one.

The production path is driven through `cli.main` (`hoh project drive` -> `drive_governed` ->
`bind_routing` -> `bind_preflight` -> `prepare` -> `check_preflight`) with only the launcher's
process boundary replaced. Nothing here starts Herdr, an agent or a real measurement; everything
lives under `tmp_path`.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_preflight_start_paths import Drive, _after, _report, git
from test_preflight_start_paths import (
    drive as drive,  # a fixture; the alias form is an explicit re-export
)

from hoh import cli
from hoh.launcher import (
    PREFLIGHT_FILE,
    PREFLIGHT_WAIVER_SCHEMA,
    Dispatch,
    HohRunLauncher,
    PreflightWaiver,
)
from hoh.preflightgate import (
    DEFAULT_MAX_AGE_SECONDS,
    DEFAULT_PROFILE,
    DEFAULT_REPORT_NAME,
    MAX_AGE_ENV,
    PROFILE_ENV,
    REPORT_ENV,
    REPORT_TIME_FORMAT,
    PreflightRefused,
    PreflightRequirement,
    check_preflight,
    default_requirement,
    environment_identity,
    requirement_from_environment,
)
from hoh import preflightgate as gate
from hoh.launcher import hoh_command
from hoh.project import TaskNode

ROOT = Path(__file__).resolve().parent.parent

#: tests/conftest.py switches the default preflight off for every module that does not say this
TESTS_THE_DEFAULT_PREFLIGHT = True


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod            # registered first: a `@dataclass` resolves its annotations here
    spec.loader.exec_module(mod)
    return mod


pf = _load("preflight_identity_probe", "tools/preflight.py")

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
WAIVER = {"schema": PREFLIGHT_WAIVER_SCHEMA, "reason": "a development project, no unattended runs",
          "decided_by": "operator", "decided_at": "2026-10-19T08:00:00Z"}


# --------------------------------------------------------------------------- #
# reports and requirements
# --------------------------------------------------------------------------- #

def _ready(environment, *, profile: str = "unattended") -> dict:
    return {"profile": profile, "verdict": "READY", "blocking": [], "inconclusive": [],
            "measured_at_utc": NOW.strftime(REPORT_TIME_FORMAT), "environment": environment}


def _put(tmp_path: Path, body, name: str = "report.json") -> PreflightRequirement:
    path = tmp_path / name
    path.write_text(json.dumps(body), encoding="utf-8")
    return PreflightRequirement(path, "unattended", 3600)


def _check(requirement, **kw):
    return check_preflight(requirement, now=NOW, **kw)


def _refused(requirement, match: str, **kw) -> None:
    with pytest.raises(PreflightRefused, match=match):
        _check(requirement, **kw)


# --------------------------------------------------------------------------- #
# the tool: a report says where it was measured, and measures what it says
# --------------------------------------------------------------------------- #

def _checks(profile: str = "demo") -> list:
    out = [pf.Check(c, "q", pf.PASS, "d", {}) for c in pf.PROFILES[profile]["requires"]]
    out.append(pf.Check("isolation", "q", pf.PASS, "d",
                        {"level": pf.PROFILES[profile]["min_isolation"]}))
    return out


def test_a_report_says_where_it_was_measured(tmp_path):
    """Catches: a report with no `environment` (nothing for the gate to compare), one that names
    the tool's own checkout when a repository was given, and one that leaves the runs directory
    out. The object is the gate's own definition of a place (`environment_identity`), so the
    producer and the consumer cannot drift apart."""
    repo, root = tmp_path / "repo", tmp_path / "root"
    body = pf.report(_checks(), "demo", deep=False, repo=repo, root=root)
    assert body["environment"] == environment_identity(repo=repo, root=root)
    assert set(body["environment"]) == {"interpreter", "prefix", "repo", "root"}
    assert body["environment"]["repo"] == os.path.realpath(repo)
    assert body["environment"]["root"] == os.path.realpath(root)
    # no repository named: this checkout, as before; no runs directory: none
    plain = pf.report(_checks(), "demo", deep=False)["environment"]
    assert plain["repo"] == os.path.realpath(pf.HOH) and plain["root"] is None
    assert plain["interpreter"] == os.path.realpath(sys.executable)
    assert plain["prefix"] == os.path.realpath(sys.prefix)


def test_main_hands_the_named_repository_and_runs_directory_to_the_measurement(
        tmp_path, monkeypatch, capsys):
    """Catches: `--repo` / `--root` parsed and dropped (the report would then say this checkout
    was measured for a launcher whose repository is another). The measurement and the report
    both get exactly what was typed."""
    seen = []

    def measure(deep=False, repo=None, root=None):
        seen.append((deep, repo, root))
        return _checks()

    monkeypatch.setattr(pf, "measure", measure)
    out = tmp_path / "report.json"
    repo, root = tmp_path / "repo", tmp_path / "root"
    pf.main(["--profile", "demo", "--repo", str(repo), "--root", str(root), "--out", str(out)])
    capsys.readouterr()
    assert seen == [(False, repo, root)]
    assert json.loads(out.read_text())["environment"] == environment_identity(repo=repo, root=root)


def test_the_git_checks_measure_the_repository_they_are_given(tmp_path):
    """Catches: `cwd` left at the tool's own checkout. A clean repository is clean, one untracked
    file in it is not -- measured in the repository named, whatever the working directory is."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a").write_text("x\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first")
    assert pf.check_repo_clean(repo).state == pf.PASS
    assert pf.check_git_repo(repo).evidence["mainline"] == "main"
    (repo / "b").write_text("y\n", encoding="utf-8")
    dirty = pf.check_repo_clean(repo)
    assert dirty.state == pf.FAIL and dirty.evidence["count"] == 1
    assert pf.check_git_repo(tmp_path / "not-a-repo-dir-yet").state != pf.PASS


def test_the_disk_check_measures_the_runs_directory_and_a_spot_it_cannot_read_fails(tmp_path):
    """Catches: the runs directory never measured, and a spot that cannot be read passing (a
    path that does not exist is not a disk with room). The named repository and runs directory
    are measured; either one missing is a FAIL that names it."""
    repo, root = tmp_path / "repo", tmp_path / "root"
    repo.mkdir()
    root.mkdir()
    ok = pf.check_disk_headroom(repo=repo, root=root)
    assert ok.state == pf.PASS
    assert ok.evidence["measured"]["root"]["path"] == str(root)
    assert ok.evidence["measured"]["repo"]["path"] == str(repo)
    gone = pf.check_disk_headroom(repo=repo, root=tmp_path / "nowhere")
    assert gone.state == pf.FAIL and "root: cannot be measured" in gone.detail
    gone = pf.check_disk_headroom(repo=tmp_path / "nowhere", root=root)
    assert gone.state == pf.FAIL and "repo: cannot be measured" in gone.detail


# --------------------------------------------------------------------------- #
# the gate: a report for another place is refused, by name
# --------------------------------------------------------------------------- #

def test_a_report_for_this_place_is_accepted_and_one_without_a_place_is_not(tmp_path):
    """The positive control (without it every refusal below passes against a gate that always
    refuses) and the fail-closed edge: a report that has no `environment` at all, or one that is
    not an object, is refused by name -- it is never read as "no place to compare"."""
    here = environment_identity()
    assert _check(_put(tmp_path, _ready(here))).profile == "unattended"
    for label, value in [("absent", None), ("list", []), ("text", "here"), ("number", 3),
                         ("null", "null")]:
        body = _ready(here)
        if label == "absent":
            del body["environment"]
        else:
            body["environment"] = None if value == "null" else value
        _refused(_put(tmp_path, body, f"{label}.json"), r"does not say where it was measured")


@pytest.mark.parametrize("key,label", [("interpreter", "interpreter"),
                                       ("prefix", "Python environment")])
def test_a_report_for_another_interpreter_or_environment_is_refused_by_name(tmp_path, key, label):
    """Catches: L4 -- a READY report measured under another interpreter passing. Another value
    is "measured for another <label>", quoting both; a value that is empty, absent or not text is
    "does not name the <label>"; the other of the two keys being right does not help."""
    here = environment_identity()
    other = {**here, key: here[key] + "-elsewhere"}
    _refused(_put(tmp_path, _ready(other)), rf"measured for another {label}.*elsewhere")
    for bad in ("", None, 7, ["x"]):
        _refused(_put(tmp_path, _ready({**here, key: bad}), "bad.json"),
                 rf"does not name the {label}")
    missing = {k: v for k, v in here.items() if k != key}
    _refused(_put(tmp_path, _ready(missing), "missing.json"), rf"does not name the {label}")


def test_the_interpreter_is_compared_by_its_real_path_and_never_by_an_empty_name(
        tmp_path, monkeypatch):
    """Catches: the spelling of the interpreter compared instead of the interpreter (a link to it
    is the same one), and `sys.executable` empty or None (embedded or broken) matching a report
    that says nothing: the cwd must not become the interpreter."""
    here = environment_identity()
    link = tmp_path / "py-link"
    link.symlink_to(sys.executable)
    monkeypatch.setattr(sys, "executable", str(link))
    assert environment_identity()["interpreter"] == here["interpreter"]
    assert _check(_put(tmp_path, _ready(here))).profile == "unattended"
    for nothing in ("", None):
        monkeypatch.setattr(sys, "executable", nothing)
        assert environment_identity()["interpreter"] == ""
        _refused(_put(tmp_path, _ready({**here, "interpreter": ""}), "empty.json"),
                 r"does not name the interpreter|another interpreter")
    monkeypatch.undo()
    other_prefix = tmp_path / "venv"
    other_prefix.mkdir()
    monkeypatch.setattr(sys, "prefix", str(other_prefix))
    _refused(_put(tmp_path, _ready(here), "prefix.json"), r"another Python environment")


def test_the_repository_and_runs_directory_are_compared_when_the_caller_names_them(tmp_path):
    """Catches: L11 -- a report measured for another repository or runs directory passing, a
    report that names no repository / runs directory passing for a caller that has one, and the
    comparison on the spelling instead of the place (a link, a trailing slash and a `..` path
    are the same place). A caller that names neither (the CLI gate) compares the interpreter
    only."""
    repo, root = tmp_path / "repo", tmp_path / "root"
    repo.mkdir()
    root.mkdir()
    (tmp_path / "other").mkdir()
    req = _put(tmp_path, _ready(environment_identity(repo=repo, root=root)))
    assert _check(req, repo=repo, root=root).profile == "unattended"
    link = tmp_path / "repo-link"
    link.symlink_to(repo)
    assert _check(req, repo=link, root=str(root) + "/").profile == "unattended"
    assert _check(req, repo=tmp_path / "other" / ".." / "repo", root=root).profile == "unattended"
    assert _check(req).profile == "unattended"                       # nothing named: not compared
    _refused(req, r"measured for another repository", repo=tmp_path / "other", root=root)
    # two places that share a long prefix differ at the end, and the refusal shows the end
    long = tmp_path / ("d" * 90)
    (long / "repo-a").mkdir(parents=True)
    (long / "repo-b").mkdir()
    a = _put(tmp_path, _ready(environment_identity(repo=long / "repo-a")), "a.json")
    _refused(a, r"it says .*repo-a'.*this one is .*repo-b'", repo=long / "repo-b")
    _refused(req, r"measured for another runs directory", repo=repo, root=tmp_path / "other")
    unplaced = _put(tmp_path, _ready(environment_identity()), "unplaced.json")
    _refused(unplaced, r"does not name the repository", repo=repo)
    _refused(unplaced, r"does not name the runs directory", root=root)
    for bad in ("", "a\0b", "lone-\ud800-surrogate"):
        with pytest.raises(ValueError, match="must be a path"):
            _check(req, repo=bad)
        with pytest.raises(ValueError, match="must be a path"):
            _check(req, root=bad)


def test_a_refusal_of_the_place_comes_after_the_verdict_and_the_clock(tmp_path):
    """Catches: the place compared before the report is judged at all -- a NOT_READY or stale
    report must keep being refused for its verdict or its age (the words an operator fixes first),
    not for where it was measured."""
    wrong = {**environment_identity(), "interpreter": "/elsewhere/python"}
    body = _ready(wrong)
    body.update(verdict="NOT_READY", blocking=["isolation"])
    _refused(_put(tmp_path, body), r"says NOT_READY: 'isolation'")
    body = _ready(wrong)
    body["measured_at_utc"] = (NOW - timedelta(days=2)).strftime(REPORT_TIME_FORMAT)
    _refused(_put(tmp_path, body, "old.json"), r"s old")


# --------------------------------------------------------------------------- #
# the default, the waiver and the tools' configuration
# --------------------------------------------------------------------------- #

def test_the_default_requirement_is_the_strict_profile_for_a_day_beside_the_project(tmp_path):
    """Catches: a default that is weaker than `unattended`, a ceiling that drifted from one day,
    or a report kept somewhere other than beside the project's own declarations."""
    req = default_requirement(tmp_path / "p")
    assert (req.profile, req.max_age_seconds, req.bind_places) == ("unattended", 86400, True)
    assert req.report == tmp_path / "p" / "preflight-report.json"
    assert (DEFAULT_PROFILE, DEFAULT_MAX_AGE_SECONDS, DEFAULT_REPORT_NAME) == (
        "unattended", 86400, "preflight-report.json")


def test_a_tool_is_configured_from_the_environment_and_has_no_default_report(tmp_path):
    """Catches: a tool that starts quota-spending runs without a report (no variable, no start),
    a profile or age that is not read, a value the requirement refuses becoming a traceback, and
    a requirement that binds the repository of a fixture that did not exist when the report was
    measured."""
    with pytest.raises(PreflightRefused, match=REPORT_ENV):
        requirement_from_environment({})
    with pytest.raises(PreflightRefused, match=REPORT_ENV):
        requirement_from_environment({REPORT_ENV: ""})
    req = requirement_from_environment({REPORT_ENV: str(tmp_path / "r.json")})
    assert req == PreflightRequirement(tmp_path / "r.json", "unattended", 86400, bind_places=False)
    req = requirement_from_environment({REPORT_ENV: str(tmp_path / "r.json"),
                                        PROFILE_ENV: "demo", MAX_AGE_ENV: "90"})
    assert (req.profile, req.max_age_seconds) == ("demo", 90)
    for age in ("", "abc", "0", "-5", "604801", "1.5", "True"):
        with pytest.raises(PreflightRefused, match="configuration is refused"):
            requirement_from_environment({REPORT_ENV: str(tmp_path / "r.json"), MAX_AGE_ENV: age})
    with pytest.raises(PreflightRefused, match="configuration is refused"):
        requirement_from_environment({REPORT_ENV: str(tmp_path / "r.json"), PROFILE_ENV: "A B"})
    with pytest.raises(ValueError, match="bind_places"):
        PreflightRequirement(tmp_path / "r.json", "demo", 60, bind_places="yes")


def _launcher(tmp_path, **kw) -> HohRunLauncher:
    return HohRunLauncher(tmp_path / "root", tmp_path / "repo", **kw)


def _project(tmp_path, name: str = "project") -> Path:
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    return path


def test_a_project_that_declares_nothing_is_bound_to_the_default(tmp_path):
    """Catches: the bind leaving a launcher that starts runs ungated. A launcher built without a
    requirement, bound to a project without `preflight.json`, is gated by the default and types
    its three options on every `hoh start` / `hoh run`; a dry-run launcher starts nothing and is
    gated by nothing; a requirement the constructor was given wins over the default; nothing is
    carried from one project to the next."""
    project = _project(tmp_path)
    launcher = _launcher(tmp_path)
    launcher.bind_preflight(project)
    assert launcher.preflight == default_requirement(project)
    assert launcher._preflight_options() == [
        "--preflight-report", str(project / "preflight-report.json"),
        "--preflight-profile", "unattended", "--preflight-max-age", "86400"]
    assert launcher.preflight_waiver is None
    dry = _launcher(tmp_path, dry_run=True)
    dry.bind_preflight(project)
    assert dry.preflight is None and dry._preflight_options() == []
    ctor = PreflightRequirement(tmp_path / "ctor.json", "demo", 120)
    given = _launcher(tmp_path, preflight=ctor)
    given.bind_preflight(project)
    assert given.preflight == ctor
    # a waiver on one project, then a project that declares nothing: the default again
    waived = _project(tmp_path, "waived")
    (waived / PREFLIGHT_FILE).write_text(json.dumps(WAIVER), encoding="utf-8")
    launcher.bind_preflight(waived)
    assert launcher.preflight is None and launcher.preflight_waiver is not None
    launcher.bind_preflight(_project(tmp_path, "later"))
    assert launcher.preflight == default_requirement(tmp_path / "later")
    assert launcher.preflight_waiver is None


def test_a_waiver_is_a_recorded_decision_and_a_launcher_built_with_a_requirement_cannot_be_waived(
        tmp_path):
    """Catches: a waiver that does not carry who, when and why; a waiver that is accepted for a
    launcher built with a requirement (a project file may not loosen what the launcher was
    built with); and a waived project that is still typed an option."""
    project = _project(tmp_path)
    (project / PREFLIGHT_FILE).write_text(json.dumps(WAIVER), encoding="utf-8")
    launcher = _launcher(tmp_path)
    launcher.bind_preflight(project)
    assert launcher.preflight is None and launcher._preflight_options() == []
    assert launcher.preflight_waiver == PreflightWaiver(
        reason=WAIVER["reason"], decided_by="operator", decided_at="2026-10-19T08:00:00Z")
    built = _launcher(tmp_path, preflight=PreflightRequirement(tmp_path / "c.json", "demo", 60))
    with pytest.raises(ValueError, match="may not loosen"):
        built.bind_preflight(project)
    assert built.preflight is None and "waives the preflight" in built._preflight_bind_failed


@pytest.mark.parametrize("label", [
    "missing-key", "extra-key", "other-schema", "empty-reason", "blank-reason", "long-reason",
    "newline-reason", "nul-reason", "number-reason", "empty-name", "list-name", "bad-time",
    "time-with-zone", "time-trailing-newline", "null-time", "boolean-reason", "time-not-a-date",
    "time-unpadded", "surrogate-reason"])
def test_a_waiver_that_is_not_exactly_one_halts_the_bind_and_starts_nothing(tmp_path, label):
    """Fail closed: whatever is at `preflight.json` and is not exactly a requirement or a waiver
    is never read as "no requirement" and never as a waiver. The bind raises with the reason, and
    the launcher refuses every start under it."""
    doc = dict(WAIVER)
    if label == "missing-key":
        del doc["decided_by"]
    elif label == "extra-key":
        doc["extra"] = 1
    elif label == "other-schema":
        doc["schema"] = "hoh-preflight-waiver/2"
    elif label == "empty-reason":
        doc["reason"] = ""
    elif label == "blank-reason":
        doc["reason"] = "   "
    elif label == "long-reason":
        doc["reason"] = "x" * 401
    elif label == "newline-reason":
        doc["reason"] = "line one\nline two"
    elif label == "nul-reason":
        doc["reason"] = "a\u0000b"
    elif label == "surrogate-reason":
        doc["reason"] = "a\ud800b"             # no file or terminal can hold it as it is
    elif label == "number-reason":
        doc["reason"] = 5
    elif label == "boolean-reason":
        doc["reason"] = True
    elif label == "empty-name":
        doc["decided_by"] = ""
    elif label == "list-name":
        doc["decided_by"] = ["operator"]
    elif label == "bad-time":
        doc["decided_at"] = "yesterday"
    elif label == "time-with-zone":
        doc["decided_at"] = "2026-10-19T08:00:00+00:00"
    elif label == "time-trailing-newline":
        doc["decided_at"] = "2026-10-19T08:00:00Z\n"
    elif label == "null-time":
        doc["decided_at"] = None
    elif label == "time-not-a-date":
        doc["decided_at"] = "2026-13-45T25:61:61Z"
    elif label == "time-unpadded":
        doc["decided_at"] = "2026-1-9T8:0:0Z"       # `strptime` reads it; the shape of a record is fixed
    project = _project(tmp_path)
    (project / PREFLIGHT_FILE).write_text(json.dumps(doc), encoding="utf-8")
    launcher = _launcher(tmp_path)
    with pytest.raises(ValueError, match="preflight requirement refused"):
        launcher.bind_preflight(project)
    assert launcher.preflight is None and launcher.preflight_waiver is None
    node = TaskNode(id="n1", spec_path=str(tmp_path / "n1.md"))
    assert "preflight requirement refused" in (launcher.prepare(node) or "")


def test_the_waiver_boundary_values_are_read(tmp_path):
    """The limits are 400 characters and not fewer: a reason of exactly 400 is a waiver (the case
    above refuses 401), and a reason with non-ASCII printable text is one too."""
    for reason in ("x" * 400, "a na\u00efve caf\u00e9 project, no overnight runs"):
        project = _project(tmp_path, f"p{len(reason)}")
        (project / PREFLIGHT_FILE).write_text(json.dumps({**WAIVER, "reason": reason}),
                                              encoding="utf-8")
        launcher = _launcher(tmp_path)
        launcher.bind_preflight(project)
        assert launcher.preflight_waiver.reason == reason


# --------------------------------------------------------------------------- #
# the launcher asks for the place
# --------------------------------------------------------------------------- #

class _Grants:
    name = "grants"

    def may_approve(self, worktree):
        return True

    def approve(self, worktree, repo):
        from hoh.approval import Approval
        return Approval(granted=True, worktree=worktree, provider=self.name)


def _recording(tmp_path, monkeypatch, requirement):
    wt = tmp_path / "worktree-home"
    wt.mkdir(exist_ok=True)
    launcher = HohRunLauncher(tmp_path / "root", tmp_path / "repo", approvals=_Grants(),
                              preflight=requirement)
    calls: list[list[str]] = []

    def run_cli(argv):
        calls.append(list(argv))
        if "worktree" in argv:
            return Dispatch(0, json.dumps({"path": str(wt)}), "")
        return Dispatch(0, "", "")

    monkeypatch.setattr(launcher, "_run_cli", run_cli)
    return launcher, calls


def test_the_launcher_holds_the_report_to_its_repository_and_runs_directory(tmp_path, monkeypatch):
    """Catches: the launcher asking the gate without its place (L11 left open on the governed
    path). A report measured for the launcher's repository and runs directory starts the run; the
    same report for another repository or runs directory is a returned reason that names it, with
    the command that measures the right place; a requirement that says it does not bind places
    (a tool's) accepts the report that names others and gives no such hint."""
    repo, root = tmp_path / "repo", tmp_path / "root"
    right = _ready(environment_identity(repo=repo, root=root))
    right["measured_at_utc"] = datetime.now(UTC).strftime(REPORT_TIME_FORMAT)
    req = _put(tmp_path, right)
    launcher, calls = _recording(tmp_path, monkeypatch, req)
    assert launcher.prepare(TaskNode(id="n1", repair_of="g", run_id="n1")) is None

    wrong = _ready(environment_identity(repo=tmp_path / "other", root=root))
    wrong["measured_at_utc"] = right["measured_at_utc"]
    req = _put(tmp_path, wrong, "wrong.json")
    launcher, calls = _recording(tmp_path, monkeypatch, req)
    why = launcher.prepare(TaskNode(id="n2", repair_of="g", run_id="n2"))
    assert isinstance(why, str) and "measured for another repository" in why and calls == []
    assert "--deep --repo REPO --root ROOT` writes a new report" in why
    # the reason gives the end of each place and the report's path is cut: however long the
    # paths are, the command that follows the reason is never the part that is cut off
    assert len(why) <= 600 and why.endswith("writes a new report")

    wrong["environment"] = environment_identity(repo=repo, root=tmp_path / "other")
    req = _put(tmp_path, wrong, "wrong-root.json")
    launcher, calls = _recording(tmp_path, monkeypatch, req)
    why = launcher.prepare(TaskNode(id="n3", repair_of="g", run_id="n3"))
    assert isinstance(why, str) and "measured for another runs directory" in why

    tool = PreflightRequirement(req.report, "unattended", 3600, bind_places=False)
    launcher, calls = _recording(tmp_path, monkeypatch, tool)
    assert launcher.prepare(TaskNode(id="n4", repair_of="g", run_id="n4")) is None
    wrong["environment"] = {**environment_identity(), "interpreter": "/elsewhere/python"}
    tool = PreflightRequirement(_put(tmp_path, wrong, "py.json").report, "unattended", 3600,
                                bind_places=False)
    launcher, calls = _recording(tmp_path, monkeypatch, tool)
    why = launcher.prepare(TaskNode(id="n5", repair_of="g", run_id="n5"))
    assert isinstance(why, str) and "measured for another interpreter" in why
    assert "--repo" not in why and calls == []


# --------------------------------------------------------------------------- #
# the production path: hoh project drive -> ... -> check_preflight
# --------------------------------------------------------------------------- #

def _report_for(path: Path, *, repo: Path, root: Path, age: int = 10) -> Path:
    """A fresh READY report in the producer's shape, for the given places."""
    _report(path, age=age)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["environment"] = environment_identity(repo=repo, root=root)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_a_project_that_declares_nothing_is_gated_by_the_default_and_a_report_for_another_place_is_refused(
        drive):
    """The production path of this slice, through the product's own surface: `hoh project drive`
    -> `drive_governed` -> `bind_routing` -> `bind_preflight` (the default, because the project
    declares nothing) -> `prepare` -> `check_preflight(repo=, root=)`.

    1. No report: a terminal halt by name (BLOCKED_DEPENDENCY) that says where the report is
       expected and the command that writes it, with the repository and runs directory; nothing
       is started (`calls == []`).
    2. The same halt, driven again: quiet -- no byte of the project's state or of any run moves
       (only the new session's own lease and epoch records).
    3. A fresh READY report for ANOTHER repository, at the default path: still refused, naming
       the repository.
    4. A fresh READY report for this repository and runs directory, plus the existing `hoh
       project unblock`: the node starts, and both subprocess commands carry the default
       requirement."""
    default = drive.store.dir / DEFAULT_REPORT_NAME
    _, first = drive.run("sA")
    assert first["halt"] == "BLOCKED_DEPENDENCY", first
    assert "the environment preflight does not allow a start" in first["reason"]
    assert f"there is no preflight report at {str(default)!a}" in first["reason"], first
    assert drive.launcher.calls == []
    # the default's producer was asked once (no tool in this test: exit 127) and recorded it
    assert len(drive.launcher.measurements) == 1
    assert "the measurement failed (exit 127)" in first["reason"], first
    assert len(list(drive.store.dir.glob("preflight-attempt-*.json"))) == 1
    frozen = drive.project_bytes()
    _, second = drive.run("sB")
    assert second["halt"] == "BLOCKED_DEPENDENCY" and "blocked: n1" in second["reason"], second
    assert drive.launcher.calls == [] and drive.launcher.measurements == []      # not measured twice an hour
    after = drive.project_bytes()
    changed = {k for k in frozen if frozen[k] != after.get(k)} | (set(after) - set(frozen))
    assert after["project.json"] == frozen["project.json"]
    assert all(k.startswith(("lease.", "epochs.json", ".lock")) for k in changed), sorted(changed)
    assert not list(drive.root.glob("n1*"))

    _report_for(default, repo=drive.tmp / "elsewhere", root=drive.root)
    assert cli.main(["--root", str(drive.root), "project", "unblock", "p", "n1",
                     "--reason", "a fresh report"]) == 0
    drive.capsys.readouterr()
    _, third = drive.run("sC")
    assert third["halt"] == "BLOCKED_DEPENDENCY", third
    assert "measured for another repository" in third["reason"], third
    assert drive.launcher.calls == [] and drive.launcher.measurements == []

    _report_for(default, repo=drive.repo, root=drive.root)
    assert cli.main(["--root", str(drive.root), "project", "unblock", "p", "n1",
                     "--reason", "a fresh report for this repository"]) == 0
    drive.capsys.readouterr()
    _, fourth = drive.run("sD")
    assert fourth["halt"] == "CLOSED", fourth
    (start,), (run,) = drive.launcher.starts(), drive.launcher.runs()
    parser = cli.build_parser()
    for argv in (start, run):
        parsed = parser.parse_args(_after(argv))
        assert (parsed.preflight_report, parsed.preflight_profile, parsed.preflight_max_age) == (
            str(default), "unattended", 86400)
    assert drive.launcher.merged == ["n1"]


def test_a_waived_project_starts_without_any_report_and_the_launcher_says_it_was_waived(drive):
    """Catches: the default applied over a waiver (a recorded decision of a person). A waived
    project starts with no report anywhere, types no option, and the launcher carries the
    waiver (who, when, why) for whoever quotes it."""
    (drive.store.dir / PREFLIGHT_FILE).write_text(json.dumps(WAIVER), encoding="utf-8")
    _, out = drive.run("sA")
    assert out["halt"] == "CLOSED", out
    assert drive.launcher.preflight_waiver is not None
    for argv in (*drive.launcher.starts(), *drive.launcher.runs()):
        assert not [w for w in argv if w.startswith("--preflight")]


# --------------------------------------------------------------------------- #
# the tools that start runs on their own
# --------------------------------------------------------------------------- #

class _Stop(Exception):
    """Raised by the recording launcher: the tool got as far as constructing it."""


@pytest.fixture
def recorder(monkeypatch):
    seen: list[dict] = []

    class Recorder:
        def __init__(self, *args, **kwargs):
            seen.append(kwargs)
            raise _Stop

    monkeypatch.setattr("hoh.launcher.HohRunLauncher", Recorder)
    for name in (REPORT_ENV, PROFILE_ENV, MAX_AGE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOH_TRUST_HELPER", "/bin/true")
    monkeypatch.setenv("HOH_WORKTREE_ROOT", "/nonexistent-worktrees")
    monkeypatch.setenv("PYTHONPATH", os.environ.get("PYTHONPATH", ""))     # restored on exit
    return seen


def _tasks(tmp_path: Path) -> Path:
    tasks = tmp_path / "tasks"
    (tasks / "t1").mkdir(parents=True)
    (tasks / "t1" / "SPEC.md").write_text("# t1\n", encoding="utf-8")
    return tasks


def test_benchmark_arm_c_starts_only_on_a_report_and_hands_it_to_its_launcher(
        tmp_path, monkeypatch, recorder):
    """Catches: a campaign cell that spends quota without asking the environment (the tool passed
    no requirement), one that asks after it has written its project, and a requirement that is
    not the one the environment names. No variable: the tool exits `refused:` naming the
    variable and has written nothing. With one: the launcher is constructed with exactly the
    requirement, and it does not bind the cell's repository (a fixture made at run time)."""
    bm = _load("benchmark_identity_probe", "tools/benchmark.py")
    monkeypatch.setattr(bm, "TASKS", _tasks(tmp_path))
    repo = tmp_path / "cell" / "repo-abcd1234"
    repo.mkdir(parents=True)
    with pytest.raises(SystemExit, match=rf"refused: no preflight report is named: set {REPORT_ENV}"):
        bm.arm_c("t1", repo, 1, "none")
    assert not (repo.parent / "root").exists() and recorder == []
    report = tmp_path / "report.json"
    monkeypatch.setenv(REPORT_ENV, str(report))
    with pytest.raises(_Stop):
        bm.arm_c("t1", repo, 1, "none")
    assert recorder[0]["preflight"] == PreflightRequirement(report, "unattended", 86400,
                                                            bind_places=False)


def test_closure_e2e_starts_only_on_a_report_and_builds_no_fixture_without_one(
        tmp_path, monkeypatch, recorder):
    """Catches: the end-to-end closure run spending quota without asking, or building its fixture
    before it knows it may. No variable: `refused:` and `_fixture` is never called. With one: the
    launcher gets the requirement."""
    ce = _load("closure_identity_probe", "tools/closure_e2e.py")
    monkeypatch.setattr(ce, "TASKS", _tasks(tmp_path))
    built: list[str] = []
    repo = tmp_path / "cell" / "repo-abcd1234"
    repo.mkdir(parents=True)

    def fixture(task):
        built.append(task)
        return repo

    monkeypatch.setattr(ce, "_fixture", fixture)
    with pytest.raises(SystemExit, match=rf"refused: no preflight report is named: set {REPORT_ENV}"):
        ce.one_run("t1", 18)
    assert built == [] and recorder == []
    report = tmp_path / "report.json"
    monkeypatch.setenv(REPORT_ENV, str(report))
    monkeypatch.setenv(MAX_AGE_ENV, "600")
    with pytest.raises(_Stop):
        ce.one_run("t1", 18)
    assert built == ["t1"]
    assert recorder[0]["preflight"] == PreflightRequirement(report, "unattended", 600,
                                                            bind_places=False)


# --------------------------------------------------------------------------- #
# the producer of the default: a report is measured once an hour, or the drive halts by name
# --------------------------------------------------------------------------- #

def _tool_says(repo: Path, root: Path, *, verdict: str = "READY", at: datetime | None = None,
               profile: str = "unattended", tamper: str | None = None) -> str:
    """What `tools/preflight.py --json` prints: the producer's own `report()` over synthetic checks,
    for the given places."""
    checks = [pf.Check(c, "q", pf.PASS, "d", {}) for c in pf.PROFILES[profile]["requires"]]
    checks.append(pf.Check("isolation", "q", pf.PASS, "d", {"level": pf.PROFILES[profile]["min_isolation"]}))
    if verdict != "READY":
        checks[0] = pf.Check(checks[0].check_id, "q", pf.FAIL, "d", {})
    body = pf.report(checks, profile, deep=True, repo=repo, root=root)
    if at is not None:
        body["measured_at_utc"] = at.strftime(REPORT_TIME_FORMAT)
        # a report with another clock is another report: the tool's digest leaves the clock out
    if tamper == "digest":
        body["blocking"] = ["trust_readiness"]
    if tamper == "extra":
        body["extra"] = 1
    return json.dumps(body)


def _mdrive(tmp_path, capsys, answer_for):
    """A drive whose launcher's measuring tool answers `answer_for(drive)`: a `(exit, stdout,
    stderr)` triple, an exception to raise, or a function of `(argv, timeout)`."""
    holder = {}

    def measure(argv, timeout):
        answer = answer_for(holder["drive"])
        if isinstance(answer, BaseException):
            raise answer
        return answer(argv, timeout) if callable(answer) else answer

    holder["drive"] = Drive(tmp_path, capsys, measure=measure)
    return holder["drive"]


def _ready_answer(drive, **kw):
    return (0, _tool_says(drive.repo, drive.root, **kw), "")


def test_the_default_measures_a_report_when_there_is_none_and_the_node_starts(tmp_path, capsys):
    """The producer of the default, through `hoh project drive`. Catches: a default that halts the
    unattended drive until a person runs the tool (nothing measured), a measurement that is not
    the tool run under the interpreter the typed commands use with the profile, `--deep`, this
    repository and runs directory and a 300 s bound, one that is not filed whole at the default
    path, and an attempt that is not recorded. A second drive does not measure again."""
    drive = _mdrive(tmp_path, capsys, lambda d: _ready_answer(d))
    _, first = drive.run("sA")
    assert first["halt"] == "CLOSED", first
    (argv, timeout), = drive.launcher.measurements
    assert argv == [hoh_command()[0], str(gate.tool_path()), "--profile", "unattended", "--deep",
                    "--repo", str(drive.launcher.repo_path), "--root", str(drive.launcher.root), "--json"]
    assert timeout == 300 == gate.MEASURE_TIMEOUT_SECONDS
    filed = drive.store.dir / DEFAULT_REPORT_NAME
    assert json.loads(filed.read_text(encoding="utf-8"))["environment"] == environment_identity(
        repo=drive.repo, root=drive.root)
    (attempt,) = drive.store.dir.glob("preflight-attempt-*.json")
    record = json.loads(attempt.read_text(encoding="utf-8"))
    assert record["schema"] == "hoh-preflight-attempt/1" and record["timeout_seconds"] == 300
    assert record["interpreter"] == hoh_command()[0] and record["pid"] == os.getpid()
    assert attempt.name == f"preflight-attempt-{record['started_at'][:13].replace('-', '').replace(':', '')}Z.json"
    _, second = drive.run("sB")
    assert second["halt"] == "CLOSED" and drive.launcher.measurements == []


def test_an_old_report_or_one_for_another_place_is_put_aside_by_version_and_replaced(
        tmp_path, capsys):
    """Catches: a measurement that overwrites the evidence it replaces (the old report must stay,
    byte for byte, under a version name), one that is not made when the report is stale (a
    verdict refusal of a report that is also too old counts) or for another place, and one that
    is made when the report is fresh and says NOT_READY (a verdict is evidence, not a gap)."""
    old = datetime.now(UTC) - timedelta(days=2)
    cases = {
        "stale": lambda d: _report_for(d.store.dir / DEFAULT_REPORT_NAME, repo=d.repo, root=d.root, age=2 * 86400),
        "other-place": lambda d: _report_for(d.store.dir / DEFAULT_REPORT_NAME, repo=d.tmp / "elsewhere", root=d.root),
        "stale-not-ready": lambda d: _put_not_ready(d, at=old),
    }
    for label, make in cases.items():
        sub = tmp_path / label
        sub.mkdir()
        drive = _mdrive(sub, capsys, lambda d: _ready_answer(d))
        make(drive)
        default = drive.store.dir / DEFAULT_REPORT_NAME
        before = default.read_bytes()
        _, out = drive.run("sA")
        assert out["halt"] == "CLOSED", (label, out)
        assert len(drive.launcher.measurements) == 1, label
        (aside,) = drive.store.dir.glob(f"{DEFAULT_REPORT_NAME}.v1.*Z")
        assert aside.read_bytes() == before, label
        assert default.read_bytes() != before, label
    # fresh and NOT_READY: asked, refused with the verdict, never replaced
    sub = tmp_path / "fresh-not-ready"
    sub.mkdir()
    drive = _mdrive(sub, capsys, lambda d: _ready_answer(d))
    _put_not_ready(drive, at=datetime.now(UTC) - timedelta(seconds=30))
    _, out = drive.run("sA")
    assert out["halt"] == "BLOCKED_DEPENDENCY" and "says NOT_READY" in out["reason"], out
    assert drive.launcher.measurements == []
    assert not list(drive.store.dir.glob("preflight-attempt-*")) and not list(
        drive.store.dir.glob(f"{DEFAULT_REPORT_NAME}.v*"))


def _put_not_ready(drive, *, at: datetime) -> None:
    (drive.store.dir / DEFAULT_REPORT_NAME).write_text(
        _tool_says(drive.repo, drive.root, verdict="NOT_READY", at=at), encoding="utf-8")


@pytest.mark.parametrize("label", [
    "exit-127", "exit-2", "timeout", "cannot-start", "not-json", "a-list", "other-profile", "bad-digest"])
def test_a_measurement_that_gives_no_usable_report_halts_by_name_once_an_hour(
        tmp_path, capsys, label):
    """Termination of the producer: bound (one attempt per project and UTC hour, created BEFORE the
    tool is run), terminal state (BLOCKED_DEPENDENCY naming why), a quiet repeat (the next drive
    in the hour runs nothing and writes nothing), and no report filed from a measurement that is
    not one. Catches: a retry in the same drive or the same hour, a halt that does not say why,
    a report filed without a digest that matches its content, and a timeout that is not killed."""
    answers = {
        "exit-127": ((127, "", "tool missing"), "the measurement failed (exit 127)"),
        "exit-2": ((2, "", "usage: error"), "the measurement failed (exit 2)"),
        "timeout": (subprocess.TimeoutExpired("preflight", 300), "did not finish within 300 s"),
        "cannot-start": (PermissionError("no"), "could not be started (PermissionError)"),
        "not-json": ((0, "not json at all", ""), "did not print one JSON report"),
        "a-list": ((0, "[1, 2]", ""), "is not a report for the required profile"),
        "other-profile": (None, "is not a report for the required profile"),
        "bad-digest": (None, "digest is not the digest of its content"),
    }
    answer, phrase = answers[label]

    def answer_for(d):
        if label == "other-profile":
            return (0, _tool_says(d.repo, d.root, profile="demo"), "")
        if label == "bad-digest":
            return (0, _tool_says(d.repo, d.root, tamper="digest"), "")
        return answer

    drive = _mdrive(tmp_path, capsys, answer_for)
    _, first = drive.run("sA")
    assert first["halt"] == "BLOCKED_DEPENDENCY" and phrase in first["reason"], first
    assert len(drive.launcher.measurements) == 1 and drive.launcher.calls == []
    assert not (drive.store.dir / DEFAULT_REPORT_NAME).exists()
    (attempt,) = drive.store.dir.glob("preflight-attempt-*.json")
    frozen = drive.project_bytes()
    _, second = drive.run("sB")
    assert second["halt"] == "BLOCKED_DEPENDENCY", second
    assert drive.launcher.measurements == [] and drive.launcher.calls == []
    after = drive.project_bytes()
    changed = {k for k in frozen if frozen[k] != after.get(k)} | (set(after) - set(frozen))
    assert all(k.startswith(("lease.", "epochs.json", ".lock")) for k in changed), sorted(changed)
    assert after[attempt.name] == frozen[attempt.name]


def test_the_attempt_bound_is_per_utc_hour_and_a_new_hour_may_measure_again(
        tmp_path, capsys, monkeypatch):
    """Catches: a bound that is forever (a transient failure would halt the project until a person
    acts) and a bound that is not there (every drive measures). The same project, three drives:
    the first fails to measure, the second half an hour later is told so and runs nothing, the
    third in the next hour measures again and, this time, files a report."""
    clock = {"now": datetime(2026, 10, 19, 10, 5, 0, tzinfo=UTC)}
    monkeypatch.setattr(gate, "_utcnow", lambda: clock["now"])
    state = {"fail": True}
    drive = _mdrive(tmp_path, capsys, lambda d: (127, "", "x") if state["fail"] else _ready_answer(d))
    _, first = drive.run("sA")
    assert first["halt"] == "BLOCKED_DEPENDENCY" and len(drive.launcher.measurements) == 1
    clock["now"] = datetime(2026, 10, 19, 10, 40, 0, tzinfo=UTC)
    _, second = drive.run("sB")
    assert drive.launcher.measurements == []
    assert "already attempted in this UTC hour (preflight-attempt-20261019T10Z.json)" in (
        drive.launcher._measure_note or "")
    clock["now"] = datetime(2026, 10, 19, 11, 10, 0, tzinfo=UTC)
    state["fail"] = False
    assert cli.main(["--root", str(drive.root), "project", "unblock", "p", "n1", "--reason", "a new hour"]) == 0
    capsys.readouterr()
    _, third = drive.run("sC")
    assert len(drive.launcher.measurements) == 1
    assert sorted(a.name for a in drive.store.dir.glob("preflight-attempt-*.json")) == [
        "preflight-attempt-20261019T10Z.json", "preflight-attempt-20261019T11Z.json"]
    assert third["halt"] == "CLOSED", third


def test_only_the_default_is_mended_by_a_measurement(tmp_path, capsys):
    """Catches: a measurement that replaces an operator's declared or constructor-given requirement
    (their report is operator-attested evidence, no self-measurement there). A project that declares
    a requirement whose report is absent halts as in slice 2 and runs nothing."""
    drive = _mdrive(tmp_path, capsys, lambda d: _ready_answer(d))
    drive.declare(drive.tmp / "nowhere.json")
    _, out = drive.run("sA")
    assert out["halt"] == "BLOCKED_DEPENDENCY" and "there is no preflight report" in out["reason"], out
    assert drive.launcher.measurements == [] and not list(drive.store.dir.glob("preflight-attempt-*"))
    ctor = tmp_path / "ctor"
    ctor.mkdir()
    launcher = _launcher(ctor, preflight=PreflightRequirement(ctor / "r.json", "unattended", 3600))
    launcher._run_measurement = lambda argv, timeout: pytest.fail("a constructor requirement was measured")
    launcher.bind_preflight(_project(ctor))
    assert launcher._preflight_default is False


def test_the_producer_records_before_it_runs_and_runs_nothing_it_cannot_record(tmp_path, monkeypatch):
    """Catches: a measurement made when its attempt could not be recorded (a crash inside the attempt
    would then not count), no tool to measure with, and an old report put aside by a name that is
    taken. The producer is called directly for the three edges a drive cannot reach."""
    req = PreflightRequirement(tmp_path / "gone" / "preflight-report.json", "unattended", 86400)
    ran = []
    why = gate.measure_default(req, python=sys.executable, repo=tmp_path, root=tmp_path,
                               runner=lambda argv, timeout: ran.append(argv))
    assert why is not None and "cannot be recorded" in why and ran == []
    here = tmp_path / "here"
    here.mkdir()
    req = PreflightRequirement(here / "preflight-report.json", "unattended", 86400)
    monkeypatch.setattr(gate, "tool_path", lambda: None)
    why = gate.measure_default(req, python=sys.executable, repo=tmp_path, root=tmp_path,
                               runner=lambda argv, timeout: ran.append(argv))
    monkeypatch.undo()
    assert why is not None and "no tools/preflight.py" in why and ran == []
    assert len(list(here.glob("preflight-attempt-*.json"))) == 1          # recorded before it was found out
    # the version name is taken: the old report stays where it is, the new one is not filed
    there = tmp_path / "there"
    there.mkdir()
    req = PreflightRequirement(there / "preflight-report.json", "unattended", 86400)
    (there / "preflight-report.json").write_text("old", encoding="utf-8")
    now = datetime(2026, 10, 19, 10, 0, 0, tzinfo=UTC)
    taken = there / "preflight-report.json.v1.20261019T100000Z"
    taken.write_text("taken", encoding="utf-8")
    why = gate.measure_default(req, python=sys.executable, repo=tmp_path, root=tmp_path, now=now,
                               runner=lambda argv, timeout: (0, _tool_says(tmp_path, tmp_path), ""))
    assert why is not None and "cannot be put aside" in why
    assert (there / "preflight-report.json").read_text(encoding="utf-8") == "old"
    assert taken.read_text(encoding="utf-8") == "taken"


def test_the_real_process_boundary_runs_the_tool_it_is_given(tmp_path, monkeypatch):
    """Catches: the default `run_tool` (a subprocess with the argument list, the stdout captured, the
    exit code returned) never exercised. A script that prints a report stands in for the tool; the
    producer runs it for real under this interpreter and files what it printed."""
    repo, root = tmp_path / "repo", tmp_path / "root"
    said = tmp_path / "said.json"
    said.write_text(_tool_says(repo, root), encoding="utf-8")
    script = tmp_path / "preflight.py"
    script.write_text("import sys\nsys.stdout.write(open(%r).read())\nsys.exit(0)\n" % str(said), encoding="utf-8")
    monkeypatch.setattr(gate, "tool_path", lambda: script)
    req = PreflightRequirement(tmp_path / "preflight-report.json", "unattended", 86400)
    assert gate.measure_default(req, python=sys.executable, repo=repo, root=root) is None
    assert json.loads(req.report.read_text(encoding="utf-8")) == json.loads(said.read_text(encoding="utf-8"))
    assert gate.run_tool([sys.executable, "-c", "import sys; print('x'); sys.exit(3)"], 30)[:2] == (3, "x\n")
    with pytest.raises(subprocess.TimeoutExpired):
        gate.run_tool([sys.executable, "-c", "import time; time.sleep(30)"], 1)


# --------------------------------------------------------------------------- #
# one report for the drive and for the commands it types (HOH_PYTHON)
# --------------------------------------------------------------------------- #

def _fake_python(tmp_path: Path, name: str, lines: str, code: int = 0) -> Path:
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' {lines}\nexit {code}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_the_launcher_compares_the_interpreter_its_commands_are_typed_with(tmp_path, monkeypatch):
    """Catches: `prepare` comparing this process's interpreter while the children it types run under
    `$HOH_PYTHON`, so no single report fits both. With HOH_PYTHON naming another interpreter the
    report for THAT interpreter is accepted and the one for this process is refused by name; an
    interpreter that cannot be asked who it is, or answers wrongly, is a refusal, never a pass."""
    repo, root = tmp_path / "repo", tmp_path / "root"
    # the fake prints two lines: the interpreter and the prefix
    fake = _fake_python(tmp_path, "fake-python", "'/fake/bin/python3' '/fake/venv'")
    monkeypatch.setenv("HOH_PYTHON", str(fake))
    fresh = datetime.now(UTC).strftime(REPORT_TIME_FORMAT)
    for_fake = {**environment_identity(repo=repo, root=root), "interpreter": "/fake/bin/python3", "prefix": "/fake/venv"}
    ok = _put(tmp_path, {**_ready(for_fake), "measured_at_utc": fresh}, "fake.json")
    launcher, calls = _recording(tmp_path, monkeypatch, ok)
    assert launcher.prepare(TaskNode(id="n1", repair_of="g", run_id="n1")) is None
    mine = _put(tmp_path, {**_ready(environment_identity(repo=repo, root=root)), "measured_at_utc": fresh}, "mine.json")
    launcher, calls = _recording(tmp_path, monkeypatch, mine)
    why = launcher.prepare(TaskNode(id="n2", repair_of="g", run_id="n2"))
    assert isinstance(why, str) and "measured for another interpreter" in why and calls == []
    # an interpreter that fails, or says too little, or is not there
    for label, python in (("fails", _fake_python(tmp_path, "bad1", "'x' 'y'", 1)),
                          ("short", _fake_python(tmp_path, "bad2", "'only-one'")),
                          ("gone", tmp_path / "no-such-python")):
        monkeypatch.setenv("HOH_PYTHON", str(python))
        launcher, calls = _recording(tmp_path, monkeypatch, ok)
        why = launcher.prepare(TaskNode(id=f"n-{label}", repair_of="g", run_id=f"n-{label}"))
        assert isinstance(why, str) and "the commands are typed with" in why and calls == [], label
    # no HOH_PYTHON: this process, asked without starting anything
    monkeypatch.delenv("HOH_PYTHON")
    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: pytest.fail("this interpreter was asked by a subprocess"))
    got = gate.interpreter_identity(sys.executable)
    assert got == {k: environment_identity()[k] for k in ("interpreter", "prefix")}


# --------------------------------------------------------------------------- #
# the tool and the register, the findings of the spec review
# --------------------------------------------------------------------------- #

def test_the_worktree_check_creates_and_removes_its_worktree_in_the_repository_it_is_given(
        tmp_path, monkeypatch):
    """Catches: `check_worktree_creation(deep, repo)` running git in the tool's own checkout (the
    unattended profile requires this check). Every git call of the deep check runs with the named
    repository as its working directory; without `--deep` it creates nothing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a").write_text("x\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first")
    cwds = []
    real = pf._run

    def spy(*argv, cwd=None, timeout=60):
        cwds.append(cwd)
        return real(*argv, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(pf, "_run", spy)
    shallow = pf.check_worktree_creation(False, repo)
    assert shallow.state == pf.INCONCLUSIVE and cwds == []
    deep = pf.check_worktree_creation(True, repo)
    assert deep.state == pf.PASS and cwds == [repo, repo]
    assert git(repo, "worktree", "list").count("\n") == 0            # created and removed again


def test_an_empty_repo_or_root_option_is_refused_by_name_not_read_as_the_current_directory(
        capsys):
    """Catches: `--repo ""` becoming `Path('.')` (truthy), so the report would say the current
    directory was measured. Both options refuse an empty or blank value with exit 2."""
    for option in ("--repo", "--root"):
        for value in ("", "  "):
            with pytest.raises(SystemExit) as exc:
                pf.main([option, value])
            assert exc.value.code == 2
            assert f"argument {option}: must name a directory" in capsys.readouterr().err


def test_the_register_row_names_the_probe_and_keeps_the_history_of_its_probes():
    """Catches (R-1): the register edit dropped, mistyped or replacing earlier probes. The row's
    probe is the node of P3-11 slice 4, which exists in its file, and carries no equivalent (the
    row is read PROVEN); the previous probes are kept by addition, each with a day and a reason,
    and slice 3's probe (this file's node) keeps the equivalent it was registered with."""
    row = next(r for r in json.loads((ROOT / "program/v3_3/REQUIREMENTS.json").read_text(
        encoding="utf-8"))["requirements"] if r["id"] == "P3-11")
    probe = row["probe"]
    assert (probe["kind"], probe["symbol"], probe["module"]) == ("symbol", "check_preflight", "preflightgate.py")
    path, _, name = probe["test"].partition("::")
    assert path == "tests/test_preflight_unbound.py"
    assert f"\ndef {name}(" in (ROOT / path).read_text(encoding="utf-8")
    assert "equivalent" not in probe
    for key in ("previous_probe", "probe_before_preg2", "probe_before_p311s2", "probe_before_p311s3",
                "probe_before_p311s4"):
        assert row[key]["replaced_on"] and row[key]["why"], key
    before = row["probe_before_p311s4"]
    assert before["test"] == (
        "tests/test_preflight_identity.py::test_a_project_that_declares_nothing_is_gated_by_the_default_"
        "and_a_report_for_another_place_is_refused")
    assert "equivalent" in before and "bench1" in before["equivalent"]
    assert row["probe_before_p311s3"]["test"] == (
        "tests/test_preflight_start_paths.py::test_a_refused_drive_is_terminal_and_quiet_and_a_fresh_report_plus_unblock_goes_on")


def test_benchmark_arm_b_types_the_requirement_into_hoh_start_and_hoh_run(tmp_path, monkeypatch):
    """Catches (spec review A, B1): a campaign cell that types `hoh start` and `hoh run` itself and
    spends quota without asking the environment. No variable: `refused:` before any command is
    run. With one: both typed commands carry exactly the three options the parser knows."""
    bm = _load("benchmark_identity_probe_b", "tools/benchmark.py")
    monkeypatch.setattr(bm, "TASKS", _tasks(tmp_path))
    monkeypatch.setenv("HOH_TRUST_HELPER", "/bin/true")
    for name in (REPORT_ENV, PROFILE_ENV, MAX_AGE_ENV):
        monkeypatch.delenv(name, raising=False)
    typed: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        typed.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(bm.subprocess, "run", fake_run)
    monkeypatch.setattr(bm, "trust_and_check", lambda worktree, repo: {})
    repo = tmp_path / "cell" / "repo-abcd1234"
    repo.mkdir(parents=True)
    with pytest.raises(SystemExit, match=rf"refused: no preflight report is named: set {REPORT_ENV}"):
        bm.arm_b("t1", repo, 1, "none")
    assert typed == []
    report = tmp_path / "report.json"
    monkeypatch.setenv(REPORT_ENV, str(report))
    bm.arm_b("t1", repo, 1, "none")
    parser = cli.build_parser()
    (start,) = [a for a in typed if "start" in a[:6]]
    (run,) = [a for a in typed if "run" in a[:6]]
    for argv in (start, run):
        parsed = parser.parse_args(argv[argv.index("hoh.cli") + 1:])
        assert (parsed.preflight_report, parsed.preflight_profile, parsed.preflight_max_age) == (
            str(report), "unattended", 86400)
