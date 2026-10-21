"""P3-WAKE slice 2: a master that took its own lapsed lease again (`Reason.RECOVERY`)
wakes the predecessor of the hand-over it began with (`hoh.wake`).

The successions are the real ones of `test_wake.py` (`Program`: `freeze_capsule`,
`Successor.acknowledge`, `handoff`, `Successor.take`); the re-arms are real acquires
by a handle on a clock a year later than the lease they find lapsed, so the epoch
chain, the lease and the token are the ones the product writes. `FakeControl`
stands for Herdr behind `AgentControlAdapter`; nothing starts Herdr, an agent or a
subprocess, reads `~/.agents`, or writes outside `tmp_path`.
"""
from __future__ import annotations

import json
import os

import pytest
from test_wake import (
    NONCE, RID, FakeControl, Program, _strings, alarm, resign, run_cli, wake_argv, wk,
)

from hoh.epoch import EpochStore, Reason
from hoh.orchestrator import write_lock
from hoh.succession import MasterAuthority
from hoh.succession_capsule import Capsule


@pytest.fixture
def no_herdr(monkeypatch):
    def boom():
        raise AssertionError("a plan must not reach for Herdr")
    monkeypatch.setattr(wk(), "make_control", boom)


def rearm(prog, session, *, reason=None, note=""):
    """`session` takes the program over a lease that has lapsed: a new epoch and a
    new token, `Reason.RECOVERY` unless `reason` says otherwise. Each call runs
    on a clock a year later than the last, so the previous lease has expired."""
    n = len(EpochStore(prog.dir).chain())
    handle = MasterAuthority(prog.dir, session, clock=lambda: f"{2100 + n:04d}-01-01T00:00:00Z",
                             serialize=lambda: write_lock(prog.store))
    extra = {} if reason is None else {"reason": reason}
    handle.acquire(program_generation=prog.store.read_state().program_generation,
                   note=note, **extra)
    prog.masters[session] = handle
    return handle


@pytest.fixture
def rearmed(tmp_path):
    """A -> B -> C (C holds epoch 3), then C takes its own lapsed lease twice:
    epochs 4 and 5, both RECOVERY, both C's."""
    p = Program(tmp_path).chain()
    assert rearm(p, "orch-c").epoch == 4 and rearm(p, "orch-c").epoch == 5
    return p


def test_a_master_that_re_armed_its_own_lease_wakes_the_predecessor_of_its_hand_over(tmp_path):
    p = Program(tmp_path).chain()
    before = p.plan().as_dict()
    assert before["succession"]["rearmed_epochs"] == []
    for epoch in (4, 5, 6):
        assert rearm(p, "orch-c").epoch == epoch
        plan = p.plan()
        s = plan.succession
        assert (s.predecessor_session, s.predecessor_epoch) == ("orch-b", 2)
        assert (s.successor_session, s.successor_epoch) == ("orch-c", 3)
        assert s.capsule_id == p.capsules[1]
        assert s.rearmed_epochs == tuple(range(4, epoch + 1)) and s.current_epoch == epoch
        now = plan.as_dict()
        assert now["succession"]["rearmed_epochs"] == list(range(4, epoch + 1))
        for key in ("cwd", "label", "agent_args", "kind"):      # what is woken is what it was
            assert now[key] == before[key], key
        assert now["succession"]["receipt_file"] == before["succession"]["receipt_file"]
        assert f"(project p, epoch {epoch})" in plan.prompt


def test_the_command_wakes_the_predecessor_of_a_session_that_re_armed_its_lease(
        rearmed, monkeypatch):
    control = FakeControl()
    monkeypatch.setattr(wk(), "make_control", lambda: control)
    monkeypatch.setattr(wk().time, "sleep", lambda s: None)
    rc, out, err = run_cli(wake_argv(rearmed, "--resume-id", RID, "--brief", rearmed.brief(),
                                     "--execute", "--predecessor-surface-closed", epoch=5))
    assert rc == 0, err
    doc = json.loads(out)
    assert doc["executed"] is True and doc["outcome"] == "WOKEN"
    assert doc["succession"]["predecessor_session"] == "orch-b"
    assert doc["succession"]["rearmed_epochs"] == [4, 5]
    assert control.verbs() == ["open_surface", "start", "prompt"]
    assert control.calls[1][1]["args"] == ("--resume", RID)
    text = control.calls[2][1]["text"]
    assert "master.token" not in text and rearmed.masters["orch-c"].token not in text


