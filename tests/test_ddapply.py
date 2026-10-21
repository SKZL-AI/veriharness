"""The application of a verified correction (V3.3 P3-42, slice 3): `hoh.ddapply`,
`hoh double-down apply|revert`, and the double-down on by default in `hoh resume-quota`.

A `repair_spec` proposal may carry a `fix` (a unified diff). The fix is applied to a scratch copy of
the run's candidate and the proposal's criteria are measured again: the block's own failing check
(a criterion with the id AND the command of a check that failed in the run's own receipts) must be
red before and green after, every keep-green criterion (id starting with K) green before and after.
Only then, and only if the run's pinned autonomy contract resolves a scope decision by policy (or a
person approves it), is the run's specification amended (the repair appended inside a fence no
criteria parser reads) and its block lifted, so that `hoh run` plans a new iteration. The candidate,
the receipts and the results of the run are never written.

Most tests drive the module with a scripted runner, a scripted materialisation and scratch
workspaces made in `tmp_path` (no process); the tests that need one ask for `processes_allowed` and
use a real git repository, the real `workspace.materialize`, `git apply` and the real
`runner.run_check` (isolation NONE: the sandbox cannot nest inside the sandbox the product's own
acceptance runs in). The bounds and the vocabulary are written out as literals on purpose: they are
the contract.
"""
from __future__ import annotations

import ast
import hashlib
import io
import itertools
import json
import os
import subprocess
import types
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hoh import cli, ddapply, ddverify, doubledown, governance, stages
from hoh.amendment import text_digest
from hoh.autonomy import (ActionClass, AutonomyContract, AutonomyMode, Escalation, Horizon, NeedsYou,
                          OnLimit, ReleasePolicy)
from hoh.contracts import Budgets, Candidate, Condition, Receipt, RunState
from hoh.project import Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.sandbox import Isolation
from hoh.spec_criteria import declared_criteria, declared_pins
from hoh.store import RunStore

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def processes_allowed():
    """Asking for this fixture is how a test says it runs git or a check for real."""
    return True


@pytest.fixture(autouse=True)
def _nothing_runs(request, monkeypatch):
    if "processes_allowed" in request.fixturenames:
        return

    def forbidden(*args, **kwargs):
        pytest.fail("a test started a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


# --- builders ------------------------------------------------------------------------------ #

RUN_SPEC = "# the run\n\nThe app says hello.\n\n```criteria\nA1 python3 -c pass\n```\n"
FIX = "--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-hello\n+hello fixed\n"
BLOCK_CMD = "grep -q fixed app.txt"
KEEP_CMD = "test -f app.txt"


def _repair_spec(*lines):
    return "# repair\n\n```criteria\n" + "\n".join(lines) + "\n```\n"


SPEC = _repair_spec(f"R1 {BLOCK_CMD}", f"K1 {KEEP_CMD}")


def _candidate(repo: Path, cid="c1", tree="b" * 40) -> Candidate:
    return Candidate(candidate_id=cid, repo_path=str(repo), commit="a" * 40, tree_clean=True,
                     tree_digest=tree)


def _hyp(path, outcome="open", evidence=("saw it",)):
    return {"path_id": path, "statement": "s", "tested": True, "outcome": outcome,
            "evidence": list(evidence)}


def _report(perspective, hyps, proposal=None, unsolvable=None):
    return json.dumps({"perspective": perspective, "hypotheses": hyps, "proposal": proposal,
                       "unsolvable": unsolvable})


def _proposal(spec=SPEC, fix=FIX, summary="apply fix", steps=("do it",)):
    p = {"path_id": "fix", "kind": "repair_spec", "summary": summary, "steps": list(steps)}
    if spec is not None:
        p["spec"] = spec
    if fix is not None:
        p["fix"] = fix
    return p


class Fake:
    """Scripted analysts: both perspectives of round 1 propose `proposal`."""

    def __init__(self, proposal):
        self.proposal = proposal

    @contextmanager
    def factory(self, halt):
        yield self

    def analyse(self, perspective, prompt, *, round_no):
        return _report(perspective, [_hyp("fix", "supported")], proposal=self.proposal)


class Spaces:
    """The scratch workspaces a session hands out, made under `base`, 0700, one per call."""

    def __init__(self, base: Path, refuse: str | None = None):
        self.base, self.refuse = base, refuse
        self.log: list[tuple] = []
        self.live: set[Path] = set()
        self.released: list[Path] = []
        base.mkdir(exist_ok=True)

    @contextmanager
    def session(self, block):
        if self.refuse == "session":
            raise ddverify.VerifyRefused("nowhere to measure")
        yield self.make

    @contextmanager
    def make(self, purpose, name, estimate):
        if self.refuse == purpose:
            raise ddverify.VerifyRefused(f"no {purpose} workspace")
        path = self.base / f"{len(self.log):02d}-{name}"
        os.mkdir(path, 0o700)
        self.log.append((purpose, name, estimate, path))
        self.live.add(path)
        try:
            yield path
        finally:
            self.live.discard(path)
            self.released.append(path)


class Copier:
    """`materialize`: puts the files the test names in the arena."""

    def __init__(self, files=None):
        self.files = files if files is not None else {"app.txt": "hello\n"}
        self.calls: list[tuple[Candidate, Path]] = []

    def __call__(self, candidate, dest):
        self.calls.append((candidate, Path(dest)))
        os.mkdir(dest)
        for name, text in self.files.items():
            (Path(dest) / name).write_text(text)
        return Path(dest)


class Fixer:
    """`fix_applier`: remembers what it was asked and, unless it is told to refuse, marks the arena."""

    def __init__(self, refuse: str | None = None):
        self.refuse, self.calls = refuse, []

    def __call__(self, arena, fix, home):
        self.calls.append((Path(arena), fix, Path(home)))
        if self.refuse:
            raise ddapply.FixRefused(self.refuse)
        (Path(arena) / "FIXED").write_text("yes\n")


class Runner:
    """`check_runner`: `script[check_id]` is `(exit code, output)` or an exception to raise; a
    callable value is called with the arena and returns one of those."""

    def __init__(self, script=None, default=(0, "ok")):
        self.script, self.default = script or {}, default
        self.calls: list[dict] = []

    def __call__(self, check, candidate, *, run_id, iteration, attempt, cwd, timeout,
                 isolation, scratch):
        self.calls.append(dict(check=check, candidate=candidate, run_id=run_id, attempt=attempt,
                               cwd=Path(cwd), isolation=isolation, scratch=Path(scratch),
                               fixed=(Path(cwd) / "FIXED").exists()))
        value = self.script.get(check.check_id, self.default)
        if callable(value):
            value = value(Path(cwd))
        if isinstance(value, BaseException):
            raise value
        code, text = value
        log = f"$ {check.command}\n--- output ---\n{text}"
        receipt = Receipt(
            receipt_id=f"{run_id}-i{iteration}-a{attempt}-{check.check_id}", run_id=run_id,
            iteration=iteration, attempt=attempt, check_id=check.check_id,
            candidate_binding=candidate.binding(), command=check.command, exit_code=code,
            started_at="2026-10-19T12:00:00Z", ended_at="2026-10-19T12:00:01Z",
            stdout_digest=hashlib.sha256(log.encode()).hexdigest(), runner_identity="fake",
            runner_ok=code not in (124, 126, 127))
        return receipt, log


def _after_fix(green=(0, "ok"), red=(1, "red")):
    """A script for the criteria: green once the fix has marked the arena, red before."""
    return lambda arena: green if (arena / "FIXED").exists() else red


_SPACES = itertools.count()


def _spaces(tmp_path, **kw):
    return Spaces(tmp_path.parent / f"{tmp_path.name}-spaces-{next(_SPACES)}", **kw)


def _contract(scope=Escalation.AUTO_POLICY, mode=AutonomyMode.PLAN_AUTONOMOUS, **kw):
    """A hand-written contract; with `scope=AUTO_POLICY` (the default here) `autonomy.route` resolves a
    `scope` decision by policy in every mode -- and the double-down must still not apply anything by it."""
    esc = {ActionClass.INTERNAL.value: Escalation.AUTO_POLICY, "decision": Escalation.SYSTEM_TWO,
           "release": Escalation.PARK_BRANCH_AND_CONTINUE}
    if scope is not None:
        esc["scope"] = scope
    if mode is AutonomyMode.UNATTENDED_LONG_HORIZON:
        kw.setdefault("horizon", Horizon(max_wallclock_seconds=7 * 24 * 3600, review_after_seconds=6 * 3600,
                                         on_limit=OnLimit.PARK))
        kw.setdefault("budgets", Budgets(max_dispatches=200, max_wallclock_seconds=7 * 24 * 3600))
        kw.setdefault("scope", ("x",))
        needs_you = NeedsYou.OFF
    else:
        needs_you = NeedsYou.ON if mode is AutonomyMode.SUPERVISED else NeedsYou.DIGEST_ONLY
    return AutonomyContract(contract_id="dd3", mode=mode, needs_you=needs_you, escalation=esc,
                            release_policy=ReleasePolicy.AUTO_PREPARE_ONLY, **kw)


def _receipt(store, rid, check_id, command, code, *, iteration=1, attempt=1, name=None):
    rec = Receipt(
        receipt_id=f"{rid}-i{iteration}-a{attempt}-{check_id}", run_id=rid, iteration=iteration,
        attempt=attempt, check_id=check_id, candidate_binding="c1", command=command, exit_code=code,
        started_at="2026-10-19T11:00:00Z", ended_at="2026-10-19T11:00:01Z", stdout_digest="d" * 64,
        runner_identity="fake", runner_ok=code not in (124, 126, 127))
    store.write_receipt(name or rec.receipt_id, rec)
    return rec


def _run(tmp_path, rid="h", *, contract=True, receipts=True, repo=None, spec_text=RUN_SPEC,
         **kw) -> RunStore:
    """A BLOCKED run with a specification file, a last accepted candidate, the receipts of one
    failed iteration (R1 red, K1 green) and, unless told otherwise, a pinned contract."""
    repo = repo or tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    spec = tmp_path / f"{rid}-spec.md"
    spec.write_text(spec_text)
    store = RunStore(tmp_path, rid)
    kw.setdefault("blocked_reason", "stuck")
    kw.setdefault("last_accepted_candidate", _candidate(repo))
    store.write_state(RunState(
        run_id=rid, repo_path=str(repo), project_name="p", spec_path=str(spec),
        spec_digest=text_digest(spec_text), policy_digest="b" * 16, profile_digest="c" * 16,
        condition=Condition.BLOCKED, **kw))
    if receipts:
        _receipt(store, rid, "R1", BLOCK_CMD, 1)
        _receipt(store, rid, "K1", KEEP_CMD, 0)
    if contract:
        governance.bind(store.dir, contract=_contract() if contract is True else contract)
    return store


def _verified(tmp_path, rid="h", *, proposal=None, before=None, **kw):
    """A blocked run whose dossier ended in a PROPOSAL with a fix and was verified RED_REPRODUCED
    (the real pass, a fake analyst, the real verification with a scripted runner); returns
    `(store, dossier path, halt id)`."""
    store = _run(tmp_path, rid, **kw)
    rows = doubledown.double_down_pass(
        tmp_path, analysts=Fake(proposal or _proposal()).factory, now=NOW)
    assert [r["action"] for r in rows].count("proposal") == 1, rows
    [d] = [p for p in (store.dir / "dd").iterdir() if p.is_dir()]
    spaces = _spaces(tmp_path)
    row = ddverify.verify(tmp_path, store.dir, d.name, session=spaces.session, now=NOW,
                          materialize=Copier(),
                          check_runner=Runner(before or {"R1": (1, "red"), "K1": (0, "ok")}))
    assert row["action"] == "red_reproduced", row
    return store, d, d.name


def _apply(tmp_path, d, *, spaces=None, runner=None, copier=None, fixer=None, now=NOW, approved=True, **kw):
    """`ddapply.apply` with fakes. A person's approval is given unless `approved=False`: in this slice
    NO path applies a proven fix without one."""
    if approved:
        kw.setdefault("approved_by", "ada")
        kw.setdefault("approval_reason", "the proof reads right")
    spaces = spaces or _spaces(tmp_path)
    runner = runner or Runner({"R1": _after_fix(), "K1": (0, "ok")})
    copier = copier or Copier()
    fixer = fixer or Fixer()
    row = ddapply.apply(tmp_path, d.parent.parent, d.name, session=spaces.session, now=now,
                        materialize=copier, check_runner=runner, fix_applier=fixer, **kw)
    return row, types.SimpleNamespace(spaces=spaces, runner=runner, copier=copier, fixer=fixer)


def _record(d, name="apply.json"):
    return json.loads((d / name).read_text())


def _bytes(path: Path) -> dict:
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(path).rglob("*")) if p.is_file()}


def _measured(store) -> dict:
    """Everything the run measured: its receipts, results and logs, byte for byte."""
    return {k: v for k, v in _bytes(store.dir).items()
            if k.split("/")[0] in ("receipts", "results", "logs")}


def _main(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli.main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout


def _real_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], env=env,   # noqa: E731
                                    capture_output=True, text=True, check=True)
    run("init", "-q", "-b", "main")
    (repo / "app.txt").write_text("hello\n")
    run("add", "-A")
    run("commit", "-q", "-m", "one")
    return repo


# --- the fix: what a scratch copy is asked to do ----------------------------------------------- #

NEW_FILE = "diff --git a/n.py b/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1 @@\n+x\n"


def test_a_diff_that_modifies_or_creates_text_files_is_read_and_its_paths_are_named():
    assert ddapply.fix_paths(FIX) == ["app.txt"]
    assert ddapply.fix_paths(NEW_FILE) == ["n.py"]
    two = FIX + "--- a/sub/b.txt\n+++ b/sub/b.txt\n@@ -1 +1 @@\n-a\n+b\n" + NEW_FILE
    assert ddapply.fix_paths(two) == ["app.txt", "n.py", "sub/b.txt"]
    assert ddapply.fix_paths(FIX + "\\ No newline at end of file\n") == ["app.txt"]


