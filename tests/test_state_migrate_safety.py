"""`hoh migrate --apply` stays inside its root, parks what it replaces, and refuses what it cannot parse.

Follow-up to V3.3 P2-12 (the integration review of `hoh migrate`, findings MINOR-1..3 of the
first reviewer and m1..m3 of the second). Three things the first tests did not hold:

* a run or project directory that is a symbolic link was followed by `--apply` and written
  outside `--root`; so was a lock file that is a link (the store truncates and rewrites it);
* `--apply` on a run replaced `process.json` -- the marker of a crashed run -- with its own,
  and kept the old one nowhere;
* a record that is nested too deeply, or whose version has too many digits, ended the command
  (and the stores' read gate) in a traceback, not a refusal.

And one thing that changed underneath: P2-15 bumped the project schema to 2 with a registered
step, so `hoh migrate` now has a real record to migrate. That is run here on a version-1
project state built the way the product wrote it.

Everything happens in `tmp_path`; nothing starts Herdr or an agent; nothing is deleted (the
tests only build, rename and compare).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pydantic import ValidationError

from hoh import cli, migrations
from hoh.contracts import RunState
from hoh.migrations import Migration, Registry
from hoh.project import ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.store import LockBusy, RunStore, StoreError

NOW = "2026-10-08T00:00:00Z"
STAMP = "2026-10-08T00-00-00Z"

DEEP_TOP = "[" * 100000 + "]" * 100000
DEEP_IN_OBJECT = '{"schema_version": 1, "zz": ' + "[" * 100000 + "]" * 100000 + "}"
BIG_VERSION = '{"schema_version": ' + "9" * 5000 + "}"
# Valid JSON at the current version whose text no UTF-8 encoder takes (a lone surrogate): the
# digest of the record raises UnicodeEncodeError, a ValueError, after the parse succeeded.
LONE_SURROGATE = '{"schema_version": 2, "zz": "\\ud800"}'
PATHOLOGICAL = {"deep-top": DEEP_TOP, "deep-in-object": DEEP_IN_OBJECT, "huge-version": BIG_VERSION,
                "lone-surrogate": LONE_SURROGATE}


def _run_state(run_id: str) -> RunState:
    return RunState(run_id=run_id, repo_path="/tmp/repo", project_name="demo",
                    spec_path="/tmp/spec.md", spec_digest="s", policy_digest="p",
                    profile_digest="h")


def _runs_one_version_up(monkeypatch) -> int:
    """The run registry one version up through one identity step (the run chain is empty
    in the product); returns the version the product was at."""
    before = migrations.STATE_REGISTRIES["run"].current_version
    reg = Registry(before)
    reg.register(Migration(before, before + 1, "run-step", "identity, for the test",
                           lambda record: dict(record)))
    monkeypatch.setitem(migrations.STATE_REGISTRIES, "run", reg)
    return before


def _v1_project(root: Path, project_id: str) -> None:
    """A project state as the product wrote it at schema version 1: the real writer, then the
    shape the version-1 writer produced (no activity log, version 1)."""
    store = ProjectStore(root, project_id)
    store.write_state(ProjectState(
        project_id=project_id, repo_path="/tmp/repo",
        nodes=[TaskNode(id="a"), TaskNode(id="b", dependencies=["a"], writes=["x.py"])]))
    doc = json.loads(store.state_path.read_text(encoding="utf-8"))
    del doc["activity_events"]
    doc["schema_version"] = 1
    store.state_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _tree(base: Path) -> dict:
    """Everything under `base`, links not followed: path -> ('link', target) | ('dir',) |
    ('file', bytes, inode, mtime_ns)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(base).as_posix()
            if path.is_symlink():
                out[rel] = ("link", os.readlink(path))
            elif path.is_dir():
                out[rel] = ("dir",)
            else:
                st = path.stat()
                out[rel] = ("file", path.read_bytes(), st.st_ino, st.st_mtime_ns)
    return out


def _migrate(root: Path, capsys, *argv: str) -> tuple[int, dict | None, str]:
    capsys.readouterr()
    rc = cli.main(["--root", str(root), "migrate", *argv])
    out, err = capsys.readouterr()
    return rc, (json.loads(out) if out.strip() else None), err


# --- P2-15 made the project chain real: a version-1 state, end to end -------------------------


