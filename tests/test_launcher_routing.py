"""P3-20 slice 3: the launcher routes the runs it starts.

Slice 2 gave `hoh run` / `hoh start` a `--routing-policy` and pinned it to the run. The runs a
project's launcher starts are typed by `HohRunLauncher` -- `hoh start` without any routing
option, `hoh run` with explicit `--planner/--developer/--qa` -- so a project's runs were not
routed. These tests drive the production path (`hoh project drive` through `cli.main`, the
driver the project registers in `driver.json`, `resumeloop.drive_governed`, `ProjectController`,
the real `HohRunLauncher.prepare` / `launch`) with only the **process boundary** replaced
(`_run_cli`: the `hoh worktree`, `hoh start` and `hoh run` subprocesses, which would start
agents) and `merge`. No agent, no Herdr, no network; everything under `tmp_path`.

Mutant ids (L1..) are the spec's table; each test says which it kills.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from hoh import cli, routing
from hoh.contracts import Candidate, Condition, RunState, Stage
from hoh.launcher import (
    ROUTING_PIN_FILE, ROUTING_PIN_SCHEMA, ROUTING_POLICY_FILE, Dispatch, HohRunLauncher,
    hoh_command,
)
from hoh.orchestrator import GateRunner, MergeResult, RunVerdict
from hoh.project import GateOutcome, GateResult, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.store import RunStore

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
#: the driver factory reads its world from here (imported by name through `driver.json`)
CONFIG: dict = {}


def git(repo, *args):
    done = subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


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
        super().__init__(root, repo, approvals=Grants(), **kw)
        self.calls: list[list[str]] = []
        self.merged: list[str] = []
        self.run_says = (0, "")            # what `hoh run` answers: (exit code, stderr)

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
        return [c for c in self.calls if "start" in c]

    def runs(self) -> list[list[str]]:
        return [c for c in self.calls if "run" in c[len(hoh_command()):][:3]]


def build_driver(store):
    """The `driver.json` factory of the CLI tests: the real launcher, a fake process boundary."""
    cfg = CONFIG
    launcher = RecordingLauncher(cfg["root"], cfg["repo"])
    cfg["launcher"] = launcher
    return launcher, Gates()


def build_plain_driver(store):
    """A launcher that cannot route: it has no `bind_routing`."""
    class Plain:
        pass

    return Plain(), Gates()


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


def _doc(**over) -> dict:
    doc = {"schema": routing.SCHEMA, "default": {"model": "sonnet", "effort": "high"},
           "roles": {"qa": {"provider": "kimi", "model": "opus"}}}
    doc.update(over)
    return doc


def _policy(store, doc=None, name=ROUTING_POLICY_FILE) -> Path:
    path = store.dir / name
    path.write_bytes(doc if isinstance(doc, bytes) else json.dumps(doc or _doc()).encode("utf-8"))
    return path


class Drive:
    """`hoh project drive p --session sA` through `cli.main`, the driver registered by name."""

    def __init__(self, tmp_path, capsys, monkeypatch, *, factory="build_driver", names=("n1",)):
        self.repo, self.root, self.store = _world(tmp_path, names)
        self.capsys = capsys
        CONFIG.clear()
        CONFIG.update(root=self.root, repo=self.repo)
        (self.store.dir / "driver.json").write_text(json.dumps({
            "factory": f"test_launcher_routing:{factory}",
            "path": str(Path(__file__).resolve().parent)}))

    def run(self, session: str = "sA") -> tuple[int, dict]:
        rc = cli.main(["--root", str(self.root), "project", "drive", "p", "--session", session])
        out = self.capsys.readouterr().out
        return rc, json.loads(out) if out.strip().startswith("{") else {}

    @property
    def launcher(self) -> RecordingLauncher:
        return CONFIG["launcher"]


@pytest.fixture
def drive(tmp_path, capsys, monkeypatch):
    return Drive(tmp_path, capsys, monkeypatch)


def _after(argv: list[str]) -> list[str]:
    """The words after `python -m hoh.cli` -- what the product's parser is given."""
    return argv[len(hoh_command()):]


