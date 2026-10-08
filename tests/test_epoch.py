"""The master epoch: who holds authority, and does a replaced master know it?

V3.3 P2-08. The cases follow the rules of the spec one by one, and each rule
has a negative control that changes only the condition under test and shows
the verdict flipping -- a refusal that also fires on the control is a refusal
of everything, which proves nothing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from hoh.epoch import (
    FILENAME,
    EpochError,
    EpochStore,
    MasterEpoch,
    Reason,
    authority_problems,
    epoch_digest,
    may_replace_self,
)

T0 = "2026-09-24T00:00:00Z"
T1 = "2026-09-24T01:00:00Z"
T2 = "2026-09-24T02:00:00Z"
T3 = "2026-09-24T03:00:00Z"

MODULE = Path(__file__).resolve().parent.parent / "src" / "hoh" / "epoch.py"


def _store(tmp_path) -> EpochStore:
    return EpochStore(tmp_path / "program")


def _two_epochs(tmp_path) -> EpochStore:
    """Epoch 1 held by 'a', epoch 2 held by 'b' after a succession."""
    store = _store(tmp_path)
    store.begin("a", reason=Reason.INITIAL, now=T0)
    store.begin("b", reason=Reason.SUCCESSION, now=T1)
    return store


def _write_raw(store: EpochStore, records: list[dict]) -> None:
    """Writes a chain directly, bypassing `begin`, so a broken one can exist."""
    store.root.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"schema_version": "hoh-epoch/1", "epochs": records}),
                          encoding="utf-8")


def _rec(epoch: int, master_id: str, reason: Reason, previous: str, note: str = "") -> dict:
    return {"epoch": epoch, "master_id": master_id, "reason": reason.value, "began_at": T0,
            "previous_digest": previous, "note": note,
            "epoch_digest": epoch_digest(epoch=epoch, master_id=master_id, reason=reason,
                                         previous_digest=previous, note=note)}


def _real_chain_records(n: int) -> list[dict]:
    recs = [_rec(1, "m1", Reason.INITIAL, "")]
    for i in range(2, n + 1):
        recs.append(_rec(i, f"m{i}", Reason.SUCCESSION, recs[-1]["epoch_digest"]))
    return recs


# --- Reason ------------------------------------------------------------------

def test_reasons_are_exactly_the_five_in_order():
    """Catches a reason added, dropped or reordered -- the order is part of the
    contract, and SELF_REPLACEMENT in particular has to exist as a member."""
    assert [r.value for r in Reason] == ["INITIAL", "SUCCESSION", "OPERATOR",
                                         "SELF_REPLACEMENT", "RECOVERY"]


# --- MasterEpoch ---------------------------------------------------------------

def test_master_epoch_refuses_an_empty_master_id_and_accepts_a_named_one():
    """Catches an epoch nobody holds. Control: the same epoch with an id is fine."""
    with pytest.raises(ValidationError):
        MasterEpoch(epoch=1, master_id="", reason=Reason.INITIAL, began_at=T0)
    with pytest.raises(ValidationError):
        MasterEpoch(epoch=1, master_id="   ", reason=Reason.INITIAL, began_at=T0)
    assert MasterEpoch(epoch=1, master_id="a", reason=Reason.INITIAL, began_at=T0).master_id == "a"


def test_master_epoch_refuses_epoch_zero_and_accepts_epoch_one():
    """Catches an epoch numbered below the first. Control: epoch 1 is accepted."""
    with pytest.raises(ValidationError):
        MasterEpoch(epoch=0, master_id="a", reason=Reason.INITIAL, began_at=T0)
    assert MasterEpoch(epoch=1, master_id="a", reason=Reason.INITIAL, began_at=T0).epoch == 1


def test_epoch_one_must_be_initial_without_predecessor():
    """Catches a first epoch that claims a reason or a predecessor it cannot
    have. Control: INITIAL with an empty previous_digest is accepted."""
    with pytest.raises(ValidationError):
        MasterEpoch(epoch=1, master_id="a", reason=Reason.SUCCESSION, began_at=T0)
    with pytest.raises(ValidationError):
        MasterEpoch(epoch=1, master_id="a", reason=Reason.INITIAL, began_at=T0,
                    previous_digest="abc")
    MasterEpoch(epoch=1, master_id="a", reason=Reason.INITIAL, began_at=T0, previous_digest="")


def test_a_later_epoch_must_not_be_initial_and_must_name_its_predecessor():
    """Catches a second 'beginning' in the middle of a chain, and an epoch that
    floats free of its predecessor. Control: SUCCESSION with a digest is fine."""
    with pytest.raises(ValidationError):
        MasterEpoch(epoch=2, master_id="b", reason=Reason.INITIAL, began_at=T0,
                    previous_digest="abc")
    with pytest.raises(ValidationError):
        MasterEpoch(epoch=2, master_id="b", reason=Reason.SUCCESSION, began_at=T0)
    MasterEpoch(epoch=2, master_id="b", reason=Reason.SUCCESSION, began_at=T0,
                previous_digest="abc")


def test_master_epoch_is_frozen():
    """Catches an epoch record that can be edited after the fact."""
    e = MasterEpoch(epoch=1, master_id="a", reason=Reason.INITIAL, began_at=T0)
    with pytest.raises(ValidationError):
        e.master_id = "b"


def test_epoch_digest_is_over_content_not_arrival_time():
    """Catches a digest that changes with when a record was written: the same
    epoch written twice would then look like a fork. Control: a change in the
    content (the holder) does change the digest."""
    a = MasterEpoch(epoch=1, master_id="a", reason=Reason.INITIAL, began_at=T0)
    later = MasterEpoch(epoch=1, master_id="a", reason=Reason.INITIAL, began_at=T3)
    other = MasterEpoch(epoch=1, master_id="z", reason=Reason.INITIAL, began_at=T0)
    assert a.epoch_digest == later.epoch_digest
    assert a.epoch_digest != other.epoch_digest


def test_a_supplied_digest_that_does_not_match_the_content_is_refused():
    """Catches a record whose digest was carried over from different content.
    Control: the matching digest is accepted."""
    good = epoch_digest(epoch=1, master_id="a", reason=Reason.INITIAL, previous_digest="")
    with pytest.raises(ValidationError):
        MasterEpoch(epoch=1, master_id="a", reason=Reason.INITIAL, began_at=T0,
                    epoch_digest="0" * 64)
    assert MasterEpoch(epoch=1, master_id="a", reason=Reason.INITIAL, began_at=T0,
                       epoch_digest=good).epoch_digest == good


# --- authority_problems ----------------------------------------------------------

def test_the_holder_at_the_current_epoch_may_act(tmp_path):
    """The positive case every refusal below is a control against."""
    assert authority_problems(_two_epochs(tmp_path), master_id="b", claimed_epoch=2) == []


def test_superseded_master_one_epoch_behind_is_refused_naming_both_numbers(tmp_path):
    """Catches a replaced master that keeps acting as though it were current --
    the stale write that becomes duplicate accepted work. The message must name
    the claimed and the current epoch. Control: the same master id at the
    current number is accepted, so the refusal is about the number alone."""
    store = _two_epochs(tmp_path)
    problems = authority_problems(store, master_id="b", claimed_epoch=1)
    assert problems and "superseded" in problems[0]
    assert re.search(r"\b1\b", problems[0]) and re.search(r"\b2\b", problems[0]), problems
    assert authority_problems(store, master_id="b", claimed_epoch=2) == []


def test_superseded_original_holder_is_refused_after_succession(tmp_path):
    """Catches the case the exit condition is about: the killed master 'a' comes
    back claiming the epoch it held. Control: before the succession, the very
    same claim was valid."""
    store = _store(tmp_path)
    store.begin("a", reason=Reason.INITIAL, now=T0)
    assert authority_problems(store, master_id="a", claimed_epoch=1) == []
    store.begin("b", reason=Reason.SUCCESSION, now=T1)
    assert authority_problems(store, master_id="a", claimed_epoch=1)


def test_master_from_the_future_one_epoch_ahead_is_refused(tmp_path):
    """Catches treating an epoch that has not begun as 'early' rather than as
    state nobody wrote: absence is not permission. Control: one less is the
    current epoch and is accepted."""
    store = _two_epochs(tmp_path)
    problems = authority_problems(store, master_id="b", claimed_epoch=3)
    assert problems and "has not begun" in problems[0]
    assert "3" in problems[0] and "2" in problems[0]
    assert authority_problems(store, master_id="b", claimed_epoch=2) == []


def test_second_master_at_the_correct_epoch_is_refused(tmp_path):
    """Catches two masters at one epoch -- the condition the lease prevents, with
    this as the second line. Control: the holder's id at the same number passes."""
    store = _two_epochs(tmp_path)
    problems = authority_problems(store, master_id="intruder", claimed_epoch=2)
    assert problems and "not the holder" in problems[0]
    assert authority_problems(store, master_id="b", claimed_epoch=2) == []


