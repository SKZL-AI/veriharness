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