# --------------------------------------------------------------------------- #
# the production path: hoh project drive -> drive_governed -> launcher
# --------------------------------------------------------------------------- #

def test_hoh_project_drive_starts_and_runs_every_node_under_the_projects_routing_policy(drive):
    """Kills L1 (`hoh start` is not given the policy), L2 (`hoh run` is not), L3 (a typed profile
    for a role the policy declares), L5 (`drive_governed` never binds) and L6 (no pin)."""
    policy_file = _policy(drive.store)
    rc, out = drive.run()
    assert rc == 0 and out["halt"] == "CLOSED", out
    launcher = drive.launcher
    (start,) = launcher.starts()
    (run,) = launcher.runs()
    assert start[start.index("--routing-policy") + 1] == str(policy_file)
    assert run[run.index("--routing-policy") + 1] == str(policy_file)
    # explicit role options only for the roles the policy does not declare a provider for
    assert run[run.index("--planner") + 1] == "claude"
    assert run[run.index("--developer") + 1] == "claude"
    assert "--qa" not in run
    # what the launcher types is what the product's parser accepts, and means what it says
    parser = cli.build_parser()
    started = parser.parse_args(_after(start))
    ran = parser.parse_args(_after(run))
    assert started.routing_policy == str(policy_file) and ran.routing_policy == str(policy_file)
    assert (ran.planner, ran.developer) == ("claude", "claude")
    assert not cli.profile_typed(ran.qa) and ran.qa == "kimi"   # unset: the policy's provider fills it
    # the project's pin
    pin = json.loads((drive.store.dir / ROUTING_PIN_FILE).read_text(encoding="utf-8"))
    assert pin["schema"] == ROUTING_PIN_SCHEMA
    assert pin["digest"] == routing.load(policy_file).digest and pin["source"] == str(policy_file)
    assert launcher.merged == ["n1"]


def test_a_project_without_a_policy_starts_and_runs_exactly_what_it_did(drive):
    """Kills L20 (a routing option, a pin or a changed role option for a project that declared
    none): the argv of `hoh start` and `hoh run` is the one the launcher typed before slice 3."""
    rc, out = drive.run()
    assert rc == 0 and out["halt"] == "CLOSED", out
    launcher = drive.launcher
    root = str(drive.root.resolve())
    (start,) = launcher.starts()
    (run,) = launcher.runs()
    assert start[:len(hoh_command())] == hoh_command()
    assert _after(start)[:3] == ["--root", root, "start"]
    assert "--routing-policy" not in start
    # (the governed drive appends `--master-session sA --master-epoch N` after these, as before)
    assert _after(run)[:14] == ["--root", root, "run", "n1", "--iterations", "2",
                                "--planner", "claude", "--developer", "claude", "--qa", "claude",
                                "--isolation", "none"]
    assert _after(run)[14] == "--master-session" and "--routing-policy" not in run
    assert not (drive.store.dir / ROUTING_PIN_FILE).exists()


def test_a_policy_that_declares_a_provider_for_every_role_types_no_role_option(drive):
    """Kills L3b (the role filter types the planner and the developer whatever the policy
    declares): a provider for every role means no role option at all."""
    _policy(drive.store, _doc(roles={"planner": {"provider": "pi"},
                                     "developer": {"provider": "codex"},
                                     "qa": {"provider": "kimi"}}))
    rc, out = drive.run()
    assert rc == 0, out
    (run,) = drive.launcher.runs()
    assert not [w for w in run if w in ("--planner", "--developer", "--qa")]
    ran = cli.build_parser().parse_args(_after(run))
    assert (ran.planner, ran.developer, ran.qa) == ("claude", "claude", "kimi")  # all unset
    assert not [v for v in (ran.planner, ran.developer, ran.qa) if cli.profile_typed(v)]


# --------------------------------------------------------------------------- #
# fail closed: nothing is started
# --------------------------------------------------------------------------- #

def _make_directory(p: Path) -> None:
    p.mkdir()


def _make_fifo(p: Path) -> None:
    os.mkfifo(p)


def _make_dangling(p: Path) -> None:
    p.symlink_to(p.parent / "nowhere.json")


