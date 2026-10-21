"""P2-43 slice c: a park is resumed only if it is the park the receipts say it is.

Slice b wrote the binding of every park (programme, run, iteration, attempt,
stage, candidate, master) into a digest-bound receipt; nothing read it. These
tests drive the same real controller on the same fakes (no Herdr, no agent,
nothing outside `tmp_path`) and then the resume side that now reads it:
`hoh.dispatchrecovery.verify_binding`, called by `hoh.resumeloop` before a park
is lifted, and the four scenarios of amendment A5 end to end through
`Controller`, `resumeloop` and `hoh.cli.main(["resume-quota"])`.

Mutant ids in a docstring (`C1`..) are the spec's table of mutations the
integration review applies; each test says which it kills.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_failure_classes import (
    AUTH,
    FIRST,
    QA_LIMIT,
    _fresh_repo,
    answer_missing,
    armed,
    build,
    history,
    pa_contract,
    pane,
)

from hoh import cli, dispatchrecovery, quota, resumeloop
from hoh.contracts import Condition, RunState, Stage
from hoh.controller import Controller
from hoh.dispatchrecovery import (
    ParkVerdict,
    RecoveryError,
    RoutingDecision,
    RoutingEscalation,
)
from hoh.epoch import EpochStore
from hoh.orchestrator import node_timer_key
from hoh.project import Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.store import RunStore
from hoh.succession import MasterAuthority
from hoh.timers import TimerKind, TimerStore

@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The same project repository as every controller test of this suite."""
    return _fresh_repo(tmp_path)


PAST = "2026-09-25T09:00:00Z"
NOW = "2026-09-25T10:00:00Z"
RATE = pane(FIRST, "⎿  API Error: 429 too many requests")


def _later(hours: int = 30) -> datetime:
    """A moment after any reset time a banner can state (never more than a day)."""
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _park(tmp_path, repo, *, text=QA_LIMIT, contract=None, declare=None):
    """A real controller parks a QA dispatch; returns (ctrl, state, store, fake)."""
    ctrl, state, store, fake = build(tmp_path, repo, contract=contract)
    if declare is not None:
        dispatchrecovery.declare_master(store.dir, session=declare[0], epoch=declare[1])
    fake.qa_raise = answer_missing(text, followed_up=True)
    ctrl.run_iteration(state)
    return ctrl, state, store, fake


def _edit(store: RunStore, **changes) -> RunState:
    """The persisted state with some fields changed, written back whole."""
    st = store.read_state()
    for name, value in changes.items():
        setattr(st, name, value)
    store.write_state(st)
    return store.read_state()


def _project(tmp_path, *, run_id="r1", lifecycle=Lifecycle.RUNNING) -> ProjectStore:
    """A project under the runs' root with one node that runs `run_id`."""
    s = ProjectStore(tmp_path / "runs", "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=[
        TaskNode(id="a", run_id=run_id, lifecycle=lifecycle)]))
    return s


def _master(project: ProjectStore, session: str) -> MasterAuthority:
    m = MasterAuthority(project.dir, session, serialize=project.lock)
    m.acquire()
    return m


def _resume_via_the_cli(root, monkeypatch, capsys) -> dict:
    later = _later().strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(resumeloop, "_now_str", lambda now: later)
    # the double-down is on by default and keeps a dossier under dd/ of a blocked run: these tests
    # are about what the resume loop itself writes, so they opt out of it
    assert cli.main(["--root", str(root), "resume-quota", "--no-double-down"]) == 0
    return json.loads(capsys.readouterr().out)


def _tree(path: Path) -> dict[str, bytes]:
    """Every file of a run directory by relative name. Not `.lock`: taking the
    run's lock rewrites it (pid and time) whatever the pass then decides."""
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*"))
            if p.is_file() and p.name != ".lock"}


# --------------------------------------------------------------------------- #
# C1-C4 -- the binding is recomputed from the live state
# --------------------------------------------------------------------------- #

def test_a_park_the_receipts_describe_is_bound_and_verifying_writes_nothing(tmp_path, repo):
    """The positive control of every test below, and the proof that verifying is
    read-only: the run directory is byte-identical after it."""
    ctrl, state, store, fake = _park(tmp_path, repo)
    before = _tree(store.dir)
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.BOUND and check.resumes
    [rec] = dispatchrecovery.read_records(store.dir)[0]
    assert check.record == rec and check.park_id == rec.park_id
    assert _tree(store.dir) == before


def write_master_file(store, body: dict) -> None:
    (store.dir / "recovery").mkdir(exist_ok=True)
    (store.dir / "recovery" / "master.json").write_text(json.dumps(body), encoding="utf-8")


def _move_iteration(store):
    return _edit(store, iteration=store.read_state().iteration + 1)


def _move_attempt(store):
    return _edit(store, attempt=store.read_state().attempt + 1)


def _move_stage(store):
    return _edit(store, stage=Stage.DEVELOPING)


def _move_candidate(store):
    cand = store.read_state().working_candidate
    return _edit(store, working_candidate=cand.model_copy(update={"tree_digest": "f" * 16}))


def _pin_a_contract_afterwards(store):
    from hoh.governance import bind

    bind(store.dir, contract=pa_contract())
    return store.read_state()


def _declare_a_master_afterwards(store):
    """A hand-edited master file: `declare_master` itself refuses to move the master
    of a parked run (Revision 2), so the moved master is written the way a person or
    another tool would."""
    write_master_file(store, {"kind": "GOVERNED", "epoch": 1, "session": "sess-1"})
    return store.read_state()


MOVES = [
    ("iteration", _move_iteration, "iteration"),
    ("attempt", _move_attempt, "attempt"),
    ("stage", _move_stage, "stage"),
    ("candidate", _move_candidate, "candidate"),
    ("programme", _pin_a_contract_afterwards, "programme"),
    ("master", _declare_a_master_afterwards, "master"),
]


@pytest.mark.parametrize("move,field", [(m[1], m[2]) for m in MOVES], ids=[m[0] for m in MOVES])
def test_a_park_that_moved_since_its_receipt_is_refused_naming_the_field(
        tmp_path, repo, move, field):
    """Kills C3 (the receipt trusted, the binding not recomputed) and C4 (the
    master left out of the comparison): iteration, attempt, stage, candidate,
    programme and master are each part of the park's identity."""
    ctrl, state, store, fake = _park(tmp_path, repo)
    st = move(store)
    check = dispatchrecovery.verify_binding(store.dir, st)
    assert check.verdict is ParkVerdict.REFUSED and not check.resumes
    assert "stale or foreign park" in check.reason and field in check.reason


