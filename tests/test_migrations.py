"""Durable-state migrations (V3.3 P2-12): refused from the future, preserved.

The exit condition behind this module is "kill, quota block, reboot, resume
with no duplicate accepted work", and the two ways a migration breaks it are
both silent: a record from a newer version read as if it were current, and a
rewrite that keeps no account of what it replaced. The cases here are one per
rule of the spec. Each positive case sits next to a negative control that
changes only the condition under test and shows the verdict flipping -- a
refusal that also fires on a current record, or a preservation test that
passes when nothing was migrated at all, is a check of nothing.

All filesystem work happens in `tmp_path`; `now` is a fixed datetime.
"""
from __future__ import annotations

import inspect
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

import hoh.migrations as migrations_module
from hoh.migrations import (
    AmbiguousChain,
    BackwardsMigration,
    ChainGap,
    Direction,
    FromTheFuture,
    Migration,
    MigrationError,
    PlanRefused,
    Registry,
    migrate,
    plan,
)

NOW = datetime(2026, 9, 24, 0, 0, 0, tzinfo=timezone.utc)
STAMP = "2026-09-24T00-00-00Z"


def _add_owner(record: dict) -> dict:
    record["owner"] = "controller"
    return record


def _rename_owner(record: dict) -> dict:
    record["actor"] = record.pop("owner")
    return record


def _step(frm: int, name: str | None = None, fn=lambda r: r) -> Migration:
    return Migration(from_version=frm, to_version=frm + 1,
                     name=name or f"v{frm}-to-v{frm + 1}",
                     description=f"step {frm} -> {frm + 1}", apply=fn)


def _registry(*steps: Migration) -> Registry:
    reg = Registry()
    for s in steps:
        reg.register(s)
    return reg


def _two_step() -> Registry:
    """1 -> 2 adds `owner`, 2 -> 3 renames it to `actor`: the second step
    cannot work unless it received the first step's output."""
    return _registry(_step(1, "add-owner", _add_owner),
                     _step(2, "rename-owner", _rename_owner))


def _write(root: Path, name: str, record: dict) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    """Every file under root with its bytes and mtime_ns."""
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


