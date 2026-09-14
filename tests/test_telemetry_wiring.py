"""The controller has to write a telemetry record for every role dispatch.

This file exists **before** the work and fails. That is deliberate: a
criterion whose own file is part of the candidate is red on the baseline
because the file is missing, which every new test satisfies and which
demonstrates nothing about behaviour (O112). Here the baseline fails because
the controller does not write telemetry, which is the behaviour under test.
"""

from __future__ import annotations

import json

from hoh.telemetry import DispatchRecord, TelemetryLog


def _log_path(store):
    return store.dir / "telemetry.jsonl"


def test_the_controller_exposes_a_telemetry_log(tmp_path):
    """The log lives beside the run's other evidence, under the run's own
    directory, so it travels with the run and not with the machine."""
    from hoh.controller import Controller
    from hoh.store import RunStore

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store
    log = c.telemetry()
    assert isinstance(log, TelemetryLog)
    assert log.path == _log_path(store)


def test_a_dispatch_appends_exactly_one_record(tmp_path):
    from hoh.controller import Controller
    from hoh.store import RunStore

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store

    c.note_dispatch(
        role="planner", run_id="r", iteration=1, attempt=1,
        started_at="2026-09-11T10:00:00Z", ended_at="2026-09-11T10:00:04Z",
        backend="herdr", outcome="ok", usage={},
    )
    saetze = c.telemetry().read()
    assert len(saetze) == 1
    r = saetze[0]
    assert r.role == "planner"
    assert r.run_id == "r"
    assert r.iteration == 1
    assert r.backend == "herdr"
    assert r.outcome == "ok"
    assert r.wallclock_seconds == 4.0


def test_usage_the_dispatcher_did_not_report_stays_unknown(tmp_path):
    """Never 0 as a stand-in for unknown. A zero that means "not measured" is
    this project's fourth false green, in one field."""
    from hoh.controller import Controller
    from hoh.store import RunStore

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store
    c.note_dispatch(
        role="qa", run_id="r", iteration=1, attempt=1,
        started_at="2026-09-11T10:00:00Z", ended_at="2026-09-11T10:00:01Z",
        backend="herdr", outcome="ok", usage={},
    )
    r = c.telemetry().read()[0]
    assert r.tokens_in is None and r.tokens_out is None


def test_usage_the_dispatcher_did_report_is_carried_through(tmp_path):
    from hoh.controller import Controller
    from hoh.store import RunStore

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store
    c.note_dispatch(
        role="developer", run_id="r", iteration=2, attempt=1,
        started_at="2026-09-11T10:00:00Z", ended_at="2026-09-11T10:00:09Z",
        backend="herdr", outcome="ok",
        usage={"input_tokens": 120, "output_tokens": 34},
    )
    r = c.telemetry().read()[0]
    assert (r.tokens_in, r.tokens_out) == (120, 34)


def test_a_failed_dispatch_is_recorded_as_failed(tmp_path):
    from hoh.controller import Controller
    from hoh.store import RunStore

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store
    c.note_dispatch(
        role="qa", run_id="r", iteration=1, attempt=2,
        started_at="2026-09-11T10:00:00Z", ended_at="2026-09-11T10:00:02Z",
        backend="herdr", outcome="failed", detail="the answer did not validate",
        usage={},
    )
    r = c.telemetry().read()[0]
    assert r.outcome == "failed"
    assert "did not validate" in r.detail


def test_a_telemetry_failure_never_fails_the_run(tmp_path, monkeypatch):
    """Telemetry is a record of the work, not a precondition for it."""
    from hoh.controller import Controller
    from hoh.store import RunStore

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store

    def kaputt(self, record):
        raise OSError("no space left on device")

    monkeypatch.setattr(TelemetryLog, "append", kaputt)
    # Must not raise.
    c.note_dispatch(
        role="planner", run_id="r", iteration=1, attempt=1,
        started_at="2026-09-11T10:00:00Z", ended_at="2026-09-11T10:00:01Z",
        backend="herdr", outcome="ok", usage={},
    )


