"""P2-43 slice a: a provider, runtime or time-budget failure is a wait or an
operator action -- never a verdict, never an iteration.

Between 2026-10-02 and 2026-10-07 a usage limit in QA was booked as a rejection
eight times (`candidate rejected: unverified, no QA verdict`), the same limit in
the planner cost an iteration on resume, and a prompt stall and a role timeout
were routed to the digest as decisions. These tests drive the real controller
with a fake dispatcher (no Herdr, no agent, nothing outside `tmp_path`) through
each of them, and the classifier with the texts those runs left behind.

Mutant ids in a docstring (`M..`, `N..`) are the spec's table of mutations the
integration review applies; each test says which it kills.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hoh import failures, resumeloop, stages, taxonomy
from hoh.autonomy import AutonomyMode, compile_profile
from hoh.contracts import Budgets, Condition, Role, RunState, Stage
from hoh.capability import SealFailed
from hoh.controller import (
    AnswerMissing, Controller, DispatchError, PaneNotClean, RoleNotQuiescent,
    WaitingForApproval,
)
from hoh.failures import NormalizedFailure as NF
from hoh.failures import RetryMode
from hoh.governance import bind
from hoh.store import RunStore
from hoh.timers import TimerKind, TimerStatus, TimerStore

# --------------------------------------------------------------------------- #
# Fixture texts, from the runs (run path shortened to /r/...)
# --------------------------------------------------------------------------- #

OPEN = "--- Visible in pane ---"
CLOSE = "-----------------------"
FIRST = ("qa ended its turn without writing the answer file (/r/answers/i1-a1-qa.json), "
         "even after one follow-up. Without a structured answer there is nothing to evaluate.")


def pane(first: str, *lines: str) -> str:
    return "\n".join([first, OPEN, *lines, CLOSE])


QA_LIMIT = pane(
    FIRST,
    "● Usage limit reached · continuing automatically at 3:20am · esc to cancel",
    "⎿  You've hit your session limit · resets 3:20am (Europe/Istanbul)",
    "⚠ Usage limit reached · limit resets 3:20am",
)
STALL = pane('planner dispatch failed: {"error":{"code":"agent_prompt_stalled",'
             '"message":"prompt was not accepted"},"id":"cli:agent:prompt"}', "(empty)")
ROLE_FIRST = ('developer dispatch failed: {"error":{"code":"timeout","message":"timed out '
              'waiting for agent status"},"id":"cli:agent:prompt"}')
ROLE_TIME = pane(ROLE_FIRST, "⎿  Running… (7m 7s · timeout 10m)",
                 "✽ Whisking… (49m 48s · ↓ 76.6k tokens)")
NO_CAUSE = pane(FIRST, "✻ Worked for 2m 3s · done 12:17 AM")
AUTH = pane(FIRST, "⎿  Invalid API key · Please run /login")
AUTH_AND_LIMIT = pane(FIRST, "● Usage limit reached", "⎿  Invalid API key · Please run /login")

SPEC = "# Spec\nadd(a, b) must add correctly.\n"


def answer_missing(text: str, *, followed_up: bool) -> AnswerMissing:
    return AnswerMissing(text, answer_path=Path("/r/answers/i1-a1-qa.json"), target="t",
                         prompt_sha256="0" * 64, prompt_bytes=1, prompt_file=None,
                         sent_at=0.0, followed_up=followed_up)


# --------------------------------------------------------------------------- #
# A controller on fakes
# --------------------------------------------------------------------------- #

def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "project"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.email", "t@example.invalid")
    git(r, "config", "user.name", "T")
    (r / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


class Fake:
    """Planner, developer and QA the test decides. A `*_raise` that is set is
    raised by every call of that role until the test heals it with None."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.calls: list[str] = []
        self.planner_raise: BaseException | None = None
        self.developer_raise: BaseException | None = None
        self.qa_raise: BaseException | None = None
        #: What the follow-up for a missing answer raises (QA only gets here
        #: when `qa_raise` is an `AnswerMissing` that was not followed up).
        self.follow_up_raise: BaseException | None = None
        self.develop = lambda repo: None

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str:
        self.calls.append(role.value)
        if role is Role.PLANNER:
            if self.planner_raise is not None:
                raise self.planner_raise
            base = state.last_accepted_candidate or state.base_candidate
            return json.dumps({
                "run_id": state.run_id, "iteration": state.iteration,
                "base_candidate_id": base.candidate_id if base else f"{state.run_id}-base",
                "spec_digest": state.spec_digest, "objective": "add done.txt",
                "targets": ["app.py"], "preserve": [],
                "acceptance_checks": [{
                    "check_id": "K1", "description": "done.txt exists",
                    "command": "test -f done.txt", "expect_exit": 0, "preserves": False}],
                "out_of_scope": [], "evidence_refs": [],
                "repair_only": False, "repair_reason": None})
        if role is Role.DEVELOPER:
            self.develop(self.repo)
            if self.developer_raise is not None:
                raise self.developer_raise
            (self.repo / "done.txt").write_text("ok\n", encoding="utf-8")
            return "done"
        if self.qa_raise is not None:
            raise self.qa_raise
        return json.dumps({
            "verdicts": [{
                "check_id": "K1", "outcome": "PASS",
                "receipt_id": f"{state.run_id}-i{state.iteration}-a{state.attempt}-K1",
                "reproduction": "pytest", "note": "green"}],
            "open_gaps": [], "summary": "all good"})

    def follow_up(self, role, state, missing, *, charge):
        charge()
        raise self.follow_up_raise or answer_missing(str(missing), followed_up=True)

    def endpoint_evidence(self, role: Role) -> str:
        return f"fake:{role.value}"


class RouteB(Fake):
    """Another dispatcher, to prove a parked wait is the state, not the route."""


def build(tmp_path: Path, repo: Path, *, contract=None, fake=None):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    store = RunStore(tmp_path / "runs", "r1")
    state = Controller.new_state(run_id="r1", repo_path=repo, project_name="project",
                                 spec_path=spec, budgets=Budgets(max_iterations=9))
    store.write_state(state)
    if contract is not None:
        bind(store.dir, contract=contract)
    fake = fake or Fake(repo)
    return Controller(store, fake, spec_path=spec), state, store, fake


def armed(store: RunStore):
    return [t for t in TimerStore(store.dir.parent).read()
            if t.status is TimerStatus.ARMED and t.kind is TimerKind.PROVIDER_RETRY
            and t.run_id == "r1"]


def heal_and_resume(ctrl, state, store, fake):
    stages.unblock(state, "the wait is over")
    fake.planner_raise = fake.developer_raise = fake.qa_raise = None
    fake.follow_up_raise = None
    return ctrl.run_iteration(state)


