"""`hoh migrate` and the stores' version gate: the migrations, run on the product's own state.

V3.3 P2-12. `src/hoh/migrations.py` existed and was tested against synthetic records, and
nothing in the product used it. Two things use it now, and every test here goes through
the product, never through `migrations.migrate` alone:

* `hoh migrate [--apply]` (`hoh.cli.main`) plans, and with `--apply` rewrites, the one
  record each real `RunStore` / `ProjectStore` keeps;
* `RunStore.read_state` / `write_state` / `read_parked_state` and the same pair on
  `ProjectStore` read a record only at the current schema version.

No schema step is registered (the current schema is the first one), so the forward path
is exercised with a registry that is patched in for the test; `STATE_REGISTRIES` is the
single place that patch lands. Each refusal sits next to a control that changes only the
condition under test. All filesystem work happens in `tmp_path`.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hoh import cli, contracts, migrations
from hoh.contracts import RunState
from hoh.migrations import Migration, Registry
from hoh.project import PROJECT_SCHEMA_VERSION, ProjectState
from hoh.projectstore import ProjectStore
from hoh.store import RunStore, StoreError

ROOT = Path(__file__).resolve().parent.parent

from conftest import PROGRAM_REGISTER, needs_evidence  # noqa: E402

NOW = "2026-10-08T00:00:00Z"
STAMP = "2026-10-08T00-00-00Z"


def _state(run_id: str = "r1") -> RunState:
    return RunState(run_id=run_id, repo_path="/tmp/repo", project_name="demo",
                    spec_path="/tmp/spec.md", spec_digest="s", policy_digest="p",
                    profile_digest="h")


def _estate(tmp_path: Path, runs=("r1", "r2"), projects=("p1",)) -> Path:
    """A root holding real stores written through the stores' own write paths."""
    root = tmp_path / "runs"
    for rid in runs:
        RunStore(root, rid).write_state(_state(rid))
    for pid in projects:
        ProjectStore(root, pid).write_state(ProjectState(project_id=pid, repo_path=str(tmp_path)))
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _cur(kind: str) -> int:
    """The version the product itself is at, read from its registry. Nothing in this
    file hard-codes it, so the schema change that registers the next real step needs
    no edit here."""
    return migrations.STATE_REGISTRIES[kind].current_version


def _two_versions(monkeypatch) -> dict[str, int]:
    """Both registries one version up through one identity step each; returns the
    versions the product was at before the patch."""
    before = {kind: _cur(kind) for kind in ("run", "project")}
    for kind, cur in before.items():
        reg = Registry(cur)
        reg.register(Migration(cur, cur + 1, f"{kind}-step", "identity, for the test",
                               lambda record: dict(record)))
        monkeypatch.setitem(migrations.STATE_REGISTRIES, kind, reg)
    return before


def _set_version(path: Path, version) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["schema_version"] = version
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _migrate(root: Path, capsys, *argv: str) -> tuple[int, dict | None, str]:
    capsys.readouterr()
    rc = cli.main(["--root", str(root), "migrate", *argv])
    out, err = capsys.readouterr()
    return rc, (json.loads(out) if out.strip() else None), err


# --- the command on current state --------------------------------------------------


def test_a_dry_run_reports_every_real_state_and_writes_nothing(tmp_path, capsys):
    root = _estate(tmp_path)
    before = _snapshot(root)
    rc, report, err = _migrate(root, capsys)
    assert (rc, err) == (0, "")
    assert report["dry_run"] is True and report["applied"] is False
    assert report["to_versions"] == {"run": _cur("run"), "project": _cur("project")}
    assert [(r["kind"], r["id"], r["file"], r["direction"]) for r in report["records"]] == [
        ("project", "p1", "project.json", "NONE"),
        ("run", "r1", "state.json", "NONE"),
        ("run", "r2", "state.json", "NONE")]
    for r in report["records"]:
        assert (r["from_version"], r["to_version"]) == (_cur(r["kind"]),) * 2
        assert r["digest_before"] == r["digest_after"] and r["preserved_to"] is None
    assert report["refused"] == [] and report["counts"] == {"records": 3, "forward": 0, "refused": 0}
    on_disk = json.loads((root / "r1" / "state.json").read_text(encoding="utf-8"))
    digest = next(r["digest_before"] for r in report["records"] if r["id"] == "r1")
    assert digest == migrations.record_digest(on_disk)
    assert _snapshot(root) == before