def test_a_version_1_project_state_is_refused_by_the_gate_until_hoh_migrate_applies_the_step(
        tmp_path, capsys):
    root = tmp_path / "runs"
    RunStore(root, "r1").write_state(_run_state("r1"))
    _v1_project(root, "demo")
    path = root / "projects" / "demo" / "project.json"
    old = path.read_bytes()
    old_doc = json.loads(old)
    assert old_doc["schema_version"] == 1 and "activity_events" not in old_doc

    # The gate: a version-1 state is not read as current, and the refusal names the command.
    with pytest.raises(StoreError, match="hoh migrate --apply"):
        ProjectStore(root, "demo").read_state()
    with pytest.raises(StoreError, match="hoh migrate --apply"):
        ProjectStore(root, "demo").write_state(ProjectState(project_id="demo", repo_path="/tmp/repo"))
    assert path.read_bytes() == old

    # The plan: one FORWARD record through the registered step, and it writes nothing.
    before = _tree(tmp_path)
    rc, plan, err = _migrate(root, capsys, "--now", NOW)
    assert (rc, err) == (0, "") and plan["dry_run"] is True and plan["applied"] is False
    assert plan["to_versions"]["project"] == 2
    forward = [r for r in plan["records"] if r["direction"] == "FORWARD"]
    assert [(r["kind"], r["id"], r["from_version"], r["to_version"], r["steps"]) for r in forward] == [
        ("project", "demo", 1, 2, ["project-add-activity-events"])]
    assert [r["direction"] for r in plan["records"] if r["kind"] == "run"] == ["NONE"]
    assert _tree(tmp_path) == before

    # The apply: the old bytes parked, the new form one version up with an empty log and
    # nothing else changed, readable again.
    rc, done, err = _migrate(root, capsys, "--apply", "--now", NOW)
    assert (rc, err) == (0, "") and done["applied"] is True
    assert (path.parent / f"project.json.v1.{STAMP}").read_bytes() == old
    new_doc = json.loads(path.read_text(encoding="utf-8"))
    assert new_doc["schema_version"] == 2 and new_doc["activity_events"] == []
    assert {k: v for k, v in new_doc.items() if k not in ("schema_version", "activity_events")} == {
        k: v for k, v in old_doc.items() if k != "schema_version"}
    state = ProjectStore(root, "demo").read_state()
    assert state.activity_events == [] and [n.id for n in state.nodes] == ["a", "b"]
    assert [r["digest_after"] for r in done["records"] if r["kind"] == "project"] == [
        migrations.record_digest(new_doc)]

    # The control: a second apply finds nothing to do and writes nothing.
    after = _tree(tmp_path)
    rc, again, _ = _migrate(root, capsys, "--apply", "--now", "2026-10-08T00:00:01Z")
    assert rc == 0 and again["counts"]["forward"] == 0 and again["applied"] is False
    assert _tree(tmp_path) == after


# --- MINOR-1 / m3: a link out of the root is refused, never followed ---------------------------


def _estate_with_a_forward_run(base: Path) -> Path:
    """`base/root` with a sound run `ok` that an apply would migrate (the run registry is
    one version up in the test) -- so a refusal elsewhere visibly stops it too."""
    root = base / "root"
    RunStore(root, "ok").write_state(_run_state("ok"))
    return root


def _case_run_dir_link(base: Path, root: Path) -> str:
    RunStore(base / "outside", "fx").write_state(_run_state("fx"))
    os.symlink(base / "outside" / "fx", root / "link")
    return "link"


def _case_run_dir_link_inside_the_root(base: Path, root: Path) -> str:
    # Not an escape, but a second name for a run that exists: still a link, still refused.
    os.symlink(root / "ok", root / "alias")
    return "alias"


def _case_run_state_file_link(base: Path, root: Path) -> str:
    RunStore(root, "a").write_state(_run_state("a"))
    (base / "outside").mkdir()
    os.rename(root / "a" / "state.json", base / "outside" / "state.json")
    os.symlink(base / "outside" / "state.json", root / "a" / "state.json")
    return "a"


def _case_run_lock_file_link(base: Path, root: Path) -> str:
    RunStore(root, "a").write_state(_run_state("a"))
    (base / "outside").mkdir()
    (base / "outside" / "victim.txt").write_text("precious\n")
    os.symlink(base / "outside" / "victim.txt", root / "a" / ".lock")
    return "a"


def _case_run_process_lock_file_link(base: Path, root: Path) -> str:
    RunStore(root, "a").write_state(_run_state("a"))
    (base / "outside").mkdir()
    (base / "outside" / "victim.txt").write_text("precious\n")
    os.symlink(base / "outside" / "victim.txt", root / "a" / ".process.lock")
    return "a"