@pytest.mark.parametrize("bad, needle", [
    ("", "empty"), ("   \n", "empty"), ("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\0\n", "NUL"),
    ("--- a/x\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n", "deletes"),
    ("deleted file mode 100644\n--- a/x\n+++ /dev/null\n", "deleted file mode"),
    ("rename from x\nrename to y\n", "rename"),
    ("diff --git a/x b/y\n--- a/x\n+++ b/y\n@@ -1 +1 @@\n-a\n+b\n", "one path twice"),
    ("--- a/x\n+++ b/y\n@@ -1 +1 @@\n-a\n+b\n", "renamed"),
    ("copy from x\ncopy to y\n", "copy"),
    ("old mode 100644\nnew mode 100755\n", "old mode"),
    ("GIT binary patch\nliteral 0\n", "binary"),
    ("Binary files a/x and b/x differ\n", "Binary files"),
    ("diff --git a/l b/l\nnew file mode 120000\n--- /dev/null\n+++ b/l\n@@ -0,0 +1 @@\n+/etc/passwd\n",
     "link"),
    ("diff --git a/s b/s\nnew file mode 160000\n--- /dev/null\n+++ b/s\n@@ -0,0 +1 @@\n+x\n", "link"),
    ("--- a/../x\n+++ b/../x\n@@ -1 +1 @@\n-a\n+b\n", "plain relative"),
    ("--- /dev/null\n+++ /tmp/x\n@@ -0,0 +1 @@\n+a\n", "does not start with 'b/'"),
    ("--- /dev/null\n+++ b//tmp/x\n@@ -0,0 +1 @@\n+a\n", "plain relative"),
    ("--- a/.git/config\n+++ b/.git/config\n@@ -1 +1 @@\n-a\n+b\n", "plain relative"),
    ("--- a/a//b\n+++ b/a//b\n@@ -1 +1 @@\n-a\n+b\n", "plain relative"),
    ("--- a/x\\y\n+++ b/x\\y\n@@ -1 +1 @@\n-a\n+b\n", "plain relative"),
    ("--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n-a\n+b\n", "fewer lines"),
    ("--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n x\n", "more lines"),
    ("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n?b\n", "must start"),
    ("just text, no diff\n", "names no file"),
    ("--- x/tests/t.py\n+++ x/tests/t.py\n@@ -1 +1 @@\n-a\n+b\n", "does not start with 'a/'"),
    ("--- a/x\n+++ x/x\n@@ -1 +1 @@\n-a\n+b\n", "does not start with 'b/'"),
    ("--- tests/t.py\n+++ tests/t.py\n@@ -1 +1 @@\n-a\n+b\n", "does not start with 'a/'"),
    ("--- b/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n", "does not start with 'a/'"),
    ("diff --git a/conftest.py b/conftest.py\nnew file mode 100644\n", "no ---/\\+\\+\\+ lines"),
    ("diff --git a/c.py b/c.py\nnew file mode 100644\ndiff --git a/d.py b/d.py\n--- a/d.py\n+++ b/d.py\n@@ -1 +1 @@\n-a\n+b\n",
     "no ---/\\+\\+\\+ lines"),
    ("diff --git a/x b/x\n--- a/y\n+++ b/y\n@@ -1 +1 @@\n-a\n+b\n", "different files"),
    ("diff --git x/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n", "does not start with 'a/'"),
])
def test_everything_a_scratch_copy_should_not_be_asked_to_do_is_refused(bad, needle):
    with pytest.raises(ddapply.FixRefused, match=needle):
        ddapply.fix_paths(bad)


def test_the_number_of_files_of_a_fix_is_bounded():
    def many(n):
        return "".join(f"--- a/f{k}\n+++ b/f{k}\n@@ -1 +1 @@\n-a\n+b\n" for k in range(n))
    assert len(ddapply.fix_paths(many(16))) == 16 == ddapply.MAX_FIX_FILES
    with pytest.raises(ddapply.FixRefused, match="at most 16"):
        ddapply.fix_paths(many(17))


def test_a_removed_line_that_looks_like_a_header_is_not_one():
    sql = "--- a/q.sql\n+++ b/q.sql\n@@ -1,2 +1,2 @@\n--- a comment\n+++ b comment\n keep\n"
    assert ddapply.fix_paths(sql) == ["q.sql"]                    # read by the hunk's line counts


def test_git_applies_a_modifying_and_a_creating_diff_to_a_copy_and_nothing_else(
        tmp_path, processes_allowed):
    arena = tmp_path / "arena"
    arena.mkdir()
    (arena / "app.txt").write_text("hello\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched\n")
    ddapply.apply_fix(arena, FIX + NEW_FILE, tmp_path)
    assert (arena / "app.txt").read_text() == "hello fixed\n" and (arena / "n.py").read_text() == "x\n"
    assert outside.read_text() == "untouched\n" and sorted(os.listdir(tmp_path)) == [
        "arena", "outside.txt"]


def test_a_diff_that_does_not_apply_or_runs_through_a_link_is_refused_and_writes_nothing(
        tmp_path, processes_allowed):
    arena = tmp_path / "arena"
    arena.mkdir()
    (arena / "app.txt").write_text("other\n")
    with pytest.raises(ddapply.FixRefused, match="does not apply"):
        ddapply.apply_fix(arena, FIX, tmp_path)
    assert (arena / "app.txt").read_text() == "other\n"
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "app.txt").write_text("hello\n")
    (arena / "sub").symlink_to(target)
    through = FIX.replace("app.txt", "sub/app.txt")
    with pytest.raises(ddapply.FixRefused, match="link"):
        ddapply.apply_fix(arena, through, tmp_path)
    assert (target / "app.txt").read_text() == "hello\n"


# --- the block's own failing check ------------------------------------------------------------ #

def test_the_failed_checks_of_the_latest_iteration_are_read_from_the_runs_own_receipts(tmp_path):
    store = RunStore(tmp_path, "h")
    store.ensure()
    _receipt(store, "h", "OLD", "cmd-old", 1, iteration=1)              # an earlier iteration
    _receipt(store, "h", "R1", "cmd-r1", 1, iteration=2, attempt=1)
    _receipt(store, "h", "R1", "cmd-r1-again", 1, iteration=2, attempt=2)   # the newest attempt speaks
    _receipt(store, "h", "OKAY", "cmd-ok", 0, iteration=2)
    _receipt(store, "h", "INFRA", "cmd-infra", 124, iteration=2)        # a timeout is no failure
    _receipt(store, "h", "BASE", "cmd-base", 1, iteration=2, name="h-i2-a1-BASE-basis")
    _receipt(store, "g", "OTHER", "cmd-other", 1, iteration=2, name="g-i2-a1-OTHER")   # another run
    (store.receipts_dir / "junk.json").write_text("{not json")
    (store.receipts_dir / "huge.json").write_text(" " * (ddapply.MAX_RECEIPT_BYTES + 1))
    (store.receipts_dir / "link.json").symlink_to(store.receipts_dir / "h-i2-a1-R1.json")
    assert ddapply.failing_checks(store.dir, "h") == {"R1": "cmd-r1-again"}
    assert ddapply.failing_checks(tmp_path / "nowhere", "h") == {}
    empty = RunStore(tmp_path, "e")
    empty.ensure()
    assert ddapply.failing_checks(empty.dir, "e") == {}


def test_a_criterion_is_the_blocks_own_only_with_the_same_id_and_the_same_command_and_not_keep_green():
    crit = declared_criteria(_repair_spec("R1 cmd one", "R2 cmd two", "K1 cmd three", "R3 other"))
    failing = {"R1": "cmd one", "R2": "cmd 2", "K1": "cmd three", "R4": "cmd four"}
    assert ddapply.bound_ids(crit, failing) == ["R1"]       # R2: another command; K1: keep-green; R3: not failed


# --- the proof is a table -------------------------------------------------------------------- #

def _rows(**outcomes):
    return [{"id": k, "outcome": v, "artefactual": False} for k, v in outcomes.items()]


def test_the_proof_of_a_fix_is_a_table():
    before = _rows(R1="FAIL", K1="PASS", N1="FAIL")
    green = _rows(R1="PASS", K1="PASS", N1="PASS")
    decide = ddapply.decide_proof
    assert decide(before, green, ["R1"], 3)[:2] == ("PROVEN", "green_after")
    assert decide(before, _rows(R1="PASS", K1="PASS"), ["R1"], 3)[:2] == ("INCONCLUSIVE", "not_measured")
    assert decide(before, _rows(R1="PASS", K1="INCONCLUSIVE", N1="PASS"), ["R1"], 3)[:2] == (
        "INCONCLUSIVE", "not_measured")
    assert decide(before, _rows(R1="PASS", K1="REFUSED", N1="PASS"), ["R1"], 3)[:2] == (
        "NOT_PROVEN", "criterion_refused")
    assert decide(_rows(R1="PASS", K1="PASS", N1="FAIL"), green, ["R1"], 3)[:2] == (
        "NOT_PROVEN", "bound_not_red_before")
    artefactual = [{"id": "R1", "outcome": "FAIL", "artefactual": True}, *before[1:]]
    assert decide(artefactual, green, ["R1"], 3)[:2] == ("NOT_PROVEN", "bound_not_red_before")
    assert decide(_rows(R1="FAIL", K1="FAIL", N1="FAIL"), green, ["R1"], 3)[:2] == (
        "NOT_PROVEN", "keep_not_green_before")
    assert decide(before, _rows(R1="FAIL", K1="PASS", N1="PASS"), ["R1"], 3)[:2] == (
        "NOT_PROVEN", "still_red")
    assert decide(before, _rows(R1="PASS", K1="PASS", N1="FAIL"), ["R1"], 3)[:2] == (
        "NOT_PROVEN", "still_red")                     # a criterion that is neither bound nor keep must be green
    assert decide(before, _rows(R1="PASS", K1="FAIL", N1="PASS"), ["R1"], 3)[:2] == (
        "NOT_PROVEN", "keep_broken")
    assert decide(before, green, [], 3)[0] == "PROVEN"  # (`apply` never asks without a bound criterion)


# --- a proven fix is applied as a new iteration of the blocked run ----------------------------- #

def test_a_proven_fix_is_applied_as_a_new_iteration_of_the_blocked_run(tmp_path):
    store, d, halt_id = _verified(tmp_path)
    candidate_before = store.read_state().last_accepted_candidate
    measured_before = _measured(store)
    row, run = _apply(tmp_path, d)
    assert (row["action"], row["status"], row["reason"], row["executed"]) == (
        "applied", "APPLIED", "green_after", True)
    assert row["amendment_id"] == f"dd-{halt_id}" and row["next_step"] == f"hoh --root {tmp_path} run h"
    # the proof: the fix was applied to a scratch copy BEFORE the criteria ran on it, none of them early
    assert [c[1] for c in run.copier.calls] == [run.fixer.calls[0][0]] == [run.runner.calls[0]["cwd"]]
    assert run.fixer.calls[0][1] == FIX and all(c["fixed"] for c in run.runner.calls)
    assert [c["check"].check_id for c in run.runner.calls] == ["R1", "K1"]
    assert all(c["run_id"] == f"dda-{halt_id}" and c["isolation"] is Isolation.STRICT
               for c in run.runner.calls)                      # the regime of the verification
    # the run: unblocked, the specification amended through the ledger, the old text parked
    state = store.read_state()
    assert state.condition is Condition.ACTIVE and state.blocked_reason is None
    new_text = Path(state.spec_path).read_text()
    assert new_text.startswith(RUN_SPEC.rstrip("\n") + "\n\n## Correction proposed by the double-down")
    assert state.spec_digest == text_digest(new_text)
    chain = store.read_amendments(origin_digest=text_digest(RUN_SPEC))
    [a] = chain.amendments
    assert (a.amendment_id, a.kind.value, a.actor, a.affected_criteria) == (
        f"dd-{halt_id}", "widen", "hoh double-down", ["R1"])
    assert (a.from_digest, a.to_digest, a.after_acceptance) == (
        text_digest(RUN_SPEC), text_digest(new_text), True)
    assert Path(a.from_path).read_text() == RUN_SPEC                  # nothing is lost
    assert a.evidence[0] == f"dossier:{halt_id}" and len(a.evidence) == 3
    assert "UNBLOCKED" in state.history[-1] and f"dd-{halt_id}" in state.history[-1]
    assert any("specification amended" in line for line in state.history)
    # the record
    rec = _record(d)
    dossier = doubledown.read_dossier(d)
    assert (rec["schema"], rec["status"], rec["reason"], rec["attempts"]) == (
        "hoh-doubledown-application/1", "APPLIED", "green_after", 1)
    assert rec["block_digest"] == dossier.block["digest"]
    assert rec["outcome_digest"] == dossier.outcome["digest"]
    assert rec["verification_digest"] == dossier.verification["digest"]
    assert rec["amendment_id"] == f"dd-{halt_id}" and "routing" not in rec
    assert rec["bound"] == ["R1"] and rec["fix_sha256"] == hashlib.sha256(FIX.encode()).hexdigest()
    assert rec["spec_sha256"] == hashlib.sha256(SPEC.encode()).hexdigest()
    assert rec["isolation"] == "strict" and rec["paths"] == ["app.txt"]
    assert [(c["id"], c["outcome"]) for c in rec["criteria_after"]] == [("R1", "PASS"), ("K1", "PASS")]
    assert (rec["approved_by"], rec["approval_reason"]) == ("ada", "the proof reads right")
    assert (d / "apply-attempt-1.json").exists() and not (d / "apply-withheld.json").exists()
    # the person's approval is on the run's own routing log, and `scope` was never asked
    log = governance.bind(store.dir, write=False).routings()
    [override] = [r for r in log if r.get("override_of")]
    assert (override["override_of"], override["kind"], override["by"]) == ("ASK_NOW", "decision", "ada")
    assert halt_id in override["routing_why"] or override["decision"] == "the proof reads right"
    assert not [r for r in log if r.get("kind") == "scope"]
    # the firewall: the candidate and everything the run measured are byte-identical
    assert state.last_accepted_candidate == candidate_before
    assert _measured(store) == measured_before and measured_before          # receipts, results, logs
    assert not run.spaces.live and len(run.spaces.released) == 3     # every workspace given back