def test_apply_with_nothing_to_migrate_touches_nothing(tmp_path, capsys):
    root = _estate(tmp_path)
    before = _snapshot(root)
    rc, report, _ = _migrate(root, capsys, "--apply", "--now", NOW)
    assert rc == 0 and report["applied"] is False and report["counts"]["forward"] == 0
    assert _snapshot(root) == before            # no lock file, no parked copy, no rewrite


def test_only_the_state_record_is_considered(tmp_path, capsys):
    root = _estate(tmp_path, runs=("r1",), projects=())
    (root / "r1" / "results").mkdir(exist_ok=True)
    (root / "r1" / "results" / "unversioned.json").write_text('{"x": 1}', encoding="utf-8")
    (root / "r1" / "evidence.json").write_text('{"y": 2}', encoding="utf-8")
    rc, report, _ = _migrate(root, capsys)
    assert rc == 0 and [r["file"] for r in report["records"]] == ["state.json"]


# --- the forward path, through the stores ------------------------------------------


def test_a_forward_migration_is_planned_then_parked_and_written(tmp_path, capsys, monkeypatch):
    root = _estate(tmp_path)
    old = (root / "r1" / "state.json").read_bytes()
    old_project = (root / "projects" / "p1" / "project.json").read_bytes()
    v = _two_versions(monkeypatch)
    up = {kind: n + 1 for kind, n in v.items()}

    # The registry is one version up; the stores read nothing below it, and say which command.
    with pytest.raises(StoreError, match="hoh migrate"):
        RunStore(root, "r1").read_state()
    with pytest.raises(StoreError, match="hoh migrate"):
        ProjectStore(root, "p1").read_state()

    before = _snapshot(root)
    rc, plan, _ = _migrate(root, capsys)
    assert rc == 0 and plan["dry_run"] is True and plan["applied"] is False
    assert plan["to_versions"] == up
    assert plan["counts"] == {"records": 3, "forward": 3, "refused": 0}
    assert {r["direction"] for r in plan["records"]} == {"FORWARD"}
    assert plan["records"][1]["steps"] == ["run-step"]
    assert all(r["preserved_to"] is None for r in plan["records"])
    assert _snapshot(root) == before                                   # a plan is not a write

    rc, done, _ = _migrate(root, capsys, "--apply", "--now", NOW)
    assert rc == 0 and done["applied"] is True and done["dry_run"] is False
    by_id = {(r["kind"], r["id"]): r for r in done["records"]}
    parked = root / "r1" / f"state.json.v{v['run']}.{STAMP}"
    assert by_id[("run", "r1")]["preserved_to"] == parked.name
    assert by_id[("project", "p1")]["preserved_to"] == f"project.json.v{v['project']}.{STAMP}"
    assert parked.read_bytes() == old
    assert (root / "projects" / "p1" / f"project.json.v{v['project']}.{STAMP}").read_bytes() == old_project
    for kind, rid, path in (("run", "r1", root / "r1" / "state.json"),
                            ("run", "r2", root / "r2" / "state.json"),
                            ("project", "p1", root / "projects" / "p1" / "project.json")):
        now_doc = json.loads(path.read_text(encoding="utf-8"))
        assert now_doc["schema_version"] == up[kind]
        assert by_id[(kind, rid)]["digest_after"] == migrations.record_digest(now_doc)
        assert by_id[(kind, rid)]["digest_before"] != by_id[(kind, rid)]["digest_after"]
    assert RunStore(root, "r1").read_state().run_id == "r1"            # readable now
    assert ProjectStore(root, "p1").read_state().project_id == "p1"
    assert not list(root.rglob("*.tmp*")) and not list(root.rglob("*.partial*"))
    # A parked copy is history: behind the current version it is refused, and the
    # message does not send the reader to a command that cannot help.
    with pytest.raises(StoreError, match="parked copy") as refused:
        RunStore(root, "r1").read_parked_state(parked)
    assert "--apply" not in str(refused.value)

    # The control: a second apply finds nothing to do and writes nothing.
    after = _snapshot(root)
    rc, again, _ = _migrate(root, capsys, "--apply", "--now", NOW)
    assert rc == 0 and again["counts"]["forward"] == 0
    assert _snapshot(root) == after