def history(state) -> str:
    return "\n".join(state.history)


def pa_contract():
    return compile_profile(AutonomyMode.PLAN_AUTONOMOUS, contract_id="pa")


# --------------------------------------------------------------------------- #
# K1 -- a usage limit in QA is a wait; resuming keeps the iteration
# --------------------------------------------------------------------------- #

def _qa_limit_wait(tmp_path, repo):
    ctrl, state, store, fake = build(tmp_path, repo)
    fake.qa_raise = answer_missing(QA_LIMIT, followed_up=False)
    return ctrl, state, store, fake, ctrl.run_iteration(state)


def test_qa_usage_limit_after_follow_up_blocks_and_is_no_rejection(tmp_path, repo):
    """Kills M1 (QA limit booked as a rejection), M6 (no timer), M12 (no
    interception): the follow-up fails too, and the run waits."""
    ctrl, state, store, fake, out = _qa_limit_wait(tmp_path, repo)
    assert not out.accepted and out.reason.startswith("no verdict")
    assert (state.condition, state.blocked_kind, state.stage) == (
        Condition.BLOCKED, "usage_limit", Stage.VERIFYING)
    assert state.retry_after
    assert "candidate rejected" not in history(state)
    assert (state.iteration, state.usage.iterations, state.usage.outages,
            state.usage.loops_without_progress) == (1, 1, 0, 0)
    persisted = store.read_state()
    assert (persisted.condition, persisted.blocked_kind) == (Condition.BLOCKED, "usage_limit")
    [timer] = armed(store)
    assert timer.reason.startswith("usage limit: ")


def test_qa_usage_limit_resume_keeps_the_iteration(tmp_path, repo):
    """Kills M1: resuming replans the same iteration and charges nothing."""
    ctrl, state, store, fake, out = _qa_limit_wait(tmp_path, repo)
    done = heal_and_resume(ctrl, state, store, fake)
    assert done.accepted
    assert (state.iteration, state.usage.iterations) == (1, 1)


def test_a_parked_qa_wait_resumes_through_the_consumer_on_another_route(tmp_path, repo):
    """Kills M6 (no timer), M8 (`QUOTA_CLASSES` without the automatic kinds): a
    wait is the state file and the timer, not the pane that failed. (The wait is
    the provider's stated reset, `3:20am (Europe/Istanbul)`, so "due" is read 30
    hours ahead: no reset time is further away than a day.)"""
    ctrl, state, store, fake, out = _qa_limit_wait(tmp_path, repo)
    assert state.condition is Condition.BLOCKED and len(armed(store)) == 1
    calls_after_park = list(fake.calls)

    records = resumeloop.resume_runs(tmp_path / "runs",
                                     now=datetime.now(timezone.utc) + timedelta(hours=30))
    [rec] = records
    assert rec["action"] == "resumed"
    assert armed(store) == []

    route_b = RouteB(repo)
    fresh = Controller(store, route_b, spec_path=tmp_path / "spec.md")
    persisted = store.read_state()
    done = fresh.run_iteration(persisted)
    assert done.accepted
    assert (persisted.iteration, persisted.usage.iterations) == (1, 1)
    assert fake.calls == calls_after_park
    assert route_b.calls
    again = resumeloop.resume_runs(tmp_path / "runs",
                                   now=datetime.now(timezone.utc) + timedelta(hours=30))
    assert all(r["action"] != "resumed" for r in again)


# --------------------------------------------------------------------------- #
# K2 -- a usage limit in the planner returns its charge and arms a timer
# --------------------------------------------------------------------------- #

def test_planner_usage_limit_returns_the_iteration_charge_and_arms_a_timer(tmp_path, repo):
    """Kills M4 (no charge returned in PLANNING), M6 (no timer)."""
    ctrl, state, store, fake = build(tmp_path, repo)
    fake.planner_raise = answer_missing(QA_LIMIT, followed_up=True)
    out = ctrl.run_iteration(state)
    assert not out.accepted
    assert (state.condition, state.blocked_kind, state.stage) == (
        Condition.BLOCKED, "usage_limit", Stage.PLANNING)
    assert state.usage.iterations == 0
    assert "charge returned" in history(state)
    [timer] = armed(store)
    assert timer.reason.startswith("usage limit: ")
    done = heal_and_resume(ctrl, state, store, fake)
    assert done.accepted
    assert (state.iteration, state.usage.iterations) == (1, 1)


# --------------------------------------------------------------------------- #
# K3 -- a prompt stall is an automatic wait
# --------------------------------------------------------------------------- #

def test_planner_prompt_stall_under_a_digest_contract_is_an_automatic_wait(tmp_path, repo):
    """Kills M3 (the wait routed through the contract), M4, M6, N6 (timer reason
    always `usage limit: `): qgg's planner stall was a non-delegable decision."""
    ctrl, state, store, fake = build(tmp_path, repo, contract=pa_contract())
    stall = DispatchError(STALL, transient=True, retry_after=0.0)
    fake.planner_raise = stall
    out = ctrl.run_iteration(state)
    assert fake.calls.count("planner") == 1 + state.budgets.max_transient_retries
    assert out.reason.startswith("no verdict")
    assert (state.condition, state.blocked_kind) == (Condition.BLOCKED, "prompt_stalled")
    assert state.retry_after
    text = history(state)
    assert "COLLECT_FOR_DIGEST" not in text and "unclassified" not in text
    assert bind(store.dir).routings() == []
    [timer] = armed(store)
    assert timer.reason.startswith("prompt stalled: ")
    assert state.usage.iterations == 0


def test_developer_prompt_stall_is_an_automatic_wait_and_keeps_the_iteration(tmp_path, repo):
    """Kills M3, M6: a developer stall blocks in DEVELOPING and resumes at (1, 1)."""
    ctrl, state, store, fake = build(tmp_path, repo)
    fake.developer_raise = DispatchError(
        STALL.replace("planner dispatch", "developer dispatch"), transient=True, retry_after=0.0)
    out = ctrl.run_iteration(state)
    assert out.reason.startswith("no verdict")
    assert (state.condition, state.blocked_kind, state.stage) == (
        Condition.BLOCKED, "prompt_stalled", Stage.DEVELOPING)
    done = heal_and_resume(ctrl, state, store, fake)
    assert done.accepted
    assert (state.iteration, state.usage.iterations) == (1, 1)


# --------------------------------------------------------------------------- #
# K4 -- a role timeout is an automatic wait that keeps the role's work
# --------------------------------------------------------------------------- #