def test_records_are_one_json_object_per_line(tmp_path):
    from hoh.controller import Controller
    from hoh.store import RunStore

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store
    for rolle in ("planner", "developer", "qa"):
        c.note_dispatch(
            role=rolle, run_id="r", iteration=1, attempt=1,
            started_at="2026-09-11T10:00:00Z", ended_at="2026-09-11T10:00:01Z",
            backend="herdr", outcome="ok", usage={},
        )
    zeilen = _log_path(store).read_text().strip().splitlines()
    assert len(zeilen) == 3
    for z in zeilen:
        assert DispatchRecord.model_validate(json.loads(z))


# --------------------------------------------------------------------------- #
# O128: the fields that were well-typed blanks on real dispatches
# --------------------------------------------------------------------------- #


def test_a_record_names_the_provider_it_actually_ran_under(tmp_path):
    """It was empty on all four records of a real three-agent run."""
    from hoh.contracts import Role
    from hoh.controller import Controller
    from hoh.store import RunStore

    class Versender:
        profiles = {Role.PLANNER: "claude"}

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store
    c.dispatcher = Versender()

    c.note_dispatch(role="planner", run_id="r", iteration=1, attempt=0,
                    started_at="2026-09-11T10:00:00Z",
                    ended_at="2026-09-11T10:00:04Z", backend="herdr", usage={})
    (satz,) = c.telemetry().read()
    assert satz.provider == "claude"


def test_an_unreportable_model_says_so_instead_of_staying_empty(tmp_path):
    """An agent in a pane picks its own model and this process never learns it.

    `NOT_AVAILABLE` is that answer. An empty string is the absence of an
    answer, and a log full of the second wearing the first is what the audit
    was written to find.
    """
    from hoh.controller import Controller
    from hoh.store import RunStore
    from hoh.telemetry import NOT_AVAILABLE

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store
    c.dispatcher = object()

    c.note_dispatch(role="planner", run_id="r", iteration=1, attempt=0,
                    started_at="2026-09-11T10:00:00Z",
                    ended_at="2026-09-11T10:00:04Z", usage={})
    (satz,) = c.telemetry().read()
    assert satz.model == NOT_AVAILABLE


def test_a_failed_dispatch_always_carries_a_class(tmp_path):
    """`None` on a failed record reads as a failure nobody looked at."""
    from hoh.controller import Controller
    from hoh.store import RunStore
    from hoh.taxonomy import FailureClass

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store
    c.dispatcher = object()

    c.note_dispatch(role="planner", run_id="r", iteration=1, attempt=0,
                    started_at="2026-09-11T10:00:00Z",
                    ended_at="2026-09-11T10:00:04Z", outcome="failed",
                    detail="planner waits for an approval in pane w1:p2", usage={})
    (satz,) = c.telemetry().read()
    assert satz.failure_class is FailureClass.NEEDS_APPROVAL


def test_a_failure_nothing_identifies_is_unknown_and_not_absent(tmp_path):
    from hoh.controller import Controller
    from hoh.store import RunStore
    from hoh.taxonomy import FailureClass

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store
    c.dispatcher = object()

    c.note_dispatch(role="planner", run_id="r", iteration=1, attempt=0,
                    started_at="2026-09-11T10:00:00Z",
                    ended_at="2026-09-11T10:00:04Z", outcome="failed",
                    detail="something happened", usage={})
    (satz,) = c.telemetry().read()
    assert satz.failure_class is FailureClass.UNKNOWN


def test_a_successful_dispatch_carries_no_class(tmp_path):
    from hoh.controller import Controller
    from hoh.store import RunStore

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store
    c.dispatcher = object()

    c.note_dispatch(role="planner", run_id="r", iteration=1, attempt=0,
                    started_at="2026-09-11T10:00:00Z",
                    ended_at="2026-09-11T10:00:04Z", usage={})
    (satz,) = c.telemetry().read()
    assert satz.failure_class is None


def test_the_verification_record_carries_the_receipts_it_produced(tmp_path, repo_fixture=None):
    """0 receipts on a QA record of a run that wrote two is the fourth false
    green's shape, one field wide."""
    from hoh.controller import Controller
    from hoh.store import RunStore

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store
    c.dispatcher = object()

    c.note_dispatch(role="qa", run_id="r", iteration=1, attempt=1,
                    started_at="2026-09-11T10:00:00Z",
                    ended_at="2026-09-11T10:00:04Z", usage={},
                    receipts=2, discriminating=1, artefactual=0)
    (satz,) = c.telemetry().read()
    assert satz.receipts == 2
    assert satz.discriminating == 1