def test_nothing_the_stores_hold_is_removed_by_an_apply(tmp_path, capsys, monkeypatch):
    root = _estate(tmp_path, runs=("r1",), projects=())
    (root / "r1" / "evidence.json").write_text('{"y": 2}', encoding="utf-8")
    store = RunStore(root, "r1")
    store.write_state(store.read_state())                              # one parked predecessor
    before = _snapshot(root)
    _two_versions(monkeypatch)
    rc, _, _ = _migrate(root, capsys, "--apply", "--now", NOW)
    assert rc == 0
    after = _snapshot(root)
    assert set(before) < set(after)                                    # strictly more files
    for name, content in before.items():
        if name != "r1/state.json":
            assert after[name] == content                              # everything else, byte for byte


# --- refusals: the future, the unversioned, one bad record, a busy store ------------


def test_a_state_from_the_future_is_refused_by_the_command_and_by_every_read_path(
        tmp_path, capsys):
    root = _estate(tmp_path)
    rpath = root / "r1" / "state.json"
    ppath = root / "projects" / "p1" / "project.json"
    run_future, project_future = _cur("run") + 1, _cur("project") + 1
    _set_version(rpath, run_future)
    _set_version(ppath, project_future)
    parked = root / "r1" / "state.json.v1.old"
    parked.write_bytes(rpath.read_bytes())
    # The control that makes the gate necessary: the model alone reads both as current.
    assert RunState.model_validate_json(rpath.read_text(encoding="utf-8")).schema_version == run_future
    assert ProjectState.model_validate_json(
        ppath.read_text(encoding="utf-8")).schema_version == project_future

    before = _snapshot(root)
    for argv in ((), ("--apply", "--now", NOW)):
        rc, report, _ = _migrate(root, capsys, *argv)
        assert rc == 1 and report["counts"]["refused"] == 2
        assert {r["id"] for r in report["refused"]} == {"r1", "p1"}
        assert all("FromTheFuture" in r["reason"] for r in report["refused"])
        assert [r["id"] for r in report["records"]] == ["r2"]          # the sound one is reported
    assert _snapshot(root) == before

    run, project = RunStore(root, "r1"), ProjectStore(root, "p1")
    for call in (run.read_state, lambda: run.read_parked_state(parked),
                 project.read_state, lambda: run.write_state(_state("r1")),
                 lambda: project.write_state(ProjectState(project_id="p1", repo_path="/tmp"))):
        with pytest.raises(StoreError, match="above the current"):
            call()
    assert _snapshot(root) == before                                   # a refused write wrote nothing
    # `hoh list` is a product reader too: it reports the run unreadable, not current.
    capsys.readouterr()
    assert cli.main(["--root", str(root), "list"]) == 0
    listing = capsys.readouterr().out
    assert "r1  UNREADABLE" in listing and "r2  " in listing and "r2  UNREADABLE" not in listing


def test_a_state_without_a_readable_version_is_refused_not_assumed_current(tmp_path, capsys):
    root = _estate(tmp_path, runs=("r1", "r2", "r3"), projects=("p1",))
    doc = json.loads((root / "r1" / "state.json").read_text(encoding="utf-8"))
    del doc["schema_version"]
    (root / "r1" / "state.json").write_text(json.dumps(doc), encoding="utf-8")
    _set_version(root / "r2" / "state.json", str(_cur("run")))         # a string is not a version
    _set_version(root / "r3" / "state.json", True)                     # nor is a bool
    assert RunState.model_validate_json((root / "r1" / "state.json").read_text()).schema_version == _cur("run")
    before = _snapshot(root)
    rc, report, _ = _migrate(root, capsys, "--apply", "--now", NOW)
    assert rc == 1 and {r["id"] for r in report["refused"]} == {"r1", "r2", "r3"}
    assert _snapshot(root) == before
    for rid in ("r1", "r2", "r3"):
        with pytest.raises(StoreError, match="schema_version"):
            RunStore(root, rid).read_state()
    assert ProjectStore(root, "p1").read_state().project_id == "p1"    # the control reads