@pytest.mark.parametrize("label", ["directory", "fifo", "dangling-link", "live-link", "unknown-key",
                                   "corrupt", "list", "empty", "secret-in-model", "oversize",
                                   "not-utf8"])
def test_a_policy_that_cannot_be_loaded_halts_the_drive_before_any_run_is_started(drive, label):
    """Kills L10 (an unreadable policy is skipped: the project runs unrouted) and L21 (a launcher
    whose bind failed goes on). Not even `hoh worktree` is typed; nothing is pinned. The reader is
    slice 1's `routing.load`; the rows reach it through the drive: a directory, a FIFO, a dangling
    and a live link, an unknown key, corrupt JSON, a list, an empty file, a secret-shaped model,
    an oversize file, bytes that are not UTF-8."""
    path = drive.store.dir / ROUTING_POLICY_FILE
    if label == "directory":
        _make_directory(path)
    elif label == "fifo":
        _make_fifo(path)
    elif label == "dangling-link":
        _make_dangling(path)
    elif label == "live-link":
        good = drive.store.dir / "good-policy.json"
        good.write_bytes(json.dumps(_doc()).encode())
        path.symlink_to(good)
    else:
        body = {"unknown-key": json.dumps(_doc(roles={"qa": {"modle": "x"}})).encode(),
                "corrupt": b'{"schema": ', "list": b"[]", "empty": b"",
                "oversize": json.dumps(_doc()).encode() + b" " * (70 * 1024),
                "not-utf8": json.dumps(_doc()).encode().replace(b"sonnet", b"son\xffnet"),
                "secret-in-model": json.dumps(
                    _doc(default={"model": "sk-ant-api03-AbCdEf0123456789xyz"})).encode()}[label]
        path.write_bytes(body)
    rc, out = drive.run()
    assert rc == 1 and out["halt"] == "AMBIGUOUS", (label, out)
    assert "routing policy refused" in out["reason"], out
    assert "AbCdEf0123456789xyz" not in json.dumps(out)
    assert drive.launcher.calls == []
    assert not (drive.store.dir / ROUTING_PIN_FILE).exists()
    assert not (drive.root / "n1").exists()          # no run was created
    assert [n.lifecycle.value for n in drive.store.read_state().nodes] == ["READY"]


def _bound_project(drive):
    """One drive that binds and pins the project's policy and closes it; returns the policy file
    and the pin's bytes."""
    policy_file = _policy(drive.store)
    rc, out = drive.run()
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert len(drive.launcher.calls) >= 3
    return policy_file, (drive.store.dir / ROUTING_PIN_FILE).read_bytes()


def test_another_policy_after_the_project_was_bound_halts_by_name_and_nothing_is_started(drive):
    """Kills L7 (the project pin's digest is not compared)."""
    policy_file, pin_bytes = _bound_project(drive)
    _policy(drive.store, _doc(default={"model": "haiku", "effort": "low"}))
    rc, out = drive.run("sB")
    assert rc == 1 and out["halt"] == "AMBIGUOUS" and "routing policy changed" in out["reason"], out
    assert drive.launcher.calls == []                      # nothing was typed, not even a worktree
    assert (drive.store.dir / ROUTING_PIN_FILE).read_bytes() == pin_bytes


def test_a_policy_that_is_gone_after_the_project_was_bound_halts_by_name(drive):
    """Kills L9 (a policy that is gone is read as 'declares nothing')."""
    policy_file, pin_bytes = _bound_project(drive)
    policy_file.rename(drive.store.dir / "moved.json")
    rc, out = drive.run("sC")
    assert rc == 1 and "routing policy changed" in out["reason"] and "is gone" in out["reason"], out
    assert drive.launcher.calls == []
    assert (drive.store.dir / ROUTING_PIN_FILE).read_bytes() == pin_bytes


def test_the_same_declaration_in_other_bytes_is_the_same_policy(drive):
    """Kills L18b (the pin compares the file's bytes, not its declaration): a project whose
    policy was re-indented runs on, under the same pin."""
    policy_file, pin_bytes = _bound_project(drive)
    policy_file.write_bytes(json.dumps(json.loads(policy_file.read_bytes()), indent=4).encode())
    rc, out = drive.run("sD")
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert (drive.store.dir / ROUTING_PIN_FILE).read_bytes() == pin_bytes


