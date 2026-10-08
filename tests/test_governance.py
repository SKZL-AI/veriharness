"""Does a real run obey its contract?

P2-24. The unit tests in `test_autonomy.py` show that `route()` answers
correctly. They cannot show that anything *asks* it -- which is why two
reviewers refused to call the capability proven while the function sat unused,
and why every case here drives the product path: `stages.block` through
`block_under_contract`, the delivery gate through `may_release`, and the
pinning through the run directory a resumed run reads back.

The negative controls matter more than the positive ones. A contract that
cannot stop anything is decoration, so each case names what would have happened
without it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hoh.autonomy import (
    AutonomyContract,
    AutonomyMode,
    Escalation,
    Handling,
    Horizon,
    NeedsYou,
    ReleasePolicy,
    compile_profile,
)
from hoh.contracts import Budgets, Condition, RunState
from hoh.governance import (
    ContractRefused,
    Governor,
    bind,
    block_under_contract,
    may_release,
    pinned_path,
)
from hoh.project import ActionClass

UNATTENDED = dict(scope=("src/hoh/",),
                  horizon=Horizon(max_wallclock_seconds=3600, review_after_seconds=900),
                  budgets=Budgets(max_dispatches=10, max_wallclock_seconds=3600))


def _state(**kw) -> RunState:
    return RunState(run_id="d7", repo_path="/x", project_name="p", spec_path="/x/s.md",
                    spec_digest="a" * 16, policy_digest="b" * 16,
                    profile_digest="c" * 16, **kw)


def _supervised() -> AutonomyContract:
    return compile_profile(AutonomyMode.SUPERVISED, contract_id="sup")


def _unattended(**kw) -> AutonomyContract:
    return compile_profile(AutonomyMode.UNATTENDED_LONG_HORIZON, contract_id="un",
                           **{**UNATTENDED, **kw})


# --- pinning: the run obeys the contract it began under -------------------------

def test_a_run_without_a_contract_behaves_exactly_as_before(tmp_path):
    """The old behaviour stays legal: no contract means no contract, not a
    silent default that would govern every run written before this existed."""
    assert bind(tmp_path) is None
    state = _state()
    assert block_under_contract(state, "something unclear", None) is None
    assert state.condition is Condition.BLOCKED
    assert state.blocked_reason == "something unclear"


def test_the_contract_is_pinned_once_and_read_back(tmp_path):
    governor = bind(tmp_path, contract=_unattended(), program_epoch=3)
    assert governor is not None and governor.contract.program_epoch == 3
    assert pinned_path(tmp_path).is_file()

    again = bind(tmp_path)                    # a resumed run offers nothing
    assert again.contract.digest() == governor.contract.digest()
    assert again.contract.program_epoch == 3


def test_a_resumed_run_may_not_be_handed_a_different_contract(tmp_path):
    """"Which rules was this run under" must have one answer. Offering another
    contract on resume is refused rather than adopted."""
    bind(tmp_path, contract=_unattended(), program_epoch=1)
    with pytest.raises(ContractRefused) as caught:
        bind(tmp_path, contract=_supervised())
    assert "pinned to contract un" in str(caught.value)
    assert "cannot be resumed under sup" in str(caught.value)
    # And the same contract again is fine, including after a re-pin attempt.
    assert bind(tmp_path, contract=_unattended()).contract.program_epoch == 1


def test_an_incoherent_contract_is_never_pinned(tmp_path):
    bad = _unattended().model_copy(update={"needs_you": NeedsYou.ON})
    with pytest.raises(ContractRefused):
        bind(tmp_path, contract=bad)
    assert not pinned_path(tmp_path).exists()


# --- what a block means, through stages.block ------------------------------------

def test_an_attended_run_still_waits_for_a_person(tmp_path):
    governor = bind(tmp_path, contract=_supervised())
    state = _state()
    routing = block_under_contract(state, "the developer needs folder trust", governor)
    assert routing.handling is Handling.ASK_NOW
    assert state.condition is Condition.BLOCKED
    assert state.blocked_kind == "human"
    assert not state.retry_after, "nobody promised to come back: a person was asked"


def test_an_unattended_run_parks_the_branch_with_a_time_to_look_again(tmp_path):
    """The never-stuck invariant is why the retry time matters: a parked branch
    with no time to look again is the control-plane defect `hoh stuck` reports."""
    governor = bind(tmp_path, contract=_unattended())
    state = _state()
    routing = block_under_contract(state, "the developer needs folder trust", governor)
    assert routing.handling is Handling.PARK_BRANCH
    assert state.condition is Condition.BLOCKED
    assert state.blocked_kind == "parked_by_contract"
    assert state.retry_after, "a parked branch owes a retry time"

    from hoh.sentinel import stuck_run

    assert stuck_run(state) == [], "a parked branch is not a stuck one"


def test_a_contract_that_stops_the_programme_stops_it(tmp_path):
    contract = _unattended(escalation={"decision": Escalation.STOP_PROGRAM},
                           note="stop the whole programme is intended here")
    governor = bind(tmp_path, contract=contract)
    state = _state()
    routing = block_under_contract(state, "a licence question", governor)
    assert routing.handling is Handling.STOP_PROGRAM
    assert state.condition is Condition.CANCELLED
    assert "the contract stops the programme" in state.stop_reason


def test_a_digest_run_collects_the_question_and_still_names_a_retry(tmp_path):
    governor = bind(tmp_path, contract=compile_profile(
        AutonomyMode.PLAN_AUTONOMOUS, contract_id="plan",
        horizon=Horizon(review_after_seconds=300)))
    state = _state()
    routing = block_under_contract(state, "rename a public flag?", governor)
    assert routing.handling is Handling.COLLECT_FOR_DIGEST
    assert state.blocked_kind == "collected_for_digest"
    assert state.retry_after


def test_a_block_is_never_resolved_by_policy_whatever_the_contract_says(tmp_path):
    """A block *is* a decision the run could not take. No mode may turn it into
    "carry on" -- the case that would be a fabricated approval."""
    for contract in (_supervised(), _unattended(),
                     compile_profile(AutonomyMode.POLICY_GOVERNED, contract_id="pg"),
                     compile_profile(AutonomyMode.PLAN_AUTONOMOUS, contract_id="pa")):
        run_dir = Path(tmp_path) / contract.contract_id
        run_dir.mkdir()
        governor = bind(run_dir, contract=contract)
        state = _state()
        routing = block_under_contract(state, "something nobody decided", governor)
        assert routing.handling is not Handling.RESOLVED_BY_POLICY
        assert routing.blocks_branch
        assert state.condition in (Condition.BLOCKED, Condition.CANCELLED)


# --- the release gate -------------------------------------------------------------

def test_a_release_is_refused_unless_the_contract_names_where_to_publish(tmp_path):
    for contract in (_supervised(), _unattended(),
                     compile_profile(AutonomyMode.PLAN_AUTONOMOUS, contract_id="pa")):
        run_dir = Path(tmp_path) / f"r-{contract.contract_id}"
        run_dir.mkdir()
        governor = bind(run_dir, contract=contract)
        routing = may_release(governor, what="deliver d7 into main")
        assert routing is not None and routing.blocks_branch, contract.mode
        assert routing.handling is not Handling.RESOLVED_BY_POLICY


def test_a_release_proceeds_when_the_contract_authorises_that_target(tmp_path):
    contract = AutonomyContract(
        contract_id="rel", mode=AutonomyMode.PLAN_AUTONOMOUS,
        needs_you=NeedsYou.DIGEST_ONLY,
        release_policy=ReleasePolicy.AUTO_PUBLISH_PREAUTHORIZED_TARGET,
        preauthorized_target="github.com/SKZL-AI/veriharness",
        # Publishing is an external action, so the contract has to allow the
        # class as well as name the target -- two separate permissions, which
        # is the point of having both.
        allowed_action_classes=(ActionClass.INTERNAL, ActionClass.EXTERNAL),
        escalation={"decision": Escalation.AUTO_POLICY,
                    ActionClass.EXTERNAL.value: Escalation.HUMAN_IF_AVAILABLE,
                    "release": Escalation.AUTO_POLICY})
    governor = bind(tmp_path, contract=contract)
    routing = may_release(governor, what="deliver d7 into main")
    assert routing.handling is Handling.RESOLVED_BY_POLICY
    assert not routing.blocks_branch


def test_a_delivery_that_leaves_this_machine_needs_the_external_class(tmp_path):
    """And one that does not, does not. A reviewer measured the previous shape:
    every delivery was routed EXTERNAL, so a `local-only` fast-forward -- which
    this project's own taxonomy calls INTERNAL and reversible -- was refused
    un-overridably in all four profiles. A gate that refuses correct work is
    how a gate teaches people to switch it off."""
    governor = bind(tmp_path, contract=_unattended())
    assert not governor.contract.may(ActionClass.EXTERNAL)

    leaving = may_release(governor, what="open a PR", mode="direct-PR")
    assert leaving.blocks_branch
    assert "not allowed by this contract at all" in leaving.why

    local = may_release(governor, what="fast-forward into main", mode="local-only")
    assert local.kind == "release", "a local merge is judged by the release policy"
    assert "not allowed by this contract at all" not in local.why


def test_without_a_contract_the_delivery_rules_decide_alone(tmp_path):
    assert may_release(None, what="deliver") is None


# --- the record -------------------------------------------------------------------

def test_every_routing_is_written_down_with_the_rule_that_produced_it(tmp_path):
    governor = bind(tmp_path, contract=_unattended(), program_epoch=2)
    block_under_contract(_state(), "trust dialog", governor)
    may_release(governor, what="deliver")
    rows = governor.routings()
    assert len(rows) == 2
    for row in rows:
        assert row["why"] and row["handling"] and row["kind"]
        assert row["contract_digest"] == governor.contract.digest()
        assert row["program_epoch"] == 2
        assert row["at"].endswith("Z")
    assert json.loads(pinned_path(tmp_path).read_text())["contract_id"] == "un"
    assert governor.evidence()["routings"] == 2


def test_the_record_survives_the_process_that_wrote_it(tmp_path):
    governor = bind(tmp_path, contract=_unattended())
    block_under_contract(_state(), "trust dialog", governor)
    assert Governor(contract=governor.contract, run_dir=Path(tmp_path)).routings()


# --- the CLI path, end to end ------------------------------------------------------

def test_the_run_command_pins_a_contract_and_a_resume_reads_it_back(tmp_path):
    """Through `hoh`, not through the module: the flag exists, the contract is
    written into the run directory, and a second invocation does not recompile
    it."""
    root = tmp_path / "runs"
    run_dir = root / "r1"
    run_dir.mkdir(parents=True)
    contract = _unattended()
    governor = bind(run_dir, contract=contract, program_epoch=7)
    assert governor.contract.program_epoch == 7

    from hoh.cli import build_parser

    action = next(a for a in build_parser()._subparsers._group_actions)
    run_parser = action.choices["run"]
    flags = {f for a in run_parser._actions for f in a.option_strings}
    assert "--autonomy" in flags and "--program-epoch" in flags
    parsed = run_parser.parse_args(["r1", "--autonomy", "c.json"])
    assert parsed.autonomy == "c.json"

    # And the pinned contract is what a later bind returns, whatever is offered.
    with pytest.raises(ContractRefused):
        bind(run_dir, contract=_supervised())


# --- the CLI's own path: does `hoh deliver` actually consult the contract? --------

def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _checkpointed_run(tmp_path: Path):
    """A run directory in the one state `hoh deliver` will act on.

    Built through the real store and the real stage machine, because a state
    this test hand-wrote would prove nothing about the command.
    """
    from hoh.contracts import Candidate, Stage
    from hoh.store import RunStore

    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    head = __import__("subprocess").run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()

    spec = tmp_path / "spec.md"
    spec.write_text("# spec\n", encoding="utf-8")
    store = RunStore(tmp_path / "runs", "r1")
    state = RunState(run_id="r1", repo_path=str(repo), project_name="p",
                     spec_path=str(spec), spec_digest="a" * 16,
                     policy_digest="b" * 16, profile_digest="c" * 16,
                     stage=Stage.CHECKPOINTED,
                     last_accepted_candidate=Candidate(
                         candidate_id="r1-i1", repo_path=str(repo), commit=head,
                         tree_clean=True, tree_digest="d" * 16))
    store.write_state(state)
    return store, repo


def test_the_deliver_command_refuses_under_a_contract_that_forbids_it(tmp_path, capsys):
    """The wiring, not the module: `cli.main(["deliver", ...])` must consult the
    pinned contract. Without this case, `may_release` could be correct and
    called by nothing -- which is exactly the state P2-24 was held at."""
    from hoh import cli

    store, _ = _checkpointed_run(tmp_path)
    bind(store.dir, contract=_unattended())

    rc = cli.main(["--root", str(tmp_path / "runs"), "deliver", "r1", "--into", "main"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "autonomy contract" in err
    # local-only under NEVER_AUTO_PUBLISH: the release policy refuses, and the
    # routing that did it is the release kind rather than the action class.
    assert "not allowed by this contract at all" not in err
    assert bind(store.dir).routings()[-1]["kind"] == "release"

    governor = bind(store.dir)
    assert governor.routings(), "the refusal is recorded, not only printed"


def test_the_deliver_command_is_unchanged_without_a_contract(tmp_path, capsys):
    """The negative control for the case above: the same run, no contract, and
    the delivery reaches the ordinary delivery rules instead of the autonomy
    refusal. Without this, the test above would pass for a command that refuses
    everything."""
    from hoh import cli

    store, _ = _checkpointed_run(tmp_path)
    rc = cli.main(["--root", str(tmp_path / "runs"), "deliver", "r1", "--into", "main"])
    err = capsys.readouterr().err
    assert "autonomy contract" not in err
    assert rc in (0, 2)          # whatever delivery decides, it is delivery deciding


def _initialised_storage_root(tmp_path: Path, monkeypatch) -> Path:
    """STORAGE-B3 fixture: `hoh run` refuses to start without an initialised
    storage state root, and on WSL without a host volume. An initialised
    `tmp_path` root through `HOH_STATE_ROOT` -- and, when the WSL probe says
    WSL, a `host_paths` policy and `storage-host.json` naming a `tmp_path`
    directory -- so the run command reaches what this test asserts."""
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


def test_the_run_command_accepts_and_pins_a_contract_file(tmp_path, capsys, monkeypatch):
    """`hoh run --autonomy` is the operator-facing half: the file is read, the
    contract is pinned into the run directory, and the evidence file names it."""
    from hoh import cli

    _initialised_storage_root(tmp_path, monkeypatch)
    store, _ = _checkpointed_run(tmp_path)
    path = tmp_path / "contract.json"
    path.write_text(_unattended().model_dump_json(indent=2), encoding="utf-8")

    rc = cli.main(["--root", str(tmp_path / "runs"), "run", "r1",
                   "--autonomy", str(path), "--program-epoch", "5",
                   "--iterations", "0"])
    assert rc == 0, "a run of zero iterations under a valid contract is not an error"
    assert pinned_path(store.dir).is_file(), "the contract was not pinned by `hoh run`"
    pinned = json.loads(pinned_path(store.dir).read_text())
    assert pinned["contract_id"] == "un" and pinned["program_epoch"] == 5
    evidence = json.loads((store.dir / "approval" / "autonomy.json").read_text())
    assert evidence["mode"] == "UNATTENDED_LONG_HORIZON"
    # The evidence digest is the pinned contract's, recomputed rather than
    # copied: a reviewer caught this assertion as a tautology that any
    # non-empty string satisfied.
    from hoh.autonomy import AutonomyContract

    assert evidence["digest"] == AutonomyContract.model_validate(pinned).digest()
    assert evidence["program_epoch"] == 5


# --- what the second review measured ----------------------------------------------

def test_b1_an_unreadable_pin_is_a_refusal_not_a_traceback(tmp_path):
    """Every call site returns an exit code. A reviewer got pydantic tracebacks
    out of `hoh run` and `hoh deliver`, and -- worst -- out of the abort handler
    whose whole job is to end bounded."""
    bind(tmp_path, contract=_unattended())
    pinned_path(tmp_path).write_text("{ not json", encoding="utf-8")
    with pytest.raises(ContractRefused) as caught:
        bind(tmp_path)
    assert "cannot be read" in str(caught.value)


def test_b1_a_broken_pin_never_swallows_the_block(tmp_path, capsys):
    """The abort handler writes the block whatever the contract file says."""
    from hoh import cli
    from hoh.contracts import Condition
    from hoh.store import RunStore

    store, _ = _checkpointed_run(tmp_path)
    bind(store.dir, contract=_unattended())
    pinned_path(store.dir).write_text("{ not json", encoding="utf-8")

    rc = cli.main(["--root", str(tmp_path / "runs"), "deliver", "r1", "--into", "main"])
    assert rc == 2 and "autonomy contract refused" in capsys.readouterr().err

    # And the run-loop path: the block is still written.
    from hoh.governance import block_under_contract

    state = RunStore(tmp_path / "runs", "r1").read_state()
    block_under_contract(state, "iteration aborted: boom", None)
    assert state.condition is Condition.BLOCKED
    assert state.blocked_reason == "iteration aborted: boom"


def test_b2_a_local_delivery_gate_is_overridable_by_the_captain(tmp_path, capsys):
    """The other half of N1: a `local-only` delivery under a contract that does
    not publish is a gate the captain may pass, and the override is recorded."""
    from hoh import cli

    store, _ = _checkpointed_run(tmp_path)
    bind(store.dir, contract=_unattended())
    cli.main(["--root", str(tmp_path / "runs"), "deliver", "r1",
              "--into", "main", "--approve"])
    err = capsys.readouterr().err
    assert "not allowed by this contract at all" not in err
    rows = bind(store.dir).routings()
    assert any(r.get("override_of") for r in rows), rows


def test_b2_a_forbidden_action_class_is_not_overridable_by_approve(tmp_path, capsys):
    """`--approve` is the captain's authority over a gate the contract *has*,
    not permission to leave the envelope it was given. Before this, --approve
    skipped the gate entirely, so the contract could not change any outcome."""
    from hoh import cli

    from hoh.store import RunStore

    store, _ = _checkpointed_run(tmp_path)
    bind(store.dir, contract=_unattended())          # EXTERNAL not allowed at all
    with store.lock():
        state = store.read_state()
        state.delivery_mode = "direct-PR"            # a delivery that leaves the machine
        store.write_state(state)
    del RunStore
    rc = cli.main(["--root", str(tmp_path / "runs"), "deliver", "r1",
                   "--into", "main", "--approve"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "autonomy contract" in err and "not allowed by this contract at all" in err


def test_b2_an_overridable_gate_is_overridden_and_the_override_is_recorded(tmp_path):
    """A human may pass a gate the contract has -- and the log must say so. It
    used to say "parked" while the release happened."""
    from hoh import cli
    from hoh.autonomy import AutonomyContract

    store, _ = _checkpointed_run(tmp_path)
    contract = AutonomyContract(
        contract_id="rel", mode=AutonomyMode.PLAN_AUTONOMOUS,
        needs_you=NeedsYou.DIGEST_ONLY,
        release_policy=ReleasePolicy.HUMAN_APPROVAL,
        allowed_action_classes=(ActionClass.INTERNAL, ActionClass.EXTERNAL),
        escalation={"decision": Escalation.AUTO_POLICY,
                    ActionClass.EXTERNAL.value: Escalation.HUMAN_IF_AVAILABLE,
                    "release": Escalation.HUMAN_IF_AVAILABLE})
    bind(store.dir, contract=contract)
    cli.main(["--root", str(tmp_path / "runs"), "deliver", "r1",
              "--into", "main", "--approve"])
    rows = bind(store.dir).routings()
    assert any(r.get("override_of") for r in rows), rows
    override = [r for r in rows if r.get("override_of")][-1]
    assert override["by"] == "--approve" and override["kind"] == "release"


def test_b3_a_quota_wait_keeps_its_classification_under_every_contract(tmp_path):
    """`hoh resume-quota` resumes only `usage_limit`. Overwriting the cause made
    an unattended run -- the one mode that must not need a person -- the only one
    that did, while a supervised run resumed by itself. Exactly backwards."""
    for contract in (_unattended(), _supervised()):
        run_dir = Path(tmp_path) / f"q-{contract.contract_id}"
        run_dir.mkdir()
        governor = bind(run_dir, contract=contract)
        state = _state()
        block_under_contract(state, "usage limit reached; resets at 10:00", governor)
        assert state.blocked_kind == "usage_limit", contract.mode
        assert state.retry_after
        assert any("autonomy:" in n for n in state.history)


def test_b4_a_parked_branch_arms_a_timer_something_can_pick_up(tmp_path):
    """Writing `retry_after` alone silenced the never-stuck detector for a
    promise no component kept. The branch now surfaces in `hoh timers`."""
    from hoh.timers import TimerStore

    run_dir = Path(tmp_path) / "runs" / "r1"
    run_dir.mkdir(parents=True)
    governor = bind(run_dir, contract=_unattended())
    state = _state()
    block_under_contract(state, "the developer needs folder trust", governor)
    timers = TimerStore(run_dir.parent).read()
    assert len(timers) == 1
    assert timers[0].run_id == "d7" and timers[0].due_at == state.retry_after
    assert "parked by the autonomy contract" in timers[0].reason


def test_b3_no_contract_leaves_a_stuck_state_the_detector_would_report(tmp_path):
    """The defect a reviewer found inside a passing test: a profile with no
    review interval parked a branch with no retry time and no human owner."""
    from hoh.sentinel import stuck_run

    for mode in AutonomyMode:
        run_dir = Path(tmp_path) / f"s-{mode.value.lower()}"
        run_dir.mkdir()
        contract = (_unattended() if mode is AutonomyMode.UNATTENDED_LONG_HORIZON
                    else compile_profile(mode, contract_id=mode.value.lower()))
        governor = bind(run_dir, contract=contract)
        state = _state()
        block_under_contract(state, "something nobody decided", governor)
        assert stuck_run(state) == [], (mode, state.blocked_kind, state.retry_after)


def test_b5_a_hand_edited_pin_is_refused_by_the_digest_recorded_beside_it(tmp_path):
    """A coherent edit widening the contract was adopted silently; only
    incoherent ones were caught. The digest lives outside the file it protects."""
    import json as _json

    run_dir = Path(tmp_path) / "runs" / "r1"
    (run_dir / "approval").mkdir(parents=True)
    governor = bind(run_dir, contract=_unattended())
    (run_dir / "approval" / "autonomy.json").write_text(
        _json.dumps(governor.evidence()), encoding="utf-8")

    wider = compile_profile(AutonomyMode.PLAN_AUTONOMOUS, contract_id="un")
    pinned_path(run_dir).write_text(wider.model_dump_json(), encoding="utf-8")
    with pytest.raises(ContractRefused) as caught:
        bind(run_dir)
    assert "has been changed since the run started" in str(caught.value)


def test_b5_a_governed_run_does_not_become_ungoverned_by_deleting_the_pin(tmp_path):
    import json as _json

    run_dir = Path(tmp_path) / "runs" / "r1"
    (run_dir / "approval").mkdir(parents=True)
    governor = bind(run_dir, contract=_unattended())
    (run_dir / "approval" / "autonomy.json").write_text(
        _json.dumps(governor.evidence()), encoding="utf-8")
    pinned_path(run_dir).rename(pinned_path(run_dir).with_suffix(".json.moved-aside"))
    with pytest.raises(ContractRefused) as caught:
        bind(run_dir)
    assert "does not quietly become an ungoverned one" in str(caught.value)


def test_b5_a_symlinked_pin_is_refused(tmp_path):
    run_dir = Path(tmp_path) / "runs" / "r1"
    run_dir.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(_unattended().model_dump_json(), encoding="utf-8")
    pinned_path(run_dir).parent.mkdir(parents=True, exist_ok=True)
    pinned_path(run_dir).symlink_to(elsewhere)
    with pytest.raises(ContractRefused) as caught:
        bind(run_dir)
    assert "is a link" in str(caught.value)


def test_the_controller_blocks_under_the_contract_too(tmp_path):
    """Reviewer B counted five block sites in `controller.py` that bypassed the
    contract entirely -- a capability violation, a shrunken amendment chain, a
    spec drift, an exhausted budget, a broken candidate binding. Those are the
    blocks a long unattended run actually hits, so "the run obeys its contract"
    was true only of the two sites in the CLI.

    This drives the real controller: its spec file changes underneath a running
    run, which is one of the five.
    """
    from hoh.contracts import Budgets, Stage
    from hoh.controller import Controller
    from hoh.store import RunStore

    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    spec = tmp_path / "spec.md"
    spec.write_text("# spec\n\nK1: it works\n", encoding="utf-8")
    store = RunStore(tmp_path / "runs", "r1")
    state = Controller.new_state(run_id="r1", repo_path=repo, project_name="p",
                                 spec_path=spec, budgets=Budgets(max_iterations=2))
    store.write_state(state)
    bind(store.dir, contract=_unattended())

    spec.write_text("# spec\n\nK1: it works differently now\n", encoding="utf-8")

    class _Dispatcher:
        def dispatch(self, *a, **kw):            # pragma: no cover - never reached
            raise AssertionError("the spec drift must block before any dispatch")

    controller = Controller(store, _Dispatcher(), spec_path=spec)
    outcome = controller.run_iteration(store.read_state())
    assert not outcome.accepted

    after = store.read_state()
    assert after.stage is not Stage.CHECKPOINTED
    assert "specification has changed" in (after.blocked_reason or "")
    # The contract decided what that block means, and said so.
    assert any("autonomy:" in note for note in after.history), after.history[-3:]
    assert after.blocked_kind == "parked_by_contract"
    assert after.retry_after, "an unattended contract parks with a time to look again"
    rows = bind(store.dir).routings()
    assert rows and rows[-1]["kind"] == "decision"
    assert "specification has changed" in rows[-1]["decision"]


def test_n3_no_escalation_value_leaves_a_state_the_detector_calls_a_defect():
    """The profile sweep missed ABORT_BRANCH, because no profile routes to it --
    and a reviewer found that an abandoned branch sat BLOCKED with no retry time
    and no owner. Swept over the enum now, not over the four profiles."""
    import tempfile

    from hoh.autonomy import Escalation
    from hoh.sentinel import stuck_run

    for strategy in Escalation:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "r"
            run_dir.mkdir()
            # Built directly, not compiled: the narrowing rule refuses some of
            # these as widenings of the profile, which is correct there and
            # beside the point here.
            contract = AutonomyContract(
                contract_id="sweep", mode=AutonomyMode.UNATTENDED_LONG_HORIZON,
                needs_you=NeedsYou.OFF, escalation={"decision": strategy},
                note="stop the whole programme is intended here", **UNATTENDED)
            governor = bind(run_dir, contract=contract)
            state = _state()
            block_under_contract(state, "something nobody decided", governor)
            assert stuck_run(state) == [], (strategy, state.blocked_kind,
                                            state.retry_after, state.condition)


def test_n5_a_branch_whose_timer_cannot_be_armed_keeps_a_human_owner(tmp_path,
                                                                    monkeypatch):
    """`retry_after` is what silences the never-stuck detector. Leaving it
    standing after the timer failed would restore exactly the shape arming the
    timer removed."""
    from hoh import governance
    from hoh.sentinel import stuck_run

    governor = bind(tmp_path, contract=_unattended())

    def _explode(*a, **kw):
        raise OSError("no room on the device")

    monkeypatch.setattr(governance, "_arm_review_timer", lambda g, s, d, r: (
        s.__setattr__("retry_after", None),
        s.note("autonomy: the review timer could not be armed (test), so this "
               "branch keeps a human owner instead")))
    state = _state()
    block_under_contract(state, "the developer needs folder trust", governor)
    assert state.retry_after is None
    assert state.blocked_kind == "human", "the third way out, since there is no retry"
    assert stuck_run(state) == []


def test_n2_the_witness_cannot_be_switched_off_by_damaging_it(tmp_path):
    """Deleting the pin was refused *because of* the witness; damaging the
    witness disabled the check on the pin. The asymmetry was the tell."""
    import json as _json

    run_dir = Path(tmp_path) / "runs" / "r1"
    (run_dir / "approval").mkdir(parents=True)
    governor = bind(run_dir, contract=_unattended())
    (run_dir / "approval" / "autonomy.json").write_text(
        _json.dumps(governor.evidence()), encoding="utf-8")

    (run_dir / "approval" / "autonomy.json").write_text("{ broken", encoding="utf-8")
    with pytest.raises(ContractRefused) as caught:
        bind(run_dir)
    assert "cannot be read" in str(caught.value)


def test_n2_the_routing_log_is_a_second_witness_when_the_first_is_gone(tmp_path):
    """An append-only record of the digest each decision was made under, which
    answers "was this contract swapped" even with the witness file removed."""
    run_dir = Path(tmp_path) / "runs" / "r1"
    run_dir.mkdir(parents=True)
    governor = bind(run_dir, contract=_unattended())
    block_under_contract(_state(), "trust dialog", governor)     # writes a routing
    # The witness `bind` wrote is removed, so only the append-only routing log
    # can answer which contract this run began under.
    (run_dir / "approval" / "autonomy.json").unlink()

    wider = compile_profile(AutonomyMode.PLAN_AUTONOMOUS, contract_id="un")
    pinned_path(run_dir).write_text(wider.model_dump_json(), encoding="utf-8")
    with pytest.raises(ContractRefused) as caught:
        bind(run_dir)
    assert "first recorded decision was made under" in str(caught.value)


def test_r1_a_pin_with_neither_record_beside_it_is_refused(tmp_path):
    """The last hole in the tamper guard: with the witness gone *and no routing
    logged yet*, a hand-edited pin was adopted -- and a freshly pinned run has
    no routing. `bind` writes the witness itself now, so "pin present, both
    records absent" can only mean somebody edited the tree."""
    run_dir = Path(tmp_path) / "runs" / "r1"
    run_dir.mkdir(parents=True)
    bind(run_dir, contract=_unattended())
    assert (run_dir / "approval" / "autonomy.json").is_file(), (
        "bind writes the witness, rather than trusting a caller to do it later")

    wider = compile_profile(AutonomyMode.PLAN_AUTONOMOUS, contract_id="un")
    pinned_path(run_dir).write_text(wider.model_dump_json(), encoding="utf-8")
    (run_dir / "approval" / "autonomy.json").unlink()
    with pytest.raises(ContractRefused) as caught:
        bind(run_dir)
    assert "neither" in str(caught.value) and "somebody edited" in str(caught.value)


def test_r2_clearing_a_parked_branch_by_hand_is_recorded_in_the_contract_log(tmp_path):
    """`hoh unblock` cleared a branch the contract had parked with no entry in
    the log that exists so a weeks-long run stays reviewable. The delivery
    override was recorded; this one was not."""
    from hoh import cli
    from hoh.store import RunStore

    store, _ = _checkpointed_run(tmp_path)
    governor = bind(store.dir, contract=_unattended())
    with store.lock():
        state = store.read_state()
        block_under_contract(state, "the developer needs folder trust", governor)
        store.write_state(state)
    assert store.read_state().blocked_kind == "parked_by_contract"

    rc = cli.main(["--root", str(tmp_path / "runs"), "unblock", "r1",
                   "--reason", "the operator granted the trust dialog"])
    assert rc == 0, "the unblock itself still works"
    rows = bind(store.dir).routings()
    override = [r for r in rows if r.get("by") == "hoh unblock"]
    assert override, rows
    assert "parked_by_contract" in override[-1]["routing_why"]
    assert override[-1]["decision"] == "the operator granted the trust dialog"
    del RunStore
