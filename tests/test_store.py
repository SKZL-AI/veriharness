"""H1/H3: persistence. The lock, atomicity, immutability, no deletion."""

from __future__ import annotations

import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from hoh.contracts import Candidate, RunState, Stage
from hoh.evidence import EvidenceBundle, EvidenceItem, EvidenceStatus
from hoh.store import (
    ImmutableViolation,
    LockBusy,
    RunStore,
    StaleWrite,
    StoreError,
    list_runs,
)


def make_state(**kw) -> RunState:
    base = dict(
        run_id="r1",
        repo_path="/tmp/repo",
        project_name="demo",
        spec_path="/tmp/spec.md",
        spec_digest="s",
        policy_digest="p",
        profile_digest="h",
    )
    return RunState(**{**base, **kw})


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    s = RunStore(tmp_path / "runs", "r1")
    s.ensure()
    return s


# --- Roundtrip ------------------------------------------------------------- #


def test_state_roundtrip(store: RunStore):
    s = make_state(stage=Stage.DEVELOPING, iteration=3)
    store.write_state(s)
    back = store.read_state()
    assert back.stage is Stage.DEVELOPING
    assert back.iteration == 3


def test_a_missing_state_says_so(store: RunStore):
    with pytest.raises(StoreError, match="no state for run"):
        store.read_state()


def test_a_corrupt_state_blocks_visibly(store: RunStore):
    """Handoff §7: a damaged state blocks progress visibly."""
    store.write_state(make_state())
    store.state_path.write_text("{ broken", encoding="utf-8")
    with pytest.raises(StoreError, match="damaged or schema-foreign"):
        store.read_state()


def test_a_schema_foreign_state_is_rejected(store: RunStore):
    store.write_state(make_state())
    store.state_path.write_text(json.dumps({"run_id": "r1", "unknown": 1}), encoding="utf-8")
    with pytest.raises(StoreError):
        store.read_state()


# --- No deletion: versioned parking places -------------------------------- #


def test_an_old_state_is_parked_not_deleted(store: RunStore):
    # One state object that gets advanced -- that is how it really runs. Three
    # fresh objects with write_seq=0 would be exactly the stale writer that the
    # fencing has rejected since the review.
    state = make_state(iteration=1)
    store.write_state(state)
    state.iteration = 2
    store.write_state(state)
    state.iteration = 3
    store.write_state(state)

    parked = store.parked_states()
    assert len(parked) == 2, "every replaced state is preserved"
    assert store.read_state().iteration == 3

    versions = sorted(
        json.loads(p.read_text(encoding="utf-8"))["iteration"] for p in parked
    )
    assert versions == [1, 2]


def test_evidence_is_parked_as_well(store: RunStore):
    b1 = EvidenceBundle(run_id="r1", iteration=1)
    store.write_evidence(b1)
    store.write_evidence(EvidenceBundle(run_id="r1", iteration=2))
    assert list(store.dir.glob("evidence.json.v*")), "a replaced bundle is preserved"
    assert store.read_evidence().iteration == 2


def test_a_stale_writer_is_rejected(store: RunStore):
    """A07: the lock serializes, but it recognizes no stale state.

    An adversarial review reproduced how a controller holding an old RunState
    overwrites an accepted checkpoint. The fencing token prevents it.
    """
    current = make_state(iteration=1)
    store.write_state(current)
    store.write_state(current)          # the sequence grows with it

    stale = make_state(iteration=0)     # a controller with an old state
    with pytest.raises(StaleWrite, match="Stale writer"):
        store.write_state(stale)

    assert store.read_state().iteration == 1, "the current state stays put"


def test_another_runs_state_is_not_overwritten(store: RunStore):
    store.write_state(make_state(run_id="r1"))
    foreign = make_state(run_id="r1")
    foreign.run_id = "a-different-one"
    with pytest.raises(StaleWrite, match="belongs to run"):
        store.write_state(foreign)


# --- Immutable artifacts --------------------------------------------------- #


def test_a_result_is_never_overwritten(store: RunStore):
    c = Candidate(
        candidate_id="c1", repo_path="/tmp/repo", commit="a" * 40,
        tree_clean=True, tree_digest="d",
    )
    store.write_result("res-1", c)
    with pytest.raises(ImmutableViolation):
        store.write_result("res-1", c)


def test_a_receipt_is_never_overwritten(store: RunStore):
    c = Candidate(
        candidate_id="c1", repo_path="/tmp/repo", commit="a" * 40,
        tree_clean=True, tree_digest="d",
    )
    store.write_receipt("rc-1", c)
    with pytest.raises(ImmutableViolation):
        store.write_receipt("rc-1", c)


def test_unsafe_file_names_are_rejected(store: RunStore):
    c = Candidate(
        candidate_id="c1", repo_path="/tmp/repo", commit="a" * 40,
        tree_clean=True, tree_digest="d",
    )
    with pytest.raises(StoreError, match="unsafe file name"):
        store.write_result("../escape", c)