def _case_project_dir_link(base: Path, root: Path) -> str:
    _v1_project(base / "outside", "pdir")
    (root / "projects").mkdir()
    os.symlink(base / "outside" / "projects" / "pdir", root / "projects" / "pl")
    return "pl"


def _case_projects_dir_link(base: Path, root: Path) -> str:
    _v1_project(base / "outside", "p1")
    os.symlink(base / "outside" / "projects", root / "projects")
    return "p1"


def _case_project_file_link(base: Path, root: Path) -> str:
    _v1_project(root, "p1")
    (base / "outside").mkdir()
    os.rename(root / "projects" / "p1" / "project.json", base / "outside" / "project.json")
    os.symlink(base / "outside" / "project.json", root / "projects" / "p1" / "project.json")
    return "p1"


def _case_project_lock_file_link(base: Path, root: Path) -> str:
    _v1_project(root, "p1")
    (base / "outside").mkdir()
    (base / "outside" / "victim.txt").write_text("precious\n")
    os.symlink(base / "outside" / "victim.txt", root / "projects" / "p1" / ".lock")
    return "p1"


LINK_CASES = {
    "run directory": _case_run_dir_link,
    "run directory, inside the root": _case_run_dir_link_inside_the_root,
    "run state file": _case_run_state_file_link,
    "run .lock": _case_run_lock_file_link,
    "run .process.lock": _case_run_process_lock_file_link,
    "project directory": _case_project_dir_link,
    "projects directory": _case_projects_dir_link,
    "project file": _case_project_file_link,
    "project .lock": _case_project_lock_file_link,
}


def test_a_link_that_leads_out_of_the_root_is_refused_and_nothing_is_written(
        tmp_path, capsys, monkeypatch):
    _runs_one_version_up(monkeypatch)
    failures = []
    for number, (label, build) in enumerate(LINK_CASES.items()):
        base = tmp_path / f"case{number}"
        root = _estate_with_a_forward_run(base)
        refused_id = build(base, root)
        before = _tree(base)
        for argv in ((), ("--apply", "--now", NOW)):
            rc, report, err = _migrate(root, capsys, *argv)
            ids = {r["id"] for r in (report or {}).get("refused", [])}
            if not (rc == 1 and err == "" and report is not None and refused_id in ids
                    and report["applied"] is False):
                failures.append(f"{label} {argv}: rc={rc} refused={sorted(ids)} err={err!r}")
            if _tree(base) != before:      # base holds the root AND everything outside it
                failures.append(f"{label} {argv}: something under the case changed")
    assert not failures, "\n".join(failures)

    # The control: the same estate without the link migrates, so the refusals above are the
    # links' doing and the apply they stopped was a real one.
    control = _estate_with_a_forward_run(tmp_path / "control")
    rc, report, _ = _migrate(control, capsys, "--apply", "--now", NOW)
    assert rc == 0 and report["applied"] is True and report["counts"]["forward"] == 1


def test_confine_judges_every_path_by_its_components_and_by_where_it_resolves(tmp_path):
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "a" / "state.json").write_text("{}")
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", root / "out")
    os.symlink(root / "a", root / "alias")
    migrations.confine(root)                                        # the root itself
    migrations.confine(root, root / "a", root / "a" / "state.json")  # real, inside
    migrations.confine(root, root / "not-yet" / "state.json")        # nothing there yet is fine
    refused = [
        root / "out",                    # a link out
        root / "out" / "x.json",         # through a link out
        root / "alias",                  # a link that stays inside is still a link
        root / "alias" / "state.json",
        root / "a" / ".." / ".." / "elsewhere",     # lexically under the root, resolves out
        tmp_path / "elsewhere",          # not under the root at all
    ]
    for path in refused:
        with pytest.raises(migrations.MigrationError):
            migrations.confine(root, path)
    with pytest.raises(migrations.MigrationError):                   # one bad path refuses the call
        migrations.confine(root, root / "a", root / "out")
    # The root may itself be reached through a link: the operator named it.
    os.symlink(root, tmp_path / "front")
    migrations.confine(tmp_path / "front", tmp_path / "front" / "a")


