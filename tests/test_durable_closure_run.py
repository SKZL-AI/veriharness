"""P2-41 closure S3: the run records of `hoh run`, the integrity close, the
repair specification and the message of `hoh restore` go through the
durable-write primitives.

`cli._cmd_run` writes four records before any role starts (`policy.json`,
`autonomy.json`, `worktree_trust.json`, `integrity-before.json`), the close of a
run writes `integrity.json`, and `HohRunLauncher.prepare` writes the repair
specification. A record that cannot be written stops the command with a
`NEEDS_YOU` message; an integrity record that cannot be written is not shown
to hold, and the earlier one is set aside so that it can never be read as the
current verdict. Every test names what it catches. Fault cases come with a
control that changes only the condition under test.

Fault injection goes through a proxy installed as `durable_io.os` (the seam of
`tests/test_durable_closure.py`, whose small helpers are copied here: test
modules do not import each other). A fault is aimed by the file behind the
descriptor (`/proc/self/fd/<fd>`), so it hits exactly one write of a
multi-file operation. No disk is filled, no subprocess is started besides the
`git` calls that build the repository of a test, `hoh run` is driven only as
`--no-herdr --iterations 0`, and nothing writes outside `tmp_path`.
"""
from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import types
from pathlib import Path

import pytest

from hoh import cli, durable_io, trust
from hoh.autonomy import AutonomyMode, Horizon, compile_profile
from hoh.contracts import Budgets, Candidate, RunState, Stage
from hoh.launcher import HohRunLauncher
from hoh.project import TaskNode
from hoh.store import RunStore

ROOT = Path(__file__).resolve().parents[1]


# --- the seam ----------------------------------------------------------------

class _FaultyOS:
    """Stands in for `os` inside one module only; overrides win."""

    def __init__(self, **overrides):
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(os, name)


def _install(monkeypatch, **overrides) -> None:
    monkeypatch.setattr(durable_io, "os", _FaultyOS(**overrides))