def test_the_credentials_of_an_epoch_the_session_re_armed_away_from_wake_nobody(
        tmp_path, no_herdr):
    p = Program(tmp_path).chain()
    old = p.token_file("orch-c", "c3.token")                    # epoch 3's token, kept
    rearm(p, "orch-c")                                          # epoch 4 now
    base = ["--resume-id", RID, "--brief", p.brief()]
    rc, out, err = run_cli(wake_argv(p, *base, epoch=4))
    assert rc == 0, err                                         # the live credentials work
    for argv in (wake_argv(p, *base, epoch=3, token=old),       # its old epoch and token
                 wake_argv(p, *base, epoch=3),                  # its old epoch, the new token
                 wake_argv(p, *base, epoch=4, token=old)):      # the new epoch, the old token
        rc, out, err = run_cli(argv)
        assert (rc, out) == (5, ""), argv
        assert "stale master" in err


def _another_holder_between(p):             # C 3 -> D takes the lapsed lease (4) -> C again (5)
    rearm(p, "orch-d")
    rearm(p, "orch-c")


def _operator_epoch_between(p):             # C 3 -> OPERATOR (4) -> RECOVERY (5)
    rearm(p, "orch-c", reason=Reason.OPERATOR)
    rearm(p, "orch-c")


def _operator_epoch_now(p):                 # C 3 -> RECOVERY (4) -> OPERATOR (5)
    rearm(p, "orch-c")
    rearm(p, "orch-c", reason=Reason.OPERATOR)


def _self_replacement_between(p):           # C 3 -> RECOVERY (4) -> SELF_REPLACEMENT (5) -> RECOVERY (6)
    rearm(p, "orch-c")
    rearm(p, "orch-c", reason=Reason.SELF_REPLACEMENT)
    rearm(p, "orch-c")


def _handed_without_a_rollover(p):          # C hands to D by hand (4, SUCCESSION, no receipt) -> D RECOVERY (5)
    c = p.masters["orch-c"]
    c.handoff("orch-d", expires_in=300, reason="by hand")
    d = p.authority("orch-d")
    d.acquire(program_generation=p.store.read_state().program_generation)
    p.masters["orch-d"] = d
    rearm(p, "orch-d")


@pytest.mark.parametrize("make,session", [
    (_another_holder_between, "orch-c"),
    (_operator_epoch_between, "orch-c"),
    (_operator_epoch_now, "orch-c"),
    (_self_replacement_between, "orch-c"),
    (_handed_without_a_rollover, "orch-d"),
], ids=["another-holder", "operator-epoch-between", "operator-epoch-now",
        "self-replacement-between", "handed-over-by-hand"])
def test_only_an_unbroken_run_of_its_own_re_arms_leads_back_to_a_hand_over(
        tmp_path, no_herdr, make, session):
    p = Program(tmp_path).chain()
    make(p)
    with pytest.raises(wk().WakeRefused, match=f"no receipt names {session!r}"):
        p.plan(session)
    rc, out, err = run_cli(wake_argv(p, "--resume-id", RID, "--brief", p.brief(),
                                     session=session, epoch=p.masters[session].epoch))
    assert (rc, out) == (5, "") and f"no receipt names {session!r}" in err


def test_a_first_master_that_re_armed_its_lease_has_no_predecessor_to_wake(tmp_path):
    p = Program(tmp_path)
    p.first("orch-a")
    p.hand_over("orch-a", "orch-b")                             # receipts exist, none names A
    p.masters["orch-b"].release(reason="test")
    rearm(p, "orch-a")                                          # epoch 3, RECOVERY, A's
    rearm(p, "orch-a")                                          # epoch 4
    with pytest.raises(wk().WakeRefused, match="no receipt names 'orch-a'"):
        p.plan("orch-a")