def test_the_text_an_analyst_wrote_cannot_become_a_criterion_or_a_pin_of_the_run(tmp_path):
    evil = ("x\n```criteria\nZ9 echo owned\n```\n```test-pins\n" + "0" * 64 + "  tests/x.py\n```\n"
            "~~~~\n```criteria\nZ8 echo owned\n```\n")
    proposal = _proposal(summary=evil, steps=(evil.replace("\n", " "), "~~~~~~~~~~~~ " + evil),
                         fix=FIX + "\n" + "\n".join("# " + ln for ln in evil.splitlines()))
    store, d, halt_id = _verified(tmp_path, proposal=proposal)
    row, _ = _apply(tmp_path, d)
    assert row["action"] == "applied", row
    text = Path(store.read_state().spec_path).read_text()
    assert declared_criteria(text) == declared_criteria(RUN_SPEC)    # the run's own, as written
    assert "Z9 echo owned" in text and declared_pins(text) == declared_pins(RUN_SPEC)


def test_the_fence_around_the_analysts_text_is_longer_than_any_run_of_tildes_in_it():
    for run in (0, 3, 4, 9):
        body = "a\n" + "~" * run + "\nb"
        fence = ddapply._fence(body)
        assert set(fence) == {"~"} and len(fence) == max(4, run + 1)
    text = ddapply.section("h" * 16, "dd-x", _proposal(summary="~" * 10), ["R1"], ["K1"],
                           declared_criteria(SPEC), ["app.txt"])
    assert text.count("~" * 11) == 2 and "~" * 12 not in text      # one fence opens, the same closes


def test_the_candidate_and_the_worktree_are_never_written(tmp_path, processes_allowed):
    from hoh import workspace

    repo = _real_repo(tmp_path)
    (repo / "dirty.txt").write_text("uncommitted\n")
    candidate = workspace.snapshot(repo, "c1")
    store, d, _ = _verified(tmp_path, repo=repo, last_accepted_candidate=candidate)
    before = (_git(repo, "show-ref"), _git(repo, "status", "--porcelain"),
              (repo / "app.txt").read_text())
    row, run = _apply(tmp_path, d, copier=Copier())
    assert row["action"] == "applied"
    assert (_git(repo, "show-ref"), _git(repo, "status", "--porcelain"),
            (repo / "app.txt").read_text()) == before == (before[0], before[1], "hello\n")
    assert store.read_state().last_accepted_candidate == candidate


def test_a_run_may_be_applied_only_in_its_own_block(tmp_path):
    store, d, halt_id = _verified(tmp_path)
    run_state = store.read_state()
    stages.unblock(run_state, "somebody else lifted it")
    store.write_state(run_state)
    row, run = _apply(tmp_path, d)
    assert row["action"] == "refused" and "not in the block" in row["why"] and "executed" not in row
    assert not (d / "apply.json").exists() and run.runner.calls == []
    store2, d2, _ = _verified(tmp_path, "g")
    row = ddapply.apply(tmp_path, store2.dir.parent / "h", d2.name, session=_spaces(tmp_path).session)
    assert row["action"] == "refused"                                # a dossier of another run


# --- what is not applied, and why: terminal at once ------------------------------------------- #

def _held_by_violation(store):
    state = store.read_state()
    state.blocked_kind = stages.CAPABILITY_VIOLATION
    store.write_state(state)


def _exhausted(store):
    state = store.read_state()
    state.budgets = Budgets(max_iterations=1)
    state.usage.iterations = 1
    store.write_state(state)


def _three_corrections(store):
    chain = store.read_amendments(origin_digest=store.read_state().spec_digest)
    from hoh.amendment import AmendmentKind, AmendmentLedger, SpecAmendment
    amendments, digest = [], chain.origin_digest
    for k in range(ddapply.dd.MAX_APPLIES_PER_RUN):
        new = f"{k:02d}" * 8
        amendments.append(SpecAmendment(
            amendment_id=f"dd-old{k}", run_id="h", from_digest=digest, to_digest=new,
            kind=AmendmentKind.WIDEN, actor="hoh double-down", reason="earlier",
            affected_criteria=["R1"]))
        digest = new
    store.write_amendments(AmendmentLedger(run_id="h", origin_digest=chain.origin_digest,
                                           amendments=amendments))
    state = store.read_state()
    state.spec_digest = digest
    store.write_state(state)


def test_a_capability_violation_is_never_lifted_by_the_loop(tmp_path):
    store, d, halt_id = _verified(tmp_path)
    _held_by_violation(store)                       # the hold is another halt: its kind is its identity
    before = store.read_state().model_dump_json()
    row, run = _apply(tmp_path, d)
    assert row["action"] == "refused" and "not in the block" in row["why"] and "executed" not in row
    assert run.runner.calls == [] and not (d / "apply.json").exists()
    assert store.read_state().model_dump_json() == before
    assert store.read_state().blocked_kind == stages.CAPABILITY_VIOLATION


@pytest.mark.parametrize("prepare, reason", [
    (_exhausted, "bound_exhausted"),
    (_three_corrections, "run_allowance_spent"),
])
def test_a_run_the_loop_may_not_or_cannot_continue_is_never_lifted(tmp_path, prepare, reason):
    store, d, halt_id = _verified(tmp_path)
    prepare(store)
    row, run = _apply(tmp_path, d)
    assert (row["action"], row["status"], row["reason"]) == ("not_applied", "NOT_APPLIED", reason)
    assert run.runner.calls == [] and run.copier.calls == []         # decided before anything was measured
    assert _record(d)["reason"] == reason and _record(d)["attempts"] == 0
    assert store.read_state().condition is Condition.BLOCKED
    assert Path(store.read_state().spec_path).read_text() == RUN_SPEC
    after = _bytes(store.dir)
    row2, _ = _apply(tmp_path, d)
    assert row2["action"] == "closed" and _bytes(store.dir) == after


def test_a_fix_the_product_refuses_is_not_applied_and_nothing_is_measured(tmp_path):
    store, d, _ = _verified(tmp_path, proposal=_proposal(fix="--- a/x\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"))
    row, run = _apply(tmp_path, d)
    assert (row["status"], row["reason"]) == ("NOT_APPLIED", "fix_refused") and run.runner.calls == []
    assert "deletes" in _record(d)["why"]


def test_a_repair_spec_whose_criteria_are_not_the_blocks_own_failing_check_is_not_applied(tmp_path):
    other = _repair_spec("Q1 test -f app.txt", "K1 " + KEEP_CMD)
    store, d, _ = _verified(tmp_path, proposal=_proposal(spec=other),
                            before={"Q1": (1, "red"), "K1": (0, "ok")})
    row, run = _apply(tmp_path, d, runner=Runner({"Q1": _after_fix()}))
    assert (row["status"], row["reason"]) == ("NOT_APPLIED", "not_bound")
    assert run.runner.calls == [] and "ties the red to the block" in _record(d)["why"]
    # the same id with another command is not the block's check either
    changed = _repair_spec("R1 grep -q fixed app.txt && true", "K1 " + KEEP_CMD)
    store, d, _ = _verified(tmp_path, "g", proposal=_proposal(spec=changed))
    row, _ = _apply(tmp_path, d)
    assert row["reason"] == "not_bound"
    # a run whose receipts hold no failed check has nothing to tie a red to
    store, d, _ = _verified(tmp_path, "k", receipts=False)
    row, _ = _apply(tmp_path, d)
    assert row["reason"] == "not_bound"


def test_a_fix_is_applied_only_to_the_candidate_the_proposal_was_verified_on(tmp_path):
    store, d, _ = _verified(tmp_path)
    state = store.read_state()
    state.last_accepted_candidate = _candidate(Path(state.repo_path), cid="c2", tree="c" * 40)
    store.write_state(state)
    row, run = _apply(tmp_path, d)
    assert (row["status"], row["reason"]) == ("NOT_APPLIED", "candidate_moved") and run.runner.calls == []


@pytest.mark.parametrize("fixer_refuses, runner_script, before, reason", [
    ("the fix does not apply to the candidate", None, None, "fix_does_not_apply"),
    (None, {"R1": (1, "still red"), "K1": (0, "ok")}, None, "still_red"),
    (None, {"R1": _after_fix(), "K1": (1, "broken")}, None, "keep_broken"),
    (None, None, {"R1": (1, "red"), "K1": (1, "red")}, "keep_not_green_before"),
])
def test_a_fix_that_is_not_proven_is_not_applied_and_the_run_stays_blocked(
        tmp_path, fixer_refuses, runner_script, before, reason):
    store, d, halt_id = _verified(tmp_path, before=before)
    row, run = _apply(tmp_path, d, fixer=Fixer(fixer_refuses),
                      runner=Runner(runner_script or {"R1": _after_fix(), "K1": (0, "ok")}))
    assert (row["action"], row["status"], row["reason"]) == ("not_applied", "NOT_APPLIED", reason)
    assert store.read_state().condition is Condition.BLOCKED
    assert Path(store.read_state().spec_path).read_text() == RUN_SPEC
    assert store.read_amendments(origin_digest=text_digest(RUN_SPEC)).amendments == []
    assert _record(d)["attempts"] == 1 and _record(d)["reason"] == reason


# --- a person applies a proven fix; no contract does -------------------------------------------- #

def _routings(store):
    gov = governance.bind(store.dir, write=False)
    return gov.routings() if gov is not None else []


@pytest.mark.parametrize("make_contract", [
    lambda: _contract(scope=Escalation.PARK_BRANCH_AND_CONTINUE),
    lambda: _contract(scope=Escalation.HUMAN_IF_AVAILABLE, mode=AutonomyMode.POLICY_GOVERNED),
    lambda: _contract(scope=Escalation.AUTO_POLICY, mode=AutonomyMode.SUPERVISED),
    lambda: _contract(scope=Escalation.AUTO_POLICY, mode=AutonomyMode.POLICY_GOVERNED),
    lambda: _contract(scope=Escalation.AUTO_POLICY, mode=AutonomyMode.PLAN_AUTONOMOUS),
    lambda: _contract(scope=Escalation.SYSTEM_TWO, mode=AutonomyMode.PLAN_AUTONOMOUS),
    lambda: _contract(scope=Escalation.AUTO_POLICY, mode=AutonomyMode.UNATTENDED_LONG_HORIZON),
    lambda: _contract(scope=Escalation.SYSTEM_ONE_THEN_SYSTEM_TWO, mode=AutonomyMode.UNATTENDED_LONG_HORIZON),
    lambda: False,
], ids=["park", "human", "auto-supervised", "auto-governed", "auto-plan", "system-two-plan",
        "auto-unattended", "system-one-unattended", "no-contract"])
def test_every_proven_fix_is_withheld_whatever_the_contract_says_and_applied_only_by_a_person(
        tmp_path, make_contract):
    """A hand-written contract whose `scope` escalation is AUTO_POLICY or SYSTEM_TWO resolves a scope
    decision by policy in EVERY mode (asserted below, as the review reproduced it): the double-down does
    not ask. The decision kind that lets an UNATTENDED_LONG_HORIZON contract apply a proven correction
    itself is a later slice's (`proven_correction`)."""
    from hoh import autonomy

    contract = make_contract()
    store, d, halt_id = _verified(tmp_path, contract=contract)
    if contract and contract.escalation.get("scope") in (Escalation.AUTO_POLICY, Escalation.SYSTEM_TWO,
                                                         Escalation.SYSTEM_ONE_THEN_SYSTEM_TWO):
        resolved = autonomy.route(contract, decision="d", kind="scope", delegable=True)
        assert resolved.handling is autonomy.Handling.RESOLVED_BY_POLICY      # the path the loop must not take
    routings = _routings(store)
    row, run = _apply(tmp_path, d, approved=False)
    assert (row["action"], row["reason"]) == ("withheld", "awaits_a_person")
    assert "proven_correction" in row["why"] and "--approved-by" in row["next_step"] and halt_id in row["next_step"]
    assert run.runner.calls == [] and run.copier.calls == [] and "executed" not in row
    rec = _record(d, "apply-withheld.json")
    dossier = doubledown.read_dossier(d)
    assert rec["verification_digest"] == dossier.verification["digest"] and dossier.withheld is not None
    assert "contract_digest" not in rec                                       # the contract is not consulted
    assert dossier.application is None and not (d / "apply-attempt-1.json").exists()
    assert store.read_state().condition is Condition.BLOCKED
    assert Path(store.read_state().spec_path).read_text() == RUN_SPEC
    assert _routings(store) == routings                                       # nothing was asked or recorded
    kept = _bytes(store.dir)
    row2, _ = _apply(tmp_path, d, approved=False)                             # said again, written never
    assert row2["action"] == "withheld" and "record" not in row2 and _bytes(store.dir) == kept
    assert ddapply.apply_if_due(tmp_path, store.dir, halt_id, session=run.spaces.session, now=NOW) is None
    # the same dossier, applied by a person: the only way
    row3, run3 = _apply(tmp_path, d)
    assert (row3["action"], row3["approved_by"]) == ("applied", "ada") and run3.runner.calls


def test_the_pass_records_that_a_verified_fix_is_withheld_and_applies_nothing(tmp_path):
    verified = [_verified(tmp_path, rid)[2] for rid in ("h", "g", "k")]
    runner = Runner({"R1": _after_fix(), "K1": (0, "ok")})
    spaces = _spaces(tmp_path)
    seen = []

    def applier(owner, halt_id, now):
        seen.append(halt_id)
        return ddapply.apply_if_due(tmp_path, owner, halt_id, session=spaces.session, now=now,
                                    materialize=Copier(), check_runner=runner, fix_applier=Fixer())
    rows = doubledown.double_down_pass(tmp_path, now=NOW, applier=applier)
    assert sorted(seen) == sorted(verified)
    assert [r["action"] for r in rows if r.get("scope") == "dossier"] == ["withheld"] * 3
    assert runner.calls == [] and spaces.log == []                  # nothing was measured, no workspace was asked for
    assert all(RunStore(tmp_path, rid).read_state().condition is Condition.BLOCKED for rid in ("h", "g", "k"))
    kept = _state_bytes_all(tmp_path)
    rows = doubledown.double_down_pass(tmp_path, now=NOW, applier=applier)   # quiet: the dossiers are decided
    assert [r for r in rows if r.get("scope") == "dossier"] == [] and _state_bytes_all(tmp_path) == kept


def _state_bytes_all(root):
    return {k: v for k, v in _bytes(Path(root)).items()}