def test_a_park_copied_to_another_run_is_refused(tmp_path, repo):
    """A foreign park: the receipts of run r1 on the state of run r2."""
    ctrl, state, store, fake = _park(tmp_path, repo)
    other = tmp_path / "runs" / "r2"
    shutil.copytree(store.dir, other)
    text = (other / "state.json").read_text(encoding="utf-8")
    (other / "state.json").write_text(text.replace('"run_id": "r1"', '"run_id": "r2"'),
                                      encoding="utf-8")
    st = RunStore(tmp_path / "runs", "r2").read_state()
    assert st.run_id == "r2"
    check = dispatchrecovery.verify_binding(other, st)
    assert check.verdict is ParkVerdict.REFUSED and "run_id" in check.reason


def test_a_wait_no_receipt_describes_is_refused_even_though_the_run_has_receipts(
        tmp_path, repo):
    """A retry_after changed by hand, or a state from another time: the run's
    receipts exist and none is for this wait."""
    ctrl, state, store, fake = _park(tmp_path, repo)
    st = _edit(store, retry_after="2026-01-01T00:00:00+00:00")
    check = dispatchrecovery.verify_binding(store.dir, st)
    assert check.verdict is ParkVerdict.REFUSED and "other wait" in check.reason


def test_a_class_the_receipt_does_not_name_is_refused(tmp_path, repo):
    ctrl, state, store, fake = _park(tmp_path, repo)
    st = _edit(store, blocked_kind="rate_limit")
    check = dispatchrecovery.verify_binding(store.dir, st)
    assert check.verdict is ParkVerdict.REFUSED and "USAGE_LIMIT" in check.reason


def test_a_forged_park_id_with_a_matching_binding_is_refused(tmp_path, repo):
    """The digest is integrity, not authenticity: a line whose park_id is not
    the one its own binding and class give validates (the digest covers the
    forged body) and is still not a receipt."""
    ctrl, state, store, fake = _park(tmp_path, repo)
    path = store.dir / "recovery" / "routing.jsonl"
    line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    line.pop("digest")
    line["park_id"] = "0" * 32
    forged = json.dumps({"digest": dispatchrecovery._digest_of(line), **line}, sort_keys=True)
    path.write_text(forged + "\n", encoding="utf-8")
    records, problems = dispatchrecovery.read_records(store.dir)
    assert problems == [] and len(records) == 1
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.REFUSED and "park id" in check.reason


# --------------------------------------------------------------------------- #
# C5 -- what has no receipt
# --------------------------------------------------------------------------- #

def _bare_park(tmp_path, rid="q", **kw) -> RunStore:
    """A park of slice a: `usage_limit` and a `retry_after`, no receipt, no master."""
    store = RunStore(tmp_path, rid)
    store.write_state(RunState(
        run_id=rid, repo_path="/x", project_name="p", spec_path="/x/s.md",
        spec_digest="a" * 16, policy_digest="b" * 16, profile_digest="c" * 16,
        condition=Condition.BLOCKED, blocked_kind="usage_limit",
        retry_after="2026-09-25T09:00:00+00:00", **kw))
    return store


def test_a_park_of_slice_a_with_no_receipt_and_no_master_still_resumes(tmp_path):
    """The legacy policy, stated: no receipt and no declared master is the park
    every slice-a run made; it resumes exactly as before, and says so."""
    store = _bare_park(tmp_path)
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.LEGACY_UNBOUND and check.resumes
    TimerStore(tmp_path).arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=PAST, reason="t",
                                 program_epoch=0, run_id="q")
    [rec] = resumeloop.resume_runs(tmp_path, now=NOW)
    assert rec["action"] == "resumed" and rec["binding"] == "LEGACY_UNBOUND"
    assert "claim" not in rec
    assert "legacy park: no routing receipt" in "\n".join(store.read_state().history)


def test_a_governed_run_whose_park_has_no_valid_receipt_is_refused(tmp_path):
    """Kills C7 (the legacy rule applied to a governed run)."""
    store = _bare_park(tmp_path)
    write_master_file(store, {"kind": "GOVERNED", "epoch": 3, "session": "sess-1"})
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.REFUSED and "governed park" in check.reason


def test_unreadable_receipts_that_describe_nothing_refuse_a_bare_park(tmp_path):
    store = _bare_park(tmp_path)
    rec_dir = store.dir / "recovery"
    rec_dir.mkdir()
    (rec_dir / "routing.jsonl").write_text("{this is not json\n", encoding="utf-8")
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.REFUSED and "unreadable line" in check.reason


def test_a_deeply_nested_line_or_master_record_is_a_problem_not_a_crash(tmp_path, repo):
    """Folded in from the integration review of slice b (LOW, PROVEN): a
    hand-written file that nests a thousand levels deep raised RecursionError
    out of the reader. Kills C15 (RecursionError not caught)."""
    ctrl, state, store, fake = _park(tmp_path, repo)
    path = store.dir / "recovery" / "routing.jsonl"
    path.write_text("[" * 200000 + "\n", encoding="utf-8")
    records, problems = dispatchrecovery.read_records(store.dir)
    assert records == [] and len(problems) == 1 and "RecursionError" in problems[0]
    (store.dir / "recovery" / "master.json").write_text("[" * 200000, encoding="utf-8")
    with pytest.raises(RecoveryError):
        dispatchrecovery.read_master(store.dir)
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.REFUSED


SESSION_SECRETS = [
    "AbCdEf0123456789AbCdEf0123456789AbCdEf01",           # 40 characters, no prefix
    "3f2b8a1c9d4e4b7a8c1d5e6f7a8b9c0d3f2b8a1c",           # a 40-digit hex digest
    "my-secret-password-hunter2",
    "abcdefghijklmnopqrstuvwxyzabcdefgh",                 # a 34-letter word
    "prod-token-2",
    "sk-ant-api03-AbCdEf0123456789xyz",
    "user:pw@host//x",
]
SESSION_NAMES = ["0b8f3a52-9c41-4d27-a6e0-5f1d7c2e9a38", "resume-20261009T090000Z-4242",
                 "sess-7", "master:eu/team-1", "S1"]


@pytest.mark.parametrize("secret", SESSION_SECRETS)
def test_a_session_id_gets_the_same_secret_shape_checks_as_an_account_handle(secret):
    """Folded in from the integration review of slice b (MEDIUM, PROVEN):
    `check_session_id` applied only the prefix list, so a prefixless 40-character
    key was accepted and printed. Kills C16."""
    with pytest.raises(RecoveryError) as caught:
        dispatchrecovery.check_session_id(secret)
    assert secret not in str(caught.value)
    with pytest.raises(RecoveryError):
        dispatchrecovery.check_master_args(secret, 1)