def test_developer_role_timeout_is_an_automatic_wait_and_keeps_its_work(tmp_path, repo):
    """Kills M3, M6: dres's developer, still working after 3038 s, is not a
    decision for the digest, and nothing it wrote is touched."""
    ctrl, state, store, fake = build(tmp_path, repo, contract=pa_contract())

    def half_done(r: Path) -> None:
        (r / "partial.txt").write_text("half\n", encoding="utf-8")
        (r / "app.py").write_text("x = 2  # half done\n", encoding="utf-8")

    fake.develop = half_done
    fake.developer_raise = DispatchError(ROLE_TIME, transient=False)
    out = ctrl.run_iteration(state)
    assert out.reason.startswith("no verdict") and "ROLE_TIMEOUT" in out.reason
    assert (state.condition, state.blocked_kind, state.stage) == (
        Condition.BLOCKED, "role_timeout", Stage.DEVELOPING)
    assert state.retry_after and len(armed(store)) == 1
    text = history(state)
    assert "COLLECT_FOR_DIGEST" not in text and "unclassified" not in text
    assert bind(store.dir).routings() == []
    assert (repo / "partial.txt").exists()
    assert (repo / "app.py").read_text(encoding="utf-8") == "x = 2  # half done\n"

    done = heal_and_resume(ctrl, state, store, fake)
    assert done.accepted
    assert (state.iteration, state.usage.iterations) == (1, 1)
    assert (repo / "partial.txt").exists()
    assert (repo / "app.py").read_text(encoding="utf-8") == "x = 2  # half done\n"


# --------------------------------------------------------------------------- #
# K5 -- an operator-required class blocks for a person
# --------------------------------------------------------------------------- #

def test_operator_required_class_blocks_for_a_person(tmp_path, repo):
    """Kills M5 (AUTH_EXPIRED made automatic), M1: an expired credential is
    never woken by a timer."""
    ctrl, state, store, fake = build(tmp_path, repo)
    fake.qa_raise = answer_missing(AUTH, followed_up=True)
    out = ctrl.run_iteration(state)
    assert out.reason.startswith("no verdict") and "AUTH_EXPIRED" in out.reason
    assert (state.condition, state.stage) == (Condition.BLOCKED, Stage.VERIFYING)
    assert state.blocked_kind not in resumeloop.QUOTA_CLASSES
    assert armed(store) == []
    assert "candidate rejected" not in history(state)
    month = datetime.now(timezone.utc) + timedelta(days=30)
    for _ in range(2):
        recs = resumeloop.resume_runs(tmp_path / "runs", now=month)
        assert all(r["action"] != "resumed" for r in recs)
    assert store.read_state().condition is Condition.BLOCKED


def test_an_expired_credential_with_a_limit_on_screen_never_gets_a_timer(tmp_path, repo):
    """Kills M11 (precedence inverted), N1 (the provider's text in the reason):
    the pane shows a usage limit *and* an expired credential."""
    ctrl, state, store, fake = build(tmp_path, repo)
    fake.qa_raise = answer_missing(AUTH_AND_LIMIT, followed_up=True)
    out = ctrl.run_iteration(state)
    assert "AUTH_EXPIRED" in out.reason
    assert state.condition is Condition.BLOCKED
    assert state.blocked_kind != "usage_limit"
    assert not state.retry_after
    assert "limit" not in (state.blocked_reason or "").lower()
    assert armed(store) == []


# --------------------------------------------------------------------------- #
# K6 -- controls: today's routes stay
# --------------------------------------------------------------------------- #

def test_unclassified_qa_outage_is_still_an_unverified_rejection(tmp_path, repo):
    """A QA turn without an answer file and without a recognised cause still
    spends the iteration (UNCLASSIFIED is not fail-closed)."""
    ctrl, state, store, fake = build(tmp_path, repo)
    fake.qa_raise = answer_missing(NO_CAUSE, followed_up=True)
    out = ctrl.run_iteration(state)
    assert out.reason.startswith(stages.UNVERIFIED)
    assert (state.condition, state.stage) == (Condition.ACTIVE, Stage.PLANNING)


def test_waiting_for_approval_keeps_its_route(tmp_path, repo):
    """A pending dialog is an approval wait, whatever its pane shows."""
    ctrl, state, store, fake = build(tmp_path, repo)
    fake.qa_raise = WaitingForApproval(QA_LIMIT.replace(
        "qa ended its turn without writing the answer file", "qa waits"))
    out = ctrl.run_iteration(state)
    assert out.waiting_for_approval
    assert out.reason.startswith(stages.UNVERIFIED)


def test_a_pane_not_clean_keeps_its_own_route(tmp_path, repo):
    """Kills M20 (`PaneNotClean` dropped from the not-infrastructure types)."""
    ctrl, state, store, fake = build(tmp_path, repo)
    fake.qa_raise = PaneNotClean(QA_LIMIT.replace(
        "qa ended its turn without writing the answer file", "qa pane is not clean"))
    with pytest.raises(PaneNotClean):
        ctrl.run_iteration(state)
    assert state.condition is Condition.ACTIVE and state.blocked_kind is None
    assert armed(store) == []
    assert "failure class" not in history(state)


def test_a_run_held_by_a_capability_violation_is_never_given_an_automatic_wait(tmp_path, repo):
    """Kills N2 (the automatic branch ignores `violation_unresolved`), M1."""
    ctrl, state, store, fake = build(tmp_path, repo)
    state.blocked_kind = stages.CAPABILITY_VIOLATION
    store.write_state(state)
    fake.qa_raise = answer_missing(QA_LIMIT, followed_up=True)
    ctrl.run_iteration(state)
    assert state.condition is Condition.BLOCKED
    assert state.blocked_kind == stages.CAPABILITY_VIOLATION
    assert stages.violation_unresolved(state)
    assert armed(store) == []


# --------------------------------------------------------------------------- #
# K7 -- every failed dispatch records its class
# --------------------------------------------------------------------------- #

def _telemetry(store: RunStore) -> list[dict]:
    path = store.dir / "telemetry.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_telemetry_records_the_normalized_class(tmp_path, repo):
    """The record carries both the A5 class and the taxonomy class it maps to."""
    ctrl, state, store, fake, _ = _qa_limit_wait(tmp_path, repo)
    lines = _telemetry(store)
    [qa] = [ln for ln in lines if ln["role"] == "qa" and ln["outcome"] != "ok"]
    assert (qa["outcome"], qa["normalized_failure"], qa["failure_class"]) == (
        "failed", "USAGE_LIMIT", "PROVIDER_QUOTA")
    assert all(ln["normalized_failure"] is None for ln in lines if ln["outcome"] == "ok")

    ctrl2, state2, store2, fake2 = build(tmp_path / "second", _fresh_repo(tmp_path / "second"))
    fake2.planner_raise = DispatchError(STALL, transient=True, retry_after=0.0)
    ctrl2.run_iteration(state2)
    [planner] = [ln for ln in _telemetry(store2) if ln["role"] == "planner"
                 and ln["outcome"] != "ok"]
    assert planner["normalized_failure"] == "PROMPT_STALLED"