def test_a_directory_the_store_cannot_name_is_a_refusal_not_a_crash(tmp_path, capsys, monkeypatch):
    root = _estate(tmp_path, runs=("r1",), projects=())
    odd = root / "has a space"
    odd.mkdir()
    (odd / "state.json").write_text("{}", encoding="utf-8")
    _two_versions(monkeypatch)
    before = _snapshot(root)
    rc, report, _ = _migrate(root, capsys, "--apply", "--now", NOW)
    assert rc == 1 and [r["id"] for r in report["refused"]] == ["has a space"]
    assert _snapshot(root) == before


def test_one_refused_record_stops_the_whole_apply(tmp_path, capsys, monkeypatch):
    root = _estate(tmp_path, runs=("r1", "r2"), projects=())
    v = _two_versions(monkeypatch)
    _set_version(root / "r2" / "state.json", v["run"] + 2)             # above the patched current
    before = _snapshot(root)
    rc, report, _ = _migrate(root, capsys, "--apply", "--now", NOW)
    assert rc == 1 and report["applied"] is False
    assert [r["id"] for r in report["refused"]] == ["r2"]
    assert _snapshot(root) == before              # r1 was forward and was not touched
    # The control: the same estate without the bad record migrates.
    _set_version(root / "r2" / "state.json", v["run"])
    rc, report, _ = _migrate(root, capsys, "--apply", "--now", NOW)
    assert rc == 0 and report["applied"] is True and report["counts"]["forward"] == 2


def test_apply_refuses_a_run_that_a_process_is_executing(tmp_path, capsys, monkeypatch):
    root = _estate(tmp_path, runs=("r1", "r2"), projects=())
    _two_versions(monkeypatch)
    # The executing run is the *second* forward one: a check made lazily, per store, would
    # have migrated r1 before it met r2.
    running = RunStore(root, "r2")
    with running.process_marker():
        before = _snapshot(root)
        rc, out, err = _migrate(root, capsys, "--apply", "--now", NOW)
        assert rc == 2 and out is None and err.startswith("migrate refused: ")
        assert "r2" in err and "executed" in err
        assert _snapshot(root) == before          # r1, also forward, was not migrated either
        # The window between the pre-check and the write: the check is blind, the process is
        # still there. The write itself holds the run's process lock, so r2 is refused (r1,
        # not running, was already done, and the message says so).
        monkeypatch.setattr(RunStore, "running_process", lambda self: None)
        r2_bytes = (root / "r2" / "state.json").read_bytes()
        rc, out, err = _migrate(root, capsys, "--apply", "--now", NOW)
        assert rc == 2 and out is None and "already being executed" in err
        assert "already migrated: ['run r1']" in err
        assert (root / "r2" / "state.json").read_bytes() == r2_bytes
        monkeypatch.undo()
        _two_versions(monkeypatch)                # undo() also took the registry patch back
        (root / "r1" / "state.json").write_bytes(before["r1/state.json"])
    rc, report, _ = _migrate(root, capsys, "--apply", "--now", NOW)   # the control: it ended
    assert rc == 0 and report["applied"] is True


def test_apply_refuses_a_locked_store_and_says_what_it_did_before(tmp_path, capsys, monkeypatch):
    root = _estate(tmp_path, runs=("r1",), projects=("p1",))
    v = _two_versions(monkeypatch)
    # A run's own lock: nothing is migrated, and nothing is left behind.
    before = _snapshot(root)
    with RunStore(root, "r1").lock():
        locked = _snapshot(root)                  # the lock file is the lock's own
        rc, out, err = _migrate(root, capsys, "--apply", "--now", NOW)
        assert rc == 2 and out is None and err.startswith("migrate refused: ")
        assert "already held" in err and "already migrated: none" in err
        assert _snapshot(root) == locked
    assert set(before) <= set(locked)
    # A project's lock: the run before it was migrated, and the message says so.
    with ProjectStore(root, "p1").lock():
        rc, out, err = _migrate(root, capsys, "--apply", "--now", NOW)
    assert rc == 2 and out is None and err.startswith("migrate refused: ")
    assert "already held" in err and "already migrated: ['run r1']" in err
    assert json.loads((root / "projects" / "p1" / "project.json").read_text())["schema_version"] == v["project"]
    rc, report, _ = _migrate(root, capsys, "--apply", "--now", NOW)   # the rerun resumes
    assert rc == 0 and {(r["kind"], r["id"]) for r in report["records"]
                        if r["direction"] == "FORWARD"} == {("project", "p1")}