@pytest.mark.parametrize("label", ["link", "directory", "fifo", "corrupt", "list", "schema",
                                   "digest-upper", "digest-newline", "oversize", "second-name",
                                   "no-pinned-at", "non-utf8"])
def test_a_project_pin_that_is_not_a_pin_halts_the_drive_and_is_never_read_as_none(
        drive, label):
    """Kills L8 (a damaged pin is read as no pin and re-pinned over) and L28 (the pin's
    `pinned_at`, which the contract names, is not required: a pin of the right digest without it
    is accepted)."""
    policy_file = _policy(drive.store)
    pin = drive.store.dir / ROUTING_PIN_FILE
    real = {"schema": ROUTING_PIN_SCHEMA, "digest": "a" * 64, "source": "/x/p.json"}
    if label == "link":
        target = drive.store.dir / "elsewhere.json"
        target.write_text(json.dumps(real))
        pin.symlink_to(target)
    elif label == "directory":
        pin.mkdir()
    elif label == "fifo":
        os.mkfifo(pin)
    elif label == "second-name":
        pin.write_text(json.dumps(real))
        os.link(pin, drive.store.dir / "another-name.json")
    else:
        right = {**real, "digest": routing.load(policy_file).digest}   # would bind, but for the shape
        pin.write_bytes({
            "no-pinned-at": json.dumps(right).encode(),
            "non-utf8": json.dumps({**right, "pinned_at": "x"}).encode().replace(b"x", b"\xff"),
            "corrupt": b'{"schema": ', "list": b"[]",
            "schema": json.dumps({**real, "schema": "x"}).encode(),
            "digest-upper": json.dumps({**real, "digest": "A" * 64}).encode(),
            "digest-newline": json.dumps({**real, "digest": "a" * 64 + "\n"}).encode(),
            "oversize": json.dumps({**real, "pad": "x" * (70 * 1024)}).encode()}[label])
    before = pin.read_bytes() if pin.is_file() and not pin.is_symlink() else None
    rc, out = drive.run()
    assert rc == 1 and out["halt"] == "AMBIGUOUS" and ROUTING_PIN_FILE in out["reason"], (label, out)
    assert not (drive.root / "n1").exists()
    if before is not None:
        assert pin.read_bytes() == before


def test_the_project_pin_ceiling_is_exactly_64_kib(tmp_path):
    """Kills L18 (the bound moved by one) and L19 (widened): a pin of exactly 64 KiB that names
    the bound policy's digest is read; one byte more is refused by name."""
    repo, root, store = _world(tmp_path)
    policy_file = _policy(store)
    digest = routing.load(policy_file).digest
    real = {"schema": ROUTING_PIN_SCHEMA, "digest": digest, "source": str(policy_file),
            "pinned_at": "2026-10-14T00:00:00Z", "pad": ""}
    room = 64 * 1024 - len(json.dumps(real).encode())
    pin = store.dir / ROUTING_PIN_FILE
    pin.write_bytes(json.dumps({**real, "pad": "x" * room}).encode())
    assert pin.stat().st_size == 64 * 1024
    RecordingLauncher(root, repo).bind_routing(store.dir)             # read, same digest: bound
    pin.write_bytes(json.dumps({**real, "pad": "x" * (room + 1)}).encode())
    with pytest.raises(ValueError, match="larger than 65536 bytes"):
        RecordingLauncher(root, repo).bind_routing(store.dir)


def test_a_project_pin_without_its_policy_halts_by_name(drive):
    """Kills L9: a project that was routed and whose policy file was removed is not unrouted."""
    policy_file = _policy(drive.store)
    assert drive.run()[0] == 0
    policy_file.rename(drive.store.dir / "moved.json")
    rc, out = drive.run("sB")
    assert rc == 1 and "is gone" in out["reason"], out