def test_a_contract_that_cannot_be_read_is_a_terminal_refusal_for_an_approved_fix_only(tmp_path):
    store, d, _ = _verified(tmp_path)
    (store.dir / "autonomy" / "contract.json").write_text("{not json")
    row, run = _apply(tmp_path, d, approved=False)                # not approved: withheld without reading it
    assert row["action"] == "withheld" and run.runner.calls == []
    row, run = _apply(tmp_path, d)
    assert (row["status"], row["reason"]) == ("NOT_APPLIED", "contract_unreadable") and run.runner.calls == []


def test_a_person_applies_a_withheld_fix_and_the_approval_is_on_the_runs_own_log(tmp_path):
    store, d, halt_id = _verified(tmp_path, contract=_contract(scope=Escalation.PARK_BRANCH_AND_CONTINUE))
    _apply(tmp_path, d, approved=False)                        # withheld, once
    row, run = _apply(tmp_path, d, approved_by="ada", approval_reason="the repair reads right")
    assert (row["action"], row["status"], row["approved_by"]) == ("applied", "APPLIED", "ada")
    assert run.runner.calls and all(c["fixed"] for c in run.runner.calls)    # the proof is still done
    rec = _record(d)
    assert (rec["approved_by"], rec["approval_reason"]) == ("ada", "the repair reads right")
    [override] = [r for r in _routings(store) if r.get("override_of")]
    assert (override["override_of"], override["kind"], override["by"], override["decision"]) == (
        "ASK_NOW", "decision", "ada", "the repair reads right")
    assert halt_id in doubledown.read_dossier(d).application["amendment_id"]
    assert store.read_state().condition is Condition.ACTIVE
    assert doubledown.read_dossier(d).withheld is not None          # what was said stays said


def test_a_person_applies_a_fix_for_a_run_with_no_contract_and_it_is_proven_all_the_same(tmp_path):
    store, d, _ = _verified(tmp_path, contract=False)
    row, run = _apply(tmp_path, d, approved_by="ada", approval_reason="no contract, my call",
                      runner=Runner({"R1": (1, "still red"), "K1": (0, "ok")}))
    assert (row["status"], row["reason"]) == ("NOT_APPLIED", "still_red")     # approval skips no proof
    assert store.read_state().condition is Condition.BLOCKED
    store, d, _ = _verified(tmp_path, "g", contract=False)
    row, _ = _apply(tmp_path, d, approved_by="ada", approval_reason="no contract, my call")
    assert row["action"] == "applied" and _record(d)["approved_by"] == "ada"
    assert not (store.dir / "autonomy").exists()


@pytest.mark.parametrize("by, reason", [("", "r"), ("a", ""), (" a", "r"), ("a", "r "), ("a\nb", "r"),
                                         ("a" * 1001, "r"), ("a", "r" * 1001), (None, "r"), ("a", None)])
def test_an_approval_names_who_and_why_in_one_printable_line(tmp_path, by, reason):
    store, d, _ = _verified(tmp_path, contract=False)
    row, run = _apply(tmp_path, d, approved=False, approved_by=by, approval_reason=reason)
    assert row["action"] == "refused" and run.runner.calls == [] and not (d / "apply-attempt-1.json").exists()


# --- termination: the bound, the terminal record, the quiet --------------------------------- #

def test_a_proof_that_measured_nothing_is_tried_at_most_twice_and_then_ends_for_good(tmp_path):
    from hoh.runner import PolicyUnavailable

    store, d, _ = _verified(tmp_path)
    spaces = _spaces(tmp_path)
    runner = Runner({"R1": PolicyUnavailable("no sandbox backend")})
    rows = [_apply(tmp_path, d, spaces=spaces, runner=runner)[0] for _ in range(2)]
    assert [(r["action"], r.get("attempts_left"), r["reason"]) for r in rows] == [
        ("inconclusive", 1, "not_measured"), ("not_applied", None, "attempts_spent")]
    assert sorted(n for n in os.listdir(d) if n.startswith("apply")) == [
        "apply-attempt-1.json", "apply-attempt-2.json", "apply.json"]
    assert "no sandbox backend" in _record(d)["why"] and _record(d)["attempts"] == 2
    kept, calls = _bytes(store.dir), len(runner.calls)
    again, _ = _apply(tmp_path, d, spaces=spaces, runner=runner)
    assert again["action"] == "closed" and _bytes(store.dir) == kept and len(runner.calls) == calls
    assert store.read_state().condition is Condition.BLOCKED


def test_a_crash_inside_an_attempt_still_counts(tmp_path):
    store, d, _ = _verified(tmp_path)
    spaces = _spaces(tmp_path)
    for k in range(2):
        with pytest.raises(KeyboardInterrupt):
            _apply(tmp_path, d, spaces=spaces, runner=Runner({"R1": KeyboardInterrupt()}))
        assert len(doubledown.read_dossier(d).apply_attempts) == k + 1      # recorded before it measured
    ok = Runner({"R1": _after_fix(), "K1": (0, "ok")})
    row, run = _apply(tmp_path, d, spaces=spaces, runner=ok)
    assert ok.calls == [] and (row["status"], row["reason"]) == ("NOT_APPLIED", "attempts_spent")
    assert store.read_state().condition is Condition.BLOCKED


def test_an_attempt_that_cannot_be_recorded_measures_nothing_and_changes_nothing(tmp_path, monkeypatch):
    store, d, _ = _verified(tmp_path)
    real = doubledown._create
    monkeypatch.setattr(doubledown, "_create", lambda path, record: (_ for _ in ()).throw(
        OSError(28, "No space left on device")) if Path(path).name.startswith("apply-attempt")
        else real(path, record))
    before = _bytes(store.dir)
    runner = Runner({"R1": _after_fix()})
    for _ in range(4):                                          # said every time, never measured
        row, run = _apply(tmp_path, d, runner=runner)
        assert row["action"] == "refused" and "No space left" in row["why"] and "executed" not in row
    assert runner.calls == [] and _bytes(store.dir) == before


def test_a_decision_that_cannot_be_written_is_said_and_never_measured_again_past_the_bound(
        tmp_path, monkeypatch):
    store, d, _ = _verified(tmp_path)
    real = doubledown._create
    monkeypatch.setattr(doubledown, "_create", lambda path, record: (_ for _ in ()).throw(
        OSError(28, "full")) if Path(path).name == "apply.json" else real(path, record))
    runner = Runner({"R1": (1, "still red"), "K1": (0, "ok")})
    rows = [_apply(tmp_path, d, runner=runner)[0] for _ in range(5)]
    assert [r["action"] for r in rows] == ["unrecorded"] * 5      # said every time ...
    assert len(runner.calls) == 4 and len(doubledown.read_dossier(d).apply_attempts) == 2   # ... measured twice
    monkeypatch.setattr(doubledown, "_create", real)
    row, _ = _apply(tmp_path, d, runner=runner)
    assert (row["action"], row["reason"]) == ("not_applied", "attempts_spent") and len(runner.calls) == 4


def test_an_application_that_cannot_be_recorded_still_changed_the_run_and_is_not_repeated(
        tmp_path, monkeypatch):
    store, d, halt_id = _verified(tmp_path)
    real = doubledown._create
    monkeypatch.setattr(doubledown, "_create", lambda path, record: (_ for _ in ()).throw(
        OSError(28, "full")) if Path(path).name == "apply.json" else real(path, record))
    row, run = _apply(tmp_path, d)
    assert (row["action"], row["status"]) == ("unrecorded", "APPLIED") and "full" in row["why"]
    assert store.read_state().condition is Condition.ACTIVE
    [a] = store.read_amendments(origin_digest=text_digest(RUN_SPEC)).amendments
    assert a.amendment_id == f"dd-{halt_id}"
    again, run2 = _apply(tmp_path, d)                              # the block is gone: nothing to answer
    assert again["action"] == "refused" and "not in the block" in again["why"] and run2.runner.calls == []
    assert len(store.read_amendments(origin_digest=text_digest(RUN_SPEC)).amendments) == 1


def test_a_terminal_application_is_quiet_for_another_call_and_for_the_pass(tmp_path):
    store, d, halt_id = _verified(tmp_path)
    row, run = _apply(tmp_path, d)
    assert row["action"] == "applied"
    kept = _bytes(store.dir)
    again, run2 = _apply(tmp_path, d)
    assert (again["action"], again["status"], again["reason"]) == ("closed", "APPLIED", "green_after")
    assert run2.runner.calls == [] and _bytes(store.dir) == kept
    assert ddapply.apply_if_due(tmp_path, store.dir, halt_id, session=run.spaces.session, now=NOW) is None
    rows = doubledown.double_down_pass(tmp_path, now=NOW, applier=lambda *a: pytest.fail("asked"))
    assert [r["action"] for r in rows] == [] and _bytes(store.dir) == kept     # the block is gone


def test_a_dossier_is_applied_once_even_if_the_same_block_comes_back(tmp_path):
    store, d, halt_id = _verified(tmp_path)
    assert _apply(tmp_path, d)[0]["action"] == "applied"
    state = store.read_state()
    stages.block(state, "stuck again")                         # the very same block: same halt id
    store.write_state(state)
    assert halt_id in {h.halt_id for h in doubledown.halts_of_run(store.read_state())}
    runner = Runner({"R1": _after_fix()})
    asked = []
    rows = doubledown.double_down_pass(
        tmp_path, now=NOW, applier=lambda owner, hid, now: asked.append(hid) or ddapply.apply_if_due(
            tmp_path, owner, hid, session=_spaces(tmp_path).session, now=now, check_runner=runner))
    assert asked == [halt_id] and [r["action"] for r in rows if r.get("scope") == "run"] == ["closed"]
    assert runner.calls == []
    assert len(store.read_amendments(origin_digest=text_digest(RUN_SPEC)).amendments) == 1
    assert store.read_state().condition is Condition.BLOCKED       # the block stays, for a person to see
    assert doubledown.decisions_for_project.__doc__                  # (the person's view: next section)


def test_a_run_another_driver_holds_is_not_touched(tmp_path, monkeypatch):
    from hoh import rundrive

    store, d, _ = _verified(tmp_path)
    monkeypatch.setattr(rundrive, "refusal_for_resume", lambda root, run_id, **kw: f"Run {run_id} is held")
    before = _bytes(store.dir)
    row, run = _apply(tmp_path, d)
    assert row["action"] == "refused" and "is held" in row["why"] and "executed" not in row
    assert run.runner.calls == [] and _bytes(store.dir) == before


def test_a_run_that_moved_while_the_fix_was_proven_is_not_changed(tmp_path):
    store, d, halt_id = _verified(tmp_path)

    def lifted_meanwhile(arena):
        state = store.read_state()
        stages.unblock(state, "a person was quicker")
        store.write_state(state)
        return (0, "ok")
    row, run = _apply(tmp_path, d, runner=Runner({"R1": lifted_meanwhile, "K1": (0, "ok")}))
    assert (row["status"], row["reason"]) == ("NOT_APPLIED", "run_moved")
    assert Path(store.read_state().spec_path).read_text() == RUN_SPEC
    assert store.read_amendments(origin_digest=text_digest(RUN_SPEC)).amendments == []


def test_a_specification_that_is_not_the_text_the_state_names_is_not_amended(tmp_path):
    store, d, _ = _verified(tmp_path)

    def edited_meanwhile(arena):
        Path(store.read_state().spec_path).write_text(RUN_SPEC + "edited by hand\n")
        return (0, "ok")
    row, _ = _apply(tmp_path, d, runner=Runner({"R1": edited_meanwhile, "K1": (0, "ok")}))
    assert (row["status"], row["reason"]) == ("NOT_APPLIED", "spec_changed")
    assert Path(store.read_state().spec_path).read_text().endswith("edited by hand\n")
    assert store.read_state().condition is Condition.BLOCKED


def test_a_failure_while_the_run_is_written_is_said_and_leaves_a_visible_state(tmp_path, monkeypatch):
    store, d, halt_id = _verified(tmp_path)
    real = RunStore.write_state

    def failing(self, state):
        if state.condition is Condition.ACTIVE:
            raise OSError(28, "No space left on device")
        return real(self, state)
    monkeypatch.setattr(RunStore, "write_state", failing)
    row, run = _apply(tmp_path, d)
    assert row["action"] == "refused" and "No space left" in row["why"] and row["executed"]
    assert store.read_state().condition is Condition.BLOCKED and not (d / "apply.json").exists()
    monkeypatch.setattr(RunStore, "write_state", real)
    row2, run2 = _apply(tmp_path, d)                               # the spec was written, the state not
    assert (row2["status"], row2["reason"]) == ("NOT_APPLIED", "spec_changed")
    assert len(doubledown.read_dossier(d).apply_attempts) == 2 and store.read_state().condition is Condition.BLOCKED


# --- the reversal ---------------------------------------------------------------------------- #

def test_a_person_reverts_an_application_with_a_second_amendment_and_it_is_recorded(tmp_path):
    store, d, halt_id = _verified(tmp_path)
    assert _apply(tmp_path, d)[0]["action"] == "applied"
    row = ddapply.revert(tmp_path, store.dir, halt_id, by="ada", reason="the repair was wrong", now=NOW)
    assert (row["action"], row["amendment_id"], row["by"]) == ("reverted", f"dd-{halt_id}-revert", "ada")
    state = store.read_state()
    assert Path(state.spec_path).read_text() == RUN_SPEC and state.spec_digest == text_digest(RUN_SPEC)
    chain = store.read_amendments(origin_digest=text_digest(RUN_SPEC))
    first, second = chain.amendments
    assert (second.kind.value, second.actor, second.reason, second.affected_criteria) == (
        "narrow", "ada", "the repair was wrong", ["R1"])
    assert (second.from_digest, second.to_digest) == (first.to_digest, first.from_digest)
    assert second.evidence == [f"reverts:dd-{halt_id}"] and chain.current_digest() == text_digest(RUN_SPEC)
    rec = _record(d, "revert.json")
    dossier = doubledown.read_dossier(d)
    assert (rec["by"], rec["reason"], rec["application_digest"]) == (
        "ada", "the repair was wrong", dossier.application["digest"])
    assert dossier.reversal is not None
    assert state.condition is Condition.ACTIVE            # the unblock is not undone: the run goes on
    kept = _bytes(store.dir)
    again = ddapply.revert(tmp_path, store.dir, halt_id, by="ada", reason="again", now=NOW)
    assert again["action"] == "closed" and _bytes(store.dir) == kept