@pytest.mark.parametrize("name", SESSION_NAMES)
def test_a_session_id_that_is_a_name_is_still_accepted(name):
    assert dispatchrecovery.check_session_id(name) == name


def test_a_park_that_cannot_be_verified_is_not_resumed(tmp_path, repo, monkeypatch):
    """Kills C14 (a failure while verifying reads as permission): a reader that
    raises, and a pinned contract that cannot be read, are each a refusal."""
    from hoh.governance import pinned_path

    ctrl, state, store, fake = _park(tmp_path, repo, contract=pa_contract())
    st = store.read_state()
    assert dispatchrecovery.verify_binding(store.dir, st).verdict is ParkVerdict.BOUND

    def unreadable(run_dir):
        raise OSError("input/output error")

    monkeypatch.setattr(dispatchrecovery, "read_records", unreadable)
    check = dispatchrecovery.verify_binding(store.dir, st)
    assert check.verdict is ParkVerdict.REFUSED and "OSError" in check.reason
    monkeypatch.undo()
    pinned_path(store.dir).write_text("not a contract", encoding="utf-8")
    check = dispatchrecovery.verify_binding(store.dir, st)
    assert check.verdict is ParkVerdict.REFUSED and "contract" in check.reason


# --------------------------------------------------------------------------- #
# C1, C2, C5, C6 -- the resume loop reads the binding
# --------------------------------------------------------------------------- #

def test_resume_runs_refuses_a_moved_park_and_leaves_state_timer_and_claims_alone(
        tmp_path, repo):
    """Kills C1 (the timer path never verifies): the refusal says why, the state
    file is byte-identical, the timer is still armed, no claim exists."""
    ctrl, state, store, fake = _park(tmp_path, repo)
    _move_iteration(store)
    before = _tree(store.dir)
    [rec] = resumeloop.resume_runs(tmp_path / "runs", now=_later())
    assert rec["action"] == "refused" and rec["binding"] == "REFUSED"
    assert "iteration" in rec["why"]
    assert _tree(store.dir) == before
    assert len(armed(store)) == 1 and not (store.dir / "recovery" / "claims").exists()
    assert store.read_state().condition is Condition.BLOCKED


def test_the_retry_after_fallback_refuses_a_moved_park_too(tmp_path, repo):
    """Kills C2 (the fallback path never verifies): with the timer withdrawn the
    run is resumed by its own retry_after, and that path asks the same question."""
    ctrl, state, store, fake = _park(tmp_path, repo)
    [timer] = armed(store)
    TimerStore(tmp_path / "runs").disarm(timer.timer_id, "test: the fallback path")
    _move_iteration(store)
    [rec] = resumeloop.resume_runs(tmp_path / "runs", now=_later())
    assert rec["verdict"] == "LEGACY_RETRY_AFTER" and rec["action"] == "refused"
    assert "iteration" in rec["why"]
    assert store.read_state().condition is Condition.BLOCKED


def test_a_bound_park_is_resumed_once_and_the_resume_leaves_its_claim(tmp_path, repo):
    """Kills C6 (no claim written): the claim is written after the lift and names
    the park, the attempt and the resumer."""
    ctrl, state, store, fake = _park(tmp_path, repo)
    [rec0] = dispatchrecovery.read_records(store.dir)[0]
    [rec] = resumeloop.resume_runs(tmp_path / "runs", now=_later())
    assert (rec["action"], rec["binding"], rec["claim"]) == ("resumed", "BOUND", "written")
    assert rec["park_id"] == rec0.park_id
    claim = dispatchrecovery.read_claim(store.dir, rec0.park_id)
    assert claim["kind"] == "PARK_RESUMED" and claim["park_id"] == rec0.park_id
    assert claim["resume_attempt"] == 1 and claim["resumer"].startswith("hoh resume-quota")
    assert store.read_state().condition is not Condition.BLOCKED
    assert f"park {rec0.park_id[:12]} verified" in "\n".join(store.read_state().history)


def test_a_park_restored_from_a_copy_cannot_be_lifted_a_second_time(tmp_path, repo):
    """Kills C5 (the claim not consulted): the state file is restored to its
    parked bytes and the timer re-armed, as after a restore from backup; the
    receipts and the claim survived, so the same park is refused."""
    ctrl, state, store, fake = _park(tmp_path, repo)
    parked = store.state_path.read_bytes()
    [first] = resumeloop.resume_runs(tmp_path / "runs", now=_later())
    assert first["action"] == "resumed"
    store.state_path.write_bytes(parked)
    TimerStore(tmp_path / "runs").arm_new(
        kind=TimerKind.PROVIDER_RETRY, due_at=PAST, reason="restored", program_epoch=0,
        run_id="r1")
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.REFUSED and "already resumed" in check.reason
    [second] = resumeloop.resume_runs(tmp_path / "runs", now=_later())
    assert second["action"] == "refused" and "already resumed" in second["why"]
    assert store.read_state().condition is Condition.BLOCKED


def test_a_claim_is_never_replaced(tmp_path, repo):
    ctrl, state, store, fake = _park(tmp_path, repo)
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    path = dispatchrecovery.claim_park(store.dir, check, resumer="a", attempt=1)
    first = path.read_bytes()
    with pytest.raises(FileExistsError):
        dispatchrecovery.claim_park(store.dir, check, resumer="b", attempt=2)
    assert path.read_bytes() == first
    with pytest.raises(ValueError):
        dispatchrecovery.claim_park(store.dir, dispatchrecovery.ParkCheck(
            ParkVerdict.LEGACY_UNBOUND, "x"), resumer="a", attempt=1)


# --------------------------------------------------------------------------- #
# C11 -- the resume-side escalation
# --------------------------------------------------------------------------- #

def test_a_park_given_up_leaves_one_retries_exhausted_escalation(tmp_path, repo):
    """Kills C11 (no escalation) and C12 (one more on every pass): after
    MAX_RESUME_ATTEMPTS the loop reports `given_up` and appends one
    RoutingEscalation that binds the same park; the timer path and the fallback
    path both reach it, and neither adds a second line."""
    ctrl, state, store, fake = _park(tmp_path, repo)
    _edit(store, resume_attempts=quota.MAX_RESUME_ATTEMPTS)
    [rec0] = dispatchrecovery.read_records(store.dir)[0]
    runs = tmp_path / "runs"
    [a] = resumeloop.resume_runs(runs, now=_later())
    assert a["action"] == "given_up" and a["escalation"]["escalation"] == "RETRIES_EXHAUSTED"
    [timer] = armed(store)
    TimerStore(runs).disarm(timer.timer_id, "test: the fallback path")
    [b] = resumeloop.resume_runs(runs, now=_later())
    assert b["action"] == "given_up" and b["escalation"]["park_id"] == rec0.park_id
    records, problems = dispatchrecovery.read_records(store.dir)
    assert problems == [] and len(records) == 2
    esc = records[1]
    assert isinstance(esc, RoutingEscalation) and esc.escalation == "RETRIES_EXHAUSTED"
    assert (esc.park_id, esc.binding, esc.reason) == (rec0.park_id, rec0.binding, "USAGE_LIMIT")
    assert esc.provenance == rec0.provenance
    assert store.read_state().condition is Condition.BLOCKED


