"""The A7 regression obligation, made mechanical (`tools/regression_watch.py`, R2).

Amendment A7 turned "later changes reopen the phase" into "later changes create a
regression obligation: re-run the eight-minute durability run at records and release
checkpoints". Nothing ran it (42 merges, 0 re-runs), so the live reading of P2 was
"unknown", reported with the word for "never proven".

Every case here uses a *fake runner*: no eight-minute run ever starts inside a test. The
repositories are small synthetic ones in temporary directories. One case per rule, each
beside a control: stale -> exactly one run; the result is terminal (PASS, FAIL, GAP);
FAIL writes a request and never reopens by itself; a records-only merge asks for nothing;
the row annotations leave the status alone.
"""
from __future__ import annotations

import errno
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from test_succession_records_window import _git, _write  # noqa: E402
import test_build_plan_closure as closure_tests  # noqa: E402


def _load(name: str, as_name: str):
    spec = importlib.util.spec_from_file_location(as_name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[as_name] = module
    spec.loader.exec_module(module)
    return module


rw = _load("regression_watch", "regression_watch_probe")

GATE = "dogfood/p2-gate"
DURABILITY = rw.DURABILITY_RECORD
WATCHED = ("P2-33", "P2-34", "P2-35")


def _probe(rid: str, key: str) -> dict:
    return {"id": rid, "name": rid, "target_phase": "P2",
            "probe": {"kind": "evidence", "path": DURABILITY, "deriver": "durability_e2e.py",
                      "evidence_dir": "dogfood/p2-durability/x", "key": key}}


def _json(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


class World:
    """E (the evidence commit, durability record naming itself), then `change()` makes
    the evidence stale by editing a relevant path, `records()` commits a records-only
    change."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.scratch = root.parent / "scratch"
        root.mkdir(parents=True)
        _git(root, "init", "-q")
        _write(root, "src/hoh/core.py", "VALUE = 1\n")
        _write(root, "README.md", "x\n")
        _write(root, "program/v3_3/REQUIREMENTS.json", _json({"requirements": [
            *(_probe(r, r) for r in WATCHED), _probe("P2-11", "stressor:DS2"),
            {"id": "P3-01", "name": "x", "target_phase": "P3",
             "probe": {"kind": "symbol"}}]}))
        _write(root, DURABILITY, _json({"evidence_head": "0" * 40}))
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "E")
        self.evidence = _git(root, "rev-parse", "HEAD")
        _write(root, DURABILITY, _json({"evidence_head": self.evidence}))
        _write(root, GATE + "/CLOSURE.json", _json({"closed_at_commit": "d" * 40}))
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "records: durability record names E")
        self.n = 0

    def head(self) -> str:
        return _git(self.root, "rev-parse", "HEAD")

    def _commit(self, rel: str, text: str, message: str) -> str:
        _write(self.root, rel, text)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", message)
        return self.head()

    def change(self) -> str:
        self.n += 1
        return self._commit("src/hoh/core.py", f"VALUE = {self.n + 1}\n", "src change")

    def records(self) -> str:
        self.n += 1
        return self._commit("dogfood/notes.md", f"note {self.n}\n", "records")

    def commit_all(self, message: str = "records: watch result") -> str:
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", message)
        return self.head()

    def watch_files(self) -> list[str]:
        d = self.root / GATE / rw.WATCH_DIR
        return sorted(p.name for p in d.glob("*.json")) if d.is_dir() else []

    def request_files(self) -> list[str]:
        return sorted(p.name for p in (self.root / GATE).glob("REOPEN-REQUEST-*.json"))


@pytest.fixture
def world(tmp_path):
    return World(tmp_path / "repo")


@pytest.fixture(autouse=True)
def ample_space(monkeypatch):
    """Whatever the host has free, a test sees plenty (the pressure nodes say otherwise)."""
    monkeypatch.setattr(rw, "_free", lambda path: (10 ** 9, 10 ** 13))


class Fake:
    """A runner that counts, remembers its timeout and returns what it is given."""

    def __init__(self, world: World, *, satisfied=None, **override) -> None:
        self.world, self.calls, self.timeouts = world, 0, []
        self.satisfied = satisfied or {r: True for r in WATCHED}
        self.override = override

    def __call__(self, repo, commit, scratch, timeout):
        self.calls += 1
        self.timeouts.append(timeout)
        derived = {
            "evidence_head": commit, "instrument_falsifiable": True, "meta_problems": [],
            "requirements": {r: {"satisfied": ok} for r, ok in self.satisfied.items()},
            "stressors": {"DS1": "PASS", "DS2": "NOT_RUN"}}
        meta = {"harness_failures": {}, "dirty": False, "dirty_at_end": False}
        derived.update(self.override.get("derived", {}))
        meta.update(self.override.get("meta", {}))
        for key in self.override.get("drop_derived", ()):
            derived.pop(key, None)
        for key in self.override.get("drop_meta", ()):
            meta.pop(key, None)
        if self.override.get("no_derived"):
            derived = None
        return rw.RunResult(derived=derived, meta=meta, run_rc=0, derive_rc=0,
                            timed_out=self.override.get("timed_out", False))


def _run(world, runner, **kw):
    kw.setdefault("scratch", world.scratch)
    return rw.obligation(world.root, runner=runner, **kw)


# --------------------------------------------------------------------------- #
# deciding
# --------------------------------------------------------------------------- #

def test_no_change_to_the_relevant_paths_asks_for_nothing(world):
    world.records()
    runner = Fake(world)
    outcome, reason, code = _run(world, runner)
    assert (outcome, code) == ("NOT_NEEDED", 0) and runner.calls == 0, reason
    assert world.watch_files() == []


def test_stale_evidence_runs_exactly_once_and_records_a_pass(world):
    commit = world.change()
    runner = Fake(world)
    outcome, reason, code = _run(world, runner)
    assert (outcome, code) == ("PASS", 0) and runner.calls == 1, reason
    [name] = world.watch_files()
    assert name.endswith(f"-{commit[:12]}.json") and rw._STAMP.fullmatch(name.split("-")[0])
    record = json.loads((world.root / GATE / "watch" / name).read_text(encoding="utf-8"))
    assert record["verdict"] == "PASS" and record["commit"] == commit
    assert record["evidence_head_before"] == world.evidence
    assert record["relevant_tree"] == rw.relevant_tree(world.root, commit)
    assert record["requirements"] == {r: True for r in WATCHED}
    assert record["relevant_paths_changed"] == ["src/hoh/core.py"]
    assert world.request_files() == []


def test_the_same_relevant_tree_is_never_run_twice_and_a_records_merge_asks_nothing(world):
    world.change()
    runner = Fake(world)
    assert _run(world, runner)[0] == "PASS"
    world.commit_all()
    world.records()
    world.records()
    outcome, reason, code = _run(world, runner)
    assert outcome == "ALREADY:PASS" and code == 0 and runner.calls == 1, reason
    assert len(world.watch_files()) == 1
    world.change()
    assert _run(world, runner)[0] == "PASS" and runner.calls == 2


def test_rerun_is_the_one_explicit_way_to_ask_again(world, monkeypatch):
    stamps = iter(["20261020T000000Z", "20261020T000001Z", "20261020T000002Z",
                   "20261020T000003Z"])
    monkeypatch.setattr(rw, "_stamp", lambda now=None: next(stamps))
    world.change()
    runner = Fake(world)
    _run(world, runner)
    world.commit_all()
    outcome, _, _ = _run(world, runner, rerun=True)
    assert outcome == "PASS" and runner.calls == 2


def test_a_dry_run_decides_and_writes_nothing(world):
    world.change()
    runner = Fake(world)
    outcome, reason, code = _run(world, runner, dry_run=True)
    assert outcome == "RUN-not-made" and code == 0 and runner.calls == 0, reason
    assert world.watch_files() == []


def test_a_relevant_edit_that_is_not_committed_asks_for_nothing(world):
    """Staleness is a statement about committed history (D2): an uncommitted edit alone
    is NOT_NEEDED, not a reason to run."""
    _write(world.root, "src/hoh/core.py", "VALUE = 99  # uncommitted\n")
    runner = Fake(world)
    outcome, reason, code = _run(world, runner)
    assert (outcome, code) == ("NOT_NEEDED", 0) and runner.calls == 0, reason


def test_an_evidence_commit_git_cannot_relate_is_a_gap_not_a_pass(world):
    world.change()
    _write(world.root, DURABILITY, _json({"evidence_head": "a" * 40}))
    world.commit_all("records: durability record names a commit this history lacks")
    runner = Fake(world)
    outcome, reason, code = _run(world, runner)
    assert (outcome, code) == ("ENVIRONMENT_GAP", 3) and "cannot relate" in reason, reason
    assert runner.calls == 0 and world.watch_files() == []


@pytest.mark.parametrize("text", [None, "[]\n", "{not json", _json({}), _json({"evidence_head": "abc"}),
                                  _json({"evidence_head": 7})], ids=repr)
def test_a_durability_record_without_an_evidence_commit_is_a_gap(world, text):
    world.change()
    if text is None:
        _git(world.root, "rm", "-q", DURABILITY)
    else:
        _write(world.root, DURABILITY, text)
    world.commit_all("records: the durability record is odd")
    runner = Fake(world)
    outcome, reason, code = _run(world, runner)
    assert (outcome, code) == ("ENVIRONMENT_GAP", 3) and "names no evidence commit" in reason, reason
    assert runner.calls == 0 and world.watch_files() == []


def test_the_newest_verdict_for_a_tree_is_the_one_a_later_call_reports(world, monkeypatch):
    stamps = iter(f"20261020T00000{n}Z" for n in range(9))
    monkeypatch.setattr(rw, "_stamp", lambda now=None: next(stamps))
    world.change()
    assert _run(world, Fake(world, timed_out=True))[0] == "ENVIRONMENT_GAP"
    world.commit_all()
    runner = Fake(world)
    assert _run(world, runner, rerun=True)[0] == "PASS"
    world.commit_all()
    outcome, _, code = _run(world, runner)
    assert (outcome, code) == ("ALREADY:PASS", 0) and runner.calls == 1


def test_a_result_that_is_written_but_not_committed_is_already_known(world):
    world.change()
    runner = Fake(world)
    assert _run(world, runner)[0] == "PASS"
    outcome, _, code = _run(world, runner)
    assert (outcome, code) == ("ALREADY:PASS", 0) and runner.calls == 1
    assert len(world.watch_files()) == 1


# --------------------------------------------------------------------------- #
# the result is terminal: PASS, FAIL, ENVIRONMENT_GAP
# --------------------------------------------------------------------------- #

def test_a_failing_watched_requirement_is_a_fail_with_a_request_and_no_reopen(world):
    commit = world.change()
    runner = Fake(world, satisfied={"P2-33": True, "P2-34": False, "P2-35": True})
    outcome, reason, code = _run(world, runner)
    assert (outcome, code) == ("FAIL", 1), reason
    assert "P2-34" in reason
    [request_name] = world.request_files()
    request = json.loads((world.root / GATE / request_name).read_text(encoding="utf-8"))
    assert request["decision"] == "PENDING" and request["kind"] == "reopen-request"
    assert request["commit"] == commit and request["failed_requirements"] == ["P2-34"]
    assert request["closed_at_commit"] == "d" * 40
    assert request["failing_run"].startswith(f"{GATE}/watch/")
    assert "not a decision" in request["note"]
    assert not (world.root / GATE / "reopen").exists(), "the tool never writes a decision"


def test_a_fail_is_terminal_for_its_relevant_tree_and_asks_for_no_second_request(world):
    world.change()
    runner = Fake(world, satisfied={"P2-33": False, "P2-34": True, "P2-35": True})
    assert _run(world, runner)[0] == "FAIL"
    world.commit_all()
    outcome, _, code = _run(world, runner)
    assert (outcome, code) == ("ALREADY:FAIL", 1) and runner.calls == 1
    assert len(world.request_files()) == 1


@pytest.mark.parametrize("name, override, fragment", [
    ("harness-failures", {"meta": {"harness_failures": {"rollover": "boom"}}},
     "harness reported failures"),
    ("harness-failures-unknown", {"meta": {"harness_failures": None}},
     "harness reported failures"),
    ("dirty-absent", {"drop_meta": ["dirty"]}, "not clean"),
    ("dirty-at-end-absent", {"drop_meta": ["dirty_at_end"]}, "not clean"),
    ("harness-failures-absent", {"drop_meta": ["harness_failures"]}, "harness reported failures"),
    ("not-falsifiable-absent", {"drop_derived": ["instrument_falsifiable"]}, "not falsifiable"),
    ("dirty", {"meta": {"dirty": True}}, "not clean"),
    ("dirty-at-end", {"meta": {"dirty_at_end": True}}, "not clean"),
    ("timed-out", {"timed_out": True}, "timed out"),
    ("no-derivation", {"no_derived": True}, "no derivable evidence"),
    ("other-commit", {"derived": {"evidence_head": "9" * 40}}, "not "),
    ("not-falsifiable", {"derived": {"instrument_falsifiable": False}}, "not falsifiable"),
    ("meta-problems", {"derived": {"meta_problems": ["x"]}}, "meta problems"),
    ("no-requirements", {"derived": {"requirements": None}}, "no requirements"),
])
def test_everything_that_prevents_a_reading_is_an_environment_gap_never_a_pass(
        world, name, override, fragment):
    world.change()
    runner = Fake(world, **override)
    outcome, reason, code = _run(world, runner)
    assert (outcome, code) == ("ENVIRONMENT_GAP", 3), (name, reason)
    assert fragment in reason, (name, reason)
    assert len(world.watch_files()) == 1 and world.request_files() == [], name
    record = json.loads((world.root / GATE / "watch" / world.watch_files()[0])
                        .read_text(encoding="utf-8"))
    assert record["verdict"] == "ENVIRONMENT_GAP"


def test_a_runner_that_raises_is_a_gap_not_a_traceback(world):
    world.change()

    def boom(repo, commit, scratch, timeout):
        raise OSError("no scratch")
    outcome, reason, code = _run(world, boom)
    assert (outcome, code) == ("ENVIRONMENT_GAP", 3) and "OSError" in reason, reason


def test_the_reboot_evidence_is_not_judged_at_a_records_checkpoint(world):
    """P2-11 reads `stressor:DS2`; a watch run does not take it, so its unsatisfied state
    is neither a FAIL nor in the watched set."""
    assert rw.watched_requirements(world.root, world.head()) == list(WATCHED)
    world.change()
    runner = Fake(world, derived={"requirements": {
        "P2-33": {"satisfied": True}, "P2-34": {"satisfied": True},
        "P2-35": {"satisfied": True}, "P2-11": {"satisfied": False}}})
    assert _run(world, runner)[0] == "PASS"


def test_a_missing_requirement_in_the_derivation_is_not_satisfied(world):
    world.change()
    runner = Fake(world, derived={"requirements": {"P2-33": {"satisfied": True},
                                                   "P2-34": {"satisfied": True}}})
    outcome, reason, _ = _run(world, runner)
    assert outcome == "FAIL" and "P2-35" in reason, reason


# --------------------------------------------------------------------------- #
# refusals before any run: a tree that is not clean, no repository, a bad bound
# --------------------------------------------------------------------------- #

def test_a_dirty_relevant_path_is_a_gap_without_a_record_and_without_a_run(world):
    world.change()
    _write(world.root, "src/hoh/core.py", "VALUE = 99  # uncommitted\n")
    runner = Fake(world)
    outcome, reason, code = _run(world, runner)
    assert (outcome, code) == ("ENVIRONMENT_GAP", 3) and runner.calls == 0, reason
    assert world.watch_files() == [], "a dirty tree today is not a terminal result"
    assert "uncommitted" in reason


def test_a_directory_that_is_not_a_repository_of_its_own_is_a_gap(tmp_path, world):
    inner = world.root / "inner"
    inner.mkdir()
    outcome, reason, code = rw.obligation(inner, runner=Fake(world))
    assert (outcome, code) == ("ENVIRONMENT_GAP", 3), reason
    plain = tmp_path / "plain"
    plain.mkdir()
    assert rw.obligation(plain, runner=Fake(world))[2] == 3


@pytest.mark.parametrize("timeout", [0, -1, rw.MAX_TIMEOUT + 1, 10 ** 9, True, "60", None],
                         ids=repr)
def test_a_timeout_outside_its_bound_is_refused_before_anything_else(world, timeout):
    world.change()
    runner = Fake(world)
    outcome, reason, code = rw.obligation(world.root, runner=runner, timeout=timeout,
                                          scratch=world.scratch)
    assert (outcome, code) == ("REFUSED", 2) and "timeout must be" in reason, reason
    assert runner.calls == 0 and world.watch_files() == []


def test_the_timeout_given_is_the_timeout_the_runner_gets(world):
    world.change()
    runner = Fake(world)
    _run(world, runner, timeout=60)
    assert runner.timeouts == [60]
    assert rw.DEFAULT_TIMEOUT == 900 and rw.MAX_TIMEOUT == 3600


def test_a_record_is_never_overwritten_before_a_run(world, monkeypatch):
    world.change()
    monkeypatch.setattr(rw, "_stamp", lambda now=None: "20261020T000000Z")
    runner = Fake(world)
    assert _run(world, runner)[0] == "PASS"
    path = world.root / GATE / "watch" / world.watch_files()[0]
    before = path.read_bytes()
    outcome, reason, code = _run(world, runner, rerun=True)
    assert (outcome, code) == ("REFUSED", 2) and "never overwritten" in reason, reason
    assert path.read_bytes() == before and runner.calls == 1


def test_a_record_that_appears_during_the_run_is_never_overwritten(world, monkeypatch):
    world.change()
    monkeypatch.setattr(rw, "_stamp", lambda now=None: "20261020T000000Z")
    fake = Fake(world)
    target = world.root / GATE / "watch" / f"20261020T000000Z-{world.head()[:12]}.json"

    def racing(repo, commit, scratch, timeout):
        result = fake(repo, commit, scratch, timeout)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("someone else's record\n", encoding="utf-8")
        return result
    outcome, reason, code = _run(world, racing)
    assert (outcome, code) == ("REFUSED", 2) and "never overwritten" in reason, reason
    assert target.read_text(encoding="utf-8") == "someone else's record\n"


def test_the_command_line_forwards_the_flags_and_returns_the_exit_code(world, monkeypatch, capsys):
    world.change()
    seen = {}

    def fake_runner(repo, commit, scratch, timeout):
        seen["timeout"] = timeout
        return Fake(world, satisfied={"P2-33": False, "P2-34": True, "P2-35": True})(
            repo, commit, scratch, timeout)
    monkeypatch.setattr(rw, "default_runner", fake_runner)
    code = rw.main(["obligation", "--repo", str(world.root), "--timeout", "60",
                    "--scratch", str(world.scratch)])
    assert code == 1 and seen["timeout"] == 60
    assert capsys.readouterr().out.startswith("FAIL: P2-33")
    world.commit_all()
    assert rw.main(["obligation", "--repo", str(world.root), "--dry-run"]) == 1
    assert "ALREADY:FAIL" in capsys.readouterr().out


def test_the_tool_writes_nothing_but_its_marker_its_record_and_its_request(world):
    world.change()
    before = sorted(p.relative_to(world.root).as_posix() for p in world.root.rglob("*")
                    if ".git" not in p.relative_to(world.root).parts and p.is_file())
    _run(world, Fake(world, satisfied={"P2-33": False, "P2-34": True, "P2-35": True}))
    after = sorted(p.relative_to(world.root).as_posix() for p in world.root.rglob("*")
                   if ".git" not in p.relative_to(world.root).parts and p.is_file())
    new = sorted(set(after) - set(before))
    assert set(before) <= set(after)
    assert len(new) == 3, new
    assert new[0].startswith(f"{GATE}/REOPEN-REQUEST-"), new
    assert new[1].startswith(f"{GATE}/watch-attempts/"), new
    assert new[2].startswith(f"{GATE}/watch/"), new
    assert [p for p in world.scratch.iterdir()] == [], "the run's scratch is gone"


def test_the_newest_records_are_the_ones_read_past_the_cap(world):
    """257 committed records: the oldest drops out, the newest (a FAIL for the current
    tree) decides, and a call asks for nothing. Reading the oldest 256 would show an
    old PASS and run again at every new tree."""
    commit = world.change()
    tree = rw.relevant_tree(world.root, commit)
    for i in range(rw.MAX_RECORDS + 1):
        newest = i == rw.MAX_RECORDS
        _write(world.root, f"{GATE}/watch/202610{i // 24 + 1:02d}T{i % 24:02d}0000Z-{commit[:12]}.json",
               _json({"record_version": 1, "verdict": "FAIL" if newest else "PASS",
                      "commit": commit, "relevant_tree": tree if newest else "0" * 64}))
    world.commit_all("records: 257 watch records")
    records = rw.watch_records(world.root, world.head())
    assert len(records) == rw.MAX_RECORDS == 256
    assert records[-1]["verdict"] == "FAIL" and records[0]["file"].endswith("T010000Z-" + commit[:12] + ".json")
    assert rw.last_watch(world.root, world.head())["verdict"] == "FAIL"
    runner = Fake(world)
    outcome, _, code = _run(world, runner)
    assert (outcome, code) == ("ALREADY:FAIL", 1) and runner.calls == 0


# --------------------------------------------------------------------------- #
# termination: the attempt is counted before it starts; a write that fails is terminal
# and visible; terminal states are quiet
# --------------------------------------------------------------------------- #

def _gate_bytes(world) -> dict[str, bytes]:
    return {p.relative_to(world.root).as_posix(): p.read_bytes()
            for p in (world.root / GATE).rglob("*") if p.is_file()}


def test_the_attempt_is_counted_before_the_run_so_a_second_invocation_does_not_run(world):
    world.change()
    seen = {}
    fake = Fake(world)

    def reentrant(repo, commit, scratch, timeout):
        seen["marker_before_run"] = sorted(
            p.name for p in (world.root / GATE / "watch-attempts").glob("*.json"))
        seen["watch_before_run"] = world.watch_files()
        seen["inner"] = rw.obligation(world.root, runner=fake, scratch=world.scratch)
        return fake(repo, commit, scratch, timeout)
    outcome, _, _ = _run(world, reentrant)
    assert outcome == "PASS" and fake.calls == 1, "the inner invocation must not run"
    assert seen["watch_before_run"] == [] and len(seen["marker_before_run"]) == 1
    marker = seen["marker_before_run"][0]
    assert marker.endswith(f"-{rw.relevant_tree(world.root, world.head())[:16]}.json")
    assert seen["inner"][0] == "ALREADY:ATTEMPTED" and seen["inner"][2] == 3, seen["inner"]
    body = json.loads((world.root / GATE / "watch-attempts" / marker).read_text(encoding="utf-8"))
    assert body["commit"] == world.head() and body["timeout_s"] == rw.DEFAULT_TIMEOUT


def test_a_killed_attempt_is_counted_visible_and_not_run_again(world, monkeypatch):
    stamps = iter(f"20261020T00000{n}Z" for n in range(9))
    monkeypatch.setattr(rw, "_stamp", lambda now=None: next(stamps))
    world.change()

    def killed(repo, commit, scratch, timeout):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        _run(world, killed)
    assert world.watch_files() == [] and world.request_files() == []
    runner = Fake(world)
    outcome, reason, code = _run(world, runner)
    assert (outcome, code) == ("ALREADY:ATTEMPTED", 3) and runner.calls == 0, reason
    assert "no result record" in reason
    first = _gate_bytes(world)
    world.commit_all("records: the killed attempt")
    outcome, _, code = _run(world, runner)
    assert (outcome, code) == ("ALREADY:ATTEMPTED", 3) and runner.calls == 0
    assert _gate_bytes(world) == first, "a terminal state is quiet: no byte changes"
    assert _run(world, runner, rerun=True)[0] == "PASS" and runner.calls == 1
    assert _run(world, runner)[0] == "ALREADY:PASS", "a result newer than the marker ends it"


def test_a_terminal_state_is_quiet_a_further_call_changes_nothing(world):
    world.change()
    runner = Fake(world, satisfied={"P2-33": False, "P2-34": True, "P2-35": True})
    assert _run(world, runner)[0] == "FAIL"
    world.commit_all()
    before = _gate_bytes(world)
    for _ in range(3):
        outcome, _, code = _run(world, runner)
        assert (outcome, code) == ("ALREADY:FAIL", 1)
    assert runner.calls == 1 and _gate_bytes(world) == before


@pytest.mark.parametrize("failing", ["REOPEN-REQUEST", "/p2-gate/watch-attempts/", "/p2-gate/watch/"])
def test_a_write_that_fails_is_a_distinct_visible_exit_and_the_result_is_printed(
        world, monkeypatch, capsys, failing):
    world.change()
    real = rw._write_new

    def full_disk(path, text):
        if failing in path.as_posix():
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, text)
    monkeypatch.setattr(rw, "_write_new", full_disk)
    fake = Fake(world, satisfied={"P2-33": False, "P2-34": True, "P2-35": True})
    monkeypatch.setattr(rw, "default_runner", fake)
    code = rw.main(["obligation", "--repo", str(world.root), "--scratch", str(world.scratch)])
    out = capsys.readouterr().out
    assert code == rw.EXIT_WRITE_FAILED == 4 and out.startswith("WRITE_FAILED:"), out
    assert "No space left on device" in out
    if failing == "/p2-gate/watch-attempts/":
        assert fake.calls == 0, "no counter, no run"
        assert "nothing was run" in out
    else:
        assert fake.calls == 1
        assert "the result is NOT saved: FAIL" in out
        saved = json.loads(out[out.index("\n{") + 1:])
        assert saved["verdict"] == "FAIL" and saved["requirements"]["P2-33"] is False
    if failing == "/p2-gate/watch/":
        assert world.request_files() != [], "the request exists: the gate shows the regression"
    monkeypatch.setattr(rw, "_write_new", real)
    outcome, _, code = _run(world, Fake(world))
    if failing == "/p2-gate/watch-attempts/":
        assert outcome == "PASS"
    else:
        assert (outcome, code) == ("ALREADY:ATTEMPTED", 3), "the counted attempt is not run again"


def test_an_unusable_scratch_is_a_write_failure_and_counts_nothing(world, tmp_path):
    world.change()
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x", encoding="utf-8")
    runner = Fake(world)
    outcome, reason, code = rw.obligation(world.root, runner=runner, scratch=blocker / "sub")
    assert (outcome, code) == ("WRITE_FAILED", 4) and runner.calls == 0, reason
    assert not (world.root / GATE / "watch-attempts").exists()


def test_a_run_needs_a_scratch_directory_and_never_defaults_to_tmp(world):
    world.change()
    runner = Fake(world)
    outcome, reason, code = rw.obligation(world.root, runner=runner)
    assert (outcome, code) == ("REFUSED", 2) and "--scratch" in reason and "/tmp" in reason
    assert runner.calls == 0 and not (world.root / GATE / "watch-attempts").exists()
    assert rw.obligation(world.root, runner=runner, dry_run=True)[0] == "RUN-not-made"
    assert rw.obligation(world.root, runner=Fake(world), scratch=None)[2] == 2


def test_a_scratch_that_is_nearly_full_is_a_gap_and_nothing_is_run(world, monkeypatch):
    world.change()
    assert rw.MIN_FREE_INODES == 2_000_000 and rw.MIN_FREE_BYTES == 60 * 1024 ** 3
    runner = Fake(world)
    for free, fragment in (((rw.MIN_FREE_INODES - 1, 10 ** 13), "free inodes"),
                           ((10 ** 9, rw.MIN_FREE_BYTES - 1), "GiB free")):
        monkeypatch.setattr(rw, "_free", lambda path, free=free: free)
        outcome, reason, code = _run(world, runner)
        assert (outcome, code) == ("ENVIRONMENT_GAP", 3) and fragment in reason, reason
    assert runner.calls == 0 and world.watch_files() == []
    assert not (world.root / GATE / "watch-attempts").exists(), "today's pressure is not counted"


def test_a_failure_under_resource_pressure_is_a_gap_not_a_request(world, monkeypatch):
    world.change()
    readings = iter([(10 ** 9, 10 ** 13), (rw.MIN_FREE_INODES - 1, 10 ** 13)])
    monkeypatch.setattr(rw, "_free", lambda path: next(readings))
    runner = Fake(world, satisfied={"P2-33": False, "P2-34": True, "P2-35": True})
    outcome, reason, code = _run(world, runner)
    assert (outcome, code) == ("ENVIRONMENT_GAP", 3), reason
    assert "resource pressure" in reason and "P2-33" in reason
    assert world.request_files() == []
    record = json.loads((world.root / GATE / "watch" / world.watch_files()[0])
                        .read_text(encoding="utf-8"))
    assert record["scratch_free"]["inodes_after"] == rw.MIN_FREE_INODES - 1


def test_the_default_runner_kills_what_the_instrument_started_even_in_a_session_of_its_own(
        world, monkeypatch, tmp_path):
    """The instrument starts its masters with start_new_session: outside the group. A
    same-group grandchild, a new-session grandchild and an orphan of the group (its parent
    gone, so no parent link leads to it) must all be gone after the timeout."""
    _with_stub(world)
    pids = tmp_path / "pids.txt"
    monkeypatch.setenv("WATCH_STUB_SLEEP", "30")
    monkeypatch.setenv("WATCH_STUB_SPAWN", str(pids))
    try:
        outcome, reason, code = rw.obligation(world.root, timeout=2, scratch=world.scratch)
        assert (outcome, code) == ("ENVIRONMENT_GAP", 3), reason
        spawned = [int(x) for x in pids.read_text(encoding="utf-8").split()]
        assert len(spawned) == 3, "same group, own session, and a reparented orphan of the group"

        def alive(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            with open(f"/proc/{pid}/stat", "rb") as fh:
                return fh.read().rsplit(b")", 1)[1].split()[0] != b"Z"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(alive(p) for p in spawned):
            time.sleep(0.1)
        assert [alive(p) for p in spawned] == [False, False, False], spawned
    finally:
        if pids.exists():
            for text in pids.read_text(encoding="utf-8").split():
                try:
                    os.kill(int(text), 9)       # processes this test started
                except (ProcessLookupError, ValueError):
                    pass


def test_the_timeout_bounds_run_and_derive_together(world, monkeypatch):
    _with_stub(world)
    monkeypatch.setenv("WATCH_STUB_SLEEP", "1")
    monkeypatch.setenv("WATCH_STUB_DERIVE_SLEEP", "30")
    started = time.monotonic()
    outcome, reason, code = rw.obligation(world.root, timeout=3, scratch=world.scratch)
    assert time.monotonic() - started < 20, "run + derive share one 3 second budget"
    assert (outcome, code) == ("ENVIRONMENT_GAP", 3) and "derive exceeded" in reason, reason


# --------------------------------------------------------------------------- #
# the default runner: argv wiring and the timeout, against a stub of the instrument
# --------------------------------------------------------------------------- #

STUB = """\
import json, os, subprocess, sys, time
args = sys.argv[1:]
if os.environ.get("WATCH_STUB_LOG"):
    with open(os.environ["WATCH_STUB_LOG"], "a") as fh:
        fh.write(json.dumps(args) + "\\n")
opt = lambda name: args[args.index(name) + 1]
if args[0] == "run":
    if os.environ.get("WATCH_STUB_SPAWN"):
        kids = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(40)"],
                                 start_new_session=new) for new in (False, True)]
        orphan = ("import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', "
                  "'import time; time.sleep(40)']); open(sys.argv[1], 'w').write(str(p.pid))")
        subprocess.run([sys.executable, "-c", orphan, os.environ["WATCH_STUB_SPAWN"] + ".orphan"])
        with open(os.environ["WATCH_STUB_SPAWN"], "w") as fh:
            fh.write(" ".join([str(k.pid) for k in kids]
                              + [open(os.environ["WATCH_STUB_SPAWN"] + ".orphan").read()]))
    time.sleep(float(os.environ.get("WATCH_STUB_SLEEP", "0")))
    os.makedirs(opt("--evidence"), exist_ok=True)
    with open(os.path.join(opt("--evidence"), "meta.json"), "w") as fh:
        json.dump({"harness_failures": {}, "dirty": False, "dirty_at_end": False}, fh)