def test_a_reversal_is_refused_when_it_would_undo_something_else_or_nothing(tmp_path, monkeypatch):
    from hoh import rundrive
    from hoh.amendment import AmendmentKind, AmendmentLedger, SpecAmendment

    store, d, halt_id = _verified(tmp_path)
    revert = lambda **kw: ddapply.revert(tmp_path, store.dir, halt_id,   # noqa: E731
                                         **{"by": "ada", "reason": "r", **kw})
    assert revert()["action"] == "refused"                    # nothing was applied yet
    assert _apply(tmp_path, d)[0]["action"] == "applied"
    before = _bytes(store.dir)
    for bad in ({"by": ""}, {"reason": " r"}, {"by": "a" * 1001}, {"reason": "r\n"}):
        assert revert(**bad)["action"] == "refused" and _bytes(store.dir) == before
    monkeypatch.setattr(rundrive, "refusal_for_resume", lambda root, run_id, **kw: "held by a driver")
    held = revert()
    assert held["action"] == "refused" and "held by a driver" in held["why"] and _bytes(store.dir) == before
    monkeypatch.undo()
    spec = Path(store.read_state().spec_path)
    spec.write_text(spec.read_text() + "hand edit\n")          # the text is no longer the one it wrote
    row = revert()
    assert row["action"] == "refused" and "no longer the text" in row["why"]
    spec.write_text(spec.read_text()[:-len("hand edit\n")])
    chain = store.read_amendments(origin_digest=text_digest(RUN_SPEC))
    newer = SpecAmendment(amendment_id="later", run_id="h", from_digest=chain.current_digest(),
                          to_digest="f" * 16, kind=AmendmentKind.CLARIFY, actor="x", reason="later")
    store.write_amendments(AmendmentLedger(run_id="h", origin_digest=chain.origin_digest,
                                           amendments=[*chain.amendments, newer]))
    row = revert()
    assert row["action"] == "refused" and "later amendment" in row["why"]
    assert not (d / "revert.json").exists()


# --- the pass and the commands ---------------------------------------------------------------- #

def test_the_pass_verifies_a_fix_in_the_tick_that_made_it_withholds_it_and_a_person_applies_it(tmp_path):
    store = _run(tmp_path, "h")
    spaces = _spaces(tmp_path)
    before_runner = Runner({"R1": (1, "red"), "K1": (0, "ok")})
    after_runner = Runner({"R1": _after_fix(), "K1": (0, "ok")})
    verifier = lambda owner, hid, now: ddverify.verify_if_due(          # noqa: E731
        tmp_path, owner, hid, session=spaces.session, now=now, materialize=Copier(),
        check_runner=before_runner)
    applier = lambda owner, hid, now: ddapply.apply_if_due(             # noqa: E731
        tmp_path, owner, hid, session=spaces.session, now=now, materialize=Copier(),
        check_runner=after_runner, fix_applier=Fixer())
    rows = doubledown.double_down_pass(tmp_path, analysts=Fake(_proposal()).factory, now=NOW,
                                       verifier=verifier, applier=applier)
    assert [r["action"] for r in rows] == ["proposal", "red_reproduced", "withheld"]
    assert store.read_state().condition is Condition.BLOCKED and after_runner.calls == []
    kept = _bytes(store.dir)
    rows = doubledown.double_down_pass(tmp_path, analysts=Fake(_proposal()).factory, now=NOW,
                                       verifier=verifier, applier=applier)
    assert [r["action"] for r in rows] == ["closed"] and _bytes(store.dir) == kept     # quiet
    [d] = [p for p in (store.dir / "dd").iterdir() if p.is_dir()]
    row, _ = _apply(tmp_path, d)                                         # the person
    assert row["action"] == "applied" and store.read_state().condition is Condition.ACTIVE


def test_the_pass_without_an_applier_applies_nothing_and_an_applier_that_raises_is_a_row(tmp_path):
    store, d, halt_id = _verified(tmp_path)
    rows = doubledown.double_down_pass(tmp_path, now=NOW)
    assert not (d / "apply.json").exists() and store.read_state().condition is Condition.BLOCKED
    assert all(r["action"] != "applied" for r in rows)

    def boom(owner, hid, now):
        raise RuntimeError("x" * 500 + "\u2028 \ud800")
    rows = doubledown.double_down_pass(tmp_path, now=NOW, applier=boom)
    [bad] = [r for r in rows if r.get("action") == "refused"]
    assert bad["halt_id"] == halt_id and bad["why"].startswith("RuntimeError") and len(bad["why"]) <= 300
    assert bad["why"].isascii()


def _ephemeral(tmp_path, monkeypatch):
    """The command's own session code with the product's real `EphemeralRoots` (a registry that
    only answers what the roots ask of it); returns what the admission saw."""
    import hoh.storage_ephemeral as se

    state_root = tmp_path.parent / f"{tmp_path.name}-registry"
    state_root.mkdir()
    reserved, released, closed = [], [], []

    class Admission:
        def reserve(self, project_id, op_kind, *, estimate_bytes=None):
            reserved.append((project_id, op_kind, estimate_bytes))
            return types.SimpleNamespace(admitted=True, id=f"res-{len(reserved)}")

        def release(self, reservation, actual_bytes):
            released.append(actual_bytes)

    class Registry:
        def _read_lease(self, project_id, session_id):
            return types.SimpleNamespace(pid=os.getpid(), pid_start=1, boot_id="b")

        def lease_is_live(self, lease):
            return True

        def scratch_dir(self, project_id, session_id, kind):
            path = state_root / "projects" / project_id / kind / session_id
            path.mkdir(parents=True, exist_ok=True)
            return path

    admission = Admission()
    roots = se.EphemeralRoots(Registry(), "pd", "dda-session", admission=admission)

    class Session:
        session_id = "dda-session"
        project_id = "pd"

        def __init__(self):
            self.roots = roots

        def close(self):
            closed.append(1)
            return ""
    asked: list = []
    monkeypatch.setattr(cli, "storage_admission_for", lambda option, **kw: (
        asked.append(option) or (admission, state_root)))
    monkeypatch.setattr(cli, "_open_ephemeral", lambda *a: (Session(), "name"))
    return types.SimpleNamespace(reserved=reserved, released=released, closed=closed,
                                 state_root=state_root, asked=asked)


def _real_runner_without_the_scratch(monkeypatch):
    """The product's real `runner.run_check`, called without its `scratch=` (the runner refuses a
    scratch that lies inside the scratch of the check that runs this test); the scratches the
    module passes are recorded."""
    from hoh import runner as runner_module

    real, scratches = runner_module.run_check, []

    def run(check, candidate, **kw):
        scratches.append(Path(kw.pop("scratch")))
        return real(check, candidate, **kw)
    monkeypatch.setattr(runner_module, "run_check", run)
    return scratches


def test_resume_quota_with_no_flag_verifies_and_withholds_and_a_person_applies_through_the_command_line(
        tmp_path, monkeypatch, processes_allowed):
    """THE PRODUCTION PATH. The unit's tick: `hoh resume-quota` (no flag) -> cmd_resume_quota -> _double_down_pass
    -> double_down_pass -> ddverify.verify_if_due (RED_REPRODUCED), then ddapply.apply_if_due (WITHHELD: nothing
    is measured, nothing is applied). The person: `hoh double-down apply --approved-by` -> cmd_double_down ->
    ddapply.apply -> workspace.materialize, `git apply` (and `git apply --numstat -z`), runner.run_check,
    governance.record_override, amendment.park_and_amend, stages.unblock."""
    from hoh import workspace

    eph = _ephemeral(tmp_path, monkeypatch)
    scratches = _real_runner_without_the_scratch(monkeypatch)
    repo = _real_repo(tmp_path)
    candidate = workspace.snapshot(repo, "c1")
    store = _run(tmp_path, "h", repo=repo, last_accepted_candidate=candidate)
    doubledown.double_down_pass(tmp_path, analysts=Fake(_proposal()).factory, now=NOW)
    [d] = [p for p in (store.dir / "dd").iterdir()]
    measured = _measured(store)
    rc, out, err = _main(["--root", str(tmp_path), "resume-quota", "--verify-isolation", "none",
                          "--storage-root", str(eph.state_root)])
    assert rc == 0, err
    rows = [r for r in json.loads(out)["double_down"] if r.get("scope") == "dossier"]
    assert [(r["action"], r.get("status")) for r in rows] == [("red_reproduced", "RED_REPRODUCED"),
                                                              ("withheld", None)]
    assert "--approved-by" in rows[1]["next_step"] and (d / "apply-withheld.json").exists()
    assert store.read_state().condition is Condition.BLOCKED and len(eph.reserved) == 3   # only the verification
    def tree():
        return {k: v for k, v in _bytes(store.dir).items() if k != ".lock"}
    kept = tree()
    rc, out2, _ = _main(["--root", str(tmp_path), "resume-quota", "--verify-isolation", "none",
                         "--storage-root", str(eph.state_root)])
    assert rc == 0 and tree() == kept and len(eph.reserved) == 3        # the second tick: quiet, nothing measured
    assert not [r for r in json.loads(out2)["double_down"] if r["action"] in ("withheld", "applied", "red_reproduced")]
    base = ["--root", str(tmp_path), "double-down", "apply", d.name, "--run", "h", "--storage-root", str(eph.state_root)]
    rc, out3, err = _main([*base, "--approved-by", "ada", "--reason", "reads right"])
    assert rc == 0, err
    row = json.loads(out3)
    assert (row["action"], row["status"], row["approved_by"]) == ("applied", "APPLIED", "ada")
    assert row["next_step"] == f"hoh --root {tmp_path} run h"
    rec = _record(d)
    assert (rec["isolation"], rec["bound"], rec["approved_by"]) == ("none", ["R1"], "ada")
    assert [(c["id"], c["outcome"]) for c in rec["criteria_after"]] == [("R1", "PASS"), ("K1", "PASS")]
    assert [r[2] for r in eph.reserved] == [512 << 20, 64 << 20, 64 << 20] * 2
    assert len(eph.released) == 6 and eph.closed == [1, 1]          # every workspace given back
    assert len(scratches) == 4 and len(set(scratches)) == 4
    assert eph.asked == [str(eph.state_root)] * 2                    # the verifier's session and the command's
    state = store.read_state()
    assert state.condition is Condition.ACTIVE and "Correction proposed" in Path(state.spec_path).read_text()
    assert (repo / "app.txt").read_text() == "hello\n"              # the fix was measured, not applied
    assert _git(repo, "status", "--porcelain") == "" and state.last_accepted_candidate == candidate
    assert _measured(store) == measured


def test_hoh_double_down_apply_prints_a_row_and_its_exit_code_says_whether_it_is_decided(
        tmp_path, monkeypatch):
    eph = _ephemeral(tmp_path, monkeypatch)
    store, d, halt_id = _verified(tmp_path, contract=_contract(scope=Escalation.PARK_BRANCH_AND_CONTINUE))
    monkeypatch.setattr("hoh.workspace.materialize", Copier())
    monkeypatch.setattr("hoh.runner.run_check", Runner({"R1": _after_fix(), "K1": (0, "ok")}))
    monkeypatch.setattr(ddapply, "apply_fix", Fixer())
    argv = ["--root", str(tmp_path), "double-down", "apply", halt_id, "--run", "h",
            "--storage-root", str(eph.state_root)]
    rc, out, _ = _main(argv)
    assert rc == 2 and json.loads(out)["action"] == "withheld"      # nothing decided
    for odd in (["--approved-by", "ada"], ["--reason", "r"]):
        rc, out, err = _main([*argv, *odd])
        assert rc == 2 and "go together" in err and out == ""
    rc, out, err = _main([*argv, "--approved-by", "ada", "--reason", "reads right"])
    assert rc == 0, err
    row = json.loads(out)
    assert (row["action"], row["approved_by"], row["halt_id"]) == ("applied", "ada", halt_id)
    rc, out, _ = _main([*argv, "--approved-by", "ada", "--reason", "reads right"])
    assert rc == 0 and json.loads(out)["action"] == "closed"
    rc, out, _ = _main(["--root", str(tmp_path), "double-down", "apply", "0" * 16, "--run", "h"])
    assert rc == 2 and json.loads(out)["action"] == "refused"
    assert eph.asked and set(eph.asked) == {str(eph.state_root)}      # the option reaches the session


def test_hoh_double_down_apply_exit_codes_of_an_attempt_that_measured_nothing_and_of_a_lost_record(
        tmp_path, monkeypatch):
    from hoh.runner import PolicyUnavailable

    eph = _ephemeral(tmp_path, monkeypatch)
    store, d, halt_id = _verified(tmp_path)
    monkeypatch.setattr("hoh.workspace.materialize", Copier())
    monkeypatch.setattr("hoh.runner.run_check", Runner({"R1": PolicyUnavailable("down")}))
    monkeypatch.setattr(ddapply, "apply_fix", Fixer())
    argv = ["--root", str(tmp_path), "double-down", "apply", halt_id, "--run", "h",
            "--storage-root", str(eph.state_root), "--approved-by", "ada", "--reason", "r"]
    assert [_main(argv)[0] for _ in range(3)] == [2, 0, 0]       # one attempt left, the end, quiet
    store2, d2, halt2 = _verified(tmp_path, "g")
    real = doubledown._create
    monkeypatch.setattr(doubledown, "_create", lambda path, record: (_ for _ in ()).throw(
        OSError(28, "full")) if Path(path).name == "apply.json" else real(path, record))
    monkeypatch.setattr("hoh.runner.run_check", Runner({"R1": _after_fix(), "K1": (0, "ok")}))
    rc, out, _ = _main(["--root", str(tmp_path), "double-down", "apply", halt2, "--run", "g",
                        "--storage-root", str(eph.state_root), "--approved-by", "ada", "--reason", "r"])
    assert rc == 1 and json.loads(out)["action"] == "unrecorded"


def test_hoh_double_down_revert_prints_a_row_and_refuses_what_it_cannot_name(tmp_path):
    store, d, halt_id = _verified(tmp_path)
    assert _apply(tmp_path, d)[0]["action"] == "applied"
    argv = ["--root", str(tmp_path), "double-down", "revert", halt_id, "--run", "h"]
    assert _main(argv)[0] == 2                                      # --by and --reason are required
    rc, out, _ = _main([*argv, "--by", "ada", "--reason", "wrong"])
    assert rc == 0 and json.loads(out)["action"] == "reverted"
    rc, out, _ = _main([*argv, "--by", "ada", "--reason", "wrong"])
    assert rc == 0 and json.loads(out)["action"] == "closed"
    rc, out, _ = _main(["--root", str(tmp_path), "double-down", "revert", "0" * 16, "--run", "h",
                        "--by", "ada", "--reason", "x"])
    assert rc == 2 and json.loads(out)["action"] == "refused"