def test_a_directory_swapped_for_a_link_after_the_plan_is_caught_before_the_write(
        tmp_path, capsys, monkeypatch):
    _runs_one_version_up(monkeypatch)
    root = tmp_path / "root"
    RunStore(root, "r1").write_state(_run_state("r1"))
    outside = tmp_path / "outside"
    outside.mkdir()
    swapped = []

    real = RunStore.running_process

    def swap_then_look(self):
        # Between the plan and the write: the run directory becomes a link to a directory
        # elsewhere (the pre-check is the one thing that runs in that window).
        if not swapped:
            os.rename(root / "r1", outside / "r1")
            os.symlink(outside / "r1", root / "r1")
            swapped.append(True)
        return real(self)

    monkeypatch.setattr(RunStore, "running_process", swap_then_look)
    rc, out, err = _migrate(root, capsys, "--apply", "--now", NOW)
    assert swapped == [True]
    assert rc == 2 and out is None and err.startswith("migrate refused: ")
    assert sorted(p.name for p in (outside / "r1").iterdir()) == [
        "logs", "receipts", "results", "state.json"]     # no lock, no parked copy, no rewrite
    assert json.loads((outside / "r1" / "state.json").read_text())["schema_version"] == 1


# --- MINOR-3 / m2: process.json is not replaced --------------------------------------------------


def test_apply_does_not_replace_a_runs_process_marker(tmp_path, capsys, monkeypatch):
    _runs_one_version_up(monkeypatch)
    root = tmp_path / "root"
    for rid in ("crashed", "clean"):
        RunStore(root, rid).write_state(_run_state(rid))
    marker = root / "crashed" / "process.json"
    marker.write_text('{"pid": 4242, "state": "running"}\n', encoding="utf-8")
    old = (marker.read_bytes(), marker.stat().st_ino, marker.stat().st_mtime_ns)

    seen = []
    real = migrations.migrate

    def spy(path, **kwargs):
        if not kwargs.get("dry_run", True):
            run_id = Path(path).name
            held = RunStore(root, run_id).running_process() is not None
            seen.append((run_id, held, marker.read_bytes()))
        return real(path, **kwargs)

    monkeypatch.setattr(migrations, "migrate", spy)
    rc, report, err = _migrate(root, capsys, "--apply", "--now", NOW)
    assert (rc, err) == (0, "") and report["applied"] is True and report["counts"]["forward"] == 2
    # The write happened with the run's process lock held (a `hoh run` starting then is
    # refused) and with the old marker still in place.
    assert seen == [("clean", True, old[0]), ("crashed", True, old[0])]
    # And afterwards: the same bytes in the same inode, untouched; no marker invented.
    assert (marker.read_bytes(), marker.stat().st_ino, marker.stat().st_mtime_ns) == old
    assert not (root / "clean" / "process.json").exists()
    for rid in ("crashed", "clean"):
        assert json.loads((root / rid / "state.json").read_text())["schema_version"] == 2
        assert (root / rid / f"state.json.v1.{STAMP}").exists()
        assert RunStore(root, rid).running_process() is None       # the lock is released


def test_the_process_lock_claims_the_run_without_a_marker_and_the_marker_is_still_a_lock_plus_marker(
        tmp_path):
    root = tmp_path / "runs"
    store = RunStore(root, "r1")
    store.write_state(_run_state("r1"))
    with store.process_lock():
        assert store.running_process() is not None
        assert not store.process_path.exists()
        with pytest.raises(LockBusy):
            with RunStore(root, "r1").process_lock():
                pass
        with pytest.raises(LockBusy):
            with RunStore(root, "r1").process_marker():
                pass
    assert store.running_process() is None and not store.process_path.exists()

    with store.process_marker():
        running = json.loads(store.process_path.read_text())
        assert running["state"] == "running" and running["pid"] == os.getpid()
        assert store.running_process() is not None
        with pytest.raises(LockBusy):
            with RunStore(root, "r1").process_lock():
                pass
    ended = json.loads(store.process_path.read_text())
    assert ended["state"] == "ended" and ended["pid"] == os.getpid() and "ended" in ended
    assert store.running_process() is None


# --- MINOR-2 / m1: a record no parser takes is a refusal, not a traceback ----------------------------