def test_a_launcher_that_cannot_route_is_refused_for_a_project_that_declares_a_policy(
        tmp_path, capsys, monkeypatch):
    """Kills L12 (a launcher without `bind_routing` is driven unrouted): a custom driver whose
    launcher does not know the hook must halt, by name, with nothing started."""
    drive = Drive(tmp_path, capsys, monkeypatch, factory="build_plain_driver")
    _policy(drive.store)
    rc, out = drive.run()
    assert rc == 1 and out["halt"] == "AMBIGUOUS", out
    assert "cannot route the runs it starts" in out["reason"], out
    # without a declaration the same launcher is driven as before (the factory is what it is)
    (drive.store.dir / ROUTING_POLICY_FILE).rename(drive.store.dir / "not-a-policy.json")
    rc, out = drive.run("sB")
    assert "cannot route" not in out.get("reason", ""), out


def test_a_pinned_project_is_refused_for_a_launcher_that_cannot_route_whatever_became_of_its_policy(
        drive):
    """Kills L23 (only the policy file is looked for: the pin-only case). The project was bound by
    a routing launcher, its policy file has since gone, and a custom driver without
    `bind_routing` is registered: driven unrouted would be the silent failure."""
    policy_file, _pin = _bound_project(drive)
    policy_file.rename(drive.store.dir / "moved.json")
    (drive.store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_launcher_routing:build_plain_driver",
        "path": str(Path(__file__).resolve().parent)}))
    rc, out = drive.run("sB")
    assert rc == 1 and out["halt"] == "AMBIGUOUS", out
    assert "is bound to a routing policy" in out["reason"] and ROUTING_PIN_FILE in out["reason"], out
    assert "cannot route the runs it starts" in out["reason"]


# --------------------------------------------------------------------------- #
# the launcher object: effective policy per project, constructor policy, bind failures
# --------------------------------------------------------------------------- #

def test_a_launcher_bound_to_one_project_carries_nothing_to_the_next(tmp_path):
    """Kills L24 (the bind keeps the previous project's policy): project A declares a policy, B
    declares nothing and has no pin; the same launcher object bound to B routes nothing, types
    every harness, and pins nothing in B."""
    repo, root, store_a = _world(tmp_path)
    policy_a = _policy(store_a)
    store_b = ProjectStore(root, "q")
    store_b.create(ProjectState(project_id="q", repo_path=str(repo), nodes=[]))
    launcher = RecordingLauncher(root, repo)
    launcher.bind_routing(store_a.dir)
    assert launcher.routing_policy == policy_a
    launcher.bind_routing(store_b.dir)
    assert launcher.routing_policy is None and launcher._routing_now() == (None, None)
    assert launcher._role_options(None) == ["--planner", "claude", "--developer", "claude",
                                            "--qa", "claude"]
    assert not (store_b.dir / ROUTING_PIN_FILE).exists()
    launcher.bind_routing(store_a.dir)                      # and back: A's own pin still holds
    assert launcher.routing_policy == policy_a


def test_a_constructor_policy_is_pinned_when_a_project_drives_the_launcher(tmp_path):
    """Kills L25 (a launcher given its policy by the constructor and driven by the governed drive
    is never pinned): the effective policy is the constructor's, the project's pin records it,
    and a launcher with another constructor policy is then refused for the project."""
    repo, root, store = _world(tmp_path)
    mine = _policy(store, name="elsewhere.json")
    launcher = RecordingLauncher(root, repo, routing_policy=mine)
    launcher.bind_routing(store.dir)
    pin = json.loads((store.dir / ROUTING_PIN_FILE).read_text(encoding="utf-8"))
    assert pin["digest"] == routing.load(mine).digest and pin["source"] == str(mine)
    other = _policy(store, _doc(default={"model": "haiku"}), name="other.json")
    with pytest.raises(ValueError, match="routing policy changed"):
        RecordingLauncher(root, repo, routing_policy=other).bind_routing(store.dir)