def _fresh_repo(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    r = base / "project"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.email", "t@example.invalid")
    git(r, "config", "user.name", "T")
    (r / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


# --------------------------------------------------------------------------- #
# K8 -- classification and the table
# --------------------------------------------------------------------------- #

def _pane_case(*lines: str) -> str:
    return pane("qa ended its turn without writing the answer file", *lines)


CASES = [
    ("qa-limit-pcon", QA_LIMIT, NF.USAGE_LIMIT),
    ("stall-qgg", STALL, NF.PROMPT_STALLED),
    ("no-cause", NO_CAUSE, NF.UNCLASSIFIED),
    ("auth-banner", AUTH, NF.AUTH_EXPIRED),
    ("limit-and-auth", _pane_case("● Usage limit reached", "⎿  Invalid API key"),
     NF.AUTH_EXPIRED),
    ("limit-beats-stall", pane(STALL.splitlines()[0], "⚠ Usage limit reached"),
     NF.USAGE_LIMIT),
    ("overloaded", _pane_case(
        '⎿  API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}'),
     NF.PROVIDER_UNAVAILABLE),
    ("api-429", _pane_case("⎿  API Error: 429 too fast"), NF.RATE_LIMIT),
    ("network", _pane_case("⎿  API Error: Connection error."), NF.NETWORK_TRANSIENT),
    ("credit", _pane_case("⎿  Credit balance is too low"), NF.ACCOUNT_ACTION_REQUIRED),
    ("model", _pane_case("⎿  There's an issue with the selected model (x)."),
     NF.MODEL_UNAVAILABLE),
    ("harness-missing", "claude could not be started: [Errno 2] No such file",
     NF.LOCAL_RUNTIME_UNAVAILABLE),
    ("subprocess-limit",
     "claude exited with 1 and without output: Claude AI usage limit reached|1759600000",
     NF.USAGE_LIMIT),
    ("role-timeout-dres", ROLE_TIME, NF.ROLE_TIMEOUT),
    ("wrapper-timeout", "qa dispatch failed: Command 'herdr' timed out after 7200 seconds",
     NF.ROLE_TIMEOUT),
    ("harness-timeout", "developer on claude gave no answer after 2700s", NF.ROLE_TIMEOUT),
    ("limit-beats-timeout", pane(ROLE_FIRST, "⎿  Running… (7m 7s · timeout 10m)",
                                 "⚠ Usage limit reached"), NF.USAGE_LIMIT),
    # Negative controls: words on a screen that say nothing about the dispatch.
    ("code-literal", _pane_case('    re.compile(r"usage limit reached", re.I),'),
     NF.UNCLASSIFIED),
    ("dict-literal", _pane_case('    "why": "usage limit reached",'), NF.UNCLASSIFIED),
    ("diff-line", _pane_case("+ Usage limit reached is now detected"), NF.UNCLASSIFIED),
    ("tool-401", _pane_case("⎿  401 Unauthorized"), NF.UNCLASSIFIED),
    ("pane-enoent", _pane_case("⎿  ls: cannot access x: No such file or directory"),
     NF.UNCLASSIFIED),
    ("pane-timeout-word", _pane_case("⎿  Running… (7m 7s · timeout 10m)",
                                      "Error: timed out after 30 seconds"), NF.UNCLASSIFIED),
    ("prose-rate-limiting", _pane_case("● Rate limiting is not implemented in app.py"),
     NF.UNCLASSIFIED),
    ("prose-connection-handling",
     _pane_case("● Connection error handling is missing: K2"), NF.UNCLASSIFIED),
    ("pytest-assertion", _pane_case("E   AssertionError: quota exceeded"), NF.UNCLASSIFIED),
    ("first-line-longer-word", "qa reported: connection errors were counted in the log",
     NF.UNCLASSIFIED),
    # P2-43b: a role's own words that start with a known phrase are not a wait.
    ("role-first-line-quota", "qa reported: quota exceeded handling is missing",
     NF.UNCLASSIFIED),
    ("role-first-line-rate", "developer noted: rate limit handling is missing",
     NF.UNCLASSIFIED),
    ("role-pane-rate-colon", _pane_case("● Rate limit: cap at 100/min"), NF.UNCLASSIFIED),
    ("role-pane-quota-comma", _pane_case("Quota exceeded, see the table below"),
     NF.UNCLASSIFIED),
    ("role-pane-sentence", _pane_case("Rate limit. We should add a backoff."),
     NF.UNCLASSIFIED),
    ("role-pane-auth-colon", _pane_case("Authentication required: the route needs a guard"),
     NF.UNCLASSIFIED),
    ("role-pane-api-error-prose", _pane_case("● the API Error: 500 handler is missing"),
     NF.UNCLASSIFIED),
    ("role-pane-api-error-mid", _pane_case("● Fix: API Error: 503 is not retried"),
     NF.UNCLASSIFIED),
    ("role-first-line-after-colon", "qa wrote: usage limit reached", NF.UNCLASSIFIED),
    # P2-43b revision: the wrapper words are anchored at the start of the line, and
    # this project's own sentences and codes are read only where it writes them.
    ("role-failed-colon", "qa reported: the test failed: quota exceeded", NF.UNCLASSIFIED),
    ("role-build-failed", "qa reported: build failed: rate limit", NF.UNCLASSIFIED),
    ("role-own-sentence-mid-line",
     "qa reported: herdr cli not on the path handling is missing", NF.UNCLASSIFIED),
    ("role-timeout-code-mid-line",
     'qa reported: the {"error":{"code":"timeout"}} branch is wrong', NF.UNCLASSIFIED),
    ("role-timeout-word-mid-line",
     "qa reported: Command x timed out after 5 seconds in the retry", NF.UNCLASSIFIED),
    ("role-gave-no-answer", "qa reported: developer on claude gave no answer after 5s",
     NF.UNCLASSIFIED),
    ("wrapper-create-tab", "create tab failed: Too many requests", NF.RATE_LIMIT),
    ("wrapper-start-agent", "start agent a1 failed: Overloaded", NF.PROVIDER_UNAVAILABLE),
    ("wrapper-herdr-stalled",
     'developer dispatch failed: {"error":{"code":"agent_prompt_stalled"}}', NF.PROMPT_STALLED),
    # A RESIDUAL, pinned: a first line that starts with the wrapper and whose
    # remainder is *exactly* a banner phrase (a role's `quota exceeded` and
    # nothing more) still reads as provider text. No raise site
    # builds such a line from role output (the contract failure is an own
    # sentence, checked first); the cost is bounded by the resume attempts.
    ("residual-wrapper-then-role-words", "qa dispatch failed: quota exceeded", NF.USAGE_LIMIT),
    ("wrapper-then-longer-role-words", "qa dispatch failed: quota exceeded handling is missing",
     NF.UNCLASSIFIED),
    # RESIDUALS, pinned (P2-43b integration review): text that a role could also
    # write and that is still read as provider text. Bounded by the resume
    # attempts; the alternative (reading only a measured banner position) needs
    # data the product has not collected. If one of these changes, say so.
    ("residual-pane-bare-banner", _pane_case("Rate limit."), NF.RATE_LIMIT),
    ("residual-pane-role-banner-lead", _pane_case("● Usage limit reached"), NF.USAGE_LIMIT),
    ("residual-pane-api-error-prose", _pane_case("API Error: 503 is not retried by our client"),
     NF.PROVIDER_UNAVAILABLE),
    ("residual-any-failed-purpose", "the test failed: Usage limit reached", NF.USAGE_LIMIT),
    ("residual-pane-quota-exceeded", _pane_case("quota exceeded"), NF.USAGE_LIMIT),
    ("residual-pane-too-many-requests", _pane_case("Too many requests"), NF.RATE_LIMIT),
    ("residual-pane-rate-limited", _pane_case("rate limited"), NF.RATE_LIMIT),
    # residual-pane-kontingent (the German phrase of the list) lives in its own test,
    # test_a_german_banner_in_a_pane_is_still_read_as_provider_text, below.
    ("residual-pane-banner-with-trail", _pane_case("Usage limit reached · resets 5pm"),
     NF.USAGE_LIMIT),
    ("residual-pane-connection-error", _pane_case("Connection error."), NF.NETWORK_TRANSIENT),
    # ... while the provider's text, carried by this project's wrappers or
    # printed as a banner, still is.
    ("handover-failed", "qa dispatch failed: Too many requests", NF.RATE_LIMIT),
    ("handover-exited", "claude exited with 1 and without output: Overloaded", NF.PROVIDER_UNAVAILABLE),
    ("banner-metadata-trail", _pane_case("● Usage limit reached | 1759600000"),
     NF.USAGE_LIMIT),
    ("banner-not-a-trail", _pane_case("● Usage limit reached, the build is red"),
     NF.UNCLASSIFIED),
    ("banner-rate-limited-eol", _pane_case("⎿  rate limited"), NF.RATE_LIMIT),
    ("banner-final-stop", _pane_case("⎿  Connection error."), NF.NETWORK_TRANSIENT),
    ("banner-api-error-lead", _pane_case("⎿  API Error: 500 internal server error"),
     NF.PROVIDER_UNAVAILABLE),
    # A ten-dash line of the role's own does not end the pane block.
    ("ten-dash-rule", _pane_case("----------", "● Usage limit reached · resets 3:20am"),
     NF.USAGE_LIMIT),
    ("ten-dash-is-no-evidence", _pane_case("----------", "● all good"), NF.UNCLASSIFIED),
    ("after-the-block", pane(FIRST, "(empty)") + "\n● Usage limit reached", NF.UNCLASSIFIED),
    # This project's own sentences.
    ("own-capability", "qa capability violation: quota exceeded", NF.NOT_INFRASTRUCTURE),
    ("own-approval-wait", "qa waits for an approval: usage limit reached",
     NF.NOT_INFRASTRUCTURE),
    ("own-needs-approval", "developer needs approval: rate limit", NF.NOT_INFRASTRUCTURE),
    ("budget", "dispatch refused: dispatch budget exhausted (9/9)", NF.NOT_INFRASTRUCTURE),
    ("contract", "planner violates the output contract even after the repair; "
                 "the repair budget is exhausted: bad", NF.NOT_INFRASTRUCTURE),
    ("contract-with-quota-text",
     "planner violates the output contract even after the repair; the repair budget "
     "is exhausted: quota exceeded", NF.NOT_INFRASTRUCTURE),
]


@pytest.mark.parametrize("text,expected", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_classification(text, expected):
    """Kills M7 (no terminal-boundary rule: `prose-connection-handling`), M11
    (precedence), N3 (own sentences not checked first: `contract-with-quota-text`),
    N8 (pane phrases after any ': ': `pytest-assertion`), N9 (no word boundary:
    `first-line-longer-word`)."""
    assert failures.classify(DispatchError(text)) is expected


@pytest.mark.parametrize("banner", ["Kontingent erschoepft"], ids=["residual-pane-kontingent"])
def test_a_german_banner_in_a_pane_is_still_read_as_provider_text(banner):
    """The pinned residual for the German phrase of the list (`kontingent
    erschoepft|aufgebraucht`, which provider CLIs print): a pane line that is exactly
    it is provider text, whoever wrote it -- the same as the English phrases in the
    `residual-*` cases of `test_classification`.

    Kept out of that module-level table on purpose. `tests/test_source_hygiene.py`
    scans module-level string tables for German prose, because a German *expectation*
    about an English source passes vacuously. This is German *input* with an English
    expectation about it (the enum), and it fails the moment the classifier stops
    reading the phrase -- the form `tests/test_phase2.py::test_quota_detection` already
    uses for the same phrase. Kills Q23 (the German phrase dropped from the list)."""
    assert failures.classify(DispatchError(_pane_case(banner))) is NF.USAGE_LIMIT


@pytest.mark.parametrize("status,expected", [
    (401, NF.AUTH_EXPIRED), (402, NF.ACCOUNT_ACTION_REQUIRED), (403, NF.AUTH_EXPIRED),
    (404, NF.MODEL_UNAVAILABLE), (429, NF.RATE_LIMIT), (500, NF.PROVIDER_UNAVAILABLE),
    (502, NF.PROVIDER_UNAVAILABLE), (503, NF.PROVIDER_UNAVAILABLE),
    (504, NF.PROVIDER_UNAVAILABLE), (529, NF.PROVIDER_UNAVAILABLE),
    (400, NF.UNCLASSIFIED), (418, NF.UNCLASSIFIED), (200, NF.UNCLASSIFIED)])
def test_the_http_status_table(status, expected):
    """A row of `_STATUS_CLASS` dropped or swapped is caught here: a status counts
    on a line that starts with `API Error`, in the pane and in the first line."""
    assert failures.classify(DispatchError(
        _pane_case(f"⎿  API Error: {status} {{\"type\":\"error\"}}"))) is expected
    assert failures.classify(DispatchError(
        f"qa dispatch failed: API Error: {status} x")) is expected


_BANNER = {  # one line each, in the form a harness prints it
    NF.AUTH_EXPIRED: "⎿  Invalid API key · Please run /login",
    NF.ACCOUNT_ACTION_REQUIRED: "⎿  Credit balance is too low",
    NF.MODEL_UNAVAILABLE: "⎿  There's an issue with the selected model (x).",
    NF.USAGE_LIMIT: "⚠ Usage limit reached · limit resets 3:20am",
    NF.RATE_LIMIT: "⎿  API Error: 429 too many requests",
    NF.PROVIDER_UNAVAILABLE: "⎿  API Error: 529 overloaded_error",
    NF.NETWORK_TRANSIENT: "⎿  API Error: Connection error.",
}
_FIRST_ONLY = {  # classes read from the failure's own first line only
    NF.LOCAL_RUNTIME_UNAVAILABLE: "herdr cli not on the path",
    NF.ROLE_TIMEOUT: '{"error":{"code":"timeout"}}',
    NF.PROMPT_STALLED: '{"error":{"code":"agent_prompt_stalled"}}',
}


def _evidence(*classes: NF) -> str:
    first = "developer dispatch failed: " + " ".join(
        _FIRST_ONLY[c] for c in classes if c in _FIRST_ONLY)
    return pane(first.strip(), *[_BANNER[c] for c in classes if c in _BANNER])


#: Written out here, not read from the module: a table read from the module
#: follows any mutation of it. Operator-required first (an automatic retry
#: against an expired credential fails identically), then the cause before its
#: symptom (a timeout or a stall with a usage limit on screen is the usage limit).
PRECEDENCE = (NF.AUTH_EXPIRED, NF.ACCOUNT_ACTION_REQUIRED, NF.MODEL_UNAVAILABLE,
              NF.LOCAL_RUNTIME_UNAVAILABLE, NF.USAGE_LIMIT, NF.RATE_LIMIT,
              NF.PROVIDER_UNAVAILABLE, NF.NETWORK_TRANSIENT, NF.ROLE_TIMEOUT,
              NF.PROMPT_STALLED)


#: Two classes that are read from the first line only cannot both be on the
#: screen: the first line is one wrapper and one message.
_PAIRS = [(a, b) for i, a in enumerate(PRECEDENCE) for b in PRECEDENCE[i + 1:]
          if not (a in _FIRST_ONLY and b in _FIRST_ONLY)]


@pytest.mark.parametrize("winner,loser", _PAIRS, ids=lambda c: c.value)
def test_class_precedence_is_total(winner, loser):
    """Kills M11, H4 and every smaller swap of the precedence: whichever two
    classes the evidence names, the earlier one of `PRECEDENCE` wins, in both
    orders of appearance."""
    assert failures.classify(DispatchError(_evidence(winner, loser))) is winner
    assert failures.classify(DispatchError(_evidence(loser, winner))) is winner


def test_text_a_role_wrote_in_the_pane_never_parks_a_run(tmp_path, repo):
    """Kills H1 and H2 at the controller: a failed QA turn
    whose pane shows the role's own `Rate limit: ...` keeps today's route -- an
    unverified rejection, no wait, no timer, no receipt."""
    ctrl, state, store, fake = build(tmp_path, repo)
    fake.qa_raise = answer_missing(
        pane(FIRST, "● Rate limit: cap at 100/min", "● Quota exceeded, see the table"),
        followed_up=True)
    out = ctrl.run_iteration(state)
    assert out.reason.startswith(stages.UNVERIFIED)
    assert state.condition is Condition.ACTIVE and state.blocked_kind is None
    assert armed(store) == []
    assert "failure class" not in history(state)


def _run_through_the_cli(tmp_path, repo, monkeypatch, fake, *, iterations="1"):
    """`hoh run` itself -- the real parser, `_cmd_run` and its generic abort
    handler -- on a fake dispatcher: no Herdr, no agent, no host storage root.
    Returns (rc, state, store, stderr)."""
    import types

    from hoh import cli, dispatchers, integrity
    from hoh import controller as controller_module

    ctrl, state, store, _ = build(tmp_path, repo, fake=fake)
    monkeypatch.setattr(cli, "storage_admission_for", lambda option, **kw: (
        types.SimpleNamespace(ledger=tmp_path / "ledger"), tmp_path / "storage"))
    monkeypatch.setattr(cli, "_hook_steward", lambda *a, **kw: None)
    monkeypatch.setattr(integrity, "snapshot",
                        lambda repo_path: (_ for _ in ()).throw(RuntimeError("not measured")))

    class NoAdmission(controller_module.Controller):
        def __init__(self, *a, **kw):
            kw["storage_admission"] = None
            super().__init__(*a, **kw)

    monkeypatch.setattr(controller_module, "Controller", NoAdmission)
    monkeypatch.setattr(dispatchers, "build_dispatcher", lambda **kw: fake)
    rc = cli.main(["--root", str(tmp_path / "runs"), "run", "r1", "--no-herdr",
                   "--iterations", iterations])
    return rc, store.read_state(), store


@pytest.mark.parametrize("role", ["planner", "developer", "qa"])
def test_a_role_s_words_never_park_a_run_through_the_cli(tmp_path, repo, monkeypatch, capsys, role):
    """Kills Q1 (the generic abort handler hands the exception's text to the quota
    detector) and Q2/Q17 (the flag ignored or not passed on): a role's `Rate limit: cap at 100/min` in the pane of a failed
    planner, developer or QA turn is not a wait on the real `hoh run` path -- no
    `usage_limit`, no `retry_after`, no timer, no receipt, and `hoh resume-quota`
    resumes nothing. (P2-43b integration review B1: the developer and planner
    were parked for an hour by `stages.block`'s phrase search.)"""
    from hoh import resumeloop

    fake = Fake(repo)
    text = pane(f"{role} ended its turn without writing the answer file",
                "● Rate limit: cap at 100/min")
    assert failures.classify(DispatchError(text)) is NF.UNCLASSIFIED
    setattr(fake, f"{role}_raise", DispatchError(text))
    rc, state, store = _run_through_the_cli(tmp_path, repo, monkeypatch, fake)
    capsys.readouterr()
    assert state.blocked_kind is None and not state.retry_after
    assert armed(store) == []
    assert not (store.dir / "recovery" / "routing.jsonl").exists()
    assert "BLOCKED (quota" not in history(state)
    if role != "qa":
        assert rc == 1 and state.condition is Condition.BLOCKED
        assert "iteration aborted" in (state.blocked_reason or "")
    else:
        assert state.condition is Condition.ACTIVE            # today's unverified rejection
    records = resumeloop.resume_runs(tmp_path / "runs",
                                     now=datetime.now(timezone.utc) + timedelta(hours=30))
    assert all(r.get("action") not in ("resumed", "resumed_without_timer") for r in records)


def test_a_provider_banner_still_parks_a_developer_run_through_the_cli(
        tmp_path, repo, monkeypatch, capsys):
    """The control for the test above: the real banner, same path, same fake, is
    a wait with a receipt and a timer -- the fix removed the false positive, not
    the detection."""
    fake = Fake(repo)
    fake.developer_raise = DispatchError(
        pane("developer ended its turn without writing the answer file",
             "● Usage limit reached · continuing automatically at 3:20am · esc to cancel",
             "⎿  You've hit your session limit · resets 3:20am (Europe/Istanbul)"))
    rc, state, store = _run_through_the_cli(tmp_path, repo, monkeypatch, fake)
    capsys.readouterr()
    assert state.condition is Condition.BLOCKED and state.blocked_kind == "usage_limit"
    assert state.retry_after and len(armed(store)) == 1
    assert (store.dir / "recovery" / "routing.jsonl").is_file()


def test_an_exception_outside_the_classifier_is_never_read_as_a_quota_wait(
        tmp_path, repo, monkeypatch, capsys):
    """Kills Q1 for a non-dispatch exception whose text names a limit: it is shown
    (the block reason and stderr carry it) and blocks for a person. A limit
    worded in a way `hoh.failures` does not read is therefore no longer parked by
    accident of text (PINNED change; slice a's docstring called it a tracked gap)."""
    fake = Fake(repo)
    fake.developer_raise = RuntimeError("HTTP 429 too many requests: rate limit")
    rc, state, store = _run_through_the_cli(tmp_path, repo, monkeypatch, fake)
    err = capsys.readouterr().err
    assert rc == 1 and state.condition is Condition.BLOCKED
    assert state.blocked_kind is None and not state.retry_after and armed(store) == []
    assert "too many requests" in (state.blocked_reason or "") and "too many requests" in err


def test_the_quota_scan_of_a_block_is_opt_out(tmp_path):
    """Kills Q2 at its root: `stages.block` still reads a quota phrase in a
    reason the product wrote (its other callers rely on it) and does not in one
    that carries an exception's text; a capability hold is kept either way."""
    spec = tmp_path / "s.md"
    spec.write_text(SPEC, encoding="utf-8")
    def fresh():
        return Controller.new_state(run_id="r1", repo_path=tmp_path, project_name="p",
                                    spec_path=spec)
    reason = "iteration aborted: Rate limit: cap at 100/min"
    parked = stages.block(fresh(), reason)
    assert parked.blocked_kind == "usage_limit" and parked.retry_after
    plain = stages.block(fresh(), reason, scan_for_quota=False)
    assert plain.condition is Condition.BLOCKED
    assert plain.blocked_kind is None and plain.retry_after is None
    held = fresh()
    held.blocked_kind = stages.CAPABILITY_VIOLATION
    assert stages.block(held, reason, scan_for_quota=False).blocked_kind == \
        stages.CAPABILITY_VIOLATION


def _seal_failure(exc):
    def fail(arena):
        raise exc
    return fail


@pytest.mark.parametrize("raised", [
    OSError(5, "stat failed: tests/test_rate_limit.py"),
    SealFailed("rate_limit.py has 2 hard links: a file with more than one name is not sealed"),
], ids=["planning-oserror", "hard-links"])
def test_a_file_name_a_role_chose_in_a_seal_failure_never_parks_a_run(
        tmp_path, repo, monkeypatch, raised):
    """Kills Q18 (the seal block scanning for a quota phrase): `capability.seal_plan`
    puts the role-created relative path into its message, and `rate_limit.py` is a
    quota phrase to `stages.block` -- the run was parked for an hour with a timer
    and no receipt (P2-43b-fix spec review A, F1). Now: a block for a person."""
    from hoh import capability

    monkeypatch.setattr(capability, "seal_plan", _seal_failure(raised))
    ctrl, state, store, fake = build(tmp_path, repo)
    out = ctrl.run_iteration(state)
    assert state.condition is Condition.BLOCKED
    assert "could not be sealed" in (state.blocked_reason or "")
    assert "rate_limit" in (state.blocked_reason or "")           # shown, not read
    assert state.blocked_kind is None and not state.retry_after and armed(store) == []
    assert not (store.dir / "recovery" / "routing.jsonl").exists()
    assert not out.accepted and "BLOCKED (quota" not in history(state)


def test_a_file_name_a_role_chose_in_a_binding_violation_never_parks_a_run(tmp_path, repo):
    """Kills Q19 (the binding block scanning for a quota phrase)."""
    ctrl, state, store, fake = build(tmp_path, repo)
    fake.qa_raise = stages.FreezeError("The working tree was modified during QA: "
                                       "tests/test_rate_limit.py")
    ctrl.run_iteration(state)
    assert state.condition is Condition.BLOCKED
    assert "candidate binding violated" in (state.blocked_reason or "")
    assert state.blocked_kind is None and not state.retry_after and armed(store) == []


def test_the_controller_block_is_opt_out_and_scans_by_default(tmp_path, repo):
    """Kills Q20 (`Controller._block` ignoring the flag) and its inverse: the
    product's own reasons are still read for a quota phrase."""
    ctrl, state, store, fake = build(tmp_path, repo)
    ctrl._block(state, "iteration aborted: Rate limit: cap at 100/min", scan_for_quota=False)
    assert state.blocked_kind is None and not state.retry_after
    ctrl2, state2, store2, _ = build(tmp_path / "second", _fresh_repo(tmp_path / "second"))
    ctrl2._block(state2, "Usage limit reached")
    assert state2.blocked_kind == "usage_limit" and state2.retry_after


def test_the_failure_class_is_on_the_record(tmp_path, repo):
    """The two history notes of a wait: the class with its mode and role, and
    the block with its kind and earliest retry."""
    ctrl, state, store, fake, _ = _qa_limit_wait(tmp_path, repo)
    text = history(state)
    assert "failure class USAGE_LIMIT (AUTOMATIC) in qa: qa ended its turn" in text
    assert "BLOCKED (usage_limit, earliest retry " in text


def test_classification_by_type():
    """Kills M20: a pane that is not provably clean keeps its own route whatever
    its text shows; a missing Herdr is a missing local runtime."""
    from hoh.herdr import HerdrUnavailable

    assert failures.classify(WaitingForApproval(QA_LIMIT)) is NF.NOT_INFRASTRUCTURE
    assert failures.classify(PaneNotClean(QA_LIMIT)) is NF.NOT_INFRASTRUCTURE
    assert failures.classify(RoleNotQuiescent(QA_LIMIT)) is NF.NOT_INFRASTRUCTURE
    assert failures.classify(HerdrUnavailable("x")) is NF.LOCAL_RUNTIME_UNAVAILABLE


def test_retry_mode_table():
    """Kills M5 (AUTH_EXPIRED made automatic)."""
    a5 = {"USAGE_LIMIT", "RATE_LIMIT", "PROVIDER_UNAVAILABLE", "NETWORK_TRANSIENT",
          "AUTH_EXPIRED", "ACCOUNT_ACTION_REQUIRED", "MODEL_UNAVAILABLE",
          "LOCAL_RUNTIME_UNAVAILABLE"}
    assert a5 <= {c.name for c in NF}
    assert set(failures.RETRY_MODE) == set(NF)
    automatic = {c for c, m in failures.RETRY_MODE.items() if m is RetryMode.AUTOMATIC}
    assert automatic == {NF.USAGE_LIMIT, NF.RATE_LIMIT, NF.PROVIDER_UNAVAILABLE,
                         NF.NETWORK_TRANSIENT, NF.PROMPT_STALLED, NF.ROLE_TIMEOUT}
    assert {c for c, m in failures.RETRY_MODE.items()
            if m is RetryMode.OPERATOR_REQUIRED} == {
        NF.AUTH_EXPIRED, NF.ACCOUNT_ACTION_REQUIRED, NF.MODEL_UNAVAILABLE,
        NF.LOCAL_RUNTIME_UNAVAILABLE}
    assert failures.RETRY_MODE[NF.NOT_INFRASTRUCTURE] is RetryMode.NOT_APPLICABLE
    assert failures.RETRY_MODE[NF.UNCLASSIFIED] is RetryMode.NOT_APPLICABLE
    assert set(failures.TAXONOMY) == failures.INFRASTRUCTURE
    assert len(failures.INFRASTRUCTURE) == 10
    for c in failures.INFRASTRUCTURE:
        assert failures.is_infrastructure(c)
        assert failures.is_automatic(c) == taxonomy.auto_resumable(failures.TAXONOMY[c])
    assert not failures.is_infrastructure(NF.UNCLASSIFIED)
    assert failures.TAXONOMY[NF.USAGE_LIMIT] is taxonomy.FailureClass.PROVIDER_QUOTA
    assert failures.AUTOMATIC_KINDS == {c.value.lower() for c in automatic}
    assert failures.blocked_kind(NF.ROLE_TIMEOUT) == "role_timeout"


def test_next_attempt_is_in_the_future():
    """Kills M5, N4 (ROLE_TIMEOUT shortened to 1 min), N5 (a stated reset time
    ignored for a usage limit)."""
    now = datetime(2026, 10, 4, 21, 19, tzinfo=timezone.utc)
    for c in failures.INFRASTRUCTURE:
        if failures.is_automatic(c):
            assert failures.next_attempt(c, QA_LIMIT, now=now) > now
    with pytest.raises(ValueError):
        failures.blocked_kind(NF.AUTH_EXPIRED)
    minutes = {NF.RATE_LIMIT: 5, NF.PROVIDER_UNAVAILABLE: 5, NF.PROMPT_STALLED: 5,
               NF.NETWORK_TRANSIENT: 2, NF.ROLE_TIMEOUT: 15}
    for c, m in minutes.items():
        assert failures.next_attempt(c, "x", now=now) - now == timedelta(minutes=m)
    assert failures.next_attempt(NF.USAGE_LIMIT, "limit resets at 11pm", now=now) == datetime(
        2026, 10, 4, 23, 0, tzinfo=timezone.utc)
    assert failures.next_attempt(NF.USAGE_LIMIT, "no time named", now=now) == now + timedelta(hours=1)


# --------------------------------------------------------------------------- #
# K9 -- resume-quota and the launcher know the automatic kinds
# --------------------------------------------------------------------------- #

PAST = "2026-09-25T09:00:00Z"
NOW = "2026-09-25T10:00:00Z"


def test_resume_quota_owns_the_automatic_kinds(tmp_path):
    """Kills M8 (`QUOTA_CLASSES` without the automatic kinds) and a
    `WAITING_KINDS` that lacks them (the resumption table reads `NEEDS_OVERRIDE`)."""
    assert failures.AUTOMATIC_KINDS
    for kind in sorted(failures.AUTOMATIC_KINDS):
        root = tmp_path / kind
        store = RunStore(root, "w")
        store.write_state(RunState(
            run_id="w", repo_path="/x", project_name="p", spec_path="/x/s.md",
            spec_digest="a" * 16, policy_digest="b" * 16, profile_digest="c" * 16,
            condition=Condition.BLOCKED, blocked_kind=kind, blocked_reason=f"{kind} in qa",
            retry_after="2026-09-25T09:00:00+00:00"))
        TimerStore(root).arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=PAST,
                                 program_epoch=0, run_id="w")
        [rec] = resumeloop.resume_runs(root, now=NOW)
        assert rec["action"] == "resumed", (kind, rec)
        assert store.read_state().condition is not Condition.BLOCKED


def test_launcher_reads_an_automatic_kind_as_a_provider_wait(tmp_path):
    """Kills M9 (`PROVIDER_BLOCKED` without the automatic kinds): a run waiting
    for time is not UNDETERMINED."""
    from hoh.launcher import HohRunLauncher
    from hoh.orchestrator import RunVerdict

    class Empty:
        exit_code = 0
        stdout = ""
        stderr = ""

    for kind in sorted(failures.AUTOMATIC_KINDS):
        state = RunState(
            run_id="r", repo_path=str(tmp_path), project_name="p",
            spec_path=str(tmp_path / "spec.md"), spec_digest="s" * 16,
            policy_digest="p" * 16, profile_digest="f" * 16,
            stage=Stage.VERIFYING, condition=Condition.BLOCKED, blocked_kind=kind,
            blocked_reason=f"{kind} in qa")
        result = HohRunLauncher(tmp_path, tmp_path)._verdict(state, None, Empty())
        assert result.verdict is RunVerdict.PROVIDER_UNAVAILABLE, kind