def _store(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    _write(root, "state.json", {"schema_version": 1, "run_id": "r1"})
    _write(root, "results/a.json", {"schema_version": 1, "run_id": "r2"})
    _write(root, "current.json", {"schema_version": 3, "run_id": "r3", "actor": "x"})
    return root


# --- the future ---------------------------------------------------------------

def test_record_from_the_future_is_refused_by_plan():
    """Catches the silent failure the spec opens with: a record written by a
    newer version planned as NONE or FORWARD and then read with its unknown
    fields dropped."""
    result = plan([{"schema_version": 4, "run_id": "r"}], registry=_two_step())
    assert result.direction is Direction.REFUSED
    assert len(result.refusals) == 1
    assert "FromTheFuture" in result.refusals[0].reason
    assert result.steps == ()


def test_future_control_record_at_current_version_is_none_not_refused():
    """Control for the future refusal: the same record one version lower is at
    `current_version` and is NONE -- catches a refusal that fires on any
    record rather than on one from the future."""
    result = plan([{"schema_version": 3, "run_id": "r"}], registry=_two_step())
    assert result.direction is Direction.NONE
    assert result.refusals == ()
    assert result.counts["NONE"] == 1


def test_future_control_record_below_current_migrates_forward():
    """Control: below current is FORWARD with the chain's steps -- catches a
    plan that calls everything not-future NONE and so never migrates."""
    result = plan([{"schema_version": 1, "run_id": "r"}], registry=_two_step())
    assert result.direction is Direction.FORWARD
    assert result.steps[0].steps == ("add-owner", "rename-owner")
    assert result.counts["FORWARD"] == 1


def test_path_from_the_future_raises_from_the_future():
    """Catches a registry that answers an empty path (read as current) for a
    start above what it knows."""
    reg = _two_step()
    with pytest.raises(FromTheFuture):
        reg.path(4)
    with pytest.raises(FromTheFuture):
        reg.path(4, 3)


def test_path_future_control_current_version_is_empty_path():
    """Control: from current_version the path is empty, not an error --
    catches a registry that refuses at the boundary instead of above it."""
    assert _two_step().path(3) == ()


def test_record_from_the_future_on_disk_is_never_touched(tmp_path):
    """Catches a migrate that migrates the readable files and skips the one
    from the future: one future record refuses the whole directory."""
    root = _store(tmp_path)
    _write(root, "newer.json", {"schema_version": 9, "run_id": "r9"})
    before = _snapshot(root)
    with pytest.raises(MigrationError) as info:
        migrate(root, registry=_two_step(), now=NOW, dry_run=False)
    assert isinstance(info.value, PlanRefused)
    assert "newer.json" in str(info.value)
    assert _snapshot(root) == before


def test_record_without_version_is_refused_not_assumed_current():
    """Catches the defaulting variant of reading the future: a record with no
    `schema_version` treated as if it were current. Control: the same record
    with the field is accepted."""
    reg = _two_step()
    assert plan([{"run_id": "r"}], registry=reg).direction is Direction.REFUSED
    assert plan([{"run_id": "r", "schema_version": "3"}],
                registry=reg).direction is Direction.REFUSED
    assert plan([{"run_id": "r", "schema_version": 3}],
                registry=reg).direction is Direction.NONE


# --- chain rules --------------------------------------------------------------

def test_two_step_path_applies_both_steps_in_order_on_the_intermediate(tmp_path):
    """Catches a two-step path collapsed into one function, or run out of
    order: the second step must receive exactly the first step's output."""
    seen: list[tuple[str, dict]] = []

    def first(r):
        seen.append(("first", dict(r)))
        return _add_owner(r)

    def second(r):
        seen.append(("second", dict(r)))
        return _rename_owner(r)

    reg = _registry(_step(1, "first", first), _step(2, "second", second))
    root = tmp_path / "s"
    _write(root, "state.json", {"schema_version": 1, "run_id": "r"})
    report = migrate(root, registry=reg, now=NOW, dry_run=False)

    assert [name for name, _ in seen] == ["first", "second"]
    assert seen[1][1] == {"schema_version": 2, "run_id": "r", "owner": "controller"}
    assert report.files[0].steps == ("first", "second")
    assert json.loads((root / "state.json").read_text()) == {
        "schema_version": 3, "run_id": "r", "actor": "controller"}


def test_two_step_control_record_one_step_behind_takes_only_the_second():
    """Control: a record already at version 2 takes only the second step --
    catches a path that always starts at the bottom of the chain."""
    assert [s.name for s in _two_step().path(2)] == ["rename-owner"]


def test_duplicate_from_version_is_refused_at_registration():
    """Catches an ambiguous chain: two steps from version 1, where which one
    runs depends on registration order."""
    reg = _registry(_step(1, "a"))
    with pytest.raises(AmbiguousChain):
        reg.register(_step(1, "b"))
    assert [s.name for s in reg.steps] == ["a"]


def test_gap_in_chain_is_refused_at_registration():
    """Catches a chain with a hole: 1 -> 2 then 3 -> 4 would leave version 2
    records with no way up and a current_version nobody can reach."""
    reg = _registry(_step(1))
    with pytest.raises(ChainGap):
        reg.register(_step(3))
    assert reg.current_version == 2


def test_chain_control_contiguous_registration_is_accepted():
    """Control for duplicate and gap: distinct, contiguous steps register (in
    either direction) -- catches a registry that refuses every second step."""
    reg = _registry(_step(2), _step(1), _step(3))
    assert [s.from_version for s in reg.steps] == [1, 2, 3]
    assert reg.current_version == 4


def test_step_that_jumps_two_versions_is_refused_at_construction():
    """Catches a step that cannot be composed or audited: 1 -> 3."""
    with pytest.raises(MigrationError):
        Migration(from_version=1, to_version=3, name="jump", description="d",
                  apply=lambda r: r)
    with pytest.raises(MigrationError):
        Migration(from_version=2, to_version=1, name="down", description="d",
                  apply=lambda r: r)


def test_step_control_one_version_up_is_constructed():
    """Control: the same step with to_version = from_version + 1 constructs --
    catches a constructor that refuses everything."""
    m = Migration(from_version=1, to_version=2, name="ok", description="d",
                  apply=lambda r: r)
    assert (m.from_version, m.to_version) == (1, 2)


def test_path_backwards_raises():
    """Catches a down migration: the ledger's correction-by-addition applies
    to data, and a path from 3 to 1 is refused."""
    with pytest.raises(BackwardsMigration):
        _two_step().path(3, 1)


def test_path_control_forward_is_answered():
    """Control: the same endpoints the other way round give the chain."""
    assert [s.from_version for s in _two_step().path(1, 3)] == [1, 2]


def test_empty_registry_current_version_is_the_contract_version():
    """Catches a registry that invents a version: with no steps it is at the
    contracts' SCHEMA_VERSION, and such a record is NONE."""
    from hoh.contracts import SCHEMA_VERSION
    reg = Registry()
    assert reg.current_version == SCHEMA_VERSION
    assert plan([{"schema_version": SCHEMA_VERSION}],
                registry=reg).direction is Direction.NONE


# --- dry run is the default ---------------------------------------------------

def test_dry_run_is_the_default_and_writes_nothing(tmp_path):
    """Catches a migration that runs because someone forgot a flag: with no
    dry_run argument every file is byte- and mtime-identical, and no new file
    appears."""
    root = _store(tmp_path)
    before = _snapshot(root)
    report = migrate(root, registry=_two_step(), now=NOW)
    assert report.dry_run is True
    assert report.direction is Direction.FORWARD
    assert _snapshot(root) == before
    assert all(f.preserved_to is None for f in report.files)
    assert inspect.signature(migrate).parameters["dry_run"].default is True


def test_dry_run_default_control_explicit_false_does_write(tmp_path):
    """Control: the same call with dry_run=False changes the directory --
    catches a dry-run test that passes because migrate never writes at all."""
    root = _store(tmp_path)
    before = _snapshot(root)
    migrate(root, registry=_two_step(), now=NOW, dry_run=False)
    assert _snapshot(root) != before


def test_dry_run_report_says_what_would_change(tmp_path):
    """Catches a dry run that says nothing useful: it reports the same steps
    and after-digests a real run produces."""
    dry_root, real_root = _store(tmp_path / "a"), _store(tmp_path / "b")
    dry = migrate(dry_root, registry=_two_step(), now=NOW)
    real = migrate(real_root, registry=_two_step(), now=NOW, dry_run=False)
    assert [(f.file, f.steps, f.digest_after) for f in dry.files] == \
        [(f.file, f.steps, f.digest_after) for f in real.files]


# --- preservation -------------------------------------------------------------

def test_real_migration_preserves_old_form_beside_the_original(tmp_path):
    """Catches a migration that overwrites and keeps no account: the versioned
    copy exists beside the original, holds the OLD bytes, and the new form is
    in place."""
    root = _store(tmp_path)
    old_state = (root / "state.json").read_bytes()
    old_result = (root / "results" / "a.json").read_bytes()
    report = migrate(root, registry=_two_step(), now=NOW, dry_run=False)

    copy = root / f"state.json.v1.{STAMP}"
    assert copy.read_bytes() == old_state
    assert (root / "results" / f"a.json.v1.{STAMP}").read_bytes() == old_result
    new = json.loads((root / "state.json").read_text())
    assert new["schema_version"] == 3 and new["actor"] == "controller"
    by_file = {f.file: f for f in report.files}
    assert by_file["state.json"].preserved_to == f"state.json.v1.{STAMP}"


def test_preserved_control_current_file_gets_no_copy_and_is_not_written(tmp_path):
    """Control: a file already at the current version gets no versioned copy
    and keeps its mtime -- catches a preservation that copies everything and
    so says nothing about what was migrated."""
    root = _store(tmp_path)
    before = (root / "current.json").stat().st_mtime_ns
    report = migrate(root, registry=_two_step(), now=NOW, dry_run=False)
    assert not list(root.glob("current.json.v*"))
    assert (root / "current.json").stat().st_mtime_ns == before
    assert {f.file: f for f in report.files}["current.json"].preserved_to is None


def test_preserved_copy_is_never_overwritten(tmp_path):
    """Catches a preservation that clobbers history: an existing copy with
    different bytes under the same name refuses the migration."""
    root = tmp_path / "s"
    _write(root, "state.json", {"schema_version": 1, "run_id": "r"})
    squatter = root / f"state.json.v1.{STAMP}"
    squatter.write_text("someone else's history", encoding="utf-8")
    with pytest.raises(MigrationError):
        migrate(root, registry=_two_step(), now=NOW, dry_run=False)
    assert squatter.read_text() == "someone else's history"
    assert json.loads((root / "state.json").read_text())["schema_version"] == 1


def test_preserved_copy_resume_control_same_bytes_are_the_same_copy(tmp_path):
    """Control: a copy with the *same* bytes, as left by a migration
    interrupted after preserving, is accepted and the migration completes --
    catches a resume after the reboot that can never finish."""
    root = tmp_path / "s"
    original = _write(root, "state.json", {"schema_version": 1, "run_id": "r"})
    (root / f"state.json.v1.{STAMP}").write_bytes(original.read_bytes())
    migrate(root, registry=_two_step(), now=NOW, dry_run=False)
    assert json.loads(original.read_text())["schema_version"] == 3


def test_preserved_copies_are_not_migrated_again(tmp_path):
    """Catches a second run that treats the history as records and "migrates"
    the preserved copies (or the stale temp files) themselves."""
    root = _store(tmp_path)
    migrate(root, registry=_two_step(), now=NOW, dry_run=False)
    (root / ".state.json.migrating.1.2").write_text("{}", encoding="utf-8")
    report = migrate(root, registry=_two_step(), now=NOW)
    assert sorted(f.file for f in report.files) == \
        ["current.json", "results/a.json", "state.json"]


def test_preserve_module_contains_no_call_that_removes_a_file():
    """Catches the house rule being broken in the code itself: the module that
    rewrites durable state names no removal, not even in a comment. Control:
    the same scan finds `os.replace`, so it is reading the right file."""
    source = Path(migrations_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("unlink", "os.remove", "rmdir", "rmtree", "removedirs",
                      "shutil.move"):
        assert forbidden not in source, forbidden
    assert "os.replace" in source


def test_failing_step_leaves_every_file_untouched(tmp_path):
    """Catches a half-applied migration: all new forms are computed before
    the first write, so a step that raises on the last file leaves the first
    one unmigrated and without a copy."""
    def boom(r):
        if r["run_id"] == "r2":
            raise ValueError("cannot migrate this one")
        return r

    root = _store(tmp_path)
    before = _snapshot(root)
    with pytest.raises(ValueError):
        migrate(root, registry=_registry(_step(1, fn=boom), _step(2)),
                now=NOW, dry_run=False)
    assert _snapshot(root) == before


def test_atomic_write_leaves_no_temp_file_behind(tmp_path):
    """Catches a write that is not write-beside-then-replace, or that leaves
    its temp file as a second, dangling record."""
    root = _store(tmp_path)
    migrate(root, registry=_two_step(), now=NOW, dry_run=False)
    assert not [p for p in root.rglob("*") if ".migrating." in p.name]


# --- idempotence --------------------------------------------------------------

def test_second_migrate_reports_none_and_writes_nothing(tmp_path):
    """Catches a migration that is not idempotent: over its own result the
    next run is NONE, and no file's bytes, mtime or existence changes."""
    root = _store(tmp_path)
    migrate(root, registry=_two_step(), now=NOW, dry_run=False)
    before = _snapshot(root)
    later = datetime(2026, 9, 25, tzinfo=timezone.utc)
    report = migrate(root, registry=_two_step(), now=later, dry_run=False)
    assert report.direction is Direction.NONE
    assert all(f.digest_before == f.digest_after for f in report.files)
    assert _snapshot(root) == before


def test_second_migrate_control_first_run_was_forward(tmp_path):
    """Control: the first run over the same store is FORWARD -- catches an
    idempotence test that passes because the store never needed migrating."""
    root = _store(tmp_path)
    assert migrate(root, registry=_two_step(), now=NOW,
                   dry_run=False).direction is Direction.FORWARD


# --- refused plans ------------------------------------------------------------

def test_refused_plan_cannot_be_applied_and_nothing_is_touched(tmp_path):
    """Catches a migrate that applies what it can and ignores the refusal:
    one unreadable record refuses the directory, and every file is byte- and
    mtime-identical, with no copy created."""
    root = _store(tmp_path)
    (root / "broken.json").write_text("{not json", encoding="utf-8")
    before = _snapshot(root)
    with pytest.raises(PlanRefused) as info:
        migrate(root, registry=_two_step(), now=NOW, dry_run=False)
    assert info.value.plan.direction is Direction.REFUSED
    assert _snapshot(root) == before


def test_refused_plan_object_refuses_to_be_applied():
    """Catches a REFUSED plan that can be carried forward as if applicable."""
    refused = plan([{"schema_version": 7}], registry=_two_step())
    assert refused.applicable is False
    with pytest.raises(PlanRefused):
        refused.require_applicable()
    ok = plan([{"schema_version": 1}], registry=_two_step())
    ok.require_applicable()                            # control: no raise


def test_refused_control_same_store_without_the_bad_record_migrates(tmp_path):
    """Control: the same store without the unreadable file migrates --
    catches a refusal that fires on every store."""
    root = _store(tmp_path)
    report = migrate(root, registry=_two_step(), now=NOW, dry_run=False)
    assert report.direction is Direction.FORWARD and report.refusals == ()


def test_plan_is_frozen_and_writes_nothing(tmp_path):
    """Catches a plan that can be edited into applicability, or a plan()
    that has side effects on the records it was handed."""
    record = {"schema_version": 1, "run_id": "r"}
    result = plan([record], registry=_two_step())
    with pytest.raises(Exception):
        result.direction = Direction.NONE              # type: ignore[misc]
    with pytest.raises(TypeError):
        result.counts["REFUSED"] = 0                   # type: ignore[index]
    assert record == {"schema_version": 1, "run_id": "r"}
    assert result.from_versions == (1,) and result.to_version == 3


# --- the report ---------------------------------------------------------------

def test_report_digests_differ_exactly_for_changed_records(tmp_path):
    """Catches an audit trail that cannot tell what changed: before/after
    digests differ for the two migrated records and are equal for the one
    already current -- the equality is the control."""
    root = _store(tmp_path)
    report = migrate(root, registry=_two_step(), now=NOW, dry_run=False)
    changed = {f.file for f in report.files if f.digest_before != f.digest_after}
    same = {f.file for f in report.files if f.digest_before == f.digest_after}
    assert changed == {"state.json", "results/a.json"}
    assert same == {"current.json"}
    assert {f.file for f in report.changed} == changed
    for f in report.files:
        assert len(f.digest_before) == 64 and len(f.digest_after) == 64
    by_file = {f.file: f for f in report.files}
    assert by_file["state.json"].from_version == 1
    assert by_file["state.json"].to_version == 3
    assert by_file["current.json"].direction is Direction.NONE


def test_report_digest_matches_the_preserved_and_new_forms(tmp_path):
    """Catches digests computed from something other than what is on disk:
    the before digest is the preserved copy's record, the after digest is the
    record now in place."""
    root = _store(tmp_path)
    report = migrate(root, registry=_two_step(), now=NOW, dry_run=False)
    entry = {f.file: f for f in report.files}["state.json"]
    old = json.loads((root / entry.preserved_to).read_text())
    new = json.loads((root / "state.json").read_text())
    assert migrations_module.record_digest(old) == entry.digest_before
    assert migrations_module.record_digest(new) == entry.digest_after


def test_naive_now_is_refused(tmp_path):
    """Catches a preserved-copy name whose UTC is a guess: a timezone-naive
    `now` is refused. Control: the aware NOW works."""
    root = _store(tmp_path)
    with pytest.raises(MigrationError):
        migrate(root, registry=_two_step(), now=datetime(2026, 9, 24))
    migrate(root, registry=_two_step(), now=NOW)


def test_step_that_stamps_a_wrong_version_is_refused(tmp_path):
    """Catches a step that lies about where it leaves the record: setting
    schema_version to 9 in a 1 -> 2 step is refused before anything is
    written."""
    def liar(r):
        r["schema_version"] = 9
        return r

    root = tmp_path / "s"
    _write(root, "state.json", {"schema_version": 1})
    before = _snapshot(root)
    with pytest.raises(MigrationError):
        migrate(root, registry=_registry(_step(1, fn=liar)), now=NOW, dry_run=False)
    assert _snapshot(root) == before
    assert os.listdir(root) == ["state.json"]