def test_a_record_no_parser_takes_is_a_refusal_not_a_traceback(tmp_path, capsys, monkeypatch):
    _runs_one_version_up(monkeypatch)
    failures = []
    for number, (label, payload) in enumerate(PATHOLOGICAL.items()):
        for kind in ("run", "project"):
            base = tmp_path / f"case{number}{kind}"
            root = _estate_with_a_forward_run(base)
            if kind == "run":
                (root / "bad").mkdir()
                (root / "bad" / "state.json").write_text(payload, encoding="utf-8")
            else:
                (root / "projects" / "bad").mkdir(parents=True)
                (root / "projects" / "bad" / "project.json").write_text(payload, encoding="utf-8")
            before = _tree(base)
            for argv in ((), ("--apply", "--now", NOW)):
                try:
                    rc, report, err = _migrate(root, capsys, *argv)
                except BaseException as exc:         # a traceback is what this test is about
                    failures.append(f"{label} {kind} {argv}: raised {type(exc).__name__}")
                    continue
                refused = (report or {}).get("refused", [])
                # What the parse itself cannot take is named as such; a valid document whose
                # text no encoder takes (the surrogate) is refused further on, under its own message.
                named = label == "lone-surrogate" or all("not readable JSON" in r["reason"] for r in refused)
                if not (rc == 1 and err == "" and [r["id"] for r in refused] == ["bad"] and named
                        and report["applied"] is False and report["counts"]["forward"] >= 1):
                    failures.append(f"{label} {kind} {argv}: rc={rc} refused={refused} err={err!r}")
            if _tree(base) != before:                # the sound forward run was not applied either
                failures.append(f"{label} {kind}: something changed")
    assert not failures, "\n".join(failures)


def test_a_step_that_overflows_the_stack_is_a_refusal_not_a_traceback(tmp_path, capsys, monkeypatch):
    root = tmp_path / "runs"
    _v1_project(root, "demo")

    def too_deep(record, *, registry):
        raise RecursionError("too deep (stub)")

    monkeypatch.setattr(migrations, "apply_steps", too_deep)
    before = _tree(tmp_path)
    for argv in ((), ("--apply", "--now", NOW)):
        rc, report, err = _migrate(root, capsys, *argv)
        assert rc == 1 and err == "" and report["applied"] is False
        assert [r["id"] for r in report["refused"]] == ["demo"]
        assert "too deep (stub)" in report["refused"][0]["reason"]
        assert _tree(tmp_path) == before


def test_the_read_gate_refuses_a_record_no_parser_takes_with_a_store_error(tmp_path, capsys):
    failures = []
    for label, payload in PATHOLOGICAL.items():
        root = tmp_path / label
        (root / "a").mkdir(parents=True)
        (root / "a" / "state.json").write_text(payload, encoding="utf-8")
        (root / "projects" / "p").mkdir(parents=True)
        (root / "projects" / "p" / "project.json").write_text(payload, encoding="utf-8")
        # A damaged parked copy has always surfaced the model's own ValidationError (the
        # gate leaves text that is not a usable object "to the model's own error"); that
        # stays. What must not happen is the parser's exception getting out instead.
        reads = {
            "run read_state": (lambda: RunStore(root, "a").read_state(), (StoreError,)),
            "run read_parked_state": (lambda: RunStore(root, "a").read_parked_state(
                root / "a" / "state.json"), (StoreError, ValidationError)),
            "project read_state": (lambda: ProjectStore(root, "p").read_state(), (StoreError,)),
            "project write_state": (lambda: ProjectStore(root, "p").write_state(
                ProjectState(project_id="p", repo_path="/tmp/repo")), (StoreError,)),
        }
        for name, (read, refusals) in reads.items():
            try:
                read()
                failures.append(f"{label} {name}: read")
            except refusals:
                pass
            except BaseException as exc:
                failures.append(f"{label} {name}: {type(exc).__name__}")
        capsys.readouterr()
        try:
            rc = cli.main(["--root", str(root), "list"])
            listing = capsys.readouterr().out
            if not (rc == 0 and "UNREADABLE" in listing):
                failures.append(f"{label} hoh list: rc={rc} {listing!r}")
        except BaseException as exc:
            failures.append(f"{label} hoh list: {type(exc).__name__}")
    assert not failures, "\n".join(failures)


def test_f06_a_record_swapped_for_one_no_encoder_takes_during_the_apply_is_a_refusal_not_a_traceback(
        tmp_path, capsys, monkeypatch):
    _runs_one_version_up(monkeypatch)
    root = _estate_with_a_forward_run(tmp_path)
    _v1_project(root, "demo")
    real = RunStore.running_process

    def swap_the_project_record(self):
        found = real(self)
        (root / "projects" / "demo" / "project.json").write_text(LONE_SURROGATE, encoding="utf-8")
        return found

    monkeypatch.setattr(RunStore, "running_process", swap_the_project_record)
    rc, report, err = _migrate(root, capsys, "--apply", "--now", NOW)
    assert rc == 2 and report is None
    assert err.startswith("migrate refused:") and "already migrated: ['run ok']" in err
    assert "rerun resumes" in err