def test_a_launcher_whose_bind_failed_starts_nothing_even_if_it_is_used_anyway(tmp_path):
    """Kills L21 (a failed bind leaves the launcher unrouted and willing): the drive halts on the
    ValueError, but an object that is used anyway must refuse every node with the same words."""
    repo, root, store = _world(tmp_path)
    _policy(store)
    (store.dir / ROUTING_PIN_FILE).write_text(json.dumps({
        "schema": ROUTING_PIN_SCHEMA, "digest": "b" * 64, "source": "/x", "pinned_at": "t"}))
    launcher = RecordingLauncher(root, repo)
    with pytest.raises(ValueError, match="routing policy changed"):
        launcher.bind_routing(store.dir)
    node = _node(tmp_path, repo, root)
    why = launcher.prepare(node)
    assert why is not None and "routing policy changed" in why and "nothing is started" in why
    assert launcher.launch(node).verdict is RunVerdict.UNDETERMINED
    assert launcher.calls == []


def test_a_dry_run_launcher_binds_the_policy_and_pins_nothing(tmp_path):
    """Kills L30 (a dry run writes the project's pin)."""
    repo, root, store = _world(tmp_path)
    policy_file = _policy(store)
    launcher = RecordingLauncher(root, repo, dry_run=True)
    launcher.bind_routing(store.dir)
    assert launcher.routing_policy == policy_file
    assert not (store.dir / ROUTING_PIN_FILE).exists()


def test_an_unreadable_project_pin_is_refused_with_its_own_words(tmp_path):
    """Kills L29 (an unreadable pin is read as no pin): the refusal names the unreadable pin, not
    a race between two drives."""
    repo, root, store = _world(tmp_path)
    _policy(store)
    pin = store.dir / ROUTING_PIN_FILE
    pin.write_text(json.dumps({"schema": ROUTING_PIN_SCHEMA, "digest": "a" * 64,
                               "source": "/x", "pinned_at": "t"}))
    pin.chmod(0)
    try:
        if os.access(pin, os.R_OK):
            pytest.skip("this process can read a mode-000 file (superuser): NOT_RUN")
        with pytest.raises(ValueError, match=r"routing-pin\.json cannot be read"):
            RecordingLauncher(root, repo).bind_routing(store.dir)
    finally:
        pin.chmod(0o600)


def test_a_relative_constructor_path_is_made_absolute_and_forwarded_so(tmp_path, monkeypatch):
    """Kills L27 (`routing_policy=` is remembered as typed: a node run started from another
    directory would read another file)."""
    repo, root, store = _world(tmp_path)
    _policy(store, name="rel.json")
    monkeypatch.chdir(store.dir)
    launcher = RecordingLauncher(root, repo, routing_policy="rel.json")
    assert launcher.routing_policy == store.dir / "rel.json" and launcher.routing_policy.is_absolute()
    monkeypatch.chdir(tmp_path)
    node = _node(tmp_path, repo, root)
    assert launcher.prepare(node) is None
    (start,) = launcher.starts()
    assert start[start.index("--routing-policy") + 1] == str(store.dir / "rel.json")


def test_a_role_routed_by_model_only_keeps_the_launchers_harness_typed(drive):
    """Kills L22 (a role the policy mentions without a provider loses its typed harness): the
    policy routes QA's model and says nothing about its provider; the launcher's named harness
    (`claude`, not the CLI default `kimi`) must still be typed, or `hoh run` would start QA on its
    default -- the harness whose credential expired here."""
    _policy(drive.store, _doc(roles={"qa": {"model": "opus", "effort": "high"}}))
    rc, out = drive.run()
    assert rc == 0, out
    (run,) = drive.launcher.runs()
    assert run[run.index("--qa") + 1] == "claude"
    assert run[run.index("--planner") + 1] == "claude" and run[run.index("--developer") + 1] == "claude"
    ran = cli.build_parser().parse_args(_after(run))
    assert cli.profile_typed(ran.qa) and ran.qa == "claude"


# --------------------------------------------------------------------------- #
# at the node: the launcher re-reads the policy it is bound to
# --------------------------------------------------------------------------- #

def _node(tmp_path, repo, root, name="n1") -> TaskNode:
    spec = tmp_path / f"{name}.md"
    spec.write_text(f"# {name}\n", encoding="utf-8")
    return TaskNode(id=name, spec_path=str(spec), writes=[f"{name}.txt"])