def test_a_given_up_park_of_slice_a_has_no_receipt_to_extend(tmp_path):
    store = _bare_park(tmp_path, resume_attempts=quota.MAX_RESUME_ATTEMPTS)
    TimerStore(tmp_path).arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=PAST, reason="t",
                                 program_epoch=0, run_id="q")
    [rec] = resumeloop.resume_runs(tmp_path, now=NOW)
    assert rec["action"] == "given_up" and "escalation" not in rec
    assert not (store.dir / "recovery").exists()


# --------------------------------------------------------------------------- #
# C12 -- the project pass asks the same question before it spends an epoch
# --------------------------------------------------------------------------- #

def _parked_node(tmp_path, repo, *, timer: bool):
    """A project with node `a` (BLOCKED, usage_limit) whose run r1 is really parked."""
    ctrl, state, store, fake = _park(tmp_path, repo)
    s = ProjectStore(tmp_path / "runs", "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=[
        TaskNode(id="a", run_id="r1", lifecycle=Lifecycle.BLOCKED, blocked_kind="usage_limit",
                 retry_after=PAST)]))
    if timer:
        TimerStore(s.dir).arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=PAST, program_epoch=1,
                                  run_id=node_timer_key("a"), node_id="a")
    return s, store


@pytest.mark.parametrize("timer", [True, False], ids=["timer", "fallback"])
def test_a_node_whose_run_is_parked_on_a_moved_park_is_not_resumed_and_spends_no_epoch(
        tmp_path, repo, timer):
    """Kills C13 (the project pass never asks): the node stays BLOCKED, no master
    acquires an epoch for it, and the refusal names the field."""
    s, store = _parked_node(tmp_path, repo, timer=timer)
    _move_iteration(store)
    recs = [r for r in resumeloop.resume_projects(tmp_path / "runs", now=NOW)
            if r.get("node") == "a"]
    [rec] = recs
    assert rec["action"] == "refused" and rec["binding"] == "REFUSED"
    assert "iteration" in rec["why"]
    assert s.read_state().node("a").lifecycle is Lifecycle.BLOCKED
    assert EpochStore(s.dir).chain() == []


@pytest.mark.parametrize("timer", [True, False], ids=["timer", "fallback"])
def test_a_node_whose_run_park_verifies_is_resumed(tmp_path, repo, timer):
    """The positive control of the gate: it does not block what verifies."""
    s, store = _parked_node(tmp_path, repo, timer=timer)
    [rec] = [r for r in resumeloop.resume_projects(tmp_path / "runs", now=NOW)
             if r.get("node") == "a"]
    assert rec["action"] in ("resumed", "resumed_without_timer")
    assert s.read_state().node("a").lifecycle is Lifecycle.READY
    assert len(EpochStore(s.dir).chain()) == 1


# --------------------------------------------------------------------------- #
# C8-C10 -- the master of a governed park: the project's epoch chain and lease
# --------------------------------------------------------------------------- #

def _governed_park(tmp_path, repo):
    """Project p owns r1; master S1 holds epoch 1 live and declares it; the QA
    dispatch parks under that master. Returns (project, S1 handle, store)."""
    project = _project(tmp_path)
    s1 = _master(project, "S1")
    ctrl, state, store, fake = _park(tmp_path, repo, declare=("S1", s1.epoch))
    [rec] = dispatchrecovery.read_records(store.dir)[0]
    assert rec.binding.master == dispatchrecovery.MasterBinding(
        kind="GOVERNED", session="S1", epoch=1)
    return project, s1, store


def test_a_live_master_keeps_the_work_and_the_consumer_defers(tmp_path, repo):
    """Kills C8 (a live lease does not defer): the consumer pass does not take
    work a live master holds, whether the live holder is the park's master or a
    successor; nothing is written."""
    project, s1, store = _governed_park(tmp_path, repo)
    before = _tree(store.dir)
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.DEFERRED and not check.resumes
    [rec] = resumeloop.resume_runs(tmp_path / "runs", now=_later())
    assert rec["action"] == "held_by_live_master" and rec["binding"] == "DEFERRED"
    assert _tree(store.dir) == before and len(armed(store)) == 1
    s1.release(reason="the drive ended")
    s2 = _master(project, "S2")
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.DEFERRED and "epoch 2" in check.reason
    assert s2.epoch == 2


def test_a_master_that_ended_its_drive_leaves_a_park_the_consumer_resumes(tmp_path, repo):
    project, s1, store = _governed_park(tmp_path, repo)
    s1.release(reason="orderly end of drive")
    [rec] = resumeloop.resume_runs(tmp_path / "runs", now=_later())
    assert (rec["action"], rec["binding"]) == ("resumed", "BOUND")


def test_a_park_of_a_superseded_master_is_inherited_by_the_current_one(tmp_path, repo):
    """Durable recovery: the predecessor's park is resumed once its successor is
    not holding a live lease, the next master relaunches the run under its own
    epoch, and the predecessor cannot."""
    project, s1, store = _governed_park(tmp_path, repo)
    s1.release(reason="the master died")
    s2 = _master(project, "S2")
    s2.release(reason="a recovery tick that found nothing to do")
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.BOUND and "superseded by epoch 2" in check.reason
    [rec] = resumeloop.resume_runs(tmp_path / "runs", now=_later())
    assert rec["action"] == "resumed"
    s3 = _master(project, "S3")
    assert s3.epoch == 3
    master = dispatchrecovery.declare_master(store.dir, session="S3", epoch=3)
    assert master.session == "S3" and master.epoch == 3
    with pytest.raises(RecoveryError):
        dispatchrecovery.declare_master(store.dir, session="S1", epoch=1)


