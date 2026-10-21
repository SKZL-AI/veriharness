"""P3-11 slice 2: every start path of the product asks the environment preflight.

Slice 1 (`tests/test_launcher_preflight.py`) built the verdict (`hoh.preflightgate`) and one
consumer, `HohRunLauncher.prepare`, which nothing configured. These tests drive the paths a run
is started by, through the product's own command surface (`cli.main`), with the real parser,
`cmd_start`, `cmd_run` / `_run_in`, the project drive (`hoh project drive` ->
`resumeloop.drive_governed` -> `ProjectController` -> the real `HohRunLauncher.prepare` /
`launch`) and only the process boundary of the launcher replaced (`_run_cli`: the `hoh
worktree`, `hoh start` and `hoh run` subprocesses, which would start agents). No agent, no
Herdr, no network; everything under `tmp_path`.

What they pin: a requirement given to `hoh start`, `hoh run` or declared by a project is asked
first; a refusal is a visible, terminal halt by name (exit 2 / a returned reason / a named
halt), writes nothing, is quiet when the drive is run again, and is never retried by anything
this slice adds. Nothing configured means nothing is asked (every start as before).
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_failure_classes import build  # noqa: F401  (fixture helper: a run `r1` on fakes)
from test_failure_classes import repo as repo  # a fixture; the alias form is an explicit re-export
from test_governance import _initialised_storage_root

from hoh import cli
from hoh.contracts import Candidate, Condition, RunState, Stage
from hoh.launcher import Dispatch, HohRunLauncher, hoh_command
from hoh.orchestrator import GateRunner, MergeResult, RunVerdict
from hoh.preflightgate import PreflightRequirement, default_requirement, environment_identity
from hoh.project import GateOutcome, GateResult, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.store import RunStore

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
#: tests/conftest.py switches the default preflight off for every module that does not say this
TESTS_THE_DEFAULT_PREFLIGHT = True
PROFILE = "unattended"
MAX_AGE = 3600
#: the project's declaration, by its name and schema (typed here, not imported: a test that
#: imports a name the pin does not have is red at collection for every node, not for its own)
PREFLIGHT_FILE = "preflight.json"
PREFLIGHT_SCHEMA = "hoh-preflight-requirement/1"
#: the driver factory reads its world from here (imported by name through `driver.json`)
CONFIG: dict = {}


def git(repo, *args):
    done = subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


# --------------------------------------------------------------------------- #
# reports and requirement options
# --------------------------------------------------------------------------- #

def _report(path: Path, *, verdict="READY", profile=PROFILE, age=10, blocking=(), inconclusive=()) -> Path:
    """A report in the producer's own shape (`tools/preflight.py report()`): the keys the gate
    reads, `measured_at_utc` to the second, and (slice 3) where it was measured: this
    interpreter and the `repo` and `root` directories beside the report (the project world's,
    `_world`); the CLI gates compare the interpreter only."""
    measured = (datetime.now(UTC) - timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps({
        "measured_at_utc": measured, "profile": profile, "verdict": verdict,
        "blocking": list(blocking), "inconclusive": list(inconclusive),
        "environment": environment_identity(repo=path.parent / "repo", root=path.parent / "root")}),
        encoding="utf-8")
    return path


def _options(report: Path, *, profile=PROFILE, age=MAX_AGE) -> list[str]:
    return ["--preflight-report", str(report), "--preflight-profile", profile,
            "--preflight-max-age", str(age)]


def _tree(root: Path) -> dict[str, bytes | None]:
    """Every name under `root` and its bytes: what a start did or did not write."""
    out: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        out[str(path.relative_to(root))] = path.read_bytes() if path.is_file() else None
    return out


# --------------------------------------------------------------------------- #
# hoh run
# --------------------------------------------------------------------------- #

@pytest.fixture
def world(tmp_path, repo, monkeypatch, capsys):
    """One run `r1` in `tmp_path/runs` and an initialised storage root; `--iterations 0
    --no-herdr` reaches the real `_run_in` without starting a role."""
    _initialised_storage_root(tmp_path, monkeypatch)
    _, state, store, _ = build(tmp_path, repo)
    return type("World", (), dict(tmp=tmp_path, root=tmp_path / "runs", state=state, store=store,
                                  capsys=capsys, repo=repo))


def _run(world, extra, *, run_id="r1"):
    rc = cli.main(["--root", str(world.root), "run", run_id, "--no-herdr", "--iterations", "0",
                   *extra])
    out = world.capsys.readouterr()
    return rc, out.out, out.err


REFUSED = {
    "not-ready": dict(verdict="NOT_READY", blocking=["isolation"]),
    "inconclusive": dict(verdict="INCONCLUSIVE", inconclusive=["herdr"]),
    "stale": dict(age=MAX_AGE + 1),
    "other-profile": dict(profile="demo"),
}


@pytest.mark.parametrize("label", [*REFUSED, "missing", "directory", "not-json", "link"])
def test_hoh_run_asks_the_report_first_and_a_refusal_writes_nothing(world, label):
    """Catches a `hoh run` that never asks (the options do not exist on the pin: the parser stops
    with exit 2 and `unrecognized arguments`, not the gate's words), one that asks after a
    process marker, a storage admission or a run write, and a refusal that is a crash or a
    silent pass. Not READY / not fresh / for another profile / missing / a directory / not JSON:
    exit 2, the reason on stderr beginning `the environment preflight does not allow a start`,
    and the whole of the runs root byte for byte as it was."""
    report = world.tmp / "report.json"
    if label in REFUSED:
        _report(report, **REFUSED[label])
    elif label == "directory":
        report.mkdir()
    elif label == "not-json":
        report.write_text("{", encoding="utf-8")
    elif label == "link":
        # a symbolic link to a perfectly good report is refused (slice 1: `O_NOFOLLOW`)
        good = _report(world.tmp / "good.json")
        report.symlink_to(good)
    before = _tree(world.root)
    rc, out, err = _run(world, _options(report))
    assert rc == 2, (rc, err)
    assert err.startswith("the environment preflight does not allow a start"), err
    assert "tools/preflight.py --profile unattended" in err
    assert '"preflight"' not in out
    assert _tree(world.root) == before


def test_hoh_run_goes_on_when_the_report_allows_and_says_which_report(world):
    """Catches a gate that blocks a READY, fresh report for the right profile, and one that
    does not say what it let through: the run proceeds to the real `_run_in` (a dispatcher is
    built, no role starts) and the start prints the profile, the time and the age."""
    report = _report(world.tmp / "report.json", age=25)
    rc, out, err = _run(world, _options(report))
    assert rc == 0, err
    objects = [json.loads(chunk) for chunk in _json_objects(out)]
    shown = [o["preflight"] for o in objects if "preflight" in o]
    assert len(shown) == 1 and shown[0]["profile"] == PROFILE
    assert 25 <= shown[0]["age_seconds"] < 120
    assert any("storage_admission" in o for o in objects), "the start went on to `_run_in`"


def test_without_the_options_nothing_is_asked_and_the_run_is_what_it_was(world):
    """The default: no option, no gate, no file read -- a report that does not exist is not looked
    for (the compatibility row: green on the pin, killed by the mutant that asks by default)."""
    rc, out, err = _run(world, [])
    assert rc == 0, err
    assert '"preflight"' not in out


# --------------------------------------------------------------------------- #
# the options themselves
# --------------------------------------------------------------------------- #

def test_the_three_options_go_together_and_their_values_are_checked(world):
    """A report with no profile or no age is no gate: any one or two of the three is exit 2 with
    its own words, never a default (slice 1: no default age exists on purpose); an age of 0 or
    past a week, a profile that is not a plain name and a path that is not a path are the
    requirement's own refusals, exit 2, nothing written."""
    report = _report(world.tmp / "report.json")
    before = _tree(world.root)
    full = _options(report)
    for keep in ([0], [1], [2], [0, 1], [0, 2], [1, 2]):
        argv = [word for i in keep for word in full[2 * i:2 * i + 2]]
        rc, _, err = _run(world, argv)
        assert rc == 2 and err.startswith("environment preflight refused"), (keep, err)
        assert "go together" in err
    for bad in (_options(report, age=0), _options(report, age=604801),
                _options(report, profile="Not A Name"), _options(report, profile=""),
                _options(report, profile="unattended\n"),
                _options(Path("with\x00nul")), _options(Path("lone\ud800surrogate"))):
        rc, _, err = _run(world, bad)
        assert rc == 2 and err.startswith("environment preflight refused"), (bad, err)
    assert _tree(world.root) == before
    rc, _, err = _run(world, _options(report, age=604800))
    assert rc == 0, err                  # the ceiling itself is a week: the extreme is accepted


def test_a_detached_run_asks_the_report_before_the_tab_and_types_the_options_into_it(world,
                                                                                    monkeypatch):
    """`hoh run --detach` types the run into a tab: a refused report is refused **here**, before
    anything is opened or written (exit 2, the gate's words, the runs root byte for byte); and the
    tab's own `hoh run` is given the three options (absolute report path), so it asks again when it
    starts -- the tab may start later than the caller looked. Without the options the tab's command
    has none (compatibility)."""
    from hoh import detach

    bad = _report(world.tmp / "bad.json", verdict="NOT_READY", blocking=["isolation"])
    before = _tree(world.root)
    for flag in ("--detach", "--require-contract"):
        rc, out, err = _run(world, [flag, *_options(bad)])
        assert rc == 2 and err.startswith("the environment preflight does not allow a start"), err
    assert _tree(world.root) == before
    good = _report(world.tmp / "good.json")
    monkeypatch.chdir(world.tmp)
    parser = cli.build_parser()
    args = parser.parse_args(["--root", "runs", "run", "r1", "--detach", "--preflight-report",
                              "good.json", "--preflight-profile", PROFILE,
                              "--preflight-max-age", str(MAX_AGE)])
    argv = detach.run_argv(args, ["python3", "-m", "hoh.cli"])
    words = {w.split("=", 1)[0]: w.split("=", 1)[1] for w in argv if w.startswith("--preflight")}
    assert words == {"--preflight-report": str(good), "--preflight-profile": PROFILE,
                     "--preflight-max-age": str(MAX_AGE)}
    again = parser.parse_args(argv[3:])
    assert again.preflight_report == str(good) and again.detach is False
    bare = parser.parse_args(["--root", "runs", "run", "r1", "--detach"])
    assert not [w for w in detach.run_argv(bare, ["python3", "-m", "hoh.cli"])
                if w.startswith("--preflight")]
    partial = parser.parse_args(["--root", "runs", "run", "r1", "--detach",
                                 "--preflight-profile", PROFILE])
    with pytest.raises(detach.DetachRefused, match="go together"):
        detach.run_argv(partial, ["python3", "-m", "hoh.cli"])


# --------------------------------------------------------------------------- #
# hoh start
# --------------------------------------------------------------------------- #

def test_hoh_start_asks_the_report_before_the_run_exists(tmp_path, repo, monkeypatch, capsys):
    """`hoh start` is the other way a run begins: a refused report leaves no run directory
    (exit 2, the gate's words); a READY one creates it; no option, as before."""
    monkeypatch.setattr(cli, "_register_trust", lambda *a, **kw: None)
    spec = tmp_path / "spec.md"
    spec.write_text("# spec\n", encoding="utf-8")
    root = tmp_path / "runs"
    base = ["--root", str(root), "start", "--repo", str(repo), "--spec", str(spec)]
    bad = _report(tmp_path / "bad.json", verdict="NOT_READY", blocking=["isolation"])
    rc = cli.main([*base, "--run-id", "s0", *_options(bad)])
    err = capsys.readouterr().err
    assert rc == 2 and err.startswith("the environment preflight does not allow a start")
    assert not root.exists() or not (root / "s0").exists()
    good = _report(tmp_path / "good.json")
    assert cli.main([*base, "--run-id", "s1", *_options(good)]) == 0
    assert '"preflight"' in capsys.readouterr().out
    assert (root / "s1" / "state.json").is_file()
    assert cli.main([*base, "--run-id", "s2"]) == 0
    assert '"preflight"' not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# the project drive: preflight.json -> launcher -> hoh start / hoh run
# --------------------------------------------------------------------------- #

class Grants:
    name = "test"

    def may_approve(self, worktree):
        return True

    def approve(self, worktree, repo):
        from hoh.approval import Approval
        return Approval(granted=True, worktree=worktree, provider=self.name)


class Gates(GateRunner):
    def subject(self) -> str:
        return "abc1234"

    def run(self, subject):
        return [GateResult(name="union", outcome=GateOutcome.GREEN, subject=subject,
                           exit_code=0, detail="")]


class RecordingLauncher(HohRunLauncher):
    """The real launcher; only the process boundary and the merge are replaced. `calls` is every
    argv it would have typed."""

    def __init__(self, root, repo, **kw):
        kw.setdefault("planner", "claude")
        kw.setdefault("developer", "claude")
        kw.setdefault("qa", "claude")
        #: P3-11 slice 3: what the measuring tool answers, `(argv, timeout) -> (exit, stdout, stderr)`;
        #: none: the tool is not there (these tests never run it), and every request is recorded.
        self.measure = kw.pop("measure", None)
        self.measurements: list[tuple[list[str], int]] = []
        super().__init__(root, repo, approvals=Grants(), **kw)
        self.calls: list[list[str]] = []
        self.merged: list[str] = []
        self.run_says = (0, "")            # what `hoh run` answers: (exit code, stderr)
        self.start_says = (0, "")

    def _run_measurement(self, argv, timeout):
        self.measurements.append((list(argv), timeout))
        if self.measure is None:
            return 127, "", "no measuring tool in this test"
        return self.measure(list(argv), timeout)

    def _run_cli(self, argv):
        self.calls.append(list(argv))
        if "worktree" in argv:
            branch = argv[argv.index("--branch") + 1]
            path = self.root.parent / "worktrees" / branch
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                git(self.repo_path, "worktree", "add", "-q", "-b", branch, str(path))
            return Dispatch(0, json.dumps({"path": str(path)}), "")
        if "start" in argv:
            code, said = self.start_says
            if code != 0:
                return Dispatch(code, "", said)
            run_id = argv[argv.index("--run-id") + 1]
            state = RunState(run_id=run_id, repo_path=argv[argv.index("--repo") + 1],
                             project_name="p", spec_path=argv[argv.index("--spec") + 1],
                             spec_digest="s" * 16, policy_digest="p" * 16,
                             profile_digest="f" * 16, stage=Stage.PLANNING,
                             condition=Condition.ACTIVE)
            RunStore(self.root, run_id).write_state(state)
            return Dispatch(0, "", "")
        if "run" in argv:
            code, said = self.run_says
            if code != 0:
                return Dispatch(code, "", said)
            run_id = argv[argv.index("run") + 1]
            store = RunStore(self.root, run_id)
            state = store.read_state().model_copy(update={
                "stage": Stage.CHECKPOINTED,
                "last_accepted_candidate": Candidate(
                    candidate_id=f"{run_id}-i1", repo_path=store.read_state().repo_path,
                    commit="a" * 40, tree_clean=True, tree_digest="d" * 16)})
            store.write_state(state)
            return Dispatch(0, "", "")
        raise AssertionError(f"unexpected cli call {argv}")

    def merge(self, node, outcome):
        self.merged.append(node.id)
        return MergeResult(landed=True, branch=node.branch or "", integrated="b" * 40,
                           candidate=outcome.candidate or "")

    def starts(self) -> list[list[str]]:
        return [c for c in self.calls if "start" in c[len(hoh_command()):][:3]]

    def runs(self) -> list[list[str]]:
        return [c for c in self.calls if "run" in c[len(hoh_command()):][:3]]


def build_driver(store):
    """The `driver.json` factory: the real launcher, a fake process boundary."""
    cfg = CONFIG
    launcher = RecordingLauncher(cfg["root"], cfg["repo"], **cfg.get("kw", {}))
    cfg["launcher"] = launcher
    return launcher, Gates()


def _world(tmp_path, names=("n1",)):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    (repo / "README").write_text("x\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first")
    root = tmp_path / "root"
    nodes = []
    for name in names:
        spec = tmp_path / f"{name}.md"
        spec.write_text(f"# {name}\n", encoding="utf-8")
        nodes.append(TaskNode(id=name, spec_path=str(spec), writes=[f"{name}.txt"]))
    store = ProjectStore(root, "p")
    store.create(ProjectState(project_id="p", repo_path=str(repo), nodes=nodes))
    return repo, root, store


class Drive:
    """`hoh project drive p --session sA` through `cli.main`, the driver registered by name."""

    def __init__(self, tmp_path, capsys, **kw):
        self.tmp = tmp_path
        self.repo, self.root, self.store = _world(tmp_path)
        self.capsys = capsys
        CONFIG.clear()
        CONFIG.update(root=self.root, repo=self.repo, kw=kw)
        (self.store.dir / "driver.json").write_text(json.dumps({
            "factory": "test_preflight_start_paths:build_driver",
            "path": str(Path(__file__).resolve().parent)}))

    def declare(self, report: Path, *, profile=PROFILE, age=MAX_AGE, **over) -> Path:
        doc = {"schema": PREFLIGHT_SCHEMA, "report": str(report), "profile": profile,
               "max_age_seconds": age}
        doc.update(over)
        path = self.store.dir / PREFLIGHT_FILE
        path.write_bytes(doc if isinstance(doc, bytes) else json.dumps(doc).encode("utf-8"))
        return path

    def run(self, session: str = "sA") -> tuple[int, dict]:
        rc = cli.main(["--root", str(self.root), "project", "drive", "p", "--session", session])
        out = self.capsys.readouterr().out
        return rc, json.loads(out) if out.strip().startswith("{") else {}

    @property
    def launcher(self) -> RecordingLauncher:
        return CONFIG["launcher"]

    def project_bytes(self) -> dict:
        return _tree(self.store.dir)


@pytest.fixture
def drive(tmp_path, capsys):
    return Drive(tmp_path, capsys)


def _after(argv: list[str]) -> list[str]:
    """The words after `python -m hoh.cli` -- what the product's parser is given."""
    return argv[len(hoh_command()):]


def test_a_project_that_declares_a_requirement_starts_every_node_and_types_it_on_start_and_run(drive):
    """The production path: `hoh project drive` -> `drive_governed` -> `bind_routing` ->
    `bind_preflight` -> `prepare` -> `hoh start` / `launch` -> `hoh run`. The declared
    requirement is asked at `prepare` and typed on **both** subprocess commands, in the form
    the product's parser accepts and means the same by (this is what closes slice 1's L2: a
    `launch` that is not preceded by a `prepare` is gated by the CLI of the run it types)."""
    report = _report(drive.tmp / "report.json")
    drive.declare(report)
    rc, out = drive.run()
    assert rc == 0 and out["halt"] == "CLOSED", out
    launcher = drive.launcher
    (start,) = launcher.starts()
    (run,) = launcher.runs()
    parser = cli.build_parser()
    for argv in (start, run):
        parsed = parser.parse_args(_after(argv))
        assert parsed.preflight_report == str(report)
        assert parsed.preflight_profile == PROFILE
        assert parsed.preflight_max_age == MAX_AGE
    assert launcher.merged == ["n1"]


def test_a_project_that_waives_the_preflight_starts_and_types_exactly_what_it_did(drive):
    """A recorded waiver (P3-11 slice 3; until then "nothing declared" meant this): no option is
    typed, no report is read (compatibility). A project that declares nothing at all is gated
    by the default now (`tests/test_preflight_identity.py`)."""
    (drive.store.dir / PREFLIGHT_FILE).write_text(json.dumps({
        "schema": "hoh-preflight-waiver/1", "reason": "a development project",
        "decided_by": "operator", "decided_at": "2026-10-19T08:00:00Z"}), encoding="utf-8")
    rc, out = drive.run()
    assert rc == 0 and out["halt"] == "CLOSED", out
    for argv in (*drive.launcher.starts(), *drive.launcher.runs()):
        assert not [w for w in argv if w.startswith("--preflight")]


@pytest.mark.parametrize("label", [*REFUSED, "missing"])
def test_a_refused_report_halts_the_project_by_name_and_starts_nothing(drive, label):
    """The terminal, visible halt: the node is BLOCKED and the drive halts BLOCKED_DEPENDENCY
    with the environment's reason (slice 1's `prepare` gate, now reachable from a project's
    own declaration); no worktree, no `hoh start`, no `hoh run`."""
    report = drive.tmp / "report.json"
    if label in REFUSED:
        _report(report, **REFUSED[label])
    drive.declare(report)
    rc, out = drive.run()
    assert rc != 0 or out.get("halt") != "CLOSED", out
    assert out["halt"] == "BLOCKED_DEPENDENCY", out
    assert "the environment preflight does not allow a start" in out["reason"]
    assert drive.launcher.calls == []


def test_a_refused_drive_is_terminal_and_quiet_and_a_fresh_report_plus_unblock_goes_on(drive):
    """Termination: the refusal is one reason per drive, no counter, no timer, no retry. A
    second drive on the same stale report halts again (`no node is runnable ... blocked: n1`)
    **without writing a byte** of the project's state or of any run, and without calling
    anything (only the new session's own lease and epoch records move); only a fresh report and the existing `hoh
    project unblock` let the node start."""
    report = _report(drive.tmp / "report.json", verdict="NOT_READY", blocking=["isolation"])
    drive.declare(report)
    rc, first = drive.run("sA")
    assert first["halt"] == "BLOCKED_DEPENDENCY"
    frozen = drive.project_bytes()
    rc, second = drive.run("sB")
    assert second["halt"] == "BLOCKED_DEPENDENCY"
    assert "blocked: n1" in second["reason"], second     # the node waits for a person, not a retry
    assert drive.launcher.calls == []
    after = drive.project_bytes()
    changed = {k for k in frozen if frozen[k] != after.get(k)} | (set(after) - set(frozen))
    # the new session's own leader lease and epoch record are the only bytes that move: the
    # project's state, its nodes and every run are untouched (`project.json` is named, not implied)
    assert "project.json" in frozen and after["project.json"] == frozen["project.json"]
    # (`.lock` holds the pid and the second the lock was taken: it differs when the two drives
    # fall in different seconds, which is not a write of the drive's)
    assert all(k.startswith(("lease.", "epochs.json", ".lock")) for k in changed), sorted(changed)
    assert not (drive.root / "n1").exists() and not list(drive.root.glob("n1*"))
    _report(report, age=5)                                  # a fresh READY report, same path
    assert cli.main(["--root", str(drive.root), "project", "unblock", "p", "n1",
                     "--reason", "a fresh report"]) == 0
    drive.capsys.readouterr()
    rc, third = drive.run("sC")
    assert third["halt"] == "CLOSED", third
    assert drive.launcher.merged == ["n1"]


UNREADABLE = ["directory", "fifo", "dangling-link", "live-link", "corrupt", "list", "unknown-key",
              "missing-key", "other-schema", "oversize", "not-utf8", "duplicate-key", "age-zero",
              "age-bool", "bad-profile", "relative-report", "padded-over-limit"]


def _write_bad(drive, label: str) -> None:
    path = drive.store.dir / PREFLIGHT_FILE
    good = {"schema": PREFLIGHT_SCHEMA, "report": str(drive.tmp / "report.json"),
            "profile": PROFILE, "max_age_seconds": MAX_AGE}
    if label == "directory":
        path.mkdir()
    elif label == "fifo":
        os.mkfifo(path)
    elif label == "dangling-link":
        path.symlink_to(drive.tmp / "nowhere")
    elif label == "live-link":
        other = drive.tmp / "elsewhere.json"
        other.write_text(json.dumps(good), encoding="utf-8")
        path.symlink_to(other)
    elif label == "corrupt":
        path.write_bytes(b"{")
    elif label == "list":
        path.write_bytes(b"[]")
    elif label == "unknown-key":
        path.write_bytes(json.dumps({**good, "extra": 1}).encode())
    elif label == "missing-key":
        path.write_bytes(json.dumps({k: v for k, v in good.items() if k != "profile"}).encode())
    elif label == "other-schema":
        path.write_bytes(json.dumps({**good, "schema": "hoh-preflight-requirement/2"}).encode())
    elif label == "oversize":
        path.write_bytes(json.dumps({**good, "profile": "x" * 5000}).encode())
    elif label == "not-utf8":
        path.write_bytes(b"\xff\xfe{}")
    elif label == "duplicate-key":
        path.write_bytes(b'{"schema": "%s", "report": "%s", "profile": "a", "profile": "b", '
                         b'"max_age_seconds": 60}' % (PREFLIGHT_SCHEMA.encode(),
                                                      str(drive.tmp / "r.json").encode()))
    elif label == "age-zero":
        path.write_bytes(json.dumps({**good, "max_age_seconds": 0}).encode())
    elif label == "age-bool":
        path.write_bytes(json.dumps({**good, "max_age_seconds": True}).encode())
    elif label == "bad-profile":
        path.write_bytes(json.dumps({**good, "profile": "Not A Name"}).encode())
    elif label == "padded-over-limit":
        # a valid declaration, one byte over the 4096-byte limit: refused for its size alone
        body = json.dumps(good).encode()
        path.write_bytes(body + b" " * (4097 - len(body)))
    elif label == "relative-report":
        # a relative path is made absolute against the cwd of whoever reads it: the declaration
        # must name an absolute file, or two processes would read two files
        path.write_bytes(json.dumps({**good, "report": "report.json"}).encode())


@pytest.mark.parametrize("label", UNREADABLE)
def test_a_declaration_that_is_not_one_halts_the_drive_before_any_run_is_started(drive, label):
    """Fail closed: whatever is at `preflight.json` and is not exactly the declaration is never
    read as "no requirement". The drive halts by name (`preflight requirement refused`,
    AMBIGUOUS: the drive raised), and the launcher typed nothing."""
    _report(drive.tmp / "report.json")
    _write_bad(drive, label)
    rc, out = drive.run()
    assert out.get("halt") == "AMBIGUOUS", (label, out)
    assert "preflight requirement refused" in out["reason"], out
    if label == "padded-over-limit":
        assert "larger than 4096 bytes" in out["reason"], out
    assert drive.launcher.calls == []


def test_a_declaration_of_exactly_the_size_limit_is_read(drive):
    """The limit is 4096 bytes and not less: the same valid declaration padded to exactly 4096
    bytes is the requirement (the drive goes on); one byte more is refused (the case above)."""
    _report(drive.tmp / "report.json")
    path = drive.declare(drive.tmp / "report.json")
    body = path.read_bytes()
    path.write_bytes(body + b" " * (4096 - len(body)))
    assert path.stat().st_size == 4096
    rc, out = drive.run()
    assert out["halt"] == "CLOSED", out


def test_a_declaration_other_than_the_constructors_requirement_is_refused(tmp_path, capsys):
    """A launcher built with a requirement and a project that declares another: the drive halts
    ("preflight requirement changed") -- a declaration may not loosen what the launcher was given."""
    ctor = PreflightRequirement(report=tmp_path / "ctor.json", profile=PROFILE,
                                max_age_seconds=MAX_AGE)
    drive = Drive(tmp_path, capsys, preflight=ctor)
    _report(tmp_path / "ctor.json")
    drive.declare(_report(tmp_path / "other.json"), age=MAX_AGE * 2)
    rc, out = drive.run()
    assert out.get("halt") == "AMBIGUOUS" and "preflight requirement changed" in out["reason"], out
    assert drive.launcher.calls == []
    # the same declaration as the constructor's is the same requirement: the drive goes on
    other = tmp_path / "second"
    other.mkdir()
    drive2 = Drive(other, capsys, preflight=PreflightRequirement(
        report=other / "report.json", profile=PROFILE, max_age_seconds=MAX_AGE))
    _report(other / "report.json")
    drive2.declare(other / "report.json")
    rc, out = drive2.run()
    assert out["halt"] == "CLOSED", out


def test_a_launcher_whose_bind_failed_starts_nothing_even_if_it_is_used_anyway(tmp_path, capsys):
    """`prepare` returns the reason and `launch` an UNDETERMINED outcome, with nothing typed:
    a caller that swallowed the bind error does not get an ungated launcher."""
    drive = Drive(tmp_path, capsys)
    _write_bad(drive, "corrupt")
    launcher = RecordingLauncher(drive.root, drive.repo)
    with pytest.raises(ValueError, match="preflight requirement refused"):
        launcher.bind_routing(drive.store.dir)
    node = TaskNode(id="n1", spec_path=str(tmp_path / "n1.md"), writes=["n1.txt"])
    said = launcher.prepare(node)
    assert said is not None and "preflight requirement refused" in said
    assert "nothing is started under it" in said
    outcome = launcher.launch(node)
    assert outcome.verdict is RunVerdict.UNDETERMINED
    assert "preflight requirement refused" in outcome.detail
    assert launcher.calls == []


@pytest.mark.parametrize("said", [
    "the environment preflight does not allow a start: the preflight report is 7200 s old. "
    "`tools/preflight.py --profile unattended --out '/r.json'` writes a new report",
    "environment preflight refused: --preflight-report, --preflight-profile and "
    "--preflight-max-age go together",
])
def test_a_run_that_hoh_run_refuses_for_the_environment_is_undetermined_and_named(drive, said):
    """The CLI of the run the launcher typed refuses (the report went stale between `prepare`
    and `hoh run`, or a malformed option): exit 2 with its words. The outcome is UNDETERMINED
    carrying them -- a halt for a person, never a REJECTED verdict (the work was not judged)
    and never a retry."""
    report = _report(drive.tmp / "report.json")
    drive.declare(report)
    launcher = RecordingLauncher(drive.root, drive.repo)
    launcher.bind_preflight(drive.store.dir)
    node = TaskNode(id="n1", spec_path=str(drive.tmp / "n1.md"), writes=["n1.txt"])
    assert launcher.prepare(node) is None
    launcher.run_says = (2, said)
    outcome = launcher.launch(node)
    assert outcome.verdict is RunVerdict.UNDETERMINED and outcome.detail == said.strip()[:600]
    from hoh.launcher import preflight_refusal

    assert preflight_refusal(said) == said.strip()[:600]
    assert preflight_refusal("some other failure\nwith lines") is None


def test_a_start_that_the_cli_refuses_at_prepare_names_the_environment_and_settles_nothing(drive):
    """`hoh start` refused the report between the launcher's own look and its command: `prepare`
    returns the reason with the CLI's words in it, the node is BLOCKED by the existing path and
    the budget hold the start took is settled (nothing is left held)."""
    report = _report(drive.tmp / "report.json")
    drive.declare(report)
    launcher = RecordingLauncher(drive.root, drive.repo, dispatch_budget=5)
    launcher.bind_preflight(drive.store.dir)
    launcher.start_says = (
        2, "the environment preflight does not allow a start: the preflight report is 9000 s old")
    node = TaskNode(id="n1", spec_path=str(drive.tmp / "n1.md"), writes=["n1.txt"])
    said = launcher.prepare(node)
    assert said is not None and "could not start run n1" in said
    assert "the environment preflight does not allow a start" in said
    assert launcher.starts() and not launcher.runs()


def _declare_in(directory: Path, report: Path, *, profile=PROFILE, age=MAX_AGE) -> None:
    (directory / PREFLIGHT_FILE).write_text(json.dumps({
        "schema": PREFLIGHT_SCHEMA, "report": str(report), "profile": profile,
        "max_age_seconds": age}), encoding="utf-8")


def test_the_requirement_is_recomputed_at_every_bind_and_a_failed_bind_leaves_nothing_behind(
        tmp_path):
    """"Recompute, never carry": the effective requirement of a bind is the project's declaration,
    else the constructor's, else none -- whatever an earlier bind of the same launcher object
    left. A bind that failed does not make the next bind fail or lose the constructor's
    requirement, and a requirement declared by one project is not carried to the next."""
    ctor_report = _report(tmp_path / "ctor.json")
    ctor = PreflightRequirement(report=ctor_report, profile=PROFILE, max_age_seconds=MAX_AGE)
    own = tmp_path / "own"
    other = tmp_path / "other"
    broken = tmp_path / "broken"
    for directory in (own, other, broken):
        directory.mkdir()
    (broken / PREFLIGHT_FILE).write_bytes(b"{")
    node = TaskNode(id="n1", spec_path=str(tmp_path / "n1.md"), writes=["n1.txt"])
    (tmp_path / "n1.md").write_text("# n1\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    (repo / "README").write_text("x\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first")

    # a launcher built with a requirement: a failed bind, then a project without a declaration
    launcher = RecordingLauncher(tmp_path / "root", repo, preflight=ctor)
    with pytest.raises(ValueError, match="preflight requirement refused"):
        launcher.bind_preflight(broken)
    assert launcher.preflight is None                     # nothing is started under a failed bind
    assert "preflight requirement refused" in (launcher.prepare(node) or "")
    launcher.bind_preflight(other)                        # no declaration: the constructor's again
    assert launcher.preflight == ctor
    assert launcher.prepare(node) is None                 # and the failure of the last bind is gone
    # the same launcher object, a project that declares the very requirement the constructor has
    _declare_in(own, ctor_report)
    launcher.bind_preflight(own)
    assert launcher.preflight == ctor

    # a launcher built with none: a requirement declared by one project is not carried on
    bare = RecordingLauncher(tmp_path / "root2", repo)
    declared = tmp_path / "declared"
    declared.mkdir()
    _declare_in(declared, ctor_report, age=90)
    bare.bind_preflight(declared)
    assert bare.preflight is not None and bare.preflight.max_age_seconds == 90
    bare.bind_preflight(other)
    # nothing is carried over: the project that declares nothing is gated by the default
    # (slice 3), not by the 90-second requirement the last project declared
    assert bare.preflight == default_requirement(other)


def test_the_message_of_the_gate_is_cut_at_six_hundred_characters(world, tmp_path):
    """Both gates cut their message to 600 characters: the CLI's stderr line and the launcher's
    `prepare` reason (a report path of three 200-character names makes either one longer)."""
    long = world.tmp / ("a" * 200) / ("b" * 200) / ("c" * 200) / "report.json"
    rc, _, err = _run(world, _options(long))
    assert rc == 2 and err.startswith("the environment preflight does not allow a start")
    assert len(err.rstrip("\n")) == 600, len(err)
    launcher = RecordingLauncher(world.tmp / "root3", world.repo, preflight=PreflightRequirement(
        report=long, profile=PROFILE, max_age_seconds=MAX_AGE))
    node = TaskNode(id="n1", spec_path=str(world.tmp / "n1.md"), writes=["n1.txt"])
    said = launcher.prepare(node)
    assert said is not None and said.startswith("the environment preflight does not allow a start")
    assert len(said) == 600, len(said)


def test_an_exit_2_that_is_not_the_environments_keeps_its_meaning_at_launch(drive):
    """`launch` reads an environment refusal only from exit 2 **and** the gate's own words: other
    words on exit 2, and the gate's words on any other exit code, are what they were before this
    slice (not the UNDETERMINED outcome that names the environment)."""
    drive.declare(_report(drive.tmp / "report.json"))
    launcher = RecordingLauncher(drive.root, drive.repo)
    launcher.bind_preflight(drive.store.dir)
    node = TaskNode(id="n1", spec_path=str(drive.tmp / "n1.md"), writes=["n1.txt"])
    assert launcher.prepare(node) is None
    words = "the environment preflight does not allow a start: the preflight report is 7200 s old"
    for code, said in ((2, "some other failure of hoh run"), (1, words)):
        launcher.run_says = (code, said)
        outcome = launcher.launch(node)
        assert said not in outcome.detail, (code, outcome)


def _json_objects(text: str) -> list[str]:
    """The JSON documents `hoh` printed one after another (`_print` indents them)."""
    out, decoder, at = [], json.JSONDecoder(), 0
    while (at := text.find("{", at)) != -1:
        try:
            _, end = decoder.raw_decode(text, at)
        except ValueError:
            at += 1
            continue
        out.append(text[at:end])
        at = end
    return out