def test_the_run_id_is_validated(tmp_path: Path):
    with pytest.raises(StoreError, match="unsafe file name"):
        RunStore(tmp_path, "../../etc")


# --- The lock: exactly one controller (A07) -------------------------------- #


def test_a_second_controller_is_rejected(store: RunStore):
    with store.lock():
        second = RunStore(store.root, "r1")
        with pytest.raises(LockBusy, match="another controller"):
            with second.lock():
                pass


def test_the_lock_is_released(store: RunStore):
    with store.lock():
        pass
    with store.lock():
        pass  # a second acquisition has to work


def _hold_lock(root: str, run_id: str, seconds: float, flag) -> None:
    s = RunStore(root, run_id)
    with s.lock():
        flag.value = 1
        time.sleep(seconds)


def test_the_lock_holds_across_processes(tmp_path: Path):
    """An old controller in another process must not write alongside."""
    root = str(tmp_path / "runs")
    RunStore(root, "r1").ensure()
    flag = mp.Value("i", 0)
    p = mp.Process(target=_hold_lock, args=(root, "r1", 1.5, flag))
    p.start()
    try:
        for _ in range(50):
            if flag.value:
                break
            time.sleep(0.05)
        assert flag.value, "the helper process did not acquire the lock"
        with pytest.raises(LockBusy):
            with RunStore(root, "r1").lock():
                pass
    finally:
        p.join(timeout=5)


# --- Pointer discipline ---------------------------------------------------- #


def test_evidence_is_validated_before_being_pointed_at(store: RunStore):
    bundle = EvidenceBundle(
        run_id="r1",
        iteration=1,
        items=[
            EvidenceItem(
                item_id="E-1",
                status=EvidenceStatus.VERIFIED,
                claim="runs",
                first_seen_iteration=1,
                last_seen_iteration=1,
            )
        ],
    )
    dig = store.write_evidence(bundle)
    assert dig, "the digest is returned so the state can point at it"
    assert store.read_evidence().items[0].item_id == "E-1"


def test_empty_evidence_is_not_an_error(store: RunStore):
    assert store.read_evidence().items == []


def test_list_runs(tmp_path: Path):
    root = tmp_path / "runs"
    assert list_runs(root) == []
    a = RunStore(root, "run-a")
    a.write_state(make_state(run_id="run-a"))
    b = RunStore(root, "run-b")
    b.write_state(make_state(run_id="run-b"))
    assert list_runs(root) == ["run-a", "run-b"]


def test_disk_usage_measures_the_arenas_where_they_really_are(tmp_path: Path):
    """K1 had no regression test at all -- reverting the fix left 309 green.

    `usage_bytes()["arena"]` used to measure `runs/<id>/arena`, while the
    arenas have sat **next to** the run directory since they were moved out of
    it. The report therefore always printed 0 for them. The existing test
    checked only the key set and `states > 0`, never `arena` -- so the fix
    with the loudest claim in the run was the one nobody watched. Found by an
    adversarial reviewer at the step 0 gate.
    """
    store = RunStore(tmp_path / "runs", "r1")
    store.write_state(make_state())

    assert store.usage_bytes()["arena"] == 0, "no arena yet, so zero is correct"

    arena = store.arenas_dir / "deadbeef"
    arena.mkdir(parents=True)
    (arena / "payload.bin").write_bytes(b"x" * 4096)

    measured = store.usage_bytes()["arena"]
    assert measured >= 4096, (
        f"the arenas hold 4096 bytes, the report says {measured} -- it is "
        "measuring the wrong path again"
    )
    # And the old, wrong location stays empty, so this test really does pin
    # the new one rather than passing by accident.
    assert not (store.dir / "arena").exists()


def test_a_parked_state_can_be_read_back(store: RunStore):
    """A system that promises nothing disappears must offer the way back.

    `parked_states()` handed back paths, and the migration sat exclusively in
    a private reader bound to `state_path`. Anyone wanting an earlier state
    had to reimplement `_migrate` -- and on this machine 23 of 119 parked
    files need it. Found by an adversarial reviewer at the step 0 gate: no
    data lost, but an undocumented way back, which for a run record is nearly
    as bad.
    """
    state = make_state(iteration=1)
    store.write_state(state)
    state.iteration = 2
    store.write_state(state)

    parked = store.parked_states()
    assert parked, "there has to be a parked state to read"
    earlier = store.read_parked_state(parked[0])
    assert earlier.iteration == 1
    assert earlier.run_id == state.run_id


def test_a_parked_state_that_needs_migration_reads_too(store: RunStore, tmp_path: Path):
    """The 23 files on disk that only load through `_migrate`."""
    import json

    store.write_state(make_state())
    raw = json.loads(store.state_path.read_text(encoding="utf-8"))
    raw["started_monotonic"] = 12345.0          # a field the schema dropped
    stale = store.dir / "state.json.v2026-01-01T00-00-00Z"
    stale.write_text(json.dumps(raw), encoding="utf-8")

    recovered = store.read_parked_state(stale)
    assert recovered.run_id == "r1"