@pytest.mark.parametrize("claim", [-1, 0, 1, 2, 99])
def test_an_empty_store_refuses_every_claim(tmp_path, claim):
    """Catches authority before any epoch has begun. Control: after INITIAL,
    the holder's claim to epoch 1 passes."""
    store = _store(tmp_path)
    assert store.current() is None
    assert authority_problems(store, master_id="a", claimed_epoch=claim)
    store.begin("a", reason=Reason.INITIAL, now=T0)
    assert authority_problems(store, master_id="a", claimed_epoch=1) == []


def test_a_broken_chain_gives_nobody_authority(tmp_path):
    """Catches picking a side of a fork silently. Control: the same holder over
    an intact chain may act."""
    store = _store(tmp_path)
    recs = _real_chain_records(2)
    _write_raw(store, recs)
    assert authority_problems(store, master_id="m2", claimed_epoch=2) == []
    fork = _rec(2, "m2x", Reason.OPERATOR, recs[0]["epoch_digest"])
    _write_raw(store, recs + [fork])
    assert authority_problems(store, master_id="m2", claimed_epoch=2)


# --- begin and the chain -----------------------------------------------------------

def test_begin_chains_each_epoch_to_its_predecessor_and_fsck_is_clean(tmp_path):
    """Catches an epoch appended without its link, which would make a fork
    undetectable. fsck over a real chain built by `begin` must be clean."""
    store = _store(tmp_path)
    e1 = store.begin("a", reason=Reason.INITIAL, now=T0)
    e2 = store.begin("b", reason=Reason.SUCCESSION, now=T1)
    e3 = store.begin("c", reason=Reason.RECOVERY, now=T2, note="after reboot")
    assert [e.epoch for e in store.chain()] == [1, 2, 3]
    assert e1.previous_digest == ""
    assert e2.previous_digest == e1.epoch_digest
    assert e3.previous_digest == e2.epoch_digest
    assert store.current() == e3
    assert store.fsck() == []