def test_a_park_of_a_master_the_project_never_had_is_refused(tmp_path, repo):
    """Kills C9 (a foreign master accepted): the run's master file names epoch 7
    of a session the project's epoch chain has never seen."""
    project = _project(tmp_path)
    s1 = _master(project, "S1")
    s1.release(reason="orderly")
    build_dir = tmp_path / "runs" / "r1"
    ctrl, state, store, fake = build(tmp_path, repo)
    (build_dir / "recovery").mkdir()
    (build_dir / "recovery" / "master.json").write_text(
        json.dumps({"kind": "GOVERNED", "epoch": 7, "session": "S9"}), encoding="utf-8")
    fake.qa_raise = answer_missing(QA_LIMIT, followed_up=True)
    ctrl.run_iteration(state)
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.REFUSED and "foreign master" in check.reason
    [rec] = resumeloop.resume_runs(tmp_path / "runs", now=_later())
    assert rec["action"] == "refused" and store.read_state().condition is Condition.BLOCKED


def test_a_governed_park_that_no_project_owns_is_refused(tmp_path, repo):
    ctrl, state, store, fake = _park(tmp_path, repo, declare=("S1", 1))
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.REFUSED and "no project has a node" in check.reason


def test_a_run_two_projects_claim_cannot_be_confirmed(tmp_path, repo):
    project = _project(tmp_path)
    _master(project, "S1")
    ctrl, state, store, fake = _park(tmp_path, repo, declare=("S1", 1))
    twin = ProjectStore(tmp_path / "runs", "q")
    twin.create(ProjectState(project_id="q", repo_path=str(tmp_path),
                             nodes=[TaskNode(id="b", run_id="r1")]))
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.REFUSED and "more than one project" in check.reason


# --------------------------------------------------------------------------- #
# C10 -- a stale master cannot resume work
# --------------------------------------------------------------------------- #

def test_a_stale_master_cannot_declare_itself_over_a_run(tmp_path, repo):
    """Kills C10 (the declaration never asks the project): the superseded master,
    a master the chain never held and a master that gave its lease up are each
    refused with the master file untouched; the current live master is not."""
    project = _project(tmp_path)
    s1 = _master(project, "S1")
    ctrl, state, store, fake = build(tmp_path, repo)
    dispatchrecovery.declare_master(store.dir, session="S1", epoch=1)
    s1.release(reason="the master was replaced")
    s2 = _master(project, "S2")
    master_file = store.dir / "recovery" / "master.json"
    before = master_file.read_bytes()
    for session, epoch, words in (("S1", 1, "superseded"), ("S3", 3, "never held"),
                                  ("S2", 1, "another session")):
        with pytest.raises(RecoveryError) as caught:
            dispatchrecovery.declare_master(store.dir, session=session, epoch=epoch)
        assert words in str(caught.value)
        assert master_file.read_bytes() == before
    s2.release(reason="orderly")
    with pytest.raises(RecoveryError) as caught:
        dispatchrecovery.declare_master(store.dir, session="S2", epoch=2)
    assert "lease" in str(caught.value) and master_file.read_bytes() == before
    s3 = _master(project, "S3")
    assert dispatchrecovery.declare_master(
        store.dir, session="S3", epoch=s3.epoch).epoch == 3


def test_a_stale_master_is_refused_by_hoh_run_itself(tmp_path, repo, monkeypatch, capsys):
    """The product's own command: `hoh run --master-session/--master-epoch` of a
    superseded master exits 2 and records nothing; the current master's runs."""
    from test_governance import _initialised_storage_root

    _initialised_storage_root(tmp_path, monkeypatch)
    project = _project(tmp_path)
    s1 = _master(project, "S1")
    ctrl, state, store, fake = build(tmp_path, repo)
    s1.release(reason="replaced")
    s2 = _master(project, "S2")
    argv = ["--root", str(tmp_path / "runs"), "run", "r1", "--iterations", "0", "--no-herdr"]
    rc = cli.main([*argv, "--master-session", "S1", "--master-epoch", "1"])
    err = capsys.readouterr().err
    assert rc == 2 and "recovery declaration refused" in err and "superseded" in err
    assert dispatchrecovery.read_master(store.dir, recorded_only=True) is None
    rc = cli.main([*argv, "--master-session", "S2", "--master-epoch", str(s2.epoch)])
    capsys.readouterr()
    assert rc == 0
    assert dispatchrecovery.read_master(store.dir).session == "S2"


# --------------------------------------------------------------------------- #
# Revision 2 -- what the spec reviews found, and the surviving mutants they named
# --------------------------------------------------------------------------- #

def test_a_restored_copy_of_a_run_is_not_the_run(tmp_path, repo):
    """Kills C25 (the directory name not compared with the state's run id).
    `hoh restore` without `--overwrite` lands the copy next to the run under
    `<run>-restored-<time>` and leaves `run_id` as it was: the copy has the
    receipts, the binding and no claim, and would be lifted by its own
    `retry_after`. A park is resumed in the run that made it."""
    ctrl, state, store, fake = _park(tmp_path, repo)
    runs = tmp_path / "runs"
    copy_dir = runs / "r1-restored-20261009T010101Z"
    shutil.copytree(store.dir, copy_dir)
    copy_state = RunStore(runs, copy_dir.name).read_state()
    assert copy_state.run_id == "r1"
    check = dispatchrecovery.verify_binding(copy_dir, copy_state)
    assert check.verdict is ParkVerdict.REFUSED and "this directory is" in check.reason
    [rec] = resumeloop.resume_runs(runs, now=_later(), only=copy_dir.name)
    assert rec["action"] == "refused" and "a copy of a run" in rec["why"]
    assert RunStore(runs, copy_dir.name).read_state().condition is Condition.BLOCKED
    assert dispatchrecovery.verify_binding(store.dir, store.read_state()).resumes


def test_a_declaration_cannot_move_the_master_of_a_parked_run(
        tmp_path, repo, monkeypatch, capsys):
    """Kills C26 (a declaration may rewrite the master file of a parked run).
    The receipt names the master the park was made under; rewriting the file
    would make `verify_binding` refuse the park its successor is entitled to
    inherit, and strand it until `hoh unblock`. The same master again changes
    nothing and is accepted; once the park is lifted the successor declares."""
    from test_governance import _initialised_storage_root

    _initialised_storage_root(tmp_path, monkeypatch)
    project, s1, store = _governed_park(tmp_path, repo)
    master_file = store.dir / "recovery" / "master.json"
    assert dispatchrecovery.declare_master(store.dir, session="S1", epoch=1).session == "S1"
    before = master_file.read_bytes()
    s1.release(reason="the master was replaced")
    s2 = _master(project, "S2")
    with pytest.raises(RecoveryError) as caught:
        dispatchrecovery.declare_master(store.dir, session="S2", epoch=s2.epoch)
    assert "parked on a provider wait" in str(caught.value)
    argv = ["--root", str(tmp_path / "runs"), "run", "r1", "--iterations", "0", "--no-herdr"]
    assert cli.main([*argv, "--master-session", "S2", "--master-epoch", str(s2.epoch)]) == 2
    assert "parked on a provider wait" in capsys.readouterr().err
    assert master_file.read_bytes() == before
    s2.release(reason="a recovery tick that found nothing to do")
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.BOUND and "superseded by epoch 2" in check.reason
    assert resumeloop.resume_runs(tmp_path / "runs", now=_later())[0]["action"] == "resumed"
    s3 = _master(project, "S3")
    assert dispatchrecovery.declare_master(store.dir, session="S3", epoch=s3.epoch).epoch == 3