# --- the double-down is on by default, and every opt-out is recorded ---------------------------- #

def _passes(monkeypatch):
    """Record what `hoh resume-quota` hands to the double-down pass instead of running it."""
    seen: list = []
    monkeypatch.setattr(cli, "_double_down_pass", lambda args: seen.append(args) or [])
    monkeypatch.setattr("hoh.resumeloop.resume_runs", lambda root, only=None: [])
    monkeypatch.setattr("hoh.resumeloop.resume_projects", lambda root, drive=False: [])
    return seen


def test_the_continuation_entry_point_turns_the_double_down_on_without_any_flag(
        tmp_path, monkeypatch):
    seen = _passes(monkeypatch)
    parsed = cli.build_parser().parse_args(["resume-quota"])
    assert (parsed.double_down, parsed.verify_proposals, parsed.apply_proposals) == (False, False, False)
    assert (parsed.no_double_down, parsed.no_verify_proposals, parsed.no_apply_proposals) == (
        False, False, False)                                         # nothing was typed ...
    rc, out, _ = _main(["--root", str(tmp_path), "resume-quota"])
    assert rc == 0 and len(seen) == 1                                # ... and the pass ran
    args = seen[0]
    assert (args.double_down, args.verify_proposals, args.apply_proposals) == (True, True, True)
    assert args.analyst_harness is None                              # no provider is spent by default
    assert "double_down" in json.loads(out)
    unit = (Path(__file__).resolve().parents[1] / "ops" / "hoh-continue.service").read_text()
    exec_line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    assert "resume-quota" in exec_line and "double-down" not in exec_line and "--no-" not in exec_line
    for explicit in (["--double-down"], ["--double-down", "--verify-proposals", "--apply-proposals"]):
        seen.clear()
        assert _main(["--root", str(tmp_path), "resume-quota", *explicit])[0] == 0
        assert (seen[0].double_down, seen[0].verify_proposals, seen[0].apply_proposals) == (True, True, True)


@pytest.mark.parametrize("flags, expected, named", [
    (["--no-double-down"], (False, False, False), ["double-down"]),
    (["--no-verify-proposals"], (True, False, False), ["verify-proposals"]),
    (["--no-apply-proposals"], (True, True, False), ["apply-proposals"]),
    (["--no-verify-proposals", "--no-apply-proposals"], (True, False, False),
     ["apply-proposals", "verify-proposals"]),
    (["--no-double-down", "--no-apply-proposals"], (False, False, False),
     ["apply-proposals", "double-down"]),
])
def test_every_opt_out_turns_its_part_off_says_so_and_is_recorded(
        tmp_path, monkeypatch, flags, expected, named):
    seen = _passes(monkeypatch)
    rc, out, err = _main(["--root", str(tmp_path), "resume-quota", *flags])
    assert rc == 0, err
    doc = json.loads(out)
    if expected[0]:
        assert len(seen) == 1 and (seen[0].double_down, seen[0].verify_proposals,
                                   seen[0].apply_proposals) == expected
    else:
        assert seen == [] and "double_down_defects" not in doc            # the pass did not run
        [row] = doc["double_down"]
        assert (row["action"], row["opted_out"], row["record"]) == ("opted_out", named, "recorded")
    rec = [json.loads(ln) for ln in (tmp_path / ".double-down-optouts.jsonl").read_text().splitlines()]
    assert [(r["schema"], r["opted_out"]) for r in rec] == [("hoh-doubledown-optout/1", named)]
    # a unit that opts out every tick writes once, not every tick
    assert _main(["--root", str(tmp_path), "resume-quota", *flags])[0] == 0
    assert len((tmp_path / ".double-down-optouts.jsonl").read_text().splitlines()) == 1
    # and opting back in is recorded, once
    assert _main(["--root", str(tmp_path), "resume-quota"])[0] == 0
    assert _main(["--root", str(tmp_path), "resume-quota"])[0] == 0
    rec = [json.loads(ln) for ln in (tmp_path / ".double-down-optouts.jsonl").read_text().splitlines()]
    assert [r["opted_out"] for r in rec] == [named, []]


def test_nobody_who_never_opted_out_leaves_a_record(tmp_path, monkeypatch):
    _passes(monkeypatch)
    assert _main(["--root", str(tmp_path), "resume-quota"])[0] == 0
    assert not (tmp_path / ".double-down-optouts.jsonl").exists()


@pytest.mark.parametrize("argv, needle", [
    (["--double-down", "--no-double-down"], "contradict"),
    (["--verify-proposals", "--no-verify-proposals"], "contradict"),
    (["--apply-proposals", "--no-apply-proposals"], "contradict"),
    (["--no-double-down", "--analyst-harness", "claude"], "--analyst-*"),
    (["--no-double-down", "--analyst-model", "m"], "--analyst-*"),
    (["--no-double-down", "--analyst-effort", "high"], "--analyst-*"),
    (["--no-double-down", "--verify-proposals"], "--verify-*"),
    (["--no-double-down", "--verify-isolation", "none"], "--verify-*"),
    (["--no-double-down", "--apply-proposals"], "--verify-*"),
    (["--no-verify-proposals", "--verify-isolation", "none"], "--verify-isolation"),
    (["--no-verify-proposals", "--apply-proposals"], "needs the measurement"),
    (["--analyst-timeout", "0"], "--analyst-timeout"),
    (["--analyst-timeout", "3601"], "--analyst-timeout"),
])
def test_switches_that_contradict_each_other_or_need_what_is_off_are_refused(
        tmp_path, monkeypatch, argv, needle):
    seen = _passes(monkeypatch)
    rc, out, err = _main(["--root", str(tmp_path), "resume-quota", *argv])
    assert rc == 2 and needle in err and out == "" and seen == []
    assert not (tmp_path / ".double-down-optouts.jsonl").exists()      # a refused call records nothing


def test_an_opt_out_record_that_cannot_be_kept_is_said_and_the_opt_out_is_honoured(tmp_path):
    path = tmp_path / ".double-down-optouts.jsonl"
    assert doubledown.record_optout(tmp_path, ["double-down"], now=NOW) == "recorded"
    assert doubledown.record_optout(tmp_path, ["double-down"], now=NOW) == "unchanged"
    assert doubledown.record_optout(tmp_path, [], now=NOW) == "recorded"
    assert doubledown.record_optout(tmp_path, [], now=NOW) == "unchanged"
    assert doubledown.record_optout(tmp_path, ["a", "a", "b"], now=NOW) == "recorded"
    assert json.loads(path.read_text().splitlines()[-1])["opted_out"] == ["a", "b"]
    path.write_text("not json\n")
    assert doubledown.record_optout(tmp_path, ["x"], now=NOW).startswith("not_recorded")
    path.write_text(json.dumps({"schema": "other", "opted_out": []}) + "\n")
    assert "not an opt-out record" in doubledown.record_optout(tmp_path, ["x"], now=NOW)
    path.write_text("x" * (doubledown.MAX_OPTOUT_BYTES + 1))
    assert "more than 65536 bytes" in doubledown.record_optout(tmp_path, ["x"], now=NOW)
    path.unlink() if False else None
    other = tmp_path / "other"
    other.mkdir()
    (other / ".double-down-optouts.jsonl").symlink_to(path)
    assert doubledown.record_optout(other, ["x"], now=NOW).startswith("not_recorded")
    missing = tmp_path / "no" / "such"
    assert doubledown.record_optout(missing, ["x"], now=NOW).startswith("not_recorded")


def test_the_pass_still_runs_when_the_opt_out_record_cannot_be_kept(tmp_path, monkeypatch):
    seen = _passes(monkeypatch)
    monkeypatch.setattr(doubledown, "record_optout", lambda root, opted: "not_recorded: full")
    rc, out, _ = _main(["--root", str(tmp_path), "resume-quota", "--no-verify-proposals"])
    assert rc == 0 and len(seen) == 1 and seen[0].verify_proposals is False
    rc, out, _ = _main(["--root", str(tmp_path), "resume-quota", "--no-double-down"])
    assert rc == 0 and json.loads(out)["double_down"][0]["record"] == "not_recorded: full"


def test_the_pass_hands_the_verifier_and_the_applier_only_to_what_is_on(tmp_path, monkeypatch):
    got: list = []
    monkeypatch.setattr(doubledown, "double_down_pass", lambda root, **kw: got.append(kw) or [])
    base = dict(root=str(tmp_path), analyst_harness=None, run_id=None, storage_root=None,
                verify_isolation=None, no_verify_proposals=False, no_apply_proposals=False)
    for verify, apply_, expected in ((True, True, (True, True)), (True, False, (True, False)),
                                     (False, False, (False, False))):
        got.clear()
        cli._double_down_pass(types.SimpleNamespace(**base, verify_proposals=verify,
                                                    apply_proposals=apply_))
        assert (got[0]["verifier"] is not None, got[0]["applier"] is not None) == expected
        assert got[0]["analysts"] is None                            # no harness, no analyst


# --- what a dossier may hold, and what a person is told ----------------------------------------- #

def test_an_odd_apply_file_is_a_refusal_of_the_dossier_never_a_decision(tmp_path):
    store, d, halt_id = _verified(tmp_path)
    assert _apply(tmp_path, d)[0]["action"] == "applied"
    block = doubledown.read_dossier(d).block
    good = doubledown.read_dossier(d)
    assert good.application["attempts"] == len(good.apply_attempts) == 1

    def rewrite(name, **changes):
        rec = json.loads((d / name).read_text())
        rec.update(changes)
        (d / f"{name}.v1").write_text((d / name).read_text())
        (d / name).write_text(json.dumps(doubledown._seal(rec), indent=2, sort_keys=True) + "\n")
        try:
            with pytest.raises(doubledown.DossierError):
                doubledown.read_dossier(d)
        finally:
            (d / name).write_text((d / f"{name}.v1").read_text())
        doubledown.read_dossier(d)                                  # restored: reads again

    rewrite("apply.json", block_digest="0" * 64)
    rewrite("apply.json", verification_digest="0" * 64)
    rewrite("apply.json", status="MAYBE")
    rewrite("apply.json", attempts=2)
    rewrite("apply-attempt-1.json", block_digest="0" * 64)
    rewrite("apply-attempt-1.json", verification_digest="0" * 64)
    rewrite("apply-attempt-1.json", n=2)
    # a gap, an attempt beyond the bound, a record that is no JSON, a link
    (d / "apply-attempt-3.json").write_text((d / "apply-attempt-1.json").read_text())
    with pytest.raises(doubledown.DossierError):
        doubledown.read_dossier(d)
    (d / "apply-attempt-3.json").rename(d / "apply-attempt-2.json")
    with pytest.raises(doubledown.DossierError):
        doubledown.read_dossier(d)                                  # n says 1, the name says 2
    (d / "apply-attempt-2.json").write_text("{")
    with pytest.raises(doubledown.DossierError):
        doubledown.read_dossier(d)
    (d / "apply-attempt-2.json").unlink()
    (d / "apply-attempt-2.json").symlink_to(d / "apply-attempt-1.json")
    with pytest.raises(doubledown.DossierError):
        doubledown.read_dossier(d)
    (d / "apply-attempt-2.json").unlink()
    assert block["halt_id"] == halt_id
    # a reversal needs an application that applied, and answers that very one
    row = ddapply.revert(tmp_path, store.dir, halt_id, by="ada", reason="r", now=NOW)
    assert row["action"] == "reverted"
    rewrite("revert.json", application_digest="0" * 64)
    rewrite("revert.json", block_digest="0" * 64)
    # a withholding belongs to the verified proposal of this block
    store2, d2, halt2 = _verified(tmp_path, "g", contract=False)
    _apply(tmp_path, d2, approved=False)
    rewrite_target = d2 / "apply-withheld.json"
    rec = json.loads(rewrite_target.read_text())
    rewrite_target.write_text(json.dumps(doubledown._seal({**rec, "verification_digest": "0" * 64}),
                                         indent=2, sort_keys=True) + "\n")
    with pytest.raises(doubledown.DossierError):
        doubledown.read_dossier(d2)


def test_a_reversal_of_an_application_that_did_not_apply_is_refused_on_read(tmp_path):
    store, d, halt_id = _verified(tmp_path, "h", contract=False)
    _apply(tmp_path, d, runner=Runner({"R1": (1, "red"), "K1": (0, "ok")}), approved_by="ada",
           approval_reason="x")
    assert _record(d)["status"] == "NOT_APPLIED"
    (d / "revert.json").write_text(json.dumps(doubledown._seal({
        "schema": doubledown.SCHEMA_REVERSAL, "block_digest": doubledown.read_dossier(d).block["digest"],
        "application_digest": _record(d)["digest"], "by": "x", "reason": "y", "reverted_at": "t"})))
    with pytest.raises(doubledown.DossierError, match="reversal"):
        doubledown.read_dossier(d)


def _blocked_node(tmp_path, run_id="h"):
    ProjectStore(tmp_path, "p").write_state(ProjectState(project_id="p", repo_path="/x", nodes=[
        TaskNode(id="n1", lifecycle=Lifecycle.BLOCKED, run_id=run_id, note="x")]))


def test_needs_you_says_what_became_of_a_fix_ahead_of_the_measurement(tmp_path):
    from hoh import missioncontrol

    store, d, halt_id = _verified(tmp_path, contract=_contract(scope=Escalation.PARK_BRANCH_AND_CONTINUE))
    _blocked_node(tmp_path)

    def decision():
        doc = missioncontrol.project_view(str(tmp_path), "p", "needs-you")
        return doc["decisions"][0]["decision"], doc["double_down"][0]
    text, entry = decision()
    assert text.startswith("correction proposal (RED_REPRODUCED, no proof") and "application" not in entry
    _apply(tmp_path, d, approved=False)                              # withheld
    text, entry = decision()
    assert text.startswith("correction proposal (WITHHELD, a person applies it "
                           "(hoh double-down apply --approved-by): RED_REPRODUCED")
    assert entry["application"] == {"status": "WITHHELD", "reason": entry["application"]["reason"]}
    _apply(tmp_path, d, runner=Runner({"R1": (1, "still red"), "K1": (0, "ok")}),
           approved_by="ada", approval_reason="r")
    text, entry = decision()
    assert text.startswith("correction proposal (NOT APPLIED (still_red): RED_REPRODUCED")
    assert entry["application"] == {"status": "NOT_APPLIED", "reason": "still_red", "reversed": False}
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert missioncontrol.run("needs-you", str(tmp_path), project="p", as_json=False) == 0
    assert "correction proposal (NOT APPLIED (still_red)" in buffer.getvalue()