def test_a_root_that_is_not_there_is_a_refusal_not_an_empty_answer(tmp_path, capsys):
    rc, out, err = _migrate(tmp_path / "no-such-root", capsys)
    assert rc == 2 and out is None and err.startswith("migrate refused: ") and "not a directory" in err
    assert not (tmp_path / "no-such-root").exists()
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    rc, out, err = _migrate(afile, capsys, "--apply")
    assert rc == 2 and out is None and err.startswith("migrate refused: ")
    empty = tmp_path / "empty"
    empty.mkdir()                                  # the control: a real, empty root is a clean answer
    rc, report, _ = _migrate(empty, capsys)
    assert rc == 0 and report["records"] == [] and report["counts"]["records"] == 0


def test_an_unusable_now_is_a_refusal_and_writes_nothing(tmp_path, capsys, monkeypatch):
    root = _estate(tmp_path)
    _two_versions(monkeypatch)
    before = _snapshot(root)
    for bad in ("not-a-time", "2026-10-08T00:00:00"):
        rc, out, err = _migrate(root, capsys, "--apply", "--now", bad)
        assert rc == 2 and out is None and err.startswith("migrate refused: ")
    assert _snapshot(root) == before


# --- the pieces underneath ---------------------------------------------------------


def test_the_state_registries_cannot_drift_from_the_models():
    run, project = migrations.STATE_REGISTRIES["run"], migrations.STATE_REGISTRIES["project"]
    assert run.current_version == contracts.SCHEMA_VERSION == RunState.model_fields["schema_version"].default
    assert project.current_version == PROJECT_SCHEMA_VERSION \
        == ProjectState.model_fields["schema_version"].default
    assert set(migrations.STATE_FILES) == set(migrations.STATE_REGISTRIES) == {"run", "project"}


def test_migrate_only_names_exactly_the_records_it_is_given(tmp_path):
    (tmp_path / "a.json").write_text('{"schema_version": 1}', encoding="utf-8")
    (tmp_path / "b.json").write_text('{"no_version": true}', encoding="utf-8")
    reg = Registry(1)
    now = __import__("datetime").datetime(2026, 10, 8, tzinfo=__import__("datetime").timezone.utc)
    report = migrations.migrate(tmp_path, registry=reg, now=now, only=["a.json"])
    assert [f.file for f in report.files] == ["a.json"]
    with pytest.raises(migrations.PlanRefused):                        # the default is unchanged
        migrations.migrate(tmp_path, registry=reg, now=now)
    (tmp_path / "link.json").symlink_to(tmp_path / "a.json")
    for bad in ("missing.json", "../a.json", str(tmp_path / "a.json"), "link.json"):
        with pytest.raises(migrations.MigrationError):
            migrations.migrate(tmp_path, registry=reg, now=now, only=[bad])


# --- what the matrix sees ----------------------------------------------------------


def test_the_command_surface_names_the_module_the_matrix_measures():
    spec = importlib.util.spec_from_file_location(
        "capability_matrix_for_migrate", ROOT / "tools" / "capability_matrix.py")
    cm = importlib.util.module_from_spec(spec)
    sys.modules["capability_matrix_for_migrate"] = cm
    spec.loader.exec_module(cm)

    graph = cm._import_graph()
    consumers = cm._reachable_from_an_entry_point("migrations", graph)
    assert "src/hoh/cli.py" in consumers

    needs_evidence(PROGRAM_REGISTER)
    register = json.loads((ROOT / PROGRAM_REGISTER.path).read_text(encoding="utf-8"))
    rows = {r["id"]: r for r in register["requirements"]}
    row = cm.classify(rows["P2-12"], cm._symbols(), ("tests/test_migrations.py", True), {}, graph)
    assert row["status"] == cm.PROVEN, (row["status"], row["evidence"])