def test_begin_refuses_initial_after_the_first_epoch(tmp_path):
    """Catches a second 'beginning' that would restart the chain. Control: a
    non-INITIAL reason at the same point is accepted."""
    store = _store(tmp_path)
    store.begin("a", reason=Reason.INITIAL, now=T0)
    with pytest.raises(EpochError):
        store.begin("b", reason=Reason.INITIAL, now=T1)
    assert store.current().epoch == 1, "a refused begin must not write"
    assert store.begin("b", reason=Reason.OPERATOR, now=T1).epoch == 2


def test_begin_refuses_a_first_epoch_that_is_not_initial(tmp_path):
    """Catches a chain that starts in the middle. Control: INITIAL starts it."""
    store = _store(tmp_path)
    with pytest.raises(EpochError):
        store.begin("a", reason=Reason.SUCCESSION, now=T0)
    assert store.current() is None
    assert store.begin("a", reason=Reason.INITIAL, now=T0).epoch == 1


def test_begin_refuses_a_timestamp_that_is_not_utc(tmp_path):
    """Catches a local time stored as though it were UTC. Control: the Z form."""
    store = _store(tmp_path)
    with pytest.raises(EpochError):
        store.begin("a", reason=Reason.INITIAL, now="2026-09-24 00:00:00")
    store.begin("a", reason=Reason.INITIAL, now=T0)