elif args[0] == "derive":
    time.sleep(float(os.environ.get("WATCH_STUB_DERIVE_SLEEP", "0")))
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    with open(opt("--out"), "w") as fh:
        json.dump({"evidence_head": head, "instrument_falsifiable": True, "meta_problems": [],
                   "requirements": {r: {"satisfied": True} for r in %r}, "stressors": {}}, fh)
"""


def _with_stub(world: World) -> None:
    _write(world.root, "tools/durability_e2e.py", STUB % (list(WATCHED),))
    world.commit_all("the instrument (a stub for the wiring tests)")


def test_the_default_runner_runs_then_derives_with_the_evidence_and_scratch_paths(
        world, monkeypatch, tmp_path):
    _with_stub(world)
    log = tmp_path / "argv.log"
    monkeypatch.setenv("WATCH_STUB_LOG", str(log))
    outcome, reason, code = rw.obligation(world.root, scratch=world.scratch)
    assert (outcome, code) == ("PASS", 0), reason
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [c[0] for c in calls] == ["run", "derive"], calls
    assert calls[0][1] == "--evidence" and calls[0][3] == "--base", calls
    assert calls[1][1] == "--evidence" and calls[1][2] == calls[0][2], calls
    assert calls[1][3] == "--out", calls
    record = json.loads((world.root / GATE / "watch" / world.watch_files()[0])
                        .read_text(encoding="utf-8"))
    assert record["requirements"] == {r: True for r in WATCHED}
    assert "derived_sha256" in record


def test_the_default_runner_timeout_is_the_one_asked_for(world, monkeypatch):
    _with_stub(world)
    monkeypatch.setenv("WATCH_STUB_SLEEP", "30")
    started = time.monotonic()
    outcome, reason, code = rw.obligation(world.root, timeout=2, scratch=world.scratch)
    assert time.monotonic() - started < 20, "a 2 second timeout must not wait 30 seconds"
    assert (outcome, code) == ("ENVIRONMENT_GAP", 3) and "exceeded 2 s" in reason, reason
    assert len(world.watch_files()) == 1


# --------------------------------------------------------------------------- #
# the gate prints what the watch says (build_plan, over a verified closure)
# --------------------------------------------------------------------------- #

@pytest.fixture
def closed(tmp_path, monkeypatch):
    repo = closure_tests.Repo(tmp_path / "repo", monkeypatch)
    repo.commit_record()
    return repo


def _line(repo) -> str:
    closure, why = closure_tests.bp.closure_record("P2")
    assert closure is not None, why
    watch = {"rows_not_proven": {}, "instruments": {}}
    return closure_tests.bp._closed_line(closure, watch)


def test_a_closure_with_unchanged_relevant_paths_says_nothing_of_a_watch(closed):
    assert "stale" not in _line(closed) and "last re-run" not in _line(closed)


def test_stale_evidence_is_named_with_its_first_commit_and_a_pending_re_run(closed):
    first = closed.commit("src/hoh/core.py", "v2\n", "src change")
    closed.commit("src/hoh/core.py", "v3\n", "another src change")
    line = _line(closed)
    assert f"evidence stale since {first[:12]}, re-run pending" in line, line


def test_a_passing_watch_for_this_tree_is_shown_and_clears_the_pending_re_run(closed):
    closed.commit("src/hoh/core.py", "v2\n", "src change")
    head = closed.commit("README.md", "records\n", "records")
    tree = rw.relevant_tree(closed.root, head)
    closed.commit(f"{GATE}/watch/20261020T120000Z-{head[:12]}.json", _json({
        "record_version": 1, "verdict": "PASS", "commit": head, "relevant_tree": tree}),
        "watch result")
    line = _line(closed)
    assert f"last re-run 20261020T120000Z PASS at {head[:12]}" in line, line
    assert "re-run pending" not in line, line


def test_a_watch_of_an_older_tree_leaves_the_re_run_pending(closed):
    older = closed.commit("src/hoh/core.py", "v2\n", "src change")
    closed.commit(f"{GATE}/watch/20261020T120000Z-{older[:12]}.json", _json({
        "record_version": 1, "verdict": "PASS", "commit": older,
        "relevant_tree": rw.relevant_tree(closed.root, older)}), "watch result")
    closed.commit("src/hoh/core.py", "v3\n", "newer src change")
    line = _line(closed)
    assert f"last re-run 20261020T120000Z PASS at {older[:12]}" in line, line
    assert "re-run pending" in line, line


def test_the_newest_watch_record_is_the_one_shown(closed):
    older = closed.commit("src/hoh/core.py", "v2\n", "src change")
    head = closed.commit("src/hoh/core.py", "v3\n", "newer src change")
    for stamp, verdict, commit in (("20261020T100000Z", "PASS", older),
                                   ("20261020T110000Z", "ENVIRONMENT_GAP", head)):
        closed.commit(f"{GATE}/watch/{stamp}-{commit[:12]}.json", _json({
            "record_version": 1, "verdict": verdict, "commit": commit,
            "relevant_tree": rw.relevant_tree(closed.root, commit)}), "watch result")
    line = _line(closed)
    assert f"last re-run 20261020T110000Z ENVIRONMENT_GAP at {head[:12]}" in line, line
    assert "re-run pending" not in line, "the newest record is for the newest relevant tree"
    closed.commit("src/hoh/core.py", "v4\n", "newer still")
    assert "stale since" in _line(closed) and "re-run pending" in _line(closed)


def _request(repo, commit, failed=("P2-33",)):
    body = {"record_version": 1, "kind": "reopen-request", "phase": "P2",
            "decision": "PENDING", "closed_at_commit": repo.x, "commit": commit,
            "failed_requirements": list(failed), "failing_run": "w.json"}
    name = f"REOPEN-REQUEST-20261020T130000Z-{commit[:12]}.json"
    repo.commit(f"{GATE}/{name}", _json(body), "reopen request")
    return name


def test_an_unanswered_request_is_printed_and_does_not_reopen(closed):
    head = closed.commit("src/hoh/core.py", "v2\n", "src change")
    name = _request(closed, head)
    closure, why = closure_tests.bp.closure_record("P2")
    assert closure is not None and why == "", "a request is not a decision"
    line = _line(closed)
    assert f"REGRESSION at {head[:12]}: P2-33 FAILED, reopen decision pending (A7 rule 3; {name})" in line, line


def test_a_request_for_another_closure_is_not_printed(closed):
    head = closed.commit("src/hoh/core.py", "v2\n", "src change")
    body = {"record_version": 1, "phase": "P2", "closed_at_commit": "e" * 40,
            "commit": head, "failed_requirements": ["P2-33"]}
    closed.commit(f"{GATE}/REOPEN-REQUEST-20261020T130000Z-{head[:12]}.json", _json(body), "x")
    assert "REGRESSION" not in _line(closed)


@pytest.mark.parametrize("commit_spelling", ["twelve", "seven", "upper"])
def test_a_decision_answers_a_request_however_it_spells_the_closing_commit(
        closed, commit_spelling):
    head = closed.commit("src/hoh/core.py", "v2\n", "src change")
    name = _request(closed, head)
    spelled = {"twelve": closed.x[:12], "seven": closed.x[:7],
               "upper": closed.x.upper()}[commit_spelling]
    closed.commit(f"{GATE}/reopen/20261021T000000Z.json", _json({
        "phase": "p2", "closed_at_commit": spelled, "decision": "KEEP_CLOSED",
        "request": name, "entry": "O290", "reason": "a fixture race, fixed"}), "answer")
    assert "REGRESSION" not in _line(closed)


def test_a_decision_for_another_closure_answers_nothing(closed):
    head = closed.commit("src/hoh/core.py", "v2\n", "src change")
    name = _request(closed, head)
    other = ("0" if closed.x[0] != "0" else "1") + closed.x[1:]
    closed.commit(f"{GATE}/reopen/20261021T000000Z.json", _json({
        "phase": "P2", "closed_at_commit": other, "decision": "KEEP_CLOSED",
        "request": name, "entry": "O290", "reason": "another closure"}), "answer")
    assert "REGRESSION" in _line(closed)


def test_a_keep_closed_decision_answers_the_request_and_the_closure_stands(closed):
    head = closed.commit("src/hoh/core.py", "v2\n", "src change")
    name = _request(closed, head)
    closed.commit(f"{GATE}/reopen/20261021T000000Z.json", _json({
        "phase": "P2", "closed_at_commit": closed.x, "decision": "KEEP_CLOSED",
        "request": name, "entry": "O290", "reason": "a fixture race, fixed"}), "answer")
    closure, why = closure_tests.bp.closure_record("P2")
    assert closure is not None, why
    assert "REGRESSION" not in _line(closed)


def test_a_reopen_decision_ends_the_closure_and_the_gate_reads_as_before(closed):
    head = closed.commit("src/hoh/core.py", "v2\n", "src change")
    name = _request(closed, head)
    closed.commit(f"{GATE}/reopen/20261021T000000Z.json", _json({
        "phase": "P2", "closed_at_commit": closed.x, "decision": "REOPEN",
        "request": name, "entry": "O290", "reason": "P2-33 regressed"}), "reopen")
    closure, why = closure_tests.bp.closure_record("P2")
    assert closure is None and why.startswith("reopened by "), why


def test_the_plan_carries_the_watch_state(closed):
    head = closed.commit("src/hoh/core.py", "v2\n", "src change")
    _request(closed, head)
    closure, _ = closure_tests.bp.closure_record("P2")
    plan = closure_tests.bp.build(
        {"capabilities": []}, ["P1", "P2"], {"P2": (True, "CLOSED")},
        closures={"P2": closure}, watch={"P2": {}})
    state = plan["phase_gates"]["P2"]["watch"]["regression_watch"]
    assert state["stale"] is True and state["stale_since"] == head
    assert [r["commit"] for r in state["requests"]] == [head]
    json.dumps(plan)


def test_a_request_survives_a_records_commit_that_removes_its_file(closed):
    """A request is answered by a decision, never by removing it or by a newer record."""
    head = closed.commit("src/hoh/core.py", "v2\n", "src change")
    name = _request(closed, head)
    closed.commit(f"{GATE}/watch/20261020T140000Z-{head[:12]}.json", _json({
        "record_version": 1, "verdict": "ENVIRONMENT_GAP", "commit": head,
        "relevant_tree": rw.relevant_tree(closed.root, head)}), "a newer watch record")
    _git(closed.root, "rm", "-q", f"{GATE}/{name}")
    closed.commit("README.md", "tidy\n", "records: remove the request file")
    assert not (closed.root / GATE / name).exists()
    line = _line(closed)
    assert f"REGRESSION at {head[:12]}: P2-33 FAILED, reopen decision pending" in line, line
    assert [r["file"] for r in rw.open_requests(closed.root, "HEAD", closed.x)] == [name]


def test_only_a_reopen_or_keep_closed_decision_answers_a_request(closed):
    head = closed.commit("src/hoh/core.py", "v2\n", "src change")
    name = _request(closed, head)
    for n, extra in enumerate(({"decision": "PENDING"}, {"decision": "keep_closed"}, {})):
        closed.commit(f"{GATE}/reopen/2026102{n}T000000Z.json", _json({
            "phase": "P2", "closed_at_commit": closed.x, "request": name, **extra}), "not an answer")
        assert [r["file"] for r in rw.open_requests(closed.root, "HEAD", closed.x)] == [name], extra
    # a RESOLVED record (slice 1's way to end a reopen) is not an answer to a request
    closed.commit(f"{GATE}/reopen/20261029T000000Z.json", _json({
        "phase": "P2", "closed_at_commit": closed.x, "decision": "RESOLVED",
        "resolves": "nothing.json", "request": name}), "resolves nothing")
    assert [r["file"] for r in rw.open_requests(closed.root, "HEAD", closed.x)] == [name]


def test_a_request_with_odd_fields_is_printed_and_never_raises(closed):
    head = closed.commit("src/hoh/core.py", "v2\n", "src change")
    body = {"phase": "P2", "closed_at_commit": closed.x, "commit": 7,
            "failed_requirements": ["P2-33", 5, None, {"a": 1}]}
    closed.commit(f"{GATE}/REOPEN-REQUEST-20261020T130000Z-{head[:12]}.json", _json(body), "odd")
    closed.commit(f"{GATE}/REOPEN-REQUEST-20261020T130001Z-{head[:12]}.json", _json({
        **body, "failed_requirements": "P2-34"}), "odd too")
    assert "REGRESSION at 7: P2-33 FAILED" in _line(closed)


def test_an_unreadable_watch_is_said_not_left_silent(closed, monkeypatch):
    closed.commit("src/hoh/core.py", "v2\n", "src change")

    class Broken:
        PHASE = "P2"

        @staticmethod
        def changed_paths(*args):
            raise RuntimeError("git went away")
    monkeypatch.setattr(closure_tests.bp, "_regression_watch", lambda: Broken)
    line = _line(closed)
    assert "regression watch unreadable (RuntimeError: git went away)" in line, line


def test_open_requests_refuses_to_answer_when_git_cannot_say(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RuntimeError, match="cannot list the reopen requests"):
        rw.open_requests(plain, "HEAD", "d" * 40)


def test_the_reboot_evidence_alone_is_left_out_of_the_watched_set(world):
    register = json.loads((world.root / "program/v3_3/REQUIREMENTS.json").read_text(encoding="utf-8"))
    register["requirements"] += [_probe("P2-12", "stressor:DS3"), _probe("P2-13", "stressor:DS2")]
    _write(world.root, "program/v3_3/REQUIREMENTS.json", _json(register))
    world.commit_all("register: a second stressor row")
    assert rw.watched_requirements(world.root, world.head()) == sorted([*WATCHED, "P2-12"])


# --------------------------------------------------------------------------- #
# the matrix rows carry annotations and keep their status
# --------------------------------------------------------------------------- #

cm = _load("capability_matrix", "capability_matrix_watch_probe")


def test_a_durability_row_is_annotated_and_its_status_is_not_touched(world, monkeypatch):
    monkeypatch.setattr(cm, "HOH", world.root)
    monkeypatch.setattr(cm, "_inside", lambda p: (world.root / p) if p else None)
    probe = _probe("P2-33", "P2-33")["probe"]
    assert cm.evidence_watch(probe) == {"evidence_commit": world.evidence,
                                        "stale_since": None, "last_watch": None}
    first = world.change()
    world.change()
    assert cm.evidence_watch(probe)["stale_since"] == first
    head = world.records()
    _write(world.root, f"{GATE}/watch/20261020T120000Z-{head[:12]}.json", _json({
        "record_version": 1, "verdict": "PASS", "commit": head,
        "relevant_tree": rw.relevant_tree(world.root, head)}))
    world.commit_all("watch")
    got = cm.evidence_watch(probe)
    assert got["last_watch"] == {"stamp": "20261020T120000Z", "verdict": "PASS",
                                 "commit": head}
    assert got["stale_since"] == first and got["evidence_commit"] == world.evidence


def test_a_row_of_another_kind_gets_no_annotation(world, monkeypatch):
    monkeypatch.setattr(cm, "HOH", world.root)
    assert cm.evidence_watch({"kind": "evidence", "deriver": "acceptance_record.py"}) == {}
    assert cm.evidence_watch({"kind": "symbol"}) == {}


def test_an_unreadable_record_leaves_every_annotation_none(world, monkeypatch):
    monkeypatch.setattr(cm, "HOH", world.root)
    monkeypatch.setattr(cm, "_inside", lambda p: (world.root / "missing.json"))
    probe = _probe("P2-33", "P2-33")["probe"]
    assert cm.evidence_watch(probe) == {"evidence_commit": None, "stale_since": None,
                                        "last_watch": None}


def test_classify_adds_the_annotations_and_keeps_the_status(world, monkeypatch):
    monkeypatch.setattr(cm, "HOH", world.root)
    monkeypatch.setattr(cm, "_inside", lambda p: (world.root / p) if p else None)
    monkeypatch.setattr(cm, "evidence_status", lambda probe, requirement=None: (
        cm.IMPLEMENTED_NOT_PROVEN, "record x: STALE (code changed since: src/hoh/core.py)"))
    world.change()
    row = cm.classify(_probe("P2-33", "P2-33"), {}, None, {}, None)
    assert row["status"] == cm.IMPLEMENTED_NOT_PROVEN
    assert set(("evidence_commit", "stale_since", "last_watch")) <= set(row)
    assert row["evidence_commit"] == world.evidence
    assert row["stale_since"] == world.head()


def test_the_reboot_row_is_annotated_as_not_re_taken_and_never_shows_a_pass(world, monkeypatch):
    monkeypatch.setattr(cm, "HOH", world.root)
    monkeypatch.setattr(cm, "_inside", lambda p: (world.root / p) if p else None)
    probe = _probe("P2-11", "stressor:DS2")["probe"]
    first = world.change()
    head = world.records()
    _write(world.root, f"{GATE}/watch/20261020T120000Z-{head[:12]}.json", _json({
        "record_version": 1, "verdict": "PASS", "commit": head,
        "relevant_tree": rw.relevant_tree(world.root, head)}))
    world.commit_all("watch")
    got = cm.evidence_watch(probe)
    assert got["last_watch"] is None and "not re-taken" in got["watch_note"], got
    assert got["stale_since"] == first and got["evidence_commit"] == world.evidence
    other = cm.evidence_watch(_probe("P2-33", "P2-33")["probe"])
    assert other["last_watch"]["verdict"] == "PASS" and "watch_note" not in other