def test_a_run_of_re_arms_that_reaches_the_first_epoch_is_refused_as_initial(tmp_path):
    """No hand-over anywhere: the run ends at epoch 1, which began as INITIAL."""
    p = Program(tmp_path)
    p.first("orch-a")
    rearm(p, "orch-a")                                          # epoch 2
    rearm(p, "orch-a")                                          # epoch 3
    (p.dir / "rollover").mkdir()                                # the records exist, empty
    with pytest.raises(wk().WakeRefused, match="epoch 1 began as INITIAL"):
        p.plan("orch-a")


def test_a_receipt_that_is_missing_or_ambiguous_leaves_a_re_armed_master_without_a_plan(
        rearmed):
    receipt = rearmed.receipt_file(rearmed.capsules[1])
    data = json.loads(receipt.read_text())
    data["capsule_id"] = "rc-dup"
    (rearmed.dir / "rollover" / "rc-dup.receipt.json").write_text(json.dumps(resign(data)))
    with pytest.raises(wk().WakeRefused, match="ambiguous"):
        rearmed.plan()
    (rearmed.dir / "rollover" / "rc-dup.receipt.json").rename(rearmed.tmp / "dup.moved")
    assert rearmed.plan().succession.predecessor_session == "orch-b"
    receipt.rename(rearmed.tmp / "receipt.moved")               # moved, not removed
    with pytest.raises(wk().WakeRefused, match="no receipt names 'orch-c'"):
        rearmed.plan()


def test_a_receipt_for_an_epoch_that_the_chain_says_was_not_that_rollover_is_refused(
        rearmed):
    """The receipt's token digest cannot be compared after a re-arm; the epoch's own
    note (`rollover <capsule id>`) is what binds the receipt to it. A receipt and a
    capsule that agree with each other but name another capsule id are not that."""
    rdir = rearmed.dir / "rollover"
    cap_file = rdir / f"{rearmed.capsules[1]}.capsule.json"
    cap = json.loads(cap_file.read_text())
    cap["capsule_id"] = "rc-forged"
    cap["capsule_digest"] = Capsule.model_validate(cap).compute_digest()
    (rdir / "rc-forged.capsule.json").write_text(json.dumps(cap))
    data = json.loads(rearmed.receipt_file(rearmed.capsules[1]).read_text())
    data.update(capsule_id="rc-forged", capsule_digest=cap["capsule_digest"])
    (rdir / "rc-forged.receipt.json").write_text(json.dumps(resign(data)))
    rearmed.receipt_file(rearmed.capsules[1]).rename(rearmed.tmp / "receipt.moved")
    with pytest.raises(wk().WakeRefused, match="does not say that rollover began"):
        rearmed.plan()


def test_an_epoch_that_only_carries_the_rollovers_note_is_not_the_rollover(tmp_path):
    """The note is free text: an OPERATOR epoch that carries the note of a real rollover,
    with that rollover's capsule and receipt copied beside it, is not a hand-over."""
    (tmp_path / "real").mkdir()
    (tmp_path / "fake").mkdir()
    real = Program(tmp_path / "real")
    real.first("orch-a")
    real.hand_over("orch-a", "orch-b")
    cap_id = real.capsules[0]
    fake = Program(tmp_path / "fake")
    fake.first("orch-a")
    rearm(fake, "orch-b", reason=Reason.OPERATOR, note=f"rollover {cap_id}")      # epoch 2
    rearm(fake, "orch-b")                                                          # epoch 3
    rdir = fake.dir / "rollover"
    rdir.mkdir()
    for kind in ("capsule", "receipt"):
        name = f"{cap_id}.{kind}.json"
        (rdir / name).write_text((real.dir / "rollover" / name).read_text())
    start = tmp_path / "started-here"
    start.mkdir()
    with pytest.raises(wk().WakeRefused, match="does not say that rollover began"):
        fake.plan("orch-b", cwd=str(start))