def test_begin_refuses_to_append_to_a_broken_chain(tmp_path):
    """Catches extending a fork, which would bury it under a valid-looking head.
    Control: the same begin over the intact chain succeeds."""
    store = _store(tmp_path)
    recs = _real_chain_records(2)
    fork = _rec(2, "m2x", Reason.OPERATOR, recs[0]["epoch_digest"])
    _write_raw(store, recs + [fork])
    with pytest.raises(EpochError):
        store.begin("m3", reason=Reason.SUCCESSION, now=T1)
    _write_raw(store, recs)
    assert store.begin("m3", reason=Reason.SUCCESSION, now=T1).epoch == 3


def test_begin_is_durable_across_a_fresh_store(tmp_path):
    """Catches an epoch that lives only in the process that began it: the exit
    condition includes a reboot."""
    store = _two_epochs(tmp_path)
    again = EpochStore(tmp_path / "program")
    assert [e.model_dump() for e in again.chain()] == [e.model_dump() for e in store.chain()]
    assert authority_problems(again, master_id="b", claimed_epoch=2) == []


def test_previous_form_is_preserved_when_the_file_is_rewritten(tmp_path):
    """Catches a rewrite that loses the history it replaced. Every earlier form
    of the file is kept byte for byte under a versioned name, and none is
    overwritten. Control: the very first write has nothing to preserve."""
    store = _store(tmp_path)
    store.begin("a", reason=Reason.INITIAL, now=T0)
    assert list(store.root.glob(f"{FILENAME}.v*")) == []
    first_form = store.path.read_bytes()
    store.begin("b", reason=Reason.SUCCESSION, now=T1)
    second_form = store.path.read_bytes()
    store.begin("c", reason=Reason.SUCCESSION, now=T1)   # same stamp on purpose
    versions = sorted(store.root.glob(f"{FILENAME}.v*"))
    assert len(versions) == 2, versions
    assert {v.read_bytes() for v in versions} == {first_form, second_form}
    assert all(re.fullmatch(rf"{re.escape(FILENAME)}\.v\d+\.\d{{8}}T\d{{6}}Z", v.name)
               for v in versions), versions


def test_the_module_contains_no_call_that_removes_a_file():
    """Catches a removal slipping into the store -- the replace-instead-of-delete
    rule, checked on the source as `test_preserve.py` does for its tool."""
    source = MODULE.read_text(encoding="utf-8")
    for forbidden in ("os.remove", "os.unlink", ".unlink(", "shutil.rmtree", "rmtree",
                      "os.rmdir", ".rmdir(", "removedirs"):
        assert forbidden not in source, forbidden
    assert "os.replace" in source, "control: the grep is reading the right module"


# --- fsck ----------------------------------------------------------------------------

def test_fsck_reports_a_gap_and_the_control_clears_it(tmp_path):
    """Catches a missing epoch -- authority that changed hands with no record."""
    store = _store(tmp_path)
    recs = _real_chain_records(3)
    gapped = [recs[0], _rec(3, "m3", Reason.SUCCESSION, recs[1]["epoch_digest"])]
    _write_raw(store, gapped)
    assert any("gap" in p for p in store.fsck()), store.fsck()
    _write_raw(store, recs)
    assert store.fsck() == []


def test_fsck_reports_a_duplicate_epoch_number_and_the_control_clears_it(tmp_path):
    """Catches a fork: two epoch 2s with different holders over the same parent."""
    store = _store(tmp_path)
    recs = _real_chain_records(2)
    _write_raw(store, recs + [_rec(2, "other", Reason.OPERATOR, recs[0]["epoch_digest"])])
    assert any("duplicate epoch number 2" in p for p in store.fsck()), store.fsck()
    with pytest.raises(EpochError):
        store.current()
    _write_raw(store, recs)
    assert store.fsck() == []


