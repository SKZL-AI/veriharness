"""`hoh mission overview` (slice 1 of the rows P3-33, P3-37, P3-38): one read-only look at every run
under a runs root, from what the product already writes.

Every test builds real runs under `tmp_path` through the product's own `RunStore`, holds a real
process lock in the test process for a "live" run, and starts no process: `subprocess.run` and
`subprocess.Popen` fail the test. Every odd file a run directory can hold is made on disk (a FIFO,
a link, a mode-000 file, a directory where a file belongs, bytes that are not UTF-8) and each of
them is looked at through the command, with a guard that turns a hang into a failure.

The exit codes and the bounds are written out as numbers on purpose: they are the contract of the
command, and a table imported from the module would agree with a mutated module.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from hoh import cli
from hoh.contracts import Candidate, Condition, RunState, Stage
from hoh.resumeloop import STORAGE_CLASSES
from hoh.store import RunStore
from hoh.timers import TimerKind, TimerStore, in_seconds

PAST = "2020-01-01T00:00:00Z"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

ROW_KEYS = ["run_id", "readable", "phase", "stage", "condition", "iteration", "blocked_kind",
            "blocked_reason", "retry_after", "stop_reason", "accepted_candidate", "state_phase",
            "process", "process_since", "process_ended", "resume_attempts", "binding", "caveat"]


def mc():
    """The module under test, looked up at the call: a missing module fails the test that needs
    it, not the collection of the file."""
    try:
        from hoh import missioncontrol
    except ImportError:
        pytest.fail("hoh.missioncontrol does not exist")
    return missioncontrol


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a test started a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


class Hang(BaseException):
    """Raised by the alarm of `Guard`. A `BaseException` on purpose: the module's own
    `except OSError` / `except Exception` clauses must not be able to turn a hang into an
    unreadable row that a test accepts (`TimeoutError` is an `OSError`)."""


class Guard:
    """A look that does not return is a failure, not a hung suite."""

    def __init__(self, seconds: int = 10):
        self.seconds = seconds

    def __enter__(self):
        def alarm(signum, frame):
            raise Hang(f"the look did not return within {self.seconds} s: it blocked")

        self.old = signal.signal(signal.SIGALRM, alarm)
        signal.alarm(self.seconds)
        return self

    def __exit__(self, *exc):
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self.old)
        return False


# --------------------------------------------------------------------------- #
# Builders: real runs
# --------------------------------------------------------------------------- #


def _make(root: Path, run_id: str, *, stage=Stage.DEVELOPING, condition=Condition.ACTIVE,
          kind: str | None = None, reason: str | None = None, retry: str | None = None,
          stop: str | None = None, candidate: bool = False, timer: bool = True) -> RunStore:
    state = RunState(run_id=run_id, repo_path=str(root / "repo"), project_name="demo",
                     spec_path=str(root / "spec.md"), spec_digest="s", policy_digest="p",
                     profile_digest="h", stage=stage, condition=condition, iteration=1)
    if kind is not None:
        state.blocked_kind = kind
    if reason is not None:
        state.blocked_reason = reason
    if retry is not None:
        state.retry_after = retry
    if stop is not None:
        state.stop_reason = stop
    if candidate:
        state.last_accepted_candidate = Candidate(
            candidate_id="cand-1", repo_path=str(root / "repo"), commit="c0ffee",
            tree_clean=True, tree_digest="t")
    store = RunStore(root, run_id)
    store.write_state(state)
    if timer and condition is Condition.BLOCKED and kind in STORAGE_CLASSES:
        TimerStore(root).arm_new(kind=TimerKind.STORAGE_RECHECK, due_at=in_seconds(3600),
                                 reason="test", run_id=run_id)
    return store


def _ended(store: RunStore) -> None:
    with store.process_marker():
        pass


def _died(store: RunStore) -> None:
    store.ensure()
    os.close(os.open(str(store.process_lock_path), os.O_CREAT | os.O_RDWR, 0o644))
    store.process_path.write_text(json.dumps(
        {"pid": 4242, "since": "2026-10-01T00-00-00Z", "state": "running"}) + "\n", encoding="utf-8")


def _ask(capsys, root: Path, *argv: str):
    """`hoh --root ROOT mission overview ARGV`: (exit code, stdout, stderr)."""
    try:
        code = cli.main(["--root", str(root), "mission", "overview", *argv])
    except SystemExit as exc:                       # argparse's own refusals
        code = exc.code
    captured = capsys.readouterr()
    if code == 2 and "invalid choice: 'mission'" in captured.err:
        pytest.fail("`hoh mission` does not exist: the parser has no such command")
    return code, captured.out, captured.err


def _rows(capsys, root: Path) -> tuple[int, dict]:
    with Guard():
        code, out, err = _ask(capsys, root, "--json")
    assert err == "", err
    return code, json.loads(out)


def _by_id(doc: dict) -> dict[str, dict]:
    return {row["run_id"]: row for row in doc["runs"]}


def _wait_phase(capsys, root: Path, run_id: str) -> dict:
    code = cli.main(["--root", str(root), "wait", run_id, "--timeout", "0"])
    out = capsys.readouterr().out
    assert code in (0, 1, 3, 4, 5, 6, 7, 8, 124), code
    return json.loads(out)


def _tree(root: Path) -> dict[str, tuple]:
    """Every entry under `root`: kind, mode, size, links, mtime and, for a regular file, content."""
    seen = {}
    for base, dirs, files in os.walk(root):
        for name in ["", *dirs, *files]:
            path = os.path.join(base, name) if name else base
            info = os.lstat(path)
            digest = None
            if os.path.isfile(path) and not os.path.islink(path):
                try:
                    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
                except OSError:
                    digest = "unreadable"
            seen[os.path.relpath(path, root)] = (info.st_mode, info.st_size, info.st_nlink,
                                                 info.st_mtime_ns, digest)
    return seen


# --------------------------------------------------------------------------- #
# V1: the phase is the phase `hoh wait` gives
# --------------------------------------------------------------------------- #


def test_every_run_gets_the_phase_and_process_that_hoh_wait_gives_it(tmp_path, capsys):
    """Fifteen real runs, covering every phase of `runwait`, a live one and a finishing one included. The
    phase is a literal here, and each row is also compared with what `hoh wait RUN --timeout 0`
    answers for the same run. Catches a phase table of its own, a process that is ignored, and a
    storage park read without its timer."""
    from contextlib import ExitStack

    root = tmp_path / "runs"
    expected: dict[str, tuple[str, str]] = {}             # run id -> (phase, process)

    def add(store, phase, process):
        expected[store.run_id] = (phase, process)

    add(_make(root, "acc-cp", stage=Stage.CHECKPOINTED, candidate=True), "accepted", "none")
    add(_make(root, "acc-rfd", stage=Stage.READY_FOR_DELIVERY, candidate=True), "accepted", "none")
    done = _make(root, "acc-ended", stage=Stage.CHECKPOINTED, candidate=True)
    _ended(done)
    add(done, "accepted", "ended")
    add(_make(root, "failed", condition=Condition.FAILED), "failed", "none")
    add(_make(root, "cancelled", condition=Condition.CANCELLED), "cancelled", "none")
    add(_make(root, "blocked", condition=Condition.BLOCKED, reason="a review finding is open"),
        "blocked", "none")
    add(_make(root, "contract", condition=Condition.BLOCKED, kind="parked_by_contract",
              reason="parked by the contract", retry=PAST), "held", "none")
    add(_make(root, "usage", condition=Condition.BLOCKED, kind="usage_limit",
              reason="usage limit reached", retry=PAST), "parked", "none")
    add(_make(root, "storage", condition=Condition.BLOCKED, kind="storage_pressure",
              reason="the disk is full", retry=PAST), "parked", "none")
    add(_make(root, "storage-no-timer", condition=Condition.BLOCKED, kind="storage_pressure",
              reason="the disk is full", retry=PAST, timer=False), "held", "none")
    add(_make(root, "paused", condition=Condition.PAUSED), "paused", "none")
    idle = _make(root, "idle")
    _ended(idle)
    add(idle, "idle", "ended")
    dead = _make(root, "died")
    _died(dead)
    add(dead, "died", "died")
    live = _make(root, "live")
    finishing = _make(root, "finishing", stage=Stage.CHECKPOINTED, candidate=True)
    add(live, "running", "live")
    add(finishing, "finishing", "live")
    assert len(expected) == 15

    with ExitStack() as held:
        held.enter_context(live.process_marker())          # a process that executes the run
        held.enter_context(finishing.process_marker())
        code, doc = _rows(capsys, root)
        waits = {rid: _wait_phase(capsys, root, rid) for rid in expected}
    assert code == 0
    rows = _by_id(doc)
    assert sorted(rows) == sorted(expected)
    for run_id, (phase, process) in expected.items():
        row = rows[run_id]
        assert row["readable"] is True, run_id
        assert (row["phase"], row["process"]) == (phase, process), run_id
        assert (waits[run_id]["phase"], waits[run_id]["process"]) == (phase, process), run_id
        assert row["stage"] == waits[run_id]["stage"] and row["condition"] == waits[run_id]["condition"]
    assert rows["storage-no-timer"]["caveat"] == "parked_without_timer"


def test_a_probe_of_a_live_run_leaves_its_lock_and_marker_as_they_were(tmp_path, capsys):
    root = tmp_path / "runs"
    live = _make(root, "live")
    with live.process_marker():
        marker = live.process_path.read_bytes()
        code, doc = _rows(capsys, root)
        assert _by_id(doc)["live"]["process"] == "live"
        assert live.process_path.read_bytes() == marker
    assert code == 0
    with live.process_lock():                              # the probe did not keep the lock
        pass


# --------------------------------------------------------------------------- #
# V2: the shape of the answer
# --------------------------------------------------------------------------- #


def test_the_answer_has_exactly_the_documented_keys(tmp_path, capsys):
    root = tmp_path / "runs"
    _make(root, "a-run", condition=Condition.BLOCKED, reason="stuck")
    (root / "b-bad").mkdir()
    (root / "b-bad" / "state.json").write_text("{", encoding="utf-8")
    code, doc = _rows(capsys, root)
    assert code == 1
    assert list(doc) == ["schema", "view", "root", "totals", "runs"]
    assert doc["schema"] == "hoh-mission/1" and doc["view"] == "overview"
    assert doc["root"] == str(root)
    assert list(doc["totals"]) == ["runs", "readable", "unreadable", "by_phase"]
    assert doc["totals"] == {"runs": 2, "readable": 1, "unreadable": 1, "by_phase": {"blocked": 1}}
    good, bad = doc["runs"]
    assert list(good) == ROW_KEYS
    assert good["phase"] == "blocked" and good["blocked_reason"] == "stuck"
    assert list(bad) == ["run_id", "readable", "error"] and bad["readable"] is False
    assert "phase" not in bad


def test_the_rows_are_sorted_by_name_and_the_totals_add_up(tmp_path, capsys):
    root = tmp_path / "runs"
    for name in ("zeta", "alpha", "mid"):
        _make(root, name)
    _make(root, "beta", condition=Condition.FAILED)
    code, doc = _rows(capsys, root)
    assert code == 0
    assert [r["run_id"] for r in doc["runs"]] == ["alpha", "beta", "mid", "zeta"]
    assert doc["totals"] == {"runs": 4, "readable": 4, "unreadable": 0,
                             "by_phase": {"failed": 1, "running": 3}}
    code, out, err = _ask(capsys, root)
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[0].startswith("mission overview  root ")
    assert lines[1] == "runs: 4 (4 readable, 0 unreadable)  failed=1  running=3"
    assert [line.split()[0] for line in lines[2:]] == ["alpha", "beta", "mid", "zeta"]
    assert lines[2].split()[1] == "running" and lines[3].split()[1] == "failed"


# --------------------------------------------------------------------------- #
# V3: it writes nothing
# --------------------------------------------------------------------------- #


def test_the_overview_writes_nothing_not_even_for_a_state_that_needs_a_migration(tmp_path, capsys):
    """`RunStore.read_state` parks (hard-links) and so rewrites a state that carries a field the
    product renamed away; the view parses in memory. Every entry of the root, with its mode,
    size, link count, mtime and content, is the same after the text view and the JSON view."""
    root = tmp_path / "runs"
    _make(root, "plain")
    legacy = _make(root, "legacy")
    record = json.loads(legacy.state_path.read_text(encoding="utf-8"))
    record["started_monotonic"] = 12345.0                  # a field `MIGRATIONS` drops
    legacy.state_path.write_text(json.dumps(record), encoding="utf-8")
    live = _make(root, "live")
    _ended(live)
    (root / "norun").mkdir()
    before = _tree(root)
    with Guard():
        assert _ask(capsys, root)[0] == 0
        code, doc = _rows(capsys, root)
        assert mc().overview(str(root))["totals"]["runs"] == 3
    assert code == 0
    assert _by_id(doc)["legacy"]["readable"] is True
    assert _tree(root) == before
    assert not list(root.glob("*/state.json.v*"))


#: The one name of each store module the project views (P3-35, P3-36) may import: the project
#: store's constructor, for its id rule and its path, and the routing-log reader. Any other
#: spelling of those modules -- `from . import projectstore`, `from .projectstore import unblock`,
#: `import hoh.governance`, an alias -- stays forbidden below, with the writers they reach.
ALLOWED_FROM = {"projectstore": ["ProjectStore"], "governance": ["read_branch_stops"]}
#: The writers of the project and governance stores, by name: imported, called or attached.
STORE_WRITERS = {"unblock", "abandon", "record_external_action", "record_override",
                 "block_under_contract", "bind", "decide", "create", "_park"}


def test_the_module_calls_nothing_that_writes():
    """The module's own source: no call of a write, a lock, a marker, a state read that parks, a
    store operation, a process or a Herdr call -- by attribute (`x.unlink()`), by a qualified
    `os` call (`os.replace`), by a name imported from `os` or `pathlib` (`from os import unlink`),
    nor by `getattr`, `__import__`, `eval`, `exec` or the builtin `open`. The one call named
    `write` is the answer on the stream it was given."""
    tree = ast.parse(Path(mc().__file__).read_text(encoding="utf-8"))
    attrs, names, qualified, imported, from_os = set(), set(), set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                attrs.add(func.attr)
                if isinstance(func.value, ast.Name):
                    qualified.add(f"{func.value.id}.{func.attr}")
            elif isinstance(func, ast.Name):
                names.add(func.id)
        if isinstance(node, ast.ImportFrom):
            if (node.level == 1 and node.module in ALLOWED_FROM and not any(
                    a.asname for a in node.names)
                    and [a.name for a in node.names] == ALLOWED_FROM[node.module]):
                continue                                  # the one import of that store module
            imported.update((node.module or "").split("."))
            if (node.module or "") in ("os", "pathlib", "shutil", "io"):
                from_os.update(a.name for a in node.names)
            imported.update(a.name for a in node.names)
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.update(a.name.split("."))
    by_attribute = {"read_state", "write_state", "lock", "process_lock", "process_marker", "ensure",
                    "write_evidence", "write_text", "write_bytes", "mkdir", "makedirs", "unlink",
                    "rename", "touch", "symlink", "truncate", "request_stop", "clear_stop_request",
                    "durable_write", "durable_create", "Popen", "system", "chmod", "utime", "fsync",
                    "write"} | STORE_WRITERS
    # `out.write` of the answer is the one write the module makes: it is on a stream it was given.
    assert (attrs & by_attribute) <= {"write"}, sorted(attrs & by_attribute)
    os_calls = {"os.replace", "os.rename", "os.link", "os.unlink", "os.remove", "os.truncate",
                "os.mkdir", "os.makedirs", "os.write", "os.chmod", "os.utime", "os.fsync",
                "os.symlink", "os.system", "os.rmdir", "shutil.copy", "shutil.move"}
    assert not qualified & os_calls, sorted(qualified & os_calls)
    assert not from_os & {"replace", "rename", "link", "unlink", "remove", "truncate", "mkdir",
                          "makedirs", "write", "chmod", "utime", "fsync", "symlink", "system",
                          "rmdir", "open"}, sorted(from_os)
    assert not names & ({"getattr", "__import__", "eval", "exec", "compile", "open", "setattr"}
                        | STORE_WRITERS), sorted(names)
    assert not imported & ({"subprocess", "shutil", "tempfile", "durable_io", "controller",
                            "launcher", "herdr", "dispatchers", "stages", "timers", "projectstore",
                            "governance"} | STORE_WRITERS), imported


# --------------------------------------------------------------------------- #
# V4: the root
# --------------------------------------------------------------------------- #


def test_a_root_that_cannot_be_listed_is_refused_and_nothing_is_created(tmp_path, capsys):
    absent = tmp_path / "nowhere" / "runs"
    code, out, err = _ask(capsys, absent)
    assert code == 2 and out == "" and "mission refused" in err
    assert not (tmp_path / "nowhere").exists()
    a_file = tmp_path / "file"
    a_file.write_text("x", encoding="utf-8")
    code, out, err = _ask(capsys, a_file)
    assert code == 2 and out == "" and "mission refused" in err
    if not IS_ROOT:
        closed = tmp_path / "closed"
        closed.mkdir()
        closed.chmod(0)
        try:
            code, out, err = _ask(capsys, closed)
        finally:
            closed.chmod(0o755)
        assert code == 2 and out == "" and "mission refused" in err


def test_an_empty_root_is_an_answer_not_an_error(tmp_path, capsys):
    root = tmp_path / "runs"
    root.mkdir()
    code, doc = _rows(capsys, root)
    assert code == 0 and doc["runs"] == []
    assert doc["totals"] == {"runs": 0, "readable": 0, "unreadable": 0, "by_phase": {}}
    code, out, err = _ask(capsys, root)
    assert (code, err) == (0, "") and out.splitlines()[1] == "runs: 0 (0 readable, 0 unreadable)"


def test_only_runs_are_listed_and_a_link_is_listed_but_never_followed(tmp_path, capsys):
    """The names the root keeps for itself and a directory without a `state.json` entry are not
    runs; a link, a dangling `state.json` and a plain file are each handled, never followed."""
    root = tmp_path / "runs"
    good = _make(root, "a-run")
    for reserved in ("_arenas", ".hidden", "backups", "projects"):
        (root / reserved).mkdir()
        (root / reserved / "state.json").write_text(good.state_path.read_text(encoding="utf-8"),
                                                    encoding="utf-8")
    (root / "norun").mkdir()
    (root / "norun" / "notes.txt").write_text("x", encoding="utf-8")
    (root / "timers.json").write_text("{}", encoding="utf-8")
    os.symlink(root / "a-run", root / "alias")
    (root / "dangling").mkdir()
    os.symlink(root / "nothing.json", root / "dangling" / "state.json")
    code, doc = _rows(capsys, root)
    assert code == 1
    rows = _by_id(doc)
    assert sorted(rows) == ["a-run", "alias", "dangling"]
    assert rows["a-run"]["readable"] is True
    assert rows["alias"]["readable"] is False and "symbolic link" in rows["alias"]["error"]
    assert rows["dangling"]["readable"] is False and "symbolic link" in rows["dangling"]["error"]


@pytest.mark.skipif(IS_ROOT, reason="a directory mode does not stop root")
def test_an_unsearchable_run_directory_and_a_listing_only_root_are_rows_not_omissions(
        tmp_path, capsys):
    """`lexists` is False on `EACCES`, so a run directory without search permission read as "no
    `state.json`" and vanished; a root that may only be listed lost every run the same way."""
    root = tmp_path / "runs"
    _make(root, "good")
    hidden = _make(root, "hidden")
    hidden.dir.chmod(0o600)
    try:
        code, doc = _rows(capsys, root)
    finally:
        hidden.dir.chmod(0o755)
    rows = _by_id(doc)
    assert code == 1 and sorted(rows) == ["good", "hidden"]
    assert rows["good"]["readable"] is True
    assert rows["hidden"]["readable"] is False and "phase" not in rows["hidden"]
    assert "state.json cannot be examined" in rows["hidden"]["error"]
    assert doc["totals"] == {"runs": 2, "readable": 1, "unreadable": 1, "by_phase": {"running": 1}}
    root.chmod(0o444)                                          # listed, not searched
    try:
        code, doc = _rows(capsys, root)
    finally:
        root.chmod(0o755)
    assert code == 1 and sorted(_by_id(doc)) == ["good", "hidden"]
    assert doc["totals"] == {"runs": 2, "readable": 0, "unreadable": 2, "by_phase": {}}
    assert all(r["readable"] is False and "cannot be examined" in r["error"] for r in doc["runs"])