def test_a_session_that_does_not_hold_the_epoch_is_not_followed_back_from_it(tmp_path):
    p = Program(tmp_path).chain()
    _another_holder_between(p)                                  # D holds 4, C holds 5
    lease = p.masters["orch-c"].read_lease()
    full = EpochStore(p.dir).chain()
    with pytest.raises(wk().WakeRefused, match="epoch 4 was held by 'orch-d'"):
        wk().read_succession(p.dir, project_id="p", session="orch-c", epoch=4,
                             token_sha256=lease.token_sha256,
                             chain={e.epoch: e.master_id for e in full}, epochs=full)


def test_a_receipt_for_an_epoch_that_another_session_held_is_refused_whatever_it_says(tmp_path):
    """C 3 -> D takes the lapsed lease (4) -> C again (5). A receipt that names C at
    epoch 4 contradicts the chain, which says D held it: the walk stops at D."""
    p = Program(tmp_path).chain()
    _another_holder_between(p)
    data = json.loads(p.receipt_file(p.capsules[1]).read_text())
    data.update(capsule_id="rc-at-four", successor_epoch=4)
    (p.dir / "rollover" / "rc-at-four.receipt.json").write_text(json.dumps(resign(data)))
    with pytest.raises(wk().WakeRefused, match="epoch 4 is held by 'orch-d'"):
        p.plan()


@pytest.mark.parametrize("label,change,sign,message", [
    ("changed-after-signing", lambda d: d.update(predecessor_session="orch-x"), False, "digest"),
    ("epoch-chain-disagrees", lambda d: d.update(predecessor_epoch=1), True, "epoch chain"),
    ("capsule-digest-differs", lambda d: d.update(capsule_digest="f" * 64), True, "capsule"),
    ("session-is-the-caller", lambda d: d.update(predecessor_session="orch-c"), True,
     "own predecessor"),
    ("session-with-newline", lambda d: d.update(predecessor_session="orch-b\nevil"), True,
     "plain id"),
    ("session-with-escape", lambda d: d.update(predecessor_session="orch-b\x1b[2J"), True,
     "plain id"),
])
def test_the_checks_of_a_receipt_stand_after_a_re_arm_too(rearmed, label, change, sign, message):
    path = rearmed.receipt_file(rearmed.capsules[1])
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(resign(data) if sign else data, indent=2, sort_keys=True))
    with pytest.raises(wk().WakeRefused, match=message):
        rearmed.plan()


def test_a_capsule_that_disagrees_with_its_receipt_is_refused_after_a_re_arm_too(rearmed):
    """Both files re-signed, so each digest holds: only the comparison of the fields can
    tell that the capsule is another project's."""
    cap_file = rearmed.dir / "rollover" / f"{rearmed.capsules[1]}.capsule.json"
    cap = json.loads(cap_file.read_text())
    cap["project_id"] = "other"
    cap["capsule_digest"] = Capsule.model_validate(cap).compute_digest()
    cap_file.write_text(json.dumps(cap))
    receipt = rearmed.receipt_file(rearmed.capsules[1])
    data = json.loads(receipt.read_text())
    data["capsule_digest"] = cap["capsule_digest"]
    receipt.write_text(json.dumps(resign(data)))
    with pytest.raises(wk().WakeRefused, match="disagree about the project"):
        rearmed.plan()


def test_two_receipts_for_the_predecessors_own_epoch_are_ambiguous_after_a_re_arm_too(rearmed):
    data = json.loads(rearmed.receipt_file(rearmed.capsules[0]).read_text())
    data["capsule_id"] = "rc-dup-earlier"
    (rearmed.dir / "rollover" / "rc-dup-earlier.receipt.json").write_text(
        json.dumps(resign(data)))
    with pytest.raises(wk().WakeRefused, match="ambiguous"):
        rearmed.plan()