def test_fsck_reports_a_broken_chain_link_and_the_control_clears_it(tmp_path):
    """Catches an epoch whose previous_digest is not its recorded predecessor --
    the other shape of a fork. Control: the same epoch with the right link."""
    store = _store(tmp_path)
    recs = _real_chain_records(2)
    _write_raw(store, [recs[0], _rec(2, "m2", Reason.SUCCESSION, "f" * 64)])
    assert any("broken link" in p for p in store.fsck()), store.fsck()
    _write_raw(store, recs)
    assert store.fsck() == []


def test_fsck_reports_a_first_epoch_that_is_not_initial_and_the_control_clears_it(tmp_path):
    """Catches a chain whose beginning claims to be a succession from nothing."""
    store = _store(tmp_path)
    bad = {"epoch": 1, "master_id": "m1", "reason": "SUCCESSION", "began_at": T0,
           "previous_digest": "", "note": "",
           "epoch_digest": epoch_digest(epoch=1, master_id="m1", reason=Reason.SUCCESSION,
                                        previous_digest="")}
    _write_raw(store, [bad])
    assert any("first epoch is not INITIAL" in p for p in store.fsck()), store.fsck()
    _write_raw(store, [_rec(1, "m1", Reason.INITIAL, "")])
    assert store.fsck() == []


def test_fsck_reports_a_tampered_record_and_the_control_clears_it(tmp_path):
    """Catches a holder edited in place after the fact: the digest no longer
    matches the content."""
    store = _store(tmp_path)
    recs = _real_chain_records(2)
    _write_raw(store, [recs[0], {**recs[1], "master_id": "someone-else"}])
    assert any("does not match" in p for p in store.fsck()), store.fsck()
    _write_raw(store, recs)
    assert store.fsck() == []


# --- may_replace_self ----------------------------------------------------------------

def test_replacing_self_within_the_epoch_is_refused(tmp_path):
    """Catches the bootstrap rule's forbidden case: a controller silently
    replacing itself mid-epoch. The message must say a new epoch is needed.
    Control: after a SELF_REPLACEMENT epoch the same kind of call passes."""
    store = _two_epochs(tmp_path)
    problems = may_replace_self(store, master_id="b", candidate_id="b2", claimed_epoch=2)
    assert problems and "new epoch" in problems[0]
    store.begin("b2", reason=Reason.SELF_REPLACEMENT, now=T2)
    assert may_replace_self(store, master_id="b2", candidate_id="b", claimed_epoch=3) == []


def test_replacing_self_with_self_is_refused(tmp_path):
    """Catches a no-op dressed as a transition. Control: a different candidate
    in the same SELF_REPLACEMENT epoch passes."""
    store = _two_epochs(tmp_path)
    store.begin("b2", reason=Reason.SELF_REPLACEMENT, now=T2)
    problems = may_replace_self(store, master_id="b2", candidate_id="b2", claimed_epoch=3)
    assert problems and "no-op" in problems[0]
    assert may_replace_self(store, master_id="b2", candidate_id="b3", claimed_epoch=3) == []


def test_replace_self_inherits_every_authority_refusal(tmp_path):
    """Catches a self-replacement path that skips the authority check: a
    superseded, future, foreign or pre-initial master may not replace anything.
    Control: the holder at the SELF_REPLACEMENT epoch may."""
    empty = EpochStore(tmp_path / "empty")
    assert may_replace_self(empty, master_id="a", candidate_id="a2", claimed_epoch=1)
    store = _two_epochs(tmp_path)
    store.begin("b2", reason=Reason.SELF_REPLACEMENT, now=T2)
    assert may_replace_self(store, master_id="b2", candidate_id="c", claimed_epoch=2)
    assert may_replace_self(store, master_id="b2", candidate_id="c", claimed_epoch=4)
    assert may_replace_self(store, master_id="x", candidate_id="c", claimed_epoch=3)
    assert may_replace_self(store, master_id="b2", candidate_id="c", claimed_epoch=3) == []