# --- the analysts' side, the numbers, the register row, the firewall -------------------------------- #

def _valid(proposal):
    data = {"perspective": "root_cause", "hypotheses": [_hyp("fix", "supported")],
            "proposal": proposal, "unsolvable": None}
    return doubledown.validate_report(data, "root_cause")


def test_a_proposal_may_carry_a_fix_only_with_a_spec_for_a_repair_spec_and_within_its_bound():
    assert _valid(_proposal())["proposal"]["fix"] == FIX
    assert "fix" not in _valid(_proposal(fix=None))["proposal"]
    assert _valid(_proposal(fix="x" * 32768))["proposal"]["fix"] == "x" * 32768       # exactly the bound
    for bad in ("x" * 32769, "", "  \n", 7, None, ["a"]):
        with pytest.raises(ValueError, match="fix"):
            _valid({**_proposal(), "fix": bad})
    with pytest.raises(ValueError, match="a proposal needs"):
        _valid(_proposal(spec=None))                                 # a fix without a spec
    with pytest.raises(ValueError, match="spec"):
        _valid({**_proposal(), "kind": "plan_note"})                 # a fix belongs to a repair_spec
    with pytest.raises(ValueError, match="a proposal needs"):
        _valid({**_proposal(), "other": 1})


def test_the_analysts_prompt_names_the_fix_the_failing_check_and_the_keep_green_rule():
    block = {"scope": "run", "object_id": "h", "project_id": None, "kind": "", "reason": "r",
             "retry_after": None, "counters": {}, "facts": {}, "tier": "HARD_BLOCKER", "rule": "x"}
    text = doubledown._prompt(block, [], "root_cause", 1)
    for needle in ('"fix"', "unified diff", "starts with K", "same id and same command",
                   "never applied", "no deletion"):
        assert needle in text, needle


def test_the_bounds_and_the_vocabulary_of_the_application_are_the_documented_numbers():
    assert ddapply.KEEP_PREFIX == "K" and ddapply.MAX_FIX_FILES == 16 and ddapply.FIX_TIMEOUT == 60
    assert ddapply.RUN_ID_PREFIX == "dda-"
    assert ddapply.AMENDMENT_PREFIX == "dd-" and ddapply.ACTOR == "hoh double-down"
    assert ddapply.MAX_RECEIPT_FILES == 2000 and not hasattr(ddapply, "SCOPE_KIND")   # `scope` is not asked
    assert not hasattr(ddapply, "APPLY_RETRY_SECONDS") and not hasattr(doubledown, "PASS_APPLY_LIMIT")
    assert doubledown.MAX_APPLY_ATTEMPTS == 2 and doubledown.MAX_APPLIES_PER_RUN == 3
    assert doubledown.MAX_FIX_CHARS == 32768
    assert doubledown.APPLY_STATUSES == {"APPLIED", "NOT_APPLIED"}
    assert doubledown.SCHEMA_APPLICATION == "hoh-doubledown-application/1"
    assert doubledown.SCHEMA_APPLY_ATTEMPT == "hoh-doubledown-apply-attempt/1"
    assert doubledown.SCHEMA_REVERSAL == "hoh-doubledown-reversal/1"
    assert doubledown.SCHEMA_WITHHELD == "hoh-doubledown-apply-withheld/1"
    assert doubledown.OPTOUT_FILE == ".double-down-optouts.jsonl" and doubledown.MAX_OPTOUT_BYTES == 65536


def test_the_register_row_names_the_function_a_person_applies_with_and_keeps_its_history():
    root = Path(__file__).resolve().parents[1]
    register = json.loads((root / "program/v3_3/REQUIREMENTS.json").read_text(encoding="utf-8"))
    [row] = [r for r in register["requirements"] if r["id"] == "P3-42"]
    probe = row["probe"]
    assert (probe["kind"], probe["symbol"], probe["module"]) == ("symbol", "apply", "ddapply.py")
    defined = {n.name for n in ast.walk(ast.parse((root / "src/hoh/ddapply.py").read_text()))
               if isinstance(n, ast.FunctionDef)}
    assert probe["symbol"] in defined
    path, _, name = probe["test"].partition("::")
    mine = {n.name for n in ast.walk(ast.parse(Path(__file__).read_text())) if isinstance(n, ast.FunctionDef)}
    assert path == "tests/test_ddapply.py" and name in mine
    for needle in ("cmd_resume_quota", "_double_down_pass", "double_down_pass", "apply_if_due", "cmd_double_down",
                   "materialize", "run_check", "park_and_amend", "unblock", "dogfood/p3dd3/spec.md"):
        assert needle in probe["note"], needle
    for needle in ("--approved-by", "proven_correction", "WITHHELD", "UNATTENDED_LONG_HORIZON"):
        assert needle in probe["equivalent"], needle
    previous = row["previous_probe"]                                    # kept by addition, three deep
    assert (previous["symbol"], previous["module"], previous["replaced_on"]) == (
        "verify_if_due", "ddverify.py", "2026-10-19")
    assert previous["previous_probe"]["symbol"] == "double_down_pass"
    assert previous["previous_probe"]["previous_probe"]["kind"] == "none"


def test_ddapply_never_calls_what_would_write_a_verdict_a_receipt_or_the_candidate():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "src/hoh/ddapply.py").read_text())
    called = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
              for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for forbidden in ("write_receipt", "write_result", "write_checks", "write_evidence", "accept_checkpoint",
                      "reject_candidate", "freeze", "snapshot", "cancel", "fail", "pause", "block",
                      "begin_iteration", "transition", "rmtree", "remove", "unlink", "rename", "system"):
        assert forbidden not in called, forbidden
    # the only writers of the run: its amendment record, its state, and its specification (through
    # `park_and_amend`, as `hoh amend` does)
    assert {"write_state", "write_amendments", "park_and_amend", "unblock"} <= called
    assert "state.last_accepted_candidate" not in (Path(__file__).resolve().parents[1]
                                                  / "src/hoh/ddapply.py").read_text().replace(
        "state.last_accepted_candidate is not None", "")