@pytest.mark.parametrize("timer", [True, False], ids=["timer", "fallback"])
def test_a_node_whose_run_state_cannot_be_read_is_not_resumed_and_spends_no_epoch(
        tmp_path, repo, timer):
    """Kills C27 (the project gate fails open): a run state that exists and cannot
    be read here is a refusal, not 'nothing to check'; the node stays BLOCKED and
    no master acquires an epoch for it."""
    s, store = _parked_node(tmp_path, repo, timer=timer)
    before = store.state_path.read_bytes()
    store.state_path.write_text("{ this is not a state", encoding="utf-8")
    [rec] = [r for r in resumeloop.resume_projects(tmp_path / "runs", now=NOW)
             if r.get("node") == "a"]
    assert rec["action"] == "refused" and "cannot be read here" in rec["why"]
    assert s.read_state().node("a").lifecycle is Lifecycle.BLOCKED
    assert EpochStore(s.dir).chain() == []
    store.state_path.write_bytes(before)
    [again] = [r for r in resumeloop.resume_projects(tmp_path / "runs", now=NOW)
               if r.get("node") == "a"]
    assert again["action"] in ("resumed", "resumed_without_timer")


def test_a_node_with_no_run_record_is_resumed_by_the_project_pass(tmp_path):
    """The legitimate 'nothing to check' of the gate: a project-only park, no run
    directory under that name, is resumed as before (and the gate does not
    create one)."""
    s = ProjectStore(tmp_path / "runs", "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=[
        TaskNode(id="ghost", lifecycle=Lifecycle.BLOCKED, blocked_kind="usage_limit",
                 retry_after=PAST)]))
    TimerStore(s.dir).arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=PAST, program_epoch=1,
                              run_id=node_timer_key("ghost"), node_id="ghost")
    [rec] = [r for r in resumeloop.resume_projects(tmp_path / "runs", now=NOW)
             if r.get("node") == "ghost"]
    assert rec["action"] == "resumed" and "binding" not in rec
    assert not (tmp_path / "runs" / "ghost").exists()


def test_a_master_whose_session_id_has_the_shape_of_a_key_halts_the_drive_with_the_reason(
        tmp_path):
    """Kills C28 (the drive hands the launcher a session id every `hoh run` refuses):
    the drive halts AMBIGUOUS with the reason, once, and ends orderly; the launcher
    is never told a master, and the id is not echoed."""
    from hoh.orchestrator import HaltClass

    project = _project(tmp_path)
    key_like = "AbCdEf0123456789AbCdEf0123456789AbCdEf01"
    authority = MasterAuthority(project.dir, key_like, serialize=project.lock)
    authority.acquire()
    told = []

    class Launcher:
        def bind_master(self, session, epoch):
            told.append((session, epoch))

    result = resumeloop.drive_governed(project, authority, lambda store_: (Launcher(), None))
    assert result.halt is HaltClass.AMBIGUOUS and "plain session id" in result.reason
    assert key_like not in result.reason and told == []
    assert authority.read_lease().released_at


def test_a_damaged_neighbour_project_does_not_stop_the_master_check(tmp_path, repo):
    """Kills C17 (a project state that cannot be read is an error, not 'not an
    owner'): one damaged neighbour must not stop the masters of the others."""
    project, s1, store = _governed_park(tmp_path, repo)
    damaged = tmp_path / "runs" / "projects" / "q"
    damaged.mkdir(parents=True)
    (damaged / "project.json").write_text("{ nope", encoding="utf-8")
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.DEFERRED
    assert dispatchrecovery.declare_master(store.dir, session="S1", epoch=1).session == "S1"


def test_an_unreadable_claim_is_still_a_claim(tmp_path, repo):
    """Kills C18 (an unreadable claim read as no claim): a claim file that is
    there and cannot be parsed still says the park was lifted."""
    ctrl, state, store, fake = _park(tmp_path, repo)
    [rec] = dispatchrecovery.read_records(store.dir)[0]
    claims = store.dir / "recovery" / "claims"
    claims.mkdir(parents=True)
    (claims / f"{rec.park_id}.json").write_text("{ not json", encoding="utf-8")
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.REFUSED and "already resumed" in check.reason


def test_a_block_of_another_class_is_not_asked_about_a_provider_park(tmp_path, repo):
    """Kills C19 (the class gate dropped): a block that is not a provider wait has
    no receipt to be checked against, even in a run that has receipts, and is
    resumed as the caller's `classes` say."""
    ctrl, state, store, fake = _park(tmp_path, repo)
    _edit(store, blocked_kind="collected_for_digest", retry_after=None)
    [rec] = resumeloop.resume_runs(tmp_path / "runs", now=_later(),
                                   classes=frozenset({"collected_for_digest"}))
    assert rec["action"] == "resumed" and "binding" not in rec and "claim" not in rec


def test_a_claim_that_cannot_be_written_is_reported_and_the_park_stays_lifted(
        tmp_path, repo, monkeypatch):
    """Kills C20 (a claim failure is fatal to the pass): the park is already
    lifted; the pass says the claim was not written and goes on."""
    def refuse(*args, **kwargs):
        raise OSError("input/output error")

    monkeypatch.setattr(dispatchrecovery, "claim_park", refuse)
    ctrl, state, store, fake = _park(tmp_path, repo)
    [rec] = resumeloop.resume_runs(tmp_path / "runs", now=_later())
    assert rec["action"] == "resumed" and rec["claim"] == "NOT written (OSError)"
    assert store.read_state().condition is not Condition.BLOCKED
    assert armed(store) == []


