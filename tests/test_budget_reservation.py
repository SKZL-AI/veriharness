"""P3-04, first increment: the shared dispatch budget is held atomically before a
run is started and released after it has been dispatched.

Every test names what it catches. Nothing here starts Herdr or an agent or reads
outside `tmp_path`: the launcher's `_run_cli` is replaced by a recorder, run
states are written by hand the way `tests/test_launcher_verdicts.py` does, and
the one multi-process test starts plain interpreters that import `hoh.reservation`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hoh import durable_io
from hoh.launcher import MAX_STATE_BYTES, Dispatch, HohRunLauncher
from hoh.orchestrator import BudgetExhausted, RunOutcome, RunVerdict
from hoh.project import TaskNode
from hoh.reservation import (
    LEDGER_NAME, LOCK_NAME, LOCK_TIMEOUT_SECONDS, MAX_LEDGER_BYTES, SCHEMA,
    BudgetReservations, HoldStatus, NothingToReserve, ReservationError,
)

SRC = Path(__file__).resolve().parents[1] / "src"


def _store(tmp_path, **kw) -> BudgetReservations:
    return BudgetReservations(tmp_path / "root", **kw)


def _spent(**runs: int):
    return lambda: dict(runs)


def _rows(store: BudgetReservations) -> list[tuple[str, int, str]]:
    return [(r.run_id, r.amount, r.status.value) for r in store.read()]


# ==== reserve: the arithmetic ===================================================

def test_a_hold_with_no_want_takes_everything_that_is_left(tmp_path):
    """Catches: a grant that is a constant or ignores the spend. 9 - 7 spent = 2."""
    store = _store(tmp_path)
    assert store.reserve("n1", ceiling=9, spent=_spent(a=7)).amount == 2
    assert _rows(store) == [("n1", 2, "HELD")]


def test_overlapping_holds_are_granted_no_more_than_the_ceiling_in_total(tmp_path):
    """Catches: every start being handed the same remainder (the defect itself).
    Three holds of 3 fill nine; a fourth finds nothing; the sum never passes 9."""
    store = _store(tmp_path)
    got = [store.reserve(f"n{i}", ceiling=9, spent=_spent(), want=3).amount
           for i in range(3)]
    assert got == [3, 3, 3]
    with pytest.raises(NothingToReserve, match=r"0 spent and 9 held of 9"):
        store.reserve("n3", ceiling=9, spent=_spent(), want=3)
    assert sum(r.amount for r in store.held()) == 9


def test_a_want_larger_than_what_is_left_is_cut_to_what_is_left(tmp_path):
    """Catches: `want` granted whole (the budget overrun) or a refusal while
    something is left. 9 - 7 = 2, want 5 -> 2."""
    store = _store(tmp_path)
    assert store.reserve("n1", ceiling=9, spent=_spent(a=7), want=5).amount == 2


def test_a_want_smaller_than_what_is_left_is_the_grant(tmp_path):
    """Catches: `want` ignored (a hold that takes everything whatever was asked)."""
    store = _store(tmp_path)
    assert store.reserve("n1", ceiling=9, spent=_spent(), want=4).amount == 4
    assert store.reserve("n2", ceiling=9, spent=_spent(), want=4).amount == 4
    assert store.reserve("n3", ceiling=9, spent=_spent(), want=4).amount == 1


def test_a_hold_and_its_own_dispatches_are_one_charge_not_two(tmp_path):
    """Catches: counting `amount` and the run's spend together. n1 holds 5 and
    has spent 3 of them: committed 5, so 9 - 5 = 4 is left. Added up it would be
    9 - 3 - 5 = 1."""
    store = _store(tmp_path)
    store.reserve("n1", ceiling=9, spent=_spent(), want=5)
    assert store.reserve("n2", ceiling=9, spent=_spent(n1=3)).amount == 4


def test_a_run_that_spent_past_its_hold_is_committed_at_what_it_spent(tmp_path):
    """Catches: a headroom that goes negative and *adds* to what is left.
    n1 holds 2 and has spent 5: committed 5, so 4 is left (not 9 - 5 + 3)."""
    store = _store(tmp_path)
    store.reserve("n1", ceiling=9, spent=_spent(), want=2)
    assert store.reserve("n2", ceiling=9, spent=_spent(n1=5)).amount == 4


def test_a_spent_ceiling_is_refused_with_the_numbers(tmp_path):
    """Catches: a grant of zero (`--max-dispatches 0` is a stop, not a budget)."""
    store = _store(tmp_path)
    with pytest.raises(NothingToReserve, match=r"9 spent and 0 held of 9"):
        store.reserve("n1", ceiling=9, spent=_spent(a=9))
    assert store.read() == []


def test_a_second_hold_of_the_same_run_supersedes_the_first(tmp_path):
    """Catches: an interrupted start, made again, stranding what it held the
    first time. The first row is kept and marked; only the second is held."""
    store = _store(tmp_path)
    store.reserve("n1", ceiling=9, spent=_spent(), want=4)
    assert store.reserve("n1", ceiling=9, spent=_spent(), want=3).amount == 3
    assert _rows(store) == [("n1", 4, "SUPERSEDED"), ("n1", 3, "HELD")]
    assert store.reserve("n2", ceiling=9, spent=_spent()).amount == 6


def test_settling_ends_the_hold_and_keeps_the_row(tmp_path):
    """Catches: a hold that is never given back (the budget shrinking for good)
    and a ledger that forgets. Settling twice, or an unknown run, ends nothing."""
    store = _store(tmp_path)
    store.reserve("n1", ceiling=9, spent=_spent())
    assert store.settle("n1", "the dispatch returned") == 1
    assert store.settle("n1", "again") == 0
    assert store.settle("nobody", "never held") == 0
    assert _rows(store) == [("n1", 9, "SETTLED")]
    assert store.reserve("n2", ceiling=9, spent=_spent(n1=4)).amount == 5


def test_settling_where_nothing_was_ever_held_writes_nothing(tmp_path):
    """Catches: a resumed run (it never held anything) creating the ledger or the
    lock file as a side effect of finishing."""
    store = _store(tmp_path)
    assert store.settle("n1", "the dispatch returned") == 0
    assert not (tmp_path / "root").exists()


def test_a_reason_is_kept_to_two_hundred_characters(tmp_path):
    """Catches: an unbounded reason written into the ledger."""
    store = _store(tmp_path)
    store.reserve("n1", ceiling=9, spent=_spent())
    store.settle("n1", "r" * 5000)
    assert len(store.read()[0].reason) == 200


def test_settling_names_the_reason_and_the_time(tmp_path):
    """Catches: a settled row that does not say why or when."""
    store = _store(tmp_path)
    store.reserve("n1", ceiling=9, spent=_spent())
    store.settle("n1", "the run could not be started")
    (row,) = store.read()
    assert row.status is HoldStatus.SETTLED and row.ended_at
    assert row.reason == "the run could not be started"


def test_a_run_own_spend_is_counted_when_it_reserves_again(tmp_path):
    """Catches: a reserving run's own dispatches dropped from the total (it would
    be granted what it has already spent). n1 spent 4 of 9, so 5 is left."""
    assert _store(tmp_path).reserve("n1", ceiling=9, spent=_spent(n1=4)).amount == 5


def test_settling_changes_only_the_open_hold_and_keeps_every_other_row_as_it_was(tmp_path):
    """Catches: a settle that rewrites rows that are already ended (their time and
    reason are the provenance of what happened to them), and a superseded row
    without its end. Rows: n1 SUPERSEDED, n1 SETTLED, n2 HELD, n1 HELD."""
    store = _store(tmp_path)
    store.reserve("n1", ceiling=9, spent=_spent(), want=2)
    store.reserve("n1", ceiling=9, spent=_spent(), want=3)         # supersedes the first
    store.settle("n1", "first dispatch returned")
    store.reserve("n2", ceiling=9, spent=_spent(), want=1)
    store.reserve("n1", ceiling=9, spent=_spent(), want=4)
    before = [r.model_dump() for r in store.read()]
    assert before[0]["status"] == "SUPERSEDED" and before[0]["ended_at"]
    assert "superseded" in before[0]["reason"]
    assert before[1]["status"] == "SETTLED" and before[1]["reason"] == "first dispatch returned"
    assert store.settle("n1", "second dispatch returned") == 1
    after = [r.model_dump() for r in store.read()]
    assert after[:3] == before[:3]                                 # n2's open hold too
    assert after[3]["status"] == "SETTLED" and after[3]["reason"] == "second dispatch returned"


def test_a_row_records_the_ceiling_it_was_granted_under(tmp_path):
    """Catches: the stored ceiling being the grant (the row could not say what
    budget it was taken from)."""
    store = _store(tmp_path)
    hold = store.reserve("n1", ceiling=9, spent=_spent(a=4), want=3)
    assert hold.ceiling == 9 and hold.amount == 3 and store.read()[0].ceiling == 9


def test_two_open_rows_of_one_run_count_at_the_larger_not_the_sum(tmp_path):
    """Catches: a hand-edited ledger with two open holds of one run charged twice
    (or, the other way, the smaller one believed). 9 - max(3, 4) = 5."""
    root = tmp_path / "root"
    root.mkdir()
    _ledger(tmp_path).write_text(_good_ledger(
        _good_row(reservation_id="r-1", run_id="n2", amount=3, ceiling=9),
        _good_row(reservation_id="r-2", run_id="n2", amount=4, ceiling=9)))
    assert BudgetReservations(root).reserve("n1", ceiling=9, spent=_spent()).amount == 5


def test_left_is_what_reserve_would_grant_and_writes_nothing(tmp_path):
    """Catches: an advisory read that counts the asking run's own hold (a start
    interrupted before `hoh start` would be refused for what it holds itself), that
    ignores other holds, or that creates the ledger / lock."""
    store = _store(tmp_path)
    assert store.left("n1", ceiling=9, spent=_spent()) == 9
    assert not (tmp_path / "root").exists()
    store.reserve("n1", ceiling=9, spent=_spent(), want=4)
    store.reserve("n2", ceiling=9, spent=_spent(), want=3)
    before = _ledger(tmp_path).read_bytes()
    assert store.left("n3", ceiling=9, spent=_spent(n1=1)) == 2     # 9 - 1 - (4-1) - 3
    assert store.left("n1", ceiling=9, spent=_spent(n1=1)) == 5     # own hold not counted, own spend is
    assert _ledger(tmp_path).read_bytes() == before


# ==== reserve: atomicity ========================================================

def test_a_second_reserve_waits_for_the_first_to_finish_reading_and_writing(tmp_path):
    """Catches: no lock (or a lock around the write only). The spend callable runs
    under the lock; a second reserve started from inside it must still be waiting
    when it returns, and then see the first hold."""
    store = _store(tmp_path)
    second: dict = {}
    inside_done = threading.Event()

    def second_reserve():
        second["grant"] = store.reserve("n2", ceiling=9, spent=_spent()).amount
        inside_done.set()

    thread = threading.Thread(target=second_reserve, daemon=True)

    def first_spent():
        thread.start()
        time.sleep(0.4)           # long enough for an unlocked second reserve to finish
        second["finished_inside"] = inside_done.is_set()
        return {}

    first = store.reserve("n1", ceiling=9, spent=first_spent, want=6)
    thread.join(10)
    assert not thread.is_alive()
    assert second["finished_inside"] is False
    assert first.amount == 6 and second["grant"] == 3


def test_concurrent_processes_are_granted_no_more_than_the_ceiling(tmp_path):
    """Catches: a race between processes. Eight interpreters start together and
    each wants 2 of a ceiling of 9: five are granted (2+2+2+2+1), three refused,
    and the ledger's open holds add up to exactly 9."""
    root = tmp_path / "root"
    go = tmp_path / "go"
    code = (
        "import os, sys, time\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from hoh.reservation import BudgetReservations, NothingToReserve\n"
        "while not os.path.exists(sys.argv[3]): time.sleep(0.005)\n"
        "try:\n"
        "    print(BudgetReservations(sys.argv[2]).reserve('r' + sys.argv[4], ceiling=9,\n"
        "          spent=lambda: {}, want=2).amount)\n"
        "except NothingToReserve:\n"
        "    print(0)\n"
    )
    procs = [subprocess.Popen([sys.executable, "-I", "-c", code, str(SRC), str(root),
                               str(go), str(i)], stdout=subprocess.PIPE, text=True)
             for i in range(8)]
    go.write_text("go")
    grants = []
    for p in procs:
        out, _ = p.communicate(timeout=60)
        assert p.returncode == 0
        grants.append(int(out.strip()))
    assert sorted(grants) == [0, 0, 0, 1, 2, 2, 2, 2]
    assert sum(r.amount for r in BudgetReservations(root).held()) == 9


