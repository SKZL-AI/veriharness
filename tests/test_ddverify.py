"""The verification of a correction proposal (V3.3 P3-42, slice 2): `hoh.ddverify`,
`hoh double-down verify`, `hoh resume-quota --double-down --verify-proposals`.

A `repair_spec` proposal that carries its spec is measured the way the product's own QA measures
a plan's criteria, on a scratch copy of the run's candidate: the spec's one `criteria` block is
read, the candidate is materialised in a registered temporary workspace, every criterion runs
through `runner.run_check` and its receipt decides. The verdict is a create-only `verify.json`
bound to the block, the outcome and the proposal. RED_REPRODUCED means a criterion is red on the
unrepaired candidate for a reason that is not its own absence; it does not mean the repair works.

Most tests drive the module with a scripted runner and scratch workspaces made in `tmp_path`
(no process); the tests that need one ask for `processes_allowed` and use a real git repository,
the real `workspace.materialize`, the real `runner.run_check` (isolation NONE: the sandbox cannot
nest inside the sandbox the product's own acceptance runs in) and the real `EphemeralRoots`.
The bounds and the statuses are written out as literals on purpose: they are the contract.
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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hoh import cli, ddverify, doubledown
from hoh.contracts import Budgets, Candidate, Condition, Receipt, RunState, Usage
from hoh.project import Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.runner import ArenaEscape, HouseRuleViolation, PolicyUnavailable
from hoh.sandbox import Isolation
from hoh.store import RunStore

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


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

SPEC = "# repair\n\n```criteria\nR1 python3 -m pytest -q tests/test_it.py\n```\n"


def _candidate(repo: Path, cid="c1", tree="b" * 40) -> Candidate:
    return Candidate(candidate_id=cid, repo_path=str(repo), commit="a" * 40, tree_clean=True,
                     tree_digest=tree)


def _run(root, rid, condition=Condition.BLOCKED, **kw) -> RunStore:
    store = RunStore(root, rid)
    kw.setdefault("blocked_reason", "stuck")
    store.write_state(RunState(run_id=rid, repo_path="/x", project_name="p", spec_path="/x/s.md",
                               spec_digest="a" * 16, policy_digest="b" * 16,
                               profile_digest="c" * 16, condition=condition, **kw))
    return store


def _hyp(path, outcome="open", evidence=("saw it",)):
    return {"path_id": path, "statement": "s", "tested": True, "outcome": outcome,
            "evidence": list(evidence)}


def _report(perspective, hyps, proposal=None, unsolvable=None):
    return json.dumps({"perspective": perspective, "hypotheses": hyps, "proposal": proposal,
                       "unsolvable": unsolvable})


def _proposal(path="fix", kind="repair_spec", spec=SPEC):
    p = {"path_id": path, "kind": kind, "summary": f"apply {path}", "steps": ["do it"]}
    if spec is not None:
        p["spec"] = spec
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


def _proposed(tmp_path, spec=SPEC, rid="h", repo=None, kind="repair_spec", **kw):
    """A blocked run whose dossier ended in a PROPOSAL (made by the real pass and a fake
    analyst); returns `(store, dossier path, halt id)`."""
    repo = repo or tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    kw.setdefault("last_accepted_candidate", _candidate(repo))
    store = _run(tmp_path, rid, **kw)
    rows = doubledown.double_down_pass(tmp_path, analysts=Fake(_proposal(spec=spec, kind=kind)).factory,
                                       now=NOW)
    assert [r["action"] for r in rows].count("proposal") == 1, rows
    [d] = [p for p in (store.dir / "dd").iterdir() if p.is_dir()]
    return store, d, d.name


class Spaces:
    """The scratch workspaces a session hands out, made under `base`, 0700, one per call."""

    def __init__(self, base: Path, refuse: str | None = None):
        self.base, self.refuse = base, refuse
        self.log: list[tuple] = []
        self.blocks: list[dict] = []
        self.live: set[Path] = set()
        self.released: list[Path] = []

    @contextmanager
    def session(self, block):
        if self.refuse == "session":
            raise ddverify.VerifyRefused("nowhere to measure")
        self.blocks.append(block)
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
    """`materialize`: puts a marker in the arena and remembers the candidate it was asked for."""

    def __init__(self):
        self.calls: list[tuple[Candidate, Path]] = []

    def __call__(self, candidate, dest):
        self.calls.append((candidate, Path(dest)))
        os.mkdir(dest)
        (Path(dest) / "app.txt").write_text("hello\n")
        return Path(dest)


class Runner:
    """`check_runner`: `script[check_id]` is `(exit code, output)` or an exception to raise."""

    def __init__(self, script=None, default=(0, "ok")):
        self.script, self.default = script or {}, default
        self.calls: list[dict] = []

    def __call__(self, check, candidate, *, run_id, iteration, attempt, cwd, timeout,
                 isolation, scratch):
        self.calls.append(dict(check=check, candidate=candidate, run_id=run_id,
                               iteration=iteration, attempt=attempt, cwd=Path(cwd),
                               timeout=timeout, isolation=isolation, scratch=Path(scratch),
                               scratch_entries=sorted(os.listdir(scratch))))
        value = self.script.get(check.check_id, self.default)
        if isinstance(value, BaseException):
            raise value
        code, text = value
        log = f"$ {check.command}\n--- output ---\n{text}"
        receipt = Receipt(
            receipt_id=f"{run_id}-i{iteration}-a{attempt}-{check.check_id}", run_id=run_id,
            iteration=iteration, attempt=attempt, check_id=check.check_id,
            candidate_binding=candidate.binding(), command=check.command, exit_code=code,
            started_at="2026-10-14T12:00:00Z", ended_at="2026-10-14T12:00:01Z",
            stdout_digest=hashlib.sha256(log.encode()).hexdigest(), runner_identity="fake",
            runner_ok=code not in (124, 126, 127))
        return receipt, log


_SPACES = itertools.count()


def _spec(*lines):
    return "# repair\n\n```criteria\n" + "\n".join(lines) + "\n```\n"


def _verify(tmp_path, d, spaces=None, runner=None, copier=None, *, isolation=Isolation.STRICT,
            now=NOW, root=None):
    spaces = spaces or Spaces(tmp_path.parent / f"{tmp_path.name}-spaces-{next(_SPACES)}")
    spaces.base.mkdir(exist_ok=True)
    runner = runner or Runner()
    copier = copier or Copier()
    row = ddverify.verify(root or tmp_path, d.parent.parent, d.name, session=spaces.session,
                          isolation=isolation, now=now, materialize=copier, check_runner=runner)
    return row, spaces, runner, copier


def _record(d, name="verify.json"):
    return json.loads((d / name).read_text())


def _bytes(path: Path) -> dict:
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob("*")) if p.is_file()}


def _state_bytes(root) -> dict:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob("*")) if p.is_file() and p.name in
            ("state.json", "project.json")}


# --- the register row ----------------------------------------------------------------------- #

def test_the_register_row_names_the_function_the_pass_calls_and_keeps_its_equivalent_and_history():
    root = Path(__file__).resolve().parents[1]
    register = json.loads((root / "program/v3_3/REQUIREMENTS.json").read_text(encoding="utf-8"))
    [row] = [r for r in register["requirements"] if r["id"] == "P3-42"]
    probe = row["previous_probe"]               # slice 3 (P3-DD3) replaced this probe, by addition
    assert (probe["kind"], probe["symbol"], probe["module"]) == ("symbol", "verify_if_due",
                                                                "ddverify.py")
    defined = {n.name for n in ast.walk(ast.parse((root / "src/hoh/ddverify.py").read_text()))
               if isinstance(n, ast.FunctionDef)}
    assert probe["symbol"] in defined
    path, _, name = probe["test"].partition("::")
    mine = {n.name for n in ast.walk(ast.parse(Path(__file__).read_text()))
            if isinstance(n, ast.FunctionDef)}
    assert path == "tests/test_ddverify.py" and name in mine         # a node that is collected here
    # PARTIAL, never PROVEN (D1, no false PROVEN): the equivalent says what is still missing
    for needle in ("--double-down", "--analyst-harness", "--verify-proposals", "double-down verify",
                   "double-down renew", "installed continuation unit", "nothing applies"):
        assert needle in probe["equivalent"], needle
    for needle in ("cmd_resume_quota", "_double_down_pass", "double_down_pass", "verify_if_due",
                   "materialize", "run_check", "cmd_double_down", "renew_bound",
                   "dogfood/p3dd2/spec.md"):
        assert needle in probe["note"], needle
    previous = probe["previous_probe"]                                # kept by addition, two deep
    assert (previous["symbol"], previous["module"], previous["replaced_on"]) == (
        "double_down_pass", "doubledown.py", "2026-10-18")
    assert previous["test"].endswith("::test_resume_quota_double_down_classifies_opens_and_advances"
                                     "_through_the_cli") and previous["equivalent"]
    assert previous["previous_probe"]["kind"] == "none"
    assert previous["previous_probe"]["replaced_on"] == "2026-10-14"


# --- the numbers and the vocabulary --------------------------------------------------------- #

def test_the_bounds_and_the_vocabulary_of_the_verification_are_the_documented_numbers():
    assert ddverify.MAX_VERIFY_ATTEMPTS == 3
    assert ddverify.MAX_CRITERIA == 8
    assert ddverify.CRITERION_TIMEOUT == 300
    assert ddverify.ARENA_ESTIMATE == 512 << 20
    assert ddverify.CHECK_ESTIMATE == 64 << 20
    assert ddverify.TAIL_CHARS == 2000
    assert ddverify.RUN_ID_PREFIX == "ddv-"
    assert ddverify.VERIFY_RETRY_SECONDS == 600
    assert ddverify.ARTEFACTUAL_EXITS == {4, 5}
    assert doubledown.PASS_VERIFY_LIMIT == 2
    assert doubledown.MAX_SPEC_CHARS == 16384
    assert doubledown.VERIFY_STATUSES == {"RED_REPRODUCED", "REFUTED", "INCONCLUSIVE"}
    assert doubledown.SCHEMA_VERIFICATION == "hoh-doubledown-verification/1"
    assert doubledown.SCHEMA_ATTEMPT == "hoh-doubledown-verify-attempt/1"
    assert doubledown.SCHEMA_RESULT == "hoh-doubledown-verify-result/1"


# --- the analyst's side: a proposal may carry a spec -------------------------------------------- #

def _valid(proposal):
    data = {"perspective": "root_cause", "hypotheses": [_hyp("fix", "supported")],
            "proposal": proposal, "unsolvable": None}
    return doubledown.validate_report(data, "root_cause")


def test_a_proposal_may_carry_a_spec_only_for_a_repair_spec_and_within_its_bound():
    assert _valid(_proposal())["proposal"]["spec"] == SPEC
    assert "spec" not in _valid(_proposal(spec=None))["proposal"]
    assert _valid(_proposal(spec="x" * 16384))["proposal"]["spec"] == "x" * 16384   # exactly the bound
    for bad in ("x" * 16385, "", "   \n", 7, None, ["a"]):
        with pytest.raises(ValueError, match="spec"):
            _valid({**_proposal(spec=None), "spec": bad})
    for kind in ("plan_note", "unblock_resolution"):
        with pytest.raises(ValueError, match="spec"):
            _valid(_proposal(kind=kind))                       # a spec belongs to a repair_spec
    extra = {**_proposal(spec=None), "other": 1}
    with pytest.raises(ValueError, match="a proposal needs"):
        _valid(extra)                                           # no other key is accepted


def test_the_analysts_prompt_asks_for_the_spec_and_says_what_counts():
    block = {"scope": "run", "object_id": "h", "project_id": None, "kind": "", "reason": "r",
             "retry_after": None, "counters": {}, "facts": {}, "tier": "HARD_BLOCKER", "rule": "x"}
    text = doubledown._prompt(block, [], "root_cause", 1)
    for needle in ("criteria", "scratch copy", "FAILS there", "its own file is not there yet",
                   '"spec"'):
        assert needle in text, needle


# --- the verdicts ----------------------------------------------------------------------------- #

def test_a_repair_spec_with_a_failing_criterion_is_red_reproduced(tmp_path):
    spec = _spec("R1 test -f app.txt", "R2 test -f gone.txt")
    store, d, halt_id = _proposed(tmp_path, spec)
    runner = Runner({"R1": (0, "ok"), "R2": (1, "missing gone.txt")})
    row, spaces, runner, copier = _verify(tmp_path, d, runner=runner)
    assert (row["action"], row["status"], row["reason"], row["executed"]) == (
        "red_reproduced", "RED_REPRODUCED", "red_found", True)
    rec = _record(d)
    assert (rec["schema"], rec["status"], rec["reason"], rec["attempts"], rec["isolation"]) == (
        "hoh-doubledown-verification/1", "RED_REPRODUCED", "red_found", 1, "strict")
    dossier = doubledown.read_dossier(d)
    assert rec["block_digest"] == dossier.block["digest"]
    assert rec["outcome_digest"] == dossier.outcome["digest"]
    assert rec["proposal_digest"] == hashlib.sha256(
        doubledown._canon(dossier.outcome["proposal"])).hexdigest()
    assert rec["spec_sha256"] == hashlib.sha256(spec.encode()).hexdigest()
    assert [(c["id"], c["outcome"], c["exit_code"], c["artefactual"]) for c in rec["criteria"]] == [
        ("R1", "PASS", 0, False), ("R2", "FAIL", 1, False)]
    assert rec["criteria"][1]["receipt_id"] == f"ddv-{halt_id}-i1-a1-R2"
    assert "missing gone.txt" in rec["criteria"][1]["tail"]
    assert rec["says"] == ("a criterion failed on the unrepaired candidate; this is neither a proof that "
                           "the repair works nor a proof that the red is the block's")
    assert rec["block"] == {"kind": "", "reason": "stuck"}            # what the red is to be compared with
    assert [c["signature_seen"] for c in rec["criteria"]] == [False, False]
    assert rec["candidate"]["source"] == "last_accepted" and rec["candidate"]["id"] == "c1"
    assert rec["candidate"]["binding"] == _candidate(tmp_path / "repo").binding()
    assert "repo_path" not in rec["candidate"] and str(tmp_path) not in json.dumps(rec)
    assert doubledown.read_dossier(d).verification["status"] == "RED_REPRODUCED"
    assert _record(d, "verify-attempt-1.json")["spec_sha256"] == rec["spec_sha256"]
    assert doubledown.read_dossier(d).outcome["status"] == "UNVERIFIED"    # the proposal is as it was


def test_a_spec_whose_criteria_are_all_green_is_refuted(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec("R1 true", "R2 true"))
    row, *_ = _verify(tmp_path, d)
    assert (row["action"], row["status"], row["reason"]) == ("refuted", "REFUTED", "nothing_red")
    assert _record(d)["why"].startswith("every criterion is already green")


def test_a_red_that_is_only_its_own_absence_does_not_verify(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec("R1 a", "R2 b"))
    row, *_ = _verify(tmp_path, d, runner=Runner(
        {"R1": (5, "no tests ran"), "R2": (1, "boom")}, default=(5, "no tests ran")))
    assert row["status"] == "RED_REPRODUCED"                         # one genuine red is enough
    rec = _record(d)
    assert [c["artefactual"] for c in rec["criteria"]] == [True, False]


def test_every_red_being_artefactual_refutes_the_spec(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec("R1 a", "R2 b"))
    row, *_ = _verify(tmp_path, d, runner=Runner(default=(5, "collected 0 items")))
    assert (row["status"], row["reason"]) == ("REFUTED", "only_artefactual")
    assert all(c["artefactual"] for c in _record(d)["criteria"])


@pytest.mark.parametrize("spec", [
    "no criteria block at all",
    "```criteria\n```\n",
    "```criteria\n\n```\n",
    "```criteria\nR1 a\n```\n```criteria\nR2 b\n```\n",
    "```criteria\nR1\n```\n",
    "```criteria\nR1 a\nR1 b\n```\n",
    "```criteria\nR1 a\n",
    "```criteria\nR1 a\x00b\n```\n",
    "```criteria\nR1 a\ud800\n```\n",
    "```criteria\nR1 a\x1b[0m\n```\n",
    "```criteria\n R1 a\n```\n```criteria\nR2 b\n```\n",
    _spec(*[f"R{k} true" for k in range(1, 10)]),
])
def test_a_spec_the_product_cannot_read_is_refuted_without_measuring_anything(tmp_path, spec):
    store, d, _ = _proposed(tmp_path, spec)
    row, spaces, runner, copier = _verify(tmp_path, d)
    assert (row["action"], row["status"], row["reason"]) == ("refuted", "REFUTED", "spec_refused")
    assert runner.calls == [] and copier.calls == [] and spaces.log == []
    rec = _record(d)
    assert rec["attempts"] == 0 and rec["criteria"] == [] and rec["candidate"] is None
    assert not any(n.startswith("verify-attempt") for n in os.listdir(d))


def test_eight_criteria_are_measured_and_the_ninth_is_not(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec(*[f"R{k} true" for k in range(1, 9)]))
    row, spaces, runner, _ = _verify(tmp_path, d)
    assert row["status"] == "REFUTED" and row["reason"] == "nothing_red"     # measured, all green
    assert [c["check"].check_id for c in runner.calls] == [f"R{k}" for k in range(1, 9)]


def test_a_criterion_the_runners_guards_refuse_refutes_the_spec_and_ends_the_attempt(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec("R1 a", "R2 b", "R3 c"))
    runner = Runner({"R2": HouseRuleViolation("a house rule"), "R3": (1, "never reached")})
    row, *_ = _verify(tmp_path, d, runner=runner)
    assert (row["status"], row["reason"]) == ("REFUTED", "criterion_refused")
    assert [c["check"].check_id for c in runner.calls] == ["R1", "R2"]       # R3 never ran
    assert [c["outcome"] for c in _record(d)["criteria"]] == ["PASS", "REFUSED"]
    store, d2, _ = _proposed(tmp_path, _spec("R1 a"), rid="g")
    row, *_ = _verify(tmp_path, d2, runner=Runner({"R1": ArenaEscape("outside the arena")}))
    assert row["reason"] == "criterion_refused"


def test_the_decision_of_an_attempt_is_a_table():
    def r(outcome, artefactual=False):
        return {"outcome": outcome, "artefactual": artefactual}
    decide = ddverify.decide
    assert decide([r("PASS"), r("FAIL")], 2)[:2] == ("RED_REPRODUCED", "red_found")
    assert decide([r("FAIL", True), r("FAIL")], 2)[:2] == ("RED_REPRODUCED", "red_found")
    assert decide([r("FAIL", True)], 1)[:2] == ("REFUTED", "only_artefactual")
    assert decide([r("PASS"), r("PASS")], 2)[:2] == ("REFUTED", "nothing_red")
    assert decide([r("PASS"), r("INCONCLUSIVE")], 2)[:2] == ("INCONCLUSIVE", "not_measured")
    assert decide([r("FAIL"), r("INCONCLUSIVE")], 2)[:2] == ("INCONCLUSIVE", "not_measured")
    assert decide([r("FAIL")], 2)[:2] == ("INCONCLUSIVE", "not_measured")     # one did not run
    assert decide([r("INCONCLUSIVE"), r("REFUSED")], 2)[:2] == ("REFUTED", "criterion_refused")
    assert decide([r("PASS"), r("REFUSED")], 2)[:2] == ("REFUTED", "criterion_refused")


# --- termination: bounded attempts, a terminal record, quiet afterwards ------------------------ #

def _failing_runner():
    return Runner({"R1": PolicyUnavailable("no sandbox backend")})


def test_a_measurement_that_did_not_come_about_is_no_verdict_and_is_tried_at_most_three_times(
        tmp_path):
    store, d, halt_id = _proposed(tmp_path, _spec("R1 a"))
    runner, spaces = _failing_runner(), Spaces(tmp_path.parent / f"{tmp_path.name}-spaces")
    rows = [_verify(tmp_path, d, spaces=spaces, runner=runner)[0] for _ in range(3)]
    assert [(r["action"], r["status"], r.get("attempts_left")) for r in rows] == [
        ("inconclusive", "INCONCLUSIVE", 2), ("inconclusive", "INCONCLUSIVE", 1),
        ("inconclusive", "INCONCLUSIVE", None)]
    assert [c["attempt"] for c in runner.calls] == [1, 2, 3]
    assert sorted(n for n in os.listdir(d) if n.startswith("verify")) == [
        "verify-attempt-1.json", "verify-attempt-2.json", "verify-attempt-3.json",
        "verify-result-1.json", "verify-result-2.json", "verify.json"]
    rec = _record(d)
    assert (rec["status"], rec["reason"], rec["attempts"]) == ("INCONCLUSIVE", "attempts_spent", 3)
    assert "no sandbox backend" in _record(d, "verify-result-1.json")["reasons"][0]
    kept = _bytes(d)
    again, *_ = _verify(tmp_path, d, spaces=spaces, runner=runner)
    assert again["action"] == "closed" and again["status"] == "INCONCLUSIVE"
    assert len(runner.calls) == 3 and _bytes(d) == kept        # the fourth call measured nothing
    assert doubledown.read_dossier(d).outcome["outcome"] == "PROPOSAL"      # nothing was decided


def test_a_crash_inside_an_attempt_still_counts(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec("R1 a"))
    runner = Runner({"R1": KeyboardInterrupt()})
    spaces = Spaces(tmp_path.parent / f"{tmp_path.name}-spaces")
    for k in range(3):
        with pytest.raises(KeyboardInterrupt):
            _verify(tmp_path, d, spaces=spaces, runner=runner)
        assert len(doubledown.read_dossier(d).attempts) == k + 1       # recorded before it measured
    ok = Runner()
    row, *_ = _verify(tmp_path, d, spaces=spaces, runner=ok)
    assert ok.calls == []                                          # no attempt is left to measure
    assert (row["action"], row["status"]) == ("inconclusive", "INCONCLUSIVE")
    assert _record(d)["reason"] == "attempts_spent" and "no result recorded" in _record(d)["why"]


def test_a_runner_that_reports_an_infrastructure_error_is_no_verdict(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec("R1 a", "R2 b"))
    row, *_ = _verify(tmp_path, d, runner=Runner({"R2": (124, "timed out")}))
    assert (row["action"], row["status"]) == ("inconclusive", "INCONCLUSIVE")
    assert not (d / "verify.json").exists() and (d / "verify-result-1.json").exists()
    assert [c["outcome"] for c in _record(d, "verify-result-1.json")["criteria"]] == [
        "PASS", "INCONCLUSIVE"]


def test_an_os_error_of_the_runner_is_no_verdict_and_the_criteria_so_far_are_kept(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec("R1 a", "R2 b"))
    row, *_ = _verify(tmp_path, d, runner=Runner({"R2": OSError(28, "No space left on device")}))
    assert (row["action"], row["status"]) == ("inconclusive", "INCONCLUSIVE")
    rows = _record(d, "verify-result-1.json")["criteria"]
    assert [(c["id"], c["outcome"]) for c in rows] == [("R1", "PASS"), ("R2", "INCONCLUSIVE")]
    assert rows[1]["why"].startswith("OSError") and "No space left" in rows[1]["why"]


def test_a_workspace_or_a_materialisation_that_fails_is_no_verdict(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec("R1 a"))
    spaces = Spaces(tmp_path.parent / f"{tmp_path.name}-spaces", refuse="check")
    row, _, runner, _ = _verify(tmp_path, d, spaces=spaces)
    assert row["action"] == "inconclusive" and runner.calls == []
    broken = Copier()
    broken.__class__ = type("Broken", (Copier,), {"__call__": lambda self, c, dest: 1 / 0})
    row, _, runner, _ = _verify(tmp_path, d, copier=broken)
    assert row["action"] == "inconclusive" and runner.calls == []
    assert len(doubledown.read_dossier(d).attempts) == 2


def test_a_verdict_that_cannot_be_written_is_said_and_the_attempt_stays_counted(
        tmp_path, monkeypatch):
    store, d, _ = _proposed(tmp_path, _spec("R1 a"))
    real = doubledown._create

    def failing(path, record):
        if Path(path).name == "verify.json":
            raise OSError(28, "No space left on device")
        return real(path, record)
    monkeypatch.setattr(doubledown, "_create", failing)
    row, *_ = _verify(tmp_path, d, runner=Runner({"R1": (1, "red")}))
    assert (row["action"], row["status"]) == ("unrecorded", "RED_REPRODUCED")
    assert "No space left" in row["why"] and not (d / "verify.json").exists()
    assert len(doubledown.read_dossier(d).attempts) == 1
    monkeypatch.setattr(doubledown, "_create", real)
    row, *_ = _verify(tmp_path, d, runner=Runner({"R1": (1, "red")}))
    assert row["action"] == "red_reproduced" and _record(d)["attempts"] == 2


def test_an_attempt_that_cannot_be_recorded_measures_nothing(tmp_path, monkeypatch):
    store, d, _ = _proposed(tmp_path, _spec("R1 a"))
    real = doubledown._create
    monkeypatch.setattr(doubledown, "_create", lambda path, record: (_ for _ in ()).throw(
        OSError(28, "No space left on device")) if Path(path).name.startswith("verify-attempt")
        else real(path, record))
    runner = Runner({"R1": (1, "red")})
    before = _bytes(d)
    for _ in range(5):                                            # said every time, never measured
        row, spaces, runner, copier = _verify(tmp_path, d, runner=runner)
        assert row["action"] == "refused" and "No space left" in row["why"]
        assert "executed" not in row and copier.calls == []
    assert runner.calls == [] and _bytes(d) == before


def test_with_no_attempt_left_and_a_terminal_record_that_cannot_be_written_nothing_is_measured(
        tmp_path, monkeypatch):
    store, d, _ = _proposed(tmp_path, _spec("R1 a"))
    runner, spaces = _failing_runner(), Spaces(tmp_path.parent / f"{tmp_path.name}-spaces")
    real = doubledown._create
    monkeypatch.setattr(doubledown, "_create", lambda path, record: (_ for _ in ()).throw(
        OSError(28, "full")) if Path(path).name in ("verify.json", "verify-result-3.json")
        else real(path, record))
    rows = [_verify(tmp_path, d, spaces=spaces, runner=runner)[0] for _ in range(3)]
    assert [r["action"] for r in rows] == ["inconclusive", "inconclusive", "unrecorded"]
    calls = len(runner.calls)
    for _ in range(3):
        row, *_ = _verify(tmp_path, d, spaces=spaces, runner=runner)
        assert row["action"] == "unrecorded"                    # said every time, measured never
    assert len(runner.calls) == calls == 3


def test_a_terminal_verification_is_quiet_for_another_call_and_for_the_pass(tmp_path):
    store, d, halt_id = _proposed(tmp_path, _spec("R1 a"))
    spaces = Spaces(tmp_path.parent / f"{tmp_path.name}-spaces")
    runner = Runner({"R1": (1, "red")})
    _verify(tmp_path, d, spaces=spaces, runner=runner)
    kept, state = _bytes(d), _state_bytes(tmp_path)
    for _ in range(2):
        row, *_ = _verify(tmp_path, d, spaces=spaces, runner=runner)
        assert row["action"] == "closed" and row["status"] == "RED_REPRODUCED" and "executed" not in row
    verifier = lambda owner, hid, now: ddverify.verify_if_due(   # noqa: E731
        tmp_path, owner, hid, session=spaces.session, now=now, materialize=Copier(),
        check_runner=runner)
    for k in range(2):
        rows = doubledown.double_down_pass(tmp_path, verifier=verifier, now=NOW + timedelta(k))
        assert [r["action"] for r in rows] == ["closed"]        # the dossier only; no verification row
    assert len(runner.calls) == 1 and _bytes(d) == kept and _state_bytes(tmp_path) == state


# --- what can and cannot be verified -------------------------------------------------------------- #

def test_only_a_repair_spec_proposal_with_a_spec_is_verified_and_nothing_is_written_otherwise(
        tmp_path):
    trivial = _run(tmp_path, "t", budgets=Budgets(max_iterations=1), usage=Usage(iterations=1))
    doubledown.double_down_pass(tmp_path, now=NOW)            # TRIVIAL: a derived plan_note
    [derived] = [p for p in (trivial.dir / "dd").iterdir()]
    cases = {
        "a repair spec without a spec": (_proposed(tmp_path, None, rid="a")[1], "carries no spec"),
        "a plan note": (_proposed(tmp_path, None, rid="b", kind="plan_note")[1], "against the plan"),
        "an unblock resolution": (_proposed(tmp_path, None, rid="c", kind="unblock_resolution")[1],
                                  "against the plan"),
        "a derived proposal": (derived, "against the plan"),
    }
    for name, (d, why) in cases.items():
        before = _bytes(d)
        row, spaces, runner, copier = _verify(tmp_path, d)
        assert row["action"] == "refused" and why in row["why"], (name, row)
        assert runner.calls == [] and spaces.log == [] and _bytes(d) == before, name


def test_a_dossier_that_has_not_ended_or_ended_otherwise_has_nothing_to_verify(tmp_path):
    store = _run(tmp_path, "h")
    doubledown.double_down_pass(tmp_path, now=NOW)                 # opened, no analyst: no outcome
    [d] = [p for p in (store.dir / "dd").iterdir()]
    row, spaces, runner, _ = _verify(tmp_path, d)
    assert row["action"] == "refused" and "no outcome" in row["why"] and runner.calls == []
    human = _run(tmp_path, "c", blocked_kind="human")
    doubledown.double_down_pass(tmp_path, now=NOW)
    [h] = [p for p in (human.dir / "dd").iterdir()]
    row, *_ = _verify(tmp_path, h)
    assert row["action"] == "refused" and "HUMAN_ONLY" in row["why"]


def test_no_registered_workspace_means_nothing_is_measured_recorded_or_counted(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec("R1 a"))
    before = _bytes(d)
    for refuse in ("session", "verification"):
        spaces = Spaces(tmp_path.parent / f"{tmp_path.name}-spaces", refuse=refuse)
        row, _, runner, _ = _verify(tmp_path, d, spaces=spaces)
        assert row["action"] == "refused" and runner.calls == []
        assert "executed" not in row and _bytes(d) == before    # no attempt was spent


def test_the_candidate_is_the_last_accepted_else_the_base_else_nothing(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    accepted, base, working = (_candidate(repo, "acc", "1" * 40), _candidate(repo, "base", "2" * 40),
                               _candidate(repo, "work", "3" * 40))
    store, d, _ = _proposed(tmp_path, _spec("R1 a"), rid="a", last_accepted_candidate=accepted,
                            base_candidate=base, working_candidate=working)
    row, _, _, copier = _verify(tmp_path, d, runner=Runner({"R1": (1, "red")}))
    assert [c.candidate_id for c, _ in copier.calls] == ["acc"]            # never the working one
    assert _record(d)["candidate"]["source"] == "last_accepted"
    store, d, _ = _proposed(tmp_path, _spec("R1 a"), rid="b", last_accepted_candidate=None,
                            base_candidate=base, working_candidate=working)
    row, _, _, copier = _verify(tmp_path, d, runner=Runner({"R1": (1, "red")}))
    assert [c.candidate_id for c, _ in copier.calls] == ["base"]
    assert _record(d)["candidate"]["source"] == "base"
    store, d, _ = _proposed(tmp_path, _spec("R1 a"), rid="c", last_accepted_candidate=None,
                            working_candidate=working)
    before = _bytes(d)
    row, spaces, runner, copier = _verify(tmp_path, d)
    assert row["action"] == "refused" and "no accepted or base candidate" in row["why"]
    assert copier.calls == [] and _bytes(d) == before
    missing = _candidate(tmp_path / "gone")
    store, d, _ = _proposed(tmp_path, _spec("R1 a"), rid="e", last_accepted_candidate=missing)
    row, *_ = _verify(tmp_path, d)
    assert row["action"] == "refused" and "not a directory" in row["why"]


def test_a_node_dossier_is_measured_on_the_nodes_run_and_a_project_halt_has_no_candidate(
        tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(tmp_path, "r", condition=Condition.ACTIVE, last_accepted_candidate=_candidate(repo))
    store = ProjectStore(tmp_path, "p")
    store.write_state(ProjectState(project_id="p", repo_path="/x", nodes=[
        TaskNode(id="n1", lifecycle=Lifecycle.ABANDONED, run_id="r", rejections=5, note="x"),
        TaskNode(id="n2", lifecycle=Lifecycle.READY, dependencies=["ghost"])]))
    fake = Fake(_proposal(spec=_spec("R1 a")))
    # an ABANDONED node is an R&D negative: it needs its rounds before a proposal; drive them
    for k in range(12):
        rows = doubledown.double_down_pass(tmp_path, analysts=fake.factory,
                                           now=NOW + timedelta(k), round_limit=8)
    node_rows = [r for r in rows if r.get("rule") == "rejections_exhausted"]
    assert node_rows and node_rows[-1].get("outcome") == "PROPOSAL", rows
    halt_id = node_rows[-1]["halt_id"]
    row, _, runner, copier = _verify(tmp_path, store.dir / "dd" / halt_id,
                                     runner=Runner({"R1": (1, "red")}))
    assert row["action"] == "red_reproduced" and copier.calls[0][0].candidate_id == "c1"
    project_halt = [r for r in rows if r["scope"] == "project"]
    assert project_halt and project_halt[0]["outcome"] == "PROPOSAL"
    row, *_ = _verify(tmp_path, store.dir / "dd" / project_halt[0]["halt_id"])
    assert row["action"] == "refused" and "carries no spec" in row["why"]


# --- the measurement: a scratch copy, a workspace per criterion, the requested isolation --------- #

def test_every_criterion_runs_in_a_workspace_of_its_own_under_the_requested_isolation(tmp_path):
    store, d, halt_id = _proposed(tmp_path, _spec("R1 a", "R2 b"))
    for isolation in (Isolation.STRICT, Isolation.NONE):
        store, d, halt_id = _proposed(tmp_path, _spec("R1 a", "R2 b"), rid=f"i-{isolation.value}")
        row, spaces, runner, copier = _verify(tmp_path, d, runner=Runner({"R1": (1, "red")}),
                                              isolation=isolation)
        assert row["status"] == "RED_REPRODUCED" and _record(d)["isolation"] == isolation.value
        assert [c["isolation"] for c in runner.calls] == [isolation, isolation]
    assert [(p, n.split("-")[0], e) for p, n, e, _ in spaces.log] == [
        ("verification", "ddv", 512 << 20), ("check", "ddc", 64 << 20), ("check", "ddc", 64 << 20)]
    assert [n for _, n, _, _ in spaces.log][1:] == [f"ddc-{halt_id}-1", f"ddc-{halt_id}-2"]
    arena = spaces.log[0][3] / "arena"
    assert copier.calls[0][1] == arena
    first, second = runner.calls
    assert first["cwd"] == second["cwd"] == arena
    assert first["scratch"] != second["scratch"] and arena not in first["scratch"].parents
    assert first["scratch_entries"] == [] and second["scratch_entries"] == []     # empty at every use
    assert (first["run_id"], first["iteration"], first["attempt"]) == (f"ddv-{halt_id}", 1, 1)
    assert first["timeout"] == second["timeout"] == 300
    assert first["candidate"].repo_path == str(arena)            # the probe names the copy, not the repo
    assert first["candidate"].binding() == _candidate(tmp_path / "repo").binding()
    assert spaces.live == set() and len(spaces.released) == 3       # every workspace was given back


def test_the_default_isolation_is_strict(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec("R1 a"))
    spaces = Spaces(tmp_path.parent / f"{tmp_path.name}-spaces")
    spaces.base.mkdir(exist_ok=True)
    runner = Runner({"R1": (1, "red")})
    ddverify.verify(tmp_path, d.parent.parent, d.name, session=spaces.session, now=NOW,
                    materialize=Copier(), check_runner=runner)
    assert [c["isolation"] for c in runner.calls] == [Isolation.STRICT]
    assert _record(d)["isolation"] == "strict"
    store, d2, _ = _proposed(tmp_path, _spec("R1 a"), rid="g")
    ddverify.verify(tmp_path, d2.parent.parent, d2.name, session=spaces.session, now=NOW,
                    isolation="none", materialize=Copier(), check_runner=runner)
    assert runner.calls[-1]["isolation"] is Isolation.NONE       # a plain word is accepted


def test_a_long_output_is_kept_as_its_tail_only(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec("R1 a"))
    text = "x" * 5000 + "THE-END"
    _verify(tmp_path, d, runner=Runner({"R1": (1, text)}))
    tail = _record(d)["criteria"][0]["tail"]
    assert len(tail) == 2000 and tail.endswith("THE-END")


# --- the live repository and the states stay as they were ----------------------------------------- #

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


def test_the_measurement_is_a_scratch_copy_and_the_live_repository_is_untouched(
        tmp_path, processes_allowed):
    from hoh import workspace

    repo = _real_repo(tmp_path)
    candidate = workspace.snapshot(repo, "c1")
    (repo / "dirty.txt").write_text("uncommitted\n")              # live work the copy must not see
    refs_before, status_before = _git(repo, "show-ref"), _git(repo, "status", "--porcelain")
    spec = _spec("R1 test -f app.txt", "R2 test ! -f dirty.txt", "R3 test -f gone.txt")
    store = _run(tmp_path, "h", last_accepted_candidate=candidate)
    doubledown.double_down_pass(tmp_path, analysts=Fake(_proposal(spec=spec)).factory, now=NOW)
    [d] = [p for p in (store.dir / "dd").iterdir()]
    fingerprint = doubledown.fingerprint([repo])["digest"]
    spaces = Spaces(tmp_path.parent / f"{tmp_path.name}-spaces")
    spaces.base.mkdir(exist_ok=True)
    seen: dict = {}
    real = Runner({"R3": (1, "red")})

    def spying(check, candidate_, **kw):
        seen[check.check_id] = sorted(os.listdir(kw["cwd"]))
        return real(check, candidate_, **kw)
    row = ddverify.verify(tmp_path, store.dir, d.name, session=spaces.session, now=NOW,
                          check_runner=spying)                    # the real workspace.materialize
    assert row["status"] == "RED_REPRODUCED", row
    assert seen["R1"] == ["app.txt"]                              # the bound tree, nothing of the live dirt
    assert doubledown.fingerprint([repo])["digest"] == fingerprint
    assert (_git(repo, "show-ref"), _git(repo, "status", "--porcelain")) == (refs_before, status_before)
    assert repo not in spaces.log[0][3].parents and spaces.live == set()


def test_a_measurement_changes_no_state_and_the_module_calls_nothing_that_would(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec("R1 a"))
    state = _state_bytes(tmp_path)
    _verify(tmp_path, d, runner=Runner({"R1": (1, "red")}))
    assert _state_bytes(tmp_path) == state
    tree = ast.parse(Path(ddverify.__file__).read_text(encoding="utf-8"))
    forbidden = {"write_state", "write_checks", "write_evidence", "unblock", "resume", "arm_new",
                 "disarm", "disarm_all", "acquire", "release", "begin_iteration",
                 "reject_candidate", "transition", "block_under_contract", "record_external_action",
                 "write_receipt", "write_log", "system", "Popen", "run"}
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)}
    called |= {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name)}
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not (forbidden & (called | imported)), sorted(forbidden & (called | imported))


# --- fail closed on odd files ---------------------------------------------------------------------- #

def _tamper_file(path: Path, how: str) -> None:
    if how == "not_json":
        path.write_text("not json")
    elif how == "list":
        path.write_text("[]")
    elif how == "bad_seal":
        body = json.loads(path.read_text())
        body["status"] = "RED_REPRODUCED" if body.get("status") != "RED_REPRODUCED" else "REFUTED"
        path.write_text(json.dumps(body))
    elif how == "oversize":
        path.write_text(" " * ((1 << 20) + 1))
    elif how == "fifo":
        path.rename(path.with_name(path.name + ".v1"))
        os.mkfifo(path)
    elif how == "directory":
        path.rename(path.with_name(path.name + ".v1"))
        os.mkdir(path)
    elif how == "hardlink":
        os.link(path, path.with_name(path.name + ".second"))
    elif how == "symlink":
        path.rename(path.with_name(path.name + ".v1"))
        path.symlink_to(path.with_name(path.name + ".v1"))
    elif how == "dangling":
        path.rename(path.with_name(path.name + ".v1"))
        path.symlink_to(path.with_name("nothing-here"))
    elif how == "latin1":
        path.write_bytes(b'{"schema": "caf\xe9"}')
    elif how == "utf16":
        path.write_bytes(json.dumps({"schema": "x"}).encode("utf-16"))
    elif how == "nul":
        path.write_bytes(b'{"schema": "a\x00b"}')
    elif how == "mode000":
        path.chmod(0)


@pytest.mark.parametrize("how", ["not_json", "list", "bad_seal", "oversize", "fifo", "directory",
                                 "hardlink", "symlink", "dangling", "latin1", "utf16", "nul",
                                 "mode000"])
@pytest.mark.parametrize("name", ["verify.json", "verify-attempt-1.json"])
def test_an_odd_verification_file_is_a_refusal_never_a_measurement_or_an_overwrite(
        tmp_path, name, how):
    if how == "mode000" and os.geteuid() == 0:
        pytest.skip("root reads a file whatever its mode")
    store, d, _ = _proposed(tmp_path, _spec("R1 a"))
    _verify(tmp_path, d, runner=Runner({"R1": (1, "red")}))
    assert (d / name).exists()
    _tamper_file(d / name, how)
    runner = Runner()
    row, *_ = _verify(tmp_path, d, runner=runner)
    assert row["action"] == "refused", row
    assert runner.calls == []


def test_a_verification_that_belongs_to_another_proposal_or_attempts_is_refused_on_read(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec("R1 a"))
    _verify(tmp_path, d, runner=Runner({"R1": (1, "red")}))
    rec = _record(d)
    for field, value in (("outcome_digest", "0" * 64), ("block_digest", "0" * 64),
                         ("status", "MAYBE"), ("attempts", 2)):
        forged = doubledown._seal({**{k: v for k, v in rec.items() if k != "digest"}, field: value})
        (d / "verify.json").chmod(0o600)
        (d / "verify.json").write_text(json.dumps(forged))
        with pytest.raises(doubledown.DossierError, match="verification does not belong"):
            doubledown.read_dossier(d)
    (d / "verify.json").write_text(json.dumps(rec))
    attempt = _record(d, "verify-attempt-1.json")
    for name, n in (("verify-attempt-2.json", 2),    # one attempt more than the verdict counts
                    ("verify-result-2.json", 2),     # a result with no attempt
                    ("verify-result-1.json", 1)):    # the attempt that gave the verdict has no result
        forged = doubledown._seal({**{k: v for k, v in attempt.items() if k != "digest"},
                                   "schema": doubledown.SCHEMA_ATTEMPT if "attempt" in name
                                   else doubledown.SCHEMA_RESULT, "n": n})
        (d / name).write_text(json.dumps(forged))
        with pytest.raises(doubledown.DossierError):
            doubledown.read_dossier(d)
        (d / name).rename(d / (name + ".v1"))
    assert doubledown.read_dossier(d).verification["status"] == "RED_REPRODUCED"


def test_a_dossier_reached_through_a_link_is_not_verified(tmp_path):
    store, d, halt_id = _proposed(tmp_path, _spec("R1 a"))
    link = tmp_path / "link"
    link.symlink_to(store.dir)
    runner = Runner()
    spaces = Spaces(tmp_path.parent / f"{tmp_path.name}-spaces")
    spaces.base.mkdir(exist_ok=True)
    row = ddverify.verify(tmp_path, link, halt_id, session=spaces.session, now=NOW,
                          materialize=Copier(), check_runner=runner)
    assert row["action"] == "refused" and runner.calls == []
    for bad in ("", "x", "0" * 16, "G" * 16, halt_id.upper(), halt_id + "\n", None):
        row = ddverify.verify(tmp_path, store.dir, bad, session=spaces.session, now=NOW,
                              materialize=Copier(), check_runner=runner)
        assert row["action"] == "refused" and runner.calls == []


def test_a_second_caller_that_finds_the_dossier_held_measures_nothing(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec("R1 a"))
    with doubledown._locked(d):
        row, _, runner, copier = _verify(tmp_path, d)
    assert row["action"] == "refused" and "another pass holds this dossier" in row["why"]
    assert runner.calls == [] and copier.calls == []


# --- the pass: the autonomous door ------------------------------------------------------------------ #

def _verifier(tmp_path, spaces, runner, **kw):
    def verifier(owner, halt_id, now):
        return ddverify.verify_if_due(tmp_path, owner, halt_id, session=spaces.session, now=now,
                                      materialize=Copier(), check_runner=runner, **kw)
    return verifier


def test_the_pass_verifies_a_proposal_in_the_tick_that_made_it_and_never_again(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(tmp_path, "h", last_accepted_candidate=_candidate(repo))
    spaces = Spaces(tmp_path.parent / f"{tmp_path.name}-spaces")
    spaces.base.mkdir(exist_ok=True)
    runner = Runner({"R1": (1, "red")})
    fake = Fake(_proposal(spec=_spec("R1 a")))
    rows = doubledown.double_down_pass(tmp_path, analysts=fake.factory,
                                       verifier=_verifier(tmp_path, spaces, runner), now=NOW)
    assert [(r["action"], r.get("status")) for r in rows] == [("proposal", None),
                                                              ("red_reproduced", "RED_REPRODUCED")]
    assert rows[1]["executed"] is True and rows[1]["scope"] == "dossier"
    for k in range(2):
        rows = doubledown.double_down_pass(tmp_path, analysts=fake.factory,
                                           verifier=_verifier(tmp_path, spaces, runner),
                                           now=NOW + timedelta(k + 1))
        assert [r["action"] for r in rows] == ["closed"]
    assert len(runner.calls) == 1


def test_the_pass_without_a_verifier_verifies_nothing(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec("R1 a"))
    rows = doubledown.double_down_pass(tmp_path, now=NOW)
    assert [r["action"] for r in rows] == ["closed"] and not (d / "verify.json").exists()
    assert not any(n.startswith("verify") for n in os.listdir(d))


def test_a_pass_executes_at_most_two_verifications_and_the_rest_wait_for_the_next(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for k in range(3):
        _run(tmp_path, f"h{k}", last_accepted_candidate=_candidate(repo),
             blocked_reason=f"stuck {k}")
    spaces = Spaces(tmp_path.parent / f"{tmp_path.name}-spaces")
    spaces.base.mkdir(exist_ok=True)
    runner = Runner({"R1": (1, "red")})
    fake = Fake(_proposal(spec=_spec("R1 a")))
    rows = doubledown.double_down_pass(tmp_path, analysts=fake.factory,
                                       verifier=_verifier(tmp_path, spaces, runner), now=NOW,
                                       round_limit=8)
    assert [r["action"] for r in rows].count("red_reproduced") == 2 and len(runner.calls) == 2
    rows = doubledown.double_down_pass(tmp_path, analysts=fake.factory,
                                       verifier=_verifier(tmp_path, spaces, runner),
                                       now=NOW + timedelta(1))
    assert sorted(r["action"] for r in rows) == ["closed", "closed", "closed", "red_reproduced"]
    assert len(runner.calls) == 3
    rows = doubledown.double_down_pass(tmp_path, verifier=_verifier(tmp_path, spaces, runner),
                                       now=NOW + timedelta(2), verify_limit=0)
    assert {r["action"] for r in rows} == {"closed"}              # the limit is a number, not a flag


def test_a_verifier_that_raises_is_a_row_and_the_pass_goes_on(tmp_path):
    store, d, _ = _proposed(tmp_path, _spec("R1 a"))
    _run(tmp_path, "z", blocked_reason="another")

    def broken(owner, halt_id, now):
        raise RuntimeError("the verifier broke")
    rows = doubledown.double_down_pass(tmp_path, verifier=broken, now=NOW)
    assert [r["action"] for r in rows if r.get("scope") == "dossier"] == ["refused"]
    assert any(r.get("target") == "z" for r in rows)              # the other block was still handled


def test_an_inconclusive_attempt_spends_a_pass_execution_and_a_refusal_does_not(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for k in range(3):
        _run(tmp_path, f"h{k}", last_accepted_candidate=_candidate(repo),
             blocked_reason=f"stuck {k}")
    fake = Fake(_proposal(spec=_spec("R1 a")))
    refusing = Spaces(tmp_path.parent / f"{tmp_path.name}-spaces", refuse="session")
    rows = doubledown.double_down_pass(tmp_path, analysts=fake.factory, now=NOW, round_limit=8,
                                       verifier=_verifier(tmp_path, refusing, Runner()))
    assert [r["action"] for r in rows].count("refused") == 3        # none spent a pass execution
    spaces = Spaces(tmp_path.parent / f"{tmp_path.name}-spaces")
    spaces.base.mkdir(exist_ok=True)
    runner = _failing_runner()
    rows = doubledown.double_down_pass(tmp_path, verifier=_verifier(tmp_path, spaces, runner),
                                       now=NOW + timedelta(1))
    assert [r["action"] for r in rows].count("inconclusive") == 2 and len(runner.calls) == 2


# --- the decision the person reads -------------------------------------------------------------------- #

def test_the_decision_in_needs_you_says_what_was_measured(tmp_path):
    from hoh import missioncontrol

    repo = tmp_path / "repo"
    repo.mkdir()
    run = _run(tmp_path, "r", last_accepted_candidate=_candidate(repo))
    ProjectStore(tmp_path, "p").write_state(ProjectState(project_id="p", repo_path="/x", nodes=[
        TaskNode(id="n1", lifecycle=Lifecycle.BLOCKED, run_id="r", note="x")]))
    doubledown.double_down_pass(tmp_path, analysts=Fake(_proposal(spec=_spec("R1 a"))).factory,
                                now=NOW)
    doc = missioncontrol.project_view(str(tmp_path), "p", "needs-you")
    assert doc["decisions"][0]["decision"].startswith("correction proposal (UNVERIFIED)")
    assert "verification" not in doc["double_down"][0]
    [d] = [p for p in (run.dir / "dd").iterdir()]
    for outcome, status, clause in (
            ({"R1": (1, "red")}, "RED_REPRODUCED",
             "RED_REPRODUCED, no proof of repair or that the red is the block's: R1 `a` exit 1"),
            ({"R1": (0, "ok")}, "REFUTED", "REFUTED: nothing_red")):
        for p in d.glob("verify*"):
            p.rename(p.with_name(p.name + f".v{status}"))
        _verify(tmp_path, d, runner=Runner(outcome))
        doc = missioncontrol.project_view(str(tmp_path), "p", "needs-you")
        assert doc["decisions"][0]["decision"].startswith(f"correction proposal ({clause}): apply fix")
        assert doc["double_down"][0]["verification"]["status"] == status
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            assert missioncontrol.run("needs-you", str(tmp_path), project="p", as_json=False) == 0
        assert f"correction proposal ({clause})" in buffer.getvalue()      # the text view says it too
    assert doc["double_down"][0]["verification"] == {"status": "REFUTED", "reason": "nothing_red"}


def test_an_unrelated_red_is_reproduced_all_the_same_and_the_person_is_told_it_proves_neither(tmp_path):
    """PINNED WEAKNESS (spec L13): the verdict never consults the block, so a failing test that has
    nothing to do with it gives the same status. What the slice owes is not to hide that: the status
    is named for what happened, the record says what it is not, and the decision names the criterion."""
    from hoh import missioncontrol

    repo = tmp_path / "repo"
    repo.mkdir()
    run = _run(tmp_path, "r", last_accepted_candidate=_candidate(repo),
               blocked_reason="the merge was obstructed by a conflict in docs/README.md")
    ProjectStore(tmp_path, "p").write_state(ProjectState(project_id="p", repo_path="/x", nodes=[
        TaskNode(id="n1", lifecycle=Lifecycle.BLOCKED, run_id="r", note="x")]))
    spec = _spec("U1 python3 -m pytest -q tests/test_something_else_entirely.py")
    doubledown.double_down_pass(tmp_path, analysts=Fake(_proposal(spec=spec)).factory, now=NOW)
    [d] = [p for p in (run.dir / "dd").iterdir()]
    row, *_ = _verify(tmp_path, d, runner=Runner({"U1": (1, "FAILED tests/test_something_else_entirely.py")}))
    assert row["status"] == "RED_REPRODUCED"                       # nothing ties the red to the block
    rec = _record(d)
    assert "neither a proof that the repair works nor a proof that the red is the block's" in rec["says"]
    assert "obstructed" in rec["block"]["reason"]
    doc = missioncontrol.project_view(str(tmp_path), "p", "needs-you")
    text = doc["decisions"][0]["decision"]
    assert "U1 `python3 -m pytest -q tests/tes` exit 1" in text
    assert "no proof of repair or that the red is the block's" in text
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert missioncontrol.run("needs-you", str(tmp_path), project="p", as_json=False) == 0
    assert "no proof of repair or that the red is the block's" in buffer.getvalue()   # not clipped away
    assert doc["double_down"][0]["verification"]["red"] == [
        {"id": "U1", "command": "python3 -m pytest -q tests/test_something_else_entirely.py"[:80], "exit_code": 1}]


def test_a_real_import_error_is_a_red_and_a_signature_counts_only_at_exit_4_and_5(tmp_path):
    cases = [
        (1, "ModuleNotFoundError: No module named 'x'", False, True),
        (2, "ImportError: cannot import name 'y' from 'z'", False, True),
        (1, "FileNotFoundError: [Errno 2] No such file or directory: 'f'", False, True),
        (5, "no tests ran", True, True),
        (4, "ERROR: file or directory not found: tests/t.py", True, True),
        (4, "a usage error that says nothing known", False, False),
        (1, "an ordinary failure", False, False),
    ]
    for k, (code, text, artefactual, seen) in enumerate(cases):
        store, d, _ = _proposed(tmp_path, _spec("R1 a"), rid=f"e{k}")
        row, *_ = _verify(tmp_path, d, runner=Runner({"R1": (code, text)}))
        crit = _record(d)["criteria"][0]
        assert (crit["artefactual"], crit["signature_seen"]) == (artefactual, seen), (code, text)
        assert row["status"] == ("REFUTED" if artefactual else "RED_REPRODUCED"), (code, text)
        if artefactual:
            assert row["reason"] == "only_artefactual"


def test_the_pass_waits_ten_minutes_between_attempts_and_a_person_does_not(tmp_path):
    store, d, halt_id = _proposed(tmp_path, _spec("R1 a"))
    spaces = Spaces(tmp_path.parent / f"{tmp_path.name}-spaces")
    spaces.base.mkdir(exist_ok=True)
    runner = _failing_runner()

    def ask(at):
        return ddverify.verify_if_due(tmp_path, store.dir, halt_id, session=spaces.session, now=at,
                                      materialize=Copier(), check_runner=runner)
    assert ask(NOW)["action"] == "inconclusive" and len(runner.calls) == 1
    assert ask(NOW + timedelta(seconds=599)) is None and len(runner.calls) == 1       # too soon: quiet
    assert ask(NOW + timedelta(seconds=600))["action"] == "inconclusive"
    assert len(runner.calls) == 2
    row, *_ = _verify(tmp_path, d, spaces=spaces, runner=runner, now=NOW + timedelta(seconds=601))
    assert row["action"] == "inconclusive" and len(runner.calls) == 3        # a person is not held back
    assert _record(d)["reason"] == "attempts_spent"


# --- the doors of the command line ----------------------------------------------------------------------- #

def _main(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli.main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()


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
    roots = se.EphemeralRoots(Registry(), "pd", "ddv-session", admission=admission)

    class Session:
        session_id = "ddv-session"
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
                                 state_root=state_root, asked=asked, se=se)


def _real_runner_without_the_scratch(monkeypatch):
    """The product's real `runner.run_check`, called without its `scratch=`: the runner refuses a
    scratch that lies inside a scratch in use, and this test runs inside the one the product's own
    acceptance gives it (the attack-list item on `run_check(scratch=)` under the outer TMPDIR).
    What is passed as the scratch is recorded; the node that runs the real guard is
    `test_the_real_runner_takes_a_registered_workspace_as_the_checks_scratch`."""
    from hoh import runner as runner_module

    real, scratches = runner_module.run_check, []

    def run(check, candidate, **kw):
        scratches.append(Path(kw.pop("scratch")))
        return real(check, candidate, **kw)
    monkeypatch.setattr(runner_module, "run_check", run)
    return scratches


def test_resume_quota_verify_proposals_measures_through_the_command_line(
        tmp_path, monkeypatch, processes_allowed):
    from hoh import workspace

    eph = _ephemeral(tmp_path, monkeypatch)
    scratches = _real_runner_without_the_scratch(monkeypatch)
    repo = _real_repo(tmp_path)
    candidate = workspace.snapshot(repo, "c1")
    store = _run(tmp_path, "h", last_accepted_candidate=candidate)
    spec = _spec("R1 test -f app.txt", "R2 test -f gone.txt")
    doubledown.double_down_pass(tmp_path, analysts=Fake(_proposal(spec=spec)).factory, now=NOW)
    [d] = [p for p in (store.dir / "dd").iterdir()]
    state = _state_bytes(tmp_path)
    rc, out, err = _main(["--root", str(tmp_path), "resume-quota", "--double-down",
                          "--verify-proposals", "--verify-isolation", "none",
                          "--storage-root", str(eph.state_root)])
    assert rc == 0, err
    rows = json.loads(out)["double_down"]
    [row] = [r for r in rows if r.get("scope") == "dossier"]
    assert (row["action"], row["status"], row["reason"], row["executed"]) == (
        "red_reproduced", "RED_REPRODUCED", "red_found", True)
    rec = _record(d)
    assert [(c["id"], c["outcome"], c["exit_code"]) for c in rec["criteria"]] == [
        ("R1", "PASS", 0), ("R2", "FAIL", 1)]
    assert rec["isolation"] == "none" and eph.asked == [str(eph.state_root)]
    assert [r[2] for r in eph.reserved] == [512 << 20, 64 << 20, 64 << 20]
    assert len(eph.released) == 3 and eph.closed == [1]           # every workspace given back
    assert len(scratches) == 2 and len(set(scratches)) == 2        # one registered workspace per criterion
    assert all(p.name.startswith("ddc-") and eph.state_root in p.parents for p in scratches)
    for ws in (eph.state_root / "projects" / "pd" / "tmp").rglob("*"):
        assert not str(ws).startswith(str(store.dir))
    assert _state_bytes(tmp_path) == state
    rc, out2, _ = _main(["--root", str(tmp_path), "resume-quota", "--double-down",
                         "--verify-proposals", "--verify-isolation", "none",
                         "--storage-root", str(eph.state_root)])
    assert rc == 0 and [r["action"] for r in json.loads(out2)["double_down"]] == ["closed"]
    assert len(eph.reserved) == 3                                 # the second tick measured nothing


def test_the_real_runner_takes_a_registered_workspace_as_the_checks_scratch(
        tmp_path, monkeypatch, processes_allowed):
    """INTEGRATION CHECK (named in the spec, not in a criterion): the real `run_check(scratch=)`
    with the product's real `EphemeralRoots` workspaces. It cannot run inside a check's own scratch,
    which the runner refuses by design, so it is replayed in a plain shell."""
    from hoh import workspace

    eph = _ephemeral(tmp_path, monkeypatch)
    repo = _real_repo(tmp_path)
    store = _run(tmp_path, "h", last_accepted_candidate=workspace.snapshot(repo, "c1"))
    doubledown.double_down_pass(tmp_path, analysts=Fake(_proposal(spec=_spec(
        "R1 test -f app.txt", "R2 test -f gone.txt"))).factory, now=NOW)
    [d] = [p for p in (store.dir / "dd").iterdir()]
    row = ddverify.verify(tmp_path, store.dir, d.name, session=cli._verification_sessions(
        types.SimpleNamespace(storage_root=None)), isolation=Isolation.NONE, now=NOW)
    assert (row["action"], row["status"]) == ("red_reproduced", "RED_REPRODUCED"), row
    assert [(c["id"], c["outcome"]) for c in _record(d)["criteria"]] == [("R1", "PASS"),
                                                                       ("R2", "FAIL")]
    assert len(eph.released) == 3


def test_hoh_double_down_verify_prints_a_row_and_its_exit_code_says_whether_a_verdict_is_on_record(
        tmp_path, monkeypatch, processes_allowed):
    from hoh import workspace

    eph = _ephemeral(tmp_path, monkeypatch)
    _real_runner_without_the_scratch(monkeypatch)
    repo = _real_repo(tmp_path)
    store = _run(tmp_path, "h", last_accepted_candidate=workspace.snapshot(repo, "c1"))
    doubledown.double_down_pass(tmp_path, analysts=Fake(_proposal(spec=_spec(
        "R1 test -f gone.txt", "R2 true"))).factory, now=NOW)
    [d] = [p for p in (store.dir / "dd").iterdir()]
    argv = ["--root", str(tmp_path), "double-down", "verify", d.name, "--run", "h",
            "--storage-root", str(eph.state_root)]
    rc, out, err = _main([*argv, "--isolation", "none"])
    assert rc == 0, err
    row = json.loads(out)
    assert (row["action"], row["status"], row["halt_id"]) == ("red_reproduced", "RED_REPRODUCED", d.name)
    rc, out, _ = _main([*argv, "--isolation", "none"])
    assert rc == 0 and json.loads(out)["action"] == "closed"
    rc, out, _ = _main(["--root", str(tmp_path), "double-down", "verify", "0" * 16, "--run", "h",
                        "--storage-root", str(eph.state_root)])
    assert rc == 2 and json.loads(out)["action"] == "refused"


def test_hoh_double_down_verify_exit_codes_of_an_attempt_that_measured_nothing_and_of_a_lost_record(
        tmp_path, monkeypatch):
    eph = _ephemeral(tmp_path, monkeypatch)
    store, d, _ = _proposed(tmp_path, _spec("R1 a"))
    monkeypatch.setattr("hoh.workspace.materialize", Copier())
    monkeypatch.setattr("hoh.runner.run_check", _failing_runner())
    argv = ["--root", str(tmp_path), "double-down", "verify", d.name, "--run", "h",
            "--storage-root", str(eph.state_root), "--isolation", "none"]
    codes = [_main(argv)[0] for _ in range(4)]
    assert codes == [2, 2, 0, 0]                  # two attempts that measured nothing, the terminal, quiet
    store, d2, _ = _proposed(tmp_path, _spec("R1 a"), rid="g")
    real = doubledown._create
    monkeypatch.setattr(doubledown, "_create", lambda path, record: (_ for _ in ()).throw(
        OSError(28, "full")) if Path(path).name == "verify.json" else real(path, record))
    monkeypatch.setattr("hoh.runner.run_check", Runner({"R1": (1, "red")}))
    rc, out, _ = _main(["--root", str(tmp_path), "double-down", "verify", d2.name, "--run", "g",
                        "--storage-root", str(eph.state_root), "--isolation", "none"])
    assert rc == 1 and json.loads(out)["action"] == "unrecorded"


def test_the_verify_flags_need_the_double_down_and_the_isolation_needs_the_verification(tmp_path):
    """The double-down and the measurement of proposals are ON by default since slice 3: the flags
    now need them not to be opted out of (`--no-double-down`, `--no-verify-proposals`)."""
    root = str(tmp_path)
    for argv in (["resume-quota", "--no-double-down", "--verify-proposals"],
                 ["resume-quota", "--no-double-down", "--verify-isolation", "none"],
                 ["resume-quota", "--no-verify-proposals", "--verify-isolation", "none"]):
        rc, out, err = _main(["--root", root, *argv])
        assert rc == 2 and "refused" in err and out == ""
    rc, out, err = _main(["--root", root, "resume-quota", "--double-down", "--verify-isolation",
                          "bwrap"])
    assert rc == 2                                               # argparse: not one of the choices
    parsed = cli.build_parser().parse_args(["resume-quota"])
    assert (parsed.verify_proposals, parsed.verify_isolation) == (False, None)   # typed flags only


def test_the_verification_the_pass_asks_for_runs_strict_unless_it_is_told_otherwise(
        tmp_path, monkeypatch):
    seen: list = []
    monkeypatch.setattr(ddverify, "verify_if_due", lambda *a, **kw: seen.append(kw) or None)
    for flag, expected in ((None, Isolation.STRICT), ("strict", Isolation.STRICT),
                           ("none", Isolation.NONE)):
        args = types.SimpleNamespace(root=str(tmp_path), verify_isolation=flag, storage_root=None)
        cli._proposal_verifier(args)(tmp_path, "0" * 16, NOW)
        assert seen[-1]["isolation"] is expected
        assert callable(seen[-1]["session"]) and seen[-1]["now"] == NOW
    args = types.SimpleNamespace(root=str(tmp_path), verify_isolation=None, storage_root=None)
    assert cli._proposal_verifier(args) is not None


def test_hoh_double_down_verify_forwards_its_isolation_and_its_storage_root(
        tmp_path, monkeypatch):
    seen: list = []
    monkeypatch.setattr(ddverify, "verify", lambda root, owner, halt_id, **kw: seen.append(
        (root, Path(owner), halt_id, kw)) or {"action": "closed", "status": "RED_REPRODUCED"})
    asked: list = []
    monkeypatch.setattr(cli, "_verification_sessions", lambda args: asked.append(args) or "session")
    store = _run(tmp_path, "h")
    for flags, expected in (([], Isolation.STRICT), (["--isolation", "strict"], Isolation.STRICT),
                            (["--isolation", "none"], Isolation.NONE),
                            (["--isolation=none"], Isolation.NONE)):
        rc, out, _ = _main(["--root", str(tmp_path), "double-down", "verify", "0" * 16, "--run",
                            "h", "--storage-root", "/some/root", *flags])
        assert rc == 0
        root, owner, halt_id, kw = seen[-1]
        assert (root, owner, halt_id) == (str(tmp_path), store.dir, "0" * 16)
        assert kw["isolation"] is expected and kw["session"] == "session"
        assert asked[-1].storage_root == "/some/root"
    ProjectStore(tmp_path, "p").write_state(ProjectState(project_id="p", repo_path="/x", nodes=[]))
    rc, _, _ = _main(["--root", str(tmp_path), "double-down", "verify", "0" * 16, "--project", "p"])
    assert rc == 0 and seen[-1][1] == ProjectStore(tmp_path, "p").dir


def test_a_storage_root_that_cannot_admit_a_write_means_nothing_is_measured(
        tmp_path, monkeypatch):
    store, d, _ = _proposed(tmp_path, _spec("R1 a"))
    monkeypatch.setattr(cli, "storage_admission_for", lambda option, **kw: (_ for _ in ()).throw(
        cli.StorageRefused("storage state root is not initialised")))
    monkeypatch.setattr("hoh.runner.run_check", Runner())
    before = _bytes(d)
    rc, out, _ = _main(["--root", str(tmp_path), "double-down", "verify", d.name, "--run", "h"])
    row = json.loads(out)
    assert rc == 2 and row["action"] == "refused" and "storage" in row["why"]
    assert _bytes(d) == before
    monkeypatch.setattr(cli, "storage_admission_for", lambda option, **kw: (object(), tmp_path))
    monkeypatch.setattr(cli, "_open_ephemeral", lambda *a: (None, "no registered project"))
    rc, out, _ = _main(["--root", str(tmp_path), "double-down", "verify", d.name, "--run", "h"])
    assert rc == 2 and "no registered temporary root" in json.loads(out)["why"]
    assert _bytes(d) == before