def test_the_root_is_resolved_the_way_the_kernel_resolves_it(tmp_path, capsys):
    """`link/..` is the parent of the link's target for every other command; a lexical
    normalisation would list the directory that holds the link instead."""
    deep = tmp_path / "real" / "deep"
    deep.mkdir(parents=True)
    _make(tmp_path / "real", "run-kernel")
    _make(tmp_path, "run-lexical")
    os.symlink(deep, tmp_path / "ln")
    code, doc = _rows(capsys, tmp_path / "ln" / "..")
    assert code == 0 and list(_by_id(doc)) == ["run-kernel"]
    assert doc["root"] == str(tmp_path / "real")


# --------------------------------------------------------------------------- #
# V5: nothing odd is read as a phase (fail closed), nothing hangs, nothing is left out
# --------------------------------------------------------------------------- #


def _state_text(root: Path) -> str:
    return _make(root.parent / "donor", "donor").state_path.read_text(encoding="utf-8")


def _set_state(run_dir: Path, how: str, valid: str) -> None:
    state = run_dir / "state.json"
    if how.startswith("bytes:"):
        state.write_bytes(bytes.fromhex(how[6:]))
    elif how == "empty":
        state.write_bytes(b"")
    elif how == "not-an-object":
        state.write_text("[1, 2]", encoding="utf-8")
    elif how == "null":
        state.write_text("null", encoding="utf-8")
    elif how == "truncated":
        state.write_text(valid[: len(valid) // 2], encoding="utf-8")
    elif how == "missing-fields":
        state.write_text('{"run_id": "bad"}', encoding="utf-8")
    elif how == "nul":
        state.write_bytes(valid.encode("utf-8")[:40] + b"\x00" * 8 + valid.encode("utf-8")[40:])
    elif how == "utf16":
        state.write_bytes(valid.encode("utf-16"))
    elif how == "latin1":
        state.write_bytes(valid.replace('"demo"', '"d' + chr(0xE9) + 'mo"').encode("latin-1"))
    elif how == "surrogate":
        state.write_text(valid.replace('"demo"', '"\\ud800"'), encoding="utf-8")
    elif how == "directory":
        state.unlink()
        state.mkdir()
    elif how == "dangling-link":
        state.unlink()
        os.symlink(run_dir / "nothing", state)
    elif how == "link-to-a-valid-state":
        valid_copy = run_dir / "elsewhere.json"
        valid_copy.write_text(valid, encoding="utf-8")
        state.unlink()
        os.symlink(valid_copy, state)
    elif how == "fifo":
        state.unlink()
        os.mkfifo(state)
    elif how == "no-access":
        state.chmod(0)
    elif how == "process-json-corrupt":
        (run_dir / "process.json").write_text("{", encoding="utf-8")
    elif how == "process-json-array":
        (run_dir / "process.json").write_text("[]", encoding="utf-8")
    elif how == "process-json-binary":
        (run_dir / "process.json").write_bytes(b"\xff\xfe\x00{")
    elif how == "process-json-link":
        target = run_dir / "marker.json"
        target.write_text('{"state": "ended"}', encoding="utf-8")
        os.symlink(target, run_dir / "process.json")
    elif how == "process-json-no-access":
        (run_dir / "process.json").write_text('{"state": "ended"}', encoding="utf-8")
        (run_dir / "process.json").chmod(0)
    elif how == "process-json-directory":
        (run_dir / "process.json").mkdir()
    elif how == "lock-fifo":
        os.mkfifo(run_dir / ".process.lock")
    elif how == "lock-no-access":
        lock = run_dir / ".process.lock"
        lock.write_bytes(b"")
        lock.chmod(0)
    elif how == "lock-link":
        target = run_dir / "lock-target"
        target.write_bytes(b"")
        os.symlink(target, run_dir / ".process.lock")
    elif how == "lock-directory":
        (run_dir / ".process.lock").mkdir()
    else:  # pragma: no cover
        raise AssertionError(how)


ODD = ["empty", "not-an-object", "null", "truncated", "missing-fields", "nul", "utf16", "latin1",
       "bytes:efbbbf7b7d", "bytes:ff", "surrogate", "directory", "dangling-link", "link-to-a-valid-state",
       "fifo", "no-access", "process-json-corrupt", "process-json-array", "process-json-binary",
       "process-json-link", "process-json-no-access", "process-json-directory", "lock-fifo",
       "lock-no-access", "lock-link", "lock-directory"]
NEEDS_USER = {"no-access", "process-json-no-access", "lock-no-access"}


@pytest.mark.parametrize("how", ODD)
def test_a_run_that_cannot_be_looked_at_is_a_row_that_says_why_and_the_rest_is_unharmed(
        tmp_path, capsys, how):
    if how in NEEDS_USER and IS_ROOT:
        pytest.skip("mode 000 does not stop root")
    root = tmp_path / "runs"
    _make(root, "good")
    bad = _make(root, "bad", stage=Stage.CHECKPOINTED, candidate=True)
    valid = bad.state_path.read_text(encoding="utf-8")
    _set_state(bad.dir, how, valid)
    before = _tree(root)
    try:
        code, doc = _rows(capsys, root)
        after = _tree(root)
    finally:
        for name in ("state.json", "process.json", ".process.lock"):
            path = bad.dir / name
            if path.exists() and not path.is_symlink() and not path.is_dir():
                path.chmod(0o644)
    assert code == 1, how
    rows = _by_id(doc)
    assert sorted(rows) == ["bad", "good"], how                # never left out
    assert rows["good"]["readable"] is True and rows["good"]["phase"] == "running"
    assert rows["bad"]["readable"] is False, how
    assert rows["bad"]["error"] and "phase" not in rows["bad"], how
    assert doc["totals"] == {"runs": 2, "readable": 1, "unreadable": 1, "by_phase": {"running": 1}}
    assert after == before, how                                # nothing written


def test_a_legitimate_marker_next_to_a_good_state_is_not_refused(tmp_path, capsys):
    """The checks are not a wall: a run that ended in order (marker and lock, both regular files)
    and a run with a marker the product wrote are rows with a phase."""
    root = tmp_path / "runs"
    ended = _make(root, "ended")
    _ended(ended)
    parked = _make(root, "parked-copy")
    os.link(parked.state_path, parked.dir / "state.json.v1.2026-10-01T00-00-00Z")  # as `_park` leaves it
    code, doc = _rows(capsys, root)
    assert code == 0 and _by_id(doc)["ended"]["phase"] == "idle"
    assert _by_id(doc)["ended"]["process"] == "ended"
    assert _by_id(doc)["parked-copy"]["phase"] == "running"


def test_a_hang_inside_a_look_is_a_failure_and_never_an_unreadable_row(tmp_path, monkeypatch):
    """The alarm raises a `BaseException`: a read that blocks (here the pre-flight's open without
    `O_NONBLOCK`, on a FIFO) must come out of the module as the alarm, not as a row that the
    module's own `except OSError` / `except Exception` made of it."""
    assert not issubclass(Hang, Exception) and not issubclass(Hang, OSError)
    root = tmp_path / "runs"
    fifo = _make(root, "fifo")
    os.unlink(fifo.state_path)
    os.mkfifo(fifo.state_path)

    def blocking(path, limit):
        return os.open(path, os.O_RDONLY)                      # no O_NONBLOCK: waits for a writer

    monkeypatch.setattr(mc(), "_open_regular", blocking)
    with pytest.raises(Hang), Guard(2):
        mc().overview(str(root))


def test_the_state_ceiling_is_eight_mebibytes_exactly(tmp_path, capsys):
    root = tmp_path / "runs"
    at = _make(root, "at-limit")
    over = _make(root, "over-limit")
    for store, size in ((at, 8 * 1024 * 1024), (over, 8 * 1024 * 1024 + 1)):
        text = store.state_path.read_bytes()
        store.state_path.write_bytes(text + b" " * (size - len(text)))
    code, doc = _rows(capsys, root)
    rows = _by_id(doc)
    assert code == 1
    assert rows["at-limit"]["readable"] is True and rows["at-limit"]["phase"] == "running"
    assert rows["over-limit"]["readable"] is False and "larger than" in rows["over-limit"]["error"]


def test_the_marker_ceiling_is_sixty_four_kibibytes_exactly(tmp_path, capsys):
    root = tmp_path / "runs"
    for name, size in (("at-limit", 64 * 1024), ("over-limit", 64 * 1024 + 1)):
        store = _make(root, name)
        _ended(store)
        body = store.process_path.read_bytes().rstrip(b"\n")
        store.process_path.write_bytes(body + b" " * (size - len(body)))
    code, doc = _rows(capsys, root)
    rows = _by_id(doc)
    assert code == 1
    assert rows["at-limit"]["readable"] is True and rows["at-limit"]["process"] == "ended"
    assert rows["over-limit"]["readable"] is False and "larger than" in rows["over-limit"]["error"]


# --------------------------------------------------------------------------- #
# V6: what is printed is plain, short and one line per run
# --------------------------------------------------------------------------- #


def test_names_and_reasons_are_written_as_plain_ascii_one_line_each(tmp_path, capsys):
    """A name that is not UTF-8, one with a space, a newline or an escape sequence, and a reason
    with a terminal escape: every row is one line of ASCII in the text view and a valid ASCII JSON
    document, and the text of a reason is cut at 120 characters (JSON: the 400 `hoh wait` gives)."""
    root = tmp_path / "runs"
    root.mkdir()
    valid = _state_text(root)
    names = [b"bad-\xffname", b"with space", b"line\nbreak", b"esc-\x1b[31m-red", b"caf\xc3\xa9",
             b"trailing\n", b"x" * 200]
    for raw in names:
        run_dir = Path(os.fsdecode(os.fsencode(str(root)) + b"/" + raw))
        run_dir.mkdir()
        (run_dir / "state.json").write_text(valid, encoding="utf-8")
    _make(root, "reasoned", condition=Condition.BLOCKED,
          reason="\x1b[2J" + "x" * 10_000 + chr(0x202E))
    with Guard():
        code, out, err = _ask(capsys, root)
    assert err == "" and code == 1
    out.encode("ascii")                                         # no character an encoder can refuse
    lines = out.splitlines()
    assert len(lines) == 2 + len(names) + 1
    assert "\x1b" not in out
    long_name = next(line for line in lines if line.startswith("xxx"))
    assert len(long_name.split()[0]) == 80 and long_name.split()[0].endswith("...")
    reasoned = next(line for line in lines if line.startswith("reasoned"))
    assert 100 < reasoned.count("x") <= 120
    code, text, err = _ask(capsys, root, "--json")
    text.encode("ascii")
    doc = json.loads(text)
    assert err == ""
    ids = {row["run_id"] for row in doc["runs"]}
    assert os.fsdecode(b"bad-\xffname") in ids and "line\nbreak" in ids and "trailing\n" in ids
    assert len(_by_id(doc)["reasoned"]["blocked_reason"]) == 400


def test_an_error_message_is_cut_at_240_characters(tmp_path, capsys, monkeypatch):
    from hoh import runwait

    root = tmp_path / "runs"
    _make(root, "boom")

    def explode(store):
        raise RuntimeError("y" * 5000)

    monkeypatch.setattr(runwait, "observe_run", explode)
    code, doc = _rows(capsys, root)
    error = _by_id(doc)["boom"]["error"]
    assert code == 1 and error.startswith("RuntimeError: yyy") and error.endswith("...")
    assert len(error) == 240


def test_every_text_bound_is_exact_from_both_sides(tmp_path, capsys, monkeypatch):
    """80 (name), 40 (kind), 120 (reason and error in the text view) and 240 (error in JSON): a
    value at the bound is written whole, one character more is cut to the bound with `...`."""
    from hoh import runwait

    root = tmp_path / "runs"
    for name in ("n" * 80, "m" * 81):
        _make(root, name)
    _make(root, "r120", condition=Condition.BLOCKED, reason="a" * 120)
    _make(root, "r121", condition=Condition.BLOCKED, reason="b" * 121)
    _make(root, "k40", condition=Condition.BLOCKED, kind="k" * 40)
    _make(root, "k41", condition=Condition.BLOCKED, kind="j" * 41)
    messages = {"e120": "x" * 106, "e121": "y" * 107, "e240": "z" * 226, "e241": "w" * 227}
    for name in messages:
        _make(root, name)
    real = runwait.observe_run

    def look(store):
        if store.run_id in messages:
            raise RuntimeError(messages[store.run_id])
        return real(store)

    monkeypatch.setattr(runwait, "observe_run", look)
    code, out, err = _ask(capsys, root)
    assert (code, err) == (1, "")
    lines = {line.split()[0]: line for line in out.splitlines()[2:]}
    assert lines["n" * 80].startswith("n" * 80 + "  ")
    assert lines["m" * 77 + "..."].startswith("m" * 77 + "...  ")       # "m" * 81, cut to 80
    assert lines["r120"].endswith("  " + "a" * 120)
    assert lines["r121"].endswith("  " + "b" * 117 + "...")
    assert lines["k40"].endswith("  " + "k" * 40)
    assert lines["k41"].endswith("  " + "j" * 37 + "...")
    assert lines["e120"].endswith("UNREADABLE  RuntimeError: " + "x" * 106)
    assert lines["e121"].endswith("UNREADABLE  RuntimeError: " + "y" * 103 + "...")
    code, text, err = _ask(capsys, root, "--json")
    rows = _by_id(json.loads(text))
    assert rows["e240"]["error"] == "RuntimeError: " + "z" * 226
    assert rows["e241"]["error"] == "RuntimeError: " + "w" * 223 + "..."


# --------------------------------------------------------------------------- #
# V7: the command
# --------------------------------------------------------------------------- #


def test_the_command_reads_the_root_it_is_given_and_the_view_is_named(tmp_path, capsys, monkeypatch):
    given, default = tmp_path / "given", tmp_path / "default"
    _make(given, "from-given")
    _make(default, "from-default")
    code, doc = _rows(capsys, given)
    assert code == 0 and list(_by_id(doc)) == ["from-given"]
    monkeypatch.setattr(cli, "DEFAULT_ROOT", default)
    assert cli.main(["mission", "overview", "--json"]) == 0
    assert list(_by_id(json.loads(capsys.readouterr().out))) == ["from-default"]
    for argv in (["mission"], ["mission", "frontier"], ["mission", "overview", "extra"]):
        with pytest.raises(SystemExit) as stop:
            cli.main(argv)
        assert stop.value.code == 2, argv
        capsys.readouterr()
    code, out, err = _ask(capsys, given)                       # the text form is not JSON
    assert code == 0 and err == ""
    with pytest.raises(ValueError):
        json.loads(out)
    link = tmp_path / "link-to-given"                          # the root itself may be a link
    os.symlink(given, link)
    code, doc = _rows(capsys, link)
    assert code == 0 and list(_by_id(doc)) == ["from-given"]


def test_an_answer_that_cannot_be_written_is_exit_2(tmp_path, capsys):
    root = tmp_path / "runs"
    _make(root, "a-run")

    class Broken:
        def __init__(self, error):
            self.error = error

        def write(self, text):
            raise self.error

        def flush(self):
            raise self.error

    for error in (OSError("No space left on device"), ValueError("I/O operation on closed file")):
        with open(tmp_path / "err.txt", "w", encoding="utf-8") as err:
            assert mc().run("overview", str(root), out=Broken(error), err=err) == 2
        text = (tmp_path / "err.txt").read_text(encoding="utf-8")
        assert "mission failed" in text and "could not be written" in text

    class FlushFails:                                          # the write is buffered, the flush fails
        def write(self, text):
            return len(text)

        def flush(self):
            raise OSError("No space left on device")

    err = io.StringIO()
    assert mc().run("overview", str(root), out=FlushFails(), err=err) == 2
    assert "could not be written" in err.getvalue()


def test_a_closed_stdout_is_exit_2_not_a_traceback(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    _make(root, "a-run")
    monkeypatch.setattr(sys, "stdout", None)                  # `hoh mission overview >&-`
    err = io.StringIO()
    assert mc().run("overview", str(root), err=err) == 2
    assert "stdout is closed" in err.getvalue()


def test_an_unknown_view_is_exit_2_and_prints_nothing(tmp_path):
    root = tmp_path / "runs"
    _make(root, "a-run")
    out, err = io.StringIO(), io.StringIO()
    assert mc().run("frontier", str(root), out=out, err=err) == 2
    assert out.getvalue() == "" and "unknown view" in err.getvalue()