def test_an_escalation_that_cannot_be_written_is_reported_and_the_park_stays_given_up(
        tmp_path, repo, monkeypatch):
    """Kills C21 (an escalation failure is fatal to the pass)."""
    def refuse(*args, **kwargs):
        raise OSError("input/output error")

    ctrl, state, store, fake = _park(tmp_path, repo)
    _edit(store, resume_attempts=quota.MAX_RESUME_ATTEMPTS)
    monkeypatch.setattr(dispatchrecovery, "append_record", refuse)
    [rec] = resumeloop.resume_runs(tmp_path / "runs", now=_later())
    assert rec["action"] == "given_up" and rec["escalation"] == "NOT written (OSError)"
    assert store.read_state().condition is Condition.BLOCKED


SESSION_EDGES_REFUSED = [
    "a1b2c3d4e5f6a7b8c9d0",                       # 20 letters and digits: the threshold
    "AbCdEf0123456789AbCdEf0123",                  # 26: between the threshold and 40
    "abcdefghijklmnopqrstuvwxyzabcd",              # 30 letters: the threshold
    "abcdefghijklmnopqrstuvwxyzabcdefg",           # 33 letters: between the threshold and 34
]
SESSION_EDGES_ACCEPTED = [
    "a1b2c3d4e5f6a7b8c9d",                         # 19 letters and digits
    "abcdefghijklmnopqrstuvwxyzabc",               # 29 letters
    "2026100909000012345678",                      # 22 digits alone: ids carry timestamps
    "abcdefghijklmnopqrstuvwxy",                   # 25 letters alone
    "resume-2026-10-09T035000Z-4242",
]


@pytest.mark.parametrize("value", SESSION_EDGES_REFUSED)
def test_a_session_id_at_the_edge_of_a_shape_rule_is_refused(value):
    """Kills C22 (the mixed-run threshold 20 raised) and C23 (the long-word
    threshold 30 raised): the values sit strictly between the real thresholds and
    the raised ones."""
    with pytest.raises(RecoveryError) as caught:
        dispatchrecovery.check_session_id(value)
    assert value not in str(caught.value)


@pytest.mark.parametrize("value", SESSION_EDGES_ACCEPTED)
def test_a_session_id_just_inside_a_shape_rule_is_accepted(value):
    """Kills C24 (a long run is refused whether or not it mixes letters and
    digits) and pins the other side of both thresholds: one character fewer."""
    assert dispatchrecovery.check_session_id(value) == value


# --------------------------------------------------------------------------- #
# The four scenarios of amendment A5, end to end
# --------------------------------------------------------------------------- #

def test_a5_scenario_1_a_usage_limit_in_qa_blocks_and_resumes_without_losing_the_iteration(
        tmp_path, repo, monkeypatch, capsys):
    """Controller -> receipt -> `hoh resume-quota` (verified, claimed, resumed)
    -> the same iteration finishes on another dispatcher."""
    ctrl, state, store, fake = _park(tmp_path, repo)
    assert state.condition is Condition.BLOCKED and state.blocked_kind == "usage_limit"
    assert "candidate rejected" not in history(state)
    out = _resume_via_the_cli(tmp_path / "runs", monkeypatch, capsys)
    [rec] = out["results"]
    assert (rec["action"], rec["binding"], rec["claim"]) == ("resumed", "BOUND", "written")
    persisted = store.read_state()
    assert persisted.condition is not Condition.BLOCKED
    fresh = Controller(store, type(fake)(repo), spec_path=tmp_path / "spec.md")
    done = fresh.run_iteration(persisted)
    assert done.accepted and (persisted.iteration, persisted.usage.iterations) == (1, 1)


def test_a5_scenario_2_an_expired_credential_is_operator_required_not_endless_retry(
        tmp_path, repo, monkeypatch, capsys):
    """An expired credential is an escalation to a person and no timer wakes it;
    a wait that keeps failing is bounded: after MAX_RESUME_ATTEMPTS the loop
    gives up and leaves an escalation receipt."""
    auth = tmp_path / "auth"
    ctrl, state, store, fake = _park(auth, _fresh_repo(auth), text=AUTH)
    [rec] = dispatchrecovery.read_records(store.dir)[0]
    assert isinstance(rec, RoutingEscalation) and rec.escalation == "OPERATOR_REQUIRED"
    assert armed(store) == []
    before = _tree(store.dir)
    out = _resume_via_the_cli(auth / "runs", monkeypatch, capsys)
    assert all(r["action"] != "resumed" for r in out["results"])
    assert _tree(store.dir) == before

    flaky = tmp_path / "flaky"
    repo2 = _fresh_repo(flaky)
    ctrl, state, store, fake = _park(flaky, repo2, text=RATE)
    runs = flaky / "runs"
    for attempt in range(1, quota.MAX_RESUME_ATTEMPTS + 1):
        [step] = resumeloop.resume_runs(runs, now=_later())
        assert (step["action"], step["attempt"]) == ("resumed", attempt)
        still_limited = type(fake)(repo2)
        still_limited.qa_raise = answer_missing(RATE, followed_up=True)
        Controller(store, still_limited, spec_path=flaky / "spec.md").run_iteration(
            store.read_state())
        assert store.read_state().condition is Condition.BLOCKED
    [last] = resumeloop.resume_runs(runs, now=_later())
    assert last["action"] == "given_up"
    assert last["escalation"]["escalation"] == "RETRIES_EXHAUSTED"
    records, problems = dispatchrecovery.read_records(store.dir)
    assert problems == []
    assert len([r for r in records if isinstance(r, RoutingDecision)]) == (
        quota.MAX_RESUME_ATTEMPTS + 1)
    assert [r.escalation for r in records if isinstance(r, RoutingEscalation)] == [
        "RETRIES_EXHAUSTED"]


def test_a5_scenario_3_a_stale_master_cannot_resume_or_fail_over(
        tmp_path, repo, monkeypatch, capsys):
    """Master S1 parks the run and is replaced by S2. A park claimed by a master
    the project never had is not resumed; S1 cannot relaunch the run; S2, the
    current master holding a live lease, keeps the work; once it is gone the
    next master resumes and relaunches."""
    project, s1, store = _governed_park(tmp_path, repo)
    s1.release(reason="the master was replaced")
    s2 = _master(project, "S2")
    out = _resume_via_the_cli(tmp_path / "runs", monkeypatch, capsys)
    [rec] = out["results"]
    assert rec["action"] == "held_by_live_master"
    with pytest.raises(RecoveryError) as caught:
        dispatchrecovery.declare_master(store.dir, session="S1", epoch=1)
    assert "superseded" in str(caught.value)
    s2.release(reason="orderly")
    out = _resume_via_the_cli(tmp_path / "runs", monkeypatch, capsys)
    [rec] = out["results"]
    assert (rec["action"], rec["binding"]) == ("resumed", "BOUND")
    s3 = _master(project, "S3")
    assert dispatchrecovery.declare_master(store.dir, session="S3", epoch=s3.epoch).epoch == 3


