"""P2-43 slice b: what a failed dispatch leaves behind, and what a park is bound to.

Slice a made a provider failure a wait or an operator action. These tests
drive the same real controller on the same fakes (no Herdr, no agent, nothing
outside `tmp_path`) and check what is now written beside the run state: the
provenance of every dispatch, the park binding, the two routing receipts, the
declared master (`NONE` for a bare run), the opaque account handle, and the
retry time read from the provider's stated reset.

Mutant ids in a docstring (`P1`..) are the spec's table of mutations the
integration review applies; each test says which it kills.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_failure_classes import (  # noqa: F401  (repo is a fixture)
    AUTH,
    FIRST,
    QA_LIMIT,
    ROLE_TIME,
    STALL,
    answer_missing,
    armed,
    build,
    history,
    pa_contract,
    pane,
)
from test_failure_classes import repo as repo  # a fixture, found by pytest by name; the alias form is an explicit re-export, so
# a test parameter named `repo` is not a redefinition of an unused import (F811)


from hoh import cli, dispatchrecovery, failures, quota, resumeloop, stages
from hoh.contracts import Condition, Role
from hoh.controller import Controller, DispatchError
from hoh.failures import NormalizedFailure as NF
from hoh.dispatchrecovery import (
    DispatchProvenance,
    MasterBinding,
    RecoveryContext,
    RecoveryError,
    RoutingDecision,
    RoutingEscalation,
)
from hoh.telemetry import NOT_AVAILABLE

SECRETS = [
    "sk-ant-api03-AbCdEf0123456789xyz",
    "sk_live_51H8abcdefghijklmnop",
    "ghp_16C7e42F292c6912E7710c838347Ae178B4a",
    "github_pat_11ABCDEFG0abcdefghij",
    "xoxb-123456789012-abcdefghijkl",
    "AKIAIOSFODNN7EXAMPLE",
    "eyJhbGciOiJIUzI1NiJ9",
    "Bearer abc",
    "a1b2c3d4e5f6a7b8c9d0e1f2a3b4",
    "work account",
    "account=work",
    "my-api-key",
    "prod-token-2",
    "password1",
    "x" * 65,
    "https://user:pw@host",
    "3f2b8a1c-9d4e-4b7a-8c1d-5e6f7a8b9c0d",
    "1234567890123456",
    "abcdefghijklmnopqrstuvwxyzabcdefgh",
    # one per credential prefix and per secret word: cutting any list to the
    # shapes above must show
    "user:pw@host", "my-sk-ant-api03-abcdefghijkl", "hf-abcdefg", "pk_live_x1", "rk-live-x1",
    "gho_abc", "ghs_abc", "ghu_abc", "ghr_abc", "glpat-abc", "ASIAABCDEF", "AIzaSyX",
    "ya29.abc", "npm_abc", "pypi-abc", "basic-abc", "bearer-abc",
    "my-secret-1", "passphrase-1", "credential-a", "private-key-x", "apikey-x", "api_key_x",
]
NAMES = ["work-account-2", "operator@claude-max", "claude:eu/team-a", "acct_7", "NOT_AVAILABLE"]


def _bind_roles(fake) -> None:
    fake.profiles = {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "kimi"}
    fake.models = {Role.QA: "kimi-k2"}


def _qa_limit_park(tmp_path, repo, **kw):
    ctrl, state, store, fake = build(tmp_path, repo, **kw)
    _bind_roles(fake)
    fake.qa_raise = answer_missing(QA_LIMIT, followed_up=True)
    return ctrl, state, store, fake, ctrl.run_iteration(state)


def _telemetry(store) -> list[dict]:
    path = store.dir / "telemetry.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# P1 -- the account handle is a name, never a secret
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("handle", NAMES)
def test_a_plain_name_is_an_acceptable_account_handle(handle):
    assert dispatchrecovery.check_account_handle(handle) == handle


def test_no_handle_is_the_stated_unknown():
    assert dispatchrecovery.check_account_handle(None) == NOT_AVAILABLE
    assert dispatchrecovery.check_account_handle("  ") == NOT_AVAILABLE
    assert RecoveryContext().accounts == {}
    assert DispatchProvenance(
        provider="claude", model="m", role="qa", capability_requirements=["x"]
    ).account_handle == NOT_AVAILABLE


@pytest.mark.parametrize("secret", SECRETS)
def test_a_secret_shaped_handle_is_refused_and_not_echoed(secret):
    """Kills P1 (the handle check dropped), Q14 (credential prefixes matched only at the
    start) and Q15 (the `user:pw@` rule): a refusal is raised at the check,
    at the record's validator and at the declaration, and the text of the
    secret is in none of the messages."""
    with pytest.raises(RecoveryError) as caught:
        dispatchrecovery.check_account_handle(secret)
    assert secret not in str(caught.value)
    with pytest.raises(ValueError):
        DispatchProvenance(provider="claude", model="m", role="qa", account_handle=secret,
                           capability_requirements=["x"])
    with pytest.raises(RecoveryError):
        RecoveryContext(accounts={Role.QA: secret})


def test_role_account_pairs_never_echo_what_was_typed():
    """Kills P16: a pasted key without `ROLE=` and a key in the role slot are
    refused with fixed text, which is what `_per_role` (it prints its input)
    would not have done."""
    key = "sk-ant-api03-AbCdEf0123456789xyz"
    for typed in (key, f"{key}=qa", f"qa={key}"):
        with pytest.raises(RecoveryError) as caught:
            dispatchrecovery.parse_role_accounts([typed])
        assert key not in str(caught.value)
    for typed in ("qa=", "=work", "boss=work-account-2"):
        with pytest.raises(RecoveryError) as caught:
            dispatchrecovery.parse_role_accounts([typed])
        assert "boss" not in str(caught.value) and "work-account-2" not in str(caught.value)
    assert dispatchrecovery.parse_role_accounts(["all=team-a", "qa=work-account-2"]) == {
        Role.PLANNER: "team-a", Role.DEVELOPER: "team-a", Role.QA: "work-account-2"}


def test_the_launcher_refuses_a_secret_shaped_handle():
    from hoh.launcher import HohRunLauncher

    with pytest.raises(RecoveryError):
        HohRunLauncher(Path("."), Path("."), role_accounts={"qa": SECRETS[0]})


# --------------------------------------------------------------------------- #
# P2 -- every dispatch carries its provenance
# --------------------------------------------------------------------------- #

def test_every_dispatch_line_carries_its_provenance(tmp_path, repo):
    """Kills P2 (no provenance written): a successful run's three roles each state
    who ran them, under which account and policy."""
    ctrl, state, store, fake = build(tmp_path, repo)
    _bind_roles(fake)
    ctrl.recovery = RecoveryContext(accounts={Role.QA: "work-account-2"})
    out = ctrl.run_iteration(state)
    assert out.accepted
    lines = [ln for ln in _telemetry(store) if ln["role"] in ("planner", "developer", "qa")]
    assert {ln["role"] for ln in lines} == {"planner", "developer", "qa"}
    by_role = {ln["role"]: DispatchProvenance.model_validate(ln["provenance"]) for ln in lines}
    assert by_role["qa"].account_handle == "work-account-2"
    assert by_role["planner"].account_handle == NOT_AVAILABLE
    assert (by_role["qa"].provider, by_role["qa"].model) == ("kimi", "kimi-k2")
    assert (by_role["planner"].provider, by_role["planner"].model) == ("claude", NOT_AVAILABLE)
    for role, prov in by_role.items():
        assert (prov.route_policy_id, prov.route_policy_version) == ("same-route-wait", 1)
        assert prov.capability_requirements == list(
            dispatchrecovery.CAPABILITY_REQUIREMENTS[role])
        assert prov.data_class == dispatchrecovery.UNSPECIFIED
        assert prov.normalized_failure is None
    # The orchestration steps reach no provider and carry none.
    assert all(ln["provenance"] is None for ln in _telemetry(store)
               if ln["role"] not in ("planner", "developer", "qa"))


def test_a_failed_dispatch_line_carries_the_failure_class_in_its_provenance(tmp_path, repo):
    ctrl, state, store, fake, _ = _qa_limit_park(tmp_path, repo)
    [qa] = [ln for ln in _telemetry(store) if ln["role"] == "qa" and ln["outcome"] != "ok"]
    prov = DispatchProvenance.model_validate(qa["provenance"])
    assert prov.normalized_failure is failures.NormalizedFailure.USAGE_LIMIT
    assert qa["normalized_failure"] == "USAGE_LIMIT"


# --------------------------------------------------------------------------- #
# P3, P4 -- the park binding and the routing receipts
# --------------------------------------------------------------------------- #

def test_a_wait_is_bound_and_receipted_before_the_state_is_parked(tmp_path, repo, monkeypatch):
    """Kills P3 (the receipt written after the timer or the parked state, or not
    at all): at the moment the timer is armed the receipt is on disk and the
    persisted state is not yet parked."""
    seen = {}
    real = Controller._arm_quota_timer

    def spy(self, state, governor):
        records, problems = dispatchrecovery.read_records(self.store.dir)
        seen["records"] = records
        seen["problems"] = problems
        seen["persisted"] = self.store.read_state().condition
        return real(self, state, governor)

    monkeypatch.setattr(Controller, "_arm_quota_timer", spy)
    ctrl, state, store, fake, out = _qa_limit_park(tmp_path, repo)
    assert seen["problems"] == [] and len(seen["records"]) == 1
    assert seen["persisted"] is not Condition.BLOCKED

    records, problems = dispatchrecovery.read_records(store.dir)
    assert problems == [] and len(records) == 1
    [rec] = records
    assert isinstance(rec, RoutingDecision)
    assert (rec.decision, rec.reason) == ("WAIT_SAME_ROUTE", "USAGE_LIMIT")
    assert rec.retry_at == state.retry_after == store.read_state().retry_after
    b = rec.binding
    assert (b.programme, b.program_epoch) == ("NONE", 0)
    assert (b.run_id, b.iteration, b.attempt, b.stage) == (
        "r1", state.iteration, state.attempt, "VERIFYING")
    assert b.candidate == dispatchrecovery.candidate_binding(state) != "NONE"
    assert b.candidate == state.working_candidate.binding()
    assert b.master == MasterBinding(kind="NONE")
    assert rec.park_id == dispatchrecovery.park_id(
        b, failures.NormalizedFailure.USAGE_LIMIT, state.retry_after)
    assert (rec.route_policy_id, rec.route_policy_version) == ("same-route-wait", 1)
    assert (f"routing receipt ROUTING_DECISION {rec.park_id[:12]} "
            "(policy same-route-wait/1, master NONE)") in history(state)
    assert (rec.provenance.role, rec.provenance.provider, rec.provenance.model) == (
        "qa", "kimi", "kimi-k2")
    assert rec.provenance.normalized_failure is failures.NormalizedFailure.USAGE_LIMIT


def test_a_governed_park_is_bound_to_its_programme_and_master(tmp_path, repo):
    """The binding takes the programme from the pinned contract and the master
    from the declaration, not from constants."""
    ctrl, state, store, fake = build(tmp_path, repo, contract=pa_contract())
    _bind_roles(fake)
    dispatchrecovery.declare_master(store.dir, session="sess-7", epoch=4)
    fake.developer_raise = DispatchError(STALL.replace("planner dispatch", "developer dispatch"),
                                         transient=True, retry_after=0.0)
    ctrl.run_iteration(state)
    [rec] = dispatchrecovery.read_records(store.dir)[0]
    assert isinstance(rec, RoutingDecision)
    assert rec.binding.programme == "pa"
    assert rec.binding.program_epoch == ctrl._contract_governor(state).contract.program_epoch
    assert rec.binding.master == MasterBinding(kind="GOVERNED", session="sess-7", epoch=4)
    assert rec.binding.stage == "DEVELOPING"
    assert rec.provenance.role == "developer"


def test_an_operator_required_class_is_escalated_not_decided(tmp_path, repo):
    """A class that needs a person is an escalation, not a decision."""
    ctrl, state, store, fake = build(tmp_path, repo)
    _bind_roles(fake)
    fake.qa_raise = answer_missing(AUTH, followed_up=True)
    ctrl.run_iteration(state)
    records, problems = dispatchrecovery.read_records(store.dir)
    assert problems == [] and len(records) == 1
    [rec] = records
    assert isinstance(rec, RoutingEscalation)
    assert (rec.escalation, rec.reason) == ("OPERATOR_REQUIRED", "AUTH_EXPIRED")
    assert rec.binding.stage == "VERIFYING"
    assert armed(store) == []


def test_a_held_run_is_escalated_as_held(tmp_path, repo):
    ctrl, state, store, fake = build(tmp_path, repo)
    state.blocked_kind = stages.CAPABILITY_VIOLATION
    store.write_state(state)
    fake.qa_raise = answer_missing(QA_LIMIT, followed_up=True)
    ctrl.run_iteration(state)
    [rec] = dispatchrecovery.read_records(store.dir)[0]
    assert isinstance(rec, RoutingEscalation) and rec.escalation == "HELD_BY_VIOLATION"
    assert armed(store) == [] and state.blocked_kind == stages.CAPABILITY_VIOLATION


AUTOMATIC_CASES = [
    ("usage-limit", QA_LIMIT, NF.USAGE_LIMIT),
    ("rate-limit", pane(FIRST, "⎿  API Error: 429 too many requests"), NF.RATE_LIMIT),
    ("provider-unavailable", pane(FIRST, "⎿  API Error: 529 overloaded_error"),
     NF.PROVIDER_UNAVAILABLE),
    ("network", pane(FIRST, "⎿  API Error: Connection error."), NF.NETWORK_TRANSIENT),
    ("prompt-stalled", STALL, NF.PROMPT_STALLED),
    ("role-timeout", ROLE_TIME, NF.ROLE_TIMEOUT),
]


def test_the_cases_cover_every_automatic_class():
    assert {c[2] for c in AUTOMATIC_CASES} == {
        c for c in NF if failures.RETRY_MODE[c] is failures.RetryMode.AUTOMATIC}
    for _id, text, expected in AUTOMATIC_CASES:
        assert failures.classify(DispatchError(text)) is expected


@pytest.mark.parametrize("text,expected", [c[1:] for c in AUTOMATIC_CASES],
                         ids=[c[0] for c in AUTOMATIC_CASES])
def test_a_wait_that_cannot_be_recorded_is_not_parked_on_a_timer(
        tmp_path, repo, monkeypatch, text, expected):
    """Kills P4 (the record failure ignored) and P15 (the class name in the
    reason, which `stages.block` reads as a quota phrase and arms a timer for --
    `RATE_LIMIT` did): a park nobody can bind needs a person, never a timer,
    for every automatic class."""
    def refuse(run_dir, record):
        raise OSError("no space left on device")

    monkeypatch.setattr(dispatchrecovery, "append_record", refuse)
    ctrl, state, store, fake = build(tmp_path, repo)
    _bind_roles(fake)
    fake.qa_raise = answer_missing(text, followed_up=True)
    out = ctrl.run_iteration(state)
    assert f"failure class {expected.value} (AUTOMATIC)" in history(state)
    assert out.reason.startswith("no verdict") and "could not be written" in out.reason
    assert state.condition is Condition.BLOCKED
    assert state.blocked_kind not in resumeloop.QUOTA_CLASSES
    assert armed(store) == [] and not state.retry_after
    persisted = store.read_state()
    assert persisted.blocked_kind not in resumeloop.QUOTA_CLASSES and not persisted.retry_after
    assert "recovery record NOT written" in history(state)


def test_records_are_digest_bound(tmp_path, repo):
    """A changed or torn line is a problem, never a record."""
    ctrl, state, store, fake, _ = _qa_limit_park(tmp_path, repo)
    path = store.dir / "recovery" / "routing.jsonl"
    good = path.read_text(encoding="utf-8")
    forged = json.loads(good)
    forged["binding"]["iteration"] = 99
    path.write_text(json.dumps(forged) + "\n{not json\n" + good, encoding="utf-8")
    records, problems = dispatchrecovery.read_records(store.dir)
    assert len(records) == 1 and records[0].binding.iteration == state.iteration
    assert len(problems) == 2 and "digest mismatch" in problems[0]


def test_park_id_depends_on_every_field_of_the_binding():
    """Kills P11 (an id of fewer fields): the tests above recompute the id with
    the module's own function, so this one varies each field and compares."""
    base = dispatchrecovery.ParkBinding(
        programme="pa", program_epoch=2, run_id="r1", iteration=1, attempt=0,
        stage="VERIFYING", candidate="c0:t0:clean",
        master=MasterBinding(kind="GOVERNED", session="s", epoch=3))
    cls, at = NF.USAGE_LIMIT, "2026-10-09T00:21:00+00:00"
    ref = dispatchrecovery.park_id(base, cls, at)
    assert len(ref) == 32 and int(ref, 16) >= 0
    assert dispatchrecovery.park_id(base.model_copy(), cls, at) == ref
    variants = [
        base.model_copy(update={"programme": "pb"}),
        base.model_copy(update={"program_epoch": 3}),
        base.model_copy(update={"run_id": "r2"}),
        base.model_copy(update={"iteration": 2}),
        base.model_copy(update={"attempt": 1}),
        base.model_copy(update={"stage": "DEVELOPING"}),
        base.model_copy(update={"candidate": "c1:t0:clean"}),
        base.model_copy(update={"master": MasterBinding(kind="GOVERNED", session="s", epoch=4)}),
        base.model_copy(update={"master": MasterBinding(kind="GOVERNED", session="t", epoch=3)}),
        base.model_copy(update={"master": MasterBinding(kind="NONE")}),
    ]
    ids = {dispatchrecovery.park_id(v, cls, at) for v in variants}
    ids |= {dispatchrecovery.park_id(base, NF.RATE_LIMIT, at),
            dispatchrecovery.park_id(base, cls, "2026-10-09T01:21:00+00:00"),
            dispatchrecovery.park_id(base, cls, None)}
    assert len(ids) == len(variants) + 3 and ref not in ids