@pytest.mark.parametrize("how", ["released", "handed-over", "expired"])
def test_a_re_armed_master_whose_lease_is_not_live_gets_no_plan(rearmed, no_herdr, how):
    c = rearmed.masters["orch-c"]
    if how == "released":
        c.release(reason="done")
    elif how == "handed-over":
        c.handoff("orch-d", expires_in=300, reason="test")
    when = {"clock": lambda: "2999-01-01T00:00:00Z"} if how == "expired" else {}
    authority = MasterAuthority(rearmed.dir, "orch-c", serialize=lambda: write_lock(rearmed.store),
                                **when)
    authority.present(epoch=5, token=c.token)
    with pytest.raises(wk().WakeRefused, match="not the master"):
        wk().plan_wake(authority, rearmed.dir, project_id="p", resume_id=RID,
                       brief=rearmed.brief(), answer=rearmed.answer(), nonce=NONCE)


def test_without_the_epoch_chain_only_a_receipt_of_the_epoch_itself_is_read(rearmed):
    lease = rearmed.masters["orch-c"].read_lease()
    chain = {e.epoch: e.master_id for e in EpochStore(rearmed.dir).chain()}
    common = dict(project_id="p", session="orch-c", epoch=5,
                  token_sha256=lease.token_sha256, chain=chain)
    with pytest.raises(wk().WakeRefused, match="no receipt names 'orch-c'"):
        wk().read_succession(rearmed.dir, **common)
    s = wk().read_succession(rearmed.dir, **common, epochs=EpochStore(rearmed.dir).chain())
    assert (s.predecessor_session, s.successor_epoch, s.rearmed_epochs) == (
        "orch-b", 3, (4, 5))


def test_an_epoch_that_is_missing_from_or_twice_in_the_list_is_refused(rearmed):
    lease = rearmed.masters["orch-c"].read_lease()
    full = EpochStore(rearmed.dir).chain()
    chain = {e.epoch: e.master_id for e in full}
    common = dict(project_id="p", session="orch-c", epoch=5,
                  token_sha256=lease.token_sha256, chain=chain)
    gap = [e for e in full if e.epoch != 4]
    with pytest.raises(wk().WakeRefused, match="epoch 4 is not in the epoch chain"):
        wk().read_succession(rearmed.dir, **common, epochs=gap)
    twice = [*full, full[-1]]
    with pytest.raises(wk().WakeRefused, match="epoch 5 twice"):
        wk().read_succession(rearmed.dir, **common, epochs=twice)
    nothing_at_the_top = [e for e in full if e.epoch != 5]
    with pytest.raises(wk().WakeRefused, match="epoch 5 is not in the epoch chain"):
        wk().read_succession(rearmed.dir, **common, epochs=nothing_at_the_top)


@pytest.mark.parametrize("who", ["ORCH-C", "orch-c ", "orch-c\n", "orch-"])
def test_a_session_name_is_matched_exactly_when_the_run_is_followed(rearmed, who):
    lease = rearmed.masters["orch-c"].read_lease()
    full = EpochStore(rearmed.dir).chain()
    with pytest.raises(wk().WakeRefused, match="no receipt names"):
        wk().read_succession(rearmed.dir, project_id="p", session=who, epoch=5,
                             token_sha256=lease.token_sha256,
                             chain={e.epoch: e.master_id for e in full}, epochs=full)


def test_an_odd_record_among_the_receipts_refuses_a_re_armed_master_too(rearmed):
    os.mkfifo(rearmed.dir / "rollover" / "rc-zz.receipt.json")
    with alarm(), pytest.raises(wk().WakeRefused, match="rc-zz.receipt.json"):
        rearmed.plan()


def test_the_plan_of_a_re_armed_master_names_no_credential_and_no_path_to_one(rearmed):
    plan = rearmed.plan()
    rollover = os.path.realpath(rearmed.dir / "rollover")
    assert plan.cwd == f"{rollover}/{rearmed.capsules[0]}.cwd"   # the predecessor's own
    everything = "\n".join(_strings(plan.as_dict()))
    assert "master.token" not in everything and f"{rearmed.capsules[1]}.cwd" not in everything
    assert rearmed.masters["orch-c"].token not in everything