def test_the_lock_is_waited_for_and_the_wait_has_an_end(tmp_path):
    """Catches: a non-blocking lock that refuses at once (the wait removed) and a
    blocking one that waits for ever (the bound removed). Another descriptor
    holds the lock: with a 0.3 s timeout the reserve is refused within a few
    seconds; released after 0.3 s under a 10 s timeout it succeeds."""
    import fcntl

    root = tmp_path / "root"
    root.mkdir()
    holder = os.open(root / LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        result: dict = {}

        def attempt(key, timeout):
            try:
                BudgetReservations(root, lock_timeout=timeout).reserve(
                    "n1", ceiling=9, spent=_spent())
                result[key] = "granted"
            except ReservationError as exc:
                result[key] = str(exc)

        t0 = time.monotonic()
        short = threading.Thread(target=attempt, args=("short", 0.3), daemon=True)
        short.start()
        short.join(10)
        assert not short.is_alive(), "the wait for the lock has no end"
        assert "not released within 0.3 s" in result["short"]
        assert time.monotonic() - t0 >= 0.25

        long_ = threading.Thread(target=attempt, args=("long", 10.0), daemon=True)
        long_.start()
        time.sleep(0.3)
        assert "long" not in result                  # still waiting, not refused
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    long_.join(10)
    assert result["long"] == "granted"


def test_the_default_wait_for_the_lock_is_ten_seconds(tmp_path):
    """Catches: a default bound that is an hour (a stuck holder then stops the
    project for that long without a word). The launcher passes no timeout."""
    assert LOCK_TIMEOUT_SECONDS == 10.0
    assert BudgetReservations(tmp_path).lock_timeout == 10.0


# ==== reserve: fail closed ======================================================

def _ledger(tmp_path) -> Path:
    return tmp_path / "root" / LEDGER_NAME


def _good_row(**over) -> dict:
    row = {"reservation_id": "res-1-n1", "run_id": "n1", "amount": 3,
           "status": "HELD", "reserved_at": "2026-10-12T10:00:00Z", "ceiling": 9}
    row.update(over)
    return row


def _good_ledger(*rows) -> str:
    return json.dumps({"schema_version": SCHEMA, "written_at": "2026-10-12T10:00:00Z",
                       "reservations": list(rows)})


BAD_LEDGERS = {
    "not json": b"{not json",
    "empty": b"",
    "a list": b"[]",
    "a scalar": b"3",
    "null": b"null",
    "wrong schema": json.dumps({"schema_version": "x", "reservations": []}).encode(),
    "no schema": json.dumps({"reservations": []}).encode(),
    "rows not a list": json.dumps({"schema_version": SCHEMA, "reservations": {}}).encode(),
    "row not an object": _good_ledger("n1").encode(),
    "row missing a key": _good_ledger({"run_id": "n1"}).encode(),
    "row with an extra key": _good_ledger(_good_row(extra=1)).encode(),
    "amount zero": _good_ledger(_good_row(amount=0)).encode(),
    "amount a string": _good_ledger(_good_row(amount="3")).encode(),
    "status unknown": _good_ledger(_good_row(status="LOST")).encode(),
    "run id with a slash": _good_ledger(_good_row(run_id="a/b")).encode(),
    "run id with a newline": _good_ledger(_good_row(run_id="n1\n")).encode(),
    "latin-1": '{"reservations": "é"}'.encode("latin-1"),
    "utf-16": _good_ledger().encode("utf-16"),
    "a NUL": b"\x00" + _good_ledger().encode(),
    "deeply nested": b"[" * 200000 + b"]" * 200000,
    "a BOM": b"\xef\xbb\xbf" + _good_ledger().encode(),
    "a lone surrogate in a run id": _good_ledger(_good_row(run_id="\ud800")).encode(
        "utf-8", "surrogatepass"),
    "a lone surrogate escape in a run id": (
        b'{"schema_version": "' + SCHEMA.encode() + b'", "reservations": [{"reservation_id": "a",'
        b' "run_id": "\\ud800", "amount": 3, "status": "HELD", "reserved_at": "t", "ceiling": 9}]}'),
}


@pytest.mark.parametrize("name", sorted(BAD_LEDGERS))
def test_a_ledger_that_cannot_be_read_refuses_and_is_left_as_it_was(tmp_path, name):
    """Catches: an unreadable ledger read as no holds (every start then granted
    the whole budget). Refused, and the file's bytes are untouched."""
    root = tmp_path / "root"
    root.mkdir()
    _ledger(tmp_path).write_bytes(BAD_LEDGERS[name])
    with pytest.raises(ReservationError):
        BudgetReservations(root).reserve("n2", ceiling=9, spent=_spent())
    assert _ledger(tmp_path).read_bytes() == BAD_LEDGERS[name]


def test_an_oversize_ledger_is_refused_at_the_ceiling(tmp_path):
    """Catches: a bound that is gone or off by one. A valid ledger padded to
    exactly MAX_LEDGER_BYTES is read; one byte more is refused."""
    root = tmp_path / "root"
    root.mkdir()
    body = _good_ledger().encode()
    _ledger(tmp_path).write_bytes(body + b" " * (MAX_LEDGER_BYTES - len(body)))
    assert BudgetReservations(root).read() == []
    _ledger(tmp_path).write_bytes(body + b" " * (MAX_LEDGER_BYTES - len(body) + 1))
    with pytest.raises(ReservationError, match="above the"):
        BudgetReservations(root).read()


def _make_dir(p):
    p.mkdir()


def _make_fifo(p):
    os.mkfifo(p)


def _make_dangling(p):
    p.symlink_to(p.parent / "nowhere")


def _make_link_to_good(p):
    real = p.parent / "real-ledger"
    real.write_text(_good_ledger())
    p.symlink_to(real)


def _make_second_name(p):
    p.write_text(_good_ledger())
    os.link(p, p.parent / "second-name")


@pytest.mark.parametrize("make", [_make_dir, _make_fifo, _make_dangling,
                                  _make_link_to_good, _make_second_name],
                         ids=["directory", "fifo", "dangling-link", "link-to-a-good-ledger",
                              "second-name"])
def test_a_ledger_that_is_not_one_regular_file_refuses(tmp_path, make):
    """Catches: a plain `read_text` (a FIFO hangs it, a link is followed, a second
    name is another file's). Run in a thread so a hang is a failure, not a stall."""
    root = tmp_path / "root"
    root.mkdir()
    make(_ledger(tmp_path))
    out: dict = {}

    def attempt():
        try:
            BudgetReservations(root).reserve("n2", ceiling=9, spent=_spent())
            out["r"] = "granted"
        except ReservationError as exc:
            out["r"] = "refused"
            out["why"] = str(exc)

    thread = threading.Thread(target=attempt, daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive(), "reading the ledger hung"
    assert out["r"] == "refused", out


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode 000")
def test_a_ledger_without_read_permission_refuses(tmp_path):
    """Catches: PermissionError read as an absent ledger."""
    root = tmp_path / "root"
    root.mkdir()
    _ledger(tmp_path).write_text(_good_ledger())
    _ledger(tmp_path).chmod(0)
    with pytest.raises(ReservationError):
        BudgetReservations(root).reserve("n2", ceiling=9, spent=_spent())


@pytest.mark.parametrize("make", [_make_dir, _make_dangling, _make_fifo],
                         ids=["directory", "dangling-link", "fifo"])
def test_a_lock_file_that_is_not_a_regular_file_refuses(tmp_path, make):
    """Catches: a lock taken on something other than the file it names (a link
    followed to wherever it points, a FIFO). Nothing is granted or written."""
    root = tmp_path / "root"
    root.mkdir()
    make(root / LOCK_NAME)
    result: dict = {}

    def attempt():
        try:
            BudgetReservations(root, lock_timeout=0.5).reserve(
                "n1", ceiling=9, spent=_spent())
            result["r"] = "granted"
        except ReservationError:
            result["r"] = "refused"

    thread = threading.Thread(target=attempt, daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()
    assert result["r"] == "refused"
    assert not _ledger(tmp_path).exists()


@pytest.mark.parametrize("bad", ["", "a/b", "..", "n1\n", " n1", "n 1", "n\x001",
                                 "é", "x" * 256, None, 7, b"n1"])
def test_a_run_id_that_is_not_a_plain_name_is_refused(tmp_path, bad):
    """Catches: a hold keyed by something that is not a run's directory name
    (`$` vs `\\Z`, path separators, folding). Nothing is created."""
    store = _store(tmp_path)
    with pytest.raises(ReservationError):
        store.reserve(bad, ceiling=9, spent=_spent())
    with pytest.raises(ReservationError):
        store.settle(bad, "x")
    assert not (tmp_path / "root").exists()


def test_a_run_id_of_the_longest_plain_name_is_accepted(tmp_path):
    """Catches: a length bound below a directory name's own (255). Control for
    the 256-character refusal above."""
    assert _store(tmp_path).reserve("x" * 255, ceiling=9, spent=_spent()).amount == 9


@pytest.mark.parametrize("ceiling", [0, -1, True, 1.5, "9", None])
def test_a_ceiling_that_is_not_a_whole_number_of_one_or_more_is_refused(tmp_path, ceiling):
    """Catches: a bool or string read as a budget; a zero ceiling granting."""
    with pytest.raises(ReservationError):
        _store(tmp_path).reserve("n1", ceiling=ceiling, spent=_spent())


@pytest.mark.parametrize("want", [0, -1, True, 1.5, "3"])
def test_a_want_that_is_not_a_whole_number_of_one_or_more_is_refused(tmp_path, want):
    """Catches: `want=0` read as `None` (everything) by a truthiness test."""
    with pytest.raises(ReservationError):
        _store(tmp_path).reserve("n1", ceiling=9, spent=_spent(), want=want)


@pytest.mark.parametrize("spent", [{"a": -1}, {"a": True}, {"a": 1.5}, {"a": "2"},
                                   {3: 1}, [1, 2], None],
                         ids=["negative", "bool", "float", "string", "int-key", "list", "none"])
def test_a_spend_that_is_not_a_count_per_run_is_refused(tmp_path, spent):
    """Catches: a negative spend *raising* what is left; a malformed spend read
    as zero."""
    with pytest.raises(ReservationError):
        _store(tmp_path).reserve("n1", ceiling=9, spent=lambda: spent)
    assert not _ledger(tmp_path).exists()


def test_a_disk_too_full_to_record_the_hold_grants_nothing(tmp_path, monkeypatch):
    """Catches: a grant returned although its record was not written (the next
    start would not see it). The ledger stays absent."""
    def full(path, payload, **kw):
        raise durable_io.DiskFullError(path, len(payload), 0, "test")

    monkeypatch.setattr(durable_io, "durable_write", full)
    with pytest.raises(ReservationError, match="could not be recorded"):
        _store(tmp_path).reserve("n1", ceiling=9, spent=_spent())
    assert not _ledger(tmp_path).exists()


def test_the_ledger_is_written_private_and_whole(tmp_path):
    """Catches: a world-readable ledger or a partial file (a temp name left, a
    row missing the schema)."""
    store = _store(tmp_path)
    store.reserve("n1", ceiling=9, spent=_spent())
    assert (_ledger(tmp_path).stat().st_mode & 0o777) == 0o600
    data = json.loads(_ledger(tmp_path).read_text())
    assert data["schema_version"] == SCHEMA and len(data["reservations"]) == 1
    assert sorted(p.name for p in (tmp_path / "root").iterdir()) == [LEDGER_NAME, LOCK_NAME]


# ==== the launcher ===============================================================

def _write_run(root: Path, run_id: str, dispatches) -> None:
    d = root / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(
        json.dumps({"run_id": run_id, "usage": {"dispatches": dispatches}}),
        encoding="utf-8")


class _Grants:
    name = "test"

    def may_approve(self, worktree):
        return True

    def approve(self, worktree, repo):
        from hoh.approval import Approval
        return Approval(granted=True, worktree=worktree, provider=self.name)


def _launcher(tmp_path, monkeypatch, *, start_exit=0, start_raises=None, **kw):
    """A launcher whose CLI is a recorder. `calls` holds every argv; the one for
    `start` is `starts`."""
    wt = tmp_path / "worktree-home"
    wt.mkdir(exist_ok=True)
    launcher = HohRunLauncher(tmp_path / "root", tmp_path / "repo", approvals=_Grants(), **kw)
    calls: list[list[str]] = []

    def run_cli(argv):
        calls.append(list(argv))
        if "worktree" in argv:
            return Dispatch(0, json.dumps({"path": str(wt)}), "")
        if "start" in argv:
            if start_raises is not None:
                raise start_raises
            return Dispatch(start_exit, "", "start refused" if start_exit else "")
        raise AssertionError(f"unexpected cli call {argv}")

    monkeypatch.setattr(launcher, "_run_cli", run_cli)
    return launcher, calls


def _node(run_id: str) -> TaskNode:
    return TaskNode(id=run_id, repair_of="gate-x", run_id=run_id)


def _start_ceiling(calls: list[list[str]]) -> str:
    start = [c for c in calls if "start" in c][-1]
    return start[start.index("--max-dispatches") + 1]


def _ledger_of(launcher) -> BudgetReservations:
    return BudgetReservations(launcher.root)


def test_prepare_holds_the_share_it_starts_the_run_with(tmp_path, monkeypatch):
    """Catches: a start whose ceiling is read, not held (the ledger stays empty),
    and a ceiling that is not the grant. 9 - 4 spent = 5 held and passed."""
    _write_run(tmp_path / "root", "old", 4)
    launcher, calls = _launcher(tmp_path, monkeypatch, dispatch_budget=9)
    assert launcher.prepare(_node("n1")) is None
    assert _start_ceiling(calls) == "5"
    assert _rows(_ledger_of(launcher)) == [("n1", 5, "HELD")]


def test_the_per_run_reservation_is_what_a_start_holds(tmp_path, monkeypatch):
    """Catches: `dispatch_reservation` not forwarded as `want`. Three prepares
    hold 3 each from nine and the fourth is refused as a spent budget."""
    launcher, calls = _launcher(tmp_path, monkeypatch, dispatch_budget=9,
                                dispatch_reservation=3)
    for run_id in ("n1", "n2", "n3"):
        assert launcher.prepare(_node(run_id)) is None
        assert _start_ceiling(calls) == "3"
    with pytest.raises(BudgetExhausted, match="cannot be reserved for n4"):
        launcher.prepare(_node("n4"))
    assert sum(r.amount for r in _ledger_of(launcher).held()) == 9
    assert len([c for c in calls if "start" in c]) == 3


def test_a_second_prepare_before_the_first_is_dispatched_gets_what_is_left(tmp_path, monkeypatch):
    """Catches: the defect on the real path. With no per-run share the first
    start holds all 9; a second prepare before the first run is dispatched is
    refused as a budget and never reaches `hoh start`."""
    launcher, calls = _launcher(tmp_path, monkeypatch, dispatch_budget=9)
    assert launcher.prepare(_node("n1")) is None
    assert _start_ceiling(calls) == "9"
    with pytest.raises(BudgetExhausted, match="cannot be reserved for n2: nothing is left"):
        launcher.prepare(_node("n2"))
    assert len([c for c in calls if "start" in c]) == 1


def test_a_second_launcher_over_the_same_root_sees_the_hold(tmp_path, monkeypatch):
    """Catches: holds kept in the launcher object (a restart or a second
    orchestrator would be granted the same share again)."""
    first, _ = _launcher(tmp_path, monkeypatch, dispatch_budget=9, dispatch_reservation=6)
    assert first.prepare(_node("n1")) is None
    second, calls = _launcher(tmp_path, monkeypatch, dispatch_budget=9, dispatch_reservation=6)
    assert second.prepare(_node("n2")) is None
    assert _start_ceiling(calls) == "3"


def test_launch_ends_the_hold_whatever_the_dispatch_does(tmp_path, monkeypatch):
    """Catches: a hold left open after the dispatch (the budget shrinking for
    good), on a verdict and on an exception. The next prepare is granted the
    remainder after what n1 spent."""
    launcher, calls = _launcher(tmp_path, monkeypatch, dispatch_budget=9)
    assert launcher.prepare(_node("n1")) is None
    monkeypatch.setattr(launcher, "_launch",
                        lambda node: RunOutcome(RunVerdict.REJECTED, "no"))
    assert launcher.launch(_node("n1")).verdict is RunVerdict.REJECTED
    assert _rows(_ledger_of(launcher)) == [("n1", 9, "SETTLED")]
    _write_run(tmp_path / "root", "n1", 4)
    assert launcher.prepare(_node("n2")) is None
    assert _start_ceiling(calls) == "5"

    def boom(node):
        raise RuntimeError("the dispatch fell over")

    monkeypatch.setattr(launcher, "_launch", boom)
    with pytest.raises(RuntimeError, match="fell over"):
        launcher.launch(_node("n2"))
    assert [s for _, _, s in _rows(_ledger_of(launcher))] == ["SETTLED", "SETTLED"]


@pytest.mark.parametrize("verdict,ends", [
    (RunVerdict.ACCEPTED, True), (RunVerdict.REJECTED, True), (RunVerdict.UNDETERMINED, True),
    (RunVerdict.PROVIDER_UNAVAILABLE, True), (RunVerdict.IN_FLIGHT, False)])
def test_a_launch_keeps_the_hold_only_of_a_run_another_process_still_executes(
        tmp_path, monkeypatch, verdict, ends):
    """Catches: ending the hold of a run that can still dispatch (IN_FLIGHT: its
    headroom would be handed out again), and keeping the hold of one that has
    finished. The state of the ledger is read after the launch."""
    launcher, _ = _launcher(tmp_path, monkeypatch, dispatch_budget=9)
    assert launcher.prepare(_node("n1")) is None
    monkeypatch.setattr(launcher, "_launch", lambda n: RunOutcome(verdict, "x"))
    launcher.launch(_node("n1"))
    assert _rows(_ledger_of(launcher)) == [("n1", 9, "SETTLED" if ends else "HELD")]


EVALUATION_ENDS_THE_HOLD = [
    # (lifecycle the orchestrator holds the node in, verdict read, hold ended?)
    # READY: the launch that follows ends it, except for an acceptance (no launch).
    ("READY", RunVerdict.ACCEPTED, True),
    ("READY", RunVerdict.REJECTED, False),
    ("READY", RunVerdict.NOT_STARTED, False),
    ("READY", RunVerdict.IN_FLIGHT, False),
    # RUNNING (a dead session's node): no launch follows unless it is NOT_STARTED.
    ("RUNNING", RunVerdict.ACCEPTED, True),
    ("RUNNING", RunVerdict.REJECTED, True),
    ("RUNNING", RunVerdict.PROVIDER_UNAVAILABLE, True),
    ("RUNNING", RunVerdict.STORAGE_WAIT, True),
    ("RUNNING", RunVerdict.NEEDS_APPROVAL, True),
    ("RUNNING", RunVerdict.IN_FLIGHT, False),
    ("RUNNING", RunVerdict.NOT_STARTED, False),
    ("RUNNING", RunVerdict.UNDETERMINED, False),
]


@pytest.mark.parametrize("lifecycle,verdict,ends", EVALUATION_ENDS_THE_HOLD,
                         ids=[f"{l}-{v.value}" for l, v, _ in EVALUATION_ENDS_THE_HOLD])
def test_an_evaluation_ends_the_hold_when_no_launch_will_follow_it(
        tmp_path, monkeypatch, lifecycle, verdict, ends):
    """Catches: a hold stranded by a node the orchestrator settles without a
    launch (a dead session's rejected or parked run, an acceptance: the budget
    would shrink for good and the repair node be refused), and a hold dropped
    before the launch that follows (a long dispatch with nothing held)."""
    from hoh.project import Lifecycle

    launcher, _ = _launcher(tmp_path, monkeypatch, dispatch_budget=9)
    assert launcher.prepare(_node("n1")) is None
    monkeypatch.setattr(launcher, "_evaluate", lambda n: RunOutcome(verdict, "x"))
    node = _node("n1")
    node.lifecycle = Lifecycle[lifecycle]
    assert launcher.evaluate(node).verdict is verdict
    assert _rows(_ledger_of(launcher)) == [("n1", 9, "SETTLED" if ends else "HELD")]


def test_a_verdict_seam_that_returns_something_else_is_passed_through(tmp_path, monkeypatch):
    """Catches: the wrappers reading `.verdict` of whatever a stubbed seam returns
    (`tests/test_dispatch_provenance.py` replaces `_verdict` with a string): the
    result is returned unchanged and the hold is ended by `launch`, not raised
    over. `evaluate` of such a result ends nothing."""
    from hoh.project import Lifecycle

    launcher, _ = _launcher(tmp_path, monkeypatch, dispatch_budget=9)
    assert launcher.prepare(_node("n1")) is None
    monkeypatch.setattr(launcher, "_evaluate", lambda n: "stub")
    node = _node("n1")
    node.lifecycle = Lifecycle.RUNNING
    assert launcher.evaluate(node) == "stub"
    assert _rows(_ledger_of(launcher)) == [("n1", 9, "HELD")]
    monkeypatch.setattr(launcher, "_launch", lambda n: "stub")
    assert launcher.launch(node) == "stub"
    assert _rows(_ledger_of(launcher)) == [("n1", 9, "SETTLED")]


def test_a_start_that_fails_gives_its_hold_back(tmp_path, monkeypatch):
    """Catches: a hold stranded by a start that never happened -- on an exit
    code and on an exception (a timeout)."""
    launcher, _ = _launcher(tmp_path, monkeypatch, start_exit=1, dispatch_budget=9)
    assert "could not start run n1" in launcher.prepare(_node("n1"))
    assert _rows(_ledger_of(launcher)) == [("n1", 9, "SETTLED")]

    raising, _ = _launcher(tmp_path, monkeypatch,
                           start_raises=subprocess.TimeoutExpired("hoh", 1),
                           dispatch_budget=9)
    with pytest.raises(subprocess.TimeoutExpired):
        raising.prepare(_node("n2"))
    assert [(r, s) for r, _, s in _rows(_ledger_of(raising))] == [
        ("n1", "SETTLED"), ("n2", "SETTLED")]
    assert _launcher(tmp_path, monkeypatch, dispatch_budget=9)[0].prepare(_node("n3")) is None


def test_a_spent_budget_is_refused_before_anything_is_created(tmp_path, monkeypatch):
    """Catches: the early refusal moved behind the worktree and the trust
    approval (side effects for work that was refused). Nothing at all is called."""
    _write_run(tmp_path / "root", "old", 9)
    launcher, calls = _launcher(tmp_path, monkeypatch, dispatch_budget=9)
    with pytest.raises(BudgetExhausted, match="9/9"):
        launcher.prepare(_node("n1"))
    assert calls == [] and launcher.approvals_given == []
    assert not (tmp_path / "root" / LEDGER_NAME).exists()


def test_a_new_run_the_holds_leave_nothing_for_is_refused_before_anything_is_created(
        tmp_path, monkeypatch):
    """Catches: the early refusal covering only a spent budget. n1 holds all 9;
    n2 is refused with no worktree, no trust approval, no specification and no
    `hoh start` -- the comment at the top of `prepare` is about exactly that."""
    launcher, calls = _launcher(tmp_path, monkeypatch, dispatch_budget=9)
    assert launcher.prepare(_node("n1")) is None
    calls_before, approvals_before = list(calls), len(launcher.approvals_given)
    with pytest.raises(BudgetExhausted, match="cannot be reserved for n2: nothing is left"):
        launcher.prepare(_node("n2"))
    assert calls == calls_before
    assert len(launcher.approvals_given) == approvals_before
    assert not (tmp_path / "root" / "n2").exists()


def test_a_start_is_not_refused_for_the_hold_it_left_itself(tmp_path, monkeypatch):
    """Catches: the early refusal counting the asking run's own earlier hold (a
    `prepare` that died before `hoh start`, made again, would block for ever). The
    new hold supersedes the old."""
    launcher, calls = _launcher(tmp_path, monkeypatch, dispatch_budget=9)
    _ledger_of(launcher).reserve("n1", ceiling=9, spent=_spent())
    assert launcher.prepare(_node("n1")) is None
    assert _start_ceiling(calls) == "9"
    assert _rows(_ledger_of(launcher)) == [("n1", 9, "SUPERSEDED"), ("n1", 9, "HELD")]


def test_a_run_that_exists_is_not_refused_because_others_hold_the_budget(tmp_path, monkeypatch):
    """Catches: the early refusal applied to the resume path (a run that passes no
    ceiling and takes no hold stopped for what other runs hold)."""
    _write_run(tmp_path / "root", "n1", 2)
    launcher, calls = _launcher(tmp_path, monkeypatch, dispatch_budget=9)
    _ledger_of(launcher).reserve("n2", ceiling=9, spent=_spent(n1=2))
    monkeypatch.setattr(launcher, "_ensure_runnable", lambda *a: None)
    assert launcher.prepare(_node("n1")) is None
    assert [c for c in calls if "start" in c] == []


def test_a_dry_run_leaves_a_hold_alone_even_when_a_ledger_exists(tmp_path, monkeypatch):
    """Catches: `_settle` ignoring `dry_run` (a dry run promises that nothing is
    dispatched or written, and would end a live hold). A second launcher over the
    root, dry, launches the node."""
    live, _ = _launcher(tmp_path, monkeypatch, dispatch_budget=9)
    assert live.prepare(_node("n1")) is None
    dry = HohRunLauncher(tmp_path / "root", tmp_path / "repo", dispatch_budget=9, dry_run=True)
    assert dry.launch(_node("n1")).verdict is RunVerdict.NOT_RUN
    assert _rows(_ledger_of(dry)) == [("n1", 9, "HELD")]


def test_a_run_that_already_exists_holds_nothing_at_prepare(tmp_path, monkeypatch):
    """Catches: a hold taken on the resume path, where no `--max-dispatches` is
    passed and no start happens (it would strand until the launch)."""
    _write_run(tmp_path / "root", "n1", 2)
    launcher, calls = _launcher(tmp_path, monkeypatch, dispatch_budget=9)
    monkeypatch.setattr(launcher, "_ensure_runnable", lambda *a: None)
    assert launcher.prepare(_node("n1")) is None
    assert [c for c in calls if "start" in c] == []
    assert not (tmp_path / "root" / LEDGER_NAME).exists()


def test_without_a_budget_nothing_is_held_and_no_file_is_made(tmp_path, monkeypatch):
    """Catches: the default behaviour changing (a ceiling nobody asked for, or a
    ledger beside a root that has no budget)."""
    launcher, calls = _launcher(tmp_path, monkeypatch)
    assert launcher.prepare(_node("n1")) is None
    assert "--max-dispatches" not in [c for c in calls if "start" in c][0]
    monkeypatch.setattr(launcher, "_launch", lambda n: RunOutcome(RunVerdict.REJECTED))
    launcher.launch(_node("n1"))
    assert not (tmp_path / "root" / LEDGER_NAME).exists()
    assert not (tmp_path / "root" / LOCK_NAME).exists()


def test_a_dry_run_launch_touches_no_ledger(tmp_path, monkeypatch):
    """Catches: `launch` settling (and so locking, and creating files) on a dry
    run, which promises that nothing is dispatched or written."""
    launcher, _ = _launcher(tmp_path, monkeypatch, dispatch_budget=9, dry_run=True)
    assert launcher.launch(_node("n1")).verdict is RunVerdict.NOT_RUN
    assert not (tmp_path / "root").exists()


def test_a_hold_that_cannot_be_ended_is_noted_and_the_verdict_stands(tmp_path, monkeypatch):
    """Catches: a failed settle raising over the run's verdict (a finished run
    reported as an error), or being silent. The hold stays open, and says why."""
    launcher, _ = _launcher(tmp_path, monkeypatch, dispatch_budget=9)
    assert launcher.prepare(_node("n1")) is None
    (tmp_path / "root" / LEDGER_NAME).write_text("{not json")
    monkeypatch.setattr(launcher, "_launch",
                        lambda n: RunOutcome(RunVerdict.ACCEPTED, "yes"))
    assert launcher.launch(_node("n1")).verdict is RunVerdict.ACCEPTED
    assert len(launcher.reservation_notes) == 1
    assert "hold of n1 stays open" in launcher.reservation_notes[0]


def test_a_note_about_a_hold_that_stays_open_is_kept_to_three_hundred_characters(
        tmp_path, monkeypatch):
    """Catches: an unbounded note (the ledger path and the error are quoted)."""
    deep = tmp_path / ("d" * 200)
    deep.mkdir()
    launcher, _ = _launcher(deep, monkeypatch, dispatch_budget=9)
    (deep / "root").mkdir()
    (deep / "root" / LEDGER_NAME).write_text("{not json" + " " * 400)
    launcher._settle("n1", "x")
    assert len(launcher.reservation_notes) == 1
    assert len(launcher.reservation_notes[0]) == 300


def test_an_unreadable_ledger_stops_a_start_as_a_budget_not_a_crash(tmp_path, monkeypatch):
    """Catches: a ReservationError escaping `prepare` (the orchestrator catches
    only BudgetExhausted) or an unreadable ledger counted as no holds."""
    launcher, calls = _launcher(tmp_path, monkeypatch, dispatch_budget=9)
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / LEDGER_NAME).write_text("[]")
    with pytest.raises(BudgetExhausted, match="cannot be reserved for n1"):
        launcher.prepare(_node("n1"))
    assert [c for c in calls if "start" in c] == []


@pytest.mark.parametrize("budget,share", [(None, 3), (9, 0), (9, -1), (9, True), (9, "3"),
                                          (9, 1.5)])
def test_a_per_run_share_without_a_budget_or_that_is_not_a_count_is_refused(
        tmp_path, budget, share):
    """Catches: a share accepted that can never be held (a silent no-op), or a
    bool/zero share read as `None` (everything)."""
    with pytest.raises(ValueError):
        HohRunLauncher(tmp_path / "root", tmp_path / "repo", dispatch_budget=budget,
                       dispatch_reservation=share)


# ==== the spend the holds are combined with ======================================

def _spend(tmp_path) -> dict[str, int]:
    return HohRunLauncher(tmp_path / "root", tmp_path / "repo",
                          dispatch_budget=9)._spent_by_run()


def test_the_spend_is_keyed_by_the_run_directory_and_sums_to_the_old_total(tmp_path):
    """Catches: a spend that is not per run (a hold could not be netted against
    its own run) or whose sum is not `spent_budget`."""
    root = tmp_path / "root"
    _write_run(root, "n1", 4)
    _write_run(root / "nested", "n2", 3)
    launcher = HohRunLauncher(root, tmp_path / "repo", dispatch_budget=9)
    assert launcher._spent_by_run() == {"n1": 4, "n2": 3}
    assert launcher.spent_budget() == 7 and launcher.remaining_budget() == 2


BAD_STATES = {
    "a list": b"[]",
    "a scalar": b"7",
    "usage a list": b'{"usage": []}',
    "dispatches negative": b'{"usage": {"dispatches": -3}}',
    "dispatches a string": b'{"usage": {"dispatches": "3"}}',
    "dispatches true": b'{"usage": {"dispatches": true}}',
    "dispatches a float": b'{"usage": {"dispatches": 1.5}}',
    "not json": b"{not json",
    "latin-1": '{"run_id": "é"}'.encode("latin-1"),
    "utf-16": json.dumps({"usage": {"dispatches": 1}}).encode("utf-16"),
    "a NUL": b'{"usage": {"dispatches": 1}}\x00',
    "deeply nested": b"[" * 200000 + b"]" * 200000,
    "dispatches NaN": b'{"usage": {"dispatches": NaN}}',
    "dispatches beyond the integer digit limit": b'{"usage": {"dispatches": 1' + b"0" * 5000 + b"}}",
    "a BOM": b"\xef\xbb\xbf" + b'{"usage": {"dispatches": 1}}',
}


@pytest.mark.parametrize("name", sorted(BAD_STATES))
def test_a_state_that_is_not_a_count_stops_the_budget(tmp_path, name):
    """Catches: a malformed state counted as zero or as a negative spend (the
    one run whose record is odd is the one that may have spent it), or escaping
    as a traceback the orchestrator does not catch."""
    root = tmp_path / "root"
    _write_run(root, "n1", 4)
    (root / "n1" / "state.json").write_bytes(BAD_STATES[name])
    with pytest.raises(BudgetExhausted, match="cannot be computed"):
        HohRunLauncher(root, tmp_path / "repo", dispatch_budget=9).spent_budget()


@pytest.mark.parametrize("kind", ["fifo", "link", "dangling", "directory"])
def test_a_state_that_is_not_a_regular_file_stops_the_budget(tmp_path, kind):
    """Catches: reading through a link to wherever it points, or hanging on a
    FIFO. In a thread; a FIFO is opened for writing at the end so that a
    reader that hung is released and the test can say so."""
    root = tmp_path / "root"
    _write_run(root, "n1", 4)
    path = root / "n2" / "state.json"
    path.parent.mkdir()
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "link":
        path.symlink_to(root / "n1" / "state.json")
    elif kind == "dangling":
        path.symlink_to(root / "nowhere")
    else:
        path.mkdir()
    out: dict = {}

    def attempt():
        try:
            out["r"] = HohRunLauncher(root, tmp_path / "repo", dispatch_budget=9).spent_budget()
        except BudgetExhausted as exc:
            out["r"] = "refused"
            out["why"] = str(exc)

    thread = threading.Thread(target=attempt, daemon=True)
    thread.start()
    thread.join(5)
    hung = thread.is_alive()
    if hung and kind == "fifo":
        fd = os.open(path, os.O_WRONLY)       # releases a reader blocked in open()
        os.close(fd)
        thread.join(5)
    assert not hung, "reading the state hung"
    assert out["r"] == "refused", out


def test_a_state_is_read_up_to_its_ceiling_and_no_further(tmp_path):
    """Catches: an unbounded read or an off-by-one ceiling. A valid state padded
    to exactly MAX_STATE_BYTES is counted; one byte more stops the budget."""
    root = tmp_path / "root"
    _write_run(root, "n1", 4)
    body = (root / "n1" / "state.json").read_bytes()
    (root / "n1" / "state.json").write_bytes(body + b" " * (MAX_STATE_BYTES - len(body)))
    assert HohRunLauncher(root, tmp_path / "repo", dispatch_budget=9).spent_budget() == 4
    (root / "n1" / "state.json").write_bytes(body + b" " * (MAX_STATE_BYTES - len(body) + 1))
    with pytest.raises(BudgetExhausted, match="cannot be computed"):
        HohRunLauncher(root, tmp_path / "repo", dispatch_budget=9).spent_budget()


def test_a_state_without_a_usage_block_or_a_count_is_a_spend_of_zero(tmp_path):
    """Control for the refusals above: only an *absent* count is zero, which is
    what a run that has dispatched nothing writes."""
    root = tmp_path / "root"
    for name, text in (("n1", b"{}"), ("n2", b'{"usage": null}'),
                       ("n3", b'{"usage": {}}'), ("n4", b'{"usage": {"dispatches": null}}')):
        (root / name).mkdir(parents=True)
        (root / name / "state.json").write_bytes(text)
    assert _spend(tmp_path) == {"n1": 0, "n2": 0, "n3": 0, "n4": 0}