def test_a_lower_or_rival_master_declaration_is_refused(tmp_path):
    """Kills P19 (declarations not monotone): epochs only grow; the same epoch
    under another session is two masters."""
    run = tmp_path / "run"
    run.mkdir()
    dispatchrecovery.declare_master(run, session="s5", epoch=5)
    for session, epoch in (("s3", 3), ("other", 5)):
        with pytest.raises(RecoveryError):
            dispatchrecovery.declare_master(run, session=session, epoch=epoch)
    assert dispatchrecovery.read_master(run) == MasterBinding(
        kind="GOVERNED", session="s5", epoch=5)
    assert dispatchrecovery.declare_master(run, session="s5", epoch=5).epoch == 5
    assert dispatchrecovery.declare_master(run, session="s6", epoch=6).epoch == 6


# --------------------------------------------------------------------------- #
# P6 -- the master of a run
# --------------------------------------------------------------------------- #

def test_a_bare_run_records_no_master_explicitly(tmp_path):
    """A bare run states `NONE`; absence is not a statement."""
    run = tmp_path / "run"
    run.mkdir()
    assert dispatchrecovery.read_master(run, recorded_only=True) is None
    assert dispatchrecovery.declare_master(run) == MasterBinding(kind="NONE")
    on_disk = json.loads((run / "recovery" / "master.json").read_text(encoding="utf-8"))
    assert on_disk["kind"] == "NONE" and on_disk["session"] is None