def test_the_next_master_resumes_the_park_and_relaunches_the_run_under_its_own_epoch(
        tmp_path, repo, monkeypatch, capsys):
    """The recovery loop end to end, with the real `resume_projects`,
    `drive_governed` and `launcher.bind_master`: master S1 parks the run and
    ends its drive; `hoh resume-quota --drive` lifts the park (verified,
    claimed), a new master acquires epoch 2, the node is resumed, the drive
    tells the launcher its master, and the run's declaration under that master
    is accepted while the lease is live. A stand-in `ProjectController` makes the
    declaration `hoh run` would make."""
    from hoh import orchestrator
    from hoh.launcher import HohRunLauncher

    root = tmp_path / "runs"
    project, s1, store = _governed_park(tmp_path, repo)
    s1.release(reason="orderly end of drive")
    st = project.read_state()
    node = st.node("a")
    node.lifecycle, node.blocked_kind, node.retry_after = (
        Lifecycle.BLOCKED, "usage_limit", PAST)
    project.write_state(st)
    TimerStore(project.dir).arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=PAST,
                                    program_epoch=1, run_id=node_timer_key("a"), node_id="a")
    declared = []

    class StandIn:
        def __init__(self, store_, launcher, gates, authority=None, **kw):
            self.launcher, self.authority = launcher, authority

        def run(self):
            declared.append(dispatchrecovery.declare_master(
                root / "r1", session=self.launcher.master_session,
                epoch=self.launcher.master_epoch))
            return orchestrator.Result(orchestrator.HaltClass.CLOSED, "stand-in drive")

    monkeypatch.setattr(orchestrator, "ProjectController", StandIn)
    monkeypatch.setattr(resumeloop, "load_driver",
                        lambda d: (lambda store_: (HohRunLauncher(root, tmp_path), None)))
    later = _later().strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(resumeloop, "_now_str", lambda now: later)
    assert cli.main(["--root", str(root), "resume-quota", "--drive"]) == 0
    out = json.loads(capsys.readouterr().out)
    [run_rec] = out["results"]
    assert (run_rec["action"], run_rec["binding"]) == ("resumed", "BOUND")
    drove = [r for r in out["projects"] if r["action"] == "drove"]
    assert len(drove) == 1 and drove[0]["epoch"] == 2
    assert [m.epoch for m in declared] == [2] and declared[0].session == drove[0]["master"]
    assert dispatchrecovery.read_master(store.dir) == declared[0]
    assert EpochStore(project.dir).current().epoch == 2


def test_a5_scenario_4_two_masters_cannot_resume_the_same_blocked_work(
        tmp_path, repo, monkeypatch, capsys):
    """A live master and a consumer cannot both take the park; two passes cannot
    lift it twice, even when the parked state is restored; two sessions cannot
    declare one epoch."""
    project, s1, store = _governed_park(tmp_path, repo)
    parked = store.state_path.read_bytes()
    out = _resume_via_the_cli(tmp_path / "runs", monkeypatch, capsys)
    assert [r["action"] for r in out["results"]] == ["held_by_live_master"]
    s1.release(reason="the drive ended")
    out = _resume_via_the_cli(tmp_path / "runs", monkeypatch, capsys)
    assert [r["action"] for r in out["results"]] == ["resumed"]
    out = _resume_via_the_cli(tmp_path / "runs", monkeypatch, capsys)
    assert all(r["action"] != "resumed" for r in out["results"])
    store.state_path.write_bytes(parked)
    TimerStore(tmp_path / "runs").arm_new(
        kind=TimerKind.PROVIDER_RETRY, due_at=PAST, reason="restored", program_epoch=0,
        run_id="r1")
    out = _resume_via_the_cli(tmp_path / "runs", monkeypatch, capsys)
    [rec] = [r for r in out["results"] if r.get("action") != "skipped"]
    assert rec["action"] == "refused" and "already resumed" in rec["why"]
    with pytest.raises(RecoveryError):
        dispatchrecovery.declare_master(store.dir, session="S2", epoch=1)


# --------------------------------------------------------------------------- #
# Stated limits, pinned (L1, L2, L6): what the verification does not do
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("command", ["unblock", "resume"])
def test_residual_hoh_unblock_and_hoh_resume_lift_a_moved_park_by_design(
        tmp_path, repo, command):
    """L6, pinned. Both are a person's command on a BLOCKED run (`hoh resume
    --force`, with unclear task references discarded, ends in `stages.unblock`): they bypass the verification and write no claim, on
    purpose -- they are the escape hatch every refusal names. Changing that is a
    decision, and this test is where it shows."""
    base = tmp_path / command
    ctrl, state, store, fake = _park(base, _fresh_repo(base))
    _move_iteration(store)
    assert dispatchrecovery.verify_binding(store.dir, store.read_state()).verdict \
        is ParkVerdict.REFUSED
    argv = {"unblock": ["unblock", "r1"], "resume": ["resume", "r1", "--force"]}[command]
    assert cli.main(["--root", str(base / "runs"), *argv]) == 0
    assert store.read_state().condition is not Condition.BLOCKED
    assert not (store.dir / "recovery" / "claims").exists()


def test_residual_a_bare_declaration_keeps_a_stale_recorded_master(tmp_path, repo):
    """L1/L2, pinned. Ownership and the lease are asked of a *governed*
    declaration only: a `hoh run` without master flags keeps whatever master is
    recorded, stale or not, and asks nobody."""
    project = _project(tmp_path)
    s1 = _master(project, "S1")
    ctrl, state, store, fake = build(tmp_path, repo)
    dispatchrecovery.declare_master(store.dir, session="S1", epoch=1)
    s1.release(reason="the master was replaced")
    _master(project, "S2")
    kept = dispatchrecovery.declare_master(store.dir)
    assert (kept.kind, kept.session, kept.epoch) == ("GOVERNED", "S1", 1)


def test_residual_a_run_no_master_declared_is_not_deferred_to_the_live_master(tmp_path, repo):
    """L1/L2, pinned. The declaration is what links a run to a master: a run that
    declared none, owned by a project whose master is live, is a bare run, and a
    consumer resumes its park (it has no master to defer to)."""
    project = _project(tmp_path)
    _master(project, "S1")
    ctrl, state, store, fake = _park(tmp_path, repo)
    check = dispatchrecovery.verify_binding(store.dir, store.read_state())
    assert check.verdict is ParkVerdict.BOUND
    [rec] = resumeloop.resume_runs(tmp_path / "runs", now=_later())
    assert rec["action"] == "resumed"