def _fd_name(fd: int) -> str:
    try:
        return os.path.basename(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return ""


def _is_reg(fd: int) -> bool:
    return stat.S_ISREG(os.fstat(fd).st_mode)


def _is_dir(fd: int) -> bool:
    return stat.S_ISDIR(os.fstat(fd).st_mode)


def _err_write_for(prefix: str, err: int = errno.ENOSPC):
    """`os.write` fails with `err` for a regular file whose name starts with
    `prefix`."""
    real = os.write

    def write(fd, data):
        if _is_reg(fd) and _fd_name(fd).startswith(prefix):
            raise OSError(err, os.strerror(err))
        return real(fd, data)
    return write


def _err_write_nth(prefix: str, nth: int, err: int = errno.ENOSPC):
    """`os.write` fails with `err` only for the `nth` distinct temp file whose
    name starts with `prefix`, counted by name from 1."""
    real = os.write
    seen: list[str] = []

    def write(fd, data):
        if _is_reg(fd):
            name = _fd_name(fd)
            if name.startswith(prefix):
                if name not in seen:
                    seen.append(name)
                if seen.index(name) + 1 == nth:
                    raise OSError(err, os.strerror(err))
        return real(fd, data)
    return write


def _dir_fsync_error(dirname: str, err: int = errno.EIO):
    """`os.fsync` of the directory whose `/proc/self/fd` basename is `dirname`
    raises `err`."""
    real = os.fsync

    def fsync(fd):
        if _is_dir(fd) and _fd_name(fd) == dirname:
            raise OSError(err, os.strerror(err))
        return real(fd)
    return fsync


def _file_fsync_error(prefix: str, err: int = errno.EIO):
    """`os.fsync` of a regular file whose name starts with `prefix` raises `err`."""
    real = os.fsync

    def fsync(fd):
        if _is_reg(fd) and _fd_name(fd).startswith(prefix):
            raise OSError(err, os.strerror(err))
        return real(fd)
    return fsync


def _statvfs_reporting(free_bytes: int):
    def fake(_path):
        return types.SimpleNamespace(f_bavail=free_bytes, f_frsize=1, f_bsize=1,
                                     f_blocks=free_bytes, f_bfree=free_bytes)
    return fake


def _litter(directory: Path) -> list[str]:
    """Temp files a durable call would leave: `.<name>.tmp.<pid>.<random>`."""
    return sorted(p.name for p in Path(directory).iterdir() if ".tmp." in p.name)


# --- a run to drive -----------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _run(tmp_path: Path):
    """A real git repository (one commit, a local identity) and a store at
    `tmp_path/runs` holding a CHECKPOINTED `r1` whose last accepted candidate is
    the repository's HEAD: the one state `hoh run` accepts without a role."""
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    head = _git(repo, "rev-parse", "HEAD")
    spec = tmp_path / "spec.md"
    spec.write_text("# spec\n", encoding="utf-8")
    store = RunStore(tmp_path / "runs", "r1")
    store.write_state(RunState(
        run_id="r1", repo_path=str(repo), project_name="p", spec_path=str(spec),
        spec_digest="a" * 16, policy_digest="b" * 16, profile_digest="c" * 16,
        stage=Stage.CHECKPOINTED,
        last_accepted_candidate=Candidate(
            candidate_id="r1-i1", repo_path=str(repo), commit=head,
            tree_clean=True, tree_digest="d" * 16)))
    return store, repo


def _storage_root(tmp_path: Path, monkeypatch) -> Path:
    """`hoh run` refuses to start without an initialised storage state root, and
    on WSL without a host volume (copied from `tests/test_governance.py`)."""
    from hoh import storage_host
    from hoh.storage_registry import StorageRegistry

    root = tmp_path / "storage-state"
    StorageRegistry(root).init()
    if storage_host.detect_host_profile() == storage_host.PROFILE_WSL2:
        volume = tmp_path / "host-volume"
        volume.mkdir()
        (root / "storage-policy.json").write_text(
            json.dumps({"host_paths": [str(volume)]}), encoding="utf-8")
        (root / "storage-host.json").write_text(
            json.dumps({"wsl_host_volume": str(volume)}), encoding="utf-8")
    monkeypatch.setenv("HOH_STATE_ROOT", str(root))
    return root


def _hoh_run(tmp_path: Path, *extra: str) -> int:
    return cli.main(["--root", str(tmp_path / "runs"), "run", "r1", "--no-herdr",
                     "--iterations", "0", *extra])


def _policy_file(tmp_path: Path) -> str:
    target = tmp_path / "policy.json"
    target.write_bytes((ROOT / "policy" / "role_approval.default.json").read_bytes())
    return str(target)


def _contract_file(tmp_path: Path) -> str:
    contract = compile_profile(
        AutonomyMode.UNATTENDED_LONG_HORIZON, contract_id="un", scope=("src/hoh/",),
        horizon=Horizon(max_wallclock_seconds=3600, review_after_seconds=900),
        budgets=Budgets(max_dispatches=10, max_wallclock_seconds=3600))
    target = tmp_path / "contract.json"
    target.write_text(contract.model_dump_json(indent=2), encoding="utf-8")
    return str(target)


def _seed(approval: Path, name: str) -> bytes:
    """An earlier record, whose bytes a failed write must leave alone."""
    approval.mkdir(parents=True, exist_ok=True)
    earlier = b'{"earlier": true}\n'
    (approval / name).write_bytes(earlier)
    return earlier


# ==== hoh run: the four pre-run records ==========================================

def test_run_with_room_writes_its_records(tmp_path, monkeypatch):
    """Control of the full-disk cases: the same command with room writes the
    three records of a run under a policy and a contract."""
    _storage_root(tmp_path, monkeypatch)
    store, _repo = _run(tmp_path)
    rc = _hoh_run(tmp_path, "--approval-policy", _policy_file(tmp_path),
                  "--autonomy", _contract_file(tmp_path))
    assert rc == 0
    for name in ("policy.json", "autonomy.json", "integrity-before.json"):
        json.loads((store.dir / "approval" / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["policy.json", "autonomy.json", "integrity-before.json"])
def test_run_full_disk_on_a_record_stops_the_run_with_the_old_record_intact(
        tmp_path, monkeypatch, capsys, name):
    """Catches: a record written in place (a full disk truncates the earlier
    one) or an unhandled `DiskFullError` as a traceback. For `autonomy.json` the
    fault hits `bind`'s witness first: that case is the `except DiskFullError`
    around `bind`."""
    _storage_root(tmp_path, monkeypatch)
    store, _repo = _run(tmp_path)
    approval = store.dir / "approval"
    earlier = _seed(approval, name)
    extra = {"policy.json": ("--approval-policy", _policy_file(tmp_path)),
             "autonomy.json": ("--autonomy", _contract_file(tmp_path)),
             "integrity-before.json": ()}[name]
    _install(monkeypatch, write=_err_write_for(f".{name}.tmp."))
    assert _hoh_run(tmp_path, *extra) == 2
    assert "NEEDS_YOU" in capsys.readouterr().err
    assert (approval / name).read_bytes() == earlier
    assert _litter(approval) == []


def test_run_full_disk_on_the_second_autonomy_write_stops_the_run(
        tmp_path, monkeypatch, capsys):
    """Catches: a dropped `return rc` after the `autonomy.json` record. The first
    temp file of that name is `bind`'s witness, the second is
    `_write_record`'s."""
    _storage_root(tmp_path, monkeypatch)
    _run(tmp_path)
    _install(monkeypatch, write=_err_write_nth(".autonomy.json.tmp.", 2))
    assert _hoh_run(tmp_path, "--autonomy", _contract_file(tmp_path)) == 2
    err = capsys.readouterr().err
    assert "autonomy.json could not be written" in err
    assert "could not be pinned" not in err


def test_run_full_disk_on_the_worktree_trust_record_stops_the_run(
        tmp_path, monkeypatch, capsys):
    """Catches: the `--trust-worktree` record written in place, or a dropped
    `return rc` after it. The real `trust_run_worktree` writes the user's Claude
    configuration; a stub stands in for it."""
    _storage_root(tmp_path, monkeypatch)
    store, repo = _run(tmp_path)
    monkeypatch.setattr(trust, "trust_run_worktree",
                        lambda _path: {"trusted": str(repo)})
    assert _hoh_run(tmp_path, "--trust-worktree") == 0
    record = store.dir / "approval" / "worktree_trust.json"
    assert json.loads(record.read_text(encoding="utf-8")) == {"trusted": str(repo)}
    capsys.readouterr()

    earlier = _seed(store.dir / "approval", "worktree_trust.json")
    _install(monkeypatch, write=_err_write_for(".worktree_trust.json.tmp."))
    assert _hoh_run(tmp_path, "--trust-worktree") == 2
    assert "NEEDS_YOU" in capsys.readouterr().err
    assert record.read_bytes() == earlier


def test_run_record_that_fails_for_another_reason_also_stops_the_run(
        tmp_path, monkeypatch, capsys):
    """Catches: a handler that knows only `DiskFullError`. An EIO at the fsync of
    the temp file is just as much a record that was not written."""
    _storage_root(tmp_path, monkeypatch)
    store, _repo = _run(tmp_path)
    _install(monkeypatch, fsync=_file_fsync_error(".policy.json.tmp."))
    assert _hoh_run(tmp_path, "--approval-policy", _policy_file(tmp_path)) == 2
    assert "NEEDS_YOU" in capsys.readouterr().err
    assert not (store.dir / "approval" / "policy.json").exists()


def test_run_record_directory_sync_failure_is_a_warning_and_the_run_goes_on(
        tmp_path, monkeypatch, capsys):
    """Catches: a failed directory fsync after the replace that stops the run.
    The new record is in place; only the rename's durability is uncertain."""
    _storage_root(tmp_path, monkeypatch)
    store, _repo = _run(tmp_path)
    (store.dir / "approval").mkdir(parents=True)
    _install(monkeypatch, fsync=_dir_fsync_error("approval"))
    assert _hoh_run(tmp_path, "--approval-policy", _policy_file(tmp_path)) == 0
    assert "may not be durable" in capsys.readouterr().err
    json.loads((store.dir / "approval" / "policy.json").read_text(encoding="utf-8"))


# ==== the integrity close ==========================================================

def _close_store(tmp_path: Path, monkeypatch, problems: list[str]) -> RunStore:
    """A store whose `approval/` exists and whose inventory check returns
    `problems`. `approval/` is created first: without it a call fails at the
    control with `FileNotFoundError`, for the wrong reason."""
    from hoh import integrity

    store = RunStore(tmp_path / "runs", "r1")
    store.ensure()
    (store.dir / "approval").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(integrity, "verify", lambda before: list(problems))
    return store


def test_integrity_close_full_disk_is_not_a_quiet_true(tmp_path, monkeypatch, capsys):
    """Catches: an integrity record that cannot be written and a close that says
    the rule held. Control: the same close with room returns True."""
    store = _close_store(tmp_path, monkeypatch, [])
    assert cli._integrity_close(store, {"inventory": []}) is True
    _install(monkeypatch, write=_err_write_for(".integrity.json.tmp."))
    assert cli._integrity_close(store, {"inventory": []}) is False
    assert "could not be written" in capsys.readouterr().err


@pytest.mark.parametrize("problems", [[], ["deleted: app.py"]], ids=["no problems", "one problem"])
def test_integrity_close_failed_write_sets_the_stale_held_record_aside(
        tmp_path, monkeypatch, problems):
    """Catches (review p241s3spec-a, finding 1): an earlier invocation's
    `{"held": true}` left in place when the new write fails, to be read by the
    acceptance record as the verdict of a run that did not happen. After a failed
    write there is no file named `integrity.json`; the earlier record survives
    under its versioned name."""
    store = _close_store(tmp_path, monkeypatch, [])
    assert cli._integrity_close(store, {"inventory": []}) is True
    approval = store.dir / "approval"
    first = (approval / "integrity.json").read_text(encoding="utf-8")
    assert json.loads(first)["held"] is True

    from hoh import integrity
    monkeypatch.setattr(integrity, "verify", lambda before: list(problems))
    _install(monkeypatch, write=_err_write_for(".integrity.json.tmp."))
    assert cli._integrity_close(store, {"inventory": []}) is False
    assert not (approval / "integrity.json").exists()
    parked = sorted(p for p in approval.iterdir() if p.name.startswith("integrity.json.v"))
    assert len(parked) == 1
    assert parked[0].read_text(encoding="utf-8") == first
    assert _litter(approval) == []


def test_integrity_close_directory_sync_failure_keeps_the_new_record(
        tmp_path, monkeypatch, capsys):
    """Catches: a failed directory fsync that is treated as a failed write, which
    would set aside a record that is in place and is the current one."""
    store = _close_store(tmp_path, monkeypatch, [])
    _install(monkeypatch, fsync=_dir_fsync_error("approval"))
    assert cli._integrity_close(store, {"inventory": []}) is True
    assert "may not be durable" in capsys.readouterr().err
    approval = store.dir / "approval"
    assert json.loads((approval / "integrity.json").read_text(encoding="utf-8"))["held"] is True
    assert [p.name for p in approval.iterdir() if p.name.startswith("integrity.json.v")] == []


def test_integrity_close_still_reports_a_violation_when_its_record_cannot_be_written(
        tmp_path, monkeypatch, capsys):
    """Catches: a close that returns early on the failed write and swallows the
    violation the captain's rule is there to report."""
    store = _close_store(tmp_path, monkeypatch, ["deleted: app.py"])
    _install(monkeypatch, write=_err_write_for(".integrity.json.tmp."))
    assert cli._integrity_close(store, {"inventory": []}) is False
    err = capsys.readouterr().err
    assert "INTEGRITY VIOLATION" in err and "deleted: app.py" in err
    assert not (store.dir / "approval" / "integrity.json").exists()


# ==== the repair specification =====================================================

def _launcher(tmp_path: Path, monkeypatch) -> tuple[HohRunLauncher, list]:
    launcher = HohRunLauncher(tmp_path, tmp_path)
    calls: list = []
    monkeypatch.setattr(launcher, "budget_argumente", list)

    def run_cli(argv):
        calls.append(argv)
        raise RuntimeError("stop")
    monkeypatch.setattr(launcher, "_run_cli", run_cli)
    return launcher, calls


def _repair_node() -> TaskNode:
    return TaskNode(id="n1", repair_of="gate-x", run_id="n1")


def test_repair_specification_full_disk_is_a_reason_and_nothing_else_starts(
        tmp_path, monkeypatch):
    """Catches: a repair specification written in place (or not at all) with the
    dispatch going on, or a full disk as a traceback out of `prepare`."""
    launcher, calls = _launcher(tmp_path, monkeypatch)
    spec_file = tmp_path / "n1" / "repair-n1.md"
    _install(monkeypatch, write=_err_write_for(".repair-n1.md.tmp."))
    reason = launcher.prepare(_repair_node())
    assert isinstance(reason, str) and "repair specification" in reason
    assert not spec_file.exists()
    assert calls == []


def test_repair_specification_is_never_written_over_one_that_appeared_meanwhile(
        tmp_path, monkeypatch):
    """Catches: a write that replaces. A person's specification that appears
    between the `exists()` check and the write is used as it is."""
    launcher, calls = _launcher(tmp_path, monkeypatch)
    spec_file = tmp_path / "n1" / "repair-n1.md"

    def generated(_node):
        spec_file.write_text("edited by a person\n", encoding="utf-8")
        return "generated"

    monkeypatch.setattr(HohRunLauncher, "_repair_spec", staticmethod(generated))
    with pytest.raises(RuntimeError, match="stop"):
        launcher.prepare(_repair_node())
    assert calls, "prepare did not go on to the dispatch"
    assert spec_file.read_text(encoding="utf-8") == "edited by a person\n"


def test_repair_specification_dangling_link_is_a_reason(tmp_path, monkeypatch):
    """Catches (review p241s3spec-a, finding 7): a dangling link at the
    specification's path that `exists()` does not see and `durable_create`
    refuses, answered by carrying on without a specification."""
    launcher, calls = _launcher(tmp_path, monkeypatch)
    spec_file = tmp_path / "n1" / "repair-n1.md"
    spec_file.parent.mkdir()
    spec_file.symlink_to(tmp_path / "nowhere.md")
    reason = launcher.prepare(_repair_node())
    assert isinstance(reason, str) and "dangling" in reason
    assert calls == []


# ==== hoh restore ==================================================================

def test_restore_command_full_disk_is_a_message_and_exit_2(tmp_path, monkeypatch, capsys):
    """Catches: a `DiskFullError` out of `hoh restore` as a traceback. The
    command says what happened and the root is as it was."""
    store, _repo = _run(tmp_path)
    archive = store.backup(tmp_path / "backups")
    before = sorted(str(p.relative_to(store.root)) for p in store.root.rglob("*"))
    _install(monkeypatch, statvfs=_statvfs_reporting(100))
    rc = cli.main(["--root", str(store.root), "restore", "r1", "--archive", str(archive)])
    assert rc == 2
    assert "NEEDS_YOU" in capsys.readouterr().err
    assert sorted(str(p.relative_to(store.root)) for p in store.root.rglob("*")) == before


# ==== P2-41 closure S2: the follow-ups of the S3 integration reviews ===============

def test_run_full_disk_creating_the_approval_directory_stops_the_run(
        tmp_path, monkeypatch, capsys):
    """Catches (p241s3int-a/-b, finding 2): a `mkdir` of `approval/` outside
    `_write_record`'s handler, so a full disk there escapes `cli.main` as a
    traceback instead of `NEEDS_YOU`, exit 2. Control: the same command with the
    real `mkdir` is `test_run_with_room_writes_its_records`."""
    _storage_root(tmp_path, monkeypatch)
    store, _repo = _run(tmp_path)
    real_mkdir = Path.mkdir

    def mkdir(self, *args, **kwargs):
        if self.name == "approval":
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    assert _hoh_run(tmp_path, "--approval-policy", _policy_file(tmp_path)) == 2
    assert "NEEDS_YOU" in capsys.readouterr().err
    assert not (store.dir / "approval").exists()


def test_run_pin_that_fails_for_another_reason_also_stops_the_run(
        tmp_path, monkeypatch, capsys):
    """Catches (p241s3int-a/-b, finding 3): a handler around `bind` that knows
    only `DiskFullError`. An EIO at the fsync of the pin is just as much a
    contract that was not pinned, and was a traceback."""
    _storage_root(tmp_path, monkeypatch)
    store, _repo = _run(tmp_path)
    _install(monkeypatch, fsync=_file_fsync_error(".contract.json.tmp."))
    assert _hoh_run(tmp_path, "--autonomy", _contract_file(tmp_path)) == 2
    assert "could not be pinned" in capsys.readouterr().err
    assert _litter(store.dir / "approval") == []


def test_repair_specification_directory_sync_failure_keeps_the_specification(
        tmp_path, monkeypatch):
    """Catches (p241s3int-a, finding 5): a `DurableWriteNotSynced` from
    `durable_create` answered by "could not write the repair specification". The
    specification is in place and whole; only its directory entry is unsynced,
    so `prepare` goes on to the dispatch (the stub `_run_cli`)."""
    launcher, calls = _launcher(tmp_path, monkeypatch)
    spec_file = tmp_path / "n1" / "repair-n1.md"
    _install(monkeypatch, fsync=_dir_fsync_error("n1"))
    with pytest.raises(RuntimeError, match="stop"):
        launcher.prepare(_repair_node())
    assert len(calls) == 1
    assert spec_file.read_text(encoding="utf-8").startswith("# Repair: n1")


def test_records_follow_the_umask(tmp_path, monkeypatch):
    """Catches (p241s3int-a, finding 4): a record written with a fixed mode
    (`mode=0o644`) instead of `mode=None`, which the old `write_text` did not do.
    Under umask 0o027 the four writers of S3 give 0o640."""
    _storage_root(tmp_path, monkeypatch)
    store, _repo = _run(tmp_path)
    launcher, _calls = _launcher(tmp_path, monkeypatch)
    old = os.umask(0o027)
    try:
        assert _hoh_run(tmp_path, "--approval-policy", _policy_file(tmp_path)) == 0
        _close_store(tmp_path, monkeypatch, [])
        assert cli._integrity_close(store, {"inventory": []}) is True
        with pytest.raises(RuntimeError, match="stop"):
            launcher.prepare(_repair_node())
    finally:
        os.umask(old)
    approval = store.dir / "approval"
    for record in (approval / "policy.json", approval / "integrity-before.json",
                   approval / "integrity.json", tmp_path / "n1" / "repair-n1.md"):
        assert stat.S_IMODE(record.stat().st_mode) == 0o640, record.name


def test_two_stale_records_set_aside_in_one_second_do_not_overwrite_each_other(
        tmp_path, monkeypatch):
    """Catches (p241s3int-a, finding 4): a counter in `_park_stale_record` that
    starts every time at the same name, so the second record set aside within a
    second replaces the first. With the clock frozen, a good close, a failed
    close, a good close and a failed close leave two versioned records."""
    monkeypatch.setattr("hoh.contracts.utcnow", lambda: "2026-10-09T00:00:00Z")
    store = _close_store(tmp_path, monkeypatch, [])
    approval = store.dir / "approval"
    for _round in range(2):
        monkeypatch.setattr(durable_io, "os", os)
        assert cli._integrity_close(store, {"inventory": []}) is True
        _install(monkeypatch, write=_err_write_for(".integrity.json.tmp."))
        assert cli._integrity_close(store, {"inventory": []}) is False
    parked = sorted(p.name for p in approval.iterdir() if p.name.startswith("integrity.json.v"))
    assert len(parked) == 2, parked