def test_a_governed_declaration_wins_and_a_bare_resume_keeps_it(tmp_path):
    """Kills P6 (a bare invocation overwriting the master)."""
    run = tmp_path / "run"
    run.mkdir()
    gov = dispatchrecovery.declare_master(run, session="s1", epoch=3)
    assert gov == MasterBinding(kind="GOVERNED", session="s1", epoch=3)
    assert dispatchrecovery.declare_master(run) == gov
    assert dispatchrecovery.read_master(run) == gov
    assert dispatchrecovery.declare_master(run, session="s2", epoch=4).epoch == 4


@pytest.mark.parametrize("session,epoch", [("s", None), (None, 2), ("s", 0), ("s", True),
                                           ("  ", 2)])
def test_a_half_or_malformed_master_is_refused(tmp_path, session, epoch):
    run = tmp_path / "run"
    run.mkdir()
    with pytest.raises(RecoveryError):
        dispatchrecovery.declare_master(run, session=session, epoch=epoch)
    assert not (run / "recovery" / "master.json").exists()


def test_run_flags_declare_the_master_and_accounts(tmp_path, repo, monkeypatch, capsys):
    """Kills P12 (`recovery=` not passed to the controller): `hoh run` with the
    flags records the declaration, and the controller it builds carries the
    account; a half declaration, a bare pasted key and a key in either slot of
    `ROLE=HANDLE` are exit 2, and the key is on neither stream."""
    from test_governance import _initialised_storage_root

    seen = {}

    class Spy(Controller):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen["recovery"] = self.recovery

    monkeypatch.setattr("hoh.controller.Controller", Spy)
    _initialised_storage_root(tmp_path, monkeypatch)
    ctrl, state, store, fake = build(tmp_path, repo)
    root = str(tmp_path / "runs")
    base = ["--root", root, "run", "r1", "--iterations", "0", "--no-herdr"]
    rc = cli.main([*base, "--master-session", "sess-9", "--master-epoch", "6",
                   "--role-account", "qa=work-account-2"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert dispatchrecovery.read_master(store.dir) == MasterBinding(
        kind="GOVERNED", session="sess-9", epoch=6)
    assert '"qa": "work-account-2"' in out and "same-route-wait/1" in out
    assert seen["recovery"].accounts == {Role.QA: "work-account-2"}

    key = "sk-ant-api03-AbCdEf0123456789xyz"
    for bad in (["--master-session", "x"], ["--master-epoch", "2"],
                ["--role-account", f"qa={key}"], ["--role-account", key],
                ["--role-account", f"{key}=qa"], ["--role-account", f"{key[:20]}=qa"],
                ["--master-session", "sess-1", "--master-epoch", "5"]):
        rc = cli.main([*base, *bad])
        captured = capsys.readouterr()
        assert rc == 2 and "recovery declaration refused" in captured.err, bad
        assert "AbCdEf0123456789xyz" not in captured.err + captured.out
        assert key[:20] not in captured.err + captured.out
    # The refused lower epoch changed nothing.
    assert dispatchrecovery.read_master(store.dir).epoch == 6


def test_the_run_command_still_pins_a_contract_without_herdr(tmp_path, repo, monkeypatch, capsys):
    """The hermetic stand-in for the deselected governance node (which needs
    `HERDR_ENV=1`): `hoh run --autonomy ... --program-epoch 5 --iterations 0
    --no-herdr` pins the contract and writes the evidence file -- the one
    `_cmd_run`-with-a-contract guard K11 would otherwise lose."""
    from test_governance import _initialised_storage_root

    _initialised_storage_root(tmp_path, monkeypatch)
    ctrl, state, store, fake = build(tmp_path, repo)
    contract = tmp_path / "contract.json"
    contract.write_text(pa_contract().model_dump_json(indent=2), encoding="utf-8")
    rc = cli.main(["--root", str(tmp_path / "runs"), "run", "r1", "--autonomy", str(contract),
                   "--program-epoch", "5", "--iterations", "0", "--no-herdr"])
    capsys.readouterr()
    assert rc == 0
    from hoh.governance import bind

    governor = bind(store.dir)
    assert governor is not None and governor.contract.program_epoch == 5
    assert governor.contract.contract_id == "pa"
    assert (store.dir / "approval" / "autonomy.json").is_file()


def test_a_bare_run_command_records_none(tmp_path, repo, monkeypatch, capsys):
    from test_governance import _initialised_storage_root

    _initialised_storage_root(tmp_path, monkeypatch)
    ctrl, state, store, fake = build(tmp_path, repo)
    rc = cli.main(["--root", str(tmp_path / "runs"), "run", "r1", "--iterations", "0",
                   "--no-herdr"])
    capsys.readouterr()
    assert rc == 0
    assert dispatchrecovery.read_master(store.dir, recorded_only=True) == MasterBinding(
        kind="NONE")


def test_the_launcher_passes_its_master_and_accounts_to_every_run(tmp_path, monkeypatch):
    """Kills P7 (the launcher drops the flags) and P8 (the governed drive never
    tells the launcher its master)."""
    from hoh import resumeloop
    from hoh.launcher import HohRunLauncher
    from hoh.orchestrator import TaskNode

    seen: list[list[str]] = []

    class Done:
        exit_code = 0
        stdout = ""
        stderr = ""

    def spy(self, argv):
        seen.append(argv)
        return Done()

    monkeypatch.setattr(HohRunLauncher, "_run_cli", spy)
    monkeypatch.setattr(HohRunLauncher, "_verdict", lambda self, *a, **k: "stub")
    monkeypatch.setattr("hoh.launcher.RunStore.read_state", lambda self: object())
    bare = HohRunLauncher(tmp_path, tmp_path)
    governed = HohRunLauncher(tmp_path, tmp_path, role_accounts={"qa": "work-account-2"})
    governed.bind_master("sess-9", 6)
    node = TaskNode(id="n1", run_id="n1")
    bare.launch(node)
    governed.launch(node)
    assert "--master-session" not in seen[0] and "--role-account" not in seen[0]
    argv = seen[1]
    assert argv[argv.index("--master-session") + 1] == "sess-9"
    assert argv[argv.index("--master-epoch") + 1] == "6"
    assert argv[argv.index("--role-account") + 1] == "qa=work-account-2"

    class FakeAuthority:
        session_id, epoch = "sess-3", 11

    bound = []

    class Launcher:
        def bind_master(self, session, epoch):
            bound.append((session, epoch))

    def factory(store):
        return Launcher(), None

    class Boom(Exception):
        pass

    monkeypatch.setattr("hoh.orchestrator.ProjectController",
                        lambda *a, **k: (_ for _ in ()).throw(Boom("stop here")))
    resumeloop.drive_governed(object(), FakeAuthority(), factory)
    assert bound == [("sess-3", 11)]


# --------------------------------------------------------------------------- #
# P9, P10 -- the retry time is the provider's stated reset, in its zone
# --------------------------------------------------------------------------- #

UTC = timezone.utc


def test_the_stated_reset_and_zone_are_the_retry_time():
    """Kills P9 (the old `now + 1 h` for every banner) and P10 (the zone
    ignored): pcon's banner at 21:19Z names 3:20am Istanbul, which is 00:20Z of
    the next day, and the retry is one minute after it."""
    now = datetime(2026, 10, 8, 21, 19, tzinfo=UTC)
    assert quota.next_attempt_at(QA_LIMIT, now=now) == datetime(2026, 10, 9, 0, 21, tzinfo=UTC)
    # Before the reset, inside the margin, and after it (tomorrow's):
    assert quota.next_attempt_at(QA_LIMIT, now=datetime(2026, 10, 9, 0, 10, tzinfo=UTC)) == \
        datetime(2026, 10, 9, 0, 21, tzinfo=UTC)
    assert quota.next_attempt_at(
        QA_LIMIT, now=datetime(2026, 10, 9, 0, 20, 30, tzinfo=UTC)) == \
        datetime(2026, 10, 9, 0, 21, tzinfo=UTC)
    assert quota.next_attempt_at(QA_LIMIT, now=datetime(2026, 10, 9, 0, 30, tzinfo=UTC)) == \
        datetime(2026, 10, 10, 0, 21, tzinfo=UTC)
    assert quota.next_attempt_at("You've hit your limit · resets 9pm (America/New_York)",
                                 now=datetime(2026, 11, 1, 0, 30, tzinfo=UTC)) == \
        datetime(2026, 11, 1, 1, 1, tzinfo=UTC)
    assert failures.next_attempt(failures.NormalizedFailure.USAGE_LIMIT, QA_LIMIT, now=now) == \
        datetime(2026, 10, 9, 0, 21, tzinfo=UTC)


def test_a_reset_without_a_known_zone_is_not_guessed():
    """Kills P10 (an unknown zone read as UTC): no evidence, the default wait.
    The older `resets at 11pm` rule is unchanged."""
    now = datetime(2026, 10, 8, 21, 19, tzinfo=UTC)
    for text in ("Usage limit reached · resets 3:20am (Mars/Olympus_Mons)",
                 "Usage limit reached · resets 3:20am",
                 "Usage limit reached · resets 3 requests (Europe/Istanbul)"):
        assert quota.next_attempt_at(text, now=now) == now + quota.DEFAULT_WAIT, text
    assert quota.next_attempt_at("limit resets at 11pm",
                                 now=datetime(2026, 10, 4, 21, 19, tzinfo=UTC)) == \
        datetime(2026, 10, 4, 23, 0, tzinfo=UTC)


def test_a_role_s_words_on_the_screen_do_not_move_the_retry_time():
    """Kills P18 (the duration rule ahead of the stated reset, or read from any
    line) and P20 (a role's own reset line supplying a time or zone): the
    banner's reset is 00:21Z whatever the role printed around it."""
    now = datetime(2026, 10, 8, 21, 19, tzinfo=UTC)
    want = datetime(2026, 10, 9, 0, 21, tzinfo=UTC)
    for role_line in ("● tests finished in 5m", "● the build took 2h to finish",
                      "● retry in 5m is what the docs say",
                      "● resets at 9pm", "● resets 5pm (Pacific/Kiritimati)",
                      "● Usage limit reached ... in 5m"):
        assert quota.next_attempt_at(QA_LIMIT + "\n" + role_line, now=now) == want, role_line
        assert quota.next_attempt_at(role_line + "\n" + QA_LIMIT, now=now) == want, role_line
    # Pane-wrapped, as the controller sees it:
    assert quota.next_attempt_at(pane(FIRST, "● tests finished in 5m",
                                      "⎿  You've hit your session limit · resets 3:20am "
                                      "(Europe/Istanbul)"), now=now) == want


def test_a_duration_is_read_only_where_a_limit_is_discussed_and_is_bounded():
    """Kills P13 (`\\b` of the duration rule) and guards the overflow: `begin 5m`
    is not `in 5m`, a number no provider means is ignored, and nothing raises."""
    now = datetime(2026, 10, 8, 21, 19, tzinfo=UTC)
    assert quota.next_attempt_at("quota exhausted, retry in 2h 30m", now=now) == \
        now + timedelta(hours=2, minutes=30)
    assert quota.next_attempt_at("quota exhausted, retry begin 5m", now=now) == \
        now + quota.DEFAULT_WAIT
    assert quota.next_attempt_at("quota exhausted, retry in 99999999999h", now=now) == \
        now + quota.DEFAULT_WAIT
    assert quota.next_attempt_at("finished in 5m", now=now) == now + quota.DEFAULT_WAIT


def test_a_parked_usage_limit_waits_for_the_stated_reset(tmp_path, repo):
    """Kills P9 end to end: the state carries the provider's reset and not
    `now + 1 h`."""
    ctrl, state, store, fake, _ = _qa_limit_park(tmp_path, repo)
    retry = datetime.fromisoformat(state.retry_after)
    assert retry.astimezone(timezone(timedelta(hours=3))).strftime("%H:%M") == "03:21"
    assert retry - datetime.now(UTC) > timedelta(0)
    [timer] = armed(store)
    assert timer.reason.startswith("usage limit: ")


# --------------------------------------------------------------------------- #
# Revision: the minors of the P2-43b integration reviews
# --------------------------------------------------------------------------- #

KEY = "sk-ant-api03-AbCdEf0123456789xyz"


def test_a_rejected_handle_is_not_quoted_by_the_record_model():
    """Kills Q3 (`hide_input_in_errors` dropped): the pydantic error of a bad
    handle in `DispatchProvenance` no longer prints it (it used to)."""
    with pytest.raises(ValueError) as caught:
        DispatchProvenance(provider="claude", model="m", role="qa", account_handle=KEY,
                           capability_requirements=["x"])
    assert KEY not in str(caught.value) and "AbCdEf0123456789xyz" not in str(caught.value)


def test_the_launcher_refuses_an_unknown_role_name_without_naming_it():
    """Kills Q13 (the launcher's role names not validated)."""
    from hoh.launcher import HohRunLauncher

    with pytest.raises(RecoveryError) as caught:
        HohRunLauncher(Path("."), Path("."), role_accounts={KEY: "work-account-2"})
    assert KEY not in str(caught.value)
    assert HohRunLauncher(Path("."), Path("."), role_accounts={"qa": "work-account-2"}
                          ).role_accounts == {"qa": "work-account-2"}


def test_a_key_in_the_wrong_option_is_not_echoed(tmp_path, repo, monkeypatch, capsys):
    """Kills Q4 (argparse echoing a mistyped `--master-epoch`) and Q16 (the session
    check): a key pasted as the epoch or as the session id is exit 2 and on neither
    stream; a UUID session id is a name and is accepted."""
    from test_governance import _initialised_storage_root

    _initialised_storage_root(tmp_path, monkeypatch)
    ctrl, state, store, fake = build(tmp_path, repo)
    base = ["--root", str(tmp_path / "runs"), "run", "r1", "--iterations", "0", "--no-herdr"]
    with pytest.raises(SystemExit) as exited:
        cli.main([*base, "--master-session", "sess-1", "--master-epoch", KEY])
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert "AbCdEf0123456789xyz" not in captured.err + captured.out
    rc = cli.main([*base, "--master-session", KEY, "--master-epoch", "3"])
    captured = capsys.readouterr()
    assert rc == 2 and "AbCdEf0123456789xyz" not in captured.err + captured.out
    assert dispatchrecovery.read_master(store.dir, recorded_only=True) is None
    uuid = "3f2b8a1c-9d4e-4b7a-8c1d-5e6f7a8b9c0d"
    assert cli.main([*base, "--master-session", uuid, "--master-epoch", "3"]) == 0
    capsys.readouterr()
    assert dispatchrecovery.read_master(store.dir).session == uuid


@pytest.mark.parametrize("line", ["null", "5", "[]", '"x"', "true"])
def test_a_routing_line_that_is_not_an_object_is_a_problem_not_a_crash(tmp_path, line):
    """Kills Q12 (`read_records` raising on a JSON line that is not an object)."""
    run = tmp_path / "run"
    (run / "recovery").mkdir(parents=True)
    (run / "recovery" / "routing.jsonl").write_text(line + "\n", encoding="utf-8")
    records, problems = dispatchrecovery.read_records(run)
    assert records == [] and len(problems) == 1


def test_a_routing_file_that_is_a_symbolic_link_is_not_appended_to(tmp_path, repo):
    """Kills Q5 (`single_link=True` dropped): the record is refused, the park
    degrades to a person, and the file the link points at is untouched."""
    ctrl, state, store, fake = build(tmp_path, repo)
    _bind_roles(fake)
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("keep\n", encoding="utf-8")
    (store.dir / "recovery").mkdir(parents=True)
    (store.dir / "recovery" / "routing.jsonl").symlink_to(target)
    fake.qa_raise = answer_missing(QA_LIMIT, followed_up=True)
    ctrl.run_iteration(state)
    assert target.read_text(encoding="utf-8") == "keep\n"
    assert state.condition is Condition.BLOCKED and armed(store) == []
    assert "recovery record NOT written" in history(state)


def test_the_record_failure_note_names_the_type_and_quotes_only_what_is_safe(tmp_path, repo,
                                                                            monkeypatch):
    """The documented rule: the message is added for every exception except a
    `ValueError` that is not a `RecoveryError` (pydantic's `ValidationError`
    quotes its input)."""
    for exc, shown in ((RuntimeError("disk said x1"), True), (OSError("disk said x2"), True),
                       (ValueError("quoted y3"), False), (RecoveryError("plain z4"), True)):
        def refuse(run_dir, record, _exc=exc):
            raise _exc

        monkeypatch.setattr(dispatchrecovery, "append_record", refuse)
        sub = tmp_path / type(exc).__name__
        sub.mkdir()
        ctrl, state, store, fake = build(sub, repo)
        fake.qa_raise = answer_missing(QA_LIMIT, followed_up=True)
        ctrl.run_iteration(state)
        note = [ln for ln in state.history if "recovery record NOT written" in ln][0]
        assert type(exc).__name__ in note
        assert (str(exc) in note) is shown, note


def test_the_roles_capability_requirements_and_the_data_class_are_written_out():
    """Kills Q6 (a table compared with itself): the three tables and the stated
    unknown are literals here."""
    assert dispatchrecovery.CAPABILITY_REQUIREMENTS == {
        "planner": ("read_repo", "structured_answer"),
        "developer": ("read_repo", "write_worktree", "run_shell"),
        "qa": ("read_repo", "run_shell", "structured_answer")}
    assert dispatchrecovery.UNSPECIFIED == "UNSPECIFIED"
    assert dispatchrecovery.ROUTE_POLICY_ID == "same-route-wait"
    assert dispatchrecovery.ROUTE_POLICY_VERSION == 1


def test_durations_and_clock_times_that_cannot_be_read_are_the_default_wait():
    """Kills Q7 (`\\d{1,5}`), Q9 (the 72 h cap), Q10 (the minute guard) and Q11 (the rollover
    boundary): a 4301-digit
    number and `resets at 99:99` do not raise; 72 h is read and 73 h and 7200 h
    are not; the retry is strictly after `now` even exactly at the margin."""
    now = datetime(2026, 10, 8, 21, 19, tzinfo=UTC)
    d = quota.DEFAULT_WAIT
    assert quota.next_attempt_at("limit: retry in " + "9" * 4301 + "h", now=now) == now + d
    assert quota.next_attempt_at("limit: retry in " + "9" * 4301 + "m", now=now) == now + d
    assert quota.next_attempt_at("limit resets at 99:99", now=now) == now + d
    assert quota.next_attempt_at("limit: retry in 72h", now=now) == now + timedelta(hours=72)
    for text in ("limit: retry in 73h", "limit: retry in 7200h", "limit: retry in 99999h"):
        assert quota.next_attempt_at(text, now=now) == now + d, text
    at_margin = datetime(2026, 10, 9, 0, 21, tzinfo=UTC)
    assert quota.next_attempt_at(QA_LIMIT, now=at_margin) == datetime(2026, 10, 10, 0, 21,
                                                                      tzinfo=UTC)


def test_the_autumn_change_is_not_a_day_late():
    """Kills Q8 (one fold only): `1:30am` happens twice on 2026-11-01 in New York;
    at 05:45Z (01:45 EDT, first pass) the next one is 06:30Z, not tomorrow's."""
    banner = "⎿  You've hit your session limit · resets 1:30am (America/New_York)"
    assert quota.next_attempt_at(banner, now=datetime(2026, 11, 1, 5, 45, tzinfo=UTC)) == \
        datetime(2026, 11, 1, 6, 31, tzinfo=UTC)
    assert quota.next_attempt_at(banner, now=datetime(2026, 11, 1, 6, 45, tzinfo=UTC)) == \
        datetime(2026, 11, 2, 6, 31, tzinfo=UTC)


def test_residual_a_banner_shaped_role_line_before_the_banner_wins():
    """PINNED RESIDUAL (the first banner-shaped line supplies the time and the
    zone): a role's `● Usage limit reached · resets 5pm (Pacific/Kiritimati)` ahead
    of the real banner moves the retry to 03:01Z instead of 00:21Z. Bounded by a
    day and by the resume attempts; reading the last banner instead would trade it
    for the opposite error. If this changes, say so."""
    now = datetime(2026, 10, 8, 21, 19, tzinfo=UTC)
    role = "● Usage limit reached · resets 5pm (Pacific/Kiritimati)"
    assert quota.next_attempt_at(role + "\n" + QA_LIMIT, now=now) == \
        datetime(2026, 10, 9, 3, 1, tzinfo=UTC)
    assert quota.next_attempt_at(QA_LIMIT + "\n" + role, now=now) == \
        datetime(2026, 10, 9, 0, 21, tzinfo=UTC)


def test_residual_without_a_stated_zone_a_limit_line_s_duration_or_clock_is_read():
    """PINNED RESIDUAL: with no banner zone, `retry in 5m` on a line that mentions a
    limit, and `limit resets at 9pm`, are read even if a role wrote them."""
    now = datetime(2026, 10, 8, 21, 19, tzinfo=UTC)
    assert quota.next_attempt_at("● the limit retry in 5m is documented", now=now) == \
        now + timedelta(minutes=5)
    assert quota.next_attempt_at("● the limit resets at 9pm", now=now) == \
        datetime(2026, 10, 9, 21, 0, tzinfo=UTC)


def test_the_spring_forward_change_is_not_read_as_an_earlier_instant():
    """Kills Q21 (every fold of a skipped time accepted): `2:30am` does not exist in
    New York on 2026-03-08. The clock reads it as 03:30 daylight time (07:30Z); the
    earlier-fold reading 06:30Z is 01:30 standard time, an hour before the change --
    an early resume that meets the limit again (P2-43b-fix spec review B, F1)."""
    banner = "⎿  You've hit your session limit · resets 2:30am (America/New_York)"
    assert quota.next_attempt_at(banner, now=datetime(2026, 3, 8, 5, 0, tzinfo=UTC)) == \
        datetime(2026, 3, 8, 7, 31, tzinfo=UTC)
    # An ordinary time on the same day is unaffected:
    assert quota.next_attempt_at(banner.replace("2:30am", "4:30am"),
                                 now=datetime(2026, 3, 8, 5, 0, tzinfo=UTC)) == \
        datetime(2026, 3, 8, 8, 31, tzinfo=UTC)


def test_a_session_id_with_a_double_slash_is_refused():
    """Kills Q22 (the `//` clause): the characters are all allowed one by one, the
    pair is the start of an address with a login."""
    for bad in ("a//b", "https://user@host", "sess//1"):
        with pytest.raises(RecoveryError) as caught:
            dispatchrecovery.check_session_id(bad)
        assert bad not in str(caught.value)
    assert dispatchrecovery.check_session_id("a/b") == "a/b"