def test_git_is_asked_what_it_writes_and_then_to_write_it_with_the_documented_bound_and_a_scratch_home(
        tmp_path, monkeypatch):
    calls: list = []

    def fake(argv, **kw):
        calls.append((argv, kw))
        return types.SimpleNamespace(returncode=0, stdout=b"1\t1\tapp.txt\0" if "--numstat" in argv else b"",
                                     stderr=b"")
    monkeypatch.setattr(subprocess, "run", fake)
    arena, home = tmp_path / "arena", tmp_path / "home"
    arena.mkdir()
    ddapply.apply_fix(arena, FIX, home)
    assert [c[0] for c in calls] == [["git", "apply", "--numstat", "-z", "-"],
                                     ["git", "apply", "--whitespace=nowarn", "-"]]
    for _, seen in calls:
        assert seen["cwd"] == arena and seen["input"] == FIX.encode() and seen["timeout"] == 60
        assert seen["env"]["HOME"] == str(home) and seen["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert seen["env"]["GIT_CONFIG_NOSYSTEM"] == "1" and set(seen["env"]) == {
            "PATH", "HOME", "LC_ALL", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"}


@pytest.mark.parametrize("numstat, returncode", [(b"1\t1\ttests/test_p.py\0", 0), (b"1\t1\tapp.txt\0" b"0\t0\tconftest.py\0", 0),
                                                 (b"", 0), (b"garbage", 0), (b"1\t1\tapp.txt\0", 128)])
def test_files_other_than_the_ones_read_are_never_written(tmp_path, monkeypatch, numstat, returncode):
    """The set the pins were compared against must be the set git writes: when git says anything else, or
    cannot say, `git apply` itself is never run."""
    calls: list = []

    def fake(argv, **kw):
        calls.append(argv)
        return types.SimpleNamespace(returncode=returncode, stdout=numstat, stderr=b"fatal: nope")
    monkeypatch.setattr(subprocess, "run", fake)
    arena = tmp_path / "arena"
    arena.mkdir()
    with pytest.raises(ddapply.FixRefused, match="files git would write"):
        ddapply.apply_fix(arena, FIX, tmp_path)
    assert calls == [["git", "apply", "--numstat", "-z", "-"]]


def test_git_and_this_reading_agree_on_what_a_fix_writes_for_real(tmp_path, processes_allowed):
    both = FIX + NEW_FILE + "--- a/sub/b.txt\n+++ b/sub/b.txt\n@@ -1 +1 @@\n-a\n+b\n"
    out = subprocess.run(["git", "apply", "--numstat", "-z", "-"], input=both.encode(), cwd=tmp_path,
                         capture_output=True, check=True).stdout
    assert ddapply.git_written(out) == ddapply.fix_paths(both) == ["app.txt", "n.py", "sub/b.txt"]
    # the form the review used to evade the pins check: git strips ONE component of ANY name
    evil = "--- x/tests/test_p.py\n+++ x/tests/test_p.py\n@@ -1 +1 @@\n-a\n+b\n"
    out = subprocess.run(["git", "apply", "--numstat", "-z", "-"], input=evil.encode(), cwd=tmp_path,
                         capture_output=True, check=True).stdout
    assert ddapply.git_written(out) == ["tests/test_p.py"]
    with pytest.raises(ddapply.FixRefused, match="does not start with 'a/'"):
        ddapply.fix_paths(evil)


def test_git_that_cannot_run_is_no_verdict_on_the_fix(tmp_path):
    store, d, _ = _verified(tmp_path)

    def no_git(arena, fix, home):
        raise FileNotFoundError(2, "git")
    row, run = _apply(tmp_path, d, fixer=no_git)
    assert (row["action"], row["attempts_left"]) == ("inconclusive", 1) and not (d / "apply.json").exists()
    assert store.read_state().condition is Condition.BLOCKED


def test_the_proof_runs_in_the_regime_the_verification_was_measured_in(tmp_path):
    store = _run(tmp_path, "h")
    doubledown.double_down_pass(tmp_path, analysts=Fake(_proposal()).factory, now=NOW)
    [d] = [p for p in (store.dir / "dd").iterdir() if p.is_dir()]
    row = ddverify.verify(tmp_path, store.dir, d.name, session=_spaces(tmp_path).session, now=NOW,
                          isolation=Isolation.NONE, materialize=Copier(),
                          check_runner=Runner({"R1": (1, "red"), "K1": (0, "ok")}))
    assert row["action"] == "red_reproduced"
    row, run = _apply(tmp_path, d)
    assert row["action"] == "applied" and _record(d)["isolation"] == "none"
    assert {c["isolation"] for c in run.runner.calls} == {Isolation.NONE}
    assert {c["scratch"].parent for c in run.runner.calls} <= {s.parent for s in run.spaces.released}


# --- odd input: every case refuses and leaves the run as it was ------------------------------------ #

def test_a_receipt_that_is_a_fifo_a_directory_or_unreadable_is_no_receipt(tmp_path):
    store = RunStore(tmp_path, "h")
    store.ensure()
    _receipt(store, "h", "R1", "cmd-r1", 1)
    os.mkfifo(store.receipts_dir / "pipe.json")                  # opening it would block for ever
    (store.receipts_dir / "dir.json").mkdir()
    (store.receipts_dir / "dir.json" / "x").write_text("x")
    secret = store.receipts_dir / "closed.json"
    secret.write_text((store.receipts_dir / "h-i1-a1-R1.json").read_text())
    secret.chmod(0)
    try:
        assert ddapply.failing_checks(store.dir, "h") == {"R1": "cmd-r1"}
    finally:
        secret.chmod(0o600)


def test_only_the_first_receipts_of_a_run_are_read_up_to_the_documented_cap(tmp_path, monkeypatch):
    store = RunStore(tmp_path, "h")
    store.ensure()
    for k in range(4):
        _receipt(store, "h", f"C{k}", f"cmd-{k}", 1)
    monkeypatch.setattr(ddapply, "MAX_RECEIPT_FILES", 3)
    assert sorted(ddapply.failing_checks(store.dir, "h")) == ["C0", "C1", "C2"]


def test_text_that_does_not_encode_is_refused_before_anything_is_measured(tmp_path):
    with pytest.raises(ddapply.FixRefused, match="UTF-8"):
        ddapply.fix_paths(FIX + "+\ud800\n")
    store, d, _ = _verified(tmp_path, proposal=_proposal(summary="lone \ud800 surrogate"))
    row, run = _apply(tmp_path, d)
    assert (row["status"], row["reason"]) == ("NOT_APPLIED", "fix_refused") and run.runner.calls == []
    assert Path(store.read_state().spec_path).read_text() == RUN_SPEC


def test_a_specification_that_is_a_link_or_not_utf8_is_never_amended(tmp_path):
    store, d, _ = _verified(tmp_path)
    spec = Path(store.read_state().spec_path)
    real = tmp_path / "real-spec.md"
    real.write_text(RUN_SPEC)
    spec.unlink()
    spec.symlink_to(real)
    row, _ = _apply(tmp_path, d)
    assert (row["status"], row["reason"]) == ("NOT_APPLIED", "fix_refused") and "link" in _record(d)["why"]
    assert real.read_text() == RUN_SPEC and store.read_state().condition is Condition.BLOCKED
    store, d2, _ = _verified(tmp_path, "g")
    spec = Path(store.read_state().spec_path)
    spec.write_bytes(RUN_SPEC.encode().replace(b"hello", b"h\xe9llo"))      # Latin-1, not UTF-8
    row, _ = _apply(tmp_path, d2)
    assert row["action"] == "refused" and "UnicodeDecodeError" in row["why"]
    assert store.read_state().condition is Condition.BLOCKED and spec.read_bytes().count(b"\xe9") == 1
    assert store.read_amendments(origin_digest=text_digest(RUN_SPEC)).amendments == []


def test_a_dossier_reached_through_a_link_is_not_applied(tmp_path):
    store, d, halt_id = _verified(tmp_path)
    alias = tmp_path / "alias-run"
    alias.symlink_to(store.dir)
    row = ddapply.apply(tmp_path, alias, halt_id, session=_spaces(tmp_path).session)
    assert row["action"] == "refused" and "executed" not in row
    row = ddapply.revert(tmp_path, alias, halt_id, by="ada", reason="r")
    assert row["action"] == "refused"
    assert store.read_state().condition is Condition.BLOCKED


def test_a_withholding_that_cannot_be_written_is_said_and_nothing_is_measured(tmp_path, monkeypatch):
    store, d, _ = _verified(tmp_path, contract=False)
    real = doubledown._create
    monkeypatch.setattr(doubledown, "_create", lambda path, record: (_ for _ in ()).throw(
        OSError(28, "full")) if Path(path).name == "apply-withheld.json" else real(path, record))
    for _ in range(3):
        row, run = _apply(tmp_path, d, approved=False)
        assert row["action"] == "withheld" and "NOT written" in row["record"] and "full" in row["record"]
        assert run.runner.calls == [] and "executed" not in row


def test_only_an_upper_case_k_is_a_keep_green_row(tmp_path):
    crit = declared_criteria(_repair_spec("k1 cmd", "K1 cmd", "KEEP cmd"))
    assert ddapply.bound_ids(crit, {"k1": "cmd", "K1": "cmd", "KEEP": "cmd"}) == ["k1"]
    before = _rows(k1="FAIL", K1="PASS")
    assert ddapply.decide_proof(before, _rows(k1="PASS", K1="FAIL"), ["k1"], 2)[:2] == (
        "NOT_PROVEN", "keep_broken")


def test_a_fix_may_not_write_a_file_the_runs_specification_pins(tmp_path):
    pins = "\n```test-pins\n" + "0" * 64 + "  app.txt\nabsent  Extra.txt\n```\n"
    store, d, _ = _verified(tmp_path, spec_text=RUN_SPEC + pins)
    row, run = _apply(tmp_path, d)
    assert (row["status"], row["reason"]) == ("NOT_APPLIED", "fix_touches_pins") and run.runner.calls == []
    assert "app.txt" in _record(d)["why"] and store.read_state().condition is Condition.BLOCKED
    for k, written in enumerate(("extra.txt", "EXTRA.TXT", "Extra.txt", "ExTrA.txt")):   # compared as a case-insensitive file system does
        other = FIX.replace("app.txt", written)
        store, d, _ = _verified(tmp_path, f"g{k}", spec_text=RUN_SPEC + pins, proposal=_proposal(fix=other))
        row, _ = _apply(tmp_path, d)
        assert row["reason"] == "fix_touches_pins" and written in _record(d)["why"], written
    unrelated = FIX.replace("app.txt", "docs.txt")           # a fix that leaves the pinned files alone goes on
    store, d, _ = _verified(tmp_path, "k", spec_text=RUN_SPEC + pins, proposal=_proposal(fix=unrelated))
    row, _ = _apply(tmp_path, d)
    assert row["action"] == "applied" and _record(d)["paths"] == ["docs.txt"]
    broken = "\n```test-pins\nnot a pin line\n```\n"
    store, d, _ = _verified(tmp_path, "m", spec_text=RUN_SPEC + broken)
    row, _ = _apply(tmp_path, d)
    assert (row["reason"], store.read_state().condition) == ("fix_refused", Condition.BLOCKED)
    assert "test-pins" in _record(d)["why"]


def test_the_section_says_which_files_the_fix_writes(tmp_path):
    store, d, _ = _verified(tmp_path)
    assert _apply(tmp_path, d)[0]["action"] == "applied"
    assert "The fix writes app.txt on that copy" in Path(store.read_state().spec_path).read_text()


@pytest.mark.parametrize("name", ["apply.json", "apply-attempt-1.json", "apply-withheld.json", "revert.json"])
@pytest.mark.parametrize("shape", ["fifo", "directory", "mode0", "second_name", "not_an_object", "oversize",
                                   "link"])
def test_an_apply_file_that_is_not_a_plain_record_is_a_refusal_never_a_hang_or_a_decision(
        tmp_path, name, shape):
    store, d, halt_id = _verified(tmp_path)
    odd = d / name
    if shape == "fifo":
        os.mkfifo(odd)                                           # opening it for read would block for ever
    elif shape == "directory":
        odd.mkdir()
    elif shape == "mode0":
        odd.write_text("{}")
        odd.chmod(0)
    elif shape == "second_name":
        odd.write_text("{}")
        os.link(odd, tmp_path / "other-name")
    elif shape == "not_an_object":
        odd.write_text("[1, 2]")
    elif shape == "oversize":
        odd.write_text(" " * (doubledown.MAX_RECORD_BYTES + 1))
    else:
        (tmp_path / "target.json").write_text("{}")
        odd.symlink_to(tmp_path / "target.json")
    def snapshot():
        return {str(p.relative_to(store.dir)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(store.dir.rglob("*")) if p.is_file() and not p.is_symlink() and p != odd}
    try:
        with pytest.raises(doubledown.DossierError):
            doubledown.read_dossier(d)
        before = snapshot()
        row, run = _apply(tmp_path, d)
        assert row["action"] == "refused" and "executed" not in row and run.runner.calls == []
        assert snapshot() == before and store.read_state().condition is Condition.BLOCKED
        assert ddapply.apply_if_due(tmp_path, store.dir, halt_id, session=run.spaces.session, now=NOW) is None
    finally:
        if odd.exists() and not odd.is_symlink() and not odd.is_dir():
            odd.chmod(0o600)


def test_the_options_of_hoh_double_down_apply_are_read_in_both_spellings(tmp_path, monkeypatch):
    seen: list = []
    monkeypatch.setattr(ddapply, "apply", lambda root, owner, halt_id, **kw: seen.append((halt_id, kw)) or {
        "action": "applied"})
    monkeypatch.setattr(cli, "_verification_sessions", lambda args: "session")
    store = _run(tmp_path, "h")
    base = ["--root", str(tmp_path), "double-down", "apply", "0" * 16, "--run", "h"]
    for extra in (["--approved-by", "ada", "--reason", "r r"], ["--approved-by=ada", "--reason=r r"]):
        assert _main([*base, *extra])[0] == 0
        halt_id, kw = seen[-1]
        assert halt_id == "0" * 16 and (kw["approved_by"], kw["approval_reason"]) == ("ada", "r r")
        assert kw["session"] == "session"
    assert _main(base)[0] == 0 and (seen[-1][1]["approved_by"], seen[-1][1]["approval_reason"]) == (None, None)
    assert store.dir.is_dir()


def test_the_controller_adopts_the_text_an_application_wrote_and_a_reversal_restores_the_old_one(tmp_path):
    """The check the controller makes before it applies a run's amendment chain (`_amendment_chain`: the
    chain names this run, every parked predecessor is there and hashes to what it claims) accepts the
    chain `ddapply` wrote, and the digest the controller computes (`contracts.digest`) is the state's."""
    from hoh.contracts import digest
    from hoh.controller import Controller

    store, d, halt_id = _verified(tmp_path)
    assert _apply(tmp_path, d)[0]["action"] == "applied"
    state = store.read_state()
    me = types.SimpleNamespace(store=store)
    chain = Controller._amendment_chain(me, state)
    assert chain is not None and [a.amendment_id for a in chain.amendments] == [f"dd-{halt_id}"]
    text = Path(state.spec_path).read_text()
    assert digest(text) == chain.current_digest() == state.spec_digest != digest(RUN_SPEC)
    assert Path(chain.amendments[0].from_path).read_text() == RUN_SPEC
    assert chain.revalidation_needed() == {"R1"}         # no acceptance until the block's own check is answered again
    assert not any("ignored" in line for line in state.history)
    assert ddapply.revert(tmp_path, store.dir, halt_id, by="ada", reason="wrong", now=NOW)["action"] == "reverted"
    state = store.read_state()
    chain = Controller._amendment_chain(me, state)
    assert chain is not None and len(chain.amendments) == 2
    assert digest(Path(state.spec_path).read_text()) == chain.current_digest() == state.spec_digest == digest(RUN_SPEC)
    assert not any("ignored" in line for line in state.history)


# --- revision 1: what the review of the draft reproduced --------------------------------------------- #

def test_a_fix_that_writes_a_pinned_test_through_an_unusual_name_is_refused_before_anything_is_measured(tmp_path):
    """git strips ONE leading component of ANY name: `x/tests/test_p.py` writes `tests/test_p.py`. The names are read
    the way git reads them (only `a/`, `b/`), a header without ---/+++ is refused, and the path set is
    cross-checked against `git apply --numstat -z`."""
    pins = "\n```test-pins\n" + "0" * 64 + "  app.txt\n```\n"
    for k, evil in enumerate((FIX.replace("a/app.txt", "x/app.txt").replace("b/app.txt", "x/app.txt"),
                              FIX.replace("--- a/app.txt", "--- app.txt").replace("+++ b/app.txt", "+++ app.txt"),
                              "diff --git a/conftest.py b/conftest.py\nnew file mode 100644\n")):
        store, d, _ = _verified(tmp_path, f"e{k}", spec_text=RUN_SPEC + pins, proposal=_proposal(fix=evil))
        row, run = _apply(tmp_path, d)
        assert (row["status"], row["reason"]) == ("NOT_APPLIED", "fix_refused"), evil
        assert run.runner.calls == [] and run.fixer.calls == [] and store.read_state().condition is Condition.BLOCKED


def test_the_revert_refuses_a_parked_text_that_does_not_hash_to_what_the_amendment_claims(tmp_path):
    store, d, halt_id = _verified(tmp_path)
    assert _apply(tmp_path, d)[0]["action"] == "applied"
    [a] = store.read_amendments(origin_digest=text_digest(RUN_SPEC)).amendments
    Path(a.from_path).write_text(RUN_SPEC + "tampered\n")        # the parked copy is no longer the text it claims
    spec_before = Path(store.read_state().spec_path).read_text()
    row = ddapply.revert(tmp_path, store.dir, halt_id, by="ada", reason="r", now=NOW)
    assert row["action"] == "refused" and "does not hash" in row["why"]
    assert Path(store.read_state().spec_path).read_text() == spec_before and not (d / "revert.json").exists()


@pytest.mark.parametrize("meanwhile, reason", [("spent", "run_moved"), ("held", "run_moved"), ("flipped", "run_moved")])
def test_a_run_held_or_spent_while_the_fix_was_proven_is_not_changed_under_the_lock(tmp_path, meanwhile, reason):
    store, d, halt_id = _verified(tmp_path)

    def meanwhile_the_run(arena):
        state = store.read_state()
        if meanwhile == "spent":
            state.budgets = Budgets(max_iterations=1)
            state.usage.iterations = 1
        elif meanwhile == "held":
            state.blocked_kind = stages.CAPABILITY_VIOLATION
        else:
            state.condition = Condition.ACTIVE
        store.write_state(state)
        return (0, "ok")
    row, _ = _apply(tmp_path, d, runner=Runner({"R1": meanwhile_the_run, "K1": (0, "ok")}))
    assert (row["status"], row["reason"]) == ("NOT_APPLIED", reason)
    assert Path(store.read_state().spec_path).read_text() == RUN_SPEC
    assert store.read_amendments(origin_digest=text_digest(RUN_SPEC)).amendments == []


def test_the_lease_is_asked_again_under_the_runs_lock_before_the_specification_is_written(tmp_path, monkeypatch):
    from hoh import rundrive

    store, d, _ = _verified(tmp_path)
    asked: list = []

    def lease(root, run_id, **kw):
        asked.append(run_id)
        return None if len(asked) == 1 else f"Run {run_id} is already being executed (a driver took it meanwhile)"
    monkeypatch.setattr(rundrive, "refusal_for_resume", lease)
    row, run = _apply(tmp_path, d)
    assert asked == ["h", "h"] and row["action"] == "refused" and "driver took it" in row["why"] and row["executed"]
    assert Path(store.read_state().spec_path).read_text() == RUN_SPEC and store.read_state().condition is Condition.BLOCKED
    assert store.read_amendments(origin_digest=text_digest(RUN_SPEC)).amendments == [] and not (d / "apply.json").exists()


def test_only_the_block_of_a_run_can_be_applied():
    node = types.SimpleNamespace(
        verification={"status": "RED_REPRODUCED"}, outcome={"proposal": {"kind": "repair_spec", "spec": "s", "fix": "f"}},
        block={"scope": "node"})
    assert "only the block of a run" in ddapply.appliable(node)
    node.block = {"scope": "run"}
    assert ddapply.appliable(node) == ""
    node.verification = {"status": "REFUTED"}
    assert "REFUTED" in ddapply.appliable(node)
    node.verification = None
    assert "no verification" in ddapply.appliable(node)


def test_the_default_resume_quota_adds_only_a_dossier_to_a_blocked_run(tmp_path):
    """D12 moved `test_dispatch_recovery` to `--no-double-down`; this pins what the DEFAULT command does to a run: it adds
    `dd/` and nothing else -- every file the run had is byte-identical, no opt-out record is written."""
    store = _run(tmp_path, "h")
    before = _bytes(store.dir)
    rc, out, err = _main(["--root", str(tmp_path), "resume-quota"])
    assert rc == 0, err
    after = _bytes(store.dir)
    # (`.lock` is the run lock the resume loop itself takes, with or without the double-down)
    assert {k: v for k, v in after.items() if not k.startswith("dd/") and k != ".lock"} == before
    assert sorted({k.split("/")[0] for k in after} - {k.split("/")[0] for k in before} - {".lock"}) == ["dd"]
    assert not (tmp_path / ".double-down-optouts.jsonl").exists()
    assert store.read_state().condition is Condition.BLOCKED
    rc, out, _ = _main(["--root", str(tmp_path), "resume-quota"])        # and the next tick adds nothing
    # (`.lock` again: it says `since=<second>`, so a tick that crosses a second boundary rewrites it)
    assert rc == 0 and {k: v for k, v in _bytes(store.dir).items() if k != ".lock"} == {
        k: v for k, v in after.items() if k != ".lock"}