def _bound_launcher(tmp_path):
    repo, root, store = _world(tmp_path)
    policy_file = _policy(store)
    launcher = RecordingLauncher(root, repo)
    launcher.bind_routing(store.dir)
    return launcher, policy_file, _node(tmp_path, repo, root)


def test_a_policy_edited_after_the_bind_starts_no_node(tmp_path):
    """Kills L4 (the per-node re-read does not compare the digest). `prepare` returns the reason
    (the node is BLOCKED with it, like the preflight's), `launch` an UNDETERMINED outcome that
    names it; nothing is typed."""
    launcher, policy_file, node = _bound_launcher(tmp_path)
    policy_file.write_bytes(json.dumps(_doc(default={"model": "haiku"})).encode())
    why = launcher.prepare(node)
    assert why is not None and "routing policy changed" in why and "nothing is started" in why
    out = launcher.launch(node)
    assert out.verdict is RunVerdict.UNDETERMINED and "routing policy changed" in out.detail
    assert launcher.calls == []


def test_a_policy_damaged_after_the_bind_starts_no_node(tmp_path):
    """Kills L13 (an unloadable policy at a node is skipped)."""
    launcher, policy_file, node = _bound_launcher(tmp_path)
    policy_file.unlink()
    policy_file.mkdir()
    why = launcher.prepare(node)
    assert why is not None and "routing policy refused" in why
    out = launcher.launch(node)
    assert out.verdict is RunVerdict.UNDETERMINED and "routing policy refused" in out.detail
    assert launcher.calls == []


def test_the_constructor_binds_a_policy_for_launchers_no_project_drives(tmp_path):
    """Kills L14 (`routing_policy=` is accepted and not applied): tools build the launcher
    directly. A policy that cannot be loaded is a refusal at construction; a valid one is typed
    into `hoh start` and `hoh run` exactly as the project-bound one is."""
    repo, root, store = _world(tmp_path)
    policy_file = _policy(store, name="elsewhere.json")
    with pytest.raises(ValueError, match="routing policy refused"):
        RecordingLauncher(root, repo, routing_policy=tmp_path / "nowhere.json")
    launcher = RecordingLauncher(root, repo, routing_policy=policy_file)
    node = _node(tmp_path, repo, root)
    assert launcher.prepare(node) is None
    (start,) = launcher.starts()
    assert start[start.index("--routing-policy") + 1] == str(policy_file)
    out = launcher.launch(node)
    assert out.verdict is RunVerdict.ACCEPTED
    (run,) = launcher.runs()
    assert run[run.index("--routing-policy") + 1] == str(policy_file) and "--qa" not in run
    # a project that declares another policy than the one the constructor bound is refused
    _policy(store, _doc(default={"model": "haiku"}))
    with pytest.raises(ValueError, match="routing policy changed"):
        launcher.bind_routing(store.dir)


@pytest.mark.parametrize("said", [
    "routing policy changed: this run is bound to policy aaaaaaaaaaaa (/x/p.json), but the "
    "policy offered declares bbbbbbbbbbbb",
    "routing policy refused: qa: the policy declares a provider that is not the harness profile",
    "routing.json is a link or not one regular file; the policy a run is bound to lives in its "
    "run directory",
], ids=["changed", "refused", "damaged-pin"])
def test_a_run_that_hoh_run_refuses_for_its_routing_is_undetermined_and_named(tmp_path, said):
    """Kills L15 (the refusal of `hoh run` falls through to a generic verdict): a run pinned to
    another policy is not started, and the node is halted with the product's own words."""
    repo, root, store = _world(tmp_path)
    policy_file = _policy(store)
    launcher = RecordingLauncher(root, repo, routing_policy=policy_file)
    node = _node(tmp_path, repo, root)
    assert launcher.prepare(node) is None
    launcher.run_says = (2, "NOTE: x\n" + said + "\n")
    out = launcher.launch(node)
    assert out.verdict is RunVerdict.UNDETERMINED and out.detail.startswith(said[:30])
    # only exit 2 is `hoh run` refusing to start: another exit code with the same words is not
    # read as that refusal (kills L26)
    launcher.run_says = (1, said + "\n")
    assert not launcher.launch(node).detail.startswith(said[:30])
